"""需在 Odoo 18 sandbox 執行：真 ORM、savepoint 與固定 revision 同步。"""
import json
from datetime import timedelta
from unittest.mock import Mock, patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorError


@tagged('post_install', '-at_install')
class TestRollbackSettingsSync(TransactionCase):
    def setUp(self):
        super().setUp()
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Rollback config QA', 'owner_id': self.env.ref('base.user_admin').id})
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Rollback config QA', 'slug': 'qa-rb-config', 'category': 'ai_llm',
            'helm_repo_url': '', 'helm_chart_name': 'oci://qa/chart', 'helm_chart_version': '0.1.7',
            'helm_value_specs': json.dumps({'optional': [
                {'key': 'config.timezone', 'type': 'text'},
                {'key': 'config.ws', 'type': 'boolean'},
                {'key': 'auth.password', 'type': 'password'},
            ]}), 'mcp_enabled': False, 'helm_default_values': '{}'})
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'Rollback config QA', 'workspace_id': workspace.id, 'template_id': template.id,
            'state': 'upgrading', 'helm_namespace': 'paas-ws-qa', 'helm_release_name': 'svc-qa',
            'helm_chart_version': '0.1.7', 'last_upgraded_at': fields.Datetime.now(),
            'helm_values': json.dumps({'config': {'timezone': 'UTC', 'ws': False},
                                       'auth': {'password': 'current-synthetic-secret'}})})
        self.service._record_rollback_config_sync(3)
        self.client = Mock()
        self.client.get_config_projection.return_value = {
            'revision': 3, 'values': {'config.timezone': 'Asia/Taipei', 'config.ws': True}, 'missing': []}

    def test_real_row_lock_updates_public_settings_keeps_credentials(self):
        self.assertTrue(self.service._sync_rollback_config(self.client, {'revision': 3}))
        values = self.service._helm_values_dict()
        self.assertEqual(values['config'], {'timezone': 'Asia/Taipei', 'ws': True})
        self.assertEqual(values['auth']['password'], 'current-synthetic-secret')
        self.assertFalse(self.service.rollback_config_sync)

    def test_failed_operator_keeps_pending_for_retry(self):
        pending = self.service.rollback_config_sync
        self.client.get_config_projection.side_effect = PaaSOperatorError('synthetic timeout')
        self.assertFalse(self.service._sync_rollback_config(self.client, {'revision': 3}))
        self.assertEqual(self.service.rollback_config_sync, pending)
        self.client.get_config_projection.side_effect = None
        self.assertTrue(self.service._sync_rollback_config(self.client, {'revision': 3}))

    def test_new_config_and_new_command_cancel_pending(self):
        self.service._apply_helm_values({'config': {'timezone': 'Europe/Paris'}})
        self.assertFalse(self.service.rollback_config_sync)
        self.service._record_rollback_config_sync(4)
        self.service.write({'last_upgraded_at': fields.Datetime.now()})
        self.assertFalse(self.service.rollback_config_sync)

    def test_write_failure_rolls_back_values_and_pending(self):
        original, pending = self.service._helm_values_dict(), self.service.rollback_config_sync
        original_apply = type(self.service)._apply_helm_values
        def fail_after_write(record, *args, **kwargs):
            original_apply(record, *args, **kwargs)
            raise ValueError('synthetic write failure')
        with patch.object(type(self.service), '_apply_helm_values', fail_after_write):
            try:
                self.service._sync_rollback_config(self.client, {'revision': 3})
            except ValueError:
                pass
            else:
                self.fail('synthetic write failure was swallowed')
        self.assertEqual(self.service._helm_values_dict(), original)
        self.assertEqual(self.service.rollback_config_sync, pending)

    def test_wrong_revision_does_not_query_projection(self):
        self.assertFalse(self.service._sync_rollback_config(self.client, {'revision': 4}))
        self.client.get_config_projection.assert_not_called()

    def test_sync_failure_times_out_and_later_recovers_with_synchronized_settings(self):
        self.service.write({'last_upgraded_at': fields.Datetime.now() - timedelta(hours=1)})
        self.service._record_rollback_config_sync(3)
        pending = self.service.rollback_config_sync
        self.client.get_status.return_value = {'release': {'status': 'deployed', 'revision': 3}}
        self.client.get_config_projection.side_effect = PaaSOperatorError('synthetic timeout')
        with patch.object(type(self.service), '_public_url_dns_state', return_value='skip'), \
                patch.object(type(self.service), '_probe_public_tunnel', return_value=(None, None)), \
                patch.object(type(self.service), '_is_release_ready', return_value=(True, '')), \
                patch.object(type(self.service), '_transition_timeout_minutes', return_value=1), \
                mute_logger('odoo.addons.woow_paas_platform.models.cloud_service'):
            self.service._poll_and_update_status(self.client)
            self.assertEqual(self.service.state, 'error')
            self.assertTrue(self.service.transition_timeout_at)
            self.assertEqual(self.service.rollback_config_sync, pending)
            self.client.get_config_projection.side_effect = None
            self.service._poll_and_update_status(self.client)
        self.assertEqual(self.service.state, 'running')
        self.assertFalse(self.service.transition_timeout_at)
        self.assertFalse(self.service.rollback_config_sync)
        self.assertEqual(self.service._helm_values_dict()['config']['timezone'], 'Asia/Taipei')

    def test_pending_field_denied_to_authorized_workspace_member(self):
        user = new_test_user(self.env, login='qa-rb-member', groups='base.group_user')
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.service.workspace_id.id, 'user_id': user.id, 'role': 'user'})
        member_service = self.service.with_user(user)
        self.assertFalse(member_service.env.is_superuser())
        self.assertEqual(member_service.read(['name'])[0]['name'], self.service.name)
        with self.assertRaises(AccessError):
            member_service.read(['rollback_config_sync'])
