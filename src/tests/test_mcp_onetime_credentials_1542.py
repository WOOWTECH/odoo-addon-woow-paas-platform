# -*- coding: utf-8 -*-
"""#1542：mcp-server 家族的一次性帳密視窗不能顯示 hermes-agent 的說明。

正式站 .210 開通 n8n MCP／Vibe Kanban MCP，視窗只有一格「管理員密碼」，說明卻是 hermes-agent
的「console 與 dashboard 共用這一組（帳號 admin）…搭配下方的 Dashboard API URL」——MCP 管理頁
只有密碼欄、沒有帳號，也沒有 console／dashboard。原因：前端 `buildOneTimeCredentials` 的前幾個
分支（app_admin_reset_kind、admin_reset_supported=False、admin_username、admin_credentials_settable）
都不符合，掉進最後的 hermes 預設分支。

修法：payload 的 template 子物件（服務 payload 與 Reset 回應兩處）鏡射模板的
`admin_password_carrier`，前端以 ``config_json``（＝has_admin_gui 的 mcp-server 家族，見 MCP_SEEDS）走 MCP 專用文案；
一次性交付另帶 `admin_url`（管理頁＝服務對外根位址，網址由後端給、前端不拼）。
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import ROLE_ADMIN
from ..models.cloud_service import _internal_provision

MCP_SEEDS = (
    'cloud_app_mcp_hermes', 'cloud_app_mcp_odoo', 'cloud_app_mcp_odoo_manage', 'cloud_app_mcp_n8n',
    'cloud_app_mcp_k8s', 'cloud_app_mcp_vk', 'cloud_app_mcp_opendesign', 'cloud_app_mcp_litellm',
    'cloud_app_mcp_nextcloud', 'cloud_app_mcp_emqx',
)


@tagged('post_install', '-at_install')
class TestMcpOneTimeCredentials1542(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.paas_domain', 'issue-1542.invalid')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': '1542 one-time credentials',
        })
        self.admin = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': '1542 admin', 'login': 'onetime-1542-admin',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': self.admin.id, 'role': ROLE_ADMIN})
        self._seq = 0

    def _service(self, xmlid, subdomain=True):
        template = self.env.ref('woow_paas_platform.%s' % xmlid)
        self._seq += 1
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-1542-%d' % self._seq,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'subdomain': 'svc-1542-%d' % self._seq if subdomain else False,
            'helm_namespace': 'paas-ws-test-1542',
            'helm_release_name': 'svc-1542-%d' % self._seq,
            'helm_values': template.helm_default_values,
        })

    # ── 判準：模板欄位，不是 slug 名單 ──────────────────────────────────────────
    def test_config_json_carrier_is_exactly_the_mcp_family(self):
        Template = self.env['woow_paas_platform.cloud_app_template'].with_context(active_test=False)
        mcp_ids = {self.env.ref('woow_paas_platform.%s' % x).id for x in MCP_SEEDS}
        for tmpl in Template.browse(sorted(mcp_ids)):
            self.assertTrue(tmpl.has_admin_gui, tmpl.slug)
            self.assertEqual(tmpl.admin_password_carrier, 'config_json', tmpl.slug)
        others = Template.search([('has_admin_gui', '=', True), ('id', 'not in', list(mcp_ids))])
        self.assertTrue(others, '前提：有其他 has_admin_gui 的模板（hermes-agent、code-server…）')
        self.assertFalse(
            others.filtered(lambda t: t.admin_password_carrier == 'config_json').mapped('slug'),
            'config_json 載體＋has_admin_gui 必須只有 mcp-server 家族，否則 MCP 文案會套到別的模板')

    # ── 服務 payload（開通回應用的就是它）───────────────────────────────────────
    def test_service_payload_mirrors_carrier(self):
        mcp = self.controller._format_service(self._service('cloud_app_mcp_n8n'))
        self.assertEqual(mcp['template']['admin_password_carrier'], 'config_json')
        hermes = self.controller._format_service(self._service('cloud_app_hermes_agent'))
        self.assertEqual(hermes['template']['admin_password_carrier'], 'helm_secret')

    def test_admin_url_delivered_for_mcp_only(self):
        service = self._service('cloud_app_mcp_vk')
        self.assertEqual(
            PaasController._admin_page_link(service),
            {'admin_url': 'https://%s.issue-1542.invalid' % service.subdomain})
        # 管理頁網址與 gateway 憑證交付分開：MCP 沒有 gateway／dashboard，憑證交付維持 {}
        self.assertEqual(PaasController._gateway_credentials(service), {},
                         'admin_url 不得混進 gateway 憑證交付（沒有 gateway 的 chart 必須回 {}）')
        # hermes-agent（helm_secret）不帶 admin_url
        hermes = self._service('cloud_app_hermes_agent')
        self.assertEqual(PaasController._admin_page_link(hermes), {})

    def test_no_admin_url_without_public_address(self):
        self.assertEqual(
            PaasController._admin_page_link(self._service('cloud_app_mcp_n8n', subdomain=False)), {},
            '沒有對外根位址就不交付半條網址')

    # ── Reset 回應 ─────────────────────────────────────────────────────────────
    def test_reset_response_carries_carrier_and_admin_url(self):
        service = self._service('cloud_app_mcp_n8n')
        Service = type(self.env['woow_paas_platform.cloud_service'])
        with patch.object(Service, '_reset_admin_password', return_value='new-pw-1542'), \
                patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                      new=MagicMock(env=self.env(user=self.admin))):
            result = self.controller.api_service_reset_admin_password(self.workspace.id, service.id)
        self.assertTrue(result.get('success'), result)
        data = result['data']
        self.assertEqual(data['template'].get('admin_password_carrier'), 'config_json')
        self.assertEqual(data.get('admin_password'), 'new-pw-1542')
        self.assertEqual(data.get('admin_url'), 'https://%s.issue-1542.invalid' % service.subdomain)
        for key in ('gateway_api_key', 'gateway_url', 'dashboard_url'):
            self.assertNotIn(key, data, 'MCP 沒有 gateway／dashboard')

    # ── 前端分支與翻譯 ─────────────────────────────────────────────────────────
    def test_frontend_branch_and_zh_tw(self):
        """前端沒有 JS 單元測試框架：鎖住分支條件、文案 msgid 與 zh_TW 條目同步存在。"""
        import os
        root = os.path.dirname(os.path.dirname(__file__))
        js = open(os.path.join(
            root, 'static/src/paas/components/modal/OneTimeCredentialsModal.js'), encoding='utf-8').read()
        po = open(os.path.join(root, 'i18n/zh_TW.po'), encoding='utf-8').read()
        self.assertIn('data.template.admin_password_carrier === "config_json"', js)
        mcp_branch = js.index('admin_password_carrier === "config_json"')
        hermes_branch = js.index('Shared by the console and the dashboard')
        self.assertLess(mcp_branch, hermes_branch, 'MCP 分支必須排在 hermes 預設分支之前')
        for msgid in ('Admin Page URL',):
            self.assertIn('msgid "%s"' % msgid, po)
        self.assertIn('"Signs in to the MCP admin page (password only, no username). If you forget "', po)
        self.assertIn('"Next: open this page, sign in with the password above, and fill in the "', po)
        self.assertIn('msgstr "管理頁網址"', po)
