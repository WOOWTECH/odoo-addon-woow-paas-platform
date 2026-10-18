"""Opt-in existing-release logs: real K8s, no create/exec/cleanup side effects.

RELEASE_LOGS_INTEGRATION=1 RELEASE_LOGS_NAMESPACE=paas-ws-...
RELEASE_LOGS_RELEASE=svc-... pytest -m integration tests/test_release_logs_readonly_integration.py

RELEASE_LOGS_IMPERSONATE_OPERATOR=1 also verifies the deployed operator ServiceAccount.
Always adds explicit --context woow-k3s. Kept outside tests/integration/ because
that directory's autouse retained-PV cleanup can mutate the shared cluster.
"""
import os
import re
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from src.api import releases
from src.config import settings
from src.main import app
from src.services.release_logs import LOG_BYTE_LIMIT, ReleaseLogReader

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("RELEASE_LOGS_INTEGRATION") != "1",
    reason="Explicit read-only integration opt-in required",
)]


def test_existing_release_live_logs_are_scoped_and_bounded(monkeypatch):
    namespace = os.environ["RELEASE_LOGS_NAMESPACE"]
    release = os.environ["RELEASE_LOGS_RELEASE"]
    assert namespace.startswith("paas-ws-")
    monkeypatch.setattr(settings, "namespace_prefix", "paas-ws-stg-" if namespace.startswith("paas-ws-stg-") else "paas-ws-")
    monkeypatch.setattr(settings, "api_key", "local-readonly-logs")
    monkeypatch.setattr(releases, "release_log_reader", ReleaseLogReader())
    real_run = subprocess.run
    commands = []

    def readonly_run(command, *args, **kwargs):
        assert command[0] == "kubectl" and command[1] in ("get", "logs")
        assert command[command.index("--namespace") + 1] == namespace
        if command[1] == "get":
            assert command[2] == "pods"
            assert command[command.index("--selector") + 1] == f"app.kubernetes.io/instance={release}"
        else:
            assert "--timestamps=true" in command and "--tail=10" in command
        commands.append(command)
        context_args = ["--context", "woow-k3s"]
        if os.environ.get("RELEASE_LOGS_IMPERSONATE_OPERATOR") == "1":
            operator_namespace = "paas-stg-system" if namespace.startswith("paas-ws-stg-") else "paas-system"
            context_args.append(f"--as=system:serviceaccount:{operator_namespace}:paas-operator")
        return real_run([command[0], *context_args, *command[1:]], *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", readonly_run)
    http = TestClient(app, headers={"X-API-Key": "local-readonly-logs"})
    started = time.monotonic()
    response = http.get(f"/api/releases/{namespace}/{release}/logs?tail_lines=10")
    assert response.status_code == 200
    result = response.json()
    assert result["available"], result["reason"]
    assert time.monotonic() - started < 10
    assert result["sources"] and result["pod"] and result["container"]
    assert 0 < len(result["text"].encode()) <= LOG_BYTE_LIMIT
    assert re.match(r"^\d{4}-\d\d-\d\dT", result["text"])
    assert len(commands) == 2
    selected = http.get(f"/api/releases/{namespace}/{release}/logs", params={
        "pod": result["pod"], "container": result["container"], "tail_lines": 10,
    })
    assert selected.status_code == 200 and selected.json()["available"]
    before = len(commands)
    denied = http.get(f"/api/releases/{namespace}/{release}/logs", params={
        "pod": "not-this-release-0", "tail_lines": 10,
    })
    assert denied.status_code == 200 and denied.json()["reason"] == "source_not_found"
    assert denied.json()["text"] == ""
    assert len(commands) == before + 1 and commands[-1][1] == "get"
