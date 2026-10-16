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

        do_GET = do_POST = do_DELETE = _handle

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
        ctx = proxy_policy.PolicyContext(self.settings, now)
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


if __name__ == '__main__':
    unittest.main()
