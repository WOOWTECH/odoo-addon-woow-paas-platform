# Design: Security Access 訂閱服務（Cloudflare Tunnel + Access Control + 自有網域 + Email Routing）

> 狀態：P1 + P2(Per-route Access Control) implemented（plans: `docs/superpowers/plans/2026-06-06-security-access-p1-mainline.md` + `docs/archived/superpowers/plans/2026-06-07-security-access-p2-access-control.md` + P2.5 per-route 重構 `docs/superpowers/plans/2026-06-09-security-access-p2.5-per-route-access.md`）；P3 Domains / P4 Email 待開。
>
> **P2.5 note**：P2 原為 Service-wide（整 SA 一個 Cloudflare Access app + 一套共用 policies/login/session）；P2.5 重構成 **Per-route**（每條 Application Route = 一個 Access app，各自 Protected toggle + policies + login methods + session）。Access 三件組從 SA 層下放到 route 子模型；Application tab 移除 Protected 欄，Access tab 改 per-route Protected Applications（無 Service-wide 切換器，廢雙模版）。operator 不變（仍 per-app，Odoo 每 protected route 各打一次 sync）。
> 來源：`docs/meeting/docs/2026-06-06 Security Access 開發需求文件.md`（27 份會議記錄彙整）
> 研究依據：6-agent 平行探勘（Smart Home 後端 / Cloud Service 訂閱 / operator+cloudflare /
> 前端 OWL / HA component 合約 / 訂閱基礎設施），結論已整合進本文。
> UI 依據：detail page mockup（header+Access / Email / Domains 三張；mockup 為 5 tab，
> 拍板後拆為 6 tab：Configuration 留給 record 自身設定、Application 管 routes）+
> dashboard「Explore More Services」三卡 mockup。

## 背景

Security Access（SA）是 WOOW PaaS 三大訂閱服務之一（Cloud Service / Smart Home / SA），
定位為 B 端遠端安全存取：**一組 Cloudflare Tunnel、多組自訂 subdomain、流量統一導向用戶
地端主機 NPM 的 80 port，最後一哩反向代理由用戶自管**。2026-04-30 盤點時為三服務中唯一
未完成者。

探勘核心結論：SA 是 CloudService / SmartHome 的「第三個平行 service model」——
**訂閱/計費層（`subscription_lifecycle_mixin`）幾乎 100% 複用，Cloudflare 層需擴充
operator，前端複用 detail-tab 框架，HA component 串接需平行 API**。

## 需求決議（brainstorming 拍板）

| # | 決議 |
|---|---|
| 範圍 | detail page **6 tab 全做**：Overview / Configuration / Application / Access / Email / Domains（Configuration = security_access record 自身設定；Application = application routes 管理） |
| 凍結策略 | **503 凍結**：到期把所有 route ingress 改指 `http_status:503`，DNS / tunnel / Access / token 全保留；恢復訂閱還原 `service_url`（沿用 SmartHome suspend pattern） |
| Access Control 計費 | **訂閱內含、不另計費**；plan flag 留加值空間，正式定價（$10/月+$7/名額案）拍板後 follow-up |
| 方案限額 | 四維度全建模：`max_routes` / `max_access_policies` / `max_custom_domains` / `max_email_rules` |
| 自有網域模型 | **雙軌制**：CNAME custom hostname（預設、輕量、只通 HTTPS）→ 逐網域可「升級」NS 委派 full_zone（解鎖 Email Routing）。技術硬限制：Cloudflare Email Routing 要求網域為平台帳號內以 Cloudflare 為權威 NS 的 zone |
| Access 粒度 | **服務層共用設定**（policies / login methods / session duration 一套）+ **逐 route 公開/受保護開關**（呼應 FR-4） |
| 架構方案 | **平行模型**：新 model family、不重構生產中的 SmartHome；抽共用 tunnel mixin 記為 follow-up |
| workspace 歸屬 | SA 與 SH / CS 一樣**隸屬某一 workspace**（`workspace_id` required + workspace-scoped ir.rule + workspace-scoped 前端路由） |
| 命名 | 實體名 `paas-sa-{ws_hash}-{res_hash}`；tunnel name / Access 物件名與其同名；使用者另可設 nickname；**所有 Cloudflare 物件的 name/description/comment 必含 canonical name 以反推來源** |
| 凍結時 Email | suspend 時 **email 轉寄規則一併 disable**（503 只擋 HTTP，email 也是訂閱權益），resume 恢復 |
| HA 串接 | 在 PaaS 端提供平行於 `/api/smarthome/*` 的 `/api/security-access/*` + 串接文件；**不修改 `woow_paas_smart_home` component 原始碼** |
| mockup 取捨 | header 的「Rollback」按鈕在 SA 無對應語義（屬 Cloud Service 部署回滾）→ 不做；「Domain」按鈕 = 跳 Domains tab |

## 非目標

- Access Control 獨立加購計費（名額計數、$7/名額）——等定價拍板
- 抽 `cloudflare_tunnel_mixin` 重構 SmartHome——SA 上線穩定後 follow-up
- 修改 HA `woow_paas_smart_home` component 原始碼（僅提供 API + 文件）
- operator per-tenant RBAC（v1 接受單一 CF account token，靠 `paas-sa-` 命名前綴隔離；記入 known risks）
- Cloud Service 走 Cloudflare Tunnel（會議明確排除）

---

## 名詞對照（與 Cloudflare 後台用語）

實作與 review 時對照用，避免混淆（特別是 Application Route 與 Custom Domain 不是同一件事）：

| 本設計術語 | 子模型 | Cloudflare dashboard 位置（zh-tw 介面名） | 本質 |
|---|---|---|---|
| **Application Route**（Application tab） | `security_access.route` | Zero Trust → Networks → Tunnels → 該 tunnel →「**已發布應用程式路由**」（Published Application Routes / Public Hostnames） | hostname → service 的 tunnel ingress 轉送規則；hostname 只能掛在「**已連線至 Cloudflare 的網域（我們帳號內 zone）**」上（平台 base domain subdomain） |
| **503 凍結機制** | （lifecycle hook） | ingress 的 `http_status:*` 回應類型 | suspend = 每條 route 的 service 換成 `http_status:503` |
| **Custom Domain**（Domains tab） | `security_access.custom_domain` | CNAME 軌：SSL/TLS → 「**自訂主機名稱**」（Custom Hostnames, Cloudflare for SaaS）；full_zone 軌：帳號內新增 **zone**（已連線網域） | 把**客戶外部網域**接進我們帳號的「接入機制」，是 Application Route 的**前置條件層**：CNAME 軌不進平台帳號（別名借道 primary route）；full_zone 軌（NS 委派）成為平台帳號內的 zone |
| **Tunnel** | 主模型 `tunnel_id` | Zero Trust → Networks → Tunnels | 每訂閱戶一條（`paas-sa-{hash}` 命名）；與平台 infra tunnel（如 `prod-paas-dev`，服務 K8s cluster）**不同層**，靠命名前綴區分 |
| **Access Policy**（Access tab） | `security_access.access_policy` | Zero Trust → Access → Applications + Policies | 受保護 hostname 的身分驗證閘 |
| **Email Rule**（Email tab） | `security_access.email_rule` | 該 zone → Email →「**電子郵件路由**」（Email Routing） | full_zone 網域的收信轉寄規則 |

v2 擴充方向（本次不做）：full_zone 軌的客戶網域成為帳號內 zone 後，可允許直接在其上
開 Application Route（如 `ha.acme.com → localhost:80`），不繞 primary 別名；屆時
`custom_domain` 加 `route` 關聯即可演進。

---

## §1 資料模型

### 主模型 `woow_paas_platform.security_access`

```
_inherit = ['woow_paas_platform.subscription_lifecycle_mixin']   # 注意：list 形式

name              Char       # nickname，使用者可改（mockup 的 "secure-gateway"）
reference_id      Char       # 建立時生成、唯讀不可變（hash 來源；改動會導致 subdomain 漂移斷路由）
workspace_id      M2O        # required，workspace 歸屬
template_id       M2O        # → security_access.template（方案綁定）
state             Selection  # pending → provisioning → active / suspended / error / deleting
pre_suspend_state Char       # 暫停前狀態回存（沿用 SmartHome）

# Cloudflare
tunnel_id         Char       # CF tunnel UUID
tunnel_token      Char       # groups='base.group_system'（field-mask，同 SmartHome）
canonical_name    Char       # paas-sa-{ws_hash}-{res_hash}（md5 前 8 碼，沿用 naming.py 演算法）
                             # ※ 建立時計算一次後「儲存、唯讀」——不可做成會重算的 computed：
                             #   既有演算法 salt 含 name，nickname 可改，重算會導致 subdomain 漂移斷路由
primary_hostname  Char(計算)  # {canonical_name}.{base_domain}（由已儲存的 canonical_name 串接，安全）

# Access（服務層共用設定）
access_enabled    Boolean
session_duration  Selection   # 30m / 1h / 8h / 24h…（mockup Default session duration）
login_email_otp / login_google / login_github / login_ip_allowlist   Boolean × 4
```

- `naming.py` 新增 `make_security_access_subdomain()`，沿用既有 md5 前 8 碼演算法，
  `paas-sa-` 為既有 `paas-cs-` / `paas-sm-` 的預留槽位延伸。
- `canonical_name` 同時用於：tunnel name、Access Application 名稱、custom
  hostname 與 DNS record 的 comment/description ——反推性需求的落點。
- Access Policy 在 CF 端的名稱 = `{canonical_name}: {使用者自訂 policy 名}`
  （mockup 的 "Internal staff" 等為使用者自訂顯示名；CF 端加 canonical 前綴，
  兩者皆可反推來源）。

### 子模型（對應 4 個 tab + 4 個 quota 維度）

| Model | 關鍵欄位 | Cloudflare 對應 |
|---|---|---|
| `…security_access.route` | `subdomain_prefix`（用戶自定、**全平台唯一** UNIQUE）、`hostname`(計算)、`service_url`（預設值來自後台設定，預設 `http://localhost:80`）、`http_host_header`（預設 True，設定傳送 HTTP Host 標頭）、`is_protected`（逐 route Access 開關）、`is_primary`（開通時自動建、不可刪）、`dns_record_id` | tunnel ingress rule + DNS CNAME record |
| `…security_access.access_policy` | `name`、`action`(allow/block)、`rule_type`(email_domain/email_list/ip_range)、`rule_value`、`session_duration`、`sequence`（評估順序；block 優先，對齊 mockup「Block rules win over allow」） | Access Application（每個受保護 hostname 一個）+ Policy |
| `…security_access.custom_domain` | `domain`、`mode`(cname/full_zone)、`state`(pending/verified/active)、`is_primary`、`verify_token`、`cf_custom_hostname_id`、`cf_zone_id`、`ssl_status` | Custom Hostname（SaaS）或 Zone（NS 委派後） |
| `…security_access.email_rule` | `custom_domain_id`（限 full_zone）、`address`、`destination_id`、`is_catch_all`、`enabled` | Email Routing rule |
| `…security_access.email_destination` | `email`、`verified`、`cf_destination_id` | Email Routing destination address |

- custom domain v1 **指向 primary hostname**（不做逐 route 指向；可後續擴充）。
- email destination 建模在**服務層**（非 workspace 層）。

### 方案 quota

extend `sh.subscription.plan`（`_inherit` 新增欄位，不改既有欄位型態——遵守
`odoo-std-field-no-override` pattern）：

```
sa_max_routes / sa_max_access_policies / sa_max_custom_domains / sa_max_email_rules   Integer
```

- 建立 route / policy / domain / email_rule 時 `@api.constrains` + controller **雙層**檢查。
- `security_access.template` 鏡像 `smart_home_template`：`subscription_plan_ids` M2M +
  `_check_no_cross_service_type_plans` 防跨服務類型共用 plan。

### ACL（B1-class 防線，硬規則）

- **對稱 ir.rule**（SmartHome 當年沒做、SA 必須做）：主模型 domain
  `[('workspace_id.access_ids.user_id','=',user.id)]`；4 個子模型 walk
  `security_access_id.workspace_id`；admin/system 另開 full rule。
- **controller 禁 `.sudo()` list-and-return**：每個 list/get endpoint 配 1 條 HttpCase
  cross-user negative test（`orm-vs-endpoint-acl-test-coverage` pattern）。
- `tunnel_token` field-mask `base.group_system`，只透過帶 workspace 檢查的專用 endpoint 流出。
- 若未來引入 scope/visibility 可翻轉欄位，須加 `write()` guard
  （`scope-flip-write-guard-not-ir-rule` pattern）；v1 模型無此類欄位。

---

## §2 Operator API 擴充（Cloudflare 代理層）

現況：operator（`extra/paas-operator/`）已有 `/api/tunnels/*`、`/api/routes/*`
（tunnel CRUD、DNS CNAME、ingress 更新）——SA 主線（建 tunnel、加 route、503 凍結）
**用現有 endpoints 即可**。所有 Cloudflare 操作一律經 operator，Odoo 不持 CF token。

### 新增 4 組 endpoints

| 組 | Endpoints | 對應 Cloudflare API |
|---|---|---|
| Access | `POST/PUT/DELETE /api/access/apps`（payload：hostname、allowed_idps、session_duration、policies 陣列） | Access Applications + Policies（v1 用 per-app policy，不用 reusable policy） |
| Custom Hostnames | `POST /api/custom-hostnames`、`GET …/{id}`（SSL 狀態 + CNAME/TXT 驗證資訊）、`DELETE` | Cloudflare for SaaS |
| Zones | `POST /api/zones`（回分配 NS 對）、`GET …/{id}`（NS 委派啟用狀態）、`DELETE` | Zone 管理（full_zone 升級軌） |
| Email Routing | `POST /api/zones/{id}/email/enable`、`POST/DELETE /api/zones/{id}/email/rules`、`POST/DELETE /api/email/destinations`、各 `GET` | Email Routing rules / destinations |

另加 `GET /api/routes/availability?hostname=` —— 查 CF 端同名 record；與 Odoo DB
UNIQUE 檢查構成雙層（「設定 route 前先反饋」需求的落點）。

> **契約以 plan + 程式為準（drift note）**：本 spec 此處與 §4 為 design 草擬
> 值，最終實作已飄移，**讀者請勿據本段建 client**。實際 operator endpoint 的
> query 參數名是 `subdomain`（非 `hostname`），見
> `extra/paas-operator/src/api/routes.py:check_route_availability`；BFF 對外是
> `POST /api/security-access/routes/check`、`prefix` 取自 JSON body（非 GET
> query），見 `src/controllers/security_access.py:api_route_check`。

### 設計原則

1. **命名 stamping 在 operator 層強制**：create payload 必帶 `canonical_name`，
   operator 寫入 CF 物件 name / description / DNS comment。
2. **Fail-fast 硬化**：現有 `CloudflareService` enabled 但 credentials 缺失時靜默停用
   —— 本次修正為啟動即失敗（進行中工作的順手硬化）。
3. **凍結/恢復零新端點**：以現有 ingress 更新 API 批次切換 `http_status:503` / 還原。
4. Auth 沿用 API key；單一 CF account token 的多租戶風險 v1 接受（known risk）。

### 測試與部署

- unit tests mock httpx（沿用現有 pytest 結構）；CF 真實串接以 staging 帳號 smoke
  script 手動驗證（文件化）。
- 既有 `-m integration` 套件不得回歸。
- operator 發版走 `build-operator` skill（`operator-v*` tag）；stg 部署走 Fleet CD
  values（coldstart pitfall 教訓路徑）。

---

## §3 訂閱 Lifecycle 與清理鏈

繼承 `subscription_lifecycle_mixin` 後計費引擎零新增（wallet 扣款、ECPay 異步開票、
dunning 連續失敗自動取消、`_force_*_dates_from_plan` 日期強制）。SA 實作三個 hook +
掛入日次 cron：

```
訂閱建立（沿用 Cloud Service flow：選 plan → trial 或 skip-trial）
  └→ SA record (pending)
      └→ _do_provision：operator 建 tunnel → 存 tunnel_id/token
          → 自動建 primary route（{canonical}.{base_domain} → 預設 service_url）
          → state: active

日次 cron `_cron_enforce_subscription_lifecycle`
  ※ 必須把 SA model 加進掃描清單——漏加 = 訂閱永不被強制執行（探勘標出的 silent gap）
  ├─ expired 超過 grace（config 預設 7 天）→ _do_suspend
  ├─ 恢復訂閱（renewed/active）            → _do_resume
  └─ cancelled 超過 cleanup_months(6)      → _do_cleanup
```

| Hook | Cloudflare 動作 | 冪等性 |
|---|---|---|
| `_do_suspend` | ① 全部 route ingress → `http_status:503`（DB 保留原 `service_url`）② email rules 全部 disable。Access apps、custom domains、DNS、tunnel token 全保留 | 可重跑 |
| `_do_resume` | ① ingress 還原各 route `service_url` ② email rules re-enable | 可重跑 |
| `_do_cleanup` | 安全順序逐項刪：email rules → email routing 停用 → destinations → custom hostnames → Access apps → DNS records → tunnel（token 失效）→ full_zone zones | 每步先查再刪，部分失敗可重入續刪 |

- **使用者主動刪除** = `unlink()` override 走 `_do_cleanup` 同一條路
  （`cloud-service-unlink-override-cleanup` + `direct-unlink-bypass-cleanup-hook`
  pattern）——「若刪除，record 與 Cloudflare 資源一併刪除」的需求落點。
- **失敗語義**：provision / suspend / resume 任一 CF 步驟失敗 → `state='error'`、
  不靜默重試、等人工介入（SmartHome 慣例）；cleanup 例外——可重入重試直到清完。
- P1 時 `_do_suspend` 只處理 routes；P4 擴充 email 停用（hook 對不存在的子資源天然跳過）。

---

## §4 前端（Standalone OWL App `/woow`）

### 路由（workspace-scoped，對齊 Cloud Service 既有規則）

```
#/workspace/:wsId/security-access                       列表
#/workspace/:wsId/security-access/:id/overview          detail（tab 為路徑段）
#/workspace/:wsId/security-access/:id/configuration
#/workspace/:wsId/security-access/:id/application
#/workspace/:wsId/security-access/:id/access
#/workspace/:wsId/security-access/:id/email
#/workspace/:wsId/security-access/:id/domains
```

**入口**：dashboard「Explore More Services」三卡之 Security Access（綠盾，
"Setup Zero Trust Tunnels"）Get Started → 該 workspace 的 SA 建立流程；卡片如尚未存在
則本次補上。

**建立流程**：`PlanSelector`（Entry/Pro 卡，顯示四 quota）→ `SkipTrialModal`
（trial 或立即付費 + wallet 餘額預估）→ detail 頁輪詢 provisioning → active。
三元件原件複用、零新建。

### 6 Tab

| Tab | 內容 |
|---|---|
| Overview | 狀態、方案層級 + 下次扣款日（FR-7）、tunnel connector 連線狀態、primary hostname |
| Configuration | **security_access record 自身設定**：nickname 編輯、tunnel 接入資訊（token 複製、Docker/HAOS 啟動指令、NPM 反向代理教學連結 FR-8） |
| Application | **application routes 管理**：新增（prefix 輸入**即時 availability check**）、service_url（預設值自後台設定）、Host header 開關、每列「受保護」toggle |
| Access | 照 mockup：保護總開關、Login Methods 四 toggle、Access Policies CRUD（名稱/allow-block/規則/session）、Default session duration |
| Email | 照 mockup：Email Routing 開關（網域未 full_zone 時鎖住並導向 Domains tab 升級）、Routing Rules CRUD、Catch-all、Destinations（驗證狀態） |
| Domains | 照 mockup：網域清單（DNS/SSL badge）、Add Domain → CNAME+TXT 指示卡、Verify、Set primary、「升級 NS 委派」流程（顯示分配 nameservers，解鎖 Email） |

Header：icon + nickname（可編輯）+ 狀態 badge + primary hostname 連結 + 刪除按鈕
（confirm modal 說明會一併刪 Cloudflare 設定）。「Rollback」不做；「Domain」= 跳 tab。

### Odoo Controller（backend-for-frontend）

新 `src/controllers/security_access.py`，鏡像 `smart_home.py` 門控
（`_get_workspace_or_error` + mutation 才查 owner/admin role）。

**路徑慣例**：BFF 走 workspace 前綴（鏡像 `/api/workspaces/<ws>/smarthomes`），
平台層資源不掛 workspace（鏡像 `/api/smart-home/templates`）——與 HA API 的
`/api/security-access/*`（§5）天然區隔，不撞路徑：

```
# workspace-scoped（session auth + _get_workspace_or_error）
GET/POST  /api/workspaces/<ws>/security-accesses                  列表（ir.rule 過濾，不 sudo）/ 建立（plan + quota 檢查）
GET/PATCH/DELETE /api/workspaces/<ws>/security-accesses/<id>      detail（6 tab 全部資料）/ nickname·access 設定 / 刪除（連動清理鏈）
POST/PATCH/DELETE /api/workspaces/<ws>/security-accesses/<id>/routes（/<rid>）
POST/PATCH/DELETE /api/workspaces/<ws>/security-accesses/<id>/policies（/<pid>）
POST   /api/workspaces/<ws>/security-accesses/<id>/domains（/<did> + verify / set-primary / upgrade-zone / DELETE）
POST/PATCH/DELETE /api/workspaces/<ws>/security-accesses/<id>/email/rules（/<rid>）
POST   /api/workspaces/<ws>/security-accesses/<id>/email/destinations（+ resend-verify / DELETE）
POST   /api/workspaces/<ws>/security-accesses/<id>/change-plan
POST   /api/workspaces/<ws>/security-accesses/<id>/skip-trial-estimate、/skip-trial
       ↑ 訂閱三件組直接鏡像 smarthomes 既有端點

# 平台層（不掛 workspace）
GET    /api/security-access/templates                    template 列表
GET    /api/security-access/templates/<id>/plans         方案列表（含 quota）
GET    /api/security-access/routes/check?prefix=         availability 反饋
       ↑ subdomain 是全平台 flat namespace，本來就不是 workspace 視角的查詢
```

> **drift note**：上面 `GET .../routes/check?prefix=` 是草擬寫法；實作為
> `POST /api/security-access/routes/check`、`prefix` 在 JSON body（見 §2 的
> drift note 與 `src/controllers/security_access.py:api_route_check`）。契約以
> plan + 程式為準。

前端 `services/security_access.js` 沿用 `{success, data, error}` envelope；
SCSS `.o_woow_sa_*` 前綴 + 既有變數體系；`StatusBadge` 加 SA 狀態映射。

---

## §5 HA Component 串接 API + 文件

背景：`woow_paas_smart_home` component 走 OAuth2 登入 PaaS → 列服務 → 選定 →
取 tunnel token → `cloudflared_manager` 啟動 connector。未來擴展可選 Smart Home **或**
Security Access 來源；本次**不改 component**，只在 PaaS 端備好 API + 文件。

新 endpoints 加在 `src/controllers/ha_api.py` 同檔案（直接共用 `_authenticate` /
`_get_user_workspaces` / `_check_workspace_access`，不複製邏輯）：

路徑形狀**完全鏡像** `/api/smarthome/*` 的三層結構（workspaces → instances →
token/status），component 未來擴展時 contract 形狀一致：

```
GET /api/security-access/workspaces                     可存取的 workspaces（鏡像 /api/smarthome/workspaces）
GET /api/security-access/workspaces/<ws>/accesses       該 workspace 的 SA 服務（id、nickname、canonical_name、
                                                        primary_hostname、state、方案層級、下次扣款日 ← FR-9 sensor）
GET /api/security-access/accesses/<id>                  detail（鏡像 …/homes/<id>）
GET /api/security-access/accesses/<id>/tunnel-token     tunnel token（高權限 scope）
GET /api/security-access/accesses/<id>/status           connector 連線狀態
```

- OAuth2 scope：鏡像 smarthome scope 命名慣例新增 SA scope（read 與 token/secret 分級），
  並授權給既有 public client（`woow-ha-smart-home`）——漏做會 token refresh 失敗（探勘 G-H3）。
- **交付文件** `docs/reference/api/security-access-ha-integration.md`：OAuth 流程、
  endpoint 規格 + request/response 範例、與 `/api/smarthome/*` 對照表、未來 component
  擴展建議（service-type 選擇步驟；config entry unique_id 須含 service type，避免同
  workspace SH+SA 相撞——探勘 G-H4 地雷）。

---

## §6 Odoo 後台（管理介面 + 設定）

| 項目 | 內容 |
|---|---|
| Views + Menu | `src/views/security_access_views.xml`：list / form view（form 以 notebook 分頁呈現 routes / policies / domains / email rules），menu 掛「Woow PaaS」root menu |
| Settings | `res.config.settings` 新欄位：預設反向代理 URL（`config_parameter`，預設 `http://localhost:80`）；同步加進 `src/hooks.py` `_CONFIG_PARAMETERS`（卸載清理，CLAUDE.md 規範） |
| Data seeds | `subscription_products.xml` 加 SA Entry / Pro 方案（含四 quota 值）、SA template seed、OAuth scope seed |
| Security | `ir.model.access.csv` + `ir_rules.xml`（§1 對稱 ir.rule 落地） |

---

## §7 測試策略 + 分期交付

### 測試（TDD：每階段先寫測試）

| 層 | 測試 | 守線 |
|---|---|---|
| Odoo model | quota constraint、canonical_name 推導、reference_id 不可變、狀態機、suspend/resume/cleanup（mock operator）、刪除鏈冪等 | 規格定義 + 回歸 |
| Odoo ACL | ir.rule 正反向 ORM 測試 + **每個 list/get endpoint 一條 HttpCase cross-user negative** | `orm-vs-endpoint-acl-test-coverage`：ORM 綠 ≠ endpoint secure |
| Lifecycle cron | SA 在掃描清單、expired→suspend、renewed→resume、cancel→cleanup | 「漏掛 cron = 永不執行」silent gap |
| Operator unit | 4 組新 endpoints、availability、命名 stamping 驗證、fail-fast credential 驗證（mock httpx） | HTTP contract |
| Operator integration | 既有 `-m integration` 不回歸；CF 真實 API staging smoke script（手動、文件化） | CF 無本地模擬 |
| E2E（merge 後） | deploy-stg + 新 operator image：playwright-cli `--headed` 訂閱流程 → 6 tab → availability 反饋 → 凍結/恢復；HA API OAuth curl smoke | 全鏈驗證 |

測試紀律：斷言用 `>=` / 存在性鎖 intent，不硬編 schema 快照
（`phase-versioned-test-assertion` pattern，避免 P2-P4 演進撞 test-vs-test red）。

### 分期交付（4 個獨立可合併 PR phase）

| Phase | 內容 | 獨立性 |
|---|---|---|
| **P1 主線** | model family + lifecycle hooks + 訂閱 products + 後台 menu/settings + 前端 list/detail（Overview + Configuration）+ dashboard 入口卡 + route availability + HA API + 串接文件 | 訂閱→tunnel→route→503 凍結→恢復→刪除全鏈閉環；Access/Email/Domains tab 以 Coming Soon 佔位 |
| **P2 Access** | operator Access endpoints + policy 接線 + Access tab + 逐 route 保護開關 | 疊加在 P1 route 上 |
| **P3 Domains** | operator custom-hostnames + zones endpoints + Domains tab（CNAME 軌 + NS 升級軌） | 獨立網域子系統 |
| **P4 Email** | operator email endpoints + email 接線 + Email tab + suspend 時 email 停用整合 | 依賴 P3 full_zone |

每 phase 內部：TDD（紅→綠）→ 實作 → code review + 對抗式審查（ACL sudo grep audit、
silent-failure hunter 掃 operator 錯誤路徑）→ k8s 本地 sandbox 驗證 → docs 持久化。

### 工作環境與收尾

- 開發在本 Floor（`feat/security-access`，已隔離）；phase 內平行 agent 各開 git
  worktree 子分支，完成併回 feature branch。
- 每 phase PR → develop；全部 merge 後請 maestri「deploy-stg」agent 跑
  `/woow:deploy-stg` + `build-operator` 出新 operator image → E2E。
- 知識持久化：本 spec 進 `docs/superpowers/specs/`；實作完成後 `/docs:promote` 升級為
  `docs/reference/features/security-access.md` + API reference；新 pattern 進
  `docs/decisions/patterns/`。

---

## Known risks

1. **單一 CF account token 多租戶邊界**（operator 全域 `account_id`）：v1 靠
   `paas-sa-` 命名前綴隔離；per-tenant RBAC 為 follow-up。
2. **NS 委派 onboarding 門檻**：full_zone 升級需客戶到 registrar 改 NS（傳播 24-48h），
   Email tab 對未升級網域顯示引導而非報錯。
3. **Cloudflare plan 限制**：Custom Hostnames（SaaS）免費額度 100 個 / Email Routing
   免費——超量時的商務策略不在本設計範圍。
4. **subdomain flat namespace**：`subdomain_prefix` 全平台唯一，熱門前綴（如 `home`）
   先到先得；availability check 提供即時反饋。

## 必讀 pattern docs（實作前對照）

- `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md`
- `docs/decisions/patterns/two-layer-acl-lockstep.md`
- `docs/decisions/patterns/scope-flip-write-guard-not-ir-rule.md`
- `docs/decisions/patterns/cloud-service-unlink-override-cleanup.md`（+ `direct-unlink-bypass-cleanup-hook.md`）
- `docs/decisions/patterns/plan-driven-date-helper.md`
- `docs/decisions/patterns/phase-versioned-test-assertion.md`
- `docs/decisions/patterns/odoo-std-field-no-override.md`
- `docs/tracking/pitfalls/cross-workspace-task-data-leak.md`（B1 反面教材）
- `docs/tracking/pitfalls/cross-repo-artifact-landing-assumption.md`（operator 落點勘查）
