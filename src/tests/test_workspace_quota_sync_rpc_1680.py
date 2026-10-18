"""#1680 Workspace quota 手動重試：真實登入的 Odoo RPC 權限邊界。

與 #1679 相同，只有 operator client 的 HTTP 方法被有狀態的假 operator 取代；呼叫者身分、
ORM、ir.rule、DB 都是真的。
"""
import uuid

from odoo.tests.common import tagged
from odoo.tools import html2plaintext

from ..services.paas_operator import PaaSOperatorAPIError
from .test_workspace_quota_floor_rpc_1679 import _AUDIT, _WS, _QuotaRpcFixture


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaSyncRpc1680(_QuotaRpcFixture):

    def _state(self):
        ws = self.workspace.sudo()
        ws.invalidate_recordset()
        return ws.quota_sync_state, ws.quota_retry_count

    def test_system_administrator_retries_manually_without_a_policy_change_record(self):
        self.authenticate('admin', 'admin')
        self.operator.put_error = PaaSOperatorAPIError('API error: boom', status_code=500)
        self._call_kw(_WS, 'set_quota_minimums', [[self.workspace.id]], {'cpu': 18})
        self.assertEqual(self._state(), ('pending', 0))
        self.operator.put_error = None
        response = self._call_kw(_WS, 'action_retry_quota_sync', [[self.workspace.id]])
        self.assertNotIn('error', response, response)
        self.assertEqual(response['result']['params']['type'], 'success')
        self.assertEqual(self._state(), ('synced', 0))
        self.assertEqual(self.operator.live[self.namespace]['limits.cpu'], '18')
        audit = self.env[_AUDIT].sudo().search([('workspace_ref', '=', self.workspace.id)])
        self.assertEqual(len(audit), 1, '手動重試不是政策變更，不寫異動紀錄')
        monitor = self.env['woow_paas_platform.platform_monitor']._singleton()
        body = html2plaintext(monitor.message_ids[:1].body)
        self.assertIn('quota sync retried manually by admin', body)
        read = self._call_kw(_WS, 'read', [[self.workspace.id]], {'fields': [
            'quota_sync_status', 'quota_retry_count', 'quota_next_retry_at',
            'quota_last_attempt_at', 'quota_verified_at', 'quota_sync_unguarded']})
        self.assertNotIn('error', read, read)
        self.assertEqual(read['result'][0]['quota_sync_status'], 'synced')

    def test_background_and_lifecycle_entries_are_not_rpc_bypasses(self):
        self.authenticate('admin', 'admin')
        for method in ('_quota_sync', '_quota_reconcile_one', '_cron_reconcile_workspace_quotas',
                       '_record_quota_sync', '_schedule_quota_reconcile'):
            with self.subTest(method=method):
                response = self._call_kw(_WS, method, [[self.workspace.id]])
                self.assertIn('error', response, response)
        for values in ({'quota_retry_count': 0}, {'quota_next_retry_at': False},
                       {'quota_expected_tokens': '{}'}, {'quota_sync_unguarded': False},
                       {'quota_attempt_fingerprint': 'x'}, {'quota_verified_at': False}):
            with self.subTest(values=values):
                response = self._call_kw(_WS, 'write', [[self.workspace.id], values])
                self.assertEqual(response['error']['data']['name'],
                                 'odoo.exceptions.AccessError', response)
        self.assertEqual(self.operator.puts, [])

    def test_other_roles_cannot_retry_or_read_retry_state(self):
        internal_admin = self._internal_user('retry-ws-admin')
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': internal_admin.id, 'role': 'admin'})
        portal = self.env['res.users'].with_context(no_reset_password=True)._create_user_from_template({
            'name': 'Retry portal', 'login': 'retry-portal-%s' % uuid.uuid4().hex})
        portal.write({'password': portal.login})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': portal.id, 'role': 'admin'})
        nonmember = self._internal_user('retry-nonmember')
        forged = {'uid': self.env.ref('base.user_admin').id, 'su': True,
                  'woow_ws_quota_policy_write': True}
        for user in (self.owner, internal_admin, portal, nonmember):
            self.authenticate(user.login, user.login)
            with self.subTest(user=user.login, call='retry'):
                response = self._call_kw(_WS, 'action_retry_quota_sync', [[self.workspace.id]],
                                         {'context': forged})
                self.assertEqual(response['error']['data']['name'],
                                 'odoo.exceptions.AccessError', response)
            with self.subTest(user=user.login, call='read'):
                response = self._call_kw(_WS, 'read', [[self.workspace.id]],
                                         {'fields': ['quota_retry_count']})
                self.assertIn('error', response, response)
        self.assertEqual((self.operator.reads, self.operator.puts), ([], []))
