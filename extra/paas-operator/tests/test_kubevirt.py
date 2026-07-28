"""Tests for the KubeVirt / CDI paths (#423 W4).

Covers the three ways a VM-type CloudService (HAOS) differs from a
container one, per design §4:

  1. readiness  — virt-launcher Running != guest booted   -> get_vmis
  2. start/stop — a VM has no replicas                    -> patch_vm_run_strategy
  3. delete     — a CDI DataVolume owns the PVC           -> label_datavolumes

The recurring theme in the assertions below is FAIL-LOUD: a missing CRD
must raise, never come back as an empty list, because the platform's
readiness gate treats "no VMIs" as "not ready yet" — a swallowed error
would park every VM service in `deploying` forever with no diagnosis.
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.helm import KubectlException, KubernetesService


def _completed(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def k8s():
    return KubernetesService()


@pytest.fixture
def client():
    """FastAPI test client (F-PC-4: real key + matching header)."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


# ===================== W4.1  get_vmis / get_datavolumes =====================


class TestGetVmis:
    """VMI parsing — phase and Ready are independent signals."""

    @patch("src.services.helm.subprocess.run")
    def test_parses_phase_ready_and_agent(self, mock_run, k8s):
        mock_run.return_value = _completed(json.dumps({
            "items": [{
                "metadata": {"name": "haos-poc"},
                "status": {
                    "phase": "Running",
                    "nodeName": "ubuntuserver-z0v3w",
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "AgentConnected", "status": "True"},
                    ],
                },
            }]
        }))

        vmis = k8s.get_vmis("paas-ws-test")

        assert len(vmis) == 1
        assert vmis[0].name == "haos-poc"
        assert vmis[0].phase == "Running"
        assert vmis[0].ready is True
        assert vmis[0].node_name == "ubuntuserver-z0v3w"
        assert vmis[0].agent_connected is True

    @patch("src.services.helm.subprocess.run")
    def test_running_but_not_ready_is_not_ready(self, mock_run, k8s):
        """phase=Running with Ready=False must NOT read as ready.

        This is the whole point of reading the VMI instead of the pod: QEMU
        is up long before the guest is usable.
        """
        mock_run.return_value = _completed(json.dumps({
            "items": [{
                "metadata": {"name": "haos-poc"},
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "False"}],
                },
            }]
        }))

        vmis = k8s.get_vmis("paas-ws-test")

        assert vmis[0].phase == "Running"
        assert vmis[0].ready is False

    @patch("src.services.helm.subprocess.run")
    def test_missing_conditions_defaults_to_not_ready(self, mock_run, k8s):
        """A VMI with no conditions yet is not ready (fail-closed)."""
        mock_run.return_value = _completed(json.dumps({
            "items": [{"metadata": {"name": "haos-poc"}, "status": {"phase": "Scheduling"}}]
        }))

        vmis = k8s.get_vmis("paas-ws-test")

        assert vmis[0].ready is False
        assert vmis[0].agent_connected is False

    @patch("src.services.helm.subprocess.run")
    def test_empty_list_when_no_vmis(self, mock_run, k8s):
        mock_run.return_value = _completed(json.dumps({"items": []}))

        assert k8s.get_vmis("paas-ws-test") == []

    @patch("src.services.helm.subprocess.run")
    def test_missing_crd_raises_not_empty_list(self, mock_run, k8s):
        """🔴 The fail-loud contract: no CRD must RAISE.

        Returning [] here would make "this cluster has no KubeVirt"
        indistinguishable from "this release has no VM" — and since the
        readiness gate is fail-closed on an empty list, every VM service
        would sit in `deploying` with no error surfaced anywhere.
        """
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr='error: the server doesn\'t have a resource type "virtualmachineinstances"',
        )

        with pytest.raises(KubectlException):
            k8s.get_vmis("paas-ws-test")

    @patch("src.services.helm.subprocess.run")
    def test_passes_label_selector(self, mock_run, k8s):
        mock_run.return_value = _completed(json.dumps({"items": []}))

        k8s.get_vmis("paas-ws-test", label_selector="app.kubernetes.io/instance=svc-abc")

        args = mock_run.call_args[0][0]
        assert "--selector" in args
        assert "app.kubernetes.io/instance=svc-abc" in args

    def test_rejects_out_of_prefix_namespace(self, k8s):
        """Entry-point guard, same as every other shell-out method."""
        with pytest.raises(ValueError):
            k8s.get_vmis("kube-system")


class TestGetDataVolumes:
    """DataVolume parsing — phase + CDI's own import progress."""

    @patch("src.services.helm.subprocess.run")
    def test_parses_phase_and_progress(self, mock_run, k8s):
        mock_run.return_value = _completed(json.dumps({
            "items": [{
                "metadata": {"name": "svc-abc-root"},
                "status": {"phase": "ImportInProgress", "progress": "42.31%"},
            }]
        }))

        dvs = k8s.get_datavolumes("paas-ws-test")

        assert dvs[0].name == "svc-abc-root"
        assert dvs[0].phase == "ImportInProgress"
        assert dvs[0].progress == "42.31%"

    @patch("src.services.helm.subprocess.run")
    def test_progress_absent_before_importer_starts(self, mock_run, k8s):
        mock_run.return_value = _completed(json.dumps({
            "items": [{"metadata": {"name": "svc-abc-root"}, "status": {"phase": "Pending"}}]
        }))

        assert k8s.get_datavolumes("paas-ws-test")[0].progress is None

    @patch("src.services.helm.subprocess.run")
    def test_missing_crd_raises(self, mock_run, k8s):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr='error: the server doesn\'t have a resource type "datavolumes"',
        )

        with pytest.raises(KubectlException):
            k8s.get_datavolumes("paas-ws-test")


# ===================== W4.3  patch_vm_run_strategy =====================


class TestPatchVmRunStrategy:

    @patch("src.services.helm.subprocess.run")
    def test_halted_patch_shape(self, mock_run, k8s):
        mock_run.return_value = _completed("")

        k8s.patch_vm_run_strategy("paas-ws-test", "svc-abc", "Halted")

        args = mock_run.call_args[0][0]
        assert "patch" in args and "virtualmachine" in args and "svc-abc" in args
        assert "--type=merge" in args
        patch_idx = args.index("--patch") + 1
        assert json.loads(args[patch_idx]) == {"spec": {"runStrategy": "Halted"}}

    @patch("src.services.helm.subprocess.run")
    def test_always_patch_shape(self, mock_run, k8s):
        mock_run.return_value = _completed("")

        k8s.patch_vm_run_strategy("paas-ws-test", "svc-abc", "Always")

        args = mock_run.call_args[0][0]
        patch_idx = args.index("--patch") + 1
        assert json.loads(args[patch_idx]) == {"spec": {"runStrategy": "Always"}}

    @patch("src.services.helm.subprocess.run")
    def test_rejects_unknown_run_strategy(self, mock_run, k8s):
        """KubeVirt silently ignores an unknown runStrategy — the API would
        report success while the VM never changed state. Reject up front."""
        with pytest.raises(ValueError, match="Unsupported runStrategy"):
            k8s.patch_vm_run_strategy("paas-ws-test", "svc-abc", "Manual")

        mock_run.assert_not_called()

    def test_rejects_out_of_prefix_namespace(self, k8s):
        with pytest.raises(ValueError):
            k8s.patch_vm_run_strategy("kube-system", "svc-abc", "Halted")


# ===================== W4.4  list/label_datavolumes =====================


class TestLabelDataVolumes:

    @patch("src.services.helm.subprocess.run")
    def test_labels_each_datavolume(self, mock_run, k8s):
        mock_run.return_value = _completed("")

        k8s.label_datavolumes(
            "paas-ws-test", ["dv-a", "dv-b"],
            {"paas.woowtech.io/orphaned-at": "1753800000"},
        )

        assert mock_run.call_count == 2
        first = mock_run.call_args_list[0][0][0]
        assert "label" in first and "datavolume" in first and "dv-a" in first
        assert "--overwrite" in first
        assert "paas.woowtech.io/orphaned-at=1753800000" in first

    @patch("src.services.helm.subprocess.run")
    def test_rejects_illegal_label_value_before_any_call(self, mock_run, k8s):
        """One bad value used to silently drop the whole batch (operator #208)."""
        with pytest.raises(ValueError):
            k8s.label_datavolumes(
                "paas-ws-test", ["dv-a"],
                {"paas.woowtech.io/orphaned-at": "2026-07-28T12:00:00Z"},  # ':' illegal
            )

        mock_run.assert_not_called()

    @patch("src.services.helm.subprocess.run")
    def test_one_failure_does_not_abort_the_rest(self, mock_run, k8s):
        """Teardown is best-effort per object — mirrors label_pvcs."""
        mock_run.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom"),
            _completed(""),
        ]

        k8s.label_datavolumes("paas-ws-test", ["dv-a", "dv-b"], {"k": "v"})

        assert mock_run.call_count == 2

    @patch("src.services.helm.subprocess.run")
    def test_list_datavolumes_parses_names(self, mock_run, k8s):
        mock_run.return_value = _completed("dv-a dv-b\n")

        assert k8s.list_datavolumes("paas-ws-test") == ["dv-a", "dv-b"]

    @patch("src.services.helm.subprocess.run")
    def test_list_datavolumes_empty(self, mock_run, k8s):
        mock_run.return_value = _completed("\n")

        assert k8s.list_datavolumes("paas-ws-test") == []


# ===================== W4.2  status API include_vm =====================


class TestReleaseStatusIncludeVm:

    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_default_does_not_query_vm_resources(self, mock_helm, mock_k8s, client):
        """Backward compatibility: existing callers pay nothing and see [] ."""
        mock_helm.get.return_value = _release_info()
        mock_k8s.get_pods.return_value = []

        response = client.get("/api/releases/paas-ws-test/svc-abc/status")

        assert response.status_code == 200
        body = response.json()
        assert body["vmis"] == []
        assert body["datavolumes"] == []
        assert body["vm_retrieval_error"] is None
        mock_k8s.get_vmis.assert_not_called()
        mock_k8s.get_datavolumes.assert_not_called()

    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_include_vm_returns_vmis_and_datavolumes(self, mock_helm, mock_k8s, client):
        from src.models.schemas import DataVolumeInfo, VmiInfo

        mock_helm.get.return_value = _release_info()
        mock_k8s.get_pods.return_value = []
        mock_k8s.get_vmis.return_value = [
            VmiInfo(name="svc-abc", phase="Running", ready=True)
        ]
        mock_k8s.get_datavolumes.return_value = [
            DataVolumeInfo(name="svc-abc-root", phase="Succeeded", progress="100.0%")
        ]

        response = client.get(
            "/api/releases/paas-ws-test/svc-abc/status", params={"include_vm": "true"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["vmis"][0]["ready"] is True
        assert body["datavolumes"][0]["progress"] == "100.0%"
        # Scoped to this release only — a workspace ns is shared (#281).
        mock_k8s.get_vmis.assert_called_once_with(
            namespace="paas-ws-test",
            label_selector="app.kubernetes.io/instance=svc-abc",
        )

    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_vm_lookup_failure_is_surfaced_not_swallowed(
        self, mock_helm, mock_k8s, client
    ):
        """🔴 Empty vmis + an error must be distinguishable from empty vmis.

        The Odoo readiness gate is fail-closed on an empty list; without
        vm_retrieval_error a broken lookup looks exactly like "still
        importing" and the service never leaves `deploying`.
        """
        mock_helm.get.return_value = _release_info()
        mock_k8s.get_pods.return_value = []
        mock_k8s.get_vmis.side_effect = KubectlException(
            message="no CRD", command="kubectl get vmi", stderr="not found",
        )

        response = client.get(
            "/api/releases/paas-ws-test/svc-abc/status", params={"include_vm": "true"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["vmis"] == []
        assert body["vm_retrieval_error"] is not None
        # The pod half must still be reported — the two lookups are independent.
        assert body["pod_retrieval_error"] is None


def _release_info():
    from src.models.schemas import ReleaseInfo, ReleaseStatus

    return ReleaseInfo(
        name="svc-abc",
        namespace="paas-ws-test",
        revision=1,
        status=ReleaseStatus.DEPLOYED,
        chart="haos-0.1.0",
        app_version="18.1",
        updated="2026-07-28T00:00:00Z",
    )
