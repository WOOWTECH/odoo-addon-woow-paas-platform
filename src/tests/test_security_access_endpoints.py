"""Endpoint-level cross-user negative coverage for Security Access.

ORM 綠 ≠ endpoint secure (``orm-vs-endpoint-acl-test-coverage`` pattern):
every list/get/routes endpoint in ``controllers/security_access.py`` is
paired here with a cross-user negative so a future ``.sudo()`` slip or an
``ir.rule`` regression can't silently widen tenant visibility past the
controller gate.

Gating under test (``SecurityAccessController._get_workspace_or_error`` /
``_get_sa_or_error``): a caller must hold a ``workspace_access`` row on an
active workspace; otherwise list/get/routes/delete all return
``{"success": False, ...}``. Mutations additionally require role
``owner``/``admin`` — a plain ``user`` member can read but not delete.

Note on the membership role: ``workspace_access`` has no ``member`` role
(valid values are ``owner``/``admin``/``user``/``guest`` — see
``models/workspace_access.py:ROLE_SELECTION``). The "general member, not
owner/admin" case is therefore modelled with ``role='user'``.
"""
import json
import uuid

from odoo.tests.common import HttpCase, tagged


def _new_user(env, login, name=None):
    """Create an internal user with password == login (HttpCase auth)."""
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestSecurityAccessEndpoints(HttpCase):
    """Cross-user negative：非 workspace member 打 endpoint 必須被拒。"""

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.alice = _new_user(self.env, f'sa_ep_alice_{suffix}')
        self.mallory = _new_user(self.env, f'sa_ep_mallory_{suffix}')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': f'SA EP WS {suffix}', 'owner_id': self.alice.id,
        })
        # workspace.create auto-seeds the owner access row; the guard below
        # keeps the fixture idempotent if that ever changes.
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.alice.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'EP Gateway', 'workspace_id': self.workspace.id,
        })

    def _rpc(self, url, params=None):
        response = self.url_open(
            url,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                             'params': params or {}}),
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()
        # Stash the raw body so callers can distinguish a structured
        # {success: False, error: ...} rejection from an endpoint crash
        # (JSON-RPC error → body has 'error', no 'result'). Without this
        # check a negative test would false-green if the gate started raising
        # a 500 instead of returning a structured denial.
        self._last_body = body
        return body.get('result') or {}

    def _assert_structured_denial(self, res):
        """Negative-path rejection must be a structured {success:False,error}
        envelope, NOT a JSON-RPC error (endpoint crash)."""
        self.assertNotIn('error', self._last_body,
                         "endpoint crashed (JSON-RPC error) instead of "
                         "returning a structured denial: %s"
                         % self._last_body.get('error'))
        self.assertFalse(res.get('success'))
        self.assertIn('error', res)

    def test_member_can_list(self):
        self.authenticate(self.alice.login, self.alice.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}/security-accesses',
            {'action': 'list'})
        self.assertTrue(res.get('success'))
        self.assertEqual(res.get('count'), 1)
        # to_dict() must never leak the (group_system-masked) tunnel_token in
        # the BFF JSON envelope — symmetric with the HA-API守線
        # (test_ha_api_security_access.py). Guards against a future
        # to_dict() that adds the field by construction.
        self.assertNotIn('tunnel_token', str(res))

    def test_member_get_omits_tunnel_token(self):
        """owner action='get' 的 to_dict() envelope 不得含 tunnel_token。"""
        self.authenticate(self.alice.login, self.alice.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'get'})
        self.assertTrue(res.get('success'))
        self.assertNotIn('tunnel_token', str(res))

    def test_non_member_list_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}/security-accesses',
            {'action': 'list'})
        self._assert_structured_denial(res)

    def test_non_member_get_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'get'})
        self._assert_structured_denial(res)

    def test_non_member_routes_list_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/routes',
            {'action': 'list'})
        self._assert_structured_denial(res)

    def test_non_member_delete_denied_and_record_survives(self):
        """寫路徑 negative：刪除被拒且 record 仍在（防注入面）。"""
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'delete'})
        self._assert_structured_denial(res)
        self.assertTrue(self.sa.exists())

    def test_member_but_not_admin_cannot_mutate(self):
        """一般 member（role='user'，非 owner/admin）可讀不可變更。

        workspace_access 無 'member' role；'user' 是 owner/admin 以下的一般
        成員級別（models/workspace_access.py:ROLE_USER）。
        """
        suffix = uuid.uuid4().hex[:6]
        carol = _new_user(self.env, f'sa_ep_carol_{suffix}')
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': carol.id, 'role': 'user',
        })
        self.authenticate(carol.login, carol.login)
        read = self._rpc(
            f'/api/workspaces/{self.workspace.id}/security-accesses',
            {'action': 'list'})
        self.assertTrue(read.get('success'))
        mutate = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'delete'})
        self.assertFalse(mutate.get('success'))

    # ---- subscription trio + routes write endpoints (sudo-cascade面) ----

    def test_non_member_change_plan_denied(self):
        """最高風險寫入面：非 member 打 /change-plan 被拒（HttpCase L3）。"""
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/change-plan',
            {'plan_id': 1})
        self._assert_structured_denial(res)

    def test_non_member_skip_trial_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/skip-trial', {})
        self._assert_structured_denial(res)

    def test_member_user_role_change_plan_denied(self):
        """role='user' 一般 member 打 /change-plan 應 Insufficient permissions。"""
        suffix = uuid.uuid4().hex[:6]
        dave = _new_user(self.env, f'sa_ep_dave_{suffix}')
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': dave.id, 'role': 'user',
        })
        self.authenticate(dave.login, dave.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/change-plan',
            {'plan_id': 1})
        self._assert_structured_denial(res)
        self.assertIn('permission', (res.get('error') or '').lower())

    def test_non_member_routes_create_denied_no_route(self):
        """非 member 打 /routes action=create 被拒且不產生 route。"""
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/routes',
            {'action': 'create', 'subdomain_prefix': 'ep-intruder-route',
             'service_url': 'http://localhost:80'})
        self._assert_structured_denial(res)
        self.assertFalse(self.env[
            'woow_paas_platform.security_access_route'].sudo().search(
                [('subdomain_prefix', '=', 'ep-intruder-route')]))

    def test_non_member_routes_delete_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/routes',
            {'action': 'delete', 'route_id': 999999})
        self._assert_structured_denial(res)
