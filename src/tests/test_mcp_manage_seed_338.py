# -*- coding: utf-8 -*-
"""#338：Odoo Manage MCP 上架骨架 seed。"""
import json
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcpManageTemplateSeed(TransactionCase):

    def test_template_seed(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        self.assertEqual(tpl.category, 'mcp')
        self.assertFalse(tpl.is_active, 'image 未 ready 前必須隱藏（#338）')
        self.assertFalse(tpl.mcp_enabled, 'MCP 內建於 body、非 sidecar（比照三 MCP admin）')
        values = json.loads(tpl.helm_default_values)
        self.assertIn('odoo-manage-mcp-admin', values['image']['repository'])

    def test_description_mentions_prereq(self):
        """租戶側前置說明（API Key／addon）放 description（#338 工作項 3）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        self.assertIn('API Key', tpl.description or '')
