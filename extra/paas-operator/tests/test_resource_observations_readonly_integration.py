"""Opt-in read-only observations against existing workloads, with no fixtures/CRUD.

RESOURCE_OBSERVATIONS_INTEGRATION=1 RESOURCE_OBSERVATIONS_NAMESPACE=paas-ws-...
RESOURCE_OBSERVATIONS_RELEASE_NAMES=svc-one,svc-two RESOURCE_PROMETHEUS_URL=http://127.0.0.1:...
pytest -m integration tests/test_resource_observations_readonly_integration.py

Kept outside tests/integration/ because that directory has an autouse retained-PV
cleanup fixture that can mutate the cluster even for a read-only test.
Uses the current kubectl context. Explicit operator namespace validation still
applies. This verifies telemetry only, not the existing deploy/upgrade CRUD suite.
"""
import os
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.main import app
from src.api import resource_observations
from src.services.resource_observations import ResourceCollector

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("RESOURCE_OBSERVATIONS_INTEGRATION") != "1", reason="Explicit read-only integration opt-in required")]


def test_existing_release_live_resource_observations(monkeypatch):
    namespace = os.environ["RESOURCE_OBSERVATIONS_NAMESPACE"]
    names = os.environ["RESOURCE_OBSERVATIONS_RELEASE_NAMES"].split(",")
    collector = ResourceCollector()
    real_run = subprocess.run
    def readonly_run(command, *args, **kwargs):
        assert command[:2] == ["kubectl", "get"], "Read-only telemetry must never invoke a mutation"
        assert "--namespace" in command and namespace in command
        return real_run(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", readonly_run)
    monkeypatch.setattr(resource_observations, "collector", collector)
    monkeypatch.setattr(settings, "api_key", "local-readonly-integration")
    client = TestClient(app, headers={"X-API-Key": "local-readonly-integration"})
    started = time.monotonic()
    response = client.post(f"/api/namespaces/{namespace}/resource-observations", json={"release_names": names})
    assert time.monotonic() - started < 10, "Collector exceeded the portal request budget"
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result["services"]) == set(names)
    assert result["observed_at"]
    for observation in result["services"].values():
        assert observation["pods"]["running"] > 0
        assert observation["cpu"]["status"] == "ok", observation
        assert observation["memory"]["status"] == "ok", observation
        assert observation["cpu"]["sampled_at"]
        assert observation["memory"]["usage"] > 0
        assert observation["cpu"]["usage"] >= 0
        assert observation["storage"]["usage"] is not None, observation
        assert observation["storage"]["capacity"] >= observation["storage"]["usage"]
        assert observation["ephemeral"]["usage"] is None
    assert collector.collect(namespace, names) == result
