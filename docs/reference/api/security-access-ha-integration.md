---
title: Security Access — HA Component 串接 API
category: reference
status: implemented (Odoo side, stg E2E 全綠) / future (component side)
audience: woow_paas_smart_home HomeAssistant custom component team
source-of-truth: src/controllers/ha_api.py（HAIntegrationController 的 /api/security-access/* 路由）
related:
  - docs/reference/api/ha-component-integration.md（HA component 串接總覽 + 概念/OAuth + Smart Home API）
  - src/models/security_access.py（to_dict / _get_subscription_info / SecurityAccessRoute.to_dict）
  - src/models/security_access_access_policy.py（access_policy to_dict）
  - src/models/subscription_lifecycle_mixin.py（_get_subscription_info）
  - src/data/oauth_clients.xml（public client `woow-ha-smart-home` 的 scopes）
  - src/data/oauth_scope_updates.xml（既有 DB 的 SA scope 補丁 _ensure_ha_smart_home_sa_scopes）
  - docs/archived/reference/woowtech-paas-api-specification.md（早期 API 設計稿，已 archived；現行端點以 src/controllers @route 為準）
  - docs/decisions/patterns/su-env-exempt-guard-false-green.md（route subdomain_prefix 不可變 guard 的內部視角）
  - docs/decisions/refactor/security-access-bare-tunnel-zero-route.md（bare tunnel + N route 架構與 route 生命週期）
---

# Security Access — HA Component 串接 API

> **這是 Security Access 端點的權威規格。** 概念（OAuth 2.0 / PKCE）、整體串接
> 流程、與 Smart Home REST API 請見
> [`ha-component-integration.md`](ha-component-integration.md)；本文件只專注 SA
> 端點與其完整 response shape，避免兩份文件對同一塊內容漂移。

對象：`woow_paas_smart_home` HomeAssistant custom component 的未來擴展
（component 將支援從 Smart Home 或 Security Access 選擇服務來源）。

本文件是 **PaaS 端 contract**；**不修改 component 原始碼**。endpoint 與 JSON
response shape 全部以 `src/controllers/ha_api.py` 的實作為準（每筆 model 物件
皆走 `to_dict()`），不是 plan 的想像值。

Security Access 端點是 Smart Home 端點的鏡像：相同認證模型、相同 workspace
隔離邏輯（`_check_workspace_access`）、相同的 tunnel-token 分權設計。差別只在
資源型別（`woow_paas_platform.security_access` vs `smart_home`）與 scope 名。

> **命名慣例（範例值）**：本文件 JSON 範例的 `canonical_name` 一律是
> `paas-sa-{ws_hash}-{res_hash}` 形式（見 `src/services/naming.py:
> make_security_access_subdomain`），例如 `paas-sa-7f3a2b9c-c1d4e9f0`。`tunnel_name`
> 等於 `canonical_name`（`action_provision` 以 `name=self.canonical_name`
> 建 tunnel）。範例 hash 為佔位，實際值由 workspace slug + reference_id + 暱稱
> 雜湊而得，請勿假設可讀字串。

## 1. 認證

OAuth 2.0 Bearer token。public client `woow-ha-smart-home`（PKCE-only，
`client_type=public`，redirect_uri 為 host-flexible 模式
`redirect_uri_match_mode=ha_callback`：path 嚴格鎖
`/auth/external/woow/callback`、host 不限〔本機 / Nabu Casa / 自訂網域 HA
base〕，詳見 [`ha-component-integration.md`](ha-component-integration.md) §1.1）。所有 SA 端點
`auth='none'`，由 `_authenticate()` → `verify_oauth_token()` 驗 Bearer +
required scopes。

```
Authorization: Bearer <access_token>
```

scope 不足回 `403 Forbidden`；token 缺失/失效回 `401 Unauthorized`，body 為
`{"error": "...", "detail": "..."}`（見 `_json_error`）。

### 1.1 Scopes（以 `src/data/oauth_clients.xml` 實際值為準）

public client `woow-ha-smart-home` 的 `scopes` 欄位實際內容：

```
smarthome:read smarthome:tunnel workspace:read security-access:read security-access:tunnel
```

Security Access 新增兩個 scope：

| Scope                    | 授權範圍                                                       |
| ------------------------ | -------------------------------------------------------------- |
| `security-access:read`   | 讀取 SA 服務清單 / detail / 狀態 / 方案資訊（FR-9 sensor 用）   |
| `security-access:tunnel` | 取得 tunnel token（高權限，獨立端點，列表/detail 絕不夾帶）     |

`workspace:read` 沿用既有 scope（SA 與 SH 共用 workspace 列舉端點語意）。

> **既有 DB 的 scope 補丁（G-H3）**：`oauth_clients.xml` 是 `noupdate="1"`，
> 編輯 seed 的 scopes 只影響**全新安裝**。已存在的 DB（sandbox / stg / prod
> 都跑 `-u`）不會從 XML 拿到新 scope。因此 `oauth_scope_updates.xml` 在每次
> 升級時呼叫 `oauth_client._ensure_ha_smart_home_sa_scopes`（idempotent）把
> `security-access:read` / `security-access:tunnel` 併入既有 client。
> `post_init_hook` 也對 fresh-install 路徑呼叫一次。少了這兩個 scope，HA
> component 的 OAuth token refresh 會壞。

## 2. Endpoints（與 /api/smarthome/* 對照）

| Security Access                                         | Smart Home 對應                                | Required Scope           |
| ------------------------------------------------------ | ---------------------------------------------- | ------------------------ |
| `GET /api/security-access/workspaces`                  | `GET /api/smarthome/workspaces`                | `workspace:read`         |
| `GET /api/security-access/workspaces/{ws}/accesses`    | `GET /api/smarthome/workspaces/{ws}/homes`     | `security-access:read`   |
| `GET /api/security-access/accesses/{id}`               | `GET /api/smarthome/homes/{id}`                | `security-access:read`   |
| `GET /api/security-access/accesses/{id}/tunnel-token`  | `GET /api/smarthome/homes/{id}/tunnel-token`   | `security-access:tunnel` |
| `GET /api/security-access/accesses/{id}/status`        | `GET /api/smarthome/homes/{id}/status`         | `security-access:read`   |

所有端點 `type=http`、`methods=['GET']`、`csrf=False`、回標準 JSON（非 Odoo
JSON-RPC）。`{ws}` / `{id}` 為資料庫整數 ID。

---

### 2.1 `GET /api/security-access/workspaces`

列出已認證使用者有 access 的 **active** workspace（`workspace_access` →
`workspace_id.state='active'`）。

- Scope：`workspace:read`
- Handler：`HAIntegrationController.sa_list_workspaces`

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://paas.example.com/api/security-access/workspaces
```

Response（`200`）：

```json
{
  "workspaces": [
    { "id": 12, "name": "My Workspace", "slug": "my-workspace" }
  ]
}
```

`slug` 缺值時為 `""`（empty string）。

---

### 2.2 `GET /api/security-access/workspaces/{ws}/accesses`

列出某 workspace 下的 Security Access 服務。回**精簡列表**：含 FR-9 sensor 所需
的 `plan_name` / `next_payment`，但**不含 tunnel_token**（只走 §2.4 專用端點）。

- Scope：`security-access:read`
- Handler：`HAIntegrationController.sa_list_accesses`
- workspace 無 access → `403`（`{"error":"Forbidden","detail":"No access to this workspace"}`）

每筆欄位直接取自 controller（非完整 `to_dict()`，是 list-tailored 子集）：

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://paas.example.com/api/security-access/workspaces/12/accesses
```

Response（`200`）：

```json
{
  "accesses": [
    {
      "id": 5,
      "name": "Front Door NVR",
      "canonical_name": "paas-sa-7f3a2b9c-c1d4e9f0",
      "state": "active",
      "tunnel_status": "connected",
      "plan_name": "Security Access Pro",
      "next_payment": "2026-07-06"
    }
  ]
}
```

> **無 `primary_hostname` / FQDN**：SA 是一條 bare Cloudflare Tunnel + N 條
> application route，沒有單一 hostname。列表只回 `canonical_name`（≠ FQDN，
> 是內部識別碼）。要顯示對外主機名 → 走 §2.3 detail 讀 `routes[].hostname`。

欄位說明（直接取自 `sa_list_accesses` controller，是 list-tailored 子集，
非完整 `to_dict()`）：

| 欄位             | 來源                                       | 備註                                                                       |
| ---------------- | ------------------------------------------ | -------------------------------------------------------------------------- |
| `canonical_name` | `security_access.canonical_name`           | 內部識別碼（`paas-sa-{ws_hash}-{res_hash}`），缺值為 `""`，**不是 FQDN**    |
| `state`          | `security_access.state`                     | `pending` / `provisioning` / `active` / `active_no_route` / `error` / `suspended` / `deleting` |
| `tunnel_status`  | `security_access.tunnel_status`             | `connected` / `disconnected` / `error`；預設 `disconnected`                 |
| `plan_name`      | `_get_subscription_info()['plan_name']`     | 無訂閱時整段 `plan_name` / `next_payment` 為 `null`                         |
| `next_payment`   | `_get_subscription_info()['next_payment']`  | ISO date（`sh_date_of_next_payment`），無則 `null`                          |

> 無訂閱（admin / legacy 建立）時 `_get_subscription_info()` 回 `None`，
> controller 將 `plan_name` 與 `next_payment` 都填 `null`。

---

### 2.3 `GET /api/security-access/accesses/{id}`

單筆 Security Access 的完整 detail，body 為 model 的完整 `to_dict()`。

- Scope：`security-access:read`
- Handler：`HAIntegrationController.sa_get_access` → `_get_sa_with_access`
- 不存在 → `404`（`{"error":"Not Found","detail":"Security access not found"}`）
- workspace 無 access → `403`

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://paas.example.com/api/security-access/accesses/5
```

Response（`200`，`security_access.to_dict()` 完整輸出，欄位順序以實作為準）：

```json
{
  "access": {
    "id": 5,
    "name": "Front Door NVR",
    "canonical_name": "paas-sa-7f3a2b9c-c1d4e9f0",
    "workspace_id": 12,
    "state": "active",
    "tunnel_id": "f3a1...",
    "tunnel_name": "paas-sa-7f3a2b9c-c1d4e9f0",
    "tunnel_status": "connected",
    "tunnel_uptime": "2026-06-06T08:30:00Z",
    "connector_id": "c0ffee...",
    "connector_type": "cloudflared",
    "connector_version": "2026.5.0",
    "edge_connection_count": 4,
    "error_message": "",
    "deployed_at": "2026-06-06T08:30:00",
    "created_at": "2026-06-06T08:29:41",
    "routes": [
      {
        "id": 9,
        "subdomain_prefix": "paas-sa-7f3a2b9c-c1d4e9f0",
        "hostname": "paas-sa-7f3a2b9c-c1d4e9f0.paas.example.com",
        "service_url": "http://localhost:80",
        "http_host_header": true,
        "is_protected": true,
        "session_duration": "24h",
        "login_google": true,
        "login_email_otp": false,
        "access_unresolved_idp_types": "",
        "access_policies": [
          {
            "id": 3,
            "name": "Allow team",
            "decision": "allow",
            "sequence": 10,
            "route_id": 9,
            "include": [{ "rule_type": "email_domain", "rule_value": "example.com" }],
            "require": [],
            "exclude": []
          }
        ]
      }
    ],
    "subscription": {
      "id": 42,
      "state": "active",
      "plan_name": "Security Access Pro",
      "plan_price": 199.0,
      "is_trial": false,
      "trial_end_date": null,
      "start_date": "2026-06-06",
      "end_date": null,
      "next_payment": "2026-07-06",
      "last_payment_status": "paid"
    }
  }
}
```

注意：

- **detail 沒有頂層 `primary_hostname` / FQDN 欄位**。SA 是 bare tunnel +
  N route 結構，對外主機名分散在每筆 `routes[].hostname`（compute =
  `{subdomain_prefix}.{paas_domain}`）。`canonical_name` 是內部識別碼，不是 FQDN。
- `tunnel_token` **不在** `to_dict()`（欄位 `groups='base.group_system'`），
  detail 端點不洩漏。要 tunnel 憑證走 §2.4。
- `tunnel_uptime` **不是已計算好的時長字串**，而是 connector 連線「開啟時間」
  的 ISO timestamp（Cloudflare `opened_at`，如 `2026-06-06T08:30:00Z`，
  原樣存入、原樣輸出）。要顯示 uptime 時長，由 consumer 端自行
  `now() - opened_at` 計算；勿把它當成 `"3h12m"` 之類的時長直接解析。
- `routes[]` 是每筆 `SecurityAccessRoute.to_dict()`（`src/models/security_access.py`
  的 `SecurityAccessRoute.to_dict`）。欄位：`id` / `subdomain_prefix` /
  `hostname`（compute）/ `service_url` / `http_host_header` / `is_protected` /
  `session_duration` / `login_google` / `login_email_otp` /
  `access_unresolved_idp_types` / `access_policies[]`。**無 `is_primary` 欄位**
  （P2.5 之後每條 route 都是平等的 application route，Access 設定下放到 route 層）。
- `routes[].access_policies[]` 是每筆 `security_access_access_policy.to_dict`：
  `id` / `name` / `decision`（`allow` / `deny` / `bypass` 等）/ `sequence` /
  `route_id` / `include[]` / `require[]` / `exclude[]`，三組各自為
  `[{rule_type, rule_value}]`（valueless selector 的 `rule_value` 為 `null`）。
- `access_unresolved_idp_types` 非空 = 該 route 上次同步時，要求的登入方式
  （如 google）在 Cloudflare 帳戶找不到對應 IdP，fail-open 靜默放寬，已被
  surface 出來讓 consumer 看得見。
- `subscription` 為 `_get_subscription_info()`；**無訂閱時整個 `subscription`
  為 `null`**（非 `{}`）。

#### 2.3.1 `routes[].id` 穩定性契約（消費端可依賴）

> **結論：route 的 `id` 在該 route 存活期間穩定，可安全當成 consumer 端 entity
> 的識別 key。唯一例外 = 改 `hostname` / `subdomain_prefix`（語意上等同砍掉重建，
> 會換新 `id`）。** 這是 PaaS 端的對外契約（已對照 codebase 驗證 @ develop /
> `feat/ha-component-apis`），HA component 以 route `id` 綁 entity unique_id 時依賴它。

**✅ in-place 編輯 = 同一筆 record、`id` 不變**：下列欄位的編輯都走「以 `id` 撈
既有 record → `write()` → 回傳同筆」，不重建、不換 `id`：

- `service_url`、`http_host_header`、`is_protected`、`session_duration`、
  `login_google`、`login_email_otp`
- `access_policies`（新增 / 修改 / 刪除 / 重排都不動 route `id`）

controller `_update_route`（`src/controllers/security_access.py:551`）以
`route_id` filter 既有 record 後 `route_rec.write(vals)`、回傳同筆；
access-config / policy CRUD 同理。`action_provision` / 重新 sync 也只把
`dns_record_id` / `access_app_id` **寫回既有 record**，不重建、不換 `id`。

**⚠️ 唯一例外：改 `hostname` / `subdomain_prefix` = 砍重建 = 新 `id`**。
`subdomain_prefix` 是**不可變 identity anchor**（hostname / DNS / ingress 全由它
推導），model `write()` 對**所有人（含 superuser）**硬擋：

```
UserError: Subdomain prefix is immutable. Delete the route and create a new one to rename.
```

（`src/models/security_access.py:1029-1036`）。`_update_route` 根本不接受
`subdomain_prefix` 欄位，前端也只在「新增 route」時可填 prefix、無 edit-prefix UI。
因此「改某條 route 的 hostname」在 API 上必然呈現為**舊 route 消失 + 新 route
出現（新 `id`）**，server 端**不提供**「new route 是 old route 的 rename」關聯訊號
（兩 `id` 無關聯）。

**消費端建議**：

- entity 的 unique_id 綁 route `id`（唯一穩定 key，涵蓋上述所有「真正算編輯」場景）。
- 改 route hostname 語意上本就是「換一個資源」，consumer 應預期該 route 對應的
  entity 歷史 / automation 參照會被重置（舊 entity 轉 unavailable、出現新 entity），
  並在 UI / docs 對使用者提示此行為。

> 此不可變設計的內部視角與「為何 guard 不能對 superuser 豁免」的教訓見
> [`su-env-exempt-guard-false-green.md`](../../decisions/patterns/su-env-exempt-guard-false-green.md)
> 與 [`security-access-bare-tunnel-zero-route.md`](../../decisions/refactor/security-access-bare-tunnel-zero-route.md)。

---

### 2.4 `GET /api/security-access/accesses/{id}/tunnel-token`

取得 tunnel 連線憑證。**獨立的高權限端點**，與列表/detail 分權（後者絕不夾帶
`tunnel_token`）。

- Scope：`security-access:tunnel`（elevated）
- Handler：`HAIntegrationController.sa_get_tunnel_token`
- 同樣經 `_get_sa_with_access`：不存在 `404`、workspace 無 access `403`

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://paas.example.com/api/security-access/accesses/5/tunnel-token
```

Response（`200`）：

```json
{
  "tunnel_token": "eyJ...",
  "tunnel_id": "f3a1..."
}
```

兩欄缺值皆回 `""`。

> **與 SH 的關鍵差異**：SH 版 tunnel-token 回 `{tunnel_token, tunnel_id, subdomain}`
> （`subdomain` 是單一 FQDN）；SA 版**只回 `tunnel_token` + `tunnel_id`，沒有
> hostname/FQDN**。SA 是一條 bare Cloudflare Tunnel + N 條 application route，
> 沒有單一 hostname 可回。要顯示對外主機名，請改從 §2.3 detail 的
> `routes[].hostname` 取。這是刻意的非對稱設計（見 `sa_get_tunnel_token` 內註解）。

啟動 tunnel 的方式與 SH 完全相同：

```bash
cloudflared tunnel run --token <tunnel_token>
```

SA 是 **remotely-managed tunnel**：ingress（N 條 route）由 paas-operator 在
server-side 宣告式管理、從 Cloudflare edge 下發；connector 端只需 token，**不需
本機 config 檔**。

---

### 2.5 `GET /api/security-access/accesses/{id}/status`

刷新並回傳即時連線狀態。controller 先呼叫 `sa.action_refresh_status()`（向
operator 拉最新 tunnel/connector 狀態），再回完整 `to_dict()`。

- Scope：`security-access:read`
- Handler：`HAIntegrationController.sa_get_status`
- `action_refresh_status()` 拋例外時：**仍回 200** + 當前 `to_dict()`
  （model 本身會在失敗時把 `tunnel_status` 更新為 `error`），controller 只記 log
  不轉成 5xx。

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://paas.example.com/api/security-access/accesses/5/status
```

Response：同 §2.3（完整 `to_dict()`，差別是 `tunnel_status` / `tunnel_uptime`
等已刷新）。

---

## 3. 錯誤碼

| 狀況                                        | HTTP | body                                                                                          |
| ------------------------------------------- | ---- | --------------------------------------------------------------------------------------------- |
| Bearer 缺失 / 失效                          | 401  | `{"error":"Unauthorized","detail":"..."}`                                                     |
| scope 不足                                  | 403  | `{"error":"Forbidden","detail":"..."}`                                                        |
| workspace 無 access（列表端點 §2.2）        | 403  | `{"error":"Forbidden","detail":"No access to this workspace"}`                                |
| workspace 無 access（detail/token/status）  | 403  | `{"error":"Forbidden","detail":"No access to the workspace containing this security access"}` |
| SA 不存在 / 跨 workspace                    | 404  | `{"error":"Not Found","detail":"Security access not found"}`                                  |

> `detail` 字串依端點而異：列表端點（§2.2 `sa_list_accesses`）直接驗
> workspace，回 `No access to this workspace`；detail / tunnel-token / status
> （§2.3–§2.5）走 `_get_sa_with_access`，回 `No access to the workspace
> containing this security access`。component 端若要斷言 `detail`，請依端點
> 取對應字串（或只斷言 HTTP 403）。

> 安全設計：`_get_sa_with_access` 先 `sudo().browse().exists()` 確認存在再驗
> workspace access；不存在回 404、存在但跨 workspace 回 403。

## 4. Component 擴展建議（未來實作參考）

以下是 `woow_paas_smart_home` component 若要支援 Security Access 來源時的參考，
**目前 component 未實作**，PaaS 端 contract 已就緒：

1. **config_flow 增加 service-type 選擇步驟**（Smart Home / Security Access），
   依選擇切換要打的 endpoint family（`/api/smarthome/*` vs
   `/api/security-access/*`）。

2. **config entry 的 `unique_id` 須含 service type（G-H4）**：

   ```python
   unique_id = f"{workspace_id}_{service_type}_{service_id}"
   ```

   否則同一 workspace 同時配 Smart Home + Security Access 兩個來源時 unique_id
   會相撞（兩者 ID 各自獨立、可能同值）。`service_type` 取 `smarthome` /
   `security_access` 之一。

3. **`cloudflared_manager` 無需改動**：SA 的 §2.4 回的同樣是 cloudflared tunnel
   token（`connector_type` 為 `cloudflared`），既有連線邏輯可直接複用。

4. **Sensor 可顯示的欄位（FR-9）**：`state`、`tunnel_status`、`plan_name`、
   `next_payment`。列表端點（§2.2）已直接帶 `plan_name` / `next_payment`，
   單筆 sensor 也可走 §2.5 status 刷新後從 `subscription` 取。

5. **per-route entity 以 route `id` 為 key（§2.3.1）**：SA 一筆可含 N 條 route，
   若每條 route 各對一個 entity（如各自的 URL sensor），unique_id 綁 route `id`
   是對的選擇 —— 它在 route 存活期間穩定（in-place 編輯不換 `id`）。唯一例外是改
   hostname（= 砍重建 = 新 `id`，舊 entity 應轉 unavailable）。完整契約見 §2.3.1。

## 5. 與 Smart Home 的差異速查

| 面向                    | Smart Home                        | Security Access                                          |
| ----------------------- | --------------------------------- | ------------------------------------------------------- |
| 資源 model              | `smart_home`                      | `security_access`                                        |
| 列表端點 path 尾段      | `/homes`                          | `/accesses`                                              |
| 單筆 path 尾段          | `/homes/{id}`                     | `/accesses/{id}`                                         |
| read scope              | `smarthome:read`                  | `security-access:read`                                   |
| tunnel scope            | `smarthome:tunnel`                | `security-access:tunnel`                                 |
| tunnel-token 回傳       | `{tunnel_token, tunnel_id, subdomain}`（含 FQDN） | `{tunnel_token, tunnel_id}`（**無 hostname**）  |
| 對外主機名來源          | tunnel-token 的 `subdomain` 或 detail `subdomain` | detail `routes[].hostname`（無單一 FQDN）       |
| detail 內結構           | `ha_port` / `subdomain`           | `routes[]`（N 條 application route + 各自 Access 設定）  |
| state enum 多出值       | —                                 | `active_no_route`（tunnel 已 provision 但 0 route）      |
| 列表端點 plan 欄位      | 不帶（走 detail 的 subscription） | 直接帶 `plan_name` / `next_payment`（FR-9）             |
