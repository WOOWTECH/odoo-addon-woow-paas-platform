"""AI 模型分頁的互動讀取快取：stale-while-revalidate（N26、DESIGN §3.2、§3.5；票 LLM-02）。

平台 Odoo 同時承載官網與 ERP，prod 只有 prefork ``workers=6``（〔REV〕50）：互動讀取的端點**不外呼**，
只回 PaaS 這邊的鏡像或快取並帶 ``data_as_of``（資料時間）；快取過期時一併回舊資料，同時排一次背景
刷新——由 cron（cron pod、CRON 金鑰、**該帳戶自己的 UP**）去讀 new-api、寫回快取。new-api 變慢或停擺時，
頁面照樣有東西看（標著資料時間），Odoo 的 HTTP worker 不會被拖住。

* **種類**（:data:`SWR_KINDS`）：

  ======================  =========  ======================================================  ==========
  kind                    TTL        背景刷新（cron）                                           端點
  ======================  =========  ======================================================  ==========
  ``self``                10 分鐘    UP ``GET /api/user/self`` → 帳戶的餘額快取（同餘額刷新的守衛）  ``state``
  ``tokens``              30 秒      UP ``GET /api/token/`` → 金鑰鏡像的用量、最後使用時間          ``keys/list``
  ``usage_recent``        60 秒      UP ``GET /api/log/self``（今天的免費試用）→ 快取的 DTO          ``usage/recent``
  ``pricing``             10 分鐘    AP ``GET /api/pricing`` → 公開 DTO（全平台一份；獨立 10 分鐘 cron）       ``models``
  ======================  =========  ======================================================  ==========

  ``usage/summary``、``usage/daily``（LLM-09）在這張表加一種 kind、寫一個 handler 就好。
* **排刷新**（:meth:`LlmSwrCache._llm_swr_touch`）：過期、還沒排、上一次嘗試也夠久了 ⇒ 以獨立 cursor
  upsert 一列 ``requested_at`` 並 ``_trigger()`` :data:`SWR_CRON_XMLID`。同一個帳戶同一種 kind 同時只會
  排一次；失敗之後至少隔 :data:`SWR_RETRY_SECONDS` 秒才再排（new-api 掛掉時頁面一直開著也不會洪水）。
  一般讀取不寫任何東西。
* **刷新 cron**（:meth:`LlmSwrCache._cron_llm_swr_refresh`，每 5 分鐘保險＋被 ``_trigger()``）：外呼類 cron，
  circuit 開著就跳過（DESIGN §3.6；半開時由它做健康檢查——web 在半開時也是叫醒它）；每 tick ≤ 30 秒、
  最多 :data:`SWR_PER_TICK` 筆（N28），以 ``FOR UPDATE SKIP LOCKED`` 搶列（兩條 cron thread 不會做同一筆）；
  價目由獨立 10 分鐘 cron 刷新；價目失敗清空，其他種類失敗只記 ``last_result``。
* **快取裡沒有秘密**：只有餘額數字、金鑰的用量與時間、用量 DTO（模型、token 數、request id）與價目。
  ACL 同其他 LLM model：system／帳務群組只讀；寫入一律走本檔的獨立 cursor SQL。
"""
import json
import logging
import re
import traceback
from datetime import datetime, timedelta, timezone

import psycopg2

from odoo import SUPERUSER_ID, _, api, fields, models
from odoo.exceptions import AccessError

from ..services import llm_crypto, llm_redact
from ..services.llm_pricing import PricingInvalid, normalize_pricing
from ..services import newapi_client as nac
from .billing_incident import _is_odoo_module_uninstall
from .llm_account import balance_stamp
from .llm_common import (
    LlmError,
    _is_llm_internal,
    _without_llm_internal,
    llm_config,
    run_llm_cron,
)

_logger = logging.getLogger(__name__)

CACHE_MODEL = 'woow_paas_platform.llm_swr_cache'
CACHE_TABLE = 'woow_paas_platform_llm_swr_cache'
ACCOUNT_MODEL = 'woow_paas_platform.llm_account'
KEY_TABLE = 'woow_paas_platform_llm_api_key'
SWR_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_swr_refresh'
PRICING_CRON_XMLID = 'woow_paas_platform.ir_cron_llm_pricing_refresh'
_LOCK_TIMEOUT = '5s'
TPE = timezone(timedelta(hours=8))

KIND_SELF = 'self'
KIND_TOKENS = 'tokens'
KIND_USAGE_RECENT = 'usage_recent'
KIND_PRICING = 'pricing'
#: kind → (TTL 秒, 範圍)。``account``＝每個帳戶一份；``global``＝全平台一份（``account_id`` 空）。
SWR_KINDS = {
    KIND_SELF: (600, 'account'),
    KIND_TOKENS: (30, 'account'),
    KIND_USAGE_RECENT: (60, 'account'),
    KIND_PRICING: (600, 'global'),
}
KIND_SELECTION = [
    (KIND_SELF, 'Account balance'),
    (KIND_TOKENS, 'API key list'),
    (KIND_USAGE_RECENT, 'Recent free trial calls'),
    (KIND_PRICING, 'Pricing'),
]
#: 一次嘗試（成功或失敗）之後，至少隔這麼久才再排同一筆。
SWR_RETRY_SECONDS = 30
#: 刷新 cron 一個 tick 最多做幾筆（N28；每筆一次 cron 讀取）。
SWR_PER_TICK = 20
#: 刷新可以寫回的帳戶狀態（provisioning／error／closed 沒有 UP 可用或不需要）。
SWR_ACCOUNT_STATES = ('active', 'suspended')
#: 一筆快取的 payload 上限（JSON 字元）：免費試用前 10 筆、一頁 100 把金鑰、價目都遠小於它。
_PAYLOAD_MAX = 512 * 1024


def _utcnow():
    return fields.Datetime.now()


def iso(value):
    """naive UTC → ISO 8601（帶 ``+00:00``）；``None`` → ``None``。"""
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def is_stale(fetched_at, kind, now=None):
    """``fetched_at``（naive UTC）比這種 kind 的 TTL 舊（或從來沒讀過）。"""
    if not fetched_at:
        return True
    now = now or _utcnow()
    return (now - fetched_at).total_seconds() > SWR_KINDS[kind][0]


def usage_recent_dto(data, names):
    """``GET /api/log/self`` 的回應 → ``usage/recent`` 的 DTO（只留白名單欄位，**剝掉 channel**）。

    ``names``：``newapi_name`` → ``(key id, kind)``（對回使用者看到的金鑰）。
    """
    data = data if isinstance(data, dict) else {}
    rows = data.get('items')
    rows = rows if isinstance(rows, list) else []
    items = []
    for row in rows[:10]:
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
            'key_id': key[0] if key else None,
            'key_kind': key[1] if key else None,
        })
    total = data.get('total')
    return {'today_count': total if type(total) is int else len(items), 'items': items}


class LlmSwrCache(models.Model):
    _name = 'woow_paas_platform.llm_swr_cache'
    _description = 'AI Models Read Cache (stale-while-revalidate)'
    _order = 'id'

    account_id = fields.Many2one(ACCOUNT_MODEL, ondelete='cascade', index=True, readonly=True,
                                 help='Empty for platform-wide kinds (pricing).')
    kind = fields.Selection(KIND_SELECTION, required=True, readonly=True)
    payload = fields.Text(readonly=True, help='JSON. Never contains keys, tokens or passwords.')
    fetched_at = fields.Datetime(readonly=True, help='When new-api was last read successfully.')
    requested_at = fields.Datetime(readonly=True, help='A background refresh is pending.')
    attempted_at = fields.Datetime(readonly=True)
    last_result = fields.Char(readonly=True, help='ok, or the classification of the last failure.')

    def init(self):
        self.env.cr.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS woow_llm_swr_cache_uniq '
            'ON %s ((COALESCE(account_id, 0)), kind)' % CACHE_TABLE)
        self.env.cr.execute(
            'CREATE INDEX IF NOT EXISTS woow_llm_swr_cache_pending '
            'ON %s (requested_at) WHERE requested_at IS NOT NULL' % CACHE_TABLE)

    # ------------------------------------------------------------------ #
    #  守衛（同其他 LLM model）                                             #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        if not _is_llm_internal(self.env):
            raise AccessError(_('The AI models cache is maintained by the platform.'))
        return _without_llm_internal(super().create(vals_list))

    def write(self, vals):
        if not _is_llm_internal(self.env):
            raise AccessError(_('The AI models cache is maintained by the platform.'))
        return super().write(vals)

    def unlink(self):
        if _is_odoo_module_uninstall(self.env):
            return super().unlink()
        raise AccessError(_('The AI models cache cannot be deleted.'))

    # ------------------------------------------------------------------ #
    #  讀（端點，不外呼）                                                    #
    # ------------------------------------------------------------------ #
    @api.model
    def _llm_swr_get(self, account, kind):
        """``{'payload', 'fetched_at', ...}`` 或 ``None``（還沒有這一列）。payload 解不開＝``None``。"""
        if kind not in SWR_KINDS:
            raise ValueError('llm_swr: unknown kind')
        account_id = account.id if account else False
        row = self.sudo().search([('account_id', '=', account_id), ('kind', '=', kind)], limit=1)
        if not row:
            return None
        payload = None
        if row.payload:
            try:
                payload = json.loads(row.payload)
            except ValueError:
                payload = None
        return {'payload': payload, 'fetched_at': row.fetched_at,
                'requested_at': row.requested_at, 'attempted_at': row.attempted_at,
                'last_result': row.last_result}

    @api.model
    def _llm_models_payload(self):
        """Public catalogue, available before activation; DB reads only, never AP or UP.

        Unlike usage history, expired/failed pricing must not advertise obsolete tariffs.
        This also rejects pre-LLM-06 raw cache rows until the next scheduled refresh.
        """
        empty = {'items': [], 'state': 'maintenance', 'data_as_of': None}
        state = self.env['woow_paas_platform.llm_runtime_state'].sudo().search([], limit=1)
        if not state or state.guard_tripped:
            return empty
        row = self._llm_swr_get(None, KIND_PRICING)
        if not row or row['last_result'] != 'ok' or is_stale(row['fetched_at'], KIND_PRICING):
            return empty
        payload = row['payload']
        if not isinstance(payload, dict) or payload.get('state') != 'ok' or not isinstance(payload.get('items'), list):
            return empty
        return {**payload, 'data_as_of': iso(row['fetched_at'])}

    @api.model
    def _llm_swr_touch(self, account, kind, fetched_at):
        """``fetched_at`` 過期就排一次背景刷新（見模組 docstring）；回傳「這一次有排」。

        盡力而為：DB 寫不進去（lock_timeout 之類）只記 log，端點照樣回舊資料。
        """
        ttl, scope = SWR_KINDS[kind]
        if scope == 'account' and not account:
            return False
        now = _utcnow()
        if not is_stale(fetched_at, kind, now):
            return False
        retry_before = now - timedelta(seconds=min(ttl, SWR_RETRY_SECONDS))
        account_id = account.id if scope == 'account' else None
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                cr.execute(
                    'INSERT INTO {t} (account_id, kind, requested_at, create_uid, create_date, '
                    'write_uid, write_date) VALUES (%s, %s, %s, %s, %s, %s, %s) '
                    'ON CONFLICT ((COALESCE(account_id, 0)), kind) DO UPDATE '
                    'SET requested_at = EXCLUDED.requested_at '
                    'WHERE {t}.requested_at IS NULL '
                    'AND ({t}.attempted_at IS NULL OR {t}.attempted_at < %s) '
                    'RETURNING id'.format(t=CACHE_TABLE),
                    (account_id, kind, now, SUPERUSER_ID, now, SUPERUSER_ID, now, retry_before))
                queued = cr.fetchone() is not None
        except psycopg2.Error as exc:
            _logger.warning('[LLM] swr %s: refresh not queued (pgcode=%s)', kind,
                            getattr(exc, 'pgcode', None))
            return False
        if queued:
            cron = self.env.ref(SWR_CRON_XMLID, raise_if_not_found=False)
            if cron:
                cron.sudo()._trigger()
        return queued

    # ------------------------------------------------------------------ #
    #  刷新 cron                                                           #
    # ------------------------------------------------------------------ #
    @api.model
    def _cron_llm_swr_refresh(self):
        """背景刷新互動讀取的快取；未設定（N27）時第一行就 return、零外呼。

        ⚠ **凍結契約**（同 ``llm_account._cron_llm_provision``）：``data/llm_cron.xml`` 是 noupdate，本方法的
        名字與所在的 model 不能改（``llm_common.LLM_CRONS``；``TestLlmCronContract`` 斷言 DB 裡的 code）。
        """
        return run_llm_cron(self.env, SWR_CRON_XMLID, self._llm_swr_refresh_tick,
                            items=SWR_PER_TICK, config=llm_config(self.env))

    @api.model
    def _cron_llm_pricing_refresh(self):
        """Every ten minutes, including before the first customer activates (N20/N27)."""
        return run_llm_cron(self.env, PRICING_CRON_XMLID, self._llm_pricing_refresh_tick,
                            items=1, config=llm_config(self.env))

    def _llm_pricing_refresh_tick(self, config, tick):
        # A dedicated schedule avoids the five-minute SWR sweep drifting to a fifteen-minute
        # price interval when its TTL comparison misses the exact ten-minute boundary.
        now = _utcnow()
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute(
                'INSERT INTO {t} (kind, attempted_at, create_uid, create_date, write_uid, write_date) '
                'VALUES (%s, %s, %s, %s, %s, %s) '
                'ON CONFLICT ((COALESCE(account_id, 0)), kind) DO UPDATE '
                'SET attempted_at = EXCLUDED.attempted_at, requested_at = NULL RETURNING id'
                .format(t=CACHE_TABLE), (KIND_PRICING, now, SUPERUSER_ID, now, SUPERUSER_ID, now))
            row_id = cr.fetchone()[0]
        try:
            client = self.env[ACCOUNT_MODEL].sudo()._llm_client(nac.ROLE_CRON)
            payload, label = self._llm_swr_refresh_one(
                client, config, None, KIND_PRICING, tick.deadline)
        except Exception:  # noqa: BLE001 - clear stale rates before cron framework alerts
            self._llm_swr_finish(row_id, None, 'internal')
            raise
        self._llm_swr_finish(row_id, payload, label)
        return False

    @api.model
    def _llm_swr_refresh_tick(self, config, tick):
        """一個 tick：搶待刷新的列 → 一筆一筆讀 new-api 寫回。回傳「還有剩」。"""
        client = None
        while tick.can_take(nac.MIN_CALL_SECONDS):
            claimed = self._llm_swr_claim(1)
            if not claimed:
                return False
            row_id, account_id, kind = claimed[0]
            tick.took()
            if client is None:
                try:
                    client = self.env[ACCOUNT_MODEL].sudo()._llm_client(nac.ROLE_CRON)
                except nac.NewApiError as exc:
                    _logger.error('[LLM] swr refresh skipped: %s', exc.code)
                    self._llm_swr_finish(row_id, None, 'config_%s' % exc.code)
                    return False
            try:
                payload, label = self._llm_swr_refresh_one(client, config, account_id, kind,
                                                           tick.deadline)
            except Exception:  # noqa: BLE001 - one row must not wedge the cron
                _logger.error('[LLM] swr %s account=%s crashed: %s', kind, account_id,
                              llm_redact.redact(traceback.format_exc()))
                payload, label = None, 'internal'
            if label == 'deadline':
                # 剩下的預算不夠一次呼叫：放回去，下一個 tick 再做。
                self._llm_swr_requeue(row_id)
                tick.more = True
                return True
            self._llm_swr_finish(row_id, payload, label)
        return self._llm_swr_pending()

    def _llm_swr_claim(self, limit):
        """獨立 cursor 搶 ``limit`` 筆待刷新的列並 commit（``FOR UPDATE SKIP LOCKED``）。"""
        now = _utcnow()
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute(
                'UPDATE {t} SET requested_at = NULL, attempted_at = %s, write_date = %s '
                'WHERE id IN (SELECT id FROM {t} WHERE requested_at IS NOT NULL '
                'ORDER BY requested_at, id FOR UPDATE SKIP LOCKED LIMIT %s) '
                'RETURNING id, account_id, kind'.format(t=CACHE_TABLE),
                (now, now, limit))
            return cr.fetchall()

    def _llm_swr_requeue(self, row_id):
        with self.env.registry.cursor() as cr:
            cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
            cr.execute('UPDATE %s SET requested_at = %%s, attempted_at = NULL WHERE id = %%s'
                       % CACHE_TABLE, (_utcnow(), row_id))

    def _llm_swr_pending(self):
        with self.env.registry.cursor() as cr:
            cr.execute('SELECT 1 FROM %s WHERE requested_at IS NOT NULL LIMIT 1' % CACHE_TABLE)
            return cr.fetchone() is not None

    def _llm_swr_finish(self, row_id, payload, label):
        """成功才發布新 payload；價目失敗清空，其他種類失敗只記 ``last_result``。"""
        label = (label or 'unknown')[:64]
        now = _utcnow()
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                if label == 'ok':
                    text = None if payload is None else json.dumps(
                        payload, ensure_ascii=True, separators=(',', ':'))
                    if text is not None and len(text) > _PAYLOAD_MAX:
                        cr.execute('UPDATE %s SET last_result = %%s, write_date = %%s '
                                   'WHERE id = %%s' % CACHE_TABLE, ('too_large', now, row_id))
                        return
                    cr.execute(
                        'UPDATE %s SET payload = COALESCE(%%s, payload), fetched_at = %%s, '
                        'last_result = %%s, write_date = %%s WHERE id = %%s' % CACHE_TABLE,
                        (text, now, 'ok', now, row_id))
                else:
                    # Fail closed for pricing: never retain privileged or obsolete tariffs
                    # after a failed refresh (usage/self/token SWR semantics stay unchanged).
                    cr.execute('UPDATE %s SET last_result = %%s, write_date = %%s, '
                               'payload = CASE WHEN kind = %%s THEN NULL ELSE payload END, '
                               'fetched_at = CASE WHEN kind = %%s THEN NULL ELSE fetched_at END '
                               'WHERE id = %%s' % CACHE_TABLE,
                               (label, now, KIND_PRICING, KIND_PRICING, row_id))
        except psycopg2.Error as exc:
            _logger.warning('[LLM] swr row=%s: result not written (pgcode=%s)', row_id,
                            getattr(exc, 'pgcode', None))

    # ------------------------------------------------------------------ #
    #  handlers                                                            #
    # ------------------------------------------------------------------ #
    def _llm_swr_refresh_one(self, client, config, account_id, kind, deadline):
        """讀一次 new-api → ``(payload 或 None, 'ok' 或失敗的 label)``。``'deadline'``＝沒送出。"""
        if kind == KIND_PRICING:
            result = client.ap_pricing(deadline=deadline, priority=nac.AP_PRIORITY_OTHER)
            if not result.ok:
                return None, self._label(result)
            try:
                return normalize_pricing(result.data), 'ok'
            except PricingInvalid:
                # A fixed error code only; raw upstream groups/models never enter alerts.
                from .llm_event import EVENT_MODEL, LlmAlert
                try:
                    self.env[EVENT_MODEL]._llm_alert(
                        LlmAlert.PRICING_INVALID, error_code='llm_newapi_invariant')
                except Exception:  # noqa: BLE001 - alert failure must not keep unsafe pricing
                    _logger.error('[LLM] pricing invariant alert could not be queued')
                return None, 'pricing_invalid'
        account = self.env[ACCOUNT_MODEL].sudo().browse(account_id).exists()
        if not account or account.state not in SWR_ACCOUNT_STATES or not account.newapi_user_id:
            return None, 'skipped'
        try:
            ip = account._llm_tenant_ip(config)
            up = account._llm_up_token(role=nac.ROLE_CRON)
        except (LlmError, llm_crypto.LlmCryptoError):
            return None, 'up_unavailable'
        if kind == KIND_SELF:
            read_at = balance_stamp()
            result = client.user_self(ip, up, deadline=deadline)
            if not result.ok:
                return None, self._label(result, account)
            data = result.data if isinstance(result.data, dict) else {}
            # 同餘額刷新的檢查（名稱、id、整數）＋寫回守衛（只寫比這次讀取舊的快取）。
            row = self.env[ACCOUNT_MODEL]._llm_balance_rows([{
                'id': data.get('id'), 'username': data.get('username'),
                'quota': data.get('quota'), 'used_quota': data.get('used_quota'),
                'request_count': data.get('request_count'), 'DeletedAt': None,
            }], re.compile(re.escape(account.newapi_username), re.ASCII))
            if not row or row[0][1] != account.newapi_user_id:
                return None, 'shape'
            if self.env[ACCOUNT_MODEL]._llm_store_balances(row, read_at) is None:
                return None, 'db'
            return None, 'ok'
        if kind == KIND_TOKENS:
            result = client.user_list_tokens(ip, up, 1, 100, timeout=nac.TIMEOUT_CRON_READ,
                                             deadline=deadline)
            if not result.ok:
                return None, self._label(result, account)
            data = result.data if isinstance(result.data, dict) else {}
            items = data.get('items')
            if not isinstance(items, list):
                return None, 'shape'
            if not self._llm_store_token_usage(account, items):
                return None, 'db'
            return None, 'ok'
        if kind == KIND_USAGE_RECENT:
            now = datetime.now(TPE)
            start = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            result = client.user_log_self(ip, up, {
                'type': 2, 'group': 'free', 'start_timestamp': start, 'p': 1, 'page_size': 10,
            }, timeout=nac.TIMEOUT_CRON_READ, deadline=deadline)
            if not result.ok:
                return None, self._label(result, account)
            names = {k.newapi_name: (k.id, k.kind) for k in account.api_key_ids if k.newapi_name}
            dto = usage_recent_dto(result.data, names)
            dto['day_tpe'] = now.date().isoformat()
            return dto, 'ok'
        return None, 'unknown_kind'

    @staticmethod
    def _label(result, account=None):
        if isinstance(result, nac.NotExecuted) and result.reason == 'deadline':
            return 'deadline'
        if account is not None and isinstance(result, nac.Definite) and result.code in (
                'AUTH_UNAUTHORIZED', 'ACCESS_TOKEN_EXPIRED'):
            account._llm_mark_up_invalid()
        return ('%s:%s' % (result.kind, result.label()))[:64]

    def _llm_store_token_usage(self, account, items):
        """金鑰清單 → 鏡像的 ``remain_quota_cached``／``used_quota_cached``／``accessed_at``／``synced_at``。

        只寫本帳戶、``newapi_token_id`` 與名稱都對得上的列（UP 只看得到本人的 token；鏡像歸屬是第二道）。
        狀態（停用、刪除）的對回在 LLM-07。
        """
        mirrors = {(k.newapi_token_id, k.newapi_name): k.id for k in account.api_key_ids
                   if k.newapi_token_id and k.newapi_name}
        now = _utcnow()
        rows = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key_id = mirrors.get((item.get('id'), item.get('name')))
            remain, used, accessed = (item.get('remain_quota'), item.get('used_quota'),
                                      item.get('accessed_time'))
            if key_id is None or not all(type(v) is int for v in (remain, used)):
                continue
            accessed_at = (datetime.fromtimestamp(accessed, tz=timezone.utc).replace(tzinfo=None)
                           if type(accessed) is int and accessed > 0 else None)
            rows.append((key_id, remain, used, accessed_at))
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = %s", (_LOCK_TIMEOUT,))
                for key_id, remain, used, accessed_at in rows:
                    cr.execute(
                        'UPDATE %s SET remain_quota_cached = %%s, used_quota_cached = %%s, '
                        'accessed_at = COALESCE(%%s, accessed_at), synced_at = %%s, '
                        'write_uid = %%s, write_date = %%s WHERE id = %%s AND account_id = %%s'
                        % KEY_TABLE,
                        (remain, used, accessed_at, now, SUPERUSER_ID, now, key_id, account.id))
        except psycopg2.Error as exc:
            _logger.warning('[LLM] swr tokens account=%s: write failed (pgcode=%s)', account.id,
                            getattr(exc, 'pgcode', None))
            return False
        return True
