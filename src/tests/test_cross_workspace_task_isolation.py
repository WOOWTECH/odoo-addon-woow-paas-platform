"""TDD red regression test — cross-workspace project.task read isolation.

CAQA report: docs/decisions/reviews/caqa-2026-05-16.md §E HIGH BLOCKER (B1)

Background:
- Implementation added ``ir.model.access.csv:27`` granting ``base.group_user``
  RWCU on ``project.task`` (first time the group has any task access).
- Implementation added ``project_task_rule_workspace_member`` in
  ``ir_rules.xml`` to workspace-gate task RWCU.
- Implementation deactivated ``project.project_public_members_rule`` in the
  ``18.0.1.0.23`` migration to remove the OR-leg leak on ``project.project``.
- **Implementation did NOT deactivate** the task-level counterparts
  ``project.task_visibility_rule`` and ``project.task_visibility_rule_project_user``
  (Odoo std, both target ``base.group_user`` / ``project.group_project_user``,
  both have implicit ``perm_read=True``).
- Per Odoo ``ir_rule.py:140-168`` the multi-rule semantics on the same group
  are OR; so the effective read domain becomes
  ``(std rule: privacy_visibility != 'followers' OR follower)`` OR
  ``(workspace member)``. Default ``project.privacy_visibility`` is
  ``'employees'``, so the std-rule leg is always-true → cross-workspace
  task read leaks across the whole instance.

Red expectation today: Alice (workspace W1 only, not a follower / member)
can ``search`` and ``browse().read()`` a task that belongs to Bob's
workspace W2, both as silent leak.

Green expectation after implementation patch: the std rules are
deactivated symmetrically with the project-level fix, so
``search`` returns empty and ``browse().read()`` raises ``AccessError``.
The own-workspace path must keep working (sanity).
"""
from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError
from ..models.cloud_service import _internal_provision


class TestTaskCrossWorkspaceACL(TransactionCase):
    """B1 — base.group_user must not read tasks from foreign workspaces."""

    def setUp(self):
        super().setUp()
        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']
        Project = self.env['project.project']
        Task = self.env['project.task']
        User = self.env['res.users']
        group_user = self.env.ref('base.group_user')

        # Two internal users with no overlapping workspace membership.
        self.alice = User.create({
            'login': 'b1_alice',
            'name': 'Alice (B1)',
            'email': 'alice-b1@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.bob = User.create({
            'login': 'b1_bob',
            'name': 'Bob (B1)',
            'email': 'bob-b1@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        # Alice owns W1, Bob owns W2 — owner_id is auto-added to access_ids
        # by workspace.create (see workspace.py:73-86).
        self.ws_alice = Workspace.create({
            'name': 'W1 Alice (B1)', 'owner_id': self.alice.id,
        })
        self.ws_bob = Workspace.create({
            'name': 'W2 Bob (B1)', 'owner_id': self.bob.id,
        })

        self.template = Template.sudo().create({
            'name': 'Tmpl B1',
            'slug': 'tmpl-b1',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-b1',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1,
            'min_ram_gb': 1.0,
            'min_storage_gb': 1,
        })

        # Bob's W2 has a bound project + one task. Default privacy_visibility
        # on project is 'employees' (Odoo std), which is exactly the value
        # the std task_visibility_rule's leak leg matches.
        self.svc_bob = _internal_provision(Service.sudo()).create({
            'name': 'Svc Bob (B1)',
            'workspace_id': self.ws_bob.id,
            'template_id': self.template.id,
        })
        self.project_bob = Project.sudo().create({
            'name': 'Project Bob (B1)',
            'cloud_service_id': self.svc_bob.id,
        })
        self.task_bob = Task.sudo().create({
            'name': 'Task Bob (B1)',
            'project_id': self.project_bob.id,
        })

    def test_alice_cannot_read_bob_task_via_orm(self):
        """Cross-workspace task read must be denied by record rule.

        Today's leak: Odoo std ``task_visibility_rule`` (perm_read=True
        implicitly, base.group_user) OR-combines with implementation's
        ``project_task_rule_workspace_member`` → Alice sees Bob's task
        because the std rule's ``privacy_visibility != 'followers'`` leg
        is always-true for default privacy 'employees'.

        Asserts BOTH ORM read paths Odoo uses post-fix:
          - ``search`` filters out the cross-ws task (returns empty)
          - explicit ``browse(id).read()`` raises ``AccessError``
        """
        Task = self.env['project.task']

        # 1. search() — record-rule denial is a silent filter.
        visible = Task.with_user(self.alice).search([
            ('id', '=', self.task_bob.id),
        ])
        self.assertFalse(
            visible,
            "B1 leak: Alice (W1) can search() Bob's W2 task. Need to "
            "deactivate std `project.task_visibility_rule` + "
            "`project.task_visibility_rule_project_user` symmetrically with "
            "the project.project fix in migrations/18.0.1.0.23.",
        )

        # 2. browse().read() — explicit ACL check, raises when blocked.
        task_as_alice = Task.with_user(self.alice).browse(self.task_bob.id)
        with self.assertRaises(
            AccessError,
            msg="B1 leak: browse().read() on Bob's W2 task succeeded for "
                "Alice — AccessError expected once the workspace-scoped "
                "task rule is the only active rule on base.group_user.",
        ):
            task_as_alice.read(['name'])

    def test_alice_cannot_read_bob_project_via_orm(self):
        """B1-class regression — std `project.project_public_members_rule` leak.

        Mirror of ``test_alice_cannot_read_bob_task_via_orm`` but at the
        ``project.project`` layer. CAQA (re-loop §"同源 code path 檢查")
        surfaced that the fresh-install B1 patch had to deactivate three
        std rules — the project-level one
        (``project.project_public_members_rule``) is the symmetric peer of
        the two task-level rules already covered by the test above. Without
        a regression lock at the project layer, a future revert of just
        the project XML ref would silently re-leak project rows even while
        the task tests stay green.

        Spec ref: D9 / AC #29 — declaration-data (``ir_rules.xml``)
        deactivation of the project rule must land in lockstep with the
        task-side rules, so both fresh-install and upgrade paths share the
        same effective ACL.

        Asserts BOTH ORM read paths Odoo uses post-fix:
          - ``search`` filters out Bob's W2 project (returns empty)
          - explicit ``browse(id).read()`` raises ``AccessError``
        """
        Project = self.env['project.project']

        # 1. search() — record-rule denial is a silent filter.
        visible = Project.with_user(self.alice).search([
            ('id', '=', self.project_bob.id),
        ])
        self.assertFalse(
            visible,
            "B1 leak (project): Alice (W1) can search() Bob's W2 project. "
            "Need to deactivate std `project.project_public_members_rule` "
            "in declaration data (security/ir_rules.xml) symmetrically with "
            "the task-side rule deactivation — see CAQA B-1 / spec D9 / "
            "AC #29.",
        )

        # 2. browse().read() — explicit ACL check, raises when blocked.
        project_as_alice = Project.with_user(self.alice).browse(
            self.project_bob.id,
        )
        with self.assertRaises(
            AccessError,
            msg="B1 leak (project): browse().read() on Bob's W2 project "
                "succeeded for Alice — AccessError expected once std "
                "`project.project_public_members_rule` is deactivated "
                "in declaration data alongside the task-side rules.",
        ):
            project_as_alice.read(['name'])

    def test_bob_can_read_own_workspace_task(self):
        """Sanity: own-workspace task read must remain accessible.

        Guards against the fix collapsing to a blanket deny; the
        workspace-scoped rule chain
        ``project_id.cloud_service_id.workspace_id.access_ids.user_id``
        must keep Bob (W2 owner ⇒ access_ids member) on the green path.
        """
        Task = self.env['project.task']
        visible = Task.with_user(self.bob).search([
            ('id', '=', self.task_bob.id),
        ])
        self.assertEqual(
            visible, self.task_bob,
            "Own-workspace task read regressed: Bob (W2 owner) lost "
            "access to W2 task — the B1 fix must not deny workspace "
            "members.",
        )
