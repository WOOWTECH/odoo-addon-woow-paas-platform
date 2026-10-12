"""Access sync 2xx receipts are exact before any anchor settlement/release."""
from copy import deepcopy
import uuid
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import (
    _cs_skip_access_sync,
    _internal_cs_access_identity,
    _internal_provision,
)
from ..models.security_access import (
    _internal_identity as sa_identity,
    _internal_identity as sa_route_identity,
    _internal_policy_cf_write,
    _skip_route_sync,
)
from ..models.smart_home_route import (
    _internal_sm_route_identity,
    _sm_skip_access_sync,
)
from ..models.smart_home import (
    _internal_identity as _sh_identity,
    _platform_order as _sh_platform_order,
)
from ..services.access_state import require_access_sync_receipt
from ..services.naming import make_service_subdomain
from .test_access_publish_pending_ac1011 import PendingFixture, SA_CLIENT


@tagged('post_install', '-at_install')
class TestAccessSyncReceiptAC1011(PendingFixture, TransactionCase):
    def setUp(self):
        super().setUp()
        self.install_fake()
        self.resources()
        operator = patch(SA_CLIENT, return_value=self.client)
        operator.start()
        self.addCleanup(operator.stop)

    def _anchor_with_policy(self, kind, policy_count=1):
        anchor = self.anchor(kind)
        if kind == 'cs':
            _cs_skip_access_sync(anchor).is_protected = True
            self.cs_active()
            field = 'cloud_service_id'
        elif kind == 'sh':
            _sm_skip_access_sync(anchor).is_protected = True
            self.sh_active()
            field = 'smart_home_route_id'
        else:
            _skip_route_sync(anchor).is_protected = True
            sa_identity(anchor.security_access_id).write({
                'state': 'active',
                'tunnel_id': 'native-receipt-sa-tunnel',
                'tunnel_name': 'native-receipt-sa-tunnel-name',
            })
            self.client.origin = True
            self.client.events.clear()
            field = 'route_id'
        Policy = self.env[
            'woow_paas_platform.security_access_access_policy']
        for sequence in range(policy_count):
            _skip_route_sync(Policy).create({
                field: anchor.id,
                'name': 'Receipt policy %s' % sequence,
                'sequence': sequence + 1,
                'decision': 'allow',
                'rule_ids': [(0, 0, {
                    'group': 'include',
                    'rule_type': 'email_list',
                    'rule_value': 'owner@example.test',
                })],
            })
        return anchor

    def _malformed_matrix(self, kind):
        anchor = self._anchor_with_policy(kind)
        original = self.client.sync_access_app
        valid = {
            'app_id': 'receipt-app',
            'policy_ids': ['receipt-policy'],
            'allowed_idps': ['receipt-idp'],
            'orphaned_policy_ids': [],
            'unresolved_idp_types': [],
        }
        body = dict(valid, success=False)
        self.client.events.clear()
        self.client.origin = True
        self.client.sync_access_app = lambda payload: deepcopy(body)
        with self.assertRaisesRegex(UserError, 'not confirmed'):
            anchor._sync_access_to_operator()
        anchor.invalidate_recordset()
        self.assertFalse(self.client.origin)
        self.assertFalse(anchor.access_app_id)
        self.assertTrue(anchor.is_protected)
        self.assertNotIn('unpark', self.names())
        if kind == 'sa':
            self.assertEqual(
                self.names().count('routes'), 1,
                'SA must perform exactly one park and no second routes release')
        else:
            self.assertNotIn('routes', self.names())
        self.client.sync_access_app = original

    def _batch_anchors(self, kind, separate_parking_group=False):
        first = self._anchor_with_policy(kind)
        if kind == 'cs':
            reference = str(uuid.uuid4())
            second = _internal_provision(
                self.env['woow_paas_platform.cloud_service'].sudo()).create({
                    'name': 'AC1011 batch CS', 'workspace_id': self.workspace.id,
                    'template_id': self.template.id, 'reference_id': reference,
                    'subdomain': make_service_subdomain(
                        self.workspace.ws_hash, reference, 'AC1011 batch CS'),
                    'helm_namespace': self.cs.helm_namespace,
                    'helm_release_name': 'batch-'+uuid.uuid4().hex[:8],
                    'state': 'running', 'is_protected': True,
                    'login_google': True,
                }).with_env(self.env)
            field = 'cloud_service_id'
        elif kind == 'sh':
            home = self.sh
            if separate_parking_group:
                home = _sh_platform_order(
                    self.env['woow_paas_platform.smart_home'].sudo()).create({
                    'name': 'AC1011 batch SH', 'workspace_id': self.workspace.id,
                    'state': 'pending'})
                home.subdomain = home._generate_subdomain()
                _sh_identity(home).write({
                    'state': 'active', 'tunnel_id': 'batch-'+uuid.uuid4().hex[:8],
                    'tunnel_name': 'batch-sh-tunnel'})
            second = _sm_skip_access_sync(
                self.env['woow_paas_platform.smart_home_route'].sudo()).create({
                    'smart_home_id': home.id,
                    'is_primary': bool(separate_parking_group),
                    'subdomain_prefix': (False if separate_parking_group else
                                         'batch-'+uuid.uuid4().hex[:8]),
                    'is_protected': True, 'login_google': True,
                }).with_env(self.env)
            field = 'smart_home_route_id'
        else:
            if separate_parking_group:
                sa = self.env['woow_paas_platform.security_access'].sudo().create({
                    'name': 'Native batch SA '+uuid.uuid4().hex[:6],
                    'workspace_id': self.workspace.id})
                second = _skip_route_sync(
                    self.env['woow_paas_platform.security_access_route'].sudo()).create({
                        'security_access_id': sa.id,
                        'subdomain_prefix': 'batch-separate-'+uuid.uuid4().hex[:8],
                        'service_url': 'http://batch-separate.example.test:80',
                        'is_protected': True, 'login_google': True,
                    }).with_env(self.env)
                sa_identity(sa).write({
                    'state': 'active', 'tunnel_id': 'batch-sa-'+uuid.uuid4().hex[:8],
                    'tunnel_name': 'batch-sa-tunnel'})
            else:
                sa = first.security_access_id
                second = _skip_route_sync(
                    self.env['woow_paas_platform.security_access_route'].sudo()).create({
                        'security_access_id': sa.id,
                        'subdomain_prefix': 'batch-'+uuid.uuid4().hex[:8],
                        'service_url': 'http://batch.example.test:80',
                        'is_protected': True, 'login_google': True,
                    }).with_env(self.env)
            field = 'route_id'
        Policy = self.env['woow_paas_platform.security_access_access_policy']
        _skip_route_sync(Policy).create({
            field: second.id, 'name': 'Batch second policy',
            'sequence': 1, 'decision': 'allow',
            'rule_ids': [(0, 0, {'group': 'include',
                'rule_type': 'email_list',
                'rule_value': 'batch@example.test'})],
        })
        for index, anchor in enumerate((first, second), 1):
            if kind == 'cs':
                _internal_cs_access_identity(anchor).access_app_id = 'old-cs-app-%s' % index
            elif kind == 'sh':
                _internal_sm_route_identity(anchor).access_app_id = 'old-sh-app-%s' % index
            else:
                sa_route_identity(anchor).access_app_id = 'old-sa-app-%s' % index
            _internal_policy_cf_write(
                anchor.access_policy_ids).cf_policy_id = 'old-%s-policy-%s' % (kind, index)
        self.client.app = True
        self.client.origin = True
        self.client.events.clear()
        self.client.history.clear()
        return first | second

    def test_three_batch_unprotect_validate_all_before_any_release(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchors = self._batch_anchors(kind)
                calls = []
                def partial_second(payload):
                    calls.append(payload['app_name'])
                    self.client.app = False
                    body = {'app_id': None, 'policy_ids': [],
                            'allowed_idps': [], 'unresolved_idp_types': [],
                            'orphaned_policy_ids': []}
                    if len(calls) == 2:
                        body['status'] = 'partial'
                    return body
                self.client.sync_access_app = partial_second
                with self.assertRaisesRegex(UserError, 'not confirmed'):
                    anchors.write({'is_protected': False})
                anchors.invalidate_recordset()
                self.assertTrue(all(anchors.mapped('is_protected')))
                self.assertEqual(len(calls), 2)
                self.assertNotIn('unpark', self.names())
                self.assertNotIn('publish', self.names())
                self.assertFalse(self.client.origin)
                self.assertFalse(any(origin for origin, _app in
                                     self.client.history), self.client.history)
                self.client.sync_access_app = lambda payload: {
                    'app_id': None, 'policy_ids': [], 'allowed_idps': [],
                    'unresolved_idp_types': [], 'orphaned_policy_ids': []}
                self.client.events.clear()
                self.assertTrue(anchors.write({'is_protected': False}))
                anchors.invalidate_recordset()
                self.assertFalse(any(anchors.mapped('is_protected')))
                self.assertFalse(any(anchors.mapped('access_app_id')))
                self.assertTrue(self.client.origin)

    def test_three_batch_second_park_failure_prevents_all_access_deletes(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchors = self._batch_anchors(kind, separate_parking_group=True)
                original = (self.client.park_release_route if kind == 'cs'
                            else self.client.configure_tunnel if kind == 'sh'
                            else self.client.sync_tunnel_routes)
                parks = []
                def fail_second(*args, **kwargs):
                    parks.append(1)
                    if len(parks) == 2:
                        raise RuntimeError('second park failed')
                    return original(*args, **kwargs)
                if kind == 'cs': self.client.park_release_route = fail_second
                elif kind == 'sh': self.client.configure_tunnel = fail_second
                else: self.client.sync_tunnel_routes = fail_second
                with self.assertRaisesRegex(RuntimeError, 'second park failed'):
                    anchors.write({'is_protected': False})
                self.assertNotIn('access', self.names())

    def test_three_batch_release_failure_is_pending_then_retries(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchors = self._batch_anchors(kind)
                self.client.sync_access_app = lambda payload: {
                    'app_id': None, 'policy_ids': [], 'allowed_idps': [],
                    'unresolved_idp_types': [], 'orphaned_policy_ids': []}
                self.fail_publish(kind)
                self.assertTrue(anchors.write({'is_protected': False}))
                anchors.invalidate_recordset()
                self.assertFalse(any(anchors.mapped('access_app_id')))
                self.assertTrue(any(anchors.mapped('access_publish_pending')))
                self.fail_publish(kind, False)
                self.client.events.clear()
                self.assertTrue(anchors.write({'is_protected': False}))
                anchors.invalidate_recordset()
                self.assertFalse(any(
                    anchors.mapped('access_publish_pending')))
                self.assertTrue(self.client.origin)

    def test_cs_rejects_malformed_access_sync_receipts_before_release(self):
        self._malformed_matrix('cs')

    def test_sh_rejects_malformed_access_sync_receipts_before_release(self):
        self._malformed_matrix('sh')

    def test_sa_rejects_malformed_access_sync_receipts_before_release(self):
        self._malformed_matrix('sa')

    def test_shared_validator_rejects_complete_malformed_matrix(self):
        payload = {
            'enabled': True,
            'protected_hostnames': ['app.example.test'],
            'policies': [{}, {}],
            'existing_policy_ids': ['old-one', 'old-two'],
            'login_idp_types': ['google'],
        }
        valid = {
            'app_id': 'receipt-app',
            'policy_ids': ['new-one', 'new-two'],
            'allowed_idps': ['idp-one'],
            'unresolved_idp_types': [],
            'orphaned_policy_ids': ['old-one'],
        }
        malformed = [None, [], 'body', {}, dict(valid, app_id=True),
                     dict(valid, app_id=''), dict(valid, app_id=None),
                     dict(valid, policy_ids=['new-one']),
                     dict(valid, policy_ids=['new-one', 'new-two', 'new-three']),
                     dict(valid, policy_ids=['same', 'same']),
                     dict(valid, policy_ids=[True, 'new-two']),
                     dict(valid, allowed_idps=None),
                     dict(valid, allowed_idps=[]),
                     dict(valid, allowed_idps=['idp-one', 'idp-two']),
                     dict(valid, allowed_idps=['same', 'same']),
                     dict(valid, unresolved_idp_types=['google']),
                     dict(valid, orphaned_policy_ids=['foreign']),
                     dict(valid, orphaned_policy_ids=['new-one']),
                     dict(valid, success=False),
                     dict(valid, error='provider rejected sync'),
                     dict(valid, status='failed'), dict(valid, status='partial'),
                     dict(valid, partial=True)]
        for body in malformed:
            with self.subTest(body=body):
                with self.assertRaisesRegex(UserError, 'not confirmed'):
                    require_access_sync_receipt(body, payload)

    def test_shared_validator_rejects_malformed_selected_idp_requests(self):
        result = {'app_id': 'receipt-app', 'policy_ids': [],
                  'allowed_idps': ['idp-one'], 'unresolved_idp_types': [],
                  'orphaned_policy_ids': []}
        base = {'enabled': True,
                'protected_hostnames': ['app.example.test'], 'policies': [],
                'existing_policy_ids': []}
        for selected in (None, 'google', [True], [''],
                         ['google', 'google']):
            with self.subTest(selected=selected):
                payload = dict(base, login_idp_types=selected)
                with self.assertRaisesRegex(UserError, 'not confirmed'):
                    require_access_sync_receipt(result, payload)

    def test_shared_validator_accepts_enabled_zero_and_multiple_policies(self):
        zero_payload = {'enabled': True,
                        'protected_hostnames': ['zero.example.test'],
                        'policies': [], 'existing_policy_ids': [],
                        'login_idp_types': []}
        zero = {'app_id': 'zero-app', 'policy_ids': [],
                'allowed_idps': [], 'unresolved_idp_types': [],
                'orphaned_policy_ids': []}
        self.assertIs(require_access_sync_receipt(zero, zero_payload), zero)
        multi_payload = dict(zero_payload, policies=[{}, {}],
                             existing_policy_ids=['old-one'],
                             login_idp_types=['google'])
        multi = dict(zero, app_id='multi-app',
                     policy_ids=['new-one', 'new-two'],
                     allowed_idps=['idp-one'],
                     orphaned_policy_ids=['old-one'])
        self.assertIs(require_access_sync_receipt(multi, multi_payload), multi)

    def test_shared_validator_requires_exact_disabled_wire_and_trusted_orphans(self):
        payload = {'enabled': False, 'protected_hostnames': [], 'policies': [],
                   'existing_policy_ids': ['old-one', 'old-two'],
                   'login_idp_types': []}
        valid = {'app_id': None, 'policy_ids': [], 'allowed_idps': [],
                 'unresolved_idp_types': [],
                 'orphaned_policy_ids': ['old-two']}
        self.assertIs(require_access_sync_receipt(valid, payload), valid)
        missing_app = dict(valid)
        missing_app.pop('app_id')
        for body in (missing_app, dict(valid, app_id='stale-app'),
                     dict(valid, policy_ids=['new-policy']),
                     dict(valid, allowed_idps=['idp']),
                     dict(valid, unresolved_idp_types=['google']),
                     dict(valid, orphaned_policy_ids=['foreign']),
                     dict(valid, orphaned_policy_ids=['old-one', 'old-one']),
                     dict(valid, status='partial'),
                     dict(valid, partial=True)):
            with self.subTest(body=body):
                with self.assertRaisesRegex(UserError, 'not confirmed'):
                    require_access_sync_receipt(body, payload)

    def test_three_teardown_helpers_validate_before_torn_down_and_retry(self):
        malformed = {'app_id': None, 'policy_ids': [], 'allowed_idps': [],
                     'unresolved_idp_types': [],
                     'orphaned_policy_ids': [], 'partial': True}
        valid = {'app_id': None, 'policy_ids': [], 'allowed_idps': [],
                 'unresolved_idp_types': [], 'orphaned_policy_ids': []}
        original = self.client.sync_access_app

        torn_down = []
        self.client.sync_access_app = lambda payload: deepcopy(malformed)
        self.assertFalse(self.cs._teardown_cs_access(
            'old-cs-app', ['old-cs-policy'], 'CS app',
            'cs.example.test', torn_down=torn_down))
        self.assertEqual(torn_down, [])
        self.client.sync_access_app = lambda payload: deepcopy(valid)
        self.assertTrue(self.cs._teardown_cs_access(
            'old-cs-app', ['old-cs-policy'], 'CS app',
            'cs.example.test', torn_down=torn_down))
        self.assertEqual(torn_down, [(self.cs._name, self.cs.id)])

        sa, route = self.sa_resource()
        torn_down = []
        self.client.sync_access_app = lambda payload: deepcopy(malformed)
        self.assertFalse(sa._teardown_route_access(
            'old-sa-app', ['old-sa-policy'], 'SA app', 'sa.example.test',
            anchor_model=route._name, anchor_id=route.id,
            torn_down=torn_down))
        self.assertEqual(torn_down, [])
        self.client.sync_access_app = lambda payload: deepcopy(valid)
        self.assertTrue(sa._teardown_route_access(
            'old-sa-app', ['old-sa-policy'], 'SA app', 'sa.example.test',
            anchor_model=route._name, anchor_id=route.id,
            torn_down=torn_down))
        self.assertEqual(torn_down, [(route._name, route.id)])

        info = {'smart_home': self.sh, 'anchor_model': self.route._name,
                'anchor_id': self.route.id, 'app_id': 'old-sh-app',
                'policy_ids': ['old-sh-policy'], 'app_name': 'SH app',
                'hostname': 'sh.example.test', 'label': 'SH app'}
        torn_down = []
        self.client.sync_access_app = lambda payload: deepcopy(malformed)
        self.assertFalse(self.route._push_access_teardown(
            info, torn_down=torn_down))
        self.assertEqual(torn_down, [])
        self.client.sync_access_app = lambda payload: deepcopy(valid)
        self.assertTrue(self.route._push_access_teardown(
            info, torn_down=torn_down))
        self.assertEqual(torn_down, [(self.route._name, self.route.id)])
        self.client.sync_access_app = original

    def test_three_normal_unprotect_paths_preserve_real_ids_then_converge(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchor = self._anchor_with_policy(kind)
                policy = anchor.access_policy_ids
                old_app = 'old-%s-app' % kind
                old_policy = 'old-%s-policy' % kind
                if kind == 'cs':
                    _internal_cs_access_identity(anchor).access_app_id = old_app
                elif kind == 'sh':
                    _internal_sm_route_identity(anchor).access_app_id = old_app
                else:
                    sa_route_identity(anchor).access_app_id = old_app
                _internal_policy_cf_write(policy).cf_policy_id = old_policy
                self.client.app = True
                self.client.origin = True
                self.client.events.clear()
                self.client.history.clear()
                self.client.access_status = 'partial'
                with self.assertRaisesRegex(UserError, 'not confirmed'):
                    anchor.write({'is_protected': False})
                anchor.invalidate_recordset()
                policy.invalidate_recordset()
                self.assertTrue(anchor.is_protected)
                self.assertEqual(anchor.access_app_id, old_app)
                self.assertEqual(policy.cf_policy_id, old_policy)
                self.assertFalse(self.client.app)
                self.assertFalse(self.client.origin)
                names = self.names()
                self.assertLess(names.index(
                    'park-release' if kind == 'cs' else
                    ('configure' if kind == 'sh' else 'routes')),
                    names.index('access'))
                self.assertNotIn('unpark', names)
                self.client.access_status = None
                self.client.events.clear()
                self.assertTrue(anchor.write({'is_protected': False}))
                anchor.invalidate_recordset()
                policy.invalidate_recordset()
                self.assertFalse(anchor.is_protected)
                self.assertFalse(anchor.access_app_id)
                self.assertFalse(policy.cf_policy_id)
                self.assertTrue(self.client.origin)
                self.assertLess(self.names().index('access'), len(self.names()) - 1)

    def test_three_normal_unprotect_park_failure_never_deletes_access(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchor = self._anchor_with_policy(kind)
                if kind == 'cs':
                    _internal_cs_access_identity(anchor).access_app_id = 'old-cs-app'
                elif kind == 'sh':
                    _internal_sm_route_identity(anchor).access_app_id = 'old-sh-app'
                else:
                    sa_route_identity(anchor).access_app_id = 'old-sa-app'
                self.client.app = True
                self.client.origin = True
                self.client.events.clear()
                self.client.fail_park = True
                with self.assertRaises(Exception):
                    anchor.write({'is_protected': False})
                anchor.invalidate_recordset()
                self.assertTrue(anchor.is_protected)
                self.assertTrue(self.client.app)
                self.assertNotIn('access', self.names())
                self.client.fail_park = False

    def test_three_normal_unprotect_release_failure_is_pending_and_retries(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchor = self._anchor_with_policy(kind)
                if kind == 'cs':
                    _internal_cs_access_identity(anchor).access_app_id = 'old-cs-app'
                elif kind == 'sh':
                    _internal_sm_route_identity(anchor).access_app_id = 'old-sh-app'
                else:
                    sa_route_identity(anchor).access_app_id = 'old-sa-app'
                self.client.app = True
                self.client.origin = True
                self.client.events.clear()
                self.fail_publish(kind)
                self.assertTrue(anchor.write({'is_protected': False}))
                anchor.invalidate_recordset()
                self.assertFalse(anchor.is_protected)
                self.assertFalse(anchor.access_app_id)
                self.assertTrue(anchor.access_publish_pending)
                self.assertFalse(self.client.app)
                self.assertFalse(self.client.origin)
                self.fail_publish(kind, False)
                self.client.events.clear()
                self.assertTrue(anchor.write({'is_protected': False}))
                anchor.invalidate_recordset()
                self.assertFalse(anchor.access_publish_pending)
                self.assertTrue(self.client.origin)

    def test_three_callers_accept_enabled_zero_policy_receipt(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchor = self.anchor(kind)
                if kind == 'cs':
                    _cs_skip_access_sync(anchor).write({
                        'is_protected': True, 'login_google': False,
                        'login_email_otp': False})
                    self.cs_active()
                elif kind == 'sh':
                    _sm_skip_access_sync(anchor).write({
                        'is_protected': True, 'login_google': False,
                        'login_email_otp': False})
                    self.sh_active()
                else:
                    _skip_route_sync(anchor).write({
                        'is_protected': True, 'login_google': False,
                        'login_email_otp': False})
                    self.client.origin = True
                app_id = 'zero-%s-app' % kind
                self.client.sync_access_app = lambda payload, app_id=app_id: {
                    'app_id': app_id, 'policy_ids': [], 'allowed_idps': [],
                    'unresolved_idp_types': [], 'orphaned_policy_ids': [],
                }
                self.assertTrue(anchor._sync_access_to_operator())
                self.assertEqual(anchor.access_app_id, app_id)
                self.assertFalse(anchor.access_publish_pending)

    def test_three_callers_reject_dropped_selected_idp_before_release(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchor = self.anchor(kind)
                if kind == 'cs':
                    _cs_skip_access_sync(anchor).is_protected = True
                    self.cs_active()
                elif kind == 'sh':
                    _sm_skip_access_sync(anchor).is_protected = True
                    self.sh_active()
                else:
                    _skip_route_sync(anchor).is_protected = True
                    self.client.origin = True
                self.client.events.clear()
                self.client.sync_access_app = lambda payload: {
                    'app_id': 'dropped-idp-app', 'policy_ids': [],
                    'allowed_idps': [], 'unresolved_idp_types': [],
                    'orphaned_policy_ids': [],
                }
                with self.assertRaisesRegex(UserError, 'not confirmed'):
                    anchor._sync_access_to_operator()
                anchor.invalidate_recordset()
                self.assertFalse(anchor.access_app_id)
                self.assertNotIn('unpark', self.names())
                if kind == 'sa':
                    self.assertEqual(
                        self.names().count('routes'), 1,
                        'SA must perform exactly one park and no routes release')
                else:
                    self.assertNotIn('routes', self.names())

    def test_three_callers_map_complete_ordered_policy_receipt(self):
        for kind in ('cs', 'sh', 'sa'):
            with self.subTest(kind=kind):
                anchor = self._anchor_with_policy(kind, policy_count=2)
                ids = ['%s-policy-one' % kind, '%s-policy-two' % kind]
                self.client.sync_access_app = lambda payload, ids=ids: {
                    'app_id': '%s-ordered-app' % kind,
                    'policy_ids': list(ids), 'allowed_idps': ['idp-one'],
                    'unresolved_idp_types': [], 'orphaned_policy_ids': [],
                }
                self.assertTrue(anchor._sync_access_to_operator())
                policies = anchor.access_policy_ids.sorted(
                    key=lambda policy: (policy.sequence, policy.id))
                self.assertEqual(policies.mapped('cf_policy_id'), ids)
                self.assertFalse(anchor.access_publish_pending)
