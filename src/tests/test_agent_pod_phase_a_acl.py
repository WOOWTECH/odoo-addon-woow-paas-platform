"""TDD red phase — Phase A: ir.rule dual-leg M2M walk + new-model ACL parity.

Plan: docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase A Tasks 4, 12-13
Design: docs/plans/2026-Q2/agent-runtime-pod-design.md
  - §1.3.3 對 refactor branch 已 fix 範圍的明確引用（ACL walk path
    cardinality 改 M2M）
  - §7.0.5 ACL dual-leg domain walk path 改 M2M
  - §4.3 project_access ACL (4 rule × project_access pattern)
  - §3.2 skill ACL (仿 mcp 6-rule split + project_access walk)
  - §4.1 agent_template ACL (4 rule, M:N project owner walk)
  - §2.12 agent_session ACL (derives from project_access)

Decision doc invariants referenced (守線 — Phase A 落地後仍要綠):
  - [[mcp-url-redesign]] Inv #11 dual-leg shape preserved:
    外層 AND ``scope='user'`` + leg 1 bound walk OR leg 2 unbound
    carve-out via ``message_partner_ids``. Phase A 只改 walk-path
    cardinality（M2O → M2M），不破 dual-leg shape.
  - [[mcp-acl-6-rule-split]] 6-rule parity (mcp_server × 3 + mcp_tool × 3).
  - [[unbound-project-acl-dual-leg]] leg 2 shape.

Acceptance criteria covered:

R.1  ``mcp_server_rule_workspace_user`` domain leg 1 references
     ``project_id.cloud_service_ids.workspace_id.access_ids.user_id``
     (M2M walk path — adds the ``_ids`` segment) and leg 2
     ``project_id.cloud_service_ids = False`` (M2M unbound).
R.2  ``mcp_tool_rule_workspace_user`` 對等 sub-model mirror with
     ``server_id.`` prefix and same M2M walk.
R.3  4/6 不動 rules (``mcp_server_rule_system_readonly`` × 2,
     ``mcp_server_rule_admin_full`` × 2) preserve perm matrix.
R.4  R.1 + R.2 守 Inv #11 外層 AND ``scope='user'`` 防 MAJOR 1 (system
     row create direction guard).
R.5  Bound walk integration: Alice (W1 owner) creates a user-scope MCP
     attached to a bound project (project.cloud_service_ids contains a
     W1 cloud_service). Alice search() returns it. Bob (W2 only) cannot.
R.6  Unbound walk integration: Charlie (W1 member, follower of unbound
     project) can read user-scope MCP attached to unbound_project.
     Dave (W1 member, NOT follower) cannot.
R.7  Cross-workspace M:N walk: project bound to svc_A (W1) + svc_B
     (W2); W1 member can read project's user-scope MCP via leg 1 W1
     walk; W2 member can also read via leg 1 W2 walk (same project,
     different cloud_service → both walks succeed).
R.8  ``base.group_user`` 對新 4 models (agent_template / agent_session
     / skill / project_access) 有 ir.model.access.csv rows 註冊.
R.9  Existing 6 ir.rule for mcp_server / mcp_tool still 6 — no
     accidental removal/merge during Phase A walk-path update.

Red expectation today (Phase A implementation has NOT started):

* R.1, R.2 → RED: today ir_rules.xml line 162 / 198 walk path is
  ``project_id.cloud_service_id.workspace_id.access_ids.user_id`` (M2O,
  no ``_ids`` segment); assert that the new M2M segment is present
  fails.
* R.5, R.6, R.7 → RED: today the 1:N walk works for some sub-cases but
  cross-workspace M:N project leg 1 returns only the legacy single
  workspace; new walk should accept either workspace.
* R.8 → RED: ir.model.access.csv has no rows for the 4 new models.
* R.9 → meta-lock — Phase A walk-path update must not drop a rule.

Boundary:

* MAJOR 1 system-create guard regression already locked by existing
  ``test_mcp_acl_unbound_isolation.py::H.11/H.12``; this file only
  re-runs an equivalent assertion via the cross-workspace cells to
  catch a Phase A regression that drops the outer AND.
* admin_full preservation already covered by refactor-branch test
  ``test_admin_can_create_system_mcp_via_backend`` — not duplicated.
* New-model ACL rules detailed coverage (4 rule per model × admin /
  user / guest / non-member) is large — Phase A scope only covers
  ``ir.model.access.csv`` row registration (R.8); per-rule behavioural
  matrix is staged for Phase B test scope expansion when models are
  populated with real fixtures.
"""
import uuid

from odoo.tests.common import TransactionCase, tagged


def _new_internal_user(env, login, name=None, password=None):
    vals = {
        'name': name or login,
        'login': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    }
    if password:
        vals['password'] = password
    return env['res.users'].sudo().create(vals)


@tagged('post_install', '-at_install')
class TestPhaseAAclDualLegM2MWalk(TransactionCase):
    """R.1 ~ R.7 — dual-leg ir.rule walk path migrated M2O → M2M."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']
        cls.McpTool = cls.env['woow_paas_platform.mcp_tool']
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.WAccess = cls.env['woow_paas_platform.workspace_access']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Project = cls.env['project.project']
        cls.IrRule = cls.env['ir.rule']

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.suffix = suffix
        # NOTE: cloud_service_ids prereq is checked *per integration test*
        # (R.5/R.6/R.7) rather than class-level — rule-domain-string tests
        # (R.1/R.2) and rule-existence tests (R.3/R.9) read ir_rules.xml
        # records directly and must run independently of M:N schema state.
        self._m2m_missing = 'cloud_service_ids' not in self.Project._fields

        self.alice = _new_internal_user(self.env, f'r_alice_{suffix}', 'Alice (R)')
        self.bob = _new_internal_user(self.env, f'r_bob_{suffix}', 'Bob (R)')
        self.charlie = _new_internal_user(self.env, f'r_charlie_{suffix}', 'Charlie (R)')
        self.dave = _new_internal_user(self.env, f'r_dave_{suffix}', 'Dave (R)')

        self.ws_a = self.Workspace.sudo().create({
            'name': f'R W1 Alice {suffix}',
            'owner_id': self.alice.id,
        })
        self.ws_b = self.Workspace.sudo().create({
            'name': f'R W2 Bob {suffix}',
            'owner_id': self.bob.id,
        })
        # Charlie + Dave join W1.
        self.WAccess.sudo().create({
            'workspace_id': self.ws_a.id,
            'user_id': self.charlie.id,
            'role': 'user',
        })
        self.WAccess.sudo().create({
            'workspace_id': self.ws_a.id,
            'user_id': self.dave.id,
            'role': 'user',
        })

        self.tmpl = self.Template.sudo().create({
            'name': f'R Tmpl {suffix}',
            'slug': f'r-tmpl-{suffix}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': f'r-tmpl-{suffix}',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.svc_a = self.Service.sudo().create({
            'name': f'R Svc A {suffix}',
            'workspace_id': self.ws_a.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'r-svc-a-{suffix}',
            'helm_namespace': f'paas-ws-r-a-{suffix}',
        })
        self.svc_b = self.Service.sudo().create({
            'name': f'R Svc B {suffix}',
            'workspace_id': self.ws_b.id,
            'template_id': self.tmpl.id,
            'helm_release_name': f'r-svc-b-{suffix}',
            'helm_namespace': f'paas-ws-r-b-{suffix}',
        })

        # M:N-dependent fixtures (cross_ws_project / bound_w1_only_project /
        # MCPs) are only built when cloud_service_ids field exists; the
        # rule-domain-string tests don't need them.
        if not self._m2m_missing:
            self.cross_ws_project = self.Project.sudo().create({
                'name': f'R cross-ws project {suffix}',
                'cloud_service_ids': [(6, 0, [self.svc_a.id, self.svc_b.id])],
            })
            self.bound_w1_only_project = self.Project.sudo().create({
                'name': f'R bound W1-only {suffix}',
                'cloud_service_ids': [(6, 0, [self.svc_a.id])],
            })
            self.unbound_project = self.Project.sudo().create({
                'name': f'R unbound project {suffix}',
            })
            self.unbound_project.message_subscribe(
                partner_ids=[
                    self.alice.partner_id.id,
                    self.charlie.partner_id.id,
                ],
            )

            self.mcp_bound_w1 = self.McpServer.sudo().create({
                'name': f'R bound W1 mcp {suffix}',
                'url': 'http://mcp-w1.example/sse',
                'transport': 'sse',
                'scope': 'user',
                'project_id': self.bound_w1_only_project.id,
            })
            self.mcp_cross_ws = self.McpServer.sudo().create({
                'name': f'R cross-ws mcp {suffix}',
                'url': 'http://mcp-cross.example/sse',
                'transport': 'sse',
                'scope': 'user',
                'project_id': self.cross_ws_project.id,
            })
            self.mcp_unbound = self.McpServer.sudo().create({
                'name': f'R unbound mcp {suffix}',
                'url': 'http://mcp-unbound.example/sse',
                'transport': 'sse',
                'scope': 'user',
                'project_id': self.unbound_project.id,
            })

    def _skip_if_m2m_missing(self):
        """Per-integration-test gate — used by R.5/R.6/R.7."""
        if self._m2m_missing:
            self.skipTest(
                "R.5/R.6/R.7 prerequisite: project.cloud_service_ids "
                "Many2many missing — see "
                "test_agent_pod_phase_a_project_schema.py:A.1"
            )

    # ------------------------------------------------------------------ #
    # R.1 — mcp_server_rule_workspace_user walk path is M2M               #
    # ------------------------------------------------------------------ #
    def test_mcp_server_rule_workspace_user_walks_cloud_service_ids(self):
        """R.1: domain leg 1 + leg 2 reference ``cloud_service_ids`` (M2M).

        Red today: domain references ``cloud_service_id`` (M2O).
        """
        rule = self.IrRule.sudo().search([
            ('name', '=', 'McpServer: workspace member CRUD on user-scope'),
        ], limit=1)
        if not rule:
            # Fallback by xml_id pattern; refactor-branch xml id is
            # mcp_server_rule_workspace_user.
            rule = self.env.ref(
                'woow_paas_platform.mcp_server_rule_workspace_user',
                raise_if_not_found=False,
            )
        self.assertTrue(
            rule, "R.1 prerequisite: mcp_server_rule_workspace_user ir.rule "
                  "not found by name or xmlid.",
        )
        domain = rule.domain_force or ''
        self.assertIn(
            "cloud_service_ids", domain,
            "R.1: domain must walk M:N field "
            "``project_id.cloud_service_ids`` (with `_ids` suffix). "
            "Plan §Phase A Task 4 / design §7.0.5.",
        )
        self.assertNotIn(
            "'project_id.cloud_service_id.", domain,
            "R.1: domain must NOT walk the deprecated M2O "
            "``project_id.cloud_service_id`` (with NO `_ids` suffix). "
            "Plan §Phase A Task 4 — M2M migration.",
        )
        # Outer AND `scope='user'` 守線 (Inv #11).
        self.assertIn(
            "'scope'", domain,
            "R.4: outer AND clause must include ``('scope', '=', 'user')`` "
            "— Inv #11 MAJOR 1 防線根基. Plan §Phase A — must not drop.",
        )
        self.assertIn(
            "'user'", domain,
            "R.4: scope='user' literal must remain in outer AND.",
        )

    # ------------------------------------------------------------------ #
    # R.2 — mcp_tool sub-model mirror M2M walk                            #
    # ------------------------------------------------------------------ #
    def test_mcp_tool_rule_workspace_user_walks_cloud_service_ids(self):
        """R.2: sub-model mirror via ``server_id.`` prefix; same M2M walk."""
        rule = self.env.ref(
            'woow_paas_platform.mcp_tool_rule_workspace_user',
            raise_if_not_found=False,
        )
        self.assertTrue(
            rule, "R.2 prerequisite: mcp_tool_rule_workspace_user not found.",
        )
        domain = rule.domain_force or ''
        self.assertIn(
            "server_id.project_id.cloud_service_ids", domain,
            "R.2: sub-model rule must walk "
            "``server_id.project_id.cloud_service_ids`` (B1 sub-model "
            "parity). Plan §Phase A Task 4.",
        )

    # ------------------------------------------------------------------ #
    # R.3 — 4/6 不動 rules preserved                                       #
    # ------------------------------------------------------------------ #
    def test_other_four_rules_intact(self):
        """R.3 + R.9: ``system_readonly`` × 2 + ``admin_full`` × 2 still
        present after Phase A.
        """
        for xmlid, expect_read in (
            ('woow_paas_platform.mcp_server_rule_system_readonly', True),
            ('woow_paas_platform.mcp_tool_rule_system_readonly', True),
            ('woow_paas_platform.mcp_server_rule_admin_full', True),
            ('woow_paas_platform.mcp_tool_rule_admin_full', True),
        ):
            rule = self.env.ref(xmlid, raise_if_not_found=False)
            self.assertTrue(
                rule,
                "R.3: rule '%s' must still exist after Phase A. Plan "
                "§Phase A Task 4 — only walk-path migrates, the 4 other "
                "rules stay untouched." % xmlid,
            )
            self.assertEqual(
                rule.perm_read, expect_read,
                f"R.3: perm_read on '{xmlid}' must stay {expect_read}.",
            )

    # R.9（6-rule split parity）刻意不另立計數測試：那 6 條規則已由上方三條逐條
    # xmlid 守住（R.1 workspace_user、R.2 tool workspace_user、R.3 system_readonly
    # ×2 ＋ admin_full ×2）。曾經存在的 ``test_mcp_acl_rule_count_parity`` 用
    # ``search_count(...) >= 6`` 冒充 parity invariant：它的 domain 只有
    # ``model_id.model in (...)``（註解宣稱要過濾 module-defined，實際上沒有這個
    # 條件），所以「刪一條 + 補一條無關的」永遠不會紅，而且 >= 讓新增規則也永不
    # 紅——不變式一件都守不住，卻掛著 parity 的名號讓人以為規則數量有守。

    # ------------------------------------------------------------------ #
    # R.5 — bound walk integration (M2M leg 1)                           #
    # ------------------------------------------------------------------ #
    def test_bound_walk_member_can_read_bound_project_mcp(self):
        """R.5: Alice (W1 owner) reads bound-W1-only project's user-scope
        MCP. Bob (W2 only) cannot.

        Red today: cross-workspace MCP search would over-grant or
        under-grant when domain still walks single M2O.
        """
        self._skip_if_m2m_missing()
        Mcp_user = self.McpServer.with_user(self.alice)
        ids_alice = Mcp_user.search([
            ('id', '=', self.mcp_bound_w1.id),
        ]).ids
        self.assertEqual(
            ids_alice, [self.mcp_bound_w1.id],
            "R.5: Alice (W1 owner) must read bound-W1 mcp via leg 1 walk.",
        )

        Mcp_bob = self.McpServer.with_user(self.bob)
        ids_bob = Mcp_bob.search([
            ('id', '=', self.mcp_bound_w1.id),
        ]).ids
        self.assertEqual(
            ids_bob, [],
            "R.5: Bob (W2 only) must NOT read W1's bound user-scope "
            "MCP — cross-workspace deny. Plan §Phase A — leg 1 walk "
            "still respects workspace boundary post M:N migration.",
        )

    # ------------------------------------------------------------------ #
    # R.6 — unbound walk integration (leg 2 follower carve-out)           #
    # ------------------------------------------------------------------ #
    def test_unbound_walk_follower_only(self):
        """R.6: Charlie (W1 member + follower) reads unbound_project's
        user-scope MCP via leg 2. Dave (W1 member, NOT follower) cannot.

        Red today: leg 2 carve-out reference may still walk M2O
        ``cloud_service_id = False`` instead of M2M
        ``cloud_service_ids = False``; the new shape must match the
        empty-M2M check semantics correctly.
        """
        self._skip_if_m2m_missing()
        Mcp_charlie = self.McpServer.with_user(self.charlie)
        ids_charlie = Mcp_charlie.search([
            ('id', '=', self.mcp_unbound.id),
        ]).ids
        self.assertEqual(
            ids_charlie, [self.mcp_unbound.id],
            "R.6: Charlie (follower of unbound project) must read via "
            "leg 2 ``message_partner_ids`` walk. Plan §Phase A — "
            "carve-out condition migrates to "
            "``project_id.cloud_service_ids = False``.",
        )

        Mcp_dave = self.McpServer.with_user(self.dave)
        ids_dave = Mcp_dave.search([
            ('id', '=', self.mcp_unbound.id),
        ]).ids
        self.assertEqual(
            ids_dave, [],
            "R.6: Dave (non-follower) must NOT read unbound user-scope "
            "MCP under leg 2 — follower-only carve-out (Inv #11 leg 2 "
            "shape preserved through M2M migration).",
        )

    # ------------------------------------------------------------------ #
    # R.7 — cross-workspace M:N walk lets both workspaces' members in    #
    # ------------------------------------------------------------------ #
    def test_cross_workspace_project_mcp_visible_to_both_workspaces(self):
        """R.7: project binds svc_A (W1) + svc_B (W2); user-scope MCP
        on that project must be readable by Alice (W1) AND Bob (W2)
        via leg 1 — both workspaces' walk succeeds because
        ``project.cloud_service_ids`` overlaps with each user's
        ``workspace_access``.

        Red today: 1:N walk picks ONE workspace via the legacy M2O,
        so half the cross-workspace members lose access.
        """
        self._skip_if_m2m_missing()
        Mcp_alice = self.McpServer.with_user(self.alice).search([
            ('id', '=', self.mcp_cross_ws.id),
        ])
        Mcp_bob = self.McpServer.with_user(self.bob).search([
            ('id', '=', self.mcp_cross_ws.id),
        ])
        self.assertEqual(
            Mcp_alice.ids, [self.mcp_cross_ws.id],
            "R.7: Alice (W1) must read cross-workspace project mcp.",
        )
        self.assertEqual(
            Mcp_bob.ids, [self.mcp_cross_ws.id],
            "R.7: Bob (W2) must ALSO read cross-workspace project mcp "
            "— M:N walk picks BOTH workspaces. Plan §Phase A — M2M "
            "walk-path migration.",
        )


@tagged('post_install', '-at_install')
class TestPhaseANewModelAclCsvRegistered(TransactionCase):
    """R.8 — ir.model.access.csv rows for 4 new models."""

    REQUIRED_MODELS = (
        'woow_paas_platform.agent_template',
        'woow_paas_platform.agent_session',
        'woow_paas_platform.skill',
        'woow_paas_platform.project_access',
    )

    def setUp(self):
        super().setUp()
        self.IrAccess = self.env['ir.model.access']

    def test_user_group_access_row_present_for_each_new_model(self):
        """R.8: ``ir.model.access.csv`` declares at least one row per
        new model granting ``base.group_user`` perm_read.

        Red today: csv has no rows for the 4 new models.
        """
        for model in self.REQUIRED_MODELS:
            rows = self.IrAccess.sudo().search([
                ('model_id.model', '=', model),
                ('group_id', '=', self.env.ref('base.group_user').id),
            ])
            self.assertTrue(
                rows,
                f"R.8: ir.model.access.csv missing base.group_user row "
                f"for '{model}'. Plan §Phase A Task 12 — add per-model "
                f"access rows.",
            )
            # At least one row must allow read.
            self.assertTrue(
                any(r.perm_read for r in rows),
                f"R.8: at least one base.group_user access row for "
                f"'{model}' must grant perm_read.",
            )
