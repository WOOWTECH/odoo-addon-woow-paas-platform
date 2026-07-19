---
name: reviewer-report-2026-05-20-mcp-refactor
phase: reviewer
team: mcp-refactor
reviewer: mcp-refactor-reviewer
plan_ref: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
design_ref: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md
test_report_ref: docs/archived/tracking/test-coverage-mcp-refactor-2026-05-20.md
caqa_ref: docs/archived/tracking/caqa-report-2026-05-20-mcp-refactor.md
issue:
  - 49
  - 51
created: 2026-05-21
verdict: APPROVED WITH CAVEATS (escalate leader on 3 hard caveats)
related:
  - docs/decisions/refactor/mcp-project-decouple.md
  - docs/decisions/patterns/mcp-acl-6-rule-split.md
  - docs/decisions/patterns/cloud-service-unlink-override-cleanup.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/tracking/pitfalls/system-mcp-create-acl-bypass.md
  - docs/tracking/pitfalls/direct-unlink-bypass-cleanup-hook.md
status: archived
archived-from: docs/tracking/reviewer-report-2026-05-20-mcp-refactor.md
archived: 2026-07-19
archived-reason: "一次性 reviewer 驗收報告，cycle 已收官；證據錨點引用已改指本處"
---

# Reviewer Report — MCP Refactor URL Redesign + Unbound Project ACL（issue #49 + #51）

## 1. Final Verdict

**APPROVED WITH CAVEATS** — 不擋 merge；但 **3 條 multi-agent 抓到 CAQA 漏抓的 MAJOR finding** 必須 surface 給 leader 拍板是否升 BLOCKER / 本 PR 內收乾 / 排 hotfix follow-up。

核心動作守線 verified ✓：
- D5 拔除整段刪、`sidecar_backed` 新欄位 + migration backfill ✓
- ACL dual-leg domain shape **structurally correct**（外層 `'&', ('scope', '=', 'user')` 是 MAJOR 1 重炸防線的根基 — type-design-analyzer 手動 polish-notation precedence walk verified；code-reviewer 同意 H.11 / H.12 explicit reproducer 鎖住 create direction）✓
- Cleanup filter 兩條路徑 (`_do_cleanup` + `unlink()` override) 對等改寫 `lambda s: s.scope == 'user' and s.sidecar_backed` ✓
- Sidecar-hint endpoint + ACL gate + 統一 `Project not found or access denied` enum 對齊 PR #53 ✓
- `_validate_mcp_url` minimal SSRF 對齊 plan AC #30；6 case 正反向鎖 ✓
- PR #47 既有 9 ACL regression + BLOCKER 2 cleanup hook 既有 9 case fixture conditional 餵料 **全綠** ✓
- 3 條 GREEN regression lock (test report ch.7) 設計強度 STRONG（`assertEqual` 嚴格鎖 enum / `assertRaises` 緊縮 scope）✓

5 agent 之間**無方向性分歧**：5 agent 都 verdict **APPROVED WITH CAVEATS**。

## 2. 5 Agent Finding 整合表

| Agent | Verdict | BLOCKER | MAJOR | MINOR | INFO |
|-------|---------|---------|-------|-------|------|
| code-reviewer | APPROVED WITH CAVEATS | 0 | **1** | 1 | 1 |
| pr-test-analyzer | APPROVED WITH CAVEATS | 0 | **1** | 4 | 2 |
| silent-failure-hunter | APPROVED WITH CAVEATS | 0 | **2** | 4 | 2 |
| type-design-analyzer | APPROVED WITH CAVEATS | 0 | 0 | 4 | 4 |
| comment-analyzer | APPROVED WITH CAVEATS | 0 | **2** | 5 | 2 |
| **合計（去重）** | — | 0 | **6 unique** | ~12 | ~7 |

### 2.1 MAJOR Findings (6 unique)

#### M-R1 (code-reviewer): Legacy `_create_mcp_server` / `_update_mcp_server` SSRF guard 漏裝

- **位置**：`src/controllers/paas.py:1348-1372` (`_create_mcp_server`) + `src/controllers/paas.py:1374-1390` (`_update_mcp_server`)
- **症狀**：legacy 端點 `/api/workspaces/<id>/services/<id>/mcp-servers` (frontend `cloud_service.js:587/612/638` 仍在用) 完全沒 call `_validate_mcp_url` → workspace owner / admin 可塞 `http://169.254.169.254/...` URL
- **Attack vector**：URL 是 stored row 後續 `action_test_connection` / `action_sync_tools` 主動發 outbound HTTP → SSRF
- **本 PR 製造？**：否（pre-existing；plan AC #30 only spec 新端點 `_create_project_mcp_server` / `_update_project_mcp_server`）
- **同源 code path 但 plan 沒列** → 屬於 scope 邊界爭議

#### M-R2 (silent-failure-hunter): `paas.py:_resolve_project_access` 對 unbound project blanket `ROLE_OWNER`

- **位置**：`src/controllers/paas.py:1483-1502` (`_resolve_project_access`)
- **症狀**：對 `not workspace`（含 unbound project + bound-but-no-workspace）case 直接 `return project, self._SyntheticAccess(role=ROLE_OWNER)` — **任何 authenticated `base.group_user` 都 blanket grant ROLE_OWNER**
- **與本 PR 收緊的 `ai_assistant.py:_check_project_access`（follower check）silent asymmetric**
- **`_SyntheticAccess` docstring 本身就 stale**：說「Aligns with `ai_assistant.py:_check_project_access`: unbound projects grant global access」+「D5 ORM constraint still protects user-scope MCP integrity」— 但本 PR 已收緊 ai_assistant + 已刪 D5
- **Attack vector**（reviewer 已 verify）：
  1. Dave (W2 user, 非 W1 unbound project follower) → `POST /api/support-projects/<W1_unbound_pid>` action=update name → `_resolve_project_access` 過 → blanket ROLE_OWNER → sudo write 成功
  2. `POST /api/support-projects/<W1_unbound_pid>/mcp-servers` action=create → 走 `McpServer.sudo().create(...)` (paas.py:1668-1682) → **bypass ir.rule leg 2** → 注入 user-scope MCP row 到 victim unbound project
  3. 之後 sync/test 走 outbound HTTP，等同 SSRF + injection 雙風險
- **H.9 endpoint test 只 cover ai_assistant.py 的 `/api/support/projects` endpoint，不 cover paas.py 的 `/api/support-projects/<pid>` endpoint**（reviewer 已讀 `test_mcp_acl_unbound_isolation.py:612-710` confirm）
- **本 PR 製造？**：否（pre-existing PR #53 引入 `_SyntheticAccess` + `_resolve_project_access`）
- **但本 PR 收緊 ai_assistant 而沒同步收緊 paas.py = silent divergence 擴大**
- **CAQA 漏抓**：caqa report line 190 處理「兩函數要不要統一」是 code style 問題，沒分析「不統一 → silent IDOR」後果

#### M-R3 (silent-failure-hunter): `_resolve_project_mcp` sudo browse 缺 caller ACL 檢查

- **位置**：`src/controllers/paas.py:1687-1695` (`_resolve_project_mcp`)
- **症狀**：`McpServer.sudo().browse(int(server_id))` + `server.project_id.id != project.id` 是唯一 resolve gate
- **Depends on M-R2** — fix M-R2 即連帶 close（M-R2 攻擊鏈第二步走的 row id 是 attacker 自己 create 拿到，後續 update/delete/sync/test 透過此 helper 全通）

#### M-R4 (pr-test-analyzer): OR-X9 32-case write/unlink direction 對 unbound 是 implicit-only

- **位置**：`src/tests/test_mcp_acl_unbound_isolation.py:1-711`
- **症狀**：H.1–H.6 只測 `search()` + `browse().read()`（read direction）— UF (unbound follower) × S × W/U 與 UN (unbound non-follower) × S × W/U 沒 **explicit** write/unlink negative assertion
- **Risk**：若 implementation 不慎把 `mcp_server_rule_workspace_user` leg 2 寫 `perm_write=True` 但 follower carve-out 漏設，Dave (non-follower) 可 write Alice unbound external MCP — 此 case PR #47 既有 9-case 不會抓（PR #47 case 是 bound-only），H group 也沒 explicit lock
- **目前防線**：reviewer 已手動讀 `src/security/ir_rules.xml:156-172` confirm `mcp_server_rule_workspace_user` 與 `mcp_tool_rule_workspace_user` 兩條 rule 的 `perm_*` 屬性 `1,1,1,1` 都套同一 dual-leg domain — leg 2 walk 套到 write/unlink direction 是同 domain，行為與 read 同。所以實際 runtime 行為 OK
- **但 test coverage GAP 真實** — 後續 PR 改 leg 2 仍可能引入 perm direction divergence 而無 H test 守線

#### M-R5 (comment-analyzer): `docs/decisions/patterns/mcp-acl-6-rule-split.md` 5 段 stale — dual-leg variant 未文件化

- **位置**：`docs/decisions/patterns/mcp-acl-6-rule-split.md:48-58, 81-84, 196-213, 218-232, 234-243`
- **症狀**：template、sub-model template、Domain walk path 一致性示例、Test scaffolding、後人改本區前 checklist 全部仍呈現 single-leg `workspace_user` domain，與 PR 落地的 dual-leg 不一致
- **Risk**：後人讀此 pattern doc 想加新 model 用「6-rule split」會直接 copy single-leg template，遺漏 unbound carve-out 設計意圖 → [[mcp-project-decouple]] New Inv #11 直接破功
- **本 PR 製造？**：是（PR 落地 dual-leg 後 doc 未同步）
- **Plan §Implementation phasing #7 reporter 已列為 reporter phase 動作** → reporter 收尾即可，不擋 merge
- **不升 BLOCKER 理由**：code 自己 `ir_rules.xml` inline comment + test docstring (H.11/H.12) 已 captures dual-leg shape rationale；runtime security 不受 doc drift 影響

#### M-R6 (comment-analyzer): `docs/decisions/refactor/mcp-project-decouple.md` Invariants 段 stale

- **位置**：`docs/decisions/refactor/mcp-project-decouple.md:221, 232-247, 252-259`
- **症狀**：仍寫 D5 active；Inv #5 表格仍 single-leg；Inv #7 URL helper 名稱未更新；缺 New Inv #9/#10/#11
- **Plan reporter phase 已列** → reporter 收尾，不擋 merge
- **與 M-R5 paired follow-up，由 reporter 一併處理**

### 2.2 MINOR / INFO 摘要（不展開，全部 reporter 收尾）

| Agent | MINOR/INFO 摘要 |
|-------|----------------|
| code-reviewer | stale module docstring `test_project_mcp_project_centric.py:21-28` 引用已刪 D5 test class（與 CAQA L4 同源） |
| pr-test-analyzer | H.4 cross-workspace assertion 比 H.3 弱（缺 `browse().read()` AccessError leg）；`test_url_rejects_unsupported_scheme` 沒鎖 error wording；SSRF empty host/port 0/userinfo edge case (by-design defer #52)；F group `_assert_rejected` IPv6 4-char substring matcher loose；H.5 一 method 兩 assertion partial info loss on red |
| silent-failure-hunter | `_check_task_access` propagation 與 plan AC #43 wording 不符（**silent 收緊**，security 嚴一點，方向正面 — 建議 plan 補述）；CAQA M1 / M2 / L1-L4 全 confirm；SSRF error format IPv4/IPv6 不一致；payload `sidecar_backed` by-design 不 enforce 一致性 |
| type-design-analyzer | `auto_created` × `sidecar_backed` 兩 boolean 並存無 schema invariant；`_cron_retry_mcp_sync` deprecated field load-bearing；`sidecar_backed` payload write 缺 server-side cross-check；`write_actions` 變數名含 `sidecar-hint` (read-only) 誤導 |
| comment-analyzer | `ir_rules.xml:145-155` workspace_user comment「sidecar-backed flow」標籤錯 + 「Both legs walk follower / workspace membership」措辭歧義；`_check_project_access` docstring 漏 task path mirror + bound-no-workspace edge case；`auto_created` help text actionable 不足；`test_mcp_d5_removal.py:194-200` D5 stale comment（CAQA L4）；migration warning branch comment 動機推測過度；CAQA L1 codebase-wide ref noise debt **confirm 本 PR 無新引入** |

### 2.3 評分摘要 (type-design-analyzer)

- Encapsulation: 3/5 — `sidecar_backed` payload write 沒 ORM-layer enforcement，由 convention 守
- Invariant expression: 2/5 — 核心 invariant「sidecar-backed lifecycle」live 在 3 個 file docstring，type 系統不表達
- Usefulness: 4/5 — D5 trade for B-plan UX flexibility 是 net positive
- Enforcement: 3/5 — ACL dual-leg DB-level enforced（5/5）；但 `sidecar_backed` 零 model-level constraint

## 3. Multi-agent 分歧處理

**無方向性分歧**。5 agent verdict 一致 **APPROVED WITH CAVEATS**，對核心動作（D5 拔、dual-leg shape、MAJOR 1 重炸防線守住、cleanup filter 改寫、SSRF check 新端點）verdict 一致正面。

**有「嚴重程度判斷」差異但非分歧**：
- code-reviewer 自評 M-R1 不阻擋本 PR（plan AC #30 only spec 新端點）
- silent-failure-hunter 自評 M-R2 / M-R3 為 follow-up 不擋本 PR（pre-existing PR #53）
- pr-test-analyzer 自評 M-R4 為 follow-up 不擋本 PR（runtime 行為 OK，test gap 是後續 PR 風險）
- comment-analyzer 自評 M-R5 / M-R6 為 reporter phase scope 不擋本 PR

**Reviewer 的 verdict 角度**（自評）：5 agent 都建議 follow-up，**但 M-R1 + M-R2 是 security MAJOR finding（reproducible attack vector）+ CAQA 漏抓** — 按 reviewer role hard rule「發現 high-severity 但 CAQA 沒抓到 → escalate leader，且不能直接 approve」，**必須 surface 給 leader**：

> Leader 需 sign-off 以下 3 條 hard caveat 是否：
> - (a) approve with hard caveat → 開 follow-up issue 排入 sprint+1 PR 收乾
> - (b) hotfix in this PR → 加 paas.py `_resolve_project_access` follower carve-out + legacy `_create_mcp_server` SSRF guard + H.9 endpoint test 鎖到 paas.py path
> - (c) escalate user 拍板

## 4. Hard Caveats（surface 給 leader 拍板）

### HC-1: Legacy SSRF guard 漏裝 (M-R1)

- **檔案**：`src/controllers/paas.py:1348-1372` + `:1374-1390`
- **嚴重度**：MAJOR security finding（同 root cause as plan AC #30 但 same-source code path 沒 fix）
- **本 PR 製造？**：否（pre-existing pre-PR）
- **建議**：
  - **Minimal hotfix** (1-2 line)：在 `_create_mcp_server` / `_update_mcp_server` URL write 前同 call `_validate_mcp_url`，與新端點對齊
  - **若不 hotfix**：reporter phase 明文 surface「workspace owner / admin 透過 `/api/workspaces/<id>/services/<id>/mcp-servers` action=create/update with `url=http://169.254.169.254/...` 仍可觸發 SSRF via sync/test」attack vector，排 follow-up issue 高優先級

### HC-2: `paas.py:_resolve_project_access` unbound project ACL asymmetric (M-R2)

- **檔案**：`src/controllers/paas.py:1483-1502`（同檔 `_SyntheticAccess` docstring 也 stale）
- **嚴重度**：MAJOR security finding（reproducible IDOR + sudo write + MCP injection）
- **本 PR 製造？**：否（pre-existing PR #53 引入）
- **但本 PR 擴大 silent divergence**：ai_assistant.py 收緊 follower check，paas.py 同 unbound carve-out surface 沒同步 → 兩 endpoint 對「unbound project + non-follower internal user」**opposite** 結果
- **CAQA 漏抓**：caqa report line 190 處理「兩函數要不要統一」code style 問題，**沒分析 silent IDOR 後果**
- **建議**：
  - **Minimal hotfix** (1 line): `_resolve_project_access` line 1496-1498 改 `if not workspace: ` 加 follower check（與 ai_assistant.py:_check_project_access 對齊）
  - **`_SyntheticAccess` docstring** 同步更新（line 1474-1481 提到 D5 ORM constraint 但已刪、提到 "Aligns with ai_assistant.py: ... grants global access" 但已收緊）
  - **H.9 endpoint test 擴**：加 paas.py `/api/support-projects/<pid>` endpoint deny non-follower update/create 鎖
  - **若不 hotfix**：reporter phase 明示「Dave (任何 internal user, non-follower) 透過 `/api/support-projects/<pid>` 仍可對 unbound project ACL bypass」attack vector，排 high-priority follow-up issue

### HC-3: OR-X9 32-case write/unlink direction 對 unbound 是 implicit-only (M-R4)

- **檔案**：`src/tests/test_mcp_acl_unbound_isolation.py:1-711`
- **嚴重度**：MAJOR test coverage gap（runtime 行為 OK，後續 PR 改 leg 2 風險）
- **建議**：
  - **本 PR 內**：reviewer 已手動 verify 兩條 `workspace_user` rule `perm_*` 都套同一 dual-leg domain，runtime 行為一致 → 不擋 merge
  - **Follow-up 加 H.3b / H.4b explicit write/unlink negative assertion**：Dave (non-follower) `external_mcp.with_user(dave).write({'name': '...'})` → assertRaises AccessError；同 unlink direction
  - **追加給 plan AC #38 / #39 test scope**：未來 PR 改 dual-leg 須加 perm direction matrix lock

## 5. Soft Caveats（reporter phase 收尾，不擋 merge）

| ID | Item | 來源 agent | 收尾段 |
|----|------|----------|------|
| SC-1 | `docs/decisions/patterns/mcp-acl-6-rule-split.md` 5 段 update 為 dual-leg variant | comment-analyzer M-R5 | reporter |
| SC-2 | `docs/decisions/refactor/mcp-project-decouple.md` Invariants 段：Inv #3 mark deleted, Inv #5 dual-leg, Inv #7 URL helper rename, 新增 New Inv #9/#10/#11 | comment-analyzer M-R6 | reporter |
| SC-3 | CAQA M1 (cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap) | CAQA M1 + silent-failure-hunter + code-reviewer + type-design-analyzer 全 4 agents confirm | follow-up PR |
| SC-4 | CAQA M2 (`_check_project_access` bound-but-no-workspace blanket allow) | CAQA M2 + silent-failure-hunter + code-reviewer confirm | follow-up PR |
| SC-5 | `_list_projects` F10 sudo + empty domain leak | silent-failure-hunter (CAQA out-of-scope confirm) | F10 follow-up |
| SC-6 | `_check_task_access` propagation 與 plan AC #43 wording 不同步（silent 收緊 — security 角度正面）| silent-failure-hunter | plan 補述「task path 透過 delegation 連帶收緊」 |
| SC-7 | `ir_rules.xml:145-155` workspace_user comment 改寫（leg 1 「sidecar-backed flow」標籤錯）| comment-analyzer | reporter |
| SC-8 | `_check_project_access` docstring 補 task path mirror + bound-no-workspace edge case | comment-analyzer | reporter |
| SC-9 | `auto_created` help text 補 actionable hint（指向 drop column TODO）| comment-analyzer + type-design-analyzer | reporter |
| SC-10 | `test_mcp_d5_removal.py:194-200` 移除「D5 active」過時假設（CAQA L4）| comment-analyzer + CAQA L4 | reporter |
| SC-11 | `migrations/18.0.1.0.25/post-migrate.py:44-46` warning branch comment 改事實陳述 | comment-analyzer | reporter |
| SC-12 | `test_project_mcp_project_centric.py:21-28` stale module docstring 拔 `TestProjectMcpD5OrmConstraint` 引用 | code-reviewer | reporter |
| SC-13 | `cloud_service.py:_model_build_mcp_endpoint_url` (line 963-984) — dead code (`grep -rn` no caller in `src/`) | code-reviewer | F19 (URL helper duplication follow-up) 一併處理 |
| SC-14 | `write_actions` 變數名含 `sidecar-hint` (read-only) 誤導 | type-design-analyzer | future PR rename `gated_actions` 或拆兩 set |
| SC-15 | H.4 cross-workspace assertion 比 H.3 弱（缺 `browse().read()` AccessError leg） | pr-test-analyzer | follow-up test 補強 |
| SC-16 | `test_url_rejects_unsupported_scheme` 沒鎖 error wording | pr-test-analyzer | follow-up test 補強 |
| SC-17 | F group `_assert_rejected` IPv6 4-char host substring matcher loose | pr-test-analyzer | follow-up test 補強（改 full-host match）|
| SC-18 | SSRF empty host / port 0 / userinfo edge case 未測（by-design defer #52）| pr-test-analyzer | F25 / #52 follow-up |
| SC-19 | CAQA L1 confirm 本 PR 無新引入 comment ref noise；既有 4 處 PR47/53 debt | CAQA L1 + comment-analyzer | reporter codebase-wide cleanup follow-up |
| SC-20 | CAQA L2 (URL helper duplication) / L3 (SSRF error format IPv4 vs IPv6) | CAQA L2 + L3 confirm | follow-up |

## 6. CAQA Cross-Check

CAQA verdict APPROVED WITH CAVEATS + 4 caveat（M1 / M2 / L1 / L4）+ defer (L2 / L3 / OR-X8) **全 confirm 不重炸**。

**CAQA 漏抓的 finding（reviewer multi-agent 新發現）**：
- M-R1（legacy SSRF guard 漏） — CAQA verify SSRF coverage 對齊 plan AC #30 minimal scope，但**沒對 same-source code path（legacy endpoint）做 grep verify**。reviewer code-reviewer agent 透過 grep + frontend service.js 引用追蹤抓到
- M-R2（paas.py `_resolve_project_access` 對 unbound blanket ROLE_OWNER）— CAQA report line 190 提到「拒絕統一 `_resolve_project_access` vs `_check_project_access`」是 code style 問題；**沒分析 silent IDOR 後果**。reviewer silent-failure-hunter agent 透過 attack vector trace 抓到
- M-R4（OR-X9 write/unlink direction test gap）— CAQA §2 cell map 標 `UF/UN × S × W/U` 為「implicit through ORM create」+ PR #47 既有 9-case；**沒 verify 「PR #47 9-case 不 cover unbound row」**。reviewer pr-test-analyzer agent 透過矩陣 cell-by-cell 對照抓到

**CAQA L1 / L2 / L3 / L4 / M1 / M2 / 11 invariants verify / OR-X 8 risk closure / 32-case ACL matrix shape / SSRF coverage** 全 confirm — 沒 over-call 也沒 under-call。

## 7. Plan Alignment Check

Implementation 對齊 plan B 案 + 44 AC + 2 wave 8 OQ 拍板：
- **Plan 路線正確**：D5 拔、auto-create 拆、UI shortcut、sidecar_backed flag、dual-leg ACL、ai_assistant carve-out 全落地
- **OQ-2 (b) implementation-driven** keep deprecated `auto_created` — leader 拍板自由度範圍內
- **本 PR scope creep（正面）**：`_check_task_access` 透過 delegation 連帶得到 unbound follower carve-out（plan AC #43 寫「task path 不擴」，實際透過 `_check_project_access` 共用收緊一邊）— **security 嚴一點，方向正面**，建議 plan 補述（SC-6）

**Plan 與 implementation 沒走偏**。

## 8. 同源 Code Path 漏網

`grep -rn` + frontend 追蹤 verify 後，**3 條 same-root-cause but unfixed**：

1. **M-R1 (Legacy `_create_mcp_server` / `_update_mcp_server` SSRF guard 漏)** — same root cause as AC #30，但 plan scope 沒列。Frontend `cloud_service.js:587/612/638` 仍主動使用。Hotfix 1-2 line。
2. **M-R2 (paas.py `_resolve_project_access` 對 unbound blanket ROLE_OWNER)** — same root cause as AC #42 (ai_assistant.py 兩層對齊)，但 plan scope 沒明寫 paas.py 那條。Hotfix 1 line + docstring 更新。
3. **SC-13 (cloud_service.py:_model_build_mcp_endpoint_url dead code)** — same root cause as L2 URL helper duplication，code-reviewer grep verify 真的 no caller in `src/`，可一併刪。

其他 plan AC 描述的 implementation 動作（D5 拔、ACL dual-leg、cleanup filter、sidecar-hint endpoint）grep 結果與 CAQA §9 同源 code path 檢查一致 — **無漏網**。

## 9. Known Pitfalls 對照

| Pitfall | 本 PR 重蹈跡象 | 評估 |
|---------|---------|------|
| [[cross-workspace-task-data-leak]] (B1) | ✗ | sub-model parity (mcp_tool 對等改 dual-leg) + dual-leg shape 守 invariant；H.4 / H.6 cross-workspace deny rule 鎖 ✓ |
| [[system-mcp-create-acl-bypass]] (PR #47 MAJOR 1) | ✗ | 外層 AND `('scope', '=', 'user')` 鎖 create direction；H.11 / H.12 explicit reproducer `assertRaises(AccessError)` 緊縮 scope ✓ |
| [[direct-unlink-bypass-cleanup-hook]] (PR #47 BLOCKER 2) | ✗ | `unlink()` override filter 同步加 `sidecar_backed` gate；既有 fixture 條件式餵 `sidecar_backed=True` 維持 9-case regression 全綠 ✓ |
| [[plan-vs-odoo-schema-mismatch]] | ✗ | plan 引用 `mcp_server.sidecar_backed` / `project.message_partner_ids` / `workspace.access_ids.user_id` 全存在 ✓ |
| [[tag-scoped-test-misses-full-u-failures]] | ✗ | implementation ack 報 558 tests 全 suite，不是 tag-scoped；MCP scope 0 fail / 0 error ✓ |
| (potential 新) cross-endpoint ACL asymmetric | **YES — M-R2** | paas.py vs ai_assistant.py 兩 layer 對 unbound project ACL semantic 不對齊 — 建議 reporter 新增 pitfall doc `cross-endpoint-acl-asymmetric-divergence.md` 抽 pattern |
| (potential 新) plan-scoped helper but same-source path 漏掉 | **YES — M-R1** | 新加 `_validate_mcp_url` 只裝在 plan AC #30 spec 端點，legacy same-source endpoint 漏。建議 reporter 加 pitfall doc 或 SSRF helper centralization follow-up |

## 10. Recommendation

**Verdict**: **APPROVED WITH CAVEATS** — proceed to Reporter phase with `escalate leader on HC-1 / HC-2 / HC-3` decision pending.

**Leader 需 sign-off**：
- HC-1 / HC-2 / HC-3 是否：
  - **(a) approve with hard caveat** → reporter 階段 surface attack vectors，排 high-priority follow-up issue（與 F4 / F10 cluster 整理）
  - **(b) hotfix in this PR** → 加 paas.py `_resolve_project_access` follower carve-out（1 line）+ legacy SSRF guard（1-2 line）+ H.9 paas.py path test 鎖（1 method）+ `_SyntheticAccess` docstring 更新（4 lines）— 共 ~10 lines code + 1 test method
  - **(c) escalate user** → 由 user 拍板 scope creep 是否擴 vs PR ship 平衡

**Reviewer 自評偏好**：建議 **(b) hotfix in this PR**，理由：
- M-R1 / M-R2 都是 reproducible security MAJOR finding
- Hotfix scope 小（~10 lines + 1 test method），不破壞 plan B 案 + 44 AC 落地
- M-R2 docstring 已 stale 必修（提到 D5 已刪）— 不修 doc-code drift 後續會誤導
- M-R3 透過 fix M-R2 連帶 close — 一石二鳥
- 既有 H.9 endpoint test fixture 可復用 — test 補只是改 endpoint URL

**Reviewer 自評不偏好 (a)**：
- 兩條 attack surface 真實，pre-existing 不代表可以拖；hotfix cost 低
- silent ACL divergence + same-source SSRF 在兩條 surface 同時存在，攻擊者可以鏈 attack（透過 M-R2 對 victim unbound project 注入 MCP row + 利用 M-R1 漏網的 sync trigger SSRF）— 雙 surface 一起 fix 防鏈攻擊

**最終由 leader 拍板**。

## 11. Sign-off (Round 1)

Reviewer phase verdict：**APPROVED WITH CAVEATS** — proceed to Reporter phase OR fix-loop back to implementation phase per leader decision on HC-1 / HC-2 / HC-3。

Reporter phase 需處理：
- SC-1 ~ SC-20 全 20 條 soft caveat
- HC-1 / HC-2 / HC-3 若 leader 拍 (a) → 排 follow-up issue + surface attack vectors in PR description
- HC-1 / HC-2 / HC-3 若 leader 拍 (b) → fix-loop 回 implementation 補 hotfix，後 reviewer 再過 spot-check
- 同時 close issue #49 + #51（plan §Implementation phasing #7 已 spec）
- PR description draft（plan §7 已 spec）

Reviewer 簽核：**mcp-refactor-reviewer**，2026-05-21

---

# Appendix A — Round 2 Minimal Verify (post-hotfix)

**Trigger**：user 拍板 (b) hotfix in this PR → implementation + tester fix-loop 完成 → scoped reviewer 不重跑 5-agent。

**Test cmd 結果**：569 tests / 8 failed / 28 errors = 36 baseline 紅持平 / 0 MCP red / 0 HC red.

## A.1 Final Round 2 Verdict

**APPROVED WITH CAVEATS** — HC-1 / HC-2 / HC-3 全 closed，1 條 new MINOR finding（與 round 1 SC-6 同 pattern，不擋 merge）。可進 Reporter phase。

## A.2 HC Closure Status

### HC-1: Legacy SSRF guard 漏裝 → **✓ CLOSED**

- **paas.py:1353** (`_create_mcp_server`)：`_validate_mcp_url(url)` call 在 name/url required check 之後、sudo create 之前。**validate-then-create order** correct（reverse 會 leak row）
- **paas.py:1387-1390** (`_update_mcp_server`)：`if 'url' in params:` partial-update gating 後 call `_validate_mcp_url`。**Idempotent + 與 `_update_project_mcp_server`:1719 對等**（只在 url 寫入 path 驗，其他 partial field write 不冗驗）
- Pattern parity verified：與 `_create_project_mcp_server`:1681 / `_update_project_mcp_server`:1719 同 idiom
- **HC-1 test coverage**（`test_mcp_external_url_creation.py:343-568` `TestLegacySsrfCheckOnMcpUrlCreate`）：
  - `test_legacy_create_mcp_server_rejects_aws_metadata_ipv4` — create + IPv4 IMDS，含 **row-not-committed sanity check**（line 467-478）
  - `test_legacy_update_mcp_server_rejects_aws_metadata_ipv4` — update + IPv4 IMDS，含 **original URL preserved sanity check**（line 509-516）
  - `test_legacy_create_mcp_server_rejects_ipv6_link_local` — create + `[fe80::1]`
  - `test_legacy_create_mcp_server_rejects_unsupported_scheme` — create + `file:///etc/passwd`
- 4 tests 都用 `_assert_rejected` helper 鎖「URL host not allowed」wording（與 new endpoint AC #31 對齊）

### HC-2: paas.py `_resolve_project_access` 對 unbound blanket ROLE_OWNER → **✓ CLOSED**

- **paas.py:1495-1519** (`_resolve_project_access`)：
  - line 1511-1515：`not workspace` case 加 follower check — `if user_partner in project.message_partner_ids: return project, _SyntheticAccess(role=ROLE_OWNER)`；deny return `(None, None)`
  - Caller `api_support_project_detail:1538-1539` + `api_support_project_mcp_servers:1620-1622` 統一回 `_PROJECT_NOT_FOUND_ERROR` 防 id enumeration
- **`_SyntheticAccess` docstring (line 1481-1491)** 更新 — 不再 stale 引用刪除的 D5；明文「Issued only after the unbound carve-out... has already verified that the current user is a project follower — the role is granted to a scope-resolved user, not to anyone authenticated」+「Mirrors the follower carve-out in ai_assistant.py:_check_project_access; both layers must stay in lock-step」
- **`_resolve_project_access` docstring (line 1496-1505)** 更新 — 明文「Unbound projects (no cloud_service / workspace) require the current user to be in `project.message_partner_ids` (follower carve-out, in lock-step with ai_assistant.py)」
- **HC-2 test coverage**（`test_mcp_acl_unbound_isolation.py:849-1060` `TestPaasEndpointUnboundNonFollower`）— 3 explicit endpoint tests：
  - `test_endpoint_paas_resolve_project_access_unbound_non_follower_update_denied`（**M-R2 attack chain step 1**）— Dave POST `/api/support-projects/<unbound_pid>` action=update → assert `error == 'Project not found or access denied'` + 統一 enum + project name unchanged sanity
  - `test_endpoint_paas_resolve_project_access_unbound_non_follower_get_denied`（**M-R2 read-side mirror**）— Dave action=get → same enum
  - `test_endpoint_paas_project_mcp_servers_unbound_non_follower_create_denied`（**M-R2 attack chain step 2**）— Dave POST `.../mcp-servers` action=create → same enum
- 3 tests 都用 `assertEqual(result.get('error'), 'Project not found or access denied')` **strict equality** lock 防 enum 洩漏（與 PR #53 AC #6/#11 wording 對齊）

### HC-3: OR-X9 32-case write/unlink direction 對 unbound implicit-only → **✓ CLOSED**

- **`test_mcp_acl_unbound_isolation.py:438-526`** — H.3b / H.4b + cross-ws W/U 4 explicit tests：
  - `test_workspace_member_non_follower_cannot_write_unbound_project_user_mcp_via_orm` — Dave (W1 member non-follower) `write({'name': '...'})` → `assertRaises(AccessError)`
  - `test_workspace_member_non_follower_cannot_unlink_unbound_project_user_mcp_via_orm` — Dave `unlink()` → `assertRaises(AccessError)`
  - `test_cross_workspace_user_cannot_write_unbound_project_user_mcp_via_orm` — Bob (W2 cross-ws) write → `assertRaises(AccessError)`
  - `test_cross_workspace_user_cannot_unlink_unbound_project_user_mcp_via_orm` — Bob unlink → `assertRaises(AccessError)`
- `assertRaises` block 緊縮 scope（1 statement per block，避免 false-negative catch unrelated error）
- 後續 PR 改 leg 2 perm_write / perm_unlink 會立刻 trip 此 4 條 lock

### Cascading Fixture Fix: `alice.message_subscribe(unbound_project)` → **✓ Scoped + Safe**

- **`test_mcp_sidecar_hint_endpoint.py:156-161`** — `setUp` 加 Alice subscribe `project_unbound` follower
- **Rationale**（comment in test）：HC-2 hotfix 後 `_resolve_project_access` 對 unbound non-follower 統一回 `Project not found or access denied` → 原本要測 AC #27 "No sidecar available for this project" wording 的 positive case test，需先讓 Alice 通過 follower check 才能 reach sidecar-hint action branch
- **Safety**：cross-check 同檔其他 tests — `project_unbound` only appears at line 156/159/284；無其他 test 依賴 Alice NOT subscribed 狀態
- **不誤殺 negative case**：HC-2 endpoint tests (paas.py 上方) + H.3 / H.7-H.9 (ai_assistant.py path) 各自獨立 fixture，用 Dave / Bob / Eve 角色當 non-follower — 與 sidecar-hint 的 Alice fixture 完全不重疊

## A.3 Attack Chain Closure Confirmation

| Step | Attack | Defense (post-hotfix) | Test Lock |
|------|--------|----------------------|-----------|
| Step 1 (update) | Dave POST `/api/support-projects/<W1_unbound_pid>` action=update | `_resolve_project_access` follower check deny → `(None, None)` → unified enum | `test_endpoint_paas_resolve_project_access_unbound_non_follower_update_denied` |
| Step 1 (read leak) | Dave action=get → information disclosure via `_format_project_detail` | 同上 | `test_endpoint_paas_resolve_project_access_unbound_non_follower_get_denied` |
| Step 2 (MCP injection) | Dave POST `.../mcp-servers` action=create → `McpServer.sudo().create({...})` bypass ir.rule leg 2 | `_resolve_project_access` follower check 在 action branch 前 deny | `test_endpoint_paas_project_mcp_servers_unbound_non_follower_create_denied` |
| Step 3 (SSRF chain) | 若 attacker 透過 legacy `/api/workspaces/<wid>/services/<sid>/mcp-servers` create row with `http://169.254.169.254/...` URL → 後續 sync/test trigger outbound HTTP | `_validate_mcp_url` 在 create + update 兩條 path validate-then-write order | 4 × HC-1 tests |

**Attack chain 全 closed** — Step 1 + Step 2 + Step 3 都有 explicit test lock + runtime enforcement。

## A.4 Round 2 New Findings

### NM-R1 (MINOR): paas.py `_resolve_project_access` 對 bound-no-workspace edge case silent 收緊（與 ai_assistant.py 不完全 lock-step）

- **位置**：`src/controllers/paas.py:1510-1515` (`_resolve_project_access`) vs `src/controllers/ai_assistant.py:53-58` (`_check_project_access`)
- **觀察**：
  - paas.py `_resolve_project_access:1510-1515` `if not workspace:` 同 treatment 涵蓋 **2 case**：
    - (i) unbound project (`cloud_service_id IS NULL`) — follower check
    - (ii) bound but `cloud_service.workspace_id IS NULL` — **同樣 follower check**（silent 收緊比 PR #53 既有 blanket ROLE_OWNER 更嚴）
  - ai_assistant.py `_check_project_access:53-58`：
    - (i) `if not project.cloud_service_id:` → follower check ✓
    - (ii) `if not project.cloud_service_id.workspace_id: return True` — **blanket allow**（CAQA M2 pre-existing）
  - **`_SyntheticAccess` docstring 寫 "Mirrors the follower carve-out in ai_assistant.py:_check_project_access; both layers must stay in lock-step"** — 對 case (i) 達成 lock-step ✓；對 case (ii) **drift**（paas.py 更嚴）
- **嚴重度**：MINOR
  - Edge case「bound service exists but service.workspace_id IS NULL」在實際 PaaS 平台幾乎不會發生（CAQA M2 already ack 此 caveat）
  - paas.py 比 ai_assistant.py 更嚴 → **security 方向正面**
  - 與 round 1 silent-failure-hunter SC-6 (`_check_task_access` propagation 與 plan AC #43 wording silent 收緊) 是 **同 pattern**（本 round 是 paas.py mirror）
- **不擋 merge** — 標 **SC-6b reporter follow-up**（與 CAQA M2 + plan AC 補述 + SC-6 task path 合併處理）
- **建議**：reporter 階段選一條路徑（任一即可）：
  - (a) paas.py 拆 case：`if not project.cloud_service_id:` 走 follower check，`if not project.cloud_service_id.workspace_id:` 與 ai_assistant.py 對齊 blanket allow
  - (b) ai_assistant.py 同步收緊 bound-no-workspace（CAQA M2 follow-up 一併處理）
  - (c) docstring 補一句 caveat 明示「lock-step only for unbound; bound-no-workspace edge case left blanket-allow in ai_assistant.py per CAQA M2 follow-up」

## A.5 Round 2 Sign-off

Reviewer phase round 2 verdict：**APPROVED WITH CAVEATS** — HC-1 / HC-2 / HC-3 全 closed ✓；1 條 new MINOR finding (NM-R1 / SC-6b) 不擋 merge。可 proceed to Reporter phase。

Reporter phase 需處理（在 round 1 SC-1~SC-20 之上新增）：
- **SC-6b**: paas.py `_resolve_project_access` 對 bound-no-workspace edge case 與 ai_assistant.py drift — 建議與 CAQA M2 + plan AC 補述 + SC-6 合併處理

Reviewer 簽核（round 2）：**mcp-refactor-reviewer**，2026-05-21
