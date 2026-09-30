# -*- coding: utf-8 -*-
"""#1257：post-deploy init 的執行管控（欠 init、attempt、lease、CAS、失敗分類）。

驗收對照（#1257 resolution c15293「本票實作範圍與驗收」）：

1. 兩條真連線並行 claim，只有一條拿到 lease、operator 只收到一次 init
   → ``TestInitLeaseRealCursor1257``。
2. claim 在呼叫 operator 前已 commit（另一條連線看得到 lease）→ 同上。
3. lease 到期 → unknown；第 2 次 unknown 轉 error；合成 legacy 不計入 → 同上。
4. 分類表逐類 → ``TestInitOutcomeClassification1257``。
5. #1266／#1267 重現 → ``TestSuspendResumeOwedInit1266``、``TestReconfigureRollbackOwedInit1267``。
6. lease 期間 redeploy／reconfigure／rollback 被拒；redeploy 登記意圖、lease 結束後
   背景先執行 → ``TestInitLeaseGuards1257``、``TestInitLeaseRealCursor1257``。
7. migration 回填 → ``TestInitMigration1257``。

🔴 ``_run_post_deploy_init`` 會 ``commit`` 呼叫端的 cursor（Q2：claim 必須在呼叫
operator 之前 commit）。TransactionCase 的測試 cursor 禁止 commit，所以凡是會走到
入口的測試都用真 cursor＋真 commit＋事後清理（樣板 ``test_billing_incident_864.py``；
依據：core 只有 HttpCase 進 test mode，``TransactionCase`` 下的 ``registry.cursor()``
是真的另一條連線）。
"""
import json
import logging
import sys
import threading
import traceback
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError, ProtocolError

from odoo import SUPERUSER_ID, api, fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from ..controllers.paas import PaasController
from ..models.cloud_service import (
    INIT_FAILED_LIMIT,
    _INIT_UNKNOWN_LIMIT_PARAM,
    _INIT_WAIT_TIMEOUT_PARAM,
    _internal_provision,
)
from ..models.cloud_service_init_attempt import (
    INIT_LEASE_SECONDS,
    classify_init_result,
    mask_init_error,
)
from ..services.paas_operator import (
    PaaSOperatorAPIError,
    PaaSOperatorClient,
    PaaSOperatorError,
    PaaSOperatorTimeoutError,
)
from .operator_contract import operator_client, status_response

SVC = 'woow_paas_platform.cloud_service'
ATTEMPT = 'woow_paas_platform.cloud_service_init_attempt'
_OPERATOR_MODULE = 'odoo.addons.woow_paas_platform.services.paas_operator'
_CONTROLLER_MODULE = 'odoo.addons.woow_paas_platform.controllers.paas'
_READY_PODS = [{'name': 'app', 'phase': 'Running', 'ready': '1/1'}]
_JWT = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LTEyNTcifQ.c2lnbmF0dXJlLTEyNTc'
# lease 的上界：承載 init 的 runtime 的 cron 硬上限（prod `limit_time_real_cron=1800`，
# woow-paas/paas-app-bundle-cd#154）。runtime 實值由部署設定決定，這裡只對帳常數。
_CRON_HARD_LIMIT_SECONDS = 1800


def _template_vals(tag, init_type):
    return {
        'name': f'1257 {init_type} {tag}',
        'slug': f'init-1257-{init_type}-{tag}',
        'helm_repo_url': 'https://charts.example.com',
        'helm_chart_name': 'chart',
        'helm_chart_version': '1.0.0',
        'workload_type': 'container',
        'post_deploy_init_type': init_type,
        'mcp_enabled': False,
    }


def _make_service(env, workspace, template, tag, **vals):
    return _internal_provision(env[SVC]).create(dict({
        'name': f'1257 Service {tag}',
        'workspace_id': workspace.id,
        'template_id': template.id,
        'state': 'deploying',
        'helm_namespace': f'dev-sandbox-1257-{tag}',
        'helm_release_name': f'svc-{tag}',
    }, **vals))


def _thread_stacks(thread):
    """等待逾時時附上 worker thread 當下的 stack（卡在哪一行才看得出來）。"""
    frame = sys._current_frames().get(thread.ident)
    return ''.join(traceback.format_stack(frame)) if frame else '(thread already finished)'


class _NoThread:
    """``_update_service`` 的背景 helm upgrade 不在本檔範圍：不啟動。"""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        pass


# ====================================================================== #
#  驗收 4：失敗分類                                                          #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestInitOutcomeClassification1257(TransactionCase):
    """分類依據是「遠端副作用是否可能已發生」，不看 ``success`` 布林（Q3／Q11）。"""

    def _via_client(self, side_effect):
        """讓真 client 的 ``_request`` 包裝 requests 例外，再交給分類。"""
        client = PaaSOperatorClient('http://operator.invalid', 'key')
        with patch.object(client._session, 'request', side_effect=side_effect):
            try:
                result = client.init_odoo(namespace='ns', release_name='rel')
            except Exception as exc:  # 分類的輸入就是 client 丟出來的例外
                return classify_init_result('odoo', exc=exc)
        return classify_init_result('odoo', result=result)

    def _response(self, status_code, payload):
        response = requests.Response()
        response.status_code = status_code
        response._content = json.dumps(payload).encode()
        return response

    def test_operator_readiness_steps_are_not_executed(self):
        for init_type in ('odoo', 'n8n'):
            for step in ('find_pod', 'wait_ready'):
                with self.subTest(init_type=init_type, step=step):
                    outcome = classify_init_result(init_type, result={
                        'success': False, 'error': f'[{step}] x',
                        'error_step': step, 'error_kind': 'not_executed'})
                    self.assertEqual(outcome.outcome, 'not_executed')
                    self.assertEqual(outcome.error_step, step)

    def test_script_reported_failure_is_failed(self):
        outcome = classify_init_result('odoo', result={
            'success': False, 'error': '[update_module_list] boom',
            'error_step': 'update_module_list', 'error_kind': 'failed'})
        self.assertEqual(outcome.outcome, 'failed')

    def test_n8n_failure_carrying_api_key_is_partial_and_keeps_the_key(self):
        outcome = classify_init_result('n8n', result={
            'success': False, 'api_key': _JWT, 'error_step': 'patch_secret',
            'error_kind': 'partial'})
        self.assertEqual(outcome.outcome, 'partial')
        self.assertEqual(outcome.api_key, _JWT)
        # 舊 operator 沒帶 error_kind 也一樣：帶 key 的失敗＝副作用已發生。
        legacy = classify_init_result('n8n', result={'success': False, 'api_key': _JWT})
        self.assertEqual(legacy.outcome, 'partial')

    def test_operator_side_unknowns_stay_unknown(self):
        """exec timeout／空輸出等由 operator 標成 unknown 的回應。"""
        for step in ('update_module_list', 'node_request', 'setup_owner'):
            with self.subTest(step=step):
                outcome = classify_init_result('odoo', result={
                    'success': False, 'error_step': step, 'error_kind': 'unknown'})
                self.assertEqual(outcome.outcome, 'unknown')

    def test_old_operator_without_error_kind_is_unknown(self):
        outcome = classify_init_result('odoo', result={
            'success': False, 'error': '[find_pod] not found'})
        self.assertEqual(outcome.outcome, 'unknown',
                         '缺 error_kind 的舊回應必須保守歸 unknown（Q11），'
                         '即使字串看起來像 find_pod')

    def test_success_paths(self):
        self.assertEqual(classify_init_result('odoo', result={'success': True}).outcome, 'success')
        ok = classify_init_result('n8n', result={'success': True, 'api_key': _JWT})
        self.assertEqual((ok.outcome, ok.api_key), ('success', _JWT))
        self.assertEqual(
            classify_init_result('n8n', result={'success': True}).outcome, 'unknown',
            'n8n 回 success 卻沒有 key：無法確認，不能當成功')

    def test_http_500_is_unknown(self):
        structured = self._via_client([self._response(500, {
            'detail': {'message': 'boom', 'error_step': None, 'error_kind': 'unknown'},
            'error_type': 'HTTPException'})])
        self.assertEqual(structured.outcome, 'unknown')
        plain = classify_init_result('odoo', exc=PaaSOperatorAPIError(
            'API error', status_code=500, detail='Internal server error'))
        self.assertEqual(plain.outcome, 'unknown')

    def test_read_timeout_is_unknown(self):
        self.assertEqual(
            self._via_client(requests.exceptions.ReadTimeout('read timed out')).outcome,
            'unknown')
        self.assertEqual(
            classify_init_result('odoo', exc=PaaSOperatorTimeoutError('t')).outcome,
            'unknown')

    def test_only_connect_phase_failures_are_not_executed(self):
        refused = requests.exceptions.ConnectionError(MaxRetryError(
            None, '/api/releases/ns/rel/init/odoo', reason=NewConnectionError(None, 'refused')))
        self.assertEqual(self._via_client(refused).outcome, 'not_executed')
        self.assertEqual(
            self._via_client(requests.exceptions.ConnectTimeout('connect timed out')).outcome,
            'not_executed')
        aborted = requests.exceptions.ConnectionError(
            ProtocolError('Connection aborted.', ConnectionResetError()))
        self.assertEqual(
            self._via_client(aborted).outcome, 'unknown',
            '連線中途被切時 request 可能已送達 ⇒ 不能當成沒執行')

    def test_invalid_json_and_unexpected_errors_are_unknown(self):
        broken = requests.Response()
        broken.status_code = 200
        broken._content = b'not json'
        self.assertEqual(self._via_client([broken]).outcome, 'unknown')
        self.assertEqual(classify_init_result('odoo', exc=ValueError('x')).outcome, 'unknown')
        self.assertEqual(classify_init_result('odoo', result=None).outcome, 'unknown')

    def test_error_text_is_masked_and_truncated(self):
        masked = mask_init_error(
            f'key {_JWT} password=hunter2 owner-pass', secrets=('owner-pass',))
        self.assertNotIn(_JWT, masked)
        self.assertNotIn('hunter2', masked)
        self.assertNotIn('owner-pass', masked)
        self.assertLess(len(mask_init_error('x' * 5000)), 2100)
        for text, secret in (
                ('password: "two words"', 'two words'),
                ('Authorization: Bearer abc.def-123', 'abc.def-123'),
                ('url?password%3Dhunter2&x=1', 'hunter2'),
                ("api_key='k k'", 'k k')):
            with self.subTest(text=text):
                self.assertNotIn(secret, mask_init_error(text))

    def test_platform_inferred_outcomes_leave_the_wire_kind_empty(self):
        """``error_kind`` 只記 operator 在 wire 上回報的值。"""
        self.assertIsNone(
            classify_init_result('odoo', exc=PaaSOperatorTimeoutError('t')).error_kind)
        self.assertEqual(classify_init_result('odoo', result={
            'success': False, 'error_kind': 'failed'}).error_kind, 'failed')

    def test_lease_lengths_follow_the_client_budget(self):
        """Q32：Odoo 1200、n8n 900；下界是 client 預算，上界小於 cron 硬上限。"""
        self.assertEqual(INIT_LEASE_SECONDS, {'odoo': 1200, 'n8n': 900})
        for seconds in INIT_LEASE_SECONDS.values():
            self.assertLess(seconds, _CRON_HARD_LIMIT_SECONDS)


@tagged('post_install', '-at_install')
class TestInitConfigParametersRegisteredForUninstall(TransactionCase):
    """Q37 的兩個 key 走「模組常數＋get_param()」，repo-lint 掃不到 ⇒ 由這裡守。"""

    def test_init_params_are_in_config_parameters_list(self):
        from ..hooks import _CONFIG_PARAMETERS

        for key in (_INIT_UNKNOWN_LIMIT_PARAM, _INIT_WAIT_TIMEOUT_PARAM):
            self.assertIn(key, _CONFIG_PARAMETERS,
                          '%s 沒進 hooks.py 的 _CONFIG_PARAMETERS ⇒ 卸載後留孤兒紀錄' % key)


# ====================================================================== #
#  驗收 1／2／3／6（背景那半）：真 cursor                                   #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestInitLeaseRealCursor1257(TransactionCase):
    """claim／lease／CAS 必須跨連線成立，所以 fixture 真的 commit、事後清掉。"""

    def setUp(self):
        super().setUp()
        self.tag = uuid.uuid4().hex[:10]
        self.addCleanup(self._purge)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self.workspace_id = env['woow_paas_platform.workspace'].create({
                'name': f'1257 Workspace {self.tag}'}).id
            workspace = env['woow_paas_platform.workspace'].browse(self.workspace_id)
            self.template_ids = {}
            self.service_ids = {}
            for init_type in ('odoo', 'n8n'):
                template = env['woow_paas_platform.cloud_app_template'].create(
                    _template_vals(self.tag, init_type))
                self.template_ids[init_type] = template.id
                service = _make_service(
                    env, workspace, template, f'{init_type}-{self.tag}',
                    state='initializing')
                service.write({'init_wait_since': service._init_db_now()})
                if init_type == 'n8n':
                    service.write({'n8n_owner_email': f'owner-{self.tag}@example.invalid',
                                   'n8n_owner_password': f'pw-{self.tag}'})
                self.service_ids[init_type] = service.id

    def _purge(self):
        with self.registry.cursor() as cr:
            ids = list(self.service_ids.values())
            cr.execute(f'DELETE FROM woow_paas_platform_cloud_service_init_attempt '
                       f'WHERE service_id = ANY(%s)', (ids,))
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
            cr.commit()

    # -- helpers ---------------------------------------------------------- #

    def _env(self, cr):
        """真 cursor 的 superuser env；worker thread 也能用。

        ``-u --test-enable`` 時主執行緒在整段測試期間持有 ``Registry._lock``，而
        ``api.Environment`` 遇到還沒有 transaction 的 cursor 會去 ``Registry(dbname)``
        查表 ⇒ worker thread 卡死在那把鎖上。直接掛上本測試的 registry 就不用查。
        """
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, SUPERUSER_ID, {})

    def _run(self, fn, init_type='odoo'):
        """在新的一條真連線上對服務執行 ``fn(service)``，結束時 commit。"""
        with self.registry.cursor() as cr:
            return fn(self._env(cr)[SVC].browse(self.service_ids[init_type]))

    def _entry(self, client, init_type='odoo'):
        self._run(lambda svc: svc._run_post_deploy_init(client), init_type)

    def _read(self, init_type='odoo'):
        def read(svc):
            return {
                'state': svc.state,
                'init_pending': svc.init_pending,
                'init_retries': svc.init_retries,
                'init_unknown_count': svc.init_unknown_count,
                'init_exit': svc.init_exit,
                'lease': svc.init_lease_until,
                'attempt': svc.init_attempt_id.id,
                'n8n_api_key': svc.n8n_api_key,
                'redeploy_requested': svc.init_redeploy_requested,
                'attempts': [
                    (a.sequence, a.outcome, a.finished_at is not False)
                    for a in svc.env[ATTEMPT].search(
                        [('service_id', '=', svc.id)], order='sequence')],
            }
        return self._run(read, init_type)

    def _sql(self, query, params):
        with self.registry.cursor() as cr:
            cr.execute(query, params)

    def _set_lease(self, init_type='odoo', seconds_from_now=-1, outcome=None, legacy=False):
        """模擬「上一次 claim 之後 worker 死掉」：寫一筆在途 attempt 與 lease。"""
        def claim(svc):
            now = svc._init_db_now()
            attempt = svc.env[ATTEMPT].create({
                'service_id': svc.id, 'generation': svc.init_generation,
                'sequence': svc.init_attempt_seq + 1, 'init_type': init_type,
                'claimed_at': now - timedelta(seconds=1300),
                'lease_until': now + timedelta(seconds=seconds_from_now),
                'outcome': 'legacy' if legacy else outcome,
            })
            svc.write({
                'init_attempt_seq': svc.init_attempt_seq + 1,
                'init_attempt_id': attempt.id,
                'init_lease_until': now + timedelta(seconds=seconds_from_now),
            })
        self._run(claim, init_type)

    # -- 驗收 1／2 ---------------------------------------------------------- #

    def test_claim_is_committed_before_the_operator_call(self):
        seen = {}

        def init_odoo(**kwargs):
            with self.registry.cursor() as cr:
                cr.execute(
                    "SELECT init_lease_until > (statement_timestamp() AT TIME ZONE 'UTC'), "
                    "init_attempt_id FROM woow_paas_platform_cloud_service WHERE id = %s",
                    (self.service_ids['odoo'],))
                seen['lease_active'], seen['attempt'] = cr.fetchone()
                cr.execute('SELECT outcome FROM woow_paas_platform_cloud_service_init_attempt '
                           'WHERE id = %s', (seen['attempt'],))
                seen['outcome'] = cr.fetchone()[0]
            return {'success': True}

        client = operator_client()
        client.init_odoo.side_effect = init_odoo
        self._entry(client)

        client.init_odoo.assert_called_once_with(
            namespace=f'dev-sandbox-1257-odoo-{self.tag}', release_name=f'svc-odoo-{self.tag}')
        self.assertTrue(seen['lease_active'], '另一條連線在 operator 呼叫期間看不到 lease')
        self.assertTrue(seen['attempt'])
        self.assertIsNone(seen['outcome'], '結果寫入之前 attempt 應是在途（outcome 空）')
        state = self._read()
        self.assertEqual(state['state'], 'running')
        self.assertFalse(state['init_pending'])
        self.assertFalse(state['lease'])
        self.assertEqual(state['attempts'], [(1, 'success', True)])

    def test_concurrent_entries_send_init_once(self):
        in_flight, release = threading.Event(), threading.Event()
        calls = []

        def slow_init(**kwargs):
            calls.append(kwargs)
            in_flight.set()
            release.wait(30)
            return {'success': True}

        client = operator_client()
        client.init_odoo.side_effect = slow_init
        errors = []

        def first():
            try:
                self._entry(client)
            except Exception as exc:  # 讓主執行緒看得到
                errors.append(exc)

        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(in_flight.wait(20), '第一條連線沒送出 init：%s\n%s' % (
                errors, _thread_stacks(thread)))
            self._entry(client)  # 第二條連線：lease 在途，不得再送
        finally:
            release.set()
            thread.join(60)
        self.assertFalse(errors, errors)
        self.assertEqual(len(calls), 1, 'operator 必須只收到一次 init')
        self.assertEqual(len(self._read()['attempts']), 1)

    def test_simultaneous_claims_only_one_wins(self):
        """兩條連線同時跑條件式 UPDATE：後到的撞 40001／lock 視為沒拿到。"""
        winner_holding, loser_done = threading.Event(), threading.Event()
        results = {}

        def claim_and_hold():
            with self.registry.cursor() as cr:
                svc = self._env(cr)[SVC].browse(self.service_ids['odoo'])
                # 先把 snapshot 拿好，再在不 commit 的情況下佔住列鎖。
                cr.execute('SELECT 1')
                cr.execute(
                    "UPDATE woow_paas_platform_cloud_service SET init_lease_until = "
                    "(statement_timestamp() AT TIME ZONE 'UTC') + interval '1 hour' "
                    "WHERE id = %s AND init_lease_until IS NULL RETURNING id", (svc.id,))
                results['winner'] = cr.fetchall()
                winner_holding.set()
                loser_done.wait(30)

        thread = threading.Thread(target=claim_and_hold)
        thread.start()
        try:
            self.assertTrue(winner_holding.wait(20), _thread_stacks(thread))
            results['loser'] = self._run(lambda svc: svc._init_claim('odoo'))
        finally:
            loser_done.set()
            thread.join(60)
        self.assertEqual(len(results['winner']), 1)
        self.assertIsNone(results['loser'], '列被別人鎖住／剛改過時必須當成沒拿到')
        self.assertEqual(self._read()['attempts'], [])

    # -- 驗收 3 ------------------------------------------------------------ #

    def test_expired_lease_becomes_unknown_and_second_unknown_errors(self):
        self._set_lease(seconds_from_now=-1)
        client = operator_client(init_odoo={
            'success': False, 'error_step': 'update_module_list', 'error_kind': 'unknown'})
        self._entry(client)

        state = self._read()
        self.assertEqual(state['attempts'], [(1, 'unknown', True), (2, 'unknown', True)],
                         '到期的在途 attempt 轉 unknown，接著送出第 2 次')
        self.assertEqual(state['init_unknown_count'], 2)
        self.assertEqual(state['state'], 'error')
        self.assertEqual(state['init_exit'], 'unknown')
        self.assertTrue(state['init_pending'], '轉 error 後仍欠 init')
        self.assertTrue(state['lease'], 'unknown 的 lease 要留到到期（可能仍在叢集內執行）')

    def test_legacy_lease_does_not_count_towards_unknown(self):
        self._set_lease(seconds_from_now=-1, legacy=True)
        client = operator_client(init_odoo={
            'success': False, 'error_step': 'update_module_list', 'error_kind': 'unknown'})
        self._entry(client)

        state = self._read()
        self.assertEqual(state['attempts'], [(1, 'legacy', True), (2, 'unknown', True)])
        self.assertEqual(state['init_unknown_count'], 1)
        self.assertEqual(state['state'], 'initializing')

    def test_unknown_waits_for_lease_expiry_before_resending(self):
        client = operator_client(init_odoo={'success': False, 'error_kind': 'unknown'})
        self._entry(client)
        self._entry(client)
        self.assertEqual(client.init_odoo.call_count, 1, 'lease 未到期前不得重送')
        self.assertEqual(self._read()['init_unknown_count'], 1)

    def test_active_lease_blocks_the_claim(self):
        self._set_lease(seconds_from_now=600)
        client = operator_client(init_odoo={'success': True})
        self._entry(client)
        client.init_odoo.assert_not_called()

    def test_definite_failures_error_on_the_fifth(self):
        client = operator_client(init_odoo={
            'success': False, 'error_step': 'update_module_list', 'error_kind': 'failed'})
        for _ in range(INIT_FAILED_LIMIT):
            self._entry(client)
        state = self._read()
        self.assertEqual(client.init_odoo.call_count, INIT_FAILED_LIMIT,
                         'failed 立即釋放 lease，下一輪就能重送')
        self.assertEqual(state['init_retries'], INIT_FAILED_LIMIT)
        self.assertEqual((state['state'], state['init_exit']), ('error', 'failed'))

    def test_not_executed_consumes_no_quota(self):
        client = operator_client(init_odoo={
            'success': False, 'error_step': 'wait_ready', 'error_kind': 'not_executed'})
        for _ in range(INIT_FAILED_LIMIT + 2):
            self._entry(client)
        state = self._read()
        self.assertEqual(client.init_odoo.call_count, INIT_FAILED_LIMIT + 2)
        self.assertEqual((state['state'], state['init_retries'], state['init_unknown_count']),
                         ('initializing', 0, 0))

    def test_wait_timeout_errors_without_consuming_quota(self):
        self._sql("UPDATE woow_paas_platform_cloud_service SET init_wait_since = "
                  "(statement_timestamp() AT TIME ZONE 'UTC') - interval '31 minutes' "
                  "WHERE id = %s", (self.service_ids['odoo'],))
        client = operator_client(init_odoo={'success': True})
        self._entry(client)
        client.init_odoo.assert_not_called()
        state = self._read()
        self.assertEqual((state['state'], state['init_exit'], state['init_retries']),
                         ('error', 'wait_timeout', 0))

    def test_n8n_partial_stores_the_key_and_counts_as_unknown(self):
        client = operator_client(init_n8n={
            'success': False, 'api_key': _JWT, 'error_step': 'patch_secret',
            'error_kind': 'partial', 'error': f'secret patch failed for {_JWT}'})
        self._entry(client, 'n8n')
        state = self._read('n8n')
        self.assertEqual(state['n8n_api_key'], _JWT, 'partial 的 key 必須先存下來')
        self.assertEqual(state['init_unknown_count'], 1)
        self.assertEqual(state['attempts'], [(1, 'partial', True)])
        self.assertTrue(state['lease'])

        def attempt_error(svc):
            return svc.env[ATTEMPT].search([('service_id', '=', svc.id)]).error or ''
        self.assertNotIn(_JWT, self._run(attempt_error, 'n8n'), 'attempt 記錄不得存 api_key')

    def test_result_is_not_applied_when_the_service_moved_on(self):
        """CAS：operator 呼叫期間服務被停權 ⇒ attempt 有結果、服務不被改成 running。"""
        def init_odoo(**kwargs):
            self._sql("UPDATE woow_paas_platform_cloud_service SET state = 'suspended', "
                      "pre_suspend_state = 'initializing' WHERE id = %s",
                      (self.service_ids['odoo'],))
            return {'success': True}

        client = operator_client()
        client.init_odoo.side_effect = init_odoo
        self._entry(client)
        state = self._read()
        self.assertEqual(state['state'], 'suspended')
        self.assertTrue(state['init_pending'])
        self.assertEqual(state['attempts'], [(1, 'success', True)])
        self.assertFalse(state['lease'], '結果已確定、遠端已結束：lease 直接釋放，resume 不必等到期')

    def test_unknown_result_on_a_moved_on_service_keeps_the_lease(self):
        def init_odoo(**kwargs):
            self._sql("UPDATE woow_paas_platform_cloud_service SET state = 'suspended', "
                      "pre_suspend_state = 'initializing' WHERE id = %s",
                      (self.service_ids['odoo'],))
            return {'success': False, 'error_kind': 'unknown'}

        client = operator_client()
        client.init_odoo.side_effect = init_odoo
        self._entry(client)
        self.assertTrue(self._read()['lease'], '結果未知＝遠端可能仍在跑，lease 留到到期')

    def test_n8n_success_stores_the_key_even_if_the_row_changed_during_the_call(self):
        """r1 S1：operator 呼叫期間不得開著交易；回來後的寫入不能撞舊 snapshot。"""
        def init_n8n(**kwargs):
            self._sql("UPDATE woow_paas_platform_cloud_service SET name = name || ' *' "
                      "WHERE id = %s", (self.service_ids['n8n'],))
            return {'success': True, 'api_key': _JWT}

        client = operator_client()
        client.init_n8n.side_effect = init_n8n
        self._entry(client, 'n8n')
        client.init_n8n.assert_called_once_with(
            namespace=f'dev-sandbox-1257-n8n-{self.tag}', release_name=f'svc-n8n-{self.tag}',
            owner_email=f'owner-{self.tag}@example.invalid', owner_password=f'pw-{self.tag}')
        state = self._read('n8n')
        self.assertEqual(state['state'], 'running')
        self.assertEqual(state['n8n_api_key'], _JWT)

    def test_local_bookkeeping_error_consumes_no_quota(self):
        """Q3：套用結果時平台自己的例外 ⇒ attempt 有結果、lease 釋放、額度不動。"""
        client = operator_client(init_odoo={'success': False, 'error_kind': 'failed'})
        model = type(self.env[SVC])
        with patch.object(model, '_init_apply_outcome', side_effect=RuntimeError('boom')):
            self._entry(client)
        state = self._read()
        self.assertEqual(state['attempts'], [(1, 'failed', True)])
        self.assertEqual((state['init_retries'], state['init_unknown_count']), (0, 0))
        self.assertFalse(state['lease'])
        self.assertEqual(state['state'], 'initializing')

    def test_claim_against_a_stale_snapshot_is_not_acquired(self):
        """Q2：40001（REPEATABLE READ 下別人剛改過這列）＝沒拿到，不是失敗。"""
        with self.registry.cursor() as cr:
            env = self._env(cr)
            svc = env[SVC].browse(self.service_ids['odoo'])
            cr.execute('SELECT 1')  # 先拿 snapshot
            self._sql("UPDATE woow_paas_platform_cloud_service SET name = name || ' *' "
                      "WHERE id = %s", (self.service_ids['odoo'],))
            self.assertIsNone(svc._init_claim('odoo'))
        self.assertEqual(self._read()['attempts'], [])

    # -- 驗收 6（背景那半）------------------------------------------------- #

    def _register_redeploy(self):
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=MagicMock()):
            return self._run(lambda svc: svc._redeploy())

    def test_redeploy_intent_stops_resend_and_runs_after_the_lease(self):
        self._set_lease(seconds_from_now=600)
        action = self._register_redeploy()
        self.assertEqual(action.get('tag'), 'display_notification')
        self.assertTrue(self._read()['redeploy_requested'])

        # 背景看到意圖就停止自動重送（即使 lease 已到期）。
        self._sql("UPDATE woow_paas_platform_cloud_service SET init_lease_until = "
                  "(statement_timestamp() AT TIME ZONE 'UTC') - interval '1 second' "
                  "WHERE id = %s", (self.service_ids['odoo'],))
        client = operator_client(init_odoo={'success': True})
        self._entry(client)
        client.init_odoo.assert_not_called()

        redeploy_client = MagicMock()
        redeploy_client.upgrade_release.return_value = {'revision': 2}
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=redeploy_client):
            with self.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                env[SVC].search([('id', '=', self.service_ids['odoo'])]) \
                    ._cron_run_requested_redeploys()
        redeploy_client.upgrade_release.assert_called_once()
        state = self._read()
        self.assertFalse(state['redeploy_requested'])
        self.assertEqual(state['state'], 'deploying')

    def test_queued_redeploy_refusal_is_recorded_not_silent(self):
        self._set_lease(seconds_from_now=600)
        self._register_redeploy()
        # 欄位是 NOT NULL，「沒釘版」在資料上是空字串（#552 的判準是 falsy）。
        self._sql("UPDATE woow_paas_platform_cloud_app_template SET helm_chart_version = '' "
                  "WHERE id = %s", (self.template_ids['odoo'],))
        self._sql("UPDATE woow_paas_platform_cloud_service SET init_lease_until = "
                  "(statement_timestamp() AT TIME ZONE 'UTC') - interval '1 second' "
                  "WHERE id = %s", (self.service_ids['odoo'],))
        redeploy_client = MagicMock()
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=redeploy_client):
            with self.registry.cursor() as cr:
                self._env(cr)[SVC]._cron_run_requested_redeploys()
        redeploy_client.upgrade_release.assert_not_called()
        state = self._run(lambda svc: (svc.init_redeploy_requested, svc.error_message))
        self.assertFalse(state[0], '被拒的意圖清掉，不每分鐘重撞')
        self.assertIn('The queued redeploy did not run', state[1] or '')


# ====================================================================== #
#  驗收 6（HTTP 那半）與 list/get 不再 dispatch init                        #
# ====================================================================== #

class _ServiceCase(TransactionCase):
    def setUp(self):
        super().setUp()
        self.tag = uuid.uuid4().hex[:8]
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': f'1257 WS {self.tag}'})
        self.templates = {
            init_type: self.env['woow_paas_platform.cloud_app_template'].create(
                _template_vals(self.tag, init_type))
            for init_type in ('odoo', 'n8n', 'none')
        }

    def _service(self, init_type='odoo', **vals):
        return _make_service(self.env, self.workspace, self.templates[init_type],
                             f'{init_type}-{uuid.uuid4().hex[:6]}', **vals)

    def _poll_http(self, service, pods=None):
        client = operator_client(get_status=status_response(pods=pods or _READY_PODS))
        with patch(f'{_CONTROLLER_MODULE}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER_MODULE}.request', new=SimpleNamespace(env=self.env)):
            PaasController()._update_service_status(service)
        return client

    def _poll_cron(self, service, pods=None):
        client = operator_client(get_status=status_response(pods=pods or _READY_PODS))
        with patch.object(type(service), '_run_post_deploy_init') as entry:
            service._poll_and_update_status(client)
        return client, entry

    def _reconfigure(self, service, odoo_addon_ids=None):
        # MagicMock 而非 autospec：`_update_service` 讀 client 的 instance 屬性
        # （base_url／_session），autospec 的 class spec 沒有它們。
        client = MagicMock()
        with patch(f'{_CONTROLLER_MODULE}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER_MODULE}.request', new=SimpleNamespace(env=self.env)), \
                patch(f'{_CONTROLLER_MODULE}.threading.Thread', _NoThread):
            return PaasController()._update_service(
                service, values={}, version=None, odoo_addon_ids=odoo_addon_ids)

    def _rollback(self, service):
        client = MagicMock()
        with patch(f'{_CONTROLLER_MODULE}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER_MODULE}.request', new=SimpleNamespace(env=self.env)):
            return PaasController()._rollback_service(service, revision=1)

    def _hold_lease(self, service):
        service.write({'init_lease_until': fields.Datetime.now() + timedelta(minutes=10)})


@tagged('post_install', '-at_install')
class TestInitLeaseGuards1257(_ServiceCase):

    def test_list_get_poll_only_advances_state(self):
        """#1257 d：HTTP poll 推進 deploying→initializing，但不呼叫 init_odoo／init_n8n。"""
        for init_type in ('odoo', 'n8n'):
            with self.subTest(init_type=init_type):
                svc = self._service(init_type)
                client = self._poll_http(svc)
                self.assertEqual(svc.state, 'initializing')
                self.assertTrue(svc.init_wait_since)
                client.init_odoo.assert_not_called()
                client.init_n8n.assert_not_called()
                client = self._poll_http(svc)
                self.assertEqual(svc.state, 'initializing')
                client.init_odoo.assert_not_called()
                client.init_n8n.assert_not_called()

    def test_cron_poll_dispatches_the_single_entry(self):
        svc = self._service('odoo')
        _client, entry = self._poll_cron(svc)
        self.assertEqual(svc.state, 'initializing')
        entry.assert_called_once()

    def test_reconfigure_and_rollback_are_refused_while_the_lease_is_held(self):
        svc = self._service('odoo', state='initializing')
        self._hold_lease(svc)
        for action in (self._reconfigure, self._rollback):
            with self.subTest(action=action.__name__):
                result = action(svc)
                self.assertFalse(result['success'])
                self.assertIn('may still be running in the cluster', result['error'])
                self.assertEqual(svc.state, 'initializing')

    def test_redeploy_during_lease_registers_intent_without_raising(self):
        svc = self._service('odoo', state='initializing')
        self._hold_lease(svc)
        client = MagicMock()
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=client):
            action = svc.action_redeploy()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertTrue(svc.init_redeploy_requested)
        client.upgrade_release.assert_not_called()

    def test_init_exhausted_error_may_reconfigure_and_opens_a_new_generation(self):
        for init_exit in ('failed', 'unknown'):
            with self.subTest(init_exit=init_exit):
                svc = self._service('odoo', state='error')
                svc.write({'init_exit': init_exit, 'init_retries': INIT_FAILED_LIMIT,
                           'init_unknown_count': 2, 'error_message': 'x'})
                generation = svc.init_generation
                result = self._reconfigure(svc)
                self.assertTrue(result['success'], result)
                self.assertEqual(svc.state, 'upgrading')
                self.assertEqual(svc.init_generation, generation + 1)
                self.assertEqual((svc.init_retries, svc.init_unknown_count, svc.init_exit),
                                 (0, 0, False))

    def test_redeploy_refusal_that_can_be_decided_now_is_raised_now(self):
        """r1 S3：lease 在途時登記意圖之前先跑 #552 的釘版檢查，admin 當場看得到。"""
        svc = self._service('odoo', state='initializing')
        self._hold_lease(svc)
        svc.template_id.helm_chart_version = ''  # NOT NULL；「沒釘版」＝空字串
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=MagicMock()):
            with self.assertRaises(UserError):
                svc._redeploy()
        self.assertFalse(svc.init_redeploy_requested)

    def test_init_exit_is_dropped_once_the_service_leaves_error(self):
        """r1 S4：刪除失敗等路徑寫回的 error 不得沿用上一次的額度出口。"""
        svc = self._service('odoo', state='error')
        svc.write({'init_exit': 'failed'})
        svc.write({'state': 'suspended', 'pre_suspend_state': 'error'})
        self.assertEqual(svc.init_exit, 'failed', '停權不算離開 error（Q25）')
        svc.write({'state': 'error', 'pre_suspend_state': False})
        self.assertEqual(svc.init_exit, 'failed')
        svc.write({'state': 'deleting'})
        self.assertFalse(svc.init_exit)
        svc.write({'state': 'error'})
        self.assertFalse(self._reconfigure(svc)['success'], 'helm 失敗的 error 照擋')

    def test_release_gone_while_initializing_hits_the_wait_timeout(self):
        """r1 S5：get_status 404 時 poll 拿不到 status，只剩等待時限能收尾。"""
        svc = self._service('odoo', state='initializing')
        svc.sudo().write({'init_wait_since': fields.Datetime.now() - timedelta(minutes=31)})
        client = operator_client()
        client.get_status.side_effect = PaaSOperatorError('gone', status_code=404)
        svc._poll_and_update_status(client)
        self.assertEqual((svc.state, svc.init_exit), ('error', 'wait_timeout'))

        other = self._service('odoo', state='initializing')
        other.sudo().write({'init_wait_since': fields.Datetime.now() - timedelta(minutes=31)})
        client.get_status.side_effect = PaaSOperatorError('down', status_code=502)
        other._poll_and_update_status(client)
        self.assertEqual(other.state, 'initializing', 'operator 整體故障是平台問題，不計時')

    def test_other_errors_still_block_reconfigure(self):
        for init_exit in (False, 'wait_timeout'):
            with self.subTest(init_exit=init_exit):
                svc = self._service('odoo', state='error')
                svc.write({'init_exit': init_exit})
                self.assertFalse(self._reconfigure(svc)['success'])
                self.assertEqual(svc.state, 'error')


# ====================================================================== #
#  驗收 5：#1267 reconfigure／rollback 繞過 init                            #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReconfigureRollbackOwedInit1267(_ServiceCase):

    def test_reconfigure_without_addon_change_keeps_the_owed_init(self):
        svc = self._service('odoo', state='initializing')
        self.assertTrue(svc.init_pending)
        generation = svc.init_generation
        self.assertTrue(self._reconfigure(svc)['success'])
        self.assertEqual(svc.state, 'upgrading')
        self.assertEqual(svc.init_generation, generation, '不改 addon 的 reconfigure 不開新一代')
        _client, entry = self._poll_cron(svc)
        self.assertEqual(svc.state, 'initializing', '欠的 init 被跳過、直接 running')
        entry.assert_called_once()

    def test_addon_change_then_plain_reconfigure_still_inits(self):
        """#1267 驗收 2：先改 addon（走 controller 真的 addon 分支），init 完成前再做一次
        不改 addon 的 reconfigure，init 仍然會執行。"""
        odoo_template = self.env['woow_paas_platform.cloud_app_template'].search(
            [('slug', '=', 'odoo')], limit=1)
        self.assertTrue(odoo_template, '前提：seed 的 odoo template 存在（addon 分支只認 slug）')
        addon = self.env['woow_paas_platform.cloud_odoo_addon'].create({
            'name': f'1257 addon {self.tag}', 'technical_name': f'addon_1257_{self.tag}',
            'source_type': 'built_in'})
        svc = _make_service(self.env, self.workspace, odoo_template, f'addon-{self.tag}',
                            state='running')
        svc.sudo().write({'init_pending': False})
        generation = svc.init_generation
        self.assertTrue(self._reconfigure(svc, odoo_addon_ids=[addon.id])['success'])
        self.assertEqual((svc.init_pending, svc.init_generation, svc.addon_update_pending),
                         (True, generation + 1, True), '改 addon 開新一代')
        self._poll_http(svc)
        self.assertEqual(svc.state, 'initializing')
        self.assertTrue(self._reconfigure(svc, odoo_addon_ids=[addon.id])['success'])
        self.assertEqual(svc.init_generation, generation + 1, '沒改 addon 不開新一代')
        self.assertTrue(svc.init_pending)
        _client, entry = self._poll_cron(svc)
        self.assertEqual(svc.state, 'initializing')
        entry.assert_called_once()

    def test_rollback_from_init_error_opens_a_new_generation(self):
        svc = self._service('odoo', state='error')
        svc.write({'init_exit': 'failed', 'init_retries': INIT_FAILED_LIMIT,
                   'init_unknown_count': 1})
        generation = svc.init_generation
        self.assertTrue(self._rollback(svc)['success'])
        self.assertEqual(svc.state, 'upgrading')
        self.assertEqual(svc.init_generation, generation + 1)
        self.assertEqual((svc.init_retries, svc.init_unknown_count), (0, 0))
        self._poll_http(svc)
        self.assertEqual(svc.state, 'initializing', 'rollback 之後被直接寫成 running')

    def test_rollback_of_owed_init_opens_a_new_generation(self):
        svc = self._service('odoo', state='initializing')
        svc.sudo().write({'init_retries': 3, 'init_unknown_count': 1})
        generation = svc.init_generation
        self.assertTrue(self._rollback(svc)['success'])
        self.assertEqual((svc.init_generation, svc.init_retries, svc.init_unknown_count),
                         (generation + 1, 0, 0))
        _client, entry = self._poll_cron(svc)
        self.assertEqual(svc.state, 'initializing')
        entry.assert_called_once()

    def test_rollback_of_completed_init_does_not_reinit(self):
        """M2 裁定 (A)／#1267 驗收 5：init 已完成的服務 rollback 不開代、不呼叫 init。"""
        for init_type in ('odoo', 'n8n'):
            with self.subTest(init_type=init_type):
                svc = self._service(init_type, state='running')
                svc.sudo().write({'init_pending': False})
                generation = svc.init_generation
                self.assertTrue(self._rollback(svc)['success'])
                self.assertEqual(svc.state, 'upgrading')
                self.assertEqual((svc.init_generation, svc.init_pending), (generation, False))
                client, entry = self._poll_cron(svc)
                self.assertEqual(svc.state, 'running')
                entry.assert_not_called()
                client.init_odoo.assert_not_called()
                client.init_n8n.assert_not_called()

    def test_services_without_owed_init_are_unchanged(self):
        none_svc = self._service('none', state='running')
        self.assertFalse(none_svc.init_pending)
        done_svc = self._service('odoo', state='running')
        done_svc.write({'init_pending': False})
        for svc in (none_svc, done_svc):
            with self.subTest(template=svc.template_id.post_deploy_init_type):
                self.assertTrue(self._reconfigure(svc)['success'])
                self._poll_http(svc)
                self.assertEqual(svc.state, 'running')
        self.assertTrue(self._rollback(none_svc)['success'])
        self._poll_http(none_svc)
        self.assertEqual(none_svc.state, 'running')


# ====================================================================== #
#  驗收 5：#1266 suspend 吃掉 init                                          #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestSuspendResumeOwedInit1266(_ServiceCase):

    def _cycle(self, svc):
        client = MagicMock()
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=client):
            svc._do_suspend()
            self.assertEqual(svc.state, 'suspended')
            svc._do_resume()

    def test_transitional_services_owing_init_resume_to_initializing(self):
        cases = (
            ('initializing', {}),
            ('deploying', {}),
            ('upgrading', {'addon_update_pending': True}),
        )
        for state, extra in cases:
            with self.subTest(state=state):
                svc = self._service('n8n', state=state, **extra)
                generation = svc.init_generation
                self._cycle(svc)
                self.assertEqual(svc.state, 'initializing')
                self.assertTrue(svc.init_pending)
                self.assertEqual(svc.init_generation, generation, 'resume 不開新一代')
                _client, entry = self._poll_cron(svc)
                entry.assert_called_once()

    def test_error_stays_error_and_keeps_its_counters(self):
        svc = self._service('odoo', state='error')
        svc.write({'init_exit': 'failed', 'init_retries': INIT_FAILED_LIMIT,
                   'init_unknown_count': 1})
        self._cycle(svc)
        self.assertEqual(svc.state, 'error')
        self.assertEqual((svc.init_retries, svc.init_unknown_count), (INIT_FAILED_LIMIT, 1))

    def test_running_service_is_unchanged(self):
        for init_type in ('odoo', 'none'):
            with self.subTest(init_type=init_type):
                svc = self._service(init_type, state='running')
                svc.write({'init_pending': False})
                self._cycle(svc)
                self.assertEqual(svc.state, 'running')

    def test_running_but_owing_init_after_suspension_resumes_to_initializing(self):
        """r2 裁定 h(i)：停權期間開了新一代（改 addon／redeploy）也要補 init。"""
        svc = self._service('odoo', state='running')
        svc.write({'init_pending': False})
        client = MagicMock()
        with patch(f'{_OPERATOR_MODULE}.get_paas_operator_client', return_value=client):
            svc._do_suspend()
            svc._open_init_generation()
            svc._do_resume()
        self.assertEqual(svc.state, 'initializing')


# ====================================================================== #
#  驗收 7：migration                                                        #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestInitMigration1257(_ServiceCase):

    def test_backfill_and_legacy_lease(self):
        make = self._service
        initializing = make('odoo', state='initializing')
        upgrading = make('odoo', state='upgrading', addon_update_pending=True)
        upgrading_plain = make('odoo', state='upgrading')
        deploying = make('n8n', state='deploying')
        pending = make('odoo', state='pending')
        none_deploying = make('none', state='deploying')
        errored = make('odoo', state='error')
        suspended = make('odoo', state='suspended', pre_suspend_state='running')
        rows = initializing | upgrading | upgrading_plain | deploying | pending \
            | none_deploying | errored | suspended
        # 模擬 #1257 以前的資料：沒有任何欠 init 標記。
        rows.write({'init_pending': False, 'init_generation': 0, 'init_retries': 3})

        with self.assertLogs('odoo.addons.woow_paas_platform.models.cloud_service',
                             level=logging.WARNING) as logs:
            backfilled, legacy = self.env[SVC]._woow_migrate_init_execution_control_1257()

        owed = initializing | upgrading | deploying | pending
        self.assertEqual(backfilled & rows, owed)
        for svc in owed:
            self.assertTrue(svc.init_pending, svc.state)
            self.assertEqual(svc.init_generation, 1)
            self.assertEqual(svc.init_retries, 0, '舊的失敗次數不得帶進新的一代')
        for svc in (upgrading_plain, none_deploying, errored, suspended):
            self.assertFalse(svc.init_pending, svc.state)
        self.assertEqual(errored.init_retries, 3, 'error 的列不動')

        self.assertEqual(legacy & rows, initializing)
        attempt = initializing.init_attempt_id
        self.assertEqual(attempt.outcome, 'legacy')
        self.assertEqual(initializing.init_retries, 0)
        self.assertAlmostEqual(
            (initializing.init_lease_until - attempt.claimed_at).total_seconds(), 1200, delta=1)
        self.assertTrue(any(suspended.name in line for line in logs.output),
                        'init 類型不是 none 的停權服務要列進 log 交人工抽查')

        # 冪等：重跑不再開新一代、不重寫 legacy。
        again, again_legacy = self.env[SVC]._woow_migrate_init_execution_control_1257()
        self.assertFalse(again & rows)
        self.assertFalse(again_legacy & rows)


# ====================================================================== #
#  Q39：attempt 記錄的保留政策                                              #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestInitAttemptRetention1257(_ServiceCase):

    def _attempt(self, svc, generation, seq, days_ago=0):
        claimed = fields.Datetime.now() - timedelta(days=days_ago)
        return self.env[ATTEMPT].create({
            'service_id': svc.id, 'generation': generation, 'sequence': seq,
            'init_type': 'odoo', 'claimed_at': claimed,
            'lease_until': claimed + timedelta(seconds=1200),
            'finished_at': claimed, 'outcome': 'failed',
        })

    def test_keeps_latest_twenty_and_the_current_generation(self):
        svc = self._service('odoo', state='initializing')
        svc.write({'init_generation': 2})
        old_gen = [self._attempt(svc, 1, seq) for seq in range(1, 26)]
        ancient_current = self._attempt(svc, 2, 26, days_ago=100)
        current = [self._attempt(svc, 2, seq) for seq in range(27, 29)]
        # 最近 20 筆裡的一筆舊代紀錄，但已超過 90 天 ⇒ 仍要清。
        old_gen[-1].write({'claimed_at': fields.Datetime.now() - timedelta(days=91)})

        self.env[ATTEMPT]._cron_purge_init_attempts()

        remaining = self.env[ATTEMPT].search([('service_id', '=', svc.id)])
        self.assertIn(ancient_current, remaining, '當代的 attempt 不論多舊都保留（額度依據）')
        for attempt in current:
            self.assertIn(attempt, remaining)
        # 最近 20 筆（依 id）＝3 筆當代＋17 筆舊代；其中 1 筆舊代超過 90 天。
        kept_old = remaining.filtered(lambda a: a.generation == 1)
        self.assertEqual(len(kept_old), 16)
        self.assertNotIn(old_gen[-1], remaining)
        self.assertEqual(set(kept_old.mapped('sequence')), set(range(9, 25)))
