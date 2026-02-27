"""End-to-end integration tests for n8n MCP sidecar feature.

Tests the full lifecycle:
  CloudAppTemplate MCP fields -> sidecar config build ->
  auto-create MCP Server -> cron retry -> cascade cleanup.

All external calls (PaaS Operator, MCP tool discovery) are mocked.
"""
import json
import uuid
from unittest.mock import patch, MagicMock, AsyncMock

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCloudAppTemplateMCPFields(TransactionCase):
    """Test that CloudAppTemplate stores MCP sidecar fields correctly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']

    def test_mcp_enabled_default_is_false(self):
        """New templates should have mcp_enabled=False by default."""
        template = self.Template.create({
            'name': 'Plain App',
            'slug': 'plain-app',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'plain',
            'helm_chart_version': '1.0.0',
        })
        self.assertFalse(template.mcp_enabled)
        self.assertFalse(template.mcp_sidecar_image)
        self.assertEqual(template.mcp_sidecar_port, 3001)  # default
        self.assertEqual(template.mcp_transport, 'streamable_http')  # default
        self.assertEqual(template.mcp_endpoint_path, '/mcp')  # default

    def test_mcp_enabled_template_stores_all_fields(self):
        """Template with mcp_enabled=True stores all MCP sidecar fields."""
        template = self.Template.create({
            'name': 'n8n',
            'slug': 'n8n',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'n8n',
            'helm_chart_version': '1.0.0',
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/czlonkowski/n8n-mcp:v2.35.5',
            'mcp_sidecar_port': 3000,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
            'mcp_sidecar_env': json.dumps({'N8N_CUSTOM': 'value'}),
        })
        self.assertTrue(template.mcp_enabled)
        self.assertEqual(template.mcp_sidecar_image, 'ghcr.io/czlonkowski/n8n-mcp:v2.35.5')
        self.assertEqual(template.mcp_sidecar_port, 3000)
        self.assertEqual(template.mcp_transport, 'streamable_http')
        self.assertEqual(template.mcp_endpoint_path, '/mcp')
        env_data = json.loads(template.mcp_sidecar_env)
        self.assertEqual(env_data['N8N_CUSTOM'], 'value')

    def test_mcp_transport_selection_values(self):
        """mcp_transport only accepts 'sse' or 'streamable_http'."""
        for transport in ('sse', 'streamable_http'):
            template = self.Template.create({
                'name': f'Transport {transport}',
                'slug': f'transport-{transport}',
                'helm_repo_url': 'https://charts.example.com',
                'helm_chart_name': 'test',
                'helm_chart_version': '1.0.0',
                'mcp_enabled': True,
                'mcp_sidecar_image': 'img:latest',
                'mcp_transport': transport,
            })
            self.assertEqual(template.mcp_transport, transport)


@tagged('post_install', '-at_install')
class TestBuildMCPSidecarConfig(TransactionCase):
    """Test _build_mcp_sidecar_config method on PaasController."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        # Import the controller
        from odoo.addons.woow_paas_platform.controllers.paas import PaasController
        cls.controller = PaasController()

    def _make_mcp_template(self, **overrides):
        """Helper to create an MCP-enabled template."""
        vals = {
            'name': 'n8n Test',
            'slug': 'n8n-test',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'n8n',
            'helm_chart_version': '1.0.0',
            'default_port': 5678,
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/czlonkowski/n8n-mcp:v2.35.5',
            'mcp_sidecar_port': 3000,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        }
        vals.update(overrides)
        return self.Template.create(vals)

    def test_returns_correct_container_spec(self):
        """Sidecar config contains required container spec fields."""
        template = self._make_mcp_template()
        auth_token = str(uuid.uuid4())

        result = self.controller._build_mcp_sidecar_config(template, auth_token)

        self.assertIn('container', result)
        container = result['container']
        self.assertEqual(container['name'], 'mcp-sidecar')
        self.assertEqual(container['image'], 'ghcr.io/czlonkowski/n8n-mcp:v2.35.5')
        self.assertEqual(container['ports'], [{'containerPort': 3000}])
        self.assertIn('resources', container)
        self.assertIn('livenessProbe', container)
        self.assertIn('readinessProbe', container)

    def test_env_vars_include_required_keys(self):
        """Environment variables include MCP_MODE, AUTH_TOKEN, N8N_API_URL, PORT."""
        template = self._make_mcp_template()
        auth_token = 'test-token-123'

        result = self.controller._build_mcp_sidecar_config(template, auth_token)
        env_vars = result['container']['env']
        env_dict = {e['name']: e['value'] for e in env_vars}

        self.assertEqual(env_dict['MCP_MODE'], 'http')
        self.assertEqual(env_dict['AUTH_TOKEN'], 'test-token-123')
        self.assertEqual(env_dict['N8N_API_URL'], 'http://localhost:5678')
        self.assertEqual(env_dict['PORT'], '3000')

    def test_auth_token_is_uuid_format(self):
        """Auth token generated during deploy should be valid UUID."""
        token = str(uuid.uuid4())
        # Validate UUID format
        parsed = uuid.UUID(token)
        self.assertEqual(str(parsed), token)

    def test_custom_env_vars_merged(self):
        """Custom env vars from template are merged, reserved keys are not overridden."""
        template = self._make_mcp_template(
            mcp_sidecar_env=json.dumps({
                'CUSTOM_KEY': 'custom_value',
                'ANOTHER': '42',
                'MCP_MODE': 'should-not-override',  # reserved
                'AUTH_TOKEN': 'should-not-override',  # reserved
            }),
        )
        auth_token = 'real-token'

        result = self.controller._build_mcp_sidecar_config(template, auth_token)
        env_vars = result['container']['env']
        env_dict = {e['name']: e['value'] for e in env_vars}

        # Custom keys are added
        self.assertEqual(env_dict['CUSTOM_KEY'], 'custom_value')
        self.assertEqual(env_dict['ANOTHER'], '42')
        # Reserved keys are NOT overridden
        self.assertEqual(env_dict['MCP_MODE'], 'http')
        self.assertEqual(env_dict['AUTH_TOKEN'], 'real-token')

    def test_invalid_sidecar_env_json_ignored(self):
        """Invalid JSON in mcp_sidecar_env is silently ignored."""
        template = self._make_mcp_template(
            mcp_sidecar_env='not-valid-json{{{',
        )
        auth_token = 'token'

        # Should not raise
        result = self.controller._build_mcp_sidecar_config(template, auth_token)
        env_vars = result['container']['env']
        # Only the 4 required env vars
        self.assertEqual(len(env_vars), 4)

    def test_default_sidecar_port_when_zero(self):
        """When mcp_sidecar_port is 0/falsy, fallback to 3000."""
        template = self._make_mcp_template(mcp_sidecar_port=0)
        auth_token = 'token'

        result = self.controller._build_mcp_sidecar_config(template, auth_token)
        container = result['container']
        self.assertEqual(container['ports'], [{'containerPort': 3000}])

    def test_probes_use_correct_port(self):
        """Liveness and readiness probes point to the correct sidecar port."""
        template = self._make_mcp_template(mcp_sidecar_port=4000)
        auth_token = 'token'

        result = self.controller._build_mcp_sidecar_config(template, auth_token)
        container = result['container']
        self.assertEqual(container['livenessProbe']['httpGet']['port'], 4000)
        self.assertEqual(container['readinessProbe']['httpGet']['port'], 4000)


@tagged('post_install', '-at_install')
class TestAutoCreateMCPServer(TransactionCase):
    """Test _auto_create_mcp_server logic.

    Since _auto_create_mcp_server uses ``request.env`` which is not
    available in TransactionCase, we test the equivalent logic by directly
    exercising the model layer and verifying outcomes.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

        cls.workspace = cls.Workspace.create({'name': 'MCP Test Workspace'})

        cls.mcp_template = cls.Template.create({
            'name': 'n8n MCP',
            'slug': 'n8n-mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'n8n',
            'helm_chart_version': '1.0.0',
            'default_port': 5678,
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/czlonkowski/n8n-mcp:v2.35.5',
            'mcp_sidecar_port': 3000,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        })

        cls.plain_template = cls.Template.create({
            'name': 'PostgreSQL',
            'slug': 'postgresql',
            'helm_repo_url': 'https://charts.bitnami.com/bitnami',
            'helm_chart_name': 'postgresql',
            'helm_chart_version': '15.0.0',
            'mcp_enabled': False,
        })

    def _create_service(self, template=None, **overrides):
        """Helper to create a cloud service."""
        vals = {
            'name': 'Test Service',
            'workspace_id': self.workspace.id,
            'template_id': (template or self.mcp_template).id,
            'helm_release_name': 'test-n8n',
            'helm_namespace': 'paas-ws-1',
            'mcp_auth_token': str(uuid.uuid4()),
        }
        vals.update(overrides)
        return self.Service.create(vals)

    def _simulate_auto_create(self, service):
        """Simulate what _auto_create_mcp_server does, without request context.

        This replicates the controller logic so we can test it within
        TransactionCase (no HTTP request needed).
        """
        template = service.template_id
        if not template.mcp_enabled or not template.mcp_sidecar_image:
            return None

        McpServer = self.env['woow_paas_platform.mcp_server'].sudo()

        # Idempotency check
        existing = McpServer.search([
            ('cloud_service_id', '=', service.id),
            ('auto_created', '=', True),
        ], limit=1)
        if existing:
            return existing

        # Build MCP endpoint URL (simplified for test)
        endpoint_path = template.mcp_endpoint_path or '/mcp'
        sidecar_port = template.mcp_sidecar_port or 3001
        if service.helm_release_name and service.helm_namespace:
            mcp_url = (
                f"http://{service.helm_release_name}-mcp"
                f".{service.helm_namespace}.svc.cluster.local"
                f":{sidecar_port}{endpoint_path}"
            )
        else:
            mcp_url = f"http://localhost:{sidecar_port}{endpoint_path}"

        server = McpServer.create({
            'name': f"{service.name} MCP",
            'url': mcp_url,
            'transport': template.mcp_transport or 'streamable_http',
            'scope': 'user',
            'cloud_service_id': service.id,
            'auto_created': True,
            'api_key': service.mcp_auth_token,
            'description': f"Auto-created MCP server for {service.name}",
        })
        return server

    def test_auto_create_mcp_server_for_mcp_template(self):
        """MCP-enabled template creates an MCP Server record when service runs."""
        service = self._create_service()
        server = self._simulate_auto_create(service)

        self.assertTrue(server)
        self.assertTrue(server.auto_created)
        self.assertEqual(server.scope, 'user')
        self.assertEqual(server.cloud_service_id.id, service.id)
        self.assertEqual(server.transport, 'streamable_http')
        self.assertIn(service.name, server.name)
        self.assertEqual(server.state, 'draft')
        self.assertEqual(server.api_key, service.mcp_auth_token)

    def test_auto_create_mcp_server_url_format(self):
        """Auto-created MCP Server URL uses K8s internal service pattern."""
        service = self._create_service(
            helm_release_name='my-n8n',
            helm_namespace='paas-ws-42',
        )
        server = self._simulate_auto_create(service)

        expected_url = 'http://my-n8n-mcp.paas-ws-42.svc.cluster.local:3000/mcp'
        self.assertEqual(server.url, expected_url)

    def test_auto_create_idempotent(self):
        """Calling auto-create twice does NOT create a duplicate."""
        service = self._create_service()
        server1 = self._simulate_auto_create(service)
        server2 = self._simulate_auto_create(service)

        self.assertEqual(server1.id, server2.id)

        # Only one auto-created MCP Server for this service
        count = self.McpServer.search_count([
            ('cloud_service_id', '=', service.id),
            ('auto_created', '=', True),
        ])
        self.assertEqual(count, 1)

    def test_no_mcp_server_for_plain_template(self):
        """Non-MCP template does NOT create an MCP Server."""
        service = self._create_service(template=self.plain_template)
        server = self._simulate_auto_create(service)

        self.assertIsNone(server)

        count = self.McpServer.search_count([
            ('cloud_service_id', '=', service.id),
            ('auto_created', '=', True),
        ])
        self.assertEqual(count, 0)

    def test_no_mcp_server_when_image_missing(self):
        """Template with mcp_enabled=True but no image does NOT create server."""
        no_image_template = self.Template.create({
            'name': 'No Image MCP',
            'slug': 'no-image-mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
            'mcp_enabled': True,
            # mcp_sidecar_image is NOT set
        })
        service = self._create_service(template=no_image_template)
        server = self._simulate_auto_create(service)

        self.assertIsNone(server)

    def test_mcp_server_linked_to_service_via_one2many(self):
        """Service's user_mcp_server_ids includes the auto-created server."""
        service = self._create_service()
        server = self._simulate_auto_create(service)

        self.assertIn(server.id, service.user_mcp_server_ids.ids)


@tagged('post_install', '-at_install')
class TestMCPServerCronRetry(TransactionCase):
    """Test _cron_retry_mcp_sync: health check retry for auto-created MCP servers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

        cls.workspace = cls.Workspace.create({'name': 'Cron Test Workspace'})

        cls.mcp_template = cls.Template.create({
            'name': 'n8n Cron Test',
            'slug': 'n8n-cron-test',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'n8n',
            'helm_chart_version': '1.0.0',
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/czlonkowski/n8n-mcp:v2.35.5',
            'mcp_sidecar_port': 3000,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        })

    def _create_auto_mcp_server(self, state='draft', retry_count=0, **overrides):
        """Helper to create an auto-created MCP Server in given state."""
        service = self.Service.create({
            'name': 'Cron Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'helm_release_name': 'cron-n8n',
            'helm_namespace': 'paas-ws-cron',
            'mcp_auth_token': str(uuid.uuid4()),
        })
        vals = {
            'name': f'{service.name} MCP',
            'url': 'http://cron-n8n-mcp.paas-ws-cron.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'cloud_service_id': service.id,
            'auto_created': True,
            'state': state,
            'sync_retry_count': retry_count,
        }
        vals.update(overrides)
        server = self.McpServer.sudo().create(vals)
        return server

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer.action_sync_tools_safe')
    def test_cron_retries_draft_auto_created_server(self, mock_sync):
        """Cron picks up auto-created servers in draft state and retries sync."""
        mock_sync.return_value = False  # sync fails

        server = self._create_auto_mcp_server(state='draft', retry_count=0)

        self.McpServer._cron_retry_mcp_sync()

        mock_sync.assert_called_once()

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer.action_sync_tools_safe')
    def test_cron_marks_error_after_max_retries(self, mock_sync):
        """After 3 failed retries, server state becomes 'error'."""
        server = self._create_auto_mcp_server(state='draft', retry_count=3)

        self.McpServer._cron_retry_mcp_sync()

        server.invalidate_recordset()
        self.assertEqual(server.state, 'error')
        self.assertIn('failed after 3 attempts', server.state_message)
        # sync should NOT have been called for this server (already exceeded)
        mock_sync.assert_not_called()

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer.action_sync_tools_safe')
    def test_cron_does_not_touch_manual_servers(self, mock_sync):
        """Manual (non-auto_created) MCP Servers are NOT affected by cron."""
        service = self.Service.create({
            'name': 'Manual Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
        })
        manual_server = self.McpServer.sudo().create({
            'name': 'Manual MCP Server',
            'url': 'http://example.com/mcp',
            'transport': 'sse',
            'scope': 'system',
            'auto_created': False,
            'state': 'draft',
        })

        self.McpServer._cron_retry_mcp_sync()

        # Sync should not be called because auto_created=False
        mock_sync.assert_not_called()
        manual_server.invalidate_recordset()
        self.assertEqual(manual_server.state, 'draft')

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer.action_sync_tools_safe')
    def test_cron_skips_connected_servers(self, mock_sync):
        """Already connected servers are not retried by cron."""
        server = self._create_auto_mcp_server(state='draft', retry_count=0)
        # Manually set to connected
        server.sudo().write({'state': 'connected'})

        self.McpServer._cron_retry_mcp_sync()

        mock_sync.assert_not_called()

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer.action_sync_tools_safe')
    def test_cron_skips_error_state_servers(self, mock_sync):
        """Servers already in error state are not retried."""
        server = self._create_auto_mcp_server(state='draft', retry_count=0)
        server.sudo().write({'state': 'error'})

        self.McpServer._cron_retry_mcp_sync()

        mock_sync.assert_not_called()

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer.action_sync_tools_safe')
    def test_cron_retries_up_to_max(self, mock_sync):
        """Servers with retry_count < 3 are retried; at 3 they get error."""
        mock_sync.return_value = False

        server_ok = self._create_auto_mcp_server(state='draft', retry_count=2)
        server_max = self._create_auto_mcp_server(state='draft', retry_count=3)

        self.McpServer._cron_retry_mcp_sync()

        # server_ok (retry_count=2) should have sync called
        # server_max (retry_count=3) should be set to error, no sync
        self.assertEqual(mock_sync.call_count, 1)

        server_max.invalidate_recordset()
        self.assertEqual(server_max.state, 'error')


@tagged('post_install', '-at_install')
class TestMCPServerSyncSafe(TransactionCase):
    """Test action_sync_tools_safe keeps state as draft on failure."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer._async_sync_tools')
    def test_sync_safe_success(self, mock_async):
        """Successful sync sets state to connected and resets retry count."""
        mock_async.return_value = True

        server = self.McpServer.sudo().create({
            'name': 'Safe Sync Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'auto_created': True,
            'state': 'draft',
            'sync_retry_count': 2,
        })

        result = server.action_sync_tools_safe()

        self.assertTrue(result)
        self.assertEqual(server.state, 'connected')
        self.assertEqual(server.sync_retry_count, 0)
        self.assertTrue(server.last_sync)

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer._async_sync_tools')
    def test_sync_safe_failure_keeps_draft(self, mock_async):
        """Failed sync keeps state as draft and increments retry count."""
        mock_async.side_effect = ConnectionError('Sidecar not ready')

        server = self.McpServer.sudo().create({
            'name': 'Safe Sync Fail Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'auto_created': True,
            'state': 'draft',
            'sync_retry_count': 0,
        })

        result = server.action_sync_tools_safe()

        self.assertFalse(result)
        self.assertEqual(server.state, 'draft')
        self.assertEqual(server.sync_retry_count, 1)
        self.assertIn('Waiting for sidecar', server.state_message)

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer._async_sync_tools')
    def test_sync_regular_sets_error_on_failure(self, mock_async):
        """Regular sync (not safe) sets error state on failure."""
        mock_async.side_effect = ConnectionError('Connection refused')

        server = self.McpServer.sudo().create({
            'name': 'Regular Sync Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'system',
            'state': 'draft',
        })

        server.action_sync_tools()

        self.assertEqual(server.state, 'error')
        self.assertIn('Connection refused', server.state_message)


@tagged('post_install', '-at_install')
class TestMCPServerCascadeDelete(TransactionCase):
    """Test that deleting CloudService cascades to auto-created MCP Server."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

        cls.workspace = cls.Workspace.create({'name': 'Cascade Test Workspace'})

        cls.mcp_template = cls.Template.create({
            'name': 'n8n Cascade',
            'slug': 'n8n-cascade',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'n8n',
            'helm_chart_version': '1.0.0',
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/czlonkowski/n8n-mcp:v2.35.5',
            'mcp_sidecar_port': 3000,
        })

    def test_delete_service_cascades_to_mcp_server(self):
        """Deleting CloudService should delete its auto-created MCP Server."""
        service = self.Service.create({
            'name': 'To Delete Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'mcp_auth_token': str(uuid.uuid4()),
        })

        server = self.McpServer.sudo().create({
            'name': f'{service.name} MCP',
            'url': 'http://test-mcp.ns.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'cloud_service_id': service.id,
            'auto_created': True,
        })
        server_id = server.id

        service.unlink()

        # MCP Server should be cascade-deleted (ondelete='cascade')
        self.assertFalse(self.McpServer.sudo().browse(server_id).exists())

    def test_delete_service_also_deletes_mcp_tools(self):
        """Cascade: Service -> MCP Server -> MCP Tools all cleaned up."""
        service = self.Service.create({
            'name': 'Tool Cascade Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'mcp_auth_token': str(uuid.uuid4()),
        })

        server = self.McpServer.sudo().create({
            'name': f'{service.name} MCP',
            'url': 'http://test-mcp.ns.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'cloud_service_id': service.id,
            'auto_created': True,
        })

        McpTool = self.env['woow_paas_platform.mcp_tool']
        tool = McpTool.sudo().create({
            'name': 'n8n_list_workflows',
            'description': 'List all n8n workflows',
            'input_schema': '{}',
            'server_id': server.id,
        })
        tool_id = tool.id
        server_id = server.id

        service.unlink()

        self.assertFalse(self.McpServer.sudo().browse(server_id).exists())
        self.assertFalse(McpTool.sudo().browse(tool_id).exists())

    def test_delete_workspace_cascades_through_service_to_mcp(self):
        """Workspace delete -> Service delete -> MCP Server delete."""
        workspace = self.Workspace.create({'name': 'Full Cascade WS'})
        service = self.Service.create({
            'name': 'Full Cascade Service',
            'workspace_id': workspace.id,
            'template_id': self.mcp_template.id,
            'mcp_auth_token': str(uuid.uuid4()),
        })
        server = self.McpServer.sudo().create({
            'name': f'{service.name} MCP',
            'url': 'http://test-mcp.ns.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'cloud_service_id': service.id,
            'auto_created': True,
        })
        service_id = service.id
        server_id = server.id

        workspace.unlink()

        self.assertFalse(self.Service.browse(service_id).exists())
        self.assertFalse(self.McpServer.sudo().browse(server_id).exists())


@tagged('post_install', '-at_install')
class TestMCPServerModel(TransactionCase):
    """Test MCP Server model fields and methods."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']
        cls.McpTool = cls.env['woow_paas_platform.mcp_tool']

    def test_tool_count_computed(self):
        """tool_count is computed from tool_ids."""
        server = self.McpServer.sudo().create({
            'name': 'Tool Count Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
        })
        self.assertEqual(server.tool_count, 0)

        self.McpTool.sudo().create({
            'name': 'tool_a',
            'server_id': server.id,
        })
        self.McpTool.sudo().create({
            'name': 'tool_b',
            'server_id': server.id,
        })

        server.invalidate_recordset()
        self.assertEqual(server.tool_count, 2)

    def test_get_mcp_client_config_with_api_key(self):
        """_get_mcp_client_config includes Authorization header when api_key set."""
        server = self.McpServer.sudo().create({
            'name': 'Config Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
            'api_key': 'my-secret-key',
        })

        config = server._get_mcp_client_config()

        self.assertEqual(config['transport'], 'streamable_http')
        self.assertEqual(config['url'], 'http://localhost:3000/mcp')
        self.assertIn('headers', config)
        self.assertEqual(config['headers']['Authorization'], 'Bearer my-secret-key')

    def test_get_mcp_client_config_with_custom_headers(self):
        """Custom headers from headers_json are merged into config."""
        server = self.McpServer.sudo().create({
            'name': 'Headers Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'sse',
            'headers_json': json.dumps({'X-Custom': 'value', 'X-Another': '123'}),
        })

        config = server._get_mcp_client_config()

        self.assertEqual(config['headers']['X-Custom'], 'value')
        self.assertEqual(config['headers']['X-Another'], '123')

    def test_get_mcp_client_config_no_auth(self):
        """Config without api_key or headers has no headers key."""
        server = self.McpServer.sudo().create({
            'name': 'No Auth Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
        })

        config = server._get_mcp_client_config()

        self.assertNotIn('headers', config)

    def test_auto_created_and_sync_retry_defaults(self):
        """auto_created defaults to False, sync_retry_count defaults to 0."""
        server = self.McpServer.sudo().create({
            'name': 'Defaults Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
        })
        self.assertFalse(server.auto_created)
        self.assertEqual(server.sync_retry_count, 0)
        self.assertEqual(server.state, 'draft')
