# -*- coding: utf-8 -*-
"""#1611：訂閱被取消（欠費）或到期之後，租戶在 /woow 自助續訂 Cloud Service。

本檔釘住的契約：

* **能不能續訂**（``cloud_service._renewal_status``）：訂閱是 cancel／expire／close、還沒被
  續約過、服務還沒被清除；close／expire 而這一期還沒過完的不賣（避免重複收費）；cancel 的
  保留期與每日 cron 分支 4 同一個日期（``_cancel_cleanup_date``），最後一天可續、清除日當天
  起不賣。
* **方案**：原方案（即使不在範本清單）＋規格、席數都相同、前台有上架的同範本方案。
* **續訂＝上游續約鏈**：舊訂閱轉 renewed、子訂閱 in_progress、從今天起算新週期、不給試用、
  錢包付清首期；付清才改綁。停權中的服務由 #1599 的觸發標記＋排程，**續訂交易內不呼叫
  operator**；commit 之後 cron 復權（scale 1 → unpark）。
* **邊界情況**：只有 owner／admin；重複點擊（先後兩次、同時兩次）；付款失敗（錢包不足、付款
  鏈中途出錯、錢沒有真的付清）都不留任何東西；保留期剛好到期；服務已經在 running。
* **每日 cron 與續訂不交錯**：停權／清除前先取同一把鎖，拿不到就跳過這一輪。

⚠ 例外之後的 DB 斷言一律用裸 try/except，不用 ``assertRaises`` 區塊
（docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md）。
"""
import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_USER,
)
from ..models.cloud_service_renewal import RenewalRefused
from .test_paid_resume_1599 import (
    _CLIENT_SEAM, _PaidResumeBase, _client, _resume_calls,
)

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAAS = os.path.join(_SRC, 'static', 'src', 'paas')
_CLEANUP_PARAM = 'woow_paas_platform.cleanup_after_months'


def _read(*parts):
    with open(os.path.join(_PAAS, *parts), encoding='utf-8') as handle:
        return handle.read()


class _RenewBase(_PaidResumeBase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(_CLEANUP_PARAM, '6')
        self.today = fields.Date.today()

    # -- fixtures ---------------------------------------------------------- #

    def _lapsed_running(self, state='expire', end_offset=-3, tag='g'):
        """訂閱已到期（或結束）、服務還在跑：寬限期內、每日 cron 還沒停權。"""
        sub = self._new_sub()
        service = self._service(sub, tag)
        sub.write({'state': state, 'sh_end_date': self.today + timedelta(days=end_offset)})
        return sub, service

    def _age_cancellation(self, sub, days):
        """把取消日（每日 cron 讀 ``write_date``）往前推 ``days`` 天。"""
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE sh_subscription_subscription SET write_date = %s WHERE id = %s',
            (fields.Datetime.now() - timedelta(days=days), sub.id))
        sub.invalidate_recordset(['write_date'])

    def _children(self, old):
        return self.Sub.search([('sh_subscription_id', '=', old.id)])

    def _points(self):
        self.env.invalidate_all()
        return self.partner.sudo().get_wallet_loyalty_card().points

    def _plan(self, name, price=300.0, unit='month', product=True, **extra):
        plan = self.env['sh.subscription.plan'].sudo().create({
            'name': name, 'sh_duration': 1, 'sh_unit': unit,
            'sh_plan_price': price, 'sh_no_of_billing_cycle': -1, **extra})
        if product:
            self.env['product.template'].sudo().create({
                'name': f'Product {name}', 'type': 'service', 'list_price': price,
                'sh_product_subscribe': True, 'sh_subscription_plan_id': plan.id,
                'taxes_id': [(6, 0, [])]})
        return plan

    def _renew(self, service, plan=None):
        with patch(_CLIENT_SEAM) as seam:
            result = service._renew_lapsed_subscription(plan or self.plan)
        self.assertEqual(seam.call_count, 0, '續訂交易內不得呼叫 operator')
        return result

    def _refusal(self, service, plan=None):
        """回 RenewalRefused（或其他例外）；沒有例外就 fail。裸 try/except（見檔頭）。"""
        try:
            with patch(_CLIENT_SEAM) as seam:
                service._renew_lapsed_subscription(plan or self.plan)
        except UserError as exc:
            self.assertEqual(seam.call_count, 0)
            self.env.invalidate_all()
            return exc
        self.fail('續訂應該被拒絕')

    def _assert_nothing_happened(self, old, service, points):
        old.invalidate_recordset()
        service.invalidate_recordset()
        self.assertEqual(old.state, 'cancel')
        self.assertFalse(old.sh_renewed)
        self.assertFalse(self._children(old), '不得留下子訂閱')
        self.assertEqual(service.subscription_id, old)
        self.assertFalse(service.resume_requested)
        self.assertFalse(self._triggers(), '不得排復權 cron')
        self.assertEqual(self._points(), points, '錢包不得變動')
        self.assertFalse(self.env['account.move'].sudo().search([
            ('sh_subscription_id', 'in', self._children(old).ids)]))


@tagged('post_install', '-at_install')
class TestRenewalStatus1611(_RenewBase):
    """``_renewal_status``：前端入口與兩支端點共用的判準。"""

    def test_cancelled_and_suspended_service_is_renewable(self):
        sub, service = self._halted_with_cancelled_sub()
        status = service._renewal_status()
        self.assertTrue(status['available'], status)
        self.assertIsNone(status['reason'])
        self.assertEqual(status['subscription_state'], 'cancel')
        cancel_date = sub.write_date.date()
        self.assertEqual(status['retention_until'],
                         (cancel_date + timedelta(days=180)).isoformat())

    def test_active_subscription_has_nothing_to_renew(self):
        service = self._service(self._new_sub())
        status = service._renewal_status()
        self.assertFalse(status['available'])
        self.assertEqual(status['reason'], 'subscription_active')
        self.assertFalse(status['resuming'])

    def test_expired_service_still_running_in_grace_is_renewable(self):
        _sub, service = self._lapsed_running()
        status = service._renewal_status()
        self.assertTrue(status['available'], status)
        self.assertIsNone(status['retention_until'], '到期沒有清除計時')

    def test_period_not_ended_is_not_sold_twice(self):
        for state in ('close', 'expire'):
            with self.subTest(state=state):
                _sub, service = self._lapsed_running(state=state, end_offset=5, tag=state)
                status = service._renewal_status()
                self.assertFalse(status['available'])
                self.assertEqual(status['reason'], 'period_not_ended')
                self.assertEqual(status['period_end'],
                                 (self.today + timedelta(days=5)).isoformat())

    def test_removed_and_never_deployed_services_are_not_renewable(self):
        _sub, removed = self._halted_with_cancelled_sub('rm')
        removed.write({'state': 'deleting'})
        self.assertEqual(removed._renewal_status()['reason'], 'service_removed')
        _sub, pending = self._halted_with_cancelled_sub('pd')
        pending.write({'state': 'pending', 'pre_suspend_state': False})
        self.assertEqual(pending._renewal_status()['reason'], 'service_not_deployed')

    def test_back_office_renewal_pending_confirmation_blocks_self_service(self):
        old, service = self._halted_with_cancelled_sub()
        old._sh_renew_subscription()  # 後台續約，子訂閱仍 draft
        self.assertEqual(service._renewal_status()['reason'], 'already_renewed')

    def test_retention_last_day_and_expiry_match_the_daily_cron(self):
        """保留期剛好到期：最後一天可續、清除日當天起不賣；與每日 cron 分支 4 同一個日期。"""
        sub, service = self._halted_with_cancelled_sub()
        lifecycle = self.Lifecycle
        for age, renewable, cleans in ((180, True, False), (181, False, True)):
            with self.subTest(age=age):
                self._age_cancellation(sub, age)
                status = service._renewal_status()
                self.assertIs(status['available'], renewable, status)
                if not renewable:
                    self.assertEqual(status['reason'], 'retention_expired')
                self.assertEqual(status['retention_until'],
                                 (self.today - timedelta(days=age - 180)).isoformat())
                with patch.object(type(service), '_do_cleanup') as cleanup:
                    lifecycle._enforce_single(service, 7, 6, self.today)
                self.assertEqual(cleanup.called, cleans)

    def test_cleanup_date_is_the_old_formula(self):
        """抽出來的 ``_cancel_cleanup_date`` 與原本的 ``days / 30 > months`` 逐日等價。"""
        sub = self._new_sub(state='cancel')
        cancel_date = sub.write_date.date()
        for months in (0, 1, 6):
            due = self.Lifecycle._cancel_cleanup_date(sub, months, self.today)
            for days in range(0, 30 * months + 3):
                day = cancel_date + timedelta(days=days)
                self.assertEqual(day >= due, days / 30.0 > months, (months, days))


@tagged('post_install', '-at_install')
class TestRenewalPlans1611(_RenewBase):
    """可選方案：原方案＋規格／席數相同、前台有上架的同範本方案。"""

    def test_previous_plan_and_same_spec_siblings_only(self):
        yearly = self._plan('Plan 1611 yearly', price=3000.0, unit='year')
        bigger = self._plan('Plan 1611 bigger', cs_vcpu=2)
        hidden = self._plan('Plan 1611 hidden', storefront_hidden=True)
        seats = self._plan('Plan 1611 seats', ac_max_access_seats=3)
        orphan = self._plan('Plan 1611 no product', product=False)
        self.template.write({'subscription_plan_ids': [
            (6, 0, [yearly.id, bigger.id, hidden.id, seats.id, orphan.id])]})
        _sub, service = self._halted_with_cancelled_sub()
        plans = service._renewal_plans()
        self.assertEqual(set(plans.ids), {self.plan.id, yearly.id},
                         '原方案不在範本清單也可沿用；換規格／席數、下架、沒有產品的不列')

    def test_quote_matches_the_charged_invoice(self):
        tax = self.env['account.tax'].sudo().create({
            'name': 'Tax 1611 5%', 'amount': 5.0, 'amount_type': 'percent',
            'type_tax_use': 'sale', 'price_include_override': 'tax_excluded'})
        self.product.product_tmpl_id.sudo().write({'taxes_id': [(6, 0, [tax.id])]})
        _sub, service = self._halted_with_cancelled_sub()
        quote = service._renewal_quote(self.plan)
        self.assertEqual(quote['billing_cycle_start'], self.today.isoformat())
        self.assertEqual(quote['billing_cycle_end'],
                         (self.today + relativedelta(months=1) - timedelta(days=1)).isoformat())
        self._credit(quote['total'])
        result = self._renew(service)
        self.assertEqual(result['invoice'].amount_total, quote['total'])
        self.assertEqual(result['invoice'].payment_state, 'paid')
        self.assertAlmostEqual(quote['untaxed'] + quote['tax_amount'], quote['total'])
        self.assertEqual(self._points(), 0.0, '錢包剛好夠：全部扣完')


@tagged('post_install', '-at_install')
class TestSelfRenew1611(_RenewBase):
    """續訂流程：上游續約鏈＋錢包付款＋付清才改綁＋#1599 復權。"""

    def test_renew_suspended_service_then_resume_after_commit(self):
        old, service = self._halted_with_cancelled_sub()
        stale = self._invoice(old)  # dunning 留下的未付草稿（欠費期間）
        self._credit(1000.0)
        result = self._renew(service)
        new = result['subscription']
        # 上游續約鏈
        self.assertEqual(old.state, 'renewed')
        self.assertTrue(old.sh_renewed)
        self.assertEqual(new.sh_subscription_id, old)
        self.assertEqual(new.state, 'in_progress')
        # 從今天起算新週期、不給試用、不補繳
        self.assertFalse(new.sh_trial)
        self.assertEqual(new.sh_start_date, self.today)
        self.assertEqual(new.sh_end_date,
                         self.today + relativedelta(months=1) - timedelta(days=1))
        self.assertEqual(new.sh_date_of_next_payment, self.today + relativedelta(months=1))
        self.assertEqual(stale.state, 'draft', '欠費期間的發票不動（不補繳）')
        # 付清首期
        self.assertEqual(result['invoice'].payment_state, 'paid')
        self.assertEqual(self._points(), 700.0)
        # 立即改綁；停權中的服務只排程，不在交易內呼叫 operator
        self.assertEqual(service.subscription_id, new)
        self.assertEqual(service.state, 'suspended')
        self.assertTrue(service.resume_requested)
        self.assertTrue(self._triggers())
        self.assertTrue(service._renewal_status()['resuming'])
        # commit 之後 #1599 的 cron 復權（scale 1 → unpark）
        client = self._run_cron()
        self._assert_resumed(service, client, 'paas-cs-1599-a')
        self.assertFalse(service._renewal_status()['resuming'])

    def test_renew_with_a_same_spec_yearly_plan(self):
        yearly = self._plan('Plan 1611 yearly', price=3000.0, unit='year')
        self.template.write({'subscription_plan_ids': [(6, 0, [yearly.id])]})
        _old, service = self._halted_with_cancelled_sub()
        self._credit(3000.0)
        new = self._renew(service, yearly)['subscription']
        self.assertEqual(new.sh_subscription_plan_id, yearly)
        self.assertEqual(new.sh_end_date,
                         self.today + relativedelta(years=1) - timedelta(days=1))
        self.assertEqual(self._points(), 0.0)

    def test_plan_with_other_resources_is_refused(self):
        bigger = self._plan('Plan 1611 bigger', cs_vcpu=2)
        self.template.write({'subscription_plan_ids': [(6, 0, [bigger.id])]})
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        exc = self._refusal(service, bigger)
        self.assertEqual(exc.code, 'plan_not_available')
        self._assert_nothing_happened(old, service, 1000.0)

    def test_running_service_in_grace_is_rebound_without_operator(self):
        """服務已經在 running（訂閱到期、寬限期內）：只改綁、不排復權、不呼叫 operator。"""
        old, service = self._lapsed_running()
        self._credit(1000.0)
        new = self._renew(service)['subscription']
        self.assertEqual(service.subscription_id, new)
        self.assertEqual(old.state, 'renewed')
        self.assertEqual(service.state, 'running')
        self.assertFalse(service.resume_requested)
        self.assertFalse(self._triggers())
        self.assertEqual(service._renewal_status()['reason'], 'subscription_active')
        # 之後每日 cron 看到 in_progress，不會停權
        with patch.object(type(service), '_do_suspend') as suspend:
            self.Lifecycle._enforce_single(service, 7, 6, self.today)
        suspend.assert_not_called()

    def test_insufficient_wallet_is_refused_before_any_write(self):
        old, service = self._halted_with_cancelled_sub()
        self._credit(100.0)
        exc = self._refusal(service)
        self.assertIsInstance(exc, RenewalRefused)
        self.assertEqual(exc.code, 'insufficient_balance')
        self.assertEqual(exc.data['shortage'], 200.0)
        self._assert_nothing_happened(old, service, 100.0)

    def test_payment_failure_inside_the_chain_leaves_nothing(self):
        """付款失敗：付款鏈中途出錯 ⇒ 整段回捲（舊訂閱維持取消、沒有子訂閱、錢包不變）。"""
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        Move = type(self.env['account.move'])
        with patch.object(Move, '_pay_from_wallet',
                          side_effect=UserError('Wallet deduction failed.')):
            exc = self._refusal(service)
        self.assertNotIsInstance(exc, RenewalRefused)
        self._assert_nothing_happened(old, service, 1000.0)

    def test_unsettled_payment_is_rolled_back(self):
        """錢沒有真的付清（上游只過帳不付款）⇒ 回 payment_failed，整段回捲。"""
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        with patch.object(type(self.Sub), '_handle_invoice_creation',
                          lambda sub, invoice: invoice.action_post()):
            exc = self._refusal(service)
        self.assertEqual(exc.code, 'payment_failed')
        self._assert_nothing_happened(old, service, 1000.0)

    def test_second_click_after_success_is_refused_and_not_charged(self):
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        self._renew(service)
        exc = self._refusal(service)
        self.assertEqual(exc.code, 'subscription_active')
        self.assertEqual(len(self._children(old)), 1)
        self.assertEqual(self._points(), 700.0, '只扣一次')

    def test_concurrent_click_gets_busy_and_does_nothing(self):
        """另一筆交易正拿著這台服務的列（同時兩個請求）：立刻回 busy，什麼都不做。"""
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        with patch.object(type(service), '_acquire_resume_lock', return_value=False):
            exc = self._refusal(service)
        self.assertEqual(exc.code, 'busy')
        self._assert_nothing_happened(old, service, 1000.0)

    def test_retention_expired_is_refused(self):
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        self._age_cancellation(old, 181)
        exc = self._refusal(service)
        self.assertEqual(exc.code, 'retention_expired')
        self._assert_nothing_happened(old, service, 1000.0)


@tagged('post_install', '-at_install')
class TestDailyCronRenewalLock1611(_RenewBase):
    """每日 cron 停權／清除前先取鎖：續訂交易拿著（或已 commit）就跳過這一輪。"""

    def _run_daily(self, service, locked):
        with patch.object(type(service), '_acquire_resume_lock', return_value=not locked), \
                patch.object(type(service), '_do_suspend') as suspend, \
                patch.object(type(service), '_do_cleanup') as cleanup:
            self.Lifecycle._enforce_single(service, 7, 6, self.today)
        return suspend, cleanup

    def test_expired_suspend_waits_for_the_lock(self):
        _sub, service = self._lapsed_running(end_offset=-10)
        suspend, _cleanup = self._run_daily(service, locked=True)
        suspend.assert_not_called()
        suspend, _cleanup = self._run_daily(service, locked=False)
        suspend.assert_called_once()

    def test_cancelled_suspend_and_cleanup_wait_for_the_lock(self):
        sub = self._new_sub()
        service = self._service(sub)
        sub.write({'state': 'cancel'})
        suspend, cleanup = self._run_daily(service, locked=True)
        suspend.assert_not_called()
        self._suspend(service)
        self._age_cancellation(sub, 181)
        suspend, cleanup = self._run_daily(service, locked=True)
        cleanup.assert_not_called()
        suspend, cleanup = self._run_daily(service, locked=False)
        cleanup.assert_called_once()
        suspend.assert_not_called()


@tagged('post_install', '-at_install')
class TestRenewEndpoints1611(_RenewBase):
    """端點：只有 owner／admin；refusal 帶穩定代碼；帳務頁清單；detail payload。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.company.write({'auto_invoice': 'manual'})  # #980 付款能力的前提（預設值，釘住）
        self.admin = self._member('renew-1611-admin', ROLE_ADMIN)
        self.user = self._member('renew-1611-user', ROLE_USER)
        self.guest = self._member('renew-1611-guest', ROLE_GUEST)
        self.outsider = self.env['res.users'].create({
            'name': 'renew-1611-outsider', 'login': 'renew-1611-outsider',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})

    def _member(self, login, role):
        user = self.env['res.users'].create({
            'name': login, 'login': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': role})
        return user

    def _as(self, user):
        return patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                     new=MagicMock(env=self.env(user=user)))

    def _estimate(self, user, service):
        with self._as(user):
            return self.controller.api_service_renew_estimate(service.id)

    def _renew_via(self, user, service, plan=None):
        with self._as(user), patch(_CLIENT_SEAM) as seam:
            result = self.controller.api_service_renew(service.id, plan_id=(plan or self.plan).id)
        self.assertEqual(seam.call_count, 0)
        self.env.invalidate_all()
        return result

    def test_owner_and_admin_can_renew(self):
        self._credit(1000.0)
        for user, tag, payer in ((self.owner, 'o', True), (self.admin, 'd', False)):
            with self.subTest(user=user.login):
                _old, service = self._halted_with_cancelled_sub(tag)
                estimate = self._estimate(user, service)
                self.assertTrue(estimate.get('success'), estimate)
                data = estimate['data']
                self.assertEqual([p['plan_id'] for p in data['plans']], [self.plan.id])
                self.assertTrue(data['plans'][0]['is_current'])
                self.assertEqual(data['plans'][0]['total'], 300.0)
                self.assertIs(data['payer_is_viewer'], payer, '扣的是擁有者的錢包')
                self.assertIn('payable_via_woow', data)
                result = self._renew_via(user, service)
                self.assertTrue(result.get('success'), result)
                self.assertEqual(result['data']['subscription']['state'], 'in_progress')
                self.assertTrue(result['data']['renewal']['resuming'])
                self.assertEqual(result['data']['invoice']['amount'], 300.0)
        self.assertEqual(self._points(), 400.0)

    def test_user_and_guest_are_refused(self):
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        for member in (self.user, self.guest):
            with self.subTest(login=member.login):
                for result in (self._estimate(member, service),
                               self._renew_via(member, service)):
                    self.assertFalse(result.get('success'))
                    self.assertEqual(result.get('error'), 'Permission denied')
                    self.assertEqual(result.get('code'), 'permission_denied')
        self._assert_nothing_happened(old, service, 1000.0)

    def test_outsider_and_concealed_service_are_not_found(self):
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        self.assertEqual(self._renew_via(self.outsider, service).get('error'),
                         'Service not found')
        self.template.write({'is_hidden': True, 'conceal_instances': True})
        self.assertEqual(self._renew_via(self.owner, service).get('error'),
                         'Service not found')
        self._assert_nothing_happened(old, service, 1000.0)

    def test_refusals_carry_stable_codes(self):
        active = self._service(self._new_sub(), 'act')
        result = self._estimate(self.owner, active)
        self.assertEqual(result.get('code'), 'subscription_active')
        self.assertFalse(result['data']['renewal']['available'])

        old, service = self._halted_with_cancelled_sub()
        self._credit(50.0)
        result = self._renew_via(self.owner, service)
        self.assertFalse(result.get('success'))
        self.assertEqual(result.get('code'), 'insufficient_balance')
        self.assertEqual(result['data']['shortage'], 250.0)
        self.assertTrue(result.get('error'))
        self._assert_nothing_happened(old, service, 50.0)

        bigger = self._plan('Plan 1611 bigger', cs_vcpu=2)
        self.template.write({'subscription_plan_ids': [(6, 0, [bigger.id])]})
        result = self._renew_via(self.owner, service, bigger)
        self.assertEqual(result.get('code'), 'plan_not_available')

    def test_unpayable_company_policy_is_refused_before_any_write(self):
        """#980：公司是 automatic 開票、發票沒有平台旗標 ⇒ /woow 不能付，續訂一開始就擋。"""
        old, service = self._halted_with_cancelled_sub()
        self._credit(1000.0)
        self.company.write({'auto_invoice': 'automatic'})
        estimate = self._estimate(self.owner, service)
        self.assertIs(estimate['data']['payable_via_woow'], False)
        result = self._renew_via(self.owner, service)
        self.assertFalse(result.get('success'))
        self.assertEqual(result.get('code'), 'flagless_automatic_not_payable')
        self._assert_nothing_happened(old, service, 1000.0)

    def test_renewals_list_is_for_managers_only(self):
        _old, service = self._halted_with_cancelled_sub()
        self._service(self._new_sub(), 'act')  # 訂閱有效的不列
        with self._as(self.owner):
            rows = self.controller.api_cloud_service_renewals()['data']
        self.assertEqual([r['id'] for r in rows], [service.id])
        self.assertEqual(rows[0]['workspace_id'], self.workspace.id)
        self.assertTrue(rows[0]['renewal']['available'])
        for member in (self.user, self.guest, self.outsider):
            with self.subTest(login=member.login), self._as(member):
                self.assertEqual(self.controller.api_cloud_service_renewals()['data'], [])

    def test_detail_payload_carries_renewal_for_every_member(self):
        _old, service = self._halted_with_cancelled_sub()
        for member in (self.owner, self.user):
            with self.subTest(login=member.login), self._as(member):
                result = self.controller.api_workspace_service(
                    self.workspace.id, service.id, action='get')
            self.assertTrue(result.get('success'), result)
            self.assertTrue(result['data']['renewal']['available'])
            self.assertIs(result['data']['viewer_can_manage'], member == self.owner)


@tagged('post_install', '-at_install')
class TestRenewFrontendContract1611(_RenewBase):
    """前端只讀後端欄位（renewal／viewer_can_manage／payable_via_woow），新字串都有中文。"""

    def test_service_page_entry_reads_backend_fields(self):
        js = _read('pages', 'service', 'ServiceDetailPage.js')
        xml = _read('pages', 'service', 'ServiceDetailPage.xml')
        self.assertIn('this.service?.renewal', js)
        self.assertIn('canRenew: this.canManageService', js)
        self.assertIn('t-if="renewalBanner.canRenew"', xml)
        self.assertIn('<RenewSubscriptionModal', xml)

    def test_modal_is_fail_closed(self):
        js = _read('components', 'modal', 'RenewSubscriptionModal.js')
        self.assertIn('payable_via_woow !== true', js)
        self.assertIn('payer_is_viewer === true', js)

    def test_billing_page_lists_renewals(self):
        js = _read('pages', 'billing', 'BillingOverviewPage.js')
        xml = _read('pages', 'billing', 'BillingOverviewPage.xml')
        self.assertIn('cloudService.fetchRenewals()', js)
        self.assertIn('<RenewSubscriptionModal', xml)

    def test_new_strings_are_translated(self):
        po = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        for src in (
            # python
            'This service has no subscription to renew.',
            "This service's subscription is still active; there is nothing to renew.",
            'This plan cannot be used to renew this service.',
            'This service is being processed right now. Please try again in a moment.',
            'Insufficient wallet balance. %(shortage)s more points are needed.',
            'The payment could not be completed, so nothing was renewed or charged. '
            'Please try again.',
            'The retention period of this service has ended, so it can no longer be '
            'renewed. Please create a new service.',
            'An error occurred while renewing the subscription.',
            # JS／XML
            'Renew Subscription', 'Renew & Pay', 'Renew', 'Services Awaiting Renewal',
            'Renew the subscription of %(name)s to bring the service back.',
            'Insufficient balance. You need %(amount)s more to renew.',
            'The subscription of this service was cancelled',
            'The service is being restored. This usually takes a few minutes.',
            'Renew by %(date)s to keep the service and its data.',
        ):
            with self.subTest(src=src):
                self.assertTrue(code.get(src), 'zh_TW.po 缺中文：%r' % src)
