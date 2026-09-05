import logging
import math
import uuid
from datetime import timedelta

import psycopg2

from odoo import _, api, fields, models, SUPERUSER_ID
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
# #864 PR-G：付款意圖預登記寫不進去 ⇒ fail-closed，⛔ 不外呼。
TOPUP_INTENT_NOT_RECORDED = 'intent_not_recorded'


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
            'intent_not_recorded'         - #864 PR-G pre-registration failed; fail-closed, ZERO outbound calls
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

        # #864 PR-G：預登記必須落在**不可逆外呼之前**。這裡是最早可能的
        # 位置——reference（＝MTN）剛算出來，tx 都還沒建立，所以 fail-closed
        # 退出時零外呼、零 tx 殘留。
        if not self._log_topup_intent(reference, token=token,
                                      charge_amount=charge_amount):
            self.write({
                'state': 'failed',
                'failure_code': TOPUP_INTENT_NOT_RECORDED,
                'failure_message': (
                    'INTENT_NOT_RECORDED: payment intent pre-registration '
                    'failed; charge deliberately not attempted'
                ),
            })
            return False, TOPUP_INTENT_NOT_RECORDED

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

    def _log_topup_intent(self, reference, token=None, charge_amount=None):
        """外呼之前用獨立交易寫一筆付款意圖預登記（#864 PR-G）。

        **為什麼一定要在外呼之前**：3DS callback 可能在本 request 的交易
        commit **之前**就抵達 ⇒ 那時 ``payment.transaction`` 這一列還不存在
        ⇒ vendored ``pay_callback`` 查無 tx、回 ``1|OK`` 叫 ECPay 停止重送
        ⇒ 錢永不入帳而且零告警。預登記（``woow.billing.incident._log()``
        的獨立 cursor ＋ commit）是唯一在那個時點就已經跨連線可見的東西，
        callback inbox 與 ``_cron_replay_callback_inbox`` 都靠它對帳。

        ``external_ref`` 用 ``reference`` 原文：topup 的 reference 是
        ``"TOPUP"`` ＋ 15 碼大寫 hex ＝ 20 字元純 alnum，vendored 的
        ``_ecpg_build_merchant_trade_no``（去非 alnum、截 20）對它是恆等
        ⇒ MTN ≡ reference。⛔ 不在平台側自抄一份 sanitize 規則（會漂）。

        回傳 incident id 或 ``False``。呼叫端對 ``False`` **必須 fail-closed**
        （issue-leader 2026-09-01 裁決 Q1）：⛔ 不得靜默繼續外呼——沒有預登記
        的那一筆就是本 PR 要消滅的掉款洞本身，而且它無聲。

        ``_log`` 契約是永不外拋；這裡仍自己包一層 try/except，因為組 snapshot
        的那段 Python 邏輯若未來重構誤觸型別錯誤，也必須收斂成 ``False``
        ＋ critical log，⛔ 不得把例外丟回付款流程。
        """
        self.ensure_one()
        try:
            incident_id = self.env['woow.billing.incident']._log({
                'external_ref': reference,
                'kind': 'intent',
                'partner_ref': self.partner_id.id or None,
                'payment_token_ref': token.id if token else None,
                'amount': float(
                    charge_amount if charge_amount is not None else self.amount
                ),
                'credits': self.credits,
                'bonus': self.bonus,
                'currency': self.currency_id.name or None,
                'source_model': self._name,
                'source_ref': self.id,
                # 通用句子：⛔ 不放 provider 原始訊息／ResultData 密文
                # （PAN-like 與 HTML 都會讓整筆 snapshot 被 fail-soft 拒收，
                #  等於預登記靜默消失；PCI 面見 billing_incident 模組 docstring）。
                'detail': (
                    '付款意圖預登記：在不可逆外呼之前以獨立交易寫入，'
                    '供 callback inbox 與 replay 對帳使用。'
                ),
            })
        except Exception:
            _logger.critical(
                '[TOPUP] CRITICAL: payment intent pre-registration raised '
                'before the outbound call (topup=%s); fail-closed, no charge '
                'will be attempted', self.id,
            )
            return False
        if not incident_id:
            _logger.critical(
                '[TOPUP] CRITICAL: payment intent pre-registration failed '
                '(topup=%s); fail-closed, no charge will be attempted — '
                'without it a callback arriving before commit would be '
                'answered 1|OK and the money would never be credited',
                self.id,
            )
        return incident_id

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
            # #864 PR-G：⛔ 不能用泛用的 `_log`。PR-G 的預登記已經用**同一個**
            # `tx.reference` 佔住那一列（`UNIQUE(external_ref)`），而 `_log` 的
            # kind 在 INSERT 當下定死 ⇒ 走 `_log` 會撞 kind collision、事實被
            # 丟棄，冷卻閘從此查不到東西。專用 API 做的是原地升級
            # `intent → charged_not_credited`（單 statement、無 pre-read）。
            incident_id = self.env[
                'woow.billing.incident']._log_charged_not_credited({
                    'external_ref': tx.reference,
                    'kind': 'charged_not_credited',
                    'partner_ref': self.partner_id.id or None,
                    'payment_token_ref': token.id if token else None,
                    'amount': float(charge_amount),
                    'credits': self.credits,
                    'bonus': self.bonus,
                    'currency': currency.name if currency else None,
                    # 🔴 #993：⛔ **不寫 `trade_no`**。這裡沒有 provider 給的
                    # 單號可寫——`tx.reference` 是我方 MerchantTradeNo，它已經
                    # 在同一個 snapshot 的 `external_ref` 裡了（同值、冗餘）。
                    # 寫進來的實害不是重複而是**覆寫**：`_upsert_incident_snapshot`
                    # 把 `trade_no` 列進 `explicit_evidence`（`billing_incident.py`
                    # 的 `explicit_evidence` 那段）⇒ `ON CONFLICT DO UPDATE SET
                    # trade_no = EXCLUDED.trade_no` ⇒ 若 PR-G 的 callback
                    # evidence 已經把**真的 ECPay TradeNo** 寫進這一列（kind 仍是
                    # `intent`），這次 promote 會把它蓋掉——而那是平台**唯一一份**
                    # ECPay 交易編號（vendored addon 只在查得到 tx 時才寫
                    # `tx.provider_reference`，本場景正是查不到）。Eugene 2026-09-02
                    # 裁決 A（#993 留言 11404）。
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

    # 公版 choke point 的行鎖預算（#864 PR-G SF4）。沿 PR-D／PR-E 既有的 1s
    # 前例：這是**安全參數不是效能旋鈕**——入口可能被 cron 的 per-record
    # cursor 持有著，無界等待會把 request thread 卡死。
    _CHOKE_LOCK_TIMEOUT = '1s'

    def _on_payment_done(self, tx):
        """Credit the wallet when the linked payment transaction succeeds.

        Idempotent: replaying with the same tx is a no-op.

        🔴 #864 PR-G SF4 —— **這是平台唯一的入帳 choke point，所以互斥點也放
        在這裡**（G 定公版，PR-F 沿用）。進來的第一件事是對自己那一列取
        **阻塞式** ``FOR UPDATE``，拿到之後才重讀狀態走冪等守衛。

        為什麼是阻塞式而不是 ``NOWAIT``：``NOWAIT`` 是 **scanner 側**的語意
        （cron 搶不到就跳過、下輪再撿）。callback 側不能那樣做——它沒有「下
        一輪」，把「別人正在補」當場變成錯誤往上拋，等於把一次可收斂的競爭
        變成一次失敗。所以這裡等，讓 winner 先把事情做完。

        ``SET LOCAL lock_timeout`` 讓等待有界，而且**用完就還原**：這個
        method 跑在呼叫端的交易裡，把逾時值留給後面的 SQL 會變成一個沒人預期
        的副作用（發票、記帳那些語句會跟著在 1 秒後失敗）。

        🔴 **contention 下 loser 的實際結局（照量測寫，⛔ 不是「乾淨冪等
        退出」）**——見 ``tests/integration/test_callback_replay_864.py`` 的
        雙向決定性競態：

        * **本 method 當 loser**（replay 先入帳並 commit）：這裡先卡在阻塞式
          ``FOR UPDATE`` 上；winner commit 之後鎖放開，而本交易的
          REPEATABLE READ 快照已經比那一列的最新版本舊 ⇒ PostgreSQL 直接給
          **40001（serialization failure）**，⛔ **不會**走到下面那個冪等守衛。
          production 上這條路徑是 RPC dispatch，Odoo 的 ``retrying()`` 會用
          新快照重放整個請求，**第二趟**才會被下面的冪等守衛擋下來。
          ⚠ ``retrying()`` **只掛 RPC dispatch**（#864 PR-E 輪已定案，cron 沒
          有），所以測試環境裡不存在——測試斷言到 40001 為止，重放那一段是
          production 行為，⛔ 沒有被測試斷言。
        * **等超過 ``lock_timeout``**：``LockNotAvailable``（55P03）往上拋 →
          對 ECPay 是一次失敗回應 → 由它重送。
        * **scanner（replay）當 loser**：它走的是 ``FOR UPDATE NOWAIT``，當場
          記 ``lock_not_available`` 跳過、留給下一輪；下一輪看到 topup 已
          ``done`` ⇒ 冪等收工。

        兩個方向都實測過**零重複入帳**——那是這一格的安全底線。
        replay 側因為已經持有同一列的鎖，這裡的重入是 no-op（同交易再取一次
        ``FOR UPDATE`` 不會等自己）。
        """
        self.ensure_one()
        _logger.info(
            "[TOPUP] _on_payment_done entry topup=%s state=%s tx=%s tx_state=%s",
            self.id, self.state, tx.id, tx.state,
        )
        self.env.cr.execute('SHOW lock_timeout')
        previous_lock_timeout = self.env.cr.fetchone()[0]
        self.env.cr.execute('SET LOCAL lock_timeout = %s',
                            (self._CHOKE_LOCK_TIMEOUT,))
        self.env.cr.execute(
            'SELECT id FROM woow_wallet_topup WHERE id = %s FOR UPDATE',
            (self.id,),
        )
        # 還原只放在**成功之後**，⛔ 不放 finally：逾時的話這條交易已經
        # aborted，那個 `SET LOCAL` 只會拋 InFailedSqlTransaction 把真正的
        # LockNotAvailable 蓋掉（實測踩過）。而交易 abort／rollback 本來就會
        # 把 `SET LOCAL` 一起還原，所以失敗路徑不需要也不應該補這一手。
        self.env.cr.execute('SET LOCAL lock_timeout = %s',
                            (previous_lock_timeout,))
        # 鎖只擋並發 writer，⛔ 不會刷新本 env 的 record cache ⇒ 必須重讀，
        # 否則冪等守衛看的是**取鎖之前**那個已經過期的快照。
        self.invalidate_recordset(['state', 'wallet_transaction_id'])
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

    # #864 PR-G — replay 引擎的形狀常數。
    # ⚠ G 先落地 ⇒ **G 定義形狀、PR-F 沿用**：入帳 choke point、行鎖形態、
    #   per-record cursor、skip 痕跡全部照這裡。⛔ 不要各寫一套。
    _REPLAY_BATCH_SIZE = 50
    # 逾時是安全參數不是效能參數：這條 cursor 可能與正在進行的付款 request
    # 搶同一列，無界等待會把 cron worker 卡在它自己剛回報的競爭上。
    _REPLAY_LOCK_TIMEOUT = '1s'
    _REPLAY_RESOLUTION_NOTE = (
        '由 #864 PR-G 的 callback replay 自動補入帳並結案；'
        '依據為 provider callback 回報的成功代碼與交易編號。'
    )
    # 升級成這兩個狀態的列要 best-effort 排人工覆核 activity（SF2）。
    # blocker 撤掉時間驅動的升級之後這兩態變稀有 ⇒ activity 不會氾濫。
    _REPLAY_REVIEW_STATES = frozenset({'needs_human', 'orphan'})
    _REPLAY_ALREADY_CREDITED_NOTE = (
        '由 #864 PR-G 的 callback replay 結案：這筆早已入帳（topup 已 done），'
        'inbox 裡的事實只是晚到的重複觀測。'
    )

    @api.model
    def _replay_candidates(self):
        """撈出「可能需要補入帳」的預登記——**獨立 cursor，繞 RR 快照**。

        🔴 為什麼不能用一般 ORM ``search``：Odoo 的 cron tick 是 REPEATABLE
        READ，快照凍結在起跑那一刻；而預登記與 callback 事實都是
        ``woow.billing.incident`` 用**獨立 cursor** commit 的，同一個 tick 內
        用 ``self.env`` 讀不到它們（同 ``_double_charge_cooldown_active``
        的教訓）。

        入集合條件三段，每一段都有它必須存在的理由：

        1. ``kind = 'intent' AND state <> 'resolved'`` —— 只有已結案的才真的
           出局。``needs_human``／``orphan`` **仍在候選裡**：它們是給人看的
           標記，⛔ 不是「cron 不再理它」的意思。
        2. ``rtn_code IS NOT NULL`` —— inbox 裝的是**已經收到 callback 的**
           那些。沒收到 callback 的預登記不是「跳過」，它只是還沒有事實可談；
           把它撈出來只能發明一個新的 skip 語意，而那是產品值不是實作細節。
        3. ``last_skipped_at IS NULL OR last_skipped_at < write_date`` ——
           防餓死。評估過就先讓開，**直到 evidence 有新事實**（atomic upsert
           會把 ``write_date`` 推前）才重新入選。
           🔴 這是**候選集合**策略，⛔ 不是狀態轉移：被拒的刷卡留在 ``open``、
           skip 欄位完整，人工隊列照設計文用**年齡查詢** derive 得到它。
           G 的 cron ⛔ 不寫任何「時間驅動的升級／放棄」，也不另造一個鐘
           （那會與 §6.6.3b 已裁的 T2＝4h 打架）。

        ⚠ 判準必須是「自上次**評估**以來有沒有新事實」，所以搶不到行鎖時
        ⛔ 不可以蓋 ``last_skipped_at``（見
        ``billing_incident._mark_replay_skip_current_cursor`` 的同名參數）。
        """
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s',
                       (self._REPLAY_LOCK_TIMEOUT,))
            cr.execute(
                "SELECT external_ref, rtn_code, trade_no, source_model, "
                "source_ref "
                "FROM woow_billing_incident "
                "WHERE kind = 'intent' AND state <> 'resolved' "
                "AND rtn_code IS NOT NULL "
                "AND (last_skipped_at IS NULL OR last_skipped_at < write_date) "
                "ORDER BY COALESCE(last_skipped_at, last_seen_at) "
                "LIMIT %s",
                (self._REPLAY_BATCH_SIZE,),
            )
            columns = ('external_ref', 'rtn_code', 'trade_no', 'source_model',
                       'source_ref')
            return [dict(zip(columns, row)) for row in cr.fetchall()]

    @api.model
    def _cron_replay_callback_inbox(self):
        """把 inbox 裡「已收到成功 callback」的預登記補成入帳（#864 PR-G）。

        每一筆走自己的獨立 cursor。**這不是效能考量是正確性考量**：
        ``FOR UPDATE NOWAIT`` 搶不到會拋 ``LockNotAvailable``，PostgreSQL 當場
        把整條交易標成 aborted ⇒ 共用一條 cursor 的話，之後每一筆的第一個 SQL
        都死在 ``InFailedSqlTransaction``；而且任一筆撞 40001 會把**整輪**
        （含前面已補成功的）一起回捲，連 ``skip_count``／``needs_human`` 這些
        安全閥標記也跟著消失。

        ⛔ 例外不外拋：cron 沒有同 tick 重試（``retrying()`` 只掛 RPC
        dispatch），拋出去只會讓後面的筆數一起不跑。
        """
        candidates = self._replay_candidates()
        summary = {
            'scanned': len(candidates),
            'credited': 0,
            'already_credited': 0,
            'failed': 0,
            'skipped_by_reason': {},
        }
        for candidate in candidates:
            outcome, reason = self._replay_one_callback_intent(candidate)
            if outcome == 'credited':
                summary['credited'] += 1
            elif outcome == 'already_credited':
                summary['already_credited'] += 1
            elif outcome == 'failed':
                summary['failed'] += 1
            else:
                summary['skipped_by_reason'][reason] = (
                    summary['skipped_by_reason'].get(reason, 0) + 1)
        # 🔴 零也要印：這是讓「今天沒事」與「每一筆都被擋掉」在日誌上可分辨的
        # 唯一辦法，也是 heartbeat 的依附點。
        _logger.info(
            '[TOPUP_REPLAY] scanned=%s credited=%s already_credited=%s '
            'failed=%s skipped_by_reason=%s',
            summary['scanned'], summary['credited'],
            summary['already_credited'], summary['failed'],
            summary['skipped_by_reason'],
        )
        return summary

    @api.model
    def _replay_one_callback_intent(self, candidate):
        """處理一筆預登記；回傳 ``(outcome, reason)``。

        步驟固定（G 定形狀、F 沿用）：
        1. 對 topup 列 ``FOR UPDATE NOWAIT`` 取序列化點；
        2. **拿到鎖之後才重讀**狀態與事實（⛔ 不用鎖前那次讀的值）；
        3. 逐狀態分流，每一個「決定不補」的分支都寫確切 ``skip_reason``；
        4. 入帳只走唯一 choke point ``topup._on_payment_done(tx)``——``topup``
           必須是**這條 per-record cursor** 上的 recordset（見該行註解：用外層
           cron 那條 cursor 會等自己的鎖、自我死鎖）；
        5. 標記 incident ``resolved`` **在同一個交易內**（⛔ 不另開 cursor，
           否則會出現「補了但沒標」或「標了但沒補」的裂縫）。

        鎖序固定 **topup → incident**（第 5 步的 transition 會對 incident 列
        取阻塞式 ``FOR UPDATE``）。⛔ 不得反序，否則兩顆 cron 交叉死鎖。
        """
        external_ref = candidate['external_ref']
        escalated = []
        try:
            with self.env.registry.cursor() as cr:
                # 必須是這條連線的第一個 statement。
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._REPLAY_LOCK_TIMEOUT,))
                env = api.Environment(cr, SUPERUSER_ID, {})
                incidents = env['woow.billing.incident'].sudo()

                def _skip(reason, state=None, bump_attempts=False):
                    # 🔴 ⛔ 這裡**沒有**任何時間條件。G 的 cron 不寫「時間驅動
                    # 的升級／放棄」狀態——人工隊列照設計文用**年齡查詢**
                    # derive（`state != 'resolved' AND create_date < now - X`），
                    # 那是查詢不是狀態轉移；G 不建佇列、不另造鐘（會與
                    # §6.6.3b 已裁的 T2＝4h 打架）。
                    # 顯式狀態轉移只有設計分支本來就有的那幾格，由呼叫端明寫。
                    marked = incidents._mark_replay_skip_current_cursor(
                        external_ref, reason, state=state,
                        bump_attempts=bump_attempts)
                    cr.commit()
                    if marked and state in self._REPLAY_REVIEW_STATES:
                        # commit 之後才排（best-effort）：activity 排不出來
                        # ⛔ 不得回捲已經 commit 的 transition。
                        escalated.append(marked)
                    return 'skipped', reason

                if (candidate['source_model'] != self._name
                        or not candidate['source_ref']):
                    return _skip('no_topup_row', state='orphan')

                cr.execute(
                    'SELECT id FROM woow_wallet_topup WHERE id = %s '
                    'FOR UPDATE NOWAIT', (candidate['source_ref'],))
                if not cr.fetchone():
                    # 主交易整段回捲過（topup 列根本不存在）＝設計分支 C 的
                    # 真孤兒 ⇒ 直接標 orphan 交人工，⛔ 不等一個自訂的鐘。
                    return _skip('no_topup_row', state='orphan')

                # --- 鎖到之後才重讀（⛔ 不用鎖前那次讀的值）--------------- #
                cr.execute(
                    'SELECT id, state, rtn_code, trade_no '
                    'FROM woow_billing_incident WHERE external_ref = %s',
                    (external_ref,))
                incident_row = cr.fetchone()
                if not incident_row or incident_row[1] != 'open':
                    # 另一個 worker 已經處理掉了。它寫下的終態就是痕跡本身，
                    # ⛔ 不再疊一層 skip（也寫不進去：resolved 是不可覆寫的）。
                    return 'skipped', 'incident_not_open'
                incident_id, _state, rtn_code, trade_no = incident_row

                topup = env['woow.wallet.topup'].sudo().browse(
                    candidate['source_ref'])
                if topup.state == 'done':
                    incidents.browse(
                        incident_id)._transition_resolved_current_cursor(
                            None, self._REPLAY_ALREADY_CREDITED_NOTE,
                            SUPERUSER_ID)
                    cr.commit()
                    return 'already_credited', None
                if topup.state == 'draft':
                    # 還沒外呼過，callback 不該存在 ⇒ ⛔ 不入帳。
                    return _skip('intent_without_charge', state='needs_human')
                if topup.state == 'failed':
                    # `_on_payment_done` 的守衛只擋 done、擋不住 failed；而
                    # `wallet_credit_failed` 的自動補點是 **PR-F** 的射程。
                    return _skip('failed_belongs_to_pr_f')
                if topup.state == 'cancelled':
                    # 已被 hard-cancel、tx 也被 `_set_canceled` 動過 ⇒ 狀態
                    # 不可信。⛔ 不自動復活（那是自動出錢的變體）。
                    return _skip('cancelled_needs_human', state='needs_human')

                # --- 這裡開始只剩 pending：驗事實再入帳 -------------------- #
                if rtn_code != '1':
                    return _skip('callback_not_successful')
                if not trade_no:
                    # ECPay 給了單號＝它那邊真的有這筆；缺了就 ⛔ 不補。
                    return _skip('missing_trade_no')
                tx = topup.payment_transaction_id
                if not tx:
                    return _skip('missing_payment_transaction',
                                 state='needs_human')

                # 唯一入帳 choke point。
                # 🔴 必須用 `topup`（＝**這條 per-record cursor** 上的
                # recordset）。用 `self.browse(...)` 會落回外層 cron 那條
                # cursor，而行鎖是這條 cursor 拿的 ⇒ 外層那條寫同一列時等自己
                # 的鎖，**自我死鎖**（實測會整支測試掛住直到 timeout）。
                topup._on_payment_done(tx)
                # 入帳（不可逆）之後才做的 tx 狀態對齊只是**記帳**：它失敗
                # ⛔ 不得把已經發生的入帳一起回捲（見
                # docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md）。
                # ⚠ #1052 標記（非裁定）：本站點與上列 doc 的規則一（OperationalError
                # 必須外拋）分歧——下方 `except Exception:` 未分流 OperationalError。
                # `cr` 是本列專用的獨立 cursor（不是外層 cron 那條），規則是否轉移
                # 到 per-record cursor、還是刻意例外，待裁定；裁定前別照抄本站形狀。
                try:
                    with cr.savepoint():
                        if trade_no and not tx.provider_reference:
                            tx.provider_reference = trade_no
                        if tx.state not in ('done', 'cancel'):
                            tx._set_done()
                except Exception:
                    _logger.exception(
                        '[TOPUP_REPLAY] wallet credited but the transaction '
                        'state could not be reconciled topup=%s tx=%s',
                        topup.id, tx.id,
                    )
                incidents.browse(
                    incident_id)._transition_resolved_current_cursor(
                        None, self._REPLAY_RESOLUTION_NOTE, SUPERUSER_ID)
                cr.commit()
                _logger.info(
                    '[TOPUP_REPLAY] credited topup=%s tx=%s', topup.id, tx.id)
                return 'credited', None
        except psycopg2.errors.LockNotAvailable:
            # 別人正在處理這一筆。本 cursor 已 aborted ⇒ 痕跡只能換一條連線寫。
            # 🔴 `touch_last_skipped=False`：沒拿到鎖代表這一列**根本沒被評估**
            # ⇒ ⛔ 不算一次 attempt、下一輪仍要撿得回來。蓋了時戳等於把一個
            # 從沒被看過的列永久排除在候選之外。
            self.env['woow.billing.incident'].sudo() \
                ._mark_replay_skip_independent(
                    external_ref, 'lock_not_available',
                    touch_last_skipped=False)
            return 'skipped', 'lock_not_available'
        except psycopg2.errors.SerializationFailure:
            self.env['woow.billing.incident'].sudo() \
                ._mark_replay_skip_independent(
                    external_ref, 'credit_failed', bump_attempts=True)
            return 'failed', 'serialization_failure'
        except Exception:
            _logger.exception(
                '[TOPUP_REPLAY] replay failed for one intent; the batch '
                'continues (the row keeps its own cursor, so nothing else '
                'is rolled back)',
            )
            self.env['woow.billing.incident'].sudo() \
                ._mark_replay_skip_independent(
                    external_ref, 'credit_failed', bump_attempts=True)
            return 'failed', None
        finally:
            # #864 PR-G SF2：升級成 needs_human／orphan 的那些列，在 transition
            # **commit 之後**才 best-effort 排人工覆核 activity。排不出來只記
            # log 就繼續——⛔ 不得回捲已 commit 的 transition、⛔ 不得殺掉整批。
            for incident_id in escalated:
                try:
                    self.env['woow.billing.incident'].sudo() \
                        ._schedule_review_activities(incident_id)
                except Exception:
                    _logger.error(
                        '[TOPUP_REPLAY] incident id=%s '
                        'category=activity_schedule_failed; the escalation '
                        'itself is already durable', incident_id,
                    )

    @api.model
    def _has_unreplayed_callback_evidence(self, topup):
        """這筆 topup 的 inbox 裡有沒有「還沒補完的成功 callback」（#864 PR-G）。

        🔴 **必須走獨立 cursor**：incident 是別的連線用獨立交易 commit 的，
        cron tick 的 REPEATABLE READ 快照凍結在起跑那一刻 ⇒ 用 ``self.env``
        的 ORM search 會讀不到「這一輪剛被 replay 或 callback 寫進去的列」
        （同 ``_double_charge_cooldown_active`` 的教訓）。

        🔴 **讀不到就當作「有」**：這道閘的下游動作是 hard-cancel——不可逆、
        而且會把「ECPay 說收了錢」的那一筆標成使用者放棄。查不出來時**晚一輪
        再說**是免費的，猜錯方向不是。
        """
        reference = topup.payment_transaction_id.reference
        if not reference:
            return False
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._REPLAY_LOCK_TIMEOUT,))
                cr.execute(
                    "SELECT 1 FROM woow_billing_incident "
                    "WHERE external_ref = %s AND kind = 'intent' "
                    "AND rtn_code = '1' AND trade_no IS NOT NULL "
                    "AND state <> 'resolved' LIMIT 1",
                    (reference,),
                )
                return cr.fetchone() is not None
        except Exception:
            _logger.error(
                '[TOPUP] callback evidence gate unavailable for topup=%s; '
                'deferring hard-cancel one round rather than cancelling a '
                'top-up that may already be paid', topup.id,
            )
            return True

    @api.model
    def _cron_cleanup_pending_topups(self):
        """Reconcile pending topups whose callback was missed / dropped.

        - tx already done → credit (catch-up).
        - tx error/cancel → mark topup failed.
        - tx still pending > HARD_CANCEL_MIN → cancel tx + mark topup cancelled,
          **除非** inbox 裡有還沒補完的成功 callback（#864 PR-G）。

        🔴 #864 PR-G：``_cron_replay_callback_inbox()`` 掛在這裡、**在迴圈之前**
        呼叫，理由有二：
        (1) ⛔ 不新增 cron，也 ⛔ 不改 ``src/data/cron_topup.xml``——那個
            ``<record>`` 在 ``noupdate="1"`` 區塊裡，既有安裝升版時對它的更新會
            被靜默 skip ⇒ 「測試（fresh install）全綠、stg 舊 DB 上 replay 根本
            不會跑」。從程式碼呼叫繞開整個 sticky 問題。
        (2) **replay 必須先於 hard-cancel**：反過來的話 cleanup 會先把這一筆標
            成 ``cancelled``，replay 隨後只能轉人工，自癒就失效了。

        🔴 兩段是**各自的 failure domain**（裁決②）：replay 段拋例外 ⛔ 不得讓
        cleanup 段死掉；反向也成立——replay 是在自己的獨立 cursor 上 commit 的，
        cleanup 段之後在 cron cursor 上出事，結構上回捲不掉它。
        """
        try:
            self._cron_replay_callback_inbox()
        except Exception:
            _logger.exception(
                '[TOPUP] cron replay segment failed; the cleanup segment '
                'below still runs (separate failure domains)',
            )
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
                # #864 PR-G：ECPay 已經回報成功、只是還沒補完的那一筆 ⛔ 不能
                # 被 hard-cancel（那正是本病的末期形態：看起來像使用者放棄）。
                if self._has_unreplayed_callback_evidence(topup):
                    _logger.info(
                        "[TOPUP] cron cleanup hard-cancel skipped "
                        "(successful callback pending replay) topup=%s tx=%s",
                        topup.id, tx.id,
                    )
                    continue
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
