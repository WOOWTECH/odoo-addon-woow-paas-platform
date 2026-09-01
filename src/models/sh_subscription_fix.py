import base64
import logging
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from psycopg2 import OperationalError

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.misc import formatLang

_logger = logging.getLogger(__name__)

#: #389：同一個 warn_key（＝一個續費週期）最多追著重送幾天。
#:
#: 不能只靠 dunning 的 ``max_consecutive_payment_failures`` 當上限——那個值
#: **可以設 0**（Decision 3：0 ＝ 停用 auto-cancel），一旦停用，訂閱就永遠留在
#: ``in_progress`` 且 ``next_payment <= today`` 永遠成立，dunning cron 每天都會
#: 重跑，「投遞失敗就重送」在沒有獨立上限時等於每天一封、無限（R1 review S2）。
#:
#: 7 天對齊 ``grace_period_days`` 的預設：寬限期一過服務就會被停權，再追著提醒
#: 也改變不了什麼。刻意寫成模組常數而不是 config parameter——它是這條重試機制
#: 自己的安全閥，不該跟著營運參數一起被關掉。
_LOW_BALANCE_RETRY_WINDOW_DAYS = 7

#: #778：上游 sh_subscription_wallet SSOT（upstream PR #14）對撞防護的兩個錨點。
#: 上游會在 wallet 自己的 post_init_hook／migration 停用 base auto-invoice cron
#: ——那兩個時點都在 module graph 的 wallet 節點執行，平台 class 尚未載入、
#: method override 不在 MRO，攔不到；防線只能是 data 層 re-assert（平台在
#: 拓撲序上永遠晚於 wallet）。param 名／xmlid 是跨 repo contract，上游改名
#: 時只改這裡（tests/test_billing_ssot_guard.py 刻意雙寫字面值當同步哨兵）。
_BASE_AUTO_INVOICE_CRON_XMLID = 'sh_subscription.sh_ir_cron_subscription_auto_invoice'
_BILLING_SSOT_PARAM = 'sh_subscription_wallet.billing_ssot'

#: #778 配套 3：計費靜默告警。門檻天數 param（'0'＝停用，沿
#: max_consecutive_payment_failures 慣例）；summary 同時是 dedup 鍵——
#: 同 summary 的 activity 未被處理前不重複告警。summary 字面在
#: tests/test_billing_silence_alert.py 刻意雙寫（同步哨兵）。
_BILLING_SILENCE_PARAM = 'woow_paas_platform.billing_silence_alert_days'
_BILLING_SILENCE_SUMMARY = '計費路徑靜默：到期訂閱連續多日未被處理'
_BILLING_SILENCE_DEFAULT_DAYS = 3

#: #864 PR-E：double-charge 冷卻閘窗口（小時）。缺值／非整數／<=0 全部
#: fail-safe 回內建 24（Eugene 裁決，2026-09-01，comment 11215——同日稍早一次
#: UI 誤觸顯示過的「24 小時」已作廢，這是其後以文字明示的有效裁決，值恰好
#: 相同、授權來源不同）。比照 `_MANUAL_AGING_PARAM`（PR-C）模式：模組常數 ＋
#: get_param，同步進 `src/hooks.py` 的 `_CONFIG_PARAMETERS`。
_DOUBLE_CHARGE_COOLDOWN_PARAM = 'woow_paas_platform.double_charge_cooldown_hours'
_DOUBLE_CHARGE_COOLDOWN_DEFAULT_HOURS = 24

#: 冷卻閘獨立 cursor 那個 SELECT 的 lock_timeout——同 ``billing_incident.py``
#: 的 ``BillingIncident._LOCK_TIMEOUT`` 值，避免這條純讀查詢在
#: ``woow_billing_incident`` 上撞到並發 DDL／長交易的 ACCESS EXCLUSIVE 鎖時
#: 無界卡住整個 cron tick。
_DOUBLE_CHARGE_COOLDOWN_QUERY_LOCK_TIMEOUT = '1s'


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
    def _ensure_base_auto_invoice_cron_active(self):
        """[#778] 把 base auto-invoice cron re-assert 回 active（idempotent）。

        這顆 cron 是平台唯一計費路徑的觸發點；上游 wallet（合併 upstream
        PR #14 後）會在自己的 install/migration 時點把它關掉，而 cron seed
        XML 是 noupdate=1——被關掉後沒有任何 XML reload 會救回。接線：
        data/billing_ssot_guard.xml（每次 -u）＋ hooks.post_init_hook
        （fresh install），兩路都晚於 wallet 的關閉動作。
        """
        cron = self.env.ref(_BASE_AUTO_INVOICE_CRON_XMLID, raise_if_not_found=False)
        if not cron:
            # sh_subscription 是硬依賴，正常環境不該發生；warning 而非硬炸，
            # 避免一顆 cron record 缺失讓整次升級中斷。
            _logger.warning(
                "[SUB_FIX] base auto-invoice cron %s not found; platform "
                "billing has NO trigger (#778)", _BASE_AUTO_INVOICE_CRON_XMLID,
            )
            return False
        if not cron.active:
            cron.sudo().write({'active': True})
            _logger.info(
                "[SUB_FIX] re-asserted %s active=True (#778 SSOT collision "
                "guard)", _BASE_AUTO_INVOICE_CRON_XMLID,
            )
        return True

    @api.model
    def _ensure_billing_ssot_optout(self):
        """[#778] 把上游 billing_ssot param 固定為 '0'（platform opt-out）。

        上游語意「未設定視同啟用 SSOT」，平台裝著的 DB 必須顯式 opt-out。
        無條件 re-assert：這是平台存亡 invariant（wallet cron 已被 no-op，
        SSOT 一開就是兩路同死），不是可手動翻掉的偏好——要吃回 wallet
        SSOT 是「移除本錨點＋移除 no-op override」的重構，不是改 param。
        """
        ICP = self.env['ir.config_parameter'].sudo()
        if ICP.get_param(_BILLING_SSOT_PARAM) != '0':
            ICP.set_param(_BILLING_SSOT_PARAM, '0')
            _logger.info(
                "[SUB_FIX] pinned %s='0' (#778 platform billing opt-out)",
                _BILLING_SSOT_PARAM,
            )
        return True

    @api.model
    def _cron_billing_silence_alert(self):
        """[#778 配套 3] 偵測計費路徑靜默死亡，告警帳務管理群組。

        凍結判準：``in_progress`` 且 ``next_payment`` overdue 超過 N 天、
        且 ``woow_last_payment_failure_at`` 也超過 N 天未被 stamp（或從未）。
        dunning 活著時 due 訂閱每天要嘛成功（next_payment 推進、不再 due）
        要嘛失敗（failure_at 被 stamp 成當下）——兩欄位同時凍結＝沒有任何
        計費路徑碰過它（#778 對撞、或 per-sub exception 長期被吞）。

        刻意不用「N 天零新 cycle invoice」當主判準：長期扣款失敗的訂閱
        首日建發票、之後每日 reuse 同一張（``_get_or_create_cycle_
        invoice``），零新發票會對正常 dunning 誤報；新發票數只進 note
        當佐證。告警沿 ``ecpay_invoice_cron._notify_admin_on_error`` 慣例
        （mail.activity → account.group_account_manager），activity 未被
        處理前不重建（處理完消失，再犯再告警）。
        """
        ICP = self.env['ir.config_parameter'].sudo()
        raw = ICP.get_param(
            _BILLING_SILENCE_PARAM, str(_BILLING_SILENCE_DEFAULT_DAYS))
        try:
            days = int(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "[SUB_FIX] invalid %s=%r; falling back to %s",
                _BILLING_SILENCE_PARAM, raw, _BILLING_SILENCE_DEFAULT_DAYS)
            days = _BILLING_SILENCE_DEFAULT_DAYS
        if days <= 0:
            return
        today = fields.Date.today()
        stale_dt = fields.Datetime.now() - relativedelta(days=days)
        frozen = self.sudo().search([
            ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '<=',
             today - relativedelta(days=days)),
            '|',
            ('woow_last_payment_failure_at', '=', False),
            ('woow_last_payment_failure_at', '<', stale_dt),
        ], order='sh_date_of_next_payment asc, id asc')
        if not frozen:
            return
        Activity = self.env['mail.activity'].sudo()
        if Activity.search_count(
                [('summary', '=', _BILLING_SILENCE_SUMMARY)]):
            return  # 上一波告警尚未被處理，不重複轟炸
        todo = self.env.ref(
            'mail.mail_activity_data_todo', raise_if_not_found=False)
        manager_group = self.env.ref(
            'account.group_account_manager', raise_if_not_found=False)
        model = self.env['ir.model']._get(self._name)
        if not (todo and manager_group and model):
            _logger.warning(
                "[SUB_FIX] cannot create billing-silence activity "
                "(missing refs: todo=%s group=%s model=%s)",
                bool(todo), bool(manager_group), bool(model))
            return
        recent_invoices = self.env['account.move'].sudo().search_count([
            ('move_type', '=', 'out_invoice'),
            ('sh_subscription_id', '!=', False),
            ('create_date', '>=', stale_dt),
        ])
        anchor = frozen[0]
        note = _(
            "%(n)s in_progress subscription(s) are more than %(days)s days "
            "past due and were not touched by any billing path during that "
            "time (woow_last_payment_failure_at was never updated).\n"
            "Oldest one: %(name)s (next_payment=%(np)s).\n"
            "Supporting data: %(inv)s cycle invoice(s) created in the last "
            "%(days)s days.\n"
            "Likely causes: the base auto-invoice cron was disabled (#778 "
            "collision), or handling of those subscriptions keeps raising an "
            "exception that is being swallowed."
        ) % {
            'n': len(frozen), 'days': days,
            'name': anchor.name or anchor.id,
            'np': anchor.sh_date_of_next_payment, 'inv': recent_invoices,
        }
        for user in manager_group.users:
            Activity.create({
                'res_model': self._name,
                'res_model_id': model.id,
                'res_id': anchor.id,
                'activity_type_id': todo.id,
                'summary': _BILLING_SILENCE_SUMMARY,
                'note': note,
                'user_id': user.id,
            })
        _logger.error(
            "[SUB_FIX] billing silence alert: %s frozen sub(s) overdue>%sd, "
            "%s recent cycle invoice(s); activities created",
            len(frozen), days, recent_invoices)

    def _generate_invoice_vals(self):
        """[#864 PR-C] choke point：替訂閱家族**四個**建票入口 stamp 開票政策。

        上游 ``sh.subscription.subscription._generate_invoice_vals()`` 回傳的
        vals 不含 ``woow_invoice_issue_state`` ⇒ 建出來的 move 吃欄位 default
        ``not_required`` ⇒ 上游 inline hook 碰不到、平台 async cron 也揀不到
        （只揀 ``pending`` / ``failed``）⇒ **永遠不開票，而且管理員連手動補救鈕
        都按不下去**（``action_woow_retry_ecpay_invoice`` 對 ``not_required``
        直接 raise）。

        四個入口共用這一支 vals builder（上游 ``sh_subscription_confirm`` /
        ``sh_generate_invoice`` / ``_sh_create_period_invoice``，加上平台的
        ``subscription_lifecycle_mixin._skip_trial``）⇒ 只補其中一個入口＝只修
        四分之一，而且下一個新入口會再犯一次。故在 choke point 上 stamp。

        ⚠ 與 cycle invoice 既有的顯式覆寫**不衝突**：``_get_or_create_cycle_
        invoice`` 是在拿到 vals **之後**才蓋成 ``not_required``（儲值時已開過
        ECPay 發票，不重複開），那一步仍然勝出。

        ⚠ ``self`` 可能是空 recordset（上游 ``_sh_create_auto_invoice`` 的既知
        bug，見本 class docstring）⇒ ``sh_company_id`` 取不到時退回
        ``env.company``，resolver 本身對空 recordset 也是 fail-closed。
        """
        vals = super()._generate_invoice_vals()
        vals['woow_invoice_issue_state'] = (
            self.env['account.move']._woow_resolve_invoice_issue_state(
                self.sh_company_id or self.env.company)
        )
        return vals

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
        """Increment counter, possibly auto-cancel + notify.

        #864 PR-E：這個 method 跑在 ``_process_subscription_billing`` 那顆外層
        savepoint **之內**，而該 savepoint 這時已經含有本輪已經確定的 charge
        結果（新開的 invoice；若這輪是走 auto-topup 的 CHARGED_NOT_CREDITED，
        還包含 ``woow.wallet.topup`` 的 ``state='failed'`` 標記——真正不可回捲
        的那份證據已由 ``wallet_topup.charge_via_token`` 用獨立 cursor commit
        進 ``woow.billing.incident``，不受這裡影響）。這個 method 本身完全沒有
        保護：``_notify_low_balance``（``:706-729``）背後連呼兩次 ``formatLang``
        都沒包，任一步炸掉就會讓外層 savepoint 整個回捲，把前面那些已經確定的
        charge 結果一起連坐捲掉。

        修法：把「dunning bookkeeping」拆成兩個各自獨立的失敗域，各自用
        ``self.env.cr.savepoint()`` 包住（bare try/except 不夠——``formatLang``
        雖是純 Python，但 ``_queue_low_balance_mail``／``_notify_subscription_
        cancelled`` 內部仍有真 DB 寫入，若真的踩到 DB 層例外，沒有 savepoint
        會讓 PG transaction 卡在 aborted 狀態，禍延同一 cron tick 後續的其他
        subscription；savepoint 進出並 flush，見
        ``docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md``
        規則三）：

        1. 失敗計數（``woow_consecutive_payment_failures`` / ``woow_last_
           payment_failure_at``）——這本身就是「這輪真的失敗了」的既成事實，
           不該被下面通知信的 bug 牽連回捲。
        2. 通知／cancel（低餘額信、逾 threshold 的 cancel + 一次性通知）——
           對照組是同檔 ``_send_invoice_pdf`` 既有的 fail-soft + warning 共識。

        兩個失敗域各自吞一般例外、各自記 log——外層 savepoint 因此看不到這裡
        的一般例外，charge 結果不會被連坐。🔴 ``OperationalError``（並發／連線
        類 DB 例外，如 ``SerializationFailure`` 40001）例外：兩個失敗域都必須
        往外拋，不能被這裡的 fail-soft catch 吞掉——此刻 PG transaction 已經
        aborted，吞掉只會讓後續 flush 再炸（見
        ``docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md``
        規則一）。

        🔴 這裡的 ``raise`` 只保證**這個 method 自己**不吞 OperationalError；
        它不會外傳到 Odoo 的 RPC ``retrying()``（``odoo/service/model.py``）
        ——那是 ``call_kw`` 專用的重試包裝，cron job 走 ``ir_cron._callback``
        這條完全不同的鏈，本來就不經過它。實際接住這個往外拋的例外的，是呼叫
        端 ``_process_subscription_billing``（同檔 ``:451``/``:463`` 附近）
        自己的 ``try/except Exception``：那顆 savepoint 回捲的範圍是**這一筆
        subscription 這一輪**，記一行 log 後 ``continue`` 到 for 迴圈的下一筆
        ——不會、也不該把整條 cron 或別的 subscription 一起拖下去。

        因此**沒有同一個 cron tick 內的重試**：這一筆的 ``next_payment`` 沒有
        在這輪被推進（只有 ``_on_payment_success`` 才推進），所以隔天的 cron
        會因為 ``next_payment <= today`` 仍為真而自然重新撿到它、重跑一次
        （呼應 T19g 的 dunning 語意）。「這筆重試」等的是次日 cron，不是任何
        同輪內的自動重跑機制。
        """
        try:
            with self.env.cr.savepoint():
                subscription.write({
                    'woow_consecutive_payment_failures':
                        subscription.woow_consecutive_payment_failures + 1,
                    'woow_last_payment_failure_at': fields.Datetime.now(),
                })
        except OperationalError:
            raise
        except Exception:
            _logger.exception(
                "[SUB_FIX] failure-counter bookkeeping failed for "
                "subscription %s; this tick's charge result still stands",
                subscription.id,
            )
        # 不論上面那個 savepoint 成功或回捲，這裡都是重讀後的權威值——成功時
        # 是新值；回捲時 savepoint 的 flush+clear 已把 cache 洗回 DB 現值。
        new_count = subscription.woow_consecutive_payment_failures

        try:
            with self.env.cr.savepoint():
                # Per-cycle low-balance warning. Marker 只在信真的排進佇列後才
                # 蓋，且會在那封信投遞失敗時重新打開——判準與蓋章都走共用
                # helper，因為 `low_balance_warning_cron` 是同一個 marker 的
                # **第二個**寫入者（#389）。
                partner = subscription.sh_partner_id
                warn_key = subscription.sh_date_of_next_payment or today
                if self._low_balance_warning_needs_send(subscription, warn_key):
                    mail = self._notify_low_balance(partner, invoice)
                    self._stamp_low_balance_warning(subscription, warn_key, mail)

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
        except OperationalError:
            raise
        except Exception:
            _logger.exception(
                "[SUB_FIX] dunning notify/cancel bookkeeping failed for "
                "subscription %s; failure counter and charge result from "
                "this tick still stand",
                subscription.id,
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
        ——也就是「有沒有**呼叫過**通知函式」。但通知一律 ``force_send=False``，
        只把信排進佇列；真正的投遞是 mail queue cron，失敗的信停在
        ``mail.mail.state == 'exception'``，而 ``process_email_queue`` 的 domain
        **只撿 outgoing**（`mail_mail.py:235-241`）——沒有任何人會重試它。於是
        投遞一失敗（#389 的 prod SMTP 全 exception），租戶在整個
        ``sh_date_of_next_payment`` 不變的週期內就再也收不到提醒。

        改成看**載體的下場**：

        * marker 不是本週期 → 要送（第一次）。
        * 有 marker 但查不到載體 → 不送。這是保守 fallback，涵蓋升級前的既有
          資料（欄位為 NULL）與被人工清掉的信；讓上線當天不會對所有欠費租戶
          補發一輪。**注意這條的正確性依賴載體不會被 Odoo 自己刪掉**——所以
          ``_queue_low_balance_mail`` 會顯式關掉 ``auto_delete``，理由見那裡。
        * 載體停在 ``exception`` → 要送（重試），但受 retry 視窗節制（見下）。
        * 載體是 ``outgoing`` / ``sent`` / ``received`` → 不送（去重仍然有效）。
        * 載體是 ``cancel`` → 不送。``cancel`` 只由 ``mail.cancel()`` 明寫（維運
          手動取消／程式主動放棄），不是投遞管道的暫時性故障；重送必然再得到
          一次 cancel，而且會蓋掉「不要送這封」的意思。記一行 warning 讓維運看
          得到，而不是靜靜跳過（R1 review S4）。

        **兩個寫入者共用這個判準**：dunning 的 ``_on_payment_failure`` 與
        D-3 預警 cron ``low_balance_warning_cron._warn_if_needed`` 寫的是同一個
        marker 欄位、同一個 key（``sh_date_of_next_payment``）。只改其中一個等於
        沒改：R1 review S1 的 prod 時序就是「D-3 預警排信＋寫 marker（無載體）
        → SMTP 掛 → D 日 dunning 判成已送達 → 整週期靜音」。
        """
        if subscription.woow_last_low_balance_warned_for != warn_key:
            return True
        mail = subscription.sudo().woow_last_low_balance_mail_id
        if not mail or not mail.exists():
            return False
        if mail.state == 'cancel':
            _logger.warning(
                "[LOW_BAL] sub=%s cycle=%s 的通知被取消（mail=%s），不重送——"
                "cancel 是明確的『不要送』，不是暫時性投遞失敗。",
                subscription.id, warn_key, mail.id,
            )
            return False
        if mail.state != 'exception':
            return False
        overdue_days = (fields.Date.today() - warn_key).days
        if overdue_days > _LOW_BALANCE_RETRY_WINDOW_DAYS:
            _logger.warning(
                "[LOW_BAL] sub=%s cycle=%s 已逾期 %s 天（>%s），停止追送低餘額"
                "通知（mail=%s 仍停在 exception）。",
                subscription.id, warn_key, overdue_days,
                _LOW_BALANCE_RETRY_WINDOW_DAYS, mail.id,
            )
            return False
        return True

    @api.model
    def _stamp_low_balance_warning(self, subscription, warn_key, mail):
        """記下「這個週期已經有一封活著的通知」。兩個寫入者共用。

        ``mail`` 為空（沒排進佇列）時**什麼都不寫**——在那種情況下蓋 marker
        正是 #389 讓整個週期靜音的動作。
        """
        if not mail:
            return
        subscription.write({
            'woow_last_low_balance_warned_for': warn_key,
            'woow_last_low_balance_mail_id': mail.id,
        })

    @api.model
    def _queue_low_balance_mail(self, partner, ctx):
        """把一封低餘額通知排進佇列，回傳它的 ``mail.mail``。兩個寫入者共用。

        Args:
            partner: 收件的 ``res.partner``。
            ctx: 餵給 ``mail_template_low_balance`` 的 render context
                （``invoice_name`` / ``amount_text`` / ``balance_text`` /
                ``topup_url``）；dunning 與 D-3 預警各自組自己的字樣。

        Returns:
            ``mail.mail`` recordset——排入成功回那一封，**排入失敗回空集**
            （找不到模板、或 ``send_mail`` 丟例外）。呼叫端據此決定要不要蓋
            marker：排入失敗還蓋 marker，就是 #389 讓整個週期靜音的那個動作。

        排入後顯式關掉 ``auto_delete``（R1 review S3）：Odoo 在
        `mail_mail.py:304-305` 會把 ``auto_delete`` 的信在
        ``failure_type in ('mail_email_invalid', 'mail_email_missing')`` 時
        一併 unlink，而 ``mail.template`` 沒設就吃預設 ``True``。載體被刪掉的話，
        ``_low_balance_warning_needs_send`` 的「查不到載體＝已送達」就會把
        「這個人的 email 是壞的」誤判成送到了——換一個殼的同一個 #389。這些信
        量很小（每個週期每筆訂閱個位數且受 retry 視窗節制），留著不刪的成本
        遠低於誤判。
        """
        Mail = self.env['mail.mail'].sudo()
        template = self.env.ref(
            'woow_paas_platform.mail_template_low_balance',
            raise_if_not_found=False,
        )
        if not template:
            _logger.warning("mail_template_low_balance not found; skip notification")
            return Mail
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
        if mail:
            mail.write({'auto_delete': False})
        _logger.info(
            "Low-balance notification queued for partner %s mail=%s",
            partner.id, mail.id or None,
        )
        return mail

    @api.model
    def _notify_low_balance(self, partner, invoice):
        """Send a low-wallet-balance email to the partner (dunning wording).

        Called when an auto-invoice cannot be paid because wallet balance
        is insufficient.

        Returns:
            ``mail.mail`` recordset — see ``_queue_low_balance_mail``.
        """
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
        return self._queue_low_balance_mail(partner, ctx)

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
        - ``'cooldown_active'``             — #864 PR-E: recent unresolved
          charged_not_credited incident for this partner; refusing to
          re-charge

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

        # #864 PR-E double-charge 冷卻閘：同 partner 若在冷卻窗口內已有一筆
        # 未 resolved 的 charged_not_credited incident，代表上一次很可能已經
        # 真的扣過款——不要再送一次。插在 resolve token 成功之後、
        # create_auto_topup 之前，這樣才不會對「根本沒有可用 token」的情況
        # 誤判成命中冷卻。
        if self._double_charge_cooldown_active(partner):
            _logger.warning(
                '[AUTO_TOPUP] cooldown active — refusing to re-charge '
                'partner=%s shortfall=%s (unresolved charged_not_credited '
                'incident within cooldown window; see woow.billing.incident)',
                partner.id, shortfall,
            )
            return False, 'cooldown_active'

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

    @api.model
    def _double_charge_cooldown_hours(self):
        """冷卻窗口小時數。🔴 回傳值恆為正整數。

        缺值／非整數／``<=0`` 全部 fail-safe 回內建
        ``_DOUBLE_CHARGE_COOLDOWN_DEFAULT_HOURS``（24）——與
        ``_woow_manual_aging_days``（PR-C）同一套 fail-safe 慣例：這是防
        double-charge 的安全機制，不給「設 0 就停用」的逃生門，避免把
        「冷卻閘失效」偽裝成「合法設定」。
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            _DOUBLE_CHARGE_COOLDOWN_PARAM,
            str(_DOUBLE_CHARGE_COOLDOWN_DEFAULT_HOURS),
        )
        try:
            hours = int(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "[AUTO_TOPUP] invalid %s=%r; falling back to %s",
                _DOUBLE_CHARGE_COOLDOWN_PARAM, raw,
                _DOUBLE_CHARGE_COOLDOWN_DEFAULT_HOURS,
            )
            return _DOUBLE_CHARGE_COOLDOWN_DEFAULT_HOURS
        if hours <= 0:
            _logger.warning(
                "[AUTO_TOPUP] %s=%r is not a positive number of hours; "
                "falling back to %s. This cooldown has no disable switch "
                "by design.",
                _DOUBLE_CHARGE_COOLDOWN_PARAM, raw,
                _DOUBLE_CHARGE_COOLDOWN_DEFAULT_HOURS,
            )
            return _DOUBLE_CHARGE_COOLDOWN_DEFAULT_HOURS
        return hours

    def _double_charge_cooldown_active(self, partner):
        """同 partner 在冷卻窗口內是否有未 resolved 的 charged_not_credited incident。

        🔴🔴 決定性陷阱（#864 PR-E 設計文，逐字遵守）：⛔ 不可以用一般 ORM
        ``search`` 讀 incident。Odoo 是 REPEATABLE READ，這個 cron tick 的快照
        凍結在起跑那一刻；而 incident 是
        ``woow.billing.incident._log()`` 用**獨立 cursor**在同一輪期間
        commit 的 ⇒ 同一個 cron tick 內，本 method 若沿用 ``self.env`` 的
        ORM search，讀不到「剛剛才被別的獨立 cursor commit 進去」的那筆列
        ⇒ 同一個 partner 有兩筆同日到期訂閱時，第二筆會判定「沒有冷卻中的
        incident」而真的被扣兩次。

        ✅ 正解：``self.env.registry.cursor()`` 開一條新 cursor，繞過本 tick
        的 RR 快照直接讀，讀完立刻關（``with`` 區塊結束即 close，不 commit
        任何東西——這裡只讀不寫）。

        ⛔ 也不可以改讀 ``woow.wallet.topup`` 的 ``failed`` 標記——那個標記
        正是會被 ``_process_subscription_billing`` 的 savepoint 回捲掉的
        東西（CHARGED_NOT_CREDITED 分支若savepoint 之後又整段回捲，topup
        列可能整個消失）；只有 incident 是真正commit 過、可信的證據。

        命中冷卻窗口只 fail-safe 不重扣，不寫任何新的產品狀態（不建
        incident、不動 topup/tx）——純觀測、log 一行讓維運看得到。
        """
        hours = self._double_charge_cooldown_hours()
        cutoff = fields.Datetime.now() - timedelta(hours=hours)
        with self.env.registry.cursor() as cr:
            # 必須是這條獨立連線的第一個statement：見
            # billing_incident.py._log() 同一手法的註解——lock_timeout 只對
            # 本連線、本次呼叫生效，不設的話另一條連線可能無界卡住。
            cr.execute('SET LOCAL lock_timeout = %s',
                       (_DOUBLE_CHARGE_COOLDOWN_QUERY_LOCK_TIMEOUT,))
            cr.execute(
                "SELECT 1 FROM woow_billing_incident "
                "WHERE partner_ref = %s AND kind = 'charged_not_credited' "
                "AND state != 'resolved' AND last_seen_at >= %s "
                "LIMIT 1",
                (partner.id, cutoff),
            )
            return cr.fetchone() is not None
