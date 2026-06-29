"""Seed assertions for MCP server cloud service templates (pilot: hermes)."""
from odoo.tests.common import TransactionCase


class TestMcpServerTemplateSeed(TransactionCase):
    """The mcp-hermes template must be seeded tenant-neutral and sidecar-free."""

    def test_hermes_template_seeded(self):
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        self.assertEqual(tmpl.slug, 'mcp-hermes')
        self.assertEqual(tmpl.category, 'mcp')
        self.assertTrue(tmpl.is_active)
        self.assertEqual(tmpl.default_port, 8080)
        self.assertTrue(tmpl.ingress_enabled)

    def test_hermes_template_is_not_sidecar_backed(self):
        """MCP capability is built into the body, NOT a sidecar."""
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        self.assertFalse(tmpl.mcp_enabled)
        self.assertFalse(tmpl.mcp_sidecar_image)

    def test_hermes_template_no_post_deploy_init(self):
        """config seed handled by chart initContainer; no post-deploy action."""
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        self.assertEqual(tmpl.post_deploy_init_type, 'none')

    def test_hermes_template_uses_mcp_server_chart(self):
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        self.assertIn('mcp-server', tmpl.helm_chart_name)
        self.assertTrue(tmpl.helm_chart_name.startswith('oci://'))

    def test_hermes_plans_seeded_at_odoo_pricing(self):
        monthly = self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_mcp_hermes_yearly')
        self.assertEqual(monthly.sh_plan_price, 499)
        self.assertEqual(monthly.sh_unit, 'month')
        self.assertEqual(yearly.sh_plan_price, 4990)
        self.assertEqual(yearly.sh_unit, 'year')

    def test_hermes_products_link_plans(self):
        prod_m = self.env.ref('woow_paas_platform.product_mcp_hermes_monthly')
        self.assertEqual(prod_m.type, 'service')
        self.assertTrue(prod_m.sh_product_subscribe)
        self.assertEqual(
            prod_m.sh_subscription_plan_id,
            self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly'),
        )

    def test_hermes_template_linked_to_plans(self):
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        linked = tmpl.subscription_plan_ids
        self.assertIn(self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly'), linked)
        self.assertIn(self.env.ref('woow_paas_platform.plan_mcp_hermes_yearly'), linked)

    def test_hermes_plans_not_shared_with_other_service_types(self):
        """The cross-service-type constraint must not be tripped by the seed."""
        monthly = self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly')
        sh = self.env['woow_paas_platform.smart_home_template'].search(
            [('subscription_plan_ids', 'in', monthly.ids)])
        sa = self.env['woow_paas_platform.security_access_template'].search(
            [('subscription_plan_ids', 'in', monthly.ids)])
        self.assertFalse(sh)
        self.assertFalse(sa)
