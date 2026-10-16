"""AI 模型 API 金鑰的鏡像（DESIGN §2.3、§1.3.2、§1.3.3；票 LLM-01b 只做 ``kind='free_trial'``）。

new-api 的 token 才是本體；PaaS 只存鏡像：token id、名稱、遮罩、狀態。**明文金鑰不進 DB、
不進 log**——只出現在 ``/api/llm/keys/create`` 的回應裡一次（DESIGN §5.4）；代理另外強制
「只能 reveal 建立 120 秒內、名稱符合 ``wp_<數字>_`` 的金鑰」。

建立流程（同步，HTTP 請求內完成，總預算 20 秒）：

1. 前端在打開建立表單時產生 ``client_request_id``；同一個 id 再送 ⇒ ``llm_key_already_shown``。
2. 檢查帳戶 active、20 把上限（D10）、免費試用每帳號最多 1 把有效（DB partial unique index
   也擋並發）。每日上限與「每天最多建 3 把」在 LLM-10。
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
from odoo.exceptions import AccessError

from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_common import (
    ERR_LLM_FREE_KEY_EXISTS,
    ERR_LLM_KEY_ALREADY_SHOWN,
    ERR_LLM_KEY_INVALID,
    ERR_LLM_KEY_LIMIT,
    ERR_LLM_RATE_LIMITED,
    ERR_LLM_UNAVAILABLE,
    MAX_KEYS_PER_ACCOUNT,
    LlmError,
    LlmQuotaInteger,
    _is_llm_internal,
)

_logger = logging.getLogger(__name__)

KEY_TABLE = 'woow_paas_platform_llm_api_key'
_LOCK_TIMEOUT = '5s'
_CLIENT_REQUEST_ID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.ASCII)
_REVEALED_KEY_RE = re.compile(r'[A-Za-z0-9]{16,128}', re.ASCII)
#: 建金鑰的總預算是 20 秒；超過這麼久還是 creating 的列，處理它的 worker 已經不在了。
_STALE_CREATING_SECONDS = 300
_COLUMN_RE = re.compile(r'[a-z][a-z0-9_]{0,62}')
_RAND4_ALPHABET = string.ascii_lowercase + string.digits
#: 「有效」的試用金鑰（DESIGN §2.3：每帳號最多 1 把）。
_FREE_TRIAL_LIVE_STATES = ('creating', 'active', 'disabled')


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
    masked_key = fields.Char(readonly=True, copy=False, help='sk- plus first 4 and last 4.')
    quota_limit_points = fields.Integer(readonly=True)
    expires_at = fields.Datetime(readonly=True)
    model_allowlist = fields.Text(readonly=True)
    created_uid = fields.Many2one('res.users', readonly=True, ondelete='set null')
    created_ip = fields.Char(readonly=True)
    revealed_at = fields.Datetime(readonly=True, copy=False)
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
        return super().create(vals_list)

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

    # ------------------------------------------------------------------ #
    #  DTO                                                                 #
    # ------------------------------------------------------------------ #
    def _llm_status(self):
        """顯示狀態由 PaaS 計算（new-api 有 Redis 時 status 3／4 不寫回 DB）。"""
        self.ensure_one()
        if self.state in ('active', 'disabled') and self.expires_at \
                and self.expires_at < _utcnow():
            return 'expired'
        return self.state

    def _llm_dto(self):
        self.ensure_one()
        return {
            'id': self.id,
            'kind': self.kind,
            'display_name': self.key_label or '',
            'masked_key': self.masked_key or None,
            'status': self._llm_status(),
            'disabled_by': self.disabled_by or None,
            'used_points': None,
            'limit_points': self.quota_limit_points or None,
            'remain_points': None,
            'last_used_at': _iso(self.accessed_at),
            'expires_at': _iso(self.expires_at),
            'created_at': _iso(self.create_date),
            'models': [m for m in (self.model_allowlist or '').split(',') if m] or None,
        }

    @api.model
    def _llm_keys_payload(self, partner):
        Account = self.env['woow_paas_platform.llm_account']
        account = Account._llm_account_for_partner(partner)
        account._llm_require_listable()
        keys = account.api_key_ids.filtered(lambda k: k.state not in ('deleted', 'lost'))
        return {
            'items': [key._llm_dto() for key in keys.sorted('id', reverse=True)],
            'data_as_of': None,
        }

    # ------------------------------------------------------------------ #
    #  建立免費試用金鑰（/api/llm/keys/create，kind='free_trial'）           #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_create_free_trial_key(self, partner, client_request_id, user, real_ip):
        Account = self.env['woow_paas_platform.llm_account']
        account = Account._llm_account_for_partner(partner)
        account._llm_require_active()
        if not isinstance(client_request_id, str) \
                or not _CLIENT_REQUEST_ID_RE.fullmatch(client_request_id.strip().lower()):
            raise LlmError(ERR_LLM_KEY_INVALID, field='client_request_id')
        client_request_id = client_request_id.strip().lower()
        keys = account.api_key_ids
        if keys.filtered(lambda k: k.client_request_id == client_request_id):
            raise LlmError(ERR_LLM_KEY_ALREADY_SHOWN)
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

        ip = account._llm_tenant_ip()
        up = account._llm_up_token()
        try:
            client = Account._llm_client(nac.ROLE_WEB)
        except nac.NewApiError as exc:
            _logger.error('[LLM] keys/create: client unavailable: %s', exc.code)
            raise LlmError(ERR_LLM_UNAVAILABLE) from None

        deadline = time.monotonic() + nac.KEY_CREATE_BUDGET_SECONDS
        expires = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
            seconds=nac.FREE_TRIAL_LIFETIME_SECONDS)
        key_id, name = self._llm_insert_creating(
            account, 'free_trial', client_request_id, user, real_ip,
            expires.replace(tzinfo=None), ','.join(nac.FREE_TRIAL_MODELS))
        try:
            return self._llm_create_remote(account, client, ip, up, key_id, name, expires,
                                           deadline)
        except Exception:
            # 程式錯誤（例如 client 自己的例外）：鏡像不能卡在 creating。已經收尾的列不受影響
            # （``_llm_finish`` 只改 creating）。
            self._llm_finish(key_id, {'state': 'failed_reveal'})
            raise

    @api.model
    def _llm_create_remote(self, account, client, ip, up, key_id, name, expires, deadline):
        """§1.3.2 第 4–7 步：POST token → 依名稱找 id → reveal → 鏡像 active。"""
        body = nac.free_trial_token_body(name, int(expires.timestamp()))
        created = client.user_create_token(ip, up, body, deadline=deadline)
        if isinstance(created, (nac.NotExecuted, nac.Definite)):
            # 確定沒建：鏡像標 deleted（使用者可以直接重建）。
            self._llm_finish(key_id, {'state': 'deleted'})
            self._llm_raise_for(account, created)
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
        return {
            'key_id': key_id,
            'key': 'sk-' + raw,
            'base_url': None,
            'id': key_id,
            'kind': 'free_trial',
            'display_name': '',
            'masked_key': masked,
            'status': 'active',
            'disabled_by': None,
            'used_points': None,
            'limit_points': None,
            'remain_points': None,
            'last_used_at': None,
            'expires_at': _iso(expires.replace(tzinfo=None)),
            'created_at': _iso(now),
            'models': list(nac.FREE_TRIAL_MODELS),
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

        回傳三種：token dict（找到）、``None``（列表成功、確定沒有）、:data:`TOKEN_LIST_FAILED`
        （列表本身失敗：NotExecuted／Unknown／Definite——不知道有沒有）。呼叫端只能在 ``None``
        時重送建立。
        """
        listed = client.user_list_tokens(ip, up, 1, 10, deadline=deadline)
        if not listed.ok:
            return TOKEN_LIST_FAILED
        data = listed.data if isinstance(listed.data, dict) else {}
        items = data.get('items') if isinstance(data.get('items'), list) else []
        for item in items:
            if isinstance(item, dict) and item.get('name') == name \
                    and type(item.get('id')) is int and item['id'] > 0:
                return item
        return None

    @api.model
    def _llm_insert_creating(self, account, kind, client_request_id, user, real_ip,
                             expires_at, models):
        """獨立 cursor INSERT ``state=creating`` 的鏡像並 commit（外呼前就持久化）。"""
        now = _utcnow()
        rand4 = ''.join(secrets.choice(_RAND4_ALPHABET) for _ in range(4))
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                cr.execute(
                    'INSERT INTO %s (account_id, partner_id, kind, client_request_id, state, '
                    'expires_at, model_allowlist, created_uid, created_ip, '
                    'create_uid, create_date, write_uid, write_date) '
                    'VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s, %%s) '
                    'RETURNING id' % KEY_TABLE,
                    (account.id, account.partner_id.id, kind, client_request_id, 'creating',
                     expires_at, models, user.id if user else None, real_ip or None,
                     SUPERUSER_ID, now, SUPERUSER_ID, now), log_exceptions=False)
                key_id = cr.fetchone()[0]
                name = 'wp_%d_%s' % (key_id, rand4)
                cr.execute('UPDATE %s SET newapi_name = %%s WHERE id = %%s' % KEY_TABLE,
                           (name, key_id))
        except IntegrityError as exc:
            constraint = getattr(getattr(exc, 'diag', None), 'constraint_name', '') or ''
            if 'free_trial' in constraint:
                raise LlmError(ERR_LLM_FREE_KEY_EXISTS) from None
            raise LlmError(ERR_LLM_KEY_ALREADY_SHOWN) from None
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
        columns = sorted(values)
        if not all(_COLUMN_RE.fullmatch(column) for column in columns):
            raise ValueError('bad column name')
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute(
                'UPDATE %s SET %s WHERE id = %%s AND state = %%s'
                % (KEY_TABLE, ', '.join('%s = %%s' % column for column in columns)),
                [values[column] for column in columns] + [key_id, 'creating'])
