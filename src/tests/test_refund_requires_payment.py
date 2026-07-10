"""#324 part (b): 退點前提＝本訂閱有成功扣款（done deduct）。

堵住套利路徑：訂閱→付款靜默失敗（服務照跑）→刪服務→退未賺得點。
never-paid（只有 failed deduct 或無交易）刪服務退 0；真已付（有 done
deduct）仍按剩餘比例退（#318 happy path 不回歸）。
"""
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class TestRefundRequiresPayment(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTx = self.env['wallet.transaction'].sudo()
        self.partner = self.env['res.partner'].create({'name': 'Gate Partner'})

    def _mid_cycle_sub(self):
        """今天正好在 [start, end] 中點的 in_progress 訂閱（純公式應回 50）。"""
        today = date.today()
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_plan_price': 100.0, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=15),
            'sh_end_date': today + timedelta(days=15),
            'sh_date_of_next_payment': today + timedelta(days=15),
            'state': 'in_progress',
        })

    def _seed_deduct(self, sub, state):
        return self.WalletTx.create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -100.0, 'subscription_id': sub.id, 'state': state,
        })

    def test_never_paid_refunds_zero(self):
        """完全無扣款交易 → 退 0（服務照跑但從未成功付款）。"""
        sub = self._mid_cycle_sub()
        self.assertEqual(sub._compute_unused_refund(), 0)

    def test_only_failed_deduct_refunds_zero(self):
        """只有 failed deduct（#324 類）→ 退 0。"""
        sub = self._mid_cycle_sub()
        self._seed_deduct(sub, 'failed')
        self.assertEqual(sub._compute_unused_refund(), 0)

    def test_done_deduct_refunds_prorata(self):
        """有 done deduct（真已付）→ 按剩餘比例退（純公式 50）。"""
        sub = self._mid_cycle_sub()
        self._seed_deduct(sub, 'done')
        self.assertEqual(sub._compute_unused_refund(), 50.0)

    def test_prorata_formula_still_reachable_without_gate(self):
        """純公式方法本身不受 gate 影響（Task 2 的抽取仍在）。"""
        sub = self._mid_cycle_sub()
        self.assertEqual(sub._prorata_unused_refund(), 50.0)

    def test_settle_on_delete_never_paid_credits_zero(self):
        """never-paid in_progress 訂閱刪除結算：取消但退 0（不得退未賺得點）。"""
        sub = self._mid_cycle_sub()          # 無 done deduct
        credited = sub._settle_on_entity_delete()
        self.assertEqual(sub.state, 'cancel')  # 仍取消以止付
        self.assertEqual(credited, 0.0)        # 但不退未賺得點
        self.assertFalse(self.WalletTx.search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund')]))

    def test_manual_invoice_pay_stamps_subscription_and_enables_refund(self):
        """#324 F1 回歸：手動付款端點 invoice._pay_from_wallet()（不帶
        subscription）付掉訂閱週期發票時，deduct 要 stamp 上 subscription_id，
        退點 gate 才認得 → 真已付的週期刪服務仍按比例退（不因手動付款誤退 0）。"""
        company = self.env.company.sudo()
        journal = self.env['account.journal'].sudo().search(
            [('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))],
            limit=1)
        if not journal:
            self.env['account.journal'].sudo().create({
                'name': 'Test Bank 324', 'type': 'bank', 'code': 'TB324',
                'company_id': company.id})
        sub = self._mid_cycle_sub()
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'sh_subscription_id': sub.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'cycle', 'quantity': 1, 'price_unit': 100.0,
                'tax_ids': [(5, 0, 0)],
            })],
        })
        self.partner.sudo().add_wallet_points(100.0, description='seed')
        self.partner.invalidate_recordset(['wallet_balance'])

        invoice._pay_from_wallet()  # 手動付款路徑：不帶 subscription

        # deduct 已被 stamp → gate 認得 → 退點回純比例（非 0）
        self.assertTrue(sub._has_paid_current_cycle())
        self.assertEqual(sub._compute_unused_refund(), 50.0)
