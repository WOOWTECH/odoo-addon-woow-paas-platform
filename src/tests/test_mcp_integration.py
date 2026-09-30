"""End-to-end integration tests for n8n MCP sidecar feature.

Tests the full lifecycle:
  CloudAppTemplate MCP fields -> sidecar config build ->
  auto-create MCP Server -> cron retry -> cascade cleanup.

All external calls (PaaS Operator, MCP tool discovery) are mocked.
"""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch, MagicMock, AsyncMock

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _internal_provision
from ..models.cloud_service_init_attempt import InitOutcome


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
    """Test _build_mcp_sidecar_config method on CloudService model.

    The helper used to live on PaasController but moved to the CloudService
    model (see ``models/cloud_service.py``). We exercise it through any
    cloud_service record since the method does not depend on instance state.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        # Use a CloudService record to access _build_mcp_sidecar_config.
        cls.builder = cls.env['woow_paas_platform.cloud_service']

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

        result = self.builder._build_mcp_sidecar_config(template, auth_token)

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

        result = self.builder._build_mcp_sidecar_config(template, auth_token)
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

        result = self.builder._build_mcp_sidecar_config(template, auth_token)
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

        # Should not raise; only the built-in env vars survive (no custom ones merged in).
        baseline = self.builder._build_mcp_sidecar_config(
            self._make_mcp_template(slug='baseline-no-env'), auth_token,
        )
        result = self.builder._build_mcp_sidecar_config(template, auth_token)
        self.assertEqual(
            len(result['container']['env']),
            len(baseline['container']['env']),
            'invalid JSON should add zero env vars beyond the baseline set',
        )

    def test_default_sidecar_port_when_zero(self):
        """When mcp_sidecar_port is 0/falsy, fallback to 3000."""
        template = self._make_mcp_template(mcp_sidecar_port=0)
        auth_token = 'token'

        result = self.builder._build_mcp_sidecar_config(template, auth_token)
        container = result['container']
        self.assertEqual(container['ports'], [{'containerPort': 3000}])

    def test_probes_use_correct_port(self):
        """Liveness and readiness probes point to the correct sidecar port."""
        template = self._make_mcp_template(mcp_sidecar_port=4000)
        auth_token = 'token'

        result = self.builder._build_mcp_sidecar_config(template, auth_token)
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
        """Helper to create a cloud service + its bound support project.

        Refactor post-condition (Invariant #4 / D5 — see
        ``docs/refactor/decisions/mcp-project-decouple.md``): user-scope
        ``mcp_server`` rows bind to ``project.project`` via ``project_id``
        and require a project that points back to the service. The
        production controller (``controllers/paas.py:_auto_create_mcp_server``)
        skips MCP creation when ``service.project_ids[:1]`` is empty, so
        every test in this class needs a bound project pre-created.
        """
        vals = {
            'name': 'Test Service',
            'workspace_id': self.workspace.id,
            'template_id': (template or self.mcp_template).id,
            'helm_release_name': 'test-n8n',
            'helm_namespace': 'paas-ws-1',
            'mcp_auth_token': str(uuid.uuid4()),
        }
        vals.update(overrides)
        service = _internal_provision(self.Service).create(vals)
        self.env['project.project'].sudo().create({
            'name': f'{service.name} Project',
            'cloud_service_id': service.id,
        })
        return service

    def _simulate_auto_create(self, service):
        """Simulate what _auto_create_mcp_server does, without request context.

        This replicates the controller logic so we can test it within
        TransactionCase (no HTTP request needed). Mirrors
        ``controllers/paas.py:_auto_create_mcp_server`` — same
        ``service.project_ids[:1]`` resolution + ``project_id`` field
        used for both idempotency search and ``create()``.
        """
        template = service.template_id
        if not template.mcp_enabled or not template.mcp_sidecar_image:
            return None

        # Resolved Q #3: project-anchored MCP. Production skips when no
        # support project bound — match that contract.
        project = service.project_ids[:1]
        if not project:
            return None

        McpServer = self.env['woow_paas_platform.mcp_server'].sudo()

        # Idempotency check
        existing = McpServer.search([
            ('project_id', '=', project.id),
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
            'project_id': project.id,
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
        # Refactor: link back via project chain
        # (server.project_id.cloud_service_id) — no direct M2O.
        self.assertEqual(server.project_id.cloud_service_id.id, service.id)
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

        # Only one auto-created MCP Server for this service.
        # Walk via project chain (server.project_id.cloud_service_id);
        # cloud_service.user_mcp_server_ids was removed by the refactor.
        count = self.McpServer.search_count([
            ('project_id.cloud_service_id', '=', service.id),
            ('auto_created', '=', True),
        ])
        self.assertEqual(count, 1)

    def test_no_mcp_server_for_plain_template(self):
        """Non-MCP template does NOT create an MCP Server."""
        service = self._create_service(template=self.plain_template)
        server = self._simulate_auto_create(service)

        self.assertIsNone(server)

        # Walk via project chain — see test_auto_create_idempotent for
        # rationale (cloud_service.user_mcp_server_ids removed).
        count = self.McpServer.search_count([
            ('project_id.cloud_service_id', '=', service.id),
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

    def test_mcp_server_linked_to_service_via_project_chain(self):
        """Service exposes user-scope MCP via project_ids.mcp_server_ids.

        Refactor: ``cloud_service.user_mcp_server_ids`` was removed in
        favour of walking through ``service.project_ids.mcp_server_ids``
        (the new inverse on ``project.project``). Lock the new chain.
        """
        service = self._create_service()
        server = self._simulate_auto_create(service)

        self.assertIn(server.id, service.project_ids.mcp_server_ids.ids)


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
        """Helper to create an auto-created MCP Server in given state.

        Post-refactor: user-scope ``mcp_server`` rows require a
        ``project_id`` (D4 invariant — scope=user iff project_id NOT
        NULL). Seed a bound project per call so each test gets a fresh
        service + project + MCP triplet.
        """
        service = _internal_provision(self.Service).create({
            'name': 'Cron Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'helm_release_name': 'cron-n8n',
            'helm_namespace': 'paas-ws-cron',
            'mcp_auth_token': str(uuid.uuid4()),
        })
        project = self.env['project.project'].sudo().create({
            'name': f'{service.name} Project',
            'cloud_service_id': service.id,
        })
        vals = {
            'name': f'{service.name} MCP',
            'url': 'http://cron-n8n-mcp.paas-ws-cron.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': project.id,
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
        """After MAX_SYNC_RETRIES (10) failed retries, server state becomes 'error'."""
        server = self._create_auto_mcp_server(state='draft', retry_count=10)

        self.McpServer._cron_retry_mcp_sync()

        server.invalidate_recordset()
        self.assertEqual(server.state, 'error')
        self.assertIn('failed after 10 attempts', server.state_message)
        # sync should NOT have been called for this server (already exceeded)
        mock_sync.assert_not_called()

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer.action_sync_tools_safe')
    def test_cron_does_not_touch_manual_servers(self, mock_sync):
        """Manual (non-auto_created) MCP Servers are NOT affected by cron."""
        service = _internal_provision(self.Service).create({
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
        """Servers with retry_count < MAX (10) are retried; at MAX they get error."""
        mock_sync.return_value = False

        server_ok = self._create_auto_mcp_server(state='draft', retry_count=9)
        server_max = self._create_auto_mcp_server(state='draft', retry_count=10)

        self.McpServer._cron_retry_mcp_sync()

        # server_ok (retry_count=9) should have sync called
        # server_max (retry_count=10) should be set to error, no sync
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
        # Post-refactor: scope='user' rows require project_id (D4
        # invariant — Python constraint @_check_scope_project_consistency
        # at mcp_server.py:116). Seed a service + bound project once at
        # class level so each sync_safe test can reuse the same project.
        Workspace = cls.env['woow_paas_platform.workspace']
        Template = cls.env['woow_paas_platform.cloud_app_template']
        Service = cls.env['woow_paas_platform.cloud_service']
        Project = cls.env['project.project']
        cls.workspace = Workspace.create({'name': 'Sync Safe WS'})
        cls.template = Template.create({
            'name': 'Tmpl Sync Safe',
            'slug': 'tmpl-sync-safe',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-sync-safe',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        cls.service = _internal_provision(Service).create({
            'name': 'Sync Safe Service',
            'workspace_id': cls.workspace.id,
            'template_id': cls.template.id,
        })
        cls.project = Project.sudo().create({
            'name': 'Sync Safe Project',
            'cloud_service_id': cls.service.id,
        })
        # #554：把 McpServer 的 `_constraint_methods` cache 先烘熱。
        #
        # 它是 memoized property（`models.py`）——第一次被讀時用 `getmembers`
        # 掃整個 class 找帶 `_constrains` 屬性的 method，算完寫回 cls。若那一刻
        # 正好落在 `@patch(..._async_sync_tools)` 生效期間，掃描會碰到 AsyncMock，
        # 而 MagicMock 對任何屬性名都會自動生出一個 mock ⇒ `_constrains` 存在
        # ⇒ 被誤判成 constraint method ⇒ `api.constrains(*func._constrains(self))`
        # 拿到 coroutine ⇒ `TypeError: argument after * must be an iterable`。
        #
        # 本 class 的三條測試原本都在 patch 內才第一次 create McpServer，所以
        # 三條全部 ERROR ——「#554 要改的那個契約，它的既有守線從來沒執行過」。
        # 在 patch 之外先建一筆把 cache 烘熱，每條測試才量得到自己要守的行為。
        cls.McpServer.sudo().create({
            'name': 'Sync Safe Cache Warmer',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': cls.project.id,
        })

    @patch('odoo.addons.woow_paas_platform.models.mcp_server.McpServer._async_sync_tools')
    def test_sync_safe_success(self, mock_async):
        """Successful sync sets state to connected and resets retry count."""
        mock_async.return_value = True

        server = self.McpServer.sudo().create({
            'name': 'Safe Sync Test',
            'url': 'http://localhost:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project.id,
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
            'project_id': self.project.id,
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
    """Lifecycle: deleting CloudService cleans up auto-created MCP Server.

    Post-refactor mechanism (no longer FK cascade): user-scope
    ``mcp_server.project_id`` is ``ondelete='cascade'`` to
    ``project.project`` but **not** to ``cloud_service``. The
    cloud-service ↔ project link uses ``ondelete='set null'``
    (see ``docs/refactor/decisions/mcp-project-decouple.md`` Invariant
    #2). Cleanup therefore happens via ``cloud_service.unlink()``
    override (BLOCKER 2 fix) + project-direct cascade for the project
    deletion path. Tests below exercise both routes.
    """

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

    def test_delete_service_cleans_user_mcp(self):
        """Direct ``service.unlink()`` removes user-scope MCP rows.

        Post-refactor: no FK cascade from cloud_service to mcp_server
        (mcp_server.project_id ondelete=cascade hits project, not
        service). The cleanup is done by ``cloud_service.unlink()``
        override — see BLOCKER 2 regression test below for the
        explicit override-vs-cascade lock.
        """
        service = _internal_provision(self.Service).create({
            'name': 'To Delete Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'mcp_auth_token': str(uuid.uuid4()),
        })
        project = self.env['project.project'].sudo().create({
            'name': f'{service.name} Project',
            'cloud_service_id': service.id,
        })
        # AC #24 regression lock (2026-05-20 mcp-refactor): cleanup filter
        # is `lambda s: s.scope == 'user' and s.sidecar_backed`; feed the
        # new flag when the field exists so this PR #47 BLOCKER 2 line
        # stays green post-refactor.
        server_vals = {
            'name': f'{service.name} MCP',
            'url': 'http://test-mcp.ns.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': project.id,
            'auto_created': True,
        }
        if 'sidecar_backed' in self.McpServer._fields:
            server_vals['sidecar_backed'] = True
        server = self.McpServer.sudo().create(server_vals)
        server_id = server.id

        service.unlink()

        # MCP Server is removed via cloud_service.unlink() override
        # (cloud_service.py:1176-1201). No FK cascade is involved on
        # the cloud_service ↔ mcp_server path.
        self.assertFalse(self.McpServer.sudo().browse(server_id).exists())

    def test_delete_service_also_deletes_mcp_tools(self):
        """Service unlink → MCP Server (via override) → Tools (FK cascade).

        Chain:
        - ``cloud_service.unlink()`` override removes user-scope MCP.
        - ``mcp_tool.server_id`` is ondelete='cascade' on ``mcp_server``,
          so tools follow automatically.
        """
        service = _internal_provision(self.Service).create({
            'name': 'Tool Cascade Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'mcp_auth_token': str(uuid.uuid4()),
        })
        project = self.env['project.project'].sudo().create({
            'name': f'{service.name} Project',
            'cloud_service_id': service.id,
        })
        # AC #24 regression lock (2026-05-20 mcp-refactor) — same rationale
        # as test_delete_service_cleans_user_mcp above.
        server_vals = {
            'name': f'{service.name} MCP',
            'url': 'http://test-mcp.ns.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': project.id,
            'auto_created': True,
        }
        if 'sidecar_backed' in self.McpServer._fields:
            server_vals['sidecar_backed'] = True
        server = self.McpServer.sudo().create(server_vals)

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

    def test_delete_workspace_preserves_mcp_via_orphaned_project(self):
        """Workspace delete → Service deleted; project + MCP survive.

        Post-refactor semantics (Invariant #2 — cloud_service ↔ project
        ondelete='set null', not cascade):

        * workspace.unlink() cascades to cloud_service via
          ``cloud_service.workspace_id`` ondelete='cascade'.
        * Cascade happens at PostgreSQL FK level — the Python
          ``cloud_service.unlink()`` override does NOT fire on the
          cascade path (DB cascade bypasses Python ORM hooks).
        * ``project.cloud_service_id`` ondelete='set null' so the
          project survives as an orphan (cloud_service_id = NULL).
        * The user-scope MCP's ``project_id`` still points at the
          surviving project, so the MCP also survives.

        This is the **intended** new behaviour: projects + their MCP
        servers are independent of any single service's lifecycle
        (D6 ruling — see ``docs/refactor/decisions/mcp-project-decouple.md``).
        The orphan project can be re-bound to a new service later
        without losing its MCP tools / chat history.

        Pre-refactor expectation (workspace.unlink → MCP gone via
        direct FK cascade on mcp_server.cloud_service_id) no longer
        applies because that direct FK was removed.
        """
        workspace = self.Workspace.create({'name': 'Workspace Survive WS'})
        service = _internal_provision(self.Service).create({
            'name': 'Survive Service',
            'workspace_id': workspace.id,
            'template_id': self.mcp_template.id,
            'mcp_auth_token': str(uuid.uuid4()),
        })
        project = self.env['project.project'].sudo().create({
            'name': f'{service.name} Project',
            'cloud_service_id': service.id,
        })
        server = self.McpServer.sudo().create({
            'name': f'{service.name} MCP',
            'url': 'http://test-mcp.ns.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': project.id,
            'auto_created': True,
        })
        service_id = service.id
        project_id = project.id
        server_id = server.id

        workspace.unlink()

        # Service is gone (FK cascade from workspace).
        self.assertFalse(
            self.Service.browse(service_id).exists(),
            "Workspace cascade should delete the service (cloud_service."
            "workspace_id ondelete='cascade').",
        )
        # Project survives — its cloud_service_id was set to NULL by
        # FK on cascade.
        surviving_project = self.env['project.project'].sudo().browse(project_id)
        self.assertTrue(
            surviving_project.exists(),
            "Invariant #2 regression: project must survive its bound "
            "service deletion (cloud_service_id ondelete='set null'). "
            "If this fails, ondelete was accidentally changed to "
            "'cascade' — D6 decision violated.",
        )
        self.assertFalse(
            surviving_project.cloud_service_id,
            "Invariant #2 regression: surviving project's "
            "cloud_service_id must be NULL after the bound service is "
            "cascaded away by workspace deletion.",
        )
        # MCP survives because its project_id is still valid.
        self.assertTrue(
            self.McpServer.sudo().browse(server_id).exists(),
            "Refactor regression: user-scope MCP should survive a "
            "workspace cascade because its project (parent) survives. "
            "If this fails, mcp_server.project_id ondelete may have "
            "been accidentally tied to the service chain.",
        )

    # ------------------------------------------------------------------ #
    # BLOCKER 2 regression — reviewer (2026-05-18, NEEDS REVISION)        #
    #                                                                    #
    # Reviewer surfaced that the round-2/round-3 suites covered the      #
    # `_do_cleanup` path but never explicitly locked the                 #
    # `cloud_service.unlink()` override path that catches direct         #
    # unlink() calls (controllers, cron, ORM ops) bypassing the          #
    # lifecycle helper. Implementation added the override in             #
    # cloud_service.py:1176-1201 covering 4 unlink entry points          #
    # (3 reviewer-surfaced + 1 sweep-found at _poll_and_update_status    #
    # line 787). This regression locks the override behaviour            #
    # **without** going through _do_cleanup — distinct from              #
    # test_delete_service_cleans_user_mcp above only in intent (this     #
    # test would still pass if the override were removed and _do_cleanup #
    # were the only path; that scenario would be the legitimate          #
    # behaviour to catch here).                                          #
    # ------------------------------------------------------------------ #
    def test_blocker2_unlink_override_cleans_user_mcp_without_do_cleanup(self):
        """BLOCKER 2: direct ``service.unlink()`` cleans user-scope MCP.

        Locks the override-vs-cascade invariant: even when no
        ``_do_cleanup`` precedes the unlink (e.g. controller deleting
        a stuck service after its own helm uninstall, cron compaction,
        a manual ORM op), the user-scope MCP rows that point at the
        service's project_ids MUST be removed in the same transaction.

        A zombie row (user-scope MCP whose backing sidecar is gone)
        violates D5 / Invariant #5 and produces silent AI-chat tool
        failures.
        """
        service = _internal_provision(self.Service).create({
            'name': 'Direct Unlink Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'helm_release_name': 'direct-unlink',
            'helm_namespace': 'paas-ws-du',
            'mcp_auth_token': str(uuid.uuid4()),
        })
        project = self.env['project.project'].sudo().create({
            'name': f'{service.name} Project',
            'cloud_service_id': service.id,
        })
        # AC #24 (2026-05-20 mcp-refactor) regression lock: also feed
        # ``sidecar_backed=True`` when the new field exists so the
        # post-refactor cleanup filter ``lambda s: s.scope == 'user' and
        # s.sidecar_backed`` matches this row. Pre-impl the field is
        # absent → branch skipped.
        mcp_vals = {
            'name': f'{service.name} MCP',
            'url': 'http://direct-unlink-mcp.paas-ws-du.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': project.id,
            'auto_created': True,
        }
        if 'sidecar_backed' in self.McpServer._fields:
            mcp_vals['sidecar_backed'] = True
        user_mcp = self.McpServer.sudo().create(mcp_vals)
        mcp_id = user_mcp.id

        # Note: we deliberately do NOT call service._do_cleanup() here.
        # The override at cloud_service.unlink() must do the cleanup on
        # its own — this is the entire point of the regression lock.
        service.unlink()

        self.assertFalse(
            self.McpServer.sudo().browse(mcp_id).exists(),
            "BLOCKER 2 regression: user-scope MCP must be removed by "
            "cloud_service.unlink() override even when _do_cleanup is "
            "not invoked first. See cloud_service.py:1176-1201.",
        )

    def test_blocker2_unlink_override_skips_system_mcp(self):
        """BLOCKER 2 boundary: system-scope MCP is NOT touched by unlink.

        The override filters on ``scope == 'user'`` (D5 / Invariant
        #1 — system-scope MCP rows are not bound to any service and
        must survive service deletion). Without this filter, deleting
        a service could blow away platform-wide system MCP and break
        every other workspace's AI assistant.
        """
        service = _internal_provision(self.Service).create({
            'name': 'System Safe Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'helm_release_name': 'sys-safe',
            'helm_namespace': 'paas-ws-ss',
            'mcp_auth_token': str(uuid.uuid4()),
        })
        # System-scope MCP — no project_id (Invariant #1).
        system_mcp = self.McpServer.sudo().create({
            'name': 'Platform System MCP',
            'url': 'http://platform.mcp/sse',
            'transport': 'sse',
            'scope': 'system',
        })
        sys_id = system_mcp.id

        service.unlink()

        self.assertTrue(
            self.McpServer.sudo().browse(sys_id).exists(),
            "BLOCKER 2 boundary: system-scope MCP must survive a "
            "service unlink — only scope='user' rows are tied to the "
            "service via project chain. See cloud_service.py:1192 "
            "(`.filtered(lambda s: s.scope == 'user')`).",
        )

    # ------------------------------------------------------------------ #
    # MAJOR 2 regression — reviewer (2026-05-18, NEEDS REVISION)         #
    #                                                                    #
    # Reviewer surfaced that no test locked the new contract from        #
    # cloud_service.py:1162-1171: when the helm uninstall step inside    #
    # _do_cleanup raises, service.state must be set to 'error' (mirror   #
    # of _do_suspend / _do_resume fail handling) — NOT left at the       #
    # pre-call state, NOT advanced to 'deleting'. Half-success state     #
    # is also a contract: user-scope MCP was unlinked BEFORE the helm    #
    # attempt (D1 / Invariant #4 ordering), so on helm fail we end up    #
    # with MCP-gone + state=error.                                       #
    # ------------------------------------------------------------------ #
    def test_major2_do_cleanup_helm_fail_sets_error_state(self):
        """MAJOR 2: ``_do_cleanup`` helm uninstall fail → state='error'.

        Mocks ``get_paas_operator_client`` to return a client whose
        ``uninstall_release`` raises. Asserts the post-fail state
        machine: state='error', error_message populated, NOT advanced
        to 'deleting'. Also confirms user-scope MCP was already
        unlinked before the helm attempt (D1 ordering).
        """
        service = _internal_provision(self.Service).create({
            'name': 'Helm Fail Service',
            'workspace_id': self.workspace.id,
            'template_id': self.mcp_template.id,
            'helm_release_name': 'helm-fail',
            'helm_namespace': 'paas-ws-hf',
            'mcp_auth_token': str(uuid.uuid4()),
            'state': 'running',
        })
        project = self.env['project.project'].sudo().create({
            'name': f'{service.name} Project',
            'cloud_service_id': service.id,
        })
        # AC #24 regression lock (2026-05-20 mcp-refactor) — same rationale
        # as test_delete_service_cleans_user_mcp above.
        user_mcp_vals = {
            'name': f'{service.name} MCP',
            'url': 'http://helm-fail-mcp.paas-ws-hf.svc.cluster.local:3000/mcp',
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': project.id,
            'auto_created': True,
        }
        if 'sidecar_backed' in self.McpServer._fields:
            user_mcp_vals['sidecar_backed'] = True
        user_mcp = self.McpServer.sudo().create(user_mcp_vals)
        mcp_id = user_mcp.id

        # Patch the local import inside _do_cleanup to return a
        # client whose uninstall_release raises a generic Exception.
        # The path is module-relative (
        # ``from ..services.paas_operator import get_paas_operator_client``)
        # so we patch the symbol at its source module.
        mock_client = MagicMock()
        mock_client.uninstall_release.side_effect = RuntimeError(
            'helm broken in test'
        )
        patch_target = (
            'odoo.addons.woow_paas_platform.services.paas_operator.'
            'get_paas_operator_client'
        )
        with patch(patch_target, return_value=mock_client):
            service._do_cleanup()

        service.invalidate_recordset()
        self.assertEqual(
            service.state, 'error',
            "MAJOR 2 regression: helm uninstall fail must set "
            "state='error' (mirrors _do_suspend / _do_resume). See "
            "cloud_service.py:1162-1171.",
        )
        self.assertNotEqual(
            service.state, 'deleting',
            "MAJOR 2 regression: helm uninstall fail must NOT advance "
            "the service to state='deleting'; that's reserved for the "
            "success path at cloud_service.py:1174.",
        )
        self.assertIn(
            'helm broken in test', service.error_message or '',
            "MAJOR 2 regression: error_message must surface the helm "
            "exception so operators can diagnose without re-reading "
            "logs.",
        )
        # Half-success: user-scope MCP was already unlinked at
        # cloud_service.py:1139-1147 before the helm attempt (D1 /
        # Invariant #4 — no zombie rows even when cleanup half-fails).
        self.assertFalse(
            self.McpServer.sudo().browse(mcp_id).exists(),
            "MAJOR 2 boundary (D1 / Invariant #4): user-scope MCP "
            "must be unlinked BEFORE the helm uninstall attempt, so "
            "even a half-success cleanup leaves no zombie rows "
            "pointing at a stale sidecar URL.",
        )


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


# ====================================================================== #
# TDD red phase — mcp_server scope/project_id consistency constraints     #
# (Plan §Constraint AC #6 & #7, Decision §Invariants 2 & 3)               #
#                                                                         #
# Today (pre-implementation):                                             #
#   * ``mcp_server.project_id`` does not exist.                           #
#   * No ``@api.constrains`` on ``scope`` / ``project_id``.               #
#                                                                         #
# Red expectation:                                                        #
#   1. Field guard ``self.fail()`` → AssertionError until ``project_id``  #
#      is added (AC #1).                                                  #
#   2. Once the field exists but the constraint is missing, ``create``    #
#      with violating values does NOT raise → ``assertRaises`` fails →    #
#      AssertionError.                                                    #
# ====================================================================== #


@tagged('post_install', '-at_install')
class TestMcpProjectScopeConstraints(TransactionCase):
    """AC #6 / #7 — D4 & D5 scope/project consistency constraints."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

    def setUp(self):
        super().setUp()
        if 'project_id' not in self.McpServer._fields:
            self.fail(
                "mcp_server.project_id field missing — constraint tests "
                "rely on the new schema (Plan AC #1). Implement schema "
                "before constraints."
            )

        self.workspace = self.Workspace.sudo().create({
            'name': 'WS Constraint MCP',
        })
        self.template = self.Template.sudo().create({
            'name': 'Tmpl Constraint MCP',
            'slug': 'tmpl-constraint-mcp',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl-c-mcp',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.bound_service = _internal_provision(self.Service.sudo()).create({
            'name': 'Constraint Bound Svc',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'constraint-svc',
            'helm_namespace': 'paas-ws-c',
        })
        self.project_bound = self.Project.sudo().create({
            'name': 'Constraint Bound Project',
            'cloud_service_id': self.bound_service.id,
        })
        self.project_unbound = self.Project.sudo().create({
            'name': 'Constraint Unbound Project',
        })

    # ------------------------------------------------------------------ #
    # AC #6 / D4 invariant — scope='user' requires project_id NOT NULL    #
    # ------------------------------------------------------------------ #
    def test_ac6_constraint_user_scope_requires_project(self):
        """AC #6 / D4: scope='user' with project_id NULL must raise ValidationError.

        The Python ``@api.constrains('scope', 'project_id')`` must
        enforce ``scope='user' ⇔ project_id IS NOT NULL`` (decision §3).
        Without the constraint, ORM accepts the inconsistent row → the
        whole D4 invariant breaks (AI chat would see a user-scope MCP
        with no project to anchor its workspace).
        """
        # ValidationError is the canonical Odoo constraint failure.
        # We import lazily to avoid import-time coupling on stub envs.
        from odoo.exceptions import ValidationError

        with self.assertRaises(
            ValidationError,
            msg=(
                "AC #6 / D4: creating scope='user' with project_id=False "
                "did not raise ValidationError. Add "
                "_check_scope_project_consistency to mcp_server."
            ),
        ):
            self.McpServer.sudo().create({
                'name': 'Bad User No Project',
                'url': 'http://x/mcp',
                'transport': 'streamable_http',
                'scope': 'user',
                'project_id': False,
            })

    # ------------------------------------------------------------------ #
    # AC #6 / D4 invariant — scope='system' requires project_id IS NULL   #
    # ------------------------------------------------------------------ #
    def test_ac6_constraint_system_scope_excludes_project(self):
        """AC #6 / D4: scope='system' with project_id set must raise.

        The bi-directional invariant: scope='system' implies
        project_id IS NULL.  Without enforcement, an operator setting
        a system MCP on a project would create a row that breaks the
        D5 reasoning (system MCP doesn't belong to a project — it's
        platform-wide).
        """
        from odoo.exceptions import ValidationError

        with self.assertRaises(
            ValidationError,
            msg=(
                "AC #6 / D4: creating scope='system' with project_id set "
                "did not raise ValidationError. The constraint must check "
                "BOTH directions (scope='user' ⇔ project_id NOT NULL)."
            ),
        ):
            self.McpServer.sudo().create({
                'name': 'Bad System With Project',
                'url': 'http://sys/mcp',
                'transport': 'streamable_http',
                'scope': 'system',
                'project_id': self.project_bound.id,
            })



def _apply_init_success(service, api_key=None):
    """#1257：以一筆持有 lease 的 attempt 驅動 init 成功的套用（不 commit）。"""
    now = fields.Datetime.now()
    attempt = service.env['woow_paas_platform.cloud_service_init_attempt'].sudo().create({
        'service_id': service.id, 'generation': service.init_generation,
        'sequence': 1, 'init_type': service._init_type(),
        'claimed_at': now, 'lease_until': now + timedelta(minutes=10),
    })
    service.sudo().write({
        'state': 'initializing', 'init_attempt_id': attempt.id,
        'init_lease_until': now + timedelta(minutes=10),
    })
    applied = service._init_apply_outcome(
        attempt, InitOutcome('success', api_key=api_key), False)
    assert applied, '前提：CAS 必須成立，否則下面的斷言零鑑別力'
    assert service.state == 'running'


# ====================================================================== #
# TDD red phase — Group C: auto-create teardown verification              #
#                                                                         #
# Plan: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md   #
#   AC #7:  cloud_service.py:_model_auto_create_mcp_server 整段刪除      #
#   AC #8:  cloud_service.py 內 3 個 call sites 全部刪除                  #
#             - line 849 (_poll_and_update_status → state='running')      #
#             - line 914 (n8n init 成功後；#1257 起在 _init_apply_outcome) #
#             - line 940 (Odoo init 成功後；同上)                         #
#   AC #9:  controllers/paas.py:_auto_create_mcp_server 整段刪除          #
#   AC #10: controllers/paas.py 內 3 個 call sites 全部刪除               #
#   AC #11: cloud service 轉 running **不會** 產生新的 mcp_server row     #
#   AC #12: n8n template 初始化成功 **不會** 產生新的 mcp_server row      #
#                                                                         #
# Red expectation today (pre-implementation, 2026-05-20):                 #
#   * test_service_state_transition_running_does_not_create_mcp → RED:    #
#     today line 849 still calls service._model_auto_create_mcp_server(); #
#     post-impl call site gone → no MCP row appears.                      #
#   * test_n8n_init_success_does_not_create_mcp → RED: line 914 same.     #
#   * test_odoo_init_success_does_not_create_mcp → RED: line 940 same.    #
#   * test_old_auto_create_methods_removed → RED: source still defines    #
#     `_model_auto_create_mcp_server` (cloud_service.py:970) and          #
#     `_auto_create_mcp_server` (paas.py:2601).                           #
#                                                                         #
# Boundary: only exercises the **model** call paths (line 849 / 914 /     #
# 940). Controller paths (paas.py:2395 / 2493 / 2560) are covered by      #
# AC #10 grep test (the entire `_auto_create_mcp_server` method gone).    #
# ====================================================================== #


@tagged('post_install', '-at_install')
class TestAutoCreateTeardown(TransactionCase):
    """C group — AC #7-12, auto-create code path physically removed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']

    def setUp(self):
        super().setUp()
        self.workspace = self.Workspace.sudo().create({
            'name': 'WS AutoCreateTeardown',
        })
        # n8n-style template — post_deploy_init_type='n8n' so init path
        # exercises the line-914 call site. mcp_enabled=True so any
        # legacy auto-create would fire.
        self.template_n8n = self.Template.sudo().create({
            'name': 'AC-Tear n8n',
            'slug': f'act-n8n-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'act-n8n',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/test/mcp:latest',
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
            'post_deploy_init_type': 'n8n',
        })
        # Odoo-style template — post_deploy_init_type='odoo' for line 940.
        self.template_odoo = self.Template.sudo().create({
            'name': 'AC-Tear Odoo',
            'slug': f'act-odoo-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'act-odoo',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/test/mcp:latest',
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
            'post_deploy_init_type': 'odoo',
        })
        # Plain mcp-enabled template (no init) — for the state='running'
        # transition path (line 849).
        self.template_plain = self.Template.sudo().create({
            'name': 'AC-Tear Plain MCP',
            'slug': f'act-plain-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'act-plain',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
            'mcp_sidecar_image': 'ghcr.io/test/mcp:latest',
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
            'post_deploy_init_type': 'none',
        })

    def _make_service_with_project(self, template, name_prefix, release_name):
        service = _internal_provision(self.Service.sudo()).create({
            'name': f'{name_prefix} Service',
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'helm_release_name': release_name,
            'helm_namespace': 'paas-ws-act',
            'mcp_auth_token': str(uuid.uuid4()),
            'state': 'deploying',
        })
        self.Project.sudo().create({
            'name': f'{service.name} Project',
            'cloud_service_id': service.id,
        })
        return service

    def _count_mcp_for_service(self, service):
        return self.McpServer.sudo().with_context(active_test=False).search_count([
            ('project_id.cloud_service_id', '=', service.id),
        ])

    # ------------------------------------------------------------------ #
    # AC #11 (and #7 / #8 by extension): state='running' → no MCP create  #
    # ------------------------------------------------------------------ #
    def test_service_state_transition_running_does_not_create_mcp(self):
        """AC #7 / #8 / #11: cloud_service.py:849 auto-create call site
        removed — state='running' transition leaves mcp_server_ids empty.

        Drives the production code path ``_poll_and_update_status`` with
        a mocked client returning a fully-ready 'deployed' release for a
        no-init template; per AC #11 the post-condition is **no
        mcp_server row** for the service.

        **Red today**：line 849 calls
        ``service._model_auto_create_mcp_server()`` which creates the
        row → count==1 → assertion fails.
        """
        service = self._make_service_with_project(
            self.template_plain, 'C1 Plain', 'c1-plain-svc',
        )
        # Before — no MCP yet.
        self.assertEqual(
            self._count_mcp_for_service(service), 0,
            "Fixture sanity: no MCP should exist before the transition.",
        )

        fake_client = MagicMock()
        fake_client.get_status.return_value = {
            'release': {'status': 'deployed', 'revision': 1},
            'pods': [{'phase': 'Running', 'ready': '1/1'}],
        }

        service._poll_and_update_status(fake_client)

        # Post — state moved to running.
        service.invalidate_recordset()
        self.assertEqual(
            service.state, 'running',
            "Fixture sanity: transition didn't reach 'running'; mock "
            "client may not match the production status shape.",
        )
        self.assertEqual(
            self._count_mcp_for_service(service), 0,
            "AC #11: state='running' transition CREATED a new mcp_server "
            "row. cloud_service.py:849 auto-create call site must be "
            "removed (AC #8 third bullet). Per AC #7 the entire "
            "_model_auto_create_mcp_server method goes too.",
        )

    # ------------------------------------------------------------------ #
    # AC #12 (and #8 / #10): n8n init success → no MCP create             #
    # ------------------------------------------------------------------ #
    def test_n8n_init_success_does_not_create_mcp(self):
        """AC #8 (line 914) / #12: n8n post-deploy init success path
        must NOT auto-create MCP row.

        #1257 起成功路徑的服務端寫入收斂在 ``_init_apply_outcome``（單一 init
        入口 ``_run_post_deploy_init`` 的 CAS 那一步；入口本身會 commit，所以這裡
        直接驅動套用那一步）。

        **Red today**：line 914 fires → count==1.
        """
        service = self._make_service_with_project(
            self.template_n8n, 'C2 n8n', 'c2-n8n-svc',
        )
        self.assertEqual(self._count_mcp_for_service(service), 0)

        _apply_init_success(service, api_key='fake-n8n-key-c2')

        self.assertEqual(
            self._count_mcp_for_service(service), 0,
            "AC #12: n8n init success created a new mcp_server row. "
            "cloud_service.py:914 auto-create call site must be removed "
            "(AC #8 fourth bullet).",
        )

    # ------------------------------------------------------------------ #
    # AC #12 / #8: odoo init success → no MCP create                      #
    # ------------------------------------------------------------------ #
    def test_odoo_init_success_does_not_create_mcp(self):
        """AC #8 (line 940) / #12: Odoo post-deploy init success path
        must NOT auto-create MCP row.

        Same pattern as n8n test (success applied through ``_init_apply_outcome``).

        **Red today**：line 940 fires → count==1.
        """
        service = self._make_service_with_project(
            self.template_odoo, 'C3 Odoo', 'c3-odoo-svc',
        )
        self.assertEqual(self._count_mcp_for_service(service), 0)

        _apply_init_success(service)

        self.assertEqual(
            self._count_mcp_for_service(service), 0,
            "AC #12 / AC #8: Odoo init success created a new mcp_server "
            "row. cloud_service.py:940 auto-create call site must be "
            "removed (AC #8 fifth bullet).",
        )

    # ------------------------------------------------------------------ #
    # AC #7 / #9: both auto-create methods physically gone from source    #
    # ------------------------------------------------------------------ #
    def test_old_auto_create_methods_removed_from_source(self):
        """AC #7 / AC #9: ``_model_auto_create_mcp_server`` (cloud_service.py)
        and ``_auto_create_mcp_server`` (paas.py) physically deleted.

        Grep both source files (more robust than ``hasattr`` reflection —
        method-name-still-defined-but-stubbed-noop wouldn't be caught by
        attribute checks; source-string match catches that too). Pattern
        mirrors A group ``test_d5_constraint_method_no_longer_exists_in_
        mcp_server_source``.

        **Red today**：both methods are still defined:
          - ``src/models/cloud_service.py:970``
            ``def _model_auto_create_mcp_server(self):``
          - ``src/controllers/paas.py:2601``
            ``def _auto_create_mcp_server(self, ...):``

        Substring matches return True → assertNotIn fails.
        """
        import inspect, os
        from odoo.addons.woow_paas_platform.models import cloud_service as cs_module
        from odoo.addons.woow_paas_platform.controllers import paas as paas_module

        # cloud_service.py — model-context auto-create.
        cs_path = inspect.getsourcefile(cs_module)
        self.assertTrue(
            cs_path and os.path.exists(cs_path),
            f"Test sanity: cannot locate cloud_service.py source "
            f"(got {cs_path!r}).",
        )
        with open(cs_path, encoding='utf-8') as fh:
            cs_source = fh.read()
        self.assertNotIn(
            'def _model_auto_create_mcp_server',
            cs_source,
            "AC #7: cloud_service.py still defines "
            "_model_auto_create_mcp_server — refactor must remove "
            "the entire method (Plan AC #7). Grep "
            "`src/models/cloud_service.py` for the @method block at "
            "line 970-1013.",
        )

        # paas.py — controller-context auto-create.
        paas_path = inspect.getsourcefile(paas_module)
        self.assertTrue(
            paas_path and os.path.exists(paas_path),
            f"Test sanity: cannot locate paas.py source (got {paas_path!r}).",
        )
        with open(paas_path, encoding='utf-8') as fh:
            paas_source = fh.read()
        self.assertNotIn(
            'def _auto_create_mcp_server',
            paas_source,
            "AC #9: controllers/paas.py still defines "
            "_auto_create_mcp_server — refactor must remove the entire "
            "method (Plan AC #9). Grep `src/controllers/paas.py` for "
            "the method body at line 2601-2660.",
        )
