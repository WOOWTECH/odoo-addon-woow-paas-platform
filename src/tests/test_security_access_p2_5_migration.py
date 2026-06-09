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
