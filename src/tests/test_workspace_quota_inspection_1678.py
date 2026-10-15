"""#1678: administrator quota inspection through real authenticated Odoo RPC.

The agreed seam is backend read/refresh -> operator HTTP. Only the external
operator client is simulated; Odoo, its caller identity, ORM and DB stay real.
"""
import json
import uuid
from unittest.mock import patch

import requests

from odoo.tests.common import HttpCase, tagged

from ..services.paas_operator import PaaSOperatorClient


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaInspection1678(HttpCase):
    def setUp(self):
        super().setUp()
        self.owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Quota owner', 'login': 'quota-owner-%s' % uuid.uuid4().hex,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].with_user(self.owner).create({
            'name': 'Quota inspection %s' % uuid.uuid4().hex,
        })
        self.namespace = 'paas-ws-%s' % self.workspace.ws_hash
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.namespace_prefix', 'paas-ws-')
        params.set_param('woow_paas_platform.operator_url', 'http://quota1678.invalid')
        params.set_param('woow_paas_platform.operator_api_key', 'isolated-test-key')
        self.report = {
            'namespace': self.namespace,
            'managed_quota': {
                'namespace': self.namespace, 'name': self.namespace + '-quota',
                'uid': 'quota-uid-1678', 'resource_version': '1678',
                'observed_at': '2026-10-12T16:30:00+00:00',
                'available': True, 'reason': None, 'namespace_exists': None,
                'hard': {'requests.cpu': '12', 'limits.cpu': '12',
                         'requests.memory': '24Gi', 'limits.memory': '24Gi',
                         'requests.storage': '100Gi'},
                'used': {'requests.cpu': '2500m', 'limits.cpu': '9500m',
                         'requests.memory': '6Gi', 'limits.memory': '18Gi',
                         'requests.storage': '60Gi'},
            },
        }

    def _call_kw(self, method, args, kwargs=None):
        response = self.url_open('/web/dataset/call_kw', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': 'woow_paas_platform.workspace', 'method': method,
                       'args': args, 'kwargs': kwargs or {}},
        }), headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_nonmember_system_administrator_reads_complete_managed_quota(self):
        self.assertFalse(self.workspace.access_ids.filtered(
            lambda access: access.user_id == self.env.ref('base.user_admin')))
        self.authenticate('admin', 'admin')  # uid 2, not Odoo SUPERUSER_ID
        fields = ['quota_namespace', 'quota_name', 'quota_uid', 'quota_resource_version',
                  'quota_observed_at', 'quota_available', 'quota_reason',
                  'quota_cpu_requests_hard', 'quota_cpu_requests_used',
                  'quota_cpu_limits_hard', 'quota_cpu_limits_used',
                  'quota_memory_requests_hard', 'quota_memory_requests_used',
                  'quota_memory_limits_hard', 'quota_memory_limits_used',
                  'quota_storage_requests_hard', 'quota_storage_requests_used']
        with patch.object(PaaSOperatorClient, 'get_namespace_quota',
                          return_value=self.report) as operator:
            response = self._call_kw('search_read', [[('id', '=', self.workspace.id)]],
                                     {'fields': fields})
        self.assertNotIn('error', response, response)
        self.assertEqual(len(response['result']), 1, response)
        row = response['result'][0]
        self.assertEqual(row['quota_namespace'], self.namespace)
        self.assertEqual(row['quota_name'], self.namespace + '-quota')
        self.assertEqual(row['quota_uid'], 'quota-uid-1678')
        self.assertEqual(row['quota_resource_version'], '1678')
        self.assertEqual(row['quota_observed_at'], '2026-10-12 16:30:00')
        self.assertIs(row['quota_available'], True)
        self.assertEqual((row['quota_cpu_requests_hard'], row['quota_cpu_requests_used']),
                         ('12', '2500m'))
        self.assertEqual((row['quota_cpu_limits_hard'], row['quota_cpu_limits_used']),
                         ('12', '9500m'))
        self.assertEqual((row['quota_memory_requests_hard'], row['quota_memory_requests_used']),
                         ('24Gi', '6Gi'))
        self.assertEqual((row['quota_memory_limits_hard'], row['quota_memory_limits_used']),
                         ('24Gi', '18Gi'))
        self.assertEqual((row['quota_storage_requests_hard'], row['quota_storage_requests_used']),
                         ('100Gi', '60Gi'))
        operator.assert_called_once_with(self.namespace, include_managed=True)

    def test_administrator_reads_managed_projection_over_operator_http(self):
        self.authenticate('admin', 'admin')
        original_request = requests.Session.request
        calls = []

        def request(session, method, url, **kwargs):
            if not url.startswith('http://quota1678.invalid/'):
                return original_request(session, method, url, **kwargs)
            calls.append((method, url, kwargs))
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(self.report).encode()
            return response

        with patch.object(requests.Session, 'request', new=request):
            result = self._call_kw('read', [[self.workspace.id]], {
                'fields': ['quota_available', 'quota_cpu_requests_used'],
            })
        self.assertNotIn('error', result, result)
        self.assertEqual(result['result'][0]['quota_cpu_requests_used'], '2500m')
        self.assertEqual(len(calls), 1)
        method, url, kwargs = calls[0]
        self.assertEqual(method, 'GET')
        self.assertEqual(url, 'http://quota1678.invalid/api/namespaces/%s/quota' % self.namespace)
        self.assertEqual(kwargs['params'], {'include_managed': True})
        self.assertEqual(kwargs['timeout'], (3, 8))

    def test_workspace_roles_and_forged_context_cannot_inspect_or_refresh(self):
        self.owner.write({'password': self.owner.login})
        internal_admin = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Internal Workspace admin', 'login': 'quota-admin-%s' % uuid.uuid4().hex,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        internal_admin.write({'password': internal_admin.login})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': internal_admin.id, 'role': 'admin',
        })
        portal = self.env['res.users'].with_context(no_reset_password=True)._create_user_from_template({
            'name': 'Portal quota member', 'login': 'quota-portal-%s' % uuid.uuid4().hex,
        })
        portal.write({'password': portal.login})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': portal.id, 'role': 'admin',
        })
        nonmember = internal_admin.copy({'login': 'quota-nonmember-%s' % uuid.uuid4().hex})
        nonmember.write({'password': nonmember.login})
        for user in (self.owner, internal_admin, nonmember):
            user.write({'groups_id': [(4, self.env.ref('base.group_allow_export').id)]})
        context = {'uid': self.env.ref('base.user_admin').id, 'su': True,
                   'quota_namespace': 'paas-ws-foreign', 'default_ws_hash': 'deadbeef',
                   'quota_inspection_admin': True}
        with patch.object(PaaSOperatorClient, 'get_namespace_quota') as operator:
            for user in (self.owner, internal_admin, portal, nonmember):
                self.assertFalse(user.has_group('base.group_system'))
                self.authenticate(user.login, user.login)
                for method, args, kwargs in (
                    ('read', [[self.workspace.id]], {'fields': ['quota_available']}),
                    ('search_read', [[]], {'fields': ['quota_cpu_requests_used']}),
                    ('export_data', [[self.workspace.id], ['quota_uid']], {}),
                    ('action_refresh_quota', [[self.workspace.id]], {}),
                ):
                    with self.subTest(user=user.login, method=method):
                        response = self._call_kw(method, args, dict(kwargs, context=context))
                        self.assertIn('error', response, response)
                        expected = ('odoo.exceptions.UserError' if user == portal and
                                    method == 'export_data' else 'odoo.exceptions.AccessError')
                        self.assertEqual(response['error']['data']['name'], expected, response)
                if user in (self.owner, internal_admin):
                    own = self._call_kw('read', [[self.workspace.id]], {'fields': ['name']})
                    self.assertNotIn('error', own, own)
                    self.assertEqual(own['result'][0]['name'], self.workspace.name)
        operator.assert_not_called()

    def test_incomplete_foreign_and_old_operator_data_remain_unknown(self):
        from copy import deepcopy
        self.authenticate('admin', 'admin')
        for change, reason in (
            ('missing_used', 'managed_quota_incomplete'),
            ('foreign', 'managed_quota_identity_mismatch'),
            ('missing_time', 'managed_quota_incomplete'),
            ('invalid_uid', 'managed_quota_incomplete'),
            ('old', 'operator_unsupported'),
        ):
            report = deepcopy(self.report)
            observation = report['managed_quota']
            if change == 'missing_used':
                observation['used'].pop('requests.cpu')
            elif change == 'foreign':
                observation['name'] = 'another-quota'
            elif change == 'missing_time':
                observation.pop('observed_at')
            elif change == 'invalid_uid':
                observation['uid'] = 1678
            else:
                report.pop('managed_quota')
            with self.subTest(change=change), patch.object(
                    PaaSOperatorClient, 'get_namespace_quota', return_value=report):
                result = self._call_kw('read', [[self.workspace.id]], {
                    'fields': ['quota_available', 'quota_reason', 'quota_cpu_requests_used'],
                })
                self.assertNotIn('error', result, result)
                row = result['result'][0]
                self.assertIs(row['quota_available'], False, row)
                self.assertEqual(row['quota_reason'], reason)
                if change in ('missing_used', 'foreign', 'old'):
                    self.assertIs(row['quota_cpu_requests_used'], False, row)

    def test_inspection_values_cannot_be_injected_through_rpc_or_defaults(self):
        self.authenticate('admin', 'admin')
        with patch.object(PaaSOperatorClient, 'get_namespace_quota') as operator:
            for method, args, kwargs in (
                ('write', [[self.workspace.id], {'quota_available': True}], {}),
                ('create', [{'name': 'Forged observation', 'quota_uid': 'forged'}], {}),
                ('create', [{'name': 'Forged default'}],
                 {'context': {'default_quota_cpu_requests_used': '0'}}),
                ('unlink', [[self.workspace.id]], {}),
                ('write', [[self.workspace.id], {'name': 'Cross-workspace write'}], {}),
            ):
                with self.subTest(method=method, args=args):
                    result = self._call_kw(method, args, kwargs)
                    self.assertIn('error', result, result)
                    self.assertEqual(result['error']['data']['name'],
                                     'odoo.exceptions.AccessError', result)
        operator.assert_not_called()

    def test_large_list_has_bounded_reads_and_refresh_observes_recovery(self):
        from copy import deepcopy
        workspaces = self.workspace | self.env['woow_paas_platform.workspace'].with_user(
            self.owner).create([{'name': 'Quota list %s' % uuid.uuid4().hex} for _ in range(6)])
        self.authenticate('admin', 'admin')

        def response(namespace, **kwargs):
            report = deepcopy(self.report)
            report['namespace'] = namespace
            report['managed_quota']['namespace'] = namespace
            report['managed_quota']['name'] = namespace + '-quota'
            return report

        with patch.object(PaaSOperatorClient, 'get_namespace_quota', side_effect=response) as operator:
            result = self._call_kw('search_read', [[('id', 'in', workspaces.ids)]], {
                'fields': ['quota_available', 'quota_reason', 'quota_cpu_requests_used',
                           'quota_cpu_limits_used'], 'order': 'id',
            })
        self.assertNotIn('error', result, result)
        self.assertEqual(len(result['result']), 7)
        self.assertEqual(operator.call_count, 5)
        self.assertEqual(sum(row['quota_available'] for row in result['result']), 5)
        for row in result['result'][5:]:
            self.assertEqual(row['quota_reason'], 'observation_batch_limit')
            self.assertIs(row['quota_cpu_requests_used'], False)
        with patch.object(PaaSOperatorClient, 'get_namespace_quota', return_value={}):
            unknown = self._call_kw('read', [[self.workspace.id]], {'fields': ['quota_available']})
            self.assertIs(unknown['result'][0]['quota_available'], False)
        refreshed = self._call_kw('action_refresh_quota', [[self.workspace.id]])
        self.assertEqual(refreshed['result'], {'type': 'ir.actions.client', 'tag': 'reload'})
        with patch.object(PaaSOperatorClient, 'get_namespace_quota', side_effect=response):
            recovered = self._call_kw('read', [[self.workspace.id]], {'fields': ['quota_available']})
        self.assertIs(recovered['result'][0]['quota_available'], True)

    def test_backend_views_expose_readonly_full_quota_and_unknown_state(self):
        from lxml import etree
        action = self.env.ref('woow_paas_platform.action_workspace_quota_inspection')
        action_views = [tuple(view) for view in action.read(['views'])[0]['views']]
        self.authenticate('admin', 'admin')
        result = self._call_kw('get_views', [action_views])
        self.assertNotIn('error', result, result)
        for kind in ('list', 'form'):
            arch = etree.fromstring(result['result']['views'][kind]['arch'].encode())
            self.assertEqual(arch.get('create'), 'false')
            self.assertEqual(arch.get('edit'), 'false')
            self.assertEqual(arch.get('delete'), 'false')
            names = {field.get('name') for field in arch.xpath('//field')}
            self.assertIn('quota_status', names)
            self.assertIn('quota_observed_at', names)
            for suffix in ('cpu_requests', 'cpu_limits', 'memory_requests',
                           'memory_limits', 'storage_requests'):
                self.assertIn('quota_%s_hard' % suffix, names)
                self.assertIn('quota_%s_used' % suffix, names)
        self.assertEqual(arch.xpath('//button[@name="action_refresh_quota"]')[0].get('type'),
                         'object')

    def test_http_failures_and_recovery_never_report_zero_as_unknown(self):
        from odoo.tools import mute_logger
        self.authenticate('admin', 'admin')
        original_request = requests.Session.request
        state = {'status': 200}

        def request(session, method, url, **kwargs):
            if not url.startswith('http://quota1678.invalid/'):
                return original_request(session, method, url, **kwargs)
            if state['status'] == 'timeout':
                raise requests.exceptions.Timeout('isolated read timeout')
            response = requests.Response()
            response.status_code = state['status']
            response._content = json.dumps(
                self.report if state['status'] == 200 else {'detail': 'isolated API failure'}
            ).encode()
            return response

        with patch.object(requests.Session, 'request', new=request), mute_logger(
                'odoo.addons.woow_paas_platform.services.paas_operator'):
            for status, reason in ((404, 'operator_unsupported'), (405, 'operator_unsupported'),
                                   (500, 'operator_error'), ('timeout', 'operator_error')):
                state['status'] = status
                result = self._call_kw('read', [[self.workspace.id]], {
                    'fields': ['quota_available', 'quota_reason', 'quota_cpu_requests_used'],
                })
                self.assertNotIn('error', result, result)
                row = result['result'][0]
                self.assertIs(row['quota_available'], False)
                self.assertEqual(row['quota_reason'], reason)
                self.assertIs(row['quota_cpu_requests_used'], False)
            state['status'] = 200
            self.report['managed_quota']['used']['requests.cpu'] = '0'
            result = self._call_kw('read', [[self.workspace.id]], {
                'fields': ['quota_available', 'quota_cpu_requests_used'],
                'context': {'quota_namespace': 'paas-ws-foreign',
                            'operator_url': 'http://foreign.invalid', 'uid': 1},
            })
            self.assertIs(result['result'][0]['quota_available'], True)
            self.assertEqual(result['result'][0]['quota_cpu_requests_used'], '0')

    def test_backend_global_read_does_not_bypass_frontend_membership(self):
        self.authenticate('admin', 'admin')
        with patch.object(PaaSOperatorClient, 'get_namespace_quota') as operator:
            response = self.url_open('/api/workspaces/%s/quota' % self.workspace.id,
                                     data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                                                      'params': {'include_managed': True}}),
                                     headers={'Content-Type': 'application/json'}).json()
        self.assertNotIn('error', response, response)
        self.assertIs(response['result']['success'], False)
        operator.assert_not_called()

    def test_default_workspace_views_stay_generic_outside_quota_action(self):
        # #1678 headed smoke：quota view 若成為 Workspace 唯一的 primary view，就是所有人的
        # 預設 view；非管理員從 Many2one 連結打開 Workspace 時，arch 有欄位但 fields 沒有，
        # web client 直接崩潰，管理員則每開一次連結就同步讀一次 operator。
        from lxml import etree
        # 已安裝 f8b31f8a 再升級的 DB 裡 quota view 的 id 較小，只有 priority 能讓一般 view 勝出。
        for kind in ('list', 'form'):
            self.assertGreater(
                self.env.ref('woow_paas_platform.view_workspace_quota_inspection_%s' % kind).priority,
                self.env.ref('woow_paas_platform.view_workspace_%s' % kind).priority)
        self.owner.password = self.owner.login
        for login, password in ((self.owner.login, self.owner.login), ('admin', 'admin')):
            self.authenticate(login, password)
            with patch.object(PaaSOperatorClient, 'get_namespace_quota') as operator:
                result = self._call_kw('get_views', [[(False, 'list'), (False, 'form')]])
                self.assertNotIn('error', result, result)
                fields = result['result']['models']['woow_paas_platform.workspace']['fields']
                for kind in ('list', 'form'):
                    arch = etree.fromstring(result['result']['views'][kind]['arch'].encode())
                    # 只看本 model 的欄位；內嵌 x2many 子 list 的欄位屬於別的 model。
                    names = {field.get('name')
                             for field in arch.xpath('//field[not(ancestor::field)]')}
                    self.assertIn('name', names, login)
                    self.assertFalse({name for name in names if name.startswith('quota_')},
                                     (login, kind))
                    self.assertLessEqual(names, set(fields), (login, kind))
                    # 照 web client 開啟紀錄的方式讀取 view 上的欄位，不得觸發 operator 讀取。
                    record = self._call_kw('read', [[self.workspace.id]], {'fields': sorted(names)})
                    self.assertNotIn('error', record, (login, kind, record))
            operator.assert_not_called()

    def test_platform_app_landing_is_not_quota_observation(self):
        # 點 Woow PaaS app 的落點維持原本第一個選單；quota 清單每列都會同步讀 operator。
        # load_web_menus 是 web client 取得 app 落點（actionID）的同一個來源。
        admin = self.env.ref('base.user_admin')
        menus = self.env['ir.ui.menu'].with_user(admin).load_web_menus(False)
        root = self.env.ref('woow_paas_platform.menu_woow_paas_platform_root')
        quota_action = self.env.ref('woow_paas_platform.action_workspace_quota_inspection')
        self.assertTrue(menus[root.id]['actionID'])
        self.assertNotEqual(menus[root.id]['actionID'], quota_action.id)
