---
name: test-coverage-mcp-refactor-2026-05-20
phase: red-explained
team: mcp-refactor
test_explainer: mcp-refactor-test-explainer
plan_ref: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md
design_ref: docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md
created: 2026-05-21
updated: 2026-05-21
status: ready-for-implementation
related:
  - docs/decisions/patterns/mcp-acl-6-rule-split.md
  - docs/decisions/patterns/cloud-service-unlink-override-cleanup.md
  - docs/tracking/pitfalls/system-mcp-create-acl-bypass.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
---

# Test Report — MCP Refactor URL Redesign（人類可讀版）

> 這份報告**不是** plan AC 的逐條翻譯（plan 是 source-of-truth）。
> 它解釋：**「為何這次 PR 這樣測、不那樣測」**，餵給 reviewer / CAQA / stakeholder 對齊測試意圖，避免 reviewer 用多 persona 看完 test 但 miss tester 的 scope rationale。
>
> **reviewer special attention**：第 3 章 OR-X9 矩陣 + 第 7 章 3 條今天就 GREEN 的 regression lock。

---

## 1. TL;DR

本 PR 把「user-scope MCP server 必須掛在某個 cloud service 衍生的 bound project」這個物理層綁定**徹底拔除**：拔 D5 constraint、刪 cloud_service 與 controller 兩路的 auto-create、把 sidecar lifecycle filter 從「掃 user-scope 全清」改成「掃 `sidecar_backed=True` 才清」，並開新 endpoint `sidecar-hint` 把舊算 URL 邏輯改名暴露給 UI shortcut，順手收一條最小 SSRF 防線。為了讓 unbound project 也能掛 user-scope MCP 而不破 workspace 邊界，`workspace_user` + `mcp_tool_workspace_user` 兩條 ir.rule 改成 dual-leg（leg 1 bound walk 原樣 + leg 2 `message_partner_ids` carve-out），`ai_assistant._check_project_access` 同步加 follower 路徑。

對應 test：**6 個新 test 檔 + 3 個 extend** 共 45 個新測試 case，目前 **560 tests / 74 failing** — 紅燈集中在「實作尚未拔 D5」「filter 還在掃 user-scope 全清」「endpoint 還沒長 sidecar-hint」「dual-leg ir.rule 還沒落」這 4 大塊，全部都是 plan 預期會 red 的 boundary；另有 **3 條 *今天就 GREEN* 的 regression lock**（見第 7 章）刻意鎖 post-impl 行為，**reviewer 不要把它們當作該 red 的漏網**。

---

## 2. A–H Group Rationale — 每段為何測這些、為何不測那些

> 每 group 對應 plan §test scope 的 A–H 段。本章重在**意圖層**：說明選 boundary 的理由，不再重複 AC 細節。

### A. D5 移除 + unbound user-scope MCP（`test_mcp_d5_removal.py` × 4）

| 為何測這 | 為何沒測那 |
|---|---|
| D5 是「user-scope iff project bound to cloud service」的物理層 invariant；拔掉這條等於**改變 user-scope MCP 的可掛範圍**。必須以 ORM create + grep-style source check 兩路鎖住「constraint 不只是停用，是整段消失」（避免改動只把 method 改成 `pass`） | **不**測 system-scope MCP 在 D5 拔除後的行為 — 因為 D5 method body 第一行就 `if record.scope != 'user': continue`，system-side 永遠不會碰 D5，沒有 boundary 可測 |
| D4（`scope='user' iff project_id IS NOT NULL`，純語意 invariant）必須**保持**綠燈 — 不能因為拔 D5 順手把 D4 也鬆掉，所以 D4 user-side / system-side 兩個 regression lock 一起鎖 | **不**測 unbound project 的 follower 邊界（那是 H 段 ACL 的事，分層） |

**意圖總結**：A 段是「拔的乾不乾淨 + 沒順手把該守的也鬆掉」。

### B. Sidecar-backed lifecycle filter（`test_mcp_sidecar_backed_lifecycle.py` × 4）

| 為何測這 | 為何沒測那 |
|---|---|
| `cloud_service._do_cleanup` 與 `cloud_service.unlink()` override 兩路 cleanup hook 在 PR #47 都是 filter `scope == 'user'` 全掃。本 PR 加 `sidecar_backed` 後，filter 必須變成 `scope == 'user' AND sidecar_backed`，否則 user 手動建的 external MCP 會被 service 生命週期事件**誤殺**。兩路都必須測（caller-agnostic safety net + lifecycle-aware ordering — 詳見 [[cloud-service-unlink-override-cleanup]] pattern 的 #3 / #4 點） | **不**測 cleanup 順序（pattern 已立規矩，本 PR 不動 order） |
| dual fixture（一個 sidecar-backed + 一個 external 並存在同一 service）— 必須能把「該清的有清 / 不該清的沒清」**同時**驗證；單一 fixture 永遠分不清「沒清是因為 filter 對 / 還是因為根本沒進 cleanup」 | **不**測 sidecar 的 patch_sidecar 行為（design 明示物理層不動） |

**意圖總結**：B 段是「filter 從 `user-scope 全清` 升級到 `sidecar_backed 才清` 的 paired 驗證，PR #47 BLOCKER 2 fixture 同步加 `sidecar_backed=True` 不破既有 9 個綠燈 test」。

### C. Auto-create 拆除 verification（散在 `test_mcp_integration.py` / `test_mcp_project_lifecycle.py` extend）

| 為何測這 | 為何沒測那 |
|---|---|
| `cloud_service._model_auto_create_mcp_server` 與 `controllers/paas.py._auto_create_mcp_server` **兩路 6 處 call site** 是 user-scope MCP 自動冒出的唯一來源。拔掉後最高優先級的 negative test 是「service 轉 running 不冒 MCP」「n8n 初始化成功不冒 MCP」 — 這兩個是過去使用者體感最強的 trigger 點 | **不**測 sidecar config 本身的內容（design 明示 `_build_mcp_sidecar_config` 不動） |
| | **不**測 method 完全消失（A 段已用 grep-style source check 鎖到，分層） |

**意圖總結**：C 段是「自動行為從外部觀察視角證明已消失，使用者層面 MCP 不會再無中生有」。

### D. OR-4 endpoint cleanup（散在 `test_project_detail_endpoints.py` extend）

| 為何測這 | 為何沒測那 |
|---|---|
| PR #53 的 `_update_support_project` unbind path 有一段「D5 violation 時用 manual SQL 把 MCP archived」的 cleanup debt — D5 拔了之後這段 query 永遠 dead code。必須測「unbind 時 user MCP 還活著的 service」走得通**且** manual query 確實移除 | **不**測 cloud_service 整體 unbind 流程（PR #53 已測） |

**意圖總結**：D 段是「PR #53 的 cleanup debt 在本 PR 自然清掉，順帶確認 unbind 不再有 D5 cascade burden」。

### E. Sidecar-hint endpoint（`test_mcp_sidecar_hint_endpoint.py` × 9）

| 為何測這 | 為何沒測那 |
|---|---|
| 這是本 PR **唯一新 endpoint**，三路 boundary 都必須鎖：(1) bound n8n / unbound / mcp_disabled template 三種 service 狀態對 sidecar-hint 回 success vs `No sidecar available`；(2) ACL 三層（owner OK / workspace user 沒 admin 拒 / non-member + 不存在 project 都統一 enum「Project not found or access denied」防 id enumeration，對齊 PR #53 既有規則）；(3) `action='create'` 接受新 payload `sidecar_backed` 並寫入 | **不**測 endpoint enforce「URL 與 sidecar_backed flag 一致性」— 設計上**故意**讓 frontend 負責 mental model（見第 7 章 3 條 regression lock 的 design intent） |
| URL 算法本身（`_build_mcp_endpoint_url` 改名 `_compute_sidecar_url_hint`）不動，所以不需重測 cluster.local / localhost fallback 等舊 case；只需確認新 endpoint 拿得到正確 URL | **不**測 frontend `McpServersSection` 拆按鈕的 OWL 行為（design out of scope） |

**意圖總結**：E 段是「新 endpoint 三路 boundary（service 狀態 / ACL / payload 接受度）一次鎖死，但**刻意**不 enforce URL 與 flag 的對偶一致性 — 由 frontend 負責 UX 邏輯」。

### F. SSRF minimal check（`test_mcp_external_url_creation.py` × 6）

| 為何測這 | 為何沒測那 |
|---|---|
| User 現在可以填 external URL → 必須有最小防線拒 cloud metadata IPv4 / IPv6 link-local / 非 http(s) scheme。順帶**正向 boundary lock**：cluster.local + 一般 https 外網**必須通過**（否則 sidecar workflow 直接斷） | **不**測完整內網 IP 黑名單 — cluster.local 必須允許，blanket ban internal network 會破壞既有 sidecar 路徑；完整 SSRF 加固留 follow-up #52 |
| stdlib only（`urllib.parse` + `ipaddress`）— 不引入新 dependency | **不**測 DNS rebinding / time-of-check-time-of-use（OQ-4 留 follow-up） |

**意圖總結**：F 段是「user 開始能填 URL 的最小自衛防線，正反向 boundary 都鎖（拒這四種 + 允這兩種），剩下交給 #52 follow-up」。

### G. Migration 18.0.1.0.25（`test_mcp_project_decouple_migration_25.py` × 3）

| 為何測這 | 為何沒測那 |
|---|---|
| Migration 三條最高風險 case：(1) backfill 對應確實正確（`auto_created=True → sidecar_backed=True`）；(2) idempotent（重跑不爆、不重複寫）；(3) **PR #47 archived orphan**（`active=False, project_id=NULL, auto_created=True`）狀態必須**保持** — 即使被 backfill 成 `sidecar_backed=True` 也不能被 reactivate | **不**測 `auto_created` 欄位最終命運（implementation 階段才決定 rename 還是 deprecated 保留 — AC 留 implementation 自由度，test 只鎖 backfill 結果） |
| 用 **raw SQL bypass ORM constraint** seed pre-migration row state（craft `auto_created=True` 的 D5 violating shape） — 沒有 SQL bypass 根本造不出測試所需的 pre-migration 狀態 | **不**測 migration 失敗時的 rollback（Odoo transactional migrate 已有 framework 級保證） |

**意圖總結**：G 段是「backfill 對 / 重跑不爆 / 既有 archived 不被改 — 三條最高風險面同時鎖」。

### H. ACL dual-leg domain + ai_assistant 擴入（`test_mcp_acl_unbound_isolation.py` × 12）

| 為何測這 | 為何沒測那 |
|---|---|
| `workspace_user` + `mcp_tool_workspace_user` 兩條 ir.rule 改 dual-leg 是**本 PR 最高 blast radius**（MAJOR 級）。任何 leg 失準 = [[cross-workspace-task-data-leak]] B1 復活 | **不**擴 `_check_task_access` 與 `_list_projects`（design 明示留 F4 / F10 follow-up，不在本 PR scope） |
| 沿 [[mcp-acl-6-rule-split]] pattern：拆 12 個獨立 test method（H.1–H.12），每個鎖一個明確 scenario — 比 32-case parametrize 在 reviewer 走查時更容易讀懂「我在守哪個 boundary」（見第 3 章說明為何不用 parametrize） | **不**測 admin_full × 2 條 / system_readonly × 2 條（design 明示不動 = 沒新 boundary） |
| 必含**負面**邊界：W1 member 但 non-follower **不可讀** unbound MCP — 這條是 leg 2 邏輯設計對不對的 acid test（若 leg 2 寫成永真，這條就會 GREEN 變紅，等同打開 B1） | **不**測 R-4 model-layer `@api.constrains`（design 明示 D5 拔了沒 invariant 可補，不補） |
| `ai_assistant._check_project_access` 必須加 unbound carve-out，否則 ai chat 流程會在 unbound MCP 上踩穿 — 與 ir.rule leg 2 對齊用 `partner_id in message_partner_ids` | |

**意圖總結**：H 段是「dual-leg 改動的 32-method 矩陣（實作為 12 method，見第 3 章），守 B1 不復活；ai_assistant 一條 method 同步擴入；其他 task/list path 留 follow-up」。

---

## 3. OR-X9 32-case Matrix — 為何 H.11 / H.12 是 MAJOR 1 重炸防線

### 為何叫「32-case matrix」但實作只有 12 method

Plan 文字描述的 OR-X9 概念矩陣是 **4 × 2 × 4 = 32**：

- **Project state × 4**：bound / unbound / 不存在 / archived
- **Sub-model × 2**：`mcp_server` / `mcp_tool`
- **Perm direction × 4**：read / write / create / unlink

但完整 32 個 parametrize 的後果是「reviewer 走查時看不出每條 case 在守哪個 boundary」。實作上拆 12 個獨立 method（H.1–H.12），每個 method 名稱直接 self-document（如 `test_workspace_member_non_follower_cannot_read_unbound_project_external_mcp`），覆蓋 32-case 中**有實質風險的子集** — 其餘 case 是 trivial collapse（例如「不存在 project」一律由前置 access check 統一防，不需在 ir.rule 層再測一次）。

**reviewer 走查時看 method 名即可對應到 dual-leg 的哪一支在守 / 哪一支該擋**，無需 parametrize ID 反查。

### H.11 / H.12 為何是 MAJOR 1 重炸防線

> 對應檔案：
> - H.11 `test_workspace_user_rule_still_blocks_system_create_after_dual_leg`（`test_mcp_acl_unbound_isolation.py`）
> - H.12 `test_mcp_tool_workspace_user_rule_still_blocks_system_create_after_dual_leg`（同檔）

PR #47 reviewer round 1 抓到的 [[system-mcp-create-acl-bypass]] 是這次設計的**直接前置威脅**：

> 當時 PR #47 用 single dual-leg rule + `perm_create=True`，導致任何 `base.group_user` user 可以透過 ORM `create({'scope': 'system'})` 創建 system-scope MCP（leg 1 `scope='system'` 永真匹配 → record 創建成功 → 全 instance 可讀）。
> CAQA 漏抓：因為 invariant template 沒對 4 個 perm direction（read/write/create/unlink）分別 evaluate 永真 leg 的 implication。

本 PR 在 `workspace_user` rule 上加第二支 leg 2（`message_partner_ids` carve-out），如果 leg 2 寫成「對 new row 永真」（例如忘了把 `scope='user'` gate 放在最外層 AND），會**重新打開**同一條 privilege escalation 路徑 — `base.group_user` user 仍可透過 ORM create `scope='system'` row。

**H.11 / H.12 直接 reproduce 攻擊**：用 `base.group_user` user 嘗試 ORM create `scope='system'` MCP server 與 mcp_tool，必須 raise `AccessError`。這條是**唯一**能在 implementation 改 dual-leg domain 時當場接住 create-direction 漏洞的測試 — 比走完整 32-case parametrize 還早把炸彈拆掉。

**reviewer 看這兩條 test 的視角**：
1. 確認 dual-leg domain 改完後，**create direction 依然只允許 `scope='user'`** — system-scope create 在 workspace_user rule 中必被擋
2. 確認 sub-model parity — `mcp_tool` 子模型不能因為 walk 經 `server_id.scope='user'` 就漏掉 create-direction 把關

---

## 4. Regression 守線 — PR #47 / PR #53 / B1 哪些 case 必須繼續 GREEN

### PR #47 — schema 解耦的 9 個既有 ACL regression

PR #47 已在 [[mcp-acl-6-rule-split]] pattern 落地 6 條 ir.rule（system_readonly × 2、workspace_user × 2、admin_full × 2）並有 9 個 ACL regression test。本 PR 改 2/6 條規則的 domain（dual-leg），其餘 4 條（system_readonly × 2、admin_full × 2）design 明示**不動**。

**規定**：所有 PR #47 既有 ACL test 必須**繼續綠**。本 PR 在 `test_mcp_acl_unbound_isolation.py:H.10` 加一條 `test_acl_dual_leg_bound_path_preserved` 專門鎖「leg 1 bound 路徑行為 unchanged」— 這條是 PR #47 既有 9 個 test 的 dual-leg 後加強，不是替代。

### PR #47 BLOCKER 2 — cleanup hook 9 test 的 fixture conditional 餵料

PR #47 的 `_do_cleanup` 與 `unlink()` override 既有 9 個 regression test 用了 `_make_service_with_project_and_mcp()` fixture。本 PR 加 `sidecar_backed` 欄位後，fixture 必須**條件式餵料**：

```python
# test_mcp_project_lifecycle.py:88-136 _make_service_with_project_and_mcp
if 'sidecar_backed' in mcp_server_obj._fields:
    vals['sidecar_backed'] = True   # post-impl 餵 True 維持原綠燈
# else: pre-impl 不餵，欄位不存在時不報錯
```

**設計意圖**：fixture 必須對 pre-impl（欄位不存在）與 post-impl（欄位存在）**雙環境兼容**，否則一改實作就連帶把 9 個 PR #47 regression 變紅，看不出真正的 regression。`test_mcp_sidecar_backed_lifecycle.py` 的新 `_seed_service_with_dual_user_mcps()` 是 paired 餵 sidecar + external 兩種 MCP，與 `_make_service_with_project_and_mcp()` 分工。

### PR #53 — Project Detail page 的 OR-4 endpoint

PR #53 的 `_update_support_project` unbind path 那段「D5 violation 用 manual SQL archive MCP」是本 PR 拔 D5 後**自然消失**的 cleanup debt。`test_project_detail_endpoints.py` 新增「unbind 時 user MCP 還活著的 service 走得通」確認 OR-4 在 D5 拔除後仍正確；同時 PR #53 既有的 `Project not found or access denied` 統一 enum 規則被 sidecar-hint endpoint 沿用（見 H.4 / 第 7 章 #3 regression lock）。

### B1 incident（[[cross-workspace-task-data-leak]]）— 跨 workspace 隔離不能破

B1 是過去 CAQA 抓到 task 模型 ACL 漏掉 cross-workspace 隔離的 incident。本 PR `mcp_server` + `mcp_tool` 改 dual-leg 是同一條風險面（記憶體疼痛點對齊）。`test_mcp_acl_unbound_isolation.py:H.4 / H.6` 兩條 cross-workspace 隔離 test（Bob @ W2 對 Alice @ W1 的 unbound MCP server / mcp_tool 都不可讀）就是針對 B1 的 attack-vector regression lock。

---

## 5. Red Proof 摘要 — 560 tests / 74 failing 高層對應

> 詳細紅燈清單在 tester scope report；本章只給高層摘要讓 reviewer 對應到 plan AC 區段。

| Failing 集中區 | 對應 plan AC 群 | 預期 implementation 拆解 |
|---|---|---|
| `test_mcp_d5_removal.py` 4 test 全紅 | AC #1, #5, #6 | 拔 D5 constraint + 拔 method body grep check |
| `test_mcp_sidecar_backed_lifecycle.py` 4 test 全紅 | AC #2, #21, #22, #24 | 加 `sidecar_backed` 欄位 + 兩路 filter 同步改寫 |
| `test_mcp_sidecar_hint_endpoint.py` 9 中 6 條紅、3 條今天 GREEN | AC #14, #27, #28, #29 | 新 endpoint 落地 + 改名舊 URL helper（**3 GREEN 是 by design，見第 7 章**） |
| `test_mcp_external_url_creation.py` 4 條紅、2 條 boundary lock GREEN | AC #30, #31, #32 | SSRF 拒清單落地 + stdlib 解析 |
| `test_mcp_acl_unbound_isolation.py` 12 中 11 紅、H.10 leg-1 regression lock 1 條 GREEN | AC #38, #39, #42 | 兩條 ir.rule dual-leg 改寫 + ai_assistant 一條 method 擴入 |
| `test_mcp_project_decouple_migration_25.py` 3 全紅 | AC #3, #35, #36 | migration 18.0.1.0.25 落地 backfill |
| 既有 `test_mcp_integration.py` / `test_mcp_project_lifecycle.py` / `test_project_detail_endpoints.py` 部分 case 紅 | AC #7, #8, #9, #10, #11, #12, #25 | 拔 auto-create call sites 6 處 + 拔 D5 manual query |

**紅燈總和對 plan AC 覆蓋率**：A、B、C、D、E、F、G、H 八個 group 全部有紅燈代表 — implementation 跑完一輪後若全綠，等於 plan AC #1–#42 的可測子集（#43、#44 是「不做」型，本來就不該有 test）全部達成。

---

## 6. Fixture 慣例 cheatsheet

> 給 implementation phase 與 reviewer 對照用。Implementation 動 fixture 時請對照下表，避免改 fixture 連帶把 regression 變紅看不出真因。

### ACL 角色命名

| 角色 | 在 H 段（`test_mcp_acl_unbound_isolation.py`） | 在 E 段（`test_mcp_sidecar_hint_endpoint.py`） |
|---|---|---|
| W1 owner | `Alice` (h_alice) | `Alice` (shp_alice) |
| W2 owner | `Bob` (h_bob) | `Bob` (shp_bob) |
| W1 member + unbound project follower | `Charlie` (h_charlie) | — |
| W1 member but **non-follower** | `Dave` (h_dave) | — |
| W1 workspace user role（非 owner/admin） | — | `Dave` (shp_dave) |
| Non-member of any workspace | — | `Eve` (shp_eve) |

> 跨 file Alice/Bob 同名不同 user record — 各 file 自己造，不共享 fixture base class。可以理解成 `Alice@H` 與 `Alice@E` 是「角色名相同、實體不同」。

### Sidecar / External MCP 雙 fixture

`test_mcp_sidecar_backed_lifecycle.py:88-135 _seed_service_with_dual_user_mcps()`：
- `sidecar_mcp`：`sidecar_backed=True`, URL = cluster.local
- `external_mcp`：`sidecar_backed=False`, URL = `https://mcp.example.com/sse`
- 兩個 MCP 掛同一 service / project — 用來同時驗證「該清的有清 / 不該清的沒清」

### PR #47 fixture conditional 餵料

`test_mcp_project_lifecycle.py:88-136 _make_service_with_project_and_mcp()`：
- 用 `if 'sidecar_backed' in mcp_server_obj._fields` gating，pre-impl 不餵、post-impl 餵 `True`
- **目的**：PR #47 既有 9 個 regression test 在實作期間（中間狀態）不爆紅，便於 reviewer 看 diff

### Unbound project 建構

`test_mcp_acl_unbound_isolation.py:138-151`：
```python
unbound_project = env['project.project'].sudo().create({'name': '...'})
# cloud_service_id 省略 → NULL (unbound)
unbound_project.message_subscribe(partner_ids=[alice.partner_id.id, charlie.partner_id.id])
# → message_partner_ids = {alice, charlie} 餵給 leg 2 carve-out
```

### Mock paas_operator client

`test_mcp_sidecar_backed_lifecycle.py`：
```python
patch('odoo.addons.woow_paas_platform.models.cloud_service.paas_operator.get_paas_operator_client')
```
**目的**：cleanup hook 會呼到 paas_operator client，本 PR 不測 operator HTTP 層；mock 後可以隔離測 ORM filter 行為。

### SQL bypass ORM constraint

`test_mcp_project_decouple_migration_25.py:137-169 _seed_user_scope_row_via_sql()`：
- 直接 `cr.execute INSERT` 繞 D4 / D5 / model-level constraint
- **唯一場景**：craft pre-migration 行狀態（含 `auto_created=True` 的 D5 violating shape），測 migration backfill

> 這是 **migration 測試專用 idiom**，非 migration 測試不要學 — 一般 test 應走 ORM。

---

## 7. 3 條今天就 GREEN 的 regression lock — Design Intent

> **reviewer 警示**：以下 3 條 test 在 tester 跑的 560/74 紅燈統計裡屬於 GREEN，是**刻意設計的 post-impl regression lock**，不是漏掉的紅燈。

OQ#1 leader 已拍板「即使今天 GREEN 也保留」，原因如下：

### #1 `test_create_endpoint_does_not_enforce_url_consistency_backed_true_external`

**位置**：`test_mcp_sidecar_hint_endpoint.py:465`

**行為**：endpoint `action='create'` 接受 `sidecar_backed=True` + external URL（如 `https://mcp.example.com`）— **不**回 validation error。

**為何今天 GREEN**：implementation 還沒寫 endpoint，但 fallback 路徑（既有 generic JSON-RPC handler）不會主動 enforce 此一致性 → fall-through default 就是允許。

**為何保留**：implementation 落地 endpoint 後，**故意不**加「sidecar_backed ⇔ cluster.local URL」一致性 enforcement — 由 frontend 負責 mental model（user 可能手動把 sidecar URL 貼到 External form，endpoint 不擋）。這條測試是**反向 regression lock**：implementation 不能順手加一致性 check，否則違反 design intent。

### #2 `test_create_endpoint_does_not_enforce_url_consistency_backed_false_cluster_local`

**位置**：`test_mcp_sidecar_hint_endpoint.py:495`

**行為**：endpoint 接受 `sidecar_backed=False` + cluster.local URL — 同樣不 enforce 反向一致性。

**為何今天 GREEN / 為何保留**：與 #1 對稱 — 兩個方向都允許「flag 與 URL 不對偶」。設計上把「Add Sidecar MCP / Add External MCP」兩個按鈕視為**UX shortcut**而非**強制契約**；user 可以把 sidecar 改 external 或反之，endpoint 不會以此為 reject 理由。

### #3 `test_sidecar_hint_acl_non_member_denied_with_project_not_found`

**位置**：`test_mcp_sidecar_hint_endpoint.py:318`

**行為**：非 workspace member（或 project 根本不存在）呼 sidecar-hint endpoint → 統一回 `Project not found or access denied`，**不**區分這兩種失敗原因（避免 id enumeration）。

**為何今天 GREEN**：implementation 還沒寫 sidecar-hint action，但既有 JSON-RPC handler 對 unknown action 的 fallback 就是「project lookup 失敗 → 統一 deny」 → fall-through 已符合期望語意。

**為何保留**：implementation 落地 sidecar-hint action 後，**必須**沿用 PR #53 的統一 deny enum，不能因為新 action 就回更詳細的 error（例如「project exists but you are not member」）。這條鎖 implementation 不可洩漏 id enumeration 訊號。

### 為何這 3 條重要

這三條與第 3 章 H.11 / H.12（OR-X9 防 create-direction leak）類似，都是「實作做下去之後**容易順手破壞**的設計約定」。tester 把它們寫成今天就 GREEN 的 regression lock，是因為：

1. **如果只在 implementation 做完才寫**：reviewer 看不出設計意圖、容易被「這條本來就該過呀」蓋過去
2. **如果寫成今天紅、明天綠**：tester 與 reviewer 都會以為實作要*主動*處理這個 boundary，反而誤導
3. **寫成今天綠、明天綠**：等於明示「這條 boundary 是『fall-through 必須保留』」 — implementation 唯一該做的是**不要主動加錯誤的 enforcement**

---

## 8. 對 reviewer 的提醒

| 段落 | reviewer 應該對照看什麼 |
|---|---|
| 第 2 章 H 段 + 第 3 章 | implementation 改 `src/security/ir_rules.xml` 的 dual-leg domain 時，**請對照 H.11 / H.12 是否仍綠** — 任何把 leg 2 寫成「對 new row 永真」的版本都會立刻把這兩條變紅，等同 PR #47 reviewer round 1 攻擊面復活 |
| 第 4 章 PR #47 BLOCKER 2 fixture conditional | implementation 加 `sidecar_backed` 欄位時，請確認 `_make_service_with_project_and_mcp()` 的 conditional 餵料邏輯**不要**改成永遠餵 — 中間狀態（欄位剛建尚未 backfill）會炸 |
| 第 4 章 B1 regression | cross-workspace 隔離 test（H.4 / H.6）是 B1 的直接 attack-vector lock；改 dual-leg 的 PR 一定要看這兩條保持綠 |
| 第 7 章 3 條 post-impl regression lock | implementation 完成後**不要**為了「看起來更嚴格」順手加 URL ↔ flag 一致性 check 或 verbose error — 這 3 條 lock 會在加錯時立刻紅，等同設計違規 |
| 第 6 章 fixture cheatsheet | 看不懂某條 test 的 Alice/Bob 是誰、為何要 mock paas_operator → 直接對照本章；不必跨檔找 |

---

## 9. 隱性假設 / 環境條件

- **Migration test 假設 Odoo 18.0** schema 與 `cr.execute` raw SQL idiom 可用（不適用未來 Odoo 升級）
- **HttpCase test 假設 `/web/session/authenticate` 與 `_post` JSON-RPC helper 可用**（本 repo 既有 idiom）
- **mock_paas_operator_client 假設** `paas_operator.get_paas_operator_client` import path 不變 — 若 implementation 期間 refactor 此 import，需同步調整 mock 路徑
- **`auto_created` 欄位**在實作期間決策為 rename 或 deprecated 保留（plan AC #4 留 implementation 自由度）— 若選 rename，migration test 須額外驗 column drop；若選 deprecated 保留，migration test 不需動

---

## 10. 不在本次 scope（明示與 plan 對齊）

- `_check_task_access` 的 unbound carve-out → **F4 follow-up**
- `_list_projects` 的跨 tenant 列舉行為 → **F10 follow-up**（獨立 attack surface）
- 完整 SSRF 加固（DNS rebinding / 內網黑名單） → **follow-up #52**
- task path / list path 的 ACL dual-leg 擴入 → 本 PR 明示 out of scope（plan AC #43）
- `discuss_channel.py` AI chat consumer 鏈、`_get_user_mcp_tools_for_channel` → 不改（plan AC #44）

---

## 參考

- Plan：[`docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md`](../../plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-plan.md)
- Design：[`docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md`](../../plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md)
- ACL 6-rule split pattern：[`docs/decisions/patterns/mcp-acl-6-rule-split.md`](../../decisions/patterns/mcp-acl-6-rule-split.md)
- unlink override cleanup pattern：[`docs/decisions/patterns/cloud-service-unlink-override-cleanup.md`](../../decisions/patterns/cloud-service-unlink-override-cleanup.md)
- system-mcp create ACL bypass（PR #47 reviewer round 1 抓到）：[`docs/tracking/pitfalls/system-mcp-create-acl-bypass.md`](../pitfalls/system-mcp-create-acl-bypass.md)
- cross-workspace task data leak（B1 incident）：[`docs/tracking/pitfalls/cross-workspace-task-data-leak.md`](../pitfalls/cross-workspace-task-data-leak.md)
