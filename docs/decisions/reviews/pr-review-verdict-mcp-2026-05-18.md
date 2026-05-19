---
name: pr-review-verdict-mcp-2026-05-18
phase: review
team: refactor
reviewer: refactor-reviewer
plan_ref: docs/refactor/plan-mcp-project-decouple.md
decision_ref: docs/refactor/decisions/mcp-project-decouple.md
test_scope_ref: docs/refactor/test-scope-report-mcp-2026-05-18.md
test_coverage_ref: docs/refactor/test-coverage-mcp-project-decouple.md
caqa_ref: docs/refactor/caqa-2026-05-18.md
diff_range: 5c5d980^..90ee0eb (8 commits; excl. 45afbbe k8s-dev-sandbox unrelated)
created: 2026-05-18T14:22:20Z
updated: 2026-05-18T14:22:20Z
status: needs-revision
---

# PR Review Verdict — MCP `cloud_service_id → project_id` Decouple

> 對象：phase-leader / implementation / tester / planner / reporter。Reviewer 透過 `/pr-review-toolkit:review-pr` 跑 5 個 sub-agent（code-reviewer / pr-test-analyzer / silent-failure-hunter / type-design-analyzer / comment-analyzer）平行 review，補抓 CAQA 報告 [[caqa-2026-05-18]] 明文排除範圍（tests/ + migrations/ + service.unlink() 全 path）的盲點。

## Verdict

**needs revision** — 2 個新 BLOCKER（CAQA 漏抓）+ 2 個 MAJOR + 17 個 minor / info。退回 **mixed phase**（tester + implementation + planner 各自有事）。

CAQA「APPROVED WITH CAVEATS」的判定本身**未涵蓋 reviewer 視角的兩個盲點**：tests/ 與 controller direct-unlink 路徑。CAQA §同源 code path 檢查（line 219-234）明文排除 tests/ 與 migrations/，因此漏抓 stale schema。CAQA Plan-alignment 接受 paas.py 7 個 endpoint（line 153-165），但沒檢查 5 個 `service.unlink()` 呼叫點是否全經 `_do_cleanup`。

## 工具 raw 輸出摘要

5 個 specialized sub-agent 平行跑（`/pr-review-toolkit:review-pr` 委派）。Multi-agent consensus 高：

| Agent | 主要產出 | 衝突？ |
|---|---|---|
| `pr-review-toolkit:code-reviewer` | 2 blocker + 1 major + 4 minor | 無 — 與 pr-test-analyzer 對 BLOCKER 1 雙簽，與 silent-failure-hunter 對 BLOCKER 2 雙簽 |
| `pr-review-toolkit:pr-test-analyzer` | confirm BLOCKER 1 + 2 minor coverage hole + 1 info | 無 |
| `pr-review-toolkit:silent-failure-hunter` | 4 high + 5 medium + 3 low silent-failure | 無 — H1（helm uninstall 吞例外）是新 finding；H4 與 code-reviewer BLOCKER 2 同源 |
| `pr-review-toolkit:type-design-analyzer` | 7 design observations，給 D5 fix 建議 F2(a) | 無 — 給 BLOCKER 2 的另一個 fix 方向 |
| `pr-review-toolkit:comment-analyzer` | 整體 durable，無 block | 無 — 補抓 CAQA 兩處 "21 AC" 寫錯（plan 實際 25 AC） |

無分歧。Approve / block 票數一致。

> 5 個 sub-agent 完整 raw transcript 在 `/private/tmp/claude-501/.../tasks/{a7c23e62e8759fe87, a852aeb6d40404781, a47ce043b07b72a6b, a0d8f3c30654fa27d, a3ab008e89a220651}.output`（執行期 ephemeral）。本 verdict 已整合所有 finding；無需 retain raw transcript。

## Plan alignment

Implementation 完全對齊 plan 的 6 個 D 決策（D1-D6）與 25 條 AC（[[caqa-2026-05-18]] Plan alignment table 已逐項對照，本 review 不重複）。

**但 plan 本身有兩個盲點**：
- **Plan §「不做的事 OUT OF SCOPE」未列「不限制 base.group_user create `scope='system'` MCP」** — implementation follow plan 把 CSV 提升 1,0,0,0 → 1,1,1,1，combine ir.rule 永遠允許 system-scope leg → 任何 internal user 可 create system MCP（MAJOR 1，見下）。
- **Plan §「service.unlink() 路徑都會經 `_do_cleanup`」是隱含假設** — 實際 controller 有 3 條 direct unlink 不經 cleanup hook（BLOCKER 2）。

## 同源 code path 檢查（CAQA 之外）

CAQA grep 排除 tests/ 與 migrations/。reviewer 補做的：

```
grep "user_mcp_server_ids" in src/                               → 2 hits ❌
   test_mcp_integration.py:380, 384
grep "cloud_service_id" in src/tests/ (mcp_server context)       → 9 hits ❌
   test_mcp_integration.py:280, 303, 318, 345, 358, 429, 636, 660, 693
grep "service\.unlink\(\)" in src/controllers/paas.py            → 3 hits ❌
   paas.py:1953, 1966, 2156（direct unlink，無 cleanup hook）
grep "self\.cloud_service_id" in src/models/mcp_server.py        → 0 hits ✅
grep "user_mcp_server_ids/mcp.cloud_service_id"
   in src/{models,controllers,views,security}                    → 0 hits ✅
grep "user_mcp_server_ids" in src/static/                        → 0 hits ✅
```

**Production code 乾淨；test 與 controller 有遺漏。**

## CAQA cross-check

CAQA 14 invariants 全 ✅。但有兩個 invariant 的 evidence 不完整：

- **Inv #4 (cleanup hook order)** — CAQA verified `_do_cleanup` 自身順序對；但**沒驗證所有 service deletion path 都經 `_do_cleanup`**。實際 paas.py 3 條 path 不經（BLOCKER 2）。
- **Inv #6 (system-scope leg always-true 是故意設計)** — CAQA 只 verify read leg 合理；漏抓 perm_create=True 在 always-true leg 下的 create privilege escalation（MAJOR 1）。

[[caqa-2026-05-18]] caveat 部分本 review 不重複：
- **C1**（cleanup hook `mapped` 補 sudo）— 與 BLOCKER 2 + MAJOR 2 一併由 implementation 解
- **F12**（controller helm_values 漏 sanitize）— pre-existing，leader 已裁示不擴 scope，本 review 不重審
- **Inv #7 措辭**（grep wrong file）— reporter finalize 改。Comment-analyzer 額外確認：CAQA report 自己有 2 處 typo `21 AC` 應為 `25 AC`（`caqa-2026-05-18.md:17, 338`）— reporter 一併修

## Blocker（必須退回）

### BLOCKER 1 — Stale schema 殘留在 registered test module → 退回 **tester**

**Location**: `src/tests/test_mcp_integration.py` 10 處：lines 280, 303, 318, 345, 358, 384, 429, 636, 660, 693

**Evidence**:
- `cloud_service.user_mcp_server_ids` 與 `mcp_server.cloud_service_id` 在 production code 已**完全移除**（grep 0 hits in `src/models/`）
- `test_mcp_integration.py` 內**3 個 class 仍引用舊 schema**：`TestAutoCreateMCPServer`（line 211, 6 tests）/ `TestMCPServerCronRetry`（line 387, 6 tests）/ `TestMCPServerCascadeDelete`（line 597, 3 tests）
- 3 個 class 都 `@tagged('post_install', '-at_install')` 且 registered（`src/tests/__init__.py:15`）
- 在 `-u woow_paas_platform --test-enable` 全 suite run 會 raise `ValueError: Invalid field 'cloud_service_id' on model 'woow_paas_platform.mcp_server'` 在 create/search 時 — **guaranteed red**

**為何 CAQA 漏抓**：
- CAQA verification command 使用 `--test-tags "/woow_paas_platform:Test{MigrationSchema,MigrationBehavior,Lifecycle,AclCrossWorkspaceIsolation,ChatConsumption,AiAssistantControllerMcpToolsForChannel,ProjectScopeConstraints}"` — 只跑 7 個指定 class
- CAQA §同源 code path 檢查明文排除 tests/（理由：tester fixture 故意保留舊鏈 reference 以鎖 regression）— 但這個 exclude 太寬：tester 的故意保留只在新 5 個 test files 內，既有 `test_mcp_integration.py` 是**本應一併更新但漏改**

**為何不是 implementation 漏改**：
- Implementation commit `12cf255` 沒動 `src/tests/`（`git show 12cf255 --stat -- src/tests/` 0 changes）
- 5c5d980（tester red suite commit）對 `test_mcp_integration.py` 只**追加** `TestMcpProjectScopeConstraints` 在尾巴，沒清舊
- Plan §Test scope 建議 「B. Constraint（補入既有 test_mcp_integration.py）」— plan 預期 tester 動這個檔案，但 plan 沒明文要求 tester 同時清舊 cascade/auto-create test
- **責任歸屬**：tester（在動 `test_mcp_integration.py` 時就該注意到既有 class 用了舊 schema），plan 盲點是次要

**Fix**:
- **Option A**: Port 3 個 class（14 個 test methods）到 new schema（`project_id` + `mcp_server_ids`），threading project fixture through `_simulate_auto_create` / `_create_auto_mcp_server` helpers
- **Option B（推薦）**：直接 retire 3 個 class — 新 `test_mcp_project_lifecycle.py` (AC #11, #12) 已 cover cascade 行為，新 `TestMcpProjectScopeConstraints` 已 cover constraint，多數既有 case 是 duplicate
- Plus：**再跑一次完整 `-u woow_paas_platform --test-enable` 提綠色證明**（不要 tag-scoped）

### BLOCKER 2 — `cloud_service.unlink()` 3 條 direct path 繞過 `_do_cleanup` → 退回 **implementation**

**Location**: `src/controllers/paas.py:1953, 1966, 2156`（3 條 direct `service.unlink()` 不經 lifecycle）

**Evidence**:
- `paas.py:1942-1953` controller 自己 `state='deleting'` + `client.uninstall_release(...)` + `service.unlink()`，**完全不走 `_do_cleanup`**
- `paas.py:1966` `PaaSOperatorError` 404 case 同樣 direct unlink
- `paas.py:2156` 同 pattern
- 新 schema FK 拓樸：`project.cloud_service_id ondelete='set null'` + `mcp_server.project_id ondelete='cascade'`
- 流程: `service.unlink()` → `project.cloud_service_id` 變 NULL → user-scope MCP **仍存活**（cascade 不會 fire，因為 project 沒 unlink）→ **D5 invariant 違反**（M.project_id.cloud_service_id IS NULL but scope='user'）+ MCP URL 指向已不存在的 sidecar（zombie）

**為何 CAQA line 87 的 mitigation claim 不成立**：
- CAQA S5 寫「若 service 刪導致 project unbind，cleanup hook (step 1) 先清掉 user-scope MCP 才走 unbind」
- 此 claim **預設所有 service.unlink 都走 `_do_cleanup`**
- 實際 3 條 controller path 不走 — CAQA 漏抓

**為何 D5 `@api.constrains` 無法 catch**：
- `@api.constrains('scope', 'project_id')` 只在 mcp_server **自身欄位**寫入時 fire
- `project.cloud_service_id` 變 NULL 不會 trigger mcp_server 的 constrain
- Silent-failure-hunter H4 同源（conceptual），type-design-analyzer F2(a) 提出 alternative fix

**Fix（leader 已裁示 Option A）**:
- **Option A（leader 裁示 ✅）**: override `cloud_service.unlink()` 在 super 之前主動 cleanup user-scope MCP，把 `_do_cleanup` 的 Step 1 邏輯收斂到 unlink override 內，無論 caller 是 lifecycle / controller / admin / direct ORM 都會走
- ~~Option B: 把 paas.py:1953, 1966, 2156 三條 path 改成呼叫 `service._do_cleanup()` + 接受 cleanup 完整接管 helm uninstall + state 寫入~~ — 沒選
- ~~Option C（type-design F2(a)）: `@api.constrains('scope', 'project_id', 'project_id.cloud_service_id')`~~ — Odoo 18 dotted-path constrains 行為**需先驗證**，沒選
- **附帶 test**：新 case `test_direct_service_unlink_via_controller_cleans_user_mcp` 鎖 BLOCKER 2

## Major（與 BLOCKER 一輪解）

### MAJOR 1 — Privilege escalation surface：任何 internal user 可 create `scope='system'` MCP → 退回 **planner** 拍板（leader 建議 A rule split）

**Location**: `src/security/ir.model.access.csv:14`（mcp_server user RWCU 1,1,1,1） + `src/security/ir_rules.xml:127-136`（rule perm_create=True，system leg always-true）

**Mechanism**:
- CSV `access_mcp_server_user 1,1,1,1` 給 base.group_user CRUD
- ir.rule `mcp_server_rule_workspace_member` 的 system leg `('scope', '=', 'system')` 對任何 user 為 true（這是 plan §Inv #6 故意設計）
- `perm_create=True` on this rule
- D4 constraint 允許 `scope='system' + project_id=NULL`
- → **任何 internal user 都能** `env['woow_paas_platform.mcp_server'].create({'scope': 'system', 'url': 'http://attacker.com', ...})`
- → 新 system MCP 對所有 user 可讀（system leg always-true）
- → 若 admin 在 `ai.assistant.mcp_server_ids` 後台不慎選了該 row → 所有 user chat 透過該 assistant 會 trigger attacker 的 tool call

**為何 plan 沒抓**：
- Plan AC #17 / #19 / #20 只規範 read leg（cross-workspace user MCP 不可讀 + system MCP 對所有 user 可讀）
- AC #21 把 CSV 提升 1,1,1,1 的 reasoning 是「讓 user 在自家 workspace CRUD user-scope MCP」— **沒考慮 ir.rule 的 perm_create=True 同時對 system leg 也放行**

**為何 CAQA 沒抓**：
- CAQA Inv #6 只 verified system leg always-true 是故意設計（read 視角）
- 沒檢查 perm_create=True 在 system leg always-true 下的 implication

**Severity 判斷**：
- 不升 BLOCKER 的理由：需要 admin 主動把 attacker MCP 加進 ai.assistant.mcp_server_ids 才生效 — social engineering 攻擊面，不是直接 RCE
- 但 surface 是新引入的（refactor 前無 rule，policy 未明文；現在 policy 是 explicit「任何 user 可 create system MCP」）— 不該默認此設計

**Fix（leader 建議 A，planner 拍板）**:
- **Option A（leader 建議 ✅，待 planner confirm）**: rule split — 拆成兩條 rule：
  - `mcp_server_rule_system` — domain `[('scope', '=', 'system')]`，groups `base.group_user`，perm_read=True，perm_create/write/unlink=False
  - `mcp_server_rule_workspace_member_user` — domain `[('scope', '=', 'user'), (workspace chain)]`，groups `base.group_user`，perm_read/write/create/unlink=True
  - CSV 維持 1,1,1,1，由 rule 細控
- Option B: 加 record rule 限制 `scope='system'` create 需要 `base.group_system`
- Option C: 接受目前設計但**明文標 invariant**「system MCP 對任何 user 可 create 是平台級設計選擇」

### MAJOR 2 — `_do_cleanup` 吞 helm uninstall 失敗仍寫 `state='deleting'` → 退回 **implementation**（與 BLOCKER 2 一輪解）

**Location**: `src/models/cloud_service.py:1146-1163`

**Mechanism**:
- Step 2 `client.uninstall_release(...)` 包在 try/except，catch all Exception，only log warning（line 1157-1161）
- Step 3 line 1163 **無條件**寫 `state='deleting'`
- **Pattern 不一致**：對比 `_do_suspend` (line 1070) / `_do_resume` (line 1106) — 失敗時都 set `state='error', error_message=...`
- 結果：helm release 留在 cluster（quota 持續佔用、sidecar URL 仍 expose），無 error flag，operator 無 signal
- 加上 Step 1 user-scope MCP 已 unlink（line 1138）— 即使後來 retry helm uninstall 也無 MCP wiring 可重建

**Fix**:
```python
# Step 2 except branch
self.write({
    'state': 'error',
    'error_message': f'Helm uninstall failed: {e}',
})
return  # 不再走 Step 3 寫 state='deleting'
```
或引入 `state='cleanup_failed'` + janitor cron 重試。

## Minor / Info（follow-up 即可，本輪不擋）

| # | Item | Location | Severity | Found by |
|---|---|---|---|---|
| 1 | Step 1 unlink 無 try/except — partial cleanup 風險 | `cloud_service.py:1133-1142` | medium | silent-failure-hunter H2 |
| 2 | SSE streaming MCP fail silently fallback to text-only — UX hole | `ai_assistant.py:447-485` | medium | silent-failure-hunter H3 |
| 3 | Migration step 1 LIMIT-1 join 依賴 unique constraint 但無 pre-flight assert | `post-migrate.py:54-69` | medium | silent-failure-hunter M1 |
| 4 | Migration step 2 archive log loop run after UPDATE — partial fail 時 log 誤報 | `post-migrate.py:88-104` | medium | silent-failure-hunter M2 |
| 5 | Migration step 4 noupdate flip 無 row-count assertion (==2) | `post-migrate.py:120-135` | medium | silent-failure-hunter M3 |
| 6 | `_sync_mcp_server` 雙層 success/state='error' 不一致 — 前端易誤判 | `paas.py:1369-1374` | medium | silent-failure-hunter M4 |
| 7 | Cron retry 到 max retries → state='error' 無 user notification | `mcp_server.py:234-247` | medium | silent-failure-hunter M5 |
| 8 | `auto_created` field 是 UI-only readonly，無 write protection | `mcp_server.py:93-98` | low | type-design F6 |
| 9 | Archived (active=False) project + user-scope MCP 場景無 test | n/a | low (sev 6) | pr-test-analyzer |
| 10 | `_model_auto_create_mcp_server` localhost URL fallback 場景無 test | `cloud_service.py:1036` | low (sev 5) | pr-test-analyzer |
| 11 | `_simulate_auto_create` helper duplicates production `_model_auto_create_mcp_server` — drift 風險 | `test_mcp_integration.py:266` | info | pr-test-analyzer |
| 12 | Controller "MCP Server not found" 對 system-scope 命中時 misleading | `paas.py:1335/1353/1366/1384` | minor | code-reviewer |
| 13 | `_model_auto_create_mcp_server` `self.project_ids[:1]` 非確定性 ordering | `cloud_service.py:980` | minor | code-reviewer |
| 14 | CSV `1,1,1,1` 可緊縮 1,1,1,0（user-scope 用 cascade unlink） | `ir.model.access.csv:14` | minor | type-design F5 |
| 15 | D4 可加 partial SQL CHECK 雙保險（raw SQL bypass 防線） | `mcp_server.py:111-125` | info | type-design F1 |
| 16 | CAQA report 兩處 "21 AC" 應為 "25 AC" | `caqa-2026-05-18.md:17, 338` | typo | comment-analyzer |
| 17 | `scope` Selection 二元假設，未來加 'org' 需重寫 D4 | `mcp_server.py:49-57` | info | type-design F7 |

## 各檢查點結論

### B1-style ACL 對等 deny enforce（mcp_server + mcp_tool）
**✅ 對等正確**。Multi-agent 一致 verify：
- `ir_rules.xml:127-136` `mcp_server_rule_workspace_member`
- `ir_rules.xml:142-151` `mcp_tool_rule_workspace_member`（domain 透過 `server_id.` 反向，完全鏡像）
- 兩條 perm_read/write/create/unlink 全 True，groups `base.group_user`
- Test `test_alice_cannot_read_bob_user_mcp_tool` 與 `test_alice_cannot_read_bob_user_mcp_via_orm` 雙 path（search + browse.read）lock B1 regression
- 但**注意**：always-true system leg + perm_create=True 引入 MAJOR 1 的新攻擊面 — 對等性正確 ≠ 設計完整

### 整體 commit history 順序是否清晰反映 TDD chain
**✅ 清晰**。
```
5c5d980 test(refactor): add TDD red suite for MCP cloud_service_id -> project_id (24 cases)
3d35e79 test(refactor): fill 2 reasoning gaps in MCP red suite (Gap 1 + Gap 2)   ← +2 cases = 26
48d49bb test(refactor): patch 2 MCP red-suite fixtures to reach green            ← fixture A-plan
12cf255 refactor: MCP cloud_service_id -> project_id decouple                    ← implementation
00c1e01 docs(refactor): publish MCP cloud_service_id -> project_id plan and design draft
7b48e67 docs(refactor): append cycle-3 fixture rationale to MCP test scope report
478fdc9 docs(refactor): add MCP project_id refactor test coverage report
90ee0eb docs(refactor): add CAQA report for MCP cloud_service_id -> project_id
```
red → 2 gap fill → fixture patch → implementation → docs publish → CAQA — 標準 TDD chain。Plan/decision doc commit 在 implementation 之後是 reporter 模式（draft 已 leader sign-off，commit 是 publish action），不算 history 邏輯違反。

### `/simplify` 後是否還有可簡化處
**有，建議 reporter follow-up**：
- Controller manual check pattern `if not server.exists() or server.project_id.cloud_service_id.id != service.id` 重複 4 次（`paas.py:1335/1353/1366/1384`）— 可抽 `_check_mcp_server_belongs_to_service(server, service)` helper（與 minor #12 一併）
- `_get_user_mcp_tools` 在 `discuss_channel.py:310` 與 `_get_user_mcp_tools_for_channel` 在 `ai_assistant.py:614` 邏輯完全一樣（chain walk + filter scope/active/state + McpTool.search），可抽 `models/discuss_channel.py` 上的 mixin function；現況 controller path 自己重寫一遍是 cursor invalidation 考量（line 415 註解有提及）— **可不簡化，但 reporter 應 surface 此 duplication**

## Recommendation（leader 已裁示）

1. **BLOCKER 1 → tester re-open**：fix `test_mcp_integration.py` 10 處 stale schema（option B retire 3 個 class 推薦），再跑 full `-u woow_paas_platform --test-enable` 提綠色證明（不要 tag-scoped）
2. **BLOCKER 2 → implementation re-open**：override `cloud_service.unlink()` 自加 user-scope MCP cleanup（option A）+ 新增 regression test 鎖 direct controller unlink 路徑
3. **MAJOR 1 → planner 拍板**：leader 建議 A rule split（system rule perm_read-only / user rule CRUD），CSV 維持 1,1,1,1 由 rule 細控；planner 評估後 confirm 或反對
4. **MAJOR 2 → implementation re-open**（與 BLOCKER 2 一輪解）：`_do_cleanup` Step 2 失敗時 mirror `_do_suspend` pattern set `state='error'`
5. **17 個 minor / info → reporter** 列入 [[follow-ups]]（其中 #16 CAQA typo 列入 reporter phase clean-up）

新 vs 已知 caveat 區分：

- **新 caveat（CAQA 漏抓，本 review 補抓）**：BLOCKER 1 / 2、MAJOR 1 / 2、minor #1-13
- **CAQA 已知**：C1 / F12 / Inv #7、minor #14-17（其中 #16 是 CAQA 自身 typo）

## See also

- [[mcp-project-decouple]] — 本次 decision draft（reporter finalize 時併修 Inv #7 措辭）
- [[cloud-service-project-decouple]] — 前次 decision（7 invariants）
- [[caqa-2026-05-18]] — CAQA 報告（reviewer 基線；本 verdict 補抓 CAQA 明文排除範圍）
- [[cross-workspace-task-data-leak]] — B1 pitfall（本次 mcp_tool 對等 rule 守住）
- `docs/refactor/plan-mcp-project-decouple.md` — 25 AC plan
- `docs/refactor/test-scope-report-mcp-2026-05-18.md` — 26 cases red proof
- `docs/refactor/test-coverage-mcp-project-decouple.md` — coverage report（test-explainer）
- `docs/refactor/pr-review-verdict-2026-05-17.md` — 前次 review verdict（同一 reviewer role 樣板）

---

**End of review report. Hand back to refactor-leader.**

Reviewer 進 standby，等 round 2（tester + implementation + planner 三 phase re-open 後 leader push 新 branch state 來做 round 2 review）。
