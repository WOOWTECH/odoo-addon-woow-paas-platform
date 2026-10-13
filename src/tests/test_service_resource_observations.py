"""Live resources are viewer-readable observations, never capacity/billing writes.

Use real HTTP requests as internal guests and portal members. Mock only the
operator boundary: superuser-only direct controller tests miss workspace ACLs.
"""
import copy
import json
import threading
import uuid
from concurrent.futures import wait as futures_wait
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorClient, PaaSOperatorTimeoutError

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_SNAPSHOT_FIELDS = [
    'state', 'allocated_vcpu', 'allocated_ram_gb', 'allocated_storage_gb',
    'helm_values', 'helm_revision', 'last_release_sync_at', 'observed_at',
    'subscription_id', 'write_date',
]
_SAMPLE_AT = '2026-10-13T01:45:36Z'
_OBSERVATION = {
    'status': 'partial', 'workload_type': 'container', 'resource_scope': 'live_pods',
    'cpu': {'usage': 0, 'request': 16, 'limit': 32, 'limit_status': 'bounded',
            'unit': 'cores', 'status': 'ok', 'sampled_at': _SAMPLE_AT,
            'window_seconds': 120, 'coverage': {'observed': 2, 'expected': 2}},
    'memory': {'usage': 5 * 1024 ** 3, 'request': 32 * 1024 ** 3,
               'limit': 64 * 1024 ** 3, 'limit_status': 'bounded', 'unit': 'bytes',
               'status': 'ok', 'sampled_at': _SAMPLE_AT,
               'coverage': {'observed': 2, 'expected': 2}},
    'storage': {'usage': None, 'capacity': 256 * 1024 ** 3,
                'requested': 256 * 1024 ** 3, 'unit': 'bytes',
                'status': 'unavailable', 'sampled_at': None,
                'coverage': {'observed': 0, 'expected': 1}, 'volumes': []},
    'ephemeral': {'usage': None, 'limit': 32 * 1024 ** 3, 'request': 4 * 1024 ** 3,
                  'limit_status': 'bounded', 'status': 'unavailable'},
    'pods': {'total': 1, 'running': 1, 'ready': 1},
    'warnings': ['metrics_storage_unavailable'],
}


@tagged('post_install', '-at_install')
class TestServiceResourceObservations(HttpCase):
    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]

        def user(label, portal=False):
            login = 'resources_%s_%s' % (label, suffix)
            return self.env['res.users'].sudo().with_context(no_reset_password=True).create({
                'name': login, 'login': login, 'password': login,
                'email': login + '@test.example',
                'groups_id': [(6, 0, [self.env.ref(
                    'base.group_portal' if portal else 'base.group_user').id])],
            })

        self.owner = user('owner')
        self.guest = user('guest')
        self.portal = user('portal', portal=True)
        self.other = user('other')
        self.assertFalse(self.guest.has_group('base.group_system'))
        self.assertTrue(self.portal.share)
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        self.workspace = Workspace.create({'name': 'Resources ' + suffix,
                                           'owner_id': self.owner.id})
        self.other_workspace = Workspace.create({'name': 'Other resources ' + suffix,
                                                 'owner_id': self.other.id})
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        for member in (self.guest, self.portal):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': member.id, 'role': 'guest'})
        self.template = self.env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': 'Resource test', 'slug': 'resources-' + suffix,
            'helm_repo_url': '', 'helm_chart_name': 'oci://example.com/resources',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container', 'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        self.service = self._service(self.workspace, 'one', suffix)
        self.second = self._service(self.workspace, 'two', suffix)
        self.foreign = self._service(self.other_workspace, 'foreign', suffix)
        self.client = MagicMock()
        self.client.base_url = 'http://operator.test'
        self.client.api_key = 'test-key-never-forwarded'
        self.route = '/api/workspaces/%s/service-resource-observations' % self.workspace.id

    def _service(self, workspace, label, suffix):
        return _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create({
            'name': label, 'workspace_id': workspace.id, 'template_id': self.template.id,
            'state': 'running', 'helm_namespace': 'paas-ws-resources-' + suffix,
            'helm_release_name': 'svc-' + label + '-' + suffix,
            'helm_values': '{}', 'allocated_vcpu': 2, 'allocated_ram_gb': 4,
            'allocated_storage_gb': 20,
        })

    def _post(self, ids, **extra):
        response = self.url_open(self.route, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'service_ids': ids, **extra},
        }), headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        body = response.json()
        self.assertNotIn('error', body, body)
        return body['result']

    def _reply(self, _base, _key, _namespace, releases):
        return {'observed_at': _SAMPLE_AT, 'refresh_seconds': 30,
                'services': {name: copy.deepcopy(_OBSERVATION) for name in releases}}

    def test_internal_and_portal_viewers_read_runtime_without_writes(self):
        before = self.service.read(_SNAPSHOT_FIELDS)[0]
        for member in (self.owner, self.guest, self.portal):
            with self.subTest(principal=member.login):
                self.authenticate(member.login, member.login)
                with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client), \
                        patch(_CONTROLLER + '._fetch_resource_observations', side_effect=self._reply) as fetch, \
                        patch.object(type(self.service), '_push_ns_quota', side_effect=AssertionError('read changed quota')), \
                        patch.object(type(self.service), '_apply_helm_values', side_effect=AssertionError('read changed capacity')):
                    result = self._post([self.service.id, self.second.id])
                self.assertTrue(result['success'])
                self.assertEqual(fetch.call_count, 1, 'one namespace must be one operator batch')
                self.assertEqual(set(fetch.call_args.args[3]), {
                    self.service.helm_release_name, self.second.helm_release_name})
                observation = result['data']['services'][str(self.service.id)]
                self.assertEqual(observation['cpu']['limit'], 32)
                self.assertEqual(observation['cpu']['usage'], 0, 'actual zero is not missing')
                self.assertIsNone(observation['storage']['usage'], 'unknown is not zero')
                self.assertEqual(observation['storage']['capacity'], 256 * 1024 ** 3)
                self.assertEqual(self.service.read(_SNAPSHOT_FIELDS)[0], before,
                                 'reading measurements must not reprice or rewrite the service')

    def test_cross_workspace_mixed_batch_is_rejected_before_operator(self):
        self.authenticate(self.guest.login, self.guest.login)
        with patch(_CONTROLLER + '.get_paas_operator_client') as factory, \
                patch(_CONTROLLER + '._fetch_resource_observations') as fetch:
            result = self._post([self.service.id, self.foreign.id])
        self.assertFalse(result['success'])
        factory.assert_not_called()
        fetch.assert_not_called()

    def test_nonmember_and_hidden_services_are_not_observable(self):
        self.authenticate(self.other.login, self.other.login)
        with patch(_CONTROLLER + '._fetch_resource_observations') as fetch:
            self.assertFalse(self._post([self.service.id])['success'])
        fetch.assert_not_called()
        self.template.write({'conceal_instances': True, 'is_hidden': True})
        self.authenticate(self.owner.login, self.owner.login)
        with patch(_CONTROLLER + '._fetch_resource_observations') as fetch:
            self.assertFalse(self._post([self.service.id])['success'])
        fetch.assert_not_called()

    def test_untrusted_anchors_and_invalid_batch_are_rejected(self):
        self.authenticate(self.guest.login, self.guest.login)
        for ids, extra in [([self.service.id], {'namespace': 'paas-ws-other'}),
                           ([self.service.id], {'release_names': ['foreign']}),
                           ([True], {}), (['1'], {}), ([-1], {}),
                           (list(range(1, 102)), {}), (None, {})]:
            with self.subTest(ids=ids, extra=extra), \
                    patch(_CONTROLLER + '._fetch_resource_observations') as fetch:
                self.assertFalse(self._post(ids, **extra)['success'])
            fetch.assert_not_called()

    def test_operator_timeout_is_unknown_and_never_leaks_error_detail(self):
        self.authenticate(self.guest.login, self.guest.login)
        before = self.service.read(_SNAPSHOT_FIELDS)[0]
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client), \
                patch(_CONTROLLER + '._fetch_resource_observations', side_effect=PaaSOperatorTimeoutError(
                    'secret-token-do-not-leak', detail='http://internal/?token=sensitive')):
            result = self._post([self.service.id])
        self.assertTrue(result['success'])
        observation = result['data']['services'][str(self.service.id)]
        self.assertEqual(observation['status'], 'unavailable')
        self.assertIsNone(observation['cpu']['usage'])
        self.assertIsNone(result['data']['observed_at'])
        self.assertNotIn('secret-token', json.dumps(result))
        self.assertNotIn('sensitive', json.dumps(result))
        self.assertEqual(self.service.read(_SNAPSHOT_FIELDS)[0], before)

    def test_namespace_groups_have_one_shared_wait_budget(self):
        self.second.write({'helm_namespace': self.second.helm_namespace + '-second'})
        self.authenticate(self.guest.login, self.guest.login)
        unblock = threading.Event()

        def blocked(*_args):
            unblock.wait(2)
            return {'services': {}}

        try:
            with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client), \
                    patch(_CONTROLLER + '.RESOURCE_OBSERVATION_TIMEOUT', 0.01), \
                    patch(_CONTROLLER + '._fetch_resource_observations', side_effect=blocked), \
                    patch(_CONTROLLER + '.wait', wraps=futures_wait) as bounded_wait:
                result = self._post([self.service.id, self.second.id])
            self.assertTrue(result['success'])
            self.assertEqual(bounded_wait.call_count, 1)
            self.assertEqual(bounded_wait.call_args.kwargs['timeout'], 0.01)
            self.assertTrue(all(value['status'] == 'unavailable'
                                for value in result['data']['services'].values()))
        finally:
            unblock.set()

    def test_operator_extra_keys_and_other_services_never_escape(self):
        self.authenticate(self.guest.login, self.guest.login)
        raw = copy.deepcopy(_OBSERVATION)
        raw['credentials'] = {'password': 'never-forward'}
        raw['warnings'].append('http://internal/?token=never-forward')
        raw['cpu']['config'] = 'never-forward'
        reply = {'observed_at': _SAMPLE_AT,
                 'services': {self.service.helm_release_name: raw,
                              self.foreign.helm_release_name: raw},
                 'api_key': 'never-forward'}
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client), \
                patch(_CONTROLLER + '._fetch_resource_observations', return_value=reply):
            result = self._post([self.service.id])
        self.assertTrue(result['success'])
        self.assertEqual(set(result['data']['services']), {str(self.service.id)})
        self.assertNotIn('never-forward', json.dumps(result))

    def test_malformed_observation_remains_unavailable_without_crashing(self):
        self.authenticate(self.guest.login, self.guest.login)
        reply = {'observed_at': 'not-a-timestamp',
                 'services': {self.service.helm_release_name: {'status': ['invalid']}}}
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client), \
                patch(_CONTROLLER + '._fetch_resource_observations', return_value=reply):
            result = self._post([self.service.id])
        observation = result['data']['services'][str(self.service.id)]
        self.assertEqual(observation['status'], 'unavailable')
        self.assertIsNone(observation['cpu']['limit'])
        self.assertIsNone(result['data']['observed_at'])

    def test_stale_unbounded_and_vm_measurements_are_not_replaced_by_allocated(self):
        self.authenticate(self.guest.login, self.guest.login)
        stale = copy.deepcopy(_OBSERVATION)
        stale['cpu'].update(status='stale', usage=0.5)
        stale['memory'].update(limit=None, limit_status='unbounded')
        vm = copy.deepcopy(_OBSERVATION)
        vm.update(status='unsupported', workload_type='vm', resource_scope='vm_guest')
        vm['cpu'].update(status='unsupported', usage=None, request=8, limit=8)
        vm['memory'].update(status='unsupported', usage=None)
        reply = {'observed_at': _SAMPLE_AT, 'services': {
            self.service.helm_release_name: stale, self.second.helm_release_name: vm}}
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=self.client), \
                patch(_CONTROLLER + '._fetch_resource_observations', return_value=reply):
            result = self._post([self.service.id, self.second.id])
        observed = result['data']['services'][str(self.service.id)]
        self.assertEqual(observed['cpu']['status'], 'stale')
        self.assertEqual(observed['cpu']['usage'], 0.5)
        self.assertEqual(observed['memory']['limit_status'], 'unbounded')
        self.assertIsNone(observed['memory']['limit'])
        observed_vm = result['data']['services'][str(self.second.id)]
        self.assertEqual(observed_vm['resource_scope'], 'vm_guest')
        self.assertEqual(observed_vm['cpu']['limit'], 8)
        self.assertIsNone(observed_vm['cpu']['usage'])
        self.assertEqual(self.second.allocated_vcpu, 2)


@tagged('post_install', '-at_install')
class TestResourceObservationClient(TransactionCase):
    def test_client_uses_batch_and_interactive_budget(self):
        client = PaaSOperatorClient('http://operator.test', 'unused')
        with patch.object(client, '_request', return_value={}) as send:
            client.get_resource_observations('paas-ws-test', ['svc-one', 'svc-two'])
        send.assert_called_once_with(
            'POST', '/api/namespaces/paas-ws-test/resource-observations',
            data={'release_names': ['svc-one', 'svc-two']}, timeout=(2, 10))
