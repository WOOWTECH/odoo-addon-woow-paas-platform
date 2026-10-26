"""Isolation and outcome tests for self-service custom domains."""
import json
import uuid
from unittest.mock import MagicMock, patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import HttpCase, tagged

from ..hooks import _CONFIG_PARAMETERS
from ..models.cloud_service import _internal_provision
from ..models.custom_domain import normalize_customer_hostname


@tagged('post_install', '-at_install')
class TestCustomDomain(HttpCase):
    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.alice = self._user('alice', suffix)
        self.bob = self._user('bob', suffix)
        Workspace = self.env['woow_paas_platform.workspace']
        self.w1 = Workspace.with_user(self.alice).create({'name': 'Domain A ' + suffix})
        self.w2 = Workspace.with_user(self.bob).create({'name': 'Domain B ' + suffix})
        template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Odoo domain test', 'slug': 'odoo-domain-' + suffix,
            'helm_chart_name': 'woow-odoo', 'helm_chart_version': '1.0.0',
            'helm_repo_url': '',
            'workload_type': 'container', 'ingress_enabled': True,
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        self.s1 = _internal_provision(Service).create({
            'name': 'Odoo A', 'workspace_id': self.w1.id,
            'template_id': template.id, 'state': 'running',
            'subdomain': 'odoo-a-' + suffix,
            'helm_namespace': 'paas-ws-test-domain-a',
            'helm_release_name': 'odoo-a-' + suffix,
        })
        self.s2 = _internal_provision(Service).create({
            'name': 'Odoo B', 'workspace_id': self.w2.id,
            'template_id': template.id, 'state': 'running',
            'subdomain': 'odoo-b-' + suffix,
            'helm_namespace': 'paas-ws-test-domain-b',
            'helm_release_name': 'odoo-b-' + suffix,
        })
        self.Domain = self.env['woow_paas_platform.custom_domain']

    def _user(self, name, suffix):
        login = '%s-domain-%s' % (name, suffix)
        return self.env['res.users'].sudo().create({
            'name': name, 'login': login, 'password': login,
            'email': login + '@example.test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def _claim(self, service, hostname):
        return self.Domain.sudo().create({
            'service_id': service.id, 'hostname': hostname,
            'claim_hostname': hostname,
            'verification_token': 'proof-token-' + uuid.uuid4().hex,
            'origin_hostname': service.subdomain + '.woowtech.io',
            'cname_target': 'customers.woowtech.io',
        })

    def _post(self, service_id, action='list', **fields):
        body = json.dumps({'jsonrpc': '2.0', 'method': 'call',
                           'params': {'action': action, **fields}})
        response = self.url_open('/api/services/%s/domains' % service_id,
                                 data=body, headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        return response.json()['result']

    def test_normalization_rejects_apex_urls_and_provider_owned_names(self):
        self.assertEqual(normalize_customer_hostname('APP.Example.COM.'),
                         'app.example.com')
        for hostname in ('example.com', 'https://app.example.com',
                         '*.example.com', 'app.woowtech.io', '127.0.0.1',
                         'app.example.com:443'):
            with self.subTest(hostname=hostname), self.assertRaises(ValidationError):
                normalize_customer_hostname(hostname)

    def test_global_claim_conflict_and_private_model(self):
        self._claim(self.s1, 'booking.example.com')
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._claim(self.s2, 'booking.example.com')
        with self.assertRaises(AccessError):
            self.Domain.with_user(self.alice).create({
                'service_id': self.s1.id,
                'hostname': 'evil.example.com',
                'claim_hostname': 'evil.example.com',
                'verification_token': 'forged',
                'origin_hostname': 'elsewhere.woowtech.io',
                'cname_target': 'customers.woowtech.io',
                'state': 'active',
            })

    def test_http_membership_and_cross_service_isolation(self):
        claim = self._claim(self.s1, 'booking.example.com')
        self.authenticate(self.alice.login, self.alice.login)
        own = self._post(self.s1.id)
        self.assertTrue(own['success'])
        self.assertEqual([d['id'] for d in own['data']['domains']], [claim.id])
        self.assertNotIn('provider_id', own['data']['domains'][0])
        foreign_service = self._post(self.s2.id)
        self.assertFalse(foreign_service['success'])

        self.authenticate(self.bob.login, self.bob.login)
        self.assertFalse(self._post(self.s1.id)['success'])
        self.assertFalse(self._post(self.s2.id, action='remove', domain_id=claim.id)['success'])
        self.assertEqual(claim.sudo().state, 'pending_dns')

    def test_no_remote_create_without_dns_proof_and_feature_flag(self):
        claim = self._claim(self.s1, 'booking.example.com')
        with patch.object(type(claim), '_dns_txt_owned', return_value=False), \
                patch('odoo.addons.woow_paas_platform.models.custom_domain.get_paas_operator_client') as factory:
            self.Domain._cron_reconcile_custom_domains()
            factory.assert_not_called()  # disabled by default
            self.env['ir.config_parameter'].sudo().set_param(
                'woow_paas_platform.custom_domains_enabled', 'True')
            client = factory.return_value
            claim._reconcile_one(client)
            client.create_custom_domain.assert_not_called()
            self.assertEqual(claim.state, 'pending_dns')

    def test_config_parameter_is_registered_for_uninstall(self):
        self.assertIn('woow_paas_platform.custom_domains_enabled', _CONFIG_PARAMETERS)

    def test_service_teardown_blocks_live_claim_and_purges_removed_row(self):
        claim = self._claim(self.s1, 'booking.example.com')
        with self.assertRaises(UserError):
            self.s1._uninstall_release(None)
        with self.assertRaises(UserError):
            self.Domain._purge_removed_for_service(self.s1)
        claim.write({'state': 'removed', 'claim_hostname': False})
        self.Domain._purge_removed_for_service(self.s1)
        self.assertFalse(claim.exists())

    def test_access_protection_and_canonical_rename_cannot_bypass_gateway(self):
        self._claim(self.s1, 'booking.example.com')
        with self.assertRaises(UserError):
            self.s1.write({'is_protected': True})
        with self.assertRaises(UserError):
            self.s1.write({'subdomain': 'different-service'})
        self.assertFalse(self.s1.is_protected)

    def test_rollout_kill_switch_still_allows_provider_cleanup(self):
        claim = self._claim(self.s1, 'booking.example.com')
        claim.write({'state': 'removing', 'provider_id': 'provider_123'})
        with patch('odoo.addons.woow_paas_platform.models.custom_domain.get_paas_operator_client') as factory:
            self.Domain._cron_reconcile_custom_domains()
            factory.return_value.delete_custom_domain.assert_called_once_with(
                'provider_123', 'booking.example.com', claim.origin_hostname)
        self.assertEqual(claim.state, 'removed')
        self.assertFalse(claim.claim_hostname)

    def test_validation_precedes_customer_dns_cutover(self):
        claim = self._claim(self.s1, 'booking.example.com')
        claim.write({'state': 'verifying', 'provider_id': 'provider_123'})
        records = claim._public_dict()['dns_records']
        self.assertEqual(records[0]['purpose'], 'verify')
        self.assertEqual(records[-1]['purpose'], 'route')
        client = MagicMock()
        client.get_custom_domain.return_value = {
            'provider_id': 'provider_123',
            'hostname': claim.hostname,
            'origin_hostname': claim.origin_hostname,
            'hostname_status': 'active', 'ssl_status': 'active',
            'route_ready': True, 'validation_records': [],
        }
        with patch.object(type(claim), '_dns_cname_points_to_target', return_value=False):
            claim._reconcile_one(client)
        self.assertEqual(claim.state, 'waiting_cname')
        with patch.object(type(claim), '_dns_cname_points_to_target', return_value=True):
            claim._reconcile_one(client)
        self.assertEqual(claim.state, 'active')

    def test_provider_create_waits_until_gateway_loaded(self):
        claim = self._claim(self.s1, 'booking.example.com')
        client = MagicMock()
        client.custom_domain_gateway_route_ready.return_value = False
        with patch.object(type(claim), '_dns_txt_owned', return_value=True):
            claim._reconcile_one(client)
        self.assertEqual(claim.state, 'pending_dns')
        client.publish_custom_domain_route.assert_called_once_with(
            claim.hostname, claim.origin_hostname,
            self.s1.helm_namespace, self.s1.helm_release_name)
        client.create_custom_domain.assert_not_called()
