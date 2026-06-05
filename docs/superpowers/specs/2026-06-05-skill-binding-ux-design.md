# Design: Project Skill Binding UX（後台兩層一站式 + 前台唯讀化）

> 狀態：design approved（brainstorming 2026-06-05），待 writing-plans。
> 來源：2026-06-03「paas-project-protal-hermes」會議決議（議題 D / F / H）+
> 使用者需求三項（R1 後台 project 頁可設定 skill、R2 skill form 可設 scope 與
> 關聯 project、R3 `/woow#/projects/{id}` 前台唯讀）。
> 必讀前置 pattern：`docs/decisions/patterns/resource-ownership-vs-template-activation.md`
> （本設計的資料模型語意基準）。
> **硬前置（block writing-plans）**：該 doc 目前只在主 repo 本地 develop
> `c8c42f0`、尚未 push，本 floor branch 上不存在 — 實作開始前必須先 push 主
> repo develop 並把 floor branch 同步到含該 doc 的 commit，否則 §5 第一列的
> doc 對齊任務無法在 floor 完成、reviewer 無從核對引用。
> 自足摘要（不依賴外部 doc 即可驗證本 spec 的引用）：
> **anti-pattern #3** = 把歸屬列表（`project.skill_ids`）當生效集合顯示
> 「agent 可用」— 歸屬 ≠ 生效，未被 resolved template 綁定的 user-scope
> skill 不進 pod；**anti-pattern #4** = 繞過 subset constraint 直接 `sudo()`
> 寫 template 綁定 — constraint 是 cross-tenant 防線的一部分。

## 背景（現況診斷）

skill 在三層都「看得到但動不了」，使用者卡住的根因：

1. **skill form 全欄位唯讀**（`src/views/skill_views.xml:30-88`）：`scope` 欄位
   model 層存在（`src/models/skill.py:33-41`）但 form 完全沒放；`project_id` 是
   view 層 `readonly="1"`（`skill_views.xml:67`），連 admin 都被 UI 擋住 — 而
   model `write()` guard（`skill.py:150-167`）本來就放行 `group_system`。
2. **project form 的 Skills 內嵌 list 點了沒反應**：`create="false" delete="false"`
   （`src/views/project_views.xml:78`），刻意設計（skill 是 bundle-backed，
   F-SKU-1 紀律：只能走 upload wizard / agent-runtime API 建立）。但 wizard
   **寫死 `scope='system'、project_id=False`**（`src/wizard/skill_upload_wizard.py:57-59`）
   → 全系統沒有任何 UI 路徑能建立 user-scope（綁 project 的）skill。
3. **「綁定」其實有兩層**（ownership vs activation，詳見 pattern doc）：
   - 歸屬層：`skill.project_id` M2O（project form 的 Skills 列表是它的 O2M 反向）
   - 生效層：`agent_template.skill_ids` M2M — pod 實際載入由
     `_build_skill_snapshot`（`src/models/agent_session.py:299-315`）決定：
     resolved template 綁定的 published skills ∪ system published skills（同名遮蔽）。
   - **歸屬 ≠ 生效**：project 名下的 user-scope skill 沒被 resolved template 綁定
     就不進 pod；目前無任何自動同步，UI 也沒有任何生效狀態提示。
4. **前台 `/woow#/projects/{id}`** 目前可編輯 name/description（BasicInfoSection）、
   cloud service binding（CloudServiceBindingSection）、user-scope MCP CRUD
   （McpServersSection）；skill 在前台完全沒有 UI。後端兩支 endpoint
   （`src/controllers/paas.py:1521` / `:1607`）全用 `.sudo()` + controller role gate。

會議決議（2026-06-03）：MCP/Skill 設定移到 Project 層級由**後台**設定（議題 D、F：
我們幫客人開好 project、挑好 skill/MCP）；前台齒輪**拿掉** Cloud Service binding
與 MCP 設定、name/Description **保留但唯讀**（議題 H）。

## 目標

1. **R2**：skill form（admin）可編輯 `scope`（system/user）與 `project_id`（單一
   project — m2o 架構不變，「綁定 project 列表」不存在也不引入）。
2. **R1**：project 後台 form 一頁完成兩層操作 — 上傳 user-scope skill（歸屬）+
   管理 resolved template 的 skill 綁定（生效），含共用 template 的
   copy-on-write fork 防護。
3. **R3**：前台 project 設定頁唯讀化，由 feature flag 封鎖（預設 off），未來翻
   參數即恢復整套編輯 UI。

## 非目標（YAGNI）

- **不動 skill↔project 的 m2o 架構**（不升級 m2m）— 跨 project 共用 = scope=system；
  per-template 多組合 = agent_template m2m，皆已存在。
- 不動 5 條 skill ir.rule、ir.model.access.csv、scope-flip write guard、
  subset constraint、ir.attachment anchor / side-channel rule。
- 不放寬 wizard ACL（維持 `group_system`）；不開放前台上傳/綁定 skill。
- 前台不新增 skill 展示 section（照會議字面，最小攻擊面）。
- 不動 MCP legacy 消費路徑（`discuss_channel.py:240` 歸屬即啟用），前台唯讀化
  只關 CRUD 寫入。
- 不做 pin-to-version（`agent-runtime-pod-design.md:1615` 預留，另案）。

## 設計決策（brainstorming 定案）

| # | 決策點 | 選擇 | 理由 |
|---|--------|------|------|
| 1 | 綁定資料模型 | **維持 m2o + 兩層一站式 UX** | 不動 schema；D4 constraint / 雙 partial unique index / is_latest 分組 / 5 條 ir.rule / subset constraint 全相容 |
| 2 | 前台收斂範圍 | **照會議字面** | 只留 name+description 唯讀；CloudServiceBinding、McpServers section 移除（flag off 時不渲染）；不加 skill 展示 |
| 3 | 封鎖/重開策略 | **feature flag**（`ir.config_parameter`，預設 off） | 「未來若要開放」= 翻參數；舊正向測試保值（開 flag 跑）；兩層 lock-step 同一開關 |
| 4 | 共用 template 防護 | **copy-on-write fork** | 共用（平台預設/多 project）template 唯讀 + 「建立專屬 template」按鈕；不可能誤改共用；平台預設 tenant-neutral invariant 安全 |

---

## §1 Skill form（後台，admin）

`src/views/skill_views.xml` form view 變更：

- 新增 `scope` 欄位（Selection radio），可編輯。
- `project_id` 拿掉 view 層 `readonly="1"`，改：
  `invisible="scope != 'user'"`（鏡像 mcp form 既有 pattern
  `src/views/mcp_server_views.xml:42-44`）+ `required="scope == 'user'"`
  （**本設計新增**，MCP form 並無此屬性 — D4 一致性實際由 model constraint
  守，view required 只是 UX 引導）。
- 其餘 bundle 欄位（name/version/sha256/…）維持 readonly — bundle-backed 紀律不變。

**Model 層不動 ACL/guard**：`write()` guard（`skill.py:150-167`）已放行
`group_system`；D4 constraint（`skill.py:118-131`）擋 scope/project 不一致；
雙 partial unique index 擋重複（改綁撞名版本時自然 fail loud）。

**連帶補強（model）**：`skill.write()` 在 `scope` / `project_id` 實際變更時，
比照 state 變更路徑（`skill.py:175-186`）觸發：

1. is_latest 重算 — 新舊兩個分組都要：**現有 `_recompute_is_latest`
   （`skill.py:294-308`）不能直接呼叫兩次** — 它經
   `_is_latest_group_domain`（`:277-292`）讀 `self` 的「當下值」，write 後
   只能算到新組，舊組會留 stale is_latest。實作必須：write 前 capture 舊
   `(scope, project_id, name)` key，並把重算入口重構為可帶 explicit group
   key（如 `_recompute_is_latest_for(group_domain)`），舊組、新組各呼叫
   一次（對抗審查 Lens 2 結論）。
2. `_push_snapshot_to_running_pods`：best-effort 推新 snapshot（搬出與搬入的
   project session 都要推；scope flip 至/自 system 時推全域）。

## §2 Project form（後台）— 兩層一站式

`src/views/project_views.xml` Settings tab 的 Skills 區塊改造為兩個小節。
**新增的操作按鈕/可編輯欄位一律 `groups="base.group_system"`**（會議 F：
後台幫客人設定）。語意釐清（對抗審查 Lens 2/3 結論）：

- view `groups=` 對非該群組使用者是**整個節點不渲染**，不是 readonly —
  非 admin 的 internal user 看到的「唯讀現況」來自保留未加 groups 的既有
  readonly skill list，新元件對他們是 absent。
- **UI 收斂 ≠ 安全邊界**：model 層既有 ir.rule
  `agent_template_rule_project_admin`（`ir_rules.xml:300-309`）允許
  project owner/admin（`base.group_user`）對 project-bound template 全
  CRUD — 即 project owner 仍可經 ORM/RPC 寫 dedicated template 的
  `skill_ids`，這是**既有刻意設計、本案不變更**。若日後要連 project owner
  都鎖，須收緊該 ir.rule 並補對等測試（two-layer lock-step），列入
  follow-up 另案決策（見「開放問題」）。

### §2.1 歸屬區（Skills owned by this project）

- 現有 `skill_ids` 內嵌 list 保持 `create="false" delete="false"`（不破壞
  F-SKU-1），上方新增 **Upload Skill** object button → 開 upload wizard，
  context 預填 `default_project_id=本 project`、`default_scope='user'`。
- list 新增 **「生效中」computed 欄位**（boolean，store=False）：該 skill 是否
  會進本 project 的 pod。實作要求（對抗審查 Lens 2 結論）：
  1. **先重構再共用**：`_build_skill_snapshot`（回 list[dict]）無法直接被
     boolean compute 複用 — 須先抽 `_effective_skill_ids(project)` helper
     （回 skill recordset = resolved template bound published ∪ system
     published，含同名遮蔽語意），再讓 `_build_skill_snapshot` 改 consume
     它序列化。禁止平行第二份公式。
  2. **防 N+1**：compute 以 project 為單位**一次**解析（resolve template +
     system search 各一次），對 self 多列做 in-memory membership 判定；
     不可 per-row 重複 resolve。
  3. snapshot 是 pod 載入來源 — 重構必須由測試鎖
     「`_build_skill_snapshot` 輸出不變（含同名遮蔽）」（見 §4.5）。
  此欄位是 pattern doc anti-pattern #3（歸屬列表誤標「agent 可用」）的
  UI mitigation：明確標示真生效而非暗示歸屬即生效。

### §2.2 Wizard 擴充（`src/wizard/skill_upload_wizard.py`）

- 加 `scope`（Selection，default 依 context：從 project form 進 = `user`；從
  Configuration menu 進 = `system`）與 `project_id`（M2O，
  `invisible/required` 隨 scope，context 預填）。
- `action_upload` 查重改 per-scope：system → `(scope,name,version)`；user →
  `(project_id,name,version)`（對齊雙 partial unique index 語意，
  `skill.py:102-116`）。
- 建立的 skill 帶使用者選的 scope/project（取代寫死 system）。
- wizard ACL 維持 `group_system`（`ir.model.access.csv:22` 不動）。

### §2.3 生效區（Agent Template binding）

新小節「Agent Template」：

- 顯示 **resolved template**：`project.project` 加 computed M2O
  `resolved_agent_template_id`（store=False，compute 呼叫既有
  `_resolve_agent_template`，`agent_session.py:267-288`）+ 共用狀態提示
  （computed selection：`dedicated` / `shared` / `platform_default` / `none`）。
- **dedicated**（template 只綁本 project）→ 可直接編輯綁定：editable related
  M2M 呈現 `resolved template.skill_ids`，widget `many2many_tags`，domain =
  `['|', ('scope','=','system'), '&', ('scope','=','user'), ('project_id','=',本 project)]`
  （與 subset constraint 一致，UI 先濾、constraint 後守）。
- **shared / platform_default** → 綁定唯讀顯示 + **「建立專屬 template」按鈕**
  （copy-on-write fork）：
  1. **必須單一 `copy(default={...})` 呼叫一次到位**，default 同時覆寫：
     `name`（帶 project 名）、`project_ids=[(6,0,[本 project])]`、
     `skill_ids=[(6,0, 過濾後)]`、`mcp_server_ids=[(6,0, 過濾後)]`、
     `state='published'`、`is_public=False`。
     理由（對抗審查 Lens 2/3 結論）：Odoo 18 Many2many 預設 `copy=True` —
     裸 `copy()` 會把來源的 project_ids/skill_ids/mcp_server_ids 整包帶進
     fork，「先 copy 再循序 write」存在兩個違規中間態：(a) fork 帶來源
     project_ids → `_check_project_ids_grantor_authority`
     （`agent_template.py:94-122`）可能直接 ValidationError；(b) 改完
     project_ids 但 skill_ids 仍含他 project 的 user-scope skill → subset
     constraint 炸。default 一次帶入讓所有 constraint 在 create 收尾一次
     校驗通過。
  2. 過濾規則：來源 `skill_ids` / `mcp_server_ids` 各自過濾成
     （scope=system ∪ 本 project user-scope）— 不以 sudo 繞 subset
     constraint（pattern doc anti-pattern #4：sudo 路徑寫入前自行負責等價
     驗證，本設計直接不走 sudo）。
  3. 同步寫 `project.agent_template_id = 新 template`（belt-and-braces；解析
     順序①「project-bound published」本就會優先命中它）。
  4. fork 寫 `project_ids` 受 HC-3 grantor authority check 管 — 按鈕限
     `group_system`（admin），authority 滿足；測試以 admin 身分跑。
  5. **平台預設 template 永不被修改**（tenant-neutral invariant +
     `_check_not_platform_default_when_bound` 不對其放行綁 project）。
- **none**（連平台預設都解析不到）→ 只顯示提示文字導向 Settings 設定平台預設
  template（不提供 fork-from-zero — 從零建立缺 `model_config_id` 來源是
  plan gap，且 seed default + post_init auto-point 落地後 none 幾乎不出現，
  YAGNI）。
- 實作註記：editable related M2M 若因 ORM 限制（write-through 穿過
  non-stored computed M2O）不可行，fallback 為「編輯綁定」按鈕開 resolved
  template 的 form modal（`res_id` 動態）— 行為等價，writing-plans 時擇一
  驗證後定案，撞牆即回報（plan-gap-stop-and-report）。

**連帶補強（model）**：`agent_template.write()` 在 `skill_ids` /
`mcp_server_ids` 實際變更時，對其綁定 projects 的 active session best-effort
`_push_snapshot_to_running_pods`（目前只有 skill state 變更會推，
`skill.py:175-186`；template 綁定變更是新觸發點）。

## §3 /woow 前台唯讀化 + feature flag

### Flag

- 新 `ir.config_parameter`：`woow_paas_platform.portal_project_settings_editable`，
  **預設 off**（參數不存在 = off，fail-closed）。
- 同步加進 `src/hooks.py` `_CONFIG_PARAMETERS`（卸載清理，CLAUDE.md 規定）。
- `res.config.settings` 加對應欄位（Settings → Woow PaaS 可翻），用
  `config_parameter=`。

### 後端（two-layer lock-step，同一 flag 同時 gate 兩層）

`src/controllers/paas.py`：

flag gate 必須覆蓋**所有**寫 project 設定欄位的 endpoint（缺一支 = 唯讀被
API 繞過，違反 two-layer-acl-lockstep）。盤點後共三支：

- `paas.py` `api_support_project_detail`（`:1521`）：`action='update'` 在
  flag off 時回 `{'error': 'Permission denied'}`（沿用該 endpoint 既有
  role-gate 措辭 `paas.py:1545`，不引入第三種字串）；`action='get'` 保留，
  response 增加 `portal_editable: <bool>` 供前端渲染。
- `paas.py` `api_support_project_mcp_servers`（`:1607`）：write_actions
  （create/update/delete/sync/test/sidecar-hint）flag off 時回
  `Permission denied`（同 `:1631` 既有措辭）；`list` 保留。
- `ai_assistant.py` `api_support_projects`（`:1303`，`/api/support/projects`）：
  `action='update'` 分支（`:1340` → `_update_project:1571`，寫
  name/description）同樣納入 flag gate — 這是對抗審查抓到的繞過路徑。
  `create`/`delete` **不 gate**（project 建立/刪除不屬「project 設定頁唯讀」
  範圍，會議未決議收掉；若要收另案處理）。
- **不新增任何 sudo 路徑**；既有 `_resolve_project_access` /
  `_check_project_access` + role gate 不動（flag 是在 role gate 之前的更
  外層閘）。
- **Payload 取捨（明確記錄）**：flag off 時 `_format_project_detail`
  （`paas.py:1549-1569`）仍回 cloud_service / mcp_server_ids 資料 — 無跨租戶
  leak（皆本 project 自有資料、access 已由 resolver 守），刻意不最小化以便
  flag on 即恢復、減少分支。

### 前端（OWL）

- `ProjectDetailPage`：依 `portal_editable` 條件渲染 —
  - off：只掛 BasicInfoSection（唯讀模式 prop）；CloudServiceBindingSection、
    McpServersSection **不渲染**（程式碼保留）。
  - on：三 section 全掛，行為同現況。
- `BasicInfoSection`：加 `readonly` prop — off 時不渲染 name/description 編輯鈕
  與 editor，只留唯讀顯示（Workspace 本來就唯讀）。
- SCSS 不刪（flag on 時仍用）。

## §4 測試策略（TDD）

### 既有測試調整（不刪除，保值）

- `test_project_detail_endpoints.py` 7 個正向 update/rebind/unbind 測試、
  `test_project_mcp_project_centric.py` 6 個正向 CRUD/sync/test 測試：
  **setUp 開 flag** 繼續跑（守 flag-on 行為不回歸）。
- `/api/support/projects` `action=update` 的既有正向測試（writing-plans 時
  grep 盤點）同樣 setUp 開 flag。
- ~~T1 create~~（`test_project_decouple_frontend_endpoint.py:162`）**不在
  調整清單** — 它測 create，本案不 gate create，flag 對其無影響
  （對抗審查 Lens 1 修正）。

### 新增守線

1. **flag-off denied（HttpCase，endpoint-level）**：owner 與 admin 對
   `action=update` / MCP write_actions 均被拒（`Permission denied`）；
   `get`/`list` 仍可用且 `portal_editable=False` — 遵守
   orm-vs-endpoint-acl-test-coverage pattern（controller 層守線必須
   HttpCase，不可只用 ORM with_user）。**必含 cross-endpoint negative**：
   `/api/support/projects` `action=update`（ai_assistant.py）flag off 同樣
   被拒 — 守 §3 的 BLOCKER 修正不回歸。
2. **wizard user-scope 上傳**：scope=user+project 建立成功、D4 擋不一致、
   per-scope 查重（user 同名同版同 project 擋、不同 project 不擋）、非 admin
   無 wizard ACL。
3. **skill form scope/project 編輯**：admin 可改（is_latest 兩組重算、snapshot
   push 觸發）、非 admin 被 write guard 擋（既有 `test_skill_dual_scope.py:165`
   已覆蓋 flip lock，補 project_id 改綁案例）。
4. **fork（copy-on-write）**：建立後 `_resolve_agent_template` 優先命中新
   template；來源綁定過濾正確（他 project 的 user-scope skill 不被帶入）；
   subset constraint 全程綠；平台預設 template 內容不變；fork 後
   `default_agent_template_id` 仍指向平台預設（不被 clobber）。
5. **「生效中」computed 欄位 + helper 重構**：欄位值與
   `_build_skill_snapshot` 結果一致（bound published ✓、未綁 user-scope ✗、
   system published ✓、draft ✗）；**並鎖重構不變式** —
   `_build_skill_snapshot` 改 consume `_effective_skill_ids` 後輸出
   （含同名遮蔽、欄位序列化）與重構前完全一致。
6. **template 綁定變更 push**：`skill_ids` 變更觸發 best-effort push（mock
   client，失敗不 abort — 對齊 #111 語意）。
7. **view arch 守線更新**：`test_project_form_mcp_skill_tab.py`（skills 區塊
   保持 create=false/delete=false + 新按鈕 groups + 生效欄）、
   `test_skill_admin_views.py`（form 有 scope、project_id 可編輯且
   invisible/required 條件正確）。
8. **phase-versioned-test-assertion 紀律**：新斷言用存在性/單調性，不硬編
   schema 快照值。

### e2e（playwright）

- 本地 k8s sandbox（`k8s-dev-sandbox`；Floor `.env` 一律絕對路徑 —
  floor-env-relative-path pitfall）：後台 project form 上傳 skill → fork
  template → 綁定 → 生效欄變 ✓ 的全流程；前台 flag off 唯讀驗證
  （無編輯鈕、無 MCP/binding section）。
- 完整串流（pod 真載入 skill）等 user merge 後在 develop 跑
  `/woow:deploy-stg` 驗證（本機 operator 過舊只能驗到 spawn 邊界，
  gap-chain pitfall 2026-06-05 節）。

## §5 文件更新（知識庫「可改→唯讀」修正）

| 檔案 | 動作 |
|---|---|
| `docs/decisions/patterns/resource-ownership-vs-template-activation.md` | **小心對齊**：錨點表行號位移（`project_views.xml:76`、`skill.py` 等）；新增兩個 snapshot push 觸發點（skill scope/project 變更、template 綁定變更）入「生效集合公式」周邊敘述；「生效中」UI 欄位補記為 anti-pattern #3 的 mitigation 案例。（doc 在主 repo 本地 develop `c8c42f0`，未 push — 實作前先同步） |
| `docs/decisions/refactor/mcp-skill-two-layer-config-entry.md` | L31「（會議議題 H：… — 另案）」→ 已落地，補前台唯讀 + flag 描述 |
| `docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` | 加 superseded 註記（D10/AC#1-3/#14/#16 編輯行為改由 flag gate，預設 off） |
| `docs/plans/2026-Q2/2026-05-30-skill-dual-scope-design.md` | L200「/woow 前台建 user skill」→ 改後台 wizard 路徑 |
| `docs/plans/2026-Q2/2026-05-30-skill-upload-phase2-design.md` | L39/L155 前台 user skill 上傳 phase 標記：本設計以後台 wizard 取代，前台上傳不做 |
| `docs/guides/paas-skill-system/03-skill-design.md` | L138-142 上傳者表：user skill 上傳者改「admin（後台 wizard，從 project form 進入）」 |
| `docs/tracking/followups/follow-ups.md` | 新增本 feature 條目（含 flag 名、未來開放方式） |
| 本檔 | design doc 本體 |

## §6 安全 invariants（全程必守，對抗式審查 checklist）

1. scope-flip write guard 不放寬（admin 放行是既有行為；非 admin 一律
   AccessError）。
2. 平台預設 template 永遠 unbound + 不綁 user-scope 資源（tenant-neutral）；
   fork 建新 template、絕不寫共用 template。
3. subset constraint 不繞過（fork 過濾先行、不 sudo 寫綁定）。
4. two-layer lock-step：flag 同時 gate endpoint 與 UI；任何一層單獨收緊/放寬
   即是 bug。flag gate 覆蓋面 = **全部三支**寫 project 設定的 endpoint
   （`paas.py` 兩支 + `ai_assistant.py` `api_support_projects` update），
   漏一支即違反本條。
5. controller 不新增 `.sudo()` 繞 ir.rule 的讀寫路徑（34 處 sudo audit 紀律）。
6. 「生效中」欄位與 `_build_skill_snapshot` 共用同一 helper，禁止平行第二份
   公式（drift = anti-pattern #3 復發）。
7. wizard / fork / template 編輯 UI 全部 `groups="base.group_system"`
   （UI 收斂；非該 group 不渲染。model 層授權沿用既有 agent_template
   ir.rule，本案不放寬也不收緊）。
8. `config_parameter` 進 `hooks.py _CONFIG_PARAMETERS`。

## §7 執行方式

- **硬前置**：主 repo develop push 含 `resource-ownership-vs-template-activation.md`
  的 commits（`c3953a5`/`c8c42f0`）→ floor branch 同步（fetch + rebase 或
  merge develop）。未完成前不進 writing-plans。
- 工作目錄：本 maestri Floor（branch `feat/mac-skill-binding-ux`）≡ 隔離要求
  已滿足；需平行改檔的子 agent 用 worktree isolation。
- 流程：writing-plans 拆 task → TDD（test 先紅後綠）→ 對抗式審查（§6
  checklist + 兩個 reviewer lens：security / reuse-simplification）→ 本地
  k8s sandbox 驗證 → docs 更新 → PR（zh-tw 描述）。
- e2e 串流驗證：merge 後由 maestri agent「deploy-stg」在 develop 跑
  `/woow:deploy-stg`。

## 開放問題（已決，記錄供 writing-plans 引用）

- 前台 MCP「唯讀 vs 移除」→ **移除**（flag off 不渲染；flag on 完整恢復）。
- skill「綁定 project 列表」→ **不存在**（m2o），form 顯示單一 project 欄位。
- 共用 template 直接編輯 → **否**，一律 fork（subset constraint 對平台預設
  綁 user-scope skill 本來就會 ValidationError，直接編輯是 UX 死路）。

## 已知限制與 follow-up（本案不處理，落 follow-ups.md）

1. **MCP inline form 無 sync/test 鈕**：legacy chat 路徑要求 mcp_server
   `state='connected'`（`discuss_channel.py:240-242`），而 connected 只能由
   `action_sync_tools` / `action_test_connection` 設定 — project form inline
   MCP form（`project_views.xml:59-73`）沒有這兩顆按鈕，admin 在 project
   form 建完 MCP 後須跳獨立 MCP view 完成 sync 才會被 legacy 路徑吃到。
   後台一站式若要補齊，另案在 inline form 加 sync/test 按鈕。
2. **project owner 經 RPC 寫 dedicated template（既有行為）**：
   `agent_template_rule_project_admin` 允許之；若會議意圖是連 project owner
   都不可自編 template，須另案收緊 ir.rule + 對等測試（two-layer
   lock-step），本案僅做 UI 收斂不動 model 授權。
