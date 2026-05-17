# Refactor: 解耦 CloudService 與 Project

Branch: `feat/decouple-cs-projects`
Plan: `docs/refactor/plan-cloud-service-project-decouple.md`（9 個決策 ✅ 全部 sign-off）
Test Scope: `docs/refactor/test-scope-report-2026-05-16.md`
Test Report: `docs/refactor/test-report-2026-05-16.md`

## 變更摘要

把 `project.project` 從「必須綁一個 CloudService」鬆綁為「可以綁也可以不綁」，補上 task 層級的多 service 參考（extras），補上 `cloud_service` 既有的 ACL 漏洞，並在 AI prompt 注入時對 `helm_values` 做 secret 遮罩 + 三欄位 blacklist。

- Project 可獨立存在（unbound），ACL 改走 project member 隔離
- Task 加 `extra_cloud_service_ids` (M2M) + `primary_cloud_service_id` (related store=True)
- Chatter AI prompt 注入 primary + extras 合併內容，dedup primary 出現在 extras 的情況
- helm_values 經 pattern sanitizer 遮罩（`password|passwd|secret|token|api[_-]?key|credential`）+ 三欄位 blacklist（`n8n_api_key` / `n8n_owner_password` / `mcp_auth_token`）
- `cloud_service` 新增 ir.rule（workspace member only），補上 D2=A 引入 task M2M widget 浮上來的 pre-existing ACL gap
- UI：`/woow#/projects` 新路徑 alias，`/woow#/ai-assistant` 為書籤保留；sidebar 新增 `Projects` 入口；CreateProjectModal 拔 `has_project` 前端 filter + 拔 service 必選 gate，下拉首項「— No service — (Unbound project)」；SupportProjectsPage 顯示 `Unbound` badge；Task form 新增 Linked Services panel

## Acceptance criteria 結果

| 群組 | AC # | 結果 |
|---|---|---|
| Project 獨立性 | #1, #2, #3 | ✅ pass |
| Project ACL（unbound 隔離）| #4, #5, #6 | ✅ pass |
| cloud_service ACL | #7, #8 | ✅ pass |
| Task extras | #9, #10, #11, #12, #13 | ✅ pass |
| AI prompt + sanitizer | #14, #15, #16, #17, #18, #19 | ✅ pass |
| UX 前端 | #20-#25 | ✅ pass |
| B1 cross-workspace task isolation | 2 sanity | ✅ pass |

**25 / 25 acceptance criteria 通過 + 2 B1 sanity tests 通過。** 7 個 regression 守線全部維持 green。

> **AC #5 註記**：plan D3=A 原列 `user_ids` / `message_partner_ids` 兩個 candidate，但 Odoo 18 `project.project` 沒有 `user_ids` 欄位（只有 `favorite_user_ids` / `collaborator_ids` / `message_partner_ids`），原 fixture 寫 `user_ids` 會被 `BaseModel.write` 直接 raise `ValueError`。Tester 在 commit b582a44 移除 `user_ids` 那行、僅保留 `message_partner_ids`，與 implementation 的 record rule unbound leg（`message_partner_ids` + `create_uid`）對齊後 AC #5 變綠。

## 4 Open Qs 拍板結果

| # | Open Q | 決議 | 落實位置 |
|---|---|---|---|
| Q1 | AC #5 user_ids vs message_partner_ids | **`message_partner_ids` + `create_uid` 雙腿**（不依賴 user_ids — 它不在 Odoo 18 project.project schema 上）| `src/security/ir_rules.xml:32`（rule domain） |
| Q2 | AC #11 domain 形式 | **Static domain on field definition** — `[('workspace_id', '=', workspace_id)]`；task 自身的 `workspace_id` 是 `related='project_id.workspace_id', store=True`，unbound task 時自動評估為 `False`，候選清單為空（UX 一致，無 NoneType 風險） | `src/models/project_task.py:43-51` |
| Q3 | AC #13 warning 欄位命名 | **`incompatible_extra_cloud_service_ids`**（computed M2M），UI panel 可對每個不相容 chip 加紅框／hover 訊息，比 boolean / 字串訊息資訊量豐富 | `src/models/project_task.py:60-79` |
| Q4 | AC #15 sentinel | **`***REDACTED***` hardcode**（與 plan / test 對齊；未來 i18n 化的需求由 reporter 寫入 follow-up） | `src/models/discuss_channel.py:18` |

## Deploy SQL（請使用者手動執行）

### 升級前（Risk R1 dry-run — plan §Migration）

確認有沒有 pre-existing 的 unbound project，避免 ACL 變嚴後使用者「東西不見了」：

```sql
-- 若 query 結果非空，逐筆檢視是否該 user 已是 project member
-- 不是 member 則需手動補 message_partner_ids
SELECT p.id, p.name, p.create_uid, u.login AS creator_login
FROM project_project p
LEFT JOIN res_users u ON u.id = p.create_uid
WHERE p.cloud_service_id IS NULL;
```

### 升級後驗證

```sql
-- 1. 新欄位 schema 建好
\d project_task_cloud_service_rel       -- 應該存在
SELECT column_name FROM information_schema.columns
WHERE table_name = 'project_task'
  AND column_name IN ('primary_cloud_service_id', 'workspace_id');

-- 2. 新 ir.rule 落地、舊 leaky rules deactivated（B1 patch 後 3 條對等）
SELECT id, name, active FROM ir_rule WHERE name LIKE '%workspace member%';
SELECT d.name AS xmlid, r.name, r.active
FROM ir_rule r
JOIN ir_model_data d ON d.res_id = r.id AND d.model = 'ir.rule'
WHERE d.module = 'project'
  AND d.name IN (
    'project_public_members_rule',          -- project.project
    'task_visibility_rule',                  -- project.task (base.group_user)
    'task_visibility_rule_project_user'      -- project.task (group_project_user)
  );
-- expect: 3 rows, all active = false

-- 3. 既有 bound project 的 workspace_id 仍可正確 derive
SELECT p.id, p.name, p.cloud_service_id, p.workspace_id
FROM project_project p
WHERE p.cloud_service_id IS NOT NULL
LIMIT 5;
```

### Rollback（若 emergency）

```sql
-- 1. 還原 3 條 Odoo std rules（B1 patch 後對等處理）
UPDATE ir_rule SET active = true
WHERE id IN (
    SELECT res_id FROM ir_model_data
    WHERE module = 'project'
      AND model = 'ir.rule'
      AND name IN (
        'project_public_members_rule',          -- project.project
        'task_visibility_rule',                  -- project.task (base.group_user)
        'task_visibility_rule_project_user'      -- project.task (group_project_user)
      )
);

-- 2. revert merge commit → restart Odoo
-- 3. ⚠️ 若 production 已有使用者設定 task extras（M2M 表 project_task_cloud_service_rel），rollback 前 dump：
--    pg_dump -t project_task_cloud_service_rel ... > extras-backup.sql
--    然後 DROP TABLE project_task_cloud_service_rel
```

## ⚠️ 不可逆 / 半可逆動作清單

| 動作 | 可逆性 | 緩解 |
|---|---|---|
| `project_task_cloud_service_rel` M2M 表創建 | **半可逆**（rollback 會丟失使用者設定的 extras 連結）| rollback 前 dump 該表；plan R2 已標 |
| `project.project_public_members_rule.active = false` | **可逆**（rollback SQL 上方提供）| 影響面：base.group_user 用戶失去「可見所有非 followers project」的權限，改為 workspace-scoped 過濾 |
| `project.task_visibility_rule.active = false` | **可逆**（rollback SQL 上方提供，CAQA B1 patch 引入）| 與上方對等的 task 端；不拔則 base.group_user 在 ORM 層仍可讀取所有非 followers project 的 task（cross-workspace 資料外洩） |
| `project.task_visibility_rule_project_user.active = false` | **可逆**（rollback SQL 上方提供，CAQA B1 patch 引入）| 同上 — group_project_user 的 task RWCU 版本；OR 語義會讓任何 group_project_user 用戶寫到其他 workspace 的 task |
| `project_rule_workspace_member` 改寫 + `cloud_service_rule_workspace_member` 新增 | **可逆**（revert XML + module update）| ir_rules.xml 用 noupdate=0 包裝，重新 -u 套用 |
| 新欄位（`extra_cloud_service_ids`, `primary_cloud_service_id`, `workspace_id`, `incompatible_extra_cloud_service_ids`, `chat_enabled` 等）| **可逆**（移欄位定義即可；Odoo 不會自動 drop column，留著無害；如需徹底清補 post-uninstall hook）| - |

**無嚴格不可逆動作。**

## Test 結果

跑 `./.claude/skills/k8s-dev-sandbox/scripts/test.sh feat-decouple-cs-projects --module woow_paas_platform`（docker-desktop sandbox）：

```
ERROR woow_feat_decouple_cs_projects odoo.tests.result: 8 failed, 24 error(s) of 437 tests when loading database
```

- **25 / 25 refactor ACs 全綠** + **2 / 2 B1 cross-workspace task isolation tests 綠**（共 39 refactor tests + 2 sanity 全部通過 — AC #5 在 commit b582a44 由 tester 修正 user_ids fixture bug 後也綠）
- **7 / 7 regression 守線維持綠**
- **32 pre-existing infra fails out of scope**（subscription journal / TWD currency / smart_home demo user — 與本 PR 無關，per test scope report `刻意不處理`）
- 排除 pre-existing infra 後，**0 個由本 PR 引入的新失敗**

## Side effects 評估

| 影響面 | 描述 |
|---|---|
| **`base.group_user` ACL on project.project / project.task** | 新增 RWCU 權限（原本只有 group_project_user / group_project_manager 有 create/write/unlink）。record rule 縮緊到 workspace member / project member，所以實際可動範圍與舊行為一致 |
| **`base.group_user` 失去「所有非 followers project 可見」**（`project.project_public_members_rule` deactivated）| 改為 workspace-scoped。對 PaaS 多租戶語義是 tightening，對純 Odoo project 使用情境是 break-change。本 module 是這個 Odoo instance 唯一的 project consumer，影響面僅限於開發者本機可能看到的 demo project |
| **`base.group_user` / `group_project_user` 失去「所有非 followers project 的 task 可見」**（`project.task_visibility_rule` + `project.task_visibility_rule_project_user` deactivated，CAQA B1 patch 對等補上）| 與 project 端對稱；OR-語義會讓 std rule 的 `privacy_visibility != 'followers'` 一直為真，旁路 D3=A 的 workspace 過濾。三條 rule 必須三位一體 deactivate 才能堵住 cross-workspace task data leak |
| **Project create_uid 自動可見** | 新 rule 加 `create_uid` 葉子，避免使用者建完自己的 unbound project 立即看不到的 UX bug |
| **MCP tools 注入** | `discuss_channel._get_user_mcp_tools` **未變動**，仍只從 primary service 取 user MCP tools；extras 的 MCP tools 不會被 chatter 自動拉。Plan §Out of scope 沒寫，這裡記錄為 follow-up 候選 |
| **Controller `has_project` flag** | 保留（CreateProjectModal 移除前端 filter 後，仍可用於 service detail page 顯示綁定狀態）|
| **`_create_project` endpoint** | 路徑不變；當 `cloud_service_id` 為空時走 workspace path 建立 unbound project（不需 server-side 改動，現有 elif workspace branch 已支援）|

## `/simplify` 摘要

- **採納**：移除實驗中加上的 `base.group_user → project.group_project_user` `implied_ids` extension — 自定的 `project_task_rule_workspace_member` (base.group_user) 已能讓 alice 寫自己 workspace 的 task，implied_ids 是冗餘的 scope expansion
- **採納**：簡化 `incompatible_extra_cloud_service_ids` 欄位定義（移除多餘的 relation/column 參數，純 compute 不需 join table）
- **拒絕**：將 `_format_cloud_service_block` 改成獨立 module-level helper — 雖然是純函式，但和 model 行為強耦合（讀取 service / template 欄位），留在 model 上維持就近原則
- **拒絕**：把 4 個新 task 欄位拆成獨立 mixin — 它們只服務 project.task 這個 model，沒第二個 caller，抽 helper / mixin 是 premature

## Follow-up（reporter 候選）

- D9 階段 2：升級 sanitizer 為 whitelist 模式（plan §Decisions D9 已記 follow-up）
- Sentinel `***REDACTED***` i18n 化
- Extras 跨 workspace 候選放寬（D6 從 C → B）
- Chatter `_get_user_mcp_tools` 是否也整合 extras 的 MCP tools
- Plan / pattern docs：把「addon override std ir.rule for ACL hardening 必須在 project + task 兩端對等」寫成 pattern note（CAQA B1 lesson）

## 主要 commits

1. `3081a41` docs(refactor): plan + test scope + report
2. `fec64b0` test(refactor): red-phase tests for 25 ACs
3. `b164d76` feat(refactor): decouple — model + ACL + UI + migration（含 18.0.1.0.22 migration）
4. `9d458f7` fix(skills): in-pod sandbox test runner `-u` + alternate ports
5. `3d43443` docs(refactor): PR description draft
6. `b582a44` test: align AC #5 helper with Odoo 18 project.project schema (tester)
7. `d570006` test: lock cross-workspace project.task read isolation (CAQA B1) (tester)
8. `<this commit>` fix(refactor): deactivate task-side std rules symmetrically (CAQA B1 patch)
