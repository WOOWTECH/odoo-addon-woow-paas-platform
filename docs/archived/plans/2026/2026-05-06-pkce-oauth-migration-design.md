---
status: archived
archived-from: docs/plans/2026-Q2/2026-05-06-pkce-oauth-migration-design.md
archived: 2026-07-19
archived-reason: "PKCE 已上線；現況=src/controllers/oauth2.py 與 guides/development/oauth2-architecture.md"
---

# PKCE-only OAuth2 migration — paas-platform ↔ HA Smart Home

> **狀態**：已實作完成（PR #21、PR #22 均 merged 進 develop）
> **建立**：2026-05-06
> **參與者**：paas-platform agent（Odoo 後端）、ha-smart-home agent（HA custom component `woow_paas_smart_home`）、paas-charts agent（chart NP 諮詢）
> **共享 memory**：Maestri note `sm-api-doc`

---

## 1. 動機

HA 端整合舊流程：

1. 使用者進 paas-platform 後台
2. 手動建立 OAuth client（confidential）
3. 抄 `client_id` / `client_secret` 回貼到 HA

新流程目標：

1. 使用者在 HA 點「Add Integration」
2. 自動跳轉 paas-platform 登入 → 授權 → 完成
3. **完全不用碰 paas-platform 後台**

採用 RFC 7636 PKCE-only public client：HA 端 hardcode 公開的 `client_id`，靠 PKCE（code_challenge + code_verifier，S256）取代 client_secret 的安全保證。

## 2. 既有 OAuth2 server（baseline）

paas-platform 是**自寫** OAuth2 provider（不是 Odoo `auth_oauth` module）。

| 項目 | 值 |
|---|---|
| Authorize URL | `GET  {BASE}/oauth2/authorize` |
| Token URL | `POST {BASE}/oauth2/token` |
| Grant types | `authorization_code`, `refresh_token`, `client_credentials` |
| 認證 | client_id + client_secret（POST body） |
| Bearer 使用 | `Authorization: Bearer <access_token>` |

舊 PKCE 邏輯**已部分實作**（如 oauth2 controller 的 `_verify_pkce` S256 算法、`oauth_code` model 的 `code_challenge` 欄位），但 token endpoint 的 client auth 強制要求 secret，且 PKCE 是「if challenge present then verify」而非「enforced for public clients」。

## 3. Smart Home REST API（不變）

全部 `GET`、JSON、需帶 Bearer token。

| # | Path | 用途 | 回傳 |
|---|---|---|---|
| 1 | `/api/smarthome/workspaces` | 列出登入者可存取的 workspace | `{ workspaces: [{ id, name, slug }] }` |
| 2 | `/api/smarthome/workspaces/{workspace_id}/homes` | 列出 workspace 下的 home | `{ homes: [{ id, name, state, subdomain, tunnel_status }] }` |
| 3 | `/api/smarthome/homes/{home_id}` | 完整 home detail | `{ home: { ... } }` |
| 4 | `/api/smarthome/homes/{home_id}/tunnel-token` | 為 home 發 Cloudflare tunnel token | `{ tunnel_token, tunnel_id, subdomain }` |
| 5 | `/api/smarthome/homes/{home_id}/status` | 輪詢 home 狀態 | `{ home: { ... } }` |

`tunnel_status` enum：`connected | disconnected | error | unknown`

錯誤 body 用 `{ "detail": "..." }` 或 `{ "error": "..." }` 之一。Client 在 401/403 觸發 reauth、404 丟 `NotFoundError`、其他 4xx/5xx 丟 `ApiError`。

HA 對每個 home 每 30 秒輪詢一次 `/status`（`const.py` 的 `UPDATE_INTERVAL`）。

## 4. Authorize / Token endpoint 變化

| 端點 | 改造後 request 變化 |
|---|---|
| `GET /oauth2/authorize` | **多帶** `code_challenge` + `code_challenge_method=S256`；本來就沒帶 `client_secret` |
| `POST /oauth2/token`（authorization_code） | **不帶** `client_secret`，**改帶** `code_verifier` |
| `POST /oauth2/token`（refresh_token） | **不帶** `client_secret`（其餘照舊） |

Token response 格式不變。

## 5. paas-platform 端實作 spec

### 5.1 Model：`OAuthClient` 加欄位 + 約束

新欄位（`src/models/oauth_client.py`）：

- `client_type`：Selection（`confidential` / `public`），預設 `confidential` 維持向下相容
- `require_pkce`：Boolean，預設 `False`；public client 自動視為 True

`@api.constrains` 強制：

- public client 不可有 `client_secret_hash`
- public client 必須 `require_pkce=True`
- public client 不可使用 `client_credentials` grant（避免繞過 PKCE）

`set_secret` 與 `action_regenerate_secret` 對 public client 直接 `raise ValidationError`。

### 5.2 Controller：`_authenticate_client` 分流

`src/controllers/oauth2.py:`

```python
if client.client_type == 'public':
    if client_secret:
        return None   # public + secret = misconfiguration, reject
    return client     # PKCE 在 code-exchange 階段把關
# confidential: 維持原邏輯，必須驗 secret
```

對 `authorize_get`、`authorize_post`、`_handle_authorization_code` 三處都加 PKCE enforcement：public / require_pkce client 必須提供 `code_challenge` 且 method 為 `S256`，否則拒絕（防止 downgrade attack）。

每條失敗路徑都加 `_logger.warning` / `_logger.info`，避免 silent failure。

### 5.3 Seed：`woow-ha-smart-home` public client

新檔 `src/data/oauth_clients.xml`，`noupdate=1` 讓 admin 編輯（如新增 redirect URI）能 survive module 升級：

```xml
<record id="oauth_client_ha_smart_home" model="woow_paas_platform.oauth_client">
  <field name="name">Home Assistant Smart Home Integration</field>
  <field name="client_id">woow-ha-smart-home</field>
  <field name="client_type">public</field>
  <field name="require_pkce" eval="True"/>
  <field name="redirect_uris">https://my.home-assistant.io/redirect/oauth</field>
  <field name="grant_types">authorization_code,refresh_token</field>
  <field name="scopes">smarthome:read smarthome:tunnel workspace:read</field>
</record>
```

Redirect URI 固定 `https://my.home-assistant.io/redirect/oauth`：HA Core 預設行為，`my` 整合是 `default_config` 一員，99% HA 使用者走這個固定 URI（Spotify / Google / Withings 上游 HA 整合都這樣）。

### 5.4 View

- Tree / form 加 `client_type` column / 欄位
- Form 加 `require_pkce`
- Header 上的「Regenerate Secret」按鈕對 public client `invisible="client_type == 'public'"`

### 5.5 Tests

`src/tests/test_oauth2.py` 新增兩個 class（採 TransactionCase + mock `request` 的 pattern，跟既有 `test_ha_api.py` 一致，避開 HttpCase 的跨 cursor 問題）：

- `TestPublicClientConstraints`：5 case 驗 `@api.constrains` 行為
- `TestPkceOnlyTokenFlow`：8 case 驗 token endpoint（PKCE 成功、帶 secret 被拒、缺/錯 verifier 被拒、plain method 被拒、refresh 不帶 secret 成功 + rotation、refresh 帶 secret 被拒、confidential 不變的 regression）
- `TestAuthorizeEndpointPkce`：3 case 驗 authorize GET/POST 的 PKCE 強制（patch `request.render` / `request.redirect` 抓 template / URL params）
- `TestPkceOnlyTokenFlow` 再加 3 個 edge case（redirect URI mismatch、unknown client_id、inactive client）

### 5.6 改動檔案 summary

```
src/models/oauth_client.py        # 加 client_type / require_pkce + @api.constrains
src/controllers/oauth2.py         # _authenticate_client 分流 + public 強制 PKCE+S256 + failure logging
src/views/oauth_client_views.xml  # UI: client_type 欄位、隱藏 secret 按鈕
src/data/oauth_clients.xml        # 新檔：seed woow-ha-smart-home public client
src/__manifest__.py               # 加 data 檔
src/tests/test_oauth2.py          # 加 PKCE-only HTTP 整合測試
```

確認向下相容：

- 既有 confidential `authorization_code` flow 不變
- 既有 `client_credentials` flow 不變
- OAuth Clients admin UI 保留，新增 `client_type` 欄位

## 6. HA 端實作 spec

| 檔案 | 動作 |
|---|---|
| `const.py` | 新增 `OAUTH2_CLIENT_ID = "woow-ha-smart-home"` |
| `application_credentials.py` | **整檔刪除** |
| `manifest.json` | `dependencies` 移除 `"application_credentials"` |
| `__init__.py` `async_setup` | 改用 `config_entry_oauth2_flow.async_register_implementation` 註冊 `LocalOAuth2ImplementationWithPkce` |
| `config_flow.py` | **不變**（繼續用 `AbstractOAuth2FlowHandler`） |

註冊範例（subclass 為了塞 scope，**super 必呼叫**保留 PKCE 行為）：

```python
class WoowPaasOAuth2(config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce):
    @property
    def extra_authorize_data(self) -> dict:
        data = {"scope": "smarthome:read smarthome:tunnel workspace:read"}
        data.update(super().extra_authorize_data)  # PKCE challenge 從這裡來
        return data
```

`application_credentials` framework 不適用：`CREATE_FIELDS` 規定 `client_secret` 為 `vol.Required`，無法跳過。

## 7. HA 端對 server 行為的確認（cross-agent verify）

paas-platform 在實作前 raise 三個確認問題，HA 端附 HA Core 行號逐題回答 ✅：

### 7.1 Token request 帶的欄位名是 `code_verifier` ✅

`homeassistant/helpers/config_entry_oauth2_flow.py:297-313` 證實：

```python
@property
def extra_token_resolve_data(self) -> dict:
    return {"code_verifier": self.code_verifier}
```

合 RFC 7636 標準名。paas-platform 直接讀 `request.params.get('code_verifier')`。

### 7.2 Refresh request 完全不帶 `client_secret`（連 key 都不會出現） ✅

`homeassistant/helpers/config_entry_oauth2_flow.py:219-226`：

```python
async def _token_request(self, data: dict) -> dict:
    data["client_id"] = self.client_id
    if self.client_secret:        # 空字串是 falsy
        data["client_secret"] = self.client_secret
```

`LocalOAuth2ImplementationWithPkce` 的建構子 `client_secret: str = ""` 預設空字串。所以 `client_secret` key 在 POST body **完全不會出現**，paas-platform 端做「if `client_secret` is truthy: reject for public」是安全的。

### 7.3 HA 自動處理 refresh token rotation ✅

`config_entry_oauth2_flow.py:217`：

```python
async def _async_refresh_token(self, token: dict) -> dict:
    new_token = await self._token_request({...})
    return {**token, **new_token}     # new_token 的 refresh_token 蓋掉舊的
```

server 在 token response 回新的 `refresh_token`，HA 自動更新；下次 refresh 用新的。HA 端不需要寫任何特殊邏輯。

### 7.4 Token response 必填欄位

| Key | 用途 |
|---|---|
| `access_token` | Bearer token |
| `refresh_token` | Rotation 時要回新值 |
| `expires_in` | int 秒數，HA 用 `time.time() + expires_in` 算 expiry（必須能被 `int()` 接受） |
| `token_type` | 通常 `"Bearer"`（case-insensitive 接受 `"bearer"`） |
| `scope` | 建議帶；HA 會 log，不強制 |

paas-platform 既有 `_issue_tokens` 已符合。

## 8. 安全 review 重點

對 PR review 的安全檢查 checklist：

1. public client 強制 `code_challenge_method=S256`（拒絕 `plain`）
2. public client 禁用 `client_credentials` grant（避免繞 PKCE）
3. Authorize / token endpoint 三層都 enforce PKCE（防 downgrade）
4. Public client 收到非空 `client_secret` 直接拒絕（防止 client 誤把 secret 帶在 body）
5. Redirect URI exact match（既有實作，符合 RFC 8252 §7.3）
6. Refresh token rotation（既有實作，public client 必要設計）

## 9. 風險與 caveat

| 項目 | 處理 |
|---|---|
| 既有 confidential client 整合 | 保留：`client_type` 預設 `confidential`，向下相容 |
| 跨環境一致 `client_id` | XML data record（`noupdate=1`），dev/stg/prod 都是 `woow-ha-smart-home` |
| HA 沒裝 `my` 整合的 fallback | 不在 paas-platform 註冊額外 redirect URI；文件指引 user 啟用 `my` |
| Real Cloudflare tunnel | 不在 PKCE migration scope；stg seed 用 mock token 驗證 OAuth + API 路徑就足夠，真 tunnel 由 paas-operator provision |

## 10. 結論

PKCE-only public client OAuth2 flow 由 paas-platform 端「擴充」既有 OAuth2 provider 完成，向下相容、無業務阻力。HA 端只用 HA Core 既有的 `LocalOAuth2ImplementationWithPkce` framework，零客製。雙方端到端對齊，可以分階段交付：

1. paas-platform 端 PR → develop → stg
2. HA 端 MR ready，等 prod 部署後 merge
3. 雙方同步上 prod

## 參考

- `src/controllers/oauth2.py`、`src/models/oauth_client.py`、`src/data/oauth_clients.xml`、`src/tests/test_oauth2.py`
- HA Core `homeassistant/helpers/config_entry_oauth2_flow.py:64-76, 194-338, 548-553`
- RFC 7636（PKCE）、RFC 8252（OAuth 2.0 for Native Apps）
- 過程記錄：`docs/plans/journal/2026-05-06-pkce-oauth-migration.md`
