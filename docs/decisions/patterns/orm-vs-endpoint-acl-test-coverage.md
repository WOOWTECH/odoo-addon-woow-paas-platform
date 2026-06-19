---
name: orm-vs-endpoint-acl-test-coverage
description: Controller layer 不可 sudo() 繞 ir.rule；ORM-level test 全綠 ≠ endpoint-level secure — 必須對等補 HttpCase endpoint test
metadata:
  type: pattern
---

# Pattern: ORM-level test 綠 ≠ Endpoint-level secure — controller sudo bypass 必須對等 endpoint-test 守

> 適用：對任何 ir.rule 收緊的 model，新增 / 改動 controller endpoint 時，必須驗證 controller layer 不會用 `.sudo()` 繞 ir.rule 把 row return 給 caller；ORM 路徑的 backend test 全綠**不蘊含**該 model 的 endpoint 安全。

## When to use

任何**改動以下檔案**的 PR，必看本 pattern：

- `src/controllers/*.py` 內 list / fetch / stats / aggregation endpoint 對 ir.rule-protected model 走 `request.env[X].sudo().search() / .browse() / .read()` 後 return user
- 新增 / 改動 ir.rule 收緊某 model 可見性（如 `project_rule_workspace_member`、`project_task_rule_workspace_member`、`mcp_server_rule_workspace_user`）
- 寫 backend `TestCase` / `TransactionCase` + `with_user()` 驗 ir.rule 的 PR — 必同步補 HttpCase endpoint test

## Core invariant

**Controller layer 不可 `sudo()` 繞 ir.rule 後 return list 給 caller**。任何 `request.env[X].sudo().search(domain)` / `.browse(id).read()` / 類似 aggregate 操作，如果結果**直接 return 給 caller**（不論是 list / dict / stats），等於 controller layer 自行繞 ir.rule，把 row 漏給不該看的人。

**守線四層**：

| Layer | 機制 | 由誰守 | 例子 |
|-------|------|--------|------|
| **L1** | ir.rule + ACL CSV | backend `TestCase` + `with_user()` | `test_project_decouple.py` AC#1/#4/#5/#6 |
| **L2** | Controller endpoint 禁止 `.sudo()` 後 list-and-return | grep lint / code review | `grep -rn "request.env\[.*\]\.sudo()\.search" src/controllers/` |
| **L3** | HttpCase + cross-user `with_user()` 反射 endpoint 端 ACL outcome | tester 補 endpoint test | `test_project_decouple_frontend_endpoint.py` T3/T4 |
| **L4** | CAQA / reviewer 對 list endpoint **必 grep** sudo 模式 + 對應 cross-user endpoint test | CAQA + reviewer | 本 pattern 為 audit checklist |

**反推**：backend ORM 層所有 test 全綠（如 `test_project_decouple.py` B1-B5），**不保證**該 model 對應 endpoint cross-user 安全。每個 list endpoint 至少要對應 1 條 HttpCase cross-user negative。

## What it looks like

### 反例（unsafe — 三變體 family，本 repo 在 cycle 6 close 4 個 instance）

**變體 A (over-access) 反例**：

```python
# src/controllers/ai_assistant.py:1501-1505 (BEFORE cycle 6 TC4 follow-on)
def _list_projects(self, workspace, params: dict) -> dict[str, Any]:
    domain = []
    if workspace:
        domain.append(('cloud_service_id.workspace_id', '=', workspace.id))
    # ❌ sudo() 繞 project_rule_workspace_member ir.rule dual-leg
    projects = request.env['project.project'].sudo().search(domain)
    data = [{...} for proj in projects]
    return {'success': True, 'data': data, 'count': len(data)}

# src/controllers/ai_assistant.py:1658-1680 (BEFORE cycle 6 CAQA BLOCKER A1)
def _list_tasks(self, workspace, params: dict) -> dict[str, Any]:
    domain = []
    if workspace:
        domain.append(('project_id.cloud_service_id.workspace_id', '=', workspace.id))
    # ❌ 同類 sudo bypass — symmetric to _list_projects
    tasks = request.env['project.task'].sudo().search(domain)
    data = [self._serialize_task(t) for t in tasks]
    return {'success': True, 'data': data, 'count': len(data)}
```

**Attack vector**：caller 走 `/api/support/projects`（無 workspace_id URL）+ body 不帶 `project_id` → `domain = []` → `sudo().search([])` 回**全 db 所有 workspace 所有 user 所有 unbound project / task** → cross-tenant data leak（[[cross-workspace-task-data-leak]] B1 family 在 controller layer 的表現）。

**變體 B (under-access) 反例**：

```python
# src/controllers/ai_assistant.py:1266-1284 (BEFORE cycle 6 F-DP-2 fix)
def api_support_stats(self, **kwargs):
    user = request.env.user
    # ❌ sudo + hand-rolled bound-only filter — 漏 unbound leg (follower / creator)
    accessible_projects = request.env['project.project'].sudo().search([
        ('cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
    ])
    task_domain = [('project_id', 'in', accessible_projects.ids)]
    Task = request.env['project.task'].sudo()
    total = Task.search_count(task_domain)
    ...
```

**UX symptom**：Alice 建一個 unbound project 並加 task → 進 `/woow#/dashboard` 看 stats → 數字偏低（不算入 unbound 那條 task）。**不是 leak（cross-tenant 隔離仍對）**，是合法 user 看不到自家 data — under-access 反方向 bug。

**變體 C (same-method 半套狀態) 反例**：

```python
# src/controllers/ai_assistant.py:1266-1286 (hypothetical intermediate: F-DP-2 fix 後留 Task.sudo())
# accessible_projects drop sudo → ir.rule walk ✓ (B 變體 close)
accessible_projects = request.env['project.project'].search([])
task_domain = [('project_id', 'in', accessible_projects.ids)]
# ❌ Task.sudo() 殘留：accessible_projects.ids 已 ir.rule filter，
# 此處 sudo() 在 ACL 角度 safe，但破壞同 method 一致性
Task = request.env['project.task'].sudo()
total = Task.search_count(task_domain)
```

**Symptom**：boundary 不清楚。Future reviewer 對該 sudo() 必須逐條評估「為何這條合法？」cognitive load 增加；新人讀 code 看到 sudo() 不知該 audit 還是已驗過；pattern doc 立論「controller 不可 sudo() 繞 ir.rule」被同 method 內違反例稀釋。**Cycle 6 拍板 option (b) unify** — drop Task.sudo() 同時收口（caqa mini re-verdict §2 對等性驗證）。

### 正例（safe — cycle 6 fix shape，三條同源）

```python
# src/controllers/ai_assistant.py:1501-1505 (AFTER cycle 6 TC4 follow-on)
# Walk ir.rule (no sudo): project_rule_workspace_member dual-leg
# filters bound projects by workspace membership, unbound projects by
# their follower / creator sub-legs. The bare-URL list path (no
# workspace_id) must NOT cross-tenant-leak.
projects = request.env['project.project'].search(domain)

# src/controllers/ai_assistant.py:1670-1674 (AFTER cycle 6 CAQA BLOCKER A1)
# Walk ir.rule (no sudo): project_task_rule_workspace_member dual-leg
# filters bound tasks by their project's workspace membership, unbound
# tasks by the project_id follower / creator sub-legs. The bare-URL
# list path (no workspace_id, no project_id) must NOT cross-tenant-leak.
tasks = request.env['project.task'].search(domain)

# src/controllers/ai_assistant.py:1266-1286 (AFTER cycle 6 F-DP-2 + Task.sudo unify)
# Walk ir.rule (no sudo, empty domain): project_rule_workspace_member
# dual-leg covers both bound (workspace member leg) and unbound
# (follower / creator legs) projects for the caller. A hand-rolled
# bound-only filter (sudo + cloud_service_id.workspace_id.access_ids
# walk) under-counts — unbound projects miss the chain entirely
# (inverse symptom of T3 / T4 sudo+empty-domain over-access).
accessible_projects = request.env['project.project'].search([])
task_domain = [('project_id', 'in', accessible_projects.ids)]
# Unify with the project-side ir.rule walk above: task ir.rule
# (project_task_rule_workspace_member) traverses project_id and
# mirrors the dual-leg the projects search already passed, so no
# sudo is needed here either — closes the same-method anti-pattern.
Task = request.env['project.task']
total = Task.search_count(task_domain)
done = Task.search_count(task_domain + [('stage_id.name', 'in', ('Done', 'Cancelled'))])
```

**註解設計**：`:1272-1277` 解釋變體 B vs A 對稱關係（under-access vs over-access）；`:1280-1283` 解釋變體 C 為何需要 unify（perf-safe ≠ pattern-safe）。互引設計讓未來 reader 看到「為什麼這 method 有兩段 sudo 都被拔掉」一目了然。

### 對等補 endpoint test（HttpCase + cross-user `with_user`）

```python
# src/tests/test_project_decouple_frontend_endpoint.py (cycle 6 T3 / T4 idiom)
class TestEndpointListProjects(HttpCase):
    def test_t3_endpoint_list_projects_blocks_cross_user_leak(self):
        # Alice 建 2 unbound + 1 bound project
        ...
        # Bob bare-URL POST → 反射 endpoint 端 ACL
        self.authenticate(bob_login, bob_password)
        result = self.url_open('/api/support/projects', data={'action': 'list'})
        bob_returned_ids = {p['id'] for p in result.json()['data']}
        # Alice's projects MUST NOT appear in Bob's response
        self.assertFalse(alice_pids & bob_returned_ids, ...)
        # bonus regression: Alice owns her own → owner-survive
        self.assertFalse(missing_from_alice, ...)
```

**設計鎖**：
- **subset (⊆) 而非 strict equality**：用 `alice_pids - alice_returned_ids` empty assert，避免 baseline DB row 噪訊。
- **intersection check for leak**：`assertFalse(alice_pids & bob_returned_ids)` 直接鎖跨用戶 leak。
- **雙向同時鎖**：owner-survive（Alice 看得到自家）+ cross-user-deny（Bob 看不到 Alice）。

**Under-access (變體 B) test 對應 idiom**（cycle 6 T5）：

```python
# src/tests/test_project_decouple_frontend_endpoint.py:845-1032 (T5)
def test_t5_api_support_stats_includes_unbound_owned_projects(self):
    # Alice 建 1 unbound + 1 bound project，各 seed 1 task
    ...
    self.authenticate(alice_login, alice_password)
    result = self.url_open('/api/support/stats', data={})
    # 鎖 under-access symptom: Alice 應看到自家 unbound + bound tasks
    self.assertGreaterEqual(alice_total, 2, "...missing count = 2 - total...")
    # bonus regression — 反向 false-green:
    # 若 fix 過頭 drop user.id leg → Bob bare-stats 變 all-tasks
    self.authenticate(bob_login, bob_password)
    bob_result = self.url_open('/api/support/stats', data={})
    self.assertEqual(bob_total, 0, "...Bob > 0 surfaces over-correction...")
```

**設計鎖（B 變體專屬）**：
- **`>=` not `==`**：避免 demo-data baseline coupling，但夠鎖 own-data visibility。
- **Bob = 0 反向斷言**：守 false-green — 修變體 B 過頭把 user.id leg drop 整條，會 surface 為 Bob 也看到 all tasks（變成變體 A）。雙向同時鎖。
- **Missing count message**：`missing = expected - total` 給 implementer 立刻定位漏哪幾條。

## Anti-pattern 三變體（family taxonomy）

Cycle 6 抽出來的 anti-pattern 不只「sudo 漏 leg = leak」一種。controller layer 對 ir.rule 的**不信任**有 3 種症狀，**共同 root cause 都是「自己手搓 ACL/scope」**：

| 變體 | Symptom | 攻擊 / UX 表現 | Root cause 細節 | Instance in cycle 6 |
|------|---------|---------------|---------------|---------------------|
| **A: Over-access** | Cross-tenant leak | attacker 看到 victim row | `sudo()` + empty / loose domain → ir.rule 被繞，全 db row 都 match | `_list_projects:1505` (T3) / `_list_tasks:1674` (T4) |
| **B: Under-access** | Creator 看不到自家 row | 合法 user 的 stats / list 偏低 | `sudo()` + hand-rolled **partial-leg** domain → 只 cover dual-leg 其中一條（如只 bound leg 不 unbound leg）| `api_support_stats:1276-1278` (T5) — sudo + `cloud_service_id.workspace_id.access_ids.user_id` bound-only filter |
| **C: Same-method 半套狀態** | Inconsistent boundary 即使 ACL-safe | 同一 method 內 drop 一條 sudo 但留另一條（input-side ✓ output-side ✗，或反之）| 一部份信任 ir.rule、一部份不信任 — boundary 模糊，**未來 reviewer 需逐條評估每個 sudo() 合法性**，pattern 立論被同 method 內違反例稀釋 | `api_support_stats:1284` (T5 fix 後 residual) — `accessible_projects` drop sudo（B closure）但 `Task.sudo()` 殘留 |

### 共同 root cause + 正解

**Root cause**：controller layer **不信任 ir.rule，自己手搓 ACL/scope 表達** → 必然 mismatch ir.rule 真實 invariant（變體 A 寫太鬆、變體 B 寫太緊、變體 C 寫一半）

**正解 — ir.rule single source of truth + 整 method 一致性**：

1. **drop sudo()**（讓 controller code 走 ir.rule 自然 filter）
2. **改 empty 或 minimal domain**（server-side filter 從 endpoint params 補，不混入 ACL leg — 不該由 controller code 重新表達已在 ir.rule 內的可見性 invariant）
3. **整 method 一致**（input-side `.search()` + output-side `Task.search_count()` 都不 sudo，避免變體 C 半套狀態 — 即使留下的 sudo() 在 ACL 角度 safe（如 `accessible_projects.ids` 已 ir.rule filter），仍應 unify）

### Why 變體 C 不只是 perf trade-off — pattern 立論 integrity 問題

**Implementation 的 perf 直覺**：F-DP-2 fix 後 `accessible_projects.ids` 已 ir.rule filter，後續 `Task.sudo().search_count(task_domain)` 走 trusted scope，sudo() 純 perf 用途（省 ir.rule re-evaluation 一輪）。

**為何仍應 unify**：

1. **同 method 內留半套狀態破壞 pattern doc 立論**：本 pattern 核心 invariant 是「controller layer 不可 `.sudo()` 繞 ir.rule」。同 method 內留一條 sudo()（即使 ACL-safe）→ 未來 reviewer 對該 sudo() 必須逐條評估「為何這條合法？」cognitive load 增加；新人讀 code 看到 sudo() 不知道是該 audit 還是已驗過 → pattern lint future-proof 失效。
2. **Perf 收益 marginal**：`search_count` ir.rule re-eval 對已 filter 的 small ID list 成本可忽略（O(n) ir.rule check 對 < 50 project 是 microseconds 級）；perf gain 不抵 readability cost。
3. **正確的 perf justification 是 enclosing transaction context，不是 sudo()**：若真有 perf hot path，正解是 `with_context(bin_size=True)` / `prefetch_fields` / batch fetch — 不是 sudo()。Sudo() 是 security primitive，不是 perf primitive，誤用會讓 future reader 把 sudo() 當 perf cache idiom。
4. **CAQA 對等性已驗**（mini re-verdict §2）：drop Task.sudo() 後結果集對等（project 可見 ⟹ task ir.rule pass，因 task `project_task_rule_workspace_member` 完全透過 `project_id` walk）。**Perf trade 是真實的（marginal），correctness 是 lossless 的**。

**Cycle 6 拍板**：option (b) unify（implementation 落地 `:1280-1286` comment unify 對等註解）。

## Anti-pattern checklist（reviewer / CAQA 對 list endpoint 必對照）

| 反 pattern | 變體 | 為何不安全 | 修法 |
|-----------|------|-----------|------|
| `request.env[Model].sudo().search(domain).map(to_dict)` 直接 return user | A | 繞 ir.rule list-disclosure | 去 `.sudo()` 走 ir.rule |
| `domain = []` + user-supplied filter（不帶 server-authoritative leg） | A | 空 domain 走 sudo → 全 db leak | 強制至少一條 server-side anchor，或去 sudo 靠 ir.rule |
| `accessible_X = sudo().search([bound-leg])` 漏 unbound leg | B | UX 不一致（own data invisible）+ 與 ir.rule dual-leg 後 visibility 對不上 | drop sudo + 改 empty/minimal domain 走 ir.rule dual-leg |
| ORM-level test 全綠 = 認定 secure | A+B | 缺 endpoint layer 反射 — controller 自行繞 ir.rule 在 ORM test 看不見 | 對等補 HttpCase cross-user negative + own-data assertion |
| `_ensure_default_X` 在 list endpoint 內 fallback `sudo().create()` 對 attacker-supplied id 寫入 | A | list endpoint 副作用寫入 → write surface | 移到 explicit write endpoint，加 `_check_X_access` guard |
| 同 method 內 input-side drop sudo 但 output-side 留 sudo（perf 直覺）| C | 同 method 一致性破壞 pattern lint future-proof / sudo 不是 perf primitive | unify drop sudo；perf 真有需求走 prefetch / batch / context flags |

## Reviewer / CAQA grep checklist

每次 PR 改 `src/controllers/`，列入 audit：

```bash
# 1. List endpoints 用 sudo().search 後 return user？
grep -rn "request\.env\[.*\]\.sudo()\.search" src/controllers/

# 2. browse(id) 後直接 return（無 _check_X_access）？
grep -rn "request\.env\[.*\]\.sudo()\.browse" src/controllers/
# 然後 verify 每個 hit 之後幾行內有 _check_X_access / partner_id 比對 / channel access 驗證

# 3. sudo().create() 對 attacker-supplied id？
grep -rn "request\.env\[.*\]\.sudo()\.create\|\.sudo()\.create" src/controllers/
# 然後 verify vals 內無 attacker-controllable id / 有 grantor authority check
```

**本 repo cycle 6 baseline 結果**（CAQA round 2 + mini re-verdict 雙輪 audit 完成）：
- `sudo().search` 第三輪掃描 34 處：32 處安全（platform-shared / channel-verified / server-authoritative）+ 1 處 pending（`_get_project_stages` line 1465 [F-DP-1] pre-existing）+ cycle 6 fix 已 close `_list_projects` / `_list_tasks` / `api_support_stats` 3 處
- `sudo().read()` 0 處（codebase idiom 用 `.sudo().browse() + attribute access`）
- `sudo().browse()` 22 處：100% 配套 `_check_X_access` / server-authoritative scope（partner_id / workspace member / channel partner / project access）

## Critical mass instance（5 instance 本 repo case，cycle 6 driver）

| # | 位置 | 變體 | 狀態 | Reference |
|---|------|------|------|-----------|
| 1 | `_list_projects` (`ai_assistant.py:1501-1505`) | A (over-access) | ✅ closed cycle 6 (TC4 follow-on) | `test_project_decouple_frontend_endpoint.py` T3 |
| 2 | `_list_tasks` (`ai_assistant.py:1670-1674`) | A (over-access) | ✅ closed cycle 6 (CAQA BLOCKER A1) | `test_project_decouple_frontend_endpoint.py` T4 |
| 3 | `api_support_stats` `accessible_projects` (`ai_assistant.py:1272-1278`) | B (under-access) | ✅ closed cycle 6 (F-DP-2) | `test_project_decouple_frontend_endpoint.py` T5 |
| 4 | `api_support_stats` `Task.sudo()` (`ai_assistant.py:1280-1286`) | C (same-method 半套) | ✅ closed cycle 6 (Task.sudo unify, option b) | T5 same — caqa mini re-verdict §2 對等性驗證 |
| 5 | `_get_project_stages` (`ai_assistant.py:1375, 1454-1471`) | A + write surface | ⏸ pending (F-DP-1) | [[follow-ups]] F-DP-1 — 需 design |

**Cycle 6 closure rate**：5 instance 4 closed（80%）+ 1 follow-up（pre-existing 雙重洞，需 design）。同 method 內 `api_support_stats` 變體 B + C 同時 close → pattern doc 立論在「同 method 一致性」維度也 airtight。

## Why this pattern

**Backend `TestCase` + `with_user()` 走 Odoo ORM `search()` / `browse()`**：自動套 ir.rule，所以 ORM 層 test 全綠 = ir.rule 設定對。

**Controller endpoint 走 `request.env[X].sudo()`**：明確繞過 ir.rule。Controller test 若只走 backend test idiom（`record = self.env['X'].with_user(bob).search(...)`），完全**不會 exercise** controller 內 `.sudo()` 的 bypass 行為。

→ **endpoint test 是必要的、不是冗餘**。Backend test 守 ir.rule layer，endpoint test 守 controller layer。兩者 cover 不同 attack surface。

## Why not 別的 pattern

**為何不靠 [[paas-workspace-member-acl-tightening]] §Controller filter 不夠 章節**：那段提到 ORM 路徑會繞 controller filter，但**沒專章講 controller layer 自己也 `.sudo()` 繞 ir.rule**。前者是「controller filter 防 ORM bypass」（攻擊者用 ORM 繞 controller），後者是「ORM ir.rule 防 controller bypass」（controller 自己 sudo 繞 ir.rule）— **方向相反**。本 pattern 補 missing 半邊。

**為何不靠 [[two-layer-acl-lockstep]]**：那個 pattern 對 Layer 1 (ir.rule + Python `@api.constrains`) + Layer 2 (controller helper) 同步收緊；但**不規範** controller 內 sudo 繞 ir.rule。本 pattern 是該 pattern 的 endpoint-test layer 補強。

**為何不靠 [[cross-workspace-task-data-leak]] pitfall**：B1 incident 的 root cause 是「CSV ACL 開放 + 漏 ir.rule scope-restrict」 — 是 **ir.rule layer 缺**；本 pattern 處理的是 **ir.rule 已 ready，但 controller `.sudo()` 繞** — 同 root cause family 的不同層次表現。Pattern 是 prescription，pitfall 是 symptom 記錄。

## See also

- [[paas-workspace-member-acl-tightening]] §Bound vs unbound 雙腿 + §Controller filter 不夠
- [[two-layer-acl-lockstep]] §rule + carve-out lockstep
- [[constraint-input-trust-expansion]] — controller subscribe / m2m authority 的對等
- [[cross-workspace-task-data-leak]] — B1 incident (root cause family)
- [[orm-test-green-endpoint-leak-blindness]] — cycle 6 pitfall：本 pattern 的 symptom-side 記錄
- [[cloud-service-project-decouple]] §Invariants #5 — `base.group_user` Project/Task ACL + ir.rule 對齊
- `docs/tracking/caqa-report-2026-05-28-decuple-project-frontend.md` §同源 sudo bypass scan + §Mini Re-verdict §2 Task.sudo() unify 對等性驗證 — 33+34 處 sudo() audit 範本 + 變體 C unify (option b) caqa-recommended reasoning
- `docs/tracking/reviewer-report-2026-05-28-decuple-project-frontend.md` §5 同源 code path 檢查 — 對等 audit 不擴 scope 的 reviewer 紀律對齊
