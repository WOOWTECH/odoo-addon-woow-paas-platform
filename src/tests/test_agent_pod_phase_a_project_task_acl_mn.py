"""TDD red phase — Phase A OQ-A-2: project / task ir.rule M:N walk.

CAQA M-1 (2026-05-22, user sign-off):

Plan §Phase A Task 4 + design §1.3.2 Q1 sign-off implicitly requires
``project_rule_workspace_member`` + ``project_task_rule_workspace_member``
to walk the M2M ``cloud_service_ids`` (not the legacy M2O
``cloud_service_id``) — same pattern as
``mcp_server_rule_workspace_user`` already covered by Phase A R.7 of
``test_agent_pod_phase_a_acl.py``.

Impl shipped the M:N migration for the project schema + updated
``mcp_server`` / ``mcp_tool`` ACL walk paths to M:N, but did NOT update
``project_rule_workspace_member`` and ``project_task_rule_workspace_member``
to use the new M:N field.

**Concrete leak**: a project P binds cloud_service_A (workspace W1) +
cloud_service_B (workspace W2). Bob is a W2 member but has no W1
access. Today's project rule walks ``cloud_service_id.workspace_id``,
which under the M2M deprecated-read path resolves to a *single*
cloud_service (e.g. the first via id-ASC, which may be in W1). Result:
Bob (legitimate W2 member of P) cannot search/read P at all — false
deny.

Worse asymmetry: ``mcp_server_rule_workspace_user`` already walks
``project_id.cloud_service_ids.workspace_id`` (per Phase A R.1 +
impl). So Bob CAN read the user-scope MCP attached to P (via leg 1
M2M OR semantics), but CANNOT read P itself → orphan-reference
inconsistency.

Per design §1.3.2 Q1 sign-off:

  > agent pod **物理邊界 = project**: pod 服務 project 內 user，跨
  > project 物理隔離

The leader confirmed OR semantics during the R.7 Q1 sign-off — every
workspace that P binds a cloud_service in becomes a "via" for project
visibility. Both project_rule + project_task_rule must adopt the same
M:N walk to match mcp_server's R.7 behaviour.

Acceptance criteria (behaviour-locked, mirroring R.7 pattern):

O1.1  W2 member can search M:N project (binds W1 + W2 cloud_services).
O1.2  W1 member can search the same M:N project — leg-1 regression lock.
O1.3  W2 member can search task belonging to the M:N project — task
      rule must walk the same M:N field as project rule.
O1.4  Cross-workspace user (in unrelated W3) cannot search the M:N
      project — domain still correctly denies non-member.
O1.5  Domain string assertions — both project_rule + task_rule
      reference ``cloud_service_ids`` (M2M segment) rather than the
      legacy ``cloud_service_id`` M2O.

Red expectation today (impl shipped Phase A but missed M:N walk on
project / task rules):

* O1.1 → RED: project_rule_workspace_member walks legacy M2O →
  cross-workspace member denied → search returns []
* O1.3 → RED: project_task_rule_workspace_member same problem
* O1.5 → RED: domain string literally contains
  ``cloud_service_id.workspace_id`` (no ``_ids``)
* O1.2, O1.4 → likely GREEN today (regression locks); must stay green
  after impl

Boundary（刻意不測）:

* Unbound-leg behaviour (``cloud_service_ids = False`` carve-out via
  message_partner_ids/create_uid) — already locked by R.6 + the
  existing project_rule's unbound leg (PR #47 era); not re-tested here.
* perm_write/create/unlink for M:N walk — Phase A scope is read-leg;
  write-side mirroring left to phase-tester expansion if needed.
"""
import uuid

from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _internal_provision


def _new_internal_user(env, login, name=None):
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestPhaseAProjectTaskAclMnWalk(TransactionCase):
    """O1.1 ~ O1.5 — project_rule + task_rule walk M:N cloud_service_ids."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.WAccess = cls.env['woow_paas_platform.workspace_access']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']
        cls.Task = cls.env['project.task']
        cls.IrRule = cls.env['ir.rule']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]

        if 'cloud_service_ids' not in self.Project._fields:
            self.skipTest(
                "OQ-A-2 prerequisite: project.cloud_service_ids M2M "
                "missing — Phase A schema migration not applied. See "
                "test_agent_pod_phase_a_project_schema.py:A.1."
            )

        self.alice = _new_internal_user(self.env, f'oq_alice_{self.suffix}', 'Alice (OQ-A-2)')
        self.bob = _new_internal_user(self.env, f'oq_bob_{self.suffix}', 'Bob (OQ-A-2)')
        self.eve = _new_internal_user(self.env, f'oq_eve_{self.suffix}', 'Eve (OQ-A-2)')

        # W1 owned by Alice; W2 owned by Bob; W3 owned by Eve (outsider).
        self.ws1 = self.Workspace.sudo().create({
            'name': f'OQ W1 {self.suffix}', 'owner_id': self.alice.id,
        })
        self.ws2 = self.Workspace.sudo().create({
            'name': f'OQ W2 {self.suffix}', 'owner_id': self.bob.id,
        })
        self.ws3 = self.Workspace.sudo().create({
            'name': f'OQ W3 (outsider) {self.suffix}', 'owner_id': self.eve.id,
        })

        # Templates + services in each workspace.
        self.tmpl = self.Template.sudo().create({
            'name': f'OQ Tmpl {self.suffix}',
            'slug': f'oq-tmpl-{self.suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'oq-tmpl-{self.suffix}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_w1 = _internal_provision(self.Service.sudo()).create({
            'name': f'OQ Svc W1 {self.suffix}',
            'workspace_id': self.ws1.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'oq-svc-w1-{self.suffix}',
            'helm_namespace': f'paas-ws-oq-w1-{self.suffix}',
        })
        self.svc_w2 = _internal_provision(self.Service.sudo()).create({
            'name': f'OQ Svc W2 {self.suffix}',
            'workspace_id': self.ws2.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'oq-svc-w2-{self.suffix}',
            'helm_namespace': f'paas-ws-oq-w2-{self.suffix}',
        })

        # M:N project binds svc_w1 AND svc_w2.
        self.cross_ws_project = self.Project.sudo().create({
            'name': f'OQ cross-ws project {self.suffix}',
            'cloud_service_ids': [(6, 0, [self.svc_w1.id, self.svc_w2.id])],
        })
        self.cross_ws_task = self.Task.sudo().create({
            'name': f'OQ cross-ws task {self.suffix}',
            'project_id': self.cross_ws_project.id,
        })

    # ------------------------------------------------------------------ #
    # O1.1 — W2 member can search M:N project                             #
    # ------------------------------------------------------------------ #
    def test_O1_1_w2_member_can_read_cross_ws_project(self):
        """O1.1: Bob (W2 owner, automatically W2 access_ids member) must
        be able to search/read the cross-workspace M:N project.

        Red today: project_rule_workspace_member walks
        ``cloud_service_id.workspace_id`` (legacy M2O which under the
        backward-compat read returns at most ONE cloud_service) — Bob's
        W2 access_ids cannot match if the legacy M2O resolves to svc_w1.
        """
        ids = self.Project.with_user(self.bob).search([
            ('id', '=', self.cross_ws_project.id),
        ]).ids
        self.assertEqual(
            ids, [self.cross_ws_project.id],
            "O1.1: W2 member must read cross-ws M:N project. Plan "
            "§Phase A — update project_rule_workspace_member walk path "
            "to ``cloud_service_ids.workspace_id.access_ids.user_id`` "
            "(M2M), mirroring the mcp_server_rule_workspace_user fix.",
        )

    # ------------------------------------------------------------------ #
    # O1.2 — W1 member also reads (regression lock)                       #
    # ------------------------------------------------------------------ #
    def test_O1_2_w1_member_can_read_cross_ws_project(self):
        """O1.2: Alice (W1 owner) must continue to read the same M:N
        project — leg-1 regression lock; must stay green after impl.
        """
        ids = self.Project.with_user(self.alice).search([
            ('id', '=', self.cross_ws_project.id),
        ]).ids
        self.assertEqual(
            ids, [self.cross_ws_project.id],
            "O1.2: W1 member must continue to read the cross-ws M:N "
            "project after the rule walks M2M (regression lock).",
        )

    # ------------------------------------------------------------------ #
    # O1.3 — task rule walks M:N too                                      #
    # ------------------------------------------------------------------ #
    def test_O1_3_w2_member_can_read_task_on_cross_ws_project(self):
        """O1.3: Bob (W2) must be able to search/read tasks belonging
        to the cross-ws project — task rule must mirror project rule's
        M:N walk via ``project_id.cloud_service_ids.workspace_id...``.

        Red today: project_task_rule_workspace_member walks the legacy
        M2O `project_id.cloud_service_id.workspace_id`.
        """
        ids = self.Task.with_user(self.bob).search([
            ('id', '=', self.cross_ws_task.id),
        ]).ids
        self.assertEqual(
            ids, [self.cross_ws_task.id],
            "O1.3: W2 member must read task on cross-ws M:N project. "
            "Plan §Phase A — update project_task_rule_workspace_member "
            "walk path to "
            "``project_id.cloud_service_ids.workspace_id.access_ids.user_id``.",
        )

    # ------------------------------------------------------------------ #
    # O1.4 — outsider W3 user cannot read (regression lock for deny)      #
    # ------------------------------------------------------------------ #
    def test_O1_4_w3_outsider_cannot_read_cross_ws_project(self):
        """O1.4: Eve (W3 owner, no W1/W2 access) must NOT see the
        cross-ws project — deny path stays green after M:N walk fix.

        Both today (legacy M2O walks W1 only, Eve not in W1) and after
        impl (M:N walks W1+W2, Eve not in either) the result is the
        same: empty search. This is a regression lock — the M:N walk
        fix must not accidentally over-grant.
        """
        ids = self.Project.with_user(self.eve).search([
            ('id', '=', self.cross_ws_project.id),
        ]).ids
        self.assertEqual(
            ids, [],
            "O1.4: W3 outsider must NOT see cross-ws M:N project "
            "after impl. Regression lock — M2M walk must not "
            "accidentally allow non-member.",
        )

    # ------------------------------------------------------------------ #
    # O1.5 — domain string assertions                                     #
    # ------------------------------------------------------------------ #
    def test_O1_5_rule_domains_walk_cloud_service_ids(self):
        """O1.5: Both project_rule_workspace_member +
        project_task_rule_workspace_member ir.rule domains must
        reference the M:N field ``cloud_service_ids`` (with ``_ids``
        suffix), not the legacy M2O ``cloud_service_id``.

        Red today: both rules literally walk ``cloud_service_id``
        without ``_ids`` segment.
        """
        for xmlid in (
            'woow_paas_platform.project_rule_workspace_member',
            'woow_paas_platform.project_task_rule_workspace_member',
        ):
            rule = self.env.ref(xmlid, raise_if_not_found=False)
            self.assertTrue(
                rule, f"O1.5 prerequisite: ir.rule '{xmlid}' not found.",
            )
            domain = rule.domain_force or ''
            self.assertIn(
                "cloud_service_ids", domain,
                f"O1.5: '{xmlid}' must walk M:N "
                f"``cloud_service_ids`` (with `_ids` suffix). Plan "
                f"§Phase A Task 4 / design §1.3.2 Q1 OR semantics.",
            )
            # Verify the legacy M2O segment (without `_ids` suffix) is
            # not the *primary* walk path. The literal string check
            # below would also match the new M2M form (``cloud_service_ids``
            # starts with ``cloud_service_id``), so look for a more
            # specific stale pattern.
            stale_patterns = (
                "cloud_service_id.workspace_id",
                "project_id.cloud_service_id.workspace_id",
                "('cloud_service_id', '=', False)",
                "('project_id.cloud_service_id', '=', False)",
            )
            for stale in stale_patterns:
                self.assertNotIn(
                    stale, domain,
                    f"O1.5: '{xmlid}' must not walk the deprecated "
                    f"M2O segment '{stale}'. Replace with the M2M "
                    f"`_ids` segment.",
                )
