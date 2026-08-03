# -*- coding: utf-8 -*-
"""#525 B5：狀態輪詢器把「刪一半的服務」靜默 commit 掉，端點還回 `success: True`。

## 缺陷本體（與 #520 同構，但比它更隱蔽）

`_update_service_status` 在 `except PaaSOperatorError` 的 404 分支裡呼叫
`service.unlink()`。`CloudService.unlink()` 是**多步驟 teardown**：

    _cleanup_helm() fail-loud 守衛 → sidecar MCP 刪除 → super().unlink()
    → 訂閱結算（取消 ＋ 按比例退點）→ ns quota → agent reach

中途拋錯就留下刪一半的狀態。而那一行在 **`except` handler 內**——Python 不會讓
同一個 `try` 的 sibling `except Exception`（本函式末尾）接到它，例外直接**逸出**
`_update_service_status`，落到呼叫端 `_list_services` 的 catch-all。

## 今天的實際症狀不是殘留，是整支清單端點 500（實測結果，不是 #525 的預測）

`_list_services` 的 catch-all 接到例外後，**自己那行 log 讀 `svc.name`**，而該
record 剛被 `super().unlink()` 刪掉 ⇒ 再拋 `MissingError`；這次同樣是在 except
handler 裡拋的，一路逸出到框架 → **整筆交易被框架 rollback**。所以：

- 殘留今天**不會落地**（被框架撿回去了）
- 代價是**租戶整個服務清單頁 500**，且在 teardown 能成功之前不會自癒

⚠ **那道保護只有一個 token 寬。** 把 log 換成 `svc.id`、或把 name 在 `try` 之前
先取下來（兩者都是很自然的「改善 log」重構），吞例外就會真正完成，殘留立刻落地。
本檔開發時做過這個實驗：只把那行改成讀事先取好的名字、savepoint 仍拿掉 ⇒ 端點回
`{'success': True, 'data': [], 'count': 0}`、`row_exists=False`、
`sub_state='in_progress'` ＝ **#520 bug class 完整重現**。

⇒ savepoint 的價值有二：**修掉今天的 500**，並讓「不留殘留」不再依賴那個意外。

## 可達性（先讀，這決定了哪幾條測試是真的）

404-unlink 分支要成立需要 `state == 'deleting'`。三個呼叫端只有一個把 `deleting`
算進輪詢範圍：

| 呼叫端 | 輪詢的 state | 上層有沒有 catch-all | 判定 |
| --- | --- | --- | --- |
| `_list_services`（`paas.py`） | deploying/initializing/upgrading/**deleting** | **有**（吞掉） | ✅ **可達且會殘留** |
| `_get_service`（`paas.py`） | deploying/upgrading/initializing | 無 → 框架 rollback | 分支不可達 |
| `_cron_check_deploying_services`（model） | deploying/upgrading/initializing | 有（吞掉） | 分支**今天不可達** |

⇒ **`_list_services` 是唯一可達的路徑**，本檔的端點級測試都走它。model 層
（`_poll_and_update_status`）結構完全同形但 cron 的 domain 不含 `deleting`，
今天打不到；那一側的 savepoint 是結構防護，本檔以直接呼叫的方式守住它，
**不假裝它是今天的線上事故**。

## 注入點為什麼挑「訂閱結算」

`unlink()` 在 `super().unlink()` **之後**只有三個步驟，逐一查過可不可能拋：

- `_settle_on_entity_delete` → `refund_wallet_points`：**會拋**
  （`res_partner_wallet.py:19/22` 兩條 `raise UserError`）。← 注入點選這裡
- `_push_ns_quota`：自帶 `except Exception` 只 log（`cloud_service.py:2590`），拋不出來。
- `_reconcile_agent_runtime_reach`：只有 GRANT 才 fail-loud
  （`project_project.py:400`）；刪服務是 REVOKE-only（`added` 恆空）⇒ 走 best-effort
  分支，拋不出來。

所以「結算失敗」是這個窗口**唯一**活著的拋出路徑，不是隨手挑的。注入用 patch 讓它
拋，模擬的是「結算那一步失敗」這個真實可能，不是憑空造一個不存在的失敗。

⚠ 刻意沒做的事：不為了造 RED 去放寬 ACL / ir.rule / 改 cron domain。

## 測試結構（前提要跟著斷言，否則整組退化成恆真）

「沒有殘留」的斷言配兩條把**前提**釘死的測試：

1. `test_poll_reaps_a_deleting_service_when_the_release_is_already_gone`
   ——陽性對照：沒注入時這條路徑**真的會刪**。少了它，fixture 一旦走不到
   `unlink()`（例如 state 打錯、operator client 沒 patch 到），「沒有殘留」
   就變成「根本沒發生過刪除」，全綠而缺陷完好。
2. `test_injection_fires_after_the_service_row_is_deleted`
   ——注入當下服務列**已經**從 DB 消失（＝`super().unlink()` 已落地）。少了它，
   注入點一被重排到 `super().unlink()` 之前，殘留不再產生，下面「沒有殘留」
   同樣退化成恆真。

探針一律用 raw SQL 打 `woow_paas_platform_cloud_service`，不經 ORM cache——
`unlink()` 之後 recordset 的 `exists()` 會受 cache 與 savepoint 的 `cr.clear()`
影響，raw SQL 問的是「這一列現在在不在交易裡」，正是殘留的定義。
"""
import json

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.sh_subscription_fix import (
    ShSubscriptionFix,
)
from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorError,
)

_CTRL = 'odoo.addons.woow_paas_platform.controllers.paas'
_SVC = 'odoo.addons.woow_paas_platform.services.paas_operator'


@tagged('post_install', '-at_install')
class TestPollTeardownLeavesNoHalfDeletedService(HttpCase):
    """輪詢器遇到 404 收屍時，teardown 失敗不得留下刪一半的服務。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id
        company = self.env.company.sudo()

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 525 B5', 'owner_id': self.admin_user.id})

        # 自建 plan／product：不綁任何 seed 商品的定價與上架狀態
        # （#478/#494 教訓——把當下的商業事實寫死成契約）。
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 525 B5', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 300.0, 'sh_no_of_billing_cycle': -1})
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Product 525 B5', 'type': 'service', 'list_price': 300.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])]})

        today = date.today()
        self.sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.partner.id,
            'sh_company_id': company.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 300.0,
            'sh_qty': 1,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_start_date': today - timedelta(days=10),
            'sh_date_of_next_payment': today + timedelta(days=20),
            'sh_taxes_ids': [(6, 0, [])],
            'state': 'in_progress'})

        self.template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'App 525 B5', 'slug': 'app-525-b5',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-525-b5',
            'helm_chart_version': '0.0.0',
            'post_deploy_init_type': 'none',
            'is_active': True, 'is_hidden': False})

        # `deleting` ＋ 有 helm 身分 ＝ 輪詢器收屍分支的入口條件。
        self.service = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': 'Svc 525 B5',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subscription_id': self.sub.id,
            'state': 'deleting',
            'helm_namespace': 'paas-ws-525b5',
            'helm_release_name': 'svc-525-b5'})
        self.service_id = self.service.id

    # ── 探針 ───────────────────────────────────────────────────────────
    def _row_exists(self, cr=None):
        """服務列現在還在不在交易裡——殘留的定義，繞過 ORM cache 直接問 DB。"""
        cr = cr or self.env.cr
        cr.execute(
            'SELECT 1 FROM woow_paas_platform_cloud_service WHERE id = %s',
            (self.service_id,))
        return bool(cr.fetchone())

    def _sub_state(self):
        self.env.invalidate_all()
        return self.sub.sudo().read(['state'])[0]['state']

    # ── 替身：operator 回 404（release 已經不在叢集上）─────────────────
    def _operator_404(self):
        client = MagicMock()
        client.get_status.side_effect = PaaSOperatorError(
            'release not found', status_code=404)
        return client

    def _patched(self, client, injection=None):
        """把兩個 `get_paas_operator_client` 一起釘住。

        controller 是 import-time 綁定（patch controller 模組的名字），
        `_cleanup_helm` / `_push_ns_quota` 是 call-time `from ..services...`
        （patch services 模組的名字）。只 patch 一邊會讓 `unlink()` 裡的
        `_cleanup_helm` 走進未預期的分支。回 None ＝ 官方文件寫的
        「operator unconfigured — no live release to tear down」→ 回 True。
        """
        stack = [
            patch(f'{_CTRL}.get_paas_operator_client', return_value=client),
            patch(f'{_SVC}.get_paas_operator_client', return_value=None),
        ]
        if injection is not None:
            stack.append(injection)
        return stack

    # ── 注入：在 super().unlink() 之後、訂閱結算當下拋例外 ─────────────
    def _fail_settlement(self, recorder=None):
        service_id = self.service_id

        def boom(sub_self, reason='Service deleted'):
            if recorder is not None:
                # 用**這個 request 自己的 cursor** 問，不能用 self.env.cr：
                # HttpCase 的請求跑在另一條執行緒上、持有 test cursor 的鎖。
                sub_self.env.cr.execute(
                    'SELECT 1 FROM woow_paas_platform_cloud_service '
                    'WHERE id = %s', (service_id,))
                recorder.append(bool(sub_self.env.cr.fetchone()))
            raise UserError('injected: subscription settlement failed')

        return patch.object(
            ShSubscriptionFix, '_settle_on_entity_delete', boom)

    # ── 使用者唯一入口（HTTP JSON-RPC）─────────────────────────────────
    def _list_services(self, client, injection=None):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call',
                           'params': {'action': 'list'}})
        patches = self._patched(client, injection)
        for p in patches:
            p.start()
        try:
            resp = self.url_open(
                '/api/workspaces/%s/services' % self.workspace.id,
                data=body, headers={'Content-Type': 'application/json'})
        finally:
            for p in reversed(patches):
                p.stop()
        parsed = resp.json()
        self.assertIn(
            'result', parsed,
            '端點回了框架層錯誤而不是 JSON-RPC result——今天沒有 savepoint 時的'
            '真實症狀：catch-all 自己 log `svc.name` 撞 MissingError 再逸出。'
            'resp: %s' % parsed)
        self.env.invalidate_all()
        return parsed['result']

    # ================================================================
    #  前提 1（陽性對照）：沒注入時這條路徑真的會刪
    # ================================================================
    def test_poll_reaps_a_deleting_service_when_the_release_is_already_gone(self):
        """少了這條，fixture 一旦走不到 `unlink()`，下面「沒有殘留」的斷言
        就變成「根本沒發生過刪除」——全綠而缺陷完好。"""
        self.assertTrue(self._row_exists(), 'fixture 前提：服務列一開始存在')

        result = self._list_services(self._operator_404())

        self.assertTrue(result.get('success'), '清單端點應成功（回 %r）' % result)
        self.assertFalse(
            self._row_exists(),
            'operator 回 404 且服務在 deleting ⇒ 輪詢器應該收屍把列刪掉。'
            '沒刪代表根本沒走到 `unlink()`，本檔其餘斷言全部零鑑別力')
        self.assertNotIn(
            self.service_id, [s['id'] for s in result['data']],
            '收屍成功後服務不該再出現在清單裡')

    # ================================================================
    #  前提 2：注入確實發生在 super().unlink() 之後
    # ================================================================
    def test_injection_fires_after_the_service_row_is_deleted(self):
        """注入點若跑到 `super().unlink()` 之前，殘留根本不會產生，
        「沒有殘留」就退化成恆真——而假綠與真綠的輸出一模一樣。"""
        seen = []

        self._list_services(self._operator_404(),
                            injection=self._fail_settlement(recorder=seen))

        self.assertEqual(
            len(seen), 1,
            '`_settle_on_entity_delete` 必須被呼叫到——沒被呼叫代表注入根本'
            '沒生效（或 fixture 的訂閱不是 in_progress）；實際 %r' % (seen,))
        self.assertFalse(
            seen[0],
            '注入觸發時服務列必須**已經**從 DB 消失（＝`super().unlink()` 已'
            '落地），「沒有殘留」的斷言才有守到東西')

    # ================================================================
    #  殘留：刪一半的服務不得留下
    # ================================================================
    def test_teardown_failure_does_not_break_the_service_list_endpoint(self):
        """今天（沒有 savepoint 時）的真實症狀：**整支清單端點掛掉**。

        機制：teardown 中途失敗 → 例外逸出 `_update_service_status` →
        `_list_services` 的 catch-all 接到 → 它自己那行 log 讀 `svc.name`，而該
        record 已被 `super().unlink()` 刪掉 → `MissingError` 在 except handler
        裡再拋一次 → 逸出到框架 → 整筆交易 rollback ＋ JSON-RPC error。

        租戶端的後果不是「一台服務刪不掉」，是**整個工作區的服務清單打不開**，
        而且在 teardown 能成功之前不會自癒。這條是本檔在現行碼上真正的 RED。
        """
        result = self._list_services(self._operator_404(),
                                     injection=self._fail_settlement())

        self.assertTrue(
            result.get('success'),
            '單一服務的 teardown 失敗不得讓整批清單失敗——輪詢器「一台壞不影響'
            '整批」的設計意圖就是為了這個（回 %r）' % result)

    def test_teardown_failure_leaves_no_half_deleted_service(self):
        result = self._list_services(self._operator_404(),
                                     injection=self._fail_settlement())

        self.assertTrue(
            self._row_exists(),
            'teardown 中途失敗 ⇒ 已刪掉的那一半必須回捲。留著等於 request 結束時'
            '照常 commit：服務列與 cascade 子表沒了，而後面的訂閱結算沒跑完。'
            '（回應：%r）' % result)

    def test_failed_teardown_does_not_orphan_the_subscription(self):
        """殘留的具體危害：服務不見了、訂閱還在 in_progress。

        `subscription_id` 是 `ondelete='set null'`（只保護反向），刪服務不會動
        訂閱——`unlink()` 之所以在 `super().unlink()` 之後補一段結算，就是為了
        堵這個孤兒（#318）。結算那一步失敗而刪除照樣落地時，孤兒訂閱重新出現，
        計費 cron 會繼續對一個已經不存在的服務扣款。
        """
        self._list_services(self._operator_404(),
                            injection=self._fail_settlement())

        row = self._row_exists()
        state = self._sub_state()
        self.assertFalse(
            (not row) and state == 'in_progress',
            '服務列已消失但訂閱仍 in_progress ⇒ 孤兒訂閱，計費 cron 會繼續'
            '扣款（#318 的危害被重新打開）；實際 row_exists=%r sub_state=%r'
            % (row, state))

    def test_service_stays_listed_so_the_next_poll_can_retry(self):
        """回捲之後服務要留在 `deleting`，下一輪 poll 才收得掉。

        端點這一支回的仍是 `success: True`（輪詢器的 catch-all 是它的設計，
        本 PR 不動）——所以「使用者拿得回這台服務」的唯一憑據就是它還在清單上。
        """
        result = self._list_services(self._operator_404(),
                                     injection=self._fail_settlement())

        listed = {s['id']: s['state'] for s in result['data']}
        self.assertIn(
            self.service_id, listed,
            'teardown 失敗後服務必須還在清單上；不在清單上又沒刪乾淨 ＝ 租戶'
            '既看不到它也救不回它（回應：%r）' % result)
        self.assertEqual(
            listed[self.service_id], 'deleting',
            '回捲後狀態要留在 deleting，下一輪 poll 才會再收一次')

    # ================================================================
    #  前提 3：例外確實逸出 `_update_service_status`（整套分析的地基）
    # ================================================================
    def test_teardown_failure_escapes_update_service_status(self):
        """`unlink()` 在 `except` handler 內，同層 sibling 的 `except Exception`
        接不到它——這是「例外會跑到 `_list_services` 被吞掉」的前提。

        這條兩邊都綠（修不修都會拋），它守的是**結構**：哪天有人把 `unlink()`
        搬進主 `try`，例外就會被本函式末尾的 catch-all 就地吃掉，上面那幾條
        「沒有殘留」會在缺陷仍在的情況下集體變成恆真。
        """
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController

        fake_request = MagicMock()
        fake_request.env = self.env
        patches = self._patched(self._operator_404(),
                                injection=self._fail_settlement())
        patches.append(patch(f'{_CTRL}.request', new=fake_request))
        for p in patches:
            p.start()
        try:
            with self.assertRaises(UserError, msg=(
                    '例外必須逸出 `_update_service_status`；被就地吃掉代表 '
                    '`unlink()` 已被搬進主 try，本檔其餘前提全部失效')):
                PaasController()._update_service_status(self.service)
        finally:
            for p in reversed(patches):
                p.stop()

        self.assertTrue(
            self._row_exists(),
            '逸出時 savepoint 必須已經把刪除回捲掉')

    # ================================================================
    #  model 層同形處（cron 路徑）——今天不可達，守的是結構
    # ================================================================
    def test_model_poll_teardown_failure_leaves_no_half_deleted_service(self):
        """`_poll_and_update_status` 的 404 分支與 controller 完全同形。

        **今天打不到**：唯一的生產呼叫端 `_cron_check_deploying_services` 的
        domain 是 `('deploying','upgrading','initializing')`，不含 `deleting`，
        所以 `self.state == 'deleting'` 在被呼叫到時恆為 False。這條測試直接
        呼叫該 method 來守住那一行的結構——domain 一旦補上 `deleting`
        （controller 側就是這樣列的），殘留立刻可達。
        """
        client = self._operator_404()
        with patch(f'{_SVC}.get_paas_operator_client', return_value=None), \
                self._fail_settlement():
            with self.assertRaises(UserError):
                self.service._poll_and_update_status(client)

        self.assertTrue(
            self._row_exists(),
            'model 層同樣不得留下刪一半的服務')

    def test_cron_domain_still_excludes_deleting(self):
        """上一條的可達性論述綁在 cron 的 domain 上，把它釘成斷言。

        domain 一旦納入 `deleting`，上一條的「今天不可達」就過期了——那正是
        savepoint 從結構防護變成擋住線上事故的時刻，測試要在那一刻說話。
        """
        import inspect

        from odoo.addons.woow_paas_platform.models.cloud_service import CloudService

        src = inspect.getsource(CloudService._cron_check_deploying_services)
        self.assertNotIn(
            "'deleting'", src,
            'cron domain 已納入 deleting ⇒ model 層 404-unlink 分支變成可達，'
            '請把 test_model_poll_teardown_failure... 的「今天不可達」註記更新，'
            '並補一條走 `_cron_check_deploying_services` 的端到端測試')
