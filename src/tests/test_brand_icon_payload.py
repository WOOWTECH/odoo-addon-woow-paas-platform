"""服務品牌圖示（2026-10-09）：前台依模板代碼挑品牌圖示，訂閱相關 API 要帶得出代碼。

- `/api/billing/subscriptions` 每筆帶 `service_template_slug`（連到雲端服務時＝模板代碼）
  與 `service_type`（方案的服務類型；智慧家庭／安全存取靠它挑圖示）。
- `/api/billing/subscriptions/<id>` 的 `services[]` 每筆帶 `template_slug`。
- 每筆另帶 `plan_template_slug`（方案所屬雲端服務模板）：服務已刪的歷史訂閱靠它挑圖示；
  同一方案掛兩個模板時取上架中的（ha-full 與已隱藏的 ha-full-apporo 共用方案）。
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
        self.ws = ws

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

    def _detach(self, svc):
        """模擬「服務已刪」：controller 判斷的是「有沒有 cloud_service 指向這筆訂閱」。
        model 層禁止把服務與訂閱解綁（SubscriptionBindingError），測試直接改 DB 欄位，
        只為造出「訂閱還在、已無服務連到它」的狀態。"""
        self.env.cr.execute(
            'UPDATE woow_paas_platform_cloud_service SET subscription_id = NULL WHERE id = %s',
            [svc.id])
        svc.invalidate_recordset(['subscription_id'])

    def _row(self, sub):
        rows = self._result('/api/billing/subscriptions', '{"filter":"all","limit":50}')
        row = next((r for r in rows if r['id'] == sub.id), None)
        self.assertTrue(row, 'fixture 訂閱應出現在清單')
        return row

    def test_plan_template_slug_survives_deleted_service(self):
        """服務刪掉（不再連到訂閱）後，仍可從方案反查模板代碼。"""
        self.assertEqual(self._row(self.sub)['plan_template_slug'], 'n8n')
        self._detach(self.svc)
        row = self._row(self.sub)
        self.assertIsNone(row['service_template_slug'], '已無連到的服務')
        self.assertEqual(row['plan_template_slug'], 'n8n')

    def test_plan_shared_by_two_templates_prefers_listed_one(self):
        """plan_ha_full_* 同時掛在 ha-full 與已隱藏的 ha-full-apporo：取上架中的 ha-full。"""
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_full')
        plan = self.env.ref('woow_paas_platform.plan_ha_full_monthly')
        apporo = self.env.ref('woow_paas_platform.cloud_app_ha_full_apporo')
        self.assertIn(plan, apporo.subscription_plan_ids, 'fixture 前提：兩個模板共用方案')
        self.assertTrue(apporo.is_hidden, 'fixture 前提：APPORO 版已隱藏')
        svc = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'workspace_id': self.ws.id, 'template_id': tpl.id,
            'name': 'brand-ha-full', 'reference_id': 'test-brand-icon-ha-ref',
            'state': 'running', 'subdomain': 'brand-icon-ha-test'})
        svc._create_and_confirm_subscription(self.partner, plan)
        sub = svc.subscription_id
        self._detach(svc)
        self.assertEqual(self._row(sub)['plan_template_slug'], 'ha-full')

    def test_cross_user_cannot_see_plan_template_slug(self):
        """_plan_template_slug 以 sudo 讀模板目錄，但只用「這筆訂閱自己的方案」查：
        別的使用者拿不到這筆訂閱，也就拿不到它的模板代碼（清單、詳情都一樣）。"""
        self.env['res.users'].sudo().with_context(no_reset_password=True).create({
            'name': 'Brand icon other', 'login': 'brand_icon_other',
            'password': 'brand_icon_other', 'email': 'brand_icon_other@example.test',
            'groups_id': [(6, 0, [self.env.ref('base.group_portal').id])],
        })
        self.authenticate('brand_icon_other', 'brand_icon_other')
        rows = self._result('/api/billing/subscriptions', '{"filter":"all","limit":50}')
        self.assertNotIn(self.sub.id, [r['id'] for r in rows])
        body = self.url_open(
            '/api/billing/subscriptions/%s' % self.sub.id,
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'}).json()
        result = body.get('result') or {}
        self.assertFalse(result.get('success'), result)
        self.assertNotIn('plan_template_slug', result.get('data') or {})
