"""#824：paas-operator 對 Cloudflare zone 每一個 DNS 突變點的**歸屬守線**。

缺陷形狀（五處都一樣）：用 hostname 或 CF 的 ``content=`` filter 找到一筆記錄，
然後**沒有任何本地檢查**就改它／刪它。守線是一個純本地、零 API 成本、fail-closed
的述詞：``dns_record_points_at(rec, cname_target)`` —— 記錄自己的 ``content`` 就是
歸屬證據（它字面寫著自己屬於哪一條 tunnel）。

五個突變點（資源側枚舉：問「誰會寫到 zone 的 DNS 記錄」，不是「誰呼叫某個函式」）：

===  ==========================================  ====================================
#    位置                                        方向
===  ==========================================  ====================================
1    ``create_dns_record_for_tunnel`` repoint     create 側 ⇒ **raise**
2    ``reconcile_tunnel_dns`` 的撈取／刪除迴圈    delete 側 ⇒ 跳過 ＋ ERROR log ＋繼續
3    ``create_dns_record`` repoint（legacy）      create 側 ⇒ **raise**
4    ``delete_dns_record(subdomain)``             delete 側 ⇒ 跳過 ＋ ERROR log ＋回 False
5    ``_cleanup_dns_for_tunnel`` 刪除迴圈         delete 側 ⇒ 跳過 ＋ ERROR log ＋繼續
===  ==========================================  ====================================

**方向為什麼不一致**：create 側的搶佔要當場擋下並讓呼叫端知道；delete 側 raise 會讓
整個 sync 中止，下一次 re-sync 撞到同一筆 ⇒ 收斂不了的迴圈，所以是跳過＋大聲 log。

⚠ **為什麼在 httpx 層造假，而不是 mock 掉 ``_list_dns_records_by_content`` /
``list_all_tunnels``**：那兩層正是**提供 scope／提供事實**的那一層，把它們換掉等於
假設結論（見 ``test_tunnel_dns_scope.py`` 檔頭的同一則紀律）。這裡的假 CF 會忠實
套用 ``type`` / ``name`` / ``content`` filter 與分頁，也**忠實重現 Cloudflare 的軟刪除
語意**（DELETE 之後 ``GET /cfd_tunnel/{id}`` 仍回 200、只是多一個 ``deleted_at``，
#833）—— 所以「把存活判定改用 ``get_tunnel_status``」這個具體退化會被
``test_soft_deleted_tunnel_counts_as_gone`` 抓到，而不是矇混過關。

⚠ **#833 修完後，那支的鑑別力性質變了，讀它之前先看這段。** ``get_tunnel_status``
現在會如實轉發 ``deleted_at`` ⇒ 「改用它做存活判定」**在行為上已經是對的**。那支
仍然會紅，但紅的是**呼叫形狀那一格**（``tunnel_get_calls == []``），不是行為。它
從行為守線變成實作形狀守線 —— 而形狀仍然值得鎖，理由（fail-closed 方向不同 ＋
N+1）寫在 ``cloudflare.py`` 的 ``_assert_dns_repoint_allowed`` docstring 裡。
"""
import logging
from unittest.mock import AsyncMock, patch

import pytest

from src.services.cloudflare import (
    CloudflareException,
    CloudflareService,
    DnsRecordOwnershipError,
    dns_record_points_at,
    tunnel_id_from_cname_target,
)

LOGGER_NAME = "src.services.cloudflare"

SHARED_TUNNEL = "tun-shared-0000"      # settings 上那條共用 tunnel（legacy 路徑）
MINE = "tun-mine-1111"                 # 本次操作的專用 tunnel
OTHER_LIVE = "tun-other-live-2222"     # 別人的 tunnel，**還活著**
OTHER_GONE = "tun-other-gone-3333"     # 別人的 tunnel，已從帳號清單消失
OTHER_SOFT = "tun-other-soft-4444"     # 別人的 tunnel，**軟刪除**（清單仍回、但有 deleted_at）


def _target(tunnel_id):
    return f"{tunnel_id}.cfargotunnel.com"


def _rec(rec_id, sub, tunnel_id_or_content):
    content = (
        tunnel_id_or_content
        if "." in tunnel_id_or_content and not tunnel_id_or_content.startswith("tun-")
        else _target(tunnel_id_or_content)
    )
    return {
        "id": rec_id,
        "name": f"{sub}.woowtech.io",
        "type": "CNAME",
        "content": content,
    }


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeCloudflare:
    """假 Cloudflare：zone DNS 記錄 ＋ 帳號級 tunnel 清單，都在 httpx 層。

    ``content_filter_broken=True`` 模擬本單的核心威脅：**CF 的 ``content=`` 沒有把
    集合收窄成預期的那一組**（不是等值比對、或是撈回來到刪掉之間有人改了那筆記錄）。
    那正是「不會刪到別人的」這件事目前完全外包給外部契約的地方。
    """

    def __init__(self, records=(), tunnels=(), content_filter_broken=False):
        self.records = [dict(r) for r in records]
        self.tunnels = [dict(t) for t in tunnels]
        self.content_filter_broken = content_filter_broken
        self.dns_list_params = []      # 每一次 zone DNS list 的 params
        self.tunnel_list_calls = []    # 每一次「帳號級 tunnel 清單」查詢
        self.tunnel_get_calls = []     # 每一次「單筆 tunnel」查詢（get_tunnel_status 的形狀）
        self.puts = []                 # [(record_id, payload)]
        self.posts = []                # [payload]
        self.deleted = []              # 被刪掉的 DNS record id
        self.deleted_tunnels = []
        self.dns_list_status = 200
        self.tunnel_list_status = 200

    # ---------------- 分頁 ----------------
    #: 假 CF 一律把 per_page 壓到 50（真 CF 也會對過大的 per_page 自己壓上限）。
    #: 這讓「沒有分頁迴圈」的實作在測試裡就會漏資料，而不是要等到線上 100+ 筆才爆。
    MAX_PER_PAGE = 50

    @classmethod
    def _paginate(cls, items, params):
        page = int(params.get("page", 1))
        per_page = min(int(params.get("per_page", cls.MAX_PER_PAGE)), cls.MAX_PER_PAGE)
        chunk = items[(page - 1) * per_page: page * per_page]
        return _FakeResponse(
            200,
            {
                "success": True,
                "result": chunk,
                "result_info": {"total_count": len(items)},
            },
        )

    # ---------------- GET ----------------
    async def get(self, url, headers=None, params=None, timeout=None):
        params = dict(params or {})
        if "/cfd_tunnel" in url:
            return self._get_tunnel(url, params)
        if url.endswith("/dns_records"):
            return self._get_dns(params)
        raise AssertionError(f"未預期的 GET：{url}")

    def _get_tunnel(self, url, params):
        tail = url.rsplit("/cfd_tunnel", 1)[1]
        if tail:
            # 單筆查詢 GET /cfd_tunnel/{id}。
            # ⚠ Cloudflare 刪 tunnel 是**軟刪除**：DELETE 之後這裡仍回 **200**，只是
            #    payload 多一個 deleted_at（#833）。假 CF 忠實照做 —— 否則「把存活
            #    判定改用 get_tunnel_status」在測試裡會看起來沒問題，而在真 CF 上
            #    永遠把軟刪 tunnel 判成活的（自癒分支永遠走不到）。
            tunnel_id = tail.strip("/")
            self.tunnel_get_calls.append(tunnel_id)
            for tunnel in self.tunnels:
                if tunnel["id"] == tunnel_id:
                    return _FakeResponse(200, {"success": True, "result": dict(tunnel)})
            return _FakeResponse(404, {"success": False, "errors": ["not found"]})

        self.tunnel_list_calls.append(dict(params))
        if self.tunnel_list_status != 200:
            return _FakeResponse(
                self.tunnel_list_status, {"success": False, "errors": ["boom"]}
            )
        return self._paginate(self.tunnels, params)

    def _get_dns(self, params):
        self.dns_list_params.append(dict(params))
        if self.dns_list_status != 200:
            return _FakeResponse(
                self.dns_list_status, {"success": False, "errors": ["boom"]}
            )
        matched = [
            r for r in self.records
            if (params.get("type") is None or r.get("type") == params["type"])
            and (params.get("name") is None or r.get("name") == params["name"])
            and (
                self.content_filter_broken
                or params.get("content") is None
                or r.get("content") == params["content"]
            )
        ]
        return self._paginate(matched, params)

    # ---------------- 寫入 ----------------
    async def put(self, url, headers=None, json=None, timeout=None):
        self.puts.append((url.rsplit("/", 1)[-1], json))
        return _FakeResponse(200, {"success": True})

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(json)
        return _FakeResponse(201, {"success": True, "result": {"id": "rec-new"}})

    async def delete(self, url, headers=None, params=None, timeout=None):
        tail = url.rsplit("/", 1)[-1]
        if "/cfd_tunnel/" in url:
            self.deleted_tunnels.append(tail)
        else:
            self.deleted.append(tail)
        return _FakeResponse(200, {"success": True})


@pytest.fixture
def cf_service():
    svc = CloudflareService()
    svc.api_token = "test-token"
    svc.account_id = "test-account"
    svc.zone_id = "test-zone"
    svc.domain = "woowtech.io"
    svc.tunnel_id = SHARED_TUNNEL
    svc.enabled = True
    svc.dns_enabled = True
    return svc


def _wire(fake):
    """把假 CF 接到 httpx 的四個動詞上。"""
    return (
        patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=fake.get)),
        patch("httpx.AsyncClient.put", new=AsyncMock(side_effect=fake.put)),
        patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=fake.post)),
        patch("httpx.AsyncClient.delete", new=AsyncMock(side_effect=fake.delete)),
    )


class _Wired:
    """with _Wired(fake): ... —— 一次接上四個動詞。"""

    def __init__(self, fake):
        self._patchers = _wire(fake)

    def __enter__(self):
        for p in self._patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patchers:
            p.stop()
        return False


# --------------------------------------------------------------------------- #
#  述詞本身（純函式，可單獨測）                                                 #
# --------------------------------------------------------------------------- #


class TestOwnershipPredicate:
    def test_content_equal_to_target_is_ownership(self):
        assert dns_record_points_at(_rec("r", "a", MINE), _target(MINE)) is True

    def test_content_pointing_elsewhere_is_not_ownership(self):
        assert dns_record_points_at(_rec("r", "a", OTHER_LIVE), _target(MINE)) is False

    def test_case_and_trailing_dot_are_the_same_dns_name(self):
        """DNS 名稱大小寫不敏感、尾點不影響身分 ⇒ 這是等價正規化，不是放寬比對。"""
        rec = {"content": f"{MINE.upper()}.CFARGOTUNNEL.COM."}
        assert dns_record_points_at(rec, _target(MINE)) is True

    def test_missing_record_or_content_is_never_ownership(self):
        """fail-closed：無法證明歸屬就當作不歸屬。"""
        assert dns_record_points_at(None, _target(MINE)) is False
        assert dns_record_points_at({}, _target(MINE)) is False
        assert dns_record_points_at({"content": None}, _target(MINE)) is False
        assert dns_record_points_at({"content": ""}, _target(MINE)) is False
        # target 自己是空的也不准算相等（否則兩邊一起壞成空字串就「相等＝通過」）
        assert dns_record_points_at({"content": ""}, "") is False

    def test_tunnel_id_extraction_is_exact_suffix_not_substring(self):
        """``abc.cfargotunnel.com.evil.com`` 含有那串字面，但**不是** tunnel 目標。

        判定欄位留了「這個結果不該出現」那一格：不只斷言回 None，也明寫它**不得**
        回 ``abc`` —— 用 substring／``in`` 寫的實作正是回 ``abc``，然後拿去查「這條
        tunnel 還在不在」、查不到就當孤兒接收，正好是本單要擋的搶佔。
        """
        evil = "abc.cfargotunnel.com.evil.com"
        assert tunnel_id_from_cname_target(evil) is None
        assert tunnel_id_from_cname_target(evil) != "abc"

    def test_tunnel_id_extraction_rejects_non_tunnel_targets(self):
        assert tunnel_id_from_cname_target("www.example.com") is None
        assert tunnel_id_from_cname_target("cfargotunnel.com") is None
        assert tunnel_id_from_cname_target(".cfargotunnel.com") is None
        assert tunnel_id_from_cname_target("a.b.cfargotunnel.com") is None
        assert tunnel_id_from_cname_target(None) is None
        assert tunnel_id_from_cname_target("") is None

    def test_tunnel_id_extraction_accepts_a_real_target(self):
        """陽性對照：上面全回 None，可能只是因為函式壞了永遠回 None。"""
        assert tunnel_id_from_cname_target(_target(MINE)) == MINE
        assert tunnel_id_from_cname_target(f"{MINE.upper()}.CFARGOTUNNEL.COM.") == MINE


# --------------------------------------------------------------------------- #
#  站點 1：create_dns_record_for_tunnel 的 repoint 分支                          #
# --------------------------------------------------------------------------- #


class TestSite1CreateForTunnelRepoint:
    @pytest.mark.asyncio
    async def test_repoint_onto_a_live_tunnels_record_is_refused(self, cf_service):
        """既有記錄指向一條**還活著**的別人 tunnel ⇒ 必須 raise，且**沒有任何 PUT**。

        這支是 #824 本體的守線。
        """
        fake = _FakeCloudflare(
            records=[_rec("rec-victim", "app", OTHER_LIVE)],
            tunnels=[{"id": OTHER_LIVE, "name": "paas-sa-victim"},
                     {"id": MINE, "name": "paas-sa-mine"}],
        )

        with _Wired(fake):
            with pytest.raises(DnsRecordOwnershipError) as exc:
                await cf_service.create_dns_record_for_tunnel(
                    subdomain="app", tunnel_id=MINE,
                )

        # 拒絕的是我們自己，不是 CF ⇒ 409，好讓呼叫端分得出來。
        assert exc.value.status_code == 409
        assert OTHER_LIVE in exc.value.message
        # 證明**守衛被叫起來並且擋下了**（不是根本沒走到那段所以沒事）：
        # tombstone 查詢確實發生過，而且一個 PUT 都沒送出去。
        assert fake.tunnel_list_calls, "沒有查過 tunnel 是否還在 —— 守衛沒被走到"
        assert fake.puts == [], f"搶佔已經送出：{fake.puts}"

    @pytest.mark.asyncio
    async def test_orphan_record_is_adopted_onto_the_per_tunnel_target(
        self, cf_service, caplog
    ):
        """既有記錄指向的 tunnel **證實已不存在** ⇒ 允許自癒接收，但要大聲 log。

        接收後必須落在**本 tunnel**的 target，不是共用 default —— 後者是 routing
        黑洞（原 ``test_repoint_existing_uses_per_tunnel_target_not_shared`` 想擋的
        就是這個，那一半在這裡保留下來）。
        """
        fake = _FakeCloudflare(
            records=[_rec("rec-orphan", "app", OTHER_GONE)],
            tunnels=[{"id": MINE, "name": "paas-sa-mine"}],  # OTHER_GONE 不在清單裡
        )

        with _Wired(fake), caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            rec_id = await cf_service.create_dns_record_for_tunnel(
                subdomain="app", tunnel_id=MINE,
            )

        assert rec_id == "rec-orphan"
        assert fake.tunnel_list_calls, "沒有查過 tunnel 是否還在 —— 守衛沒被走到"
        assert len(fake.puts) == 1, f"應該剛好一次 repoint：{fake.puts}"
        record_id, payload = fake.puts[0]
        assert record_id == "rec-orphan"
        assert payload["content"] == _target(MINE)
        assert payload["content"] != _target(SHARED_TUNNEL), "掉回共用 target ＝ routing 黑洞"
        assert any(OTHER_GONE in r.message for r in caplog.records), \
            "接收殘留記錄必須留下可查的訊號"

    @pytest.mark.asyncio
    async def test_soft_deleted_tunnel_counts_as_gone(self, cf_service):
        """軟刪除的 tunnel **不算還在**（#833）。

        這支是「存活判定只能走 ``list_all_tunnels``、不准用 ``get_tunnel_status``」的
        判別式：假 CF 忠實重現 CF 的軟刪除（單筆 GET 仍回 200 ＋ ``deleted_at``）。

        ⚠ **鎖的是呼叫形狀，不是行為**（#833 修完後的誠實說法）。原本的說法是
        「改用 ``get_tunnel_status`` 會把它判成活的 ⇒ raise ⇒ 本支紅」——那在
        ``get_tunnel_status`` 不讀 ``deleted_at`` 的年代成立，現在不成立了：那支
        已經會轉發 ``deleted_at``，改用它在**行為上是對的**。本支仍會紅，紅在最後
        一格 ``tunnel_get_calls == []``。形狀仍然值得鎖，理由（``list_all_tunnels``
        的 fail-closed 方向不同 ＋ 迴圈裡單筆查詢是 N+1）在
        ``cloudflare.py::_assert_dns_repoint_allowed`` 的 docstring。
        """
        fake = _FakeCloudflare(
            records=[_rec("rec-orphan", "app", OTHER_SOFT)],
            tunnels=[
                {"id": OTHER_SOFT, "name": "paas-sa-gone",
                 "deleted_at": "2026-08-22T06:44:52.396846Z"},
                {"id": MINE, "name": "paas-sa-mine"},
            ],
        )

        with _Wired(fake):
            rec_id = await cf_service.create_dns_record_for_tunnel(
                subdomain="app", tunnel_id=MINE,
            )

        assert rec_id == "rec-orphan"
        assert len(fake.puts) == 1
        assert fake.puts[0][1]["content"] == _target(MINE)
        assert fake.tunnel_get_calls == [], \
            "不得用單筆 GET /cfd_tunnel/{id} 判存活 —— 它看不到軟刪除（#833）"

    @pytest.mark.asyncio
    async def test_non_tunnel_content_is_always_refused(self, cf_service):
        """content 根本不是 ``*.cfargotunnel.com`` ⇒ 沒有 tunnel 可查 ⇒ 一律拒。

        ⛔ 這一格不准當成「查不到＝不存在＝可以接收」。
        """
        for content in ("www.example.com", "abc.cfargotunnel.com.evil.com"):
            fake = _FakeCloudflare(
                records=[{"id": "rec-x", "name": "app.woowtech.io",
                          "type": "CNAME", "content": content}],
                tunnels=[{"id": MINE, "name": "paas-sa-mine"}],
            )

            with _Wired(fake):
                with pytest.raises(DnsRecordOwnershipError):
                    await cf_service.create_dns_record_for_tunnel(
                        subdomain="app", tunnel_id=MINE,
                    )

            assert fake.puts == [], f"{content}：搶佔已經送出"
            # 連查都不必查（沒有 tunnel 可查）——順便釘住「不是靠查不到才拒絕的」。
            assert fake.tunnel_list_calls == [], f"{content}：不該有 tunnel 查詢"

    @pytest.mark.asyncio
    async def test_tunnel_lookup_failure_fails_closed(self, cf_service):
        """查詢本身失敗 ⇒ raise，不得 fail-open 當成孤兒接收。"""
        fake = _FakeCloudflare(
            records=[_rec("rec-victim", "app", OTHER_LIVE)],
            tunnels=[{"id": OTHER_LIVE, "name": "paas-sa-victim"}],
        )
        fake.tunnel_list_status = 500

        with _Wired(fake):
            with pytest.raises(CloudflareException):
                await cf_service.create_dns_record_for_tunnel(
                    subdomain="app", tunnel_id=MINE,
                )

        assert fake.puts == [], "查不到就搶 ＝ fail-open"

    @pytest.mark.asyncio
    async def test_matching_record_costs_no_extra_api_call(self, cf_service):
        """happy path（content 相符）零額外成本 —— 釘住 tombstone 查詢只在罕見分支。

        沒有這支，哪天有人把查詢挪到分支外，就變成每次 sync 都打一次 tunnel 清單。
        """
        fake = _FakeCloudflare(
            records=[_rec("rec-mine", "app", MINE)],
            tunnels=[{"id": MINE, "name": "paas-sa-mine"}],
        )

        with _Wired(fake):
            rec_id = await cf_service.create_dns_record_for_tunnel(
                subdomain="app", tunnel_id=MINE,
            )

        assert rec_id == "rec-mine"
        assert fake.tunnel_list_calls == [], "content 相符時不該查 tunnel 清單"
        assert fake.tunnel_get_calls == []
        assert fake.puts == []

    @pytest.mark.asyncio
    async def test_no_existing_record_costs_no_extra_api_call(self, cf_service):
        """另一條 happy path：根本沒有既有記錄 ⇒ 直接 POST，零額外成本。"""
        fake = _FakeCloudflare(tunnels=[{"id": MINE, "name": "paas-sa-mine"}])

        with _Wired(fake):
            rec_id = await cf_service.create_dns_record_for_tunnel(
                subdomain="app", tunnel_id=MINE, comment="paas-sa-x-y",
            )

        assert rec_id == "rec-new"
        assert fake.tunnel_list_calls == []
        assert len(fake.posts) == 1
        assert fake.posts[0]["content"] == _target(MINE)
        assert fake.posts[0]["comment"] == "paas-sa-x-y"


# --------------------------------------------------------------------------- #
#  站點 2：reconcile_tunnel_dns 的撈取／刪除迴圈                                 #
# --------------------------------------------------------------------------- #


class TestSite2ReconcileDeleteLoop:
    @pytest.mark.asyncio
    async def test_out_of_scope_record_is_neither_deleted_nor_adopted(
        self, cf_service, caplog
    ):
        """CF 的 ``content=`` 沒收窄時，本地複驗必須把別人的記錄擋在刪除之外。

        ``content_filter_broken=True`` 就是這個威脅：撈回來的清單裡混進了指向別條
        tunnel 的記錄。這在本單之前**完全外包給 Cloudflare 的外部契約**。
        """
        fake = _FakeCloudflare(
            records=[
                _rec("rec-keep", "keep", MINE),      # 本 tunnel + desired ⇒ 保留
                _rec("rec-stale", "stale", MINE),    # 本 tunnel + 非 desired ⇒ 刪（陽性對照）
                _rec("rec-other", "other", OTHER_LIVE),  # 別條 tunnel ⇒ 絕不可刪
            ],
            tunnels=[{"id": MINE, "name": "paas-sa-mine"},
                     {"id": OTHER_LIVE, "name": "paas-sa-victim"}],
            content_filter_broken=True,
        )

        with _Wired(fake), caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            result = await cf_service.reconcile_tunnel_dns(
                tunnel_id=MINE, subdomains=["keep"], comment="paas-sa-x",
            )

        # 陽性對照：刪除迴圈**真的會刪** —— 否則「別人的還在」可能只是因為它整個沒運作。
        assert "rec-stale" in fake.deleted
        # 本單的守線：
        assert "rec-other" not in fake.deleted, "刪掉了指向別條 tunnel 的記錄"
        assert fake.deleted == ["rec-stale"], f"刪除範圍超出預期：{fake.deleted}"
        # 也不得把別人的記錄當成自己的回報給呼叫端（那會被寫進 Odoo 的 dns_record_id）。
        assert result == {"keep": "rec-keep"}
        assert "rec-other" not in result.values()
        assert any("rec-other" in r.message for r in caplog.records), \
            "略過別人的記錄必須留下 ERROR 級訊號，否則守衛等於沒有輸出"

    @pytest.mark.asyncio
    async def test_in_scope_records_still_reconcile_normally(self, cf_service):
        """陽性對照（乾淨情境）：CF filter 正常時，建/留/刪三種行為都照舊。"""
        fake = _FakeCloudflare(
            records=[
                _rec("rec-keep", "keep", MINE),
                _rec("rec-stale", "stale", MINE),
            ],
            tunnels=[{"id": MINE, "name": "paas-sa-mine"}],
        )

        with _Wired(fake):
            result = await cf_service.reconcile_tunnel_dns(
                tunnel_id=MINE, subdomains=["keep", "fresh"], comment=None,
            )

        assert result == {"keep": "rec-keep", "fresh": "rec-new"}
        assert fake.deleted == ["rec-stale"]
        assert len(fake.posts) == 1


# --------------------------------------------------------------------------- #
#  站點 3：create_dns_record（legacy 共用 tunnel）                               #
# --------------------------------------------------------------------------- #


class TestSite3CreateDnsRecordLegacy:
    @pytest.mark.asyncio
    async def test_repoint_onto_a_live_tunnels_record_is_refused(self, cf_service):
        fake = _FakeCloudflare(
            records=[_rec("rec-victim", "app", OTHER_LIVE)],
            tunnels=[{"id": OTHER_LIVE, "name": "paas-sa-victim"},
                     {"id": SHARED_TUNNEL, "name": "shared"}],
        )

        with _Wired(fake):
            with pytest.raises(DnsRecordOwnershipError) as exc:
                await cf_service.create_dns_record(subdomain="app")

        assert exc.value.status_code == 409
        assert fake.tunnel_list_calls, "沒有查過 tunnel 是否還在 —— 守衛沒被走到"
        assert fake.puts == [], f"搶佔已經送出：{fake.puts}"

    @pytest.mark.asyncio
    async def test_orphan_record_is_adopted_onto_the_shared_target(self, cf_service):
        fake = _FakeCloudflare(
            records=[_rec("rec-orphan", "app", OTHER_GONE)],
            tunnels=[{"id": SHARED_TUNNEL, "name": "shared"}],
        )

        with _Wired(fake):
            ok = await cf_service.create_dns_record(subdomain="app")

        assert ok is True
        assert len(fake.puts) == 1
        assert fake.puts[0][1]["content"] == _target(SHARED_TUNNEL)

    @pytest.mark.asyncio
    async def test_matching_record_costs_no_extra_api_call(self, cf_service):
        fake = _FakeCloudflare(
            records=[_rec("rec-mine", "app", SHARED_TUNNEL)],
            tunnels=[{"id": SHARED_TUNNEL, "name": "shared"}],
        )

        with _Wired(fake):
            ok = await cf_service.create_dns_record(subdomain="app")

        assert ok is True
        assert fake.tunnel_list_calls == []
        assert fake.puts == []

    @pytest.mark.asyncio
    async def test_create_route_reports_dns_failed_not_an_unhandled_exception(
        self, cf_service
    ):
        """下游行為（實測，不是假設）：``create_route`` 的 ``except CloudflareException``
        會接住這個子類 ⇒ 走 #648 既有的 ``dns_failed`` 管線，是「軟失敗但看得見」。

        ingress 那一半照樣建起來（``configured=True``），符合 ``create_route`` 原本
        「DNS 半邊失敗不擋 ingress」的設計。
        """
        fake = _FakeCloudflare(
            records=[_rec("rec-victim", "app", OTHER_LIVE)],
            tunnels=[{"id": OTHER_LIVE, "name": "paas-sa-victim"},
                     {"id": SHARED_TUNNEL, "name": "shared"}],
        )

        with _Wired(fake), \
                patch.object(cf_service, "get_tunnel_config",
                             new=AsyncMock(return_value={"config": {"ingress": []}})), \
                patch.object(cf_service, "update_tunnel_config",
                             new=AsyncMock(return_value=True)):
            outcome = await cf_service.create_route(
                subdomain="app", service_url="http://localhost:80",
            )

        assert outcome.dns_failed is True
        assert outcome.configured is True
        assert fake.puts == []


# --------------------------------------------------------------------------- #
#  站點 4：delete_dns_record(subdomain)                                          #
# --------------------------------------------------------------------------- #


class TestSite4DeleteDnsRecord:
    @pytest.mark.asyncio
    async def test_refuses_to_delete_a_record_owned_by_another_tunnel(
        self, cf_service, caplog
    ):
        """只用 hostname 找、找到就刪 —— 這一處連 CF 的 ``content=`` 收窄都沒有。"""
        fake = _FakeCloudflare(
            records=[_rec("rec-victim", "app", OTHER_LIVE)],
            tunnels=[{"id": OTHER_LIVE, "name": "paas-sa-victim"}],
        )

        with _Wired(fake), caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            ok = await cf_service.delete_dns_record(subdomain="app")

        assert ok is False, "拒絕刪別人的記錄時必須回 False（不得謊報成功刪除）"
        assert fake.deleted == [], "刪掉了不屬於本 deployment 的記錄"
        assert any("rec-victim" in r.message for r in caplog.records), \
            "略過必須留下 ERROR 級訊號"

    @pytest.mark.asyncio
    async def test_deletes_the_record_that_points_at_this_tunnel(self, cf_service):
        """陽性對照：屬於自己的記錄照刪 —— 否則上一支的綠燈可能只是刪除整個壞了。"""
        fake = _FakeCloudflare(
            records=[_rec("rec-mine", "app", SHARED_TUNNEL)],
            tunnels=[{"id": SHARED_TUNNEL, "name": "shared"}],
        )

        with _Wired(fake):
            ok = await cf_service.delete_dns_record(subdomain="app")

        assert ok is True
        assert fake.deleted == ["rec-mine"]

    @pytest.mark.asyncio
    async def test_no_record_at_all_is_still_a_no_op_success(self, cf_service):
        """第三格（「這個結果不該出現」的對照）：沒有記錄時既不刪也不報錯。"""
        fake = _FakeCloudflare(records=[], tunnels=[])

        with _Wired(fake):
            ok = await cf_service.delete_dns_record(subdomain="app")

        assert ok is True
        assert fake.deleted == []

    @pytest.mark.asyncio
    async def test_delete_route_still_removes_ingress_and_returns_true(
        self, cf_service
    ):
        """下游行為（實測）：拒絕刪 DNS 之後，route ingress 仍已移除、``delete_route``
        仍回 True。這是對的結果 —— ingress 是我們自己的，DNS 記錄不是。
        """
        fake = _FakeCloudflare(
            records=[_rec("rec-victim", "app", OTHER_LIVE)],
            tunnels=[{"id": OTHER_LIVE, "name": "paas-sa-victim"}],
        )
        update = AsyncMock(return_value=True)

        with _Wired(fake), \
                patch.object(cf_service, "get_tunnel_config", new=AsyncMock(
                    return_value={"config": {"ingress": [
                        {"hostname": "app.woowtech.io", "service": "http://x"},
                        {"service": "http_status:404"},
                    ]}})), \
                patch.object(cf_service, "update_tunnel_config", new=update):
            ok = await cf_service.delete_route(subdomain="app")

        assert ok is True
        update.assert_awaited_once()
        assert [r.get("hostname") for r in update.await_args.args[0]] == [None]
        assert fake.deleted == []


# --------------------------------------------------------------------------- #
#  站點 5：_cleanup_dns_for_tunnel（delete_tunnel 的清理迴圈）                    #
# --------------------------------------------------------------------------- #


class TestSite5CleanupDnsForTunnel:
    @pytest.mark.asyncio
    async def test_out_of_scope_record_survives_tunnel_deletion(
        self, cf_service, caplog
    ):
        fake = _FakeCloudflare(
            records=[
                _rec("rec-mine", "app", MINE),
                _rec("rec-other", "other", OTHER_LIVE),
            ],
            tunnels=[{"id": MINE, "name": "paas-sa-mine"}],
            content_filter_broken=True,
        )

        with _Wired(fake), caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            await cf_service.delete_tunnel(MINE)

        # 陽性對照：清理迴圈真的會刪（本 tunnel 自己的那筆）。
        assert "rec-mine" in fake.deleted
        assert "rec-other" not in fake.deleted, "刪掉了指向別條 tunnel 的記錄"
        assert fake.deleted == ["rec-mine"]
        assert MINE in fake.deleted_tunnels, "tunnel 本身仍必須被刪除"
        assert any("rec-other" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_cleanup_walks_every_page(self, cf_service):
        """清理迴圈必須走完分頁。

        原實作寫死單次 GET（無分頁迴圈），帶著 ``*-mcp 漏 15 筆`` 那個已經在
        ``_list_dns_records_by_content`` 修掉的分頁 bug：超過一頁的記錄會被當成
        「不存在」而永遠殘留。
        """
        records = [_rec(f"rec-{i:03d}", f"h{i:03d}", MINE) for i in range(120)]
        fake = _FakeCloudflare(records=records, tunnels=[{"id": MINE, "name": "m"}])
        assert len(records) > _FakeCloudflare.MAX_PER_PAGE, "情境本身沒有跨頁 ⇒ 零鑑別力"

        with _Wired(fake):
            await cf_service.delete_tunnel(MINE)

        assert len(fake.deleted) == 120, f"只刪到 {len(fake.deleted)} 筆，分頁沒走完"


# --------------------------------------------------------------------------- #
#  下游行為：這個新例外型別實際怎麼傳到呼叫端（實測，不是讀碼推論）              #
# --------------------------------------------------------------------------- #


class TestOwnershipErrorReachesTheCaller:
    """``DnsRecordOwnershipError`` 繼承 ``CloudflareException``，所以既有的
    ``except CloudflareException`` 會直接接住它 —— 它**不會**變成未處理例外。

    三條下游各驗一條：``create_route`` 的 ``dns_failed``（在 ``TestSite3`` 那邊）、
    provision 端點的 ``dns_failed``、``sync_tunnel_routes`` 的 HTTP 狀態碼。
    """

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from src.main import app

        with patch("src.main.settings") as mock_settings:
            mock_settings.api_key = "test-api-key"
            mock_settings.namespace_prefix = "paas-ws-"
            yield TestClient(app, headers={"X-API-Key": "test-api-key"})

    @patch("src.api.tunnels.cloudflare_service")
    def test_sync_tunnel_routes_surfaces_the_refusal_as_409(self, mock_cf, client):
        """``sync_tunnel_routes`` 把 ``CloudflareException`` 轉成 HTTPException 並帶
        ``e.status_code`` ⇒ 我們自己的拒絕會以 **409** 出現，跟 CF 的 5xx 分得開。
        """
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.domain = "woowtech.io"
        mock_cf.configure_tunnel_ingress = AsyncMock(return_value=None)
        mock_cf.reconcile_tunnel_dns = AsyncMock(
            side_effect=DnsRecordOwnershipError("refusing to repoint app.woowtech.io")
        )

        response = client.put(
            "/api/tunnels/tun-1/routes",
            json={"routes": [{"subdomain": "app",
                              "service_url": "http://localhost:80",
                              "http_host_header": True}]},
        )

        assert response.status_code == 409, response.text
        assert "refusing to repoint" in response.text

    @patch("src.api.tunnels.cloudflare_service")
    def test_provision_reports_dns_failed_instead_of_500(self, mock_cf, client):
        """provision（``POST /api/tunnels``）那條路徑把它吞成 ``dns_failed=True``，
        走 #648／#484 既有的管線 —— 「軟失敗但看得見」，不是硬失敗。
        """
        mock_cf.api_token = "t"
        mock_cf.account_id = "a"
        mock_cf.dns_enabled = True
        mock_cf.create_tunnel = AsyncMock(
            return_value={"tunnel_id": "tun-1", "tunnel_name": "paas-sa-x"}
        )
        mock_cf.get_tunnel_token = AsyncMock(return_value="token-xyz")
        mock_cf.configure_tunnel = AsyncMock()
        mock_cf.create_dns_record_for_tunnel = AsyncMock(
            side_effect=DnsRecordOwnershipError("refusing to repoint app.woowtech.io")
        )

        response = client.post(
            "/api/tunnels",
            json={"name": "paas-sa-x", "hostname": "app.woowtech.io",
                  "service_url": "http://localhost:8080"},
        )

        assert response.status_code == 201, response.text
        data = response.json()
        assert data["dns_failed"] is True
        assert data["dns_record_id"] is None
