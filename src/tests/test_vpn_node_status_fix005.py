# -*- coding: utf-8 -*-
"""INFRA-FIX-005：VPN 節點（charts/tailscale）的即時狀態——pod Ready ≠ 節點在線。

INFRA-TS-005／TS-007（Tailscale 242，工作區 Headscale 為控制面）在 .243 的實測：

1. 節點被控制面到期／刪除 ⇒ BackendState=NeedsLogin，pod 仍 Ready，平台 ``state`` 一直 running，
   /woow 顯示「執行中／ONLINE」；
2. 原生網頁的登入網址是叢集內部位址，瀏覽器打不開，/woow 也沒說該怎麼辦；
3. ``tailscale.serve`` 被略過的項目只在容器 stderr，Service 不存在的項目甚至不會被略過。

修法（operator ``GET …/vpn-node-status`` ＋ 平台 ``POST …/node-status`` ＋ 服務頁「VPN 節點狀態」卡）。
守的事：

* ``public_report``（純函式）：operator 回應**再驗一次**——封閉集合、型別、限長；不是 dict ⇒ ``None``；
  state 的七個值；**登入入口只給 owner／admin**；
* ``control_plane_of``：同 chart 的解讀（沒有 controlPlane 時 loginServer 非空 ⇒ custom）；
* 能力 gate 是 ``cloud_app_template.has_vpn_node_status``（只有 tailscale 為 True），端點、payload、
  前端都讀它，前端沒有 slug 名單；
* 端點**純讀**：不寫 DB（``write_date`` 不動）、不碰 state；沒有能力／不是 running／operator 失敗都是
  正常降級，不把 operator 的訊息放到租戶畫面；
* 跨工作區／concealed 的服務一律「Service not found」。
"""
import json
import re
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase
from odoo.tests.common import BaseCase, TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..services import vpn_node_status as vns
from ..services.paas_operator import PaaSOperatorAPIError

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_STATIC = Path(__file__).resolve().parents[1] / 'static' / 'src' / 'paas'


def _payload(**over):
    base = {
        'probe': 'ok', 'pod': 'svc-x-0', 'backend_state': 'Running', 'online': True,
        'tailnet_ips': ['100.64.0.3'], 'dns_name': 'node.tailnet.internal',
        'login': {'kind': 'none', 'url': None, 'register_key': None},
        'health_warnings': 0,
        'serve': {
            'wanted_known': True,
            'forwards': [{'port': 9004, 'service': 'svc-0b4a8def-headscale',
                          'target_port': 8080, 'status': 'applied'}],
            'skipped': [{'entry': '9102=svc-x.other-ns.svc.cluster.local:80',
                         'reason': 'other_namespace'}],
            'ignored_all': None, 'extra_config': False,
        },
    }
    base.update(over)
    return base


@tagged('post_install', '-at_install')
class TestPublicReportFix005(BaseCase):

    def _report(self, payload=None, can_manage=True, plane='workspace'):
        return vns.public_report(
            _payload() if payload is None else payload, can_manage=can_manage, control_plane=plane)

    def test_online_report(self):
        r = self._report()
        self.assertEqual((r['state'], r['probe']), ('online', 'ok'))
        self.assertEqual(r['tailnet_ips'], ['100.64.0.3'])
        self.assertEqual(r['serve']['forwards'][0]['status'], 'applied')
        self.assertEqual(r['serve']['skipped'][0]['reason'], 'other_namespace')

    def test_state_table(self):
        """Pod 仍 Ready 的 NeedsLogin 必須是 needs_login，不是 online（TS-005 p2／p7）。"""
        cases = [
            ('Running', True, 'online'), ('Running', None, 'online'), ('Running', False, 'offline'),
            ('NeedsLogin', True, 'needs_login'), ('NeedsMachineAuth', None, 'needs_approval'),
            ('NoState', None, 'starting'), ('Starting', None, 'starting'),
            ('Stopped', None, 'stopped'), ('Unknown', None, 'unknown'), ('weird', None, 'unknown'),
        ]
        for backend, online, expected in cases:
            with self.subTest(backend=backend, online=online):
                r = self._report(_payload(backend_state=backend, online=online))
                self.assertEqual(r['state'], expected)

    def test_degraded_probe_is_unknown_whatever_the_stale_fields_say(self):
        for probe in ('no_pod', 'pod_not_running', 'exec_failed', 'no_output'):
            with self.subTest(probe=probe):
                r = self._report(_payload(probe=probe, backend_state='Running'))
                self.assertEqual(r['state'], 'unknown')
        self.assertEqual(self._report(_payload(probe='bogus'))['probe'], 'no_output')

    def test_login_entrance_only_for_owner_or_admin(self):
        login = {'kind': 'external_url', 'url': 'https://login.tailscale.com/a/abc123', 'register_key': None}
        self.assertEqual(self._report(_payload(login=login), can_manage=True)['login']['url'], login['url'])
        member = self._report(_payload(login=login), can_manage=False)['login']
        self.assertEqual(member, {'kind': 'external_url', 'url': None, 'register_key': None})
        internal = {'kind': 'cluster_internal', 'url': None, 'register_key': 'authreq-abc123'}
        self.assertEqual(
            self._report(_payload(login=internal), can_manage=True)['login']['register_key'], 'authreq-abc123')
        self.assertIsNone(
            self._report(_payload(login=internal), can_manage=False)['login']['register_key'])

    def test_login_fields_are_closed_even_for_managers(self):
        bad = [
            {'kind': 'external_url', 'url': 'javascript:alert(1)', 'register_key': None},
            {'kind': 'external_url', 'url': 'https://x.io/a b', 'register_key': None},
            {'kind': 'cluster_internal', 'url': 'http://svc.ns.svc.cluster.local/x', 'register_key': '<b>'},
            {'kind': 'surprise', 'url': 'https://x.io/', 'register_key': 'abcd'},
            'not-a-dict',
        ]
        for login in bad:
            with self.subTest(login=login):
                r = self._report(_payload(login=login))['login']
                self.assertIsNone(r['url'])
                self.assertIsNone(r['register_key'])
                self.assertIn(r['kind'], ('none', 'external_url', 'cluster_internal'))
                self.assertNotIn('cluster.local', json.dumps(r))

    def test_internal_url_never_crosses_even_if_operator_sent_it(self):
        r = self._report(_payload(login={
            'kind': 'cluster_internal', 'url': 'http://svc-x.paas-ws-1.svc.cluster.local:8080/register/abcd',
            'register_key': 'abcd'}))
        self.assertIsNone(r['login']['url'])

    def test_serve_rows_are_filtered_not_trusted(self):
        r = self._report(_payload(serve={
            'wanted_known': 'yes',
            'forwards': [
                {'port': 9004, 'service': 'ok-svc', 'target_port': 8080, 'status': 'applied'},
                {'port': 0, 'service': 'x', 'status': 'applied'},
                {'port': 70000, 'service': 'x', 'status': 'applied'},
                {'port': True, 'service': 'x', 'status': 'applied'},
                {'port': 9005, 'service': 'Bad_Name<script>', 'target_port': 'a', 'status': 'applied'},
                {'port': 9006, 'service': 'x', 'status': 'exploded'},
                'junk', None,
            ],
            'skipped': [
                {'entry': '9102=a:1', 'reason': 'port_range'},
                {'entry': '<script>', 'reason': 'port_range'},
                {'entry': 'x' * 300, 'reason': 'port_range'},
                {'entry': '9103=b:2', 'reason': 'made-up'},
                7,
            ],
            'ignored_all': 'other', 'extra_config': 'true',
        }))
        serve = r['serve']
        self.assertIs(serve['wanted_known'], False, '只認布林 True')
        self.assertEqual([f['port'] for f in serve['forwards']], [9004, 9005])
        self.assertIsNone(serve['forwards'][1]['service'])
        self.assertIsNone(serve['forwards'][1]['target_port'])
        self.assertEqual(serve['skipped'], [
            {'entry': '9102=a:1', 'reason': 'port_range'},
            {'entry': '9103=b:2', 'reason': 'other'},
        ])
        self.assertIsNone(serve['ignored_all'])
        self.assertIs(serve['extra_config'], False)

    def test_item_counts_are_capped(self):
        many = [{'port': p, 'service': 's', 'target_port': 1, 'status': 'applied'} for p in range(1, 200)]
        self.assertEqual(len(self._report(_payload(serve={'forwards': many}))['serve']['forwards']), vns.MAX_ITEMS)

    def test_junk_scalars_are_dropped(self):
        r = self._report(_payload(tailnet_ips=['100.64.0.3', 5, '<x>', 'fd7a::3'], dns_name='a b<c>'))
        self.assertEqual(r['tailnet_ips'], ['100.64.0.3', 'fd7a::3'])
        self.assertIsNone(r['dns_name'])

    def test_non_dict_payload_means_unreadable(self):
        for junk in (None, [], 'x', 3):
            self.assertIsNone(vns.public_report(junk, can_manage=True))

    def test_report_contains_nothing_beyond_the_declared_keys(self):
        """operator 多送的欄位（原始 log、stderr、token…）不得透傳。"""
        r = self._report(_payload(raw_log='tskey-auth-kSECRET', stderr='boom', **{'pod': 'p'}))
        self.assertNotIn('SECRET', json.dumps(r))
        self.assertEqual(set(r), {'state', 'probe', 'control_plane', 'tailnet_ips', 'dns_name', 'login', 'serve'})

    def test_control_plane_of(self):
        f = vns.control_plane_of
        self.assertEqual(f({'tailscale': {'controlPlane': 'workspace'}}), 'workspace')
        self.assertEqual(f({'tailscale': {'controlPlane': 'custom', 'loginServer': ''}}), 'custom')
        # 0.1.2 之前開通：沒有 controlPlane，loginServer 非空 ⇒ custom，否則 official。
        self.assertEqual(f({'tailscale': {'loginServer': 'https://hs.example.com'}}), 'custom')
        self.assertEqual(f({'tailscale': {'loginServer': '  '}}), 'official')
        self.assertEqual(f({'tailscale': {'controlPlane': 'bogus'}}), 'official')
        for junk in (None, {}, {'tailscale': None}, {'tailscale': 'x'}, [], 'x'):
            self.assertEqual(f(junk), 'official')


@tagged('post_install', '-at_install')
class TestVpnNodeStatusCapabilityFix005(TransactionCase):

    def test_only_tailscale_has_the_capability(self):
        Template = self.env['woow_paas_platform.cloud_app_template'].sudo().with_context(active_test=False)
        with_flag = Template.search([('has_vpn_node_status', '=', True)])
        self.assertEqual(with_flag.mapped('slug'), ['tailscale'])
        self.assertTrue(self.env.ref('woow_paas_platform.cloud_app_tailscale').has_vpn_node_status)

    def test_service_method_mirrors_the_template_flag(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_tailscale')
        service = _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create({
            'name': 'ts-gate', 'workspace_id': self.env['woow_paas_platform.workspace'].sudo().create(
                {'name': 'ts gate', 'owner_id': self.env.user.id}).id,
            'template_id': tpl.id, 'state': 'running', 'helm_namespace': 'paas-ws-gate',
            'helm_release_name': 'svc-gate', 'helm_values': '{}',
        })
        self.assertTrue(service._chart_has_vpn_node_status())
        other = self.env.ref('woow_paas_platform.cloud_app_postgresql')
        service.template_id = other
        self.assertFalse(service._chart_has_vpn_node_status())

    def test_seed_text_no_longer_points_to_the_container_log(self):
        """被略過的轉發原因現在顯示在服務頁；說明文字不能再叫租戶去看租戶看不到的「服務記錄」。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_tailscale')
        serve = {s['key']: s for s in tpl._parse_helm_value_specs()['optional']}['tailscale.serve']
        self.assertNotIn('服務記錄', serve['description'])
        self.assertIn('VPN 節點狀態', serve['description'])


@tagged('post_install', '-at_install')
class TestNodeStatusEndpointFix005(HttpCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]

        def user(label):
            login = 'vpnns_%s_%s' % (label, suffix)
            return self.env['res.users'].sudo().with_context(no_reset_password=True).create({
                'name': login, 'login': login, 'password': login, 'email': login + '@test.example',
                'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
            })

        self.owner, self.guest, self.other = user('owner'), user('guest'), user('other')
        Workspace = self.env['woow_paas_platform.workspace'].sudo()
        self.workspace = Workspace.create({'name': 'VPN ' + suffix, 'owner_id': self.owner.id})
        self.other_workspace = Workspace.create({'name': 'VPN other ' + suffix, 'owner_id': self.other.id})
        self.env['woow_paas_platform.workspace_access'].sudo().create({
            'workspace_id': self.workspace.id, 'user_id': self.guest.id, 'role': 'guest'})
        self.tailscale = self.env.ref('woow_paas_platform.cloud_app_tailscale')
        self.postgres = self.env.ref('woow_paas_platform.cloud_app_postgresql')
        self.service = self._service(self.workspace, self.tailscale, 'ts', suffix)
        self.plain = self._service(self.workspace, self.postgres, 'pg', suffix)
        self.foreign = self._service(self.other_workspace, self.tailscale, 'foreign', suffix)
        self.route = '/api/workspaces/%s/services/%s/node-status'

    def _service(self, workspace, template, label, suffix, **extra):
        vals = {
            'name': label, 'workspace_id': workspace.id, 'template_id': template.id,
            'state': 'running', 'helm_namespace': 'paas-ws-vpn-' + suffix,
            'helm_release_name': 'svc-' + label + '-' + suffix,
            'helm_values': json.dumps({'tailscale': {'controlPlane': 'workspace'}}),
        }
        vals.update(extra)
        return _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create(vals)

    def _client(self, payload=None, error=None):
        client = MagicMock()
        if error:
            client.get_vpn_node_status.side_effect = error
        else:
            client.get_vpn_node_status.return_value = payload
        return client

    def _post(self, service, **params):
        response = self.url_open(self.route % (service.workspace_id.id, service.id), data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        body = response.json()
        self.assertNotIn('error', body, body)
        return body['result']

    def _call(self, member, service, client, **params):
        self.authenticate(member.login, member.login)
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=client):
            return self._post(service, **params)

    def test_needs_login_is_reported_for_a_running_service_and_asks_the_right_pod(self):
        client = self._client(_payload(backend_state='NeedsLogin', login={
            'kind': 'cluster_internal', 'url': None, 'register_key': 'authreq-abc123'}))
        result = self._call(self.owner, self.service, client)
        self.assertTrue(result['success'])
        data = result['data']
        self.assertTrue(data['available'])
        self.assertEqual(data['report']['state'], 'needs_login')
        self.assertEqual(data['report']['control_plane'], 'workspace')
        self.assertEqual(data['report']['login']['register_key'], 'authreq-abc123')
        client.get_vpn_node_status.assert_called_once_with(
            self.service.helm_namespace, self.service.helm_release_name)
        self.assertEqual(self.service.state, 'running', '端點不碰 state')

    def test_guest_sees_state_but_not_the_login_entrance(self):
        client = self._client(_payload(backend_state='NeedsLogin', login={
            'kind': 'cluster_internal', 'url': None, 'register_key': 'authreq-abc123'}))
        data = self._call(self.guest, self.service, client)['data']
        self.assertEqual(data['report']['state'], 'needs_login')
        self.assertIsNone(data['report']['login']['register_key'])

    def test_read_does_not_write_the_ledger(self):
        before = self.service.read(['state', 'helm_values', 'write_date', 'helm_revision'])[0]
        self._call(self.owner, self.service, self._client(_payload()))
        self.assertEqual(self.service.read(['state', 'helm_values', 'write_date', 'helm_revision'])[0], before)

    def test_service_without_the_capability_is_not_found_and_operator_is_not_asked(self):
        client = self._client(_payload())
        result = self._call(self.owner, self.plain, client)
        self.assertFalse(result['success'])
        client.get_vpn_node_status.assert_not_called()

    def test_other_workspaces_service_is_not_found(self):
        client = self._client(_payload())
        result = self._call(self.guest, self.foreign, client)
        self.assertFalse(result['success'])
        client.get_vpn_node_status.assert_not_called()

    def test_not_running_is_a_normal_degraded_answer(self):
        stopped = self._service(self.workspace, self.tailscale, 'ts-err', uuid.uuid4().hex[:8], state='error')
        client = self._client(_payload())
        data = self._call(self.owner, stopped, client)['data']
        self.assertEqual((data['available'], data['reason'], data['report']), (False, 'not_running', None))
        client.get_vpn_node_status.assert_not_called()

    def test_operator_failures_degrade_without_leaking_operator_text(self):
        for error in (PaaSOperatorAPIError('tskey-auth-kSECRET internal detail', status_code=404),
                      PaaSOperatorAPIError('boom', status_code=500)):
            with self.subTest(error=str(error)):
                result = self._call(self.owner, self.service, self._client(error=error))
                self.assertTrue(result['success'])
                self.assertEqual(result['data']['reason'], 'operator_unavailable')
                self.assertNotIn('SECRET', json.dumps(result))

    def test_no_operator_configured_degrades(self):
        self.authenticate(self.owner.login, self.owner.login)
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=None):
            result = self._post(self.service)
        self.assertEqual(result['data']['reason'], 'operator_unavailable')

    def test_non_dict_operator_answer_degrades(self):
        result = self._call(self.owner, self.service, self._client(['not', 'a', 'dict']))
        self.assertEqual(result['data']['reason'], 'operator_unavailable')

    def test_unexpected_params_are_rejected_before_anything_else(self):
        client = self._client(_payload())
        result = self._call(self.owner, self.service, client, namespace='paas-ws-other', release='svc-other')
        self.assertFalse(result['success'])
        client.get_vpn_node_status.assert_not_called()

    def test_detail_payload_exposes_only_the_capability_flag(self):
        """狀態不進 payload（卡片即時問），payload 只帶 gate。"""
        self.authenticate(self.owner.login, self.owner.login)
        response = self.url_open(
            '/api/workspaces/%s/services/%s' % (self.workspace.id, self.service.id),
            data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': {}}),
            headers={'Content-Type': 'application/json'})
        data = response.json()['result']['data']
        self.assertIs(data['has_vpn_node_status'], True)
        self.assertNotIn('vpn_node_status', json.dumps(data).replace('has_vpn_node_status', ''))


@tagged('post_install', '-at_install')
class TestVpnNodeStatusFrontendFix005(BaseCase):
    """靜態守線（行為另由 tests/ui 的 node 測試驗）：前端不得用 slug 判能力、元件有接上 helper。"""

    def _read(self, rel):
        return (_STATIC / rel).read_text(encoding='utf-8')

    def test_card_gate_reads_the_backend_flag_not_a_slug_list(self):
        xml = self._read('pages/service/tabs/OverviewTab.xml')
        js = self._read('pages/service/tabs/OverviewTab.js')
        self.assertIn('props.service.has_vpn_node_status', xml)
        self.assertIn('has_vpn_node_status', js)
        for text in (xml, js, self._read('services/vpn_node_status.js'), self._read('utils/vpn_node_status.js')):
            self.assertIsNone(re.search(r'''['"]tailscale['"]''', text),
                              '前端不得用 slug 判斷能力（capability-gate-single-source-field）')

    def test_connection_status_yields_to_a_node_problem(self):
        js = self._read('pages/service/tabs/OverviewTab.js')
        self.assertIn('hasNodeProblem(this.vpn.state.report)', js)

    def test_detail_page_hands_the_workspace_id_to_the_overview_tab(self):
        self.assertRegex(self._read('pages/service/ServiceDetailPage.xml'),
                         r'<OverviewTab[^>]*workspaceId="props\.workspaceId"')

    def test_every_new_tenant_string_has_a_zh_tw_translation(self):
        po = (Path(__file__).resolve().parents[1] / 'i18n' / 'zh_TW.po').read_text(encoding='utf-8')
        msgids = set(re.findall(r'^msgid "(.*)"$', po, re.M))
        for rel in ('utils/vpn_node_status.js',):
            for match in re.finditer(r'_t\(\s*"((?:[^"\\]|\\.)*)"', self._read(rel)):
                self.assertIn(match.group(1), msgids, 'zh_TW.po 缺少：%s' % match.group(1))
        js = self._read('pages/service/tabs/OverviewTab.js')
        for text in ('VPN node status', 'Node', 'Tailnet address', 'Forwards',
                     'No forwards are configured.', 'Reading the node status...'):
            self.assertIn(text, msgids)
            self.assertIn('_t("%s")' % text, js)
