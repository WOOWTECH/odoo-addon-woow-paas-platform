---
name: caqa-report-2026-05-20-mcp-refactor
phase: caqa
team: mcp-refactor
caqa: mcp-refactor-caqa
plan_ref: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
design_ref: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md
test_report_ref: docs/archived/tracking/test-coverage-mcp-refactor-2026-05-20.md
issue:
  - 49
  - 51
created: 2026-05-21
related:
  - docs/decisions/refactor/mcp-project-decouple.md
  - docs/decisions/patterns/mcp-acl-6-rule-split.md
  - docs/decisions/patterns/cloud-service-unlink-override-cleanup.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/tracking/pitfalls/system-mcp-create-acl-bypass.md
status: archived
archived-from: docs/tracking/caqa-report-2026-05-20-mcp-refactor.md
archived: 2026-07-19
archived-reason: "一次性 CAQA 驗收報告，cycle 已收官；patterns/pitfalls 證據錨點引用已改指本處"
---

# CAQA Report — MCP Refactor URL Redesign + Unbound Project ACL（issue #49 + #51）

## Verdict

**APPROVED WITH CAVEATS** — 不阻斷 reviewer phase。

四面向（Quality / Security / Performance / Architecture）皆 PASS；無 blocker，無 high-severity finding。共識 implementation 已對齊 plan 44 AC + design B 案 + test report 三 source-of-truth；OR-X9 ACL dual-leg 32-case 概念矩陣（實作為 12 test method H.1–H.12）落點精準守住 MAJOR 1 重炸防線。

**4 條 caveat 留 surface 給 reviewer / reporter**（不擋 merge）：cron `_cron_retry_mcp_sync` 對新 sidecar-backed row 留 gap、`_check_project_access` bound-but-no-workspace edge case、URL helper duplication（已 defer F19）、codebase-wide comment ref noise debt。

---

## 1. Invariants verify table

### PR #47 既有 8 條 invariants（見 `docs/decisions/refactor/mcp-project-decouple.md` §Invariants）

| Inv # | 主旨 | 本 PR 動作 | Verify | Evidence |
|-------|------|----------|--------|----------|
| #1 | `mcp_server.project_id` 必維持 `ondelete='cascade'` | **不動** | ✓ | `git diff` 對 `src/models/mcp_server.py` 內 `project_id` 欄位定義無改動 |
| #2 | D4：`scope='user' ⇔ project_id IS NOT NULL` | **不動** | ✓ | `mcp_server.py:111-129` D4 `_check_scope_project_consistency` 保留；`test_mcp_d5_removal.py:test_d4_user_scope_requires_project_id_after_refactor` + `test_d4_system_scope_forbids_project_id_after_refactor` 雙向鎖 |
| #3 | D5：user-scope MCP 必有 bound project | **整段刪除**（本 PR 核心動作） | ✓ | `mcp_server.py` `_check_user_scope_requires_bound_project` method body 完全移除（plan AC #1）；`test_mcp_d5_removal.py:test_d5_constraint_method_no_longer_exists_in_mcp_server_source` grep-style source check 鎖此 |
| #4 | `_do_cleanup` 順序：unlink user MCP → helm uninstall → state | **保留 + filter 改寫** | ✓ | `cloud_service.py:1067-1124` `_do_cleanup` 三步順序不變；filter 改成 `lambda s: s.scope == 'user' and s.sidecar_backed`（plan AC #21）；同 pattern 適用 `unlink()` override（AC #22）；`test_mcp_sidecar_backed_lifecycle.py` 4-method paired fixture（sidecar-backed unlink + external preserve）×2 路徑全鎖 |
| #5 | mcp_server + mcp_tool **6-rule** ACL split 對等存在 | **`workspace_user` × 2 改 dual-leg；其餘 4/6 不動** | ✓ | `ir_rules.xml` `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` domain 改 `'&', ('scope', '=', 'user'), '|', leg1, '&', leg2a, leg2b`（外層 AND 鎖 scope='user' 是 OR-X9 守線根基）；`system_readonly` × 2 + `admin_full` × 2 完全不動 ✓ |
| #6 | `mcp_server_rule_system_readonly` 對 `base.group_user` 永真 read leg + `perm_create=False` 是 invariant | **不動** | ✓ | `ir_rules.xml` `mcp_server_rule_system_readonly` + `mcp_tool_rule_system_readonly` 內 `perm_create=False, perm_write=False, perm_unlink=False` 全保留；`test_mcp_acl_unbound_isolation.py:H.11/H.12` 主動跑「Charlie ORM create scope='system' → AccessError」reproducer 鎖此 |
| #7 | `mcp_server.url` 為 snapshot + 演算法物理歸 service | **保留 helper + 改 invoking pattern** | ✓ | `cloud_service.py:_model_build_mcp_endpoint_url`（含算法本體）保留為 helper（plan AC #13）；`paas.py:_compute_sidecar_url_hint` 改名（從 `_build_mcp_endpoint_url`）+ drop redundant template param（/simplify 採納）；URL algorithm 不變 — `cluster.local` fallback / subdomain / localhost 三段不動 |
| #8 | System-scope MCP create 限 sudo / admin / demo data | **不動** | ✓ | `system_readonly` `perm_create=False` 仍 enforce；`workspace_user` 外層 AND `scope='user'` 鎖 create direction 不可建 system row（OR-X9 守線）；H.11 / H.12 雙 test 鎖此 |

### 新增 invariants（design 已敘事，本 PR 落地）

| Inv # | 主旨 | Verify | Evidence |
|-------|------|--------|----------|
| **#9** | sidecar-backed user-scope MCP 在 `_do_cleanup` + `unlink()` 兩條路徑被清掉；filter 必含 `sidecar_backed=True`；漏 filter → external MCP 被誤殺，漏整段 cleanup → sidecar 拆後 cluster URL zombie | ✓ | 雙路徑 filter 已對等改寫（`cloud_service.py:1086, 1138`）；`test_mcp_sidecar_backed_lifecycle.py` 4-method paired fixture 鎖兩路徑兩方向（sidecar-backed cleanup + external preserve）×（`_do_cleanup` + `unlink()`）|
| **#10** | UI shortcut「Add Sidecar MCP」create 時必須帶 `sidecar_backed=True`；endpoint 不接受繞 path | ✓ | `McpServersSection.js:submitForm` line 157 `payload.sidecar_backed = !!this.state.isSidecarFlow`；`_create_project_mcp_server` 接受 `params.get('sidecar_backed', False)` 寫入 ORM；`test_mcp_sidecar_hint_endpoint.py:test_create_with_sidecar_backed_true_persists_flag` + `..._false_persists_flag` 雙向鎖 |
| **#11** | `workspace_user` + `mcp_tool_workspace_user` rule domain 必須 dual-leg（bound + unbound carve-out via `message_partner_ids`）；任何把 leg 2 改 always-true / 拔 leg 2 / 改 single leg = ACL leak | ✓ | `ir_rules.xml:147-164, 196-211` dual-leg shape 正確（外層 AND `scope='user'` + 內層 OR leg1/leg2）；`test_mcp_acl_unbound_isolation.py` 12 method 全方位（4 project state × 2 sub-models × 4 perm direction 收斂）覆蓋 |

**Invariant verify 結論**：11 / 11 全 ✓，無破壞點。

---

## 2. OR-X9 32-case ACL Matrix 行為驗證

OR-X9 概念矩陣 = **4 project state × 2 sub-models × 4 perm direction = 32**。實作為 12 test method（test report §3 已敘事 — 矩陣 trivial-collapse case 用 method 命名 self-document 替代 parametrize）。本 CAQA 站 reviewer 角度跑「每條 method 對應到哪幾個 32-cell + 哪幾個 cell 沒 explicit lock 但 trivial-true / trivial-false」。

### Cell map（4 state × 2 sub-models × 4 perm direction）

State：`B = bound project` / `UF = unbound + follower` / `UN = unbound + non-follower` / `X = cross-workspace`
Direction：`R = read` / `W = write` / `C = create` / `U = unlink`
Sub-model：mcp_server（簡寫 `S`）、mcp_tool（簡寫 `T`）

| Cell | Expected | H.# | Cover 方式 |
|------|----------|-----|-----------|
| **B × S × R** (bound owner read) | allow | H.10 | explicit assertEqual(visible, bound_mcp) |
| **B × T × R** | allow | H.10（leg-1 regression lock 雙向通約） | implicit — leg 1 unchanged，PR #47 9-case 守 |
| **UF × S × R** | allow (Alice owner) | H.1 | explicit |
| **UF × S × R** | allow (Charlie follower) | H.2 | explicit |
| **UF × T × R** | allow (Charlie follower) | H.5 part-1 | explicit |
| **UN × S × R** | deny (Dave member, 非 follower) | H.3 | explicit |
| **UN × T × R** | deny (Dave) | H.5 part-2 | explicit |
| **X × S × R** | deny (Bob W2) | H.4 | explicit |
| **X × T × R** | deny (Bob) | H.6 | explicit |
| **B × S × W/U** | leg-1 walk preserve | PR #47 既有 regression（implicit） | regression lock,本 PR 不破壞 |
| **B × T × W/U** | 同上 sub-model 對等 | PR #47 既有 regression | implicit |
| **UF × S × W/U** | allow (per dual-leg) | implicit through ORM create allowed in H.1 setUp() + endpoint test H.9 | implicit — endpoint write path 在 `_create_project_mcp_server` |
| **UF × T × W/U** | allow | implicit |
| **UN × S × W/U** | deny | implicit — leg 2 walk 在 read direction 已 deny；write/unlink 同 domain 同 deny |
| **UN × T × W/U** | deny | implicit |
| **X × S × W/U** | deny | implicit |
| **X × T × W/U** | deny | implicit |
| **\* × S × C** for `scope='system'` | **deny base.group_user** | **H.11** | **explicit reproducer** — Charlie (W1 follower of unbound) 對 ORM `create({'scope': 'system'})` → AccessError |
| **\* × T × C** for `server_id.scope='system'` | **deny base.group_user** | **H.12** | **explicit reproducer** — Charlie 對 sudo-seeded system MCP 加 tool → AccessError |
| **\* × S × C** for `scope='user'` follower | allow | H.1 setUp() 隱含 | implicit |
| **\* × T × C** for follower path | allow | H.5 setUp() 隱含 | implicit |
| **UN × S × C** for `scope='user'` non-follower | deny | implicit — leg 2 walk fail；endpoint 層在 H.9 explicit |
| **X × S × C** for `scope='user'` cross-ws | deny | implicit |

### MAJOR 1 重炸防線評估

**核心威脅**：dual-leg domain shape 寫成 `'|', leg, '&', leg2_a, leg2_b`（scope filter 與 leg 2 用 OR 連），導致 new `scope='system'` row 在 leg 2 sub-leg2（`message_partner_ids`）為真時，整 OR 為真，rule allow create → 重炸 PR #47 round 1 system-mcp-create-acl-bypass。

**實作 shape**：`'&', ('scope', '=', 'user'), '|', leg1, '&', leg2_a, leg2_b`（**外層 AND 鎖 scope='user'**）—

- 對 new `scope='system'` row：外層 AND 第一項為 False → 整 domain False → block create（**leg 2 永不被評估**）
- 對 new `scope='user'` + bound project + workspace member：外層 AND OK，OR leg 1 walk True → allow
- 對 new `scope='user'` + unbound + follower：外層 AND OK，OR leg 2 (cloud_service_id=False ∧ follower) True → allow
- 對 new `scope='user'` + unbound + non-follower：外層 AND OK，OR 兩腿都 False → deny

**H.11 / H.12 explicit reproducer**：直接跑 `Charlie.create({'scope': 'system'})` → 必 AccessError。若 implementation 不慎把 dual-leg shape 寫成 OR-precedence 漏洞版，此 2 條會立刻 RED → MAJOR 1 重炸 alarm。

**結論**：dual-leg shape 對 4 perm direction × 4 project state × 2 sub-models 全方向行為**全部正確**；MAJOR 1 防線 verified。

---

## 3. OR-X 8 risk + 3 caveat closure review

| ID | Risk / Caveat | Status | Evidence |
|----|---------------|--------|----------|
| **OR-X1** | 既有 stg sandbox 部署 n8n service 既有 `auto_created=True` row 升 .25 後仍 functional | **closed** | Migration 18.0.1.0.25 idempotent UPDATE backfill `sidecar_backed=TRUE WHERE auto_created=TRUE AND sidecar_backed=FALSE`；既有 row 行為不變；`test_mcp_project_decouple_migration_25.py:test_migration_backfills_sidecar_backed_from_auto_created` + `test_migration_idempotent` 雙鎖 |
| **OR-X2** | n8n template 不動 + auto-create 拆 → new n8n service 不會 auto-create MCP，user 必須走 UI shortcut | **closed (留 reporter 處理 onboarding doc)** | `cloud_service.py` 3 個 call sites（849 / 914 / 940 對應到舊 line）全刪 + `_model_auto_create_mcp_server` method 整段刪；`controllers/paas.py` 同步刪 3 個 call sites + `_auto_create_mcp_server` method；frontend「Add Sidecar MCP」按鈕替代；`McpServersSection.js:showAddSidecarForm` 流程驗證 hint→prefill→submit |
| **OR-X3** | Frontend Add Sidecar MCP 按鈕 visibility 依賴 `template.mcp_enabled` 資訊；既有 `_format_project_detail` 是否包含？ | **closed**（implementation 已 surface） | `paas.py:_format_project_detail` 加 `cloud_service_mcp_enabled` payload boolean（implementation ack 「6 意外發現 #1」）；`McpServersSection.js:get canAddSidecarMcp` 使用此 field |
| **OR-X4** | `sidecar_backed` flag user-modifiable？form view 預設 readonly；admin override 是否保留 | **closed (read-only + admin override 保留)** | Field 定義 `readonly=True`；frontend 不暴露 toggle（只有點 button 時內部 set）；`admin_full` rule + sudo() bypass 保留 admin override 能力（plan 設計意圖）；endpoint `_create_project_mcp_server` 接受 `sidecar_backed` payload field 但 endpoint 故意不 enforce 一致性（plan AC #28 明文）|
| **OR-X5** | URL allowlist 範圍 minimal SSRF — 完整 SSRF 防禦留 #52 follow-up | **closed (留 reporter 標 F25 cluster partial coverage)** | `paas.py:_validate_mcp_url` 拒 169.254.169.254 + IPv6 link-local + fd00:ec2::254 + non-http(s) scheme；保留 cluster.local；`test_mcp_external_url_creation.py` 6 case 正反向鎖 |
| **OR-X6** | ~~Caveat 1 leader 拍板路徑~~ — 雙 wave resolve（OQ-3 + OQ-7 + OQ-8） | **resolved** | OQ-3 (a) 合 #51；OQ-7 ai_assistant.py 擴入 YES；OQ-8 OR-7 leg 2 = (c) message_partner_ids walk — 三者全落地 |
| **OR-X7** | PR #47 Inv #7「URL 是 snapshot」未動，service rename / namespace 變動後 URL stale | **承襲 PR #47 §Risk #4 caveat — out of scope** | URL algorithm 不變；本 PR 不主動處理 stale URL 問題（留 future） |
| **OR-X8** | `_compute_sidecar_url_hint` (controller) + `_model_build_mcp_endpoint_url` (model) algorithm 重複 | **deferred to F19 follow-up（plan 明文）** | 兩 method 演算法仍重複；`cloud_service.py:957-961` comment 已標 「kept for the cron / non-request callers; see docs/decisions for the duplication follow-up」 |
| **OR-X9** | ACL domain 改動 blast radius — 32 cases ACL regression matrix | **closed** | dual-leg shape 正確；`test_mcp_acl_unbound_isolation.py` 12 method 覆蓋 — 詳見上節 §2 cell map |

### Design Caveat closure

| Caveat | Status | Notes |
|--------|--------|-------|
| **Caveat 1** (ACL domain dual-leg blast radius) | **closed** | 合 #51 後升級為 OR-X9 — 12 H-group test 全覆蓋 |
| **Caveat 2** (`auto_created` rename) | **resolved → OQ-2 = (b) keep deprecated** | implementation ack 明示 grep 顯示 (a) rename 會炸 10+ regression test → leader 拍板 (b)；migration 只 UPDATE backfill 不 DROP COLUMN；field 標 deprecation note |
| **Caveat 3** (URL allowlist 範圍) | **resolved → OQ-4 = minimal SSRF check** | `_validate_mcp_url` 5-condition reject + 1-condition accept；cluster.local 必須 allow（sidecar-backed URL 形態）|

---

## 4. 四面向分析

### Quality

**44 AC alignment**：逐條 mapping（plan §AC #1–#44）對應 implementation diff，全部 covered：

- AC #1（D5 整段刪除）→ `mcp_server.py` `_check_user_scope_requires_bound_project` 移除 ✓
- AC #2（`sidecar_backed` 新增 boolean）→ `mcp_server.py:97-103` 新欄位 ✓
- AC #3, #35, #36（Migration .25 idempotent backfill）→ `migrations/18.0.1.0.25/post-migrate.py` ✓
- AC #4（`auto_created` 處置：OQ-2 leader 拍 (b) keep deprecated）→ field 標 `string='Auto Created (deprecated)'` + help 「Deprecated; use sidecar_backed for new code paths」 ✓
- AC #5（D4 不動）→ verified via `test_mcp_d5_removal.py:test_d4_*` 雙向 ✓
- AC #6（unbound user-scope MCP allowed）→ via D5 拔除自然達成 ✓
- AC #7-#12（auto-create 兩路 6 call sites + 2 method 全刪）→ `cloud_service.py` 3 處 + `paas.py` 3 處 call sites 全刪；`_model_auto_create_mcp_server` + `_auto_create_mcp_server` method 整段刪 ✓
- AC #13（`_model_build_mcp_endpoint_url` 保留為 helper）→ `cloud_service.py:962` 保留 ✓
- AC #14（`_build_mcp_endpoint_url` 改名 `_compute_sidecar_url_hint`）→ `paas.py:2640` 改名 + drop redundant template param（/simplify 採納）✓
- AC #15（URL algorithm 不變）→ `paas.py:_compute_sidecar_url_hint` 內 cluster.local / subdomain / localhost 三段邏輯保留 ✓
- AC #16-#20（sidecar 物理機制 / cloud_app_template 7 fields / paas-operator / demo data 全不動）→ diff 無變動 ✓
- AC #21, #22, #23, #24（cleanup hook + unlink override filter 改 + PR #47 fixture conditional 餵料）→ `cloud_service.py:1086, 1138` 雙 filter 改成 `lambda s: s.scope == 'user' and s.sidecar_backed`；`test_mcp_project_lifecycle.py:_make_service_with_project_and_mcp` 條件式餵 `sidecar_backed=True`（when field exists）✓
- AC #25（OR-4 endpoint manual D5 query 整段刪 + R-4 不補）→ `paas.py:_update_support_project` line 1528-1547 已刪；`test_project_detail_endpoints.py:TestProjectDetailUnbindOr4Cleanup` 3-case 鎖（external 成功 + sidecar_backed 成功 + grep source 「lingering」字串無）✓
- AC #26（R-4 不補）→ verified — `project.project` 無新增 `@api.constrains` ✓
- AC #27, #28, #29（sidecar-hint endpoint + ACL gate + payload field）→ `paas.py:_compute_project_sidecar_hint` + `write_actions` 加 `sidecar-hint`；`test_mcp_sidecar_hint_endpoint.py` 9-case 全鎖 ✓
- AC #30, #31, #32（SSRF minimal check + 統一錯誤訊息 + stdlib only）→ `paas.py:_validate_mcp_url` module-level helper（urllib.parse + ipaddress）；`_create_project_mcp_server` + `_update_project_mcp_server` 雙 call site；`test_mcp_external_url_creation.py` 6-case 正反鎖 ✓
- AC #33, #34（Frontend 拆兩按鈕 + service method）→ `McpServersSection.{js,xml}` `canAddSidecarMcp` getter + `showAddSidecarForm` async prefill + `showAddExternalForm` 空表單；`support_service.js:fetchSidecarHint` ✓
- AC #37（version bump .24 → .25）→ `__manifest__.py` 1.0.25 ✓
- AC #38, #39, #40, #41（ir.rule dual-leg + 4 條不動 + R-4 不補）→ `ir_rules.xml` workspace_user × 2 改 dual-leg；system_readonly × 2 + admin_full × 2 不動 ✓
- AC #42（`ai_assistant._check_project_access` unbound carve-out 擴入）→ `controllers/ai_assistant.py:44-58` 改 `if not project.cloud_service_id: return user_partner in project.message_partner_ids` ✓
- AC #43（task path / list path 不擴）→ diff 無 `_check_task_access` / `_list_projects` 改動 ✓
- AC #44（不動 `discuss_channel.py` / `ai_assistant._get_user_mcp_tools_for_channel` / `mcp_server_views.xml` 主 layout / `_check_task_access` / `_list_projects`）→ diff 無相關改動 ✓

**Result**：44 AC alignment **100%**，無漏網。

**Comment ref noise cleanup**：implementation /simplify pass 已局部清除本 PR 引入的 plan/OQ/OR ref。grep verify `Plan AC|OQ-[0-9]|OR-X[0-9]|mcp-refactor-url-redesign|2026-05-20|合 #51|New Inv #` 在 production code（非 tests）僅 4 處 hit，均為**過去 PR（PR #47 / PR #53）留下的 maintenance debt**，不是本 PR 引入：

- `ir_rules.xml:251`「CAQA B-1 (re-loop 2026-05-20)」— PR #53 留下
- `support_service.js:454`「Plan AC #17 / #21」— PR #53 留下
- `McpServersTab.js:12`「Plan AC #22 / #24」— PR #47 / #53 留下
- `html_sanitize.js:26`「CAQA C-3, re-loop 2026-05-20」— PR #53 留下

**Result**：本 PR 已 clean；codebase-wide debt 留 reporter 標 follow-up（implementation ack 「6 意外發現 #6」對齊）。

**/simplify 採納 / 拒絕 reasoning 評估**：

| 採納 / 拒絕 | 動作 | Reasoning sound? |
|------------|-----|------------------|
| 採納 | drop `_compute_sidecar_url_hint(self, service, template)` 中 template param | ✓ template 可從 `service.template_id` 取，無資訊損失 |
| 採納 | comment noise cleanup (Plan AC / OQ / OR refs) | ✓ 不影響 production 邏輯，rotting comment 是 maintenance debt 來源 |
| 採納 | changelog comment 刪除 (B 案 / PR #47 references) | ✓ 同上 |
| 採納 | migration docstring 簡潔化 | ✓ 保留 rationale (`auto_created kept because legacy test suite still seeds rows; backfill idempotent`) |
| 採納 | `mcp_server.py` `auto_created` field help 一行 | ✓ deprecation 一行夠表達 |
| 採納 | `ai_assistant` docstring drop OR-7/OQ-7/OQ-8 refs | ✓ 改強調 behavioural intent + two-layer ACL consistency — 維護價值更高 |
| **拒絕** | 不刪 `_model_build_mcp_endpoint_url`（agent 建議刪 dead code）| ✓ plan AC #13 明文「保留」；agent 不知 plan context — 拒絕正確 |
| **拒絕** | 不抽 `_column_exists` 到共用 `_utils.py` | ✓ migration files 慣例獨立；本 PR scope 不擴 |
| **拒絕** | 不統一 `_resolve_project_access` vs `_check_project_access` | ✓ plan AC #42 explicit scope 只擴 `_check_project_access`；task / list path 留 F4 / F10 follow-up — 拒絕正確 |
| **拒絕** | 不抽 `isSidecarFlow` state（agent 認為可 derive）| ✓ URL/flag 一致性故意不 enforce（plan AC #28 明文）；agent 誤解 design intent — 拒絕正確 |
| **拒絕** | 不抽 action name 為 enum const | ✓ Odoo 既有 idiom raw string；不引入新 abstraction — 拒絕正確 |

**Result**：/simplify reasoning sound — 採納全部合理（不違反 plan、不引入新 risk），拒絕全部都有 plan AC / design intent 引用 — implementation 沒被 agent 誤導偏離 plan。

**Cron `_cron_retry_mcp_sync` 仍 query `auto_created=True`**：

- 現狀（`mcp_server.py:214-217`）：cron 仍 `search([('auto_created', '=', True), ('state', '=', 'draft')])`
- 行為差異：
  - **Legacy row**（PR #47 既有 `auto_created=True` AND `sidecar_backed=True`）→ cron 仍 retry sync ✓
  - **新 sidecar-backed row**（UI shortcut 創建，`auto_created=False` AND `sidecar_backed=True`）→ cron **不會** retry sync ✗
  - **External row**（user 自填 URL，`auto_created=False` AND `sidecar_backed=False`）→ cron 不掃（design intent — external 由 user 自管理）✓
- 風險評估：
  - implementation ack 解釋「sidecar 已 ready 時 user 才點 button,理論上不需 cron retry」
  - **但實際上**：sidecar K8s service object / Cloudflare route 啟動可能 lag；user 點按鈕後 first sync 可能失敗（state='draft', sync_retry_count=1），此時 cron 不掃 → user 必須手動點 sync。比 PR #47 auto-create 行為差（PR #47 下 auto-create 走 `action_sync_tools_safe()` + cron retry 雙保險）
  - **嚴重度**：UX caveat（user 可手動 retry），不是 data loss / security risk
- **建議**：標為 **caveat / F follow-up**（後續 PR 把 cron query 改 `sidecar_backed` 或廢除 cron），不擋 merge

### Security

**重點 1：OR-X9 32-case ACL blast radius matrix** — 詳見 §2，dual-leg shape 對 4 perm direction × 4 project state × 2 sub-models 全方向行為正確；H.11 / H.12 主動 reproducer 鎖 MAJOR 1 重炸防線。

**重點 2：MAJOR 1 重炸防線（H.11 / H.12 雙鎖）**：

- 外層 AND `'&', ('scope', '=', 'user'), ...` 是核心守線 — scope filter 在 OR 連結之外
- H.11 explicit：Charlie (W1 member + unbound project follower) ORM `create({'scope': 'system'})` → 必 AccessError
- H.12 explicit：sub-model 對等 — Charlie 對 sudo-seeded system MCP ORM create mcp_tool → 必 AccessError
- Sub-model 對等性（`server_id.` 前綴）正確 — `mcp_tool_rule_workspace_user` 外層 AND 是 `server_id.scope='user'`

**結論**：MAJOR 1 防線 verified ✓ — 與 `mcp-acl-6-rule-split.md` pattern + `system-mcp-create-acl-bypass.md` pitfall 完全對齊。

**重點 3：SSRF check 完整性**：

- `_validate_mcp_url`（`paas.py:30-65`）攻擊面 coverage：
  - ✓ Scheme allowlist：拒 `ftp://` / `file://` / `gopher://` 等（只 accept `http` / `https`）— `test_url_rejects_unsupported_scheme`
  - ✓ IPv4 metadata：拒 `169.254.169.254`（AWS / GCP / Azure IMDSv1）— `test_url_rejects_aws_metadata_ipv4`
  - ✓ IPv6 link-local：拒 `fe80::/10`（`ipaddress.IPv6Address.is_link_local`）— `test_url_rejects_ipv6_link_local`
  - ✓ IPv6 IMDS：拒 `fd00:ec2::254`（AWS IMDSv2 IPv6）— `test_url_rejects_imds_ipv6`
  - ✓ 正向 boundary：`https://mcp.example.com/sse` + `http://*.svc.cluster.local:*/mcp` 允許 — 2 條 GREEN regression lock 鎖
- **未 cover 但 by design**（design Caveat 3 / OQ-4）：
  - RFC1918（10/8、192.168/16、172.16/12）— cluster.local 解析到 10.x pod IP，blanket ban 會破壞 sidecar workflow；F25 / #52 follow-up
  - 0.0.0.0 / 127.0.0.0/8 — plan AC 未列入；F25 follow-up
  - DNS rebinding — 完整 SSRF 留 #52
- **double-check**：`urllib.parse.urlparse` 對 `http://[fe80::1]/` 解析後 `parsed.hostname` 是 `fe80::1`（無 brackets）— implementation 用 `ip = ipaddress.ip_address(host)` parse + `is_link_local` 判斷正確
- **缺點**：error message 對 IPv6 用 `[{host}]` 格式（line 60, 64），但對 IPv4 用 raw host — 一致性微差但不影響 security

**結論**：SSRF coverage 對齊 plan AC #30 minimal scope；未 cover 部分由 plan / design 明文 defer #52 follow-up。

**重點 4：ACL 兩層對齊（ir.rule leg 2 + `_check_project_access` carve-out）**：

- `ir.rule` `mcp_server_rule_workspace_user` leg 2：`('project_id.cloud_service_id', '=', False) AND ('project_id.message_partner_ids', 'in', [user.partner_id.id])`
- `ai_assistant.py:_check_project_access`：`if not project.cloud_service_id: return user_partner in project.message_partner_ids`
- **兩層 domain 對齊** ✓ — 防 PR #47 round 2 教訓「IDOR partial fix」復活
- **未擴的 path**（plan AC #43 / #44 明文 out of scope）：
  - `_check_task_access`（F4 task path）— 仍 leaky；本 PR 不擴
  - `_list_projects`（F10 list path）— 仍 leaky；本 PR 不擴
- **caveat**：`ai_assistant.py:53-55` 仍保留 `if not project.cloud_service_id.workspace_id: return True` 對 bound-service-but-no-workspace edge case — 屬於 pre-existing path（改前 `if not project.cloud_service_id or not project.cloud_service_id.workspace_id: return True`），本 PR 不擴大也不收緊；實際 PaaS 平台幾乎不會發生 bound service 沒 workspace_id

**結論**：兩層對齊 ✓；task / list path 留 follow-up（plan 明文 out of scope）；bound-but-no-workspace edge case 屬於 pre-existing caveat — 標 follow-up 不擋 merge

**重點 5：Caveat 1 closure — 合 #51 ACL dual-leg 真的 close unbound external MCP visibility?**

- 合 #51 前：D5 禁止 user-scope MCP 掛 unbound project → external MCP 對 unbound project 整段 unavailable
- 合 #51 後：D5 拔 + dual-leg domain leg 2 carve-out → external MCP **可** 掛 unbound project，**僅 follower** 可讀
- H.1 (Alice owner 可讀) + H.2 (Charlie follower 可讀) + H.3 (Dave non-follower 不可讀) + H.4 (Bob cross-ws 不可讀) — 四方向全鎖
- H.5 / H.6 mcp_tool sub-model 對等
- H.7 / H.8 / H.9 `_check_project_access` controller 兩層對齊
- **結論**：Caveat 1 ACL closure verified ✓

**重點 6：sidecar_backed admin override (OR-X4)**：

- Field `readonly=True` + ORM-level — 阻擋 base.group_user 對 form view 改 toggle
- `admin_full` rule 對 `group_system` `[(1, '=', 1)]` + `perm_write=True` → admin sudo backend 可改
- frontend 無暴露 toggle — UI shortcut 流程內部 set `payload.sidecar_backed = !!isSidecarFlow`，user 看不到 raw toggle
- Endpoint `_create_project_mcp_server` 接受 `params.get('sidecar_backed', False)` 寫入 — 但 plan AC #28 明文「**故意不** enforce URL ↔ flag 一致性」frontend 負責 mental model
- **結論**：admin override 設計合理 — 後台維護 + ORM bypass 路徑保留，frontend 不暴露繞 path

**重點 7：sidecar-hint endpoint ACL**：

- `paas.py:1609` `write_actions = ('create', 'update', 'delete', 'sync', 'test', 'sidecar-hint')` — sidecar-hint 套同 owner/admin gate
- non-member（Eve）→ 統一 `Project not found or access denied`（test report §7 #3 regression lock 鎖 id enumeration 防護）
- 'user' role（Dave）→ `Permission denied`（與 create action 同層級）
- 既有 PR #53 `Project not found or access denied` 統一 enum 對齊 ✓
- **結論**：sidecar-hint ACL 對齊 plan AC #29 + PR #53 設計 ✓

### Performance

**Migration .25 idempotent + 範圍評估**：

- SQL：`UPDATE woow_paas_platform_mcp_server SET sidecar_backed = TRUE WHERE auto_created = TRUE AND sidecar_backed = FALSE`
- WHERE clause 兩個 condition：
  - `auto_created = TRUE`：filter 既有 auto-create row（n8n template 觸發的 user-scope MCP）
  - `sidecar_backed = FALSE`：idempotent guard（重跑時 row 已 backfill → 不重複寫）
- Index 利用：實際依賴 PostgreSQL planner — 若 `auto_created` 沒 index，全表掃描；但 PR #47 引入欄位時也沒加 index
- 規模評估：stg / dev sandbox row count 預計 < 100；prod 無 data — full table scan 成本可忽略
- `_column_exists` guard 防 schema drift（drop column 後第二次跑 noop）
- **結論**：Migration idempotent + 範圍合理；無 performance blocker

**OR-X8 `_compute_sidecar_url_hint` vs `_model_build_mcp_endpoint_url` 重複**：

- 兩個 method algorithm 完全重複（cluster.local / subdomain / localhost fallback）
- `cloud_service.py:_model_build_mcp_endpoint_url` 給 model context 用（cron 等 non-request caller）— PR #47 留下
- `paas.py:_compute_sidecar_url_hint` 給 controller context 用（sidecar-hint endpoint）— 本 PR 改名（從 `_build_mcp_endpoint_url`）
- 短期 cost：duplication maintenance debt — 若 algorithm 需 update（如加新 fallback），兩 method 需同步改
- Plan 明文 defer F19 follow-up；implementation 已在 `cloud_service.py:957-961` 加 comment 解釋 dual-helper 設計
- **結論**：performance cost 可忽略（兩 method 都不在 hot path）；maintenance cost 由 F19 cluster code clarity follow-up 承接

**Frontend `Add Sidecar MCP` visibility — `cloud_service_mcp_enabled` payload 擴展**：

- `_format_project_detail` 加 `'cloud_service_mcp_enabled': bool(service and service.template_id.mcp_enabled)` — 額外一個 boolean payload，序列化成本可忽略
- Caller 影響：`McpServersSection.js:get canAddSidecarMcp` 使用；其他 caller（如 service tab）目前不用此 field，無 side effect
- **結論**：payload 擴展 cost 可忽略

### Architecture

**8 invariants + 2 new invariants verification table**：見 §1

**6 意外發現處理完整性**（對齊 implementation ack）：

1. **OR-X3 endpoint payload (`cloud_service_mcp_enabled`)** — implementation 已在 `_format_project_detail` 加 ✓
2. **OR-X4 sidecar_backed admin override** — 設計保留，diff 對齊 plan caveat（無 enforcement，frontend 不暴露）✓
3. **OR-X8 URL helper duplication** — plan 明文 defer F19；implementation 加 comment 解釋 ✓
4. **cron `_cron_retry_mcp_sync` 仍 query `auto_created`** — 兼容 legacy fixture；implementation ack 已 surface；**留 caveat / F follow-up**（見 Quality 段細節）
5. **OQ-2 (b) implementation-driven 決策** — grep 顯示 10+ regression test 引用 `auto_created`，leader 拍 (b) keep deprecated；migration 只 UPDATE 不 DROP COLUMN ✓
6. **/simplify pass: comment ref noise 普遍 maintenance debt** — implementation ack 已 surface；4 處 production hit 全為過去 PR 留下；**留 reporter 標 codebase-wide cleanup follow-up**

**Architectural drift 評估**：

- Schema 解耦完成度：D5 拔 + dual-leg ACL + sidecar_backed flag = user-scope MCP 在 schema **與 ACL 兩平面**完全 decouple from cloud_service 物理綁定
- B 案 hybrid 模型清晰：sidecar 物理仍存（template.mcp_enabled / patch_sidecar）+ sidecar_backed flag 標 row 物理依賴 + dual-leg ACL 守 unbound external MCP visibility — 三層 contract 全 explicit
- 對比 A 案（純 user-driven）優勢：n8n out-of-box AI 整合不破；migration cost 最低
- 對比 C 案（mcp template 拆獨立）優勢：no cross-service binding 新概念引入；UX 摩擦最低

**結論**：Architecture coherent — invariant 全守，hybrid 模型有明確分層；無 drift

---

## 5. High-severity findings

**無**。

無 blocker / 無 MAJOR finding。implementation 對齊 plan + design + test report 三 source-of-truth，OR-X9 守線（H.11 / H.12）有 explicit reproducer test 鎖 MAJOR 1 重炸防線。

---

## 6. Medium / Low findings

### Medium

| ID | Finding | Location | Severity | 建議 |
|----|---------|----------|----------|------|
| M1 | `_cron_retry_mcp_sync` 仍 query `auto_created=True` — 新 sidecar-backed row（UI shortcut 創建）`auto_created=False` → cron 不掃 → first sync 失敗時 user 必須手動 retry（PR #47 雙保險 fallback 失效） | `src/models/mcp_server.py:214-217` | Medium（UX caveat，非 security / data loss） | F follow-up：cron query 改 `sidecar_backed` 或廢除 cron（user UI 已有 sync button） |
| M2 | `_check_project_access` bound service + service.workspace_id IS NULL 走 `return True` blanket allow — pre-existing edge case，本 PR 不擴大 / 不收緊 | `src/controllers/ai_assistant.py:54-55` | Medium（pre-existing，非本 PR 引入；實際 PaaS 平台 cloud_service 創建時要 workspace_id，case 罕見）| F follow-up：與 F4 / F10 一起整理 ai_assistant.py controller 整體 IDOR cleanup |

### Low

| ID | Finding | Location | Severity | 建議 |
|----|---------|----------|----------|------|
| L1 | Codebase-wide comment ref noise — 4 處 production code 殘留過去 PR（PR #47 / PR #53）的 plan/OQ/CAQA refs | `ir_rules.xml:251`, `support_service.js:454`, `McpServersTab.js:12`, `html_sanitize.js:26` | Low（maintenance debt，非本 PR 引入）| reporter follow-up：codebase-wide cleanup（implementation ack 已 surface）|
| L2 | OR-X8 URL helper duplication — `_compute_sidecar_url_hint` (controller) + `_model_build_mcp_endpoint_url` (model) algorithm 重複 | `paas.py:2640`, `cloud_service.py:962` | Low（plan 明文 defer）| F19 follow-up（plan §OR-X8 已敘事）|
| L3 | SSRF error message IPv6 vs IPv4 格式微不一致（IPv6 用 `[{host}]`、IPv4 raw）| `paas.py:_validate_mcp_url:60, 64` | Low（不影響 security）| 可選 — 後續 PR 統一格式（如統一用 `repr(host)` 或統一加 brackets）|
| L4 | `test_mcp_d5_removal.py:test_d4_user_scope_requires_project_id_after_refactor` 註解寫「Note: today the D5 constraint method is also live」但 post-impl D5 已拔 — 註解 slight stale | `src/tests/test_mcp_d5_removal.py:194-200` | Low（comment-only）| Reporter follow-up 順手清 |

---

## 7. Caveat list（給 reporter 標 follow-up）

1. **F-cluster: cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap**（M1）— 預計 dedicated PR 改 cron query 或廢除 cron
2. **F4 / F10 / F-cluster: `ai_assistant.py` controller IDOR cleanup**（M2 + plan AC #43 / #44 out-of-scope）— 整理 `_check_task_access` + `_list_projects` + `_check_project_access` bound-but-no-workspace edge case
3. **F25 / #52: 完整 SSRF 加固** — DNS rebinding / RFC1918 internal network filter（design Caveat 3 / OQ-4 明文 defer）
4. **F19: URL helper duplication 收乾**（L2）— `_compute_sidecar_url_hint` vs `_model_build_mcp_endpoint_url` algorithm 統一
5. **Codebase-wide comment ref noise cleanup**（L1）— 4 處 production code 殘留 + tests/ 內可能更多
6. **OR-X2 onboarding doc update**（reporter）— n8n template 新 onboarding flow：service deploy → user 點「Add Sidecar MCP」按鈕（不再 auto-create）
7. **PR description draft 必含**：ACL OR-X9 blast radius 摘要 + AC #42 `_check_project_access` 已修 project path / task+list path follow-up 標記

---

## 8. Plan alignment check

Implementation 完全沿 plan 拍板的 B 案方向走 — 沒有走偏：

- Plan B 案核心 = 保留 sidecar 物理 + 不自動建 MCP record + UI shortcut + sidecar_backed flag
- Implementation:
  - sidecar 物理機制保留（`_build_mcp_sidecar_config` / `patch_sidecar` / cloud_app_template 7 fields 全不動）✓
  - auto-create 拆（cloud_service.py 3 calls + paas.py 3 calls + 兩 method 全刪）✓
  - UI shortcut（McpServersSection 拆兩按鈕 + sidecar-hint endpoint）✓
  - `sidecar_backed` 新欄位 + dual-leg ACL + ai_assistant carve-out ✓
- OQ-2 (b) keep deprecated `auto_created` 是 implementation-driven 決策（grep 顯示 (a) rename 會炸 10+ regression test），符合 plan 「grep > 5 cross-module 回退 deprecated」自由度

**結論**：implementation 路線正確，無 plan 漏洞 / 無走偏 / 無 unexpected scope creep。

---

## 9. 同源 code path 檢查

對本 PR 改動的 pattern 跨 file grep verify「沒漏網」：

| Pattern | grep 結果 | 評估 |
|---------|----------|------|
| `_model_auto_create_mcp_server` / `_auto_create_mcp_server` | 0 hit in `src/models/cloud_service.py` 與 `src/controllers/paas.py`（method 整段刪 + call sites 全刪）| ✓ 對齊 plan AC #7-#10 |
| `scope == 'user' and not s.sidecar_backed`（誤判方向）| 0 hit | ✓ filter 寫法正確（`and s.sidecar_backed` 而非 `and not`）|
| `scope == 'user'` cleanup filter without `sidecar_backed` gate | 0 hit in `src/models/cloud_service.py`（兩處 filter 已加 gate）| ✓ |
| D5 manual query `lingering = project.mcp_server_ids`（plan AC #25 要求刪）| 0 hit in `paas.py` | ✓ |
| `_check_user_scope_requires_bound_project` | 0 hit in production code（保留在 test grep-check 字串中）| ✓ |
| `_validate_mcp_url` 是否 call 在所有 user-facing URL write entry | `_create_project_mcp_server` + `_update_project_mcp_server` 兩處 call ✓；其他可寫 url 的 path（form view backend admin）走 admin_full rule bypass — 設計允許 admin override | ✓ |
| dual-leg domain shape 寫對 `'&', scope, '|', leg1, leg2`（外層 AND 鎖 scope）| `ir_rules.xml` 兩處（mcp_server + mcp_tool）shape 一致 | ✓ MAJOR 1 守線根基 |

**結論**：同源 code path 全 clean — 沒漏網的「同 root cause 但沒改」處。

---

## 10. Known pitfalls 對照

對照本 repo `docs/tracking/pitfalls/`，本 PR 重蹈跡象：

| Pitfall | 重蹈跡象 | 評估 |
|---------|---------|------|
| [[cross-workspace-task-data-leak]] (B1) | ACL 改動 — sub-model 對等性必須守 | ✓ `mcp_tool_workspace_user` 同步改 dual-leg；H.5 / H.6 / H.12 sub-model parity test 鎖 |
| [[system-mcp-create-acl-bypass]] (PR #47 MAJOR 1) | dual-leg domain create direction privilege escalation | ✓ 外層 AND `scope='user'` 鎖；H.11 / H.12 explicit reproducer 鎖 |
| [[direct-unlink-bypass-cleanup-hook]] (PR #47 BLOCKER 2) | `unlink()` override filter 改寫風險 | ✓ AC #22 `unlink()` override filter 同步加 `sidecar_backed` gate；AC #24 BLOCKER 2 既有 fixture 條件式餵 `sidecar_backed=True` 維持 9-case 既有 regression 全綠 |
| [[plan-vs-odoo-schema-mismatch]] | plan 引用不存在欄位 | ✓ plan 引用的 `mcp_server.sidecar_backed`、`project.message_partner_ids`、`workspace.access_ids.user_id` 全存在 |
| [[tag-scoped-test-misses-full-u-failures]] | CAQA 用 tag-scoped 驗綠漏抓全 suite | implementation ack 報 558 tests / 0 MCP fail / 36 baseline 紅持平 — 全 suite 跑，不是 tag-scoped；無重蹈跡象 |

**結論**：無重蹈 pitfall 跡象 ✓

---

## 11. 工具 raw 輸出摘要

本 CAQA 為快速並聯模式（受限 maestri agent 無 implementation phase 直連），未 dispatch sub-agents（`/sc:analyze` / code-analyzer / architect-reviewer / security-auditor）— 改採人工 cross-reference + grep + git diff inspection 完成四面向 verify。

依賴材料：
- Plan：`docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md`（44 AC + 9 OR-X + Sign-off log + Test scope A-H 段）
- Design：`docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md`（B 案 invariant 改動敘事 + 3 caveat closure status + OR-7 三 options brainstorm）
- Test report：`docs/archived/tracking/test-coverage-mcp-refactor-2026-05-20.md`（10 章 + OR-X9 矩陣 + 3 條 GREEN regression lock）
- Implementation ack（leader routing）：6 step 落地 + /simplify 採納/拒絕 + 6 意外發現 + 558 tests / 0 MCP fail
- PR #47 invariants：`docs/decisions/refactor/mcp-project-decouple.md`（8 invariants + Round 2 改動敘事）
- Patterns：`docs/decisions/patterns/cloud-service-unlink-override-cleanup.md` + `mcp-acl-6-rule-split.md`
- Pitfalls：`docs/tracking/pitfalls/cross-workspace-task-data-leak.md` + `system-mcp-create-acl-bypass.md` + `direct-unlink-bypass-cleanup-hook.md` + `plan-vs-odoo-schema-mismatch.md` + `tag-scoped-test-misses-full-u-failures.md`
- Git diff scope：`git diff HEAD`（develop@969479c → working tree）— 14 files modified + 5 files added（含 migration .25 + 5 新 test files）

掃描範圍：
- `src/__manifest__.py`（version bump）
- `src/models/mcp_server.py`（D5 刪 + sidecar_backed 加 + auto_created deprecate）
- `src/models/cloud_service.py`（auto-create 拆 + filter 改寫 + URL helper 保留）
- `src/controllers/paas.py`（auto-create 拆 + OR-4 cleanup + sidecar-hint endpoint + SSRF check + 改名 + payload 擴展）
- `src/controllers/ai_assistant.py`（_check_project_access carve-out）
- `src/security/ir_rules.xml`（dual-leg domain × 2）
- `src/static/src/paas/pages/project-detail/sections/McpServersSection.{js,xml}`（拆兩按鈕 + sidecar flow）
- `src/static/src/paas/services/support_service.js`（fetchSidecarHint）
- `src/migrations/18.0.1.0.25/post-migrate.py`（新檔 — backfill + idempotent）
- `src/tests/__init__.py`（+6 new test module imports）
- 6 新 test files + 4 existing test files diff（lifecycle / project-detail / project-mcp-centric / integration）

---

## 12. Blocker 清單

**無**。

---

## 13. Sign-off

CAQA verdict：**APPROVED WITH CAVEATS** — 進入 reviewer phase。

Caveat 範圍（reporter 階段接手）：
1. M1 cron `auto_created` query gap（F follow-up）
2. M2 `_check_project_access` bound-but-no-workspace edge case（F4 / F10 cluster follow-up）
3. L1-L4 maintenance debt（reporter 收乾）

Reviewer special attention（per plan §Implementation phasing #6）:
- **OR-X9 ACL dual-leg domain blast radius** — 5-agent split：code-reviewer 看 domain shape / pr-test-analyzer 跑 32-case matrix 覆蓋 / silent-failure-hunter 看 ai_assistant carve-out 是否 silently 對 task path 漏掉 / type-design-analyzer 評估 dual-leg shape vs `'|', leg1, '&', leg2_a, leg2_b` AND/OR precedence / comment-analyzer 對 [[mcp-acl-6-rule-split]] doc 是否需要 update
