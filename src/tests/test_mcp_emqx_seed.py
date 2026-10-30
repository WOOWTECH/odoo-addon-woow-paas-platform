"""EMQX MCP seed, normal plans and the real credential assembly path.

The published PR97 app owns its 39-tool default and per-operation revocation.
These ORM tests protect the platform contract; they do not claim broker calls.
"""
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..controllers.paas import PaasController
from ..models.cloud_service import _internal_provision


IMAGE_TAG = (
    'b5ed89ca01acfc33a7ef57987f0f64a11f603ea3@sha256:'
    'f60858334f11147675a0071b5caf699692ccf31fb493347da5d856bf9a8828dc'
)
JCR = 'jcr-prod.woowtech.io/woow-paas-docker-local/'


@tagged('post_install', '-at_install')
class TestMcpEmqxSeed(TransactionCase):
    def _template(self):
        return self.env.ref('woow_paas_platform.cloud_app_mcp_emqx')

    def _values(self):
        return json.loads(self._template().helm_default_values)

    def _service(self, suffix='one'):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'emqx-seed.invalid')
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'EMQX credential fixture ' + suffix,
        })
        template = self._template()
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'emqx-seed-' + suffix,
            'workspace_id': workspace.id,
            'template_id': template.id,
            'state': 'running',
            'subdomain': 'emqx-seed-' + suffix,
            'helm_namespace': 'paas-ws-emqx-seed',
            'helm_release_name': 'emqx-seed-' + suffix,
            'helm_values': template.helm_default_values,
        })

    def test_marketplace_identity_is_separate_from_broker(self):
        template = self._template()
        broker = self.env.ref('woow_paas_platform.cloud_app_emqx')
        self.assertNotEqual(template, broker)
        self.assertEqual((template.name, template.slug, template.category),
                         ('MCP：EMQX', 'mcp-emqx', 'mcp'))
        self.assertEqual(template.workload_type, 'container')
        self.assertEqual(template.default_port, 8080)
        self.assertEqual(self._values()['service'], {'port': 8080})
        self.assertEqual(set(json.loads(template.tags)), {'mcp', 'emqx', 'mqtt', 'mcp-server', 'ai'})

    def test_exact_published_image_and_chart(self):
        template = self._template()
        self.assertEqual(template.helm_chart_name, 'oci://' + JCR + 'mcp-server')
        self.assertEqual(template.helm_chart_version, '0.1.9')
        self.assertEqual(self._values()['image'], {
            'repository': JCR + 'emqx-mcp-admin', 'tag': IMAGE_TAG,
        })
        self.assertEqual(self._values()['app'], 'emqx')

    def test_fixed_child_launch_and_env_prefix(self):
        values = self._values()
        self.assertEqual(values['envPrefix'], 'EMQX_MCP_')
        self.assertEqual(values['config']['nonSensitive']['mcp_server'], {
            'command': 'emqx-mcp-server', 'args': ['--port', '8000'],
            'port': 8000, 'env_prefix': 'EMQX_MCP_',
        })

    def test_app_owns_all_on_and_revocable_sql_policy(self):
        # Exact permitted shape rejects tools/SQL/readonly/disabled lists and
        # connection or arbitrary env injection at every supported seed entry.
        values = self._values()
        self.assertEqual(set(values), {
            'app', 'image', 'service', 'envPrefix', 'persistence',
            'updateStrategy', 'resources', 'config',
        })
        self.assertEqual(set(values['config']), {
            'requireFields', 'requiredFields', 'sensitive', 'nonSensitive',
        })
        self.assertEqual(set(values['config']['nonSensitive']), {'mcp_server'})

    def test_connection_is_tenant_supplied_not_a_boot_requirement(self):
        config = self._values()['config']
        self.assertTrue(config['requireFields'])
        self.assertEqual(config['requiredFields'], ['admin_password'])
        self.assertEqual(config['sensitive'], {})
        self.assertEqual(json.loads(self._template().helm_value_specs), {
            'required': [], 'optional': [],
        })
        for key in ('api_key', 'api_secret', 'base_url', 'WEBHOOK_SECURITY_MODE'):
            self.assertNotIn(key, json.dumps(self._values()))

    def test_listed_after_actual_backend_and_template_acceptance(self):
        template = self._template()
        self.assertTrue(template.is_active)
        self.assertFalse(template.is_hidden)
        self.assertFalse(template.conceal_instances)
        public = self.env['woow_paas_platform.cloud_app_template'].search([
            ('is_active', '=', True), ('is_hidden', '=', False),
        ])
        self.assertIn(template, public)
        self.assertIn(self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud'), public)

    def test_operator_copy_explains_actual_grants_and_credentials(self):
        text = self._template().full_description
        for required in ('39', '7 類', '預設全開', '寫入', '刪除',
                         'emqx_test_rule_sql', 'execute', '獨立撤銷',
                         'Connection', 'API key', 'API secret', 'Dashboard',
                         'Tokens', '/private_<token>/mcp', 'Streamable HTTP'):
            self.assertIn(required, text)

    def test_two_normal_plans_with_no_foreign_links(self):
        template = self._template()
        plans = self.env.ref('woow_paas_platform.plan_mcp_emqx_monthly') | self.env.ref(
            'woow_paas_platform.plan_mcp_emqx_yearly')
        self.assertEqual(set(template.subscription_plan_ids.ids), set(plans.ids))
        for plan in plans:
            linked = self.env['woow_paas_platform.cloud_app_template'].search([
                ('subscription_plan_ids', 'in', plan.ids),
            ])
            self.assertEqual(linked, template)
            self.assertEqual(plan.service_type, 'cloud_service')
            self.assertFalse(plan.storefront_hidden)
            self.assertEqual(plan.sh_duration, 1)
            self.assertTrue(plan.sh_override_product)
            self.assertTrue(plan.sh_never_expire)
            self.assertEqual(plan.sh_no_of_billing_cycle, -1)

    def test_monthly_yearly_prices_trials_and_products(self):
        for cadence, unit, price in (('monthly', 'month', 3000), ('yearly', 'year', 24000)):
            plan = self.env.ref('woow_paas_platform.plan_mcp_emqx_' + cadence)
            product = self.env.ref('woow_paas_platform.product_mcp_emqx_' + cadence)
            self.assertEqual((plan.sh_unit, plan.sh_plan_price), (unit, price))
            self.assertTrue(plan.sh_trial)
            self.assertEqual((plan.sh_trial_duration, plan.sh_trial_unit), (7, 'day'))
            self.assertFalse(plan.sh_start_immediately)
            self.assertTrue(plan.sh_is_close_by_customer)
            self.assertEqual(product.sh_subscription_plan_id, plan)
            self.assertEqual(product.list_price, price)
            self.assertEqual(product.type, 'service')
            self.assertTrue(product.sale_ok)
            self.assertTrue(product.sh_product_subscribe)
            self.assertFalse(product.purchase_ok)
            self.assertFalse(product.taxes_id.filtered(lambda tax: not tax.price_include))

    def test_seed_metadata_converges_template_and_preserves_plan_choices(self):
        names = ['cloud_app_mcp_emqx', 'plan_mcp_emqx_monthly', 'plan_mcp_emqx_yearly',
                 'product_mcp_emqx_monthly', 'product_mcp_emqx_yearly']
        rows = self.env['ir.model.data'].search([
            ('module', '=', 'woow_paas_platform'), ('name', 'in', names),
        ])
        self.assertEqual(set(rows.mapped('name')), set(names))
        for row in rows:
            self.assertEqual(row.noupdate, row.name != 'cloud_app_mcp_emqx')

    def test_generated_admin_credentials_are_unique_and_not_image_default(self):
        first, second = self._service('first'), self._service('second')
        first_secret, second_secret = first._get_secret_record(), second._get_secret_record()
        self.assertTrue(first_secret)
        self.assertTrue(second_secret)
        self.assertNotEqual(first_secret, second_secret)
        self.assertNotEqual(first_secret.admin_password, second_secret.admin_password)
        for secret in (first_secret, second_secret):
            self.assertGreaterEqual(len(secret.admin_password), 16)
            self.assertNotEqual(secret.admin_password, 'admin')
            self.assertFalse(secret.gateway_api_key)

    def test_only_admin_secret_enters_the_sensitive_channel(self):
        service = self._service()
        password = service._get_secret_record().admin_password
        self.assertEqual(service._build_mcp_sensitive_values(), {
            'config': {'sensitive': {'admin_password': password}},
        })
        self.assertNotIn(password, service.helm_values)
        self.assertNotIn(password, self._template().helm_default_values)
        self.assertEqual(self._template().admin_password_carrier, 'config_json')
        self.assertFalse(service._chart_has_gateway())
        self.assertNotIn('gateway_api_key', PaasController._gateway_credentials(service))

    def test_reset_uses_config_json_and_keeps_launch_and_broker_state_out_of_values(self):
        service = self._service()
        original_values = service.helm_values
        original_password = service._get_secret_record().admin_password
        with patch('odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client') as get_client:
            get_client.return_value.reset_release_config.return_value = {'chart_version': '0.1.9'}
            service._reset_admin_password()
        call = get_client.return_value.reset_release_config
        call.assert_called_once()
        self.assertEqual(call.call_args.kwargs['admin_password_carrier'], 'config_json')
        new_password = service._get_secret_record().admin_password
        self.assertNotEqual(new_password, original_password)
        self.assertEqual(call.call_args.kwargs['sensitive_values'], {
            'config': {'sensitive': {'admin_password': new_password}},
        })
        self.assertEqual(service.helm_values, original_values)
