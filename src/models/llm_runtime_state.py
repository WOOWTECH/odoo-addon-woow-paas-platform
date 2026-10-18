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
"""
import logging

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError

from .billing_incident import _is_odoo_module_uninstall
from .llm_common import LlmQuotaInteger, _is_llm_internal, _without_llm_internal

_logger = logging.getLogger(__name__)

STATE_MODEL = 'woow_paas_platform.llm_runtime_state'
STATE_TABLE = 'woow_paas_platform_llm_runtime_state'
_LOCK_TIMEOUT = '5s'

#: 可以經 :meth:`LlmRuntimeState._llm_write` 改的欄位（＝本 model 的業務欄位）。
STATE_FIELDS = (
    # 免費試用計數的游標（LLM-10；DESIGN §1.3.3）。new-api 的 log id 與 unix 秒都用 int8。
    'free_meter_last_log_id',
    'free_meter_last_ts',
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
)


class LlmRuntimeState(models.Model):
    _name = 'woow_paas_platform.llm_runtime_state'
    _description = 'AI Models Runtime State (singleton)'

    free_meter_last_log_id = LlmQuotaInteger(readonly=True, default=0)
    free_meter_last_ts = LlmQuotaInteger(readonly=True, default=0,
                                         help='Unix seconds of the last free-usage log read.')
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
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute('SELECT %s FROM %s ORDER BY id LIMIT 1' % (columns, STATE_TABLE))
            row = cr.dictfetchone()
            if row is None:
                self._llm_ensure_row(cr)
                cr.execute('SELECT %s FROM %s ORDER BY id LIMIT 1' % (columns, STATE_TABLE))
                row = cr.dictfetchone()
        return row

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
