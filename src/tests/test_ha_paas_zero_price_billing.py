"""0 元 HA PaaS 訂閱 × #324 payment gate 邊界坐實（#336）。

純測試（無 src 變更），坐實既有行為：
- 0 元訂閱從未有成功扣款（``done`` deduct）→ ``_has_paid_current_cycle`` 恆 False。
- 故 ``_compute_unused_refund`` 經 never-paid gate 短路 → 退點恆 0（無「刪服務退未
  賺得點」套利）。
- 0 元發票金額 <=0 → ``_pay_from_wallet`` 走 "Nothing to pay" guard，永不進扣款路徑。

對照 ``test_refund_requires_payment.py`` 的 gate 語意。
"""
from odoo.tests.common import TransactionCase


class TestHAPaaSZeroPriceBilling(TransactionCase):
    """0 元 HA PaaS 訂閱 × #324 payment gate 邊界（退點恆 0、不建 deduct）。"""

    def setUp(self):
        super().setUp()
        self.plan = self.env.ref('woow_paas_platform.plan_ha_paas_monthly')
        self.partner = self.env['res.partner'].create({'name': 'Zero Price User'})
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTxn = self.env['wallet.transaction'].sudo()

    def _make_zero_sub(self):
        product = self.env['product.template'].search(
            [('sh_product_subscribe', '=', True),
             ('sh_subscription_plan_id', '=', self.plan.id)], limit=1)
        self.assertTrue(product, '0 元 plan 對應 product 必須存在')
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'product_id': product.product_variant_id.id,
            'sh_subscription_plan_id': self.plan.id,
            'sh_source': 'manual',
        })

    def test_zero_sub_has_no_done_deduct(self):
        sub = self._make_zero_sub()
        self.assertFalse(self.WalletTxn.search_count([
            ('subscription_id', '=', sub.id),
            ('transaction_type', '=', 'deduct'),
            ('state', '=', 'done'),
        ]), '0 元訂閱不得產生 done deduct')
        self.assertFalse(sub._has_paid_current_cycle(),
                         '0 元訂閱 _has_paid_current_cycle 恆 False')

    def test_zero_sub_refund_is_zero(self):
        sub = self._make_zero_sub()
        self.assertEqual(sub._compute_unused_refund(), 0.0,
                         '0 元訂閱退點恆 0（#324 payment gate 邊界，無套利）')

    def test_zero_invoice_never_hits_pay_from_wallet(self):
        from odoo.exceptions import UserError
        move = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice', 'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'HA PaaS 0 元', 'quantity': 1, 'price_unit': 0.0})],
        })
        move.action_post()
        with self.assertRaises(UserError):
            move._pay_from_wallet()  # amount<=0 → "Nothing to pay"
