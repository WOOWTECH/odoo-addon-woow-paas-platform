"""租戶自設 admin 帳號／密碼（open-design：OpenDesign 與 ttyd 共用一組）。

能力事實源是 ``template.admin_username_helm_path``：有值＝帳密是 chart Secret 管的
一組 basic-auth，租戶可自設；空值＝只能 Reset（mcp-server 家族、hermes-agent）。

本檔守五件事：

1. **能力 gate 單一來源**：model、controller、payload 都讀同一個欄位，沒有 slug 名單。
2. **提交點語意不變**：operator 回 2xx 之前失敗 ⇒ DB 的帳號與密碼都還是舊值（叢集
   也是）；2xx 之後帳號與密碼一起落庫——少落一個，下一次 redeploy 會用舊值覆寫叢集。
3. **輸入把關**：帳號會原樣進 htpasswd 與 ttyd ``-c``，含 ':'／空白會把帳密切成
   另一組；壞輸入必須在呼叫 operator **之前**被擋下。
4. **明文外洩面**：租戶自己填的密碼不回傳；平台鑄造的（選「隨機產生」）照舊一次性交付。
5. **換發的 gateway key 一定交付**（LiteLLM 的 master key）：有 gateway 的 chart 每次
   重設都換一把、舊的立即失效，租戶自填密碼時回應仍要帶新 key，前端也要為它開視窗
   （``TestTenantSetDeliversGatewayKey``）。
"""
import json
import os
import re
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import ROLE_USER
from ..models.cloud_service import _internal_provision

_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
USERNAME_PATH = 'authProxy.basicAuth.username'
GOOD_PASSWORD = 'Str0ng-Tenant-Pass!'


class _CredentialsFixture(TransactionCase):
    """一台 running 的 helm_secret 服務（帳號可改）＋ 一台 config_json 服務（只能 Reset）。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.admin_user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Admin credentials workspace',
            'owner_id': self.admin_user.id,
        })
        self.editable_template = self._make_template(
            'creds-editable', 'oci://example/open-design', 'helm_secret',
            username_path=USERNAME_PATH,
            defaults={'authProxy': {'enabled': True, 'basicAuth': {
                'enabled': True, 'username': 'admin'}}},
        )
        self.reset_only_template = self._make_template(
            'creds-reset-only', 'oci://example/mcp-server', 'config_json',
        )
        self.service = self._make_service(self.editable_template, 'svc-creds-edit')
        self.reset_only_service = self._make_service(
            self.reset_only_template, 'svc-creds-reset')

    def _make_template(self, slug, chart, carrier, username_path=False, defaults=None):
        return self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Creds %s' % slug,
            'slug': slug,
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': chart,
            'helm_chart_version': '0.2.0',
            'mcp_enabled': False,
            'has_admin_gui': True,
            'admin_password_carrier': carrier,
            'admin_username_helm_path': username_path,
            'helm_default_values': json.dumps(defaults or {}),
        })

    def _make_service(self, template, release):
        values = json.loads(template.helm_default_values or '{}')
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service'].with_context(
                mcp_admin_password='old-password-0000',
            )).create({
                'name': 'Svc %s' % release,
                'workspace_id': self.workspace.id,
                'template_id': template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-test',
                'helm_release_name': release,
                'helm_values': json.dumps(values),
            })

    def _secret_password(self, service):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', service.id)], limit=1).admin_password

    def _stored_username(self, service):
        values = service._helm_values_dict()
        return values.get('authProxy', {}).get('basicAuth', {}).get('username')

    def _patch_request_as(self, user):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=self.env(user=user)),
        )


class TestAdminCredentialsRestartScope1675(_CredentialsFixture):
    """#1675：文案與兩份 payload 同讀 template 旗標，不更動重啟行為。"""

    def _set_credentials(self, scope=None, password=GOOD_PASSWORD,
                         gateway=False, service=None):
        service = service if service is not None else self.service
        if scope is not None:
            service.template_id.credential_change_restart_scope = scope
        gateway_data = {'gateway_api_key': 'new-gateway-key-1675'} if gateway else {}
        with patch(_OPERATOR_CLIENT) as get_client, self._patch_request_as(
                self.admin_user), patch.object(
                self.controller, '_gateway_credentials', return_value=gateway_data):
            get_client.return_value = MagicMock()
            result = self.controller.api_service_set_admin_credentials(
                self.workspace.id, service.id, password=password,
                rotate_gateway_key=gateway)
        self.assertTrue(result.get('success'), result)
        get_client.return_value.reset_release_config.assert_called_once()
        data = result['data']
        expected_scope = scope or 'service'
        self.assertEqual(
            data['template']['credential_change_restart_scope'], expected_scope)
        self.assertEqual(
            self.controller._format_service(service)['template'][
                'credential_change_restart_scope'], expected_scope)
        if password:
            self.assertNotIn('admin_password', data)
            self.assertNotIn(password, json.dumps(result))
        return data

    def _assert_no_service_restart_claim(self, data):
        self.assertNotIn('restarts the service', data['message'])
        self.assertNotIn('The service restarts', data['message'])

    def test_console_only_custom_password(self):
        data = self._set_credentials('console_only')
        self._assert_no_service_restart_claim(data)
        self.assertIn('the main service keeps running', data['message'])
        self.assertIn('only the console restarts', data['message'])
        self.assertIn('within about a minute', data['message'])

    def test_console_only_generated_password(self):
        data = self._set_credentials('console_only', password='')
        self._assert_no_service_restart_claim(data)
        self.assertEqual(data['admin_password'], self._secret_password(self.service))
        self.assertIn('Save the new password now', data['message'])
        self.assertIn('only the console restarts', data['message'])

    def test_default_password_only_template_still_restarts_service(self):
        """未設旗標的 helm_secret、只有密碼的模板沿用既有提示（code-server 類）。"""
        template = self._make_template(
            'creds-password-only-1675', 'oci://example/password-only', 'helm_secret')
        template.admin_password_settable = True
        service = self._make_service(template, 'svc-creds-password-1675')
        self.assertEqual(template.credential_change_restart_scope, 'service')
        data = self._set_credentials(service=service)
        self.assertEqual(
            data['message'],
            'Admin credentials updated. The service restarts to apply them '
            '(about a minute).')

    def test_none_does_not_claim_a_restart(self):
        data = self._set_credentials('none')
        self._assert_no_service_restart_claim(data)
        self.assertIn('within about a minute; the service does not restart.', data['message'])

    def test_console_only_gateway_replacement_copy(self):
        """gateway 交付以 mock 隔離；只驗本單的訊息組裝，實際換發由既有測試守住。"""
        data = self._set_credentials('console_only', gateway=True)
        self._assert_no_service_restart_claim(data)
        self.assertEqual(data['gateway_api_key'], 'new-gateway-key-1675')
        self.assertIn('Save the new Gateway API Key now', data['message'])
        self.assertIn('only the console restarts', data['message'])

    def test_none_gateway_replacement_copy(self):
        data = self._set_credentials('none', gateway=True)
        self._assert_no_service_restart_claim(data)
        self.assertEqual(data['gateway_api_key'], 'new-gateway-key-1675')
        self.assertIn('Save the new Gateway API Key now', data['message'])
        self.assertIn('the service does not restart.', data['message'])

    def test_seed_declares_console_only(self):
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        self.assertEqual(template.credential_change_restart_scope, 'console_only')
        self.assertEqual(template.admin_password_carrier, 'helm_secret')


class TestAdminUsernameHelmPathConstraint(_CredentialsFixture):
    """能力欄位只能設在「憑證真的活在 chart Secret」的模板上。"""

    def test_config_json_carrier_rejected(self):
        """config_json 的帳號活在 PVC 的 config.json：改 values 會假裝成功。"""
        with self.assertRaises(ValidationError):
            self.reset_only_template.write({'admin_username_helm_path': USERNAME_PATH})

    def test_requires_admin_gui(self):
        with self.assertRaises(ValidationError):
            self.editable_template.write({'has_admin_gui': False})

    def test_path_format(self):
        for bad in ('authProxy..username', '.username', 'auth proxy.username', 'a.b.'):
            with self.assertRaises(ValidationError, msg=bad):
                self.editable_template.write({'admin_username_helm_path': bad})

    def test_open_design_seed_declares_the_path(self):
        """seed：open-design 的帳號在 chart 的 authProxy.basicAuth.username。"""
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        self.assertEqual(template.admin_username_helm_path, USERNAME_PATH)
        self.assertEqual(template.admin_password_carrier, 'helm_secret')

    def test_only_helm_secret_templates_declare_a_path(self):
        """結構不變式（不硬編清單）：有路徑 ⟹ has_admin_gui ＋ helm_secret。"""
        templates = self.env['woow_paas_platform.cloud_app_template'].with_context(
            active_test=False).search([('admin_username_helm_path', '!=', False)])
        self.assertTrue(templates, '探針讀不到任何可改帳號的模板（seed 未載入？）')
        for template in templates:
            self.assertTrue(template.has_admin_gui, template.slug)
            self.assertEqual(template.admin_password_carrier, 'helm_secret', template.slug)


class TestTenantSetCredentialsModel(_CredentialsFixture):
    """model：輸入把關、送給 operator 的內容、提交點前後的落庫。"""

    @patch(_OPERATOR_CLIENT)
    def test_username_and_password_reach_operator_and_db(self, get_client):
        client = MagicMock()
        get_client.return_value = client

        applied = self.service._reset_admin_password(
            new_password=GOOD_PASSWORD, new_username='design.team')

        self.assertEqual(applied, GOOD_PASSWORD, '租戶的密碼原樣生效，不另鑄')
        kwargs = client.reset_release_config.call_args.kwargs
        self.assertEqual(
            kwargs['values']['authProxy']['basicAuth']['username'], 'design.team',
            '新帳號必須在送給 operator 的 values 裡——否則 htpasswd 與 ttyd 還是舊帳號')
        self.assertEqual(
            kwargs['sensitive_values']['config']['sensitive']['admin_password'],
            GOOD_PASSWORD)
        self.assertEqual(kwargs['admin_password_carrier'], 'helm_secret')
        self.assertEqual(self._secret_password(self.service), GOOD_PASSWORD)
        self.assertEqual(
            self._stored_username(self.service), 'design.team',
            '帳號沒落庫 ⇒ 下一次 redeploy 會用舊帳號覆寫叢集')
        self.assertEqual(self.service._admin_username(), 'design.team')
        self.assertEqual(self.service.state, 'upgrading')

    @patch(_OPERATOR_CLIENT)
    def test_generated_password_with_new_username(self, get_client):
        get_client.return_value = MagicMock()

        applied = self.service._reset_admin_password(new_username='ops')

        self.assertTrue(applied and applied != 'old-password-0000')
        self.assertEqual(self._secret_password(self.service), applied)
        self.assertEqual(self._stored_username(self.service), 'ops')

    @patch(_OPERATOR_CLIENT)
    def test_password_only_keeps_username(self, get_client):
        get_client.return_value = MagicMock()

        self.service._reset_admin_password(new_password=GOOD_PASSWORD)

        self.assertEqual(self._stored_username(self.service), 'admin')
        values = get_client.return_value.reset_release_config.call_args.kwargs['values']
        self.assertEqual(values['authProxy']['basicAuth']['username'], 'admin')

    @patch(_OPERATOR_CLIENT)
    def test_bad_input_is_rejected_before_the_operator(self, get_client):
        client = MagicMock()
        get_client.return_value = client
        cases = [
            {'new_username': 'a:b'}, {'new_username': 'a b'}, {'new_username': ''},
            {'new_username': 'x' * 65}, {'new_username': 'ünï'},
            {'new_password': 'short'}, {'new_password': 'has space inside!!'},
            {'new_password': 'line\nbreak-password'}, {'new_password': 'x' * 129},
        ]
        for kwargs in cases:
            with self.assertRaises(ValidationError, msg=repr(kwargs)):
                self.service._reset_admin_password(**kwargs)
        client.reset_release_config.assert_not_called()
        self.assertEqual(self._secret_password(self.service), 'old-password-0000')
        self.assertEqual(self._stored_username(self.service), 'admin')

    @patch(_OPERATOR_CLIENT)
    def test_reset_only_template_refuses_tenant_values(self, get_client):
        client = MagicMock()
        get_client.return_value = client
        for kwargs in ({'new_password': GOOD_PASSWORD}, {'new_username': 'someone'}):
            with self.assertRaises(UserError, msg=repr(kwargs)):
                self.reset_only_service._reset_admin_password(**kwargs)
        client.reset_release_config.assert_not_called()

    @patch(_OPERATOR_CLIENT)
    def test_operator_failure_changes_nothing(self, get_client):
        """2xx 之前失敗：叢集仍是舊帳密，DB 也必須是。"""
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError
        client = MagicMock()
        client.reset_release_config.side_effect = PaaSOperatorError('boom')
        get_client.return_value = client

        try:
            self.service._reset_admin_password(
                new_password=GOOD_PASSWORD, new_username='design.team')
        except UserError:
            pass
        else:
            self.fail('operator 失敗必須讓 reset 失敗')

        self.assertEqual(self._secret_password(self.service), 'old-password-0000')
        self.assertEqual(self._stored_username(self.service), 'admin')

    @patch(_OPERATOR_CLIENT)
    def test_plain_reset_is_unchanged(self, get_client):
        """不帶參數＝原本的 Reset：鑄新密碼、帳號不動（兩種模板都一樣）。"""
        get_client.return_value = MagicMock()
        for service in (self.service, self.reset_only_service):
            applied = service._reset_admin_password()
            self.assertEqual(self._secret_password(service), applied)
        self.assertEqual(self._stored_username(self.service), 'admin')


class TestTenantSetCredentialsEndpoint(_CredentialsFixture):
    """controller：授權、能力 gate、回應的明文外洩面。"""

    def _call(self, user, service, **params):
        with self._patch_request_as(user):
            return self.controller.api_service_set_admin_credentials(
                self.workspace.id, service.id, **params)

    @patch(_OPERATOR_CLIENT)
    def test_tenant_password_is_not_echoed_back(self, get_client):
        get_client.return_value = MagicMock()

        result = self._call(self.admin_user, self.service,
                            username='design.team', password=GOOD_PASSWORD)

        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['admin_username'], 'design.team')
        self.assertNotIn('admin_password', result['data'])
        self.assertNotIn(GOOD_PASSWORD, json.dumps(result))

    @patch(_OPERATOR_CLIENT)
    def test_generated_password_is_delivered_once(self, get_client):
        get_client.return_value = MagicMock()

        result = self._call(self.admin_user, self.service, username='', password='')

        self.assertTrue(result.get('success'), result)
        self.assertEqual(
            result['data']['admin_password'], self._secret_password(self.service))
        self.assertEqual(result['data']['admin_username'], 'admin')

    @patch(_OPERATOR_CLIENT)
    def test_reset_only_template_gets_an_error(self, get_client):
        client = MagicMock()
        get_client.return_value = client

        result = self._call(self.admin_user, self.reset_only_service,
                            password=GOOD_PASSWORD)

        self.assertFalse(result.get('success'))
        client.reset_release_config.assert_not_called()

    @patch(_OPERATOR_CLIENT)
    def test_validation_message_reaches_the_tenant(self, get_client):
        get_client.return_value = MagicMock()

        result = self._call(self.admin_user, self.service, username='bad:name')

        self.assertFalse(result.get('success'))
        self.assertIn('Username', result.get('error', ''))

    @patch(_OPERATOR_CLIENT)
    def test_non_admin_member_is_denied(self, get_client):
        """與 Reset 同一道 ROLE_ADMIN gate：非 admin 成員不能換掉真 admin 的帳密。"""
        get_client.return_value = MagicMock()
        member = self.env['res.users'].create({
            'name': 'creds-member', 'login': 'creds-member',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': member.id, 'role': ROLE_USER,
        })

        result = self._call(member, self.service,
                            username='hijack', password=GOOD_PASSWORD)

        self.assertFalse(result.get('success'))
        self.assertEqual(self._secret_password(self.service), 'old-password-0000')
        self.assertEqual(self._stored_username(self.service), 'admin')

    @patch(_OPERATOR_CLIENT)
    def test_reset_endpoint_reports_username_for_editable_templates(self, get_client):
        get_client.return_value = MagicMock()
        with self._patch_request_as(self.admin_user):
            result = self.controller.api_service_reset_admin_password(
                self.workspace.id, self.service.id)
        self.assertTrue(result.get('success'), result)
        self.assertEqual(result['data']['admin_username'], 'admin')
        self.assertTrue(result['data']['admin_password'])


class TestAdminCredentialsPayload(_CredentialsFixture):
    """payload 鏡射能力欄位（capability-gate-single-source-field）。"""

    def test_payload_mirrors_the_template_field(self):
        editable = self.controller._format_service(self.service)
        reset_only = self.controller._format_service(self.reset_only_service)

        self.assertIs(editable['template']['admin_username_editable'], True)
        self.assertEqual(editable['admin_username'], 'admin')
        self.assertIs(reset_only['template']['admin_username_editable'], False)
        self.assertIsNone(reset_only['admin_username'])

    def test_broken_helm_values_do_not_break_the_payload(self):
        """列表序列化會呼叫 _admin_username：一台服務的壞 JSON 不得拖垮整個列表。"""
        self.service.sudo().write({'helm_values': '{not json'})
        payload = self.controller._format_service(self.service)
        self.assertEqual(payload['admin_username'], 'admin', '退回 template 預設')


#: 租戶自填密碼（不回傳）、但 gateway key 被換發時的回應訊息。程式字串，走 .po。
TENANT_SET_KEY_REPLACED = (
    'Admin credentials updated and the Gateway API Key was replaced. '
    'Save the new Gateway API Key now — it will not be shown again. '
    'The service restarts to apply them.'
)
#: 其他情境原本的訊息（本修正不動）。
BOTH_RESET = (
    'Admin password and Gateway API Key reset. Save both now — '
    'they will not be shown again. Update the Gateway API Key '
    'wherever you configured it (e.g. your MCP server).'
)
TENANT_SET_NO_GATEWAY = (
    'Admin credentials updated. The service restarts to apply them '
    '(about a minute).'
)


class TestTenantSetDeliversGatewayKey(_CredentialsFixture):
    """有 gateway 的服務（LiteLLM 的 master key）租戶自設密碼：新 key 必須交付。

    #1557 之後換 key 是租戶勾選的選項（預設保留）；本 class 守「勾了換」的那一半，
    「沒勾就保留、不交付」在 ``test_litellm_master_key_1557``。

    model 端 ``_reset_admin_password`` 對有 gateway 的 chart **每次**都換發 gateway
    key、舊的立即失效；controller 只是不回傳租戶自填的密碼。修前前端只在回應帶
    ``admin_password`` 時開一次性視窗 ⇒ 新 master key 從未交到租戶手上，租戶所有用
    master key 的自動化當場斷掉，只能再按一次「隨機產生」才拿得到。

    用真的 LiteLLM seed（不是自建模板）：守的是「上架中的那張卡」的實際行為。
    """

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'gw-tenant-set.invalid')
        self.litellm = self.env.ref('woow_paas_platform.cloud_app_litellm')
        self.gw_service = _internal_provision(
            self.env['woow_paas_platform.cloud_service']).create({
                'name': 'Svc litellm tenant-set',
                'workspace_id': self.workspace.id,
                'template_id': self.litellm.id,
                'state': 'running',
                'subdomain': 'svc-gw-tenant-set',
                'helm_namespace': 'paas-ws-test',
                'helm_release_name': 'svc-gw-tenant-set',
                'helm_values': self.litellm.helm_default_values,
            })
        # 有 gateway 的 chart 在這裡補鑄 key（_set_admin_password_secret 的不變式）。
        self.gw_service._set_admin_password_secret('old-password-0000')
        self.old_key = self._secret(self.gw_service).gateway_api_key

    def _secret(self, service):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', service.id)], limit=1)

    def _set(self, service, **params):
        with self._patch_request_as(self.admin_user):
            return self.controller.api_service_set_admin_credentials(
                self.workspace.id, service.id, **params)

    @patch(_OPERATOR_CLIENT)
    def test_tenant_password_delivers_only_the_new_gateway_key(self, get_client):
        client = MagicMock()
        get_client.return_value = client
        self.assertTrue(self.litellm.admin_credentials_settable, '前提：LiteLLM 可自設帳密')
        self.assertTrue(self.old_key, '前提：有 gateway 的服務已經有一把 key')

        # #1557：換 key 改成租戶明確勾選（預設保留，見 test_litellm_master_key_1557）。
        result = self._set(self.gw_service, username='llm.ops', password=GOOD_PASSWORD,
                           rotate_gateway_key=True)

        self.assertTrue(result.get('success'), result)
        data = result['data']
        new_key = self._secret(self.gw_service).gateway_api_key
        self.assertNotEqual(new_key, self.old_key, '前提：model 端換發了 key（舊的已失效）')
        self.assertEqual(
            data.get('gateway_api_key'), new_key,
            '新 key 只在這次回應出現；少了它租戶的自動化全斷、且拿不回來')
        self.assertEqual(
            client.reset_release_config.call_args.kwargs[
                'sensitive_values']['auth']['apiServerKey'],
            new_key, '交付給租戶的就是叢集生效的那一把')
        self.assertEqual(data.get('gateway_url'), 'https://svc-gw-tenant-set.gw-tenant-set.invalid/v1')
        self.assertEqual(data.get('gateway_api_key_hint'), self.litellm.gateway_api_key_hint)
        self.assertEqual(data.get('admin_username'), 'llm.ops')
        self.assertNotIn('admin_password', data, '租戶自填的密碼不回傳')
        self.assertNotIn(GOOD_PASSWORD, json.dumps(result))
        self.assertEqual(
            data.get('message'), TENANT_SET_KEY_REPLACED,
            '不能說「兩個都存好」：密碼沒有回傳，要交代的是新 key')

    @patch(_OPERATOR_CLIENT)
    def test_generated_password_still_delivers_both(self, get_client):
        """自設流程選「隨機產生」＋勾選換 key：密碼與 key 都交付，訊息維持原樣。"""
        get_client.return_value = MagicMock()

        result = self._set(self.gw_service, password='', rotate_gateway_key=True)

        self.assertTrue(result.get('success'), result)
        data = result['data']
        self.assertEqual(data.get('admin_password'), self._secret(self.gw_service).admin_password)
        self.assertEqual(data.get('gateway_api_key'), self._secret(self.gw_service).gateway_api_key)
        self.assertEqual(data.get('message'), BOTH_RESET)

    @patch(_OPERATOR_CLIENT)
    def test_reset_endpoint_is_unchanged(self, get_client):
        get_client.return_value = MagicMock()
        with self._patch_request_as(self.admin_user):
            result = self.controller.api_service_reset_admin_password(
                self.workspace.id, self.gw_service.id)

        self.assertTrue(result.get('success'), result)
        self.assertTrue(result['data'].get('admin_password'))
        self.assertTrue(result['data'].get('gateway_api_key'))
        self.assertEqual(result['data'].get('message'), BOTH_RESET)

    @patch(_OPERATOR_CLIENT)
    def test_tenant_password_without_gateway_is_unchanged(self, get_client):
        """沒有 gateway 的服務（open-design 型）：不交付 key、訊息維持原樣。"""
        get_client.return_value = MagicMock()

        result = self._set(self.service, password=GOOD_PASSWORD)

        self.assertTrue(result.get('success'), result)
        self.assertNotIn('gateway_api_key', result['data'])
        self.assertNotIn('admin_password', result['data'])
        self.assertEqual(result['data'].get('message'), TENANT_SET_NO_GATEWAY)

    def test_new_strings_are_translated(self):
        """新訊息是程式字串（走 .po），一次性視窗新增的帳號提示也要有中文。"""
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        for src in (TENANT_SET_KEY_REPLACED,
                    'Signs in to this service, together with the password you set.'):
            self.assertTrue(code.get(src), 'zh_TW.po 缺中文：%r' % src)

    def test_frontend_opens_the_dialog_for_a_gateway_key(self):
        """前端契約（靜態讀檔）：回應只帶 gateway key、沒有密碼時也要開一次性視窗。"""
        src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        page = os.path.join(src, 'static', 'src', 'paas', 'pages', 'service',
                            'ServiceDetailPage.js')
        with open(page, encoding='utf-8') as f:
            text = f.read()
        match = re.search(r'async onAdminCredentialsSaved\(data\)\s*\{(.*?)\n    \}', text, re.S)
        self.assertTrue(match, '找不到 onAdminCredentialsSaved（改名了？同步更新本測試）')
        self.assertIn(
            'data.gateway_api_key', match.group(1),
            '只看 admin_password 會把換發的 gateway key 丟掉（舊的已失效）')
