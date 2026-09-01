import logging
import math
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


# Stable failure codes returned by `charge_via_token`. Mirrors the constants
# in sh_subscription_fix.py so callers can switch between the two charge paths
# without relearning the vocabulary.
TOPUP_OK = 'ok'
TOPUP_INVALID_STATE = 'invalid_state'
TOPUP_NO_TOKEN = 'no_token'
TOPUP_PROVIDER_NOT_ENABLED = 'provider_not_enabled'
TOPUP_CURRENCY_NOT_CONFIGURED = 'currency_not_configured'
TOPUP_3DS_REQUIRED = '3ds_required'
TOPUP_SDK_ERROR = 'sdk_error'
TOPUP_WALLET_CREDIT_FAILED = 'wallet_credit_failed'


class WalletTopup(models.Model):
    _name = 'woow.wallet.topup'
    _description = 'Wallet Top-up Order'
    _order = 'create_date desc, id desc'

    partner_id = fields.Many2one('res.partner', required=True, index=True, ondelete='restrict')
    plan_id = fields.Many2one('woow_paas_platform.topup_plan', ondelete='restrict')

    amount = fields.Monetary(required=True)
    credits = fields.Float(required=True)
    bonus = fields.Float(default=0)
    currency_id = fields.Many2one('res.currency', required=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('pending', 'Pending'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, index=True)

    source = fields.Selection([
        ('manual', 'Manual (user-initiated)'),
        ('auto', 'Auto (system-triggered shortfall)'),
    ], default='manual', required=True, index=True,
       help='manual = user clicked topup; auto = system triggered to cover '
            'subscription invoice shortfall.')

    payment_transaction_id = fields.Many2one('payment.transaction', ondelete='restrict', index=True)
    wallet_transaction_id = fields.Many2one('wallet.transaction', readonly=True)

    failure_code = fields.Char()
    failure_message = fields.Char()

    invoice_carrier_type = fields.Selection([
        ('email', 'Email carrier'),
        ('mobile', 'Mobile barcode'),
        ('citizen', 'Citizen digital cert'),
        ('triplicate', 'Triplicate (B2B)'),
        ('paper', 'Paper'),
        ('donation', 'Donation'),
    ], default='email')
    invoice_carrier = fields.Char()
    invoice_tax_id = fields.Char()
    invoice_title = fields.Char()
    invoice_lovecode = fields.Char()
    invoice_account_move_id = fields.Many2one('account.move', readonly=True)
    invoice_issued_at = fields.Datetime(readonly=True)

    _FAILURE_KEYWORDS = (
        ('card_declined', ('declined', 'decline', '10100248')),
        ('limit_exceeded', ('limit', 'insufficient', '10100058')),
        ('duplicate_transaction', ('duplicate', '10100073')),
    )

    @api.model
    def _classify_failure(self, message):
        """Map a free-text ECPay error message to a stable internal code."""
        if not message:
            return 'generic'
        low = message.lower()
        for code, keywords in self._FAILURE_KEYWORDS:
            if any(k in low for k in keywords):
                return code
        return 'generic'

    @api.model
    def create_auto_topup(self, partner, amount):
        """Factory: create a draft auto-topup for the given shortfall.

        System-triggered topup (no fixed plan, no bonus, currency=TWD).
        Used by Phase 4.3's deprecation shim and the future active auto-topup
        cron (tracked in maestri 'pending' note).

        Args:
            partner: res.partner record
            amount: positive float, the shortfall to cover (TWD)

        Returns:
            woow.wallet.topup record (state='draft', source='auto')

        Raises:
            ValidationError: if amount <= 0

        Note: amount is ceil()'d to integer TWD so the wallet credit, the
        Odoo invoice price_unit, and the ECPay charge all reconcile to the
        same value. The fractional surplus (max 0.99 TWD) accrues to the
        user's wallet for future use.
        """
        if amount <= 0:
            raise ValidationError(
                _("Auto-topup amount must be positive (got %s)") % amount
            )

        # Ceil to integer TWD: ECPay requires positive integer charge amount,
        # and wallet credit / invoice line price must match the actual charge
        # to keep the customer's invoice + card statement + wallet ledger
        # reconcilable. Surplus stays in the wallet.
        ceil_amount = float(math.ceil(amount))

        twd = self.env.ref('base.TWD', raise_if_not_found=True)
        vals = {
            'partner_id': partner.id,
            'amount': ceil_amount,
            'credits': ceil_amount,
            'bonus': 0.0,
            'currency_id': twd.id,
            'source': 'auto',
            'state': 'draft',
        }
        # Inherit partner's invoice preference (mirrors manual topup flow).
        # Guard on the actual partner-side field we read — both the partner
        # extension and this topup model ship together, so the gate matches
        # the real precondition rather than checking for an unrelated method.
        if 'woow_invoice_carrier_type' in partner._fields:
            for src, dst in (
                ('woow_invoice_carrier_type', 'invoice_carrier_type'),
                ('woow_invoice_carrier', 'invoice_carrier'),
                ('woow_invoice_tax_id', 'invoice_tax_id'),
                ('woow_invoice_title', 'invoice_title'),
                ('woow_invoice_lovecode', 'invoice_lovecode'),
            ):
                if src in partner._fields:
                    val = partner[src]
                    if val:
                        vals[dst] = val
        return self.sudo().create(vals)

    def charge_via_token(self, token):
        """Synchronously charge `token` for self.amount, credit wallet, create invoice.

        Args:
            token: payment.token record (must be active, ECPG-bound)

        Returns:
            (ok: bool, message: str). Side effects:
            - state transitions: draft -> pending -> done/failed
            - on done: wallet credited, invoice created (woow_skip_upstream_hook=True)
            - on 3DS required: state stays 'pending' (cron can't drive 3DS)

        Return codes (message):
            'ok'                          - charge succeeded
            'invalid_state'               - topup not in 'draft' state (concurrent/replay)
            'no_token'                    - token missing/inactive/no ecpg_bindcard_id
            'provider_not_enabled'        - token's provider has no enable_ecpg
            'currency_not_configured'    - neither provider nor TWD available
            '3ds_required'                - ECPG returned ThreeDURL; cron can't complete
            'sdk_error'                   - exception from _send_payment_request
            'wallet_credit_failed'        - ECPay charged but wallet credit failed (CRITICAL)
            '<state>'                     - tx ended in non-done state (state name returned)
        """
        self.ensure_one()
        # Defense against concurrent callers (cron + webhook + manual button):
        # only a draft topup is chargeable. Once another caller flips to 'pending',
        # this caller should bail rather than create a second payment.transaction.
        if self.state != 'draft':
            _logger.info(
                '[TOPUP_AUTO] charge_via_token aborted: state=%s topup=%s',
                self.state, self.id,
            )
            return False, TOPUP_INVALID_STATE
        if not token or not token.active or not token.ecpg_bindcard_id:
            return False, TOPUP_NO_TOKEN

        provider = token.provider_id
        if not provider or not getattr(provider, 'enable_ecpg', False):
            return False, TOPUP_PROVIDER_NOT_ENABLED

        currency = provider.main_currency_id or self.env.ref(
            'base.TWD', raise_if_not_found=False,
        )
        if not currency:
            return False, TOPUP_CURRENCY_NOT_CONFIGURED

        self.write({'state': 'pending'})
        charge_amount = int(math.ceil(self.amount))
        # 20-char limit on ECPay reference; UUID hex keeps unique portion first
        # so ecpay's _ecpg_build_merchant_trade_no truncation preserves uniqueness.
        reference = f'TOPUP{uuid.uuid4().hex[:15].upper()}'

        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': provider.id,
            'payment_method_id': token.payment_method_id.id,
            'reference': reference,
            'amount': float(charge_amount),
            'currency_id': currency.id,
            'partner_id': self.partner_id.id,
            'token_id': token.id,
            'operation': 'offline',
            # Link the tx back to this topup so the _set_done callback in
            # payment_transaction.py can credit the wallet if the user later
            # completes 3DS via the web flow (without waiting for the cleanup
            # cron to reconcile).
            'woow_topup_id': self.id,
        })
        self.write({'payment_transaction_id': tx.id})
        _logger.info(
            "[TOPUP_AUTO] tx created topup=%s tx=%s ref=%s amount=%s token=%s",
            self.id, tx.id, reference, charge_amount, token.id,
        )

        try:
            result = tx._send_payment_request()
        except Exception as e:
            if tx.state == 'done':
                # Inner callback (_set_done → _on_payment_done) raised AFTER ECPay
                # accepted the charge. Card was actually charged; route to CRITICAL
                # path so ops gets the manual-reconciliation alert with full context.
                _logger.exception(
                    'CRITICAL: auto-topup tx _set_done() ran (ECPay charged tx=%s '
                    'amount=%.2f) but inner callback raised; manual reconciliation '
                    'required (topup=%s, partner=%s, partner_email=%s)',
                    tx.reference, charge_amount, self.id, self.partner_id.id,
                    self.partner_id.email or '(none)',
                )
                # #864 PR-E 順序：incident 先落地（獨立交易，即使自己失敗也
                # fail-soft 不拋），下面那個 `state='failed'` 標記才是保底。
                self._log_charged_not_credited_incident(
                    tx, token, charge_amount, currency,
                )
                self.write({
                    'state': 'failed',
                    'failure_code': TOPUP_WALLET_CREDIT_FAILED,
                    'failure_message': f"CHARGED_NOT_CREDITED: {str(e)[:400]}",
                })
                return False, TOPUP_WALLET_CREDIT_FAILED
            # Real SDK error (ECPay never accepted) — safe to retry
            _logger.exception(
                '[TOPUP_AUTO] send_payment_request failed topup=%s', self.id,
            )
            self.write({
                'state': 'failed',
                'failure_code': TOPUP_SDK_ERROR,
                'failure_message': str(e)[:500],
            })
            return False, TOPUP_SDK_ERROR

        # 3DS - keep state pending, don't fail. User may complete via web later.
        if isinstance(result, dict) and result.get('redirect_url'):
            _logger.info(
                '[TOPUP_AUTO] 3DS required topup=%s tx=%s; aborting cron path',
                self.id, tx.id,
            )
            return False, TOPUP_3DS_REQUIRED

        if tx.state != 'done':
            state_msg = tx.state_message or ''
            _logger.warning(
                '[TOPUP_AUTO] tx not done topup=%s tx=%s state=%s msg=%r',
                self.id, tx.id, tx.state, state_msg[:200],
            )
            classified = self._classify_failure(state_msg)
            self.write({
                'state': 'failed',
                # Prefer classified code (e.g. 'card_declined') over raw tx.state
                # when ECPay error message gives us a hint
                'failure_code': classified if classified != 'generic' else tx.state,
                'failure_message': state_msg[:500],
            })
            return False, tx.state

        # ECPay has charged the card. Subsequent failure during _on_payment_done
        # must NOT propagate - otherwise the outer transaction rolls back and the
        # next caller would charge AGAIN. Catch + log critical.
        try:
            self._on_payment_done(tx)
        except Exception as e:
            _logger.exception(
                'CRITICAL: auto-topup charged ECPay (tx=%s amount=%.2f currency=%s) '
                'but post-charge bookkeeping failed; manual reconciliation required '
                '(topup=%s, partner=%s, partner_email=%s)',
                tx.reference, charge_amount, self.currency_id.name,
                self.id, self.partner_id.id,
                self.partner_id.email or '(none)',
            )
            # #864 PR-E 順序：incident 先落地（獨立交易，即使自己失敗也
            # fail-soft 不拋），下面那個 `state='failed'` 標記才是保底。
            self._log_charged_not_credited_incident(
                tx, token, charge_amount, currency,
            )
            self.write({
                'state': 'failed',
                'failure_code': TOPUP_WALLET_CREDIT_FAILED,
                # Prefix lets ops filter/search dashboards for "card was charged
                # but bookkeeping failed" — distinct from genuine "did not charge"
                # failures.
                'failure_message': f"CHARGED_NOT_CREDITED: {str(e)[:400]}",
            })
            return False, TOPUP_WALLET_CREDIT_FAILED

        return True, TOPUP_OK

    def _log_charged_not_credited_incident(self, tx, token, charge_amount, currency):
        """獨立交易落一筆 CHARGED_NOT_CREDITED incident（#864 PR-D／PR-E）。

        兩個呼叫端都是「ECPay 已確認扣款（``tx.state == 'done'``），但入帳／
        記帳後續處理失敗」——這正是 double-charge 冷卻閘（見
        ``sh_subscription_fix.py`` 的 ``_double_charge_cooldown_active``）
        要讀的那個訊號。

        🔴 順序：呼叫端必須在 ``self.write({'state': 'failed', ...})``
        **之前**呼叫這個 method——``woow.billing.incident._log()`` 用獨立
        cursor commit，即使它自己失敗也只 fail-soft 記 log、不往上拋，這樣
        原本那個 ``failed`` 標記仍是保底，兩者不互相取代。

        snapshot 只放 allowlisted 純量／代碼：``detail`` 刻意寫死通用句子，
        不帶 ``str(exc)`` 原始內容——ECPay 例外訊息可能夾帶 PAN-like 內容或
        HTML 片段，``woow.billing.incident._validate_log_snapshot`` 對這兩者
        是 fail-soft 拒收**整筆**（見 ``billing_incident.py`` 模組
        docstring）；與其賭一次 regex 不如從源頭不放。原始例外仍完整留在
        ``self.failure_message``（未被本 method 動到）。同理刻意不帶
        ``partner_email``——沿用 ``account_move_invoice_status.py`` 既有
        的教訓：email 形狀驗證失敗會讓整筆 snapshot 被拒收、告警靜默消失，
        識別走 ``partner_ref`` / ``source_ref`` 即可。

        🔴 這個 method 本身也是「incident 寫入」的一部分，必須自己包
        try/except（抄 ``cf_access_policy_reap.py`` 的形狀）：``_log()`` 的
        contract 是「內部已吞掉所有例外、永不外拋」，但這個 method 在呼叫
        ``_log()`` 之前還有一段組 snapshot dict 的 Python 邏輯——這段本身若
        炸掉（如未來重構誤觸型別錯誤），⛔ 不能讓例外反過來連累呼叫端
        `charge_via_token` 那個保底的 `state='failed'` 標記。失敗只
        log critical，不外拋。
        """
        try:
            incident_id = self.env['woow.billing.incident']._log({
                'external_ref': tx.reference,
                'kind': 'charged_not_credited',
                'partner_ref': self.partner_id.id or None,
                'payment_token_ref': token.id if token else None,
                'amount': float(charge_amount),
                'credits': self.credits,
                'bonus': self.bonus,
                'currency': currency.name if currency else None,
                'trade_no': tx.reference,
                'failure_code': TOPUP_WALLET_CREDIT_FAILED,
                'source_model': self._name,
                'source_ref': self.id,
                'detail': (
                    'ECPay 已接受扣款（tx.state=done）但入帳／記帳後續處理'
                    '失敗；原始例外請查 woow.wallet.topup.failure_message '
                    '與伺服器 log。'
                ),
            })
            _logger.info(
                '[TOPUP_AUTO] charged_not_credited incident logged topup=%s '
                'tx=%s incident_id=%s',
                self.id, tx.id, incident_id,
            )
        except Exception:
            _logger.critical(
                '[TOPUP_AUTO] CRITICAL: charged_not_credited incident '
                'logging itself failed (topup=%s tx=%s) — the charge has '
                'already happened; only the failed marker below now stands '
                'as evidence',
                self.id, tx.id,
            )

    def _on_payment_done(self, tx):
        """Credit the wallet when the linked payment transaction succeeds.

        Idempotent: replaying with the same tx is a no-op.
        """
        self.ensure_one()
        _logger.info(
            "[TOPUP] _on_payment_done entry topup=%s state=%s tx=%s tx_state=%s",
            self.id, self.state, tx.id, tx.state,
        )
        if self.state == 'done' or self.wallet_transaction_id:
            _logger.info(
                "[TOPUP] _on_payment_done skip already done topup=%s wallet_tx=%s",
                self.id, self.wallet_transaction_id.id if self.wallet_transaction_id else None,
            )
            return
        # Auto-topups have no plan_id (Path B Phase 1 (Task 1.1) made plan_id nullable).
        # Without a guard the description would render "Top-up: False" in the
        # wallet ledger. Distinguish auto vs manual fallback so customer support
        # can tell at a glance which entries came from system-triggered topups.
        if self.plan_id:
            description = f"Top-up: {self.plan_id.name}"
        elif self.source == 'auto':
            description = "Wallet Top-up (auto)"
        else:
            description = "Wallet Top-up"
        total_credits = self.credits + self.bonus
        _logger.info(
            "[TOPUP] crediting wallet topup=%s partner=%s credits=%s+bonus=%s total=%s",
            self.id, self.partner_id.id, self.credits, self.bonus, total_credits,
        )
        self.partner_id.add_wallet_points(
            total_credits,
            description=description,
            payment_transaction=tx,
        )
        # add_wallet_points writes loyalty.card.points, but partner.wallet_balance
        # is a non-stored compute that won't recompute until cache is dropped.
        # When this runs in a synchronous _send_payment_request callback, any
        # subsequent reader in the same request would otherwise see the
        # pre-credit balance.
        self.partner_id.invalidate_recordset(['wallet_balance'])
        wtx = self.env['wallet.transaction'].search([
            ('payment_transaction_id', '=', tx.id),
            ('transaction_type', '=', 'topup'),
        ], limit=1)
        # G2-B: persist the structured breakdown on the transaction so the
        # frontend can show "NT$ 1000 + 100 bonus = 1,100 pts" instead of a
        # collapsed amount. Defensive `if wtx:` mirrors the existing
        # implicit guard at `wtx.id` on the next line.
        if wtx:
            wtx.write({
                'base_amount': self.credits,
                'bonus_amount': self.bonus,
            })
        self.write({'state': 'done', 'wallet_transaction_id': wtx.id})
        _logger.info(
            "[TOPUP] _on_payment_done complete topup=%s wallet_tx=%s new_state=done",
            self.id, wtx.id,
        )
        # Create the customer invoice so the issuing cron can later call
        # ecpay_invoice_tw. Best-effort: a failure here does not roll back the
        # wallet credit that already happened above.
        self._ensure_topup_invoice()

    def _on_payment_failed(self, tx):
        """Record failure details when the linked payment transaction fails."""
        self.ensure_one()
        _logger.info(
            "[TOPUP] _on_payment_failed entry topup=%s state=%s tx=%s tx_state=%s msg=%r",
            self.id, self.state, tx.id, tx.state, (tx.state_message or '')[:200],
        )
        if self.state in ('done', 'failed'):
            _logger.info(
                "[TOPUP] _on_payment_failed skip terminal state topup=%s state=%s",
                self.id, self.state,
            )
            return
        message = tx.state_message or ''
        code = self._classify_failure(message)
        self.write({
            'state': 'failed',
            'failure_message': message,
            'failure_code': code,
        })
        _logger.info(
            "[TOPUP] _on_payment_failed complete topup=%s code=%s",
            self.id, code,
        )

    _CLEANUP_GRACE_MIN = 30
    _CLEANUP_HARD_CANCEL_MIN = 60

    @api.model
    def _cron_cleanup_pending_topups(self):
        """Reconcile pending topups whose callback was missed / dropped.

        - tx already done → credit (catch-up).
        - tx error/cancel → mark topup failed.
        - tx still pending > HARD_CANCEL_MIN → cancel tx + mark topup cancelled.
        """
        now = fields.Datetime.now()
        grace = now - timedelta(minutes=self._CLEANUP_GRACE_MIN)
        hard = now - timedelta(minutes=self._CLEANUP_HARD_CANCEL_MIN)
        stuck = self.sudo().search([
            ('state', '=', 'pending'),
            ('create_date', '<', grace),
        ])
        if stuck:
            _logger.info(
                "[TOPUP] cron cleanup scanning %s stuck pending topups ids=%s",
                len(stuck), stuck.ids,
            )
        for topup in stuck:
            tx = topup.payment_transaction_id
            if not tx:
                _logger.info(
                    "[TOPUP] cron cleanup no tx → cancelled topup=%s",
                    topup.id,
                )
                topup.write({'state': 'cancelled'})
                continue
            if tx.state == 'done':
                _logger.info(
                    "[TOPUP] cron cleanup tx done → catch-up credit topup=%s tx=%s",
                    topup.id, tx.id,
                )
                topup._on_payment_done(tx)
            elif tx.state in ('error', 'cancel'):
                _logger.info(
                    "[TOPUP] cron cleanup tx %s → failed topup=%s tx=%s",
                    tx.state, topup.id, tx.id,
                )
                topup._on_payment_failed(tx)
            elif tx.state in ('draft', 'pending') and topup.create_date < hard:
                _logger.info(
                    "[TOPUP] cron cleanup hard-cancel stuck 3DS topup=%s tx=%s tx_state=%s",
                    topup.id, tx.id, tx.state,
                )
                try:
                    tx._set_canceled("Timed out waiting for 3DS confirmation")
                except Exception:
                    _logger.exception(
                        "[TOPUP] cron cleanup _set_canceled failed topup=%s tx=%s",
                        topup.id, tx.id,
                    )
                topup.write({'state': 'cancelled'})

    def _ensure_topup_invoice(self):
        """Create the customer invoice tied to this topup if not already created.

        Idempotent — safe to call repeatedly. A failure does not propagate so a
        wallet credit that already happened in `_on_payment_done` is never
        rolled back. The retry cron / admin button will pick up any invoice
        that was created but failed to post or to issue an e-invoice.
        """
        self.ensure_one()
        if self.invoice_account_move_id:
            return self.invoice_account_move_id
        try:
            move = self._create_topup_invoice()
        except Exception:
            _logger.exception(
                "CRITICAL: [TOPUP] invoice create failed; wallet may have been "
                "credited but no Odoo invoice / e-invoice will be issued. "
                "topup=%s partner=%s (email=%s) amount=%s source=%s. "
                "Find orphaned topups via: woow.wallet.topup with "
                "state='done' AND invoice_account_move_id=False",
                self.id, self.partner_id.id, self.partner_id.email or '?',
                self.amount, self.source,
            )
            return self.env['account.move']
        self.write({
            'invoice_account_move_id': move.id,
            'invoice_issued_at': False,
        })
        _logger.info("[TOPUP] invoice created topup=%s move=%s", self.id, move.id)
        return move

    def _create_topup_invoice(self):
        """Build and post an out_invoice mirroring this topup, ready for ECPay
        e-invoice issuance by the woow_invoice_issue_state cron."""
        self.ensure_one()
        product = self._get_topup_product()
        line_name = f"Wallet Topup: {self.plan_id.name}" if self.plan_id else "Wallet Topup"
        # #864 PR-C（裁決 ④）：開票政策由 res.company.auto_invoice 決定，與訂閱
        # 家族共用同一顆 fail-closed resolver。原本這裡寫死 'pending' ⇒ 平台的
        # async cron（完全不看 auto_invoice，只要看到 pending 就開）會無條件自動
        # 開票，「paas 選 manual」根本不生效。
        issue_state = self.env['account.move']._woow_resolve_invoice_issue_state(
            self.env.company)
        move_vals = {
            'move_type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'invoice_origin': f'TOPUP-{self.id}',
            'invoice_date': fields.Date.context_today(self),
            'currency_id': self.currency_id.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': line_name,
                'quantity': 1.0,
                'price_unit': self.amount,
            })],
            'woow_invoice_issue_state': issue_state,
            # Path B: PaaS issues e-invoices via cron only. The skip flag
            # short-circuits the upstream `create_ecpay_invoice` action_post
            # hook so the same move isn't issued twice (once inline, once by
            # the cron). Required for both manual and auto topups.
            'woow_skip_upstream_hook': True,
        }
        move_vals.update(self._map_carrier_to_ecpay_vals())
        move = self.env['account.move'].sudo().create(move_vals)
        try:
            move.action_post()
        except Exception:
            # Leave the move in draft for a human / cron to recover.
            # ⚠ #864 PR-C 更正：這裡原本寫「issue state 維持 'pending' 所以開票
            # cron 不會撿」——那句話現在是錯的：政策為 manual 時 stamp 出來的就是
            # 'manual'，而 automatic 時才是 'pending'，兩者都不是這裡的理由。
            # cron 不撿它的**真正原因是這張 move 還停在 draft**：
            # `_cron_issue_pending_invoices` 的 domain 帶 ('state', '=', 'posted')。
            # 守線見 tests/test_invoice_issue_policy_864.py 的
            # TestCronIgnoresDraftMoves864（含 posted+pending 陽性對照）。
            _logger.exception(
                "CRITICAL: topup invoice action_post failed; wallet was credited "
                "(or pending credit) but no e-invoice will be issued without "
                "manual recovery. topup=%s move=%s partner=%s amount=%s",
                self.id, move.id, self.partner_id.id, self.amount,
            )
            # Surface via mail.activity so accounting managers see it in their
            # inbox. Reuse the cron's notify helper (logs + creates activity for
            # account.group_account_manager users).
            try:
                self.env['woow_paas_platform.ecpay_invoice_cron']._notify_admin_on_error(
                    move,
                    "Topup invoice action_post failed; manual review required. "
                    "Wallet credit may have happened — check topup record before "
                    "re-posting.",
                )
            except Exception:
                # Best-effort notification — don't let activity creation failure
                # mask the original action_post failure.
                _logger.exception(
                    '[TOPUP] mail.activity creation failed for stuck draft move=%s',
                    move.id,
                )
        return move

    @api.model
    def _get_topup_product(self):
        """Lazily fetch (or create) the placeholder product used on topup
        invoice lines. A single shared product is fine — the line description
        carries the per-topup detail."""
        Product = self.env['product.product'].sudo()
        product = Product.search([('default_code', '=', 'WALLET_TOPUP')], limit=1)
        if not product:
            product = Product.create({
                'name': 'Wallet Topup',
                'default_code': 'WALLET_TOPUP',
                'type': 'service',
            })
        return product

    def _map_carrier_to_ecpay_vals(self):
        """Map the PaaS-side `invoice_carrier_type` (and friends) to the field
        combination ecpay_invoice_tw expects on `account.move`.

        ecpay_invoice_tw uses:
          - carrierType: '1' = ECPay carrier (email-based), '2' = Citizen cert,
            '3' = Mobile barcode
          - input_carrier_num: actual carrier number (citizen/mobile)
          - ecpay_CustomerIdentifier: tax_id (B2B 統編)
          - ec_ident_name: invoice title (B2B 抬頭)
          - is_print: paper invoice
          - is_donation + lovecode: donation
        """
        self.ensure_one()
        vals = {}
        ct = self.invoice_carrier_type
        if ct == 'email':
            vals['carrierType'] = '1'
        elif ct == 'mobile':
            vals.update({
                'carrierType': '3',
                'input_carrier_num': self.invoice_carrier or '',
            })
        elif ct == 'citizen':
            vals.update({
                'carrierType': '2',
                'input_carrier_num': self.invoice_carrier or '',
            })
        elif ct == 'triplicate':
            vals.update({
                'ecpay_CustomerIdentifier': self.invoice_tax_id or '',
                'ec_ident_name': self.invoice_title or self.partner_id.name or '',
            })
        elif ct == 'paper':
            vals['is_print'] = True
        elif ct == 'donation':
            vals.update({
                'is_donation': True,
                'lovecode': self.invoice_lovecode or '',
            })
        return vals
