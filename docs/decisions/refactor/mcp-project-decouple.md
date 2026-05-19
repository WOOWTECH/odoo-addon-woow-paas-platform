# Decision: MCP `cloud_service_id` → `project_id` Refactor

**Date**: 2026-05-18
**Status**: final
**Components**: `woow_paas_platform.mcp_server`, `woow_paas_platform.mcp_tool`, `project.project`, `cloud_service`, `ir.rule`

> Finalized 2026-05-18 reporter phase。本版反映 7-phase 跑完後的完整設計（plan 6 decisions + reviewer round-1 fix loop 補的 ACL rule split + cleanup hook override）。後人改本區前先讀 §Invariants（1-8 條 + Round 2 改動敘事）。

## Context

Refactor 前的耦合：`mcp_server` 有 `cloud_service_id` M2O 指向 `woow_paas_platform.cloud_service`。AI chat 的真實消費鏈是 `channel → task → project → cloud_service → user_mcp_server_ids → tools` — `cloud_service` 在這條鏈上只是中介，**真正擁有 AI chat context 的是 `project`**。

語意錯位的具體後果：
- 任何 AI chat 邏輯要拿 user-scope MCP 都得多走一跳 `project → cloud_service`
- 「Project 屬於 AI chat 場域」這個業務語意在 schema 沒被反映
- 配套缺陷：`mcp_server` / `mcp_tool` 完全沒有 `ir.rule`，整個 instance 上的所有 MCP（含 user-scope）對所有 `base.group_user` 可讀 — pre-existing leak，類似 [[cloud-service-project-decouple]] §Context 描述的 `cloud_service` ACL 缺口

兩個耦合面要在同一個 PR 處理：消費鏈改路徑 + ACL 從無到有。否則 ACL 補洞會被新消費鏈引入的新存取 path 繞過。

物理現實的張力：**user-scope MCP 語意上歸 project，物理上是 sidecar 的 wrapper**。`mcp_server.url` 來自 `service.helm_release_name + service.helm_namespace` — 即使 schema 改成 project_id，url 計算仍要回查 service。Lifecycle 因此必須在兩條路徑都對齊：
- `project.unlink()` → DB cascade（`project_id ondelete='cascade'`）
- `service.unlink()` → service 端 cleanup hook 主動 unlink user-scope MCP（service 真實 delete 前）

跨平面動作：Schema（drop `cloud_service_id`、加 `project_id`）+ Migration（backfill + drop column 一刀過）+ ACL（兩條新 ir.rule）+ Constraint（兩條 Python `_check_*`）+ Lifecycle（service `_do_cleanup` hook）+ Backend view + Frontend OWL（service tab API 微調）。

## Decision

採取「**純改名 + service 端 lifecycle hook**」的 surgical refactor：

- **Schema (D1=B)**：drop `mcp_server.cloud_service_id`，新增 `mcp_server.project_id` (M2O → `project.project`, `ondelete='cascade'`)。`cloud_service.user_mcp_server_ids` 反向 O2M 一併移除。`project.project` 新增 `mcp_server_ids` 反向 O2M。
- **Lifecycle (D1)**：`cloud_service._do_cleanup` 在 helm uninstall **之前**主動 `unlink` 該 service 對應的 user-scope MCP；project 不動。Project 端則靠 DB cascade。
- **Migration (D2/D3)**：`18.0.1.0.24/post-migrate.py` 做 defensive backfill SQL（idempotent） + orphan archive (active=False，**不 unlink** — 留 rollback 救命繩) + 一次 drop `cloud_service_id` column。**不**走兩階段 deprecate。
- **Constraint (D4/D5)**：保留 `scope` 欄位，加 Python `@api.constrains`：
  - `_check_scope_project_consistency`：`scope='user' ⇔ project_id IS NOT NULL`
  - `_check_user_scope_requires_bound_project`：user-scope MCP 的 project 必須是 bound（`project_id.cloud_service_id IS NOT NULL`）
- **ACL（MAJOR 1 fix 後修訂為 rule split）**：新增**四條** `ir.rule` 套 `base.group_user`，按 read direction 與 create/write/unlink direction 拆開：
  - `mcp_server_rule_system_readonly` (`scope='system'`) — `perm_read=True`，其餘 false
  - `mcp_server_rule_workspace_user` (`scope='user'` + workspace 條件) — 全 perm true
  - `mcp_tool_rule_system_readonly` (`server_id.scope='system'`) — read only
  - `mcp_tool_rule_workspace_user` (`server_id.scope='user'` + workspace 條件) — 全 perm true

  CSV row 從 `1,0,0,0` 提升為 `1,1,1,1`（hard cap，rule 層細控 row-level）。原 dual-leg single rule 設計（`'|', ('scope', '=', 'system'), (workspace chain)`）有 create-direction leak：base.group_user 可透過 ORM `create({'scope': 'system', ...})` 創建 system-scope MCP（leg 1 對 new row 永真）。Rule split 分離 read 與 create permission 後，user 只能 create scope='user' MCP 在自家 workspace 內。
- **AI chat consumer**：`discuss_channel._get_user_mcp_tools` 與 `controllers/ai_assistant._get_user_mcp_tools_for_channel` 改走 `task.project_id.mcp_server_ids`。
- **Backend view (D6=A)**：`mcp_server_views.xml` form 上 `cloud_service_id` 欄位替換為 `project_id`。
- **Frontend (D6=A)**：`McpServersTab.xml` 保留在 service 頁，內部 query 改 `service.project_ids[0].mcp_server_ids`，**不**搬到 project 頁。

## Considered options

本節記錄「為什麼這類選擇對這類情境是對的」，逐項對比細節在 `plan-mcp-project-decouple.md §Rejected options`。

### 1. 純改名 + cleanup hook vs 雙 FK vs UI-only（D1）

**為什麼純改名 + cleanup hook 對這類 refactor 對**：「MCP 是 project 的附屬資源」是 brief 字面語意。Schema 必須反映語意，否則 maintainer 看 schema 還是會以為 MCP 屬於 service。雙 FK（cloud_service_id + project_id 都留）會留兩個 source of truth — 任何寫入時都要 sync，consistency invariant 變難守。UI-only（schema 不動）等於把語意決定塞在邏輯層，違反「schema is the most durable contract」。

**為什麼 cleanup hook 是配套必需**：user-scope MCP 物理上是 sidecar 的 wrapper（url 從 service 算）。Schema 上歸 project 後，service.unlink() 不會觸發 project_id cascade（service 跟 project 是 `ondelete='set null'` 雙向解耦，[[cloud-service-project-decouple]] Invariant #2）。沒 cleanup hook = service 刪後 MCP 變 zombie（指向已不存在的 sidecar URL）。

**對未來而言**：若 MCP 改成完全跟 service 同生共死（罕見），可把 cleanup hook 拔掉，改 `cloud_service.project_ids` cascade — 但要走獨立 plan + Invariant #2 review。

### 2. 一次 drop column vs 兩階段 deprecate（D3）

**為什麼一次 drop 對這類 surgical refactor 對**：
- 本次 deploy target 只有 dev-sandbox + stg，無正式 prod data（inventory 未由實環境 SQL 驗證，但 backfill 採 defensive 設計 — see `plan-mcp-project-decouple.md` Open Risk #1）
- Rollback 顆粒度是「整包退版」，不會用個別 column rollback
- 兩階段會留 dead column 兩個版本，是 cognitive load + 增加 mistake surface

**對未來而言**：若未來這類 refactor 涉及 production data + 有實際停機成本，兩階段 deprecate 才是合理選擇（先改 application code 不讀該 column → 一段時間後再 drop）。本次不適用。

### 3. Scope 欄位保留 + Python constraint（D4）

**為什麼保留 scope 而非用 project_id NULL 反推**：
- `scope` 是顯式 enum，比「`project_id IS NULL` ⇒ system」這種 implicit semantics 更 self-documenting
- 未來若要加第三種 scope（如 `org`），保留 enum 可擴
- Constraint 用 Python `@api.constrains` 而非 DB constraint：因為要驗證雙向蘊含（`scope='user' ⇔ project_id NOT NULL` 兩個方向）— DB constraint 寫起來會比較囉嗦

### 4. User-scope MCP 必須掛 bound project（D5）

**為什麼禁止 unbound project 有 user-scope MCP**：
- User-scope MCP 物理上是 sidecar wrapper — sidecar 屬於 service，service 屬於 workspace
- Unbound project（無 cloud_service_id）= 沒 service = 沒 sidecar = 沒 MCP 該存在的物理基礎
- Constraint 跟物理現實對齊；違反現實的狀態根本不該被允許 create
- B1 pitfall 提醒：靠 ACL rule 兜底「不該存在的狀態」是補丁、不解 root cause。從 schema 層拒絕創建才是正解

**對 ACL rule 的 implication**：D5 ACL rule domain `('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)` — 看起來怕「unbound project 的 user-scope MCP」會 leak（cloud_service_id IS NULL → 整條 chain False → 看不到 → 等於 cross-user invisible）。但 constraint 已禁止這種 row 存在，所以 ACL domain 不需要 dual-leg fallback。

### 5'. ACL Rule split (MAJOR 1 fix, 2026-05-18 post-CAQA)

**為什麼必須拆 rule，而非 perm CSV 或 accept-as-design**：

原始 plan 用單一 dual-leg rule（`'|', ('scope', '=', 'system'), (workspace chain)`）+ CSV `1,1,1,1`。reviewer MAJOR 1 抓到 **create direction leak**：

- ORM `create({'scope': 'system', ...})` 時 Odoo 對 new record 套 record rule 的 create domain check
- Leg 1 `('scope', '=', 'system')` 對 new row 為真 → rule allow create
- D4 constraint 允許 scope='system' + project_id=NULL → ORM 層通過
- 新 row 立刻被 same rule 的 read leg 1 永真所有 base.group_user 可讀

**攻擊 vector 具體**：
- Phishing URL：user-created system MCP 出現在 ai.assistant.mcp_server_ids domain (`scope='system' AND active=True`) 內，admin 設定 assistant 時可能不慎選入 → 全 instance AI chat 走 attacker endpoint
- Tool definition forgery：mcp_tool 同 dual-leg rule，user 對既有 system MCP 添加假 tool 定義 → 改變 AI behavior
- DoS：灌爆 mcp_server 與 mcp_tool table

**為什麼 perm CSV gate（option B）行不通**：CSV access right 是 model-level binary cap，無法表達「user 可創 scope=user / 不可創 scope=system」的 row-level 區分。把 user CSV row perm_create=0 會把所有 create path 都堵掉，含合法的 user-scope create — 等於把語意決定塞錯 layer。

**為什麼 accept-as-design（option C）不行**：CAQA Invariant #6 的「always-true leg 是 intentional」前提是「system MCP 只有 admin / sudo create」— 但 CSV/rule 沒 enforce 這個前提。Accept 等於把「假設」當「現實」、留 known security hole 給 reviewer 標 MAJOR；違反 [[cross-workspace-task-data-leak]] 「ACL 補丁不解 root cause」教訓。

**為什麼 rule split 對**：
- ACL layer 直接 enforce — row-level 區分用 record rule 表達是 Odoo idiom
- 既有 codebase pattern：`workspace_rule_member_access`（read-only）+ `workspace_rule_create`（create-only）已是 split idiom（`ir_rules.xml` line 5-26）
- 對等 mcp_tool sub-model 同步拆（B1 對等性）
- sudo() context 不被 rule 卡，`_model_auto_create_mcp_server` + demo data + admin manual create 路徑都保留

### 5. ACL 自家 model，無 std rule 對等問題（D5/ACL）

**為什麼不需要 [[odoo-addon-std-rule-symmetric-deactivation]] checklist**：
- `mcp_server` / `mcp_tool` 是自家 model（`_name = 'woow_paas_platform.*'`），Odoo `addons/` 內無同名 std rule
- 加 ACL 不會 trigger 任何被 OR-bypass 的 std rule
- 不需要 migration deactivate SQL，也不需要對等 sub-model rule deactivation

**Verify SQL**（可選 sanity，但非必要）：
```sql
SELECT d.module, d.name FROM ir_model_data d
JOIN ir_rule r ON r.id = d.res_id AND d.model = 'ir.rule'
WHERE r.model_id IN (
  SELECT id FROM ir_model WHERE model IN (
    'woow_paas_platform.mcp_server', 'woow_paas_platform.mcp_tool'
  )
) AND d.module != 'woow_paas_platform';
-- expect: 0 rows
```

### 6. 前端不搬（D6）

**為什麼 service 頁列 MCP 仍合理**：
- User-scope MCP 是 sidecar wrapper — user 在 service 頁看「該 service 的 MCP」符合心智模型
- 搬到 project 頁等於改 UX 路徑，比本次 schema/ACL 動作 surface 大
- Brief 字面要求是「AI chat 從 project 拿 MCP server」（消費鏈），不是「前端入口從 service 搬到 project」（UX 入口）

**對未來而言**：若使用者實際 feedback「我都從 project 進 chat，找不到 MCP 設定」，再評估搬 — 屬於 UX feature 而非 schema refactor。

## Round 2 改動敘事（reviewer NEEDS REVISION → APPROVED WITH CAVEATS）

本次 refactor 經兩輪 review，round 1 reviewer 補抓 CAQA 漏掉的 2 BLOCKER + 2 MAJOR — 對未來 refactor cycle 有結構性教訓，故記錄於此。

### CAQA 漏抓的兩個盲點

CAQA report (`docs/refactor/caqa-2026-05-18.md`) 對 14 invariants 全 verify ✅，verdict 為 APPROVED WITH CAVEATS。但 §同源 code path 檢查（line 219-234）明文排除 `tests/` 與 `migrations/`，理由是「tester fixture 故意保留舊鏈 reference 以鎖 regression」。**這個 exclude 太寬**：

| 盲點 | CAQA 漏抓內容 | Reviewer 補抓 |
|---|---|---|
| **盲點 A（tests/ 範圍）** | `test_mcp_integration.py` 內 3 個 既有 class（`TestAutoCreateMCPServer` / `TestMCPServerCronRetry` / `TestMCPServerCascadeDelete`，共 14 個 test method）仍引用 `cloud_service.user_mcp_server_ids` 與 `mcp_server.cloud_service_id` 舊 schema | 10 處 stale schema reference → 全 suite `-u woow_paas_platform --test-enable` 必 `ValueError: Invalid field 'cloud_service_id'` red |
| **盲點 B（service.unlink 全 path）** | CAQA Inv #4 verified `_do_cleanup` 自身順序對，但**沒驗證所有 `service.unlink()` path 都經 `_do_cleanup`** | `paas.py:1953, 1966, 2156` 3 條 controller direct unlink + `mcp_server.py:787` cron self-unlink 都 bypass `_do_cleanup` → user-scope MCP zombie |

### Reviewer 用 5-agent multi-agent 補 CAQA

Round 1 reviewer 透過 `/pr-review-toolkit:review-pr` 跑 5 個 sub-agent（code-reviewer / pr-test-analyzer / silent-failure-hunter / type-design-analyzer / comment-analyzer）。Multi-agent **無分歧**：

- BLOCKER 1（stale test schema）— code-reviewer + pr-test-analyzer 雙簽
- BLOCKER 2（direct unlink → zombie MCP）— code-reviewer + silent-failure-hunter（H4）雙簽
- MAJOR 1（system create data leak）— code-reviewer 獨自抓到（CAQA Inv #6 只 verify read leg always-true，沒檢查 `perm_create=True` 在 always-true leg 下的 implication）
- MAJOR 2（helm fail state）— code-reviewer + silent-failure-hunter（H1）雙簽

5-agent 平行 review 是 CAQA 單 agent 視角無法替代的補強路徑。

### Leader 的 fix loop coordination

Round 1 verdict 退回**三個 phase**（tester + impl + planner），leader 並聯 dispatch + 串聯 verify：

```
e9a5927  round 1 verdict (NEEDS REVISION, 2 BLOCKER + 2 MAJOR)
   ↓
4ff8f4b  planner re-doc       — MAJOR 1 rule split 設計（Inv #5/#6 重寫 + Inv #8 新增）
   ↓
fdf51fc  impl ACL             — 6-rule split + migration step 5 retire 舊 rule
   ↓
cf34a65  impl unlink + state  — BLOCKER 2 + MAJOR 2 一輪解（unlink override + helm fail → state='error'）
   ↓
97b5313  tester port + 9 regression test — BLOCKER 1 + 鎖 BLOCKER 2 / MAJOR 1 / MAJOR 2
   ↓
9f147d8  round 2 verdict (APPROVED WITH CAVEATS, 4 finding 全 closed + 4 new minor)
```

Leader 選 **Option A (port 既有 class)** 而非 Option B (retire 3 class)：port + 9 regression test 比 retire 完整（保留既有 test 名 + 補 BLOCKER/MAJOR regression）。Tester 判斷對。

### 為什麼這對未來 refactor cycle 重要

1. **CAQA exclude 範圍要 narrowly scope**：「tests/ 排除」應該只 cover tester 故意建立的 regression fixture，不該 cover 既有 test 模組的 stale schema reference。未來 CAQA template 須區分這兩種 tests/ 內容。
2. **「同源 code path 檢查」必須 walk 全 unlink/delete path**，不只 verify `_do_cleanup` 自身。Pattern: `grep -rn "model_name\.unlink\(\)\|env\['model'\]\.unlink\(\)" src/`。
3. **Always-true ACL leg 必須分 read direction 與 create direction 評估**：read direction 永真可能是設計（system-scope 公開資源），create direction 永真**永遠是漏洞**（user 自創 system row → 全 instance 可讀）。CAQA invariant template 須拆 read / create 兩個 verify check。
4. **Multi-agent review 對單一 reviewer (CAQA) 視角的補強價值已被驗證**：5-agent 抓 4 個 CAQA 漏的 finding。未來高風險 refactor（schema + ACL + lifecycle 三平面同改）應預設走 multi-agent review，不只靠 CAQA。

對應 follow-up 已列入 `docs/refactor/followups/follow-ups.md`（R2-1 ~ R2-4）。

## Consequences

### Positive

- **語意對齊**：MCP 在 schema 層直接歸 project；maintainer 讀 model 即可理解 AI chat 消費鏈
- **ACL 從無到有**：補上 pre-existing leak（mcp_server / mcp_tool 對 cross-workspace user 不再可讀）
- **Lifecycle 對齊**：service 刪、project 刪兩條路徑都不會留 zombie
- **Schema 簡單**：一個 column 改名 + 一個反向 O2M 移到 project，無雙 FK / sync overhead
- **Rollback 顆粒度清晰**：整包退版 = 完整 rollback；不需要個別 column 復原
- **配合既有 invariant**：D5 constraint 與 [[cloud-service-project-decouple]] Invariant #2（service↔project `ondelete='set null'`）天然一致，cleanup hook 是必要配套而非衝突補丁

### Negative / 要持續注意

- **MAJOR 1 fix：rule split 後若任何人把 4 條 rule 合併回單一 dual-leg rule 或把 system_readonly 的 perm_create / perm_write / perm_unlink 改 True，立刻重炸 reviewer 已 catch 的 data leak**。Test #19-#22 是這個 invariant 的 regression lock，PR review 必看 ir_rules.xml 內 `mcp_server_rule_system_readonly` / `mcp_tool_rule_system_readonly` 的 perm_* 是否被改。
- **D1 cleanup hook 必須維持「unlink MCP → helm uninstall」順序**。任何把 hook 移到 helm uninstall 後 / 漏 filter scope='user' 的修改 = MCP zombie。Test #8 鎖此順序，PR review 必看 hook 位置。
- **D5 constraint 與 cleanup hook 互依**：constraint 假設「user-scope MCP 必有 bound project」；cleanup hook 必須在 project unbind 或 service unlink 前清掉 user-scope MCP，否則 constraint 在 unbind 動作中 violate（無法寫成）。
- **`scope='system'` 仍對所有 `base.group_user` 可讀**（Risk #3）。本次不收緊 system-scope ACL — 假設 system MCP 是平台級工具、含 admin-curated 配置。若未來引入 sensitive system MCP（含 API key 等），需重新 review。
- **`mcp_server.url` 仍依賴 backing service**（Risk #4）。Schema 上 mcp_server 看不到 service，但 url 計算邏輯仍走 `self.project_id.cloud_service_id` 拿 helm namespace。Project unbind 後 url 失效 — 這個情境靠 D5 constraint 阻止（user-scope MCP 在 unbind 動作中已被 cleanup hook 清掉）。
- **`_model_auto_create_mcp_server` 對「service running 但無 project」情境無 retry**。目前實作 skip create — 後續 user 建 project 不會自動補建 MCP。屬於 follow-up，本次 plan 暫不解決（Open Q #3）。
- **Migration 一次 drop column 不可逆**（Risk #1）。dev-sandbox / stg 的 inventory SQL 因 shared infra 授權問題未由 planner / leader 直接驗證，已交由 user 跑；plan 採 defensive backfill 設計（idempotent + 0 row noop + orphan cleanup），不依賴 inventory 結果即可安全 deploy。Production deploy 流程未來必須加 `SELECT COUNT(*) FROM woow_paas_platform_mcp_server WHERE scope='user'` sanity check 作為 deploy gate。

## Invariants（後人改本區前必看）

對應 [[cloud-service-project-decouple]] §Invariants 1-7 的格式 — 改本 plan 落地的程式碼前必看：

1. **`mcp_server.project_id` 必須維持 `ondelete='cascade'`**。改成 `set null` 會把 deleted project 的 user-scope MCP 變 zombie；改成 `restrict` 會把刪 project 卡死（user-scope MCP 還在）。Project cascade 行為是 D1 的根基。

2. **`scope='user' ⇔ project_id IS NOT NULL`（D4 invariant）**。任何寫入路徑（ORM create/write、migration backfill、direct SQL）破壞此關係 = `_check_scope_project_consistency` raise。`scope` 與 `project_id` 必須同步遷移 — 不可只改一個。

3. **User-scope MCP 必須掛 bound project（D5 invariant）**。`scope='user' AND project_id.cloud_service_id IS NULL` 是禁止狀態。Cleanup hook（D1）負責在「project unbind」與「service unlink」場景下先清掉 user-scope MCP，防止這個狀態出現。任何拔 cleanup hook / 改 unbind 邏輯的動作 = 違反 invariant。

4. **`cloud_service._do_cleanup` 必須在 helm uninstall 之前主動 unlink user-scope MCP（D1 invariant）**。順序：先 `self.project_ids.mapped('mcp_server_ids').filtered(lambda s: s.scope == 'user').sudo().unlink()` → 再 `client.uninstall_release(...)` → 再 `self.write({'state': 'deleting'})`。test #8 是這個 invariant 的 regression lock。

   **為何這個順序（後人不要重排）**：cleanup 三步驟之間的時間窗存在的「中介狀態」是 race condition 來源。順序選擇明確避開兩個風險：
   - **若先 uninstall 再 unlink**：時間窗 1 內 helm 已拆 sidecar、url 無效，但 ORM 還看得到 mcp_server 與 mcp_tool 的 active row — 另一個 process（cron / SSE 串流 / channel.message_post）此刻拿到 mcp_tool 引用發起 RPC，連線到不存在的 endpoint → uncaught NetworkError / silent fail。AI chat 回覆會看起來「執行 tool 但無 output」— 比 fast-fail 更難 debug。
   - **若先 write state='deleting' 再 unlink**：時間窗 2 內 state 已是 deleting（front-end 假設 service 在拆），但 mcp_server 還 active — UI 可能同時觸發 cleanup retry / cron 觸發 auto-create 重建 MCP，造成 ORM 狀態打架。
   - **先 unlink → 再 uninstall → 再 state='deleting'** 的理由：ORM 層先一致（mcp_server / mcp_tool 即刻看不到，後續路徑全 fail-fast），再讓 helm 物理清理（時間窗內物件已不可被 reference），最後 state 收尾標示 deleting 給其他 process 看。**三步 atomicity 不夠**（Odoo transaction 內 helm RPC 不能 rollback），但時間窗風險最小。

   改順序會讓 service 刪後留 MCP zombie（url 失效但 row 還在），最終由 D5 constraint 在 user 動作中 raise — 但 user-facing error 比 quiet cleanup 差。

5. **`mcp_server` + `mcp_tool` ACL 必須有**六條** rule 對等存在（MAJOR 1 fix，3 mcp_server × 3 mcp_tool 對等鏡像）**：

   | mcp_server | mcp_tool（走 `server_id` 反向） | Groups | Domain | Perms |
   |---|---|---|---|---|
   | `mcp_server_rule_system_readonly` | `mcp_tool_rule_system_readonly` | `base.group_user` | `scope='system'` / `server_id.scope='system'` | `R=True, W/C/U=False` |
   | `mcp_server_rule_workspace_user` | `mcp_tool_rule_workspace_user` | `base.group_user` | `scope='user' AND workspace chain` / 同上反向 | `R/W/C/U=True` |
   | `mcp_server_rule_admin_full` | `mcp_tool_rule_admin_full` | `base.group_system` | `[(1, '=', 1)]`（unconditional） | `R/W/C/U=True` |

   **為什麼需要 admin_full**：`group_system` 透過 `_group_implied_ids` 繼承 `group_user`，OR-combined 後 `system_readonly.perm_create=False` 會把 admin 後台 create 也擋掉（admin 想在 backend 維護 system MCP 失敗）。`admin_full` 套 `group_system` + `domain=[(1,'=',1)]` short-circuit deny。**Odoo per-group rule OR semantics 決定** — `ir_rule.py:140-168`。

   **invariant**：
   - 任何加 `mcp_server` ACL 但漏 `mcp_tool` 對等 rule = sub-model leak（B1-style）
   - 任何把六條合併回單一 dual-leg rule = 重炸 reviewer MAJOR 1 抓到的 create-direction leak
   - 任何拿掉 `*_admin_full` 任一條 = admin 後台 system MCP 維護壞掉（partial failure mode 比 full failure 更難 debug）
   - `mcp_server` rule count 必須 ≡ `mcp_tool` rule count（CAQA 級 lint，後人若加第 4 對 rule 必須加雙邊）

   雖然兩個 model 都是自家、無 std rule 對等問題，但 ACL 對等性原則仍適用 — see [[odoo-addon-std-rule-symmetric-deactivation]] §Why pattern + [[mcp-acl-6-rule-split]] pattern。

6. **`mcp_server_rule_system_readonly` 的 system-scope domain 是故意 always-true（對 read direction），但 `perm_create=False` / `perm_write=False` / `perm_unlink=False` 是 invariant（MAJOR 1 fix）**。Domain `[('scope', '=', 'system')]` — 對任何 `base.group_user` 與任何 system-scope row 永真。**read direction 的 always-true 是設計**（system MCP 是平台級工具，所有 internal user 透過 ai.assistant 用）；但 **create/write/unlink direction 的 always-true 是 MAJOR 1 漏洞**（user 自創 system MCP → 全 instance 可讀 → phishing / forgery / DoS 攻擊面）。後人若把這條 rule 的 perm_create / perm_write / perm_unlink 改 True，或把 system_readonly 與 workspace_user 合併回單一 dual-leg rule，立刻重炸 MAJOR 1（reviewer 已 catch 一次）。Read direction always-true 跟 [[cross-workspace-task-data-leak]] B1 的 std rule always-true 性質不同（B1 是 std rule 對 employees-privacy project 的隱含放行；這裡是明確 scope='system' 的顯式設計）。

7. **`mcp_server.url` 由 `cloud_service._model_build_mcp_endpoint_url` 在 auto-create 時計算並 snapshot 寫入**。演算法物理上歸 service（`helm_release_name + helm_namespace + sidecar_port + endpoint_path`），因此**留在 `cloud_service.py:1015` 而非搬到 `mcp_server.py`** — schema 上看不到 service，邏輯上仍依賴 backing service。

   **本欄位是 snapshot，不是 dynamic compute**：service 重命名 / 換 namespace 後 `mcp_server.url` 不會自動同步，須由 caller 顯式重算或重建 row。屬於 pre-existing 行為（plan §Risk #4），不在本次 refactor scope。

   **後人 verify**：
   - `mcp_server.py` 內 grep `self.cloud_service_id` 必 0 hit（這是 trivially pass 的 sanity，因為演算法不在這個檔案 — 真正守護點在 `cloud_service._model_build_mcp_endpoint_url`）
   - `cloud_service.py::_model_build_mcp_endpoint_url` 內若加 walk path，必須是 `self.project_ids[:1].mcp_server_ids` 反向（不走舊 `user_mcp_server_ids` 反向 O2M — 已移除）
   - 若未來把 URL 改成 dynamic compute（為了解 stale URL 風險），須走獨立 plan 並評估 chat-runtime 效能（每次 tool call 額外 4-jump walk）

8. **System-scope MCP create 必須限制在 sudo() / admin (`group_system`) / demo data XML 三條路徑（MAJOR 1 invariant）**。`base.group_user` 透過 ORM `env['woow_paas_platform.mcp_server'].create({'scope': 'system', ...})` 必須 raise `AccessError`。Enforcement：`mcp_server_rule_system_readonly.perm_create=False`（套 base.group_user）+ `mcp_server_rule_workspace_user` domain 限 `scope='user'`。合法 create paths：
   - `_model_auto_create_mcp_server` 用 sudo() create 的是 scope='user'，不適用此 invariant（被 D5 constraint 覆蓋）
   - admin 後台 form view 透過 `access_mcp_server_admin` (group_system) row 走 admin CSV — perm_create=1
   - `cloud_app_templates.xml` 等 demo data noupdate=0 XML record bypass record rule
   - 任何未來新增的 system MCP create path 必須走 sudo() 或 group_system，並文件化此 invariant，否則違反

   Test #19 是這個 invariant 的 regression lock；mcp_tool 對等 test #20。Migration `18.0.1.0.24` 刪舊兩條 dual-leg rule 的 SQL 是這個 invariant 的 enforcement entry（防 module update 殘留舊 rule OR-combine 重新打開漏洞）。

## See also

- Patterns:
  - [[paas-workspace-member-acl-tightening]] — `mcp_server` / `mcp_tool` ACL 的三件套（workspace-scoped CRUD）
  - [[odoo-addon-std-rule-symmetric-deactivation]] — 雖然本次 model 是自家、無 std rule 對等，但「ACL 對等」原則仍適用（mcp_server ↔ mcp_tool）
  - [[mcp-acl-6-rule-split]] — 本次 MAJOR 1 fix 抽出的 pattern（`system_readonly` + `workspace_user` + `admin_full` per-model triplet，共 6 條 rule）
  - [[cloud-service-unlink-override-cleanup]] — 本次 BLOCKER 2 fix 抽出的 pattern（parent `ondelete='set null'` + child cleanup via `unlink()` override，所有 caller path 全 catch）
- Pitfalls:
  - [[cross-workspace-task-data-leak]] — B1 pitfall，sub-model 對等 leak 範例（mcp_tool 必須跟 mcp_server 一起加 rule）
  - [[direct-unlink-bypass-cleanup-hook]] — BLOCKER 2 踩坑（controller direct unlink 繞 `_do_cleanup`，`@api.constrains` 救不了 parent-field 變動）
  - [[system-mcp-create-acl-bypass]] — MAJOR 1 踩坑（dual-leg rule `perm_create=True` 對 always-true leg 是 privilege escalation surface）
  - [[tag-scoped-test-misses-full-u-failures]] — BLOCKER 1 踩坑（CAQA 用 `--test-tags` scope-limited 驗綠，漏抓 cross-module stale schema reference）
- Plan:
  - `docs/refactor/plan-mcp-project-decouple.md`（本 decision 的 implementation plan，25 AC）
- Inputs（reporter 合成來源 — 不在 durable docs 重複，只列引用）:
  - `docs/refactor/test-scope-report-mcp-2026-05-18.md`（26 cases red proof + cycle-3 fixture rationale）
  - `docs/refactor/test-coverage-mcp-project-decouple.md`（test-explainer 視角 coverage report）
  - `docs/refactor/caqa-2026-05-18.md`（CAQA 14 invariants check + C1 / F12 / Inv #7 caveat）
  - `docs/refactor/pr-review-verdict-mcp-2026-05-18.md`（round 1 NEEDS REVISION，2 BLOCKER + 2 MAJOR + 17 minor）
  - `docs/refactor/pr-review-verdict-mcp-round2-2026-05-18.md`（round 2 APPROVED WITH CAVEATS，4 finding closed + 4 new minor）
- 關鍵 code（finalize 時填入實際 line）:
  - `src/__manifest__.py:3` — version `18.0.1.0.24`
  - `src/migrations/18.0.1.0.24/post-migrate.py:49-180` — 5-step migration（backfill / orphan archive / drop column / noupdate flip / retire 舊 dual-leg rule）
  - `src/security/ir_rules.xml:134-216` — 6 rule split（3 mcp_server × 3 mcp_tool 對等）
  - `src/security/ir.model.access.csv:14,16` — `base.group_user` 對 `mcp_server` / `mcp_tool` 從 `1,0,0,0` → `1,1,1,1`
  - `src/models/mcp_server.py:58-64` — `project_id` 欄位（`ondelete='cascade'`, indexed）
  - `src/models/mcp_server.py:111-139` — D4 + D5 兩條 `@api.constrains`
  - `src/models/cloud_service.py:1015-1036` — `_model_build_mcp_endpoint_url`（演算法物理歸 service）
  - `src/models/cloud_service.py:1119-1171` — `_do_cleanup` 三步 + helm fail → state='error'（MAJOR 2 fix）
  - `src/models/cloud_service.py:1176-1201` — `unlink()` override（BLOCKER 2 fix，catch direct unlink path）
  - `src/models/project_project.py:?` — 新增 `mcp_server_ids = fields.One2many(..., 'project_id', ...)`
  - `src/models/discuss_channel.py:310` + `src/controllers/ai_assistant.py:614` — 消費鏈走 `task.project_id.mcp_server_ids`
  - `src/tests/test_mcp_acl_isolation.py` + `test_mcp_project_lifecycle.py` + `test_mcp_chat_consumption.py` + `test_mcp_project_decouple_migration.py` + `test_mcp_integration.py::TestMcpProjectScopeConstraints` — 共 26 + 9 = 35 regression cases
