"""HA API tunnel-token 端點的角色閘：只有 workspace owner／admin。

2026-09-30 正式站實測：role=user 成員在 /woow 取 SA token 被擋（權限不足），
改走 HA OAuth 卻從 ``/api/smarthome/homes/<id>/tunnel-token`` 與
``/api/security-access/accesses/<id>/tunnel-token`` 拿到完整 token——這兩支只過
``_check_workspace_access``（是否為成員），沒看 role。拿到 token 的人可以自己跑
connector 加入 tunnel、分走流量，屬於設定等級；user／guest 只能用瀏覽類功能。

守的性質：
* owner／admin → 200（SH、SA 各一）
* user／guest → 403，``error == 'insufficient_role'``（HA component 靠它和
  scope 不足／非成員區分，改提示「請 owner／admin 設定」而不是叫人 reauth）
* 同一個 user 成員的唯讀端點（詳情、狀態、列表）照舊 200——只收緊 token
* 非成員仍是原本的 403（不是 insufficient_role，閘門順序不變）
"""
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _sa_internal,
)

from .smart_home_fixture import sh_model

_SCOPES = ('workspace:read smarthome:read smarthome:tunnel '
           'security-access:read security-access:tunnel')


@tagged('post_install', '-at_install')
class TestHaApiTunnelTokenRole(HttpCase):

    def setUp(self):
        super().setUp()
        import uuid
        suffix = uuid.uuid4().hex[:6]
        User = self.env['res.users'].sudo()

        def _user(tag):
            return User.create({
                'name': f'HA Role {tag} {suffix}',
                'login': f'ha_role_{tag}_{suffix}',
                'email': f'ha_role_{tag}_{suffix}@test.example',
                'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
            })

        self.owner = _user('owner')
        self.admin_member = _user('admin')
        self.plain_member = _user('user')
        self.guest = _user('guest')
        self.outsider = _user('out')

        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': f'HA Role WS {suffix}', 'owner_id': self.owner.id,
        })
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.owner.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.owner.id, 'role': 'owner'})
        for user, role in ((self.admin_member, 'admin'),
                           (self.plain_member, 'user'),
                           (self.guest, 'guest')):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': user.id, 'role': role})

        self.home = sh_model(self.env).sudo().create({
            'name': 'Role HA',
            'workspace_id': self.workspace.id,
            'state': 'active',
            'tunnel_id': 'tun-role-sh',
            'tunnel_token': 'tok-role-sh',
            'subdomain': 'paas-sm-role0001-02030405',
            'tunnel_status': 'connected',
        })
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'Role Gateway', 'workspace_id': self.workspace.id,
        })
        _sa_internal(self.sa.sudo()).write({
            'state': 'active', 'tunnel_id': 'tun-role-sa',
            'tunnel_token': 'tok-role-sa',
        })
        self.client_rec = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home')

    def _token(self, user):
        return self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': f'ha-role-{user.id}',
            'scope': _SCOPES,
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'client_id': self.client_rec.id,
            'user_id': user.id,
        })

    def _get(self, user, url):
        token = self._token(user)
        return self.url_open(
            url, headers={'Authorization': f'Bearer {token.access_token}'})

    def _sh_url(self):
        return f'/api/smarthome/homes/{self.home.id}/tunnel-token'

    def _sa_url(self):
        return f'/api/security-access/accesses/{self.sa.id}/tunnel-token'

    # ---- owner / admin 放行 ------------------------------------------ #

    def test_owner_gets_both_tokens(self):
        sh = self._get(self.owner, self._sh_url())
        self.assertEqual(sh.status_code, 200)
        self.assertEqual(sh.json()['tunnel_token'], 'tok-role-sh')
        sa = self._get(self.owner, self._sa_url())
        self.assertEqual(sa.status_code, 200)
        self.assertEqual(sa.json()['tunnel_token'], 'tok-role-sa')

    def test_admin_gets_both_tokens(self):
        self.assertEqual(self._get(self.admin_member, self._sh_url()).status_code, 200)
        self.assertEqual(self._get(self.admin_member, self._sa_url()).status_code, 200)

    # ---- user / guest 擋下 ------------------------------------------- #

    def _assert_insufficient_role(self, resp):
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body['error'], 'insufficient_role')
        self.assertNotIn('tok-role', resp.text)

    def test_user_member_denied_both_tokens(self):
        self._assert_insufficient_role(self._get(self.plain_member, self._sh_url()))
        self._assert_insufficient_role(self._get(self.plain_member, self._sa_url()))

    def test_guest_member_denied_both_tokens(self):
        self._assert_insufficient_role(self._get(self.guest, self._sh_url()))
        self._assert_insufficient_role(self._get(self.guest, self._sa_url()))

    def test_user_member_still_reads_non_secret_endpoints(self):
        """只收緊 token；成員的瀏覽類端點不受影響。"""
        for url in (
            '/api/smarthome/workspaces',
            f'/api/smarthome/workspaces/{self.workspace.id}/homes',
            f'/api/smarthome/homes/{self.home.id}',
            f'/api/security-access/workspaces/{self.workspace.id}/accesses',
            f'/api/security-access/accesses/{self.sa.id}',
        ):
            resp = self._get(self.plain_member, url)
            self.assertEqual(resp.status_code, 200, url)

    def test_outsider_keeps_membership_denial(self):
        """非成員在角色閘之前就被擋，錯誤碼維持原本的 Forbidden。"""
        resp = self._get(self.outsider, self._sh_url())
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['error'], 'Forbidden')
