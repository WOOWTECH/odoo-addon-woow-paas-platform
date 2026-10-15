# -*- coding: utf-8 -*-
"""Guard the n8n webhook-error release candidate's seed and init contract."""

import json

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestN8nSeed(TransactionCase):

    def test_pinned_fixed_image_and_chart(self):
        template = self.env.ref('woow_paas_platform.cloud_app_n8n')
        values = json.loads(template.helm_default_values)
        self.assertEqual(template.slug, 'n8n')
        self.assertTrue(template.is_active)
        self.assertEqual(template.helm_chart_name,
                         'oci://8gears.container-registry.com/library/n8n')
        self.assertEqual(template.helm_chart_version, '2.0.1')
        self.assertEqual(values['image'], {
            'repository': 'jcr-prod.woowtech.io/woow-paas-docker-local/n8n',
            'tag': '20261015.2215@sha256:371dd463f02e8c35a4bc5aaf9855fc4cfec1babab97e5a02a1e31923a2ec8f5a',
        })

    def test_init_and_persistent_database_contract(self):
        template = self.env.ref('woow_paas_platform.cloud_app_n8n')
        values = json.loads(template.helm_default_values)
        self.assertEqual(template.post_deploy_init_type, 'n8n')
        self.assertFalse(template.mcp_api_key_helm_path)
        self.assertEqual(values['main']['secret']['N8N_API_KEY'], 'set-by-platform-init')
        self.assertEqual(values['main']['config']['N8N_PUBLIC_API_ENABLED'], 'true')
        self.assertTrue(values['main']['persistence']['enabled'])
        self.assertEqual(values['updateStrategy']['type'], 'Recreate')
        self.assertEqual(values['main']['readinessProbe']['httpGet']['path'],
                         '/healthz/readiness')

    def test_seed_updates_existing_template(self):
        data = self.env['ir.model.data'].search([
            ('module', '=', 'woow_paas_platform'),
            ('name', '=', 'cloud_app_n8n'),
        ], limit=1)
        self.assertTrue(data)
        self.assertFalse(data.noupdate)
