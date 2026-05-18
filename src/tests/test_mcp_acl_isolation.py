"""TDD red phase tests — MCP server / tool cross-workspace ACL isolation.

Plan: docs/refactor/plan-mcp-project-decouple.md §ACL (AC #17-21)
Decision: docs/refactor/decisions/mcp-project-decouple.md §Invariants 5, 6
Patterns: paas-workspace-member-acl-tightening.md
Pitfall: cross-workspace-task-data-leak.md (B1 — sub-model symmetric deny)

This file is the **B1-style regression lock** for mcp_server / mcp_tool ACL.

Pre-refactor state (today, 2026-05-18):

* ``ir.model.access.csv`` grants ``base.group_user`` ``read=1`` on
  ``mcp_server`` and ``mcp_tool`` (rows 14, 16).
* **No ir.rule** is registered for either model.
* Effective ACL: any internal user can ``search`` / ``browse().read()``
  every mcp_server and mcp_tool in the instance — pre-existing leak
  (see ``decisions/mcp-project-decouple.md §Context``).

Post-refactor expectation (AC #17, #18):

* ``mcp_server_rule_workspace_member`` (base.group_user) with domain
  ``['|', ('scope', '=', 'system'),
        ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)]``
* ``mcp_tool_rule_workspace_member`` (base.group_user) with the same
  shape but walking ``server_id`` for sub-model symmetry — **missing
  this is the B1 leak pattern**.

Red expectations for each case (clean ``AssertionError``):

1. ``test_alice_cannot_read_bob_user_mcp_via_orm`` —
   ``search()`` currently returns the row → ``assertFalse(visible)`` raises.
2. ``test_alice_cannot_read_bob_user_mcp_tool`` —
   sub-model search returns the row → same red shape.
3. ``test_bob_can_read_own_workspace_mcp`` — should stay green
   throughout (regression lock against over-correction to blanket deny).
4. ``test_system_mcp_readable_by_all_users`` — should stay green
   throughout (Invariant #6: system leg is intentionally always-true).
"""
from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestMcpAclCrossWorkspaceIsolation(TransactionCase):
    """B1-style — base.group_user must not read foreign-workspace MCP rows."""

    def setUp(self):
        super().setUp()
        self.McpServer = self.env['woow_paas_platform.mcp_server']
        self.McpTool = self.env['woow_paas_platform.mcp_tool']

        if 'project_id' not in self.McpServer._fields:
            self.fail(
                "mcp_server.project_id field missing — ACL tests rely on "
                "the new domain "
                "(project_id.cloud_service_id.workspace_id.access_ids...). "
                "Plan §D1 / AC #1."
            )

        Workspace = self.env['woow_paas_platform.workspace']
        Template = self.env['woow_paas_platform.cloud_app_template']
        Service = self.env['woow_paas_platform.cloud_service']
        Project = self.env['project.project']
        User = self.env['res.users']
        group_user = self.env.ref('base.group_user')

        # Two internal users with no overlapping workspace access.
        self.alice = User.create({
            'login': 'mcp_acl_alice',
            'name': 'Alice (MCP-ACL)',
            'email': 'alice-mcp-acl@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })
        self.bob = User.create({
            'login': 'mcp_acl_bob',
            'name': 'Bob (MCP-ACL)',
            'email': 'bob-mcp-acl@test.example',
            'groups_id': [(6, 0, [group_user.id])],
        })

        # owner_id auto-adds to access_ids in workspace.create.
        self.ws_alice = Workspace.create({
            'name': 'W1 Alice (MCP-ACL)', 'owner_id': self.alice.id,
        })
        self.ws_bob = Workspace.create({
            'name': 'W2 Bob (MCP-ACL)', 'owner_id': self.bob.id,
        })

        self.template = Template.sudo().create({
            'name': 'Tmpl MCP-ACL',
            'slug': 'tmpl-mcp-acl',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-mcp-acl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })

        # Bob's W2: bound service + project + user-scope MCP + one tool.
        self.svc_bob = Service.sudo().create({
            'name': 'Svc Bob (MCP-ACL)',
            'workspace_id': self.ws_bob.id,
            'template_id': self.template.id,
            'helm_release_name': 'bob-svc',
            'helm_namespace': 'paas-ws-bob',
        })
        self.project_bob = Project.sudo().create({
            'name': 'Project Bob (MCP-ACL)',
            'cloud_service_id': self.svc_bob.id,
        })
        self.mcp_bob = self.McpServer.sudo().create({
            'name': 'Bob User MCP',
            'url': 'http://bob-svc-mcp.paas-ws-bob.svc.cluster.local:3001/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project_bob.id,
        })
        self.tool_bob = self.McpToolSudo = self.McpTool.sudo().create({
            'name': 'bob_internal_tool',
            'description': 'Tool exposed by Bob\'s sidecar — must be private.',
            'input_schema': '{}',
            'server_id': self.mcp_bob.id,
        })

        # Alice's W1: own service + project + user MCP (sanity / positive test).
        self.svc_alice = Service.sudo().create({
            'name': 'Svc Alice (MCP-ACL)',
            'workspace_id': self.ws_alice.id,
            'template_id': self.template.id,
            'helm_release_name': 'alice-svc',
            'helm_namespace': 'paas-ws-alice',
        })
        self.project_alice = Project.sudo().create({
            'name': 'Project Alice (MCP-ACL)',
            'cloud_service_id': self.svc_alice.id,
        })
        self.mcp_alice = self.McpServer.sudo().create({
            'name': 'Alice User MCP',
            'url': 'http://alice-svc-mcp.paas-ws-alice.svc.cluster.local:3001/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project_alice.id,
        })

        # A system-scope MCP (no project) for the public-readability test.
        self.system_mcp = self.McpServer.sudo().create({
            'name': 'Platform System MCP',
            'url': 'http://platform.mcp/sse',
            'transport': 'sse',
            'scope': 'system',
        })

    # ------------------------------------------------------------------ #
    # AC #19: cross-workspace user must not see another ws's user MCP     #
    # ------------------------------------------------------------------ #
    def test_alice_cannot_read_bob_user_mcp_via_orm(self):
        """AC #19: ORM search + browse.read must deny across workspaces.

        Mirrors the B1 regression pattern (test_cross_workspace_task_isolation):
        both ``search`` (silent filter) and ``browse().read()`` (raises
        AccessError) must be denied.  Today the rule is missing, so both
        paths succeed — the test fails with two clean ``AssertionError``s.
        """
        # 1. search() — record-rule denial is a silent filter.
        visible = self.McpServer.with_user(self.alice).search([
            ('id', '=', self.mcp_bob.id),
        ])
        self.assertFalse(
            visible,
            "AC #19 violated: Alice (W1) can search() Bob's W2 user-scope "
            "mcp_server. mcp_server_rule_workspace_member missing — see "
            "plan §AC #17 for the required domain.",
        )

        # 2. browse().read() — explicit ACL check, raises when blocked.
        mcp_as_alice = self.McpServer.with_user(self.alice).browse(
            self.mcp_bob.id,
        )
        with self.assertRaises(
            AccessError,
            msg=(
                "AC #19 violated: browse().read() on Bob's W2 mcp_server "
                "succeeded for Alice — AccessError expected once "
                "mcp_server_rule_workspace_member is the active rule on "
                "base.group_user."
            ),
        ):
            mcp_as_alice.read(['name', 'url'])

    # ------------------------------------------------------------------ #
    # AC #19 / B1 sub-model: mcp_tool must mirror mcp_server deny         #
    # ------------------------------------------------------------------ #
    def test_alice_cannot_read_bob_user_mcp_tool(self):
        """AC #18 / B1 pattern: mcp_tool must enforce the same deny.

        Sub-model rules are easy to forget — B1 caught this on project.task
        (see pitfalls/cross-workspace-task-data-leak.md).  ``mcp_tool``
        has no ``project_id`` of its own; the rule must walk through
        ``server_id.project_id.cloud_service_id.workspace_id.access_ids...``
        for symmetry.  Without it, Alice can search bob_internal_tool and
        learn the existence + name of Bob's private tools.
        """
        visible = self.McpTool.with_user(self.alice).search([
            ('id', '=', self.tool_bob.id),
        ])
        self.assertFalse(
            visible,
            "AC #18 / B1 sub-model leak: Alice can search() Bob's "
            "mcp_tool — mcp_tool_rule_workspace_member is missing or "
            "the domain doesn't walk through server_id. See B1 pattern "
            "(pitfalls/cross-workspace-task-data-leak.md): every "
            "model-level rule needs a sub-model counterpart.",
        )

        tool_as_alice = self.McpTool.with_user(self.alice).browse(
            self.tool_bob.id,
        )
        with self.assertRaises(
            AccessError,
            msg=(
                "AC #18 / B1 sub-model leak: browse().read() on Bob's "
                "mcp_tool succeeded for Alice. mcp_tool_rule_workspace_member "
                "with domain "
                "[('|', ('server_id.scope', '=', 'system'), "
                "  ('server_id.project_id.cloud_service_id."
                "workspace_id.access_ids.user_id', '=', user.id))] "
                "is missing."
            ),
        ):
            tool_as_alice.read(['name', 'description'])

    # ------------------------------------------------------------------ #
    # AC #20: own-workspace allow (regression lock against over-correction)#
    # ------------------------------------------------------------------ #
    def test_bob_can_read_own_workspace_mcp(self):
        """AC #20: Bob retains read access to his own W2's user MCP.

        Guards against the implementation tightening the rule to a blanket
        deny ('1=0' or a wrong domain).  Bob is on ``access_ids`` of
        ``ws_bob`` (auto-added via workspace.create) so the chain
        ``project_id.cloud_service_id.workspace_id.access_ids.user_id ==
        bob`` evaluates True.
        """
        visible = self.McpServer.with_user(self.bob).search([
            ('id', '=', self.mcp_bob.id),
        ])
        self.assertEqual(
            visible, self.mcp_bob,
            "AC #20 regression: Bob lost read access to his own W2 "
            "user MCP. Domain must not collapse to a blanket deny.",
        )

        # And browse().read() must work too.
        mcp_as_bob = self.McpServer.with_user(self.bob).browse(self.mcp_bob.id)
        self.assertEqual(
            mcp_as_bob.read(['name'])[0]['name'], 'Bob User MCP',
            "AC #20 regression: Bob's browse().read() on own MCP failed.",
        )

    # ------------------------------------------------------------------ #
    # AC #17 (system leg) / Invariant #6: system MCP readable by all      #
    # ------------------------------------------------------------------ #
    def test_system_mcp_readable_by_all_users(self):
        """AC #17 system leg / Invariant #6: scope='system' is universally readable.

        The OR leg ``('scope', '=', 'system')`` is intentionally always-true
        for any base.group_user — system MCPs are platform tools (see
        decisions/mcp-project-decouple.md §Invariant 6 — explicitly *not*
        a B1-style OR-bypass bug).

        Verifies Alice and Bob (different workspaces, neither involved in
        the system MCP's lifecycle) both see it.
        """
        for user, label in ((self.alice, 'Alice (W1)'), (self.bob, 'Bob (W2)')):
            visible = self.McpServer.with_user(user).search([
                ('id', '=', self.system_mcp.id),
            ])
            self.assertEqual(
                visible, self.system_mcp,
                f"Invariant #6: {label} cannot see scope='system' MCP — "
                f"the system leg of mcp_server_rule_workspace_member must "
                f"be always-true. (This is intentional — see Invariant #6 "
                f"long-form comment; not a B1-style OR-bypass bug.)",
            )

    # ------------------------------------------------------------------ #
    # Tester Re-Open #1 (2026-05-18) — Gap 2: AC #21 CSV CRUD 4-leg       #
    #                                                                    #
    # AC #20 covers the READ leg of                                      #
    # ``ir.model.access.csv:access_mcp_server_user`` (perm bits change   #
    # from ``1,0,0,0`` → ``1,1,1,1``) — Bob can search/read his own MCP. #
    # But a CSV typo like ``1,1,0,0`` would still let Bob read while     #
    # blocking create/write/unlink — Test #20 alone misses that.         #
    #                                                                    #
    # This case exercises the remaining 3 legs (CREATE / WRITE / UNLINK) #
    # in one parametrised method on the same workspace-member positive   #
    # path used by Test #20.  Confirms the full CRUD CSV bits flipped    #
    # AND the rule's perm_write / perm_create / perm_unlink are True.    #
    # ------------------------------------------------------------------ #
    def test_ac21_bob_can_crud_own_workspace_mcp(self):
        """AC #21 / AC #20 sanity: 4-leg CRUD positive path.

        Sub-cases (single test method, 3 ops in sequence over a fresh
        target row — kept atomic so a partial CSV flip surfaces as a
        single test red, not three):

        1. CREATE — Bob creates a new user-scope mcp_server bound to his
           own workspace's project. Must succeed.
        2. WRITE  — Bob writes a benign field on the just-created row.
           Must succeed.
        3. UNLINK — Bob unlinks the row. Must succeed.

        Today (red): CSV row reads ``1,0,0,0`` so CREATE raises
        ``AccessError`` immediately — clean ``AssertionError`` from
        ``self.fail(...)`` catch. Even after CSV opens, the row needs
        ``mcp_server.project_id`` to exist and the rule's
        ``perm_create``/``perm_write``/``perm_unlink`` to be True.
        """
        from odoo.exceptions import AccessError

        # The fixture's setUp() field-guard already short-circuits when
        # project_id is missing, so by the time we reach here the schema
        # is at least field-ready.

        BobMcpServer = self.McpServer.with_user(self.bob)

        # ---- CREATE ----
        try:
            new_mcp = BobMcpServer.create({
                'name': 'Bob Own-WS CRUD Test MCP',
                'url': 'http://bob-svc-mcp.paas-ws-bob.svc.cluster.local:3001/mcp',
                'transport': 'streamable_http',
                'scope': 'user',
                'project_id': self.project_bob.id,
            })
        except AccessError as exc:
            self.fail(
                f"AC #21 CREATE leg: Bob cannot create a user MCP in his "
                f"own workspace W2. CSV bit perm_create likely still 0 "
                f"on access_mcp_server_user (or the rule's perm_create "
                f"flag is False). Raised: {exc}"
            )
        self.assertTrue(
            new_mcp.exists(),
            "AC #21 CREATE leg: created record did not materialise.",
        )

        # ---- WRITE ----
        try:
            new_mcp.write({'description': 'CRUD leg sanity update.'})
        except AccessError as exc:
            self.fail(
                f"AC #21 WRITE leg: Bob cannot write to his own W2 MCP. "
                f"CSV bit perm_write likely still 0 (or rule's "
                f"perm_write flag False). Raised: {exc}"
            )
        # Re-read with bob context to confirm the change landed visibly.
        self.assertEqual(
            new_mcp.description, 'CRUD leg sanity update.',
            "AC #21 WRITE leg: write call returned without error but "
            "value did not update.",
        )

        # ---- UNLINK ----
        target_id = new_mcp.id
        try:
            new_mcp.unlink()
        except AccessError as exc:
            self.fail(
                f"AC #21 UNLINK leg: Bob cannot unlink his own W2 MCP. "
                f"CSV bit perm_unlink likely still 0 (or rule's "
                f"perm_unlink flag False). Raised: {exc}"
            )
        # Confirm the row is gone (use sudo to bypass any post-deletion
        # rule weirdness — we are checking DB state, not visibility).
        self.assertFalse(
            self.McpServer.sudo()
            .with_context(active_test=False)
            .browse(target_id).exists(),
            "AC #21 UNLINK leg: unlink succeeded silently but row "
            "remained — investigate ondelete cascade or rule blocking "
            "physical delete.",
        )
