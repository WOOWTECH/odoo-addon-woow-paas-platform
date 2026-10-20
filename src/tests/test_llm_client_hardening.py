"""LLM-02：newapi_client 與代理完整化的 Odoo 端（DESIGN §1.5、§2.11、§3.2、§3.6、§5.2 T3；N26、N27）。

**mock 的邊界**：circuit 與限速測**真的** ``NewApiClient``＋``LlmCallGuard``（DB 狀態），只把 requests 的
Session 換成假的（回固定的回應或丟傳輸例外）——分類、鉤子、獨立 cursor 的 UPDATE 都真的跑。SWR 與 cron
沿用 ``llm_fake_newapi``（mock 的是 client）。HttpCase 是為了 test mode：helper 的獨立 cursor 是同一條測試
交易的 savepoint，全部跟著回捲（同 ``test_llm_platform_foundation``）。

Class 一覽（對到票的驗收）：

* ``TestLlmCircuitBreaker``（AC2）：連續 5 次 NotExecuted／Unknown 才開、開的那一次告警一次；denied、中性的
  NotExecuted 不計；代理 401／403 ``policy`` 與 AP 401 一次就開；開著時不送；半開時 cron 健康檢查成功就關、
  失敗再開；web 在半開時叫醒外呼類 cron；bg 不受擋但每次告警。
* ``TestLlmCircuitCronGate``（AC2「cron 跳過」）：外呼類 cron 在 circuit 開著時不跑工作函式，清理類照跑。
* ``TestLlmApRateLimit``（§1.5）：一分鐘 60 次、低優先序先停（餘額刷新最後）、換分鐘歸零、只限 ap。
* ``TestLlmSwr``（N26、AC2）：state／keys/list／usage/recent 只讀快取、零外呼、帶資料時間；過期才排背景
  刷新（不重複排）；刷新 cron 以 CRON 金鑰、該帳戶的 UP 讀；new-api 掛掉時舊資料照回；circuit 開著照回快取。
* ``TestLlmConfigChange``（§2.11）：``llm_public_base_url`` 只收白名單；三個 key 一改就（commit 後）寫
  ``config_change``、更新指紋、告警；其他 key 不會。
* MCP 跳板（AC5）在 ``test_llm_mcp_jump_guard``。
* ``TestLlmConnectionTest``：〔測試連線〕只給 system、排 cron；cron 跑 readyz／AP 健康檢查／status 不變量，
  結果只有狀態碼與布林。
"""
import json
import os
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import requests
from requests.structures import CaseInsensitiveDict

from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import HttpCase, new_test_user, tagged

from ..models import llm_common
from ..models import llm_event as llm_event_module
from ..models import llm_runtime_state as runtime_module
from ..models.llm_common import run_llm_cron
from ..models.llm_runtime_state import STATE_TABLE
from ..models.llm_swr import CACHE_MODEL, CACHE_TABLE, SWR_CRON_XMLID
from ..services import newapi_client as nac
from .llm_fake_newapi import FakeNewApi
from .llm_test_support import (
    ACCOUNT,
    EVENT,
    RUNTIME,
    TEST_TOS,
    LlmClientAdapter,
    configure,
    llm_env,
    scope_claims,
    scope_swr_queue,
)

KID = 'k-test'
SECRET = b'S' * 40
TENANT_IP = '198.18.17.9'
UP = 'nap_' + 'a1B2' * 10 + 'xyz'
CLOCK = datetime(2031, 4, 1, 4, 0, 0)


class _Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self.content = b'' if body is None else json.dumps(body).encode()
        self.headers = CaseInsensitiveDict(headers or {'Content-Type': 'application/json'})


class _Session:
    """requests.Session 的替身：``outcome`` 是回應或要丟的例外；``sent`` 記送出的次數。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.sent = 0

    def request(self, *args, **kwargs):
        self.sent += 1
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    def close(self):
        pass


class _LlmCase(HttpCase):
    """設定好 LLM（stg 代理、prefix ``wqa``、block 7）＋測試用 env；告警時鐘固定在 2031 年。"""

    def setUp(self):
        super().setUp()
        configure(self.env)
        env_patch = patch.dict(os.environ, llm_env())
        env_patch.start()
        self.addCleanup(env_patch.stop)
        clock = patch.object(llm_event_module, '_utcnow', lambda: CLOCK)
        clock.start()
        self.addCleanup(clock.stop)
        self.State = self.env[RUNTIME].sudo()

    def _alerts(self, alert_code, error_code=None):
        # 只看本測試寫的（固定時鐘 2031 年）：共用 DB 裡 live server 真的告警都在這之前。
        domain = [('alert_code', '=', alert_code), ('create_date', '>=', CLOCK - timedelta(hours=1))]
        if error_code:
            domain.append(('error_code', '=', error_code))
        return self.env[EVENT].sudo().search(domain)

    def _client(self, outcome, role=nac.ROLE_WEB):
        session = _Session(outcome)
        client = nac.NewApiClient(nac.PROXY_URL_STG, KID, SECRET, role,
                                  guard=self.State._llm_call_guard(role),
                                  session_factory=lambda: session)
        return client, session

    def _state(self):
        return self.State._llm_read()

    def _set_circuit(self, open_until, failures=nac.CIRCUIT_FAILURE_THRESHOLD, reason='x'):
        self.env.cr.execute(
            'UPDATE %s SET circuit_open_until = %%s, circuit_failures = %%s, circuit_reason = %%s'
            % STATE_TABLE, (open_until, failures, reason))


# ====================================================================== #
#  circuit breaker（DESIGN §3.6；AC2）                                     #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_client_hardening')
class TestLlmCircuitBreaker(_LlmCase):

    def test_five_consecutive_failures_open_it_once_and_block_the_sixth(self):
        client, session = self._client(requests.exceptions.ChunkedEncodingError())
        for attempt in range(nac.CIRCUIT_FAILURE_THRESHOLD):
            result = client.user_self(TENANT_IP, UP)
            self.assertIsInstance(result, nac.Unknown, attempt)
        self.assertEqual(session.sent, nac.CIRCUIT_FAILURE_THRESHOLD)
        state = self._state()
        self.assertGreater(state['circuit_open_until'], fields.Datetime.now())
        self.assertEqual(state['circuit_reason'], nac.CIRCUIT_FAILURES_CODE)
        blocked = client.user_self(TENANT_IP, UP)
        self.assertIsInstance(blocked, nac.NotExecuted)
        self.assertEqual(blocked.reason, 'circuit_open')
        self.assertGreater(blocked.retry_after, 0)
        self.assertEqual(session.sent, nac.CIRCUIT_FAILURE_THRESHOLD, '開著時不送')
        alerts = self._alerts('circuit_open', nac.CIRCUIT_FAILURES_CODE)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts.occurrences, 1, '只有打開的那一次告警')

    def test_open_and_failed_probe_schedule_the_next_recovery(self):
        cron = self.env.ref(SWR_CRON_XMLID)
        triggers = self.env['ir.cron.trigger'].sudo()
        before = set(triggers.search([('cron_id', '=', cron.id)]).ids)
        self.State._llm_circuit_record(nac.CIRCUIT_TRIP, 'llm_proxy_policy')
        opened_until = self._state()['circuit_open_until']
        queued = triggers.search([('cron_id', '=', cron.id), ('id', 'not in', list(before))])
        self.assertEqual(queued.mapped('call_at'), [opened_until],
                         '開啟時就要排到期探測，不等 5 分鐘保險排程或使用者重整')
        before.update(queued.ids)
        self._set_circuit(fields.Datetime.now() - timedelta(seconds=1))
        self.assertFalse(self.State._llm_circuit_gate(lambda: nac.NotExecuted('connect')))
        reopened_until = self._state()['circuit_open_until']
        queued = triggers.search([('cron_id', '=', cron.id), ('id', 'not in', list(before))])
        self.assertEqual(queued.mapped('call_at'), [reopened_until],
                         '探測失敗也必須排下一次，不能失去自動恢復')

    def test_success_resets_the_count_and_business_answers_count_as_reachable(self):
        failing, _session = self._client(nac_connect_error())
        for _ in range(nac.CIRCUIT_FAILURE_THRESHOLD - 1):
            failing.user_self(TENANT_IP, UP)
        self.assertEqual(self._state()['circuit_failures'], nac.CIRCUIT_FAILURE_THRESHOLD - 1)
        ok, _session = self._client(_Response(200, {'success': True, 'data': {}}))
        self.assertTrue(ok.user_self(TENANT_IP, UP).ok)
        self.assertEqual(self._state()['circuit_failures'], 0)
        for _ in range(nac.CIRCUIT_FAILURE_THRESHOLD - 1):
            failing.user_self(TENANT_IP, UP)
        business, _session = self._client(_Response(200, {'success': False, 'message_key': 'x'}))
        business.user_self(TENANT_IP, UP)
        self.assertEqual(self._state()['circuit_failures'], 0, 'new-api 明確的拒絕＝連得到')
        self.assertFalse(self._state()['circuit_open_until'])

    def test_denied_and_neutral_results_do_not_count(self):
        denied, _session = self._client(_Response(
            403, {'success': False, 'proxy_error': 'denied', 'reason': 'reveal_window'},
            {'X-Woow-Proxy-Error': 'denied'}))
        limited, _session = self._client(_Response(429, None, {'Retry-After': '9'}))
        budget, _session = self._client(_Response(
            429, {'success': False, 'proxy_error': 'budget', 'reason': 'hourly_cap'},
            {'X-Woow-Proxy-Error': 'budget'}), role=nac.ROLE_CRON)
        for _ in range(nac.CIRCUIT_FAILURE_THRESHOLD + 1):
            denied.user_reveal_token(TENANT_IP, UP, 5)
            limited.user_self(TENANT_IP, UP)
            budget.ap_add_quota(5, 15625)
        self.assertEqual(self._state()['circuit_failures'], 0)
        self.assertFalse(self._state()['circuit_open_until'])

    def test_proxy_policy_and_service_pat_rejection_open_it_at_once(self):
        policy, _session = self._client(_Response(
            403, {'success': False, 'proxy_error': 'policy', 'reason': 'route'},
            {'X-Woow-Proxy-Error': 'policy'}))
        policy.user_self(TENANT_IP, UP)
        state = self._state()
        self.assertTrue(state['circuit_open_until'])
        self.assertEqual(state['circuit_reason'], 'llm_proxy_policy')
        self.assertEqual(len(self._alerts('circuit_open', 'llm_proxy_policy')), 1)

        self._set_circuit(None, failures=0, reason=None)
        ap, _session = self._client(_Response(401, {'success': False, 'code': 'AUTH_UNAUTHORIZED'}),
                                    role=nac.ROLE_CRON)
        ap.ap_get_user(5)
        state = self._state()
        self.assertTrue(state['circuit_open_until'])
        self.assertEqual(state['circuit_reason'], 'newapi_auth_unauthorized')

        self._set_circuit(None, failures=0, reason=None)
        up, _session = self._client(_Response(401, {'success': False, 'code': 'AUTH_UNAUTHORIZED'}))
        up.user_self(TENANT_IP, UP)
        self.assertFalse(self._state()['circuit_open_until'], '一戶的 UP 被拒不是全域故障')

    def test_half_open_web_triggers_the_probe_cron_and_stays_closed_to_web(self):
        self._set_circuit(fields.Datetime.now() - timedelta(seconds=1))
        cron = self.env.ref(SWR_CRON_XMLID)
        triggers = self.env['ir.cron.trigger'].sudo().search_count([('cron_id', '=', cron.id)])
        client, session = self._client(_Response(200, {'success': True, 'data': {}}))
        result = client.user_self(TENANT_IP, UP)
        self.assertEqual((result.kind, result.reason), ('not_executed', 'circuit_open'))
        self.assertEqual(session.sent, 0, 'web 沒有 AP，不能自己探')
        self.assertGreater(
            self.env['ir.cron.trigger'].sudo().search_count([('cron_id', '=', cron.id)]), triggers)

    def test_gate_probe_closes_or_reopens(self):
        self.assertTrue(self.State._llm_circuit_gate(lambda: self.fail('關著不用探')))
        self._set_circuit(fields.Datetime.now() + timedelta(seconds=60))
        self.assertFalse(self.State._llm_circuit_gate(lambda: self.fail('還沒到不用探')))

        self._set_circuit(fields.Datetime.now() - timedelta(seconds=1))
        self.assertFalse(self.State._llm_circuit_gate(lambda: nac.NotExecuted('connect')))
        state = self._state()
        self.assertGreater(state['circuit_open_until'], fields.Datetime.now(), '失敗就再開')

        self._set_circuit(fields.Datetime.now() - timedelta(seconds=1))
        probes = []

        def probe():
            probes.append(runtime_module._PROBING.get())
            return nac.Ok(data={'items': []}, status=200)
        self.assertTrue(self.State._llm_circuit_gate(probe))
        self.assertEqual(probes, [True], '探的那一次不受 circuit 擋')
        state = self._state()
        self.assertEqual((state['circuit_open_until'], state['circuit_failures'],
                          state['circuit_reason']), (None, 0, None))

    def test_probe_call_is_not_blocked_but_others_are(self):
        self._set_circuit(fields.Datetime.now() + timedelta(seconds=60))
        client, session = self._client(_Response(200, {'success': True, 'data': {}}),
                                       role=nac.ROLE_CRON)
        self.assertEqual(client.ap_health('wqa').reason, 'circuit_open')
        token = runtime_module._PROBING.set(True)
        try:
            self.assertTrue(client.ap_health('wqa').ok)
        finally:
            runtime_module._PROBING.reset(token)
        self.assertEqual(session.sent, 1)

    def test_break_glass_is_never_blocked_and_always_alerts(self):
        self._set_circuit(fields.Datetime.now() + timedelta(seconds=60))
        client, session = self._client(_Response(200, {'success': True, 'data': {}}),
                                       role=nac.ROLE_CRON)
        self.assertTrue(client.bg_set_user_group(5, 'free_blocked').ok)
        off, _session = self._client(_Response(
            403, {'success': False, 'proxy_error': 'policy', 'reason': 'break_glass_off'},
            {'X-Woow-Proxy-Error': 'policy'}), role=nac.ROLE_CRON)
        off.bg_subtract_quota(5, 15625)
        self.assertEqual(session.sent, 1)
        self.assertEqual(len(self._alerts('break_glass_used', llm_common.BREAK_GLASS_USED)), 1)
        self.assertEqual(len(self._alerts('break_glass_used', llm_common.BREAK_GLASS_FAILED)), 1)
        self.assertEqual(self._state()['circuit_reason'], 'x', 'bg 的結果不動 circuit')


def nac_connect_error():
    """連線前就失敗（NewConnectionError）的 requests 例外：NotExecuted('connect')。"""
    from urllib3.exceptions import NewConnectionError
    return requests.exceptions.ConnectionError(NewConnectionError(None, 'refused'))


@tagged('post_install', '-at_install', 'llm_client_hardening')
class TestLlmCircuitCronGate(_LlmCase):

    def setUp(self):
        super().setUp()
        self.fake = FakeNewApi()
        LlmClientAdapter(self, self.fake, real_circuit=True)

    def test_outbound_cron_skips_while_open_and_probes_when_half_open(self):
        ran = []
        work = lambda config, tick: ran.append(1)  # noqa: E731
        self._set_circuit(fields.Datetime.now() + timedelta(seconds=60))
        self.assertFalse(run_llm_cron(self.env, SWR_CRON_XMLID, work, items=1))
        self.assertEqual(ran, [], 'circuit 開著：跳過這一輪')
        self.assertFalse(self.fake.calls)
        # 清理類（不碰 new-api）照跑。
        run_llm_cron(self.env, SWR_CRON_XMLID, work, items=1, outbound=False)
        self.assertEqual(ran, [1])
        # 半開：AP 健康檢查通過才跑，circuit 關上。
        self._set_circuit(fields.Datetime.now() - timedelta(seconds=1))
        run_llm_cron(self.env, SWR_CRON_XMLID, work, items=1)
        self.assertEqual(ran, [1, 1])
        self.assertEqual([name for name, _ip in self.fake.calls], ['ap_health'])
        self.assertFalse(self._state()['circuit_open_until'])


    def test_health_probe_consumes_the_same_tick_budget_as_work(self):
        now = [100.0]
        real_tick = llm_common.LlmCronTick
        observed = []

        def tick_factory(seconds, items):
            return real_tick(seconds, items, monotonic=lambda: now[0])

        def gate(env, config, deadline=None):
            observed.append(('probe_deadline', deadline))
            now[0] += 25
            return True

        def work(config, tick):
            observed.append(('work_remaining', tick.remaining()))
            return False

        with patch.object(llm_common, 'LlmCronTick', side_effect=tick_factory), \
                patch.object(llm_common, 'circuit_allows', side_effect=gate):
            run_llm_cron(self.env, SWR_CRON_XMLID, work, seconds=30, items=1)
        self.assertEqual(observed, [('probe_deadline', 130.0), ('work_remaining', 5.0)])

    def test_half_open_probe_receives_the_cron_deadline(self):
        self._set_circuit(fields.Datetime.now() - timedelta(seconds=1))
        with patch.object(self.fake, 'ap_health', wraps=self.fake.ap_health) as health:
            self.assertTrue(llm_common.circuit_allows(
                self.env, llm_common.llm_config(self.env), deadline=1234.0))
        self.assertEqual(health.call_args.kwargs['deadline'], 1234.0)


# ====================================================================== #
#  AP 限速（DESIGN §1.5）                                                  #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_client_hardening')
class TestLlmApRateLimit(_LlmCase):

    def test_priorities_share_sixty_a_minute_and_the_balance_refresh_stops_first(self):
        now = 1_900_000_020
        acquire = self.State._llm_ap_rate_acquire
        for _ in range(nac.AP_PRIORITY_CEILINGS[nac.AP_PRIORITY_BALANCE]):
            self.assertIsNone(acquire(nac.AP_PRIORITY_BALANCE, now=now))
        retry = acquire(nac.AP_PRIORITY_BALANCE, now=now)
        self.assertEqual(retry, 60 - now % 60)
        for _ in range(nac.AP_PRIORITY_CEILINGS[nac.AP_PRIORITY_OTHER] - 30):
            self.assertIsNone(acquire(nac.AP_PRIORITY_OTHER, now=now))
        self.assertIsNotNone(acquire(nac.AP_PRIORITY_OTHER, now=now))
        for _ in range(nac.AP_RATE_PER_MINUTE - nac.AP_PRIORITY_CEILINGS[nac.AP_PRIORITY_OTHER]):
            self.assertIsNone(acquire(nac.AP_PRIORITY_CONVERSION, now=now))
        self.assertIsNotNone(acquire(nac.AP_PRIORITY_CONVERSION, now=now), '全平台 60 次／分')
        self.assertIsNone(acquire(nac.AP_PRIORITY_BALANCE, now=now + 60), '換一分鐘就歸零')

    def test_only_ap_calls_are_limited(self):
        self.env.cr.execute('UPDATE %s SET ap_rate_window = %%s, ap_rate_count = 60' % STATE_TABLE,
                            (int(datetime.now().timestamp()) // 60,))
        ap, ap_session = self._client(_Response(200, {'success': True, 'data': {}}),
                                      role=nac.ROLE_CRON)
        result = ap.ap_list_users(1)
        self.assertEqual(result.reason, 'ap_rate_limited')
        self.assertEqual(ap_session.sent, 0)
        user, user_session = self._client(_Response(200, {'success': True, 'data': {}}))
        self.assertTrue(user.user_self(TENANT_IP, UP).ok)
        self.assertEqual(self._state()['circuit_failures'], 0, '限速不算 circuit 失敗')


# ====================================================================== #
#  SWR（N26；AC2）                                                         #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_client_hardening')
class TestLlmSwr(_LlmCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        self.password = 'llm-swr-%s' % suffix
        self.user = new_test_user(
            self.env, login='llm_swr_%s' % suffix, password=self.password,
            groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        self.fake = FakeNewApi()
        self.adapter = LlmClientAdapter(self, self.fake, full_factory=True, real_circuit=True)
        partner_id = self.user.partner_id.id
        scope_claims(self, lambda env: env[ACCOUNT].sudo().search(
            [('partner_id', '=', partner_id)]).ids)
        self.env[ACCOUNT]._llm_activate(self.user.partner_id, TEST_TOS, None, '')
        self.env[ACCOUNT]._cron_llm_provision()
        self.env.invalidate_all()
        self.account = self.env[ACCOUNT]._llm_account_for_partner(self.user.partner_id)
        self.assertEqual(self.account.state, 'active')
        self.Cache = self.env[CACHE_MODEL].sudo()
        # 共用 DB：別的帳戶（與全平台價目）的待刷新列不在本測試的範圍。
        scope_swr_queue(self.env, [self.account.id])

    def _rpc(self, path, params=None):
        self.authenticate(self.user.login, self.password)
        response = self.url_open(path, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params or {}}),
            headers={'Content-Type': 'application/json'})
        return response.json()['result']

    def _row(self, kind):
        # helper 以獨立 cursor 的 SQL 寫：先丟掉 ORM 的快取再讀。
        self.env.invalidate_all()
        return self.Cache.search([('account_id', '=', self.account.id), ('kind', '=', kind)])

    def _age_cache(self):
        """讓本帳戶的快取看起來是 10 分鐘前讀、10 分鐘前試的（過期、也過了失敗後的等待）。"""
        self.env.cr.execute(
            "UPDATE %s SET fetched_at = fetched_at - interval '10 minutes', "
            "attempted_at = attempted_at - interval '10 minutes' WHERE account_id = %%s"
            % CACHE_TABLE, (self.account.id,))

    def _refresh(self):
        self.env.invalidate_all()
        self.env[CACHE_MODEL]._cron_llm_swr_refresh()
        self.env.invalidate_all()

    def test_usage_recent_reads_the_cache_and_the_cron_refreshes_it(self):
        user_id = self.fake.user_id_for_up(next(iter(self.fake.pats)))
        self.fake.add_log(user_id, 'wp_1_abcd')
        calls = len(self.fake.calls)
        first = self._rpc('/api/llm/usage/recent')
        self.assertTrue(first['success'], first)
        self.assertEqual((first['data']['items'], first['data']['data_as_of']), ([], None))
        self.assertEqual(len(self.fake.calls), calls, '端點不外呼（N26）')
        self.assertTrue(self._row('usage_recent').requested_at, '排了一次背景刷新')
        again = self._rpc('/api/llm/usage/recent')
        self.assertTrue(again['success'])
        self.assertEqual(len(self.Cache.search([('account_id', '=', self.account.id),
                                                 ('kind', '=', 'usage_recent')])), 1)

        roles = len(self.fake.role_calls)
        self._refresh()
        self.assertIn(('user_log_self', nac.ROLE_CRON), self.fake.role_calls[roles:],
                      '背景刷新用 CRON 金鑰、本帳戶的 UP')
        refreshed = self._rpc('/api/llm/usage/recent')['data']
        self.assertEqual(refreshed['today_count'], 1)
        self.assertEqual(len(refreshed['items']), 1)
        self.assertNotIn('channel', refreshed['items'][0])
        self.assertTrue(refreshed['data_as_of'])
        self.assertFalse(self._row('usage_recent').requested_at, '新鮮的快取不再排')

    def test_usage_summary_and_daily_are_cache_first_and_refresh_last_good(self):
        """Summary and chart series are served from SWR; failures retain the last good snapshot."""
        params = {'range': 'today'}
        created = self._rpc('/api/llm/keys/create', {
            'kind': 'free_trial', 'client_request_id': str(uuid.uuid4()),
        })
        self.assertTrue(created['success'], created)
        key_id = created['data']['key_id']
        self.env.invalidate_all()
        local_key = self.env['woow_paas_platform.llm_api_key'].sudo().browse(key_id)
        user_id = self.fake.user_id_for_up(next(iter(self.fake.pats)))
        token = next(token for token in self.fake.tokens.values()
                     if token['user_id'] == user_id and token['name'] == local_key.newapi_name)
        now = int(self.fake.clock())
        for log_id, model, quota, prompt, completion in (
                (7101, 'paid-a', 1, 4, 6), (7102, 'paid-b', 2, 8, 12)):
            self.fake.logs.append({
                'id': log_id, 'user_id': user_id, 'token_name': token['name'],
                'model_name': model, 'group': token.get('group') or 'free',
                'created_at': now, 'type': 2, 'prompt_tokens': prompt,
                'completion_tokens': completion, 'use_time': 1, 'quota': quota,
                'request_id': 'req_usage_%s' % log_id,
            })
        # A plausible upstream row from another user's log must not enter this account's flow.
        self.fake.logs.append({
            'id': 7199, 'user_id': user_id + 1, 'token_name': token['name'],
            'model_name': 'foreign-model', 'group': 'free', 'created_at': now,
            'type': 2, 'prompt_tokens': 999, 'completion_tokens': 999,
            'use_time': 1, 'quota': 999, 'request_id': 'req_usage_foreign',
        })
        first_summary = self._rpc('/api/llm/usage/summary', params)
        first_daily = self._rpc('/api/llm/usage/daily', params)
        self.assertTrue(first_summary['success'], first_summary)
        self.assertTrue(first_daily['success'], first_daily)
        self.assertFalse(first_summary['data']['complete'])
        self.assertIsNone(first_summary['data']['quota'],
                          'a cache miss is incomplete, not a fabricated zero')
        day_rows = self.Cache.search([('account_id', '=', self.account.id),
                                      ('kind', '=', 'usage_day')])
        self.assertEqual(len(day_rows), 7, 'summary batches today plus the recent seven-day window')
        self.assertEqual(len(set(day_rows.mapped('cache_key'))), 7,
                         'summary and daily enqueue each account/date at most once')
        before = len(self.fake.calls)
        summary = self._rpc('/api/llm/usage/summary', params)
        daily = self._rpc('/api/llm/usage/daily', params)
        self.assertTrue(summary['success'], summary)
        self.assertTrue(daily['success'], daily)
        self.assertEqual(len(self.fake.calls), before, 'usage HTTP endpoints are cache-first')

        self._refresh()
        last_good_summary = self._rpc('/api/llm/usage/summary', params)['data']
        last_good_daily = self._rpc('/api/llm/usage/daily', params)['data']
        self.assertTrue(last_good_summary['complete'])
        self.assertTrue(last_good_daily['complete'])
        self.assertEqual((last_good_summary['requests'], last_good_summary['quota'],
                          last_good_summary['tokens']), (2, 3, 30))
        self.assertAlmostEqual(last_good_summary['spend_points'], 3 / 15625)
        model_rows = {row['id']: row for row in last_good_daily['series']}
        self.assertEqual(set(model_rows), {'paid-a', 'paid-b'})
        self.assertEqual((model_rows['paid-a']['quota'][0], model_rows['paid-b']['quota'][0]),
                         (1, 2))
        self.assertAlmostEqual(sum(row['values'][0] for row in model_rows.values()), 3 / 15625)
        key_daily = self._rpc('/api/llm/usage/daily', {
            **params, 'group_by': 'key', 'metric': 'spend',
        })['data']
        self.assertEqual(len(key_daily['series']), 1)
        self.assertEqual(key_daily['series'][0]['id'], str(key_id))
        self.assertEqual(key_daily['series'][0]['quota'][0], 3)
        self.assertAlmostEqual(key_daily['series'][0]['values'][0], 3 / 15625)
        good_payloads = {row.cache_key: row.payload for row in day_rows}
        self._age_cache()
        self._rpc('/api/llm/usage/summary', params)
        self._rpc('/api/llm/usage/daily', params)
        self.fake.fail_before_all['user_data_flow_self'] = nac.Unknown('proxy_timeout')
        self._refresh()
        stale_summary = self._rpc('/api/llm/usage/summary', params)['data']
        stale_daily = self._rpc('/api/llm/usage/daily', params)['data']
        self.assertEqual((stale_summary['requests'], stale_summary['quota'],
                          stale_summary['tokens']), (2, 3, 30))
        for field in ('quota', 'spend_points', 'requests', 'tokens'):
            self.assertEqual(stale_summary[field], last_good_summary[field], field)
        self.assertEqual(stale_daily['series'], last_good_daily['series'])
        self.assertTrue(stale_summary['stale'])
        self.assertTrue(stale_daily['stale'])
        after_rows = self.Cache.search([('account_id', '=', self.account.id),
                                        ('kind', '=', 'usage_day')])
        self.assertEqual({row.cache_key: row.payload for row in after_rows}, good_payloads,
                         'failed multi-day refresh must not replace any last-good date payload')

    def test_usage_day_cache_isolated_by_taipei_date_and_boundary(self):
        today = datetime.now(ZoneInfo('Asia/Taipei')).date()
        yesterday = today - timedelta(days=1)
        old_payload = json.dumps({'day': yesterday.isoformat(), 'items': [{
            'day': yesterday.isoformat(), 'key_id': 999, 'key_name': 'yesterday-only',
            'key_kind': 'paid', 'model': 'paid-a', 'requests': 99,
            'quota': 99000, 'tokens': 999,
        }]})
        self.env.cr.execute(
            'INSERT INTO %s (account_id, kind, cache_key, payload, fetched_at, create_date, write_date) '
            "VALUES (%%s, 'usage_day', %%s, %%s, now() at time zone 'UTC', now(), now())" % CACHE_TABLE,
            (self.account.id, yesterday.isoformat(), old_payload))

        result = self._rpc('/api/llm/usage/daily', {'range': 'today'})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['dates'], [today.isoformat()])
        self.assertFalse(result['data']['complete'])
        self.assertEqual(result['data']['series'], [],
                         'yesterday snapshot cannot be reused to fill today')
        rows = self.Cache.search([('account_id', '=', self.account.id),
                                  ('kind', '=', 'usage_day')])
        self.assertEqual(set(rows.mapped('cache_key')), {yesterday.isoformat(), today.isoformat()})
        today_row = rows.filtered(lambda row: row.cache_key == today.isoformat())
        self.assertTrue(today_row.requested_at, 'today miss is independently queued')
        self.assertEqual(rows.filtered(lambda row: row.cache_key == yesterday.isoformat()).payload,
                         old_payload)

    def test_late_swr_result_cannot_overwrite_a_newer_claim(self):
        self._rpc('/api/llm/usage/recent')
        claimed = self.Cache._llm_swr_claim(1)
        self.assertEqual(len(claimed), 1)
        row_id, _account_id, _kind, _key, old_attempt = claimed[0]
        new_attempt = old_attempt + timedelta(seconds=31)
        self.env.cr.execute(
            'UPDATE %s SET attempted_at=%%s, payload=%%s WHERE id=%%s' % CACHE_TABLE,
            (new_attempt, json.dumps({'today_count': 8, 'items': []}), row_id))
        self.Cache._llm_swr_finish(
            row_id, {'today_count': 1, 'items': []}, 'ok', attempted_at=old_attempt)
        self.env.invalidate_all()
        row = self.Cache.browse(row_id)
        self.assertEqual(json.loads(row.payload)['today_count'], 8)
        self.Cache._llm_swr_requeue(row_id, attempted_at=old_attempt)
        self.env.invalidate_all()
        self.assertFalse(row.requested_at, 'expired worker cannot requeue a newer claim')
        self.Cache._llm_swr_finish(
            row_id, {'today_count': 9, 'items': []}, 'ok', attempted_at=new_attempt)
        self.env.invalidate_all()
        self.assertEqual(json.loads(row.payload)['today_count'], 9)

    def test_yesterdays_cache_is_not_shown_as_today(self):
        self.env.cr.execute(
            'INSERT INTO %s (account_id, kind, payload, fetched_at, create_date, write_date) '
            "VALUES (%%s, 'usage_recent', %%s, now() at time zone 'UTC', now(), now())"
            % CACHE_TABLE,
            (self.account.id, json.dumps({'day_tpe': '2001-01-01', 'today_count': 9, 'items': []})))
        data = self._rpc('/api/llm/usage/recent')['data']
        self.assertEqual((data['today_count'], data['data_as_of']), (None, None))
        self.assertTrue(self._row('usage_recent').requested_at)

    def test_new_api_down_keeps_the_old_data_and_does_not_hammer(self):
        self._rpc('/api/llm/usage/recent')
        self._refresh()
        good = self._rpc('/api/llm/usage/recent')['data']
        self._age_cache()
        self._rpc('/api/llm/usage/recent')
        self.fake.fail_before['user_log_self'] = nac.Unknown('proxy_timeout')
        self._refresh()
        row = self._row('usage_recent')
        self.assertEqual(row.last_result, 'unknown:proxy_timeout')
        stale = self._rpc('/api/llm/usage/recent')['data']
        self.assertEqual(stale['items'], good['items'], '舊資料照回、標著舊的資料時間')
        self.assertTrue(stale['data_as_of'])
        self.assertFalse(self._row('usage_recent').requested_at, '剛失敗過：30 秒內不再排')

    def test_state_refreshes_the_balance_only_when_stale(self):
        self._rpc('/api/llm/state')
        self.assertFalse(self._row('self'), 'P9 剛寫過餘額：還新鮮')
        self.env.cr.execute("UPDATE woow_paas_platform_llm_account SET balance_synced_at = "
                            "balance_synced_at - interval '11 minutes' WHERE id = %s",
                            (self.account.id,))
        user = next(iter(self.fake.users.values()))
        user['quota'] = 7 * 15625
        calls = len(self.fake.calls)
        state = self._rpc('/api/llm/state')['data']
        self.assertEqual(state['account']['balance_points'], 0, '這次先回舊的')
        self.assertEqual(len(self.fake.calls), calls)
        self._refresh()
        state = self._rpc('/api/llm/state')['data']
        self.assertEqual(state['account']['balance_points'], 7)

    def test_keys_list_refreshes_last_used(self):
        result = self._rpc('/api/llm/keys/create', {'client_request_id': str(uuid.uuid4()),
                                                     'kind': 'free_trial'})
        self.assertTrue(result['success'], result)
        key_id = result['data']['key_id']
        token = next(iter(self.fake.tokens.values()))
        token['accessed_time'] = 1_900_000_000
        token['used_quota'] = 0
        listed = self._rpc('/api/llm/keys/list')['data']
        self.assertIsNone(listed['data_as_of'])
        self._refresh()
        listed = self._rpc('/api/llm/keys/list')['data']
        self.assertTrue(listed['data_as_of'])
        row = next(item for item in listed['items'] if item['id'] == key_id)
        self.assertTrue(row['last_used_at'].startswith('2030-03-17'))

    def test_circuit_open_still_serves_the_cache_and_skips_the_refresh(self):
        self._rpc('/api/llm/usage/recent')
        self._refresh()
        self._age_cache()
        self._set_circuit(fields.Datetime.now() + timedelta(seconds=60))
        data = self._rpc('/api/llm/usage/recent')
        self.assertTrue(data['success'], 'SWR 端點照樣回快取')
        self.assertTrue(data['data']['data_as_of'])
        self.assertTrue(self._row('usage_recent').requested_at)
        calls = len(self.fake.calls)
        self._refresh()
        self.assertEqual(len(self.fake.calls), calls, 'circuit 開著：刷新 cron 跳過這一輪')
        self.assertTrue(self._row('usage_recent').requested_at, '還在排，關上之後再刷')

    def test_pricing_is_one_platform_row_refreshed_by_the_schedule(self):
        self.env.cr.execute("DELETE FROM %s WHERE account_id IS NULL" % CACHE_TABLE)
        self.Cache._cron_llm_pricing_refresh()
        self.Cache._cron_llm_pricing_refresh()
        row = self.Cache.search([('account_id', '=', False), ('kind', '=', 'pricing')])
        self.assertEqual(len(row), 1)
        self.assertEqual(row.last_result, 'ok')
        self.assertEqual(json.loads(row.payload)['items'][0]['model'], 'qwen3-4b-cluster1')
        self.assertIn(('ap_pricing', nac.ROLE_CRON), self.fake.role_calls)


    def test_models_before_activation_reads_only_cache(self):
        self.Cache._cron_llm_pricing_refresh()
        calls = len(self.fake.calls)
        # The route depends on the platform session, never an LLM account or UP.
        with patch.object(type(self.env[ACCOUNT]), '_llm_account_for_partner',
                          side_effect=AssertionError('models must not look up an account')):
            response = self._rpc('/api/llm/models')
        self.assertTrue(response['success'], response)
        self.assertEqual(response['data']['state'], 'ok')
        self.assertEqual(len(self.fake.calls), calls)
        self.assertNotIn('group_ratio', response['data'])

    def test_invalid_pricing_clears_old_catalogue_alerts_and_recovers(self):
        self.Cache._cron_llm_pricing_refresh()
        bad = nac.Ok(data={'data': [], 'group_ratio': {'default': 1.3, 'internal': 1}})
        with patch.object(self.fake, 'ap_pricing', return_value=bad):
            self.Cache._cron_llm_pricing_refresh()
        self.env.invalidate_all()
        row = self.Cache.search([('kind', '=', 'pricing'), ('account_id', '=', False)], limit=1)
        self.assertFalse(row.payload)
        self.assertFalse(row.fetched_at)
        self.assertEqual(self._rpc('/api/llm/models')['data']['items'], [])
        self.assertEqual(self._rpc('/api/llm/models')['data']['state'], 'maintenance')
        self.assertTrue(self._alerts('pricing_invalid', 'llm_newapi_invariant'))
        self.Cache._cron_llm_pricing_refresh()
        self.assertEqual(self._rpc('/api/llm/models')['data']['state'], 'ok')

    def test_pricing_expiry_outage_and_global_guard_never_serve_old_rates(self):
        self.Cache._cron_llm_pricing_refresh()
        self.State._llm_write(guard_tripped=True)
        self.assertEqual(self._rpc('/api/llm/models')['data']['items'], [])
        self.State._llm_write(guard_tripped=False)
        self.env.cr.execute("UPDATE %s SET fetched_at = fetched_at - interval '11 minutes' "
                            "WHERE kind = 'pricing'" % CACHE_TABLE)
        self.env.invalidate_all()
        self.assertEqual(self._rpc('/api/llm/models')['data']['state'], 'maintenance')
        self.Cache._cron_llm_pricing_refresh()
        with patch.object(self.fake, 'ap_pricing', return_value=nac.NotExecuted('connect')):
            self.Cache._cron_llm_pricing_refresh()
        self.assertEqual(self._rpc('/api/llm/models')['data']['items'], [])


# ====================================================================== #
#  設定守衛與變更稽核（DESIGN §2.11）                                        #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_client_hardening')
class TestLlmConfigChange(_LlmCase):

    def _run_postcommit(self):
        self.env.cr.postcommit.run()

    def test_public_base_url_allow_list(self):
        icp = self.env['ir.config_parameter'].sudo()
        for value in sorted(llm_common.ALLOWED_PUBLIC_BASE_URLS):
            icp.set_param(llm_common.PARAM_PUBLIC_BASE_URL, value)
            self.assertEqual(llm_common.llm_config(self.env)['public_base_url'], value)
        for bad in ('http://newapi.woowtech.io/v1', 'https://newapi.woowtech.io/v1/',
                    'https://evil.example/v1', 'https://newapi.woowtech.io.evil.example/v1',
                    'https://user:pw@newapi.woowtech.io/v1', 'https://newapi.woowtech.io/v1?x=1',
                    'javascript:alert(1)'):
            with self.assertRaises(ValidationError, msg=bad):
                icp.set_param(llm_common.PARAM_PUBLIC_BASE_URL, bad)
        self.env.cr.execute("UPDATE ir_config_parameter SET value = 'https://evil.example/v1' "
                            "WHERE key = %s", (llm_common.PARAM_PUBLIC_BASE_URL,))
        self.env.registry.clear_cache()
        self.assertIsNone(llm_common.llm_config(self.env)['public_base_url'], 'SQL 直改＝不顯示')

    def test_watched_keys_write_an_event_a_fingerprint_and_an_alert_after_commit(self):
        icp = self.env['ir.config_parameter'].sudo()
        self._run_postcommit()
        before = self._state()['last_config_fingerprint']
        events = self.env[EVENT].sudo().search_count([('action', '=', 'config_change')])
        icp.set_param(llm_common.PARAM_PROXY_URL, nac.PROXY_URL_PROD)
        icp.set_param(llm_common.PARAM_PUBLIC_BASE_URL, 'https://newapi-stg.woowtech.io/v1')
        icp.set_param(llm_common.PARAM_IP_BLOCK, '8')  # 不在監看清單
        self.assertEqual(self.env[EVENT].sudo().search_count([('action', '=', 'config_change')]),
                         events, 'commit 之前什麼都不寫')
        self._run_postcommit()
        changes = self.env[EVENT].sudo().search([('action', '=', 'config_change')],
                                                order='id desc', limit=2)
        self.assertEqual(set(changes.mapped('error_code')),
                         {'llm_config_proxy_url', 'llm_config_public_base_url'})
        self.assertTrue(all(c.target_model == 'ir.config_parameter' for c in changes))
        self.assertNotIn(nac.PROXY_URL_PROD, json.dumps(changes.read(['details_json'])),
                         '不記值')
        self.assertNotEqual(self._state()['last_config_fingerprint'], before)
        self.assertTrue(self._alerts('config_changed', 'llm_config_proxy_url'))

    def test_same_value_and_unrelated_keys_change_nothing(self):
        icp = self.env['ir.config_parameter'].sudo()
        self._run_postcommit()
        events = self.env[EVENT].sudo().search_count([('action', '=', 'config_change')])
        icp.set_param(llm_common.PARAM_PROXY_URL, nac.PROXY_URL_STG)  # 沒變
        icp.set_param(llm_common.PARAM_SHOW_LLM, 'True')
        self._run_postcommit()
        self.assertEqual(self.env[EVENT].sudo().search_count([('action', '=', 'config_change')]),
                         events)


# ====================================================================== #
#  後台〔測試連線〕                                                         #
# ====================================================================== #
@tagged('post_install', '-at_install', 'llm_client_hardening')
class TestLlmConnectionTest(_LlmCase):

    def setUp(self):
        super().setUp()
        self.fake = FakeNewApi()
        LlmClientAdapter(self, self.fake)

    def test_button_is_system_only_and_queues_the_cron(self):
        accountant = new_test_user(self.env, login='llm_conn_acc_%s' % uuid.uuid4().hex[:6],
                                   groups='account.group_account_manager')
        record = self.State.browse(self._state()['id'])
        with self.assertRaises(AccessError):
            record.with_user(accountant).action_llm_test_connection()
        action = record.with_user(self.env.ref('base.user_admin')).action_llm_test_connection()
        self.assertEqual(action['tag'], 'display_notification')
        self.assertTrue(self._state()['connection_test_requested_at'])

    def test_cron_runs_the_three_checks_and_stores_codes_only(self):
        record = self.State.browse(self._state()['id'])
        record.with_user(self.env.ref('base.user_admin')).action_llm_test_connection()
        readyz = {'reachable': True, 'ready': True, 'upstream': 'ok', 'replay_store': 'ok',
                  'ap_days_left': 170, 'op_days_left': None, 'break_glass': False}
        with patch.object(nac, 'fetch_readyz', return_value=readyz) as fetch:
            self.env[RUNTIME]._cron_llm_connection_test()
        fetch.assert_called_once_with(nac.PROXY_URL_STG)
        state = self._state()
        self.assertFalse(state['connection_test_requested_at'])
        result = json.loads(state['connection_test_result'])
        self.assertEqual(result['readyz'], readyz)
        self.assertEqual(result['ap_health'], 'ok:ok')
        self.assertEqual(result['status'], 'ok:ok')
        self.assertFalse(result['status_invariants_ok'], 'fake 的 /api/status 只有 quota_per_unit')
        self.assertTrue(result['status_invariants']['quota_per_unit'])
        self.assertEqual([name for name, _ip in self.fake.calls], ['ap_health', 'status'])
        # 沒有請求時第一步就 return。
        self.env[RUNTIME]._cron_llm_connection_test()
        self.assertEqual(len(self.fake.calls), 2)

    def test_readyz_helpers(self):
        self.assertEqual(nac.readyz_url(nac.PROXY_URL_STG),
                         'http://newapi-proxy.new-api-stg.svc.cluster.local:8081/readyz')
        session = SimpleNamespace(
            get=lambda *a, **kw: SimpleNamespace(status_code=503, content=json.dumps({
                'ready': False, 'upstream': 'connect', 'replay_store': 'ok',
                'ap_days_left': 3, 'secret': 'nap_' + 'x' * 40}).encode()),
            close=lambda: None)
        out = nac.fetch_readyz(nac.PROXY_URL_STG, session_factory=lambda: session)
        self.assertEqual(out, {'reachable': True, 'http_status': 503, 'ready': False,
                               'upstream': 'connect', 'replay_store': 'ok', 'ap_days_left': 3})
