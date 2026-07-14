# -*- coding: utf-8 -*-
"""#338 範疇①：Odoo Manage MCP 真 image 接線 seed。"""
import json
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMcpManageTemplateSeed(TransactionCase):

    def test_template_seed(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        self.assertEqual(tpl.category, 'mcp')
        # 範疇①上架：image ready 後翻 is_active=True（原骨架為 False）。
        self.assertTrue(tpl.is_active, 'image ready 後上架，應 is_active=True（#338 範疇①）')
        self.assertFalse(tpl.mcp_enabled, 'MCP 內建於 body、非 sidecar（比照三 MCP admin）')
        values = json.loads(tpl.helm_default_values)
        self.assertIn('odoo-manage-mcp-admin', values['image']['repository'])

    def test_image_tag_is_pinned_digest(self):
        """上架必須釘死 digest（不得留 latest 佔位）——雙釘 git-sha@sha256。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        tag = json.loads(tpl.helm_default_values)['image']['tag']
        self.assertNotEqual(tag, 'latest', '上架不得留 latest 佔位')
        self.assertIn('@sha256:', tag, 'image tag 必須雙釘 git-sha@sha256')

    def test_mcp_server_launch_uses_native_streamable_http(self):
        """啟動契約守線：ivnvxd mcp-server-odoo 0.7.1 用 FastMCP、原生 StreamableHTTP，
        直跑 console script（比照 n8n），NOT 標準 odoo 的 mcp_sse_wrapper 包。骨架的
        mcp_server_odoo_manage.server:server 是 Wave A 佔位猜測、對 ivnvxd 不存在，若回退到
        wrapper spec 會 ImportError；此測鎖定正確入口。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        mcp = json.loads(tpl.helm_default_values)['config']['nonSensitive']['mcp_server']
        self.assertEqual(mcp['command'], 'mcp-server-odoo')
        self.assertIn('streamable-http', mcp['args'])
        self.assertNotIn('mcp_admin_core.mcp_sse_wrapper', mcp['args'],
                         'ivnvxd 原生 http、不經 wrapper')
        self.assertEqual(mcp['port'], 8000)

    def test_description_mentions_prereq(self):
        """租戶側前置說明（API Key／addon）放 description（#338 工作項 3）。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        self.assertIn('API Key', tpl.description or '')


@tagged('post_install', '-at_install')
class TestMcpManagePlansSeed(TransactionCase):

    EXPECTED = {
        'plan_mcp_odoo_manage_monthly': (3000, 'month'),
        'plan_mcp_odoo_manage_yearly': (24000, 'year'),
    }

    def test_plans(self):
        for xmlid, (price, unit) in self.EXPECTED.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertEqual(plan.sh_plan_price, price)
            self.assertEqual(plan.sh_unit, unit)
            self.assertEqual(plan.service_type, 'cloud_service')

    def test_products_and_m2m(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo_manage')
        plans = tpl.sudo().subscription_plan_ids
        # Wave B 後另掛 access 變體（ac_max_access_seats>0）；本測驗 base 骨架，
        # 故過濾變體只數 base（monthly+yearly）。
        base_plans = plans.filtered(lambda p: p.ac_max_access_seats == 0)
        self.assertEqual(len(base_plans), 2)
        for xmlid in self.EXPECTED:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}')
            self.assertIn(plan, plans)
            product = self.env.ref(
                f'woow_paas_platform.{xmlid.replace("plan_", "product_")}')
            self.assertEqual(product.sh_subscription_plan_id, plan)
