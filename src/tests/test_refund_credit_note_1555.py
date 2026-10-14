"""#1555：退點時同步建立貸項並以退點沖銷原發票。

2026-10-07 第二階段實測（N8）：已付款的服務刪除時錢包退點，原發票卻仍是 posted／paid，
沒有 reversal、沒有折讓。本檔以固定測試時鐘（同 ``test_refund_requires_payment``）驗：

* 付費中刪除（剩一半）→ 貸項＝一半的未稅金額、``reversed_entry_id`` 指回原發票、
  以退點的 outbound payment 沖銷（未稅商品：貸項 paid）；
* 含稅商品（政策 A，2026-10-14）→ 退點以實付含稅總額按比例計算，貸項總額＝退點、全額沖銷；
  內含稅全退時貸項不得被夾成未稅額；
* 全額退 → 原發票若還沒開電子發票，改為 ``not_required``（不能替已全額退款的交易開票）；
* 原發票已開電子發票 → 不在交易裡打 ECPay，留言＋替帳務管理群組建「需開折讓」待辦；
* 公司設成自動開票時，貸項過帳不會 inline 打 ECPay 折讓（``run_refund`` override）；
* 試用中（沒付過）→ 不建貸項；
* 建貸項失敗 → 只記 log，刪除結算（取消＋退點）照常；
* 換方案的退點（三個 controller）同樣呼叫這支（靜態讀檔）。
"""
import os
from datetime import date
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger

_PLAN_PRICE = 100.0
_NEXT_PAYMENT = date(2026, 5, 16)
_CYCLE_START = date(2026, 4, 16)       # 30 天的一期
_MID_CYCLE = date(2026, 5, 1)          # 剩 15 天 ⇒ 退一半
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _today(day):
    return patch.object(fields.Date, 'today', staticmethod(lambda *args: day))


class _RefundFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Refund 1555 Partner'})
        company = self.env.company.sudo()
        if not self.env['account.journal'].sudo().search(
                [('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Bank 1555', 'type': 'bank', 'code': 'TB155',
                'company_id': company.id})
        self.tax_5 = self.env['account.tax'].sudo().create({
            'name': 'Refund 1555 Tax 5%', 'amount_type': 'percent', 'amount': 5.0,
            'type_tax_use': 'sale', 'price_include_override': 'tax_excluded'})
        self.Sub = self.env['sh.subscription.subscription'].sudo()

    def _paid_sub(self, taxes=None, today=_MID_CYCLE):
        """一張 in_progress、這一期已用錢包付清的訂閱，回傳 (sub, invoice)。"""
        sub = self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_plan_price': _PLAN_PRICE, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': _CYCLE_START,
            'sh_end_date': _NEXT_PAYMENT,
            'sh_date_of_next_payment': _NEXT_PAYMENT,
            'state': 'in_progress',
        })
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'sh_subscription_id': sub.id,
            'invoice_date': _CYCLE_START,
            'invoice_line_ids': [(0, 0, {
                'name': 'Plan 1555 cycle', 'quantity': 1, 'price_unit': _PLAN_PRICE,
                'tax_ids': [(6, 0, (taxes or self.env['account.tax']).ids)],
            })],
        })
        self.partner.sudo().add_wallet_points(invoice.amount_total, description='seed 1555')
        self.partner.invalidate_recordset(['wallet_balance'])
        invoice._pay_from_wallet()
        self.assertEqual(invoice.payment_state, 'paid', '前提：原發票已付清')
        return sub, invoice

    def _credit_notes(self, invoice):
        return self.env['account.move'].sudo().search([
            ('reversed_entry_id', '=', invoice.id), ('move_type', '=', 'out_refund')])

    def _balance(self):
        self.partner.invalidate_recordset(['wallet_balance'])
        return self.partner.wallet_balance


@tagged('post_install', '-at_install')
class TestRefundCreditNote1555(_RefundFixture):

    def test_delete_mid_cycle_credits_half_and_settles_it(self):
        sub, invoice = self._paid_sub()
        before = self._balance()
        with _today(_MID_CYCLE):
            credited = sub._settle_on_entity_delete()
        self.assertEqual(credited, _PLAN_PRICE / 2)
        self.assertAlmostEqual(self._balance() - before, _PLAN_PRICE / 2, places=2)
        credit_note = self._credit_notes(invoice)
        self.assertEqual(len(credit_note), 1, '一次退點＝一張貸項')
        self.assertEqual(credit_note.state, 'posted')
        self.assertAlmostEqual(credit_note.amount_untaxed, _PLAN_PRICE / 2, places=2)
        self.assertEqual(credit_note.payment_state, 'paid', '未稅商品：退點即全額沖銷')
        self.assertTrue(credit_note.woow_skip_upstream_hook)
        self.assertEqual(credit_note.woow_invoice_issue_state, 'not_required')
        self.assertEqual(invoice.payment_state, 'paid', '原發票本身仍是客戶付清的那張')

    def test_excluded_tax_invoice_refunds_tax_included_amount(self):
        """政策 A（2026-10-14，取代 #1261 待決）：外加稅的舊發票（付 105）剩一半 ⇒ 退 52.5
        （含稅），貸項總額＝退點、全額沖銷，不再留稅額那段未沖。"""
        sub, invoice = self._paid_sub(taxes=self.tax_5)
        self.assertAlmostEqual(invoice.amount_total, 105.0, places=2)
        with _today(_MID_CYCLE):
            credited = sub._settle_on_entity_delete()
        self.assertAlmostEqual(credited, 52.5, places=2, msg='退點基準＝實付含稅總額')
        credit_note = self._credit_notes(invoice)
        self.assertAlmostEqual(credit_note.amount_untaxed, 50.0, places=2)
        self.assertAlmostEqual(credit_note.amount_tax, 2.5, places=2)
        self.assertAlmostEqual(credit_note.amount_total, credited, places=2)
        self.assertEqual(credit_note.payment_state, 'paid')

    def test_included_tax_full_refund_credit_note_equals_refund(self):
        """內含稅（政策 A）：付 100（未稅 95.24＋稅 4.76）立即全退 ⇒ 退 100、貸項總額 100。

        原本第二道 clamp 夾在原發票**未稅額** 95.24、再當含稅單價開立 ⇒ 貸項只記 95.24。"""
        self.tax_5.write({'price_include_override': 'tax_included'})
        sub, invoice = self._paid_sub(taxes=self.tax_5)
        self.assertAlmostEqual(invoice.amount_total, 100.0, places=2)
        with _today(_CYCLE_START):
            credited = sub._settle_on_entity_delete()
        self.assertAlmostEqual(credited, 100.0, places=2)
        credit_note = self._credit_notes(invoice)
        self.assertAlmostEqual(credit_note.amount_total, 100.0, places=2)
        self.assertAlmostEqual(credit_note.amount_untaxed, invoice.amount_untaxed, places=2)
        self.assertEqual(credit_note.payment_state, 'paid')

    def test_full_refund_before_einvoice_stops_issuance(self):
        sub, invoice = self._paid_sub()
        invoice.write({'woow_invoice_issue_state': 'manual'})
        with _today(_CYCLE_START):
            credited = sub._settle_on_entity_delete()
        self.assertEqual(credited, _PLAN_PRICE, '前提：整期未用＝全額退')
        self.assertEqual(invoice.woow_invoice_issue_state, 'not_required',
                         '已全額退款的交易不得再開電子發票')

    def test_partial_refund_before_einvoice_keeps_issuance_but_notes_it(self):
        sub, invoice = self._paid_sub()
        invoice.write({'woow_invoice_issue_state': 'manual'})
        with _today(_MID_CYCLE):
            sub._settle_on_entity_delete()
        self.assertEqual(invoice.woow_invoice_issue_state, 'manual')
        credit_note = self._credit_notes(invoice)
        self.assertTrue(invoice.message_ids.filtered(
            lambda m: credit_note.name in (m.body or '')),
            '補開電子發票的人要看得到已退的部分')

    def test_issued_einvoice_asks_for_a_manual_allowance(self):
        sub, invoice = self._paid_sub()
        invoice.write({
            'woow_invoice_issue_state': 'issued',
            'ecpay_invoice_id': self.env['uniform.invoice'].sudo().create({
                'name': 'AB12345678', 'company_id': self.env.company.id}).id,
        })
        managers = self.env.ref('account.group_account_manager').users
        with _today(_MID_CYCLE):
            sub._settle_on_entity_delete()
        credit_note = self._credit_notes(invoice)
        self.assertTrue(credit_note)
        self.assertEqual(invoice.woow_invoice_issue_state, 'issued')
        activities = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'account.move'), ('res_id', '=', credit_note.id)])
        if managers:
            self.assertEqual(set(activities.mapped('user_id').ids), set(managers.ids))
        self.assertTrue(credit_note.message_ids.filtered(
            lambda m: 'AB12345678' in (m.body or '')))

    def test_automatic_company_does_not_call_ecpay_inline(self):
        """公司設成自動開票：上游 action_post 會對 out_refund inline run_refund（打 ECPay、
        找不到原發票的電子發票就 raise）。平台貸項帶 skip 旗標，不得走到那裡。"""
        sub, invoice = self._paid_sub()
        self.env.company.sudo().write({'auto_invoice': 'automatic'})
        with _today(_MID_CYCLE), patch(
                'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove.run_refund',
                side_effect=AssertionError('must not reach ECPay')) as upstream:
            sub._settle_on_entity_delete()
        upstream.assert_not_called()
        self.assertEqual(self._credit_notes(invoice).state, 'posted')

    def test_plan_change_then_delete_credits_each_invoice_once(self):
        """換方案（舊訂閱退剩餘期間）後再刪除（新訂閱退剩餘期間）：兩張發票各一張貸項。

        換方案的步驟照三個 controller 的順序：算退點 → 退回錢包 → 建貸項 → 關舊訂閱。
        """
        old_sub, old_invoice = self._paid_sub()
        with _today(_MID_CYCLE):
            refund = old_sub._compute_unused_refund()
            self.partner.sudo().refund_wallet_points(
                amount=refund, description='Plan change refund', subscription=old_sub)
            old_sub._woow_record_refund_credit_note(refund, 'Plan change')
            old_sub.write({'state': 'close'})
        new_sub, new_invoice = self._paid_sub(taxes=self.tax_5)
        with _today(_MID_CYCLE):
            credited = new_sub._settle_on_entity_delete()
        old_cn, new_cn = self._credit_notes(old_invoice), self._credit_notes(new_invoice)
        self.assertEqual((len(old_cn), len(new_cn)), (1, 1))
        self.assertAlmostEqual(old_cn.amount_total, refund, places=2)
        self.assertAlmostEqual(new_cn.amount_total, credited, places=2)
        self.assertIn('Plan change', old_cn.ref)
        self.assertEqual(old_cn.payment_state, 'paid')
        self.assertEqual(new_cn.payment_state, 'paid', '政策 A：退點含稅，貸項全額沖銷')

    def test_trial_or_unpaid_creates_nothing(self):
        sub = self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_plan_price': _PLAN_PRICE, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': _CYCLE_START, 'sh_date_of_next_payment': _NEXT_PAYMENT,
            'state': 'in_progress',
        })
        before = self.env['account.move'].sudo().search_count([('move_type', '=', 'out_refund')])
        with _today(_MID_CYCLE):
            self.assertEqual(sub._settle_on_entity_delete(), 0.0)
        self.assertEqual(
            self.env['account.move'].sudo().search_count([('move_type', '=', 'out_refund')]),
            before)

    def test_bookkeeping_failure_does_not_break_the_settlement(self):
        sub, invoice = self._paid_sub()
        before = self._balance()
        with _today(_MID_CYCLE), patch.object(
                type(sub), '_woow_create_refund_credit_note',
                side_effect=RuntimeError('boom')), mute_logger(
                'odoo.addons.woow_paas_platform.models.sh_subscription_refund_credit_note'):
            credited = sub._settle_on_entity_delete()
        self.assertEqual(credited, _PLAN_PRICE / 2)
        self.assertEqual(sub.state, 'cancel')
        self.assertAlmostEqual(self._balance() - before, _PLAN_PRICE / 2, places=2)
        self.assertFalse(self._credit_notes(invoice))

    def test_run_refund_short_circuits_only_flagged_moves(self):
        flagged = self.env['account.move'].sudo().new({
            'move_type': 'out_refund', 'woow_skip_upstream_hook': True})
        with patch(
                'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove.run_refund',
                side_effect=AssertionError('must not reach ECPay')) as upstream:
            self.assertIsNone(flagged.run_refund())
        upstream.assert_not_called()


@tagged('post_install', '-at_install')
class TestPlanChangeRefundCreditNote1555(TransactionCase):
    """換方案的退點（CS／SA／SH 三個 controller）緊接著建貸項（靜態讀檔）。"""

    def test_each_plan_change_refund_records_a_credit_note(self):
        for rel in ('paas.py', 'security_access.py', 'smart_home.py'):
            with self.subTest(controller=rel):
                with open(os.path.join(_SRC, 'controllers', rel), encoding='utf-8') as f:
                    source = f.read()
                start = source.find('refund_wallet_points(')
                self.assertGreaterEqual(start, 0)
                window = source[start:start + 900]
                self.assertIn("old_sub._woow_record_refund_credit_note(refund, 'Plan change')",
                              window)
