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

## 當時（#525）的實際症狀與可達性

#525 時 404-unlink 分支只有 `_list_services`（輪詢範圍含 `deleting`、上層有 catch-all）
走得到：teardown 中途失敗 → catch-all 自己 log `svc.name` 撞 `MissingError` → 整筆交易
被框架 rollback、**整支清單端點 500**。當時用 HttpCase 走真端點守那條路徑。

## #1159 之後

list/get 純讀（#1159 Q1），`_list_services` 不再 poll、不再收屍；controller 那份 poll
已刪除。deleting 的收尾改由背景短 poll（`_cron_check_deploying_services`，domain 納入
`deleting`）做——#525 當時的 tripwire（「cron domain 不含 deleting」）按它自己的指示
換成下面走 cron 的端到端測試。

「沒有殘留」今天由三層回捲單位疊著守（`unlink()` 自己的 #794 savepoint、poll 404 分支
的 savepoint、cron 迴圈每台服務的 commit／rollback），細節與 mutation 結果見
`TestCronReapLeavesNoHalfDeletedService` 的 docstring。#525 記的「直呼路徑下刪除仍被
回捲、原因未查明」，原因就是 #794 的那一層。

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

- 陽性對照：沒注入時 cron 真的會收屍、訂閱一併結算——少了它，fixture 一旦走不到
  `unlink()`，「沒有殘留」就變成「根本沒發生過刪除」。
- 注入當下服務列**已經**從 DB 消失（＝`super().unlink()` 已落地）——少了它，注入點一
  被重排到 `super().unlink()` 之前，「沒有殘留」同樣退化成恆真。

探針一律用 raw SQL 打 `woow_paas_platform_cloud_service`，不經 ORM cache。
"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID, api
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.sh_subscription_fix import (
    ShSubscriptionFix,
)
from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorError,
)
from ..models.cloud_service import _internal_provision

_CTRL = 'odoo.addons.woow_paas_platform.controllers.paas'
_SVC = 'odoo.addons.woow_paas_platform.services.paas_operator'


@tagged('post_install', '-at_install')
class TestCronReapLeavesNoHalfDeletedService(TransactionCase):
    """#1159 Q1：deleting 的收尾由背景短 poll（``_cron_check_deploying_services``）做。

    #1159 以前這條分支不可達（cron 的 domain 不含 deleting，本檔原本只留一條 tripwire
    釘住它）；#1159 把 deleting 納入 domain、並讓 list/get 純讀之後，這裡是**唯一**的
    收尾路徑。

    「沒有殘留」今天由**三層**回捲單位疊著守，最內層就足以單獨成立：

    1. ``unlink()`` 自己的 savepoint（#794 加的，涵蓋 ``super().unlink()``＋訂閱結算）。
       本檔前面「直呼路徑下刪除仍被回捲、原因未查明」的原因就是它。
    2. ``_poll_and_update_status`` 404 分支的 savepoint（#525 B5）。
    3. cron 迴圈的「每台服務 commit／rollback」（#1159 Q36）。

    #1159 實測（mutation）：拿掉 2、把 3 改成例外時照樣 commit、兩者同時做，本檔都仍綠
    ⇒ 本檔守的是**端到端結果**（有沒有殘留、服務是否留在 deleting 等下一輪），不是
    其中任何一層；要讓它轉紅得連 1 一起拿掉。

    cron 會 commit ⇒ 真 cursor＋真 commit＋事後清理（樣板 ``test_billing_incident_864``）。
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(self._purge)
        self.ids_ = {}
        with self.registry.cursor() as cr:
            env = self._env(cr)
            admin = env.ref('base.user_admin')
            company = env.company.sudo()
            workspace = env['woow_paas_platform.workspace'].create({
                'name': 'WS 525 cron', 'owner_id': admin.id})
            plan = env['sh.subscription.plan'].sudo().create({
                'name': 'Plan 525 cron', 'sh_duration': 1, 'sh_unit': 'month',
                'sh_plan_price': 300.0, 'sh_no_of_billing_cycle': -1})
            product_tmpl = env['product.template'].sudo().create({
                'name': 'Product 525 cron', 'type': 'service', 'list_price': 300.0,
                'sh_product_subscribe': True, 'sh_subscription_plan_id': plan.id,
                'taxes_id': [(6, 0, [])]})
            today = date.today()
            sub = env['sh.subscription.subscription'].sudo().create({
                'sh_partner_id': admin.partner_id.id,
                'sh_company_id': company.id,
                'product_id': product_tmpl.product_variant_id.id,
                'sh_subscription_plan_id': plan.id,
                'sh_plan_price': 300.0, 'sh_qty': 1, 'sh_recurrency': 1,
                'sh_unit': 'month',
                'sh_start_date': today - timedelta(days=10),
                'sh_date_of_next_payment': today + timedelta(days=20),
                'sh_taxes_ids': [(6, 0, [])],
                'state': 'in_progress'})
            template = env['woow_paas_platform.cloud_app_template'].sudo().create({
                'name': 'App 525 cron', 'slug': 'app-525-cron',
                'helm_repo_url': '',
                'helm_chart_name': 'oci://example.invalid/app-525-cron',
                'helm_chart_version': '0.0.0',
                'post_deploy_init_type': 'none',
                'is_active': True, 'is_hidden': False})
            service = _internal_provision(env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'Svc 525 cron',
                'workspace_id': workspace.id,
                'template_id': template.id,
                'subscription_id': sub.id,
                'state': 'deleting',
                'helm_namespace': 'paas-ws-525cron',
                'helm_release_name': 'svc-525-cron'})
            self.ids_ = {
                'workspace': workspace.id, 'plan': plan.id,
                'product_tmpl': product_tmpl.id, 'sub': sub.id,
                'template': template.id, 'service': service.id,
            }

    def _env(self, cr):
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, SUPERUSER_ID, {})

    def _purge(self):
        ids = self.ids_
        if not ids:
            return
        with self.registry.cursor() as cr:
            cr.execute('DELETE FROM woow_paas_platform_cloud_service WHERE id = %s',
                       (ids['service'],))
            cr.execute('DELETE FROM woow_paas_platform_cloud_app_template WHERE id = %s',
                       (ids['template'],))
            for model, res_id in (('sh.subscription.subscription', ids['sub']),
                                  ('woow_paas_platform.workspace', ids['workspace'])):
                for table, column in (('mail_followers', 'res_model'),
                                      ('mail_message', 'model')):
                    cr.execute(f'DELETE FROM {table} WHERE {column} = %s AND res_id = %s',
                               (model, res_id))
            cr.execute('DELETE FROM sh_subscription_subscription WHERE id = %s', (ids['sub'],))
            cr.execute('DELETE FROM product_template WHERE id = %s', (ids['product_tmpl'],))
            cr.execute('DELETE FROM sh_subscription_plan WHERE id = %s', (ids['plan'],))
            cr.execute('DELETE FROM woow_paas_platform_workspace_access WHERE workspace_id = %s',
                       (ids['workspace'],))
            cr.execute('DELETE FROM woow_paas_platform_workspace WHERE id = %s',
                       (ids['workspace'],))
            # #1159 Q38：cron 結尾的監控在真 cursor 下會留下 activity／通知訊息。
            for table, column in (('mail_activity', 'res_model'), ('mail_message', 'model'),
                                  ('mail_followers', 'res_model')):
                cr.execute(f"DELETE FROM {table} WHERE {column} = "
                           "'woow_paas_platform.platform_monitor'")
            cr.commit()

    def _probe(self):
        with self.registry.cursor() as cr:
            cr.execute('SELECT state FROM woow_paas_platform_cloud_service WHERE id = %s',
                       (self.ids_['service'],))
            row = cr.fetchone()
            cr.execute('SELECT state FROM sh_subscription_subscription WHERE id = %s',
                       (self.ids_['sub'],))
            return (row[0] if row else None), cr.fetchone()[0]

    def _run_cron(self, injection=None):
        client = MagicMock()
        client.get_status.side_effect = PaaSOperatorError(
            'release not found', status_code=404)
        patches = [patch(f'{_SVC}.get_paas_operator_client', return_value=client)]
        if injection is not None:
            patches.append(injection)
        for p in patches:
            p.start()
        try:
            with self.registry.cursor() as cr:
                model = self._env(cr)['woow_paas_platform.cloud_service']
                model._cron_check_deploying_services()
        finally:
            for p in reversed(patches):
                p.stop()

    def _fail_settlement(self, recorder):
        service_id = self.ids_['service']

        def boom(sub_self, reason='Service deleted'):
            sub_self.env.cr.execute(
                'SELECT 1 FROM woow_paas_platform_cloud_service WHERE id = %s', (service_id,))
            recorder.append(bool(sub_self.env.cr.fetchone()))
            raise UserError('injected: subscription settlement failed')

        return patch.object(ShSubscriptionFix, '_settle_on_entity_delete', boom)

    def test_cron_reaps_a_deleting_service_when_the_release_is_gone(self):
        """陽性對照：沒注入時 cron 真的會收屍（否則下面「沒有殘留」零鑑別力）。"""
        self._run_cron()
        service_state, sub_state = self._probe()
        self.assertIsNone(service_state, 'operator 回 404 的 deleting 服務要被 unlink')
        self.assertNotEqual(sub_state, 'in_progress', '收屍要一併結算訂閱（#318）')

    def test_teardown_failure_leaves_no_half_deleted_service(self):
        seen = []
        self._run_cron(injection=self._fail_settlement(seen))
        self.assertEqual(seen, [False],
                         '前提：注入觸發時服務列已被 super().unlink() 刪掉（實際 %r）' % (seen,))
        service_state, sub_state = self._probe()
        self.assertEqual(
            (service_state, sub_state), ('deleting', 'in_progress'),
            'teardown 中途失敗：已刪掉的那一半必須回捲、服務留在 deleting 等下一輪；'
            '服務消失而訂閱仍 in_progress ＝ 孤兒訂閱（#318）')
