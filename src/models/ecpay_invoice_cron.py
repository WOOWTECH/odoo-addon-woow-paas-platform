import logging
from datetime import timedelta

from odoo import _, api, fields, models

from .account_move import _forced_ecpay_issue

_logger = logging.getLogger(__name__)


class EcpayInvoiceCron(models.AbstractModel):
    """Asynchronous ECPay e-invoice issuing.

    Why a cron rather than ecpay_invoice_tw's inline `action_post()` hook:
    that hook raises `UserError` on ECPay failure, rolling back the entire
    `_pay_from_wallet` transaction (including the wallet deduction). Issuing
    asynchronously keeps payment / reconcile durable and lets us retry.

    Driver:
      - Selects posted out_invoice rows where `woow_invoice_issue_state` is
        `pending` (never tried) or `failed` with `next_retry_at <= now`.
      - Calls `move.create_ecpay_invoice()` (provided by ecpay_invoice_tw).
      - Tracks retries on the move; after 3 failures marks `error` and
        creates a `mail.activity` for `account.group_account_manager`.
    """
    _name = 'woow_paas_platform.ecpay_invoice_cron'
    _description = 'ECPay E-Invoice Issuing Cron'

    _RETRY_BACKOFF_MIN = (5, 30, 120)
    _MAX_RETRIES = 3
    _BATCH_SIZE = 50

    @api.model
    def _cron_issue_pending_invoices(self):
        AccountMove = self.env['account.move'].sudo()
        now = fields.Datetime.now()

        candidates = AccountMove.search(
            [
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'),
                '|',
                ('woow_invoice_issue_state', '=', 'pending'),
                '&',
                ('woow_invoice_issue_state', '=', 'failed'),
                ('woow_invoice_next_retry_at', '<=', now),
            ],
            limit=self._BATCH_SIZE,
            order='id asc',
        )
        if not candidates:
            return
        _logger.info(
            "[ECPAY_INV] cron processing %s candidates ids=%s",
            len(candidates), candidates.ids,
        )
        for move in candidates:
            try:
                self._issue_one(move)
            except Exception:
                # Defensive: never let one bad move halt the batch.
                _logger.exception(
                    "[ECPAY_INV] _issue_one crashed move=%s", move.id,
                )

    def _issue_one(self, move):
        """Try once to issue an ECPay invoice for `move`, recording outcome."""
        # Already issued (e.g. another cron tick or the inline trigger
        # succeeded) — sync our tracking field and exit.
        if move.uniform_state == 'invoiced' or move.ecpay_invoice_id:
            move.write({
                'woow_invoice_issue_state': 'issued',
                'woow_invoice_last_error': False,
            })
            self._sync_topup_invoice_issued_at(move)
            return

        move.write({'woow_invoice_issue_state': 'issuing'})
        try:
            # 開閘 bypass Task 2.1 的 short-circuit（PaaS-flagged invoice 只允許
            # 這支 cron 開立，杜絕上游 inline hook）。
            # 🔴 #787：一定要走 helper，不可寫成 `with_context(woow_force_issue=True)`
            #    —— 判定是**值的物件身分**，裸 `True` 從此不開閘（而且會拋例外）。
            _forced_ecpay_issue(move).create_ecpay_invoice()
        except Exception as exc:
            self._record_failure(move, str(exc))
            return

        move.write({
            'woow_invoice_issue_state': 'issued',
            'woow_invoice_last_error': False,
        })
        _logger.info("[ECPAY_INV] issued move=%s", move.id)
        self._sync_topup_invoice_issued_at(move)

    def _record_failure(self, move, error_msg):
        truncated = (error_msg or '')[:1000]
        new_count = move.woow_invoice_retry_count + 1
        if new_count >= self._MAX_RETRIES:
            move.write({
                'woow_invoice_issue_state': 'error',
                'woow_invoice_retry_count': new_count,
                'woow_invoice_last_error': truncated,
                'woow_invoice_next_retry_at': False,
            })
            self._notify_admin_on_error(move, truncated)
            _logger.error(
                "[ECPAY_INV] max retries reached move=%s err=%s",
                move.id, truncated,
            )
            return
        backoff_idx = min(new_count - 1, len(self._RETRY_BACKOFF_MIN) - 1)
        backoff_min = self._RETRY_BACKOFF_MIN[backoff_idx]
        next_retry = fields.Datetime.now() + timedelta(minutes=backoff_min)
        move.write({
            'woow_invoice_issue_state': 'failed',
            'woow_invoice_retry_count': new_count,
            'woow_invoice_last_error': truncated,
            'woow_invoice_next_retry_at': next_retry,
        })
        _logger.warning(
            "[ECPAY_INV] issue failed move=%s retry=%s/%s next=%s err=%s",
            move.id, new_count, self._MAX_RETRIES, next_retry, truncated,
        )

    def _sync_topup_invoice_issued_at(self, move):
        """If this move was created from a wallet topup, stamp its
        invoice_issued_at field so the Billing UI can render the timestamp."""
        topup = self.env['woow.wallet.topup'].sudo().search(
            [('invoice_account_move_id', '=', move.id)], limit=1,
        )
        if topup and not topup.invoice_issued_at:
            topup.write({'invoice_issued_at': fields.Datetime.now()})

    def _notify_admin_on_error(self, move, error_msg):
        Activity = self.env['mail.activity'].sudo()
        todo = self.env.ref(
            'mail.mail_activity_data_todo', raise_if_not_found=False,
        )
        manager_group = self.env.ref(
            'account.group_account_manager', raise_if_not_found=False,
        )
        model = self.env['ir.model']._get('account.move')
        if not (todo and manager_group and model):
            _logger.warning(
                "[ECPAY_INV] cannot create activity for move=%s "
                "(missing refs: todo=%s manager_group=%s model=%s)",
                move.id, bool(todo), bool(manager_group), bool(model),
            )
            return
        summary = _("ECPay 發票開立失敗（已達重試上限）")
        note = _(
            "Invoice %(name)s 開立 ECPay 發票連續失敗 %(n)s 次。\n"
            "最後錯誤：%(err)s"
        ) % {
            'name': move.name or '',
            'n': self._MAX_RETRIES,
            'err': error_msg,
        }
        for user in manager_group.users:
            Activity.create({
                'res_model': 'account.move',
                'res_model_id': model.id,
                'res_id': move.id,
                'activity_type_id': todo.id,
                'summary': summary,
                'note': note,
                'user_id': user.id,
            })
