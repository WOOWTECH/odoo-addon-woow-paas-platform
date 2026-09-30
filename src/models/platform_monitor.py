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
| n8n ready→owner 建立 | 事件（n8n init 成功） | > 5 分，每台追加一行 |

init cron 看 ``active`` 與 ``lastcall``、不看 ``failure_count``：core 停用 cron 時會把
``failure_count`` 歸 0，``lastcall`` 也只在正常結束或 FAILED 時更新（被 SIGKILL 的那一趟
不會）。core 的 ``_notify_admin`` 是最後備援。
"""
import logging
from datetime import timedelta

from markupsafe import Markup

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
N8N_OWNER_WINDOW_MINUTES = 5


class PlatformMonitor(models.Model):
    _name = 'woow_paas_platform.platform_monitor'
    _description = 'PaaS Platform Monitor'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(required=True)

    # 每個監控項一個 summary ＝ 各自的去重鍵。
    SUMMARY_INIT_CRON = 'PaaS monitor: post-deploy init cron is not running'
    SUMMARY_POLL_CRON = 'PaaS monitor: service status poll cron is not running'
    SUMMARY_OBSERVED = 'PaaS monitor: service observations are stale'
    SUMMARY_GAVE_UP = 'PaaS monitor: init results unknown or readiness gave up'
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
        """事件型監控項從業務交易裡呼叫：告警失敗只 log，不得連累呼叫端的寫入。"""
        try:
            with self.env.cr.savepoint():
                return self._alert(summary, note, append_line=append_line)
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
    def _check_unknown_results(self):
        Attempt = self.env['woow_paas_platform.cloud_service_init_attempt'].sudo()
        since = fields.Datetime.now() - timedelta(hours=UNKNOWN_WINDOW_HOURS)
        attempts = Attempt.search([
            ('outcome', 'in', ('unknown', 'partial')), ('finished_at', '>=', since)])
        if not attempts:
            return
        self._alert(self.SUMMARY_GAVE_UP, _(
            '%(count)s post-deploy init attempt(s) ended with an unknown or partial result in '
            'the last %(hours)s hours (services: %(names)s). Check the operator (a wrong '
            'operator API key is classified as unknown).',
            count=len(attempts), hours=UNKNOWN_WINDOW_HOURS,
            names=', '.join(sorted(set(attempts.mapped('service_id.name'))))))

    @api.model
    def _run_poll_cron_checks(self):
        """短 poll cron 每一趟結束時跑：init cron 的狀態＋全域監控項。"""
        for check in (self._check_init_cron_health, self._check_oldest_observation,
                      self._check_unknown_results):
            try:
                with self.env.cr.savepoint():
                    check()
            except Exception:
                _logger.exception("[#1159] monitor check %s failed", check.__name__)

    @api.model
    def _run_init_cron_checks(self):
        """init cron 每一趟結束時跑：短 poll cron 的狀態。"""
        try:
            with self.env.cr.savepoint():
                self._check_poll_cron_health()
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

