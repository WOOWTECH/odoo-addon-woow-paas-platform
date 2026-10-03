"""Tests for Cloud App Template model."""
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


class TestCloudAppTemplate(TransactionCase):
    """Test cases for woow_paas_platform.cloud_app_template model."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.Template = self.env['woow_paas_platform.cloud_app_template']

    def test_create_template_minimal(self):
        """Test creating a template with minimal required fields."""
        template = self.Template.create({
            'name': 'Test App',
            'slug': 'test-app',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test-chart',
            'helm_chart_version': '1.0.0',
        })
        self.assertEqual(template.name, 'Test App')
        self.assertEqual(template.slug, 'test-app')
        self.assertTrue(template.is_active)
        self.assertEqual(template.default_port, 80)
        self.assertTrue(template.ingress_enabled)

    def test_create_template_with_category(self):
        """Test creating template with category selection."""
        template = self.Template.create({
            'name': 'PostgreSQL',
            'slug': 'postgresql',
            'category': 'database',
            'helm_repo_url': 'https://charts.bitnami.com/bitnami',
            'helm_chart_name': 'postgresql',
            'helm_chart_version': '15.0.0',
        })
        self.assertEqual(template.category, 'database')

    def test_create_template_with_resources(self):
        """Test creating template with custom resource requirements."""
        template = self.Template.create({
            'name': 'Heavy App',
            'slug': 'heavy-app',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'heavy',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 4,
            'min_ram_gb': 8.0,
            'min_storage_gb': 100,
        })
        self.assertEqual(template.min_vcpu, 4)
        self.assertEqual(template.min_ram_gb, 8.0)
        self.assertEqual(template.min_storage_gb, 100)

    def test_create_template_with_default_values(self):
        """Test creating template with default Helm values."""
        template = self.Template.create({
            'name': 'App with Values',
            'slug': 'app-values',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'app',
            'helm_chart_version': '1.0.0',
            'helm_default_values': '{"replicas": 2, "resources": {"cpu": "500m"}}',
        })
        self.assertIn('replicas', template.helm_default_values)

    def test_template_default_values(self):
        """Test that templates have sensible defaults."""
        template = self.Template.create({
            'name': 'Default Test',
            'slug': 'default-test',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
        })
        # Check defaults
        self.assertEqual(template.default_port, 80)
        self.assertTrue(template.ingress_enabled)
        self.assertEqual(template.min_vcpu, 1)
        self.assertEqual(template.min_ram_gb, 1.0)
        self.assertEqual(template.min_storage_gb, 5)
        self.assertTrue(template.is_active)

    def test_category_options(self):
        """Test that all category options are valid."""
        valid_categories = [
            'ai_llm', 'automation', 'database',
            'analytics', 'devops', 'web', 'container', 'mcp'
        ]

        for category in valid_categories:
            template = self.Template.create({
                'name': f'Test {category}',
                'slug': f'test-{category}',
                'category': category,
                'helm_repo_url': 'https://charts.example.com',
                'helm_chart_name': 'test',
                'helm_chart_version': '1.0.0',
            })
            self.assertEqual(template.category, category)

    def test_deactivate_template(self):
        """Test deactivating a template."""
        template = self.Template.create({
            'name': 'To Deactivate',
            'slug': 'to-deactivate',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
        })
        self.assertTrue(template.is_active)

        template.write({'is_active': False})
        self.assertFalse(template.is_active)

    def test_update_chart_version(self):
        """Test updating Helm chart version."""
        template = self.Template.create({
            'name': 'Version Test',
            'slug': 'version-test',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
        })
        self.assertEqual(template.helm_chart_version, '1.0.0')

        template.write({'helm_chart_version': '2.0.0'})
        self.assertEqual(template.helm_chart_version, '2.0.0')

    def test_search_by_category(self):
        """Test searching templates by category."""
        # Create templates in different categories
        self.Template.create({
            'name': 'DB 1',
            'slug': 'db-1',
            'category': 'database',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'db',
            'helm_chart_version': '1.0.0',
        })
        self.Template.create({
            'name': 'Web 1',
            'slug': 'web-1',
            'category': 'web',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'web',
            'helm_chart_version': '1.0.0',
        })

        db_templates = self.Template.search([('category', '=', 'database')])
        self.assertGreaterEqual(len(db_templates), 1)
        self.assertTrue(all(t.category == 'database' for t in db_templates))

    def test_search_active_templates(self):
        """Test searching only active templates."""
        self.Template.create({
            'name': 'Active Template',
            'slug': 'active',
            'is_active': True,
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
        })
        self.Template.create({
            'name': 'Inactive Template',
            'slug': 'inactive',
            'is_active': False,
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
        })

        active_templates = self.Template.search([('is_active', '=', True)])
        self.assertGreaterEqual(len(active_templates), 1)
        self.assertTrue(all(t.is_active for t in active_templates))

    def test_no_seeded_spec_targets_the_platform_stripped_subtree(self):
        """租戶可填欄位的 key 不得落在 ``config.sensitive`` 底下。

        平台把 ``config.sensitive`` 整棵當成自己的憑證通道：存檔與部署前，
        ``CloudService._strip_mcp_sensitive_values`` 會從租戶送來的 values 剝掉
        這棵，再只回填平台自己供給的值（admin_password）。放在這裡的 spec 欄位
        ⇒ 租戶填的值**永遠到不了 chart**，而且沒有任何錯誤提示。

        2026-10-03 正式站實例：EMQX 的 ngrok token 用 ``config.sensitive.ngrok_authtoken``
        ⇒ chart 收不到 token、helm upgrade 失敗、租戶服務進 error（已改 ``ngrok.authtoken``）。
        掃全部 seed template（含隱藏／封存），新商品上架時一起受守。
        """
        templates = self.Template.with_context(active_test=False).search([])
        offenders = []
        for tpl in templates:
            specs = tpl._parse_helm_value_specs() or {}
            for group in ('required', 'optional'):
                for spec in specs.get(group) or []:
                    key = spec.get('key') if isinstance(spec, dict) else spec
                    if isinstance(key, str) and (
                            key == 'config.sensitive'
                            or key.startswith('config.sensitive.')):
                        offenders.append('%s: %s' % (tpl.slug, key))
        self.assertFalse(
            offenders,
            'spec key 落在平台會剝掉的 config.sensitive 底下（租戶值到不了 chart）：%s'
            % ', '.join(offenders))
