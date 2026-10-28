"""Per-identity batch Access ordering; fake desired state, never edge proof."""
import logging
import uuid
from copy import deepcopy
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import (
    _internal_cs_access_identity,
    _internal_provision,
)
from ..models.security_access import (
    _internal_identity as _sa_identity,
    _internal_policy_cf_write,
    _skip_route_sync,
)
from ..models.smart_home import (
    _internal_identity as _sh_identity,
    _platform_order as _sh_platform_order,
)
from ..models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_skip_access_sync,
)
from ..services.naming import make_namespace, make_service_subdomain
from ..services.paas_operator import PaaSOperatorError
from .test_access_publish_pending_ac1011 import PendingFixture, SA_CLIENT
from .test_cs_sh_fail_closed_ac1011 import FakeOperator
from .publication_receipt_fixture import configure_receipt, routes_receipt


_logger = logging.getLogger(__name__)


class PerIdentityOperator(FakeOperator):
    def get_tunnel_status(self, tunnel_id, *args, **kwargs):
        # Security Access recovery re-publishes only against a live connector;
        # the fixture names each tunnel after its id.
        return {'tunnel_id': tunnel_id, 'name': tunnel_id,
                'status': 'healthy', 'deleted_at': None,
                'connections': [{'connector_id': 'per-identity-connector'}]}

    """Track app/origin and every phase by stable Access app identity."""

    def __init__(self):
        super().__init__()
        self.apps = {}
        self.origins = {}
        self.app_by_name = {}
        self.app_by_subdomain = {}
        self.apps_by_group = {}
        self.phase_history = []
        self.sync_requests = []
        self.partial_key = None
        self.fail_park_group = None
        self.fail_release_group = None

    def register(self, key, app_name, subdomain, group):
        self.apps[key] = True
        self.origins[key] = True
        self.app_by_name[app_name] = key
        self.app_by_subdomain[subdomain] = key
        self.apps_by_group.setdefault(group, []).append(key)

    def _phase(self, phase, key, **extra):
        self.phase_history.append({
            'phase': phase, 'key': key,
            'app': self.apps.get(key), 'origin': self.origins.get(key),
            **extra,
        })

    def sync_access_app(self, payload):
        key = self.app_by_name[payload['app_name']]
        has_delete_identity = bool(
            payload.get('existing_app_id') or
            payload.get('existing_policy_ids'))
        self.sync_requests.append({
            'key': key, 'enabled': bool(payload['enabled']),
            'has_delete_identity': has_delete_identity,
        })
        if payload['enabled']:
            self.apps[key] = True
            self._phase('access-enable', key, payload=deepcopy(payload))
        elif has_delete_identity:
            self.apps[key] = False
            self._phase('access-delete', key, payload=deepcopy(payload))
        result = {
            'app_id': 'new-'+key if payload['enabled'] else None,
            'policy_ids': (['new-%s-%s' % (key, index)
                            for index, _policy in enumerate(
                                payload.get('policies', []), 1)]
                           if payload['enabled'] else []),
            'allowed_idps': (['idp-'+kind for kind in
                              payload.get('login_idp_types', [])]
                             if payload['enabled'] else []),
            'unresolved_idp_types': [], 'orphaned_policy_ids': [],
        }
        if key == self.partial_key:
            result['status'] = 'partial'
        return result

    def park_release_route(self, _namespace, _release, subdomain):
        key = self.app_by_subdomain[subdomain]
        if self.fail_park_group == key:
            self._phase('park-failed', key)
            raise PaaSOperatorError('keyed park failure', status_code=503)
        self.origins[key] = False
        self._phase('park', key)
        return {'status': 'parked'}

    def unpark_release_route(self, _namespace, _release, expose):
        key = self.app_by_subdomain[expose['subdomain']]
        if self.fail_release_group == key:
            self._phase('release-failed', key)
            raise PaaSOperatorError('keyed release failure', status_code=502)
        self.origins[key] = True
        self._phase('release', key)
        return {'status': 'unparked'}

    def sync_tunnel_routes(self, tunnel_id, routes, **_kwargs):
        keys = self.apps_by_group.get(tunnel_id, [])
        public = {
            self.app_by_subdomain[row['subdomain']]
            for row in routes
            if (row['subdomain'] in self.app_by_subdomain
                and not row['service_url'].startswith('http_status:'))
        }
        is_release = bool(public)
        if is_release and self.fail_release_group == tunnel_id:
            for key in keys:
                self._phase('release-failed', key, group=tunnel_id)
            raise PaaSOperatorError('keyed routes release failure',
                                    status_code=502)
        if not is_release and self.fail_park_group == tunnel_id:
            for key in keys:
                self._phase('park-failed', key, group=tunnel_id)
            raise PaaSOperatorError('keyed routes park failure',
                                    status_code=503)
        for key in keys:
            self.origins[key] = key in public
            self._phase('release' if key in public else 'park', key,
                        group=tunnel_id)
        return routes_receipt(tunnel_id, routes, domain='example.test')

    def configure_tunnel(self, tunnel_id, **kwargs):
        keys = self.apps_by_group.get(tunnel_id, [])
        release = not kwargs['service_url'].startswith('http_status:')
        if release and self.fail_release_group == tunnel_id:
            for key in keys:
                self._phase('release-failed', key, group=tunnel_id)
            raise PaaSOperatorError('keyed ingress release failure',
                                    status_code=502)
        if not release and self.fail_park_group == tunnel_id:
            for key in keys:
                self._phase('park-failed', key, group=tunnel_id)
            raise PaaSOperatorError('keyed ingress park failure',
                                    status_code=503)
        for key in keys:
            self.origins[key] = release
            self._phase('release' if release else 'park', key,
                        group=tunnel_id)
        return configure_receipt(
            tunnel_id, kwargs['hostname'], kwargs['service_url'])


@tagged('post_install', '-at_install')
class TestAccessSyncPerIdentityAC1011(PendingFixture, TransactionCase):
    CASES = ('cs-two', 'sa-same', 'sa-different',
             'sh-same', 'sh-different')

    def setUp(self):
        super().setUp()
        self.install_fake()
        self.client = PerIdentityOperator()
        for path in (
            'odoo.addons.woow_paas_platform.services.paas_operator.'
            'get_paas_operator_client',
            'odoo.addons.woow_paas_platform.models.smart_home.'
            'get_paas_operator_client',
            SA_CLIENT,
        ):
            mock = patch(path, return_value=self.client)
            mock.start()
            self.addCleanup(mock.stop)
        self.resources()

    def _policy(self, anchor, field, key):
        policy = _skip_route_sync(
            self.env['woow_paas_platform.security_access_access_policy'].sudo()
        ).create({
            field: anchor.id, 'name': 'Keyed '+key, 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                'rule_type': 'email_list',
                'rule_value': key+'@example.test'})],
        })
        _internal_policy_cf_write(policy).cf_policy_id = 'old-policy-'+key

    def _cs(self, key):
        reference = str(uuid.uuid4())
        record = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].sudo()).create({
                'name': key, 'workspace_id': self.workspace.id,
                'template_id': self.template.id, 'reference_id': reference,
                'subdomain': make_service_subdomain(
                    self.workspace.ws_hash, reference, key),
                'helm_namespace': make_namespace(self.workspace.ws_hash),
                'helm_release_name': key+'-'+uuid.uuid4().hex[:6],
                'state': 'running', 'is_protected': True,
                'login_google': True,
            }).with_env(self.env)
        _internal_cs_access_identity(record).access_app_id = 'old-app-'+key
        self._policy(record, 'cloud_service_id', key)
        self.client.register(key, record._build_access_payload()['app_name'],
                             record.subdomain, key)
        return record

    def _sa(self, key, gateway=None):
        if gateway is None:
            gateway = self.env[
                'woow_paas_platform.security_access'].sudo().create({
                    'name': key, 'workspace_id': self.workspace.id})
            _sa_identity(gateway).write({
                'state': 'active', 'tunnel_id': 'tunnel-'+key,
                'tunnel_name': 'tunnel-'+key})
        route = _skip_route_sync(
            self.env['woow_paas_platform.security_access_route'].sudo()).create({
                'security_access_id': gateway.id,
                'subdomain_prefix': key+'-'+uuid.uuid4().hex[:6],
                'service_url': 'http://'+key+'.example.test:80',
                'is_protected': True, 'login_google': True,
            }).with_env(self.env)
        _sa_identity(route).access_app_id = 'old-app-'+key
        self._policy(route, 'route_id', key)
        payload = route._build_access_payload()
        self.client.register(key, payload['app_name'],
                             route.subdomain_prefix, gateway.tunnel_id)
        return route, gateway

    def _home(self, key):
        home = _sh_platform_order(
            self.env['woow_paas_platform.smart_home'].sudo()).create({
                'name': key, 'workspace_id': self.workspace.id,
                'state': 'pending'})
        home.subdomain = home._generate_subdomain()
        _sh_identity(home).write({
            'state': 'active', 'tunnel_id': 'tunnel-'+key,
            'tunnel_name': 'tunnel-'+key})
        return home

    def _sh(self, key, home, primary):
        route = _sm_skip_access_sync(
            self.env['woow_paas_platform.smart_home_route'].sudo()).create({
                'smart_home_id': home.id, 'is_primary': primary,
                'subdomain_prefix': False if primary else key,
                'is_protected': True, 'login_google': True,
            }).with_env(self.env)
        _internal_sm_route_identity(route).access_app_id = 'old-app-'+key
        self._policy(route, 'smart_home_route_id', key)
        payload = route._build_access_payload()
        self.client.register(key, payload['app_name'], route.hostname,
                             home.tunnel_id)
        return route

    def _case(self, name):
        suffix = uuid.uuid4().hex[:6]
        one, two, peer = (name+'-a-'+suffix, name+'-b-'+suffix,
                          name+'-peer-'+suffix)
        if name == 'cs-two':
            records = self._cs(one) | self._cs(two)
            groups = (one, two)
            sentinel = self._cs(peer)
        elif name.startswith('sa-'):
            first, gateway = self._sa(one)
            second, second_gateway = self._sa(
                two, gateway if name == 'sa-same' else None)
            records = first | second
            groups = (gateway.tunnel_id, second_gateway.tunnel_id)
            sentinel, _sentinel_gateway = self._sa(peer)
        else:
            first_home = self._home(one)
            second_home = (first_home if name == 'sh-same'
                           else self._home(two))
            records = self._sh(one, first_home, True) | self._sh(
                two, second_home, second_home != first_home)
            groups = (first_home.tunnel_id, second_home.tunnel_id)
            sentinel = self._sh(peer, self._home(peer), True)
        records.flush_recordset()
        sentinel.flush_recordset()
        self.client.phase_history.clear()
        self.client.sync_requests.clear()
        return records, (one, two), groups, sentinel, peer

    def _assert_old_db(self, records, keys):
        records.invalidate_recordset()
        self.assertTrue(all(records.mapped('is_protected')))
        self.assertFalse(any(records.mapped('access_publish_pending')))
        self.assertEqual(set(records.mapped('access_app_id')),
                         {'old-app-'+key for key in keys})
        self.assertEqual(set(records.mapped(
            'access_policy_ids.cf_policy_id')),
            {'old-policy-'+key for key in keys})

    def _assert_sentinel_untouched(self, sentinel, key):
        self._assert_old_db(sentinel, (key,))
        self.assertTrue(self.client.apps[key])
        self.assertTrue(self.client.origins[key])
        self.assertFalse(any(
            row['key'] == key for row in self.client.phase_history))
        self.assertFalse(any(
            row['key'] == key for row in self.client.sync_requests))

    def test_partial_second_delete_rolls_back_every_identity_without_release(self):
        for name in self.CASES:
            with self.subTest(case=name):
                records, keys, _groups, sentinel, peer = self._case(name)
                self.client.partial_key = keys[1]
                with self.assertRaisesRegex(UserError, 'not confirmed'):
                    records.write({'is_protected': False})
                self._assert_old_db(records, keys)
                self.assertFalse(self.client.apps[keys[0]])
                self.assertFalse(self.client.apps[keys[1]])
                self.assertFalse(self.client.origins[keys[0]])
                self.assertFalse(self.client.origins[keys[1]])
                phases = self.client.phase_history
                deletes = [row for row in phases
                           if row['phase'] == 'access-delete']
                self.assertEqual([row['key'] for row in deletes], list(keys))
                first_delete = phases.index(deletes[0])
                self.assertTrue(all(
                    any(row['phase'] == 'park' and row['key'] == key
                        for row in phases[:first_delete]) for key in keys))
                self.assertFalse(any(row['phase'] == 'release'
                                     for row in phases))
                self._assert_sentinel_untouched(sentinel, peer)
                _logger.info(
                    'PER_IDENTITY_CELL scenario=partial-delete group=%s complete',
                    name)

    def test_park_failure_precedes_every_remote_delete(self):
        for name in self.CASES:
            with self.subTest(case=name):
                records, keys, groups, sentinel, peer = self._case(name)
                self.client.fail_park_group = groups[-1]
                with self.assertRaises(PaaSOperatorError):
                    records.write({'is_protected': False})
                self._assert_old_db(records, keys)
                self.assertTrue(self.client.apps[keys[0]])
                self.assertTrue(self.client.apps[keys[1]])
                self.assertFalse(any(
                    row['phase'].startswith('access-')
                    for row in self.client.phase_history))
                self.assertFalse(self.client.sync_requests)
                failed_keys = set(self.client.apps_by_group[groups[-1]])
                earlier_keys = (set() if groups[0] == groups[-1] else
                                set(self.client.apps_by_group[groups[0]]))
                self.assertEqual(
                    {row['key'] for row in self.client.phase_history
                     if row['phase'] == 'park'}, earlier_keys)
                self.assertEqual(
                    {row['key'] for row in self.client.phase_history
                     if row['phase'] == 'park-failed'}, failed_keys)
                self.assertTrue(all(
                    self.client.origins[key] == (key not in earlier_keys)
                    for key in keys))
                self._assert_sentinel_untouched(sentinel, peer)
                _logger.info(
                    'PER_IDENTITY_CELL scenario=park-failure group=%s complete',
                    name)

    def test_release_failure_marks_exact_group_pending_and_retry_only_releases(self):
        for name in self.CASES:
            with self.subTest(case=name):
                records, keys, groups, sentinel, peer = self._case(name)
                failed_group = groups[-1]
                self.client.fail_release_group = failed_group
                self.assertTrue(records.write({'is_protected': False}))
                records.invalidate_recordset()
                pending = {
                    self.client.app_by_name[
                        record._build_access_payload()['app_name']]:
                    record.access_publish_pending for record in records}
                self.assertEqual(
                    {key for key, value in pending.items() if value},
                    set(self.client.apps_by_group[failed_group]))
                failed_keys = set(self.client.apps_by_group[failed_group])
                successful_keys = set(keys) - failed_keys
                self.assertFalse(any(records.mapped('is_protected')))
                self.assertFalse(any(records.mapped('access_app_id')))
                self.assertTrue(all(not self.client.apps[key] for key in keys))
                self.assertEqual(
                    {row['key'] for row in self.client.phase_history
                     if row['phase'] == 'release'}, successful_keys)
                self.assertEqual(
                    {row['key'] for row in self.client.phase_history
                     if row['phase'] == 'release-failed'}, failed_keys)
                self.assertTrue(all(
                    self.client.origins[key] == (key in successful_keys)
                    for key in keys))
                delete_indexes = [
                    index for index, row in enumerate(self.client.phase_history)
                    if row['phase'] == 'access-delete']
                release_indexes = [
                    index for index, row in enumerate(self.client.phase_history)
                    if row['phase'] in ('release', 'release-failed')
                ]
                self.assertTrue(delete_indexes)
                self.assertTrue(release_indexes)
                self.assertLess(max(delete_indexes), min(release_indexes))
                self._assert_sentinel_untouched(sentinel, peer)
                retry_records = records.filtered('access_publish_pending')
                self.assertEqual(
                    {self.client.app_by_name[
                        record._build_access_payload()['app_name']]
                     for record in retry_records}, failed_keys)
                self.client.fail_release_group = None
                self.client.phase_history.clear()
                self.client.sync_requests.clear()
                self.assertTrue(
                    retry_records.write({'is_protected': False}))
                records.invalidate_recordset()
                self.assertFalse(any(records.mapped('access_publish_pending')))
                self.assertFalse(any(row['phase'] == 'access-enable'
                                     for row in self.client.phase_history))
                self.assertEqual(
                    {row['key'] for row in self.client.phase_history
                     if row['phase'] == 'access-delete'}, set())
                self.assertEqual(
                    {row['key'] for row in self.client.sync_requests},
                    failed_keys)
                self.assertFalse(any(
                    row['has_delete_identity']
                    for row in self.client.sync_requests))
                self.assertEqual(
                    {row['key'] for row in self.client.phase_history
                     if row['phase'] == 'release'}, failed_keys)
                self.assertFalse(any(
                    row['key'] in successful_keys
                    for row in self.client.phase_history))
                self.assertTrue(all(not self.client.apps[key] for key in keys))
                self.assertTrue(all(self.client.origins[key] for key in keys))
                self._assert_sentinel_untouched(sentinel, peer)
                _logger.info(
                    'PER_IDENTITY_CELL scenario=release-retry group=%s complete',
                    name)
