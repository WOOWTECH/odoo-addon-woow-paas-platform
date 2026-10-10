"""服務品牌圖示（2026-10-09）：前台依模板代碼挑品牌圖示，訂閱相關 API 要帶得出代碼。

- `/api/billing/subscriptions` 每筆帶 `service_template_slug`（連到雲端服務時＝模板代碼）
  與 `service_type`（方案的服務類型；智慧家庭／安全存取靠它挑圖示）。
- `/api/billing/subscriptions/<id>` 的 `services[]` 每筆帶 `template_slug`。
對照表在 static/src/paas/utils/brand_icons.js。
"""
from odoo.tests.common import HttpCase, tagged

from ..models.cloud_service import _internal_provision


@tagged('post_install', '-at_install')
class TestBrandIconPayload(HttpCase):

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        self.partner = self.env.ref('base.partner_admin')
        admin = self.env.ref('base.user_admin')
        tpl = self.env.ref('woow_paas_platform.cloud_app_n8n')
        plan = self.env.ref('woow_paas_platform.plan_n8n_monthly')
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Brand icon WS', 'owner_id': admin.id})
        self.svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': ws.id, 'template_id': tpl.id,
            'name': 'brand-n8n', 'reference_id': 'test-brand-icon-ref',
            'state': 'running', 'subdomain': 'brand-icon-test',
            'provision_partner_id': self.partner.id})
        self.svc._create_and_confirm_subscription(self.partner, plan)
        self.sub = self.svc.subscription_id

    def _result(self, url, params='{}'):
        body = self.url_open(
            url, data='{"jsonrpc":"2.0","method":"call","params":' + params + '}',
            headers={'Content-Type': 'application/json'}).json()
        self.assertIn('result', body, 'resp: %s' % body)
        self.assertTrue(body['result']['success'], body['result'])
        return body['result']['data']

    def test_subscription_list_carries_template_slug_and_service_type(self):
        rows = self._result('/api/billing/subscriptions', '{"filter":"active"}')
        row = next((r for r in rows if r['id'] == self.sub.id), None)
        self.assertTrue(row, 'fixture 訂閱應出現在清單')
        self.assertEqual(row['service_template_slug'], 'n8n')
        self.assertEqual(row['service_type'], 'cloud_service')

    def test_subscription_detail_services_carry_template_slug(self):
        data = self._result('/api/billing/subscriptions/%s' % self.sub.id)
        self.assertEqual([s['template_slug'] for s in data['services']], ['n8n'])
