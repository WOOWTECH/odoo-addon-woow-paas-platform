"""Native Odoo ORM/controller regression using fake remote client only.

This is ordering/rollback evidence, not Cloudflare propagation evidence.
"""
from copy import deepcopy
from unittest.mock import patch
from urllib.parse import urlparse

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, HttpCase, tagged
from odoo.addons.woow_paas_platform.models.security_access import _internal_identity
from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError

CLIENT_PATH = ('odoo.addons.woow_paas_platform.models.security_access.'
               'get_paas_operator_client')


class FakeOperator:
    def __init__(self):
        self.events = []
        self.live_routes = {}
        self.fail_access = False
        self.fail_park = False
        self.malformed = False
        self.fail_at_prefix = None
        self.domain = 'woowtech.io'

    def sync_tunnel_routes(self, tunnel_id, routes, **kwargs):
        rows = deepcopy(routes)
        parked = all(row['service_url'] == 'http_status:503' for row in rows)
        self.events.append(('park' if parked else 'publish', rows))
        if parked and self.fail_park:
            raise PaaSOperatorError('fake parking unavailable', status_code=503)
        self.live_routes = {row['subdomain']: row['service_url'] for row in rows}
        return routes_receipt(tunnel_id, routes, domain=self.domain)

    def sync_access_app(self, payload):
        self.events.append(('access', deepcopy(payload)))
        if self.fail_access or self.fail_at_prefix == payload['app_name']:
            raise PaaSOperatorError('Selected Access login method unavailable',
                                    status_code=409)
        return {'app_id': ('fake-app' if payload['enabled'] and
                          not self.malformed else None),
                'policy_ids': (['fake-policy-%s' % index
                                for index, _policy in enumerate(
                                    payload.get('policies', []), 1)]
                               if payload['enabled'] else []),
                'allowed_idps': (['fake-idp-'+kind for kind in
                                  payload.get('login_idp_types', [])]
                                 if payload['enabled'] else []),
                'orphaned_policy_ids': [],
                'unresolved_idp_types': []}

    def create_tunnel(self, **kwargs):
        self.events.append(('create-tunnel', kwargs))
        return {'tunnel_id': 'fake-tunnel', 'tunnel_token': 'fake-token',
                'tunnel_name': kwargs['name']}

    def delete_tunnel(self, *args, **kwargs):
        self.events.append(('delete-tunnel', None))
        return {}


def forbid_external(original):
    def request(session, method, url, *args, **kwargs):
        host = urlparse(url).hostname
        if host not in ('127.0.0.1', 'localhost', '::1'):
            raise AssertionError('AC1011 blocked unexpected external HTTP')
        return original(session, method, url, *args, **kwargs)
    return request


@tagged('post_install', '-at_install')
class TestSecurityAccessFailClosedAC1011(TransactionCase):
    def setUp(self):
        super().setUp()
        import requests
        guard = patch('requests.sessions.Session.request',
                      new=forbid_external(requests.sessions.Session.request))
        guard.start()
        self.addCleanup(guard.stop)
        self.client = FakeOperator()
        self.client.domain = self.env['ir.config_parameter'].sudo().get_param('woow_paas_platform.paas_domain', 'woowtech.io')
        client_patch = patch(CLIENT_PATH, return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        self.workspace = self.env['woow_paas_platform.workspace'].create(
            {'name': 'AC1011 isolated security tests'})
        self.SA = self.env['woow_paas_platform.security_access']
        self.Route = self.env['woow_paas_platform.security_access_route']
        self.sa = self.SA.create({'name': 'AC1011 gateway',
                                 'workspace_id': self.workspace.id})
        self.route = self.Route.create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'ac1011-isolated-origin',
            'service_url': 'http://fake-origin.example.test:8123',
            'is_protected': True, 'login_google': True,
            'login_email_otp': False,
        })

    def activate(self, app=False):
        _internal_identity(self.sa).write({
            'state': 'active', 'tunnel_id': 'fake-tunnel',
            'tunnel_name': 'fake-tunnel-name'})
        if app:
            _internal_identity(self.route).access_app_id = 'fake-old-app'
        self.client.events.clear()
        self.client.live_routes = {
            self.route.subdomain_prefix: self.route.service_url}

    def assert_parked(self):
        self.assertEqual(self.client.live_routes[self.route.subdomain_prefix],
                         'http_status:503')

    def test_protected_rename_rejected_before_remote_or_db_mutation(self):
        self.activate(app=True)
        old = self.route.subdomain_prefix
        with self.assertRaisesRegex(UserError, 'Protected routes cannot be renamed'):
            self.route.write({'subdomain_prefix': 'ac1011-renamed'})
        self.assertEqual(self.route.subdomain_prefix, old)
        self.assertEqual(self.client.events, [])

    def test_enable_failure_rolls_back_intent_but_keeps_remote_parked(self):
        # Start unprotected, without creating an Access app during fixture change.
        self.route.write({'is_protected': False})
        self.activate()
        self.client.fail_access = True
        with self.assertRaises(PaaSOperatorError):
            self.route.write({'is_protected': True})
        self.route.invalidate_recordset()
        self.assertFalse(self.route.is_protected)
        self.assertFalse(self.route.access_app_id)
        self.assert_parked()

    def test_enable_success_establishes_app_before_release(self):
        self.route.write({'is_protected': False})
        self.activate()
        self.route.write({'is_protected': True})
        self.assertEqual(self.route.access_app_id, 'fake-app')
        labels = [event[0] for event in self.client.events]
        self.assertEqual(labels[-1], 'publish')
        self.assertTrue(all(label == 'park' for label in labels[:labels.index('access')]))
        self.assertEqual(self.client.live_routes[self.route.subdomain_prefix],
                         self.route.service_url)

    def test_existing_app_failure_preserves_identity_and_origin(self):
        self.activate(app=True)
        self.client.fail_access = True
        # A longer session only widens access: no revocation, so no park before the
        # failing call (a narrowing edit parks first: test_access_authorization_change_revoke).
        with self.assertRaises(PaaSOperatorError):
            self.route.write({'session_duration': '168h'})
        self.route.invalidate_recordset()
        self.assertEqual(self.route.access_app_id, 'fake-old-app')
        self.assertEqual([e[0] for e in self.client.events], ['access'])
        self.assertEqual(self.client.live_routes[self.route.subdomain_prefix],
                         self.route.service_url)

    def test_direct_lost_app_repair_failure_parks_before_access(self):
        self.activate()
        self.route.access_protection_lost = True
        self.client.fail_access = True
        with self.assertRaises(PaaSOperatorError):
            self.route._sync_access_to_operator()
        self.assertEqual([e[0] for e in self.client.events], ['park', 'access'])
        self.assert_parked()
        self.assertTrue(self.route.access_protection_lost)

    def test_direct_repair_park_failure_does_not_mutate_access(self):
        self.activate()
        self.route.access_protection_lost = True
        self.client.fail_park = True
        with self.assertRaises(PaaSOperatorError):
            self.route._sync_access_to_operator()
        self.assertEqual([e[0] for e in self.client.events], ['park'])
        self.assertFalse(self.route.access_app_id)

    def test_cleanup_park_failure_preserves_existing_app(self):
        self.activate(app=True)
        self.client.fail_park = True
        with self.assertRaises(PaaSOperatorError):
            self.sa._cleanup_cloudflare()
        self.assertEqual(self.route.access_app_id, 'fake-old-app')
        self.assertEqual([e[0] for e in self.client.events], ['park'])

    def test_malformed_access_success_cannot_release_origin(self):
        self.activate()
        self.client.malformed = True
        with self.assertRaisesRegex(UserError, 'protection was not established'):
            self.route._sync_access_to_operator()
        self.assert_parked()
        self.assertFalse(self.route.access_app_id)

    def test_actual_provision_failure_reports_error_and_keeps_origin_parked(self):
        self.client.fail_access = True
        self.sa.action_provision()
        self.assertEqual(self.sa.state, 'error')
        self.assertTrue(self.sa.tunnel_id)
        self.assert_parked()
        self.assertNotIn('publish', [e[0] for e in self.client.events])

    def test_actual_resume_failure_keeps_parked(self):
        self.activate(app=True)
        self.sa.write({'state': 'suspended', 'pre_suspend_state': 'active'})
        self.client.fail_access = True
        self.sa._do_resume()
        self.assertEqual(self.sa.state, 'error')
        self.assert_parked()
        self.assertNotIn('publish', [e[0] for e in self.client.events])

    def test_second_route_failure_does_not_release_first_route(self):
        second = self.Route.create({
            'security_access_id': self.sa.id,
            'subdomain_prefix': 'ac1011-second-isolated',
            'is_protected': True,
            'service_url': 'http://fake-origin.example.test:8124'})
        self.activate()
        self.client.fail_at_prefix = second.subdomain_prefix
        self.sa.write({'state': 'suspended', 'pre_suspend_state': 'active'})
        self.sa._do_resume()
        self.assertEqual(self.sa.state, 'error')
        self.assertTrue(self.route.access_app_id)
        self.assertEqual(set(self.client.live_routes.values()), {'http_status:503'})
        self.assertNotIn('publish', [e[0] for e in self.client.events])

    def test_create_failure_rolls_back_row_and_leaves_remote_new_route_blocked(self):
        self.activate(app=True)
        self.client.fail_access = True
        domain = [('subdomain_prefix', '=', 'ac1011-failed-new-route')]
        self.assertFalse(self.Route.search(domain))
        with self.assertRaises(PaaSOperatorError):
            self.Route.create({
                'security_access_id': self.sa.id,
                'subdomain_prefix': 'ac1011-failed-new-route',
                'is_protected': True,
                'service_url': 'http://fake-origin.example.test:9999'})
        self.assertFalse(self.Route.search(domain))
        self.assertEqual(self.client.live_routes['ac1011-failed-new-route'],
                         'http_status:503')


@tagged('post_install', '-at_install')
class TestSecurityAccessFailClosedHTTPAC1011(HttpCase):
    def test_owner_provision_remote_failure_returns_truthful_error(self):
        import json
        import uuid
        import requests
        suffix = uuid.uuid4().hex[:7]
        login = 'ac1011_http_' + suffix
        user = self.env['res.users'].sudo().create({
            'name': 'AC1011 synthetic owner', 'login': login,
            'password': login, 'email': login + '@example.test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'AC1011 HTTP isolated WS', 'owner_id': user.id})
        sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'AC1011 HTTP SA', 'workspace_id': workspace.id})
        self.env['woow_paas_platform.security_access_route'].sudo().create({
            'security_access_id': sa.id, 'subdomain_prefix': 'ac1011-http-' + suffix,
            'is_protected': True, 'service_url': 'http://fake-origin.example.test:80'})
        client = FakeOperator()
        client.fail_access = True
        self.authenticate(login, login)
        with patch(CLIENT_PATH, return_value=client), patch(
                'requests.sessions.Session.request',
                new=forbid_external(requests.sessions.Session.request)):
            response = self.url_open(
                f'/api/workspaces/{workspace.id}/security-accesses/{sa.id}',
                data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                                 'params': {'action': 'provision'}}),
                headers={'Content-Type': 'application/json'})
        body = response.json()
        self.assertNotIn('error', body)
        result = body['result']
        self.assertFalse(result['success'])
        self.assertEqual(result['data']['state'], 'error')
        self.assertTrue(result['error'])
        self.assertEqual(set(client.live_routes.values()), {'http_status:503'})

from .publication_receipt_fixture import routes_receipt
