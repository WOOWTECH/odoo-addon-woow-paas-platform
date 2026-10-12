"""Native caller -> real operator source -> persistent fake CF -> caller retry.

Known publication failures retain ORM anchors. This uses the original real
IdP-fail-closed operator contract, not a fictitious idempotent ownership API.
Unexpected outer transaction rollback/crash recovery remains a separate gate.
"""
from copy import deepcopy
import json
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _cs_skip_access_sync
from ..models.smart_home_route import _sm_skip_access_sync
from ..models.security_access import (
    _internal_identity as _sa_identity, _skip_route_sync)
from ..services.paas_operator import PaaSOperatorError
from .test_cs_sh_fail_closed_ac1011 import Fixture, FakeOperator, CLIENT_PATHS
from .native_operator_transport import load_cloudflare, ActualCloudflareContract


SA_CLIENT = ('odoo.addons.woow_paas_platform.models.security_access.'
             'get_paas_operator_client')


class RealSourceBridge(FakeOperator):
    def __init__(self):
        super().__init__()
        self.cf = load_cloudflare()
        self.contract = ActualCloudflareContract(self.cf)
        self.bindings = {}

    def register_resource(self, anchor):
        payload = anchor._build_access_payload()
        key = (payload['canonical_name'], payload['primary_hostname'])
        self.bindings[key] = anchor

    def sync_access_app(self, payload):
        data = deepcopy(payload)
        self.events.append(('access', data))
        key = (data['canonical_name'], data['primary_hostname'])
        anchor = self.bindings.get(key)
        if anchor is None:
            raise AssertionError('Unregistered native resource')
        # Stable ORM policy row key is harmless metadata on this fixed operator
        # contract. This fixture provides no ownership verifier or signing key.
        for desired, policy in zip(data['policies'],
                                   anchor.access_policy_ids.sorted(
                                       key=lambda p: (p.sequence, p.id))):
            desired['resource_policy_key'] = str(policy.id)
        try:
            result = self.contract.sync(data)
        except self.cf.CloudflareException as error:
            raise PaaSOperatorError(error.message,
                                    status_code=error.status_code) from error
        self.app = bool(result['app_id'])
        return result


@tagged('post_install', '-at_install')
class TestActualOperatorPublishRecoveryAC1011(Fixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self.install_fake()
        self.resources()
        self.client = RealSourceBridge()
        for path in CLIENT_PATHS + (SA_CLIENT,):
            stub = patch(path, return_value=self.client)
            stub.start()
            self.addCleanup(stub.stop)

    def register(self, anchor, kind, tunnel_id, tunnel_name,
                 namespace=None, release=None):
        self.client.register_resource(anchor)
        # A real policy row exercises policy identity rollback/reuse too.
        Policy = self.env['woow_paas_platform.security_access_access_policy']
        field = {'cloud_service': 'cloud_service_id',
                 'smart_home_route': 'smart_home_route_id',
                 'security_access_route': 'route_id'}[kind]
        _skip_route_sync(Policy).create({
            field: anchor.id, 'name': 'Native recovery known user',
            'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                                  'rule_type': 'email_list',
                                  'rule_value': 'owner@example.test'})],
        })

    def assert_one_app_and_policy(self, anchor):
        account = self.client.contract.account
        self.assertEqual(account.next_app, 1,
                         'Retry POSTed a duplicate remote Access app')
        self.assertFalse(anchor.access_publish_pending)
        self.assertTrue(anchor.access_app_id)
        self.assertTrue(anchor.access_policy_ids.cf_policy_id)
        self.assertEqual(set(account.apps), {anchor.access_app_id})
        self.assertEqual(set(account.policies),
                         set(anchor.access_policy_ids.mapped('cf_policy_id')))

    def assert_remote_cleanup(self):
        self.assertFalse(self.client.contract.account.apps)
        self.assertFalse(self.client.contract.account.policies)

    def test_cs_late_unpark_failure_retry_reuses_remote_identity_then_cleanup(self):
        _cs_skip_access_sync(self.cs).is_protected = False
        self.register(self.cs, 'cloud_service',
                      'native-isolated-shared-tunnel', 'native-shared-tunnel',
                      self.cs.helm_namespace, self.cs.helm_release_name)
        self.client.origin = True
        self.client.fail_unpark = True
        self.assertTrue(self.cs.write({'is_protected': True}))
        self.assertTrue(self.cs.access_publish_pending)
        self.assertTrue(self.cs.access_app_id)
        self.assertTrue(self.cs.is_protected)
        self.cs.invalidate_recordset()
        self.assertFalse(self.client.origin)
        self.assertEqual(self.client.contract.account.next_app, 1)
        self.client.fail_unpark = False
        self.cs.write({'is_protected': True})
        self.assert_one_app_and_policy(self.cs)
        self.cs._do_cleanup()
        self.assert_remote_cleanup()

    def test_sh_late_publish_failure_retry_reuses_remote_identity_then_cleanup(self):
        self.sh_active()
        _sm_skip_access_sync(self.route).is_protected = False
        self.register(self.route, 'smart_home_route', self.sh.tunnel_id,
                      self.sh.tunnel_name)
        self.client.fail_publish = True
        self.assertTrue(self.route.write({'is_protected': True}))
        self.assertTrue(self.route.access_publish_pending)
        self.assertTrue(self.route.access_app_id)
        self.assertTrue(self.route.is_protected)
        self.route.invalidate_recordset()
        self.assertFalse(self.client.origin)
        self.assertEqual(self.client.contract.account.next_app, 1)
        self.client.fail_publish = False
        self.route.write({'is_protected': True})
        self.assert_one_app_and_policy(self.route)
        self.sh._teardown_routes_access()
        self.assert_remote_cleanup()

    def test_sa_late_publish_failure_retry_reuses_remote_identity_then_cleanup(self):
        sa = self.env['woow_paas_platform.security_access'].create({
            'name': 'Native source recovery SA',
            'workspace_id': self.workspace.id})
        route = _skip_route_sync(
            self.env['woow_paas_platform.security_access_route']).create({
                'security_access_id': sa.id,
                'subdomain_prefix': 'ac1011-recovery-own-sa',
                'service_url': 'http://own-native-origin.example.test:80',
                'is_protected': False, 'login_google': True,
                'login_email_otp': False,
            }).with_env(self.env)
        _sa_identity(sa).write({'state': 'active',
                                'tunnel_id': 'native-own-sa-tunnel',
                                'tunnel_name': 'native-own-sa-tunnel-name'})
        self.register(route, 'security_access_route', sa.tunnel_id,
                      sa.tunnel_name)
        self.client.origin = True
        self.client.fail_publish = True
        self.assertTrue(route.write({'is_protected': True}))
        self.assertTrue(route.access_publish_pending)
        self.assertTrue(route.access_app_id)
        self.assertTrue(route.is_protected)
        route.invalidate_recordset()
        self.assertFalse(self.client.origin)
        self.assertEqual(self.client.contract.account.next_app, 1)
        self.client.fail_publish = False
        route.write({'is_protected': True})
        self.assert_one_app_and_policy(route)
        sa._cleanup_cloudflare()
        self.assert_remote_cleanup()

    def test_missing_selected_idp_400_preserves_existing_remote_identity(self):
        payload = self.cs._build_access_payload()
        payload['enabled'] = True
        payload['login_idp_types'] = ['google']
        first = self.client.contract.sync(payload)
        self.assertTrue(first['app_id'])
        account = self.client.contract.account
        before = (deepcopy(account.apps), deepcopy(account.policies))
        call_offset = len(account.calls)
        payload['existing_app_id'] = first['app_id']
        payload['existing_policy_ids'] = first['policy_ids']
        payload['login_idp_types'] = ['google', 'onetimepin']
        with self.assertRaises(self.client.cf.CloudflareException) as raised:
            self.client.contract.sync(payload)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual((account.apps, account.policies), before)
        self.assertTrue(account.calls[call_offset:])
        self.assertTrue(all(method == 'GET'
                            for method, _, _ in account.calls[call_offset:]))
