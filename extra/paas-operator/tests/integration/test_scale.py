"""End-to-end: real HTTP → real kubectl → real K8s state changes.

These would have caught BUG-7/8/9 before they hit staging:

- ``test_scale_statefulset_only`` exercises the StatefulSet branch
  (BUG-7 — Bitnami releases were 404'ing because only Deployments were
  queried).
- ``params={"replicas": ...}`` everywhere documents the wire contract
  (BUG-8 — client was sending it as a body and the operator's Query default
  silently kicked in).
- When run with the operator's real ServiceAccount in CI, the kubectl
  scale call exercises the ``deployments/scale`` and ``statefulsets/scale``
  subresource permissions (BUG-9).
"""
import subprocess

import pytest


pytestmark = pytest.mark.integration


def _replicas(kind: str, name: str, namespace: str) -> int:
    out = subprocess.check_output([
        "kubectl", "get", kind, name, "-n", namespace,
        "-o", "jsonpath={.spec.replicas}",
    ])
    return int(out.decode() or "0")


class TestScaleDeployment:

    def test_scale_to_zero(self, app_client, deployment_release):
        ns = deployment_release["namespace"]
        rel = deployment_release["release"]
        name = deployment_release["name"]
        assert _replicas("deployment", name, ns) == 1

        r = app_client.post(
            f"/api/releases/{ns}/{rel}/scale",
            params={"replicas": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Scaled 1 workload" in body["message"]
        assert body["deployments"] == name
        assert body["statefulsets"] == ""

        assert _replicas("deployment", name, ns) == 0

    def test_scale_back_up(self, app_client, deployment_release):
        ns = deployment_release["namespace"]
        rel = deployment_release["release"]
        name = deployment_release["name"]

        r0 = app_client.post(f"/api/releases/{ns}/{rel}/scale",
                             params={"replicas": 0})
        assert r0.status_code == 200

        r1 = app_client.post(f"/api/releases/{ns}/{rel}/scale",
                             params={"replicas": 1})
        assert r1.status_code == 200

        assert _replicas("deployment", name, ns) == 1


class TestScaleStatefulSet:
    """BUG-7 regression: Bitnami charts ship as StatefulSets."""

    def test_scale_to_zero(self, app_client, statefulset_release):
        ns = statefulset_release["namespace"]
        rel = statefulset_release["release"]
        name = statefulset_release["name"]
        assert _replicas("statefulset", name, ns) == 1

        r = app_client.post(
            f"/api/releases/{ns}/{rel}/scale",
            params={"replicas": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Scaled 1 workload" in body["message"]
        assert body["statefulsets"] == name
        assert body["deployments"] == ""

        assert _replicas("statefulset", name, ns) == 0

    def test_scale_back_up(self, app_client, statefulset_release):
        ns = statefulset_release["namespace"]
        rel = statefulset_release["release"]
        name = statefulset_release["name"]

        app_client.post(f"/api/releases/{ns}/{rel}/scale",
                        params={"replicas": 0})
        r1 = app_client.post(f"/api/releases/{ns}/{rel}/scale",
                             params={"replicas": 1})
        assert r1.status_code == 200
        assert _replicas("statefulset", name, ns) == 1


class TestScaleMixed:
    """Real releases often pair an app Deployment with a backing StatefulSet."""

    def test_scale_both_kinds(self, app_client, mixed_release):
        ns = mixed_release["namespace"]
        rel = mixed_release["release"]
        dep = mixed_release["deployment_name"]
        sts = mixed_release["statefulset_name"]

        r = app_client.post(
            f"/api/releases/{ns}/{rel}/scale",
            params={"replicas": 0},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Scaled 2 workload" in body["message"]
        assert body["deployments"] == dep
        assert body["statefulsets"] == sts

        assert _replicas("deployment", dep, ns) == 0
        assert _replicas("statefulset", sts, ns) == 0


class TestScaleErrors:

    def test_404_when_no_workloads(self, app_client, test_namespace):
        """No workload of ANY kind → 404.

        #423 W4.3 doubles as a real-cluster check of the VirtualMachine
        branch's CRD handling: docker-desktop has no KubeVirt, so
        ``get_virtualmachines`` raises inside the endpoint. The 404 below
        only appears if that KubectlException is caught and folded into
        "no workloads" — if it escaped, this would be a 500. That is the
        exact contract drift a mocked test cannot catch.
        """
        r = app_client.post(
            f"/api/releases/{test_namespace}/nonexistent-rel/scale",
            params={"replicas": 0},
        )
        assert r.status_code == 404, r.text
        assert "No deployments, statefulsets or virtualmachines" in r.json()["detail"]

    def test_replicas_in_body_is_ignored_so_query_is_authoritative(
        self, app_client, deployment_release,
    ):
        """BUG-8 contract: ``replicas`` is read from the URL query string.

        Sending it in the body alone (as the pre-fix Odoo client did) silently
        falls back to ``Query(default=0)`` — i.e. always scales to 0. This test
        documents the contract: query wins, body is decoration.
        """
        ns = deployment_release["namespace"]
        rel = deployment_release["release"]
        name = deployment_release["name"]

        # Body says 5, query says 1. Server must honor the query.
        r = app_client.post(
            f"/api/releases/{ns}/{rel}/scale",
            params={"replicas": 1},
            json={"replicas": 5},
        )
        assert r.status_code == 200
        assert _replicas("deployment", name, ns) == 1


# --- #423 W6.3: 混合拓撲（容器 + VM）-----------------------------------------
# 這些 helper 的單一真源在 test_kubevirt_crd_contract：`REQUIRE_KUBEVIRT=1` 時把
# skip 升級成 fail，正是為了不讓「驗收 VM 路徑」被一個安靜的 skip 冒充通過。
from .test_kubevirt_crd_contract import _has_crd, _skip_or_fail  # noqa: E402
from .conftest import _apply_manifest, _make_deployment  # noqa: E402

_VM_LABEL = "app.kubernetes.io/instance"


def _vm_manifest(namespace: str, name: str, release: str) -> dict:
    """永不啟動的最小 VM（runStrategy Halted、無 DataVolume），只付 CR 往返成本。"""
    return {
        "apiVersion": "kubevirt.io/v1",
        "kind": "VirtualMachine",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {_VM_LABEL: release},
        },
        "spec": {
            "runStrategy": "Halted",
            "template": {
                "metadata": {"labels": {_VM_LABEL: release}},
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


def _apply_vm(manifest: dict) -> None:
    import json as _json
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=_json.dumps(manifest).encode(),
        check=True, capture_output=True, timeout=30,
    )


def _run_strategy(namespace: str, name: str) -> str:
    return subprocess.check_output([
        "kubectl", "get", "vm", name, "-n", namespace,
        "-o", "jsonpath={.spec.runStrategy}",
    ]).decode()


def _delete_vm(namespace: str, name: str) -> None:
    subprocess.run(
        ["kubectl", "delete", "vm", name, "-n", namespace,
         "--ignore-not-found", "--wait=false"],
        capture_output=True, timeout=30,
    )


class TestScaleMixedTopology:
    """#423 W6.3：VM 的處理不得再被「有沒有容器 workload」擋掉。

    缺陷形狀：舊 gate 是 `if not deployments and not statefulsets:`，HAOS chart
    0.1.2 加上 xff-proxy Deployment 後永遠不成立 → suspend 只 scale 了 proxy，
    VM 照跑照計費，端點還回 200。單元測試以 mock 驗接線；這裡驗真叢集行為。
    """

    def test_container_only_release_survives_unconditional_vm_lookup(
        self, app_client, deployment_release
    ):
        """容器 release 在**沒有 KubeVirt 的真叢集**上照樣 scale 成功。

        這是 docker-desktop 上最有價值的一條：拆掉 gate 之後，每一次 scale 都會
        多發一次必定失敗的 `kubectl get virtualmachines`。這條證明那個失敗確實被
        CRD-absent 容忍吸收，而不是讓全平台的容器服務 suspend 變成 500——mock
        測得到「有 catch」，測不到真 kubectl 回的 stderr 是否真的落進那個收窄
        判別式（is_crd_absent_error 是字串比對，只有真 kubectl 說了算）。
        """
        ns = deployment_release["namespace"]
        rel = deployment_release["release"]
        name = deployment_release["name"]
        assert _replicas("deployment", name, ns) == 1

        r = app_client.post(f"/api/releases/{ns}/{rel}/scale", params={"replicas": 0})

        assert r.status_code == 200, r.text
        assert "Scaled 1 workload" in r.json()["message"]
        assert _replicas("deployment", name, ns) == 0
        if not _has_crd("virtualmachines.kubevirt.io"):
            # 明確記錄本次真正走到的是哪條分支，避免日後誤讀成「有 KubeVirt 也綠」。
            assert r.json()["virtualmachines"] == ""

    def test_vm_only_release_halts_via_endpoint(self, app_client, test_namespace):
        """VM-only（W4 既有行為回歸）：真 VM → runStrategy Halted。"""
        _skip_or_fail("virtualmachines.kubevirt.io", "KubeVirt")
        release, vm_name = "vm-only-rel", "vm-only"
        _apply_vm(_vm_manifest(test_namespace, vm_name, release))
        try:
            r = app_client.post(
                f"/api/releases/{test_namespace}/{release}/scale",
                params={"replicas": 1},
            )
            assert r.status_code == 200, r.text
            assert r.json()["virtualmachines"] == vm_name
            assert _run_strategy(test_namespace, vm_name) == "Always"

            r = app_client.post(
                f"/api/releases/{test_namespace}/{release}/scale",
                params={"replicas": 0},
            )
            assert r.status_code == 200, r.text
            assert _run_strategy(test_namespace, vm_name) == "Halted"
        finally:
            _delete_vm(test_namespace, vm_name)

    def test_mixed_deployment_and_vm_scales_both(self, app_client, test_namespace):
        """🔴 本 PR 的核心回歸：Deployment 與 VM 同 release → 兩半都要動。

        修復前這條會綠一半——Deployment 收到 0，VM 的 runStrategy 原封不動停在
        Always，而端點回 200 說「Scaled 1 workload」。
        """
        _skip_or_fail("virtualmachines.kubevirt.io", "KubeVirt")
        release, dep_name, vm_name = "mixed-rel", "mixed-proxy", "mixed-vm"
        _apply_manifest(_make_deployment(test_namespace, dep_name, release))
        _apply_vm(_vm_manifest(test_namespace, vm_name, release))
        try:
            # 先讓 VM 處於 Always，才能證明 suspend 真的動了它。
            subprocess.run(
                ["kubectl", "patch", "vm", vm_name, "-n", test_namespace,
                 "--type", "merge", "-p", '{"spec":{"runStrategy":"Always"}}'],
                check=True, capture_output=True, timeout=30,
            )
            assert _replicas("deployment", dep_name, test_namespace) == 1
            assert _run_strategy(test_namespace, vm_name) == "Always"

            r = app_client.post(
                f"/api/releases/{test_namespace}/{release}/scale",
                params={"replicas": 0},
            )

            assert r.status_code == 200, r.text
            body = r.json()
            assert "Scaled 2 workload" in body["message"], (
                f"混合拓撲下兩種 workload 都該被處理，got {body!r}"
            )
            assert body["deployments"] == dep_name
            assert body["virtualmachines"] == vm_name
            assert _replicas("deployment", dep_name, test_namespace) == 0
            assert _run_strategy(test_namespace, vm_name) == "Halted", (
                "VM 沒有被 Halted —— 這就是 stg W6.3 的現場：帳面 suspended、"
                "VM 照跑照計費"
            )
        finally:
            _delete_vm(test_namespace, vm_name)
