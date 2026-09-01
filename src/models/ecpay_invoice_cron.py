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
        """Try once to issue an ECPay invoice for `move`, recording outcome.

        🔴 **本方法的任何路徑都不得「沒打 ECPay 卻把狀態寫成 ``issued``（或
        stamp ``invoice_issued_at``）」**。這裡原本有一段 OR 短路
        （``uniform_state == 'invoiced' or ecpay_invoice_id``）會直接寫
        ``issued`` 就 return——它同時吃掉兩種完全不同的情況：

        * 真的已核發（有號碼）⇒ 把它標成 issued 看似對，但那是**未經查證的
          自我宣稱**，而且會把「作廢／折讓後又被送進來」也一起吞掉；
        * 只有號碼為空的**失敗殘值** ⇒ 標成 issued 是**靜默假成功**：對帳報表
          全綠、客戶手上一張發票都沒有。

        改成與 ``account.move._woow_ecpay_issuance_confirmed()`` **共用同一顆
        判準**（⛔ 不再有 OR／AND 兩把尺的差集）：

        * 有真號碼 ⇒ ``_record_failure()`` fail-loud（外呼 0 次、⛔ 不 stamp
          issued），重試上限後轉 ``error`` 並通知帳務管理群組，由人處理；
        * 沒有真號碼但掛著殘值連結 ⇒ 先解除連結再開票，否則上游
          ``create_ecpay_invoice()`` 自己的重複守衛
          （``uniform_state == 'invoiced' or ecpay_invoice_id``）會把**合法的
          retry 擋死**。

        ⚠ 還有第三格：``uniform_state == 'invoiced'`` **卻沒有號碼**（資料不
        一致）。本判準刻意回 ``False``——沒有號碼就不能宣稱「已核發」。它會往下
        走進上游，被上游那道重複守衛 raise，落到 ``_record_failure()``：外呼 0
        次、⛔ 不 stamp issued、原因寫進 ``woow_invoice_last_error`` 由人處理。
        移除 OR 短路之後上游守衛才**真的可達**（在那之前它永遠碰不到），
        由 ``test_ecpay_invoice_retry`` 的
        ``test_already_invoiced_without_number_is_refused_not_stamped`` 覆蓋。
        """
        if move._woow_ecpay_issuance_confirmed():
            self._record_failure(
                move,
                "ECPay invoice number %s is already issued for this move; "
                "refusing to re-issue (needs manual reconciliation)."
                % move.ecpay_invoice_id.name,
            )
            return

        if move.ecpay_invoice_id:
            # 號碼為空的殘值：不是發票，只是失敗留下的空殼。留著它的話上游的
            # 重複守衛會擋掉真正的 retry，所以先解除連結（⛔ 不刪記錄，保留
            # 稽核痕跡）。
            _logger.warning(
                "[ECPAY_INV] move=%s detaching numberless residual "
                "uniform.invoice=%s before re-issuing",
                move.id, move.ecpay_invoice_id.id,
            )
            move.write({'ecpay_invoice_id': False})

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
        summary = _("ECPay e-invoice issuance failed (retry limit reached)")
        note = _(
            "Issuing the ECPay e-invoice for invoice %(name)s failed "
            "%(n)s times in a row.\n"
            "Last error: %(err)s"
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
