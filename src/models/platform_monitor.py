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
| n8n owner 未確認 | 短 poll cron | 首次觀測 ready 後 5 分內沒有可信確認；每 episode 一行 |
| 叢集儲存額度偏低（#1415） | 獨立 cron（15 分） | 有備援的最大可放單顆 volume ＜ 設定值（預設 80 GiB）或任一節點預訂 ＞ 90% |
| 開通被叢集儲存額度擋下（#1415） | 事件（``_execute_deploy``） | 發生即追加一行 |
| running 服務的 workload 沒有 Pod（#1647） | 獨立 cron（10 分）＋每日對帳 cron | 期望 ≥1、實際 0；有 FailedCreate 證據立即告警，沒有則持續 ≥5 分 |

init cron 看 ``active`` 與 ``lastcall``、不看 ``failure_count``：core 停用 cron 時會把
``failure_count`` 歸 0，``lastcall`` 也只在正常結束或 FAILED 時更新（被 SIGKILL 的那一趟
不會）。core 的 ``_notify_admin`` 是最後備援。
"""
import logging
from datetime import timedelta

from lxml import etree
from lxml import html as lxml_html
from markupsafe import Markup
from psycopg2 import OperationalError

from odoo import _, api, fields, models

from .cloud_service import (
    OBSERVATION_TRANSITION_STATES,
    _COMPUTE_QUOTA_REASON_ROLLING_ON_DOWNGRADE,
    _compute_quota_detail_rows,
)

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
# 掃描範圍（#1159 owner 裁決 2026-10-01）：等待、失敗（含 wait_timeout）、running（冷補套／
# 晚到的確認）、停權都掃；deleting 不掃。
N8N_OWNER_SCAN_STATES = ('initializing', 'error', 'running', 'suspended')
#: #1415：叢集儲存額度告警。最大可放單顆 volume 的門檻是設定參數（「模組常數＋get_param」，
#: 卸載清理登記在 hooks.py）：80 GiB 當初依 Nextcloud 資料磁碟 80 GiB 訂定；#1634（2026-10-10）
#: Nextcloud 改為整台 20G、#1637（同日「每台上限 30G」）之後，範本最大的單顆 PVC 是 HAOS 30 GiB
#: （CDI 實建約 31.8 GiB）、方案最大單顆是 pi Coding Agent 20 GiB，預設值刻意沿用 80（偏早告警；
#: 要調低改設定參數即可）；'0' 停用這一項。節點預訂率門檻是常數
#: （Longhorn 排程額度＝(磁碟−保留)×over-provisioning%）。
CLUSTER_STORAGE_MIN_VOLUME_PARAM = 'woow_paas_platform.cluster_storage_alert_min_volume_gib'
CLUSTER_STORAGE_MIN_VOLUME_GIB_DEFAULT = 80
CLUSTER_STORAGE_NODE_RATIO_LIMIT = 0.9
_GIB = 1024 ** 3
#: #1647：一張告警 activity 最多追加幾行（``_alert`` 的 ``append_line``）。事件型的來源有些
#: 可以被反覆觸發；超過之後只補一行「還有更多」，其餘寫 log。
ALERT_MAX_APPENDED_LINES = 50
#: #1647：compute 配額告警（``_report_compute_quota_rejection``）固定用這個語言組，不跟著
#: 觸發的那位租戶走——與 cron 產生的告警一致（cron 的 env 沒有語言 ⇒ 英文）。
COMPUTE_QUOTA_ALERT_LANG = 'en_US'


class PlatformMonitor(models.Model):
    _name = 'woow_paas_platform.platform_monitor'
    _description = 'PaaS Platform Monitor'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char(
        string='Name',
        required=True,
        help='Label of the platform monitor record that system alerts are attached to.',
    )
    notified_unknown_attempt_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_service_init_attempt',
        relation='woow_paas_platform_monitor_unknown_attempt_rel',
        column1='monitor_id',
        column2='attempt_id',
        string='Notified Unknown Init Attempts',
        copy=False,
        help='Unknown or partial init attempts delivered while still in the monitor window.',
    )
    # #1647：`CloudService._cron_check_running_workloads` 的輪替游標＝上一趟最後處理的
    # service id（成功、失敗、略過都算），下一趟從它之後依 id 升冪、繞回開頭。
    # 記在這裡而不是服務列上：那條 cron 每 10 分鐘跑一次，在每台 running 服務留時間戳
    # 等於每 10 分鐘 commit 一次所有 running 列，會與每日對帳、生命週期 cron 的長交易互撞。
    # 本單例這一列沒有長交易在寫（週期性的寫入者只有短 poll 結尾的
    # `_check_unknown_results`，純 DB），而且每趟都查得完時游標值不變、連這裡也不寫。
    # 沒有 default：新欄位由 ORM 自動建欄，既有那一列是 NULL，讀起來就是 0（從頭開始）。
    workload_check_cursor = fields.Integer(
        string='Workload Check Cursor',
        copy=False,
        readonly=True,
        help='ID of the last cloud service handled by the periodic check of running '
             'workloads. The next run continues after it and wraps around.',
    )

    # 每個監控項一個 summary ＝ 各自的去重鍵。
    SUMMARY_INIT_CRON = 'PaaS monitor: post-deploy init cron is not running'
    SUMMARY_POLL_CRON = 'PaaS monitor: service status poll cron is not running'
    SUMMARY_OBSERVED = 'PaaS monitor: service observations are stale'
    SUMMARY_GAVE_UP = 'PaaS monitor: init results unknown or readiness gave up'
    SUMMARY_CAPACITY = 'PaaS monitor: eligible initialization is waiting for capacity'
    SUMMARY_N8N_OWNER = 'PaaS monitor: n8n owner not yet confirmed by the platform'
    SUMMARY_WORKLOAD_FAULT = 'PaaS monitor: running services whose workloads have no pods'
    SUMMARY_STORAGE_LOW = 'PaaS monitor: cluster storage scheduling capacity is running low'
    SUMMARY_STORAGE_REJECTED = 'PaaS monitor: deployments rejected for lack of cluster storage'
    SUMMARY_STORAGE_EXPANSION_REJECTED = 'PaaS monitor: plan changes rejected because a disk cannot be enlarged'
    SUMMARY_COMPUTE_QUOTA_REJECTED = 'PaaS monitor: changes rejected because the workspace quota cannot fit them'

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

    @staticmethod
    def _note_paragraphs(note):
        """activity note（HTML）→ 每個 ``<p>`` 的純文字（空白收斂成單一空格）。

        比純文字而不是比 HTML 原始字串：``mail.activity.note`` 寫入時會被 sanitize，跳脫的
        寫法會變（``&#34;`` → ``"``），拿原始字串比對會以為「這一行還沒寫過」。
        """
        if not note:
            return []
        try:
            root = lxml_html.fragment_fromstring(str(note), create_parent='div')
        except (etree.LxmlError, ValueError, TypeError):
            return []
        return [' '.join(p.text_content().split()) for p in root.iter('p')]

    @api.model
    def _alert(self, summary, note, append_line=None):
        """開一張 activity 給每位 ``base.group_system`` 使用者；同 summary 未處理前不重建。

        ``append_line`` 給值時，已存在的那一張追加這一行（事件型監控項：一台一行）。
        回傳是否新開了一張。

        追加有兩道上限（#1647）——事件型的來源有些可以被反覆觸發（租戶對同一台、同一個
        原因連按「儲存設定」，每次被擋都會走到這裡）：

        * **同一行已經在 note 裡就不再追加**：同一台同一個原因只佔一行。
        * **每張最多 ``ALERT_MAX_APPENDED_LINES`` 行**：滿了之後補一行「還有更多、見 server
          log」就不再追加；被略過的那一行照樣寫進 log，不會無聲消失。
        """
        monitor = self._singleton()
        Activity = self.env['mail.activity'].sudo()
        existing = Activity.search([
            ('res_model', '=', self._name), ('res_id', '=', monitor.id),
            ('summary', '=', summary)])
        if existing:
            if append_line:
                line_html = Markup('<p>%s</p>') % append_line
                line_text = ' '.join(self._note_paragraphs(line_html))
                for activity in existing:
                    paragraphs = self._note_paragraphs(activity.note)
                    if line_text and line_text in paragraphs:
                        continue
                    # 第一段是說明文字，其餘才是追加的行（滿了之後多一行「還有更多」）。
                    appended = len(paragraphs) - 1
                    if appended >= ALERT_MAX_APPENDED_LINES:
                        _logger.warning(
                            "[#1647] monitor alert %r already lists %s lines; not appended: %s",
                            summary, ALERT_MAX_APPENDED_LINES, line_text)
                        if appended == ALERT_MAX_APPENDED_LINES:
                            activity.note = (activity.note or Markup()) + Markup('<p>%s</p>') % _(
                                'More entries were not listed here (the limit of %(limit)s '
                                'lines was reached); see the server log.',
                                limit=ALERT_MAX_APPENDED_LINES)
                        continue
                    activity.note = (activity.note or Markup()) + line_html
            return False
        todo, users = self._alert_recipients()
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
    def _alert_recipients(self):
        """告警的 activity type 與收件者（active、非 share 的 ``base.group_system``）。"""
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        users = self.env.ref('base.group_system').users.filtered(
            lambda u: u.active and not u.share)
        return todo, users

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
            # 與其他 monitor 通知一致只排 queue：外層交易回捲時不留下已同步寄出的信。
            self.with_context(mail_notify_force_send=False)._alert(self.SUMMARY_INIT_CRON, problem)

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
        self.with_context(mail_notify_force_send=False)._alert(self.SUMMARY_OBSERVED, _(
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

    def _n8n_owner_confirmed_services(self, services):
        """回傳在各自期限內已取得可信 owner 確認的服務（#1159 owner 裁決 2026-10-01）。

        確認＝本代、本 episode（finished_at ≥ 首次 ready）、期限內收到的 n8n ``success``，
        且本 attempt 的 key 已落庫。operator 先以平台帳密 login 才建得出 API key，所以語意是
        「平台帳密能登入的 owner 已存在」。login／create_api_key 的 failed／unknown 不算：
        那正是 owner 被他人先註冊的症狀。``finished_at`` 是平台收到結果的時間，不是 owner
        建立時間。
        """
        window = timedelta(minutes=N8N_OWNER_WINDOW_MINUTES)
        attempts = self.env['woow_paas_platform.cloud_service_init_attempt'].sudo().search([
            ('service_id', 'in', services.ids), ('init_type', '=', 'n8n'),
            ('outcome', '=', 'success'), ('n8n_key_persisted', '=', True),
            ('finished_at', '!=', False)])
        confirmed = services.browse()
        for attempt in attempts:
            service = attempt.service_id
            if (attempt.generation == (service.init_generation or 0)
                    and service.init_ready_since <= attempt.finished_at
                    <= service.init_ready_since + window):
                confirmed |= service
        return confirmed

    @api.model
    def _check_n8n_owner_unconfirmed(self):
        """Q28b：首次觀測 ready 後 5 分內平台仍未確認 n8n owner，就每 episode 告警一行。

        依「確認時間 ≤ 首次 ready＋5 分」判斷，與掃描時點無關；晚到的確認仍是 5 分內
        未確認。缺證據＝未確認，不代表 owner 不存在。marker 與告警同一個 check savepoint，
        通知失敗時一起回捲；沒有可收件者時不寫 marker，下一趟可重試。不改 state、額度或
        等待預算。
        """
        Service = self.env['woow_paas_platform.cloud_service'].sudo()
        now = Service._init_db_now()
        window = timedelta(minutes=N8N_OWNER_WINDOW_MINUTES)
        candidates = Service.search([
            ('template_id.post_deploy_init_type', '=', 'n8n'),
            ('state', 'in', N8N_OWNER_SCAN_STATES),
            ('init_ready_since', '!=', False),
            ('init_ready_since', '<', now - window),
            ('n8n_owner_alerted_at', '=', False),
        ], order='init_ready_since, id')
        unconfirmed = candidates - self._n8n_owner_confirmed_services(candidates)
        if not unconfirmed:
            return
        todo, users = self._alert_recipients()
        if not (todo and users):
            # 告警送不出去就不記 marker，否則本 episode 的告警會永久消失。
            _logger.warning("[#1159] cannot raise monitor alert %r for %d service(s) "
                            "(todo=%s users=%s); will retry", self.SUMMARY_N8N_OWNER,
                            len(unconfirmed), bool(todo), len(users))
            return
        note = _(
            'The platform has not yet confirmed that the n8n owner exists within %(minutes)s '
            'minutes after it first observed the service ready. The owner may already be set up; '
            'only a successful init whose API key was stored confirms it. Until confirmed, anyone '
            'who can reach the URL might register the owner (#1159 Q28; route protection is #1264):',
            minutes=N8N_OWNER_WINDOW_MINUTES)
        alerting = self.with_context(mail_notify_force_send=False)
        for service in unconfirmed:
            alerting._alert(self.SUMMARY_N8N_OWNER, note, append_line=_(
                '%(name)s (id=%(id)s, state=%(state)s): ready first observed at %(ready)s UTC, '
                'owner not yet confirmed by %(deadline)s UTC',
                name=service.name, id=service.id, state=service.state,
                ready=service.init_ready_since, deadline=service.init_ready_since + window))
        unconfirmed.write({'n8n_owner_alerted_at': now})

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
                      self._check_unknown_results, self._check_init_capacity_wait,
                      self._check_n8n_owner_unconfirmed):
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
    def _report_storage_capacity_rejection(self, service, detail):
        """#1415：開通在 helm install 前被叢集儲存（Longhorn）額度擋下——一台一行。"""
        volumes = ', '.join(
            '%s %s x%s' % (v.get('name'), v.get('size'), v.get('replicas'))
            for v in detail.get('unschedulable') or [] if isinstance(v, dict))
        orphans = detail.get('orphaned_pvcs') or {}
        line = _(
            '%(name)s (id=%(id)s, namespace %(ns)s): cannot schedule %(volumes)s; the largest '
            'new volume that still fits is %(largest)s. Orphaned PVCs: %(orphans)s.',
            name=service.name, id=service.id, ns=detail.get('namespace') or '?',
            volumes=volumes or '?', largest=detail.get('largest_placeable_volume') or '?',
            orphans=self._orphans_text(orphans) if orphans else '?')
        return self._safe_alert(self.SUMMARY_STORAGE_REJECTED, _(
            'Deployments were rejected before anything was created because the cluster storage '
            '(Longhorn) has no room for their disks. The tenants were told to retry later. Free '
            'scheduling capacity (for example orphaned PVCs still in their retention period) or '
            'add storage:'), append_line=line)

    @api.model
    def _report_storage_expansion_rejection(self, service, plan, detail):
        """#1513：換方案在扣款前被 operator 升級預檢擋下（磁碟擴不了）——一台一行。"""
        pvcs = ', '.join(
            '%s %s→%s' % (v.get('name'), v.get('live'), v.get('rendered'))
            for v in detail.get('pvcs') or [] if isinstance(v, dict))
        line = _(
            '%(name)s (id=%(id)s, namespace %(ns)s) → plan "%(plan)s": %(pvcs)s (%(reason)s)',
            name=service.name, id=service.id, ns=detail.get('namespace') or '?',
            plan=plan.name, pvcs=pvcs or '?', reason=detail.get('reason') or '?')
        return self._safe_alert(self.SUMMARY_STORAGE_EXPANSION_REJECTED, _(
            'Plan changes were rejected before anything was charged or applied because a disk '
            'could not be enlarged (reason "capacity" means the cluster storage (Longhorn) has no '
            'room for the extra size on the disks holding its replicas). Free scheduling capacity '
            'or add storage, then ask the tenant to try again:'), append_line=line)

    @api.model
    def _report_compute_quota_rejection(self, service, operation, detail):
        """#1647：變更在套用（與扣款）前被 operator 的 compute 配額預檢擋下——一台一行。

        ``operation``：租戶的 ``reconfigure``／``change_plan``／``rollback``，與
        ``queued_redeploy``（管理員登記、由 cron 執行的 Redeploy；管理員當場按的 Redeploy 在
        對話框就看到數字，不走這裡）。後備路徑另有 ``background_reconfigure``／
        ``background_change_plan``：同步預檢沒問到、背景的 PATCH 才被擋——服務的狀況與說明文字
        講的不同，行尾會註明。``detail`` 是 operator 409 的 dict（``shortfalls``／``consumers``
        形狀不對時當成沒有）。同一台同一個原因反覆被擋時行的內容相同，``_alert`` 不會重複追加。

        **固定用 ``COMPUTE_QUOTA_ALERT_LANG`` 組字，不用呼叫端的語言。** 這個告警大多由租戶的
        請求觸發，``self.env`` 帶的是那位租戶的語言：照它組的話，同一台、同一個原因換一位不同
        語系的成員被擋，行的文字就不同（標點與半句譯文），``_alert`` 以文字去重會認不出來、
        再追加一行；行裡的數字段沒有翻譯，同一行還會中英夾雜。第一段說明文字也會變成「第一個
        觸發的人」的語言。⛔ 下面的 ``env._`` 不要改回模組層的 ``_``（那會讀呼叫端的語言）。
        """
        monitor = self.with_context(lang=COMPUTE_QUOTA_ALERT_LANG)
        env = monitor.env

        def describe(shortfall):
            text = '%s needs %s / limit %s / other workloads %s / short by %s' % (
                shortfall.get('resource'), shortfall.get('required'), shortfall.get('hard'),
                shortfall.get('other'), shortfall.get('short_by'))
            if shortfall.get('reason') == _COMPUTE_QUOTA_REASON_ROLLING_ON_DOWNGRADE:
                # 換方案降級：不是別人佔太多，而是新方案的上限放不下新舊並存——調高配額
                # 不是對的處置（租戶已被告知聯絡客服）。
                text += (' [plan downgrade: the smaller quota of the new plan cannot hold the '
                         'old and the new pod side by side during the rolling update]')
            return text

        shortfalls = '; '.join(
            describe(s) for s in _compute_quota_detail_rows(detail, 'shortfalls'))
        consumers = ', '.join(
            '%s (%s)' % (c.get('owner'), c.get('value'))
            for c in _compute_quota_detail_rows(detail, 'consumers'))
        line = env._(
            '%(name)s (id=%(id)s, namespace %(ns)s, %(operation)s): %(shortfalls)s. Largest '
            'other consumers: %(consumers)s.',
            name=service.name, id=service.id, ns=detail.get('namespace') or '?',
            operation=operation, shortfalls=shortfalls or '?', consumers=consumers or '-')
        # 後備路徑的行：說明文字講的是「套用與扣款之前就被擋下」，這兩種的現況不同，在行尾
        # 講清楚（與上面的降級註記一樣是固定的英文，不進翻譯）。
        line += {
            'background_reconfigure': (
                ' [refused during the background upgrade, after the pre-check gave no '
                'answer: the new settings are saved on the service but were not applied, and '
                'the service shows this failure]'),
            'background_change_plan': (
                ' [refused during the background upgrade, after the plan change went '
                'through: the plan was changed and charged but the service was not resized, '
                'and the service shows this failure]'),
        }.get(operation, '')
        return monitor._safe_alert(self.SUMMARY_COMPUTE_QUOTA_REJECTED, env._(
            'Changes were rejected before anything was applied or charged because the CPU / '
            'memory quota of the workspace cannot fit the service after the change. Tenants who '
            'requested a change were told to contact support. Raise the quota of that '
            'workspace or free resources in its namespace, then ask the tenant to try again '
            '(for a queued redeploy: redeploy the service again):'), append_line=line)

    @api.model
    def _orphans_text(self, orphans):
        return _('%(count)s PVCs, %(size)s requested, %(scheduled)s of Longhorn scheduling '
                 'capacity including replicas',
                 count=orphans.get('count', 0), size=orphans.get('requested') or '0Gi',
                 scheduled=orphans.get('longhorn_scheduled') or '0Gi')

    @api.model
    def _cluster_storage_min_volume_gib(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(CLUSTER_STORAGE_MIN_VOLUME_PARAM)
        try:
            value = int(raw) if raw not in (None, False, '') else CLUSTER_STORAGE_MIN_VOLUME_GIB_DEFAULT
        except (TypeError, ValueError):
            _logger.warning("[#1415] %s=%r is not an integer; using %s GiB",
                            CLUSTER_STORAGE_MIN_VOLUME_PARAM, raw,
                            CLUSTER_STORAGE_MIN_VOLUME_GIB_DEFAULT)
            value = CLUSTER_STORAGE_MIN_VOLUME_GIB_DEFAULT
        return max(0, value)

    @api.model
    def _cluster_storage_problems(self, report):
        """回 (問題行, 細節行)；問題行為空＝不告警。``report`` 是 operator 的額度回報。"""
        problems, details = [], []
        min_gib = self._cluster_storage_min_volume_gib()
        largest = report.get('largest_placeable_volume_bytes')
        if min_gib and isinstance(largest, int) and largest < min_gib * _GIB:
            problems.append(_(
                'The largest new volume with all %(replicas)s replicas that still fits is '
                '%(largest)s, below the %(min)s GiB the largest template disk needs (StorageClass '
                '%(sc)s).', replicas=report.get('replicas') or '?',
                largest=report.get('largest_placeable_volume') or '0Gi', min=min_gib,
                sc=report.get('storage_class') or '?'))
        for node in report.get('nodes') or []:
            ratio = node.get('scheduled_ratio')
            line = _('%(node)s: %(scheduled)s of %(limit)s reserved, %(headroom)s left on its '
                     'largest disk', node=node.get('name'), scheduled=node.get('scheduled'),
                     limit=node.get('limit'), headroom=node.get('headroom'))
            details.append(line)
            if node.get('schedulable') and isinstance(ratio, (int, float)) \
                    and ratio > CLUSTER_STORAGE_NODE_RATIO_LIMIT:
                problems.append(_(
                    'Node %(node)s has %(pct)s%% of its Longhorn scheduling limit reserved.',
                    node=node.get('name'), pct=int(ratio * 100)))
        if report.get('orphaned_pvcs'):
            details.append(_('Orphaned PVCs (kept for their retention period): %(orphans)s',
                             orphans=self._orphans_text(report['orphaned_pvcs'])))
        return problems, details

    @api.model
    def _cron_check_cluster_storage_capacity(self):
        """#1415：定時查 operator 的叢集儲存（Longhorn）排程額度，偏低時告警給系統管理員。

        條件（任一）：有備援的最大可放單顆 volume ＜ ``CLUSTER_STORAGE_MIN_VOLUME_PARAM``
        （預設 80 GiB，見 ``CLUSTER_STORAGE_MIN_VOLUME_GIB_DEFAULT``），或任一可排程節點的預訂率 ＞ 90%。告警附各節點
        額度與孤兒 PVC 佔用；同 summary 未處理前不重開（``_alert`` 的規則）。

        operator 太舊（沒有端點、404）安靜略過；operator 連不上或讀不到 Longhorn 只記 log
        （其他監控項管 operator 健康）。本方法只讀，不碰任何服務狀態。
        """
        from ..services.paas_operator import (
            PaaSOperatorAPIError, PaaSOperatorError, get_paas_operator_client,
        )
        client = get_paas_operator_client(self.env)
        if not client:
            return
        storage_class = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.storage_class_name', 'longhorn-delete') or None
        try:
            report = client.get_cluster_storage_capacity(storage_class=storage_class)
        except PaaSOperatorAPIError as e:
            if e.status_code == 404:
                _logger.info("[#1415] operator has no /api/cluster/storage-capacity yet; skipped")
            else:
                _logger.warning("[#1415] cluster storage capacity check failed: %s", e)
            return
        except PaaSOperatorError as e:
            _logger.warning("[#1415] cluster storage capacity check failed: %s", e)
            return
        if not isinstance(report, dict) or not report.get('available'):
            _logger.info("[#1415] cluster storage capacity not available: %s",
                         (report or {}).get('reason') if isinstance(report, dict) else report)
            return
        problems, details = self._cluster_storage_problems(report)
        if not problems:
            return
        note = Markup('<br/>').join(problems + [''] + details)
        self.with_context(mail_notify_force_send=False)._alert(self.SUMMARY_STORAGE_LOW, note)

    @api.model
    def _report_gave_up(self, service, exit_kind, message):
        """等待逾時／fast-fail 轉 error 時追加一行（unknown 由 ``_check_unknown_results`` 管）。"""
        line = _('%(name)s (id=%(id)s): %(exit)s — %(message)s', name=service.name,
                 id=service.id, exit=exit_kind, message=message)
        self._safe_alert(self.SUMMARY_GAVE_UP, _(
            'Post-deploy initialization gave up on the following service(s):'),
            append_line=line)

    @api.model
    def _report_workload_fault(self, service):
        """#1647：running 的服務有 workload 期望有 Pod、實際一顆都沒有——一台一行。

        回傳「這一台的通知有沒有落地」：新開一張或追加到既有那張都算 True；沒有收件者、
        或寫入失敗回 False。呼叫端（``CloudService._apply_workload_observation``）只在 True
        時寫 ``workload_fault_alerted_at``，False 的下一輪重試。

        ⚠ 不用 ``_alert`` 的回傳值：它回的是「有沒有**新開**一張」，追加到既有 activity 時
        也是 False——拿它當「通知落地了沒」，第二台起的服務會每一輪都重複追加。
        """
        service = service.sudo()
        monitor = self._singleton()
        existing = self.env['mail.activity'].sudo().search([
            ('res_model', '=', self._name), ('res_id', '=', monitor.id),
            ('summary', '=', self.SUMMARY_WORKLOAD_FAULT)])
        todo, users = self._alert_recipients()
        if not existing and not (todo and users):
            # 送不出去就不讓呼叫端記標記，否則這一段異常的告警會永久消失。
            _logger.warning(
                "[#1647] cannot raise monitor alert %r for service id=%s (todo=%s users=%s); "
                "will retry", self.SUMMARY_WORKLOAD_FAULT, service.id, bool(todo), len(users))
            return False
        line = _(
            '%(name)s (id=%(id)s, namespace %(ns)s, release %(release)s), since %(since)s UTC: '
            '%(detail)s',
            name=service.name, id=service.id, ns=service.helm_namespace or '?',
            release=service.helm_release_name or '?', since=service.workload_fault_since,
            detail=service.workload_fault_detail or '?')
        note = _(
            'These services are shown as running, but at least one of their workloads '
            '(Deployment or StatefulSet) wants pods and has none, so the service is actually '
            'down. The most common cause is the namespace ResourceQuota rejecting the pods '
            '(FailedCreate: exceeded quota). Check the quota of the namespace and the events '
            'of its ReplicaSets and StatefulSets. The platform clears the flag by itself once '
            'the pods are back. The service state has not been changed:')
        try:
            with self.env.cr.savepoint():
                # 只排 queue（同 `_safe_alert`）：savepoint 回捲時不留下已同步寄出的信。
                self.with_context(mail_notify_force_send=False)._alert(
                    self.SUMMARY_WORKLOAD_FAULT, note, append_line=line)
            return True
        except OperationalError:
            raise
        except Exception:
            _logger.exception(
                "[#1647] monitor alert %r could not be recorded for service id=%s",
                self.SUMMARY_WORKLOAD_FAULT, service.id)
            return False

    @api.model
    def _note_workload_recovered(self, service, since):
        """#1647：通知過的 workload 異常恢復了——在還沒處理的那張 activity 追加一行。

        不新開 activity（恢復不是要人處理的事）；都處理完了就只留在 monitor 的 chatter。
        通知失敗只記 log：欄位已經清掉了，恢復本身不因此不成立。
        """
        service = service.sudo()
        line = _(
            '%(name)s (id=%(id)s): its workloads have pods again (had none since '
            '%(since)s UTC)', name=service.name, id=service.id, since=since)
        try:
            with self.env.cr.savepoint():
                monitor = self._singleton()
                activities = self.env['mail.activity'].sudo().search([
                    ('res_model', '=', self._name), ('res_id', '=', monitor.id),
                    ('summary', '=', self.SUMMARY_WORKLOAD_FAULT)])
                for activity in activities:
                    activity.note = (activity.note or Markup()) + Markup('<p>%s</p>') % line
                monitor._message_log(body=line)
        except OperationalError:
            raise
        except Exception:
            _logger.exception(
                "[#1647] workload recovery of service id=%s could not be recorded", service.id)
