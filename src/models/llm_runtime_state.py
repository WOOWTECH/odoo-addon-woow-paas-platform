"""AI 模型的執行期狀態（單例；DESIGN §2.5；票 LLM-02b）。

游標、guard、circuit、canary、DR 模式、設定指紋，以及 LLM-02b 的餘額刷新進度。不放
``ir.config_parameter``：游標每分鐘寫一次，而 ICP 的寫入會清整個 registry 的 ormcache
（DESIGN §2.5；circuit 照 ``models/ecpg_query_circuit.py:19-20`` 的先例）。

* **單例**：表上有一個建在常數上的 unique index（``(true)``），最多一列；``init()`` 在安裝／
  升級時補上那一列，:meth:`LlmRuntimeState._llm_read`／:meth:`_llm_write` 遇到它不見了
  （例如有人以 SQL 刪掉）也會補回來。
* **讀寫一律走獨立 cursor**：cron 與請求各自的交易都是 REPEATABLE READ，主 cursor 的
  snapshot 看不到別的 tick 剛寫的游標；寫入也不該跟著呼叫端的交易一起 rollback（例如餘額刷新
  已經處理完的頁數）。
* 守衛同其他 LLM model：所有群組 ACL 只有讀；ORM 的 create／write 要 ``_llm_internal`` 的把手；
  unlink 只在模組卸載時允許。

**circuit breaker 與 AP 限速**（DESIGN §1.5、§3.6；LLM-02）也住在這一列，因為每個 pod、每條 cron
thread 都要看到同一份：

* :class:`LlmCallGuard` 是 ``newapi_client.CallGuard`` 的 DB 實作，:func:`newapi_client.get_newapi_client`
  一律掛上它。送出前：circuit 開著（``circuit_open_until`` 還沒到，或到了但還沒有人做完健康檢查）⇒
  ``NotExecuted('circuit_open')``，不送；``ap`` 身分另外經 :meth:`LlmRuntimeState._llm_ap_rate_acquire`
  （固定一分鐘窗、全平台 60 次，依優先序各有上限）。分類完：``newapi_client.circuit_effect`` 決定這次算
  失敗（連續 5 次 ⇒ 開 2 分鐘）、一次就開（代理 401／403 ``policy``、AP／OP 被拒）、成功（失敗數歸零）或不計。
  開的那一次告警 ``circuit_open``（錯誤碼＝原因）。``bg`` 不受 circuit 擋、也不計，但每次都告警
  ``break_glass_used``。
* **半開**：``circuit_open_until`` 過了之後，只有 cron 能把它關上——:func:`llm_common.run_llm_cron` 在外呼類
  cron 的 tick 開頭呼叫 :meth:`LlmRuntimeState._llm_circuit_gate`：搶到「這一輪由我探」的人以 AP 做一次會碰 DB
  的健康檢查（``NewApiClient.ap_health``），成功就關、失敗就再開 2 分鐘；沒搶到或還沒到的 ⇒ 跳過這一輪。
  web 在半開時照樣回 ``circuit_open``，並 ``_trigger()`` 一支外呼類 cron 去探（web pod 沒有 AP）。
* 每次呼叫多一次獨立 cursor 的讀（與 ap 的一次限速寫）；成功時只有在失敗數 > 0 才寫。
"""
import contextvars
from contextlib import contextmanager
import json
import logging
import time
import traceback
from datetime import timedelta

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError

from ..services import llm_redact
from ..services.llm_sql_budget import cursor_deadline, deadline_cursor, trigger_cron
from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    LlmQuotaInteger,
    _is_llm_internal,
    _without_llm_internal,
    llm_config,
    llm_unconfigured,
    report_break_glass_used,
    report_circuit_open,
    run_llm_cron,
)

_logger = logging.getLogger(__name__)

STATE_MODEL = 'woow_paas_platform.llm_runtime_state'
STATE_TABLE = 'woow_paas_platform_llm_runtime_state'
_LOCK_TIMEOUT = '5s'

#: 可以經 :meth:`LlmRuntimeState._llm_write` 改的欄位（＝本 model 的業務欄位）。
STATE_FIELDS = (
    'canary_generation', 'canary_started_at', 'canary_lease_until', 'canary_work',
    'canary_probe_result', 'canary_latency_ms', 'canary_failures',
    'status_armed', 'status_generation', 'status_started_at', 'status_completed_at',
    'status_success_at', 'status_result', 'status_fingerprint',
    # 免費試用計數的游標（LLM-10；DESIGN §1.3.3）。new-api 的 log id 與 unix 秒都用 int8。
    'free_meter_last_log_id',
    'free_meter_last_ts',
    'free_meter_scan_start',
    'free_meter_scan_end',
    'free_meter_scan_page',
    'free_meter_scan_max_id',
    # I3 的「外部入帳」水位線（LLM-12b）。
    'external_credit_watermark_ts',
    # 不變量 guard（N17；LLM-12b／LLM-14）。
    'guard_tripped',
    'guard_reasons',
    'guard_since',
    # circuit breaker（DESIGN §3.6；LLM-02）。
    'circuit_open_until',
    'circuit_failures',
    # canary（N16；LLM-14）。
    'last_canary_at',
    'last_canary_result',
    # DR 模式（DESIGN §5.6；LLM-19）。
    'dr_mode',
    # newapi_proxy_url／llm_public_base_url／newapi_username_prefix 的指紋（DESIGN §2.11）。
    'last_config_fingerprint',
    # 餘額刷新（LLM-02b；DESIGN §1.3.6）：下一頁、這一輪開始與上一輪跑完的時間。
    'balance_refresh_page',
    'balance_refresh_started_at',
    'balance_refresh_finished_at',
    # circuit 是被什麼打開的（錯誤碼）與何時（LLM-02）。
    'circuit_reason',
    'circuit_opened_at',
    # 後台「測試連線」（LLM-02）：請求時間、上一次結果（JSON：只有狀態碼、天數與布林）與時間。
    'connection_test_requested_at',
    'connection_test_at',
    'connection_test_result',
)
#: 健康檢查搶到之後，這麼久之內別人當成「還開著」（探的人有這麼多時間做完一次 cron 讀取）。
CIRCUIT_PROBE_LEASE_SECONDS = 30
#: 半開時 web 要叫醒去探的那支 cron（外呼類、以 CRON 金鑰跑；_trigger() 很便宜）。
CIRCUIT_PROBE_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_swr_refresh'
#: 後台「測試連線」：按鈕只記請求時間並叫醒這支 cron（web pod 沒有 AP，DESIGN §1.4.2-1）。
CONNECTION_TEST_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_connection_test'
#: 「這一次是健康檢查」：:meth:`LlmRuntimeState._llm_circuit_gate` 探的那一次呼叫不受 circuit 擋。
_PROBING = contextvars.ContextVar('woow_llm_circuit_probe', default=False)
_STATUS_PROBE = contextvars.ContextVar('woow_llm_status_probe', default=None)


@contextmanager
def status_probe(client):
    """Allow exactly one anon call on this CRON client's own DB guard.

    Used only around client.status(). Other identities/clients in this scope
    are refused, not exempted from the circuit; normal behavior is restored
    even when transport raises. This never closes the circuit.
    """
    guard = client._guard
    if not isinstance(guard, LlmCallGuard) or guard.role != nac.ROLE_CRON:
        raise ValueError('invalid_status_probe')
    token = _STATUS_PROBE.set({'guard': guard, 'remaining': 1})
    try:
        yield
    finally:
        _STATUS_PROBE.reset(token)


class LlmRuntimeState(models.Model):
    _name = 'woow_paas_platform.llm_runtime_state'
    _description = 'AI Models Runtime State (singleton)'

    free_meter_last_log_id = LlmQuotaInteger(readonly=True, default=0)
    free_meter_last_ts = LlmQuotaInteger(readonly=True, default=0,
                                         help='Unix seconds of the last free-usage log read.')
    free_meter_scan_start = LlmQuotaInteger(readonly=True, default=0)
    free_meter_scan_end = LlmQuotaInteger(readonly=True, default=0)
    free_meter_scan_page = fields.Integer(readonly=True, default=0)
    free_meter_scan_max_id = LlmQuotaInteger(readonly=True, default=0)
    external_credit_watermark_ts = LlmQuotaInteger(readonly=True, default=0,
                                                   help='Unix seconds (invariant I3).')
    guard_tripped = fields.Boolean(readonly=True, default=False)
    guard_reasons = fields.Char(readonly=True, help='Comma-separated reason codes.')
    guard_since = fields.Datetime(readonly=True)
    circuit_open_until = fields.Datetime(readonly=True)
    circuit_failures = fields.Integer(readonly=True, default=0)
    last_canary_at = fields.Datetime(readonly=True)
    last_canary_result = fields.Char(readonly=True, help='Result code of the last canary run.')
    dr_mode = fields.Boolean(readonly=True, default=False)
    last_config_fingerprint = fields.Char(readonly=True)
    balance_refresh_page = fields.Integer(readonly=True, default=1)
    balance_refresh_started_at = fields.Datetime(readonly=True)
    balance_refresh_finished_at = fields.Datetime(readonly=True)
    circuit_reason = fields.Char(string='Circuit reason', readonly=True,
                                 help='Error code that opened the circuit.')
    circuit_opened_at = fields.Datetime(string='Circuit opened at', readonly=True)
    #: AP 限速（DESIGN §1.5）：目前這一分鐘（unix 秒 // 60）與這一分鐘已經用了幾次。只經
    #: :meth:`_llm_ap_rate_acquire` 的原子 UPDATE 改，不在 STATE_FIELDS（``_llm_write`` 改不到）。
    ap_rate_window = LlmQuotaInteger(string='AP rate window (unix minute)', readonly=True,
                                     default=0)
    ap_rate_count = fields.Integer(string='AP calls this minute', readonly=True, default=0)
    connection_test_requested_at = fields.Datetime(string='Connection test requested at',
                                                   readonly=True)
    connection_test_at = fields.Datetime(string='Last connection test at', readonly=True)
    connection_test_result = fields.Text(
        string='Last connection test result', readonly=True,
        help='Last connection test (JSON: states, day counts and booleans only).')

    def init(self):
        cr = self.env.cr
        cr.execute('CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_runtime_state_singleton '
                   'ON %s ((true))' % STATE_TABLE)
        self._llm_ensure_row(cr)

    @staticmethod
    def _llm_ensure_row(cr):
        cr.execute(
            "INSERT INTO {t} (free_meter_last_log_id, free_meter_last_ts, "
            "external_credit_watermark_ts, guard_tripped, circuit_failures, dr_mode, "
            "balance_refresh_page, create_uid, create_date, write_uid, write_date) "
            "SELECT 0, 0, 0, false, 0, false, 1, %s, now() at time zone 'UTC', %s, "
            "now() at time zone 'UTC' WHERE NOT EXISTS (SELECT 1 FROM {t}) "
            "ON CONFLICT DO NOTHING".format(t=STATE_TABLE), (SUPERUSER_ID, SUPERUSER_ID))

    # ------------------------------------------------------------------ #
    #  守衛                                                                #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            raise AccessError(_('The AI models runtime state is maintained by the platform.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            raise AccessError(_('The AI models runtime state is maintained by the platform.'))
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('The AI models runtime state cannot be deleted.'))

    # ------------------------------------------------------------------ #
    #  讀寫（獨立 cursor）                                                  #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_read(self):
        """回傳單例目前的值（dict；獨立 cursor 讀，看得到別的交易剛 commit 的游標）。"""
        columns = ', '.join(('id',) + STATE_FIELDS)
        with deadline_cursor(self.env.registry.cursor, cursor_deadline(self.env.cr)) as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute('SELECT %s FROM %s ORDER BY id LIMIT 1' % (columns, STATE_TABLE))
            row = cr.dictfetchone()
            if row is None:
                self._llm_ensure_row(cr)
                cr.execute('SELECT %s FROM %s ORDER BY id LIMIT 1' % (columns, STATE_TABLE))
                row = cr.dictfetchone()
            return self._llm_status_guard(cr, row)

    @api.model
    def _llm_write(self, **values):
        """以獨立 cursor 改單例（只收 :data:`STATE_FIELDS`）並 commit。"""
        unknown = set(values) - set(STATE_FIELDS)
        if unknown or not values:
            raise ValueError('llm_runtime_state: unknown or no fields')
        columns = sorted(values)
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            self._llm_ensure_row(cr)
            cr.execute(
                "UPDATE %s SET %s, write_uid = %%s, write_date = now() at time zone 'UTC'"
                % (STATE_TABLE, ', '.join('%s = %%s' % column for column in columns)),
                [values[column] for column in columns] + [SUPERUSER_ID])

    def _llm_update(self, sql_set, params, where='TRUE', returning=None):
        """獨立 cursor、READ COMMITTED 的原子 UPDATE（circuit 與限速的「讀＋改」在同一句）。

        ``sql_set``／``where``／``returning`` 一律是本檔的字面 SQL 片段；值只經 ``params``。回傳
        ``RETURNING`` 的那一列（沒有符合 ``where`` 的列 ⇒ ``None``）。
        """
        from .llm_event import _begin
        with deadline_cursor(self.env.registry.cursor, cursor_deadline(self.env.cr)) as cr:
            _begin(self.env, cr, read_committed=True)
            self._llm_ensure_row(cr)
            cr.execute(
                'UPDATE %s SET %s WHERE %s%s' % (
                    STATE_TABLE, sql_set, where, ' RETURNING %s' % returning if returning else ''),
                params)
            return cr.fetchone() if returning else None

    # ------------------------------------------------------------------ #
    #  circuit breaker（DESIGN §3.6）                                     #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_circuit_state(self):
        """``('closed'|'open'|'half_open', retry_after 秒)``（獨立 cursor 讀）。"""
        row = self._llm_read()
        until = row.get('circuit_open_until')
        if not until:
            return 'closed', None
        now = fields.Datetime.now()
        if now < until:
            return 'open', max(1, int((until - now).total_seconds()) + 1)
        return 'half_open', nac.CIRCUIT_OPEN_SECONDS

    #: 「剛剛從關著變成開著」：old 是 UPDATE 之前的值（同一句 UPDATE 先 FOR UPDATE 讀到它）。
    _CIRCUIT_WAS_CLOSED = '(old.until IS NULL OR old.until <= %(now)s)'

    @api.model
    def _llm_circuit_record(self, effect, code):
        """一次呼叫的結果記進 circuit（``newapi_client.circuit_effect`` 的回傳）；剛打開時告警。

        回傳「這一次把 circuit 打開了」。失敗與 trip 都是一句 UPDATE：``WITH old AS (… FOR UPDATE)``
        先鎖住並讀出舊值，再依舊值算新值與「是不是這一次打開的」——兩個 pod 同時失敗也只有一個看到
        「打開了」（只告警一次）。
        """
        now = fields.Datetime.now()
        params = {'now': now, 'open_until': now + timedelta(seconds=nac.CIRCUIT_OPEN_SECONDS),
                  'code': code, 'th': nac.CIRCUIT_FAILURE_THRESHOLD}
        if effect == nac.CIRCUIT_SUCCESS:
            # 只把失敗數歸零；開著的 circuit 只有健康檢查能關（_llm_circuit_gate）。
            self._llm_update('circuit_failures = 0', (), where='circuit_failures > 0')
            return False
        if effect == nac.CIRCUIT_FAILURE:
            opens = '(old.failures + 1 >= %%(th)s AND %s)' % self._CIRCUIT_WAS_CLOSED
            sql_set = (
                'circuit_failures = old.failures + 1, '
                'circuit_open_until = CASE WHEN {o} THEN %(open_until)s ELSE t.circuit_open_until END, '
                'circuit_reason = CASE WHEN {o} THEN %(code)s ELSE t.circuit_reason END, '
                'circuit_opened_at = CASE WHEN {o} THEN %(now)s ELSE t.circuit_opened_at END'
            ).format(o=opens)
        elif effect == nac.CIRCUIT_TRIP:
            # 一次就開；已經開著就延長（例：AP 一直被拒）並換成最新的原因。
            opens = self._CIRCUIT_WAS_CLOSED
            sql_set = (
                'circuit_open_until = %(open_until)s, circuit_reason = %(code)s, '
                'circuit_opened_at = CASE WHEN {o} THEN %(now)s ELSE t.circuit_opened_at END, '
                'circuit_failures = GREATEST(t.circuit_failures, %(th)s)'
            ).format(o=opens)
        else:
            return False
        from .llm_event import _begin
        with deadline_cursor(self.env.registry.cursor, cursor_deadline(self.env.cr)) as cr:
            _begin(self.env, cr, read_committed=True)
            self._llm_ensure_row(cr)
            cr.execute(
                'WITH old AS (SELECT id, circuit_open_until AS until, circuit_failures AS failures '
                '  FROM {t} ORDER BY id LIMIT 1 FOR UPDATE) '
                'UPDATE {t} AS t SET {s} FROM old WHERE t.id = old.id RETURNING {o}'.format(
                    t=STATE_TABLE, s=sql_set, o=opens),
                params)
            row = cr.fetchone()
        opened = bool(row and row[0])
        if opened or effect == nac.CIRCUIT_TRIP:
            self._llm_trigger_probe(at=params['open_until'])
        if opened:
            _logger.warning('[LLM] circuit opened for %ss: %s', nac.CIRCUIT_OPEN_SECONDS, code)
            report_circuit_open(self.env, code)
        return opened

    @api.model
    def _llm_circuit_gate(self, probe):
        """外呼類 cron 的 tick 開頭（:func:`llm_common.run_llm_cron`）：``True``＝照常跑、``False``＝跳過這一輪。

        關著 ⇒ ``True``。開著、還沒到 ⇒ ``False``。到了（半開）⇒ 原子地搶「這一輪由我探」（把
        ``circuit_open_until`` 往後推 :data:`CIRCUIT_PROBE_LEASE_SECONDS`，別人在這段時間看到的是「開著」），
        搶到的呼叫 ``probe()``（回 ``newapi_client.Result``）：成功（Ok，或 new-api 明確的回答）就關上、
        失敗就再開 2 分鐘。``probe`` 本身丟例外＝失敗。
        """
        state, _retry = self._llm_circuit_state()
        if state == 'closed':
            return True
        if state == 'open':
            return False
        now = fields.Datetime.now()
        claimed = self._llm_update(
            'circuit_open_until = %s',
            (now + timedelta(seconds=CIRCUIT_PROBE_LEASE_SECONDS), now),
            where='circuit_open_until IS NOT NULL AND circuit_open_until <= %s',
            returning='id')
        if not claimed:
            return False
        token = _PROBING.set(True)
        try:
            result = probe()
        except Exception:  # noqa: BLE001 - a crashing probe is a failed probe
            _logger.error('[LLM] circuit health check crashed: %s',
                          llm_redact.redact(traceback.format_exc()))
            result = nac.Unknown('client_internal')
        finally:
            _PROBING.reset(token)
        effect, code = nac.circuit_effect('ap', result)
        if effect == nac.CIRCUIT_SUCCESS:
            self._llm_update('circuit_open_until = NULL, circuit_failures = 0, '
                             'circuit_reason = NULL, circuit_opened_at = NULL', ())
            _logger.info('[LLM] circuit closed: health check %s/%s', result.kind, result.label())
            return True
        reopen = fields.Datetime.now() + timedelta(seconds=nac.CIRCUIT_OPEN_SECONDS)
        self._llm_update('circuit_open_until = %s, circuit_reason = COALESCE(%s, circuit_reason)',
                         (reopen, code))
        self._llm_trigger_probe(at=reopen)
        _logger.warning('[LLM] circuit stays open: health check %s/%s', result.kind,
                        result.label())
        return False

    @api.model
    def _llm_trigger_probe(self, at=None):
        """排健康探測；開啟／再次開啟時排在到期時間，web 半開時立即叫醒。

        circuit 狀態已經獨立 commit，探測也要獨立 commit，不能被呼叫端之後的 rollback 取消。
        否則待刷新的 SWR 列不會再觸發 cron，只能等 5 分鐘保險排程。
        """
        from .llm_event import _env_on
        try:
            with deadline_cursor(self.env.registry.cursor, cursor_deadline(self.env.cr)) as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                env = _env_on(self.env.registry, cr)
                cron = env.ref(CIRCUIT_PROBE_CRON_XMLID, raise_if_not_found=False)
                if cron:
                    trigger_cron(cron, at=at)
        except Exception:  # noqa: BLE001 - never break the caller
            _logger.warning('[LLM] could not trigger the circuit health check')

    # ------------------------------------------------------------------ #
    #  AP 限速（DESIGN §1.5）                                              #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_ap_rate_acquire(self, priority, now=None):
        """這一分鐘還能用 AP 嗎（全平台 60 次、依優先序各有上限）：可以 ⇒ ``None``；不行 ⇒ 等幾秒。

        一句原子 UPDATE：換了一分鐘就從 1 重新算；同一分鐘內 ``ap_rate_count`` 小於該優先序的上限
        才加 1。READ COMMITTED：兩個 pod 同時搶，後到的那句等對方 commit 之後以新值重新判斷。
        """
        ceiling = nac.AP_PRIORITY_CEILINGS[priority]
        now = time.time() if now is None else now
        window = int(now) // 60
        row = self._llm_update(
            'ap_rate_count = CASE WHEN ap_rate_window = %s THEN ap_rate_count + 1 ELSE 1 END, '
            'ap_rate_window = %s',
            (window, window, window, ceiling),
            where='ap_rate_window IS DISTINCT FROM %s OR ap_rate_count < %s',
            returning='ap_rate_count')
        if row:
            return None
        return max(1, 60 - int(now) % 60)

    @api.model
    def _llm_call_guard(self, role):
        return LlmCallGuard(self.env, role)

    # ------------------------------------------------------------------ #
    #  後台：狀態表單與「測試連線」（system 才看得到；LLM-02）               #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_action_open(self):
        """選單的 server action：打開單例的表單。"""
        row = self._llm_read()
        return {
            'type': 'ir.actions.act_window',
            'name': _('AI Models Connection'),
            'res_model': STATE_MODEL,
            'res_id': row['id'],
            'view_mode': 'form',
            'target': 'current',
        }

    def action_llm_test_connection(self):
        """〔測試連線〕：排一次 cron（CRON 金鑰）跑三項檢查，結果寫回 ``connection_test_result``。

        三項：代理管理埠 ``:8081/readyz``（含 AP／OP 到期剩餘天數、Valkey、break-glass）、AP 健康檢查
        （``NewApiClient.ap_health``，會碰 DB）、匿名 ``/api/status`` 的不變量（DESIGN §4.4 I8）。結果只有
        狀態碼、天數與布林。按鈕只給 system（帳務群組看得到這張表、但不能觸發外呼）。
        """
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only administrators can test the AI models connection.'))
        if llm_unconfigured(self.env):
            return self._llm_notification(
                _('AI models are not configured'),
                _('Set the proxy URL and switch the feature on (or set a pilot or canary) first.'),
                'warning')
        self._llm_write(connection_test_requested_at=fields.Datetime.now())
        cron = self.env.ref(CONNECTION_TEST_CRON_XMLID, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        return self._llm_notification(
            _('Connection test queued'),
            _('A cron worker runs it with the CRON key within a few seconds. '
              'Reload this page to see the result.'),
            'info')

    @staticmethod
    def _llm_notification(title, message, kind):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'title': title, 'message': message, 'type': kind, 'sticky': False},
        }

    @api.model
    def _cron_llm_connection_test(self):
        """〔測試連線〕的執行者；未設定（N27）時第一行就 return、零外呼。

        ⚠ **凍結契約**（同 ``llm_account._cron_llm_provision``）：方法名與所在的 model 不能改
        （``llm_common.LLM_CRONS``）。不過 circuit 的閘門（``outbound=False``）：circuit 開著的時候正是
        最需要診斷的時候，檢查本身也不受 circuit 擋（同健康檢查）。
        """
        return run_llm_cron(self.env, CONNECTION_TEST_CRON_XMLID, self._llm_connection_test_tick,
                            items=1, config=llm_config(self.env), outbound=False)

    @api.model
    def _llm_connection_test_tick(self, config, tick):
        row = self._llm_read()
        if not row['connection_test_requested_at']:
            return False
        self._llm_write(connection_test_requested_at=None)
        circuit, _retry = self._llm_circuit_state()
        result = {'circuit': circuit, 'circuit_reason': row['circuit_reason'] or None,
                  'readyz': nac.fetch_readyz(config['proxy_url'])}
        try:
            client = self.env['woow_paas_platform.llm_account'].sudo()._llm_client(nac.ROLE_CRON)
        except nac.NewApiError as exc:
            result['client'] = exc.code
        else:
            token = _PROBING.set(True)
            try:
                health = client.ap_health(config['prefix'], deadline=tick.deadline)
                status = client.status(deadline=tick.deadline)
            finally:
                _PROBING.reset(token)
            result['ap_health'] = '%s:%s' % (health.kind, health.label())
            result['status'] = '%s:%s' % (status.kind, status.label())
            if status.ok:
                invariants = nac.status_invariants(status.data)
                result['status_invariants'] = invariants
                result['status_invariants_ok'] = all(invariants.values())
        self._llm_write(connection_test_at=fields.Datetime.now(),
                        connection_test_result=json.dumps(result, sort_keys=True, indent=1))
        _logger.info('[LLM] connection test: readyz=%s ap_health=%s status=%s',
                     result['readyz'].get('ready'), result.get('ap_health'), result.get('status'))
        return False


class LlmCallGuard(nac.CallGuard):
    """``newapi_client.CallGuard`` 的 DB 實作（見模組 docstring）。"""

    def __init__(self, env, role):
        self.env = env
        self.role = role

    def _state(self):
        return self.env[STATE_MODEL].sudo()

    def before(self, identity, priority):
        probe = _STATUS_PROBE.get()
        if probe is not None:
            if probe['guard'] is self and identity == 'anon' and probe['remaining'] == 1:
                probe['remaining'] = 0
                return None
            return nac.NotExecuted('guard_unavailable')
        state = self._state()
        if identity != 'bg' and not _PROBING.get():
            circuit, retry_after = state._llm_circuit_state()
            if circuit != 'closed':
                if circuit == 'half_open' and self.role == nac.ROLE_WEB:
                    state._llm_trigger_probe()
                return nac.NotExecuted('circuit_open', retry_after=retry_after)
        if identity == 'ap':
            retry_after = state._llm_ap_rate_acquire(priority)
            if retry_after is not None:
                return nac.NotExecuted('ap_rate_limited', retry_after=retry_after)
        return None

    def after(self, identity, result):
        probe = _STATUS_PROBE.get()
        if probe is not None and probe['guard'] is self and identity == 'anon':
            # /api/status proves invariants, not DB health. Its success or
            # failure must not change circuit bookkeeping. before() still
            # admits only one anon send; all other admissions remain refused.
            return
        if identity == 'bg':
            report_break_glass_used(self.env, result)
            return
        effect, code = nac.circuit_effect(identity, result)
        if effect is not None:
            try:
                self._state()._llm_circuit_record(effect, code)
            except psycopg2.Error as exc:
                _logger.warning('[LLM] circuit bookkeeping failed (pgcode=%s)',
                                getattr(exc, 'pgcode', None))
