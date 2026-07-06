---
title: "#275 /woow Project 功能隱藏開關設計"
date: 2026-07-06
issue: 275
status: approved
related:
  - "#275 來源：2026-07-01 會議 todo #11（本階段 Project 不上線）"
  - "portal_project_settings_editable（paas.py:42-50）— fail-closed 範式，但語義不同（ACL 寫入守衛 vs 本案 cosmetic 可見性）"
  - "docs/decisions/refactor/cloud-service-project-decouple.md — 勿為隱藏而 revert CS↔Project 解耦"
---

# #275 /woow Project 功能隱藏開關設計

## 1. 背景與問題框架

2026-07-01 會議決議：本階段 **Project（專案 / 任務 / AI 助理）不上線**。需求是用**同一個 build + 一個參數**把環境割開——**prod 隱藏、stg 顯示**（供內部 dogfood），避免為此維護兩份 build。

現況缺口：

- **nav 硬編**：`Sidebar.js`（`showItem` 只支援 `adminOnly`）與 `BottomNav.js`（**完全無過濾、一律渲染**）都硬編 Projects 入口。
- **路由硬編**：`router.js:20-27`（`projects`、`projects/:id`、`ai-assistant/projects[/:id]`、`ai-assistant/tasks`、`ai-assistant/chat`）。隱藏 nav **不會擋直接 hash 導航**。
- **無 flag 遞送通道**：`/api/me` 僅回 `is_admin`。
- **環境參數已存在**：`woow_paas_platform.deploy_env`（`res_config_settings.py:36-45`）已在 `hooks.py:131-132` 的 `_CONFIG_PARAMETERS`。STG/prod 割開的參數層已具備。

## 2. 決策（已與使用者 + plan-reviewer 定案）

### D1. Flag 來源 — 獨立 `show_projects` config_parameter（fail-closed）

新增獨立 `woow_paas_platform.show_projects` Boolean config_parameter，**fail-closed**（缺值 / `false` = 隱藏）。

**不由 `deploy_env` 推導**：`res_config_settings.py:44` 有成文原則「獨立於 namespace_prefix（**禁隱式推導**）」；把 UI gating 耦合到環境身分會違反此原則，且無法「prod dogfood 顯示」或「stg 臨時隱藏」。獨立 flag 保留正交控制，並 1:1 對齊既有 `_portal_project_settings_editable` fail-closed 範式。

### D2. Enforcement 深度 — cosmetic 前端

flag OFF 時**僅前端收口**：nav 隱藏 + router redirect。**後端 project/task/chat API 不動**。

理由：(a) issue 驗收僅要求前端 nav + router redirect；(b) repo 慣例（`profile.py:50`）明述「前端 nav 隱藏是 cosmetic、非安全邊界」；(c) 隱藏的是使用者自己的專案資料、非敏感外洩；(d) 同一 build 下 stg（flag ON）須照常運作，後端逐一 gate 面積大、回歸風險高、超出驗收。

### D3. 交付通道 — session_info 同步注入（非 /api/me）

`router.init()` 在 OWL `onMounted` **同步**執行，而 `/api/me` 是 async。若直接載入 `/woow#/projects`，async flag 會讓 redirect 慢一拍、閃現 Projects 頁。

`paas_app.xml:13-18` 已內嵌 `<script>` 設 `odoo.__session_info__ = <json session_info>`（**同步可讀**）。故決定把 flag 注入 `session_info`，讓 **router + Sidebar + BottomNav 三個 consumer 共用同一個同步來源**，零 async race、免動 `/api/me`、免給 BottomNav 接 `profileService`。

## 3. 邊界：`show_projects` vs `portal_project_settings_editable`

兩者都是「portal 開關」但 enforcement 語義**完全不同，勿混用**：

| | `portal_project_settings_editable` | `show_projects`（本案） |
|---|---|---|
| 職責 | ACL 寫入守衛（two-layer-acl-lockstep） | cosmetic 可見性 |
| enforcement 語義 | **真安全邊界**：擋 `/api/support-projects` 寫入 endpoint（create/update/delete） | **非安全邊界**：僅前端 nav + router redirect |
| fail 行為 | `false` → 寫入回 403 | `false` → nav 隱藏 + router redirect |
| scope | 後端 API（project CRUD + MCP servers） | 前端 rendering |

`portal_project_settings_editable=false` 時後端 API 仍可**唯讀** project 資料；`show_projects=false` 時前端完全隱藏但後端 API 依舊可正常讀寫。

## 4. 架構

### 4.1 後端

**A1. flag 欄位** — `src/models/res_config_settings.py`
新增 `woow_show_projects` Boolean，`config_parameter='woow_paas_platform.show_projects'`，**無 default**（未設 = fail-closed 隱藏；比照 `woow_portal_project_settings_editable`）。help 註明「cosmetic feature gate，非安全邊界，見 `_show_projects`」。

**A2. lockstep 登記** — `src/hooks.py`
`_CONFIG_PARAMETERS` 加 `'woow_paas_platform.show_projects'`（CLAUDE.md 硬性要求；卸載時清除 config_parameter；repo-lint lockstep gate）。

**A3. fail-closed reader** — `src/controllers/paas.py`
1:1 複製 `_portal_project_settings_editable` 語義：

```python
SHOW_PROJECTS_FLAG = 'woow_paas_platform.show_projects'

def _show_projects(env) -> bool:
    """Cosmetic feature gate（#275）：/woow Projects/AI-assistant 可見性。
    缺值 = off（fail-closed）。**非安全邊界** — 只驅動前端 nav + router
    redirect；後端 project/task/chat API 不受此開關影響（對照
    _portal_project_settings_editable 才是 ACL 寫入守衛）。"""
    return (env['ir.config_parameter'].sudo().get_param(
        SHOW_PROJECTS_FLAG) or '').strip().lower() in ('true', '1')
```

**A4. 同步交付（session_info 注入）** — `paas.py` 的 `paas_app()`

```python
session_info = dict(request.env['ir.http'].session_info())   # shallow copy，不碰全域 ir.http
session_info['woow_show_projects'] = _show_projects(request.env)
return request.render('woow_paas_platform.paas_app', {'session_info': session_info})
```

`paas_app.xml` 已 `json.dumps(session_info)` → **零 XML 改動**。
效能：每次 `/woow` render 即時讀一次 config_parameter、**無快取**——單次頁面載入成本可忽略；code 加 comment 交代此代價邊界。

### 4.2 前端（單一 sync source，三 consumer 共用）

**B1. reader** — `src/static/src/paas/core/platform_config.js`（新檔）
直讀 global，與既有 `ai_service.js:149` / `AiChat.js:573`（bare `odoo.*`）慣例一致，**不 import `@web/session`**（`assets_paas` 以 `web.assets_backend` 為基、不保證 `@web/session` ESM 在 app bundle 內）：

```javascript
/** @odoo-module **/
/** #275 cosmetic feature gate — 同步讀 bootstrap（paas_app 注入 session_info）。
 *  router 與 nav 共用此單一 sync source，避免 async /api/me 的 redirect race。 */
export function showProjects() {
    return odoo?.__session_info__?.woow_show_projects === true;   // fail-closed: 缺值 → false
}
```

**B2. router redirect gate** — `src/static/src/paas/core/router.js`
`projects` 與 `ai-assistant/projects` 共用 `ai-projects` routeName（見 routes 表註解），故用 routeName 集合：

```javascript
const PROJECT_ROUTE_NAMES = new Set([
    "ai-projects", "project-detail", "ai-project-kanban",
    "ai-tasks", "ai-task-detail", "ai-chat",
]);
```

`parseRoute()` **小重構成單一 tail gate**（現有 pattern-match 與 exact-match 兩個 return 點合流），在 `this.current` 定案後統一套 gate：

```javascript
if (!showProjects() && PROJECT_ROUTE_NAMES.has(this.current)) {
    this.current = "dashboard";
    this.params = {};
    if (window.location.hash !== "#/dashboard") window.location.hash = "#/dashboard";
    return;
}
```

**B3. Sidebar** — `src/static/src/paas/layout/sidebar/Sidebar.js`
projects nav 項標 `requiresProjects: true`；擴充 `showItem`（`Sidebar.xml` 已 `t-if="showItem(item)"`）：

```javascript
showItem(item) {
    return (!item.adminOnly || this.isAdmin)
        && (!item.requiresProjects || showProjects());
}
```

**B4. BottomNav** — `src/static/src/paas/layout/bottom_nav/BottomNav.js` + `BottomNav.xml`
⚠️ 實作易漏點：`BottomNav.xml` 目前**完全無 `t-if`**。

- `BottomNav.js`：新增與 Sidebar 同構的 `showItem(item)`；projects 項標 `requiresProjects: true`。
- `BottomNav.xml`：在 nav 迭代加 `t-if="showItem(item)"`（否則 JS 改了畫面不動）。

## 5. 測試策略（TDD 先紅後綠）

**Python**（`src/tests/`）

1. `_show_projects` fail-closed 矩陣：unset / `''` / `'false'` / `'0'` → `False`；`'true'` / `'1'` / `' TRUE '`（strip+lower）→ `True`。
2. `/woow` render 後 `session_info['woow_show_projects']` 值隨 flag 切換（flag on/off 兩情境）。
3. `_CONFIG_PARAMETERS` 含 `woow_paas_platform.show_projects`（lockstep gate）。

**前端 headed playwright**（依 `woow playwright E2E gotchas`）

4. **flag ON**：桌面側欄 + 手機底部導航皆有 Projects；`#/projects` 正常渲染。
5. **flag OFF**：兩 nav 皆無 Projects；直打 `#/projects` 被 redirect 到 dashboard；**deep-link bookmark**（`#/projects/123` → project-detail、`#/ai-assistant/tasks`、`#/ai-assistant/chat/1` 等）亦被 redirect；無 OWL console error。

## 6. 部署（非 code）

- **stg**：設 `woow_paas_platform.show_projects=true`（Settings → Woow PaaS，或 values）。
- **prod**：留空 → fail-closed 隱藏。
- 同一 build，只差參數。

## 7. 明確不做（YAGNI / 防越界）

- ❌ 後端 endpoint 守衛（cosmetic 決策 D2）。
- ❌ 不 revert CS↔Project 解耦（`cloud_service.py` `project_ids` 維持 M2M；見 related doc）。
- ❌ Header 麵包屑不動（route 被 gate 後那段 project 麵包屑 code dead-but-harmless）。
- ⚠️ **Project 交叉入口（cosmetic 一併收口）**：Dashboard 確認無 project 入口（grep）。但 service 路由（不在 PROJECT_ROUTE_NAMES、flag OFF 時仍可達）下有兩個 project 交叉入口，**須一併以 `showProjects()` gate**——否則 router gate 只擋得住導航、擋不掉「建資料」與「按鈕可見」：
  - `ServiceDetailPage`（`ServiceDetailPage.xml:55-70`）「Support Project」/「Create Support Project」按鈕。**關鍵**：「Create Support Project」在 flag OFF 的 prod 仍能建出隱藏 project 資料 → 超出 cosmetic、違背「Project 不上線」；「Support Project」點了 dead-end 回 dashboard。
  - `McpServersTab`（`McpServersTab.xml:18-22`）「Go to Project Settings」按鈕（→ `projects/{id}` 被 gate 打回 dashboard）。
  - 實作見 plan Task 7（gate 這些交叉入口的按鈕；仍屬 cosmetic 前端，後端 create endpoint 不動——只是 flag OFF 時無 UI 路徑觸發）。
- ℹ️ **連鎖效應（intended）**：Projects 隱藏後，MCP-via-project 管理入口（`McpServersTab` 的「Go to Project Settings」）一併不可達——因 MCP 綁 project 為單一真實來源。此為刻意；與 #271 `portal_project_settings_editable` 各自正交，勿混判。

## 8. 檔案異動總覽

| 類 | 檔案 |
|---|---|
| 後端 | `src/models/res_config_settings.py`、`src/hooks.py`、`src/controllers/paas.py` |
| 前端 | `src/static/src/paas/core/platform_config.js`（新）、`core/router.js`、`layout/sidebar/Sidebar.js`、`layout/bottom_nav/BottomNav.js` + `BottomNav.xml` |
| 測試 | `src/tests/`（Python 3 項）、playwright E2E（flag ON/OFF + deep-link） |

## 9. 實作守線（pattern-guard read-first）

- **`paas.py`（controller-endpoint）**：`_show_projects` 的 `ir.config_parameter.sudo().get_param` 與既有 `_portal_project_settings_editable` 同款；controller `.sudo()` 有 repo-lint 白名單 gate，須確認此 read-only config sudo 在白名單內（標準 Odoo idiom）。讀 `two-layer-acl-lockstep.md`、`orm-vs-endpoint-acl-test-coverage.md`。
- **`BottomNav.xml`（owl-template）**：加 `t-if` 前讀 `owl-template-crash-class-syntax.md`（OWL 語法錯誤過 XML parse + Python test，runtime 全站白屏，唯 headed + repo-lint 攔）。

## 10. 審查共識記錄（plan-reviewer，2026-07-06）

多 agent 對抗審查（maestri `plan-reviewer`）逐點結論：五點全數同意、無阻擋核心（「方向 solid，可進入實作」）。實質改進與確認：

- **★ reader 機制改進**：原設計用 `@web/session`，改為直讀 `odoo?.__session_info__`（`assets_paas` 以 `web.assets_backend` 為基不保證 ESM module 在 bundle；`ai_service.js:149` / `AiChat.js:573` 既有 bare `odoo.*` 先例 — 已自驗屬實）。
- **單一 sync source 確認**：router + Sidebar + BottomNav 皆讀 `showProjects()`，不接 `profileService`（避免 async race 閃現）。
- **邊界註記**（§3）、**router 單一 tail gate + 共用 routeName 註解**（§4.2 B2）、**perf 無快取 comment**（§4.1 A4）、**deep-link bookmark 測試**（§5 case 5）、**BottomNav.xml `t-if` 易漏點**（§4.2 B4）皆納入。
