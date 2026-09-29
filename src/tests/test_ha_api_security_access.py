"""HA component integration endpoints for Security Access.

Mirrors the SmartHome HA API: OAuth bearer-token protected, scope-graded
(security-access:read for listing/detail, security-access:tunnel for the
elevated tunnel-credentials endpoint), workspace-isolated.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged

# #522: server-owned identity anchors (tunnel_id/tunnel_token/dns_record_id/
# access_app_id/...) are immutable for everyone; provisioning code stores the
# ids Cloudflare hands back through this in-process sentinel hatch, and these
# fixtures simulate exactly that. See models/security_access.py.
from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
)


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
        _internal(self.sa.sudo()).write({
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

    def test_get_access_owner_omits_tunnel_token(self):
        """sa_get_access（security-access:read，較低 scope）回傳完整 to_dict()，
        owner 200 且 body 絕不含 tunnel_token。

        這條低-scope 全序列化端點先前無正向守線：未來 to_dict() 一旦新增敏感
        欄位（或把 tunnel_token 加回）會在這個端點靜默外洩給只有 read scope 的
        client。鎖住 token-not-in-body（鏡像 smart_home get_home 覆蓋）。
        """
        token = self._make_token(self.user, 'security-access:read')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}', token)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['access']['name'], 'HA Gateway')
        self.assertNotIn('tunnel_token', str(data))
        # tok-ha-secret 絕不可從 read-scope detail 端點外洩
        self.assertNotIn('tok-ha-secret', str(data))

    def test_outsider_cannot_get_foreign_sa_detail(self):
        """sa_get_access 各自的 cross-user negative（非僅靠 tunnel-token 間接守）。

        三個 read 端點（detail / status / tunnel-token）共用 _get_sa_with_access；
        每個 get 端點都需自己的 outsider 403，鎖死 resolver 不可被拆開後某端點
        失去守線（orm-vs-endpoint-acl-test-coverage：每 get endpoint 對等
        negative）。
        """
        token = self._make_token(self.outsider, 'security-access:read')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}', token)
        self.assertEqual(resp.status_code, 403)

    def test_outsider_cannot_get_foreign_sa_status(self):
        """sa_get_status 各自的 cross-user negative。

        status 端點在低 scope 下會打 operator 讀即時 tunnel 狀態（改動前還會
        action_refresh_status() 寫 DB；現在只讀，見 test_sh_live_tunnel_status），
        外人必須在 resolver 就被擋（403），絕不進到 operator 呼叫。"""
        token = self._make_token(self.outsider, 'security-access:read')
        resp = self._get(
            f'/api/security-access/accesses/{self.sa.id}/status', token)
        self.assertEqual(resp.status_code, 403)

    def test_list_workspaces_scopes_to_member(self):
        """sa_list_workspaces：member 看得到自己的 workspace，外人看不到。

        鏡像 smart_home 的 workspace 列表覆蓋；先前此端點完全無測試。"""
        member_tok = self._make_token(self.user, 'workspace:read')
        resp = self._get('/api/security-access/workspaces', member_tok)
        self.assertEqual(resp.status_code, 200)
        ws_ids = [w['id'] for w in resp.json()['workspaces']]
        self.assertIn(self.workspace.id, ws_ids)

        out_tok = self._make_token(self.outsider, 'workspace:read')
        resp = self._get('/api/security-access/workspaces', out_tok)
        self.assertEqual(resp.status_code, 200)
        out_ws_ids = [w['id'] for w in resp.json()['workspaces']]
        self.assertNotIn(self.workspace.id, out_ws_ids)

    def test_ha_client_allows_sa_scopes(self):
        """G-H3 守線：public client 必須被授權新 scope，否則 refresh fail。"""
        allowed = self.client_rec.get_allowed_scopes()
        self.assertIn('security-access:read', allowed)
        self.assertIn('security-access:tunnel', allowed)
