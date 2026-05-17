"""TDD red phase tests — Task ↔ CloudService extras (AC #9-13).

Plan: docs/refactor/plan-cloud-service-project-decouple.md (D2=A, D6=C)

Red expectation in current code: ``project.task`` has neither
``extra_cloud_service_ids`` nor ``primary_cloud_service_id``; every
``_fields`` lookup raises KeyError, which surfaces as the correct
"not yet implemented" red signal.
"""
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError, ValidationError


class _ExtrasFixtureMixin:
    """Shared fixture for tasks across workspaces with services in each."""

    def _seed_two_workspaces_with_services(self):
        Service = self.env['woow_paas_platform.cloud_service']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Workspace = self.env['woow_paas_platform.workspace']
        User = self.env['res.users']
        group_user = self.env.ref('base.group_user')

        self.alice = User.create({
            'login': 'tse_alice',
            'name': 'Alice TSE',
            'email': 'alice-tse@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.bob = User.create({
            'login': 'tse_bob',
            'name': 'Bob TSE',
            'email': 'bob-tse@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        self.w1 = Workspace.create({
            'name': 'W1 TSE', 'owner_id': self.alice.id,
        })
        self.w2 = Workspace.create({
            'name': 'W2 TSE', 'owner_id': self.bob.id,
        })

        self.template = Template.sudo().create({
            'name': 'Tmpl TSE',
            'slug': 'tmpl-tse',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-tse',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })

        self.svc_w1_primary = Service.sudo().create({
            'name': 'Svc W1 primary',
            'workspace_id': self.w1.id, 'template_id': self.template.id,
        })
        self.svc_w1_extra = Service.sudo().create({
            'name': 'Svc W1 extra',
            'workspace_id': self.w1.id, 'template_id': self.template.id,
        })
        self.svc_w2 = Service.sudo().create({
            'name': 'Svc W2',
            'workspace_id': self.w2.id, 'template_id': self.template.id,
        })

        Project = self.env['project.project']
        self.project_w1 = Project.sudo().create({
            'name': 'Project W1', 'cloud_service_id': self.svc_w1_primary.id,
        })
        self.project_w2 = Project.sudo().create({
            'name': 'Project W2', 'cloud_service_id': self.svc_w2.id,
        })

        Task = self.env['project.task']
        self.task = Task.sudo().create({
            'name': 'Task in W1 project',
            'project_id': self.project_w1.id,
        })


class TestTaskExtrasFieldsExist(TransactionCase, _ExtrasFixtureMixin):
    """AC #9-10 — schema fields and primary related."""

    def setUp(self):
        super().setUp()
        self._seed_two_workspaces_with_services()

    # ---------------- AC #9 ---------------- #
    def test_ac9_extra_cloud_service_ids_field_exists(self):
        """AC #9: ``project.task`` must declare ``extra_cloud_service_ids`` (M2M)."""
        Task = self.env['project.task']
        self.assertIn(
            'extra_cloud_service_ids', Task._fields,
            "AC #9: project.task.extra_cloud_service_ids is missing",
        )
        field = Task._fields['extra_cloud_service_ids']
        self.assertEqual(
            field.type, 'many2many',
            "AC #9: extra_cloud_service_ids must be Many2many",
        )
        self.assertEqual(
            field.comodel_name, 'woow_paas_platform.cloud_service',
            "AC #9: extra_cloud_service_ids must target cloud_service",
        )

    def test_ac9_extras_can_be_empty(self):
        """AC #9: extras is optional (empty by default)."""
        self.assertFalse(
            self.task.extra_cloud_service_ids,
            "AC #9: a newly-created task must have empty extras",
        )

    # ---------------- AC #10 ---------------- #
    def test_ac10_primary_cloud_service_related_to_project(self):
        """AC #10: ``primary_cloud_service_id`` mirrors project's service."""
        Task = self.env['project.task']
        self.assertIn(
            'primary_cloud_service_id', Task._fields,
            "AC #10: project.task.primary_cloud_service_id is missing",
        )
        self.assertEqual(
            self.task.primary_cloud_service_id, self.svc_w1_primary,
            "AC #10: primary must derive from project.cloud_service_id",
        )

    def test_ac10_primary_updates_when_project_rebound(self):
        """AC #10: store=True related — re-binding project propagates."""
        # bind project_w1 to a new service via sudo
        new_svc = self.env['woow_paas_platform.cloud_service'].sudo().create({
            'name': 'Svc W1 rebind',
            'workspace_id': self.w1.id,
            'template_id': self.template.id,
        })
        # first unbind the original so the unique constraint stays satisfied
        self.project_w1.sudo().write({'cloud_service_id': new_svc.id})
        self.task.invalidate_recordset(['primary_cloud_service_id'])
        self.assertEqual(
            self.task.primary_cloud_service_id, new_svc,
            "AC #10: rebinding project's service must propagate to "
            "task.primary_cloud_service_id (store=True related)",
        )


class TestTaskExtrasDomain(TransactionCase, _ExtrasFixtureMixin):
    """AC #11 — extras candidate list limited to task's project workspace."""

    def setUp(self):
        super().setUp()
        self._seed_two_workspaces_with_services()

    def test_ac11_extras_field_has_workspace_scoped_domain(self):
        """AC #11: ``extra_cloud_service_ids`` field declares a domain that
        references ``workspace_id`` so the UI candidate list cannot offer
        cross-workspace services.
        """
        Task = self.env['project.task']
        field = Task._fields['extra_cloud_service_ids']
        domain = field.domain
        # domain may be a list-of-tuples literal or a string (dynamic).
        domain_repr = str(domain)
        self.assertTrue(
            domain,
            "AC #11: extra_cloud_service_ids must declare a domain "
            "(empty domain offers cross-workspace candidates)",
        )
        self.assertIn(
            'workspace_id', domain_repr,
            "AC #11: domain must filter on workspace_id; got: "
            f"{domain_repr!r}",
        )


class TestTaskExtrasRecordRuleEnforcement(TransactionCase, _ExtrasFixtureMixin):
    """AC #12 — cross-workspace extras blocked at the ORM layer.

    D8=A's ir.rule on ``cloud_service`` makes a W2 service invisible to a
    W1 user; subsequent read of ``extra_cloud_service_ids`` on the task
    must therefore exclude it.  We also assert that an outright write
    attempt by a W1 user trying to link a W2 service is rejected.
    """

    def setUp(self):
        super().setUp()
        self._seed_two_workspaces_with_services()
        # Alice is W1 only; the test exercises her ORM view.
        self.task_as_alice = self.env['project.task'].with_user(
            self.alice,
        ).browse(self.task.id)

    def test_ac12_cross_workspace_extra_invisible_or_rejected(self):
        """AC #12: cross-workspace write must be rejected OR invisible.

        Implementation may choose enforcement via ir.rule (W2 svc unreadable
        ⇒ M2M write fails), an ``@api.constrains`` ValidationError, or
        both.  We accept any of AccessError / ValidationError, *or* a
        silent-drop where the read-back excludes the cross-workspace id.
        """
        try:
            self.task_as_alice.write({
                'extra_cloud_service_ids': [(4, self.svc_w2.id)],
            })
        except (AccessError, ValidationError):
            return  # acceptable enforcement path
        # If write did not raise, the cross-workspace id must not be
        # visible in the read-back (effectively dropped by record rule)
        self.task_as_alice.invalidate_recordset(['extra_cloud_service_ids'])
        ids = self.task_as_alice.extra_cloud_service_ids.ids
        self.assertNotIn(
            self.svc_w2.id, ids,
            "AC #12: cross-workspace extra must be either rejected at "
            "write time or filtered out by record rule on read",
        )

    def test_ac12_same_workspace_extra_accepted(self):
        """AC #12 sanity: same-workspace extra link succeeds."""
        self.task_as_alice.write({
            'extra_cloud_service_ids': [(4, self.svc_w1_extra.id)],
        })
        self.task_as_alice.invalidate_recordset(['extra_cloud_service_ids'])
        self.assertIn(
            self.svc_w1_extra.id, self.task_as_alice.extra_cloud_service_ids.ids,
            "Same-workspace extra must be accepted (sanity)",
        )


class TestTaskCrossProjectMove(TransactionCase, _ExtrasFixtureMixin):
    """AC #13 — extras survive a cross-project move; UI flags incompatibles.

    Implementation note: AC #13 says "UI shows warning chip, does not
    block save".  Server-side this means:
      a) extras are *not* auto-stripped on cross-project move
      b) a server-side helper / computed flag exposes which extras are
         no longer compatible with the new project's workspace
    We test (a) directly and (b) via a contract on the model.
    """

    def setUp(self):
        super().setUp()
        self._seed_two_workspaces_with_services()
        # Pre-link svc_w1_extra so the move scenario carries a real extras row.
        self.task.sudo().write({
            'extra_cloud_service_ids': [(4, self.svc_w1_extra.id)],
        })

    def test_ac13_extras_preserved_on_cross_project_move(self):
        """AC #13: moving task across projects must not strip extras."""
        self.task.sudo().write({'project_id': self.project_w2.id})
        self.task.invalidate_recordset(['extra_cloud_service_ids'])
        self.assertIn(
            self.svc_w1_extra.id, self.task.sudo().extra_cloud_service_ids.ids,
            "AC #13: cross-project move must preserve extras (UI may warn "
            "but server must not silently drop)",
        )

    def test_ac13_incompatible_extras_flag_exposed(self):
        """AC #13: model exposes a way to tell which extras are out-of-scope.

        The UI panel needs a server-side signal to render the warning
        chip; implementation chooses the name (e.g.
        ``incompatible_extra_cloud_service_ids`` computed M2M, or a
        ``has_incompatible_extras`` boolean).  We assert at least one of
        the conventional names exists so the UI contract is not silent.
        """
        self.task.sudo().write({'project_id': self.project_w2.id})
        Task = self.env['project.task']
        candidates = (
            'incompatible_extra_cloud_service_ids',
            'has_incompatible_extras',
            'extra_cloud_service_warning',
        )
        present = [c for c in candidates if c in Task._fields]
        self.assertTrue(
            present,
            "AC #13: project.task must expose one of "
            f"{candidates!r} so the UI panel can render the warning "
            "chip; none of these fields exist yet",
        )
