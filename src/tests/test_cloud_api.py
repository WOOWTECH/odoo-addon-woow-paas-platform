"""Tests for Cloud API endpoints."""
import json
from unittest.mock import patch, MagicMock

from odoo.tests.common import HttpCase, TransactionCase
from ..models.cloud_service import _internal_provision


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
        service = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
            'name': 'Detail Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'helm_namespace': 'paas-ws-a1b2c3d4',
            'helm_release_name': 'test-release',
        })

        retrieved = self.Service.browse(service.id)
        self.assertTrue(retrieved.exists())
        self.assertEqual(retrieved.name, 'Detail Service')
        self.assertEqual(retrieved.helm_namespace, 'paas-ws-a1b2c3d4')

    def test_list_workspace_services(self):
        """Test listing all services in a workspace."""
        # Create multiple services
        _internal_provision(self.Service).create({
            'name': 'Service 1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
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

        service1 = _internal_provision(self.Service).create({
            'name': 'Service WS1',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
        })
        service2 = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
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
        service = _internal_provision(self.Service).create({
            'name': 'Error Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'error',
            'error_message': 'Deployment failed: timeout',
        })
        self.assertEqual(service.state, 'error')
        self.assertIn('timeout', service.error_message)


class TestCloudServiceController(TransactionCase):
    """Test cases for cloud service controller logic."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.user = self.env.ref('base.user_admin')
        self.Template = self.env['woow_paas_platform.cloud_app_template']
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.Service = self.env['woow_paas_platform.cloud_service']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']

        # Create template with value specs
        self.template_with_specs = self.Template.create({
            'name': 'Template With Specs',
            'slug': 'spec-template',
            'category': 'web',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'spec-chart',
            'helm_chart_version': '1.0.0',
            'description': 'Template with value specs',
            'is_active': True,
            'helm_default_values': json.dumps({'replicas': 1, 'port': 8080}),
            'helm_value_specs': json.dumps({
                'required': [
                    {'key': 'replicas', 'type': 'integer', 'label': 'Replicas'}
                ],
                'optional': [
                    {'key': 'port', 'type': 'integer', 'label': 'Port'}
                ]
            }),
        })

        # Create workspace owned by admin
        self.workspace = self.Workspace.sudo().with_user(self.user).create({
            'name': 'Controller Test Workspace',
        })

    def test_helm_values_filtering(self):
        """Test that only allowed Helm values are passed through."""
        # Import the controller to test the helper method
        from ..controllers.paas import PaasController

        controller = PaasController()

        # Values with allowed and disallowed keys
        user_values = {
            'replicas': 3,            # allowed
            'port': 9000,             # allowed
            'malicious_key': 'hack',  # NOT allowed
            'image': 'evil:latest',   # NOT allowed
        }

        filtered, rejected = controller._filter_allowed_helm_values(
            user_values,
            self.template_with_specs,
        )

        # Only allowed keys should remain
        self.assertIn('replicas', filtered)
        self.assertIn('port', filtered)
        self.assertNotIn('malicious_key', filtered)
        self.assertNotIn('image', filtered)
        self.assertEqual(filtered['replicas'], 3)
        self.assertEqual(filtered['port'], 9000)
        # Disallowed keys are reported back so the caller can warn the user.
        self.assertIn('malicious_key', rejected)
        self.assertIn('image', rejected)

    def test_helm_values_filtering_no_specs(self):
        """Test that all values pass through when no specs defined."""
        from ..controllers.paas import PaasController

        controller = PaasController()

        # Template without specs
        template_no_specs = self.Template.create({
            'name': 'No Specs Template',
            'slug': 'no-specs',
            'category': 'web',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'no-specs',
            'helm_chart_version': '1.0.0',
            'is_active': True,
        })

        user_values = {
            'anything': 'goes',
            'any_key': 'any_value',
        }

        filtered, rejected = controller._filter_allowed_helm_values(
            user_values,
            template_no_specs,
        )

        # All values should pass through, nothing rejected
        self.assertEqual(filtered, user_values)
        self.assertEqual(rejected, [])

    def test_helm_values_filtering_empty_values(self):
        """Test filtering with empty values."""
        from ..controllers.paas import PaasController

        controller = PaasController()

        filtered, rejected = controller._filter_allowed_helm_values(
            {},
            self.template_with_specs,
        )

        self.assertEqual(filtered, {})
        self.assertEqual(rejected, [])

    def test_helm_values_filtering_none_values(self):
        """Test filtering with None values."""
        from ..controllers.paas import PaasController

        controller = PaasController()

        filtered, rejected = controller._filter_allowed_helm_values(
            None,
            self.template_with_specs,
        )

        self.assertEqual(filtered, {})
        self.assertEqual(rejected, [])

    def test_service_subdomain_uniqueness(self):
        """Test that subdomain uniqueness is enforced."""
        # Create first service with subdomain
        service1 = _internal_provision(self.Service).create({
            'name': 'Service 1',
            'workspace_id': self.workspace.id,
            'template_id': self.template_with_specs.id,
            'subdomain': 'unique-subdomain',
        })

        self.assertEqual(service1.subdomain, 'unique-subdomain')

        # Try to create another service with same subdomain
        with self.assertRaises(Exception):  # Should raise IntegrityError
            _internal_provision(self.Service).create({
                'name': 'Service 2',
                'workspace_id': self.workspace.id,
                'template_id': self.template_with_specs.id,
                'subdomain': 'unique-subdomain',
            })

    def test_service_reference_id_uniqueness(self):
        """Test that reference_id uniqueness is enforced."""
        # Create first service with reference_id
        service1 = _internal_provision(self.Service).create({
            'name': 'Service 1',
            'workspace_id': self.workspace.id,
            'template_id': self.template_with_specs.id,
            'reference_id': 'unique-ref-123',
        })

        self.assertEqual(service1.reference_id, 'unique-ref-123')

        # Try to create another service with same reference_id
        with self.assertRaises(Exception):  # Should raise IntegrityError
            _internal_provision(self.Service).create({
                'name': 'Service 2',
                'workspace_id': self.workspace.id,
                'template_id': self.template_with_specs.id,
                'reference_id': 'unique-ref-123',
            })

    def test_service_state_transitions(self):
        """Test valid service state transitions."""
        service = _internal_provision(self.Service).create({
            'name': 'State Test Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template_with_specs.id,
            'state': 'pending',
        })

        # pending -> deploying
        service.write({'state': 'deploying'})
        self.assertEqual(service.state, 'deploying')

        # deploying -> running
        service.write({'state': 'running'})
        self.assertEqual(service.state, 'running')

        # running -> upgrading
        service.write({'state': 'upgrading'})
        self.assertEqual(service.state, 'upgrading')

        # upgrading -> running
        service.write({'state': 'running'})
        self.assertEqual(service.state, 'running')

        # running -> error
        service.write({'state': 'error'})
        self.assertEqual(service.state, 'error')

    def test_service_helm_revision_tracking(self):
        """Test that helm_revision is properly tracked."""
        service = _internal_provision(self.Service).create({
            'name': 'Revision Test Service',
            'workspace_id': self.workspace.id,
            'template_id': self.template_with_specs.id,
            'helm_revision': 1,
        })

        self.assertEqual(service.helm_revision, 1)

        # Simulate upgrade
        service.write({'helm_revision': 2})
        self.assertEqual(service.helm_revision, 2)

        # Simulate another upgrade
        service.write({'helm_revision': 3})
        self.assertEqual(service.helm_revision, 3)


class TestSeedTemplateHelmValueSpecs(TransactionCase):
    """Regression: seeded CloudAppTemplate JSON fields must be strict-valid JSON so
    the frontend 'Application Settings' section renders and template fetch never 500s.

    Root cause (fix/n8n-creation-issue): the n8n seed wrapped a long label /
    description across XML lines, embedding a literal newline INSIDE a JSON string
    value. Strict ``json.loads`` rejects control characters in strings, so
    ``_parse_helm_value_specs`` swallowed the error and returned ``{}`` -> the entire
    Application Settings card (including the required Admin Password field) disappeared
    -> the backend still enforced ``post_deploy_init_type == 'n8n'`` password rules ->
    every launch failed with 'Password must be at least 8 characters' for a field the
    user could not even see. Net effect: n8n could never be created.
    """

    def setUp(self):
        super().setUp()
        self.Template = self.env['woow_paas_platform.cloud_app_template']

    def test_all_seeded_json_fields_are_strict_valid(self):
        """Every seeded template's JSON-in-XML fields must parse under strict JSON (no
        literal newlines split across XML lines inside strings). Covers every field
        that is consumed by an unguarded / display json.loads on a hot endpoint path:
        helm_value_specs, helm_default_values AND tags (tags 500s the whole marketplace
        listing if malformed, paas.py:822)."""
        templates = self.Template.search([])
        # Guard against a vacuous pass if seed loading ever regresses.
        self.assertTrue(templates, "no cloud_app_template seeds loaded — test would pass vacuously")
        broken = []
        for tmpl in templates:
            for field_name in ('helm_value_specs', 'helm_default_values', 'tags'):
                raw = tmpl[field_name]
                if not raw:
                    continue
                try:
                    json.loads(raw)  # strict=True: rejects control chars in strings
                except (json.JSONDecodeError, TypeError):
                    broken.append('%s.%s' % (tmpl.slug or tmpl.name, field_name))
        self.assertEqual(
            broken, [],
            "Seeded template JSON must be strict-valid (a wrapped long string value "
            "embeds a newline and breaks json.loads). Broken: %s" % broken,
        )

    def test_n8n_seed_exposes_admin_credential_fields(self):
        """The active n8n template must surface the _init.owner_* required fields
        through the exact code path the configure page consumes."""
        from ..controllers.paas import PaasController
        controller = PaasController()
        n8n = self.env.ref('woow_paas_platform.cloud_app_n8n')

        specs = controller._parse_helm_value_specs(n8n)
        required_keys = {f['key'] for f in specs.get('required', [])}
        self.assertIn('_init.owner_email', required_keys)
        self.assertIn('_init.owner_password', required_keys)

        # Endpoint-level guard: the payload the frontend actually receives must carry a
        # non-empty required list, otherwise the Application Settings card is hidden.
        formatted = controller._format_template(n8n, include_values=True)
        self.assertTrue(
            formatted['helm_value_specs'].get('required'),
            "n8n template must expose required helm_value_specs to the configure page",
        )

        # Coupling guard: the frontend required password field only makes sense while
        # the backend still enforces the n8n init password. If post_deploy_init_type
        # drifts away from 'n8n' the required field would have no consumer (or vice
        # versa the backend would validate a field the UI never renders — the original
        # bug's shape). Keep the two ends coupled.
        self.assertEqual(n8n.post_deploy_init_type, 'n8n')

    def test_parse_tolerates_embedded_newline_fallback(self):
        """Defense-in-depth: a stray newline inside a string value must degrade to a
        tolerant parse (strict=False) and still surface the required keys, never {}."""
        from ..controllers.paas import PaasController
        controller = PaasController()
        tmpl = self.Template.create({
            'name': 'Newline Specs',
            'slug': 'newline-specs',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'x',
            'helm_chart_version': '1.0.0',
            # Literal newline embedded INSIDE the label string value.
            'helm_value_specs': '{"required": [{"key": "k", "label": "Broken\nLabel"}], "optional": []}',
        })
        specs = controller._parse_helm_value_specs(tmpl)
        self.assertEqual(
            [f['key'] for f in specs.get('required', [])], ['k'],
            "parser must recover required keys from control-char JSON, not return {}",
        )

    def test_heal_sticky_helm_value_specs_cleans_dirty_row(self):
        """The upgrade healer must rewrite a control-char-dirty helm_value_specs into
        clean strict JSON in place (so DBs where the sticky noupdate flag skipped the
        seed correction converge to the fixed data and stop logging fallback errors)."""
        tmpl = self.Template.create({
            'name': 'Dirty Specs',
            'slug': 'dirty-specs',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'x',
            'helm_chart_version': '1.0.0',
            # Embedded newline + indent inside the label string value (the seed bug shape).
            'helm_value_specs': '{"required": [{"key": "k", "label": "n8n\n                Admin Email"}], "optional": []}',
        })
        # Precondition: strict parse fails on the dirty row.
        with self.assertRaises(json.JSONDecodeError):
            json.loads(tmpl.helm_value_specs)

        self.Template._heal_sticky_helm_value_specs()

        # Now strict-valid and the label whitespace collapsed to a single space.
        healed = json.loads(tmpl.helm_value_specs)  # must not raise
        self.assertEqual(healed['required'][0]['label'], 'n8n Admin Email')
        self.assertNotIn('\n', tmpl.helm_value_specs)

    def test_heal_clears_sticky_noupdate_flag(self):
        """The healer's other half: it must flip a sticky ir.model.data.noupdate=True
        on a seeded cloud_app_template xmlid to False, so a DB installed under the
        historical noupdate=1 block stops silently skipping seed corrections on -u."""
        tmpl = self.Template.create({
            'name': 'Sticky Template',
            'slug': 'sticky-template',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'x',
            'helm_chart_version': '1.0.0',
        })
        imd = self.env['ir.model.data'].create({
            'module': 'woow_paas_platform',
            'name': 'sticky_test_cloud_app_template',
            'model': 'woow_paas_platform.cloud_app_template',
            'res_id': tmpl.id,
            'noupdate': True,
        })
        self.assertTrue(imd.noupdate)

        self.Template._heal_sticky_helm_value_specs()

        imd.invalidate_recordset()
        self.assertFalse(imd.noupdate, "healer must clear the sticky noupdate flag")


class TestWorkspaceAPI(TransactionCase):
    """Test cases for workspace API operations."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.user = self.env.ref('base.user_admin')
        self.Workspace = self.env['woow_paas_platform.workspace']
        self.WorkspaceAccess = self.env['woow_paas_platform.workspace_access']

    def test_workspace_creation_creates_owner_access(self):
        """Test that creating workspace automatically creates owner access."""
        workspace = self.Workspace.sudo().with_user(self.user).create({
            'name': 'New Workspace',
        })

        # Check owner access was created
        access = self.WorkspaceAccess.search([
            ('workspace_id', '=', workspace.id),
            ('user_id', '=', self.user.id),
        ])

        self.assertEqual(len(access), 1)
        self.assertEqual(access.role, 'owner')

    def test_workspace_member_count(self):
        """Test workspace member count computed field."""
        workspace = self.Workspace.sudo().with_user(self.user).create({
            'name': 'Member Count Test',
        })

        # Initially should have 1 member (owner)
        self.assertEqual(workspace.member_count, 1)

        # Create a test user instead of using demo user
        test_user = self.env['res.users'].create({
            'name': 'Test Member User',
            'login': 'test_member_user',
            'email': 'test_member@example.com',
        })

        # Add as a member
        self.WorkspaceAccess.create({
            'workspace_id': workspace.id,
            'user_id': test_user.id,
            'role': 'user',
        })

        # Should now have 2 members
        self.assertEqual(workspace.member_count, 2)

    def test_workspace_archive(self):
        """Test workspace archiving."""
        workspace = self.Workspace.sudo().with_user(self.user).create({
            'name': 'Archive Test',
        })

        self.assertEqual(workspace.state, 'active')

        workspace.action_archive()

        self.assertEqual(workspace.state, 'archived')
