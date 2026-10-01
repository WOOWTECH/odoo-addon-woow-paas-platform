"""#1159 Q38：post-deploy init 執行面的背景健康監控。

告警通路是 ``mail.activity``，收件群組 ``base.group_system``，每個監控項用不同的 summary
各自去重（上一張沒處理完就不重建，處理完消失、再犯再告警）——與 repo 既有慣例的差別是
「各自」：既有的全域 summary 去重會讓一項未處理就吞掉另一項。

掛點（#1159 實作期裁決 D3＝B）：``mail.activity`` 的目標 model 必須是 ``mail.thread``
（建立時呼叫 ``message_subscribe``、完成時 ``message_post_with_source``），``ir.cron`` 與
``cloud_service`` 都不是 ⇒ 新增本 mail.thread 單例，只給 ``base.group_system``。不掛公司
partner（ERP 共用）或 workspace（租戶讀得到）。查找一律以 xmlid 為準（``_singleton``）。

監控項與門檻（常數；#1257 Q37 只開兩個 config_parameter，這些不開）：

| 監控項 | 誰檢查 | 門檻 |
| --- | --- | --- |
| init cron 停用或停擺 | 短 poll cron | ``active=False`` 或 ``lastcall`` 距今 > 40 分 |
| 短 poll cron 停擺 | init cron | ``lastcall`` 距今 > 10 分 |
| 全平台最舊的 observed_at | 短 poll cron | 轉移狀態（非 pending）最舊觀測 > 15 分 |
| 結果未知／等待放棄 | 短 poll cron＋事件 | 24h 內 unknown／partial ≥ 1；wait_timeout／fast_fail 發生即追加 |
| eligible-unclaimed 容量等待 | 短 poll cron | 持續可 dispatch、未獲 claim ≥ 30 分；不扣額度或 D2 預算 |
| n8n ready→owner 建立 | 事件（n8n init 成功） | > 5 分，每台追加一行 |

init cron 看 ``active`` 與 ``lastcall``、不看 ``failure_count``：core 停用 cron 時會把
``failure_count`` 歸 0，``lastcall`` 也只在正常結束或 FAILED 時更新（被 SIGKILL 的那一趟
不會）。core 的 ``_notify_admin`` 是最後備援。
"""
import logging
from datetime import timedelta

from markupsafe import Markup
from psycopg2 import OperationalError

from odoo import _, api, fields, models

from .cloud_service import OBSERVATION_TRANSITION_STATES

_logger = logging.getLogger(__name__)

_SINGLETON_XMLID = 'woow_paas_platform.platform_monitor_singleton'
_INIT_CRON_XMLID = 'woow_paas_platform.ir_cron_run_post_deploy_inits'
_POLL_CRON_XMLID = 'woow_paas_platform.ir_cron_check_deploying_services'

INIT_CRON_STALE_MINUTES = 40
POLL_CRON_STALE_MINUTES = 10
OBSERVED_STALE_MINUTES = 15
UNKNOWN_WINDOW_HOURS = 24
CAPACITY_WAIT_MINUTES = 30
N8N_OWNER_WINDOW_MINUTES = 5


class PlatformMonitor(models.Model):
    _name = 'woow_paas_platform.platform_monitor'
    _description = 'PaaS Platform Monitor'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(required=True)
    notified_unknown_attempt_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_service_init_attempt',
        relation='woow_paas_platform_monitor_unknown_attempt_rel',
        column1='monitor_id',
        column2='attempt_id',
        string='Notified Unknown Init Attempts',
        copy=False,
        help='Unknown or partial init attempts delivered while still in the monitor window.',
    )

    # 每個監控項一個 summary ＝ 各自的去重鍵。
    SUMMARY_INIT_CRON = 'PaaS monitor: post-deploy init cron is not running'
    SUMMARY_POLL_CRON = 'PaaS monitor: service status poll cron is not running'
    SUMMARY_OBSERVED = 'PaaS monitor: service observations are stale'
    SUMMARY_GAVE_UP = 'PaaS monitor: init results unknown or readiness gave up'
    SUMMARY_CAPACITY = 'PaaS monitor: eligible initialization is waiting for capacity'
    SUMMARY_N8N_OWNER = 'PaaS monitor: n8n owner created late after the app became public'

    @api.model
    def _singleton(self):
        """唯一掛點；以 xmlid 查找。data record 被刪時自建一筆並綁回 xmlid，不另生第二筆。"""
        monitor = self.env.ref(_SINGLETON_XMLID, raise_if_not_found=False)
        if monitor:
            return monitor.sudo()
        _logger.error("[#1159] platform monitor record %s is missing — recreating it",
                      _SINGLETON_XMLID)
        monitor = self.sudo().create({'name': 'PaaS Platform Monitor'})
        module, name = _SINGLETON_XMLID.split('.')
        self.env['ir.model.data'].sudo().create({
            'module': module, 'name': name, 'model': self._name,
            'res_id': monitor.id, 'noupdate': True})
        return monitor

    @api.model
    def _alert(self, summary, note, append_line=None):
        """開一張 activity 給每位 ``base.group_system`` 使用者；同 summary 未處理前不重建。

        ``append_line`` 給值時，已存在的那一張追加這一行（事件型監控項：一台一行）。
        回傳是否新開了一張。
        """
        monitor = self._singleton()
        Activity = self.env['mail.activity'].sudo()
        existing = Activity.search([
            ('res_model', '=', self._name), ('res_id', '=', monitor.id),
            ('summary', '=', summary)])
        if existing:
            if append_line:
                for activity in existing:
                    activity.note = (activity.note or Markup()) + Markup('<p>%s</p>') % append_line
            return False
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        users = self.env.ref('base.group_system').users.filtered(
            lambda u: u.active and not u.share)
        if not (todo and users):
            _logger.warning("[#1159] cannot raise monitor alert %r (todo=%s users=%s)",
                            summary, bool(todo), len(users))
            return False
        body = Markup('<p>%s</p>') % note
        if append_line:
            body += Markup('<p>%s</p>') % append_line
        for user in users:
            monitor.activity_schedule(
                activity_type_id=todo.id, summary=summary, note=body, user_id=user.id)
        _logger.warning("[#1159] monitor alert raised: %s — %s", summary, note)
        return True

    @api.model
    def _safe_alert(self, summary, note, append_line=None):
        """事件通知以 savepoint 隔離；OperationalError 交 caller，其餘例外 log／False。

        此 helper 只復原通知段，不替 caller 決定回捲範圍；已 commit 的結果／key
        不得因通知失敗被抹掉，未 commit 的業務寫入仍由原 caller 邊界處理。
        """
        try:
            with self.env.cr.savepoint():
                # 只限本次 monitor 通知排 queue，避免後續 recipient 失敗留下同步寄出的部分信。
                return self.with_context(mail_notify_force_send=False)._alert(
                    summary, note, append_line=append_line)
        except OperationalError:
            raise
        except Exception:
            _logger.exception("[#1159] monitor alert %r could not be recorded", summary)
            return False

    # ------------------------------------------------------------------ #
    #  兩個 cron 的互查（Q38）                                            #
    # ------------------------------------------------------------------ #

    def _cron_stale(self, xmlid, minutes):
        cron = self.env.ref(xmlid, raise_if_not_found=False)
        if not cron:
            return _('cron %s is missing') % xmlid
        cron = cron.sudo()
        if not cron.active:
            return _('cron "%s" is inactive') % cron.name
        last = cron.lastcall or cron.write_date
        if last and fields.Datetime.now() - last > timedelta(minutes=minutes):
            return _('cron "%(name)s" last finished at %(last)s UTC (more than %(min)s minutes ago)',
                     name=cron.name, last=last, min=minutes)
        return None

    @api.model
    def _check_init_cron_health(self):
        problem = self._cron_stale(_INIT_CRON_XMLID, INIT_CRON_STALE_MINUTES)
        if problem:
            self._alert(self.SUMMARY_INIT_CRON, problem)

    @api.model
    def _check_poll_cron_health(self):
        problem = self._cron_stale(_POLL_CRON_XMLID, POLL_CRON_STALE_MINUTES)
        if problem:
            self._alert(self.SUMMARY_POLL_CRON, problem)

    # ------------------------------------------------------------------ #
    #  全域監控項                                                          #
    # ------------------------------------------------------------------ #

    @api.model
    def _check_oldest_observation(self):
        """依與 API 相同的觀測時計，對最舊的等待／真觀測告警。

        真 observed_at 早於 episode 時用等待邊界；舊列只在背景補一次 grace。
        不以 write_date 計時，避免無關寫入不斷延後 15 分鐘告警。
        """
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        Service.flush_model(['state', 'observed_at', 'observation_episode_started_at'])
        # nullable 新欄位不需 migration；legacy 轉移列在首趟 monitor 取得一次 grace。
        # 這是 poll 後的本地短 UPDATE，不跨 operator I/O 持有列鎖。
        self.env.cr.execute(
            f"""UPDATE {Service._table}
                   SET observation_episode_started_at =
                       (statement_timestamp() AT TIME ZONE 'UTC')
                 WHERE state IN %s
                   AND observation_episode_started_at IS NULL""",
            (OBSERVATION_TRANSITION_STATES,))
        Service.invalidate_model(['observation_episode_started_at'])
        self.env.cr.execute(
            f"""SELECT id,
                       CASE
                           WHEN observation_episode_started_at IS NOT NULL
                            AND (observed_at IS NULL
                                 OR observed_at < observation_episode_started_at)
                           THEN observation_episode_started_at
                           ELSE COALESCE(observed_at, observation_episode_started_at, write_date)
                       END AS seen
                  FROM {Service._table}
                 WHERE state IN %s
                 ORDER BY seen ASC NULLS FIRST LIMIT 1""",
            (OBSERVATION_TRANSITION_STATES,))
        row = self.env.cr.fetchone()
        if not row or not row[1]:
            return
        age = fields.Datetime.now() - row[1]
        if age <= timedelta(minutes=OBSERVED_STALE_MINUTES):
            return
        service = Service.browse(row[0])
        self._alert(self.SUMMARY_OBSERVED, _(
            'The oldest background observation of a transitional service is %(minutes)s '
            'minutes old: %(name)s (id=%(id)s, state=%(state)s). Check the operator and the '
            'status poll cron.', minutes=int(age.total_seconds() // 60), name=service.name,
            id=service.id, state=service.state))

    @api.model
    def _check_init_capacity_wait(self):
        """只觀測持續 eligible-unclaimed 的 episode，不動租戶 state／執行預算。

        legacy 新時計從本次背景 pass 起算；中斷時清空，再 eligible 才重開。
        這是持續條件，不是 attempt 事件：沿同 summary 未完成前不重建的慣例，
        不反覆 append 同一服務；全部完成後若條件仍存在，下一趟可以再告警。
        """
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        now = Service._init_db_now()
        services = Service.search([
            '|', ('state', '=', 'initializing'), ('init_capacity_wait_since', '!=', False)])
        waiting = Service.browse()
        for service in services:
            if not service._init_capacity_wait_eligible(now):
                if service.init_capacity_wait_since:
                    service.write({'init_capacity_wait_since': False})
                continue
            if not service.init_capacity_wait_since:
                service.write({'init_capacity_wait_since': now})
            if now - service.init_capacity_wait_since >= timedelta(minutes=CAPACITY_WAIT_MINUTES):
                waiting |= service
        if waiting:
            self.with_context(mail_notify_force_send=False)._alert(self.SUMMARY_CAPACITY, _(
                'Initialization has continuously been eligible but unclaimed for at least '
                '30 minutes. Check platform capacity: %(services)s',
                services=', '.join('%s (id=%s)' % (service.name, service.id)
                                   for service in waiting)))

    def _unknown_attempt_line(self, attempt):
        return _('%(name)s (id=%(id)s), attempt %(attempt)s: %(outcome)s',
                 name=attempt.service_id.name, id=attempt.service_id.id,
                 attempt=attempt.id, outcome=attempt.outcome)

    def _deliver_unknown_attempts(self, monitor, attempts):
        """將新完成的 unknown／partial attempts 交付目前每位 system user。

        active 卡追加新事件，已完成舊卡者則新建；只有完整收件名單都成功投影後，
        呼叫端才能記錄 attempt identity。
        """
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        users = self.env.ref('base.group_system').users.filtered(
            lambda user: user.active and not user.share)
        if not (todo and users):
            _logger.warning("[#1159] cannot raise monitor alert %r (todo=%s users=%s)",
                            self.SUMMARY_GAVE_UP, bool(todo), len(users))
            return False
        Activity = self.env['mail.activity'].sudo()
        activities = Activity.search([
            ('res_model', '=', self._name), ('res_id', '=', monitor.id),
            ('summary', '=', self.SUMMARY_GAVE_UP)])
        lines = [self._unknown_attempt_line(attempt) for attempt in attempts]
        body = Markup('<p>%s</p>') % _(
            'Post-deploy initialization ended with an unknown or partial result:')
        for line in lines:
            body += Markup('<p>%s</p>') % line
        for user in users:
            user_activities = activities.filtered(lambda activity: activity.user_id == user)
            if user_activities:
                for activity in user_activities:
                    for line in lines:
                        activity.note = (activity.note or Markup()) + Markup('<p>%s</p>') % line
                continue
            # Odoo 18 mail.thread 支援此 context key，只將本 monitor 通知排入 mail queue。
            # 後續 recipient 失敗時，savepoint 可回捲整批，不會留下已同步寄出的部分通知。
            monitor.with_context(mail_notify_force_send=False).activity_schedule(
                activity_type_id=todo.id, summary=self.SUMMARY_GAVE_UP, note=body,
                user_id=user.id)
        _logger.warning("[#1159] monitor alert raised: %s — %s", self.SUMMARY_GAVE_UP,
                        ', '.join(lines))
        return True

    @api.model
    def _check_unknown_results(self):
        """以 attempt identity 將目前的 unknown／partial 事件投影至各使用者活動。"""
        Attempt = self.env['woow_paas_platform.cloud_service_init_attempt'].sudo()
        since = fields.Datetime.now() - timedelta(hours=UNKNOWN_WINDOW_HOURS)
        monitor = self._singleton()
        try:
            # 活動投影與 identity ledger 必須一起成立；無收件者或排程失敗時仍可重試。
            with self.env.cr.savepoint():
                expired = monitor.notified_unknown_attempt_ids.filtered(
                    lambda attempt: not attempt.finished_at or attempt.finished_at < since)
                if expired:
                    monitor.write({'notified_unknown_attempt_ids': [
                        (3, attempt.id) for attempt in expired]})
                attempts = Attempt.search([
                    ('outcome', 'in', ('unknown', 'partial')),
                    ('finished_at', '>=', since)], order='id')
                new_attempts = attempts - monitor.notified_unknown_attempt_ids
                if not new_attempts:
                    return
                if not self._deliver_unknown_attempts(monitor, new_attempts):
                    return
                monitor.write({'notified_unknown_attempt_ids': [
                    (4, attempt.id) for attempt in new_attempts]})
        except OperationalError:
            # DB 故障交回交易邊界，不能偽裝成這輪 monitor 已正常完成。
            raise
        except Exception:
            _logger.exception("[#1159] unknown init-attempt monitor delivery failed")

    @api.model
    def _run_poll_cron_checks(self):
        """短 poll cron 每一趟結束時跑：init cron 的狀態＋全域監控項。"""
        for check in (self._check_init_cron_health, self._check_oldest_observation,
                      self._check_unknown_results, self._check_init_capacity_wait):
            try:
                with self.env.cr.savepoint():
                    check()
            except OperationalError:
                raise
            except Exception:
                _logger.exception("[#1159] monitor check %s failed", check.__name__)

    @api.model
    def _run_init_cron_checks(self):
        """init loop 的 commits 之後查短 poll 健康；OperationalError 外拋至 job 邊界。

        savepoint 只復原這次健康通知；先前已 commit 的 services／attempts 不回捲。
        其餘例外（如非 DB RuntimeError）維持 log／原 return 語意，不改租戶 init outcome。
        """
        try:
            with self.env.cr.savepoint():
                # 局部 queued-mail，與本 savepoint 的通知原子性一致，不改全域 outbox／SMTP。
                self.with_context(mail_notify_force_send=False)._check_poll_cron_health()
        except OperationalError:
            raise
        except Exception:
            _logger.exception("[#1159] monitor check _check_poll_cron_health failed")

    # ------------------------------------------------------------------ #
    #  事件型監控項                                                        #
    # ------------------------------------------------------------------ #

    @api.model
    def _report_gave_up(self, service, exit_kind, message):
        """等待逾時／fast-fail 轉 error 時追加一行（unknown 由 ``_check_unknown_results`` 管）。"""
        line = _('%(name)s (id=%(id)s): %(exit)s — %(message)s', name=service.name,
                 id=service.id, exit=exit_kind, message=message)
        self._safe_alert(self.SUMMARY_GAVE_UP, _(
            'Post-deploy initialization gave up on the following service(s):'),
            append_line=line)

    @api.model
    def _report_n8n_owner_window(self, service, ready_since, finished_at):
        """Q28b：n8n 從 ready（公開可達）到 owner 建立的時間，超過門檻就追加一行。"""
        if not (ready_since and finished_at):
            return
        delay = finished_at - ready_since
        if delay <= timedelta(minutes=N8N_OWNER_WINDOW_MINUTES):
            return
        line = _('%(name)s (id=%(id)s): owner created %(minutes)s minutes after the app '
                 'became ready', name=service.name, id=service.id,
                 minutes=int(delay.total_seconds() // 60))
        self._safe_alert(self.SUMMARY_N8N_OWNER, _(
            'Until the n8n owner exists anyone who can reach the URL can register it '
            '(#1159 Q28). These services stayed open longer than %(minutes)s minutes:',
            minutes=N8N_OWNER_WINDOW_MINUTES), append_line=line)

