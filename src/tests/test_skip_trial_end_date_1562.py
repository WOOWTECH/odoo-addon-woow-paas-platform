# -*- coding: utf-8 -*-
"""#1562：略過試用後 ``sh_end_date`` 要跟著重算。

2026-10-07 第二階段實測（N7）：Headscale 10-07 略過 7 天試用後，訂閱 end_date 仍是試用
推算的 11-13（next_payment 11-07）；同一天換方案建立的新訂閱則是 end 11-06／next 11-07。

``sh_end_date`` 的讀者（#1562 查證）：

* 上游 ``_sh_auto_expired_subscription``（每日 cron）：in_progress 且
  ``sh_end_date == today`` ⇒ expire／renewed；
* 平台 ``_is_in_grace_period``／``_is_past_grace_period``／生命週期 cron：expire／close
  之後以 ``today − sh_end_date`` 算寬限期；
* 上游提醒 cron、續約按鈕 cron、訂閱確認信「到期日」、帳務頁非使用中訂閱的「結束日」。

續扣（``_sh_create_auto_invoice``）與退點（``_prorata_unused_refund``）只看
``sh_date_of_next_payment``，不受影響。

修法：``_skip_trial`` 以同一個起點（今天）與週期重算，與 ``_force_active_dates_from_plan``
（換方案／直接開通）同一個慣例：``end = start + cycle − 1 天``、``next_payment = start +
cycle``。fixture 鏡射 ``test_skip_trial_dates_1261``。
"""
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _internal_provision


@tagged('post_install', '-at_install')
class TestSkipTrialEndDate1562(TransactionCase):

    def setUp(self):
        super().setUp()
        self.admin_user = self.env.ref('base.user_admin')
        self.partner = self.admin_user.partner_id
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 1562', 'code': 'TS1562', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(company.sh_journal_id, '本檔需要可用的訂閱 journal')

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1562 skip end date', 'owner_id': self.admin_user.id})
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 1562', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 300.0, 'sh_no_of_billing_cycle': -1,
            'sh_trial': True, 'sh_trial_duration': 7, 'sh_trial_unit': 'day'})
        product_tmpl = self.env['product.template'].sudo().create({
            'name': 'Product 1562', 'type': 'service', 'list_price': 300.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])]})

        today = date.today()
        start = today + timedelta(days=7)
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
            'sh_trial_end_date': start - timedelta(days=1),
            'sh_start_date': start,
            # 試用建立時的形狀（_force_trial_dates_from_plan）：end = start + 1 個月 − 1 天
            'sh_end_date': start + relativedelta(months=1) - timedelta(days=1),
            'sh_date_of_next_payment': start,
            'sh_taxes_ids': [(6, 0, [])],
            'state': 'in_progress'})

        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'App 1562', 'slug': 'app-1562-skip-end-date',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-1562',
            'helm_chart_version': '0.0.0',
            'is_active': True, 'is_hidden': False})
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'Svc 1562', 'workspace_id': self.workspace.id,
                'template_id': template.id, 'subscription_id': self.sub.id})

        self.partner.sudo().add_wallet_points(
            1000.0, description='1562 skip-trial end date test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _skip(self):
        self.service._skip_trial()
        self.sub.invalidate_recordset()
        return self.sub

    def test_positive_control(self):
        """陽性對照：skip 真的走完（否則下面的日期斷言是假綠）。"""
        self.assertFalse(self._skip().sh_trial)

    def test_end_date_is_recomputed_from_today(self):
        today = date.today()
        sub = self._skip()
        self.assertEqual(sub.sh_end_date, today + relativedelta(months=1) - timedelta(days=1))

    def test_end_date_is_the_day_before_next_payment(self):
        """與換方案／直接開通的訂閱同一個形狀：end = next_payment − 1 天。"""
        sub = self._skip()
        self.assertEqual(sub.sh_date_of_next_payment, date.today() + relativedelta(months=1))
        self.assertEqual(sub.sh_end_date, sub.sh_date_of_next_payment - timedelta(days=1))

    def test_same_shape_as_the_active_date_helper(self):
        """與 `_force_active_dates_from_plan` 對同一個 plan 算出的 start／end 完全相同。"""
        sub = self._skip()
        probe = self.env['sh.subscription.subscription'].sudo().create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 300.0, 'sh_recurrency': 1, 'sh_unit': 'month',
        })
        self.service._force_active_dates_from_plan(probe, self.plan)
        self.assertEqual((sub.sh_start_date, sub.sh_end_date),
                         (probe.sh_start_date, probe.sh_end_date))
