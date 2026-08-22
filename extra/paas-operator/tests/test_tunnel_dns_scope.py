"""#531 第 3 點：``reconcile_tunnel_dns`` 的刪除範圍必須是 **tunnel-scoped**。

issue #531 原文說這條端點會「連帶刪除該 tunnel 在**整個 zone** 的其他 DNS 記錄」。
讀碼結論是：撈的 URL 確實是 zone 級（``/zones/{zone_id}/dns_records``），但查詢帶了
``content={tunnel_id}.cfargotunnel.com``，所以集合被收窄到「指向**這條** tunnel」的
記錄。也就是說它不會碰別條 tunnel 的記錄 —— 原文那句照字面讀會誤導。本檔把這個性質
鎖住，讓它之後不能無聲退化。

⚠ **為什麼在 HTTP 層造假，而不是 mock 掉 ``_list_dns_records_by_content``**：
既有的 ``TestReconcileTunnelDns``（test_tunnel_routes_sync.py）正是 patch 掉那個
method，再餵一組「每筆 content 都已經等於本 tunnel target」的 fixture —— 那把**提供
scope 的那一層整個換掉了**，等於假設結論，對「scope 是不是 tunnel 級」零鑑別力。
這裡改成在 ``httpx`` 層放一個會**忠實套用 content filter** 的假 zone：若被測碼哪天
不再送 content（或送錯），假 zone 就會把別條 tunnel 的記錄一起回給它，然後那筆記錄
會被當 stale 刪掉 —— 測試就紅。teeth 在這裡。
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.services.cloudflare import CloudflareService

MINE = "tun-mine-1111"
OTHER = "tun-other-2222"


def _rec(rec_id, name, tunnel_id):
    return {
        "id": rec_id,
        "name": name,
        "type": "CNAME",
        "content": f"{tunnel_id}.cfargotunnel.com",
    }


#: 假 zone 的內容。三筆的角色各自不同，缺一不可：
#:   keep  — 本 tunnel、且在 desired 內 ⇒ 必須保留（證明沒有誤刪）
#:   stale — 本 tunnel、不在 desired 內 ⇒ 必須刪除（陽性對照：證明刪除迴圈**真的會刪**，
#:           否則「別條 tunnel 的記錄還在」可能只是因為它整個沒在運作）
#:   other — **別條** tunnel ⇒ 必須存活（本檔要鎖的性質）
ZONE = [
    _rec("rec-keep", "keep.woowtech.io", MINE),
    _rec("rec-stale", "stale.woowtech.io", MINE),
    _rec("rec-other", "other.woowtech.io", OTHER),
]


@pytest.fixture
def cf_service():
    svc = CloudflareService()
    svc.api_token = "test-token"
    svc.account_id = "test-account"
    svc.zone_id = "test-zone"
    svc.domain = "woowtech.io"
    svc.dns_enabled = True
    return svc


class _FakeZone:
    """會忠實套用 ``type`` / ``content`` filter 的假 Cloudflare zone。"""

    def __init__(self, records):
        self.records = list(records)
        self.deleted = []
        self.list_params = []

    async def get(self, url, headers=None, params=None, timeout=None):
        params = params or {}
        self.list_params.append(dict(params))
        matched = [
            r for r in self.records
            if (params.get("type") is None or r["type"] == params["type"])
            # ⬇ 這一行就是 scope。被測碼沒送 content 時 params.get 回 None，
            #   假 zone 就會把**整個 zone** 回給它 —— 正是我們要偵測的退化。
            and (params.get("content") is None or r["content"] == params["content"])
        ]
        page = int(params.get("page", 1))
        per_page = int(params.get("per_page", 1000))
        chunk = matched[(page - 1) * per_page: page * per_page]
        return _FakeResponse(
            200, {"success": True, "result": chunk,
                  "result_info": {"total_count": len(matched)}},
        )

    async def delete(self, url, headers=None, timeout=None):
        self.deleted.append(url.rsplit("/", 1)[-1])
        return _FakeResponse(200, {"success": True})


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class TestReconcileDnsIsTunnelScoped:
    @pytest.mark.asyncio
    async def test_other_tunnels_cname_survives_a_full_sync(self, cf_service):
        """同 zone 內指向**別條 tunnel** 的 CNAME，在本 tunnel 全量同步後必須還在。"""
        zone = _FakeZone(ZONE)

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=zone.get)), \
                patch("httpx.AsyncClient.delete",
                      new=AsyncMock(side_effect=zone.delete)):
            result = await cf_service.reconcile_tunnel_dns(
                tunnel_id=MINE, subdomains=["keep"], comment="paas-sa-x",
            )

        # 本 tunnel 的 stale 記錄被刪 —— 陽性對照，證明刪除迴圈真的在運作。
        assert "rec-stale" in zone.deleted
        # 別條 tunnel 的記錄必須存活。
        assert "rec-other" not in zone.deleted, (
            "指向別條 tunnel 的 DNS 記錄被刪了 —— reconcile 不再是 tunnel-scoped"
        )
        # 也不准動到本 tunnel 仍然 desired 的那筆。
        assert "rec-keep" not in zone.deleted
        assert zone.deleted == ["rec-stale"], f"刪除範圍超出預期：{zone.deleted}"
        assert result == {"keep": "rec-keep"}

    @pytest.mark.asyncio
    async def test_the_list_query_is_narrowed_by_this_tunnels_cname_target(
        self, cf_service
    ):
        """收窄 scope 的就是那個 ``content=`` 參數 —— 直接把它釘住。

        上一支測「結果對」，這支測「用什麼收窄的」。兩者缺一：只測結果的話，未來
        若有人改成「撈全 zone、再在本地過濾」，結果一樣對、但每次同步都要把整個
        zone 拉回來（而且本地過濾一旦寫錯就是全域誤刪）。
        """
        zone = _FakeZone(ZONE)

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=zone.get)), \
                patch("httpx.AsyncClient.delete",
                      new=AsyncMock(side_effect=zone.delete)):
            await cf_service.reconcile_tunnel_dns(
                tunnel_id=MINE, subdomains=["keep"], comment=None,
            )

        assert zone.list_params, "根本沒有發出 list 查詢 —— 探針壞了，不是通過"
        first = zone.list_params[0]
        assert first.get("content") == f"{MINE}.cfargotunnel.com"
        assert first.get("type") == "CNAME"
