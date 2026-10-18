import logging

from odoo import _, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# ------------------------------------------------------------------------- #
#  #787：ECPay 發票強制開立的逃生門（private sentinel，形制抄 #536）           #
# ------------------------------------------------------------------------- #
#
# `woow_force_issue` 讓一張帶 `woow_skip_upstream_hook=True` 的發票**不再早退**，
# 而是走進上游 `ecpay_invoice_tw` 的 `create_ecpay_invoice()` —— 那支方法會把
# 請求送去 `einvoice.ecpay.com.tw`，開出一張**不可逆**的電子發票。生產側唯一的
# 正當呼叫端是非同步的 `ecpay_invoice_cron`。
#
# 原本的判定只看 truthy，而 `/web/dataset/call_kw` 把呼叫端 context 整包換進 env
# （`odoo/api.py::call_kw` → `split_context()` → `recs.with_context(context)`）
# ⇒ 一行 JSON 就開得了閘。
#
# 可達性是 #787 **實測**過的（`src/tests/test_ecpay_force_issue_ctx_forgery_787.py`
# 真 call_kw），不是推論，而且兩個租戶身分的結論**相反**——這正是為什麼不能只驗
# 一個身分就下結論：
#
#   * `base.group_user`   打不到：Odoo 18 的 `account` 對 `account.move` 一列 ACL
#                         都沒有給它（實測回 `AccessError`，連 `read` 都不行）。
#   * `base.group_portal` **打得到**：`account` 明給 portal 一列 `read=1`
#                         （`access_account_invoice_portal`）＋
#                         `account_invoice_rule_portal` 限縮到「自己的、已 post
#                         的」那幾張，而本平台的 signup 樣板使用者正是只有
#                         `base.group_portal`。實測：偽造的 context 讓流程真的走
#                         進了 `super()`。
#
# 目前**對外送出**還撞得到第二道牆：上游方法體在送出前要先建 `uniform.invoice`，
# 而那個 model 的 ACL 只給 `base.group_user`（實測 portal 回 `AccessError`）。
# 所以這把是 **defense-in-depth**——那道牆哪天鬆一格（給 portal 一列、或上游改
# 掉建立順序），這裡就是最後一道。形狀與 #787 的 `sm_route_cascade_unlink` 相同。
#
# 🔴 **fail-safe 的方向與前四把（skip 型）相反，不能照抄**：
#   * skip 型（`cs_skip_access_sync` 等）「不開閘」＝照常推送一次冪等的全量狀態，
#     代價可以吸收，所以「WARNING ＋ 靜靜不開閘」就夠了。
#   * 這一把是 **force 型**：「不開閘」＝**該開的發票沒開**。而唯一的生產呼叫端
#     `ecpay_invoice_cron._issue_one()` 在 `create_ecpay_invoice()` 沒有丟例外時
#     會把 `woow_invoice_issue_state` 寫成 `'issued'` —— 也就是說「靜靜不開閘」
#     會讓帳本記著已開立、ECPay 上什麼都沒有，而且 cron 只撿 `pending` / `failed`
#     ⇒ 再也沒有任何東西會回頭撿它。那是無聲且不可逆的漏開。
#   ⇒ 因此「truthy 但不是 sentinel」在這裡**必須大聲失敗**（WARNING ＋ 拋例外），
#     不能沿用 skip 型那條靜默路徑。對偽造者而言結果一樣是「開不成」，對「還在傳
#     `True` 的 in-process 碼」而言則會被 `_issue_one` 記成失敗、進重試與告警，
#     而不是被記成已開立。
#
# ⚠ key 名**沒有改**，改的是值 ⇒ `with_context(woow_force_issue=False)` 的語意
# 原封不動（一直都是「照常早退」）。
#
# ⚠ 命名雙向機械查過：與既有 sentinel／reader（SA／CS／SH 三檔那幾把、以及 #519
# 那組開閘符號）無同名、無互為子字串。註解裡也刻意不逐字列出 repo-lint
# `check_provision_sentinel_allowlist` 的判準符號（那是整檔字面比對）。
class _InvoiceIssueControl:
    """ECPay 發票強制開立逃生門的 marker type —— 見上方註解。"""

    __slots__ = ()

    def __repr__(self):
        return '<internal invoice issue control>'


_INTERNAL_INVOICE_ISSUE_CONTROL = _InvoiceIssueControl()

#: key 本身不是判準（呼叫端當然抄得到這個字串），判準是它的**值身分**。
_CTX_WOOW_FORCE_ISSUE = 'woow_force_issue'


def _forced_ecpay_issue(records):
    """開閘：這一次呼叫要真的向 ECPay 開立發票，即使發票帶著 skip 旗標。

    這是 `with_context(woow_force_issue=True)` 這種**可偽造寫法**的唯一替代品。
    今天生產側只有 `ecpay_invoice_cron._issue_one()` 用得到它。
    """
    return records.with_context(
        **{_CTX_WOOW_FORCE_ISSUE: _INTERNAL_INVOICE_ISSUE_CONTROL})


def _is_invoice_issue_control(env, key):
    """這個強制開立開關是不是**伺服器端程式碼**設的（而非呼叫端偽造的）。

    只有 sentinel 物件算數。JSON 表達不出物件同一性，所以租戶送
    ``context={"woow_force_issue": true}`` 進 ``call_kw`` 時到不了 ``True`` 這條
    return —— 走的是下面那條 WARNING ＋ `UserError`。

    「truthy 但不是 sentinel」為什麼要**拋例外**而不是靜靜回 `False`：見上方註解
    的 fail-safe 那一段。一句話——這一把「不開閘」＝「該開的發票沒開」，而唯一的
    生產呼叫端會把靜默的早退記成 `issued`。靜靜回 `False` 會製造一個帳本說謊、
    沒有任何重試會回頭撿的無聲漏開；拋例外則讓它落進 `_record_failure()` 的重試
    與告警。對偽造者來說兩者結果相同（開不成），差別全在「誰會知道」。
    """
    value = env.context.get(key)
    if value is _INTERNAL_INVOICE_ISSUE_CONTROL:
        return True
    if value:
        _logger.warning(
            "Ignoring non-sentinel %r in context (value=%r, uid=%s): this "
            "hatch only opens for in-process callers using the module's "
            "helper — see _INTERNAL_INVOICE_ISSUE_CONTROL (#787). Either a "
            "forged RPC context, or in-process code still passing True.",
            key, value, env.uid)
        raise UserError(_(
            "This e-invoice can only be issued by the platform's own issuing "
            "job."
        ))
    return False


class AccountMove(models.Model):
    _inherit = 'account.move'

    def create_ecpay_invoice(self):
        """Short-circuit when invoice is PaaS-flagged and not forced.

        Why: upstream `ecpay_invoice_tw` calls `create_ecpay_invoice()` inline
        from its `action_post()` hook (when company.auto_invoice='automatic'),
        raising UserError on ECPay failure — which would roll back the entire
        `_pay_from_wallet` transaction (including the wallet deduction).

        PaaS-created invoices set `woow_skip_upstream_hook=True` so this
        method returns early when called from the upstream hook. Our async
        `ecpay_invoice_cron` opens the back door via the module-private
        `_forced_ecpay_issue()` helper (#787 — a plain truthy context value is
        forgeable over `call_kw`; see the sentinel block at the top of this
        module) so it can still issue the invoice.

        Thread-safe: pure inheritance + context (no class-level monkey-patch).
        """
        self.ensure_one()
        # `and` 的短路是刻意的：沒有 skip 旗標的發票（官網／ERP 開的那些）本來就
        # 該走 super()，判定函式連叫都不用叫 —— 也因此它們不會被「context 裡有一
        # 個沒作用的 woow_force_issue」波及。
        if (self.woow_skip_upstream_hook
                and not _is_invoice_issue_control(
                    self.env, _CTX_WOOW_FORCE_ISSUE)):
            _logger.debug(
                "[INV] create_ecpay_invoice short-circuit invoice=%s "
                "(skip flag set, no force ctx)",
                self.id,
            )
            return
        return super().create_ecpay_invoice()

    def run_refund(self):
        """#1555：PaaS 建立的貸項不在上游 inline hook 打 ECPay 折讓。

        上游 ``ecpay_invoice_tw.action_post()`` 在 ``company.auto_invoice='automatic'`` 時對
        ``out_refund`` inline 呼叫 ``run_refund()``（對外開折讓，失敗即 raise）。平台在租戶的
        刪除／換方案請求裡建的退點貸項（``sh_subscription_refund_credit_note``）帶
        ``woow_skip_upstream_hook``：外部呼叫不可在那個交易裡發生（billing-philosophy §5；
        本檔 billing-philosophy「Scope limitation」記著這一格待補）。需要折讓的貸項由該處
        建待辦交給人工在綠界開立。沒有 skip 旗標的貸項（ERP／官網）照舊走上游。
        """
        self.ensure_one()
        if self.woow_skip_upstream_hook:
            _logger.info(
                "[INV] run_refund short-circuit credit_note=%s (PaaS refund; allowance is "
                "issued manually)", self.id,
            )
            return
        return super().run_refund()

    def _woow_render_tenant_invoice_pdf(self):
        """AUDITFIX tax-invoice-pdf-403：以平台身分渲染這張發票的 PDF，回傳 ``(bytes, 檔名)``。

        租戶（portal）以自己的身分走原生 ``/report/pdf/account.account_invoices/<id>`` 時，
        報表要讀發票行的 ``account.tax``，portal 沒有讀取權 ⇒ 403（政策 A 起所有新發票都帶
        5% 稅，等於每張都下載不了）。不給 portal 全域的 ``account.tax`` 讀取權（那會把
        公司所有稅別設定開給所有租戶），改由 ``/api/billing/invoices/<id>/pdf`` 先驗
        「這張發票屬於目前使用者」，再呼叫這裡以 sudo 渲染。**呼叫端必須先驗擁有權**。

        語言用發票客戶的語言（與原生 portal 下載相同）。
        """
        self.ensure_one()
        move = self.sudo()
        lang = move.partner_id.lang or self.env.lang
        report = self.env['ir.actions.report'].sudo().with_company(
            move.company_id).with_context(lang=lang)
        content, _content_type = report._render_qweb_pdf(
            'account.account_invoices', move.ids)
        filename = '%s.pdf' % ((move.name or 'invoice').replace('/', '_'))
        return content, filename

    def _pay_from_wallet(self, subscription=None):
        """Settle this invoice using wallet points.

        Deducts `amount_total` (for draft) or `amount_residual` (for posted)
        from `partner_id.wallet_balance`, posts the invoice if still in draft,
        creates an `account.payment`, and reconciles the receivable lines.

        Caller is expected to have already verified balance sufficiency; if the
        wallet can't cover the amount when this method runs we raise UserError
        rather than silently partial-deducting (partner.deduct_wallet_points
        would otherwise return only what it could take).

        :param subscription: optional ``sh.subscription.subscription`` record
            used for the wallet.transaction description / audit link.
        :returns: True on success.
        :raises UserError: insufficient wallet balance, wallet deduction
            failure, or missing payment journal.
        """
        self.ensure_one()
        # #324：手動付款端點 api_invoice_pay 呼叫時不帶 subscription。若這是
        # 訂閱週期發票（account.move.sh_subscription_id 有值），從發票回推
        # subscription，讓 deduct_wallet_points 把 subscription_id stamp 到該筆
        # done deduct 上——否則退點 gate _has_paid_current_cycle 找不到 stamped
        # deduct，會把「手動付掉整個週期」的訂閱誤判為未付、刪服務退 0。
        if subscription is None and self.sh_subscription_id:
            subscription = self.sh_subscription_id
        partner = self.partner_id.sudo()
        amount = self.amount_total if self.state == 'draft' else self.amount_residual
        _logger.info(
            "[INV] _pay_from_wallet entry invoice=%s name=%s state=%s partner=%s amount=%s balance=%s sub=%s",
            self.id, self.name, self.state, partner.id, amount,
            partner.wallet_balance,
            subscription.id if subscription else None,
        )
        if amount <= 0:
            _logger.info(
                "[INV] _pay_from_wallet nothing_to_pay invoice=%s", self.id,
            )
            raise UserError(_("Nothing to pay for invoice %s.", self.name or ""))
        if partner.wallet_balance < amount:
            _logger.info(
                "[INV] _pay_from_wallet insufficient invoice=%s need=%s have=%s",
                self.id, amount, partner.wallet_balance,
            )
            raise UserError(_("Insufficient wallet balance."))

        # Post first so the sequence assigns self.name before the wallet
        # transaction records it in its description.
        if self.state == 'draft':
            _logger.info("[INV] _pay_from_wallet posting draft invoice=%s", self.id)
            self.action_post()

        description = (
            f"Subscription invoice: {self.name}" if subscription
            else f"Invoice payment: {self.name}"
        )
        deducted = partner.deduct_wallet_points(
            amount, description, subscription=subscription,
        )
        # Drop the partner.wallet_balance cache so any caller that re-reads it
        # in the same request (controllers building responses, the renewal cron
        # processing the next subscription) gets the post-deduction value.
        partner.invalidate_recordset(['wallet_balance'])
        _logger.info(
            "[INV] _pay_from_wallet deducted invoice=%s requested=%s got=%s",
            self.id, amount, deducted,
        )
        if deducted <= 0:
            raise UserError(_("Wallet deduction failed."))

        company = self.company_id.sudo()
        journal = company.sh_paid_subscription_journal
        if not journal:
            journal = self.env['account.journal'].sudo().search(
                [
                    ('company_id', '=', company.id),
                    ('type', 'in', ('bank', 'cash')),
                ],
                limit=1,
            )
        if not journal:
            _logger.warning(
                "[INV] _pay_from_wallet no journal invoice=%s company=%s",
                self.id, company.id,
            )
            raise UserError(_("No payment journal configured for company %s.", company.name))

        payment_method_line = journal.inbound_payment_method_line_ids[:1]
        payment = self.env['account.payment'].sudo().create({
            'journal_id': journal.id,
            'amount': amount,
            'payment_type': 'inbound',
            'payment_method_line_id': payment_method_line.id if payment_method_line else False,
            'partner_type': 'customer',
            'partner_id': partner.id,
        })
        payment.action_post()
        _logger.info(
            "[INV] _pay_from_wallet payment created invoice=%s payment=%s journal=%s",
            self.id, payment.id, journal.id,
        )

        receivable_lines = self.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
        )
        payment_lines = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
        )
        if receivable_lines and payment_lines:
            (receivable_lines + payment_lines).reconcile()
            _logger.info(
                "[INV] _pay_from_wallet reconciled invoice=%s inv_lines=%s pay_lines=%s",
                self.id, len(receivable_lines), len(payment_lines),
            )
        else:
            _logger.warning(
                "[INV] _pay_from_wallet reconcile skipped invoice=%s inv_lines=%s pay_lines=%s",
                self.id, len(receivable_lines), len(payment_lines),
            )

        self.invalidate_recordset(['payment_state', 'amount_residual'])
        _logger.info(
            "[INV] _pay_from_wallet done invoice=%s name=%s amount=%s partner=%s",
            self.id, self.name, amount, partner.id,
        )
        # #1599：訂閱發票付清了 ⇒ 請付款後復權的 cron 在 commit 之後立即復權這筆訂閱底下
        # 停權中的雲端服務。這裡仍在付款交易內：只寫旗標＋排程，不呼叫 operator，也不會讓
        # 付款失敗（見 `_request_paid_resume`）。訂閱扣款、/woow 付發票、略過試用、確認
        # 訂閱開票都經過這裡。
        if subscription:
            lifecycle = self.env['woow_paas_platform.subscription_lifecycle_cron'].sudo()
            lifecycle._request_paid_resume(subscription, source='invoice_paid')
        return True
