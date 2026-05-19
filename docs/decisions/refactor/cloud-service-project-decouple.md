# Decision: 解耦 CloudService 與 Project

**Date**: 2026-05-17
**Status**: accepted
**Components**: `project.project`, `project.task`, `woow_paas_platform.cloud_service`, `ir.rule`, AI prompt sanitizer, `/woow#/projects` 入口

## Context

Refactor 前的耦合：每個 `project.project` 必須綁一個 `cloud_service`（前端阻擋未綁、後端 unique constraint、record rule 對 `cloud_service_id IS NULL` 全放行）。耦合走 3 條腿同時撐住 — 任何一條被誤拆都會炸租戶隔離。

實際目標：Project 可以**獨立存在**；同時要保留「綁 Service 時，Task chatter 能看到 Service 資訊」這條主要使用者路徑。要求是 surgical：**0 schema migration、< 10 分鐘 rollback、stg 既有 Project 行為不變**。

兩個 pre-existing 缺口在這次必須一起處理，否則「解耦」本身就會 trigger leak：
- `cloud_service` 有 ACL CSV 但**沒 ir.rule**（隔離靠 controller endpoint，Task M2M widget 走 ORM 會繞過）
- `project.project` 既有 rule 對 `cloud_service_id IS NULL` 短路放行（今天靠前端阻擋未爆，「Project 可獨立」一上 leak 立刻浮）

跨平面動作：Schema（Task 加欄位）、ACL（Project / Task / CloudService 三個 model 同步收緊）、UX（路徑 alias + Modal + Task form panel）、AI prompt（secret sanitize + extras 合併）。

## Decision

採取「最小可逆解耦 + 必要 root-cause 補洞」：

- **保留** `unique_cloud_service` SQL constraint；只放寬「綁定可空」這一點。Schema 0 變動。
- 用 **Task 端 M2M extras** 表達「task 可參考多個 service」，不擴 Project ↔ Service 為 1:N。
- 對 Odoo std rule 採取**對等 deactivation**：`project.project_public_members_rule` + `project.task_visibility_rule` + `project.task_visibility_rule_project_user` 三條一起拔（**對等**是 invariant — 詳見 [[odoo-addon-std-rule-symmetric-deactivation]]）。
- 用 addon 自有的 `*_rule_workspace_member` 三條規則（Project / Task / CloudService 各一）把 ACL 收回 workspace member 範圍。
- AI prompt 注入時，`helm_values` 走 pattern sanitizer + 三欄位硬 blacklist；redacted 字面值 `***REDACTED***`。
- UX 上 `/woow#/projects` 為新 canonical 路徑；舊 `/woow#/ai-assistant` 與 `/woow#/ai-assistant/projects` 保留為 alias 供既有書籤。

## Considered options

本節記錄「為什麼這類選擇對這類情境是對的」，不重複 plan §Rejected options 的 9 條逐項對比。

### 1. 保留 unique constraint vs 拆成 1:N（D1）

**為什麼這類 refactor 不擴 1:N**：1:1 → 1:N 是 **schema-level 不可逆動作**。當第二筆 binding 落地，rollback 就會丟資料。Refactor 的契約是「不能 break stg」+「< 10 分鐘 rollback」，這種 schema 跨越不能塞進「解耦」這個 scope。

**對未來而言**：1:N 是另一個 feature，要走自己的 plan + migration + 資料 audit。本次留 constraint 不擋未來改，但**改的時候必須走 schema migration、補資料 audit**。

### 2. Task 端 M2M extras vs mail.message reference vs UI-only（D2）

**為什麼 schema 表達優於 mail.message reference**：使用者選的「額外 service 參考」是**意圖**，需要持久化、可被 AI prompt 組裝程式碼直接讀（不解 message body）、可被前端 chip 直接渲染。mail.message reference 三個能力都做不到（藏在 body / 不可 query / UI 不可發現）。

**為什麼 schema 表達優於 UI-only**：UI-only 解法等於把意圖塞在前端 state 內，AI prompt 注入要重新對使用者拉取，且失去 cross-session 持久性。M2M 是 Odoo idiomatic 的「多對多參考」做法，成本只是新表 + 兩欄位，且 ORM auto-create rollback-friendly（drop table 即清）。

### 3. unbound Project ACL 走 project member 而非要求必填 workspace_id（D3）

**為什麼貼齊 Odoo 原生 project 隔離邏輯比改 schema 便宜**：
- Odoo 原生 `project.project` 的隔離心智模型就是「follower / project member」，所有第三方 project 工具（views / wizards / mail.thread integration）都假設此模型。
- D3=B（把 Project 加必填 workspace_id）等於跨入 Odoo project 模型的「主要欄位」層，要回填現有 row、改 form views、處理 `_compute_workspace_id` 與必填的 chicken-and-egg。Scope 暴漲。
- 我們的 D3=A 改寫 record rule，對 Odoo 模型只動了 ACL，**Project 本身的 schema 與 attribute 完全不變**。

**對未來而言**：若 PaaS 將來想擁有獨立於 Service 的 workspace 邏輯（如沒綁 service 也想要 workspace 隔離），那是新 feature；不適合塞進「解耦」refactor。

### 4. URL alias 而非 rename / 新增 menu（D4）

**為什麼 alias 是 net-positive**：既有書籤 / 第三方整合用 `/woow#/ai-assistant` 入口。Rename 會破書籤，新增 menu 會 UI 重複。Alias 是兩條 path 指向同一 routeName，net cost 是 router config 一行多增加。

**Invariant**：未來若要徹底 sunset 舊路徑，須先做使用者轉移統計（看舊 path 是否還有實際流量），不是直接拔。

### 5. 同 workspace 收緊 vs user-visible workspace（D6）

**為什麼收緊比放寬便宜**：D6=B（user 可 view 全 workspace 範圍當 extras 候選）違反多租戶隔離直覺，且**目前沒有實際 cross-workspace task 用例**。先收緊 — 哪天真的有需求，再放寬一條 domain 加一些 cross-workspace permission 即可；反向（先放寬、撞到問題再收緊）要處理「已經建立的 cross-workspace extras 怎麼辦」的資料審計，貴得多。

### 6. AI prompt 注入 helm_values 全併 vs 不注入（D7）

**為什麼合併注入比完全捨棄更好**：使用者明確掛 extras 的目的就是讓 AI 知道這些 service 存在；不注入等於 extras 退化成純 UI 連結。**但合併代價是 secret 風險面擴大** — 所以 D9 sanitizer 是 D7 的必要前提，不能拆開做。

### 7. Pattern sanitizer 上線、Whitelist 留 follow-up（D9）

**為什麼分兩階段而非一步到位 whitelist**：
- Pattern sanitizer 立刻可上線，覆蓋當前已知的 sensitive key（password / token / api_key / secret / credential）+ 三欄位硬 blacklist。
- Whitelist 需要對**每張 chart** 的 helm_values keys 做 inventory + curation；ship 不出來會延誤 PR；ship 出來會把 AI debug 能用的資訊砍掉太多。
- 分兩階段的 invariant：階段 1 的 telemetry 要記錄「redact 命中次數 + 命中 keys」，否則階段 2 的 whitelist 不知道該白哪些。Telemetry 補上是 follow-up 一部分。

### 8. Odoo std rule 對等 deactivation（B1 incident 補洞 — 詳見 [[cross-workspace-task-data-leak]]）

**為什麼三條 Odoo std rule 必須對等拔，不能只拔 project 端**：

Odoo `ir_rule.py:140-168` 對同 group 多 rule 採 **OR 語意**。`project.project_public_members_rule` 與 `project.task_visibility_rule` 都套 `base.group_user`，domain 都含 always-true leg `privacy_visibility != 'followers'`（對 Odoo default `'employees'` 永遠為真）。

→ 自己寫一條 restrictive rule 套到同 group，被 OR 旁路；自己加 NOT clause 無效（NOT 限的是自家 rule，不是 std rule）；換 security group 等於跨 Odoo 模型動 user 設定，scope 暴漲。

→ **唯一 root-cause-correct 解就是 deactivate std rule**，且必須對 model + 對等 sub-model 同步處理。Project 端拔了、Task 端沒拔 = leak（CAQA B1 抓到）。

**Invariant**：未來在 `base.group_user` 上加任何 Project 或 Task model 的 ACL 收緊規則，**必須**對等檢查 std rule 是否已 deactivate；新 PR 改動 `ir.model.access.csv` 給 `base.group_user` 加 `project.project` / `project.task` RWCU 時，也必須對等檢查。

### 9. 4 Open Qs 拍板（Q1-Q4）

| Q | 拍板 | 為什麼這類選擇是對的 |
|---|------|-------------------|
| Q1 unbound rule 是哪一條腿 | `message_partner_ids` + `create_uid` 雙腿 | `user_ids` 不在 Odoo 18 `project.project` schema（見 [[plan-vs-odoo-schema-mismatch]]）；`create_uid` 是必要的 self-create-not-visible 防呆 — 沒 create_uid 腿，使用者建完自己 unbound project 立刻看不到 |
| Q2 extras domain static / dynamic | Static via `task.workspace_id` related | Related field unbound 時自動評為 `False`，static domain `[('workspace_id', '=', workspace_id)]` 結果是「候選為空」— UI 一致、無 NoneType 風險；Dynamic 用 onchange 是工程量加倍且無 UX gain |
| Q3 incompatible warning 欄位形式 | Computed M2M | 給 UI per-chip 紅框的最小資訊密度；Boolean 退化成「有東西不對」但無法定位；字串訊息固定 UI 表達能力 |
| Q4 redacted sentinel literal | `***REDACTED***` hardcode | 不 i18n — 它是 audit token 不是使用者文案；保持 grep-able 對未來 incident 反查直接可用 |

## Consequences

### Positive

- **0 schema migration**、半小時內可 rollback（M2M 表 + 3 條 std rule 對等 reactivate）。
- 補上 2 個 pre-existing 隔離缺口（cloud_service 沒 ir.rule、project rule 對 unbound 全放行），且**用同一個 PR 把 base.group_user 在 Project / Task 兩個 model 的 ACL 全部對齊**（這在獨立 PR 內做反而會留時間窗 leak）。
- Task chatter 從「單一 service 寫死」變成「primary + extras」可組合，AI prompt 對使用者意圖更貼合。
- Sanitizer pattern + blacklist 雙保險：pattern 抓未來 chart 引入的新 sensitive key，blacklist 抓「key 名不像 secret 但實際是 secret」的明確情境。

### Negative / 要持續注意

- **B1-class leak 在未來增改 ACL 時可能復活**。每次有人改 `ir.model.access.csv` 給 `base.group_user` 加 `project.project` / `project.task` 權限，或新增 `ir.rule` 套同 group，都要重新跑「對等 std rule deactivation」checklist（見 [[odoo-addon-std-rule-symmetric-deactivation]]）。
- **`base.group_user` 失去「所有非 followers project / task 可見」這條 std 行為**。對純 Odoo project 使用情境是 break-change；本 instance 沒有第三方 project consumer（已驗證），但若未來在同一 Odoo instance 上線非 PaaS 的 project 使用，要重新 review。
- **Sanitizer 不對 list-of-strings 遮罩**。helm_values 結構罕見此 case，但理論存在；若未來 chart 有 `["password123", ...]` 這樣的純字串 list，會漏網（low-severity caveat）。
- **`incompatible_extra_cloud_service_ids` 是非 store compute**。目前 view 只用 form，將來若放 list view 會 N+1。
- **`_check_project_access` / `_check_task_access` 對 unbound project 全放行**（pre-existing IDOR；本次 refactor 放大 surface — unbound project 從「無法存在」變「可存在」）。屬於 follow-up，不在本次 fix 範圍（see [[follow-ups]]）。
- **`_get_cloud_service_context` 對 incompatible extras 仍注入 AI prompt**（plan AC #13 未明訂 strip；secret 已 sanitize 但 service name / subdomain 仍洩漏跨 workspace 語意資訊）。屬於 follow-up。

## Invariants（後人改本區前必看）

1. **`project.project._sql_constraints['unique_cloud_service']`** 不可拔。要拔就是把 Project ↔ Service 改成 1:N，必須走獨立 plan + schema migration + 既有資料 audit。
2. **`project.project.cloud_service_id` 必須維持 `ondelete='set null'`**。改成 cascade 或 restrict 會把刪 service 連坐刪 project，違反「Project 可獨立存在」的目標。
3. **`project.project.workspace_id` 必須維持 `compute='_compute_workspace_id'` + `store=True` + `related-style` 對 `cloud_service_id.workspace_id`**。任何把它改成手動寫入或 unstored compute 的動作會破 AC #3 與 stg 既有資料一致性。
4. **3 條 Odoo std rule 必須一直 active=false**：
   - `project.project_public_members_rule`
   - `project.task_visibility_rule`
   - `project.task_visibility_rule_project_user`
   恢復其中任一條，B1 leak（[[cross-workspace-task-data-leak]]）立刻復活。Migration `src/migrations/18.0.1.0.23/post-migrate.py:93-107` 是這個 invariant 的 enforcement 點。
5. **`base.group_user` 在 `project.project` / `project.task` 上的 ACL 必須 + ir.rule 框住**：今天 `ir.model.access.csv:26-27` 給 RWCU，靠 `project_rule_workspace_member` + `project_task_rule_workspace_member` 兩條 rule 收緊。任何拔 rule 但留 RWCU 的動作 = leak。
6. **AI prompt 注入 `helm_values` 必須經 `_sanitize_helm_values`**（`src/models/discuss_channel.py:41-49`）。新增任何把 `service.helm_values` 流向使用者可見 surface（AI prompt / logs / API response）的程式碼，要先檢查 sanitizer 是否覆蓋。
7. **`***REDACTED***` 字面 literal 是 audit token**。改名要全 grep 同步（codebase + tests + plan + 既有 incident docs）。

## See also

- Patterns:
  - [[odoo-addon-std-rule-symmetric-deactivation]] — addon override Odoo std ACL 的對等處置原則
  - [[paas-workspace-member-acl-tightening]] — 在 addon 內收緊 `base.group_user` ACL 的 idiomatic pattern
- Pitfalls:
  - [[cross-workspace-task-data-leak]] — B1 incident 完整脈絡
  - [[plan-vs-odoo-schema-mismatch]] — AC#5 `user_ids` 不存在事件
- Follow-ups:
  - [[follow-ups]] — 9 條未處理事項追蹤
- Inputs（合成來源，原始紀錄）:
  - `docs/plans/2026-Q2/2026-05-16-cloud-service-project-decouple-plan.md`
  - `docs/tracking/test-reports/test-scope-report-2026-05-16.md`
  - `docs/tracking/test-reports/test-report-2026-05-16.md`
  - `docs/decisions/reviews/caqa-2026-05-16.md`
  - `docs/decisions/reviews/pr-review-verdict-2026-05-17.md`
  - `docs/decisions/reviews/pr-description-draft.md`
- 關鍵 code：
  - `src/migrations/18.0.1.0.23/post-migrate.py:93-107`（B1 fix invariant enforcement）
  - `src/security/ir_rules.xml:65-117`（3 條 addon ir.rule）
  - `src/security/ir.model.access.csv:26-27`（base.group_user RWCU）
  - `src/models/discuss_channel.py:13-49`（sanitizer）
  - `src/models/project_task.py:31-72`（M2M extras + incompatible compute）
  - `src/models/project_project.py:27-33`（unique constraint 保留）
