"""TS-2: operator 帳號級 list-all + 分頁（L4 reconciler reverse-discovery 地基）。

DP-PAGINATION 已用權威三來源（CF API docs / OpenAPI schema / cloudflare-python SDK）確認：
``dns_records`` / ``cfd_tunnel`` / ``access/apps`` / ``access/apps/{id}/policies`` /
``access/policies`` **5 個 endpoint 全部是 page/per_page offset 分頁**（無 cursor、無不分頁），
皆有 ``result_info.total_count``（cfd_tunnel 缺 total_pages）。故統一用 total_count 主停 +
空頁 fallback 的迴圈，對「server 把 per_page 上限偷壓低」「cfd_tunnel 無 total_pages」都 robust。

守的事故：``_list_dns_records_by_content`` 寫死 ``per_page=100`` 無分頁迴圈 → `*-mcp` 漏 15 筆
（超過 100 筆同 content 的孤兒被當「不存在」）。設計 doc §Phase A.1 / A.2。
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.services.cloudflare import CloudflareException, CloudflareService


@pytest.fixture
def cf():
    svc = CloudflareService()
    svc.api_token = "test-token"
    svc.account_id = "acct-1"
    svc.zone_id = "zone-1"
    svc.domain = "woowtech.io"
    svc.enabled = True
    return svc


def _paged_fake_get(pages, total_count, calls, *, status=200, success=True):
    """回傳 fake_get：依 request 的 ``page`` 參數回對應頁。

    pages: list-of-list，第 N 頁（1-based）的 result。``result_info.total_count`` = total_count
    （None 表示模擬「無 total_count」→ 測空頁 fallback）。calls 記錄每次 request 的 params。
    """
    async def fake_get(url, headers=None, params=None, timeout=None):
        params = dict(params or {})
        calls.append({"url": url, **params})
        page = params.get("page", 1)
        result = pages[page - 1] if 1 <= page <= len(pages) else []
        info = {"page": page, "per_page": params.get("per_page"), "count": len(result)}
        if total_count is not None:
            info["total_count"] = total_count

        class R:
            status_code = status
            text = "fake-cf-error-body"

            @staticmethod
            def json():
                return {"success": success, "result": result, "result_info": info}

        return R()

    return fake_get


# ── 分頁核心（對標 *-mcp 漏 15 筆事故）────────────────────────────────

class TestPaginationWalksAllPages:
    @pytest.mark.asyncio
    async def test_walks_all_pages_until_total_count(self, cf):
        """150 筆分 2 頁（100 + 50），total_count=150 → 必須全撈 150、非只第 1 頁 100。"""
        page1 = [{"id": f"t{i}", "name": f"tun{i}"} for i in range(100)]
        page2 = [{"id": f"t{i}", "name": f"tun{i}"} for i in range(100, 150)]
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([page1, page2], 150, calls))):
            result = await cf.list_all_tunnels()
        assert len(result) == 150, "分頁沒走到底，漏抓第 2 頁（*-mcp 漏 15 筆同型 bug）"
        assert [c.get("page") for c in calls] == [1, 2], (
            f"page 參數沒遞增/沒帶（漏帶分頁 = 把 count 截在第 1 頁）：{[c.get('page') for c in calls]}"
        )
        assert all("per_page" in c for c in calls), "每次 request 必帶 per_page"

    @pytest.mark.asyncio
    async def test_stops_at_last_page_no_extra_request(self, cf):
        """total_count 達標即停，不多打第 3 頁（末頁終止）。"""
        page1 = [{"id": f"t{i}"} for i in range(100)]
        page2 = [{"id": f"t{i}"} for i in range(100, 130)]
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([page1, page2], 130, calls))):
            result = await cf.list_all_tunnels()
        assert len(result) == 130
        assert [c.get("page") for c in calls] == [1, 2], "達 total_count 後不應再打第 3 頁"

    @pytest.mark.asyncio
    async def test_empty_page_fallback_when_no_total_count(self, cf):
        """無 total_count 時靠空頁終止（不能假設 server 一定回 total_count）。"""
        page1 = [{"id": f"t{i}"} for i in range(100)]
        page2 = [{"id": f"t{i}"} for i in range(100, 150)]
        page3 = []
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([page1, page2, page3], None, calls))):
            result = await cf.list_all_tunnels()
        assert len(result) == 150
        assert [c.get("page") for c in calls] == [1, 2, 3], "無 total_count 應走到空頁才停"

    @pytest.mark.asyncio
    async def test_runaway_pagination_raises_not_silently_truncates(self, cf):
        """server 一直回滿頁且無 total_count（病態/被忽略 page 參數）→ max-pages 防護 raise，
        絕不靜默截斷（截斷會讓下游差集把未取到的當孤兒）。"""
        full = [{"id": f"t{i}"} for i in range(100)]

        async def always_full(url, headers=None, params=None, timeout=None):
            class R:
                status_code = 200

                @staticmethod
                def json():
                    return {"success": True, "result": full, "result_info": {"count": 100}}

            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=always_full)):
            with pytest.raises(CloudflareException):
                await cf._get_paginated(cf._tunnels_url(), resource="tunnels", max_pages=5)


# ── fail-closed（絕不回部分/空清單）────────────────────────────────────

class TestFailClosed:
    @pytest.mark.asyncio
    async def test_403_raises_with_status_not_empty(self, cf):
        """403（token scope 不足）→ raise CloudflareException(status_code=403)，
        絕不回空清單（空清單 = 下游把全部當『無孤兒』，與沒權限無法區分）。"""
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([[]], 0, calls, status=403))):
            with pytest.raises(CloudflareException) as exc:
                await cf.list_all_tunnels()
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_success_false_raises(self, cf):
        """HTTP 200 但 success=False（CF 應用層錯）→ raise，不回部分結果。"""
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([[]], 0, calls, success=False))):
            with pytest.raises(CloudflareException):
                await cf.list_all_access_apps()

    @pytest.mark.asyncio
    async def test_mid_pagination_failure_raises_no_partial(self, cf):
        """第 2 頁失敗 → 整個 raise，絕不回第 1 頁的部分結果（部分清單最危險）。"""
        page1 = [{"id": f"t{i}"} for i in range(100)]
        call_n = {"i": 0}

        async def fail_on_page2(url, headers=None, params=None, timeout=None):
            call_n["i"] += 1
            page = dict(params or {}).get("page", 1)

            class R:
                status_code = 200 if page == 1 else 500
                text = "fake-cf-error-body"

                @staticmethod
                def json():
                    if page == 1:
                        return {"success": True, "result": page1,
                                "result_info": {"total_count": 150, "count": 100}}
                    return {"success": False, "errors": "boom"}

            return R()

        with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fail_on_page2)):
            with pytest.raises(CloudflareException):
                await cf.list_all_tunnels()


# ── 軟刪過濾 + 各 endpoint URL 正確 ────────────────────────────────────

class TestSoftDeleteAndEndpoints:
    @pytest.mark.asyncio
    async def test_list_all_tunnels_filters_soft_deleted(self, cf):
        """deleted_at != null 的 tunnel 不入清單（軟刪不是孤兒、別誤回收）。"""
        page = [
            {"id": "t-live", "name": "live", "deleted_at": None},
            {"id": "t-gone", "name": "gone", "deleted_at": "2026-06-01T00:00:00Z"},
            {"id": "t-live2", "name": "live2"},  # 無 deleted_at 欄位 = 活著
        ]
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([page], 3, calls))):
            result = await cf.list_all_tunnels()
        ids = {t["id"] for t in result}
        assert ids == {"t-live", "t-live2"}, f"軟刪過濾錯誤：{ids}"
        assert "cfd_tunnel" in calls[0]["url"]

    @pytest.mark.asyncio
    async def test_list_all_access_apps_url(self, cf):
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([[{"id": "app-1"}]], 1, calls))):
            result = await cf.list_all_access_apps()
        assert [a["id"] for a in result] == ["app-1"]
        assert calls[0]["url"].endswith("/access/apps")
        # access per_page 硬上限未明訂 → 保守值（≤100，落在 CF default 內），防回退成過大值
        assert calls[0]["per_page"] <= 100

    @pytest.mark.asyncio
    async def test_list_policies_of_app_url(self, cf):
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([[{"id": "pol-1"}]], 1, calls))):
            result = await cf.list_policies_of_app("app-xyz")
        assert [p["id"] for p in result] == ["pol-1"]
        assert calls[0]["url"].endswith("/access/apps/app-xyz/policies")

    @pytest.mark.asyncio
    async def test_list_all_reusable_policies_url(self, cf):
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([[{"id": "rp-1"}]], 1, calls))):
            result = await cf.list_all_reusable_policies()
        assert [p["id"] for p in result] == ["rp-1"]
        assert calls[0]["url"].endswith("/access/policies")


# ── DNS 分頁修（*-mcp 漏 15 筆的直接修復）──────────────────────────────

class TestDnsPaginationFix:
    @pytest.mark.asyncio
    async def test_list_dns_by_content_walks_all_pages(self, cf):
        """同一 content 的 CNAME 超過單頁也要全撈（修 per_page=100 無迴圈的截斷）。"""
        content = "abc.cfargotunnel.com"
        page1 = [{"id": f"d{i}", "type": "CNAME", "content": content} for i in range(100)]
        page2 = [{"id": f"d{i}", "type": "CNAME", "content": content} for i in range(100, 115)]
        calls = []
        with patch("httpx.AsyncClient.get",
                   new=AsyncMock(side_effect=_paged_fake_get([page1, page2], 115, calls))):
            result = await cf._list_dns_records_by_content(content)
        assert len(result) == 115, "DNS 分頁沒走到底（這正是 *-mcp 漏 15 筆的形態）"
        assert "dns_records" in calls[0]["url"]
        # content/type filter 仍要帶
        assert calls[0].get("content") == content
        assert calls[0].get("type") == "CNAME"
