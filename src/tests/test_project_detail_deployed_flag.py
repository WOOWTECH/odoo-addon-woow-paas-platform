"""TDD — #271 project-detail payload exposes cloud_service_deployed.

Frontend gates the 'Add Sidecar MCP' button on this so an undeployed
(mcp_enabled) service can't prefill a public sidecar URL that violates the
in-cluster invariant. Design §4 ⑥ / 測試計畫 #7.

Red expectation: 'cloud_service_deployed' not yet in _format_project_detail
payload → KeyError in the assertions.
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


@tagged('post_install', '-at_install')
class TestProjectDetailDeployedFlag(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.alice = _new_user(self.env, 'dep_alice', 'Alice (Deployed)')
        self.ws = self.Workspace.sudo().create({
            'name': 'Dep WS', 'owner_id': self.alice.id,
        })
        self.mcp_template = self.Template.sudo().create({
            'name': 'Dep Tmpl',
            'slug': f'dep-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'dep-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
            'mcp_enabled': True,
        })
        # deployed = helm metadata present.
        self.svc_deployed = self.Service.sudo().create({
            'name': 'Dep Svc',
            'workspace_id': self.ws.id,
            'template_id': self.mcp_template.id,
            'helm_release_name': 'dep-svc',
            'helm_namespace': 'paas-ws-dep',
        })
        self.project_deployed = self.Project.sudo().create({
            'name': 'Dep Project',
            'cloud_service_id': self.svc_deployed.id,
        })
        # undeployed = no helm metadata.
        self.svc_undeployed = self.Service.sudo().create({
            'name': 'Undep Svc',
            'workspace_id': self.ws.id,
            'template_id': self.mcp_template.id,
        })
        self.project_undeployed = self.Project.sudo().create({
            'name': 'Undep Project',
            'cloud_service_id': self.svc_undeployed.id,
        })

    def _detail(self, project):
        response = self.url_open(
            f'/api/support-projects/{project.id}',
            data=json_lib.dumps({
                'jsonrpc': '2.0', 'method': 'call',
                'params': {'action': 'get'},
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn('result', body, f"Body: {body!r}")
        return body['result']['data']

    def test_deployed_service_flag_true(self):
        self.authenticate('dep_alice', 'dep_alice')
        data = self._detail(self.project_deployed)
        self.assertTrue(data['cloud_service_deployed'])
        self.assertTrue(data['cloud_service_mcp_enabled'])

    def test_undeployed_service_flag_false(self):
        self.authenticate('dep_alice', 'dep_alice')
        data = self._detail(self.project_undeployed)
        self.assertFalse(
            data['cloud_service_deployed'],
            "undeployed (no helm metadata) service must report "
            "cloud_service_deployed=False so the frontend hides Add Sidecar")
        self.assertTrue(
            data['cloud_service_mcp_enabled'],
            "template capability flag stays True even when undeployed")
