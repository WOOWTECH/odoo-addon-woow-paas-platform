"""TDD red phase — D5 invariant removal + unbound user-scope MCP allowance.

Plan: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
  - AC #1: ``mcp_server.py:_check_user_scope_requires_bound_project`` integrally
    deleted. **D5 不再存在於 codebase**。
  - AC #5: D4 invariant (``_check_scope_project_consistency``) **不動**.
  - AC #6: ``scope='user'`` MCP may attach to an **unbound** project
    (``project.cloud_service_id IS NULL``); ORM ``create`` / ``write`` must not
    raise ValidationError.

Design context:
  - B 案 拍板（OQ-1）— sidecar 物理機制保留，但 schema 不再強要 user-scope MCP
    一定要掛 bound project；frontend 透過 sidecar-hint endpoint 控制 mental
    model。
  - 任何要 enforce 「unbound project user-scope MCP zombie」的擔憂留給
    ai.rule dual-leg（AC #38-39）+ ai_assistant carve-out（AC #42）兩層 ACL
    解決，不靠 schema constraint。

Red expectation today (pre-implementation, 2026-05-20):

* AC #6 → RED：今天 mcp_server.py:127-139 仍存在 D5
  ``_check_user_scope_requires_bound_project``，create user-scope MCP 掛
  unbound project 會 raise ValidationError。test 用 ``assertNoRaise`` 包，
  raise 即 fail with clean trace「ValidationError: ... bound to a Cloud
  Service」.
* AC #1 → RED：今天 source 仍 import / define
  ``_check_user_scope_requires_bound_project``；grep test 找得到 → assertNotIn
  fail with assertion message。
* AC #5 兩條 D4 regression lock → 今天 GREEN（D4 已存在），refactor 後仍
  GREEN（plan AC #5 明文「D4 不動」）。明示為 regression lock，**今天綠是
  predicted behaviour**，不是 fixture broken。

Boundary（刻意不測）:
* 不測 ``_check_user_scope_requires_bound_project`` 對 unbound system-scope
  MCP 的行為 — system-scope 永遠 project_id=NULL（D4 enforce），D5 也不會
  觸發於 system row（method body 內 ``if record.scope != 'user': continue``).
* PR #47 archived orphan row（``auto_created=True, active=False,
  project_id=NULL``）保留行為由 G group ``test_mcp_project_decouple_
  migration_25.py:test_migration_preserves_pr47_orphan_archive`` 負責，本檔
  不重複測。
"""
import inspect
import os

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import ValidationError
from ..models.cloud_service import _internal_provision


@tagged('post_install', '-at_install')
class TestMcpD5RemovalAndUnboundAllowed(TransactionCase):
    """A group — D5 deletion + AC #6 unbound allowance + AC #5 D4 regression."""

    def setUp(self):
        super().setUp()
        self.McpServer = self.env['woow_paas_platform.mcp_server']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Project = self.env['project.project']

        # Shared fixture: a workspace + bound service (used only by D4
        # `system + bound project` negative test). Unbound projects use
        # cloud_service_id=False / no service in their setup.
        self.workspace = self.Workspace.sudo().create({
            'name': 'WS D5 Removal',
        })
        self.template = self.Template.sudo().create({
            'name': 'Tmpl D5',
            'slug': 'tmpl-d5-removal',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-d5',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.bound_service = _internal_provision(self.Service.sudo()).create({
            'name': 'Bound Svc D5',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'd5-svc',
            'helm_namespace': 'paas-ws-d5',
        })

    # ------------------------------------------------------------------ #
    # AC #6: user-scope MCP can attach to an unbound project              #
    # ------------------------------------------------------------------ #
    def test_user_scope_mcp_can_have_unbound_project(self):
        """AC #6: ``scope='user'`` MCP on unbound project must not raise.

        Sets up a project with ``cloud_service_id=False`` (unbound — no
        sidecar physically) then creates a user-scope MCP whose URL points
        at an *external* host. Per Plan AC #6 the create must succeed; D5
        is gone, and the new mental model (B 案 / OQ-1) lets unbound
        projects carry external user-scope MCP rows.

        **Red today**：mcp_server.py:127-139 ``_check_user_scope_requires_
        bound_project`` raises ``ValidationError: must be linked to a
        project bound to a Cloud Service`` — the assertion below tags that
        as a clean failure pointing at AC #1 (D5 removal owed).
        """
        unbound_project = self.Project.sudo().create({
            'name': 'Unbound Project (D5 removal)',
            # cloud_service_id deliberately omitted — Plan AC #6 unbound
            # state.
        })
        # Sanity — make sure the project really is unbound. Guards against
        # a future setUp regression that auto-binds.
        self.assertFalse(
            unbound_project.cloud_service_id,
            "Fixture sanity: project must be unbound (cloud_service_id "
            "must be empty) for AC #6 to be meaningful.",
        )

        try:
            mcp = self.McpServer.sudo().create({
                'name': 'External User MCP on Unbound Project',
                'url': 'https://mcp.example.com/sse',
                'transport': 'sse',
                'scope': 'user',
                'project_id': unbound_project.id,
            })
        except ValidationError as exc:
            self.fail(
                "AC #6 violated: user-scope MCP on unbound project raised "
                f"ValidationError — D5 (mcp_server.py:_check_user_scope_"
                f"requires_bound_project) is still active. Plan AC #1 "
                f"requires the constraint method to be deleted entirely. "
                f"Raised: {exc}"
            )
        self.assertEqual(
            mcp.scope, 'user',
            "AC #6 sanity: created row's scope must remain 'user'.",
        )
        self.assertEqual(
            mcp.project_id.id, unbound_project.id,
            "AC #6 sanity: created row's project_id must equal the "
            "unbound project.",
        )
        self.assertFalse(
            mcp.project_id.cloud_service_id,
            "AC #6 sanity: the project the new MCP is attached to must "
            "still be unbound (no implicit auto-bind side effect).",
        )

    # ------------------------------------------------------------------ #
    # AC #1: D5 constraint method physically gone from source            #
    # ------------------------------------------------------------------ #
    def test_d5_constraint_method_no_longer_exists_in_mcp_server_source(self):
        """AC #1: ``_check_user_scope_requires_bound_project`` deleted.

        Grep-style source check — more robust than ``getattr(...)`` /
        ``AttributeError`` reflection because Odoo loads ``@api.constrains``
        decorators at registry build time and a stale .pyc could mask a
        partial removal. Inspecting the live module's source file content
        catches both stub-leftovers and method-renamed-but-still-defined
        cases.

        **Red today**：method body is at mcp_server.py:127-139 — substring
        match returns True → assertNotIn fails with a clean assertion
        message.
        """
        from odoo.addons.woow_paas_platform.models import mcp_server as mcp_server_module
        source_path = inspect.getsourcefile(mcp_server_module)
        self.assertTrue(
            source_path and os.path.exists(source_path),
            f"Test sanity: cannot locate mcp_server.py source file "
            f"(got {source_path!r}). Skip impossible — this is a fixture "
            f"problem, not a refactor problem.",
        )
        with open(source_path, encoding='utf-8') as fh:
            source = fh.read()
        self.assertNotIn(
            '_check_user_scope_requires_bound_project',
            source,
            "AC #1: mcp_server.py still defines "
            "_check_user_scope_requires_bound_project — D5 constraint "
            "must be physically deleted by the refactor (Plan AC #1: "
            "'D5 不再存在於 codebase'). Grep `src/models/mcp_server.py` "
            "to confirm the @api.constrains block at lines 127-139 is "
            "gone, not merely commented out or renamed.",
        )

    # ------------------------------------------------------------------ #
    # AC #5: D4 invariant — user-scope MCP requires project_id            #
    # ------------------------------------------------------------------ #
    def test_d4_user_scope_requires_project_id_after_refactor(self):
        """AC #5: D4 ``_check_scope_project_consistency`` still fires on
        ``scope='user'`` + ``project_id IS NULL``.

        **Regression lock (today GREEN, must stay GREEN post-refactor)**:
        Plan AC #5 明文「D4 不動」. D5 拔了不會把 D4 也順便拔掉 —
        但 implementation 可能把整段 @api.constrains 都刪（誤讀 plan）→
        本 test 鎖此 attack。

        Note: today the D5 constraint method is also live; this test only
        depends on the D4 leg ``if record.scope == 'user' and not
        record.project_id``. Even with D5 active D4 fires first
        (constraint method order in Odoo is registry-order, but both are
        @api.constrains on the same fields so either firing produces
        ValidationError — the assertion just locks that *some* validation
        raises for this row shape).
        """
        with self.assertRaises(
            ValidationError,
            msg=(
                "AC #5 regression: scope='user' + project_id=NULL must "
                "raise ValidationError via D4 "
                "(_check_scope_project_consistency). If this test went "
                "red, the implementation likely deleted the entire "
                "@api.constrains method instead of only the D5 leg. "
                "Plan AC #5 explicitly states 'D4 不動'."
            ),
        ):
            self.McpServer.sudo().create({
                'name': 'Invalid User MCP — no project_id',
                'url': 'https://example.com/mcp',
                'transport': 'sse',
                'scope': 'user',
                # No project_id — D4 must reject.
            })

    def test_d4_system_scope_forbids_project_id_after_refactor(self):
        """AC #5: D4 still fires on ``scope='system'`` + ``project_id``
        set — the *system-side* leg of D4 is independent of D5.

        **Regression lock**: ``_check_scope_project_consistency`` has two
        legs:

          - user-scope requires project_id  (tested above)
          - system-scope forbids project_id (tested here)

        Both must survive the D5 removal. Implementation that deletes the
        entire `_check_scope_project_consistency` method (instead of just
        ``_check_user_scope_requires_bound_project``) would silently let
        ``scope='system' + project_id=bound`` through — breaking
        Invariant #2 from PR #47.
        """
        bound_project = self.Project.sudo().create({
            'name': 'Bound Project D5 D4 sanity',
            'cloud_service_id': self.bound_service.id,
        })
        with self.assertRaises(
            ValidationError,
            msg=(
                "AC #5 regression: scope='system' + project_id NOT NULL "
                "must raise ValidationError via D4. If this test went "
                "red, the implementation accidentally killed the "
                "system-side leg of D4 along with D5. Plan AC #5 "
                "explicitly states 'D4 不動'."
            ),
        ):
            self.McpServer.sudo().create({
                'name': 'Invalid System MCP — has project_id',
                'url': 'https://platform.example/mcp',
                'transport': 'sse',
                'scope': 'system',
                'project_id': bound_project.id,
            })
