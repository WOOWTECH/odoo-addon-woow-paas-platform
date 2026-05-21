# Follow-ups — PaaS refactor cycle 追蹤

> 本 doc 累積跨 refactor cycle 沒處理 / 刻意延後的事項。每條附 (severity, scope origin, 動手建議, 對誰處理)。Reporter 不解決，列冊讓未來 PR / issue 持續追蹤。

**State as of 2026-05-21**：
- Cycle 1（cloud-service ↔ project decouple, 2026-05-17）：F1-F11，B1 已 RESOLVED、PR APPROVED WITH CAVEATS
- Cycle 2（mcp cloud_service_id → project_id decouple, 2026-05-18）：F12-F20 新增，PR APPROVED WITH CAVEATS round 2，4 BLOCKER/MAJOR 全 close
- Cycle 3（Project Detail page, issue #48, 2026-05-20）：F21-F28 新增，APPROVED WITH CAVEATS，B-1 fresh-install fix verified；AC #28 reviewer E2E 完成 10/11 pass + 1 FAIL（E2E-G1，不擋 merge）；R-1 / R-2 hard caveats 收於 reporter phase（commit pending），R-3 / R-6 / E2E-G1 + 其他 polish 進 follow-up issue
- Cycle 4（MCP refactor URL redesign + unbound project ACL, issue #49 + #51, 2026-05-21）：**F22 + F23 RESOLVED**；F-X1~F-X8 新增；reviewer round 2 APPROVED WITH CAVEATS，3 hard caveat (HC-1/2/3) 全 closed in PR；1 new MINOR (SC-6b)；20 條 soft caveat (SC-1~SC-20) 分散 reporter inline / dedicated follow-up / batch 三類處理

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
| F21 | medium | refactor (cycle 3) | 3 | URL pattern 統一 + service-centric MCP endpoint deprecation (OR-1 / OR-6) | gitea #50 ✓ |
| F22 | medium | refactor (cycle 3) | 3 | D5 invariant 重設計 cluster — sidecar MCP auto-create / rebind race / `@api.constrains` ORM enforcement (OR-2 / OR-4 / OR-5 / R-4) | ✅ **RESOLVED cycle 4** — see [[mcp-url-redesign]]，issue #49 closed |
| F23 | medium | spec gap (cycle 3) | 3 | Unbound project ACL — endpoint 對 unbound 路徑無防 enumeration (OR-7 / R-4 partial) | ✅ **RESOLVED cycle 4** — see [[mcp-url-redesign]]，issue #51 closed（合 PR #49） |
| F-X1 | medium | refactor (cycle 4) | 4 | Cron `_cron_retry_mcp_sync` 仍 query `auto_created=True`，新 sidecar-backed row 不掃 (CAQA M1) | dedicated PR — `mcp_server.py:214-217` |
| F-X2 | medium | pre-existing + refactor (cycle 4) | 4 | `ai_assistant._check_project_access` bound-but-no-workspace blanket allow (CAQA M2) + paas.py `_resolve_project_access` 與 ai_assistant.py drift edge case (reviewer SC-6b) + F4 task path + F10 list path 整理 | cluster PR with F4 / F10 |
| F-X3 | low | refactor (cycle 4) | 4 | Codebase-wide comment ref noise — 4 處 production code 殘留過去 PR plan/OQ/CAQA refs (CAQA L1, reviewer SC-19) | reporter phase / batch with F19 |
| F-X4 | low | refactor (cycle 4) | 4 | URL helper duplication — `_compute_sidecar_url_hint` (controller) + `_model_build_mcp_endpoint_url` (model) algorithm 重複 (CAQA L2, plan OR-X8) — 含 SC-13 dead code 評估 | F19 follow-up |
| F-X5 | low | refactor (cycle 4) | 4 | SSRF error message IPv4 vs IPv6 格式微不一致（IPv6 用 `[{host}]`、IPv4 raw） (CAQA L3) | future PR / 統一格式 |
| F-X6 | trivial | refactor (cycle 4) | 4 | `test_mcp_d5_removal.py:194-200` 註解寫「D5 active」但 D5 已拔 stale (CAQA L4, reviewer SC-10) | reporter 順手清 or batch |
| F-X7 | medium | refactor (cycle 4 deferred) | 4 | OQ-2 (b) keep deprecated `auto_created` field — 等 F-X1 cron 改 sidecar_backed 後 drop column 安全 | follow-up after F-X1 |
| F-X8 | low | tester surface (cycle 4) | 4 | `scripts/test-addon.sh` worktree env setup gaps surfaced during cycle 4 test runs | dev script PR / batch with F24 sub-(b) |
| SC-cluster | minor | reviewer (cycle 4) | 4 | Round 1 reviewer 20 條 soft caveat（SC-1~SC-20）— SC-1/SC-2 doc updates by reporter; SC-3~SC-6/SC-6b 已聚 F-X1/F-X2; SC-7/SC-8/SC-9/SC-10/SC-11/SC-12 doc clean by reporter; SC-13 → F-X4; SC-14 (`write_actions` naming) → future PR; SC-15~SC-18 test gaps → batch with cycle 1 F8 + cycle 2 F17 + cycle 3 F27; SC-19 → F-X3; SC-20 → F-X4/F-X5 | 分散處理 |
| F24 | trivial | doc (cycle 3) | 3 | Issue #48 carve-out comment（OR-3）+ worktree env gaps（OR-8）+ declaration↔migration pitfall（OR-9） | reporter phase ✅ (本批已落) |
| F25 | important+ | reviewer (cycle 3) | 3 | Frontend UX hardening + error handling — McpServersSection delete/sync/test 沒 else (R-3) + `_delete_project_mcp_server` 沒 try/except + update 路徑沒 savepoint parity (R-6) + CloudServiceBindingSection rebind-from-unbound dropdown 空 (E2E-G1) | Follow-up issue: #52 |
| F26 | minor | reviewer (cycle 3) | 3 | Polish cluster — `workspace_id` 缺 `readonly=True`（R-5）/ `support_service.js` optimistic merge stuck spinner（R-9）/ `ir_rules.xml:217-238` comment 改 zh-tw（R-10） | 新 polish issue 待開 |
| F27 | minor | reviewer (cycle 3) | 3 | Test gaps G1 / G3 / G6（R-7）— update whitelist 範圍 / sync/test ACL matrix / XSS sanitize 未測 | tester 補 red 或 follow-up issue |
| F28 | long-term | architectural (cycle 3) | 3 | Description rich-text editor 升級 — `document.execCommand` (legacy) → ProseMirror / TipTap (R-8) | architectural issue，等 editor 需求變寬時開 |

下方逐條細節 + 觸發條件 + 建議落點。F1-F11 維持 cycle 1 原文。F12-F20 是 cycle 2。F21+ 是 cycle 3 (issue #48 Project Detail page)。

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

---

# Cycle 3 — Project Detail Page (Issue #48, 2026-05-20)

> Cycle 3 完成於 2026-05-20，final commit `32cf414`。Reviewer `approved with caveats`。R-1（doc dead refs）+ R-2（BasicInfoSection seed sanitize）由 reporter phase 收（commit pending，leader 統一 amend）。其餘 caveats 進 follow-up issue / 既有 #49 #50 #51。本 cycle 新加 F21-F28。OR-* 編號對應 spec `docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` §Open risks，R-* 編號對應 reviewer report `docs/tracking/reviewer-report-2026-05-20-project-detail-page-issue-48.md`。

## F21 — URL pattern 統一 + service-centric MCP endpoint deprecation（OR-1 / OR-6）

- **Severity**: medium（架構 debt，不擋 ship）
- **Origin**: spec §Open risks OR-1 + OR-6；CAQA 確認；reviewer follow-up alignment
- **OR-1**：本 cycle 新增 `/api/support-projects/<id>/mcp-servers` project-centric endpoint，但舊 service-centric MCP endpoint（`/api/cloud-services/<sid>/mcp-servers`）仍保留為 dead code。需要 deprecate 流程（grep call sites → 移除前端 / 後端引用 → drop endpoint）
- **OR-6**：URL pattern 不一致 — 部分 endpoint 用 `/api/workspaces/<wid>/...` nesting，部分用 root-level `/api/support-projects/<pid>/...`。建議全部統一到 `/api/workspaces` nesting
- **Status**: ✓ 已 surface 在 gitea #50（合併 OR-1 + OR-6）
- **何時**：#50 排程

## F22 — D5 invariant 重設計 cluster（OR-2 / OR-4 / OR-5 / R-4）

- **Severity**: medium（D5 是物理現實 invariant，目前靠 multi-layer enforcement，不夠收斂）
- **Origin**: spec §Open risks OR-2/4/5；reviewer R-4
- **D5 invariant**: user-scope MCP 必須掛 bound project（`project.cloud_service_id IS NOT NULL`）。目前 enforcement 分散三層：
  - **Endpoint manual query**（rebind / unbind 走 controller 手動 query 擋）— `paas.py` `_update_support_project` D5 leg（OR-4）
  - **ORM 沒 `@api.constrains`**（`project.project.cloud_service_id` 沒 model-layer enforcement，靠 controller / 前端 gate）— R-4
  - **Rebind race condition**（同時 rebind + create user MCP 可能繞過 D5）— OR-2
  - **Sidecar auto-create + URL 機制**（D5 violation 後的 sidecar lifecycle 沒設計完）— OR-5
- **Status**: ✓ 已 surface 在 gitea #49（合併 OR-2 / OR-4 / OR-5 / R-4 一次重設計）
- **建議**：#49 同 PR 內考慮加 `@api.constrains('cloud_service_id')` model-layer enforcement，把 D5 收斂到 ORM 層，endpoint manual query 變 redundant 可刪
- **何時**：#49 排程

## F23 — Unbound project ACL — 缺防 enumeration / spec gap（OR-7 / R-4 partial）

- **Severity**: medium（pre-existing IDOR 性質，與 F4 同源 — F4 是 ai_assistant.py controller，本條是 paas.py support-projects endpoint）
- **Origin**: spec §Open risks OR-7（spec deferred 不測 unbound）；CAQA §C-3b；reviewer
- **狀況**：endpoint `/api/support-projects/<id>` 對 unbound project（`cloud_service_id IS NULL`）路徑走 OR-7 carve-out 給 `_SyntheticAccess(ROLE_OWNER)`，**沒有**統一防 enum 訊息層（與 bound non-member 路徑不對稱）。任意 internal user brute-force id 都拿 ROLE_OWNER → 可讀整 payload + update name/description
- **OR-7 三選項**：
  - A：creator-only carve-out（`create_uid == user.id OR user.partner_id in message_partner_ids`）
  - B：unbound 一律走防 enum 訊息（與 bound non-member 一致）
  - C：unbound project 廢除（要求 user 必先 bind cloud service）
- **Status**: ✓ 已 surface 在 gitea #51（OR-7 ACL spec gap）+ partial 重疊到 #49（D5 重設計時一併）
- **何時**：#51 排程，與 F4 同一 PR 處理較自然

## F24 — Reporter phase 已落的 doc work（OR-3 / OR-8 / OR-9）

- **Severity**: trivial（doc-only）
- **Origin**: reviewer follow-up alignment 標 reporter phase
- **Status**: ✅ 本批 reporter phase 已落
- **明細**：
  - **OR-3** — issue #48 comment 澄清 D6 status carve-out（碰到 issue 字面誤會「Status 欄位」一事）：已在 reporter phase 之前 post on gitea #48 comment
  - **OR-8** — worktree env setup gaps：sub-(a) `hooks.py` `pycryptodomex` import 已在本 cycle implementation 內補；sub-(b) `PAAS_CI_ADDONS_PATH` / `requirements.txt` pre-install 等 dev env gap 屬 worktree-sync skill / dev script 範疇，本 cycle 不展開
  - **OR-9** — declaration data ↔ migration 1:1 對稱 hardening：本 cycle B-1 fresh-install 重蹈 task pair pattern 的根因；已補進 [[cross-workspace-task-data-leak]] §Hardening `declaration-data ↔ migration 1:1 對稱`
- **何時**：✅ 完成

## F25 — Frontend UX hardening + error handling cluster（R-3 / R-6 / E2E-G1）

- **Severity**: important+（UX silent failure / data-loading gap — issue #48 是 UI page，UI 功能 regression / silent failure 是主功能 regression）
- **Origin**: reviewer R-3 + R-6（silent-failure-hunter critical NEW + important）+ reviewer AC #28 E2E 2026-05-20 step 11 FAIL（E2E-G1）
- **R-3 細節**：`McpServersSection.js:136-161` 三個 method 都用 `if (result.success)` 沒 `else`。Backend 回 `{success: false}` 時 UI 不 surface error（最嚴重 deleteServer：row 仍在 UI 上但 user 以為已刪）。Real surface 場景：sync / test 在 sidecar 斷時 fall-through 至此
- **R-6 細節**：`_delete_project_mcp_server`（`paas.py:1653`）沒 try/except 包 unlink；`_update_project_mcp_server`（L1638）+ `_update_support_project`（L1504）沒 savepoint（vs `_create_project_mcp_server` 有 savepoint）
- **E2E-G1 細節**：`CloudServiceBindingSection.js` `loadAvailableServices` 對 unbound project（`workspace_id == null`）沒 fetch services → dropdown 只剩 "Unbound" 選項，無法在 Project Detail 頁完成 rebind from unbound state。Workaround：Create modal 可指定 `cloud_service`；Same-session unbind 後可走通（cached），reload 或從 list 頁進來踩到
- **Status**: Follow-up issue: #52 (已 post on gitea, 涵蓋 R-3 + R-6 + E2E-G1)
- **建議 fix direction (E2E-G1)**：A：`loadAvailableServices` 對 `workspace_id == null` fall-back fetch all-accessible-workspaces 同 Create modal 行為；B：UI 顯示 disabled dropdown + 提示文案
- **何時**：下一個 sprint 處理（leader 已 post issue #52）

## F26 — Polish cluster (R-5 / R-9 / R-10)

- **Severity**: minor（style / behavior polish，不擋 ship）
- **Origin**: reviewer R-5 / R-9 / R-10
- **明細**：
  - **R-5** — `project.project.workspace_id` 是 compute field 沒 `readonly=True`：silent write 不報錯但無效，misleading（type-design-analyzer）。1-line fix
  - **R-9** — `support_service.js:469-471 + 487-490` `updateProject` optimistic merge 在 `try` 內、`finally clear loading`：若 merge throw 會 stuck spinner（code-reviewer）。1-2 line fix
  - **R-10** — `ir_rules.xml:217-238` comment block 英文（CLAUDE.md 要求 zh-tw），其他相鄰 rule 都用 zh-tw（code-reviewer）。Style polish
- **建議落點**：新 polish issue「project-detail page polish — post-merge maintainability sweep」（reviewer report Soft caveat tracked 表已建議標題）
- **何時**：next paas.py / project.project / ir_rules.xml touch PR 順手做，或單獨 polish issue

## F27 — Test gaps G1 / G3 / G6（R-7）

- **Severity**: minor（implementation 都對；test 是「future regression 強守線」缺塊）
- **Origin**: reviewer R-7（pr-test-analyzer）
- **缺塊**：
  - **G1** — `update` action 的 whitelist scope / `project_id` 寫入是否被 reject 未測
  - **G3** — `sync` / `test` action 的完整 ACL matrix（owner / admin / member / non-member × success / fail）不完整
  - **G6** — XSS sanitize 在 description 寫入 + 讀取雙路徑未被 test 守
- **建議**：每條對應加 1-2 條 test
- **何時**：next test hardening PR；可與 cycle 1 F8 + cycle 2 F17 一併 batch

## F28 — Description rich-text editor 升級（long-term，R-8）

- **Severity**: long-term architectural
- **Origin**: reviewer R-8
- **狀況**：本 cycle `BasicInfoSection` 用 `document.execCommand("bold"/"italic"/...)` 實作最小 rich-text。API legacy，瀏覽器仍支援但 spec 已 deprecated。產品演進到需要 more 編輯能力（如 mention / embed / collaborative editing）時，需要切到 ProseMirror / TipTap / Lexical 之類正式 rich-text framework
- **與本 cycle 的關係**：本 cycle ship 的 execCommand 寫法**沒有錯**（minimum viable rich-text，符合 issue #48 scope），但記在 follow-up 為了告訴未來 maintainer「進一步功能需求要 framework 切換」
- **何時**：產品需求變寬時開 architectural issue 評估

---

---

# Cycle 4 — MCP refactor URL redesign + unbound project ACL (Issue #49 + #51, 2026-05-21)

> Cycle 4 完成於 2026-05-21，PR 預計 merge to develop。Reviewer round 2 APPROVED WITH CAVEATS — HC-1 / HC-2 / HC-3 三條 hard caveat 全 closed in PR；1 條 new MINOR (SC-6b)；20 條 soft caveat 分散處理。F22 + F23 **RESOLVED**（cycle 4 收尾，issue #49 + #51 closed）。本 cycle 新加 F-X1~F-X8 + SC-cluster batch。

## F-X1 — Cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap (CAQA M1)

- **Severity**: medium（UX caveat，非 security / data loss）
- **Origin**: CAQA `docs/tracking/caqa-report-2026-05-20-mcp-refactor.md` M1 + impl finding #4 + reviewer SC-3
- **Location**: `src/models/mcp_server.py:214-217`
- **狀況**：cron 仍 `search([('auto_created', '=', True), ('state', '=', 'draft')])`，行為差異：
  - **Legacy row**（PR #47 既有 `auto_created=True AND sidecar_backed=True`）→ cron 仍 retry sync ✓
  - **新 sidecar-backed row**（cycle 4 UI shortcut 創建，`auto_created=False AND sidecar_backed=True`）→ cron **不會** retry ✗
  - **External row**（user 自填 URL）→ cron 不掃（design intent — external 由 user 自管理）✓
- **風險**：sidecar K8s service / Cloudflare route 啟動可能 lag → user 點 button 後 first sync 失敗 → user 必須手動 retry（比 PR #47 auto-create + cron retry 雙保險差）
- **建議落點**：cron query 改 `sidecar_backed=True` 或廢除 cron（user UI 已有 sync button）
- **何時**：dedicated PR；F-X7 等此 PR 完成後 drop `auto_created` column 才安全

## F-X2 — `_check_project_access` workspace_id NULL edge case + paas.py drift cluster (CAQA M2 + SC-6b + F4 / F10)

- **Severity**: medium（pre-existing + cycle 4 silent 擴大 surface）
- **Origin**: CAQA M2 + reviewer SC-6 + reviewer round 2 NM-R1 (SC-6b) + cycle 1 F4 + cycle 1 F10
- **三個獨立但同 controller IDOR cleanup PR scope**：
  1. **`ai_assistant.py:_check_project_access:54-55`** — `if not project.cloud_service_id.workspace_id: return True` blanket allow（pre-existing，本 PR 不擴大 / 不收緊；實際 PaaS 平台幾乎不會發生 bound service 沒 workspace_id）
  2. **`paas.py:_resolve_project_access:1510-1515`** — `if not workspace:` 同 treatment 涵蓋 2 case：(i) unbound project（follower check ✓，HC-2 hotfix）；(ii) bound-but-no-workspace（**同樣 follower check**，silent 收緊比 ai_assistant.py 嚴）→ `_SyntheticAccess` docstring「lock-step with ai_assistant.py」承諾 drift（reviewer round 2 NM-R1）
  3. **`_check_task_access`** (F4 task path) + **`_list_projects`** (F10 list path) — cycle 1 留 follow-up，cycle 4 plan AC #43 明文 out of scope
- **解法建議**（reporter 階段 surface 三選一）：
  - (a) paas.py 拆 case：`if not project.cloud_service_id:` 走 follower check，`if not project.cloud_service_id.workspace_id:` 與 ai_assistant.py 對齊 blanket allow（最簡）
  - (b) ai_assistant.py 同步收緊 bound-no-workspace（最嚴，security 最強）
  - (c) docstring 補一句 caveat 明示「lock-step only for unbound; bound-no-workspace edge case left blanket-allow in ai_assistant.py per CAQA M2 follow-up」（最弱、doc-only）
- **建議落點**：cluster PR 統一處理 `ai_assistant.py` controller 整體 IDOR — F4 + F10 + F-X2 三者**同一 controller / 同一 attack surface 概念**，分批改 surface 容易再 drift
- **何時**：next security audit / IDOR cleanup PR

## F-X3 — Codebase-wide comment ref noise cleanup (CAQA L1 + reviewer SC-19)

- **Severity**: low（maintenance debt，非本 PR 引入）
- **Origin**: CAQA L1 + impl /simplify pass surface + reviewer SC-19 confirm
- **狀況**：4 處 production code 殘留過去 PR（PR #47 / PR #53）的 plan/OQ/CAQA refs：
  - `ir_rules.xml:251`「CAQA B-1 (re-loop 2026-05-20)」— PR #53 留下
  - `support_service.js:454`「Plan AC #17 / #21」— PR #53 留下
  - `McpServersTab.js:12`「Plan AC #22 / #24」— PR #47 / #53 留下
  - `html_sanitize.js:26`「CAQA C-3, re-loop 2026-05-20」— PR #53 留下
- **狀況補充**：本 PR 已 verify implementation /simplify pass 對 cycle 4 自己加的 plan/OQ refs 已清；tests/ 內可能更多但 reviewer SC-19 不要求清測試
- **建議落點**：codebase-wide grep cleanup — batch with cycle 2 F19 「code clarity」cluster
- **何時**：next polish PR / 順手清

## F-X4 — URL helper duplication 收乾 (CAQA L2 + plan OR-X8 + reviewer SC-13)

- **Severity**: low（plan 明文 defer）
- **Origin**: plan OR-X8 + CAQA L2 + reviewer SC-13（code-reviewer grep verify `_model_build_mcp_endpoint_url` no caller in `src/`）
- **狀況**：兩 method algorithm 完全重複（cluster.local / subdomain / localhost fallback）：
  - `cloud_service.py:_model_build_mcp_endpoint_url` (model side) — PR #47 留下；給 model context 用（cron / non-request caller）— **code-reviewer grep 顯示無 caller**，可能 dead code
  - `paas.py:_compute_sidecar_url_hint` (controller side) — cycle 4 改名（從 `_build_mcp_endpoint_url`）；給 controller context 用（sidecar-hint endpoint）
- **建議落點**（兩種路徑）：
  - (a) verify `_model_build_mcp_endpoint_url` 真的 dead code → 刪；controller side 改抽 module-level helper 共用
  - (b) 保留兩個 method 但抽 algorithm 到共用 helper（`_compute_mcp_url(release, namespace, port, path, subdomain, paas_domain)` module function），兩 method 都呼叫
- **何時**：next paas.py / cloud_service.py touch PR；可獨立做也可 batch with F-X3

## F-X5 — SSRF error message IPv4 vs IPv6 format 微不一致 (CAQA L3)

- **Severity**: low（不影響 security）
- **Origin**: CAQA L3 + reviewer SC-20
- **Location**: `src/controllers/paas.py:_validate_mcp_url:60, 64`
- **狀況**：IPv6 用 `[{host}]` 格式（line 60, 64），IPv4 用 raw host — 一致性微差
- **建議**：統一格式（如統一用 `repr(host)` 或統一加 brackets）
- **何時**：next paas.py touch PR / 順手清

## F-X6 — `test_mcp_d5_removal.py` 註解 stale (CAQA L4 + reviewer SC-10)

- **Severity**: trivial
- **Origin**: CAQA L4 + comment-analyzer M-R5 SC-10 confirm
- **Location**: `src/tests/test_mcp_d5_removal.py:194-200`
- **狀況**：`test_d4_user_scope_requires_project_id_after_refactor` 註解寫「Note: today the D5 constraint method is also live」但 post-impl D5 已拔 — 註解 slight stale；同檔 `test_project_mcp_project_centric.py:21-28` stale module docstring 引用已刪 `TestProjectMcpD5OrmConstraint`（reviewer SC-12）也屬於此 cluster
- **建議落點**：reporter 順手清 or batch with F-X3 codebase-wide cleanup
- **何時**：reporter phase / 順手 / batch

## F-X7 — OQ-2 (b) keep deprecated `auto_created` 後續 column drop (deferred)

- **Severity**: medium（schema cleanup debt）
- **Origin**: cycle 4 implementation phase + OQ-2 leader 拍板 (b) keep deprecated
- **狀況**：cycle 4 implementation phase grep `auto_created` 顯示 10+ regression test 引用 → leader 拍板 (b) keep deprecated（rename 會炸）；migration `18.0.1.0.25` 只 UPDATE backfill 不 DROP COLUMN；field 標 `string='Auto Created (deprecated)'` + help `'Deprecated; use sidecar_backed for new code paths'`
- **依賴**：F-X1（cron query 改 `sidecar_backed`）必須先完成
- **後續動作**：
  1. F-X1 cron PR merge 後 → grep cleanup：把 test fixture / 任何 production code 對 `auto_created` 的 reference 改 `sidecar_backed` 或刪
  2. 寫 migration `18.0.1.0.<future>/post-migrate.py`：`ALTER TABLE woow_paas_platform_mcp_server DROP COLUMN IF EXISTS auto_created`
  3. `mcp_server.py` field 定義刪除
- **何時**：F-X1 完成後的 next cleanup PR

## F-X8 — `scripts/test-addon.sh` worktree env setup gap (tester surface)

- **Severity**: low（dev env，非 production）
- **Origin**: cycle 4 tester phase 試 run 時 surfaces；不在 plan AC 範圍
- **狀況**：worktree 環境跑 `./scripts/test-addon.sh` 時對某些 setup case 有 gap（如 PAAS_CI_ADDONS_PATH / requirements.txt pre-install / 共用 PostgreSQL 切換 — 與 cycle 3 F24 sub-(b) 同 cluster）
- **建議落點**：dev script PR / batch with cycle 3 F24 sub-(b)（worktree-sync skill / dev script 範疇）
- **何時**：next dev tooling PR / 順手做

---

## Cycle 4 SC-cluster batch（reviewer round 1 20 條 soft caveat 收尾分布）

| SC | 內容 | 落點 |
|----|------|------|
| SC-1 | `docs/decisions/patterns/mcp-acl-6-rule-split.md` 5 段 update 為 dual-leg variant | **reporter phase inline** ✅（已更新） |
| SC-2 | `docs/decisions/refactor/mcp-project-decouple.md` Invariants 段：Inv #3 D5 deleted / Inv #4 cleanup filter sidecar_backed / Inv #5 dual-leg / Inv #7 URL helper rename / 新 Inv #9/#10/#11 | **reporter phase inline** ✅（已更新，Inv #9/#10/#11 canonical home 在 [[mcp-url-redesign]]） |
| SC-3 | CAQA M1 cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap | F-X1 ↗ |
| SC-4 | CAQA M2 `_check_project_access` bound-but-no-workspace blanket allow | F-X2 ↗ |
| SC-5 | `_list_projects` F10 sudo + empty domain leak | F10 / F-X2 cluster ↗ |
| SC-6 | `_check_task_access` propagation 與 plan AC #43 wording 不同步（silent 收緊 — security 角度正面） | F-X2 cluster ↗ |
| SC-6b | paas.py `_resolve_project_access` 對 bound-no-workspace edge case 與 ai_assistant.py drift | F-X2 cluster ↗ |
| SC-7 | `ir_rules.xml:145-155` workspace_user comment 改寫（leg 1 「sidecar-backed flow」標籤錯）| reporter phase / batch with F-X3 |
| SC-8 | `_check_project_access` docstring 補 task path mirror + bound-no-workspace edge case | reporter phase / batch with F-X3 |
| SC-9 | `auto_created` help text 補 actionable hint（指向 drop column TODO） | reporter phase / batch with F-X3 |
| SC-10 | `test_mcp_d5_removal.py:194-200` 移除「D5 active」過時假設 | F-X6 ↗ |
| SC-11 | `migrations/18.0.1.0.25/post-migrate.py:44-46` warning branch comment 改事實陳述 | reporter phase / batch with F-X3 |
| SC-12 | `test_project_mcp_project_centric.py:21-28` stale module docstring 拔 `TestProjectMcpD5OrmConstraint` 引用 | F-X6 ↗ |
| SC-13 | `cloud_service.py:_model_build_mcp_endpoint_url` dead code（code-reviewer grep no caller） | F-X4 ↗（URL helper duplication 一併處理） |
| SC-14 | `write_actions` 變數名含 `sidecar-hint` (read-only) 誤導 | future PR rename `gated_actions` 或拆兩 set |
| SC-15 | H.4 cross-workspace assertion 比 H.3 弱（缺 `browse().read()` AccessError leg） | follow-up test 補強 / batch with F27 |
| SC-16 | `test_url_rejects_unsupported_scheme` 沒鎖 error wording | follow-up test 補強 / batch with F27 |
| SC-17 | F group `_assert_rejected` IPv6 4-char host substring matcher loose | follow-up test 補強 / batch with F27 |
| SC-18 | SSRF empty host / port 0 / userinfo edge case 未測（by-design defer #52） | F25 / #52 follow-up |
| SC-19 | Codebase-wide comment ref noise debt（CAQA L1 confirm） | F-X3 ↗ |
| SC-20 | CAQA L2 URL helper duplication + L3 SSRF error format IPv4 vs IPv6 | F-X4 / F-X5 ↗ |

20 條 SC 全分配。SC-1 + SC-2 已 reporter phase inline 處理；SC-3~SC-6/SC-6b/SC-10/SC-12/SC-13/SC-19/SC-20 聚到 F-X1~F-X6；SC-14~SC-18 等 future PR / batch with cycle 1-3 既有 F8/F27 test gap cluster。

---

## Cross-cutting: 給未來 PR 作者的 hint

每個改動下列檔案的 PR，自查本 doc 對應條目：
- `src/security/ir_rules.xml` / `src/security/ir.model.access.csv` → F1 / F4 / F16 / F24 (declaration↔migration 對稱) / F26 (R-10 zh-tw comment) / F-X3 (cycle 4 comment ref noise — `ir_rules.xml:251` PR #53 ref)
- `src/migrations/<v>/post-migrate.py` → F5 (cycle 1 xmlid drift 防呆) / F13 (cycle 2 task ACL 對齊) / F-X7 (cycle 4 drop `auto_created` column after F-X1)
- `src/models/discuss_channel.py` → F2 / F3 / F6 / F9
- `src/models/cloud_service.py` → F14 / F15 / F-X4 (URL helper duplication `_model_build_mcp_endpoint_url`)
- `src/models/mcp_server.py` → F19 (type design / test coverage cluster) / F-X1 (cron `_cron_retry_mcp_sync` 改 sidecar_backed) / F-X7 (drop `auto_created` field)
- `src/models/project_project.py` → ~~F22 (D5 `@api.constrains`)~~ RESOLVED cycle 4 / F26 (R-5 `workspace_id` readonly)
- `src/controllers/ai_assistant.py` → F4 / F10 / F12 / F20 / F-X2 (bound-no-workspace edge case + drift cluster)
- `src/controllers/paas.py` → F19 / F20 / ~~F22 (D5 重設計)~~ RESOLVED cycle 4 / ~~F23 (unbound ACL)~~ RESOLVED cycle 4 / F25 (R-6 try/except + savepoint) / F-X2 (`_resolve_project_access` drift) / F-X4 (`_compute_sidecar_url_hint` duplication) / F-X5 (SSRF error format)
- `src/tests/test_*.py` → F8 / F17 / F19 / F27 (G1 / G3 / G6 test gaps) / F-X6 (cycle 4 D5 stale comment) / SC-15~SC-17 (cycle 4 test gaps)
- `src/static/src/paas/layout/` → F7
- `src/static/src/paas/pages/project-detail/` → F25 (R-3 McpServersSection error display + E2E-G1 CloudServiceBindingSection rebind-from-unbound) / F28 (R-8 editor migration) / F-X3 (`McpServersTab.js:12` comment noise)
- `src/static/src/paas/services/support_service.js` → F26 (R-9 optimistic merge order) / F-X3 (`support_service.js:454` comment noise)
- `scripts/test-addon.sh` / dev tooling → F-X8 (worktree env gap)

## See also

- [[cloud-service-project-decouple]] §Consequences — cycle 1 決策後仍要持續注意的負面影響
- [[mcp-project-decouple]] §Consequences + §Round 2 改動敘事 — cycle 2 決策後仍要持續注意的負面影響
- [[mcp-url-redesign]] §Consequences + §Round 2 改動敘事 — cycle 4 決策後仍要持續注意的負面影響
- [[odoo-addon-std-rule-symmetric-deactivation]] — F1 / F13 的 doc 形式
- [[cross-workspace-task-data-leak]] §Hardening 建議 — F5 的更完整 context
- [[direct-unlink-bypass-cleanup-hook]] — F14 的根因 context
- [[system-mcp-create-acl-bypass]] — F16 的根因 context
- [[tag-scoped-test-misses-full-u-failures]] — F19 「test coverage hole」cluster 的根因 context
- [[two-layer-acl-asymmetric-deny]] — F-X2 的根因 context（cycle 4 HC-2）
- [[legacy-endpoint-scope-blindness]] — cycle 4 HC-1 的根因 context（plan AC scope blindness）
- 原始 review（合成來源，本 doc 引用不複製）：
  - `docs/decisions/reviews/caqa-2026-05-16.md` §Caveat 清單（M1-M4 + L1-L3 — cycle 1）
  - `docs/decisions/reviews/caqa-2026-05-18.md` §Caveat / Out-of-scope（C1 + F12 — cycle 2）
  - `docs/decisions/reviews/pr-review-verdict-2026-05-17.md` §Recommendation（reporter follow-up 1-9 整合 — cycle 1）
  - `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md` §Minor / Info（17 條 round 1 minor — cycle 2）
  - `docs/decisions/reviews/pr-review-verdict-mcp-round2-2026-05-18.md` §新 minor / info（R2-1 ~ R2-4 — cycle 2 round 2）
  - `docs/decisions/reviews/pr-description-draft.md` §Follow-up（cycle 1 候選清單）
  - `docs/tracking/caqa-report-2026-05-20-project-detail-page-issue-48.md` §Blocker + §Caveat（B-1 + C-1..C-3b — cycle 3）
  - `docs/tracking/reviewer-report-2026-05-20-project-detail-page-issue-48.md` §Hard caveats + §Soft caveat tracked（R-1 ~ R-10 — cycle 3）
  - `docs/tracking/caqa-report-2026-05-20-mcp-refactor.md` §Medium / Low findings（M1 / M2 / L1-L4 — cycle 4）
  - `docs/tracking/reviewer-report-2026-05-20-mcp-refactor.md` §Hard Caveats + §Soft Caveats + §Appendix A round 2 closure（HC-1/2/3 + SC-1~SC-20 + NM-R1 — cycle 4）
