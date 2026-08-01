import base64
import logging

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.misc import formatLang

_logger = logging.getLogger(__name__)


class ShSubscriptionFix(models.Model):
    """Fix upstream sh_subscription bug in _sh_create_auto_invoice.

    The original method uses `self._generate_invoice_vals()` inside a loop
    over `subscription` records, but since it's an @api.model method, `self`
    is an empty recordset — producing invoice vals with all NULL fields.

    This override replaces `self` with `subscription` so each invoice
    references the correct partner, journal, and product.
    """
    _inherit = 'sh.subscription.subscription'

    def _cycle_delta(self):
        """一個計費週期的 relativedelta（由 sh_unit + sh_recurrency 決定）。

        退點視窗（``_prorata_unused_refund``）與續期推進（``_on_payment_success``）
        共用，避免 unit→delta 對照表在多處漂移。回 ``None``（呼叫端據此保守退
        0 / 不推進）於：sh_recurrency 為 0/未設（否則 cycle_delta 為零長度、
        cycle_days=0，語意無效且會退全額）、或 sh_unit 非 day/week/month/year。
        """
        self.ensure_one()
        if not self.sh_recurrency:
            return None
        return {
            'day': relativedelta(days=self.sh_recurrency),
            'week': relativedelta(weeks=self.sh_recurrency),
            'month': relativedelta(months=self.sh_recurrency),
            'year': relativedelta(years=self.sh_recurrency),
        }.get(self.sh_unit)

    def _has_paid_current_cycle(self):
        """本訂閱是否有過成功扣款（``done`` 的 ``deduct`` 交易）。

        真扣款（``_pay_from_wallet`` → ``deduct_wallet_points``）建的
        wallet.transaction 預設 ``state='done'`` 且 stamp 上 ``subscription_id``
        （cron 路徑帶 subscription；手動付款端點經 ``_pay_from_wallet`` 從發票
        回推 subscription，見 #324）；#324 類「付款靜默失敗」訂閱只有
        ``state='failed'`` 的 deduct。以此區分「真付過」與「從未付過」，堵住
        「付款靜默失敗→刪服務退未賺得點」的套利。

        範圍限制（沿用 #318）：本方法查「本訂閱歷來任一成功扣款」、不做週期
        範圍限定——它只回答「有沒有真付過」，退點**金額**由 ``_prorata_unused_
        refund`` 決定。退點視窗自 #327 起改以 ``sh_date_of_next_payment`` 反推
        當期區間 ``[next_payment − cycle_delta, next_payment)``，已正確涵蓋
        「第 2 個週期起、凍結 sh_end_date 已過」的長壽 in_progress 訂閱（不再
        恆退 0）。本 gate 與視窗計算正交，維持不變。

        NewId（in-memory 記錄）無 persisted id，直接回 False（純公式路徑
        ``_prorata_unused_refund`` 不經此 gate）。
        """
        self.ensure_one()
        if not isinstance(self.id, int):
            return False
        return bool(self.env['wallet.transaction'].sudo().search_count([
            ('subscription_id', '=', self.id),
            ('transaction_type', '=', 'deduct'),
            ('state', '=', 'done'),
        ]))

    def _compute_unused_refund(self):
        """Pro-rata refund for the unused portion of the current paid cycle.

        #324 part (b)：退點前提為「本訂閱有成功扣款」。無成功付款（never-paid
        或僅 failed deduct）→ 退 0，對齊 trial→0 語意，堵住「付款靜默失敗→
        刪服務退未賺得點」的套利。有成功付款才套用 ``_prorata_unused_refund``。
        """
        self.ensure_one()
        if not self._has_paid_current_cycle():
            return 0
        return self._prorata_unused_refund()

    def _prorata_unused_refund(self):
        """純比例退點公式：today 落在「當前已付週期」的剩餘比例（不驗付款）。

        當前已付週期以 ``sh_date_of_next_payment`` 反推（#327）：遞迴計費只推進
        next_payment、永不推進凍結的 ``sh_end_date``，故視窗必須跟隨 next_payment
        才能正確涵蓋 cycle-2+ 的長壽 in_progress 訂閱（否則過期後 clamp 恆退 0）。

        視窗 = ``[next_payment − cycle_delta, next_payment)``；剩餘比例
        = ``(next_payment − today) / cycle_days``，clamp 到 ``[0, cycle_days]`` 使
        單次退點 ∈ ``[0, 一週期 plan 價]``（守則：不跨期回溯多退）。

        Returns 0 when next_payment / start_date 缺失、``today < sh_start_date``
        （trial / 未開始）、或 cycle_delta 無法解析。``sh_start_date`` 僅作 trial
        guard；視窗起點用動態 ``cycle_start``，不再依賴凍結的 ``sh_end_date``，
        故對上游 ``_sh_auto_expired_subscription``（sh_end_date==today→expire）
        零交互影響。
        """
        self.ensure_one()
        next_payment = self.sh_date_of_next_payment
        if not next_payment or not self.sh_start_date:
            return 0
        today = fields.Date.today()
        if today < self.sh_start_date:
            return 0
        cycle_delta = self._cycle_delta()
        if not cycle_delta:
            return 0
        cycle_start = next_payment - cycle_delta
        cycle_days = (next_payment - cycle_start).days or 1
        remaining_days = max(0, min((next_payment - today).days, cycle_days))
        return round(self.sh_plan_price * remaining_days / cycle_days, 2)

    def _settle_on_entity_delete(self, reason='Service deleted'):
        """Cancel this subscription and prorata-refund the unused paid cycle
        to the wallet when the owning entity (CloudService/SmartHome/SA) is
        deleted (issue #318).

        The entity↔subscription FK is ``ondelete='set null'`` (protects only
        the reverse direction), so deleting the entity never touches the sub —
        leaving an ``in_progress`` orphan the billing cron keeps charging.

        Idempotent: only an ``in_progress`` sub is settled. The
        lifecycle-cleanup path (sub already cancelled, cleanup fired after
        ``cleanup_months``) and dunning-cancelled subs are ``!= in_progress``
        → no-op returning ``0.0`` (no double refund). Refund uses the SSOT
        ``_compute_unused_refund`` (trial → 0). Credited as a ``refund``-typed
        wallet transaction so reconciliation can tell it from a top-up.

        Returns the amount actually credited (0.0 when trial / already-settled
        / no partner on the sub — ``sh_partner_id`` is not required upstream).
        The sub is always cancelled to stop billing even when nothing is
        credited.
        """
        self.ensure_one()
        if self.state != 'in_progress':
            return 0.0
        refund = self._compute_unused_refund()
        partner = self.sh_partner_id
        credited = 0.0
        if refund > 0 and partner:
            partner.sudo().refund_wallet_points(
                amount=refund,
                description='%s refund: %s' % (
                    reason, self.sh_subscription_plan_id.name),
                subscription=self,
            )
            partner.invalidate_recordset(['wallet_balance'])
            credited = refund
        self.sudo().write({'state': 'cancel', 'sh_reason': reason})
        return credited

    @api.model
    def _cron_wallet_charge(self):
        """[#324] Neutralize 上游 sh_subscription_wallet 的 daily 扣款 cron。

        上游 ``_cron_wallet_charge`` 有缺陷 B（扣款成功不推進
        ``sh_date_of_next_payment``、且用 ``==`` 等值比對），與本 repo
        dunning-aware 的 ``_sh_create_auto_invoice`` 職責重疊且互相打架。
        本 fix 層（MRO 晚於上游）將其 override 成 no-op，讓 invoice-based
        的 ``_sh_create_auto_invoice`` 成為唯一計費路徑（該路徑為超集：
        錢包扣款 + auto-topup 刷卡 + dunning 收斂）。

        刻意不呼叫 ``super()``——目的就是完全停用上游行為。以 method
        override 而非 flip ``ir.cron.active``，是為了耐上游模組升級
        （升級不會重加此方法，override 恆勝出）。
        """
        _logger.info(
            "[SUB_FIX] _cron_wallet_charge neutralized (#324); "
            "billing handled solely by _sh_create_auto_invoice"
        )
        return

    @api.model
    def _cron_wallet_charge_retry(self):
        """[#324] Neutralize 上游 sh_subscription_wallet 的 hourly 重試 cron。

        上游 ``_cron_wallet_charge_retry`` 有缺陷 A：重試失敗時
        ``_process_wallet_payment`` 會 rollback 後再 create 一筆新的
        ``state='failed'`` 交易（create_date=now），使「近 24h failed」窗
        永遠有剛產生的成員 → 窗永不過期 → 每小時無限重試、failed 列爆量
        （stg 坐實 832 筆）。dunning 重試已由 ``_sh_create_auto_invoice``
        以有限次數（threshold 後 auto-cancel）承擔，故此處 no-op。
        """
        _logger.info(
            "[SUB_FIX] _cron_wallet_charge_retry neutralized (#324); "
            "bounded retry handled by _sh_create_auto_invoice dunning"
        )
        return

    @api.model
    def _sh_create_auto_invoice(self):
        """Daily auto-invoice cron with T19 dunning semantics.

        Behaviour:
        - Filter is ``next_payment <= today`` so a sub whose previous tick
          left ``next_payment`` un-advanced (failed payment) keeps being
          retried until either resolution or auto-cancel.
        - Existing unpaid invoice for the sub is reused; we never stack a
          second invoice on top while the first is outstanding.
        - ``next_payment`` only advances on successful payment (Decision 1).
        - Counter increments on each failed retry; resets on success.
        - At ``threshold`` consecutive failures the sub is auto-cancelled
          and a one-shot cancellation email is queued (Decision 2/4). The
          existing ``subscription_lifecycle_cron`` then suspends K8s
          resources.
        """
        today = fields.Date.today()
        Subscription = self.env['sh.subscription.subscription']
        # Decision 1 follow-through: ``<= today`` (not ``==``) so a stuck
        # next_payment continues to be processed daily.
        subscriptions = Subscription.sudo().search([
            ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '<=', today),
        ])
        threshold = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.max_consecutive_payment_failures', '6'))
        _logger.info(
            "[SUB_FIX] _sh_create_auto_invoice scan due_or_overdue=%s threshold=%s",
            len(subscriptions), threshold,
        )
        for subscription in subscriptions:
            self._process_subscription_billing(subscription, threshold)

    def _process_subscription_billing(self, subscription, threshold):
        """Per-subscription billing for one cron tick. Isolated for testability."""
        today = fields.Date.today()
        partner = subscription.sh_partner_id
        _logger.info(
            "[SUB_FIX] auto_invoice start sub=%s partner=%s price=%s "
            "balance=%s failures=%s",
            subscription.id, partner.id, subscription.sh_plan_price,
            partner.wallet_balance,
            subscription.woow_consecutive_payment_failures,
        )
        try:
            with self.env.cr.savepoint():
                invoice, is_new = self._get_or_create_cycle_invoice(subscription)
                paid = self._try_settle_invoice(subscription, invoice)

                if paid:
                    self._on_payment_success(subscription, invoice, today)
                    if is_new:
                        self._send_invoice_pdf(subscription, invoice)
                else:
                    self._on_payment_failure(
                        subscription, invoice, today, threshold,
                    )
        except Exception:
            _logger.exception(
                'Auto-invoice failed for subscription %s; continuing',
                subscription.name,
            )

    def _get_or_create_cycle_invoice(self, subscription):
        """Return (invoice, is_new). Reuse the outstanding invoice if any."""
        existing = self.env['account.move'].sudo().search([
            ('sh_subscription_id', '=', subscription.id),
            ('payment_state', 'not in', ('paid', 'in_payment')),
            ('state', '!=', 'cancel'),
            ('move_type', '=', 'out_invoice'),
        ], order='invoice_date desc, id desc', limit=1)
        if existing:
            _logger.info(
                "[SUB_FIX] auto_invoice reuse sub=%s invoice=%s amount=%s",
                subscription.id, existing.id, existing.amount_total,
            )
            return existing, False

        invoice_vals = subscription._generate_invoice_vals()
        # Defense in depth: upstream stamps journal_id from
        # company.sh_journal_id, which can be unset on a freshly-onboarded
        # environment (see res.company._ensure_subscription_journal). A False
        # journal_id would hit the NOT NULL constraint and be swallowed by our
        # outer except — a silent billing outage. Fall back to the company sale
        # journal and warn loudly instead of failing invisibly.
        if not invoice_vals.get('journal_id'):
            fallback_journal = self.env['account.journal'].sudo().search([
                ('company_id', '=', subscription.sh_company_id.id),
                ('type', '=', 'sale'),
            ], limit=1)
            if fallback_journal:
                _logger.warning(
                    "[SUB_FIX] company %s has no sh_journal_id; using fallback "
                    "sale journal %s for invoice on sub=%s — configure "
                    "sh_journal_id to silence this.",
                    subscription.sh_company_id.id, fallback_journal.name,
                    subscription.id,
                )
                invoice_vals['journal_id'] = fallback_journal.id
        # Carry the partner's invoice carrier preference into the move so the
        # ECPay issuing cron has everything it needs at post time.
        invoice_vals.update(
            subscription.sh_partner_id._woow_partner_invoice_vals()
        )
        # Path B: cycle invoice 由 wallet 扣款結算（儲值時已開過 ECPay 發票），
        # 不重複開發票。woow_skip_upstream_hook 隔離上游 inline hook
        # (即使 company.auto_invoice='automatic' 也不會被觸發開立)。
        # 詳見 docs/architecture/billing-philosophy.md Principle 3-5。
        invoice_vals['woow_invoice_issue_state'] = 'not_required'
        invoice_vals['woow_skip_upstream_hook'] = True
        invoice = self.env['account.move'].sudo().create(invoice_vals)
        subscription.compute_view_invoice()
        _logger.info(
            "[SUB_FIX] auto_invoice created sub=%s invoice=%s amount=%s",
            subscription.id, invoice.id, invoice.amount_total,
        )
        return invoice, True

    def _try_settle_invoice(self, subscription, invoice):
        """Attempt wallet payment, then auto-topup. Returns True on success."""
        partner = subscription.sh_partner_id
        if partner.wallet_balance >= invoice.amount_total:
            subscription._handle_invoice_creation(invoice)
            return True

        shortfall = invoice.amount_total - partner.wallet_balance
        topup_ok, topup_msg = self._attempt_auto_topup(partner, shortfall)
        if topup_ok:
            _logger.info(
                'Auto-topup covered shortfall=%.2f for subscription %s; '
                'proceeding with invoice %s',
                shortfall, subscription.name, invoice.name,
            )
            subscription._handle_invoice_creation(invoice)
            return True

        _logger.warning(
            "Insufficient wallet balance for subscription %s and "
            "auto-topup failed (%s): balance=%.2f, required=%.2f — "
            "invoice %s left unpaid",
            subscription.name, topup_msg, partner.wallet_balance,
            invoice.amount_total, invoice.name,
        )
        return False

    def _on_payment_success(self, subscription, invoice, today):
        """Advance next_payment, reset dunning counter."""
        delta = subscription._cycle_delta()
        vals = {}
        if delta:
            vals['sh_date_of_next_payment'] = today + delta
        if subscription.woow_consecutive_payment_failures:
            vals['woow_consecutive_payment_failures'] = 0
        if vals:
            subscription.write(vals)
        _logger.info(
            "[SUB_FIX] auto_invoice paid sub=%s invoice=%s next_payment=%s",
            subscription.id, invoice.name,
            vals.get('sh_date_of_next_payment',
                     subscription.sh_date_of_next_payment),
        )

    def _on_payment_failure(self, subscription, invoice, today, threshold):
        """Increment counter, possibly auto-cancel + notify."""
        new_count = subscription.woow_consecutive_payment_failures + 1
        subscription.write({
            'woow_consecutive_payment_failures': new_count,
            'woow_last_payment_failure_at': fields.Datetime.now(),
        })

        # Per-cycle low-balance warning. The marker is stamped **only** when the
        # mail actually reached the queue, and is re-opened when that mail ends
        # up undelivered — see `_low_balance_warning_needs_send` (#389).
        partner = subscription.sh_partner_id
        warn_key = subscription.sh_date_of_next_payment or today
        if self._low_balance_warning_needs_send(subscription, warn_key):
            mail = self._notify_low_balance(partner, invoice)
            if mail:
                subscription.write({
                    'woow_last_low_balance_warned_for': warn_key,
                    'woow_last_low_balance_mail_id': mail.id,
                })

        # Threshold check (Decision 4: hard-cancel; Decision 3: 0 = disabled).
        if threshold and new_count >= threshold:
            _logger.warning(
                "[SUB_FIX] auto_cancel sub=%s after %s consecutive failures",
                subscription.id, new_count,
            )
            subscription.write({'state': 'cancel'})
            if not subscription.woow_cancellation_notified:
                self._notify_subscription_cancelled(subscription, invoice)
                subscription.write({'woow_cancellation_notified': True})
        else:
            _logger.info(
                "[SUB_FIX] auto_invoice failure recorded sub=%s failures=%s/%s",
                subscription.id, new_count, threshold or '∞',
            )

    def _send_invoice_pdf(self, subscription, invoice):
        """Render + email invoice PDF. First-creation only, never on retry."""
        try:
            pdf_content, _content_type = self.env[
                'ir.actions.report'
            ].sudo()._render_qweb_pdf('account.account_invoices', invoice.id)
            attachment = self.env['ir.attachment'].sudo().create({
                'name': 'Subscription Invoice',
                'type': 'binary',
                'datas': base64.b64encode(pdf_content),
                'mimetype': 'application/pdf',
                'res_id': invoice.id,
                'res_model': 'account.move',
            })
            subscription._sh_send_subscription_email(attachment)
            _logger.info(
                "Auto-invoice created for subscription %s (amount=%s, next=%s)",
                subscription.name, invoice.amount_total,
                subscription.sh_date_of_next_payment,
            )
        except Exception:
            _logger.warning(
                "Failed to generate/send invoice PDF for subscription %s",
                subscription.name, exc_info=True,
            )

    @api.model
    def _low_balance_warning_needs_send(self, subscription, warn_key):
        """這個續費週期還需不需要（再）送一次低餘額通知？（#389）

        原本的判準是「``woow_last_low_balance_warned_for`` 有沒有等於 warn_key」
        ——也就是「有沒有**呼叫過**通知函式」。但 ``_notify_low_balance`` 用
        ``force_send=False``，只把信排進佇列；真正的投遞是 mail queue cron，
        失敗的信會停在 ``mail.mail.state == 'exception'``，而 Odoo 的 scheduler
        **只撿 outgoing**——沒有任何人會重試它。於是投遞一失敗（#389 的 prod
        SMTP 全 exception），租戶在整個 ``sh_date_of_next_payment`` 不變的週期
        內就再也收不到提醒。

        改成看**載體的下場**：

        * marker 不是本週期 → 要送（第一次）。
        * 有 marker 但沒有載體 → 不送。Odoo 對 ``auto_delete`` 的信在送出成功後
          會刪掉 mail.mail，所以「查不到載體」的正常成因就是**送成功了**；升級
          前的既有資料（欄位為 NULL）走同一條，上線當天不會對所有欠費租戶補發
          一輪。
        * 載體停在 ``exception`` → 要送（重試）。
        * 載體是 ``outgoing`` / ``sent`` → 不送（去重仍然有效）。

        重送有天然上限：連續失敗到 ``max_consecutive_payment_failures``（預設 6）
        就 auto-cancel，訂閱離開 ``in_progress`` 後 dunning cron 不再撈它。
        """
        if subscription.woow_last_low_balance_warned_for != warn_key:
            return True
        mail = subscription.sudo().woow_last_low_balance_mail_id
        if not mail or not mail.exists():
            return False
        return mail.state == 'exception'

    @api.model
    def _notify_low_balance(self, partner, invoice):
        """Send a low-wallet-balance email to the partner.

        Called when an auto-invoice cannot be paid because wallet balance
        is insufficient.

        Returns:
            ``mail.mail`` recordset — the queued notification, or an **empty**
            recordset when nothing made it into the queue (no template, or
            ``send_mail`` raised). The caller keys the per-cycle dedup marker
            off this: stamping on a failed enqueue is what silenced the whole
            renewal cycle in #389.
        """
        Mail = self.env['mail.mail'].sudo()
        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning("mail_template_low_balance not found; skip notification")
            return Mail

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '')
        amount_text = formatLang(
            self.env, invoice.amount_total, currency_obj=invoice.currency_id,
        )
        balance_text = formatLang(
            self.env, partner.wallet_balance,
            currency_obj=partner.company_id.currency_id,
        )
        ctx = {
            'invoice_name': invoice.name or '',
            'amount_text': amount_text,
            'balance_text': balance_text,
            'topup_url': '%s/woow#/billing/topup' % base_url.rstrip('/'),
        }
        try:
            mail_id = template.with_context(**ctx).send_mail(
                partner.id,
                force_send=False,
                email_layout_xmlid='mail.mail_notification_light',
            )
        except Exception:
            _logger.exception(
                "Failed to queue low-balance notification for partner %s",
                partner.id,
            )
            return Mail
        mail = Mail.browse(mail_id) if mail_id else Mail
        _logger.info(
            "Low-balance notification queued for partner %s mail=%s",
            partner.id, mail.id or None,
        )
        return mail

    @api.model
    def _notify_subscription_cancelled(self, subscription, invoice):
        """Send a one-shot "subscription auto-cancelled" email to the partner.

        Called once when ``woow_consecutive_payment_failures`` crosses the
        configured threshold and the sub flips to ``state='cancel'``. Dedup
        is the caller's responsibility via ``woow_cancellation_notified``.
        """
        template = self.env.ref(
            'woow_paas_platform.mail_template_subscription_cancelled',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning(
                "mail_template_subscription_cancelled not found; skip notification"
            )
            return

        partner = subscription.sh_partner_id
        cleanup_months = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.cleanup_after_months', '6'))
        amount_text = formatLang(
            self.env, invoice.amount_total, currency_obj=invoice.currency_id,
        )
        ctx = {
            'subscription_name': subscription.name or '',
            'plan_name': (
                subscription.sh_subscription_plan_id.name
                if subscription.sh_subscription_plan_id else ''
            ),
            'invoice_name': invoice.name or '',
            'amount_text': amount_text,
            'failures': subscription.woow_consecutive_payment_failures,
            'preservation_months': cleanup_months,
        }
        try:
            template.with_context(**ctx).send_mail(
                partner.id,
                force_send=False,
                email_layout_xmlid='mail.mail_notification_light',
            )
            _logger.info(
                "[SUB_FIX] cancellation email queued partner=%s sub=%s",
                partner.id, subscription.id,
            )
        except Exception:
            _logger.exception(
                "Failed to queue cancellation email for partner %s sub=%s",
                partner.id, subscription.id,
            )

    def _handle_invoice_creation(self, invoice_id):
        """Override upstream: when wallet can cover, post + pay from wallet.

        Upstream's two modes (``sh_invoice_generated='post'`` posts only,
        ``='paid'`` posts + creates a dummy inbound payment) both leave the
        wallet untouched. For our platform the wallet IS the payment source,
        so we bypass both modes and call ``account.move._pay_from_wallet``
        when the balance can cover — decrementing the wallet, creating a
        real ``account.payment``, and reconciling.

        Defensive ``super()`` fallback handles the (should-not-happen) case
        where the caller didn't pre-check balance; the normal cron path via
        ``_sh_create_auto_invoice`` guarantees sufficiency before this runs.
        """
        partner = self.sh_partner_id
        amount = invoice_id.amount_total or 0
        _logger.info(
            "[SUB_FIX] _handle_invoice_creation sub=%s invoice=%s amount=%s partner=%s balance=%s",
            self.id, invoice_id.id, amount,
            partner.id if partner else None,
            partner.wallet_balance if partner else None,
        )
        if partner and amount > 0 and partner.wallet_balance >= amount:
            _logger.info(
                "[SUB_FIX] _handle_invoice_creation pay from wallet sub=%s invoice=%s",
                self.id, invoice_id.id,
            )
            invoice_id._pay_from_wallet(subscription=self)
            return
        _logger.info(
            "[SUB_FIX] _handle_invoice_creation fallback super sub=%s invoice=%s",
            self.id, invoice_id.id,
        )
        return super()._handle_invoice_creation(invoice_id)

    @api.model
    def _attempt_auto_topup(self, partner, shortfall):
        """[DEPRECATED] Thin wrapper around wallet.topup.charge_via_token.

        Kept for backwards compatibility with existing tests and the existing
        ``_try_settle_invoice`` call site. New code should call wallet.topup
        directly via ``create_auto_topup()`` + ``charge_via_token()``.

        Returns ``(ok, msg)`` — same contract as before. The actual ECPay
        charge, wallet credit, and invoice creation are delegated to
        ``wallet.topup``.

        Return codes (preserved from original implementation):

        - ``'ok'``                          — charge succeeded, wallet credited
        - ``'no_shortfall'``                — shortfall <= 0
        - ``'no_token'``                    — no usable default ECPG token
        - ``'provider_not_enabled'``        — token's provider missing enable_ecpg
        - ``'currency_not_configured'``     — neither provider nor TWD available
        - ``'3ds_required'``                — ECPG returned ThreeDURL
        - ``'<state>'``                     — tx ended in non-done state
        - ``'sdk_error'``                   — exception from _send_payment_request
        - ``'wallet_credit_failed'``        — ECPay charged but wallet credit failed
        - ``'invalid_state'``               — topup state guard (concurrent caller)

        TODO: remove after one stable release once all callers migrated.
        Tracked in: ``docs/plans/2026-05-03-invoice-issue-fix-path-b.md``
        "Follow-up tickets" section.
        """
        _logger.info(
            '[AUTO_TOPUP] delegating to wallet.topup partner=%s shortfall=%s',
            partner.id, shortfall,
        )
        if shortfall <= 0:
            return False, 'no_shortfall'

        token = self._resolve_default_token(partner)
        if not token:
            return False, 'no_token'

        # Note: create_auto_topup pre-condition (amount > 0) is already checked
        # above, so any ValidationError reaching here would be a programming bug
        # we want to surface. ValueError catches the real case:
        # env.ref('base.TWD', raise_if_not_found=True) raising on missing
        # currency.
        try:
            topup = self.env['woow.wallet.topup'].sudo().create_auto_topup(
                partner=partner, amount=shortfall,
            )
        except ValueError:
            _logger.exception(
                '[AUTO_TOPUP] create_auto_topup ValueError (likely missing TWD '
                'ref) partner=%s shortfall=%s',
                partner.id, shortfall,
            )
            return False, 'currency_not_configured'
        return topup.charge_via_token(token)

    def _resolve_default_token(self, partner):
        """Find the default ECPG-bound payment token for ``partner``.

        Community-edition limitation: community ed has no
        ``property_payment_token_id`` so we fall back to the most recently
        created active ECPG-bound token. A future ticket should add an
        explicit ``is_default`` field on payment.token for deterministic
        selection across editions.

        Returns: ``payment.token`` record. Empty recordset if no usable
        token exists.
        """
        Token = self.env['payment.token'].sudo()
        token = Token
        if 'property_payment_token_id' in partner._fields:
            token = partner.with_company(
                partner.company_id or self.env.company,
            ).sudo().property_payment_token_id
        if not token or not token.active or not token.ecpg_bindcard_id:
            token = Token.search(
                [
                    ('partner_id', '=', partner.id),
                    ('active', '=', True),
                    ('ecpg_bindcard_id', '!=', False),
                ],
                order='id desc', limit=1,
            )
        if token and token.active and token.ecpg_bindcard_id:
            return token
        return self.env['payment.token']
