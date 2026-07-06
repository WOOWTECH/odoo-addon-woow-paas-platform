"""TDD — #271 migration healer 18.0.1.0.43.

Unset sidecar_backed on rows whose url is NOT in-cluster (public regress /
legacy auto_created backfill). Preserve url + good in-cluster rows. Idempotent.
Pattern: test_mcp_project_decouple_migration_25.py.

Red expectation: src/migrations/18.0.1.0.43/post-migrate.py absent →
_load_migrate_function() returns None → _assert_migration_implemented fails.
"""
import importlib.util
import os

from odoo.tests.common import TransactionCase, tagged


_MIGRATION_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..', 'migrations', '18.0.1.0.43', 'post-migrate.py',
    )
)


def _load_migrate_function():
    if not os.path.exists(_MIGRATION_PATH):
        return None
    spec = importlib.util.spec_from_file_location(
        'mcp_sidecar_url_invariant_migration_18_0_1_0_43', _MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'migrate', None)


@tagged('post_install', '-at_install')
class TestMigration43SidecarUrlHealer(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.migrate = _load_migrate_function()
        # sidecar_backed column must exist (impl prerequisite).
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'woow_paas_platform_mcp_server' "
            "  AND column_name = 'sidecar_backed'"
        )
        if not self.env.cr.fetchone():
            self.fail("sidecar_backed column missing — Task 1 must land first.")

        self.workspace = self.Workspace.sudo().create({'name': 'WS Mig43'})
        self.template = self.Template.sudo().create({
            'name': 'Tmpl Mig43',
            'slug': 'tmpl-mig43',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-mig43',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = self.Service.sudo().create({
            'name': 'Svc Mig43',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'mig43-svc',
            'helm_namespace': 'paas-ws-mig43',
        })
        self.project = self.Project.sudo().create({
            'name': 'Bound Mig43 Project',
            'cloud_service_id': self.service.id,
        })

    def _assert_migration_implemented(self):
        self.assertIsNotNone(
            self.migrate,
            "Migration src/migrations/18.0.1.0.43/post-migrate.py is missing. "
            "Impl: delegate to mcp_server._heal_sidecar_backed_non_cluster_url().",
        )

    def _seed_row_via_sql(self, name, url, sidecar_backed):
        """Insert a user-scope row via raw SQL, bypassing the new
        @api.constrains so we can craft the pre-migration bad shape."""
        self.env.cr.execute(
            """
            INSERT INTO woow_paas_platform_mcp_server
                (name, url, transport, scope, project_id, active,
                 state, sidecar_backed,
                 create_uid, create_date, write_uid, write_date)
            VALUES
                (%s, %s, 'streamable_http', 'user', %s, TRUE,
                 'draft', %s, 1, NOW(), 1, NOW())
            RETURNING id
            """,
            (name, url, self.project.id, sidecar_backed),
        )
        return self.env.cr.fetchone()[0]

    def _read(self, row_id):
        self.env.cr.execute(
            "SELECT sidecar_backed, url FROM woow_paas_platform_mcp_server "
            "WHERE id = %s", (row_id,),
        )
        return self.env.cr.fetchone()

    def test_healer_unsets_public_sidecar_backed_row(self):
        self._assert_migration_implemented()
        bad_id = self._seed_row_via_sql(
            'Mig43 Bad', 'https://evil.example.com/mcp', True)
        good_id = self._seed_row_via_sql(
            'Mig43 Good',
            'http://mig43-svc-mcp.paas-ws-mig43.svc.cluster.local:3001/mcp',
            True)
        external_id = self._seed_row_via_sql(
            'Mig43 External', 'https://mcp.example.com/sse', False)

        self.migrate(self.env.cr, '18.0.1.0.42')

        bad_backed, bad_url = self._read(bad_id)
        self.assertFalse(
            bad_backed,
            "healer must unset sidecar_backed on the public-url row")
        self.assertEqual(
            bad_url, 'https://evil.example.com/mcp',
            "healer must preserve the url (data retained; only flag flips)")

        good_backed, _ = self._read(good_id)
        self.assertTrue(
            good_backed,
            "healer must NOT touch a valid in-cluster sidecar row")

        ext_backed, _ = self._read(external_id)
        self.assertFalse(
            ext_backed,
            "sidecar_backed=False row stays False (untouched)")

    def test_healer_unsets_https_in_cluster_row(self):
        # Residual the raw-SQL host-only LIKE would MISS but the constraint
        # rejects: host ends in .svc.cluster.local yet scheme is https (not
        # http). Row-by-row _is_in_cluster_url() catches it → healer stays in
        # lock-step with the constraint (no write-time explosion later).
        self._assert_migration_implemented()
        https_id = self._seed_row_via_sql(
            'Mig43 HttpsCluster',
            'https://mig43-svc-mcp.paas-ws-mig43.svc.cluster.local:3001/mcp',
            True)

        self.migrate(self.env.cr, '18.0.1.0.42')

        backed, url = self._read(https_id)
        self.assertFalse(
            backed,
            "healer must unset sidecar_backed on an https in-cluster row "
            "(scheme != http fails the invariant just like the constraint)")
        self.assertEqual(
            url,
            'https://mig43-svc-mcp.paas-ws-mig43.svc.cluster.local:3001/mcp',
            "url preserved")

    def test_healer_idempotent(self):
        self._assert_migration_implemented()
        bad_id = self._seed_row_via_sql(
            'Mig43 Idem', 'https://evil.example.com/mcp', True)

        self.migrate(self.env.cr, '18.0.1.0.42')
        first = self._read(bad_id)
        self.assertFalse(first[0])

        try:
            self.migrate(self.env.cr, '18.0.1.0.42')
        except Exception as exc:  # noqa: BLE001
            self.fail(f"second run raised {type(exc).__name__}: {exc}")
        self.assertEqual(first, self._read(bad_id), "state drifted on re-run")
