"""HA component integration endpoints for Security Access.

Mirrors the SmartHome HA API: OAuth bearer-token protected, scope-graded
(security-access:read for listing/detail, security-access:tunnel for the
elevated tunnel-credentials endpoint), workspace-isolated.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestHaApiSecurityAccess(HttpCase):
    """HA component 串接端點：scope 分級 + workspace 隔離。"""

    def setUp(self):
        super().setUp()
        import uuid as _uuid
        suffix = _uuid.uuid4().hex[:6]
        User = self.env['res.users'].sudo()
        self.user = User.create({
            'name': f'HA SA User {suffix}',
            'login': f'ha_sa_user_{suffix}',
            'email': f'ha_sa_{suffix}@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.outsider = User.create({
            'name': f'HA SA Outsider {suffix}',
            'login': f'ha_sa_out_{suffix}',
            'email': f'ha_sa_out_{suffix}@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': f'HA SA WS {suffix}', 'owner_id': self.user.id})
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.user.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.user.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'HA Gateway', 'workspace_id': self.workspace.id})
        self.sa.sudo().write({
            'state': 'active', 'tunnel_id': 'tun-ha',
            'tunnel_token': 'tok-ha-secret',
        })
        self.client_rec = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home')

    def _make_token(self, user, scope):
        return self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': f'sa-tok-{user.id}-{scope.replace(" ", "_")}',
            'scope': scope,
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'client_id': self.client_rec.id,
            'user_id': user.id,
        })

    def _get(self, url, token):
        return self.url_open(
            url, headers={'Authorization': f'Bearer {token.access_token}'})

    def test_list_accesses_with_read_scope(self):
        token = self._make_token(self.user, 'security-access:read')
        resp = self._get(
            f'/api/security-access/workspaces/{self.workspace.id}/accesses',
            token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data['accesses']), 1)
        self.assertEqual(data['accesses'][0]['name'], 'HA Gateway')
        # 列表不可外洩 token
        self.assertNotIn('tunnel_token', str(data))

    def test_tunnel_token_requires_tunnel_scope(self):
        read_only = self._make_token(self.user, 'security-access:read')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}/tunnel-token',
            read_only)
        self.assertEqual(resp.status_code, 403)

    def test_tunnel_token_with_correct_scope(self):
        token = self._make_token(
            self.user, 'security-access:read security-access:tunnel')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}/tunnel-token',
            token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['tunnel_token'], 'tok-ha-secret')
        self.assertEqual(data['tunnel_id'], 'tun-ha')

    def test_outsider_cannot_read_foreign_sa(self):
        token = self._make_token(
            self.outsider, 'security-access:read security-access:tunnel')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}/tunnel-token',
            token)
        self.assertEqual(resp.status_code, 403)

    def test_outsider_cannot_list_foreign_workspace_accesses(self):
        """sa_list_accesses 用 _check_workspace_access 直接擋（非 _get_sa_with_access）。

        orm-vs-endpoint-acl-test-coverage pattern：每個 list 端點都需一條
        token-bearing 外人 vs 外部 workspace 的 cross-user negative，鎖住
        list gate 與 detail gate 各自獨立的迴歸守線（鏡像 smart_home 的
        test_home_not_visible_in_other_workspace / test_no_workspace_access）。
        """
        token = self._make_token(self.outsider, 'security-access:read')
        resp = self._get(
            f'/api/security-access/workspaces/{self.workspace.id}/accesses',
            token)
        self.assertEqual(resp.status_code, 403)

    def test_ha_client_allows_sa_scopes(self):
        """G-H3 守線：public client 必須被授權新 scope，否則 refresh fail。"""
        allowed = self.client_rec.get_allowed_scopes()
        self.assertIn('security-access:read', allowed)
        self.assertIn('security-access:tunnel', allowed)
