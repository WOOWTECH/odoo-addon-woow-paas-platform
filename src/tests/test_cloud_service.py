"""Tests for Cloud Service model."""
from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


class TestCloudService(TransactionCase):
    """Test cases for woow_paas_platform.cloud_service model."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']

        # Create test workspace
        self.workspace = self.Workspace.create({'name': 'Test Workspace'})

        # Create test template
        self.template = self.Template.create({
            'name': 'Test Template',
            'slug': 'test',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test',
            'helm_chart_version': '1.0.0',
            'min_vcpu': 2,
            'min_ram_gb': 4.0,
            'min_storage_gb': 10,
        })

    def test_create_service_minimal(self):
        """Test creating a service with minimal fields."""
        service = self.Service.create({
            'name': 'My Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.assertEqual(service.name, 'My Service')
        self.assertEqual(service.state, 'pending')
        self.assertTrue(service.reference_id)
        self.assertEqual(service.workspace_id.id, self.workspace.id)
        self.assertEqual(service.template_id.id, self.template.id)

    def test_service_default_state(self):
        """Test that new services start in pending state."""
        service = self.Service.create({
            'name': 'Pending Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.assertEqual(service.state, 'pending')

    def test_service_state_transitions(self):
        """Test valid state transitions."""
        service = self.Service.create({
            'name': 'State Test',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        # pending → deploying
        service.write({'state': 'deploying'})
        self.assertEqual(service.state, 'deploying')

        # deploying → running
        service.write({'state': 'running'})
        self.assertEqual(service.state, 'running')

        # running → upgrading
        service.write({'state': 'upgrading'})
        self.assertEqual(service.state, 'upgrading')

        # upgrading → running
        service.write({'state': 'running'})
        self.assertEqual(service.state, 'running')

    def test_service_error_state(self):
        """Test setting error state with message."""
        service = self.Service.create({
            'name': 'Error Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        service.write({
            'state': 'error',
            'error_message': 'Deployment failed: connection timeout',
        })

        self.assertEqual(service.state, 'error')
        self.assertIn('timeout', service.error_message)

    def test_service_helm_configuration(self):
        """Test setting Helm configuration."""
        service = self.Service.create({
            'name': 'Helm Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_release_name': 'test-release',
            'helm_namespace': 'paas-ws-a1b2c3d4',
            'helm_values': '{"replicas": 2}',
            'helm_revision': 1,
        })

        self.assertEqual(service.helm_release_name, 'test-release')
        self.assertEqual(service.helm_namespace, 'paas-ws-1')
        self.assertEqual(service.helm_revision, 1)
        self.assertIn('replicas', service.helm_values)

    def test_service_network_configuration(self):
        """Test network configuration fields."""
        service = self.Service.create({
            'name': 'Network Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'subdomain': 'my-app',
            'custom_domain': 'myapp.example.com',
            'internal_port': 8080,
        })

        self.assertEqual(service.subdomain, 'my-app')
        self.assertEqual(service.custom_domain, 'myapp.example.com')
        self.assertEqual(service.internal_port, 8080)

    def test_service_resource_allocation(self):
        """Test resource allocation from template."""
        service = self.Service.create({
            'name': 'Resource Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'allocated_vcpu': self.template.min_vcpu,
            'allocated_ram_gb': self.template.min_ram_gb,
            'allocated_storage_gb': self.template.min_storage_gb,
        })

        self.assertEqual(service.allocated_vcpu, 2)
        self.assertEqual(service.allocated_ram_gb, 4.0)
        self.assertEqual(service.allocated_storage_gb, 10)

    def test_service_belongs_to_workspace(self):
        """Test that service is properly linked to workspace."""
        service = self.Service.create({
            'name': 'Workspace Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        self.assertEqual(service.workspace_id.id, self.workspace.id)
        self.assertIn(service.id, self.workspace.service_ids.ids)

    def test_service_delete_cascade(self):
        """Test that service is deleted when workspace is deleted."""
        service = self.Service.create({
            'name': 'Cascade Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        service_id = service.id
        self.workspace.unlink()

        # Service should be deleted
        self.assertFalse(self.Service.browse(service_id).exists())

    def test_multiple_services_per_workspace(self):
        """Test creating multiple services in same workspace."""
        service1 = self.Service.create({
            'name': 'Service 1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        service2 = self.Service.create({
            'name': 'Service 2',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })

        self.assertEqual(len(self.workspace.service_ids), 2)
        self.assertIn(service1.id, self.workspace.service_ids.ids)
        self.assertIn(service2.id, self.workspace.service_ids.ids)

    def test_service_timestamp_fields(self):
        """Test timestamp fields."""
        from datetime import datetime

        service = self.Service.create({
            'name': 'Timestamp Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'deployed_at': datetime.now(),
        })

        self.assertTrue(service.create_date)
        self.assertTrue(service.deployed_at)

    def test_service_upgrade_revision(self):
        """Test incrementing revision on upgrade."""
        service = self.Service.create({
            'name': 'Upgrade Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_revision': 1,
            'state': 'running',
        })

        # Simulate upgrade
        service.write({
            'helm_revision': 2,
            'state': 'upgrading',
        })

        self.assertEqual(service.helm_revision, 2)
        self.assertEqual(service.state, 'upgrading')

    def test_search_services_by_state(self):
        """Test searching services by state."""
        self.Service.create({
            'name': 'Running Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
        })
        self.Service.create({
            'name': 'Pending Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'pending',
        })

        running_services = self.Service.search([
            ('state', '=', 'running'),
            ('workspace_id', '=', self.workspace.id),
        ])
        self.assertGreaterEqual(len(running_services), 1)
        self.assertTrue(all(s.state == 'running' for s in running_services))

    def test_search_services_by_template(self):
        """Test searching services by template."""
        # Create another template
        template2 = self.Template.create({
            'name': 'Template 2',
            'slug': 'template-2',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'test2',
            'helm_chart_version': '1.0.0',
        })

        self.Service.create({
            'name': 'Service T1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        self.Service.create({
            'name': 'Service T2',
            'workspace_id': self.workspace.id,
            'template_id': template2.id,
        })

        services_t1 = self.Service.search([
            ('template_id', '=', self.template.id),
        ])
        self.assertGreaterEqual(len(services_t1), 1)
        self.assertTrue(all(s.template_id.id == self.template.id for s in services_t1))
