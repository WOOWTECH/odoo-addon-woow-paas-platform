"""LLM-02 AC5：MCP URL 不能是 new-api／newapi-proxy 的跳板（DESIGN §5.2 T3，〔REV〕13）。

判斷本身（``services/mcp_url_guard``）的表格測試在 ``tests/test_llm_services.py``；這裡驗它接上的三個地方：
model constraint、``/api/support-projects/<id>/mcp-servers`` 端點（工作區 owner 與 admin 成員都拿到同一句友善
訊息——two-layer lockstep）、連線時的再查（DNS rebinding）與不跟跨主機 redirect。DNS 一律換成固定的表
（``mcp_url_guard.resolve``），不碰真的叢集 DNS。
"""
import asyncio
import json
import uuid
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import HttpCase, new_test_user, tagged

from ..models import cloud_service as cloud_service_module
from ..services import mcp_url_guard


# ====================================================================== #
#  MCP 跳板（DESIGN §5.2 T3；AC5）                                          #
# ====================================================================== #
PROXY_CLUSTER_IP = '10.43.88.10'
NEWAPI_CLUSTER_IP = '10.43.88.11'
PROXY_POD_IP = '10.42.3.77'
SIDECAR_IP = '10.43.12.5'


def _resolver(extra=None):
    table = {
        'newapi-proxy.new-api-stg.svc.cluster.local': {PROXY_CLUSTER_IP},
        'newapi.new-api-stg.svc.cluster.local': {NEWAPI_CLUSTER_IP},
        'newapi-proxy.new-api.svc.cluster.local': {'10.43.99.10'},
        'newapi.new-api.svc.cluster.local': {'10.43.99.11'},
        'sneaky.example.com': {PROXY_CLUSTER_IP},
        'other-cluster.example.com': {'10.43.200.1'},
        'demo-mcp.paas-ws-abc.svc.cluster.local': {SIDECAR_IP},
        'mcp.example.com': {'203.0.113.7'},
    }
    table.update(extra or {})
    return lambda host: set(table.get(host, ()))


#: AC5 的清單（「會 302 到代理的 URL」在連線時擋，見 test_redirect_to_another_host_is_an_error）。
REJECTED_URLS = (
    'http://newapi-proxy.new-api-stg.svc.cluster.local:8080/sse',
    'http://%s:8080/sse' % PROXY_CLUSTER_IP,
    'http://%s:8080/sse' % PROXY_POD_IP,
    'http://newapi-proxy.new-api-stg:8080/sse',
    'http://newapi-proxy.new-api-stg.svc.cluster.local.:8080/sse',
    'http://NEWAPI-PROXY.New-Api-Stg.svc.cluster.local:8080/sse',
    'http://sneaky.example.com/sse',
    'http://other-cluster.example.com/sse',
    'http://[::ffff:%s]:8080/sse' % PROXY_CLUSTER_IP,
    'http://newapi.new-api.svc.cluster.local:3000/sse',
)
ALLOWED_URLS = (
    'http://demo-mcp.paas-ws-abc.svc.cluster.local:3001/mcp',
    'https://mcp.example.com/sse',
    'http://unresolvable.example.invalid/sse',
)


@tagged('post_install', '-at_install', 'llm_client_hardening')
class TestLlmMcpJumpGuard(HttpCase):

    def setUp(self):
        super().setUp()
        resolve = patch.object(mcp_url_guard, 'resolve', _resolver())
        resolve.start()
        self.addCleanup(resolve.stop)
        mcp_url_guard._protected_cache.update(until=0.0)
        self.addCleanup(mcp_url_guard._protected_cache.update, until=0.0)
        self.McpServer = self.env['woow_paas_platform.mcp_server'].sudo()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        suffix = uuid.uuid4().hex[:8]
        # /woow 的租戶使用者（工作區 owner 與 admin 成員）。Odoo 的 portal（share）帳號一律不是
        # 工作區成員（workspace.check_user_access，#1261），所以這裡是內部帳號、只有工作區角色。
        tenant = 'base.group_user'
        self.owner = new_test_user(self.env, login='mcpjump_owner_%s' % suffix,
                                   password='mcpjump-owner-%s' % suffix, groups=tenant,
                                   context={'no_reset_password': True})
        self.admin = new_test_user(self.env, login='mcpjump_admin_%s' % suffix,
                                   password='mcpjump-admin-%s' % suffix, groups=tenant,
                                   context={'no_reset_password': True})
        self.ws = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'MCP jump %s' % suffix, 'owner_id': self.owner.id})
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.ws.id, 'user_id': self.admin.id, 'role': 'admin'})
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'MCP jump tmpl', 'slug': 'mcpjump-%s' % suffix,
            'helm_repo_url': 'https://charts.example.com', 'helm_chart_name': 'mcpjump',
            'helm_chart_version': '1.0.0', 'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1})
        service = cloud_service_module._internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': 'MCP jump svc', 'workspace_id': self.ws.id, 'template_id': template.id,
                'helm_release_name': 'mcpjump-%s' % suffix, 'helm_namespace': 'paas-ws-mcpjump'})
        self.project = self.env['project.project'].sudo().create({
            'name': 'MCP jump project', 'cloud_service_id': service.id})

    def _post(self, login, params):
        self.authenticate(login, login.replace('_', '-'))
        response = self.url_open(
            '/api/support-projects/%s/mcp-servers' % self.project.id,
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'})
        return response.json()['result']

    def test_shared_judgment(self):
        for url in REJECTED_URLS:
            self.assertTrue(self.McpServer._internal_service_reason(url), url)
        for url in ALLOWED_URLS:
            self.assertIsNone(self.McpServer._internal_service_reason(url), url)

    def test_model_constraint_rejects_with_a_friendly_message(self):
        for url in REJECTED_URLS:
            with self.subTest(url=url), self.assertRaises(ValidationError) as caught:
                self.McpServer.create({'name': 'jump', 'url': url, 'transport': 'sse'})
            self.assertIn('internal platform service', str(caught.exception))
            self.assertNotIn('10.4', str(caught.exception), '訊息不帶位址')
        server = self.McpServer.create({'name': 'ok', 'url': ALLOWED_URLS[1], 'transport': 'sse'})
        with self.assertRaises(ValidationError):
            server.write({'url': REJECTED_URLS[0]})

    def test_owner_and_admin_members_get_the_friendly_error(self):
        for login in (self.owner.login, self.admin.login):
            for url in REJECTED_URLS:
                with self.subTest(login=login, url=url):
                    result = self._post(login, {'action': 'create', 'name': 'jump', 'url': url,
                                                'transport': 'sse'})
                    self.assertFalse(result.get('success'), result)
                    self.assertIn('internal platform service', result.get('error', ''))
        created = self._post(self.owner.login, {'action': 'create', 'name': 'fine',
                                                'url': ALLOWED_URLS[1], 'transport': 'sse'})
        self.assertTrue(created.get('success'), created)
        server_id = created['data']['id']
        updated = self._post(self.admin.login, {'action': 'update', 'server_id': server_id,
                                                'url': REJECTED_URLS[3]})
        self.assertFalse(updated.get('success'), updated)
        self.assertIn('internal platform service', updated.get('error', ''))
        self.assertEqual(self.McpServer.browse(server_id).url, ALLOWED_URLS[1])

    def test_dns_rebinding_is_caught_before_connecting(self):
        server = self.McpServer.create({'name': 'rebind', 'url': 'https://mcp.example.com/sse',
                                        'transport': 'sse'})
        with patch.object(mcp_url_guard, 'resolve',
                          _resolver({'mcp.example.com': {PROXY_CLUSTER_IP}})):
            self.assertFalse(server.action_test_connection())
        self.assertEqual(server.state, 'error')
        self.assertIn('internal platform service', server.state_message)

    def test_client_factory_rechecks_every_request_and_refuses_redirects(self):
        import httpx

        config = self.McpServer.create({'name': 'f', 'url': 'https://mcp.example.com/sse',
                                        'transport': 'sse'})._get_mcp_client_config()
        self.assertIn('httpx_client_factory', config)

        def transport(request):
            if request.url.path == '/redirect':
                return httpx.Response(302, headers={'location': 'http://%s:8080/' % PROXY_CLUSTER_IP})
            return httpx.Response(200, json={})

        def base_factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(transport=httpx.MockTransport(transport))

        async def run(resolver):
            factory = mcp_url_guard.guarded_http_client_factory(base_factory, resolver=resolver)
            async with factory() as client:
                self.assertFalse(client.follow_redirects)
                ok = await client.get('https://mcp.example.com/ok')
                self.assertEqual(ok.status_code, 200)
                with self.assertRaises(mcp_url_guard.McpUrlRejected) as caught:
                    await client.get('https://mcp.example.com/redirect')
                self.assertEqual(caught.exception.reason, mcp_url_guard.REASON_REDIRECT)
            async with factory() as client:
                with self.assertRaises(mcp_url_guard.McpUrlRejected) as caught:
                    await client.get('http://newapi-proxy.new-api-stg:8080/')
                self.assertEqual(caught.exception.reason, mcp_url_guard.REASON_NAMESPACE)
        asyncio.run(run(_resolver()))
