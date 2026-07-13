"""HA PaaS hidden template 鏈測試（#336）。

- TestHAPaaSSeed：0 元 plan／product／隱藏 template seed 存在性與屬性。
- TestHiddenChainFiltering：hidden template 的 service／訂閱／發票不出現在前台
  任何清單（marketplace／workspace／dashboard／billing）。
"""
from odoo import fields
from odoo.tests.common import HttpCase, TransactionCase, tagged


class TestHAPaaSSeed(TransactionCase):
    """HA PaaS 0 元 plan／product／隱藏 template seed 存在性與屬性。"""

    def test_ha_paas_template_hidden_and_inactive(self):
        tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        self.assertTrue(tpl.is_hidden, 'HA PaaS template 必須隱藏')
        self.assertFalse(tpl.is_active, '工作項5 image 未備前 template 不可 deployable')

    def test_ha_paas_plan_zero_price_cloud_service(self):
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        self.assertEqual(plan.sh_plan_price, 0.0)
        self.assertEqual(plan.service_type, 'cloud_service')
        self.assertFalse(plan.sh_trial, '0 元不走 trial 路徑')

    def test_ha_paas_plan_is_orphan(self):
        # #341（Wave A）撞車收斂：HA PaaS plan 刻意 orphan——不掛任何 template M2M。
        # 前台隱藏由兩層縱深達成：plan 層 storefront_hidden（擋購買選項）＋ template 層
        # is_hidden（擋服務/訂閱/發票鏈）。provision 端點直接把 plan ref 傳給
        # _create_and_confirm_subscription，不經 template↔plan M2M，故 plan 無需掛 template。
        # 與 Wave A test_ha_paas_zero_plan_336.test_orphan_no_template_links 一致。
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        linked = self.env['woow_paas_platform.cloud_app_template'].sudo().search_count(
            [('subscription_plan_ids', 'in', plan.id)])
        self.assertEqual(linked, 0, 'HA PaaS plan 不掛任何 template（orphan 設計）')

    def test_existing_templates_default_visible(self):
        n8n = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.assertFalse(n8n.is_hidden)


@tagged('post_install', '-at_install')
class TestHiddenChainFiltering(HttpCase):
    """HA PaaS（hidden template）的 service／訂閱／發票不出現在前台任何清單。

    負向覆蓋設計（同時滿足 repo-lint 對 controller sudo 的 cross-user 要求）：以 admin
    session 登入，且 hidden HA PaaS service/subscription 的 owner 正是 admin 本人——證明
    HiddenChainMixin 的 sudo helper 是**收斂式排除過濾**（subtractive），即使對擁有者本人
    也不把 hidden 鏈資料回給前台端點，非 sudo-list-and-return 的 data leak。
    """

    def setUp(self):
        super().setUp()
        self.authenticate('admin', 'admin')
        # HttpCase 下 self.env.user 是 __system__；用 session 綁定的 admin partner
        self.partner = self.env.ref('base.partner_admin')
        self.admin_user = self.env.ref('base.user_admin')
        self.tpl = self.env.ref('woow_paas_platform.cloud_app_ha_paas')
        self.plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        # 由 admin 擁有的 hidden HA PaaS 服務＋0 元訂閱（owner 本人也不該看到）
        self.ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'HA PaaS WS 336', 'owner_id': self.admin_user.id})
        self.svc = self.env['woow_paas_platform.cloud_service'].create({
            'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'name': 'ha-paas', 'reference_id': 'test-ha-ref-336',
            'state': 'running', 'subdomain': 'ha-test-336',
            'provision_partner_id': self.partner.id})
        self.svc._create_and_confirm_subscription(self.partner, self.plan)
        self.sub = self.svc.subscription_id

    def _post(self, url, params='{}'):
        return self.url_open(url,
            data='{"jsonrpc":"2.0","method":"call","params":' + params + '}',
            headers={'Content-Type': 'application/json'})

    def _result(self, url, params='{}'):
        body = self._post(url, params).json()
        self.assertIn('result', body, 'resp: %s' % body)
        return body['result']

    def test_marketplace_endpoint_excludes_hidden(self):  # 曝光點 1
        r = self._result('/api/cloud/templates')
        self.assertTrue(r['success'], r)
        self.assertNotIn(self.tpl.id, [t['id'] for t in r['data']],
                         'marketplace 不得列出 hidden template')

    def test_services_list_excludes_hidden(self):  # 曝光點 3
        r = self._result('/api/workspaces/%s/services' % self.ws.id,
                         '{"action":"list"}')
        self.assertTrue(r['success'], r)
        self.assertNotIn(self.svc.id, [s['id'] for s in r['data']],
                         'workspace 服務清單不得列出 hidden service')

    def test_subscriptions_endpoint_excludes_hidden(self):  # 曝光點 6
        # filter='active' 會納入 in_progress（hidden sub 即 in_progress），
        # 若無 hidden 過濾就會現形——故此斷言有意義。
        r = self._result('/api/billing/subscriptions', '{"filter":"active"}')
        self.assertTrue(r['success'], r)
        self.assertNotIn(self.sub.id, [s['id'] for s in r['data']],
                         '訂閱清單不得列出 0 元 hidden 訂閱')

    def test_invoices_endpoint_excludes_hidden(self):  # 曝光點 8
        # 建一張掛 hidden sub 的 out_invoice（admin partner），確認被排除
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'sh_subscription_id': self.sub.id})
        r = self._result('/api/billing/invoices')  # status=None → 全狀態
        self.assertTrue(r['success'], r)
        self.assertNotIn(inv.id, [i['id'] for i in r['data']],
                         'hidden sub 的發票不得出現在發票清單')

    def test_upcoming_renewals_excludes_hidden(self):  # 曝光點 14
        # 強制 next_payment 落在 3 日窗內使其「本會續期」，證明是 hidden 過濾排除它
        self.sub.write({'sh_date_of_next_payment': fields.Date.today()})
        r = self._result('/api/billing/upcoming-renewals')
        self.assertTrue(r['success'], r)
        sub_ids = [u['subscription_id'] for u in r['data']['upcoming']]
        self.assertNotIn(self.sub.id, sub_ids,
                         '即將續期清單不得列出 0 元 hidden 訂閱')

    def test_generic_create_rejects_hidden_template(self):  # 曝光點 16
        r = self._result('/api/workspaces/%s/services' % self.ws.id,
            '{"action":"create","template_id":%s,"name":"x"}' % self.tpl.id)
        self.assertFalse(r['success'],
                         'generic create 不得直接部署 hidden template')

    def test_service_detail_by_id_not_found(self):  # 曝光點 10
        r = self._result('/api/workspaces/%s/services/%s'
                         % (self.ws.id, self.svc.id))
        self.assertFalse(r['success'],
                         'hidden service by-id detail 應回 not found')

    def test_change_plan_by_id_not_found(self):  # 曝光點 9
        r = self._result('/api/cloud-services/services/%s/change-plan'
                         % self.svc.id, '{"plan_id":%s}' % self.plan.id)
        self.assertFalse(r['success'],
                         'hidden service change-plan 應回 not found')

    def test_invoice_detail_by_id_not_found(self):  # 曝光點 13
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'sh_subscription_id': self.sub.id})
        r = self._result('/api/billing/invoices/%s' % inv.id)
        self.assertFalse(r['success'],
                         'hidden sub 發票 by-id detail 應回 not found')
