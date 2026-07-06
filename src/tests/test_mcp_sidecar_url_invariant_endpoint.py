"""TDD — #271 controller-layer friendly error for sidecar url invariant.

Covers all 4 write endpoints (legacy + project-centric, create + update).
Design §4 ②. Reference pattern: test_mcp_external_url_creation.py.

Red expectation (after Task 1, before Task 2):
  * Legacy update path has NO try/except ValidationError, and old
    _validate_mcp_url(url) ignores the stored flag → model constrains raises
    an UNCAUGHT ValidationError → JSON-RPC body has no 'result' →
    test_legacy_update_sidecar_row_to_external_rejected fails in _post
    (AssertionError: 'result' not in body).
  * The project-centric create/update cases may already pass via Task 1's
    model @api.constrains (endpoints catch ValidationError) — that is
    defense-in-depth and expected; Task 2 adds the controller-layer error
    string + covers the legacy path.
"""
import json as json_lib
import uuid

from odoo.tests.common import HttpCase, tagged


def _new_user(env, login, name=None):
    return env['res.users'].sudo().create({
        'name': name or login,
        'login': login,
        'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


_IN_CLUSTER = 'http://inv-ep-svc-mcp.paas-ws-invep.svc.cluster.local:3001/mcp'
_EXTERNAL = 'https://external.example.com/sse'


@tagged('post_install', '-at_install')
class TestMcpSidecarUrlInvariantEndpoint(HttpCase):
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
        # portal write flag is fail-closed off by default.
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.alice = _new_user(self.env, 'invep_alice', 'Alice (InvEp)')
        self.ws = self.Workspace.sudo().create({
            'name': 'InvEp WS', 'owner_id': self.alice.id,
        })
        self.template = self.Template.sudo().create({
            'name': 'InvEp Tmpl',
            'slug': f'invep-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'invep-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
            'mcp_sidecar_port': 3001,
            'mcp_transport': 'streamable_http',
            'mcp_endpoint_path': '/mcp',
        })
        self.service = self.Service.sudo().create({
            'name': 'InvEp Svc',
            'workspace_id': self.ws.id,
            'template_id': self.template.id,
            'helm_release_name': 'inv-ep-svc',
            'helm_namespace': 'paas-ws-invep',
        })
        self.project = self.Project.sudo().create({
            'name': 'InvEp Project',
            'cloud_service_id': self.service.id,
        })
        # An existing sidecar-backed in-cluster row (valid under the invariant).
        self.existing_sidecar = self.McpServer.sudo().create({
            'name': 'InvEp Existing Sidecar',
            'url': _IN_CLUSTER,
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project.id,
            'sidecar_backed': True,
        })

    # ---- JSON-RPC helpers ---- #
    def _post(self, url, params):
        response = self.url_open(
            url,
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': params,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']

    def _project_url(self):
        return f'/api/support-projects/{self.project.id}/mcp-servers'

    def _legacy_url(self):
        return (
            f'/api/workspaces/{self.ws.id}'
            f'/services/{self.service.id}/mcp-servers'
        )

    # ---- project-centric create ---- #
    def test_project_create_sidecar_backed_external_rejected(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._project_url(), {
            'action': 'create',
            'name': 'Backed External',
            'url': _EXTERNAL,
            'transport': 'sse',
            'sidecar_backed': True,
        })
        self.assertFalse(result.get('success'), f"Got: {result!r}")
        self.assertIn('cluster.local', (result.get('error') or '').lower())

    def test_project_create_sidecar_backed_in_cluster_ok(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._project_url(), {
            'action': 'create',
            'name': 'Backed InCluster',
            'url': _IN_CLUSTER,
            'transport': 'streamable_http',
            'sidecar_backed': True,
        })
        self.assertTrue(result.get('success'), f"Got: {result!r}")

    # ---- project-centric update ---- #
    def test_project_update_sidecar_row_to_external_rejected(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._project_url(), {
            'action': 'update',
            'server_id': self.existing_sidecar.id,
            'url': _EXTERNAL,
        })
        self.assertFalse(result.get('success'), f"Got: {result!r}")
        self.assertIn('cluster.local', (result.get('error') or '').lower())
        self.existing_sidecar.invalidate_recordset()
        self.assertEqual(
            self.existing_sidecar.url, _IN_CLUSTER,
            "rejected update must not persist the external url",
        )

    # ---- legacy update (the true RED — no try/except in that path) ---- #
    def test_legacy_update_sidecar_row_to_external_rejected(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._legacy_url(), {
            'action': 'update',
            'server_id': self.existing_sidecar.id,
            'url': _EXTERNAL,
        })
        self.assertFalse(result.get('success'), f"Got: {result!r}")
        self.assertIn('cluster.local', (result.get('error') or '').lower())
        self.existing_sidecar.invalidate_recordset()
        self.assertEqual(self.existing_sidecar.url, _IN_CLUSTER)

    # ---- legacy create ignores the flag → external stays allowed ---- #
    def test_legacy_create_ignores_sidecar_flag_external_ok(self):
        self.authenticate('invep_alice', 'invep_alice')
        result = self._post(self._legacy_url(), {
            'action': 'create',
            'name': 'Legacy External',
            'url': _EXTERNAL,
            'transport': 'sse',
            'sidecar_backed': True,  # ignored by legacy create
        })
        self.assertTrue(result.get('success'), f"Got: {result!r}")
        new_id = (result.get('data') or {}).get('id')
        self.assertTrue(new_id)
        self.assertFalse(
            self.McpServer.sudo().browse(new_id).sidecar_backed,
            "legacy create must never store sidecar_backed=True (flag "
            "excluded from create vals) — so external url stays allowed",
        )
