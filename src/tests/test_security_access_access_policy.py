from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase

MOCK_GET_CLIENT = (
    'odoo.addons.woow_paas_platform.models.security_access.'
    'get_paas_operator_client'
)


class TestAccessPolicyModel(TransactionCase):
    """access_policy 子模型 + 主模型 access 欄位."""

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Policy = self.env['woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AP WS'})

    def _make_sa(self):
        return self.SA.create({'name': 'AP GW', 'workspace_id': self.workspace.id})

    def test_main_model_access_fields_default(self):
        sa = self._make_sa()
        self.assertFalse(sa.access_enabled)
        self.assertFalse(sa.login_google)
        self.assertFalse(sa.login_email_otp)
        self.assertEqual(sa.session_duration, '24h')

    def test_create_policy(self):
        sa = self._make_sa()
        pol = self.Policy.create({
            'security_access_id': sa.id, 'name': 'corp',
            'decision': 'allow', 'rule_type': 'email_domain',
            'rule_value': 'acme.com', 'sequence': 1})
        self.assertIn(pol, sa.access_policy_ids)

    def test_policy_rule_value_validation(self):
        sa = self._make_sa()
        # email_domain 不可含 @
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'bad',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': '@acme.com'})
        # ip_range 必須是 CIDR
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'bad2',
                'decision': 'allow', 'rule_type': 'ip_range',
                'rule_value': 'not-an-ip'})

    def test_policy_quota_enforced(self):
        sa = self._make_sa()
        plan = self.env['sh.subscription.plan'].create({
            'name': 'AP Plan', 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'sa_max_access_policies': 1})
        sub = self.env['sh.subscription.subscription'].create({
            'sh_partner_id': self.env.user.partner_id.id, 'state': 'in_progress'})
        sub.sh_subscription_plan_id = plan.id
        sa.subscription_id = sub.id
        self.Policy.create({
            'security_access_id': sa.id, 'name': 'p1', 'decision': 'allow',
            'rule_type': 'email_domain', 'rule_value': 'a.com'})
        with self.assertRaises(ValidationError):
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'p2', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'b.com'})


class TestAccessSync(TransactionCase):
    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AS WS'})

    def _active_sa(self, mock_client):
        with patch(MOCK_GET_CLIENT, return_value=mock_client):
            sa = self.SA.create({'name': 'AS GW',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-as'})
            self.Route.create({'security_access_id': sa.id,
                               'subdomain_prefix': sa.canonical_name,
                               'is_primary': True, 'is_protected': True})
        return sa

    def _mock(self):
        m = MagicMock()
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {
            'app_id': 'app-1', 'policy_ids': ['pol-1'],
            'allowed_idps': ['idp-g', 'idp-otp']}
        return m

    def test_enable_access_syncs_app(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True, 'login_google': True,
                      'login_email_otp': True, 'session_duration': '8h'})
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
        payload = mc.sync_access_app.call_args.args[0]
        self.assertTrue(payload['enabled'])
        self.assertEqual(sorted(payload['login_idp_types']),
                         ['google', 'onetimepin'])
        self.assertEqual(payload['session_duration'], '8h')
        # 只有 is_protected 的 route 進 protected_hostnames
        self.assertEqual(payload['protected_hostnames'],
                         [f"{sa.canonical_name}.{sa._get_paas_domain()}"])
        self.assertEqual(payload['policies'][0]['rule_value'], 'acme.com')
        # app_id / policy_ids 寫回
        self.assertEqual(sa.access_app_id, 'app-1')

    def test_protected_route_toggle_resyncs(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            mc.sync_access_app.reset_mock()
            r2 = self.Route.create({'security_access_id': sa.id,
                                    'subdomain_prefix': 'as-extra',
                                    'is_protected': False})
            # 改 is_protected 觸發 access re-sync
            r2.write({'is_protected': True})
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        hosts = payload['protected_hostnames']
        self.assertIn(f"as-extra.{sa._get_paas_domain()}", hosts)

    def test_disable_access_tears_down(self):
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            sa.write({'access_app_id': 'app-1'})
            mc.sync_access_app.reset_mock()
            sa.write({'access_enabled': False})
        payload = mc.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-1')

    def test_policy_create_sync_failure_rolls_back_db_row(self):
        """create 的 Access re-sync 失敗 → 整筆 policy 不落 DB（無幽靈 policy），
        與 route 子模型 savepoint 對齊。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            mc.sync_access_app.side_effect = PaaSOperatorError('cf down')
            with self.assertRaises(PaaSOperatorError):
                self.Policy.create({
                    'security_access_id': sa.id, 'name': 'ghost-pol',
                    'decision': 'allow', 'rule_type': 'email_domain',
                    'rule_value': 'ghost.com'})
        self.assertFalse(self.Policy.search(
            [('name', '=', 'ghost-pol')]))

    def test_policy_write_sync_failure_rolls_back_decision(self):
        """write 的 Access re-sync 失敗 → decision 回滾，DB 與 CF 不分歧。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            pol = self.Policy.create({
                'security_access_id': sa.id, 'name': 'flip-pol',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'flip.com'})
            mc.sync_access_app.side_effect = PaaSOperatorError('cf down')
            with self.assertRaises(PaaSOperatorError):
                pol.write({'decision': 'deny'})
        self.assertEqual(pol.decision, 'allow')

    def test_policy_unlink_sync_failure_rolls_back_keeps_row(self):
        """unlink 的 Access re-sync 失敗 → DB row 保留（下次 idempotent
        re-sync 可收斂），與 route 子模型最嚴重方向對齊。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import (
            PaaSOperatorError,
        )
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            pol = self.Policy.create({
                'security_access_id': sa.id, 'name': 'doomed-pol',
                'decision': 'deny', 'rule_type': 'email_domain',
                'rule_value': 'doomed.com'})
            pol_id = pol.id
            mc.sync_access_app.side_effect = PaaSOperatorError('cf down')
            with self.assertRaises(PaaSOperatorError):
                pol.unlink()
        self.assertTrue(self.Policy.browse(pol_id).exists())

    def test_enable_writeback_maps_cf_policy_id_onto_row(self):
        """zip writeback branch: the returned ['pol-1'] must land on the policy
        row's cf_policy_id (the next idempotent re-sync sends it as
        existing_policy_ids to delete the stale CF policy). If the zip order
        were swapped or the mapping deleted, this assertion fails."""
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            pol = self.Policy.create({
                'security_access_id': sa.id, 'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
        # mock returns policy_ids=['pol-1'] → written back onto the single row.
        self.assertEqual(pol.cf_policy_id, 'pol-1')

    def test_disable_writeback_clears_app_and_policy_cf_ids(self):
        """Teardown writeback branch (app_id=None): disabling must clear
        access_app_id to False AND wipe every policy's stale cf_policy_id, so a
        later resync doesn't try to delete already-gone CF policies. Drives the
        `if not result.get('app_id')` branch the prior tests never exercised
        (their mock always returned app_id='app-1')."""
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            pol = self.Policy.create({
                'security_access_id': sa.id, 'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
            self.assertEqual(pol.cf_policy_id, 'pol-1')
            self.assertEqual(sa.access_app_id, 'app-1')
            # Now flip the mock to a real teardown shape (app_id=None) and
            # disable: the writeback must clear both the app id and the stale
            # policy cf id.
            mc.sync_access_app.return_value = {
                'app_id': None, 'policy_ids': [], 'allowed_idps': []}
            sa.write({'access_enabled': False})
        self.assertFalse(sa.access_app_id)
        self.assertFalse(pol.cf_policy_id)

    def test_error_state_with_live_tunnel_still_syncs_access(self):
        """F-MODEL-2: access-sync liveness gate must match route-sync —
        ('active','error') + tunnel. An SA stuck in 'error' with a live tunnel
        (stage-2 provision failure) must still push Access config, else CF
        destinations go stale vs the protected route set with no converge
        trigger. Pre-fix this was gated on state=='active' only and would skip."""
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            # Drop into 'error' but keep the live tunnel (stage-2 failure shape).
            sa.write({'state': 'error'})
            mc.sync_access_app.reset_mock()
            # An access config edit in 'error' state must still reach operator.
            sa.write({'session_duration': '8h'})
            config_synced = mc.sync_access_app.called
            mc.sync_access_app.reset_mock()
            # And an is_protected route toggle in 'error' state too.
            r2 = self.Route.create({'security_access_id': sa.id,
                                    'subdomain_prefix': 'err-extra',
                                    'is_protected': False})
            r2.write({'is_protected': True})
            route_toggle_synced = mc.sync_access_app.called
        self.assertTrue(config_synced,
                        "access config edit in 'error' state must sync")
        self.assertTrue(route_toggle_synced,
                        "is_protected toggle in 'error' state must sync access")

    def test_error_state_policy_crud_still_syncs_access(self):
        """F-MODEL-2 (policy sub-model leg): the policy create/write/unlink
        liveness gate must match the SA/route gate — ('active','error') +
        tunnel. An SA stuck in 'error' with a live tunnel (stage-2 provision
        failure) must still push Access on a policy mutation, else CF's
        reusable allow/deny set diverges from the DB with no converge trigger
        (a pure policy edit doesn't touch the SA fields that fire the SA-level
        re-sync). Pre-fix the policy gate was state=='active' only and would
        silently skip every CF push here while the endpoint reported success."""
        mc = self._mock()
        sa = self._active_sa(mc)
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa.write({'access_enabled': True})
            # Drop into 'error' but keep the live tunnel (stage-2 failure shape).
            sa.write({'state': 'error'})
            # create in 'error' must reach operator.
            mc.sync_access_app.reset_mock()
            pol = self.Policy.create({
                'security_access_id': sa.id, 'name': 'err-pol',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'err.com'})
            create_synced = mc.sync_access_app.called
            # write in 'error' must reach operator.
            mc.sync_access_app.reset_mock()
            pol.write({'decision': 'deny'})
            write_synced = mc.sync_access_app.called
            # unlink in 'error' must reach operator.
            mc.sync_access_app.reset_mock()
            pol.unlink()
            unlink_synced = mc.sync_access_app.called
        self.assertTrue(create_synced,
                        "policy create in 'error' state must sync access")
        self.assertTrue(write_synced,
                        "policy write in 'error' state must sync access")
        self.assertTrue(unlink_synced,
                        "policy unlink in 'error' state must sync access")


class TestProvisionPushesAccess(TransactionCase):
    """F-MODEL-1: configure-then-provision must push Access at provision time.

    A user can enable Access + add policies on a 'pending' SA (the BFF gates on
    subscription state, not provisioning state). Those writes never reach the
    operator (no live tunnel yet). action_provision / _do_resume must therefore
    re-sync Access once the tunnel exists, else the SA lands active-but-inert:
    access_enabled=True with no CF Access app, silently, until a field re-toggle.
    """

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'PPA WS'})

    def _mock(self):
        m = MagicMock()
        m.create_tunnel.return_value = {
            'tunnel_id': 'tun-ppa', 'tunnel_token': 'tok',
            'tunnel_name': 'paas-sa-x-y', 'dns_record_id': 'rec-1'}
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {
            'app_id': 'app-ppa', 'policy_ids': ['pol-ppa'], 'allowed_idps': []}
        return m

    def test_first_provision_pushes_preconfigured_access(self):
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'PPA GW',
                                  'workspace_id': self.workspace.id})
            # Configure Access while still pending (no tunnel) — must NOT sync.
            sa.write({'access_enabled': True, 'login_google': True})
            self.Policy.create({
                'security_access_id': sa.id, 'name': 'corp', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'acme.com'})
            self.assertFalse(mc.sync_access_app.called,
                             "pending SA must not push Access prematurely")
            # Now provision: tunnel comes up → Access must be pushed.
            sa.action_provision()
        self.assertEqual(sa.state, 'active')
        self.assertTrue(mc.sync_access_app.called,
                        "provision must push pre-configured Access")
        payload = mc.sync_access_app.call_args.args[0]
        self.assertTrue(payload['enabled'])
        self.assertEqual(sa.access_app_id, 'app-ppa')

    def test_provision_without_access_enabled_does_not_sync_access(self):
        """Negative: access_enabled=False at provision → no Access push (the
        gate must be access_enabled, not unconditional)."""
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'PPA GW2',
                                  'workspace_id': self.workspace.id})
            sa.action_provision()
        self.assertEqual(sa.state, 'active')
        self.assertFalse(mc.sync_access_app.called)


class TestAccessLifecycle(TransactionCase):
    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AL WS'})

    def _mock(self):
        m = MagicMock()
        m.sync_tunnel_routes.return_value = {'routes': []}
        m.sync_access_app.return_value = {'app_id': None, 'policy_ids': [],
                                          'allowed_idps': []}
        return m

    def test_cleanup_tears_down_access(self):
        mc = self._mock()
        with patch(MOCK_GET_CLIENT, return_value=mc):
            sa = self.SA.create({'name': 'AL GW',
                                 'workspace_id': self.workspace.id})
            sa.write({'state': 'active', 'tunnel_id': 'tun-al',
                      'access_enabled': True})
            self.Route.create({'security_access_id': sa.id,
                               'subdomain_prefix': sa.canonical_name,
                               'is_primary': True})
            # Set access_app_id LAST: both the access_enabled write and the
            # route create above fire _sync_access_to_operator, whose mock
            # returns app_id=None and writes it back (= the real operator's
            # teardown shape). Writing access_app_id alone does NOT re-trigger
            # sync (not in the write() trigger keys), so it survives to
            # _do_cleanup — mirroring an SA that already has a live CF app.
            sa.write({'access_app_id': 'app-al'})
            mc.sync_access_app.reset_mock()
            sa._do_cleanup()
        # cleanup 應呼叫 access teardown（enabled=False payload）
        self.assertTrue(mc.sync_access_app.called)
        payload = mc.sync_access_app.call_args.args[0]
        self.assertFalse(payload['enabled'])
        self.assertEqual(payload['existing_app_id'], 'app-al')
        # _cleanup_cloudflare must also CLEAR the stored app id once the
        # teardown succeeded — otherwise the DB keeps a stale CF app id that a
        # later resync would PUT-update against a deleted app. The mock didn't
        # raise, so access_app_id must be False now.
        self.assertFalse(sa.access_app_id)


class TestAccessPolicyAcl(TransactionCase):
    def setUp(self):
        super().setUp()
        User = self.env['res.users']
        self.alice = User.create({
            'name': 'AP Alice', 'login': 'ap_acl_alice',
            'email': 'ap_acl_alice@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.mallory = User.create({
            'name': 'AP Mallory', 'login': 'ap_acl_mallory',
            'email': 'ap_acl_mallory@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'AP ACL WS', 'owner_id': self.alice.id})
        Access = self.env['woow_paas_platform.workspace_access']
        if not Access.search([('workspace_id', '=', self.ws.id),
                              ('user_id', '=', self.alice.id)]):
            Access.create({'workspace_id': self.ws.id,
                           'user_id': self.alice.id, 'role': 'owner'})
        self.sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'AP ACL GW', 'workspace_id': self.ws.id})
        self.pol = self.env[
            'woow_paas_platform.security_access_access_policy'].create({
                'security_access_id': self.sa.id, 'name': 'p',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'a.com'})

    def test_member_can_read_policy(self):
        v = self.env['woow_paas_platform.security_access_access_policy'
                     ].with_user(self.alice).search([('id', '=', self.pol.id)])
        self.assertEqual(len(v), 1)

    def test_non_member_cannot_read_policy(self):
        v = self.env['woow_paas_platform.security_access_access_policy'
                     ].with_user(self.mallory).search([('id', '=', self.pol.id)])
        self.assertEqual(len(v), 0)

    def _add_user_member(self):
        """role='user' 一般 member（非 owner/admin），測 read/write 拆分。"""
        carol = self.env['res.users'].create({
            'name': 'AP Carol', 'login': 'ap_acl_carol',
            'email': 'ap_acl_carol@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.ws.id, 'user_id': carol.id, 'role': 'user'})
        return carol

    def test_user_role_member_can_read_but_not_write_policy(self):
        """ir.rule read/write 拆分（與 SA/Route 對齊）：role='user' member
        可讀 policy，但 ORM write 必須被 ir.rule 擋——access policy 是 CF Access
        allow/deny 認證閘，比 route 更敏感，不可讓一般 member 直接改 decision。"""
        from odoo.exceptions import AccessError
        carol = self._add_user_member()
        # read OK（member read rule 對所有 member 開放）
        visible = self.env[
            'woow_paas_platform.security_access_access_policy'].with_user(
                carol).search([('id', '=', self.pol.id)])
        self.assertEqual(len(visible), 1)
        # write DENY（manage rule 只給 owner/admin）—— 例如把 deny 改 allow
        with self.assertRaises(AccessError):
            self.pol.with_user(carol).write({'decision': 'deny'})

    def test_user_role_member_cannot_create_or_unlink_policy(self):
        """role='user' member 不得 ORM-create 新 allow policy（開放受保護
        hostname）或 unlink 既有 deny policy，繞過 owner/admin role gate。"""
        from odoo.exceptions import AccessError
        carol = self._add_user_member()
        with self.assertRaises(AccessError):
            self.env[
                'woow_paas_platform.security_access_access_policy'].with_user(
                    carol).create({
                        'security_access_id': self.sa.id, 'name': 'evil',
                        'decision': 'allow', 'rule_type': 'email_domain',
                        'rule_value': 'attacker.com'})
        with self.assertRaises(AccessError):
            self.pol.with_user(carol).unlink()

    def test_owner_can_mutate_policy(self):
        """正向：owner（alice）可 create/write/unlink policy（不被 manage rule 誤擋）。
        sa 為 pending 無 tunnel → 不觸發 operator sync（由
        test_pending_sa_policy_mutation_does_not_sync 直接 assert_not_called 鎖）。"""
        new_pol = self.env[
            'woow_paas_platform.security_access_access_policy'].with_user(
                self.alice).create({
                    'security_access_id': self.sa.id, 'name': 'owner-pol',
                    'decision': 'allow', 'rule_type': 'email_domain',
                    'rule_value': 'owner.com'})
        self.assertTrue(new_pol.exists())
        new_pol.with_user(self.alice).write({'decision': 'deny'})
        self.assertEqual(new_pol.decision, 'deny')
        new_pol.with_user(self.alice).unlink()
        self.assertFalse(new_pol.exists())

    def test_pending_sa_policy_mutation_does_not_sync(self):
        """F-TEST-SKIPGUARD: the non-active/no-tunnel skip-sync guard
        (security_access_access_policy.py create/write/unlink: access_enabled +
        state=='active' + tunnel_id) must be directly asserted. self.sa is
        pending with no tunnel even when access_enabled — a regression dropping
        the liveness guard would call the operator. Patch the client and assert
        sync_access_app was NEVER called across create/write/unlink."""
        from unittest.mock import MagicMock, patch as _patch
        mc = MagicMock()
        # access_enabled=True so ONLY the state/tunnel legs of the guard hold
        # the line; this isolates exactly the liveness guard under test.
        self.sa.write({'access_enabled': True})
        with _patch(MOCK_GET_CLIENT, return_value=mc):
            pol = self.env[
                'woow_paas_platform.security_access_access_policy'].create({
                    'security_access_id': self.sa.id, 'name': 'pend-pol',
                    'decision': 'allow', 'rule_type': 'email_domain',
                    'rule_value': 'pend.com'})
            pol.write({'decision': 'deny'})
            pol.unlink()
        mc.sync_access_app.assert_not_called()
        # main-model access-config write on a pending SA must also skip sync.
        with _patch(MOCK_GET_CLIENT, return_value=mc):
            self.sa.write({'session_duration': '8h'})
        mc.sync_access_app.assert_not_called()
