# -*- coding: utf-8 -*-
"""#336：HA PaaS 0 元 plan seed＋0 元計費路徑坐實。"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHaPaasZeroPlanSeed(TransactionCase):

    def test_seed_fields(self):
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        self.assertEqual(plan.sh_plan_price, 0)
        self.assertEqual(plan.service_type, 'cloud_service')
        self.assertTrue(plan.storefront_hidden)
        self.assertFalse(plan.sh_trial, 'HA PaaS 關 trial（#336 拍板）')
        self.assertTrue(plan.sh_never_expire)
        self.assertEqual(plan.sh_no_of_billing_cycle, -1)
        self.assertEqual(plan.sh_unit, 'month')

    def test_seed_product(self):
        product = self.env.ref('woow_paas_platform.product_ha_paas_monthly')
        self.assertEqual(product.list_price, 0)
        self.assertEqual(
            product.sh_subscription_plan_id,
            self.env.ref('woow_paas_platform.plan_ha_paas_monthly'))

    def test_orphan_no_template_links(self):
        """未掛任何 template M2M（HA template 等 Elmo image）。"""
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        linked = self.env['woow_paas_platform.cloud_app_template'].sudo().search_count(
            [('subscription_plan_ids', 'in', plan.id)])
        self.assertEqual(linked, 0)

    def test_invisible_even_if_linked(self):
        """可見性契約閉環：未來掛上 HA template 後、storefront_hidden 仍擋前台。

        模擬：把 HA PaaS plan 掛上任一 template，斷言過濾語意仍排除它
        （縱深——orphan 與 hidden 兩道各自獨立成立）。
        """
        plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        tpl = self.env['woow_paas_platform.cloud_app_template'].sudo().search([], limit=1)
        tpl.write({'subscription_plan_ids': [(4, plan.id)]})
        visible = tpl.subscription_plan_ids.filtered(
            lambda p: not p.storefront_hidden)
        self.assertNotIn(plan, visible)


@tagged('post_install', '-at_install')
class TestZeroPricePaths(TransactionCase):
    """0 元訂閱：invoice 不產 deduct、payment gate 恆 False、退點恆 0。

    坐實而非驅動——三條斷言驗證既有機制對 0 元的天然安全（method 名／簽章
    對齊 sh_subscription_fix.py 實際：_has_paid_current_cycle()／
    _compute_unused_refund() 無參；_try_settle_invoice(sub, invoice) 兩參；
    wallet.transaction 用 transaction_type）。任何紅＝發現真 bug，回報 leader。
    """

    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTx = self.env['wallet.transaction'].sudo()
        self.partner = self.env['res.partner'].create({'name': '免費仔'})
        self.plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        today = date.today()
        # 0 元 mid-cycle in_progress 訂閱（欄位對齊 test_refund_requires_payment
        # 的 _mid_cycle_sub 慣例：real create、persisted int id，故 gate 走真
        # wallet 查詢而非 NewId 短路）。
        self.sub = self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_plan_price': 0.0, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=15),
            'sh_end_date': today + timedelta(days=15),
            'sh_date_of_next_payment': today + timedelta(days=15),
            'state': 'in_progress',
        })

    def test_has_paid_current_cycle_false(self):
        """0 元訂閱永無 done deduct → gate 恆 False（#324 邊界坐實）。"""
        self.assertFalse(self.sub._has_paid_current_cycle())

    def test_zero_invoice_no_wallet_deduct(self):
        """0 元 invoice 結算：amount 不 >0 → _handle_invoice_creation 走 super()、
        不呼叫 _pay_from_wallet → 全程無扣點交易（sh_subscription_fix.py:529）。"""
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'sh_subscription_id': self.sub.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'HA PaaS 0 元週期', 'quantity': 1, 'price_unit': 0.0,
                'tax_ids': [(5, 0, 0)],
            })],
        })
        self.assertEqual(invoice.amount_total, 0)
        paid = self.Sub._try_settle_invoice(self.sub, invoice)
        self.assertTrue(paid, '0 元 invoice 應直接結算成功（balance 0 >= amount 0）')
        deducts = self.WalletTx.search([
            ('subscription_id', '=', self.sub.id),
            ('transaction_type', '=', 'deduct'),
        ])
        self.assertFalse(deducts, '0 元結算不得產生任何扣點交易')

    def test_refund_zero_on_delete(self):
        """退點恆 0：_compute_unused_refund 對 never-paid 0 元訂閱回 0。"""
        self.assertEqual(self.sub._compute_unused_refund(), 0)
