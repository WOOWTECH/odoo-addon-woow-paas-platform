"""Known publication failure contract: saved anchors, explicit pending, retry.

No external network. A mock API ACK is not evidence of an edge traffic fence.
"""
from unittest.mock import patch

from odoo.exceptions import AccessError, ValidationError, UserError
from odoo.tests.common import TransactionCase, tagged
from ..models.cloud_service import _cs_skip_access_sync, _internal_cs_access_identity, _internal_provision
from ..models.smart_home_route import _sm_skip_access_sync, _internal_sm_route_identity
from ..models.security_access import _internal_identity as _sa_identity, _skip_route_sync
from ..services.access_state import access_configuration_ready
from .test_cs_sh_fail_closed_ac1011 import Fixture, TestCsShBoundaryHttpAC1011

SA_CLIENT = 'odoo.addons.woow_paas_platform.models.security_access.get_paas_operator_client'


class PendingFixture(Fixture):
    def sa_resource(self):
        sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'Native pending SA', 'workspace_id': self.workspace.id})
        route = _skip_route_sync(self.env['woow_paas_platform.security_access_route'].sudo()).create({
            'security_access_id': sa.id, 'subdomain_prefix': 'ac1011-pending-own-sa',
            'service_url': 'http://own-native-origin.example.test:80',
            'is_protected': False, 'login_google': True, 'login_email_otp': False,
        }).with_env(self.env)
        _sa_identity(sa).write({'state': 'active', 'tunnel_id': 'native-pending-sa-tunnel',
                               'tunnel_name': 'native-pending-sa-tunnel-name'})
        return sa, route

    def anchor(self, kind):
        if kind == 'cs':
            _cs_skip_access_sync(self.cs).is_protected = False
            return self.cs
        if kind == 'sh':
            self.sh_active()
            _sm_skip_access_sync(self.route).is_protected = False
            return self.route
        return self.sa_resource()[1]

    def fail_publish(self, kind, failed=True):
        if kind == 'cs': self.client.fail_unpark = failed
        else: self.client.fail_publish = failed

    def assert_pending(self, anchor):
        self.assertTrue(anchor.access_publish_pending)
        self.assertFalse(access_configuration_ready(anchor))
        self.assertTrue(anchor.access_app_id)
        self.assertTrue(anchor.is_protected)


@tagged('post_install', '-at_install')
class TestAccessPublishPendingAC1011(PendingFixture, TransactionCase):
    def setUp(self):
        super().setUp(); self.install_fake(); self.resources()
        mock = patch(SA_CLIENT, return_value=self.client); mock.start(); self.addCleanup(mock.stop)

    def saved_pending_and_retry(self, kind):
        anchor = self.anchor(kind); self.client.origin = True; self.fail_publish(kind)
        self.assertTrue(anchor.write({'is_protected': True}))
        anchor.invalidate_recordset(); self.assert_pending(anchor)
        self.assertFalse(self.client.origin)  # fixture only; not a real edge ACK.
        self.fail_publish(kind, False)
        self.assertTrue(anchor.write({'is_protected': True}))
        self.assertFalse(anchor.access_publish_pending)
        self.assertTrue(access_configuration_ready(anchor))
        self.assertTrue(self.client.origin)

    def disable_pending_and_retry(self, kind):
        anchor = self.anchor(kind); self.fail_publish(kind)
        anchor.write({'is_protected': True}); self.assert_pending(anchor)
        # Save explicit public intent; remote app teardown succeeds, publication fails.
        self.assertTrue(anchor.write({'is_protected': False}))
        self.assertFalse(anchor.is_protected); self.assertFalse(anchor.access_app_id)
        self.assertTrue(anchor.access_publish_pending)
        self.assertFalse(access_configuration_ready(anchor))
        self.fail_publish(kind, False)
        anchor.write({'is_protected': False})
        self.assertFalse(anchor.access_publish_pending)
        self.assertTrue(access_configuration_ready(anchor)); self.assertTrue(self.client.origin)

    def test_cs_saved_anchor_pending_retry(self): self.saved_pending_and_retry('cs')
    def test_sh_saved_anchor_pending_retry(self): self.saved_pending_and_retry('sh')
    def test_sa_saved_anchor_pending_retry(self): self.saved_pending_and_retry('sa')
    def test_cs_public_intent_pending_retry(self): self.disable_pending_and_retry('cs')
    def test_sh_public_intent_pending_retry(self): self.disable_pending_and_retry('sh')
    def test_sa_public_intent_pending_retry(self): self.disable_pending_and_retry('sa')

    def test_three_legs_pending_flag_is_server_owned_even_sudo_and_context(self):
        for kind in ('cs', 'sh', 'sa'):
            anchor = self.anchor(kind)
            with self.assertRaisesRegex(UserError, 'immutable'): anchor.sudo().write({'access_publish_pending': True})
            with self.assertRaisesRegex(UserError, 'immutable'):
                anchor.sudo().with_context(woow_cs_access_identity_write=True,
                                          woow_sm_route_access_identity_write=True,
                                          sa_internal_identity_write=True).write({'access_publish_pending': True})
            self.assertFalse(anchor.access_publish_pending)

    def test_pending_default_and_explicit_create_values_are_neutralized(self):
        cs = _internal_provision(self.env['woow_paas_platform.cloud_service']).with_context(
            default_access_publish_pending=True).create({
                'name': 'Native pending create CS', 'workspace_id': self.workspace.id,
                'template_id': self.template.id, 'access_publish_pending': True})
        self.assertFalse(cs.access_publish_pending)
        route = _skip_route_sync(self.env['woow_paas_platform.security_access_route']).with_context(
            default_access_publish_pending=True).create({
                'security_access_id': self.sa_resource()[0].id,
                'subdomain_prefix': 'ac1011-default-pending-sa',
                'service_url': 'http://own.example.test:80',
                'access_publish_pending': True})
        self.assertFalse(route.access_publish_pending)
        shroute = _sm_skip_access_sync(self.env['woow_paas_platform.smart_home_route']).with_context(
            default_access_publish_pending=True).create({
                'smart_home_id': self.sh.id, 'is_primary': False,
                'subdomain_prefix': 'ac1011-default-pending-sh',
                'access_publish_pending': True})
        self.assertFalse(shroute.access_publish_pending)

    def test_pending_cs_generic_expose_builder_stays_private_and_retry_parks(self):
        anchor = self.anchor('cs'); self.fail_publish('cs')
        anchor.write({'is_protected': True}); self.assert_pending(anchor)
        self.assertIsNone(anchor._build_expose_config())
        self.client.events.clear()
        anchor._sync_access_to_operator()
        self.assertEqual(self.names()[0], 'park-release')
        self.assertTrue(anchor.access_publish_pending)

    def test_public_cs_resume_failure_retry_does_not_silently_skip_unpark(self):
        _cs_skip_access_sync(self.cs).is_protected = False
        self.cs.write({'state': 'suspended', 'pre_suspend_state': 'running'})
        self.client.fail_unpark = True; self.cs._do_resume()
        self.assertEqual(self.cs.state, 'error'); self.assertTrue(self.cs.access_publish_pending)
        self.assertEqual(self.cs.pre_suspend_state, 'running')
        self.client.fail_unpark = False; self.client.events.clear(); self.cs._do_resume()
        self.assertEqual(self.cs.state, 'running'); self.assertFalse(self.cs.access_publish_pending)
        self.assertFalse(self.cs.pre_suspend_state); self.assertIn('unpark', self.names())
        self.assertTrue(self.client.origin)

    def test_cs_pending_deploy_success_recomputes_origin_expose(self):
        anchor = self.anchor('cs'); self.client.fail_unpark = True
        anchor.write({'is_protected': True}); self.assert_pending(anchor)
        self.client.fail_unpark = False; anchor.write({'state': 'pending'})
        self.client.events.clear(); anchor._execute_deploy()
        self.assertFalse(anchor.access_publish_pending)
        self.assertIn('install-expose', self.names())
        self.assertLess(self.names().index('access'), self.names().index('install-expose'))
        self.assert_safe_history()

    def test_cs_public_pending_deploy_publish_failure_does_not_install(self):
        anchor = self.anchor('cs'); self.client.fail_unpark = True
        anchor.write({'is_protected': True}); anchor.write({'is_protected': False})
        self.assertTrue(anchor.access_publish_pending); self.assertFalse(anchor.access_app_id)
        anchor.write({'state': 'pending'}); self.client.events.clear()
        anchor._execute_deploy()
        self.assertEqual(anchor.state, 'error'); self.assertTrue(anchor.access_publish_pending)
        self.assertNotIn('install', self.names()); self.assertFalse(self.client.origin)

    def test_cs_late_permission_error_still_raises_and_rolls_back(self):
        anchor = self.anchor('cs')
        with patch.object(type(anchor), '_release_access_origin', side_effect=AccessError('known authorization denial')):
            with self.assertRaises(AccessError): anchor.write({'is_protected': True})
        anchor.invalidate_recordset(); self.assertFalse(anchor.is_protected)
        self.assertFalse(anchor.access_app_id); self.assertFalse(anchor.access_publish_pending)
        self.assertTrue(self.client.app)  # unexpected outer rollback remains a separate recovery gate.

    def test_sh_fresh_permission_error_is_not_absorbed_by_known_protection_catch(self):
        with patch.object(type(self.route), '_sync_access_to_operator', side_effect=AccessError('known authorization denial')):
            with self.assertRaises(AccessError): self.sh.action_provision()

    def test_sh_late_validation_error_still_raises_and_rolls_back(self):
        anchor = self.anchor('sh')
        original = type(self.sh)._sync_access_ingress
        def deny(home, *args, **kwargs):
            if home.route_ids.access_app_id: raise ValidationError('known validation denial')
            return original(home, *args, **kwargs)
        with patch.object(type(self.sh), '_sync_access_ingress', new=deny):
            with self.assertRaises(ValidationError): anchor.write({'is_protected': True})
        anchor.invalidate_recordset(); self.assertFalse(anchor.is_protected)
        self.assertFalse(anchor.access_app_id); self.assertFalse(anchor.access_publish_pending)


@tagged('post_install', '-at_install')
class TestAccessPublishPendingHttpAC1011(TestCsShBoundaryHttpAC1011):
    # Inherited adjacent authorization/preflight cases retain their assertions.
    def test_http_cs_known_publish_failure_saved_but_reports_false(self):
        _cs_skip_access_sync(self.cs).is_protected = False; self.client.fail_unpark = True
        result = self.rpc(f'/api/workspaces/{self.workspace.id}/services/{self.cs.id}/access-config',
                          {'action': 'update', 'is_protected': True})
        self.assertFalse(result['success']); self.assertFalse(result['access_configuration_ready'])
        self.assertTrue(result['data']['access_publish_pending']); self.assertNotIn('security_ready', result)
        self.assertNotIn('remains paused', result['error']); self.assertIn('not confirmed', result['error'])
        self.cs.invalidate_recordset(); self.assertTrue(self.cs.access_app_id)
        self.assertTrue(self.cs.access_publish_pending); self.assertTrue(self.cs.is_protected)

    def test_http_sh_known_publish_failure_saved_but_reports_false(self):
        self.sh_active(); _sm_skip_access_sync(self.route).is_protected = False
        self.client.fail_publish = True
        result = self.rpc(f'/api/workspaces/{self.workspace.id}/smarthomes/{self.sh.id}/routes/{self.route.id}/access-config',
                          {'action': 'update', 'is_protected': True})
        self.assertFalse(result['success']); self.assertFalse(result['access_configuration_ready'])
        self.assertTrue(result['data']['access_publish_pending']); self.route.invalidate_recordset()
        self.assertTrue(self.route.access_app_id); self.assertTrue(self.route.is_protected)

    def test_http_sa_known_publish_failure_saved_but_reports_false(self):
        self.client.fail_publish = True
        mock = patch(SA_CLIENT, return_value=self.client); mock.start(); self.addCleanup(mock.stop)
        sa, route = PendingFixture.sa_resource(self)
        result = self.rpc(f'/api/workspaces/{self.workspace.id}/security-accesses/{sa.id}/routes/{route.id}/access-config',
                          {'action': 'update', 'is_protected': True})
        self.assertFalse(result['success']); self.assertFalse(result['access_configuration_ready'])
        self.assertTrue(result['data']['access_publish_pending']); route.invalidate_recordset()
        self.assertTrue(route.access_app_id); self.assertTrue(route.is_protected)
