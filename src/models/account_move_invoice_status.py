import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

#: #864 PR-C：`manual` 發票的老化告警門檻（天）。**正整數才生效**；`'0'`、負值、
#: 非法值、缺值一律 fail-safe 回內建預設。🔴 ⛔ **沒有 disable 逃生門**
#: （Eugene 2026-09-01 終判）——見 `_woow_manual_aging_days()` 的 docstring。
#: ⚠ 這一點刻意**與** `woow_paas_platform.billing_silence_alert_days` 的
#: 「'0' 停用」慣例**不同**：那是通知節流，這是稽核告警。載體形式仍沿用同一套
#: 「模組常數 ＋ get_param」，而不是 res.config.settings 的 `config_parameter=` 欄位。
#: ⚠ 新增 config_parameter 必須同步加進 `src/hooks.py` 的 `_CONFIG_PARAMETERS`，
#: 否則卸載 addon 時這個 key 會留在 DB。
_MANUAL_AGING_PARAM = 'woow_paas_platform.manual_invoice_aging_days'
_MANUAL_AGING_DEFAULT_DAYS = 7
#: summary 同時是 dedup 鍵——同 summary 的 activity 未被處理前不重複告警（沿
#: `_cron_billing_silence_alert` 慣例）。字面在
#: `tests/test_invoice_issue_policy_864.py` 刻意雙寫（同步哨兵）。
_MANUAL_AGING_SUMMARY = '手動開票逾期：發票已過帳但 ECPay 發票號碼仍為空'

#: #990 老化掃描的三個預算旋鈕（形狀抄 `access_resync_cron._resync_leg`，
#: 該處 docstring 引 #791 覆核 F1 ——同一個「naive limit 餓死後排」問題）。
#: `_AGING_ALERT_LIMIT` 限的是「這一輪**寫幾筆 incident**」，**成敗都算**
#: （只算成功的話，`_log` 持續失敗時這一輪會一路打到 `_AGING_MAX_SCAN`，
#: 變成失敗時做得比成功時還多）。
_AGING_ALERT_LIMIT = 500
#: 每次 `search` 抓幾列（游標分批的批量，不是預算）。
_AGING_SCAN_BATCH = 1000
#: 掃描筆數安全上界；踩到必出聲，且「是否真的被截斷」用**實查**判定。
_AGING_MAX_SCAN = 20000


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
    #: #864 #990：老化告警的去重 marker。非空＝這張 move 已經被
    #: `_cron_alert_stale_manual_invoices` 告警過，⛔ 不再進掃描視窗。
    #:
    #: 🔴 **為什麼需要它**：逾期條件**不會自消**（沒人手動開票前它一直符合
    #: domain），所以 naive `search(limit=N)` 的視窗會被同一批舊 move 佔死、
    #: 後排永遠餓死（#791 覆核 F1 的同一個形狀）。把「已告警」寫進 move 本身
    #: 才能讓它離開 domain ⇒ 視窗會前進。
    #:
    #: ⚠ 失效方向是刻意選的：marker **只在** `Incident._log()` 回傳非 False
    #: 時才寫，且它走主 cursor、incident 走獨立 cursor ⇒ 主交易若回捲，marker
    #: 消失而 incident 還在 ⇒ 下一輪重掃、upsert 累加 occurrences ＝**重複告警**
    #: （無害）。⛔ 反過來「marker 寫了但 incident 沒寫」＝漏告警，不會發生。
    woow_aging_alerted_at = fields.Datetime(
        string='Aging Alert Sent At',
        index=True,
        copy=False,
        readonly=True,
        help='Set when the manual-invoice aging cron has logged a billing '
             'incident for this move. Non-empty rows leave the scan window so '
             'newly-aged invoices can be reached (see #990).',
    )
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

    def _woow_ecpay_issuance_confirmed(self):
        """ECPay 是否**已核發真實發票號碼**給這張 move。

        🔴 **這是本模組唯一的判準**：``ecpay_invoice_cron._issue_one()`` 與
        ``action_woow_retry_ecpay_invoice()`` 共用同一顆，⛔ 兩處不得各自寫
        條件——一個用 OR、一個用 AND，差集就是「一邊放行、另一邊靜默當成功」
        的破口（#864 review BLOCK 的成因）。

        判準＝``ecpay_invoice_id.name`` 非空。依據是 vendored
        ``ecpay_invoice_tw`` 的 ``create_ecpay_invoice()``：``uniform.invoice``
        記錄在**呼叫 ECPay 之前**就先建好（只帶 ``company_id``、``name`` 為
        NULL）；``RtnCode != 1`` 直接 raise，此時 ``self.ecpay_invoice_id``
        還沒被指派。號碼只會來自 ``aReturn_Info['InvoiceNo']``。
        ⇒ **號碼非空 ⟺ ECPay 真的核發過一張發票**。

        ⛔ 不可只判 ``ecpay_invoice_id`` 非空：開票失敗可能留下號碼為空的殘值，
        那樣寫會把合法的 retry 一起擋死。

        ⚠ ``uniform_state`` 是 ``invalid``（已作廢）／``Discounted``（已折讓）
        時**仍然回 True**：號碼已經核發出去了，重開會產生第二張真發票。這兩格
        要的是 fail-loud（讓人來判斷），⛔ 不是自動重開、更不是靜默標成已開。
        作廢後要重開屬產品決策，本模組不自行發明。
        """
        self.ensure_one()
        return bool(self.ecpay_invoice_id.name)

    def action_woow_retry_ecpay_invoice(self):
        """Manual fallback for `account.group_account_manager` users.

        Resets the retry tracking on this move and immediately tries to issue
        the ECPay e-invoice. The cron will not pick the move up again until
        this run completes.

        ⚠ #864 PR-C：``'manual'`` **刻意放行**——那正是「該開票、但依政策由人工
        開」的發票，這顆鈕就是人工開票的入口。⛔ 不要為了「收緊守衛」把它加進
        下面的拒絕清單；那會讓 `auto_invoice='manual'` 的租戶永遠開不出發票
        （這正是 #864 要修的病：連手動補救鈕都按不下去）。

        🔴 但 ``manual`` 進了 UI 入口，也就擴大了「這顆鈕按得到哪些 move」的
        可達面，所以同時收口重送洞：**ECPay 已核發真實發票號碼的 move，⛔ 一律
        拒絕再次對外開票**。判準見 ``_woow_ecpay_issuance_confirmed()``，與
        ``ecpay_invoice_cron._issue_one()`` **共用同一顆**；守衛置於所有 issue
        state 判斷之前，故 base 的 ``failed``／``error`` 兩格同洞一併覆蓋。
        """
        self.ensure_one()
        # 先於所有 issue state 判斷：`woow_invoice_issue_state` 是我們自己寫的欄位
        # （`issued` 在 demo 模式下照樣會被寫出來，§0.6#3），而發票號碼與
        # `uniform_state` 是上游依 ECPay 回應寫的。故 base 的 failed／error 兩格
        # 同洞也由這一道一併覆蓋。
        if self._woow_ecpay_issuance_confirmed():
            raise UserError(_(
                "ECPay has already issued invoice number %s for this "
                "document. Re-issuing would create a second real invoice; "
                "resolve it manually instead."
            ) % self.ecpay_invoice_id.name)
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
        """老化門檻天數。🔴 **回傳值恆為正整數**。

        `'0'`、負值、非法值、缺值——全部 fail-safe 回內建
        ``_MANUAL_AGING_DEFAULT_DAYS``（7），⛔ 也不讓 cron 炸掉。

        🔴 ⛔ **這裡刻意沒有 disable 逃生門**（Eugene 2026-09-01 終判）：老化
        告警是安全／稽核機制，而「把門檻設成 0 就靜默不告警」是未授權的產品
        語意——那種開關會讓稽核告警整條消失，**且資料面看起來跟「沒有逾期發票」
        一模一樣**。這正是本 issue 家族的根因（一堆 admin 可改的 gate 疊起來讓
        金流靜默卡死）。未來若真要 disable，須 Eugene 另行明示。
        """
        raw = self.env['ir.config_parameter'].sudo().get_param(
            _MANUAL_AGING_PARAM, str(_MANUAL_AGING_DEFAULT_DAYS))
        try:
            days = int(raw)
        except (TypeError, ValueError):
            _logger.warning(
                "[ECPAY_INV] invalid %s=%r; falling back to %s",
                _MANUAL_AGING_PARAM, raw, _MANUAL_AGING_DEFAULT_DAYS)
            return _MANUAL_AGING_DEFAULT_DAYS
        if days <= 0:
            _logger.warning(
                "[ECPAY_INV] %s=%r is not a positive number of days; falling "
                "back to %s. This alert has no disable switch by design.",
                _MANUAL_AGING_PARAM, raw, _MANUAL_AGING_DEFAULT_DAYS)
            return _MANUAL_AGING_DEFAULT_DAYS
        return days

    @api.model
    def _woow_stale_manual_domain(self, cutoff):
        """老化判準的**唯一**來源（掃描、計數、截斷實查三處共用）。

        判準：``move_type='out_invoice'``、``state='posted'``、
        ``woow_invoice_issue_state='manual'``、``create_date`` 已逾 N 天、
        **尚未告警過**（``woow_aging_alerted_at`` 為空），且 **ECPay 回傳的
        發票號碼仍為空**。

        🔴 判準 ⛔ **不能用** ``woow_invoice_issue_state == 'issued'`` 當「已真的
        開出來」：``res.company.ecpay_demo_mode`` 預設 ``True``，demo 之下開票
        cron 一樣會把 move 標成 ``issued``（對帳報表 100% 綠），但發票是打到
        ECPay **測試** API——客戶一張真發票都沒有，而所有儀表板顯示正常。唯一有
        鑑別力的證據是 ``ecpay_invoice_id.name``（``uniform.invoice`` 的統一發票
        號碼）。詳見設計 §0.6 第 3 條。

        ⚠ 抽成一支的理由是**只留一份真相**：掃描 domain 與「還剩幾筆」的
        ``search_count`` 若各寫一份，改了一邊沒改另一邊時症狀是「告警訊息裡的
        數字對不上」——而那是無聲的。
        """
        return [
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('woow_invoice_issue_state', '=', 'manual'),
            ('create_date', '<=', cutoff),
            # #990：已告警過的離開視窗，否則 limit 會被舊批佔死（見欄位註解）。
            ('woow_aging_alerted_at', '=', False),
            # 🔴 有 ECPay 發票號碼＝真的開出來了，不算逾期——即使 issue
            # state 還停在 manual。狀態欄是我們自己寫的，號碼才是對方給的。
            '|',
            ('ecpay_invoice_id', '=', False),
            ('ecpay_invoice_id.name', 'in', [False, '']),
        ]

    @api.model
    def _cron_alert_stale_manual_invoices(self, limit=None, scan_batch=None,
                                          max_scan=None):
        """[#864 PR-C／#990] ``manual`` 發票沒人開就是另一種靜默，逾期則落案並告警。

        判準見 ``_woow_stale_manual_domain()``；門檻天數見
        ``_woow_manual_aging_days()``（⛔ **恆開**，沒有 disable 開關）。
        以 ``create_date`` 而非 ``invoice_date`` 計齡：``invoice_date`` 是可被
        回填／前推的會計日期，``create_date`` 是系統觀測到這張 move 進來的時間。

        落地：PR-D 的 ``woow.billing.incident``（獨立交易寫入、以 ``external_ref``
        ``invoice:<id>`` upsert）＋ ``mail.activity``（summary 為 dedup 鍵）。

        ## 🔴 #990：為什麼是 id 游標批次，而不是 ``search(limit=N)``

        逾期條件**不會自消**（沒人手動開票前它一直符合 domain），而 ``_order``
        讓最舊的那批永遠排在前面 ⇒ 每個 tick 的 limit 視窗都被同一批舊 move
        佔住 ⇒ **新變老的 move 永遠拿不到告警**，而那個狀態與「今天沒事做」在
        輸出上完全一樣。這與 ``access_resync_cron._resync_leg``（#791 覆核 F1）
        是同一個形狀，解法也照抄：沿 id 游標一批批掃過去，``limit`` 只算
        「這一輪對外寫幾筆」。

        視窗前進靠**兩件事同時成立**，缺一不可：

        1. ``woow_aging_alerted_at`` 讓已告警的 move **離開 domain**
           ⇒ 下一輪的視窗會落在新的 move 上；
        2. ``id > last_id`` 游標讓**這一輪**不重掃同一列——⚠ 這一條不是多餘的：
           ``Incident._log()`` 是 fail-soft（回 ``False``），失敗的那些**不會**被
           標 marker、仍留在 domain 裡；沒有游標的話同一批會被反覆撈出來，
           迴圈原地空轉直到打完 ``limit``。

        ⚠ 掃描順序從 ``create_date asc, id asc`` 改成 ``id``：游標的正確性完全
        建立在「這一批的最大 id 之後還有沒有列」上（⛔ 不靠 ``_order``，顯式
        帶 ``order='id'``）。對 ``account.move`` 而言 id 與 ``create_date`` 同向
        （都是建立時序），「最舊的先告警」這個性質仍近似成立。

        ⚠ **每一輪都出聲，零命中那輪也是**，而且空手那一行同樣自帶計數：
        「掃了 0 列」與「掃了 500 列全被排除」是兩件事，同一句話要能分開。

        :param limit: 這一輪最多寫幾筆 incident（**成敗都算**）。
        :param scan_batch: 每次 ``search`` 的批量。
        :param max_scan: 掃描筆數安全上界；踩到必出聲。
        """
        # `_woow_manual_aging_days()` 保證回正整數（⛔ 無 disable 逃生門），
        # 所以這裡沒有、也不該有「門檻不合法就整段 return」的早退分支。
        days = self._woow_manual_aging_days()
        cutoff = fields.Datetime.now() - timedelta(days=days)
        limit = _AGING_ALERT_LIMIT if limit is None else limit
        scan_batch = _AGING_SCAN_BATCH if scan_batch is None else scan_batch
        max_scan = _AGING_MAX_SCAN if max_scan is None else max_scan

        Move = self.sudo()
        domain = self._woow_stale_manual_domain(cutoff)
        # ⚠ 在**寫任何 marker 之前**取總數：寫完再算會把本輪處理掉的排除在外，
        # 告警訊息裡的數字就會從「總逾期數」縮水成「這批的筆數」——而縮水的
        # 那一側是無聲的（沒有人會發現數字變小了）。
        pending_total = Move.search_count(domain)

        Incident = self.env['woow.billing.incident']
        now = fields.Datetime.now()
        last_id = 0
        scanned = 0
        attempted = 0          # 已對外寫幾筆（成敗都算）＝ limit 的計量單位
        failed = 0
        alerted = Move.browse()
        truncated = False

        while attempted < limit:
            rows = Move.search(
                domain + [('id', '>', last_id)],
                limit=scan_batch, order='id')
            if not rows:
                break
            last_id = rows[-1].id
            scanned += len(rows)
            for move in rows:
                # ⚠ snapshot 刻意不帶 partner_email、detail 刻意不帶 move.name：
                # `_validate_log_snapshot` 對 email 形狀與自由文字的敏感內容是
                # fail-soft 拒收（整筆回 False）⇒ 一旦被拒，這筆告警就靜默消失。
                # 識別走 external_ref / source_ref（都是純量、且不經內容驗證器）；
                # 人看的名字放在下面 mail.activity 的 note 裡。
                incident_id = Incident._log({
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
                attempted += 1
                if incident_id:
                    # 🔴 marker **只在 incident 真的落地後**才寫。順序反過來就是
                    # 「標了但沒落案」＝永久漏告警，而且無聲。
                    move.woow_aging_alerted_at = now
                    alerted |= move
                else:
                    # `_log` 已經吞掉例外並自行 log；這裡只計數，⛔ 不標 marker
                    # ⇒ 下一輪還撿得回來。
                    failed += 1
                if attempted >= limit:
                    break
            if scanned >= max_scan:
                # 「是不是真的被截斷」用**實查**不用猜：拿「這一批是滿的」當代理
                # 判準時，剛好整除 `scan_batch` 就會發假截斷警報。訊號通膨與訊號
                # 缺席一樣糟（同 access_resync_cron 的教訓）。
                truncated = bool(Move.search(
                    domain + [('id', '>', last_id)], limit=1, order='id'))
                break

        if alerted:
            self._woow_notify_stale_manual_invoices(alerted, days, pending_total)

        if truncated:
            _logger.warning(
                "[ECPAY_INV] aging scan cap (%d) reached before the stale rows "
                "were exhausted — the rest wait for the next run", max_scan)
        # ids 只印前 20 筆：事故現場可能是四位數，整串 id 會把 log 洗掉；
        # 完整清單在 woow.billing.incident（external_ref `invoice:<id>` 可查）。
        (_logger.error if alerted or failed else _logger.info)(
            "[ECPAY_INV] aging scan: scanned=%d alerted=%d failed=%d "
            "pending_total=%d days=%d truncated=%s first_ids=%s",
            scanned, len(alerted), failed, pending_total, days, truncated,
            alerted.ids[:20],
        )
        return len(alerted)

    @api.model
    def _woow_notify_stale_manual_invoices(self, moves, days,
                                           pending_total=None):
        """建 mail.activity 給 ``account.group_account_manager``。

        summary 即 dedup 鍵：上一波告警還沒被處理完就不重複建（處理完消失，
        再犯再告警）。形狀沿 ``ecpay_invoice_cron._notify_admin_on_error`` 與
        ``sh_subscription_fix._cron_billing_silence_alert``。

        🔴 #990：``moves`` 自 batch 化之後只是**這一輪處理的那批**，⛔ 不是
        全部逾期數。note 裡的總數必須用 ``pending_total``（呼叫端在寫任何
        marker **之前**取的 ``search_count``），否則管理員看到的數字會從
        「總逾期數」縮水成 ``limit``——而縮水的那一側完全無聲，正是這條告警
        本來要偵測的那種靜默。省略時退回 ``len(moves)``（相容舊呼叫端）。
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
        total = len(moves) if pending_total is None else pending_total
        note = _(
            "%(total)s posted invoice(s) have been waiting more than %(days)s "
            "days for a manual ECPay e-invoice and still have no invoice "
            "number (%(batch)s of them were logged in this run).\n"
            "Oldest one in this run: %(name)s (created %(created)s).\n"
            "Issue them from the invoice form (\"Retry ECPay e-invoice\"), or "
            "switch the company's \"Issue e-invoice\" setting to automatic.\n"
            "Note: woow_invoice_issue_state='issued' is NOT proof of issuance "
            "(ecpay_demo_mode defaults to True); the ECPay invoice number is."
        ) % {
            'total': total,
            'batch': len(moves),
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
