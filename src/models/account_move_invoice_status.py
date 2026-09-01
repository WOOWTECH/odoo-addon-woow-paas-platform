import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

#: #864 PR-C：`manual` 發票的老化告警門檻（天）。`'0'` ＝ 明確停用；缺值或非法值
#: 走模組內建預設。沿 `woow_paas_platform.billing_silence_alert_days` 的慣例走
#: 「模組常數 ＋ get_param」，而不是 res.config.settings 的 `config_parameter=` 欄位。
#: ⚠ 新增 config_parameter 必須同步加進 `src/hooks.py` 的 `_CONFIG_PARAMETERS`，
#: 否則卸載 addon 時這個 key 會留在 DB。
_MANUAL_AGING_PARAM = 'woow_paas_platform.manual_invoice_aging_days'
_MANUAL_AGING_DEFAULT_DAYS = 7
#: summary 同時是 dedup 鍵——同 summary 的 activity 未被處理前不重複告警（沿
#: `_cron_billing_silence_alert` 慣例）。字面在
#: `tests/test_invoice_issue_policy_864.py` 刻意雙寫（同步哨兵）。
_MANUAL_AGING_SUMMARY = '手動開票逾期：發票已過帳但 ECPay 發票號碼仍為空'


class AccountMoveInvoiceStatus(models.Model):
    _inherit = 'account.move'

    woow_invoice_issue_state = fields.Selection(
        [
            ('not_required', 'Not Required'),
            # #864 PR-C：「該開票、但依政策由人工開」。由
            # `_woow_resolve_invoice_issue_state()` 依 `res.company.auto_invoice`
            # stamp；async cron 不撿它（domain 只揀 pending/failed），管理員用
            # `action_woow_retry_ecpay_invoice()` 手動開。
            ('manual', 'Manual Issue (by policy)'),
            ('pending', 'Pending Issue'),
            ('issuing', 'Issuing'),
            ('issued', 'Issued'),
            ('failed', 'Failed (will retry)'),
            ('error', 'Error (max retries)'),
        ],
        default='not_required',
        index=True,
        copy=False,
    )
    woow_invoice_retry_count = fields.Integer(default=0, copy=False)
    woow_invoice_last_error = fields.Text(copy=False)
    woow_invoice_next_retry_at = fields.Datetime(copy=False)
    woow_skip_upstream_hook = fields.Boolean(
        default=False,
        copy=False,
        help='When True, this account.move\'s create_ecpay_invoice() '
             'short-circuits when called from upstream ecpay_invoice_tw\'s '
             'inline action_post hook. PaaS-created invoices set this so only '
             'our async ecpay_invoice_cron (which opens the hatch through the '
             'module-private helper in models/account_move.py — a plain truthy '
             'context value is forgeable over call_kw and no longer works, '
             'see #787) issues the e-invoice. Has no '
             'effect when company.auto_invoice is "manual" (the upstream inline '
             'hook is a no-op in that case anyway).',
    )

    # ------------------------------------------------------------------
    # #864 PR-C：開票政策 resolver（訂閱家族與儲值兩個呼叫點共用這一顆）
    # ------------------------------------------------------------------

    @api.model
    def _woow_resolve_invoice_issue_state(self, company):
        """依 ``res.company.auto_invoice`` 決定新建發票要 stamp 的開票狀態。

        🔴 **fail-closed**：值域是 ``manual`` / ``automatic`` / ``hand in``
        （vendored ``ecpay_invoice_tw/models/company.py`` 的 Selection），
        **只有明確等於** ``'automatic'`` 才回 ``'pending'``（交給 async cron
        自動開）；其餘值——``hand in``、上游未來新增的值、DB 尚未回填的 NULL、
        以及拿不到 company——一律回 ``'manual'``。

        ⛔ 不可寫成 ``!= 'manual'``：那會讓上游新增的值默默變成自動開票，而
        「自動打到 ECPay」是不可逆的對外動作。

        ⚠ 訂閱家族與儲值**不是**同一條建立路徑，不能用同一個 override 蓋住；
        共用的是這一顆 resolver（設計 §4.2「一個 resolver、兩個呼叫點」）：

        * 訂閱家族四個入口 → ``sh.subscription.subscription``
          `_generate_invoice_vals()` override（choke point）
        * 儲值 → ``woow.wallet.topup._create_topup_invoice()``
        """
        return 'pending' if company[:1].auto_invoice == 'automatic' else 'manual'

    def action_woow_retry_ecpay_invoice(self):
        """Manual fallback for `account.group_account_manager` users.

        Resets the retry tracking on this move and immediately tries to issue
        the ECPay e-invoice. The cron will not pick the move up again until
        this run completes.

        ⚠ #864 PR-C：``'manual'`` **刻意放行**——那正是「該開票、但依政策由人工
        開」的發票，這顆鈕就是人工開票的入口。⛔ 不要為了「收緊守衛」把它加進
        下面的拒絕清單；那會讓 `auto_invoice='manual'` 的租戶永遠開不出發票
        （這正是 #864 要修的病：連手動補救鈕都按不下去）。
        """
        self.ensure_one()
        if self.woow_invoice_issue_state == 'issued':
            raise UserError(_("This invoice has already been issued."))
        if self.woow_invoice_issue_state == 'not_required':
            raise UserError(_("This invoice does not need an ECPay e-invoice."))
        if self.state != 'posted':
            raise UserError(_("Invoice must be posted before issuing."))
        self.write({
            'woow_invoice_issue_state': 'pending',
            'woow_invoice_retry_count': 0,
            'woow_invoice_last_error': False,
            'woow_invoice_next_retry_at': False,
        })
        self.env['woow_paas_platform.ecpay_invoice_cron']._issue_one(self)
        return True

    # ------------------------------------------------------------------
    # #864 PR-C：`manual` 發票的老化告警
    # ------------------------------------------------------------------

    @api.model
    def _woow_manual_aging_days(self):
        """讀老化門檻天數；缺值／非法值一律退回內建預設，⛔ 不讓 cron 炸掉。"""
        raw = self.env['ir.config_parameter'].sudo().get_param(
            _MANUAL_AGING_PARAM, str(_MANUAL_AGING_DEFAULT_DAYS))
        try:
            return int(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "[ECPAY_INV] invalid %s=%r; falling back to %s",
                _MANUAL_AGING_PARAM, raw, _MANUAL_AGING_DEFAULT_DAYS)
            return _MANUAL_AGING_DEFAULT_DAYS

    @api.model
    def _cron_alert_stale_manual_invoices(self):
        """[#864 PR-C] ``manual`` 發票沒人開就是另一種靜默，逾期則落案並告警。

        判準：``state='posted'``、``woow_invoice_issue_state='manual'``、
        ``create_date`` 已逾 N 天，且 **ECPay 回傳的發票號碼仍為空**。

        🔴 判準 ⛔ **不能用** ``woow_invoice_issue_state == 'issued'`` 當「已真的
        開出來」：``res.company.ecpay_demo_mode`` 預設 ``True``，demo 之下開票
        cron 一樣會把 move 標成 ``issued``（對帳報表 100% 綠），但發票是打到
        ECPay **測試** API——客戶一張真發票都沒有，而所有儀表板顯示正常。唯一有
        鑑別力的證據是 ``ecpay_invoice_id.name``（``uniform.invoice`` 的統一發票
        號碼）。詳見設計 §0.6 第 3 條。

        以 ``create_date`` 而非 ``invoice_date`` 計齡：``invoice_date`` 是可被
        回填／前推的會計日期，``create_date`` 是系統觀測到這張 move 進來的時間。

        落地：PR-D 的 ``woow.billing.incident``（獨立交易寫入、以 ``external_ref``
        ``invoice:<id>`` upsert ⇒ 重跑冪等、只累加 occurrences）＋ ``mail.activity``
        （summary 為 dedup 鍵，上一波沒被處理完就不重複轟炸）。
        """
        days = self._woow_manual_aging_days()
        if days <= 0:
            return  # '0'（或任何非正值）＝ 明確停用
        cutoff = fields.Datetime.now() - timedelta(days=days)
        # ⛔ 刻意不下 `limit`：逾期條件不會自己解除（沒人開票的發票明天還是逾期），
        # 所以任何固定上限都會讓第 N+1 筆**永遠**掃不到——每個 tick 都被同一批
        # 最舊的佔滿。那是零訊號的盲區，不是「下一輪收斂」。真的多到跑不完時，
        # 本身就是必須有人處理的事故。
        #
        # 「已經真的開出來」的判準放進 domain（而不是取回後再 filtered）：
        # 判準只留一份，且 order/limit 若日後要加也不會與它錯位。
        stale = self.sudo().search(
            [
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'),
                ('woow_invoice_issue_state', '=', 'manual'),
                ('create_date', '<=', cutoff),
                # 🔴 有 ECPay 發票號碼＝真的開出來了，不算逾期——即使 issue
                # state 還停在 manual。狀態欄是我們自己寫的，號碼才是對方給的。
                '|',
                ('ecpay_invoice_id', '=', False),
                ('ecpay_invoice_id.name', 'in', [False, '']),
            ],
            order='create_date asc, id asc',
        )
        if not stale:
            return
        Incident = self.env['woow.billing.incident']
        for move in stale:
            # ⚠ snapshot 刻意不帶 partner_email、detail 刻意不帶 move.name：
            # `_validate_log_snapshot` 對 email 形狀與自由文字的敏感內容是
            # fail-soft 拒收（整筆回 False）⇒ 一旦被拒，這筆告警就靜默消失。
            # 識別走 external_ref / source_ref（都是純量、且不經內容驗證器）；
            # 人看的名字放在下面 mail.activity 的 note 裡。
            Incident._log({
                'external_ref': 'invoice:%s' % move.id,
                'kind': 'manual_invoice_stale',
                'partner_ref': move.partner_id.id or None,
                'amount': move.amount_total,
                'currency': move.currency_id.name or None,
                'source_model': 'account.move',
                'source_ref': move.id,
                'assessment_reason': 'manual_invoice_aging',
                'detail': (
                    '發票已過帳且開票政策為人工開票（manual），逾 %s 天仍未取得 '
                    'ECPay 統一發票號碼。識別請用 external_ref / source_ref。'
                    % days
                ),
            })
        self._woow_notify_stale_manual_invoices(stale, days)
        # ids 只印前 20 筆：沒有 limit 的掃描在事故現場可能是四位數，整串 id
        # 會把 log 洗掉；完整清單在 woow.billing.incident（external_ref 可查）。
        _logger.error(
            "[ECPAY_INV] %s manual invoice(s) posted >%sd without an ECPay "
            "invoice number; incidents logged, first ids=%s",
            len(stale), days, stale.ids[:20],
        )

    @api.model
    def _woow_notify_stale_manual_invoices(self, moves, days):
        """建 mail.activity 給 ``account.group_account_manager``。

        summary 即 dedup 鍵：上一波告警還沒被處理完就不重複建（處理完消失，
        再犯再告警）。形狀沿 ``ecpay_invoice_cron._notify_admin_on_error`` 與
        ``sh_subscription_fix._cron_billing_silence_alert``。
        """
        Activity = self.env['mail.activity'].sudo()
        if Activity.search_count([('summary', '=', _MANUAL_AGING_SUMMARY)]):
            return  # 上一波告警尚未被處理，不重複轟炸
        todo = self.env.ref(
            'mail.mail_activity_data_todo', raise_if_not_found=False)
        manager_group = self.env.ref(
            'account.group_account_manager', raise_if_not_found=False)
        model = self.env['ir.model']._get('account.move')
        if not (todo and manager_group and model):
            _logger.warning(
                "[ECPAY_INV] cannot create stale-manual activity "
                "(missing refs: todo=%s group=%s model=%s)",
                bool(todo), bool(manager_group), bool(model))
            return
        anchor = moves[0]
        note = _(
            "%(n)s posted invoice(s) have been waiting more than %(days)s days "
            "for a manual ECPay e-invoice and still have no invoice number.\n"
            "Oldest one: %(name)s (created %(created)s).\n"
            "Issue them from the invoice form (\"Retry ECPay e-invoice\"), or "
            "switch the company's \"Issue e-invoice\" setting to automatic.\n"
            "Note: woow_invoice_issue_state='issued' is NOT proof of issuance "
            "(ecpay_demo_mode defaults to True); the ECPay invoice number is."
        ) % {
            'n': len(moves),
            'days': days,
            'name': anchor.name or anchor.id,
            'created': anchor.create_date,
        }
        for user in manager_group.users:
            Activity.create({
                'res_model': 'account.move',
                'res_model_id': model.id,
                'res_id': anchor.id,
                'activity_type_id': todo.id,
                'summary': _MANUAL_AGING_SUMMARY,
                'note': note,
                'user_id': user.id,
            })
