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
        self._assert_structured_denial(mutate)

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

    # ---- collection create dispatch role gate ----

    def test_member_user_role_create_dispatch_denied(self):
        """role='user' 成員打 collection action='create' 必須被結構化拒絕。

        Locks the role gate in api_security_accesses (create branch). SA
        建立是會開計費 tunnel 的入口，這個 gate 是一般 member 與『私自開
        billed tunnel』之間唯一防線；前面的測試只直接呼叫 helper，從未經過
        dispatch 本身的 role 檢查。
        """
        suffix = uuid.uuid4().hex[:6]
        eve = _new_user(self.env, f'sa_ep_eve_{suffix}')
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': eve.id, 'role': 'user',
        })
        self.authenticate(eve.login, eve.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}/security-accesses',
            {'action': 'create', 'name': 'Eve Gateway',
             'template_id': 1, 'plan_id': 1})
        self._assert_structured_denial(res)
        self.assertIn('permission', (res.get('error') or '').lower())
        # no SA created for the gated member
        self.assertFalse(self.env[
            'woow_paas_platform.security_access'].sudo().search(
                [('name', '=', 'Eve Gateway')]))

    def test_owner_create_dispatch_passes_role_gate(self):
        """owner 打 collection action='create' 通過 role gate（正向）。

        owner 不應被 role gate 擋；缺 template/plan 時的失敗必須是下游業務
        邏輯錯誤，而非 'Insufficient permissions'（確認 gate 已放行）。
        """
        self.authenticate(self.alice.login, self.alice.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}/security-accesses',
            {'action': 'create', 'name': 'Owner Created GW',
             'template_id': 999999, 'plan_id': 999999})
        # Guard against false-green: _rpc returns {} on a JSON-RPC error
        # (endpoint crash), which would silently satisfy the assertions below
        # (None→'' has no 'permission'/'template'). Require a structured body so
        # a future create path that crashes AFTER the gate is caught, not
        # mistaken for "gate passed".
        self.assertNotIn('error', self._last_body,
                         "endpoint crashed (JSON-RPC error) instead of "
                         "returning a structured result: %s"
                         % self._last_body.get('error'))
        # role gate cleared → not a permission denial …
        self.assertNotIn('permission', (res.get('error') or '').lower())
        # … and the failure IS the downstream template validation, positively
        # locking that the gate let us reach business logic.
        self.assertIn('template', (res.get('error') or '').lower())

    # ---- detail refresh_status / provision / update dispatch role gate ----

    def test_member_user_role_refresh_status_denied(self):
        """role='user' member 打 action='refresh_status' 須被結構化拒絕。

        refresh_status 在 tunnel_id 存在時會 sa.write(...)，SA write ir.rule
        只放行 owner/admin。把它放在 owner/admin gate 之後可避免對 member
        直接外洩 AccessError 內部細節（model/rule 技術名）。
        """
        suffix = uuid.uuid4().hex[:6]
        frank = _new_user(self.env, f'sa_ep_frank_{suffix}')
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': frank.id, 'role': 'user',
        })
        self.authenticate(frank.login, frank.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'refresh_status'})
        self._assert_structured_denial(res)
        self.assertIn('permission', (res.get('error') or '').lower())

    def test_member_user_role_provision_denied(self):
        """role='user' member 打 action='provision' 須被結構化拒絕。

        provision 開計費 tunnel；P1 安全收緊重點是避免一般 member 從 BFF
        觸發 provision。鎖住 detail dispatch 的 provision role gate。
        """
        suffix = uuid.uuid4().hex[:6]
        grace = _new_user(self.env, f'sa_ep_grace_{suffix}')
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': grace.id, 'role': 'user',
        })
        self.authenticate(grace.login, grace.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'provision'})
        self._assert_structured_denial(res)
        self.assertIn('permission', (res.get('error') or '').lower())

    def test_member_user_role_update_denied(self):
        """role='user' member 打 action='update' 須被結構化拒絕。"""
        suffix = uuid.uuid4().hex[:6]
        heidi = _new_user(self.env, f'sa_ep_heidi_{suffix}')
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': heidi.id, 'role': 'user',
        })
        self.authenticate(heidi.login, heidi.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'update', 'name': 'Renamed By Member'})
        self._assert_structured_denial(res)
        self.assertIn('permission', (res.get('error') or '').lower())
        # name unchanged
        self.assertNotEqual(self.sa.name, 'Renamed By Member')

    # ---- owner positive write paths (A/B 變體 half-coverage 補正向) ----

    def test_owner_detail_update_renames(self):
        """owner action='update' 改 name 必須真的寫進去（正向）。

        既有覆蓋只有 role='user' member 的 update-denied negative
        (test_member_user_role_update_denied)；缺 owner 正向，未來把 update
        改成靜默 no-op 不會被任何測試抓到。
        """
        self.authenticate(self.alice.login, self.alice.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'update', 'name': 'Renamed By Owner'})
        self.assertTrue(res.get('success'), res.get('error'))
        self.sa.invalidate_recordset(['name'])
        self.assertEqual(self.sa.name, 'Renamed By Owner')

    def test_owner_route_update_writes_service_url_and_is_protected(self):
        """owner /routes action='update' 改 service_url + is_protected 正向。

        _update_route 的 is_protected 寫入分支先前完全無測試；service_url 正向
        亦無。self.sa 未 provision（無 tunnel_id），route write 不觸發 operator
        sync，可純驗 ORM 寫入結果。
        """
        route = self.env[
            'woow_paas_platform.security_access_route'].sudo().create({
                'security_access_id': self.sa.id,
                'subdomain_prefix': 'ep-owner-update',
                'service_url': 'http://localhost:80',
                'is_protected': False,
            })
        self.authenticate(self.alice.login, self.alice.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/routes',
            {'action': 'update', 'route_id': route.id,
             'service_url': 'http://localhost:9000', 'is_protected': True})
        self.assertTrue(res.get('success'), res.get('error'))
        self.assertEqual(res['data']['service_url'], 'http://localhost:9000')
        self.assertTrue(res['data']['is_protected'])
        route.invalidate_recordset(['service_url', 'is_protected'])
        self.assertEqual(route.service_url, 'http://localhost:9000')
        self.assertTrue(route.is_protected)

    def test_route_check_endpoint_requires_prefix(self):
        """platform-level api_route_check：缺 prefix → 結構化 400 分支。

        只有 helper _check_route_prefix 被直接呼叫測到；endpoint dispatch 本身
        與 'prefix is required' 分支先前未被任何測試走到。
        """
        self.authenticate(self.alice.login, self.alice.login)
        res = self._rpc('/api/security-access/routes/check', {})
        self._assert_structured_denial(res)
        self.assertIn('prefix', (res.get('error') or '').lower())

    def test_owner_refresh_status_passes_role_gate(self):
        """owner 打 action='refresh_status'（無 tunnel_id）通過 role gate。

        self.sa 未 provision（tunnel_id 空），action_refresh_status 提早
        return 不打 operator，所以 owner 應拿到 success（確認 gate 放行且
        read-ish 行為對 owner 正常）。
        """
        self.authenticate(self.alice.login, self.alice.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}',
            {'action': 'refresh_status'})
        self.assertTrue(res.get('success'), res.get('error'))


@tagged('post_install', '-at_install')
class TestSecurityAccessAccessEndpoints(HttpCase):
    """Cross-user negatives for the P2 Access Control endpoints.

    Mirrors the route/detail negatives: a non-member打 access-config /
    access-policies must be rejected at the controller gate (workspace →
    role → _check_operation_allowed), and no policy row may be created nor
    any access field flipped as a side effect of the denied call.
    """

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:6]
        self.alice = _new_user(self.env, f'sa_ac_alice_{suffix}')
        self.mallory = _new_user(self.env, f'sa_ac_mallory_{suffix}')
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': f'SA AC WS {suffix}', 'owner_id': self.alice.id})
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.alice.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'AC EP GW', 'workspace_id': self.workspace.id})
        self.pol = self.env[
            'woow_paas_platform.security_access_access_policy'].sudo().create({
                'security_access_id': self.sa.id, 'name': 'p',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'a.com'})

    def _rpc(self, url, params=None):
        r = self.url_open(url, data=json.dumps({'jsonrpc': '2.0',
                          'method': 'call', 'params': params or {}}),
                          headers={'Content-Type': 'application/json'})
        return r.json().get('result') or {}

    def test_non_member_policy_list_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/access-policies',
            {'action': 'list'})
        self.assertFalse(res.get('success'))

    def test_non_member_policy_create_denied_and_none_made(self):
        before = self.env[
            'woow_paas_platform.security_access_access_policy'].sudo(
            ).search_count([('security_access_id', '=', self.sa.id)])
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/access-policies',
            {'action': 'create', 'name': 'inject', 'decision': 'allow',
             'rule_type': 'email_domain', 'rule_value': 'evil.com'})
        self.assertFalse(res.get('success'))
        after = self.env[
            'woow_paas_platform.security_access_access_policy'].sudo(
            ).search_count([('security_access_id', '=', self.sa.id)])
        self.assertEqual(after, before)

    def test_non_member_access_config_denied(self):
        self.authenticate(self.mallory.login, self.mallory.login)
        res = self._rpc(
            f'/api/workspaces/{self.workspace.id}'
            f'/security-accesses/{self.sa.id}/access-config',
            {'access_enabled': True})
        self.assertFalse(res.get('success'))
        self.assertFalse(self.sa.sudo().access_enabled)
