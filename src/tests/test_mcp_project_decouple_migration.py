"""TDD red phase tests — Migration 18.0.1.0.24 for MCP cloud_service_id → project_id.

Plan: docs/refactor/plan-mcp-project-decouple.md (D1/D2/D3 — schema + migration)
Decision: docs/refactor/decisions/mcp-project-decouple.md §Invariants 1, 2

These tests verify the four AC blocks that the new migration must satisfy:

* AC #1/#2/#5 — post-state schema: ``project_id`` column exists,
  ``cloud_service_id`` column dropped, ``project.project.mcp_server_ids``
  inverse exists, ``cloud_service.user_mcp_server_ids`` removed.
* AC #3 — migration backfills ``project_id`` for user-scope rows that had
  a ``cloud_service_id`` matching a ``project.project.cloud_service_id``.
* AC #4 — migration is idempotent (second run is a noop).
* D2/Resolved Q4 — orphan rows (user-scope MCP with no matching project)
  are **archived** (``active=False``) **not** unlinked; the row stays in DB
  as a rollback safety net.
* AC #3 + system-scope untouched — system-scope rows (no ``cloud_service_id``,
  no ``project_id`` after migration) are untouched, ``active`` remains True.

Red expectation today (2026-05-18, pre-implementation):

1. ``src/migrations/18.0.1.0.24/post-migrate.py`` does not yet exist →
   ``_load_migrate_function()`` returns ``None`` → ``assertIsNotNone``
   raises ``AssertionError`` (clean red).
2. ``project_id`` column does not yet exist on ``woow_paas_platform_mcp_server``
   → ``information_schema`` lookup is empty → ``assertTrue(...)`` raises
   ``AssertionError``.
3. ``cloud_service_id`` column still exists → ``assertIsNone(...)`` on
   the lookup result raises ``AssertionError``.
4. ``cloud_service.user_mcp_server_ids`` field still present → ``assertNotIn``
   raises ``AssertionError``.
5. ``project.project`` lacks ``mcp_server_ids`` inverse field → ``assertIn``
   raises ``AssertionError``.

After the implementation lands, every test transitions to green with the same
fixtures (no test edits required).
"""
import importlib.util
import os

from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _internal_provision


_MIGRATION_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..',
        'migrations',
        '18.0.1.0.24',
        'post-migrate.py',
    )
)


def _load_migrate_function():
    """Dynamically load the migration's ``migrate(cr, version)`` callable.

    Migration directories use dot-separated names (``18.0.1.0.24``) which
    are not valid Python module identifiers, so we cannot use a plain
    ``import``.  ``importlib.util.spec_from_file_location`` works around
    this for tests.

    Returns ``None`` if the migration file does not exist yet (this is
    the expected red state before implementation).
    """
    if not os.path.exists(_MIGRATION_PATH):
        return None
    spec = importlib.util.spec_from_file_location(
        'mcp_project_decouple_migration_18_0_1_0_24', _MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'migrate', None)


@tagged('post_install', '-at_install')
class TestMcpProjectDecoupleMigrationSchema(TransactionCase):
    """Schema-state assertions: AC #1, #2, #5.

    These are independent of migration runtime — they verify the final
    schema after ``-u woow_paas_platform`` has executed the new migration.
    """

    def test_ac1_mcp_server_has_project_id_field(self):
        """AC #1: mcp_server has a project_id M2O field (ondelete='cascade')."""
        fields_def = self.env['woow_paas_platform.mcp_server']._fields
        self.assertIn(
            'project_id', fields_def,
            "AC #1: mcp_server.project_id field missing — refactor not "
            "yet implemented. Plan: drop cloud_service_id and add "
            "project_id M2O to project.project with ondelete='cascade'.",
        )
        # Invariant #1: ondelete must be 'cascade' (not set null / restrict).
        project_id_field = fields_def['project_id']
        self.assertEqual(
            project_id_field.comodel_name, 'project.project',
            "AC #1: project_id must target project.project model",
        )
        self.assertEqual(
            project_id_field.ondelete, 'cascade',
            "AC #1 / Invariant #1: project_id.ondelete must be 'cascade' "
            "(see decisions/mcp-project-decouple.md §Invariants 1)",
        )

    def test_ac1_mcp_server_no_cloud_service_id_field(self):
        """AC #1: mcp_server.cloud_service_id removed after refactor."""
        fields_def = self.env['woow_paas_platform.mcp_server']._fields
        self.assertNotIn(
            'cloud_service_id', fields_def,
            "AC #1: mcp_server.cloud_service_id still present — must be "
            "dropped by 18.0.1.0.24 in favour of project_id.",
        )

    def test_ac2_project_project_has_mcp_server_ids_inverse(self):
        """AC #2: project.project has mcp_server_ids reverse O2M."""
        fields_def = self.env['project.project']._fields
        self.assertIn(
            'mcp_server_ids', fields_def,
            "AC #2: project.project.mcp_server_ids inverse missing — "
            "AI chat consumption chain (channel→task→project→mcp_servers) "
            "depends on this inverse.",
        )
        inverse_field = fields_def['mcp_server_ids']
        self.assertEqual(
            inverse_field.comodel_name, 'woow_paas_platform.mcp_server',
            "AC #2: mcp_server_ids must target woow_paas_platform.mcp_server",
        )
        self.assertEqual(
            inverse_field.inverse_name, 'project_id',
            "AC #2: mcp_server_ids inverse must be project_id (not cloud_service_id)",
        )

    def test_ac2_cloud_service_no_user_mcp_server_ids(self):
        """AC #2: cloud_service.user_mcp_server_ids reverse O2M removed."""
        fields_def = self.env['woow_paas_platform.cloud_service']._fields
        self.assertNotIn(
            'user_mcp_server_ids', fields_def,
            "AC #2: cloud_service.user_mcp_server_ids must be removed — "
            "consumption now walks via project.mcp_server_ids.",
        )

    def test_ac5_cloud_service_id_column_dropped(self):
        """AC #5: cloud_service_id column physically dropped from mcp_server table."""
        self.cr.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'woow_paas_platform_mcp_server'
              AND column_name = 'cloud_service_id'
        """)
        row = self.cr.fetchone()
        self.assertIsNone(
            row,
            "AC #5: woow_paas_platform_mcp_server.cloud_service_id column "
            "must be dropped by migration 18.0.1.0.24. The column still "
            "exists — backfill SQL or DROP COLUMN SQL not yet implemented.",
        )

    def test_ac1_project_id_column_exists(self):
        """AC #1 (physical): project_id column exists in mcp_server table."""
        self.cr.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'woow_paas_platform_mcp_server'
              AND column_name = 'project_id'
        """)
        self.assertTrue(
            self.cr.fetchone(),
            "AC #1: woow_paas_platform_mcp_server.project_id column "
            "missing — Odoo schema sync has not added the new field "
            "(refactor not implemented).",
        )


@tagged('post_install', '-at_install')
class TestMcpProjectDecoupleMigrationBehavior(TransactionCase):
    """Migration runtime behaviour: AC #3, #4, orphan archive, system untouched.

    All four cases share the same prologue:

    1. Load ``migrate`` via ``importlib.util`` (returns ``None`` pre-impl).
    2. Assert it exists — failure here is the canonical red signal that the
       migration file has not been written yet.
    3. Construct an isolated scenario via raw SQL (bypassing the new
       ``@api.constrains`` so we can simulate the pre-migration state on
       a partially migrated DB).
    4. Call ``migrate(self.cr, '18.0.1.0.23')`` and assert post-state.
    """

    def setUp(self):
        super().setUp()
        self.migrate = _load_migrate_function()
        # Restore legacy ``cloud_service_id`` column so the migration can
        # be exercised in a state matching prod-upgrade (column-present
        # when migrate() runs, column-dropped when migrate() finishes).
        # Implementation's post-migrate.py guards the orphan-archive
        # branch on this column's presence; without restoring it the
        # behaviour suite degenerates to the second-run idempotent path
        # and never tests the real backfill/archive logic.
        # Leader ruling 2026-05-18 (D3=A "one-shot drop") authorises
        # this fixture-side restore — tester fixture only, no production
        # code touched.
        self.env.cr.execute(
            "ALTER TABLE woow_paas_platform_mcp_server "
            "ADD COLUMN IF NOT EXISTS cloud_service_id INTEGER"
        )
        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']
        Project = self.env['project.project']

        self.workspace = Workspace.sudo().create({'name': 'WS MCP Migration'})
        self.template = Template.sudo().create({
            'name': 'Tmpl MCP Migration',
            'slug': 'tmpl-mcp-mig',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-mcp-mig',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = _internal_provision(Service.sudo()).create({
            'name': 'Svc MCP Migration',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'mig-svc',
            'helm_namespace': 'paas-ws-mig',
        })
        self.bound_project = Project.sudo().create({
            'name': 'Bound Migration Project',
            'cloud_service_id': self.service.id,
        })

    def _assert_migration_implemented(self):
        """Fail fast with a clean AssertionError if the migration file is missing."""
        self.assertIsNotNone(
            self.migrate,
            "Migration file src/migrations/18.0.1.0.24/post-migrate.py "
            "is missing. Implementation phase must create it with "
            "backfill SQL + orphan archive + DROP COLUMN + ir_model_data "
            "noupdate flip. See plan §Migration path.",
        )

    def _seed_user_scope_row_via_sql(
        self, name='Mig User MCP', cloud_service_id=None, project_id=None,
        active=True,
    ):
        """Insert a user-scope mcp_server row via raw SQL.

        Bypasses ``@api.constrains`` so we can simulate the pre-migration
        state (where rows may have ``cloud_service_id`` and no
        ``project_id``) or any half-migrated mix the migration must handle.

        Returns the new row's id.
        """
        # Build INSERT dynamically — both columns may or may not exist
        # depending on schema state, so introspect first.
        self.cr.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'woow_paas_platform_mcp_server'
              AND column_name IN ('cloud_service_id', 'project_id')
        """)
        existing_cols = {row[0] for row in self.cr.fetchall()}

        cols = ['name', 'url', 'transport', 'scope', 'active', 'state',
                'create_uid', 'create_date', 'write_uid', 'write_date']
        vals = [name, 'http://placeholder/mcp', 'streamable_http', 'user',
                active, 'draft', 1, 'NOW()', 1, 'NOW()']

        if 'cloud_service_id' in existing_cols:
            cols.append('cloud_service_id')
            vals.append(cloud_service_id)
        if 'project_id' in existing_cols:
            cols.append('project_id')
            vals.append(project_id)

        placeholders = ', '.join(
            'NOW()' if v == 'NOW()' else '%s' for v in vals
        )
        # SQL identifiers in cols are safe (whitelisted).
        col_sql = ', '.join(cols)
        # Filter out the literal 'NOW()' values from the placeholder bind list.
        params = tuple(v for v in vals if v != 'NOW()')
        self.cr.execute(
            f"INSERT INTO woow_paas_platform_mcp_server ({col_sql}) "
            f"VALUES ({placeholders}) RETURNING id",
            params,
        )
        return self.cr.fetchone()[0]

    def test_ac3_migrate_backfill_user_scope_mcp(self):
        """AC #3: migration backfills project_id from cloud_service_id.

        Setup mimics a row created against the pre-refactor schema that
        has a ``cloud_service_id`` pointing to a service whose
        ``project.project`` is known.  After migration, the row should
        gain ``project_id`` equal to the bound project, and the
        ``cloud_service_id`` column should be physically gone.
        """
        self._assert_migration_implemented()

        # Seed a pre-migration row: has cloud_service_id, missing project_id
        row_id = self._seed_user_scope_row_via_sql(
            name='Backfill Target',
            cloud_service_id=self.service.id,
            project_id=None,
        )

        # Run the migration
        self.migrate(self.cr, '18.0.1.0.23')

        # Assert post-state via raw SQL (works regardless of ORM field state).
        self.cr.execute(
            "SELECT project_id FROM woow_paas_platform_mcp_server "
            "WHERE id = %s",
            (row_id,),
        )
        result = self.cr.fetchone()
        self.assertIsNotNone(
            result,
            "AC #3: seeded row vanished — migration must not unlink "
            "rows whose cloud_service_id is mapped to a known project.",
        )
        self.assertEqual(
            result[0], self.bound_project.id,
            f"AC #3: project_id not backfilled to bound project "
            f"({self.bound_project.id}); got {result[0]}.",
        )

    def test_ac4_migrate_idempotent(self):
        """AC #4: running migration twice is a noop on the second pass.

        Re-running ``-u woow_paas_platform`` is a common operation
        (e.g. test runner, manual upgrade); the migration must not
        raise on the second call and must not duplicate rows or re-
        activate archived orphans.
        """
        self._assert_migration_implemented()

        # First seed an orphan row (user-scope, no project mapping).
        # Setup a service that has no support project to make this row orphan.
        Service = self.env['woow_paas_platform.cloud_service']
        orphan_service = _internal_provision(Service.sudo()).create({
            'name': 'Orphan Svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        orphan_id = self._seed_user_scope_row_via_sql(
            name='Orphan Row',
            cloud_service_id=orphan_service.id,
            project_id=None,
        )

        # First migration run
        self.migrate(self.cr, '18.0.1.0.23')

        # Snapshot post-first-run state
        self.cr.execute("""
            SELECT id, active FROM woow_paas_platform_mcp_server
            WHERE id = %s
        """, (orphan_id,))
        first_state = self.cr.fetchone()
        self.assertIsNotNone(
            first_state,
            "AC #4 / Resolved Q4: orphan row should still exist after first "
            "migration (archived, not unlinked).",
        )
        self.assertFalse(
            first_state[1],
            "AC #4: orphan row should be archived (active=False) after "
            "first migration.",
        )

        # Second migration run — should be a noop
        try:
            self.migrate(self.cr, '18.0.1.0.23')
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"AC #4: second migration run raised {type(exc).__name__}: "
                f"{exc}. Migration must be idempotent."
            )

        # Post-second-run: orphan must remain archived (not re-activated)
        self.cr.execute("""
            SELECT id, active FROM woow_paas_platform_mcp_server
            WHERE id = %s
        """, (orphan_id,))
        second_state = self.cr.fetchone()
        self.assertEqual(
            first_state, second_state,
            "AC #4: second migration changed orphan row state — "
            "expected idempotent noop, got drift.",
        )

    def test_ac3_migrate_orphan_user_scope_archive(self):
        """Resolved Q4: orphan user-scope rows are archived (active=False), not unlinked.

        An "orphan" is a user-scope mcp_server row whose ``cloud_service_id``
        references a service that has no support project (so backfill
        cannot find a project to point to).  Migration is irreversible —
        archive keeps the row as a rollback safety net while
        ``active=False`` removes it from ai chat / sync paths.
        """
        self._assert_migration_implemented()

        # Seed a service WITHOUT a support project (so backfill fails to find a project).
        Service = self.env['woow_paas_platform.cloud_service']
        orphan_service = _internal_provision(Service.sudo()).create({
            'name': 'Orphan Svc 2',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        orphan_id = self._seed_user_scope_row_via_sql(
            name='Truly Orphan',
            cloud_service_id=orphan_service.id,
            project_id=None,
        )

        self.migrate(self.cr, '18.0.1.0.23')

        # Row still exists (NOT unlinked — rollback safety net)
        self.cr.execute("""
            SELECT id, active, project_id FROM woow_paas_platform_mcp_server
            WHERE id = %s
        """, (orphan_id,))
        result = self.cr.fetchone()
        self.assertIsNotNone(
            result,
            "Resolved Q4: orphan row must NOT be unlinked; migration is "
            "irreversible so the row stays in DB as rollback safety net.",
        )
        # active=False (archived)
        self.assertFalse(
            result[1],
            "Resolved Q4: orphan row must be archived (active=False) so "
            "AI chat / sync paths don't pick it up.",
        )
        # project_id still NULL (backfill failed gracefully)
        self.assertIsNone(
            result[2],
            "Resolved Q4: orphan row must have project_id=NULL; "
            "migration must not falsely link to an unrelated project.",
        )

    def test_ac3_migrate_system_scope_untouched(self):
        """AC #3: system-scope rows (cloud_service_id IS NULL) are untouched.

        System-scope mcp_server rows have no ``cloud_service_id`` (the
        legacy field was scoped to user MCP) and must not gain a
        ``project_id`` — they remain platform-level resources.  This
        guards against an over-eager backfill that would falsely link
        system rows to any project.
        """
        self._assert_migration_implemented()

        # Seed a system-scope row (no cloud_service_id, no project_id)
        # via raw SQL.  Use scope='system' directly so the row matches the
        # platform-MCP shape.
        self.cr.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'woow_paas_platform_mcp_server'
              AND column_name IN ('cloud_service_id', 'project_id')
        """)
        existing_cols = {row[0] for row in self.cr.fetchall()}

        cols = ['name', 'url', 'transport', 'scope', 'active', 'state',
                'create_uid', 'create_date', 'write_uid', 'write_date']
        vals = ['System MCP Mig', 'http://sys/mcp', 'streamable_http',
                'system', True, 'draft', 1, 'NOW()', 1, 'NOW()']
        for c in ('cloud_service_id', 'project_id'):
            if c in existing_cols:
                cols.append(c)
                vals.append(None)

        placeholders = ', '.join(
            'NOW()' if v == 'NOW()' else '%s' for v in vals
        )
        col_sql = ', '.join(cols)
        params = tuple(v for v in vals if v != 'NOW()')
        self.cr.execute(
            f"INSERT INTO woow_paas_platform_mcp_server ({col_sql}) "
            f"VALUES ({placeholders}) RETURNING id",
            params,
        )
        sys_id = self.cr.fetchone()[0]

        self.migrate(self.cr, '18.0.1.0.23')

        self.cr.execute("""
            SELECT scope, active, project_id
            FROM woow_paas_platform_mcp_server WHERE id = %s
        """, (sys_id,))
        result = self.cr.fetchone()
        self.assertIsNotNone(result,
            "AC #3: system-scope row vanished after migration "
            "(migration must not touch scope='system' rows).",
        )
        self.assertEqual(result[0], 'system',
            "AC #3: system row scope changed by migration — must remain 'system'.",
        )
        self.assertTrue(result[1],
            "AC #3: system row was archived by migration — only "
            "user-scope orphans should be archived.",
        )
        self.assertIsNone(result[2],
            "AC #3: system row gained project_id from backfill — "
            "system-scope MCP must remain project_id=NULL per D4 invariant "
            "(scope='system' iff project_id IS NULL).",
        )
