"""#1678 opt-in real Odoo RPC -> operator HTTP -> K8s quota read gate.

No mocked client, namespace resolver or operator. The only test-harness change
allows the exact isolated read URL through Odoo's external-request guard.
This is not a substitute for the separately required headed backend smoke.
"""
import json
import os
import unittest
import uuid
from urllib.parse import urlsplit

from odoo.tests.common import HttpCase, _super_send, tagged

_OPERATOR_URL = os.environ.get('QUOTA1678_OPERATOR_URL', '').rstrip('/')
_NAMESPACE = 'dev-sandbox-quota1678-1678cafe'


@tagged('post_install', '-at_install', 'quota1678_live')
class TestWorkspaceQuotaInspectionLive1678(HttpCase):
    @classmethod
    def _request_handler(cls, session, request, **kwargs):
        expected = urlsplit(_OPERATOR_URL + '/api/namespaces/' + _NAMESPACE + '/quota')
        actual = urlsplit(request.url)
        if (_OPERATOR_URL and request.method == 'GET' and actual.scheme == expected.scheme
                and actual.netloc == expected.netloc and actual.path == expected.path):
            return _super_send(session, request, **kwargs)
        return super()._request_handler(session, request, **kwargs)

    def test_nonmember_administrator_observes_real_managed_quota(self):
        if not _OPERATOR_URL:
            if os.environ.get('REQUIRE_QUOTA1678_LIVE') == '1':
                self.fail('QUOTA1678_OPERATOR_URL required for real quota read gate')
            raise unittest.SkipTest('Explicit isolated real operator fixture required')
        owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Live quota owner', 'login': 'live-quota-%s' % uuid.uuid4().hex,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Live quota inspection 1678', 'owner_id': owner.id, 'ws_hash': '1678cafe',
        })
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.namespace_prefix', 'dev-sandbox-quota1678-')
        params.set_param('woow_paas_platform.operator_url', _OPERATOR_URL)
        params.set_param('woow_paas_platform.operator_api_key', os.environ['QUOTA1678_API_KEY'])
        self.assertFalse(workspace.access_ids.filtered(
            lambda access: access.user_id == self.env.ref('base.user_admin')))
        self.authenticate('admin', 'admin')
        fields = ['quota_namespace', 'quota_name', 'quota_uid', 'quota_resource_version',
                  'quota_observed_at', 'quota_available', 'quota_reason',
                  'quota_cpu_requests_hard', 'quota_cpu_requests_used',
                  'quota_cpu_limits_hard', 'quota_cpu_limits_used',
                  'quota_memory_requests_hard', 'quota_memory_requests_used',
                  'quota_memory_limits_hard', 'quota_memory_limits_used',
                  'quota_storage_requests_hard', 'quota_storage_requests_used']
        response = self.url_open('/web/dataset/call_kw', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': {
                'model': 'woow_paas_platform.workspace', 'method': 'read',
                'args': [[workspace.id]], 'kwargs': {'fields': fields},
            },
        }), headers={'Content-Type': 'application/json'}).json()
        self.assertNotIn('error', response, response)
        row = response['result'][0]
        if os.environ.get('QUOTA1678_EXPECT_AVAILABLE') == 'false':
            self.assertIs(row['quota_available'], False, row)
            self.assertEqual(row['quota_reason'], 'managed_quota_not_found')
            for resource in ('cpu_requests', 'cpu_limits', 'memory_requests',
                             'memory_limits', 'storage_requests'):
                for quantity in ('hard', 'used'):
                    self.assertIs(row['quota_%s_%s' % (resource, quantity)], False)
            return
        self.assertIs(row['quota_available'], True, row)
        self.assertEqual(row['quota_namespace'], _NAMESPACE)
        self.assertEqual(row['quota_name'], _NAMESPACE + '-quota')
        self.assertEqual(row['quota_uid'], os.environ['QUOTA1678_QUOTA_UID'])
        self.assertEqual(row['quota_resource_version'], os.environ['QUOTA1678_QUOTA_VERSION'])
        self.assertTrue(row['quota_observed_at'])
        expected = {'cpu_requests': '2', 'cpu_limits': os.environ.get('QUOTA1678_CPU_LIMIT', '7'),
                    'memory_requests': '3Gi', 'memory_limits': '5Gi', 'storage_requests': '11Gi'}
        for resource, hard in expected.items():
            self.assertEqual(row['quota_%s_hard' % resource], hard)
            self.assertEqual(row['quota_%s_used' % resource], '0')
