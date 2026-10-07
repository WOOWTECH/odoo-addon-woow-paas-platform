"""#1557：LiteLLM 改「管理員帳密」預設保留 Gateway API Key（master key）。

2026-10-07 第二階段實測（N13）：在服務頁改管理員帳密（產生或自設）時，平台同時換發
master key——Secret 的 master_key 改變、原 key 立即 401；操作前的對話框只寫「儲存後
服務會重新啟動」，事後才在一次性視窗看到新 key。只想改後台登入密碼的客戶，會讓所有
用 master key 的管理腳本與整合同時失效。

拆成兩個選擇（同一個對話框）：

* 預設（``rotate_gateway_key`` 未給／False）：只改帳密，**保留**目前的 key——明確送回
  chart（``auth.apiServerKey``＝舊值），DB 不變，回應不交付 key（交付沒換過的 key 等於
  開一條回讀端點，#530 憑證一律一次性）；
* 租戶勾選「一併換掉」（``rotate_gateway_key=True``）：照 #1478 的行為換發並一次性交付
  （``test_admin_credentials_tenant_set.TestTenantSetDeliversGatewayKey``）。

legacy 服務還沒有 key 時照樣鑄一把並交付，否則叢集與 DB 會各自一把。Reset 端點
（hermes-agent）不受影響，仍一次換兩把。
"""
import json
import os
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision

_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAAS = os.path.join(_SRC, 'static', 'src', 'paas')
GOOD_PASSWORD = 'Str0ng-Tenant-Pass!'
TENANT_SET_NO_KEY = (
    'Admin credentials updated. The service restarts to apply them (about a minute).')
GENERATED_NO_KEY = (
    'Admin credentials updated. Save the new password now — it will not be shown again. '
    'The service restarts to apply them.')


def _read(*parts):
    with open(os.path.join(_PAAS, *parts), encoding='utf-8') as handle:
        return handle.read()


@tagged('post_install', '-at_install')
class TestLitellmKeepsMasterKey1557(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.admin_user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'LiteLLM 1557 workspace', 'owner_id': self.admin_user.id,
        })
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'gw-1557.invalid')
        self.litellm = self.env.ref('woow_paas_platform.cloud_app_litellm')
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create({
                'name': 'Svc litellm 1557',
                'workspace_id': self.workspace.id,
                'template_id': self.litellm.id,
                'state': 'running',
                'subdomain': 'svc-gw-1557',
                'helm_namespace': 'paas-ws-test',
                'helm_release_name': 'svc-gw-1557',
                'helm_values': self.litellm.helm_default_values,
            })
        self.service._set_admin_password_secret('old-password-1557')
        self.old_key = self._secret().gateway_api_key

    def _secret(self):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', self.service.id)], limit=1)

    def _set(self, **params):
        with patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                   new=MagicMock(env=self.env(user=self.admin_user))):
            return self.controller.api_service_set_admin_credentials(
                self.workspace.id, self.service.id, **params)

    @patch(_OPERATOR_CLIENT)
    def test_tenant_password_keeps_the_key_by_default(self, get_client):
        client = MagicMock()
        get_client.return_value = client
        self.assertTrue(self.old_key, '前提：服務已經有一把 key')

        result = self._set(username='llm.ops', password=GOOD_PASSWORD)

        self.assertTrue(result.get('success'), result)
        self.assertEqual(self._secret().gateway_api_key, self.old_key, 'DB 的 key 不得被換掉')
        self.assertEqual(self._secret().admin_password, GOOD_PASSWORD)
        sent = client.reset_release_config.call_args.kwargs['sensitive_values']
        self.assertEqual(sent['auth']['apiServerKey'], self.old_key,
                         '保留也要明確送回同一把，不靠 chart 回讀既有 Secret')
        data = result['data']
        self.assertNotIn('gateway_api_key', data, '沒換的 key 不得被回讀交付')
        self.assertNotIn('admin_password', data)
        self.assertNotIn(self.old_key, json.dumps(result))
        self.assertEqual(data.get('message'), TENANT_SET_NO_KEY)

    @patch(_OPERATOR_CLIENT)
    def test_generated_password_keeps_the_key_by_default(self, get_client):
        get_client.return_value = MagicMock()

        result = self._set(password='')

        self.assertTrue(result.get('success'), result)
        data = result['data']
        self.assertEqual(data.get('admin_password'), self._secret().admin_password)
        self.assertNotIn('gateway_api_key', data)
        self.assertEqual(self._secret().gateway_api_key, self.old_key)
        self.assertEqual(data.get('message'), GENERATED_NO_KEY)

    @patch(_OPERATOR_CLIENT)
    def test_non_boolean_flag_does_not_rotate(self, get_client):
        """只認 JSON 的 true；其他值（字串等）一律當作不換（保留是安全的那一邊）。"""
        get_client.return_value = MagicMock()
        result = self._set(password=GOOD_PASSWORD, rotate_gateway_key='true')
        self.assertTrue(result.get('success'), result)
        self.assertEqual(self._secret().gateway_api_key, self.old_key)

    @patch(_OPERATOR_CLIENT)
    def test_opting_in_rotates_and_delivers(self, get_client):
        client = MagicMock()
        get_client.return_value = client

        result = self._set(password=GOOD_PASSWORD, rotate_gateway_key=True)

        self.assertTrue(result.get('success'), result)
        new_key = self._secret().gateway_api_key
        self.assertNotEqual(new_key, self.old_key)
        self.assertEqual(result['data'].get('gateway_api_key'), new_key)
        self.assertEqual(
            client.reset_release_config.call_args.kwargs['sensitive_values']['auth'][
                'apiServerKey'], new_key)

    @patch(_OPERATOR_CLIENT)
    def test_service_without_a_key_still_gets_one(self, get_client):
        """legacy：還沒有 key 時照樣鑄一把並交付（DB 與叢集必須是同一把）。"""
        client = MagicMock()
        get_client.return_value = client
        self._secret().write({'gateway_api_key': False})

        result = self._set(password=GOOD_PASSWORD)

        self.assertTrue(result.get('success'), result)
        new_key = self._secret().gateway_api_key
        self.assertTrue(new_key)
        self.assertEqual(result['data'].get('gateway_api_key'), new_key)
        self.assertEqual(
            client.reset_release_config.call_args.kwargs['sensitive_values']['auth'][
                'apiServerKey'], new_key)

    def test_model_default_still_rotates(self):
        """model 預設維持 #530 的 Reset 語意（一次換兩把），只有自設流程傳 False。"""
        with patch(_OPERATOR_CLIENT) as get_client:
            get_client.return_value = MagicMock()
            self.service._reset_admin_password()
        self.assertNotEqual(self._secret().gateway_api_key, self.old_key)


@tagged('post_install', '-at_install')
class TestLitellmKeyChoiceFrontend1557(TransactionCase):
    """對話框的前端契約（靜態讀檔）與中文。"""

    def test_dialog_offers_an_explicit_choice(self):
        js = _read('components', 'modal', 'AdminCredentialsModal.js')
        xml = _read('components', 'modal', 'AdminCredentialsModal.xml')
        self.assertIn('rotateGatewayKey: false', js, '預設必須是不換')
        self.assertIn('rotate_gateway_key: this.hasGateway && this.state.rotateGatewayKey', js)
        self.assertIn('this.props.service?.has_gateway === true', js)
        self.assertIn('t-if="hasGateway"', xml)
        self.assertIn('t-on-change="onRotateGatewayKeyChange"', xml)
        page = _read('pages', 'service', 'ServiceDetailPage.xml')
        self.assertIn('service="service or undefined"', page)
        self.assertIn(
            't-if="adminResetSupported and hasGateway and !canEditAdminCredentials"', page,
            '「Reset 也會換 key」那句只講 Reset 按鈕，不講可自設帳密的服務')

    def test_new_strings_are_translated(self):
        po = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        for src in (
            'Also replace the Gateway API Key',
            'Not selected: the current Gateway API Key keeps working, so scripts and '
            'integrations that use it are not affected.',
            'The current Gateway API Key stops working as soon as you save; every script '
            'or integration that uses it is rejected until you update it. The new key is '
            'shown once after saving.',
        ):
            with self.subTest(src=src):
                self.assertTrue(code.get(src), 'zh_TW.po 缺中文：%r' % src)
