"""Security Access route 健康檢查：正式站還沒實測過的五種結果（#1234 補測）。

正式站 2026-09-29 實測過 ok／access_login／tunnel_down／dns_missing／
origin_error／proxy_no_site。本檔補另外五種，每一種都是**一個觸發條件成立時的
正向斷言＋一個條件不成立時不會誤判的對照**：

======================  ===========================================================
狀態                     觸發條件（``services/route_probe.py``）
======================  ===========================================================
``access_missing``       route 設為保護（``is_protected=True``），回應卻**直接來自
                         origin**：不是導去 ``*.cloudflareaccess.com``、不是 CF 的
                         tunnel／DNS 錯誤碼、也不是 5xx（``classify`` 的
                         ``if is_protected`` 分支；排在 proxy 預設頁判斷之前）
``access_unexpected``    route 設為公開，回應卻是 3xx 且 ``Location`` 的 host 以
                         ``.cloudflareaccess.com`` 結尾（#1239：平台不管的 Access）
``not_routed``           ``Server: cloudflare`` 且狀態碼 ≥ 400，body 帶 Cloudflare
                         錯誤碼 1016／1001／1014（``_CF_CODE_STATUS``）
``timeout``              ``_fetch`` 拋 ``requests.Timeout`` 或
                         ``urllib3.exceptions.TimeoutError``（含子類），或整批超過
                         ``_BATCH_DEADLINE`` 還沒回來
``unknown``              探測本身失敗且不屬於上面任一種：非 DNS 的
                         ``ConnectionError``、edge IP 拒絕連線（``NewConnectionError``）、
                         其他任何例外、hostname 不合格式、
                         executor 排不進去。⚠ ``classify`` 對**任何 HTTP 回應**
                         都不會回 ``unknown``（本檔 ``test_classify_never_yields_
                         unknown`` 鎖住）
======================  ===========================================================

「建議文字」不在後端：API 只回 ``status``／``healthy``／``http_code``／
``cf_error_code``／``checked_at``，文案與紅黃配色在
``static/src/paas/pages/security-access/SecurityAccessDetailPage.js`` 的
``ROUTE_HEALTH_LABELS``／``ROUTE_HEALTH_HINTS``／``WARN_HEALTH_STATUSES``。
``TestRouteHealthFrontendContract`` 以靜態讀檔守「後端每一種狀態前端都有標籤與
建議文字」——少一格時前端會靜默退回 ``unknown`` 的文案（``|| ROUTE_HEALTH_HINTS.
unknown``），畫面不會壞、也沒有任何東西會紅。

既有 ``test_sa_route_health_1234.py`` 已涵蓋的格子（1016、302＋公開、200＋保護、
ReadTimeout、批次截止、ValueError、格式不合的 hostname）不重複寫；本檔只補其他
錯誤碼／其他例外型別／對照組，以及「經過端點、``is_protected`` 取自 DB」這一層。

網路一律 patch：``route_probe._fetch``（單一 hostname 的 GET）是唯一的 IO 縫，
endpoint 層 patch 它而不是 ``probe_routes``，讓真正的 ``classify`` 與
``_probe_one`` 跑在端點後面。
"""
import json
import os
import re
import uuid
from unittest.mock import MagicMock, patch

import requests
import urllib3

from odoo.tests.common import HttpCase, TransactionCase, tagged
from odoo.tools import mute_logger

from odoo.addons.woow_paas_platform.models.security_access import (
    _skip_route_sync,
)
from odoo.addons.woow_paas_platform.services import route_probe

_CF = {'Server': 'cloudflare', 'Content-Type': 'text/html'}
_ACCESS_LOGIN = (
    'https://woowtech.cloudflareaccess.com/cdn-cgi/access/login/'
    'woowsa-x.woowtech.io?kid=abc&redirect_url=%2F')
_LOG_PROBE = 'odoo.addons.woow_paas_platform.services.route_probe'


def _read_timeout():
    return urllib3.exceptions.ReadTimeoutError(
        None, '/', 'Read timed out. (read timeout=5.0)')


@tagged('post_install', '-at_install')
class TestRouteHealthUncoveredStates(TransactionCase):
    """回應／例外 → 狀態。純函式層，不碰 DB。"""

    def setUp(self):
        super().setUp()
        route_probe._cache.clear()

    def _classify(self, code, headers=None, body='', protected=False):
        return route_probe.classify(code, headers or {}, body, protected)

    def _probe(self, exc=None, response=None, protected=False):
        """跑真的 ``_probe_one``，只把 ``_fetch`` 換掉。"""
        kw = {'side_effect': exc} if exc is not None else {'return_value': response}
        with patch.object(route_probe, '_fetch', **kw):
            return route_probe._probe_one('x.woowtech.io', protected)

    def _assert_unhealthy(self, result, status, http_code=None, cf_code=None):
        self.assertEqual(result['status'], status, result)
        self.assertFalse(result['healthy'], '%s 不是健康狀態' % status)
        self.assertEqual(result['http_code'], http_code)
        self.assertEqual(result['cf_error_code'], cf_code)
        self.assertTrue(result['checked_at'], '每個結果都要帶檢查時間')

    # ================================================================== #
    #  1. access_missing                                                  #
    # ================================================================== #

    def test_access_missing_for_any_origin_answer_on_protected_route(self):
        """設了保護，卻拿到 origin 自己的回應（2xx／3xx 導去自家登入頁／4xx）
        ⇒ 前面沒有 Access 登入頁。http_code 照實帶出。"""
        cases = [
            (200, {}, ''),
            (204, {'Server': 'cloudflare'}, ''),
            (302, {'Location': 'https://woowsa-x.woowtech.io/login'}, ''),
            (401, {'Server': 'cloudflare'}, 'Unauthorized'),
            (404, {'Server': 'cloudflare'}, 'Not Found'),
        ]
        for code, headers, body in cases:
            with self.subTest(code=code):
                self._assert_unhealthy(
                    self._classify(code, headers, body, protected=True),
                    'access_missing', http_code=code)

    def test_access_missing_outranks_proxy_default_page(self):
        """保護沒生效比「反向代理沒有站台」嚴重：同一個 NPM 預設頁，保護中的
        route 要報 access_missing，公開的 route 才是 proxy_no_site。"""
        body = '<html><head><title>Default Site</title></head></html>'
        self.assertEqual(
            self._classify(200, _CF, body, protected=True)['status'],
            'access_missing')
        self.assertEqual(
            self._classify(200, _CF, body, protected=False)['status'],
            'proxy_no_site', '對照：公開 route 同一個回應不得被說成 access_missing')

    def test_access_missing_control_cases_on_protected_route(self):
        """對照：保護中的 route 只要回應**不是** origin 直接給的，就不是
        access_missing——Access 登入頁＝正常；tunnel／DNS／5xx 各歸各的。"""
        cases = [
            (302, {'Location': _ACCESS_LOGIN}, '', 'access_login'),
            (530, _CF, 'error code: 1033', 'tunnel_down'),
            (530, _CF, 'error code: 1016', 'not_routed'),
            (502, _CF, '<center>openresty</center>', 'origin_error'),
        ]
        for code, headers, body, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    self._classify(code, headers, body, protected=True)['status'],
                    expected)

    def test_public_route_answering_directly_is_not_access_missing(self):
        """對照：同樣是 200，公開 route 就是 ok。"""
        self.assertEqual(self._classify(200, {}, '', protected=False)['status'], 'ok')

    def test_probe_routes_forwards_the_protection_flag(self):
        """``probe_routes`` 把每條 route 自己的 is_protected 交給分類——同一個
        回應，保護中的報 access_missing、公開的報 ok。"""
        with patch.object(route_probe, '_fetch', return_value=(200, {}, '')):
            res = route_probe.probe_routes(
                [('p.woowtech.io', True), ('q.woowtech.io', False)], force=True)
        self.assertEqual(res['p.woowtech.io']['status'], 'access_missing')
        self.assertEqual(res['q.woowtech.io']['status'], 'ok')

    # ================================================================== #
    #  2. access_unexpected                                               #
    # ================================================================== #

    def test_access_unexpected_for_every_redirect_code_to_access(self):
        """公開 route 被任何一種 3xx 導去 ``*.cloudflareaccess.com``
        ⇒ access_unexpected；host 大小寫不影響。"""
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code):
                self._assert_unhealthy(
                    self._classify(code, {'Location': _ACCESS_LOGIN}),
                    'access_unexpected', http_code=code)
        upper = _ACCESS_LOGIN.replace('woowtech.cloudflareaccess.com',
                                      'WoowTech.CloudflareAccess.COM')
        self.assertEqual(
            self._classify(302, {'location': upper})['status'], 'access_unexpected',
            'header 名與 host 都要大小寫不敏感')

    def test_lookalike_or_relative_redirect_is_not_access_unexpected(self):
        """對照：只有 host **以 ``.cloudflareaccess.com`` 結尾**才算。仿冒網域、
        相對路徑（origin 自己的 ``/cdn-cgi/access/login``）、沒有 Location 的
        3xx、body 裡提到 Access 的 200，都不是。"""
        cases = [
            (302, {'Location': 'https://cloudflareaccess.com.evil.example/login'}, ''),
            (302, {'Location': 'https://evilcloudflareaccess.com/login'}, ''),
            (302, {'Location': '/cdn-cgi/access/login/woowsa-x.woowtech.io'}, ''),
            (302, {}, ''),
            (200, {}, '<a href="https://x.cloudflareaccess.com/">login</a>'),
        ]
        for code, headers, body in cases:
            with self.subTest(headers=headers, body=body[:20]):
                self.assertEqual(
                    self._classify(code, headers, body, protected=False)['status'],
                    'ok')

    def test_access_redirect_on_protected_route_is_login_not_unexpected(self):
        """對照：同一個導向，route 設為保護就是正常的 access_login（healthy）。"""
        result = self._classify(302, {'Location': _ACCESS_LOGIN}, protected=True)
        self.assertEqual(result['status'], 'access_login')
        self.assertTrue(result['healthy'])

    # ================================================================== #
    #  3. not_routed                                                      #
    # ================================================================== #

    def test_not_routed_for_each_cloudflare_origin_dns_code(self):
        """1016（Origin DNS error）／1001（DNS resolution）／1014（CNAME
        cross-user）三碼，純文字與 HTML 頁內 ``errorCode:`` 兩種寫法都認；
        cf_error_code 帶回整數。"""
        for cf_code in (1016, 1001, 1014):
            for body in ('error code: %d' % cf_code,
                         '<script>a={properties:{errorCode: %d }}</script>' % cf_code):
                with self.subTest(cf_code=cf_code, body=body[:12]):
                    self._assert_unhealthy(
                        self._classify(530, _CF, body),
                        'not_routed', http_code=530, cf_code=cf_code)

    def test_not_routed_also_on_non_530_cloudflare_error_status(self):
        """碼才是判準，狀態碼只要 ≥ 400：CF 用 409／404 回這幾碼時也一樣。"""
        for code in (404, 409):
            with self.subTest(code=code):
                self.assertEqual(
                    self._classify(code, _CF, 'error code: 1014')['status'],
                    'not_routed')

    def test_not_routed_code_ignored_unless_cloudflare_error_response(self):
        """對照：碼只在「Cloudflare 自己回的錯誤頁」才可信。

        * origin（非 cloudflare server）頁面剛好寫了 1016 ⇒ 不是 not_routed
          （404 → ok；530 → 依 530 規則歸 tunnel_down）。
        * Cloudflare 回 2xx 但 body 有那串字 ⇒ 不讀碼 ⇒ ok。
        * 1033 是 tunnel_down，不是 not_routed。
        """
        self.assertEqual(
            self._classify(404, {'Server': 'nginx'}, 'error code: 1016')['status'], 'ok')
        self.assertEqual(
            self._classify(530, {'Server': 'nginx'}, 'error code: 1016')['status'],
            'tunnel_down')
        self.assertEqual(
            self._classify(200, _CF, 'error code: 1016')['status'], 'ok')
        self.assertEqual(
            self._classify(530, _CF, 'error code: 1033')['status'], 'tunnel_down')

    def test_unmapped_cloudflare_code_is_not_routed(self):
        """對照：不在對照表裡的 CF 碼（如 1020 防火牆）不得被說成 not_routed。"""
        self.assertNotEqual(
            self._classify(403, _CF, 'error code: 1020')['status'], 'not_routed')

    # ================================================================== #
    #  4. timeout                                                         #
    # ================================================================== #

    def test_timeout_for_every_timeout_exception_type(self):
        """DoH 有答案時走 ``_fetch_via_ip``（urllib3，``retries=False`` ⇒ 原樣
        拋 urllib3 的例外）；DoH 不可用時走 requests。兩邊的連線逾時與讀取逾時
        都要歸 timeout，http_code／cf_error_code 為 None。"""
        cases = [
            _read_timeout(),
            urllib3.exceptions.ConnectTimeoutError(None, 'connect timed out'),
            requests.exceptions.ConnectTimeout('connect timed out'),
            requests.exceptions.ReadTimeout('read timed out'),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                self._assert_unhealthy(self._probe(exc=exc), 'timeout')

    def test_timeout_result_is_cached_but_batch_straggler_is_not(self):
        """請求本身逾時（``_probe_one`` 回 timeout）是一個完成的結果，進快取；
        只有「超過批次截止還沒回來」那種不進快取（既有測試鎖住後者）。"""
        with patch.object(route_probe, '_fetch', side_effect=_read_timeout()):
            res = route_probe.probe_routes([('t.woowtech.io', False)], force=True)
        self.assertEqual(res['t.woowtech.io']['status'], 'timeout')
        self.assertIn('t.woowtech.io', route_probe._cache)

    def test_non_timeout_failures_are_not_timeout(self):
        """對照：拒絕連線（requests 路徑）→ unknown；DNS 查無 → dns_missing；
        CF 504／524（origin 逾時，但請求**有**到 Cloudflare 並拿到回應）→
        origin_error。都不是「沒回應」。"""
        self.assertEqual(
            self._probe(exc=requests.exceptions.ConnectionError(
                '[Errno 111] Connection refused'))['status'], 'unknown')
        self.assertEqual(
            self._probe(exc=route_probe._DnsMissing('x.woowtech.io'))['status'],
            'dns_missing')
        for code in (504, 524):
            with self.subTest(code=code):
                self.assertEqual(
                    self._classify(code, _CF, 'error code: 524')['status'],
                    'origin_error')

    def test_edge_ip_connection_refused_is_unknown_not_timeout(self):
        """``urllib3.exceptions.NewConnectionError`` 是 ``ConnectTimeoutError`` 的
        子類（urllib3 1.26 與 2.x 皆然）。DoH 路徑上「edge IP 拒絕連線」曾被
        ``except (requests.Timeout, urllib3.TimeoutError)`` 吃成 timeout；同一件事
        走 requests 路徑是 unknown（見上一條）。兩條路徑必須一致。

        對照：真正的連線逾時（``ConnectTimeoutError`` 本身）仍是 timeout。
        """
        self.assertTrue(issubclass(urllib3.exceptions.NewConnectionError,
                                   urllib3.exceptions.TimeoutError),
                        '前提：urllib3 的類別階層（不成立時本條失去鑑別力）')
        exc = urllib3.exceptions.NewConnectionError(
            None, 'Failed to establish a new connection: [Errno 111] '
                  'Connection refused')
        self.assertEqual(self._probe(exc=exc)['status'], 'unknown')
        self.assertEqual(
            self._probe(exc=urllib3.exceptions.ConnectTimeoutError(
                None, 'connect timed out'))['status'], 'timeout')

    # ================================================================== #
    #  5. unknown                                                         #
    # ================================================================== #

    def test_unknown_for_unclassifiable_probe_failures(self):
        """非 DNS 的 ConnectionError、TLS 驗證失敗、其他例外 ⇒ unknown，
        而且不會往外拋。"""
        cases = [
            requests.exceptions.ConnectionError('[Errno 111] Connection refused'),
            requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED'),
            urllib3.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED'),
            urllib3.exceptions.ProtocolError('Connection aborted.'),
            OSError('Network is unreachable'),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__), mute_logger(_LOG_PROBE):
                self._assert_unhealthy(self._probe(exc=exc), 'unknown')

    def test_unexpected_exception_is_logged_with_hostname(self):
        """其他例外 ⇒ unknown，且留一行指名 hostname 與例外型別的 warning
        （探針炸掉不能是無聲的）。"""
        with self.assertLogs(_LOG_PROBE, level='WARNING') as logs:
            result = self._probe(exc=RuntimeError('boom'))
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(
            any('x.woowtech.io' in m and 'RuntimeError' in m for m in logs.output),
            logs.output)

    def test_unknown_when_executor_cannot_schedule(self):
        """interpreter 收攤中 executor 排不進去 ⇒ unknown，不 raise、不打網路。"""
        broken = MagicMock()
        broken.submit.side_effect = RuntimeError(
            'cannot schedule new futures after interpreter shutdown')
        with patch.object(route_probe, '_executor', return_value=broken), \
                patch.object(route_probe, '_fetch') as fetch, \
                self.assertLogs(_LOG_PROBE, level='WARNING'):
            res = route_probe.probe_routes([('u.woowtech.io', False)], force=True)
        self.assertEqual(res['u.woowtech.io']['status'], 'unknown')
        fetch.assert_not_called()

    def test_dns_failure_texts_are_not_unknown(self):
        """對照：requests 的 ConnectionError 帶任一種「查無此名」字樣
        ⇒ dns_missing，不是 unknown。"""
        for text in ("Failed to resolve 'x' (NameResolutionError)",
                     '[Errno -2] Name or service not known',
                     'nodename nor servname provided, or not known',
                     'getaddrinfo failed'):
            with self.subTest(text=text):
                self.assertEqual(
                    self._probe(exc=requests.exceptions.ConnectionError(text))['status'],
                    'dns_missing')

    def test_classify_never_yields_unknown(self):
        """``unknown`` 只來自「探測本身失敗」：任何 HTTP 回應（100–599 × 是否
        CF × 是否保護）都會被分到其他十種之一。"""
        seen = set()
        for code in range(100, 600):
            for headers in ({}, _CF):
                for protected in (False, True):
                    status = self._classify(code, headers, '', protected)['status']
                    seen.add(status)
                    self.assertNotEqual(
                        status, 'unknown',
                        'classify(%s, cf=%s, protected=%s)' % (code, bool(headers), protected))
        self.assertTrue(seen <= set(route_probe.STATUSES), seen)


def _new_user(env, login):
    return env['res.users'].sudo().create({
        'name': login, 'login': login, 'password': login,
        'email': '%s@test.example' % login,
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestRouteHealthStatesViaEndpoint(HttpCase):
    """routes 端點 ``health`` action，後面跑真的 ``probe_routes``／``classify``。

    每條測試都在**同一次呼叫**裡放一條「條件成立」與一條「條件不成立」的
    route：對照組與受測組共用同一支 patch，patch 沒接上時兩條會一起錯。
    """

    def setUp(self):
        super().setUp()
        route_probe._cache.clear()
        suffix = uuid.uuid4().hex[:6]
        self.suffix = suffix
        self.member = _new_user(self.env, 'sa_hs_user_%s' % suffix)
        owner = _new_user(self.env, 'sa_hs_owner_%s' % suffix)
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'SA HS WS %s' % suffix, 'owner_id': owner.id})
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id, 'user_id': self.member.id,
            'role': 'user'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create(
            {'name': 'HS GW %s' % suffix, 'workspace_id': self.workspace.id})

    def _route(self, tag, protected=False):
        # SA 沒有 tunnel，sync 本來就被 liveness gate 擋住；再加
        # `_skip_route_sync`（伺服器端 sentinel）讓 fixture 完全不碰 operator。
        return _skip_route_sync(
            self.env['woow_paas_platform.security_access_route'].sudo()).create({
                'security_access_id': self.sa.id,
                'subdomain_prefix': 'hs-%s-%s' % (tag, self.suffix),
                'is_protected': protected})

    def _health(self, fetch):
        self.authenticate(self.member.login, self.member.login)
        with patch.object(route_probe, '_fetch', side_effect=fetch):
            resp = self.url_open(
                '/api/workspaces/%s/security-accesses/%s/routes'
                % (self.workspace.id, self.sa.id),
                data=json.dumps({'jsonrpc': '2.0', 'method': 'call',
                                 'params': {'action': 'health', 'force': True}}),
                headers={'Content-Type': 'application/json'})
        body = resp.json()
        self.assertNotIn('error', body, body.get('error'))
        result = body['result']
        self.assertTrue(result.get('success'), result)
        return result['data']

    def _assert_shape(self, item):
        self.assertEqual(
            set(item), {'status', 'healthy', 'http_code', 'cf_error_code', 'checked_at'},
            'API 回傳欄位（前端 routeHealthLabel／Hint／Class 讀的就是這幾個）')

    def test_access_missing_uses_is_protected_from_db(self):
        """兩條 route 收到同一個 200：DB 上 is_protected=True 的報 access_missing、
        公開的報 ok。呼叫端沒辦法影響 is_protected（參數只有 force）。"""
        guarded = self._route('am-p', protected=True)
        public = self._route('am-o', protected=False)
        data = self._health(lambda host: (200, {'Server': 'cloudflare'}, '<h1>app</h1>'))
        self._assert_shape(data[str(guarded.id)])
        self.assertEqual(data[str(guarded.id)]['status'], 'access_missing')
        self.assertFalse(data[str(guarded.id)]['healthy'])
        self.assertEqual(data[str(guarded.id)]['http_code'], 200)
        self.assertEqual(data[str(public.id)]['status'], 'ok')
        self.assertTrue(data[str(public.id)]['healthy'])

    def test_access_unexpected_only_on_public_route(self):
        """兩條 route 都被導去 Access：公開的報 access_unexpected、保護中的報
        access_login。"""
        public = self._route('au-o', protected=False)
        guarded = self._route('au-p', protected=True)
        data = self._health(lambda host: (302, {'Location': _ACCESS_LOGIN}, ''))
        self.assertEqual(data[str(public.id)]['status'], 'access_unexpected')
        self.assertFalse(data[str(public.id)]['healthy'])
        self.assertEqual(data[str(public.id)]['http_code'], 302)
        self.assertEqual(data[str(guarded.id)]['status'], 'access_login')
        self.assertTrue(data[str(guarded.id)]['healthy'])

    def test_not_routed_carries_cf_error_code(self):
        """1016 ⇒ not_routed 並帶 cf_error_code；同批 1033 ⇒ tunnel_down。"""
        a = self._route('nr-a')
        b = self._route('nr-b')
        bodies = {a.hostname: 'error code: 1016', b.hostname: 'error code: 1033'}
        data = self._health(lambda host: (530, _CF, bodies[host]))
        self.assertEqual(data[str(a.id)]['status'], 'not_routed')
        self.assertEqual(data[str(a.id)]['cf_error_code'], 1016)
        self.assertEqual(data[str(a.id)]['http_code'], 530)
        self.assertFalse(data[str(a.id)]['healthy'])
        self.assertEqual(data[str(b.id)]['status'], 'tunnel_down')
        self.assertEqual(data[str(b.id)]['cf_error_code'], 1033)

    def test_timeout_has_no_http_code(self):
        """逾時那條 ⇒ timeout、http_code／cf_error_code 為 null；同批另一條照常 ok。"""
        slow = self._route('to-s')
        fine = self._route('to-f')

        def fetch(host):
            if host == slow.hostname:
                raise _read_timeout()
            return 200, {}, ''
        data = self._health(fetch)
        item = data[str(slow.id)]
        self._assert_shape(item)
        self.assertEqual(item['status'], 'timeout')
        self.assertIsNone(item['http_code'])
        self.assertIsNone(item['cf_error_code'])
        self.assertFalse(item['healthy'])
        self.assertEqual(data[str(fine.id)]['status'], 'ok')

    def test_unknown_for_unclassifiable_failure(self):
        """TLS 驗證失敗那條 ⇒ unknown（端點仍回 success）；同批另一條照常 ok。"""
        bad = self._route('uk-b')
        fine = self._route('uk-f')

        def fetch(host):
            if host == bad.hostname:
                raise urllib3.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED')
            return 200, {}, ''
        with self.assertLogs(_LOG_PROBE, level='WARNING'):
            data = self._health(fetch)
        self.assertEqual(data[str(bad.id)]['status'], 'unknown')
        self.assertIsNone(data[str(bad.id)]['http_code'])
        self.assertFalse(data[str(bad.id)]['healthy'])
        self.assertEqual(data[str(fine.id)]['status'], 'ok')


@tagged('post_install', '-at_install')
class TestRouteHealthFrontendContract(TransactionCase):
    """後端狀態 ↔ 前端標籤／建議文字／配色的對帳（靜態讀檔）。

    前端查不到狀態時會退回 ``unknown`` 的標籤與文案（``|| ROUTE_HEALTH_LABELS.
    unknown``），畫面不壞、也沒有 console 錯誤——後端多一種狀態或前端少一格，
    使用者只會看到一句不相干的「The check could not complete」。
    """

    _PAGE = ('pages', 'security-access', 'SecurityAccessDetailPage.js')

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = os.path.join(os.path.dirname(__file__), '..', 'static', 'src',
                            'paas', *cls._PAGE)
        with open(path, encoding='utf-8') as handle:
            cls.source = handle.read()

    def _object(self, name):
        """``const NAME = { key: _t("..."), ... };`` → ``{key: text}``。"""
        m = re.search(r'const %s = \{(.*?)\n\};' % name, self.source, re.S)
        self.assertTrue(m, '找不到 %s' % name)
        entries = re.findall(r'^\s*(\w+):\s*_t\(\s*"((?:[^"\\]|\\.)*)"', m.group(1), re.M)
        keys = re.findall(r'^\s*(\w+):', m.group(1), re.M)
        self.assertEqual(
            [k for k, _v in entries], keys,
            '%s 每一格都要是 _t("...")（#1261：裸字串不會被翻譯）' % name)
        return dict(entries)

    def _warn(self):
        m = re.search(r'const WARN_HEALTH_STATUSES = \[(.*?)\];', self.source, re.S)
        self.assertTrue(m, '找不到 WARN_HEALTH_STATUSES')
        return set(re.findall(r'"(\w+)"', m.group(1)))

    def test_every_backend_status_has_label_and_hint(self):
        statuses = set(route_probe.STATUSES)
        for name in ('ROUTE_HEALTH_LABELS', 'ROUTE_HEALTH_HINTS'):
            with self.subTest(table=name):
                table = self._object(name)
                self.assertEqual(set(table), statuses,
                                 '%s 必須與 route_probe.STATUSES 一一對應' % name)
                self.assertTrue(all(v.strip() for v in table.values()))

    def test_target_states_have_actionable_hints(self):
        """本次補測的五種，建議文字要說得出「該做什麼」（鎖關鍵詞，不鎖全文）。"""
        hints = self._object('ROUTE_HEALTH_HINTS')
        expected = {
            'access_missing': 'Access tab',
            'access_unexpected': 'Contact support',
            'not_routed': 'check again',
            'timeout': 'running',
            'unknown': 'Try again',
        }
        for status, phrase in expected.items():
            with self.subTest(status=status):
                self.assertIn(phrase, hints[status])

    def test_warn_vs_error_colouring(self):
        """黃色＝多半等一下就好；紅色＝要動手。配色由前端依 ``healthy`` 與
        ``WARN_HEALTH_STATUSES`` 決定。"""
        warn = self._warn()
        self.assertTrue(warn <= set(route_probe.STATUSES), warn)
        self.assertFalse(warn & route_probe.HEALTHY, '健康狀態不該出現在警告清單')
        for status in ('not_routed', 'timeout', 'unknown'):
            self.assertIn(status, warn, '%s 應為黃色（等一下再查）' % status)
        for status in ('access_missing', 'access_unexpected'):
            self.assertNotIn(status, warn, '%s 是安全設定不符，應為紅色' % status)
            self.assertNotIn(status, route_probe.HEALTHY)
