"""#1528：HA REST API 對 Portal 成員的成員／角色判準與 /woow 一致。

2026-10-07 正式站實測（.210，Portal 帳號 uid 205）：/woow 列得出自己的 SH／SA，
走 HA 元件的 device flow 也拿得到完整 scope 的 token，但
``/api/smarthome/workspaces`` 回空、``/workspaces/<id>/homes`` 與 SA 端點全部 403
——``ha_api._get_user_workspaces``／``_check_workspace_access`` 對 ``user.share``
直接回空（#1261 時代的判斷），而 /woow 自 #991 起已改走 ``_PaaSCapability``。
真實租戶都是 Portal 帳號，所以所有客戶都接不上 HA 元件。

守的性質（每一格都同時對 Portal 與 internal 跑同一組斷言，證明兩邊一致）：
* 成員（owner／admin／user／guest）：兩支 workspace 清單列出、且**只**列出自己
  所屬的 active workspace；SH／SA 的清單、詳情、狀態端點 200。
* tunnel token（SH、SA）：只有 owner／admin（#1287）；user／guest 403
  ``insufficient_role``，回應不含 token。
* 角色以該 workspace 為準：A 的 admin 在 B 只是 guest ⇒ B 的 token 拿不到。
* 非成員、跨 workspace、封存的 workspace：403 ``Forbidden``（不是
  ``insufficient_role``），回應不含 token。
* 停用的帳號：清單 200 空、其他 403（不 500）。
* scope 閘不變：Portal owner 的 token 少了 ``smarthome:tunnel`` 照樣拿不到 token。
* 端到端：Portal owner 以真 HTTP 走完 HA 元件的 device flow 與 authorization
  code（PKCE）flow，拿到的 token 依元件的呼叫順序打完 SH／SA 全部端點，
  最後 revoke 後 401。
"""
import base64
import hashlib
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlencode
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import HttpCase, tagged

from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _sa_internal,
)

from .smart_home_fixture import sh_model

PASSWORD = 'HaPortal1528!pw'
HA_CLIENT_ID = 'woow-ha-smart-home'
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
REDIRECT_URI = 'http://homeassistant.local:8123/auth/external/woow/callback'
_SCOPES = ('workspace:read smarthome:read smarthome:tunnel '
           'security-access:read security-access:tunnel')
ROLES = ('owner', 'admin', 'user', 'guest')


@tagged('post_install', '-at_install')
class TestHaApiPortalMembership1528(HttpCase):

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        User = self.env['res.users'].sudo()
        groups = {
            'portal': self.env.ref('base.group_portal'),
            'internal': self.env.ref('base.group_user'),
        }

        def make_user(kind, tag):
            return User.create({
                'name': f'HA1528 {kind} {tag} {self.suffix}',
                'login': f'ha1528_{kind}_{tag}_{self.suffix}',
                'email': f'ha1528-{kind}-{tag}-{self.suffix}@test.example',
                'password': PASSWORD,
                'groups_id': [(6, 0, [groups[kind].id])],
            })

        self.users = {
            kind: {role: make_user(kind, role) for role in ROLES}
            for kind in ('portal', 'internal')
        }
        self.p_outsider = make_user('portal', 'outsider')
        self.p_gone = make_user('portal', 'gone')

        # 鑑別力前提：帳號類型真的如名字所說（`share` 是本單的分歧點）。
        for role in ROLES:
            self.assertTrue(self.users['portal'][role].share)
            self.assertFalse(self.users['internal'][role].share)
        self.assertTrue(self.p_outsider.share)

        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        self.Access = self.env['woow_paas_platform.workspace_access'].sudo()
        self.ws = {}
        self.home = {}
        self.sa = {}
        for kind in ('portal', 'internal'):
            members = self.users[kind]
            ws = Workspace.create({
                'name': f'HA1528 {kind} WS {self.suffix}',
                'owner_id': members['owner'].id,
            })
            self._ensure_member(ws, members['owner'], 'owner')
            for role in ('admin', 'user', 'guest'):
                self._ensure_member(ws, members[role], role)
            self.ws[kind] = ws
            self.home[kind] = sh_model(self.env).sudo().create({
                'name': f'HA1528 {kind} home',
                'workspace_id': ws.id,
                'state': 'active',
                'tunnel_id': f'tun-1528-sh-{kind}',
                'tunnel_token': f'tok-1528-sh-{kind}-{self.suffix}',
                'subdomain': f'paas-sm-1528{kind[:4]}-{self.suffix}',
                'tunnel_status': 'connected',
            })
            sa = self.env['woow_paas_platform.security_access'].sudo().create({
                'name': f'HA1528 {kind} gateway', 'workspace_id': ws.id,
            })
            _sa_internal(sa.sudo()).write({
                'state': 'active',
                'tunnel_id': f'tun-1528-sa-{kind}',
                'tunnel_token': f'tok-1528-sa-{kind}-{self.suffix}',
            })
            self.sa[kind] = sa

        # 跨 workspace 角色：Portal workspace 的 admin 在 internal workspace 只是 guest。
        self._ensure_member(
            self.ws['internal'], self.users['portal']['admin'], 'guest')

        # Portal owner 另有一個已封存（沒有服務）的 workspace。
        self.ws_archived = Workspace.create({
            'name': f'HA1528 archived WS {self.suffix}',
            'owner_id': self.users['portal']['owner'].id,
        })
        self._ensure_member(
            self.ws_archived, self.users['portal']['owner'], 'owner')
        self.ws_archived.write({'state': 'archived'})

        # 已停用的 Portal 帳號：成員列與停用前取得的 token 都還在，但 bearer
        # validity 必須在 controller/resource lookup 之前 fail closed。
        self._ensure_member(self.ws['portal'], self.p_gone, 'user')
        self._tokens = {}
        self._gone_token = self._token(self.p_gone)
        self.p_gone.active = False

        self.client_rec = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home')

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _ensure_member(self, workspace, user, role):
        row = self.Access.search([('workspace_id', '=', workspace.id),
                                  ('user_id', '=', user.id)])
        if row:
            # Workspace.create 已自動補 owner 列；角色相同就不寫（owner 列有
            # 寫入保護，no-op 寫入也會被擋）。
            self.assertEqual(row.role, role, 'fixture 前提：既有成員列角色一致')
        else:
            self.Access.create({'workspace_id': workspace.id,
                                'user_id': user.id, 'role': role})

    def _token(self, user, scope=_SCOPES):
        key = (user.id, scope)
        if key not in self._tokens:
            client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')
            self._tokens[key] = self.env['woow_paas_platform.oauth_token'].sudo().create({
                'access_token': f'ha1528-{user.id}-{uuid.uuid4().hex}',
                'scope': scope,
                'expires_at': fields.Datetime.now() + timedelta(hours=1),
                'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
                'client_id': client.id,
                'user_id': user.id,
            }).access_token
        return self._tokens[key]

    def _get(self, token, url):
        return self.url_open(url, headers={'Authorization': f'Bearer {token}'})

    def _as(self, user, url, scope=_SCOPES):
        return self._get(self._token(user, scope), url)

    def _urls(self, kind):
        ws, home, sa = self.ws[kind], self.home[kind], self.sa[kind]
        return {
            'sh_homes': f'/api/smarthome/workspaces/{ws.id}/homes',
            'sh_home': f'/api/smarthome/homes/{home.id}',
            'sh_status': f'/api/smarthome/homes/{home.id}/status',
            'sh_token': f'/api/smarthome/homes/{home.id}/tunnel-token',
            'sa_accesses': f'/api/security-access/workspaces/{ws.id}/accesses',
            'sa_access': f'/api/security-access/accesses/{sa.id}',
            'sa_status': f'/api/security-access/accesses/{sa.id}/status',
            'sa_token': f'/api/security-access/accesses/{sa.id}/tunnel-token',
        }

    def _secrets(self):
        return [r.tunnel_token for r in (*self.home.values(), *self.sa.values())]

    def _assert_no_secret(self, resp):
        for secret in self._secrets():
            self.assertNotIn(secret, resp.text)

    def _listed(self, user, url='/api/smarthome/workspaces'):
        resp = self._as(user, url)
        self.assertEqual(resp.status_code, 200, resp.text)
        return {w['id'] for w in resp.json()['workspaces']}

    def _assert_forbidden(self, resp, ctx):
        self.assertEqual(resp.status_code, 403, ctx)
        self.assertEqual(resp.json()['error'], 'Forbidden', ctx)
        self._assert_no_secret(resp)

    # ------------------------------------------------------------------ #
    # 清單
    # ------------------------------------------------------------------ #
    def test_workspace_lists_portal_matches_internal(self):
        """每個角色都列得出、且只列得出自己所屬的 active workspace（SH、SA 兩支）。"""
        expected = {
            ('portal', 'owner'): {self.ws['portal'].id},
            ('portal', 'admin'): {self.ws['portal'].id, self.ws['internal'].id},
            ('portal', 'user'): {self.ws['portal'].id},
            ('portal', 'guest'): {self.ws['portal'].id},
            ('internal', 'owner'): {self.ws['internal'].id},
            ('internal', 'admin'): {self.ws['internal'].id},
            ('internal', 'user'): {self.ws['internal'].id},
            ('internal', 'guest'): {self.ws['internal'].id},
        }
        for (kind, role), ids in expected.items():
            user = self.users[kind][role]
            for url in ('/api/smarthome/workspaces',
                        '/api/security-access/workspaces'):
                with self.subTest(kind=kind, role=role, url=url):
                    self.assertEqual(self._listed(user, url), ids)

    def test_non_member_portal_lists_nothing(self):
        """沒有任何成員資格的 Portal 帳號：200 空清單（HA 元件據此顯示「沒有工作區」）。"""
        for url in ('/api/smarthome/workspaces', '/api/security-access/workspaces'):
            with self.subTest(url=url):
                self.assertEqual(self._listed(self.p_outsider, url), set())

    # ------------------------------------------------------------------ #
    # 瀏覽類端點
    # ------------------------------------------------------------------ #
    def test_read_endpoints_all_roles_portal_and_internal(self):
        for kind in ('portal', 'internal'):
            urls = self._urls(kind)
            home, sa = self.home[kind], self.sa[kind]
            for role in ROLES:
                user = self.users[kind][role]
                ctx = f'{kind}/{role}'
                resp = self._as(user, urls['sh_homes'])
                self.assertEqual(resp.status_code, 200, ctx)
                self.assertEqual([h['id'] for h in resp.json()['homes']], [home.id], ctx)
                self._assert_no_secret(resp)
                for key in ('sh_home', 'sh_status'):
                    resp = self._as(user, urls[key])
                    self.assertEqual(resp.status_code, 200, f'{ctx} {key}')
                    self.assertEqual(resp.json()['home']['id'], home.id)
                    self._assert_no_secret(resp)
                resp = self._as(user, urls['sa_accesses'])
                self.assertEqual(resp.status_code, 200, ctx)
                self.assertEqual([a['id'] for a in resp.json()['accesses']], [sa.id], ctx)
                self._assert_no_secret(resp)
                for key in ('sa_access', 'sa_status'):
                    resp = self._as(user, urls[key])
                    self.assertEqual(resp.status_code, 200, f'{ctx} {key}')
                    self.assertEqual(resp.json()['access']['id'], sa.id)
                    self._assert_no_secret(resp)

    # ------------------------------------------------------------------ #
    # tunnel token（#1287 角色閘）
    # ------------------------------------------------------------------ #
    def test_tunnel_token_owner_admin_only_portal_and_internal(self):
        for kind in ('portal', 'internal'):
            urls = self._urls(kind)
            for role in ('owner', 'admin'):
                user = self.users[kind][role]
                with self.subTest(kind=kind, role=role):
                    sh = self._as(user, urls['sh_token'])
                    self.assertEqual(sh.status_code, 200, sh.text)
                    self.assertEqual(sh.json()['tunnel_token'],
                                     self.home[kind].tunnel_token)
                    sa = self._as(user, urls['sa_token'])
                    self.assertEqual(sa.status_code, 200, sa.text)
                    self.assertEqual(sa.json()['tunnel_token'],
                                     self.sa[kind].tunnel_token)
            for role in ('user', 'guest'):
                user = self.users[kind][role]
                for key in ('sh_token', 'sa_token'):
                    with self.subTest(kind=kind, role=role, url=key):
                        resp = self._as(user, urls[key])
                        self.assertEqual(resp.status_code, 403)
                        self.assertEqual(resp.json()['error'], 'insufficient_role')
                        self._assert_no_secret(resp)

    def test_role_is_per_workspace(self):
        """Portal workspace 的 admin 在 internal workspace 只是 guest：可看不可拿 token。"""
        p_admin = self.users['portal']['admin']
        urls = self._urls('internal')
        for key in ('sh_homes', 'sh_home', 'sh_status',
                    'sa_accesses', 'sa_access', 'sa_status'):
            with self.subTest(url=key):
                self.assertEqual(self._as(p_admin, urls[key]).status_code, 200)
        for key in ('sh_token', 'sa_token'):
            with self.subTest(url=key):
                resp = self._as(p_admin, urls[key])
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(resp.json()['error'], 'insufficient_role')
                self._assert_no_secret(resp)

    # ------------------------------------------------------------------ #
    # 負向：非成員、跨 workspace、封存、停用、scope
    # ------------------------------------------------------------------ #
    def test_non_member_and_cross_workspace_denied(self):
        cases = [
            # 完全沒有成員資格的 Portal 帳號
            (self.p_outsider, 'portal'),
            (self.p_outsider, 'internal'),
            # 別的 workspace 的 owner（Portal → internal、internal → Portal）
            (self.users['portal']['owner'], 'internal'),
            (self.users['portal']['user'], 'internal'),
            (self.users['internal']['owner'], 'portal'),
            (self.users['internal']['admin'], 'portal'),
        ]
        for user, target in cases:
            for key, url in self._urls(target).items():
                with self.subTest(user=user.login, target=target, url=key):
                    self._assert_forbidden(self._as(user, url), f'{user.login} {url}')

    def test_unknown_ids_are_not_found(self):
        user = self.users['portal']['owner']
        for url in ('/api/smarthome/homes/999999999',
                    '/api/smarthome/homes/999999999/tunnel-token',
                    '/api/security-access/accesses/999999999',
                    '/api/security-access/accesses/999999999/tunnel-token'):
            with self.subTest(url=url):
                self.assertEqual(self._as(user, url).status_code, 404)
        for url in ('/api/smarthome/workspaces/999999999/homes',
                    '/api/security-access/workspaces/999999999/accesses'):
            with self.subTest(url=url):
                self._assert_forbidden(self._as(user, url), url)

    def test_archived_workspace_denied(self):
        owner = self.users['portal']['owner']
        self.assertNotIn(self.ws_archived.id, self._listed(owner))
        for url in (f'/api/smarthome/workspaces/{self.ws_archived.id}/homes',
                    f'/api/security-access/workspaces/{self.ws_archived.id}/accesses'):
            with self.subTest(url=url):
                self._assert_forbidden(self._as(owner, url), url)

    def test_archived_workspace_hides_its_resources(self):
        """workspace 封存後，裡面的 SH／SA 對原本的 owner 也一律 403（含 token）。

        前提用 SQL 擺：#1295 之後 ORM 不讓「底下還有服務」的 workspace 封存，
        但 #1295 之前封存的資料可能就是這個形狀——HA 端點仍必須擋住。
        """
        ws = self.ws['portal']
        self.env.cr.execute(
            "UPDATE woow_paas_platform_workspace SET state = 'archived' WHERE id = %s",
            (ws.id,))
        self.env.invalidate_all()
        owner = self.users['portal']['owner']
        self.assertNotIn(ws.id, self._listed(owner))
        for key, url in self._urls('portal').items():
            with self.subTest(url=key):
                self._assert_forbidden(self._as(owner, url), url)

    def test_inactive_portal_user_denied_without_500(self):
        token = self._gone_token
        from odoo.addons.woow_paas_platform.controllers import ha_api

        def lookup_entered(*args, **kwargs):
            raise AssertionError('inactive bearer entered HA resource lookup')

        urls = [
            '/api/smarthome/workspaces',
            '/api/security-access/workspaces',
            *self._urls('portal').values(),
        ]
        with patch.object(ha_api, '_get_user_workspaces',
                          side_effect=lookup_entered) as get_workspaces, \
             patch.object(ha_api, '_check_workspace_access',
                          side_effect=lookup_entered) as check_workspace, \
             patch.object(ha_api.HAIntegrationController, '_get_home_with_access',
                          side_effect=lookup_entered) as get_home, \
             patch.object(ha_api.HAIntegrationController, '_get_sa_with_access',
                          side_effect=lookup_entered) as get_access:
            for url in urls:
                with self.subTest(url=url):
                    resp = self._get(token, url)
                    self.assertEqual(resp.status_code, 401, resp.text)
                    self.assertNotEqual(resp.status_code, 500, resp.text)
                    self.assertEqual(resp.json()['error'], 'Unauthorized')
                    self._assert_no_secret(resp)
            get_workspaces.assert_not_called()
            check_workspace.assert_not_called()
            get_home.assert_not_called()
            get_access.assert_not_called()

    def test_scope_gate_still_applies_to_portal_owner(self):
        owner = self.users['portal']['owner']
        urls = self._urls('portal')
        narrow = 'workspace:read smarthome:read'
        resp = self._as(owner, urls['sh_token'], scope=narrow)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['error'], 'Forbidden')
        self.assertIn('Insufficient scope', resp.json()['detail'])
        self._assert_no_secret(resp)
        for key in ('sa_accesses', 'sa_access', 'sa_token'):
            with self.subTest(url=key):
                resp = self._as(owner, urls[key], scope=narrow)
                self.assertEqual(resp.status_code, 403)
                self.assertIn('Insufficient scope', resp.json()['detail'])

    # ------------------------------------------------------------------ #
    # 端到端：HA 元件實際走的 OAuth 流程（真 HTTP）
    # ------------------------------------------------------------------ #
    def _csrf(self, html):
        match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        self.assertTrue(match, 'consent／verify 頁必須帶 csrf_token')
        return match.group(1)

    def _component_walk(self, access_token, kind='portal'):
        """照 HA 元件的呼叫順序打一輪（config flow → coordinator）。"""
        ws, home, sa = self.ws[kind], self.home[kind], self.sa[kind]
        listed = self._get(access_token, '/api/smarthome/workspaces')
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual([w['id'] for w in listed.json()['workspaces']], [ws.id])
        homes = self._get(access_token, f'/api/smarthome/workspaces/{ws.id}/homes')
        self.assertEqual(homes.status_code, 200, homes.text)
        self.assertEqual([h['id'] for h in homes.json()['homes']], [home.id])
        for url in (f'/api/smarthome/homes/{home.id}',
                    f'/api/smarthome/homes/{home.id}/status'):
            self.assertEqual(self._get(access_token, url).status_code, 200, url)
        token = self._get(access_token, f'/api/smarthome/homes/{home.id}/tunnel-token')
        self.assertEqual(token.status_code, 200, token.text)
        self.assertEqual(token.json()['tunnel_token'], home.tunnel_token)

        sa_ws = self._get(access_token, '/api/security-access/workspaces')
        self.assertEqual([w['id'] for w in sa_ws.json()['workspaces']], [ws.id])
        accesses = self._get(
            access_token, f'/api/security-access/workspaces/{ws.id}/accesses')
        self.assertEqual(accesses.status_code, 200, accesses.text)
        self.assertEqual([a['id'] for a in accesses.json()['accesses']], [sa.id])
        for url in (f'/api/security-access/accesses/{sa.id}',
                    f'/api/security-access/accesses/{sa.id}/status'):
            self.assertEqual(self._get(access_token, url).status_code, 200, url)
        token = self._get(
            access_token, f'/api/security-access/accesses/{sa.id}/tunnel-token')
        self.assertEqual(token.status_code, 200, token.text)
        self.assertEqual(token.json()['tunnel_token'], sa.tunnel_token)

    def _revoke_and_check(self, access_token):
        resp = self.url_open('/oauth2/revoke', data={
            'token': access_token, 'client_id': HA_CLIENT_ID})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(
            self._get(access_token, '/api/smarthome/workspaces').status_code, 401)

    def test_device_flow_end_to_end_portal_owner(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'web.base.url', self.base_url())
        owner = self.users['portal']['owner']
        resp = self.url_open('/oauth2/device_authorization', data={
            'client_id': HA_CLIENT_ID, 'scope': _SCOPES})
        self.assertEqual(resp.status_code, 200, resp.text)
        device = resp.json()

        def poll():
            return self.url_open('/oauth2/token', data={
                'grant_type': DEVICE_GRANT,
                'device_code': device['device_code'],
                'client_id': HA_CLIENT_ID,
            })

        pending = poll()
        self.assertEqual(pending.json().get('error'), 'authorization_pending')

        self.authenticate(owner.login, PASSWORD)
        page = self.url_open('/device?user_code=%s' % device['user_code'])
        self.assertEqual(page.status_code, 200)
        csrf = self._csrf(page.text)
        consent = self.url_open('/device', data={
            'csrf_token': csrf, 'user_code': device['user_code'], 'verify': '1'})
        self.assertEqual(consent.status_code, 200)
        approved = self.url_open('/device', data={
            'csrf_token': self._csrf(consent.text),
            'user_code': device['user_code'], 'approve': '1'})
        self.assertEqual(approved.status_code, 200)
        self.logout()

        issued = poll()
        self.assertEqual(issued.status_code, 200, issued.text)
        body = issued.json()
        self.assertEqual(set(body['scope'].split()), set(_SCOPES.split()))
        self._component_walk(body['access_token'])
        self._revoke_and_check(body['access_token'])

    def test_authorization_code_flow_end_to_end_portal_owner(self):
        owner = self.users['portal']['owner']
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        state = secrets.token_urlsafe(16)

        self.authenticate(owner.login, PASSWORD)
        page = self.url_open('/oauth2/authorize?' + urlencode({
            'response_type': 'code', 'client_id': HA_CLIENT_ID,
            'redirect_uri': REDIRECT_URI, 'scope': _SCOPES, 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
        }))
        self.assertEqual(page.status_code, 200)
        self.assertIn('approve', page.text)
        decision = self.url_open('/oauth2/authorize', data={
            'csrf_token': self._csrf(page.text),
            'response_type': 'code', 'client_id': HA_CLIENT_ID,
            'redirect_uri': REDIRECT_URI, 'scope': _SCOPES, 'state': state,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'approve': '1',
        }, allow_redirects=False)
        self.assertIn(decision.status_code, (302, 303), decision.text)
        location = decision.headers['Location']
        self.assertTrue(location.startswith(REDIRECT_URI), location)
        code = re.search(r'[?&]code=([^&]+)', location).group(1)
        self.logout()

        issued = self.url_open('/oauth2/token', data={
            'grant_type': 'authorization_code', 'code': code,
            'redirect_uri': REDIRECT_URI, 'client_id': HA_CLIENT_ID,
            'code_verifier': verifier,
        })
        self.assertEqual(issued.status_code, 200, issued.text)
        self._component_walk(issued.json()['access_token'])
        self._revoke_and_check(issued.json()['access_token'])
