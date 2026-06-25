"""TDD red phase — Phase A: migration 18.0.1.0.26 (project M2O → M2M + bootstraps).

Plan: docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase A Tasks 5, 6
Design: docs/plans/2026-Q2/agent-runtime-pod-design.md
  - §7.0.2 6-step dual-write/backfill migration
  - §7.0.7 Additive step 7-10 (new models / external_mcp_allowed default /
    project_access bootstrap)

Acceptance criteria covered:

M.1  ``src/migrations/18.0.1.0.26/pre-migrate.py`` exists with
     ``migrate(cr, version)`` and CREATEs ``project_cloud_service_rel``.
M.2  ``src/migrations/18.0.1.0.26/post-migrate.py`` exists with
     ``migrate(cr, version)``.
M.3  Step 2 backfill: INSERTs (project_id, cloud_service_id) rows from
     existing ``project.cloud_service_id`` 1:1 bindings; idempotent via
     ON CONFLICT DO NOTHING.
M.4  Step 3 drop UNIQUE constraint: ``project_project_unique_cloud_service``
     (or whatever the existing constraint name is) removed.
M.5  Step 4 deprecation: ``project_project.cloud_service_id`` column
     stays (NOT dropped — backward-compat read).
M.6  Step 8 backfill: ``external_mcp_allowed`` defaults to FALSE for
     all existing rows (NULL → FALSE).
M.7  Step 9 project_access bootstrap: existing projects get an
     ``role='owner'`` project_access row derived from
     ``cloud_service.workspace_id.access_ids`` owner partner (or the
     project ``create_uid`` partner if unbound).
M.8  Migration is idempotent — re-running the migrate() function on an
     already-migrated DB produces the same final state.
M.9  ``__manifest__.py`` version bumps from ``18.0.1.0.25`` to
     ``18.0.1.0.26`` so Odoo picks up the new migration directory.

Red expectation today (Phase A implementation has NOT started):

* M.1, M.2 → RED: ``src/migrations/18.0.1.0.26/`` directory does not
  yet exist; ``_load_migrate_function('pre' / 'post')`` returns None.
* M.3 through M.8 → RED via fail-fast in ``_assert_migration_present``.
* M.9 → RED: manifest still ``18.0.1.0.25``.

Pattern note: Migration tests follow the existing idiom in
``test_mcp_project_decouple_migration_25.py`` — dynamically load the
migration module file via ``importlib.util.spec_from_file_location`` and
exercise its ``migrate(cr, version)`` against a fixture-prepared DB.
This isolates the test from the Odoo ``-u`` flow and lets us assert
post-conditions deterministically.

Boundary（刻意不測）:

* Step 1 CREATE TABLE — covered indirectly when Step 2 INSERT succeeds
  (FK target must exist).
* Step 5 (workspace_id deprecation strategy) — left flexible in design
  (§7.0.3); not asserted to allow implementation latitude (keep related
  field vs replace with computed). Test_agent_pod_phase_a_project_schema
  R.A.2 already asserts workspace_ids is the new canonical store.
* Step 6 (cloud_service.project_ids M2M reverse) — covered by
  ``test_agent_pod_phase_a_cloud_service_mn.py``.
* Step 7 (CREATE TABLE for new models) — Odoo handles via model
  registration; covered by ``test_agent_pod_phase_a_new_models.py``.
* Step 10 (default ai.assistant fallback preserved) — Phase F territory
  (Phase A leaves the config_parameter untouched; not asserted).
"""
import importlib.util
import os
import re
import uuid

from odoo.tests.common import TransactionCase, tagged


_MIGRATION_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        '..',
        'migrations',
        '18.0.1.0.26',
    )
)


def _load_migrate_function(kind):
    """Load ``migrate(cr, version)`` from pre/post-migrate.py.

    Returns ``None`` if file missing — canonical red signal.
    ``kind`` is 'pre' or 'post'.
    """
    path = os.path.join(_MIGRATION_DIR, f'{kind}-migrate.py')
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(
        f'agent_pod_phase_a_{kind}_migrate_18_0_1_0_26', path,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'migrate', None)


@tagged('post_install', '-at_install')
class TestPhaseAMigrationFilesExist(TransactionCase):
    """M.1, M.2, M.9 — file presence + manifest version bump."""

    def test_pre_migrate_file_exists(self):
        """M.1: pre-migrate.py with migrate(cr, version)."""
        migrate = _load_migrate_function('pre')
        self.assertIsNotNone(
            migrate,
            "M.1: src/migrations/18.0.1.0.26/pre-migrate.py missing or "
            "doesn't expose migrate(cr, version). Plan §Phase A Task 5 "
            "— SQL CREATE for project_cloud_service_rel; design §7.0.2 "
            "Step 1.",
        )

    def test_post_migrate_file_exists(self):
        """M.2: post-migrate.py with migrate(cr, version)."""
        migrate = _load_migrate_function('post')
        self.assertIsNotNone(
            migrate,
            "M.2: src/migrations/18.0.1.0.26/post-migrate.py missing or "
            "doesn't expose migrate(cr, version). Plan §Phase A Task 6 "
            "— Steps 2/3/4/8/9 (backfill, drop UNIQUE, deprecation, "
            "external_mcp_allowed default, project_access bootstrap).",
        )

    def test_manifest_version_bumped(self):
        """M.9: Phase A migration dir stays on Odoo's upgrade path + the
        manifest version does not regress below it.

        Real intent (NOT "manifest == 18.0.1.0.26 forever"): the
        ``migrations/18.0.1.0.26/`` directory must still exist so Odoo's
        upgrade path picks it up, and ``__manifest__.py`` must declare a
        version **>= 18.0.1.0.26**. Later phases legitimately bump it higher
        (Phase C → 18.0.1.0.27 per plan §Phase C Task 13, impl ec226b7), so
        a hard-coded ``== 26`` would make this Phase A test and every later
        phase's version bump mutually exclusive (test-vs-test red). The
        monotonic check keeps the original guarantee — version must not fall
        below the value that activates the Phase A migration — without
        pinning it. (Version-agnostic fix signed off, same model as Cycle 1
        retire of test_ac2.)
        """
        # (a) Phase A migration directory still present on the upgrade path.
        self.assertTrue(
            os.path.isdir(_MIGRATION_DIR),
            "M.9: src/migrations/18.0.1.0.26/ directory missing — the Phase "
            "A migration would be dropped from Odoo's upgrade path.",
        )

        # (b) manifest version >= 18.0.1.0.26 (monotonic, not pinned).
        manifest_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), '..', '__manifest__.py',
            )
        )
        with open(manifest_path, 'r', encoding='utf-8') as f:
            content = f.read()
        match = re.search(
            r'["\']version["\']\s*:\s*["\']([0-9]+(?:\.[0-9]+)*)["\']',
            content,
        )
        self.assertIsNotNone(
            match,
            "M.9: __manifest__.py has no parseable version string.",
        )
        version_tuple = tuple(int(p) for p in match.group(1).split('.'))
        self.assertGreaterEqual(
            version_tuple, (18, 0, 1, 0, 26),
            "M.9: __manifest__.py version %s must be >= 18.0.1.0.26 so the "
            "migrations/18.0.1.0.26/ directory is honoured by the Odoo "
            "loader. Later phases may bump higher (Phase C → 18.0.1.0.27). "
            "Plan §Phase A Task 19." % match.group(1),
        )


@tagged('post_install', '-at_install')
class TestPhaseAMigrationBehaviour(TransactionCase):
    """M.3 ~ M.8 — migration behavioural assertions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.pre_migrate = _load_migrate_function('pre')
        self.post_migrate = _load_migrate_function('post')

        self.ws = self.Workspace.sudo().create({'name': f'M WS {self.suffix}'})
        self.tmpl = self.Template.sudo().create({
            'name': f'M tmpl {self.suffix}',
            'slug': f'm-tmpl-{self.suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'm-tmpl-{self.suffix}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc = self.Service.sudo().create({
            'name': f'M svc {self.suffix}',
            'workspace_id': self.ws.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'm-svc-{self.suffix}',
            'helm_namespace': f'paas-ws-m-{self.suffix}',
        })

    def _assert_migration_present(self):
        if self.pre_migrate is None:
            self.fail(
                "Migration prerequisite: 18.0.1.0.26/pre-migrate.py "
                "missing — see M.1."
            )
        if self.post_migrate is None:
            self.fail(
                "Migration prerequisite: 18.0.1.0.26/post-migrate.py "
                "missing — see M.2."
            )

    # ------------------------------------------------------------------ #
    # M.3 — Step 2 backfill                                               #
    # ------------------------------------------------------------------ #
    def test_step2_backfill_existing_1to1_binding(self):
        """M.3: existing ``project.cloud_service_id`` 1:1 binding shows
        up in ``project_cloud_service_rel`` after migrate().

        Test pattern: seed a project on the old M2O column via raw SQL
        (mimicking a pre-Phase-A DB state), run migration, then assert
        the relation table row exists.
        """
        self._assert_migration_present()

        # Seed: project on old M2O column.
        project = self.Project.sudo().create({
            'name': f'M.3 backfill src {self.suffix}',
            'cloud_service_id': self.svc.id,
        })

        # Drop relation table to simulate pre-state (idempotent).
        self.env.cr.execute(
            "DROP TABLE IF EXISTS project_cloud_service_rel"
        )

        self.pre_migrate(self.env.cr, '18.0.1.0.25')
        self.post_migrate(self.env.cr, '18.0.1.0.25')

        self.env.cr.execute(
            "SELECT 1 FROM project_cloud_service_rel "
            "WHERE project_id = %s AND cloud_service_id = %s",
            (project.id, self.svc.id),
        )
        row = self.env.cr.fetchone()
        self.assertIsNotNone(
            row,
            "M.3: existing 1:1 binding must be backfilled to "
            "project_cloud_service_rel. Plan §Phase A — Step 2.",
        )

    # ------------------------------------------------------------------ #
    # M.4 — Step 3 drop UNIQUE constraint                                 #
    # ------------------------------------------------------------------ #
    def test_step3_drops_unique_cloud_service_constraint(self):
        """M.4: after post-migrate, the
        ``project_project_unique_cloud_service`` SQL UNIQUE constraint
        is gone.

        Red today: constraint still present; query returns 1 row.
        """
        self._assert_migration_present()
        self.post_migrate(self.env.cr, '18.0.1.0.25')

        self.env.cr.execute(
            """
            SELECT 1 FROM information_schema.table_constraints
            WHERE table_name = 'project_project'
              AND constraint_type = 'UNIQUE'
              AND constraint_name LIKE %s
            """,
            ('%cloud_service%',),
        )
        rows = self.env.cr.fetchall()
        self.assertEqual(
            rows, [],
            "M.4: UNIQUE constraint on project_project(cloud_service_id) "
            "must be dropped. Plan §Phase A — Step 3 / design §7.0.2.",
        )

    # ------------------------------------------------------------------ #
    # M.5 — Step 4 cloud_service_id column kept (NOT dropped)             #
    # ------------------------------------------------------------------ #
    def test_step4_cloud_service_id_column_kept(self):
        """M.5: deprecation strategy keeps the legacy column for one
        version (backward-compat read).
        """
        self._assert_migration_present()
        self.post_migrate(self.env.cr, '18.0.1.0.25')

        self.env.cr.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'project_project'
              AND column_name = 'cloud_service_id'
            """,
        )
        row = self.env.cr.fetchone()
        self.assertIsNotNone(
            row,
            "M.5: project_project.cloud_service_id column must NOT be "
            "dropped in Phase A — kept as deprecated for one version "
            "(design §7.0.2 Step 4). Drop happens in a later version.",
        )

    # ------------------------------------------------------------------ #
    # M.6 — Step 8 external_mcp_allowed default backfill                  #
    # ------------------------------------------------------------------ #
    def test_step8_external_mcp_allowed_default_false(self):
        """M.6: existing projects get ``external_mcp_allowed = FALSE``
        after Phase A migration.

        Red today: column doesn't exist; field absent.
        """
        self._assert_migration_present()
        # Need an existing project pre-migration (without the new field).
        project = self.Project.sudo().create({
            'name': f'M.6 pre-existing {self.suffix}',
            'cloud_service_id': self.svc.id,
        })
        self.post_migrate(self.env.cr, '18.0.1.0.25')

        # Read raw SQL — avoids depending on ORM cache.
        self.env.cr.execute(
            "SELECT external_mcp_allowed FROM project_project WHERE id = %s",
            (project.id,),
        )
        row = self.env.cr.fetchone()
        self.assertIsNotNone(
            row, "M.6: external_mcp_allowed column missing on project_project.",
        )
        self.assertEqual(
            row[0], False,
            "M.6: external_mcp_allowed default must backfill to FALSE. "
            "Plan §Phase A — Step 8 / design §6.3.",
        )

    # ------------------------------------------------------------------ #
    # M.7 — Step 9 project_access bootstrap for existing projects         #
    # ------------------------------------------------------------------ #
    def test_step9_project_access_bootstrap_for_existing_project(self):
        """M.7: existing project gets a role='owner' project_access row
        derived from cloud_service.workspace_id owner partner.

        Plan §Phase A Task 6 — bootstrap; design §4.3.
        """
        self._assert_migration_present()
        if 'woow_paas_platform.project_access' not in self.env.registry:
            self.skipTest(
                "M.7 prerequisite: project_access model missing — see "
                "test_agent_pod_phase_a_new_models.py:N.9."
            )

        project = self.Project.sudo().create({
            'name': f'M.7 pre-existing bound {self.suffix}',
            'cloud_service_id': self.svc.id,
        })
        # Run the migration — bootstrap should populate project_access.
        self.post_migrate(self.env.cr, '18.0.1.0.25')

        PAccess = self.env['woow_paas_platform.project_access']
        owner_rows = PAccess.sudo().search([
            ('project_id', '=', project.id),
            ('role', '=', 'owner'),
        ])
        self.assertTrue(
            owner_rows,
            "M.7: post-migrate must seed an owner project_access for "
            "the existing bound project (derived from "
            "cloud_service.workspace_id.access_ids owner partner). "
            "Plan §Phase A Task 6 — Step 9.",
        )

    # ------------------------------------------------------------------ #
    # M.8 — Idempotent: rerun does not raise, does not duplicate           #
    # ------------------------------------------------------------------ #
    def test_post_migrate_is_idempotent(self):
        """M.8: running migrate twice produces same final state."""
        self._assert_migration_present()

        project = self.Project.sudo().create({
            'name': f'M.8 idempotent {self.suffix}',
            'cloud_service_id': self.svc.id,
        })

        # First run.
        try:
            self.post_migrate(self.env.cr, '18.0.1.0.25')
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"M.8 (first run): migrate raised "
                f"{type(exc).__name__}: {exc}",
            )

        # Second run — must NOT raise / duplicate.
        try:
            self.post_migrate(self.env.cr, '18.0.1.0.25')
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"M.8: second migrate run raised "
                f"{type(exc).__name__}: {exc}. Migration must be "
                f"idempotent — use ON CONFLICT DO NOTHING for INSERTs, "
                f"DROP CONSTRAINT IF EXISTS, etc. Plan §Phase A Task 6.",
            )

        # No duplicate rel rows.
        self.env.cr.execute(
            "SELECT COUNT(*) FROM project_cloud_service_rel "
            "WHERE project_id = %s AND cloud_service_id = %s",
            (project.id, self.svc.id),
        )
        cnt = self.env.cr.fetchone()[0]
        self.assertEqual(
            cnt, 1,
            "M.8: relation row must remain singleton after re-run "
            "(ON CONFLICT DO NOTHING).",
        )
