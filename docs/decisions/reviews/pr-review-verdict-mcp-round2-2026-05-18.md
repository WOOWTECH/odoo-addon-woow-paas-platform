---
name: pr-review-verdict-mcp-round2-2026-05-18
phase: review
round: 2
team: refactor
reviewer: refactor-reviewer
round1_verdict_ref: docs/refactor/pr-review-verdict-mcp-2026-05-18.md
plan_ref: docs/refactor/plan-mcp-project-decouple.md
decision_ref: docs/refactor/decisions/mcp-project-decouple.md
caqa_ref: docs/refactor/caqa-2026-05-18.md
diff_range: e9a5927..HEAD (4 commits: 4ff8f4b docs / fdf51fc refactor / cf34a65 fix / 97b5313 test)
created: 2026-05-18T15:30:22Z
updated: 2026-05-18T15:30:22Z
status: approved-with-caveats
---

# PR Review Verdict (Round 2) — MCP `cloud_service_id → project_id` Decouple

> 對象：phase-leader / reporter。Round 1 verdict (`pr-review-verdict-mcp-2026-05-18.md`, commit `e9a5927`) NEEDS REVISION 提的 2 BLOCKER + 2 MAJOR 全 close。Reviewer round 2 透過 `/pr-review-toolkit:review-pr` 雙 sub-agent（code-reviewer + pr-test-analyzer）平行 verify，加上 reviewer 獨立 grep / Read，三邊鎖死。

## Verdict

**✅ approved with caveats** — proceed to **reporter phase**。

Round 1 4 finding 全 closed；無新 blocker/major；4 條 minor 由 reporter 列 follow-up doc 即可。

## 工具 raw 輸出摘要

Round 2 只 launch 2 個 sub-agent（scope 受限於 4 specific findings，不需 5 agent 全跑）：

| Agent | 主要產出 | 結論 |
|---|---|---|
| `pr-review-toolkit:code-reviewer` | 4 finding 全 CLOSED + 1 info caveat + 5 strengths | approved with caveats |
| `pr-review-toolkit:pr-test-analyzer` | 4 regression locks robust + 2 minor | approved with caveats |

Multi-agent **無分歧**。Out-of-scope 42 reds 判定 sound（兩邊獨立 verify `test_cross_workspace_task_isolation` / `test_project_decouple` 在 round 2 commit range 內 0 改動）。

## Per-finding closure status

| Finding | Severity | Owner | Fix commit | Round 2 status | Evidence |
|---|---|---|---|---|---|
| BLOCKER 1 (10 stale refs in test_mcp_integration.py) | 🔴 | tester | `97b5313` | ✅ CLOSED | grep `mcp_server.cloud_service_id` 0 hits；剩下都是 `project.cloud_service_id` 合法 chain；`_create_service` helper 自動建 bound project 守 D5；renamed cascade test 對齊新 schema |
| BLOCKER 2 (direct unlink → zombie MCP) | 🔴 | impl | `cf34a65` | ✅ CLOSED | `cloud_service.py:1176-1201` `unlink()` override catches all 4 paths（paas.py:1953/1966/2156 + cron self-unlink at `:787`）；cleanup BEFORE super().unlink() 順序正確；2 regression test (`test_blocker2_unlink_override_cleans_user_mcp_without_do_cleanup` + `test_blocker2_unlink_override_skips_system_mcp`) 鎖正反兩面 |
| MAJOR 1 (system create data leak) | 🟡 | planner+impl | `fdf51fc` + `4ff8f4b` | ✅ CLOSED | 6-rule split 對等（3 mcp_server × 3 mcp_tool 鏡像，全 verified line 134/149/163/176/194/205）；`system_readonly.perm_create=False` 守住 ORM create scope='system' 攻擊；`mcp_server.scope` 預設 `'system'`，explicit-omit 也被擋；migration step 5 retire 舊 dual-leg rule（DELETE 順序對：rule rows → ir_model_data pointers）；6 regression test (`test_user_cannot_create_system_mcp_via_orm` + `test_user_cannot_create_mcp_tool_for_system_server` + WRITE/UNLINK + sudo + admin + over-correction) cover 全攻擊面 |
| MAJOR 2 (helm fail state) | 🟡 | impl | `cf34a65` | ✅ CLOSED | `cloud_service.py:1162-1171` set `state='error' + error_message` then `return` — mirrors `_do_suspend` (line 1070) / `_do_resume` (line 1106) pattern；D1 ordering 保留（user-scope MCP 已 unlink in Step 1 before helm attempt）；regression test (`test_major2_do_cleanup_helm_fail_sets_error_state`) 4 assertion 獨立鎖：state='error' + state!='deleting' + error_message + half-success MCP cleanup |

## 各檢查點結論

### B1-style ACL 對等 deny enforce（mcp_server + mcp_tool）

**✅ 對等正確（在新 6-rule design 下仍 enforce）**。

```
mcp_server_rule_system_readonly  ↔  mcp_tool_rule_system_readonly
mcp_server_rule_workspace_user   ↔  mcp_tool_rule_workspace_user
mcp_server_rule_admin_full       ↔  mcp_tool_rule_admin_full
```

3 對對等 rule 都正確 mirror，mcp_tool domain 透過 `server_id.` 反向 walk。新 Invariant #5（decision doc 已 update）明文要求「四條 rule 對等」（不含 admin_full bypass，因為它對 group_system 不是 B1 風險面）。

**Code-reviewer 點到的 follow-up caveat**：無 automated lint 檢查 mcp_server / mcp_tool rule-count parity。Reporter 列入 follow-up doc：「`ir_rules.xml` 內 mcp_server rule 數量 ≠ mcp_tool rule 數量視為 MAJOR until proven safe」。

### 整體 commit history 順序是否清晰反映 re-loop chain

**✅ 清晰**。

```
e9a5927 docs(refactor): publish reviewer verdict for MCP cloud_service_id -> project_id  ← round 1 verdict
4ff8f4b docs(refactor): MAJOR 1 rule split fix - system MCP create deny                   ← planner sign-off (Inv #5/#6 重寫 + Inv #8 新增)
fdf51fc refactor: MCP ACL rule split for system create deny                               ← MAJOR 1 impl + migration step 5
cf34a65 fix: cloud_service unlink override + cleanup helm fail state                      ← BLOCKER 2 + MAJOR 2 一輪解
97b5313 test(refactor): re-loop tester fix for reviewer round-2 NEEDS REVISION            ← BLOCKER 1 port + 9 regression test
```

順序：reviewer verdict → planner doc → impl ACL → impl unlink/state → tester regression — 標準 fix loop chain。

### Out-of-scope 42 reds 判定是否 sound

**✅ Sound**。Multi-agent 雙 verify：
- `test_cross_workspace_task_isolation` 最後改動在 `d570006`（pre-baseline 12cf255），round 2 commit range 內 0 改動 — failure pattern 是 B1 task regression，root cause 在 migrations `18.0.1.0.22/0.23` std-rule deactivation timing，與本次 `18.0.1.0.24` 無關
- `test_project_decouple` AC4 同樣 pre-baseline，round 2 0 改動
- Round 2 commit range 只動 MCP scope files（test_mcp_integration.py / test_mcp_acl_isolation.py + 5 production files），42 reds 是 non-MCP test families（subscription / OAuth / smart_home）— 無 blast radius

**Implementation 的 baseline survey 方法（git stash → re-run baseline → 同 reds reproduce）方法學健全**。

B1 task pre-existing → next refactor cycle `18.0.1.0.25` task ACL deactivation pattern（對齊 `18.0.1.0.23` project_public_members_rule pattern）— **不擋本次**。

### `/simplify` 後是否還有可簡化處（round 1 提的）

Round 2 沒處理 round 1 已標的 simplification suggestion（controller manual check helper 抽取 + `_get_user_mcp_tools` mixin 抽取）— 屬於 follow-up，不擋。Reporter 維持列在 follow-up doc。

## 新 minor / info（reporter follow-up）

| # | Item | Severity | Found by | Reporter action |
|---|---|---|---|---|
| R2-1 | `_do_cleanup` retry-on-error semantics 未文件化：subscription cron 在 cancel 後再 trigger `_do_cleanup` 是 retry-friendly 設計（state='error' 不擋）— 後人若加 `if state == 'error': return` short-circuit guard 會打破 auto-retry | info | code-reviewer | doc invariant 在 [[follow-ups]] |
| R2-2 | mcp_server / mcp_tool rule-count parity 無 automated lint check（依賴人眼 / Invariant #5 doc） | info | code-reviewer | 列 [[follow-ups]] |
| R2-3 | `_do_cleanup` 在 `state='error'` 時 re-entry 行為未鎖 test | minor | pr-test-analyzer | 列 [[follow-ups]]（reporter 評估是否要新 test case） |
| R2-4 | `_create_admin_user` helper 的 docstring 對 `(4, ...)` stale-cache 行為的 claim 是 unverified Odoo 民俗，但用 `(6, 0, ...)` 是 idiomatic — 無害 over-cautious | trivial | pr-test-analyzer | 可不處理或 reporter 順手清 docstring |

合計 **4 條新 minor / info**，全 reporter 級 follow-up，**無 blocker / major**。

## Round 2 強項（worth calling out）

來自 code-reviewer 的 5 strengths（reviewer 同意）：

1. **Rule split rationale 嚴謹** — `ir_rules.xml:119-128` 帶 inline justification 連回 MAJOR 1 finding；`ir_rules.xml:187-193` 解釋 `group_system` admin bypass rule 的必要性（Odoo group inheritance OR-semantics）。後人想合併 4 條回 dual-leg 會看到 "MAJOR 1 fix" 警示
2. **Migration step 5 idempotency** — `_column_exists` introspect + silent zero-row DELETE 確保 re-run 安全；DELETE 順序正確（rule rows via `res_id` lookup → ir_model_data pointers — 反過來會 lose lookup key）
3. **Regression test 具體性** — 6 MAJOR 1 cases 每條獨立 + over-correction guard (`test_user_can_still_create_user_scope_in_own_workspace`) 鎖 defensive over-correction 風險。後人若把 system_readonly domain 改寬 + perm_create=True，這條 test 會 red
4. **D1 ordering invariant 在 failure path 保留** — `_do_cleanup` MAJOR 2 fix 保 "unlink MCP BEFORE helm" 順序，docstring 明文 half-success 狀態（line 1127-1130）— 解掉 round 1 reviewer 提的 edge case 要求
5. **Decision doc cross-link 清晰** — Invariant #6 (decision doc:187) 區分「read direction always-true 是設計」vs「create/write/unlink always-true 是 MAJOR 1 hole」，並與 B1 std rule always-true 性質明文對照 — 防後人誤讀為 B1-class leak

額外 reviewer 直接觀察：

6. **`mcp_server.scope` 預設值 `'system'` 不開後門** — 即使 user 走 `Mcp.create({...})` 不寫 scope，會落到 default scope='system' → `system_readonly.perm_create=False` 擋下 → AccessError raise。Default 不是攻擊面。Code-reviewer 獨立驗證
7. **Tester 選 Option A (port) 而非 Option B (retire)** — 我 round 1 建議 B，但 A 加上 9 個新 regression test 更完整（保留既有 test 名 + 新增 BLOCKER/MAJOR regression）。Tester 判斷對

## 17 minor + Inv #7 wording + CAQA typo follow-up

Round 1 的 17 minor / info 與 CAQA 已知 caveat（C1 / F12 / Inv #7 / 21→25 AC typo）— 本 round 不擋，reporter phase 列入 [[follow-ups]] doc：
- F12（controller helm_values 漏 sanitize）：leader round 1 已裁示不擴 scope
- Inv #7（grep wrong file）：reporter finalize 改 wording
- CAQA "21 AC" typo（caqa-2026-05-18.md:17, 338）：reporter 一併修
- 其他 13 條 minor：silent-failure / type-design / test coverage hole — reporter 評估 batch 或留 next cycle

Round 2 新加 4 條（R2-1 ~ R2-4，見上表）。

## Recommendation（給 leader）

1. **Verdict: approved with caveats — proceed to reporter phase**
2. **Reporter 任務**：
   - 列入 [[follow-ups]]：R2-1（cleanup retry 文件化）+ R2-2（rule parity lint）+ R2-3（state='error' re-entry test）
   - Round 1 17 minor + Inv #7 + CAQA "21 AC" typo 一併 surface
   - B1 task next refactor cycle `18.0.1.0.25` 標 next-cycle 不擋本次
3. **無 round 3 需求** — 4 finding 全 close，無新 blocker/major，可直接 ship reporter phase artefacts

Reviewer 結案進 standby。

## See also

- [[pr-review-verdict-mcp-2026-05-18]] — round 1 verdict（NEEDS REVISION，含完整 17 minor list）
- [[mcp-project-decouple]] — decision draft（Inv #5/#6 重寫 + Inv #8 新增，round 2 fdf51fc/4ff8f4b 同步更新）
- [[caqa-2026-05-18]] — CAQA 報告
- [[cross-workspace-task-data-leak]] — B1 pitfall（reviewer round 2 確認新 6-rule split 仍守住 B1 sub-model parity）
- `docs/refactor/plan-mcp-project-decouple.md` — 25 AC plan（round 2 4ff8f4b 同步更新 MAJOR 1 fix）
- `docs/refactor/test-coverage-mcp-project-decouple.md` — coverage report

---

**End of round 2 review report. Hand back to refactor-leader.**

Reviewer 進 standby。本次 refactor cycle reviewer phase 收尾。
