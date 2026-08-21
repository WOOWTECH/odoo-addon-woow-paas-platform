"""#531：三個 tunnel 寫入端點的歸屬驗證（縱深防禦）。

被守的攻擊形狀（＝ #522 的形狀）：呼叫端持有**自己那一列**的 tunnel 名稱，
卻把**別人的** ``tunnel_id`` 塞進 path。operator 只有一把平台級 X-API-Key，
在此之前完全分辨不出這件事。

⚠ 這道檢查的誠實強度：tunnel name **不是機密**（CF dashboard 看得到、也 stamp
在 DNS record 的 comment 上）。它擋的是「拿自己的 name ＋ 別人的 id」，擋不住
同時掌握兩個值的攻擊者。不是完整授權，是縱深防禦。

測試紀律（#531 工單）：每一個「應該被拒」的案例，旁邊都有一個「合法請求仍然
通過」的對照，且兩者**只差在 claim 那一個變數**。
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api import tunnels as tunnels_module
from src.main import app
from src.services.cloudflare import CloudflareException

# 受害者 tunnel 的 id；三個端點共用同一組常數，讓「只差 claim」一目了然。
TUNNEL_ID = "tun-victim-0001"
# 這條 tunnel 在 Cloudflare 上的**實際** name（唯一權威來源）。
CF_ACTUAL_NAME = "paas-sa-victim-abc123"
# 攻擊者自己那一列的 name —— 他真的擁有它，只是它不屬於上面那條 tunnel。
ATTACKER_OWN_NAME = "paas-sa-attacker-def456"

ROUTES_BODY = {
    "routes": [
        {
            "subdomain": "app",
            "service_url": "http://localhost:80",
            "http_host_header": True,
        }
    ],
    "comment": "paas-sa-attacker-def456",
}
CONFIG_BODY = {
    "hostname": "app.woowtech.io",
    "service_url": "http_status:503",
}


@pytest.fixture
def client():
    """FastAPI test client（F-PC-4：真 key ＋ 對應 header），比照 test_tunnels.py。"""
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


def _cf_ready(mock_cf, *, cf_name=CF_ACTUAL_NAME):
    """把 mock 的 CloudflareService 佈成「一切正常」，只留 claim 當變數。"""
    mock_cf.api_token = "test-token"
    mock_cf.account_id = "test-account"
    mock_cf.domain = "woowtech.io"
    mock_cf.dns_enabled = True
    mock_cf.get_tunnel_status = AsyncMock(
        return_value={
            "tunnel_id": TUNNEL_ID,
            "name": cf_name,
            "status": "healthy",
            "connections": [],
            "created_at": None,
        }
    )
    mock_cf.configure_tunnel_ingress = AsyncMock()
    mock_cf.reconcile_tunnel_dns = AsyncMock(return_value={"app": "rec-app"})
    mock_cf.configure_tunnel = AsyncMock()
    mock_cf.delete_tunnel = AsyncMock()
    return mock_cf


def _assert_denied(response, expected_error, *, leaked=CF_ACTUAL_NAME):
    """拒絕時必須是結構化錯誤，而且**不得回吐** CF 上的真實 name。

    回吐等於把攻擊者手上缺的那一半直接送給他（他本來就有 id，只差 name）。
    """
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert isinstance(detail, dict), f"錯誤必須結構化，實得：{detail!r}"
    assert detail["error"] == expected_error
    assert detail["tunnel_id"] == TUNNEL_ID
    assert "operation" in detail
    assert leaked not in response.text, "不得把 CF 上的真實 tunnel name 回吐給呼叫端"


# --------------------------------------------------------------------------- #
#  claim 有帶但對不上 ⇒ 一律拒（三個端點都立刻生效）                            #
# --------------------------------------------------------------------------- #


class TestClaimMismatchIsRejected:
    @patch("src.api.tunnels.cloudflare_service")
    def test_sync_routes_rejects_foreign_tunnel(self, mock_cf, client):
        """PUT /routes：拿自己的 name ＋ 別人的 tunnel_id ⇒ 403，且 CF 一步都不准動。"""
        _cf_ready(mock_cf)

        response = client.put(
            f"/api/tunnels/{TUNNEL_ID}/routes",
            json=dict(ROUTES_BODY, tunnel_name=ATTACKER_OWN_NAME),
        )

        _assert_denied(response, "tunnel_ownership_mismatch")
        mock_cf.configure_tunnel_ingress.assert_not_called()
        mock_cf.reconcile_tunnel_dns.assert_not_called()

    @patch("src.api.tunnels.cloudflare_service")
    def test_sync_routes_allows_own_tunnel(self, mock_cf, client):
        """陽性對照：與上一支**只差 claim 這一個變數**，合法請求必須照常通過。"""
        _cf_ready(mock_cf)

        response = client.put(
            f"/api/tunnels/{TUNNEL_ID}/routes",
            json=dict(ROUTES_BODY, tunnel_name=CF_ACTUAL_NAME),
        )

        assert response.status_code == 200, response.text
        mock_cf.configure_tunnel_ingress.assert_called_once()
        mock_cf.reconcile_tunnel_dns.assert_called_once()

    @patch("src.api.tunnels.cloudflare_service")
    def test_update_config_rejects_foreign_tunnel(self, mock_cf, client):
        _cf_ready(mock_cf)

        response = client.put(
            f"/api/tunnels/{TUNNEL_ID}/config",
            json=dict(CONFIG_BODY, tunnel_name=ATTACKER_OWN_NAME),
        )

        _assert_denied(response, "tunnel_ownership_mismatch")
        mock_cf.configure_tunnel.assert_not_called()

    @patch("src.api.tunnels.cloudflare_service")
    def test_update_config_allows_own_tunnel(self, mock_cf, client):
        """陽性對照：只差 claim。"""
        _cf_ready(mock_cf)

        response = client.put(
            f"/api/tunnels/{TUNNEL_ID}/config",
            json=dict(CONFIG_BODY, tunnel_name=CF_ACTUAL_NAME),
        )

        assert response.status_code == 200, response.text
        mock_cf.configure_tunnel.assert_called_once()

    @patch("src.api.tunnels.cloudflare_service")
    def test_delete_rejects_foreign_tunnel(self, mock_cf, client):
        """DELETE 的 claim 走 query param（載體選型理由見 tunnels.py 的註解）。"""
        _cf_ready(mock_cf)

        response = client.delete(
            f"/api/tunnels/{TUNNEL_ID}",
            params={"tunnel_name": ATTACKER_OWN_NAME},
        )

        _assert_denied(response, "tunnel_ownership_mismatch")
        mock_cf.delete_tunnel.assert_not_called()

    @patch("src.api.tunnels.cloudflare_service")
    def test_delete_allows_own_tunnel(self, mock_cf, client):
        """陽性對照：只差 claim。"""
        _cf_ready(mock_cf)

        response = client.delete(
            f"/api/tunnels/{TUNNEL_ID}",
            params={"tunnel_name": CF_ACTUAL_NAME},
        )

        assert response.status_code == 200, response.text
        mock_cf.delete_tunnel.assert_called_once_with(TUNNEL_ID)


# --------------------------------------------------------------------------- #
#  claim 沒帶 ⇒ 兩段 rollout：預設 warning 放行，翻旗後拒                        #
#  兩段都要有測試釘住，不能只測有生效的那一段。                                  #
# --------------------------------------------------------------------------- #

_NO_CLAIM_CALLS = [
    pytest.param(
        lambda c: c.put(f"/api/tunnels/{TUNNEL_ID}/routes", json=ROUTES_BODY),
        id="routes",
    ),
    pytest.param(
        lambda c: c.put(f"/api/tunnels/{TUNNEL_ID}/config", json=CONFIG_BODY),
        id="config",
    ),
    pytest.param(
        lambda c: c.delete(f"/api/tunnels/{TUNNEL_ID}"),
        id="delete",
    ),
]


class TestRolloutDefaultIsWarnNotEnforce:
    """預設值本身要有測試釘住，不是只測「旗標開起來會拒」。

    只測 enforce 的話，把預設值改成 True（＝ operator 一上線就打爆三個端點）
    整套測試**照樣全綠**。這條是那個改動唯一會踩到的地雷。
    """

    def test_require_claim_defaults_to_false(self):
        from src.config import Settings

        assert Settings().tunnel_ownership_require_claim is False, (
            "stage 1 預設必須是 warn。改成 True 之前先讀 config.py 該欄位的翻旗前提："
            "送 claim 的平台版本已部署，且該環境 log 已不再出現 tunnel_claim_absent。"
        )

    def test_marker_names_are_not_superstrings_of_each_other(self):
        """四個 error code ＋ stage-1 marker 兩兩不得互為前綴。

        否則 `grep` / `in` 分不開，用它們寫的判準（含上面那條 stage-1 斷言、以及
        現場拿來判斷「可以翻旗了沒」的 log 查詢）會靜默失去鑑別力。
        """
        markers = [
            "tunnel_claim_absent",
            "tunnel_ownership_mismatch",
            "tunnel_ownership_claim_required",
            "tunnel_ownership_unverifiable",
            "tunnel_ownership_check_failed",
        ]
        assert len(set(markers)) == len(markers)
        for a in markers:
            for b in markers:
                if a is not b:
                    assert not b.startswith(a), f"{b!r} 是 {a!r} 的超字串"


class TestMissingClaimRolloutStages:
    @pytest.mark.parametrize("call", _NO_CLAIM_CALLS)
    @patch("src.api.tunnels.cloudflare_service")
    def test_stage1_missing_claim_warns_but_passes(self, mock_cf, client, call, caplog):
        """Stage 1（預設）：舊呼叫端還沒送 claim ⇒ 放行，但必須留下 warning。

        無聲放行等於這一段完全沒有訊號可以判斷「可以翻旗了沒」。
        """
        _cf_ready(mock_cf)

        with caplog.at_level("WARNING", logger="src.api.tunnels"):
            response = call(client)

        assert response.status_code == 200, response.text
        # 判準用**精確 marker**，不是 "tunnel_ownership" 這種前綴。前綴會同時命中
        # tunnel_ownership_mismatch / _claim_required / _unverifiable，等於這個
        # 斷言分不出「放行時記了什麼」——那正是它該分辨的事。
        messages = [r.getMessage() for r in caplog.records]
        assert any(
            m.startswith("tunnel_claim_absent:") for m in messages
        ), f"stage 1 放行必須留 tunnel_claim_absent warning，實得：{messages}"

    @pytest.mark.parametrize("call", _NO_CLAIM_CALLS)
    @patch("src.api.tunnels.cloudflare_service")
    def test_stage2_missing_claim_is_rejected_when_enforced(
        self, mock_cf, client, call
    ):
        """Stage 2（翻旗後）：沒帶 claim 就是拒。"""
        _cf_ready(mock_cf)

        with patch.object(
            tunnels_module.settings, "tunnel_ownership_require_claim", True
        ):
            response = call(client)

        _assert_denied(response, "tunnel_ownership_claim_required")
        mock_cf.configure_tunnel_ingress.assert_not_called()
        mock_cf.configure_tunnel.assert_not_called()
        mock_cf.delete_tunnel.assert_not_called()


# --------------------------------------------------------------------------- #
#  「查不到就當通過」是這道守衛最容易長出的假綠 —— 逐條釘住                      #
# --------------------------------------------------------------------------- #


class TestVerificationFailuresFailClosed:
    @patch("src.api.tunnels.cloudflare_service")
    def test_empty_cf_name_is_not_a_match(self, mock_cf, client):
        """CF 回了空 name（``get_tunnel_status`` 的 ``result.get("name", "")``）。

        若不特判，claim 也是空字串時 ``"" == ""`` 會**通過** —— 兩側一起壞成同一個
        垃圾值，比不相等**更容易**成立。空 name ⇒ 無法驗證 ⇒ 拒。
        """
        _cf_ready(mock_cf, cf_name="")

        response = client.put(
            f"/api/tunnels/{TUNNEL_ID}/routes",
            json=dict(ROUTES_BODY, tunnel_name=ATTACKER_OWN_NAME),
        )

        _assert_denied(response, "tunnel_ownership_unverifiable", leaked="\x00")
        mock_cf.configure_tunnel_ingress.assert_not_called()

    @patch("src.api.tunnels.cloudflare_service")
    def test_cf_lookup_error_fails_closed(self, mock_cf, client):
        """CF 查詢本身炸掉 ⇒ 「沒驗到」不等於「驗過了」，不得放行。"""
        _cf_ready(mock_cf)
        mock_cf.get_tunnel_status = AsyncMock(
            side_effect=CloudflareException("upstream boom", status_code=500)
        )

        response = client.put(
            f"/api/tunnels/{TUNNEL_ID}/routes",
            json=dict(ROUTES_BODY, tunnel_name=CF_ACTUAL_NAME),
        )

        assert response.status_code == 502, response.text
        assert response.json()["detail"]["error"] == "tunnel_ownership_check_failed"
        mock_cf.configure_tunnel_ingress.assert_not_called()

    @patch("src.api.tunnels.cloudflare_service")
    def test_missing_tunnel_keeps_delete_idempotent(self, mock_cf, client):
        """CF 上查無此 tunnel ⇒ 沒有別人的資源可傷，必須放行。

        ``CloudflareService.delete_tunnel`` 對 404 是**當成功處理**的（teardown
        重試靠這個冪等性）。若歸屬前置檢查把 404 當拒絕，等於把「重試把殘留收乾淨」
        這條路封死 —— tunnel 已經不在了，DB 那一列卻永遠拆不掉。
        """
        _cf_ready(mock_cf)
        mock_cf.get_tunnel_status = AsyncMock(
            side_effect=CloudflareException(
                f"Tunnel {TUNNEL_ID} not found", status_code=404
            )
        )

        response = client.delete(
            f"/api/tunnels/{TUNNEL_ID}",
            params={"tunnel_name": CF_ACTUAL_NAME},
        )

        assert response.status_code == 200, response.text
        mock_cf.delete_tunnel.assert_called_once_with(TUNNEL_ID)
