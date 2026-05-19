# Follow-ups — PaaS refactor cycle 追蹤

> 本 doc 累積跨 refactor cycle 沒處理 / 刻意延後的事項。每條附 (severity, scope origin, 動手建議, 對誰處理)。Reporter 不解決，列冊讓未來 PR / issue 持續追蹤。

**State as of 2026-05-18**：
- Cycle 1（cloud-service ↔ project decouple, 2026-05-17）：F1-F11，B1 已 RESOLVED、PR APPROVED WITH CAVEATS
- Cycle 2（mcp cloud_service_id → project_id decouple, 2026-05-18）：F12-F20 新增，PR APPROVED WITH CAVEATS round 2，4 BLOCKER/MAJOR 全 close

## Index

| # | Severity | Origin | Cycle | 主題 | Status |
|---|----------|--------|-------|------|--------|
| F1 | medium | refactor | 1 | ACL 對等性 checklist 持久化 | doc-only ✅（本 doc 已寫進 [[odoo-addon-std-rule-symmetric-deactivation]]） |
| F2 | medium | refactor | 1 | `_sanitize_helm_values` 同名異實重命名 | 待新 PR |
| F3 | medium | refactor | 1 | Cross-workspace incompatible extras 注 AI prompt | 待設計 |
| F4 | medium | pre-existing | 1 | `_check_project_access` / `_check_task_access` 對 unbound IDOR | 待新 PR |
| F5 | medium | refactor (deferred) | 1 | Migration xmlid drift 防呆 assertion | 使用者 accept 不加本 PR；待新 PR |
| F6 | medium | refactor | 1 | D9 階段 2 — sanitizer 升級 whitelist 模式 | 等 telemetry，待新 PR |
| F7 | minor | refactor | 1 | BottomNav mobile vs Sidebar D4 拆分後 UX 一致性 | 待設計 |
| F8 | minor | refactor (test) | 1 | Test hardening — AC#11 / AC#14 / B1 group_project_user / sanitizer fallback | 待新 PR |
| F9 | minor | refactor | 1 | Sanitizer / `_loadCloudServices` 等 silent fallback observability | 待新 PR |
| F10 | minor | pre-existing | 1 | `_list_projects` with sudo + empty domain 跨 tenant 列 | D5=A 放大；待新 PR |
| F11 | minor | refactor | 1 | Sentinel `***REDACTED***` i18n 化 | 由 F6 觸發後一起評估 |
| F12 | medium | pre-existing | 2 | Controller `helm_values` 漏 sanitize（違反 cycle 1 Inv #6） | 與 F2 合併 or 獨立 hotfix |
| F13 | medium | refactor | 2 | B1 task ACL deactivation 對齊 `18.0.1.0.23` pattern | next refactor cycle `18.0.1.0.25` |
| F14 | medium | refactor (caveat) | 2 | Cleanup hook `mapped` 路徑 belt-and-braces `sudo()`（CAQA C1） | 待新 PR |
| F15 | info | refactor | 2 | `_do_cleanup` retry-on-error semantics 文件化（R2-1） | doc invariant inline + [[mcp-project-decouple]] §Inv |
| F16 | info | refactor | 2 | `mcp_server` / `mcp_tool` rule-count parity lint check（R2-2） | 待新 PR |
| F17 | minor | refactor (test) | 2 | `state='error'` re-entry test case（R2-3） | 待新 PR |
| F18 | trivial | refactor | 2 | `_create_admin_user` helper docstring 過 cautious（R2-4） | reporter 順手清 or 留 |
| F19 | minor | refactor | 2 | Round 1 17 minor / info 集合（silent-failure / type-design / migration assertion）| 留 batch 處理 |
| F20 | minor | refactor (simplify) | 2 | Controller manual check helper 抽取 + `_get_user_mcp_tools` mixin | 待新 PR |

下方逐條細節 + 觸發條件 + 建議落點。F1-F11 維持 cycle 1 原文。F12+ 是 cycle 2 新增。

---

## F1 — ACL 對等性 checklist 持久化（doc-only）

- **Severity**: medium（不處理會在後續類似 PR 再炸 B1-class leak）
- **Status**: ✅ 已透過本批 docs 處理：
  - Pattern: [[odoo-addon-std-rule-symmetric-deactivation]]
  - Pitfall: [[cross-workspace-task-data-leak]]
  - Decision invariant: [[cloud-service-project-decouple]] §Invariants 4-5
- **下一步**：[[propose-claude-md-known-patterns]]（提議 leader / 使用者把 pattern doc 引用加進 CLAUDE.md，讓未來 dev / agent 改 ACL 時自動 surface）

---

## F2 — `_sanitize_helm_values` 同名異實重命名 / 抽 utils

- **Severity**: medium（語意混淆風險，不是 functional bug）
- **Origin**: 本次 refactor 新加的 `models/discuss_channel.py:41` `_sanitize_helm_values`（AI prompt sanitizer，pattern + blacklist + `***REDACTED***`）與 pre-existing `controllers/paas.py:1460` `_sanitize_helm_values`（API response sanitize，`secret` key 下 truncate `[:8] + '...'`）同名異實
- **影響**：未來維護者 grep `_sanitize_helm_values` 拿到兩個結果，可能誤用一個 sanitizer 在錯誤的 surface（如把 API sanitizer 用在 AI prompt 上 → 漏 password / token；或把 AI sanitizer 用在 API response → 改變既有前端 behavior）
- **建議落點**：
  - 重命名 `models/discuss_channel.py::_sanitize_helm_values` → `_sanitize_helm_values_for_ai_prompt`，並同步調整 callers
  - 或抽出共用 module-level helper（如 `src/services/helm_values_sanitizer.py`），明確兩個 mode（`for_api_response` / `for_ai_prompt`），controller / discuss_channel 都改 import
- **何時**：下次任一邊 sanitizer 邏輯需要動的 PR 順手做（不需要單獨 PR）

---

## F3 — Cross-workspace incompatible extras 仍注入 AI prompt

- **Severity**: medium（非 secret leak，但違反多租戶資訊隔離精神）
- **Origin**: 本次 refactor 對 task cross-project move 設計成「extras 保留」（AC #13），但 `_get_cloud_service_context` 走 `sudo()` 取 extras（`src/models/discuss_channel.py:267-280`），sanitizer 只去 secret 不去 cross-workspace
- **觸發路徑**：
  1. 使用者 A 在 W1 為 task 加 W1 服務 X 為 extras
  2. Task 被 cross-project move 到 W2（B 的 project）
  3. W2 member 在該 task chatter 觸發 AI 自動回覆
  4. AI prompt 內包含 X 的 service.name / subdomain（公開域名）/ template name / sanitized helm_values
- **洩漏內容**：service.name（可能含租戶業務語意）、subdomain、template name、helm_values（secret 已 redact 但保留 chart 配置）
- **建議落點**：
  - `_get_cloud_service_context` 過濾 `incompatible_extra_cloud_service_ids`，cross-workspace extras 即使存在也不注入
  - 或對 cross-workspace extras 注入時降級為「placeholder 提示 + service name only」（無 helm_values / subdomain）
- **設計待議**：兩個方向各有 UX tradeoff（過濾 = AI 不知道有 extras / placeholder = 部分資訊但複雜），由使用者決定

---

## F4 — `_check_project_access` / `_check_task_access` 對 unbound project 全放行（IDOR）

- **Severity**: medium（pre-existing IDOR；本次 refactor **放大 surface**）
- **Origin**: `src/controllers/ai_assistant.py:43-61` 對 `project.cloud_service_id IS NULL` 直接 return True，跳過所有 access check
- **Pre-refactor 情況**：unbound project 前端阻擋無法建立，路徑無實際 trigger
- **Post-refactor 放大**：D5=A 拔前端 gate，unbound project 可正常建立 → IDOR surface 浮上水面
- **可觸發 endpoint**：
  - `api_support_projects(action='get/update/delete', task_id=X)` — 對猜中 ID 的 unbound project / task 可直接 R/U/D
  - `_list_projects` / `_list_tasks` 前端不顯示 unbound（但 API endpoint 知 ID 即可操作）
- **建議落點**：
  - `_check_project_access` / `_check_task_access` 對 unbound 改檢查 `project.message_partner_ids` / `create_uid`，與 ir.rule 對齊
  - 或統一改走 ORM 路徑（`with_user(env.user).project.browse(id).check_access('write')`），讓 ir.rule 自動把關
- **何時**：下個 security audit / IDOR cleanup PR；不阻擋本 refactor merge

---

## F5 — Migration xmlid drift 防呆 assertion（deferred）

- **Severity**: medium（隱性風險）
- **Origin**: 本次 refactor reviewer cleanup #6；使用者 Q1 拍板「accept 不加本 PR」
- **問題**：`src/migrations/18.0.1.0.23/post-migrate.py:93-107` 的 3-row UPDATE 無 row-count 防呆。若 Odoo 18.x 某 patch 改 `task_visibility_rule` 等 xmlid，UPDATE 命中 < 3 rows，rowcount 仍 success，B1 leak 靜默回來
- **建議落點**：
  ```python
  cr.execute("""
      SELECT COUNT(*) FROM ir_model_data
      WHERE module='project' AND model='ir.rule'
        AND name IN ('project_public_members_rule',
                     'task_visibility_rule',
                     'task_visibility_rule_project_user')
  """)
  actual = cr.fetchone()[0]
  if actual != 3:
      raise RuntimeError(
          f"Odoo std rule xmlid drift detected: found {actual}, expected 3. "
          "B1 leak might silently return — abort migration. "
          "See docs/tracking/pitfalls/cross-workspace-task-data-leak.md"
      )
  ```
- **何時**：下次 migration touch 或 Odoo 版本升級前必加
- **替代防線**：Pre-deploy verify SQL（已寫進 PR description 的 deploy checklist + [[odoo-addon-std-rule-symmetric-deactivation]]）— 但靠手動跑

---

## F6 — D9 階段 2：sanitizer 升級 whitelist 模式

- **Severity**: medium（pattern 比 whitelist 鬆，但本次先 ship pattern 是對的 — 詳見 [[cloud-service-project-decouple]] §7）
- **Origin**: plan D9 拍板「A → B」，本次只做 A
- **觸發條件**：
  - 階段 1 telemetry 累積夠 dataset（哪些 keys 實際出現在 helm_values、哪些被 redact）
  - Pen-test / security audit 標示「pattern 漏網」實例
  - 或新 chart 上線 PR review 時發現需要更嚴的策略
- **建議落點**：
  - 新 helper `_sanitize_helm_values_whitelist(helm_values_json, allowlist)`，allowlist 由 chart-specific 配置驅動
  - `_sanitize_obj` 改為「key 不在 allowlist → 整 value redact」而非「key 匹配 pattern → redact」
  - 過渡期可雙模式並存，A 與 B 對同一 input 跑後 log diff 找出 pattern 漏網案例
- **何時**：階段 1 已 ship 至少 N 個月 + 有 telemetry 數據後

---

## F7 — BottomNav mobile vs Sidebar D4 拆分後 UX 一致性

- **Severity**: minor（PaaS 主要 desktop）
- **Origin**: `src/static/src/paas/layout/bottom_nav/BottomNav.js:18-24` mobile bottom nav 仍把 `ai-projects` / `ai-project-kanban` 算進 AI 入口；desktop sidebar 已拆出獨立 `Projects` entry
- **症狀**：mobile 上點到 Project Kanban 會 highlight "AI" tab
- **建議**：BottomNav 加 `Projects` entry 對齊 Sidebar；或把 mobile AI tab 內 sub-menu 加上 Projects
- **何時**：mobile UX 下次 sprint，或 D4 sidebar 確認最終 UX 形態時一起調整

---

## F8 — Test hardening

- **Severity**: minor（implementation 都對；test 是「future regression 強守線」缺塊）
- **Origin**: pr-test-analyzer + reviewer R7 / R8
- **缺塊**：
  - **AC #11 domain test 是 string-grep stub**：只 grep `'workspace_id'` literal 出現於 `field.domain.repr()`，未跑真實 ORM filter — 未來 implementation 用 `[('workspace_id', '!=', False)]` 之類無意義 domain 也能過關
  - **AC #14 dedup test format-brittle**：用 `count('Primary Svc') == 1` 偵測 dedup，prompt 模板改字（如 `### Primary Service`）可能繞過 — implementation 用 `extras - primary` recordset 層級 dedup 是對的，但 test 不擋未來 implementation 改成字串層級操作
  - **B1 regression 沒涵蓋 `group_project_user` write/unlink leg via `task_visibility_rule_project_user`**：PaaS 不給租戶 group_project_user，但 paranoia 加 1 條 sibling test 防 future role grant
  - **AC #15 sanitizer 深度 >5 / 非 JSON helm_values fail-closed path 未被測**：implementation `_sanitize_helm_values` 已 fail-close 為 `''`，無 test 守
- **建議**：每條對應加 1 條 test。落在下個 touch testing 的 PR

---

## F9 — Silent fallback observability

- **Severity**: minor（不破功能，但 silent fallback 影響 debug 與 incident 反查）
- **Origin**: silent-failure-hunter R9
- **缺塊**：
  - `_sanitize_helm_values`（`src/models/discuss_channel.py:41-49`）JSON parse 失敗 silently 回 `''`，無 log。Helm chart 把 `helm_values` 存 non-JSON 時 AI 拿不到 context，使用者完全不知為什麼
  - `_sanitize_obj` depth > 5 silently 回 `_REDACTED` literal，與真實 redacted value 無辨識
  - `CreateProjectModal._loadCloudServices` catch 後沒 set `state.error`（不一致於 `_loadWorkspaces`），dropdown 靜默變空使用者誤判「無 service」
  - `_post_ai_reply` 外層 except 把 programming error 跟 transient AI failure 包成同一個 generic notification — 影響 incident triage
- **建議**：逐條加 `_logger.warning` + observable signal（log level 降到 warning 足夠，不是錯誤）
- **何時**：observability 下次 sprint 或下個 touch 對應檔案的 PR

---

## F10 — Pre-existing `_list_projects` with sudo + empty domain 跨 tenant 列

- **Severity**: minor（pre-existing 不在本 refactor 引入）
- **Origin**: reviewer 偽陽性 verification 順帶發現
- **狀況**：`_list_projects` 在某 endpoint 用 `domain=[]` + `sudo()` browse → 跨 tenant 列所有 project；前端 `SupportProjectsPage` 呼叫 `fetchAllProjects()` 走無 workspace 過濾的 endpoint
- **與本次 refactor 的關係**：D5=A 拔前端阻擋後**放大可見度**（以前看不到的 unbound project 現在會出現在頁面），但 leak shape 是 pre-existing 的 controller-level filter gap，不是本次 refactor 引入
- **建議**：`_list_projects` 改走 ORM 路徑用 `with_user(env.user)`，依靠 ir.rule（已收緊）自動隔離 — 一致於 [[paas-workspace-member-acl-tightening]] 的整體方針
- **何時**：與 F4 IDOR cleanup 同一個 PR 處理較自然（都是 ai_assistant.py controller）

---

## F11 — Sentinel `***REDACTED***` i18n 化

- **Severity**: minor（純 audit token）
- **Origin**: plan §Open Q4 / PR description follow-up
- **判斷**：本次拍板 hardcode `***REDACTED***` 是對的（[[cloud-service-project-decouple]] §9 Q4）— 它是 audit grep-able token，不是 user-facing 文案
- **何時觸發**：若 F6（D9 階段 2 whitelist）執行時順帶評估是否需要 i18n（whitelist 模式可能會把 redacted value 暴露給更多 surface）
- **建議**：F6 PR 內一併評估；若決定 i18n 化，要同步改 plan / test / pitfall doc 內所有 `***REDACTED***` literal 引用

---

---

# Cycle 2 — MCP `cloud_service_id → project_id` Decouple (2026-05-18)

> Cycle 2 完成於 2026-05-18，commit chain `5c5d980 → 9f147d8`。Reviewer round 2 APPROVED WITH CAVEATS。新 F12-F20 條目來自 CAQA C1 / F12 + round 1 verdict 17 minor + round 2 verdict 4 new minor + reporter durable docs 寫作時順帶整理。

## F12 — 控制器 `helm_values` 漏 sanitize（pre-existing，違反 cycle 1 Inv #6）

- **Severity**: medium
- **Origin**: CAQA `docs/decisions/reviews/caqa-2026-05-18.md §F12` out-of-scope finding（CAQA 跨 cycle 1 + cycle 2 漏抓兩次）
- **Location**: `src/controllers/ai_assistant.py:609-610` — SSE streaming path 注入 `service.helm_values` 到 AI system prompt **未走 `_sanitize_helm_values`**；對比 `src/models/discuss_channel.py:303` non-streaming path 有 sanitize
- **Violation**: cycle 1 Invariant #6（AI prompt 注入 `helm_values` 必須經 `_sanitize_helm_values`）
- **為何沒在本次擋下**：本次 implementation diff 未改此段 code，無新增 surface。本次 verdict 評估「是否引入 / 加劇」— pre-existing 不擋
- **觸發路徑**：user 開啟 SSE chat 且 task 連到 bound project + `chat_enabled=True` + `ai_auto_reply=True` 才會 fire（非任何 user 隨意觸發）
- **建議落點**：
  - 路徑 1：與 F2（同名異實重命名 / 抽 utils）合併處理，徹底解決
  - 路徑 2：若評估 AI chat 已上 stg / 即將 prod，獨立 hotfix PR 立刻修（機械式 1-line fix：`service.helm_values` → `_sanitize_helm_values(service.helm_values)` 並 import）
- **何時**：next AI chat 相關 PR 一併處理

## F13 — B1 task ACL deactivation 對齊 `18.0.1.0.23` pattern（next refactor cycle `18.0.1.0.25`）

- **Severity**: medium
- **Origin**: round 2 verdict 42 reds baseline survey（commit 9f147d8 §Out-of-scope 42 reds）
- **狀況**：`test_cross_workspace_task_isolation` AC4 與 `test_project_decouple` 在 round 2 baseline 仍 red — failure pattern 是 B1 task regression，root cause 在 migrations `18.0.1.0.22/0.23` std-rule deactivation timing，與本次 cycle 2 無關
- **下個 cycle 動作**：建立 `src/migrations/18.0.1.0.25/post-migrate.py` 對齊 `18.0.1.0.23` project_public_members_rule pattern — single UPDATE + IN clause deactivate task std rules
- **不擋 cycle 2**：reviewer round 2 已驗 baseline survey 方法學健全（git stash → re-run baseline → 同 reds reproduce），cycle 2 commit range 內 0 改動這些 file → 無 blast radius
- **何時**：next refactor cycle，把這 42 reds 收尾

## F14 — Cleanup hook `mapped` 路徑 belt-and-braces `sudo()`（CAQA C1）

- **Severity**: medium
- **Origin**: CAQA `docs/decisions/reviews/caqa-2026-05-18.md §C1`
- **Location**: `src/models/cloud_service.py:1134-1136` + `:1192-1194`（`_do_cleanup` step 1 + `unlink()` override）
- **現狀**：
  ```python
  user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
      lambda s: s.scope == 'user'
  )
  if user_mcps:
      user_mcps.sudo().unlink()
  ```
  `mapped('mcp_server_ids')` 走 ir.rule（如果 caller context 不是 sudo）— 目前 caller 都是 cron / subscription_lifecycle_mixin / controller 直 unlink，預期都是 sudo / admin context，但 `_do_cleanup` 與 `unlink()` 都是 public method
- **風險**：未來 callable surface 擴大（如 non-sudo caller）+ user 對某些 user-scope MCP 無 read 權限（cross-workspace edge case） → `mapped` 漏掉那些 row → 沒 unlink → 變 zombie
- **建議落點**：
  - A：`self.project_ids.mapped('mcp_server_ids').sudo().filtered(...).unlink()` — sudo mapped 結果
  - B：`self.sudo().project_ids.mapped('mcp_server_ids').filtered(...).sudo().unlink()` — sudo 整條 walk
- **何時**：next touch `cloud_service.py` cleanup path PR 順手做

## F15 — `_do_cleanup` retry-on-error semantics 文件化（R2-1）

- **Severity**: info（doc invariant only，無 code change）
- **Origin**: round 2 verdict R2-1（code-reviewer 提的 info caveat）
- **狀況**：subscription cron 在 cancel 後再 trigger `_do_cleanup` 是 retry-friendly 設計（state='error' 不擋）— 後人若加 `if state == 'error': return` short-circuit guard 會打破 auto-retry
- **建議落點**：
  - 在 [[mcp-project-decouple]] §Invariants 新增「`_do_cleanup` 必須 re-entry safe — `state='error'` 不該 short-circuit return」一條（已部分於 cycle 2 finalize 補 — see Inv #4 helm fail state ordering description）
  - 在 `cloud_service.py:1119` docstring 加 retry note
- **何時**：reporter inline 處理（cycle 2 完成）— 或下個 touch `_do_cleanup` PR

## F16 — `mcp_server` / `mcp_tool` rule-count parity lint check（R2-2）

- **Severity**: info
- **Origin**: round 2 verdict R2-2（code-reviewer 提）
- **狀況**：無 automated lint 檢查 `mcp_server` / `mcp_tool` rule-count parity。依賴人眼 / [[mcp-project-decouple]] §Invariant #5 doc。任何後人加第 4 對 rule 漏對等 = B1-style sub-model leak
- **建議落點**：
  - A：post-migrate 加 invariant assertion — `SELECT COUNT(*) FROM ir_rule WHERE model_id IN (mcp_server, mcp_tool) GROUP BY model_id` — 兩個 count 不等 raise
  - B：CI lint script grep `ir_rules.xml` 內 mcp_server vs mcp_tool record count
  - C：dev-only `make check-mcp-rule-parity` target
- **何時**：next hardening sprint / next ir_rules.xml touch PR

## F17 — `state='error'` re-entry test case（R2-3）

- **Severity**: minor
- **Origin**: round 2 verdict R2-3（pr-test-analyzer 提）
- **狀況**：`_do_cleanup` 在 `state='error'` 時 re-entry 行為未鎖 test — 後人若加 short-circuit guard（見 F15）會打破 retry，無 regression lock
- **建議落點**：`src/tests/test_mcp_project_lifecycle.py` 加 case：
  ```python
  def test_do_cleanup_reentry_after_error_state(self):
      """If _do_cleanup left state='error' (helm uninstall fail),
      a subsequent _do_cleanup call must retry (not short-circuit)."""
      service = self._make_service_in_error_state(...)
      # _do_cleanup again should retry helm uninstall, not no-op
      with patch(...) as mock_helm:
          service._do_cleanup()
          mock_helm.assert_called_once()
  ```
- **何時**：next test hardening PR

## F18 — `_create_admin_user` helper docstring 過 cautious（R2-4）

- **Severity**: trivial
- **Origin**: round 2 verdict R2-4（pr-test-analyzer 提）
- **狀況**：`_create_admin_user` helper 的 docstring 對 `(4, ...)` stale-cache 行為的 claim 是 unverified Odoo 民俗，但實際用 `(6, 0, ...)` 是 idiomatic — 無害 over-cautious
- **建議落點**：clean docstring，移除 unverified Odoo 民俗 claim，保留「use `(6, 0, ...)` 的 idiomatic reason」就好
- **何時**：reporter 順手清 or 留下個 touch 順手做（trivial — 不擋任何 PR）

## F19 — Round 1 17 minor / info 集合（cross-reference round 1 verdict）

- **Severity**: minor / info（mixed）
- **Origin**: `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md §Minor / Info`（commit `e9a5927`）— round 1 reviewer 5-agent 平行 review 提的 17 條 minor，本 verdict 不擋 round 2 approval
- **內容 cluster**（不在本 doc 重複列每條，避免 ephemeral 雜訊）：
  - **Silent failure** (silent-failure-hunter H2/H3/M1-M5)：cleanup step 1 unlink 無 try/except、SSE streaming MCP fail silent fallback、migration pre-flight assertion 缺失、cron retry 無 user notification — 7 條
  - **Type design** (type-design-analyzer F1/F5/F6/F7)：D4 partial SQL CHECK 雙保險、CSV 1,1,1,1 可緊縮、`auto_created` write protection、`scope` 二元假設 — 4 條
  - **Test coverage hole** (pr-test-analyzer)：archived project + user-scope MCP 場景、localhost URL fallback 場景、`_simulate_auto_create` helper drift — 3 條
  - **Code clarity** (code-reviewer)：Controller "MCP Server not found" 對 system-scope misleading、`self.project_ids[:1]` 非確定性 ordering — 2 條
  - **CAQA typo**：21 AC → 25 AC — ✅ DONE（reporter phase 已修）
- **建議落點**：
  - 多數 silent-failure cluster 等 F9 observability sprint 一併 batch
  - Type design 等 next mcp_server.py touch PR 順手評估
  - Test coverage 跟 F8 batch
  - Code clarity 等 next paas.py touch PR 順手做
- **何時**：分散 batch，不獨立 PR

## F20 — Controller manual check helper + `_get_user_mcp_tools` mixin abstraction（/simplify）

- **Severity**: minor
- **Origin**: round 1 verdict §`/simplify` 後是否還有可簡化處
- **狀況**：
  - Controller manual check pattern `if not server.exists() or server.project_id.cloud_service_id.id != service.id` 在 `paas.py:1335/1353/1366/1384` 重複 4 次 — 可抽 `_check_mcp_server_belongs_to_service(server, service)` helper（與 F19 minor #12 一併）
  - `_get_user_mcp_tools` 在 `discuss_channel.py:310` 與 `_get_user_mcp_tools_for_channel` 在 `ai_assistant.py:614` chain walk 邏輯一樣，可抽 mixin function。Controller path 自己重寫一遍是 cursor invalidation 考量（line 415 註解有提及）— **可不簡化，但 surface 此 duplication 給未來 maintainer**
- **建議落點**：next touch 對應 file 的 PR 順手做。Test #26（AC #14）已是 behavioural lock，helper-wrap-safe（helper 內部走舊鏈仍會被抓）— 抽 helper 不會破測試
- **何時**：next paas.py / ai_assistant.py 整理 PR

---

## Cross-cutting: 給未來 PR 作者的 hint

每個改動下列檔案的 PR，自查本 doc 對應條目：
- `src/security/ir_rules.xml` / `src/security/ir.model.access.csv` → F1 / F4 / F16
- `src/migrations/<v>/post-migrate.py` → F5 (cycle 1 xmlid drift 防呆) / F13 (cycle 2 task ACL 對齊)
- `src/models/discuss_channel.py` → F2 / F3 / F6 / F9
- `src/models/cloud_service.py` → F14 / F15
- `src/models/mcp_server.py` → F19 (type design / test coverage cluster)
- `src/controllers/ai_assistant.py` → F4 / F10 / F12 / F20
- `src/controllers/paas.py` → F19 / F20
- `src/tests/test_*.py` → F8 / F17 / F19
- `src/static/src/paas/layout/` → F7

## See also

- [[cloud-service-project-decouple]] §Consequences — cycle 1 決策後仍要持續注意的負面影響
- [[mcp-project-decouple]] §Consequences + §Round 2 改動敘事 — cycle 2 決策後仍要持續注意的負面影響
- [[odoo-addon-std-rule-symmetric-deactivation]] — F1 / F13 的 doc 形式
- [[cross-workspace-task-data-leak]] §Hardening 建議 — F5 的更完整 context
- [[direct-unlink-bypass-cleanup-hook]] — F14 的根因 context
- [[system-mcp-create-acl-bypass]] — F16 的根因 context
- [[tag-scoped-test-misses-full-u-failures]] — F19 「test coverage hole」cluster 的根因 context
- 原始 review（合成來源，本 doc 引用不複製）：
  - `docs/decisions/reviews/caqa-2026-05-16.md` §Caveat 清單（M1-M4 + L1-L3 — cycle 1）
  - `docs/decisions/reviews/caqa-2026-05-18.md` §Caveat / Out-of-scope（C1 + F12 — cycle 2）
  - `docs/decisions/reviews/pr-review-verdict-2026-05-17.md` §Recommendation（reporter follow-up 1-9 整合 — cycle 1）
  - `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md` §Minor / Info（17 條 round 1 minor — cycle 2）
  - `docs/decisions/reviews/pr-review-verdict-mcp-round2-2026-05-18.md` §新 minor / info（R2-1 ~ R2-4 — cycle 2 round 2）
  - `docs/decisions/reviews/pr-description-draft.md` §Follow-up（cycle 1 候選清單）
