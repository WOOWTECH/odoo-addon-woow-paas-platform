"""ha:provision scope healer 冪等性測試（比照 _ensure_ha_smart_home_sa_scopes）。

驗證：
- seed 的 HA App public client（oauth_client_ha_app）含 ha:provision scope。
- healer 冪等把缺漏的 ha:provision 併進既有 client（升級路徑）。
- 缺 client 時 healer no-op（fail-safe）。
- F1：device 同意頁把 ha:provision 顯示為人話描述（非 raw scope 字串）。
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase

_CTRL = 'odoo.addons.woow_paas_platform.controllers.oauth2'


class TestHAProvisionScopeHealer(TransactionCase):
    """ha:provision scope healer 冪等性（比照 _ensure_ha_smart_home_sa_scopes）。"""

    def test_seeded_app_client_has_provision_scope(self):
        client = self.env.ref('woow_paas_platform.oauth_client_ha_app')
        self.assertIn('ha:provision', client.get_allowed_scopes())
        self.assertEqual(client.client_type, 'public')
        self.assertFalse(client.client_secret_hash, 'public client 不得有 secret')

    def test_healer_grants_missing_scope_idempotently(self):
        client = self.env.ref('woow_paas_platform.oauth_client_ha_app')
        client.scopes = 'workspace:read smarthome:read'
        self.env['woow_paas_platform.oauth_client']._ensure_ha_app_provision_scope()
        self.assertIn('ha:provision', client.get_allowed_scopes())
        before = client.scopes
        self.env['woow_paas_platform.oauth_client']._ensure_ha_app_provision_scope()
        self.assertEqual(client.scopes, before, '重跑 healer 不應再變動 scopes')

    def test_healer_noop_when_client_absent(self):
        # method 對缺 client 安全（比照先例 if not client: return）
        self.env['woow_paas_platform.oauth_client']._ensure_ha_app_provision_scope()


class TestHAProvisionDeviceConsentScope(TransactionCase):
    """F1：device 同意頁把 ha:provision 顯示為人話描述（非 raw scope 字串）。

    App 走 device flow 主線，/device 同意頁是使用者唯一看到授權範圍之處；
    原本模板直印 raw scope 字串，抽 module 級 SCOPE_DESCRIPTIONS 共用後兩頁一致。
    """

    def setUp(self):
        super().setUp()
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_app')
        self.DeviceCode = self.env['woow_paas_platform.oauth_device_code']

    def _req(self):
        from odoo import http
        m = MagicMock()
        m.env = self.env
        m.session = {}
        m.render.return_value = http.Response('ok')
        return m

    def test_device_consent_renders_ha_provision_human_description(self):
        from odoo.addons.woow_paas_platform.controllers.oauth2 import (
            OAuth2Controller,
        )
        dc = self.DeviceCode.create_for_client(
            self.client, 'ha:provision workspace:read')
        req = self._req()
        with patch(f'{_CTRL}.request', req):
            # 不帶 approve/deny → Stage 1 consent（render device_consent）
            OAuth2Controller().device_verify_post(user_code=dc.user_code)
        template, ctx = req.render.call_args[0]
        self.assertEqual(template, 'woow_paas_platform.device_consent')
        scopes = ctx['scopes']
        # scopes 是 [{'name','description'}]，ha:provision 顯示人話
        ha = next(s for s in scopes if s['name'] == 'ha:provision')
        self.assertEqual(
            ha['description'],
            'Create and manage your cloud Home Assistant instance')
        self.assertNotEqual(ha['description'], 'ha:provision',
                            'device 同意頁不得只印 raw scope 字串')
