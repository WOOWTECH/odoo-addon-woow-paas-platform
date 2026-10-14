"""#1555：退點時同步建立貸項（credit note）並以退點沖銷。

2026-10-07 第二階段實測（N8）：已付款的服務刪除時錢包按比例退點，但原發票仍是
posted／paid，沒有貸項、沒有折讓——帳上留著已退款的「已付」發票（收入虛增），之後
若照發票補開電子發票，會開到已退款的交易。換方案的退點（舊訂閱剩餘期間）同一個缺口。

做法（一次退點＝一張貸項）：

1. 找這張訂閱最近一張**已付**的 ``out_invoice``（退點視窗就是它付的那一期，
   ``_prorata_unused_refund``）。
2. 建 ``out_refund``：``reversed_entry_id`` 指回原發票，一行＝原發票那一行的商品、
   科目與**稅**，**貸項總額＝退的點數**（政策 A：點數是含稅金額；內含稅／無稅時單價就是
   退點，外加稅的舊發票單價取未稅比例）。帶 ``woow_skip_upstream_hook``，上游 ``action_post``
   不會在租戶的請求裡 inline 打 ECPay 折讓（見 ``account.move.run_refund`` override）。
3. 以退回錢包的點數建一筆 **outbound** customer payment（與 ``_pay_from_wallet`` 同一個
   journal），和貸項的應收沖銷。

**稅（2026-10-14 政策 A 裁決 #1261）**：標價含稅，退點以本期實付的**含稅**總額按比例計算
（``_woow_refund_cycle_amount``），貸項依原發票稅率沖銷同一個含稅金額 ⇒ 退點全額沖銷
（``payment_state='paid'``），不再留下「稅額那一段未沖」。

電子發票（正式站目前人工開立）：

* 原發票**已開** ECPay 發票（``_woow_ecpay_issuance_confirmed``）⇒ 需要折讓。平台不在
  租戶的刪除請求裡對外呼叫 ECPay（billing-philosophy §5），改為在貸項與原發票留言，並
  替帳務管理群組建待辦，由人工在綠界開立折讓。
* 原發票**未開**且被**全額**沖銷 ⇒ 原發票的 ``woow_invoice_issue_state`` 改為
  ``not_required``（不能再替已全額退款的交易開票），並留言說明。
* 原發票**未開**、部分沖銷 ⇒ 在原發票留言：補開時須扣除已退部分，或開立後再折讓。

失敗語意（post-commit-bookkeeping）：呼叫端（服務刪除、換方案）在這之前已經做完不可
回捲的外部操作或退點；這裡任何失敗都只是**記帳**失敗，``cr.savepoint()`` 包住、記
log、不得翻轉呼叫端的結果（``OperationalError`` 例外：照規則外拋給 ``retrying()``）。
"""
import logging

from psycopg2 import OperationalError

from odoo import SUPERUSER_ID, _, models
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)

#: 原發票還沒開電子發票、可以改成「不需開立」的狀態（``issuing``／``issued`` 不動）。
_UNISSUED_STATES = ('manual', 'pending', 'failed', 'error')


class ShSubscriptionRefundCreditNote(models.Model):
    _inherit = 'sh.subscription.subscription'

    def _woow_refunded_invoice(self):
        """這張訂閱最近一張已付款的 out_invoice（退點對應的那一期）；沒有回空 recordset。"""
        self.ensure_one()
        return self.env['account.move'].sudo().search([
            ('sh_subscription_id', '=', self.id),
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('payment_state', 'in', ('paid', 'in_payment')),
        ], order='invoice_date desc, id desc', limit=1)

    def _woow_record_refund_credit_note(self, amount, reason):
        """退點 ``amount``（含稅點數，政策 A）之後呼叫：建貸項並以退點沖銷；回傳貸項或 False。

        只做記帳，失敗不外拋（見模組 docstring）。``amount <= 0``（試用、從沒付過）時
        什麼都不做。
        """
        self.ensure_one()
        if not amount or amount <= 0:
            return False
        sub = self.sudo()
        try:
            with self.env.cr.savepoint():
                return sub._woow_create_refund_credit_note(amount, reason)
        except OperationalError:
            raise
        except Exception:
            _logger.exception(
                "[#1555] refund of %s points for subscription %s (id=%s, %s) was credited "
                "to the wallet but the credit note could not be recorded; the original "
                "invoice still reads as fully paid — record the credit note manually",
                amount, sub.name, sub.id, reason)
            return False

    def _woow_create_refund_credit_note(self, amount, reason):
        self.ensure_one()
        invoice = self._woow_refunded_invoice()
        if not invoice:
            _logger.warning(
                "[#1555] subscription %s (id=%s): %s points refunded (%s) but no paid "
                "invoice was found to credit", self.name, self.id, amount, reason)
            return False
        invoice = invoice.with_company(invoice.company_id)
        source_lines = invoice.invoice_line_ids.filtered(
            lambda line: line.display_type == 'product')
        if not source_lines:
            _logger.warning(
                "[#1555] invoice %s (id=%s) has no product line to credit", invoice.name,
                invoice.id)
            return False
        source = source_lines[0]
        currency = invoice.currency_id
        # 2026-10-14 政策 A：退的點數＝含稅金額，貸項總額必須等於它。第二道 clamp 對的是原發票
        # **含稅總額**（原本夾在未稅額：含稅價時 300 被夾成 286 再當含稅單價開立，貸項只記 286）。
        refund = min(currency.round(amount), invoice.amount_total)
        if source.tax_ids.filtered(lambda t: not t.price_include) and source.price_total:
            # 外加稅（政策 A 之前的發票）：單價取未稅比例，讓 單價＋稅＝退點。
            price_unit = refund * source.price_subtotal / source.price_total
        else:
            # 內含稅或無稅：單價本身就是含稅金額。
            price_unit = refund
        credit_note = self.env['account.move'].with_company(invoice.company_id).create({
            'move_type': 'out_refund',
            'partner_id': invoice.partner_id.id,
            'journal_id': invoice.journal_id.id,
            'currency_id': currency.id,
            'reversed_entry_id': invoice.id,
            'invoice_origin': invoice.name,
            'ref': _('Refund for %(invoice)s: %(reason)s', invoice=invoice.name, reason=reason),
            'woow_skip_upstream_hook': True,
            'woow_invoice_issue_state': 'not_required',
            'invoice_line_ids': [(0, 0, {
                'product_id': source.product_id.id,
                'name': _('Refund: %(line)s', line=source.name or self.name),
                'account_id': source.account_id.id,
                'quantity': 1,
                'price_unit': price_unit,
                'tax_ids': [(6, 0, source.tax_ids.ids)],
            })],
        })
        credit_note.action_post()
        self._woow_settle_refund_credit_note(credit_note, refund)
        self._woow_flag_refund_for_einvoice(invoice, credit_note)
        _logger.info(
            "[#1555] subscription %s (id=%s): credit note %s (id=%s) untaxed=%s total=%s "
            "against invoice %s for %s points (%s); payment_state=%s",
            self.name, self.id, credit_note.name, credit_note.id, credit_note.amount_untaxed,
            credit_note.amount_total, invoice.name, amount, reason, credit_note.payment_state)
        return credit_note

    def _woow_settle_refund_credit_note(self, credit_note, amount):
        """以退回錢包的點數沖銷貸項：outbound customer payment ＋ 對沖應收。

        journal 與 ``account.move._pay_from_wallet`` 相同（訂閱付款 journal，沒有就退到
        第一個 bank／cash）：點數付款進帳時用它，退回時也用它，兩邊對得起來。
        """
        company = credit_note.company_id.sudo()
        journal = company.sh_paid_subscription_journal or self.env['account.journal'].sudo().search(
            [('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))], limit=1)
        if not journal:
            _logger.warning(
                "[#1555] no payment journal for company %s; credit note %s stays open",
                company.id, credit_note.name)
            return False
        currency = credit_note.currency_id
        pay_amount = min(currency.round(amount), credit_note.amount_residual)
        if float_is_zero(pay_amount, precision_rounding=currency.rounding):
            return False
        method_line = journal.outbound_payment_method_line_ids[:1]
        payment = self.env['account.payment'].sudo().with_company(company).create({
            'journal_id': journal.id,
            'amount': pay_amount,
            'payment_type': 'outbound',
            'partner_type': 'customer',
            'partner_id': credit_note.partner_id.id,
            'payment_method_line_id': method_line.id if method_line else False,
            'memo': _('Wallet refund: %(credit_note)s', credit_note=credit_note.name),
        })
        payment.action_post()
        lines = (credit_note.line_ids | payment.move_id.line_ids).filtered(
            lambda line: line.account_id.account_type == 'asset_receivable'
            and not line.reconciled)
        if lines:
            lines.reconcile()
        credit_note.invalidate_recordset(['payment_state', 'amount_residual'])
        return payment

    @staticmethod
    def _woow_internal_note(record, body):
        """內部備註（不對 portal 顯示）、以 OdooBot 署名——呼叫端常是租戶自己的刪除請求，
        不能把租戶記成帳務文件上的留言者（也不讓他變成 follower）。"""
        record.with_user(SUPERUSER_ID).message_post(
            body=body, subtype_xmlid='mail.mt_note')

    def _woow_flag_refund_for_einvoice(self, invoice, credit_note):
        """電子發票的後續處理標示（見模組 docstring）。"""
        currency = invoice.currency_id
        full = float_compare(
            credit_note.amount_total, invoice.amount_total,
            precision_rounding=currency.rounding) >= 0
        amount_label = '%s %s' % (credit_note.amount_total, currency.name or '')
        if invoice._woow_ecpay_issuance_confirmed():
            note = _(
                'E-invoice %(number)s was already issued for %(invoice)s. Credit note '
                '%(credit_note)s (%(amount)s) needs an ECPay allowance issued manually.',
                number=invoice.ecpay_invoice_id.name, invoice=invoice.name,
                credit_note=credit_note.name, amount=amount_label)
            self._woow_internal_note(credit_note, note)
            self._woow_internal_note(invoice, note)
            self._woow_notify_allowance_needed(credit_note, note)
        elif full and invoice.woow_invoice_issue_state in _UNISSUED_STATES:
            invoice.write({'woow_invoice_issue_state': 'not_required'})
            self._woow_internal_note(invoice, _(
                'Fully refunded by credit note %(credit_note)s before an e-invoice was '
                'issued; no e-invoice is required for this invoice any more.',
                credit_note=credit_note.name))
        elif invoice.woow_invoice_issue_state in _UNISSUED_STATES:
            self._woow_internal_note(invoice, _(
                'Partly refunded by credit note %(credit_note)s (%(amount)s) before an '
                'e-invoice was issued. When issuing the e-invoice, deduct the refunded '
                'part or issue an allowance for it afterwards.',
                credit_note=credit_note.name, amount=amount_label))

    def _woow_notify_allowance_needed(self, credit_note, note):
        """替帳務管理群組建待辦（與 ``ecpay_invoice_cron._notify_admin_on_error`` 同形）。"""
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        managers = self.env.ref('account.group_account_manager', raise_if_not_found=False)
        model = self.env['ir.model']._get('account.move')
        if not (todo and managers and model):
            _logger.warning(
                "[#1555] cannot create the allowance activity for credit note %s",
                credit_note.name)
            return
        Activity = self.env['mail.activity'].sudo()
        for user in managers.users:
            Activity.create({
                'res_model': 'account.move',
                'res_model_id': model.id,
                'res_id': credit_note.id,
                'activity_type_id': todo.id,
                'summary': _('ECPay allowance needed'),
                'note': note,
                'user_id': user.id,
            })
