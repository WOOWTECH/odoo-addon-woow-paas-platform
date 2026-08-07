# Decision: MCP user-scope URL 重設計 + sidecar auto-create 退場 + unbound project ACL（issue #49 + #51）

**Date**: 2026-05-21
**Status**: accepted
**Components**: `woow_paas_platform.mcp_server`, `woow_paas_platform.mcp_tool`, `woow_paas_platform.cloud_service`, `woow_paas_platform.cloud_app_template`, `project.project`, `controllers/paas.py`, `controllers/ai_assistant.py`, `security/ir_rules.xml`, frontend `McpServersSection`, migrations `18.0.1.0.25`

> 本 decision finalize 於 2026-05-21 reporter phase；反映從 design → plan → tester → implementation → CAQA → reviewer round 1 + round 2 完整 fix-loop 的最終落地。後人改本區前先讀 §Invariants（PR #47 既有 8 條 + 本 cycle 新加 #9/#10/#11）+ §Round 2 attack chain。

## Context

[[mcp-project-decouple]] (PR #47) 完成**語意層** decouple — `mcp_server.cloud_service_id` 改名 `project_id`、AI chat 消費鏈走 `task.project_id.mcp_server_ids`、6-rule split ACL 收緊。但**物理層**仍留兩條把 user-scope MCP 綁回 `cloud_service` 的繩子：(1) `cloud_service` 轉 `running` 時 sidecar auto-create user-scope MCP，URL 算法強依賴 `helm_release_name + helm_namespace`；(2) D5 invariant `_check_user_scope_requires_bound_project` 禁止 unbound project 掛 user-scope MCP。

這兩條繩子的下游後果：[[follow-ups]] F22 cluster（OR-2 rebind race / OR-4 endpoint manual D5 query / OR-5 sidecar auto-create UX 怪 / R-4 ORM-layer 沒 `@api.constrains` 守 D5）；F23 unbound project ACL spec gap（#51 / OR-7）；issue #49 描述的「使用者無法手動加 external MCP」用戶痛點。

本 cycle scope：拔 D5 + 拆 auto-create + 重設計 URL 來源 + dual-leg ACL 守 unbound project external MCP visibility + ai_assistant.py controller carve-out 對齊。**合 #49 + #51 一輪**（OQ-3 拍板 (a)，2026-05-20 wave 1）。

## Decision

採取 **B 案：保留 sidecar 物理機制 + 拆 auto-create + UI shortcut（拆兩按鈕）+ sidecar_backed flag + dual-leg ACL**：

| 動作 | 細節 |
|------|-----|
| **拔 D5 invariant** | `mcp_server._check_user_scope_requires_bound_project` 整段刪除；D4（`scope='user' ⇔ project_id IS NOT NULL`）保留 |
| **拆 auto-create 兩路** | `cloud_service._model_auto_create_mcp_server` + `paas.py._auto_create_mcp_server` 兩 method 整段刪 + 6 個 call sites（service state transition / n8n init / odoo init × 2 路）全刪 |
| **保留 sidecar 物理** | `_build_mcp_sidecar_config` / `client.patch_sidecar` / `cloud_app_template` 7 個 mcp_* fields / paas-operator side / demo data 全不動 |
| **URL helper 改投** | `cloud_service._model_build_mcp_endpoint_url` 保留 algorithm；`controllers/paas._build_mcp_endpoint_url` 改名 `_compute_sidecar_url_hint`，從「auto-create 觸發點」改為「UI shortcut 預填 helper」|
| **新 `sidecar_backed` boolean field** | `mcp_server.sidecar_backed`（default False, readonly）標 row 物理是否仍依賴 backing service 的 sidecar；migration 對既有 `auto_created=True` row 做 SQL backfill；`auto_created` field 保留為 deprecated（OQ-2 leader implementation 拍板 (b) keep，因為 grep 顯示 10+ regression test 引用，rename cost > benefit）|
| **Cleanup filter 雙路徑改寫** | `_do_cleanup` step 1 + `unlink()` override 兩條 filter 從 `lambda s: s.scope == 'user'` 改 `lambda s: s.scope == 'user' and s.sidecar_backed` — sidecar-backed 清，external preserve |
| **新 sidecar-hint endpoint** | `POST /api/support-projects/<id>/mcp-servers action=sidecar-hint` 給 UI shortcut 預填 URL/transport/api_key，套同 owner/admin gate + 統一 `Project not found or access denied` 防 id enumeration |
| **最小 SSRF check** | `_validate_mcp_url` 拒 `169.254.169.254` (AWS/GCP/Azure IMDSv1 IPv4) / IPv6 link-local (`fe80::/10`) / `fd00:ec2::254` (AWS IMDSv2 IPv6) / 非 http(s) scheme；保留 cluster.local（sidecar-backed URL 形態必須允許）|
| **OR-4 cleanup debt 清** | `paas.py._update_support_project` unbind path 內手動 query user-scope MCP 擋 unbind 的整段邏輯刪除；R-4 不補（D5 拔了沒 invariant 可補）|
| **合 #51 ACL dual-leg** | `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` 兩條 rule domain 改 dual-leg：leg 1 bound walk（原樣）+ leg 2 unbound carve-out `('project_id.cloud_service_id', '=', False) AND ('project_id.message_partner_ids', 'in', user.partner_id.ids)`；其餘 4/6 條（`system_readonly` × 2 + `admin_full` × 2）**不動** |
| **ai_assistant 兩層對齊** | `controllers/ai_assistant.py._check_project_access` unbound carve-out 從 `return True` 改 `return user_partner in project.message_partner_ids` — 與 ir.rule leg 2 對齊；`_check_task_access`（F4 task path） + `_list_projects`（F10 list path）本 PR 不擴，留 follow-up |
| **Frontend 拆兩按鈕** | `McpServersSection` 拆「Add Sidecar MCP」（僅 bound service + `template.mcp_enabled=True` 時顯示）+「Add External MCP」（永遠顯示，含 unbound project）；前者點擊呼 sidecar-hint endpoint 預填 form，後者空白 form |
| **Migration `18.0.1.0.25`** | `UPDATE woow_paas_platform_mcp_server SET sidecar_backed = TRUE WHERE auto_created = TRUE AND sidecar_backed = FALSE` — idempotent backfill；orphan `auto_created=True AND project_id IS NULL` 既 archive row 保留現狀（不 unarchive / 不 delete）|

## Considered options（URL 三方案 A / B / C）

| 方案 | 核心動作 | 拒絕理由 |
|------|---------|---------|
| **A — 純 user-driven external URL** | 全拆 sidecar 物理機制（template `mcp_enabled=False`、operator `patch_sidecar` 不被呼叫、7 mcp_* fields 全刪、URL helper 全刪）；user-scope MCP 100% user-provided URL；D5 自然拔（無物理依賴可守） | **UX HIGH cost**：n8n template「out-of-box AI 整合」value prop 被砍；既有 stg sandbox 的 n8n service 全變 broken state（cluster.local URL 物理失效）；user 必須自架 n8n-mcp container；marketplace 描述要改。**architectural cleanliness 最高但 UX regression 不值** |
| **B — 保留 sidecar + UI shortcut + 拆 auto-create**（**picked**）| sidecar 物理保留 + auto-create 拆 + UI shortcut + sidecar_backed flag + dual-leg ACL | **picked，理由見 §Decision 下方「為什麼 B」**|
| **C — sidecar 拆成獨立 mcp template** | 新增「MCP Service」template；user deploy n8n 後另需 deploy MCP service 引用 n8n 為 target；引入新 schema relation `cloud_service.mcp_target_service_id`；既有 records 無自動 migration path | **UX HIGH-MEDIUM cost**：雙 service / 雙計費 / onboarding 反直覺；cross-service binding 是**從未有的概念**，本意「decouple」反而加複雜度；migration cost 最高（helm deploy 不可逆 + 涉及計費，自動化 migration 不安全） |

### 為什麼 B（拍板理由）

1. **UX 摩擦最低**：n8n template「out-of-box AI 整合」價值 prop 從「自動」改「一鍵 add」— 比今天多一 click，但換來 user 對「MCP 何時 active」有掌控；同時 unblock issue #49 描述的「使用者無法手動加 external MCP」（同一 section 既能 add sidecar MCP 也能 add external MCP）。
2. **Migration cost 最低**：既有 `auto_created=True` records 行為不變（sidecar 仍在 → URL 仍 valid）；只需 SQL backfill `sidecar_backed=True`，無 archive / delete / re-deploy。
3. **Schema 解耦目標仍達成**：D5 拔得乾淨 — OR-2 race / OR-4 endpoint manual query / R-4 model-layer constraint / OR-5 UI 措辭尷尬**全部自然消失**（沒 invariant 可被 race / 可被 ORM 繞）。
4. **後續演進路徑 open**：若未來想徹底走 A 案，可以從 B 不破壞性退化（UI shortcut button 改 hidden + demo 改 `mcp_enabled=False` + 既有 sidecar-backed records 自然 fade out）。
5. **唯一 caveat**：B 是 hybrid，cognitive load 比 A 高一點 — 後續 maintainer 看 `template.mcp_*` 仍存在 + `_build_mcp_sidecar_config` 仍存在，但 user-scope MCP record 不再自動建。**這個「物理仍 backed 但 schema 不 enforce」的 hybrid 必須在 invariant doc 寫清楚**（即本 doc §Invariants）。

完整三方案 8 維度 tradeoff matrix（UX cost / Security SSRF / Security phishing / Security cluster URL exfil / Migration cost / Helm chart impact / n8n template impact / Architectural cleanliness / 維護成本）見 `docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md` §URL 三方案探索。

## Sign-off log — 8 OQ resolution（雙 wave）

| Wave | Date | OQ | 拍板者 | 結論 |
|------|------|----|-------|-----|
| 1 | 2026-05-20 | OQ-1（URL 三方案 A / B / C） | user | **B**（保留 sidecar + UI shortcut + 不自動建 MCP record） |
| 1 | 2026-05-20 | OQ-3（Caveat 1 ACL domain 處理） | user | **(a) 合 #51 一輪**（本 PR + #51 共 PR / 共 review） |
| 1 | 2026-05-20 | OQ-5（既有 `auto_created` records migration 路徑） | user | **(c) 保留 + flag rename** 為 `sidecar_backed` |
| 1 | 2026-05-20 | OQ-6（UI 拆兩按鈕 vs 統一 toggle） | user | **拆兩按鈕**（Add Sidecar MCP / Add External MCP 並列，mental model 清楚） |
| 1 | 2026-05-20 | OQ-2（`auto_created` rename 細節） | leader | implementation 階段判斷（design 偏好 rename；grep > 5 cross-module 引用則回退 deprecated）|
| 1 | 2026-05-20 | OQ-4（URL allowlist 範圍） | leader | implementation 階段判斷（design 偏好 minimal SSRF check：cloud metadata IPv4/IPv6 + scheme allowlist）|
| 2 | 2026-05-20 | OQ-7（ai_assistant.py `_check_project_access` 是否擴入本 PR） | user | **YES**（minimal 擴入 project path 一個 method；task / list path 留 follow-up）|
| 2 | 2026-05-20 | OQ-8（OR-7 unbound ACL leg 2 三 options） | user | **(c) message_partner_ids walk**（與 [[cloud-service-project-decouple]] D3=A 既有 idiom 對齊）|

OQ-2 / OQ-4 是「leader 拍板：留 implementation 階段判斷」— implementation 階段 final 落地：

- OQ-2 → **(b) keep deprecated**（grep 顯示 10+ regression test 引用 `auto_created`，rename 會炸；migration 只 UPDATE backfill 不 DROP COLUMN；field 標 `string='Auto Created (deprecated)'` + help `'Deprecated; use sidecar_backed for new code paths'`）
- OQ-4 → minimal SSRF check 落地：`_validate_mcp_url` 拒 4 條 + 接受 cluster.local + https/http；其餘完整 SSRF（DNS rebinding / RFC1918 internal） defer **#52**

## Round 2 改動敘事（multi-agent review 抓 CAQA 漏的 silent IDOR）

本 cycle 經 CAQA 與 reviewer 雙 phase。CAQA verdict **APPROVED WITH CAVEATS**（4 條 caveat M1/M2/L1/L4，無 blocker）。Reviewer phase 透過 `/pr-review-toolkit:review-pr` 跑 **5-agent 平行 review**（code-reviewer / pr-test-analyzer / silent-failure-hunter / type-design-analyzer / comment-analyzer），抓到 **CAQA 漏的 3 條 hard caveat**，全屬 security MAJOR。

### CAQA 漏抓的盲點 + 5-agent 補強路徑

| Hard caveat | 根因 | CAQA 為何漏 | 5-agent 哪個抓到 |
|-------------|------|------------|--------------|
| **HC-1** Legacy `_create_mcp_server` / `_update_mcp_server` SSRF guard 漏裝 | `_validate_mcp_url` 只裝在 plan AC #30 spec 的新端點（`_create_project_mcp_server` / `_update_project_mcp_server`）；舊 service-centric endpoint `/api/workspaces/<wid>/services/<sid>/mcp-servers` 是 same-source code path 但 plan 沒 list | CAQA verify SSRF coverage 對齊 plan AC #30 minimal scope，但**沒對 same-source code path 做 grep verify**（plan scoping 太窄被當作合法 boundary） | **code-reviewer**（grep + frontend `cloud_service.js:587/612/638` 引用追蹤） |
| **HC-2** `paas.py._resolve_project_access` 對 unbound project blanket `_SyntheticAccess(ROLE_OWNER)` | PR #53 引入的 `_SyntheticAccess` 對「unbound or no-workspace」case 直接 grant ROLE_OWNER；本 PR 收緊 ai_assistant.py 而 paas.py 同 unbound carve-out surface 沒同步 → 兩 endpoint 對「unbound project + non-follower internal user」**opposite** 結果 | CAQA report line 190 處理「兩函數要不要統一」是 code style 問題；**沒分析 silent IDOR 後果**（兩層收緊不對等 = ai_assistant 修一半，paas.py 反向擴大 attack surface） | **silent-failure-hunter**（attack vector trace）|
| **HC-3** OR-X9 32-case write/unlink direction 對 unbound 是 implicit-only | `test_mcp_acl_unbound_isolation.py` H.1–H.6 只測 `search()` + `browse().read()`（read direction）；UF/UN × S × W/U 沒 **explicit** write/unlink negative assertion | CAQA §2 cell map 標 `UF/UN × S × W/U` 為「implicit through ORM create」+ 「PR #47 既有 9-case」，**沒 verify 「PR #47 9-case 不 cover unbound row」**（implicit 假設過寬）| **pr-test-analyzer**（矩陣 cell-by-cell 對照）|

### Attack chain reproducer（HC-1 + HC-2 雙 surface 鏈攻擊）

**Pre-hotfix 攻擊鏈**（Dave 為 W2 user 非 W1 unbound project follower）：

1. **Step 1（HC-2 — paas.py 對 unbound blanket ROLE_OWNER）**：Dave `POST /api/support-projects/<W1_unbound_pid>` action=update name → `_resolve_project_access` 對 `not workspace` case 直接 `return project, _SyntheticAccess(ROLE_OWNER)` → sudo write 成功，victim unbound project name 被改
2. **Step 2（HC-2 後續）**：Dave `POST /api/support-projects/<W1_unbound_pid>/mcp-servers` action=create → 走 `McpServer.sudo().create({...})`（`paas.py:1668-1682`）→ **bypass ir.rule leg 2** → 注入 user-scope MCP row 到 victim unbound project
3. **Step 3（HC-1 — legacy endpoint SSRF）**：若 attacker 同時透過 legacy `/api/workspaces/<wid>/services/<sid>/mcp-servers` create row with `url=http://169.254.169.254/...` → 後續 `action_sync_tools` / `action_test_connection` 主動發 outbound HTTP → SSRF 雲端 metadata endpoint

**單獨 HC-1 不擋本 PR 設計**（pre-existing pre-PR）；**單獨 HC-2 也不擋**（pre-existing PR #53）；**但本 PR 收緊 ai_assistant 而沒同步收緊 paas.py = silent divergence 擴大** — 攻擊鏈在「pre-existing + 本 PR 部分對齊」的疊加下變得可重現。

### Leader fix-loop coordination（(a) approve / (b) hotfix / (c) escalate user）

Reviewer 自評偏好 **(b) hotfix in this PR**，理由：

- HC-1 / HC-2 兩條都是 reproducible security MAJOR finding（不只 conceptual gap）
- Hotfix scope 小（~10 lines code + 1 test method），不破壞 plan B 案 + 44 AC 落地
- HC-2 `_SyntheticAccess` docstring 已 stale 必修（提到「D5 ORM constraint」但已刪、提到「Aligns with ai_assistant.py: ... grants global access」但已收緊）— 不修 doc-code drift 後續會誤導
- **雙 surface 一起 fix 防鏈攻擊**：HC-1 + HC-2 在 attack chain 中可串接（Step 2 注入 MCP + Step 3 SSRF），任一不修都會留下半邊洞

User 拍板 **(b)** → implementation + tester fix-loop。

### 三條 HC fix 落地（round 2）

| HC | Fix location | Test lock |
|----|-------------|-----------|
| **HC-1** | `paas.py:1353` (`_create_mcp_server`) + `:1387-1390` (`_update_mcp_server`) — validate-then-create order 加 `_validate_mcp_url(url)` call；`_update` partial-update gating 後同 call（idempotent + 與新端點對等）| `test_mcp_external_url_creation.py:343-568` `TestLegacySsrfCheckOnMcpUrlCreate` — 4 tests（create + update × IPv4 IMDS / IPv6 link-local / unsupported scheme），含 **row-not-committed sanity check** + **original URL preserved sanity check** |
| **HC-2** | `paas.py:1495-1519` (`_resolve_project_access`) — `not workspace` case 加 follower check：`if user_partner in project.message_partner_ids: return project, _SyntheticAccess(ROLE_OWNER); else: return (None, None)`；`_SyntheticAccess` + `_resolve_project_access` docstring 同步更新明示「lock-step with ai_assistant.py」| `test_mcp_acl_unbound_isolation.py:849-1060` `TestPaasEndpointUnboundNonFollower` — 3 explicit endpoint tests（Step 1 update / Step 1 read mirror / Step 2 MCP create），全用 `assertEqual(result.get('error'), 'Project not found or access denied')` strict equality 鎖統一 enum 防 id enumeration |
| **HC-3** | `test_mcp_acl_unbound_isolation.py:438-526` — H.3b / H.4b + cross-ws W/U 4 explicit tests（Dave / Bob × write / unlink）；`assertRaises(AccessError)` 緊縮 scope 1 statement per block | 後續 PR 改 leg 2 `perm_write` / `perm_unlink` 立即 trip 此 4 條 lock |

**Cascading fixture fix**：`test_mcp_sidecar_hint_endpoint.py:156-161` setUp 加 `alice.message_subscribe(unbound_project)` — HC-2 hotfix 後 `_resolve_project_access` 對 unbound non-follower 統一回 `Project not found or access denied` → 原本要測 AC #27 "No sidecar available for this project" wording 的 positive case，需先讓 Alice 通過 follower check 才能 reach sidecar-hint action branch。Cross-check 同檔其他 tests 確認 `project_unbound` 無依賴 Alice non-follower 狀態 → 安全。

### Round 2 reviewer verdict

**APPROVED WITH CAVEATS** — HC-1 / HC-2 / HC-3 全 closed；1 條 new MINOR（NM-R1 / SC-6b）：`paas.py._resolve_project_access` 對 **bound-but-no-workspace** edge case silent 收緊（同 `not workspace` branch 涵蓋 2 case：unbound 與 bound-no-workspace，後者 ai_assistant.py 仍 blanket allow per CAQA M2）— security 方向**正面**（paas.py 更嚴），但與 `_SyntheticAccess` docstring「lock-step」承諾 drift。**不擋 merge** — 標 reporter follow-up（與 CAQA M2 + SC-6 task path 合併處理）。

### 為什麼這對未來 refactor cycle 重要

PR #47 round 2 已抽過一次「multi-agent review 補 CAQA 視角」 lesson（見 [[mcp-project-decouple]] §Round 2 改動敘事）；本 cycle 是**第二次**驗證同 pattern：

1. **「plan AC 只 cover 新 endpoint」是 scope blindness 的 hot zone**：任何新加 security helper / ACL gate 的 PR，必須 grep same-source code path（legacy endpoint / 同 model 別處 mutation entry / cron job 等）verify 全部 update — CAQA 與 reviewer 任一 phase 都該檢查。[[legacy-endpoint-scope-blindness]] pitfall 抽 pattern。
2. **「同一 surface 兩層 ACL 收緊」必須 lock-step**：ir.rule 與 controller carve-out / 兩個 controller endpoint 任一不對齊 = silent divergence，partial fix 比沒 fix 更糟（攻擊者繞知道修哪邊、不修哪邊）。[[two-layer-acl-lockstep]] pattern 抽 + [[two-layer-acl-asymmetric-deny]] pitfall 抽。
3. **「test cell map 標 implicit through」是 coverage gap hot zone**：CAQA cell map 容易把「同 domain 同 walk」當「同 lock」— 但 `perm_*` direction 是獨立 evaluation，後續 PR 改 perm 不會 trip。reviewer matrix cell-by-cell 對照才抓得到。
4. **Multi-agent review 的 ROI 已被驗證兩次**：5-agent 抓 3 個 CAQA 漏的 security MAJOR finding（PR #47 round 1 抓 4 個）。**任何高風險 refactor（schema + ACL + lifecycle + controller 四平面同改）應預設走 multi-agent review，不只靠 CAQA**。

## Consequences

### Positive

- **Issue #49 描述的兩個痛點同時 unblock**：(1) 拆 sidecar + n8n 失去自帶 MCP（A 案問題）→ B 不發生；(2) 使用者無法手動加 external MCP → 同 section 同時支援 sidecar-backed + external URL，dual-leg ACL 允 unbound project + follower 看到 external MCP
- **Schema 解耦延伸到 ACL 兩平面**：D5 拔 + dual-leg ir.rule + sidecar_backed flag = user-scope MCP 在 schema **與 ACL 兩平面**完全 decouple from cloud_service 物理綁定
- **F22 cluster 一輪收乾**：OR-2 race / OR-4 endpoint manual query / OR-5 UI 措辭 / R-4 model-layer constraint **全部自然消失**（D5 拔了，無 invariant 可被 race / 可被 ORM 繞）
- **F23 unbound project ACL spec gap 收乾**：合 #51；OR-7 採 (c) message_partner_ids walk 與 [[cloud-service-project-decouple]] D3=A 既有 idiom 對齊
- **F4 partial fix**：`ai_assistant._check_project_access` project path 已修；task path / `_list_projects` 留 follow-up（仍 leaky 但本 PR scope 明文不擴）
- **Multi-agent review pattern 再次驗證**：5-agent 抓 3 條 CAQA 漏的 security MAJOR；HC-1 + HC-2 鏈攻擊在 fix-loop 內全 close

### Negative / 要持續注意

- **B 案 hybrid cognitive load**：後續 maintainer 看 `template.mcp_*` 仍存在 + `_build_mcp_sidecar_config` 仍存在，但 user-scope MCP record 不再自動建。**hybrid 模型必須靠 doc 維護**（即本 doc + Inv #9/#10/#11）— 任何後人試圖「順手清掉 sidecar 物理機制」會直接違反 B 案 design intent
- **D5 拔後 ORM-layer 無 invariant 守**：[[mcp-project-decouple]] PR #47 設計時 D5 是「schema 層拒絕不合法 row」的根基（B1 pitfall 教訓「ACL 補丁不解 root cause」的具體應用 — 從 schema 層拒絕創建才是正解）。**本 cycle 拔 D5 是清楚的 tradeoff**：拔了之後 unbound project + user-scope MCP 是合法狀態，靠 ACL dual-leg 與 sidecar_backed flag 兩個 mechanism 守不變 — 任何後人想「補回 D5 風格的 invariant」(R-4 model-layer `@api.constrains` 或新 sql constraint) 須先評估「為什麼當初拍板拔」否則重新引入 OR-2 / OR-4 / OR-5 cluster
- **`auto_created` field 留 deprecated 狀態**：OQ-2 (b) implementation 拍板留欄位（rename 會炸 10+ regression test）。短期 maintainer 看 `mcp_server` model 會看到 `auto_created` + `sidecar_backed` 兩個 boolean 並存 + 互相重複；長期 cleanup 路徑：**M1 cron 改 query `sidecar_backed` 後** 才能安全 drop column。F-X1 follow-up 追蹤
- **`_compute_sidecar_url_hint` (controller) + `_model_build_mcp_endpoint_url` (model) 兩個 method algorithm 重複**：B 案下兩條 path 仍存在（model side 給 cron / non-request caller；controller side 給 sidecar-hint endpoint）。F19 / F-X4 follow-up 追蹤，不在本 PR scope 收乾
- **M1 cron `_cron_retry_mcp_sync` 仍 query `auto_created=True`**：新 sidecar-backed row（UI shortcut 創建，`auto_created=False`）cron 不掃 → first sync 失敗時 user 必須手動 retry（比 PR #47 auto-create 的 cron retry 雙保險差）。**嚴重度 UX caveat，非 security / data loss**。F-X1 follow-up 追蹤
- **M2 `_check_project_access` bound-but-no-workspace edge case 仍 blanket allow**：pre-existing path，本 PR 不擴大也不收緊；實際 PaaS 平台幾乎不會發生 bound service 沒 workspace_id。F-X2 follow-up（與 F4 / F10 cluster 合併）
- **SSRF minimal coverage 不擋 RFC1918 / DNS rebinding**：cluster.local 必須允許 → 不能 blanket ban internal network。完整 SSRF 加固留 **#52 follow-up**（F25 cluster partial coverage）。F-X5 IPv4 vs IPv6 error format 微不一致

### 不可逆 / 半可逆動作清單

| 動作 | 可逆性 | 緩解 |
|------|-------|------|
| D5 constraint method 整段刪 | **可逆**（git revert 即可，無 DB-level constraint 對應） | rollback 流程整包退版到 18.0.1.0.24 |
| Migration `18.0.1.0.25` SQL `UPDATE ... SET sidecar_backed=TRUE WHERE auto_created=TRUE` | **可逆**（idempotent；rollback 不會炸 — 反向 SQL `UPDATE SET sidecar_backed=FALSE WHERE ...` 即可）| 既有 row 行為不變 |
| `_model_auto_create_mcp_server` + `_auto_create_mcp_server` method 刪 | **半可逆**（git revert 後既有 sidecar-backed records 已透過 UI shortcut 創建，functional preserved；但 _new_ n8n service deploy 後不會 auto-create MCP record — user 必須走 UI shortcut）| OR-X2 onboarding doc 已 surface；reporter phase 標 follow-up |
| ACL `workspace_user` × 2 改 dual-leg | **可逆**（XML revert 即可）| ir.rule 是 noupdate=0 包裝，重新 -u 套用 |
| `_check_project_access` unbound carve-out 從 `return True` 改 follower check | **可逆**（git revert）| 但 revert 後 IDOR surface 復活，需評估 attack vector |

## Invariants（後人改本區前必看）

> 本段是 New Inv #9/#10/#11 的 canonical home（避免與 [[mcp-project-decouple]] §Invariants 雙寫漂移）；[[mcp-project-decouple]] §Invariants Inv #3（D5）+ Inv #4（cleanup filter）+ Inv #5（ACL rule split）已對應更新並 cross-reference 本 doc。

### Inv #9 — `sidecar_backed=True` 的 user-scope MCP 必由 `_do_cleanup` 與 `unlink()` 兩條 path 清掉

`cloud_service._do_cleanup`（step 1）與 `cloud_service.unlink()` override 兩條 filter **必須包含 `sidecar_backed=True`**：

```python
user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
    lambda s: s.scope == 'user' and s.sidecar_backed
)
```

**漏 filter** → external MCP 被誤殺（user 自填 URL，service 刪了不該被連坐清）
**漏整段 cleanup** → sidecar 拆後 cluster.local URL zombie，重新打開 [[direct-unlink-bypass-cleanup-hook]] pitfall

雙路徑成對改寫是 [[cloud-service-unlink-override-cleanup]] pattern 的延伸 — pattern doc 已記「filter 條件擴一個維度」可預期變動，不破壞 pattern 本身。

Regression lock：`test_mcp_sidecar_backed_lifecycle.py` 4-method paired fixture（sidecar-backed cleanup + external preserve）× 2 路徑（`_do_cleanup` + `unlink()`）。

### Inv #10 — UI shortcut「Add Sidecar MCP」create path 必帶 `sidecar_backed=True`

任何 create user-scope MCP record 的 path，若該 record 物理依賴 backing service 的 sidecar URL，**必須在 ORM write 時帶 `sidecar_backed=True`**，否則 Inv #9 的 cleanup filter 不會清到，service 拆後留 zombie URL。

**enforcement 點**：
- Endpoint `paas.py._create_project_mcp_server` 接受 `params.get('sidecar_backed', False)` 寫入 ORM
- Frontend `McpServersSection.js:saveServer`：`payload.sidecar_backed = !!this.state.isSidecarFlow`（user 點「Add Sidecar MCP」按鈕走 sidecar flow → flag=True；點「Add External MCP」走 external flow → flag=False）

**故意不 enforce「sidecar_backed ⇔ cluster.local URL」一致性**（plan AC #28）— frontend 負責 mental model，endpoint 不擋；user 可手動把 sidecar URL 貼到 External form 或反之。詳見 test report §7 「3 條 GREEN regression lock」設計意圖。

Regression lock：`test_mcp_sidecar_hint_endpoint.py:test_create_with_sidecar_backed_true_persists_flag` + `..._false_persists_flag` + `..._does_not_enforce_url_consistency` (×2 reverse pairs)。

### Inv #11 — `workspace_user` + `mcp_tool_workspace_user` 兩條 rule domain 必須 dual-leg（外層 AND `scope='user'` 是 MAJOR 1 重炸防線根基）

Domain shape 必為：

```python
[
    '&',
    ('scope', '=', 'user'),  # 外層 AND — MAJOR 1 防線根基，鎖 create direction 不可建 system row
    '|',
    # leg 1 (bound walk) — sub-model 須前綴 server_id.
    ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
    '&',
    # leg 2 (unbound carve-out via message_partner_ids)
    ('project_id.cloud_service_id', '=', False),
    ('project_id.message_partner_ids', 'in', [user.partner_id.id]),
]
```

**禁止動作**：
- 把 leg 2 拔掉 → unbound project user-scope MCP 不可見（B 案核心需求破功）
- 把 leg 2 改 always-true（如 `(1, '=', 1)` / `('project_id', '!=', False)`） → ACL leak（B1-class，跨 workspace user 可讀 unbound external MCP）
- 把 leg 2 改其他 walk（如只剩 `create_uid`）→ workspace 內 collaboration 退化（第二位 follower 看不到）
- 把外層 AND 拆掉改 `'|', ('scope', '=', 'user'), ('scope', '=', 'system'), ...` → 重炸 [[system-mcp-create-acl-bypass]] MAJOR 1（leg 1 對 new `scope='system'` row 永真 → user 可 ORM create system row）
- mcp_server 與 mcp_tool 兩條 rule **必須對等改**（sub-model 對等原則 [[mcp-acl-6-rule-split]]）— 任一漏改 = sub-model leak（B1-style）
- 4 條不動 rule（`system_readonly` × 2 + `admin_full` × 2）合併回 dual-leg = 重炸 MAJOR 1

**enforcement 點**：
- `src/security/ir_rules.xml` `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user`
- `controllers/ai_assistant.py._check_project_access` 對 unbound 走 `user_partner in project.message_partner_ids` — 與 leg 2 對齊（兩層 ACL lock-step，[[two-layer-acl-lockstep]] pattern）

Regression lock：`test_mcp_acl_unbound_isolation.py` 12-method matrix（H.1–H.12）— 特別是 **H.11 / H.12 explicit MAJOR 1 reproducer**（Charlie ORM `create({'scope': 'system'})` + sub-model 對等 → 必 `AccessError`）；HC-3 round 2 補加 H.3b / H.4b + cross-ws W/U 4 explicit tests 鎖 perm direction matrix。

### Inv 對應更新（[[mcp-project-decouple]] §Invariants）

- **Inv #3（D5）** — 本 cycle **整段刪除**，留 deprecation note：「拔 D5 後 ORM-layer 無 invariant 守，靠 ACL dual-leg + sidecar_backed flag 兩個 mechanism 守不變」— 詳見本 doc §Consequences Negative
- **Inv #4（cleanup hook 順序）** — 順序不變；filter **加 `sidecar_backed=True` 條件**（Inv #9 的具體落地點）
- **Inv #5（6-rule ACL split）** — `workspace_user` × 2 改 dual-leg（升級 + 不破壞 6-rule split 結構）；其餘 4/6 不動。**rule count parity invariant** 仍適用 — 改 dual-leg shape 時 mcp_server 與 mcp_tool 必須對等。[[mcp-acl-6-rule-split]] pattern doc **必須 update 為 dual-leg variant**（SC-1 reporter phase 處理）
- **Inv #7（URL helper）** — 演算法物理歸 service 不變；invoking pattern 改：`_model_build_mcp_endpoint_url` 不再被 model context 內任何方法呼叫（auto-create 拆了），保留為 cron / non-request caller helper；`controllers/paas._build_mcp_endpoint_url` 改名 `_compute_sidecar_url_hint`（語意 self-documenting）

## See also

- **Predecessor decision**：[[mcp-project-decouple]] — PR #47 schema decouple + 8 invariants（本 cycle 改動 Inv #3/#4/#5/#7，新加 Inv #9/#10/#11）
- **Predecessor decision**：[[cloud-service-project-decouple]] — D3=A 既有 `message_partner_ids + create_uid` 雙腿 idiom 是 OR-7 (c) 拍板的對齊依據
- **Patterns（cycle 4 抽出 / cycle 4 升級）**：
  - [[two-layer-acl-lockstep]] — 本 cycle HC-2 抽出（controller-layer ACL 多 entry point 必須 lock-step）
  - [[unbound-project-acl-dual-leg]] — 本 cycle OR-7 (c) 抽出（ir.rule dual-leg domain shape 給 optional-binding model）
  - [[sidecar-backed-flag-lifecycle]] — 本 cycle Inv #9/#10 抽出（schema flag-driven cleanup filter contract）
  - [[mcp-acl-6-rule-split]] — 本 cycle 升級為 dual-leg variant（SC-1 update 已落地）
  - [[cloud-service-unlink-override-cleanup]] — Inv #9 filter 改寫沿用 pattern（filter 條件擴維度 / pattern 本身不破）
- **Pitfalls（cycle 4 抽出 / cycle 4 再現）**：
  - [[two-layer-acl-asymmetric-deny]] — 本 cycle HC-2 根因抽出（兩層 ACL 收緊一層另一層 silent 放行 → silent IDOR）
  - [[legacy-endpoint-scope-blindness]] — 本 cycle HC-1 根因抽出（plan AC 只 cover 新 endpoint，legacy same-source path silent miss）
  - [[system-mcp-create-acl-bypass]] — 本 cycle dual-leg shape 外層 AND `scope='user'` 直接守此 pitfall（MAJOR 1 重炸防線）
  - [[direct-unlink-bypass-cleanup-hook]] — Inv #9 filter 漏 `sidecar_backed` 就會 zombie URL（不破壞 pattern 但延伸）
  - [[cross-workspace-task-data-leak]] — sub-model 對等性 invariant 在 dual-leg 上下文仍適用
- **Plan / design**：
  - `docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md`（三方案 8 維度 tradeoff matrix）
  - `docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md`（44 AC + 9 OR-X + sign-off log）
- **Inputs（reporter 合成來源 — 不重複，只列引用）**：
  - `docs/archived/tracking/test-coverage-mcp-refactor-2026-05-20.md`（test report — A–H group rationale + OR-X9 矩陣 + 3 GREEN regression lock）
  - `docs/archived/tracking/caqa-report-2026-05-20-mcp-refactor.md`（CAQA — 11 invariants verify + 32-case cell map + 4 caveat M1/M2/L1/L4）
  - `docs/archived/tracking/reviewer-report-2026-05-20-mcp-refactor.md`（reviewer — 3 HC + 20 SC + round 2 attack chain closure）
- **Follow-ups（cycle 4 cluster）**：見 [[follow-ups]] §Cycle 4 — F22/F23 RESOLVED；新增 F-X1（cron query gap）/ F-X2（ai_assistant edge case + F4/F10 cluster）/ F-X3（comment ref noise）/ F-X4（URL helper duplication）/ F-X5（SSRF error format）/ F-X6（test docstring stale）/ F-X7（`auto_created` column drop）/ F-X8（worktree env gap）
- **關鍵 code（reporter finalize 時對齊 implementation）**：
  - `src/__manifest__.py:3` — version `18.0.1.0.25`
  - `src/migrations/18.0.1.0.25/post-migrate.py` — idempotent SQL backfill `auto_created → sidecar_backed`
  - `src/models/mcp_server.py` — D5 method body 刪 + `sidecar_backed` field 新增 + `auto_created` deprecation note
  - `src/models/cloud_service.py` — `_model_auto_create_mcp_server` + 3 call sites 刪 / `_do_cleanup` step 1 filter 加 `sidecar_backed` / `unlink()` override filter 同步 / `_model_build_mcp_endpoint_url` 保留
  - `src/controllers/paas.py` — `_auto_create_mcp_server` + 3 call sites 刪 / `_update_support_project` D5 manual query 刪 / `_compute_sidecar_url_hint` 改名 + `_validate_mcp_url` 新增 + `_create_mcp_server` / `_update_mcp_server` 加 SSRF call（HC-1 hotfix）/ `_resolve_project_access` follower check（HC-2 hotfix）
  - `src/controllers/ai_assistant.py:44-58` — `_check_project_access` unbound carve-out 改 follower check
  - `src/security/ir_rules.xml` — `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` dual-leg domain
  - `src/static/src/paas/pages/project-detail/sections/McpServersSection.{js,xml}` — 拆兩按鈕 + sidecar flow
  - `src/static/src/paas/services/support_service.js` — `fetchSidecarHint(projectId)`
  - 6 新 test files：`test_mcp_d5_removal.py` / `test_mcp_sidecar_backed_lifecycle.py` / `test_mcp_sidecar_hint_endpoint.py` / `test_mcp_external_url_creation.py` / `test_mcp_acl_unbound_isolation.py` / `test_mcp_project_decouple_migration_25.py`
