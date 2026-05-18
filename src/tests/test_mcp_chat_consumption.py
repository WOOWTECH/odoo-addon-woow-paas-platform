"""TDD red phase tests — AI chat consumption rebar for MCP project_id.

Plan: docs/refactor/plan-mcp-project-decouple.md §AI chat 消費鏈 (AC #13-15)
Decision: docs/refactor/decisions/mcp-project-decouple.md §Decision (consumer chain)

The AI chat consumption chain shifts from:

    channel → task → project → cloud_service → user_mcp_server_ids → tools

to:

    channel → task → project → mcp_server_ids → tools

The cloud_service hop is removed because MCP is now logically owned by
project (see Context section of the decision doc).  Two consumer paths
are touched:

* ``DiscussChannel._get_user_mcp_tools`` (src/models/discuss_channel.py:310)
* ``AiAssistantController._get_user_mcp_tools_for_channel`` (src/controllers/ai_assistant.py:614)

This file locks three AC:

* **AC #13**: bound-project channel returns the MCP's tools.
* **AC #13 negative**: unbound-project channel returns an empty recordset
  (the AI prompt path must degrade gracefully — D5 constraint forbids the
  unbound user-scope MCP row from existing, so the consumer must handle
  "project exists but no MCP" without crashing).
* **AC #15**: ``ai.assistant.mcp_server_ids`` M2M still returns system-scope
  MCP unchanged (refactor must not regress the system-MCP path).

Red expectations today (pre-implementation):

1. ``mcp_server.project_id`` field missing → ``self.fail()`` → AssertionError.
2. ``project.project.mcp_server_ids`` inverse missing → ``self.fail()`` →
   AssertionError.
3. ``DiscussChannel._get_user_mcp_tools`` still walks
   ``task.project_id.cloud_service_id.user_mcp_server_ids`` — with new
   MCP linked only by ``project_id`` (no cloud_service_id) the old
   chain returns empty → AC #13 fails with AssertionError.
4. ``ai.assistant`` regression test should stay green throughout — it's
   the regression lock for "don't break the system MCP path".
"""
import uuid

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcpChatConsumption(TransactionCase):
    """Three-case consumer-chain regression — bound, unbound, system."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.Task = cls.env['project.task']
        cls.Channel = cls.env['discuss.channel']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']
        cls.McpTool = cls.env['woow_paas_platform.mcp_tool']

    def setUp(self):
        super().setUp()

        if 'project_id' not in self.McpServer._fields:
            self.fail(
                "mcp_server.project_id field missing — chat-consumption "
                "tests rely on the new schema (AC #1)."
            )
        if 'mcp_server_ids' not in self.Project._fields:
            self.fail(
                "project.project.mcp_server_ids inverse O2M missing — "
                "AI chat consumption chain (AC #13/#14) cannot follow "
                "task.project_id.mcp_server_ids without it."
            )

        self.workspace = self.Workspace.sudo().create({'name': 'WS Chat MCP'})
        self.template = self.Template.sudo().create({
            'name': 'Tmpl Chat MCP',
            'slug': 'tmpl-chat-mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-chat-mcp',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/test/mcp:latest',
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        })
        self.service = self.Service.sudo().create({
            'name': 'Svc Chat MCP',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'chat-svc',
            'helm_namespace': 'paas-ws-chat',
            'mcp_auth_token': str(uuid.uuid4()),
        })
        self.project_bound = self.Project.sudo().create({
            'name': 'Bound Chat Project',
            'cloud_service_id': self.service.id,
        })
        self.project_unbound = self.Project.sudo().create({
            'name': 'Unbound Chat Project',
        })

    # ------------------------------------------------------------------ #
    # AC #13: bound project → channel sees user-scope MCP tools           #
    # ------------------------------------------------------------------ #
    def test_ac13_channel_gets_user_mcp_via_project(self):
        """AC #13: channel._get_user_mcp_tools walks via project_id.mcp_server_ids.

        New chain: channel → task → project_id → mcp_server_ids → tool_ids
        (no cloud_service hop).  Seed a user-scope MCP linked **only by
        project_id** (no cloud_service_id) and one active tool, then call
        the channel consumer.  If the consumer still walks the old chain
        (cloud_service_id.user_mcp_server_ids), it will return an empty
        recordset.
        """
        channel = self.Channel.sudo().create({
            'name': 'Bound Chat Channel',
            'channel_type': 'channel',
        })
        self.Task.sudo().create({
            'name': 'Bound Chat Task',
            'project_id': self.project_bound.id,
            'channel_id': channel.id,
        })

        # Note: 'state' set to 'connected' so the filter in
        # _get_user_mcp_tools(.filtered('state == "connected"')) passes.
        mcp = self.McpServer.sudo().create({
            'name': 'Bound Project User MCP',
            'url': 'http://chat-svc-mcp.paas-ws-chat.svc.cluster.local:3001/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project_bound.id,
            'state': 'connected',
        })
        tool = self.McpTool.sudo().create({
            'name': 'list_workflows',
            'description': 'List workflows',
            'input_schema': '{}',
            'server_id': mcp.id,
        })

        tools = channel._get_user_mcp_tools()

        self.assertEqual(
            tools, tool,
            "AC #13: channel._get_user_mcp_tools must return the project's "
            "user-scope MCP tools. Got empty — consumer probably still "
            "walks task.project_id.cloud_service_id.user_mcp_server_ids "
            "(legacy chain) instead of task.project_id.mcp_server_ids.",
        )

    # ------------------------------------------------------------------ #
    # AC #13 negative: unbound project → channel sees empty               #
    # ------------------------------------------------------------------ #
    def test_ac13_channel_no_mcp_for_unbound_project(self):
        """AC #13 negative: unbound project (no cloud_service_id) → empty.

        D5 constraint forbids user-scope MCP rows on unbound projects,
        but the consumer must still degrade gracefully (return an empty
        recordset rather than raise) when invoked on a channel whose
        task points to an unbound project — e.g. a manually created
        project with no service yet.
        """
        channel = self.Channel.sudo().create({
            'name': 'Unbound Chat Channel',
            'channel_type': 'channel',
        })
        self.Task.sudo().create({
            'name': 'Unbound Chat Task',
            'project_id': self.project_unbound.id,
            'channel_id': channel.id,
        })

        # No MCP row attached — unbound project has no user-scope MCP
        # by D5 invariant. Consumer must not crash.
        try:
            tools = channel._get_user_mcp_tools()
        except Exception as exc:  # noqa: BLE001
            self.fail(
                f"AC #13 negative: channel._get_user_mcp_tools raised "
                f"{type(exc).__name__} on unbound project: {exc}. "
                f"Consumer must degrade gracefully (return empty recordset)."
            )

        self.assertFalse(
            tools,
            "AC #13 negative: unbound project channel returned non-empty "
            "tools — recordset must be empty.",
        )
        # Strong type check — must be an mcp_tool recordset (not None / list).
        self.assertEqual(
            tools._name, 'woow_paas_platform.mcp_tool',
            "AC #13 negative: empty result should still be an mcp_tool "
            "recordset (so callers can | with other recordsets without "
            "type errors).",
        )

    # ------------------------------------------------------------------ #
    # AC #15: ai.assistant.mcp_server_ids system path unchanged           #
    # ------------------------------------------------------------------ #
    def test_ac15_assistant_system_mcp_unchanged(self):
        """AC #15: ai.assistant.mcp_server_ids still returns system MCP.

        The M2M domain on ``ai.assistant.mcp_server_ids`` is
        ``[('scope', '=', 'system'), ('active', '=', True)]`` (see
        models/ai_assistant.py:7-11) — refactor must not regress this.
        Regression lock: ensure ``get_enabled_mcp_tools`` still returns
        the tools of an attached system MCP.
        """
        system_mcp = self.McpServer.sudo().create({
            'name': 'System Chat MCP',
            'url': 'http://platform.mcp/sse',
            'transport': 'sse',
            'scope': 'system',
        })
        system_tool = self.McpTool.sudo().create({
            'name': 'platform_get_status',
            'description': 'Platform status',
            'input_schema': '{}',
            'server_id': system_mcp.id,
        })

        # Build the assistant — sh from data file or build minimal record.
        # We follow the safe path: take any existing system-mcp-capable
        # assistant if seeded, otherwise create one against the inherited
        # ai.assistant model.
        Assistant = self.env['ai.assistant']
        assistant = Assistant.sudo().create({
            'name': 'TestSystemAssistant',
            'mcp_server_ids': [(6, 0, [system_mcp.id])],
        })

        tools = assistant.get_enabled_mcp_tools()
        self.assertIn(
            system_tool, tools,
            "AC #15 regression: ai.assistant.mcp_server_ids no longer "
            "exposes system-scope MCP tools. Refactor must not change "
            "the M2M domain or get_enabled_mcp_tools behaviour.",
        )
