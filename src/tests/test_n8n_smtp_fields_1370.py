"""#1370 後半：n8n 忘記密碼／邀請信要能寄出——範本開放租戶選填 SMTP。

n8n 只能用環境變數設定 SMTP（UI 沒有入口），所以平台不開欄位租戶就完全無法寄信：
忘記密碼、邀請成員的信都靜默失敗。這裡鎖：

- 6 個選填欄位都在 n8n 範本、都不是必填；預設值等於 n8n 自己的預設（465／SSL true），
  沒填 SMTP Host 的既有行為不變。
- 只有密碼走 ``main.secret``（chart 的 app-secret，與平台注入的 N8N_API_KEY 同一顆
  Secret）、type=password，服務詳情 API 會遮罩（#535 spec-driven）；其他走 ``main.config``。
"""
import json

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController

SMTP_KEYS = {
    'main.config.N8N_SMTP_HOST': 'text',
    'main.config.N8N_SMTP_PORT': 'number',
    'main.config.N8N_SMTP_SSL': 'select',
    'main.config.N8N_SMTP_USER': 'text',
    'main.secret.N8N_SMTP_PASS': 'password',
    'main.config.N8N_SMTP_SENDER': 'text',
}


@tagged('post_install', '-at_install')
class TestN8nSmtpFields1370(TransactionCase):

    def _specs(self):
        return json.loads(self.env.ref('woow_paas_platform.cloud_app_n8n').helm_value_specs)

    def test_optional_smtp_fields(self):
        specs = self._specs()
        optional = {s['key']: s for s in specs['optional']}
        required = {s['key'] for s in specs['required']}
        for key, typ in SMTP_KEYS.items():
            self.assertIn(key, optional, key)
            self.assertNotIn(key, required, key)
            self.assertEqual(optional[key]['type'], typ, key)

    def test_defaults_match_n8n_defaults(self):
        optional = {s['key']: s for s in self._specs()['optional']}
        self.assertEqual(optional['main.config.N8N_SMTP_PORT'].get('default'), 465)
        self.assertEqual(optional['main.config.N8N_SMTP_SSL'].get('default'), 'true')
        self.assertEqual(optional['main.config.N8N_SMTP_SSL'].get('options'), ['true', 'false'])
        for key in ('main.config.N8N_SMTP_HOST', 'main.config.N8N_SMTP_USER',
                    'main.secret.N8N_SMTP_PASS', 'main.config.N8N_SMTP_SENDER'):
            self.assertNotIn('default', optional[key], key)

    def test_password_is_masked_in_api(self):
        template = self.env.ref('woow_paas_platform.cloud_app_n8n')
        sanitized = PaasController._sanitize_helm_values({
            'main': {
                'config': {'N8N_SMTP_HOST': 'smtp.example.com', 'N8N_SMTP_USER': 'bot'},
                'secret': {'N8N_SMTP_PASS': 'smtp-secret-1370'},
            },
        }, template)
        self.assertNotIn('smtp-secret-1370', json.dumps(sanitized))
        self.assertEqual(sanitized['main']['config']['N8N_SMTP_HOST'], 'smtp.example.com')
        self.assertEqual(sanitized['main']['config']['N8N_SMTP_USER'], 'bot')
