# -*- coding: utf-8 -*-
"""P3：一次性憑證視窗的 gateway 兩格提示依範本切換。

前端 `buildOneTimeCredentials` 內建的提示是 hermes-agent 專用（「填進 MCP 連線設定的
Gateway API Server 區」）。LiteLLM 之後也會走 #530 的 gateway 通道（master key ＋
OpenAI 相容 Base URL），沿用那段文案等於叫租戶去一個不存在的 MCP 設定頁。

- 範本欄位 `gateway_api_key_hint`／`gateway_url_hint`（可翻譯）；
- `PaasController._gateway_credentials`（建立與 Reset 兩個呼叫點共用）在**交付 key 時**
  一併帶出同名鍵；空值不帶，前端沿用內建文案；
- 只是顯示字：交付判準不變（seed 的 gateway.externalPath ＋ 服務有對外根位址）。
"""
import json

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

KEY_HINT = 'Master key: the gateway admin key. Give apps a virtual key instead.'
URL_HINT = 'OpenAI-compatible base URL (base_url / OPENAI_BASE_URL).'


@tagged('post_install', '-at_install')
class TestGatewayHintTemplate(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'p3-hint.invalid')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'gateway hint workspace',
        })
        self._seq = 0

    def _template(self, slug, gateway_path='/v1', **hints):
        values = {'image': {'repository': 'example/app', 'tag': 'v1'}}
        if gateway_path is not None:
            values['gateway'] = {'externalPath': gateway_path}
        return self.env['woow_paas_platform.cloud_app_template'].create(dict({
            'name': 'Gateway hint %s' % slug,
            'slug': slug,
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.1.0',
            'mcp_enabled': False,
            'has_admin_gui': True,
            'helm_default_values': json.dumps(values),
        }, **hints))

    def _creds(self, template, subdomain=True):
        self._seq += 1
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-hint-%d' % self._seq,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'subdomain': 'svc-hint-p3-%d' % self._seq if subdomain else False,
            'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-hint-%d' % self._seq,
            'helm_values': template.helm_default_values,
        })
        service._set_admin_password_secret('pw-p3-hint')
        return PaasController._gateway_credentials(service)

    def test_template_hints_ride_along_with_the_key(self):
        creds = self._creds(self._template(
            'litellm-like', gateway_api_key_hint=KEY_HINT, gateway_url_hint=URL_HINT))
        self.assertTrue(creds.get('gateway_api_key'), '前提：key 要真的交付')
        self.assertEqual(
            creds.get('gateway_url'), 'https://svc-hint-p3-%d.p3-hint.invalid/v1' % self._seq)
        self.assertEqual(creds.get('gateway_api_key_hint'), KEY_HINT)
        self.assertEqual(creds.get('gateway_url_hint'), URL_HINT)

    def test_each_hint_is_independent(self):
        creds = self._creds(self._template('key-only', gateway_api_key_hint=KEY_HINT))
        self.assertEqual(creds.get('gateway_api_key_hint'), KEY_HINT)
        self.assertNotIn('gateway_url_hint', creds, '沒設的那一格沿用前端內建文案')

    def test_empty_hints_keep_the_built_in_text(self):
        """空值不帶鍵 ⇒ 前端走 `data.gateway_api_key_hint || _t(hermes 文案)`。"""
        creds = self._creds(self._template('hermes-like'))
        self.assertTrue(creds.get('gateway_api_key'), '陽性對照：key 照常交付')
        self.assertNotIn('gateway_api_key_hint', creds)
        self.assertNotIn('gateway_url_hint', creds)

    def test_no_hint_without_a_delivered_key(self):
        """沒有 gateway（或算不出對外網址）就沒有那兩格——提示不得單獨出現。"""
        no_gateway = self._template(
            'no-gw', gateway_path=None, gateway_api_key_hint=KEY_HINT,
            gateway_url_hint=URL_HINT)
        no_subdomain = self._template(
            'no-sub', gateway_api_key_hint=KEY_HINT, gateway_url_hint=URL_HINT)
        for label, creds in (('沒有 gateway', self._creds(no_gateway)),
                             ('沒有 subdomain', self._creds(no_subdomain, subdomain=False))):
            with self.subTest(case=label):
                self.assertNotIn('gateway_api_key', creds)
                self.assertNotIn('gateway_api_key_hint', creds)
                self.assertNotIn('gateway_url_hint', creds)

    def test_hermes_seed_keeps_the_built_in_text(self):
        """hermes-agent 不設提示：它的 MCP 說明就是前端內建的那一段。"""
        hermes = self.env.ref('woow_paas_platform.cloud_app_hermes_agent')
        self.assertTrue(hermes._gateway_external_path(), '前提：hermes-agent 有 gateway')
        self.assertFalse(hermes.gateway_api_key_hint)
        self.assertFalse(hermes.gateway_url_hint)

    def test_hint_fields_are_translatable(self):
        fields = self.env['woow_paas_platform.cloud_app_template']._fields
        for name in ('gateway_api_key_hint', 'gateway_url_hint', 'public_tunnel_label'):
            self.assertTrue(fields[name].translate, name)
