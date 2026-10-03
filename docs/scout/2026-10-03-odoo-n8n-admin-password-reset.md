---
title: Odoo／n8n 管理員密碼重設——方案偵察與介面草稿（#1370）
category: scout
status: draft
created: 2026-10-03
updated: 2026-10-03
related: [docs/reference/features/cloud-services.md]
---

# Odoo／n8n 管理員密碼重設——方案偵察與介面草稿（#1370）

> 本文是**決策前偵察**：回答「平台要替 Odoo 與 n8n 重設管理員密碼，該怎麼做、風險在哪」。
> 拍板後 Odoo 部分 promote 到 [#1186](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1186)
> 的實作 PR，n8n 部分另開票；本文屆時標 `superseded`。
> 文中的程式碼區塊全是**草稿**，不是 repo 內會被執行或被 CI 收集的檔案。

## 1. 問題

[#1370](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1370)（正式站 2026-10-03 實測）：

- 平台的「重設管理員密碼」只支援 `template.has_admin_gui` 的範本。對 odoo（service 110）與
  n8n（service 109）呼叫 `reset-admin-password`，回「只有內建 admin GUI 的範本才支援重設管理員密碼」
  （`src/controllers/paas.py` `_service_admin_credentials` 的第一道能力 gate）。
- 兩者都**沒有設定寄信**：Odoo `ir.mail_server` 為空；n8n `/rest/settings` 的 `smtpSetup: false`。
- 結果：租戶忘記 Odoo／n8n 管理員密碼時，平台不能重設、服務本身的「忘記密碼」信也寄不出，
  只能刪掉重開（資料會失去）。

Odoo 這一半已有完整規格 [#1186](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1186)
（operator 不碰 helm 的 exec 端點、平台獨立能力欄位、私有 model 方法、前端按鈕／modal 分流），
只是一直沒排進實作。n8n 沒有任何既有規格。

## 2. 範圍

| 在範圍 | 不在範圍（另開票） |
|---|---|
| Odoo：重設 `base.user_admin` 的密碼（login 不變），清掉 2FA | 範本預設接上平台寄信服務／開通表單填 SMTP（#1370 建議 2） |
| n8n：重設 owner 帳號的密碼（email 不變），關掉 MFA | 服務頁提示「尚未設定寄信」 |
| 平台：沿用 `_service_admin_credentials` 的授權與一次性顯示流程 | Odoo 一般使用者的密碼（租戶自己用 Odoo 管理員改） |
| 前端：Reset 按鈕出現在 odoo／n8n 服務頁 | 結構化稽核、頻率限制（平台級缺口，與 #1186 相同處理） |
| | omnigent（`app_managed`，#1370 留言）：需要它自己的 CLI／DB 方案，另評估 |

## 3. 現況：為什麼不能直接沿用既有 Reset

既有 Reset（#357／#449）的兩個載體都建立在「密碼由 chart 下發」上：

- `config_json`：operator read-modify-write PVC 上的 `/data/config.json`（mcp-server 家族）。
- `helm_secret`：helm upgrade 換 Secret ＋ rollout restart（hermes-agent／open-design／code-server）。

Odoo 與 n8n 都**不是**這種形態：

- **Odoo（woow-odoo chart）**：初始密碼走 sensitive `conf.adminPassword`，由 bootstrap initContainer
  的 `set-admin.py` 寫進 DB，並在同一交易寫完成標記 `woow_paas_platform.bootstrap_admin_password_set=1`
  （woow-paas-charts `charts/woow-odoo/files/set-admin.py`）。之後 bootstrap 重跑**不覆寫**密碼——
  所以「helm upgrade 換一個新的 adminPassword」不會生效，正是 `app_managed` 那種「假裝成功」的形狀。
  bootstrap.sh ⑤ 的註解已寫明「重設走 operator exec，#1186」。tenant schema 對 `conf.adminPassword`
  在升級時也有限制（`additionalProperties:false`、`maxLength`），不能借 `has_admin_gui` 的
  `config.sensitive.admin_password` 路徑（#1186 已指出）。
- **n8n（8gears chart 2.0.1／appVersion 1.122.4）**：owner 由平台 post-deploy init 呼叫
  `POST /rest/owner/setup` 建立（`extra/paas-operator/src/services/n8n_init.py`），密碼的權威在
  n8n 自己的 DB（預設 sqlite，在 PVC 上；也可選 postgres）。chart 沒有任何「管理員密碼」值可以下發。

因此兩者都需要一條**不碰 helm、直接改 app DB** 的 exec 路徑。

## 4. Odoo 方案（＝#1186，補上偵察細節）

### 4.1 operator：exec 一支 ORM 腳本

形狀比照 `init/odoo`（`src/services/odoo_init.py`）：

1. pod 定位一律用 `odoo_chart_contract.py` 的 `odoo_pod_selectors()`／`validate_fallback_pod()`、
   容器 `ODOO_CONTAINER_NAME`、直譯器 `ODOO_PYTHON`（#1129：不得再手刻容器名／selector／python 路徑）。
2. 腳本原始碼放在 `src/services/odoo_scripts/reset_admin_password.py`（與 `update_module_list.py`
   同一套限制：只 import stdlib＋odoo、不用 `__file__`、stdout 最後一行是 JSON）。
3. **密碼走 stdin**（`exec_in_pod(..., input_data=...)`，kubectl `-i`），不進 argv、不進 log、
   不進錯誤訊息。`odoo_module.py` 的 `-c` argv 寫法不要照抄（#1186）。
4. 腳本內容（同一個 transaction）：
   - `parse_config(["-c", "/etc/odoo/odoo.conf", "--no-http"])` 取 `db_name`（woow-odoo chart 的
     conf 掛在 `/etc/odoo/odoo.conf`，與 `update_module_list.py` 同一來源）。
   - `env.ref("base.user_admin", raise_if_not_found=False)`——用 xmlid，不寫死 id=2。
   - 找不到 ⇒ `{"success": false, "code": "admin_missing"}`；`active=False` ⇒ `code: "admin_inactive"`。
     兩者都**不改任何東西**（fail-loud，平台轉成可行動訊息）。
   - `user.write({"password": new_password})`（compute/inverse，經 `_set_password` 以 passlib hash）。
   - 2FA：`"totp_secret" in user._fields` 才處理（`auth_totp` 未裝時欄位不存在）：
     `user.sudo().write({"totp_secret": False})`＋`user._revoke_all_devices()`。
     ⛔ 不可呼叫 `action_totp_disable()`／`revoke_all_devices()`：兩者掛 `@check_identity`，
     沒有 HTTP request 時直接 `UserError("This method can only be accessed over HTTP")`
     （Odoo 18 `odoo/addons/base/models/res_users.py`）。
   - `env.cr.commit()`；最後一行印 `{"success": true, "login": "<login>", "totp_cleared": <bool>}`。
   - 改密碼會改變 session token ⇒ 既有登入 session 自然失效（預期行為，寫進租戶文案）。
5. 不需要 rollout restart：Odoo 每次登入都讀 DB，改完立刻生效。

### 4.2 逾時與錯誤分類

- 階梯（秒）：operator exec 15 ＜ operator 端點硬上限 20 ＜ 平台 client 35 ＜ 前端 45。
  實際數字要在 dev sandbox 用最終 chart 實測（冷啟動的 `odoo` import 約數秒）後定案，並比照
  `check_init_odoo_budget_ladder`／`check_reset_budget_ladder` 加一條 repo-lint 階梯檢查。
- 結果分類沿用 `InitErrorKind` 的語意：
  - `not_executed`：找不到 pod／fallback pod 驗不過——DB 沒被碰，可安全重按。
  - `failed`：腳本自己回報失敗（`admin_missing`／`admin_inactive`／write 例外）——交易已 rollback，密碼未變。
  - `unknown`：exec 逾時、輸出不可解析——不知道 commit 了沒有。平台**不得**宣稱「密碼未變」，
    文案比照 #461 C11′：「狀態未知，請稍後再按一次 Reset」（下一次會鑄新密碼覆蓋，結果一致）。

### 4.3 平台端

- **能力欄位**：`cloud_app_template.app_admin_reset_kind = fields.Selection([('none','None'),
  ('odoo_exec','Odoo (exec)'),('n8n_exec','n8n (exec)')], default='none')`（seed 明寫，`odoo`／`n8n`
  範本各自填）。**不重用** `has_admin_gui`（會觸發 `config.sensitive.admin_password` 下發，tenant schema 會擋）、
  **不擴充** `admin_password_carrier`（那條契約前提是「一定 helm upgrade」）。
  #1186 原提名 `has_odoo_admin_reset`（Boolean）；若 n8n 同票一起做，Selection 一欄比兩個 Boolean 好，
  待決（§8 Q1）。
- **`admin_reset_supported` 的分流**：它是前端按鈕、controller gate、model 防線三處共讀的唯一事實源
  （#357 C01 紀律）。建議改成
  `(has_admin_gui and carrier != 'app_managed') or app_admin_reset_kind != 'none'`，
  讓前端按鈕自動出現；controller 的第一道 gate（`if not has_admin_gui`）改為依 kind 分派。
  `admin_credentials_settable` 維持 False（只做 Reset、不開放租戶自設，自設需要另一套密碼強度規則）。
- **model**：新增 private `_reset_app_admin_password()`（必須 `_` 開頭，理由同 `_reset_admin_password`
  docstring 的 #357 C05：回傳明文、授權只在 controller）。前置條件比照既有：`FOR UPDATE` 列鎖、
  `state == 'running'`、有 release。成功後新密碼寫回 `cloud_service_secret.admin_password`（Odoo）
  或 `n8n_owner_password`（n8n，見 §5.3）。**不**改 state（不像 helm 路徑要 `upgrading`）。
- **controller**：沿用 `_service_admin_credentials`（`ROLE_ADMIN`、conceal gate、例外分類、
  OperationalError 外拋、一次性回應組裝）——只把「執行」那一段依 kind 分派到新 model 方法。
  `admin_username` 回 Odoo 的 login（＝`conf.adminEmail`）／n8n 的 owner email。
- **前端**：`ServiceDetailPage.adminResetSupported` 已讀 payload 的 `template.admin_reset_supported`，
  能力欄位改對了按鈕就會出現；`OneTimeCredentialsModal` 的 `admin_password` hint 目前寫 MCP 文案，
  要依 payload `template.app_admin_reset_kind` 分流成「用 <login> 與此密碼登入 Odoo／n8n；
  所有已登入的裝置會被登出；2FA／MFA 已關閉，請登入後重新啟用」。`hasGateway` 提示維持只綁 MCP。
- **i18n**：新字串補 `src/i18n/zh_TW.po`。

## 5. n8n 方案

### 5.1 方案比較

| | 作法 | 評估 |
|---|---|---|
| **A** | `n8n user-management:reset`（官方 CLI） | **不採**。它把使用者管理打回初始狀態：owner 變成空殼、實例回到 setup 模式。服務網址是公開的，在租戶完成 setup 前**任何人都能搶先設定 owner**，接手所有 workflow 與 credential。就算平台緊接著自動呼叫 `/rest/owner/setup`，中間仍有競態窗口，且其他成員帳號會被清掉。 |
| **B** | exec 進 n8n 容器，用 n8n 自帶的 `bcryptjs` 算 hash，直接更新 owner 列的 `password` 欄位 | **建議**。n8n 1.x 的密碼是 bcryptjs hash（cost 10）存在 `user` 表；n8n 每次登入讀 DB，不需重啟。缺點是依賴 n8n 內部 schema（表名、`role` 欄位值、tablePrefix），升級 n8n 時要回歸測試。 |
| **B′** | 同 B，MFA 另用官方 CLI `n8n mfa:disable --email=<owner>` | B 的補充：MFA 用官方 CLI 關，不手改 `mfaEnabled`／`mfaSecret` 欄位，少依賴一塊內部 schema。 |
| **C** | 經 n8n REST 改密碼（`PATCH /rest/me/password`） | 需要舊密碼，忘記密碼的情境用不上。 |
| **D** | `POST /rest/forgot-password` 寄重設信 | 需要 SMTP（#1370 另一半），而且信會寄到 owner email；本票不依賴寄信。 |

查無「直接設定某使用者密碼」的官方 n8n CLI（1.x 的 CLI 只有 `user-management:reset`、`mfa:disable`、
`ldap:reset` 等），故採 B＋B′。

### 5.2 B 的執行細節（草稿）

1. pod 定位沿用 `N8nInitService._get_n8n_pod()`，容器 `n8n`。
2. 新密碼走 stdin；`node -e <script>` 讀 stdin、`require` n8n 安裝目錄下的 `bcryptjs`
   （路徑在 image 內解析，不寫死版本目錄；找不到 ⇒ `failed`，不 fallback 到別的 hash 實作）。
3. DB 型態由容器 env 判斷：
   - `DB_TYPE` 缺或 `sqlite`：`${N8N_USER_FOLDER:-/home/node}/.n8n/database.sqlite`，用 n8n 自帶的
     `sqlite3` node binding 開檔，**單一 UPDATE**，`WHERE` 鎖定 owner 列。
   - `postgresdb`：用 n8n 自帶的 `pg` 套件，連線參數讀 `DB_POSTGRESDB_*` env（含 `_FILE` 變體）。
   - 表名要套 `DB_TABLE_PREFIX`。
4. owner 的判定：n8n 1.x 是 `role = 'global:owner'`。UPDATE 影響列數必須恰為 1，否則 rollback
   並回 `failed`（找不到 owner／多個 owner 都不寫）。
5. 之後跑 `n8n mfa:disable --email=<owner email>`（MFA 未開時是 no-op）。
6. 不需重啟：n8n 的 auth cookie（JWT）內含由密碼 hash 推導的值，改密碼後既有 session 自然失效。

### 5.3 不受影響／要同步的東西

- **n8n API key 與 MCP sidecar 不受影響**：API key 是獨立的一筆記錄，與 owner 密碼無關；
  sidecar 讀 Secret 裡的 `N8N_API_KEY`，Reset 不動它、也不 rollout restart。
- **平台存的 `n8n_owner_password` 必須同步更新**：post-deploy init 在 redeploy／addon 變更時會用它
  `POST /rest/login` 取 cookie 再建新 API key（`n8n_init.py` Step 3）。Reset 後若不更新，下一次 init
  會在 `login` 步驟失敗。
  - 附帶發現（既有潛在缺陷，與本票無關但同源）：租戶在 n8n UI 裡**自己**改了 owner 密碼後，
    平台存的 `n8n_owner_password` 就過期，下一次 init 一樣會 login 失敗。平台 Reset 會把兩邊重新對齊，
    可當成這個情境的復原手段，寫進文案／runbook。

## 6. 介面草稿

### 6.1 operator（pydantic，`src/models/schemas.py`）

```python
AppAdminResetCode = Literal[
    "admin_missing",      # Odoo：找不到 base.user_admin／n8n：找不到 owner
    "admin_inactive",     # Odoo：base.user_admin.active=False
    "owner_ambiguous",    # n8n：UPDATE 影響列數 != 1
    "hash_unavailable",   # n8n：image 內找不到 bcryptjs
]


class AppAdminPasswordResetRequest(BaseModel):
    """新密碼只在 request body；operator 以 stdin 交給容器，不進 argv／log。"""

    new_password: SecretStr = Field(..., min_length=12, max_length=128)
    # n8n 用：mfa:disable 要 email；平台傳 n8n_owner_email。Odoo 忽略。
    owner_email: Optional[str] = None


class AppAdminPasswordResetResponse(BaseModel):
    success: bool
    login: Optional[str] = None          # Odoo login／n8n owner email
    second_factor_cleared: bool = False  # Odoo TOTP／n8n MFA
    message: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[AppAdminResetCode] = None
    error_kind: Optional[InitErrorKind] = None  # not_executed / failed / unknown
```

### 6.2 operator 端點（`src/api/` 新檔或併入 `init.py`）

```python
@router.post(
    "/{namespace}/{release_name}/odoo/admin-password",
    response_model=AppAdminPasswordResetResponse,
)
def reset_odoo_admin_password(namespace: str, release_name: str,
                              request: AppAdminPasswordResetRequest): ...


@router.post(
    "/{namespace}/{release_name}/n8n/owner-password",
    response_model=AppAdminPasswordResetResponse,
)
def reset_n8n_owner_password(namespace: str, release_name: str,
                             request: AppAdminPasswordResetRequest): ...
```

業務失敗（`failed`／`not_executed`）回 200＋`success=false`（與 `init/*` 一致，平台照 `error_kind` 分類）；
非預期例外回 500＋`detail.error_kind="unknown"`（同 `_unexpected_init_failure`）。

### 6.3 平台 client（`src/services/paas_operator.py`）

```python
APP_ADMIN_RESET_TIMEOUT = 35  # 階梯見 §4.2，由 repo-lint 驗算

def reset_odoo_admin_password(self, namespace: str, release_name: str,
                              new_password: str) -> dict: ...

def reset_n8n_owner_password(self, namespace: str, release_name: str,
                             new_password: str, owner_email: str) -> dict: ...
```

### 6.4 model／controller

```python
# cloud_service.py — 必須 private（#357 C05）
def _reset_app_admin_password(self):
    """FOR UPDATE → state=='running' → 鑄密碼 → 依 template.app_admin_reset_kind
    呼叫 operator → 成功才寫回 secret（Odoo: cloud_service_secret.admin_password；
    n8n: n8n_owner_password）→ 回明文。unknown ⇒ UserError（狀態未知，請稍後重按）。"""

# controllers/paas.py — _service_admin_credentials 內的分派
kind = service.template_id.app_admin_reset_kind
if kind != 'none':
    applied_password = service._reset_app_admin_password()
elif not service.template_id.has_admin_gui:
    return {'success': False, 'error': _('Reset admin password is only supported on ...')}
else:
    ...  # 既有 #357 路徑不變
```

route 不新增：沿用 `POST /api/workspaces/<ws>/services/<id>/reset-admin-password`，前端呼叫點不用改。

## 7. 測試計畫

- **operator unit**：腳本字串在假 `odoo` 環境下執行（比照 `test_odoo_init_cleanup.py` 的作法）：
  找不到 admin／inactive／有無 `totp_secret` 欄位／write 例外的 JSON 回報；exec 逾時 ⇒ `unknown`；
  密碼不出現在 argv 與 log（斷言 `exec_in_pod` 的 `command` 不含密碼、`input_data` 含）。
  n8n：sqlite／postgres 兩種 env 組出的 node 腳本、影響列數 != 1 的處理。
- **operator integration（真 K8s，`paas-operator-dev` skill，helm 3.13.3）**：在 `dev-sandbox-*`
  部署 woow-odoo 與 n8n（sqlite 與 postgres 各一）→ Reset → 用新密碼登入、舊密碼失效；
  Odoo 先開 2FA 再 Reset ⇒ 不需驗證碼；停用 `base.user_admin` 後 Reset ⇒ 可行動錯誤、密碼未變；
  n8n 開 MFA 再 Reset ⇒ 不需 MFA；Reset 後觸發一次 redeploy，n8n init 的 login 成功（驗 §5.3）。
- **平台（沙盒 Odoo 測試，全模組）**：新 model 方法為 private、`get_public_method` 必須 raise
  （`src/tests/test_mcp_admin_password_authz.py` 同款）；能力欄位與 `admin_reset_supported` 的組合表；
  controller 角色 gate（admin 可、user/guest 不可）、conceal gate；`unknown` 不寫回 secret。
  新增測試 class 要更新 `scripts/audit/baselines/test-collection.txt`。
- **正式站驗收**：Portal 會員（uid 207）與一般內部帳號（uid 204）各開一台 odoo 與 n8n →
  Reset → 新密碼登入 → MCP（mcp-odoo／mcp-n8n）串接照常可用。

## 8. 風險與待決問題

| # | 問題 | 建議 |
|---|---|---|
| Q1 | 能力欄位：#1186 的 `has_odoo_admin_reset`（Boolean）還是本文的 `app_admin_reset_kind`（Selection） | 若 n8n 會跟上，用 Selection；只做 Odoo 就照 #1186 |
| Q2 | n8n 方案 B 依賴 n8n 內部 schema（`user` 表、`role='global:owner'`、bcryptjs 位置） | 接受，但 n8n 升版（chart 2.0.1／app 1.122.4 以外）必須跑 integration test；腳本對影響列數做嚴格檢查，寧可失敗不可改錯人 |
| Q3 | n8n 以 postgres 部署時，operator 要從容器 env 讀 DB 密碼 | 只在容器內讀、不回傳；或改用 `n8n` 自己的 TypeORM 設定解析（更穩但更依賴內部 API） |
| Q4 | Odoo Bitnami 存量（舊 chart）服務能不能用 | `odoo_pod_selectors()` 的 fallback 已涵蓋；conf 路徑不同（`/opt/bitnami/odoo/conf/odoo.conf`）——腳本要依序嘗試，或只開放 woow-odoo chart（`template._is_woow_odoo_chart()`） |
| Q5 | 重設後是否也要讓平台存的 Odoo 初始密碼（`cloud_service_secret`）可被「查看」 | 不要。維持一次性顯示，與 #357 一致 |
| Q6 | 頻率限制 | 與 #1186 相同，不在本票；Reset 已有 `FOR UPDATE` 序列化 |

## 9. 建議的 PR 拆法

1. **PR-1（Odoo，以 #1186 為主票）**：operator 腳本＋端點＋unit；平台能力欄位＋private model 方法＋
   controller 分派＋前端 modal 文案＋i18n；沙盒全模組測試；integration test 結果寫進 PR。`Closes #1186`、`Refs #1370`。
2. **PR-2（n8n，新開子票）**：operator n8n 腳本（sqlite／postgres）＋`mfa:disable`＋端點＋unit；
   平台 kind 擴充 `n8n_exec`、寫回 `n8n_owner_password`；integration test（兩種 DB）。
   完成後 `Closes #1370` 的「平台重設」那一半。
3. **另開票**：#1370 的寄信／SMTP 半邊；omnigent（`app_managed`）忘記密碼的復原途徑。

兩支 PR 都只動 operator 與 `src/`，不需要 migration（新欄位由 `-u` 建欄，seed 是 `noupdate=0`）；
operator 需重建 image 並與 Odoo 發版協調先後（operator 先上，平台後上；反過來時平台會打到不存在的端點，
應回 `not_executed`）。
