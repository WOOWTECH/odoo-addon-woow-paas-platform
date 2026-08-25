"""#833：`get_tunnel_status` 看不見 Cloudflare 的軟刪除。

缺陷形狀：Cloudflare 刪 tunnel 是**軟刪除**——`DELETE` 之後 `GET /cfd_tunnel/{id}`
仍回 **200**，只是 payload 多一個 `deleted_at`。`list_all_tunnels` 有濾
（`cloudflare.py` 的 `return [t for t in tunnels if not t.get("deleted_at")]`），
`get_tunnel_status` **沒濾** ⇒ 一條已刪除的 tunnel 與一條「還在、只是 cloudflared
沒連上」的 tunnel，在回傳上**逐欄相同**。平台端據此寫 `disconnected`，於是
「要重建」與「等連線恢復」在 UI 與 DB 欄位上是同一個值。

⚠ **這一族 bug 的驗收紀律**：壞掉的樣子與正常的樣子一致 ⇒ 「跑起來沒報錯」「回
200」都不是驗收訊號。本檔的判別式是 `test_soft_deleted_is_distinguishable_...`：
它拿**同一條 tunnel**、只翻 `deleted_at` 一個變數，斷言兩次回傳**不相等**。未修
時兩者完全相同 ⇒ 紅。

⚠ **為什麼在 httpx 層造假，而不是 mock 掉 `get_tunnel_status`**：那支正是被測的
對象，把它換掉等於假設結論——而且 repo 內既有的每一支測試都是那樣做的，那正是
這個 bug 能活這麼久的原因（`get_tunnel_status` 的真實實作在本檔之前**零覆蓋**）。
造假手法比照 `test_dns_record_ownership_824.py`；`tests/conftest.py` 的 autouse
`block_real_outbound` 會讓漏 patch 的動詞當場炸，而不是靜默對真 CF 發請求。
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services.cloudflare import CloudflareService

TID = "tun-subject-0001"
CF_NAME = "paas-sa-subject"
DELETED_AT = "2026-08-22T06:44:52.396846Z"   # #833 實撞紀錄裡的那個值


def _tunnel(tunnel_id=TID, *, name=CF_NAME, connections=(), deleted_at=None):
    """一顆 CF cfd_tunnel payload。

    ⚠ `deleted_at` 的兩種「未刪除」形狀都要能造出來：CF 自己回的是 **`null`
    （欄位存在）**（官方 openapi 的 `tunnel_deleted_at`：「Timestamp of when the
    resource was deleted. If `null`, the resource has not been deleted.」），而
    **舊 operator** 的回應則是**整個 key 缺席**。兩者都必須被當成「沒刪」。
    """
    payload = {
        "id": tunnel_id,
        "name": name,
        "status": "inactive",
        "connections": list(connections),
        "created_at": "2026-08-01T00:00:00Z",
    }
    if deleted_at is not _ABSENT:
        payload["deleted_at"] = deleted_at
    return payload


class _Absent:
    def __repr__(self):
        return "<absent>"


_ABSENT = _Absent()


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeCloudflare:
    """假 CF：只服務單筆 `GET /cfd_tunnel/{id}`（本檔要測的就是那一條路）。"""

    def __init__(self, tunnels=()):
        self.tunnels = [dict(t) for t in tunnels]
        self.tunnel_get_calls = []

    async def get(self, url, headers=None, params=None, timeout=None):
        if "/cfd_tunnel" not in url:
            raise AssertionError(f"未預期的 GET：{url}")
        tail = url.rsplit("/cfd_tunnel", 1)[1].strip("/")
        if not tail:
            raise AssertionError("本檔不該打帳號級 tunnel 清單")
        self.tunnel_get_calls.append(tail)
        for tunnel in self.tunnels:
            if tunnel["id"] == tail:
                return _FakeResponse(200, {"success": True, "result": dict(tunnel)})
        return _FakeResponse(404, {"success": False, "errors": ["not found"]})


class _Wired:
    def __init__(self, fake):
        self._patcher = patch(
            "httpx.AsyncClient.get", new=AsyncMock(side_effect=fake.get)
        )

    def __enter__(self):
        self._patcher.start()
        return self

    def __exit__(self, *exc):
        self._patcher.stop()
        return False


@pytest.fixture
def cf_service():
    svc = CloudflareService()
    svc.api_token = "test-token"
    svc.account_id = "test-account"
    svc.zone_id = "test-zone"
    svc.domain = "woowtech.io"
    svc.enabled = True
    svc.dns_enabled = True
    return svc


async def _status(cf_service, tunnel):
    fake = _FakeCloudflare([tunnel])
    with _Wired(fake):
        return await cf_service.get_tunnel_status(tunnel["id"])


# --------------------------------------------------------------------------- #
#  判別式：軟刪除必須與「還在、只是沒連上」可區分                                #
# --------------------------------------------------------------------------- #


class TestSoftDeleteIsVisible:
    @pytest.mark.asyncio
    async def test_soft_deleted_is_distinguishable_from_merely_disconnected(
        self, cf_service
    ):
        """🔴 #833 的判別式。修法沒生效時，這一支就是紅的那一支。

        兩次查的是**同一條 tunnel id、同一個 name、同樣沒有 connections**，
        只翻 `deleted_at` 一個變數。修法生效前 `get_tunnel_status` 不讀
        `deleted_at` ⇒ 兩次回傳**逐欄相同** ⇒ 本斷言紅。

        ⚠ 斷言刻意**不寫成** `assert got["deleted_at"] == DELETED_AT`：那樣未修
        時會紅在 `KeyError`，而 `KeyError` 只證明「欄位不存在」，不證明「兩種狀態
        不可區分」——後者才是 #833 本身。用兩個狀態的輸出相等當判別式，紅的訊息
        才會直接說出 bug。
        """
        merely_down = await _status(cf_service, _tunnel())
        soft_deleted = await _status(cf_service, _tunnel(deleted_at=DELETED_AT))

        assert soft_deleted != merely_down, (
            "#833：軟刪除的 tunnel 與「還在、只是沒連上」的 tunnel 回傳完全相同 "
            f"⇒ 平台無從分辨。兩次都得到：{merely_down!r}"
        )

    @pytest.mark.asyncio
    async def test_soft_delete_timestamp_is_passed_through_verbatim(self, cf_service):
        """轉發的是 CF 的事實本身（時間戳），不是我們算出來的判斷。"""
        got = await _status(cf_service, _tunnel(deleted_at=DELETED_AT))
        assert got["deleted_at"] == DELETED_AT


# --------------------------------------------------------------------------- #
#  陽性對照：否定性斷言必須配「正常那一側仍然正常」                              #
# --------------------------------------------------------------------------- #


class TestLiveTunnelsAreUnaffected:
    @pytest.mark.asyncio
    async def test_disconnected_tunnel_still_reports_empty_connections(
        self, cf_service
    ):
        """真的沒連上（無 `deleted_at`）⇒ 仍然是「沒有 connection」那一格。

        沒有這條，上面那支「不再相同」可能只是把兩種狀態一起弄壞了。
        """
        got = await _status(cf_service, _tunnel())
        assert got["connections"] == []
        assert not got.get("deleted_at"), "活著的 tunnel 不得被標成已刪除"

    @pytest.mark.asyncio
    async def test_cf_payload_without_the_key_at_all_means_not_deleted(
        self, cf_service
    ):
        """CF payload **整個沒有** `deleted_at` 這個 key ⇒ 仍然是「沒刪」。

        官方 openapi 的 `tunnel_cfd_tunnel` **沒有 `required` 陣列**（所有欄位皆
        optional），而實際 response 範例印的是 `"deleted_at": null`。⇒ 兩種形狀
        都可能出現，判準必須是 truthy 而不是 `"deleted_at" in payload`。
        """
        got = await _status(cf_service, _tunnel(deleted_at=_ABSENT))
        assert not got.get("deleted_at")

    @pytest.mark.asyncio
    async def test_connected_tunnel_is_untouched(self, cf_service):
        """有 connection 的 tunnel，既有欄位語意一格都不准變。"""
        conn = {
            "id": "conn-1",
            "type": "cloudflared",
            "origin_ip": "192.168.1.100",
            "opened_at": "2026-08-01T01:00:00Z",
        }
        got = await _status(cf_service, _tunnel(connections=[conn]))
        assert got["tunnel_id"] == TID
        assert got["name"] == CF_NAME
        assert got["status"] == "inactive"
        assert got["connections"] == [
            {
                "connector_id": "conn-1",
                "type": "cloudflared",
                "origin_ip": "192.168.1.100",
                "opened_at": "2026-08-01T01:00:00Z",
            }
        ]
        assert got["created_at"] == "2026-08-01T00:00:00Z"
        assert not got.get("deleted_at")


# --------------------------------------------------------------------------- #
#  route 層：schema 改了但建構式忘了補 ⇒ 欄位被 pydantic 靜默丟棄                #
# --------------------------------------------------------------------------- #


@pytest.fixture
def client():
    with patch("src.main.settings") as mock_settings:
        mock_settings.api_key = "test-api-key"
        mock_settings.namespace_prefix = "paas-ws-"
        yield TestClient(app, headers={"X-API-Key": "test-api-key"})


class TestRouteSurfacesTheSignal:
    """`TunnelStatusResponse` 沒有 `model_config` ⇒ pydantic 預設 `extra='ignore'`。

    ⇒ 只改 service 的 return dict 而沒改 schema、或改了 schema 卻沒改
    `tunnels.py` 那個**逐欄手動組裝**的建構式，欄位都會被**靜默丟棄**：HTTP 仍
    200、body 仍是原本那幾個 key、沒有任何錯誤。那正是 #833 的同族失敗模式，所以
    這一格要有自己的測試，不能只測 service 層。
    """

    @patch("src.api.tunnels.cloudflare_service")
    def test_deleted_at_reaches_the_http_body(self, mock_cf, client):
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"
        mock_cf.get_tunnel_status = AsyncMock(
            return_value={
                "tunnel_id": TID,
                "name": CF_NAME,
                "status": "inactive",
                "connections": [],
                "created_at": None,
                "deleted_at": DELETED_AT,
            }
        )

        response = client.get(f"/api/tunnels/{TID}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert "deleted_at" in body, (
            "`deleted_at` 沒有出現在 HTTP body —— 被 response_model 靜默丟棄了"
            f"（實得的 key：{sorted(body)}）"
        )
        assert body["deleted_at"] == DELETED_AT

    @patch("src.api.tunnels.cloudflare_service")
    def test_old_shape_without_deleted_at_still_serves_200(self, mock_cf, client):
        """service 沒給 `deleted_at` 時，route 不得炸。

        ⚠ 現行 handler 對其餘四欄用的是**下標**而非 `.get()`，下標在 key 缺席時
        raise `KeyError` —— 那不是 `CloudflareException`，會整個繞過錯誤映射變成
        未處理的 500。新欄位不准再加一個這種地雷。
        """
        mock_cf.api_token = "test-token"
        mock_cf.account_id = "test-account"
        mock_cf.get_tunnel_status = AsyncMock(
            return_value={
                "tunnel_id": TID,
                "name": CF_NAME,
                "status": "inactive",
                "connections": [],
                "created_at": None,
            }
        )

        response = client.get(f"/api/tunnels/{TID}")

        assert response.status_code == 200, response.text
        assert response.json()["deleted_at"] is None


# --------------------------------------------------------------------------- #
#  #531 的 `_assert_tunnel_ownership`：軟刪除只放寬「CF 沒回 name」那一格        #
# --------------------------------------------------------------------------- #
#
# 為什麼要動這支：`_assert_tunnel_ownership` 的 docstring 把「CF 回 404」寫成唯一
# 放行例外，理由是 `delete_tunnel` 對 404 當成功、teardown 重試靠這個冪等性收斂。
# **但 CF 是軟刪除，DELETE 成功之後 CF 根本不會回 404** ⇒ 那條專為 teardown 重試
# 寫的例外，在它被寫來服務的情境裡從來沒觸發過。
#
# ⛔ **但這不表示「軟刪 ⇒ 無條件放行」**。今天一條**別人的、已軟刪**的 tunnel 打
# 進寫入端點，name 對不上 ⇒ 403 `tunnel_ownership_mismatch`。無條件放行會把它翻成
# 允許 —— 那是對安全守衛做 **fail-open 翻轉**，而且放行後真的有東西可動：
# `PUT /{id}/routes` 的 `reconcile_tunnel_dns` 會對**共用 zone** 建立／刪除 DNS
# 記錄，`delete_tunnel` 也會先跑 `_cleanup_dns_for_tunnel`。**tunnel 死了，zone 沒死。**
#
# ⇒ 只放寬**唯一**真的會卡死 teardown 的那一格：**軟刪 ＋ CF 沒回 name**。
# `deleted_at` 在這裡的作用不是「放行的理由」，是**把兩種空 name 分開**：
# 活著的空 name（不可驗證 ⇒ 拒）vs 已刪的空 name（沒得驗也不必驗 ⇒ 放）。

OWNER_TUNNEL = "tun-victim-0001"
OWNER_CF_NAME = "paas-sa-victim-abc123"
OTHER_NAME = "paas-sa-attacker-def456"


def _cf_for_ownership(mock_cf, *, cf_name=OWNER_CF_NAME, deleted_at=None):
    mock_cf.api_token = "test-token"
    mock_cf.account_id = "test-account"
    mock_cf.domain = "woowtech.io"
    mock_cf.dns_enabled = True
    result = {
        "tunnel_id": OWNER_TUNNEL,
        "name": cf_name,
        "status": "inactive",
        "connections": [],
        "created_at": None,
        "deleted_at": deleted_at,
    }
    mock_cf.get_tunnel_status = AsyncMock(return_value=result)
    mock_cf.delete_tunnel = AsyncMock()
    mock_cf.configure_tunnel_ingress = AsyncMock()
    mock_cf.reconcile_tunnel_dns = AsyncMock(return_value={})
    return mock_cf


class TestOwnershipUnderSoftDelete:
    @patch("src.api.tunnels.cloudflare_service")
    def test_soft_deleted_without_a_name_keeps_teardown_unblocked(
        self, mock_cf, client
    ):
        """🔴 本單唯一的放寬：軟刪 ＋ CF 沒回 name ⇒ 放行。

        今天這一格會走 `tunnel_ownership_unverifiable` ⇒ **403 ⇒ 那條 tunnel 的
        teardown 被永久擋死**（tunnel 已經不在了，DB 那一列卻拆不掉）。

        ⚠ CF 對軟刪 tunnel 到底還回不回 `name`，官方文件未載明、也沒有授權實測，
        所以這一格是**防禦性**的：若 CF 照回 name，這條分支永遠走不到（無害）；
        若 CF 不回，它就是唯一擋住 teardown 卡死的東西。
        """
        _cf_for_ownership(mock_cf, cf_name="", deleted_at=DELETED_AT)

        response = client.delete(
            f"/api/tunnels/{OWNER_TUNNEL}",
            params={"tunnel_name": OWNER_CF_NAME},
        )

        assert response.status_code == 200, response.text
        mock_cf.delete_tunnel.assert_called_once_with(OWNER_TUNNEL)

    @patch("src.api.tunnels.cloudflare_service")
    def test_live_tunnel_without_a_name_is_still_refused(self, mock_cf, client):
        """陽性對照：**活著**卻回空 name ⇒ 仍然 403 unverifiable。

        沒有這條，上面那支可能只是把空 name 那一整格弄成放行 —— 那會是把守衛關掉，
        不是修 bug。兩支只差 `deleted_at` 一個變數。
        """
        _cf_for_ownership(mock_cf, cf_name="", deleted_at=None)

        response = client.delete(
            f"/api/tunnels/{OWNER_TUNNEL}",
            params={"tunnel_name": OWNER_CF_NAME},
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "tunnel_ownership_unverifiable"
        mock_cf.delete_tunnel.assert_not_called()

    @patch("src.api.tunnels.cloudflare_service")
    def test_soft_deleted_with_a_mismatched_claim_is_still_refused(
        self, mock_cf, client
    ):
        """🔴 **fail-open 守線**：軟刪 ＋ claim 對不上 ⇒ 仍然 403 mismatch。

        這一支釘住的是本單設計裡被打掉的第一版做法（「軟刪 ⇒ 無條件放行」）。
        它在修法前後都應該是綠的；**一旦有人把軟刪判斷提到 name 比對之前無條件
        return，它就會紅** —— 那正是我們要它紅的時候。

        為什麼放行有代價（不是「反正 tunnel 死了沒差」）：放行後 `PUT /routes`
        會對共用 zone 建刪 DNS 記錄。tunnel 死了，zone 沒死。
        """
        _cf_for_ownership(mock_cf, deleted_at=DELETED_AT)

        response = client.delete(
            f"/api/tunnels/{OWNER_TUNNEL}",
            params={"tunnel_name": OTHER_NAME},
        )

        assert response.status_code == 403, response.text
        assert response.json()["detail"]["error"] == "tunnel_ownership_mismatch"
        assert OWNER_CF_NAME not in response.text, (
            "不得把 CF 上的真實 tunnel name 回吐給呼叫端"
        )
        mock_cf.delete_tunnel.assert_not_called()

    @patch("src.api.tunnels.cloudflare_service")
    def test_soft_deleted_with_a_matching_claim_still_passes(self, mock_cf, client):
        """陽性對照：軟刪 ＋ claim 相符 ⇒ 照常放行（行為與今天相同）。"""
        _cf_for_ownership(mock_cf, deleted_at=DELETED_AT)

        response = client.delete(
            f"/api/tunnels/{OWNER_TUNNEL}",
            params={"tunnel_name": OWNER_CF_NAME},
        )

        assert response.status_code == 200, response.text
        mock_cf.delete_tunnel.assert_called_once_with(OWNER_TUNNEL)
