# -*- coding: utf-8 -*-
"""2026-10-27：服務頁看得出 MCP 服務「還沒設定連線」。

第三輪 staging 實測（全新開通的 Nextcloud MCP）：管理頁存好 Nextcloud 連線之前，``initialize``／
``tools/list`` 一律 503（子行程缺 ``NEXTCLOUD_MCP_BASE_URL``／``USERNAME``／``APP_PASSWORD`` 以 exit 2
結束），平台 ``state`` 卻是 ``running``，/woow 顯示「運作中」，租戶看不出哪裡不對。

修法：woow-mcp-server 的 ``/healthz`` 多回 ``mcp``（running／not_configured／not_running；舊映像沒有）；
平台在服務頁打開時以 ``POST …/mcp-status`` 由伺服器對該服務自己的對外根位址 GET ``/healthz``，服務頁
在 not_configured／not_running 時顯示提示（附管理頁網址），服務狀態本身不動。守的事：

* ``parse_healthz``（純函式）：只有 200＋JSON 物件＋認得的字串才回那個值；舊映像（沒有欄位）、
  其他值、非 200、非 JSON、超長 body 一律 ``unknown``；
* ``healthz_url``：只收平台算出的 ``https://<hostname>`` 形狀，其他形狀不發請求；
* ``_fetch``：不跟隨轉址、設自己的 User-Agent、短逾時、body 有上限；
* ``probe``：逾時（總截止時間）、網路錯誤都是 ``unknown``、從不 raise；同一網址短時間內共用結果；
* 能力 gate＝``_admin_page_public_url()`` 非空（has_admin_gui＋config_json 載體＋有 subdomain），
  與 mcp-server chart 家族完全重合；不是 slug 名單；
* 端點：工作區成員（含 guest、Portal 租戶）讀得到；別的工作區／非成員／沒有能力的服務一律拒絕且
  **不發請求**；不是 running 不查；呼叫端多送的參數（例如網址）整個拒絕；純讀、不寫帳本；
* detail payload 只帶 gate（``has_mcp_setup_status``），狀態不進 payload；
* 前端：gate 讀後端旗標、不用 slug；新字串都有 zh_TW 譯文。

網路一律 patch：``mcp_setup_status._fetch``（單一 GET）是唯一的 IO 縫；``_fetch`` 本身的參數另以
patch ``requests.get`` 驗。前端行為另由 ``tests/ui/test_mcp_setup_status_20261027.mjs``（node）驗。
"""
import json
import re
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import polib
import requests

from odoo.tests import HttpCase
from odoo.tests.common import BaseCase, TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..services import mcp_setup_status as mss

_FETCH = 'odoo.addons.woow_paas_platform.services.mcp_setup_status._fetch'
_REQUESTS_GET = 'odoo.addons.woow_paas_platform.services.mcp_setup_status.requests.get'
_ADDON = Path(__file__).resolve().parents[1]
_STATIC = _ADDON / 'static' / 'src' / 'paas'


def _healthz(**fields):
    return json.dumps({'status': 'ok', **fields}).encode()


@tagged('post_install', '-at_install')
class TestMcpHealthzContract20261027(BaseCase):
    """``/healthz`` 回應 → 狀態，以及「只查平台網址」的形狀閘。"""

    def test_the_three_reported_states_pass_through(self):
        for state in ('running', 'not_configured', 'not_running'):
            with self.subTest(state=state):
                self.assertEqual(mss.parse_healthz(200, _healthz(mcp=state)), state)
                self.assertEqual(mss.parse_healthz(200, _healthz(mcp=state).decode()), state)

    def test_old_image_without_the_field_is_unknown(self):
        """第三輪實測的正式站映像只回 {"status":"ok"}。"""
        self.assertEqual(mss.parse_healthz(200, b'{"status":"ok"}'), 'unknown')

    def test_values_outside_the_contract_are_unknown(self):
        for value in (None, '', 'RUNNING', 'Not_Configured', 'configured', 'error', 1, True,
                      ['running'], {'state': 'not_configured'}):
            with self.subTest(value=value):
                self.assertEqual(mss.parse_healthz(200, _healthz(mcp=value)), 'unknown')

    def test_non_200_non_json_and_oversized_bodies_are_unknown(self):
        good = _healthz(mcp='not_configured')
        for status in (201, 204, 301, 302, 401, 403, 404, 500, 502, 503, 530):
            with self.subTest(status=status):
                self.assertEqual(mss.parse_healthz(status, good), 'unknown')
        for body in (b'', b'<html>error code: 1033</html>', b'[]', b'"not_configured"',
                     b'{"mcp": "not_configured"', b'\xff\xfe\x00junk', None, 42):
            with self.subTest(body=body):
                self.assertEqual(mss.parse_healthz(200, body), 'unknown')
        oversized = json.dumps({'mcp': 'not_configured', 'pad': 'x' * mss._BODY_MAX_BYTES}).encode()
        self.assertEqual(mss.parse_healthz(200, oversized), 'unknown')

    def test_healthz_url_only_accepts_the_platform_shape(self):
        ok = 'https://paas-cs-5a105e8b-230ef033.woowtech.io'
        self.assertEqual(mss.healthz_url(ok), ok + '/healthz')
        self.assertEqual(mss.healthz_url(ok + '/'), ok + '/healthz')
        for bad in (None, '', 'paas-cs-x.woowtech.io', 'http://paas-cs-x.woowtech.io',
                    'https://paas-cs-x.woowtech.io:8443', 'https://user:pw@paas-cs-x.woowtech.io',
                    'https://paas-cs-x.woowtech.io/private_abc/mcp', 'https://paas-cs-x.woowtech.io/healthz',
                    'https://paas-cs-x.woowtech.io?x=1', 'https://paas-cs-x.woowtech.io#x',
                    'https://127.0.0.1', 'https://localhost', 'https://169.254.169.254',
                    'https://svc_x.paas-ws-1.svc', 'https://[::1]', 'ftp://paas-cs-x.woowtech.io',
                    'https://paas-cs-x.woowtech.io:bad'):
            with self.subTest(bad=bad):
                self.assertEqual(mss.healthz_url(bad), '')


@tagged('post_install', '-at_install')
class TestMcpSetupProbe20261027(BaseCase):
    """``_fetch``／``probe``：不跟隨轉址、短逾時、有上限、從不 raise、短快取。"""

    base = 'https://paas-cs-feedbeef-0badf00d.woowtech.io'

    def setUp(self):
        super().setUp()
        mss._cache.clear()
        self.addCleanup(mss._cache.clear)

    def _response(self, status=200, chunks=(b'{"status":"ok","mcp":"not_configured"}',)):
        response = MagicMock()
        response.status_code = status
        response.iter_content.return_value = iter(chunks)
        return response

    def test_fetch_never_follows_redirects_and_sends_its_own_user_agent(self):
        response = self._response()
        with patch(_REQUESTS_GET, return_value=response) as get:
            self.assertEqual(mss.probe(self.base), 'not_configured')
        get.assert_called_once()
        args, kwargs = get.call_args
        self.assertEqual(args, (self.base + '/healthz',))
        self.assertIs(kwargs['allow_redirects'], False)
        self.assertIs(kwargs['stream'], True)
        self.assertEqual(kwargs['timeout'], (mss._CONNECT_TIMEOUT, mss._READ_TIMEOUT))
        self.assertLessEqual(max(kwargs['timeout']), 3.0, '短逾時')
        self.assertTrue(kwargs['headers']['User-Agent'].startswith('WoowPaaS-'))
        self.assertNotIn('python-requests', kwargs['headers']['User-Agent'])
        self.assertNotIn('Cookie', kwargs['headers'])
        self.assertNotIn('Authorization', kwargs['headers'])
        response.close.assert_called_once()

    def test_redirect_answer_is_unknown(self):
        """開了 Cloudflare Access 的服務：/healthz 被 302 到登入頁 ⇒ unknown，不跟過去。"""
        with patch(_REQUESTS_GET, return_value=self._response(status=302, chunks=(b'',))):
            self.assertEqual(mss.probe(self.base), 'unknown')

    def test_body_read_stops_at_the_limit(self):
        consumed = []

        def endless():
            while True:
                consumed.append(1)
                yield b'x' * 1024

        response = self._response(chunks=())
        response.iter_content.return_value = endless()
        with patch(_REQUESTS_GET, return_value=response):
            self.assertEqual(mss.probe(self.base), 'unknown')
        self.assertLessEqual(len(consumed), mss._BODY_MAX_BYTES // 1024 + 1)
        response.close.assert_called_once()

    def test_network_failures_are_unknown_and_never_raise(self):
        for error in (requests.exceptions.ConnectionError('Name or service not known'),
                      requests.exceptions.ConnectTimeout('slow'), requests.exceptions.ReadTimeout('slow'),
                      requests.exceptions.SSLError('bad cert'), RuntimeError('boom'), ValueError('x')):
            with self.subTest(error=type(error).__name__):
                mss._cache.clear()
                with patch(_FETCH, side_effect=error):
                    self.assertEqual(mss.probe(self.base), 'unknown')

    def test_the_total_deadline_bounds_the_wait(self):
        """requests 的 timeout 不涵蓋 DNS；卡住的查詢最多讓 worker 等 _DEADLINE_SECONDS。"""
        def slow(url):
            time.sleep(1.0)
            return 200, _healthz(mcp='not_configured')

        with patch.object(mss, '_DEADLINE_SECONDS', 0.2), patch(_FETCH, side_effect=slow):
            started = time.monotonic()
            self.assertEqual(mss.probe(self.base), 'unknown')
            self.assertLess(time.monotonic() - started, 0.9)

    def test_results_are_shared_for_a_short_while(self):
        with patch(_FETCH, return_value=(200, _healthz(mcp='not_configured'))) as fetch:
            self.assertEqual(mss.probe(self.base), 'not_configured')
            self.assertEqual(mss.probe(self.base + '/'), 'not_configured')
        self.assertEqual(fetch.call_count, 1, '同一網址在快取窗內只問一次')
        with patch.object(mss, '_CACHE_TTL_SECONDS', 0), \
                patch(_FETCH, return_value=(200, _healthz(mcp='running'))) as fetch:
            self.assertEqual(mss.probe(self.base), 'running')
        self.assertEqual(fetch.call_count, 1, '過了窗口就重問（租戶存完連線切回來要看得到）')

    def test_unknown_is_cached_too_so_a_dead_host_does_not_cost_every_poll(self):
        with patch(_FETCH, side_effect=requests.exceptions.ConnectTimeout('slow')) as fetch:
            self.assertEqual(mss.probe(self.base), 'unknown')
            self.assertEqual(mss.probe(self.base), 'unknown')
        self.assertEqual(fetch.call_count, 1)

    def test_an_invalid_base_url_never_touches_the_network(self):
        with patch(_FETCH) as fetch:
            for bad in ('', 'http://paas-cs-x.woowtech.io', 'https://127.0.0.1', 'https://evil.example:8443'):
                self.assertEqual(mss.probe(bad), 'unknown')
        fetch.assert_not_called()


@tagged('post_install', '-at_install')
class TestMcpSetupCapability20261027(TransactionCase):
    """能力 gate＝``_admin_page_public_url()`` 非空，而且恰好是 mcp-server chart 家族。"""

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create(
            {'name': 'mcp gate ' + self.suffix, 'owner_id': self.env.user.id})

    def _service(self, template, subdomain=True):
        label = uuid.uuid4().hex[:8]
        vals = {
            'name': 'gate-' + label, 'workspace_id': self.workspace.id, 'template_id': template.id,
            'state': 'running', 'helm_namespace': 'paas-ws-gate-' + self.suffix,
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
        self.assertTrue(nextcloud._admin_page_public_url().startswith('https://' + nextcloud.subdomain + '.'))
        self.assertTrue(mss.healthz_url(nextcloud._admin_page_public_url()), '平台算出的網址一定過得了形狀閘')

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


@tagged('post_install', '-at_install')
class TestMcpStatusEndpoint20261027(HttpCase):
    """``POST /api/workspaces/<ws>/services/<id>/mcp-status``：授權、gate、降級、純讀。"""

    def setUp(self):
        super().setUp()
        mss._cache.clear()
        self.addCleanup(mss._cache.clear)
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
        self.assertEqual(result['data'], {'state': 'not_configured', 'admin_url': admin_url})
        fetch.assert_called_once_with(admin_url + '/healthz')
        self.assertEqual(self.service.state, 'running', '服務狀態本身不動')

    def test_running_and_not_running_pass_through(self):
        for state in ('running', 'not_running'):
            with self.subTest(state=state):
                mss._cache.clear()
                result, _fetch = self._call(self.owner, self.service, (200, _healthz(mcp=state)))
                self.assertEqual(result['data']['state'], state)

    def test_old_image_without_the_field_is_unknown_not_an_error(self):
        result, fetch = self._call(self.owner, self.service, (200, b'{"status":"ok"}'))
        self.assertTrue(result['success'])
        self.assertEqual(result['data']['state'], 'unknown')
        fetch.assert_called_once()

    def test_timeout_and_unreachable_are_unknown_not_an_error(self):
        for error in (requests.exceptions.ReadTimeout('slow'),
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
        """網址一律由平台算；呼叫端多送任何參數（包括網址）整個拒絕、不發請求。"""
        for params in ({'url': 'https://evil.example/healthz'}, {'admin_url': 'http://169.254.169.254'},
                       {'subdomain': 'paas-cs-other'}):
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
class TestMcpSetupFrontend20261027(BaseCase):
    """靜態守線（行為另由 tests/ui 的 node 測試驗）：gate 讀後端旗標、不用 slug；譯文齊。"""

    def _read(self, rel):
        return (_STATIC / rel).read_text(encoding='utf-8')

    def test_the_notice_gate_reads_the_backend_flag_not_a_slug(self):
        xml = self._read('pages/service/ServiceDetailPage.xml')
        page = self._read('pages/service/ServiceDetailPage.js')
        component = self._read('components/mcp-setup-status/McpSetupStatusNotice.js')
        self.assertRegex(xml, r'<McpSetupStatusNotice\s+t-if="hasMcpSetupStatus"')
        self.assertIn('this.service?.has_mcp_setup_status === true', page)
        self.assertIn('this.props.service.has_mcp_setup_status === true', component)
        for rel in ('components/mcp-setup-status/McpSetupStatusNotice.js',
                    'components/mcp-setup-status/McpSetupStatusNotice.xml',
                    'services/mcp_setup_status.js', 'utils/mcp_setup_status.js'):
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
        translated = {entry.msgid: entry.msgstr for entry in po if entry.msgstr}
        found = re.findall(r'_t\(\s*"((?:[^"\\]|\\.)*)"', self._read('utils/mcp_setup_status.js'))
        self.assertEqual(len(found), 3)
        for msgid in found:
            with self.subTest(msgid=msgid):
                self.assertIn(msgid, translated, 'zh_TW.po 缺少：%s' % msgid)
        self.assertEqual(
            translated['MCP is not connected yet: open the admin page → Connection, fill it in and '
                       'save. MCP starts providing tools only after that.'],
            'MCP 尚未設定連線：請到管理頁 → Connection 填好並儲存，MCP 才會開始提供工具。')
        self.assertEqual(translated['The MCP child process is not running. Check Logs on the admin page.'],
                         'MCP 子行程沒有在跑，請到管理頁查看 Logs。')
