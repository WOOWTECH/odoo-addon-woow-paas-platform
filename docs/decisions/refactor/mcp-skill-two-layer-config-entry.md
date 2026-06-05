# Decision: MCP / Skill 設定入口的雙層架構（Configuration 目錄 × project form 綁定）

**Date**: 2026-06-04
**Status**: accepted
**Components**: `ir.ui.menu`（`menu_mcp_servers` / `menu_skills`）、`project.edit_project` form、`action_skill` / `view_skill_search`、`woow_paas_platform.mcp_server` / `.skill`

## Context

會議決議 2026-06-03 議題 D：MCP / Skill 設定跟著 Project（by project），從 Woow PaaS 後台移到 Project 層級，原話是「**在 Project 點進去的 setting 旁新增一欄**，由後台設定」。第一步（PR #143）把 `menu_mcp_servers` / `menu_skills` 搬到 `project.menu_project_config` 之下後浮現語意矛盾：MCP 選單 action domain 鎖 `scope='system'` —— 掛在「Project 的 Configuration」下，看到的卻是全域的、看不到 project 的。

關鍵觀察：**Odoo 慣例中 app 的 Configuration 選單本來就是 app 級全域設定**（原生 Project 的 Configuration 放 Task Stages / Tags / Activity Types，全是跨 project 的），per-project 設定住在 project form 本身。矛盾的根源不是「選單顯示錯東西」，而是「雙層架構只蓋了目錄層、缺綁定層」—— 會議原話指的正是 form 那層。

名詞對齊（D4 invariant，constraint 守）：`scope='user'` ⟺ `project_id` 有值（= project-scoped）；`scope='system'` ⟺ `project_id` NULL（= 全域）。

## Decision

採**雙層架構**：

```
層 1（全域目錄）  Project → Configuration → MCP Servers / Skills
                  管理 scope='system' 的定義與生命週期
層 2（綁定）      project form → 原生 Settings tab 內嵌 list
                  該 project 的 user-scope MCP servers / skills
```

| 入口 | 內容 | gate | 寫入權 |
|---|---|---|---|
| Configuration → MCP Servers | `scope='system'`（action hard domain，維持） | 繼承父選單 `project.group_project_manager`（PM 唯讀目錄） | `system_readonly` rule 擋；admin via `admin_full` |
| Configuration → Skills | 全部 scope，**default soft filter** `search_default_filter_scope_system`（admin 移除 filter 仍可管 user-scope） | `base.group_system`（admin-only，維持） | publish / deprecate 生命週期 = ops 職權 |
| project form Settings tab | 該 project 的 `mcp_server_ids` / `skill_ids`（One2many 內嵌 list） | 跟著 project form 的存取；record-level 由既有 ir.rule 過濾 | MCP：dual-leg rule（member/follower CRUD）；Skill：唯讀 list（bundle-backed） |
| 前台 /woow 齒輪 | （會議議題 H：MCP 設定拿掉、名稱/描述唯讀 — 另案） | — | — |
| Portal user | 兩個 model 的 ACL CSV 無 portal 行 = **零權限 fail-closed**（invariant） | — | — |

### MCP（hard domain）與 Skill（soft filter）的刻意不對稱

- MCP Configuration 選單 PM 可見 → 目錄必須 hard domain 鎖 system，否則 PM 在全域目錄看到自家 project 綁定，語意又混回去。
- Skills 選單 admin-only → soft filter 即可完成目錄語意，同時保留 admin 在同一列表管理 user-scope 的能力（skill 沒有「PM 在目錄誤看」問題）。

### 實作上的兩條紀律

1. **MCP 內嵌 list 必帶 `context="{'default_scope': 'user'}"`**：model default 是 `'system'`，不帶 context 會讓「從 project form 新增」直接撞 D4 constraint。
2. **Skill 內嵌 list `create="false" delete="false"`**：skill 是 bundle-backed（`bundle_sha256` required / `is_latest` 不變式），只能走 upload wizard 或 agent-runtime API 建立（F-SKU-1 紀律），form 內嵌 list 只做檢視。

## 安全邊界（本決策不動的）

record-level 安全**不在 UI 層**：6-rule split（[[mcp-acl-6-rule-split]]）+ scope-flip write guard（[[scope-flip-write-guard-not-ir-rule]]）+ `api_key` field-level `groups='base.group_system'` 三層全部維持。內嵌 One2many 經 ORM 讀取同樣被 ir.rule 過濾 —— PM 開 project form 只看得到 rule 允許的 row（非 member/follower 看到空 list，fail-closed）。

## Alternatives considered

- **B：Configuration 選單拿掉 domain、全 scope + filter** — 便宜，但 per-project 視角變成「過濾後的全域列表」，與會議「點進 project 設定」心智模型不合；PM 在大列表混看全域與綁定，語意仍亂。
- **C：拆兩條選單（System / Projects）** — 選單膨脹，且「project 的 MCP」放全域 Configuration 下同樣彆扭。

## Open follow-ups（不阻塞本決策）

- **membership 機制統一**：MCP project-scope 走 workspace/follower walk、Skill 走自家 `project_access` role —— 同是「project 的東西」兩套判定，長期應統一（傾向 `project_access`）。本決策的綁定層直接沿用各自既有 rule，不先解。
- **share 架構調整**（最高優先 task #1）動 project 可見性時，須同步補「portal 對 MCP/Skill 零權限」的 negative test。
- internal user（非 PM）對 system-scope 的唯讀可見性維持現狀（agent_template m2m 挑選需要）。
