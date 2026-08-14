"""TDD red phase — Migration 18.0.1.0.25 (auto_created → sidecar_backed).

Plan: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
  - AC #3: Migration ``src/migrations/18.0.1.0.25/post-migrate.py`` 把
    既有 ``auto_created=True`` row 設成 ``sidecar_backed=True`` (SQL
    UPDATE), idempotent.
  - AC #35: Migration 順序 — (1) backfill, (2) drop or keep auto_created
    per implementation choice, (3) preserve PR #47 archived orphan row
    (``active=False`` 持續 archive；不主動 unarchive，不主動 delete).
  - AC #36: Migration **idempotent** — 重跑 ``-u woow_paas_platform`` 不
    raise / 不重複 backfill / 不破壞 archived row.

Test pattern: 仿 PR #47 ``test_mcp_project_decouple_migration.py`` 的
``importlib.util.spec_from_file_location`` + raw SQL fixture, isolated
file per migration version (與 .24 分離 per leader OQ-C).

Red expectation today (pre-implementation, 2026-05-20):

* 所有 3 tests RED：``src/migrations/18.0.1.0.25/post-migrate.py`` 尚未
  存在 → ``_load_migrate_function`` 回 None → ``_assert_migration_
  implemented`` 失敗 with clean
  ``AssertionError: Migration file ... is missing``.
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
        '18.0.1.0.25',
        'post-migrate.py',
    )
)


def _load_migrate_function():
    """Dynamically load ``migrate(cr, version)`` from the .25 migration.

    Returns ``None`` if file doesn't exist yet (canonical red signal).
    """
    if not os.path.exists(_MIGRATION_PATH):
        return None
    spec = importlib.util.spec_from_file_location(
        'mcp_sidecar_backed_migration_18_0_1_0_25', _MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'migrate', None)


@tagged('post_install', '-at_install')
class TestMigration25SidecarBackedBackfill(TransactionCase):
    """G group — Plan AC #3, #35.4, #36."""

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

        # If implementation chose option (a) drop `auto_created` column,
        # post-`-u` the column is gone from prod schema. Our fixture
        # restores it so we can exercise the backfill branch — this is
        # the same idiom PR #47 used for `cloud_service_id` restoration
        # (test_mcp_project_decouple_migration.py:200-204). Leader
        # tester ruling for D3=A: fixture-side schema restore is
        # tester-only territory.
        self.env.cr.execute(
            "ALTER TABLE woow_paas_platform_mcp_server "
            "ADD COLUMN IF NOT EXISTS auto_created BOOLEAN DEFAULT FALSE"
        )
        # Ensure sidecar_backed column also exists (impl prerequisite —
        # plan AC #2). If field declaration is still missing we surface
        # via setUp fail-fast.
        self.env.cr.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'woow_paas_platform_mcp_server' "
            "  AND column_name = 'sidecar_backed'"
        )
        if not self.env.cr.fetchone():
            self.fail(
                "Fixture-side schema guard: sidecar_backed column missing "
                "on woow_paas_platform_mcp_server. Plan AC #2 — new field "
                "declaration must land before migration test can exercise "
                "backfill. Implementation should add field then re-run "
                "`-u` to pull in schema."
            )

        # Build a minimal valid mcp_server seed environment (template /
        # service / project chain to avoid FK failures on `project_id`
        # column).
        self.workspace = self.Workspace.sudo().create({
            'name': 'WS Mig25',
        })
        self.template = self.Template.sudo().create({
            'name': 'Tmpl Mig25',
            'slug': 'tmpl-mig25',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-mig25',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = _internal_provision(self.Service.sudo()).create({
            'name': 'Svc Mig25',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'mig25-svc',
            'helm_namespace': 'paas-ws-mig25',
        })
        self.bound_project = self.Project.sudo().create({
            'name': 'Bound Mig25 Project',
            'cloud_service_id': self.service.id,
        })

    def _assert_migration_implemented(self):
        self.assertIsNotNone(
            self.migrate,
            "Migration file src/migrations/18.0.1.0.25/post-migrate.py "
            "is missing. Plan §Migration path — implementation phase "
            "must create it with: (1) backfill `UPDATE ... SET "
            "sidecar_backed = TRUE WHERE auto_created = TRUE`, (2) "
            "optional DROP COLUMN auto_created (impl choice a), (3) "
            "preserve PR #47 archived orphan row state."
        )

    def _seed_user_scope_row_via_sql(
        self,
        name,
        auto_created,
        active=True,
        sidecar_backed=False,
        project_id=None,
    ):
        """Insert a user-scope mcp_server via raw SQL, bypassing ORM
        constraints (so we can craft pre-migration row shapes).

        Returns the new row's id.
        """
        if project_id is None:
            project_id = self.bound_project.id
        # `scope='user'` + `project_id` row to mirror PR #47 post-state.
        self.env.cr.execute(
            """
            INSERT INTO woow_paas_platform_mcp_server
                (name, url, transport, scope, project_id, active,
                 state, auto_created, sidecar_backed,
                 create_uid, create_date, write_uid, write_date)
            VALUES
                (%s, %s, 'streamable_http', 'user', %s, %s,
                 'draft', %s, %s, 1, NOW(), 1, NOW())
            RETURNING id
            """,
            (
                name, 'http://seed/mcp', project_id, active,
                auto_created, sidecar_backed,
            ),
        )
        return self.env.cr.fetchone()[0]

    # ------------------------------------------------------------------ #
    # AC #3: backfill sidecar_backed from auto_created                    #
    # ------------------------------------------------------------------ #
    def test_migration_backfills_sidecar_backed_from_auto_created(self):
        """AC #3: ``UPDATE SET sidecar_backed = TRUE WHERE auto_created
        = TRUE``.

        **Red today**：migration file absent → fail-fast via
        ``_assert_migration_implemented``. After impl: backfill propagates
        auto_created→sidecar_backed for the seeded row.
        """
        self._assert_migration_implemented()

        # Two seeds: one auto_created=True (expect backfill), one
        # auto_created=False (must remain sidecar_backed=False — no
        # over-eager backfill).
        backfill_id = self._seed_user_scope_row_via_sql(
            name='Mig25 Backfill Target',
            auto_created=True,
            sidecar_backed=False,
        )
        external_id = self._seed_user_scope_row_via_sql(
            name='Mig25 External (auto_created=False)',
            auto_created=False,
            sidecar_backed=False,
        )

        self.migrate(self.env.cr, '18.0.1.0.24')

        # Read back via SQL to be robust to ORM state.
        self.env.cr.execute(
            "SELECT id, sidecar_backed FROM woow_paas_platform_mcp_server "
            "WHERE id = ANY(%s) ORDER BY id",
            ([backfill_id, external_id],),
        )
        rows = {row[0]: row[1] for row in self.env.cr.fetchall()}
        self.assertEqual(
            rows.get(backfill_id), True,
            "AC #3: auto_created=True row not backfilled to "
            "sidecar_backed=True. Migration UPDATE step missing or "
            "scoped wrong.",
        )
        self.assertEqual(
            rows.get(external_id), False,
            "AC #3 boundary: auto_created=False row was wrongly "
            "backfilled to sidecar_backed=True. UPDATE WHERE auto_created "
            "= TRUE must be scoped — non-auto_created rows are "
            "external user MCP, must stay sidecar_backed=False.",
        )

    # ------------------------------------------------------------------ #
    # AC #36: idempotent on second run                                    #
    # ------------------------------------------------------------------ #
    def test_migration_idempotent(self):
        """AC #36: running migration twice must not raise / not duplicate.

        Particularly important if implementation drops auto_created
        column (option a) — second run hits a schema where the source
        column for backfill is gone. Migration must guard with
        ``IF EXISTS`` / similar.

        **Red today**：migration file absent → first run fails
        ``_assert_migration_implemented``. Post-impl: second run is a
        no-op.
        """
        self._assert_migration_implemented()

        target_id = self._seed_user_scope_row_via_sql(
            name='Mig25 Idempotent',
            auto_created=True,
            sidecar_backed=False,
        )

        # First run.
        self.migrate(self.env.cr, '18.0.1.0.24')

        self.env.cr.execute(
            "SELECT sidecar_backed, active FROM "
            "woow_paas_platform_mcp_server WHERE id = %s",
            (target_id,),
        )
        first_state = self.env.cr.fetchone()
        self.assertEqual(
            first_state[0], True,
            "AC #36 prerequisite: first run must backfill — fixture "
            "verification before idempotent re-run.",
        )

        # Second run — must not raise.
        try:
            self.migrate(self.env.cr, '18.0.1.0.24')
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"AC #36: second migration run raised "
                f"{type(exc).__name__}: {exc}. Migration must be "
                f"idempotent — option (a) drop_column path must use "
                f"`DROP COLUMN IF EXISTS`; backfill UPDATE must guard "
                f"against `auto_created` column absent."
            )

        self.env.cr.execute(
            "SELECT sidecar_backed, active FROM "
            "woow_paas_platform_mcp_server WHERE id = %s",
            (target_id,),
        )
        second_state = self.env.cr.fetchone()
        self.assertEqual(
            first_state, second_state,
            "AC #36: row state drifted between two idempotent runs. "
            "Expected stable (sidecar_backed=True, active=unchanged).",
        )

    # ------------------------------------------------------------------ #
    # AC #35.4: PR #47 archived orphan row preserved                      #
    # ------------------------------------------------------------------ #
    def test_migration_preserves_pr47_archived_orphan(self):
        """AC #35.4: ``auto_created=True, active=False, project_id=NULL``
        rows (PR #47 archived orphans) must survive .25 migration
        unchanged.

        Plan §AC #35 fourth bullet: 「**保留**現狀（active=False 持續
        archive；不主動 unarchive，不主動 delete）」.

        Backfill SQL ``WHERE auto_created = TRUE`` would touch these
        rows (and set sidecar_backed=True); active flag must NOT be
        changed. The row stays archived as PR #47 rollback safety net.

        **Red today**：migration file absent → fail-fast.
        """
        self._assert_migration_implemented()

        # Seed an archived orphan (no project_id — bypass ORM via raw
        # SQL since the test fixture cannot create scope='user' +
        # project_id=NULL via ORM).
        self.env.cr.execute(
            """
            INSERT INTO woow_paas_platform_mcp_server
                (name, url, transport, scope, project_id, active,
                 state, auto_created, sidecar_backed,
                 create_uid, create_date, write_uid, write_date)
            VALUES
                ('PR47 Archived Orphan', 'http://orphan/mcp',
                 'streamable_http', 'user', NULL, FALSE,
                 'draft', TRUE, FALSE, 1, NOW(), 1, NOW())
            RETURNING id
            """,
        )
        orphan_id = self.env.cr.fetchone()[0]

        self.migrate(self.env.cr, '18.0.1.0.24')

        self.env.cr.execute(
            "SELECT active, sidecar_backed, project_id "
            "FROM woow_paas_platform_mcp_server WHERE id = %s",
            (orphan_id,),
        )
        result = self.env.cr.fetchone()
        self.assertIsNotNone(
            result,
            "AC #35.4: archived orphan row was deleted by migration. "
            "Plan: 「保留現狀 ... 不主動 delete」.",
        )
        active, sidecar_backed_val, project_id = result
        self.assertFalse(
            active,
            "AC #35.4: archived orphan row was unarchived by migration. "
            "Plan: 「active=False 持續 archive ... 不主動 unarchive」.",
        )
        # sidecar_backed=True is acceptable side effect of backfill (this
        # row had auto_created=True), but active flag must remain False.
        # We accept either True or False on sidecar_backed since plan
        # doesn't constrain (UPDATE ... SET sidecar_backed = auto_created
        # would set True here; UPDATE ... WHERE active=True would not).
        self.assertIsNone(
            project_id,
            "AC #35.4: archived orphan project_id flipped from NULL. "
            "Plan: migration must not modify project_id on archived "
            "rows.",
        )
