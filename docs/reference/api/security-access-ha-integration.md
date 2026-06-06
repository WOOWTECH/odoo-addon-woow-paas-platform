---
title: Security Access — HA Component 串接 API
category: reference
status: implemented (Odoo side) / future (component side)
audience: woow_paas_smart_home HomeAssistant custom component team
source-of-truth: src/controllers/ha_api.py（HAIntegrationController 的 /api/security-access/* 路由）
related:
  - src/models/security_access.py（to_dict / _get_subscription_info / SecurityAccessRoute.to_dict）
  - src/models/subscription_lifecycle_mixin.py（_get_subscription_info）
  - src/data/oauth_clients.xml（public client `woow-ha-smart-home` 的 scopes）
  - src/data/oauth_scope_updates.xml（既有 DB 的 SA scope 補丁 _ensure_ha_smart_home_sa_scopes）
  - docs/reference/api/woowtech-paas-api-specification.md（平台 API 總規格）
---

# Security Access — HA Component 串接 API

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
> make_security_access_subdomain`），例如 `paas-sa-7f3a2b-c1d4e9`。`tunnel_name`
> 等於 `canonical_name`（`action_provision` 以 `name=self.canonical_name`
> 建 tunnel）。範例 hash 為佔位，實際值由 workspace slug + reference_id + 暱稱
> 雜湊而得，請勿假設可讀字串。

## 1. 認證

OAuth 2.0 Bearer token。public client `woow-ha-smart-home`（PKCE-only，
`client_type=public`，redirect_uri 為 HA 標準
`https://my.home-assistant.io/redirect/oauth`）。所有 SA 端點
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
      "canonical_name": "paas-sa-7f3a2b-c1d4e9",
      "state": "active",
      "primary_hostname": "paas-sa-7f3a2b-c1d4e9.paas.example.com",
      "tunnel_status": "connected",
      "plan_name": "Security Access Pro",
      "next_payment": "2026-07-06"
    }
  ]
}
```

欄位說明：

| 欄位               | 來源                                          | 備註                                                              |
| ------------------ | --------------------------------------------- | ----------------------------------------------------------------- |
| `state`            | `security_access.state`                       | `pending` / `provisioning` / `active` / `error` / `suspended` / `deleting` |
| `primary_hostname` | `security_access.primary_hostname`（compute） | `{canonical_name}.{paas_domain}`；缺值為 `""`                     |
| `tunnel_status`    | `security_access.tunnel_status`               | `connected` / `disconnected` / `error`；預設 `disconnected`       |
| `plan_name`        | `_get_subscription_info()['plan_name']`       | 無訂閱時整段 `plan_name` / `next_payment` 為 `null`               |
| `next_payment`     | `_get_subscription_info()['next_payment']`    | ISO date（`sh_date_of_next_payment`），無則 `null`                |

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

Response（`200`，`security_access.to_dict()` 完整輸出）：

```json
{
  "access": {
    "id": 5,
    "name": "Front Door NVR",
    "canonical_name": "paas-sa-7f3a2b-c1d4e9",
    "workspace_id": 12,
    "state": "active",
    "tunnel_id": "f3a1...",
    "tunnel_name": "paas-sa-7f3a2b-c1d4e9",
    "tunnel_status": "connected",
    "tunnel_uptime": "3h12m",
    "connector_id": "c0ffee...",
    "connector_type": "cloudflared",
    "primary_hostname": "paas-sa-7f3a2b-c1d4e9.paas.example.com",
    "error_message": "",
    "deployed_at": "2026-06-06T08:30:00",
    "created_at": "2026-06-06T08:29:41",
    "routes": [
      {
        "id": 9,
        "subdomain_prefix": "paas-sa-7f3a2b-c1d4e9",
        "hostname": "paas-sa-7f3a2b-c1d4e9.paas.example.com",
        "service_url": "http://localhost:80",
        "http_host_header": true,
        "is_primary": true,
        "is_protected": false
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

- `tunnel_token` **不在** `to_dict()`（欄位 `groups='base.group_system'`），
  detail 端點不洩漏。要 tunnel 憑證走 §2.4。
- `routes[]` 是每筆 `SecurityAccessRoute.to_dict()`（6 欄 + `hostname`
  compute）。`is_primary=true` 那筆為 provision 時自動建立、不可刪，其
  `subdomain_prefix` 等於 `canonical_name`。
- `subscription` 為 `_get_subscription_info()`；**無訂閱時整個 `subscription`
  為 `null`**（非 `{}`）。

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
  "tunnel_id": "f3a1...",
  "primary_hostname": "paas-sa-7f3a2b-c1d4e9.paas.example.com"
}
```

三欄缺值皆回 `""`。對照 SH 版回的是 `subdomain`（FQDN），SA 版回 `primary_hostname`。

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

| 狀況                      | HTTP | body                                                            |
| ------------------------- | ---- | -------------------------------------------------------------- |
| Bearer 缺失 / 失效        | 401  | `{"error":"Unauthorized","detail":"..."}`                      |
| scope 不足                | 403  | `{"error":"Forbidden","detail":"..."}`                         |
| workspace 無 access       | 403  | `{"error":"Forbidden","detail":"No access to this workspace"}` |
| SA 不存在 / 跨 workspace  | 404  | `{"error":"Not Found","detail":"Security access not found"}`   |

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

## 5. 與 Smart Home 的差異速查

| 面向                   | Smart Home                       | Security Access                              |
| ---------------------- | -------------------------------- | -------------------------------------------- |
| 資源 model             | `smart_home`                     | `security_access`                            |
| 列表端點 path 尾段     | `/homes`                         | `/accesses`                                  |
| 單筆 path 尾段         | `/homes/{id}`                    | `/accesses/{id}`                             |
| read scope             | `smarthome:read`                 | `security-access:read`                       |
| tunnel scope           | `smarthome:tunnel`               | `security-access:tunnel`                     |
| tunnel-token 回的主機名 | `subdomain`（FQDN）              | `primary_hostname`                           |
| detail 內多出的結構    | `ha_port` / `subdomain`          | `routes[]`（多組 application route）         |
| 列表端點 plan 欄位     | 不帶（走 detail 的 subscription） | 直接帶 `plan_name` / `next_payment`（FR-9）  |
