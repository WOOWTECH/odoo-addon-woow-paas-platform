"""LLM-01b 的純 Python 單元測試：``src/services/{newapi_client,llm_crypto,llm_redact}.py``。

Run:  python3 tests/test_llm_services.py -v
CI:   .gitea/workflows/llm-services-unit-tests.yml（PR 時跑；只需要 requests 與 cryptography）。

這三支模組刻意不 import odoo，所以這裡在沒有 Odoo 的環境直接載入（照
``tests/test_shc_bare_create_transport.py`` 的做法）。和代理的契約用代理自己的參考實作
驗：``extra/newapi-proxy/newapi_proxy/{signing,policy,settings}.py`` 只用標準函式庫，
所以直接 import 來驗章、跑 body 政策——兩邊任一邊改了簽章字串或欄位，這裡就紅。
``TestClientMatchesProxyRouteTable`` 再拿代理的 ``routetable.py``／``canonical.py`` 逐一比對
**每一個** client 方法送出的請求：路由存在、身分在路由的集合內、query key 與值過得了路由的
validator、body／Authorization／proof 的有無、以及 ``WEB_METHODS`` 恰好等於 ``web=True`` 的
路由——Odoo 測試的 fake 換掉了整個 client，路由錯配只有這裡（和 stg）抓得到。

傳輸錯誤用真的 loopback socket（不是 mock）：連線被拒、送出後斷線、讀取逾時、DNS 失敗
都由 requests／urllib3 自己丟例外，再看分類對不對（DESIGN §3.4、〔REV〕24）。
"""
import importlib
import io
import json
import logging
import socket
import sys
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / 'src' / 'services'
PROXY_ROOT = ROOT / 'extra' / 'newapi-proxy'

# 合成套件：載入 src/services 底下的檔案，但不執行 services/__init__.py（它會 import
# 依賴 Odoo 的模組）。``from . import llm_redact`` 照樣解析得到。
_PKG = 'woow_llm_services_under_test'
_package = types.ModuleType(_PKG)
_package.__path__ = [str(SERVICES)]
sys.modules[_PKG] = _package
nac = importlib.import_module(_PKG + '.newapi_client')
llm_crypto = importlib.import_module(_PKG + '.llm_crypto')
llm_redact = importlib.import_module(_PKG + '.llm_redact')
mcp_url_guard = importlib.import_module(_PKG + '.mcp_url_guard')

sys.path.insert(0, str(PROXY_ROOT))
from newapi_proxy import canonical as proxy_canonical  # noqa: E402
from newapi_proxy import policy as proxy_policy  # noqa: E402
from newapi_proxy import routetable as proxy_routes  # noqa: E402
from newapi_proxy import settings as proxy_settings  # noqa: E402
from newapi_proxy import signing as proxy_signing  # noqa: E402

KID = 'kid-1'
SECRET = 'S' * 40
UP = 'nap_' + 'a1B2' * 10 + 'xyz'
JWT = 'eyJabcd.eyJefgh1234.sig_-abcd'
TENANT_IP = '198.18.17.9'


# --------------------------------------------------------------------------- #
#  loopback 邊界                                                                 #
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self):
        self.requests = []
        self.mode = 'respond'
        self.status = 200
        self.body = b'{"success":true,"message":"","data":{"id":7}}'
        self.headers = {'Content-Type': 'application/json', 'X-Oneapi-Request-Id': 'rid-123'}
        self.delay = 0


def _serve(recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def _handle(self):
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(length) if length else b''
            recorder.requests.append({
                'method': self.command, 'path': self.path,
                'headers': {k: v for k, v in self.headers.items()}, 'body': body})
            if recorder.mode == 'disconnect':
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if recorder.delay:
                time.sleep(recorder.delay)
            self.send_response(recorder.status)
            for name, value in recorder.headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(recorder.body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(recorder.body)

        do_GET = do_POST = do_DELETE = do_PUT = _handle

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    server.handle_error = lambda request, client_address: None  # 斷線測試的預期雜訊
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class _Base(unittest.TestCase):
    def setUp(self):
        self.recorder = _Recorder()
        self.server = _serve(self.recorder)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def client(self, **kw):
        return nac.NewApiClient(self.base, KID, SECRET.encode(), nac.ROLE_CRON, **kw)


# --------------------------------------------------------------------------- #
#  簽章與標頭                                                                    #
# --------------------------------------------------------------------------- #
class TestSigningMatchesProxy(_Base):

    def _verify(self, request, identity, client_ip=''):
        path, _, query = request['path'].partition('?')
        h = request['headers']
        message = proxy_signing.string_to_sign(
            request['method'], path, query, request['body'], h['X-Woow-Proxy-Ts'],
            h['X-Woow-Proxy-Nonce'], h['X-Woow-Proxy-Kid'], identity, client_ip)
        return proxy_signing.signature_matches(SECRET.encode(), message, h['X-Woow-Proxy-Sig'])

    def test_every_identity_verifies_with_the_proxy_reference(self):
        client = self.client()
        self.assertTrue(client.status().ok)
        client.ap_search_user('wsab0000000042')
        client.user_login(TENANT_IP, 'wsab0000000042', 'p' * 64)
        client.user_log_self(TENANT_IP, UP, {'type': 2, 'group': 'free',
                                             'start_timestamp': 1, 'p': 1, 'page_size': 10})
        anon, ap, login, log = self.recorder.requests
        self.assertTrue(self._verify(anon, 'anon'))
        self.assertTrue(self._verify(ap, 'ap'))
        self.assertEqual(ap['path'], '/api/user/search?keyword=wsab0000000042&p=1&page_size=10')
        self.assertTrue(self._verify(login, 'user', TENANT_IP))
        self.assertEqual(login['headers']['X-Woow-Client-IP'], TENANT_IP)
        self.assertNotIn('Authorization', login['headers'], 'login 不帶 Authorization')
        self.assertTrue(self._verify(log, 'user', TENANT_IP))
        self.assertEqual(log['path'], '/api/log/self?group=free&p=1&page_size=10'
                                      '&start_timestamp=1&type=2', 'query 依 key 排序')
        self.assertEqual(log['headers']['Authorization'], 'Bearer ' + UP)
        for request in (anon, ap):
            self.assertNotIn('X-Woow-Client-IP', request['headers'])

    def test_nonce_is_fresh_for_every_request_and_never_the_key(self):
        client = self.client()
        for _ in range(3):
            client.status()
        nonces = [r['headers']['X-Woow-Proxy-Nonce'] for r in self.recorder.requests]
        self.assertEqual(len(set(nonces)), 3)
        for request in self.recorder.requests:
            self.assertRegex(request['headers']['X-Woow-Proxy-Nonce'], r'^[A-Za-z0-9_-]{22,64}$')
            self.assertNotIn(SECRET, json.dumps(request['headers']))
            self.assertEqual(request['headers']['Connection'], 'close')

    def test_read_timeout_header_and_deadline(self):
        client = self.client()
        client.user_self(TENANT_IP, UP, timeout=(2, 3))
        self.assertEqual(self.recorder.requests[-1]['headers']['X-Woow-Read-Timeout'], '2')
        result = client.status(deadline=time.monotonic() + 0.5)
        self.assertIsInstance(result, nac.NotExecuted)
        self.assertEqual(result.reason, 'deadline')
        self.assertEqual(len(self.recorder.requests), 1, '剩餘預算不夠就不送')

    def test_fresh_session_per_call(self):
        sessions = []

        def factory():
            session = nac.NewApiClient._new_session()
            sessions.append(session)
            return session
        client = self.client(session_factory=factory)
        client.status()
        client.status()
        self.assertEqual(len(sessions), 2)
        self.assertFalse(sessions[0].trust_env)
        adapter = sessions[0].get_adapter('http://x')
        self.assertEqual(adapter.max_retries.total, 0)

    def test_bodies_pass_the_proxy_body_policy(self):
        settings, _warnings = proxy_settings.load_settings({
            'PROXY_AUTH_CRON': '%s:%s' % (KID, SECRET),
            'PROXY_USERNAME_REGEX': r'^ws[a-z]{2}\d{10}$',
            'PROXY_TENANT_IP_RANGE': '198.18.17.0-198.19.255.255',
        }, int(time.time()))
        ctx = proxy_policy.PolicyContext(settings, int(time.time()))
        client = self.client()
        now = int(time.time())
        expires = now + nac.UP_LIFETIME_SECONDS
        client.ap_create_user('wsab0000000042', 'P' * 64, 'PaaS 42')
        client.user_login(TENANT_IP, 'wsab0000000042', 'P' * 64)
        client.user_verify_generate(TENANT_IP, JWT, 'P' * 64, expires)
        client.user_create_access_token(TENANT_IP, JWT, JWT, nac.up_name(42, now), expires)
        client.user_create_token(TENANT_IP, UP, nac.free_trial_token_body('wp_17_ab12', expires))
        bodies = [json.loads(r['body']) for r in self.recorder.requests]
        policies = (proxy_policy.user_create, proxy_policy.login, proxy_policy.verify,
                    proxy_policy.access_token_create, proxy_policy.token_create)
        for body, rule in zip(bodies, policies):
            self.assertEqual(rule(body, ctx), body, rule.__name__)
        self.assertEqual(self.recorder.requests[3]['headers']['X-Security-Proof'], JWT)


# --------------------------------------------------------------------------- #
#  與代理 routetable 的契約（finding 12）                                          #
# --------------------------------------------------------------------------- #
def _representative_calls(now):
    """每個 client 公開方法一次代表性呼叫（參數合法、形狀與正式程式碼一致）。"""
    expires = now + nac.UP_LIFETIME_SECONDS
    return {
        'status': lambda c: c.status(),
        'ap_create_user': lambda c: c.ap_create_user('wsab0000000042', 'P' * 64, 'PaaS 42'),
        'ap_search_user': lambda c: c.ap_search_user('wsab0000000042'),
        'ap_get_user': lambda c: c.ap_get_user(42),
        # 餘額刷新（llm_account._cron_llm_balance_refresh，LLM-02b）。
        'ap_list_users': lambda c: c.ap_list_users(3, 100),
        'user_login': lambda c: c.user_login(TENANT_IP, 'wsab0000000042', 'P' * 64),
        'user_list_access_tokens': lambda c: c.user_list_access_tokens(TENANT_IP, JWT),
        'user_verify_generate': lambda c: c.user_verify_generate(TENANT_IP, JWT, 'P' * 64,
                                                                 expires),
        'user_create_access_token': lambda c: c.user_create_access_token(
            TENANT_IP, JWT, JWT, nac.up_name(42, now), expires),
        'user_delete_session': lambda c: c.user_delete_session(TENANT_IP, JWT, 'sid_AB-12'),
        'user_self': lambda c: c.user_self(TENANT_IP, UP),
        'user_create_token': lambda c: c.user_create_token(
            TENANT_IP, UP, nac.free_trial_token_body('wp_17_ab12', expires)),
        'user_list_tokens': lambda c: c.user_list_tokens(TENANT_IP, UP, 1, 10),
        'user_get_token': lambda c: c.user_get_token(TENANT_IP, UP, 5),
        'user_reveal_token': lambda c: c.user_reveal_token(TENANT_IP, UP, 5),
        # usage/recent 送的 query（llm_account._llm_usage_recent）＋其餘 log 篩選鍵。
        'user_log_self': lambda c: c.user_log_self(TENANT_IP, UP, {
            'type': 2, 'group': 'free', 'start_timestamp': now - 60, 'end_timestamp': now,
            'p': 1, 'page_size': 10, 'model_name': 'qwen3-4b-cluster1',
            'token_name': 'wp_17_ab12'}),
        # LLM-02：白名單的其餘路由。
        'op_options': lambda c: c.op_options(),
        'ap_add_quota': lambda c: c.ap_add_quota(42, 50 * nac.QUOTA_PER_POINT),
        # 免費試用計數（LLM-10）與對帳（LLM-12b）的 admin log 查詢。
        'ap_log': lambda c: c.ap_log({'type': 2, 'group': 'free', 'start_timestamp': now - 300,
                                      'end_timestamp': now, 'p': 1, 'page_size': 100,
                                      'username': 'wsab0000000042'}),
        'ap_log_stat': lambda c: c.ap_log_stat({'type': 2, 'start_timestamp': now - 3600,
                                                'end_timestamp': now,
                                                'model_name': 'qwen3-4b-cluster1'}),
        'ap_data': lambda c: c.ap_data(now - 3600, now, 'wsab0000000042'),
        'ap_data_users': lambda c: c.ap_data_users(now - 3600, now),
        'ap_data_flow': lambda c: c.ap_data_flow(now - 3600, now),
        'ap_verify_methods': lambda c: c.ap_verify_methods('access_token.generate'),
        'ap_status': lambda c: c.ap_status(),
        'ap_pricing': lambda c: c.ap_pricing(),
        'ap_health': lambda c: c.ap_health('wsab'),
        'bg_set_user_group': lambda c: c.bg_set_user_group(42, 'free_blocked'),
        'bg_subtract_quota': lambda c: c.bg_subtract_quota(42, 3 * nac.QUOTA_PER_POINT),
        'user_verify_revoke': lambda c: c.user_verify_revoke(TENANT_IP, JWT, 'P' * 64, 9),
        'user_delete_access_token': lambda c: c.user_delete_access_token(TENANT_IP, JWT, JWT, 9),
        'user_audit_self': lambda c: c.user_audit_self(TENANT_IP, UP),
        'user_pricing': lambda c: c.user_pricing(TENANT_IP, UP),
        'user_status': lambda c: c.user_status(TENANT_IP, UP),
        'user_models': lambda c: c.user_models(TENANT_IP, UP),
        'user_groups': lambda c: c.user_groups(TENANT_IP, UP),
        'user_update_token': lambda c: c.user_update_token(
            TENANT_IP, UP, {**nac.free_trial_token_body('wp_17_ab12', expires), 'id': 5}),
        'user_set_token_status': lambda c: c.user_set_token_status(TENANT_IP, UP, 5, False),
        'user_delete_token': lambda c: c.user_delete_token(TENANT_IP, UP, 5),
        'user_delete_tokens': lambda c: c.user_delete_tokens(TENANT_IP, UP, [5, 6]),
        'user_log_self_stat': lambda c: c.user_log_self_stat(TENANT_IP, UP, {
            'type': 2, 'start_timestamp': now - 86400, 'end_timestamp': now}),
        'user_data_self': lambda c: c.user_data_self(TENANT_IP, UP, now - 86400, now),
        'user_data_flow_self': lambda c: c.user_data_flow_self(TENANT_IP, UP, now - 86400, now),
    }


def _public_client_methods():
    return {name for name in dir(nac.NewApiClient)
            if not name.startswith('_') and callable(getattr(nac.NewApiClient, name))}


class TestClientMatchesProxyRouteTable(_Base):

    def setUp(self):
        super().setUp()
        self.settings, _warnings = proxy_settings.load_settings({
            'PROXY_AUTH_CRON': '%s:%s' % (KID, SECRET),
            'PROXY_USERNAME_REGEX': r'^ws[a-z]{2}\d{10}$',
            'PROXY_TENANT_IP_RANGE': '198.18.17.0-198.19.255.255',
        }, int(time.time()))

    def test_every_public_method_is_covered(self):
        self.assertEqual(set(_representative_calls(int(time.time()))), _public_client_methods(),
                         '新增或改名 client 方法 ⇒ 在 _representative_calls 補一筆、重跑契約')
        self.assertLessEqual(nac.WEB_METHODS, _public_client_methods())

    def test_every_request_is_on_the_proxy_whitelist(self):
        now = int(time.time())
        client = self.client()
        for name, call in sorted(_representative_calls(now).items()):
            before = len(self.recorder.requests)
            self.assertTrue(call(client).ok, name)
            self.assertEqual(len(self.recorder.requests), before + 1, name)
            request = self.recorder.requests[-1]
            raw_path, _, raw_query = request['path'].partition('?')
            path = proxy_canonical.check_raw_path(raw_path.encode())
            pairs = proxy_canonical.parse_query(raw_query.encode())
            matched = proxy_routes.match(request['method'], path)
            self.assertIsNotNone(matched, '%s：%s %s 不在代理白名單' % (name, request['method'], path))
            route = matched.route
            headers = request['headers']
            identity = headers['X-Woow-Identity']
            self.assertIn(identity, route.identities, name)
            ctx = proxy_policy.PolicyContext(self.settings, now, identity=identity,
                                             query=dict(pairs))
            for key, value in pairs:
                spec = route.query.get(key)
                self.assertIsNotNone(spec, '%s：query key %s 代理不收' % (name, key))
                pattern = self.settings.username_re if spec == proxy_routes.USERNAME else spec
                self.assertTrue(pattern.fullmatch(value), '%s：%s=%s 過不了代理' % (name, key, value))
            if route.body == 'none':
                self.assertEqual(request['body'], b'', name)
            else:
                body = json.loads(request['body'])
                self.assertIsInstance(body, dict, name)
                if route.body_policy is not None:
                    self.assertEqual(route.body_policy(body, ctx), body, name)
            if identity == 'user':
                if route.user_auth == 'forbidden':
                    self.assertNotIn('Authorization', headers, name)
                else:
                    self.assertIn('Authorization', headers, name)
            else:
                self.assertNotIn('Authorization', headers, '%s：ap／anon 不帶憑證' % name)
            self.assertEqual('X-Security-Proof' in headers, route.proof, name)
            self.assertEqual(name in nac.WEB_METHODS, route.web,
                             '%s：WEB_METHODS 與代理 routetable 的 web 旗標不一致' % name)

    def test_every_proxy_route_has_a_client_method(self):
        """反方向：代理白名單的每條路由（含每個身分）都有 client 方法會送到它。"""
        now = int(time.time())
        client = self.client()
        covered = set()
        for call in _representative_calls(now).values():
            call(client)
            request = self.recorder.requests[-1]
            path = request['path'].partition('?')[0]
            matched = proxy_routes.match(request['method'], path)
            covered.add((matched.route.name, request['headers']['X-Woow-Identity']))
        expected = {(route.name, identity) for route in proxy_routes.ROUTES
                    for identity in route.identities}
        self.assertEqual(expected - covered, set(), '代理有路由、client 沒有方法送到它')

    def test_web_client_refuses_cron_routes_before_sending(self):
        now = int(time.time())
        web = nac.NewApiClient(self.base, KID, SECRET.encode(), nac.ROLE_WEB)
        sent = set()
        for name, call in sorted(_representative_calls(now).items()):
            before = len(self.recorder.requests)
            try:
                call(web)
            except nac.NewApiInputError as exc:
                self.assertEqual(exc.code, 'web_key_scope', name)
                self.assertEqual(len(self.recorder.requests), before, '%s：擋下的不能送出' % name)
                continue
            sent.add(name)
        self.assertEqual(sent, set(nac.WEB_METHODS))

    def test_list_users_rejects_bad_paging_before_sending(self):
        client = self.client()
        for page, size in ((0, 100), (1, 0), (1, 101), ('1', 100), (True, 100), (1, 1.5)):
            with self.subTest(page=page, size=size):
                with self.assertRaises(nac.NewApiInputError):
                    client.ap_list_users(page, size)
        self.assertEqual(self.recorder.requests, [])
        self.assertTrue(client.ap_list_users(1).ok)
        raw_query = self.recorder.requests[-1]['path'].partition('?')[2]
        self.assertEqual(raw_query, 'p=1&page_size=100&sort_by=id&sort_order=asc')

    def test_signing_key_for_role_never_falls_back(self):
        environ = {nac.ENV_AUTH_CRON: '%s:%s' % (KID, SECRET)}
        self.assertEqual(nac.signing_key_for_role(nac.ROLE_CRON, environ), (KID, SECRET.encode()))
        with self.assertRaises(nac.NewApiConfigError) as caught:
            nac.signing_key_for_role(nac.ROLE_WEB, environ)
        self.assertEqual(caught.exception.code, 'signing_key_missing')
        with self.assertRaises(nac.NewApiConfigError):
            nac.signing_key_for_role('admin', environ)

    def test_read_timeout_header_is_always_inside_the_proxy_range(self):
        """X-Woow-Read-Timeout＝read − 餘裕、夾在代理接受的 1–30，而且一定比 Odoo 的 read 短。"""
        client = self.client()
        for name in dir(nac):
            if not name.startswith('TIMEOUT_'):
                continue
            connect, read = getattr(nac, name)
            client.user_self(TENANT_IP, UP, timeout=(connect, read))
            header = int(self.recorder.requests[-1]['headers']['X-Woow-Read-Timeout'])
            self.assertTrue(1 <= header <= nac.PROXY_READ_TIMEOUT_MAX, name)
            self.assertLess(header, read, '%s：代理的期限要比 Odoo 的 read 早到' % name)


# --------------------------------------------------------------------------- #
#  分類（DESIGN §3.4）                                                           #
# --------------------------------------------------------------------------- #
class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def _classify(status, body, headers=None, write=True):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return nac.classify_response(status, _Headers(headers or {}), raw, write)


class TestResponseClassification(unittest.TestCase):

    def test_success(self):
        result = _classify(200, {'success': True, 'message': '', 'data': {'id': 3}},
                           {'X-Oneapi-Request-Id': 'abc'})
        self.assertIsInstance(result, nac.Ok)
        self.assertEqual((result.data, result.request_id), ({'id': 3}, 'abc'))

    def test_message_key_is_definite(self):
        result = _classify(200, {'success': False, 'message': 'x',
                                 'message_key': nac.MSG_LOGIN_FAILED})
        self.assertIsInstance(result, nac.Definite)
        self.assertEqual(result.message_key, nac.MSG_LOGIN_FAILED)

    def test_code_wins_over_message_key(self):
        for status in (200, 400, 401, 403, 409, 429, 500):
            result = _classify(status, {'success': False, 'code': 'AUTH_UNAUTHORIZED',
                                        'message_key': 'x'})
            self.assertIsInstance(result, nac.Definite, status)
            self.assertEqual(result.code, 'AUTH_UNAUTHORIZED')

    def test_success_false_without_message_key(self):
        self.assertIsInstance(_classify(200, {'success': False, 'message': 'db'}, write=True),
                              nac.Unknown)
        read = _classify(200, {'success': False, 'message': 'record not found'}, write=False)
        self.assertIsInstance(read, nac.Definite)
        self.assertEqual(read.reason, 'no_message_key')

    def test_empty_429_and_500_are_not_executed(self):
        limited = _classify(429, b'', {'Retry-After': '17'})
        self.assertIsInstance(limited, nac.NotExecuted)
        self.assertEqual(limited.retry_after, 17)
        self.assertIsInstance(_classify(500, b''), nac.NotExecuted)

    def test_panic_and_non_json_are_unknown(self):
        self.assertIsInstance(_classify(500, {'error': {'type': 'new_api_panic'}}), nac.Unknown)
        self.assertIsInstance(_classify(200, b'<html>'), nac.Unknown)
        self.assertIsInstance(_classify(200, b'[1,2]'), nac.Unknown)

    def test_proxy_error_classes(self):
        cases = {
            'connect': nac.NotExecuted, 'busy': nac.NotExecuted, 'budget': nac.NotExecuted,
            'body_timeout': nac.NotExecuted, 'config': nac.NotExecuted,
            'reset': nac.Unknown, 'timeout': nac.Unknown, 'internal': nac.Unknown,
            'auth': nac.Definite, 'policy': nac.Definite, 'bad_request': nac.Definite,
            'too_large': nac.Definite, 'denied': nac.Definite, 'weird': nac.Unknown,
        }
        for proxy_error, expected in cases.items():
            body = {'success': False, 'message': 'newapi-proxy: %s' % proxy_error,
                    'proxy_error': proxy_error, 'reason': 'reveal_window'}
            result = _classify(502, body, {'X-Woow-Proxy-Error': proxy_error})
            self.assertIsInstance(result, expected, proxy_error)
        denied = _classify(403, {'reason': 'reveal_window'}, {'X-Woow-Proxy-Error': 'denied'})
        self.assertFalse(denied.circuit, 'denied 是預期內的業務結果，不開 circuit')
        self.assertEqual(denied.reason, 'reveal_window')
        self.assertTrue(_classify(401, {}, {'X-Woow-Proxy-Error': 'auth'}).circuit)

    def test_precheck_relay_is_classified_as_new_api_answer(self):
        result = _classify(401, {'success': False, 'code': 'ACCESS_TOKEN_EXPIRED'},
                           {'X-Woow-Proxy-Precheck': 'reveal'})
        self.assertIsInstance(result, nac.Definite)
        self.assertEqual((result.code, result.precheck, result.proxy_error),
                         ('ACCESS_TOKEN_EXPIRED', 'reveal', None))

    def test_garbage_codes_do_not_reach_logs(self):
        result = _classify(400, {'code': 'nap_secretvalue\nInjected'})
        self.assertEqual(result.code, 'UNRECOGNISED')


class TestTransportClassification(_Base):

    def test_connection_refused_is_not_executed(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        client = nac.NewApiClient('http://127.0.0.1:%d' % port, KID, SECRET.encode(),
                                  nac.ROLE_CRON)
        result = client.ap_create_user('wsab0000000042', 'P' * 64, 'PaaS 42')
        self.assertIsInstance(result, nac.NotExecuted)
        self.assertEqual(result.reason, 'connect')

    def test_dns_failure_is_not_executed(self):
        def fail(*args, **kwargs):
            raise socket.gaierror(-2, 'Name or service not known')
        client = nac.NewApiClient('http://newapi-proxy.example', KID, SECRET.encode(),
                                  nac.ROLE_CRON)
        with patch('socket.getaddrinfo', side_effect=fail):
            result = client.ap_create_user('wsab0000000042', 'P' * 64, 'PaaS 42')
        self.assertIsInstance(result, nac.NotExecuted)

    def test_disconnect_after_send_is_unknown_for_writes_and_reads(self):
        self.recorder.mode = 'disconnect'
        client = self.client()
        write = client.ap_create_user('wsab0000000042', 'P' * 64, 'PaaS 42')
        self.assertIsInstance(write, nac.Unknown)
        read = client.user_self(TENANT_IP, UP)
        self.assertIsInstance(read, nac.Unknown)
        self.assertEqual(len(self.recorder.requests), 2, '已送出（伺服器讀到了請求）')

    def test_read_timeout_is_unknown(self):
        self.recorder.delay = 1.5
        client = self.client()
        result = client.ap_create_user('wsab0000000042', 'P' * 64, 'PaaS 42', timeout=(1, 1))
        self.assertIsInstance(result, nac.Unknown)

    def test_connect_timeout_is_not_executed(self):
        import requests
        exc = requests.exceptions.ConnectTimeout('timed out')
        self.assertIsInstance(nac.classify_transport_error(exc), nac.NotExecuted)


# --------------------------------------------------------------------------- #
#  NA-03 錄製的 fixture（src/tests/fixtures/newapi/，LLM-02）                       #
# --------------------------------------------------------------------------- #
FIXTURES = ROOT / 'src' / 'tests' / 'fixtures' / 'newapi'

#: fixture → (分類, label)：DESIGN §3.4 的分類表逐列（寫入呼叫）。
FIXTURE_EXPECTED = {
    'newapi_500_empty_valkey_down': (nac.NotExecuted, 'upstream_unavailable'),
    'newapi_500_panic': (nac.Unknown, 'new_api_panic'),
    'proxy_auth': (nac.Definite, 'proxy_auth'),
    'proxy_bad_request_authorization_not_allowed': (nac.Definite, 'proxy_bad_request'),
    'proxy_body_timeout': (nac.NotExecuted, 'proxy_body_timeout'),
    'proxy_budget': (nac.NotExecuted, 'proxy_budget'),
    'proxy_busy_inflight': (nac.NotExecuted, 'proxy_busy'),
    'proxy_busy_replay_store': (nac.NotExecuted, 'proxy_busy'),
    'proxy_config': (nac.NotExecuted, 'proxy_config'),
    'proxy_connect': (nac.NotExecuted, 'proxy_connect'),
    'proxy_connect_deadline': (nac.NotExecuted, 'proxy_connect'),
    'proxy_denied_manage_not_found': (nac.Definite, 'proxy_denied'),
    'proxy_denied_manage_target': (nac.Definite, 'proxy_denied'),
    'proxy_denied_reveal_name': (nac.Definite, 'proxy_denied'),
    'proxy_denied_reveal_not_found': (nac.Definite, 'proxy_denied'),
    'proxy_denied_reveal_token_mismatch': (nac.Definite, 'proxy_denied'),
    'proxy_denied_reveal_window': (nac.Definite, 'proxy_denied'),
    'proxy_internal': (nac.Unknown, 'proxy_internal'),
    'proxy_policy_manage_mode': (nac.Definite, 'proxy_policy'),
    'proxy_policy_route': (nac.Definite, 'proxy_policy'),
    'proxy_reset': (nac.Unknown, 'proxy_reset'),
    'proxy_timeout': (nac.Unknown, 'proxy_timeout'),
    'proxy_too_large': (nac.Definite, 'proxy_too_large'),
    'token_enable_exhausted_error': (nac.Definite, 'message_key'),
    'token_enable_expired_error': (nac.Definite, 'message_key'),
}
#: 傳輸層的 fixture（沒有 body）：由 loopback 伺服器重演。
TRANSPORT_FIXTURES = {'transport_send_then_drop_manage'}


def _load_fixture(name):
    return json.loads((FIXTURES / ('%s.json' % name)).read_text(encoding='utf-8'))


def _fixture_response(fixture):
    body = b'' if fixture.get('_raw_empty') or fixture.get('body') is None \
        else json.dumps(fixture['body']).encode()
    return fixture['_http_status'], _Headers(fixture.get('_headers') or {}), body


class TestRecordedFixtureClassification(_Base):
    """每個 NA-03 fixture 都依 DESIGN §3.4 分類；代理的錯誤 class 與 circuit 的影響一併對。"""

    def test_every_fixture_has_an_expectation(self):
        names = {path.stem for path in FIXTURES.glob('*.json')}
        self.assertEqual(names, set(FIXTURE_EXPECTED) | TRANSPORT_FIXTURES,
                         'fixture 重新同步後多了或少了檔：在 FIXTURE_EXPECTED 補上它的分類')

    def test_http_fixtures(self):
        for name, (kind, label) in sorted(FIXTURE_EXPECTED.items()):
            with self.subTest(fixture=name):
                fixture = _load_fixture(name)
                status, headers, body = _fixture_response(fixture)
                result = nac.classify_response(status, headers, body, write=True)
                self.assertIsInstance(result, kind)
                self.assertEqual(result.label(), label)
                self.assertNotIn('<redacted>', repr(result))
                reason = (fixture.get('body') or {}).get('reason') if isinstance(
                    fixture.get('body'), dict) else None
                if isinstance(result, nac.Definite) and result.proxy_error:
                    self.assertEqual(result.reason, reason)

    def test_fixture_circuit_effects(self):
        """denied 不開 circuit；簽章／政策錯一次就開；busy／connect／reset 算連續失敗；budget 不計。"""
        expected = {
            'proxy_denied_reveal_window': (None, None),
            'proxy_auth': ('trip', 'llm_proxy_auth'),
            'proxy_policy_route': ('trip', 'llm_proxy_policy'),
            'proxy_busy_inflight': ('failure', nac.CIRCUIT_FAILURES_CODE),
            'proxy_connect': ('failure', nac.CIRCUIT_FAILURES_CODE),
            'proxy_reset': ('failure', nac.CIRCUIT_FAILURES_CODE),
            'proxy_budget': (None, None),
            'newapi_500_empty_valkey_down': ('failure', nac.CIRCUIT_FAILURES_CODE),
            'token_enable_expired_error': ('success', None),
        }
        for name, effect in expected.items():
            with self.subTest(fixture=name):
                status, headers, body = _fixture_response(_load_fixture(name))
                result = nac.classify_response(status, headers, body, write=True)
                self.assertEqual(nac.circuit_effect('user', result), effect)

    def test_send_then_drop_add_quota_is_unknown_and_never_resent(self):
        """X09：代理把 add_quota 轉給 new-api 之後、回應之前 pod 被殺 ⇒ Unknown，不重送。"""
        fixture = _load_fixture('transport_send_then_drop_manage')
        self.assertEqual((fixture['_transport'], fixture['after_send']), ('RemoteDisconnected', True))
        self.recorder.mode = 'disconnect'
        sessions = []

        def factory():
            session = nac.NewApiClient._new_session()
            sessions.append(session)
            return session
        client = self.client(session_factory=factory)
        result = client.ap_add_quota(42, 50 * nac.QUOTA_PER_POINT)
        self.assertIsInstance(result, nac.Unknown)
        self.assertEqual(result.reason, 'transport')
        self.assertEqual(len(self.recorder.requests), 1, '寫入只送一次（client 不重試）')
        request = self.recorder.requests[0]
        self.assertEqual(request['headers']['Connection'], 'close')
        self.assertEqual(json.loads(request['body']),
                         {'id': 42, 'action': 'add_quota', 'mode': 'add',
                          'value': 50 * nac.QUOTA_PER_POINT})
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].get_adapter('http://x').max_retries.total, 0)
        self.assertEqual(nac.circuit_effect('ap', result), ('failure', nac.CIRCUIT_FAILURES_CODE))

    def test_rows_not_recorded_by_na03(self):
        """§3.4 其餘幾列（NA-03 沒錄到）：依表自己組。"""
        cases = [
            ((200, {'success': True, 'data': {}}, {}), True, nac.Ok, 'ok'),
            ((401, {'success': False, 'code': 'AUTH_UNAUTHORIZED'}, {}), True, nac.Definite,
             'AUTH_UNAUTHORIZED'),
            ((200, {'success': False, 'message': 'database error'}, {}), True, nac.Unknown,
             'no_message_key'),
            ((200, {'success': False, 'message': 'record not found'}, {}), False, nac.Definite,
             'no_message_key'),
            ((429, b'', {'Retry-After': '30'}), True, nac.NotExecuted, 'rate_limited'),
            ((401, {'success': False, 'code': 'ACCESS_TOKEN_EXPIRED'},
              {'X-Woow-Proxy-Precheck': 'manage'}), True, nac.Definite, 'ACCESS_TOKEN_EXPIRED'),
            ((502, b'<html>bad gateway</html>', {}), True, nac.Unknown, 'non_json'),
        ]
        for (status, body, headers), write, kind, label in cases:
            with self.subTest(status=status, label=label):
                result = _classify(status, body, headers, write=write)
                self.assertIsInstance(result, kind)
                self.assertEqual(result.label(), label)


# --------------------------------------------------------------------------- #
#  circuit breaker 與 AP 限速的鉤子（DESIGN §1.5、§3.6）                            #
# --------------------------------------------------------------------------- #
class _RecordingGuard(nac.CallGuard):
    def __init__(self, block=None, raise_before=False, raise_after=False):
        self.block = block
        self.raise_before = raise_before
        self.raise_after = raise_after
        self.before_calls = []
        self.after_calls = []

    def before(self, identity, priority):
        self.before_calls.append((identity, priority))
        if self.raise_before:
            raise RuntimeError('db down ' + SECRET)
        return self.block

    def after(self, identity, result):
        self.after_calls.append((identity, result.kind))
        if self.raise_after:
            raise RuntimeError('db down')


class TestCallGuard(_Base):

    def test_guard_sees_every_call_with_identity_and_priority(self):
        guard = _RecordingGuard()
        client = self.client(guard=guard)
        client.ap_list_users(1)
        client.ap_add_quota(42, 15625)
        client.ap_search_user('wsab0000000042')
        client.user_self(TENANT_IP, UP)
        self.assertEqual(guard.before_calls, [
            ('ap', nac.AP_PRIORITY_BALANCE), ('ap', nac.AP_PRIORITY_CONVERSION),
            ('ap', nac.AP_PRIORITY_OTHER), ('user', None)])
        self.assertEqual(guard.after_calls, [('ap', 'ok'), ('ap', 'ok'), ('ap', 'ok'),
                                             ('user', 'ok')])

    def test_blocked_call_is_not_sent(self):
        guard = _RecordingGuard(block=nac.NotExecuted('circuit_open'))
        client = self.client(guard=guard)
        result = client.user_create_token(TENANT_IP, UP,
                                          nac.free_trial_token_body('wp_17_ab12', 2 ** 31))
        self.assertIsInstance(result, nac.NotExecuted)
        self.assertEqual(result.reason, 'circuit_open')
        self.assertEqual(self.recorder.requests, [])
        self.assertEqual(guard.after_calls, [], '沒送出的不再計一次')

    def test_guard_failure_before_is_fail_closed_and_after_is_ignored(self):
        client = self.client(guard=_RecordingGuard(raise_before=True))
        result = client.status()
        self.assertIsInstance(result, nac.NotExecuted)
        self.assertEqual(result.reason, 'guard_unavailable')
        self.assertEqual(self.recorder.requests, [])
        client = self.client(guard=_RecordingGuard(raise_after=True))
        self.assertTrue(client.status().ok)

    def test_ap_calls_need_a_known_priority(self):
        client = self.client()
        for priority in (None, 'urgent', 0):
            with self.assertRaises(nac.NewApiInputError):
                client.ap_status(priority=priority)
        self.assertEqual(self.recorder.requests, [])

    def test_priority_ceilings_put_the_balance_refresh_last(self):
        ceilings = nac.AP_PRIORITY_CEILINGS
        self.assertEqual(ceilings[nac.AP_PRIORITY_CONVERSION], nac.AP_RATE_PER_MINUTE)
        self.assertGreater(ceilings[nac.AP_PRIORITY_CONVERSION],
                           ceilings[nac.AP_PRIORITY_FREE_METER])
        self.assertGreater(ceilings[nac.AP_PRIORITY_FREE_METER], ceilings[nac.AP_PRIORITY_OTHER])
        self.assertGreater(ceilings[nac.AP_PRIORITY_OTHER], ceilings[nac.AP_PRIORITY_BALANCE])

    def test_circuit_effect_table(self):
        denied = _classify(403, {'reason': 'reveal_window'}, {'X-Woow-Proxy-Error': 'denied'})
        policy = _classify(403, {'reason': 'route'}, {'X-Woow-Proxy-Error': 'policy'})
        ap_revoked = _classify(401, {'success': False, 'code': 'AUTH_UNAUTHORIZED'})
        up_revoked = ap_revoked
        business = _classify(200, {'success': False, 'message_key': 'x'})
        cases = [
            ('ap', nac.Ok(status=200), ('success', None)),
            ('user', nac.NotExecuted('proxy_busy'), ('failure', nac.CIRCUIT_FAILURES_CODE)),
            ('user', nac.Unknown('proxy_timeout'), ('failure', nac.CIRCUIT_FAILURES_CODE)),
            ('ap', nac.NotExecuted('connect'), ('failure', nac.CIRCUIT_FAILURES_CODE)),
            ('user', denied, (None, None)),
            ('user', policy, ('trip', 'llm_proxy_policy')),
            ('ap', ap_revoked, ('trip', 'newapi_auth_unauthorized')),
            ('op', ap_revoked, ('trip', 'newapi_auth_unauthorized')),
            ('user', up_revoked, ('success', None)),  # 一戶的 UP 失效不是全域故障
            ('ap', business, ('success', None)),
            ('bg', policy, (None, None)),  # break_glass_off 不能讓全平台停擺
            ('bg', nac.Unknown('transport'), (None, None)),
        ]
        for reason in nac.CIRCUIT_NEUTRAL_REASONS:
            cases.append(('user', nac.NotExecuted(reason), (None, None)))
        for identity, result, effect in cases:
            with self.subTest(identity=identity, result=result):
                self.assertEqual(nac.circuit_effect(identity, result), effect)
        trip_codes = {nac.newapi_error_code(code) for code in nac.SERVICE_AUTH_CODES}
        self.assertNotIn(nac.NEWAPI_OTHER_CODE, trip_codes, '每個 trip 碼都在封閉登記表裡')


# --------------------------------------------------------------------------- #
#  MCP 跳板（DESIGN §5.2 T3；LLM-02 AC5）                                         #
# --------------------------------------------------------------------------- #
class TestMcpUrlGuard(unittest.TestCase):
    TABLE = {
        'newapi-proxy.new-api-stg.svc.cluster.local': {'10.43.88.10'},
        'newapi.new-api-stg.svc.cluster.local': {'10.43.88.11'},
        'newapi-proxy.new-api.svc.cluster.local': {'10.43.99.10'},
        'newapi.new-api.svc.cluster.local': {'10.43.99.11'},
        'sneaky.example.com': {'203.0.113.9', '10.43.88.10'},  # 兩個答案，其中一個是代理
        'cluster-ip.example.com': {'10.43.1.2'},
        'demo-mcp.paas-ws-abc.svc.cluster.local': {'10.43.12.5'},
        'headless-mcp.paas-ws-abc.svc.cluster.local': {'10.42.7.8'},
        'mcp.example.com': {'203.0.113.7'},
    }

    def resolver(self, host):
        return set(self.TABLE.get(host, ()))

    def test_ac5_urls_are_rejected(self):
        cases = {
            'http://newapi-proxy.new-api-stg.svc.cluster.local:8080': 'newapi_namespace',
            'http://newapi-proxy.new-api-stg:8080/x': 'newapi_namespace',
            'http://newapi-proxy.new-api-stg.svc:8080': 'newapi_namespace',
            'http://newapi-proxy.new-api-stg.svc.cluster.local.:8080': 'newapi_namespace',
            'http://NewApi-Proxy.NEW-API.svc.cluster.local': 'newapi_namespace',
            'http://10-42-3-7.new-api-stg.pod.cluster.local:8080': 'newapi_namespace',
            'http://10.43.88.10:8080': 'newapi_address',
            'http://[::ffff:10.43.88.10]:8080': 'newapi_address',
            'http://10.42.3.77:8080': 'cluster_address',  # pod IP
            'http://sneaky.example.com/sse': 'newapi_address',
            'http://cluster-ip.example.com/sse': 'cluster_address',
            'http://user:pw@10.43.99.11:3000/': 'newapi_address',
        }
        for url, reason in cases.items():
            with self.subTest(url=url):
                self.assertEqual(mcp_url_guard.jump_reason(url, self.resolver), reason)

    def test_legitimate_urls_pass(self):
        for url in ('http://demo-mcp.paas-ws-abc.svc.cluster.local:3001/mcp',
                    'http://headless-mcp.paas-ws-abc.svc.cluster.local:3001/mcp',
                    'https://mcp.example.com/sse', 'http://localhost:3000/mcp',
                    'http://unknown.example.invalid/sse', 'https://new-api.example.com/sse',
                    'not a url', '', None):
            with self.subTest(url=url):
                self.assertIsNone(mcp_url_guard.jump_reason(url, self.resolver))

    def test_resolution_failures_and_timeouts_are_empty(self):
        def boom(host):
            raise socket.gaierror(-2, 'nope')
        with patch.object(mcp_url_guard, '_getaddrinfo', side_effect=boom):
            self.assertEqual(mcp_url_guard.resolve('x.example'), set())
        with patch.object(mcp_url_guard, 'RESOLVE_TIMEOUT_SECONDS', 0.05), \
                patch.object(mcp_url_guard, '_getaddrinfo', side_effect=lambda h: time.sleep(0.5)):
            self.assertEqual(mcp_url_guard.resolve('slow.example'), set())

    def test_protected_names_cover_both_environments(self):
        self.assertEqual(set(mcp_url_guard.PROTECTED_HOSTS), {
            'newapi-proxy.new-api.svc.cluster.local', 'newapi.new-api.svc.cluster.local',
            'newapi-proxy.new-api-stg.svc.cluster.local', 'newapi.new-api-stg.svc.cluster.local'})
        self.assertIn(nac.PROXY_URL_PROD.split('//')[1].split(':')[0],
                      mcp_url_guard.PROTECTED_HOSTS)
        self.assertIn(nac.PROXY_URL_STG.split('//')[1].split(':')[0],
                      mcp_url_guard.PROTECTED_HOSTS)


# --------------------------------------------------------------------------- #
#  健康檢查 helper（後台〔測試連線〕；LLM-14 也用）                                  #
# --------------------------------------------------------------------------- #
class TestHealthHelpers(_Base):

    def test_status_invariants_need_the_exact_type(self):
        good = dict(nac.STATUS_INVARIANTS)
        self.assertTrue(all(nac.status_invariants(good).values()))
        bad = {**good, 'quota_per_unit': 500000.0, 'register_enabled': 0}
        result = nac.status_invariants(bad)
        self.assertFalse(result['quota_per_unit'])
        self.assertFalse(result['register_enabled'])
        self.assertFalse(any(nac.status_invariants(None).values()))

    def test_readyz_only_returns_states_numbers_and_booleans(self):
        self.recorder.status = 503
        self.recorder.body = json.dumps({
            'ready': False, 'upstream': 'connect', 'replay_store': 'ok', 'ap_days_left': 12,
            'op_days_left': None, 'break_glass': False, 'version': '0.2.0',
            'extra': 'nap_' + 'x' * 40, 'upstream_detail': 'Bearer abc'}).encode()
        base = self.base.rsplit(':', 1)[0] + ':8080'
        with patch.object(nac, 'PROXY_ADMIN_PORT', self.server.server_address[1]):
            out = nac.fetch_readyz(base)
        self.assertEqual(out, {'reachable': True, 'http_status': 503, 'ready': False,
                               'upstream': 'connect', 'replay_store': 'ok', 'ap_days_left': 12,
                               'op_days_left': None, 'break_glass': False, 'version': '0.2.0'})
        self.assertEqual(self.recorder.requests[-1]['path'], '/readyz')
        self.recorder.mode = 'disconnect'
        with patch.object(nac, 'PROXY_ADMIN_PORT', self.server.server_address[1]):
            self.assertEqual(nac.fetch_readyz(base), {'reachable': False, 'error': 'unknown'})


# --------------------------------------------------------------------------- #
#  秘密不外洩                                                                    #
# --------------------------------------------------------------------------- #
class TestSecretsDoNotLeak(_Base):

    def test_header_values_are_validated_before_requests_sees_them(self):
        client = self.client()
        for bearer in (UP + '\n', 'Bearer ' + UP, UP + ' x'):
            with self.assertRaises(nac.NewApiInputError) as caught:
                client.user_self(TENANT_IP, bearer)
            self.assertNotIn(UP, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
        self.assertEqual(self.recorder.requests, [], '什麼都沒送出')

    def test_signing_key_env_is_stripped_and_errors_carry_no_value(self):
        self.assertEqual(nac.parse_signing_key('%s:%s\n' % (KID, SECRET)),
                         (KID, SECRET.encode()))
        self.assertEqual(nac.parse_signing_key(' %s:%s , next:%s' % (KID, SECRET, 'N' * 40))[0],
                         KID)
        for bad in (None, '', '\n', 'nokid', '%s:short' % KID, 'bad kid:%s' % SECRET,
                    '%s:%s,broken' % (KID, SECRET)):
            with self.assertRaises(nac.NewApiConfigError) as caught:
                nac.parse_signing_key(bad)
            text = str(caught.exception)
            self.assertNotIn(SECRET, text)
            self.assertNotIn('short', text)
            self.assertIsNone(caught.exception.__cause__)

    def test_web_role_never_falls_back_to_cron_key(self):
        class Icp:
            def __init__(self, value):
                self.value = value

            def sudo(self):
                return self

            def get_param(self, key):
                return self.value

        env = {'ir.config_parameter': Icp(nac.PROXY_URL_STG)}
        environ = {nac.ENV_AUTH_CRON: '%s:%s' % (KID, SECRET)}
        self.assertEqual(nac.get_newapi_client(env, nac.ROLE_CRON, environ=environ).role, 'cron')
        with self.assertRaises(nac.NewApiConfigError) as caught:
            nac.get_newapi_client(env, nac.ROLE_WEB, environ=environ)
        self.assertEqual(caught.exception.code, 'signing_key_missing')
        for url, code in ((None, 'proxy_url_unconfigured'),
                          (nac.PROXY_URL_BLACKHOLE, 'proxy_url_unconfigured'),
                          ('http://evil.example', 'proxy_url_invalid')):
            with self.assertRaises(nac.NewApiConfigError) as caught:
                nac.get_newapi_client({'ir.config_parameter': Icp(url)}, nac.ROLE_CRON,
                                      environ=environ)
            self.assertEqual(caught.exception.code, code)

    def test_log_lines_have_no_body_and_no_secret(self):
        self.recorder.body = json.dumps({'success': True, 'data': {
            'key': 'K' * 48, 'token': UP, 'access_token': JWT}}).encode()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger(nac.__name__)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        client = self.client()
        client.user_reveal_token(TENANT_IP, UP, 5)
        client.user_login(TENANT_IP, 'wsab0000000042', 'P' * 64)
        output = stream.getvalue()
        self.assertIn('/api/token/{id}/key', output, '只記路徑樣板')
        for secret in ('K' * 48, UP, JWT, 'P' * 64, SECRET, '/api/token/5/key'):
            self.assertNotIn(secret, output)

    def test_result_repr_has_no_data(self):
        result = nac.Ok(data={'key': 'K' * 48}, status=200)
        self.assertNotIn('K' * 48, repr(result))

    def test_unexpected_client_error_is_raised_from_none(self):
        def broken_factory():
            raise RuntimeError('kaboom ' + SECRET)
        client = self.client(session_factory=broken_factory)
        with self.assertRaises(nac.NewApiTransportError) as caught:
            client.status()
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)


# --------------------------------------------------------------------------- #
#  合成 IP、使用者名稱、免費試用 body                                             #
# --------------------------------------------------------------------------- #
class TestIdentityHelpers(unittest.TestCase):

    def test_synthetic_ip_boundaries(self):
        self.assertEqual(nac.tenant_ip(1, 0), '198.18.1.1')
        self.assertEqual(nac.tenant_ip(nac.PROD_MAX_ACCOUNT_ID, 0), '198.19.255.255')
        self.assertEqual(nac.tenant_ip(1, 1), '198.18.17.1')
        self.assertEqual(nac.tenant_ip(4095, 1), '198.18.32.255')
        self.assertEqual(nac.tenant_ip(3839, 31), '198.19.255.255')
        for account_id, block in ((0, 0), (nac.PROD_MAX_ACCOUNT_ID + 1, 0), (4096, 1),
                                  (3840, 31), (1, 32), (1, -1), ('1', 0), (True, 0)):
            with self.assertRaises(nac.NewApiInputError, msg=(account_id, block)):
                nac.tenant_ip(account_id, block)

    def test_every_tenant_ip_is_inside_the_proxy_range(self):
        first = proxy_settings.TENANT_FIRST
        last = proxy_settings.TENANT_LAST
        import ipaddress
        for account_id, block in ((1, 0), (130815, 0), (1, 1), (4095, 31 - 1), (3839, 31)):
            self.assertTrue(first <= ipaddress.IPv4Address(nac.tenant_ip(account_id, block))
                            <= last)

    def test_username(self):
        self.assertEqual(nac.newapi_username('wp', 123), 'wp0000000123')
        self.assertEqual(nac.newapi_username('wsab', 9), 'wsab0000000009')
        for prefix in ('ws_a', 'Wp', 'w', 'wabcd', ''):
            with self.assertRaises(nac.NewApiInputError):
                nac.newapi_username(prefix, 1)
        with self.assertRaises(nac.NewApiInputError):
            nac.newapi_username('wp', 0)

    def test_prefix_block_pairing(self):
        nac.check_prefix_block('wp', 0)
        nac.check_prefix_block('wsab', 7)
        for prefix, block in (('wp', 1), ('wsab', 0), ('wsab', 32), ('xx', 1)):
            with self.assertRaises(nac.NewApiConfigError):
                nac.check_prefix_block(prefix, block)

    def test_free_trial_body_is_the_design_fuse(self):
        body = nac.free_trial_token_body('wp_17_ab12', 1_900_000_000)
        self.assertEqual(body, {
            'name': 'wp_17_ab12', 'expired_time': 1_900_000_000, 'remain_quota': 1,
            'unlimited_quota': False, 'model_limits_enabled': True,
            'model_limits': 'qwen3-4b-aicoder,qwen3-4b-cluster1', 'allow_ips': '',
            'group': 'free', 'cross_group_retry': False, 'allow_user_quota_query': False})
        with self.assertRaises(nac.NewApiInputError):
            nac.free_trial_token_body('ws_17_ab12', 1)

    def test_up_name_matches_proxy_pattern(self):
        self.assertRegex(nac.up_name(123, 1_800_000_000), proxy_policy.UP_NAME_RE.pattern)


# --------------------------------------------------------------------------- #
#  llm_crypto                                                                  #
# --------------------------------------------------------------------------- #
class TestLlmCrypto(unittest.TestCase):

    def setUp(self):
        self.key = Fernet.generate_key().decode()
        self.env = {llm_crypto.ENV_SECRET_KEYS: self.key}

    def test_roundtrip_and_binding(self):
        password = llm_crypto.generate_password()
        self.assertRegex(password, r'^[A-Za-z0-9]{64}$')
        token = llm_crypto.encrypt(5, 'wp0000000005', llm_crypto.FIELD_PASSWORD, password,
                                   environ=self.env)
        self.assertNotIn(password, token)
        self.assertEqual(llm_crypto.decrypt(token, 5, 'wp0000000005', llm_crypto.FIELD_PASSWORD,
                                            environ=self.env), password)
        # A 的密文搬到 B 的列、或搬到另一個欄位：解不開
        for account_id, username, field in ((6, 'wp0000000006', llm_crypto.FIELD_PASSWORD),
                                            (5, 'wp0000000005', llm_crypto.FIELD_UP_TOKEN),
                                            (5, 'ws0000000005', llm_crypto.FIELD_PASSWORD)):
            with self.assertRaises(llm_crypto.LlmSecretBindingMismatch):
                llm_crypto.decrypt(token, account_id, username, field, environ=self.env)

    def test_neutralized_sentinel_fails_closed(self):
        with self.assertRaises(llm_crypto.LlmSecretNeutralized):
            llm_crypto.decrypt('neutralized', 5, 'wp0000000005', llm_crypto.FIELD_UP_TOKEN,
                               environ=self.env)
        with self.assertRaises(llm_crypto.LlmSecretNeutralized):
            llm_crypto.decrypt('neutralized', 5, 'wp0000000005', llm_crypto.FIELD_UP_TOKEN,
                               environ={})

    def test_rotation_with_multifernet(self):
        old = self.key
        token = llm_crypto.encrypt(5, 'wp0000000005', llm_crypto.FIELD_UP_TOKEN, UP,
                                   environ={llm_crypto.ENV_SECRET_KEYS: old})
        new = Fernet.generate_key().decode()
        both = {llm_crypto.ENV_SECRET_KEYS: '%s,%s\n' % (new, old)}
        self.assertEqual(llm_crypto.decrypt(token, 5, 'wp0000000005', llm_crypto.FIELD_UP_TOKEN,
                                            environ=both), UP)
        with self.assertRaises(llm_crypto.LlmSecretBindingMismatch):
            llm_crypto.decrypt(token, 5, 'wp0000000005', llm_crypto.FIELD_UP_TOKEN,
                               environ={llm_crypto.ENV_SECRET_KEYS: new})

    def test_env_problems_fail_closed_without_the_value(self):
        for raw in (None, '', ' , ', 'not-a-fernet-key', self.key + ',garbage'):
            env = {} if raw is None else {llm_crypto.ENV_SECRET_KEYS: raw}
            with self.assertRaises(llm_crypto.LlmCryptoUnavailable) as caught:
                llm_crypto.encrypt(5, 'wp0000000005', llm_crypto.FIELD_UP_TOKEN, UP, environ=env)
            self.assertNotIn(self.key, str(caught.exception))
            self.assertNotIn('garbage', str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
        # 尾端 \n（--from-file 建的 Secret）照常可用
        self.assertTrue(llm_crypto.available({llm_crypto.ENV_SECRET_KEYS: self.key + '\n'}))

    def test_malformed_secret_is_rejected(self):
        with self.assertRaises(llm_crypto.LlmSecretMalformed):
            llm_crypto.encrypt(5, 'wp0000000005', llm_crypto.FIELD_UP_TOKEN, 'sk-notapat',
                               environ=self.env)
        with self.assertRaises(llm_crypto.LlmSecretMalformed):
            llm_crypto.encrypt(5, 'wp0000000005', llm_crypto.FIELD_PASSWORD, 'short',
                               environ=self.env)


# --------------------------------------------------------------------------- #
#  llm_redact                                                                  #
# --------------------------------------------------------------------------- #
class TestRedact(unittest.TestCase):

    def test_masks_every_secret_shape(self):
        text = ('Authorization: Bearer %s token=%s key="%s" sk-%s proof_token=%s '
                'password=hunter2hunter2 {"password": "p@ss word"} jwt %s' % (
                    UP, UP, 'K' * 48, 'Q' * 48, JWT, 'eyJhbGciOi.eyJzdWIiOiJ4In0.c2lnbmF0dXJl'))
        masked = llm_redact.redact(text)
        for secret in (UP, 'K' * 48, 'Q' * 48, JWT, 'hunter2hunter2', 'p@ss word',
                       'eyJhbGciOi.eyJzdWIiOiJ4In0'):
            self.assertNotIn(secret, masked)
        self.assertIn('Authorization', masked)

    def test_never_raises(self):
        class Bad:
            def __str__(self):
                raise ValueError('no')
        self.assertEqual(llm_redact.redact(Bad()), llm_redact.MASK)
        self.assertEqual(llm_redact.redact(Bad(), mask='***'), '***')

    def test_ticket_ac5_shapes(self):
        """LLM-02b AC5：Bearer、JWT、sk-…、nap_…、proof_token=…、password=… 都被遮。"""
        samples = {
            'Bearer': ('Authorization: Bearer opaque.Value-123', 'opaque.Value-123'),
            'JWT': ('jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI0MiJ9.c2lnbmF0dXJlLXZhbHVl',
                    'eyJzdWIiOiI0MiJ9'),
            'sk-': ('key in text sk-%s here' % ('Z9' * 24), 'Z9' * 24),
            'nap_': ('pat nap_%s end' % ('Q1w2' * 11), 'Q1w2' * 11),
            'proof_token': ('proof_token=abc.def-ghi&next=1', 'abc.def-ghi'),
            'password': ('password=hunter2hunter2 tail', 'hunter2hunter2'),
        }
        for label, (text, secret) in samples.items():
            with self.subTest(label=label):
                masked = llm_redact.redact(text)
                self.assertNotIn(secret, masked)
                self.assertIn(llm_redact.MASK, masked)

    def test_authorization_header_masks_the_bearer_value_not_the_word(self):
        # Bearer 必須先於 key=value：否則「Authorization: <redacted> 真值」。
        masked = llm_redact.redact('Authorization: Bearer abc.def-123')
        self.assertNotIn('abc.def-123', masked)

    def test_authorization_masks_the_credential_for_every_scheme(self):
        """審查 LLM-02b 第 2 輪：值遇到空白就停的話，``Basic``／``Token`` 只遮到 scheme 那個字。"""
        digest = ('Digest username="admin", realm="woow", nonce="dcd98b7102dd2f0e", uri="/", '
                  'response="6629fae49393a05397450978507c4ef1", opaque="5ccc069c403ebaf9"')
        samples = {
            'Basic': ('Authorization: Basic dXNlcjpwYXNzd29yZA==', ['dXNlcjpwYXNzd29yZA']),
            'Token': ('Authorization: Token abcdef123456 next', ['abcdef123456']),
            'Digest': ('Authorization: %s' % digest,
                       ['6629fae49393a05397450978507c4ef1', 'dcd98b7102dd2f0e', 'admin']),
            'quoted': ('{"Authorization": "Basic dXNlcjpw", "Host": "x"}', ['dXNlcjpw']),
            'escaped': ('{"authorization": "Digest response=\\"z9z9z9\\"", "a": 1}', ['z9z9z9']),
            'proxy': ("{'Proxy-Authorization': 'Basic cHJveHk6cHc='}", ['cHJveHk6cHc']),
            'no scheme': ('Authorization: opaqueValue123', ['opaqueValue123']),
            'url form': ('authorization=Basic%20abc&x=1', ['abc&']),
        }
        for label, (text, secrets) in samples.items():
            for mask in (llm_redact.MASK, '***'):
                with self.subTest(label=label, mask=mask):
                    masked = llm_redact.redact(text, mask=mask)
                    for secret in secrets:
                        self.assertNotIn(secret, masked)
                    self.assertIn(mask, masked)
        self.assertEqual(llm_redact.redact('Authorization: Token abcdef123456 next'),
                         'Authorization: <redacted> next')
        self.assertEqual(llm_redact.redact('{"Authorization": "Basic x", "Host": "h"}'),
                         '{"Authorization": <redacted>, "Host": "h"}', '後面的欄位不被吃掉')

    def test_init_attempt_shapes_still_masked(self):
        """``cloud_service_init_attempt.mask_init_error`` 原本自己的三條 regex 涵蓋的形狀（LLM-02b 收斂）。"""
        for text, secret in (
                ('password: "two words"', 'two words'),
                ('Authorization: Bearer abc.def-123', 'abc.def-123'),
                ('url?password%3Dhunter2&x=1', 'hunter2'),
                ("api_key='k k'", 'k k'),
                ("{'password': 'p y'}", 'p y'),
                ('"secret": "s3"', 's3'),
                ('access_token=tok123', 'tok123'),
                ('key eyJabcde.eyJfghij.klmnopq', 'eyJfghij'),
                # 原本的 init regex 對 token 是後綴比對（ngrok 的 authtoken）：收斂後不能少遮。
                ('authtoken=2abcDEF345ghi', '2abcDEF345ghi'),
                ('apitoken: zz99', 'zz99')):
            with self.subTest(text=text):
                masked = llm_redact.redact(text, mask='***')
                self.assertNotIn(secret, masked)
                self.assertIn('***', masked)

    def test_ordinary_words_are_not_mangled(self):
        """審查 LLM-02b finding 12：``sk-``／``nap_``／裸的 ``key`` 要有左邊界（init attempt 的錯誤
        訊息常有這些字；舊的 init regex 從來不遮它們）。"""
        for text in ('node had condition: disk-pressure', 'task-runner failed', 'monkey=1',
                     'snap_shot_42 restored', 'whisk-it-all-together-please-now-ok',
                     'publickey=ssh-ed25519'):
            with self.subTest(text=text):
                self.assertEqual(llm_redact.redact(text), text)
                self.assertEqual(llm_redact.redact(text, mask='***'), text)

    def test_real_keys_still_masked_at_a_boundary(self):
        key = 'sk-' + 'Ab9' * 16
        for text in (key, '"%s"' % key, 'x=%s' % key, '(%s)' % key, 'nap_%s' % ('Q1w2' * 11),
                     '{"key": "%s"}' % ('K' * 48), 'key=%s' % ('K' * 48)):
            with self.subTest(text=text):
                masked = llm_redact.redact(text)
                self.assertIn(llm_redact.MASK, masked)
                self.assertNotIn('Ab9' * 16, masked)
                self.assertNotIn('Q1w2' * 11, masked)
                self.assertNotIn('K' * 48, masked)


class TestClosedErrorCodes(unittest.TestCase):
    """N24（審查 LLM-02b findings 4、13）：PaaS 的錯誤碼是封閉集合。"""

    def test_newapi_codes_go_through_the_allow_list(self):
        self.assertEqual(nac.newapi_error_code('AUTH_SESSION_LIMIT'), 'newapi_auth_session_limit')
        for code in ('UNRECOGNISED', 'GRANT_1000_POINTS_TO_ACCOUNT_5', 'auth_session_limit',
                     '', None, 42):
            with self.subTest(code=code):
                self.assertEqual(nac.newapi_error_code(code), nac.NEWAPI_OTHER_CODE)
        for code in nac.KNOWN_NEWAPI_CODES:
            self.assertRegex(code, r'^[A-Z][A-Z0-9_]{0,63}$')

    def test_crypto_error_codes_cover_every_code_in_the_module(self):
        source = (SERVICES / 'llm_crypto.py').read_text(encoding='utf-8')
        import re
        literals = set(re.findall(r"code = '([a-z_]+)'", source))
        literals |= set(re.findall(r"(?:LlmCryptoError|LlmCryptoUnavailable|LlmSecretNeutralized|"
                                   r"LlmSecretBindingMismatch|LlmSecretMalformed)\('([a-z_]+)'\)",
                                   source))
        self.assertTrue(literals)
        self.assertLessEqual(literals, llm_crypto.ERROR_CODES)

    def test_definite_proxy_errors_match_the_classifier(self):
        self.assertEqual(nac.PROXY_DEFINITE_ERRORS, {'auth', 'policy', 'bad_request', 'too_large',
                                                     'denied'})


if __name__ == '__main__':
    unittest.main()
