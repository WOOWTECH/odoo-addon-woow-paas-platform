# -*- coding: utf-8 -*-
"""#1159：post-deploy init 的執行面（list/get 純讀、背景 poll、init cron、監控、UI 資料）。

驗收對照（#1159 resolution c15307「本票實作範圍與驗收」；#1257 c15462 的分工調整；
實作期裁決 D1–D4 見 PR 描述）：

1. list/get 不呼叫 operator、不寫入 cloud_service（含 DNS 旗標）；回應含 ``observed_at``
   與 ``init_phase`` → ``TestListGetPureRead1159``。
2. deleting 由背景在 operator 回 404 後 unlink；一台失敗不影響同一 tick 其他台
   → ``TestShortPollObservation1159``、``TestExecSurfaceRealCursor1159``。
3. init 在獨立 ir.cron、與短 poll 不同 job／交易；慢 init 進行中另一台的
   upgrading→running 仍在短 poll 落地（真 cursor）→ ``TestExecSurfaceRealCursor1159``。
4. 每 tick 遵守 B＝1100s 與軟上限 10 筆；積壓時自我 ``_trigger()``、回報
   ``remaining=0``；n8n 優先、其次等待最久 → ``TestInitCron1159``。
5. 等待時限（#1257 已做）＋正式 fast-fail 接入位置（D1-R 暫停代用規則）＋容量排隊不計時（D2）
   → ``TestShortPollObservation1159``。

🔴 會走到 init 入口或 cron 迴圈（兩者都會 ``commit``）的測試用真 cursor＋真 commit＋
事後清理（樣板 ``test_billing_incident_864.py``、``test_init_execution_control_1257.py``）。
"""
import hashlib
import json
import re
import sys
import threading
import traceback
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from psycopg2 import Error as PostgreSQLError, OperationalError

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

from ..controllers.paas import PaasController
from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorError
from .operator_contract import operator_client, status_response
from .committed_cron_scope import CommittedCronScope
from .monitor_boundary_probe import (
    assert_notification_rollback, notification_fault, poll_health_time, prepare_system_recipients,
)

SVC = 'woow_paas_platform.cloud_service'
ATTEMPT = 'woow_paas_platform.cloud_service_init_attempt'
_OPERATOR_MODULE = 'odoo.addons.woow_paas_platform.services.paas_operator'
_MODEL_MODULE = 'odoo.addons.woow_paas_platform.models.cloud_service'
INIT_CRON = 'woow_paas_platform.ir_cron_run_post_deploy_inits'
_READY_PODS = [{'name': 'app', 'phase': 'Running', 'ready': '1/1'}]
_NOT_READY_PODS = [{'name': 'app', 'phase': 'Running', 'ready': '0/1'}]


def _template_vals(tag, init_type):
    return {
        'name': f'1159 {init_type} {tag}',
        'slug': f'init-1159-{init_type}-{tag}',
        'helm_repo_url': 'https://charts.example.com',
        'helm_chart_name': 'chart',
        'helm_chart_version': '1.0.0',
        'workload_type': 'container',
        'post_deploy_init_type': init_type,
        'mcp_enabled': False,
    }


def _make_service(env, workspace, template, tag, **vals):
    return _internal_provision(env[SVC]).create(dict({
        'name': f'1159 Service {tag}',
        'workspace_id': workspace.id,
        'template_id': template.id,
        'state': 'deploying',
        'helm_namespace': f'dev-sandbox-1159-{tag}',
        'helm_release_name': f'svc-{tag}',
    }, **vals))


def _thread_stacks(thread):
    frame = sys._current_frames().get(thread.ident)
    return ''.join(traceback.format_stack(frame)) if frame else '(thread already finished)'


class _ServiceCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.tag = uuid.uuid4().hex[:8]
        self.scope = CommittedCronScope(self)
        with self.scope.track():
            self.scope.monitor_id = self.env[MONITOR].create({'name': f'1159 monitor {self.tag}'}).id
            self.workspace = self.env['woow_paas_platform.workspace'].create({
                'name': f'1159 WS {self.tag}'})
            self.templates = {
                init_type: self.env['woow_paas_platform.cloud_app_template'].create(
                    _template_vals(self.tag, init_type))
                for init_type in ('odoo', 'n8n', 'none')
            }
        self.enterContext(self.scope.isolated(real_monitor=True))

    def _service(self, init_type='odoo', **vals):
        service = _make_service(self.env, self.workspace, self.templates[init_type],
                                f'{init_type}-{uuid.uuid4().hex[:6]}', **vals)
        if vals.get('state') == 'initializing':
            service.sudo().write({'init_pending': True})
        return service

    def _poll(self, service, pods=None, **status):
        client = operator_client(get_status=status_response(
            pods=_READY_PODS if pods is None else pods, **status))
        service._poll_and_update_status(self.scope.operator(client))
        return client

    def _init_cron_triggers(self):
        cron = self.env.ref(INIT_CRON)
        return self.env['ir.cron.trigger'].sudo().search_count([('cron_id', '=', cron.id)])


# ====================================================================== #
#  短 poll：只觀測、不執行 init                                             #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestShortPollObservation1159(_ServiceCase):

    def test_ready_initializing_service_is_queued_for_the_init_cron(self):
        """Q1／Q4：短 poll 只記「pods ready」並分派給 init cron，不呼叫 operator 的 init。"""
        for init_type in ('odoo', 'n8n'):
            with self.subTest(init_type=init_type):
                svc = self._service(init_type)
                triggers = self._init_cron_triggers()
                client = self._poll(svc)
                self.assertEqual(svc.state, 'initializing')
                self.assertTrue(svc.init_ready_observed)
                self.assertTrue(svc.init_ready_since)
                self.assertTrue(svc.observed_at)
                client.init_odoo.assert_not_called()
                client.init_n8n.assert_not_called()
                self.assertGreater(self._init_cron_triggers(), triggers,
                                   '觀測到 ready 應 trigger init cron（n8n 搶註窗口，Q28a）')
                # 已在 initializing 的下一輪也一樣不 dispatch。
                client = self._poll(svc)
                client.init_odoo.assert_not_called()
                client.init_n8n.assert_not_called()

    def test_not_ready_observation_leaves_the_init_queue(self):
        svc = self._service('odoo')
        self._poll(svc)
        self.assertTrue(svc.init_ready_observed)
        self._poll(svc, pods=_NOT_READY_PODS)
        self.assertEqual(svc.state, 'initializing')
        self.assertFalse(svc.init_ready_observed, 'not ready 時 init cron 不得 claim')
        self.assertTrue(svc.init_ready_since, '第一次 ready 的時間保留（排序與 n8n 窗口）')

    def test_queue_time_does_not_count_towards_the_wait_timeout(self):
        """D2：排隊等 init cron（已觀測到 ready）不計時；錨點前推到最近一次 ready 觀測。"""
        svc = self._service('odoo', state='initializing')
        svc.sudo().write({'init_wait_since': fields.Datetime.now() - timedelta(minutes=45)})
        self._poll(svc)
        self.assertEqual(svc.state, 'initializing', '在佇列裡等 init cron 不得逾時')
        self.assertGreater(svc.init_wait_since,
                           fields.Datetime.now() - timedelta(minutes=1))
        # 之後 pods 掉了：從最近一次 ready 觀測起算，不是從 45 分前。
        self._poll(svc, pods=_NOT_READY_PODS)
        self.assertEqual(svc.state, 'initializing')

    def test_waiting_for_readiness_still_times_out(self):
        svc = self._service('odoo', state='initializing')
        svc.sudo().write({'init_wait_since': fields.Datetime.now() - timedelta(minutes=31)})
        self._poll(svc, pods=_NOT_READY_PODS)
        self.assertEqual((svc.state, svc.init_exit, svc.init_retries),
                         ('error', 'wait_timeout', 0))

    def test_entering_initializing_resets_the_ready_observation(self):
        svc = self._service('odoo', state='initializing')
        svc.sudo().write({'init_ready_observed': True,
                          'init_ready_since': fields.Datetime.now() - timedelta(hours=1)})
        svc.sudo().write({'state': 'upgrading'})
        svc.sudo().write({'state': 'initializing'})
        self.assertFalse(svc.init_ready_observed)
        self.assertFalse(svc.init_ready_since)

    # -- D1：#1192 fast-fail 短路 ------------------------------------------- #

    def _crashing_pods(self, restarts, ready='0/1', phase='Running'):
        return [{'name': 'app-0', 'phase': phase, 'ready': ready, 'restarts': restarts}]

    def test_historical_pod_restarts_do_not_short_circuit_the_wait(self):
        """D1-R：終生累計3或100次都不是本次失敗證據，但正常等待時限仍有效。"""
        for restarts in (3, 100):
            with self.subTest(restarts=restarts):
                svc = self._service('odoo', state='initializing')
                self._poll(svc, pods=self._crashing_pods(restarts))
                self.assertEqual((svc.state, svc.init_exit, svc.init_retries, svc.init_unknown_count),
                                 ('initializing', False, 0, 0))
                self.assertTrue(svc.init_pending)
                svc.sudo().write({'init_wait_since': fields.Datetime.now() - timedelta(minutes=31)})
                self._poll(svc, pods=self._crashing_pods(restarts))
                self.assertEqual((svc.state, svc.init_exit, svc.init_retries, svc.init_unknown_count),
                                 ('error', 'wait_timeout', 0, 0))

    def test_fewer_restarts_keep_waiting(self):
        svc = self._service('odoo', state='initializing')
        self._poll(svc, pods=self._crashing_pods(2))
        self.assertEqual(svc.state, 'initializing')

    def test_restarts_on_a_ready_pod_do_not_short_circuit(self):
        svc = self._service('odoo', state='initializing')
        self._poll(svc, pods=self._crashing_pods(7, ready='1/1'))
        self.assertEqual(svc.state, 'initializing')
        self.assertTrue(svc.init_ready_observed)

    def test_no_short_circuit_while_an_attempt_holds_the_lease(self):
        svc = self._service('odoo', state='initializing')
        svc.sudo().write({'init_lease_until': fields.Datetime.now() + timedelta(minutes=10)})
        self._poll(svc, pods=self._crashing_pods(5))
        self.assertEqual(svc.state, 'initializing', 'lease 在途由 lease／額度收尾')

    def test_fast_fail_error_is_not_a_reconfigure_exit(self):
        """歷史 fast_fail 記錄仍不是額度出口；D1-R 不會因此放寬 reconfigure。"""
        svc = self._service('odoo', state='error')
        svc.sudo().write({'init_exit': 'fast_fail'})
        from ..controllers.paas import PaasController
        from types import SimpleNamespace
        client = MagicMock()
        with patch('odoo.addons.woow_paas_platform.controllers.paas.get_paas_operator_client',
                   return_value=client), \
                patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                      new=SimpleNamespace(env=self.env)):
            result = PaasController()._update_service(svc, values={}, version=None)
        self.assertFalse(result['success'])
        client.upgrade_release.assert_not_called()

    # -- deleting 收尾（驗收 2 的單筆那半）------------------------------------ #

    def test_deleting_service_is_unlinked_once_the_release_is_gone(self):
        svc = self._service('none', state='deleting')
        client = operator_client()
        client.get_status.side_effect = PaaSOperatorError('gone', status_code=404)
        with patch.object(type(svc), 'unlink', autospec=True) as unlink:
            svc._poll_and_update_status(client)
        unlink.assert_called_once()

    def test_deleting_service_with_a_live_release_is_not_resurrected(self):
        """#1159 以前 controller 的 list poll 會把它寫回 running。"""
        svc = self._service('odoo', state='deleting')
        self._poll(svc)
        self.assertEqual(svc.state, 'deleting')
        self.assertTrue(svc.observed_at)


# ====================================================================== #
#  驗收 1：list/get 純讀                                                    #
# ====================================================================== #

_CONTROLLER_MODULE = 'odoo.addons.woow_paas_platform.controllers.paas'


@tagged('post_install', '-at_install')
class TestListGetPureRead1159(_ServiceCase):
    """Q1：list/get 只回 DB 的最近觀測；不呼叫 operator、不寫 cloud_service（含 DNS 旗標）。"""

    def _call(self, fn):
        from types import SimpleNamespace
        from ..controllers.paas import PaasController
        model = type(self.env[SVC])
        with patch(f'{_CONTROLLER_MODULE}.get_paas_operator_client') as factory, \
                patch(f'{_CONTROLLER_MODULE}.request', new=SimpleNamespace(env=self.env)), \
                patch.object(model, 'write', autospec=True,
                             side_effect=AssertionError('list/get 不得寫入 cloud_service')) as write, \
                patch('odoo.addons.woow_paas_platform.services.dns_probe.resolve_state',
                      side_effect=AssertionError('list/get 不得探 DNS')):
            result = fn(PaasController())
        factory.assert_not_called()
        write.assert_not_called()
        return result

    def _fleet(self):
        observed = fields.Datetime.now() - timedelta(minutes=4)
        services = [self._service('none', state=state) for state in (
            'deploying', 'upgrading', 'deleting')]
        services.append(self._service('odoo', state='initializing'))
        running = self._service('none', state='running', subdomain=f'dns-{self.tag}')
        running.sudo().write({'public_url_dns_unverified': True})
        services.append(running)
        for svc in services:
            # 這是已在同一輪轉移裡收到的真觀測，不是剛開的新等待 episode。
            svc.sudo().write({
                'observed_at': observed,
                'observation_episode_started_at': observed - timedelta(seconds=1),
            })
        return services, observed

    def test_list_does_not_poll_or_write(self):
        services, observed = self._fleet()
        result = self._call(lambda ctrl: ctrl._list_services(self.workspace))
        self.assertTrue(result['success'])
        by_id = {row['id']: row for row in result['data']}
        for svc in services:
            self.assertEqual(by_id[svc.id]['observed_at'], observed.isoformat())
            self.assertIn('init_phase', by_id[svc.id])
            # 前端「最後更新 X 前」用伺服器算好的秒數：observed_at 是 naive UTC，前端的
            # 日期層目前把不帶 offset 的字串當本地時間讀（#914 未切換）。
            self.assertTrue(230 <= by_id[svc.id]['observed_age_seconds'] <= 300,
                            by_id[svc.id]['observed_age_seconds'])
        self.assertTrue(by_id[services[-1].id]['public_url_dns_unverified'],
                        'DNS 旗標只由背景重探清除（Q16）')

    def test_get_does_not_poll_or_write(self):
        services, observed = self._fleet()
        for svc in services:
            with self.subTest(state=svc.state):
                result = self._call(lambda ctrl: ctrl._get_service(svc))
                self.assertTrue(result['success'])
                self.assertEqual(result['data']['observed_at'], observed.isoformat())
                self.assertEqual(result['data']['state'], svc.state)


@tagged('post_install', '-at_install')
class TestObservationFreshness1159(_ServiceCase):
    """新轉移等待新的真觀測；同一個 episode 的時鐘供 payload 與監控共用。"""

    def test_new_transition_preserves_the_last_real_observation_and_starts_pending_episode(self):
        svc = self._service('none', state='running')
        observed = fields.Datetime.now() - timedelta(days=3)
        svc.sudo().write({'observed_at': observed})

        svc.sudo().write({'state': 'upgrading'})

        self.assertEqual(svc.observed_at, observed, '命令時間不得偽裝成 operator 觀測')
        self.assertTrue(svc.observation_episode_started_at)
        self.assertGreater(svc.observation_episode_started_at,
                           fields.Datetime.now() - timedelta(minutes=1))
        payload = PaasController()._format_service(svc)
        self.assertTrue(payload['observation_pending'])
        self.assertTrue(0 <= payload['observed_age_seconds'] < 60,
                        '新等待必須從這次轉移起算，不能立刻沿用三天前的 observed_at')

    def test_same_state_and_unrelated_writes_do_not_reset_pending_episode(self):
        svc = self._service('none', state='upgrading')
        started = fields.Datetime.now() - timedelta(minutes=3)
        svc.sudo().write({'observation_episode_started_at': started})

        svc.sudo().write({'state': 'upgrading'})
        svc.sudo().write({'error_message': 'unrelated bookkeeping'})

        self.assertEqual(svc.observation_episode_started_at, started)
        payload = PaasController()._format_service(svc)
        self.assertTrue(payload['observation_pending'])
        self.assertTrue(170 <= payload['observed_age_seconds'] <= 190,
                        'three minutes without a real poll remains a pending wait')

    def test_mixed_recordset_stamps_only_records_entering_the_transition(self):
        entering = self._service('none', state='running')
        already_deleting = self._service('none', state='deleting')
        old_episode = fields.Datetime.now() - timedelta(minutes=3)
        already_deleting.sudo().write({'observation_episode_started_at': old_episode})

        (entering | already_deleting).sudo().write({'state': 'deleting'})

        self.assertGreater(entering.observation_episode_started_at,
                           fields.Datetime.now() - timedelta(minutes=1))
        self.assertEqual(already_deleting.observation_episode_started_at, old_episode)

    def test_real_poll_converges_pending_payload_without_changing_episode_on_reads(self):
        svc = self._service('none', state='upgrading')
        before = svc.observation_episode_started_at
        self._poll(svc)
        payload = PaasController()._format_service(svc)

        self.assertTrue(svc.observed_at)
        self.assertGreaterEqual(svc.observed_at, before)
        self.assertFalse(payload['observation_pending'])
        self.assertTrue(0 <= payload['observed_age_seconds'] < 60)

    def _workspace_admin(self):
        user = self.env['res.users'].create({
            'name': f'1159 observation admin {self.tag}',
            'login': f'1159-observation-{self.tag}',
            'lang': 'en_US',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': 'admin',
        })
        return user

    def test_workspace_admin_cannot_forge_an_observation_or_reset_the_episode(self):
        svc = self._service('none', state='deploying')
        user = self._workspace_admin()
        managed = svc.with_user(user).with_context(lang='en_US')
        managed.write({'name': '合法的一般欄位更新'})
        self.assertEqual(managed.name, '合法的一般欄位更新', '先證明這個使用者真的可寫該record')
        for key in ('observed_at', 'observation_episode_started_at'):
            with self.subTest(field=key):
                try:
                    managed.write({key: fields.Datetime.now()})
                except AccessError as exc:
                    self.assertIn('observation fields are managed by the platform', str(exc))
                else:
                    self.fail('platform observation guard 未拒絕偽造欄位')

    def test_provisioning_neutralizes_observation_vals_and_default_carriers(self):
        """合法開通仍不可把 raw vals、context 或使用者 ir.default 變成假的觀測證據。"""
        user = self._workspace_admin()
        future = fields.Datetime.now() + timedelta(days=3)
        fake = fields.Datetime.to_string(future)
        creator = _internal_provision(self.env[SVC].with_user(user).sudo())
        # sudo 不改 uid：這正是合法 server-side provisioning 仍會吃到使用者default的風險。
        self.assertEqual(creator.env.uid, user.id)
        for carrier in ('values', 'context', 'ir.default'):
            for state in ('pending', 'deploying'):
                with self.subTest(carrier=carrier, state=state):
                    model = creator
                    vals = {'name': f'觀測載體 {carrier} {state}', 'workspace_id': self.workspace.id,
                            'template_id': self.templates['none'].id, 'state': state}
                    if carrier == 'values':
                        vals.update(observed_at=fake, observation_episode_started_at=fake)
                    elif carrier == 'context':
                        model = model.with_context(default_observed_at=fake,
                            default_observation_episode_started_at=fake)
                    else:
                        defaults = self.env['ir.default'].with_user(user)
                        for key in ('observed_at', 'observation_episode_started_at'):
                            defaults.set(SVC, key, fake, user_id=user.id)
                    if carrier != 'values':
                        values = model.default_get(['observed_at', 'observation_episode_started_at'])
                        self.assertEqual(fields.Datetime.to_datetime(values['observed_at']), future,
                                         '前置：偽造觀測default必須真的抵達框架')
                        self.assertEqual(fields.Datetime.to_datetime(values['observation_episode_started_at']),
                                         future, '前置：episode default也必須真的抵達框架')
                    svc = model.create(vals)
                    self.assertFalse(svc.observed_at, '新服務尚未被operator觀測，不可沿用caller證據')
                    if state == 'pending':
                        self.assertFalse(svc.observation_episode_started_at)
                    else:
                        self.assertLess(svc.observation_episode_started_at, future)
                    svc.sudo().write({'state': 'deploying'})
                    svc.sudo().write({'observed_at': fields.Datetime.now()})
                    self.assertFalse(svc._observation_freshness()[0], '真正的後續平台觀測仍必須可寫入')

    def test_monitor_initializes_legacy_episode_once_then_alerts_after_grace(self):
        svc = self._service('none', state='deploying')
        stale_observation = fields.Datetime.now() - timedelta(days=3)
        svc.sudo().write({
            'observed_at': stale_observation,
            'observation_episode_started_at': False,
        })
        monitor = self.env[MONITOR]
        summary = monitor.SUMMARY_OBSERVED

        monitor._check_oldest_observation()
        self.assertTrue(svc.observation_episode_started_at)
        self.assertFalse(self.env['mail.activity'].sudo().search([
            ('res_model', '=', MONITOR), ('summary', '=', summary)]),
            'legacy row gets exactly one grace window instead of immediate stale alert')
        svc.sudo().write({
            'observation_episode_started_at': fields.Datetime.now() - timedelta(minutes=16),
        })
        monitor._check_oldest_observation()
        self.assertTrue(self.env['mail.activity'].sudo().search([
            ('res_model', '=', MONITOR), ('summary', '=', summary)]))


@tagged('post_install', '-at_install')
class TestInitPhase1159(_ServiceCase):
    """Q40：不新增 state 值；initializing 依 ``init_phase`` 細分。"""

    def test_phases(self):
        svc = self._service('odoo', state='initializing')
        self.assertEqual(svc._init_phase(), 'waiting_ready', '還沒觀測到 ready')
        svc.sudo().write({'init_ready_observed': True})
        self.assertEqual(svc._init_phase(), 'queued')
        svc.sudo().write({'init_retries': 1})
        self.assertEqual(svc._init_phase(), 'retrying')
        attempt = self.env[ATTEMPT].sudo().create({
            'service_id': svc.id, 'generation': svc.init_generation, 'sequence': 2,
            'init_type': 'odoo', 'claimed_at': fields.Datetime.now(),
            'lease_until': fields.Datetime.now() + timedelta(minutes=20)})
        svc.sudo().write({'init_attempt_id': attempt.id,
                          'init_lease_until': fields.Datetime.now() + timedelta(minutes=20)})
        self.assertEqual(svc._init_phase(), 'running')
        attempt.write({'outcome': 'unknown', 'finished_at': fields.Datetime.now()})
        self.assertEqual(svc._init_phase(), 'retrying',
                         'unknown 之後 lease 刻意留到到期，租戶看到的是「重試中」')
        svc.sudo().write({'init_ready_observed': False, 'init_lease_until': False})
        self.assertEqual(svc._init_phase(), 'waiting_ready')

    def test_no_phase_outside_initializing(self):
        for state in ('deploying', 'running', 'error'):
            with self.subTest(state=state):
                self.assertFalse(self._service('odoo', state=state)._init_phase())


# ====================================================================== #
#  真 cursor：init cron 與短 poll（兩者都 commit）                          #
# ====================================================================== #

class _RealCursorCase(TransactionCase):
    """fixture 真的 commit、事後以 SQL 清掉（樣板同 #1257 ``TestInitLeaseRealCursor1257``）。"""

    def setUp(self):
        super().setUp()
        self.tag = uuid.uuid4().hex[:10]
        self.service_ids = []
        self.scope = CommittedCronScope(self)
        self.addCleanup(self._purge)  # 在任何會 commit 的 fixture 建立前登記。
        with self.scope.track(), self.registry.cursor() as cr:
            env = self._env(cr)
            self.scope.monitor_id = env[MONITOR].create({'name': f'1159 test monitor {self.tag}'}).id
            self.workspace_id = env['woow_paas_platform.workspace'].create({
                'name': f'1159 Workspace {self.tag}'}).id
            self.template_ids = {
                init_type: env['woow_paas_platform.cloud_app_template'].create(
                    _template_vals(self.tag, init_type)).id
                for init_type in ('odoo', 'n8n', 'none')
            }
        # 一般 cron cases 不驗 health hooks；容量 cases 用 monitoring() 跑真 monitor。
        self.enterContext(self.scope.isolated())

    def tearDown(self):
        try:
            self.assertFalse(self.scope.foreign_calls, 'catch-all 後仍必須核 operator 無 foreign calls')
        finally:
            super().tearDown()

    def _purge(self):
        self.scope.purge()

    def _env(self, cr):
        """真 cursor 的 superuser env；worker thread 也能用（見 #1257 同名 helper）。"""
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, SUPERUSER_ID, {})

    def _add(self, init_type, state='initializing', ready_minutes_ago=None, **vals):
        """建一台服務並 commit；``ready_minutes_ago`` 給值＝已在 init cron 佇列裡。"""
        with self.registry.cursor() as cr:
            env = self._env(cr)
            service = _make_service(
                env, env['woow_paas_platform.workspace'].browse(self.workspace_id),
                env['woow_paas_platform.cloud_app_template'].browse(
                    self.template_ids[init_type]),
                f'{init_type}-{uuid.uuid4().hex[:6]}-{self.tag}', state=state, **vals)
            if state == 'initializing':
                control = {'init_pending': True}
                if ready_minutes_ago is not None:
                    now = service._init_db_now()
                    queue_turn = now - timedelta(minutes=ready_minutes_ago)
                    control.update(init_ready_observed=True,
                                   init_ready_since=queue_turn,
                                   init_queue_turn=queue_turn)
                if init_type == 'n8n':
                    control.update(n8n_owner_email=f'o-{self.tag}@example.invalid',
                                   n8n_owner_password=f'pw-{self.tag}')
                service.write(control)
            self.service_ids.append(service.id)
            return service.id

    def _read(self, service_id, *fnames):
        with self.registry.cursor() as cr:
            svc = self._env(cr)[SVC].browse(service_id)
            if not svc.exists():
                return None
            return tuple(svc[f] for f in fnames) if len(fnames) > 1 else svc[fnames[0]]

    def _run_cron(self, method, client, context=None):
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=self.scope.operator(client)):
            with self.registry.cursor() as cr:
                env = self._env(cr)
                getattr(env[SVC].with_context(**(context or {})), method)()

    def _triggers(self, xmlid=INIT_CRON):
        with self.registry.cursor() as cr:
            cron = self._env(cr).ref(xmlid)
            cr.execute('SELECT count(*) FROM ir_cron_trigger WHERE cron_id = %s AND id = ANY(%s)',
                       (cron.id, sorted(self.scope.ids['ir.cron.trigger'])))
            return cr.fetchone()[0]


@tagged('post_install', '-at_install')
class TestInitCron1159(_RealCursorCase):
    """Q4／Q33：獨立的 init ir.cron。"""

    def _recording_client(self, clock=None, seconds_per_init=0):
        """記下 init 的呼叫順序；``clock`` 給值時每次 init 讓假時鐘前進。"""
        calls = []
        client = operator_client()

        def record(kind):
            def init(**kwargs):
                calls.append(kwargs['release_name'])
                if clock is not None:
                    clock['now'] += seconds_per_init
                if kind == 'n8n':
                    return {'success': True, 'api_key': 'eyJhbGciOi.eyJzdWIiOi.c2lnbmF0'}
                return {'success': True}
            return init
        client.init_odoo.side_effect = record('odoo')
        client.init_n8n.side_effect = record('n8n')
        return client, calls

    def _release(self, service_id):
        return self._read(service_id, 'helm_release_name')

    def test_n8n_first_then_the_longest_waiting(self):
        odoo_recent = self._add('odoo', ready_minutes_ago=5)
        odoo_oldest = self._add('odoo', ready_minutes_ago=20)
        n8n_newest = self._add('n8n', ready_minutes_ago=1)
        client, calls = self._recording_client()
        self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(calls, [self._release(n8n_newest), self._release(odoo_oldest),
                                 self._release(odoo_recent)])
        for sid in (odoo_recent, odoo_oldest, n8n_newest):
            self.assertEqual(self._read(sid, 'state'), 'running')

    def test_capacity_monitor_forty_minute_wait_remains_dispatchable_and_claim_restarts_episode(self):
        """真 PG 的 claim／not_executed lifecycle：容量 40 分不耗 D2 預算。"""
        sid = self._add('odoo')
        client = operator_client(get_status=status_response(pods=_READY_PODS))
        claimed = []

        def observe_claim(**kwargs):
            # 外部 operator 邊界讀另一條真 cursor：claim 必須已 commit 且時計已清。
            with self.registry.cursor() as observer_cr:
                observed = self._env(observer_cr)[SVC].browse(sid)
                claimed.append((observed._init_lease_active(), observed.init_capacity_wait_since))
            return {'success': False, 'error_kind': 'not_executed'}

        client.init_odoo.side_effect = observe_claim
        with self.scope.monitoring(), self.registry.cursor() as cr:
            env = self._env(cr)
            svc = env[SVC].browse(sid)
            svc._poll_and_update_status(self.scope.operator(client))
            now = svc._init_db_now()
            monitor = env['woow_paas_platform.platform_monitor']
            domain = [('res_model', '=', monitor._name), ('res_id', '=', self.scope.monitor_id),
                      ('summary', '=', monitor.SUMMARY_CAPACITY)]
            self.assertFalse(env['mail.activity'].search(domain), '本組 target 無舊容量 activity')
            with patch.object(type(svc), '_init_db_now', return_value=now - timedelta(minutes=40)):
                monitor._run_poll_cron_checks()
            monitor._run_poll_cron_checks()
            self.assertTrue(env['mail.activity'].search(domain), '純容量等待 40 分必須可觀測')
            self.assertEqual((svc.state, svc.init_retries, svc.init_unknown_count,
                              svc.init_wait_spent_seconds), ('initializing', 0, 0, 0))
            queue_turn = svc.init_queue_turn
            env['mail.activity'].search(domain).action_feedback(feedback='capacity checked')
            self.assertTrue(svc._run_post_deploy_init(self.scope.operator(client)),
                            '40 分容量等待後仍可原子 claim')
            client.init_odoo.assert_called_once()
            self.assertEqual(len(claimed), 1)
            self.assertTrue(claimed[0][0], '外部呼叫發生時另一 cursor 真能看到 committed lease')
            self.assertFalse(claimed[0][1], '成功 claim 的同一交易必須立即結束舊 episode')
            svc.invalidate_recordset()
            self.assertFalse(svc.init_capacity_wait_since, '結果完成後也不可復活舊 episode')
            self.assertEqual((svc.state, svc.init_retries, svc.init_unknown_count),
                             ('initializing', 0, 0))
            self.assertLess(svc.init_wait_spent_seconds, 5, '只計本次 request，不扣前面 40 分容量')
            self.assertGreaterEqual(svc.init_queue_turn, queue_turn)
            finished = svc.init_wait_started_at
            eligible = svc.init_retry_eligible_at
            self.assertEqual(eligible - finished, timedelta(seconds=60), 'D2 完整 cooldown 不變')
            with patch.object(type(svc), '_init_db_now', return_value=finished + timedelta(seconds=30)) as clock:
                monitor._run_poll_cron_checks()
                self.assertFalse(svc.init_capacity_wait_since, 'cooldown 尚未 eligible，不是容量等待')
                clock.return_value = eligible
                monitor._run_poll_cron_checks()
                self.assertEqual(svc.init_capacity_wait_since, eligible)
                clock.return_value = eligible + timedelta(minutes=30) - timedelta(seconds=1)
                monitor._run_poll_cron_checks()
                self.assertFalse(env['mail.activity'].search(domain))
                clock.return_value = eligible + timedelta(minutes=30)
                monitor._run_poll_cron_checks()
                self.assertTrue(env['mail.activity'].search(domain), '新 episode 完整 30 分才重告警')

    def test_capacity_monitor_excludes_active_and_expired_unsettled_leases(self):
        sid = self._add('odoo')
        client = operator_client(get_status=status_response(pods=_READY_PODS))
        with self.scope.monitoring(), self.registry.cursor() as cr:
            env = self._env(cr)
            svc = env[SVC].browse(sid)
            svc._poll_and_update_status(self.scope.operator(client))
            monitor = env['woow_paas_platform.platform_monitor']
            now = svc._init_db_now()
            monitor._run_poll_cron_checks()
            attempt = svc._init_claim('odoo')
            self.assertTrue(attempt, '建立真 attempt／lease，而非 mock 監控判準')
            domain = [('res_model', '=', monitor._name), ('res_id', '=', self.scope.monitor_id),
                      ('summary', '=', monitor.SUMMARY_CAPACITY)]
            with patch.object(type(svc), '_init_db_now', return_value=now + timedelta(minutes=40)) as clock:
                monitor._run_poll_cron_checks()
                self.assertFalse(svc.init_capacity_wait_since)
                self.assertFalse(env['mail.activity'].search(domain), 'active lease 不算容量等待')
                svc.write({'init_lease_until': now - timedelta(seconds=1)})
                self.assertFalse(svc._init_lease_active())
                self.assertTrue(svc.init_attempt_id, '過期但尚未 settle 的真 lease pointer 仍在')
                monitor._run_poll_cron_checks()
                self.assertFalse(svc.init_capacity_wait_since, '未 settle 的過期 lease 不能當空槽')
                self.assertEqual(svc.init_unknown_count, 0, 'monitor 不自行收 lease／扣 quota')
            svc._init_settle_expired_lease()
            self.assertFalse(svc.init_attempt_id)
            self.assertFalse(svc.init_lease_until)
            self.assertEqual(svc.init_unknown_count, 1, '只有既有 settle 入口扣一次 unknown')
            with patch.object(type(svc), '_init_db_now', return_value=now + timedelta(minutes=40)) as clock:
                monitor._run_poll_cron_checks()
                self.assertEqual(svc.init_capacity_wait_since, now + timedelta(minutes=40))
                self.assertFalse(env['mail.activity'].search(domain))
                clock.return_value = now + timedelta(minutes=70)
                monitor._run_poll_cron_checks()
                self.assertTrue(env['mail.activity'].search(domain))
                self.assertEqual(svc.init_unknown_count, 1, '新容量 episode 不再扣 quota')

    def test_capacity_monitor_does_not_continue_after_real_success(self):
        sid = self._add('odoo')
        client = operator_client(get_status=status_response(pods=_READY_PODS),
                                 init_odoo={'success': True})
        with self.scope.monitoring(), self.registry.cursor() as cr:
            env = self._env(cr)
            svc = env[SVC].browse(sid)
            svc._poll_and_update_status(self.scope.operator(client))
            monitor = env['woow_paas_platform.platform_monitor']
            now = svc._init_db_now()
            monitor._run_poll_cron_checks()
            self.assertTrue(svc.init_capacity_wait_since)
            self.assertTrue(svc._run_post_deploy_init(self.scope.operator(client)))
            svc.invalidate_recordset()
            self.assertEqual(svc.state, 'running')
            self.assertFalse(svc.init_pending)
            with patch.object(type(svc), '_init_db_now', return_value=now + timedelta(hours=1)):
                monitor._run_poll_cron_checks()
            self.assertFalse(svc.init_capacity_wait_since)
            self.assertFalse(env['mail.activity'].search([
                ('res_model', '=', monitor._name), ('res_id', '=', self.scope.monitor_id),
                ('summary', '=', monitor.SUMMARY_CAPACITY)]))

    def test_missing_queue_turn_falls_back_to_first_ready_anchor(self):
        """D2-R 相容列沒有 queue_turn 時仍按既有 first-ready 排，不可在新流量下飢餓。"""
        legacy = self._add('odoo')
        fresh = self._add('odoo', ready_minutes_ago=5)
        with self.registry.cursor() as cr:
            svc = self._env(cr)[SVC].browse(legacy)
            now = svc._init_db_now()
            svc.write({
                'init_ready_observed': True,
                'init_ready_since': now - timedelta(minutes=20),
                'init_queue_turn': False,
            })
        client, calls = self._recording_client()
        self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(calls, [self._release(legacy), self._release(fresh)])

    def test_budget_cutoff_then_same_type_retry_rotates_to_the_second_service(self):
        """E4：A 第一次 not_executed 後移尾；下一 tick 即使 A 已 eligible，B 仍先取得機會。"""
        first = self._add('odoo', ready_minutes_ago=20)
        second = self._add('odoo', ready_minutes_ago=10)
        calls = []
        client = operator_client()
        clock = {'now': 0}
        with patch(f'{_MODEL_MODULE}._init_cron_clock', side_effect=lambda: clock['now']):
            def first_call(**kwargs):
                calls.append(kwargs['release_name'])
                clock['now'] = 81
                return {'success': False, 'error_kind': 'not_executed'}
            client.init_odoo.side_effect = first_call
            self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(calls, [self._release(first)])
        self.assertEqual(self._read(second, 'state'), 'initializing')
        with self.registry.cursor() as cr:
            svc = self._env(cr)[SVC].browse(first)
            now = svc._init_db_now()
            svc.write({
                'init_wait_started_at': now - timedelta(seconds=60),
                'init_retry_eligible_at': now,
            })
        calls.clear()
        client.init_odoo.side_effect = lambda **kwargs: calls.append(kwargs['release_name']) or {'success': True}
        with patch(f'{_MODEL_MODULE}._init_cron_clock', return_value=0):
            self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(calls[0], self._release(second))

    def test_ready_revoked_after_cron_queue_snapshot_is_not_dispatched(self):
        """E5：cron 已取 queue 後另一真 cursor 撤 Ready，舊 queue recordset 也不得送出。"""
        service_id = self._add('odoo', ready_minutes_ago=5)
        queued, revoke_done, release = threading.Event(), threading.Event(), threading.Event()
        calls, errors = [], []
        client = operator_client()
        client.init_odoo.side_effect = lambda **kwargs: calls.append(kwargs) or {'success': True}
        model = type(self.env[SVC])
        original = model._init_cron_queue

        def queue_then_pause(records):
            result = original(records)
            queued.set()
            release.wait(20)
            return result

        def run_cron():
            try:
                self._run_cron('_cron_run_post_deploy_inits', client)
            except Exception as exc:
                errors.append(exc)

        monitor = type(self.env['woow_paas_platform.platform_monitor'])
        with patch.object(model, '_init_cron_queue', autospec=True, side_effect=queue_then_pause), \
                patch.object(monitor, '_run_init_cron_checks', autospec=True):
            thread = threading.Thread(target=run_cron)
            thread.start()
            try:
                self.assertTrue(queued.wait(20), _thread_stacks(thread))
                with self.registry.cursor() as cr:
                    cr.execute("UPDATE woow_paas_platform_cloud_service SET init_ready_observed = FALSE "
                               "WHERE id = %s", (service_id,))
                revoke_done.set()
            finally:
                release.set()
                thread.join(30)
        self.assertTrue(revoke_done.is_set())
        self.assertFalse(thread.is_alive(), _thread_stacks(thread))
        self.assertFalse(errors, errors)
        self.assertEqual(calls, [])
        self.assertEqual(self._read(service_id, 'state'), 'initializing')

    def test_services_not_observed_ready_are_not_claimed(self):
        waiting = self._add('odoo')  # 還沒被短 poll 觀測到 ready
        client, calls = self._recording_client()
        self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(calls, [])
        self.assertEqual(self._read(waiting, 'state'), 'initializing')

    def test_wall_budget_stops_claiming_and_self_triggers(self):
        """B＝1100s：已用時間＋該類最壞 attempt（Odoo 1020）超過 B 就不再 claim。"""
        first = self._add('odoo', ready_minutes_ago=10)
        second = self._add('odoo', ready_minutes_ago=5)
        clock = {'now': 1000.0}
        client, calls = self._recording_client(clock=clock, seconds_per_init=100)
        triggers = self._triggers()
        with patch(f'{_MODEL_MODULE}._init_cron_clock', side_effect=lambda: clock['now']):
            self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(calls, [self._release(first)],
                         '已用 100s＋Odoo 最壞 1020s > 1100s，第二台必須留到下一趟')
        self.assertEqual(self._read(second, 'state'), 'initializing')
        self.assertGreater(self._triggers(), triggers, '有積壓要自我 _trigger()')

    def test_soft_cap_of_ten_per_tick(self):
        ids = [self._add('n8n', ready_minutes_ago=30 - i) for i in range(12)]
        client, calls = self._recording_client()
        triggers = self._triggers()
        self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(calls, [self._release(sid) for sid in ids[:10]])
        self.assertGreater(self._triggers(), triggers)

    def test_progress_is_reported_as_fully_done(self):
        """不回報 remaining>0：core 會在同一趟、同一個 watchdog 內續跑最多 10 批。"""
        for _i in range(12):
            self._add('n8n', ready_minutes_ago=3)
        with self.registry.cursor() as cr:
            _cron, progress = self._env(cr).ref(INIT_CRON)._add_progress()
            progress_id = progress.id
        client, _calls = self._recording_client()
        self._run_cron('_cron_run_post_deploy_inits', client,
                       context={'ir_cron_progress_id': progress_id})
        with self.registry.cursor() as cr:
            cr.execute('SELECT done, remaining FROM ir_cron_progress WHERE id = %s',
                       (progress_id,))
            done, remaining = cr.fetchone()
            cr.execute('DELETE FROM ir_cron_progress WHERE id = %s', (progress_id,))
        self.assertEqual((done, remaining), (10, 0))

    def test_operator_wait_expiry_converges_without_a_client_or_ready_pod(self):
        """D2-R：計時收尾在 queue/client 分支之前，不能被純容量排隊遮蔽。"""
        service_id = self._add('odoo')  # 尚無 Ready 觀測，init cron 不能 claim
        with self.registry.cursor() as cr:
            svc = self._env(cr)[SVC].browse(service_id)
            now = svc._init_db_now()
            svc.write({
                'init_wait_spent_seconds': 1800,
                'init_wait_started_at': now - timedelta(seconds=60),
                'init_retry_eligible_at': now + timedelta(seconds=60),
            })
        # operator client 缺席時 cron 仍必先收時計；這裡故意不建立 recording client。
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=None):
            with self.registry.cursor() as cr:
                self._env(cr)[SVC]._cron_run_post_deploy_inits()
        self.assertEqual(self._read(service_id, 'state'), 'error')

    def test_spent_operator_wait_expires_without_active_interval_for_ready_and_client_variants(self):
        """E8：四個組合各自建立後執行，不能讓第一輪 sweep 偷先處理第二輪的服務。"""
        key = 'woow_paas_platform.init_wait_timeout_minutes'
        with self.registry.cursor() as cr:
            cr.execute('SELECT value FROM ir_config_parameter WHERE key = %s', (key,))
            old = cr.fetchone()

        def restore_param():
            with self.registry.cursor() as cleanup_cr:
                cleanup_env = self._env(cleanup_cr)
                parameter = cleanup_env['ir.config_parameter'].sudo()
                if old is None:
                    parameter.search([('key', '=', key)]).unlink()
                else:
                    parameter.set_param(key, old[0])
                cleanup_cr.commit()
                cleanup_cr.execute('SELECT value FROM ir_config_parameter WHERE key = %s', (key,))
                self.assertEqual(cleanup_cr.fetchone(), old, '跨 cursor 設定必須原樣恢復')

        # 在更動前登記；也以 raw DB 值捕捉「不存在」，不依賴 get_param 的 cache／default。
        self.addCleanup(restore_param)
        with self.registry.cursor() as cr:
            env = self._env(cr)
            env['ir.config_parameter'].sudo().set_param(key, '1')
            cr.commit()
        for has_client in (False, True):
            with self.subTest(has_client=has_client):
                service_ids = [self._add('odoo', ready_minutes_ago=2), self._add('odoo')]
                with self.registry.cursor() as cr:
                    env = self._env(cr)
                    env[SVC].browse(service_ids).write({
                        'init_wait_spent_seconds': 120,
                        'init_wait_started_at': False,
                        'init_retry_eligible_at': False,
                        'init_lease_until': False,
                    })
                self.assertTrue(all(self._read(sid, 'state') == 'initializing' for sid in service_ids))
                client, calls = self._recording_client()
                if has_client:
                    self._run_cron('_cron_run_post_deploy_inits', client)
                else:
                    with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=None):
                        with self.registry.cursor() as cr:
                            env = self._env(cr)
                            env[SVC]._cron_run_post_deploy_inits()
                            cr.commit()
                self.assertEqual(calls, [])
                for service_id in service_ids:
                    state, retries, unknown = self._read(
                        service_id, 'state', 'init_retries', 'init_unknown_count')
                    self.assertEqual((state, retries, unknown), ('error', 0, 0))

    def test_no_backlog_no_trigger(self):
        self._add('odoo', ready_minutes_ago=1)
        client, _calls = self._recording_client()
        triggers = self._triggers()
        self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(self._triggers(), triggers)

    def test_init_cron_success_confirms_the_owner_only_within_the_window(self):
        """Q28b：真 init cron 寫出的 success＋key 只有在首次 ready 後 5 分內收到才算確認。

        掃描時間推到兩台都已逾期（ready＋6 分以上），fast 才真的進候選；
        它的確認證據完全來自真 init 路徑寫出的 attempt，不是測試手寫。
        """
        slow = self._add('n8n', ready_minutes_ago=6)
        fast = self._add('n8n', ready_minutes_ago=1)
        client, _calls = self._recording_client()
        self._run_cron('_cron_run_post_deploy_inits', client)
        with self.registry.cursor() as cr:
            env = self._env(cr)
            fast_attempt = env[ATTEMPT].search([('service_id', '=', fast)], limit=1)
            self.assertEqual((fast_attempt.outcome, fast_attempt.n8n_key_persisted), ('success', True),
                             '前提：fast 的確認證據來自真 init 路徑')
            scan_at = env[SVC]._init_db_now() + timedelta(minutes=6)
        with self.scope.monitoring(), self.registry.cursor() as cr, \
                patch.object(type(self.env[SVC]), '_init_db_now', return_value=scan_at):
            env = self._env(cr)
            monitor = env['woow_paas_platform.platform_monitor']
            candidates = env[SVC].search([('id', 'in', [slow, fast]),
                                          ('init_ready_since', '<', scan_at - timedelta(minutes=5))])
            self.assertEqual(set(candidates.ids), {slow, fast}, '兩台都必須真的進入逾期候選')
            monitor._run_poll_cron_checks()
            notes = ' '.join(env['mail.activity'].search([
                ('res_model', '=', 'woow_paas_platform.platform_monitor'),
                ('res_id', '=', self.scope.monitor_id),
                ('summary', '=', monitor.SUMMARY_N8N_OWNER)]).mapped(
                    lambda a: str(a.note or '')))
            slow_name = env[SVC].browse(slow).name
            fast_name = env[SVC].browse(fast).name
        self.assertEqual(self._read(slow, 'state'), 'running')
        self.assertIn(slow_name, notes, '第 6 分才收到 success：5 分內未確認')
        self.assertNotIn(fast_name, notes, '5 分內收到真 success＋key：已確認')
        self.assertTrue(self._read(slow, 'n8n_owner_alerted_at'))
        self.assertFalse(self._read(fast, 'n8n_owner_alerted_at'))

    def test_requested_redeploys_run_in_the_init_cron(self):
        """Q27／#1257 f：redeploy 意圖由 init cron（掌管 lease 的那一個）執行。"""
        client, _calls = self._recording_client()
        with patch.object(type(self.env[SVC]), '_cron_run_requested_redeploys',
                          autospec=True) as run:
            self._run_cron('_cron_run_post_deploy_inits', client)
            run.assert_called_once()
            run.reset_mock()
            self._run_cron('_cron_check_deploying_services', client)
            run.assert_not_called()


@tagged('post_install', '-at_install')
class TestExecSurfaceRealCursor1159(_RealCursorCase):
    """驗收 2／3：短 poll 的 per-service commit，與 init cron 分屬不同 job／交易。"""

    def test_committing_cron_and_cleanup_preserve_unrelated_owned_sentinels(self):
        foreign = CommittedCronScope(self)
        self.addCleanup(foreign.purge)  # 所有 sentinel 建立之前登記，失敗也精確清理。
        with self.scope.paused():
            before = foreign.make_sentinels(self.tag)
        own = self._add('none', state='upgrading')
        own_release = self._read(own, 'helm_release_name')
        foreign_calls = []
        client = operator_client()

        def own_status(namespace, release_name, include_vm=False):
            if release_name != own_release:
                foreign_calls.append((namespace, release_name))
                raise AssertionError('test operator received a foreign release')
            return status_response(pods=_READY_PODS)

        client.get_status.side_effect = own_status
        self._run_cron('_cron_check_deploying_services', client)
        own_state = self._read(own, 'state')
        self._purge()
        after = foreign.snapshot()
        own_exists = self._read(own, 'state')
        print('1159_SCOPE_SENTINELS=' + json.dumps({
            'before': before, 'after': after, 'foreign_calls': foreign_calls,
            'own_state_after_cron': own_state, 'own_service_after_purge': own_exists}, sort_keys=True), flush=True)
        self.assertFalse(foreign_calls, '產品 catch-all 可吞 AssertionError，必須另核 foreign calls')
        self.assertEqual(own_state, 'running', '自己的真 poll 必須發生且 commit')
        self.assertEqual(after, before, '非 operation/cleanup scope 的身份與內容全部保持不變')
        self.assertIsNone(own_exists, '本組 service 確實清理，不能以 purge no-op 假綠')

    def test_real_monitor_and_all_cron_sweeps_keep_foreign_sentinels_unchanged(self):
        foreign = CommittedCronScope(self)
        self.addCleanup(foreign.purge)
        with self.scope.paused():
            before = foreign.make_sentinels(self.tag)
        queued = self._add('odoo')
        spent = self._add('odoo')
        redeploy = self._add('none', state='running')
        dns = self._add('none', state='running', subdomain=f'own-dns-{self.tag}')
        with self.registry.cursor() as cr:
            env = self._env(cr)
            env[SVC].browse(spent).write({'init_wait_spent_seconds': 1800})
            env[SVC].browse(redeploy).write({'init_redeploy_requested': True})
            env[SVC].browse(dns).write({'public_url_dns_unverified': True})
        client = operator_client(get_status=status_response(pods=_READY_PODS), init_odoo={'success': True})
        client.upgrade_release.return_value = {'revision': 2}
        with self.scope.monitoring():
            self._run_cron('_cron_check_deploying_services', client)
            with self.registry.cursor() as cr:
                env = self._env(cr)
                monitor = env[MONITOR]
                now = env[SVC]._init_db_now()
                # 真 check/clock/savepoint/mail，只改 system-boundary 時間；外組不在候選範圍。
                env[SVC].browse(queued).write({'init_capacity_wait_since': False})
                with patch.object(type(env[SVC]), '_init_db_now', return_value=now - timedelta(minutes=40)):
                    monitor._run_poll_cron_checks()
                monitor._run_poll_cron_checks()
                activities = env['mail.activity'].search([
                    ('res_model', '=', MONITOR), ('res_id', '=', self.scope.monitor_id),
                    ('summary', '=', monitor.SUMMARY_CAPACITY)])
                self.assertIn(env[SVC].browse(queued).name, activities[:1].note or '')
                self.assertNotIn('Sentinel', activities[:1].note or '', '不能把 foreign service 投影進 own 卡片')
                self.assertFalse(monitor._singleton().notified_unknown_attempt_ids,
                                 'foreign unknown attempt 不能進 own ledger')
            self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(self._read(queued, 'state'), 'running', '真 queue/claim/outcome 不能被 adapter fake')
        self.assertEqual(self._read(spent, 'state'), 'error', 'own timeout sweep 確實執行')
        self.assertFalse(self._read(redeploy, 'init_redeploy_requested'), 'own redeploy sweep 確實執行')
        dns_calls, foreign_dns_calls = [], []

        def resolve(hostname, *args, **kwargs):
            if not hostname.startswith(f'own-dns-{self.tag}'):
                foreign_dns_calls.append(hostname)
                raise AssertionError('test resolver received a foreign hostname')
            dns_calls.append(hostname)
            return 'ok'

        with patch('odoo.addons.woow_paas_platform.services.dns_probe.resolve_state', side_effect=resolve):
            self._run_cron('_cron_reprobe_public_url_dns', client)
        self.assertTrue(dns_calls, 'own DNS probe 必須真的執行')
        self.assertFalse(foreign_dns_calls, 'helper catch-all 後仍另外核 foreign DNS calls')
        self.assertFalse(self._read(dns, 'public_url_dns_unverified'))
        self._purge()
        after = foreign.snapshot()
        own_after = self.scope.snapshot()
        print('1159_ALL_SWEEPS_SENTINELS=' + json.dumps({'before': before, 'after': after,
            'own_after': own_after, 'foreign_calls': self.scope.foreign_calls}, sort_keys=True), flush=True)
        self.assertEqual(after, before, '原 singleton/外組 notes、ledger、clock、trigger 全部不變')
        self.assertFalse(any(own_after.values()), 'own records 與 mail/trigger IDs 確實清理')
        self.assertFalse(self.scope.foreign_calls)

    def test_partial_fixture_failure_cleanup_preserves_other_group_records(self):
        foreign = CommittedCronScope(self)
        partial = CommittedCronScope(self)
        self.addCleanup(foreign.purge)
        self.addCleanup(partial.purge)  # 必須在 partial allocation／commit／失敗之前登記。
        with self.scope.paused():
            before = foreign.make_sentinels(self.tag)
            caught = None
            try:
                with partial.track(), self.registry.cursor() as cr:
                    env = partial.env(cr)
                    env['woow_paas_platform.workspace'].create({'name': f'partial WS {self.tag}'})
                    env[MONITOR].create({'name': f'partial monitor {self.tag}'})
                    cr.commit()
                    raise RuntimeError('test fixture failed after a partial commit')
            except RuntimeError as error:
                caught = str(error)
        self.assertEqual(caught, 'test fixture failed after a partial commit')
        allocated = partial.snapshot()
        self.assertTrue(allocated['woow_paas_platform.workspace'], '失敗前確實有 committed allocation')
        self.assertTrue(allocated[MONITOR])
        partial.purge()
        own_after = partial.snapshot()
        after = foreign.snapshot()
        print('1159_PARTIAL_FIXTURE_CLEANUP=' + json.dumps({'before': before, 'after': after,
            'allocated': allocated, 'own_after': own_after}, sort_keys=True), flush=True)
        self.assertFalse(any(own_after.values()), 'partial failure 的 own records 必須精確清掉')
        self.assertEqual(after, before, '失敗也不可清理外組 monitor/mail/trigger')

    def _q36_poll_probe(self, fault_code):
        # 建立順序故意與 search 順序不同；create_date 全部不同，沒有未定義的 ties。
        last = self._add('none', state='upgrading')
        first = self._add('none', state='upgrading')
        broken = self._add('none', state='upgrading', error_message='q36 original error')
        expected = [first, broken, last]
        anchor = fields.Datetime.now() - timedelta(hours=1)
        stamps = {first: anchor + timedelta(seconds=30), broken: anchor + timedelta(seconds=20),
                  last: anchor + timedelta(seconds=10)}
        with self.registry.cursor() as cr:
            env = self._env(cr)
            for sid, stamp in stamps.items():
                cr.execute('UPDATE woow_paas_platform_cloud_service SET create_date = %s WHERE id = %s',
                           (stamp, sid))
            actual = env[SVC].search([
                ('state', 'in', ['deploying', 'upgrading', 'initializing', 'deleting'])]).ids
            cr.execute('SELECT id, create_date FROM woow_paas_platform_cloud_service WHERE id = ANY(%s)',
                       (expected,))
            self.assertEqual(dict(cr.fetchall()), stamps, '真 create_date 與 IDs 必須證明 fixture 順序')
            self.assertEqual(actual, expected, '真 search 順序必須為先成功、broken、後成功')
        releases = {self._read(sid, 'helm_release_name'): sid for sid in expected}
        visits, injected, pre_error, first_committed = [], [], [], []
        client = operator_client()

        def status(namespace, release_name, include_vm=False):
            visits.append(releases[release_name])
            return status_response(pods=_READY_PODS)

        client.get_status.side_effect = status
        model = type(self.env[SVC])
        original_progress = model._store_import_progress

        def progress(service, response):
            result = original_progress(service, response)
            if service.id == broken and fault_code:
                # 正常 get_status 已返回；這裡在 deleting/savepoint 之外，先有真本台寫入。
                service.write({'error_message': 'q36 dirty before SQL error'})
                service.flush_recordset(['error_message', 'observed_at'])
                service.env.cr.execute('SELECT error_message, observed_at FROM '
                                       'woow_paas_platform_cloud_service WHERE id = %s', (broken,))
                pre_error.append(service.env.cr.fetchone())
                with self.registry.cursor() as observer:
                    observer.execute('SELECT state, observed_at FROM woow_paas_platform_cloud_service '
                                     'WHERE id = %s', (first,))
                    first_committed.append(observer.fetchone())
                try:
                    if fault_code == '22012':
                        service.env.cr.execute('SELECT 1 / 0')
                    else:
                        service.env.cr.execute("""
                            DO $$ BEGIN
                                RAISE EXCEPTION 'q36 true PostgreSQL conflict' USING ERRCODE = '40001';
                            END $$
                        """)
                except PostgreSQLError as error:
                    injected.append(error.pgcode)
                    raise
            return result

        job_error, usable = None, False
        with patch.object(model, '_store_import_progress', progress), \
                patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=self.scope.operator(client)):
            with self.registry.cursor() as cr:
                env = self._env(cr)
                try:
                    env[SVC]._cron_check_deploying_services()
                    cr.execute('SELECT 1')
                    usable = cr.fetchone() == (1,)
                except PostgreSQLError as error:
                    # 突出的 DB error 是 observable 結果；清理 cursor 後由外面的 assertion 判 RED。
                    # 不把 InFailedSqlTransaction runner ERROR／assertRaises 自動回捲當保護。
                    job_error = (type(error).__name__, error.pgcode)
                    cr.rollback()
        with self.registry.cursor() as observer:
            observer.execute('SELECT id, state, observed_at, error_message FROM '
                             'woow_paas_platform_cloud_service WHERE id = ANY(%s)', (expected,))
            rows = {row[0]: row[1:] for row in observer.fetchall()}
        print('1159_Q36_SQL=' + json.dumps({'fault': fault_code, 'expected': expected, 'visits': visits,
            'stamps': stamps, 'injected': injected, 'pre_error': pre_error,
            'first_committed_at_failure': first_committed, 'job_error': job_error,
            'cursor_usable': usable, 'rows': rows}, default=str, sort_keys=True), flush=True)
        self.assertIsNone(job_error, 'Q36 job/cursor 未恢復；不能以 runner SQL ERROR 充數')
        self.assertTrue(usable, 'job boundary 之後 cursor 必須可查')
        self.assertEqual(rows[last][0], 'running', '中間故障後的後排仍須成功 commit，不能 except break')
        self.assertEqual(visits, expected, '真 loop 必須照已釘 search 順序訪問全部 own records')
        self.assertEqual(rows[first][0], 'running')
        self.assertTrue(rows[first][1], '先者真 observation 已 commit')
        self.assertTrue(rows[last][1], '後者真 observation 已 commit')
        if fault_code:
            self.assertEqual(injected, [fault_code], '真 PG fault 必須到達，不是 Python exception')
            self.assertEqual(pre_error[0][0], 'q36 dirty before SQL error')
            self.assertTrue(pre_error[0][1], 'SQL 失敗前本台 observation 真已 flush')
            self.assertEqual(first_committed[0][0], 'running', '故障發生時另一 cursor 已看到先者 commit')
            self.assertTrue(first_committed[0][1])
            self.assertEqual(rows[broken], ('upgrading', None, 'q36 original error'),
                             '只回捲 broken 的未 commit state/observation/error 寫入')
        else:
            self.assertFalse(injected)
            self.assertEqual(rows[broken][0], 'running', '無故障陽性對照必須處理中間台')
            self.assertTrue(rows[broken][1])
        self.assertFalse(self.scope.foreign_calls)

    def test_q36_real_sql_abort_rolls_back_only_the_current_service(self):
        self._q36_poll_probe('22012')

    def test_q36_real_serialization_failure_keeps_committed_neighbors(self):
        self._q36_poll_probe('40001')

    def test_q36_without_fault_commits_all_three_services(self):
        self._q36_poll_probe(None)

    def _boundary_recipients(self):
        with self.registry.cursor() as cr:
            env = self._env(cr)
            prepare_system_recipients(self, env)

    def _give_up_notification_boundary(self, fault):
        self._boundary_recipients()
        foreign = CommittedCronScope(self)
        self.addCleanup(foreign.purge)
        with self.scope.paused():
            sentinel_before = foreign.make_sentinels(self.tag)
        last = self._add('none', state='upgrading')
        first = self._add('none', state='upgrading')
        broken = self._add('odoo')
        order = [first, broken, last]
        with self.registry.cursor() as cr:
            env = self._env(cr)
            anchor = fields.Datetime.now() - timedelta(hours=1)
            for index, sid in enumerate(order):
                cr.execute('UPDATE woow_paas_platform_cloud_service SET create_date = %s WHERE id = %s',
                           (anchor - timedelta(seconds=index), sid))
            env[SVC].browse(broken).write({'init_wait_since': fields.Datetime.now() - timedelta(minutes=31)})
            self.assertEqual(env[SVC].search([
                ('state', 'in', ['deploying', 'upgrading', 'initializing', 'deleting'])]).ids, order)
        releases = {self._release_name(sid): sid for sid in order}
        visited, pre_error = [], []
        client = operator_client()

        def status(namespace, release_name, include_vm=False):
            sid = releases[release_name]
            visited.append(sid)
            return status_response(pods=_NOT_READY_PODS if sid == broken else _READY_PODS)

        def observed_before_fault(records):
            records.env.cr.execute('SELECT state, observed_at FROM woow_paas_platform_cloud_service '
                                   'WHERE id = %s', (broken,))
            current = records.env.cr.fetchone()
            with self.registry.cursor() as observer:
                observer.execute('SELECT state, observed_at FROM woow_paas_platform_cloud_service '
                                 'WHERE id = %s', (first,))
                committed = observer.fetchone()
            pre_error.append({'current': current, 'first_committed': committed})

        client.get_status.side_effect = status
        summary = self.env[MONITOR].SUMMARY_GAVE_UP
        with notification_fault(self, summary, fault=fault, before_fault=observed_before_fault) as evidence:
            self._run_cron('_cron_check_deploying_services', client)
            with self.registry.cursor() as cr:
                env = self._env(cr)
                assert_notification_rollback(self, env, evidence)
                rows = env[SVC].browse(order).read(['state', 'observed_at', 'init_exit',
                                                  'init_retries', 'init_unknown_count'])
            self.assertEqual(visited, order, 'helper DB 故障必須交 Q36 當台 rollback 後繼續後排')
            self.assertEqual(pre_error[0]['current'][0], 'error', '給 up 的本台未 commit 寫入真已發生')
            self.assertTrue(pre_error[0]['current'][1])
            self.assertEqual(pre_error[0]['first_committed'][0], 'running')
            self.assertTrue(pre_error[0]['first_committed'][1], '故障當下另一 cursor 真看到先者 commit')
            by_id = {row['id']: row for row in rows}
            self.assertEqual((by_id[first]['state'], by_id[last]['state']), ('running', 'running'))
            self.assertTrue(by_id[first]['observed_at'])
            self.assertTrue(by_id[last]['observed_at'])
            if fault == '40001':
                self.assertEqual(evidence.pgcodes, ['40001'])
                self.assertEqual((by_id[broken]['state'], by_id[broken]['observed_at'],
                                  by_id[broken]['init_exit']), ('initializing', False, False))
            else:
                self.assertFalse(evidence.pgcodes)
                self.assertEqual((by_id[broken]['state'], by_id[broken]['init_exit']), ('error', 'wait_timeout'))
                self.assertTrue(by_id[broken]['observed_at'], '非 DB fail-soft 不否決 caller 原已允許 state')
            self.assertEqual((by_id[broken]['init_retries'], by_id[broken]['init_unknown_count']), (0, 0))
            evidence.send.assert_not_called()
            evidence.send_after_commit.assert_not_called()
        self._purge()
        sentinel_after = foreign.snapshot()
        self.assertEqual(sentinel_after, sentinel_before)
        self.assertFalse(any(self.scope.snapshot().values()))
        print('1159_BOUNDARY_GIVE_UP=' + json.dumps({'fault': fault, 'order': order, 'visits': visited,
            'pre_error': pre_error, 'rows': rows, 'sentinel_before': sentinel_before,
            'sentinel_after': sentinel_after, 'sentinels_unchanged': sentinel_after == sentinel_before,
            'own_cleaned': True}, default=str, sort_keys=True), flush=True)

    def _release_name(self, sid):
        return self._read(sid, 'helm_release_name')

    def test_give_up_database_notification_failure_rolls_back_only_current_poll_service(self):
        self._give_up_notification_boundary('40001')

    def test_give_up_non_database_notification_failure_keeps_allowed_state(self):
        self._give_up_notification_boundary('runtime')

    def test_recorded_outcome_monitor_failure_keeps_recovery_pointer_and_accounts_once(self):
        self._boundary_recipients()
        sid = self._add('odoo', ready_minutes_ago=1)
        with self.registry.cursor() as cr:
            env = self._env(cr)
            env[SVC].browse(sid).write({'init_wait_spent_seconds': 1799})
        client = operator_client()
        model = type(self.env[SVC])
        with patch.object(model, '_init_db_now', return_value=fields.Datetime.now()) as clock:
            def not_executed(namespace, release_name):
                with self.registry.cursor() as observer:
                    observer.execute('SELECT claimed_at FROM woow_paas_platform_cloud_service_init_attempt '
                                     'WHERE service_id = %s ORDER BY sequence DESC LIMIT 1', (sid,))
                    clock.return_value = observer.fetchone()[0] + timedelta(seconds=2)
                return {'success': False, 'error_kind': 'not_executed'}
            client.init_odoo.side_effect = not_executed
            with notification_fault(self, self.env[MONITOR].SUMMARY_GAVE_UP) as evidence:
                with self.registry.cursor() as cr:
                    env = self._env(cr)
                    self.assertTrue(env[SVC].browse(sid)._run_post_deploy_init(self.scope.operator(client)))
                before = self._recorded_boundary_facts(sid)
                self.assertEqual(evidence.pgcodes, ['40001'])
                with self.registry.cursor() as cr:
                    assert_notification_rollback(self, self._env(cr), evidence)
                self.assertEqual((before['state'], before['spent'], before['retries'], before['unknown']),
                                 ('initializing', 1799, 0, 0))
                self.assertTrue(before['pointer'] and before['lease'])
                self.assertEqual(before['outcome'], 'not_executed')
                self.assertTrue(before['finished'])
                self.assertFalse(before['applied'], 'DB fault 不得讓 accounting marker 假成立')
                evidence.send.assert_not_called()
                evidence.send_after_commit.assert_not_called()
            with self.registry.cursor() as cr:
                env = self._env(cr)
                self.assertFalse(env[SVC].browse(sid)._run_post_deploy_init(self.scope.operator(client)),
                                 '冷 recovery 只補套已記錄 outcome，不重送 operator')
            after = self._recorded_boundary_facts(sid)
            self.assertEqual((after['state'], after['exit'], after['spent'], after['retries'], after['unknown']),
                             ('error', 'wait_timeout', 1801, 0, 0))
            self.assertFalse(after['pointer'] or after['lease'])
            self.assertTrue(after['applied'])
            with self.registry.cursor() as cr:
                env = self._env(cr)
                self.assertFalse(env[SVC].browse(sid)._run_post_deploy_init(self.scope.operator(client)))
            self.assertEqual(self._recorded_boundary_facts(sid), after, '第二趟不重扣 quota／累計／marker')
            client.init_odoo.assert_called_once()
        print('1159_BOUNDARY_RECOVERY=' + json.dumps({'before': before, 'after': after,
            'operator_calls': client.init_odoo.call_count}, default=str, sort_keys=True), flush=True)

    def _recorded_boundary_facts(self, sid):
        with self.registry.cursor() as cr:
            env = self._env(cr)
            svc = env[SVC].browse(sid).sudo()
            attempt = env[ATTEMPT].search([('service_id', '=', sid)], order='sequence desc', limit=1)
            return {'state': svc.state, 'exit': svc.init_exit, 'spent': svc.init_wait_spent_seconds,
                'retries': svc.init_retries, 'unknown': svc.init_unknown_count,
                'pending': svc.init_pending, 'pointer': svc.init_attempt_id.id,
                'lease': bool(svc.init_lease_until), 'attempt': attempt.id,
                'outcome': attempt.outcome, 'finished': bool(attempt.finished_at),
                'applied': attempt.applied_at, 'key_persisted': attempt.n8n_key_persisted,
                'key_present': bool(svc.n8n_api_key),
                'key_hash': hashlib.sha256(svc.n8n_api_key.encode()).hexdigest() if svc.n8n_api_key else None}

    def test_n8n_owner_alert_database_failure_reaches_the_poll_job_boundary(self):
        """owner 告警的 DB 故障到短 poll job 邊界；先前已 commit 的結果不回捲。"""
        self._boundary_recipients()
        sid = self._add('n8n', ready_minutes_ago=6)
        upgrading = self._add('none', state='upgrading')
        fake_key = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJib3VuZGFyeSJ9.c2lnbmF0dXJl'
        init_client = operator_client(init_n8n={'success': True, 'api_key': fake_key})
        self._run_cron('_cron_run_post_deploy_inits', init_client)
        committed = self._recorded_boundary_facts(sid)
        self.assertEqual((committed['state'], committed['outcome']), ('running', 'success'))
        self.assertTrue(committed['applied'] and committed['key_persisted'] and committed['key_present'])
        poll_client = operator_client(get_status=status_response(pods=_READY_PODS))
        caught = None
        with self.scope.monitoring(), \
                notification_fault(self, self.env[MONITOR].SUMMARY_N8N_OWNER) as evidence:
            try:
                self._run_cron('_cron_check_deploying_services', poll_client)
            except OperationalError as error:
                caught = error
            self.assertEqual(evidence.pgcodes, ['40001'])
            self.assertIsNotNone(caught, 'owner 告警的 DB 故障不得在 monitor 層降成正常完成')
            self.assertEqual(caught.pgcode, '40001')
            with self.registry.cursor() as cr:
                assert_notification_rollback(self, self._env(cr), evidence)
            self.assertEqual(self._read(upgrading, 'state'), 'running',
                             '同一 tick 先前逐台 commit 的 poll 結果不回捲')
            self.assertEqual(self._recorded_boundary_facts(sid), committed,
                             '已 commit 的 success／key／quota 不因告警故障改變')
            self.assertFalse(self._read(sid, 'n8n_owner_alerted_at'), 'marker 隨告警一起回捲，可重試')
            # 真 poll cron 也跑其他既有 checks（sandbox 的 init cron 健康告警會同步寄出）；
            # 這裡只核 owner 告警自己的 mail 走 queue、沒有被同步寄出。
            sent = {mail.id for mock in (evidence.send, evidence.send_after_commit)
                    for call in mock.call_args_list for mail in call.args[0]}
            self.assertFalse(sent & set(evidence.mails), 'owner 告警只排 queue，不同步寄出部分通知')
        with self.scope.monitoring():
            self._run_cron('_cron_check_deploying_services', poll_client)
        self.assertTrue(self._read(sid, 'n8n_owner_alerted_at'), '故障後下一趟重新交付')
        init_client.init_n8n.assert_called_once()
        print('1159_BOUNDARY_N8N_OWNER=' + json.dumps({'facts': committed,
            'operator_calls': init_client.init_n8n.call_count}, default=str, sort_keys=True), flush=True)

    def test_cold_recovered_n8n_success_is_still_monitored(self):
        """live 套用失敗、下一趟冷補套 success，owner 監控照樣看得到（舊事件路徑看不到）。"""
        self._boundary_recipients()
        sid = self._add('n8n', ready_minutes_ago=7)
        fake_key = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJjb2xkIn0.c2lnbmF0dXJl'
        client = operator_client(init_n8n={'success': True, 'api_key': fake_key})
        model = type(self.env[SVC])
        original_apply = model._init_apply_outcome
        failures = []

        def fail_live_apply(records, *args, **kwargs):
            if not failures:
                failures.append(True)
                raise RuntimeError('1159 live apply bookkeeping failure')
            return original_apply(records, *args, **kwargs)

        with patch.object(model, '_init_apply_outcome', fail_live_apply):
            with self.registry.cursor() as cr:
                self._env(cr)[SVC].browse(sid)._run_post_deploy_init(self.scope.operator(client))
            recorded = self._recorded_boundary_facts(sid)
            self.assertEqual((recorded['state'], recorded['outcome']), ('initializing', 'success'))
            self.assertTrue(recorded['pointer'] and recorded['key_persisted'])
            self.assertFalse(recorded['applied'], '前提：live 套用真的失敗、留下 recovery pointer')
            with self.registry.cursor() as cr:
                self._env(cr)[SVC].browse(sid)._run_post_deploy_init(self.scope.operator(client))
        recovered = self._recorded_boundary_facts(sid)
        self.assertEqual(recovered['state'], 'running')
        self.assertTrue(recovered['applied'])
        client.init_n8n.assert_called_once()
        with self.scope.monitoring(), self.registry.cursor() as cr:
            env = self._env(cr)
            monitor = env[MONITOR]
            monitor._run_poll_cron_checks()
            notes = ' '.join(str(activity.note or '') for activity in env['mail.activity'].search([
                ('res_model', '=', MONITOR), ('res_id', '=', self.scope.monitor_id),
                ('summary', '=', monitor.SUMMARY_N8N_OWNER)]))
            name = env[SVC].browse(sid).name
        self.assertIn(name, notes, '冷補套的 success 第 7 分才收到，5 分內未確認')

    def test_init_job_health_failure_does_not_undo_previously_committed_attempts(self):
        self._boundary_recipients()
        sid = self._add('odoo', ready_minutes_ago=1)
        client = operator_client(init_odoo={'success': True})
        caught = None
        with self.scope.monitoring(), poll_health_time(self, 11), \
                notification_fault(self, self.env[MONITOR].SUMMARY_POLL_CRON) as evidence:
            try:
                self._run_cron('_cron_run_post_deploy_inits', client)
            except OperationalError as error:
                caught = error
            facts = self._recorded_boundary_facts(sid)
            with self.registry.cursor() as cr:
                assert_notification_rollback(self, self._env(cr), evidence)
            self.assertEqual(evidence.pgcodes, ['40001'])
            self.assertIsNotNone(caught, 'health fault 必須穿過真 cron 入口，而不是正常完成')
            self.assertEqual(caught.pgcode, '40001')
            self.assertEqual((facts['state'], facts['outcome'], facts['retries'], facts['unknown']),
                             ('running', 'success', 0, 0))
            self.assertTrue(facts['finished'] and facts['applied'])
            self.assertFalse(facts['pointer'] or facts['lease'])
            evidence.send.assert_not_called()
            evidence.send_after_commit.assert_not_called()
        with self.scope.monitoring(), poll_health_time(self, 5):
            self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(self._recorded_boundary_facts(sid), facts, '健康對照不重送已完成 service')
        client.init_odoo.assert_called_once()
        print('1159_BOUNDARY_INIT_HEALTH=' + json.dumps({'facts': facts, 'pgcode': caught.pgcode,
            'operator_calls': client.init_odoo.call_count}, default=str, sort_keys=True), flush=True)

    def test_init_job_non_database_health_failure_is_soft_after_committed_success(self):
        self._boundary_recipients()
        sid = self._add('odoo', ready_minutes_ago=1)
        client = operator_client(init_odoo={'success': True})
        with self.scope.monitoring(), poll_health_time(self, 11), \
                notification_fault(self, self.env[MONITOR].SUMMARY_POLL_CRON, fault='runtime') as evidence:
            self._run_cron('_cron_run_post_deploy_inits', client)
            facts = self._recorded_boundary_facts(sid)
            with self.registry.cursor() as cr:
                assert_notification_rollback(self, self._env(cr), evidence)
            self.assertFalse(evidence.pgcodes)
            self.assertEqual((facts['state'], facts['outcome'], facts['retries'], facts['unknown']),
                             ('running', 'success', 0, 0))
            self.assertTrue(facts['finished'] and facts['applied'])
            self.assertFalse(facts['pointer'] or facts['lease'])
            evidence.send.assert_not_called()
            evidence.send_after_commit.assert_not_called()
        with self.scope.monitoring(), poll_health_time(self, 5):
            self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(self._recorded_boundary_facts(sid), facts)
        client.init_odoo.assert_called_once()
        print('1159_BOUNDARY_INIT_HEALTH_SOFT=' + json.dumps({'facts': facts,
            'operator_calls': client.init_odoo.call_count}, default=str, sort_keys=True), flush=True)

    def test_slow_init_does_not_hold_back_the_short_poll(self):
        """一筆慢 init 進行中，另一台的 upgrading→running 在短 poll 落地且別的連線看得到。"""
        slow = self._add('odoo', ready_minutes_ago=2)
        upgrading = self._add('none', state='upgrading')
        in_flight, release = threading.Event(), threading.Event()

        def slow_init(**kwargs):
            in_flight.set()
            release.wait(30)
            return {'success': True}

        init_client = operator_client()
        init_client.init_odoo.side_effect = slow_init
        errors = []

        def run_init_cron():
            try:
                with self.registry.cursor() as cr:
                    self._env(cr)[SVC].browse(slow)._run_post_deploy_init(self.scope.operator(init_client))
            except Exception as exc:  # 讓主執行緒看得到
                errors.append(exc)

        thread = threading.Thread(target=run_init_cron)
        thread.start()
        try:
            self.assertTrue(in_flight.wait(20), '%s\n%s' % (errors, _thread_stacks(thread)))
            poll_client = operator_client(get_status=status_response(pods=_READY_PODS))
            with self.registry.cursor() as cr:
                # 保留 env 參照到 cursor 結束：`Transaction.envs` 是 WeakSet，env 被回收
                # 之後 exit 的 commit 找不到 env 可 flush，pending 寫入會被丟掉。
                svc = self._env(cr)[SVC].browse(upgrading)
                svc._poll_and_update_status(self.scope.operator(poll_client))
            self.assertEqual(self._read(upgrading, 'state'), 'running',
                             '另一條連線必須看得到短 poll 的結果（init 仍在途）')
            self.assertEqual(self._read(slow, 'state'), 'initializing')
        finally:
            release.set()
            thread.join(60)
        self.assertFalse(errors, errors)
        self.assertEqual(self._read(slow, 'state'), 'running')

    def test_short_poll_commits_each_service_and_one_failure_does_not_stop_the_tick(self):
        """驗收 2：deleting 由背景在 404 後 unlink；一台 teardown 失敗，其他台照常收尾。"""
        broken = self._add('none', state='deleting')
        gone = self._add('none', state='deleting')
        upgrading = self._add('none', state='upgrading')
        releases = {self._read(sid, 'helm_release_name'): sid
                    for sid in (broken, gone, upgrading)}

        def get_status(namespace, release_name, include_vm=False):
            if releases[release_name] == upgrading:
                return status_response(pods=_READY_PODS)
            raise PaaSOperatorError('release not found', status_code=404)

        client = operator_client()
        client.get_status.side_effect = get_status
        model = type(self.env[SVC])
        original_unlink = model.unlink

        def unlink(records):
            if broken in records.ids:
                raise RuntimeError('teardown boom')
            return original_unlink(records)

        with patch.object(model, 'unlink', unlink):
            self._run_cron('_cron_check_deploying_services', client)
        self.assertEqual(self._read(broken, 'state'), 'deleting', '失敗的那台留在 deleting')
        self.assertIsNone(self._read(gone, 'state'), 'release 已不在的 deleting 被 unlink')
        self.assertEqual(self._read(upgrading, 'state'), 'running')


@tagged('post_install', '-at_install')
class TestDnsReprobeCron1159(_RealCursorCase):
    """Q16／D4：`public_url_dns_unverified` 改由背景低頻重探清除（list/get 純讀）。"""

    def _running(self, subdomain, flagged=True):
        sid = self._add('none', state='running', subdomain=subdomain)
        with self.registry.cursor() as cr:
            svc = self._env(cr)[SVC].browse(sid)
            svc.write({'public_url_dns_unverified': flagged})
        return sid

    def test_flag_is_cleared_once_dns_resolves_and_kept_otherwise(self):
        landed = self._running(f'landed-{self.tag}')
        missing = self._running(f'missing-{self.tag}')
        resolver_down = self._running(f'down-{self.tag}')
        healthy = self._running(f'healthy-{self.tag}', flagged=False)
        probed = []

        def resolve(hostname, *args, **kwargs):
            probed.append(hostname)
            if hostname.startswith(f'landed-{self.tag}'):
                return 'ok'
            if hostname.startswith(f'down-{self.tag}'):
                return 'unknown'
            return 'missing'

        with patch('odoo.addons.woow_paas_platform.services.dns_probe.resolve_state',
                   side_effect=resolve):
            self._run_cron('_cron_reprobe_public_url_dns', operator_client())
        self.assertFalse(self._read(landed, 'public_url_dns_unverified'))
        self.assertTrue(self._read(missing, 'public_url_dns_unverified'))
        self.assertTrue(self._read(resolver_down, 'public_url_dns_unverified'),
                        'resolver 不可用是平台故障，不清也不改')
        self.assertFalse(any(h.startswith(f'healthy-{self.tag}') for h in probed),
                         '沒帶旗標的服務不探（成本綁在出問題的那幾台）')

    def test_cron_record_runs_every_fifteen_minutes(self):
        with self.registry.cursor() as cr:
            cron = self._env(cr).ref('woow_paas_platform.ir_cron_reprobe_public_url_dns')
            self.assertEqual((cron.interval_number, cron.interval_type, cron.active),
                             (15, 'minutes', True))


# ====================================================================== #
#  驗收 6：背景健康監控（Q38／D3，掛點＝mail.thread 單例 platform_monitor）   #
# ====================================================================== #

MONITOR = 'woow_paas_platform.platform_monitor'
POLL_CRON = 'woow_paas_platform.ir_cron_check_deploying_services'


@tagged('post_install', '-at_install')
class TestPlatformMonitor1159(_ServiceCase):

    def setUp(self):
        super().setUp()
        self.Monitor = self.env[MONITOR]
        self.singleton = self.Monitor._singleton()
        self.assertEqual(self.singleton.id, self.scope.monitor_id, '每個 monitor case 有自有 target')
        self.admins = self.env.ref('base.group_system').users.filtered(
            lambda u: u.active and not u.share)
        # 兩個 cron 都「剛跑過」＝健康的基準線；個別測試再把它弄舊。
        for xmlid in (INIT_CRON, POLL_CRON):
            self.env.ref(xmlid).sudo().write({'active': True, 'lastcall': fields.Datetime.now()})

    def _activities(self, summary=None):
        domain = [('res_model', '=', MONITOR), ('res_id', '=', self.singleton.id)]
        if summary:
            domain.append(('summary', '=', summary))
        return self.env['mail.activity'].sudo().search(domain)

    def _unknown_attempt(self, service, sequence, finished_at=None, outcome='unknown'):
        return self.env[ATTEMPT].sudo().create({
            'service_id': service.id,
            'generation': 1,
            'sequence': sequence,
            'init_type': 'odoo',
            'claimed_at': fields.Datetime.now() - timedelta(hours=1),
            'lease_until': fields.Datetime.now(),
            'finished_at': finished_at,
            'outcome': outcome,
        })

    def test_safe_alert_database_failure_reaches_the_caller_with_a_usable_cursor(self):
        runner = prepare_system_recipients(self, self.env)
        summary = '1159 boundary safe alert'
        caught = None
        with notification_fault(self, summary) as evidence:
            try:
                runner._safe_alert(summary, 'own monitor boundary notification')
            except OperationalError as error:
                caught = error
            assert_notification_rollback(self, self.env, evidence)
            self.assertEqual(evidence.pgcodes, ['40001'], '真 PG40001 必須到達')
            self.assertIsNotNone(caught, 'DB failure 不得在 helper 層降成 False')
            self.assertEqual(caught.pgcode, '40001')
            evidence.send.assert_not_called()
            evidence.send_after_commit.assert_not_called()

    def test_init_side_health_database_failure_reaches_the_job_boundary(self):
        runner = prepare_system_recipients(self, self.env)
        # 本 test 不 commit；真 cron metadata 由 TransactionCase 原樣回捲，不改共享 runtime。
        self.env.ref(POLL_CRON).sudo().write({
            'lastcall': fields.Datetime.now() - timedelta(minutes=11)})
        caught = None
        with notification_fault(self, self.Monitor.SUMMARY_POLL_CRON) as evidence:
            try:
                runner._run_init_cron_checks()
            except OperationalError as error:
                caught = error
            assert_notification_rollback(self, self.env, evidence)
            self.assertEqual(evidence.pgcodes, ['40001'])
            self.assertIsNotNone(caught, 'init-side health DB failure 不得正常 return')
            self.assertEqual(caught.pgcode, '40001')
            evidence.send.assert_not_called()
            evidence.send_after_commit.assert_not_called()

    def test_safe_alert_non_database_failure_is_soft_and_preserves_caller_state(self):
        runner = prepare_system_recipients(self, self.env)
        svc = self._service('none', state='error', error_message='own allowed caller state')
        summary = '1159 boundary non database alert'
        with notification_fault(self, summary, fault='runtime') as evidence:
            result = runner._safe_alert(summary, 'own non database notification')
            self.assertIs(result, False, 'RuntimeError 維持原 fail-soft return')
            assert_notification_rollback(self, self.env, evidence)
            self.assertFalse(evidence.pgcodes)
            self.assertEqual((svc.state, svc.error_message), ('error', 'own allowed caller state'))
            evidence.send.assert_not_called()
            evidence.send_after_commit.assert_not_called()

    def test_init_side_health_non_database_failure_is_soft_and_healthy_check_is_real(self):
        runner = prepare_system_recipients(self, self.env)
        self.assertIsNone(runner._run_init_cron_checks())
        self.assertFalse(self._activities(self.Monitor.SUMMARY_POLL_CRON), '真 healthy metadata 不告警')
        self.env.ref(POLL_CRON).sudo().write({
            'lastcall': fields.Datetime.now() - timedelta(minutes=11)})
        with notification_fault(self, self.Monitor.SUMMARY_POLL_CRON, fault='runtime') as evidence:
            self.assertIsNone(runner._run_init_cron_checks(), '非 DB 失敗維持原 return 語意')
            assert_notification_rollback(self, self.env, evidence)
            self.assertFalse(evidence.pgcodes)
            evidence.send.assert_not_called()
            evidence.send_after_commit.assert_not_called()

    def test_capacity_wait_alerts_system_users_at_thirty_minutes_without_spending_quota(self):
        """容量時計只看持續可 dispatch 的平台觀測，不回充舊 first-ready。"""
        svc = self._service('odoo', state='initializing')
        self._poll(svc)
        started = svc._init_db_now()
        svc.sudo().write({'init_ready_since': started - timedelta(hours=2)})
        summary = 'PaaS monitor: eligible initialization is waiting for capacity'
        before = svc.read(['state', 'init_pending', 'init_retries', 'init_unknown_count',
                           'init_wait_spent_seconds', 'init_wait_started_at',
                           'init_retry_eligible_at', 'init_queue_turn'])[0]
        with patch.object(type(svc), '_init_db_now', return_value=started) as clock:
            self.Monitor._run_poll_cron_checks()
            self.assertFalse(self._activities(summary), 'legacy 時計不得回充 first-ready 歷史')
            clock.return_value = started + timedelta(minutes=30) - timedelta(seconds=1)
            self.Monitor._run_poll_cron_checks()
            self.assertFalse(self._activities(summary), '門檻前不得告警')
            clock.return_value = started + timedelta(minutes=30)
            self.Monitor._run_poll_cron_checks()
            activities = self._activities(summary)
            self.assertTrue(activities, '持續 eligible-unclaimed 達 30 分鐘必須通知平台')
            self.assertEqual(set(activities.mapped('user_id')), set(self.admins))
            self.assertIn(svc.name, activities[:1].note or '')
            original_notes = activities.mapped('note')
            clock.return_value = started + timedelta(minutes=40)
            self.Monitor._run_poll_cron_checks()
            self.assertEqual(self._activities(summary), activities, '持續條件不另建 activity')
            self.assertEqual(activities.mapped('note'), original_notes, '重複 tick 不重複 append')
        self.assertEqual(svc.read(list(before.keys() - {'id'}))[0], before,
                         '告警不得改 state、quota、operator-wait 預算或排序')

    def test_capacity_ready_revocation_between_monitor_ticks_restarts_the_episode(self):
        svc = self._service('odoo', state='initializing')
        self._poll(svc)
        started = svc._init_db_now()
        summary = self.Monitor.SUMMARY_CAPACITY
        with patch.object(type(svc), '_init_db_now', return_value=started) as clock:
            self.Monitor._run_poll_cron_checks()
            clock.return_value = started + timedelta(minutes=20)
            self._poll(svc, pods=_NOT_READY_PODS)
            self.assertFalse(svc.init_ready_observed, 'Ready 真被背景撤回')
            self._poll(svc)
            self.assertTrue(svc.init_ready_observed, '同一 monitor tick 前又恢復 Ready')
            self.Monitor._run_poll_cron_checks()
            clock.return_value = started + timedelta(minutes=30)
            self.Monitor._run_poll_cron_checks()
            self.assertFalse(self._activities(summary), '中斷後不能沿用舊容量 episode')
            clock.return_value = started + timedelta(minutes=50)
            self.Monitor._run_poll_cron_checks()
            self.assertIn(svc.name, self._activities(summary)[:1].note or '')

    def test_capacity_dispatch_blockers_do_not_start_a_clock_or_change_service_state(self):
        started = self.env[SVC]._init_db_now()
        blockers = {
            'redeploy': {'init_redeploy_requested': True},
            'cooldown': {'init_wait_started_at': started,
                         'init_retry_eligible_at': started + timedelta(hours=4)},
            'failed_quota': {'init_retries': 5},
            'unknown_quota': {'init_unknown_count': 2},
            'operator_budget': {'init_wait_spent_seconds': 1800},
            'operator_interval_budget': {'init_wait_spent_seconds': 1790,
                'init_wait_started_at': started - timedelta(seconds=60),
                'init_retry_eligible_at': started - timedelta(seconds=1)},
            'not_owed': {'init_pending': False},
            'not_ready': {'init_ready_observed': False},
            'state_exit': {'state': 'suspended'},
        }
        with patch.object(type(self.env[SVC]), '_init_db_now', return_value=started) as clock:
            for reason, controls in blockers.items():
                with self.subTest(reason=reason):
                    svc = self._service('odoo', state='initializing')
                    self._poll(svc)
                    self.Monitor._run_poll_cron_checks()
                    self.assertTrue(svc.init_capacity_wait_since, '前提：先建立真 eligible episode')
                    svc.sudo().write(controls)
                    before = svc.read(['state', 'init_pending', 'init_retries', 'init_unknown_count',
                                       'init_wait_spent_seconds', 'init_wait_started_at',
                                       'init_retry_eligible_at'])[0]
                    clock.return_value = started + timedelta(minutes=40)
                    self.Monitor._run_poll_cron_checks()
                    self.assertFalse(svc.init_capacity_wait_since, '%s 不是真容量等待' % reason)
                    self.assertFalse(self._activities(self.Monitor.SUMMARY_CAPACITY))
                    self.assertEqual(svc.read(list(before.keys() - {'id'}))[0], before,
                                     '監控不能將 exhausted／timeout 自行變成 error')
                    clock.return_value = started
            none = self._service('none', state='initializing')
            none.sudo().write({'init_ready_observed': True})
            self.Monitor._run_poll_cron_checks()
            self.assertFalse(none.init_capacity_wait_since, 'legacy none 類型不 dispatch')

    def test_capacity_lifecycle_interruptions_between_ticks_start_new_episodes(self):
        started = self.env[SVC]._init_db_now()
        for boundary in ('generation', 'state_exit', 'not_owed', 'redeploy', 'cooldown'):
            with self.subTest(boundary=boundary):
                svc = self._service('odoo', state='initializing')
                with patch.object(type(svc), '_init_db_now', return_value=started) as clock:
                    self._poll(svc)
                    self.Monitor._run_poll_cron_checks()
                    clock.return_value = started + timedelta(minutes=20)
                    if boundary == 'generation':
                        svc._open_init_generation()
                    elif boundary == 'state_exit':
                        svc.sudo().write({'state': 'suspended'})
                        svc.sudo().write({'state': 'initializing'})
                        self._poll(svc)
                    elif boundary == 'not_owed':
                        svc.sudo().write({'init_pending': False})
                        svc._open_init_generation()
                    elif boundary == 'redeploy':
                        svc.sudo().write({'init_redeploy_requested': True})
                        svc.sudo().write({'init_redeploy_requested': False})
                    else:
                        svc.sudo().write({'init_wait_started_at': clock.return_value,
                            'init_retry_eligible_at': clock.return_value + timedelta(seconds=60)})
                        # 已有 real not_executed cooldown 回歸；此處只釘 tick 之間的中斷。
                        svc.sudo().write({'init_wait_started_at': False, 'init_retry_eligible_at': False})
                    self.Monitor._run_poll_cron_checks()
                    self.assertEqual(svc.init_capacity_wait_since, clock.return_value)
                    clock.return_value = started + timedelta(minutes=30)
                    self.Monitor._run_poll_cron_checks()
                    self.assertFalse(self._activities(self.Monitor.SUMMARY_CAPACITY))
                    clock.return_value = started + timedelta(minutes=50)
                    self.Monitor._run_poll_cron_checks()
                    self.assertIn(svc.name, self._activities(self.Monitor.SUMMARY_CAPACITY)[:1].note or '')
                    self._activities(self.Monitor.SUMMARY_CAPACITY).action_feedback(feedback='handled')
                    svc.sudo().write({'state': 'running', 'init_pending': False})

    def test_capacity_same_state_and_ready_refresh_do_not_restart_waiting(self):
        svc = self._service('n8n', state='initializing')
        self._poll(svc)
        started = svc._init_db_now()
        with patch.object(type(svc), '_init_db_now', return_value=started) as clock:
            self.Monitor._run_poll_cron_checks()
            clock.return_value = started + timedelta(minutes=20)
            svc.sudo().write({'state': 'initializing', 'name': '同 state 無關更新'})
            self._poll(svc)
            self.assertEqual(svc.init_capacity_wait_since, started)
            clock.return_value = started + timedelta(minutes=30)
            self.Monitor._run_poll_cron_checks()
            activities = self._activities(self.Monitor.SUMMARY_CAPACITY)
            self.assertTrue(activities)
            activities.action_feedback(feedback='condition checked')
            self.Monitor._run_poll_cron_checks()
            self.assertTrue(self._activities(self.Monitor.SUMMARY_CAPACITY),
                            '條件告警全部完成後仍持續，沿慣例可再開；不是 attempt event ledger')

    def test_capacity_clock_platform_authority_and_create_default_injection(self):
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': '1159 capacity workspace admin', 'login': f'1159-capacity-{self.tag}',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': 'admin'})
        svc = self._service('odoo', state='initializing')
        managed = svc.with_user(user).with_context(lang='en_US')
        managed.write({'name': '合法容量一般欄位更新'})
        self.assertEqual(managed.name, '合法容量一般欄位更新')
        stamp = fields.Datetime.now()
        try:
            managed.write({'init_capacity_wait_since': stamp})
        except AccessError as exc:
            self.assertIn('managed by the platform', str(exc))
        else:
            self.fail('workspace admin 不得偽造容量時計')
        platform = svc.with_user(self.admins.filtered(lambda admin: admin.id != SUPERUSER_ID)[0])
        self.assertFalse(platform.env.su, '陽性對照必須是真 system user，不是 su-env 假綠')
        platform.write({'init_capacity_wait_since': stamp})
        self.assertEqual(platform.init_capacity_wait_since, stamp)
        future = stamp + timedelta(days=3)
        fake = fields.Datetime.to_string(future)
        creator = _internal_provision(self.env[SVC].with_user(user).sudo())
        self.assertEqual(creator.env.uid, user.id, 'sudo 仍會讀使用者 ir.default')
        for carrier in ('values', 'context', 'ir.default'):
            for init_type in ('none', 'odoo'):
                with self.subTest(carrier=carrier, init_type=init_type):
                    model = creator
                    vals = {'name': f'capacity {carrier} {init_type}',
                            'workspace_id': self.workspace.id, 'template_id': self.templates[init_type].id,
                            'state': 'initializing'}
                    if carrier == 'values':
                        vals['init_capacity_wait_since'] = fake
                    elif carrier == 'context':
                        model = model.with_context(default_init_capacity_wait_since=fake)
                    else:
                        self.env['ir.default'].with_user(user).set(
                            SVC, 'init_capacity_wait_since', fake, user_id=user.id)
                    if carrier != 'values':
                        defaults = model.default_get(['init_capacity_wait_since'])
                        self.assertEqual(fields.Datetime.to_datetime(defaults['init_capacity_wait_since']),
                                         future, '前提：default 注入真的抵達 framework')
                    created = model.create(vals)
                    self.assertFalse(created.init_capacity_wait_since,
                                     '新時計不得吃 caller 的 raw／context／ir.default')
                    created.sudo().write({'init_capacity_wait_since': stamp})
                    self.assertEqual(created.init_capacity_wait_since, stamp, '平台後續合法寫入仍成功')

    def test_capacity_delivery_rolls_back_partial_mail_and_propagates_database_conflicts(self):
        Mail = self.env['mail.mail'].sudo()
        self.env['res.users'].with_context(no_reset_password=True).create([
            {'name': f'1159 capacity recipient {suffix}', 'login': f'1159-cap-mail-{suffix}-{self.tag}',
             'email': f'1159-cap-mail-{suffix}-{self.tag}@example.invalid', 'notification_type': 'email',
             'groups_id': [(6, 0, [self.env.ref('base.group_system').id])]}
            for suffix in ('a', 'b')])
        recipients = self.env.ref('base.group_system').users.filtered(
            lambda user: user.active and not user.share)
        recipients[:1].write({'email': f'1159-cap-first-{self.tag}@example.invalid',
                             'notification_type': 'email'})
        runner = self.Monitor.with_user(recipients[-1]).sudo()
        self.assertNotEqual(recipients[0], runner.env.user)
        svc = self._service('odoo', state='initializing')
        self._poll(svc)
        started = svc._init_db_now()
        with patch.object(type(svc), '_init_db_now', return_value=started):
            runner._run_poll_cron_checks()
        mail_domain = [('model', '=', MONITOR), ('res_id', '=', self.singleton.id)]
        original_schedule = type(self.singleton).activity_schedule
        for database_error in (False, True):
            with self.subTest(database_error=database_error):
                before_mails = set(Mail.search(mail_domain).ids)
                calls, first_activities, first_mails = [], [], []

                def fail_second(records, *args, **kwargs):
                    if kwargs.get('summary') != self.Monitor.SUMMARY_CAPACITY:
                        return original_schedule(records, *args, **kwargs)
                    calls.append(kwargs['user_id'])
                    if len(calls) == 2:
                        if database_error:
                            records.env.cr.execute("""
                                DO $$ BEGIN
                                    RAISE EXCEPTION '1159 capacity monitor conflict'
                                        USING ERRCODE = '40001';
                                END $$
                            """)
                        raise RuntimeError('1159 second capacity recipient failed')
                    activity = original_schedule(records, *args, **kwargs)
                    records.env.flush_all()
                    first_activities.extend(activity.ids)
                    first_mails.extend(set(Mail.search(mail_domain).ids) - before_mails)
                    return activity

                with patch.object(type(svc), '_init_db_now', return_value=started + timedelta(minutes=30)), \
                        patch.object(type(Mail), 'send', autospec=True) as send, \
                        patch.object(type(Mail), 'send_after_commit', autospec=True) as send_after_commit:
                    caught = None
                    with patch.object(type(self.singleton), 'activity_schedule', autospec=True,
                                      side_effect=fail_second):
                        try:
                            runner._run_poll_cron_checks()
                        except OperationalError as exc:
                            caught = exc
                    self.assertEqual(calls, recipients[:2].ids)
                    self.assertTrue(first_activities, '故障前真建立 activity，不是 mock 假綠')
                    self.assertTrue(first_mails, '故障前真排入 mail queue')
                    if database_error:
                        self.assertIsNotNone(caught, 'OperationalError 必須交回真正 transaction 邊界')
                        self.assertEqual(caught.pgcode, '40001')
                    else:
                        self.assertIsNone(caught)
                    self.assertFalse(self._activities(self.Monitor.SUMMARY_CAPACITY))
                    self.assertEqual(set(Mail.search(mail_domain).ids), before_mails)
                    self.assertFalse(Mail.browse(first_mails).exists())
                    self.assertEqual(svc.init_capacity_wait_since, started, '回捲不能抹掉先前 episode')
                    self.env.cr.execute('SELECT 1')
                    self.assertEqual(self.env.cr.fetchone(), (1,), '產品 savepoint 真復原 aborted cursor')
                    runner._run_poll_cron_checks()
                    self.assertEqual(set(self._activities(self.Monitor.SUMMARY_CAPACITY).mapped('user_id')),
                                     set(recipients), '失敗後重試仍交付完整 system user 名單')
                    send.assert_not_called()
                    send_after_commit.assert_not_called()
                self._activities(self.Monitor.SUMMARY_CAPACITY).with_context(
                    mail_notify_force_send=False).action_feedback(feedback='capacity verified')

    def test_singleton_is_the_data_record(self):
        with self.scope.paused():
            original = self.Monitor._singleton()
            self.assertEqual(original, self.env.ref('woow_paas_platform.platform_monitor_singleton'))
            self.assertEqual(self.Monitor._singleton(), original, '原查找仍以 xmlid 為準，不重建')
        self.assertNotEqual(self.singleton, original, 'test-owned target 不改全域 xmlid')
        self.assertEqual(self.Monitor._singleton(), self.singleton)

    def test_alert_goes_to_every_system_user_and_dedupes_per_summary(self):
        self.assertTrue(self.admins)
        self.assertTrue(self.Monitor._alert('1159 test A', 'first'))
        self.assertEqual(set(self._activities('1159 test A').mapped('user_id')), set(self.admins))
        self.assertFalse(self.Monitor._alert('1159 test A', 'second'), '同 summary 未處理前不重建')
        self.assertEqual(len(self._activities('1159 test A')), len(self.admins))
        self.assertTrue(self.Monitor._alert('1159 test B', 'other'), '不同監控項各自去重')

    def test_non_system_user_cannot_read_the_monitor_or_its_activities(self):
        self.Monitor._alert('1159 test secret', 'platform only')
        user = self.env['res.users'].create({
            'name': '1159 plain user', 'login': f'u1159-{self.tag}',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]})
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.Monitor.with_user(user).search([])
        self.assertFalse(self.env['mail.activity'].with_user(user).search([
            ('res_model', '=', MONITOR)]))

    def test_init_cron_health_is_checked_by_the_short_poll(self):
        cron = self.env.ref(INIT_CRON).sudo()
        summary = self.Monitor.SUMMARY_INIT_CRON
        self.Monitor._check_init_cron_health()
        self.assertFalse(self._activities(summary), '健康時不告警')
        cron.write({'lastcall': fields.Datetime.now() - timedelta(minutes=41)})
        self.Monitor._check_init_cron_health()
        self.assertTrue(self._activities(summary))
        self._activities(summary).unlink()
        cron.write({'lastcall': fields.Datetime.now(), 'active': False})
        self.Monitor._check_init_cron_health()
        self.assertTrue(self._activities(summary), 'init cron 被停用也要告警')

    def test_short_poll_health_is_checked_by_the_init_cron(self):
        summary = self.Monitor.SUMMARY_POLL_CRON
        self.Monitor._check_poll_cron_health()
        self.assertFalse(self._activities(summary))
        self.env.ref(POLL_CRON).sudo().write(
            {'lastcall': fields.Datetime.now() - timedelta(minutes=11)})
        self.Monitor._check_poll_cron_health()
        self.assertTrue(self._activities(summary))

    def test_each_cron_runs_its_side_of_the_mutual_check(self):
        model = type(self.Monitor)
        service_model = type(self.env[SVC])
        # 此處只驗 monitor 接線；服務處理另有真 cursor 回歸，不在這條測試修改全庫服務。
        # cron 會 commit，不能再把它送進 TransactionCase 的禁 commit cursor。
        with patch.object(model, '_run_poll_cron_checks', autospec=True) as poll_side, \
                patch.object(model, '_run_init_cron_checks', autospec=True) as init_side, \
                patch.object(service_model, '_cron_run_requested_redeploys', autospec=True), \
                patch.object(service_model, '_init_settle_operator_wait_timeouts', autospec=True), \
                patch.object(service_model, '_init_cron_queue', autospec=True,
                             side_effect=lambda records: records.browse()), \
                patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=None):
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env[SVC]._cron_check_deploying_services()
                poll_side.assert_called_once()
                init_side.assert_not_called()
                poll_side.reset_mock()
                env[SVC]._cron_run_post_deploy_inits()
                init_side.assert_called_once()
                poll_side.assert_not_called()

    def test_oldest_observation(self):
        summary = self.Monitor.SUMMARY_OBSERVED
        svc = self._service('none', state='deploying')
        svc.sudo().write({
            'observation_episode_started_at': fields.Datetime.now() - timedelta(minutes=20),
            'observed_at': fields.Datetime.now() - timedelta(minutes=10),
        })
        pending = self._service('none', state='pending')
        pending.sudo().write({'observed_at': fields.Datetime.now() - timedelta(hours=5)})
        self.Monitor._check_oldest_observation()
        self.assertFalse(self._activities(summary), 'pending 不在範圍；10 分未超過門檻')
        svc.sudo().write({'observed_at': fields.Datetime.now() - timedelta(minutes=16)})
        self.Monitor._check_oldest_observation()
        self.assertIn(svc.name, self._activities(summary)[:1].note or '')

    def test_unknown_results_in_the_last_day(self):
        summary = self.Monitor.SUMMARY_GAVE_UP
        svc = self._service('odoo', state='initializing')
        self.Monitor._check_unknown_results()
        self.assertFalse(self._activities(summary))
        attempt = self._unknown_attempt(
            svc, 1, finished_at=fields.Datetime.now() - timedelta(minutes=30))
        self.Monitor._check_unknown_results()
        self.assertTrue(self._activities(summary))
        self.assertIn(attempt, self.singleton.notified_unknown_attempt_ids)

    def test_unknown_attempt_does_not_reopen_after_all_recipients_finish(self):
        summary = self.Monitor.SUMMARY_GAVE_UP
        attempt = self._unknown_attempt(
            self._service('odoo', state='initializing'), 1, fields.Datetime.now())
        self.Monitor._check_unknown_results()
        self.assertEqual(len(self._activities(summary)), len(self.admins))
        self._activities(summary).action_feedback(feedback='handled')
        self.assertFalse(self._activities(summary))

        self.Monitor._check_unknown_results()
        self.assertFalse(self._activities(summary), '同一 attempt 在 24h 內不得重開')
        self.assertIn(attempt, self.singleton.notified_unknown_attempt_ids)

    def test_notified_unknown_attempt_identity_expires_with_the_monitor_window(self):
        attempt = self._unknown_attempt(
            self._service('odoo', state='initializing'), 1, fields.Datetime.now())
        self.Monitor._check_unknown_results()
        self.assertIn(attempt, self.singleton.notified_unknown_attempt_ids)

        attempt.write({'finished_at': fields.Datetime.now() - timedelta(hours=25)})
        self.Monitor._check_unknown_results()
        self.assertNotIn(attempt, self.singleton.notified_unknown_attempt_ids)

    def test_late_finished_lower_id_unknown_attempt_is_delivered(self):
        summary = self.Monitor.SUMMARY_GAVE_UP
        early_service = self._service('odoo', state='initializing')
        late_service = self._service('odoo', state='initializing')
        early = self._unknown_attempt(early_service, 1)
        later = self._unknown_attempt(late_service, 1, fields.Datetime.now(), 'partial')
        self.Monitor._check_unknown_results()
        self.assertIn(later, self.singleton.notified_unknown_attempt_ids)
        self.assertNotIn(early, self.singleton.notified_unknown_attempt_ids)

        early.write({'finished_at': fields.Datetime.now()})
        self.Monitor._check_unknown_results()
        self.assertIn(early, self.singleton.notified_unknown_attempt_ids)
        self.assertIn(early_service.name, self._activities(summary)[:1].note or '')

    def test_new_unknown_attempt_reopens_done_recipient_and_appends_active_recipient(self):
        summary = self.Monitor.SUMMARY_GAVE_UP
        extra = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': '1159 extra system user',
            'login': f'1159-extra-{self.tag}',
            'groups_id': [(6, 0, [self.env.ref('base.group_system').id])],
        })
        recipients = self.env.ref('base.group_system').users.filtered(
            lambda user: user.active and not user.share)
        self.assertIn(extra, recipients)
        first = recipients[0]
        still_active = extra if extra != first else recipients[1]
        old = self._unknown_attempt(
            self._service('odoo', state='initializing'), 1, fields.Datetime.now())
        self.Monitor._check_unknown_results()
        old_activity = self._activities(summary).filtered(lambda activity: activity.user_id == first)
        active_activity = self._activities(summary).filtered(
            lambda activity: activity.user_id == still_active)
        active_activity_id = active_activity.id
        old_activity.with_user(first).action_feedback(feedback='handled only by this user')

        new_service = self._service('odoo', state='initializing')
        new = self._unknown_attempt(new_service, 1, fields.Datetime.now(), 'partial')
        self.Monitor._check_unknown_results()
        activities = self._activities(summary)
        reopened = activities.filtered(lambda activity: activity.user_id == first)
        appended = activities.filtered(lambda activity: activity.user_id == still_active)
        self.assertTrue(reopened)
        self.assertEqual(appended.id, active_activity_id)
        self.assertIn(new_service.name, reopened.note or '')
        self.assertIn(new_service.name, appended.note or '')
        self.assertIn(old, self.singleton.notified_unknown_attempt_ids)
        self.assertIn(new, self.singleton.notified_unknown_attempt_ids)

    def test_unknown_attempt_is_not_marked_when_no_recipient_or_delivery_fails(self):
        summary = self.Monitor.SUMMARY_GAVE_UP
        no_recipient = self._unknown_attempt(
            self._service('odoo', state='initializing'), 1, fields.Datetime.now())
        self.admins.write({'active': False})
        self.Monitor._check_unknown_results()
        self.assertFalse(self._activities(summary))
        self.assertNotIn(no_recipient, self.singleton.notified_unknown_attempt_ids)
        self.admins.write({'active': True})
        self.Monitor._check_unknown_results()
        self.assertIn(no_recipient, self.singleton.notified_unknown_attempt_ids)
        self._activities(summary).action_feedback(feedback='handled before failure test')

        delivery_failure = self._unknown_attempt(
            self._service('odoo', state='initializing'), 1, fields.Datetime.now())
        with patch.object(type(self.singleton), 'activity_schedule', autospec=True,
                          side_effect=RuntimeError('mail activity delivery failed')):
            self.Monitor._check_unknown_results()
        self.assertNotIn(delivery_failure, self.singleton.notified_unknown_attempt_ids)
        self.Monitor._check_unknown_results()
        self.assertIn(delivery_failure, self.singleton.notified_unknown_attempt_ids)

    def test_unknown_second_recipient_failure_rolls_back_all_delivery(self):
        Mail = self.env['mail.mail'].sudo()
        with patch.object(type(Mail), 'send', autospec=True) as send, \
                patch.object(type(Mail), 'send_after_commit', autospec=True) as send_after_commit:
            self.env['res.users'].with_context(no_reset_password=True).create([
                {'name': f'1159 mail recipient {suffix}',
                 'login': f'1159-mail-{suffix}-{self.tag}',
                 'email': f'1159-mail-{suffix}-{self.tag}@example.invalid',
                 'notification_type': 'email',
                 'groups_id': [(6, 0, [self.env.ref('base.group_system').id])]}
                for suffix in ('a', 'b')])
            recipients = self.env.ref('base.group_system').users.filtered(
                lambda user: user.active and not user.share)
            self.assertGreaterEqual(len(recipients), 2)
            # 只在會回捲的 TransactionCase 調整首位收件者，以真 mail queue 作為前提。
            recipients[:1].write({
                'email': f'1159-first-{self.tag}@example.invalid', 'notification_type': 'email'})
            runner = self.Monitor.with_user(recipients[-1]).sudo()
            self.assertNotEqual(recipients[0], runner.env.user, '排程者不可就是首位收件者')
            attempt = self._unknown_attempt(
                self._service('odoo', state='initializing'), 1, fields.Datetime.now())
            mail_domain = [('model', '=', MONITOR), ('res_id', '=', self.singleton.id)]
            before_mail_ids = set(Mail.search(mail_domain).ids)
            created_activity_ids = []
            created_mail_ids = []
            original_schedule = type(self.singleton).activity_schedule
            calls = []

            def fail_second_recipient(records, *args, **kwargs):
                calls.append(kwargs['user_id'])
                if len(calls) == 2:
                    raise RuntimeError('1159 second recipient delivery failed')
                activity = original_schedule(records, *args, **kwargs)
                records.env.flush_all()
                created_activity_ids.extend(activity.ids)
                created_mail_ids.extend(set(Mail.search(mail_domain).ids) - before_mail_ids)
                return activity

            with patch.object(type(self.singleton), 'activity_schedule', autospec=True,
                              side_effect=fail_second_recipient):
                runner._check_unknown_results()
            self.assertEqual(calls, recipients[:2].ids, '必須先投影首位，再於第二位失敗')
            self.assertTrue(created_activity_ids, '故障前確實建立過 activity')
            self.assertTrue(created_mail_ids, '故障前確實排入過 mail queue，不是純 mock')
            self.assertFalse(self._activities(self.Monitor.SUMMARY_GAVE_UP))
            self.assertEqual(set(Mail.search(mail_domain).ids), before_mail_ids)
            self.assertFalse(Mail.browse(created_mail_ids).exists())
            self.assertNotIn(attempt, self.singleton.notified_unknown_attempt_ids)

            runner._check_unknown_results()
            self.assertEqual(set(self._activities(self.Monitor.SUMMARY_GAVE_UP).mapped('user_id')),
                             set(recipients), '回捲後重試仍須交付完整名單')
            self.assertIn(attempt, self.singleton.notified_unknown_attempt_ids)
            self.assertTrue(set(Mail.search(mail_domain).ids) - before_mail_ids)
            send.assert_not_called()
            send_after_commit.assert_not_called()

    def test_unknown_database_failure_reaches_the_monitor_job_boundary(self):
        attempt = self._unknown_attempt(
            self._service('odoo', state='initializing'), 1, fields.Datetime.now())
        original_schedule = type(self.singleton).activity_schedule
        for entry in ('_check_unknown_results', '_run_poll_cron_checks'):
            with self.subTest(entry=entry):
                failures = []

                def fail_unknown_delivery(records, *args, **kwargs):
                    if kwargs.get('summary') != self.Monitor.SUMMARY_GAVE_UP:
                        return original_schedule(records, *args, **kwargs)
                    failures.append(True)
                    # 真 PG 40001；由產品 savepoint 復原 cursor，不靠 assertRaises 代為回捲。
                    records.env.cr.execute("""
                        DO $$ BEGIN
                            RAISE EXCEPTION '1159 monitor database conflict'
                                USING ERRCODE = '40001';
                        END $$
                    """)

                caught = None
                with patch.object(type(self.singleton), 'activity_schedule', autospec=True,
                                  side_effect=fail_unknown_delivery):
                    try:
                        getattr(self.Monitor, entry)()
                    except OperationalError as error:
                        caught = error
                self.assertEqual(len(failures), 1, '必須到達 unknown event 的 DB 故障注入點')
                self.assertIsNotNone(caught, 'DB 故障不得被內層或 cron monitor 邊界吞掉')
                self.assertEqual(caught.pgcode, '40001')
                self.assertNotIn(attempt, self.singleton.notified_unknown_attempt_ids)
                self.assertFalse(self._activities(self.Monitor.SUMMARY_GAVE_UP))
                self.env.cr.execute('SELECT 1')
                self.assertEqual(self.env.cr.fetchone(), (1,), 'savepoint 必須解除 SQL aborted 狀態')

    def test_readiness_give_ups_are_reported(self):
        summary = self.Monitor.SUMMARY_GAVE_UP
        svc = self._service('odoo', state='initializing')
        svc.sudo().write({'init_wait_since': fields.Datetime.now() - timedelta(minutes=31)})
        self._poll(svc, pods=_NOT_READY_PODS)
        self.assertEqual(svc.init_exit, 'wait_timeout')
        self.assertIn(svc.name, self._activities(summary)[:1].note or '')
        other = self._service('odoo', state='initializing')
        # 歷史／未來正式訊號的事件介面仍可報告；本版不再由 Pod 累計 restarts 產生它。
        other._init_to_error('fast_fail', '已記錄的正式 fast-fail 事件')
        self.assertEqual(other.init_exit, 'fast_fail')
        self.assertIn(other.name, self._activities(summary)[:1].note or '',
                      '同一張未處理的 activity 追加一行，不另開')


@tagged('post_install', '-at_install')
class TestN8nOwnerMonitor1159(_ServiceCase):
    """#1159 Q28b（owner 裁決 2026-10-01）：n8n 首次觀測 ready 後 5 分內，平台未確認 owner 就告警。

    確認＝本代、本 episode、期限內 ``success`` 且本 attempt 的 key 已落庫；
    login／create_api_key 的 failed／unknown 不算確認（那正是 owner 被他人先註冊的症狀）。
    掃描在短 poll 的 monitor checks，等待／失敗／冷補套／停權都涵蓋，不再只看 live 成功事件。
    """

    def setUp(self):
        super().setUp()
        self.Monitor = self.env[MONITOR]
        self.singleton = self.Monitor._singleton()
        self.assertEqual(self.singleton.id, self.scope.monitor_id, '每個 case 有自有 monitor target')
        for xmlid in (INIT_CRON, POLL_CRON):
            self.env.ref(xmlid).sudo().write({'active': True, 'lastcall': fields.Datetime.now()})
        self.now = fields.Datetime.now().replace(microsecond=0)
        self.sequence = 0

    def _ready_n8n(self, minutes, state='initializing', **vals):
        """本 episode 首次觀測 ready 在 ``minutes`` 分鐘前的 n8n 服務。"""
        svc = self._service('n8n', state='initializing')
        svc.sudo().write({'init_ready_observed': True,
                          'init_ready_since': self.now - timedelta(minutes=minutes)})
        if state != 'initializing' or vals:
            svc.sudo().write(dict(vals, state=state))
        return svc

    def _attempt(self, svc, minutes_after_ready, outcome='success', key=True, generation=None,
                 step=False, kind=False):
        self.sequence += 1
        ready = svc.init_ready_since
        return self.env[ATTEMPT].sudo().create({
            'service_id': svc.id,
            'generation': svc.init_generation if generation is None else generation,
            'sequence': self.sequence,
            'init_type': 'n8n',
            'claimed_at': ready,
            'lease_until': ready + timedelta(minutes=15),
            'finished_at': ready + timedelta(minutes=minutes_after_ready),
            'applied_at': ready + timedelta(minutes=minutes_after_ready),
            'outcome': outcome,
            'n8n_key_persisted': key,
            'error_step': step,
            'error_kind': kind,
        })

    def _scan(self, at=None):
        with patch.object(type(self.env[SVC]), '_init_db_now', return_value=at or self.now):
            self.Monitor._run_poll_cron_checks()

    def _owner_activities(self):
        return self.env['mail.activity'].sudo().search([
            ('res_model', '=', MONITOR), ('res_id', '=', self.singleton.id),
            ('summary', '=', self.Monitor.SUMMARY_N8N_OWNER)])

    def _notes(self):
        return ' '.join(str(activity.note or '') for activity in self._owner_activities())

    def test_unconfirmed_owner_is_reported_while_waiting_failing_or_suspended(self):
        """還在等待、失敗、額度出口、wait_timeout、停權都要告警。"""
        queued = self._ready_n8n(6)
        login_failed = self._ready_n8n(6)
        self._attempt(login_failed, 1, outcome='failed', key=False, step='login', kind='failed')
        key_unknown = self._ready_n8n(6)
        self._attempt(key_unknown, 1, outcome='unknown', key=False, step='create_api_key',
                      kind='unknown')
        not_executed = self._ready_n8n(6)
        self._attempt(not_executed, 1, outcome='not_executed', key=False, step='wait_ready',
                      kind='not_executed')
        partial = self._ready_n8n(6)
        self._attempt(partial, 1, outcome='partial', key=True, step='patch_secret', kind='partial')
        exhausted = self._ready_n8n(6, state='error', init_exit='failed')
        self._attempt(exhausted, 2, outcome='failed', key=False, step='login', kind='failed')
        timed_out = self._ready_n8n(6, state='error', init_exit='wait_timeout')
        suspended = self._ready_n8n(6, state='suspended')
        reported = (queued, login_failed, key_unknown, not_executed, partial, exhausted,
                    timed_out, suspended)
        deleting = self._ready_n8n(6, state='deleting')
        odoo = self._service('odoo', state='initializing')
        odoo.sudo().write({'init_ready_observed': True,
                           'init_ready_since': self.now - timedelta(minutes=6)})
        before = {svc.id: svc.read(['state', 'init_pending', 'init_retries', 'init_unknown_count',
                                    'init_exit', 'init_wait_spent_seconds'])[0]
                  for svc in reported}
        self._scan()
        activities = self._owner_activities()
        self.assertEqual(set(activities.mapped('user_id')),
                         set(self.env.ref('base.group_system').users.filtered(
                             lambda user: user.active and not user.share)))
        notes = self._notes()
        for svc in reported:
            with self.subTest(service=svc.name, state=svc.state):
                self.assertIn(svc.name, notes, '5 分內沒有可信確認證據就必須告警')
                self.assertEqual(svc.read(list(before[svc.id].keys() - {'id'}))[0], before[svc.id],
                                 '監控不得改 state／額度／等待預算')
        self.assertNotIn(deleting.name, notes, 'deleting 不在掃描範圍')
        self.assertNotIn(odoo.name, notes, '只監控 n8n')

    def test_success_with_stored_key_within_the_window_confirms_the_owner(self):
        """只有 success＋本 attempt key 已落庫才算確認。"""
        confirmed = self._ready_n8n(6, state='running')
        self._attempt(confirmed, 1)
        keyless = self._ready_n8n(6)
        self._attempt(keyless, 1, key=False)
        self._scan()
        notes = self._notes()
        self.assertNotIn(confirmed.name, notes)
        self.assertIn(keyless.name, notes, 'success 但 key 未落庫不是可信確認')

    def test_late_confirmation_is_reported_without_claiming_an_owner_creation_time(self):
        """第 7 分才確認仍是 5 分內未確認；文案不得斷言 owner 建立時間或不存在。"""
        late = self._ready_n8n(7, state='running')
        self._attempt(late, 7)
        self._scan()
        notes = self._notes()
        self.assertIn(late.name, notes)
        self.assertIn('The platform has not yet confirmed that the n8n owner exists within 5 minutes '
                      'after it first observed the service ready.', notes)
        self.assertIn('owner not yet confirmed by', notes)
        forbidden = re.compile(r"\b(created|creation|late|later|delayed|does not exist|doesn't exist|"
                               r"not exist|no owner|owner is missing)\b", re.IGNORECASE)
        self.assertFalse(forbidden.findall(notes), '只報「平台未確認」，不推 owner 建立時間或不存在')

    def test_window_boundary_is_strict_and_independent_of_scan_timing(self):
        """邊界：剛好 5:00 不告警；5:00 確認算數；過 5:00 後才掃描仍按期限判斷。"""
        at_boundary = self._ready_n8n(5)
        confirmed_at_deadline = self._ready_n8n(6, state='running')
        self._attempt(confirmed_at_deadline, 5)
        self._scan()
        notes = self._notes()
        self.assertNotIn(at_boundary.name, notes, '剛好 5 分鐘尚未逾時')
        self.assertNotIn(confirmed_at_deadline.name, notes, '期限當下收到的確認算數')
        self._scan(at=self.now + timedelta(seconds=1))
        self.assertIn(at_boundary.name, self._notes())

    def test_other_generation_or_previous_episode_success_does_not_confirm(self):
        """舊代、舊 episode 的 success 不能確認本代本 episode。"""
        old_generation = self._ready_n8n(6)
        old_generation.sudo().write({'init_generation': 2})
        self._attempt(old_generation, 1, generation=1)
        previous_episode = self._ready_n8n(6)
        self._attempt(previous_episode, -2)
        self._scan()
        notes = self._notes()
        self.assertIn(old_generation.name, notes)
        self.assertIn(previous_episode.name, notes)

    def test_each_episode_reports_one_line_and_a_new_episode_can_report_again(self):
        """同 episode 只一行；重新進 initializing 清 marker，可再告警。"""
        svc = self._ready_n8n(6)
        self._scan()
        self.assertEqual(self._notes().count(svc.name), 1)
        self.assertEqual(svc.n8n_owner_alerted_at, self.now, 'marker 記平台告警時間')
        self._scan(at=self.now + timedelta(minutes=1))
        self.assertEqual(self._notes().count(svc.name), 1, '重複 tick 不重複 append')
        self._attempt(svc, 1)
        svc.sudo().write({'state': 'running'})
        svc.sudo().write({'state': 'initializing'})
        self.assertFalse(svc.init_ready_since, '新 episode 的 ready 錨點重來')
        self.assertFalse(svc.n8n_owner_alerted_at, '新 episode 的告警 marker 一起清掉')
        svc.sudo().write({'init_ready_observed': True,
                          'init_ready_since': self.now + timedelta(minutes=1)})
        self._scan(at=self.now + timedelta(minutes=7))
        self.assertEqual(self._notes().count(svc.name), 2,
                         '舊 episode 的 success 不確認新 episode，新 episode 再告警一行')

    def test_database_failure_reaches_the_monitor_job_boundary(self):
        """真 PG 40001 交回 job 邊界；marker 與 activity 一起回捲，cursor 可用。"""
        svc = self._ready_n8n(6)
        original_schedule = type(self.singleton).activity_schedule
        failures = []

        def fail_owner_delivery(records, *args, **kwargs):
            if kwargs.get('summary') != self.Monitor.SUMMARY_N8N_OWNER:
                return original_schedule(records, *args, **kwargs)
            failures.append(True)
            records.env.cr.execute("""
                DO $$ BEGIN
                    RAISE EXCEPTION '1159 n8n owner monitor conflict' USING ERRCODE = '40001';
                END $$
            """)

        caught = None
        with patch.object(type(self.singleton), 'activity_schedule', autospec=True,
                          side_effect=fail_owner_delivery):
            try:
                self._scan()
            except OperationalError as error:
                caught = error
        self.assertEqual(len(failures), 1, '必須到達 owner 告警的 DB 故障注入點')
        self.assertIsNotNone(caught, 'DB 故障不得被 owner check 或 cron monitor 邊界吞掉')
        self.assertEqual(caught.pgcode, '40001')
        self.env.cr.execute('SELECT 1')
        self.assertEqual(self.env.cr.fetchone(), (1,), 'savepoint 必須解除 SQL aborted 狀態')
        svc.invalidate_recordset()
        self.assertFalse(svc.n8n_owner_alerted_at, 'marker 與告警同一 savepoint 回捲，可重試')
        self.assertFalse(self._owner_activities())
        self._scan()
        self.assertIn(svc.name, self._notes(), '故障後下一趟可重新交付')

    def test_alert_marker_is_platform_managed_and_not_injectable_at_create(self):
        """marker 是平台管控欄位；租戶寫入被擋、create 的 raw／context／ir.default 被中和。"""
        user = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': '1159 owner monitor workspace admin', 'login': f'1159-owner-mon-{self.tag}',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': 'admin'})
        svc = self._ready_n8n(6)
        managed = svc.with_user(user).with_context(lang='en_US')
        managed.write({'name': '合法 owner 監控一般欄位更新'})
        stamp = fields.Datetime.now()
        try:
            managed.write({'n8n_owner_alerted_at': stamp})
        except AccessError as exc:
            self.assertIn('managed by the platform', str(exc))
        else:
            self.fail('workspace admin 不得偽造 owner 告警 marker 來壓掉告警')
        future = fields.Datetime.to_string(stamp + timedelta(days=3))
        creator = _internal_provision(self.env[SVC].with_user(user).sudo())
        for carrier in ('values', 'context', 'ir.default'):
            with self.subTest(carrier=carrier):
                model = creator
                vals = {'name': f'owner marker {carrier}', 'workspace_id': self.workspace.id,
                        'template_id': self.templates['n8n'].id, 'state': 'initializing'}
                if carrier == 'values':
                    vals['n8n_owner_alerted_at'] = future
                elif carrier == 'context':
                    model = model.with_context(default_n8n_owner_alerted_at=future)
                else:
                    self.env['ir.default'].with_user(user).set(
                        SVC, 'n8n_owner_alerted_at', future, user_id=user.id)
                created = model.create(vals)
                self.assertFalse(created.n8n_owner_alerted_at, '新服務不得帶入告警 marker')
        svc.sudo().write({'n8n_owner_alerted_at': stamp})
        self.assertEqual(svc.n8n_owner_alerted_at, stamp, '平台寫入仍成功')

    def test_confirmation_at_the_ready_instant_and_among_failed_attempts(self):
        """邊界：finished_at 等於首次 ready 也算；同服務先 failed 後 success 仍以 success 確認。"""
        instant = self._ready_n8n(6, state='running')
        self._attempt(instant, 0)
        mixed = self._ready_n8n(6, state='running')
        self._attempt(mixed, 1, outcome='failed', key=False, step='create_api_key', kind='failed')
        self._attempt(mixed, 3)
        self._scan()
        notes = self._notes()
        self.assertNotIn(instant.name, notes)
        self.assertNotIn(mixed.name, notes)

    def test_new_generation_outside_initializing_does_not_reuse_the_old_episode_anchor(self):
        """開新一代就清 ready 錨點與告警 marker，不能沿用前一代已確認 episode 的舊時間。

        停權中 redeploy（state 維持 suspended）與開代後在部署階段失敗（從未回到
        initializing）都不能拿舊錨點對新一代誤報。
        """
        for name, before_state, after_state in (('suspended-redeploy', 'suspended', 'suspended'),
                                                ('deploy-failed', 'upgrading', 'error')):
            with self.subTest(case=name):
                svc = self._ready_n8n(6, state='running')
                self._attempt(svc, 1)
                self._scan()
                self.assertNotIn(svc.name, self._notes(), '前提：前一代 episode 已確認')
                svc.sudo().write({'state': before_state})
                generation = svc.init_generation
                svc._open_init_generation()
                self.assertEqual(svc.init_generation, generation + 1)
                svc.sudo().write({'state': after_state})
                self._scan(at=self.now + timedelta(minutes=10))
                self.assertNotIn(svc.name, self._notes(), '新一代不能用舊錨點誤報')
                self.assertFalse(svc.init_ready_since, '開代即清 ready 錨點')
                self.assertFalse(svc.n8n_owner_alerted_at)

    def test_new_generation_while_initializing_keeps_dispatch_and_restarts_the_anchor(self):
        """在 initializing 中開代：仍可 claim（ready 旗標不清）；錨點與 marker 重來，新一代可再告警。"""
        svc = self._ready_n8n(6)
        self._scan()
        self.assertEqual(self._notes().count(svc.name), 1, '前提：前一代已告警')
        svc._open_init_generation()
        self.assertTrue(svc.init_ready_observed, 'initializing 中開代仍留在 init 佇列')
        self.assertFalse(svc.init_ready_since)
        self.assertFalse(svc.n8n_owner_alerted_at, '新一代不沿用前一代的告警 marker')
        self._scan(at=self.now + timedelta(minutes=10))
        self.assertEqual(self._notes().count(svc.name), 1, '錨點空著時不用前一代時間告警')
        self._poll(svc)
        anchor = svc.init_ready_since
        self.assertTrue(anchor, '下一次短 poll 觀測寫回新一代的錨點')
        self._scan(at=anchor + timedelta(minutes=6))
        self.assertEqual(self._notes().count(svc.name), 2, '新一代 5 分內未確認，再告警一行')

    def test_undeliverable_alert_keeps_the_marker_unset_for_retry(self):
        """沒有可收件的 system user 時不寫 marker；之後有收件者，下一趟補告警。"""
        svc = self._ready_n8n(6)
        recipients = self.env.ref('base.group_system').users.filtered(
            lambda user: user.active and not user.share)
        self.assertTrue(recipients)
        recipients.write({'active': False})
        self._scan()
        self.assertFalse(self._owner_activities(), '前提：真的沒有收件者')
        self.assertFalse(svc.n8n_owner_alerted_at, '告警沒送出就不能把本 episode 記成已告警')
        recipients.write({'active': True})
        self._scan(at=self.now + timedelta(minutes=1))
        self.assertIn(svc.name, self._notes())
        self.assertEqual(svc.n8n_owner_alerted_at, self.now + timedelta(minutes=1))
