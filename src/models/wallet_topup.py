import logging
import math
import uuid
from datetime import timedelta

import psycopg2
import requests

from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import AccessError, ValidationError

from .account_move_invoice_status import _ecpay_mtn_write
from .wallet_transaction import _wallet_credit_metadata

_logger = logging.getLogger(__name__)

# JSON/RPC 無法偽造 object identity。只有 query-gated PR-F repair path 可帶
# 此 capability，用來抑制 same-transaction invoice creation。
_AUTO_RECREDIT_CAPABILITY = object()
_AUTO_RECREDIT_SKIP_INVOICE_CTX = '_woow_auto_recredit_skip_invoice'


def _is_auto_recredit_skip_invoice(env):
    return env.context.get(_AUTO_RECREDIT_SKIP_INVOICE_CTX) is _AUTO_RECREDIT_CAPABILITY


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
    # Branch-B reconstructed row 沒有正常 callback lifecycle；此 scalar
    # ledger link 防止 callback/cleanup 繞過 PR-F safety gates。
    repair_incident_ref = fields.Integer(
        index=True,
        readonly=True,
        groups='base.group_system',
    )

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

    def init(self):
        """self table 建好後才建立跨 topup/account.move 的 MTN backstop。"""
        parent_init = getattr(super(), 'init', None)
        if parent_init:
            parent_init()
        self.env['account.move']._init_woow_ecpay_mtn_index()

    @api.model_create_multi
    def create(self, values_list):
        if (not _is_auto_recredit_skip_invoice(self.env)
                and any(values.get('repair_incident_ref')
                        for values in values_list)):
            raise AccessError(_(
                'Repair incident references are managed only by the '
                'query-gated repair process.'
            ))
        return super().create(values_list)

    def write(self, values):
        if ('repair_incident_ref' in values
                and not _is_auto_recredit_skip_invoice(self.env)):
            raise AccessError(_(
                'Repair incident references are managed only by the '
                'query-gated repair process.'
            ))
        return super().write(values)

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

    # ── 自動補值的最低扣款金額 ────────────────────────────────────────────
    # ECPay 每筆信用卡交易收「手續費率」與「最低手續費 NT$5」取大者：缺口 12 元
    # 就只刷 12 元時，手續費佔 42%。把實際扣款拉到這個下限，多收的部分**留在錢包**
    # 抵下一期（與下面 ceil 的餘額同一個語意，只是金額大一點）。
    # ⚠ 這是對使用者的卡片扣款，金額大於當期欠款 ⇒ 產品面要在自動補值的說明與
    #   同意流程揭露；設成 0 即關閉下限、回到「缺多少刷多少」。
    _AUTO_TOPUP_MIN_PARAM = 'woow_paas_platform.auto_topup_min_amount'
    _AUTO_TOPUP_MIN_DEFAULT = '100'

    @api.model
    def _auto_topup_min_amount(self):
        """自動補值的最低扣款金額（整數 TWD；0 ＝ 不設下限）。

        🔴 fail-safe 往「不設下限」倒：參數被填成非數字時，寧可回到舊行為
        （缺多少刷多少）也不要用一個猜的數字去多刷使用者的卡。
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            self._AUTO_TOPUP_MIN_PARAM, self._AUTO_TOPUP_MIN_DEFAULT)
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            _logger.warning(
                '[AUTO_TOPUP] %s=%r is not a number; charging the exact '
                'shortfall (no minimum)', self._AUTO_TOPUP_MIN_PARAM, raw,
            )
            return 0.0
        return float(max(value, 0))

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

        The charge is then raised to ``_auto_topup_min_amount()`` when the
        shortfall is below it (ECPay's NT$5 minimum fee makes tiny charges
        uneconomic); that surplus also stays in the wallet.
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
        # 最低扣款金額（見 `_auto_topup_min_amount`）。只抬高、不壓低——缺口大於
        # 下限時照缺口刷。credits 必須跟著一起抬，三邊（錢包／發票／ECPay 扣款）
        # 才對得起來。
        charge_amount = max(ceil_amount, self._auto_topup_min_amount())
        if charge_amount != ceil_amount:
            _logger.info(
                '[AUTO_TOPUP] shortfall %s raised to the minimum charge %s '
                'partner=%s; the surplus stays in the wallet',
                ceil_amount, charge_amount, partner.id,
            )

        twd = self.env.ref('base.TWD', raise_if_not_found=True)
        vals = {
            'partner_id': partner.id,
            'amount': charge_amount,
            'credits': charge_amount,
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

        # 外呼前先凍結 durable incident 所需的所有純 scalar。失敗後 PostgreSQL
        # transaction 可能已 aborted；except 區塊不得再 lazy-read ORM 欄位。
        origin_xid = self._origin_xid()
        cni_snapshot = self._freeze_charged_not_credited_snapshot(
            tx, token, charge_amount, currency, origin_xid=origin_xid,
        )
        try:
            result = tx._send_payment_request()
        except Exception as e:
            cached_tx_state = tx.env.cache.get(
                tx, tx._fields['state'], None)
            if cached_tx_state == 'done':
                _logger.exception(
                    'CRITICAL: auto-topup callback confirmed a charge but '
                    'wallet credit failed (topup=%s partner=%s)',
                    cni_snapshot['source_ref'], cni_snapshot['partner_ref'],
                )
                # #864 PR-E 順序：incident 先落地（獨立交易，即使自己失敗也
                # fail-soft 不拋），下面那個 `state='failed'` 標記才是保底。
                self._log_charged_not_credited_snapshot(cni_snapshot)
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
                'CRITICAL: auto-topup charge succeeded but post-charge '
                'bookkeeping failed (topup=%s partner=%s)',
                cni_snapshot['source_ref'], cni_snapshot['partner_ref'],
            )
            # #864 PR-E 順序：incident 先落地（獨立交易，即使自己失敗也
            # fail-soft 不拋），下面那個 `state='failed'` 標記才是保底。
            self._log_charged_not_credited_snapshot(cni_snapshot)
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

    def _origin_xid(self):
        """在 failure handling 前凍結 outer xid。

        ``pg_current_xact_id()`` 即使對原本可能沒有 xid 的 transaction，也會
        刻意配置真實 xid；此副作用已接受，因 Branch B 必須區分仍在執行與已被
        PostgreSQL abort 的 payment transaction。
        """
        self.env.cr.execute('SELECT pg_current_xact_id()::text')
        return self.env.cr.fetchone()[0]

    def _freeze_charged_not_credited_snapshot(
            self, tx, token, charge_amount, currency, *, origin_xid=None):
        """在外呼／入帳失敗區域之前凍結 CHARGED_NOT_CREDITED 純 scalar。"""
        self.ensure_one()
        return {
            'external_ref': tx.reference,
            'kind': 'charged_not_credited',
            'partner_ref': self.partner_id.id or None,
            'payment_token_ref': token.id if token else None,
            'amount': float(charge_amount),
            'credits': self.credits,
            'bonus': self.bonus,
            'currency': currency.name if currency else None,
            'failure_code': TOPUP_WALLET_CREDIT_FAILED,
            'source_model': self._name,
            'source_ref': self.id,
            'origin_xid': origin_xid,
            'topup_plan_ref': self.plan_id.id or None,
            'topup_source': self.source,
            'detail': (
                'ECPay 已接受扣款（tx.state=done）但入帳／記帳後續處理'
                '失敗；原始例外請查 woow.wallet.topup.failure_message '
                '與伺服器 log。'
            ),
        }

    def _log_charged_not_credited_snapshot(self, snapshot):
        """以獨立交易寫入已凍結 snapshot；此 method 不讀任何 ORM 欄位。"""
        try:
            incident_id = self.env[
                'woow.billing.incident']._log_charged_not_credited(snapshot)
            _logger.info(
                '[TOPUP_AUTO] charged_not_credited incident logged '
                'incident_id=%s', incident_id,
            )
            return incident_id
        except Exception:
            _logger.critical(
                '[TOPUP_AUTO] CRITICAL: charged_not_credited incident '
                'logging itself failed — the charge has already happened'
            )
            return False

    def _log_charged_not_credited_incident(
            self, tx, token, charge_amount, currency, *, origin_xid=None):
        """相容既有 caller：先凍結，再以獨立交易落 CNI incident。

        新的外呼失敗路徑必須在進入 failure region 前直接呼叫
        ``_freeze_charged_not_credited_snapshot``，不得在 except 內走本 wrapper。

        獨立交易落一筆 CHARGED_NOT_CREDITED incident（#864 PR-D／PR-E）。

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
        snapshot = self._freeze_charged_not_credited_snapshot(
            tx, token, charge_amount, currency, origin_xid=origin_xid,
        )
        return self._log_charged_not_credited_snapshot(snapshot)

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
        self.invalidate_recordset([
            'state', 'wallet_transaction_id', 'repair_incident_ref',
        ])
        if (self.repair_incident_ref
                and not _is_auto_recredit_skip_invoice(self.env)):
            raise ValidationError(_(
                'Reconstructed wallet top-ups may be credited only by the '
                'query-gated repair process.'
            ))
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
        # PR-F auto-recredit 先 commit 點數；invoice 另走 best-effort
        # transaction。journal failure 不得 rollback 已驗證的 ECPay credit；
        # reconstructed row 永不自動開票。
        if not _is_auto_recredit_skip_invoice(self.env):
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
    def _pending_reconciliation_blocker(self, topup):
        """用 fresh cursor 回傳 unresolved inbox/CNI blocker 與其 key。

        Cleanup 不可成為第二個 abandonment clock。tx 尚未建立／已 rollback 時，
        仍可用 durable incident 的 ``source_model/source_ref`` 找回 intent。
        """
        reference = (
            topup.payment_transaction_id.reference
            if topup.payment_transaction_id else None
        )
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._REPLAY_LOCK_TIMEOUT,))
                cr.execute(
                    "SELECT external_ref, kind FROM woow_billing_incident "
                    "WHERE state <> 'resolved' AND "
                    "(external_ref = %s OR "
                    "(source_model = %s AND source_ref = %s)) AND ("
                    "(kind = 'intent' AND rtn_code = '1' "
                    "AND trade_no IS NOT NULL) "
                    "OR kind = 'charged_not_credited') "
                    "ORDER BY id LIMIT 1",
                    (reference, self._name, topup.id),
                    log_exceptions=False,
                )
                row = cr.fetchone()
                return (
                    {'external_ref': row[0], 'kind': row[1]}
                    if row else False
                )
        except Exception:
            _logger.error(
                '[TOPUP] reconciliation blocker unavailable; '
                'deferring cancel topup=%s', topup.id)
            return {'external_ref': None, 'kind': 'unavailable'}

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
        return bool(self._pending_reconciliation_blocker(topup))

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
        try:
            self._cron_auto_recredit_charged_not_credited()
        except Exception:
            _logger.exception(
                '[TOPUP] PR-F auto-recredit failed; pending cleanup still runs',
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
                # hard age 前沒有 tx 不代表使用者放棄；到 hard age 仍必須先過
                # 同一個 durable blocker gate。
                if topup.create_date >= hard:
                    continue
                blocker = self._pending_reconciliation_blocker(topup)
                if blocker:
                    if blocker['external_ref']:
                        reason = (
                            'cleanup_waiting_auto_recredit'
                            if blocker['kind'] == 'charged_not_credited'
                            else 'cleanup_waiting_callback_replay'
                        )
                        self.env['woow.billing.incident'].sudo() \
                            ._mark_replay_skip_independent(
                                blocker['external_ref'], reason,
                                touch_last_skipped=False)
                    continue
                _logger.info("[TOPUP] cron cleanup no tx → cancelled topup=%s", topup.id)
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
                blocker = self._pending_reconciliation_blocker(topup)
                if blocker:
                    if blocker['external_ref']:
                        reason = (
                            'cleanup_waiting_auto_recredit'
                            if blocker['kind'] == 'charged_not_credited'
                            else 'cleanup_waiting_callback_replay'
                        )
                        self.env['woow.billing.incident'].sudo() \
                            ._mark_replay_skip_independent(
                                blocker['external_ref'], reason,
                                touch_last_skipped=False)
                    _logger.info(
                        '[TOPUP] cron cleanup hard-cancel skipped '
                        'blocker=%s topup=%s tx=%s',
                        blocker['kind'], topup.id, tx.id,
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

    # #864 PR-F：ECPG QueryTrade auto-recredit ------------------------ #
    _AUTO_RECREDIT_T0_MINUTES = 10
    _AUTO_RECREDIT_T1_MINUTES = 15
    _AUTO_RECREDIT_T2_MINUTES = 4 * 60
    _AUTO_RECREDIT_BATCH_SIZE = 5
    _AUTO_RECREDIT_LOCK_TIMEOUT = '1s'
    _AUTO_RECREDIT_PARAM = 'woow_paas_platform.auto_recredit_enabled'
    _AUTO_RECREDIT_HEARTBEAT_PARAM = 'woow_paas_platform.auto_recredit_heartbeat_at'
    # 封閉 vendor contract mapping。測試直接對 vendored enum 的 key set；
    # future addon 增值時必須 fail-closed，而非靜默 drift。
    _AUTO_RECREDIT_VERDICT_ACTIONS = {
        'safe': 'credit_after_second_query',
        'unsafe': 'needs_human',
        'indeterminate': 'retry_until_t2',
    }
    _AUTO_RECREDIT_REASON_ACTIONS = {
        'captured_amount_confirmed': 'credit_after_second_query',
        'not_found_or_not_visible': 'retry_short',
        'awaiting_capture': 'retry_short',
        'adverse_lifecycle': 'needs_human',
        'adverse_close_record': 'needs_human',
        'amount_mismatch': 'needs_human',
        'closed_amount_mismatch': 'needs_human',
        'close_data_missing': 'retry_medium',
        'close_data_amount_mismatch': 'retry_medium',
        'unknown_lifecycle': 'retry_medium',
    }

    @api.model
    def _auto_recredit_enabled(self):
        """Malformed config 一律 fail-closed；kill-switch off-state 必須有聲。"""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            self._AUTO_RECREDIT_PARAM, 'True')
        if raw in ('True', '1', True):
            return True
        if raw in ('False', '0', False):
            return False
        _logger.error('[TOPUP_RECREDIT] invalid kill-switch value; fail closed')
        return False

    @api.model
    def _auto_recredit_candidates(self):
        """用 fresh snapshot 讀 candidates，不沿用 cron 的 RR snapshot。"""
        with self.env.registry.cursor() as cr:
            cr.execute('SET LOCAL lock_timeout = %s',
                       (self._AUTO_RECREDIT_LOCK_TIMEOUT,))
            cr.execute(
                "SELECT id, external_ref, partner_ref, payment_token_ref, amount, "
                "credits, bonus, currency, trade_no, source_ref, origin_xid, "
                "topup_plan_ref, topup_source, first_seen_at, query_attempts, "
                "t1_overdue_at FROM woow_billing_incident "
                "WHERE kind = 'charged_not_credited' AND state = 'open' "
                "AND failure_code = 'wallet_credit_failed' "
                "AND (next_query_at IS NULL OR next_query_at <= NOW()) "
                "ORDER BY first_seen_at, id LIMIT %s", (self._AUTO_RECREDIT_BATCH_SIZE,))
            names = ('id', 'external_ref', 'partner_ref', 'payment_token_ref',
                     'amount', 'credits', 'bonus', 'currency', 'trade_no',
                     'source_ref', 'origin_xid', 'topup_plan_ref',
                     'topup_source', 'first_seen_at', 'query_attempts',
                     't1_overdue_at')
            return [dict(zip(names, row)) for row in cr.fetchall()]

    @api.model
    def _auto_recredit_pg_capable(self):
        """任何 external query 前先探測 PostgreSQL >=13 xid8 support。

        ``pg_current_xact_id`` 會刻意配置 xid；此副作用已接受，Branch B 才能
        區分 in-progress 與 aborted work。
        """
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SELECT pg_xact_status(pg_current_xact_id())')
                return cr.fetchone()[0] == 'in progress'
        except Exception:
            _logger.exception('[TOPUP_RECREDIT] PostgreSQL xid8 capability unavailable')
            return False

    @api.model
    def _auto_recredit_timing(self, candidate, now):
        first_seen = fields.Datetime.to_datetime(candidate['first_seen_at'])
        age = now - first_seen
        if age >= timedelta(minutes=self._AUTO_RECREDIT_T2_MINUTES):
            return 't2'
        if age < timedelta(minutes=self._AUTO_RECREDIT_T0_MINUTES):
            return 't0'
        return 't1' if age >= timedelta(minutes=self._AUTO_RECREDIT_T1_MINUTES) else 'ready'

    @api.model
    def _auto_recredit_next(self, candidate, minutes):
        """Durable retry 時間含 deterministic jitter，且永不越過 T2。"""
        first_seen = fields.Datetime.to_datetime(candidate['first_seen_at'])
        deadline = first_seen + timedelta(
            minutes=self._AUTO_RECREDIT_T2_MINUTES)
        jitter_seconds = (
            candidate['id'] * 17 + candidate['query_attempts'] * 13
        ) % 31
        return min(
            fields.Datetime.now()
            + timedelta(minutes=minutes, seconds=jitter_seconds),
            deadline,
        )

    @api.model
    def _auto_recredit_retry_minutes(self, candidate, category):
        if category == 'short':
            return 2
        if category == 'medium':
            return 5
        attempt = max(1, int(candidate['query_attempts'] or 0) + 1)
        return min(2 ** attempt, 30)

    @api.model
    def _auto_recredit_expected_amount(self, candidate):
        amount = candidate['amount']
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or amount <= 0 or int(amount) != amount):
            return False
        return int(amount)

    @api.model
    def _auto_recredit_credit_cap_ok(self, candidate):
        total = (candidate['credits'] or 0) + (candidate['bonus'] or 0)
        if total <= 0 or not math.isfinite(total):
            return False
        plans = self.env['woow_paas_platform.topup_plan'].sudo().search([
            ('active', '=', True)])
        cap = max((p.credits + p.bonus for p in plans), default=0)
        return bool(cap and total <= cap)

    @api.model
    def _auto_recredit_partner_daily_ok(self, candidate):
        """用 fresh cursor 守同一 partner 每個 UTC day 最多自動補一次。"""
        today = fields.Date.today()
        start = fields.Datetime.to_datetime(today)
        end = start + timedelta(days=1)
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._AUTO_RECREDIT_LOCK_TIMEOUT,))
                cr.execute(
                    "SELECT 1 FROM wallet_transaction "
                    "WHERE partner_id = %s "
                    "AND woow_credit_origin = 'auto_recredit' "
                    "AND create_date >= %s AND create_date < %s LIMIT 1",
                    (candidate['partner_ref'], start, end),
                    log_exceptions=False,
                )
                return cr.fetchone() is None
        except Exception:
            _logger.exception(
                '[TOPUP_RECREDIT] partner/day safety probe failed')
            return False

    @api.model
    def _auto_recredit_possible_manual_credit(self, candidate):
        """用 fresh cursor 偵測 incident 後的 support/manual credit。

        support adjustment 未必帶 payment tx FK；因此用同 partner＋first_seen
        後相同點數做保守偵測：寧可轉人工，也不可補第二次。
        """
        total = (candidate['credits'] or 0) + (candidate['bonus'] or 0)
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._AUTO_RECREDIT_LOCK_TIMEOUT,))
                cr.execute(
                    "SELECT 1 FROM wallet_transaction WHERE partner_id = %s "
                    "AND create_date >= %s AND amount = %s "
                    "AND COALESCE(woow_repair_incident_ref, 0) <> %s LIMIT 1",
                    (candidate['partner_ref'], candidate['first_seen_at'], total,
                     candidate['id']), log_exceptions=False)
                return cr.fetchone() is not None
        except Exception:
            # 此 guard 保護不可逆 credit；probe unavailable 不得解讀成「沒有
            # manual credit」。
            _logger.exception('[TOPUP_RECREDIT] manual-credit safety probe failed')
            return True

    @api.model
    def _auto_recredit_provider(self, candidate):
        """query 前解析 provider，讓 typed 403 能開啟正確 shared scope。"""
        token = self.env['payment.token'].sudo().browse(
            candidate['payment_token_ref']).exists()
        provider = token.provider_id if token else self.env['payment.provider']
        if not provider:
            # Branch A 仍可由 payment.transaction 找回 provider。
            topup = self.sudo().browse(candidate['source_ref']).exists()
            provider = topup.payment_transaction_id.provider_id if topup else provider
        if (not provider
                or provider.code != 'ECPay'
                or not provider.enable_ecpg
                or not provider.MerchantID
                or not provider.HashKey
                or not provider.HashIV):
            raise ValueError(
                'missing or invalid ECPay provider for durable incident')
        return provider

    @api.model
    def _auto_recredit_query(self, candidate, provider=None):
        """呼叫一次 vendor public interface；平台絕不解析 provider raw status。"""
        from odoo.addons.payment_ecpay_ecpg.sdk import ECPGClient
        provider = provider or self._auto_recredit_provider(candidate)
        client = ECPGClient(
            provider.MerchantID, provider.HashKey, provider.HashIV,
            sandbox=(provider.state != 'enabled'),
        )
        lookup = client.query_credit_trade(candidate['external_ref'])
        return provider, lookup.assess_for_credit(
            expected_amount=self._auto_recredit_expected_amount(candidate))

    @api.model
    def _auto_recredit_mark_terminal(
            self, candidate, reason, assessment_reason=None):
        marked = self.env[
            'woow.billing.incident'].sudo()._transition_needs_human_independent(
                candidate['external_ref'], reason, assessment_reason)
        if not marked:
            return 'failed', 'terminal_transition_failed'
        return 'needs_human', reason

    @api.model
    def _auto_recredit_handle_query_exception(
            self, candidate, provider, exc, *, t1_overdue=False):
        """窮舉 vendored error interface；config/protocol 類不可盲目 retry。"""
        from odoo.addons.payment_ecpay_ecpg.sdk import (
            ECPGConfigError, ECPGCreditTradeQueryError, ECPGProtocolError,
            ECPGQueryForbiddenError, ECPGTransientError,
        )
        Incident = self.env['woow.billing.incident'].sudo()
        if isinstance(exc, ECPGQueryForbiddenError):
            if not provider:
                return self._auto_recredit_mark_terminal(
                    candidate, 'query_invalid_input')
            try:
                count, blocked_until = self.env[
                    'woow_paas_platform.ecpg_query_circuit'].sudo() \
                    ._record_forbidden(
                        provider.MerchantID, provider.id,
                        exc.retry_after_seconds)
            except Exception:
                _logger.exception(
                    '[TOPUP_RECREDIT] cannot persist shared 403 circuit')
                return self._auto_recredit_mark_terminal(
                    candidate, 'query_circuit_open')
            Circuit = self.env[
                'woow_paas_platform.ecpg_query_circuit'].sudo()
            if (count >= 2
                    and Circuit._claim_repeat_forbidden_alert(
                        provider.MerchantID)):
                Incident._record_query_outcome_independent(
                    candidate['external_ref'],
                    reason='query_circuit_open',
                    increment_attempt=True,
                    t1_overdue=t1_overdue,
                )
                return self._auto_recredit_mark_terminal(
                    candidate, 'query_circuit_open')
            deadline = fields.Datetime.to_datetime(
                candidate['first_seen_at']) + timedelta(
                    minutes=self._AUTO_RECREDIT_T2_MINUTES)
            recorded = Incident._record_query_outcome_independent(
                candidate['external_ref'],
                next_query_at=min(blocked_until, deadline),
                increment_attempt=True,
                skip_reason='query_circuit_open',
                t1_overdue=t1_overdue,
            )
            if not recorded:
                return 'failed', 'query_audit_failed'
            return 'skipped', 'query_circuit_open'
        if isinstance(exc, (
                ECPGTransientError,
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
        )):
            recorded = Incident._record_query_outcome_independent(
                candidate['external_ref'],
                next_query_at=self._auto_recredit_next(
                    candidate,
                    self._auto_recredit_retry_minutes(
                        candidate, 'transient')),
                increment_attempt=True,
                skip_reason='query_transient',
                t1_overdue=t1_overdue,
            )
            if not recorded:
                return 'failed', 'query_audit_failed'
            return 'skipped', 'query_transient'
        if isinstance(exc, ECPGConfigError):
            terminal_reason = 'query_config_error'
        elif isinstance(exc, ECPGProtocolError):
            terminal_reason = 'query_protocol_error'
        elif isinstance(exc, ECPGCreditTradeQueryError):
            terminal_reason = 'query_business_error'
        else:
            terminal_reason = 'query_invalid_input'
        Incident._record_query_outcome_independent(
            candidate['external_ref'],
            reason=terminal_reason,
            increment_attempt=True,
            t1_overdue=t1_overdue,
        )
        return self._auto_recredit_mark_terminal(
            candidate, terminal_reason)

    @api.model
    def _auto_recredit_assessment_action(self, assessment):
        """驗證 verdict/reason 是 vendored contract 允許的封閉組合。"""
        verdict = assessment.verdict.value
        reason = assessment.reason.value
        action = self._AUTO_RECREDIT_VERDICT_ACTIONS.get(verdict)
        reason_action = self._AUTO_RECREDIT_REASON_ACTIONS.get(reason)
        valid = (
            action == 'credit_after_second_query'
            and reason_action == 'credit_after_second_query'
            or action == 'needs_human'
            and reason_action == 'needs_human'
            or action == 'retry_until_t2'
            and reason_action in ('retry_short', 'retry_medium')
        )
        return action if valid else False

    @api.model
    def _auto_recredit_record_assessment(
            self, candidate, assessment, timing, *, next_query_at=None,
            skip_reason=None):
        """每一次真 HTTP query 都 durable 記錄 verdict/reason/attempt。"""
        return self.env[
            'woow.billing.incident'].sudo()._record_query_outcome_independent(
                candidate['external_ref'],
                verdict=assessment.verdict.value,
                reason=assessment.reason.value,
                next_query_at=next_query_at,
                increment_attempt=True,
                skip_reason=skip_reason,
                t1_overdue=(timing == 't1'),
            )

    @api.model
    def _auto_recredit_one(self, candidate):
        """在 credit transaction 之外連續查兩次；第二次仍 SAFE 才補。"""
        Incident = self.env['woow.billing.incident'].sudo()
        now = fields.Datetime.now()
        timing = self._auto_recredit_timing(candidate, now)
        if timing == 't1' and not candidate.get('t1_overdue_at'):
            _logger.warning(
                '[TOPUP_RECREDIT] incident exceeded soft T1; '
                'query/retry remains enabled incident_id=%s',
                candidate['id'],
            )
        if timing == 't2':
            return self._auto_recredit_mark_terminal(
                candidate, 't2_expired')
        if not self._auto_recredit_enabled():
            return self._auto_recredit_mark_terminal(
                candidate, 'kill_switch_off')
        if not self._auto_recredit_expected_amount(candidate):
            return self._auto_recredit_mark_terminal(
                candidate, 'query_invalid_input')
        if not self._auto_recredit_credit_cap_ok(candidate):
            return self._auto_recredit_mark_terminal(
                candidate, 'credit_cap_exceeded')
        if not self._auto_recredit_partner_daily_ok(candidate):
            return self._auto_recredit_mark_terminal(
                candidate, 'partner_daily_limit')
        if self._auto_recredit_possible_manual_credit(candidate):
            return self._auto_recredit_mark_terminal(
                candidate, 'possible_manual_credit')
        if timing == 't0':
            recorded = Incident._record_query_outcome_independent(
                candidate['external_ref'],
                next_query_at=(
                    fields.Datetime.to_datetime(candidate['first_seen_at'])
                    + timedelta(minutes=self._AUTO_RECREDIT_T0_MINUTES)
                ),
                increment_attempt=False,
                skip_reason='too_early',
            )
            return (
                ('skipped', 'too_early') if recorded
                else ('failed', 'query_audit_failed')
            )

        try:
            provider = self._auto_recredit_provider(candidate)
        except Exception:
            # provider/config lookup 尚未發出 HTTP，不能灌大 query_attempts。
            return self._auto_recredit_mark_terminal(
                candidate, 'query_invalid_input')
        try:
            # circuit 用 fresh cursor 讀，且 next_query_at 必 clamp 到 T2。
            blocked_until = self.env[
                'woow_paas_platform.ecpg_query_circuit'].sudo() \
                ._blocked_until(provider.MerchantID)
        except Exception:
            _logger.exception(
                '[TOPUP_RECREDIT] shared circuit lookup failed')
            return self._auto_recredit_mark_terminal(
                candidate, 'query_circuit_open')
        if blocked_until and blocked_until > now:
            deadline = fields.Datetime.to_datetime(
                candidate['first_seen_at']) + timedelta(
                    minutes=self._AUTO_RECREDIT_T2_MINUTES)
            recorded = Incident._record_query_outcome_independent(
                candidate['external_ref'],
                next_query_at=min(blocked_until, deadline),
                increment_attempt=False,
                skip_reason='query_circuit_open',
                t1_overdue=(timing == 't1'),
            )
            return (
                ('skipped', 'query_circuit_open') if recorded
                else ('failed', 'query_audit_failed')
            )
        try:
            provider, assessment = self._auto_recredit_query(
                candidate, provider)
            self.env[
                'woow_paas_platform.ecpg_query_circuit'].sudo() \
                ._record_success(provider.MerchantID)
        except Exception as exc:
            return self._auto_recredit_handle_query_exception(
                candidate, provider, exc, t1_overdue=(timing == 't1'))

        action = self._auto_recredit_assessment_action(assessment)
        if not action:
            self._auto_recredit_record_assessment(
                candidate, assessment, timing)
            return self._auto_recredit_mark_terminal(
                candidate, 'query_invalid_input')
        if action == 'needs_human':
            self._auto_recredit_record_assessment(
                candidate, assessment, timing)
            return self._auto_recredit_mark_terminal(
                candidate, 'query_unsafe', assessment.reason.value)
        if action == 'retry_until_t2':
            reason_action = self._AUTO_RECREDIT_REASON_ACTIONS[
                assessment.reason.value]
            minutes = self._auto_recredit_retry_minutes(
                candidate,
                'short' if reason_action == 'retry_short' else 'medium',
            )
            recorded = self._auto_recredit_record_assessment(
                candidate, assessment, timing,
                next_query_at=self._auto_recredit_next(candidate, minutes),
                skip_reason='query_indeterminate',
            )
            return (
                ('skipped', 'query_indeterminate') if recorded
                else ('failed', 'query_audit_failed')
            )

        # 第一次 SAFE 也要留下 query attempt，不能只記失敗。
        if not self._auto_recredit_record_assessment(
                candidate, assessment, timing):
            return 'failed', 'query_audit_failed'

        # Query 與 credit 非原子：真正動錢前再做一次 fresh query。
        try:
            _provider, second = self._auto_recredit_query(
                candidate, provider)
            self.env[
                'woow_paas_platform.ecpg_query_circuit'].sudo() \
                ._record_success(provider.MerchantID)
        except Exception as exc:
            return self._auto_recredit_handle_query_exception(
                candidate, provider, exc, t1_overdue=(timing == 't1'))

        second_action = self._auto_recredit_assessment_action(second)
        if not second_action:
            self._auto_recredit_record_assessment(
                candidate, second, timing)
            return self._auto_recredit_mark_terminal(
                candidate, 'query_invalid_input')
        if second_action == 'needs_human':
            self._auto_recredit_record_assessment(
                candidate, second, timing)
            return self._auto_recredit_mark_terminal(
                candidate, 'query_unsafe', second.reason.value)
        if second_action == 'retry_until_t2':
            second_reason_action = self._AUTO_RECREDIT_REASON_ACTIONS[
                second.reason.value]
            minutes = self._auto_recredit_retry_minutes(
                candidate,
                'short' if second_reason_action == 'retry_short'
                else 'medium',
            )
            recorded = self._auto_recredit_record_assessment(
                candidate, second, timing,
                next_query_at=self._auto_recredit_next(candidate, minutes),
                skip_reason='query_indeterminate',
            )
            return (
                ('skipped', 'query_indeterminate') if recorded
                else ('failed', 'query_audit_failed')
            )
        if not second.is_safe_to_credit:
            return self._auto_recredit_mark_terminal(
                candidate, 'query_invalid_input')
        if not self._auto_recredit_record_assessment(
                candidate, second, timing):
            return 'failed', 'query_audit_failed'
        return self._auto_recredit_credit(
            candidate, second.reason.value,
            provider.id, provider.MerchantID)

    @api.model
    def _auto_recredit_credit(
            self, candidate, assessment_reason, provider_id, merchant_id):
        """每筆獨立 transaction：scanner NOWAIT，入帳沿用 G blocking choke。"""
        external_ref = candidate['external_ref']
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._AUTO_RECREDIT_LOCK_TIMEOUT,))
                env = api.Environment(cr, SUPERUSER_ID, {})
                cr.execute(
                    'SELECT id FROM woow_wallet_topup WHERE id = %s '
                    'FOR UPDATE NOWAIT',
                    (candidate['source_ref'],),
                    log_exceptions=False,
                )
                row = cr.fetchone()
                if not row:
                    cr.rollback()
                else:
                    topup = env['woow.wallet.topup'].sudo().browse(row[0])
                    topup.invalidate_recordset([
                        'state', 'wallet_transaction_id',
                        'payment_transaction_id', 'repair_incident_ref',
                    ])
                    if topup.wallet_transaction_id:
                        env['woow.billing.incident'].browse(
                            candidate['id']
                        )._transition_resolved_current_cursor(
                            None,
                            'PR-F 確認該 top-up 已有 wallet transaction。',
                            SUPERUSER_ID,
                        )
                        cr.commit()
                        return 'already_credited', 'already_credited'

                    tx = topup.payment_transaction_id
                    card = topup.partner_id.get_wallet_loyalty_card()
                    if not card:
                        cr.rollback()
                        return self._auto_recredit_mark_terminal(
                            candidate, 'reconstruct_incomplete',
                            assessment_reason)
                    # 固定鎖序：topup → loyalty card → incident。
                    cr.execute(
                        'SELECT id FROM loyalty_card WHERE id = %s '
                        'FOR UPDATE NOWAIT', (card.id,),
                        log_exceptions=False)
                    if not cr.fetchone():
                        cr.rollback()
                        return self._auto_recredit_mark_terminal(
                            candidate, 'reconstruct_incomplete',
                            assessment_reason)
                    cr.execute(
                        'SELECT state, kind, failure_code, partner_ref, '
                        'payment_token_ref, amount, credits, bonus, currency, '
                        'source_ref, topup_plan_ref, topup_source '
                        'FROM woow_billing_incident WHERE id = %s FOR UPDATE',
                        (candidate['id'],), log_exceptions=False)
                    incident_row = cr.fetchone()
                    incident_names = (
                        'state', 'kind', 'failure_code', 'partner_ref',
                        'payment_token_ref', 'amount', 'credits', 'bonus',
                        'currency', 'source_ref', 'topup_plan_ref',
                        'topup_source',
                    )
                    incident = (
                        dict(zip(incident_names, incident_row))
                        if incident_row else None
                    )
                    expected_snapshot = {
                        name: candidate[name] for name in (
                            'partner_ref', 'payment_token_ref', 'amount',
                            'credits', 'bonus', 'currency', 'source_ref',
                            'topup_plan_ref', 'topup_source',
                        )
                    }
                    incident_snapshot = (
                        {name: incident[name] for name in expected_snapshot}
                        if incident else None
                    )
                    valid = bool(
                        incident
                        and incident['state'] == 'open'
                        and incident['kind'] == 'charged_not_credited'
                        and incident['failure_code'] ==
                        TOPUP_WALLET_CREDIT_FAILED
                        and incident_snapshot == expected_snapshot
                        and topup.state == 'failed'
                        and topup.failure_code ==
                        TOPUP_WALLET_CREDIT_FAILED
                        and not topup.repair_incident_ref
                        and topup.partner_id.id == candidate['partner_ref']
                        and topup.plan_id.id ==
                        (candidate['topup_plan_ref'] or False)
                        and topup.source == candidate['topup_source']
                        and topup.amount == candidate['amount']
                        and topup.credits == candidate['credits']
                        and topup.bonus == candidate['bonus']
                        and topup.currency_id.name == candidate['currency']
                        and tx
                        and tx.woow_topup_id.id == topup.id
                        and tx.state == 'done'
                        and tx.reference == external_ref
                        and tx.partner_id.id == candidate['partner_ref']
                        and tx.provider_id.id == provider_id
                        and tx.provider_id.MerchantID == merchant_id
                        and tx.token_id.id == candidate['payment_token_ref']
                        and tx.amount == candidate['amount']
                        and tx.currency_id.name == candidate['currency']
                    )
                    if not valid:
                        cr.rollback()
                        return self._auto_recredit_mark_terminal(
                            candidate, 'reconstruct_incomplete',
                            assessment_reason)

                    topup.with_context(**{
                        _AUTO_RECREDIT_SKIP_INVOICE_CTX:
                            _AUTO_RECREDIT_CAPABILITY,
                    })._on_payment_done(tx)
                    topup.invalidate_recordset(['wallet_transaction_id'])
                    wtx = topup.wallet_transaction_id
                    if not wtx:
                        raise ValidationError(_(
                            'Wallet credit completed without a wallet '
                            'transaction record.'
                        ))
                    _wallet_credit_metadata(wtx).write({
                        'woow_credit_origin': 'auto_recredit',
                        'woow_repair_incident_ref': candidate['id'],
                    })
                    cr.execute(
                        'UPDATE woow_billing_incident '
                        'SET wallet_transaction_ref = %s, write_uid = %s, '
                        'write_date = %s WHERE id = %s',
                        (wtx.id, SUPERUSER_ID, fields.Datetime.now(),
                         candidate['id']),
                        log_exceptions=False,
                    )
                    env['woow.billing.incident'].browse(
                        candidate['id']
                    )._transition_resolved_current_cursor(
                        None,
                        'PR-F QueryTrade confirmed capture; wallet credited.',
                        SUPERUSER_ID,
                    )
                    topup_id = topup.id
                    cr.commit()
                    # 發票一定在點數＋incident commit 之後另開 transaction。
                    self._auto_recredit_invoice_followup(topup_id)
                    return 'credited', 'captured_amount_confirmed'

            # source row 不存在時另開 READ COMMITTED cursor；Branch B 必須
            # 先鎖 incident，不能沿用上面已建立 RR snapshot 的 cursor。
            return self._auto_recredit_branch_b(
                candidate, assessment_reason, provider_id, merchant_id)
        except psycopg2.errors.LockNotAvailable:
            marked = self.env['woow.billing.incident'].sudo() \
                ._mark_replay_skip_independent(
                    external_ref, 'lock_not_available',
                    touch_last_skipped=False)
            return (
                ('skipped', 'lock_not_available') if marked
                else ('failed', 'skip_persistence_failed')
            )
        except psycopg2.errors.SerializationFailure:
            self.env['woow.billing.incident'].sudo() \
                ._mark_replay_skip_independent(
                    external_ref, 'credit_failed', bump_attempts=True)
            return 'failed', 'serialization_failure'
        except Exception:
            _logger.exception(
                '[TOPUP_RECREDIT] one credit transaction failed')
            self.env['woow.billing.incident'].sudo() \
                ._mark_replay_skip_independent(
                    external_ref, 'credit_failed', bump_attempts=True)
            return 'failed', 'credit_failed'

    @api.model
    def _auto_recredit_branch_b(
            self, candidate, assessment_reason, provider_id, merchant_id):
        """只有 origin transaction 確認 aborted 才可 reconstruct。"""
        if (not candidate.get('origin_xid')
                or not str(candidate['origin_xid']).isdigit()):
            return self._auto_recredit_mark_terminal(
                candidate, 'reconstruct_incomplete', assessment_reason)
        try:
            with self.env.registry.cursor() as cr:
                cr.execute('SET LOCAL lock_timeout = %s',
                           (self._AUTO_RECREDIT_LOCK_TIMEOUT,))
                # topup 查無後才來到此處；新 cursor 在第一個 query 前改成
                # READ COMMITTED，才能看見 origin transaction 隨後的 commit。
                cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                env = api.Environment(cr, SUPERUSER_ID, {})
                cr.execute(
                    'SELECT state, kind, failure_code, partner_ref, '
                    'payment_token_ref, amount, credits, bonus, currency, '
                    'trade_no, source_ref, origin_xid, topup_plan_ref, '
                    'topup_source FROM woow_billing_incident '
                    'WHERE id = %s FOR UPDATE',
                    (candidate['id'],), log_exceptions=False)
                row = cr.fetchone()
                names = (
                    'state', 'kind', 'failure_code', 'partner_ref',
                    'payment_token_ref', 'amount', 'credits', 'bonus',
                    'currency', 'trade_no', 'source_ref', 'origin_xid',
                    'topup_plan_ref', 'topup_source',
                )
                current = dict(zip(names, row)) if row else None
                expected = {
                    name: candidate[name] for name in (
                        'partner_ref', 'payment_token_ref', 'amount',
                        'credits', 'bonus', 'currency', 'trade_no',
                        'source_ref', 'origin_xid', 'topup_plan_ref',
                        'topup_source',
                    )
                }
                current_snapshot = (
                    {name: current[name] for name in expected}
                    if current else None
                )
                if (not current
                        or current['state'] != 'open'
                        or current['kind'] != 'charged_not_credited'
                        or current['failure_code'] !=
                        TOPUP_WALLET_CREDIT_FAILED
                        or current_snapshot != expected):
                    cr.rollback()
                    return self._auto_recredit_mark_terminal(
                        candidate, 'reconstruct_incomplete',
                        assessment_reason)

                cr.execute(
                    'SELECT pg_xact_status(%s::xid8)',
                    (candidate['origin_xid'],),
                    log_exceptions=False)
                status = cr.fetchone()[0]
                if status == 'in progress':
                    next_query_at = self._auto_recredit_next(candidate, 2)
                    now = fields.Datetime.now()
                    cr.execute(
                        'UPDATE woow_billing_incident '
                        'SET next_query_at = %s, '
                        "skip_reason = 'origin_transaction_in_progress', "
                        'last_skipped_at = %s, skip_count = skip_count + 1, '
                        'write_uid = %s, write_date = %s WHERE id = %s',
                        (next_query_at, now, SUPERUSER_ID, now,
                         candidate['id']),
                        log_exceptions=False,
                    )
                    cr.commit()
                    return 'skipped', 'origin_transaction_in_progress'
                if status == 'committed':
                    cr.execute(
                        'SELECT wt.id FROM woow_wallet_topup wt '
                        'JOIN payment_transaction pt '
                        'ON pt.id = wt.payment_transaction_id '
                        'WHERE wt.id = %s AND pt.reference = %s LIMIT 1',
                        (candidate['source_ref'],
                         candidate['external_ref']),
                        log_exceptions=False,
                    )
                    visible_topup = cr.fetchone()
                    cr.rollback()
                    if visible_topup:
                        return self._auto_recredit_credit(
                            candidate, assessment_reason,
                            provider_id, merchant_id)
                    return self._auto_recredit_mark_terminal(
                        candidate, 'source_missing_after_commit',
                        assessment_reason)
                if status != 'aborted':
                    cr.rollback()
                    return self._auto_recredit_mark_terminal(
                        candidate, 'reconstruct_incomplete',
                        assessment_reason)
                return self._auto_recredit_reconstruct_current_cursor(
                    env, cr, candidate, assessment_reason,
                    provider_id, merchant_id)
        except psycopg2.errors.LockNotAvailable:
            marked = self.env['woow.billing.incident'].sudo() \
                ._mark_replay_skip_independent(
                    candidate['external_ref'], 'lock_not_available',
                    touch_last_skipped=False)
            return (
                ('skipped', 'lock_not_available') if marked
                else ('failed', 'skip_persistence_failed')
            )
        except Exception:
            _logger.exception(
                '[TOPUP_RECREDIT] branch-B reconstruction failed')
            self.env['woow.billing.incident'].sudo() \
                ._mark_replay_skip_independent(
                    candidate['external_ref'], 'credit_failed',
                    bump_attempts=True)
            return 'failed', 'reconstruction_failed'

    @api.model
    def _auto_recredit_invoice_followup(self, topup_id):
        """僅 Branch A：credit commit 後以 fresh cursor create/recover invoice。"""
        try:
            with self.env.registry.cursor() as cr:
                env = api.Environment(cr, SUPERUSER_ID, {})
                topup = env['woow.wallet.topup'].sudo().browse(topup_id).exists()
                if topup and topup.state == 'done' and not topup.invoice_account_move_id:
                    topup._ensure_topup_invoice()
                cr.commit()
        except Exception:
            _logger.exception(
                '[TOPUP_RECREDIT] wallet credit committed but invoice follow-up failed topup=%s',
                topup_id,
            )

    @api.model
    def _auto_recredit_reconstruct_current_cursor(
            self, env, cr, candidate, assessment_reason,
            provider_id, merchant_id):
        """只重建已確認 aborted 且 scalar evidence 完整的付款；零外呼。"""
        partner = env['res.partner'].sudo().browse(
            candidate['partner_ref']).exists()
        token = env['payment.token'].sudo().browse(
            candidate['payment_token_ref']).exists()
        plan = (
            env['woow_paas_platform.topup_plan'].sudo().browse(
                candidate['topup_plan_ref']).exists()
            if candidate['topup_plan_ref'] else False
        )
        # Fresh DB 的 base.TWD 預設可能 active=False；一般 search 會被
        # active_test 靜默濾掉。XMLID 是此 currency 的 authoritative identity。
        currency = env.ref('base.TWD', raise_if_not_found=False)
        source_valid = (
            candidate['topup_source'] == 'manual'
            and bool(plan)
            or candidate['topup_source'] == 'auto'
            and not candidate['topup_plan_ref']
            and candidate['credits'] == candidate['amount']
            and candidate['bonus'] == 0
        )
        valid = bool(
            partner
            and token
            and token.partner_id.id == partner.id
            and token.provider_id.id == provider_id
            and token.provider_id.MerchantID == merchant_id
            and token.ecpg_bindcard_id
            and currency
            and currency.name == 'TWD'
            and candidate['currency'] == 'TWD'
            and source_valid
            and (not plan or (
                plan.amount == candidate['amount']
                and plan.credits == candidate['credits']
                and plan.bonus == candidate['bonus']))
        )
        if not valid:
            cr.rollback()
            return self._auto_recredit_mark_terminal(
                candidate, 'reconstruct_incomplete', assessment_reason)
        existing = env['payment.transaction'].sudo().search([
            ('reference', '=', candidate['external_ref']),
        ], limit=1)
        if existing:
            cr.rollback()
            return self._auto_recredit_mark_terminal(
                candidate, 'source_missing_after_commit', assessment_reason)

        topup = env['woow.wallet.topup'].sudo().with_context(**{
            _AUTO_RECREDIT_SKIP_INVOICE_CTX: _AUTO_RECREDIT_CAPABILITY,
        }).create({
            'partner_id': partner.id,
            'plan_id': candidate['topup_plan_ref'] or False,
            'amount': candidate['amount'],
            'credits': candidate['credits'],
            'bonus': candidate['bonus'],
            'currency_id': currency.id,
            'state': 'failed',
            'source': candidate['topup_source'],
            'failure_code': TOPUP_WALLET_CREDIT_FAILED,
            'repair_incident_ref': candidate['id'],
        })
        tx = env['payment.transaction'].sudo().create({
            'provider_id': token.provider_id.id,
            'payment_method_id': token.payment_method_id.id,
            'partner_id': partner.id,
            'amount': candidate['amount'],
            'currency_id': currency.id,
            'reference': candidate['external_ref'],
            'token_id': token.id,
            'operation': 'offline',
            'woow_topup_id': topup.id,
            # 只有 callback 寫入的 provider TradeNo 可進此欄；我方 MTN
            # 永遠留在 reference/external_ref。
            'provider_reference': candidate.get('trade_no') or False,
        })
        cr.execute(
            "UPDATE payment_transaction SET state = 'done' WHERE id = %s",
            (tx.id,), log_exceptions=False)
        tx.invalidate_recordset(['state'])
        topup.write({'payment_transaction_id': tx.id})
        card = partner.get_wallet_loyalty_card()
        if not card:
            raise ValidationError(_('Unable to get wallet loyalty card.'))
        cr.execute(
            'SELECT id FROM loyalty_card WHERE id = %s FOR UPDATE NOWAIT',
            (card.id,), log_exceptions=False)
        if not cr.fetchone():
            raise ValidationError(_('Unable to lock wallet loyalty card.'))
        topup.with_context(**{
            _AUTO_RECREDIT_SKIP_INVOICE_CTX: _AUTO_RECREDIT_CAPABILITY,
        })._on_payment_done(tx)
        topup.invalidate_recordset(['wallet_transaction_id'])
        wtx = topup.wallet_transaction_id
        if not wtx:
            raise ValidationError(_(
                'Wallet credit completed without a wallet transaction record.'
            ))
        _wallet_credit_metadata(wtx).write({
            'woow_credit_origin': 'auto_recredit',
            'woow_repair_incident_ref': candidate['id'],
        })
        cr.execute(
            'UPDATE woow_billing_incident '
            'SET wallet_transaction_ref = %s, repair_topup_ref = %s, '
            'write_uid = %s, write_date = %s WHERE id = %s',
            (wtx.id, topup.id, SUPERUSER_ID, fields.Datetime.now(),
             candidate['id']), log_exceptions=False)
        env['woow.billing.incident'].browse(
            candidate['id'])._transition_resolved_current_cursor(
                None,
                'PR-F reconstructed an aborted payment and credited wallet.',
                SUPERUSER_ID,
            )
        partner_id = partner.id
        topup_id = topup.id
        cr.commit()

        # 重建 topup 的 id 與原列不同，自動開電子發票有 duplicate invoice
        # 風險；另建人工 incident，禁止在本 transaction 開票。
        self.env['woow.billing.incident']._log({
            'external_ref': 'invoice:%s' % candidate['external_ref'],
            'kind': 'manual_invoice_stale',
            'state': 'needs_human',
            'partner_ref': partner_id,
            'source_model': 'woow.wallet.topup',
            'source_ref': topup_id,
            'detail': 'PR-F 重建儲值已補點，電子發票必須人工覆核。',
        })
        return 'reconstructed', 'origin_transaction_aborted'

    @api.model
    def _auto_recredit_stamp_heartbeat(self):
        """每輪無條件 stamp，讓另一顆 cron 分得出 disabled 與空 queue。"""
        self.env['ir.config_parameter'].sudo().set_param(
            self._AUTO_RECREDIT_HEARTBEAT_PARAM,
            fields.Datetime.to_string(fields.Datetime.now()),
        )

    @api.model
    def _cron_auto_recredit_charged_not_credited(self):
        """執行 QueryTrade auto-heal；每個 candidate 各自一筆 transaction。"""
        candidates = self._auto_recredit_candidates()
        summary = {
            'scanned': len(candidates),
            'credited': 0,
            'reconstructed': 0,
            'already_credited': 0,
            'needs_human': 0,
            'failed': 0,
            'skipped_by_reason': {},
        }
        if candidates and not self._auto_recredit_pg_capable():
            for candidate in candidates:
                outcome, reason = self._auto_recredit_mark_terminal(
                    candidate, 'postgres_xid_capability_missing')
                summary[outcome] = summary.get(outcome, 0) + 1
                summary['skipped_by_reason'][reason] = (
                    summary['skipped_by_reason'].get(reason, 0) + 1)
            self._auto_recredit_stamp_heartbeat()
            _logger.error(
                '[TOPUP_RECREDIT] PostgreSQL xid capability missing; '
                'scanned=%s needs_human=%s',
                summary['scanned'], summary['needs_human'])
            return summary
        for candidate in candidates:
            outcome, reason = self._auto_recredit_one(candidate)
            if outcome in summary:
                summary[outcome] += 1
            else:
                summary['failed'] += 1
            if reason and outcome in ('skipped', 'needs_human', 'failed'):
                summary['skipped_by_reason'][reason] = (
                    summary['skipped_by_reason'].get(reason, 0) + 1)
        self._auto_recredit_stamp_heartbeat()
        _logger.info(
            '[TOPUP_RECREDIT] scanned=%s credited=%s reconstructed=%s '
            'already_credited=%s needs_human=%s failed=%s '
            'skipped_by_reason=%s',
            summary['scanned'], summary['credited'],
            summary['reconstructed'], summary['already_credited'],
            summary['needs_human'], summary['failed'],
            summary['skipped_by_reason'],
        )
        return summary

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
        # 2026-10-14 政策 A：刷卡金額＝發票總額。稅一律明寫成 PaaS 專用 5% 內含稅（50 → 48＋2），
        # 不吃商品或公司預設稅——預設稅是 5% 外加時，50 元刷卡會開出 53 元發票／電子發票。
        # 公司沒有 5% 銷項稅可當來源時回空 ⇒ 不課稅（總額仍＝刷卡金額）。
        vat = self.env['product.template']._woow_paas_included_sale_tax(self.env.company)
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
                'tax_ids': [(6, 0, vat.ids)],
            })],
            'woow_invoice_issue_state': issue_state,
            # 同一 MTN 是 cross-topup invoice idempotency key；Branch-B
            # reconstruction 會取得新 topup id，所以不可再只靠 TOPUP-{id}。
            'woow_ecpay_mtn': self.payment_transaction_id.reference or False,
            # Path B: PaaS issues e-invoices via cron only. The skip flag
            # short-circuits the upstream `create_ecpay_invoice` action_post
            # hook so the same move isn't issued twice (once inline, once by
            # the cron). Required for both manual and auto topups.
            'woow_skip_upstream_hook': True,
        }
        move_vals.update(self._map_carrier_to_ecpay_vals())
        move = _ecpay_mtn_write(
            self.env['account.move'].sudo()).create(move_vals)
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
            # 政策 A：不吃公司預設稅（見 _create_topup_invoice）；之後每次 -u 由
            # _woow_sync_paas_included_vat 收斂。
            vat = self.env['product.template']._woow_paas_included_sale_tax(self.env.company)
            product = Product.create({
                'name': 'Wallet Topup',
                'default_code': 'WALLET_TOPUP',
                'type': 'service',
                'taxes_id': [(6, 0, vat.ids)],
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
