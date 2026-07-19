---
name: caqa-report-2026-05-28-decuple-project-frontend
phase: caqa
team: decuple-project (feature agent pod)
analyst: decuple-project-caqa
epic: cloud-service-project-decouple (frontend + controller 收尾)
cycle: TC4 follow-on（plan A+A+A signed-off 2026-05-28；13/13 全綠後 CAQA）
scope: 本 branch fix/decuple-project 改動 + 同 controller (ai_assistant.py) / 同模組 sub-model CRUD 漏掃；invariants 對齊複驗
diff_base: develop
branch: fix/decuple-project
plan_ref: docs/plans/2026-Q2/2026-05-28-decouple-project-from-workspace-frontend-plan.md
decision_ref: docs/decisions/refactor/cloud-service-project-decouple.md
test_report_ref: docs/archived/tracking/2026-05-28-decouple-project-frontend-endpoint.md
created: 2026-05-28
updated: 2026-05-28 (mini re-verdict appended after F-DP-2 fix)
verdict: APPROVED WITH CAVEATS  # mini re-verdict 2026-05-28: F-DP-2 closed; CAVEAT #2 unchanged → follow-up; Task.sudo() unify caqa-recommends option (b) — leader pick
counts_original: { blocker: 1, caveat: 4, info: 5, out_of_scope_followup: 2 }
counts_reverdict_a1: { blocker: 0, caveat: 4, info: 5, out_of_scope_followup: 2, new_findings: 0 }
counts_mini_reverdict_dp2: { blocker: 0, caveat: 3, info: 5, out_of_scope_followup: 1, new_findings: 0, decision_points: 1 }
tools: [direct-diff-read, grep, Read, ir_rules.xml 對照]
status: archived
archived-from: docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md
archived: 2026-07-19
archived-reason: "一次性 CAQA 驗收報告，cycle 已收官；patterns/pitfalls 證據錨點引用已改指本處"
---

# CAQA Report — Decouple Project（Frontend + Controller 收尾 + TC4 follow-on）

> 本 PR 6 個改動點分為 (a) plan A+A+A 5 點 + (b) TC4 follow-on `_list_projects` 移 sudo 1 點。
> CAQA 範圍：(a)(b) 全部 + 同 controller (ai_assistant.py) 內同類「sudo bypass ir.rule list」漏掃廣度有界 audit。

---

## Verdict

**BLOCKED** — 1 condition blocking reviewer phase。

**Blocker 摘要**：本 PR fix 了 `_list_projects` 的 cross-tenant leak（TC4 follow-on），但**同 controller 同 endpoint family** 的 `_list_tasks` (ai_assistant.py:1325) 與 `_get_project_stages` (ai_assistant.py:1139-1156, entry @ line 1046) **存在完全同源的 sudo bypass leak / write surface，本 PR 沒一起修也沒明確開 follow-up**。

具體判決邏輯：
- TC4 follow-on 已證明 implementation 認知到「sudo().search 繞 ir.rule = cross-tenant leak」這個 attack vector
- Leader hand-off 明確要求 caqa「同 controller 其它 sudo() 路徑是否還有類似漏掃，若 ≥ 1 處則 high-severity 標記阻斷」
- 同源 grep 結果：**2 處同類 untouched 漏洞**（見 §同源 sudo bypass scan 章節）

**推薦行動**：
- Leader 拍板：(option A) 退回 implementation 一併 fix `_list_tasks` + `_get_project_stages`（與本 PR 同類，scope 同質）；或 (option B) 本 PR scope 收斂為「decouple project 收尾」**並明確開 P1 follow-up issue** 鎖住 `_list_tasks` / `_get_project_stages` 在下個 cycle 必修

---

## Plan alignment check（先過）

對照 plan §Affected files 5 點 + TC4 follow-on 1 點：

| Plan 改動點 | 實際 diff | 對齊 |
|---|---|---|
| `ai_assistant.py:1038-1040` 移 dispatcher guard | `git diff` line 1038-1040 移除 ✓ | ✅ |
| `ai_assistant.py:1206-1242` `_create_project` 加 `message_subscribe` | line 1230-1238 `project.sudo().message_subscribe(partner_ids=[request.env.user.partner_id.id])` ✓ | ✅ |
| `_list_projects` 移 `sudo()`（TC4 follow-on）| line 1186-1190 改 `request.env['project.project'].search(domain)`（無 sudo）+ comment 註明 dual-leg ir.rule walk ✓ | ✅ |
| `CreateProjectModal.js:124-126` 移 hard guard | line 121-160 移 hard guard + 三分支（cs / ws / unbound）✓ | ✅ |
| `CreateProjectModal.xml:41-63` workspace optional + No workspace option | line 40-63 label `Workspace (optional)` + option `— No workspace — (Personal project)` ✓ | ✅ |
| `support_service.js` `createUnboundProject` | line 267-292 新增（payload + URL `/api/support/projects`）✓ | ✅ |
| D3=A cloud_service dropdown hide on no-workspace | XML line 66 `t-if="state.workspaceId"` ✓ | ✅ |

**Plan alignment：100% 對齊**，方向無走偏。

---

## 四面向結果

### Quality

#### `info` — 三 service caller method 結構高度重複 — `src/static/src/paas/services/support_service.js:247-320`

`createProject` / `createUnboundProject` / `createProjectForService` 三個 method 主體 ≈ 一模一樣（differ 只在 URL 與 operationLoading key），各 ~20 行。implementation 自查標記「三分支只一處用，不抽 helper」— 評估：抽 helper 可 dedupe ~40 行但增加一層間接，且三個 method 有獨立 jsDoc + 不同 URL pattern；**結論 implementation 判斷可接受，作為 low-priority 後續演進機會**。若未來新增第 4 種 create 方式（如 `createProjectForTemplate`），建議一併抽 `_createProjectRequest(url, data, operationKey)` helper。

#### `info` — `_check_project_access` docstring 引用錯誤 — `src/controllers/ai_assistant.py:48-52`

```
- Unbound project (no cloud_service): requires follower membership
  (``message_partner_ids``). This must mirror the ir.rule
  ``mcp_server_rule_workspace_user`` leg 2 — diverging from it
  re-opens the IDOR surface where the rule layer denies but the
  controller layer still serves the row.
```

docstring 註解的 mirror 對象是 `mcp_server_rule_workspace_user`，但本 helper 是 project model 用，應該對應 `project_rule_workspace_member`（位於 `src/security/ir_rules.xml:65-78`，與 mcp_server rule 是兄弟而非同條 rule）。docstring drift。

**修正建議**：將 `mcp_server_rule_workspace_user` 改為 `project_rule_workspace_member`。或補述「兩條 rule 在 unbound leg 共享同一 `message_partner_ids` 結構」，避免誤導後人去動 mcp_server 那條 rule 卻沒同步動 project 那條。

#### `info` — `CreateProjectModal._loadWorkspaces` only-one-workspace auto-select 與 personal project flow 有摩擦 — `src/static/src/paas/components/modal/CreateProjectModal.js:48-50`

```js
if (!this.state.workspaceId && this.state.workspaces.length === 1) {
    this.state.workspaceId = this.state.workspaces[0].id;
    this._loadCloudServices(this.state.workspaceId);
}
```

D1=A 引入 personal project 後，「使用者只屬一個 workspace 想建 personal project」flow 變成：modal 一打開 workspaceId 自動填到唯一 workspace（且開始 load cloud services），使用者要手動切回「— No workspace —」才能建 personal project。

**評估**：對解耦初衷（personal project 是頭等公民）有 minor UX 摩擦。但對「典型使用者進 modal 就是要綁 workspace」是 net positive。**implementation 沒動這條 auto-select，是合理的「最小改動」選擇**。建議列為 follow-up：reporter phase 評估是否值得 D1=A 後同步重新審視 auto-select policy（例如 personal project entry 用 dedicated button，避開這條 default 行為）。

### Security

#### `blocker` — **同 controller 同類「sudo bypass list」漏網**：`_list_tasks` — `src/controllers/ai_assistant.py:1325`

**Attack vector**：

```
POST /api/support/tasks
  body: { action: 'list' }   # 無 workspace_id, 無 project_id
```

```python
# Line 1313-1331
def _list_tasks(self, workspace, params: dict) -> dict[str, Any]:
    project_id = params.get('project_id')
    domain = []
    if workspace:
        domain.append(('project_id.cloud_service_id.workspace_id', '=', workspace.id))
    if project_id:
        try:
            domain.append(('project_id', '=', int(project_id)))
        except (ValueError, TypeError):
            return {'success': False, 'error': 'Invalid project_id'}

    tasks = request.env['project.task'].sudo().search(domain)  # ← domain == [] 時 leak
    data = [self._serialize_task(task) for task in tasks]
    return {'success': True, 'data': data, 'count': len(data)}
```

當 caller 用無 `workspace_id` URL + body 不帶 `project_id` 呼叫 `/api/support/tasks`，`domain = []`，`sudo().search([])` 回**全 db 任何 workspace 任何 project 任何 owner** 的 task → **cross-tenant data leak**（同 [[cross-workspace-task-data-leak]] B1 family）。

**ir.rule 已 ready**（`src/security/ir_rules.xml:108-117` `project_task_rule_workspace_member` dual-leg domain：bound leg via `project_id.cloud_service_ids.workspace_id.access_ids` + unbound leg via `project_id.message_partner_ids` / `project_id.create_uid`），但 controller `.sudo()` 強行繞過。

**與本 PR 的同源關係**：
- 本 PR TC4 follow-on 改 `_list_projects` (line 1186-1190) 從 `.sudo().search()` → `.search()`，理由是「project 在無 workspace_id + 無 cloud_service_id 入口會 cross-tenant leak」
- `_list_tasks` 是**完全同 endpoint family**（`/api/support/tasks` 與 `/api/support/projects` 對等）、**完全同 fix pattern**（移 sudo 走 ir.rule dual-leg）、**完全同 ir.rule baseline**（task 那條 dual-leg 與 project 那條對稱設計，line 65 與 line 108 對等）
- 修 _list_projects 但漏修 _list_tasks = root-cause-correct closure 不完整

**修法（推薦）**：

```python
# Line 1325 改為（去掉 .sudo()）
tasks = request.env['project.task'].search(domain)
```

新增 comment 對齊 `_list_projects` 註解：

```python
# Walk ir.rule (no sudo): project_task_rule_workspace_member dual-leg
# filters bound tasks by workspace membership, unbound tasks by their
# project's creator / follower sub-legs. The bare-URL list path (no
# workspace_id, no project_id) must NOT cross-tenant-leak.
tasks = request.env['project.task'].search(domain)
```

並對等補測試（同 T1+T2 idiom）：unbound endpoint create 後 cross-user `with_user` 不可見。

**Severity 規範對照**：data leak（cross-tenant task disclosure）→ HIGH。本條符合 leader hand-off 內「若發現 ≥1 處，high-severity 標記阻斷」rule。

#### `caveat` — `_get_project_stages` 雙重漏洞（pre-existing，本 PR 未引入也未修）— `src/controllers/ai_assistant.py:1046, 1139-1177`

```python
@route('/api/support/projects/<int:project_id>/stages', auth='user', methods=['POST'], type='json')
def api_support_project_stages(self, project_id: int, **kwargs):
    stages = self._get_project_stages(project_id)  # ← 完全沒 access check
    return {'success': True, 'data': stages}

def _get_project_stages(self, project_id: int) -> list[dict]:
    stages = request.env['project.task.type'].sudo().search(
        [('project_ids', 'in', [project_id])],
        order='sequence asc',
    )
    if not stages:
        stages = self._ensure_default_stages(project_id)  # ← attacker-supplied project_id 會被 sudo create 寫入
    return [...]
```

兩重洞：
1. **Read leak**：attacker 可 enumerate 任意 `project_id`，拿到該 project 的 stage list（含自訂 stage names，可能洩漏 tenant business 語意如 `"ClientX Q3 Roadmap"`）
2. **Write surface via fallback**：若 attacker 命中尚無 stages 的 project_id，`_ensure_default_stages` 會 `TaskType.sudo().create()` 寫入 `New / In Progress / Done` 三 stage 並 `project_ids: [(4, project_id)]` 綁到 victim project — attacker 可對任意 project mass-create / clutter stage data

**為何標 caveat 而非 blocker**：
- pre-existing；不是本 PR 引入也不是本 PR 修
- 但**同 controller、同 endpoint family**（`/api/support/projects/<id>/stages` 與 `/api/support/projects` 對稱），與本 PR 的 sudo bypass family 高度同源
- 不阻擋本 PR scope，但 reporter 階段**必須**開 P1 follow-up，建議與 `_list_tasks` blocker 同期 fix

**修法**：
1. 加 access check（呼叫 `_check_project_access`，與 `_update_project` / `_delete_project` 同 idiom）
2. 評估是否能去 `_get_project_stages` 內的 `.sudo()`（`project.task.type` 可能無 ir.rule，需要查）
3. `_ensure_default_stages` 應該移到「使用者首次以 owner 身分進 project」的合法路徑，不是 list endpoint 副作用

#### `caveat` — `_check_project_access` 對 unbound 走 follower-only，與 ir.rule unbound dual-leg 不完全對等 — `src/controllers/ai_assistant.py:53-55`

```python
if not project.cloud_service_id:
    user_partner = request.env.user.partner_id
    return user_partner in project.message_partner_ids
```

ir.rule `project_rule_workspace_member` (line 68) 的 unbound clause 是 **dual sub-leg**：
```
'|', ('message_partner_ids', 'in', [user.partner_id.id]),
     ('create_uid', '=', user.id)
```

而 `_check_project_access` 對 unbound 只看 follower（`message_partner_ids`），**沒看 `create_uid`**。

**為何不算 blocker**：
- 本 PR D2=A 設計：`sudo().create()` 後 `create_uid = SUPERUSER`，所以 caller 不會走 create_uid sub-leg；message_subscribe 補 follower sub-leg → controller 的 follower-only check 與「實際生效的 leg」對齊
- 若未來 D2 切換到 D2=B（`with_user(user).create()` 讓 `create_uid = user`），controller 的 follower-only check 會 **拒** create_uid 應該放行的 row → controller layer 比 ir.rule layer **更嚴**，會 false-negative（合法 owner 被擋），不會 false-positive（IDOR）— 安全側 fail-closed，但 UX bug

**評估**：本 PR D2=A 設計下不是漏洞，但 `_check_project_access` 與 ir.rule unbound dual-leg **非對等鏡像**是隱性 coupling 點。建議：
- 本 PR scope 不動（D2=A 鎖定 follower 機制，controller follower-only 對齊）
- reporter 階段在 `_check_project_access` docstring 補一句「intentionally narrower than ir.rule unbound dual-leg：assumes D2=A subscribe-after-sudo-create idiom」
- 若 plan-level 未來考慮 D2=B，必須**同步**改本 helper（與 plan §D2 註解內「与 ir.rule 不一致需獨立 audit」呼應）

### Performance

#### `info` — `_list_projects` after-fix 的 N+1 風險 — `src/controllers/ai_assistant.py:1190-1201`

```python
projects = request.env['project.project'].search(domain)
data = [{
    'id': proj.id,
    'name': proj.name,
    'description': proj.description or '',
    'cloud_service_id': proj.cloud_service_id.id if proj.cloud_service_id else None,
    'cloud_service_name': proj.cloud_service_id.name if proj.cloud_service_id else '',
    'workspace_id': proj.workspace_id.id if proj.workspace_id else None,
    'workspace_name': proj.workspace_id.name if proj.workspace_id else '',
    'task_count': proj.task_count,
    'created_date': proj.create_date.isoformat() if proj.create_date else None,
} for proj in projects]
```

Odoo ORM 自動 prefetch 同 recordset 的同一 field，所以 for-loop 讀 `proj.cloud_service_id.name` / `proj.workspace_id.name` 不會 N+1（一次 fetch all）。`proj.task_count` 是 computed field（store=True 假設），亦不 N+1。

**結論**：無 N+1 風險，現況 fine。**implementation 沒做 prefetch 是 net positive**（避免 premature optimization）。

#### `info` — `_create_project` `message_subscribe` 是 1 額外 INSERT — `src/controllers/ai_assistant.py:1236-1238`

每次 create project 都 trigger `mail.followers` INSERT — 標準 Odoo `mail.thread` 行為，cost 可忽略。

### Architecture

#### `caveat` — Frontend 三分支 mode 在 `onSubmit` 而非 service layer — `src/static/src/paas/components/modal/CreateProjectModal.js:138-147`

```js
let result;
if (this.state.cloudServiceId) {
    result = await supportService.createProjectForService(...);
} else if (this.state.workspaceId) {
    result = await supportService.createProject(...);
} else {
    result = await supportService.createUnboundProject(payload);
}
```

三 mode dispatch 在 component 層（modal `onSubmit`），不是 service 層。**結構評估**：
- ✅ Service 層 keep simple（3 個獨立 method，無 mode switching）
- ✅ Modal 是唯一三 mode 都會碰到的 caller，分支寫在 modal 內 = local cohesion
- ⚠️ 未來若有第 2 個 caller（如 `SupportProjectsPage` 自家 trigger create），會被迫複製 dispatch
- **結論**：本 PR scope acceptable；future 演進若出現第 2 caller，建議抽 `supportService.createProjectAuto({workspaceId?, cloudServiceId?, ...payload})`，由 service 層 dispatch（簡化 caller）

**implementation 自查說「三分支只一處用，不抽」與此一致 — 同意**。

#### `info` — `_create_project` 的 `elif workspace:` 分支內 `cs_id` 處理是 legacy carve-out — `src/controllers/ai_assistant.py:1224-1228`

```python
elif workspace:
    # Legacy: find cloud_service_id from params if provided
    cs_id = params.get('cloud_service_id')
    if cs_id:
        vals['cloud_service_id'] = int(cs_id)
```

當 endpoint 走 `/api/support/projects/<workspace_id>` 且 body 帶 `cloud_service_id` 時，controller 接受 `cs_id` 並寫入。**但**：
- frontend `createProject(workspaceId, data)` (`support_service.js:247-265`) **不會** 在 `data` 內帶 `cloud_service_id`（payload 只有 `name` + `description`）
- 等同 dead code path

**結論**：legacy 預留，不影響 security（`int(cs_id)` 接受任意攻擊者輸入 → 寫到 `cloud_service_id` → ir.rule 不會擋 attacker write）。Wait — 應該驗證：sudo create 時 ir.rule 不會擋；如果 attacker 在 workspace 內，cloud_service 是另一 workspace 的，會綁錯？因為 attacker create 不會繞 unique_cloud_service constraint，且 sudo create 也不會走 cloud_service 的 ir.rule（只走 project 的 sudo write）。

**Sub-finding（low）**：`elif workspace:` 分支接受 `cloud_service_id` 從 attacker-controlled body 寫入，且 sudo create 不做 cross-workspace check — 雖然有 `unique_cloud_service` SQL constraint 擋「同一 cloud_service 已有 project」，但**不擋「attacker 把自己 workspace project 綁到別 workspace 的 cloud_service」**。
- 但這條 path 沒 frontend caller 實際 trigger
- 建議 reporter 階段評估：要嘛刪 dead code，要嘛補 cross-workspace check（呼叫 `_check_workspace_access(cloud_service.workspace_id)`）

**結論**：不阻 PR，列 follow-up。

---

## Plan alignment check

implementation 與 plan A+A+A signed-off **100% 對齊**（見前面 plan alignment 表）。**無走偏**。

---

## 同源 code path 檢查（sudo bypass scan）

對 `src/controllers/` 全 grep `.sudo().search\b`（不含 search_count），結果 33 處。逐一分類：

| 路徑 | 類型 | 評估 |
|---|---|---|
| `ai_assistant.py:96` `ai.config` sudo search | 平台共享資源列表（無 api_key 回 user）| 安全 — 設計即全 instance 共享 |
| `ai_assistant.py:118` `ai.assistant` sudo search | 同上 | 安全 |
| `ai_assistant.py:172` `mail.message` sudo search | 已先 `_check_channel_access` (line 161) | 安全 |
| `ai_assistant.py:180` `ai.assistant` sudo([]) for partner_id mapping | 同上 platform shared | 安全 |
| `ai_assistant.py:388` `project.task` sudo search by channel_id | channel 已驗證 access | 安全 |
| `ai_assistant.py:413, 595` `mail.message` by channel_id | 已驗證 channel | 安全 |
| `ai_assistant.py:772` `woow_paas_platform.project_access` by user.id | server-authoritative | 安全 |
| `ai_assistant.py:848, 880, 917` `project.task` by channel_id | server-internal resolver | 安全 |
| `ai_assistant.py:974` `project.project` for stats | bound leg only，**漏 unbound** | **caveat #1**（見下） |
| `ai_assistant.py:1150` `project.task.type` by project_ids | **無 project ownership check** | **blocker family #2**（見下） |
| `ai_assistant.py:1190` `project.project` 列 unbound | **已 TC4 follow-on fix（移 sudo）** | ✅ |
| `ai_assistant.py:1325` `project.task` 列 task | **同 _list_projects 同類漏 sudo** | **BLOCKER #1** |
| `billing.py:49` `payment.provider` | 平台 share | 安全 |
| `billing.py:64` `payment.token` by partner.id | server-authoritative | 安全 |
| `billing.py:900` `sh.subscription.subscription` | 待 verify partner scope | OOS（不在本 PR 同類）|
| `oauth2.py:*` 多處 token/client/code by token_value/client_id | server-internal resolver | 安全 |
| `ha_api.py:83, 100` `workspace_access` by user.id | server-authoritative | 安全 |
| `ha_api.py:174` `smart_home` by workspace.id（已驗證）| 安全 | 安全 |
| `agent_runtime_skill.py:113` `skill` by claim | server-internal | OOS |
| `agent_runtime_webhook.py:93` `agent_session` by token | server-internal | OOS |
| `dashboard.py:105, 257` `sh.subscription` / `wallet.topup` | 待 verify partner scope | OOS |
| `paas.py` `sudo()` calls | 都是針對已驗證 record 的 sudo().write | 安全 |
| `smart_home.py` | 都是 sudo() 屬性傳遞，無 list | 安全 |

**結論**：同 controller (ai_assistant.py) 內**確認 2 處同源洞**（一新發現 BLOCKER + 一 pre-existing CAVEAT 待 follow-up）+ **1 處 stats 漏 unbound leg**（caveat）。其他 controllers 因 scope 控制不在本 PR 同類，建議單獨 follow-up audit。

### 1. BLOCKER #1 — `_list_tasks` (line 1325) 詳見 §Security blocker 章節

### 2. CAVEAT #2 — `_get_project_stages` (line 1139) 詳見 §Security caveat 章節

### 3. CAVEAT #3 — `api_support_stats` accessible_projects 漏 unbound — `src/controllers/ai_assistant.py:972-976`

```python
accessible_projects = request.env['project.project'].sudo().search([
    ('cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
])
task_domain = [('project_id', 'in', accessible_projects.ids)]
```

domain 只走 **bound leg**（透過 cloud_service.workspace），完全沒 unbound leg。導致：
- unbound project（無 cloud_service）的 tasks **不算入 stats** → total/active/completion 數字偏低
- 與 _list_projects/_list_tasks (after-fix) ir.rule dual-leg 的可見性語意不對齊

**不是 leak**（stats 偏低不是 cross-tenant 越權），但是 **UX 不一致 bug**。

**修法**：
```python
accessible_projects = request.env['project.project'].search([])
# ir.rule project_rule_workspace_member dual-leg 自動處理 bound + unbound
task_domain = [('project_id', 'in', accessible_projects.ids)]
```

或直接 ir.rule 過濾 task 端：
```python
Task = request.env['project.task']  # no sudo, walk ir.rule dual-leg
total = Task.search_count([])
done = Task.search_count([('stage_id.name', 'in', ('Done', 'Cancelled'))])
```

**Severity**：Medium（UX bug + 同源寬度漏修；不是 cross-tenant leak）；建議與 BLOCKER #1 一起改。

---

## Known pitfalls 對照（leader hand-off 列的專案 Known pitfalls）

對照本 repo CLAUDE.md §Known patterns / pitfalls + plan 引用：

| Pattern / Pitfall | 本 PR 對齊狀況 | 結論 |
|---|---|---|
| [[cloud-service-project-decouple]] §Invariants 7 條 | 全部不衝突；#5 base.group_user ACL + ir.rule 對齊（_list_projects 改走 ir.rule = 強化 #5）| ✅ |
| [[paas-workspace-member-acl-tightening]] | `_create_project` 後 `message_subscribe` 對齊 unbound leg follower 機制；`_list_projects` 走 ir.rule dual-leg | ✅ |
| [[constraint-input-trust-expansion]] | `message_subscribe` 走 `request.env.user.partner_id`（server-authoritative），不從 kwargs 取 partner_id | ✅ |
| [[odoo-addon-std-rule-symmetric-deactivation]] | 本 PR 不動 std rule | ✅ 不適用 |
| [[odoo-std-field-no-override]] | 本 PR 不動 std field 型態 | ✅ 不適用 |
| [[cross-workspace-task-data-leak]] B1 family | **本 PR 主動 close `_list_projects` 同源 leak（TC4 follow-on）— 但漏 close `_list_tasks` / `_get_project_stages` 同源** | ⚠️ **同 family 漏修** → BLOCKER #1 |

**重要對照**：[[cross-workspace-task-data-leak]] B1 incident 的 root cause 是「CSV ACL 開放 + 漏 ir.rule scope-restrict」；本 PR 處理的是「ir.rule 已 ready，但 controller .sudo() 繞」— 同 root cause family 的不同層次表現。已抽出的 pattern [[paas-workspace-member-acl-tightening]] 在 §Controller filter 不夠 章節有提及，但**沒有專門講「controller 不能用 sudo() 取後 return user」這個常見錯誤**。pattern 建議補充（見下節）。

---

## 既有 invariants 對齊複驗

implementation 自查的 3 條：

| Invariant | implementation 自查 | CAQA 複驗 |
|---|---|---|
| [[cloud-service-project-decouple]] #5 `base.group_user` Project/Task ACL + ir.rule | 對齊（unbound clause follower-anchor）| ✅ **同意**（且 `_list_projects` 改走 ir.rule = 強化 #5）|
| [[paas-workspace-member-acl-tightening]] bound/unbound dual-leg | 對齊 follower 機制 | ✅ **同意**（補一點：`_check_project_access` 走 follower-only 與 ir.rule 的 dual sub-leg 非對等鏡像 — 是 D2=A 設計副效應，記入 caveat #4，不算違反 invariant）|
| [[constraint-input-trust-expansion]] search domain server-authoritative | 對齊 | ✅ **同意**（`_create_project` 走 `request.env.user.partner_id`，不從 kwargs 取 partner_id）|

額外複驗 invariants 1-4, 6, 7 是否間接破壞：

| # | Invariant | 本 PR 對 |
|---|---|---|
| 1 | `unique_cloud_service` SQL constraint 不可拔 | ✅ 不動 |
| 2 | `cloud_service_id` ondelete='set null' | ✅ 不動 |
| 3 | `workspace_id` compute/store/related | ✅ 不動 |
| 4 | 3 條 Odoo std rule active=false | ✅ 不動（migration `18.0.1.0.23` + `ir_rules.xml:262` 維持）|
| 5 | base.group_user Project/Task ACL + ir.rule | ✅ 對齊（已上方說明）|
| 6 | AI prompt 注入 `helm_values` 經 `_sanitize_helm_values` | ✅ 不動 |
| 7 | `***REDACTED***` literal | ✅ 不動 |

**結論**：所有 invariants 對齊。

---

## ORM vs endpoint coverage gap pattern doc 建議

### 結論：**建議寫**

理由：
- 本 PR fix 的 `_list_projects` sudo bypass 是 instance #1（顯性）
- §同源 sudo bypass scan 找到 `_list_tasks` (BLOCKER #1) 與 `_get_project_stages` (CAVEAT #2) — instance #2、#3
- [[paas-workspace-member-acl-tightening]] §Controller filter 不夠 提到 ORM 路徑會繞，但沒專章講「controller layer 自己也走 sudo() 繞 ir.rule」這個內部攻擊面
- backend test 5 條（ORM 層）綠不蘊含 endpoint 層綠 — 這個分層測試覆蓋斷層**未被任何現有 pattern 明寫**

### Pattern doc 草稿大綱（reporter phase 落筆）

**建議路徑**：`docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md`
（或合併進 [[paas-workspace-member-acl-tightening]] §Controller filter 不夠 章節作子節擴寫；視 reporter 判斷）

**標題**：Pattern: ORM-level test 綠 ≠ Endpoint-level secure — controller sudo bypass 必須對等 endpoint-test 守

**核心 invariant**：
> 對任何 ir.rule 收緊的 model，**ORM-level test（backend `TestCase` / `TransactionCase` + `with_user()` 驗 ir.rule）綠不保證 endpoint-level secure**。Controller 內任何 `request.env[X].sudo().search/browse/create/write` 如果結果直接 return 給 caller，等於 controller layer 自行繞 ir.rule。
>
> **守線層**：
> - L1（ORM 層）：ir.rule + ACL — backend test 守
> - L2（Controller 層）：禁止 `.sudo()` 後 list-and-return；list endpoint 必須 walk ir.rule（不 sudo）— **lint 或 test 守**
> - L3（Endpoint 層）：HttpCase + cross-user `with_user` 反射出 endpoint 端 ACL outcome — **本 pattern 的 must-have**
> - L4（Audit checklist）：每個新 list endpoint 必對應 cross-user endpoint test；CAQA / reviewer 必 grep `controller/.*sudo\(\)\.search` 並驗 caller path

**反例 case**（要列舉）：
- Instance #1：`_list_projects` (本 PR before TC4 follow-on) — `test_project_decouple.py` 5 條 backend test 全綠 + 沒寫 endpoint test → cross-tenant leak 通過 CI 直到 TC4 才被 catch
- Instance #2：`_list_tasks` (本 PR 漏修) — 同 ir.rule baseline ready，同 sudo bypass
- Instance #3：`_get_project_stages` (pre-existing) — 額外帶 write surface via fallback

**正例 case**：
- `_update_project` / `_delete_project` (line 1265, 1302) — sudo browse + `_check_project_access` 應用層 guard — 安全 idiom
- `billing.py:api_transactions` — sudo + server-authoritative `partner_id` filter — 安全 idiom

**Anti-pattern checklist**（reviewer 對照清單）：
| 反 pattern | 問題 | 修法 |
|-----------|------|------|
| Controller `Model.sudo().search(domain).map(to_dict)` 直接 return user | 繞 ir.rule list-disclosure | 去 sudo() 或加 server-authoritative domain 限制 |
| `domain = []` + user-supplied filter（不帶 server-authoritative leg）| 空 domain 走 sudo = 全 db leak | 強制至少一條 server-side anchor |
| ORM-level test 全綠 = 認定 secure | 缺 endpoint layer 反射 | 補 HttpCase cross-user negative |
| `_ensure_default_X` 在 list endpoint 內 fallback `sudo().create()` | list endpoint 副作用寫入 attacker-supplied id | 移到 explicit write endpoint，加 access check |

**See also**：
- [[paas-workspace-member-acl-tightening]] §Controller filter 不夠（rule 與 controller carve-out 對等）
- [[two-layer-acl-lockstep]] §rule + carve-out lockstep
- [[cross-workspace-task-data-leak]] B1 incident (root cause family)
- 本 caqa report（instance 證據）

---

## Blocker 清單（給 leader / implementation）

1. **`src/controllers/ai_assistant.py:1325`** `_list_tasks` `.sudo().search(domain)` 同類 cross-tenant leak — 與 TC4 follow-on fix 完全同源
   - **建議方向**：移 `.sudo()`，讓 `project_task_rule_workspace_member` dual-leg ir.rule 處理（baseline 已 ready）
   - **配套**：補 endpoint-level cross-user negative test（同 T2.bob 結構）
   - 不寫具體 patch — 由 implementation 決定 fix 範圍（是否要對等改 `_create_task` 走相同 idiom，是否要審視同 family 其他 list endpoint）

---

## Caveat 清單（給 leader 決定是否帶入 reviewer）

1. **`src/controllers/ai_assistant.py:1046, 1139`** `_get_project_stages` 缺 access check + 副作用寫入（pre-existing）— 不阻擋本 PR，但**必須**開 P1 follow-up；建議與 BLOCKER #1 同期 fix
2. **`src/controllers/ai_assistant.py:974`** `api_support_stats` accessible_projects 漏 unbound leg（pre-existing）— UX 不一致；建議與 BLOCKER #1 同期改成走 ir.rule
3. **`src/controllers/ai_assistant.py:53-55`** `_check_project_access` follower-only 與 ir.rule unbound dual-leg 非對等鏡像 — 是 D2=A 設計副效應；建議 reporter 階段在 docstring 補述「intentionally narrower than ir.rule unbound dual-leg：assumes D2=A subscribe-after-sudo-create idiom」
4. **`src/controllers/ai_assistant.py:1224-1228`** `_create_project` 內 `elif workspace:` legacy carve-out 接受 attacker `cloud_service_id` 但沒做 cross-workspace check — dead code path（無 frontend caller），建議 reporter 階段評估刪除或補 check

---

## Info 清單（low / minor — 不阻擋）

1. **`src/static/src/paas/services/support_service.js:247-320`** 三 service caller method 高度重複 — implementation 自查可接受；建議第 4 種 create 出現時抽 helper
2. **`src/controllers/ai_assistant.py:48-52`** `_check_project_access` docstring 引用 `mcp_server_rule_workspace_user` 應為 `project_rule_workspace_member`（docstring drift）
3. **`src/static/src/paas/components/modal/CreateProjectModal.js:48-50`** `_loadWorkspaces` only-one-workspace auto-select 對 personal project flow 有摩擦 — minor UX 建議
4. **`src/static/src/paas/components/modal/CreateProjectModal.xml:66`** D3=A hide DOM 沒給「unbound project 不能綁 service」inline hint — 對 D3=A 推薦理由（不暴露 cross-workspace 語意）正確；但首次體驗的使用者可能不知道為什麼選 No workspace 就沒看到 service dropdown — minor UX 建議
5. **Frontend** 三分支 dispatch 在 component `onSubmit` 而非 service 層 — 本 PR scope acceptable；future 若有第 2 caller 再抽

---

## Out-of-scope follow-ups（不阻 PR，建議追蹤）

1. **`api_support_stats`** 改走 ir.rule walk（解決 unbound 漏算 + 同 BLOCKER #1 一致化）
2. **Pattern doc**：`orm-vs-endpoint-acl-test-coverage.md` reporter phase 寫實際 doc，本 caqa report §pattern doc 草稿大綱 為輸入

---

## 工具 raw 輸出摘要

- **direct-diff-read**: `git diff` 5 files（含 untracked 1 file），完整 6 個 implementation 改動點對齊 plan A+A+A + TC4 follow-on
- **grep**: `src/controllers/` 全 sudo CRUD scan，33 處 `.sudo().search()` 逐一分類；ai_assistant.py 內 1 處 BLOCKER + 2 處 CAVEAT 同源 missed-fix
- **ir.rule 對照**: `src/security/ir_rules.xml:65-78`（project dual-leg）+ `:108-117`（task dual-leg）+ `:5-50`（workspace + workspace_access）+ `:130-208`（mcp dual-leg）+ `:289-469`（agent template / session / skill / project_access B1 fix 16 rules）
- **invariants 對照**: 7 條全綠
- **pattern doc 對照**: 3 條（paas-workspace-member-acl-tightening / constraint-input-trust-expansion / cross-workspace-task-data-leak）全綠對齊；建議補 orm-vs-endpoint pattern

---

## 下一 phase 建議

**Verdict: BLOCKED**

**option A（推薦）**：退回 implementation 一併補 fix `_list_tasks`（symmetric to TC4 follow-on）。理由：
- 同 controller、同 endpoint family、同 attack vector、同 fix pattern（移 sudo + ir.rule dual-leg ready）
- root-cause-correct closure 必須對等
- 若 ship 只 fix _list_projects 不 fix _list_tasks，等於告訴未來 maintainer「兩個漏洞被 catch 了一個」— 留 surface 與 B1 incident 同 family

**option B**：本 PR scope 收斂為「decouple project 收尾」**並明確開 P1 follow-up issue** 鎖住 `_list_tasks` 在下個 cycle 必修。caveat 1-4 同步開 follow-up。

**Leader 拍板**：
- option A：要不要本 PR 帶 fix _list_tasks（推薦：是 — scope 同質、改動 1 行 + 1 test）
- option B：能不能允許「同源洞分批修」（caqa 立場：偏向 no，但尊重 leader 對 PR scope 紀律的判斷）

無論 A 或 B，**reporter 階段必須**：
- 開 P1 follow-up 鎖 `_list_tasks` / `_get_project_stages` / `api_support_stats`（若 option B）
- 評估是否寫 `orm-vs-endpoint-acl-test-coverage.md` pattern doc（本 caqa report 已給草稿大綱）
- 在 [[cloud-service-project-decouple]] §Consequences 加 follow-up 連結

**Escalate-to-leader 的 design decision**：
- 是否同意 BLOCKER #1 阻擋本 PR（option A vs B 之選）
- pattern doc 是否確定要寫（reporter 階段判斷，但 CAQA 立場推薦寫）

---

## Re-verdict (2026-05-28, BLOCKER A1 fix verification)

> Leader 拍板 option A1：補修 `_list_tasks` 同 TC4 pattern。tester 補 T4 red，implementation 1 行 fix，14/14 全綠。本節為 read-only 複驗，不重跑 4 面向 audit；focus 在 BLOCKER #1 是否解 + 第二輪同類 sudo 掃描（補 read/browse 盲區）+ invariants/caveats 維持確認。

### Re-verdict TL;DR

| 項 | 值 |
|---|---|
| Verdict | **APPROVED WITH CAVEATS** |
| BLOCKER #1 (`_list_tasks:1325` sudo bypass) | ✅ **CLOSED** — fix landed at `src/controllers/ai_assistant.py:1325`（drop `.sudo()` + 4 行 idiom-aligned 註解），T4 endpoint test 加在 `src/tests/test_project_decouple_frontend_endpoint.py:574-817` |
| 同類 sudo 第二輪掃描（補 read/browse 盲區）| ✅ **無新 high-severity 發現** |
| CAVEAT #2 / #3 / #4 / Caveat-arch1 | ✅ **維持 follow-up status**（reporter phase 寫 follow-ups.md）|
| Invariants 1-7 + 3 pattern docs | ✅ **維持全綠**（本輪 fix 與 _list_projects fix 完全同構，無新 invariant 風險）|
| Next phase | **進 reviewer**，帶 4 caveats |

### 1. BLOCKER #1 — `_list_tasks:1325` fix verification

**Fix landed**（`src/controllers/ai_assistant.py:1322-1326`）：

```python
# Walk ir.rule (no sudo): project_task_rule_workspace_member dual-leg
# filters bound tasks by their project's workspace membership, unbound
# tasks by the project_id follower / creator sub-legs. The bare-URL
# list path (no workspace_id, no project_id) must NOT cross-tenant-leak.
tasks = request.env['project.task'].search(domain)
```

**對齊評估**：
- ✅ **Shape 完全對稱 `_list_projects:1186-1190` fix**（move `.sudo()` → 走 ir.rule + 4 行註解格式對齊）
- ✅ **註解明確點出**：dual-leg 的兩條 leg（bound via workspace member / unbound via follower or creator）+ bare-URL leak warning
- ✅ **走 `project_task_rule_workspace_member`**（`src/security/ir_rules.xml:108-117`），dual-leg 已 ready：
  ```
  '|', ('project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id),
  '&', ('project_id.cloud_service_ids', '=', False),
  '|', ('project_id.message_partner_ids', 'in', [user.partner_id.id]),
       ('project_id.create_uid', '=', user.id)
  ```

**Test landed**（`src/tests/test_project_decouple_frontend_endpoint.py:598-817`）：

`test_t4_endpoint_list_tasks_blocks_cross_user_leak`：
- **setup**: Alice 建 2 unbound + 1 bound project，各 seed 1 task（共 3 tasks）
- **attack reproducer**: Bob bare-URL `POST /api/support/tasks` `{action: 'list'}` → `assertFalse(alice_tids & bob_returned_tids)` 鎖 leak
- **bonus regression**: Alice 自己 list → `assertFalse(missing_from_alice)` 鎖 own visibility（避免 over-correct）

**Assertion 訊息品質**：
- ✅ 紅燈訊息**明確指向 `ai_assistant.py:1325`** + 修法（drop sudo 走 ir.rule）+ 對齊 ir.rule line 號（`ir_rules.xml:108-117`）
- ✅ 解釋為何**不能 ride on T3**（task-rule 獨立於 project-rule，分屬不同 ir.rule record；T3 修 _list_projects 不會 close _list_tasks surface）
- ✅ 解釋為何**ORM-layer task tests miss this**（`Task.with_user(user).search()` 走 ir.rule，但 controller endpoint sudo() 繞 ir.rule —— 這正是 §pattern doc 草稿大綱記錄的「ORM vs endpoint coverage gap」instance #2）

**14 條 test/TC 全綠 leader 確認**：T1+T2+T3+T4 (4 endpoint) + B1-B5 (5 backend) + Playwright TC1-TC5 (5 UI) = 14 條全綠。caqa read-only 複驗：fix shape 對、test shape 對、assertion 對應 fix code-site，**無理由質疑 14 綠 verdict 一致**。

**結論**：BLOCKER #1 **CLOSED**。fix 是 root-cause-correct，**不留 surface**。

### 2. 同類 sudo 第二輪掃描（補 read/browse 盲區 + search 第三輪 confirmation）

#### 2.1 `request.env[...].sudo().read()` — 全 `src/controllers/` 掃描結果

```bash
$ grep -rn "\.sudo()\.read\b" src/controllers/
(no output)
```

**0 hit**。**結論**：controllers/ 內**完全無**直接 `sudo().read()` pattern。

> 說明：本 codebase idiom 用 `.sudo().browse()` + attribute access（如 `record.field`）取代 `.sudo().read([fields])`。後者罕用是 Odoo recordset access pattern 的 idiomatic 演進；爽掃確認盲區無。

#### 2.2 `request.env[...].sudo().browse()` — 全 `src/controllers/` 掃描結果

**22 hit**，逐一分類：

| Line | Model | 後續 access check | 評估 |
|---|---|---|---|
| `ai_assistant.py:158` | `discuss.channel` (chat history) | `:161` `_check_channel_access` ✓ | 安全 |
| `ai_assistant.py:240` | `discuss.channel` (chat post) | `:243` `_check_channel_access` ✓ | 安全 |
| `ai_assistant.py:291` | `discuss.channel` (chat upload) | `:298` `_check_channel_access` ✓ | 安全 |
| `ai_assistant.py:378` | `discuss.channel` (SSE stream) | `:381` `_check_channel_access` ✓ | 安全 |
| `ai_assistant.py:864` | `ai.assistant` (default lookup) | platform-shared, by design | 安全 |
| `ai_assistant.py:1023` | `cloud_service` (dispatcher resolve) | `:1026` `_check_workspace_access` ✓ | 安全 |
| `ai_assistant.py:1029` | `workspace` (dispatcher resolve) | `:1032` `_check_workspace_access` ✓ | 安全 |
| `ai_assistant.py:1087` | `workspace` (task dispatcher) | `:1090` `_check_workspace_access` ✓ | 安全 |
| `ai_assistant.py:1116` | `project.task` (task detail) | `:1119` `_check_task_access` ✓ | 安全 |
| `ai_assistant.py:1265` | `project.project` (_update_project) | `:1268` `_check_project_access` ✓ | 安全 |
| `ai_assistant.py:1302` | `project.project` (_delete_project) | `:1305` `_check_project_access` ✓ | 安全 |
| `ai_assistant.py:1352` | `project.project` (_create_task) | `:1351` `_check_project_access` ✓ | 安全 |
| `ai_assistant.py:1374` | `project.task.type` (_create_task stage) | 同 _create_task context，project access 已驗 | 安全 |
| `billing.py:55` | `payment.token` (_get_owned_token) | `:58` `token.partner_id != request.env.user.partner_id` ✓ | 安全 |
| `billing.py:704` | `wallet.topup` (topup status) | `:705` `topup.partner_id.id != partner.id` ✓ | 安全 |
| `billing.py:809` | `sh.subscription` (sub detail) | `:811` `sub.sh_partner_id != partner` ✓ | 安全 |
| `billing.py:851` | `sh.subscription` (sub cancel) | `:857` `sub.sh_partner_id != partner` ✓ | 安全 |
| `billing.py:1044` | `account.move` (invoice detail) | `:1046` `invoice.partner_id != partner` ✓ | 安全 |
| `billing.py:1120` | `account.move` (invoice pay) | `:1126` `invoice.partner_id != partner` ✓ | 安全 |
| `ha_api.py:304` | `smart_home` (HA resolver) | `:310` `_check_workspace_access` ✓ | 安全 |
| `oauth2.py:604` | `res.users` (SUPERUSER_ID for service user) | service user lookup by ID literal, by design | 安全 |

**結論**：22 處 sudo().browse() **100% 有對應 access check**，全部走 server-authoritative scope（partner_id / workspace member / channel partner / project access）。idiomatic 安全模式，無新 finding。

#### 2.3 `request.env[...].sudo().search()` — 第三輪 confirmation

ai_assistant.py 內原 14 處 search（含 `_list_projects:1190` + `_list_tasks:1325`），本輪 fix 後**兩處已不在 list 中**（drop sudo 後不匹配 grep pattern）。

剩餘 13 處（ai_assistant.py 內 13 + 其他 controllers 18，共 31 處）逐一複驗前輪分類：

- **ai_assistant.py 內**：12 處安全（platform-shared / channel-verified / server-authoritative）+ 2 處維持 CAVEAT（_get_project_stages line 1150 / api_support_stats line 974）
- **billing.py**: 3 處（payment.provider 平台 / payment.token by partner.id / sh.subscription by partner.id）全安全
- **oauth2.py**: 8 處全 token/client/code by server-side identifier，server-internal resolver，全安全
- **ha_api.py**: 3 處（workspace_access by user.id × 2 + smart_home by verified workspace.id）全安全
- **dashboard.py**: 2 處（sh.subscription / wallet.topup by partner，需 verify partner scope — 與本 PR 同類 family 距離較遠，OOS）
- **agent_runtime_skill.py / agent_runtime_webhook.py**: 2 處 server-internal（by claim / by token），OOS

**結論**：第三輪 search 確認**無新發現**；剩餘 OOS controllers（dashboard / agent runtime）按 caqa 同 PR scope 紀律不擴展 audit，建議 reporter phase 評估是否值得獨立 sudo audit follow-up。

#### 2.4 第二輪掃描整體結論

| 掃描範圍 | hit 數 | 結果 |
|---------|-------|------|
| `sudo().read()` | 0 | 盲區無，confirmed |
| `sudo().browse()` | 22 | 100% 有 access check（前 4 channel + 8 project/workspace/cs/task + 6 billing partner + 1 ha + 1 oauth service user）|
| `sudo().search()` 第三輪 | 31 (ai_assistant 13 + 其他 18) | 12 + 18 = 30 safe；剩 2 維持 CAVEAT #2 #3 follow-up；**無新 finding** |

**整體**：**無新 high-severity / medium 發現**。BLOCKER 不增。

### 3. CAVEAT #2 / #3 / #4 / Caveat-arch1 維持狀態確認

| 編號 | 描述 | 本輪變化 |
|---|---|---|
| CAVEAT #2 | `_get_project_stages` (line 1139) 雙重洞（缺 access check + sudo fallback create attacker-supplied project_id）— pre-existing | 維持 follow-up，reporter 寫 `follow-ups.md` |
| CAVEAT #3 | `api_support_stats` (line 974) `accessible_projects` 漏 unbound leg — UX 不一致 | 維持 follow-up |
| CAVEAT #4 | `_check_project_access` follower-only vs ir.rule dual sub-leg 非對等鏡像 — D2=A 設計副效應 | 維持（docstring 加註建議 reporter phase 處理）|
| Caveat-arch1 | Frontend 三分支 dispatch 在 component `onSubmit` 而非 service 層 — 本 PR scope acceptable | 維持（future caller 再抽 helper）|
| Caveat（_create_project elif workspace legacy cs_id）| dead code path 接受 attacker `cloud_service_id` 無 cross-workspace check | 維持（建議 reporter phase 評估刪除或補 check）|

**所有 caveat unchanged**。本輪 BLOCKER fix 與 caveat 列無 coupling，**無新 caveat、無 caveat 升級**。

### 4. Invariants 對齊維持確認

| 對齊對象 | 本輪變化 |
|---|---|
| [[cloud-service-project-decouple]] §Invariants 7 條 | 維持全綠（本輪 fix 不動 schema / std rule / sanitizer / literal）|
| [[paas-workspace-member-acl-tightening]] | 維持對齊（_list_tasks 走 ir.rule dual-leg = 強化對 pattern §Controller filter 不夠 的踐行）|
| [[constraint-input-trust-expansion]] | 維持對齊（本輪 fix 無 attacker input 處理變動）|
| [[cross-workspace-task-data-leak]] B1 family | **本輪 fix 真正 close family 第 2 instance**（task-layer mirror 的 sudo bypass）；family 在 controller layer 還剩 CAVEAT #2 / #3 兩條 follow-up |
| [[odoo-addon-std-rule-symmetric-deactivation]] | 不適用 |
| [[odoo-std-field-no-override]] | 不適用 |

**結論**：所有 invariant 維持綠；本輪 fix 對 [[cross-workspace-task-data-leak]] B1 family 是**淨正面**（多 close 1 instance）。

### 5. Re-verdict 整體

**APPROVED WITH CAVEATS**。

| 維度 | 狀態 |
|---|---|
| BLOCKER #1 | ✅ closed（fix 落地 + T4 endpoint test + 14/14 全綠）|
| 新發現 | 0（read=0 hit / browse=22 全 check / search 第三輪 confirm 無新）|
| Caveats | 4 條維持（CAVEAT #2/#3/#4/Caveat-arch1 + 1 misc，均不阻擋）|
| Invariants | 7 條 + 3 pattern docs 維持全綠 |
| Pattern doc 草稿 | 草稿大綱已寫在 §ORM vs endpoint coverage gap 章節；reporter phase 落筆 |

### 6. 下一 phase 建議

**進 reviewer**，帶 4 caveats（CAVEAT #2 / #3 / #4 / Caveat-arch1）+ pattern doc 草稿大綱。

**Reviewer 階段 focus 點建議**：
1. **不必重做 caqa 廣度 audit**（本 caqa report + re-verdict 已覆蓋四面向 + 同 controller 三輪掃描）
2. **建議 reviewer 重點對照**：
   - PR description 是否清楚標示 D1=A + D2=A + D3=A + TC4 follow-on + BLOCKER A1 fix（7 個 改動點）
   - test file 對應契約（T1 mechanism lock + T2 outcome lock + T3 _list_projects + T4 _list_tasks + B1-B5 backend baseline）
   - [[cloud-service-project-decouple]] §Consequences 是否需 reporter phase 補 follow-up 連結
3. **reviewer 對等 audit 不擴展**：reviewer 不再展 dashboard / agent_runtime 等 OOS controllers 的 sudo audit（留 reporter phase 評估獨立 follow-up）

**Reporter 階段 must-do（無論進 reviewer / merge 後）**：
1. 開 P1 follow-up 鎖 CAVEAT #2 (`_get_project_stages` 雙重洞)、CAVEAT #3 (`api_support_stats` UX)
2. 寫進 `docs/tracking/followups/follow-ups.md` 含 design 大綱（兩 caveat 修法不對等：#2 需 design — 移 `_ensure_default_stages` 到 explicit write endpoint；#3 只是改 sudo→regular search）
3. 評估是否落筆 `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md` pattern doc（草稿大綱在本 caqa report §ORM vs endpoint coverage gap 章節）
4. 在 [[cloud-service-project-decouple]] §Consequences 加 follow-up 連結（本 PR 補的 controller layer 收尾 + 對 family 第 2 instance 的 closure）

### 7. Escalate-to-leader

**無 design escalation**。本輪 fix shape root-cause-correct，無需 leader 進一步拍板。

---

**Re-verdict 結束**。CAQA phase 對本 PR 工作完成，hand-off 到 reviewer。

---

## Mini Re-verdict (2026-05-28, F-DP-2 fix verification)

> 使用者拍板「F-DP-2 本 PR 帶」。tester 補 T5 red、implementation drop sudo + domain `[]`，15/15 全綠。本節為 read-only mini 複驗，4 件 focused：F-DP-2 解 + Task.sudo() unify 拍板 + 第三輪 sudo scan + pattern doc anti-pattern 兩變體 endorse。

### Mini Re-verdict TL;DR

| 項 | 值 |
|---|---|
| Verdict | **APPROVED WITH CAVEATS**（不變）|
| F-DP-2 (`api_support_stats:972-978` under-access bug) | ✅ **CLOSED** — `ai_assistant.py:978` drop sudo + empty domain，T5 endpoint test `:845-1032` |
| Task.sudo() unify (line 980) | **caqa recommends option (b) unify**，但 (a) 補 perf doc 也可接 — leader 拍板 |
| 第三輪 sudo() 全 ai_assistant.py 掃描 | ✅ **無新 high/medium finding**；剩 1 條 implementation-flagged 決策點（Task.sudo()）+ CAVEAT #2 維持 follow-up |
| Pattern doc anti-pattern 兩變體 | ✅ **強烈 endorse**（給 reporter phase 強授權落筆）|
| Next phase | **進 reporter**（reviewer phase 略過 — 此 PR 已 close 4 family instance + leader 已 alignment）|

### 1. F-DP-2 fix verification

**Fix landed**（`src/controllers/ai_assistant.py:972-984`）：

```python
# Walk ir.rule (no sudo, empty domain): project_rule_workspace_member
# dual-leg covers both bound (workspace member leg) and unbound
# (follower / creator legs) projects for the caller. A hand-rolled
# bound-only filter (sudo + cloud_service_id.workspace_id.access_ids
# walk) under-counts — unbound projects miss the chain entirely
# (inverse symptom of T3 / T4 sudo+empty-domain over-access).
accessible_projects = request.env['project.project'].search([])
task_domain = [('project_id', 'in', accessible_projects.ids)]
Task = request.env['project.task'].sudo()   # ← implementation-flagged for caqa decision
total = Task.search_count(task_domain)
done = Task.search_count(task_domain + [('stage_id.name', 'in', ('Done', 'Cancelled'))])
```

**對齊評估**：
- ✅ **Shape 對稱 T3/T4 fix idiom**（drop sudo + ir.rule walk）
- ✅ **註解 6 行明確點出 anti-pattern 兩變體**：
  - 變體 A (over-access, T3/T4)：sudo + empty-domain → cross-tenant leak
  - 變體 B (under-access, T5)：sudo + hand-rolled partial-leg domain → creator miss own data
  - 共同 root cause：controller 不信任 ir.rule，自己手搓 ACL
- ✅ **Dead `user` 變數移除**（diff verify：original `user = request.env.user` 已不在 fix 後 method body）

**Test landed**（`test_project_decouple_frontend_endpoint.py:845-1032`）：

`test_t5_api_support_stats_includes_unbound_owned_projects`：
- **setup**: Alice 建 1 unbound + 1 bound project，各 seed 1 task（2 total）
- **attack reproducer**: Alice stats → `assertGreaterEqual(alice_total, 2)` — 鎖 under-access symptom
- **bonus regression**: Bob bare stats → `assertEqual(bob_total, 0)` — 鎖過度修正（drop user.id leg 整條變 all-tasks）

**Assertion 訊息品質**：
- ✅ 明確指向 `ai_assistant.py:974-976` + 修法
- ✅ 對比 T3/T4 over-access vs T5 under-access — anti-pattern 兩變體說明清楚
- ✅ 計算 `missing count = 2 - total` 給 implementer 立刻定位漏哪幾條
- ✅ Bob regression 寫明「dropping user.id leg entirely 會 surface 為 Bob > 0」— false-green 風險明確守
- ✅ `>=` not `==` 解釋避免 demo-data baseline drift coupling

**15/15 全綠 leader 確認**：T1-T5 (5) + B1-B5 (5) + Playwright TC1-TC5 (5) = 15 條全綠。caqa read-only 複驗：fix shape 對、test shape 對、assertion 對應 fix code-site，**無理由質疑 15 綠 verdict 一致**。

**結論**：F-DP-2 **CLOSED**。fix 是 root-cause-correct，無 surface 殘留（除 Task.sudo() implementation-flagged 決策點）。

### 2. Task.sudo() unify 拍板（line 980）

**Implementation 留 Task.sudo() 的理由（自 flag）**：`accessible_projects.ids` 已 ir.rule filter → `task_domain` 是 trusted scope → Task.sudo() 純 perf 用途（省 ir.rule re-evaluation 一輪）。

**caqa 三選一評估**：

| Option | Tradeoff | caqa 評估 |
|--------|----------|-----------|
| **(a)** 保留 + 補 inline doc note 標 perf justification | ir.rule re-eval 對 search_count 成本可忽略；perf 收益 marginal；保留增加 reader cognitive load（未來 reviewer 需評估這條 sudo 合法性）| 合理但非 root-cause-correct |
| **(b) caqa 推薦** Unify (drop Task.sudo() → 走 ir.rule) | 與 _list_tasks fix 對齊；anti-pattern lint future-proof；本 PR 已建立「drop sudo + ir.rule single source of truth」pattern 在同一個 method 內留一條 sudo 違反 pattern doc 核心 invariant；1 行改動 + 0 new test | 對齊 anti-pattern closure 哲學 ✓ |
| **(c)** Caveat medium / reporter follow-up | 拖延決策；pattern doc 立論稍弱（同 method 內留決策不一致 instance）| 不推薦 |

**caqa 對等性驗證（爲什麼 (b) 安全）**：

drop Task.sudo() 後行為對等性，要驗 `accessible_projects.ids ⟹ task ir.rule pass`：

- `accessible_projects = project.search([])` 走 `project_rule_workspace_member` dual-leg → 結果集 = user 對該 project 可見的 set
- task `project_task_rule_workspace_member` dual-leg（`ir_rules.xml:108-117`）：
  - bound leg: `project_id.cloud_service_ids.workspace_id.access_ids.user_id == user.id`（同 project leg #1，via `project_id.` walk）
  - unbound leg: `project_id.message_partner_ids in [user.partner_id.id]` OR `project_id.create_uid == user.id`（同 project leg #2）
- 兩條 task leg **完全透過 `project_id` walk**，project 可見 ⟹ task ir.rule pass（無 task-specific overlay rule，本 codebase 確認）
- → drop Task.sudo() 結果集**對等**，純 ir.rule re-eval cost trade（可忽略）

**caqa recommendation**: **option (b) unify**

**Severity 規範對照**：本決策不是 leak / invariant 破壞（只是 perf vs consistency trade），不阻擋 PR。**option (a) 接受亦可**（caqa 不 re-block）；leader 對 PR scope 紀律拍板。

**對 BLOCKER #1 retrospective 對齊**：BLOCKER #1 阻擋 PR 是因為 cross-tenant leak（security high-severity）；Task.sudo() 不是 leak（accessible_projects 已 ir.rule filter），是 consistency caveat。**severity 不同，不對等阻擋**。

### 3. 第三輪 sudo() 全 ai_assistant.py 掃描

Post-F-DP-2 fix 跑 `grep -nE "request\.env\[.*\]\.sudo\(\)" src/controllers/ai_assistant.py`：

**34 hit**，逐一分類（含本 PR 已修 + 仍存）：

| 類型 | hit | 處置 |
|---|---|---|
| 平台共享 model（ai.config / ai.assistant / ir.config_parameter）| 4 | by design, OK |
| 已 access check（_check_channel_access × 4 / _check_workspace_access × 3 / _check_project_access × 4 / _check_task_access × 1 / partner_id × 0）| 12 | idiomatic 安全 |
| Server-internal resolver（mail.message by channel_id / project.task by channel_id × 4 / workspace_access by user.id / project_access by user.id / runtime helper / agent_template / mcp_tool / attachment in verified-channel context）| 13 | server-authoritative scope |
| 本 PR 已修（D2=A `_create_project` + create_task `_create_task` 後 access check）| 3 | OK |
| **CAVEAT #2 follow-up**（`_get_project_stages` × 2: line 1152 search + 1165 fallback create）| 2 | 維持 follow-up |
| **implementation-flagged 決策點**（line 980 Task.sudo() — §2 三選一）| 1 | 待 leader 拍板 |

**新 high-severity findings**: **0**

**新 medium findings**: **0**（line 980 Task.sudo() 是 design trade-off，不是 missed bug；severity 不到 medium）

**Spot-check 兩條前輪未細查**：

- **line 322 `ir.attachment.sudo().create()`**（chat upload, `api_ai_chat_upload`）：先 `_check_channel_access(channel)` (line 298)；create payload `res_model='discuss.channel' + res_id=channel_id`（已 verify channel）→ **安全 idiom**
- **line 757 `AgentTemplate.sudo()`**（`_resolve_agent_template` SSE runtime helper）：caller 已 resolve project + access check（project 來自已驗證 task）→ **server-internal resolver, 安全**

**第三輪掃描結論**：F-DP-2 fix 後 ai_assistant.py 內**無第 5 條同型 anti-pattern bug**；剩 1 條 implementation-flagged 決策點（line 980 Task.sudo()）+ CAVEAT #2（`_get_project_stages` pre-existing 雙重洞）follow-up。

### 4. Pattern doc anti-pattern 兩變體 endorse

**caqa 強烈 endorse**：給 reporter phase 強授權，落筆 `orm-vs-endpoint-acl-test-coverage.md` 時**必須包含**兩變體描述：

#### Anti-pattern Family（caqa 認定）

| 變體 | Symptom | Root cause | Instance in this PR |
|------|---------|------------|---------------------|
| **A: Over-access** | cross-tenant leak（attacker 看到 victim row）| sudo + empty/loose domain → ir.rule 被繞 | `_list_projects:1186` (T3) / `_list_tasks:1325` (T4) |
| **B: Under-access** | creator missing own data（合法 user 看不到自己的 row）| sudo + hand-rolled partial-leg domain → 漏寫 dual-leg 之一 | `api_support_stats:974-976` (T5) |

**共同症狀**：controller layer **不信任 ir.rule**，自己手搓 ACL 表達 → 必然 mismatch ir.rule 真實 invariant（不論 over 或 under）

**正解**：drop sudo + 改 empty/minimal domain（讓 server-side filter 從 endpoint params 補，不混入 ACL leg）→ ir.rule 是 **single source of truth**

**Endorse 理由**：
1. **本 PR 是 4 instance 抽 family 的 critical mass**（_list_projects + _list_tasks + _get_project_stages [pre-existing] + api_support_stats）— 1 個 PR 內 3 個 over-access + 1 個 under-access 都 close → pattern 立論 airtight
2. **變體 B (under-access) 反直覺**：security audit 一般 focus over-access；under-access 通常被當 UX bug 忽略 — pattern doc 寫入 anti-pattern family 後，未來 reviewer 看到 hand-rolled domain 立刻 trigger 反射檢查
3. **本 caqa report 已給草稿大綱**（§ORM vs endpoint coverage gap pattern doc 建議）；reporter 階段補兩變體後可直接落筆

**Pattern doc 強授權範圍**：
- 兩變體描述（含表格 + symptom + root cause + fix）
- 反例（4 instance：T3 / T4 / T5 / CAVEAT #2 _get_project_stages）
- 正例（_update_project / _delete_project / billing.py token / billing.py invoice / ha_api smart_home — 22 處 browse + access check idiom）
- Anti-pattern checklist（reviewer 對照）
- 對 [[paas-workspace-member-acl-tightening]] §Controller filter 不夠 的擴寫提示

**Reporter 拍板自由度**：endorse 強，但**寫法、章節編排、文長**由 reporter 判斷；caqa 不規定 outline 細節。

### 5. Mini Re-verdict 整體

**APPROVED WITH CAVEATS**（不變 — 本輪 fix 不引入新 caveat / blocker）。

| 維度 | 狀態 |
|---|---|
| F-DP-2 | ✅ closed（fix + T5 + 15/15 全綠）|
| 第三輪 sudo() scan | ✅ 無新 high/medium finding |
| Task.sudo() unify | ⚖️ caqa recommends (b) unify；leader 拍板（不阻擋）|
| CAVEAT #2 (`_get_project_stages`) | 維持 follow-up |
| CAVEAT #4 / Caveat-arch1 | 維持 follow-up |
| Pattern doc anti-pattern 兩變體 | ✅ 強 endorse |
| Invariants 1-7 + 3 pattern docs | ✅ 全綠 |

### 6. 下一 phase 建議

**進 reporter**（reviewer phase 略過建議）：

理由：
- 4 個 family instance close 3 + 1 design caveat（同 method）已對齊 leader expectation
- caqa 已 5-pass audit（plan alignment + 4 面向 + 三輪 sudo scan + invariants + pattern doc 草稿）
- reviewer phase 重點是「PR description 對應 commit + test contract」；leader 已 alignment user，可直接由 reporter 重生 commit/PR draft → leader 與 user 對焦最終 commit/push/PR

**Reporter 階段 must-do**：
1. 重生 commit message + PR description（含 8 個改動點：plan 5 + TC4 1 + BLOCKER A1 1 + F-DP-2 1，依 leader 對 Task.sudo() 的拍板決定是否 8+1）
2. 落筆 `docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md`（含 anti-pattern 兩變體 + 4 instance reference + checklist；草稿大綱在本 caqa report §pattern doc 建議章節）
3. 開 P1 follow-up 鎖 CAVEAT #2 (`_get_project_stages`)（含 design 大綱：需移 `_ensure_default_stages` 到 explicit write endpoint）
4. 在 [[cloud-service-project-decouple]] §Consequences 加 follow-up 連結（family 第 2、3、4 instance 已 close + 第 5 instance follow-up）
5. 評估 Task.sudo() 是否寫入 pattern doc anti-pattern 「**perf justification 不在 ACL gap 列表**」例外章節（避免未來 reader 誤把這條當 anti-pattern instance）

### 7. Escalate-to-leader

**1 條 decision point（line 980 Task.sudo()）**，不阻擋 PR：
- (a) keep + perf note：本 PR 收 commit + reporter 階段補 note
- (b) **caqa-recommended** unify：implementation 再 1 行 + commit
- (c) caveat + follow-up：本 PR 收 commit + reporter 開 follow-up

leader 對 PR scope 紀律 + 何時 ship 拍板。caqa 立場：(b) 對齊 root-cause-correct 哲學，但 (a)/(c) 都不 re-block。

---

**Mini Re-verdict 結束**。CAQA phase 對本 PR 全部工作完成，hand-off 到 reporter（pending leader 對 Task.sudo() 的 1 行拍板）。
