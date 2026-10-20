"""#1681 管理員降額確認：真實登入、非 sudo 的 Odoo RPC 正負向。

只有 operator client 的 HTTP 方法被有狀態的假 operator 取代（同 #1679／#1680 的 RPC 測試），
呼叫者身分、ORM、ir.rule、ACL、DB 都是真的。
"""
import uuid

from odoo.tests.common import tagged

from .test_workspace_quota_floor_rpc_1679 import _QuotaRpcFixture, _WS

_WIZARD = 'woow_paas_platform.workspace_quota_minimum_wizard'


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaLowerConfirmRpc1681(_QuotaRpcFixture):

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.workspace.with_user(self.admin).set_quota_minimums(cpu=18)
        self.assertEqual(self.operator.live[self.namespace]['limits.cpu'], '18')

    def _error_name(self, response):
        self.assertIn('error', response, response)
        return response['error']['data']['name']

    def test_system_administrator_previews_and_confirms_a_decrease(self):
        self.operator.used[self.namespace] = {'limits.cpu': '5'}
        self.authenticate('admin', 'admin')
        preview = self._call_kw(_WS, 'preview_quota_minimums', [[self.workspace.id]], {'cpu': 0})
        self.assertNotIn('error', preview, preview)
        preview = preview['result']
        self.assertEqual(preview['state'], 'confirmation_required')
        self.assertIn('not atomic with Kubernetes admission', preview['warning'])
        self.assertEqual(self._floors()[0], 18, '預覽不保存')
        done = self._call_kw(_WS, 'set_quota_minimums', [[self.workspace.id]],
                             {'cpu': 0, 'confirmation': preview['confirmation']})
        self.assertNotIn('error', done, done)
        self.assertEqual(done['result']['state'], 'synced')
        self.assertEqual(self.operator.live[self.namespace]['limits.cpu'], '8')
        self.assertEqual(self._floors(), (0, 0, 0, 2, 'synced'))

    def test_system_administrator_without_valid_confirmation_is_rejected(self):
        self.authenticate('admin', 'admin')
        before = self._floors()
        token = self._call_kw(_WS, 'preview_quota_minimums', [[self.workspace.id]],
                              {'cpu': 10})['result']['confirmation']
        for kwargs in ({'cpu': 0}, {'cpu': 0, 'confirmation': token},
                       {'cpu': 0, 'confirmation': token[:-2] + '00'},
                       {'cpu': 0, 'confirmation': 'forged.sig'},
                       {'cpu': 0, 'context': {'allow_lower': True,
                                              'woow_ws_quota_policy_write': True}}):
            with self.subTest(kwargs=sorted(kwargs)):
                response = self._call_kw(_WS, 'set_quota_minimums', [[self.workspace.id]], kwargs)
                self.assertEqual(self._error_name(response), 'odoo.exceptions.UserError')
        self.assertEqual(self._floors(), before)
        self.assertEqual(self.operator.live[self.namespace]['limits.cpu'], '18')

    def test_unsafe_decrease_is_rejected_over_rpc(self):
        self.operator.used[self.namespace] = {'requests.cpu': '12'}
        self.authenticate('admin', 'admin')
        preview = self._call_kw(_WS, 'preview_quota_minimums', [[self.workspace.id]],
                                {'cpu': 0})['result']
        self.assertEqual(preview['state'], 'rejected')
        self.assertIn('requests.cpu used_exceeds', '; '.join(preview['reasons']))

    def test_other_roles_cannot_preview_confirm_or_reach_internal_sync(self):
        internal_admin = self._internal_user('lower-ws-admin')
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': internal_admin.id, 'role': 'admin'})
        portal = self.env['res.users'].with_context(
            no_reset_password=True)._create_user_from_template({
                'name': 'Lower portal', 'login': 'lower-portal-%s' % uuid.uuid4().hex})
        portal.write({'password': portal.login})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': portal.id, 'role': 'admin'})
        nonmember = self._internal_user('lower-nonmember')
        token = self.workspace.with_user(self.admin).preview_quota_minimums(
            cpu=0)['confirmation']
        forged = {'uid': self.admin.id, 'su': True, 'woow_ws_quota_policy_write': True,
                  'allow_lower': True}
        before = self._floors()
        for user in (self.owner, internal_admin, portal, nonmember):
            self.assertFalse(user.has_group('base.group_system'))
            self.authenticate(user.login, user.login)
            for model, method, args, kwargs in (
                (_WS, 'preview_quota_minimums', [[self.workspace.id]], {'cpu': 0}),
                (_WS, 'set_quota_minimums', [[self.workspace.id]],
                 {'cpu': 0, 'confirmation': token}),
                (_WS, 'write', [[self.workspace.id], {'quota_lower_authorization': 'x'}], {}),
                (_WIZARD, 'create', [{'workspace_id': self.workspace.id, 'cpu': 0}], {}),
            ):
                with self.subTest(user=user.login, method=method):
                    response = self._call_kw(model, method, args, dict(kwargs, context=forged))
                    self.assertEqual(self._error_name(response), 'odoo.exceptions.AccessError')
            # 底線方法不能經 RPC 呼叫（生命週期／背景的免確認入口）。
            response = self._call_kw(_WS, '_quota_sync', [[self.workspace.id], 'lifecycle'],
                                     {'allow_lower': True})
            self.assertIn('error', response, response)
        self.assertEqual(self._floors(), before)
        self.assertEqual(self.operator.live[self.namespace]['limits.cpu'], '18')
