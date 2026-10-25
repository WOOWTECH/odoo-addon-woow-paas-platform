"""Administrator pins are isolated from product defaults and tenant inputs."""
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import HttpCase, tagged

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorError
from .ns_quota_fixture import answer_quota_reads
from .test_redeploy_images_1408 import _Base, _CLIENT_PATH, _NEW
from .test_template_id_write_guard_auditfix import _group_user


_VERSION = '0.5.8-paseo.2+build.7'
_TAG = 'candidate@sha256:' + 'a' * 64
_REF = {'repository': 'registry.example/open-design', 'tag': _TAG}
_PIN = {'deployment_policy': 'pinned', 'deployment_chart_version': _VERSION,
        'deployment_image_overrides': {'image': _REF}}
_UNPIN = {'deployment_policy': 'template', 'deployment_chart_version': False,
          'deployment_image_overrides': {}}


@tagged('post_install', '-at_install')
class TestServiceDeploymentPin(_Base):
    def test_default_still_follows_template(self):
        self.assertEqual(self.service.deployment_policy, 'template')
        client = self._redeploy()
        self.assertEqual(client.upgrade_release.call_args.kwargs['version'], '0.1.4')
        self.assertEqual(self._values()['image']['tag'], _NEW)

    def test_admin_pin_preserves_features_and_does_not_change_peer_or_template(self):
        peer = _internal_provision(self.env[self.service._name]).create({
            'name': 'peer-148', 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'helm_values': self.service.helm_values,
        })
        template_before = self.template.helm_default_values
        values = self._values()
        values.update({'paseo': {'enabled': True}, 'omnigentHost': {'enabled': True},
                       'mcp': {'enabled': False}})
        self.service.write({'helm_values': json.dumps(values)})
        self.service.with_user(self.env.ref('base.user_admin')).write(_PIN)
        client = self._redeploy({'revision': 30, 'chart_version': _VERSION})
        sent = client.upgrade_release.call_args.kwargs
        self.assertEqual(sent['version'], _VERSION)
        self.assertTrue(sent['reset_values'])
        self.assertEqual(sent['values']['image'], _REF)
        for key in ('paseo', 'omnigentHost', 'mcp'):
            self.assertEqual(sent['values'][key], values[key])
        self.assertEqual(self._values()['image'], _REF)
        self.assertEqual(self.service.helm_chart_version, _VERSION)
        self.assertFalse(self.service._redeploy_chart_check_message(_VERSION))
        self.assertTrue(self.service._redeploy_chart_check_message('0.1.4'))
        self.assertEqual(peer._resolve_deployment_target()['version'], '0.1.4')
        self.assertEqual(peer.deployment_policy, 'template')
        self.assertEqual(self.template.helm_default_values, template_before)

    def test_fresh_install_uses_same_pin(self):
        self.service.write(_PIN)
        self.service.write({'state': 'pending'})
        client = MagicMock()
        answer_quota_reads(client)
        client.install_release.return_value = {'revision': 1, 'chart_version': _VERSION}
        with patch(_CLIENT_PATH, return_value=client):
            self.service._execute_deploy()
        sent = client.install_release.call_args.kwargs
        self.assertEqual(sent['version'], _VERSION)
        self.assertEqual(sent['values']['image'], _REF)
        self.assertEqual(self._values()['image'], _REF)

    def test_unpin_restores_template_on_next_redeploy(self):
        self.service.write(_PIN)
        self._redeploy({'revision': 30, 'chart_version': _VERSION})
        self.service.write(_UNPIN)
        client = self._redeploy()
        self.assertEqual(client.upgrade_release.call_args.kwargs['version'], '0.1.4')
        self.assertEqual(self._values()['image']['tag'], _NEW)

    def test_repeated_redeploy_keeps_pin_after_product_changes(self):
        self.service.write(_PIN)
        self._redeploy({'revision': 30, 'chart_version': _VERSION})
        self.template.write({'helm_chart_version': '0.9.0'})
        client = self._redeploy({'revision': 31, 'chart_version': _VERSION})
        self.assertEqual(client.upgrade_release.call_args.kwargs['version'], _VERSION)
        self.assertEqual(self._values()['image'], _REF)

    def test_failed_redeploy_rolls_back_image_refresh_but_keeps_pin(self):
        self.service.write(_PIN)
        before = self._values()
        with self.assertRaises(UserError), self.env.cr.savepoint():
            self._redeploy(side_effect=PaaSOperatorError(
                'API error', status_code=409, detail='Synthetic conflict'))
        self.assertEqual(self._values(), before)
        self.assertEqual(self.service.deployment_policy, 'pinned')

    def test_invalidated_pin_stops_fresh_install_with_clear_error(self):
        self.service.write(_PIN)
        self.template.write({'helm_default_values': '{}'})
        self.service.write({'state': 'pending'})
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.service._execute_deploy()
        client.install_release.assert_not_called()
        client.create_namespace.assert_not_called()
        self.assertEqual(self.service.state, 'error')
        self.assertIn('existing product image path', self.service.error_message)

    def test_queued_redeploy_resolves_pin_at_execution(self):
        self.service.write(_PIN)
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client), \
                patch.object(type(self.service), '_init_lease_active', return_value=True):
            self.service.action_redeploy()
        self.assertTrue(self.service.init_redeploy_requested)
        client.upgrade_release.assert_not_called()
        self.service.write({'deployment_chart_version': '0.5.8-paseo.3'})
        client = self._redeploy({'revision': 31, 'chart_version': '0.5.8-paseo.3'})
        self.assertEqual(client.upgrade_release.call_args.kwargs['version'], '0.5.8-paseo.3')

    def test_invalid_pins_are_rejected_before_operator_calls(self):
        invalid = [dict(_PIN, deployment_chart_version='latest'),
                   dict(_PIN, deployment_image_overrides={'paseo': {'enabled': True}}),
                   dict(_PIN, deployment_image_overrides={'image': dict(_REF, tag='latest')})]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.service.write(values)

    def test_context_cannot_default_new_service_to_pin_even_under_sudo(self):
        service = _internal_provision(self.env[self.service._name].sudo().with_context(
            **{'default_' + k: v for k, v in _PIN.items()})).create({
                'name': 'context-default-test', 'workspace_id': self.workspace.id,
                'template_id': self.template.id,
            })
        self.assertEqual(service.deployment_policy, 'template')
        self.assertFalse(service.deployment_chart_version)
        self.assertFalse(service.deployment_image_overrides)

    def test_tenant_values_cannot_select_deployment_target(self):
        values = self._values()
        values.update({'deployment_policy': 'pinned', 'deployment_chart_version': '9.9.9',
                       'deployment_image_overrides': {'image': _REF}})
        self.service.write({'helm_values': json.dumps(values)})
        client = self._redeploy()
        sent = client.upgrade_release.call_args.kwargs
        self.assertEqual(sent['version'], '0.1.4')
        self.assertEqual(sent['values']['image']['tag'], _NEW)

    def test_non_admin_read_write_denied_and_field_metadata_hidden(self):
        owner, _login = _group_user(self.env, 'pin-owner')
        workspace = self.env['woow_paas_platform.workspace'].with_user(owner).create(
            {'name': 'pin-owned-workspace'})
        service = _internal_provision(self.env[self.service._name]).create({
            'name': 'pin-owned-service', 'workspace_id': workspace.id,
            'template_id': self.template.id,
        })
        tenant_service = service.with_user(owner)
        tenant_service.write({'name': 'positive-control'})
        self.assertFalse(set(_PIN).intersection(tenant_service.fields_get()))
        for name, value in _PIN.items():
            with self.subTest(field=name), self.assertRaises(AccessError):
                tenant_service.write({name: value})
        with self.assertRaises(AccessError):
            tenant_service.read(list(_PIN))


@tagged('post_install', '-at_install')
class TestServiceDeploymentPinRpc(HttpCase):
    def test_owner_cannot_write_pin_fields_over_rpc(self):
        owner, login = _group_user(self.env, 'pin-rpc-owner')
        workspace = self.env['woow_paas_platform.workspace'].with_user(owner).create(
            {'name': 'pin-rpc-workspace'})
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'pin-rpc-service', 'workspace_id': workspace.id,
            'template_id': self.env.ref('woow_paas_platform.cloud_app_opendesign').id,
        })
        self.authenticate(login, login)
        for values in [{'name': 'positive-control'}, _PIN]:
            response = self.url_open('/web/dataset/call_kw', data=json.dumps({
                'jsonrpc': '2.0', 'method': 'call', 'params': {
                    'model': service._name, 'method': 'write',
                    'args': [[service.id], values], 'kwargs': {},
                }}), headers={'Content-Type': 'application/json'}).json()
            if 'name' in values:
                self.assertTrue(response.get('result'), response)
            else:
                self.assertIn('error', response)
        service.invalidate_recordset()
        self.assertEqual(service.deployment_policy, 'template')
