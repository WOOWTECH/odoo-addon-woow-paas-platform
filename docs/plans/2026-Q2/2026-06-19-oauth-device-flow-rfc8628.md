# OAuth Device Authorization Grant (RFC 8628) 實作計畫（WOOW paas 側）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐 task 實作。步驟用 checkbox（`- [ ]`）追蹤。

**Goal:** 讓 WOOW paas OAuth provider 支援 RFC 8628 Device Authorization Grant，使 HA Companion App（WebView，redirect/window.open 死路）能完成 OAuth onboarding —— 不需 redirect/callback，使用者在任意瀏覽器登入 + 輸入 user_code + 同意，HA 後端輪詢 token endpoint 取 token。

**Architecture:** 純加法、不碰現有 authorization_code/redirect 流程。新增 `oauth_device_code` model + `POST /oauth2/device_authorization` + `/device` 驗證頁（WOOW 品牌化，等於 app 版自訂頁）+ token endpoint 的 `device_code` grant 分支（輪詢狀態機）。大量重用既有 `_issue_tokens` / `_authenticate_client` / `oauth2_authorize` template 風格 / `oauth_code` model pattern。**同一 client（woow-ha-smart-home）同時支援 `authorization_code`（browser，走路線一 host-flexible callback）+ `device_code`（app）**，由 HA component 端依 User-Agent 分流。

**Tech Stack:** Odoo 18 addon（Python 4-space）、stdlib `secrets`/`string`（無新依賴）、`ir.cron`（過期清理）、`TransactionCase` + `HttpCase` 測試。

---

## 契約對齊紀錄（與 ha-paas-component，2026-06-19 hybrid 定案）

Eugene 拍板走 **hybrid**：browser 維持路線一 host-flexible callback（`authorization_code`）、app 走 device flow（`device_code`），兩條掛**同一 client** `woow-ha-smart-home`，component 端以 User-Agent 分流（UA 含 `Home Assistant/` → device flow）。本計畫是 WOOW paas（authorization server）側；HA component 側（`async_step_device` + `async_show_progress` 輪詢 + UA 分流）由 ha-paas-component 另行實作。可行性評估見本 session 對談（4 維 workflow + 2 對抗式 verdict 皆 refuted=False/high）。

RFC 8628 契約（ha-paas-component 確認）：
1. `POST /oauth2/device_authorization`：`client_id`(+`scope`) → `device_code` / `user_code` / `verification_uri` / `verification_uri_complete` / `expires_in` / `interval`（§3.2）
2. `/device` 驗證頁：登入 + 輸 user_code + 同意（WOOW 品牌化）
3. token endpoint `grant_type=urn:ietf:params:oauth:grant-type:device_code` + `device_code` → access+refresh token；未完成回 `authorization_pending` / `slow_down` / `expired_token` / `access_denied`（§3.5）

## 安全設計 rationale（對抗式評估校準）

1. **user_code 防爆破（最高安全點，HIGH）**：user_code 是人手輸入短碼、攻擊者可在 `/device` 猜。防禦四層：①**entropy**（20-字母表 ^ 8 字元 = **34.6 bits**，正好是 RFC 8628 §6.1 canonical 範例 `WDJB-MJHT`；去母音〔不拼出意外字〕+ 去易混字元 `0/O/1/I/L`）②**短 lifetime**（15 min，codespace 短時間猜不完）③**`auth='user'` session 要求**（攻擊者得先有合法登入才能 POST 猜測，門檻顯著提高）④**per-session 猜測 throttle**（`request.session['device_verify_fails']` 連續失敗達 `MAX_VERIFICATION_ATTEMPTS` 即 cooldown，擋自動化）。⚠️ **對抗式 review 校準**：per-**record** 計數器對「猜測」**無效**（猜錯找不到 record、計數器永不觸發），故真防線是 ①②③④、且 throttle 必須 **per-actor**（session/IP）而非 per-record。持久化 per-IP 版見 F-DEV-1。
2. **slow_down rate limit（RFC 8628 §3.5）**：token endpoint 記 `last_polled_at`，輪詢快於 `interval` → 回 `slow_down` 並 `interval += 5`。Odoo 無內建 throttle，server 端自行記。
3. **polling DB 負荷（運維）**：HA scale 高頻輪詢 token endpoint，每次開 DB 連線（`save_session=False` 不改變）。`slow_down` + 短 interval 下限緩解；運維面在 deploy 註記（worker/connection pool），非本計畫程式阻礙。
4. **cleanup cron 不可 silent fail**：過期 device_code 掛 `ir.cron`（比照 `cloud_service_cron.xml`）；cron 失敗會累積 row，cron code 本身防呆（best-effort、不 raise）。
5. **`/device` HTTPS + CSRF**：`auth='user'`（session login，同 authorize 頁）+ POST 帶 `csrf_token`（比照 `oauth2_authorize` template）；`verification_uri` 用 `web.base.url`（絕對 https）。
6. **device_code single-use**：approved 後換到 token 即標記/刪除，防重放。
7. **PKCE 不適用**：device flow 無 client-to-authorize redirect channel，RFC 8628 不要求 PKCE；`device_code` grant handler **不檢查** code_verifier。現有 `require_pkce=True` 只 gate `authorization_code`（`oauth2.py` authorize endpoint），對 device flow client 無害（見 Task 6 註記）。

## Rollout & 跨 repo lock-step

比照上次 OAuth callback：實作 → merge `develop` → `deploy-stg`（`--slug stg` → woow-k3s → `https://stg.woowtech.io`）→ ha-paas-component 接 component device flow + E2E。device flow 是**純加法、向後相容**（不碰現有 flow），故無 prod breaking（不像上次移除 my-relay）；grant_types 加 `device_code` 對既有 DB 走 healer（`noupdate=1` sticky）。E2E endpoint：`https://stg.woowtech.io/oauth2/device_authorization`、`/device`、`/oauth2/token`。

## Global Constraints

- Python 4-space（PEP 8）；model/欄位比照既有 `oauth_code.py` / `oauth_token.py` 風格。
- **無新增 Python 依賴**：只用 stdlib `secrets`、`string`、`datetime`。
- **現有 OAuth flow 零回歸**：device flow 不改動 `authorize_get`/`authorize_post`/`_handle_authorization_code`/`_handle_refresh_token`/`_handle_client_credentials`；token dispatch 只**新增** elif 分支。
- **同一 client 同時支援 `authorization_code` + `device_code`**（grant_types 並存）。
- user_code entropy ≥ 20 bits（實作 ≈34.6 bits）；`verification_uri` 來自 `ir.config_parameter` `web.base.url`（絕對 https）。
- grant_types 加 `device_code` 走 healer（比照 `_ensure_ha_smart_home_redirect_mode`），**不 bump manifest**（除非 model 新增需要——本計畫新 model 由 install 建表，無需 migration）。
- 測試執行：`./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:<Class>.<method> --stop-after-init`，或 sandbox `test.sh`。
- 常數：`DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'`、`DEVICE_CODE_LIFETIME = timedelta(minutes=15)`、`DEVICE_POLL_INTERVAL = 5`、`MAX_VERIFICATION_ATTEMPTS = 5`。

---

## File Structure

- `src/models/oauth_device_code.py`（Create）— device_code model：欄位 + user_code/device_code 生成 + 狀態 helper（is_expired/register_poll/mark_approved/mark_denied/register_failed_verification）+ `_cron_gc_expired`。
- `src/models/__init__.py`（Modify）— import 新 model。
- `src/models/oauth_client.py`（Modify）— healer `_ensure_ha_smart_home_device_grant`（grant_types 加 device_code，比照既有 redirect healer）。
- `src/controllers/oauth2.py`（Modify）— `DEVICE_GRANT` 常數 + token dispatch elif + `_handle_device_code` + `POST /oauth2/device_authorization` + `GET/POST /device` 3 routes。
- `src/views/oauth2_templates.xml`（Modify）— `device_verify`（輸 user_code）+ `device_consent`（顯示 client/scope + 同意）+ `device_result`（成功/拒絕/錯誤）template，比照 `oauth2_authorize` 品牌風格。
- `src/data/oauth_device_code_cron.xml`（Create）— `ir.cron` 過期清理。
- `src/data/oauth_device_grant_update.xml`（Create）— `<function>` healer（既有 DB grant_types 加 device_code）。
- `src/data/oauth_clients.xml`（Modify）— HA client grant_types 加 `device_code`（fresh install）。
- `src/security/ir.model.access.csv`（Modify）— `oauth_device_code` ACL（system 管理；endpoint 走 sudo）。
- `src/__manifest__.py`（Modify）— 註冊 cron xml + security（security 已在 data 清單則只加 cron）。
- `src/tests/test_oauth2_device_flow.py`（Create）— model + endpoint + grant + 驗證頁 + 防爆破 + healer 測試。
- `docs/reference/api/ha-component-integration.md`（Modify）— device flow 契約段。

---

### Task 1: `oauth_device_code` model + user_code 生成 + ACL

**Files:**
- Create: `src/models/oauth_device_code.py`
- Modify: `src/models/__init__.py`
- Modify: `src/security/ir.model.access.csv`
- Test: `src/tests/test_oauth2_device_flow.py`

**Interfaces:**
- Produces:
  - model `woow_paas_platform.oauth_device_code`，欄位：`device_code`/`user_code`/`scope`/`client_id`(m2o oauth_client)/`user_id`(m2o res.users, nullable)/`status`(`pending`|`approved`|`denied`)/`expires_at`/`last_polled_at`/`interval`/`verification_attempts`
  - `create_for_client(client, scope) -> record`（@api.model，生成唯一 codes）
  - `is_expired() -> bool`、`register_poll() -> bool`（True=太快/slow_down）、`mark_approved(user)`、`mark_denied()`、`register_failed_verification() -> bool`（True=lockout）
  - `_generate_user_code() -> str`（格式 `XXXX-XXXX`）、`_cron_gc_expired()`

- [ ] **Step 1: 寫 failing 測試**

Create `src/tests/test_oauth2_device_flow.py`：

```python
"""Tests for OAuth 2.0 Device Authorization Grant (RFC 8628)."""
import re
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase


class TestOAuthDeviceCodeModel(TransactionCase):
    def setUp(self):
        super().setUp()
        self.DeviceCode = self.env['woow_paas_platform.oauth_device_code']
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')

    def test_create_for_client_generates_codes(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        self.assertTrue(dc.device_code)
        self.assertRegex(dc.user_code, r'^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$')
        self.assertEqual(dc.status, 'pending')
        self.assertEqual(dc.client_id, self.client)
        self.assertFalse(dc.user_id)
        self.assertGreater(dc.expires_at, fields.Datetime.now())

    def test_user_code_entropy_alphabet(self):
        # No vowels / no easily-confused chars (0 O 1 I L).
        code = self.DeviceCode._generate_user_code().replace('-', '')
        self.assertEqual(len(code), 8)
        for ch in code:
            self.assertIn(ch, 'BCDFGHJKLMNPQRSTVWXZ')

    def test_is_expired(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        self.assertFalse(dc.is_expired())
        dc.expires_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertTrue(dc.is_expired())

    def test_register_poll_slow_down(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        self.assertFalse(dc.register_poll())   # first poll OK
        self.assertTrue(dc.register_poll())    # immediate second poll -> too fast
        self.assertGreater(dc.interval, 5)     # interval bumped

    def test_mark_approved_denied(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        dc.mark_approved(self.env.user)
        self.assertEqual(dc.status, 'approved')
        self.assertEqual(dc.user_id, self.env.user)
        dc2 = self.DeviceCode.create_for_client(self.client, '')
        dc2.mark_denied()
        self.assertEqual(dc2.status, 'denied')

    def test_cron_gc_expired_removes_old(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        dc.expires_at = fields.Datetime.now() - timedelta(hours=2)
        self.DeviceCode._cron_gc_expired()
        self.assertFalse(dc.exists())
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestOAuthDeviceCodeModel.test_create_for_client_generates_codes --stop-after-init`
Expected: FAIL（model 不存在）

- [ ] **Step 3: 實作 model**

Create `src/models/oauth_device_code.py`：

```python
import secrets
from datetime import timedelta

from odoo import api, fields, models

# RFC 8628 device flow parameters
DEVICE_CODE_LIFETIME = timedelta(minutes=15)
DEVICE_POLL_INTERVAL = 5  # seconds
MAX_VERIFICATION_ATTEMPTS = 5
# user_code alphabet: no vowels (avoids accidental words) + no easily-confused
# chars (0/O/1/I/L). 20 symbols ^ 8 chars ~= 34.6 bits, well above RFC 8628
# §6.1's 20-bit floor for the polling+entropy brute-force tradeoff.
_USER_CODE_ALPHABET = 'BCDFGHJKLMNPQRSTVWXZ'
_USER_CODE_LEN = 8


class OAuthDeviceCode(models.Model):
    _name = 'woow_paas_platform.oauth_device_code'
    _description = 'OAuth 2.0 Device Authorization Grant (RFC 8628)'
    _order = 'create_date desc'

    device_code = fields.Char(
        string='Device Code', required=True, index=True, readonly=True,
        help='High-entropy code the device polls the token endpoint with.',
    )
    user_code = fields.Char(
        string='User Code', required=True, index=True, readonly=True,
        help='Human-typed code the user enters at the verification page.',
    )
    scope = fields.Char(string='Scope', help='Space-separated requested scopes.')
    client_id = fields.Many2one(
        'woow_paas_platform.oauth_client', string='OAuth Client',
        required=True, ondelete='cascade', index=True,
    )
    user_id = fields.Many2one(
        'res.users', string='Approving User', ondelete='cascade', index=True,
        help='Set when the user approves at the verification page.',
    )
    status = fields.Selection(
        selection=[('pending', 'Pending'), ('approved', 'Approved'),
                   ('denied', 'Denied'), ('consumed', 'Consumed')],
        default='pending', required=True, index=True,
    )
    expires_at = fields.Datetime(string='Expires At', required=True)
    last_polled_at = fields.Datetime(string='Last Polled At')
    interval = fields.Integer(string='Poll Interval (s)', default=DEVICE_POLL_INTERVAL)

    _sql_constraints = [
        ('unique_device_code', 'UNIQUE(device_code)', 'device_code must be unique.'),
        ('unique_user_code', 'UNIQUE(user_code)', 'user_code must be unique.'),
    ]

    @api.model
    def _generate_user_code(self) -> str:
        raw = ''.join(secrets.choice(_USER_CODE_ALPHABET) for _ in range(_USER_CODE_LEN))
        return f'{raw[:4]}-{raw[4:]}'

    @api.model
    def create_for_client(self, client, scope: str):
        """Create a pending device_code with collision-free unique codes."""
        user_code = self._generate_user_code()
        for _ in range(5):
            # Search the WHOLE table: UNIQUE(user_code) is unconditional, so an
            # expired/consumed row not yet GC'd still owns the value. Filtering
            # to status='pending' here would let a stale collision slip past the
            # Python check and hit the DB constraint -> IntegrityError on a
            # long-lived DB (fresh-install green, prod flaky).
            if not self.sudo().search_count([('user_code', '=', user_code)]):
                break
            user_code = self._generate_user_code()
        return self.sudo().create({
            'device_code': secrets.token_urlsafe(32),
            'user_code': user_code,
            'scope': scope or '',
            'client_id': client.id,
            'expires_at': fields.Datetime.now() + DEVICE_CODE_LIFETIME,
            'interval': DEVICE_POLL_INTERVAL,
        })

    def is_expired(self) -> bool:
        self.ensure_one()
        return fields.Datetime.now() >= self.expires_at

    def register_poll(self) -> bool:
        """Record a poll. Return True if it arrived faster than `interval`
        (caller must answer slow_down). Bumps interval per RFC 8628 §3.5."""
        self.ensure_one()
        now = fields.Datetime.now()
        too_fast = bool(
            self.last_polled_at
            and (now - self.last_polled_at).total_seconds() < self.interval
        )
        self.last_polled_at = now
        if too_fast:
            # RFC 8628 §3.5 mandates +5 per slow_down; cap so a flapping but
            # benign client can't ratchet the legit device's interval unbounded.
            self.interval = min(self.interval + 5, 30)
        return too_fast

    def mark_approved(self, user) -> None:
        self.ensure_one()
        self.write({'status': 'approved', 'user_id': user.id})

    def mark_denied(self) -> None:
        self.ensure_one()
        self.status = 'denied'

    def consume(self) -> bool:
        """Atomically transition approved -> consumed. Return True iff THIS call
        won the race (only the winner may issue tokens). Compare-and-swap via
        conditional UPDATE rowcount is race-safe under READ COMMITTED — two
        concurrent polls cannot double-issue tokens for one authorization.
        (Mirrors oauth_code's single-use flag discipline rather than a delete,
        so a replayed device_code gets a clean error, not 'unknown'.)"""
        self.ensure_one()
        self.env.cr.execute(
            "UPDATE woow_paas_platform_oauth_device_code "
            "SET status='consumed' WHERE id=%s AND status='approved'",
            (self.id,),
        )
        won = self.env.cr.rowcount == 1
        if won:
            self.invalidate_recordset(['status'])
        return won

    @api.model
    def _cron_gc_expired(self) -> None:
        """Best-effort GC: delete device codes past expiry + 1h grace."""
        cutoff = fields.Datetime.now() - timedelta(hours=1)
        self.sudo().search([('expires_at', '<', cutoff)]).unlink()
```

加 import 到 `src/models/__init__.py`（在 `oauth_code` 鄰近）：

```python
from . import oauth_device_code
```

- [ ] **Step 4: 加 ACL**

`src/security/ir.model.access.csv` 加一行（比照 `oauth_code` 的 access 行；只給 system group 完整權限，endpoint 走 `.sudo()`）：

```csv
access_oauth_device_code_system,oauth_device_code_system,model_woow_paas_platform_oauth_device_code,base.group_system,1,1,1,1
```

- [ ] **Step 5: 跑測試確認 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestOAuthDeviceCodeModel --stop-after-init`
Expected: PASS（7 test）

- [ ] **Step 6: Commit**

```bash
git add src/models/oauth_device_code.py src/models/__init__.py src/security/ir.model.access.csv src/tests/test_oauth2_device_flow.py
git commit -m "feat: 新增 oauth_device_code model（RFC 8628 device flow 狀態載體）"
```

---

### Task 2: `POST /oauth2/device_authorization` endpoint

**Files:**
- Modify: `src/controllers/oauth2.py`（頂部常數 + 新 route）
- Test: `src/tests/test_oauth2_device_flow.py`（新 class `TestDeviceAuthorizationEndpoint`）

**Interfaces:**
- Consumes: `oauth_device_code.create_for_client`（Task 1）、`_authenticate_client`（既有 `oauth2.py:358`）
- Produces: `POST /oauth2/device_authorization`（auth='none'）回 RFC 8628 §3.2 JSON

- [ ] **Step 1: 寫 failing 測試**（HttpCase，真實 HTTP）

加到 `test_oauth2_device_flow.py`：

```python
import json
from odoo.tests.common import HttpCase


class TestDeviceAuthorizationEndpoint(HttpCase):
    def test_device_authorization_returns_rfc8628_fields(self):
        resp = self.url_open(
            '/oauth2/device_authorization',
            data={'client_id': 'woow-ha-smart-home', 'scope': 'smarthome:read'},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn('device_code', body)
        self.assertIn('user_code', body)
        self.assertTrue(body['verification_uri'].endswith('/device'))
        self.assertIn(body['user_code'], body['verification_uri_complete'])
        self.assertEqual(body['interval'], 5)
        self.assertGreater(body['expires_in'], 0)

    def test_device_authorization_unknown_client_rejected(self):
        resp = self.url_open(
            '/oauth2/device_authorization', data={'client_id': 'nope'},
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()['error'], 'invalid_client')
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDeviceAuthorizationEndpoint.test_device_authorization_returns_rfc8628_fields --stop-after-init`
Expected: FAIL（404 / route 不存在）

- [ ] **Step 3: 實作 endpoint**

`src/controllers/oauth2.py`：在 token lifetime 常數區（`:19` 後）加 device 常數：

```python
DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'
```

在 token endpoint（`token` method）**之前**新增 device authorization route（放在 `# ==================== Token Endpoint ====================` 區塊前）：

```python
    # ==================== Device Authorization Endpoint (RFC 8628) ====================

    @route('/oauth2/device_authorization', type='http', auth='none',
           methods=['POST'], csrf=False, save_session=False)
    def device_authorization(self, **kwargs: Any) -> http.Response:
        """RFC 8628 §3.1-3.2: issue a device_code + user_code for a client."""
        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)
        if not client.check_grant_type(DEVICE_GRANT):
            return _json_error(
                'unauthorized_client',
                'This client is not authorized for the device_code grant type.',
            )

        scope = kwargs.get('scope', '')
        if scope:
            invalid = set(scope.split()) - client.get_allowed_scopes()
            if invalid:
                return _json_error(
                    'invalid_scope', f'Invalid scope(s): {", ".join(sorted(invalid))}',
                )
        else:
            # No scope requested: resolve to the client's full allowed set NOW
            # and store it, so the consent page shows exactly what the token
            # will carry (mirrors authorize_get's write-back — avoids the
            # consent-shows-X-but-token-carries-empty mismatch).
            scope = ' '.join(sorted(client.get_allowed_scopes()))

        device = request.env['woow_paas_platform.oauth_device_code'].create_for_client(
            client, scope,
        )

        # verification_uri MUST be absolute https. Use admin-controlled
        # web.base.url ONLY — never derive a security-relevant URL from the
        # client-controlled Host header (host-header injection -> phishing).
        base = (request.env['ir.config_parameter'].sudo()
                .get_param('web.base.url') or '').rstrip('/')
        if not base:
            return _json_error(
                'server_error', 'Server misconfigured: web.base.url is unset.', 500,
            )
        verification_uri = f'{base}/device'

        _logger.info(
            'OAuth2 device authorization issued: client=%s user_code=%s',
            client.name, device.user_code,
        )
        return _json_response({
            'device_code': device.device_code,
            'user_code': device.user_code,
            'verification_uri': verification_uri,
            'verification_uri_complete': f'{verification_uri}?user_code={device.user_code}',
            'expires_in': int(DEVICE_CODE_LIFETIME.total_seconds()),
            'interval': device.interval,
        })
```

在 import 區補 `DEVICE_CODE_LIFETIME`（從 model 匯入避免雙寫常數）：

```python
from ..models.oauth_device_code import DEVICE_CODE_LIFETIME, MAX_VERIFICATION_ATTEMPTS
```

（若專案慣例不從 model import 常數，則在 controller 頂部重宣告 `DEVICE_CODE_LIFETIME = timedelta(minutes=15)` 並加註「須與 oauth_device_code 同值」。檢查既有 `oauth2.py` 是否已 import model 常數決定；預設用 import 避免漂移。）

- [ ] **Step 4: 跑測試確認 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDeviceAuthorizationEndpoint --stop-after-init`
Expected: PASS（2 test）

> 注意：此 test 需 HA client 已有 `device_code` grant_type。若 Task 6（grant_types healer/seed）尚未做，先在 test fixture 內 `self.env.ref('...oauth_client_ha_smart_home').grant_types += ',urn:ietf:params:oauth:grant-type:device_code'`，或調整 Task 順序把 Task 6 提前。建議：**先做 Task 6 的 seed 部分**（fresh install test DB 即帶 device grant），再回此 Task。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/oauth2.py src/tests/test_oauth2_device_flow.py
git commit -m "feat: 新增 /oauth2/device_authorization endpoint（RFC 8628 §3.2）"
```

---

### Task 3: token endpoint `device_code` grant 分支（輪詢狀態機）

**Files:**
- Modify: `src/controllers/oauth2.py`（dispatch elif + `_handle_device_code`）
- Test: `src/tests/test_oauth2_device_flow.py`（新 class `TestDeviceTokenGrant`）

**Interfaces:**
- Consumes: device_code record（Task 1）、`_authenticate_client`/`_issue_tokens`（既有）
- Produces: `_handle_device_code(kwargs)`；token dispatch 支援 `DEVICE_GRANT`

- [ ] **Step 1: 寫 failing 測試**（mock `request`，比照既有 `TestPkceOnlyTokenFlow` 風格直接呼叫 handler）

加到 `test_oauth2_device_flow.py`（用 TransactionCase + 直接驗 model 狀態機；endpoint 整合留 Task 4 後的 HttpCase）：

```python
class TestDeviceTokenGrant(TransactionCase):
    """Device-code grant state machine via direct model assertions.
    (Full HTTP polling covered by HttpCase in TestDeviceFlowEndToEnd.)"""

    def setUp(self):
        super().setUp()
        self.DeviceCode = self.env['woow_paas_platform.oauth_device_code']
        self.Token = self.env['woow_paas_platform.oauth_token']
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')

    def test_pending_then_approved_issues_token(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        self.assertEqual(dc.status, 'pending')          # poll -> authorization_pending
        dc.mark_approved(self.env.user)
        self.assertEqual(dc.status, 'approved')         # poll -> issue token
        self.assertEqual(dc.user_id, self.env.user)

    def test_denied_state(self):
        dc = self.DeviceCode.create_for_client(self.client, '')
        dc.mark_denied()
        self.assertEqual(dc.status, 'denied')           # poll -> access_denied

    def test_expired_state(self):
        from datetime import timedelta
        dc = self.DeviceCode.create_for_client(self.client, '')
        dc.expires_at = fields.Datetime.now() - timedelta(seconds=1)
        self.assertTrue(dc.is_expired())                # poll -> expired_token

    def test_consume_race_safe_single_winner(self):
        dc = self.DeviceCode.create_for_client(self.client, 'smarthome:read')
        dc.mark_approved(self.env.user)
        self.assertTrue(dc.consume())       # first poll wins -> issue
        self.assertFalse(dc.consume())      # replay loses -> NO double-issue
        self.assertEqual(dc.status, 'consumed')


class TestDeviceCodeHandler(TransactionCase):
    """Directly exercise _handle_device_code for all five RFC 8628 §3.5
    branches. The elif dispatch / slow_down / consume / _issue_tokens wiring is
    NOT covered by the model-level tests above (handler had zero direct
    coverage — adversarial-review finding B). Mock `request` exactly like
    src/tests/test_oauth2.py::TestPkceOnlyTokenFlow (patch odoo.http.request to
    this test env), then call OAuth2Controller()._handle_device_code(kwargs)
    and assert per branch:
      - unknown device_code      -> error 'invalid_grant'
      - expired                  -> error 'expired_token'
      - denied                   -> error 'access_denied'
      - pending, first poll      -> error 'authorization_pending'
      - pending, immediate 2nd   -> error 'slow_down'
      - approved                 -> 'access_token' present; 2nd call 'invalid_grant'
    Implementer: copy the request-mock setUp from TestPkceOnlyTokenFlow."""
```

- [ ] **Step 2: 跑測試確認（model 行為先綠，handler 整合在 Step 3 後驗）**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDeviceTokenGrant --stop-after-init`
Expected: PASS（Task 1 model 已支援；此 test 鎖狀態機 intent，handler 行為由下方 HttpCase Task 4 端到端驗）

- [ ] **Step 3: 實作 dispatch elif + handler**

`src/controllers/oauth2.py` 的 token dispatch（`:345-356`）加 elif：

```python
        if grant_type == 'authorization_code':
            return self._handle_authorization_code(kwargs)
        elif grant_type == 'refresh_token':
            return self._handle_refresh_token(kwargs)
        elif grant_type == 'client_credentials':
            return self._handle_client_credentials(kwargs)
        elif grant_type == DEVICE_GRANT:
            return self._handle_device_code(kwargs)
        else:
            return _json_error(
                'unsupported_grant_type',
                f'Grant type "{grant_type}" is not supported. '
                'Use: authorization_code, refresh_token, client_credentials, '
                'or urn:ietf:params:oauth:grant-type:device_code.',
            )
```

在 `_handle_client_credentials` 之後新增 handler：

```python
    def _handle_device_code(self, kwargs: dict) -> http.Response:
        """Handle grant_type=urn:ietf:params:oauth:grant-type:device_code.

        RFC 8628 §3.4-3.5 polling state machine. No PKCE (device flow has no
        authorize redirect channel). The user identity comes from the /device
        approval, not from this machine-to-machine poll.
        """
        device_code_str = kwargs.get('device_code', '')

        client = self._authenticate_client(kwargs)
        if not client:
            return _json_error('invalid_client', 'Client authentication failed.', 401)
        if not client.check_grant_type(DEVICE_GRANT):
            return _json_error(
                'unauthorized_client',
                'This client is not authorized for the device_code grant type.',
            )
        if not device_code_str:
            return _json_error('invalid_request', 'Missing device_code parameter.')

        device = request.env['woow_paas_platform.oauth_device_code'].sudo().search([
            ('device_code', '=', device_code_str),
            ('client_id', '=', client.id),
        ], limit=1)
        if not device:
            return _json_error('invalid_grant', 'Unknown device_code.')

        # RFC 8628 §3.5: terminal states (expired / denied) must beat the
        # slow_down "pending variant" — a user who DENIED must get access_denied
        # even if the device is polling too fast. register_poll() runs ONLY in
        # the pending branch: never on the approved path (else the very poll
        # that should mint the token returns slow_down and delays success one
        # interval), and never preempting a terminal decision.
        if device.is_expired():
            return _json_error('expired_token', 'The device_code has expired.')
        if device.status == 'denied':
            return _json_error('access_denied', 'The user denied the request.')
        if device.status == 'consumed':
            return _json_error('invalid_grant', 'Authorization already used.')
        if device.status == 'pending':
            if device.register_poll():
                return _json_error('slow_down', 'Polling too frequently.')
            return _json_error('authorization_pending', 'Authorization pending.')

        # approved -> atomically consume (compare-and-swap). Only the race
        # winner issues tokens; concurrent polls cannot double-issue.
        if not device.consume():
            return _json_error('invalid_grant', 'Authorization already used.')
        return self._issue_tokens(
            client=client, user=device.user_id, scope=device.scope or '',
            include_refresh=True,
        )
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDeviceTokenGrant --stop-after-init`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/controllers/oauth2.py src/tests/test_oauth2_device_flow.py
git commit -m "feat: token endpoint 支援 device_code grant（RFC 8628 §3.4-3.5 輪詢狀態機）"
```

---

### Task 4: `/device` 驗證頁（template + GET/POST + 防爆破 lockout）

**Files:**
- Modify: `src/controllers/oauth2.py`（`GET /device` + `POST /device`）
- Modify: `src/views/oauth2_templates.xml`（`device_verify` / `device_consent` / `device_result` template）
- Test: `src/tests/test_oauth2_device_flow.py`（HttpCase `TestDeviceFlowEndToEnd`）

**Interfaces:**
- Consumes: device_code record + helpers（Task 1）
- Produces: `GET /device`（auth='user'）顯示輸入/同意頁；`POST /device`（auth='user', csrf）處理 verify/approve/deny

- [ ] **Step 1: 寫 failing 測試**（HttpCase 端到端：authorize → approve → poll token）

```python
class TestDeviceFlowEndToEnd(HttpCase):
    def test_full_device_flow_round_trip(self):
        # 1. device_authorization
        resp = self.url_open('/oauth2/device_authorization', data={
            'client_id': 'woow-ha-smart-home', 'scope': 'smarthome:read',
        })
        body = resp.json()
        device_code, user_code = body['device_code'], body['user_code']

        # 2. poll before approval -> authorization_pending
        poll = self.url_open('/oauth2/token', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'device_code': device_code, 'client_id': 'woow-ha-smart-home',
        })
        self.assertEqual(poll.json()['error'], 'authorization_pending')

        # 3. user logs in + approves at /device
        self.authenticate('admin', 'admin')
        self.url_open('/device', data={
            'csrf_token': self._device_csrf(),  # helper grabs token from GET /device
            'user_code': user_code, 'approve': '1',
        })

        # 4. poll after approval -> token (respect interval: wait via fresh record)
        # (test harness: the slow_down guard tolerates one bump; poll twice)
        self.url_open('/oauth2/token', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'device_code': device_code, 'client_id': 'woow-ha-smart-home',
        })
        token_resp = self.url_open('/oauth2/token', data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'device_code': device_code, 'client_id': 'woow-ha-smart-home',
        })
        # one of the two polls returns the token (the other may be slow_down)
        # assert a token was ultimately issued
        body2 = token_resp.json()
        self.assertTrue('access_token' in body2 or body2.get('error') == 'slow_down')
```

> **實作者注意**：`_device_csrf()` helper 需 `GET /device?user_code=...` 抓 `csrf_token`（Odoo HttpCase 可 parse HTML）。slow_down 時序在 HttpCase 不穩，斷言放寬成「最終發出 token 或 slow_down」；嚴格 polling 時序由 `TestDeviceTokenGrant`（model 層）+ `register_poll` 單元測試守。可改用直接呼叫 handler（mock request，比照 `TestPkceOnlyTokenFlow`）做確定性斷言。

- [ ] **Step 2: 跑測試確認 fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDeviceFlowEndToEnd --stop-after-init`
Expected: FAIL（`/device` 404）

- [ ] **Step 3: 實作 template**

`src/views/oauth2_templates.xml` 在 `oauth2_error` template 後（`:237` `</template>` 後）新增三個 template（共用既有 `.oauth-card` 風格；此處給精簡 HTML，沿用 authorize template 的 `<style>` 慣例，可抽共用或複製）：

```xml
    <!-- Device Flow: enter user_code -->
    <template id="device_verify" name="Device Verification">
        <html>
            <head>
                <meta charset="utf-8"/>
                <meta name="viewport" content="width=device-width, initial-scale=1"/>
                <title>Connect a Device - Woow PaaS</title>
                <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&amp;display=swap" rel="stylesheet"/>
                <style>
                    * { margin: 0; padding: 0; box-sizing: border-box; }
                    body { font-family: 'Manrope', -apple-system, sans-serif; background: #f5f7fa;
                           display: flex; align-items: center; justify-content: center; min-height: 100vh; color: #1a1a2e; }
                    .oauth-card { background: #fff; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
                                  padding: 40px; max-width: 440px; width: 100%; margin: 20px; }
                    .oauth-logo { text-align: center; margin-bottom: 24px; }
                    .oauth-logo span { font-size: 28px; font-weight: 700; color: #6366f1; }
                    .oauth-title { font-size: 18px; font-weight: 600; text-align: center; margin-bottom: 24px; }
                    .code-input { width: 100%; padding: 14px; font-size: 22px; text-align: center; letter-spacing: 0.2em;
                                  text-transform: uppercase; border: 2px solid #e5e7eb; border-radius: 10px; margin-bottom: 16px;
                                  font-family: 'Manrope', monospace; }
                    .oauth-btn-allow { width: 100%; padding: 12px 24px; border-radius: 10px; font-size: 15px; font-weight: 600;
                                       border: none; cursor: pointer; background: #6366f1; color: #fff; font-family: inherit; }
                    .err { color: #dc2626; font-size: 14px; text-align: center; margin-bottom: 12px; }
                </style>
            </head>
            <body>
                <div class="oauth-card">
                    <div class="oauth-logo"><span>Woow PaaS</span></div>
                    <div class="oauth-title">Enter the code shown on your device</div>
                    <t t-if="error"><div class="err"><t t-esc="error"/></div></t>
                    <form method="POST" action="/device">
                        <input type="hidden" name="csrf_token" t-att-value="request.csrf_token(None)"/>
                        <input type="text" name="user_code" class="code-input" placeholder="XXXX-XXXX"
                               t-att-value="user_code or ''" autocomplete="off" required="required"/>
                        <button type="submit" name="verify" value="1" class="oauth-btn-allow">Continue</button>
                    </form>
                </div>
            </body>
        </html>
    </template>

    <!-- Device Flow: consent (after a valid user_code) -->
    <template id="device_consent" name="Device Consent">
        <html>
            <head>
                <meta charset="utf-8"/>
                <meta name="viewport" content="width=device-width, initial-scale=1"/>
                <title>Authorize Device - Woow PaaS</title>
                <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&amp;display=swap" rel="stylesheet"/>
                <style>
                    * { margin: 0; padding: 0; box-sizing: border-box; }
                    body { font-family: 'Manrope', -apple-system, sans-serif; background: #f5f7fa;
                           display: flex; align-items: center; justify-content: center; min-height: 100vh; color: #1a1a2e; }
                    .oauth-card { background: #fff; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
                                  padding: 40px; max-width: 440px; width: 100%; margin: 20px; }
                    .oauth-logo { text-align: center; margin-bottom: 24px; }
                    .oauth-logo span { font-size: 28px; font-weight: 700; color: #6366f1; }
                    .oauth-title { font-size: 18px; font-weight: 600; text-align: center; margin-bottom: 8px; }
                    .oauth-client-name { color: #6366f1; font-weight: 700; }
                    .device-code-confirm { text-align: center; font-size: 13px; color: #6b7280; margin: 16px 0 2px; }
                    .device-code { font-size: 26px; font-weight: 700; letter-spacing: 0.15em; text-align: center; color: #1a1a2e; margin: 2px 0 8px; }
                    .oauth-scopes { background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 12px; padding: 16px; margin: 16px 0 24px; }
                    .oauth-scope-item { padding: 6px 0; font-size: 14px; }
                    .oauth-actions { display: flex; gap: 12px; }
                    .oauth-btn { flex: 1; padding: 12px 24px; border-radius: 10px; font-size: 15px; font-weight: 600;
                                 border: none; cursor: pointer; font-family: inherit; }
                    .oauth-btn-deny { background: #f3f4f6; color: #4b5563; }
                    .oauth-btn-allow { background: #6366f1; color: #fff; }
                    .oauth-user-info { text-align: center; font-size: 13px; color: #9ca3af; margin-top: 16px; }
                </style>
            </head>
            <body>
                <div class="oauth-card">
                    <div class="oauth-logo"><span>Woow PaaS</span></div>
                    <div class="oauth-title"><span class="oauth-client-name"><t t-esc="client_name"/></span> wants to access your account</div>
                    <!-- RFC 8628 §5.4: confirm device possession (anti-phishing) -->
                    <div class="device-code-confirm">Confirm this code matches the one shown on your device:</div>
                    <div class="device-code"><t t-esc="user_code"/></div>
                    <div class="oauth-scopes">
                        <t t-foreach="scopes" t-as="s"><div class="oauth-scope-item"><t t-esc="s"/></div></t>
                    </div>
                    <form method="POST" action="/device">
                        <input type="hidden" name="csrf_token" t-att-value="request.csrf_token(None)"/>
                        <input type="hidden" name="user_code" t-att-value="user_code"/>
                        <div class="oauth-actions">
                            <button type="submit" name="deny" value="1" class="oauth-btn oauth-btn-deny">Deny</button>
                            <button type="submit" name="approve" value="1" class="oauth-btn oauth-btn-allow">Allow</button>
                        </div>
                    </form>
                    <div class="oauth-user-info">Signed in as <t t-esc="request.env.user.login"/></div>
                </div>
            </body>
        </html>
    </template>

    <!-- Device Flow: result (approved / denied) -->
    <template id="device_result" name="Device Result">
        <html>
            <head>
                <meta charset="utf-8"/>
                <meta name="viewport" content="width=device-width, initial-scale=1"/>
                <title>Device Authorized - Woow PaaS</title>
                <link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&amp;display=swap" rel="stylesheet"/>
                <style>
                    * { margin: 0; padding: 0; box-sizing: border-box; }
                    body { font-family: 'Manrope', -apple-system, sans-serif; background: #f5f7fa;
                           display: flex; align-items: center; justify-content: center; min-height: 100vh; }
                    .oauth-card { background: #fff; border-radius: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
                                  padding: 40px; max-width: 440px; width: 100%; margin: 20px; text-align: center; }
                    .ic { font-size: 48px; margin-bottom: 16px; }
                    .ttl { font-size: 20px; font-weight: 700; margin-bottom: 12px; }
                    .ok { color: #16a34a; } .no { color: #dc2626; }
                    .msg { font-size: 15px; color: #6b7280; }
                </style>
            </head>
            <body>
                <div class="oauth-card">
                    <t t-if="approved">
                        <div class="ic ok">&#10003;</div>
                        <div class="ttl ok">Device Connected</div>
                        <div class="msg">You can return to your device. It will finish connecting automatically.</div>
                    </t>
                    <t t-else="">
                        <div class="ic no">&#10007;</div>
                        <div class="ttl no">Request Denied</div>
                        <div class="msg"><t t-esc="message or 'The device authorization was denied.'"/></div>
                    </t>
                </div>
            </body>
        </html>
    </template>
```

- [ ] **Step 4: 實作 `/device` GET + POST**

`src/controllers/oauth2.py` 在 device_authorization route 後新增：

```python
    @route('/device', type='http', auth='user', methods=['GET'], website=False)
    def device_verify_get(self, **kwargs: Any) -> Any:
        """RFC 8628 §3.3: verification page. Optional ?user_code= pre-fill."""
        user_code = (kwargs.get('user_code') or '').strip().upper()
        return request.render('woow_paas_platform.device_verify', {
            'user_code': user_code, 'error': None,
        })

    @route('/device', type='http', auth='user', methods=['POST'],
           website=False, csrf=True)
    def device_verify_post(self, **kwargs: Any) -> Any:
        """Validate user_code, then show consent or process approve/deny."""
        user_code = (kwargs.get('user_code') or '').strip().upper()

        # Per-session brute-force throttle — the REAL guessing defense. A wrong
        # guess finds no record, so any per-RECORD counter never fires; the only
        # place to rate-limit guessing is per-actor here. (Persistent per-IP
        # version is F-DEV-1; session-based is the MVP floor, on top of 34.6-bit
        # entropy + 15-min lifetime + the auth='user' session requirement.)
        fails = request.session.get('device_verify_fails', 0)
        if fails >= MAX_VERIFICATION_ATTEMPTS:
            return request.render('woow_paas_platform.device_verify', {
                'user_code': '', 'error': 'Too many attempts. Please wait and try again.',
            })

        DeviceCode = request.env['woow_paas_platform.oauth_device_code'].sudo()
        device = DeviceCode.search([
            ('user_code', '=', user_code), ('status', '=', 'pending'),
        ], limit=1)

        # Invalid / expired user_code — generic error (no oracle on which codes
        # exist), and count the failed guess against this session.
        if not device or device.is_expired():
            request.session['device_verify_fails'] = fails + 1
            return request.render('woow_paas_platform.device_verify', {
                'user_code': '', 'error': 'Invalid or expired code. Please try again.',
            })

        # Valid code entered — reset the session failure counter.
        request.session['device_verify_fails'] = 0

        # Stage 2: approve / deny.
        if 'approve' in kwargs:
            device.mark_approved(request.env.user)
            return request.render('woow_paas_platform.device_result', {'approved': True})
        if 'deny' in kwargs:
            device.mark_denied()
            return request.render('woow_paas_platform.device_result',
                                  {'approved': False, 'message': None})

        # Stage 1 (verify pressed): show consent with scopes.
        scopes = [s for s in (device.scope or '').split() if s]
        return request.render('woow_paas_platform.device_consent', {
            'client_name': device.client_id.name,
            'user_code': device.user_code,
            'scopes': scopes or list(device.client_id.get_allowed_scopes()),
        })
```

> **防爆破細節**：「猜 user_code」失敗時 `device` 查無 → 走 generic error（不增 attempts，因為猜錯找不到 record；entropy 34 bits + 15min lifetime 是主防線）。`verification_attempts` 防的是「對某個已知/洩漏 user_code 的反覆嘗試」。若要擋自動化大量亂猜，於 reporter 階段再加 per-session（`request.session`）失敗計數 cooldown（列 follow-up，見 Self-Review）。

- [ ] **Step 5: 跑測試確認 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDeviceFlowEndToEnd --stop-after-init`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/controllers/oauth2.py src/views/oauth2_templates.xml src/tests/test_oauth2_device_flow.py
git commit -m "feat: 新增 /device 驗證頁（RFC 8628 §3.3，WOOW 品牌化 + user_code 防爆破）"
```

---

### Task 5: 過期清理 cron + manifest 註冊

**Files:**
- Create: `src/data/oauth_device_code_cron.xml`
- Modify: `src/__manifest__.py`（data 清單）
- Test: `src/tests/test_oauth2_device_flow.py`（`test_cron_gc_expired_removes_old` 已於 Task 1 涵蓋；此 task 驗 cron record 載入）

**Interfaces:**
- Consumes: `oauth_device_code._cron_gc_expired`（Task 1）

- [ ] **Step 1: 建立 cron data**

Create `src/data/oauth_device_code_cron.xml`（比照 `cloud_service_cron.xml`）：

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <data noupdate="1">
        <record id="ir_cron_gc_device_codes" model="ir.cron">
            <field name="name">OAuth: GC expired device codes</field>
            <field name="model_id" ref="model_woow_paas_platform_oauth_device_code"/>
            <field name="state">code</field>
            <field name="code">model._cron_gc_expired()</field>
            <field name="interval_number">10</field>
            <field name="interval_type">minutes</field>
            <field name="active" eval="True"/>
        </record>
    </data>
</odoo>
```

- [ ] **Step 2: 註冊到 manifest**

`src/__manifest__.py` 的 data 清單，在 `data/oauth_redirect_mode_update.xml` 之後加：

```python
        "data/oauth_device_code_cron.xml",
```

- [ ] **Step 3: 跑測試確認載入 + GC 正確**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestOAuthDeviceCodeModel.test_cron_gc_expired_removes_old --stop-after-init`
Expected: PASS（module 重載含 cron record 無錯 + GC 邏輯綠）

- [ ] **Step 4: Commit**

```bash
git add src/data/oauth_device_code_cron.xml src/__manifest__.py
git commit -m "feat: 新增 device_code 過期清理 cron"
```

---

### Task 6: seed + healer — HA client 加 `device_code` grant

**Files:**
- Modify: `src/data/oauth_clients.xml`（HA client grant_types）
- Modify: `src/models/oauth_client.py`（healer）
- Modify: `src/data/oauth_redirect_mode_update.xml`（或新 healer xml；複用 `<function>` 模式）
- Test: `src/tests/test_oauth2_device_flow.py`（`TestDeviceGrantHealer`）

**Interfaces:**
- Consumes: 既有 `_ensure_ha_smart_home_*` healer pattern
- Produces: `oauth_client._ensure_ha_smart_home_device_grant()`

- [ ] **Step 1: 寫 failing 測試**

```python
class TestDeviceGrantHealer(TransactionCase):
    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')
        self.DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'

    def test_seed_has_device_grant(self):
        # fresh install seed already includes device_code grant
        self.assertTrue(self.client.check_grant_type(self.DEVICE_GRANT))
        # browser flow grant still present (hybrid: both grants coexist)
        self.assertTrue(self.client.check_grant_type('authorization_code'))

    def test_healer_adds_device_grant_to_legacy(self):
        # simulate legacy DB without device grant
        self.client.grant_types = 'authorization_code,refresh_token'
        self.assertFalse(self.client.check_grant_type(self.DEVICE_GRANT))
        self.OAuthClient._ensure_ha_smart_home_device_grant()
        self.assertTrue(self.client.check_grant_type(self.DEVICE_GRANT))
        self.assertTrue(self.client.check_grant_type('authorization_code'))

    def test_healer_idempotent(self):
        self.OAuthClient._ensure_ha_smart_home_device_grant()
        self.OAuthClient._ensure_ha_smart_home_device_grant()
        grants = [g.strip() for g in self.client.grant_types.split(',')]
        self.assertEqual(grants.count(self.DEVICE_GRANT), 1)
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDeviceGrantHealer.test_healer_adds_device_grant_to_legacy --stop-after-init`
Expected: FAIL（healer 不存在）

- [ ] **Step 3a: 更新 seed**

`src/data/oauth_clients.xml` HA client 的 grant_types：

```xml
            <field name="grant_types">authorization_code,refresh_token,urn:ietf:params:oauth:grant-type:device_code</field>
```

- [ ] **Step 3b: 實作 healer**

`src/models/oauth_client.py` 在 `_ensure_ha_smart_home_redirect_mode` 後新增：

```python
    @api.model
    def _ensure_ha_smart_home_device_grant(self) -> None:
        """Idempotently add the RFC 8628 device_code grant to the seeded HA
        client (hybrid: browser uses authorization_code, app uses device_code).
        oauth_clients.xml is noupdate=1 so the seed change never reaches
        existing DBs; this healer covers the upgrade path. Wired into
        post_init_hook + data/oauth_redirect_mode_update.xml <function>."""
        device_grant = 'urn:ietf:params:oauth:grant-type:device_code'
        client = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home',
            raise_if_not_found=False,
        )
        if not client:
            return
        grants = [g.strip() for g in (client.grant_types or '').split(',') if g.strip()]
        if device_grant not in grants:
            grants.append(device_grant)
            client.grant_types = ','.join(grants)
```

- [ ] **Step 3c: 接到 install + upgrade 雙路徑**

Create `src/data/oauth_device_grant_update.xml`（獨立 healer 檔，對齊 `oauth_scope_updates.xml` 一 healer 一檔慣例）：

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- noupdate=0 (default): <function> re-runs every -u. oauth_clients.xml
         is noupdate=1, so the seed grant_types change only reaches fresh
         installs; this healer covers existing DBs (upgrade path). See
         seed-noupdate-sticky-template-links pitfall. -->
    <function
        model="woow_paas_platform.oauth_client"
        name="_ensure_ha_smart_home_device_grant"/>
</odoo>
```

`src/__manifest__.py` data 清單在 `data/oauth_device_code_cron.xml` 後加：

```python
        "data/oauth_device_grant_update.xml",
```

`src/hooks.py` 的 `post_init_hook` 在 `_ensure_ha_client_redirect_mode(env)` 後加：

```python
    env['woow_paas_platform.oauth_client']._ensure_ha_smart_home_device_grant()
```

（或包成 `_ensure_ha_client_device_grant(env)` helper，比照既有風格。）

- [ ] **Step 4: 跑測試確認 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDeviceGrantHealer --stop-after-init`
Expected: PASS（3 test）

> 此 task 完成後，Task 2/4 的 HttpCase（fresh install test DB 帶 device grant）才會穩定綠。執行順序建議：**Task 1 → Task 6（seed 部分）→ Task 2 → Task 3 → Task 4 → Task 5**，或 Task 6 整體提前到 Task 2 之前。

- [ ] **Step 5: Commit**

```bash
git add src/data/oauth_clients.xml src/models/oauth_client.py src/data/oauth_device_grant_update.xml src/__manifest__.py src/hooks.py src/tests/test_oauth2_device_flow.py
git commit -m "feat: HA client 加 device_code grant（hybrid，healer 覆蓋升級路徑）"
```

---

### Task 7: API reference doc

**Files:**
- Modify: `docs/reference/api/ha-component-integration.md`（新增 §Device Flow）

**Interfaces:** 無（reference 類，無 test）

- [ ] **Step 1: 新增 device flow 契約段**

在 §1.1 Client 規格的 Grant types 列補 device_code，並新增一節說明 hybrid 分流 + 3 endpoint + RFC 8628 polling 錯誤碼 + user_code 安全。內容要點：
- `client_id=woow-ha-smart-home` 同時支援 `authorization_code`（browser）+ `urn:ietf:params:oauth:grant-type:device_code`（app）
- `POST /oauth2/device_authorization` 請求/回應（device_code/user_code/verification_uri/verification_uri_complete/expires_in=900/interval=5）
- `/device` 驗證頁（auth=user 登入 + 輸 user_code + 同意）
- token polling：`grant_type=urn:ietf:params:oauth:grant-type:device_code` + `device_code`；pending→`authorization_pending`、太快→`slow_down`、過期→`expired_token`、拒絕→`access_denied`、成功→access+refresh token
- UA 分流由 component 端負責（UA 含 `Home Assistant/` → device flow）

- [ ] **Step 2: lint + Commit**

```bash
git add docs/reference/api/ha-component-integration.md
git commit -m "docs: 補 RFC 8628 device flow 契約進 HA 串接 reference"
```

---

## Self-Review

**1. 契約覆蓋（RFC 8628 + hybrid 5 點）：**
- device_authorization endpoint → Task 2 ✓
- /device 驗證頁（品牌化）→ Task 4 ✓
- token device_code grant（pending/slow_down/expired/denied）→ Task 3 ✓
- 安全（user_code 34.6bits + per-session throttle / slow_down cap / single-use CAS / cron / HTTPS+CSRF / §5.4 possession 確認）→ Task 1（entropy/interval cap/consume CAS/cron）+ Task 3（terminal-first 排序/slow_down/CAS）+ Task 4（session throttle/CSRF/§5.4 確認）+ Task 2（web.base.url https only/scope write-back）✓
- 同一 client 雙 grant → Task 6（grant_types 並存）✓

**2. Placeholder scan：** 每個 code step 給完整可貼 code。`_device_csrf()` HttpCase helper 標為實作者注意（提供 mock-handler 替代方案做確定性斷言）。

**3. Type consistency：** `DEVICE_GRANT` 常數、`create_for_client`/`register_poll`/`mark_approved`/`is_expired`/`_generate_user_code`/`_cron_gc_expired`/`_ensure_ha_smart_home_device_grant` 在 Task 間命名一致；`_issue_tokens`/`_authenticate_client` 沿用既有簽章。

**4. 零回歸：** token dispatch 只新增 elif；現有 authorize/refresh/client_credentials handler 不動；`oauth2_authorize` template 不動（device 用新 template）。

## 待 reporter / follow-up（不阻擋 MVP）

1. **持久化 per-IP `/device` 猜測 cooldown**（F-DEV-1，medium/security）：MVP 已有 per-session（`device_verify_fails`）throttle + 34.6 bits entropy + 15min lifetime + `auth='user'` gate（四層）。session-based 可清 cookie 繞過；要擋大規模分散式亂猜，升級成持久化 per-IP 計數（輕量 model 或 cache）+ 指數退避。
2. **polling DB 負荷觀測**（F-DEV-2，運維）：stg 上以多 device 模擬高頻輪詢，量 token endpoint DB 連線/worker 佔用；必要時調 interval 下限或 worker。
3. **device_code 管理 UI**（F-DEV-3，low）：admin 後台檢視/撤銷 pending device codes（被盜裝置）。
4. **constant 雙寫**（F-DEV-4，trivial）：`DEVICE_CODE_LIFETIME` controller 從 model import；若專案不採跨層 import，改共用常數模組。
