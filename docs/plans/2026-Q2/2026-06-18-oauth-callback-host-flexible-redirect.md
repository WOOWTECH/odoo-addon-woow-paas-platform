# OAuth Callback Host-Flexible Redirect 實作計畫（WOOW paas 側）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（推薦）或 superpowers:executing-plans 逐 task 實作。步驟用 checkbox（`- [ ]`）追蹤。

**Goal:** 讓 OAuth client `woow-ha-smart-home` 的 `redirect_uri` 驗證從「精確比對固定的 `my.home-assistant.io`」放寬成「host 不限、path 鎖死 `/auth/external/woow/callback`、scheme 受 RFC 8252 規則約束」的 pattern 比對，使 HA component 路線一（callback 直接落使用者本機 HA、由 component 渲染 WOOW 品牌頁）成立，完全消除 my.home-assistant.io 第三方頁。

**Architecture:** 在 `oauth_client` model 新增 `redirect_uri_match_mode` Selection 欄位（`exact` 預設 / `ha_callback`），`check_redirect_uri` 依模式分支；`ha_callback` 模式走 host-flexible 驗證（path 常數比對 + scheme/host 類別規則 + 拒 query/fragment/userinfo）。**controller (`oauth2.py`) 零改動** —— 兩個 caller（`:173` authorize GET、`:273` authorize POST）簽章不變，token endpoint 的一致性比對（`:486` `auth_code.redirect_uri != redirect_uri`，RFC 6749 §4.1.3）本就不做白名單驗證、放寬後自動相容。既有 stg/prod DB 因 seed `noupdate=1`（sticky）靠 idempotent healer（比照現有 `_ensure_ha_smart_home_sa_scopes`）翻正，不走 migration、不 bump manifest 版號。

**Tech Stack:** Odoo 18 addon（Python 4-space）、`urllib.parse.urlsplit` + `ipaddress`（皆 stdlib，無新依賴）、Odoo `@api.constrains`、`<function>` data healer、`TransactionCase` 單元測試。

---

## 契約對齊紀錄（與 maestri agent `ha-paas-component`，2026-06-18 定稿）

本計畫是跨 repo feature 的 WOOW paas 側；HA component 側（override `async_get_redirect_uri()`、註冊自家 view 渲染 WOOW 頁、重用 HA Core `_decode_jwt`/`async_configure`）由 `ha-paas-component` 另行定稿。研究結論見 maestri note `research-custom-callback-page`（歸 Elmo）。

Eugene 拍板的 5 點 + 我方 audit 對應：

| # | 拍板 | 本計畫對應 |
|---|---|---|
| 1 | path = `/auth/external/woow/callback` 為跨 repo 契約常數（HA 端 `WOOW_AUTH_CALLBACK_PATH`） | model class 常數 `OAuthClient.HA_CALLBACK_PATH`（Task 1） |
| 2 | http 範圍採 RFC 8252：公網 host 強制 https；http 僅允許 loopback / RFC1918 私有 / `.local` | `_is_local_host()` host 分類（Task 1） |
| 3 | query/fragment → reject | `_check_ha_callback_uri()` 拒 query/fragment/userinfo（Task 1） |
| 4 | **移除 `my.home-assistant.io`，只接受新 pattern（不走 superset）** | seed 改值 + healer 清舊值（Task 3/4）；prod 切換 lock-step 見下方 Rollout 節 |
| 5 | flag 形態由我方拍板 | **Selection `redirect_uri_match_mode`**（可擴充、自我說明；雙方皆傾向）（Task 1） |

附帶：`docs/reference/api/ha-component-integration.md` §1.1 表格「精確比對」字樣需同步更新（Task 5）；走 healer 不 bump manifest（已確認）。

## 安全設計 rationale（對抗式 audit 校準後）

1. **PKCE 兜底的語意邊界（重要校準）**：PKCE 防的是「被竊的 authorization code 無法兌換」（無 `code_verifier` 換不到 token），**不是**「code 不會被導去攻擊者 host」。放寬 host 後，open-redirect 的殘餘面（code 被導去攻擊者控制的 callback）由三者共同收斂：**path+scheme 白名單 + 一次性 code（`oauth_code.is_used`）+ PKCE**。對 HA 路線一可接受：導去攻擊者 host 的 code 無 verifier 不可兌換，且 callback 頁本就只 bounce code 回本機 HA。
2. **為何不限私有網段**：必須支援 Nabu Casa cloud（`*.ui.nabu.casa`，公網 https）、自訂網域、遠端 HA，限私有網段會擋掉合法情境。改靠 path 鎖死 + scheme 類別規則收斂。
3. **`ha_callback` ⇒ `require_pkce` 不變量（Task 2）**：放寬 host 但無 PKCE = 裸 open-redirect。用 `@api.constrains` 強制「`redirect_uri_match_mode='ha_callback'` 必須 `require_pkce=True`」，把放寬與 PKCE 綁定。此不變量**不綁死 `client_id`**，比 hardcode 特例通用且安全（即使 admin 對別的 client 開 `ha_callback` 也被迫要 PKCE）。
4. **token endpoint 不需同步改（lock-step 不斷裂）**：`oauth2.py:486` 是「token 請求帶的 redirect_uri == 發 code 時存進 `auth_code` 的值」exact 一致性比對，非白名單驗證。合法 flow：authorize 用 `https://{user-host}/auth/external/woow/callback` 過放寬 → 原樣存進 `auth_code`（`:311`）→ token 帶同一 URI → `:486` exact 通過。放寬只動 `check_redirect_uri` 比對邏輯即完整生效。
5. **衍生不變量（記入程式註解）**：放寬**只能**動 `check_redirect_uri` 的比對邏輯，**不可**在 authorize→`auth_code`→token 之間對 redirect_uri 做 normalize/改寫 —— 否則 `:486` 一致性比對才會真的斷裂。同理 `urlsplit` 自身會默默移除 `\t\r\n`（隱性 normalize），故 `_check_ha_callback_uri` 在進 `urlsplit` **之前**先 fail-closed 拒含 control char / 前後空白的 raw 字串，維持「驗證器看到的 host == 儲存/瀏覽器看到的字面」不變量（對抗式 review HIGH-1）。
6. **已知可接受邊角**：loopback/私有/link-local（含雲端 metadata `169.254.169.254`、IPv4-mapped `::ffff:...` 拆解後）會被 `_is_local_host` 判為 local 而允許 http。殘餘面是 code 走一段明文 http hop、可能導向非預期內部端點，但此處是「瀏覽器 top-level navigation 的目標」而非 server-side fetch（SSRF 不適用），且仍受 path 鎖 + PKCE + 一次性 code 三重約束，無 verifier 的 code 不可兌換，故可接受。`.local` mDNS 名（如 `homeassistant.local`，HA 安裝預設）刻意保留以維持本機相容；攻擊者搶 mDNS 名需 LAN 鄰接，殘餘仍由 PKCE 兜底。

## Rollout & 跨 repo lock-step（第 4 點移除舊值的後果）

- **stg**：兩端皆我方可控。次序維持 **a→d**：paas 先上 stg（healer 移除舊值）→ `ha-paas-component` 用新 callback 版本接 E2E。無 superset 無妨（Eugene 確認）。
- **prod（⚠ open question，發版時須與 `ha-paas-component` 再確認）**：移除舊值是 breaking change，healer 在 prod `-u` 跑即清掉 `my.home-assistant.io`。**因此 prod 的 paas 發版必須與 component「已支援新 callback」lock-step**。影響面評估：
  - `refresh_token` grant **不帶 redirect_uri**（見 `_handle_refresh_token`），既有 token 續期不受影響。
  - 僅「全新授權 / 重新授權」（走 `/oauth2/authorize`）會用到 redirect_uri，故空窗影響僅限切換期間新接入或重新授權者。
  - **待 `ha-paas-component` 回覆**：prod 是否已有真實 HA 使用者綁 `my.home-assistant.io`？若有，需議定 paas↔component 的發版窗口（component 先具備新 callback → paas 發版）。

## Global Constraints

- Python 4-space 縮排（PEP 8）；model 命名與欄位定義比照 repo 既有 `oauth_client.py` 風格（keyword args + `help`）。
- **無新增 Python 依賴**：只用 stdlib `urllib.parse`、`ipaddress`。
- **走 `<function>` healer，不走 versioned migration、不 bump `__manifest__.py`**（現為 `18.0.1.0.40`）：本變更是「`noupdate=1` sticky seed 翻正」，正是 `<function>` healer 對症場景（比照 `data/oauth_scope_updates.xml` + `oauth_client._ensure_ha_smart_home_sa_scopes`）。versioned migration（如 `18.0.1.0.40` 的 `reaped_since` backfill）是用於補既有壞資料列，與此性質不同。新增 `required=True, default='exact'` 的 Selection 欄位由 Odoo column-add 自動 backfill 既有 row 成 `'exact'`，無需 migration。
- **controller `oauth2.py` 不得改動**（本 feature 的安全性正建立在 caller 不變）。
- path 契約常數值固定為 `'/auth/external/woow/callback'`，與 HA component `const.py:WOOW_AUTH_CALLBACK_PATH` 對齊；改值屬跨 repo breaking，須兩端 lock-step。
- 測試執行（任一）：
  - 本機 Odoo：`./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:<Class>.<method> --stop-after-init`
  - K8s sandbox：`./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox> --module woow_paas_platform`

---

## File Structure

- `src/models/oauth_client.py`（Modify）— 新增 `HA_CALLBACK_PATH` 常數、`redirect_uri_match_mode` 欄位、`_is_local_host()`、`_check_ha_callback_uri()`、`check_redirect_uri()` 分支、`_check_ha_callback_requires_pkce()` constrains、`_ensure_ha_smart_home_redirect_mode()` healer。單一 model 檔，職責明確，沿用既有結構。
- `src/data/oauth_redirect_mode_update.xml`（Create）— `<function>` healer（`noupdate=0`，每次 `-u` 跑），比照 `oauth_scope_updates.xml`。
- `src/data/oauth_clients.xml`（Modify line 14-15）— HA record 加 `redirect_uri_match_mode`、`redirect_uris` 改為 path 值。
- `src/__manifest__.py`（Modify `data` 清單）— 註冊新 `oauth_redirect_mode_update.xml`。
- `src/hooks.py`（Modify `post_init_hook`）— fresh-install path 呼叫新 healer。
- `src/tests/test_oauth2.py`（Modify）— 新增 host-flexible 驗證 + constraint + healer 測試；更新 `test_seeded_ha_smart_home_client`。
- `docs/reference/api/ha-component-integration.md`（Modify §1.1）— 更新 redirect_uri 契約描述。

---

### Task 1: `redirect_uri_match_mode` 欄位與 host-flexible 驗證

**Files:**
- Modify: `src/models/oauth_client.py`（import 區、欄位區 `:52` 後、`check_redirect_uri` `:132-147`）
- Test: `src/tests/test_oauth2.py`（`TestOAuthClient` class，`:56` 後）

**Interfaces:**
- Consumes: 無（基礎 task）
- Produces:
  - `OAuthClient.HA_CALLBACK_PATH: str = '/auth/external/woow/callback'`（class 常數）
  - `OAuthClient.redirect_uri_match_mode`（Selection `exact`|`ha_callback`，default `exact`）
  - `OAuthClient._is_local_host(host: str) -> bool`（staticmethod）
  - `OAuthClient._check_ha_callback_uri(self, uri: str) -> bool`
  - `OAuthClient.check_redirect_uri(self, uri: str) -> bool`（行為擴充，簽章不變）

- [ ] **Step 1: 寫 failing 測試（host-flexible 驗證矩陣 + exact 模式回歸）**

加到 `src/tests/test_oauth2.py` 的 `TestOAuthClient` class（`test_check_redirect_uri` 之後）：

```python
def _ha_client(self):
    """A public + PKCE client in ha_callback mode (mirrors the seeded HA client)."""
    return self.OAuthClient.create({
        'name': 'HA Callback Test',
        'client_type': 'public',
        'require_pkce': True,
        'redirect_uri_match_mode': 'ha_callback',
        'redirect_uris': '/auth/external/woow/callback',
    })

def test_ha_callback_accepts_loopback_http(self):
    c = self._ha_client()
    self.assertTrue(c.check_redirect_uri('http://127.0.0.1:8123/auth/external/woow/callback'))
    self.assertTrue(c.check_redirect_uri('http://localhost:8123/auth/external/woow/callback'))
    self.assertTrue(c.check_redirect_uri('http://[::1]:8123/auth/external/woow/callback'))

def test_ha_callback_accepts_private_and_local_http(self):
    c = self._ha_client()
    self.assertTrue(c.check_redirect_uri('http://192.168.1.50:8123/auth/external/woow/callback'))
    self.assertTrue(c.check_redirect_uri('http://10.0.0.5:8123/auth/external/woow/callback'))
    self.assertTrue(c.check_redirect_uri('http://homeassistant.local:8123/auth/external/woow/callback'))

def test_ha_callback_accepts_public_https(self):
    c = self._ha_client()
    # Nabu Casa cloud + custom domains: public host, must be https
    self.assertTrue(c.check_redirect_uri('https://abc123.ui.nabu.casa/auth/external/woow/callback'))
    self.assertTrue(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback'))

def test_ha_callback_rejects_public_http(self):
    c = self._ha_client()
    # public host over http -> reject (RFC 8252)
    self.assertFalse(c.check_redirect_uri('http://ha.mydomain.com/auth/external/woow/callback'))
    self.assertFalse(c.check_redirect_uri('http://attacker.example.com/auth/external/woow/callback'))

def test_ha_callback_rejects_wrong_path(self):
    c = self._ha_client()
    self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/evil'))
    self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback/extra'))
    self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback/../evil'))

def test_ha_callback_rejects_query_fragment_userinfo(self):
    c = self._ha_client()
    self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback?x=1'))
    self.assertFalse(c.check_redirect_uri('https://ha.mydomain.com/auth/external/woow/callback#frag'))
    self.assertFalse(c.check_redirect_uri('https://user:pass@ha.mydomain.com/auth/external/woow/callback'))

def test_ha_callback_rejects_bad_scheme(self):
    c = self._ha_client()
    self.assertFalse(c.check_redirect_uri('ftp://ha.mydomain.com/auth/external/woow/callback'))
    self.assertFalse(c.check_redirect_uri('javascript:alert(1)//auth/external/woow/callback'))

def test_ha_callback_rejects_parser_confusion(self):
    # urlsplit silently strips \t\r\n; fail closed on raw control chars /
    # surrounding whitespace so the validated host cannot diverge from the
    # stored/browser-seen literal (HIGH-1 from adversarial review).
    c = self._ha_client()
    self.assertFalse(c.check_redirect_uri('http://local\thost:8123/auth/external/woow/callback'))
    self.assertFalse(c.check_redirect_uri('http://127.0.0.1\n/auth/external/woow/callback'))
    self.assertFalse(c.check_redirect_uri(' http://127.0.0.1:8123/auth/external/woow/callback'))
    self.assertFalse(c.check_redirect_uri('https://exa\tmple.com/auth/external/woow/callback'))

def test_ha_callback_local_host_classifier_edges(self):
    c = self._ha_client()
    # bare '.local' (empty label) is NOT local -> public -> http reject
    self.assertFalse(c.check_redirect_uri('http://.local/auth/external/woow/callback'))
    # IPv4-mapped IPv6 loopback unwraps and is allowed over http
    self.assertTrue(c.check_redirect_uri('http://[::ffff:127.0.0.1]/auth/external/woow/callback'))
    # uppercase host normalized; .local mDNS allowed over http
    self.assertTrue(c.check_redirect_uri('http://HomeAssistant.LOCAL:8123/auth/external/woow/callback'))

def test_exact_mode_unaffected_by_relaxation(self):
    # Default exact-mode clients keep strict membership matching.
    c = self.OAuthClient.create({
        'name': 'Exact Mode',
        'redirect_uris': 'https://example.com/cb',
    })
    self.assertEqual(c.redirect_uri_match_mode, 'exact')
    self.assertTrue(c.check_redirect_uri('https://example.com/cb'))
    # An ha_callback-shaped URI must NOT be accepted by an exact-mode client.
    self.assertFalse(c.check_redirect_uri('https://anything.com/auth/external/woow/callback'))
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestOAuthClient.test_ha_callback_accepts_loopback_http --stop-after-init`
Expected: FAIL（`redirect_uri_match_mode` 欄位不存在 / `Invalid field`）

- [ ] **Step 3: 實作 — import、常數、欄位、驗證邏輯**

`src/models/oauth_client.py` 頂部 import 區（`:1-5`）改為：

```python
import ipaddress
import secrets
from urllib.parse import urlsplit

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from werkzeug.security import check_password_hash, generate_password_hash
```

在 class body 開頭（`_order = 'create_date desc'` 之後，`name` 欄位之前）加 class 常數：

```python
    # Cross-repo contract constant: the fixed callback path the HA custom
    # component registers as its own view. Mirrors WOOW_AUTH_CALLBACK_PATH in
    # the HA component's const.py. Changing this is a cross-repo breaking
    # change requiring lock-step release of both repos.
    HA_CALLBACK_PATH = '/auth/external/woow/callback'
```

在 `redirect_uris` 欄位（`:52-57`）之後新增欄位：

```python
    redirect_uri_match_mode = fields.Selection(
        selection=[
            ('exact', 'Exact Match'),
            ('ha_callback', 'HA Callback Pattern'),
        ],
        string='Redirect URI Match Mode',
        default='exact',
        required=True,
        help="exact: redirect_uri must appear verbatim in 'Redirect URIs' "
             "(RFC 6749 strict matching; the default for all clients). "
             "ha_callback: host-flexible matching for the Home Assistant "
             "custom component — scheme in {http,https}, path locked to "
             "HA_CALLBACK_PATH, host unconstrained (http only for "
             "loopback/private/.local hosts). Relies on PKCE to bound the "
             "open-redirect surface; require_pkce must be enabled.",
    )
```

把 `check_redirect_uri`（`:132-147`）替換為分支版本，並新增兩個 helper：

```python
    def check_redirect_uri(self, uri: str) -> bool:
        """Check if a redirect URI is acceptable for this client.

        exact mode: verbatim membership in redirect_uris.
        ha_callback mode: host-flexible pattern (see _check_ha_callback_uri).

        Security invariant: this method is the ONLY redirect_uri whitelist
        gate (the token endpoint does a redirect_uri *consistency* check
        against the stored auth_code value, not a whitelist check). Do NOT
        normalize/rewrite redirect_uri between authorize and token, or the
        token-side equality check (oauth2.py:486) will break.
        """
        self.ensure_one()
        if self.redirect_uri_match_mode == 'ha_callback':
            return self._check_ha_callback_uri(uri)
        if not self.redirect_uris:
            return False
        registered = [
            u.strip() for u in self.redirect_uris.strip().splitlines() if u.strip()
        ]
        return uri in registered

    def _check_ha_callback_uri(self, uri: str) -> bool:
        """Host-flexible redirect_uri check for the HA custom component.

        Accepts any host as long as: scheme in {http,https}; path is exactly
        HA_CALLBACK_PATH; no query/fragment/userinfo; and http is used only
        for loopback / RFC1918 private / .local hosts (public hosts must use
        https, RFC 8252). The relaxed host surface is bounded by PKCE +
        single-use codes (see this client's require_pkce constraint).
        """
        if not uri:
            return False
        # urlsplit silently strips \t\r\n before parsing, which would let the
        # validated host diverge from the stored / browser-seen literal (the
        # token-endpoint equality check at oauth2.py:486 trusts that literal).
        # Fail closed on raw control chars / surrounding whitespace BEFORE
        # urlsplit — defends against the parser-confusion class.
        if uri != uri.strip() or any(c in uri for c in '\t\r\n'):
            return False
        try:
            parts = urlsplit(uri)
        except ValueError:
            return False
        if parts.scheme not in ('http', 'https'):
            return False
        if parts.path != self.HA_CALLBACK_PATH:
            return False
        if parts.query or parts.fragment:
            return False
        if parts.username or parts.password:
            return False
        try:
            host = parts.hostname
        except ValueError:
            return False
        if not host:
            return False
        if parts.scheme == 'http' and not self._is_local_host(host):
            return False
        return True

    @staticmethod
    def _is_local_host(host: str) -> bool:
        """True for loopback / RFC1918 private / link-local / .local hosts.

        Decides whether plain http is acceptable. Public hostnames (incl.
        Nabu Casa *.ui.nabu.casa) return False -> https required.

        Notes:
        - mDNS: requires a non-empty label before '.local' (bare '.local'
          rejected); '.localhost' subdomains accepted (loopback convention).
        - IPv4-mapped IPv6 (::ffff:x.x.x.x) is unwrapped so it classifies like
          the embedded IPv4 (predictable, no http-downgrade surprise).
        - ipaddress.is_private also covers reserved/doc ranges (TEST-NET,
          class E); allowing http there is harmless (not attacker-routable),
          so we keep the classifier simple rather than over-engineer it.
        """
        h = (host or '').lower().rstrip('.')  # normalize FQDN trailing dot
        if not h:
            return False
        if h == 'localhost' or h.endswith('.localhost'):
            return True
        if h != '.local' and h.endswith('.local'):
            return True
        try:
            ip = ipaddress.ip_address(h)
        except ValueError:
            return False  # a real domain name -> public
        mapped = getattr(ip, 'ipv4_mapped', None)
        if mapped is not None:
            ip = mapped  # treat ::ffff:x.x.x.x like the IPv4 it embeds
        return ip.is_loopback or ip.is_private or ip.is_link_local
```

- [ ] **Step 4: 跑全組測試確認 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestOAuthClient --stop-after-init`
Expected: PASS（含既有 `test_check_redirect_uri` 回歸 + 8 個新 test）

- [ ] **Step 5: Commit**

```bash
git add src/models/oauth_client.py src/tests/test_oauth2.py
git commit -m "feat: oauth_client 新增 ha_callback host-flexible redirect_uri 驗證模式"
```

---

### Task 2: `ha_callback ⇒ require_pkce` 不變量

**Files:**
- Modify: `src/models/oauth_client.py`（`_check_client_type_consistency` `:83-107` 之後新增 constrains）
- Test: `src/tests/test_oauth2.py`（`TestPublicClientConstraints` class，`:242` 起）

**Interfaces:**
- Consumes: `redirect_uri_match_mode`、`require_pkce`（Task 1）
- Produces: `OAuthClient._check_ha_callback_requires_pkce()`（`@api.constrains`）

- [ ] **Step 1: 寫 failing 測試**

加到 `TestPublicClientConstraints`（`_public_vals` helper 已存在於該 class，沿用）：

```python
def test_ha_callback_mode_requires_pkce(self):
    """ha_callback relaxes host; without PKCE it would be a bare open-redirect."""
    with self.assertRaises(ValidationError):
        self.OAuthClient.create({
            'name': 'Bad HA Callback',
            'client_type': 'confidential',
            'require_pkce': False,
            'redirect_uri_match_mode': 'ha_callback',
            'redirect_uris': '/auth/external/woow/callback',
        })

def test_ha_callback_mode_with_pkce_allowed(self):
    client = self.OAuthClient.create({
        'name': 'Good HA Callback',
        'client_type': 'public',
        'require_pkce': True,
        'redirect_uri_match_mode': 'ha_callback',
        'redirect_uris': '/auth/external/woow/callback',
    })
    self.assertEqual(client.redirect_uri_match_mode, 'ha_callback')
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestPublicClientConstraints.test_ha_callback_mode_requires_pkce --stop-after-init`
Expected: FAIL（未 raise ValidationError — constrains 尚不存在）

- [ ] **Step 3: 實作 constrains**

在 `src/models/oauth_client.py` 的 `_check_client_type_consistency` method（`:83-107`）之後新增：

```python
    @api.constrains('redirect_uri_match_mode', 'require_pkce')
    def _check_ha_callback_requires_pkce(self) -> None:
        """ha_callback mode relaxes the host check and relies on PKCE to bound
        the open-redirect surface, so PKCE must be enabled. Not keyed on
        client_id on purpose: any client opted into ha_callback inherits the
        same safety floor."""
        for record in self:
            if record.redirect_uri_match_mode == 'ha_callback' and not record.require_pkce:
                raise ValidationError(
                    "redirect_uri_match_mode='ha_callback' relaxes the "
                    "redirect_uri host check and depends on PKCE to bound the "
                    "open-redirect surface. Enable 'Require PKCE'."
                )
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestPublicClientConstraints --stop-after-init`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/models/oauth_client.py src/tests/test_oauth2.py
git commit -m "feat: 強制 ha_callback redirect 模式必須啟用 PKCE"
```

---

### Task 3: 既有 DB healer（sticky-noupdate 翻正）

**Files:**
- Modify: `src/models/oauth_client.py`（`_ensure_ha_smart_home_sa_scopes` `:171-198` 之後新增 healer method）
- Create: `src/data/oauth_redirect_mode_update.xml`
- Modify: `src/__manifest__.py`（`data` 清單，緊接 `oauth_scope_updates.xml` 之後）
- Modify: `src/hooks.py`（`_ensure_ha_client_sa_scopes` 旁 + `post_init_hook` `:186`）
- Test: `src/tests/test_oauth2.py`（新 class `TestHaRedirectModeHealer`）

**Interfaces:**
- Consumes: `redirect_uri_match_mode`、`HA_CALLBACK_PATH`、constrains（Task 1/2）
- Produces: `OAuthClient._ensure_ha_smart_home_redirect_mode()`（`@api.model`，idempotent）

- [ ] **Step 1: 寫 failing 測試**

新增 class 到 `src/tests/test_oauth2.py`：

```python
class TestHaRedirectModeHealer(TransactionCase):
    """Healer migrates the seeded HA client to ha_callback mode on -u
    (sticky-noupdate path: seed XML edits never reach existing DBs)."""

    def setUp(self):
        super().setUp()
        self.OAuthClient = self.env['woow_paas_platform.oauth_client']
        self.client = self.env.ref('woow_paas_platform.oauth_client_ha_smart_home')

    def test_healer_flips_legacy_record_to_ha_callback(self):
        # Simulate a legacy DB row still in exact mode with the old value.
        self.client.write({
            'redirect_uri_match_mode': 'exact',
            'redirect_uris': 'https://my.home-assistant.io/redirect/oauth',
        })
        self.OAuthClient._ensure_ha_smart_home_redirect_mode()
        self.assertEqual(self.client.redirect_uri_match_mode, 'ha_callback')
        self.assertEqual(self.client.redirect_uris, self.OAuthClient.HA_CALLBACK_PATH)
        # Legacy my.home-assistant.io value is gone (not superset).
        self.assertNotIn('my.home-assistant.io', self.client.redirect_uris)

    def test_healer_is_idempotent(self):
        self.OAuthClient._ensure_ha_smart_home_redirect_mode()
        self.OAuthClient._ensure_ha_smart_home_redirect_mode()
        self.assertEqual(self.client.redirect_uri_match_mode, 'ha_callback')
        self.assertEqual(self.client.redirect_uris, self.OAuthClient.HA_CALLBACK_PATH)
```

- [ ] **Step 2: 跑測試確認 fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestHaRedirectModeHealer.test_healer_flips_legacy_record_to_ha_callback --stop-after-init`
Expected: FAIL（`_ensure_ha_smart_home_redirect_mode` 不存在 / `AttributeError`）

- [ ] **Step 3a: 實作 healer method**

在 `src/models/oauth_client.py` 的 `_ensure_ha_smart_home_sa_scopes`（`:171-198`）之後新增：

```python
    @api.model
    def _ensure_ha_smart_home_redirect_mode(self) -> None:
        """Idempotently migrate the seeded HA client to host-flexible callback
        matching. ``oauth_clients.xml`` is ``noupdate="1"`` (sticky), so the
        seed change to redirect_uri_match_mode / redirect_uris never reaches
        existing databases. This healer is wired into both:

        - ``hooks.py:post_init_hook`` — fresh install path.
        - ``data/oauth_redirect_mode_update.xml`` ``<function>`` — every -u.

        Removes the legacy ``my.home-assistant.io`` value (route one drops the
        relay entirely; not a superset). require_pkce is already True on this
        seeded client, so the ha_callback PKCE invariant holds.

        NB: in ha_callback mode ``redirect_uris`` does NOT participate in host
        matching (validation uses HA_CALLBACK_PATH + scheme/host class), so
        overwriting it to the path constant is intentional and loses nothing.
        To allow extra hosts, change _is_local_host — not redirect_uris.
        """
        client = self.env.ref(
            'woow_paas_platform.oauth_client_ha_smart_home',
            raise_if_not_found=False,
        )
        if not client:
            return
        target = self.HA_CALLBACK_PATH
        if (client.redirect_uri_match_mode != 'ha_callback'
                or client.redirect_uris != target):
            client.write({
                'redirect_uri_match_mode': 'ha_callback',
                'redirect_uris': target,
            })
```

- [ ] **Step 3b: 建立 healer data XML**

Create `src/data/oauth_redirect_mode_update.xml`：

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- noupdate="0" (default): the <function> re-executes on every -u,
         independent of the noupdate flag on the seed record it touches.
         oauth_clients.xml is noupdate="1", so editing the seed's
         redirect_uri_match_mode / redirect_uris only reaches *fresh* installs;
         existing DBs (sandbox/stg/prod run -u) never see it from XML. The
         idempotent merge lives on the model and is also called from
         post_init_hook for the fresh-install path. See pitfall
         docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md. -->
    <function
        model="woow_paas_platform.oauth_client"
        name="_ensure_ha_smart_home_redirect_mode"/>
</odoo>
```

- [ ] **Step 3c: 註冊到 manifest**

`src/__manifest__.py` 的 `data` 清單中，緊接 `'data/oauth_scope_updates.xml'` 之後加一行：

```python
        'data/oauth_redirect_mode_update.xml',
```

（若清單該行寫法不同，以「在 `oauth_scope_updates.xml` 條目下方插入同格式條目」為準。）

- [ ] **Step 3d: 接到 fresh-install hook**

`src/hooks.py`：在 `_ensure_ha_client_sa_scopes`（`:172-179`）之後新增 helper：

```python
def _ensure_ha_client_redirect_mode(env):
    """Migrate the HA public client to host-flexible ha_callback redirect
    matching (fresh-install path). Upgrade path is covered by
    data/oauth_redirect_mode_update.xml. See seed-noupdate-sticky pitfall."""
    env['woow_paas_platform.oauth_client']._ensure_ha_smart_home_redirect_mode()
```

並在 `post_init_hook` 中（`:186` `_ensure_ha_client_sa_scopes(env)` 那行之後）加一行：

```python
    _ensure_ha_client_redirect_mode(env)
```

- [ ] **Step 4: 跑測試確認 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestHaRedirectModeHealer --stop-after-init`
Expected: PASS（flip + idempotent 兩 test）

- [ ] **Step 5: Commit**

```bash
git add src/models/oauth_client.py src/data/oauth_redirect_mode_update.xml src/__manifest__.py src/hooks.py src/tests/test_oauth2.py
git commit -m "feat: 新增 HA redirect 模式 healer 覆蓋既有 DB 升級路徑"
```

---

### Task 4: 更新 seed 與其斷言

**Files:**
- Modify: `src/data/oauth_clients.xml`（`:14-15`）
- Test: `src/tests/test_oauth2.py`（`test_seeded_ha_smart_home_client` `:285`、`TestPkceOnlyTokenFlow.REDIRECT_URI` `:306`）

**Interfaces:**
- Consumes: `redirect_uri_match_mode`、`HA_CALLBACK_PATH`（Task 1）
- Produces: 無（資料 + 斷言對齊）

- [ ] **Step 1: 更新 seed XML**

`src/data/oauth_clients.xml` 的 HA record，`require_pkce` 行之後加 mode 欄位，並把 `redirect_uris` 值改成 path（移除 my.home-assistant.io）。把 `:14-15`：

```xml
            <field name="require_pkce" eval="True"/>
            <field name="redirect_uris">https://my.home-assistant.io/redirect/oauth</field>
```

改為：

```xml
            <field name="require_pkce" eval="True"/>
            <field name="redirect_uri_match_mode">ha_callback</field>
            <field name="redirect_uris">/auth/external/woow/callback</field>
```

同步更新該 record 上方註解（`:7-9`）的 redirect_uri 說明：

```xml
        <!-- Home Assistant Smart Home Integration (public client + PKCE-only).
             client_id is fixed because HA custom component hardcodes it in const.py.
             redirect_uri uses ha_callback mode: host-flexible, path locked to
             /auth/external/woow/callback (the component's own view). my.home-assistant.io
             is intentionally removed (route one drops the relay). In ha_callback mode the
             redirect_uris value below is a display/record placeholder — host matching
             uses the path constant + scheme/host class, not this field. -->
```

- [ ] **Step 2: 更新既有 seed 斷言（先看它現在斷言什麼）**

`test_seeded_ha_smart_home_client`（`:285-301`）目前斷言 `redirect_uris == 'https://my.home-assistant.io/redirect/oauth'`。把該斷言（`:298` 附近）改為：

```python
        self.assertEqual(client.redirect_uri_match_mode, 'ha_callback')
        self.assertEqual(client.redirect_uris, '/auth/external/woow/callback')
        # The relay value must be gone (route one消除 my.home-assistant.io).
        self.assertNotIn('my.home-assistant.io', client.redirect_uris or '')
```

（保留該 test 既有的 `client_type=='public'`、`require_pkce` 斷言不動。）

`TestPkceOnlyTokenFlow` 的 `REDIRECT_URI`（`:306`）是該 class **自建** client 用的值，其自建 client 為預設 `exact` 模式（`:317` `redirect_uris=self.REDIRECT_URI`），故 `https://my.home-assistant.io/redirect/oauth` 對 exact client 仍成立、**不需改動**；保留以維持既有 token-flow 覆蓋。

- [ ] **Step 3: 跑測試確認 seed 與 flow 測試 pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestPublicClientConstraints,/woow_paas_platform:TestPkceOnlyTokenFlow --stop-after-init`
Expected: PASS（seed 斷言更新後綠；token-flow 不受影響）

- [ ] **Step 4: 全模組測試回歸**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform --stop-after-init`
Expected: PASS（全綠，無回歸）

- [ ] **Step 5: Commit**

```bash
git add src/data/oauth_clients.xml src/tests/test_oauth2.py
git commit -m "feat: seed HA client 改用 ha_callback 模式並移除 my.home-assistant.io"
```

---

### Task 5: 更新 API reference doc

**Files:**
- Modify: `docs/reference/api/ha-component-integration.md`（§1.1 表格 redirect_uri 行 + 其下 Seed 註記）

**Interfaces:**
- Consumes: 無
- Produces: 無（文件對齊；reference 類，無 test）

- [ ] **Step 1: 更新 §1.1 表格的 redirect_uri 行**

把表格中（約 `:48`）：

```markdown
| `redirect_uri`    | `https://my.home-assistant.io/redirect/oauth`（HA 標準 callback，精確比對）|
```

改為：

```markdown
| `redirect_uri`    | host-flexible：`{使用者本機/Nabu Casa/自訂網域 HA base}/auth/external/woow/callback`（path 固定，host 不限；`redirect_uri_match_mode=ha_callback`）|
| 比對規則          | scheme∈{http,https}、path 嚴格 `=/auth/external/woow/callback`、拒 query/fragment/userinfo；http 僅限 loopback/私有/`.local`，公網強制 https（RFC 8252）；open-redirect 殘餘面由 PKCE + 一次性 code 兜底 |
```

- [ ] **Step 2: 更新 Seed 註記段**

把表格下方提到 redirect_uri 的 Seed 段落（`:55-57` 附近）補一句，說明 sticky-noupdate 由 healer 覆蓋：

```markdown
Seed 記錄見 `src/data/oauth_clients.xml`（`noupdate="1"`，僅 fresh install
落地）。redirect_uri / match-mode 的既有 DB 升級由 `data/oauth_redirect_mode_update.xml`
的 `<function>` healer（`oauth_client._ensure_ha_smart_home_redirect_mode`）覆蓋；
既有 DB 的 scope 補丁見 §1.2 註記。
```

- [ ] **Step 3: lint 連結檢查（若本機有 lychee）**

Run: `lychee docs/reference/api/ha-component-integration.md || true`
Expected: 無 broken link（Phase 1 僅 warning，不阻擋）

- [ ] **Step 4: Commit**

```bash
git add docs/reference/api/ha-component-integration.md
git commit -m "docs: 更新 HA 串接 redirect_uri 契約為 ha_callback host-flexible"
```

---

## Self-Review

**1. Spec coverage（5 點拍板 → task 對照）：**
- 點 1（path 常數）→ Task 1 `HA_CALLBACK_PATH` ✓
- 點 2（RFC 8252 http 範圍）→ Task 1 `_is_local_host` + `_check_ha_callback_uri` scheme 分支 ✓
- 點 3（拒 query/fragment）→ Task 1 `_check_ha_callback_uri` ✓（並加碼拒 userinfo）
- 點 4（移除舊值）→ Task 3 healer + Task 4 seed；prod lock-step → Rollout 節 open question ✓
- 點 5（Selection flag）→ Task 1 `redirect_uri_match_mode` ✓
- 附帶（doc 更新）→ Task 5 ✓；（不 bump / 走 healer）→ Global Constraints + Task 3 ✓
- 安全不變量（ha_callback⇒PKCE）→ Task 2 ✓
- controller 零改 → 全程無 `oauth2.py` 改動 task ✓

**2. Placeholder scan：** 無 TBD/TODO；每個 code step 含完整可貼上的 code 與確切指令。host 分類、驗證矩陣、healer、seed、doc 皆給實值。✓

**3. Type consistency：** `redirect_uri_match_mode`（值 `'exact'`/`'ha_callback'`）、`HA_CALLBACK_PATH`、`_is_local_host`、`_check_ha_callback_uri`、`_ensure_ha_smart_home_redirect_mode` 在 Task 1→4 命名一致；`check_redirect_uri` 簽章不變（caller 不需改）。✓

**4. 風險覆蓋：** sticky-noupdate（healer 覆蓋 + 引用 pitfall）；OWL/controller 無涉；無新依賴；prod breaking 已標 lock-step open question。

## 待 `ha-paas-component` / Eugene 回覆的 open question（不阻擋本側實作，發版前須結）

1. **prod 是否已有真實 HA 使用者綁 `my.home-assistant.io`？** 決定 prod paas↔component 發版窗口與是否需短暫過渡。
2. **`http` 對「公網但使用者自簽 / 內網自訂網域」**：目前規則「公網 host 一律 https」。若有使用者在私有網段用自訂網域（DNS 指私有 IP 但網域非 `.local`），會被判公網而要求 https。需確認此情境是否存在（如存在，可加「DNS 私有不可由 redirect_uri 字面判定」的說明，建議該類使用者用 IP 或 `.local`）。
