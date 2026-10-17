# -*- coding: utf-8 -*-
"""service-update-api-validation：服務更新／開通 API 依 helm_value_specs 的 type 驗證值。

商用驗收（2026-10-16）實測，``_update_service`` 只查鍵白名單、``format`` 欄位與
create_only，值本身完全不檢查：

* AI-LLM-017-1：``'90d\\nevil_setting: true'``（換行注入）、``'1d'``（select 選項外）、
  ``apiDocs='maybe'``（boolean 填字串）都被接受並套用（新 helm revision＋Pod 重建）；
* AI-LLM-017-2：送 ``null`` 讓 helm 刪掉 chart 預設（apiDocs 預設 true → /docs 被關）；
* AI-LLM-017-3：未知頂層參數（chart_version、helm_chart_version）被靜默吞掉回 success，
  送回同一組值也照樣跑一次 helm upgrade；
* INFRA-EMQX-011-1／INFRA-HS-006-1：values 是陣列時回 Python 例外原文；
* INFRA-EMQX-024-3：EMQX ngrok 保留位址可存任意字串（含空白與 shell 字元）；
* INFRA-HS-013-4：Headscale ngrok 網域說明假設「不屬於帳號的網域會立刻失敗」，實測不成立；
  改網域也沒有二次確認；
* X-LC-002-4：開通（``_create_service``）同樣不驗 select／number／boolean。
"""
import json
import os
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_app_template import (
    SPEC_FORMAT_HOST_PORT,
    SPEC_FORMAT_PARSERS,
    check_spec_value,
    parse_host_port,
)
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_BG_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.PaaSOperatorClient'
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TAB = os.path.join(_SRC, 'static', 'src', 'paas', 'pages', 'service', 'tabs')

# 仿 LiteLLM 範本的四種欄位（select 用 {value,label} 選項、boolean 有預設 true）。
SPECS = {
    'required': [],
    'optional': [
        {'key': 'litellm.spendLogRetention', 'label': '用量明細保留天數', 'type': 'select',
         'default': '90d', 'options': [
             {'value': '7d', 'label': '7 天'}, {'value': '30d', 'label': '30 天'},
             {'value': '90d', 'label': '90 天'}, {'value': '365d', 'label': '365 天'}]},
        {'key': 'litellm.apiDocs', 'label': 'API 說明頁', 'type': 'boolean', 'default': True},
        {'key': 'smtp.port', 'label': 'SMTP 連接埠', 'type': 'number', 'default': 465,
         'min': 1, 'max': 65535, 'integer': True},
        {'key': 'smtp.host', 'label': 'SMTP 主機', 'type': 'text'},
        {'key': 'auth.apiKey', 'label': 'API 金鑰', 'type': 'password'},
        {'key': 'level', 'label': '層級', 'type': 'select', 'options': ['info', 'debug']},
    ],
}

STORED = {
    'litellm': {'spendLogRetention': '30d', 'apiDocs': False},
    'smtp': {'port': 587, 'host': 'smtp.example.com'},
    'auth': {'apiKey': 'sk-current'},
}


class _SyncThread:
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


@tagged('post_install', '-at_install')
class TestCheckSpecValue(TransactionCase):
    """純函式：各 type 的合法／不合法形態。"""

    def test_boolean_accepts_only_real_booleans(self):
        spec = {'type': 'boolean'}
        for value in (True, False):
            self.assertIsNone(check_spec_value(spec, value)[1], value)
        for value in ('maybe', 'true', 1, 0, '', {'x': 1}, [True]):
            self.assertEqual(check_spec_value(spec, value)[1][0], 'not_boolean', repr(value))

    def test_number_type_range_and_integer(self):
        spec = {'type': 'number', 'min': 1, 'max': 65535, 'integer': True}
        for value in (1, 465, 65535, 587.0, ''):
            self.assertIsNone(check_spec_value(spec, value)[1], repr(value))
        # 用 tuple 清單而不是 dict：[1] 不能當 dict 鍵，而且 True 與 1 會被 dict 合併成同一鍵。
        cases = [
            ('587', 'not_number'), (True, 'not_number'), (float('nan'), 'not_number'),
            (float('inf'), 'not_number'), ([1], 'not_number'),
            (465.5, 'not_integer'), (0, 'below_min'), (70000, 'above_max'),
        ]
        for value, code in cases:
            self.assertEqual(check_spec_value(spec, value)[1][0], code, repr(value))
        self.assertIsNone(check_spec_value({'type': 'number'}, 1.5)[1], '沒有 integer 時收小數')

    def test_select_must_be_one_of_the_options(self):
        spec = SPECS['optional'][0]
        self.assertIsNone(check_spec_value(spec, '7d')[1])
        for value in ('1d', '90d\nevil_setting: true', '', 7, '7D'):
            error = check_spec_value(spec, value)[1]
            self.assertEqual(error[0], 'not_option', repr(value))
            self.assertIn('365d', error[1]['options'])
        plain = {'type': 'select', 'options': ['info', 'debug']}
        self.assertIsNone(check_spec_value(plain, 'debug')[1])
        self.assertEqual(check_spec_value(plain, 'trace')[1][0], 'not_option')

    def test_text_and_password_are_single_line_strings(self):
        for spec in ({'type': 'text'}, {'type': 'password'}, {}):
            self.assertIsNone(check_spec_value(spec, 'smtp.example.com')[1])
            self.assertIsNone(check_spec_value(spec, '')[1])
            self.assertIsNone(check_spec_value(spec, '全形　空白 OK')[1])
            for value in ('a\nb: c', 'a\rb', 'a\tb', 'x\x00', 'x\x85[core]', 'x\u2028y', 'x\u2029y'):
                self.assertEqual(check_spec_value(spec, value)[1][0], 'control_chars', repr(value))
            for value in ({'nested': 1}, ['a']):
                self.assertEqual(check_spec_value(spec, value)[1][0], 'not_text', repr(value))
        self.assertEqual(check_spec_value({'type': 'text'}, 5)[1][0], 'not_text')


@tagged('post_install', '-at_install')
class TestParseHostPort(TransactionCase):
    """``format: host_port``（EMQX ngrok.remoteAddr）：與 chart ngrok.sh 的規則一致。"""

    def test_registered_as_a_spec_format(self):
        self.assertEqual(SPEC_FORMAT_HOST_PORT, 'host_port')
        self.assertIs(SPEC_FORMAT_PARSERS['host_port'], parse_host_port)

    def test_valid_and_empty(self):
        self.assertEqual(parse_host_port('1.tcp.ngrok.io:12345'), ('1.tcp.ngrok.io:12345', None))
        self.assertEqual(parse_host_port(' 1.TCP.ngrok.IO:443 '), ('1.tcp.ngrok.io:443', None))
        self.assertEqual(parse_host_port('10.0.0.5:1883'), ('10.0.0.5:1883', None))
        for raw in (None, '', '   '):
            self.assertEqual(parse_host_port(raw), (None, None), repr(raw))

    def test_error_codes(self):
        cases = {
            'tcp://bad host;touch /tmp/cafinal024-pwn': 'host_port_scheme',
            'tcp://1.tcp.ngrok.io:12345': 'host_port_scheme',
            '1.tcp.ngrok.io': 'host_port_invalid',
            '1.tcp.ngrok.io:': 'host_port_invalid',
            ':12345': 'host_port_invalid',
            'ngrok:12345': 'host_port_invalid',
            '1.tcp.ngrok.io:0': 'host_port_invalid',
            '1.tcp.ngrok.io:65536': 'host_port_invalid',
            '1.tcp.ngrok.io:12345/x': 'host_port_invalid',
            'bad host.ngrok.io:1': 'host_port_invalid',
            'a;b.ngrok.io:1': 'host_port_invalid',
            '1.tcp.ngrok.io:1:2': 'host_port_invalid',
            'ünicode.io:1': 'host_port_invalid',
        }
        for raw, code in cases.items():
            self.assertEqual(parse_host_port(raw)[1][0], code, raw)
        self.assertEqual(parse_host_port(12345)[1][0], 'not_text')


class _ProbeBase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Value validation probe', 'slug': 'svcupd-probe',
            'category': 'ai_llm', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example/svcupd-probe', 'helm_chart_version': '0.1.0',
            'workload_type': 'container', 'post_deploy_init_type': 'none',
            'mcp_enabled': False, 'helm_default_values': '{}',
            'helm_value_specs': json.dumps(SPECS),
        })
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS svcupd', 'owner_id': self.admin.id})
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'svc-svcupd', 'reference_id': 'test-svcupd-ref',
            'state': 'running', 'subdomain': 'svc-svcupd',
            'helm_namespace': 'paas-ws-svcupd', 'helm_release_name': 'svc-svcupd',
            'helm_chart_version': '0.1.0',
            'helm_values': json.dumps(STORED),
        })

    def _update(self, values, **kwargs):
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 3, 'chart_version': '0.1.0'}
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.admin))), \
                patch(_BG_CLIENT, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = PaasController()._update_service(
                self.svc, values=values, version=None, **kwargs)
        self.svc.invalidate_recordset()
        return result, client

    def _assert_refused(self, values, needle=None):
        before = self.svc._helm_values_dict()
        result, client = self._update(values)
        self.assertFalse(result.get('success'), (values, result))
        if needle:
            self.assertIn(needle, result.get('error', ''), values)
        client.upgrade_release.assert_not_called()
        self.assertEqual(self.svc._helm_values_dict(), before, values)
        self.assertEqual(self.svc.state, 'running', values)
        return result


@tagged('post_install', '-at_install')
class TestValidateSpecValues(_ProbeBase):
    """model：null 語意、送回目前值放行、訊息帶 label 且有中文。"""

    def test_null_restores_the_spec_default_or_clears_text(self):
        values, error = self.tpl._validate_spec_values({
            'litellm.apiDocs': None, 'litellm.spendLogRetention': None,
            'smtp.host': None, 'smtp.port': None})
        self.assertIsNone(error)
        self.assertEqual(values, {
            'litellm.apiDocs': True, 'litellm.spendLogRetention': '90d',
            'smtp.host': '', 'smtp.port': 465})

    def test_null_is_refused_without_a_default(self):
        for key in ('auth.apiKey', 'level'):
            _values, error = self.tpl._validate_spec_values({key: None})
            self.assertIn('null', error or '', key)

    def test_current_value_echo_passes_even_if_it_breaks_the_rules(self):
        """表單整包送出時，別格「這次之前存進去」的舊值不能讓存檔失敗。"""
        values, error = self.tpl._validate_spec_values(
            {'litellm.spendLogRetention': '1d', 'litellm.apiDocs': 'maybe'},
            current_values={'litellm.spendLogRetention': '1d', 'litellm.apiDocs': 'maybe'})
        self.assertIsNone(error)
        self.assertEqual(values['litellm.spendLogRetention'], '1d')
        _values, error = self.tpl._validate_spec_values(
            {'litellm.apiDocs': 'maybe'}, current_values={'litellm.apiDocs': True})
        self.assertTrue(error, '只有「等於目前值」才放行，改成別的不合法值照擋')

    def test_message_names_the_field_and_is_translated(self):
        _values, error = self.tpl._validate_spec_values({'litellm.apiDocs': 'maybe'})
        self.assertIn('API 說明頁', error)
        self.assertIn('true or false', error)
        self.env['res.lang']._activate_lang('zh_TW')
        tpl = self.tpl.with_context(lang='zh_TW')
        for values, needle in (
                ({'litellm.apiDocs': 'maybe'}, '開啟或關閉'),
                ({'litellm.spendLogRetention': '1d'}, '只能是下列其中一個'),
                ({'smtp.host': 'a\nb: c'}, '單行文字'),
                ({'smtp.port': 70000}, '不得大於 65535'),
                ({'smtp.port': 'x'}, '必須是數字'),
                ({'level': None}, '空值'),
        ):
            _values, error = tpl._validate_spec_values(values)
            self.assertIn(needle, error or '', (values, error))

    def test_new_strings_are_translated(self):
        po = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        for src in (
            '%(label)s must be on or off (true or false).',
            '%(label)s must be a number.',
            '%(label)s must be a whole number.',
            '%(label)s must be at least %(min)s.',
            '%(label)s must be at most %(max)s.',
            '%(label)s must be one of: %(options)s.',
            '%(label)s must be a single line without line breaks or control characters.',
            '%(label)s cannot be set to an empty (null) value.',
            '%(label)s must be text.',
            'Reserved address: enter host:port only, without tcp:// (for example '
            '1.tcp.ngrok.io:12345 instead of "%(entry)s").',
            'Reserved address: "%(entry)s" is not a host:port address such as '
            '1.tcp.ngrok.io:12345.',
            'Unknown parameters: %(params)s',
            'No changes to apply.',
            'Back to editing',
            'Confirm and save',
        ):
            with self.subTest(src=src):
                self.assertTrue(code.get(src), 'zh_TW.po 缺中文：%r' % src)


@tagged('post_install', '-at_install')
class TestUpdateServiceValueValidation(_ProbeBase):
    """controller ``_update_service``：被拒的請求什麼都不寫、不升級。"""

    def test_values_must_be_an_object(self):
        for values in (['image.repository'], 'litellm.apiDocs', 5):
            result = self._assert_refused(values)
            self.assertEqual(result['error'], 'Invalid request')
            self.assertNotIn('object has no attribute', result['error'])

    def test_ai_llm_017_values_are_refused(self):
        self._assert_refused({'litellm.spendLogRetention': '90d\nevil_setting: true'}, '用量明細')
        self._assert_refused({'litellm.spendLogRetention': '1d'}, '365d')
        self._assert_refused({'litellm.apiDocs': 'maybe'}, 'true or false')
        self._assert_refused({'smtp.host': 'smtp.example.com\nextra: 1'}, 'line breaks')
        self._assert_refused({'smtp.port': '587'}, 'number')
        self._assert_refused({'auth.apiKey': None}, 'null')

    def test_valid_change_is_applied(self):
        result, client = self._update({'litellm.spendLogRetention': '7d', 'litellm.apiDocs': True})
        self.assertTrue(result.get('success'), result)
        self.assertFalse(result.get('no_changes'))
        client.upgrade_release.assert_called_once()
        stored = self.svc._helm_values_dict()
        self.assertEqual(stored['litellm'], {'spendLogRetention': '7d', 'apiDocs': True})

    def test_null_restores_the_default_instead_of_deleting_it(self):
        """AI-LLM-017-2：null 不再變成 helm 的 null（會刪掉 chart 預設 true）。"""
        result, client = self._update({'litellm.apiDocs': None})
        self.assertTrue(result.get('success'), result)
        self.assertIs(self.svc._helm_values_dict()['litellm']['apiDocs'], True)
        sent = client.upgrade_release.call_args.kwargs['values']
        self.assertIs(sent['litellm']['apiDocs'], True)

    def test_resubmitting_the_current_values_is_a_no_op(self):
        """AI-LLM-017-3：送回同一組值不呼叫 operator、不產生 revision。"""
        before = self.svc._helm_values_dict()
        result, client = self._update({
            'litellm.spendLogRetention': '30d', 'litellm.apiDocs': False,
            'smtp.port': 587, 'smtp.host': 'smtp.example.com'})
        self.assertTrue(result.get('success'), result)
        self.assertTrue(result.get('no_changes'), result)
        self.assertEqual(result['data']['id'], self.svc.id)
        client.upgrade_release.assert_not_called()
        self.assertEqual(self.svc._helm_values_dict(), before)
        self.assertEqual(self.svc.state, 'running')

    def test_masked_secret_echo_is_also_a_no_op(self):
        result, client = self._update({'auth.apiKey': '***', 'litellm.apiDocs': False})
        self.assertTrue(result.get('no_changes'), result)
        client.upgrade_release.assert_not_called()
        self.assertEqual(self.svc._helm_values_dict()['auth']['apiKey'], 'sk-current')

    def test_empty_request_keeps_the_reapply_semantics(self):
        """空請求（沒有值、沒有 addon、沒有 clear_keys）維持既有的「以目前設定重新套用」，
        是否改成 no-op 屬產品決定（#1456）；前端本來就不送空請求。"""
        result, client = self._update({})
        self.assertTrue(result.get('success'), result)
        self.assertFalse(result.get('no_changes'))
        client.upgrade_release.assert_called_once()

    def test_upgrade_failure_exit_is_never_short_circuited(self):
        """#1552：背景 upgrade 失敗轉 error 時，重送同一組值就是租戶唯一的重試方式。"""
        with patch.object(type(self.svc), '_upgrade_failure_reconfigure_exit',
                          return_value='retry allowed'):
            result, client = self._update({'litellm.apiDocs': False})
        self.assertTrue(result.get('success'), result)
        self.assertFalse(result.get('no_changes'))
        client.upgrade_release.assert_called_once()

    def test_stored_legacy_value_does_not_block_saving_other_fields(self):
        self.svc._apply_helm_values(dict(STORED, litellm={
            'spendLogRetention': '1d', 'apiDocs': False}))
        result, client = self._update({
            'litellm.spendLogRetention': '1d', 'litellm.apiDocs': True})
        self.assertTrue(result.get('success'), result)
        client.upgrade_release.assert_called_once()
        self.assertIs(self.svc._helm_values_dict()['litellm']['apiDocs'], True)


@tagged('post_install', '-at_install')
class TestUpdateRouteRejectsUnknownParameters(_ProbeBase):
    """AI-LLM-017-3：update 的頂層參數只認 values／version／odoo_addon_ids／clear_keys。"""

    def test_unknown_parameters_are_refused_before_update(self):
        with patch.object(PaasController, '_update_service',
                          return_value={'success': True, 'data': {}}) as mocked, \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.admin))):
            result = PaasController().api_workspace_service(
                self.ws.id, self.svc.id, action='update', values={},
                chart_version='9.9.9', helm_chart_version='9.9.9')
        self.assertFalse(result.get('success'), result)
        self.assertIn('chart_version', result['error'])
        self.assertIn('helm_chart_version', result['error'])
        mocked.assert_not_called()

    def test_known_parameters_still_reach_update(self):
        with patch.object(PaasController, '_update_service',
                          return_value={'success': True, 'data': {}}) as mocked, \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.admin))):
            result = PaasController().api_workspace_service(
                self.ws.id, self.svc.id, action='update',
                values={'litellm.apiDocs': True}, clear_keys=[])
        self.assertTrue(result.get('success'), result)
        mocked.assert_called_once()


@tagged('post_install', '-at_install')
class TestCreateServiceValueValidation(TransactionCase):
    """X-LC-002-4：開通走同一套驗證，在收錢、建服務之前擋下。"""

    def setUp(self):
        super().setUp()
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_mcp_odoo')
        self.plan = self.env.ref('woow_paas_platform.plan_mcp_odoo_monthly')
        self.tpl.write({'helm_value_specs': json.dumps(SPECS)})
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'svcupd create workspace'})
        self.member = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'svcupd create admin', 'login': 'svcupd-create-admin',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': self.member.id, 'role': 'admin'})

    def _create(self, values):
        with patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.member))):
            return PaasController()._create_service(
                workspace=self.workspace, template_id=self.tpl.id, name='svcupd-create',
                values=values, plan_id=self.plan.id)

    def _services(self):
        return self.env['woow_paas_platform.cloud_service'].search([
            ('workspace_id', '=', self.workspace.id)])

    def test_invalid_values_are_refused_before_anything_is_created(self):
        for values, needle in (
                (['litellm.apiDocs'], 'Invalid request'),
                ({'litellm.spendLogRetention': '1d'}, '365d'),
                ({'litellm.apiDocs': 'maybe'}, 'true or false'),
                ({'smtp.port': 0}, 'at least 1'),
                ({'smtp.host': 'a\nb'}, 'line breaks'),
        ):
            with self.subTest(values=values):
                result = self._create(values)
                self.assertFalse(result.get('success'), result)
                self.assertIn(needle, result.get('error', ''))
                self.assertFalse(self._services(), '被擋的開通不得留下服務列')


@tagged('post_install', '-at_install')
class TestSeedSpecsServiceUpdateValidation(TransactionCase):
    """seed：EMQX 保留位址有格式、Headscale 改網域要確認、n8n 連接埠有範圍。"""

    def _optional(self, xmlid):
        tpl = self.env.ref('woow_paas_platform.%s' % xmlid)
        return tpl, {s['key']: s for s in tpl._parse_helm_value_specs()['optional']}

    def test_emqx_reserved_address_is_validated_on_save(self):
        tpl, specs = self._optional('cloud_app_emqx')
        self.assertEqual(specs['ngrok.remoteAddr'].get('format'), 'host_port')
        self.assertIsNone(tpl._validate_spec_formats({'ngrok.remoteAddr': '1.tcp.ngrok.io:12345'}))
        self.assertIsNone(tpl._validate_spec_formats({'ngrok.remoteAddr': ''}), '選填：空值放行')
        self.assertTrue(tpl._validate_spec_formats(
            {'ngrok.remoteAddr': 'tcp://bad host;touch /tmp/cafinal024-pwn'}))

    def test_headscale_domain_change_needs_confirmation(self):
        _tpl, specs = self._optional('cloud_app_headscale')
        spec = specs['ngrok.domain']
        self.assertIn('平台無法核對', spec['description'])
        self.assertIn('重新登入', spec.get('confirm_change') or '')

    def test_n8n_smtp_port_has_a_range(self):
        _tpl, specs = self._optional('cloud_app_n8n')
        spec = specs['main.config.N8N_SMTP_PORT']
        self.assertEqual((spec.get('min'), spec.get('max'), spec.get('integer')), (1, 65535, True))
        self.assertIsNone(check_spec_value(spec, spec['default'])[1])

    def test_every_seed_default_passes_its_own_type_check(self):
        """前置（防假綠）：seed 的預設值本身要過 type 檢查，否則開通頁送預設就被擋。"""
        templates = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False).search([])
        for tpl in templates:
            for key, spec in tpl._value_specs_by_key().items():
                if 'default' not in spec:
                    continue
                with self.subTest(template=tpl.slug, key=key):
                    self.assertIsNone(check_spec_value(spec, spec['default'])[1])


@tagged('post_install', '-at_install')
class TestConfirmChangeFrontendContract(TransactionCase):
    """ConfigurationTab：spec ``confirm_change`` 的值有改 ⇒ 存檔前先顯示說明、按確認才送。"""

    def setUp(self):
        super().setUp()
        with open(os.path.join(_TAB, 'ConfigurationTab.js'), encoding='utf-8') as f:
            self.js = f.read()
        with open(os.path.join(_TAB, 'ConfigurationTab.xml'), encoding='utf-8') as f:
            self.xml = f.read()

    def test_save_asks_for_confirmation_first(self):
        self.assertIn('confirmMessages(payload)', self.js)
        self.assertIn('spec.confirm_change', self.js)
        # Save 按鈕經 onClick.bind 呼叫時第一個參數是事件物件，不能被當成「已確認」。
        self.assertIn('if (confirmed !== true)', self.js)
        self.assertIn('return this.saveChanges(true);', self.js)

    def test_template_renders_the_confirmation(self):
        self.assertIn('state.pendingConfirm.length', self.xml)
        self.assertIn('onClick.bind="confirmSave"', self.xml)
        self.assertIn('onClick.bind="dismissConfirm"', self.xml)
