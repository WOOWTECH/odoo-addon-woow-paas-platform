# -*- coding: utf-8 -*-
"""2026-10-10：「試用中」要看實際試用期，不能只看上游 ``sh_trial``。

上游 ``sh_trial`` 的語意是「方案有試用期」；試用自然結束、開始計費時沒有任何地方會清它
（只有略過試用／換方案的 active 分支會清）。正式站 10-10 盤點：14 筆進行中訂閱試用已過、
其中 12 筆已扣過一期仍是 True；171 筆已取消／過期仍是 True。直接拿它當「試用中」的後果：

* 已取消的訂閱在訂閱頁「已過期」分頁顯示「試用中」；付費中的訂閱各頁也顯示試用。
* 「略過試用」只檢查旗標 ⇒ 已付過當期的訂閱還能再略過一次 ＝ 開新帳單、再扣一期。

修法：``sh.subscription.subscription._woow_in_trial()``（進行中＋sh_trial＋今天 ≤ 試用最後
一天），用在 ``_get_subscription_info``／billing 序列化的 ``is_trial`` 與兩個略過試用守門。
fixture 鏡射 ``test_skip_trial_end_date_1562``。
"""
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged
from ..models.cloud_service import _internal_provision


@tagged('post_install', '-at_install')
class TestTrialFlagStale1010(TransactionCase):

    def setUp(self):
        super().setUp()
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 1010', 'code': 'TS1010', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1010 trial flag', 'owner_id': self.admin_user.id})
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 1010', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 300.0, 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day'})
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Product 1010', 'type': 'service', 'list_price': 300.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])]})

        start = date.today() + timedelta(days=7)
        self.sub = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.partner.id,
            'sh_company_id': company.id,
            'product_id': product_tmpl.product_variant_id.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 300.0, 'sh_qty': 1,
            'sh_recurrency': 1, 'sh_unit': 'month',
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day',
            'sh_trial_end_date': start - timedelta(days=1),
            'sh_start_date': start,
            'sh_end_date': start + relativedelta(months=1) - timedelta(days=1),
            'sh_date_of_next_payment': start,
            'sh_taxes_ids': [(6, 0, [])],
            'state': 'in_progress'})

        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'App 1010', 'slug': 'app-1010-trial-flag',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-1010',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False})
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'Svc 1010', 'workspace_id': self.workspace.id,
                'template_id': template.id, 'subscription_id': self.sub.id})

        self.partner.sudo().add_wallet_points(1000.0, description='1010 trial flag test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _trial_ended_and_paid(self):
        """正式站 SUB-00005 等的形狀：試用已過、已開始計費、下次扣款在下個月，旗標仍 True。"""
        today = date.today()
        self.sub.write({
            'sh_trial_end_date': today - timedelta(days=9),
            'sh_start_date': today - timedelta(days=8),
            'sh_end_date': today - timedelta(days=8) + relativedelta(months=1) - timedelta(days=1),
            'sh_date_of_next_payment': today - timedelta(days=8) + relativedelta(months=1),
        })
        self.assertTrue(self.sub.sh_trial, 'fixture 前提：旗標仍 True')

    def _invoice_count(self):
        return self.env['account.move'].sudo().search_count([
            ('partner_id', '=', self.partner.id), ('move_type', '=', 'out_invoice')])

    # ── 判準本身 ──

    def test_in_trial_during_trial(self):
        self.assertTrue(self.sub._woow_in_trial())
        self.assertTrue(self.service._get_subscription_info()['is_trial'])

    def test_last_trial_day_still_counts(self):
        self.sub.write({'sh_trial_end_date': date.today(), 'sh_start_date': date.today() + timedelta(days=1)})
        self.assertTrue(self.sub._woow_in_trial())

    def test_not_in_trial_after_trial_end(self):
        self._trial_ended_and_paid()
        self.assertFalse(self.sub._woow_in_trial())
        self.assertFalse(self.service._get_subscription_info()['is_trial'])

    def test_cancelled_is_never_in_trial(self):
        """已取消但試用期還沒過（試用中就取消）：也不算試用中。"""
        self.sub.write({'state': 'cancel'})
        self.assertTrue(self.sub.sh_trial, 'fixture 前提：旗標仍 True')
        self.assertFalse(self.sub._woow_in_trial())

    def test_missing_trial_end_falls_back_to_start_date(self):
        self.sub.write({'sh_trial_end_date': False})
        self.assertTrue(self.sub._woow_in_trial(), 'start 在未來 ⇒ 試用中')
        self.sub.write({'sh_start_date': date.today()})
        self.assertFalse(self.sub._woow_in_trial(), 'start＝今天 ⇒ 已開始計費')

    # ── 略過試用不能對已付費訂閱再扣一期 ──

    def test_skip_trial_refused_after_trial_end(self):
        self._trial_ended_and_paid()
        before_inv = self._invoice_count()
        before_bal = self.partner.wallet_balance
        before_next = self.sub.sh_date_of_next_payment
        with self.assertRaisesRegex(UserError, 'not in trial period'):
            self.service._skip_trial()
        self.sub.invalidate_recordset()
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self._invoice_count(), before_inv, '不得開新帳單')
        self.assertEqual(self.partner.wallet_balance, before_bal, '不得扣點')
        self.assertEqual(self.sub.sh_date_of_next_payment, before_next, '計費週期不得重算')

    def test_skip_trial_estimate_refused_after_trial_end(self):
        self._trial_ended_and_paid()
        with self.assertRaisesRegex(UserError, 'not in trial period'):
            self.service._skip_trial_estimate()

    def test_positive_control_skip_still_works_during_trial(self):
        """陽性對照：真的在試用期內，略過試用照常（否則上面的拒絕是假綠）。"""
        self.service._skip_trial()
        self.sub.invalidate_recordset()
        self.assertFalse(self.sub.sh_trial)
        self.assertEqual(self.sub.sh_start_date, date.today())


@tagged('post_install', '-at_install')
class TestTrialFlagStaleBillingPayload1010(HttpCase):
    """訂閱頁（/api/billing/subscriptions）的 is_trial 同一個判準：已取消、試用已過都是 False。"""

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.partner = self.env.ref('base.partner_admin')
        admin = self.env.ref('base.user_admin')
        tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        plan = self.env.ref('woow_paas_platform.plan_n8n_monthly')
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Trial flag WS 1010', 'owner_id': admin.id})
        svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': ws.id, 'template_id': tpl.id,
            'name': 'trial-flag-n8n', 'reference_id': 'test-trial-flag-1010',
            'state': 'running', 'subdomain': 'trial-flag-1010',
            'provision_partner_id': self.partner.id})
        svc._create_and_confirm_subscription(self.partner, plan)
        self.sub = svc.subscription_id.sudo()

    def _row(self):
        body = self.url_open(
            '/api/billing/subscriptions',
            data='{"jsonrpc":"2.0","method":"call","params":{"filter":"all","limit":50}}',
            headers={'Content-Type': 'application/json'}).json()
        rows = body['result']['data']
        return next(r for r in rows if r['id'] == self.sub.id)

    def test_in_trial_then_ended_then_cancelled(self):
        self.assertTrue(self.sub.sh_trial, 'fixture 前提：方案有試用、建立時旗標 True')
        self.assertTrue(self._row()['is_trial'], '剛開通、試用期內')
        today = date.today()
        self.sub.write({'sh_trial_end_date': today - timedelta(days=1), 'sh_start_date': today})
        self.assertFalse(self._row()['is_trial'], '試用已過（旗標仍 True）')
        self.sub.write({'sh_trial_end_date': today + timedelta(days=3),
                        'sh_start_date': today + timedelta(days=4), 'state': 'cancel'})
        self.assertFalse(self._row()['is_trial'], '已取消')
