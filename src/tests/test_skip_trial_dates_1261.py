# -*- coding: utf-8 -*-
"""#1261 (d)：skip-trial 後 `sh_trial_end_date＝昨天` 是日期慣例，不是 off-by-one。

2026-09-29 正式站 E2E：9/29 按 Skip Trial → `trial_end_date` 讀回 9/28，被記成疑似
bug。查證結論（見 `subscription_lifecycle_mixin._skip_trial` 內的 #1261 註解）：

- 本模組的慣例是 `sh_trial_end_date`＝試用**最後一天（含）**、`sh_start_date`＝
  第一個計費日＝`trial_end + 1`。建立時的 `_force_trial_dates_from_plan` 就是
  `start_date = trial_end + 1`（test_subscription_lifecycle／
  test_trial_reset_guard_367 釘著同一條不變式）。
- skip 當天就是第一個計費日（今天付今天的錢），最後一個試用日自然是昨天；寫成
  今天會讓今天同時算試用又算計費。
- 續扣／到期／退點都不讀 `sh_trial_end_date`，前端也只在 `is_trial` 時顯示它。

所以這裡**不改行為**，而是把慣例釘成可執行的斷言：哪天有人「順手修正」成
`today`，這一檔會紅、並把人指回那段註解。

每個 test method 只放一個核心斷言（#520 教訓：AND 起來的斷言只報第一個失敗）；
`test_skip_trial_succeeds_*` 是陽性對照——fixture 若走不完 skip-trial（例如缺
journal），它先紅，下面兩條日期斷言就不會在「根本沒 skip 成功」的狀態下假綠。

fixture 鏡射 test_skip_trial_rollback_525.setUp（自建 plan／product、明寫空稅、
自帶訂閱 journal），但直接呼叫 model 的 `_skip_trial()`：要驗的是日期寫入，
不是 HTTP 端點的錯誤分類（那是 #525／#567 的範圍）。
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _internal_provision


@tagged('post_install', '-at_install')
class TestSkipTrialDateConvention(TransactionCase):
    """skip-trial 寫入的日期三元組必須符合「trial_end + 1 == start」慣例。"""

    def setUp(self):
        super().setUp()
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id

        # 訂閱發票需要 `company.sh_journal_id`（sale journal）；bare install 不保證
        # 已收斂，fixture 自帶前提（同 test_skip_trial_rollback_525.setUp）。
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 1261', 'code': 'TS1261', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(
            company.sh_journal_id,
            '本檔需要可用的訂閱 journal；沒有它 skip-trial 根本走不完')

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1261 skip dates', 'owner_id': self.admin_user.id})

        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 1261', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 300.0, 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day'})
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Product 1261', 'type': 'service', 'list_price': 300.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])]})

        # 試用中的訂閱：日期三元組照 `_force_trial_dates_from_plan` 的形狀
        # （trial_end = today + 7 - 1、start = trial_end + 1）。
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
            'sh_trial': True,
            'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
            'sh_trial_end_date': today + timedelta(days=6),
            'sh_start_date': today + timedelta(days=7),
            'sh_date_of_next_payment': today + timedelta(days=7),
            'sh_taxes_ids': [(6, 0, [])],
            'state': 'in_progress'})

        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'App 1261', 'slug': 'app-1261-skip-dates',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-1261',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False})
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'Svc 1261', 'workspace_id': self.workspace.id,
                'template_id': template.id, 'subscription_id': self.sub.id})

        self.partner.sudo().add_wallet_points(
            1000.0, description='1261 skip-trial dates test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _skip(self):
        self.service._skip_trial()
        self.sub.invalidate_recordset()
        return self.sub

    def test_skip_trial_succeeds_positive_control(self):
        """陽性對照：skip 真的走完、訂閱脫離試用——否則下面兩條是假綠。"""
        sub = self._skip()
        self.assertFalse(sub.sh_trial, 'skip-trial 後訂閱必須脫離試用')

    def test_skip_trial_records_yesterday_as_last_trial_day(self):
        """#1261：skip 當天＝第一個計費日，最後一個試用日是昨天（刻意，非 off-by-one）。"""
        sub = self._skip()
        self.assertEqual(
            sub.sh_trial_end_date, date.today() - timedelta(days=1),
            'sh_trial_end_date 是「試用最後一天（含）」；skip 當天已開始計費，'
            '所以必須是昨天。改成今天前先讀 _skip_trial 裡的 #1261 註解。')

    def test_skip_trial_keeps_start_equals_trial_end_plus_one(self):
        """與 `_force_trial_dates_from_plan` 同一條不變式：start == trial_end + 1 == today。"""
        sub = self._skip()
        self.assertEqual(
            (sub.sh_trial_end_date + timedelta(days=1), sub.sh_start_date),
            (date.today(), date.today()),
            'skip 後 sh_start_date 必須是今天，且恰為 sh_trial_end_date 的隔天'
            '——與建立時 _force_trial_dates_from_plan 的慣例一致')
