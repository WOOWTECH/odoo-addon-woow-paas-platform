# -*- coding: utf-8 -*-
"""#1424：開通失敗、從未正常運作過的服務不扣款（使用者 2026-10-04 決定採 A）。

1. operator 明確拒絕開通（409 ``workspace_storage_quota_exhausted``（#1348）／
   ``cluster_storage_capacity_exhausted``（#1415））且服務從未 running ⇒ 自動取消訂閱、
   不扣款；訊息說「訂閱已自動取消、不會扣款，請刪除這個服務後稍後再重新開通」。
2. 一直卡在 error、從未 running 過的服務 ⇒ 扣款排程跳過它的訂閱、提醒租戶一次；服務之後
   恢復 running ⇒ 下一次排程照常扣款，新週期從扣款日起算。

守的事（除上面兩條外）：

* 取消後 lifecycle cron 不去停權（叢集上沒東西可停，打 operator 只會 404 並把說明蓋掉）；
  cleanup 期滿時 operator 回 404＝已經不在，照常收尾、孤兒 PVC 只留 1 天（#1431）；
* 訂閱已取消，租戶仍能刪除這台服務（其他已取消訂閱的服務照舊擋）——含真 HTTP 端點；
* 曾經 running 過、平台代開（HA PaaS）、其他錯誤、取消失敗 ⇒ 訂閱不動、訊息照舊；
* 已付費的週期依未使用天數退回錢包；
* 計費靜默告警、D-3 低餘額預警不把暫停中的訂閱當成問題。
"""
import json
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, TransactionCase, tagged

from ..models.cloud_service import (
    CLUSTER_STORAGE_CAPACITY_ERROR_CODE,
    STORAGE_QUOTA_ERROR_CODE,
    _internal_provision,
)
from ..services.paas_operator import PaaSOperatorAPIError

_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)
_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_SILENCE_SUMMARY = '計費路徑靜默：到期訂閱連續多日未被處理'
GIB = 1024 ** 3

CAPACITY_DETAIL = {
    'error_code': CLUSTER_STORAGE_CAPACITY_ERROR_CODE,
    'namespace': 'paas-ws-test',
    'unschedulable': [{'name': 'svc-1424-data', 'kind': 'PersistentVolumeClaim',
                       'size': '80Gi', 'size_bytes': 80 * GIB, 'replicas': 2,
                       'replicas_placeable': 0}],
}
QUOTA_DETAIL = {
    'error_code': STORAGE_QUOTA_ERROR_CODE,
    'namespace': 'paas-ws-test',
    'exhausted': ['persistentvolumeclaims'],
    'usage': {'persistentvolumeclaims': {'used': '10', 'hard': '10'}},
    'orphaned_pvc_count': 9, 'orphaned_storage': '90Gi',
}


class _Fixture:
    """TransactionCase 與 HttpCase 共用：試用方案、租戶、服務＋訂閱。"""

    def _setup_fixture(self):
        self.env['res.lang']._activate_lang('zh_TW')
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 1424', 'code': 'TS1424', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Billing 1424', 'slug': 'billing-1424', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/billing-1424',
            'helm_chart_version': '0.1.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })
        self.plan = self.env['sh.subscription.plan'].create({
            'name': 'Plan 1424', 'sh_plan_price': 100.0, 'sh_duration': 1, 'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1, 'sh_trial': True, 'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })
        self.env['product.template'].sudo().create({
            'name': 'Plan 1424 P', 'type': 'service', 'sh_product_subscribe': True,
            'sh_subscription_plan_id': self.plan.id, 'list_price': 100.0,
        })

    def _user(self, lang='zh_TW', password=None, email=True):
        login = 'tenant1424-%s@tenant1424.tw' % uuid.uuid4().hex[:8]
        vals = {
            'name': 'Tenant 1424 %s' % lang, 'login': login, 'lang': lang,
            'tz': 'Asia/Taipei', 'email': login if email else False,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        }
        if password:
            vals['password'] = password
        return self.env['res.users'].create(vals)

    def _service(self, user, subscription=True, **extra):
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1424 %s' % uuid.uuid4().hex[:6], 'owner_id': user.id})
        vals = {
            'name': 'svc-1424', 'workspace_id': ws.id, 'template_id': self.tpl.id,
            'state': 'pending', 'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-1424-%s' % uuid.uuid4().hex[:6],
            'subdomain': 'paas-cs-1424-%s' % uuid.uuid4().hex[:6],
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        }
        vals.update(extra)
        svc = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].with_user(user).sudo()
        ).create(vals).with_env(self.env)
        if subscription:
            svc._create_and_confirm_subscription(user.partner_id, self.plan)
        return svc

    def _deploy_failing_with(self, svc, detail, status=409):
        client = MagicMock()
        client.install_release.side_effect = PaaSOperatorAPIError(
            message='API error: %s' % detail, status_code=status, detail=detail)
        with patch(_CLIENT_PATH, return_value=client):
            svc._execute_deploy()
        svc.invalidate_recordset()
        return svc

    def _moves(self, partner):
        return self.env['account.move'].sudo().search([('partner_id', '=', partner.id)])

    def _deducts(self, partner):
        return self.env['wallet.transaction'].sudo().search([
            ('partner_id', '=', partner.id), ('transaction_type', '=', 'deduct')])


@tagged('post_install', '-at_install')
class TestProvisionRejectionCancelsSubscription1424(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._setup_fixture()

    def test_cluster_capacity_rejection_cancels_the_trial_subscription(self):
        user = self._user('zh_TW')
        svc = self._service(user)
        sub = svc.sudo().subscription_id
        self.assertEqual((sub.state, sub.sh_trial), ('in_progress', True), 'fixture 前提')
        moves_before = len(self._moves(user.partner_id))

        svc = self._deploy_failing_with(svc, CAPACITY_DETAIL)

        self.assertEqual(svc.state, 'error')
        self.assertEqual(sub.state, 'cancel', '#1424：operator 明確拒絕 ⇒ 訂閱自動取消')
        self.assertEqual(svc.provision_rejected_code, CLUSTER_STORAGE_CAPACITY_ERROR_CODE)
        self.assertEqual(len(self._moves(user.partner_id)), moves_before, '不產生發票')
        self.assertFalse(self.env['account.move'].sudo().search(
            [('sh_subscription_id', '=', sub.id)]))
        self.assertFalse(self._deducts(user.partner_id), '不扣點')
        msg = svc.error_message
        self.assertIn('平台儲存空間暫時不足', msg)
        self.assertIn('這個服務的訂閱已自動取消，不會扣款。', msg)
        self.assertIn('請刪除這個服務後稍後再重新開通', msg)
        self.assertNotIn('刪除服務會一併取消訂閱', msg, '舊說法（刪除才取消）已不成立')

    def test_workspace_quota_rejection_cancels_the_trial_subscription(self):
        user = self._user('zh_TW')
        svc = self._deploy_failing_with(self._service(user), QUOTA_DETAIL)
        self.assertEqual(svc.sudo().subscription_id.state, 'cancel')
        self.assertEqual(svc.provision_rejected_code, STORAGE_QUOTA_ERROR_CODE)
        msg = svc.error_message
        self.assertIn('工作區儲存配額已滿', msg)
        self.assertIn('這個服務的訂閱已自動取消，不會扣款。', msg)
        self.assertIn('請刪除這個服務', msg)
        self.assertNotIn('如需提早釋放請聯絡客服，或改在其他工作區開通此服務。', msg,
                         '取消後的版本改成先叫租戶刪除')

    def test_english_creator(self):
        user = self._user('en_US')
        svc = self._deploy_failing_with(self._service(user), CAPACITY_DETAIL)
        self.assertIn(
            'Its subscription has been cancelled automatically, so it will not be charged.',
            svc.error_message)
        self.assertIn('Please delete this service and deploy it again later', svc.error_message)
        svc = self._deploy_failing_with(self._service(self._user('en_US')), QUOTA_DETAIL)
        self.assertIn('Please delete this service, then contact support', svc.error_message)

    def test_a_paid_cycle_is_refunded_and_said_so(self):
        user = self._user('zh_TW')
        svc = self._service(user)
        sub = svc.sudo().subscription_id
        today = fields.Date.today()
        # 已付費的一期（非試用、剛扣款）：退點前提是真有一筆 done 的 deduct（#324）。
        sub.write({
            'sh_trial': False, 'sh_start_date': today,
            'sh_date_of_next_payment': today + timedelta(days=30),
            'sh_unit': 'day', 'sh_recurrency': 30,
        })
        self.env['wallet.transaction'].sudo().create({
            'partner_id': user.partner_id.id, 'transaction_type': 'deduct',
            'amount': -sub.sh_plan_price, 'subscription_id': sub.id, 'state': 'done',
        })
        self.env.invalidate_all()

        svc = self._deploy_failing_with(svc, CAPACITY_DETAIL)

        self.assertEqual(sub.state, 'cancel')
        refunds = self.env['wallet.transaction'].sudo().search([
            ('partner_id', '=', user.partner_id.id), ('transaction_type', '=', 'refund')])
        self.assertEqual(sum(refunds.mapped('amount')), sub.sh_plan_price,
                         '剛扣款就被拒 ⇒ 整期退回')
        self.assertIn('已扣的點數已依未使用天數退回錢包', svc.error_message)

    def test_a_service_that_ran_before_keeps_its_subscription(self):
        user = self._user('zh_TW')
        svc = self._service(user)
        svc.write({'first_running_at': fields.Datetime.now() - timedelta(days=3)})
        svc = self._deploy_failing_with(svc, CAPACITY_DETAIL)
        self.assertEqual(svc.sudo().subscription_id.state, 'in_progress')
        self.assertFalse(svc.provision_rejected_code)
        self.assertIn('刪除服務會一併取消訂閱', svc.error_message, '照舊說法')

    def test_platform_provisioned_service_is_not_cancelled(self):
        """HA PaaS 這類平台代開的服務：App 會原地重試開通，取消訂閱會讓重試成功的服務被停權。"""
        user = self._user('zh_TW')
        svc = self._service(user, provision_partner_id=user.partner_id.id)
        svc = self._deploy_failing_with(svc, QUOTA_DETAIL)
        self.assertEqual(svc.sudo().subscription_id.state, 'in_progress')
        self.assertFalse(svc.provision_rejected_code)

    def test_other_operator_errors_do_not_cancel(self):
        user = self._user('en_US')
        svc = self._deploy_failing_with(
            self._service(user),
            'Helm installation failed. Check operator logs for details.', status=500)
        self.assertEqual(svc.state, 'error')
        self.assertEqual(svc.sudo().subscription_id.state, 'in_progress')
        self.assertFalse(svc.provision_rejected_code)

    def test_when_cancelling_fails_the_error_still_lands_with_the_old_wording(self):
        user = self._user('zh_TW')
        svc = self._service(user)
        sub = svc.sudo().subscription_id
        with patch.object(type(sub), '_settle_on_entity_delete',
                          side_effect=RuntimeError('boom')):
            svc = self._deploy_failing_with(svc, CAPACITY_DETAIL)
        self.assertEqual(svc.state, 'error')
        self.assertEqual(sub.state, 'in_progress', '取消失敗 ⇒ 整組不生效')
        self.assertFalse(svc.provision_rejected_code)
        self.assertIn('刪除服務會一併取消訂閱', svc.error_message)

    def test_without_a_subscription_nothing_mentions_one(self):
        user = self._user('zh_TW')
        svc = self._deploy_failing_with(self._service(user, subscription=False), CAPACITY_DETAIL)
        self.assertEqual(svc.provision_rejected_code, CLUSTER_STORAGE_CAPACITY_ERROR_CODE)
        self.assertNotIn('訂閱', svc.error_message)


@tagged('post_install', '-at_install')
class TestRejectedServiceAfterCancellation1424(_Fixture, TransactionCase):
    """取消之後：lifecycle cron、cleanup、刪除閘。"""

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.user = self._user('zh_TW')
        self.svc = self._deploy_failing_with(self._service(self.user), CAPACITY_DETAIL)
        self.sub = self.svc.sudo().subscription_id
        self.assertEqual(self.sub.state, 'cancel', 'fixture 前提')
        self.message = self.svc.error_message
        self.Cron = self.env['woow_paas_platform.subscription_lifecycle_cron']

    def _run_lifecycle(self, client):
        with patch(_CLIENT_PATH, return_value=client):
            self.Cron._cron_enforce_subscription_lifecycle()
        self.svc.invalidate_recordset()

    def test_lifecycle_cron_does_not_suspend_or_touch_it(self):
        client = MagicMock()
        client.scale_release.side_effect = PaaSOperatorAPIError(
            message='not found', status_code=404, detail='No deployments')
        for _day in range(2):
            self._run_lifecycle(client)
            self.assertEqual(self.svc.state, 'error')
            self.assertEqual(self.svc.error_message, self.message, '說明不被蓋掉')
        calls = [c for c in client.scale_release.call_args_list
                 if self.svc.helm_release_name in c.args]
        self.assertFalse(calls, '沒有東西可停，不打 operator')

    def test_cleanup_after_retention_tolerates_the_missing_release(self):
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE sh_subscription_subscription SET write_date = now() - interval '400 days' "
            "WHERE id = %s", (self.sub.id,))
        self.env.invalidate_all()
        client = MagicMock()
        client.uninstall_release.side_effect = PaaSOperatorAPIError(
            message='not found', status_code=404,
            detail='Release svc not found in namespace paas-ws-test')
        self._run_lifecycle(client)
        self.assertEqual(self.svc.state, 'deleting', '404＝已經不在，照常收尾（不是 Cleanup failed）')
        mine = [c for c in client.uninstall_release.call_args_list
                if self.svc.helm_release_name in c.args]
        self.assertEqual(mine[0].kwargs.get('grace_period_days'), 1, '#1431：從未 running')
        # 收尾：短 poll 看到 release 不在 ⇒ 刪掉記錄（既有路徑）。
        client.get_status.side_effect = PaaSOperatorAPIError(
            message='not found', status_code=404, detail='not found')
        svc_id = self.svc.id
        self.svc._poll_and_update_status(client)
        self.assertFalse(self.env['woow_paas_platform.cloud_service'].browse(svc_id).exists())

    def test_tenant_can_delete_it_but_not_change_it(self):
        svc = self.svc.with_user(self.user)
        svc._check_operation_allowed(operation='delete')  # 不 raise
        with self.assertRaises(UserError):
            svc._check_operation_allowed(operation='write')

    def test_other_cancelled_services_still_cannot_be_deleted(self):
        other = self._service(self._user('zh_TW'))
        other.sudo().subscription_id.write({'state': 'cancel'})
        other.write({'state': 'error', 'error_message': 'crashed'})
        with self.assertRaises(UserError):
            other.with_user(other.workspace_id.owner_id)._check_operation_allowed(
                operation='delete')

    def test_tenant_cannot_forge_the_marker(self):
        other = self._service(self.user)
        try:
            other.with_user(self.user).write(
                {'provision_rejected_code': CLUSTER_STORAGE_CAPACITY_ERROR_CODE})
        except Exception as e:  # AccessError
            self.assertIn('Service history fields are managed by the platform', str(e))
        else:
            self.fail('租戶不得寫 provision_rejected_code')


@tagged('post_install', '-at_install')
class TestNeverRunningBillingHold1424(_Fixture, TransactionCase):

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.today = fields.Date.today()

    def _stuck(self, lang='zh_TW', email=True, **service_extra):
        user = self._user(lang, email=email)
        svc = self._service(user, **service_extra)
        svc.write({'state': 'deploying'})
        svc.write({'state': 'error', 'error_message': 'Deployment timed out after 30 minutes'})
        sub = svc.sudo().subscription_id
        # 試用期滿、今天到期。
        sub.write({'sh_date_of_next_payment': self.today, 'sh_unit': 'month', 'sh_recurrency': 1})
        self.assertFalse(svc.first_running_at, 'fixture 前提：從未 running')
        return user, svc, sub

    def _hold_mails(self, partner):
        if not partner.email:
            return self.env['mail.mail']
        return self.env['mail.mail'].sudo().search([
            ('email_to', 'ilike', partner.email),
            ('subject', 'in', ('[Woow PaaS] 從未正常運作的服務不會扣款',
                               '[Woow PaaS] A service that never started will not be charged')),
        ])

    def _patch_settle(self, succeed=True):
        return patch.object(
            type(self.Sub), '_try_settle_invoice', lambda self, sub, invoice: succeed)

    def test_not_charged_and_told_once(self):
        user, svc, sub = self._stuck('zh_TW')
        for _day in range(2):
            with self._patch_settle(True):
                self.Sub._sh_create_auto_invoice()
            sub.invalidate_recordset()
            self.assertFalse(self.env['account.move'].sudo().search(
                [('sh_subscription_id', '=', sub.id)]), '不開發票')
            self.assertEqual(sub.state, 'in_progress')
            self.assertEqual(sub.sh_date_of_next_payment, self.today, '付款日不動')
            self.assertEqual(sub.woow_consecutive_payment_failures, 0, '不算付款失敗')
        mails = self._hold_mails(user.partner_id)
        self.assertEqual(len(mails), 1, '只提醒一次')
        self.assertEqual(mails.subject, '[Woow PaaS] 從未正常運作的服務不會扣款', '依收件人語言')
        self.assertIn('svc-1424', mails.body_html)
        self.assertIn('/woow#/workspace/%s/services/%s' % (svc.workspace_id.id, svc.id),
                      mails.body_html)
        self.assertTrue(sub.woow_billing_hold_notified_at)
        self.assertFalse(self._deducts(user.partner_id))

    def test_english_tenant_gets_the_english_notice(self):
        user, _svc, _sub = self._stuck('en_US')
        self.Sub._sh_create_auto_invoice()
        self.assertEqual(self._hold_mails(user.partner_id).subject,
                         '[Woow PaaS] A service that never started will not be charged')

    def test_without_an_email_nothing_is_stamped(self):
        user, _svc, sub = self._stuck('zh_TW', email=False)
        user.partner_id.email = False
        self.Sub._sh_create_auto_invoice()
        self.assertFalse(sub.woow_billing_hold_notified_at, '沒寄出就不蓋章，下次再試')

    def test_billing_resumes_once_it_runs_and_the_period_starts_that_day(self):
        _user, svc, sub = self._stuck('zh_TW')
        self.Sub._sh_create_auto_invoice()  # 暫停
        svc.write({'state': 'running', 'error_message': False})  # #1416 自癒／修好
        self.assertTrue(svc.first_running_at)
        with self._patch_settle(True):
            self.Sub._sh_create_auto_invoice()
        sub.invalidate_recordset()
        self.assertEqual(len(self.env['account.move'].sudo().search(
            [('sh_subscription_id', '=', sub.id)])), 1, '恢復後照常開票扣款（只扣一期）')
        self.assertEqual(sub.sh_date_of_next_payment, self.today + relativedelta(months=1),
                         '新週期從扣款日起算')

    def test_a_service_that_ran_before_is_billed_as_usual(self):
        user, svc, sub = self._stuck('zh_TW')
        svc.write({'first_running_at': fields.Datetime.now() - timedelta(days=2)})
        with self._patch_settle(True):
            self.Sub._sh_create_auto_invoice()
        self.assertEqual(len(self.env['account.move'].sudo().search(
            [('sh_subscription_id', '=', sub.id)])), 1)
        self.assertFalse(self._hold_mails(user.partner_id))

    def test_platform_provisioned_service_is_billed_as_usual(self):
        user = self._user('zh_TW')
        user2, svc, sub = self._stuck('zh_TW', provision_partner_id=user.partner_id.id)
        with self._patch_settle(True):
            self.Sub._sh_create_auto_invoice()
        self.assertEqual(len(self.env['account.move'].sudo().search(
            [('sh_subscription_id', '=', sub.id)])), 1)
        self.assertFalse(self._hold_mails(user2.partner_id))

    def test_billing_silence_alert_ignores_held_subscriptions(self):
        self.env['res.users'].sudo().create({
            'name': 'Billing Manager 1424', 'login': 'billing_mgr_1424',
            'groups_id': [(4, self.env.ref('account.group_account_manager').id)],
        })
        _user, _svc, sub = self._stuck('zh_TW')
        sub.write({'sh_date_of_next_payment': self.today - timedelta(days=10)})
        Activity = self.env['mail.activity'].sudo()
        self.Sub._cron_billing_silence_alert()
        self.assertFalse(Activity.search([('summary', '=', _SILENCE_SUMMARY)]),
                         '暫停扣款是刻意的，不是計費路徑死掉')
        # 對照：同樣逾期、沒有綁暫停中服務的訂閱照樣告警。
        control = self.Sub.create({
            'sh_partner_id': self._user('zh_TW').partner_id.id,
            'sh_plan_price': 100.0, 'sh_qty': 1, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': self.today - timedelta(days=60),
            'sh_end_date': self.today + timedelta(days=300),
            'sh_date_of_next_payment': self.today - timedelta(days=10),
            'state': 'in_progress',
        })
        self.Sub._cron_billing_silence_alert()
        alerts = Activity.search([('summary', '=', _SILENCE_SUMMARY)])
        self.assertTrue(alerts)
        self.assertEqual(set(alerts.mapped('res_id')), {control.id})

    def test_pre_renewal_low_balance_warning_skips_held_subscriptions(self):
        user, _svc, sub = self._stuck('zh_TW')
        sub.write({'sh_date_of_next_payment': self.today + timedelta(days=2)})
        self.env['woow_paas_platform.low_balance_warning_cron'] \
            ._cron_warn_upcoming_renewal_low_balance()
        sub.invalidate_recordset()
        self.assertFalse(sub.woow_last_low_balance_warned_for)
        self.assertFalse(self.env['mail.mail'].sudo().search([
            ('email_to', 'ilike', user.partner_id.email),
            ('subject', '=', 'Action needed: Please top up your wallet')]))


@tagged('post_install', '-at_install')
class TestDeleteEndpointAfterRejection1424(_Fixture, HttpCase):
    """真 HTTP：租戶從 /woow 的刪除端點刪得掉「開通被拒、訂閱已取消」的服務。"""

    PASSWORD = 'tenant-1424-Pw!'

    def setUp(self):
        super().setUp()
        self._setup_fixture()
        self.user = self._user('zh_TW', password=self.PASSWORD)

    def _delete(self, svc):
        client = MagicMock()
        client.uninstall_release.side_effect = PaaSOperatorAPIError(
            message='not found', status_code=404, detail='Release not found')
        self.authenticate(self.user.login, self.PASSWORD)
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call',
                           'params': {'action': 'delete'}})
        with patch('%s.get_paas_operator_client' % _CONTROLLER, return_value=client), \
                patch(_CLIENT_PATH, return_value=client):
            resp = self.url_open(
                '/api/workspaces/%s/services/%s' % (svc.workspace_id.id, svc.id),
                data=body, headers={'Content-Type': 'application/json'})
        return resp.json()['result']

    def test_rejected_service_can_be_deleted(self):
        svc = self._deploy_failing_with(self._service(self.user), CAPACITY_DETAIL)
        self.assertEqual(svc.sudo().subscription_id.state, 'cancel', 'fixture 前提')
        svc_id = svc.id
        result = self._delete(svc)
        self.assertTrue(result.get('success'), result)
        self.assertFalse(self.env['woow_paas_platform.cloud_service'].browse(svc_id).exists())

    def test_ordinary_cancelled_service_is_still_blocked(self):
        svc = self._service(self.user)
        svc.sudo().subscription_id.write({'state': 'cancel'})
        svc.write({'state': 'error', 'error_message': 'crashed'})
        result = self._delete(svc)
        self.assertFalse(result.get('success'), result)
        self.assertTrue(svc.exists())
