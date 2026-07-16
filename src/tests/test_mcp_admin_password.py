"""Regression tests for #357's one-time MCP admin password flow."""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController


class TestMcpAdminPassword(TransactionCase):
    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'MCP password workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'MCP Odoo test',
            'slug': 'mcp-odoo',
            'category': 'mcp',
            'helm_chart_name': 'oci://example/mcp-server',
            'helm_chart_version': '0.1.5',
            'mcp_enabled': True,
        })
        self.service = self.env['woow_paas_platform.cloud_service'].with_context(
            mcp_admin_password='old-password',
        ).create({
            'name': 'MCP Odoo',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'mcp-odoo-test',
            'helm_values': json.dumps({
                'config': {
                    'nonSensitive': {
                        'connection': {'url': 'https://tenant.example'},
                        'tools': {'enabled': ['search']},
                    },
                },
            }),
        })

    def test_service_detail_masks_all_config_sensitive_leaves(self):
        values = {
            'config': {
                'sensitive': {
                    'admin_password': 'must-not-leak',
                    'nested': {'token': 'must-not-leak-either'},
                },
            },
        }
        sanitized = PaasController._sanitize_helm_values(values, self.template)
        self.assertNotIn('must-not-leak', json.dumps(sanitized))
        self.assertNotIn('must-not-leak-either', json.dumps(sanitized))

    def test_service_detail_api_masks_legacy_sensitive_values(self):
        self.service.write({
            'helm_values': json.dumps({
                'config': {
                    'sensitive': {
                        'admin_password': 'must-not-leak',
                    },
                    'nonSensitive': {
                        'connection': {'url': 'https://tenant.example'},
                        'tools': {'enabled': ['search']},
                    },
                },
            }),
        })
        payload = self.controller._format_service(self.service, include_details=True)
        self.assertNotIn('must-not-leak', json.dumps(payload))
        self.assertEqual(
            payload['helm_values']['config']['nonSensitive']['connection'],
            {'url': 'https://tenant.example'},
        )

    @patch(
        'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
    )
    def test_reset_preserves_gui_owned_connection_and_tools(self, get_client):
        client = MagicMock()
        get_client.return_value = client

        new_password = self.service.action_reset_admin_password()

        self.assertNotEqual(new_password, 'old-password')
        values = json.loads(self.service.helm_values)
        self.assertEqual(
            values['config']['nonSensitive']['connection'],
            {'url': 'https://tenant.example'},
        )
        self.assertEqual(
            values['config']['nonSensitive']['tools'],
            {'enabled': ['search']},
        )
        secret = self.env['woow_paas_platform.cloud_service_secret'].search([
            ('service_id', '=', self.service.id),
        ], limit=1)
        self.assertEqual(secret.admin_password, new_password)
        payload = client.reset_release_config.call_args.kwargs['values']
        self.assertEqual(
            payload,
            {
                'config': {
                    'nonSensitive': {
                        'connection': {'url': 'https://tenant.example'},
                        'tools': {'enabled': ['search']},
                    },
                },
            },
        )
        self.assertEqual(
            client.reset_release_config.call_args.kwargs['sensitive_values'],
            {'config': {'sensitive': {'admin_password': new_password}}},
        )
