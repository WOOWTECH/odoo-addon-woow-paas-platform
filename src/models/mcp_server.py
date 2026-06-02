import asyncio
import json
import logging

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)


class McpServer(models.Model):
    _name = 'woow_paas_platform.mcp_server'
    _description = 'MCP Server'
    _order = 'name'

    name = fields.Char(
        string='Name',
        required=True,
        index=True,
    )
    url = fields.Char(
        string='Endpoint URL',
        required=True,
        help='MCP Server endpoint URL (e.g. https://mcp.example.com/sse)',
    )
    transport = fields.Selection(
        selection=[
            ('sse', 'SSE'),
            ('streamable_http', 'Streamable HTTP'),
        ],
        string='Transport',
        default='sse',
        required=True,
    )
    api_key = fields.Char(
        string='API Key',
        groups='base.group_system',
    )
    headers_json = fields.Text(
        string='Custom Headers (JSON)',
        help='Additional HTTP headers as JSON object, e.g. {"Authorization": "Bearer xxx"}',
    )
    description = fields.Text(
        string='Description',
    )
    active = fields.Boolean(
        default=True,
    )
    scope = fields.Selection(
        selection=[
            ('system', 'System'),
            ('user', 'User'),
        ],
        string='Scope',
        default='system',
        required=True,
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        ondelete='cascade',
        index=True,
        help='Only set for user-scoped MCP servers. Cascade-delete with project.',
    )
    tool_ids = fields.One2many(
        'woow_paas_platform.mcp_tool',
        'server_id',
        string='Tools',
    )
    tool_count = fields.Integer(
        string='Tool Count',
        compute='_compute_tool_count',
        store=True,
    )
    last_sync = fields.Datetime(
        string='Last Sync',
        readonly=True,
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('connected', 'Connected'),
            ('error', 'Error'),
        ],
        string='State',
        default='draft',
        readonly=True,
    )
    state_message = fields.Text(
        string='State Message',
        readonly=True,
    )
    auto_created = fields.Boolean(
        string='Auto Created (deprecated)',
        default=False,
        readonly=True,
        help='Deprecated; use sidecar_backed for new code paths.',
    )
    sidecar_backed = fields.Boolean(
        string='Sidecar Backed',
        default=False,
        readonly=True,
        help='MCP record was populated from a sidecar URL hint and is tied to '
             'the backing cloud service lifecycle',
    )
    sync_retry_count = fields.Integer(
        string='Sync Retry Count',
        default=0,
        readonly=True,
        help='Number of sync retry attempts for auto-created servers',
    )

    @api.depends('tool_ids')
    def _compute_tool_count(self):
        for record in self:
            record.tool_count = len(record.tool_ids)

    @api.constrains('scope', 'project_id')
    def _check_scope_project_consistency(self):
        # D4 invariant: scope='user' iff project_id IS NOT NULL.
        for record in self:
            if record.scope == 'user' and not record.project_id:
                raise ValidationError(
                    "User-scope MCP server '%s' must be linked to a project "
                    "(scope='user' requires project_id)." % record.name
                )
            if record.scope == 'system' and record.project_id:
                raise ValidationError(
                    "System-scope MCP server '%s' must not be linked to a "
                    "project (scope='system' requires project_id=NULL)."
                    % record.name
                )

    def write(self, vals):
        # scope / project_id 對非 admin 不可變更：擋窄→寬 flip（user-scope →
        # system-scope = 平台公開、載進每個租戶 pod → 跨租戶外洩）。ir.rule 擋不住
        # —— Odoo 對 write rule 用 PRE-write 狀態判定（flip 前 row 仍是合法
        # scope='user'、owner 仍是 workspace member），rule 放行，改完才變寬 scope。
        # 鏡像 skill.py 的同款 guard（sibling model parity，issue #98 / F-SDS-1）。
        # pattern: docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md
        if ('scope' in vals or 'project_id' in vals) and not self.env.su \
                and not self.env.user.has_group('base.group_system'):
            for server in self:
                if 'scope' in vals and vals['scope'] != server.scope:
                    raise AccessError(
                        "Only administrators can change an MCP server's scope."
                    )
                if 'project_id' in vals \
                        and vals.get('project_id') != server.project_id.id:
                    raise AccessError(
                        "Only administrators can change an MCP server's project."
                    )
        result = super().write(vals)
        # #111 parity: mcp snapshot 只序列化 server 的 name/url/transport
        # (_build_mcp_snapshot)，故僅這些欄位變動才改 pod 看到的 mcp.json →
        # 觸發 best-effort 推送。active/state/state_message/tool 變動不改 snapshot
        # 故不推（mcp_tool 同理不觸發）。
        _MCP_SNAPSHOT_FIELDS = {'name', 'url', 'transport'}
        if _MCP_SNAPSHOT_FIELDS & set(vals):
            Session = self.env['woow_paas_platform.agent_session']
            for server in self:
                Session._push_snapshot_to_running_pods(
                    'mcp', server.scope, server.project_id.id or None)
        return result

    def _get_mcp_client_config(self):
        """Build config dict for MultiServerMCPClient."""
        config = {
            'transport': self.transport,
            'url': self.url,
        }
        if self.api_key:
            headers = {'Authorization': f'Bearer {self.api_key}'}
        else:
            headers = {}
        if self.headers_json:
            try:
                extra_headers = json.loads(self.headers_json)
                headers.update(extra_headers)
            except (json.JSONDecodeError, TypeError):
                _logger.warning("Invalid headers_json for MCP server %s", self.name)
        if headers:
            config['headers'] = headers
        return config

    def action_sync_tools(self):
        """Sync tools from the MCP server via langchain-mcp-adapters."""
        self.ensure_one()
        try:
            result = asyncio.run(self._async_sync_tools())
            self.write({
                'state': 'connected',
                'state_message': False,
                'last_sync': fields.Datetime.now(),
                'sync_retry_count': 0,
            })
            return result
        except Exception as e:
            _logger.warning("MCP sync failed for %s: %s", self.name, e)
            self.write({
                'state': 'error',
                'state_message': str(e),
            })

    def action_sync_tools_safe(self):
        """Sync tools without setting error state on failure.

        Used by auto-created MCP servers where the sidecar may not be
        ready yet.  Keeps state as 'draft' on failure so the cron retry
        mechanism can pick it up later.

        Returns:
            bool: True if sync succeeded, False otherwise.
        """
        self.ensure_one()
        try:
            asyncio.run(self._async_sync_tools())
            self.write({
                'state': 'connected',
                'state_message': False,
                'last_sync': fields.Datetime.now(),
                'sync_retry_count': 0,
            })
            return True
        except Exception as e:
            _logger.info(
                "MCP sync (safe) not ready for %s (attempt %d): %s",
                self.name, self.sync_retry_count + 1, e,
            )
            self.write({
                'sync_retry_count': self.sync_retry_count + 1,
                'state_message': f"Waiting for sidecar (attempt {self.sync_retry_count + 1}): {e}",
            })
            return False

    @api.model
    def _cron_retry_mcp_sync(self):
        """Cron job: retry tool sync for auto-created MCP servers in draft state.

        Finds auto-created servers still in 'draft' state and retries
        tool discovery.  After MAX_SYNC_RETRIES (10) failed attempts the
        server is moved to 'error' state with a descriptive message.
        """
        MAX_SYNC_RETRIES = 10

        servers = self.sudo().search([
            ('auto_created', '=', True),
            ('state', '=', 'draft'),
        ])

        if not servers:
            return

        _logger.info(
            "MCP sync retry cron: found %d auto-created server(s) pending sync",
            len(servers),
        )

        for server in servers:
            if server.sync_retry_count >= MAX_SYNC_RETRIES:
                server.write({
                    'state': 'error',
                    'state_message': (
                        f"MCP sidecar sync failed after {MAX_SYNC_RETRIES} attempts. "
                        "The sidecar may not be running. "
                        "Use the Cloud Service settings page to manually re-sync."
                    ),
                })
                _logger.warning(
                    "MCP Server '%s' (id=%s) exceeded max retries (%d), marking as error",
                    server.name, server.id, MAX_SYNC_RETRIES,
                )
                continue

            _logger.info(
                "Retrying MCP sync for '%s' (id=%s, attempt %d/%d)",
                server.name, server.id, server.sync_retry_count + 1, MAX_SYNC_RETRIES,
            )
            server.action_sync_tools_safe()

    async def _async_sync_tools(self):
        """Async: connect to MCP server and discover tools."""
        from langchain_mcp_adapters.client import MultiServerMCPClient

        config = {self.name: self._get_mcp_client_config()}
        client = MultiServerMCPClient(config)
        tools = await client.get_tools()

        # Process discovered tools back in sync context
        McpTool = self.env['woow_paas_platform.mcp_tool']
        existing = {t.name: t for t in self.tool_ids}
        discovered_names = set()

        for tool in tools:
            discovered_names.add(tool.name)
            schema = {}
            if hasattr(tool, 'args_schema'):
                schema = tool.args_schema
            elif hasattr(tool, 'args'):
                schema = tool.args
            input_schema = json.dumps(schema) if schema else '{}'
            vals = {
                'name': tool.name,
                'description': tool.description or '',
                'input_schema': input_schema,
                'server_id': self.id,
            }
            if tool.name in existing:
                existing[tool.name].write(vals)
            else:
                McpTool.create(vals)

        # Deactivate tools that are no longer discovered
        for name, record in existing.items():
            if name not in discovered_names:
                record.write({'active': False})

        return True

    def action_test_connection(self):
        """Test if the MCP server URL is reachable."""
        self.ensure_one()
        try:
            asyncio.run(self._async_test_connection())
            self.write({
                'state': 'connected',
                'state_message': False,
            })
        except Exception as e:
            _logger.warning("MCP connection test failed for %s: %s", self.name, e)
            self.write({
                'state': 'error',
                'state_message': str(e),
            })

    async def _async_test_connection(self):
        """Async: attempt to connect to MCP server."""
        from langchain_mcp_adapters.client import MultiServerMCPClient

        config = {self.name: self._get_mcp_client_config()}
        client = MultiServerMCPClient(config)
        # If we can connect and get tools, the server is reachable
        await client.get_tools()
