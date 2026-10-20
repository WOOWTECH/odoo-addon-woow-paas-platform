"""AI 模型 API 金鑰的鏡像（DESIGN §2.3、§1.3.2、§1.3.3；票 LLM-01b 只做 ``kind='free_trial'``）。

new-api 的 token 才是本體；PaaS 只存鏡像：token id、名稱、遮罩、狀態。**明文金鑰不進 DB、
不進 log**——只出現在 ``/api/llm/keys/create`` 的回應裡一次（DESIGN §5.4）；代理另外強制
「只能 reveal 建立 120 秒內、名稱符合 ``wp_<數字>_`` 的金鑰」。

建立流程（同步，HTTP 請求內完成，總預算 20 秒）：

1. 前端在打開建立表單時產生 ``client_request_id``；同一個 id 再送 ⇒ ``llm_key_already_shown``。
2. 檢查帳戶 active、條款（改版且要求重新同意 ⇒ ``llm_tos_outdated``，LLM-04）、20 把上限（D10）、
   免費試用每帳號最多 1 把有效（DB partial unique index 也擋並發）。每日上限與「每天最多建 3 把」
   由 llm_free_usage 的台北日計數守住。
   2′. **每帳號每小時最多 10 次建金鑰**（N6；LLM-02b）：本地檢查都過了、第一次外呼之前，
   ``llm_event._llm_throttle_reserve`` 以獨立交易數＋記一筆 ``key_create``；到上限回
   ``llm_rate_limited``＋``retry_after``，零外呼。做完以 ``_llm_event_finish`` 補結果（ok／錯誤碼）；
   確定沒打到 new-api 的（第 4 步 NotExecuted 或被代理擋下、第 3 步就失敗）記 ``not_executed``，
   不算進 10 次（N6 保護的是 new-api 的 CT 桶；所有嘗試另有每小時 30 次的上限）。
3. **獨立 cursor** INSERT 鏡像 ``state=creating``、``newapi_name=wp_<id>_<rand4>`` 並 commit。
4. UP ``POST /api/token/`` → 5. ``GET /api/token/`` 依名稱找 id → 6. ``POST /api/token/:id/key``。
7. 獨立 cursor 把鏡像改成 ``active``（token id、遮罩、``revealed_at``）。

中途失敗：第 4 步確定沒執行 ⇒ 鏡像標 ``deleted``（從沒存在過）；結果未知 ⇒ 第 5 步依名稱
找：**列表成功而且確定沒有**才用同名重送一次（DESIGN §1.3.2）；列表本身失敗（不知道有沒有）
⇒ 不重送、鏡像標 ``failed_reveal``、回 ``llm_unavailable``——否則第一次 POST 其實已 commit 時，
new-api 會多一把同名、沒有鏡像的試用金鑰（LLM-10 經鏡像停用「該帳號全部試用金鑰」時漏掉它）。
第 6 步失敗 ⇒ ``failed_reveal``（超過 120 秒的金鑰不能再 reveal，這是刻意的）。整段吞例外、不讓 ``OperationalError`` 往外拋給 ``retrying()``
（controller 那一層），以免重播時又建一把。
"""
import logging
import re
import secrets
import string
import time
from datetime import datetime, timedelta, timezone

from psycopg2 import IntegrityError

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    ERR_LLM_FREE_KEY_EXISTS,
    ERR_LLM_INTERNAL,
    ERR_LLM_KEY_ALREADY_SHOWN,
    ERR_LLM_KEY_INVALID,
    ERR_LLM_KEY_LIMIT,
    ERR_LLM_KEY_NOT_FOUND,
    ERR_LLM_KEY_LIMIT_BELOW_USED,
    ERR_LLM_KEY_CANNOT_ENABLE,
    ERR_LLM_RATE_LIMITED,
    ERR_LLM_UNAVAILABLE,
    MAX_KEYS_PER_ACCOUNT,
    QUOTA_PER_POINT,
    RESULT_NOT_EXECUTED,
    LlmError,
    LlmQuotaInteger,
    _is_llm_internal,
    _llm_internal,
    _without_llm_internal,
)
from .llm_event import _begin, safe_code

_logger = logging.getLogger(__name__)

KEY_MODEL = 'woow_paas_platform.llm_api_key'
KEY_TABLE = 'woow_paas_platform_llm_api_key'
EVENT_MODEL = 'woow_paas_platform.llm_event'
_LOCK_TIMEOUT = '5s'
_CLIENT_REQUEST_ID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.ASCII)
_REVEALED_KEY_RE = re.compile(r'[A-Za-z0-9]{16,128}', re.ASCII)
#: 建金鑰的總預算是 20 秒；超過這麼久還是 creating 的列，處理它的 worker 已經不在了。
_STALE_CREATING_SECONDS = 300
_COLUMN_RE = re.compile(r'[a-z][a-z0-9_]{0,62}')
_RAND4_ALPHABET = string.ascii_lowercase + string.digits
# Must remain no broader than ``newapi_client.paid_token_body`` / proxy policy.
# A stale or malformed cached catalogue is an availability failure, never a reason
# to reserve throttle or create a mirror that later cannot be posted.
_MODEL_ID_RE = re.compile(r'[A-Za-z0-9._:/-]{1,128}', re.ASCII)
_PAID_EXPIRY_DAYS = {'30d': 30, '90d': 90, '180d': 180}
_MAX_KEY_LABEL_LENGTH = 64
_MAX_PAID_LIMIT_POINTS = nac.TOKEN_MAX_REMAIN_QUOTA // QUOTA_PER_POINT
_OMITTED = object()
#: 「有效」的試用金鑰（DESIGN §2.3：每帳號最多 1 把）。
_FREE_TRIAL_LIVE_STATES = ('creating', 'active', 'disabled')
_DISABLED_STATUS_BY_REASON = {
    'free_daily_cap': 'free_capped',
    'suspend': 'suspended',
    'overdraft': 'overdraft',
}


class _ListFailed:
    """``_llm_find_token`` 的「列表本身失敗」：不知道 token 在不在（≠ 確定不在的 ``None``）。"""

    __slots__ = ()

    def __repr__(self):
        return '<token list failed>'


TOKEN_LIST_FAILED = _ListFailed()

KEY_STATE_SELECTION = [
    ('creating', 'Creating'),
    ('active', 'Active'),
    ('disabled', 'Disabled'),
    ('failed_reveal', 'Creation interrupted'),
    ('deleted', 'Deleted'),
    ('lost', 'Lost'),
]


def _utcnow():
    return fields.Datetime.now()


def _iso(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


class LlmOptionalLimitInteger(fields.Integer):
    """Keep an unlimited SQL NULL distinct from a corrupt zero total limit.

    Odoo's standard Integer converts NULL/False to 0 at each ORM boundary.
    Only this optional limit field needs nullable semantics; its PostgreSQL
    int4 column and the framework's registered integer type stay unchanged.
    """

    def convert_to_cache(self, value, record, validate=True):
        if value is None or value is False:
            return None
        return super().convert_to_cache(value, record, validate=validate)

    def convert_to_column(self, value, record, values=None, validate=True):
        if value is None or value is False:
            return None
        return super().convert_to_column(value, record, values=values, validate=validate)

    def convert_to_record(self, value, record):
        return None if value is None else super().convert_to_record(value, record)


class LlmApiKey(models.Model):
    _name = 'woow_paas_platform.llm_api_key'
    _description = 'AI Models API Key (mirror)'
    _order = 'id desc'
    _rec_name = 'newapi_name'

    account_id = fields.Many2one(
        'woow_paas_platform.llm_account', required=True, ondelete='restrict', index=True,
        readonly=True)
    partner_id = fields.Many2one(
        related='account_id.partner_id', store=True, index=True, readonly=True)
    kind = fields.Selection(
        [('paid', 'Paid'), ('free_trial', 'Free trial')], required=True, readonly=True)
    key_label = fields.Char(
        readonly=True,
        help='User-facing name (only in PaaS). Never placed in alerts, incidents or emails (N24).')
    newapi_name = fields.Char(readonly=True, index=True, copy=False,
                              help='wp_<id>_<rand4> — the token name on new-api.')
    newapi_token_id = fields.Integer(readonly=True, copy=False)
    client_request_id = fields.Char(readonly=True, copy=False)
    state = fields.Selection(KEY_STATE_SELECTION, required=True, default='creating',
                             readonly=True, index=True)
    disabled_by = fields.Selection(
        [('user', 'User'), ('suspend', 'Suspended'), ('free_daily_cap', 'Free daily cap'),
         ('overdraft', 'Overdraft'), ('ops', 'Operations'), ('dr_reconcile', 'DR reconcile')],
        readonly=True, copy=False)
    free_capped_day = fields.Date(readonly=True, copy=False)
    free_cap_attempted_at = fields.Datetime(readonly=True, copy=False)
    free_cap_pending = fields.Boolean(readonly=True, copy=False, default=False)
    masked_key = fields.Char(readonly=True, copy=False, help='sk- plus first 4 and last 4.')
    quota_limit_points = LlmOptionalLimitInteger(readonly=True)
    expires_at = fields.Datetime(readonly=True)
    model_allowlist = fields.Text(readonly=True)
    created_uid = fields.Many2one('res.users', readonly=True, ondelete='set null')
    created_ip = fields.Char(readonly=True)
    revealed_at = fields.Datetime(readonly=True, copy=False)
    # LLM-07：到期通知要可去重；worker 寫入發送時間，絕不靠瞬時 new-api 狀態推論。
    expiry_notice_7d_at = fields.Datetime(readonly=True, copy=False)
    expiry_notice_1d_at = fields.Datetime(readonly=True, copy=False)
    created_notice_pending = fields.Boolean(readonly=True, default=False, copy=False,
                                            groups='base.group_system')
    not_me_disable_attempts = fields.Integer(readonly=True, default=0, copy=False,
                                             groups='base.group_system')
    not_me_disable_next_at = fields.Datetime(readonly=True, copy=False,
                                             groups='base.group_system')
    not_me_scan_page = fields.Integer(readonly=True, default=1, copy=False,
                                      groups='base.group_system')
    not_me_attempt_notice_id = fields.Integer(readonly=True, default=0, copy=False,
                                              groups='base.group_system')
    not_me_disabled_notice_id = fields.Integer(readonly=True, default=0, copy=False,
                                               groups='base.group_system')
    remain_quota_cached = LlmQuotaInteger(readonly=True, copy=False)
    used_quota_cached = LlmQuotaInteger(readonly=True, copy=False)
    accessed_at = fields.Datetime(readonly=True, copy=False)
    synced_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [
        ('client_request_uniq', 'UNIQUE(account_id, client_request_id)',
         'This key request was already processed.'),
    ]

    def init(self):
        # DESIGN §2.3：每帳號最多一把「有效」的免費試用金鑰；並發的兩個請求由 DB 擋。
        self.env.cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_api_key_free_trial_uniq '
            "ON %s (account_id) WHERE kind = 'free_trial' "
            "AND state IN ('creating', 'active', 'disabled')" % KEY_TABLE)

    # ------------------------------------------------------------------ #
    #  守衛（同 llm_account）                                               #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            raise AccessError(_('AI model keys can only be created by the platform.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            raise AccessError(_('AI model keys can only be changed by the platform.'))
        if 'account_id' in vals and any(k.account_id.id != vals['account_id'] for k in self):
            raise AccessError(_('An AI model key cannot move to another account.'))
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('AI model keys cannot be deleted; they are marked deleted.'))

    @api.constrains('account_id', 'state')
    def _llm_check_account_key_limit(self):
        """All non-deleted/non-lost keys occupy one of the account's 20 slots (D10)."""
        for account in self.mapped('account_id'):
            count = self.sudo().search_count([
                ('account_id', '=', account.id),
                ('state', 'not in', ('deleted', 'lost')),
            ])
            if count > MAX_KEYS_PER_ACCOUNT:
                raise ValidationError(_(
                    'An account can have at most %(max_keys)s API keys.',
                    max_keys=MAX_KEYS_PER_ACCOUNT,
                ))

    # ------------------------------------------------------------------ #
    #  DTO                                                                 #
    # ------------------------------------------------------------------ #
    def _llm_is_stale_creating(self):
        """卡在 ``creating`` 超過 ``_STALE_CREATING_SECONDS``：處理它的 worker 已經不在了。

        這種列（換版重啟 pod 等，worker 在請求途中被殺）本來只有下一次 ``keys/create`` 才會由
        ``_llm_settle_stale_creating`` 收尾；但 ``/api/llm/state`` 若照樣把它算成「已經有一把試用
        金鑰」，前端就把〔建立免費試用金鑰〕藏起來、永遠不會送出那一次。所以讀取端（state、清單）
        用同一條規則把它當成 ``failed_reveal``——只看不寫，真正的收尾仍在 ``keys/create``。
        """
        self.ensure_one()
        return self.state == 'creating' and bool(self.create_date) and self.create_date < (
            _utcnow() - timedelta(seconds=_STALE_CREATING_SECONDS))

    def _llm_status(self):
        """顯示狀態由 PaaS 計算（new-api 有 Redis 時 status 3／4 不寫回 DB）。"""
        self.ensure_one()
        if self._llm_is_stale_creating():
            return 'failed_reveal'
        if self.state in ('active', 'disabled') and self.expires_at \
                and self.expires_at < _utcnow():
            return 'expired'
        if self.state == 'disabled':
            return _DISABLED_STATUS_BY_REASON.get(self.disabled_by, 'disabled')
        if self.state == 'active' and self.quota_limit_points and \
                self.used_quota_cached is not None and \
                self.used_quota_cached >= self.quota_limit_points * QUOTA_PER_POINT:
            return 'exhausted'
        return self.state

    @staticmethod
    def _llm_points_from_quota(quota):
        """Translate new-api integer quota to whole PaaS points without float arithmetic."""
        # ``False`` is how an unset Odoo Integer may surface in a record cache;
        # it is not a zero-quota observation.  Only an actual non-negative
        # Python integer is safe to expose as an integer point value.
        if type(quota) is not int or quota < 0:
            return None
        return quota // QUOTA_PER_POINT

    def _llm_dto(self):
        self.ensure_one()
        return {
            'id': self.id,
            'kind': self.kind,
            'display_name': self.key_label or '',
            'masked_key': self.masked_key or None,
            'status': self._llm_status(),
            'disabled_by': self.disabled_by or None,
            'used_points': self._llm_points_from_quota(self.used_quota_cached),
            'limit_points': self.quota_limit_points or None,
            'remain_points': self._llm_points_from_quota(self.remain_quota_cached),
            'last_used_at': _iso(self.accessed_at),
            'expires_at': _iso(self.expires_at),
            'created_at': _iso(self.create_date),
            'models': [m for m in (self.model_allowlist or '').split(',') if m] or None,
        }

    @api.model
    def _llm_keys_payload(self, partner):
        """金鑰清單：只讀鏡像、不外呼（SWR，N26；LLM-02）。

        ``data_as_of``＝鏡像的用量與最後使用時間上一次從 new-api 讀回的時間（``llm_swr`` 的 ``tokens``）；
        超過 30 秒就排一次背景刷新（cron 以 CRON 金鑰、本帳戶的 UP 讀 ``GET /api/token/``）。
        """
        Account = self.env['woow_paas_platform.llm_account']
        account = Account._llm_account_for_partner(partner)
        account._llm_require_listable()
        keys = account.api_key_ids.filtered(lambda k: k.state not in ('deleted', 'lost'))
        Cache = self.env['woow_paas_platform.llm_swr_cache']
        cached = Cache._llm_swr_get(account, 'tokens')
        fetched_at = cached and cached['fetched_at']
        if keys.filtered('newapi_token_id'):
            Cache._llm_swr_touch(account, 'tokens', fetched_at)
        return {
            'items': [key._llm_dto() for key in keys.sorted('id', reverse=True)],
            'data_as_of': _iso(fetched_at) if fetched_at else None,
        }

    # ------------------------------------------------------------------ #
    #  建立免費試用金鑰（/api/llm/keys/create，kind='free_trial'）           #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_client_request_id(self, client_request_id):
        if not isinstance(client_request_id, str):
            raise LlmError(ERR_LLM_KEY_INVALID, field='client_request_id')
        client_request_id = client_request_id.strip().lower()
        if not _CLIENT_REQUEST_ID_RE.fullmatch(client_request_id):
            raise LlmError(ERR_LLM_KEY_INVALID, field='client_request_id')
        return client_request_id

    @api.model
    def _llm_preflight_create(self, account, client_request_id):
        """Reject replay, settle stale mirrors, and apply the cheap 20-key check locally."""
        existing = self.sudo().search([
            ('account_id', '=', account.id),
            ('client_request_id', '=', client_request_id),
        ], limit=1)
        if existing:
            raise LlmError(ERR_LLM_KEY_ALREADY_SHOWN)
        stale_before = _utcnow() - timedelta(seconds=_STALE_CREATING_SECONDS)
        keys = self.sudo().search([('account_id', '=', account.id)])
        stale = keys.filtered(lambda key: key.state == 'creating' and key.create_date
                              and key.create_date < stale_before)
        if stale:
            self._llm_settle_stale_creating(account, stale_before)
            self.invalidate_model()
            account.invalidate_recordset(['api_key_ids'])
            keys = self.sudo().search([('account_id', '=', account.id)])
        live = keys.filtered(lambda key: key.state not in ('deleted', 'lost'))
        if len(live) >= MAX_KEYS_PER_ACCOUNT:
            raise LlmError(ERR_LLM_KEY_LIMIT, max_keys=MAX_KEYS_PER_ACCOUNT)
        return live

    @api.model
    def _llm_validate_paid_create(self, account, *, name, limit_points, expires, models):
        """Translate only the closed paid-key form into a safe mirror/token specification."""
        account.ensure_one()
        if not isinstance(name, str) or not name.strip() or len(name) > _MAX_KEY_LABEL_LENGTH:
            raise LlmError(ERR_LLM_KEY_INVALID, field='name')
        if limit_points is not None and (
                type(limit_points) is not int
                or not 1 <= limit_points <= _MAX_PAID_LIMIT_POINTS):
            raise LlmError(ERR_LLM_KEY_INVALID, field='limit_points')
        if expires not in (*_PAID_EXPIRY_DAYS, 'never'):
            raise LlmError(ERR_LLM_KEY_INVALID, field='expires')
        expires_at = None
        if expires != 'never':
            expires_at = _utcnow().replace(microsecond=0) + timedelta(
                days=_PAID_EXPIRY_DAYS[expires])

        default_models = self._llm_default_models()
        selected = None
        if models is not None:
            if type(models) is not list or not models \
                    or any(not isinstance(model, str) for model in models) \
                    or len(set(models)) != len(models) \
                    or not set(models) <= set(default_models):
                raise LlmError(ERR_LLM_KEY_INVALID, field='models')
            requested = set(models)
            # Use cached catalogue order, never client ordering.
            selected = [model for model in default_models if model in requested]
        return {
            'kind': 'paid',
            'key_label': name,
            'limit_points': limit_points,
            'expires_at': expires_at,
            'model_allowlist': ','.join(selected) if selected else None,
            'models': tuple(selected) if selected else None,
            'model_limits_enabled': selected is not None,
        }

    @api.model
    def _llm_default_models(self):
        """Return validated cached default model IDs or fail closed before any external call."""
        payload = self.env['woow_paas_platform.llm_swr_cache']._llm_models_payload()
        if not isinstance(payload, dict) or payload.get('state') != 'ok' \
                or not isinstance(payload.get('items'), list):
            raise LlmError(ERR_LLM_UNAVAILABLE)
        default_models = []
        for item in payload['items']:
            if not isinstance(item, dict) or item.get('group') != 'default':
                continue
            model = item.get('model')
            if not isinstance(model, str) or not _MODEL_ID_RE.fullmatch(model) \
                    or model in default_models:
                raise LlmError(ERR_LLM_UNAVAILABLE)
            default_models.append(model)
        if not default_models:
            raise LlmError(ERR_LLM_UNAVAILABLE)
        return default_models

    @staticmethod
    def _llm_expired_time(expires_at):
        if expires_at is None:
            return -1
        return int(expires_at.replace(tzinfo=timezone.utc).timestamp())

    @api.model
    def _llm_create_with_reveal(self, account, client_request_id, user, real_ip, spec,
                                payload_factory):
        """Persist a validated mirror, create/reveal remotely, and close its audit event.

        Callers own their authorization, consent, reauthentication, and local quota
        checks.  Keeping the external transaction here makes paid and free-trial
        creation share the same durable ``creating``/one-time-reveal semantics.
        """
        Account = self.env['woow_paas_platform.llm_account']
        ip = account._llm_tenant_ip()
        up = account._llm_up_token()
        client = Account._llm_web_client('keys/create')
        Event = self.env[EVENT_MODEL]
        event_id = Event._llm_throttle_reserve(
            account, 'key_create', uid=user.id if user else None, real_ip=real_ip)
        deadline = time.monotonic() + nac.KEY_CREATE_BUDGET_SECONDS
        key_id = None
        try:
            key_id, newapi_name = self._llm_insert_creating(
                account, spec['kind'], client_request_id, user, real_ip,
                spec['expires_at'], spec['model_allowlist'], key_label=spec['key_label'],
                quota_limit_points=spec['limit_points'])
            created = self._llm_create_remote(
                account, client, ip, up, key_id, newapi_name, spec['expires_at'], deadline,
                payload_factory=payload_factory, spec=spec)
        except LlmError as exc:
            Event._llm_event_finish(event_id,
                                    RESULT_NOT_EXECUTED if exc.not_executed else 'failed',
                                    error_code=safe_code(exc.code),
                                    target_model=KEY_MODEL if key_id else None,
                                    target_id=key_id)
            raise
        except Exception:
            # A client/programming exception must not leave a durable creating mirror.
            if key_id:
                self._llm_finish(key_id, {'state': 'failed_reveal'})
            Event._llm_event_finish(event_id, 'failed', error_code=ERR_LLM_INTERNAL,
                                    target_model=KEY_MODEL if key_id else None,
                                    target_id=key_id)
            raise
        Event._llm_event_finish(event_id, 'ok', target_model=KEY_MODEL, target_id=key_id)
        return created

    @api.model
    def _llm_create_free_trial_key(self, partner, client_request_id, user, real_ip):
        Account = self.env['woow_paas_platform.llm_account']
        account = Account._llm_account_for_partner(partner)
        account._llm_require_active()
        # 免費試用不是付費點數轉換；僅需帳戶有效與目前條款已同意。
        account._llm_require_current_terms()
        if not isinstance(client_request_id, str) \
                or not _CLIENT_REQUEST_ID_RE.fullmatch(client_request_id.strip().lower()):
            raise LlmError(ERR_LLM_KEY_INVALID, field='client_request_id')
        client_request_id = client_request_id.strip().lower()
        keys = account.api_key_ids
        if keys.filtered(lambda k: k.client_request_id == client_request_id):
            raise LlmError(ERR_LLM_KEY_ALREADY_SHOWN)
        self.env['woow_paas_platform.llm_free_usage']._llm_assert_can_create(account.id)
        # 卡在 creating 的列（處理它的 worker 在請求途中被殺）會一直佔住「1 把試用金鑰」的名額：
        # 超過預算很久的那些先標成 failed_reveal（獨立交易），這次的判斷也一起排除。
        stale_before = _utcnow() - timedelta(seconds=_STALE_CREATING_SECONDS)
        stale = keys.filtered(lambda k: k.state == 'creating' and k.create_date
                              and k.create_date < stale_before)
        if stale:
            self._llm_settle_stale_creating(account, stale_before)
        keys -= stale
        live = keys.filtered(lambda k: k.state not in ('deleted', 'lost'))
        if len(live) >= MAX_KEYS_PER_ACCOUNT:
            raise LlmError(ERR_LLM_KEY_LIMIT, max_keys=MAX_KEYS_PER_ACCOUNT)
        if live.filtered(lambda k: k.kind == 'free_trial' and k.state in _FREE_TRIAL_LIVE_STATES):
            raise LlmError(ERR_LLM_FREE_KEY_EXISTS)

        expires_at = _utcnow().replace(microsecond=0) + timedelta(
            seconds=nac.FREE_TRIAL_LIFETIME_SECONDS)
        spec = {
            'kind': 'free_trial',
            'key_label': '',
            'limit_points': None,
            'expires_at': expires_at,
            'model_allowlist': ','.join(nac.FREE_TRIAL_MODELS),
            'models': tuple(nac.FREE_TRIAL_MODELS),
            'model_limits_enabled': True,
        }
        return self._llm_create_with_reveal(
            account, client_request_id, user, real_ip, spec,
            lambda newapi_name: nac.free_trial_token_body(
                newapi_name, self._llm_expired_time(expires_at)))

    @api.model
    def _llm_create_paid_key(self, partner, client_request_id, user, real_ip, session, *,
                             name, limit_points, expires, models):
        """Create a paid mirror/token pair without exposing its generated upstream name."""
        Account = self.env['woow_paas_platform.llm_account']
        account = Account._llm_account_for_partner(partner)
        account._llm_require_active()
        client_request_id = self._llm_client_request_id(client_request_id)
        spec = self._llm_validate_paid_create(
            account, name=name, limit_points=limit_points, expires=expires, models=models)
        account._llm_require_current_terms()
        if spec['expires_at'] is None and spec['limit_points'] is None:
            account._llm_require_reauth(session)
        self._llm_preflight_create(account, client_request_id)

        def paid_body(newapi_name):
            return nac.paid_token_body(
                newapi_name, self._llm_expired_time(spec['expires_at']),
                (spec['limit_points'] or 0) * QUOTA_PER_POINT,
                spec['limit_points'] is None,
                spec['model_limits_enabled'], spec['model_allowlist'] or '')

        return self._llm_create_with_reveal(
            account, client_request_id, user, real_ip, spec, paid_body)

    @api.model
    def _llm_create_remote(self, account, client, ip, up, key_id, name, expires, deadline,
                           *, payload_factory=None, spec=None):
        """§1.3.2 第 4–7 步：POST token → 依名稱找 id → reveal → 鏡像 active。"""
        body = (payload_factory(name) if payload_factory is not None else
                nac.free_trial_token_body(name, int(expires.timestamp())))
        created = client.user_create_token(ip, up, body, deadline=deadline)
        if isinstance(created, (nac.NotExecuted, nac.Definite)):
            # 確定沒建：鏡像標 deleted（使用者可以直接重建）。
            self._llm_finish(key_id, {'state': 'deleted'})
            try:
                self._llm_raise_for(account, created)
            except LlmError as exc:
                # 沒送出（NotExecuted）或代理就擋下了（proxy_error）：這次沒打到 new-api，節流不算它。
                exc.not_executed = isinstance(created, nac.NotExecuted) or bool(
                    getattr(created, 'proxy_error', None))
                raise
        token = self._llm_find_token(client, ip, up, name, deadline)
        if token is None and isinstance(created, nac.Unknown):
            # 結果未知、而且列表**成功**證明確實沒有：用同名重送一次（〔PROV〕§6.2）。
            retried = client.user_create_token(ip, up, body, deadline=deadline)
            if retried.ok or isinstance(retried, nac.Unknown):
                token = self._llm_find_token(client, ip, up, name, deadline)
        if token is None or token is TOKEN_LIST_FAILED:
            # 找不到，或列表失敗、不知道有沒有建成：不再送第二次 POST。
            self._llm_finish(key_id, {'state': 'failed_reveal'})
            raise LlmError(ERR_LLM_UNAVAILABLE)
        token_id = token['id']

        revealed = client.user_reveal_token(ip, up, token_id, deadline=deadline)
        if isinstance(revealed, nac.NotExecuted) and revealed.reason != 'deadline' \
                and not revealed.retry_after:
            revealed = client.user_reveal_token(ip, up, token_id, deadline=deadline)
        raw = revealed.data.get('key') if revealed.ok and isinstance(revealed.data, dict) else None
        if not (isinstance(raw, str) and _REVEALED_KEY_RE.fullmatch(raw)):
            self._llm_finish(key_id, {'state': 'failed_reveal', 'newapi_token_id': token_id})
            if revealed.ok:
                raise LlmError(ERR_LLM_UNAVAILABLE)
            self._llm_raise_for(account, revealed)
        masked = 'sk-%s…%s' % (raw[:4], raw[-4:])
        now = _utcnow()
        self._llm_finish(key_id, {
            'state': 'active',
            'newapi_token_id': token_id,
            'masked_key': masked,
            'revealed_at': now,
        })
        # 鏡像是獨立交易寫的：主 cursor 的 snapshot 看不到這一列，所以 DTO 直接由已知的值組。
        expires_at = spec['expires_at'] if spec is not None else expires.replace(tzinfo=None)
        return {
            'key_id': key_id,
            'key': 'sk-' + raw,
            'base_url': None,
            'id': key_id,
            'kind': spec['kind'] if spec is not None else 'free_trial',
            'display_name': spec['key_label'] if spec is not None else '',
            'masked_key': masked,
            'status': 'active',
            'disabled_by': None,
            'used_points': None,
            'limit_points': spec['limit_points'] if spec is not None else None,
            'remain_points': None,
            'last_used_at': None,
            'expires_at': _iso(expires_at),
            'created_at': _iso(now),
            'models': (list(spec['models']) if spec is not None and spec['models'] else
                       (None if spec is not None else list(nac.FREE_TRIAL_MODELS))),
        }

    @api.model
    def _llm_raise_for(self, account, result):
        """client 結果 → 使用者看得到的錯誤碼；UP 被拒 ⇒ ``up_state=invalid``（重簽在 LLM-03）。"""
        if isinstance(result, nac.Definite) and result.code in (
                'AUTH_UNAUTHORIZED', 'ACCESS_TOKEN_EXPIRED'):
            account._llm_mark_up_invalid()
        if result.status == 429 and result.retry_after:
            raise LlmError(ERR_LLM_RATE_LIMITED, retry_after=int(result.retry_after))
        _logger.warning('[LLM] keys/create account=%s: %s/%s', account.id, result.kind,
                        result.label())
        raise LlmError(ERR_LLM_UNAVAILABLE)

    @api.model
    def _llm_find_token(self, client, ip, up, name, deadline):
        """依名稱在 token 列表找剛建的那把。

        回傳三種：token dict（找到）、``None``（完整列表成功、確定沒有）、
        :data:`TOKEN_LIST_FAILED`（列表失敗或無法確認首頁完整——不知道有沒有）。呼叫端只能在
        ``None`` 時重送建立。
        """
        listed = client.user_list_tokens(ip, up, 1, 10, deadline=deadline)
        if not listed.ok or not isinstance(listed.data, dict):
            return TOKEN_LIST_FAILED
        items = listed.data.get('items')
        total = listed.data.get('total')
        if not isinstance(items, list) or type(total) is not int or total < 0 \
                or len(items) != total:
            return TOKEN_LIST_FAILED
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) \
                    or type(item.get('id')) is not int or item['id'] <= 0:
                return TOKEN_LIST_FAILED
            if item['name'] == name:
                return item
        return None

    @api.model
    def _llm_insert_creating(self, account, kind, client_request_id, user, real_ip,
                             expires_at, models, *, key_label='', quota_limit_points=None):
        """獨立 cursor INSERT ``state=creating`` 的鏡像並 commit（外呼前就持久化）。"""
        if kind not in ('paid', 'free_trial'):
            raise LlmError(ERR_LLM_KEY_INVALID, field='kind')
        if not isinstance(client_request_id, str) or not _CLIENT_REQUEST_ID_RE.fullmatch(
                client_request_id):
            raise LlmError(ERR_LLM_KEY_INVALID, field='client_request_id')
        if not isinstance(key_label, str) or len(key_label) > _MAX_KEY_LABEL_LENGTH:
            raise LlmError(ERR_LLM_KEY_INVALID, field='name')
        if expires_at is not None and not isinstance(expires_at, datetime):
            raise LlmError(ERR_LLM_KEY_INVALID, field='expires')
        if models is not None and not isinstance(models, str):
            raise LlmError(ERR_LLM_KEY_INVALID, field='models')
        if quota_limit_points is not None and (
                type(quota_limit_points) is not int or quota_limit_points <= 0):
            raise LlmError(ERR_LLM_KEY_INVALID, field='limit_points')
        now = _utcnow()
        rand4 = ''.join(secrets.choice(_RAND4_ALPHABET) for _ in range(4))
        try:
            with self.env.registry.cursor() as cr:
                _begin(self.env, cr, read_committed=True)
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                # This account-row lock serializes the count+insert pair across web workers.
                # The cheap service preflight avoids a throttle/event for ordinary 21st-key
                # attempts; this independent-cursor guard closes the remaining race.
                cr.execute('SELECT id FROM %s WHERE id = %%s FOR NO KEY UPDATE' % account._table,
                           (account.id,))
                if cr.fetchone() is None:
                    raise LlmError(ERR_LLM_UNAVAILABLE)
                cr.execute(
                    "SELECT count(*) FROM %s WHERE account_id = %%s "
                    "AND state NOT IN ('deleted', 'lost')" % KEY_TABLE,
                    (account.id,))
                if cr.fetchone()[0] >= MAX_KEYS_PER_ACCOUNT:
                    raise LlmError(ERR_LLM_KEY_LIMIT, max_keys=MAX_KEYS_PER_ACCOUNT)
                if kind == 'free_trial':
                    from .llm_free_usage import taipei_day
                    self.env['woow_paas_platform.llm_free_usage']._llm_reserve_key_cr(
                        cr, account.id, taipei_day(now.replace(tzinfo=timezone.utc).timestamp()))
                cr.execute(
                    'INSERT INTO %s (account_id, partner_id, kind, client_request_id, state, '
                    'key_label, quota_limit_points, expires_at, model_allowlist, '
                    'created_uid, created_ip, create_uid, create_date, write_uid, write_date) '
                    'VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, '
                    '%%s, %%s) '
                    'RETURNING id' % KEY_TABLE,
                    (account.id, account.partner_id.id, kind, client_request_id, 'creating',
                     key_label, quota_limit_points, expires_at, models, user.id if user else None,
                     real_ip or None, SUPERUSER_ID, now, SUPERUSER_ID, now), log_exceptions=False)
                key_id = cr.fetchone()[0]
                name = 'wp_%d_%s' % (key_id, rand4)
                cr.execute('UPDATE %s SET newapi_name = %%s WHERE id = %%s' % KEY_TABLE,
                           (name, key_id))
        except LlmError as exc:
            exc.not_executed = True
            raise
        except IntegrityError as exc:
            constraint = getattr(getattr(exc, 'diag', None), 'constraint_name', '') or ''
            error = LlmError(ERR_LLM_FREE_KEY_EXISTS if 'free_trial' in constraint
                             else ERR_LLM_KEY_ALREADY_SHOWN)
            error.not_executed = True   # 還沒外呼
            raise error from None
        return key_id, name

    @api.model
    def _llm_settle_stale_creating(self, account, stale_before):
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute(
                'UPDATE %s SET state = %%s, write_uid = %%s, write_date = %%s '
                'WHERE account_id = %%s AND state = %%s AND create_date < %%s' % KEY_TABLE,
                ('failed_reveal', SUPERUSER_ID, _utcnow(), account.id, 'creating', stale_before))

    @api.model
    def _llm_finish(self, key_id, values):
        values = dict(values, write_uid=SUPERUSER_ID, write_date=_utcnow())
        if values.get('state') == 'active':
            values['created_notice_pending'] = True
        columns = sorted(values)
        if not all(_COLUMN_RE.fullmatch(column) for column in columns):
            raise ValueError('bad column name')
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute(
                'UPDATE %s SET %s WHERE id = %%s AND state = %%s '
                'RETURNING account_id, kind, create_date'
                % (KEY_TABLE, ', '.join('%s = %%s' % column for column in columns)),
                [values[column] for column in columns] + [key_id, 'creating'])
            changed = cr.fetchone()
            if changed and changed[1] == 'free_trial' and values.get('state') == 'deleted':
                # Definite non-creation gives back the reserved daily slot once.
                from .llm_free_usage import USAGE_TABLE, taipei_day
                cr.execute('UPDATE %s SET trial_keys_created=GREATEST(0,trial_keys_created-1) '
                           'WHERE account_id=%%s AND day_tpe=%%s' % USAGE_TABLE,
                           (changed[0], taipei_day(changed[2].replace(tzinfo=timezone.utc).timestamp())))
        if changed and values.get('state') == 'active':
            # This mirror committed on the independent cursor. A fresh cursor
            # avoids the HTTP transaction's stale REPEATABLE READ snapshot.
            from .llm_notifications import MODEL as NOTIFICATION_MODEL
            try:
                with self.env.registry.cursor() as notice_cr:
                    notice_env = api.Environment(notice_cr, SUPERUSER_ID, {})
                    notice_id = notice_env[NOTIFICATION_MODEL]._llm_notice(
                        changed[0], 'key_created', KEY_MODEL, key_id,
                        occurrence_key=str(key_id), token=True, email=True)
                    if notice_id:
                        key = notice_env[KEY_MODEL].sudo().browse(key_id)
                        from .llm_common import _llm_internal
                        _llm_internal(key).write({'created_notice_pending': False})
            except Exception:  # noqa: BLE001 - key creation must survive notification failures
                _logger.warning('[LLM] key-created notice deferred key=%s', key_id)

    @api.model
    def _llm_management_key(self, partner, key_id, *, allow_failed_reveal=False):
        """Anchor an exposed mirror id to the caller's account before any upstream access."""
        account = self.env['woow_paas_platform.llm_account']._llm_account_for_partner(partner)
        if not account:
            raise LlmError(ERR_LLM_KEY_NOT_FOUND)
        account._llm_require_active()
        key = self.sudo().search([('id', '=', key_id), ('account_id', '=', account.id)], limit=1)
        if not key:
            raise LlmError(ERR_LLM_KEY_NOT_FOUND)
        # SWR writes the mirror through an independent cursor. Lock before
        # remote I/O so a stale snapshot fails here, not after a DELETE or a
        # relative expiry extension which Odoo would replay on transaction retry.
        # NOWAIT also keeps competing management requests bounded.
        self.env.cr.execute(
            'SELECT id FROM %s WHERE id = %%s AND account_id = %%s FOR UPDATE NOWAIT'
            % KEY_TABLE, (key.id, account.id))
        if not self.env.cr.fetchone():
            raise LlmError(ERR_LLM_KEY_NOT_FOUND)
        if key.state in ('deleted', 'lost', 'creating') or \
                (key.state == 'failed_reveal' and not allow_failed_reveal):
            raise LlmError(ERR_LLM_KEY_NOT_FOUND)
        return account, key

    @api.model
    def _llm_management_token_id(self, key, *, allow_missing=False):
        """Return an exact valid upstream id, or fail closed before client construction."""
        token_id = key.newapi_token_id
        if type(token_id) is int and 1 <= token_id <= nac.INT32_MAX:
            return token_id
        if allow_missing:
            return None
        raise LlmError(ERR_LLM_UNAVAILABLE)

    @api.model
    def _llm_management_remote(self, account):
        return (account._llm_tenant_ip(), account._llm_up_token(),
                account._llm_web_client('keys/manage'))

    @api.model
    def _llm_management_missing(self, result):
        return isinstance(result, nac.Definite) and result.reason == 'no_message_key'

    @api.model
    def _llm_management_raise(self, account, result):
        self._llm_raise_for(account, result)

    @api.model
    def _llm_management_audit(self, action, account, key, user, real_ip):
        self.env[EVENT_MODEL]._llm_record(
            action, account_id=account.id, partner_id=account.partner_id.id,
            uid=user.id if user else None, real_ip=real_ip or None,
            target_model=KEY_MODEL, target_id=key.id)

    @api.model
    def _llm_update_key(self, partner, key_id, *, user=None, real_ip=None, name=_OMITTED,
                        limit_points=_OMITTED, expires=_OMITTED, models=_OMITTED):
        account, key = self._llm_management_key(partner, key_id)
        changes = any(value is not _OMITTED for value in (limit_points, expires, models))
        if name is not _OMITTED and (not isinstance(name, str) or not name.strip()
                                     or len(name) > _MAX_KEY_LABEL_LENGTH):
            raise LlmError(ERR_LLM_KEY_INVALID, field='name')
        if key.kind == 'free_trial' and changes:
            raise LlmError(ERR_LLM_KEY_INVALID)
        if not changes:
            if name is not _OMITTED and name != key.key_label:
                _llm_internal(key).write({'key_label': name})
                self._llm_management_audit('key_update', account, key, user, real_ip)
            dto = key._llm_dto()
            dto['remain_after_points'] = dto['remain_points']
            return dto
        if limit_points is _OMITTED:
            local_limit = key.quota_limit_points
            if local_limit is False or local_limit is None:
                requested_limit = None
            elif type(local_limit) is int and 1 <= local_limit <= _MAX_PAID_LIMIT_POINTS:
                requested_limit = local_limit
            else:
                raise LlmError(ERR_LLM_KEY_INVALID, field='limit_points')
        else:
            requested_limit = limit_points
        if requested_limit is not None and (type(requested_limit) is not int or
                not 1 <= requested_limit <= _MAX_PAID_LIMIT_POINTS):
            raise LlmError(ERR_LLM_KEY_INVALID, field='limit_points')
        expiry = key.expires_at if expires is _OMITTED else expires
        if expires is not _OMITTED and expires not in (*_PAID_EXPIRY_DAYS, 'never', None):
            raise LlmError(ERR_LLM_KEY_INVALID, field='expires')
        expires_at = (None if expiry in ('never', None) else
                      _utcnow().replace(microsecond=0) + timedelta(days=_PAID_EXPIRY_DAYS[expiry])
                      if expires is not _OMITTED else expiry)
        model_value = key.model_allowlist if models is _OMITTED else models
        if models is not _OMITTED:
            defaults = self._llm_default_models()
            if models is not None and (type(models) is not list or not models or
                    any(not isinstance(m, str) for m in models) or len(set(models)) != len(models)
                    or not set(models) <= set(defaults)):
                raise LlmError(ERR_LLM_KEY_INVALID, field='models')
            selected = [m for m in defaults if m in set(models)] if models is not None else None
            model_value = ','.join(selected) if selected else None
        token_id = self._llm_management_token_id(key)
        ip, up, client = self._llm_management_remote(account)
        got = client.user_get_token(ip, up, token_id)
        if not got.ok or not isinstance(got.data, dict):
            if self._llm_management_missing(got):
                _llm_internal(key).write({'state': 'deleted'})
                raise LlmError(ERR_LLM_KEY_NOT_FOUND)
            self._llm_management_raise(account, got)
        remote = got.data
        used = remote.get('used_quota')
        if limit_points is not _OMITTED:
            if type(used) is not int or used < 0:
                raise LlmError(ERR_LLM_UNAVAILABLE)
            if requested_limit is not None and used > requested_limit * QUOTA_PER_POINT:
                raise LlmError(ERR_LLM_KEY_LIMIT_BELOW_USED,
                               used=self._llm_points_from_quota(used))
        remaining = (0 if requested_limit is None else
                     max(0, requested_limit * QUOTA_PER_POINT -
                         (used if type(used) is int else 0))) \
            if limit_points is not _OMITTED else None
        try:
            body = nac.paid_token_update_body(
                remote, token_id, key.newapi_name,
                remain_quota=remaining,
                unlimited_quota=(requested_limit is None if limit_points is not _OMITTED else None),
                expired_time=(self._llm_expired_time(expires_at) if expires is not _OMITTED else None),
                model_limits_enabled=(bool(model_value) if models is not _OMITTED else None),
                model_limits=(model_value or '' if models is not _OMITTED else None))
        except nac.NewApiInputError:
            raise LlmError(ERR_LLM_UNAVAILABLE) from None
        written = client.user_update_token(ip, up, body)
        if not written.ok:
            self._llm_management_raise(account, written)
        vals = {}
        if limit_points is not _OMITTED:
            vals['quota_limit_points'] = requested_limit if requested_limit is not None else False
            vals['remain_quota_cached'] = remaining
            vals['used_quota_cached'] = used
        if expires is not _OMITTED:
            vals['expires_at'] = expires_at
        if models is not _OMITTED:
            vals['model_allowlist'] = model_value
        if name is not _OMITTED:
            vals['key_label'] = name
        _llm_internal(key).write(vals)
        self._llm_management_audit('key_update', account, key, user, real_ip)
        dto = key._llm_dto()
        dto['remain_after_points'] = (self._llm_points_from_quota(remaining)
                                      if limit_points is not _OMITTED else dto['remain_points'])
        return dto

    @api.model
    def _llm_extend_key(self, partner, key_id, user=None, real_ip=None):
        account, key = self._llm_management_key(partner, key_id)
        if key.kind != 'paid':
            raise LlmError(ERR_LLM_KEY_INVALID)
        if not key.expires_at:
            return key._llm_dto()
        token_id = self._llm_management_token_id(key)
        ip, up, client = self._llm_management_remote(account)
        got = client.user_get_token(ip, up, token_id)
        if not got.ok or not isinstance(got.data, dict):
            if self._llm_management_missing(got):
                _llm_internal(key).write({'state': 'deleted'})
                raise LlmError(ERR_LLM_KEY_NOT_FOUND)
            self._llm_management_raise(account, got)
        try:
            current = got.data.get('expired_time')
            if type(current) is not int or current <= 0:
                raise nac.NewApiInputError('token_response')
            base = max(int(_utcnow().replace(tzinfo=timezone.utc).timestamp()), current)
            expiry = base + 90 * 86400
            body = nac.paid_token_update_body(got.data, token_id, key.newapi_name,
                                              expired_time=expiry)
        except nac.NewApiInputError:
            raise LlmError(ERR_LLM_UNAVAILABLE) from None
        result = client.user_update_token(ip, up, body)
        if not result.ok:
            self._llm_management_raise(account, result)
        expiry_dt = datetime.fromtimestamp(expiry, timezone.utc).replace(tzinfo=None)
        _llm_internal(key).write({'expires_at': expiry_dt, 'expiry_notice_7d_at': False,
                                  'expiry_notice_1d_at': False})
        self._llm_management_audit('key_update', account, key, user, real_ip)
        return key._llm_dto()

    @api.model
    def _llm_set_key_status(self, partner, key_id, enabled, user=None, real_ip=None, session=None):
        if type(enabled) is not bool:
            raise LlmError(ERR_LLM_KEY_INVALID)
        account, key = self._llm_management_key(partner, key_id)
        if enabled:
            reason = key._llm_status()
            if reason in ('expired', 'exhausted', 'free_capped', 'suspended', 'overdraft'):
                raise LlmError(ERR_LLM_KEY_CANNOT_ENABLE, reason=reason)
            account._llm_require_current_terms()
        token_id = self._llm_management_token_id(key)
        ip, up, client = self._llm_management_remote(account)
        result = client.user_set_token_status(ip, up, token_id, enabled)
        if not result.ok:
            self._llm_management_raise(account, result)
        _llm_internal(key).write({'state': 'active' if enabled else 'disabled',
                                  'disabled_by': False if enabled else 'user'})
        self._llm_management_audit('key_status', account, key, user, real_ip)
        return key._llm_dto()

    @api.model
    def _llm_delete_key(self, partner, key_id, confirm_name, user=None, real_ip=None):
        account, key = self._llm_management_key(partner, key_id, allow_failed_reveal=True)
        if not isinstance(confirm_name, str) or confirm_name != (key.key_label or ''):
            raise LlmError(ERR_LLM_KEY_INVALID)
        token_id = self._llm_management_token_id(
            key, allow_missing=(key.state == 'failed_reveal'))
        if token_id is not None:
            ip, up, client = self._llm_management_remote(account)
            result = client.user_delete_token(ip, up, token_id)
            if not result.ok:
                self._llm_management_raise(account, result)
        _llm_internal(key).write({'state': 'deleted'})
        self._llm_management_audit('key_delete', account, key, user, real_ip)
        return {'deleted': True}
