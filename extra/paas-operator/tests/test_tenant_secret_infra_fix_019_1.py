"""INFRA-FIX-019-1：租戶憑證（ngrok authtoken、Tailscale auth key）不得進 helm release 歷史。

## 缺陷

helm 把**每一個** revision 的 user values 與渲染後的 manifest 存在
``sh.helm.release.v1.<release>.v<N>`` Secret。租戶的 ngrok token 走
``ngrok.authtoken``（values）→ chart 自己的 Secret（manifest），所以租戶清除或換掉
token 之後，舊值仍完整留在每個 superseded revision（Headscale 238 實測：v19／v21／
v22／v25 仍是 49 字元 token）。

## 修法（operator 這一半）

平台改送 ``tenant_secret = {"name": "<release>-tenant-secret", "data": {...}}``，
values 只帶 ``tenantSecret.name``／``revision``／``nonEmptyKeys``。operator 在 helm
**之前**用 server-side apply 把 data 寫進那個**不屬於 release** 的 Secret：

* ``kubectl apply --server-side``：client-side apply 會把整份 manifest（含 data）寫進
  ``kubectl.kubernetes.io/last-applied-configuration`` annotation——等於另存一份明文；
* manifest 走 stdin（不落暫存檔、不在 argv）；
* 名稱只接受 ``<release>-tenant-secret``：這個端點不能被拿來覆寫 chart 自己的 Secret、
  helm 的 release Secret 或別的服務的東西；
* upgrade 預檢（dry-run）不寫；預檢擋下（409）時也不寫；刪服務時一併刪除。
"""
import base64
import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.models.schemas import (
    ReleaseCreateRequest,
    ReleaseInfo,
    ReleaseStatus,
    ReleaseUpgradeRequest,
    TenantSecretSpec,
)
from src.services.helm import KubectlException, KubernetesService
from src.services.tenant_secret import (
    TENANT_SECRET_FIELD_MANAGER,
    build_tenant_secret_manifest,
    tenant_secret_name,
    validate_tenant_secret,
)

NS = "paas-ws-test"
REL = "svc-aaaa1111"
TOKEN = "2abcDEFghiJKLmnoPQRstuVWXyz0123456789_ngrokTOKEN"


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _release(revision=3):
    return ReleaseInfo(
        name=REL, namespace=NS, revision=revision, status=ReleaseStatus.DEPLOYED,
        chart="headscale", chart_version="0.1.7", app_version="v0.29.4",
        updated="2026-10-17T00:00:00Z",
    )


def _tenant_secret(token=TOKEN, name=None):
    return {"name": name or f"{REL}-tenant-secret", "data": {"ngrok_authtoken": token}}


# ── 純函式：名稱、驗證、manifest ────────────────────────────────────────────────


class TestTenantSecretManifest:
    def test_name_is_release_scoped(self):
        assert tenant_secret_name(REL) == f"{REL}-tenant-secret"

    def test_manifest_carries_data_base64_and_no_string_data(self):
        manifest = build_tenant_secret_manifest(NS, REL, {"ngrok_authtoken": TOKEN})
        assert manifest["kind"] == "Secret"
        assert manifest["metadata"]["name"] == f"{REL}-tenant-secret"
        assert manifest["metadata"]["namespace"] == NS
        assert manifest["type"] == "Opaque"
        assert "stringData" not in manifest
        assert base64.b64decode(manifest["data"]["ngrok_authtoken"]).decode() == TOKEN

    def test_manifest_is_not_helm_managed(self):
        """helm 不能認領它：沒有 managed-by=Helm、沒有 meta.helm.sh annotation。"""
        manifest = build_tenant_secret_manifest(NS, REL, {"ngrok_authtoken": TOKEN})
        labels = manifest["metadata"]["labels"]
        assert labels["app.kubernetes.io/instance"] == REL
        assert labels["app.kubernetes.io/managed-by"] == "paas-operator"
        assert labels["paas.woowtech.io/tenant-secret"] == "true"
        assert not any(k.startswith("meta.helm.sh") for k in manifest["metadata"].get("annotations", {}))

    def test_cleared_value_is_written_as_empty(self):
        """清除＝寫空字串（chart 的 secretKeyRef 是 optional，sidecar 會閒置並說明）。"""
        manifest = build_tenant_secret_manifest(NS, REL, {"ngrok_authtoken": ""})
        assert manifest["data"]["ngrok_authtoken"] == ""

    def test_validate_rejects_a_foreign_name(self):
        for bad in (f"{REL}-headscale", "sh.helm.release.v1.svc-aaaa1111.v3",
                    "svc-bbbb2222-tenant-secret", f"{REL}-tenant-secret-x"):
            with pytest.raises(ValueError):
                validate_tenant_secret(REL, TenantSecretSpec(name=bad, data={"k": "v"}))

    def test_validate_accepts_the_release_name(self):
        validate_tenant_secret(REL, TenantSecretSpec(**_tenant_secret()))


class TestTenantSecretValidation:
    """形狀檢查放在 ``validate_tenant_secret``（→ 400 通用訊息），**不**放在 pydantic：
    FastAPI 的 422 會把 ``input`` 原樣回給呼叫端——等於把 token 寫進平台的錯誤 log。"""

    def _bad(self, data):
        with pytest.raises(ValueError) as exc:
            validate_tenant_secret(REL, TenantSecretSpec(name=f"{REL}-tenant-secret", data=data))
        assert TOKEN not in str(exc.value)

    def test_key_shape_is_enforced(self):
        for bad in ("", "a/b", "../x", "a b", "x" * 254):
            self._bad({bad: TOKEN})

    def test_value_must_be_a_string(self):
        self._bad({"k": {"nested": TOKEN}})
        self._bad({"k": 12345})

    def test_value_size_is_bounded(self):
        self._bad({"k": TOKEN + "x" * (64 * 1024)})

    def test_key_count_is_bounded(self):
        self._bad({f"k{i}": TOKEN for i in range(17)})

    def test_repr_never_shows_the_data(self):
        assert TOKEN not in repr(TenantSecretSpec(**_tenant_secret()))

    def test_requests_accept_the_field(self):
        assert ReleaseUpgradeRequest(tenant_secret=_tenant_secret()).tenant_secret.data == {
            "ngrok_authtoken": TOKEN}
        assert ReleaseCreateRequest(
            namespace=NS, name=REL, chart="x", tenant_secret=_tenant_secret()
        ).tenant_secret is not None

    @patch("src.api.releases._upgrade_preflight")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_bad_value_is_400_without_echo(self, mock_helm, mock_k8s, mock_pre, client):
        mock_pre.return_value = {"expansions": []}
        response = client.patch(f"/api/releases/{NS}/{REL}", json={
            "chart": "oci://x/headscale", "version": "0.1.7",
            "tenant_secret": {"name": f"{REL}-tenant-secret", "data": {"k": {"x": TOKEN}}},
        })
        assert response.status_code == 400
        assert TOKEN not in response.text
        mock_k8s.apply_tenant_secret.assert_not_called()
        mock_helm.upgrade.assert_not_called()


# ── kubectl：server-side apply、stdin、argv 不含秘密 ────────────────────────────


class TestKubectlApply:
    @patch("subprocess.run")
    def test_server_side_apply_through_stdin(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="secret/x serverside-applied\n", stderr="")
        KubernetesService().apply_tenant_secret(NS, REL, {"ngrok_authtoken": TOKEN})

        argv = mock_run.call_args.args[0]
        assert "apply" in argv and "--server-side" in argv
        assert f"--field-manager={TENANT_SECRET_FIELD_MANAGER}" in argv
        assert "--force-conflicts" in argv
        assert argv[argv.index("-f") + 1] == "-"
        assert not any(TOKEN in a for a in argv), "the token must never be on argv"
        assert not any(base64.b64encode(TOKEN.encode()).decode() in a for a in argv)
        sent = json.loads(mock_run.call_args.kwargs["input"])
        assert sent["metadata"]["name"] == f"{REL}-tenant-secret"
        assert base64.b64decode(sent["data"]["ngrok_authtoken"]).decode() == TOKEN

    @patch("subprocess.run")
    def test_failure_raises_without_the_token(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="forbidden")
        with pytest.raises(KubectlException) as exc:
            KubernetesService().apply_tenant_secret(NS, REL, {"ngrok_authtoken": TOKEN})
        assert TOKEN not in str(exc.value.command)

    def test_namespace_is_validated(self):
        with pytest.raises(ValueError):
            KubernetesService().apply_tenant_secret("kube-system", REL, {"k": "v"})

    @patch("subprocess.run")
    def test_delete_ignores_not_found(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        KubernetesService().delete_tenant_secret(NS, REL)
        argv = mock_run.call_args.args[0]
        assert argv[1:4] == ["delete", "secret", f"{REL}-tenant-secret"]
        assert "--ignore-not-found" in argv
        assert argv[argv.index("--namespace") + 1] == NS


# ── API：install／upgrade／preflight／reset／delete ────────────────────────────


@pytest.fixture
def no_install_prechecks():
    """開通預檢（孤兒資源、儲存配額、叢集容量）各有自己的測試；這裡只看寫入順序。"""
    with patch("src.api.releases._precheck_orphaned_release_resources"), \
            patch("src.api.releases._precheck_storage_quota"), \
            patch("src.api.releases._precheck_cluster_storage_capacity"):
        yield


@pytest.mark.usefixtures("no_install_prechecks")
class TestInstallWritesTenantSecretFirst:
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_install_applies_before_helm(self, mock_helm, mock_k8s, client):
        order = MagicMock()
        mock_k8s.apply_tenant_secret.side_effect = lambda *a, **k: order.apply()
        mock_helm.install.side_effect = lambda **k: (order.install(), _release(1))[1]

        response = client.post("/api/releases", json={
            "namespace": NS, "name": REL, "chart": "headscale",
            "values": {"tenantSecret": {"name": f"{REL}-tenant-secret", "revision": "r1"}},
            "tenant_secret": _tenant_secret(),
        })

        assert response.status_code == 201, response.text
        assert order.mock_calls == [call.apply(), call.install()]
        mock_k8s.apply_tenant_secret.assert_called_once_with(NS, REL, {"ngrok_authtoken": TOKEN})
        sent_values = json.dumps(mock_helm.install.call_args.kwargs["values"])
        assert TOKEN not in sent_values
        assert TOKEN not in response.text

    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_install_refuses_a_foreign_name_before_any_write(self, mock_helm, mock_k8s, client):
        response = client.post("/api/releases", json={
            "namespace": NS, "name": REL, "chart": "headscale",
            "tenant_secret": _tenant_secret(name=f"{REL}-headscale"),
        })
        assert response.status_code == 400
        mock_k8s.apply_tenant_secret.assert_not_called()
        mock_helm.install.assert_not_called()


class TestUpgradeWritesTenantSecretFirst:
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_upgrade_applies_after_preflight_before_helm(self, mock_helm, mock_k8s, client):
        order = MagicMock()
        mock_k8s.apply_tenant_secret.side_effect = lambda *a, **k: order.apply()

        def _upgrade(**kwargs):
            if kwargs.get("dry_run"):
                order.preflight()
                return ""
            order.upgrade()
            return _release()

        mock_helm.upgrade.side_effect = _upgrade
        mock_k8s.get_items.return_value = []

        response = client.patch(f"/api/releases/{NS}/{REL}", json={
            "chart": "oci://x/headscale", "version": "0.1.7", "reset_values": True,
            "values": {"ngrok": {"authtoken": ""},
                       "tenantSecret": {"name": f"{REL}-tenant-secret", "revision": "r2"}},
            "tenant_secret": _tenant_secret(),
        })

        assert response.status_code == 200, response.text
        assert order.mock_calls == [call.preflight(), call.apply(), call.upgrade()]
        for c in mock_helm.upgrade.call_args_list:
            assert TOKEN not in json.dumps(c.kwargs.get("values") or {})
            assert TOKEN not in json.dumps(c.kwargs.get("sensitive_values") or {})

    @patch("src.api.releases._upgrade_preflight")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_preflight_refusal_writes_nothing(self, mock_helm, mock_k8s, mock_pre, client):
        from fastapi import HTTPException
        mock_pre.side_effect = HTTPException(status_code=409, detail="immutable field")
        response = client.patch(f"/api/releases/{NS}/{REL}", json={
            "chart": "oci://x/headscale", "version": "0.1.7",
            "tenant_secret": _tenant_secret(),
        })
        assert response.status_code == 409
        mock_k8s.apply_tenant_secret.assert_not_called()
        mock_helm.upgrade.assert_not_called()

    @patch("src.api.releases._upgrade_preflight")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_apply_failure_stops_before_helm(self, mock_helm, mock_k8s, mock_pre, client):
        mock_pre.return_value = {"expansions": []}
        mock_k8s.apply_tenant_secret.side_effect = KubectlException(
            message="kubectl command failed with code 1", command="kubectl apply", stderr="forbidden")
        response = client.patch(f"/api/releases/{NS}/{REL}", json={
            "chart": "oci://x/headscale", "version": "0.1.7",
            "tenant_secret": _tenant_secret(),
        })
        assert response.status_code == 500
        assert TOKEN not in response.text
        mock_helm.upgrade.assert_not_called()

    @patch("src.api.releases._upgrade_preflight")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_without_tenant_secret_nothing_is_applied(self, mock_helm, mock_k8s, mock_pre, client):
        mock_pre.return_value = {"expansions": []}
        mock_helm.upgrade.return_value = _release()
        response = client.patch(f"/api/releases/{NS}/{REL}", json={
            "chart": "oci://x/headscale", "version": "0.1.6", "values": {"a": 1},
        })
        assert response.status_code == 200
        mock_k8s.apply_tenant_secret.assert_not_called()

    @patch("src.api.releases._upgrade_preflight")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_upgrade_refuses_a_foreign_name(self, mock_helm, mock_k8s, mock_pre, client):
        mock_pre.return_value = {"expansions": []}
        response = client.patch(f"/api/releases/{NS}/{REL}", json={
            "chart": "oci://x/headscale", "version": "0.1.7",
            "tenant_secret": _tenant_secret(name="sh.helm.release.v1.svc-aaaa1111.v3"),
        })
        assert response.status_code == 400
        mock_k8s.apply_tenant_secret.assert_not_called()
        mock_helm.upgrade.assert_not_called()


class TestPreflightNeverWrites:
    @patch("src.api.releases._upgrade_preflight")
    @patch("src.api.releases.k8s_service")
    def test_preflight_ignores_tenant_secret(self, mock_k8s, mock_pre, client):
        mock_pre.return_value = {"expansions": []}
        response = client.post(f"/api/releases/{NS}/{REL}/upgrade-preflight", json={
            "chart": "oci://x/headscale", "version": "0.1.7",
            "tenant_secret": _tenant_secret(),
        })
        assert response.status_code == 200
        mock_k8s.apply_tenant_secret.assert_not_called()


class TestResetConfigKeepsTenantSecret:
    @pytest.mark.parametrize("carrier", ["helm_secret", "config_json"])
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_reset_applies_before_helm(self, mock_helm, mock_k8s, carrier, client):
        order = MagicMock()
        mock_k8s.apply_tenant_secret.side_effect = lambda *a, **k: order.apply()

        def _upgrade(**kwargs):
            order.upgrade()
            raise RuntimeError("stop after the helm call")  # the rest is covered elsewhere

        mock_helm.get.return_value = _release()
        mock_helm.upgrade.side_effect = _upgrade
        with pytest.raises(RuntimeError):
            client.post(f"/api/releases/{NS}/{REL}/reset-config", json={
                "chart": "oci://x/headscale", "version": "0.1.7", "reset_values": True,
                "values": {"tenantSecret": {"name": f"{REL}-tenant-secret", "revision": "r2"}},
                "sensitive_values": {"config": {"sensitive": {"admin_password": "new-pwd-1"}}},
                "admin_password_carrier": carrier,
                "tenant_secret": _tenant_secret(),
            })
        assert order.mock_calls[:2] == [call.apply(), call.upgrade()]

    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_reset_refuses_a_foreign_name(self, mock_helm, mock_k8s, client):
        response = client.post(f"/api/releases/{NS}/{REL}/reset-config", json={
            "chart": "oci://x/headscale", "version": "0.1.7",
            "sensitive_values": {"config": {"sensitive": {"admin_password": "new-pwd-1"}}},
            "admin_password_carrier": "helm_secret",
            "tenant_secret": _tenant_secret(name=f"{REL}-headscale"),
        })
        assert response.status_code == 400
        mock_k8s.apply_tenant_secret.assert_not_called()
        mock_helm.upgrade.assert_not_called()


class TestDeleteRemovesTenantSecret:
    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_delete_removes_it_after_uninstall(self, mock_helm, mock_k8s, mock_cf, client):
        order = MagicMock()
        mock_helm.uninstall.side_effect = lambda *a, **k: (order.uninstall(), {"message": "Release uninstalled"})[1]
        mock_k8s.delete_tenant_secret.side_effect = lambda *a, **k: order.delete_secret()
        mock_k8s.list_pvcs.return_value = []
        mock_k8s.has_datavolume_crd.return_value = True
        mock_k8s.list_datavolumes.return_value = []
        mock_cf.delete_route = AsyncMock(return_value=True)

        response = client.delete(f"/api/releases/{NS}/{REL}")

        assert response.status_code == 200
        assert order.mock_calls[:2] == [call.uninstall(), call.delete_secret()]
        mock_k8s.delete_tenant_secret.assert_called_once_with(NS, REL)

    @patch("src.api.releases.cloudflare_service")
    @patch("src.api.releases.k8s_service")
    @patch("src.api.releases.helm_service")
    def test_delete_survives_a_secret_delete_failure(self, mock_helm, mock_k8s, mock_cf, client):
        mock_helm.uninstall.return_value = {"message": "Release uninstalled"}
        mock_k8s.delete_tenant_secret.side_effect = KubectlException(
            message="kubectl command failed with code 1", command="kubectl delete", stderr="boom")
        mock_k8s.list_pvcs.return_value = []
        mock_k8s.has_datavolume_crd.return_value = True
        mock_k8s.list_datavolumes.return_value = []
        mock_cf.delete_route = AsyncMock(return_value=True)

        response = client.delete(f"/api/releases/{NS}/{REL}")

        assert response.status_code == 200
