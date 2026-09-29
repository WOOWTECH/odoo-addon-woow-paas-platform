"""Security Access route 健康探測（#1234 第一步）。

兩層：

* ``TestRouteProbeClassify`` —— ``services/route_probe.py`` 的分類與批次行為。
  分類用的回應樣本取自 2026-09-29 正式站實測（Cloudflare 1033 的 HTML 頁、
  ``error code: 1016`` 純文字、Access 登入頁的 302），網路一律 patch 掉。
* ``TestRouteHealthEndpoint`` —— routes 端點的 ``health`` action：權限與
  ``list`` 相同（workspace 成員即可）、探測目標只取自 DB 的 route.hostname、
  ``to_dict()`` 帶 ``route_quota``。
"""
import json
import time
import uuid
from unittest.mock import patch

import requests

from odoo.tests.common import HttpCase, TransactionCase, tagged

from odoo.addons.woow_paas_platform.services import route_probe

# 正式站 Odoo pod 以 requests 探到的 1033 頁（節錄）：錯誤碼只出現在頁內 JS 的
# `errorCode: 1033`，標題是「Cloudflare Tunnel error」。
_CF_1033_HTML = (
    '<!doctype html><html><head><title>Cloudflare Tunnel error | '
    'woowsa-npm.woowtech.io | Cloudflare</title></head><body>'
    '<script>a={event:"feedback clicked",properties:{errorCode: 1033 },'
    'helpful:a,version: 1 };</script></body></html>'
)
_CF_HEADERS = {'Server': 'cloudflare', 'Content-Type': 'text/html'}
_ACCESS_LOCATION = (
    'https://woowtech.cloudflareaccess.com/cdn-cgi/access/login/'
    'woowsa-hermes.woowtech.io?kid=abc&redirect_url=%2F')


@tagged('post_install', '-at_install')
class TestRouteProbeClassify(TransactionCase):
    """回應 → 狀態。每一格都是使用者該做的事不同的一種「打不開」。"""

    def setUp(self):
        super().setUp()
        route_probe._cache.clear()

    def _status(self, code, headers=None, body='', protected=False):
        return route_probe.classify(code, headers or {}, body, protected)['status']

    def test_tunnel_down_from_html_error_page(self):
        self.assertEqual(self._status(530, _CF_HEADERS, _CF_1033_HTML), 'tunnel_down')

    def test_tunnel_down_from_plain_error_code(self):
        self.assertEqual(self._status(530, _CF_HEADERS, 'error code: 1033'), 'tunnel_down')

    def test_530_without_code_is_tunnel_down(self):
        self.assertEqual(self._status(530, _CF_HEADERS, ''), 'tunnel_down')

    def test_1016_is_not_routed(self):
        """改名後舊名、或剛建好 DNS 還沒生效——使用者只要等一下。"""
        self.assertEqual(self._status(530, _CF_HEADERS, 'error code: 1016'), 'not_routed')

    def test_access_login_when_protected(self):
        self.assertEqual(
            self._status(302, {'Location': _ACCESS_LOCATION}, protected=True),
            'access_login')

    def test_access_on_public_route_is_unexpected(self):
        """#1239：平台說公開、Cloudflare 上卻有 Access 擋著。"""
        self.assertEqual(
            self._status(302, {'Location': _ACCESS_LOCATION}, protected=False),
            'access_unexpected')

    def test_protected_route_answering_directly_is_access_missing(self):
        self.assertEqual(self._status(200, {}, protected=True), 'access_missing')

    def test_origin_5xx_is_origin_error(self):
        self.assertEqual(
            self._status(502, {'Server': 'cloudflare'}, '<center>openresty</center>'),
            'origin_error')

    def test_app_redirect_and_4xx_are_reachable(self):
        """origin 自己的 302（例如導去 /login）與 404 都代表整條路是通的。"""
        self.assertEqual(
            self._status(302, {'Location': 'https://woowsa-hermes.woowtech.io/login'}),
            'ok')
        self.assertEqual(self._status(404, {'Server': 'cloudflare'}, 'Not Found'), 'ok')

    def test_cf_code_ignored_when_not_from_cloudflare(self):
        """origin 自己的頁面剛好寫了 'error code: 1033' 不能被當成 tunnel 斷線。"""
        self.assertEqual(
            self._status(500, {'Server': 'nginx'}, 'error code: 1033'), 'origin_error')

    def test_healthy_flag(self):
        self.assertTrue(route_probe.classify(200, {}, '', False)['healthy'])
        self.assertTrue(route_probe.classify(
            302, {'Location': _ACCESS_LOCATION}, '', True)['healthy'])
        self.assertFalse(route_probe.classify(530, _CF_HEADERS, '', False)['healthy'])

    # ---- 批次行為 --------------------------------------------------------- #

    def test_probe_routes_maps_fetch_results(self):
        def fake_fetch(hostname):
            return {
                'a.woowtech.io': (200, {}, ''),
                'b.woowtech.io': (530, _CF_HEADERS, _CF_1033_HTML),
            }[hostname]
        with patch.object(route_probe, '_fetch', side_effect=fake_fetch):
            res = route_probe.probe_routes(
                [('a.woowtech.io', False), ('b.woowtech.io', False)])
        self.assertEqual(res['a.woowtech.io']['status'], 'ok')
        self.assertEqual(res['b.woowtech.io']['status'], 'tunnel_down')

    def test_network_failures_are_classified_not_raised(self):
        def fake_fetch(hostname):
            if hostname.startswith('t.'):
                raise requests.exceptions.ReadTimeout('slow')
            if hostname.startswith('d.'):
                raise requests.exceptions.ConnectionError(
                    "Failed to resolve 'd.woowtech.io' (NameResolutionError)")
            raise ValueError('boom')
        with patch.object(route_probe, '_fetch', side_effect=fake_fetch):
            res = route_probe.probe_routes([
                ('t.woowtech.io', False), ('d.woowtech.io', False),
                ('x.woowtech.io', False)])
        self.assertEqual(res['t.woowtech.io']['status'], 'timeout')
        self.assertEqual(res['d.woowtech.io']['status'], 'dns_missing')
        self.assertEqual(res['x.woowtech.io']['status'], 'unknown')

    def test_cache_and_force(self):
        with patch.object(route_probe, '_fetch', return_value=(200, {}, '')) as fetch:
            route_probe.probe_routes([('c.woowtech.io', False)])
            route_probe.probe_routes([('c.woowtech.io', False)])
            self.assertEqual(fetch.call_count, 1, 'second call within TTL must hit cache')
            route_probe.probe_routes([('c.woowtech.io', False)], force=True)
            self.assertEqual(fetch.call_count, 2)
            # 保護狀態變了，舊結果的判讀不再成立 ⇒ 不吃快取。
            route_probe.probe_routes([('c.woowtech.io', True)])
            self.assertEqual(fetch.call_count, 3)

    def test_batch_deadline_turns_stragglers_into_timeout(self):
        def slow_fetch(hostname):
            time.sleep(0.5)
            return 200, {}, ''
        with patch.object(route_probe, '_BATCH_DEADLINE', 0.05), \
                patch.object(route_probe, '_fetch', side_effect=slow_fetch):
            res = route_probe.probe_routes([('s.woowtech.io', False)])
        self.assertEqual(res['s.woowtech.io']['status'], 'timeout')
        self.assertNotIn('s.woowtech.io', route_probe._cache,
                         'a straggler must not poison the cache')

    def test_unprobeable_hostname_is_never_fetched(self):
        """防呆：非 hostname 形狀的值（含 scheme、路徑、IP 字面值）一律不送出。"""
        with patch.object(route_probe, '_fetch') as fetch:
            res = route_probe.probe_routes([
                ('http://evil.example/x', False), ('', False),
                ('169.254.169.254', False), ('a/b.woowtech.io', False)])
        fetch.assert_not_called()
        self.assertTrue(all(r['status'] == 'unknown' for r in res.values()))


def _new_user(env, login):
    return env['res.users'].sudo().create({
        'name': login, 'login': login, 'password': login,
        'email': f'{login}@test.example',
        'groups_id': [(6, 0, [env.ref('base.group_user').id])],
    })


@tagged('post_install', '-at_install')
class TestRouteHealthEndpoint(HttpCase):
    """routes 端點的 ``health`` action 與 ``to_dict()['route_quota']``。"""

    def setUp(self):
        super().setUp()
        route_probe._cache.clear()
        suffix = uuid.uuid4().hex[:6]
        self.owner = _new_user(self.env, f'sa_hl_owner_{suffix}')
        self.plain_member = _new_user(self.env, f'sa_hl_user_{suffix}')
        self.outsider = _new_user(self.env, f'sa_hl_out_{suffix}')
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        Access = self.env['woow_paas_platform.workspace_access'].sudo()
        self.workspace = Workspace.create({
            'name': f'SA Health WS {suffix}', 'owner_id': self.owner.id})
        if not Access.search([('workspace_id', '=', self.workspace.id),
                              ('user_id', '=', self.owner.id)]):
            Access.create({'workspace_id': self.workspace.id,
                           'user_id': self.owner.id, 'role': 'owner'})
        Access.create({'workspace_id': self.workspace.id,
                       'user_id': self.plain_member.id, 'role': 'user'})
        self.sa = self.env['woow_paas_platform.security_access'].sudo().create({
            'name': 'Health Gateway', 'workspace_id': self.workspace.id})
        Route = self.env['woow_paas_platform.security_access_route'].sudo()
        # operator 同步一律擋掉（patch 註冊表上的實際 class，見
        # odoo-test-registry-patch 的教訓）；本檔只測探測與端點。
        with patch.object(type(self.env['woow_paas_platform.security_access']),
                          '_sync_routes_to_operator'), \
                patch.object(type(Route), '_sync_access_to_operator'):
            self.r1 = Route.create({'security_access_id': self.sa.id,
                                    'subdomain_prefix': f'hl-a-{suffix}',
                                    'service_url': 'http://localhost:8080'})
            self.r2 = Route.create({'security_access_id': self.sa.id,
                                    'subdomain_prefix': f'hl-b-{suffix}',
                                    'service_url': 'http://localhost:8081'})

    def _rpc(self, url, params):
        resp = self.url_open(url, data=json.dumps(
            {'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'})
        self._last_body = resp.json()
        return self._last_body.get('result') or {}

    def _health(self, **params):
        return self._rpc(
            '/api/workspaces/%s/security-accesses/%s/routes'
            % (self.workspace.id, self.sa.id), dict(action='health', **params))

    def _fake_probe(self, targets, force=False):
        self._probed = (list(targets), force)
        return {host: route_probe._result('ok', 200) for host, _p in targets}

    def test_member_gets_health_per_route_id(self):
        """唯讀、與 list 同權限：role=user 也看得到。"""
        self.authenticate(self.plain_member.login, self.plain_member.login)
        with patch.object(route_probe, 'probe_routes', side_effect=self._fake_probe):
            res = self._health()
        self.assertTrue(res.get('success'), self._last_body)
        self.assertEqual(set(res['data']), {str(self.r1.id), str(self.r2.id)})
        self.assertEqual(res['data'][str(self.r1.id)]['status'], 'ok')

    def test_probe_targets_come_from_db_hostnames_only(self):
        """呼叫端塞 hostname／url 參數不影響探測目標。"""
        self.authenticate(self.owner.login, self.owner.login)
        with patch.object(route_probe, 'probe_routes', side_effect=self._fake_probe):
            self._health(hostname='169.254.169.254', url='http://evil.example', force=True)
        targets, force = self._probed
        self.assertEqual(sorted(h for h, _p in targets),
                         sorted([self.r1.hostname, self.r2.hostname]))
        self.assertTrue(force)

    def test_outsider_denied(self):
        self.authenticate(self.outsider.login, self.outsider.login)
        with patch.object(route_probe, 'probe_routes', side_effect=self._fake_probe) as probe:
            res = self._health()
        self.assertNotIn('error', self._last_body, self._last_body.get('error'))
        self.assertFalse(res.get('success'))
        probe.assert_not_called()

    def test_to_dict_carries_route_quota(self):
        self.authenticate(self.owner.login, self.owner.login)
        res = self._rpc('/api/workspaces/%s/security-accesses/%s'
                        % (self.workspace.id, self.sa.id), {'action': 'get'})
        self.assertTrue(res.get('success'), self._last_body)
        self.assertIn('route_quota', res['data'])
        # 沒有訂閱（admin／legacy）⇒ 不限，與 _check_route_quota 同口徑。
        self.assertIsNone(res['data']['route_quota'])
