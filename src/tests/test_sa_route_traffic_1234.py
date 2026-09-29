"""Security Access route 流量（#1234 第二步）：routes 端點的 ``traffic`` action。

資料來自 operator 的 ``POST /api/analytics/traffic``（Cloudflare zone analytics）。
這裡守的是 Odoo 端：權限同 list、每種拿不到的原因都變成可解釋的值而不是錯誤
（2026-09-29 正式站 operator token 實際缺 Zone Analytics Read ⇒ ``permission``）。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorAPIError

_CLIENT_FACTORY = 'odoo.addons.woow_paas_platform.models.security_access.get_paas_operator_client'


def _new_user(env, login):
    return env['res.users'].sudo().create({
        'name': login, 'login': login, 'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestRouteTrafficEndpoint(HttpCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.member = _new_user(self.env, f'sa_tr_user_{suffix}')
        self.outsider = _new_user(self.env, f'sa_tr_out_{suffix}')
        owner = _new_user(self.env, f'sa_tr_owner_{suffix}')
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        self.workspace = Workspace.create({'name': f'SA Traffic {suffix}', 'owner_id': owner.id})
        Access.create({'workspace_id': self.workspace.id,
                       'user_id': self.member.id, 'role': 'user'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'Traffic GW', 'workspace_id': self.workspace.id})
        Route = self.env['woow_paas_platform.security_access_route'].sudo()
        with patch.object(type(self.env['woow_paas_platform.security_access']),
                          '_sync_routes_to_operator'), \
                patch.object(type(Route), '_sync_access_to_operator'):
            self.route = Route.create({'security_access_id': self.sa.id,
                                       'subdomain_prefix': f'tr-a-{suffix}',
                                       'service_url': 'http://localhost:8080'})

    def _traffic(self):
        resp = self.url_open(
            '/api/workspaces/%s/security-accesses/%s/routes' % (self.workspace.id, self.sa.id),
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': {'action': 'traffic'}}),
            headers={'Content-Type': 'application/json'})
        self._last_body = resp.json()
        return self._last_body.get('result') or {}

    def _client(self, **kw):
        client = MagicMock()
        client.get_hostname_traffic = MagicMock(**kw)
        return client

    def test_member_gets_traffic_keyed_by_route_id(self):
        host = self.route.hostname
        client = self._client(return_value={
            'available': True, 'window_hours': 24,
            'hosts': {host: {'requests': 7, 'status_2xx': 7, 'status_3xx': 0,
                             'status_4xx': 0, 'status_5xx': 0,
                             'last_request_at': '2026-09-29T00:50:00Z'}}})
        self.authenticate(self.member.login, self.member.login)
        with patch(_CLIENT_FACTORY, return_value=client):
            res = self._traffic()
        self.assertTrue(res.get('success'), self._last_body)
        self.assertTrue(res['data']['available'])
        self.assertEqual(res['data']['routes'][str(self.route.id)]['requests'], 7)
        client.get_hostname_traffic.assert_called_once_with([host], 24)

    def test_permission_reason_is_passed_through(self):
        client = self._client(return_value={'available': False, 'reason': 'permission', 'hosts': {}})
        self.authenticate(self.member.login, self.member.login)
        with patch(_CLIENT_FACTORY, return_value=client):
            res = self._traffic()
        self.assertTrue(res.get('success'), self._last_body)
        self.assertEqual((res['data']['available'], res['data']['reason']), (False, 'permission'))

    def test_old_operator_without_endpoint_is_operator_outdated(self):
        client = self._client(side_effect=PaaSOperatorAPIError('Not Found', status_code=404))
        self.authenticate(self.member.login, self.member.login)
        with patch(_CLIENT_FACTORY, return_value=client):
            res = self._traffic()
        self.assertTrue(res.get('success'), self._last_body)
        self.assertEqual(res['data']['reason'], 'operator_outdated')

    def test_no_operator_configured_is_disabled(self):
        self.authenticate(self.member.login, self.member.login)
        with patch(_CLIENT_FACTORY, return_value=None):
            res = self._traffic()
        self.assertEqual(res['data']['reason'], 'disabled')

    def test_outsider_denied(self):
        self.authenticate(self.outsider.login, self.outsider.login)
        with patch(_CLIENT_FACTORY) as factory:
            res = self._traffic()
        self.assertNotIn('error', self._last_body, self._last_body.get('error'))
        self.assertFalse(res.get('success'))
        factory.assert_not_called()
