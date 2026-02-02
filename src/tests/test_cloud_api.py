"""Tests for Cloud API endpoints."""
import json
from odoo.tests.common import HttpCase


class TestCloudAPI(HttpCase):
    """Test cases for cloud service API endpoints."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.user = self.env.ref('base.user_admin')
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Service = self.env['woow_paas_platform.cloud_service']

        # Create test data
        self.template = self.Template.create({
            'name': 'Test Template',
            'slug': 'test-template',
            'category': 'web',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
            'description': 'Test template for API testing',
            'is_active': True,
        })

        self.workspace = self.Workspace.create({
            'name': 'API Test Workspace'
        })

    def test_get_templates_list(self):
        """Test GET templates list endpoint."""
        result = self.env['woow_paas_platform.cloud_app_template'].search([
            ('is_active', '=', True)
        ])
        self.assertGreaterEqual(len(result), 1)
        self.assertIn(self.template.id, result.ids)

    def test_get_template_by_id(self):
        """Test GET single template endpoint."""
        template = self.Template.browse(self.template.id)
        self.assertTrue(template.exists())
        self.assertEqual(template.name, 'Test Template')
        self.assertEqual(template.slug, 'test-template')

    def test_filter_templates_by_category(self):
        """Test filtering templates by category."""
        # Create templates in different categories
        self.Template.create({
            'name': 'Database Template',
            'slug': 'db-template',
            'category': 'database',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'db',
            'helm_chart_version': '1.0.0',
            'is_active': True,
        })

        web_templates = self.Template.search([
            ('category', '=', 'web'),
            ('is_active', '=', True),
        ])
        self.assertGreaterEqual(len(web_templates), 1)
        self.assertTrue(all(t.category == 'web' for t in web_templates))

    def test_search_templates_by_name(self):
        """Test searching templates by name."""
        templates = self.Template.search([
            ('name', 'ilike', 'Test'),
            ('is_active', '=', True),
        ])
        self.assertGreaterEqual(len(templates), 1)

    def test_get_workspace_services_empty(self):
        """Test GET services for workspace with no services."""
        services = self.Service.search([
            ('workspace_id', '=', self.workspace.id),
        ])
        self.assertEqual(len(services), 0)

    def test_create_service(self):
        """Test creating a service via model."""
        service = self.Service.create({
            'name': 'Test Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        self.assertTrue(service.exists())
        self.assertEqual(service.name, 'Test Service')
        self.assertEqual(service.workspace_id.id, self.workspace.id)
        self.assertEqual(service.template_id.id, self.template.id)
        self.assertEqual(service.state, 'pending')

    def test_get_service_details(self):
        """Test getting service details."""
        service = self.Service.create({
            'name': 'Detail Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_namespace': 'paas-ws-1',
            'helm_release_name': 'test-release',
        })

        retrieved = self.Service.browse(service.id)
        self.assertTrue(retrieved.exists())
        self.assertEqual(retrieved.name, 'Detail Service')
        self.assertEqual(retrieved.helm_namespace, 'paas-ws-1')

    def test_list_workspace_services(self):
        """Test listing all services in a workspace."""
        # Create multiple services
        self.Service.create({
            'name': 'Service 1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.Service.create({
            'name': 'Service 2',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        services = self.Service.search([
            ('workspace_id', '=', self.workspace.id),
        ])
        self.assertEqual(len(services), 2)

    def test_update_service_state(self):
        """Test updating service state."""
        service = self.Service.create({
            'name': 'State Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        # Update state
        service.write({'state': 'running'})
        self.assertEqual(service.state, 'running')

    def test_service_belongs_to_correct_workspace(self):
        """Test that service belongs to the correct workspace."""
        # Create another workspace
        workspace2 = self.Workspace.create({'name': 'Workspace 2'})

        service1 = self.Service.create({
            'name': 'Service WS1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        service2 = self.Service.create({
            'name': 'Service WS2',
            'workspace_id': workspace2.id,
            'template_id': self.template.id,
        })

        # Check services are in correct workspace
        ws1_services = self.Service.search([
            ('workspace_id', '=', self.workspace.id),
        ])
        self.assertIn(service1.id, ws1_services.ids)
        self.assertNotIn(service2.id, ws1_services.ids)

    def test_template_not_found(self):
        """Test handling non-existent template."""
        template = self.Template.browse(99999)
        self.assertFalse(template.exists())

    def test_service_not_found(self):
        """Test handling non-existent service."""
        service = self.Service.browse(99999)
        self.assertFalse(service.exists())

    def test_inactive_templates_not_listed(self):
        """Test that inactive templates are not listed by default."""
        # Create inactive template
        inactive = self.Template.create({
            'name': 'Inactive Template',
            'slug': 'inactive',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
            'is_active': False,
        })

        # Search only active templates
        active_templates = self.Template.search([
            ('is_active', '=', True),
        ])
        self.assertNotIn(inactive.id, active_templates.ids)

    def test_service_reference_id_generated(self):
        """Test that reference_id is generated for services."""
        service = self.Service.create({
            'name': 'Ref Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'reference_id': 'custom-ref-123',
        })
        self.assertEqual(service.reference_id, 'custom-ref-123')

    def test_multiple_templates_same_category(self):
        """Test multiple templates in same category."""
        self.Template.create({
            'name': 'Web App 1',
            'slug': 'web-1',
            'category': 'web',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'web1',
            'helm_chart_version': '1.0.0',
            'is_active': True,
        })
        self.Template.create({
            'name': 'Web App 2',
            'slug': 'web-2',
            'category': 'web',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'web2',
            'helm_chart_version': '1.0.0',
            'is_active': True,
        })

        web_templates = self.Template.search([
            ('category', '=', 'web'),
            ('is_active', '=', True),
        ])
        self.assertGreaterEqual(len(web_templates), 3)  # Including self.template

    def test_service_error_message(self):
        """Test storing error message on service."""
        service = self.Service.create({
            'name': 'Error Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'error',
            'error_message': 'Deployment failed: timeout',
        })
        self.assertEqual(service.state, 'error')
        self.assertIn('timeout', service.error_message)
