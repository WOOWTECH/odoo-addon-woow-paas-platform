# -*- coding: utf-8 -*-
"""INFRA-TS-004-1：設定欄位在某個條件下會被 chart 略過時（spec ``ignored_when``），存檔要擋下。

2026-10-16 商用驗收實測（INFRA-TS-004）：VPN 節點（tailscale chart）控制平面選「同工作區
Headscale」時，chart 0.1.4（#1554）會**忽略** ``tailscale.tags``（寫一行 log、export
``TS_TAGS=``）——Headscale 不接受節點用 pre-auth key 登入時自己要求的標籤，帶了會被登出。
欄位說明已寫「請留空」，但設定頁照樣接受 ``tag:qa``、回 success、服務白白升級一次
（rev 22），儲存後畫面也沒有任何「tags 未套用」的提示。

修法同一個判準（spec 的 ``"ignored_when": {"key": <條件欄位>, "values": [...]}``）：

* seed：tailscale 的 ``tailscale.tags`` 標 ``ignored_when`` controlPlane ∈ {workspace}；
* 後端 ``CloudAppTemplate._ignored_when_violation``：條件成立、欄位有值、且這次請求碰到
  欄位或條件欄位 ⇒ 開通與儲存設定都整筆拒絕（在任何寫入與 helm upgrade 之前）；
  請求沒碰到這兩格（例如只改別的 API 呼叫）照舊放行，不讓既有服務被鎖死；
* 前端 HelmValueForm／ConfigurationTab：條件成立時欄位下顯示原因、空白時停用輸入、
  有值時送出前就標錯；檢視模式在值旁標「未套用」。
"""
import json
import os
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAAS = os.path.join(_SRC, 'static', 'src', 'paas')
REFUSED = 'is not applied when'
BACKEND_MSG = ('"%(label)s" is not applied when "%(condition)s" is "%(choice)s"; '
               'clear it before saving.')
FRONTEND_MSG = 'Not applied when "%(condition)s" is "%(choice)s"; leave this field empty.'
VIEW_MSG = 'Not applied: "%(condition)s" is "%(choice)s".'
PASSED_CHECKS = 'stopped at the compute preflight (all value checks passed)'


def _probe_template(env, specs):
    return env['woow_paas_platform.cloud_app_template'].create({
        'name': 'Ignored-when probe', 'slug': 'ignored-when-probe-ts004',
        'category': 'web', 'helm_repo_url': '',
        'helm_chart_name': 'oci://example/probe', 'helm_chart_version': '0.1.0',
        'helm_value_specs': json.dumps({'required': [], 'optional': specs}),
    })


_MODE = {'key': 'mode', 'label': '模式', 'type': 'select', 'default': 'a',
         'options': [{'value': 'a', 'label': '甲'}, {'value': 'b', 'label': '乙'}]}
_EXTRA = {'key': 'extra', 'label': '附加', 'type': 'text',
          'ignored_when': {'key': 'mode', 'values': ['b']}}
_OTHER = {'key': 'other', 'label': '其他', 'type': 'text'}


@tagged('post_install', '-at_install')
class TestIgnoredWhenSeedTs004(TransactionCase):

    def test_tailscale_tags_ignored_in_workspace_mode(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_tailscale')
        specs = tpl._ignored_when_value_specs()
        self.assertEqual(set(specs), {'tailscale.tags'})
        cond_key, values = specs['tailscale.tags'][1:]
        self.assertEqual(cond_key, 'tailscale.controlPlane')
        self.assertEqual(values, frozenset({'workspace'}))
        # 條件欄位必須是同一份 specs 裡的租戶欄位，且值是它真的有的選項。
        options = {
            o['value'] for o in next(
                s for s in tpl._parse_helm_value_specs()['optional']
                if s['key'] == 'tailscale.controlPlane')['options']}
        self.assertLessEqual(values, options)

    def test_malformed_declarations_are_ignored(self):
        """seed 寫錯（非 dict、條件鍵不存在、values 不是非空字串清單）不得擋住存檔。"""
        tpl = _probe_template(self.env, [
            _MODE,
            dict(_EXTRA, key='e1', ignored_when='mode=b'),
            dict(_EXTRA, key='e2', ignored_when={'key': 'missing', 'values': ['b']}),
            dict(_EXTRA, key='e3', ignored_when={'key': 'mode', 'values': 'b'}),
            dict(_EXTRA, key='e4', ignored_when={'key': 'mode', 'values': []}),
            dict(_EXTRA, key='e5', ignored_when={'key': 'e5', 'values': ['x']}),
            _EXTRA,
        ])
        self.assertEqual(set(tpl._ignored_when_value_specs()), {'extra'})


@tagged('post_install', '-at_install')
class TestIgnoredWhenViolationTs004(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tpl = _probe_template(self.env, [_MODE, _EXTRA, _OTHER])

    def _check(self, submitted, base=None):
        return self.tpl._ignored_when_violation(submitted, base or {})

    def test_refused_when_condition_holds_and_value_set(self):
        for submitted, base in (
                ({'mode': 'b', 'extra': 'x'}, {}),
                ({'extra': 'x'}, {'mode': 'b'}),            # 條件來自既有值
                ({'mode': 'b'}, {'extra': 'x'}),            # 切到條件、既有值還在
                ({'mode': ' b ', 'extra': ' x '}, {}),
        ):
            with self.subTest(submitted=submitted, base=base):
                msg = self._check(submitted, base)
                self.assertTrue(msg)
                self.assertIn(REFUSED, msg)
                # 訊息用租戶在設定頁看到的 label 與選項名稱（seed 已是中文）。
                self.assertIn('附加', msg)
                self.assertIn('模式', msg)
                self.assertIn('乙', msg)

    def test_allowed(self):
        for submitted, base in (
                ({'mode': 'b', 'extra': ''}, {}),
                ({'mode': 'b', 'extra': '   '}, {}),
                ({'mode': 'b', 'extra': None}, {}),
                ({'mode': 'a', 'extra': 'x'}, {}),
                ({'extra': 'x'}, {}),                       # 條件欄位用 spec 預設 a
                ({'mode': 'b'}, {}),
                ({'other': 'y'}, {'mode': 'b', 'extra': 'x'}),  # 沒碰到這兩格：不鎖死既有服務
                ({}, {'mode': 'b', 'extra': 'x'}),
                ({'mode': 'a'}, {'extra': 'x'}),
        ):
            with self.subTest(submitted=submitted, base=base):
                self.assertIsNone(self._check(submitted, base))

    def test_nested_base_values(self):
        tpl = _probe_template(self.env, [
            dict(_MODE, key='t.mode'),
            dict(_EXTRA, key='t.extra', ignored_when={'key': 't.mode', 'values': ['b']}),
        ])
        self.assertTrue(tpl._ignored_when_violation({'t.extra': 'x'}, {'t': {'mode': 'b'}}))
        self.assertIsNone(tpl._ignored_when_violation({'t.extra': 'x'}, {'t': {'mode': 'a'}}))

    def test_message_translated_for_zh_tw(self):
        self.env['res.lang']._activate_lang('zh_TW')
        msg = self.tpl.with_context(lang='zh_TW')._ignored_when_violation(
            {'mode': 'b', 'extra': 'x'}, {})
        self.assertIn('不會套用', msg)
        self.assertIn('「附加」', msg)


@tagged('post_install', '-at_install')
class TestTailscaleTagsSaveTs004(TransactionCase):
    """真 seed（tailscale）經 controller 開通與儲存設定。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_tailscale')
        self.plan = self.env.ref('woow_paas_platform.plan_tailscale_monthly')
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'TS-004 WS'})
        # 開通走 controller 的 `_PaaSCapability`（要登入身分）：本 workspace 的 internal admin
        # 成員（同 test_helm_values_whitelist_fail_closed_1488）。
        self.member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'TS-004 workspace admin', 'login': 'ts004-workspace-admin',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.ws.id, 'user_id': self.member.id, 'role': 'admin'})
        values = json.loads(self.tpl.helm_default_values)
        values['tailscale'] = {'controlPlane': 'workspace', 'tags': '', 'hostname': 'n1'}
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-ts004', 'workspace_id': self.ws.id,
            'template_id': self.tpl.id, 'state': 'running',
            'helm_release_name': 'svc-ts004ab', 'helm_namespace': 'paas-ws-ts004',
            'helm_values': json.dumps(values),
        })

    def _update(self, values):
        """``_update_service``；通過所有值驗證後停在算力預檢（#1647），不寫 DB、不起背景 upgrade。"""
        client = MagicMock()
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)), \
                patch.object(PaasController, '_reconfigure_compute_quota_denial',
                             return_value={'success': False, 'error': PASSED_CHECKS}):
            result = self.controller._update_service(self.svc, values=values, version=None)
        return result, client

    def test_reconfigure_with_tags_in_workspace_mode_is_refused_before_any_write(self):
        before = self.svc._helm_values_dict()
        # 設定頁「儲存變更」整包送出非密碼欄位（ConfigurationTab.buildSavePayload）。
        for values in (
                {'tailscale.controlPlane': 'workspace', 'tailscale.tags': 'tag:qa',
                 'tailscale.hostname': 'n1', 'tailscale.serve': '',
                 'tailscale.loginServer': '', 'tailscale.noLogs': True},
                {'tailscale.tags': 'tag:qa'},
        ):
            with self.subTest(values=values):
                result, client = self._update(values)
                self.assertFalse(result.get('success'), result)
                self.assertIn(REFUSED, result.get('error', ''))
                self.assertIn('標籤 tags', result['error'])
                self.assertIn('同工作區 Headscale', result['error'])
                client.upgrade_release.assert_not_called()
                self.svc.invalidate_recordset()
                self.assertEqual(self.svc._helm_values_dict(), before)
                self.assertEqual(self.svc.state, 'running')

    def test_switching_to_workspace_with_stored_tags_is_refused(self):
        stored = self.svc._helm_values_dict()
        stored['tailscale'].update({'controlPlane': 'official', 'tags': 'tag:server'})
        self.svc._apply_helm_values(stored)
        result, client = self._update({'tailscale.controlPlane': 'workspace'})
        self.assertFalse(result.get('success'), result)
        self.assertIn(REFUSED, result.get('error', ''))
        client.upgrade_release.assert_not_called()

    def test_reconfigure_with_tags_cleared_passes_the_check(self):
        result, _client = self._update({
            'tailscale.controlPlane': 'workspace', 'tailscale.tags': '',
            'tailscale.hostname': 'n2'})
        self.assertEqual(result.get('error'), PASSED_CHECKS)

    def test_tags_still_accepted_for_other_control_planes(self):
        for plane in ('official', 'custom'):
            with self.subTest(plane=plane):
                result, _client = self._update({
                    'tailscale.controlPlane': plane, 'tailscale.tags': 'tag:server'})
                self.assertEqual(result.get('error'), PASSED_CHECKS)

    def test_unrelated_save_on_a_legacy_service_is_not_locked(self):
        """ignored_when 上線前就存了 tags 的服務：沒碰到 tags／控制平面的請求照舊放行。"""
        stored = self.svc._helm_values_dict()
        stored['tailscale']['tags'] = 'tag:legacy'
        self.svc._apply_helm_values(stored)
        result, _client = self._update({'tailscale.hostname': 'n3'})
        self.assertEqual(result.get('error'), PASSED_CHECKS)

    def test_create_with_tags_in_workspace_mode_is_refused(self):
        count = self.env['woow_paas_platform.cloud_service'].search_count([])
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.member))):
            result = self.controller._create_service(
                workspace=self.ws, template_id=self.tpl.id, name='ts004-new',
                values={'tailscale.controlPlane': 'workspace',
                        'tailscale.tags': 'tag:qa'},
                plan_id=self.plan.id)
        self.assertFalse(result.get('success'), result)
        self.assertIn(REFUSED, result.get('error', ''))
        self.assertEqual(
            self.env['woow_paas_platform.cloud_service'].search_count([]), count)


@tagged('post_install', '-at_install')
class TestIgnoredWhenFrontendContractTs004(TransactionCase):
    """前端與後端同一個判準（靜態讀檔；行為由 tests/ui/test_spec_ignored_when_ts004.mjs 驗）。"""

    def _read(self, *parts):
        with open(os.path.join(_PAAS, *parts), encoding='utf-8') as f:
            return f.read()

    def test_form_and_tab_use_the_shared_helper(self):
        form_js = self._read('components', 'config', 'HelmValueForm.js')
        form_xml = self._read('components', 'config', 'HelmValueForm.xml')
        tab_js = self._read('pages', 'service', 'tabs', 'ConfigurationTab.js')
        tab_xml = self._read('pages', 'service', 'tabs', 'ConfigurationTab.xml')
        self.assertIn('utils/ignored_when', form_js)
        self.assertIn('utils/ignored_when', tab_js)
        # required 與 optional 兩段的文字欄位各一處。
        self.assertEqual(form_xml.count('t-att-disabled="isIgnoredInputDisabled(spec)"'), 2)
        self.assertEqual(form_xml.count('ignoredWhenHint(spec)'), 4)
        validate = tab_js[tab_js.find('validateForm() {'):]
        validate = validate[:validate.find('\n    }\n')]
        self.assertIn('ignoredWhenMessage(', validate)
        self.assertIn('ignoredWhenViewNote(spec)', tab_xml)

    def test_new_strings_are_translated(self):
        po = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        for src in (BACKEND_MSG, FRONTEND_MSG, VIEW_MSG):
            with self.subTest(src=src):
                self.assertTrue(code.get(src), 'zh_TW.po 缺中文：%r' % src)
