"""#1156：kubectl Pod metadata 必須穿過 status API，而非只留在 getter。"""
import json
import subprocess
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import PodInfo, ReleaseInfo, ReleaseStatus


@pytest.mark.parametrize("metadata, expected", [
    ({"deletionTimestamp": "2026-09-29T00:00:00Z"}, "2026-09-29T00:00:00Z"),
    ({"deletionTimestamp": None}, None),
    ({}, None),
])
def test_status_exposes_pod_deletion_timestamp(metadata, expected):
    pods = {"items": [{
        "metadata": {"name": "old-odoo", **metadata},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"ready": False, "restartCount": 0}],
        },
    }]}
    release = ReleaseInfo(
        name="svc-1156", namespace="paas-ws-test", revision=1,
        status=ReleaseStatus.DEPLOYED, chart="odoo", app_version="18.0",
        updated="2026-09-29T00:00:00Z",
    )
    with patch("src.main.settings.api_key", "test-1156"), \
            patch("src.api.releases.helm_service.get", return_value=release), \
            patch("subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=json.dumps(pods), stderr="",
            )):
        response = TestClient(app, headers={"X-API-Key": "test-1156"}).get(
            "/api/releases/paas-ws-test/svc-1156/status",
        )
    assert response.status_code == 200
    body = response.json()
    assert body["pod_retrieval_error"] is None
    assert body["pods"][0]["deletion_timestamp"] == expected
    assert body["pods"][0]["phase"] == "Running"
    assert body["pods"][0]["ready"] == "0/1"


def test_legacy_pod_info_needs_no_deletion_timestamp():
    pod = PodInfo(name="odoo", phase="Running", ready="1/1", restarts=0, age="1m")
    assert pod.deletion_timestamp is None
