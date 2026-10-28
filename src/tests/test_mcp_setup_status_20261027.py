# -*- coding: utf-8 -*-
"""2026-10-27：服務頁看得出 MCP 服務「還沒設定連線」（#2040；第五輪起含後端連線與探測加固）。

第三輪 staging 實測（全新開通的 Nextcloud MCP）：管理頁存好 Nextcloud 連線之前，``initialize``／
``tools/list`` 一律 503（子行程缺 ``NEXTCLOUD_MCP_BASE_URL``／``USERNAME``／``APP_PASSWORD`` 以 exit 2
結束），平台 ``state`` 卻是 ``running``，/woow 顯示「運作中」，租戶看不出哪裡不對。

修法：woow-mcp-server 的 ``/healthz`` 多回 ``mcp``（running／not_configured／not_running；舊映像沒有），
第五輪宣告了後端連線欄位的產品（odoo、litellm、hermes、n8n 等）另回 ``backend``（configured／missing）。平台在服務頁打開時以
``POST …/mcp-status`` 由伺服器對該服務自己的對外根位址 GET ``/healthz``，服務頁顯示提示（附管理頁網址），
服務狀態本身不動。守的事：

* ``parse_healthz``（純函式）：只有 200＋JSON 物件＋認得的字串才回那個值；舊映像（沒有欄位）、
  其他值、非 200、非 JSON、超長 body 一律 ``unknown``；``backend`` 只認 configured／missing；
* ``healthz_url``：只收 ``https://<一個 label>.<paas_domain>``，其他形狀（含叢集內名稱、別的網域）不發請求；
* ``_fetch``：不跟隨轉址、設自己的 User-Agent、body 有上限；**總時限**：慢速逐 byte 的 header／body
  在時限到時被切斷（本機真的 socket 驗），執行緒不會被拖住；
* ``probe``：逾時、網路錯誤都是 ``unknown``、從不 raise；**同一網址同時只探測一次**；晚到的結果照樣寫快取；
  **快取期限依結果**：要租戶動手的結果 2 秒（存好連線、切回分頁就看得到新狀態）、其他 10 秒；
* 能力 gate＝``_admin_page_public_url()`` 非空，與 mcp-server chart 家族完全重合；不是 slug 名單；
  ``CloudService._mcp_setup_status`` 只在 running 時查、用平台的 ``paas_domain``；
* 端點：工作區成員（含 guest、Portal 租戶）讀得到；別的工作區／非成員／沒有能力的服務一律拒絕且
  **不發請求**；不是 running 不查；呼叫端多送的參數（例如網址）整個拒絕；純讀、不寫帳本；回 backend；
* detail payload 與開通回應只帶 gate（``has_mcp_setup_status``），狀態不進 payload；
* 前端：gate 讀後端旗標、不用 slug；新字串都有 zh_TW 譯文，而且帶 ``#. odoo-javascript``（少了它前端
  會變回英文——Odoo 只把有這個註記的條目送給 JS），以 Odoo 自己的 ``code_translations`` 實際確認。

網路一律不出本機：``mcp_setup_status._fetch``（單一 GET）是 probe 層的 IO 縫；``_connection`` 是 fetch 層的縫，
換成連 127.0.0.1 的 ``HTTPConnection``，對著本機的假伺服器驗真的 socket 行為。前端行為另由
``tests/ui/test_mcp_setup_status_20261027.mjs``（node）驗。
"""
import http.client
import json
import re
import socket
import ssl
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import polib
import requests

from odoo.tests import HttpCase
from odoo.tests.common import BaseCase, TransactionCase, tagged
from odoo.tools.translate import code_translations

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import ROLE_ADMIN
from ..models.cloud_service import _internal_provision
from ..services import mcp_setup_status as mss

_FETCH = 'odoo.addons.woow_paas_platform.services.mcp_setup_status._fetch'
_ADDON = Path(__file__).resolve().parents[1]
_STATIC = _ADDON / 'static' / 'src' / 'paas'
_DOMAIN = 'woowtech.io'
_TEST_DOMAIN = 'mcpst-r5.example'


def _healthz(**fields):
    return json.dumps({'status': 'ok', **fields}).encode()


def _status(state, backend='unknown'):
    return mss.McpSetupStatus(state, backend)


class _LocalHttp:
    """本機一次性的 HTTP 伺服器：收一個連線、讀完 request header，交給 handler(conn) 回應。

    ``_connection`` 換成連這裡的 ``HTTPConnection``（plain HTTP），``_fetch`` 其他步驟（看門狗、header、
    讀取上限）原封不動跑在真的 socket 上。
    """

    def __init__(self, handler):
        self.handler = handler
        self.request = b''
        self.closed_by_client = threading.Event()
        self._server = socket.create_server(('127.0.0.1', 0))
        self._server.settimeout(10)
        self.port = self._server.getsockname()[1]
        self.connections = 0
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            conn, _addr = self._server.accept()
        except OSError:
            return
        self.connections += 1
        conn.settimeout(10)
        try:
            data = b''
            while b'\r\n\r\n' not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            self.request = data
            self.handler(conn)
        except OSError:
            self.closed_by_client.set()
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def connection_factory(self):
        return lambda host: http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)

    def close(self):
        try:
            self._server.close()
        except OSError:
            pass
        self._thread.join(15)


def _drip(conn, data, every):
    for byte in data:
        conn.sendall(bytes([byte]))
        time.sleep(every)


@tagged('post_install', '-at_install')
class TestMcpHealthzContract20261027(BaseCase):
    """``/healthz`` 回應 → 狀態，以及「只查平台網址」的形狀閘。"""

    def test_the_three_reported_states_pass_through(self):
        for state in ('running', 'not_configured', 'not_running'):
            with self.subTest(state=state):
                self.assertEqual(mss.parse_healthz(200, _healthz(mcp=state)), _status(state))
                self.assertEqual(mss.parse_healthz(200, _healthz(mcp=state).decode()), _status(state))

    def test_old_image_without_the_field_is_unknown(self):
        """第三輪實測的正式站映像只回 {"status":"ok"}（第五輪從 pi-agent 唯讀再量一次也是）。"""
        self.assertEqual(mss.parse_healthz(200, b'{"status":"ok"}'), mss.UNKNOWN)

    def test_values_outside_the_contract_are_unknown(self):
        for value in (None, '', 'RUNNING', 'Not_Configured', 'configured', 'error', 1, True,
                      ['running'], {'state': 'not_configured'}):
            with self.subTest(value=value):
                self.assertEqual(mss.parse_healthz(200, _healthz(mcp=value, backend='missing')),
                                 mss.UNKNOWN)

    def test_non_200_non_json_and_oversized_bodies_are_unknown(self):
        good = _healthz(mcp='not_configured')
        for status in (201, 204, 301, 302, 401, 403, 404, 500, 502, 503, 530):
            with self.subTest(status=status):
                self.assertEqual(mss.parse_healthz(status, good), mss.UNKNOWN)
        for body in (b'', b'<html>error code: 1033</html>', b'[]', b'"not_configured"',
                     b'{"mcp": "not_configured"', b'\xff\xfe\x00junk', None, 42):
            with self.subTest(body=body):
                self.assertEqual(mss.parse_healthz(200, body), mss.UNKNOWN)
        oversized = json.dumps({'mcp': 'not_configured', 'pad': 'x' * mss._BODY_MAX_BYTES}).encode()
        self.assertEqual(mss.parse_healthz(200, oversized), mss.UNKNOWN)

    def test_healthz_url_only_accepts_the_platform_shape(self):
        ok = 'https://paas-cs-5a105e8b-230ef033.woowtech.io'
        self.assertEqual(mss.healthz_url(ok, _DOMAIN), ok + '/healthz')
        self.assertEqual(mss.healthz_url(ok + '/', _DOMAIN), ok + '/healthz')
        self.assertEqual(mss.healthz_url(ok, 'WoowTech.IO'), ok + '/healthz', 'paas_domain 不分大小寫')
        for bad in (None, '', 'paas-cs-x.woowtech.io', 'http://paas-cs-x.woowtech.io',
                    'https://paas-cs-x.woowtech.io:8443', 'https://user:pw@paas-cs-x.woowtech.io',
                    'https://paas-cs-x.woowtech.io/private_abc/mcp', 'https://paas-cs-x.woowtech.io/healthz',
                    'https://paas-cs-x.woowtech.io?x=1', 'https://paas-cs-x.woowtech.io#x',
                    'https://127.0.0.1', 'https://localhost', 'https://169.254.169.254',
                    'https://svc_x.paas-ws-1.svc', 'https://[::1]', 'ftp://paas-cs-x.woowtech.io',
                    'https://paas-cs-x.woowtech.io:bad'):
            with self.subTest(bad=bad):
                self.assertEqual(mss.healthz_url(bad, _DOMAIN), '')

    def test_healthz_url_is_confined_to_the_platform_domain(self):
        """第五輪 D3-B（縱深防禦）：第四輪審查找到的反例全部擋下——叢集內名稱、metadata、別的網域、
        後綴把戲、paas_domain 本身、兩層 label。"""
        for bad in ('https://svc-x.paas-ws-1.svc', 'https://kubernetes.default.svc.cluster.local',
                    'https://paas-odoo-platform.paas-system.svc.cluster.local',
                    'https://metadata.google.internal', 'https://1.2.3.4.nip.io',
                    'https://paas-cs-x.woowtech.io.evil.example', 'https://paas-cs-x.notwoowtech.io',
                    'https://woowtech.io', 'https://a.paas-cs-x.woowtech.io', 'https://-bad.woowtech.io'):
            with self.subTest(bad=bad):
                self.assertEqual(mss.healthz_url(bad, _DOMAIN), '')
        ok = 'https://paas-cs-aa-bb.woowtech.io'
        for domain in (None, '', '   ', 'localhost', '127.0.0.1', 'woowtech.io/x', 'woow_tech.io', 42):
            with self.subTest(domain=domain):
                self.assertEqual(mss.healthz_url(ok, domain), '', 'paas_domain 不合法就一律不查')
        self.assertEqual(mss.healthz_url(ok, 'stg.woowtech.io'), '', '網域不符（例：正式站網址＋staging 設定）')


@tagged('post_install', '-at_install')
class TestMcpBackendField20261027(BaseCase):
    """第五輪 P3：宣告了後端連線欄位的產品（odoo、litellm、hermes、n8n 等）的 ``/healthz`` 多回 ``backend``。"""

    def test_backend_passes_through_with_the_mcp_state(self):
        for state in ('running', 'not_configured', 'not_running'):
            for backend in ('configured', 'missing'):
                with self.subTest(state=state, backend=backend):
                    self.assertEqual(mss.parse_healthz(200, _healthz(mcp=state, backend=backend)),
                                     _status(state, backend))

    def test_missing_or_unrecognised_backend_is_unknown_and_keeps_the_mcp_state(self):
        """舊映像／其他產品沒有這個欄位＝``unknown``（不提示）；值不認得也一樣，``mcp`` 照常讀。"""
        self.assertEqual(mss.parse_healthz(200, _healthz(mcp='running')), _status('running'))
        for value in (None, '', 'Missing', 'CONFIGURED', 'none', 'error', 1, True, [], {'v': 'missing'}):
            with self.subTest(value=value):
                self.assertEqual(mss.parse_healthz(200, _healthz(mcp='running', backend=value)),
                                 _status('running'))

    def test_actionable_matrix_matches_the_frontend_condition(self):
        """``is_actionable``（決定快取期限）＝前端 ``mcpSetupCondition`` 有值的那三種情況。"""
        actionable = {('not_configured', b) for b in mss.BACKENDS} | {
            ('not_running', b) for b in mss.BACKENDS} | {('running', 'missing')}
        for state in mss.STATES:
            for backend in mss.BACKENDS:
                with self.subTest(state=state, backend=backend):
                    self.assertEqual(mss.is_actionable(_status(state, backend)),
                                     (state, backend) in actionable)
        self.assertEqual(mss.STATES, {'running', 'not_configured', 'not_running', 'unknown'})
        self.assertEqual(mss.BACKENDS, {'configured', 'missing', 'unknown'})


@tagged('post_install', '-at_install')
class TestMcpSetupFetch20261027(BaseCase):
    """``_fetch``：真的 socket（本機假伺服器）——送出的請求、不跟隨轉址、body 上限、總時限。"""

    url = 'https://paas-cs-feedbeef-0badf00d.woowtech.io/healthz'

    def _serve(self, handler):
        server = _LocalHttp(handler)
        self.addCleanup(server.close)
        patcher = patch.object(mss, '_connection', server.connection_factory())
        patcher.start()
        self.addCleanup(patcher.stop)
        return server

    def test_real_connection_verifies_tls_on_port_443(self):
        """不連線，只看建出來的物件：HTTPS、443、驗憑證與主機名、只談 HTTP/1.1。"""
        conn = mss._connection('paas-cs-feedbeef-0badf00d.woowtech.io')
        try:
            self.assertIsInstance(conn, http.client.HTTPSConnection)
            self.assertEqual((conn.host, conn.port), ('paas-cs-feedbeef-0badf00d.woowtech.io', 443))
            self.assertEqual(conn._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(conn._context.check_hostname)
            self.assertLessEqual(conn.timeout, 3.0)
        finally:
            conn.close()

    def test_request_shape_own_user_agent_no_credentials(self):
        body = _healthz(mcp='not_configured', backend='missing')
        server = self._serve(lambda conn: conn.sendall(
            b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s'
            % (len(body), body)))
        self.assertEqual(mss._fetch(self.url), (200, body))
        request = server.request.decode()
        lines = request.split('\r\n')
        self.assertEqual(lines[0], 'GET /healthz HTTP/1.1')
        headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                   for line in lines[1:] if ':' in line}
        self.assertEqual(headers['host'], 'paas-cs-feedbeef-0badf00d.woowtech.io')
        self.assertTrue(headers['user-agent'].startswith('WoowPaaS-'))
        self.assertNotIn('python', headers['user-agent'].lower())
        self.assertEqual(headers['connection'], 'close')
        for absent in ('cookie', 'authorization', 'accept-encoding', 'x-forwarded-for'):
            self.assertNotIn(absent, headers)

    def test_redirect_is_not_followed_and_is_unknown(self):
        """開了 Cloudflare Access 的服務：/healthz 被 302 到登入頁 ⇒ 不跟過去、unknown。"""
        server = self._serve(lambda conn: conn.sendall(
            b'HTTP/1.1 302 Found\r\nLocation: https://login.example/\r\nContent-Length: 0\r\n\r\n'))
        status_code, body = mss._fetch(self.url)
        self.assertEqual((status_code, body), (302, b''))
        self.assertEqual(server.connections, 1, '只連了一次（沒有跟著轉址再連）')
        self.assertEqual(mss.parse_healthz(status_code, body), mss.UNKNOWN)

    def test_complete_response_is_read_without_waiting_for_close(self):
        """伺服器回完不關連線（keep-alive）：照 Content-Length 讀完就走，不等到總時限。"""
        body = _healthz(mcp='running', backend='configured')

        def keepalive(conn):
            conn.sendall(b'HTTP/1.1 200 OK\r\nConnection: keep-alive\r\nContent-Length: %d\r\n\r\n%s'
                         % (len(body), body))
            time.sleep(2)

        self._serve(keepalive)
        started = time.monotonic()
        self.assertEqual(mss._fetch(self.url), (200, body))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_chunked_response_is_understood(self):
        body = _healthz(mcp='not_running')
        self._serve(lambda conn: conn.sendall(
            b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n%x\r\n%s\r\n0\r\n\r\n' % (len(body), body)))
        self.assertEqual(mss._fetch(self.url), (200, body))

    def test_body_read_stops_at_the_limit(self):
        big = b'{"mcp":"running","pad":"' + b'x' * (4 * mss._BODY_MAX_BYTES) + b'"}'
        self._serve(lambda conn: conn.sendall(
            b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s' % (len(big), big)))
        status_code, body = mss._fetch(self.url)
        self.assertEqual(len(body), mss._BODY_MAX_BYTES + 1)
        self.assertEqual(mss.parse_healthz(status_code, body), mss.UNKNOWN)

    def test_slow_drip_body_is_cut_at_the_total_deadline(self):
        """第四輪 review raw/08：每 1.5 秒 1 byte 的 body 把執行緒拖住 24 秒。現在總時限到就切斷。"""
        payload = b'{"status":"ok","mcp":"running","pad":"' + b'x' * 400 + b'"}'

        def drip(conn):
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(payload))
            _drip(conn, payload, 0.05)      # 全部滴完要 20 秒以上

        self._serve(drip)
        with patch.object(mss, '_DEADLINE_SECONDS', 0.6):
            started = time.monotonic()
            with self.assertRaises(mss._DeadlineExceeded):
                mss._fetch(self.url)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_slow_drip_headers_are_cut_at_the_total_deadline(self):
        """header 也能逐 byte 滴（每次 recv 都在 socket 逾時內）——同樣在總時限切斷。"""
        self._serve(lambda conn: _drip(conn, b'HTTP/1.1 200 OK\r\nX-Slow: ' + b'a' * 400, 0.05))
        with patch.object(mss, '_DEADLINE_SECONDS', 0.6):
            started = time.monotonic()
            with self.assertRaises(mss._DeadlineExceeded):
                mss._fetch(self.url)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_silent_server_is_cut_at_the_total_deadline(self):
        self._serve(lambda conn: time.sleep(5))
        with patch.object(mss, '_DEADLINE_SECONDS', 0.6):
            started = time.monotonic()
            with self.assertRaises(mss._DeadlineExceeded):
                mss._fetch(self.url)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_slow_drip_does_not_hold_the_probe_thread(self):
        """經 probe：呼叫端在時限拿到 unknown，executor 的那條執行緒也在時限附近被放掉（不再是滴完才放）。"""
        mss._cache.clear()
        self.addCleanup(mss._cache.clear)
        payload = b'{"status":"ok","mcp":"running","pad":"' + b'x' * 400 + b'"}'

        def drip(conn):
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(payload))
            _drip(conn, payload, 0.05)

        self._serve(drip)
        base = 'https://paas-cs-feedbeef-0badf00d.woowtech.io'
        with patch.object(mss, '_DEADLINE_SECONDS', 0.6):
            started = time.monotonic()
            self.assertEqual(mss.probe(base, _DOMAIN), mss.UNKNOWN)
            self.assertLess(time.monotonic() - started, 1.5)
            for _i in range(30):
                if mss.healthz_url(base, _DOMAIN) not in mss._inflight:
                    break
                time.sleep(0.05)
            self.assertNotIn(mss.healthz_url(base, _DOMAIN), mss._inflight,
                             '探測執行緒在總時限附近就結束（滴完要 20 秒以上）')
            self.assertLess(time.monotonic() - started, 2.5)


@tagged('post_install', '-at_install')
class TestMcpSetupProbe20261027(BaseCase):
    """``probe``：從不 raise、呼叫端總時限、防重複探測、晚到的結果、快取期限依結果而定。"""

    base = 'https://paas-cs-feedbeef-0badf00d.woowtech.io'

    def setUp(self):
        super().setUp()
        mss._cache.clear()
        mss._inflight.clear()
        self.addCleanup(mss._cache.clear)
        self.clock = [1000.0]
        patcher = patch.object(mss, '_now', lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _wait_idle(self, url, timeout=3.0):
        deadline = time.monotonic() + timeout
        while url in mss._inflight and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertNotIn(url, mss._inflight)

    def test_network_failures_are_unknown_and_never_raise(self):
        for error in (requests.exceptions.ConnectionError('Name or service not known'),
                      socket.gaierror(-2, 'Name or service not known'), socket.timeout('slow'),
                      ssl.SSLError('bad cert'), http.client.RemoteDisconnected('gone'),
                      mss._DeadlineExceeded('slow'), RuntimeError('boom'), ValueError('x')):
            with self.subTest(error=type(error).__name__):
                mss._cache.clear()
                with patch(_FETCH, side_effect=error):
                    self.assertEqual(mss.probe(self.base, _DOMAIN), mss.UNKNOWN)

    def test_the_callers_deadline_bounds_the_wait(self):
        """DNS 卡住時探測本身停不下來；呼叫端仍最多只等 _DEADLINE_SECONDS。"""
        def slow(url):
            time.sleep(1.0)
            return 200, _healthz(mcp='not_configured')

        with patch.object(mss, '_DEADLINE_SECONDS', 0.2), patch(_FETCH, side_effect=slow):
            started = time.monotonic()
            self.assertEqual(mss.probe(self.base, _DOMAIN), mss.UNKNOWN)
            self.assertLess(time.monotonic() - started, 0.9)
            self._wait_idle(mss.healthz_url(self.base, _DOMAIN))

    def test_one_probe_per_url_at_a_time(self):
        """第五輪 D3-B：同一網址已經有探測在跑時，後到的請求等同一個，不另外送。"""
        started = threading.Event()
        release = threading.Event()
        calls = []

        def blocking(url):
            calls.append(url)
            started.set()
            release.wait(5)
            return 200, _healthz(mcp='not_configured')

        results = []
        with patch(_FETCH, side_effect=blocking):
            threads = [threading.Thread(target=lambda: results.append(mss.probe(self.base, _DOMAIN)))
                       for _i in range(3)]
            for thread in threads:
                thread.start()
            self.assertTrue(started.wait(3))
            time.sleep(0.2)            # 讓另外兩個請求都進到 probe、等在同一個 future 上
            self.assertEqual(len(mss._inflight), 1)
            release.set()
            for thread in threads:
                thread.join(5)
        self.assertEqual(len(calls), 1, '三個同時的請求只送出一次探測')
        self.assertEqual(results, [_status('not_configured')] * 3)

    def test_a_late_result_is_still_cached_for_the_next_poll(self):
        def late(url):
            time.sleep(0.5)
            return 200, _healthz(mcp='not_configured')

        url = mss.healthz_url(self.base, _DOMAIN)
        with patch.object(mss, '_DEADLINE_SECONDS', 0.2), patch(_FETCH, side_effect=late):
            self.assertEqual(mss.probe(self.base, _DOMAIN), mss.UNKNOWN)
            self._wait_idle(url)
        with patch(_FETCH) as fetch:
            self.assertEqual(mss.probe(self.base, _DOMAIN), _status('not_configured'))
        fetch.assert_not_called()

    def test_saving_the_connection_shows_up_on_the_next_request(self):
        """第五輪 D3-B（第四輪 review S2：提示多掛 31.2 秒）：not_configured 之後 10 秒內改成 running，
        下一次請求（超過 2 秒）就要拿到 running；2 秒內的重複請求共用一次探測。"""
        with patch(_FETCH, return_value=(200, _healthz(mcp='not_configured'))) as fetch:
            self.assertEqual(mss.probe(self.base, _DOMAIN), _status('not_configured'))
            self.clock[0] += 1.0
            self.assertEqual(mss.probe(self.base + '/', _DOMAIN), _status('not_configured'))
        self.assertEqual(fetch.call_count, 1, '2 秒內共用（同一頁的 focus＋visibilitychange、多個分頁）')
        self.clock[0] += 1.5     # 距第一次 2.5 秒：第四輪這時還在 10 秒快取裡
        with patch(_FETCH, return_value=(200, _healthz(mcp='running'))) as fetch:
            self.assertEqual(mss.probe(self.base, _DOMAIN), _status('running'))
        self.assertEqual(fetch.call_count, 1)

    def test_every_actionable_result_has_the_short_lifetime(self):
        for body in (_healthz(mcp='not_configured'), _healthz(mcp='not_running'),
                     _healthz(mcp='running', backend='missing')):
            with self.subTest(body=body):
                mss._cache.clear()
                with patch(_FETCH, return_value=(200, body)):
                    mss.probe(self.base, _DOMAIN)
                self.clock[0] += mss._ACTIONABLE_CACHE_TTL_SECONDS + 0.01
                with patch(_FETCH, return_value=(200, _healthz(mcp='running'))) as fetch:
                    self.assertEqual(mss.probe(self.base, _DOMAIN), _status('running'))
                fetch.assert_called_once()

    def test_healthy_and_unknown_results_keep_the_longer_lifetime(self):
        for answer in ((200, _healthz(mcp='running')), (200, _healthz(mcp='running', backend='configured')),
                       (200, b'{"status":"ok"}')):
            with self.subTest(answer=answer):
                mss._cache.clear()
                with patch(_FETCH, return_value=answer) as fetch:
                    first = mss.probe(self.base, _DOMAIN)
                    self.clock[0] += mss._CACHE_TTL_SECONDS - 0.5
                    self.assertEqual(mss.probe(self.base, _DOMAIN), first)
                self.assertEqual(fetch.call_count, 1)
                self.clock[0] += 1.0
                with patch(_FETCH, return_value=(200, _healthz(mcp='not_running'))) as fetch:
                    self.assertEqual(mss.probe(self.base, _DOMAIN), _status('not_running'))
                fetch.assert_called_once()
        self.assertGreater(mss._CACHE_TTL_SECONDS, mss._ACTIONABLE_CACHE_TTL_SECONDS)
        self.assertLessEqual(mss._ACTIONABLE_CACHE_TTL_SECONDS, 3.0, '要租戶動手的結果不能再快取 10 秒')

    def test_unreachable_is_cached_so_a_dead_host_does_not_cost_every_poll(self):
        with patch(_FETCH, side_effect=socket.timeout('slow')) as fetch:
            self.assertEqual(mss.probe(self.base, _DOMAIN), mss.UNKNOWN)
            self.clock[0] += 5
            self.assertEqual(mss.probe(self.base, _DOMAIN), mss.UNKNOWN)
        self.assertEqual(fetch.call_count, 1)

    def test_an_invalid_base_url_or_domain_never_touches_the_network(self):
        with patch(_FETCH) as fetch:
            for base, domain in (('', _DOMAIN), ('http://paas-cs-x.woowtech.io', _DOMAIN),
                                 ('https://127.0.0.1', _DOMAIN), ('https://evil.example:8443', _DOMAIN),
                                 ('https://evil.example', _DOMAIN),
                                 ('https://kubernetes.default.svc.cluster.local', _DOMAIN),
                                 (self.base, ''), (self.base, 'stg.woowtech.io')):
                with self.subTest(base=base, domain=domain):
                    self.assertEqual(mss.probe(base, domain), mss.UNKNOWN)
        fetch.assert_not_called()


@tagged('post_install', '-at_install')
class TestMcpSetupCapability20261027(TransactionCase):
    """能力 gate＝``_admin_page_public_url()`` 非空，而且恰好是 mcp-server chart 家族；
    ``_mcp_setup_status`` 只在 running 時、以平台的 paas_domain 查。"""

    def setUp(self):
        super().setUp()
        mss._cache.clear()
        self.addCleanup(mss._cache.clear)
        self.env['ir.config_parameter'].sudo().set_param('woow_paas_platform.paas_domain', _TEST_DOMAIN)
        self.suffix = uuid.uuid4().hex[:8]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'mcp gate ' + self.suffix, 'owner_id': self.env.user.id})

    def _service(self, template, subdomain=True, state='running'):
        label = uuid.uuid4().hex[:8]
        vals = {
            'name': 'gate-' + label, 'workspace_id': self.workspace.id, 'template_id': template.id,
            'state': state, 'helm_namespace': 'paas-ws-gate-' + self.suffix,
            'helm_release_name': 'svc-' + label, 'helm_values': '{}',
        }
        if subdomain:
            vals['subdomain'] = 'paas-cs-%s-%s' % (self.suffix, label)
        return _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create(vals)

    def test_capability_matches_exactly_the_mcp_server_chart_family(self):
        Template = self.env['woow_paas_platform.cloud_app_template'].sudo().with_context(active_test=False)
        family = Template.search([]).filtered(
            lambda t: (t.helm_chart_name or '').rstrip('/').endswith('/mcp-server'))
        self.assertIn(self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud'), family)
        self.assertGreaterEqual(len(family), 2)
        for template in Template.search([]):
            with self.subTest(slug=template.slug):
                predicate = bool(template.has_admin_gui and template.admin_password_carrier == 'config_json')
                self.assertEqual(predicate, template in family,
                                 'has_admin_gui＋config_json 必須恰好是 mcp-server chart 家族')

    def test_service_method_is_the_admin_page_url_being_non_empty(self):
        nextcloud = self._service(self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud'))
        self.assertTrue(nextcloud._chart_has_mcp_setup_status())
        self.assertEqual(nextcloud._admin_page_public_url(),
                         'https://%s.%s' % (nextcloud.subdomain, _TEST_DOMAIN))
        self.assertTrue(mss.healthz_url(nextcloud._admin_page_public_url(), _TEST_DOMAIN),
                        '平台算出的網址一定過得了形狀閘')

    def test_other_templates_do_not_have_it(self):
        """有 admin GUI 但載體不是 config_json（hermes-agent／open-design＝helm_secret、omnigent＝
        app_managed）與沒有 admin GUI 的模板都沒有這個能力，即使有 subdomain。"""
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        checked = 0
        for xmlid in ('cloud_app_postgresql', 'cloud_app_hermes_agent', 'cloud_app_omnigent',
                      'cloud_app_opendesign'):
            template = self.env.ref('woow_paas_platform.' + xmlid, raise_if_not_found=False)
            if not template:
                continue
            checked += 1
            with self.subTest(xmlid=xmlid):
                draft = Service.new({'template_id': template.id, 'subdomain': 'paas-cs-x-' + self.suffix})
                self.assertFalse(draft._chart_has_mcp_setup_status())
                self.assertEqual(draft._admin_page_public_url(), '')
        self.assertGreaterEqual(checked, 3, 'fixture 至少要涵蓋三個對照模板')

    def test_no_subdomain_means_no_capability(self):
        service = self._service(self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud'), subdomain=False)
        self.assertFalse(service._chart_has_mcp_setup_status())
        self.assertEqual(service._admin_page_public_url(), '')

    def test_model_probe_uses_the_admin_page_url_and_the_platform_domain(self):
        service = self._service(self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud'))
        with patch(_FETCH, return_value=(200, _healthz(mcp='running', backend='missing'))) as fetch:
            status, admin_url = service._mcp_setup_status()
        self.assertEqual(status, _status('running', 'missing'))
        self.assertEqual(admin_url, 'https://%s.%s' % (service.subdomain, _TEST_DOMAIN))
        fetch.assert_called_once_with(admin_url + '/healthz')

    def test_model_probe_only_when_running(self):
        for state in ('deploying', 'initializing', 'upgrading', 'error', 'suspended'):
            with self.subTest(state=state):
                service = self._service(self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud'), state=state)
                with patch(_FETCH) as fetch:
                    status, admin_url = service._mcp_setup_status()
                self.assertEqual(status, mss.UNKNOWN)
                self.assertTrue(admin_url, '管理頁網址照樣給（提示用），只是不查')
                fetch.assert_not_called()

    def test_model_probe_refuses_an_address_outside_the_platform_domain(self):
        """縱深防禦：即使哪天 `_admin_page_public_url` 給了別的網域（例：設定被改），也不查。"""
        service = self._service(self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud'))
        Service = type(self.env['woow_paas_platform.cloud_service'])
        for rogue in ('https://evil.example', 'https://kubernetes.default.svc.cluster.local',
                      'https://paas-cs-x.%s.evil.example' % _TEST_DOMAIN):
            with self.subTest(rogue=rogue):
                with patch.object(Service, '_admin_page_public_url', return_value=rogue), \
                        patch(_FETCH) as fetch:
                    status, _url = service._mcp_setup_status()
                self.assertEqual(status, mss.UNKNOWN)
                fetch.assert_not_called()


@tagged('post_install', '-at_install')
class TestMcpStatusEndpoint20261027(HttpCase):
    """``POST /api/workspaces/<ws>/services/<id>/mcp-status``：授權、gate、降級、純讀、backend。"""

    def setUp(self):
        super().setUp()
        mss._cache.clear()
        self.addCleanup(mss._cache.clear)
        self.env['ir.config_parameter'].sudo().set_param('woow_paas_platform.paas_domain', _TEST_DOMAIN)
        suffix = uuid.uuid4().hex[:8]
        self.suffix = suffix

        def user(label, group='base.group_user'):
            login = 'mcpst_%s_%s' % (label, suffix)
            return self.env['res.users'].sudo().with_context(no_reset_password=True).create({
                'name': login, 'login': login, 'password': login, 'email': login + '@test.example',
                'groups_id': [(6, 0, [self.env.ref(group).id])],
            })

        self.owner, self.guest, self.other, self.stranger = (
            user('owner'), user('guest'), user('other'), user('stranger'))
        self.portal = user('portal', 'base.group_portal')
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        self.workspace = Workspace.create({'name': 'MCP status ' + suffix, 'owner_id': self.owner.id})
        self.other_workspace = Workspace.create({'name': 'MCP other ' + suffix, 'owner_id': self.other.id})
        Access.create({'workspace_id': self.workspace.id, 'user_id': self.guest.id, 'role': 'guest'})
        Access.create({'workspace_id': self.workspace.id, 'user_id': self.portal.id, 'role': 'user'})
        self.nextcloud = self.env.ref('woow_paas_platform.cloud_app_mcp_nextcloud')
        self.postgres = self.env.ref('woow_paas_platform.cloud_app_postgresql')
        self.service = self._service(self.workspace, self.nextcloud, 'nc')
        self.plain = self._service(self.workspace, self.postgres, 'pg')
        self.foreign = self._service(self.other_workspace, self.nextcloud, 'foreign')
        self.route = '/api/workspaces/%s/services/%s/mcp-status'

    def _service(self, workspace, template, label, **extra):
        vals = {
            'name': label, 'workspace_id': workspace.id, 'template_id': template.id,
            'state': 'running', 'helm_namespace': 'paas-ws-mcpst-' + self.suffix,
            'helm_release_name': 'svc-%s-%s' % (label, self.suffix),
            'subdomain': 'paas-cs-%s-%s' % (self.suffix, label), 'helm_values': '{}',
        }
        vals.update(extra)
        return _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create(vals)

    def _post(self, workspace_id, service_id, **params):
        response = self.url_open(self.route % (workspace_id, service_id), data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        body = response.json()
        self.assertNotIn('error', body, body)
        return body['result']

    def _call(self, member, service, fetch_result=None, fetch_error=None, workspace_id=None, **params):
        self.authenticate(member.login, member.login)
        kwargs = {'side_effect': fetch_error} if fetch_error else {
            'return_value': fetch_result or (200, _healthz(mcp='running'))}
        with patch(_FETCH, **kwargs) as fetch:
            result = self._post(workspace_id or service.workspace_id.id, service.id, **params)
        return result, fetch

    def test_not_configured_is_reported_with_the_admin_page_url(self):
        result, fetch = self._call(self.owner, self.service, (200, _healthz(mcp='not_configured')))
        self.assertTrue(result['success'], result)
        admin_url = self.service._admin_page_public_url()
        self.assertEqual(result['data'], {'state': 'not_configured', 'backend': 'unknown',
                                          'admin_url': admin_url})
        fetch.assert_called_once_with(admin_url + '/healthz')
        self.assertEqual(self.service.state, 'running', '服務狀態本身不動')

    def test_running_and_not_running_pass_through(self):
        for state in ('running', 'not_running'):
            with self.subTest(state=state):
                mss._cache.clear()
                result, _fetch = self._call(self.owner, self.service, (200, _healthz(mcp=state)))
                self.assertEqual(result['data']['state'], state)

    def test_backend_field_passes_through(self):
        """第五輪 P3：backend（configured／missing）原樣給前端。"""
        for backend in ('missing', 'configured'):
            with self.subTest(backend=backend):
                mss._cache.clear()
                result, _fetch = self._call(self.owner, self.service,
                                            (200, _healthz(mcp='running', backend=backend)))
                self.assertEqual((result['data']['state'], result['data']['backend']), ('running', backend))
        mss._cache.clear()
        result, _fetch = self._call(self.owner, self.service,
                                    (200, _healthz(mcp='running', backend='<script>')))
        self.assertEqual(result['data']['backend'], 'unknown', '不認得的值不轉出')

    def test_old_image_without_the_field_is_unknown_not_an_error(self):
        result, fetch = self._call(self.owner, self.service, (200, b'{"status":"ok"}'))
        self.assertTrue(result['success'])
        self.assertEqual((result['data']['state'], result['data']['backend']), ('unknown', 'unknown'))
        fetch.assert_called_once()

    def test_timeout_and_unreachable_are_unknown_not_an_error(self):
        for error in (requests.exceptions.ReadTimeout('slow'), mss._DeadlineExceeded('slow'),
                      requests.exceptions.ConnectionError('Name or service not known')):
            with self.subTest(error=type(error).__name__):
                mss._cache.clear()
                result, _fetch = self._call(self.owner, self.service, fetch_error=error)
                self.assertTrue(result['success'])
                self.assertEqual(result['data']['state'], 'unknown')
                self.assertNotIn('slow', json.dumps(result))
                self.assertNotIn('Name or service', json.dumps(result))

    def test_every_workspace_member_can_read_including_guest_and_portal_tenants(self):
        for member in (self.guest, self.portal):
            with self.subTest(member=member.login):
                mss._cache.clear()
                result, _fetch = self._call(member, self.service, (200, _healthz(mcp='not_configured')))
                self.assertTrue(result['success'], result)
                self.assertEqual(result['data']['state'], 'not_configured')

    def test_other_workspaces_and_non_members_are_refused_without_a_request(self):
        cases = (
            (self.guest, self.foreign, self.foreign.workspace_id.id),     # 別人的工作區
            (self.guest, self.foreign, self.workspace.id),                # 自己的工作區＋別人的服務 id
            (self.stranger, self.service, self.workspace.id),             # 不是成員
        )
        for member, service, workspace_id in cases:
            with self.subTest(member=member.login, service=service.name, workspace=workspace_id):
                result, fetch = self._call(member, service, workspace_id=workspace_id)
                self.assertFalse(result['success'])
                self.assertNotIn('data', result)
                fetch.assert_not_called()

    def test_service_without_the_capability_is_not_found_and_not_probed(self):
        result, fetch = self._call(self.owner, self.plain)
        self.assertFalse(result['success'])
        fetch.assert_not_called()

    def test_not_running_is_unknown_without_a_request(self):
        for state in ('deploying', 'initializing', 'upgrading', 'error', 'suspended'):
            with self.subTest(state=state):
                service = self._service(self.workspace, self.nextcloud, 'st-' + state, state=state)
                result, fetch = self._call(self.owner, service, (200, _healthz(mcp='not_configured')))
                self.assertTrue(result['success'], result)
                self.assertEqual(result['data']['state'], 'unknown')
                fetch.assert_not_called()

    def test_caller_supplied_parameters_are_refused_before_anything_else(self):
        """網址一律由平台算；呼叫端多送任何參數（包括網址、繞過快取的旗標）整個拒絕、不發請求。"""
        for params in ({'url': 'https://evil.example/healthz'}, {'admin_url': 'http://169.254.169.254'},
                       {'subdomain': 'paas-cs-other'}, {'fresh': True}, {'paas_domain': 'evil.example'}):
            with self.subTest(params=params):
                result, fetch = self._call(self.owner, self.service, **params)
                self.assertFalse(result['success'])
                fetch.assert_not_called()

    def test_read_does_not_write_the_ledger(self):
        fields_ = ['state', 'helm_values', 'write_date', 'helm_revision', 'error_message']
        before = self.service.read(fields_)[0]
        self._call(self.owner, self.service, (200, _healthz(mcp='not_configured')))
        self.assertEqual(self.service.read(fields_)[0], before)

    def test_detail_payload_exposes_only_the_capability_flag(self):
        """狀態不進 payload（服務頁即時問），payload 只帶 gate。"""
        self.authenticate(self.owner.login, self.owner.login)
        flags = {}
        for service in (self.service, self.plain):
            with patch(_FETCH) as fetch:
                response = self.url_open(
                    '/api/workspaces/%s/services/%s' % (self.workspace.id, service.id),
                    data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {}}),
                    headers={'Content-Type': 'application/json'})
            data = response.json()['result']['data']
            flags[service.name] = data['has_mcp_setup_status']
            fetch.assert_not_called()
            self.assertNotIn('not_configured', json.dumps(data))
        self.assertEqual(flags, {'nc': True, 'pg': False})


@tagged('post_install', '-at_install')
class TestMcpSetupCreateResponse20261027(TransactionCase):
    """第五輪 D2-B：開通回應帶 ``has_mcp_setup_status``（一次性憑證視窗「先到 Connection 存好連線」的 gate）。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.env['ir.config_parameter'].sudo().set_param('woow_paas_platform.paas_domain', _TEST_DOMAIN)
        self.workspace = self.env['woow_paas_platform.workspace'].create({'name': 'mcp setup create r5'})
        self.member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'mcp setup create admin', 'login': 'mcp-setup-create-r5-admin',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': self.member.id, 'role': ROLE_ADMIN})

    def _create(self, template_xmlid, plan_xmlid, name):
        with patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                   new=MagicMock(env=self.env(user=self.member))), patch(_FETCH) as fetch:
            result = self.controller._create_service(
                workspace=self.workspace,
                template_id=self.env.ref('woow_paas_platform.' + template_xmlid).id,
                name=name, values={},
                plan_id=self.env.ref('woow_paas_platform.' + plan_xmlid).id,
            )
        fetch.assert_not_called()   # 開通當下不探測（服務還沒起來）
        self.assertTrue(result.get('success'), result.get('error'))
        return result['data']

    def test_mcp_family_creation_carries_the_flag_with_the_admin_page_url(self):
        data = self._create('cloud_app_mcp_nextcloud', 'plan_mcp_nextcloud_monthly', 'Nextcloud MCP r5')
        self.assertIs(data.get('has_mcp_setup_status'), True)
        self.assertTrue(data.get('admin_url', '').endswith('.' + _TEST_DOMAIN))

    def test_other_templates_carry_false(self):
        """hermes-agent 也有 admin GUI、一樣交付一次性密碼，但載體是 helm_secret——不是 mcp-server 家族。"""
        data = self._create('cloud_app_hermes_agent', 'plan_hermes_agent_monthly', 'Hermes r5')
        self.assertTrue(data.get('admin_password'), '前提：這也是會開一次性憑證視窗的模板')
        self.assertIs(data.get('has_mcp_setup_status'), False)
        self.assertNotIn('admin_url', data)


@tagged('post_install', '-at_install')
class TestMcpSetupFrontend20261027(BaseCase):
    """靜態守線（行為另由 tests/ui 的 node 測試驗）：gate 讀後端旗標、不用 slug；譯文齊而且送得到前端。"""

    _TENANT_FILES = ('utils/mcp_setup_status.js', 'components/modal/OneTimeCredentialsModal.js')

    def _read(self, rel):
        return (_STATIC / rel).read_text(encoding='utf-8')

    def _new_msgids(self):
        """本功能的 tenant 字串：utils 全部＋開通視窗新加的那一句。"""
        found = re.findall(r'_t\(\s*"((?:[^"\\]|\\.)*)"', self._read('utils/mcp_setup_status.js'))
        modal = self._read('components/modal/OneTimeCredentialsModal.js')
        hint = re.search(r'export function oneTimeMcpSetupHint[\s\S]*?_t\(\s*"((?:[^"\\]|\\.)*)"', modal)
        self.assertTrue(hint, '開通視窗的那一句要走 _t')
        return found + [hint.group(1)]

    def test_the_gate_reads_the_backend_flag_not_a_slug(self):
        xml = self._read('pages/service/ServiceDetailPage.xml')
        page = self._read('pages/service/ServiceDetailPage.js')
        self.assertRegex(xml, r'<McpSetupStatusNotice\s+t-if="mcpSetupCondition"')
        self.assertIn('this.service?.has_mcp_setup_status === true', page)
        self.assertRegex(page, r'get mcpSetupEnabled\(\) \{\s+return this\.hasMcpSetupStatus && '
                               r'this\.service\?\.state === "running";')
        self.assertIn('data.has_mcp_setup_status === true',
                      self._read('components/modal/OneTimeCredentialsModal.js'))
        for rel in ('components/mcp-setup-status/McpSetupStatusNotice.js',
                    'components/mcp-setup-status/McpSetupStatusNotice.xml',
                    'services/mcp_setup_status.js', 'utils/mcp_setup_status.js',
                    'pages/service/tabs/OverviewTab.js'):
            text = self._read(rel)
            with self.subTest(rel=rel):
                self.assertIsNone(re.search(r'''['"]mcp-[a-z0-9-]+['"]''', text),
                                  '前端不得用 slug 判斷能力（capability-gate-single-source-field）')

    def test_the_poller_asks_the_platform_route_only(self):
        hook = self._read('services/mcp_setup_status.js')
        self.assertIn('/services/${getServiceId()}/mcp-status`', hook)
        self.assertNotIn('/healthz', hook, '前端不直接打服務；網址由平台伺服器端查')
        controller = (_ADDON / 'controllers' / 'paas.py').read_text(encoding='utf-8')
        self.assertIn('/api/workspaces/<int:workspace_id>/services/<int:service_id>/mcp-status', controller)

    def test_every_new_tenant_string_has_a_zh_tw_translation(self):
        po = polib.pofile(str(_ADDON / 'i18n' / 'zh_TW.po'))
        entries = {entry.msgid: entry for entry in po if entry.msgstr and not entry.obsolete}
        msgids = self._new_msgids()
        self.assertEqual(len(msgids), 11)
        for msgid in msgids:
            with self.subTest(msgid=msgid):
                self.assertIn(msgid, entries, 'zh_TW.po 缺少：%s' % msgid)
        self.assertEqual(
            entries['MCP is not connected yet: open the admin page → Connection, fill it in and '
                    'save. MCP starts providing tools only after that.'].msgstr,
            'MCP 尚未設定連線：請到管理頁 → Connection 填好並儲存，MCP 才會開始提供工具。')
        self.assertEqual(entries['The MCP server is not running. Check Logs on the admin page.'].msgstr,
                         'MCP 服務程式沒有在運作，請到管理頁查看 Logs。')
        self.assertEqual(entries['NEEDS SETUP'].msgstr, '待設定')
        self.assertEqual(entries['MCP NOT RUNNING'].msgstr, 'MCP 未運作')
        self.assertEqual(entries['BACKEND NOT SET'].msgstr, '後端未設定')

    def test_every_new_entry_is_marked_odoo_javascript(self):
        """第五輪 D3-B（第四輪 review 變異 S）：拿掉 ``#. odoo-javascript``，31 條測試仍全綠、前端卻變回英文。"""
        po = polib.pofile(str(_ADDON / 'i18n' / 'zh_TW.po'))
        entries = {entry.msgid: entry for entry in po if not entry.obsolete}
        for msgid in self._new_msgids():
            with self.subTest(msgid=msgid):
                self.assertIn('odoo-javascript', entries[msgid].comment.split('\n'),
                              'Odoo 只把帶 odoo-javascript 的條目送給前端')
                self.assertTrue(any(path.startswith('code:addons/woow_paas_platform/static/src/')
                                    for path, _line in entries[msgid].occurrences))

    def test_the_translations_actually_reach_the_web_client(self):
        """Odoo 自己的載入路徑（``code_translations.get_web_translations``）：前端拿到的就是這些。"""
        messages = {m['id']: m['string'] for m in
                    code_translations.get_web_translations('woow_paas_platform', 'zh_TW')['messages']}
        po = polib.pofile(str(_ADDON / 'i18n' / 'zh_TW.po'))
        expected = {entry.msgid: entry.msgstr for entry in po if entry.msgstr}
        for msgid in self._new_msgids():
            with self.subTest(msgid=msgid):
                self.assertEqual(messages.get(msgid), expected[msgid])

    def test_no_engineering_jargon_in_the_tenant_copy(self):
        """D2-B：not_running 不再寫「子行程」（child process）。"""
        po = polib.pofile(str(_ADDON / 'i18n' / 'zh_TW.po'))
        entries = {entry.msgid: entry for entry in po if entry.msgstr}
        for msgid in self._new_msgids():
            with self.subTest(msgid=msgid):
                self.assertNotIn('child process', msgid.lower())
                self.assertNotIn('子行程', entries[msgid].msgstr)
        self.assertFalse([e.msgid for e in po if 'MCP child process' in e.msgid],
                         '換掉的舊條目不要留在 po 裡')
