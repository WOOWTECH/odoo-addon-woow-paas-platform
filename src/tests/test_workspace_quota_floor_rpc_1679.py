"""#1679 Workspace quota 最低保障：真實登入的 Odoo RPC 權限邊界。

只有 operator client 的 HTTP 方法被有狀態的假 operator 取代（class 層級 patch，與 #1678
相同），Odoo 的呼叫者身分、ORM、ir.rule、DB 都是真的。
"""
import json
import uuid
from unittest.mock import patch

from odoo.tests.common import HttpCase, tagged

from ..services.paas_operator import PaaSOperatorClient
from .ns_quota_fixture import FakeQuotaOperator, build_hard

_WS = 'woow_paas_platform.workspace'
_AUDIT = 'woow_paas_platform.workspace_quota_audit'


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaFloorRpc1679(HttpCase):

    def setUp(self):
        super().setUp()
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.namespace_prefix', 'paas-ws-')
        params.set_param('woow_paas_platform.operator_url', 'http://quota1679.invalid')
        params.set_param('woow_paas_platform.operator_api_key', 'isolated-test-key')
        self.owner = self._internal_user('floor-owner')
        self.workspace = self.env[_WS].with_user(self.owner).create({
            'name': 'Floor RPC %s' % uuid.uuid4().hex})
        self.namespace = 'paas-ws-%s' % self.workspace.ws_hash
        self.operator = FakeQuotaOperator(
            {self.namespace: build_hard('8', '8Gi', '100Gi', '4Gi', '80Gi')})
        patches = [patch.object(PaaSOperatorClient, name, side_effect=getattr(self.operator, name))
                   for name in ('get_namespace_quota', 'update_namespace_quota', 'create_namespace')]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _internal_user(self, prefix):
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': prefix, 'login': '%s-%s' % (prefix, uuid.uuid4().hex),
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        user.write({'password': user.login})
        return user

    def _call_kw(self, model, method, args, kwargs=None):
        response = self.url_open('/web/dataset/call_kw', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': model, 'method': method, 'args': args, 'kwargs': kwargs or {}},
        }), headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _floors(self):
        ws = self.workspace.sudo()
        ws.invalidate_recordset()
        return (ws.quota_floor_cpu, ws.quota_floor_memory_gi, ws.quota_floor_storage_gi,
                ws.quota_target_version, ws.quota_sync_state)

    def test_system_administrator_sets_minimum_through_rpc(self):
        self.authenticate('admin', 'admin')
        response = self._call_kw(_WS, 'set_quota_minimums', [[self.workspace.id]],
                                 {'cpu': 18, 'storage_gi': 300})
        self.assertNotIn('error', response, response)
        self.assertEqual(response['result']['state'], 'synced')
        self.assertEqual(self.operator.puts[-1],
                         (self.namespace, '18', '8Gi', '300Gi', '4Gi', '80Gi'))
        self.assertEqual(self._floors(), (18, 0, 300, 1, 'synced'))
        read = self._call_kw(_WS, 'read', [[self.workspace.id]], {'fields': [
            'quota_floor_cpu', 'quota_formula_cpu', 'quota_target_cpu', 'quota_sync_state',
            'quota_synced_at', 'quota_audit_ids']})
        self.assertNotIn('error', read, read)
        row = read['result'][0]
        self.assertEqual((row['quota_formula_cpu'], row['quota_target_cpu']), ('8', '18'))
        self.assertTrue(row['quota_synced_at'])
        self.assertEqual(len(row['quota_audit_ids']), 1)

    def test_other_roles_and_forged_context_cannot_change_minimums(self):
        internal_admin = self._internal_user('floor-ws-admin')
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': internal_admin.id, 'role': 'admin'})
        portal = self.env['res.users'].with_context(no_reset_password=True)._create_user_from_template({
            'name': 'Floor portal', 'login': 'floor-portal-%s' % uuid.uuid4().hex})
        portal.write({'password': portal.login})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': portal.id, 'role': 'admin'})
        nonmember = self._internal_user('floor-nonmember')
        forged = {'uid': self.env.ref('base.user_admin').id, 'su': True,
                  'woow_ws_quota_policy_write': True, 'default_quota_floor_cpu': 99}
        before = self._floors()
        for user in (self.owner, internal_admin, portal, nonmember):
            self.assertFalse(user.has_group('base.group_system'))
            self.authenticate(user.login, user.login)
            for method, args, kwargs in (
                ('set_quota_minimums', [[self.workspace.id]], {'cpu': 99}),
                ('write', [[self.workspace.id], {'quota_floor_cpu': 99}], {}),
                ('write', [[self.workspace.id], {'quota_sync_state': 'synced'}], {}),
                ('create', [{'name': 'Forged floor', 'quota_floor_cpu': 99}], {}),
                ('create', [{'name': 'Forged default'}], {}),
            ):
                with self.subTest(user=user.login, method=method, args=args):
                    response = self._call_kw(_WS, method, args, dict(kwargs, context=forged))
                    self.assertIn('error', response, response)
                    self.assertEqual(response['error']['data']['name'],
                                     'odoo.exceptions.AccessError', response)
        self.assertEqual(self._floors(), before)
        self.assertEqual(self.operator.puts, [])
        self.assertFalse(self.env[_WS].sudo().search([('name', 'like', 'Forged')]))

    def test_system_administrator_cannot_bypass_the_quota_action(self):
        # 管理員同時是工作區 admin 成員：ir.rule 放行一般寫入，擋下 quota 欄位的只剩寫入守衛。
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': self.env.ref('base.user_admin').id,
            'role': 'admin'})
        self.authenticate('admin', 'admin')
        renamed = self._call_kw(_WS, 'write', [[self.workspace.id], {'name': 'Admin member rename'}])
        self.assertNotIn('error', renamed, '前提：成員管理員可以一般寫入')
        for values in ({'quota_floor_cpu': 99}, {'quota_target_version': 9},
                       {'quota_sync_state': 'synced'}, {'quota_pending_decrease': False}):
            with self.subTest(values=values):
                response = self._call_kw(_WS, 'write', [[self.workspace.id], values])
                self.assertEqual(response['error']['data']['name'],
                                 'odoo.exceptions.AccessError', response)
        self.assertEqual(self._floors(), (0, 0, 0, 0, False))
        self.assertNotIn('quota_floor_cpu', self.workspace.sudo().copy_data()[0])

    def test_portal_workspace_endpoint_ignores_quota_keys(self):
        portal = self.env['res.users'].with_context(no_reset_password=True)._create_user_from_template({
            'name': 'Floor portal admin', 'login': 'floor-portal-admin-%s' % uuid.uuid4().hex})
        portal.write({'password': portal.login})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': portal.id, 'role': 'admin'})
        self.authenticate(portal.login, portal.login)
        response = self.url_open('/api/workspaces/%s' % self.workspace.id, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': {
                'action': 'update', 'name': 'Renamed by portal',
                'quota_floor_cpu': 99, 'quota_target_version': 9, 'quota_sync_state': 'synced',
                'context': {'woow_ws_quota_policy_write': True, 'default_quota_floor_cpu': 99},
            }}), headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.workspace.invalidate_recordset()
        self.assertEqual(self.workspace.name, 'Renamed by portal')
        self.assertEqual(self._floors(), (0, 0, 0, 0, False))
        self.assertEqual(self.operator.puts, [])

    def test_quota_change_log_is_admin_only_and_immutable(self):
        self.authenticate('admin', 'admin')
        self._call_kw(_WS, 'set_quota_minimums', [[self.workspace.id]], {'cpu': 18})
        audit = self._call_kw(_AUDIT, 'search_read', [[('workspace_ref', '=', self.workspace.id)]],
                              {'fields': ['actor_login', 'new_values']})
        self.assertNotIn('error', audit, audit)
        self.assertEqual(audit['result'][0]['actor_login'], 'admin')
        audit_id = audit['result'][0]['id']
        for method, args in (('write', [[audit_id], {'new_values': 'forged'}]),
                             ('unlink', [[audit_id]]),
                             ('create', [{'workspace_ref': self.workspace.id, 'actor_uid': 2}])):
            with self.subTest(method=method):
                response = self._call_kw(_AUDIT, method, args)
                self.assertEqual(response['error']['data']['name'],
                                 'odoo.exceptions.AccessError', response)
        self.authenticate(self.owner.login, self.owner.login)
        denied = self._call_kw(_AUDIT, 'search_read', [[]], {'fields': ['new_values']})
        self.assertEqual(denied['error']['data']['name'], 'odoo.exceptions.AccessError', denied)
        denied = self._call_kw(_WS, 'read', [[self.workspace.id]], {'fields': ['quota_audit_ids']})
        self.assertEqual(denied['error']['data']['name'], 'odoo.exceptions.AccessError', denied)
