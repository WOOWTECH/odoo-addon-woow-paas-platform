"""TDD red phase — Phase A: project.project schema migration to M:N.

Plan: docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase A — Schema migration
Design: docs/plans/2026-Q2/agent-runtime-pod-design.md
  - §1.3.1 Project 升格的 schema 衝擊（M2O → M2M 完整 table）
  - §1.3.3 對 refactor branch 已 fix 範圍的明確引用
  - §6.3 (Q1) ``project.external_mcp_allowed`` Hybrid external MCP scheme
  - §4 ``project.agent_template_id`` agent template binding
  - §7.0.2 6-step dual-write/backfill migration
  - §7.0.7 Additive schema changes
  - §7.1 Phase A bullets 1-4 (use_agent_runtime flag added Phase A,
    flipped during Phase D-F)

Acceptance criteria covered（plan doc Phase A Test scope）:

A.1  ``project.cloud_service_ids`` Many2many field exists with relation
     table ``project_cloud_service_rel``.
A.2  ``project.workspace_ids`` computed Many2many aggregates
     ``cloud_service_ids.workspace_id``.
A.3  SQL ``UNIQUE(cloud_service_id)`` constraint removed; one project
     can bind multiple cloud_services.
A.4  ``project.cloud_service_id`` kept deprecated; reads from
     ``cloud_service_ids[:1]`` (backward-compat single-FK contract).
A.5  ``project.write({'cloud_service_id': X})`` syncs ``cloud_service_ids``
     to include X (idempotent dual-write).
A.6  ``project.external_mcp_allowed = Boolean(default=False)`` added
     (§6.3 hybrid NetworkPolicy scheme gate).
A.7  ``project.agent_template_id = Many2one('woow_paas_platform.agent_template')``
     added.
A.8  ``project.use_agent_runtime = Boolean(default=False)`` added
     (Phase D-F dual-write toggle; default False in Phase A — flag dormant).
A.9  Cross-workspace binding allowed (project binds cloud_service in W1
     **and** cloud_service in W2 simultaneously); pre-refactor 1:1 path
     forced single-workspace, M:N must lift that.

Red expectation today (Phase A implementation has NOT started):

* A.1, A.2, A.6, A.7, A.8 → RED: AttributeError / KeyError when accessing
  new fields on ``project.project``; fixture annotates the missing field.
* A.3 → RED: Today ``UNIQUE(cloud_service_id)`` blocks the second
  ``cloud_service_id`` binding to the same project AND blocks creating
  two projects on the same ``cloud_service_id``; M:N drops that — both
  should succeed post-impl.
* A.4 → RED today: ``cloud_service_id`` getter still reads the raw column.
  Post-impl: getter delegates to ``cloud_service_ids[:1].id``.
* A.5 → RED today: no ``write()`` override syncs the deprecated field.
* A.9 → RED today: 1:1 schema cannot represent the cross-workspace pair.

刻意不測（boundary）:

* Migration script itself — covered by ``test_agent_pod_phase_a_migration.py``.
* ``cloud_service.project_ids`` reverse M:N — covered by
  ``test_agent_pod_phase_a_cloud_service_mn.py``.
* Task model chained changes — covered by
  ``test_agent_pod_phase_a_task_schema.py``.
* New models (``agent_template``, ``agent_session``, etc.) schema —
  covered by ``test_agent_pod_phase_a_new_models.py``.
* ACL dual-leg walk path on M2M — covered by ``test_agent_pod_phase_a_acl.py``.
* External-MCP-allowed ACL admin-only enforcement — covered by
  ``test_agent_pod_phase_a_acl.py`` (admin/owner role × write).

Fixture / env assumptions:

* PR #54 refactor branch merged (D5 removed; ``mcp_server.project_id`` +
  ``mcp_server.sidecar_backed`` exist) — checked in setUp fail-fast.
* No Phase A migration has been applied; new fields ABSENT on
  ``project.project`` is the canonical red.
"""
import uuid

from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _internal_provision


_RELATION_TABLE = 'project_cloud_service_rel'


@tagged('post_install', '-at_install')
class TestPhaseAProjectM2MSchema(TransactionCase):
    """A.1, A.2, A.3, A.9 — M:N schema shape on project.project."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']

        # Refactor-branch invariant guard.
        Mcp = cls.env['woow_paas_platform.mcp_server']
        if 'project_id' not in Mcp._fields or 'sidecar_backed' not in Mcp._fields:
            raise RuntimeError(
                "Refactor-branch invariant guard failed: mcp_server.project_id "
                "and/or mcp_server.sidecar_backed missing — Phase A tests "
                "assume PR #54 is merged. See design §1.3.3."
            )

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.ws_a = self.Workspace.sudo().create({'name': f'WS A {suffix}'})
        self.ws_b = self.Workspace.sudo().create({'name': f'WS B {suffix}'})
        self.tmpl = self.Template.sudo().create({
            'name': f'Tmpl {suffix}',
            'slug': f'tmpl-{suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'tmpl-{suffix}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_a = _internal_provision(self.Service.sudo()).create({
            'name': f'Svc A {suffix}',
            'workspace_id': self.ws_a.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-a-{suffix}',
            'helm_namespace': f'paas-ws-a-{suffix}',
        })
        self.svc_b = _internal_provision(self.Service.sudo()).create({
            'name': f'Svc B {suffix}',
            'workspace_id': self.ws_b.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-b-{suffix}',
            'helm_namespace': f'paas-ws-b-{suffix}',
        })

    # ------------------------------------------------------------------ #
    # A.1 — cloud_service_ids M2M field exists                            #
    # ------------------------------------------------------------------ #
    def test_project_has_cloud_service_ids_m2m_field(self):
        """A.1: ``project.cloud_service_ids`` is a Many2many to
        ``woow_paas_platform.cloud_service`` via
        ``project_cloud_service_rel``.

        Red today: ``cloud_service_ids`` not declared in
        ``src/models/project_project.py``; ``_fields`` lookup raises
        KeyError or the assertion below fails.
        """
        self.assertIn(
            'cloud_service_ids', self.Project._fields,
            "A.1: project.cloud_service_ids Many2many field missing. "
            "Plan §Phase A Task 1.1 — add "
            "`cloud_service_ids = Many2many('woow_paas_platform.cloud_service', "
            "relation='project_cloud_service_rel', column1='project_id', "
            "column2='cloud_service_id')` on project.project.",
        )
        fld = self.Project._fields['cloud_service_ids']
        self.assertEqual(
            fld.type, 'many2many',
            "A.1: project.cloud_service_ids must be Many2many; "
            "design §1.3.1 forbids reusing One2many here.",
        )
        self.assertEqual(
            fld.comodel_name, 'woow_paas_platform.cloud_service',
            "A.1: comodel_name must be woow_paas_platform.cloud_service.",
        )
        self.assertEqual(
            fld.relation, _RELATION_TABLE,
            f"A.1: relation table must be '{_RELATION_TABLE}' "
            "to share storage with cloud_service.project_ids reverse "
            "(design §7.0.2 step 6).",
        )

    # ------------------------------------------------------------------ #
    # A.2 — workspace_ids computed M2M                                    #
    # ------------------------------------------------------------------ #
    def test_project_has_workspace_ids_computed_m2m(self):
        """A.2: ``project.workspace_ids`` is computed M2M derived from
        ``cloud_service_ids.workspace_id`` (stored).

        Red today: ``workspace_ids`` not declared; only legacy
        ``workspace_id`` (related M2O) exists.
        """
        self.assertIn(
            'workspace_ids', self.Project._fields,
            "A.2: project.workspace_ids computed Many2many missing. "
            "Plan §Phase A Task 1.2 — add "
            "`workspace_ids = Many2many('woow_paas_platform.workspace', "
            "compute='_compute_workspace_ids', store=True)`.",
        )
        fld = self.Project._fields['workspace_ids']
        self.assertEqual(fld.type, 'many2many')
        self.assertTrue(
            getattr(fld, 'compute', None),
            "A.2: workspace_ids must be computed; design §1.3.1 — "
            "aggregate from cloud_service_ids.workspace_id.",
        )
        self.assertTrue(
            getattr(fld, 'store', False),
            "A.2: workspace_ids must be stored — used by ir.rule "
            "domains + per-project agent NetworkPolicy egress "
            "rendering (design §2.7.3).",
        )

    def test_workspace_ids_aggregates_across_cloud_services(self):
        """A.2 behaviour: project binding two cloud_services from
        different workspaces → workspace_ids = {W_A, W_B}.

        Red today: field absent → AttributeError on access.
        """
        if 'cloud_service_ids' not in self.Project._fields:
            self.skipTest("A.2 prerequisite: cloud_service_ids missing — see A.1")

        project = self.Project.sudo().create({
            'name': 'A.2 project',
            'cloud_service_ids': [(6, 0, [self.svc_a.id, self.svc_b.id])],
        })
        self.assertEqual(
            set(project.workspace_ids.ids),
            {self.ws_a.id, self.ws_b.id},
            "A.2: workspace_ids should aggregate workspaces from "
            "all bound cloud_services (cross-workspace binding).",
        )

    # ------------------------------------------------------------------ #
    # A.3 — UNIQUE(cloud_service_id) constraint dropped                  #
    # ------------------------------------------------------------------ #
    def test_unique_cloud_service_id_constraint_removed(self):
        """A.3: After Phase A migration, two projects must be allowed to
        bind the same cloud_service (M:N semantics; primary M2O column
        kept only for backward-compat read).

        Red today: ``unique_cloud_service`` SQL constraint blocks the
        second project create → ``psycopg2.IntegrityError`` /
        ``odoo.exceptions.ValidationError`` raised; assert flips.
        """
        self.Project.sudo().create({
            'name': 'A.3 first project',
            'cloud_service_id': self.svc_a.id,
        })
        try:
            self.Project.sudo().create({
                'name': 'A.3 second project (same svc)',
                'cloud_service_id': self.svc_a.id,
            })
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "A.3: second project on same cloud_service must succeed "
                "(M:N drops UNIQUE constraint). Got %s: %s. Plan §Phase "
                "A Task 1.3 — drop `UNIQUE(cloud_service_id)` via "
                "post-migrate ALTER TABLE." % (type(exc).__name__, exc)
            )

    def test_two_cloud_services_on_one_project(self):
        """A.3 behaviour: ``cloud_service_ids`` allows two members on
        one project (write through M:N field).

        Red today: ``cloud_service_ids`` not a field; AttributeError.
        """
        if 'cloud_service_ids' not in self.Project._fields:
            self.skipTest("A.3 prerequisite: cloud_service_ids missing")

        project = self.Project.sudo().create({
            'name': 'A.3 dual-binding project',
            'cloud_service_ids': [(6, 0, [self.svc_a.id, self.svc_b.id])],
        })
        self.assertEqual(
            set(project.cloud_service_ids.ids),
            {self.svc_a.id, self.svc_b.id},
            "A.3: M:N allows two cloud_services per project.",
        )

    # ------------------------------------------------------------------ #
    # A.9 — cross-workspace binding allowed                               #
    # ------------------------------------------------------------------ #
    def test_cross_workspace_cloud_service_binding(self):
        """A.9: project bound to cloud_service in W1 + cloud_service in
        W2 → project.workspace_ids = {W1, W2}.

        Red today: pre-refactor schema forces single workspace
        (cloud_service_id → workspace_id related).
        """
        if 'cloud_service_ids' not in self.Project._fields:
            self.skipTest("A.9 prerequisite: cloud_service_ids missing")
        if 'workspace_ids' not in self.Project._fields:
            self.skipTest("A.9 prerequisite: workspace_ids missing")

        project = self.Project.sudo().create({
            'name': 'A.9 cross-workspace project',
            'cloud_service_ids': [(6, 0, [self.svc_a.id, self.svc_b.id])],
        })
        self.assertIn(self.ws_a.id, project.workspace_ids.ids)
        self.assertIn(self.ws_b.id, project.workspace_ids.ids)


@tagged('post_install', '-at_install')
class TestPhaseAProjectDeprecatedFieldDualWrite(TransactionCase):
    """A.4, A.5 — cloud_service_id deprecated path + dual-write hook."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.ws = self.Workspace.sudo().create({'name': f'WS DW {suffix}'})
        self.tmpl = self.Template.sudo().create({
            'name': f'Tmpl DW {suffix}',
            'slug': f'tmpl-dw-{suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'tmpl-dw-{suffix}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_x = _internal_provision(self.Service.sudo()).create({
            'name': f'Svc X {suffix}',
            'workspace_id': self.ws.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-x-{suffix}',
            'helm_namespace': f'paas-ws-x-{suffix}',
        })
        self.svc_y = _internal_provision(self.Service.sudo()).create({
            'name': f'Svc Y {suffix}',
            'workspace_id': self.ws.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-y-{suffix}',
            'helm_namespace': f'paas-ws-y-{suffix}',
        })

    # ------------------------------------------------------------------ #
    # A.4 — cloud_service_id getter returns cloud_service_ids[:1]        #
    # ------------------------------------------------------------------ #
    def test_legacy_cloud_service_id_reads_first_of_m2m(self):
        """A.4: ``project.cloud_service_id`` (deprecated M2O) must
        delegate to ``cloud_service_ids[:1].id`` — no two sources of
        truth on a backward-compat read path.

        Red today: no override; legacy column read returns the legacy
        FK independently of the M2M set.
        """
        if 'cloud_service_ids' not in self.Project._fields:
            self.skipTest("A.4 prerequisite: cloud_service_ids missing")

        project = self.Project.sudo().create({
            'name': 'A.4 single binding',
            'cloud_service_ids': [(6, 0, [self.svc_x.id])],
        })
        self.assertEqual(
            project.cloud_service_id.id, self.svc_x.id,
            "A.4: cloud_service_id must read from cloud_service_ids[:1]; "
            "plan §Phase A Task 1.1 backward-compat read override.",
        )

        # Add second service — first remains primary (M2M ordering by id).
        project.sudo().write({'cloud_service_ids': [(4, self.svc_y.id)]})
        first_id = sorted([self.svc_x.id, self.svc_y.id])[0]
        self.assertEqual(
            project.cloud_service_id.id, first_id,
            "A.4: cloud_service_id reflects M2M deterministic first; "
            "after add second, primary stays the lower id.",
        )

    # ------------------------------------------------------------------ #
    # A.5 — write(cloud_service_id=X) syncs cloud_service_ids            #
    # ------------------------------------------------------------------ #
    def test_write_legacy_cloud_service_id_syncs_m2m(self):
        """A.5: ``project.write({'cloud_service_id': X})`` must add X to
        ``cloud_service_ids`` (idempotent dual-write).

        Red today: no write override exists; legacy column write does
        not propagate to M2M.
        """
        if 'cloud_service_ids' not in self.Project._fields:
            self.skipTest("A.5 prerequisite: cloud_service_ids missing")

        project = self.Project.sudo().create({'name': 'A.5 dual-write'})
        project.sudo().write({'cloud_service_id': self.svc_x.id})
        self.assertIn(
            self.svc_x.id, project.cloud_service_ids.ids,
            "A.5: writing legacy cloud_service_id must sync into "
            "cloud_service_ids (plan §Phase A Task 1.1 — write() override).",
        )

        # Idempotent — writing the same value again should not error
        # nor duplicate.
        project.sudo().write({'cloud_service_id': self.svc_x.id})
        self.assertEqual(
            project.cloud_service_ids.ids.count(self.svc_x.id), 1,
            "A.5: dual-write must be idempotent (M2M sets ignore duplicates).",
        )


@tagged('post_install', '-at_install')
class TestPhaseAProjectAdditiveFields(TransactionCase):
    """A.6, A.7, A.8 — additive boolean / FK fields on project."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Project = cls.env['project.project']

    # ------------------------------------------------------------------ #
    # A.6 — external_mcp_allowed Boolean default False                   #
    # ------------------------------------------------------------------ #
    def test_external_mcp_allowed_field(self):
        """A.6: ``project.external_mcp_allowed = Boolean(default=False)``.

        Red today: field absent. After Phase A: present, default False.
        Design §6.3 hybrid scheme — admin must explicitly opt-in to
        external MCP NetworkPolicy egress.
        """
        self.assertIn(
            'external_mcp_allowed', self.Project._fields,
            "A.6: project.external_mcp_allowed Boolean missing. "
            "Plan §Phase A Task 1.4 — add "
            "`external_mcp_allowed = fields.Boolean(default=False)`. "
            "Design §6.3 (Q1 sign-off) — admin-toggled per-project SSRF "
            "gate for hybrid NetworkPolicy egress.",
        )
        fld = self.Project._fields['external_mcp_allowed']
        self.assertEqual(fld.type, 'boolean')

        project = self.Project.sudo().create({'name': 'A.6 default check'})
        self.assertFalse(
            project.external_mcp_allowed,
            "A.6: default must be False (conservative — closed-egress).",
        )

    # ------------------------------------------------------------------ #
    # A.7 — agent_template_id Many2one                                    #
    # ------------------------------------------------------------------ #
    def test_agent_template_id_field(self):
        """A.7: ``project.agent_template_id = Many2one('woow_paas_platform.
        agent_template')``.

        Red today: ``agent_template`` model does not exist; field
        absent on project.
        """
        self.assertIn(
            'agent_template_id', self.Project._fields,
            "A.7: project.agent_template_id Many2one missing. "
            "Plan §Phase A Task 1.5 — add "
            "`agent_template_id = Many2one('woow_paas_platform.agent_template')`. "
            "Design §4 agent_template binding.",
        )
        fld = self.Project._fields['agent_template_id']
        self.assertEqual(fld.type, 'many2one')
        self.assertEqual(
            fld.comodel_name, 'woow_paas_platform.agent_template',
            "A.7: comodel_name must be woow_paas_platform.agent_template.",
        )

    # ------------------------------------------------------------------ #
    # A.8 — use_agent_runtime Boolean default False                       #
    # ------------------------------------------------------------------ #
    def test_use_agent_runtime_field(self):
        """A.8: ``project.use_agent_runtime = Boolean(default=False)``.

        Plan §Phase A Task 1.6 — flag added in Phase A but dormant
        (default False); Phase D-F switches per-project to true.
        Phase F destructive cleanup removes the field.

        Red today: field absent.
        """
        self.assertIn(
            'use_agent_runtime', self.Project._fields,
            "A.8: project.use_agent_runtime Boolean missing. "
            "Plan §Phase A Task 1.6 — add (default=False); flipped "
            "during Phase D pilot rollout.",
        )
        fld = self.Project._fields['use_agent_runtime']
        self.assertEqual(fld.type, 'boolean')
        project = self.Project.sudo().create({'name': 'A.8 default check'})
        self.assertFalse(
            project.use_agent_runtime,
            "A.8: default must be False — Phase A ships flag dormant.",
        )
