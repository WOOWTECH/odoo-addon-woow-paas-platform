# -*- coding: utf-8 -*-
"""#1599：續訂付款成功後立即復權被欠費停權的 Cloud Service（不必等每日生命週期 cron）。

本檔釘住的契約：

* **觸發點**：訂閱發票付清（``account.move._pay_from_wallet``——每日扣款、/woow 付發票、
  略過試用、確認訂閱開票都經過它）與訂閱回到 in_progress（``sh.subscription.subscription
  .write``——後台確認續約子訂閱、人工改回）。兩者都只寫 ``cloud_service.resume_requested``
  並 ``_trigger()`` 付款後復權的 cron；**付款交易內不呼叫 operator**。
* **commit 之後才復權**：旗標與 ``ir.cron.trigger`` 都是 DB 列——付款回捲（含只回捲包著它
  的 savepoint）時一起消失；commit 之後由 cron worker 執行，與每日 cron 走同一段
  ``_enforce_single``（``resume_only=True``）。
* **不能復權的情形**：付款失敗、部分付款、還有未付清的發票、訂閱（沿續約鏈）不是
  in_progress。``resume_only`` 保證這條路絕不停權、絕不清除。
* **冪等與並發**：重複請求只復權一次；鎖被另一筆交易拿著時（每日 cron 正在處理），
  兩邊都跳過、不重複 scale，旗標留著稍後再試。另有一組用真的第二條 DB 連線驗鎖。
* **失敗語意不變**：operator／unpark 失敗寫 error、保留停權旗標，由每日 cron 重試；付款不受
  影響。
* **範圍**：只做 Cloud Service；Smart Home／Security Access 不會被標記，每日 cron 對它們
  也不取鎖（行為不變）。

⚠ 例外之後的 DB 斷言一律用裸 try/except，不用 ``assertRaises`` 區塊
（docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md）。
"""
from datetime import date, timedelta
from unittest.mock import MagicMock, call, patch
from .publication_receipt_fixture import install_publication_receipts

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..models.security_access import _internal_identity as _sa_internal
from ..services.paas_operator import PaaSOperatorAPIError
from .committed_cron_scope import CommittedCronScope
from .smart_home_fixture import open_gates, sh_model

_CLIENT_SEAM = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
_CRON_XMLID = 'woow_paas_platform.ir_cron_resume_paid_cloud_services'
_SERVICE = 'woow_paas_platform.cloud_service'
_LIFECYCLE = 'woow_paas_platform.subscription_lifecycle_cron'


class _Boom(Exception):
    """模擬付款交易在旗標寫入之後才失敗。"""


def _client():
    client = install_publication_receipts(MagicMock())
    client.unpark_release_route.return_value = {'status': 'unparked'}
    client.park_release_route.return_value = {'status': 'parked'}
    return client


def _resume_calls(client):
    return [c for c in client.mock_calls
            if c[0] in ('scale_release', 'unpark_release_route')]


class _PaidResumeBase(TransactionCase):

    def setUp(self):
        super().setUp()
        # 新建的工作區擁有者：錢包從 0 開始，「餘額不足」的情境才是確定的。
        self.owner = self.env['res.users'].create({
            'login': 'owner_1599_paid_resume', 'name': 'Owner 1599',
            'email': 'owner-1599@example.invalid',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.partner = self.owner.partner_id
        self.company = self.env.company.sudo()
        Journal = self.env['account.journal'].sudo()
        if not Journal.search([('company_id', '=', self.company.id),
                               ('type', '=', 'sale')], limit=1):
            Journal.create({'name': 'Test Sales 1599', 'code': 'TS1599',
                            'type': 'sale', 'company_id': self.company.id})
        self.bank_journal = Journal.search([('company_id', '=', self.company.id),
                                            ('type', 'in', ('bank', 'cash'))], limit=1)
        if not self.bank_journal:
            self.bank_journal = Journal.create({
                'name': 'Test Bank 1599', 'code': 'TB159', 'type': 'bank',
                'company_id': self.company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(self.company.sh_journal_id, '本檔需要可用的訂閱 journal')

        self.Service = self.env[_SERVICE].sudo()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.Lifecycle = self.env[_LIFECYCLE].sudo()
        self.cron = self.env.ref(_CRON_XMLID)

        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1599 paid resume', 'owner_id': self.owner.id})
        self.plan = self.env['sh.subscription.plan'].sudo().create({
            'name': 'Plan 1599', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 300.0, 'sh_no_of_billing_cycle': -1})
        self.product = self.env['product.template'].sudo().create({
            'name': 'Product 1599', 'type': 'service', 'list_price': 300.0,
            'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id,
            'taxes_id': [(6, 0, [])]}).product_variant_id
        self.template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'App 1599', 'slug': 'app-1599-paid-resume',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.invalid/app-1599',
            'helm_chart_version': '0.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'ingress_enabled': True})

    # -- fixtures ---------------------------------------------------------- #

    def _new_sub(self, **extra):
        today = date.today()
        vals = {
            'sh_partner_id': self.partner.id,
            'sh_company_id': self.company.id,
            'product_id': self.product.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 300.0,
            'sh_qty': 1,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_start_date': today,
            'sh_end_date': today + relativedelta(months=1) - timedelta(days=1),
            'sh_date_of_next_payment': today + relativedelta(months=1),
            'sh_taxes_ids': [(6, 0, [])],
            'state': 'in_progress',
        }
        vals.update(extra)
        return self.Sub.create(vals)

    def _service(self, sub, tag='a'):
        """綁著 ``sub``（綁定當下 in_progress，#519）的對外雲端服務。"""
        return _internal_provision(self.Service).create({
            'name': f'Svc 1599 {tag}',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-1599test',
            'helm_release_name': f'svc-1599-{tag}',
            'subdomain': f'paas-cs-1599-{tag}',
            'subscription_id': sub.id,
        })

    def _suspend(self, service):
        service.write({'state': 'suspended', 'pre_suspend_state': 'running'})
        self.assertTrue(service._is_halted())
        return service

    def _halted_with_cancelled_sub(self, tag='a'):
        """欠費停權的形狀：訂閱已被 dunning 取消、服務停權中。"""
        sub = self._new_sub()
        service = self._suspend(self._service(sub, tag))
        sub.write({'state': 'cancel'})
        return sub, service

    def _invoice(self, sub, amount=300.0):
        """訂閱的週期發票（草稿），形狀同 dunning 的 ``_get_or_create_cycle_invoice``。"""
        vals = sub._generate_invoice_vals()
        vals['invoice_line_ids'][0][2]['price_unit'] = amount
        vals['woow_invoice_issue_state'] = 'not_required'
        vals['woow_skip_upstream_hook'] = True
        return self.env['account.move'].sudo().create(vals)

    def _credit(self, amount):
        self.partner.sudo().add_wallet_points(amount, description='1599 test credit')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _triggers(self):
        return self.env['ir.cron.trigger'].sudo().search([('cron_id', '=', self.cron.id)])

    def _run_cron(self, client=None):
        client = client or _client()
        with patch(_CLIENT_SEAM, return_value=client):
            self.Lifecycle._cron_resume_paid_cloud_services()
        return client

    def _assert_resumed(self, service, client, subdomain):
        self.assertEqual(service.state, 'running')
        self.assertFalse(service.pre_suspend_state)
        self.assertFalse(service.resume_requested)
        self.assertEqual(_resume_calls(client), [
            call.scale_release(service.helm_release_name, 'paas-ws-1599test', replicas=1),
            call.unpark_release_route(
                'paas-ws-1599test', service.helm_release_name,
                {'enabled': True, 'subdomain': subdomain}),
        ])


@tagged('post_install', '-at_install')
class TestPaidResumeRequest1599(_PaidResumeBase):
    """付款交易內：只寫旗標＋排程，不呼叫 operator；付款回捲就什麼都不留。"""

    def test_invoice_payment_requests_resume_without_calling_operator(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        invoice = self._invoice(sub)
        self._credit(300.0)
        triggers_before = self._triggers()

        with patch(_CLIENT_SEAM) as seam:
            invoice._pay_from_wallet()
        self.assertEqual(seam.call_count, 0, '付款交易內不得呼叫 operator')
        self.assertEqual(invoice.payment_state, 'paid')
        self.assertTrue(service.resume_requested)
        self.assertEqual(service.state, 'suspended', '復權要等 commit 之後由 cron 做')
        new_triggers = self._triggers() - triggers_before
        self.assertTrue(new_triggers, '付款交易要排一次付款後復權的 cron')
        self.assertTrue(all(t.call_at <= fields.Datetime.now() for t in new_triggers))

    def test_rolled_back_payment_leaves_no_request_and_no_trigger(self):
        """付款在旗標寫入之後才失敗（savepoint 回捲）⇒ 旗標與 trigger 一起消失。

        這正是不用 ``cr.postcommit`` 的理由：postcommit 的 callback 不認 savepoint。
        """
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        invoice = self._invoice(sub)
        self._credit(300.0)
        triggers_before = self._triggers()
        try:
            with self.env.cr.savepoint():
                invoice._pay_from_wallet()
                self.assertTrue(service.resume_requested)  # 陽性對照：真的寫過
                raise _Boom()
        except _Boom:
            pass
        service.invalidate_recordset()
        invoice.invalidate_recordset()
        self.assertFalse(service.resume_requested)
        self.assertEqual(self._triggers() - triggers_before, self.env['ir.cron.trigger'])
        self.assertNotEqual(invoice.payment_state, 'paid')

    def test_failed_wallet_payment_requests_nothing(self):
        """錢包不足（付款失敗）：不標記、不排程；cron 跑了也不復權。"""
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        invoice = self._invoice(sub, amount=500.0)
        self._credit(100.0)
        triggers_before = self._triggers()
        failed = False
        try:
            with self.env.cr.savepoint():  # 與 /api/billing/invoices/<id>/pay 同形
                invoice._pay_from_wallet()
        except UserError:
            failed = True
        self.assertTrue(failed, '陽性對照：付款真的失敗了')
        self.assertFalse(service.resume_requested)
        self.assertEqual(self._triggers() - triggers_before, self.env['ir.cron.trigger'])
        client = self._run_cron()
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')

    def test_paying_a_cancelled_subscription_requests_nothing(self):
        """付的是已取消訂閱的舊帳：訂閱沒有回到 in_progress ⇒ 不標記。"""
        sub, service = self._halted_with_cancelled_sub()
        invoice = self._invoice(sub)
        self._credit(300.0)
        invoice._pay_from_wallet()
        self.assertEqual(invoice.payment_state, 'paid')  # 陽性對照：付款成功
        self.assertFalse(service.resume_requested)

    def test_running_service_is_not_requested(self):
        sub = self._new_sub()
        service = self._service(sub)
        invoice = self._invoice(sub)
        self._credit(300.0)
        invoice._pay_from_wallet()
        self.assertFalse(service.resume_requested)

    def test_busy_row_is_skipped_without_blocking_the_payment(self):
        """另一筆交易正拿著這一列（例如每日 cron 正在處理）：付款照樣成功、不等鎖。"""
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        invoice = self._invoice(sub)
        self._credit(300.0)
        with patch.object(type(service), '_acquire_resume_lock', return_value=False):
            invoice._pay_from_wallet()
        self.assertEqual(invoice.payment_state, 'paid')
        self.assertFalse(service.resume_requested)

    def test_request_failure_never_fails_the_payment(self):
        """標記這一段若出錯，只記 log，付款照樣成功（由每日 cron 照舊復權）。"""
        sub = self._new_sub()
        self._suspend(self._service(sub))
        invoice = self._invoice(sub)
        self._credit(300.0)
        with patch.object(type(self.Lifecycle), '_mark_paid_resume',
                          side_effect=RuntimeError('bug')), \
                self.assertLogs('odoo.addons.woow_paas_platform.models.subscription_lifecycle_cron',
                                level='ERROR'):
            invoice._pay_from_wallet()
        self.assertEqual(invoice.payment_state, 'paid')

    def test_tenant_cannot_write_resume_requested(self):
        """旗標只有平台身分寫得進去（``groups='base.group_system'``）。"""
        tenant = self.env['res.users'].create({
            'login': 'tenant_1599', 'name': 'Tenant 1599',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1599 tenant', 'owner_id': tenant.id})
        service = _internal_provision(self.Service).create({
            'name': 'Svc 1599 tenant', 'workspace_id': workspace.id,
            'template_id': self.template.id, 'state': 'suspended',
            'pre_suspend_state': 'running'})
        as_tenant = service.with_user(tenant)
        as_tenant.write({'name': 'Svc 1599 tenant renamed'})  # 陽性對照：租戶寫得動這一列
        denied = False
        try:
            as_tenant.write({'resume_requested': True})
        except AccessError:
            denied = True
        self.assertTrue(denied)
        self.assertFalse(service.resume_requested)


@tagged('post_install', '-at_install')
class TestPaidResumeCron1599(_PaidResumeBase):
    """commit 之後由 cron 復權：沿用 ``_enforce_single`` 的判斷與失敗語意。"""

    def test_cron_resumes_after_wallet_payment(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        invoice = self._invoice(sub)
        self._credit(300.0)
        invoice._pay_from_wallet()
        client = self._run_cron()
        self._assert_resumed(service, client, 'paas-cs-1599-a')
        self.assertEqual(service.subscription_id, sub)

    def test_renewal_confirm_resumes_through_the_renewal_chain(self):
        """後台確認續約子訂閱（錢包足夠）⇒ 改綁新訂閱並復權。"""
        old, service = self._halted_with_cancelled_sub()
        new = old._sh_renew_subscription()
        self.assertEqual(old.state, 'renewed')
        self.assertEqual(new.state, 'draft')
        self._credit(1000.0)
        with patch(_CLIENT_SEAM) as seam:
            new.sh_subscription_confirm()
        self.assertEqual(seam.call_count, 0, '確認／付款交易內不得呼叫 operator')
        self.assertEqual(new.state, 'in_progress')
        self.assertTrue(service.resume_requested)
        client = self._run_cron()
        self.assertEqual(service.subscription_id, new)
        self._assert_resumed(service, client, 'paas-cs-1599-a')

    def test_renewal_confirmed_without_funds_waits_for_the_invoice_payment(self):
        """續約確認時錢包不足：發票只過帳沒付 ⇒ 不復權；租戶付清後才復權。"""
        self.company.sh_invoice_generated = 'post'  # 錢包不足時上游只過帳、不付款
        old, service = self._halted_with_cancelled_sub()
        new = old._sh_renew_subscription()
        new.sh_subscription_confirm()
        invoice = self.env['account.move'].sudo().search(
            [('sh_subscription_id', '=', new.id)], limit=1)
        self.assertTrue(invoice)
        self.assertNotEqual(invoice.payment_state, 'paid')
        client = self._run_cron()
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')
        self.assertFalse(service.resume_requested)

        self._credit(1000.0)
        invoice._pay_from_wallet()
        self.assertTrue(service.resume_requested)
        client = self._run_cron()
        self._assert_resumed(service, client, 'paas-cs-1599-a')

    def test_subscription_reopened_by_admin_resumes(self):
        sub, service = self._halted_with_cancelled_sub()
        sub.write({'state': 'in_progress'})
        self.assertTrue(service.resume_requested)
        client = self._run_cron()
        self._assert_resumed(service, client, 'paas-cs-1599-a')

    def test_unpaid_invoice_blocks_resume(self):
        sub, service = self._halted_with_cancelled_sub()
        self._invoice(sub)  # dunning 留下的未付草稿
        sub.write({'state': 'in_progress'})
        self.assertTrue(service.resume_requested)
        client = self._run_cron()
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')
        self.assertFalse(service.resume_requested, '處理過就清掉；付清時會再標記')

    def test_partial_payment_blocks_resume(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        invoice = self._invoice(sub)
        invoice.action_post()
        # 與 `_pay_from_wallet` 同一套收款 journal／付款方式，只是金額只付三分之一。
        journal = self.company.sh_paid_subscription_journal or self.bank_journal
        method_line = journal.inbound_payment_method_line_ids[:1]
        payment = self.env['account.payment'].sudo().create({
            'journal_id': journal.id,
            'amount': 100.0,
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
        self.assertEqual(invoice.payment_state, 'partial', '陽性對照：真的是部分付款')
        self.assertEqual(self.Lifecycle._paid_resume_block_reason(sub), 'unpaid_invoice')

        self.Lifecycle._request_paid_resume(sub, source='test')
        self.assertTrue(service.resume_requested)
        client = self._run_cron()
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')

    def test_stale_request_never_suspends_or_cleans_up(self):
        """旗標還在、訂閱卻已不是 in_progress：不呼叫 ``_enforce_single``，絕不停權／清除。"""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.cleanup_after_months', '-1')
        sub, service = self._halted_with_cancelled_sub()
        service.write({'resume_requested': True})
        Service = type(service)
        with patch.object(Service, '_do_cleanup') as cleanup, \
                patch.object(Service, '_do_suspend') as suspend:
            client = self._run_cron()
        cleanup.assert_not_called()
        suspend.assert_not_called()
        self.assertEqual(_resume_calls(client), [])
        self.assertFalse(service.resume_requested)

    def test_resume_only_never_reaches_suspend_or_cleanup(self):
        """``resume_only``：訂閱不是 in_progress 就結束（對照組：每日 cron 會停權＋清除）。"""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.cleanup_after_months', '-1')
        sub = self._new_sub()
        service = self._service(sub)
        sub.write({'state': 'cancel'})
        Service = type(service)
        with patch.object(Service, '_do_cleanup') as cleanup, \
                patch.object(Service, '_do_suspend') as suspend:
            self.Lifecycle._enforce_single(service, 7, -1, date.today(), resume_only=True)
            cleanup.assert_not_called()
            suspend.assert_not_called()
            self.Lifecycle._enforce_single(service, 7, -1, date.today())
            suspend.assert_called_once()  # 對照組：沒有 resume_only 就是每日 cron 的行為
            cleanup.assert_called_once()

    def test_duplicate_requests_resume_once(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        for _ in range(3):  # 重複的付款回呼／確認
            self.Lifecycle._request_paid_resume(sub, source='test')
        self.assertTrue(service.resume_requested)
        client = _client()
        self._run_cron(client)
        self._run_cron(client)
        self.assertEqual(client.scale_release.call_count, 1)
        self.assertEqual(client.unpark_release_route.call_count, 1)
        self.assertEqual(service.state, 'running')

    def test_busy_lock_keeps_the_request_and_retries_later(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        self.Lifecycle._request_paid_resume(sub, source='test')
        triggers_before = self._triggers()
        with patch.object(type(service), '_acquire_resume_lock', return_value=False):
            client = self._run_cron()
        self.assertEqual(_resume_calls(client), [], '另一筆交易正在處理：不得重複 scale')
        self.assertTrue(service.resume_requested, '旗標留著，稍後再試')
        retry = self._triggers() - triggers_before
        self.assertTrue(retry)
        self.assertTrue(all(t.call_at > fields.Datetime.now() for t in retry),
                        '延後重試，不要立刻重排而原地空轉')
        client = self._run_cron()
        self._assert_resumed(service, client, 'paas-cs-1599-a')

    def test_operator_failure_keeps_the_payment_and_leaves_retry_to_the_daily_cron(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        invoice = self._invoice(sub)
        self._credit(300.0)
        invoice._pay_from_wallet()
        self.partner.invalidate_recordset(['wallet_balance'])
        balance_after_payment = self.partner.wallet_balance

        broken = _client()
        broken.scale_release.side_effect = PaaSOperatorAPIError('boom', status_code=500)
        self._run_cron(broken)
        self.assertEqual(service.state, 'error')
        self.assertTrue(service.error_message)
        self.assertTrue(service._is_halted(), '停權旗標保留，每日 cron 才會重試')
        self.assertFalse(service.resume_requested)
        invoice.invalidate_recordset()
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(invoice.payment_state, 'paid', 'operator 失敗不影響付款')
        self.assertEqual(self.partner.wallet_balance, balance_after_payment)

        client = _client()
        with patch(_CLIENT_SEAM, return_value=client):
            self.Lifecycle._cron_enforce_subscription_lifecycle()
        self.assertEqual(service.state, 'running')
        self.assertFalse(service.pre_suspend_state)
        self.assertFalse(service.error_message, '重試成功後不能留著「Resume failed」橫幅')

    def test_unpark_failure_is_loud_and_retried_by_the_daily_cron(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        self.Lifecycle._request_paid_resume(sub, source='test')
        broken = _client()
        broken.unpark_release_route.side_effect = PaaSOperatorAPIError(
            'API error', status_code=409,
            detail={'error_code': 'route_target_unresolved', 'message': 'no target'})
        self._run_cron(broken)
        self.assertEqual(service.state, 'error')
        self.assertIn('no target', service.error_message)
        self.assertTrue(service._is_halted())

        client = _client()
        with patch(_CLIENT_SEAM, return_value=client):
            self.Lifecycle._cron_enforce_subscription_lifecycle()
        self.assertEqual(service.state, 'running')
        self.assertFalse(service.error_message, '重試成功後不能留著「Resume failed」橫幅')

    def test_backlog_triggers_another_pass(self):
        services = self.Service
        for tag in range(3):
            sub = self._new_sub()
            services |= self._suspend(self._service(sub, f'b{tag}'))
            self.Lifecycle._request_paid_resume(sub, source='test')
        triggers_before = self._triggers()
        with patch('odoo.addons.woow_paas_platform.models.subscription_lifecycle_cron.'
                   '_PAID_RESUME_BATCH', 2):
            self._run_cron()
        self.assertEqual(len(services.filtered('resume_requested')), 1)
        self.assertTrue(self._triggers() - triggers_before, '有積壓要再排一趟')
        self._run_cron()
        self.assertFalse(services.filtered('resume_requested'))
        self.assertEqual(set(services.mapped('state')), {'running'})

    def test_cron_record_is_active(self):
        """``_trigger()`` 對停用的 cron 不建立立即觸發 ⇒ 這顆 cron 必須是 active。"""
        self.assertTrue(self.cron.active)
        self.assertEqual(self.cron.code.strip(), 'model._cron_resume_paid_cloud_services()')


@tagged('post_install', '-at_install')
class TestResumeClearsStaleError1599(_PaidResumeBase):
    """復權成功後清掉停權期間留下的錯誤說明（#1599 沙盒端到端發現）。

    ServiceDetailPage 的紅色橫幅只看 ``error_message``、不看 state：上一輪復權失敗寫的
    「Resume failed …will be retried automatically」若不清，重試成功後租戶會看到一台
    running 的服務掛著錯誤。回到 error 的服務（停權前就是 error）維持原說明。
    """

    def test_successful_resume_clears_the_previous_resume_failure(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        service.write({'error_message': 'Resume failed: operator down'})
        with patch(_CLIENT_SEAM, return_value=_client()):
            service._do_resume()
        self.assertEqual(service.state, 'running')
        self.assertFalse(service.error_message)

    def test_service_that_was_in_error_keeps_its_message(self):
        sub = self._new_sub()
        service = self._service(sub)
        service.write({'state': 'suspended', 'pre_suspend_state': 'error',
                       'error_message': 'ImagePullBackOff'})
        with patch(_CLIENT_SEAM, return_value=_client()):
            service._do_resume()
        self.assertEqual(service.state, 'error')
        self.assertEqual(service.error_message, 'ImagePullBackOff')


@tagged('post_install', '-at_install')
class TestDailyCronResumeLock1599(_PaidResumeBase):
    """每日 cron 也先取鎖：與付款後復權的 cron 同時跑到同一台時不重複 scale。"""

    def test_daily_cron_skips_a_service_being_resumed_elsewhere(self):
        sub = self._new_sub()
        service = self._suspend(self._service(sub))
        client = _client()
        with patch(_CLIENT_SEAM, return_value=client), \
                patch.object(type(service), '_acquire_resume_lock', return_value=False):
            self.Lifecycle._enforce_single(service, 7, 6, date.today())
        self.assertEqual(_resume_calls(client), [])
        self.assertEqual(service.state, 'suspended')

    def test_daily_cron_skips_renewal_rebind_when_busy(self):
        old, service = self._halted_with_cancelled_sub()
        new = old._sh_renew_subscription()
        new.write({'state': 'in_progress'})
        client = _client()
        with patch(_CLIENT_SEAM, return_value=client), \
                patch.object(type(service), '_acquire_resume_lock', return_value=False):
            self.Lifecycle._enforce_single(service, 7, 6, date.today())
        self.assertEqual(service.subscription_id, old, '鎖拿不到就連改綁也不做')
        self.assertEqual(_resume_calls(client), [])


@tagged('post_install', '-at_install')
class TestPaidResumeScope1599(_PaidResumeBase):
    """只做 Cloud Service：Smart Home／Security Access 不被標記、每日 cron 對它們不取鎖。"""

    def _smart_home(self):
        home = sh_model(self.env).create({
            'name': 'Home 1599', 'workspace_id': self.workspace.id})
        open_gates(home).write({'tunnel_id': 'tun-1599-sh'})
        return home

    def _security_access(self):
        sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'Gateway 1599', 'workspace_id': self.workspace.id})
        _sa_internal(sa).write({'state': 'active', 'tunnel_id': 'tun-1599-sa'})
        return sa

    def test_smart_home_and_security_access_are_never_requested(self):
        entities = [self._smart_home(), self._security_access()]
        subs = self.Sub
        for entity in entities:
            sub = self._new_sub()
            entity.write({'subscription_id': sub.id})
            entity.write({'state': 'suspended', 'pre_suspend_state': 'active'})
            sub.write({'state': 'cancel'})
            subs |= sub
        with patch.object(type(entities[0]), '_do_resume') as home_resume, \
                patch.object(type(entities[1]), '_do_resume') as sa_resume:
            for sub in subs:
                sub.write({'state': 'in_progress'})
                marked = self.Lifecycle._request_paid_resume(sub, source='test')
                self.assertFalse(marked)
            self._run_cron()
        home_resume.assert_not_called()
        sa_resume.assert_not_called()
        self.assertEqual([e.state for e in entities], ['suspended', 'suspended'])

    def test_daily_cron_still_resumes_smart_home_and_security_access(self):
        """seam 預設不取鎖：每日 cron 對 Smart Home／Security Access 的復權照舊。"""
        for entity in (self._smart_home(), self._security_access()):
            sub = self._new_sub()
            entity.write({'subscription_id': sub.id})
            entity.write({'state': 'suspended', 'pre_suspend_state': 'active'})
            self.assertTrue(entity._acquire_resume_lock())
            with patch.object(type(entity), '_do_resume') as resume:
                self.Lifecycle._enforce_single(entity, 7, 6, date.today())
            resume.assert_called_once()


@tagged('post_install', '-at_install')
class TestResumeLockRealTransactions1599(TransactionCase):
    """用真的第二、第三條 DB 連線驗 ``_acquire_resume_lock``（不是 mock）。

    * 另一筆交易正拿著這一列 ⇒ ``LockNotAvailable`` ⇒ False（不等、不重複 scale）；
    * 另一筆交易已 commit 改過這一列、本交易的 REPEATABLE READ 快照比它舊 ⇒
      ``SerializationFailure`` ⇒ False（每日 cron 跑到一半、付款後復權的 cron 已復權的情形）；
    * 新快照、沒人拿 ⇒ True。

    committed 的 fixture 由 ``CommittedCronScope`` 記帳並在結束時精確刪除。
    """

    def test_nowait_lock_against_real_transactions(self):
        scope = CommittedCronScope(self)
        self.addCleanup(scope.purge)
        with scope.track(), self.registry.cursor() as cr:
            env = scope.env(cr)
            workspace = env['woow_paas_platform.workspace'].create({'name': 'WS 1599 lock'})
            template = env['woow_paas_platform.cloud_app_template'].create({
                'name': 'App 1599 lock', 'slug': 'app-1599-lock',
                'helm_repo_url': 'https://example.invalid', 'helm_chart_name': 'chart',
                'helm_chart_version': '1.0.0', 'post_deploy_init_type': 'none'})
            service = _internal_provision(env[_SERVICE]).create({
                'name': 'Svc 1599 lock', 'workspace_id': workspace.id,
                'template_id': template.id, 'state': 'suspended',
                'pre_suspend_state': 'running',
                'helm_namespace': 'dev-sandbox-1599-lock',
                'helm_release_name': 'svc-1599-lock'})
            service_id = service.id
            table = service._table
            cr.commit()

        with self.registry.cursor() as holder, self.registry.cursor() as contender:
            contender_service = scope.env(contender)[_SERVICE].browse(service_id)
            # 釘住 contender 的快照（REPEATABLE READ：第一個查詢就決定）。
            contender.execute(f'SELECT id FROM {table} WHERE id = %s', (service_id,))
            holder.execute(f'SELECT id FROM {table} WHERE id = %s FOR NO KEY UPDATE',
                           (service_id,))
            self.assertFalse(contender_service._acquire_resume_lock(),
                             '別人拿著鎖：要立刻回 False')
            holder.execute(f'UPDATE {table} SET resume_requested = TRUE WHERE id = %s',
                           (service_id,))
            holder.commit()
            self.assertFalse(contender_service._acquire_resume_lock(),
                             '快照比別人的 commit 舊：40001 也要回 False')
            contender.rollback()

        with self.registry.cursor() as fresh:
            self.assertTrue(scope.env(fresh)[_SERVICE].browse(service_id)._acquire_resume_lock())
            fresh.rollback()
