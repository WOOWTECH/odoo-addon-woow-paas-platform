# -*- coding: utf-8 -*-
"""商用驗收 INFRA-FIX-005 覆核後的補強（疊在 PR #1771 之上）。

覆核判定 #1771 對 TS-005-5、TS-007-1 只做了一半、``/node-status`` 沒有伺服器端節流：

1. **INFRA-TS-005-5**：儲存 ``controlPlane``／``loginServer``／``authKey`` 完全不驗證（控制平面與 key
   種類錯配、自訂網址留空都照存）；已登入的節點會靜默忽略設定裡的 key；登入失敗原因
   （``authkey expired``…）只在容器日誌。
   ⇒ 存檔前擋「一定不會成功」的組合；operator 回 ``login_error``（封閉代碼）與
   ``authkey_configured``，平台轉成 ``login_error``／``authkey_ignored`` 給服務頁。
2. **INFRA-TS-007-1**：轉發清單不驗證（他 namespace、IP、埠超界、重複埠照存）。
   ⇒ 存檔前用與映像 entrypoint ``parse_serve`` 同一套規則擋下（前端另有同規則的即時提示）。
3. ``/node-status`` 每次都 kubectl exec ⇒ 同一台服務 10 秒內重複請求共用上一份 operator 回應
   （角色過濾仍逐次重做）。
"""
import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from odoo.tests import HttpCase
from odoo.tests.common import BaseCase, TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..services import tailscale_settings as tss
from ..services import vpn_node_status as vns

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_NS = 'paas-ws-5e41efa6'
_STATIC = Path(__file__).resolve().parents[1] / 'static' / 'src' / 'paas'


# --------------------------------------------------------------------------- 純函式
@tagged('post_install', '-at_install')
class TestServeRulesMatchTheEntrypoint(BaseCase):
    """與 entrypoint ``parse_serve`` 同一套規則（TS-007-1 的實測輸入逐條驗）。"""

    def code(self, raw, ns=_NS):
        result = tss.check_serve(raw, ns)
        return result and result[0]

    def test_ts007_inputs_are_all_rejected_with_the_entrypoint_reason(self):
        self.assertEqual(self.code('9102=svc-x.other-ns.svc.cluster.local:80'), 'serve_other_namespace')
        self.assertEqual(self.code('9103=10.0.0.5:80'), 'serve_other_namespace')
        self.assertEqual(self.code('70000=a:1'), 'serve_port_range')
        self.assertEqual(self.code('9101=no-such-svc'), 'serve_bad_format')
        self.assertEqual(self.code('9104=Bad_Name:80'), 'serve_bad_format')
        self.assertEqual(self.code('9105=localhost:80'), 'serve_not_service_name')
        self.assertEqual(self.code('9106=-x:80'), 'serve_not_service_name')
        self.assertEqual(self.code('9004=a:80,9004=b:80'), 'serve_duplicate_port')

    def test_own_namespace_forms_and_spaces_are_accepted(self):
        ok = ('9004=svc-0b4a8def-headscale.%s.svc.cluster.local:8080, 8069=svc-1a2b3c4d:80,'
              ' 8070=svc-1a2b3c4d.%s:80, 8071=SVC-X.%s.svc:1' % (_NS, _NS, _NS))
        self.assertIsNone(tss.check_serve(ok, _NS))
        self.assertIsNone(tss.check_serve('', _NS))
        self.assertIsNone(tss.check_serve(None, _NS))
        self.assertIsNone(tss.check_serve(' , ,', _NS))

    def test_unknown_namespace_only_accepts_bare_names(self):
        self.assertIsNone(tss.check_serve('8069=svc-1a2b3c4d:80', ''))
        self.assertEqual(self.code('8069=svc-1a2b3c4d.%s:80' % _NS, ns=''), 'serve_other_namespace')

    def test_at_most_32_forwards(self):
        raw = ','.join('%d=svc-a:80' % p for p in range(1000, 1033))
        self.assertEqual(self.code(raw), 'serve_too_many')
        self.assertIsNone(tss.check_serve(','.join('%d=svc-a:80' % p for p in range(1000, 1032)), _NS))

    def test_non_string_is_a_format_error_and_entries_are_sanitised(self):
        self.assertEqual(self.code(['9004=a:80']), 'serve_bad_format')
        code, params = tss.check_serve('9004=<script>:80', _NS)
        self.assertEqual(code, 'serve_bad_format')
        self.assertNotIn('<', params['entry'])


@tagged('post_install', '-at_install')
class TestLoginRules(BaseCase):

    def code(self, plane, server, key):
        result = tss.check_login(plane, server, key)
        return result and result[0]

    def test_key_kind_must_match_the_control_plane(self):
        self.assertEqual(self.code('official', '', 'hskey-auth-abc'), 'auth_key_not_tailscale')
        self.assertEqual(self.code('official', '', 'a' * 48), 'auth_key_not_tailscale')
        self.assertEqual(self.code('workspace', '', 'tskey-auth-kABC'), 'auth_key_is_tailscale')
        self.assertEqual(self.code('custom', 'https://hs.example.com', 'tskey-auth-k'), 'auth_key_is_tailscale')
        self.assertIsNone(self.code('official', '', 'tskey-auth-kABC'))
        self.assertIsNone(self.code('workspace', '', 'hskey-auth-abc'))
        self.assertIsNone(self.code('custom', 'https://hs.example.com', 'hskey-auth-abc'))

    def test_custom_pointing_at_official_tailscale_accepts_a_tskey(self):
        self.assertIsNone(self.code('custom', 'https://controlplane.tailscale.com', 'tskey-auth-k'))

    def test_custom_needs_a_usable_url(self):
        self.assertEqual(self.code('custom', '', None), 'login_server_required')
        self.assertEqual(self.code('custom', '   ', 'hskey-auth-a'), 'login_server_required')
        for bad in ('headscale.example.com', 'ftp://hs.example.com', 'https://', 'https://a b.com',
                    'http://hs_x.example.com'):
            self.assertEqual(self.code('custom', bad, None), 'login_server_invalid', bad)
        internal = 'http://svc-0b4a8def-headscale.%s.svc.cluster.local:8080' % _NS
        self.assertIsNone(self.code('custom', internal, None))

    def test_whitespace_inside_the_key(self):
        self.assertEqual(self.code('official', '', 'tskey-auth-k\nABC'), 'auth_key_whitespace')

    def test_no_key_is_fine_and_legacy_login_server_means_custom(self):
        self.assertIsNone(self.code('official', '', None))
        self.assertIsNone(self.code('official', '', ''))
        self.assertEqual(self.code(None, 'https://hs.example.com', 'tskey-auth-k'), 'auth_key_is_tailscale')
        self.assertEqual(tss.effective_control_plane('bogus', ''), 'official')


@tagged('post_install', '-at_install')
class TestPublicReportLoginFields(BaseCase):
    """operator 的 login_error／authkey_configured → 平台的 login_error／authkey_ignored（再驗一次）。"""

    def report(self, **over):
        payload = {
            'probe': 'ok', 'backend_state': 'NeedsLogin', 'online': None,
            'login': {'kind': 'none'}, 'serve': {}, **over,
        }
        return vns.public_report(payload, can_manage=True, control_plane='workspace')

    def test_closed_login_error_codes_pass_through(self):
        for code in ('authkey_expired', 'authkey_used', 'authkey_invalid', 'control_unreachable', 'other'):
            self.assertEqual(self.report(login_error=code)['login_error'], code)

    def test_unknown_login_error_text_never_passes_through(self):
        for junk in ('authkey expired', 'The last login error was: x', 5, ['authkey_expired']):
            self.assertIsNone(self.report(login_error=junk)['login_error'], junk)

    def test_login_error_only_while_the_node_is_not_logged_in(self):
        self.assertIsNone(self.report(backend_state='Running', online=True,
                                      login_error='authkey_expired')['login_error'])
        self.assertIsNone(self.report(probe='exec_failed', login_error='authkey_expired')['login_error'])

    def test_authkey_ignored_only_for_a_logged_in_node_with_a_key(self):
        running = {'backend_state': 'Running', 'online': True}
        self.assertTrue(self.report(authkey_configured=True, **running)['authkey_ignored'])
        self.assertFalse(self.report(authkey_configured=False, **running)['authkey_ignored'])
        self.assertFalse(self.report(authkey_configured='yes', **running)['authkey_ignored'])
        self.assertFalse(self.report(authkey_configured=True)['authkey_ignored'], 'NeedsLogin 時 key 會被用')


# --------------------------------------------------------------------------- 存檔驗證（template）
class TestTailscaleSaveValidation(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_tailscale')

    def test_seed_declares_the_save_validators(self):
        specs = {s['key']: s for s in self.tpl._parse_helm_value_specs()['optional']}
        self.assertEqual(specs['tailscale.serve'].get('validate'), 'tailscale_serve')
        self.assertEqual(specs['tailscale.controlPlane'].get('validate'), 'tailscale_login')

    def test_bad_serve_entry_is_rejected_with_the_entry_in_the_message(self):
        msg = self.tpl._validate_spec_formats(
            {'tailscale.serve': '9102=svc-x.other-ns.svc.cluster.local:80'}, namespace=_NS)
        self.assertTrue(msg)
        self.assertIn('9102=svc-x.other-ns.svc.cluster.local:80', msg)

    def test_good_serve_passes(self):
        self.assertIsNone(self.tpl._validate_spec_formats(
            {'tailscale.serve': '9004=svc-0b4a8def-headscale.%s.svc.cluster.local:8080' % _NS},
            namespace=_NS))

    def test_mismatched_key_is_rejected_using_the_stored_control_plane(self):
        existing = {'tailscale': {'controlPlane': 'workspace'}}
        self.assertTrue(self.tpl._validate_spec_formats(
            {'tailscale.authKey': 'tskey-auth-kABC'}, existing_values=existing, namespace=_NS))
        self.assertIsNone(self.tpl._validate_spec_formats(
            {'tailscale.authKey': 'hskey-auth-abc'}, existing_values=existing, namespace=_NS))

    def test_switching_plane_checks_the_stored_key(self):
        existing = {'tailscale': {'controlPlane': 'workspace', 'authKey': 'hskey-auth-abc'}}
        self.assertTrue(self.tpl._validate_spec_formats(
            {'tailscale.controlPlane': 'official'}, existing_values=existing, namespace=_NS))
        # 同一請求清掉 key（#1455 clear_keys）就不再拿舊 key 比。
        self.assertIsNone(self.tpl._validate_spec_formats(
            {'tailscale.controlPlane': 'official'}, existing_values=existing, namespace=_NS,
            cleared_keys=['tailscale.authKey']))

    def test_custom_without_url_is_rejected(self):
        self.assertTrue(self.tpl._validate_spec_formats({'tailscale.controlPlane': 'custom'}, namespace=_NS))

    def test_unrelated_save_does_not_revalidate_a_stored_bad_combination(self):
        """已存的錯配（舊版允許存進去）不擋租戶改別的欄位；只有動到登入三欄才驗。"""
        existing = {'tailscale': {'controlPlane': 'custom', 'loginServer': ''}}
        self.assertIsNone(self.tpl._validate_spec_formats(
            {'tailscale.hostname': 'office'}, existing_values=existing, namespace=_NS))

    def test_deploy_assembly_keeps_a_stored_value_that_save_would_reject(self):
        """存檔驗證不得變成部署時把整欄丟掉（chart 只略過壞的那一筆）。"""
        values = {'tailscale': {'serve': '9004=svc-a:80,70000=a:1'}}
        self.tpl._apply_spec_formats(values)
        self.assertEqual(values['tailscale']['serve'], '9004=svc-a:80,70000=a:1')

    def test_other_templates_are_untouched(self):
        pg = self.env.ref('woow_paas_platform.cloud_app_postgresql')
        self.assertIsNone(pg._validate_spec_formats({'tailscale.serve': '70000=a:1'}, namespace=_NS))


# --------------------------------------------------------------------------- 端點
class TestTailscaleEndpointsFollowup(HttpCase):

    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex[:8]
        login = 'vpnfu_owner_%s' % suffix
        self.owner = self.env['res.users'].sudo().with_context(no_reset_password=True).create({
            'name': login, 'login': login, 'password': login, 'email': login + '@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'VPN fu ' + suffix, 'owner_id': self.owner.id})
        self.service = _internal_provision(self.env['woow_paas_platform.cloud_service'].sudo()).create({
            'name': 'ts', 'workspace_id': self.workspace.id,
            'template_id': self.env.ref('woow_paas_platform.cloud_app_tailscale').id,
            'state': 'running', 'helm_namespace': 'paas-ws-vpnfu-' + suffix,
            'helm_release_name': 'svc-ts-' + suffix,
            'helm_values': json.dumps({'tailscale': {'controlPlane': 'workspace'}}),
        })
        # 節流表是模組層級：每個測試從乾淨的開始。
        from odoo.addons.woow_paas_platform.controllers import paas as paas_controller
        paas_controller._NODE_STATUS_CACHE.clear()

    def _rpc(self, path, **params):
        self.authenticate(self.owner.login, self.owner.login)
        response = self.url_open(path, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params}),
            headers={'Content-Type': 'application/json'})
        self.env.invalidate_all()
        body = response.json()
        self.assertNotIn('error', body, body)
        return body['result']

    def _assert_no_upgrade(self, getter):
        client = getter.return_value
        client.upgrade_release.assert_not_called()
        client.upgrade_preflight.assert_not_called()

    def _node_status(self, client):
        with patch(_CONTROLLER + '.get_paas_operator_client', return_value=client):
            return self._rpc('/api/workspaces/%s/services/%s/node-status'
                             % (self.workspace.id, self.service.id))

    def test_repeated_node_status_within_the_window_asks_the_operator_once(self):
        client = MagicMock()
        client.get_vpn_node_status.return_value = {
            'probe': 'ok', 'backend_state': 'NeedsLogin', 'login_error': 'authkey_expired',
            'login': {'kind': 'none'}, 'serve': {}}
        first = self._node_status(client)
        second = self._node_status(client)
        self.assertEqual(client.get_vpn_node_status.call_count, 1)
        self.assertEqual(first['data']['report'], second['data']['report'])
        self.assertEqual(second['data']['report']['login_error'], 'authkey_expired')

    def test_operator_failures_are_not_cached(self):
        from ..services.paas_operator import PaaSOperatorAPIError
        client = MagicMock()
        client.get_vpn_node_status.side_effect = PaaSOperatorAPIError('boom', status_code=500)
        self._node_status(client)
        self._node_status(client)
        self.assertEqual(client.get_vpn_node_status.call_count, 2)

    def test_reconfigure_rejects_a_foreign_namespace_forward_before_any_upgrade(self):
        before = self.service.helm_values
        with patch(_CONTROLLER + '.get_paas_operator_client') as getter:
            result = self._rpc('/api/workspaces/%s/services/%s' % (self.workspace.id, self.service.id),
                               action='update',
                               values={'tailscale.serve': '9102=svc-x.other-ns.svc.cluster.local:80'})
        self.assertFalse(result['success'])
        self.assertIn('9102=svc-x.other-ns.svc.cluster.local:80', result['error'])
        self.assertEqual(self.service.helm_values, before)
        self.assertEqual(self.service.state, 'running')
        self._assert_no_upgrade(getter)

    def test_reconfigure_rejects_a_tailscale_key_for_the_workspace_headscale(self):
        with patch(_CONTROLLER + '.get_paas_operator_client') as getter:
            result = self._rpc('/api/workspaces/%s/services/%s' % (self.workspace.id, self.service.id),
                               action='update', values={'tailscale.authKey': 'tskey-auth-kABC'})
        self.assertFalse(result['success'])
        self.assertNotIn('tskey-auth-kABC', result['error'], '錯誤訊息不回顯 key')
        self._assert_no_upgrade(getter)


# --------------------------------------------------------------------------- 前端靜態守線
@tagged('post_install', '-at_install')
class TestFollowupFrontendWiring(BaseCase):

    def _read(self, rel):
        return (_STATIC / rel).read_text(encoding='utf-8')

    def test_both_forms_run_the_spec_driven_settings_check(self):
        for rel in ('pages/service/tabs/ConfigurationTab.js', 'pages/configure/AppConfigurationPage.js'):
            self.assertIn('settingsErrors(', self._read(rel), rel)

    def test_every_new_tenant_string_has_a_zh_tw_translation(self):
        import re
        po = (Path(__file__).resolve().parents[1] / 'i18n' / 'zh_TW.po').read_text(encoding='utf-8')
        msgids = set(re.findall(r'^msgid "(.*)"$', po, re.M))
        for rel in ('utils/vpn_node_status.js', 'utils/tailscale_settings.js'):
            for match in re.finditer(r'_t\(\s*"((?:[^"\\]|\\.)*)"', self._read(rel)):
                self.assertIn(match.group(1), msgids, 'zh_TW.po 缺少：%s' % match.group(1))
