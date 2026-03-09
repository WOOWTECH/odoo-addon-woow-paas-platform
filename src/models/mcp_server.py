import asyncio
import json
import logging

from odoo import api, fields, models

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
    cloud_service_id = fields.Many2one(
        'woow_paas_platform.cloud_service',
        string='Cloud Service',
        ondelete='cascade',
        help='Only set for user-scoped MCP servers',
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

    @api.depends('tool_ids')
    def _compute_tool_count(self):
        for record in self:
            record.tool_count = len(record.tool_ids)

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
            })
            return result
        except Exception as e:
            _logger.warning("MCP sync failed for %s: %s", self.name, e)
            self.write({
                'state': 'error',
                'state_message': str(e),
            })

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
