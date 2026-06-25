"""TDD red phase — Phase A: project.task chained schema changes.

Plan: docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase A Task 2
Design: docs/plans/2026-Q2/agent-runtime-pod-design.md §7.0.3 Task model 連鎖修改

Acceptance criteria covered:

T.1  ``task.primary_cloud_service_id`` is an independent ``Many2one`` —
     **no longer** ``related('project_id.cloud_service_id')``. Computed
     default falls back to ``project_id.cloud_service_ids[:1]`` when not
     manually set.
T.2  ``task.primary_cloud_service_id`` value is writable (not ``readonly``
     due to related); user can pick any one of
     ``project.cloud_service_ids``.
T.3  ``task.extra_cloud_service_ids`` domain drops the
     ``('workspace_id', '=', workspace_id)`` same-workspace restriction;
     new domain limits to ``('id', 'in', parent.project_id.cloud_service_ids.ids)``.
T.4  M:N project lifts the cross-workspace restriction: a task on a
     project bound to cloud_services in W1 and W2 can attach an
     ``extra_cloud_service`` from either workspace.
T.5  Hybrid spec (leader sign-off Q2 (c), 2026-05-22 — to be
     literalised in design §7.0.3):
     (a) Default = ``project_id.cloud_service_ids[:1]`` (id ASC) — auto
         correct for 1-binding project.
     (b) User-overridable via task form; field domain scopes to
         ``project_id.cloud_service_ids``.
     (c) Drift reset: when ``project.cloud_service_ids`` changes, if
         existing primary ∈ new set → preserved (user choice intact);
         otherwise → reset to new first (id ASC).

Red expectation today (Phase A implementation has NOT started):

* T.1 → RED: today field is declared as
  ``related='project_id.cloud_service_id'`` (see
  ``src/models/project_task.py:38-44``); the assert that ``related`` is
  empty / None fails.
* T.2 → RED: today writing ``primary_cloud_service_id`` on a task is
  blocked because related field is implicitly readonly (writes raise
  ``AccessError`` / ``UserError``).
* T.3 → RED: today domain string is
  ``"[('workspace_id', '=', workspace_id)]"`` literally; the assert that
  the string no longer references ``workspace_id`` fails.
* T.4 → RED: pre-refactor schema makes the cross-workspace task domain
  reject the second cloud_service.
* T.5 → RED: no computed default exists — empty after create.

刻意不測（boundary）:
* ``project.cloud_service_ids`` M:N — covered by
  ``test_agent_pod_phase_a_project_schema.py``.
* ``task.workspace_id`` deprecation strategy — design §7.0.3 留 §7 final
  決定（保留 single workspace_id for related compat 還是改 workspace_ids
  computed），本檔不鎖（avoid pre-binding to one of the two design options).
* ``incompatible_extra_cloud_service_ids`` computed (existing) — refactor
  branch behaviour; not part of Phase A scope.
"""
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestPhaseATaskChainedSchema(TransactionCase):
    """T.1 ~ T.5 — chained task schema changes per §7.0.3."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']
        cls.Task = cls.env['project.task']

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
        self.svc_a = self.Service.sudo().create({
            'name': f'Svc A {suffix}',
            'workspace_id': self.ws_a.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-a-{suffix}',
            'helm_namespace': f'paas-ws-a-{suffix}',
        })
        self.svc_b = self.Service.sudo().create({
            'name': f'Svc B {suffix}',
            'workspace_id': self.ws_b.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-b-{suffix}',
            'helm_namespace': f'paas-ws-b-{suffix}',
        })

    def _create_cross_workspace_project(self):
        """Build a project that binds svc_a (W1) and svc_b (W2).

        Falls back to skip if cloud_service_ids M2M field is missing (a
        prerequisite covered by test_agent_pod_phase_a_project_schema.py).
        """
        if 'cloud_service_ids' not in self.Project._fields:
            self.skipTest(
                "Task T-* prerequisite: project.cloud_service_ids missing "
                "(see test_agent_pod_phase_a_project_schema.py:A.1)."
            )
        return self.Project.sudo().create({
            'name': 'Task T cross-workspace project',
            'cloud_service_ids': [(6, 0, [self.svc_a.id, self.svc_b.id])],
        })

    # ------------------------------------------------------------------ #
    # T.1 — primary_cloud_service_id is independent M2O                  #
    # ------------------------------------------------------------------ #
    def test_primary_cloud_service_id_not_related(self):
        """T.1: After Phase A, ``primary_cloud_service_id`` must be an
        independent Many2one (not a related field).

        Red today: field declared as
        ``related='project_id.cloud_service_id'``.
        """
        fld = self.Task._fields.get('primary_cloud_service_id')
        self.assertIsNotNone(
            fld,
            "T.1 prerequisite: primary_cloud_service_id field missing.",
        )
        # ``related`` is the tuple of path components on a related field;
        # an independent M2O has ``related == None``.
        self.assertFalse(
            getattr(fld, 'related', None),
            "T.1: primary_cloud_service_id must be an independent "
            "Many2one (no `related='project_id.cloud_service_id'`). "
            "Plan §Phase A Task 2.1 / design §7.0.3.",
        )
        self.assertEqual(
            fld.type, 'many2one',
            "T.1: primary_cloud_service_id remains Many2one.",
        )

    # ------------------------------------------------------------------ #
    # T.2 — primary_cloud_service_id writable on multi-binding project   #
    # ------------------------------------------------------------------ #
    def test_primary_cloud_service_id_user_writable(self):
        """T.2: User can pick which member of project.cloud_service_ids
        is the primary by writing ``primary_cloud_service_id``.

        Red today: related field rejects direct write.
        """
        project = self._create_cross_workspace_project()
        task = self.Task.sudo().create({
            'name': 'T.2 task',
            'project_id': project.id,
        })
        try:
            task.sudo().write({'primary_cloud_service_id': self.svc_b.id})
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "T.2: writing primary_cloud_service_id must succeed on a "
                "multi-binding project. Got %s: %s. Plan §Phase A Task 2.1 "
                "— field must be independent Many2one with computed default."
                % (type(exc).__name__, exc)
            )
        self.assertEqual(
            task.primary_cloud_service_id.id, self.svc_b.id,
            "T.2: primary_cloud_service_id reflects the write.",
        )

    # ------------------------------------------------------------------ #
    # T.3 — extra_cloud_service_ids domain drops same-workspace clause   #
    # ------------------------------------------------------------------ #
    def test_extra_cloud_service_ids_domain_no_workspace_clause(self):
        """T.3: ``extra_cloud_service_ids`` domain no longer pins to
        ``workspace_id`` — replaced with project-scope constraint.

        Red today: domain literally ``[('workspace_id', '=', workspace_id)]``
        — see ``src/models/project_task.py:50``.
        """
        fld = self.Task._fields.get('extra_cloud_service_ids')
        self.assertIsNotNone(
            fld, "T.3 prerequisite: extra_cloud_service_ids missing.",
        )
        domain_str = getattr(fld, 'domain', '') or ''
        if not isinstance(domain_str, str):
            domain_str = str(domain_str)
        self.assertNotIn(
            "('workspace_id', '=', workspace_id)", domain_str,
            "T.3: extra_cloud_service_ids must not pin to single "
            "workspace under M:N (design §7.0.3). New domain should "
            "scope to `parent.project_id.cloud_service_ids` instead.",
        )

    # ------------------------------------------------------------------ #
    # T.4 — extras may cross workspaces under M:N                         #
    # ------------------------------------------------------------------ #
    def test_extra_cloud_service_ids_allows_cross_workspace(self):
        """T.4: With project binding svc_a (W1) + svc_b (W2), a task
        may add either as ``extra_cloud_service_ids``.

        Red today: cross-workspace addition blocked by stale domain
        clause / single-workspace task pre-condition.
        """
        project = self._create_cross_workspace_project()
        task = self.Task.sudo().create({
            'name': 'T.4 task',
            'project_id': project.id,
            'primary_cloud_service_id': self.svc_a.id,
        })
        try:
            task.sudo().write({
                'extra_cloud_service_ids': [(4, self.svc_b.id)],
            })
        except Exception as exc:  # noqa: BLE001
            self.fail(
                "T.4: cross-workspace extra_cloud_service must be "
                "allowed on M:N project. Got %s: %s. Plan §Phase A "
                "Task 2.2." % (type(exc).__name__, exc)
            )
        self.assertIn(
            self.svc_b.id, task.extra_cloud_service_ids.ids,
            "T.4: extra includes svc_b across workspace boundary.",
        )

    # ------------------------------------------------------------------ #
    # T.5 — hybrid (default + user override + drift reset)                #
    # ------------------------------------------------------------------ #
    # Leader sign-off 2026-05-22 Q2 (c) — three-part spec:
    #
    #   (a) default = project_id.cloud_service_ids[:1] (id ASC) —
    #       1-binding project automatically correct.
    #   (b) user-overridable via task form; domain restricts choice to
    #       project_id.cloud_service_ids (multi-binding case gives user
    #       a meaningful pick).
    #   (c) drift reset: when project.cloud_service_ids changes, if the
    #       existing primary survives the new set it is preserved (user
    #       choice intact); otherwise primary is reset to the new first
    #       member (no orphan FK).
    #
    # planner committed to literalise this in design §7.0.3.

    def test_primary_default_first_of_m2m_on_create(self):
        """T.5 (a): Newly-created task with no explicit
        ``primary_cloud_service_id`` defaults to the first member of
        ``project.cloud_service_ids`` (deterministic — lower id first).

        Red today: independent Many2one has no default → ``False``.
        """
        project = self._create_cross_workspace_project()
        task = self.Task.sudo().create({
            'name': 'T.5a task',
            'project_id': project.id,
        })
        expected = sorted([self.svc_a.id, self.svc_b.id])[0]
        self.assertEqual(
            task.primary_cloud_service_id.id, expected,
            "T.5 (a): primary_cloud_service_id must default to the "
            "first (lowest id) of project.cloud_service_ids. Plan "
            "§Phase A Task 2.1 / design §7.0.3 hybrid spec — computed "
            "default from cloud_service_ids[:1].",
        )

    def test_primary_user_override_survives_write(self):
        """T.5 (b): user picks a non-default member via task form;
        write() must persist the override (no eager reset to first).

        Red today: either related field rejects direct write (T.2
        territory) or write succeeds but a subsequent ORM read recomputes
        / overwrites it. Post-impl: stored independent M2O honours the
        write, primary stays = user choice across re-reads.
        """
        project = self._create_cross_workspace_project()
        task = self.Task.sudo().create({
            'name': 'T.5b task',
            'project_id': project.id,
        })
        # Pick the non-default member (svc_b if it has higher id than
        # svc_a, otherwise svc_a — pick the one the default does NOT
        # auto-select).
        default_id = sorted([self.svc_a.id, self.svc_b.id])[0]
        override_id = (
            self.svc_b.id if default_id == self.svc_a.id else self.svc_a.id
        )

        task.sudo().write({'primary_cloud_service_id': override_id})
        task.invalidate_recordset()  # force re-read from DB
        self.assertEqual(
            task.primary_cloud_service_id.id, override_id,
            "T.5 (b): user override on primary_cloud_service_id must "
            "persist — no eager reset to cloud_service_ids[:1]. Design "
            "§7.0.3 hybrid spec — user choice respected.",
        )

    def test_primary_domain_limits_to_project_cloud_service_ids(self):
        """T.5 (b): the field's domain restricts pickable values to
        ``project_id.cloud_service_ids`` (no cross-project bleed).

        Red today: independent Many2one has no domain string.
        """
        fld = self.Task._fields.get('primary_cloud_service_id')
        self.assertIsNotNone(
            fld, "T.5 prerequisite: primary_cloud_service_id missing.",
        )
        domain_str = getattr(fld, 'domain', '') or ''
        if not isinstance(domain_str, str):
            domain_str = str(domain_str)
        self.assertIn(
            "cloud_service_ids", domain_str,
            "T.5 (b): primary_cloud_service_id domain must scope to "
            "``project_id.cloud_service_ids`` (form-level pick limited "
            "to project's bound services). Design §7.0.3 hybrid spec.",
        )

    def test_primary_drift_preserve_when_still_in_new_set(self):
        """T.5 (c): when project.cloud_service_ids changes and the
        existing primary is STILL ∈ the new set, primary is preserved.

        Drift case 1: project P binds [A, B]; user picks B as primary.
        Project re-bound to [B, C] (A removed, C added). Primary should
        remain B (user choice intact).

        Red today: no drift handler exists; primary stays = B "by
        accident of stale value" pre-refactor OR gets blank-reset. We
        assert it explicitly survives the project rebind.
        """
        project = self._create_cross_workspace_project()
        # Add a third cloud_service so we can swap a third in.
        svc_c = self.Service.sudo().create({
            'name': f'Svc C {uuid.uuid4().hex[:8]}',
            'workspace_id': self.ws_a.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-c-{uuid.uuid4().hex[:8]}',
            'helm_namespace': f'paas-ws-c-{uuid.uuid4().hex[:8]}',
        })

        task = self.Task.sudo().create({
            'name': 'T.5c task',
            'project_id': project.id,
            'primary_cloud_service_id': self.svc_b.id,
        })
        # Drift: project now binds [B, C] — A removed; B still in set.
        project.sudo().write({
            'cloud_service_ids': [(6, 0, [self.svc_b.id, svc_c.id])],
        })
        task.invalidate_recordset()
        self.assertEqual(
            task.primary_cloud_service_id.id, self.svc_b.id,
            "T.5 (c): when project.cloud_service_ids drift but existing "
            "primary survives the new set, primary must be PRESERVED "
            "(user choice intact). Design §7.0.3 hybrid spec — drift "
            "preserve leg.",
        )

    def test_primary_drift_reset_when_evicted_from_new_set(self):
        """T.5 (c): when project.cloud_service_ids drift and the
        existing primary is NO LONGER ∈ the new set, primary resets to
        the new first member (no orphan FK).

        Drift case 2: project P binds [A, B]; user picks B as primary.
        Project re-bound to [A, C] (B removed). Primary must reset —
        new first is min(A.id, C.id).

        Red today: no drift handler → primary stays = B referencing a
        cloud_service no longer in the project's set (orphan FK).
        """
        project = self._create_cross_workspace_project()
        svc_c = self.Service.sudo().create({
            'name': f'Svc C {uuid.uuid4().hex[:8]}',
            'workspace_id': self.ws_a.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'svc-c2-{uuid.uuid4().hex[:8]}',
            'helm_namespace': f'paas-ws-c2-{uuid.uuid4().hex[:8]}',
        })

        task = self.Task.sudo().create({
            'name': 'T.5d task',
            'project_id': project.id,
            'primary_cloud_service_id': self.svc_b.id,
        })
        # Drift: B evicted; new set = [A, C].
        project.sudo().write({
            'cloud_service_ids': [(6, 0, [self.svc_a.id, svc_c.id])],
        })
        task.invalidate_recordset()
        expected = sorted([self.svc_a.id, svc_c.id])[0]
        self.assertEqual(
            task.primary_cloud_service_id.id, expected,
            "T.5 (c): when project.cloud_service_ids drift and primary "
            "is EVICTED, primary must reset to the new first member "
            "(lowest id). Design §7.0.3 hybrid spec — drift reset leg. "
            "No orphan FK to cloud_service no longer bound.",
        )
