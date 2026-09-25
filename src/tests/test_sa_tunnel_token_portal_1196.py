"""Portal-facing tunnel-token endpoint (#1196).

`security_access.tunnel_token` carries ``groups='base.group_system'``, so a
tenant cannot read it through the ORM, and ``to_dict()`` deliberately omits
it. The pre-existing `/api/security-access/accesses/<id>/tunnel-token` is
``auth='none'`` + OAuth scope — built for the HA component (a *machine*
consumer), not for a human who wants to paste the token into the Cloudflared
Web GUI add-on or `docker run`.

`api_security_access_tunnel_token` is the human-facing hatch. Because it
`sudo()`s past the field's group, its gate is the whole security story, so
every rung is pinned here:

* non-member            → denied (workspace gate)
* member, role=user     → denied (mutation-grade role gate)
* owner / admin         → allowed
* cross-workspace sa_id → denied (SA must belong to the gated workspace)
* unprovisioned tunnel  → structured error, never an empty-string "token"

`to_dict()` must still never carry the token — the new endpoint is the *only*
way out. That negative is asserted here too so a future convenience field on
`to_dict()` can't quietly widen exposure.
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged

# #522: server-owned identity anchors (tunnel_id/tunnel_token/...) are
# immutable for everyone; provisioning stores what Cloudflare returns through
# this in-process sentinel hatch, and the fixture below simulates exactly that.
from odoo.addons.woow_paas_platform.models.security_access import (
    _internal_identity as _internal,
)

_FAKE_TOKEN = 'eyJhIjoiZmFrZSIsInQiOiJmYWtlIiwicyI6ImZha2UifQ=='


def _new_user(env, login):
    """Internal user whose password == login (HttpCase auth convention)."""
    return env['res.users'].sudo().create({
        'name': login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestSaTunnelTokenPortal(HttpCase):
    """#1196：租戶自助取 tunnel token 的閘門。"""

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.owner = _new_user(self.env, f'sa_tok_owner_{suffix}')
        self.admin_member = _new_user(self.env, f'sa_tok_admin_{suffix}')
        self.plain_member = _new_user(self.env, f'sa_tok_user_{suffix}')
        self.outsider = _new_user(self.env, f'sa_tok_out_{suffix}')

        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        SA = self.env['woow_paas_platform.security_access'].sudo()

        self.workspace = Workspace.create({
            'name': f'SA Tok WS {suffix}', 'owner_id': self.owner.id,
        })
        # workspace.create auto-seeds the owner row; keep the fixture
        # idempotent in case that ever changes.
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.owner.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.owner.id, 'role': 'owner'})
        Access.create({'workspace_id': self.workspace.id,
                       'user_id': self.admin_member.id, 'role': 'admin'})
        Access.create({'workspace_id': self.workspace.id,
                       'user_id': self.plain_member.id, 'role': 'user'})

        self.sa = SA.create({
            'name': 'Tok Gateway', 'workspace_id': self.workspace.id,
        })
        # Simulate a provisioned tunnel (server-owned anchors). `_internal`
        # returns the recordset in the one context allowed to write them — it
        # is not a context manager.
        _internal(self.sa.sudo()).write({
            'tunnel_id': 'ffffffff-0000-0000-0000-000000000001',
            'tunnel_name': 'paas-sa-test-tok',
            'tunnel_token': _FAKE_TOKEN,
        })

        # A second workspace the same users are NOT members of, to pin the
        # cross-workspace case.
        self.other_ws = Workspace.create({
            'name': f'SA Tok Other {suffix}', 'owner_id': self.outsider.id,
        })
        self.other_sa = SA.create({
            'name': 'Other Gateway', 'workspace_id': self.other_ws.id,
        })

    # ------------------------------------------------------------------ #

    def _token_url(self, workspace_id=None, sa_id=None):
        return ('/api/workspaces/%s/security-accesses/%s/tunnel-token'
                % (workspace_id or self.workspace.id, sa_id or self.sa.id))

    def _rpc(self, url, params=None):
        response = self.url_open(
            url,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params or {}}),
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()
        # Keep the raw body so a negative test can tell a structured denial
        # apart from an endpoint crash (JSON-RPC error → no 'result').
        self._last_body = body
        return body.get('result') or {}

    def _assert_structured_denial(self, res):
        self.assertNotIn(
            'error', self._last_body,
            'endpoint crashed (JSON-RPC error) instead of returning a '
            'structured denial: %s' % self._last_body.get('error'))
        self.assertFalse(res.get('success'))
        self.assertIn('error', res)

    # ---- positive ---------------------------------------------------- #

    def test_owner_gets_token(self):
        self.authenticate(self.owner.login, self.owner.login)
        res = self._rpc(self._token_url())
        self.assertTrue(res.get('success'), res)
        self.assertEqual(res['data']['tunnel_token'], _FAKE_TOKEN)
        self.assertEqual(res['data']['tunnel_name'], 'paas-sa-test-tok')

    def test_admin_member_gets_token(self):
        """admin 與 owner 同級——能改 route 的人本來就能刪掉 SA 重建換 token，
        給讀取權不擴大實際攻擊面。"""
        self.authenticate(self.admin_member.login, self.admin_member.login)
        res = self._rpc(self._token_url())
        self.assertTrue(res.get('success'), res)
        self.assertEqual(res['data']['tunnel_token'], _FAKE_TOKEN)

    # ---- negative ------------------------------------------------------ #

    def test_plain_member_denied(self):
        """role=user 讀得到 SA，但拿不到 token（token 屬於變更等級的權限）。"""
        self.authenticate(self.plain_member.login, self.plain_member.login)
        res = self._rpc(self._token_url())
        self._assert_structured_denial(res)

    def test_non_member_denied(self):
        self.authenticate(self.outsider.login, self.outsider.login)
        res = self._rpc(self._token_url())
        self._assert_structured_denial(res)

    def test_cross_workspace_sa_denied(self):
        """sa_id 必須屬於被閘門放行的那個 workspace，不能借道別人的 SA。"""
        self.authenticate(self.owner.login, self.owner.login)
        res = self._rpc(self._token_url(sa_id=self.other_sa.id))
        self._assert_structured_denial(res)

    def test_unprovisioned_tunnel_is_structured_error(self):
        """還沒 provision 的 SA 要回明確錯誤，不能回空字串讓前端顯示一個假 token。"""
        SA = self.env['woow_paas_platform.security_access'].sudo()
        bare = SA.create({
            'name': 'Bare Gateway', 'workspace_id': self.workspace.id,
        })
        self.authenticate(self.owner.login, self.owner.login)
        res = self._rpc(self._token_url(sa_id=bare.id))
        self._assert_structured_denial(res)

    def test_to_dict_still_hides_token(self):
        """新端點是唯一出口；to_dict() 不可以順手把 token 帶出去。"""
        self.authenticate(self.owner.login, self.owner.login)
        res = self._rpc(
            '/api/workspaces/%s/security-accesses/%s' % (
                self.workspace.id, self.sa.id),
            {'action': 'get'})
        self.assertTrue(res.get('success'), res)
        self.assertNotIn('tunnel_token', res.get('data') or {})
