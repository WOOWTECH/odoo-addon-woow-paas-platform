"""#1540：停權／刪除後的限時唯讀資料取回入口（平台端）。

服務停權（``state='suspended'``，scale 0）或已刪除（磁碟仍在保留期）時，工作區 owner／admin
可以在 /woow 按「取回資料」：平台請 operator 在同一個工作區 namespace 開一個臨時、唯讀的
檔案瀏覽 pod（basic auth、經 tunnel 掛臨時 hostname、預設 72 小時後自動關閉）。資料不經過
平台、平台也不保存檔案；**密碼只在開的回應出現一次**，這裡的兩個 model 都不存它。

兩個 model（ACL 只給 ``base.group_system``；租戶一律經 controller → 本檔的 private method）：

* ``retained_service_data``：已刪除、磁碟仍在保留期的服務帳本。刪除會 ``unlink()`` 服務記錄，
  沒有這本帳，/woow 就再也不知道那顆磁碟屬於哪個工作區、叫什麼名字。在
  ``CloudService.unlink()``（``super().unlink()`` 之前）建立。
* ``data_retrieval``：一次開啟的入口（session）。同一個 (namespace, release) 同時只能有一筆
  ``active``／``closing``（部分唯一索引；operator 端另有同名物件的天然唯一）。

operator 才是權威：開的時候 operator 會再驗一次（PVC 沒有被活 pod 掛著、release 已縮到 0
或已刪除且保留期內），平台這裡的檢查只是為了給租戶看得懂的訊息。
"""
import logging
from datetime import datetime, timedelta, timezone

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

#: 一個入口開多久（小時）。operator 另有上限 ``DATA_RETRIEVAL_MAX_TTL_HOURS``（72），已刪除
#: 服務還會截到磁碟的保留期限。
RETRIEVAL_TTL_HOURS = 72
#: 已刪除服務的磁碟保留天數（平台端的**估計**，只用來顯示「保留到」與決定帳本列何時下架）。
#: 權威是 operator 貼在 PVC 上的 grace-period-days（prod 90、stg 7），開的時候 operator 會驗。
DATA_RETENTION_DAYS_PARAM = 'woow_paas_platform.data_retention_days'
DEFAULT_DATA_RETENTION_DAYS = 90
#: 帳本列在保留期過後多留多久才刪（讓「已過期」的列不會在租戶眼前忽隱忽現）。
RETAINED_ROW_PURGE_GRACE = timedelta(days=1)
#: cron 一輪最多處理幾筆 session（其餘下一輪）。
CRON_BATCH = 50

OPEN_STATES = ('active', 'closing')

# operator 的 ``detail.error_code``（extra/paas-operator ``services/data_retrieval.py``；改名兩邊一起改）。
ERR_ALREADY_OPEN = 'retrieval_already_open'
ERR_RELEASE_RUNNING = 'retrieval_release_running'
ERR_PVC_IN_USE = 'retrieval_pvc_in_use'
ERR_PVC_NOT_FOUND = 'retrieval_pvc_not_found'
ERR_RETENTION_EXPIRED = 'data_retention_expired'
ERR_QUOTA_EXCEEDED = 'retrieval_quota_exceeded'
ERR_CLOSING = 'retrieval_closing'
ERR_PROFILE_UNKNOWN = 'retrieval_profile_unknown'
ERR_ROUTE_CLEANUP_FAILED = 'route_cleanup_failed'


def _error_code(error):
    detail = getattr(error, 'detail', None)
    return detail.get('error_code') if isinstance(detail, dict) else None


def _parse_operator_time(value):
    """operator 回的 ISO 8601（帶 Z／offset）→ Odoo 的 naive UTC datetime；壞值回 False。"""
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return False
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso_utc(value):
    """naive UTC datetime → 帶 offset 的 ISO（前端照當地時間顯示；同 #1456 的 SHAPE-4）。"""
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


class RetainedServiceData(models.Model):
    _name = 'woow_paas_platform.retained_service_data'
    _description = 'Retained data of a deleted cloud service (#1540)'
    _order = 'deleted_at desc, id desc'

    name = fields.Char(string='Service Name', required=True)
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        required=True,
        ondelete='cascade',
        index=True,
    )
    template_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_app_template',
        ondelete='set null',
    )
    helm_namespace = fields.Char(required=True)
    helm_release_name = fields.Char(required=True)
    data_retrieval_profile = fields.Char(
        required=True,
        help='Snapshot of the template profile at deletion time (operator whitelist name).',
    )
    deleted_at = fields.Datetime(required=True, default=fields.Datetime.now)
    retained_until = fields.Datetime(
        required=True,
        help='Platform estimate of when the retained volume is reclaimed. The operator '
             'checks the real retention label when an entrance is opened.',
    )

    _sql_constraints = [
        ('retained_service_data_release_uniq', 'UNIQUE(helm_namespace, helm_release_name)',
         'Each deleted release has at most one retained-data record.'),
    ]

    @api.model
    def _retention_days(self):
        raw = self.env['ir.config_parameter'].sudo().get_param(DATA_RETENTION_DAYS_PARAM)
        try:
            days = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_DATA_RETENTION_DAYS
        return days if days > 0 else DEFAULT_DATA_RETENTION_DAYS

    @api.model
    def _record_deleted_service(self, service):
        """``CloudService.unlink()`` 在 ``super().unlink()`` 前呼叫：記下這台服務留下的磁碟。

        只記「範本支援取回、有 release、曾經 running」的服務——從沒 running 過的服務沒有
        租戶資料（operator 只保留 1 天，#1431）。回傳帳本列（沒記回空 recordset）。
        """
        service.ensure_one()
        Retained = self.sudo()
        profile = service.template_id.data_retrieval_profile
        if not (profile and service.helm_namespace and service.helm_release_name
                and service.first_running_at):
            return Retained.browse()
        existing = Retained.search([
            ('helm_namespace', '=', service.helm_namespace),
            ('helm_release_name', '=', service.helm_release_name),
        ], limit=1)
        if existing:
            return existing
        now = fields.Datetime.now()
        return Retained.create({
            'name': service.name or service.helm_release_name,
            'workspace_id': service.workspace_id.id,
            'template_id': service.template_id.id,
            'helm_namespace': service.helm_namespace,
            'helm_release_name': service.helm_release_name,
            'data_retrieval_profile': profile,
            'deleted_at': now,
            'retained_until': now + timedelta(days=self._retention_days()),
        })

    @api.model
    def _for_workspace(self, workspace):
        """工作區裡還在保留期的帳本列（呼叫端已確認呼叫者是 owner／admin）。"""
        return self.sudo().search([
            ('workspace_id', '=', workspace.id),
            ('retained_until', '>', fields.Datetime.now()),
        ])

    @api.model
    def _get_for_workspace(self, workspace, retained_id):
        """工作區裡 id 為 ``retained_id`` 的帳本列（不在這個工作區、或已過保留期都回空）。"""
        row = self.sudo().browse(int(retained_id)).exists()
        if not row or row.workspace_id.id != workspace.id:
            return self.sudo().browse()
        return row

    def _retrieval_view(self, live=False):
        """與 ``CloudService._data_retrieval_view`` 同形（前端兩個入口共用同一個視窗）。"""
        self.ensure_one()
        session = self.env['woow_paas_platform.data_retrieval']._open_session_for(
            self.helm_namespace, self.helm_release_name)
        live_status = session._live_status() if (session and live) else None
        session = session.exists().filtered(lambda s: s.state != 'closed')
        return {
            'supported': True,
            'available': self.retained_until > fields.Datetime.now(),
            'session': session._to_dict(live_status=live_status) if session else None,
        }

    def _to_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'template': {
                'name': self.template_id.name or '',
                'slug': self.template_id.slug or '',
            },
            'deleted_at': _iso_utc(self.deleted_at),
            'retained_until': _iso_utc(self.retained_until),
            'data_retrieval': self._retrieval_view(),
        }

    def _close_retrieval(self):
        """租戶手動關閉（沒有開著的入口＝冪等成功）。"""
        self.ensure_one()
        session = self.env['woow_paas_platform.data_retrieval']._open_session_for(
            self.helm_namespace, self.helm_release_name)
        if session:
            session._close('manual', raise_on_error=True)
        return True

    def _open_retrieval(self, user):
        """開這顆已刪除服務磁碟的取回入口。回 ``(session, credentials)``。"""
        self.ensure_one()
        if self.retained_until <= fields.Datetime.now():
            raise UserError(_("The retention period of this deleted service's data has ended."))
        return self.env['woow_paas_platform.data_retrieval']._open(
            workspace=self.workspace_id, name=self.name, namespace=self.helm_namespace,
            release=self.helm_release_name, profile=self.data_retrieval_profile, user=user,
            retained=self)


class DataRetrieval(models.Model):
    _name = 'woow_paas_platform.data_retrieval'
    _description = 'Temporary read-only data retrieval entrance (#1540)'
    _order = 'opened_at desc, id desc'

    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        required=True,
        ondelete='cascade',
        index=True,
    )
    service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        ondelete='set null',
        index=True,
    )
    retained_data_id = fields.Many2one(
        comodel_name='woow_paas_platform.retained_service_data',
        ondelete='set null',
        index=True,
    )
    service_name = fields.Char()
    helm_namespace = fields.Char(required=True)
    helm_release_name = fields.Char(required=True)
    profile = fields.Char(required=True)
    state = fields.Selection(
        selection=[
            ('active', 'Active'),
            ('closing', 'Closing'),   # pod 已停、route 待刪（cron 重試）
            ('closed', 'Closed'),
        ],
        required=True,
        default='active',
        index=True,
    )
    hostname = fields.Char()
    username = fields.Char()
    opened_by_id = fields.Many2one(comodel_name='res.users', ondelete='set null')
    opened_at = fields.Datetime()
    expires_at = fields.Datetime(index=True)
    closed_at = fields.Datetime()
    close_reason = fields.Selection(
        selection=[
            ('manual', 'Closed by user'),
            ('expired', 'Expired'),
            ('resumed', 'Service resumed'),
            ('external', 'Gone on the cluster'),
        ],
    )
    last_error = fields.Char()

    def init(self):
        # 同一個 release 同時只能有一個入口（active／closing）。closed 的列是稽核紀錄，不受限。
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS woow_paas_platform_data_retrieval_one_open
            ON woow_paas_platform_data_retrieval (helm_namespace, helm_release_name)
            WHERE state IN ('active', 'closing')
        """)

    # ------------------------------------------------------------------ #
    #  查詢                                                               #
    # ------------------------------------------------------------------ #

    @api.model
    def _open_session_for(self, namespace, release):
        if not (namespace and release):
            return self.sudo().browse()
        return self.sudo().search([
            ('helm_namespace', '=', namespace),
            ('helm_release_name', '=', release),
            ('state', 'in', OPEN_STATES),
        ], limit=1)

    def _to_dict(self, live_status=None):
        """前端用的 session 資訊（**不含密碼**——平台根本沒有它）。"""
        self.ensure_one()
        return {
            'id': self.id,
            'state': self.state,
            'live_status': live_status,
            'hostname': self.hostname or None,
            'url': f'https://{self.hostname}/' if self.hostname else None,
            'username': self.username or None,
            'opened_at': _iso_utc(self.opened_at),
            'expires_at': _iso_utc(self.expires_at),
            'opened_by': self.opened_by_id.name or None,
        }

    def _live_status(self):
        """問 operator 這個入口現在的狀態（``active``／``starting``／``stopped``）；問不到回 None。

        operator 說已經沒有這個入口（sweep 收掉了、到期被刪）⇒ 這筆標成 closed。
        """
        self.ensure_one()
        from ..services.paas_operator import PaaSOperatorError, get_paas_operator_client
        client = get_paas_operator_client(self.env)
        if not client:
            return None
        try:
            result = client.get_data_retrieval(self.helm_namespace, self.helm_release_name)
        except PaaSOperatorError as e:
            _logger.info("[DATA_RETRIEVAL] status of %s/%s unavailable: %s",
                         self.helm_namespace, self.helm_release_name, e)
            return None
        status = (result or {}).get('status')
        if status == 'not_found' and self.state == 'active':
            expired = self.expires_at and self.expires_at <= fields.Datetime.now()
            self.sudo().write({
                'state': 'closed',
                'closed_at': fields.Datetime.now(),
                'close_reason': 'expired' if expired else 'external',
            })
            return None
        return status

    # ------------------------------------------------------------------ #
    #  開                                                                 #
    # ------------------------------------------------------------------ #

    @api.model
    def _operator_error_text(self, error):
        """operator 錯誤 → 租戶看得懂的一句話（不透出 operator 原文）。"""
        code = _error_code(error)
        if getattr(error, 'status_code', None) == 404:
            return _("Data retrieval is not available yet on this platform. "
                     "Please contact support.")
        messages = {
            ERR_ALREADY_OPEN: _("A data retrieval link is already open for this service. "
                                "Close it before opening a new one."),
            ERR_RELEASE_RUNNING: _("The service is still running. Data retrieval is only "
                                   "available while the service is suspended."),
            ERR_PVC_IN_USE: _("The service has not fully stopped yet. Please try again in a "
                              "minute."),
            ERR_CLOSING: _("The previous data retrieval link is still shutting down. Please "
                           "try again in a minute."),
            ERR_PVC_NOT_FOUND: _("The data disk of this service could not be found."),
            ERR_RETENTION_EXPIRED: _("The retention period of this deleted service's data "
                                     "has ended."),
            # ⛔ 出口只有「聯絡客服」，不要加回「停止／刪除其他服務再試」：/woow 沒有讓租戶
            # 停止服務的操作，刪除也換不回空間（理由同 ``cloud_service.py`` 的
            # ``_compute_quota_error_message``）。也不要寫成配額已滿／用完——常見形狀是
            # 「還有剩，但不夠啟動」，會與工作區頁配額面板的剩餘量對不起來（#1686）。
            ERR_QUOTA_EXCEEDED: _("This workspace does not have enough CPU or memory quota "
                                  "left to start the download service. Please contact "
                                  "support."),
            ERR_PROFILE_UNKNOWN: _("This service does not support data retrieval."),
        }
        return messages.get(code) or _(
            "Could not open the data retrieval link. Please try again later.")

    @api.model
    def _open(self, *, workspace, name, namespace, release, profile, user,
              service=None, retained=None):
        """請 operator 開入口並記下 session。回 ``(session, credentials)``；credentials 含一次性密碼。

        密碼只存在回傳值裡：**不得**寫進 DB、log 或例外訊息。
        """
        from ..services.paas_operator import PaaSOperatorError, get_paas_operator_client
        Session = self.sudo()
        if not profile:
            raise UserError(_("This service does not support data retrieval."))
        existing = Session._open_session_for(namespace, release)
        if existing and existing.state == 'closing':
            # 上一個入口的 pod 已停、只剩 hostname 沒刪成：先再收一次，收乾淨就可以開新的。
            existing._close(existing.close_reason or 'manual')
            existing = Session._open_session_for(namespace, release)
        if existing:
            raise UserError(_("A data retrieval link is already open for this service. "
                              "Close it before opening a new one."))
        client = get_paas_operator_client(self.env)
        if not client:
            raise UserError(_("PaaS Operator not configured. Contact administrator."))
        try:
            result = client.open_data_retrieval(namespace, release, profile, RETRIEVAL_TTL_HOURS)
        except PaaSOperatorError as e:
            _logger.warning("[DATA_RETRIEVAL] open %s/%s refused: status=%s code=%s",
                            namespace, release, getattr(e, 'status_code', None), _error_code(e))
            raise UserError(self._operator_error_text(e))
        result = result or {}
        vals = {
            'workspace_id': workspace.id,
            'service_id': service.id if service else False,
            'retained_data_id': retained.id if retained else False,
            'service_name': name,
            'helm_namespace': namespace,
            'helm_release_name': release,
            'profile': profile,
            'state': 'active',
            'hostname': result.get('hostname') or False,
            'username': result.get('username') or False,
            'opened_by_id': user.id,
            'opened_at': _parse_operator_time(result.get('opened_at')) or fields.Datetime.now(),
            'expires_at': _parse_operator_time(result.get('expires_at')) or False,
        }
        try:
            with self.env.cr.savepoint():
                session = Session.create(vals)
        except Exception:
            # 入口已經開了、帳卻沒記成：立刻關掉（不留沒人看得到的入口），再回報失敗。
            _logger.exception("[DATA_RETRIEVAL] could not record session for %s/%s; closing it",
                              namespace, release)
            try:
                client.close_data_retrieval(namespace, release)
            except PaaSOperatorError:
                _logger.exception("[DATA_RETRIEVAL] rollback close of %s/%s failed (the "
                                  "operator sweep will close it at expiry)", namespace, release)
            raise UserError(_("Could not open the data retrieval link. Please try again later."))
        _logger.info("[DATA_RETRIEVAL] opened %s/%s by uid=%s host=%s expires=%s",
                     namespace, release, user.id, session.hostname, session.expires_at)
        credentials = {
            'url': result.get('url') or None,
            'hostname': result.get('hostname') or None,
            'username': result.get('username') or None,
            'password': result.get('password') or None,
            'expires_at': _iso_utc(session.expires_at),
            'data_expires_at': _iso_utc(_parse_operator_time(result.get('data_expires_at'))),
        }
        return session, credentials

    # ------------------------------------------------------------------ #
    #  關                                                                 #
    # ------------------------------------------------------------------ #

    def _close(self, reason, raise_on_error=False):
        """關入口（冪等）。operator 先停 pod 再刪 route；route 刪失敗 ⇒ ``closing``（cron 重試）。

        Returns: 全部成功關閉回 True。
        """
        from ..services.paas_operator import PaaSOperatorError, get_paas_operator_client
        client = get_paas_operator_client(self.env)
        ok = True
        for session in self.sudo():
            if session.state == 'closed':
                continue
            if not client:
                ok = False
                if raise_on_error:
                    raise UserError(_("PaaS Operator not configured. Contact administrator."))
                continue
            subdomain = (session.hostname or '').split('.', 1)[0] or None
            try:
                client.close_data_retrieval(
                    session.helm_namespace, session.helm_release_name, subdomain)
            except PaaSOperatorError as e:
                code = _error_code(e)
                if code == ERR_ROUTE_CLEANUP_FAILED:
                    # pod 已停（資料不再對外），只剩 hostname 沒刪成：交給 cron 重試。
                    session.write({'state': 'closing', 'close_reason': reason,
                                   'last_error': code})
                    continue
                if getattr(e, 'status_code', None) == 404:
                    # 舊 operator：它不可能開過任何入口。
                    session.write({'state': 'closed', 'closed_at': fields.Datetime.now(),
                                   'close_reason': reason, 'last_error': 'operator_404'})
                    continue
                ok = False
                session.write({'last_error': code or (getattr(e, 'message', None) or 'error')[:200]})
                _logger.warning("[DATA_RETRIEVAL] close %s/%s failed: %s",
                                session.helm_namespace, session.helm_release_name, e)
                if raise_on_error:
                    raise UserError(_("Could not close the data retrieval link. Please try "
                                      "again later."))
                continue
            session.write({
                'state': 'closed',
                'closed_at': fields.Datetime.now(),
                'close_reason': session.close_reason if session.state == 'closing' else reason,
                'last_error': False,
            })
            _logger.info("[DATA_RETRIEVAL] closed %s/%s (%s)", session.helm_namespace,
                         session.helm_release_name, reason)
        return ok

    @api.model
    def _close_for_release(self, namespace, release, reason):
        """復權前呼叫：關掉這個 release 的入口（RWO：入口的 pod 不走，服務的 pod 掛不上磁碟）。

        **永不 raise**：關不掉只記 log，不擋復權（入口的 pod 到期也會自己停）。
        """
        session = self._open_session_for(namespace, release)
        if not session:
            return True
        try:
            return session._close(reason)
        except Exception:  # noqa: BLE001 — 不得讓復權失敗
            _logger.exception("[DATA_RETRIEVAL] close before resume failed for %s/%s",
                              namespace, release)
            return False

    # ------------------------------------------------------------------ #
    #  cron                                                               #
    # ------------------------------------------------------------------ #

    @api.model
    def _cron_close_expired(self):
        """每 15 分鐘：關到期的入口、重試 closing、請 operator sweep、刪過期的帳本列。"""
        from ..services.paas_operator import PaaSOperatorError, get_paas_operator_client
        now = fields.Datetime.now()
        Session = self.sudo()
        due = Session.search([
            '|',
            '&', ('state', '=', 'active'), ('expires_at', '<=', now),
            ('state', '=', 'closing'),
        ], limit=CRON_BATCH, order='expires_at asc, id asc')
        for session in due:
            try:
                with self.env.cr.savepoint():
                    session._close('expired')
            except Exception:  # noqa: BLE001 — 一筆壞掉不擋其他筆
                _logger.exception("[DATA_RETRIEVAL] cron close failed for session %s", session.id)
        client = get_paas_operator_client(self.env)
        if client:
            try:
                result = client.sweep_data_retrievals() or {}
                if result.get('closed') or result.get('failed'):
                    _logger.info("[DATA_RETRIEVAL] operator sweep closed=%s failed=%s",
                                 result.get('closed'), result.get('failed'))
            except PaaSOperatorError as e:
                if getattr(e, 'status_code', None) != 404:
                    _logger.warning("[DATA_RETRIEVAL] operator sweep failed: %s", e)
        stale = self.env['woow_paas_platform.retained_service_data'].sudo().search([
            ('retained_until', '<', now - RETAINED_ROW_PURGE_GRACE),
        ], limit=CRON_BATCH)
        busy = set(Session.search([
            ('retained_data_id', 'in', stale.ids), ('state', 'in', OPEN_STATES),
        ]).mapped('retained_data_id').ids)
        stale.filtered(lambda r: r.id not in busy).unlink()
        if len(due) >= CRON_BATCH:
            self.env.ref('woow_paas_platform.ir_cron_data_retrieval_maintenance')._trigger()
