"""Integration: the KubeVirt/CDI CR-reading contract (#423 W4).

Split in two by design:

**Always-on (any cluster, CRDs or not)** — the *fail-loud* contract. The unit
suite asserts these against a mocked ``subprocess.run``; here they run against
a real ``kubectl`` on a real API server, which is the only way to prove the
error actually surfaces as a ``KubectlException`` rather than, say, an empty
JSON document that quietly parses to ``[]``. That distinction is load-bearing:
the platform readiness gate is fail-CLOSED on an empty VMI list, so a
swallowed CRD error would park every VM service in ``deploying`` forever.

**KubeVirt-only** — skipped unless the cluster actually has the CRDs. These
cover the parsing/patching happy paths against real objects. docker-desktop
has no KubeVirt, so they skip there; run them on a cluster that does (the
#423 W0 install put KubeVirt v1.8.0 + CDI v1.64.0 on woow-k3s) using a
``dev-sandbox-*`` namespace.
"""
import subprocess

import pytest

from src.services.helm import KubectlException, KubernetesService

# Every module in this directory must carry the marker itself — conftest's
# module-level pytestmark does NOT propagate, and pytest.ini defaults to
# `-m "not integration"`, so an unmarked file is silently DESELECTED
# (collected, never run, reported green).
pytestmark = pytest.mark.integration


def _has_crd(name: str) -> bool:
    try:
        return subprocess.run(
            ["kubectl", "get", "crd", name],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


requires_kubevirt = pytest.mark.skipif(
    not _has_crd("virtualmachineinstances.kubevirt.io"),
    reason="cluster has no KubeVirt CRDs (expected on docker-desktop)",
)
requires_cdi = pytest.mark.skipif(
    not _has_crd("datavolumes.cdi.kubevirt.io"),
    reason="cluster has no CDI CRDs (expected on docker-desktop)",
)


def _write_probe_chart(chart_dir) -> str:
    """Minimal real Helm chart so ``/status`` has an actual release to read.

    A raw kubectl-applied manifest is not enough: the endpoint calls
    ``helm_service.get`` first, which 500s without a real release.
    """
    chart_dir.mkdir(parents=True, exist_ok=True)
    (chart_dir / "Chart.yaml").write_text(
        "apiVersion: v2\nname: vm-status-probe\nversion: 0.1.0\n"
    )
    templates = chart_dir / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "cm.yaml").write_text(
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: {{ .Release.Name }}-cfg\n"
        "  labels:\n"
        "    app.kubernetes.io/instance: {{ .Release.Name }}\n"
        "data:\n"
        "  hello: world\n"
    )
    return str(chart_dir)


@pytest.fixture
def k8s():
    return KubernetesService()


class TestFailLoudWithoutCrds:
    """Runs everywhere. On a CRD-less cluster these prove the fail-loud path;
    on a KubeVirt cluster they prove the same calls simply succeed."""

    def test_get_vmis_never_silently_returns_empty(self, k8s, test_namespace):
        """Either the CRD exists and we get a list, or we get an exception.

        What must NEVER happen is "no CRD" quietly looking like "no VMs".
        """
        if _has_crd("virtualmachineinstances.kubevirt.io"):
            assert k8s.get_vmis(test_namespace) == []
        else:
            with pytest.raises(KubectlException):
                k8s.get_vmis(test_namespace)

    def test_get_datavolumes_never_silently_returns_empty(self, k8s, test_namespace):
        if _has_crd("datavolumes.cdi.kubevirt.io"):
            assert k8s.get_datavolumes(test_namespace) == []
        else:
            with pytest.raises(KubectlException):
                k8s.get_datavolumes(test_namespace)

    def test_status_endpoint_surfaces_vm_error_without_500(
        self, app_client, test_namespace, tmp_path,
    ):
        """?include_vm=true on a CRD-less cluster: 200 + vm_retrieval_error.

        The VM lookup failing must not take down the whole status response —
        the pod half still has to be reported, or the platform loses sight of
        every container service the moment KubeVirt is missing.

        Also pins backward compatibility: the SAME release without the flag
        must return 200 with empty VM fields and no error.
        """
        chart = _write_probe_chart(tmp_path / "chart")
        release = "vm-status-probe"
        r = app_client.post("/api/releases", json={
            "namespace": test_namespace, "name": release, "chart": chart,
        })
        assert r.status_code == 201, r.text

        # --- default (no include_vm): unchanged behaviour -------------------
        r = app_client.get(f"/api/releases/{test_namespace}/{release}/status")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["vmis"] == []
        assert body["datavolumes"] == []
        assert body["vm_retrieval_error"] is None

        # --- opt-in: VM fields present, error surfaced not swallowed -------
        r = app_client.get(
            f"/api/releases/{test_namespace}/{release}/status",
            params={"include_vm": "true"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["vmis"] == []
        if _has_crd("virtualmachineinstances.kubevirt.io"):
            assert body["vm_retrieval_error"] is None
        else:
            assert body["vm_retrieval_error"] is not None, (
                "a missing KubeVirt CRD must surface as vm_retrieval_error — "
                "otherwise an empty vmis list is indistinguishable from "
                "'still importing' and the fail-closed readiness gate never "
                "releases the service"
            )
        # The pod half is independent and must still be reported.
        assert body["pod_retrieval_error"] is None


@requires_kubevirt
class TestVirtualMachineLifecycle:
    """Happy paths against real KubeVirt objects (skipped on docker-desktop)."""

    def test_patch_run_strategy_round_trip(self, k8s, test_namespace):
        """Halted → Always → Halted against a real VM object.

        Uses a VM that is never started (runStrategy Halted from birth, no
        DataVolume) so the test costs nothing but the CR round-trip.
        """
        vm_name = "itest-vm"
        manifest = {
            "apiVersion": "kubevirt.io/v1",
            "kind": "VirtualMachine",
            "metadata": {
                "name": vm_name,
                "namespace": test_namespace,
                "labels": {"app.kubernetes.io/instance": "itest"},
            },
            "spec": {
                "runStrategy": "Halted",
                "template": {
                    "metadata": {"labels": {"app.kubernetes.io/instance": "itest"}},
                    "spec": {
                        "domain": {
                            "resources": {
                                "requests": {"memory": "64Mi"},
                                "limits": {"memory": "64Mi"},
                            },
                            "devices": {"disks": []},
                        },
                        "volumes": [],
                    },
                },
            },
        }
        k8s.apply_manifest(manifest)
        try:
            vms = k8s.get_virtualmachines(
                test_namespace, label_selector="app.kubernetes.io/instance=itest",
            )
            assert [v["name"] for v in vms] == [vm_name]
            assert vms[0]["run_strategy"] == "Halted"

            k8s.patch_vm_run_strategy(test_namespace, vm_name, "Always")
            vms = k8s.get_virtualmachines(test_namespace)
            assert vms[0]["run_strategy"] == "Always"

            k8s.patch_vm_run_strategy(test_namespace, vm_name, "Halted")
            vms = k8s.get_virtualmachines(test_namespace)
            assert vms[0]["run_strategy"] == "Halted"
        finally:
            subprocess.run(
                ["kubectl", "delete", "vm", vm_name, "-n", test_namespace,
                 "--ignore-not-found", "--wait=false"],
                capture_output=True, timeout=30,
            )


@requires_cdi
class TestDataVolumeOrphanLabeling:
    """DataVolume orphan-stamp path against real CDI objects."""

    def test_list_and_label_datavolumes(self, k8s, test_namespace):
        dv_name = "itest-dv"
        manifest = {
            "apiVersion": "cdi.kubevirt.io/v1beta1",
            "kind": "DataVolume",
            "metadata": {
                "name": dv_name,
                "namespace": test_namespace,
                "labels": {"app.kubernetes.io/instance": "itest"},
            },
            "spec": {
                "source": {"blank": {}},
                "storage": {
                    "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "1Gi"}},
                },
            },
        }
        k8s.apply_manifest(manifest)
        try:
            names = k8s.list_datavolumes(
                test_namespace, label_selector="app.kubernetes.io/instance=itest",
            )
            assert names == [dv_name]

            k8s.label_datavolumes(
                test_namespace, names,
                {"paas.woowtech.io/orphaned-at": "1753800000"},
            )

            out = subprocess.run(
                ["kubectl", "get", "datavolume", dv_name, "-n", test_namespace,
                 "-o", "jsonpath={.metadata.labels.paas\\.woowtech\\.io/orphaned-at}"],
                capture_output=True, text=True, timeout=30,
            )
            assert out.stdout.strip() == "1753800000"
        finally:
            subprocess.run(
                ["kubectl", "delete", "datavolume", dv_name, "-n", test_namespace,
                 "--ignore-not-found", "--wait=false"],
                capture_output=True, timeout=60,
            )
