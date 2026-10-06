# -*- coding: utf-8 -*-
"""#1416：部署逾時轉 error 後，release 之後就緒要能自己恢復。

2026-10-04 正式站 svc 147（Nextcloud）：01:47 被 #489 的 age timeout 打成 ``error``，02:03
Longhorn 額度放出後 pod 2/2 Ready、功能全正常，服務頁卻一直顯示「錯誤／離線」到刪除為止——
短 poll 只撈轉移狀態，寫進 error 之後沒有任何路徑會再看它（``cloud_service.py`` 的註解自己也寫了
「誤殺不可自癒」）。逾時只代表「等太久」，不代表失敗。

守的事：

* 逾時轉 error 時記下來源與時間（``transition_timeout_at``）；
* ``_cron_recover_timed_out_services`` 低頻重查「逾時造成、時間窗內、未停權、有 release」的
  error 服務，就緒就走**同一條**推進路徑（要 init 的進 initializing，否則 running），清掉
  error_message 與標記，並在 Platform Monitor 留紀錄；
* 其他來源的 error（operator 回 failed、刪除失敗、停權…）完全不碰：標記只在逾時那一刻寫，
  任何改寫 error_message 或離開 error（停權除外）的寫入都會清掉它；
* optimistic locking 照舊；時間窗可用 ``ir.config_parameter`` 調，缺值／亂填退回預設。
"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import (
    TIMEOUT_RECOVERY_WINDOW_HOURS_DEFAULT,
    _internal_provision,
)
from ..services.paas_operator import PaaSOperatorAPIError
from .operator_contract import pod_info, status_response

_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
_PARAM = 'woow_paas_platform.timeout_recovery_window_hours'
_TIMEOUT_MINUTES = 30

_STUCK = [pod_info(name='svc-1416-0', phase='Pending', ready='0/2', waiting={
    'container': 'volume-permissions', 'init_container': True,
    'reason': 'PodInitializing', 'message': None})]
_READY = [pod_info(name='svc-1416-0', phase='Running', ready='2/2')]


@tagged('post_install', '-at_install')
class TestTimeoutRecovery1416(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Service = cls.env['woow_paas_platform.cloud_service']
        Template = cls.env['woow_paas_platform.cloud_app_template']
        common = {
            'helm_repo_url': 'https://charts.example.com', 'helm_chart_name': 'chart',
            'helm_chart_version': '1.0.0', 'workload_type': 'container', 'mcp_enabled': False,
        }
        cls.template = Template.create(dict(
            common, name='Recover 1416', slug='recover-1416', post_deploy_init_type='none'))
        cls.init_template = Template.create(dict(
            common, name='Recover 1416 odoo', slug='recover-1416-odoo',
            post_deploy_init_type='odoo'))
        cls.workspace = cls.env['woow_paas_platform.workspace'].create({'name': 'WS 1416'})

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().search([
            ('key', 'in', [_PARAM, 'woow_paas_platform.deploy_timeout_minutes'])]).unlink()

    # ------------------------------------------------------------------ #
    #  helpers                                                             #
    # ------------------------------------------------------------------ #

    def _service(self, template=None, tag='a'):
        return _internal_provision(self.Service).create({
            'name': 'svc-1416-%s' % tag, 'workspace_id': self.workspace.id,
            'template_id': (template or self.template).id, 'state': 'deploying',
            'helm_namespace': 'paas-ws-test-1416', 'helm_release_name': 'svc-1416-%s' % tag,
            'deployed_at': fields.Datetime.now() - timedelta(minutes=_TIMEOUT_MINUTES + 1),
        })

    def _poll(self, svc, body):
        client = MagicMock()
        client.get_status.return_value = body
        svc._poll_and_update_status(client)
        svc.invalidate_recordset()
        return client

    def _timed_out(self, template=None, tag='a'):
        svc = self._service(template, tag)
        self._poll(svc, status_response(pods=_STUCK))
        self.assertEqual(svc.state, 'error', '前提：這一輪應該逾時')
        self.assertTrue(svc.transition_timeout_at)
        return svc

    def _recover(self, body=None, side_effect=None):
        """跑自癒 cron（mock operator；TransactionCase 禁 commit ⇒ no-op）。"""
        client = MagicMock()
        if side_effect is not None:
            client.get_status.side_effect = side_effect
        else:
            client.get_status.return_value = body
        with patch(_CLIENT, return_value=client), \
                patch.object(self.env.cr, 'commit', lambda: None):
            self.Service._cron_recover_timed_out_services()
        self.Service.invalidate_model()
        return client

    @staticmethod
    def _polled(client):
        return [c.kwargs.get('release_name') for c in client.get_status.call_args_list]

    def _monitor_notes(self):
        monitor = self.env['woow_paas_platform.platform_monitor']._singleton()
        return monitor.message_ids.mapped('body')

    # ------------------------------------------------------------------ #
    #  自癒                                                                #
    # ------------------------------------------------------------------ #

    def test_ready_release_recovers_to_running(self):
        """🔴 10-04 的形狀：逾時之後 release 就緒 ⇒ 回 running、訊息與標記清掉、留紀錄。"""
        svc = self._timed_out()
        timed_out_at = svc.transition_timeout_at

        client = self._recover(status_response(pods=_READY))

        self.assertIn(svc.helm_release_name, self._polled(client))
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.error_message)
        self.assertFalse(svc.transition_timeout_at)
        notes = [b for b in self._monitor_notes() if 'svc-1416-a' in str(b)]
        self.assertTrue(notes, 'Platform Monitor 沒有留下自癒紀錄')
        self.assertIn(str(timed_out_at), str(notes[0]))
        self.assertIn('running', str(notes[0]))

    def test_owed_init_goes_through_initializing(self):
        """需要 post-deploy init 的服務照正常路徑進 initializing（不跳過 init）。"""
        svc = self._timed_out(self.init_template, tag='init')
        self.assertTrue(svc.init_pending)

        self._recover(status_response(pods=_READY))

        self.assertEqual(svc.state, 'initializing')
        self.assertTrue(svc.init_ready_observed, '就緒觀測沒記下來，init cron 不會 claim')
        self.assertFalse(svc.transition_timeout_at)

    def test_still_stuck_stays_error_untouched(self):
        svc = self._timed_out()
        message, marker = svc.error_message, svc.transition_timeout_at

        client = self._recover(status_response(pods=_STUCK))

        self.assertIn(svc.helm_release_name, self._polled(client))
        self.assertEqual((svc.state, svc.error_message, svc.transition_timeout_at),
                         ('error', message, marker))

    def test_release_gone_stops_rechecking(self):
        svc = self._timed_out()
        message = svc.error_message

        self._recover(side_effect=PaaSOperatorAPIError('not found', status_code=404))

        self.assertEqual((svc.state, svc.error_message), ('error', message))
        self.assertFalse(svc.transition_timeout_at, 'release 已不在，不該繼續每 5 分鐘重查')

    def test_operator_error_keeps_rechecking(self):
        """operator 暫時連不上不是 release 不在：標記保留，下一輪再看。"""
        svc = self._timed_out()

        self._recover(side_effect=PaaSOperatorAPIError('boom', status_code=500))

        self.assertEqual(svc.state, 'error')
        self.assertTrue(svc.transition_timeout_at)

    def test_release_failed_turns_into_an_operator_error(self):
        """helm 之後明確回報 failed：換成 operator 的錯誤訊息，不再自癒。"""
        svc = self._timed_out()

        self._recover(status_response('failed', release={
            'status': 'failed', 'revision': 1, 'description': 'upgrade failed: boom'}))

        self.assertEqual((svc.state, svc.error_message), ('error', 'upgrade failed: boom'))
        self.assertFalse(svc.transition_timeout_at)

    # ------------------------------------------------------------------ #
    #  只碰逾時造成的 error                                                #
    # ------------------------------------------------------------------ #

    def test_other_error_sources_are_never_polled(self):
        operator_failed = self._service(tag='failed')
        self._poll(operator_failed, status_response('failed'))
        self.assertEqual(operator_failed.state, 'error')
        cleanup_failed = self._timed_out(tag='cleanup')
        # 刪除失敗的善後（`_do_cleanup`）在 error 上改寫訊息 ⇒ 來源換了。
        cleanup_failed.write({'state': 'error', 'error_message': 'Cleanup failed: boom'})
        self.assertFalse(cleanup_failed.transition_timeout_at)

        client = self._recover(status_response(pods=_READY))

        polled = self._polled(client)
        self.assertNotIn(operator_failed.helm_release_name, polled)
        self.assertNotIn(cleanup_failed.helm_release_name, polled)
        self.assertEqual((operator_failed.state, cleanup_failed.state), ('error', 'error'))

    def test_halted_service_is_not_recovered(self):
        """停權中的 workload 是被正確地停著，推回 running＝計費繞過（#482／#497）。"""
        svc = self._timed_out()
        svc.write({'pre_suspend_state': 'running'})

        client = self._recover(status_response(pods=_READY))

        self.assertNotIn(svc.helm_release_name, self._polled(client))
        self.assertEqual(svc.state, 'error')

    def test_marker_survives_suspend_and_resume(self):
        """停權（error → suspended → error，Q25）不算離開：復權後仍可自癒。"""
        svc = self._timed_out()
        svc.write({'pre_suspend_state': 'error', 'state': 'suspended'})
        self.assertTrue(svc.transition_timeout_at)
        svc.write({'pre_suspend_state': False, 'state': 'error'})
        self.assertTrue(svc.transition_timeout_at)

        self._recover(status_response(pods=_READY))

        self.assertEqual(svc.state, 'running')

    def test_leaving_error_clears_the_marker(self):
        svc = self._timed_out()
        svc.write({'state': 'deploying'})   # 例：管理員 Redeploy
        self.assertFalse(svc.transition_timeout_at)

    def test_rewritten_error_message_clears_the_marker(self):
        svc = self._timed_out()
        svc.write({'error_message': 'Resume failed: boom'})
        self.assertFalse(svc.transition_timeout_at)

    def test_tenant_cannot_set_the_marker(self):
        """租戶自己寫標記 ⇒ cron 會以 superuser 把別種 error 推回去（同 init 管控欄位）。"""
        svc = self._service(tag='tenant')
        svc.write({'state': 'error', 'error_message': 'Deployment failed: x'})
        user = self.env['res.users'].create({
            'name': 'Tenant 1416', 'login': 'tenant1416@tenant1416.tw',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            svc.with_user(user).write({'transition_timeout_at': fields.Datetime.now()})

    def test_optimistic_lock_blocks_the_recovery_write(self):
        """operator 回應期間服務被別人改走（例：Redeploy）⇒ 不得覆寫。"""
        svc = self._timed_out()

        def _redeploy_meanwhile(*args, **kwargs):
            svc.write({'state': 'deploying'})
            return status_response(pods=_READY)

        self._recover(side_effect=_redeploy_meanwhile)

        self.assertEqual(svc.state, 'deploying')

    def test_source_change_during_the_poll_blocks_the_recovery(self):
        """回應期間 error 被別的來源接手（標記被 write() 清掉）⇒ 不推。"""
        svc = self._timed_out()

        def _cleanup_failed_meanwhile(*args, **kwargs):
            svc.write({'error_message': 'Cleanup failed: boom'})
            return status_response(pods=_READY)

        self._recover(side_effect=_cleanup_failed_meanwhile)

        self.assertEqual((svc.state, svc.error_message), ('error', 'Cleanup failed: boom'))

    # ------------------------------------------------------------------ #
    #  時間窗                                                              #
    # ------------------------------------------------------------------ #

    def test_window_default_and_fail_safe(self):
        Service = self.Service
        self.assertEqual(Service._timeout_recovery_window_hours(), 7 * 24)
        self.assertEqual(TIMEOUT_RECOVERY_WINDOW_HOURS_DEFAULT, 7 * 24)
        for garbage in ('abc', '0', '-5', ' '):
            with self.subTest(value=garbage):
                self.env['ir.config_parameter'].sudo().set_param(_PARAM, garbage)
                self.assertEqual(Service._timeout_recovery_window_hours(), 7 * 24,
                                 '亂填不得停用自癒')
        self.env['ir.config_parameter'].sudo().set_param(_PARAM, '2')
        self.assertEqual(Service._timeout_recovery_window_hours(), 2)

    def test_expired_window_is_not_polled(self):
        self.env['ir.config_parameter'].sudo().set_param(_PARAM, '2')
        fresh = self._timed_out(tag='fresh')
        stale = self._timed_out(tag='stale')
        stale.sudo().write({
            'transition_timeout_at': fields.Datetime.now() - timedelta(hours=3)})

        client = self._recover(status_response(pods=_READY))

        polled = self._polled(client)
        self.assertIn(fresh.helm_release_name, polled)
        self.assertNotIn(stale.helm_release_name, polled)
        self.assertEqual((fresh.state, stale.state), ('running', 'error'))

    def test_running_services_are_not_polled(self):
        svc = self._service(tag='run')
        self._poll(svc, status_response(pods=_READY))
        self.assertEqual(svc.state, 'running')

        client = self._recover(status_response(pods=_READY))

        self.assertNotIn(svc.helm_release_name, self._polled(client))

    # ------------------------------------------------------------------ #
    #  接線                                                                #
    # ------------------------------------------------------------------ #

    def test_cron_record_is_wired(self):
        cron = self.env.ref('woow_paas_platform.ir_cron_recover_timed_out_services')
        self.assertTrue(cron.active)
        self.assertEqual(cron.code.strip(), 'model._cron_recover_timed_out_services()')
        self.assertEqual((cron.interval_number, cron.interval_type), (5, 'minutes'))


class TestRecoveryConfigParameterRegisteredForUninstall(TransactionCase):
    """「模組常數＋get_param()」寫法 repo-lint 掃不到，補這條（CLAUDE.md §config_parameter）。"""

    def test_window_param_is_in_config_parameters_list(self):
        from ..hooks import _CONFIG_PARAMETERS

        self.assertIn(_PARAM, _CONFIG_PARAMETERS)
