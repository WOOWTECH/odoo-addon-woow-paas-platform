"""Seed assertions for MCP server cloud service templates (hermes / odoo / n8n)."""
import json

from odoo.tests.common import TransactionCase


def _assert_storage_resources_aligned(tc, tmpl):
    """#268 lockstep guard: the seeded helm_default_values must equal the
    advertised marketplace spec, so the PVC size and container limits the
    tenant actually gets match what the marketplace card promised.

    Historic drift (#268): persistence.size was 2Gi while min_storage_gb
    advertised 5, and the container memory limit fell back to the chart
    default 512Mi while min_ram_gb advertised 1.0. Assumes whole-Gi specs
    (all mcp-* templates are 5G storage / 1G RAM / 1 vCPU)."""
    values = json.loads(tmpl.helm_default_values)
    tc.assertEqual(
        values['persistence']['size'], '%dGi' % int(tmpl.min_storage_gb),
        'persistence.size must match advertised min_storage_gb')
    limits = values['resources']['limits']
    tc.assertEqual(
        limits['memory'], '%dGi' % int(tmpl.min_ram_gb),
        'resources.limits.memory must match advertised min_ram_gb')
    tc.assertEqual(
        limits['cpu'], str(int(tmpl.min_vcpu)),
        'resources.limits.cpu must match advertised min_vcpu')


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

    def test_hermes_storage_and_resources_match_advertised_spec(self):
        """#268: PVC size + container limits must equal the advertised spec."""
        tmpl = self.env.ref('woow_paas_platform.cloud_app_mcp_hermes')
        _assert_storage_resources_aligned(self, tmpl)

    def test_hermes_plans_seeded_at_odoo_pricing(self):
        # #330 定價重定：Hermes T1＝1500／15000（沿用 xmlid、原地改 T1）
        monthly = self.env.ref('woow_paas_platform.plan_mcp_hermes_monthly')
        yearly = self.env.ref('woow_paas_platform.plan_mcp_hermes_yearly')
        self.assertEqual(monthly.sh_plan_price, 1500)
        self.assertEqual(monthly.sh_unit, 'month')
        self.assertEqual(yearly.sh_plan_price, 15000)
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


class _McpServerSeedAssertions:
    """Shared seed assertions for an mcp-* cloud service template.

    Subclasses bind app-specific xmlids/values. This mixin is intentionally
    NOT a TransactionCase, so Odoo's test runner skips it and only runs the
    concrete odoo / n8n subclasses below.
    """

    SLUG = None
    TMPL = None
    PLAN_M = None
    PLAN_Y = None
    PROD_M = None
    IMAGE_REPO_SUFFIX = None  # e.g. 'odoo-mcp-admin'
    APP = None  # helm values `app:` selector, e.g. 'odoo'
    EXPECT_ENV_PREFIX = ''  # odoo/n8n run with empty envPrefix (hermes uses 'HERMES_')
    EXPECT_MCP_COMMAND = None  # config.nonSensitive.mcp_server.command per app
    # odoo is live; n8n is seeded inactive pending the A2 launcher (GAP-N1).
    EXPECT_ACTIVE = True

    def test_template_seeded(self):
        tmpl = self.env.ref(self.TMPL)
        self.assertEqual(tmpl.slug, self.SLUG)
        self.assertEqual(tmpl.category, 'mcp')
        self.assertEqual(tmpl.is_active, self.EXPECT_ACTIVE)
        # External port is unified 8080 across all three apps (design D7).
        self.assertEqual(tmpl.default_port, 8080)
        self.assertTrue(tmpl.ingress_enabled)

    def test_template_is_not_sidecar_backed(self):
        """MCP capability is built into the body, NOT the n8n-mcp sidecar."""
        tmpl = self.env.ref(self.TMPL)
        self.assertFalse(tmpl.mcp_enabled)
        self.assertFalse(tmpl.mcp_sidecar_image)

    def test_template_no_post_deploy_init(self):
        """config seed handled by chart initContainer; no post-deploy action."""
        tmpl = self.env.ref(self.TMPL)
        self.assertEqual(tmpl.post_deploy_init_type, 'none')

    def test_template_uses_mcp_server_chart(self):
        tmpl = self.env.ref(self.TMPL)
        self.assertIn('mcp-server', tmpl.helm_chart_name)
        self.assertTrue(tmpl.helm_chart_name.startswith('oci://'))

    def test_template_helm_values_use_camelcase_envprefix(self):
        """camelCase guard (design 階段1 守線): the values key must be
        ``envPrefix``, not ``env_prefix`` — helm silently ignores unknown
        keys and falls back to the chart default, which mis-configures
        odoo/n8n (empty envPrefix) without any error."""
        tmpl = self.env.ref(self.TMPL)
        values = json.loads(tmpl.helm_default_values)
        self.assertIn('envPrefix', values)
        self.assertNotIn('env_prefix', values)
        # Assert the VALUE, not just the key: the most likely copy-paste error
        # is pasting hermes' envPrefix 'HERMES_' (or app 'hermes') and forgetting
        # to clear it — the image-repo check below cannot catch that drift.
        self.assertEqual(values['envPrefix'], self.EXPECT_ENV_PREFIX)
        self.assertEqual(values['app'], self.APP)
        self.assertIn(self.IMAGE_REPO_SUFFIX, values['image']['repository'])
        # External service port stays 8080 regardless of internal app port.
        self.assertEqual(values['service']['port'], 8080)

    def test_template_overrides_chart_default_mcp_command(self):
        """GAP-O1/N1 guard: the shared chart's DEFAULT config is hermes-shaped
        (config.nonSensitive.mcp_server.command='hermes-mcp-server'). odoo/n8n
        MUST override that command in helm_default_values, otherwise the chart
        seeds odoo/n8n with the hermes launch command, the wrong child process
        is spawned (or none), and the MCP endpoint silently 502s while /healthz
        and the GUI still look fine. Asserts the per-app command is set and is
        NOT the stale hermes default — the highest-value runtime footgun in the
        whole replication (caught at odoo layer-2, not by unit tests before)."""
        tmpl = self.env.ref(self.TMPL)
        values = json.loads(tmpl.helm_default_values)
        command = values['config']['nonSensitive']['mcp_server']['command']
        self.assertEqual(command, self.EXPECT_MCP_COMMAND)
        self.assertNotEqual(command, 'hermes-mcp-server')

    def test_template_storage_and_resources_match_advertised_spec(self):
        """#268: PVC size + container limits must equal the advertised spec
        (min_storage_gb / min_ram_gb / min_vcpu) — guards the 2Gi-vs-5G and
        512Mi-vs-1G drift this issue fixed."""
        _assert_storage_resources_aligned(self, self.env.ref(self.TMPL))

    def test_plans_seeded_at_odoo_pricing(self):
        # Wave B 重定：MCP odoo/n8n 月 3000 不動、年 30000→24000（×8 對齊）
        monthly = self.env.ref(self.PLAN_M)
        yearly = self.env.ref(self.PLAN_Y)
        self.assertEqual(monthly.sh_plan_price, 3000)
        self.assertEqual(monthly.sh_unit, 'month')
        self.assertEqual(yearly.sh_plan_price, 24000)
        self.assertEqual(yearly.sh_unit, 'year')

    def test_products_link_plans(self):
        prod_m = self.env.ref(self.PROD_M)
        self.assertEqual(prod_m.type, 'service')
        self.assertTrue(prod_m.sh_product_subscribe)
        self.assertEqual(prod_m.sh_subscription_plan_id, self.env.ref(self.PLAN_M))

    def test_template_linked_to_plans(self):
        tmpl = self.env.ref(self.TMPL)
        linked = tmpl.subscription_plan_ids
        self.assertIn(self.env.ref(self.PLAN_M), linked)
        self.assertIn(self.env.ref(self.PLAN_Y), linked)

    def test_plan_bound_only_to_its_own_template(self):
        """Replication footgun guard: the mcp-* plan must not be cross-linked
        to a different cloud_app_template (e.g. the regular cloud_app_odoo /
        cloud_app_n8n cloud service), which would happen if a copy-paste
        reused the wrong plan xmlid. Covers BOTH monthly and yearly — the
        yearly plan is just as likely to be mis-bound and the model
        @api.constrains does not catch same-(cloud)-model cross-binding."""
        Template = self.env['woow_paas_platform.cloud_app_template']
        own = self.env.ref(self.TMPL)
        for plan_ref in (self.PLAN_M, self.PLAN_Y):
            plan = self.env.ref(plan_ref)
            others = Template.search(
                [('subscription_plan_ids', 'in', plan.ids),
                 ('id', '!=', own.id)])
            self.assertFalse(others, "%s cross-linked to %s" % (plan_ref, others))

    def test_plans_not_shared_with_other_service_types(self):
        """The cross-service-type constraint must not be tripped by the seed
        (both monthly and yearly)."""
        for plan_ref in (self.PLAN_M, self.PLAN_Y):
            plan = self.env.ref(plan_ref)
            sh = self.env['woow_paas_platform.smart_home_template'].search(
                [('subscription_plan_ids', 'in', plan.ids)])
            sa = self.env['woow_paas_platform.security_access_template'].search(
                [('subscription_plan_ids', 'in', plan.ids)])
            self.assertFalse(sh, "%s leaked to smart_home" % plan_ref)
            self.assertFalse(sa, "%s leaked to security_access" % plan_ref)


class TestMcpOdooTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-odoo template: tenant-neutral, sidecar-free, odoo-level pricing."""

    SLUG = 'mcp-odoo'
    TMPL = 'woow_paas_platform.cloud_app_mcp_odoo'
    PLAN_M = 'woow_paas_platform.plan_mcp_odoo_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_odoo_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_odoo_monthly'
    IMAGE_REPO_SUFFIX = 'odoo-mcp-admin'
    APP = 'odoo'
    # odoo-mcp-server 0.1.3 is stdio-only; launched via core's mcp_sse_wrapper
    # (GAP-O1: architecture.md's 'odoo-mcp-server --transport sse' is stale).
    EXPECT_MCP_COMMAND = 'python'


class TestMcpN8nTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-n8n template: tenant-neutral, sidecar-free, odoo-level pricing.

    Active (launcher sha 2ef1dc16 + chart 0.1.4, whose secret.yaml
    self-generates the mandatory n8n AUTH_TOKEN — no operator per-instance
    Secret needed); validated layer-3 on stg. EXPECT_ACTIVE defaults to True.
    """

    SLUG = 'mcp-n8n'
    TMPL = 'woow_paas_platform.cloud_app_mcp_n8n'
    PLAN_M = 'woow_paas_platform.plan_mcp_n8n_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_n8n_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_n8n_monthly'
    IMAGE_REPO_SUFFIX = 'n8n-mcp-admin'
    APP = 'n8n'
    EXPECT_MCP_COMMAND = 'n8n-mcp-http'


class TestMcpOdooManageTemplateSeed(_McpServerSeedAssertions, TransactionCase):
    """mcp-odoo-manage template（#338 範疇①）: tenant-neutral, sidecar-free, odoo-level pricing.

    Active（image 真接線: odoo-manage-mcp-admin 雙釘 digest, woow-mcp-server #14 打包
    ivnvxd/mcp-server-odoo 0.7.1）. 底層 server 用 FastMCP、原生 StreamableHTTP，故直跑
    console script `mcp-server-odoo`（NOT 標準 odoo 的 mcp_sse_wrapper 包）——ivnvxd 無
    module-level server 物件，wrapper spec 對它不存在. EXPECT_ACTIVE defaults to True.
    """

    SLUG = 'mcp-odoo-manage'
    TMPL = 'woow_paas_platform.cloud_app_mcp_odoo_manage'
    PLAN_M = 'woow_paas_platform.plan_mcp_odoo_manage_monthly'
    PLAN_Y = 'woow_paas_platform.plan_mcp_odoo_manage_yearly'
    PROD_M = 'woow_paas_platform.product_mcp_odoo_manage_monthly'
    IMAGE_REPO_SUFFIX = 'odoo-manage-mcp-admin'
    APP = 'odoo-manage'
    EXPECT_MCP_COMMAND = 'mcp-server-odoo'
