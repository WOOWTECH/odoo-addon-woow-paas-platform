---
title: Skill 後台管理 UI 設計
date: 2026-05-27
status: approved
branch: feat/skill-list
topic: skill-admin-backend-ui
related:
  - src/models/skill.py
  - src/views/mcp_server_views.xml
  - docs/guides/paas-skill-system/10-roadmap-phases.md
  - docs/plans/2026-Q2/agent-runtime-pod-design.md
---

# Skill 後台管理 UI 設計

## 背景

`woow_paas_platform.skill`（`src/models/skill.py`）目前只有資料層 model + ACL，**沒有任何 Odoo 後台管理介面**：

- `src/views/` 沒有 skill 相關的 view 檔
- 沒有 menu item、沒有 OWL 前端頁
- skill 只能透過既有上傳 API（`src/controllers/agent_runtime_skill.py`，`/api/agent-runtimes/skill-bundle`）與 agent-runtime pod 互動

對比之下，MCP（`woow_paas_platform.mcp_server`）有完整的後台 CRUD：`src/views/mcp_server_views.xml` 提供 form（含 header transition button + statusbar）、list（state badge）、action、menu。

本設計補上 skill 的後台管理 UI，**對齊 MCP 的既有 pattern**，讓平台維運者能稽核、改狀態、緊急下架 skill。

### 順帶修正的 doc drift

`docs/guides/paas-skill-system/10-roadmap-phases.md:36` 聲稱「Phase A 已完成 skill model + 後台 view」，但實際上 view 並不存在。本次一併修正該描述。

## 已核准的關鍵決策

| 決策 | 結果 | 理由 |
|------|------|------|
| **管理範圍** | 平台維運全域頁：menu 限 `base.group_system`、list 列出跨所有 project 的全部 skill | skill 全為 project-scoped（`project_id` 必填），無 system scope 可像 MCP 那樣 domain 過濾；改採 admin 維運視角，且 bundle/state 等敏感操作本就偏維運 |
| **狀態管理** | 完整 transition button：`skill.py` 補 action method、維護 `is_latest` 不變式、`state` 改 readonly | state lifecycle 是 skill 管理核心價值；`is_latest` 需邏輯維護，光點 selection 會破壞不變式 |
| **新增來源** | 唯讀檢視 + 禁後台 create：bundle 走既有上傳 API、form 隱藏 Create、bundle 欄位唯讀 | `bundle_sha256` required+readonly、`bundle_attachment_id` 限 `group_system`，後台手動建會卡住並破壞不變式；既有 controller 已負責解析 SKILL.md 算 sha256 |

## 設計

### ① Model 變更（`src/models/skill.py`）

**欄位：**

- `state`：加上 `readonly=True` — 只能透過 transition button 改（對齊 MCP 的 `state` readonly）。
- 新增 `published_at = fields.Datetime(readonly=True)` — `is_latest` 重算的排序依據。比 `uploaded_at` 準確，因為上傳時間 ≠ 發佈時間，且 skill 可能 deprecated 後 republish。

**`is_latest` 不變式（核心）：**

> 每個 `(project_id, name)` group 內，至多一筆 `is_latest=True`，且該筆必為 `state='published'`。

**`_recompute_is_latest(project_id, name)` helper：**

1. 撈該 `(project_id, name)` 下所有 `state='published'` 的 skill。
2. 若有：按 `published_at desc`（tiebreak `id desc`）取第一筆設 `is_latest=True`，同 group 其餘設 `False`。
3. 若無 published：同 group 全部 `is_latest=False`。

每個 transition method 結尾都呼叫此 helper 收斂不變式（單一維護點）。

**三個 transition method**（皆 `ensure_one`，含合法來源狀態的前置 guard，非法來源 raise `UserError` 以防 API 誤用）：

| Method | 合法來源 state | 動作 |
|--------|--------------|------|
| `action_publish` | `draft`, `deprecated` | → `published`；set `published_at = now()`；recompute |
| `action_deprecate` | `published` | → `deprecated`；recompute（latest 自動移交次新 published） |
| `action_archive` | `draft`, `published`, `deprecated` | → `archived`；recompute |

`archived` 為終態：本次不做 unarchive（列 follow-up）。

### ② View（新增 `src/views/skill_views.xml`）

結構對齊 `src/views/mcp_server_views.xml`。

**List view：**

| 欄位 | 顯示 |
|------|------|
| `project_id` | |
| `name` | |
| `version` | |
| `state` | badge：`published`=success、`deprecated`=warning、`archived`=muted、`draft`=info |
| `is_latest` | boolean_toggle |
| `bundle_size_kb` | |
| `uploaded_by_uid` | |
| `uploaded_at` | |

**Form view：**

- **header**：
  - `Publish` button — `invisible="state not in ('draft','deprecated')"`
  - `Deprecate` button — `invisible="state != 'published'"`
  - `Archive` button — `invisible="state == 'archived'"`
  - `state` widget=`statusbar`（readonly）
- **sheet**：
  - `oe_title`：`name` + `version`
  - group「識別」：`name` / `version` / `title` / `is_latest` / `project_id`（全 readonly）
  - group「Bundle」：`bundle_sha256` / `bundle_size_kb` / `uploaded_by_uid` / `uploaded_at` / `published_at`（全 readonly）
  - `description`（readonly）
  - notebook page「Metadata」：`skill_metadata`（唯讀 JSON 文字）

**Action（`act_window`）：**

- `view_mode = list,form`
- **無 domain**（全域列出所有 project 的 skill）
- `context = {'create': False}` — 禁後台新增

**Menu：**

```xml
<menuitem id="menu_skills"
          name="Skills"
          parent="menu_woow_paas_platform_root"
          action="action_skill"
          groups="base.group_system"
          sequence="60"/>
```

`sequence=60` 緊接 MCP 的 50（root 現有：Cloud=30、Smart Home=40、MCP/OAuth=50，見 `src/views/menu.xml`）。

### ③ 安全 / ACL

- menu `groups="base.group_system"` → 只有系統管理員看得到入口。
- 既有 `ir.model.access.csv` 的 `access_skill_admin`（`base.group_system` 1,1,1,1）已足夠支援 transition write，**不需新增 ACL 列**。
- form `create="false"` + action `context={'create': False}` → 落實「禁後台 create」。
- `bundle_attachment_id` 已是 `groups='base.group_system'`，form 顯示但唯讀。

### ④ Manifest 註冊（`src/__manifest__.py`）

在 data 清單中，緊接 `views/mcp_server_views.xml` 之後加入 `views/skill_views.xml`。

### ⑤ Test（新增 `src/tests/test_skill_admin_lifecycle.py`）

涵蓋 transition 行為與 `is_latest` 不變式：

- `test_publish_from_draft_sets_latest` — draft publish 後 `is_latest=True`、`published_at` 有值
- `test_publish_second_version_steals_latest` — v1 published(latest)，publish v2 → v2 latest、v1 `is_latest=False`
- `test_deprecate_latest_reassigns_to_next_published` — v1+v2 published（v2 latest），deprecate v2 → latest 移交 v1
- `test_deprecate_only_published_clears_latest` — 唯一 published deprecate 後同 group 無 latest
- `test_archive_clears_latest_and_recomputes`
- `test_publish_from_archived_raises` — 非法 transition raise `UserError`
- `test_invariant_at_most_one_latest_per_group` — 多次混合 transition 後，同 group `is_latest` count ≤ 1

### ⑥ Doc 修正

`docs/guides/paas-skill-system/10-roadmap-phases.md:36`：把「Phase A 已完成 skill model + 後台 view」改為「Phase A 完成 model + ACL；後台管理 view 於 `feat/skill-list` 補上」。

## 非目標（Non-goals / YAGNI）

- **OWL 前端**（`/woow` 前台）的 skill 管理頁 — 本次只做 Odoo backend。
- **後台上傳 bundle / 從零 create** — bundle 一律走既有上傳 API。
- **放寬既有 model 約束** — `bundle_sha256` required、unique(project,name,version) 等維持原狀。
- **`archived` → 復原（unarchive）** — archived 視為終態（follow-up）。
- **semver 精確排序** — `is_latest` 以 `published_at` 近似最新發佈，不解析 semver 字串大小（follow-up）。

## 實作注意 / 待驗證

- **`group_system` 對 skill `ir.rule` 的可見性**：list 要全域顯示跨 project 的 skill，需驗證 skill 既有 project-based `ir.rule`（project_access walk）對 `base.group_system` 是否全域放行。Odoo system group 通常 bypass record rule，但須確認 skill 沒有針對 group_system 的 restrictive rule，否則 admin 在後台會看不到別人 project 的 skill。
- transition method 改了 `state` 後，Odoo form 需重新載入才會更新 button 的 `invisible` — method 回傳值可不帶 reload（Odoo 預設 write 後 refresh），實作時確認 button 顯示隨 state 即時更新。
- **`is_latest` 雙寫漂移風險**：既有上傳 API（`src/controllers/agent_runtime_skill.py`）若會直接以 `state='published'` 建立 skill，將繞過本次的 `_recompute_is_latest`。本 spec 不改 controller，但列為 follow-up：上傳路徑建立／發佈 published skill 時應共用同一 helper，否則後台 transition 維護的不變式會被 API 路徑破壞。

## 檔案異動清單

| 檔案 | 動作 |
|------|------|
| `src/models/skill.py` | 修改：`published_at` 欄位、`state` readonly、`_recompute_is_latest` helper、`action_publish/deprecate/archive` |
| `src/views/skill_views.xml` | 新增：list + form + action + menu |
| `src/__manifest__.py` | 修改：data 註冊 `views/skill_views.xml` |
| `src/tests/test_skill_admin_lifecycle.py` | 新增：transition + 不變式測試 |
| `docs/guides/paas-skill-system/10-roadmap-phases.md` | 修正：Phase A doc drift |
