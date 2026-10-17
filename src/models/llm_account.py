"""AI 模型帳戶（DESIGN §2.2、§1.3.1、§2.9、§2.10；票 LLM-01b）。

每個 PaaS 帳號（``res.partner``，與點數錢包同層，D1）最多一個未關閉的 AI 帳戶，對應
new-api 上一個使用者 ``<prefix>%010d``。本檔有三件事：

1. **model 與守衛**：所有群組 ACL 都只有讀（``ir.model.access.csv``）；create／write 另要
   ``llm_common._llm_internal`` 的把手（sudo 也擋）；``newapi_username``、``partner_id``、
   ``newapi_user_id`` 設定後不能改；unlink 只在模組卸載時允許。
2. **開通**（``_llm_activate``）：同意條款 → 建 ``state=provisioning`` 的列 → ``_trigger()``
   開戶 cron。冪等：已有未關閉的帳戶就回它。條款版本由 ``llm_terms`` 的 published 版提供
   （LLM-04）：沒帶或不是目前的版本 ⇒ ``llm_tos_required``。改版後的重新同意是
   ``_llm_accept_terms``；建金鑰、轉換、啟用金鑰前呼叫 ``_llm_require_current_terms``（gate）。
   每一次同意都寫一筆 ``llm_event``（``tos_accept``：版本＝對象 ``llm_terms``、時間、uid、
   controller 取得的真實 IP），等這個交易 commit 之後才寫。
3. **開戶 cron**（``_cron_llm_provision``，P0–P9，〔PROV〕§3.2）：

   * 第一行就檢查「未設定」（N27）：未設定 ⇒ 直接 return，不外呼。
   * **一個 tick 只做一戶**、總 deadline 60 秒（N28）；還有待開戶的就 ``_trigger()``。
   * **鎖與 cursor**（〔REV〕25）：主 cursor 不對 ``llm_account`` 下列鎖。搶戶用獨立 cursor
     一次完成並 commit（``UPDATE … WHERE id=(SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)
     RETURNING``，同時寫 lease 與嘗試次數）；之後每一步要存的東西（密碼密文、
     ``newapi_user_id``、UP 密文、狀態）都以獨立 cursor 帶守衛寫回（``WHERE id=%s AND
     provision_lease_token=%s``），rowcount ≠ 1 就放棄本 tick。每個獨立交易都設
     ``lock_timeout``：萬一哪天有人在主 cursor 鎖了同一列，結果是那一步失敗、不是卡死。
   * 中途當掉重跑會收斂：P0 後 → 從 P1；P1 回應遺失 → P1 撞名 → P2 找到 → P4 用存好的
     密碼登入、確認 id 相同才存；P2 後 → 跳過 P1／P2；P7 回應遺失 → 重做 P4–P7（多一把
     拿不到明文的孤兒 PAT，清理在 LLM-03）；P7 存好、P9 前當掉 → 下次只跑 P9。
   * 退避 1、2、5、15、60 分鐘；第 6 次失敗或不可重試的錯誤 ⇒ ``state=error``。
   * 帶密文的寫回（P0 密碼、P7 UP）一律 ``log_exceptions=False``：``odoo.sql_db`` 預設會把
     失敗的 SQL 連同 bound params 寫成 ERROR 的 ``bad query``——等於把 Fernet 密文抄進 pod
     log（DESIGN §5.5 沒列這個管道）。失敗時只記欄位名與 pgcode。
   * 放棄（``state=error``）⇒ 告警 ``provision_failed``（LLM-02b 的出口；錯誤碼＝``last_error_code``）；
     密文被拒（綁定不符、解不開、內容不合）⇒ incident ``llm_secret_mismatch``＋告警 ``secret_rejected``。
4. **餘額刷新 cron**（``_cron_llm_balance_refresh``，DESIGN §1.3.6；LLM-02b）：每 10 分鐘以 AP 分頁讀
   new-api 的全部使用者（``GET /api/user/``、id 由小到大、每頁 100），只看符合本環境前綴
   ``<prefix>`` 加 10 位數字的，以獨立 cursor 每頁一次寫回 ``quota_cached``／``used_quota_cached``／
   ``request_count_cached``／``balance_synced_at``（只寫 active／suspended、``newapi_user_id`` 對得上、
   而且 ``balance_synced_at`` 比這次讀取還舊的列；時間戳是微秒精度的 :func:`balance_stamp`）。每 tick
   ≤ 30 秒、最多 :data:`BALANCE_PAGES_PER_TICK` 頁，讀到哪一頁記在 ``llm_runtime_state.balance_refresh_page``，
   剩下的 :data:`BALANCE_TICK_SPACING_SECONDS` 秒後 ``_trigger()`` 接著跑（N28；分攤節奏見該常數）。
   低額度提醒在 LLM-11、透支（I5）的動作在 LLM-12b。
"""
import ipaddress
import logging
import re
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2 import IntegrityError

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError

from ..services import llm_crypto, llm_redact
from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    CONFIG_CRYPTO_UNAVAILABLE,
    CONFIG_CRYPTO_UNAVAILABLE_WEB,
    CONFIG_IDENTITY_MISMATCH,
    CONFIG_PROXY_AUTH_UNAVAILABLE,
    CONFIG_PROXY_AUTH_UNAVAILABLE_WEB,
    ERR_LLM_ACCOUNT_ERROR,
    ERR_LLM_CLOSED,
    ERR_LLM_NOT_ACTIVATED,
    ERR_LLM_PROVISIONING,
    ERR_LLM_SUSPENDED,
    ERR_LLM_TOS_OUTDATED,
    ERR_LLM_TOS_REQUIRED,
    ERR_LLM_TOS_UNAVAILABLE,
    ERR_LLM_UNAVAILABLE,
    FREE_DAILY_LIMIT,
    CONVERT_MAX_POINTS,
    CONVERT_MIN_POINTS,
    MAX_KEYS_PER_ACCOUNT,
    PROVISION_BACKOFF_MINUTES,
    PROVISION_LEASE_MINUTES,
    PROVISION_MAX_ATTEMPTS,
    QUOTA_PER_POINT,
    LlmError,
    LlmQuotaInteger,
    _is_llm_internal,
    _llm_internal,
    _without_llm_internal,
    llm_config,
    report_config_unavailable,
    run_llm_cron,
)
from .llm_event import LlmAlert, LlmIncident, _begin, safe_code
from .llm_terms import TERMS_MODEL, clean_version

_logger = logging.getLogger(__name__)

ACCOUNT_MODEL = 'woow_paas_platform.llm_account'
ACCOUNT_TABLE = 'woow_paas_platform_llm_account'
EVENT_MODEL = 'woow_paas_platform.llm_event'
PROVISION_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_provision'
BALANCE_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_balance_refresh'
#: 餘額刷新：每頁幾個使用者（new-api 上限 100）、一個 tick 最多幾頁（N28）。
BALANCE_PAGE_SIZE = 100
BALANCE_PAGES_PER_TICK = 10
#: 還有頁要讀時，下一個 tick 至少隔這麼久（審查 LLM-02b finding 8）。AP 的 GA 桶是全平台共用的
#: 198.18.0.1（360 次／180 秒；stg 還是所有沙盒共用），轉換 worker 也走它：DESIGN §1.5 的預算是
#: 「1 萬帳號約 10 次／分」——每分鐘最多 10 頁，100 頁（1 萬帳號）約 10 分鐘＝本 cron 的排程間隔。
#: ⚠ 依賴：LLM-02 的 client 端 AP 限速器（60 次／分）要把餘額刷新列為**最低**優先序（轉換 worker >
#: 免費計數 > 其他 > 餘額刷新）；這裡的節奏只是讓刷新本身不衝高。
BALANCE_TICK_SPACING_SECONDS = 60
#: 會寫回餘額快取的帳戶狀態（provisioning 由 P9 寫第一次；error／closed 不顯示餘額）。
BALANCE_STATES = ('active', 'suspended')
_INT8_MAX = 2 ** 63 - 1
#: 獨立 cursor 等列鎖的上限：等不到就讓那一步失敗，不卡死整個 cron。
_LOCK_TIMEOUT = '5s'
#: 開戶時 P5 才看的孤兒 PAT 門檻（new-api 每人 20 把，含已過期 7 天內；〔PROV〕§3.2）。
_PAT_LIMIT_THRESHOLD = 18
#: 一經設定就不能再改的欄位（DR 模式的人工確認方法在 LLM-19）。
_IMMUTABLE_ONCE_SET = ('partner_id', 'newapi_username', 'newapi_user_id')
#: 帶守衛寫回的欄位名一律是程式裡的字面值；這個檢查只是讓拼字錯誤不會變成 SQL。
_COLUMN_RE = re.compile(r'[a-z][a-z0-9_]{0,62}')

STATE_SELECTION = [
    ('provisioning', 'Provisioning'),
    ('active', 'Active'),
    ('suspended', 'Suspended'),
    ('error', 'Error'),
    ('closed', 'Closed'),
]


class _ProvisionStop(Exception):
    """本 tick 的開戶停下來：``code`` 記進 ``last_error_code``（固定字串，不帶值）。"""

    def __init__(self, code, *, terminal=False, retry_after=None, backoff=True):
        super().__init__(code)
        self.code = code
        self.terminal = terminal
        self.retry_after = retry_after
        self.backoff = backoff


class _LeaseLost(Exception):
    """帶守衛的寫回影響 0 列：別的 worker 已接手（或帳戶狀態變了），放棄本 tick。"""


def _stop_for(result, prefix='llm_newapi'):
    """把非 Ok 的 client 結果轉成 ``_ProvisionStop``（DESIGN §3.4 的「主要錯誤」）。"""
    if isinstance(result, nac.NotExecuted):
        if result.reason == 'deadline':
            return _ProvisionStop('llm_tick_deadline', backoff=False)
        return _ProvisionStop('%s_not_executed' % prefix, retry_after=result.retry_after)
    if isinstance(result, nac.Unknown):
        return _ProvisionStop('%s_unknown' % prefix)
    if isinstance(result, nac.Definite):
        if result.proxy_error:
            return _ProvisionStop('llm_proxy_%s' % result.proxy_error,
                                  retry_after=result.retry_after)
        if result.code:
            # 封閉對照（N24）：已知的 new-api code ⇒ newapi_<小寫>，其他 ⇒ llm_newapi_other。
            return _ProvisionStop(nac.newapi_error_code(result.code),
                                  retry_after=result.retry_after)
        return _ProvisionStop('%s_rejected' % prefix)
    return _ProvisionStop('%s_unexpected' % prefix)


def _utcnow():
    return fields.Datetime.now()


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _clean_ip(real_ip):
    """controller 取得的對端位址 → 正規化的字串；空值或不合格 ⇒ ``False``（不存）。"""
    try:
        return str(ipaddress.ip_address(real_ip)) if real_ip else False
    except ValueError:
        return False


def _items(data):
    """new-api 的列表回應：``data.items`` 或直接是 list。"""
    if isinstance(data, dict):
        items = data.get('items')
        return items if isinstance(items, list) else []
    return data if isinstance(data, list) else []


def _config_code(exc):
    """cron 取 client／載入加密金鑰失敗 → 設定類告警的錯誤碼（``llm_common.CONFIG_ERROR_CODES``）。"""
    if isinstance(exc, llm_crypto.LlmCryptoError):
        return CONFIG_CRYPTO_UNAVAILABLE
    return CONFIG_PROXY_AUTH_UNAVAILABLE


def balance_stamp():
    """``balance_synced_at`` 的時間戳：naive UTC、**微秒**精度。

    每個寫 ``quota_cached``／``balance_synced_at`` 的地方（P9、餘額刷新、LLM-08 的轉換）都用它，
    刷新的寫回條件是嚴格的 ``balance_synced_at < read_at``：秒精度（``fields.Datetime.now()``）下
    「刷新在 S 秒記下讀取時間、同一秒內轉換入帳寫 S、刷新再以 S <= S 用入帳前的舊值蓋掉」會成立
    （審查 LLM-02b finding 6）。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LlmAccount(models.Model):
    _name = 'woow_paas_platform.llm_account'
    _description = 'AI Models Account'
    _order = 'id desc'
    _rec_name = 'newapi_username'

    partner_id = fields.Many2one(
        'res.partner', required=True, ondelete='restrict', index=True, readonly=True,
        help='The PaaS account (D1). One non-closed AI account per partner.')
    state = fields.Selection(
        STATE_SELECTION, required=True, default='provisioning', readonly=True, index=True)
    newapi_username = fields.Char(
        readonly=True, copy=False, index=True,
        help='<prefix>%010d, computed at creation; never changes.')
    newapi_user_id = fields.Integer(
        readonly=True, copy=False,
        help='new-api user id; stored-id guard: never changes once set (DR in LLM-19).')
    newapi_password_enc = fields.Text(
        groups='base.group_system', readonly=True, copy=False,
        help='MultiFernet ciphertext bound to this row and field (DESIGN §2.9).')
    up_token_enc = fields.Text(groups='base.group_system', readonly=True, copy=False)
    up_token_newapi_id = fields.Integer(readonly=True, copy=False)
    up_token_hint = fields.Char(readonly=True, copy=False)
    up_created_at = fields.Datetime(readonly=True, copy=False)
    up_expires_at = fields.Datetime(readonly=True, copy=False)
    up_state = fields.Selection(
        [('valid', 'Valid'), ('invalid', 'Invalid'), ('resigning', 'Re-signing')],
        readonly=True, copy=False)
    provision_lease_until = fields.Datetime(readonly=True, copy=False)
    provision_lease_token = fields.Char(readonly=True, copy=False)
    provision_attempts = fields.Integer(readonly=True, default=0, copy=False)
    next_attempt_at = fields.Datetime(readonly=True, copy=False)
    last_error_code = fields.Char(readonly=True, copy=False)
    tos_version = fields.Char(readonly=True, copy=False)
    tos_accepted_at = fields.Datetime(readonly=True, copy=False)
    tos_accepted_uid = fields.Many2one('res.users', readonly=True, copy=False,
                                       ondelete='set null')
    tos_accepted_ip = fields.Char(readonly=True, copy=False)
    quota_cached = LlmQuotaInteger(readonly=True, default=0, copy=False)
    used_quota_cached = LlmQuotaInteger(readonly=True, default=0, copy=False)
    request_count_cached = LlmQuotaInteger(readonly=True, default=0, copy=False)
    balance_synced_at = fields.Datetime(readonly=True, copy=False)
    low_balance_threshold = fields.Integer(readonly=True, default=50)
    suspend_reason = fields.Selection(
        [('ops', 'Operations'), ('account_inactive', 'Account inactive'),
         ('policy', 'Policy'), ('not_me', 'Reported by the account owner')],
        readonly=True, copy=False)
    api_key_ids = fields.One2many(
        'woow_paas_platform.llm_api_key', 'account_id', readonly=True)

    def init(self):
        # D1：一個 partner 同時只有一個未關閉的 AI 帳戶；關閉後可由維運重新開通（§2.10）。
        self.env.cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_account_partner_open_uniq '
            "ON %s (partner_id) WHERE state <> 'closed'" % ACCOUNT_TABLE)

    # ------------------------------------------------------------------ #
    #  守衛                                                                #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            raise AccessError(_('AI accounts can only be created by the platform.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            raise AccessError(_('AI accounts can only be changed by the platform.'))
        for name in _IMMUTABLE_ONCE_SET:
            if name not in vals:
                continue
            for record in self:
                current = record[name]
                current = current.id if hasattr(current, 'id') else current
                if current and vals[name] != current:
                    raise AccessError(_('This AI account field cannot be changed.'))
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('AI accounts cannot be deleted.'))

    # ------------------------------------------------------------------ #
    #  共用 helper                                                          #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_client(self, role):
        """唯一的 client 取得點（測試在這裡換成 mock）。"""
        return nac.get_newapi_client(self.env, role)

    @api.model
    def _llm_web_client(self, what):
        """HTTP 路徑（web pod）的 client；簽章金鑰缺或不合 ⇒ 告警＋``llm_unavailable``。

        prod 的 web 與 cron 是兩個 pod、env 各自獨立（DESIGN §2.11）：``WOOW_NEWAPI_PROXY_AUTH_WEB`` 只在
        web pod，cron 的 tick 驗不到它。所以 fail closed 的同時在這裡報 ``config_unavailable``／
        ``llm_proxy_auth_unavailable_web``（30 分鐘去重、不外呼；N27 未設定時不發）。``what`` 只進 log。
        """
        try:
            return self._llm_client(nac.ROLE_WEB)
        except nac.NewApiError as exc:
            _logger.error('[LLM] %s: client unavailable: %s', what, exc.code)
            report_config_unavailable(self.env, CONFIG_PROXY_AUTH_UNAVAILABLE_WEB)
            raise LlmError(ERR_LLM_UNAVAILABLE) from None

    @api.model
    def _llm_account_for_partner(self, partner):
        """partner 的未關閉 AI 帳戶（sudo；呼叫端傳入的 partner 一律是 request.env.user 的）。"""
        if not partner:
            return self.sudo().browse()
        return self.sudo().search(
            [('partner_id', '=', partner.id), ('state', '!=', 'closed')], limit=1)

    def _llm_require_active(self):
        """非 active 的帳戶 ⇒ 對應的 ``LlmError``。"""
        if not self:
            raise LlmError(ERR_LLM_NOT_ACTIVATED)
        self.ensure_one()
        if self.state == 'active':
            return
        raise LlmError({
            'provisioning': ERR_LLM_PROVISIONING,
            'suspended': ERR_LLM_SUSPENDED,
            'closed': ERR_LLM_CLOSED,
            'error': ERR_LLM_ACCOUNT_ERROR,
        }.get(self.state, ERR_LLM_UNAVAILABLE))

    def _llm_require_listable(self):
        """唯讀清單：active 與 suspended 都能看（暫停中的帳戶仍要看得到自己的金鑰）。"""
        if self and self.state == 'suspended':
            return
        self._llm_require_active()

    def _llm_tenant_ip(self, config=None):
        self.ensure_one()
        config = config or llm_config(self.env)
        if not config['identity_ok']:
            raise LlmError(ERR_LLM_UNAVAILABLE)
        try:
            return nac.tenant_ip(self.id, config['block'])
        except nac.NewApiError:
            raise LlmError(ERR_LLM_UNAVAILABLE) from None

    def _llm_up_token(self, role=nac.ROLE_WEB):
        """解出本帳戶的 UP。**只**從帳戶取（DESIGN §3.1：絕不從資源記錄反推）。

        ``role``：呼叫端在哪個 pod（現在只有 HTTP 路徑＝web）。加密金鑰沒設或不合
        （``LlmCryptoUnavailable``）是那個 pod 的設定問題：報 ``config_unavailable``（web ⇒
        ``llm_crypto_unavailable_web``）——cron 的 tick 只驗得到 cron pod 自己的 env。
        """
        self.ensure_one()
        record = self.sudo()
        if record.up_state == 'invalid' or not record.up_token_enc:
            raise LlmError(ERR_LLM_UNAVAILABLE)
        try:
            return llm_crypto.decrypt(record.up_token_enc, record.id, record.newapi_username,
                                      llm_crypto.FIELD_UP_TOKEN)
        except llm_crypto.LlmCryptoUnavailable as exc:
            _logger.error('[LLM] account=%s UP unusable: %s', record.id, exc.code)
            report_config_unavailable(self.env, CONFIG_CRYPTO_UNAVAILABLE_WEB
                                      if role == nac.ROLE_WEB else CONFIG_CRYPTO_UNAVAILABLE)
            raise LlmError(ERR_LLM_UNAVAILABLE) from None
        except llm_crypto.LlmCryptoError as exc:
            _logger.error('[LLM] account=%s UP unusable: %s', record.id, exc.code)
            self._llm_report_secret_rejected(record.id, exc)
            raise LlmError(ERR_LLM_UNAVAILABLE) from None

    @api.model
    def _llm_report_secret_rejected(self, account_id, exc):
        """密文被拒 ⇒ 告警 ``secret_rejected``＋incident ``llm_secret_mismatch``（DESIGN §2.9）。

        不在這裡報的兩種：sentinel（neutralize 的預期結果）與加密金鑰沒設或不合
        （``LlmCryptoUnavailable``）——後者是**那個 pod** 的設定問題，報的是 ``config_unavailable``：
        cron pod 由每個 tick 的 ``llm_crypto.load()``（:meth:`_llm_provision_tick`）、web pod 由
        :meth:`_llm_up_token`。兩種告警都受全平台寄信預算限制（``llm_event`` 模組 docstring 第 3 點）：
        金鑰被「換掉」時每戶都是 ``llm_secret_undecryptable``，也只會是每 30 分鐘 1–2 封。
        先告警、incident 另外包：incident 那邊任何失敗都不能把告警吃掉。盡力而為、不往外丟。
        """
        if isinstance(exc, (llm_crypto.LlmSecretNeutralized, llm_crypto.LlmCryptoUnavailable)):
            return
        code = safe_code(getattr(exc, 'code', None))
        Event = self.env[EVENT_MODEL]
        try:
            Event._llm_alert(LlmAlert.SECRET_REJECTED, account_id=account_id,
                             target_model=ACCOUNT_MODEL, target_id=account_id, error_code=code)
        except Exception:  # noqa: BLE001 - reporting must not change the fail-closed result
            _logger.error('[LLM] account=%s: secret rejection alert not raised: %s', account_id,
                          llm_redact.redact(traceback.format_exc()))
        try:
            Event._llm_incident(LlmIncident.SECRET_MISMATCH, account_id=account_id,
                                error_code=code)
        except Exception:  # noqa: BLE001 - same
            _logger.error('[LLM] account=%s: secret rejection incident not opened: %s',
                          account_id, llm_redact.redact(traceback.format_exc()))

    def _llm_mark_up_invalid(self):
        """UP 被 new-api 拒絕（401）⇒ ``up_state=invalid``，重簽在 LLM-03。獨立交易寫。

        盡力而為：寫不進去（例如 lock_timeout）只記 pgcode，不讓呼叫端的回應變成 500——
        下一次用到這把 UP 時 new-api 會再回一次 401。
        """
        self.ensure_one()
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                cr.execute(
                    'UPDATE %s SET up_state = %%s, write_uid = %%s, write_date = %%s '
                    'WHERE id = %%s' % ACCOUNT_TABLE,
                    ('invalid', SUPERUSER_ID, _utcnow(), self.id), log_exceptions=False)
        except psycopg2.Error as exc:
            _logger.warning('[LLM] account=%s: could not mark the UP invalid (pgcode=%s)',
                            self.id, getattr(exc, 'pgcode', None))

    # ------------------------------------------------------------------ #
    #  開通（/api/llm/account/activate）                                    #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_activate(self, partner, tos_version, user, real_ip):
        """同意條款並建立 provisioning 帳戶；冪等。回傳帳戶（sudo、**不帶**內部寫入把手）。

        同一個 partner 兩個請求同時開通：輸的那筆的 INSERT 會等贏家 commit、然後撞 partial
        unique index。Odoo 的交易是 REPEATABLE READ，snapshot 在本請求的第一個查詢（session、
        gate）就建立了——在這個交易裡重新 search **看不到**贏家剛 commit 的那一列。所以輸的
        那筆回 ``llm_provisioning``：這是**預期結果**，前端照常輪詢 ``/api/llm/state``（下一個
        請求是新的交易、看得到那一列）。``TestLlmActivateRace`` 用兩條真連線驗。
        """
        current = self.env[TERMS_MODEL]._llm_current()
        if current is None:
            raise LlmError(ERR_LLM_TOS_UNAVAILABLE)
        # 沒帶、或帶的不是目前 published 的版本（舊版、亂填）：都是「還沒同意目前的條款」（LLM-04 AC1）。
        if clean_version(tos_version) != current.version:
            raise LlmError(ERR_LLM_TOS_REQUIRED, current_version=current.version)
        Account = self.sudo()
        existing = Account._llm_account_for_partner(partner)
        if existing:
            return existing
        config = llm_config(self.env)
        if not config['identity_ok']:
            raise LlmError(ERR_LLM_UNAVAILABLE)
        ip_text = _clean_ip(real_ip)
        try:
            with self.env.cr.savepoint():
                account = _llm_internal(Account).create({
                    'partner_id': partner.id,
                    'state': 'provisioning',
                    'tos_version': current.version,
                    'tos_accepted_at': _utcnow(),
                    'tos_accepted_uid': user.id if user else False,
                    'tos_accepted_ip': ip_text,
                })
                try:
                    username = nac.newapi_username(config['prefix'], account.id)
                    nac.tenant_ip(account.id, config['block'])
                except nac.NewApiError:
                    # 合成 IP 用完（prod 130,815 戶、stg 每 block 4,095 戶）：fail closed。
                    _logger.error('[LLM] account id %s does not fit the synthetic IP block',
                                  account.id)
                    raise LlmError(ERR_LLM_UNAVAILABLE) from None
                _llm_internal(account).write({'newapi_username': username})
        except IntegrityError:
            # 另一個請求先 commit 了同一個 partner 的帳戶（見 docstring：本交易的 snapshot
            # 看不到它，重新 search 也找不到）⇒ 預期結果 llm_provisioning。
            raise LlmError(ERR_LLM_PROVISIONING) from None
        # 帳戶是這個交易剛建的：稽核等 commit 之後才寫（llm_event 模組 docstring 第 1 點的前提一）。
        self.env[EVENT_MODEL]._llm_record_after_commit(
            'tos_accept', account_id=account.id, partner_id=partner.id,
            uid=user.id if user else None, real_ip=ip_text or None,
            target_model=TERMS_MODEL, target_id=current.id)
        cron = self.env.ref(PROVISION_CRON_XMLID, raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
        # 內部寫入把手只掛在上面落地的那幾次呼叫；回給 controller 的是乾淨的 sudo 記錄，
        # 從它走到的任何東西（api_key_ids…）都過不了兩個 LLM model 的 create／write 守衛。
        return Account.browse(account.id)

    # ------------------------------------------------------------------ #
    #  條款：改版後重新同意（/api/llm/account/accept_terms）與 gate（LLM-04）   #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_accept_terms(self, partner, tos_version, user, real_ip):
        """同意目前 published 的條款版本（DESIGN §6.4-3）。回 ``{'tos': …}``（同 ``/api/llm/state``）。

        * 沒有未關閉的帳戶 ⇒ ``llm_not_activated``（還沒開通的人走 ``activate``，它本身就記同意）。
        * 沒帶版本 ⇒ ``llm_tos_required``；帶的不是目前的版本（對話框開著的時候又改版了）⇒
          ``llm_tos_outdated``＋``current_version``，前端重新讀條款。
        * 已經同意的就是目前的版本 ⇒ 什麼都不寫（不是新的同意）。
        * 暫停中、開戶失敗的帳戶也可以同意（同意本身不會建立或恢復任何東西）。

        帳戶列以 ORM 寫（``FOR NO KEY UPDATE``）；兩個請求同時同意 ⇒ 後到的那筆在 REPEATABLE READ 下撞
        serialization failure，``retrying()`` 重播時看到已經同意、不再寫。稽核在 commit 之後才寫：交易回捲
        （或被重播）的那一次不會留下「同意了」的紀錄。
        """
        account = self._llm_account_for_partner(partner)
        if not account:
            raise LlmError(ERR_LLM_NOT_ACTIVATED)
        current = self.env[TERMS_MODEL]._llm_current()
        if current is None:
            raise LlmError(ERR_LLM_TOS_UNAVAILABLE)
        version = clean_version(tos_version)
        if version is None:
            raise LlmError(ERR_LLM_TOS_REQUIRED, current_version=current.version)
        if version != current.version:
            raise LlmError(ERR_LLM_TOS_OUTDATED, current_version=current.version)
        if account.tos_version != current.version:
            ip_text = _clean_ip(real_ip)
            _llm_internal(account).write({
                'tos_version': current.version,
                'tos_accepted_at': _utcnow(),
                'tos_accepted_uid': user.id if user else False,
                'tos_accepted_ip': ip_text,
            })
            self.env[EVENT_MODEL]._llm_record_after_commit(
                'tos_accept', account_id=account.id, partner_id=account.partner_id.id,
                uid=user.id if user else None, real_ip=ip_text or None,
                target_model=TERMS_MODEL, target_id=current.id)
        return {'tos': account._llm_tos_dto(current)}

    def _llm_require_current_terms(self):
        """gate（DESIGN §6.4-3）：條款改版且要求重新同意 ⇒ ``llm_tos_outdated``。

        **建金鑰、轉換、啟用金鑰都要在第一次外呼之前呼叫**（LLM-07 的付費金鑰與啟用、LLM-08 的轉換
        照用）；既有金鑰不受影響——這裡只擋 PaaS 端的寫入，不碰 new-api 上的 token。判準見
        ``llm_terms.LlmTerms._llm_reaccept_required``。
        """
        self.ensure_one()
        Terms = self.env[TERMS_MODEL]
        current = Terms._llm_current()
        if Terms._llm_reaccept_required(self.tos_version or None, current=current):
            raise LlmError(ERR_LLM_TOS_OUTDATED,
                           current_version=current.version if current else None)

    def _llm_tos_dto(self, current):
        """``/api/llm/state`` 的 ``account.tos``：``needs_reaccept``＝版本不同（顯示「條款已更新」橫幅）；
        ``blocking``＝gate 會擋（橫幅改說「同意前不能建立金鑰、轉換點數或重新啟用金鑰」）。
        ``requires_reaccept=False`` 的改版只有前者（LLM-05 審查：畫面與 gate 要一致）。"""
        self.ensure_one()
        accepted = self.tos_version or None
        return {
            'accepted_version': accepted,
            'current_version': current.version if current else None,
            'needs_reaccept': current is not None and accepted != current.version,
            'blocking': self.env[TERMS_MODEL]._llm_reaccept_required(accepted, current=current),
        }

    # ------------------------------------------------------------------ #
    #  /api/llm/state                                                      #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_state_payload(self, partner):
        current = self.env[TERMS_MODEL]._llm_current()
        account = self._llm_account_for_partner(partner)
        keys = account.api_key_ids.filtered(lambda k: k.state not in ('deleted', 'lost'))
        # 卡在 creating 太久的那把不算（見 llm_api_key._llm_is_stale_creating）：否則前端藏起
        # 〔建立免費試用金鑰〕，使用者永遠送不出會替它收尾的那一次 keys/create。
        free_key = keys.filtered(
            lambda k: k.kind == 'free_trial' and k.state in ('creating', 'active', 'disabled')
            and not k._llm_is_stale_creating())[:1]
        account_dto = None
        if account:
            quota = int(account.quota_cached or 0)
            balance_points = quota // QUOTA_PER_POINT if quota > 0 else 0
            overdraft_points = (-quota + QUOTA_PER_POINT - 1) // QUOTA_PER_POINT if quota < 0 else 0
            account_dto = {
                'state': account.state,
                # 整數 quota（DESIGN §6.6：換成點只在顯示時做）。balance_points／overdraft_points
                # 是捨去／進位後的整數點數，前端要顯示 2 位小數（12.99 點）就得用這個。
                'balance_quota': quota,
                'balance_points': balance_points,
                'overdraft_points': overdraft_points,
                'suspend_reason': account.suspend_reason or None,
                'low_balance_threshold': account.low_balance_threshold,
                'tos': account._llm_tos_dto(current),
            }
        return {
            'account': account_dto,
            # 目前 published 的條款（沒有＝null：「尚未開通」畫面不能開通，見前端 tosUnavailable）。
            'tos_current_version': current.version if current else None,
            # 草稿文字（D15）：「尚未開通」畫面在勾選框下標「草稿，尚未生效」。
            'tos_current_draft': bool(current and current.is_draft),
            'free_trial': {
                'key_id': free_key.id or None,
                # 每日計數在 LLM-10（llm_free_usage）；tracer 用 /api/llm/usage/recent。
                'today_count': None,
                'daily_limit': FREE_DAILY_LIMIT,
                'capped': False,
                'keys_created_today': None,
            },
            'limits': {
                'max_keys': MAX_KEYS_PER_ACCOUNT,
                'keys_used': len(keys),
                'convert_min': CONVERT_MIN_POINTS,
                'convert_max': CONVERT_MAX_POINTS,
            },
            'pending_review': False,
            'public_base_url': None,
            'guard': {'tripped': False},
            'data_as_of': _iso(account.balance_synced_at) if account else None,
        }

    # ------------------------------------------------------------------ #
    #  /api/llm/usage/recent（tracer；LLM-02 改 SWR、LLM-09／10 取代）       #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_usage_recent(self, partner):
        """今天的免費試用呼叫（前 10 筆）。

        ⚠ **刻意偏離 DESIGN §3.2／§3.5／N26**（記在 DESIGN 變更紀錄第 4 版）：DESIGN 的
        usage/recent 是 SWR、只讀 DB；tracer 期間這裡**同步外呼**（WEB 金鑰、UP、read 3 秒），
        每次頁面載入吃該租戶合成 IP 1 格 GA。收斂回 SWR（cron 以 CRON 金鑰刷新、端點只讀
        快取）在 LLM-02。
        """
        account = self._llm_account_for_partner(partner)
        account._llm_require_active()
        ip = account._llm_tenant_ip()
        up = account._llm_up_token()
        client = self._llm_web_client('usage/recent')
        now = datetime.now(timezone(timedelta(hours=8)))
        start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        result = client.user_log_self(ip, up, {
            'type': 2, 'group': 'free', 'start_timestamp': start, 'p': 1, 'page_size': 10,
        }, timeout=nac.TIMEOUT_INTERACTIVE_READ)
        if not result.ok:
            if isinstance(result, nac.Definite) and result.code in (
                    'AUTH_UNAUTHORIZED', 'ACCESS_TOKEN_EXPIRED'):
                account._llm_mark_up_invalid()
            raise LlmError(ERR_LLM_UNAVAILABLE)
        data = result.data if isinstance(result.data, dict) else {}
        names = {k.newapi_name: k for k in account.api_key_ids if k.newapi_name}
        items = []
        for row in _items(data)[:10]:
            if not isinstance(row, dict):
                continue
            key = names.get(row.get('token_name'))
            created = row.get('created_at')
            items.append({
                'time': (datetime.fromtimestamp(created, tz=timezone.utc).isoformat()
                         if type(created) is int else None),
                'model': row.get('model_name') if isinstance(row.get('model_name'), str) else None,
                'input_tokens': row.get('prompt_tokens') if type(row.get('prompt_tokens')) is int else None,
                'output_tokens': (row.get('completion_tokens')
                                  if type(row.get('completion_tokens')) is int else None),
                'duration_ms': row.get('use_time') * 1000 if type(row.get('use_time')) is int else None,
                'request_id': row.get('request_id') if isinstance(row.get('request_id'), str) else None,
                'key_id': key.id if key else None,
                'key_kind': key.kind if key else None,
            })
        total = data.get('total')
        return {
            'today_count': total if type(total) is int else len(items),
            'daily_limit': FREE_DAILY_LIMIT,
            'items': items,
            'data_as_of': datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------ #
    #  開戶 cron（P0–P9）                                                   #
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_llm_provision(self):
        """每 tick 開一戶；未設定（N27）時第一行就 return、零外呼。

        ⚠ **凍結契約**：``data/llm_cron.xml`` 是 ``noupdate="1"``，發版的 ``-u`` 不會再改已安裝
        DB 裡這支 cron 的 ``code``（``model._cron_llm_provision()``）與 ``model_id``。所以本方法
        的**名字與所在的 model** 不能改；要改名或搬 model，必須同時寫 migration 更新既有 DB 的
        ``ir_act_server.code``（否則 prod／stg 的 cron 會一直靜默呼叫不存在的方法，見 pitfall
        ``seed-noupdate-sticky-template-links``）。``TestLlmCronContract`` 斷言 DB 裡的 code。
        """
        # N28 的唯一例外：開戶一個 tick 只做一戶、總預算 60 秒（DESIGN §3.5）。
        run_llm_cron(self.env, PROVISION_CRON_XMLID, self._llm_provision_tick,
                     seconds=nac.PROVISION_TICK_SECONDS, items=1, config=llm_config(self.env))

    @api.model
    def _llm_provision_tick(self, config, tick):
        """一個開戶 tick（:func:`llm_common.run_llm_cron` 已排除「未設定」）。回傳「還有待開戶」。"""
        Account = self.sudo()
        if not config['identity_ok']:
            _logger.warning('[LLM] provisioning skipped: username prefix / IP block misconfigured')
            report_config_unavailable(self.env, CONFIG_IDENTITY_MISMATCH)
            return False
        try:
            client = Account._llm_client(nac.ROLE_CRON)
            llm_crypto.load()
        except (nac.NewApiError, llm_crypto.LlmCryptoError) as exc:
            # fail closed、不搶戶：帳戶留在 provisioning，不吃嘗試次數。訊息只有原因碼。每個 tick
            # 都檢查（不論有沒有待開戶）：prod 的 cron pod 少了 env，負責人下一個 tick 就收得到告警。
            _logger.error('[LLM] provisioning skipped: %s', exc.code)
            report_config_unavailable(self.env, _config_code(exc))
            return False
        deadline = tick.deadline
        tick.took()
        claim = Account._llm_claim_for_provision()
        if claim:
            account_id, token, attempts = claim
            stop = None
            try:
                Account._llm_provision_one(client, config, account_id, token, attempts, deadline)
            except _ProvisionStop as exc:
                stop = exc
                _logger.warning('[LLM] provisioning account=%s attempt=%s stopped: %s',
                                account_id, attempts, stop.code)
            except _LeaseLost:
                _logger.warning('[LLM] provisioning account=%s: lease lost, tick abandoned',
                                account_id)
            except Exception:  # noqa: BLE001 - one account must not wedge the cron
                _logger.error('[LLM] provisioning account=%s crashed: %s', account_id,
                              llm_redact.redact(traceback.format_exc()))
                stop = _ProvisionStop('llm_provision_internal')
            if stop is not None:
                Account._llm_provision_failed(account_id, token, attempts, stop)
        return Account._llm_provision_pending()

    @api.model
    def _llm_claim_for_provision(self, account_ids=None):
        """獨立 cursor 搶一戶並 commit：寫 lease、token、嘗試次數＋1。

        ``account_ids``：只在這些 id 裡搶（``None``＝全部）。生產的 cron 不傳；測試的 adapter
        用它把搶戶限縮到自己建的帳戶（照 ``tests/committed_cron_scope.py`` 的 ``isolated()``：
        保留真的 SQL、排序與鎖，只限縮候選），共用 DB 裡別人的 provisioning 帳戶不會被搶走。
        """
        token = uuid.uuid4().hex
        now = _utcnow()
        scope = ''
        params = {'until': now + timedelta(minutes=PROVISION_LEASE_MINUTES), 'token': token,
                  'uid': SUPERUSER_ID, 'now': now, 'state': 'provisioning'}
        if account_ids is not None:
            scope = 'AND id = ANY(%(ids)s) '
            params['ids'] = sorted(int(i) for i in account_ids)
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute(
                'UPDATE {t} SET provision_lease_until = %(until)s, '
                'provision_lease_token = %(token)s, '
                'provision_attempts = COALESCE(provision_attempts, 0) + 1, '
                'write_uid = %(uid)s, write_date = %(now)s '
                'WHERE id = (SELECT id FROM {t} WHERE state = %(state)s {scope}'
                'AND (next_attempt_at IS NULL OR next_attempt_at <= %(now)s) '
                'AND (provision_lease_until IS NULL OR provision_lease_until < %(now)s) '
                'ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1) '
                'RETURNING id, provision_attempts'.format(t=ACCOUNT_TABLE, scope=scope),
                params)
            row = cr.fetchone()
        if not row:
            return None
        return row[0], token, row[1]

    @api.model
    def _llm_provision_pending(self, account_ids=None):
        """還有可以立刻開戶的帳戶嗎（獨立 cursor：主 cursor 的 snapshot 看不到本 tick 的寫回）。

        ``account_ids`` 的語意同 :meth:`_llm_claim_for_provision`。
        """
        now = _utcnow()
        scope = ''
        params = [now, now]
        if account_ids is not None:
            scope = ' AND id = ANY(%s)'
            params.append(sorted(int(i) for i in account_ids))
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT 1 FROM %s WHERE state = %%s '
                'AND (next_attempt_at IS NULL OR next_attempt_at <= %%s) '
                'AND (provision_lease_until IS NULL OR provision_lease_until < %%s)%s LIMIT 1'
                % (ACCOUNT_TABLE, scope), ['provisioning'] + params)
            return cr.fetchone() is not None

    @api.model
    def _llm_read_row(self, account_id):
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT id, state, newapi_username, newapi_user_id, newapi_password_enc, '
                'up_token_enc, up_state, provision_lease_token FROM %s WHERE id = %%s'
                % ACCOUNT_TABLE, (account_id,))
            row = cr.dictfetchone()
        return row

    @api.model
    def _llm_guarded_update(self, account_id, token, values):
        """以獨立 cursor 帶守衛寫回；守衛不成立（lease 被接手、狀態變了）⇒ ``_LeaseLost``。

        DB 錯誤（最常見是故意設的 5 秒 ``lock_timeout``）⇒ ``_ProvisionStop('llm_db_write_failed')``
        （可重試）。這裡會綁密文（P0 密碼、P7 UP），所以 SQL 一律 ``log_exceptions=False``、
        例外 ``from None``：log 只有欄位名與 pgcode，不會有 ``bad query`` 帶出 bound params。
        """
        values = dict(values, write_uid=SUPERUSER_ID, write_date=_utcnow())
        columns = sorted(values)
        if not all(_COLUMN_RE.fullmatch(column) for column in columns):
            raise ValueError('bad column name')
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,), log_exceptions=False)
                cr.execute(
                    'UPDATE %s SET %s WHERE id = %%s AND provision_lease_token = %%s AND state = %%s'
                    % (ACCOUNT_TABLE, ', '.join('%s = %%s' % column for column in columns)),
                    [values[column] for column in columns] + [account_id, token, 'provisioning'],
                    log_exceptions=False)
                if cr.rowcount != 1:
                    raise _LeaseLost()
        except psycopg2.Error as exc:
            _logger.warning('[LLM] account=%s: guarded write of %s failed (pgcode=%s)',
                            account_id, ','.join(c for c in columns
                                                 if c not in ('write_uid', 'write_date')),
                            getattr(exc, 'pgcode', None))
            raise _ProvisionStop('llm_db_write_failed') from None

    @api.model
    def _llm_provision_failed(self, account_id, token, attempts, stop):
        now = _utcnow()
        terminal = stop.terminal or attempts >= PROVISION_MAX_ATTEMPTS
        values = {'last_error_code': stop.code[:64], 'provision_lease_until': None,
                  'provision_lease_token': None}
        next_at = None
        if terminal:
            values.update(state='error', next_attempt_at=None)
        else:
            if stop.backoff:
                index = min(max(attempts, 1), len(PROVISION_BACKOFF_MINUTES)) - 1
                next_at = now + timedelta(minutes=PROVISION_BACKOFF_MINUTES[index])
                if stop.retry_after:
                    next_at = max(next_at, now + timedelta(seconds=int(stop.retry_after)))
            values['next_attempt_at'] = next_at
        # 本方法**不往外拋**：記不下失敗（lease 已被接手、DB 寫不進去）時只留 log，lease 到期
        # 後下一個 tick 會重新搶到這一戶。
        try:
            self._llm_guarded_update(account_id, token, values)
        except _LeaseLost:
            return
        except _ProvisionStop as failed:
            _logger.warning('[LLM] provisioning account=%s: could not record %s (%s)',
                            account_id, stop.code, failed.code)
            return
        except Exception:  # noqa: BLE001 - see above
            _logger.error('[LLM] provisioning account=%s: could not record the failure: %s',
                          account_id, llm_redact.redact(traceback.format_exc()))
            return
        if terminal:
            _logger.error('[LLM] provisioning account=%s gave up: %s', account_id, stop.code)
            try:
                self.env[EVENT_MODEL]._llm_alert(
                    LlmAlert.PROVISION_FAILED, account_id=account_id,
                    target_model=ACCOUNT_MODEL, target_id=account_id,
                    error_code=safe_code(stop.code), numbers={'attempts': int(attempts)})
            except Exception:  # noqa: BLE001 - the failure itself is already recorded
                _logger.error('[LLM] provisioning account=%s: alert not raised: %s', account_id,
                              llm_redact.redact(traceback.format_exc()))
        cron = self.env.ref(PROVISION_CRON_XMLID, raise_if_not_found=False)
        if cron and not terminal:
            if next_at:
                cron._trigger(at=next_at)
            else:
                cron._trigger()

    @api.model
    def _llm_provision_one(self, client, config, account_id, token, attempts, deadline):
        """對一戶跑 P0–P9（DESIGN §1.3.1）。失敗以 ``_ProvisionStop`` 表示。"""
        row = self._llm_read_row(account_id)
        if not row or row['state'] != 'provisioning' or row['provision_lease_token'] != token:
            raise _LeaseLost()
        username = row['newapi_username'] or ''
        prefix = config['prefix']
        if not (nac.USERNAME_RE.fullmatch(username) and username.startswith(prefix)
                and len(username) == len(prefix) + 10):
            raise _ProvisionStop('llm_identity_mismatch', terminal=True)
        try:
            ip = nac.tenant_ip(account_id, config['block'])
        except nac.NewApiError:
            raise _ProvisionStop('llm_synthetic_ip_exhausted', terminal=True) from None

        password = self._llm_p0_password(row, token)
        user_id = row['newapi_user_id'] or 0

        # 已存好 UP（P7 之後、P9 之前當掉）⇒ 只跑 P9。
        if row['up_token_enc'] and row['up_state'] != 'invalid':
            up = self._llm_decrypt(row, llm_crypto.FIELD_UP_TOKEN, row['up_token_enc'])
            me = client.user_self(ip, up, timeout=nac.TIMEOUT_PROVISION, deadline=deadline)
            if me.ok or not (isinstance(me, nac.Definite) and me.code in (
                    'AUTH_UNAUTHORIZED', 'ACCESS_TOKEN_EXPIRED')):
                return self._llm_p9_finish(account_id, token, user_id, me)
            self._llm_guarded_update(account_id, token, {'up_state': 'invalid'})

        pending_user_id = None
        if not user_id:
            user_id, pending_user_id = self._llm_p1_p2(
                client, account_id, token, username, password, deadline)

        # P4：以消費者密碼登入（租戶合成 IP）。
        login = client.user_login(ip, username, password, deadline=deadline)
        if not login.ok:
            if isinstance(login, nac.Definite) and login.message_key == nac.MSG_LOGIN_FAILED:
                # 撞名而登入失敗 ⇒ 不是我們上次建的；自己的帳號登入失敗 ⇒ 密碼漂移。
                raise _ProvisionStop('llm_newapi_name_taken' if pending_user_id
                                     else 'llm_newapi_login_failed', terminal=True)
            raise _stop_for(login)
        data = login.data if isinstance(login.data, dict) else {}
        jwt = data.get('access_token')
        session = data.get('session') if isinstance(data.get('session'), dict) else {}
        sid = session.get('sid')
        if not (isinstance(jwt, str) and nac.JWT_RE.fullmatch(jwt)):
            jwt = None
        # 從這裡開始 new-api 可能已經簽發了一個 session：之後**任何**一步失敗（含守衛寫回丟
        # _LeaseLost／_ProvisionStop），finally 都會撤掉它（P8）。不撤的話它活 30 天、佔 new-api
        # 每人 50 個同時 session 的名額，而撞名收斂的路徑每次重試都會再多一個。
        try:
            if data.get('require_verification'):
                raise _ProvisionStop('llm_newapi_2fa_enabled', terminal=True)
            if jwt is None:
                raise _ProvisionStop('llm_newapi_contract_login', terminal=True)
            login_user = data.get('user') if isinstance(data.get('user'), dict) else {}
            expected = user_id or pending_user_id
            if login_user.get('id') != expected:
                raise _ProvisionStop('llm_newapi_user_mismatch', terminal=True)
            if pending_user_id:
                self._llm_guarded_update(account_id, token, {'newapi_user_id': pending_user_id})
                user_id = pending_user_id
            up = self._llm_p5_to_p7(client, account_id, token, username, ip, jwt, password,
                                    attempts, deadline)
        finally:
            # P8：撤掉這次的 session；失敗不擋開戶（30 天後自然過期）。
            if jwt is not None and isinstance(sid, str) and nac.SID_RE.fullmatch(sid):
                try:
                    # 清理步驟另有一小段寬限：tick 的預算用完（例如前一步就是逾時）也要試著撤。
                    ended = client.user_delete_session(ip, jwt, sid, deadline=max(
                        deadline, time.monotonic() + nac.SESSION_REVOKE_GRACE_SECONDS))
                except Exception:  # noqa: BLE001 - must not mask the error being unwound
                    ended = nac.Unknown('client_internal')
                if not ended.ok and not (isinstance(ended, nac.Definite)
                                         and ended.code == 'AUTH_SESSION_NOT_FOUND'):
                    _logger.warning('[LLM] provisioning account=%s: session revoke %s',
                                    account_id, ended.label())

        me = client.user_self(ip, up, timeout=nac.TIMEOUT_PROVISION, deadline=deadline)
        return self._llm_p9_finish(account_id, token, user_id, me)

    def _llm_decrypt(self, row, field, ciphertext):
        try:
            return llm_crypto.decrypt(ciphertext, row['id'], row['newapi_username'], field)
        except llm_crypto.LlmSecretNeutralized:
            raise _ProvisionStop('llm_secret_neutralized', terminal=True) from None
        except llm_crypto.LlmCryptoUnavailable:
            raise _ProvisionStop('llm_crypto_unavailable') from None
        except llm_crypto.LlmCryptoError as exc:
            # 綁定不符、解不開或內容不合：有人動過密文，或加密金鑰換掉了（DESIGN §2.9）。
            _logger.critical('[LLM] account=%s %s ciphertext rejected: %s',
                             row['id'], field, exc.code)
            self._llm_report_secret_rejected(row['id'], exc)
            raise _ProvisionStop(exc.code, terminal=True) from None

    @api.model
    def _llm_p0_password(self, row, token):
        """P0：沒有密碼才產生（只限 provisioning＋沒有 newapi_user_id），先加密存好再外呼。"""
        ciphertext = row['newapi_password_enc']
        if ciphertext:
            return self._llm_decrypt(row, llm_crypto.FIELD_PASSWORD, ciphertext)
        if row['newapi_user_id']:
            # 已有 new-api 使用者卻沒有密碼：不能重產（那會變成另一組帳密）。
            raise _ProvisionStop('llm_password_missing', terminal=True)
        password = llm_crypto.generate_password()
        try:
            encrypted = llm_crypto.encrypt(row['id'], row['newapi_username'],
                                           llm_crypto.FIELD_PASSWORD, password)
        except llm_crypto.LlmCryptoError as exc:
            raise _ProvisionStop(exc.code) from None
        self._llm_guarded_update(row['id'], token, {'newapi_password_enc': encrypted})
        return password

    @api.model
    def _llm_p1_p2(self, client, account_id, token, username, password, deadline):
        """P1 建使用者、P2 search 取 id。回傳 ``(stored_user_id, pending_user_id)``。

        P1 成功 ⇒ P2 的 id 立刻存（stored）；P1 撞名或結果未知 ⇒ P2 找到的 id 只是候選
        （pending），要等 P4 用存好的密碼登入、``data.user.id`` 相同才存。
        """
        created = client.ap_create_user(username, password, 'PaaS %d' % account_id,
                                        deadline=deadline)
        if isinstance(created, nac.NotExecuted):
            raise _stop_for(created)
        if isinstance(created, nac.Definite):
            if created.proxy_error or created.code:
                raise _stop_for(created)
            if created.message_key:
                # Invalid parameters／權限不足：PaaS 這邊送錯了，重試沒有用。
                raise _ProvisionStop('llm_newapi_user_create_rejected', terminal=True)
        # Ok；或 success:false 沒有 message_key／結果未知（寫入 ⇒ Unknown）：可能撞名。
        found = client.ap_search_user(username, deadline=deadline)
        if not found.ok:
            raise _stop_for(found)
        matches = [item for item in _items(found.data)
                   if isinstance(item, dict) and item.get('username') == username]
        if not matches:
            raise _ProvisionStop('llm_newapi_user_missing' if created.ok
                                 else 'llm_newapi_user_create_unknown')
        if len(matches) > 1:
            raise _ProvisionStop('llm_newapi_user_conflict', terminal=True)
        match = matches[0]
        user_id = match.get('id')
        if (type(user_id) is not int or user_id <= 0 or match.get('DeletedAt')
                or match.get('status') != 1 or match.get('group') != 'default'
                or match.get('role') != 1):
            raise _ProvisionStop('llm_newapi_user_conflict', terminal=True)
        if created.ok:
            self._llm_guarded_update(account_id, token, {'newapi_user_id': user_id})
            return user_id, None
        return 0, user_id

    @api.model
    def _llm_p5_to_p7(self, client, account_id, token, username, ip, jwt, password,
                      attempts, deadline):
        """P5（只在重試時）數 PAT、P6 換 proof、P7 建 UP 並立刻加密存好。回傳 UP。"""
        if attempts > 1:
            listed = client.user_list_access_tokens(ip, jwt, deadline=deadline)
            if not listed.ok:
                raise _stop_for(listed)
            if len(_items(listed.data)) >= _PAT_LIMIT_THRESHOLD:
                # 孤兒 PAT 太多：清理（撤銷要 proof＋DELETE 路由）在 LLM-03。
                raise _ProvisionStop('llm_newapi_pat_limit', terminal=True)
        now = int(time.time())
        expires_at = now + nac.UP_LIFETIME_SECONDS
        verified = client.user_verify_generate(ip, jwt, password, expires_at, deadline=deadline)
        if not verified.ok:
            raise _stop_for(verified)
        vdata = verified.data if isinstance(verified.data, dict) else {}
        proof = vdata.get('proof_token')
        if not (isinstance(proof, str) and nac.JWT_RE.fullmatch(proof)):
            raise _ProvisionStop('llm_newapi_contract_verify', terminal=True)
        created = client.user_create_access_token(
            ip, jwt, proof, nac.up_name(account_id, now), expires_at, deadline=deadline)
        if not created.ok:
            # Unknown ⇒ new-api 可能多了一把孤兒 PAT；下次重試由 P5 數。
            raise _stop_for(created)
        cdata = created.data if isinstance(created.data, dict) else {}
        item = cdata.get('item') if isinstance(cdata.get('item'), dict) else {}
        up = cdata.get('token')
        up_id = item.get('id')
        hint = item.get('token_hint')
        if not (isinstance(up, str) and nac.UP_RE.fullmatch(up)) or type(up_id) is not int:
            raise _ProvisionStop('llm_newapi_contract_pat', terminal=True)
        try:
            encrypted = llm_crypto.encrypt(account_id, username, llm_crypto.FIELD_UP_TOKEN, up)
        except llm_crypto.LlmCryptoError as exc:
            raise _ProvisionStop(exc.code) from None
        self._llm_guarded_update(account_id, token, {
            'up_token_enc': encrypted,
            'up_token_newapi_id': up_id,
            'up_token_hint': hint[:16] if isinstance(hint, str) else None,
            'up_created_at': datetime.fromtimestamp(now, tz=timezone.utc).replace(tzinfo=None),
            'up_expires_at': datetime.fromtimestamp(expires_at, tz=timezone.utc).replace(tzinfo=None),
            'up_state': 'valid',
        })
        return up

    @api.model
    def _llm_p9_finish(self, account_id, token, user_id, me):
        """P9：以 UP 讀 ``/api/user/self``，id、分組、額度都對才 ``state=active``。"""
        if not me.ok:
            raise _stop_for(me)
        data = me.data if isinstance(me.data, dict) else {}
        quota = data.get('quota')
        if (data.get('id') != user_id or data.get('group') != 'default'
                or type(quota) is not int or quota != 0):
            raise _ProvisionStop('llm_newapi_invariant', terminal=True)
        used = data.get('used_quota')
        count = data.get('request_count')
        self._llm_guarded_update(account_id, token, {
            'state': 'active',
            'quota_cached': quota,
            'used_quota_cached': used if type(used) is int else 0,
            'request_count_cached': count if type(count) is int else 0,
            'balance_synced_at': balance_stamp(),
            'provision_attempts': 0,
            'next_attempt_at': None,
            'last_error_code': None,
            'provision_lease_until': None,
            'provision_lease_token': None,
        })
        _logger.info('[LLM] provisioning account=%s active', account_id)
        return True

    # ------------------------------------------------------------------ #
    #  餘額刷新 cron（DESIGN §1.3.6；LLM-02b）                              #
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_llm_balance_refresh(self):
        """每 10 分鐘以 AP 分頁刷新餘額快取；未設定（N27）時第一行就 return、零外呼。

        ⚠ **凍結契約**（同 ``_cron_llm_provision``）：``data/llm_cron.xml`` 是 noupdate，本方法的名字
        與所在的 model 不能改（``llm_common.LLM_CRONS``；``TestLlmCronContract`` 斷言 DB 裡的 code）。
        """
        run_llm_cron(self.env, BALANCE_CRON_XMLID, self._llm_balance_refresh_tick,
                     items=BALANCE_PAGES_PER_TICK, config=llm_config(self.env))

    @api.model
    def _llm_balance_refresh_tick(self, config, tick):
        """一個 tick：從 ``balance_refresh_page`` 接著讀，最多 ``tick.items`` 頁。回傳「還有剩」。

        一輪只有在真的讀到一頁 list、而且是最後一頁（筆數 < page size，或 page×size ≥ total）時才算
        跑完（page 回 1、寫 ``balance_refresh_finished_at``）。回應形狀不對（``data`` 不是 dict、
        ``items`` 不是 list）＝失敗：停在這一頁、不寫 finished_at（LLM-14 看刷新是否過期才準）。
        """
        if not config['identity_ok']:
            _logger.warning('[LLM] balance refresh skipped: username prefix / IP block misconfigured')
            report_config_unavailable(self.env, CONFIG_IDENTITY_MISMATCH)
            return False
        Account = self.sudo()
        if not Account.search_count([('state', 'in', BALANCE_STATES),
                                     ('newapi_user_id', '>', 0)], limit=1):
            # 沒有任何要刷新的帳戶：不外呼（也不必翻 new-api 上別的環境的使用者）。
            return False
        try:
            client = Account._llm_client(nac.ROLE_CRON)
        except nac.NewApiError as exc:
            _logger.error('[LLM] balance refresh skipped: %s', exc.code)
            report_config_unavailable(self.env, _config_code(exc))
            return False
        State = self.env['woow_paas_platform.llm_runtime_state']
        page = max(1, int(State._llm_read()['balance_refresh_page'] or 1))
        if page == 1:
            State._llm_write(balance_refresh_started_at=_utcnow())
        pattern = re.compile(re.escape(config['prefix']) + '[0-9]{10}', re.ASCII)
        matched = updated = 0
        tick.delay = BALANCE_TICK_SPACING_SECONDS
        while tick.can_take(nac.MIN_CALL_SECONDS):
            read_at = balance_stamp()
            result = client.ap_list_users(page, BALANCE_PAGE_SIZE, deadline=tick.deadline)
            tick.took()
            if not result.ok:
                if isinstance(result, nac.NotExecuted) and result.reason == 'deadline':
                    tick.more = True
                    break
                # 停在這一頁：下一次排程（10 分鐘後）從同一頁再試，不在這裡連續重送。
                _logger.warning('[LLM] balance refresh page=%s: %s/%s', page, result.kind,
                                result.label())
                return False
            data = result.data
            items = data.get('items') if isinstance(data, dict) else None
            if not isinstance(items, list):
                # ok 但形狀不對：不能當成「這一輪跑完」（那會讓過期監控以為刷新正常）。
                _logger.warning('[LLM] balance refresh page=%s: unexpected response shape', page)
                return False
            rows = self._llm_balance_rows(items, pattern)
            matched += len(rows)
            stored = self._llm_store_balances(rows, read_at)
            if stored is None:
                # 寫不回去（DB 錯誤）：停在這一頁，下一次排程從這一頁重讀（不跳過這 100 戶）。
                return False
            updated += stored
            total = data.get('total')
            if len(items) < BALANCE_PAGE_SIZE or (
                    type(total) is int and page * BALANCE_PAGE_SIZE >= total):
                State._llm_write(balance_refresh_page=1, balance_refresh_started_at=None,
                                 balance_refresh_finished_at=_utcnow())
                _logger.info('[LLM] balance refresh finished at page=%s (this tick: matched=%s '
                             'updated=%s)', page, matched, updated)
                return False
            page += 1
            State._llm_write(balance_refresh_page=page)
        _logger.info('[LLM] balance refresh continues at page=%s in %ss (this tick: matched=%s '
                     'updated=%s)', page, tick.delay, matched, updated)
        return True

    @staticmethod
    def _llm_balance_rows(items, pattern):
        """new-api 使用者列表 → ``(username, user_id, quota, used_quota, request_count)``。

        只留本環境前綴、沒被軟刪除、數字都是整數的；其他（別的環境、管理員、形狀不對）一律略過。
        ``quota`` 可以是負的（透支，DESIGN §4.9）。
        """
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            username = item.get('username')
            user_id = item.get('id')
            quota = item.get('quota')
            used = item.get('used_quota')
            count = item.get('request_count')
            if not (isinstance(username, str) and pattern.fullmatch(username)):
                continue
            if type(user_id) is not int or not 0 < user_id <= nac.INT32_MAX \
                    or item.get('DeletedAt'):
                continue
            if not all(type(v) is int for v in (quota, used, count)):
                continue
            if not (-_INT8_MAX <= quota <= _INT8_MAX and 0 <= used <= _INT8_MAX
                    and 0 <= count <= _INT8_MAX):
                continue
            rows.append((username, user_id, quota, used, count))
        return rows

    @api.model
    def _llm_store_balances(self, rows, read_at):
        """一頁的餘額以獨立 cursor 一次寫回並 commit，回傳更新了幾戶；DB 錯誤回 ``None``。

        守衛：使用者名稱與 ``newapi_user_id`` 都要對得上、帳戶是 active／suspended、而且現有的
        ``balance_synced_at`` **嚴格早於**這次讀取（``read_at``，:func:`balance_stamp`，在 GET 之前取）：
        P9 或日後轉換在讀取之後寫的值不會被這頁的舊值蓋掉；同一微秒也不蓋（寧可等下一輪）。

        交易是 **READ COMMITTED**（審查 LLM-02b 第 2 輪）：別的交易（P9、LLM-08 的轉換入帳）改了這一頁
        某一戶、而且 commit 了，REPEATABLE READ 會整句 40001（整頁 100 戶都丟掉）；READ COMMITTED
        等對方 commit 之後逐列重新檢查守衛（EvalPlanQual），對方寫的較新值留著、其他戶照寫。
        """
        if not rows:
            return 0
        values = ', '.join(['(%s::varchar, %s::integer, %s::bigint, %s::bigint, %s::bigint)']
                           * len(rows))
        # 參數依 SQL 裡出現的順序：SET、VALUES、WHERE。
        params = [read_at, SUPERUSER_ID, _utcnow()]
        for row in rows:
            params.extend(row)
        params.extend([BALANCE_STATES, read_at])
        try:
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                cr.execute(
                    'UPDATE {t} AS a SET quota_cached = v.quota, used_quota_cached = v.used, '
                    'request_count_cached = v.cnt, balance_synced_at = %s, write_uid = %s, '
                    'write_date = %s '
                    'FROM (VALUES {values}) AS v(username, user_id, quota, used, cnt) '
                    'WHERE a.newapi_username = v.username AND a.newapi_user_id = v.user_id '
                    'AND a.state IN %s '
                    'AND (a.balance_synced_at IS NULL OR a.balance_synced_at < %s)'.format(
                        t=ACCOUNT_TABLE, values=values),
                    params)
                return cr.rowcount
        except psycopg2.Error as exc:
            _logger.warning('[LLM] balance refresh: page write failed (pgcode=%s)',
                            getattr(exc, 'pgcode', None))
            return None
