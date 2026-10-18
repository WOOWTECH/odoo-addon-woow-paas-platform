"""Exercise documentation/log authorization over real HTTP, across tenants."""
import json
import uuid
from unittest.mock import Mock, patch

from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorTimeoutError

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'


@tagged('post_install', '-at_install')
class TestServiceDocumentationLogs(HttpCase):
    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]

        def user(role, portal=False):
            login = 'service_docs_%s_%s' % (role, suffix)
            return self.env['res.users'].sudo().with_context(no_reset_password=True).create({
                'name': login, 'login': login, 'password': login,
                'email': login + '@test.example',
                'groups_id': [(6, 0, [self.env.ref(
                    'base.group_portal' if portal else 'base.group_user').id])],
            })

        self.owner = user('owner')
        self.admin = user('admin', portal=True)
        self.member = user('member')
        self.guest = user('guest', portal=True)
        self.outsider = user('outsider')
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        self.workspace = Workspace.create({'name': 'Service docs ' + suffix,
                                           'owner_id': self.owner.id})
        self.foreign_workspace = Workspace.create({'name': 'Foreign docs ' + suffix,
                                                   'owner_id': self.outsider.id})
        for member, role in ((self.admin, 'admin'), (self.member, 'user'), (self.guest, 'guest')):
            self.env['woow_paas_platform.workspace_access'].sudo().create({
                'workspace_id': self.workspace.id, 'user_id': member.id, 'role': role,
            })
        self.template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Documentation example', 'slug': 'docs-' + suffix,
            'helm_repo_url': '', 'helm_chart_name': 'oci://example.com/docs',
            'helm_chart_version': '1.0.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
            'description': 'Short introduction', 'full_description': 'Marketplace introduction',
            'documentation': '# Getting started\n\nService usage guide.',
            'documentation_url': 'https://example.com/guide',
        })

        def service(workspace, name):
            return _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': name, 'workspace_id': workspace.id, 'template_id': self.template.id,
                'state': 'running', 'helm_namespace': 'paas-ws-docs-' + suffix,
                'helm_release_name': 'svc-' + name + '-' + suffix,
                'helm_values': '{}',
            })

        self.service = service(self.workspace, 'own')
        self.foreign_service = service(self.foreign_workspace, 'foreign')
        self.client = Mock()
        self.client.get_release_logs.return_value = {
            'available': True, 'reason': None, 'pod': 'svc-own-pod', 'container': 'app',
            'sources': [{'pod': 'svc-own-pod', 'containers': ['app']}],
            'text': '2026-10-18T10:00:00Z ready\n', 'truncated': False,
        }

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _post(self, endpoint, service=None, workspace=None, **params):
        route = '/api/workspaces/%s/services/%s/%s' % (
            (workspace or self.workspace).id, (service or self.service).id, endpoint)
        response = self.url_open(route, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params,
        }), headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        body = response.json()
        self.assertNotIn('error', body, body)
        return body['result']

    def test_documentation_is_member_readable_with_dedicated_content(self):
        for user in (self.owner, self.admin, self.member, self.guest):
            self._login(user)
            with patch(_CONTROLLER + '.get_paas_operator_client') as factory:
                result = self._post('documentation')
            factory.assert_not_called()
            self.assertTrue(result['success'])
            self.assertEqual(result['data']['content'], self.template.documentation)
            self.assertEqual(result['data']['documentation_url'], 'https://example.com/guide')

    def test_documentation_fallback_and_unsafe_external_url(self):
        self._login(self.owner)
        self.template.write({'documentation': False, 'documentation_url': 'javascript:alert(1)'})
        result = self._post('documentation')
        self.assertEqual(result['data']['content'], 'Marketplace introduction')
        self.assertEqual(result['data']['documentation_url'], '')
        self.template.write({'full_description': False})
        self.assertEqual(self._post('documentation')['data']['content'], 'Short introduction')
        self.template.write({'description': False})
        self.assertEqual(self._post('documentation')['data']['content'], '')

    def test_only_owner_and_admin_can_read_logs_without_mutation(self):
        before = self.service.read(['state', 'helm_values', 'write_date'])[0]
        for user in (self.owner, self.admin):
            self._login(user)
            with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client):
                result = self._post('logs', pod='svc-own-pod', container='app',
                                    tail_lines=100, previous=True)
            self.assertTrue(result['success'])
            self.assertEqual(result['data']['text'], self.client.get_release_logs.return_value['text'])
            self.client.get_release_logs.assert_called_with(
                namespace=self.service.helm_namespace, release_name=self.service.helm_release_name,
                pod='svc-own-pod', container='app', tail_lines=100, previous=True)
        self.assertEqual(self.service.read(['state', 'helm_values', 'write_date'])[0], before)
        for user in (self.member, self.guest):
            self._login(user)
            with patch(_CONTROLLER + '.get_paas_operator_client') as factory:
                result = self._post('logs')
            self.assertFalse(result['success'])
            factory.assert_not_called()

    def test_cross_tenant_mismatch_and_concealed_service_are_denied(self):
        self._login(self.outsider)
        with patch(_CONTROLLER + '.get_paas_operator_client') as factory:
            for endpoint in ('documentation', 'logs'):
                self.assertFalse(self._post(endpoint)['success'])
        factory.assert_not_called()
        self._login(self.owner)
        with patch(_CONTROLLER + '.get_paas_operator_client') as factory:
            for endpoint in ('documentation', 'logs'):
                self.assertFalse(self._post(endpoint, service=self.foreign_service)['success'])
            self.template.write({'is_hidden': True, 'conceal_instances': True})
            for endpoint in ('documentation', 'logs'):
                self.assertFalse(self._post(endpoint)['success'])
        factory.assert_not_called()

    def test_invalid_log_parameters_do_not_reach_operator(self):
        self._login(self.owner)
        with patch(_CONTROLLER + '.get_paas_operator_client') as factory:
            for params in ({'tail_lines': 2001}, {'tail_lines': True}, {'tail_lines': -1},
                           {'tail_lines': '500'}, {'previous': 'false'}, {'pod': '--all'},
                           {'container': '../other'}, {'namespace': 'other-tenant'},
                           {'release_name': 'foreign'}, {'pod': []}):
                self.assertFalse(self._post('logs', **params)['success'], params)
        factory.assert_not_called()

    def test_operator_error_does_not_leak_transport_details(self):
        self._login(self.owner)
        self.client.get_release_logs.side_effect = PaaSOperatorTimeoutError('private-url secret-key')
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client):
            result = self._post('logs')
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['reason'], 'operator_unavailable')
        self.assertNotIn('secret-key', json.dumps(result))
        self.assertNotIn('private-url', json.dumps(result))

    def test_error_state_remains_readable_and_undeployed_is_explicit(self):
        self._login(self.owner)
        self.service.write({'state': 'error'})
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client):
            self.assertTrue(self._post('logs')['data']['available'])
        self.service.write({'helm_release_name': False})
        with patch(_CONTROLLER + '.get_paas_operator_client') as factory:
            self.assertEqual(self._post('logs')['data']['reason'], 'not_deployed')
        factory.assert_not_called()
