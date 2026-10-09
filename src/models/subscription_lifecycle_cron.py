import logging
from datetime import timedelta

from psycopg2 import OperationalError

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

#: #1599：付款入帳後立即復權的 cron（data/subscription_lifecycle_cron.xml）。
_PAID_RESUME_CRON_XMLID = 'woow_paas_platform.ir_cron_resume_paid_cloud_services'
#: 一趟最多處理幾台；有積壓就自我 `_trigger()` 開新的一趟（同 init cron 的做法）。
_PAID_RESUME_BATCH = 20
#: 鎖被別的交易拿著（例如每日生命週期 cron 正在跑）時，隔多久再試一次。
_PAID_RESUME_RETRY_MINUTES = 1
#: #1610：付清以外的人工出口（訂閱上的欄位，定義在 sh_subscription_resume_trigger.py）。
_MANUAL_RESUME_FIELD = 'woow_resume_payment_confirmed'
#: #1610：復權暫停說明裡最多列幾張發票（其餘以「…」帶過，說明不會無限長）。
_HOLD_NOTE_MAX_INVOICES = 5


class SubscriptionLifecycleCron(models.AbstractModel):
    _name = 'woow_paas_platform.subscription_lifecycle_cron'
    _description = 'Subscription Lifecycle Enforcement'

    def _cron_enforce_subscription_lifecycle(self):
        """Daily cron: enforce subscription states on services and smart homes.

        Checks all entities with a subscription and takes action:
          - subscription renewed → follow renewal chain, resume if suspended
          - subscription in_progress + entity suspended → resume, but only when the
            subscription has no unpaid invoice or a billing administrator confirmed the
            payment manually (#1610, ``_paid_resume_block_reason``)
          - subscription expired/closed > grace period → suspend
          - subscription cancelled → suspend + check cleanup threshold
        """
        grace_days, cleanup_months = self._lifecycle_params()
        today = fields.Date.today()
        _logger.info(
            "[SUB_CRON] _cron_enforce_subscription_lifecycle start grace_days=%s cleanup_months=%s today=%s",
            grace_days, cleanup_months, today,
        )

        total = 0
        for Model in [
            self.env['woow_paas_platform.cloud_service'],
            self.env['woow_paas_platform.smart_home'],
            self.env['woow_paas_platform.security_access'],
        ]:
            entities = Model.search([('subscription_id', '!=', False)])
            _logger.info(
                "[SUB_CRON] scanning model=%s count=%s",
                Model._name, len(entities),
            )
            for entity in entities:
                total += 1
                try:
                    with self.env.cr.savepoint():
                        self._enforce_single(entity, grace_days, cleanup_months, today)
                except Exception:
                    _logger.exception(
                        "[SUB_CRON] enforcement failed for %s (id=%s)",
                        entity._name, entity.id,
                    )
        _logger.info("[SUB_CRON] _cron_enforce_subscription_lifecycle done entities=%s", total)

    def _lifecycle_params(self):
        """(grace_days, cleanup_months)：寬限天數與取消後保留月數（ir.config_parameter）。"""
        params = self.env['ir.config_parameter'].sudo()
        return (
            int(params.get_param('woow_paas_platform.grace_period_days', '7')),
            int(params.get_param('woow_paas_platform.cleanup_after_months', '6')),
        )

    def _renewal_successor(self, sub):
        """續約鏈的下一筆：以 ``sub`` 為來源、已生效（in_progress）的子訂閱；沒有回空。"""
        return self.env['sh.subscription.subscription'].search([
            ('sh_subscription_id', '=', sub.id),
            ('state', '=', 'in_progress'),
        ], limit=1)

    def _enforce_single(self, entity, grace_days, cleanup_months, today,
                        resume_only=False):
        """依訂閱狀態對單一記錄做復權／停權／清除。

        Args:
            resume_only: #1599 付款後即時復權用。只走續約鏈與復權（分支 1、2），訂閱沒有
                回到 in_progress 就直接結束——**絕不**停權或清除（分支 3、4 只屬於每日
                cron）。
        """
        sub = entity.subscription_id
        _logger.info(
            "[SUB_CRON] enforce entity=%s:%s entity_state=%s sub=%s sub_state=%s end_date=%s "
            "resume_only=%s",
            entity._name, entity.id, entity.state, sub.id, sub.state, sub.sh_end_date,
            resume_only,
        )

        # #1599：Cloud Service 的復權有兩個執行者（本 cron 與付款後即時復權的 cron），
        # 可能同時跑到同一台。可能走到復權（含續約鏈改綁）的列先取鎖，拿不到＝另一筆交易
        # 正在處理或剛處理完，這一輪跳過——不重複 scale／unpark。Smart Home／Security
        # Access 的 seam 預設恆為 True（不取鎖，行為不變）。
        if (sub.state in ('renewed', 'in_progress') and entity._is_halted()
                and not entity._acquire_resume_lock()):
            _logger.info(
                "[SUB_CRON] skip %s %s: another transaction is resuming it",
                entity._name, entity.id,
            )
            return

        # 1. Follow renewal chain
        if sub.state == 'renewed':
            new_sub = self._renewal_successor(sub)
            if new_sub:
                entity.write({'subscription_id': new_sub.id})
                _logger.info(
                    "[SUB_CRON] renewal_chain %s %s → new sub=%s",
                    entity._name, entity.id, new_sub.id,
                )
                sub = new_sub

        # 2. Active subscription → resume if halted
        #
        # #497：判準是 `_is_halted()` 而不是 `state == 'suspended'`。CloudService 的
        # state 會被 upgrade 管線改寫（#482），改寫過的服務**仍在停權中**卻走不到
        # 這條分支：既不 resume 也不再 suspend（分支 3／4 只在訂閱失效時才跑），於是
        # 租戶付了錢、服務永遠回不來——天級窗口，且沒有任何排程會修好它。
        #
        # 對 SmartHome / SecurityAccess 是零行為變化（mixin 的預設實作就是
        # `state == 'suspended'`）。對 CloudService 還額外拆掉一顆地雷：被 upgrade
        # 喚醒後被 poll 標成 `running` 但停權旗標還在的服務，`_do_resume` 會把旗標
        # 清掉——不清的話，#493 的 state 收斂會在它下一次被 redeploy／reconfigure
        # 時把這台**正在跑的付費服務**關掉。scale 1 對已在跑的服務是冪等的，零停機。
        #
        # #1610：訂閱 in_progress 還不夠——還要付清（或帳務管理員人工確認過付款）才復權，
        # 與 #1599 付款後即時復權共用同一個判斷 `_paid_resume_block_reason`。原本這裡只看
        # in_progress：後台確認續約、發票沒付，即時那條路擋下了，隔天每日 cron 照樣復權。
        # 擋下時留 log 與 `resume_hold_reason`（維運看得到為什麼還沒恢復），服務維持停權；
        # 停權那一側（分支 3、4）不變。
        if sub.state == 'in_progress':
            if entity._is_halted():
                hold = self._paid_resume_block_reason(sub)
                if hold:
                    self._hold_resume(entity, sub, hold)
                    return
                manual_unpaid = self._manually_confirmed_unpaid_invoices(sub)
                entity._do_resume()
                _logger.info(
                    "[SUB_CRON] resumed %s %s (subscription active)",
                    entity._name, entity.id,
                )
                if manual_unpaid and not entity._is_halted():
                    self._log_manual_resume(entity, sub, manual_unpaid)
            self._clear_resume_hold(entity)
            return

        # 訂閱已不是 in_progress：之前「未付清，暫不復權」的說明不再適用（停權原因回到
        # 訂閱狀態本身）。
        self._clear_resume_hold(entity)
        if resume_only:
            return

        # 3. Expired/Closed → check grace period
        if sub.state in ('expire', 'close'):
            if not sub.sh_end_date:
                return
            days_expired = (today - sub.sh_end_date).days
            # 'deleting' is terminal-ish (cleanup already ran) — excluding it
            # here, alongside 'suspended', avoids the reverse
            # 'deleting'→'suspended' state thrash on every subsequent pass.
            if (days_expired > grace_days
                    and entity.state not in ('suspended', 'deleting')):
                entity._do_suspend()
                _logger.info(
                    "[SUB_CRON] suspended %s %s (expired %d days, grace=%d)",
                    entity._name, entity.id, days_expired, grace_days,
                )
            return

        # 4. Cancelled → suspend + check cleanup
        if sub.state == 'cancel':
            # See branch 3: never re-suspend a record already cleaned up
            # ('deleting'); that reverse transition is the state thrash.
            if entity.state not in ('suspended', 'deleting'):
                entity._do_suspend()
                _logger.info(
                    "[SUB_CRON] suspended %s %s (cancelled)",
                    entity._name, entity.id,
                )

            cancel_date = sub.write_date.date() if sub.write_date else today
            months_since_cancel = (today - cancel_date).days / 30.0
            if months_since_cancel > cleanup_months:
                entity._do_cleanup()
                _logger.info(
                    "[SUB_CRON] cleaned up %s %s (cancelled %.1f months ago)",
                    entity._name, entity.id, months_since_cancel,
                )

    # ------------------------------------------------------------------ #
    # #1599 — 續訂付款成功後立即復權（只做 Cloud Service）
    # ------------------------------------------------------------------ #
    #
    # 上面的每日 cron 是復權唯一的執行者時，租戶付款後最久要等約一天，這段時間對外網址
    # 仍是停權說明頁（#1563）。這裡補一條「付款入帳就排一次」的路：
    #
    # 1. 付款入帳／訂閱回到 in_progress 的交易內（`account.move._pay_from_wallet`、
    #    `sh.subscription.subscription.write`，#1610 起也包括帳務管理員勾選「允許復權
    #    （已人工確認付款）」）只呼叫 `_request_paid_resume`：替這張訂閱
    #    底下停權中的雲端服務寫 `resume_requested`，再 `_trigger()` 下面那顆 cron。
    #    **不呼叫 operator**——付款回呼不會因此變慢，operator 失敗也不會讓付款回捲。
    # 2. 旗標與 ir.cron.trigger 都是 DB 列，跟著付款交易 commit；付款回捲（含只回捲包著它
    #    的 savepoint）時一起消失 ⇒ 不會替沒付成的訂閱復權。
    # 3. commit 之後由 cron worker 跑 `_cron_resume_paid_cloud_services`：取鎖 → 再判一次
    #    `_paid_resume_block_reason`（訂閱沿續約鏈是 in_progress，而且沒有未付清的發票或
    #    帳務管理員已人工確認；#1610 起每日 cron 也用同一個判斷）→
    #    `_enforce_single(..., resume_only=True)`。復權本身與每日 cron 同一段
    #    （`_do_resume`：scale 1 → unpark；失敗寫 error 並保留停權旗標，由每日 cron 重試）。
    #
    # Smart Home／Security Access 的停權語意不同（專屬 tunnel＋客戶端 connector），另議；
    # 這一段只找 Cloud Service，不會碰到它們。

    def _resume_target_subscription(self, entity):
        """``_enforce_single`` 實際會拿來判斷的訂閱：綁的是 renewed 就沿續約鏈走一步。"""
        sub = entity.sudo().subscription_id
        if sub.state == 'renewed':
            return self._renewal_successor(sub) or sub
        return sub

    def _unpaid_subscription_invoices(self, sub):
        """這筆訂閱還沒付清的客戶發票（``_paid_resume_block_reason`` 的判準本體）。

        * 草稿且金額不為 0：dunning 沒扣成的週期發票就停在草稿（金額 0 的草稿不算——0 元
          方案不能因此被擋）。
        * 已過帳但 ``payment_state`` 不是 paid／in_payment／reversed：例如續約確認時錢包不足，
          發票只過帳沒付；部分付款是 ``partial``。已過帳的 0 元發票 Odoo 直接記成 paid。

        刻意**不**只看「逾期」：訂閱發票沒有設付款條件（到期日＝開立日），而停權中的服務
        只會在續約確認／人工改回 in_progress 時遇到這個判斷——那一刻開的發票當天還沒逾期，
        只看逾期等於「續約確認、沒付錢」也照樣復權（#1610 要堵的就是這一格）。
        """
        if not sub:
            return self.env['account.move']
        invoices = self.env['account.move'].sudo().search([
            ('sh_subscription_id', '=', sub.id),
            ('move_type', '=', 'out_invoice'),
            ('state', 'in', ('draft', 'posted')),
            ('payment_state', 'not in', ('paid', 'in_payment', 'reversed')),
        ], order='id')
        return invoices.filtered(
            lambda inv: inv.state == 'posted'
            or not inv.currency_id.is_zero(inv.amount_total))

    def _paid_resume_block_reason(self, sub):
        """**唯一**的「這筆訂閱底下停權中的服務可以復權嗎」判斷：可以回 ``None``，不行回原因代碼。

        兩個復權執行者共用（#1610）：#1599 付款後即時復權的 cron（``_resume_paid_single``）
        與每日生命週期 cron（``_enforce_single`` 分支 2）。不要在別處另寫一套。

        * ``subscription_not_active``：訂閱（已沿續約鏈）不是 in_progress。付款失敗、付的是
          已取消訂閱的舊帳、續約還沒確認……都不能復權。人工出口也不能越過這一格。
        * ``unpaid_invoice``：還有沒付清的發票（``_unpaid_subscription_invoices``），而且
          帳務管理員沒有在訂閱上勾「允許復權（已人工確認付款）」
          （``woow_resume_payment_confirmed``，線下付款、人工確認但還沒在系統沖帳的出口）。

        試用中、0 元方案、沒有發票的訂閱沒有未付清的發票，照舊可以復權。
        """
        if not sub or sub.state != 'in_progress':
            return 'subscription_not_active'
        if sub.sudo()[_MANUAL_RESUME_FIELD]:
            return None
        if self._unpaid_subscription_invoices(sub):
            return 'unpaid_invoice'
        return None

    def _manually_confirmed_unpaid_invoices(self, sub):
        """人工出口真的派上用場（有未付清的發票、靠人工確認才放行）時回那些發票，否則空。"""
        if not sub.sudo()[_MANUAL_RESUME_FIELD]:
            return self.env['account.move']
        return self._unpaid_subscription_invoices(sub)

    @staticmethod
    def _invoice_list_text(invoices):
        """「INV/2026/00012 (300.00)」；草稿沒有號碼，``display_name`` 只有「Draft Invoice」，
        所以補上 id，好讓維運在後台找得到是哪一張。"""
        names = []
        for inv in invoices[:_HOLD_NOTE_MAX_INVOICES]:
            label = inv.display_name
            if inv.state == 'draft':
                label = '%s #%s' % (label, inv.id)
                amount = inv.amount_total
            else:
                amount = inv.amount_residual
            names.append('%s (%.2f)' % (label, amount))
        if len(invoices) > _HOLD_NOTE_MAX_INVOICES:
            names.append('…')
        return ', '.join(names)

    def _hold_resume(self, entity, sub, reason):
        """#1610：判斷不讓復權——服務維持停權，留 log 與 ``resume_hold_reason`` 說明原因。

        說明只在內容變了才寫（每日 cron 每天都會走到，不要每天改一次 write_date）。
        """
        invoices = self._unpaid_subscription_invoices(sub)
        _logger.info(
            "[SUB_CRON] resume held %s %s sub=%s reason=%s unpaid_invoices=%s",
            entity._name, entity.id, sub.id, reason, invoices.ids,
        )
        note = _(
            'Resume on hold: subscription %(subscription)s has unpaid invoices '
            '(%(invoices)s). The service resumes automatically once they are paid, or '
            'after a billing administrator ticks "Allow Resume (Payment Confirmed '
            'Manually)" on the subscription.',
            subscription=sub.display_name,
            invoices=self._invoice_list_text(invoices),
        )
        entity = entity.sudo()
        if entity.resume_hold_reason != note:
            entity.write({'resume_hold_reason': note})

    def _clear_resume_hold(self, entity):
        entity = entity.sudo()
        if entity.resume_hold_reason:
            entity.write({'resume_hold_reason': False})

    def _log_manual_resume(self, entity, sub, invoices):
        """人工出口讓一台服務在發票未付清時復權：log＋訂閱 chatter（審計）。

        附上復權後的 state：Security Access／Smart Home 的復權失敗是寫 ``error``（不再算
        停權中），不能在 chatter 寫成「已恢復」卻不交代結果。
        """
        _logger.info(
            "[SUB_CRON] resumed %s %s on manual payment confirmation sub=%s state=%s "
            "unpaid_invoices=%s",
            entity._name, entity.id, sub.id, entity.state, invoices.ids,
        )
        sub.sudo().message_post(body=_(
            '%(service)s was resumed (now %(state)s) although invoices %(invoices)s are '
            'unpaid, because a billing administrator confirmed the payment manually.',
            service='%s #%s (%s)' % (entity._description, entity.id, entity.display_name),
            state=entity.state,
            invoices=self._invoice_list_text(invoices),
        ))

    @api.model
    def _request_paid_resume(self, subscriptions, source):
        """#1599：付款入帳／訂閱回到 in_progress 時呼叫——**只**寫旗標並排一次 cron。

        對象：綁著這些訂閱（或它們的續約來源，同 ``_enforce_single`` 分支 1 的一步）、停權中
        （``_is_halted()``）、而且沿續約鏈之後訂閱是 in_progress 的 Cloud Service。

        在付款交易內執行，所以：

        * 不呼叫 operator（operator 呼叫在 cron 裡，commit 之後）。
        * 旗標與 ``ir.cron.trigger`` 隨交易 commit／回捲。
        * 不等鎖：先對該列取 ``NOWAIT`` 鎖（``_acquire_resume_lock``），拿不到代表另一筆交易
          （每日 cron 或本 cron）此刻正在處理這台，跳過這一台——付款回呼不會卡在鎖上。
        * **永不讓付款失敗**：這裡是付款之後的記帳，非 ``OperationalError`` 的例外只記 log
          （由每日 cron 照舊復權）；``OperationalError`` 照規則往外拋
          （docs/decisions/patterns/post-commit-bookkeeping-rollback-unit.md）。

        為什麼是 ir.cron trigger 而不是 ``cr.postcommit``：

        1. 本 repo 的慣例：部署、poll、init 都是「寫狀態＋``_trigger()``」，沒有任何地方用
           postcommit。
        2. postcommit 不認 savepoint：``_pay_from_wallet`` 幾乎都在 savepoint 內被呼叫
           （``/api/billing/invoices/<id>/pay``、每日扣款），savepoint 回捲了，callback 仍會
           在外層 commit 後執行 ⇒ 會替沒付成的發票復權。trigger 是 DB 列，跟著回捲。
        3. postcommit 在同一個 HTTP worker、回應送出之前同步執行：scale＋unpark（含
           Cloudflare API）會拖慢付款回呼，還得自己開 cursor、自己處理例外與並發。
        4. 持久：commit 之後 worker 若掛了，trigger 列還在，cron 照跑；postcommit 的
           callback 只在記憶體裡。
        5. 同一顆 ir.cron 不會並行（core 以 ``SKIP LOCKED`` 取 job），多次 trigger 合併成
           一趟 ⇒ 重複的付款回呼不會重複復權。

        Returns:
            這次被標記的 ``cloud_service`` recordset（測試與 log 用）。
        """
        lifecycle = self.sudo()
        Service = lifecycle.env['woow_paas_platform.cloud_service']
        subs = subscriptions.sudo().exists()
        if not subs:
            return Service
        # 付款本身的待寫入先落地：下面若出事只回捲本段，不能把付款的錯誤一起吞掉。
        lifecycle.env.flush_all()
        try:
            with lifecycle.env.cr.savepoint():
                marked = lifecycle._mark_paid_resume(subs)
        except OperationalError:
            raise
        except Exception:
            _logger.exception(
                "[SUB_CRON] paid-resume request failed source=%s subs=%s; the payment "
                "stands and the daily lifecycle cron will resume the services",
                source, subs.ids,
            )
            return Service
        if marked:
            cron = lifecycle.env.ref(_PAID_RESUME_CRON_XMLID, raise_if_not_found=False)
            if cron:
                cron._trigger()
            _logger.info(
                "[SUB_CRON] paid-resume requested source=%s subs=%s services=%s",
                source, subs.ids, marked.ids,
            )
        return marked

    def _mark_paid_resume(self, subs):
        """``_request_paid_resume`` 的寫入段：挑出要復權的 Cloud Service、取鎖、寫旗標。"""
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        bound = subs | subs.sh_subscription_id
        candidates = Service.search([('subscription_id', 'in', bound.ids)]).filtered(
            lambda svc: svc._is_halted()
            and self._resume_target_subscription(svc).state == 'in_progress')
        marked = Service
        for service in candidates:
            if service.resume_requested:
                marked |= service
                continue
            if not service._acquire_resume_lock():
                continue
            service.write({'resume_requested': True})
            marked |= service
        return marked

    @api.model
    def _cron_resume_paid_cloud_services(self):
        """#1599：處理 ``resume_requested`` 的 Cloud Service（付款交易 ``_trigger()`` 叫醒）。

        每台：取鎖（拿不到＝另一筆交易正在處理，旗標留著、一分鐘後再試）→ 清旗標 → 判
        ``_paid_resume_block_reason`` → ``_enforce_single(..., resume_only=True)``。

        ⚠ 不會停權、不會清除：``resume_only`` 讓 ``_enforce_single`` 在訂閱不是
        in_progress 時直接結束；而且前面的判斷沒過就根本不呼叫它。復權失敗（operator 或
        unpark）沿用 ``_do_resume`` 的語意：寫 error＋說明、保留停權旗標，由每日 cron 重試；
        這裡不另外重試（旗標已清）。
        """
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        queue = Service.search([('resume_requested', '=', True)], order='id',
                               limit=_PAID_RESUME_BATCH + 1)
        if not queue:
            return
        backlog = len(queue) > _PAID_RESUME_BATCH
        grace_days, cleanup_months = self._lifecycle_params()
        today = fields.Date.today()
        outcomes = {}
        for service in queue[:_PAID_RESUME_BATCH]:
            outcome = self._resume_paid_single(service, grace_days, cleanup_months, today)
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        _logger.info(
            "[SUB_CRON] paid-resume pass outcomes=%s backlog=%s", outcomes, backlog)
        cron = self.env.ref(_PAID_RESUME_CRON_XMLID, raise_if_not_found=False)
        if not cron:
            return
        if outcomes.get('busy'):
            # 鎖還被拿著（多半是每日 cron 正在跑）：稍後再來，不要立刻重排而原地空轉。
            cron._trigger(fields.Datetime.now()
                          + timedelta(minutes=_PAID_RESUME_RETRY_MINUTES))
        elif backlog:
            cron._trigger()

    def _resume_paid_single(self, service, grace_days, cleanup_months, today):
        """一台的付款後復權。回傳 ``busy``／``skipped``／``resumed``／``not_halted``／``failed``。"""
        if not service._acquire_resume_lock():
            return 'busy'
        service.write({'resume_requested': False})
        target = self._resume_target_subscription(service)
        reason = self._paid_resume_block_reason(target)
        if reason:
            if reason == 'unpaid_invoice':
                # 與每日 cron 同一段說明（#1610）：維運看得到為什麼付款／確認之後還沒恢復。
                self._hold_resume(service, target, reason)
            _logger.info(
                "[SUB_CRON] paid-resume skipped service=%s sub=%s reason=%s",
                service.id, target.id, reason,
            )
            return 'skipped'
        was_halted = service._is_halted()
        try:
            with self.env.cr.savepoint():
                self._enforce_single(service, grace_days, cleanup_months, today,
                                     resume_only=True)
        except Exception:
            _logger.exception(
                "[SUB_CRON] paid-resume failed service=%s; the daily lifecycle cron will "
                "retry", service.id,
            )
            return 'failed'
        if not was_halted:
            return 'not_halted'
        return 'failed' if service._is_halted() else 'resumed'
