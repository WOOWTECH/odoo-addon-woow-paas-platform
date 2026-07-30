"""Tests for release scale API endpoint.

Covers Deployment + StatefulSet workloads since Bitnami-based releases
(Redis, PostgreSQL, Odoo, ...) ship as StatefulSets — without the
StatefulSet branch the operator returned 404 for those releases (BUG-7).
"""
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.helm import KubectlException


@pytest.fixture
def client():
    """Create FastAPI test client (F-PC-4: real key + matching header)."""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestScaleRelease:
    """Test POST /api/releases/{namespace}/{name}/scale endpoint."""

    @patch("src.api.releases.k8s_service")
    def test_scale_down_deployment_only(self, mock_k8s, client):
        """Deployment release scales to 0 (suspend)."""
        mock_k8s.get_deployments.return_value = [{"name": "svc-test-main"}]
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.scale_deployment.return_value = None
        # #423 W6.3：VM lookup 現在無條件執行，必須顯式 stub 成空。
        # 不 stub 的話 MagicMock 預設值是 truthy（且 __iter__ 給空迭代），
        # 會靜默變成「有 VM 但一顆都沒處理」的假語意。
        mock_k8s.get_virtualmachines.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 1 workload" in data["message"]
        assert data["deployments"] == "svc-test-main"
        assert data["statefulsets"] == ""
        mock_k8s.scale_deployment.assert_called_once_with(
            "paas-ws-test", "svc-test-main", 0,
        )
        mock_k8s.scale_statefulset.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_scale_up_multiple_deployments(self, mock_k8s, client):
        """Release with multiple Deployments scales them all to 1."""
        mock_k8s.get_deployments.return_value = [
            {"name": "svc-test-main"},
            {"name": "svc-test-worker"},
        ]
        mock_k8s.get_statefulsets.return_value = []
        # #423 W6.3：VM lookup 現在無條件執行，必須顯式 stub 成空。
        # 不 stub 的話 MagicMock 預設值是 truthy（且 __iter__ 給空迭代），
        # 會靜默變成「有 VM 但一顆都沒處理」的假語意。
        mock_k8s.get_virtualmachines.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 1},
        )

        assert response.status_code == 200
        assert "Scaled 2 workload" in response.json()["message"]
        assert mock_k8s.scale_deployment.call_count == 2
        mock_k8s.scale_statefulset.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_scale_statefulset_only(self, mock_k8s, client):
        """Bitnami-style release (StatefulSet only) — BUG-7 regression."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = [
            {"name": "svc-redis-master"},
        ]
        mock_k8s.scale_statefulset.return_value = None
        # #423 W6.3：VM lookup 現在無條件執行，必須顯式 stub 成空。
        # 不 stub 的話 MagicMock 預設值是 truthy（且 __iter__ 給空迭代），
        # 會靜默變成「有 VM 但一顆都沒處理」的假語意。
        mock_k8s.get_virtualmachines.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-redis/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 1 workload" in data["message"]
        assert data["statefulsets"] == "svc-redis-master"
        mock_k8s.scale_statefulset.assert_called_once_with(
            "paas-ws-test", "svc-redis-master", 0,
        )
        mock_k8s.scale_deployment.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_scale_mixed_deployment_and_statefulset(self, mock_k8s, client):
        """Release with both kinds (e.g. Odoo + Postgres) scales both."""
        mock_k8s.get_deployments.return_value = [{"name": "svc-app"}]
        mock_k8s.get_statefulsets.return_value = [{"name": "svc-db"}]
        # #423 W6.3：VM lookup 現在無條件執行，必須顯式 stub 成空。
        # 不 stub 的話 MagicMock 預設值是 truthy（且 __iter__ 給空迭代），
        # 會靜默變成「有 VM 但一顆都沒處理」的假語意。
        mock_k8s.get_virtualmachines.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 2 workload" in data["message"]
        assert data["deployments"] == "svc-app"
        assert data["statefulsets"] == "svc-db"
        mock_k8s.scale_deployment.assert_called_once()
        mock_k8s.scale_statefulset.assert_called_once()

    @patch("src.api.releases.k8s_service")
    def test_scale_no_workloads(self, mock_k8s, client):
        """Release that owns nothing → 404."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        # #423 W4.3: the VM branch is consulted when there is no container
        # workload, so it must be stubbed empty or the MagicMock default
        # (truthy) would suppress the 404 this test exists to prove.
        mock_k8s.get_virtualmachines.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-missing/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 404
        assert "No deployments, statefulsets or virtualmachines" in response.json()["detail"]

    # ---- #423 W4.3: VirtualMachine branch ----------------------------------
    # Same shape as the BUG-7 StatefulSet regression above: a workload kind
    # the endpoint did not know about returned 404, and the Odoo caller turned
    # that into state='error'. For VMs that meant every unpaid HAOS tenant
    # landing in a bogus error state instead of being suspended.

    @patch("src.api.releases.k8s_service")
    def test_scale_down_virtualmachine_maps_to_halted(self, mock_k8s, client):
        """replicas=0 (suspend) → runStrategy Halted, NOT a 404."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 1 workload" in data["message"]
        assert data["virtualmachines"] == "svc-haos"
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Halted",
        )
        mock_k8s.scale_deployment.assert_not_called()
        mock_k8s.scale_statefulset.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_scale_up_virtualmachine_maps_to_always(self, mock_k8s, client):
        """replicas=1 (resume) → runStrategy Always."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 1},
        )

        assert response.status_code == 200
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Always",
        )

    @patch("src.api.releases.k8s_service")
    def test_any_nonzero_replicas_maps_to_always(self, mock_k8s, client):
        """A VM has no notion of 'N replicas' — everything >=1 is Always."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 3},
        )

        assert response.status_code == 200
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Always",
        )

    # ---- #423 W6.3: 混合拓撲（容器 + VM）------------------------------------
    # 本組取代舊的 test_container_release_never_queries_virtualmachines。那條
    # 測試斷言「有容器 workload 時不得查 VM」——它把 W4 的最佳化當成契約寫死，
    # 於是 chart 0.1.2 給 HAOS 加上 xff-proxy Deployment 之後，缺陷被測試保護
    # 著：suspend 只 scale 了 proxy，VM 照跑照計費，而測試全綠。
    #
    # 教訓：斷言「某個查詢不會發生」是在鎖定實作的省事路徑，不是在保護行為。
    # 真正該守的行為是「這個 release 擁有的每一種 workload 都被處理到」。

    @patch("src.api.releases.k8s_service")
    def test_mixed_deployment_and_vm_scales_both(self, mock_k8s, client):
        """🔴 #423 W6.3 回歸：Deployment 在場時 VM **不得**被跳過。

        HAOS chart 0.1.2 起是 xff-proxy Deployment + VirtualMachine 的混合拓撲。
        舊 gate（`if not deployments and not statefulsets`）在這個形狀下永遠不
        成立，suspend 於是只把 proxy 收到 0：stg 實測 Odoo state=suspended、
        proxy replicas=0，但 VM 仍 Running、quota 只釋放 50m，VM 的 2000m/4.4Gi
        照燒。欠費停權帳面成立、成本全額在燒，且端點回 200 沒有任何錯誤訊號。
        """
        mock_k8s.get_deployments.return_value = [{"name": "svc-haos-xff-proxy"}]
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        data = response.json()
        assert "Scaled 2 workload" in data["message"]
        assert data["deployments"] == "svc-haos-xff-proxy"
        assert data["virtualmachines"] == "svc-haos"
        mock_k8s.scale_deployment.assert_called_once_with(
            "paas-ws-test", "svc-haos-xff-proxy", 0,
        )
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Halted",
        )

    @patch("src.api.releases.k8s_service")
    def test_mixed_deployment_and_vm_resumes_both(self, mock_k8s, client):
        """resume 方向同樣要兩半都動（只復原 proxy＝付了錢卻沒有機器）。"""
        mock_k8s.get_deployments.return_value = [{"name": "svc-haos-xff-proxy"}]
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 1},
        )

        assert response.status_code == 200
        mock_k8s.scale_deployment.assert_called_once_with(
            "paas-ws-test", "svc-haos-xff-proxy", 1,
        )
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Always",
        )

    @patch("src.api.releases.k8s_service")
    def test_mixed_statefulset_and_vm_scales_both(self, mock_k8s, client):
        """StatefulSet 那半同理——gate 的兩個條件都不該再決定 VM 查不查。"""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = [{"name": "svc-haos-db"}]
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-haos"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        assert "Scaled 2 workload" in response.json()["message"]
        mock_k8s.scale_statefulset.assert_called_once_with(
            "paas-ws-test", "svc-haos-db", 0,
        )
        mock_k8s.patch_vm_run_strategy.assert_called_once_with(
            "paas-ws-test", "svc-haos", "Halted",
        )

    @patch("src.api.releases.k8s_service")
    def test_all_three_kinds_scale_together(self, mock_k8s, client):
        """三種 workload 同時在場 → 三種都處理（計數涵蓋全部）。"""
        mock_k8s.get_deployments.return_value = [{"name": "svc-app"}]
        mock_k8s.get_statefulsets.return_value = [{"name": "svc-db"}]
        mock_k8s.get_virtualmachines.return_value = [{"name": "svc-vm"}]

        response = client.post(
            "/api/releases/paas-ws-test/svc/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        assert "Scaled 3 workload" in response.json()["message"]
        mock_k8s.scale_deployment.assert_called_once()
        mock_k8s.scale_statefulset.assert_called_once()
        mock_k8s.patch_vm_run_strategy.assert_called_once()

    @patch("src.api.releases.k8s_service")
    def test_container_release_now_queries_virtualmachines(self, mock_k8s, client):
        """純容器 release 現在也會查 VM——正確性不再依賴 chart 的拓撲形狀。

        這正是舊測試斷言「不會查」的那一條，語意刻意反轉：省一次 lookup 換來的
        是把「有哪些 workload」變成猜測，而猜錯的代價是靜默漏停一台 VM。
        """
        mock_k8s.get_deployments.return_value = [{"name": "svc-test-main"}]
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.return_value = []

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        mock_k8s.get_virtualmachines.assert_called_once_with(
            namespace="paas-ws-test",
            label_selector="app.kubernetes.io/instance=svc-test",
        )
        # 查了但沒有 VM → 不該 patch 任何東西。
        mock_k8s.patch_vm_run_strategy.assert_not_called()

    @patch("src.api.releases.k8s_service")
    def test_container_release_tolerates_missing_crd(self, mock_k8s, client):
        """沒有 KubeVirt 的叢集：容器 release 照樣 200，不因多出來的 lookup 而壞。

        拆掉 gate 之後，非 KubeVirt 叢集的每一次 scale 都會多付一次必定失敗的
        CRD lookup。這條守住那個代價被 CRD-absent 容忍正確吸收——否則全平台的
        容器服務 suspend 都會 500。
        """
        mock_k8s.get_deployments.return_value = [{"name": "svc-test-main"}]
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.side_effect = KubectlException(
            message="no CRD",
            command="kubectl get virtualmachines",
            stderr='the server doesn\'t have a resource type "virtualmachines"',
        )

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 200
        assert "Scaled 1 workload" in response.json()["message"]
        mock_k8s.scale_deployment.assert_called_once()

    @patch("src.api.releases.k8s_service")
    def test_container_release_vm_forbidden_is_500_not_partial_success(
        self, mock_k8s, client
    ):
        """混合拓撲下被吞掉的 Forbidden ＝「成功地沒停機」，必須 500。

        容器那半會成功，所以若把 Forbidden 當成「沒有 VM」，端點會回 200
        宣告已 suspend，而 VM 依然在跑——與本 PR 修的缺陷是同一個結果，只是
        換了一個成因。只有 CRD-absent 能被容忍。
        """
        mock_k8s.get_deployments.return_value = [{"name": "svc-haos-xff-proxy"}]
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.side_effect = KubectlException(
            message="forbidden",
            command="kubectl get virtualmachines",
            stderr=(
                'Error from server (Forbidden): virtualmachines.kubevirt.io is '
                'forbidden: User "system:serviceaccount:paas-system:paas-operator" '
                'cannot list resource "virtualmachines"'
            ),
        )

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 500

    @patch("src.api.releases.k8s_service")
    def test_missing_kubevirt_crd_still_404s(self, mock_k8s, client):
        """No CRD → genuinely no VM workload → the honest answer is 404.

        This is the ONLY KubectlException that may be folded into the 404.
        """
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.side_effect = KubectlException(
            message="no CRD",
            command="kubectl get virtualmachines",
            stderr='the server doesn\'t have a resource type "virtualmachines"',
        )

        response = client.post(
            "/api/releases/paas-ws-test/svc-missing/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 404

    @patch("src.api.releases.k8s_service")
    def test_rbac_forbidden_is_500_not_404(self, mock_k8s, client):
        """🔴 MAJOR-1 regression: Forbidden must NOT look like 'no workloads'.

        Live risk at the time of writing: KubeVirt CRDs are installed on
        woow-k3s but the operator ClusterRole has no kubevirt.io apiGroup
        yet (chart 0.1.19 stuck in the CI queue). With a blanket
        `except KubectlException` this returned 404, the Odoo caller wrote
        state=error, and the VM was never Halted — an unpaid tenant keeps
        running. A 500 is loud, diagnosable, and leaves the subscription
        machinery able to retry.
        """
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.side_effect = KubectlException(
            message="forbidden",
            command="kubectl get virtualmachines",
            stderr=(
                'Error from server (Forbidden): virtualmachines.kubevirt.io is '
                'forbidden: User "system:serviceaccount:paas-system:paas-operator" '
                'cannot list resource "virtualmachines"'
            ),
        )

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 500, (
            "an RBAC Forbidden must surface as 500, never be folded into the "
            "'no workloads' 404 — see MAJOR-1"
        )

    @patch("src.api.releases.k8s_service")
    def test_timeout_is_500_not_404(self, mock_k8s, client):
        """Same rule for a kubectl timeout — only missing-CRD gets the 404."""
        mock_k8s.get_deployments.return_value = []
        mock_k8s.get_statefulsets.return_value = []
        mock_k8s.get_virtualmachines.side_effect = KubectlException(
            message="timeout",
            command="kubectl get virtualmachines",
            stderr="kubectl command timed out after 30s",
        )

        response = client.post(
            "/api/releases/paas-ws-test/svc-haos/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 500

    @patch("src.api.releases.k8s_service")
    def test_scale_kubectl_failure(self, mock_k8s, client):
        """kubectl error during scale → 500."""
        mock_k8s.get_deployments.return_value = [{"name": "svc-test-main"}]
        mock_k8s.get_statefulsets.return_value = []
        # #423 W6.3：VM lookup 現在無條件執行，必須顯式 stub 成空。
        # 不 stub 的話 MagicMock 預設值是 truthy（且 __iter__ 給空迭代），
        # 會靜默變成「有 VM 但一顆都沒處理」的假語意。
        mock_k8s.get_virtualmachines.return_value = []
        mock_k8s.scale_deployment.side_effect = KubectlException(
            message="kubectl scale failed",
            command="kubectl scale",
            stderr="error",
        )

        response = client.post(
            "/api/releases/paas-ws-test/svc-test/scale",
            params={"replicas": 0},
        )

        assert response.status_code == 500
