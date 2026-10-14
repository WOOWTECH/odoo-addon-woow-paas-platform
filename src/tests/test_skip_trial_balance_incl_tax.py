"""略過試用的錢包門檻必須是含稅總額（2026-10-14）。

正式站 Smart Home 訂閱商品帶 5% 未稅外加稅（方案 300 → 預估／發票／扣點 315）。
`_skip_trial` 原本只比未稅 `sh_plan_price`：錢包落在 [300, 315) 時放行，發票 315
付不起 ⇒ `_handle_invoice_creation` 退回上游（`sh_invoice_generated='draft'` 留草稿），
但試用已結束、下次扣款日已推一期 ⇒ **沒扣點就開始付費週期**。

走真的 Smart Home 下單路徑（seed 方案＋`_create_and_confirm_subscription`），只把 seed
商品的稅改成 5% 未稅（模擬正式站現況，交易結束即回捲）。

⚠ 拒絕案例不用 `assertRaises`：它的 savepoint 會把區塊內的寫入一起回捲，「沒有殘留」
的斷言會假綠（docs/tracking/pitfalls/odoo-assertraises-savepoint-rollback.md）。
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.models.smart_home import _platform_order


@tagged('post_install', '-at_install')
class TestSkipTrialBalanceIncludesTax(TransactionCase):

    def setUp(self):
        super().setUp()
        company = self.env.company.sudo()
        Journal = self.env['account.journal'].sudo()
        if not Journal.search([('company_id', '=', company.id), ('type', '=', 'sale')], limit=1):
            Journal.create({'name': 'Test Sales SKT', 'code': 'TSSKT', 'type': 'sale',
                            'company_id': company.id})
        if not Journal.search([('company_id', '=', company.id),
                               ('type', 'in', ('bank', 'cash'))], limit=1):
            Journal.create({'name': 'Test Bank SKT', 'code': 'TBSKT', 'type': 'bank',
                            'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(company.sh_journal_id, '前提：訂閱 journal 已設定')

        self.tax_5 = self.env['account.tax'].sudo().create({
            'name': 'SKT 5% excluded', 'amount_type': 'percent', 'amount': 5.0,
            'type_tax_use': 'sale', 'price_include_override': 'tax_excluded'})
        self.plan = self.env.ref('woow_paas_platform.plan_smarthome_mcp_monthly')
        self.product = self.env.ref('woow_paas_platform.product_smarthome_mcp_monthly')
        self.assertEqual(self.plan.sh_plan_price, 300.0, '前提：seed 方案 300 點／月')

        self.owner = self.env['res.users'].sudo().create({
            'name': 'SKT Owner', 'login': 'skt-owner@example.invalid'})
        self.partner = self.owner.partner_id
        self.workspace = self.env['woow_paas_platform.workspace'].sudo().create({
            'name': 'SKT WS', 'owner_id': self.owner.id})

    def _home_in_trial(self, taxes, wallet):
        self.product.product_tmpl_id.write({'taxes_id': [(6, 0, taxes.ids)]})
        home = _platform_order(self.env['woow_paas_platform.smart_home'].sudo()).create({
            'name': 'SKT Home', 'workspace_id': self.workspace.id})
        sub = home._create_and_confirm_subscription(self.partner, self.plan)
        self.assertTrue(sub._woow_in_trial(), '前提：新訂閱在試用中')
        self.partner.add_wallet_points(wallet, description='SKT seed')
        self.partner.invalidate_recordset(['wallet_balance'])
        return home, sub

    def _balance(self):
        self.partner.invalidate_recordset(['wallet_balance'])
        return self.partner.wallet_balance

    def _invoices(self, sub):
        return self.env['account.move'].sudo().search([
            ('sh_subscription_id', '=', sub.id), ('move_type', '=', 'out_invoice')])

    def test_wallet_below_tax_included_total_is_rejected_without_residue(self):
        home, sub = self._home_in_trial(self.tax_5, 310.0)
        self.assertEqual(home._skip_trial_estimate()['total'], 315.0, '前提：預估含稅 315')
        try:
            home._skip_trial()
        except UserError as exc:
            self.assertIn('315.00', str(exc), '訊息要寫出實際需要的含稅金額')
        else:
            self.fail('錢包 310 < 含稅 315，略過試用必須被拒絕')
        sub.invalidate_recordset()
        self.assertTrue(sub._woow_in_trial(), '被拒絕時試用不得被結束')
        self.assertFalse(self._invoices(sub), '被拒絕時不得開出（草稿）發票')
        self.assertAlmostEqual(self._balance(), 310.0, places=2)

    def test_wallet_equal_to_tax_included_total_pays_in_full(self):
        home, sub = self._home_in_trial(self.tax_5, 315.0)
        result = home._skip_trial()
        invoice = self.env['account.move'].browse(result['invoice_id'])
        self.assertAlmostEqual(invoice.amount_total, 315.0, places=2)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertAlmostEqual(self._balance(), 0.0, places=2, msg='扣點＝發票含稅總額')

    def test_tax_free_seed_still_needs_only_the_plan_price(self):
        """#366 契約（seed 免稅）不受影響：錢包剛好 300 即可。"""
        home, sub = self._home_in_trial(self.env['account.tax'], 300.0)
        result = home._skip_trial()
        invoice = self.env['account.move'].browse(result['invoice_id'])
        self.assertAlmostEqual(invoice.amount_total, 300.0, places=2)
        self.assertIn(invoice.payment_state, ('paid', 'in_payment'))
        self.assertAlmostEqual(self._balance(), 0.0, places=2)
