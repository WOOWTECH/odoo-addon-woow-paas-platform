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
5. 等待時限（#1257 已做）＋ fast-fail 短路（D1）＋容量排隊不計時（D2）
   → ``TestShortPollObservation1159``。

🔴 會走到 init 入口或 cron 迴圈（兩者都會 ``commit``）的測試用真 cursor＋真 commit＋
事後清理（樣板 ``test_billing_incident_864.py``、``test_init_execution_control_1257.py``）。
"""
import sys
import threading
import traceback
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID, api, fields
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import PaaSOperatorError
from .operator_contract import operator_client, status_response

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
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': f'1159 WS {self.tag}'})
        self.templates = {
            init_type: self.env['woow_paas_platform.cloud_app_template'].create(
                _template_vals(self.tag, init_type))
            for init_type in ('odoo', 'n8n', 'none')
        }

    def _service(self, init_type='odoo', **vals):
        service = _make_service(self.env, self.workspace, self.templates[init_type],
                                f'{init_type}-{uuid.uuid4().hex[:6]}', **vals)
        if vals.get('state') == 'initializing':
            service.sudo().write({'init_pending': True})
        return service

    def _poll(self, service, pods=None, **status):
        client = operator_client(get_status=status_response(
            pods=_READY_PODS if pods is None else pods, **status))
        service._poll_and_update_status(client)
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

    def test_crash_looping_pod_short_circuits_the_wait(self):
        svc = self._service('odoo', state='initializing')
        self._poll(svc, pods=self._crashing_pods(3))
        self.assertEqual((svc.state, svc.init_exit, svc.init_retries, svc.init_unknown_count),
                         ('error', 'fast_fail', 0, 0), '短路不耗額度')
        self.assertIn('app-0', svc.error_message)
        self.assertTrue(svc.init_pending, '仍欠 init')

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
        """Q30 裁定 i／D1：與 wait_timeout 同類——pods 起不來，reconfigure 照擋。"""
        svc = self._service('odoo', state='initializing')
        self._poll(svc, pods=self._crashing_pods(3))
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
            svc.sudo().write({'observed_at': observed})
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
        self.addCleanup(self._purge)
        with self.registry.cursor() as cr:
            env = self._env(cr)
            self.workspace_id = env['woow_paas_platform.workspace'].create({
                'name': f'1159 Workspace {self.tag}'}).id
            self.template_ids = {
                init_type: env['woow_paas_platform.cloud_app_template'].create(
                    _template_vals(self.tag, init_type)).id
                for init_type in ('odoo', 'n8n', 'none')
            }

    def _purge(self):
        with self.registry.cursor() as cr:
            ids = self.service_ids
            cr.execute('DELETE FROM woow_paas_platform_cloud_service_init_attempt '
                       'WHERE service_id = ANY(%s)', (ids,))
            cr.execute('DELETE FROM woow_paas_platform_cloud_service_secret '
                       'WHERE service_id = ANY(%s)', (ids,))
            cr.execute('DELETE FROM woow_paas_platform_cloud_service WHERE id = ANY(%s)', (ids,))
            cr.execute('DELETE FROM woow_paas_platform_cloud_app_template WHERE id = ANY(%s)',
                       (list(self.template_ids.values()),))
            cr.execute('DELETE FROM woow_paas_platform_workspace_access WHERE workspace_id = %s',
                       (self.workspace_id,))
            for table, column in (('mail_followers', 'res_model'), ('mail_message', 'model')):
                cr.execute(f'DELETE FROM {table} WHERE {column} = %s AND res_id = %s',
                           ('woow_paas_platform.workspace', self.workspace_id))
            cr.execute('DELETE FROM woow_paas_platform_workspace WHERE id = %s',
                       (self.workspace_id,))
            # 兩個 cron 結尾都跑監控（Q38）：真 cursor 下 activity／通知訊息會落地。
            for table, column in (('mail_activity', 'res_model'), ('mail_message', 'model'),
                                  ('mail_followers', 'res_model')):
                cr.execute(f"DELETE FROM {table} WHERE {column} = "
                           "'woow_paas_platform.platform_monitor'")
            cr.execute("DELETE FROM ir_cron_trigger WHERE cron_id IN "
                       "(SELECT res_id FROM ir_model_data WHERE module = 'woow_paas_platform' "
                       "AND name IN ('ir_cron_run_post_deploy_inits', "
                       "'ir_cron_check_deploying_services'))")
            cr.commit()

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
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=client):
            with self.registry.cursor() as cr:
                env = self._env(cr)
                getattr(env[SVC].with_context(**(context or {})), method)()

    def _triggers(self, xmlid=INIT_CRON):
        with self.registry.cursor() as cr:
            cron = self._env(cr).ref(xmlid)
            cr.execute('SELECT count(*) FROM ir_cron_trigger WHERE cron_id = %s', (cron.id,))
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
        client.init_odoo.side_effect = [
            {'success': False, 'error_kind': 'not_executed'},
            {'success': True},
        ]
        client.init_odoo.side_effect = lambda **kwargs: (
            calls.append(kwargs['release_name']) or (
                {'success': False, 'error_kind': 'not_executed'} if len(calls) == 1
                else {'success': True}))
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
        service_id = self._add('odoo')  # no Ready observation: init cron cannot claim it
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

    def test_no_backlog_no_trigger(self):
        self._add('odoo', ready_minutes_ago=1)
        client, _calls = self._recording_client()
        triggers = self._triggers()
        self._run_cron('_cron_run_post_deploy_inits', client)
        self.assertEqual(self._triggers(), triggers)

    def test_slow_n8n_owner_creation_is_reported(self):
        """Q28b：n8n 從 ready（公開可達）到 owner 建立超過 5 分就告警（每台一行）。"""
        slow = self._add('n8n', ready_minutes_ago=6)
        fast = self._add('n8n', ready_minutes_ago=1)
        client, _calls = self._recording_client()
        self._run_cron('_cron_run_post_deploy_inits', client)
        with self.registry.cursor() as cr:
            env = self._env(cr)
            monitor = env['woow_paas_platform.platform_monitor']
            notes = ' '.join(env['mail.activity'].search([
                ('res_model', '=', 'woow_paas_platform.platform_monitor'),
                ('summary', '=', monitor.SUMMARY_N8N_OWNER)]).mapped(
                    lambda a: str(a.note or '')))
            slow_name = env[SVC].browse(slow).name
            fast_name = env[SVC].browse(fast).name
        self.assertIn(slow_name, notes)
        self.assertNotIn(fast_name, notes)

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
                    self._env(cr)[SVC].browse(slow)._run_post_deploy_init(init_client)
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
                svc._poll_and_update_status(poll_client)
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
        self.env['mail.activity'].search([
            ('res_model', '=', MONITOR)]).unlink()  # 本交易內，結束時回捲
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

    def test_singleton_is_the_data_record(self):
        self.assertEqual(self.singleton, self.env.ref('woow_paas_platform.platform_monitor_singleton'))
        self.assertEqual(self.Monitor._singleton(), self.singleton, '查找以 xmlid 為準，不重建')

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
        with patch.object(model, '_run_poll_cron_checks', autospec=True) as poll_side, \
                patch.object(model, '_run_init_cron_checks', autospec=True) as init_side, \
                patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=None):
            self.env[SVC]._cron_check_deploying_services()
            poll_side.assert_called_once()
            init_side.assert_not_called()
            poll_side.reset_mock()
            self.env[SVC]._cron_run_post_deploy_inits()
            init_side.assert_called_once()
            poll_side.assert_not_called()

    def test_oldest_observation(self):
        summary = self.Monitor.SUMMARY_OBSERVED
        svc = self._service('none', state='deploying')
        svc.sudo().write({'observed_at': fields.Datetime.now() - timedelta(minutes=10)})
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
        self.env[ATTEMPT].sudo().create({
            'service_id': svc.id, 'generation': 1, 'sequence': 1, 'init_type': 'odoo',
            'claimed_at': fields.Datetime.now() - timedelta(hours=1),
            'lease_until': fields.Datetime.now(),
            'finished_at': fields.Datetime.now() - timedelta(minutes=30),
            'outcome': 'unknown'})
        self.Monitor._check_unknown_results()
        self.assertTrue(self._activities(summary))

    def test_readiness_give_ups_are_reported(self):
        summary = self.Monitor.SUMMARY_GAVE_UP
        svc = self._service('odoo', state='initializing')
        svc.sudo().write({'init_wait_since': fields.Datetime.now() - timedelta(minutes=31)})
        self._poll(svc, pods=_NOT_READY_PODS)
        self.assertEqual(svc.init_exit, 'wait_timeout')
        self.assertIn(svc.name, self._activities(summary)[:1].note or '')
        other = self._service('odoo', state='initializing')
        self._poll(other, pods=[{'name': 'x-0', 'phase': 'Running', 'ready': '0/1', 'restarts': 4}])
        self.assertEqual(other.init_exit, 'fast_fail')
        self.assertIn(other.name, self._activities(summary)[:1].note or '',
                      '同一張未處理的 activity 追加一行，不另開')
