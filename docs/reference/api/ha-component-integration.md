---
title: HA Component 串接 API — 概念 / OAuth / Smart Home
category: reference
status: implemented (Odoo side, stg E2E 全綠) / future (component side)
audience: woow_paas_smart_home HomeAssistant custom component team
source-of-truth:
  - src/controllers/oauth2.py（OAuth 2.0 provider：authorize / token / introspect / revoke）
  - src/controllers/ha_api.py（HAIntegrationController：/api/smarthome/* 與 /api/security-access/*）
related:
  - docs/reference/api/security-access-ha-integration.md（Security Access 端點權威規格）
  - docs/guides/development/oauth2-architecture.md（OAuth provider 內部實作架構）
  - src/models/smart_home.py（smart_home.to_dict）
  - src/models/oauth_client.py（public client 驗證、scope 解析）
  - src/data/oauth_clients.xml（public client `woow-ha-smart-home` 的 seed）
  - docs/archived/reference/woowtech-paas-api-specification.md（早期 API 設計稿，已 archived；現行端點以 src/controllers @route 為準）
---

# HA Component 串接 API — 概念 / OAuth / Smart Home

對象：HomeAssistant custom component `woow_paas_smart_home` 的開發者。

本文件是 **PaaS 端 contract**（Odoo 自寫 OAuth 2.0 provider + REST API），不修改
component 原始碼。所有 endpoint 與 JSON response shape 以 `src/controllers/` 的實作
為準，不是設計稿的想像值。

文件分工（避免同一塊內容兩份漂移）：

| 內容                                | 在哪份文件                                                            |
| ----------------------------------- | -------------------------------------------------------------------- |
| 概念 / OAuth 2.0 / PKCE / 串接總覽  | **本文件 §1–§3**                                                      |
| Smart Home REST API（5 端點 + shape）| **本文件 §4**                                                        |
| Security Access REST API（權威規格）| [`security-access-ha-integration.md`](security-access-ha-integration.md)（本文件 §5 只放速覽 + 指標） |
| OAuth provider 內部 model 架構      | [`oauth2-architecture.md`](../../guides/development/oauth2-architecture.md)（內部視角，非開發者 how-to） |

---

## 1. OAuth 2.0（PKCE-only public client）

HA component 以 OAuth 2.0 Authorization Code + PKCE 取得 access token，再以
`Authorization: Bearer <access_token>` 呼叫 REST API。

### 1.1 Client 規格

| 項目              | 值                                                                       |
| ----------------- | ------------------------------------------------------------------------ |
| `client_id`       | `woow-ha-smart-home`（HA component 硬編在 `const.py`，固定不可改）        |
| Client type       | **public**（`client_type=public`）—— **無 `client_secret`**              |
| PKCE              | **S256 強制**（public client 拒 `plain`、拒缺 `code_challenge`）          |
| Grant types       | `authorization_code`、`refresh_token`                                     |
| `redirect_uri`    | host-flexible：`{本機 / Nabu Casa / 自訂網域 HA base}/auth/external/woow/callback`（path 固定、host 不限；`redirect_uri_match_mode=ha_callback`，由 component 自家 view 渲染方向 A 品牌頁）|
| 比對規則          | scheme∈{http,https}、path 嚴格 `= /auth/external/woow/callback`、拒 query/fragment/userinfo；http 僅限 loopback/私有/`.local`，公網強制 https（RFC 8252）；open-redirect 殘餘面由 PKCE + 一次性 code 兜底 |
| Scopes（seed）    | 見 §1.2                                                                   |

> public client **不得**在 `/oauth2/token` 帶 `client_secret`：帶了會被
> `_authenticate_client` 直接拒絕（`src/controllers/oauth2.py:382`）。PKCE 的
> `code_verifier` 取代 secret 作為持有證明。

Seed 記錄見 `src/data/oauth_clients.xml`（`noupdate="1"`，僅 fresh install
落地）。`redirect_uri_match_mode` / `redirect_uris` 的既有 DB 升級由
`data/oauth_redirect_mode_update.xml` 的 `<function>` healer
（`oauth_client._ensure_ha_smart_home_redirect_mode`）覆蓋；既有 DB 的 scope
補丁見 §1.2 註記。`redirect_uri` 比對放寬僅限本 client（`woow-ha-smart-home`
→ `ha_callback` 模式），其餘 client 維持 `exact` 精確比對。

### 1.2 Scopes

public client `woow-ha-smart-home` 的 `scopes` 欄位實際內容（5 個）：

```
smarthome:read smarthome:tunnel workspace:read security-access:read security-access:tunnel
```

| Scope                    | 授權範圍                                                          | 用於                    |
| ------------------------ | ---------------------------------------------------------------- | ----------------------- |
| `workspace:read`         | 列舉使用者可存取的 workspace（SH / SA 共用）                      | SH + SA workspace 端點  |
| `smarthome:read`         | 讀取 Smart Home 清單 / detail / 狀態                              | Smart Home              |
| `smarthome:tunnel`       | 取得 Smart Home 的 tunnel token（高權限，獨立端點，列表絕不夾帶） | Smart Home              |
| `security-access:read`   | 讀取 Security Access 清單 / detail / 狀態 / 方案資訊              | Security Access         |
| `security-access:tunnel` | 取得 Security Access 的 tunnel token（高權限，獨立端點）          | Security Access         |

Smart Home 功能用前 3 個（`smarthome:read` / `smarthome:tunnel` /
`workspace:read`）；Security Access 功能用 `workspace:read` +
`security-access:read` / `security-access:tunnel`。

> **既有 DB 的 SA scope 補丁**：`oauth_clients.xml` 是 `noupdate="1"`，編輯 seed
> 的 scopes 只影響**全新安裝**。已存在的 DB（sandbox / stg / prod 都跑 `-u`）
> 不會從 XML 拿到新加的 `security-access:*` scope。因此
> `src/data/oauth_scope_updates.xml` 在每次升級呼叫
> `oauth_client._ensure_ha_smart_home_sa_scopes`（idempotent）把兩個 SA scope
> 併入既有 client；`post_init_hook` 也對 fresh-install 路徑呼叫一次。少了這兩個
> scope，HA component 對 SA 端點的 OAuth token 會缺權限。

### 1.3 Authorize → Token（PKCE 流程，RFC 7636）

```
① component 端產生 code_verifier（隨機）→ 算 code_challenge = BASE64URL(SHA256(verifier))

② Authorize（user agent 開瀏覽器導向）
   GET {BASE}/oauth2/authorize
     ?response_type=code
     &client_id=woow-ha-smart-home
     &redirect_uri=https://my.home-assistant.io/redirect/oauth
     &scope=smarthome:read smarthome:tunnel workspace:read
     &state=<csrf-random>
     &code_challenge=<challenge>
     &code_challenge_method=S256
   → 使用者登入 + 同意 → 302 回 redirect_uri?code=<auth_code>&state=<同上>

③ Token（component 後端 POST，無瀏覽器）
   POST {BASE}/oauth2/token
     grant_type=authorization_code
     code=<auth_code>
     redirect_uri=https://my.home-assistant.io/redirect/oauth
     client_id=woow-ha-smart-home
     code_verifier=<verifier>        # 不帶 client_secret
   → 200 JSON token（見下）
```

PKCE 強制點（`src/controllers/oauth2.py`）：

- authorize GET / POST：public client 缺 `code_challenge` → reject；
  `code_challenge_method != 'S256'` → reject（拒 `plain`）。
- token：public client 的 auth_code 必須帶 `code_challenge`，且方法必須 `S256`，
  否則拒（防降級）；最後以 `code_verifier` 比對 `code_challenge`。

### 1.4 Token 回應 shape

`POST /oauth2/token` 成功（`200`）回：

```json
{
  "access_token": "<opaque>",
  "token_type": "bearer",
  "expires_in": 3600,
  "scope": "smarthome:read smarthome:tunnel workspace:read",
  "refresh_token": "<opaque>"
}
```

| 欄位            | 說明                                                                |
| --------------- | ------------------------------------------------------------------- |
| `access_token`  | Bearer token，有效 **1 小時**（`ACCESS_TOKEN_LIFETIME`）             |
| `token_type`    | 固定字串 `"bearer"`（小寫）                                          |
| `expires_in`    | access token 秒數（`3600`）                                          |
| `scope`         | 實際授予的 scope（空白分隔）                                         |
| `refresh_token` | refresh token，有效 **30 天**（`REFRESH_TOKEN_LIFETIME`）            |

回應帶 `Cache-Control: no-store`。

### 1.5 Refresh（rotation）

access token 過期後，用 refresh token 換新 pair：

```
POST {BASE}/oauth2/token
  grant_type=refresh_token
  refresh_token=<old_refresh_token>
  client_id=woow-ha-smart-home    # public client，不帶 client_secret
```

- **Rotation**：每次 refresh 都會**撤銷舊 token**並發新的 access + refresh pair。
  component 端必須以回應裡的新 `refresh_token` 取代舊值。
- **Scope 不變**：refresh 沿用原 token 的 scope，**不會改變授權範圍**
  （`src/controllers/oauth2.py:573` 以 `token.scope` 重發）。
- 舊 refresh token 過期 / 已撤銷 → `400 {"error":"invalid_grant", ...}`。

### 1.6 既有 entry 的 reauth 行為

token 是 **per-entry** 的，不強制全體 reauth：

- 新增 config entry 走正常 authorize，拿到的是當下 client seed 的全部 scope。
- 既有 entry 保留舊 scope（refresh 不改 scope），照常運作。
- 某 entry 的 scope 不足、打到無權端點 → `403`。component 端的
  `403 → AuthenticationError → ConfigEntryAuthFailed` safety net 會自動觸發
  **該 entry** 的 reauth（取得更新後 scope），不波及其他 entry。

---

## 2. 串接總覽

HA component 對 Smart Home 與 Security Access 的串接模型幾乎同形：

```
  ① OAuth 2.0 (PKCE) 取得 access token
        │
  ② GET workspaces        → 選一個 workspace（使用者可存取的全部 active workspace）
        │
  ③ GET 該 workspace 的實例 → 選一個 Smart Home（home）或 Security Access（access）
        │
  ④ GET 該實例的 tunnel-token → 取得 Cloudflare tunnel token
        │
  ⑤ cloudflared tunnel run --token <tunnel_token>   ← 在使用者端啟 tunnel
        │
  ⑥ coordinator 每實例每 30s 打 /status            ← 輪詢狀態（sensor 更新）
```

兩產品差別只在：(a) 端點 prefix（`/api/smarthome/*` vs
`/api/security-access/*`）、(b) scope、(c) tunnel-token 的回傳形狀（SA 無單一
hostname）、(d) SA 多 `active_no_route` 狀態。詳見 §5 與 SA 專文。

> **workspace 不綁產品**：`/api/smarthome/workspaces` 與
> `/api/security-access/workspaces` 回傳**同一組** workspace ——
> 即使用者所屬的全部 active workspace，與選哪個產品無關（stg live E2E 已實證兩
> 端點回相同集合）。選 workspace 後才依產品分流到 `/homes` 或 `/accesses`。

### config entry unique_id 建議

同一 workspace 可同時配 Smart Home + Security Access，兩產品的實例 ID 各自獨立
（可能同值）。config entry 的 `unique_id` 必須含 service type 避免相撞：

```python
unique_id = f"{workspace_id}_{service_type}_{service_id}"
# service_type ∈ {"smarthome", "security_access"}
```

---

## 3. REST API 共通約定

所有 `/api/smarthome/*` 與 `/api/security-access/*` 端點：

- `type=http`、`methods=['GET']`、`csrf=False`，回**標準 JSON**（非 Odoo JSON-RPC）。
- `auth='none'`，實際認證由 `_authenticate()` → `verify_oauth_token()` 驗 Bearer
  + required scopes（`src/controllers/ha_api.py`）。
- 路徑參數 `{workspace_id}` / `{id}` 為資料庫整數 ID。

請求：

```
Authorization: Bearer <access_token>
```

### 3.1 錯誤模型

| 狀況                         | HTTP | body                                                                |
| ---------------------------- | ---- | ------------------------------------------------------------------- |
| Bearer 缺失 / 失效 / 過期    | 401  | `{"error":"Unauthorized","detail":"..."}`                            |
| scope 不足                   | 403  | `{"error":"Forbidden","detail":"..."}`                               |
| workspace 無 access          | 403  | `{"error":"Forbidden","detail":"..."}`（detail 字串依端點而異）       |
| 實例不存在 / 跨 workspace    | 404  | `{"error":"Not Found","detail":"..."}`                               |

> component 端建議的映射：`401/403 → AuthenticationError`（觸發該 entry
> reauth）、`404 → NotFoundError`、其他 → `ApiError`。body 可能是
> `{"error","detail"}`（HA API 端點，見上）或 OAuth 端點的 `{"error","error_description"}`，
> 解析時兩者皆容忍。

### 3.2 共通 enum

| enum            | 值                                                  | 備註                                  |
| --------------- | -------------------------------------------------- | ------------------------------------- |
| `tunnel_status` | `connected` / `disconnected` / `error`             | **無 `unknown`**；空值預設回 `disconnected` |
| Smart Home `state` | `pending` / `provisioning` / `active` / `error` / `suspended` / `deleting` |                          |
| Security Access `state` | 同上 + `active_no_route`                    | `active_no_route` = tunnel 已起但 0 route（catch-all 404） |

---

## 4. Smart Home REST API

全部 `GET` / Bearer / JSON。Handler 在 `src/controllers/ha_api.py`
（`HAIntegrationController`），逐字以實作為準。

| Path                                            | Scope               | Handler                  |
| ----------------------------------------------- | ------------------- | ------------------------ |
| `GET /api/smarthome/workspaces`                 | `workspace:read`    | `list_workspaces`        |
| `GET /api/smarthome/workspaces/{ws}/homes`      | `smarthome:read`    | `list_homes`             |
| `GET /api/smarthome/homes/{id}`                 | `smarthome:read`    | `get_home`               |
| `GET /api/smarthome/homes/{id}/tunnel-token`    | `smarthome:tunnel`  | `get_tunnel_token`       |
| `GET /api/smarthome/homes/{id}/status`          | `smarthome:read`    | `get_home_status`        |

### 4.1 `GET /api/smarthome/workspaces`

列出已認證使用者可存取的 **active** workspace。

```json
{
  "workspaces": [
    { "id": 12, "name": "My Workspace", "slug": "my-workspace" }
  ]
}
```

`slug` 缺值為 `""`。

### 4.2 `GET /api/smarthome/workspaces/{ws}/homes`

列出某 workspace 下的 Smart Home（精簡列表）。workspace 無 access → `403`
（`{"error":"Forbidden","detail":"No access to this workspace"}`）。

```json
{
  "homes": [
    {
      "id": 7,
      "name": "Home Assistant",
      "state": "active",
      "subdomain": "my-home",
      "tunnel_status": "connected"
    }
  ]
}
```

> 列表的 `subdomain` 是 prefix（非 FQDN）；完整 FQDN 在 detail / tunnel-token。

### 4.3 `GET /api/smarthome/homes/{id}`

單筆完整 detail，body 為 `smart_home.to_dict()`（`src/models/smart_home.py` 的
`to_dict`）。不存在 → `404`、跨 workspace → `403`。

```json
{
  "home": {
    "id": 7,
    "name": "Home Assistant",
    "workspace_id": 12,
    "state": "active",
    "tunnel_id": "f3a1...",
    "tunnel_name": "paas-sh-...",
    "tunnel_route": "...",
    "tunnel_status": "connected",
    "tunnel_uptime": "2026-06-06T08:30:00Z",
    "connector_id": "c0ffee...",
    "connector_type": "cloudflared",
    "ha_port": 8123,
    "subdomain": "my-home",
    "error_message": "",
    "deployed_at": "2026-06-06T08:30:00",
    "created_at": "2026-06-06T08:29:41",
    "subscription": {
      "id": 42,
      "state": "active",
      "plan_name": "Smart Home Pro",
      "plan_price": 99.0,
      "is_trial": false,
      "trial_end_date": null,
      "start_date": "2026-06-06",
      "end_date": null,
      "next_payment": "2026-07-06",
      "last_payment_status": "paid"
    },
    "mcp": "ha_mcp_tools"
  }
}
```

注意：

- `tunnel_token` **不在** `to_dict()`（欄位受 `groups='base.group_system'`
  保護），detail / 列表絕不洩漏，只走 §4.4 專用端點。
- `tunnel_uptime` 是 connector 連線「開啟時間」的 ISO timestamp（Cloudflare
  `opened_at`），**不是已算好的時長字串**；要 uptime 時長由 consumer 端自行
  `now() - opened_at`。
- `subscription` 為 `_get_subscription_info()`；**無訂閱時整個 `subscription`
  為 `null`**（非 `{}`）。
- `subdomain` 是 prefix；對外 FQDN = `{subdomain}.{paas_domain}`，由 §4.4
  tunnel-token 端點組好回傳。
- `mcp`（2026-07 補錄，#270）：字串，值為 `""` 或 `"ha_mcp_tools"`，來自
  active plan 的 `mcp_integration`（`smart_home.to_dict()` →
  `_get_mcp_integration()`，`src/models/smart_home.py:320`；欄位定義
  `src/models/sh_subscription_plan_sh_mcp.py:14`）。無訂閱 / plan 無旗標
  一律回 `""`（非 `null`、非缺欄位）。語意見 §4.4 的 reconcile 說明。

### 4.4 `GET /api/smarthome/homes/{id}/tunnel-token`

取得 tunnel 連線憑證。**獨立的高權限端點**（scope `smarthome:tunnel`），與
列表 / detail 分權。

```json
{
  "tunnel_token": "eyJ...",
  "tunnel_id": "f3a1...",
  "subdomain": "my-home.paas.example.com",
  "mcp": "ha_mcp_tools"
}
```

> 這裡的 `subdomain` 是**完整 FQDN**（`{prefix}.{paas_domain}`，controller 組好），
> 與 detail 裡的 `subdomain`（prefix）不同。各欄缺值皆回 `""`。

`mcp` 欄位（2026-07 補錄，#270；`src/controllers/ha_api.py:251`）與 §4.3
`to_dict()` 同源同值。component 端契約＝**每次同步 reconcile 到當前值**
（冪等收斂，非一次性建立）：

- `mcp == "ha_mcp_tools"` → 確保存在一顆 MCP sensor entity，狀態三態
  （未安裝 / 已安裝未啟動 / 運作中）。
- `mcp == ""`（或缺欄位）→ 確保 sensor 不存在——**含先前建過的移除路徑**
  （降頻、退訂、換非 MCP 檔）；平台每次同步廣播當前值，不另發「取消」信號。
- sensor 純 HA 本地 entity；**平台不回讀 component 狀態**（軟性、無 enforcement）。

設計依據：[`../../plans/2026-Q3/2026-07-09-smart-home-mcp-design.md`](../../plans/2026-Q3/2026-07-09-smart-home-mcp-design.md) §4.2、§5。

啟動：

```bash
cloudflared tunnel run --token <tunnel_token>
```

### 4.5 `GET /api/smarthome/homes/{id}/status`

刷新並回即時狀態。controller 先呼叫 `home.action_refresh_status()`（向 operator
拉最新 tunnel/connector 狀態），再回完整 `to_dict()`（同 §4.3，差別是
`tunnel_status` / `tunnel_uptime` 等已刷新）。

- `action_refresh_status()` 拋例外時：**仍回 `200`** + 當前 `to_dict()`
  （model 失敗時會把 `tunnel_status` 更新為 `error`），controller 只記 log
  不轉成 5xx。
- coordinator 每 home 每 **30s** 打此端點更新 sensor。

---

## 5. Security Access REST API（速覽）

> **完整權威規格在
> [`security-access-ha-integration.md`](security-access-ha-integration.md)**
> （端點、完整 `to_dict()` shape、route / access_policy / subscription 結構、
> 錯誤碼、與 SH 差異速查）。本節只放速覽，不重複 shape 細節，避免漂移。

HA component 多支援一種產品 Security Access（SA）：選完 workspace 後，除了
Smart Home，可多選 SA。SA 同樣是「取 Cloudflare tunnel token → 跑 cloudflared
啟 tunnel」，與 Smart Home 同形。

| Path                                                  | Scope                    |
| ----------------------------------------------------- | ------------------------ |
| `GET /api/security-access/workspaces`                 | `workspace:read`         |
| `GET /api/security-access/workspaces/{ws}/accesses`   | `security-access:read`   |
| `GET /api/security-access/accesses/{id}`              | `security-access:read`   |
| `GET /api/security-access/accesses/{id}/tunnel-token` | `security-access:tunnel` |
| `GET /api/security-access/accesses/{id}/status`       | `security-access:read`   |

與 Smart Home 的 4 個關鍵差異：

1. **tunnel-token 不含 hostname**：SH 版回 `{tunnel_token, tunnel_id, subdomain,
   mcp}`（含 FQDN 與 MCP 旗標，§4.4）；SA 版只回 `{tunnel_token, tunnel_id}`
   （無 `mcp`）。SA 是一條 bare Cloudflare
   Tunnel + N 條 application route，沒有單一 hostname。要顯示主機名 → 讀 SA
   detail 的 `routes[].hostname`。
2. **啟 tunnel 方式完全相同**（`cloudflared tunnel run --token <token>`）。SA 是
   remotely-managed tunnel：ingress（N route）由 paas-operator server-side 宣告式
   管理、從 CF edge 下發；connector 只需 token、不需本機 config。
3. **多 `active_no_route` 狀態**：tunnel 已 provision 但 0 route（catch-all 404），
   與 `active`（≥1 route）區分。
4. **`tunnel_token` 受 `base.group_system` 保護**，只在 tunnel-token 端點
   （`security-access:tunnel` scope）出現，列表 / detail 絕不外洩。

---

## 附錄 A：在環境中建測試資料（可選）

E2E 驗證 / 開發時若需要可串接的 workspace + 實例 + 訂閱，請走
`/woow-dev:k8s-dev-sandbox` 沙盒並用 repo 自帶 seed hook，或參考
[`woowtech-paas-api-specification.md`](../../archived/reference/woowtech-paas-api-specification.md)（歷史設計稿） 的對應端點
手動建立。本文件只描述 API contract，不固定 seed 腳本（易腐爛）。
