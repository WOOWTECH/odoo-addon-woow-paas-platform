"""TDD red phase — ACL dual-leg domain + ai_assistant carve-out for unbound
projects (合 issue #51 — OR-7 (c) message_partner_ids walk).

Plan: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
  - AC #38: ``mcp_server_rule_workspace_user`` dual-leg domain
    (bound workspace path OR unbound message_partner_ids carve-out).
  - AC #39: ``mcp_tool_rule_workspace_user`` 對等 dual-leg via
    ``server_id.`` prefix (B1 sub-model parity).
  - AC #40: 4/6 other rules (system_readonly / admin_full × 2) 不動.
  - AC #41: R-4 (model-layer ``@api.constrains`` on
    ``project.project.cloud_service_id``) 不補.
  - AC #42: ``ai_assistant.py:_check_project_access`` unbound carve-out
    使用 ``user_partner in project.message_partner_ids``（aligned with
    ir.rule leg 2）.
  - AC #43: task path / list path 不擴 (out of scope — F4 / F10 follow-up).

OR-X9 守線（MAJOR 級 blast radius）:
  - workspace_user + mcp_tool_workspace_user 改 dual-leg 後，attack surface
    最大威脅是 **leg 2 對 perm_create direction 永真** → user 可透過 ORM
    ``create({'scope': 'system', ...})`` 創建 system-scope row（domain shape
    寫錯時 leg 2 sub-leg2 ``message_partner_ids`` 對 new row 永真，且 leg
    上層 OR 對 scope=system 也永真）。
  - 守線 = 跑與 PR #47 MAJOR 1 case 1 等價的 ``base.group_user.create
    ({'scope': 'system', ...})`` 必須 raise AccessError（H.11 / H.12）.

Design context (OQ-7 wave 2 + OQ-8 wave 2 + OQ-3 wave 1):
  - 不擴 task path（``_check_task_access``）— F4 attack surface 留 follow-up
  - 不擴 list path（``_list_projects``）— F10 attack surface 留 follow-up
  - 兩層 ACL 對齊（ir.rule + controller）防 IDOR partial fix

Red expectation today (pre-implementation, 2026-05-20):

* H.1-H.4 mcp_server unbound ACL — RED：今天 ir.rule 是 single-leg
  ``('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=',
  user.id)``，unbound project 的 mcp_server 整條 walk 全 False → 沒人看得
  到（連 owner 自己都看不到）→ H.1 RED.
* H.3-H.4 non-follower / cross-ws deny — 今天 single-leg 已 deny（pass
  trivially），post-refactor dual-leg 後仍 deny（regression lock）.
* H.5-H.6 mcp_tool sub-model 對等 — 同 H.1-H.4 結構.
* H.7-H.9 ai_assistant carve-out — RED：today line 49-51 直接
  ``return True`` 對 unbound project → non-follower 也通過 → H.8 / H.9 RED.
* H.10 leg-1 bound preserve — GREEN today + GREEN post-refactor (regression
  lock — leg 1 unchanged).
* H.11-H.12 OR-X9 system-create guard — GREEN today (既有 PR #47 MAJOR 1
  case 1 等價 test 已綠)，post-refactor dual-leg 後**必須仍 GREEN** — 若
  dual-leg domain shape 寫錯導致 leg 2 對 system create 永真，H.11 / H.12
  會 RED → 立刻 surface MAJOR 1 重炸.
"""
from odoo.tests.common import TransactionCase, HttpCase, tagged
from odoo.exceptions import AccessError

import json as json_lib
import uuid
from ..models.cloud_service import _internal_provision


def _new_internal_user(env, login, name=None, with_password=False):
    """Create internal user (helper).

    ``with_password=True`` for users that need HttpCase ``authenticate``
    (H.9 endpoint integration test); others use plain ``with_user``.
    """
    vals = {
        'name': name or login,
        'login': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    }
    if with_password:
        vals['password'] = login
    return env['res.users'].sudo().create(vals)


# =========================================================================== #
# H group fixture — Alice/Bob/Charlie/Dave/Eve mirror cross_workspace pattern  #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestMcpAclUnboundIsolation(TransactionCase):
    """H.1-H.6, H.10, H.11, H.12 — ORM-level dual-leg ACL.

    Layout::

        W1 (Alice = owner, access_ids+follower of all W1 projects)
          ├ Charlie (W1 member, ALSO follower of unbound project)
          ├ Dave    (W1 member, NOT follower of unbound project)
          └ unbound_project (cloud_service_id=NULL, message_partner_ids
                             ⊆ {Alice, Charlie} via subscribe)
                └ external_mcp (scope='user', sidecar_backed=False,
                                project_id=unbound_project)
                └ external_mcp_tool (server_id=external_mcp)

        W2 (Bob = owner)
          └ (Bob has no overlap with W1 unbound project)
    """

    def setUp(self):
        super().setUp()
        self.McpServer = self.env['woow_paas_platform.mcp_server']
        self.McpTool = self.env['woow_paas_platform.mcp_tool']
        Workspace = self.env['woow_paas_platform.workspace']
        Project = self.env['project.project']
        WorkspaceAccess = self.env['woow_paas_platform.workspace_access']

        if 'project_id' not in self.McpServer._fields:
            self.fail(
                "Fixture-side schema guard: mcp_server.project_id field "
                "missing — PR #47 not yet applied? H group tests rely on "
                "the project_id walk introduced in 18.0.1.0.24."
            )

        self.alice = _new_internal_user(self.env, 'h_alice', 'Alice (H-ACL)')
        self.bob = _new_internal_user(self.env, 'h_bob', 'Bob (H-ACL)')
        self.charlie = _new_internal_user(self.env, 'h_charlie', 'Charlie (H-ACL)')
        self.dave = _new_internal_user(self.env, 'h_dave', 'Dave (H-ACL)')

        self.ws_alice = Workspace.sudo().create({
            'name': 'H W1 Alice', 'owner_id': self.alice.id,
        })
        self.ws_bob = Workspace.sudo().create({
            'name': 'H W2 Bob', 'owner_id': self.bob.id,
        })

        # Charlie + Dave join W1 (both have workspace membership).
        # role values come from workspace_access_role module — 'user'
        # is the lowest non-admin tier.
        WorkspaceAccess.sudo().create({
            'workspace_id': self.ws_alice.id,
            'user_id': self.charlie.id,
            'role': 'user',
        })
        WorkspaceAccess.sudo().create({
            'workspace_id': self.ws_alice.id,
            'user_id': self.dave.id,
            'role': 'user',
        })

        # Unbound project — NO cloud_service_id.
        self.unbound_project = Project.sudo().create({
            'name': 'H Unbound W1 Project',
        })
        # Alice = create_uid + auto-subscribe by Odoo std.
        # Charlie explicitly subscribed → ``message_partner_ids in
        # [charlie.partner]``.
        self.unbound_project.message_subscribe(
            partner_ids=[self.alice.partner_id.id, self.charlie.partner_id.id],
        )
        # Dave intentionally NOT subscribed — non-follower.

        # External (user-scope, sidecar_backed=False) MCP attached to
        # unbound project. **Only legal if D5 was already dropped.**
        # In the red phase D5 still raises ValidationError — H tests will
        # surface that as the canonical red message.
        try:
            self.external_mcp = self.McpServer.sudo().create({
                'name': 'H External MCP on Unbound',
                'url': 'https://mcp.example.com/sse',
                'transport': 'sse',
                'scope': 'user',
                'project_id': self.unbound_project.id,
            })
        except Exception as exc:
            # Pre-impl: D5 constraint blocks this. The first H test will
            # surface a cleaner failure via the field-check; we annotate
            # this fixture branch with the fact for clarity.
            self.fail(
                "Fixture sanity (H group): cannot create user-scope MCP "
                "on unbound project — D5 ``"
                "_check_user_scope_requires_bound_project`` still blocks "
                f"unbound creation. Plan AC #1 / AC #6 required. Raised: "
                f"{exc}"
            )

        self.external_tool = self.McpTool.sudo().create({
            'name': 'h_external_tool',
            'description': 'Tool on unbound external MCP — ACL parity test.',
            'input_schema': '{}',
            'server_id': self.external_mcp.id,
        })

        # Bound project + bound MCP — sanity / leg-1 regression lock.
        Service = self.env['woow_paas_platform.cloud_service']
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.template = Template.sudo().create({
            'name': 'H ACL Tmpl',
            'slug': f'h-acl-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'h-acl-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.bound_service = _internal_provision(Service.sudo()).create({
            'name': 'H Bound Svc',
            'workspace_id': self.ws_alice.id,
            'template_id': self.template.id,
            'helm_release_name': 'h-bound-svc',
            'helm_namespace': 'paas-ws-h',
        })
        self.bound_project = Project.sudo().create({
            'name': 'H Bound W1 Project',
            'cloud_service_id': self.bound_service.id,
        })
        self.bound_mcp = self.McpServer.sudo().create({
            'name': 'H Bound MCP (leg-1 lock)',
            'url': 'http://h-bound-svc-mcp.paas-ws-h.svc.cluster.local:3001/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.bound_project.id,
        })

    # ================================================================= #
    # H.1 — owner of unbound project can read own external MCP            #
    # ================================================================= #
    def test_owner_can_read_own_unbound_project_external_mcp(self):
        """AC #38, #39, #6: Alice (creator + follower of unbound project)
        can search + read MCP attached to her unbound project.

        **Red today**：current ir.rule single-leg walk
        ``project_id.cloud_service_id.workspace_id.access_ids.user_id``
        — ``cloud_service_id`` is NULL on unbound project → entire chain
        evaluates False → search returns empty even for Alice → first
        assertion fails with a clean trace.
        """
        visible = self.McpServer.with_user(self.alice).search([
            ('id', '=', self.external_mcp.id),
        ])
        self.assertEqual(
            visible, self.external_mcp,
            "AC #38: Alice (creator + follower of unbound project) "
            "cannot see external MCP on her own unbound project. "
            "ir.rule mcp_server_rule_workspace_user must adopt dual-leg "
            "domain — leg 2 (message_partner_ids walk for unbound "
            "project, OR-7 (c)) is missing. Plan AC #38 contract.",
        )
        as_alice = self.McpServer.with_user(self.alice).browse(
            self.external_mcp.id,
        )
        # browse().read() must work too.
        rows = as_alice.read(['name', 'url'])
        self.assertEqual(
            rows[0]['name'], self.external_mcp.name,
            "AC #38: Alice browse().read() must succeed once dual-leg "
            "domain is live.",
        )

    # ================================================================= #
    # H.2 — workspace member + follower can read                          #
    # ================================================================= #
    def test_workspace_member_follower_can_read_unbound_project_external_mcp(self):
        """AC #38, #39: Charlie (W1 member, follower of unbound project)
        can read.

        Tests the carve-out's positive boundary: the leg-2
        ``message_partner_ids`` walk grants visibility to any user in
        the follower set — workspace membership not directly required
        (unbound projects don't have workspace via cloud_service chain).

        **Red today**：same root cause as H.1 — single-leg walk fails.
        """
        visible = self.McpServer.with_user(self.charlie).search([
            ('id', '=', self.external_mcp.id),
        ])
        self.assertEqual(
            visible, self.external_mcp,
            "AC #38: Charlie (workspace member + project follower of "
            "unbound project) cannot see external MCP. dual-leg domain "
            "leg 2 (project_id.message_partner_ids in user.partner_id) "
            "must include Charlie via message_subscribe.",
        )

    # ================================================================= #
    # H.3 — workspace member, NOT follower cannot read                    #
    # ================================================================= #
    def test_workspace_member_non_follower_cannot_read_unbound_project_external_mcp(self):
        """AC #38, #39: Dave (W1 member, NOT follower of unbound) cannot
        read.

        Critical boundary — proves leg 2 carve-out is **follower-keyed**
        not **workspace-keyed**. Just having W1 access_ids row is
        insufficient when the project is unbound.

        **Red today**：single-leg walk fails for everyone on unbound
        projects → Dave's ``search`` returns empty → ``assertFalse``
        already passes today (trivially). After dual-leg lands, leg 2
        domain ``project_id.message_partner_ids in user.partner_id.ids``
        STILL excludes Dave (he didn't subscribe) → still empty. This is
        a **regression lock**: must stay GREEN post-impl.
        """
        visible = self.McpServer.with_user(self.dave).search([
            ('id', '=', self.external_mcp.id),
        ])
        self.assertFalse(
            visible,
            "AC #38 (boundary): Dave (W1 member, not subscribed) can "
            "see external MCP on unbound project — leg 2 carve-out "
            "must be follower-keyed (project_id.message_partner_ids), "
            "not workspace-keyed. Otherwise leg 2 widens to all W1 "
            "members → IDOR for any internal user that joins W1.",
        )
        as_dave = self.McpServer.with_user(self.dave).browse(
            self.external_mcp.id,
        )
        with self.assertRaises(
            AccessError,
            msg=(
                "AC #38 (boundary): Dave.browse().read() on unbound MCP "
                "must raise AccessError once dual-leg lands. Today "
                "browse may succeed (no ir.rule blocks unbound external "
                "MCP read for anyone) — that is the same root cause as "
                "search being empty pre-impl."
            ),
        ):
            as_dave.read(['name', 'url'])

    # ================================================================= #
    # H.4 — cross-workspace user cannot read                              #
    # ================================================================= #
    def test_cross_workspace_user_cannot_read_unbound_project_external_mcp(self):
        """AC #38, #39: Bob (W2 owner, no W1 access) cannot read W1's
        unbound MCP.

        Distinct from H.3: Bob isn't even in W1's workspace_access — he
        crosses workspace boundary. Both legs of the dual-leg domain
        must reject him:

          - leg 1 (workspace walk) — Bob not in W1.access_ids → False
          - leg 2 (message_partner_ids) — Bob not in unbound project
            followers → False

        **Red today**：single-leg walk fails for everyone → Bob also
        gets empty (trivially). Post-impl regression lock.
        """
        visible = self.McpServer.with_user(self.bob).search([
            ('id', '=', self.external_mcp.id),
        ])
        self.assertFalse(
            visible,
            "AC #38 (cross-ws): Bob (W2 owner, no overlap with W1) can "
            "see W1 unbound project's external MCP. dual-leg domain "
            "must reject — both legs must fail for cross-ws user. "
            "Otherwise unbound projects become a cross-tenant leak path.",
        )

    # ================================================================= #
    # H.5 — mcp_tool sub-model parity (server_id walk)                   #
    # ================================================================= #
    def test_mcp_tool_unbound_parity_follower_vs_non_follower(self):
        """AC #39 / B1 sub-model parity: mcp_tool inherits dual-leg via
        ``server_id`` prefix.

        Two sub-asserts in one method (合 OQ-A scoped 2 case per leader
        sign-off):

          - Charlie (follower) → can search the tool
          - Dave (non-follower) → cannot search the tool

        **Red today**：mcp_tool_rule_workspace_user is also single-leg —
        Charlie can't see her own tool → first assertion fails.
        """
        # Follower can read.
        visible_charlie = self.McpTool.with_user(self.charlie).search([
            ('id', '=', self.external_tool.id),
        ])
        self.assertEqual(
            visible_charlie, self.external_tool,
            "AC #39: Charlie (project follower) cannot see mcp_tool on "
            "unbound project's external MCP. ir.rule "
            "mcp_tool_rule_workspace_user must adopt dual-leg via "
            "server_id walk — server_id.project_id.message_partner_ids "
            "leg 2.",
        )

        # Non-follower cannot read (regression lock).
        visible_dave = self.McpTool.with_user(self.dave).search([
            ('id', '=', self.external_tool.id),
        ])
        self.assertFalse(
            visible_dave,
            "AC #39 (boundary): Dave (W1 member, not subscribed) can "
            "see mcp_tool on unbound project. Sub-model leg 2 must "
            "still gate on server_id.project_id.message_partner_ids.",
        )

    # ================================================================= #
    # H.6 — mcp_tool cross-workspace isolation                            #
    # ================================================================= #
    def test_mcp_tool_cross_workspace_unbound_isolation(self):
        """AC #39: Bob (W2 owner) cannot see W1 unbound mcp_tool.

        Sub-model对等 for H.4. Combines ``search`` empty +
        ``browse.read`` AccessError into single method for partial-leak
        detection.

        **Red today**：trivially empty (no rule lets anyone in pre-impl).
        Regression lock.
        """
        visible = self.McpTool.with_user(self.bob).search([
            ('id', '=', self.external_tool.id),
        ])
        self.assertFalse(
            visible,
            "AC #39 (cross-ws sub-model): Bob can search() W1 unbound "
            "mcp_tool. Dual-leg sub-model rule must reject cross-ws.",
        )

        as_bob = self.McpTool.with_user(self.bob).browse(
            self.external_tool.id,
        )
        with self.assertRaises(
            AccessError,
            msg=(
                "AC #39 (cross-ws sub-model): browse.read() on Bob "
                "succeeded for W1 unbound mcp_tool. Sub-model parity "
                "(server_id walk) must reject."
            ),
        ):
            as_bob.read(['name', 'description'])

    # ================================================================= #
    # HC-3 / OR-X9 — H.3b: write/unlink direction explicit lock           #
    #                                                                    #
    # Reviewer M-R4 (multi-agent, 2026-05-21): H.1–H.6 only exercise     #
    # ``search()`` + ``browse().read()`` (R direction). UF×W/U and       #
    # UN×W/U are implicit-only — relying on the fact that                #
    # ``mcp_server_rule_workspace_user`` shares the same dual-leg domain #
    # for ``perm_read / perm_write / perm_create / perm_unlink``. Runtime #
    # behaviour is correct today, but a future PR that bifurcates the    #
    # rule (e.g. introduces ``perm_write=True`` with a different leg-2    #
    # walk) could silently regress and break the OR-X9 32-case matrix    #
    # without H suite catching it.                                       #
    #                                                                    #
    # H.3b / H.4b add explicit assertRaises locks for                    #
    # ``with_user(non_follower).write(...)`` and ``unlink()`` on the    #
    # unbound external MCP — the same row H.3 / H.4 covered for read.   #
    # If implementation accidentally widens leg 2 on the write/unlink   #
    # direction, these tests fire.                                      #
    # ================================================================= #
    def test_workspace_member_non_follower_cannot_write_unbound_project_user_mcp_via_orm(self):
        """HC-3 / OR-X9 W direction: Dave (W1 member, NOT follower) must
        NOT be able to ``.write({...})`` the unbound external MCP via ORM.

        Companion to H.3 (read direction). Today's runtime is correct
        (rule shares perm bits across CRUD), but H.3b makes the
        invariant explicit so a future leg-2 PR cannot silently
        regress W direction.
        """
        as_dave = self.McpServer.with_user(self.dave).browse(
            self.external_mcp.id,
        )
        with self.assertRaises(
            AccessError,
            msg=(
                "HC-3 / OR-X9 W direction: Dave (W1 member, non-follower) "
                "wrote to Alice's unbound external MCP. "
                "mcp_server_rule_workspace_user leg 2 must reject "
                "write-direction access for non-followers (follower-keyed, "
                "not workspace-keyed). Future PR changing leg 2 perm "
                "settings must keep W direction symmetric with R."
            ),
        ):
            as_dave.write({'name': 'Dave Hijack Attempt — Write'})

    def test_workspace_member_non_follower_cannot_unlink_unbound_project_user_mcp_via_orm(self):
        """HC-3 / OR-X9 U direction: Dave (W1 member, NOT follower) must
        NOT be able to ``.unlink()`` the unbound external MCP via ORM.

        Companion to H.3 (read direction). Same rationale as W test
        above; this locks the U direction explicitly.
        """
        as_dave = self.McpServer.with_user(self.dave).browse(
            self.external_mcp.id,
        )
        with self.assertRaises(
            AccessError,
            msg=(
                "HC-3 / OR-X9 U direction: Dave (W1 member, non-follower) "
                "unlinked Alice's unbound external MCP. "
                "mcp_server_rule_workspace_user leg 2 must reject "
                "unlink-direction access for non-followers. "
                "Future PR splitting leg 2 perm bits must keep U "
                "symmetric with R."
            ),
        ):
            as_dave.unlink()

    def test_cross_workspace_user_cannot_write_unbound_project_user_mcp_via_orm(self):
        """HC-3 / OR-X9 W direction (cross-workspace): Bob (W2 owner, no
        W1 access at all) must NOT be able to write the unbound MCP.

        Stricter than H.3b — Bob is not even in W1.access_ids, so both
        legs of the dual-leg domain reject him. Locks the W direction
        for cross-workspace attackers explicitly.
        """
        as_bob = self.McpServer.with_user(self.bob).browse(
            self.external_mcp.id,
        )
        with self.assertRaises(
            AccessError,
            msg=(
                "HC-3 / OR-X9 W direction (cross-ws): Bob (W2 owner, "
                "no W1 access) wrote Alice's W1 unbound external MCP. "
                "Both legs of mcp_server_rule_workspace_user must reject "
                "cross-workspace write — leg 1 (workspace walk) and "
                "leg 2 (message_partner_ids walk) BOTH false for Bob."
            ),
        ):
            as_bob.write({'name': 'Bob Cross-WS Hijack — Write'})

    def test_cross_workspace_user_cannot_unlink_unbound_project_user_mcp_via_orm(self):
        """HC-3 / OR-X9 U direction (cross-workspace): Bob must NOT
        unlink the unbound MCP.

        Companion to W cross-ws test above; locks U direction.
        """
        as_bob = self.McpServer.with_user(self.bob).browse(
            self.external_mcp.id,
        )
        with self.assertRaises(
            AccessError,
            msg=(
                "HC-3 / OR-X9 U direction (cross-ws): Bob (W2 owner) "
                "unlinked Alice's W1 unbound external MCP. Both dual-leg "
                "legs must reject cross-workspace unlink."
            ),
        ):
            as_bob.unlink()

    # ================================================================= #
    # H.7-H.8 — ai_assistant._check_project_access carve-out (AC #42)     #
    # ================================================================= #
    def _check_project_access(self, user, project):
        """Invoke ai_assistant.AiAssistantController._check_project_access
        in a request-scoped env.

        Wraps the lookup in ``patch('odoo.http.request', ...)`` so the
        method's ``request.env.user`` call sees ``user``. Tests target
        the **logic** (predicate), not the HTTP wrapper — that's H.9's
        job.
        """
        from unittest.mock import patch, MagicMock
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )

        controller = AiAssistantController()
        fake_request = MagicMock()
        fake_request.env = self.env(user=user.id)
        with patch(
            'odoo.addons.woow_paas_platform.controllers.ai_assistant.request',
            fake_request,
        ):
            return controller._check_project_access(project)

    def test_check_project_access_unbound_follower_returns_true(self):
        """AC #42: ``ai_assistant._check_project_access`` for an unbound
        project allows follower (message_partner_ids member).

        **Red today**：ai_assistant.py:49-51 short-circuits
        ``if not project.cloud_service_id or not project.cloud_service_id
        .workspace_id: return True`` — returns True for **anyone** on an
        unbound project. After impl: must require user.partner_id in
        project.message_partner_ids.

        For Charlie (follower) the **post-impl** behaviour is also True
        — so this test passes pre-impl trivially. Mark as regression
        lock: validates that **follower** path stays True after the
        tightening lands. If implementation accidentally over-tightens
        (e.g. requires workspace access), this fails.
        """
        allowed = self._check_project_access(self.charlie, self.unbound_project)
        self.assertTrue(
            allowed,
            "AC #42: Charlie (project follower via message_subscribe) "
            "must be allowed by _check_project_access on unbound "
            "project. Post-impl leg uses "
            "`user.partner_id in project.message_partner_ids`.",
        )

    def test_check_project_access_unbound_non_follower_returns_false(self):
        """AC #42 (negative): non-follower must NOT be allowed.

        Dave is W1 member but NOT subscribed to unbound project — must
        be rejected.

        **Red today**：``return True`` blanket → Dave passes → assertion
        fails. This is the **core attack surface fix** for AC #42 — IDOR
        partial fix on the project path.
        """
        allowed = self._check_project_access(self.dave, self.unbound_project)
        self.assertFalse(
            allowed,
            "AC #42 (core fix): Dave (W1 member, not subscribed to "
            "unbound project) was allowed by _check_project_access. "
            "ai_assistant.py:49-51 must tighten from `return True` to "
            "`return user.partner_id in project.message_partner_ids`. "
            "Aligns controller carve-out with ir.rule leg 2 — two-layer "
            "ACL consistency防 IDOR.",
        )

    # ================================================================= #
    # H.10 — leg-1 bound project regression lock                          #
    # ================================================================= #
    def test_acl_dual_leg_bound_path_preserved(self):
        """AC #38 (leg-1 regression lock): bound-project ACL behaviour
        unchanged.

        Combines an own-workspace allow + cross-workspace deny on the
        **bound** MCP path. Today both pass (PR #47 single-leg = leg-1
        of the new dual-leg); post-impl leg-1 unchanged → both still
        pass. **Must stay GREEN both before and after**.

        If implementation accidentally tightens leg-1 (e.g. adds a leg
        that breaks bound workspace path), this surfaces immediately.
        """
        # Alice (W1 owner + bound project's workspace member) sees bound MCP.
        visible_alice = self.McpServer.with_user(self.alice).search([
            ('id', '=', self.bound_mcp.id),
        ])
        self.assertEqual(
            visible_alice, self.bound_mcp,
            "AC #38 leg-1 regression: Alice lost read on her own "
            "bound-project MCP. Dual-leg leg 1 (workspace walk) must "
            "not regress — PR #47 9-case suite already locks this.",
        )

        # Bob (W2, cross-ws) cannot see bound MCP.
        visible_bob = self.McpServer.with_user(self.bob).search([
            ('id', '=', self.bound_mcp.id),
        ])
        self.assertFalse(
            visible_bob,
            "AC #38 leg-1 regression: Bob (cross-ws) can see W1 bound "
            "MCP. Dual-leg implementation must keep leg-1 walk intact.",
        )

    # ================================================================= #
    # OR-X9 — H.11: workspace_user rule cannot grant system-scope create  #
    # ================================================================= #
    def test_workspace_user_rule_still_blocks_system_create_after_dual_leg(self):
        """OR-X9 (MAJOR 1 重炸 regression lock): ``base.group_user`` must
        NOT create ``scope='system'`` MCP via dual-leg domain.

        **核心攻擊面**：dual-leg domain shape 寫成
        ``'|', leg, '&', sub_a, sub_b`` instead of
        ``'&', leg, '|', sub_a, sub_b`` 會把 scope filter 與 leg 2 用 OR
        連結，導致 new ``scope='system'`` row 過 leg 2 永真（``cloud_
        service_id IS NULL`` 對 new row 是 True；``message_partner_ids``
        對 new row 取決於 default，但若 OR-combined 在 leg 2 sub-leg2
        為真，整條 leg 2 為真）。

        **Today GREEN** (PR #47 既有 ``test_user_cannot_create_system_
        mcp_via_orm`` 已綠)，**post-impl 必須仍 GREEN** — dual-leg 引入
        後此 attack 必須繼續被擋。若 implementation 寫錯 precedence /
        operator，此 test RED → MAJOR 1 重炸 alarm.
        """
        with self.assertRaises(
            AccessError,
            msg=(
                "OR-X9 / MAJOR 1 重炸: base.group_user (Charlie, W1 "
                "member) was allowed to create scope='system' mcp_server "
                "row after the dual-leg domain refactor. The new domain "
                "shape (AC #38) must keep scope filter AND-connected "
                "with the OR(leg1, leg2) — writing '|', leg, '&', "
                "leg2_a, leg2_b would let leg 2 be true for new "
                "scope='system' rows. Plan §OR-X9 explicit guard."
            ),
        ):
            self.McpServer.with_user(self.charlie).create({
                'name': 'OR-X9 Forbidden System MCP',
                'url': 'http://malicious.example/sse',
                'transport': 'sse',
                'scope': 'system',
            })

    # ================================================================= #
    # OR-X9 — H.12: mcp_tool对等 system-scope create block               #
    # ================================================================= #
    def test_mcp_tool_workspace_user_rule_still_blocks_system_create_after_dual_leg(self):
        """OR-X9 sub-model parity: ``base.group_user`` cannot create
        ``mcp_tool`` attached to scope='system' MCP after dual-leg.

        Sub-model对等 (B1 pattern). Symmetric to PR #47
        ``test_user_cannot_create_mcp_tool_for_system_server``.

        Setup: seed a sudo() scope='system' MCP. Charlie attempts to
        attach a tool via ORM create. dual-leg rule must keep blocking
        this (server_id.scope='system' leg 1 path matches but
        perm_create=False; sub-leg fallback must not over-widen).
        """
        # Sudo-seed a system MCP to attack against.
        system_mcp = self.McpServer.sudo().create({
            'name': 'OR-X9 Sudo System MCP',
            'url': 'http://platform.mcp/sse',
            'transport': 'sse',
            'scope': 'system',
        })

        with self.assertRaises(
            AccessError,
            msg=(
                "OR-X9 sub-model parity: Charlie attached a tool to a "
                "scope='system' mcp_server after dual-leg. mcp_tool_rule_"
                "workspace_user must still gate sub-model creation — "
                "leg 2 ``server_id.project_id.message_partner_ids`` "
                "walk must NOT widen to match scope='system' tools."
            ),
        ):
            self.McpTool.with_user(self.charlie).create({
                'name': 'or_x9_malicious_tool',
                'description': 'Attempted system-scope tool injection.',
                'input_schema': '{}',
                'server_id': system_mcp.id,
            })


# =========================================================================== #
# H.9 — endpoint integration test (TransactionCase + mock request per OQ-D)   #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestAiAssistantEndpointUnboundNonFollower(HttpCase):
    """H.9 — ``/api/support/projects`` action=update on unbound project as
    non-follower must be denied.

    Per OQ-D leader sign-off: use HttpCase pattern (parity with
    test_project_detail_endpoints — JSON-RPC over url_open). Pure-Python
    mock-request alternative was deemed too brittle for the request-env
    coupling in ``ai_assistant.py:_update_project``.
    """

    def setUp(self):
        super().setUp()
        # skill-binding-ux：portal settings flag 預設 fail-closed off — 本
        # class 打 write endpoint（action=update），需開 flag 才能走過 flag
        # gate、真正驗到 _check_project_access 非 follower 守線（否則 flag
        # gate 先回 'Access denied' → 假綠繞過 ACL 守線）。flag-off denied
        # 守線在 test_portal_settings_flag.py。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        Workspace = self.env['woow_paas_platform.workspace']
        Project = self.env['project.project']
        WorkspaceAccess = self.env['woow_paas_platform.workspace_access']

        self.alice = _new_internal_user(
            self.env, 'h_ep_alice', 'Alice (H.9)', with_password=True,
        )
        self.dave = _new_internal_user(
            self.env, 'h_ep_dave', 'Dave (H.9)', with_password=True,
        )

        self.ws_w1 = Workspace.sudo().create({
            'name': 'H.9 W1', 'owner_id': self.alice.id,
        })
        WorkspaceAccess.sudo().create({
            'workspace_id': self.ws_w1.id, 'user_id': self.dave.id,
            'role': 'user',
        })

        self.unbound_project = Project.sudo().create({
            'name': 'H.9 Unbound Project',
        })
        # Alice subscribed by create_uid auto; Dave NOT subscribed.
        self.unbound_project.message_subscribe(
            partner_ids=[self.alice.partner_id.id],
        )

    def _post(self, url, params):
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(
            response.status_code, 200,
            f"Endpoint {url!r} returned status={response.status_code}. "
            f"Body excerpt: {response.text[:200]!r}",
        )
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']

    def test_endpoint_update_project_unbound_non_follower_denied(self):
        """AC #42: ``ai_assistant.api_support_projects`` action=update on
        unbound project by non-follower returns ``{success: False,
        error: 'Access denied'}``.

        Today's red expectation: ``_check_project_access`` returns True
        for unbound (line 49-51), so Dave's update goes through — the
        ``success`` assertion below fails. Post-impl: tightened
        carve-out denies Dave → 'Access denied'.
        """
        self.authenticate('h_ep_dave', 'h_ep_dave')
        result = self._post(
            '/api/support/projects',
            {
                'action': 'update',
                'project_id': self.unbound_project.id,
                'name': 'H.9 Hijack Attempt',
            },
        )
        self.assertFalse(
            result.get('success'),
            "AC #42: Dave (non-follower) was allowed to update unbound "
            "project via `/api/support/projects` action=update. "
            "ai_assistant._check_project_access must reject non-follower "
            "on unbound projects (aligns with ir.rule leg 2). "
            f"Got: {result!r}.",
        )
        self.assertEqual(
            result.get('error'), 'Access denied',
            "AC #42: endpoint denial wording must remain 'Access "
            "denied' (existing ai_assistant.py:_update_project "
            "convention). Got: %r." % (result.get('error'),),
        )

        # Sanity: project name unchanged.
        self.unbound_project.invalidate_recordset()
        self.assertEqual(
            self.unbound_project.name, 'H.9 Unbound Project',
            "AC #42: rejected update must not have partially written "
            "name (no save).",
        )


# =========================================================================== #
# HC-2 — paas.py:/api/support-projects/<pid> + /mcp-servers endpoint coverage  #
#                                                                              #
# Reviewer M-R2 (multi-agent, 2026-05-21): ``paas.py:_resolve_project_access`` #
# (line 1483-1502) returns blanket ``ROLE_OWNER`` synthetic access for any     #
# authenticated internal user against an unbound project. AC #42 / H.9        #
# only covered the ``ai_assistant.py:/api/support/projects`` endpoint via     #
# ``_check_project_access``; the paas.py endpoint surface (project detail +   #
# project-centric MCP CRUD) is silently divergent → reproducible IDOR +       #
# sudo write + MCP injection (reviewer-verified attack chain).                #
#                                                                              #
# Hotfix surface (per leader (b) ruling):                                      #
#   - ``_resolve_project_access`` `if not workspace:` branch must add        #
#     follower carve-out (aligned with ai_assistant._check_project_access)    #
#   - ``_SyntheticAccess`` docstring updated (D5 已刪 + ai_assistant 收緊)    #
#                                                                              #
# These tests reproduce the M-R2 attack chain and lock the fix:                #
#   1. action=update on unbound project → must deny non-follower               #
#   2. action=get on unbound project → must deny non-follower                  #
#   3. /mcp-servers action=create on unbound project → must deny non-follower  #
#                                                                              #
# Red expectation today (pre-hotfix): all 3 succeed for Dave (non-follower).   #
# Post-hotfix: unified ``Project not found or access denied`` error.           #
# =========================================================================== #


@tagged('post_install', '-at_install')
class TestPaasEndpointUnboundNonFollower(HttpCase):
    """HC-2 — paas.py endpoint M-R2 IDOR reproducer + hotfix lock.

    Uses HttpCase (same idiom as TestAiAssistantEndpointUnboundNonFollower
    above + test_project_detail_endpoints.py — leader OQ-D ruling)。
    """

    def setUp(self):
        super().setUp()
        Workspace = self.env['woow_paas_platform.workspace']
        Project = self.env['project.project']
        WorkspaceAccess = self.env['woow_paas_platform.workspace_access']

        self.alice = _new_internal_user(
            self.env, 'hc2_alice', 'Alice (HC-2)', with_password=True,
        )
        self.dave = _new_internal_user(
            self.env, 'hc2_dave', 'Dave (HC-2)', with_password=True,
        )

        self.ws_w1 = Workspace.sudo().create({
            'name': 'HC-2 W1', 'owner_id': self.alice.id,
        })
        # Dave joins W1 as 'user' role so he is workspace_access member
        # but NOT a project follower — mirrors H.3 / H.7-H.9 attack
        # vector (workspace-keyed access ≠ project-follower access).
        WorkspaceAccess.sudo().create({
            'workspace_id': self.ws_w1.id, 'user_id': self.dave.id,
            'role': 'user',
        })

        # Unbound project — no cloud_service_id, Alice is creator (auto-
        # subscribed as follower); Dave NOT subscribed.
        self.unbound_project = Project.sudo().create({
            'name': 'HC-2 Unbound Project',
        })
        self.unbound_project.message_subscribe(
            partner_ids=[self.alice.partner_id.id],
        )

    def _post(self, url, params):
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(
            response.status_code, 200,
            f"Endpoint {url!r} returned status={response.status_code}. "
            f"Body excerpt: {response.text[:200]!r}",
        )
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']

    @staticmethod
    def _url_detail(project_id):
        return f'/api/support-projects/{project_id}'

    @staticmethod
    def _url_mcp_servers(project_id):
        return f'/api/support-projects/{project_id}/mcp-servers'

    # ------------------------------------------------------------------ #
    # M-R2 attack chain step 1: action=update                            #
    # ------------------------------------------------------------------ #
    def test_endpoint_paas_resolve_project_access_unbound_non_follower_update_denied(self):
        """HC-2 / M-R2 attack chain step 1: Dave (non-follower) POST
        ``/api/support-projects/<unbound_pid>`` action=update must deny.

        **Red today**：``_resolve_project_access`` (paas.py:1483-1502)
        returns ``self._SyntheticAccess(role=ROLE_OWNER)`` for any
        authenticated user when project has no workspace → action=update
        passes ``access.role in (ROLE_OWNER, ROLE_ADMIN)`` gate → sudo
        write succeeds → Dave hijacks Alice's project name.

        Post-hotfix expectation: paas.py:_resolve_project_access aligns
        with ai_assistant._check_project_access — non-follower of unbound
        project returns ``(None, None)`` → unified ``Project not found
        or access denied`` error (id-enum 防, PR #53 AC #6/#11 wording).
        """
        self.authenticate('hc2_dave', 'hc2_dave')
        result = self._post(
            self._url_detail(self.unbound_project.id),
            {'action': 'update', 'name': 'HC-2 Dave Hijack Attempt'},
        )
        self.assertFalse(
            result.get('success'),
            "HC-2 / M-R2 step 1: Dave (non-follower) succeeded action="
            "update on Alice's unbound project via "
            "/api/support-projects/<pid>. paas.py:_resolve_project_access "
            "blanket ROLE_OWNER carve-out must be tightened with "
            "follower check (align with ai_assistant._check_project_"
            f"access). Got: {result!r}.",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "HC-2 / M-R2 step 1: denial wording must be unified "
            "'Project not found or access denied' (PR #53 id-enum 防 / "
            "AC #6 / #11). Got: %r." % (result.get('error'),),
        )

        # Sanity: project name unchanged.
        self.unbound_project.invalidate_recordset()
        self.assertEqual(
            self.unbound_project.name, 'HC-2 Unbound Project',
            "HC-2 / M-R2 step 1: rejected update must leave project "
            "name unchanged (no partial sudo write).",
        )

    def test_endpoint_paas_resolve_project_access_unbound_non_follower_get_denied(self):
        """HC-2 / M-R2 read-side mirror: Dave (non-follower) POST
        ``/api/support-projects/<unbound_pid>`` action=get must deny.

        action=get does not gate on ``access.role`` (read is allowed
        for any access record) — so the **only** ACL gate is the
        ``_resolve_project_access`` return path. If carve-out grants
        synthetic ROLE_OWNER, Dave can browse the project detail incl.
        ``mcp_server_ids`` payload → information disclosure.

        Post-hotfix: same unified error as update test.
        """
        self.authenticate('hc2_dave', 'hc2_dave')
        result = self._post(
            self._url_detail(self.unbound_project.id),
            {'action': 'get'},
        )
        self.assertFalse(
            result.get('success'),
            "HC-2 / M-R2 read-side: Dave succeeded action=get on "
            "Alice's unbound project via /api/support-projects/<pid>. "
            "_resolve_project_access must deny non-follower at the "
            "first gate, before action branching. "
            f"Got: {result!r}.",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "HC-2 / M-R2 read-side: denial wording must be unified. "
            "Got: %r." % (result.get('error'),),
        )

    # ------------------------------------------------------------------ #
    # M-R2 attack chain step 2: /mcp-servers action=create                #
    # ------------------------------------------------------------------ #
    def test_endpoint_paas_project_mcp_servers_unbound_non_follower_create_denied(self):
        """HC-2 / M-R2 attack chain step 2: Dave (non-follower) POST
        ``/api/support-projects/<unbound_pid>/mcp-servers`` action=create
        must deny.

        This is the MCP injection vector. The endpoint goes through
        the same ``_resolve_project_access`` gate, so a hotfix on
        ``_resolve_project_access`` closes both steps (1 and 2) in
        one fix — the second-step lock here proves the hotfix's
        downstream surface coverage.

        **Red today**：carve-out grants synthetic ROLE_OWNER → action=
        create passes ``access.role in (ROLE_OWNER, ROLE_ADMIN)`` →
        ``McpServer.sudo().create({...})`` succeeds → user-scope MCP row
        injected into Alice's unbound project → later sync/test
        triggers outbound HTTP under Alice's tenant context.

        Post-hotfix: unified error, no row created.
        """
        self.authenticate('hc2_dave', 'hc2_dave')
        result = self._post(
            self._url_mcp_servers(self.unbound_project.id),
            {
                'action': 'create',
                'name': 'HC-2 Dave Injected MCP',
                'url': 'https://attacker-controlled.example/mcp',
                'transport': 'sse',
            },
        )
        self.assertFalse(
            result.get('success'),
            "HC-2 / M-R2 step 2: Dave (non-follower) injected a "
            "user-scope MCP row into Alice's unbound project via "
            "/api/support-projects/<pid>/mcp-servers action=create. "
            "_resolve_project_access hotfix must close this surface "
            f"too. Got: {result!r}.",
        )
        self.assertEqual(
            result.get('error'), 'Project not found or access denied',
            "HC-2 / M-R2 step 2: denial wording must be unified. "
            "Got: %r." % (result.get('error'),),
        )

        # Sanity: no MCP row injected.
        injected = self.env['woow_paas_platform.mcp_server'].sudo().search([
            ('project_id', '=', self.unbound_project.id),
            ('name', '=', 'HC-2 Dave Injected MCP'),
        ])
        self.assertFalse(
            injected,
            "HC-2 / M-R2 step 2: rejected create must not leave any "
            "MCP row pointing at the unbound project. Even if endpoint "
            "returned success=False, a sudo() create that fired before "
            "the deny gate would still leak a row — guard against "
            "fix-order regressions.",
        )
