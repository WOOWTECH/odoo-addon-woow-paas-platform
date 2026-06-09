"""P2.5 migration (18.0.1.0.37): Service-wide Access → Per-route.

Simulates the legacy DB shape (orphan SA-level Access columns + a policy whose
route_id is NULL) and drives the migrate() function, asserting the per-route
re-homing + login/session copy + access_app_id clear + idempotency.

The legacy SA-level columns no longer exist in the P2.5 ORM, so the test adds
them back as raw orphan columns (exactly what Odoo leaves in a real upgraded DB)
before running the migration, then lets migrate() consume + clear them.
"""
from odoo.tests.common import TransactionCase


def _migrate_fn():
    # Import the version-dir module by file path (migration dirs are not Python
    # packages, so a normal import won't resolve "18.0.1.0.37").
    import importlib.util
    import os
    here = os.path.dirname(os.path.dirname(__file__))
    path = os.path.join(here, 'migrations', '18.0.1.0.37', 'post-migrate.py')
    spec = importlib.util.spec_from_file_location('p2_5_post_migrate', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate


class TestP2_5Migration(TransactionCase):
    _SA_TABLE = 'woow_paas_platform_security_access'
    _POL_TABLE = 'woow_paas_platform_security_access_access_policy'

    def setUp(self):
        super().setUp()
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'MIG WS'})
        self.migrate = _migrate_fn()

    def _make_route_id_nullable(self):
        """In a REAL upgrade Odoo adds the new required route_id column nullable
        and can't apply NOT NULL because legacy rows violate it (it logs +
        leaves it nullable), which is exactly the state this migration runs in.
        A fresh test DB has the full NOT NULL constraint, so drop it to recreate
        the upgrade shape, then null the legacy policy's route_id."""
        self.env.cr.execute(
            "ALTER TABLE %s ALTER COLUMN route_id DROP NOT NULL" % self._POL_TABLE)

    def _null_policy_route(self, pol, sa):
        self.env.cr.execute(
            "UPDATE %s SET route_id = NULL, security_access_id = %%s "
            "WHERE id = %%s" % self._POL_TABLE, (sa.id, pol.id))

    def _add_legacy_columns(self):
        """Re-create the orphan SA-level Access columns the upgraded DB carries."""
        self.env.cr.execute(
            "ALTER TABLE %s "
            "ADD COLUMN IF NOT EXISTS access_enabled boolean, "
            "ADD COLUMN IF NOT EXISTS login_google boolean, "
            "ADD COLUMN IF NOT EXISTS login_email_otp boolean, "
            "ADD COLUMN IF NOT EXISTS session_duration varchar, "
            "ADD COLUMN IF NOT EXISTS access_app_id varchar" % self._SA_TABLE)

    def _drop_legacy_columns(self):
        self.env.cr.execute(
            "ALTER TABLE %s "
            "DROP COLUMN IF EXISTS access_enabled, "
            "DROP COLUMN IF EXISTS login_google, "
            "DROP COLUMN IF EXISTS login_email_otp, "
            "DROP COLUMN IF EXISTS session_duration, "
            "DROP COLUMN IF EXISTS access_app_id" % self._SA_TABLE)

    def _set_legacy(self, sa, **vals):
        cols = ', '.join("%s = %%s" % k for k in vals)
        self.env.cr.execute(
            "UPDATE %s SET %s WHERE id = %%s" % (self._SA_TABLE, cols),
            list(vals.values()) + [sa.id])

    def test_migration_moves_sa_access_to_protected_route(self):
        """Legacy SA-level login/session + a NULL-route policy → re-homed onto
        the SA's protected route; SA access_app_id cleared."""
        self._add_legacy_columns()
        self._make_route_id_nullable()
        try:
            sa = self.SA.create({'name': 'MIG GW',
                                 'workspace_id': self.workspace.id})
            primary = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': True})
            # A policy created on the route, then forced to look legacy
            # (route_id NULL) via raw SQL — the real upgraded shape.
            pol = self.Policy.create({
                'route_id': primary.id, 'name': 'legacy-pol',
                'decision': 'allow', 'rule_type': 'email_domain',
                'rule_value': 'a.com'})
            self._null_policy_route(pol, sa)
            # Seed legacy SA-level Access values.
            self._set_legacy(sa, login_google=True, login_email_otp=False,
                             session_duration='8h', access_app_id='legacy-app',
                             access_enabled=True)
            self.SA.invalidate_model()
            self.Route.invalidate_model()
            self.Policy.invalidate_model()

            self.migrate(self.env.cr, '18.0.1.0.37')

            # policy re-homed onto the protected primary route
            pol.invalidate_recordset()
            self.assertEqual(pol.route_id, primary,
                             "legacy policy must be re-homed onto the route")
            self.assertEqual(pol.security_access_id, sa)
            # login/session copied onto the protected route
            primary.invalidate_recordset()
            self.assertTrue(primary.login_google)
            self.assertFalse(primary.login_email_otp)
            self.assertEqual(primary.session_duration, '8h')
            # SA-level access_app_id cleared
            self.env.cr.execute(
                "SELECT access_app_id FROM %s WHERE id = %%s" % self._SA_TABLE,
                (sa.id,))
            self.assertIsNone(self.env.cr.fetchone()[0])
        finally:
            self._drop_legacy_columns()

    def test_migration_idempotent(self):
        """Re-running finds no NULL-route_id legacy policy and no orphan
        access_app_id → no further change."""
        self._add_legacy_columns()
        self._make_route_id_nullable()
        try:
            sa = self.SA.create({'name': 'MIG GW2',
                                 'workspace_id': self.workspace.id})
            primary = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': True})
            pol = self.Policy.create({
                'route_id': primary.id, 'name': 'p', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'a.com'})
            self._null_policy_route(pol, sa)
            self._set_legacy(sa, session_duration='1h',
                             access_app_id='legacy-app-2')
            self.SA.invalidate_model()
            self.Policy.invalidate_model()
            self.migrate(self.env.cr, '18.0.1.0.37')
            pol.invalidate_recordset()
            self.assertEqual(pol.route_id, primary)
            route_after = pol.route_id
            # second pass: no-op
            self.migrate(self.env.cr, '18.0.1.0.37')
            pol.invalidate_recordset()
            self.assertEqual(pol.route_id, route_after,
                             "idempotent: route_id unchanged on re-run")
        finally:
            self._drop_legacy_columns()

    def test_migration_multi_protected_routes_copies_policy(self):
        """An SA with 2 protected routes → the legacy shared policy is copied to
        each (behaviour-equivalent: old single app's policy set applied to all
        its destinations)."""
        self._add_legacy_columns()
        self._make_route_id_nullable()
        try:
            sa = self.SA.create({'name': 'MIG GW3',
                                 'workspace_id': self.workspace.id})
            r1 = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': True})
            r2 = self.Route.create({
                'security_access_id': sa.id, 'subdomain_prefix': 'mig-r2',
                'is_protected': True})
            pol = self.Policy.create({
                'route_id': r1.id, 'name': 'shared', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'a.com'})
            self._null_policy_route(pol, sa)
            self._set_legacy(sa, session_duration='24h')
            self.SA.invalidate_model()
            self.Policy.invalidate_model()
            self.migrate(self.env.cr, '18.0.1.0.37')
            # each protected route ends with a copy of the policy
            r1_pols = self.Policy.search([('route_id', '=', r1.id)])
            r2_pols = self.Policy.search([('route_id', '=', r2.id)])
            self.assertEqual(len(r1_pols), 1)
            self.assertEqual(len(r2_pols), 1)
            self.assertEqual(r1_pols.name, 'shared')
            self.assertEqual(r2_pols.name, 'shared')
        finally:
            self._drop_legacy_columns()

    def test_migration_fan_out_bypasses_policy_quota(self):
        """fan-out copy() 不可撞 _check_policy_quota → 不中斷 migration。

        quota 是 per-SA 總量，為 OLD 單一共享 app 模型設計（policy set 算一次）。
        把同一 set fan-out 到 M 條 route 會在同預算下乘 M 倍，N×M 可能 > quota。
        migration 是 behaviour-equivalent 的 re-home，不是使用者新增配額，所以
        fan-out 必須跳過 quota 檢查，否則「多 protected route + 近 quota」的 stg
        測試資料會在升級途中觸發 ValidationError 中斷整個 migrate()。

        本測試用真實 subscription（plan quota=1）+ 1 legacy policy × 2 protected
        route：fan-out 後總量 2 > 1。修前 copy() 觸發 @api.constrains 拋
        ValidationError → migrate() abort（紅）；修後 migration 內跳過 quota。
        """
        self._add_legacy_columns()
        self._make_route_id_nullable()
        try:
            sa = self.SA.create({'name': 'MIG GW Quota',
                                 'workspace_id': self.workspace.id})
            # 真實 subscription，policy quota=1（per-SA 總量）。route quota 給足
            # （sa_max_routes=5）以免第二條 route 撞到無關的 route 配額，本測試
            # 只針對 policy quota 的 fan-out bypass。
            plan = self.env['sh.subscription.plan'].create({
                'name': 'MIG Plan', 'sh_duration': 1, 'sh_unit': 'month',
                'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
                'sa_max_routes': 5, 'sa_max_access_policies': 1})
            sub = self.env['sh.subscription.subscription'].create({
                'sh_partner_id': self.env.user.partner_id.id,
                'state': 'in_progress'})
            sub.sh_subscription_plan_id = plan.id
            sa.subscription_id = sub.id
            r1 = self.Route.create({
                'security_access_id': sa.id,
                'subdomain_prefix': sa.canonical_name,
                'is_primary': True, 'is_protected': True})
            r2 = self.Route.create({
                'security_access_id': sa.id, 'subdomain_prefix': 'mig-q-r2',
                'is_protected': True})
            pol = self.Policy.create({
                'route_id': r1.id, 'name': 'shared-q', 'decision': 'allow',
                'rule_type': 'email_domain', 'rule_value': 'a.com'})
            self._null_policy_route(pol, sa)
            self._set_legacy(sa, session_duration='24h')
            self.SA.invalidate_model()
            self.Policy.invalidate_model()
            # 修前：fan-out 第 2 筆 copy 觸發 quota（2 > 1）→ ValidationError abort
            self.migrate(self.env.cr, '18.0.1.0.37')
            r1_pols = self.Policy.search([('route_id', '=', r1.id)])
            r2_pols = self.Policy.search([('route_id', '=', r2.id)])
            self.assertEqual(len(r1_pols), 1)
            self.assertEqual(len(r2_pols), 1,
                             "fan-out must land on both routes despite quota")
            self.assertEqual(r2_pols.name, 'shared-q')
        finally:
            self._drop_legacy_columns()
