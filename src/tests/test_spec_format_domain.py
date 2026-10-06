# -*- coding: utf-8 -*-
"""P2：helm_value_specs 的 ``format: domain``（Headscale 的 ``ngrok.domain``）。

chart 把這個值組成 ``ngrok http … --url https://<domain>`` 與 Headscale 的
``server_url``。租戶最常貼錯的是整條網址（``https://xxx.ngrok-free.app/``）或帶埠——
要在**存檔**時就擋下並給看得懂的（中文）訊息。靠 chart 在 render 時 ``fail`` 會重演
EMQX 0.1.1「升級失敗、服務進 error」。

1. 純函式 ``parse_domain``：合法值正規化（小寫、去結尾點），每一種錯誤形態有自己的
   錯誤碼（訊息才講得出「不要加 https://」「不要加埠號」）。
2. 存檔：``_validate_spec_formats`` 回租戶語言的訊息；空值放行（選填欄位）；
   reconfigure 被擋時什麼都不寫、不升級。
3. 部署組裝：``_apply_spec_formats`` 把存值正規化；空值／不合法的存值丟掉（chart 預設
   補位，記 warning 不記值）。
"""
import json
import logging
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_app_template import (
    SPEC_FORMAT_DOMAIN,
    SPEC_FORMAT_PARSERS,
    parse_domain,
)
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_TEMPLATE_LOGGER = 'odoo.addons.woow_paas_platform.models.cloud_app_template'
_KEY = 'ngrok.domain'


@tagged('post_install', '-at_install')
class TestParseDomain(TransactionCase):
    """純函式：正規化與每一種錯誤形態。"""

    def test_registered_as_a_spec_format(self):
        self.assertEqual(SPEC_FORMAT_DOMAIN, 'domain')
        self.assertIs(SPEC_FORMAT_PARSERS['domain'], parse_domain)

    def test_valid_domains_are_normalised(self):
        cases = {
            'abc-123.ngrok-free.app': 'abc-123.ngrok-free.app',
            '  ABC-123.Ngrok-Free.App  ': 'abc-123.ngrok-free.app',
            'vpn.example.com.': 'vpn.example.com',            # FQDN 根的點
            'a.b.c.d.example.co.uk': 'a.b.c.d.example.co.uk',
            'xn--fiq228c.example.com': 'xn--fiq228c.example.com',  # IDN 用 punycode
            '1password.example': '1password.example',
            ('a' * 63) + '.com': ('a' * 63) + '.com',
        }
        for raw, expected in cases.items():
            self.assertEqual(parse_domain(raw), (expected, None), repr(raw))

    def test_empty_is_allowed(self):
        for raw in (None, '', '   ', '\t'):
            self.assertEqual(parse_domain(raw), (None, None), repr(raw))

    def test_error_codes(self):
        cases = {
            'https://abc.ngrok-free.app': 'domain_scheme',
            'http://abc.ngrok-free.app': 'domain_scheme',
            'tcp://0.tcp.ngrok.io:12345': 'domain_scheme',
            'abc.ngrok-free.app/': 'domain_path',
            'abc.ngrok-free.app/path': 'domain_path',
            'abc.ngrok-free.app?x=1': 'domain_path',
            'abc.ngrok-free.app#x': 'domain_path',
            'user@abc.ngrok-free.app': 'domain_path',
            'abc.ngrok-free.app:443': 'domain_port',
            'localhost': 'domain_single_label',
            'headscale': 'domain_single_label',
            'a..example.com': 'domain_invalid',
            '-bad.example.com': 'domain_invalid',
            'bad-.example.com': 'domain_invalid',
            'under_score.example.com': 'domain_invalid',
            '*.example.com': 'domain_invalid',
            'exa mple.com': 'domain_invalid',
            'example.com..': 'domain_invalid',
            '1.2.3.4': 'domain_invalid',                  # IP 不是網域
            'ëxample.com': 'domain_invalid',               # 非 ASCII
            'Kelvin.example.com': 'domain_invalid',   # KELVIN SIGN lower() 會變成 k
            'example‮.com': 'domain_invalid',         # bidi 控制字元
            'a b.example.com': 'domain_invalid',
            ('a' * 64) + '.com': 'domain_invalid',          # label > 63
            '"; rm -rf /': 'domain_path',
            '$(id).example.com': 'domain_invalid',
        }
        for raw, code in cases.items():
            value, error = parse_domain(raw)
            self.assertIsNone(value, repr(raw))
            self.assertEqual(error and error[0], code, repr(raw))

    def test_too_long(self):
        raw = '.'.join(['a' * 60] * 5) + '.com'   # 308 字
        self.assertEqual(parse_domain(raw), (None, ('domain_too_long', {'max': 253})))

    def test_not_text(self):
        self.assertEqual(parse_domain(123), (None, ('not_text', {})))
        self.assertEqual(parse_domain(['a.com']), (None, ('not_text', {})))


class _DomainTemplateBase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Domain format', 'slug': 'domain-format-p2', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example/domain', 'helm_chart_version': '0.1.0',
            'category': 'devops',
            'helm_value_specs': json.dumps({'required': [], 'optional': [
                {'key': 'ngrok.enabled', 'label': 'ngrok', 'type': 'boolean'},
                {'key': _KEY, 'label': 'ngrok 網域', 'type': 'text',
                 'format': 'domain'},
            ]}),
        })
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'domain WS'})
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-domain01', 'workspace_id': self.ws.id,
            'template_id': self.tpl.id, 'state': 'running',
            'helm_release_name': 'svc-domain01', 'helm_namespace': 'paas-ws-domain01',
            'helm_values': json.dumps({'ngrok': {
                'enabled': True, 'domain': ' ABC-123.Ngrok-Free.App '}}),
        })


@tagged('post_install', '-at_install')
class TestDomainFormatOnSave(_DomainTemplateBase):

    def test_spec_is_picked_up(self):
        self.assertEqual(self.tpl._formatted_value_specs(), {_KEY: 'domain'})

    def test_valid_and_empty_pass(self):
        for raw in ('abc-123.ngrok-free.app', 'ABC.Example.com', '', '  '):
            self.assertIsNone(self.tpl._validate_spec_formats({_KEY: raw}), repr(raw))
        self.assertIsNone(self.tpl._validate_spec_formats({'ngrok.enabled': True}))

    def test_invalid_returns_a_specific_message(self):
        for raw, needle in (
                ('https://abc.ngrok-free.app/', 'https://'),
                ('abc.ngrok-free.app/x', 'path'),
                ('abc.ngrok-free.app:443', 'port'),
                ('headscale', 'full domain name'),
                ('bad_host.example.com', 'not a valid domain name'),
        ):
            msg = self.tpl._validate_spec_formats({_KEY: raw})
            self.assertTrue(msg, raw)
            self.assertIn(needle, msg, (raw, msg))

    def test_messages_are_traditional_chinese_for_zh_tw(self):
        self.env['res.lang']._activate_lang('zh_TW')
        tpl = self.tpl.with_context(lang='zh_TW')
        for raw, needle in (
                ('https://abc.ngrok-free.app', '不要加 https://'),
                ('abc.ngrok-free.app/', '不要包含路徑'),
                ('abc.ngrok-free.app:443', '不要加埠號'),
                ('headscale', '不是完整的網域名稱'),
                ('bad_host.example.com', '不是有效的網域名稱'),
                ('.'.join(['a' * 60] * 5) + '.com', '最長 253'),
        ):
            msg = tpl._validate_spec_formats({_KEY: raw})
            self.assertIn(needle, msg or '', (raw, msg))

    def _update(self, values):
        client = MagicMock()
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env)):
            result = PaasController()._update_service(self.svc, values=values, version=None)
        return result, client

    def test_reconfigure_rejects_before_any_write(self):
        before = self.svc._helm_values_dict()
        result, client = self._update({_KEY: 'https://abc.ngrok-free.app/'})
        self.assertFalse(result.get('success'), result)
        self.assertIn('https://', result.get('error', ''))
        client.upgrade_release.assert_not_called()
        self.svc.invalidate_recordset()
        self.assertEqual(self.svc._helm_values_dict(), before)
        self.assertEqual(self.svc.state, 'running')


@tagged('post_install', '-at_install')
class TestDomainFormatOnDeploy(_DomainTemplateBase):

    def _ngrok(self):
        return self.svc._assemble_deploy_values()['values'].get('ngrok')

    def test_stored_value_is_normalised_for_the_chart(self):
        stored = self.svc._helm_values_dict()
        self.assertEqual(self._ngrok(), {'enabled': True, 'domain': 'abc-123.ngrok-free.app'})
        self.assertEqual(self.svc._helm_values_dict(), stored,
                         'helm_values 保留租戶原文（設定頁顯示與再存檔不變形）')

    def test_blank_is_dropped_so_the_chart_default_applies(self):
        self.svc._apply_helm_values({'ngrok': {'enabled': True, 'domain': '  '}})
        self.assertEqual(self._ngrok(), {'enabled': True})

    def test_invalid_stored_value_is_dropped_with_a_warning(self):
        """繞過存檔驗證（例如管理員直接改 helm_values）也不能把一行字串送進 chart。"""
        self.svc._apply_helm_values({'ngrok': {
            'enabled': True, 'domain': 'evil.example.com --log=/etc/passwd'}})
        with self.assertLogs(_TEMPLATE_LOGGER, level=logging.WARNING) as logs:
            ngrok = self._ngrok()
        self.assertEqual(ngrok, {'enabled': True})
        joined = '\n'.join(logs.output)
        self.assertIn(_KEY, joined)
        self.assertNotIn('passwd', joined, '存值不進 log')
