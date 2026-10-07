"""#1552：背景 upgrade（修改配置／換方案）失敗時，平台不再「DB 是新值、畫面執行中、叢集沒套用、
沒有任何錯誤」，而是轉 error、附上租戶看得懂的說明，並允許從這個 error 直接重新送出修改配置。

正式站 2026-10-07 13:53：三台服務同時改設定，operator OOMKilled，請求全斷：
- Code Server／LiteLLM 轉 error，訊息是英文原始例外，error 狀態下「無法更新」；
- Nextcloud 的新值已寫進 DB、背景 poll 先一步把它推回 running，之後的失敗被丟掉（靜默遺失）。
"""
import json
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase
from odoo.tools import mute_logger
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.services.paas_operator import (
    OPERATOR_BUSY_ERROR_CODE,
    PaaSOperatorAPIError,
    PaaSOperatorConnectionError,
    PaaSOperatorTimeoutError,
)
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
# 背景失敗路徑本來就會 log ERROR（Background upgrade failed）；測試裡不讓它洗版。
_quiet = mute_logger(_CONTROLLER)


class _NonClosingCursor:
    """背景執行緒的 ``odoo.registry(db).cursor()`` 導回測試 transaction（同 #1456 測試）。"""

    def __init__(self, cr):
        self._cr = cr

    def __enter__(self):
        return self._cr

    def __exit__(self, *exc_info):
        return False


class _SyncThread:

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _ServiceFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1552 Workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': '1552 Template',
            'slug': 'upgrade-failure-1552',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/upgrade-failure-1552',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-1552',
            'workspace_id': self.workspace.id,
            'template_id': self.template.id,
            'state': 'running',
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-1552',
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        })
        self.service.write({'helm_chart_version': '1.0.0', 'helm_revision': 4})

    def _reload(self):
        self.env.invalidate_all()
        return self.service.browse(self.service.id)

    def _run_update(self, client):
        fake_request = MagicMock()
        fake_request.env = self.env
        fake_registry = MagicMock()
        fake_registry.cursor.return_value = _NonClosingCursor(self.env.cr)
        client_path = 'odoo.addons.woow_paas_platform.services.paas_operator.PaaSOperatorClient'
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=fake_request), \
                patch(client_path, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread), \
                patch('odoo.registry', return_value=fake_registry):
            return PaasController()._update_service(self.service, values={}, version=None)

    def _failing_client(self, exc, race_to=None):
        """upgrade_release 失敗；``race_to`` 給值時，失敗前先模擬背景 poll 改了 state。"""
        client = MagicMock()

        def _upgrade(**_kwargs):
            if race_to:
                self.env.flush_all()
                self.env.cr.execute(
                    'UPDATE woow_paas_platform_cloud_service SET state = %s WHERE id = %s',
                    (race_to, self.service.id))
                self.env.invalidate_all()
            raise exc

        client.upgrade_release.side_effect = _upgrade
        return client


def _aborted():
    return PaaSOperatorConnectionError(
        message='Failed to connect to PaaS Operator service',
        detail="('Connection aborted.', RemoteDisconnected('Remote end closed connection "
               "without response'))")


def _busy():
    return PaaSOperatorAPIError(
        message='API error', status_code=503,
        detail={'error_code': OPERATOR_BUSY_ERROR_CODE, 'message': '部署服務忙碌', 'retryable': True})


class TestBackgroundUpgradeFailure(_ServiceFixture):

    @_quiet
    def test_connection_lost_while_upgrading_becomes_retryable_error(self):
        result = self._run_update(self._failing_client(_aborted()))
        self.assertTrue(result.get('success'), result)
        svc = self._reload()
        self.assertEqual(svc.state, 'error')
        self.assertTrue(svc.upgrade_failed_at)
        self.assertIn('not certain whether the new settings were applied', svc.error_message)
        self.assertIn('Submit the configuration again', svc.error_message)
        self.assertNotIn('RemoteDisconnected', svc.error_message,
                         '原始例外不再原樣給租戶看')

    @_quiet
    def test_poll_won_the_race_failure_is_no_longer_silent(self):
        """正式站 Nextcloud：poll 先推回 running，之後的失敗以前被丟掉。"""
        self._run_update(self._failing_client(_aborted(), race_to='running'))
        svc = self._reload()
        self.assertEqual(svc.state, 'error', 'DB 已是新值、叢集沒套用時不得顯示執行中')
        self.assertTrue(svc.upgrade_failed_at)

    @_quiet
    def test_busy_operator_says_nothing_was_applied(self):
        self._run_update(self._failing_client(_busy()))
        svc = self._reload()
        self.assertEqual(svc.state, 'error')
        self.assertIn('were not applied', svc.error_message)

    def test_timeout_and_operator_detail_messages(self):
        svc = self.service
        timeout = PaaSOperatorTimeoutError(message='timed out', detail='read timeout')
        self.assertIn('did not answer in time', svc._upgrade_failure_message(timeout))
        helm = PaaSOperatorAPIError(message='API error', status_code=500,
                                    detail='Helm 升級失敗：找不到指定版本的 chart')
        msg = svc._upgrade_failure_message(helm)
        self.assertIn('Helm 升級失敗', msg, 'operator 的中文說明要帶到租戶面前')
        self.assertIn('could not apply the new settings', msg)

    @_quiet
    def test_newer_attempt_is_not_flipped_by_an_older_failure(self):
        client = self._failing_client(_aborted())
        original = client.upgrade_release.side_effect

        def _newer_attempt_started(**kwargs):
            self.env.flush_all()
            later = fields.Datetime.now() + timedelta(seconds=30)
            self.env.cr.execute(
                'UPDATE woow_paas_platform_cloud_service SET last_upgraded_at = %s, state = %s '
                'WHERE id = %s', (later, 'running', self.service.id))
            self.env.invalidate_all()
            return original(**kwargs)

        client.upgrade_release.side_effect = _newer_attempt_started
        self._run_update(client)
        svc = self._reload()
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.upgrade_failed_at)

    @_quiet
    def test_initializing_is_left_to_the_init_lifecycle(self):
        self._run_update(self._failing_client(_aborted(), race_to='initializing'))
        self.assertEqual(self._reload().state, 'initializing')

    @_quiet
    def test_suspended_service_keeps_suspended_semantics(self):
        """#482：停權中的服務失敗仍停著，只記錯誤訊息，不開放 error 出口。"""
        self.service.sudo().write({'state': 'suspended', 'pre_suspend_state': 'running'})
        with patch.object(type(self.service), '_is_halted', return_value=True):
            self._run_update(self._failing_client(_aborted()))
        svc = self._reload()
        self.assertEqual(svc.state, 'suspended')
        self.assertTrue(svc.error_message)
        self.assertFalse(svc.upgrade_failed_at)

    def test_message_uses_service_creator_language(self):
        self.env['res.lang']._activate_lang('zh_TW')
        self.service.create_uid.sudo().lang = 'zh_TW'
        msg = self.service._upgrade_failure_message(_aborted())
        self.assertIn('連線中斷', msg)
        self.assertIn('儲存變更', msg)


class TestRetryFromUpgradeFailure(_ServiceFixture):

    def _mark_failed(self):
        self.service.sudo().write({
            'state': 'error', 'error_message': 'x',
            'upgrade_failed_at': fields.Datetime.now()})

    def test_reconfigure_allowed_from_upgrade_failure_error(self):
        self._mark_failed()
        self.assertTrue(self.service._upgrade_failure_reconfigure_exit())
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 5}
        result = self._run_update(client)
        self.assertTrue(result.get('success'), result)
        svc = self._reload()
        self.assertEqual(svc.state, 'upgrading')
        self.assertFalse(svc.upgrade_failed_at, '離開 error 時標記一起清掉')
        self.assertFalse(svc.error_message)
        client.upgrade_release.assert_called_once()

    def test_other_errors_still_refuse_reconfigure(self):
        self.service.sudo().write({'state': 'error', 'error_message': 'install failed'})
        client = MagicMock()
        result = self._run_update(client)
        self.assertFalse(result.get('success'))
        client.upgrade_release.assert_not_called()
        self.assertEqual(self._reload().state, 'error')

    def test_marker_follows_the_error_message_source(self):
        self._mark_failed()
        self.service.sudo().write({'error_message': 'deletion failed'})
        self.assertFalse(self.service.upgrade_failed_at,
                         '別的來源改寫 error_message ⇒ 不再是升級失敗的 error')

    def test_marker_survives_suspension_round_trip(self):
        self._mark_failed()
        self.service.sudo().write({'state': 'suspended'})
        self.assertTrue(self.service.upgrade_failed_at)
        self.service.sudo().write({'state': 'error'})
        self.assertTrue(self.service._upgrade_failure_reconfigure_exit())

    def test_tenant_cannot_write_the_marker(self):
        user = self.env['res.users'].create({
            'name': '1552 tenant', 'login': 'tenant_1552',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaisesRegex(AccessError, 'Upgrade failure fields'):
            self.service.with_user(user).write({'upgrade_failed_at': fields.Datetime.now()})

    def test_payload_mirrors_the_guard(self):
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            payload = PaasController()._format_service(self.service, include_details=True)
        self.assertIs(payload['upgrade_retry_allowed'], False)
        self._mark_failed()
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            payload = PaasController()._format_service(self.service, include_details=True)
        self.assertIs(payload['upgrade_retry_allowed'], True)


class TestUpgradeFailureTranslations(TransactionCase):

    def test_new_messages_have_traditional_chinese(self):
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        entries = {e['src']: e['value'] for e in PoFileReader(po)}
        for src in (
            'The connection to the deployment service was interrupted, so it is not '
            'certain whether the new settings were applied.',
            'The deployment service did not answer in time, so it is not certain '
            'whether the new settings were applied.',
            'The deployment service was busy with other changes, so the new settings '
            'were not applied.',
            'The deployment service could not apply the new settings: %(detail)s',
            '%(reason)s Submit the configuration again to retry. If it keeps failing, use '
            'Rollback or contact support.',
            'The plan was changed, but resizing the service did not complete. '
            '%(reason)s Submit the configuration again to retry, or contact support.',
            'Upgrade failure fields are managed by the platform: %(fields)s',
        ):
            with self.subTest(src=src[:40]):
                self.assertTrue(entries.get(src), 'zh_TW.po 缺翻譯：%r' % src)
