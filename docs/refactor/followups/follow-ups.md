# Follow-ups — CloudService ↔ Project Decouple

> 本次 refactor 沒處理 / 刻意延後的事項。每條附 (severity, scope origin, 動手建議, 對誰處理)。Reporter 不解決，列冊讓未來 PR / issue 持續追蹤。

**State as of 2026-05-17**：B1 已 RESOLVED、PR APPROVED WITH CAVEATS、9 條 follow-up 待後續處理。

## Index

| # | Severity | Origin | 主題 | Status |
|---|----------|--------|------|--------|
| F1 | medium | refactor | ACL 對等性 checklist 持久化 | doc-only ✅（本 doc 已寫進 [[odoo-addon-std-rule-symmetric-deactivation]]） |
| F2 | medium | refactor | `_sanitize_helm_values` 同名異實重命名 | 待新 PR |
| F3 | medium | refactor | Cross-workspace incompatible extras 注 AI prompt | 待設計 |
| F4 | medium | pre-existing | `_check_project_access` / `_check_task_access` 對 unbound IDOR | 待新 PR |
| F5 | medium | refactor (deferred) | Migration xmlid drift 防呆 assertion | 使用者 accept 不加本 PR；待新 PR |
| F6 | medium | refactor | D9 階段 2 — sanitizer 升級 whitelist 模式 | 等 telemetry，待新 PR |
| F7 | minor | refactor | BottomNav mobile vs Sidebar D4 拆分後 UX 一致性 | 待設計 |
| F8 | minor | refactor (test) | Test hardening — AC#11 / AC#14 / B1 group_project_user / sanitizer fallback | 待新 PR |
| F9 | minor | refactor | Sanitizer / `_loadCloudServices` 等 silent fallback observability | 待新 PR |
| F10 | minor | pre-existing | `_list_projects` with sudo + empty domain 跨 tenant 列 | D5=A 放大；待新 PR |
| F11 | minor | refactor | Sentinel `***REDACTED***` i18n 化 | 由 F6 觸發後一起評估 |

下方逐條細節 + 觸發條件 + 建議落點。

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
          "See docs/refactor/pitfalls/cross-workspace-task-data-leak.md"
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

## Cross-cutting: 給未來 PR 作者的 hint

每個改動下列檔案的 PR，自查本 doc 對應條目：
- `src/security/ir_rules.xml` / `src/security/ir.model.access.csv` → F1 / F4
- `src/migrations/<v>/post-migrate.py` → F5 (xmlid drift 防呆)
- `src/models/discuss_channel.py` → F2 / F3 / F6 / F9
- `src/controllers/ai_assistant.py` → F4 / F10
- `src/tests/test_*.py` → F8
- `src/static/src/paas/layout/` → F7

## See also

- [[cloud-service-project-decouple]] §Consequences — 本次決策後仍要持續注意的負面影響
- [[odoo-addon-std-rule-symmetric-deactivation]] — F1 的 doc 形式
- [[cross-workspace-task-data-leak]] §Hardening 建議 — F5 的更完整 context
- 原始 review：
  - `docs/refactor/caqa-2026-05-16.md` §Caveat 清單（M1-M4 + L1-L3 來源）
  - `docs/refactor/pr-review-verdict-2026-05-17.md` §Defer to reporter（R-mobile, R-test-hardening, R-silent-degradation）+ §Recommendation（reporter follow-up 1-9 整合）
  - `docs/refactor/pr-description-draft.md` §Follow-up（候選清單）
