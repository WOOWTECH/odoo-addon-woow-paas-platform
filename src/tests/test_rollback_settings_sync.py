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


@tagged('post_install', '-at_install')
class TestRollbackImageLedgerSync(TransactionCase):
    """AI-OMN-005-2：回復後範本管理的 image 值也要回到目標 revision。

    Omnigent 246 實測：rev5→rev2 回復後叢集跑 20261007.0720，但帳本仍是
    20261014.0731——之後租戶儲存設定（沿用帳本 image，#1408）會悄悄前滾。
    Omnigent 的 spec 只有 password 型鍵，舊版 public_paths 是空集合，連 operator
    都不問就宣告同步完成。
    """

    OLD = '20261007.0720@sha256:' + 'a' * 64
    NEW = '20261014.0731@sha256:' + 'b' * 64
    HOST = 'jcr-prod.woowtech.io/woow-paas-docker-local/omnigent-host'
    REPO = 'jcr-prod.woowtech.io/woow-paas-docker-local/omnigent-server-kubernetes'

    def setUp(self):
        super().setUp()
        workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Rollback image QA', 'owner_id': self.env.ref('base.user_admin').id})
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Rollback image QA', 'slug': 'qa-rb-image', 'category': 'ai_llm',
            'helm_repo_url': '', 'helm_chart_name': 'oci://qa/omnigent', 'helm_chart_version': '0.1.7',
            'helm_value_specs': json.dumps({'required': [], 'optional': [
                {'key': 'auth.openrouterApiKey', 'type': 'password'},
            ]}), 'mcp_enabled': False, 'helm_default_values': '{}'})
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'Rollback image QA', 'workspace_id': workspace.id, 'template_id': self.template.id,
            'state': 'upgrading', 'helm_namespace': 'paas-ws-qa', 'helm_release_name': 'svc-qa-img',
            'helm_chart_version': '0.1.7', 'last_upgraded_at': fields.Datetime.now(),
            'helm_values': json.dumps({
                'image': {'repository': self.REPO, 'tag': self.NEW},
                'runner': {'image': self.HOST + ':' + self.NEW},
                'auth': {'openrouterApiKey': 'current-synthetic-key'},
                'persistence': {'size': '5Gi'},
            })})
        self.service._record_rollback_config_sync(7)
        self.client = Mock()
        self.client.get_config_projection.return_value = {
            'revision': 7,
            'values': {'image.repository': self.REPO, 'image.tag': self.OLD,
                       'runner.image': self.HOST + ':' + self.OLD},
            'missing': ['image.registry', 'image.digest'],
        }

    def test_rollback_writes_target_revision_images_back_to_ledger(self):
        self.assertTrue(self.service._sync_rollback_config(self.client, {'revision': 7}))
        kwargs = self.client.get_config_projection.call_args.kwargs
        self.assertEqual(kwargs['revision'], 7)
        self.assertEqual(set(kwargs['paths']), {
            'image.registry', 'image.repository', 'image.tag', 'image.digest', 'runner.image'})
        values = self.service._helm_values_dict()
        self.assertEqual(values['image'], {'repository': self.REPO, 'tag': self.OLD})
        self.assertEqual(values['runner']['image'], self.HOST + ':' + self.OLD)
        # 憑證與非 image 的範本值不動。
        self.assertEqual(values['auth']['openrouterApiKey'], 'current-synthetic-key')
        self.assertEqual(values['persistence'], {'size': '5Gi'})
        self.assertFalse(self.service.rollback_config_sync)

    def test_tenant_owned_image_key_is_not_projected_as_template_image(self):
        self.template.write({'helm_value_specs': json.dumps({'optional': [
            {'key': 'auth.openrouterApiKey', 'type': 'password'},
            {'key': 'image.tag', 'type': 'password'},
        ]})})
        self.service._record_rollback_config_sync(7)
        self.client.get_config_projection.return_value = {
            'revision': 7, 'values': {'image.repository': self.REPO,
                                      'runner.image': self.HOST + ':' + self.OLD},
            'missing': ['image.registry', 'image.digest']}
        self.assertTrue(self.service._sync_rollback_config(self.client, {'revision': 7}))
        self.assertNotIn('image.tag', self.client.get_config_projection.call_args.kwargs['paths'])
        self.assertEqual(self.service._helm_values_dict()['image']['tag'], self.NEW)

    def test_non_string_image_value_keeps_pending(self):
        pending = self.service.rollback_config_sync
        self.client.get_config_projection.return_value = {
            'revision': 7,
            'values': {'image.repository': self.REPO, 'image.tag': True,
                       'runner.image': self.HOST + ':' + self.OLD},
            'missing': ['image.registry', 'image.digest']}
        self.assertFalse(self.service._sync_rollback_config(self.client, {'revision': 7}))
        self.assertEqual(self.service.rollback_config_sync, pending)
        self.assertEqual(self.service._helm_values_dict()['image']['tag'], self.NEW)
