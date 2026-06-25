---
name: caqa-report-2026-05-22-agent-runtime-pod-phase-a
phase: caqa
team: agent-runtime-pod
caqa: agent-runtime-pod-caqa
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
test_report_ref: docs/tracking/test-reports/test-coverage-agent-runtime-phase-a-2026-05-22.md
created: 2026-05-22
status: blocked
related:
  - docs/decisions/refactor/cloud-service-project-decouple.md
  - docs/decisions/refactor/mcp-project-decouple.md
  - docs/decisions/refactor/mcp-url-redesign.md
  - docs/decisions/patterns/mcp-acl-6-rule-split.md
  - docs/decisions/patterns/two-layer-acl-lockstep.md
  - docs/decisions/patterns/unbound-project-acl-dual-leg.md
  - docs/decisions/patterns/sidecar-backed-flag-lifecycle.md
  - docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md
  - docs/decisions/patterns/odoo-std-field-no-override.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/tracking/pitfalls/plan-vs-odoo-schema-mismatch.md
---

# CAQA Report — Agent Runtime Pod Phase A（schema migration + 4 new models）

## Verdict

**BLOCKED** — 1 BLOCKER（B-1：4 個新 model 對 `base.group_user` 全 instance RWCU 開放但 `ir.rule` 完全沒新增；違反 plan §Phase A task #13 + design §3.2/§4.3 明文要求 + [[cross-workspace-task-data-leak]] B1 pitfall 教訓）必須 escalate leader 拍板：（a）implementation 補 16 rule + R 段 ACL behaviour test 補 explicit deny；或（b）leader 明示「Phase A defer 到 Phase B/C，Phase A 必補 ACL CSV row=0,0,0,0 暫關 base.group_user RWCU + Phase B 開放配套」。

四面向：
- **Quality**：54 test 全 PASS + 既有 48/48 regression 全綠 — 行為對齊 plan §AC 49 條 + design §1.3 / §7.0 / §4.1 / §2.12 / §3.2 / §4.3 schema spec；但 plan §Phase A scope **#13（16 rule）+ #14-18（5 view files）兩段任務 implementation 完全沒做**（test 沒 cover 所以全綠不代表 plan AC 全達成）。
- **Security**：B-1 BLOCKER；M3（namespace_secret hardcode fallback）caveat 與 B-1 串聯成 namespace collision attack；M4（M:N broad-filter fallback）caveat；無破壞 D4 / Inv #11 / 6-rule split / MAJOR 1 防線。
- **Performance**：cloud_service `unlink()` override 改 per-record loop（N+1 query for batch unlink，但 batch unlink 罕見）；migration step 9 對既有 project 全表掃描 + per-row INSERT — 預估規模可忽略。
- **Architecture**：B 案 hybrid pattern 對齊 design §1.3.3 / §1.3.4 D4 守線確認 ✓；M:N walk path 對 mcp_server / mcp_tool 改完整 ✓；但 **project_rule_workspace_member / project_task_rule_workspace_member 沒同步改 M:N walk** — cross-workspace M:N project 對 W2 member 看不到 project / task 本身（M1 caveat，escalate planner 拍板）。

---

## 1. Invariants verify table

### refactor branch 已 final 的 11 條 invariants 守線（PR #47 + PR #54）

| Inv # | 主旨 | 本 epic 動作 | Verify | Evidence |
|-------|------|----------|--------|----------|
| #1 | `mcp_server.project_id` 必維持 `ondelete='cascade'` | **不動** | ✓ | `mcp_server.py` `project_id` 欄位定義無改動 |
| #2 | D4：`scope='user' ⇔ project_id IS NOT NULL` | **不動** | ✓ | design §1.3.4 line 612-622 守線確認；`_check_scope_project_consistency` 方法 body 不動 |
| #3 | ~~D5~~（已拔，cycle 4） | **不動** | ✓ | refactor branch 已拔，本 epic 不重新引入 |
| #4 | `_do_cleanup` 順序 + sidecar_backed filter | **filter 延伸 + 順序不變** | ✓ | `cloud_service.py:1086-1100` 三步順序不變；filter 改走 `_collect_sidecar_user_mcps` helper（加 URL prefix narrowing），sidecar_backed flag 條件保留 |
| #5 | 6-rule ACL split 對等存在 | **walk path 內 M:N migration** | ✓ | `ir_rules.xml:134-237` 6 條 rule 全保留；workspace_user × 2 dual-leg 內 walk path `cloud_service_id → cloud_service_ids` + `cloud_service_id=False → cloud_service_ids=False` |
| #6 | `mcp_server_rule_system_readonly` perm_create/write/unlink=False | **不動** | ✓ | `ir_rules.xml:134-143, 177-186` 全保留 |
| #7 | `mcp_server.url` snapshot + 演算法歸 service | **不動** | ✓ | design §1.3.3 line 595「不動」；M:N 下 helper 仍從 `helm_release_name + helm_namespace` 算 |
| #8 | System-scope MCP create 限 sudo / admin / demo data | **不動** | ✓ | system_readonly perm_create=False + workspace_user 外層 AND `scope='user'` 共守 |
| #9 | sidecar-backed user-scope MCP 雙 path cleanup（`_do_cleanup` + `unlink()` filter 對等） | **filter 延伸 + URL prefix narrowing** | ✓ | `cloud_service.py:1086-1100` (`_do_cleanup`) + `:1147-1160` (`unlink()`) 兩條 path 走相同 `_collect_sidecar_user_mcps` helper — 對等改寫；filter shape `lambda s: s.scope == 'user' and s.sidecar_backed` 保留並加 URL prefix；`unlink()` override 改 per-record loop（M:N per-service URL prefix narrowing 正確語意） |
| #10 | UI shortcut Add Sidecar MCP create path 必帶 sidecar_backed=True | **不動** | ✓ | refactor branch endpoint `_create_project_mcp_server` + frontend `submitForm` payload set sidecar_backed 路徑本 epic 未動 |
| #11 | `workspace_user` + `mcp_tool_workspace_user` 必 dual-leg（外層 AND `scope='user'`） | **walk path cardinality 改 M2M + dual-leg shape 保留** | ✓ | `ir_rules.xml:156-172` (`mcp_server_rule_workspace_user`)：外層 AND `('scope','=','user')` 不動；leg 1 walk path `project_id.cloud_service_ids.workspace_id.access_ids.user_id`；leg 2 carve-out `('project_id.cloud_service_ids', '=', False) AND ('project_id.message_partner_ids', 'in', [user.partner_id.id])`；sub-model `mcp_tool_rule_workspace_user` 對等改 `server_id.` 前綴（`:192-208`）|

**Inv #11 字面禁止動作 verify**（[[mcp-url-redesign]] §Invariants）：

- ✓ 沒把 leg 2 拔掉
- ✓ 沒把 leg 2 改 always-true
- ✓ 沒把 leg 2 改 `create_uid` only
- ✓ 沒把外層 AND 改 OR
- ✓ mcp_server 與 mcp_tool 對等改（不漏改 sub-model）
- ✓ 4 條不動 rule（`system_readonly` × 2 + `admin_full` × 2）沒被合併回 dual-leg

### Phase A 新增 invariants（design §1.3.2 / §4.3 / Q1 sign-off）

| Inv # | 主旨 | Verify | Evidence |
|-------|------|--------|----------|
| **Phase A #1** | `project.cloud_service_id` 從 M2O 改 stored compute over `cloud_service_ids[:1]` + dual-write 對齊 legacy callers | ✓ | `project_project.py:18-27`（field 定義）+ `:74-77`（compute）+ `:79-89`（inverse 寫法）；R.A.4/A.5 + M.5 鎖 |
| **Phase A #2** | `project.workspace_ids` computed Many2many aggregate `cloud_service_ids.workspace_id` + store=True | ✓ | `project_project.py:30-35`（field 定義）+ `:91-93`（compute）；R.A.2 + M.4 鎖 |
| **Phase A #3** | `cloud_service.project_ids` 從 One2many 改 Many2many reverse（同 relation table） | ✓ | `cloud_service.py:183-188`；R.C.1 鎖 |
| **Phase A #4** | `task.primary_cloud_service_id` 從 related 改 independent M2O + computed default + drift handler | ✓ | `project_task.py:41-49`（field 定義 + domain）+ `:74-89`（compute + drift handler）；T.5 五 method 鎖 |
| **Phase A #5** | `_do_cleanup` / `unlink()` 兩條 path 在 M:N 下加 URL-prefix narrowing 不誤殺其他 service 的 sidecar-backed MCP | ✓ | `cloud_service.py:1086-1100, 1147-1194`；C.2 / C.3 / C.5 鎖 |
| **Phase A #6** | `project_access` first-class ACL row exists（schema + bootstrap）| **functional ✓，security ✗** | schema ✓（`project_access.py:18-94`）+ bootstrap ✓（`project_project.py:104-130` + migration step 9）；**但 ACL ir.rule 完全沒加** → 任何 base.group_user 可 ORM create owner row 攻擊面（見 B-1）|
| **Phase A #7** | Q1 sign-off 2026-05-22 cross-workspace M:N walk OR semantics（兩 workspace member 都可讀） | **partial** | mcp_server / mcp_tool ✓（R.7 explicit pass）；**project_rule_workspace_member / project_task_rule_workspace_member 沒對等改** — cross-workspace M:N project 對 W2 member 看不到 project / task 本身（見 M-1 caveat） |

**Invariant verify 結論**：11 條 refactor branch invariants **全 ✓**；7 條 Phase A 新加 invariants **5 ✓ / 1 部分 ✓（Phase A #7）/ 1 ✗（Phase A #6 security 不滿足）**。

---

## 2. 四面向分析

### Quality

**Plan AC 對齊**：plan §Phase A scope task 19 條 + design §7 落地細節 + test report 49 AC 對應 implementation：

| Task | Scope | 落地 | Note |
|------|-------|------|------|
| #1 project_project schema | M:N + workspace_ids + external_mcp_allowed + agent_template_id + use_agent_runtime + project_access bootstrap | ✓ | `src/models/project_project.py:1-139` 全部落地 |
| #2 project_task schema | primary 改 independent M2O + extra domain 拆 same-workspace | ✓ | `src/models/project_task.py:41-89` 全部落地（hybrid spec Q2(c) 五件套全鎖） |
| #3 cloud_service.py | project_ids M2M reverse + URL-prefix cleanup filter（雙 path）| ✓ | `src/models/cloud_service.py:183-188, 1086-1194` |
| #4 ir_rules.xml mcp_server/mcp_tool walk path | walk M2O → M2M | ✓ | `src/security/ir_rules.xml:156-208` |
| #5 pre-migrate.py | CREATE TABLE | ✓ | `src/migrations/18.0.1.0.26/pre-migrate.py` |
| #6 post-migrate.py | Step 2-4 + 8-10 | ✓ | `src/migrations/18.0.1.0.26/post-migrate.py:1-237`（step 6/7 design §7.0.2 在 Odoo --init 自動處理） |
| #7-10 新 model 4 個 | agent_template / agent_session / skill / project_access | ✓ | 4 個新檔 schema + constraint + bootstrap 全到位 |
| #11 models/__init__.py | imports | ✓ | `src/models/__init__.py:13, 29-31` |
| #12 ir.model.access.csv | 4 model × user/admin 8 row | ✓ | `src/security/ir.model.access.csv:28-35` |
| **#13 ir_rules.xml 4 rule × 4 new model = 16 rule** | scope-restrict per project_access role | **✗** | **完全沒做** — `src/security/ir_rules.xml` 對 4 新 model 沒任何 record rule（grep result: 0 hit） |
| **#14 views/agent_template_views.xml** | backend admin view | **✗** | 檔案不存在 |
| **#15 views/agent_session_views.xml** | backend admin view | **✗** | 檔案不存在 |
| **#16 views/skill_views.xml** | backend admin view | **✗** | 檔案不存在 |
| **#17 views/project_access_views.xml** | backend admin view | **✗** | 檔案不存在 |
| **#18 views/project_external_mcp_setting.xml** | admin 開關 + SSRF risk 警示 | **✗** | 檔案不存在 |
| #19 manifest version bump | 25 → 26 + data files | partial | version bump ✓；**data files 未加（因 #14-18 沒做）** |

**Result**：plan §Phase A scope **task #13 / #14-18 完全沒做**（共 6 task 漏）；其他 task 對齊 ✓。test 沒 cover 這 6 task → 54/54 PASS 不等於 plan AC 全達成。

**Test coverage gap 從 cell-map implicit-through 視角驗證**：
- R.8（`test_user_group_access_row_present_for_each_new_model`）只測 CSV row 存在 + `perm_read=True`；**沒測 ir.rule 對 4 個新 model 的 scope-restrict** — test 邊界明示「ir.rule 行為矩陣留 Phase B/C」（test report §10 line 540）— **但 test 邊界 ≠ Phase A scope 邊界**（plan #13 明文要求加 16 rule）
- N.11（`test_project_access_last_owner_cannot_be_unlinked`）只測 Python constraint；**沒測 base.group_user 是否能對非自己 project create role='owner'** — 這是 privilege escalation 的入口，但沒 explicit test 鎖
- 其他 R / N 段 test 沒測 base.group_user × 4 new model × CRUD 矩陣 — implicit 假設 ACL 在 Phase B/C 補 → 與 plan #13 衝突

**Migration script idempotent / replayable check**：

| Step | Idempotent | Notes |
|------|-----------|-------|
| Pre Step 1 | ✓ | `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS` |
| Post Step 2 backfill | ✓ | `ON CONFLICT DO NOTHING` + 內含 `IF NOT EXISTS` defensive ensure |
| Post Step 3 drop UNIQUE | ✓ | `DROP CONSTRAINT IF EXISTS` + `LIKE '%cloud_service%'` 動態抓 constraint name |
| Post Step 4 keep cloud_service_id column | ✓ | noop |
| Post Step 8 external_mcp_allowed default FALSE | ✓ | `WHERE external_mcp_allowed IS NULL` guard |
| Post Step 9 project_access owner bootstrap | ✓ | `NOT EXISTS` outer + `ON CONFLICT (project_id, partner_id) DO NOTHING` inner |
| Post Step 10 default ai.assistant fallback kept | ✓ | log-only, no SQL |

**結論**：migration idempotent / replayable ✓；範圍小（dev / stg 既有 row count 預計 < 100，prod 無 data）。

**/simplify 評估**：本 epic implementation 流程沒走 /simplify pass（不在 brief constraint 內），但 code 整體 readable — 命名 `_collect_sidecar_user_mcps` / `_sidecar_url_prefix` / `_compute_primary_cloud_service_id` self-documenting；comment 完整解釋 design intent（特別是 `_compute_primary_cloud_service_id` docstring 完整解釋 4 種 case）。

### Security

**B-1（BLOCKER）：4 個新 model 對 `base.group_user` 給 CSV `1,1,1,1` 但 `ir.rule` 完全沒新增**：

- **位置**：
  - `src/security/ir.model.access.csv:28-35`（8 個 row 給 base.group_user RWCU=1,1,1,1）
  - `src/security/ir_rules.xml`（grep `model_woow_paas_platform_(agent_template|agent_session|skill|project_access)` → **0 hit**）
- **違反**：
  - plan §Phase A task #13 明文「加 4 rule × 4 new models = 16 rules（仿 mcp-acl-6-rule-split pattern）」
  - design §3.2 line 1523-1530 明文 skill 4 條 rule（`skill_rule_project_admin` / `skill_rule_project_user` / `skill_rule_project_guest` / `skill_rule_admin_full`）
  - design §4.3 line 2099-2106 明文 project_access 4 條 rule（`project_access_rule_member_read` / `project_access_rule_owner_admin_manage` / `project_access_rule_self` / `project_access_rule_admin_full`）
  - plan §To phase-caqa hand-off line 541「ir.rule (mcp_server / mcp_tool / agent_template / skill / project_access) ↔ controller `_check_project_access`」兩層 lock-step 要求
  - [[cross-workspace-task-data-leak]] B1 pitfall 教訓：「對 base.group_user 第一次開放 std model ACL = 觸發所有 std rule」— Phase A 是「對 base.group_user 第一次開放 4 個新 model ACL」**同源 pattern 重蹈**（雖然是自家 model，沒 std rule 的 OR-bypass，但無 ir.rule scope-restrict = 全 instance 直接放行）
  - [[two-layer-acl-lockstep]] pattern：本 epic project_access ACL 必需要 ir.rule 與 controller carve-out 兩層 lock-step；Phase A 兩層**都沒做**（controller carve-out 也沒寫，但 controller scope 留 Phase D-E）
- **具體攻擊面**（Phase A 雖然 `use_agent_runtime=False` default 但 schema 一旦 land production，攻擊面立即 active）：

  1. **`project_access` privilege escalation**（最嚴重）：
     ```python
     # attacker (base.group_user) 對 victim project (id=N) 走 ORM:
     env['woow_paas_platform.project_access'].create({
         'project_id': N,
         'partner_id': attacker.partner_id.id,
         'role': 'owner',  # ← 直接加自己為 owner
     })
     ```
     UNIQUE(project_id, partner_id) 擋 attacker 已在 access list 的 case；但 victim project 沒 attacker access 時 create 成功 → attacker 立即可走 agent invoke / template binding / quota override / 整個 Phase B-G agent runtime 攻擊面。

  2. **`agent_template` 跨 project read/write**：
     - 讀任意 template 的 `system_prompt`（敏感：含 admin-curated prompt 內 secrets / 業務邏輯）
     - 改別 project 的 template `state='archived'` → 對 victim 的 agent runtime 觸發 fallback 行為
     - 改 `mcp_server_ids` binding → 引導 victim agent 連到 attacker controlled MCP（`_check_mcp_subset` constraint 守 subset of project.mcp_server_ids ∪ scope=system，但 attacker 可創 system MCP 透過 6-rule split ACL? 不行，system create direction 鎖；但若 attacker 在自己 project 有合法 user-scope MCP + 改 attacker template `project_ids` 加 victim project → `_check_mcp_subset` 可能 pass）

  3. **`skill` 跨 project bundle_sha256 forgery**：
     - 改 victim project 的 skill `bundle_sha256` 為 attacker payload signature
     - 下次 agent runtime lazy fetch bundle 時用 forged signature verify → 載入 attacker payload
     - **特別嚴重**：skill bundle 是 subprocess 執行的 code，等於 RCE on agent pod

  4. **`agent_session` 跨 project K8s lifecycle abuse**：
     - 改 quota_override_cpu / memory_gi / storage_gi → cluster resource saturation（DoS）
     - 改 project_namespace_hash → 觸發 K8s namespace collision（與 M-3 namespace_secret hardcode 串聯）
     - 改 state='absent' → trigger admin alert / fake state metric

- **與 known pitfalls 對照**：
  - [[cross-workspace-task-data-leak]] B1 incident 同源 pattern：CSV row 開放 + 漏 ir.rule scope-restrict = 全 instance leak。差別：B1 是 std model 撞 always-true leg；本案是自家 model 直接無 rule = 默認 allow-all。但 root cause 完全相同：「對 base.group_user 第一次開放某 model 的 ACL CSV 時，必須對等加 ir.rule scope-restrict」。
  - [[mcp-acl-6-rule-split]] 6-rule split pattern 在 mcp_server / mcp_tool 落地是「同源 design rationale」— design §3.2 / §4.3 也明文要 4 rule split，但 implementation 完全沒套用此 pattern 到 4 個新 model。
  - [[system-mcp-create-acl-bypass]] MAJOR 1 教訓：「CSV `1,1,1,1` + 無 rule scope-restrict」 是 privilege escalation surface（user 透過 ORM create 不被擋）。本案 4 model 全 surface 同等狀態。

- **同源 code path 檢查（grep verify）**：
  ```
  grep "model_woow_paas_platform_(agent_template|agent_session|skill|project_access)" ir_rules.xml
  → 0 hit
  ```
  確認 implementation **完全沒** 嘗試加 4 個新 model 的 rule；不是「加錯」也不是「加少」— **完全 missing**。

- **嚴重度**：**HIGH (BLOCKER)** — 直接 privilege escalation + cross-workspace data leak + RCE-on-pod surface；違反 plan §Phase A scope 明文 + design 明文 + 兩個 known pattern + 重蹈一個 known pitfall

- **不擅自降 severity 理由**：security 漏洞絕不標 medium（[[mcp-project-decouple]] §Round 2 改動敘事 line 187 教訓「always-true ACL leg 必須分 read direction 與 create direction 評估」延伸：「ACL CSV 開放 + 漏 ir.rule scope-restrict」在 read + create 兩個 direction 都漏，不只 read direction 是 always-true 那麼簡單，是**全 direction always-true** — 比 MAJOR 1 更嚴重）

- **建議方向**（不寫具體 patch，給 leader 拍板）：
  - **(a) implementation 補 16 rule + 補 explicit ACL deny test**（plan #13 要求的方向）— 對齊 design §3.2 / §4.3 + plan §To phase-caqa hand-off + [[mcp-acl-6-rule-split]] pattern
  - **(b) leader 拍板「Phase A defer 4 model ACL 到 Phase B/C」**— 此情境 implementation **必須** 把 `ir.model.access.csv:28-35` 8 個 row 的 base.group_user 部分改 `0,0,0,0` 暫關 RWCU（保留 base.group_system row）；Phase A 期間後台與 ORM 都不可動 4 個新 model；Phase B/C 配套加 16 rule + 重開 1,1,1,1
  - **(c) 其他 — escalate user 拍板**

**重點 2：M:N walk path 對 mcp_server / mcp_tool 改完整對齊 Inv #11**：

`ir_rules.xml:156-172` (`mcp_server_rule_workspace_user`) + `:192-208` (`mcp_tool_rule_workspace_user`)：

- ✓ 外層 AND `('scope', '=', 'user')` 守 MAJOR 1 防線（[[system-mcp-create-acl-bypass]] regression lock）
- ✓ leg 1 walk path 加 `_ids` segment（M2M），保留語意對 cross-workspace 兩 workspace member 都可讀（Q1 sign-off OR semantics）
- ✓ leg 2 unbound carve-out 改 `cloud_service_ids = False`（M2M empty set 語意）+ `message_partner_ids` 維持
- ✓ sub-model 對等改 `server_id.` 前綴

**重點 3：D4 invariant 在 per-project M:N pivot 下守線**：

design §1.3.4 已明文確認；implementation：
- `mcp_server.project_id` 欄位定義不動（grep `cloud_service_id` in `mcp_server.py` → 0 hit；仍 `Many2one('project.project', ondelete='cascade')`）
- `_check_scope_project_consistency` constraint 不動
- D4 行為（`scope='user' ⇔ project_id IS NOT NULL`）與 project 綁幾個 cloud_service 無關 — 不被 M:N 影響

**重點 4：cross-workspace M:N data leak 防護（B1 pattern 對齊）**：

從 mcp_server / mcp_tool 視角驗證：
- mcp_server M:N walk OR semantics + sub-model 對等 + 外層 AND `scope='user'` = 三層 defense in depth
- W2 member 對 cross-workspace project（綁 W1 + W2）的 user-scope MCP **可讀**（R.7 explicit pass）
- W2 member 對純 W1 unbound project 的 user-scope MCP **不可讀**（leg 2 walk follower not match → deny）
- 攻擊向量 [[cross-workspace-task-data-leak]] B1 sub-model parity 守線 — mcp_tool 對等改寫 ✓
- 但是 **project_rule_workspace_member / project_task_rule_workspace_member 沒對等改 M:N walk**（見 M-1） — project / task ORM read 對 cross-workspace M:N project 退化為 single workspace visibility

**重點 5：`agent_session._get_namespace_secret` fallback hardcoded（M-3 caveat）**：

- `src/models/agent_session.py:7` 寫死 `_DEFAULT_SECRET = b'woow-paas-agent-namespace-fallback-secret'`
- `ir.config_parameter.get_param('woow_paas_platform.agent_namespace_secret', '')` 沒設時 fallback default
- 公開 git repo → attacker 取得 default secret → 預計算 hash for any project_id
- **與 B-1 串聯**：attacker 透過 B-1 漏洞 create `agent_session(project_id=victim_pid, project_namespace_hash=<pre-computed>)` → UNIQUE(project_namespace_hash) 衝突 → victim spawn pod 時 raise IntegrityError → namespace squat attack
- 即使 B-1 修了，仍是 caveat — admin 應該強制 `woow_paas_platform.agent_namespace_secret` config_parameter 不為空才允許 install（hard guard）

**重點 6：SSRF check 守線（refactor branch 既有）**：

- `controllers/paas.py._validate_mcp_url` 既有（refactor branch），本 epic 不動 ✓
- 4 個新 model 沒新增 URL 寫入 entry → SSRF surface 不擴大
- caveat：`external_mcp_allowed` 是 NetworkPolicy egress gate（Phase B-C helm chart 才落地），Phase A 只加 schema field，不增 SSRF surface

### Performance

**Migration script SQL 範圍**：
- Step 2 INSERT INTO + SELECT FROM project_project：scale ≪ 100 row（dev/stg/prod 既有 project count）— 可忽略
- Step 3 DROP CONSTRAINT：DDL 立即 — 可忽略
- Step 8 UPDATE 全表掃描：同 Step 2 scale — 可忽略
- Step 9 SELECT JOIN 4 table + per-row INSERT：N+1 query for N projects，但 N 預計 ≪ 100 — 可忽略
- **結論**：migration 無 performance blocker

**Computed field re-compute storm 評估**：
- `project_project.workspace_ids` (computed M2M, store=True, depend on `cloud_service_ids.workspace_id`)：每次 cloud_service_ids 或關聯 cloud_service.workspace_id 變動觸發 recompute；對單 project / single transaction 影響低
- `project_project.cloud_service_id` (computed M2O, store=True, depend on `cloud_service_ids`)：同上
- `project_project.workspace_id` (computed M2O, store=True, depend on `cloud_service_id.workspace_id`)：透過 stored compute chain 觸發；可能 chain-trigger
- `project_task.primary_cloud_service_id` (computed M2O, store=True, readonly=False, depend on `project_id.cloud_service_ids`)：每次 project.cloud_service_ids 變動觸發**全部 task** recompute（hybrid drift handler）
  - **caveat（M-4 補充）**：對大 task table（千 task 級）每次 add/remove cloud_service 觸發全表 recompute；但 PaaS 平台 task count 預計小（per project 數十）— 可忽略
- **結論**：computed field re-compute 在預期 scale 下無 performance blocker；大 instance（千 task / 萬 project）需 follow-up 評估

**M:N walk path query plan**：
- `('project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id)` — Odoo ORM 轉成多 JOIN：project_project ⋈ project_cloud_service_rel ⋈ cloud_service ⋈ workspace ⋈ workspace_access — 5 table JOIN，每 table 有 PK + relevant index（pre-migrate 加了 `project_cloud_service_rel_cloud_service_id_idx`）
- 對 ORM `search()` 與 record rule evaluation 都會跑此 JOIN — 每個 ORM read mcp_server / mcp_tool 都觸發
- caveat：可能在大 instance 下 query plan 退化，需 monitoring follow-up

**cloud_service.unlink() override 改 per-record loop**：
- 舊：batch `self.project_ids.mapped(...)` 一次 query
- 新：per-record loop `for service in self`，每 service 跑一次 `_collect_sidecar_user_mcps`（內含 mapped + filtered）
- N service batch unlink → N+1 query overhead
- 但 batch unlink multiple cloud_service 在 production 罕見（通常單 service unlink）— 可忽略

**Index 評估**：
- 新 `project_cloud_service_rel`：加 `(cloud_service_id)` index（per pre-migrate.py:29-33）；PRIMARY KEY 自動 cover `(project_id, cloud_service_id)` 雙向 lookup — ✓ 充分
- 新 4 model：
  - `agent_template`：`name` index ✓
  - `agent_session`：`project_id` + `project_namespace_hash` + `state` + UNIQUE 多條 index ✓
  - `skill`：`name` + `project_id` + `bundle_sha256` + `state` + UNIQUE composite ✓
  - `project_access`：`project_id` + `partner_id` + `user_id` + `role` + UNIQUE composite ✓
- **結論**：4 個新 model index 充分

### Architecture

**per-project ownership 邊界守線**（design §1.3.2 ACL 兩層解耦）：

- ✓ project_access first-class model 已落地（schema + bootstrap）
- ✗ project_access ACL ir.rule 未落地 → 兩層解耦的 first-class 部分功能不全（見 B-1）
- ✓ workspace_access 與 project_access 解耦在 schema 層（project_access 不依賴 cloud_service.workspace_id）
- ✓ design §1.3.2 line 575-577「workspace_access 不蘊含 project_access」概念在 schema 達成；ACL 落地 incomplete

**4 新 model 命名 / 模式**：

- ✓ `_name = 'woow_paas_platform.X'` namespace 對齊既有 workspace_access pattern
- ✓ `partner_id + user_id (computed)` D3=A idiom 對齊 [[cloud-service-project-decouple]] §Q1
- ✓ `_check_at_least_one_owner` constraint shape 對齊 workspace_access ROLE_HIERARCHY
- ✓ `agent_session` UNIQUE(project_id) 對齊「per-project pod」design §1.2 sign-off
- ✓ `agent_session.project_namespace_hash` hmac_sha256 base32 16 char 對齊 design §2.7
- ⚠ `agent_template.is_public` boolean — 缺對應 system-scope read-only rule（與 mcp_server scope='system' 同 pattern 應該套）— 包含在 B-1 範圍

**paas-operator scope 不擴大確認**：

- ✓ Phase A 沒動 paas-operator 任何 file（grep `extra/paas-operator/` diff → 0 hit）
- ✓ 對齊 brief constraint「per task brief constraint」

**沒動 std field 型態確認**（[[odoo-std-field-no-override]] pattern）：

- ✓ `project.project` 沒 override std field 型態（只加新 field）
- ✓ `project.task` 沒 override std field 型態（只擴 `extra_cloud_service_ids` domain）

**[[odoo-addon-std-rule-symmetric-deactivation]] pattern 守線**：

- ✓ refactor branch 既有 3 條 std rule deactivation（`project.project_public_members_rule` / `project.task_visibility_rule` / `project.task_visibility_rule_project_user`）在 `ir_rules.xml:262-267` `<function>` 對 declaration data 路徑保留
- ✓ Phase A 沒新加 base.group_user 對 Odoo std model 的 CSV row（已存在於 refactor branch）— 不觸發 pattern 重評
- ⚠ 但 Phase A 新加 4 個自家 model 對 base.group_user CSV `1,1,1,1` — 雖然不在 pattern scope（自家 model 無 std rule OR-bypass）但 [[cross-workspace-task-data-leak]] 同源 root cause 仍適用：「對 base.group_user 第一次開放某 model 的 ACL = 必須對等加 ir.rule scope-restrict」（見 B-1）

**Architectural drift 評估**：

- Schema 解耦：M:N + 4 first-class model 落地 ✓
- ACL 解耦：schema 層 ✓；ir.rule 層 ✗（B-1） / partial（M-1）
- Hybrid 模型：refactor branch sidecar_backed flag + 6-rule split + dual-leg dual-leg ACL 全繼承 ✓
- B 案延伸 M:N 對齊 design §1.3.3 + §1.3.4 sign-off ✓

**結論**：Architecture 大方向對齊 design，schema 落地完整；ACL 落地半成（B-1 阻斷生效）；plan #13-#18 6 task 漏做反映 implementation 階段缺 hand-off checklist 對齊 plan §Phase A scope 全清單。

---

## 3. Plan alignment check

Implementation 對 plan §Phase A 19 task：

- ✓ 12 / 19 task 落地（#1-12, #19 partial）
- ✗ 6 / 19 task **完全沒做**（#13 / #14 / #15 / #16 / #17 / #18）
- partial：#19 manifest version bump ✓ 但 data 沒加 view files（因為 #14-18 沒做）

**走偏點**：implementation 沒走 plan §Phase A scope 全清單，跳過 5 個 view + 16 條 ir.rule；可能：
- (a) implementation 預設 Phase B/C 補 — 但 plan 沒明說 defer，CSV row 已開放等於開了攻擊面
- (b) implementation 漏看 plan §Phase A task #13 / #14-18 — 但 plan 顯式分項清楚
- (c) implementation 走 test-driven，因 test 沒 cover 視為 out-of-scope — 但 test report §10「ACL 行為矩陣留 Phase B/C」是 **test scope 邊界**，不是 plan scope 邊界

**escalation**：B-1（plan #13 漏做 = security issue）必須 leader 拍板 (a) / (b)；M-2（plan #14-18 漏做 = admin operational gap）建議標 caveat 留 follow-up（不擋 merge）。

---

## 4. 同源 code path 檢查

對本 epic 改動的 pattern 跨 file grep verify「沒漏網」：

| Pattern | grep 結果 | 評估 |
|---------|----------|------|
| `cloud_service_ids` walk in `ir_rules.xml` for mcp_server/mcp_tool | 4 處 hit（leg 1 + leg 2，mcp_server + mcp_tool） ✓ | M:N walk 完整 |
| `cloud_service_id` walk in `ir_rules.xml` for project/task | 2 處 hit（`ir_rules.xml:68` project_rule_workspace_member + `:111` project_task_rule_workspace_member） | **未對等改 M:N walk** — 見 M-1 caveat |
| `cloud_service_id` reads in `controllers/paas.py` | 12 處 hit | controller 大量 walk single M2O；Phase A 沒擴 controller carve-out 是 design 預期（Phase D-E 才動），但 `_resolve_project_access:1510` `project.cloud_service_id.workspace_id` 在 M:N 下退化為 first workspace lookup — 與 mcp_server M:N walk 不對齊；caveat 級 — 見 M-1 |
| `cloud_service_id` reads in `controllers/ai_assistant.py` | 12 處 hit | 同上 |
| `model_woow_paas_platform_(agent_template\|agent_session\|skill\|project_access)` in `ir_rules.xml` | **0 hit** | **完全沒新增 rule** — B-1 BLOCKER |
| `agent_template_views\|agent_session_views\|skill_views\|project_access_views\|project_external_mcp_setting` in `__manifest__.py` | **0 hit** | **5 view files 完全沒加** — M-2 caveat |
| `sidecar_backed` filter shape in `_do_cleanup` + `unlink()` | 2 處 hit（雙 path 對等） ✓ | Inv #9 守線 |
| dual-leg domain `&', ('scope', '=', 'user'), '|', leg1, '&', leg2_a, leg2_b` in `ir_rules.xml` for workspace_user × 2 | 2 處 hit（mcp_server + mcp_tool 對等） ✓ | Inv #11 守線 |

**結論**：4 個新 model ACL rule 完全 missing（B-1）；project/task rule walk path 沒對等改 M:N（M-1）；mcp_server/mcp_tool 改寫完整；5 view files 全 missing（M-2）。

---

## 5. Known pitfalls 對照

對照 leader hand-off 列的 known pitfalls：

| Pitfall | 重蹈跡象 | 評估 |
|---------|---------|------|
| [[cross-workspace-task-data-leak]] B1 incident | **HIGH 重蹈跡象** | 同源 root cause：「對 `base.group_user` 第一次開放 model 的 CSV ACL 必須對等加 ir.rule scope-restrict」— Phase A 對 4 個新 model CSV 開放 `1,1,1,1` 但完全沒加 ir.rule。雖然差別在「自家 model vs Odoo std model」（自家無 std rule OR-bypass），但 leak severity 反而更糟（自家 model 直接 no rule = 預設 allow-all；Odoo std 至少有 std rule 隱含限制） |
| [[plan-vs-odoo-schema-mismatch]] | 無重蹈 | plan 引用的 `message_partner_ids` / `create_uid` / `cloud_service.workspace_id.access_ids` / `workspace_access.role='owner'` / `res.users.partner_id` 全在 Odoo 18 真實存在；migration step 9 SQL JOIN path 全 verified |
| [[system-mcp-create-acl-bypass]] MAJOR 1 | 無重蹈 | dual-leg 外層 AND `('scope', '=', 'user')` 守住；H.11/H.12 explicit regression lock 在 refactor branch 保留 |
| [[direct-unlink-bypass-cleanup-hook]] | 無重蹈 | `cloud_service.unlink()` override 走 `_collect_sidecar_user_mcps` helper + URL prefix narrowing；雙 path 對等改寫 |
| [[two-layer-acl-asymmetric-deny]] | 部分重蹈 | mcp_server ir.rule M:N walk 改了 + controller `_check_project_access` 仍 walk single `cloud_service_id` — 兩層對齊 partial drift；但 controller scope 留 Phase D-E（plan §AC #43-44 明示） — 視為 design intent 而非重蹈；M-1 caveat 提醒未來 phase 對齊 |
| [[legacy-endpoint-scope-blindness]] | 無重蹈 | 本 epic 沒新加 endpoint；refactor branch 既有 SSRF check / sidecar-hint 路徑不動 |

**結論**：**重蹈 [[cross-workspace-task-data-leak]] B1 pitfall**（B-1 BLOCKER 對應）；其他 pitfall 無重蹈跡象。

---

## 6. High-severity findings — Blocker 清單

### B-1 — 4 new model 對 base.group_user 無 ir.rule scope-restrict（HIGH / BLOCKER）

| 欄位 | 內容 |
|------|------|
| **嚴重度** | HIGH (BLOCKER) |
| **面向** | Security (primary) + Architecture + Quality |
| **位置** | `src/security/ir.model.access.csv:28-35`（CSV 8 row 給 base.group_user/base.group_system × 4 model × 1,1,1,1） + `src/security/ir_rules.xml`（缺 16 條 rule） |
| **違反** | plan §Phase A task #13 + design §3.2/§4.3 explicit rule list + [[cross-workspace-task-data-leak]] B1 pitfall + [[two-layer-acl-lockstep]] pattern + plan §To phase-caqa hand-off line 541 |
| **攻擊面** | (1) `project_access` privilege escalation — base.group_user 對任意 project ORM `create(role='owner')` 把自己加成 owner；(2) `agent_template` 跨 project read/write system_prompt + binding；(3) `skill` 跨 project bundle_sha256 forgery → RCE on agent pod；(4) `agent_session` 跨 project K8s quota / namespace_hash / state mutation → DoS / namespace squat |
| **同源 pitfall** | [[cross-workspace-task-data-leak]] B1 — 對 base.group_user 第一次開放某 model 的 CSV ACL 必須對等加 ir.rule scope-restrict（自家 model 無 std rule OR-bypass，但 leak severity 更糟 — 預設 allow-all） |
| **建議方向**（不寫具體 patch） | **(a)** implementation 補 16 rule（4 rule × 4 model）對齊 design §3.2 / §4.3 explicit list + 補 explicit ACL deny test（base.group_user × 4 model × 4 perm × cross-project = matrix）；**(b)** leader 拍板 Phase A defer 4 model ACL 到 Phase B/C → implementation 必須把 CSV row 8 條 base.group_user perm 改 `0,0,0,0` 暫關，保留 base.group_system；**(c)** 其他 — escalate user |
| **escalation** | **必須 escalate leader 拍板**（CAQA 不擅自降 severity） |

---

## 7. Medium / Low findings — Caveat 清單

### Medium

| ID | Finding | Location | Severity | 建議 |
|----|---------|----------|----------|------|
| **M-1** | `project_rule_workspace_member` + `project_task_rule_workspace_member` 沒對等改 M:N walk path — cross-workspace M:N project（綁 W1 + W2）對 W2 member ORM 看不到 project / task 本身（只看到 mcp_server via R.7） | `src/security/ir_rules.xml:65-78, 108-117` | Medium (架構不對稱 + Q1 sign-off cross-workspace OR semantics 在 project/task 層級 partial 實現) | **escalate planner**：plan §Phase A task #4 沒列改 project/task rule，但 design §1.3.2 Q1 sign-off 隱含 cross-workspace 應對等可見。建議 (a) plan/leader 確認「Phase A 不擴 project/task rule walk path」是 design intent，列為 caveat；或 (b) 補 rule 與 mcp_server walk path 對齊 |
| **M-2** | Plan §Phase A task #14-18 五個 view file 完全沒做（agent_template_views / agent_session_views / skill_views / project_access_views / project_external_mcp_setting） | `src/views/` 缺檔 + `src/__manifest__.py` 沒 data reference | Medium (admin operational gap，admin 後台無法直接管理 4 個新 model；external_mcp_allowed 無 admin UI 開關) | F follow-up — Phase A 不擋 merge；Phase B/C admin UI 補；project_external_mcp_setting 需含 SSRF risk 警示（plan #18 明文）|
| **M-3** | `agent_session._get_namespace_secret` fallback default `_DEFAULT_SECRET = b'woow-paas-agent-namespace-fallback-secret'` hardcoded — 公開 git repo → attacker 可預計算 hash | `src/models/agent_session.py:7` | Medium (security caveat；單獨不可利用，但與 B-1 串聯成 namespace collision attack；即使 B-1 修了仍是 caveat) | F follow-up — install hook 加 hard guard：`woow_paas_platform.agent_namespace_secret` config_parameter 必設且 ≥ 32 bytes 才允許 install，否則 raise；或改 default 為 `secrets.token_bytes(32)` 隨機生成（per-install 不同） |
| **M-4** | `cloud_service._sidecar_url_prefix` return `None` for missing helm metadata → `_collect_sidecar_user_mcps` 走 broad filter（清所有 sidecar-backed candidates）— M:N 下 service A 無 helm metadata + service B 有 helm metadata + project 綁兩個 → A unlink 誤殺 B 的 sidecar-backed MCP | `src/models/cloud_service.py:1175-1194` | Medium (誤殺 risk；production 影響低 — service deploy 後才有 sidecar-backed MCP；fixture / pre-deploy state 可能踩) | F follow-up — fallback 改用 `cloud_service.id` 作為 namespace marker（不存在於其他 service URL）；或 raise/warn 強制 helm metadata 一致性 |
| **M-5** | `project.cloud_service_id` 改 stored compute 後失去明示 `ondelete='set null'` — Odoo 18 default 對 stored compute Many2one with store=True 的 ondelete 行為依賴 default fallback（未驗證 Odoo 18 對 stored compute 的具體行為） | `src/models/project_project.py:18-27` | Medium (correctness uncertainty；不一定是 bug 但 best practice 應該保留明示) | F follow-up — verify Odoo 18 對 stored compute M2O FK constraint default ondelete；明示 `ondelete='set null'` 對齊舊版本契約 |

### Low / Info

| ID | Finding | Location | Severity | 建議 |
|----|---------|----------|----------|------|
| **I-1** | `discuss_channel.py:262` comment stale — 寫「task's primary service (project's cloud_service_id) first」但 implementation 已用 `task.primary_cloud_service_id` (hybrid spec) | `src/models/discuss_channel.py:262` | Low (comment-only) | reporter follow-up 順手清 |
| **I-2** | `agent_template` 缺 `is_public=True` 對應的 system-scope read-only rule（與 mcp_server scope='system' 同 pattern 應該套）| `src/models/agent_template.py` + `src/security/ir_rules.xml` | Low（包含在 B-1 範圍，B-1 修時順帶解） | 與 B-1 同步處理 |
| **I-3** | `cloud_service._do_cleanup` 沒明示 `self.ensure_one()` — multi-record self 在 helper 內部會 raise，但行為不清楚 | `src/models/cloud_service.py:1086-1101` | Low (defensive style) | F follow-up — 加 ensure_one() 明示 single-record semantics |
| **I-4** | `project_task.primary_cloud_service_id` docstring 寫「The compute acts as a default」描述不精確（Odoo readonly=False + computed 是 implicit default idiom）| `src/models/project_task.py:38-39, 74-89` | Low (docstring clarity) | reporter follow-up 補 clarity |
| **I-5** | `project_project.create()` override 對 `SUPERUSER_ID` skip bootstrap；demo / install hook 創建的 project owner 將透過 migration step 9 fallback 到 `create_uid` partner（可能是 OdooBot）| `src/models/project_project.py:108-110` + `src/migrations/18.0.1.0.26/post-migrate.py:165-167` | Low (functional 對齊 design，但 OdooBot owner 概念上奇怪) | F follow-up — 評估是否 demo data project 預設不需 owner row（design §4.3「real human owner」明文 — OdooBot 不算）|
| **I-6** | `project_access._compute_user_id` 用 `sudo().search([('partner_id', '=', rec.partner_id.id)], limit=1)` — 無 deterministic ordering | `src/models/project_access.py:72-77` | Low (single partner 對應多 user 邏輯上不該發生，但 limit=1 沒明示 ordering) | F follow-up — 加 `order='id ASC'` 保 deterministic |

---

## 8. Open questions（如分析過程發現 design / plan 漏）

### OQ-A-1 — Phase A 對 4 個新 model 的 ACL 是否必須在 Phase A 落地

**現況**：
- plan §Phase A task #13 明文「加 4 rule × 4 new models = 16 rules（仿 mcp-acl-6-rule-split pattern）」
- design §3.2 + §4.3 對 skill / project_access 列出 explicit 4 rule
- design §4.1 + §2.12 對 agent_template / agent_session **沒明文列 rule list**（但 plan #13 統一要 4 rule × 4 model）
- test report §10 寫「4 個新 model 的 per-rule × per-role × CRUD 矩陣 → 留 Phase B/C test scope expansion」
- implementation 對 4 個新 model 完全沒加 ir.rule

**衝突**：plan #13 / design §3.2 / §4.3 要求 vs test report §10 留 Phase B/C / implementation 沒做。

**escalate**：leader 拍板：
- (a) plan #13 為主 — Phase A 必須補 16 rule + 補 ACL behaviour test
- (b) test report §10 為主 — Phase A defer，但 CSV row 必須改 `0,0,0,0` 暫關
- (c) 部分 — Phase A 補 project_access 4 rule（因為直接的 privilege escalation surface），其他 3 model 留 Phase B/C 但 CSV row 暫關

### OQ-A-2 — project_rule_workspace_member / project_task_rule_workspace_member 是否對等改 M:N walk

**現況**：
- design §1.3.2 Q1 sign-off「cross-workspace M:N walk OR semantics」明示「兩 workspace member 都可讀」— 但只在 mcp_server / mcp_tool 視角 explicit lock（R.7 test）
- plan §Phase A task #4 沒明文要求改 project_rule / task_rule walk path
- implementation 沒改 → cross-workspace M:N project 對 W2 member 看不到 project / task 本身

**衝突**：M:N OR semantics 對 project / task 層級是否該對等實現？

**escalate**：planner / leader 拍板：
- (a) plan 漏列 — 補 project_rule / task_rule M:N walk 對齊 mcp_server
- (b) design intent 是「project / task 對 W2 member 不可見，只 mcp_server 可見」— 標 caveat 留 follow-up，escalate UX 確認
- (c) 設計合理：W2 member 透過 collaboration（被 add 進 message_partner_ids）才可見 → 但這對 cross-workspace 默認行為不對稱（mcp_server 自動可見、project 不自動可見）

### OQ-A-3 — Phase A view files 是否 in-scope

**現況**：plan §Phase A task #14-18 列 5 個 view file；implementation 完全沒做；test 沒測 view 存在；54/54 PASS 不受影響。

**escalate**：leader 拍板：
- (a) Phase A 必須補 view（plan 明文）— 但這是 admin operational，security/data correctness 不擋
- (b) view 留 Phase B/C — 標 follow-up，admin UI gap acceptable
- (c) `project_external_mcp_setting.xml` 必須補（plan #18 含 SSRF risk 警示是 design intent 重要 surface），其他 4 個留 Phase B/C

---

## 9. Caveat 清單（給 leader 決定是否帶入 reviewer）

1. **M-1: project_rule / task_rule M:N walk 對齊**（OQ-A-2 待 planner 拍板；若不擴則標 follow-up）
2. **M-2: 5 view file 缺**（OQ-A-3 待 leader 拍板；若 defer 則標 follow-up）
3. **M-3: agent_session namespace_secret hardcode fallback**（F follow-up — install hook hard guard）
4. **M-4: cloud_service _sidecar_url_prefix broad-filter fallback**（F follow-up — namespace marker 或 raise）
5. **M-5: project.cloud_service_id 失去明示 ondelete='set null'**（F follow-up — verify + 補明示）
6. **I-1 ~ I-6** 共 6 條 low-severity comment / clarity / defensive style 改善

---

## 10. Blocker 清單（給 leader / implementation）

1. **B-1**（HIGH / BLOCKER）— 4 new model 對 base.group_user 無 ir.rule scope-restrict
   - **位置**：`src/security/ir.model.access.csv:28-35` + `src/security/ir_rules.xml`
   - **escalation**：必須 leader 拍板處理路徑（(a) 補 16 rule / (b) CSV 暫關 / (c) 其他）
   - **不擅自降 severity 理由**：privilege escalation + cross-workspace data leak + RCE-on-pod 三條 attack vector 同時 active；重蹈 [[cross-workspace-task-data-leak]] B1 pitfall 同源 root cause

---

## 11. 工具 raw 輸出摘要

本 CAQA 採人工 cross-reference + grep + git diff inspection 完成四面向 verify；**未** dispatch `/sc:analyze` / code-analyzer / architect-reviewer / security-auditor sub-agents（evidence 充分 + invariants 對齊清楚 + 4 個新 model ACL leak 為直接 plan task 漏做 + 對應 known pitfall 同源；sub-agent 第二意見對「實際 implementation 沒做某項 plan task」這類「漏做事實」價值低）。

依賴材料：

- **Plan**：`docs/plans/2026-Q2/agent-runtime-pod-plan.md`（682 行，19-task Phase A scope + 7-phase plan + hand-off checklist）
- **Design**：`docs/plans/2026-Q2/agent-runtime-pod-design.md`（4794 行，§1-§7 sign-off；本 CAQA 重點 §1.3 / §3.2 / §4.1 / §4.3 / §2.12 / §7.0）
- **Test report**：`docs/tracking/test-reports/test-coverage-agent-runtime-phase-a-2026-05-22.md`（589 行，cell map + Q1/Q2 sign-off 落地 + 3 條 regression lock）
- **既有 CAQA 範例**：`docs/tracking/caqa-report-2026-05-20-mcp-refactor.md`（PR #54 caqa 格式對齊）
- **Refactor branch invariants**（守線參考）：
  - `docs/decisions/refactor/mcp-project-decouple.md`（8 invariants）
  - `docs/decisions/refactor/mcp-url-redesign.md`（Inv #9/#10/#11）
  - `docs/decisions/patterns/mcp-acl-6-rule-split.md`
  - `docs/decisions/patterns/two-layer-acl-lockstep.md`
  - `docs/decisions/patterns/unbound-project-acl-dual-leg.md`
  - `docs/decisions/patterns/sidecar-backed-flag-lifecycle.md`
  - `docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md`
  - `docs/decisions/patterns/odoo-std-field-no-override.md`
- **Pitfalls**：
  - `docs/tracking/pitfalls/cross-workspace-task-data-leak.md`（B1 同源）
  - `docs/tracking/pitfalls/plan-vs-odoo-schema-mismatch.md`
- **Git diff scope**：`git diff HEAD`（feature/agent-pod tip 801a91d → working tree） — 9 modified + 9 untracked = 14 file scope + test reports
- **Grep 同源 code path**：14 處 grep verify（§4 同源 code path 檢查 + §5 known pitfalls）

掃描範圍：

- 14 file Phase A diff 全讀
- 8 個 decision / pattern docs 全讀
- 2 個 pitfall doc 全讀
- design §1.3 (Project first-class) + §1.3.2 (ACL 兩層) + §1.3.3 (refactor branch 引用) + §1.3.4 (D4 守線) + §3.2 (skill ACL) + §4.1 (agent_template) + §4.3 (project_access ACL 4 rule) + §4.7 (primary_workspace) + §7.0 (migration steps)
- plan §Phase A 19-task scope 全對照
- test report §3 cell map + §5 regression lock + §10 不在 scope 列表
- 既有 ir_rules.xml + ir.model.access.csv 全讀

---

## 12. Sign-off

CAQA verdict：**BLOCKED** — B-1 BLOCKER 必須 leader 拍板，無法直接進 reviewer phase。

**Verdict 彙整**：
- **1 BLOCKER**（B-1 — 4 new model 無 ir.rule scope-restrict）
- **5 Caveat**（M-1 project/task rule M:N walk / M-2 5 view files / M-3 namespace_secret hardcode / M-4 broad-filter fallback / M-5 ondelete 未明示）
- **6 Info**（I-1 ~ I-6 — comment / clarity / defensive style）
- **3 Open Questions**（OQ-A-1 4 model ACL / OQ-A-2 project/task rule M:N / OQ-A-3 5 view files）

**escalation paths**：
- B-1 → leader 拍板 implementation 路徑（補 rule / CSV 暫關 / 其他）
- OQ-A-2 → planner 拍板「Phase A 是否擴 project/task rule M:N walk」
- OQ-A-3 → leader 拍板「Phase A 5 view files 是否 in-scope」

**Reviewer special attention（B-1 unblock 後）**：

- ACL ir.rule scope-restrict shape 對齊 design §3.2 / §4.3 explicit rule list（per-project_access role per perm）
- [[two-layer-acl-lockstep]] 預期 Phase B-E 補 controller carve-out — Phase A 完成 ir.rule 後留 follow-up
- ACL behaviour test：4 model × base.group_user × 4 perm × cross-project = ≥ 16 case explicit deny matrix
- N.11 `_check_at_least_one_owner` 不擋 base.group_user create owner row to non-own project — 必須加 explicit deny test

**Hand-off back to leader**：等 leader 對 B-1 拍板後 re-run CAQA verdict（或直接由 implementation re-loop 補完）。

---

## 13. Re-verify after impl Round 2（2026-05-22）

> Leader 選 **(a)** 退回 impl 補 16 rule + ACL behaviour test 路徑。Impl Round 2 完成：137/137 PASS（B-1 29 + OQ-A-2 5 + Phase A 既有 54 + named regression 49）；full suite 656 test / 0 fail。本節 verify B-1 + OQ-A-2 closure 並更新 verdict。

### 13.1 Verdict 更新

**APPROVED WITH CAVEATS** — B-1 closed；OQ-A-2 closed；OQ-A-3（5 view files defer）leader 拍板成 follow-up；可進 reviewer phase。

- **B-1 closure**：✅ 16 new rule（4 model × 4 rule）+ PA-5 Python gate + SK-6 field-level mask 全到位；攻擊面（privilege escalation × cross-project data leak × bundle RCE × DoS）全部關閉
- **OQ-A-2 closure**：✅ project_rule_workspace_member + project_task_rule_workspace_member M:N walk path 對齊 mcp_server / mcp_tool pattern
- **OQ-A-3**：leader 拍板 defer to Phase B/C → follow-up F-PA-9
- **5 caveat（M-1 ~ M-5）** + **6 info（I-1 ~ I-6）** + 本輪新發現 3 caveat（C-A-1 ~ C-A-3）→ durable to `docs/tracking/followups/follow-ups.md` F-PA-1 ~ F-PA-12

### 13.2 B-1 closure 證據（16 rule × attack surface mapping）

對照原 §6 列的 4 條 attack vector，逐條 verify rule 守住的具體 path：

#### Attack 1 — `project_access` privilege escalation

**Pre-fix**：base.group_user 對任意 project ORM `create({'project_id': N, 'partner_id': me, 'role': 'owner'})` 把自己加成 owner（CSV 1,1,1,1 無 rule）。

**Post-fix rule guard**（`ir_rules.xml:431-462`）：
- `project_access_rule_owner_admin_manage`：domain `[('project_id.project_access_ids', 'any', [('user_id', '=', user.id), ('role', 'in', ['owner', 'admin'])])]` perm_create=True
  - **'any' operator 鎖 user_id + role 同 row**：attacker 在 victim project 沒 access row → 任何 row 都不滿足 `user_id = attacker.uid AND role in [owner, admin]` → `'any'` 為 False → rule 不通 → create direction deny

**Post-fix Python guard**（`project_access.py:109-158`）：
- `_check_assignable_role(target_role, project_id)` 在 create 與 write 兩 path call
- attacker (no access row) → grantor_role = None → `ASSIGNABLE_ROLES_BY_ROLE.get(None, [])` = `[]` → target_role 'owner' not in [] → `AccessError`
- Defense in depth：ir.rule 擋 create direction；Python gate 即使 sudo 路徑也擋 role hierarchy 違反（PA-5）

**Verify lines**：
- `ir_rules.xml:431-440`（rule definition）
- `project_access.py:20-25`（ASSIGNABLE_ROLES_BY_ROLE table）
- `project_access.py:109-123`（create/write 兩 path call _check_assignable_role）
- `project_access.py:125-158`（_check_assignable_role body + bypass logic for SUPERUSER + group_system）

**Closure status**：✅ **closed** — double guard（ir.rule + Python）

#### Attack 2 — `agent_template` 跨 project read system_prompt / write binding

**Pre-fix**：base.group_user 對任意 template 全 RWCU。

**Post-fix rule guard**（`ir_rules.xml:286-326`）：
- `agent_template_rule_public_readonly`: domain `[('is_public', '=', True)]` perm_read=True only（platform-level public template — system-curated）
- `agent_template_rule_project_admin`: domain `[('project_ids.project_access_ids', 'any', [('user_id', '=', user.id), ('role', 'in', ['owner', 'admin'])])]` R/W/C/U
- `agent_template_rule_project_user`: 同上 walk role='user' R only
- `agent_template_rule_admin_full`: group_system [(1, '=', 1)] R/W/C/U

**對應 design §4.1 line 1965-1968** ✓ 名稱與 domain 完全對齊。

**Cross-row 攻擊守線**：attacker 對自己有 access 的 project X owner role + victim project Y user role（如果他有的話）→ 'any' operator 鎖 X / Y 各自 row，attacker 對 victim template (project Y) 評估時，需要 Y 上 user_id+owner/admin 同 row 滿足；attacker 在 Y 只是 user role → 不滿足 → deny。

**Closure status**：✅ **closed** — 4 rule per design §4.1 ✓ + 'any' 守 cross-row

#### Attack 3 — `skill` 跨 project bundle_sha256 forgery → RCE on agent pod

**Pre-fix**：base.group_user 對任意 skill 全 RWCU + 對 bundle_attachment_id 全 read（pre-fix CSV 1,1,1,1 沒 group restrict on attachment field）。

**Post-fix rule guard**（`ir_rules.xml:354-394`）：
- `skill_rule_project_admin`: project owner/admin R/W/C/U（'any' user_id+role 同 row）
- `skill_rule_project_user`: user R only
- `skill_rule_project_guest`: guest R only
- `skill_rule_admin_full`: group_system R/W/C/U

**Post-fix field-level guard**（`skill.py:42-47`）：
- `bundle_attachment_id = fields.Many2one('ir.attachment', groups='base.group_system')`
- field-level mask — 任何 user lacking group_system read field returns False / write raises AccessError
- design §3.2 line 1529「不可 read `bundle_attachment_id`，through binary field group restrict」對齊 ✓
- SK-6 path (a) sign-off ✓

**對應 design §3.2 line 1527-1530**：4 rule 名稱對齊 ✓；但 `skill_rule_project_user` design 寫 「R=True, W/C=True (only own draft), U=False」— implementation 簡化為 R only（ir.rule 無法表達 "only own draft" — 需 Python state guard）→ **C-A-1 caveat**（F-PA-10）

**Closure status**：✅ **closed** — row-level deny + bundle binary field-level mask；attacker 無法 R bundle binary 即無法 forge sha256 + verify chain；即使 attacker 透過 RCE 路徑想直接 write `bundle_sha256` 也被 row-level rule 擋

#### Attack 4 — `agent_session` 跨 project K8s lifecycle abuse

**Pre-fix**：base.group_user 對任意 agent_session 全 RWCU（含 quota_override / namespace_hash / state）。

**Post-fix rule guard**（`ir_rules.xml:328-368`）：
- `agent_session_rule_owner_admin`: project owner/admin R/W/C/U（'any' user_id+role 同 row）
- `agent_session_rule_user`: user R only
- `agent_session_rule_guest`: guest R only
- `agent_session_rule_admin_full`: group_system R/W/C/U

**對應 design**：design §2.12 沒 explicit list 4 rule，但 line 1278 寫「agent_session 走 project_access 推導」+ plan #13 統一「4 rule × 4 model」要求 — implementation 對齊 intent ✓

**與 M-3 串聯（namespace_secret hardcode）**：B-1 修了後，attacker 即使知 default secret 預計算 hash for any project_id，也無法 ORM `create({'project_id': victim_pid, 'project_namespace_hash': pre_computed})` — `agent_session_rule_owner_admin` 鎖 attacker 必須在 victim project owner/admin role 才能 create → attack chain 被切斷。M-3 caveat 從「直接 RCE」降為「敏感資料」（attacker 仍可預計算自己 project 的 hash 預測未來 K8s namespace，但無法占位）→ M-3 嚴重度從 medium 降 low → 仍寫入 F-PA-1 follow-up

**Closure status**：✅ **closed** — row-level + 與 M-3 串聯切斷

### 13.3 OQ-A-2 closure 證據（project / task rule M:N walk）

**Pre-fix**（原 §7 M-1 caveat）：
- `ir_rules.xml:68` `project_rule_workspace_member` walk `cloud_service_id.workspace_id.*` (single M2O)
- `ir_rules.xml:111` `project_task_rule_workspace_member` 同上
- Cross-workspace M:N project（綁 W1 + W2）對 W2 member 看不到 project / task 本身（mcp_server 對等 R.7 已可見，但 project / task 不對等 — silent functional regression）

**Post-fix**（`ir_rules.xml:68, 111`）：
- `project_rule_workspace_member`: leg 1 `cloud_service_id.workspace_id...` → `cloud_service_ids.workspace_id...` + leg 2 `cloud_service_id = False` → `cloud_service_ids = False`
- `project_task_rule_workspace_member`: 同對等改 + `project_id.` 前綴維持
- 對齊 `mcp_server_rule_workspace_user` / `mcp_tool_rule_workspace_user` walk pattern（all 4 rule consistent）

**Q1 sign-off cross-workspace OR semantics 對齊**：
- 之前 mcp_server R.7 test cross_workspace_project_mcp_visible_to_both_workspaces pass — 但 project / task 沒對等 test
- impl 補了 5 test 走 OQ-A-2 path（per task brief「OQ-A-2 5」）— W1 / W2 member 對 cross-workspace project / task 對等可見

**Closure status**：✅ **closed** — walk path 4 rule（mcp_server + mcp_tool + project + task）全對齊 M:N + Q1 sign-off cross-workspace OR semantics 在 project / task 層級實現

### 13.4 11+7 invariants 守線 re-verify

對應原 §1：

| Inv | 守線 | Notes |
|-----|------|-------|
| #1 D4 (mcp_server.project_id ondelete=cascade) | ✓ | impl Round 2 不動 mcp_server.py |
| #2 D4 (scope='user' iff project_id NOT NULL) | ✓ | 同上 |
| #4 (cleanup hook 順序 + sidecar_backed filter) | ✓ | impl Round 2 不動 cloud_service.py |
| #5 (6-rule split mcp_server/mcp_tool) | ✓ | impl Round 2 只動 4 個新 model + project/task M:N walk，6-rule split 全保留 |
| #6 (system_readonly perm_create/write/unlink=False) | ✓ | impl Round 2 不動 |
| #7 (mcp_server.url snapshot) | ✓ | impl Round 2 不動 |
| #8 (system-scope create 限 sudo/admin/demo) | ✓ | impl Round 2 不動 |
| #9-#11 (sidecar_backed lifecycle + dual-leg + Inv #11) | ✓ | impl Round 2 不動 dual-leg shape |
| Phase A #6 (project_access first-class ACL) | ✅ **fully closed** | schema + bootstrap + 4 ir.rule + Python gate 全到位 |
| Phase A #7 (cross-workspace M:N OR semantics) | ✅ **fully closed** | mcp_server + mcp_tool + project + task 4 rule 全對等 M:N walk |
| Phase A #1-#5 | ✓ | impl Round 2 不動 |

**Inv #11 re-verify** — `workspace_user` × 2 dual-leg shape：
- 外層 AND `('scope', '=', 'user')` 不動 ✓
- leg 1 walk `cloud_service_ids` M:N（impl Round 1 已改）✓
- leg 2 carve-out `cloud_service_ids = False AND message_partner_ids in user.partner_id.id` ✓
- mcp_server / mcp_tool 對等 ✓
- 4 條不動 rule（system_readonly × 2 + admin_full × 2）不動 ✓

### 13.5 Tester observation #1 confirm — 9 PASS regression lock 仍綠

Impl Round 2 報 137/137 PASS（B-1 29 + OQ-A-2 5 + Phase A 既有 54 + named regression 49）；49 named regression 涵蓋 refactor branch 9 PASS regression lock。本 CAQA verify 守線：

- `test_mcp_acl_unbound_isolation.py:H.11/H.12` MAJOR 1 reproducer — 對 base.group_user create scope='system' 必 raise — Phase A 不動 mcp_server schema / 6-rule split → 守線
- `test_mcp_sidecar_backed_lifecycle.py` 4-method paired fixture — sidecar-backed cleanup vs external preserve；Phase A `_do_cleanup` + `unlink()` filter 加 URL prefix narrowing 不破 sidecar_backed flag 條件 → 守線
- `test_other_four_rules_intact` (R.3) + `test_mcp_acl_rule_count_parity` (R.9) — 6-rule split 對等 + count parity；Phase A 不動 6-rule + 加 16 rule 不衝突（其中 4 model × 4 rule 各自獨立 ir.rule，不影響 mcp_server 的 6-rule count parity）→ 守線

### 13.6 新發現 caveat（C-A-1 ~ C-A-3）— durable as follow-ups

| ID | 內容 | Severity | follow-up |
|----|------|----------|----------|
| **C-A-1** | `skill_rule_project_user` perm_write/perm_create=False — design §3.2 line 1528「user W/C only own draft」implementation 簡化為 conservative R only（ir.rule 無法表達「own draft」需 Python state guard） | low | F-PA-10 |
| **C-A-2** | `ASSIGNABLE_ROLES_BY_ROLE[ROLE_OWNER]` 包含 ROLE_OWNER；design §4.3 line 2090 `ASSIGNABLE_ROLES_BY_OWNER = ['admin', 'user', 'guest']` 不含 owner — implementation 比 design 寬鬆（owner 可邀請其他 owner） | low | F-PA-11 |
| **C-A-3** | `skill.bundle_attachment_id` 用 `groups='base.group_system'` field-level mask — 同時擋 project owner/admin 透過 backend form 直接 attach bundle（必須走 ORM sudo / API endpoint upload）；design intent 是 protect from non-system，implementation choice acceptable but maintainer documentation 不足 | low | F-PA-12 |

### 13.7 [[two-layer-acl-lockstep]] 半實現確認

Phase A 完成 ir.rule 層（Layer 1）；controller carve-out（Layer 2a / 2b）留 Phase B-E（plan §AC #43-44 + design §1.3.2 / §4.3 明示）。

**lock-step 半實現狀態**：
- Layer 1 ir.rule 16 new rule + project/task M:N walk 全到位 ✓
- Layer 2 controller `_check_project_access` / `_resolve_project_access` 對 project_access role 沒對齊 — Phase B-E 補

**Risk**：Phase A → Phase B-E 中間期，ORM read mcp_server 經 ir.rule M:N walk OK；但 controller endpoint 仍 walk single `cloud_service_id.*` → silent asymmetric — **不算 silent IDOR**（沒收緊一邊放鬆另一邊；兩邊都用既有 walk）。Phase B-E 補時必須對齊 [[two-layer-acl-lockstep]] pattern。

**Reviewer special attention**：reviewer 確認 Layer 1 落地完整即可；Layer 2 lock-step Phase A scope 外。

### 13.8 Hand-off updated

CAQA Round 2 verdict：**APPROVED WITH CAVEATS** — 進 reviewer phase。

**Reviewer special attention**：
- ACL 16 rule shape 對齊 design §3.2 / §4.1 / §4.3 explicit list（agent_session per plan #13 統一 4 rule 但 design 無 explicit list — flag review）
- `'any'` operator 守 cross-row 攻擊（每條走 `project_access_ids` walk 都用）
- PA-5 Python gate + ir.rule create direction 雙保險（defense in depth）
- skill bundle attachment field-level mask `groups='base.group_system'` — 對 admin 後台 form 行為（attach 不能透過 backend form）需 reviewer 確認 acceptable
- project / task M:N walk 對 cross-workspace project OR semantics 落地
- C-A-1 / C-A-2 / C-A-3 三新 caveat surface — leader 已標 follow-up，不擋 merge
- M-1 / M-2 / M-3 / M-4 / M-5 + I-1~6 + OQ-A-3 12 follow-up（F-PA-1~F-PA-12）durable in `docs/tracking/followups/follow-ups.md`

**等 leader hand-off reviewer**。

---

## 14. Re-verify after Cycle 3 hotfix（HC-3 + HC-4，2026-05-23）

> Reviewer round 1 (2026-05-22) 抓兩條 hard caveat（HC-3 cross-project write-injection on agent_template.project_ids + HC-4 ir.attachment side-channel deny on `/web/content`）；leader 採 option C（hotfix in this PR）。Impl round 3 完成 144/144 PASS（HC-3 4 + HC-4 4 + 既有 137）；full suite 664 / 0 fail / 10 pre-existing env error 不變。本節 verify HC-3 / HC-4 closure + std rule symmetric deactivation 紅線。

### 14.1 Verdict 更新

**APPROVED WITH CAVEATS**（維持 round 2）— HC-3 + HC-4 全 closed；std rule symmetric deactivation 紅線守線（Odoo 18 base 對 `ir.attachment` 0 ir.rule on base.group_user，independent grep verified）；無新 blocker。

### 14.2 HC-3 closure 證據 — `agent_template._check_project_ids_grantor_authority`

**Attack vector**：project A admin Charlie 建 template T bound to A，然後 `T.write({'project_ids': [(4, B)]})` 把 T 延伸到 B；T 上 project_ids 仍包含 A（user 有 admin role）→ `agent_template_rule_project_admin` 的 `'any'` operator 仍 match A row → ir.rule 允許 write；T 對 B 也 visible → cross-project information leak + 模板綁定汙染 victim B 的 agent runtime stack。

**Fix**（`src/models/agent_template.py:94-122`）：

```python
@api.constrains('project_ids')
def _check_project_ids_grantor_authority(self):
    if self.env.is_superuser() or self.env.user.has_group('base.group_system'):
        return
    user = self.env.user
    for tmpl in self:
        for proj in tmpl.project_ids:
            authorized = proj.project_access_ids.filtered(
                lambda r: r.user_id == user and r.role in ('owner', 'admin')
            )
            if not authorized:
                raise ValidationError(...)
```

**Verify**：

- `@api.constrains('project_ids')` 對 create + write 兩 path 都 trigger ✓
- **per-project loop** 確保 every member of `project_ids` 都通過 grantor authority check（不只 newly-added row）— 比 reviewer 字面要求嚴格（防 future regression：若 future 改 constraint 為 delta-only，仍可被既有 row 假通過）
- **bypass logic**:
  - `is_superuser()` → 對齊 PA-5 `_check_assignable_role` bypass pattern + 對 sudo seeding path 開放 ✓
  - `has_group('base.group_system')` → 對齊 admin_full ir.rule pattern + admin backend curation 路徑 ✓
- **role check**: `r.user_id == user and r.role in ('owner', 'admin')` ✓ 對齊 plan §4.4 line 2127「Modify template ↔ project binding | ✓ owner/admin only」

**Tester HC3.1-3.4 cell map verify**（`test_agent_pod_phase_a_acl_new_models.py:822-980+`）：

| Test | 攻擊 / regression case | Expected | Impl behaviour |
|------|--------------------|---------|----------------|
| HC3.1 | Charlie (P1 admin, NO access to P2) write project_ids=[(4, P2)] | `ValidationError` | loop project P2: filter `user_id=charlie AND role in ('owner','admin')` → empty → raise ✓ |
| HC3.2 | Charlie (admin on BOTH P1 AND P2) write project_ids=[(4, P2)] | success | loop P1+P2 each pass → no raise ✓ |
| HC3.3 | sudo() context write cross-project | success (bypass) | `is_superuser()` → early return ✓ |
| HC3.4 | base.group_system user (Eve) write cross-project | success (bypass) | `has_group('base.group_system')` → early return ✓ |

**Closure status**：✅ **closed** — impl 對齊 attack vector + 4 cell 全 cover + bypass 路徑明確

**Caveat surface**：
- (none new) — design §4.1 沒明文 list grantor authority constraint，但 plan §4.4 invoke 矩陣對齊；planner 已 reverse-document 入 design（task brief 寫 「planner 字面化 design §4.1 spec」）

### 14.3 HC-4 closure 證據 — skill bundle attachment row-level + side-channel deny

**Attack vector**（reviewer round 1 抓的精準路徑）：
1. `skill.bundle_attachment_id` 是 `groups='base.group_system'` field-level mask（B-1.SK-6 path a）— 對 base.group_user **read field returns False / write raises**，但 **沒守 attachment row itself**
2. attacker (base.group_user + project_access) 知 attachment id（從 backend / logs / OWL devtools） → `/web/content/<id>?download=1` HTTP endpoint
3. Endpoint 走 `ir.attachment._check` Python delegation 邏輯：`res_model + res_id` set → delegate to skill model ACL → skill `_rule_project_user/_project_guest` allow R → attachment R allow → **stream bundle bytes** → RCE potential

**Fix two layers**：

#### Layer 1 — Anchor stamping（`src/models/skill.py:91-129`）

```python
@api.model_create_multi
def create(self, vals_list):
    skills = super().create(vals_list)
    for skill in skills:
        skill._anchor_bundle_attachment()
    return skills

def write(self, vals):
    result = super().write(vals)
    if 'bundle_attachment_id' in vals:
        for skill in self:
            skill._anchor_bundle_attachment()
    return result

def _anchor_bundle_attachment(self):
    self.ensure_one()
    att = self.bundle_attachment_id
    if not att:
        return
    updates = {}
    if att.res_model != self._name:
        updates['res_model'] = self._name
    if att.res_id != self.id:
        updates['res_id'] = self.id
    if att.public:
        updates['public'] = False
    if updates:
        att.sudo().write(updates)
```

**Verify**：
- create + write 兩 path 都 trigger anchor ✓
- 對 attachment stamp `res_model='woow_paas_platform.skill' + res_id=skill.id + public=False` 三件套 ✓
- `att.sudo().write(updates)` 必要：res_model/res_id/public 本身是 attachment field，base.group_user 受 ir.rule + Python `_check` 限制；sudo bypass 才能 stamp
- **Subtle**：`self.bundle_attachment_id` field 是 `groups='base.group_system'` masked — 對 base.group_user 走 `self.bundle_attachment_id` read returns False → early `if not att: return` → skip anchor。這對 base.group_user create skill **沒 trigger anchor** — 但這 OK，因為 base.group_user 無法寫 `bundle_attachment_id` field（field-level mask 擋 write），所以 attacker 走 ORM create skill 時 **不可能** 帶 bundle_attachment_id；anchor 只在 sudo / group_system path 觸發（API upload endpoint / cron / migration）✓

#### Layer 2 — ir.rule on `ir.attachment`（`src/security/ir_rules.xml`，new 2 rules at end）

```xml
<record id="ir_attachment_rule_skill_bundle_user_scope" model="ir.rule">
    <field name="domain_force">[('res_model', '!=', 'woow_paas_platform.skill')]</field>
    <field name="groups" eval="[(4, ref('base.group_user'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>

<record id="ir_attachment_rule_skill_bundle_admin_full" model="ir.rule">
    <field name="domain_force">[(1, '=', 1)]</field>
    <field name="groups" eval="[(4, ref('base.group_system'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>
```

**Verify rule logic 對 attack vector**：

- base.group_user 對 attachment row `res_model='woow_paas_platform.skill'`：
  - `ir_attachment_rule_skill_bundle_user_scope` domain `res_model != skill` → **False** → group_user 那條 path deny
  - 沒其他 group rule for base.group_user → 整個 deny ✓
- base.group_system（continue 繼承 base.group_user via `_group_implied_ids`）：
  - group_user rule: deny for skill attachment
  - `ir_attachment_rule_skill_bundle_admin_full`: `[(1, '=', 1)]` → **True** → group_system path allow
  - per-group OR: True → allow ✓
- **write direction attack（attacker 對既有 attachment write res_model='skill'）**：
  - Odoo 對 ir.rule write direction evaluate post-write state（`ir_rule.py:_compute_domain` against new vals）
  - new vals.res_model='skill' → group_user rule domain False → deny write ✓
  - attacker 無法把任意 attachment hijack 成 skill bundle ✓

**Tester HC4.1-4.4 cell map verify**（`test_agent_pod_phase_a_attachment_acl.py:109-310+`）：

| Test | 攻擊 / regression case | Expected | Impl behaviour |
|------|--------------------|---------|----------------|
| HC4.1 | Charlie (project_access role=user) `bundle_attachment.with_user(charlie).read(['datas'])` | `AccessError` | ir.rule group_user domain False for skill bundle → deny ✓ |
| HC4.2 | Charlie 升 project_access role=admin (still no base.group_system) | `AccessError` | project-level admin role ≠ platform-level system admin；仍走 base.group_user rule → deny ✓ |
| HC4.3 | Eve (base.group_system) read attachment | success + bytes | admin_full rule [(1,'=',1)] True → allow ✓ |
| HC4.4 | HTTP `GET /web/content/<bundle_id>?download=1` for Charlie | deny | controller 走 attachment row-level access → ir.rule deny ✓ |

**Closure status**：✅ **closed** — defense in depth：
- Layer 1 anchor 確保 stamp 正確（防 attachment row 無 res_model / res_id 走 fallback admin-only mask Charlie by accident）
- Layer 2 ir.rule 對 base.group_user **直接 deny** skill bundle attachment row（不依賴 Python delegation）
- 4 cell test 涵蓋 ORM read + role-promote regression + group_system regression + HTTP side-channel

### 14.4 std rule symmetric deactivation 紅線 verify（independent）

> 任務 brief 明文要求「你獨立 verify」— 不只信 impl claim。

**Impl claim**：Odoo 18 base 對 `ir.attachment` 對 `base.group_user` 0 std ir.rule。

**Independent grep verify**（task brief「DB query 或 grep base addons」二選一，採 grep）：

```bash
# 1. base/security/ 內所有 ir.attachment 引用
grep -rn "ir.attachment" /Users/eugene/.../odoo-18.0/odoo/addons/base/security/
# → 1 hit:
#   base/security/ir.model.access.csv:3 "access_ir_attachment_group_user","...","model_ir_attachment","group_user",1,1,1,1
#   base/security/ir.model.access.csv:4 "access_ir_attachment_group_portal_public","...","model_ir_attachment",,0,0,0,0
# CSV ACL row only — NO ir.rule

# 2. base_security.xml 內 ir.attachment ir.rule
grep -nE "(ir.attachment|model_ir_attachment)" /Users/eugene/.../odoo-18.0/odoo/addons/base/security/base_security.xml
# → 0 hit

# 3. 廣 grep — Odoo 全 addons 對 ir.attachment 的 ir.rule (model + domain)
grep -rEn "(model_ir_attachment|res_model.*'ir.attachment'|model.*'ir.attachment')" \
    /Users/eugene/.../odoo-18.0/odoo/addons/*/security/*.xml \
    /Users/eugene/.../odoo-18.0/odoo/addons/*/data/*.xml | grep -iE "rule|domain"
# → 0 hit

# 4. base/security/ 目錄結構
ls /Users/eugene/.../odoo-18.0/odoo/addons/base/security/
# → base_groups.xml + base_security.xml + ir.model.access.csv
# base_security.xml 內無 ir.attachment rule
```

**Conclusion**：✅ **0 std ir.rule on ir.attachment for base.group_user in Odoo 18 base + all bundled addons** — impl claim verified independent grep。

**對 [[odoo-addon-std-rule-symmetric-deactivation]] pattern 紅線**：
- pattern trigger 條件「對既有有 std rule on base.group_user 的 model 加新自家 rule」
- 本案 ir.attachment 有 std CSV row（access_ir_attachment_group_user 1,1,1,1）但 **無 std ir.rule** → pattern 不 trigger → **不需要 deactivate**
- impl 加 ir.rule 是「first record rule on base.group_user for ir.attachment」— 對齊 [[cross-workspace-task-data-leak]] B1 root cause「對 base.group_user 第一次加 ir.rule scope-restrict 必須對等加 group_system admin_full bypass」— impl **同步加了 `ir_attachment_rule_skill_bundle_admin_full`** ✓

**紅線守線結論**：✅ **守線** — Odoo 18 base / addons 全 grep 0 std rule on ir.attachment for base.group_user；impl 不撞 [[odoo-addon-std-rule-symmetric-deactivation]] pattern 觸發條件；對齊 [[cross-workspace-task-data-leak]] B1 pattern「first rule on base.group_user → 對等加 group_system admin_full」

### 14.5 Side-effect on `ir.attachment._search` Python delegation

**Subtle architectural verify**：Odoo 18 `ir.attachment._search`（`addons/base/models/ir_attachment.py:520+`）對 sudo bypass 後仍走 Python delegation — for `res_model='X' + res_id=Y` attachment 去 X model 看 user 能不能 read record id=Y。

**對 skill bundle attachment**：
- ir.attachment._search 內 `if self.env.is_superuser(): return super()._search(...)` — sudo 直接 bypass Python delegation
- 對 non-sudo base.group_user：先走 ir.rule（Odoo ORM `_search` 內 evaluate record rule）→ skill bundle attachment row 被 `ir_attachment_rule_skill_bundle_user_scope` 排除（domain False） → 不進 Python delegation → defense in depth Layer 1 工作 ✓
- 即使 ir.rule layer 失效（未來有人不慎 disable）→ Python delegation 走 skill model ACL → skill_rule_project_user/_project_guest perm_read=True → user 仍可 read attachment → Layer 1 alone 不夠；ir.rule 是必要 ✓

**Conclusion**：Layer 1 anchor + Layer 2 ir.rule 兩層皆必要；anchor 守 delegation correct target，ir.rule 守 row-level access — defense in depth 完整對齊 [[two-layer-acl-lockstep]] pattern intent。

### 14.6 New findings（none blocking）

- **(none new BLOCKER)** — HC-3 / HC-4 完整 close；無 new attack surface surface
- **(no new CAVEAT)** — anchor 對 base.group_user 不 trigger 是 design intent（field-level mask 已擋 write，anchor skip 是 acceptable behaviour）

### 14.7 Verify table update（13.4 + 14.4 累積）

| Inv / pattern | 守線 | Notes |
|--------------|------|------|
| 11 條 refactor branch invariants（PR #47 + #54）| ✓ | round 3 不動 mcp_server / cloud_service / dual-leg ACL |
| 7 條 Phase A 新加 invariants | ✓ | round 3 不動 schema / cleanup filter / 4 model rule（除 agent_template constraint 補強）|
| [[two-layer-acl-lockstep]] | ✓ partial | Layer 1 (ir.rule) 全到位；anchor + Layer 2 ir.attachment 對 skill bundle defense in depth |
| [[unbound-project-acl-dual-leg]] | ✓ | mcp_server / mcp_tool dual-leg shape 維持 |
| [[cross-workspace-task-data-leak]] B1 root cause | ✓ | round 3 對 ir.attachment 第一次加 base.group_user ir.rule → 對等加 group_system admin_full ✓ |
| [[odoo-addon-std-rule-symmetric-deactivation]] | ✓ | independent grep 0 std rule on ir.attachment for base.group_user → pattern 不 trigger |
| [[mcp-acl-6-rule-split]] | ✓ | round 3 不動 mcp_server / mcp_tool 6-rule split |
| [[sidecar-backed-flag-lifecycle]] | ✓ | round 3 不動 cleanup filter |
| Phase A #8（HC-3 grantor authority + HC-4 attachment row-level） **NEW invariant** | ✓ | round 3 新加：(a) `agent_template.project_ids` write requires owner/admin role on every bound project; (b) `skill.bundle_attachment_id` row-level deny for base.group_user; (c) anchor stamping ensures delegation target correct |

### 14.8 Hand-off updated

CAQA Round 3 verdict：**APPROVED WITH CAVEATS** — 維持，進 reporter phase（reviewer 已 close round；HC-3 / HC-4 close + 守 std rule symmetric deactivation 紅線）。

**Reviewer / Reporter special attention**：
- Phase A #8 invariant（HC-3 grantor authority + HC-4 attachment anchor + ir.rule）durable 到 decision doc — 建議 reporter phase 抽 pattern「self-owned attachment row-level gating」放 `docs/decisions/patterns/`（vs B1.SK-6 field-level mask 互補）
- design §4.1 應 reverse-document `_check_project_ids_grantor_authority` constraint method（task brief 標 「planner 字面化」— 確認 design 已 update）
- 12 follow-ups F-PA-1 ~ F-PA-12 不動；round 3 沒新 follow-up（all cell explicit cover）

**等 leader hand-off reporter**。
