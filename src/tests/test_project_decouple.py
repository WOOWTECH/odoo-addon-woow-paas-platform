"""TDD red phase tests — Project ↔ CloudService decouple (AC #1-6).

Plan: docs/refactor/plan-cloud-service-project-decouple.md
Decisions covered:
- D1=B: keep ``unique_cloud_service`` SQL constraint; Project can be unbound.
- D3=A: unbound Project visibility is gated by project membership
  (user_ids / message_partner_ids), not by ``base.group_user``.

These tests are written against the *post-refactor* contract; in the current
codebase ACL is short-circuited by ``ir_rules.xml`` line 55 ('|',
('cloud_service_id', '=', False), ...) so AC #4 will fail with
AssertionError (B can see A's unbound project — the bug we are fixing).
"""
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError, AccessError
from psycopg2 import IntegrityError
from odoo.tools import mute_logger


class TestProjectDecouple(TransactionCase):
    """AC #1-3 — Project unbound creation + uniqueness + bound regression."""

    def setUp(self):
        super().setUp()
        self.Project = self.env['project.project']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']

        self.workspace = self.Workspace.create({'name': 'WS Decouple'})
        self.template = self.Template.create({
            'name': 'Tmpl Decouple',
            'slug': 'tmpl-decouple',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1,
            'min_ram_gb': 1.0,
            'min_storage_gb': 1,
        })
        self.service = self.Service.create({
            'name': 'Service Decouple',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

    # ---------------- AC #1 ---------------- #
    def test_ac1_create_unbound_project_is_allowed(self):
        """AC #1: creating project.project without cloud_service_id must succeed.

        Currently this passes at ORM level (field is nullable), but the
        full contract includes that ACL doesn't hide the row from the
        creator (covered alongside AC #4-5).  This assertion locks the
        baseline so a future migration cannot accidentally re-add a NOT
        NULL on ``cloud_service_id``.
        """
        project = self.Project.create({'name': 'Unbound P0'})
        self.assertTrue(project.exists())
        self.assertFalse(project.cloud_service_id)
        self.assertFalse(project.workspace_id,
            "Unbound project must have empty workspace_id (computed)")

    # ---------------- AC #2 ---------------- #
    def test_ac2_unique_cloud_service_constraint_preserved(self):
        """AC #2: A Service may have at most one bound Project (regression)."""
        self.Project.create({
            'name': 'First Bound',
            'cloud_service_id': self.service.id,
        })
        with self.assertRaises(IntegrityError), \
                self.cr.savepoint(), \
                mute_logger('odoo.sql_db'):
            self.Project.create({
                'name': 'Second Bound (should fail)',
                'cloud_service_id': self.service.id,
            })

    # ---------------- AC #3 ---------------- #
    def test_ac3_bound_project_workspace_id_unchanged(self):
        """AC #3: existing bound project — workspace_id computed result stable.

        This is regression: the compute chain must remain
        ``project.cloud_service_id.workspace_id``.  Implementation must not
        replace this with a stored field that breaks existing rows.
        """
        project = self.Project.create({
            'name': 'Pre-existing Bound',
            'cloud_service_id': self.service.id,
        })
        self.assertEqual(project.workspace_id, self.workspace)
        # mutate service workspace and ensure recompute still works
        ws2 = self.Workspace.create({'name': 'WS2 Decouple'})
        self.service.write({'workspace_id': ws2.id})
        project.invalidate_recordset(['workspace_id'])
        self.assertEqual(project.workspace_id, ws2,
            "workspace_id must re-derive from cloud_service_id.workspace_id")


class TestProjectUnboundACL(TransactionCase):
    """AC #4-6 — unbound project visibility gated by project membership.

    Red expectation in current code:
    ``ir_rules.xml`` line 55 has ``'|', ('cloud_service_id', '=', False), ...``
    so any base.group_user sees every unbound project → AC #4 fails with
    AssertionError ("Bob can see Alice's unbound project").
    """

    def setUp(self):
        super().setUp()
        self.Project = self.env['project.project']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Access = self.env['woow_paas_platform.workspace_access']
        User = self.env['res.users']

        group_user = self.env.ref('base.group_user')

        # Two internal users — neither shares a project membership with the
        # other by default.
        self.alice = User.create({
            'login': 'decouple_alice',
            'name': 'Alice Decouple',
            'email': 'alice-decouple@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.bob = User.create({
            'login': 'decouple_bob',
            'name': 'Bob Decouple',
            'email': 'bob-decouple@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        # Shared workspace so the *bound* project regression (AC #6) works.
        self.workspace = self.Workspace.create({
            'name': 'WS Shared Decouple',
            'owner_id': self.alice.id,
        })
        # Add Bob as member of the workspace so bound project is reachable
        self.Access.sudo().create({
            'workspace_id': self.workspace.id,
            'user_id': self.bob.id,
            'role': 'user',
        })

        self.template = self.Template.create({
            'name': 'Tmpl Decouple ACL',
            'slug': 'tmpl-decouple-acl',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-acl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1,
            'min_ram_gb': 1.0,
            'min_storage_gb': 1,
        })
        self.service = self.Service.sudo().create({
            'name': 'Svc Shared Decouple',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

    def _create_unbound_as(self, user, name):
        return self.Project.with_user(user).create({'name': name})

    def _add_project_member(self, project, user):
        """Add ``user`` as a project member.

        Odoo 18 ``project.project`` exposes no ``user_ids`` field — D3=A
        in the plan used that label loosely.  The schema-accurate axes
        are ``message_partner_ids`` (follower) and ``create_uid``; the
        implementation's record rule covers both legs, and we follow the
        follower path here because it matches the ad-hoc "share with X"
        flow the AC describes.
        """
        project.sudo().write({
            'message_partner_ids': [(4, user.partner_id.id)],
        })

    # ---------------- AC #4 ---------------- #
    def test_ac4_unbound_project_invisible_to_non_member(self):
        """AC #4: Bob (non-member) must not see Alice's unbound project."""
        p1 = self._create_unbound_as(self.alice, 'Alice Unbound')
        self.assertFalse(p1.cloud_service_id,
            "Sanity: setup project must be unbound")

        visible = self.Project.with_user(self.bob).search([
            ('id', '=', p1.id),
        ])
        self.assertFalse(
            visible,
            "AC #4 violated: Bob can see Alice's unbound project — "
            "ir_rules.xml short-circuit on cloud_service_id=False is the "
            "leak. Implementation must remove the short-circuit.",
        )

    # ---------------- AC #5 ---------------- #
    def test_ac5_unbound_project_visible_after_member_add(self):
        """AC #5: once Bob is added as project member, he sees P1."""
        p1 = self._create_unbound_as(self.alice, 'Alice Unbound 2')
        self._add_project_member(p1, self.bob)

        visible = self.Project.with_user(self.bob).search([
            ('id', '=', p1.id),
        ])
        self.assertEqual(
            visible, p1,
            "AC #5: Bob added as project member must be able to read P1",
        )

    # ---------------- AC #6 (regression) ---------------- #
    def test_ac6_bound_project_visible_to_workspace_member(self):
        """AC #6 (regression): Bob, workspace member, sees bound project."""
        bound = self.Project.sudo().create({
            'name': 'Bound for Bob via WS',
            'cloud_service_id': self.service.id,
        })
        visible = self.Project.with_user(self.bob).search([
            ('id', '=', bound.id),
        ])
        self.assertEqual(
            visible, bound,
            "AC #6 regression: bound project must remain visible to "
            "workspace members after the unbound short-circuit is removed",
        )
