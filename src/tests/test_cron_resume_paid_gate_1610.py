# -*- coding: utf-8 -*-
"""#1610：每日生命週期 cron 也要「付清才復權」，並留一條帳務管理員的人工出口。

本檔釘住的契約：

* **同一個判斷**：每日 cron（``_enforce_single`` 分支 2）與 #1599 付款後即時復權的 cron 都問
  ``subscription_lifecycle_cron._paid_resume_block_reason``。訂閱 in_progress 但還有未付清的
  發票 ⇒ 不復權（#1599 端到端看到的「即時擋下、隔天每日 cron 照樣復權」不再發生）。
* **付清就復權**：錢包付款，或在 Odoo 內登記付款沖帳（線下付款）之後，每日 cron 照常復權。
* **人工出口**：訂閱上的「允許復權（已人工確認付款）」——只有帳務管理員
  （``account.group_account_manager``）設得了；勾了就不看發票照樣復權；異動有 tracking、勾選
  當下另記一則列出未付清發票的 chatter；Cloud Service 立即排付款後復權的 cron；訂閱離開
  in_progress 自動清掉。
* **不誤擋**：試用中、0 元方案（草稿或已過帳的 0 元發票）、沒有發票的訂閱照舊復權。
* **看得到原因**：擋下時寫 log ``[SUB_CRON] resume held`` 與服務的 ``resume_hold_reason``；
  復權或訂閱不再是 in_progress 時清掉。
* **停權那一側不變**：未付清但 in_progress 的服務不會因此被停權；取消的訂閱照舊停權。
* 三種服務都適用（Cloud Service／Security Access／Smart Home 共用每日 cron 的分支 2）。

⚠ 例外之後的 DB 斷言一律用裸 try/except，不用 ``assertRaises`` 區塊
（docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md）。
"""
import logging
from datetime import date, timedelta
from unittest.mock import call, patch

from odoo.exceptions import AccessError
from odoo.tests.common import tagged

from ..models.cloud_service import _internal_provision
from ..models.security_access import _internal_identity as _sa_internal
from .smart_home_fixture import open_gates, sh_model
from .test_paid_resume_1599 import (
    _CLIENT_SEAM,
    _PaidResumeBase,
    _client,
    _resume_calls,
)

_LOGGER = 'odoo.addons.woow_paas_platform.models.subscription_lifecycle_cron'
_FLAG = 'woow_resume_payment_confirmed'


class _GateBase(_PaidResumeBase):

    def setUp(self):
        super().setUp()
        self.company.sh_invoice_generated = 'post'  # 錢包不足時上游只過帳、不付款

    # -- fixtures ---------------------------------------------------------- #

    def _reopened_with_unpaid_draft(self, tag='a'):
        """欠費停權後人工把訂閱改回 in_progress，dunning 留下的未付草稿還在。"""
        sub, service = self._halted_with_cancelled_sub(tag)
        invoice = self._invoice(sub)
        sub.write({'state': 'in_progress'})
        return sub, service, invoice

    def _daily(self, entity, client=None):
        """每日 cron 對這一台的那一段（``_cron_enforce_subscription_lifecycle`` 逐台呼叫它）。"""
        client = client or _client()
        with patch(_CLIENT_SEAM, return_value=client):
            self.Lifecycle._enforce_single(entity, 7, 6, date.today())
        return client

    def _user(self, login, *group_xmlids):
        groups = ['base.group_user', 'sh_subscription.group_manager_sh_subscription']
        groups += list(group_xmlids)
        return self.env['res.users'].create({
            'login': login, 'name': login, 'email': f'{login}@example.invalid',
            'groups_id': [(6, 0, [self.env.ref(g).id for g in groups])]})

    def _billing_admin(self):
        return self._user('billing_admin_1610', 'account.group_account_manager')

    def _register_payment(self, invoice, amount):
        """在 Odoo 內登記一筆付款並沖帳（線下付款入帳的形狀，不經錢包）。"""
        if invoice.state == 'draft':
            invoice.action_post()
        journal = self.company.sh_paid_subscription_journal or self.bank_journal
        method_line = journal.inbound_payment_method_line_ids[:1]
        payment = self.env['account.payment'].sudo().create({
            'journal_id': journal.id,
            'amount': amount,
            'payment_type': 'inbound',
            'payment_method_line_id': method_line.id if method_line else False,
            'partner_type': 'customer',
            'partner_id': self.partner.id,
        })
        payment.action_post()
        lines = (invoice.line_ids + payment.move_id.line_ids).filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
        lines.reconcile()
        invoice.invalidate_recordset(['payment_state', 'amount_residual'])

    def _assert_daily_resumed(self, service, client, subdomain):
        """每日 cron 復權了：同 ``_assert_resumed``，但不看 ``resume_requested``。

        訂閱回到 in_progress／付款入帳時 #1599 會標記 ``resume_requested``；每日 cron 先
        復權的話，這個旗標留給付款後復權的 cron 收掉（它會判成 ``not_halted``）。
        """
        self.assertEqual(service.state, 'running')
        self.assertFalse(service.pre_suspend_state)
        self.assertEqual(_resume_calls(client), [
            call.scale_release(service.helm_release_name, 'paas-ws-1599test', replicas=1),
            call.unpark_release_route(
                'paas-ws-1599test', service.helm_release_name,
                {'enabled': True, 'subdomain': subdomain}),
        ])

    def _messages(self, sub, needle):
        sub.invalidate_recordset(['message_ids'])
        return sub.message_ids.filtered(lambda m: needle in (m.body or ''))



@tagged('post_install', '-at_install')
class TestDailyCronPaidGate1610(_GateBase):
    """每日 cron：訂閱 in_progress 但未付清 ⇒ 不復權；付清（錢包或 Odoo 內沖帳）⇒ 復權。"""

    def test_unpaid_draft_holds_the_daily_resume(self):
        sub, service, invoice = self._reopened_with_unpaid_draft()
        with self.assertLogs(_LOGGER, level=logging.INFO) as logs:
            client = self._daily(service)
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')
        self.assertTrue(service._is_halted())
        self.assertTrue(any('[SUB_CRON] resume held' in line and 'unpaid_invoice' in line
                            for line in logs.output), logs.output)
        self.assertIn(sub.display_name, service.resume_hold_reason)
        self.assertIn(invoice.display_name, service.resume_hold_reason)

    def test_full_daily_cron_holds_too(self):
        """整支每日 cron（不是只有 ``_enforce_single``）也擋得住。"""
        _sub, service, _invoice = self._reopened_with_unpaid_draft()
        client = _client()
        with patch(_CLIENT_SEAM, return_value=client):
            self.Lifecycle._cron_enforce_subscription_lifecycle()
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')
        self.assertTrue(service.resume_hold_reason)

    def test_renewal_confirmed_without_funds_is_held_by_the_daily_cron(self):
        """#1599 端到端看到的那一格：續約確認、發票只過帳沒付 ⇒ 每日 cron 也不復權。"""
        old, service = self._halted_with_cancelled_sub()
        new = old._sh_renew_subscription()
        new.sh_subscription_confirm()
        invoice = self.env['account.move'].sudo().search(
            [('sh_subscription_id', '=', new.id)], limit=1)
        self.assertEqual(invoice.state, 'posted')
        self.assertNotIn(invoice.payment_state, ('paid', 'in_payment'))

        client = self._daily(service)
        self.assertEqual(service.subscription_id, new, '續約鏈照樣改綁')
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')
        self.assertIn(new.display_name, service.resume_hold_reason)

    def test_wallet_payment_lets_the_daily_cron_resume_and_clears_the_note(self):
        sub, service, invoice = self._reopened_with_unpaid_draft()
        self._daily(service)
        self.assertTrue(service.resume_hold_reason)
        self._credit(300.0)
        invoice._pay_from_wallet()
        self.assertEqual(invoice.payment_state, 'paid')
        client = self._daily(service)
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')
        self.assertFalse(service.resume_hold_reason)

    def test_payment_registered_in_odoo_lets_the_daily_cron_resume(self):
        """線下付款在 Odoo 內登記並沖帳（不經錢包、不觸發即時復權）⇒ 每日 cron 復權。"""
        sub, service, invoice = self._reopened_with_unpaid_draft()
        self._register_payment(invoice, invoice.amount_total)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        client = self._daily(service)
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')

    def test_partial_payment_is_still_held(self):
        sub, service, invoice = self._reopened_with_unpaid_draft()
        self._register_payment(invoice, 100.0)
        self.assertEqual(invoice.payment_state, 'partial', '陽性對照：真的是部分付款')
        client = self._daily(service)
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')

    def test_daily_cron_asks_the_shared_gate(self):
        """結構：每日 cron 用的就是 ``_paid_resume_block_reason``，不是另一份判斷。"""
        _sub, service, _invoice = self._reopened_with_unpaid_draft()
        Lifecycle = type(self.Lifecycle)
        with patch.object(Lifecycle, '_paid_resume_block_reason', return_value=None) as gate:
            client = self._daily(service)
        gate.assert_called()
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')

    def test_hold_note_is_stable_and_follows_the_invoices(self):
        sub, service, invoice = self._reopened_with_unpaid_draft()
        self._daily(service)
        first = service.resume_hold_reason
        self._daily(service)
        self.assertEqual(service.resume_hold_reason, first)
        second = self._invoice(sub, amount=50.0)
        self._daily(service)
        self.assertIn(second.display_name, service.resume_hold_reason)

    def test_note_is_cleared_when_the_subscription_is_no_longer_active(self):
        sub, service, _invoice = self._reopened_with_unpaid_draft()
        self._daily(service)
        self.assertTrue(service.resume_hold_reason)
        sub.write({'state': 'cancel'})
        self._daily(service)
        self.assertFalse(service.resume_hold_reason)
        self.assertEqual(service.state, 'suspended')

    def test_paid_resume_cron_writes_the_same_note(self):
        """#1599 那條路擋下時也留同一段說明（同一個 helper）。"""
        _sub, service, invoice = self._reopened_with_unpaid_draft()
        self.assertTrue(service.resume_requested)
        client = self._run_cron()
        self.assertEqual(_resume_calls(client), [])
        self.assertIn(invoice.display_name, service.resume_hold_reason)

    def test_tenant_cannot_write_the_hold_note(self):
        """說明只有平台寫得進去（``groups='base.group_system'``）。"""
        tenant = self.env['res.users'].create({
            'login': 'tenant_1610', 'name': 'Tenant 1610',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1610 tenant', 'owner_id': tenant.id})
        service = _internal_provision(self.Service).create({
            'name': 'Svc 1610 tenant', 'workspace_id': workspace.id,
            'template_id': self.template.id, 'state': 'suspended',
            'pre_suspend_state': 'running'})
        as_tenant = service.with_user(tenant)
        as_tenant.write({'name': 'Svc 1610 tenant renamed'})  # 陽性對照
        denied = False
        try:
            as_tenant.write({'resume_hold_reason': 'forged'})
        except AccessError:
            denied = True
        self.assertTrue(denied)
        self.assertFalse(service.resume_hold_reason)


@tagged('post_install', '-at_install')
class TestDailyCronNotOverBlocked1610(_GateBase):
    """試用中、0 元方案、沒有發票：照舊復權。"""

    def test_subscription_without_invoices_resumes(self):
        sub, service = self._halted_with_cancelled_sub()
        sub.write({'state': 'in_progress'})
        client = self._daily(service)
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')
        self.assertFalse(service.resume_hold_reason)

    def test_trial_subscription_resumes(self):
        today = date.today()
        sub = self._new_sub(
            sh_trial=True, sh_trial_duration=7, sh_trial_unit='day',
            sh_trial_end_date=today + timedelta(days=6),
            sh_start_date=today + timedelta(days=7),
            sh_date_of_next_payment=today + timedelta(days=7))
        service = self._suspend(self._service(sub))
        self.assertIsNone(self.Lifecycle._paid_resume_block_reason(sub))
        client = self._daily(service)
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')

    def test_zero_price_draft_invoice_does_not_block(self):
        sub, service = self._halted_with_cancelled_sub()
        self._invoice(sub, amount=0.0)
        sub.write({'state': 'in_progress'})
        client = self._daily(service)
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')

    def test_zero_price_posted_invoice_does_not_block(self):
        sub, service = self._halted_with_cancelled_sub()
        invoice = self._invoice(sub, amount=0.0)
        invoice.action_post()
        self.assertEqual(invoice.state, 'posted')
        sub.write({'state': 'in_progress'})
        self.assertIsNone(self.Lifecycle._paid_resume_block_reason(sub),
                          'payment_state=%s' % invoice.payment_state)
        client = self._daily(service)
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')

    def test_cancelled_and_paid_invoices_do_not_block(self):
        sub, service = self._halted_with_cancelled_sub()
        cancelled = self._invoice(sub)
        cancelled.button_cancel()
        self.assertEqual(cancelled.state, 'cancel')
        paid = self._invoice(sub)
        self._credit(300.0)
        paid._pay_from_wallet()
        sub.write({'state': 'in_progress'})
        client = self._daily(service)
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')


@tagged('post_install', '-at_install')
class TestManualResumeConfirmation1610(_GateBase):
    """人工出口：帳務管理員勾「允許復權（已人工確認付款）」。"""

    def test_billing_admin_confirmation_resumes_right_away(self):
        sub, service, invoice = self._reopened_with_unpaid_draft()
        self._run_cron()  # 付款後即時復權：未付清，擋下
        self.assertEqual(service.state, 'suspended')

        admin = self._billing_admin()
        with patch(_CLIENT_SEAM) as seam:
            sub.with_user(admin).write({_FLAG: True})
        self.assertEqual(seam.call_count, 0, '勾選的交易內不得呼叫 operator')
        self.assertTrue(sub[_FLAG])
        self.assertTrue(service.resume_requested, 'Cloud Service 立即排付款後復權的 cron')

        note = self._messages(sub, 'Payment confirmed manually')
        self.assertEqual(len(note), 1)
        self.assertIn(invoice.display_name, note.body)
        self.assertEqual(note.author_id, admin.partner_id)

        client = self._run_cron()
        self._assert_resumed(service, client, 'paas-cs-1599-a')
        self.assertFalse(service.resume_hold_reason)
        resumed = self._messages(sub, 'was resumed (now running)')
        self.assertEqual(len(resumed), 1)
        self.assertIn(invoice.display_name, resumed.body)

    def test_confirmation_is_tracked(self):
        sub, _service, _invoice = self._reopened_with_unpaid_draft()
        admin = self._billing_admin()
        sub.with_user(admin).write({_FLAG: True})
        self.env.flush_all()
        self.env.cr.flush()  # 跑 precommit：mail tracking 在這裡落地
        tracked = self.env['mail.tracking.value'].sudo().search([
            ('field_id.name', '=', _FLAG),
            ('mail_message_id.model', '=', 'sh.subscription.subscription'),
            ('mail_message_id.res_id', '=', sub.id),
        ])
        self.assertTrue(tracked)
        self.assertEqual(tracked[:1].mail_message_id.author_id, admin.partner_id)

    def test_daily_cron_honours_the_confirmation(self):
        sub, service, _invoice = self._reopened_with_unpaid_draft()
        self._daily(service)
        self.assertTrue(service.resume_hold_reason)
        sub.with_user(self._billing_admin()).write({_FLAG: True})
        client = self._daily(service)
        self._assert_daily_resumed(service, client, 'paas-cs-1599-a')
        self.assertFalse(service.resume_hold_reason)

    def test_confirmation_cannot_bypass_an_inactive_subscription(self):
        sub, service = self._halted_with_cancelled_sub()
        sub.with_user(self._billing_admin()).write({_FLAG: True})
        self.assertEqual(self.Lifecycle._paid_resume_block_reason(sub),
                         'subscription_not_active')
        self.assertFalse(service.resume_requested)
        client = self._daily(service)
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')

    def test_confirmation_is_cleared_when_the_subscription_leaves_in_progress(self):
        sub, service, invoice = self._reopened_with_unpaid_draft()
        sub.with_user(self._billing_admin()).write({_FLAG: True})
        sub.write({'state': 'cancel'})
        self.assertFalse(sub[_FLAG])
        self.assertEqual(len(self._messages(sub, 'was cleared because the subscription')), 1)

        sub.write({'state': 'in_progress'})  # 之後再被改回 in_progress、仍未付
        client = self._daily(service)
        self.assertEqual(_resume_calls(client), [], '要重新確認，不是永久免付款')
        self.assertEqual(service.state, 'suspended')

    def test_non_billing_admin_cannot_set_it(self):
        sub, service, _invoice = self._reopened_with_unpaid_draft()
        for user in (self._user('sub_manager_1610'),
                     self._user('billing_user_1610', 'account.group_account_invoice')):
            as_user = sub.with_user(user)
            as_user.write({'sh_reason': 'checked by %s' % user.login})  # 陽性對照：寫得動訂閱
            denied = False
            try:
                as_user.write({_FLAG: True})
            except AccessError:
                denied = True
            self.assertTrue(denied, user.login)
        self.assertFalse(sub[_FLAG])
        client = self._daily(service)
        self.assertEqual(_resume_calls(client), [])

    def test_billing_admin_can_clear_it(self):
        sub, _service, _invoice = self._reopened_with_unpaid_draft()
        admin = self._billing_admin()
        sub.with_user(admin).write({_FLAG: True})
        sub.with_user(admin).write({_FLAG: False})
        self.assertFalse(sub[_FLAG])
        self.assertEqual(self.Lifecycle._paid_resume_block_reason(sub), 'unpaid_invoice')

    def test_renewal_successor_confirmation_resumes_through_the_chain(self):
        old, service = self._halted_with_cancelled_sub()
        new = old._sh_renew_subscription()
        new.sh_subscription_confirm()  # 錢包不足：發票過帳未付
        self._run_cron()
        self.assertEqual(service.state, 'suspended')
        new.with_user(self._billing_admin()).write({_FLAG: True})
        self.assertTrue(service.resume_requested)
        client = self._run_cron()
        self.assertEqual(service.subscription_id, new)
        self._assert_resumed(service, client, 'paas-cs-1599-a')


@tagged('post_install', '-at_install')
class TestSuspendSideUnchanged1610(_GateBase):
    """停權那一側不變：未付清的 in_progress 不會被停權；取消照舊停權。"""

    def test_running_service_with_unpaid_in_progress_subscription_is_not_suspended(self):
        sub = self._new_sub()
        service = self._service(sub)
        self._invoice(sub)
        Service = type(service)
        with patch.object(Service, '_do_suspend') as suspend, \
                patch.object(Service, '_do_cleanup') as cleanup:
            self._daily(service)
        suspend.assert_not_called()
        cleanup.assert_not_called()
        self.assertEqual(service.state, 'running')
        self.assertFalse(service.resume_hold_reason)

    def test_confirmation_does_not_prevent_suspension_after_cancel(self):
        sub = self._new_sub()
        service = self._service(sub)
        sub.with_user(self._billing_admin()).write({_FLAG: True})
        sub.write({'state': 'cancel'})
        with patch.object(type(service), '_do_suspend') as suspend:
            self._daily(service)
        suspend.assert_called_once()


@tagged('post_install', '-at_install')
class TestGateCoversAllEntityTypes1610(_GateBase):
    """Security Access／Smart Home 也走每日 cron 的分支 2：同一個判斷。"""

    def _smart_home(self):
        home = sh_model(self.env).create({
            'name': 'Home 1610', 'workspace_id': self.workspace.id})
        open_gates(home).write({'tunnel_id': 'tun-1610-sh'})
        return home

    def _security_access(self):
        sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'Gateway 1610', 'workspace_id': self.workspace.id})
        _sa_internal(sa).write({'state': 'active', 'tunnel_id': 'tun-1610-sa'})
        return sa

    def _halted_entity(self, entity):
        sub = self._new_sub()
        entity.write({'subscription_id': sub.id})
        entity.write({'state': 'suspended', 'pre_suspend_state': 'active'})
        return sub

    def test_unpaid_holds_and_confirmation_resumes(self):
        admin = self._billing_admin()
        for entity in (self._smart_home(), self._security_access()):
            sub = self._halted_entity(entity)
            self._invoice(sub)
            with patch.object(type(entity), '_do_resume') as resume:
                self._daily(entity)
            resume.assert_not_called()
            self.assertIn(sub.display_name, entity.resume_hold_reason)

            sub.with_user(admin).write({_FLAG: True})
            with patch.object(type(entity), '_do_resume') as resume:
                self._daily(entity)
            resume.assert_called_once()

    def test_without_invoices_they_still_resume(self):
        for entity in (self._smart_home(), self._security_access()):
            self._halted_entity(entity)
            with patch.object(type(entity), '_do_resume') as resume:
                self._daily(entity)
            resume.assert_called_once()
            self.assertFalse(entity.resume_hold_reason)
