"""TDD — #271 mcp_server sidecar-backed url in-cluster invariant.

Model-layer @api.constrains + shared _is_in_cluster_url helper.
Design: docs/plans/2026-Q3/2026-07-06-271-internal-mcp-bypass-guard-design.md §4 ①

Red expectation (pre-impl):
  * _is_in_cluster_url helper does not exist yet → AttributeError.
  * No @api.constrains → creating sidecar_backed=True + public url succeeds
    → assertRaises(ValidationError) fails ("ValidationError not raised").
"""
import uuid

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcpSidecarUrlInvariant(TransactionCase):
    """#271: sidecar_backed=True ⟹ url is in-cluster (http + .svc.cluster.local)."""

    _IN_CLUSTER = 'http://inv-svc-mcp.paas-ws-inv.svc.cluster.local:3001/mcp'
    _PUBLIC = 'https://mcp.example.com/sse'
    _HTTPS_CLUSTER = 'https://inv-svc-mcp.paas-ws-inv.svc.cluster.local:3001/mcp'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.McpServer = cls.env['woow_paas_platform.mcp_server']
        cls.Workspace = cls.env['woow_paas_platform.workspace']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        cls.Project = cls.env['project.project']

        cls.workspace = cls.Workspace.sudo().create({'name': 'Inv WS'})
        cls.template = cls.Template.sudo().create({
            'name': 'Inv Tmpl',
            'slug': f'inv-tpl-{uuid.uuid4().hex[:8]}',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'inv-tpl',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        cls.service = cls.Service.sudo().create({
            'name': 'Inv Svc',
            'workspace_id': cls.workspace.id,
            'template_id': cls.template.id,
            'helm_release_name': 'inv-svc',
            'helm_namespace': 'paas-ws-inv',
        })
        cls.project = cls.Project.sudo().create({
            'name': 'Inv Project',
            'cloud_service_id': cls.service.id,
        })

    def _make(self, **overrides):
        vals = {
            'name': 'Inv MCP',
            'url': self._IN_CLUSTER,
            'transport': 'streamable_http',
            'scope': 'user',
            'project_id': self.project.id,
            'sidecar_backed': True,
        }
        vals.update(overrides)
        return self.McpServer.sudo().create(vals)

    # ---- helper unit ---- #
    def test_helper_is_in_cluster_url(self):
        self.assertTrue(self.McpServer._is_in_cluster_url(self._IN_CLUSTER))
        self.assertFalse(self.McpServer._is_in_cluster_url(self._PUBLIC))
        self.assertFalse(
            self.McpServer._is_in_cluster_url(self._HTTPS_CLUSTER),
            "https scheme must fail even on a cluster.local host "
            "(in-cluster sidecar is plain http)",
        )
        self.assertFalse(self.McpServer._is_in_cluster_url(''))
        self.assertFalse(self.McpServer._is_in_cluster_url('not a url'))

    # ---- constrains (sudo path — proves no su-env false-green) ---- #
    def test_sidecar_backed_public_url_rejected(self):
        with self.assertRaises(ValidationError):
            self._make(url=self._PUBLIC)

    def test_sidecar_backed_https_in_cluster_rejected(self):
        with self.assertRaises(ValidationError):
            self._make(url=self._HTTPS_CLUSTER)

    def test_sidecar_backed_in_cluster_accepted(self):
        mcp = self._make()
        self.assertTrue(mcp.exists())
        self.assertTrue(mcp.sidecar_backed)

    def test_non_sidecar_public_url_accepted(self):
        mcp = self._make(sidecar_backed=False, url=self._PUBLIC)
        self.assertTrue(mcp.exists())
        self.assertFalse(mcp.sidecar_backed)

    def test_write_flip_in_cluster_to_public_rejected(self):
        mcp = self._make()
        original_url = mcp.url
        with self.assertRaises(ValidationError):
            mcp.write({'url': self._PUBLIC})
        mcp.invalidate_recordset()
        self.assertEqual(
            mcp.url, original_url,
            "rejected write must not persist the public url",
        )
