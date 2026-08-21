# -*- coding: utf-8 -*-
"""#815：SH `_do_cleanup` 的「事後複查 route 殘留 Access id」冗餘防線。

## 這一檔守的**不是**一個今天會出事的 bug

issue #815 自陳「非現行 bug，是少一道冗餘防線」。
`smart_home._teardown_routes_access()` 裡「push 回 True」與「清掉
`access_app_id` / `cf_policy_id`」綁在同一個 `if` 的同一段（`smart_home.py`
`if Route._push_access_teardown(info, ...)` 那格），失敗才 `failed.append(...)`
⇒ **今天**「`failed` 是否為空」與「事後查 `_has_access_teardown()` 有無殘留」
是等價的兩把尺，量同一件事。

要補的是 SA 那腿有、SH 這腿沒有的**冗餘**：呼叫端不信 helper 的回傳值，teardown
之後回頭看列上的狀態（SA 在 `security_access.py` `_do_cleanup` 的
`lingering_access = self.route_ids.filtered(lambda r: r._has_access_teardown())`）。
它防的是**下一個**改 `_teardown_routes_access()` 的人讓「清 id」與「計 failed」
分家——那一刻兩把尺才會分歧，而分歧的方向是 SH 無聲放過殘留（#785 原症狀同型）。

⇒ 本檔的斷言全部是「契約漂移那天的行為」，不是「今天修好了什麼洞」。

## 🔴 為什麼主測試**必須** patch `_teardown_routes_access`

**真實路徑造不出「helper 回報乾淨、但列還髒」這個狀態**：push 回 True 的當下
helper 自己就把 id 清了；push 回 False 的話 `failed` 就非空、舊閘自己會擋。
兩把尺今天等價 ⇒ 沒有任何真實輸入能讓它們分歧。

所以主測試把 `SmartHome._teardown_routes_access` 換成一個「回 `[]` 但**不清列**」
的 stub，模擬 issue 點名的那次未來重構。這是**模擬未來的契約漂移，不是今天可達
的狀態**——讀到這裡的人不要把它讀成「這個 bug 現在就會發生」。

patch 一個 helper 來測「呼叫端不信它的回傳值」是正當的：那道冗餘防線的存在理由
就是不信它。stub 被呼叫與否本身也是前提檢查（見各條的第一個斷言）——patch 沒生效
時，下面每一條的結論都是「本條沒測到任何東西」，不是「通過」。

## ⚠ patch 點

- `MOCK_GET_CLIENT` 走 `models.smart_home` 的 namespace（頂部 `from ... import`
  在 import 當下就 bind 了一份 copy，patch 來源模組動不到它），沿用
  `test_sh_teardown_capture_semantics_785` 的常數，理由見
  `docs/decisions/patterns/unittest-mock-dual-namespace-patch.md`。
- `_teardown_routes_access` 是 model 上的方法 ⇒ patch `type(sh)`（registry class），
  不是 module 屬性。

## ⚠ `assertLogs` 的 level 一律傳**數值**

Odoo 的 `netsvc.py` 用 `addLevelName(RUNBOT, "INFO")` 覆寫了 stdlib 的名稱表，
傳字串 `'WARNING'` 會被解析成別的門檻（repo-lint `assertlogs-numeric-level`）。

## A5 為什麼是「閘放錯位置」的唯一機械判別式

SA 的複查放在 `_cleanup_cloudflare()` **之後**（SA 把拆 access 與刪 tunnel 包在
同一個 call 裡）。SH 是兩段（先 access、後 tunnel），而 #785 的
`test_C2_a_failed_access_teardown_never_reaches_delete_tunnel` 已鎖住「access
teardown 沒過就不准碰 tunnel」。逐字照抄 SA 的位置（放到 `_cleanup_tunnel()` 之後）
語意會變成「先把 tunnel 刪掉、再抱怨 access 沒清乾淨」——`state` 照樣是 `error`，
A1／A6 照樣綠，只有 A5（`delete_tunnel` 不得被呼叫）會紅。
"""
import logging
from unittest.mock import MagicMock, patch

from odoo.tests import tagged
from odoo.tools import mute_logger

from .test_sh_teardown_capture_semantics_785 import (
    LOG_SH,
    LOG_SH_ROUTE,
    MOCK_GET_CLIENT,
    _ShTeardownFixture,
    _sync_result,
)

#: 既有分支（`failed_access` 非空）在 `error_message` 上的簽名。#785／PR #813 的
#: 成敗語意表在斷言它，本次不得改動。
_FAILED_MARK = 'Access teardown failed'

#: 新的 lingering 分支在 `error_message` 上的簽名。
#: 🔴 刻意**不是** `_FAILED_MARK` 的超字串或子字串——否則 `in` 比對分不開兩個
#: 分支，新防線真的響時輸出與舊分支不可區分，等於加了一道永遠不知道有沒有響過
#: 的守衛。兩個方向都要驗：lingering 那格要有這句、且**沒有** `_FAILED_MARK`。
_LINGERING_MARK = 'still hold Access object id'


@tagged('post_install', '-at_install')
class TestShDoCleanupLingeringAccess(_ShTeardownFixture):
    """`_do_cleanup()` 在 teardown 之後、刪 tunnel 之前的事後複查。"""

    # ------------------------------------------------------------------ #
    #  helpers                                                            #
    # ------------------------------------------------------------------ #

    def _stub_teardown(self, sh, returns=None):
        """把 `_teardown_routes_access` 換成「回指定值、**不動列**」的 stub。

        回傳 `patch` 的 context manager 與那顆 mock；mock 是前提檢查用的——
        它沒被呼叫就代表 patch 沒接上，那一條測到的是空氣。
        """
        stub = MagicMock(return_value=list(returns or []))
        return patch.object(
            type(sh), '_teardown_routes_access', stub), stub

    # ------------------------------------------------------------------ #
    #  A1：主測試（補這道閘之前必須紅在 state 那一行，實際值 `deleting`）    #
    # ------------------------------------------------------------------ #

    def test_A1_lingering_row_lands_error_even_when_the_helper_claims_clean(
            self):
        """helper 回報「這輪乾淨」但列上還有 Access id ⇒ 必須落 `error`。

        🔴 這是**模擬契約漂移**，不是今天可達的狀態（見檔頭）。stub 回 `[]`
        而不清列，正是 issue 點名的那次未來重構——「清 id」與「計 failed」分家。

        補這道閘之前：`failed_access` 為空 ⇒ 直接往下走，`_cleanup_tunnel()`
        把 tunnel 刪掉、`state` 寫成 `deleting`。那是一個「這台已經清乾淨了」的
        假成功訊號，而訂閱已取消、cron 之後不會再回來看這筆 ⇒ CF 上那組 Access
        app ＋ reusable policy 永久留著。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        self._app_id(route, 'app-815-a1')
        client = self._client()
        patcher, stub = self._stub_teardown(sh, returns=[])

        self.assertTrue(
            route._has_access_teardown(),
            '🔴 前提檢查：列上必須真的有 Access id 要拆。這一格不成立時，下面的'
            '結論是「本條沒測到任何東西」，不是「通過」')

        with patcher, patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        self.assertTrue(
            stub.called,
            '🔴 前提檢查：stub 必須真的取代掉 `_teardown_routes_access`；沒接上'
            '的話走的是真 helper，本條測到的是空氣')
        self.assertTrue(
            route._has_access_teardown(),
            '🔴 前提檢查：stub 不得清列——這一條要造的就是「helper 說乾淨、列還'
            '髒」，列被清掉的話等於前提沒擺成功')
        self.assertEqual(
            sh.state, 'error',
            'helper 回報乾淨不算數：列上還握著 CF Access app／policy 的 id 就是'
            '沒清乾淨，寫 deleting ＝ 假成功訊號，而 cron 不會再回來')
        self.assertTrue(
            sh.tunnel_id,
            'tunnel 錨點必須留著給下一輪 cron 重試')

    # ------------------------------------------------------------------ #
    #  A2：收斂對照（沒有這一格，「補了就一律擋刪」也全綠）                 #
    # ------------------------------------------------------------------ #

    def test_A2_clean_rows_still_converge_to_deleting(self):
        """列**乾淨** ＋ helper 回 `[]` ⇒ 照常收斂到 `deleting`。

        本條證明新閘擋的是**列上有殘留**，不是「一律擋刪」。把無聲漏拆換成無謂
        擋刪同樣是錯的修法，而那個錯法會讓 A1 全綠。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        client = self._client()
        patcher, stub = self._stub_teardown(sh, returns=[])

        self.assertFalse(
            route._has_access_teardown(),
            '🔴 前提檢查：這一條的前提是列上乾淨')

        with patcher, patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        self.assertTrue(stub.called, '🔴 前提檢查：stub 必須真的被走到')
        self.assertEqual(
            sh.state, 'deleting',
            '列乾淨就該收斂——新的事後複查不得阻擋正常刪除')
        self.assertTrue(
            client.delete_tunnel.called,
            '收斂路徑上 tunnel 必須真的被拆掉')

    # ------------------------------------------------------------------ #
    #  A3／A4：既有語意不變（#785／PR #813 的成敗語意表）                   #
    # ------------------------------------------------------------------ #

    @mute_logger(LOG_SH_ROUTE, LOG_SH)
    def test_A3_a_real_teardown_failure_keeps_the_existing_message(self):
        """端到端（不 patch helper）：推失敗 ⇒ 仍落 `error` ＋ 既有訊息。

        這一條刻意走真實路徑：operator 掛掉 ⇒ `failed_access` 非空**而且**列上
        的 id 留著 ⇒ 兩個條件同時成立。合併成同一道閘之後，訊息必須仍然是既有
        那條（`failed_access` 優先），否則 #813 的成敗語意表被改掉了。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        self._app_id(route, 'app-815-a3')
        client = self._client(sync_side_effect=Exception('operator down'))

        with patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        self.assertTrue(
            client.sync_access_app.called,
            '🔴 前提檢查：teardown 必須真的被推出去過')
        self.assertEqual(sh.state, 'error')
        self.assertIn(
            _FAILED_MARK, sh.error_message or '',
            '既有訊息的語意不得被新分支蓋掉（#813 成敗語意表）')
        self.assertFalse(
            client.delete_tunnel.called,
            'Access 拆不掉就不准往下刪 tunnel（#785 C2 的不變式）')

    @mute_logger(LOG_SH)
    def test_A4_failed_labels_take_precedence_over_the_lingering_wording(self):
        """`failed_access` 非空、列乾淨 ⇒ 訊息仍是既有那條，不是 lingering 那條。

        A3 是端到端；本條把兩個觸發條件**拆開**：helper 回報失敗標籤，但列上沒有
        任何殘留。合併閘若把兩格的訊息寫反（或無條件用新措辭），A3 因為兩格同時
        成立而測不出來，本條會紅。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        client = self._client()
        patcher, stub = self._stub_teardown(sh, returns=['route-815-a4'])

        self.assertFalse(
            route._has_access_teardown(),
            '🔴 前提檢查：這一條的前提是列上乾淨、只有 helper 回報失敗')

        with patcher, patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        self.assertTrue(stub.called, '🔴 前提檢查：stub 必須真的被走到')
        self.assertEqual(sh.state, 'error')
        self.assertIn(
            _FAILED_MARK, sh.error_message or '',
            'helper 回報失敗時的訊息語意不得改變')
        self.assertIn(
            'route-815-a4', sh.error_message or '',
            '既有訊息會把失敗的 route 標籤列出來——那是維運唯一的線索')

    # ------------------------------------------------------------------ #
    #  A5：位置正確性（閘放錯位置的唯一機械判別式）                        #
    # ------------------------------------------------------------------ #

    @mute_logger(LOG_SH)
    def test_A5_lingering_never_reaches_delete_tunnel(self):
        """lingering 觸發時，那顆還在計費的 tunnel 不准被碰。

        新閘必須放在 `_cleanup_tunnel()` **之前**。放到之後的話語意變成「先把
        tunnel 刪掉、再抱怨 access 沒清乾淨」，與 #785 C2 鎖住的不變式相反，而且
        `state` 照樣是 `error` ⇒ 只有這一條會紅。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        self._app_id(route, 'app-815-a5')
        tunnel_id = sh.tunnel_id
        client = self._client()
        patcher, stub = self._stub_teardown(sh, returns=[])

        self.assertTrue(
            route._has_access_teardown(),
            '🔴 前提檢查：列上必須真的有殘留')

        with patcher, patch(MOCK_GET_CLIENT, return_value=client):
            sh._do_cleanup()

        self.assertTrue(stub.called, '🔴 前提檢查：stub 必須真的被走到')
        self.assertFalse(
            client.delete_tunnel.called,
            '🔴 Access 沒清乾淨就不准往下刪 tunnel')
        self.assertEqual(
            sh.tunnel_id, tunnel_id,
            'tunnel 錨點必須留著；被清掉的話下一輪重試連對象都沒有')

    # ------------------------------------------------------------------ #
    #  A6：兩種原因必須可區分（否則新防線響了也沒人知道）                   #
    # ------------------------------------------------------------------ #

    def test_A6_the_two_reasons_are_distinguishable(self):
        """lingering 那格的 `error_message` 與 log 都要與既有那格分得開。

        兩格共用同一個 `state='error'`，訊息與 log 是**唯一**能分出「這輪卡在
        哪」的東西。措辭撞在一起的話，新防線真的響時輸出與舊分支一模一樣——那等
        於加了一道你永遠不知道有沒有響過的守衛。

        log 一併鎖 `lingering_access` 的 route id（比照 SA 的
        `[SA] _do_cleanup incomplete ... lingering_access=%s`）：沒有 id，維運
        拿不到「是哪幾條 route 還髒」這個唯一的把手。
        """
        sh = self._smart_home(tunnel=True)
        route = self._route(sh)
        self._app_id(route, 'app-815-a6')
        client = self._client()
        patcher, stub = self._stub_teardown(sh, returns=[])

        self.assertTrue(
            route._has_access_teardown(),
            '🔴 前提檢查：列上必須真的有殘留')
        self.assertEqual(
            sh.sudo().route_ids.ids, [route.id],
            '🔴 前提檢查：這台 SH 底下只有這一條 route——下面比對的是完整的'
            'lingering id 清單，多一條就對不上')

        with patcher, patch(MOCK_GET_CLIENT, return_value=client), \
                self.assertLogs(LOG_SH, level=logging.WARNING) as cm:
            sh._do_cleanup()

        self.assertTrue(stub.called, '🔴 前提檢查：stub 必須真的被走到')
        message = sh.error_message or ''
        self.assertIn(
            _LINGERING_MARK, message,
            'lingering 那格要說得出「helper 回報成功但列上還有 id」')
        self.assertNotIn(
            _FAILED_MARK, message,
            '🔴 兩格的措辭必須分得開；用同一句話的話，新防線響了與舊分支響了'
            '在輸出上不可區分')
        joined = '\n'.join(cm.output)
        self.assertIn(
            'lingering_access=[%d]' % route.id, joined,
            'log 要指名是哪幾條 route 還髒——比照 SA 帶上 lingering_access.ids')
