"""#1157：同名重裝不得靜默接回上一次 uninstall 留下的孤兒 PVC／DataVolume。

``delete_release`` 會在留下的 PVC／DataVolume 打 ``orphaned-at``／``grace-period-days``／
``release-name`` 三個 label，而 ``create_release`` 從前完全不看它們：同 namespace＋同
release name 重裝時 helm 接回舊 PVC（舊站原地復活），label 卻還在 ⇒ 回收 CronJob 在保留期
到期後刪掉活站資料。裁決 A（2026-09-27）：install 前偵測到就回 409，不提供接回旗標。

這裡守：

* ``KubernetesService.list_orphaned_release_resources`` 的 selector 形狀與 CDI 缺席處理；
* ``POST /api/releases``：有孤兒資源 ⇒ 409＋固定 ``error_code``，**不**碰任何叢集寫入、
  也不跑其他預檢；查不到 ⇒ 503（fail-closed）；沒有 ⇒ 照舊 install；
  release name 不是合法 label 值 ⇒ 400。
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.releases import (
    ORPHANED_RELEASE_CHECK_UNAVAILABLE_ERROR_CODE,
    ORPHANED_RELEASE_RESOURCES_ERROR_CODE,
    build_orphan_pvc_labels,
)
from src.main import app
from src.models.schemas import ReleaseInfo, ReleaseStatus
from src.services.helm import KubectlException, KubernetesService

NS = "paas-ws-test1157"
SELECTOR = "paas.woowtech.io/release-name=svc-x,paas.woowtech.io/orphaned-at"


def _stdout(text):
    out = MagicMock()
    out.stdout = text
    return out


class TestLookupKubectl:
    def _svc(self, has_cdi=True):
        svc = KubernetesService()
        svc._has_dv_crd = has_cdi
        return svc

    def test_selects_this_releases_orphans_only(self):
        """selector＝uninstall 打的 release-name 值 **且** 有 orphaned-at（兩者都要）。"""
        svc = self._svc()
        calls = []

        def fake_run(args, **kwargs):
            calls.append(args)
            return _stdout("a-data b-data" if args[1] == "pvc" else "vm-disk")

        with patch.object(svc, "_run_command", side_effect=fake_run):
            found = svc.list_orphaned_release_resources(NS, "svc-x")
        assert found == {"pvcs": ["a-data", "b-data"], "datavolumes": ["vm-disk"]}
        assert [c[c.index("--selector") + 1] for c in calls] == [SELECTOR, SELECTOR]
        assert [c[:2] for c in calls] == [["get", "pvc"], ["get", "datavolumes"]]

    def test_selector_matches_the_labels_uninstall_stamps(self):
        """與 ``build_orphan_pvc_labels`` 同一組 key——任一邊改名，這裡就會選不到。"""
        labels = build_orphan_pvc_labels("svc-x", 7)
        key_value, presence = SELECTOR.split(",")
        key, value = key_value.split("=")
        assert labels[key] == value
        assert presence in labels

    def test_no_cdi_skips_datavolume_lookup(self):
        svc = self._svc(has_cdi=False)
        with patch.object(svc, "_run_command", return_value=_stdout("")) as run:
            found = svc.list_orphaned_release_resources(NS, "svc-x")
        assert found == {"pvcs": [], "datavolumes": []}
        assert run.call_count == 1

    def test_datavolume_crd_absent_error_is_empty(self):
        svc = self._svc()

        def fake_run(args, **kwargs):
            if args[1] == "datavolumes":
                raise KubectlException(
                    message="x", command="kubectl get datavolumes",
                    stderr='error: the server doesn\'t have a resource type "datavolumes"')
            return _stdout("")

        with patch.object(svc, "_run_command", side_effect=fake_run):
            assert svc.list_orphaned_release_resources(NS, "svc-x")["datavolumes"] == []

    def test_other_datavolume_failure_raises(self):
        """Forbidden／逾時不是「沒有 DataVolume」——吞掉就回到 #1157 的資料遺失路徑。"""
        svc = self._svc()

        def fake_run(args, **kwargs):
            if args[1] == "datavolumes":
                raise KubectlException(message="x", command="kubectl get datavolumes",
                                       stderr="Error from server (Forbidden)")
            return _stdout("")

        with patch.object(svc, "_run_command", side_effect=fake_run):
            with pytest.raises(KubectlException):
                svc.list_orphaned_release_resources(NS, "svc-x")

    def test_pvc_failure_raises(self):
        svc = self._svc()
        with patch.object(svc, "_run_command", side_effect=KubectlException(
                message="x", command="kubectl get pvc", stderr="timeout")):
            with pytest.raises(KubectlException):
                svc.list_orphaned_release_resources(NS, "svc-x")

    @pytest.mark.parametrize("name", ["svc-x,foo", "svc x", "-svc", "a" * 64])
    def test_name_that_would_change_the_selector_is_rejected(self, name):
        svc = self._svc()
        with patch.object(svc, "_run_command") as run:
            with pytest.raises(ValueError):
                svc.list_orphaned_release_resources(NS, name)
        run.assert_not_called()

    def test_rejects_foreign_namespace(self):
        with pytest.raises(ValueError):
            self._svc().list_orphaned_release_resources("kube-system", "svc-x")


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _release():
    return ReleaseInfo(name="svc-x", namespace=NS, revision=1, status=ReleaseStatus.DEPLOYED,
                       chart="odoo", app_version="18", updated="2026-10-06T00:00:00Z")


def _post(client, name="svc-x"):
    return client.post("/api/releases", json={"namespace": NS, "name": name,
                                              "chart": "oci://example.invalid/odoo"})


class TestCreateReleaseOrphanGate:
    @patch("src.api.releases.helm_service")
    def test_orphan_pvcs_return_409_before_any_cluster_write(
        self, mock_helm, client, orphaned_release_resources, sa_pull_secret_convergence,
        storage_quota_snapshot,
    ):
        orphaned_release_resources.return_value = {
            "pvcs": ["svc-x-odoo", "data-svc-x-postgresql-0"], "datavolumes": []}
        resp = _post(client)

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == ORPHANED_RELEASE_RESOURCES_ERROR_CODE
        assert detail["pvcs"] == ["data-svc-x-postgresql-0", "svc-x-odoo"]
        assert detail["datavolumes"] == []
        # 訊息要指出兩條出路，呼叫端（手動操作者）才知道怎麼辦
        assert "delete these resources first" in detail["message"]
        assert "new release name" in detail["message"]
        orphaned_release_resources.assert_called_once_with(NS, "svc-x")
        mock_helm.install.assert_not_called()
        sa_pull_secret_convergence.assert_not_called()
        storage_quota_snapshot.assert_not_called()

    @patch("src.api.releases.helm_service")
    def test_orphan_datavolume_alone_also_blocks(
        self, mock_helm, client, orphaned_release_resources
    ):
        orphaned_release_resources.return_value = {"pvcs": [], "datavolumes": ["svc-x-disk"]}
        resp = _post(client)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["datavolumes"] == ["svc-x-disk"]
        mock_helm.install.assert_not_called()

    @patch("src.api.releases.helm_service")
    def test_no_orphans_installs_as_before(self, mock_helm, client, orphaned_release_resources):
        mock_helm.install.return_value = _release()
        assert _post(client).status_code == 201
        orphaned_release_resources.assert_called_once_with(NS, "svc-x")
        mock_helm.install.assert_called_once()

    @patch("src.api.releases.helm_service")
    def test_lookup_failure_fails_closed_with_503(
        self, mock_helm, client, orphaned_release_resources, sa_pull_secret_convergence
    ):
        """查不到＝不知道：與 #1348／#1415 相反，這裡不放行（放行就是 #1157 的資料遺失路徑）。"""
        orphaned_release_resources.side_effect = KubectlException(
            message="boom", command="kubectl get pvc", stderr="Unable to connect to the server")
        resp = _post(client)
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["error_code"] == ORPHANED_RELEASE_CHECK_UNAVAILABLE_ERROR_CODE
        mock_helm.install.assert_not_called()
        sa_pull_secret_convergence.assert_not_called()

    @patch("src.api.releases.helm_service")
    def test_invalid_release_name_is_400(self, mock_helm, client, orphaned_release_resources):
        orphaned_release_resources.side_effect = ValueError("Invalid k8s label value")
        assert _post(client, name="svc-x,foo").status_code == 400
        mock_helm.install.assert_not_called()
