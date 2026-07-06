# /woow Project 隱藏開關 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 /woow 前端加一個 `show_projects` 開關，flag OFF（prod 預設）時隱藏 Projects/AI-assistant nav + 擋直接 hash 導航；flag ON（stg）維持現行。兩環境同一 build，只差參數。

**Architecture:** 後端新增 fail-closed config_parameter `woow_paas_platform.show_projects`，由 `paas_app()` 同步注入 `session_info['woow_show_projects']`；前端三個 consumer（router、Sidebar、BottomNav）共用同步 reader `showProjects()`（直讀 `odoo.__session_info__`）。cosmetic-only：後端 API 不動。

**Tech Stack:** Odoo 18 addon（Python controllers/models/views + OWL/JS 前端）；測試 = Odoo `TransactionCase`/`HttpCase`；前端行為驗證 = headed playwright（repo 無 JS 單元 harness）。

## Global Constraints

以下值 verbatim，每個 task 隱含遵守：

- config_parameter key：`woow_paas_platform.show_projects`
- 後端 helper：`_show_projects(env)` + 常數 `SHOW_PROJECTS_FLAG`，放 `src/controllers/paas.py`
- fail-closed 語義：`(env['ir.config_parameter'].sudo().get_param(SHOW_PROJECTS_FLAG) or '').strip().lower() in ('true', '1')`
- session_info key：`woow_show_projects`（值為 Python `bool`）
- Settings 欄位：`woow_show_projects`（`res.config.settings` Boolean，無 default）
- 前端 reader：`showProjects()`（`src/static/src/paas/core/platform_config.js`）→ `odoo?.__session_info__?.woow_show_projects === true`
- 受管制 routeName（6）：`ai-projects`、`project-detail`、`ai-project-kanban`、`ai-tasks`、`ai-task-detail`、`ai-chat`
- manifest 版號：`18.0.1.0.43` → `18.0.1.0.44`
- repo-lint sudo：`paas.py` 只能**新增 1 個** `.sudo()`（ceiling 26 / 現 25），多了要 `--update-baseline`
- commit 訊息：`{type}: #275 …`（zh-tw，prefix 英文小寫，**不加** AI 署名）
- 測試 tag：`TransactionCase` 不加 tag；`HttpCase` 必配 `@tagged('post_install', '-at_install')`
- 新測試檔要在 `src/tests/__init__.py` 手動 `from . import …` 註冊
- **不做**：後端 endpoint 守衛、revert CS↔Project 解耦、改 Header 麵包屑

## 執行環境備註

- 工作目錄 = worktree `.claude/worktrees/feat-275-project-hide-toggle`，branch `feat/275-woow-project-hide-toggle`（base = develop 6869be2）。
- 跑 Odoo 測試：`./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:<Class> --stop-after-init`，經 `k8s-dev-sandbox` test.sh 或 docker-build-local（見 `reference_docker_build_local_odoo_test`）。單一 class 用 `--test-tags /woow_paas_platform:TestX`。
- 前端資產改動需 `-u woow_paas_platform` 重建 assets bundle 才生效。
- **pattern-guard read-first**（一般 session 會觸發；本 maestri session 對 worktree 路徑 fail-open 不觸發，但仍應履行）：編輯 `src/controllers/paas.py` 觸發 controller-endpoint 守線（讀 `two-layer-acl-lockstep.md` / `orm-vs-endpoint-acl-test-coverage.md`）；編輯 `src/hooks.py` 觸發 **seed-default-template** 守線（與本改動無關，讀完 retry 即放行）；編輯 `BottomNav.xml` / Service 頁 `.xml` 觸發 **owl-template** 守線（讀 `owl-template-crash-class-syntax.md`）。讀完重試同一編輯即放行。

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `src/controllers/paas.py` | `SHOW_PROJECTS_FLAG` + `_show_projects()` fail-closed helper；`paas_app()` 注入 session_info | Modify |
| `src/models/res_config_settings.py` | `woow_show_projects` Boolean 欄位 | Modify |
| `src/views/res_config_settings_views.xml` | Portal block 加 `<setting>` 讓 UI 可切換 | Modify |
| `src/hooks.py` | `_CONFIG_PARAMETERS` 加 key（卸載清理） | Modify |
| `src/__manifest__.py` | 版號 → 18.0.1.0.44 | Modify |
| `src/static/src/paas/core/platform_config.js` | `showProjects()` 同步 reader | **Create** |
| `src/static/src/paas/core/router.js` | `PROJECT_ROUTE_NAMES` + `_applyProjectGate()` redirect | Modify |
| `src/static/src/paas/layout/sidebar/Sidebar.js` | projects 項 `requiresProjects` + `showItem` 擴充 | Modify |
| `src/static/src/paas/layout/bottom_nav/BottomNav.js` | 新增 `showItem` + projects 項 `requiresProjects` | Modify |
| `src/static/src/paas/layout/bottom_nav/BottomNav.xml` | `<a>` 加 `t-if="showItem(item)"` | Modify |
| `src/static/src/paas/pages/service/ServiceDetailPage.js` + `.xml` | Support Project 按鈕組加 `projectsVisible` gate（Task 7） | Modify |
| `src/static/src/paas/pages/service/tabs/McpServersTab.js` + `.xml` | 「Go to Project Settings」按鈕加 `projectsVisible` gate（Task 7） | Modify |
| `src/tests/test_show_projects_flag.py` | 3 test class（matrix / lockstep / session_info） | **Create** |
| `src/tests/__init__.py` | 註冊新測試檔 | Modify |

依賴序：後端 Task 1→2→3 → 前端 Task 4→5→6 → Service 交叉入口 Task 7 → E2E Task 8。

---

### Task 1: 後端 fail-closed helper `_show_projects` + 矩陣測試

**Files:**
- Create: `src/tests/test_show_projects_flag.py`
- Modify: `src/tests/__init__.py`
- Modify: `src/controllers/paas.py`（在 `_portal_project_settings_editable` 之後、`_validate_mcp_url` 之前，約 line 50-53 之間插入）

**Interfaces:**
- Produces：`SHOW_PROJECTS_FLAG: str`、`_show_projects(env) -> bool`（module-level，`src/controllers/paas.py`）。後續 Task 2（session_info）、前端不直接 import 此二者，但 Task 2 的 HttpCase 會 import `SHOW_PROJECTS_FLAG`。

- [ ] **Step 1: 寫失敗測試**

Create `src/tests/test_show_projects_flag.py`：

```python
"""#275 /woow Project 隱藏開關 — 後端。

分三 class：
- TestShowProjectsFlag：_show_projects fail-closed 矩陣（Task 1）
- TestShowProjectsConfigLockstep：_CONFIG_PARAMETERS 卸載清理 lockstep（Task 3）
- TestWoowSessionInfoShowProjects：/woow session_info 注入（Task 2）
"""
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import (
    _show_projects, SHOW_PROJECTS_FLAG,
)


class TestShowProjectsFlag(TransactionCase):
    """fail-closed：只有 'true'/'1'（strip+lower 後）才 True，其餘一律 False。"""

    def _set(self, raw):
        self.env['ir.config_parameter'].sudo().set_param(SHOW_PROJECTS_FLAG, raw)

    def test_unset_is_false(self):
        # falsy → Odoo 刪除 param → 等效未設；fail-closed 隱藏
        self._set(False)
        self.assertFalse(_show_projects(self.env))

    def test_falsey_raw_values_are_false(self):
        for raw in ('', 'false', 'False', '0', 'no', 'off'):
            with self.subTest(raw=raw):
                self._set(raw)
                self.assertFalse(_show_projects(self.env))

    def test_truthy_raw_values_are_true(self):
        for raw in ('true', 'True', '1', ' TRUE '):  # strip+lower 後命中白名單
            with self.subTest(raw=raw):
                self._set(raw)
                self.assertTrue(_show_projects(self.env))
```

Register in `src/tests/__init__.py`（append 一行，比照既有 `from . import test_deploy_env_config`）：

```python
from . import test_show_projects_flag
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestShowProjectsFlag --stop-after-init`
Expected: FAIL — `ImportError: cannot import name '_show_projects'`（helper 尚未存在）。

- [ ] **Step 3: 實作 helper**

在 `src/controllers/paas.py`，`_portal_project_settings_editable`（結束於 line 50）之後、`_validate_mcp_url`（line 53）之前插入：

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

> repo-lint：此為 `paas.py` 第 26 個 `.sudo()`（ceiling 26）→ `26 > 26 == False` 過關，免動 baseline。

- [ ] **Step 4: 跑測試確認通過**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestShowProjectsFlag --stop-after-init`
Expected: PASS（3 test method 綠）。

- [ ] **Step 5: repo-lint 確認**

Run: `python3 scripts/audit/repo-lint.py`
Expected: PASS（controller-sudo 不報 `paas.py`）。

- [ ] **Step 6: Commit**

```bash
git add src/controllers/paas.py src/tests/test_show_projects_flag.py src/tests/__init__.py
git commit -m "feat: #275 _show_projects fail-closed helper（+ 矩陣測試）"
```

---

### Task 2: `paas_app()` 同步注入 `session_info['woow_show_projects']` + HttpCase

**Files:**
- Modify: `src/controllers/paas.py`（`paas_app()`，約 line 100-116）
- Modify: `src/tests/test_show_projects_flag.py`（append 一個 HttpCase class）

**Interfaces:**
- Consumes：Task 1 的 `_show_projects`、`SHOW_PROJECTS_FLAG`。
- Produces：`/woow` 回傳 HTML 內嵌 `odoo.__session_info__.woow_show_projects`（bool）。前端 Task 4 的 `showProjects()` 消費。

- [ ] **Step 1: 寫失敗測試**

Append 到 `src/tests/test_show_projects_flag.py`：

```python
import json  # noqa: E402  (module 頂端已 import TransactionCase；此為新增依賴)
from odoo.tests.common import HttpCase, tagged  # noqa: E402


@tagged('post_install', '-at_install')
class TestWoowSessionInfoShowProjects(HttpCase):
    """/woow 把 woow_show_projects 注入 session_info（值隨 flag）。
    用 substring 斷言（json.dumps 產 `"woow_show_projects": true/false`），
    避免解析整包 session_info JSON 的脆弱正則。"""

    def setUp(self):
        super().setUp()
        self.user = self.env['res.users'].sudo().create({
            'name': 'sp_user', 'login': 'sp_user', 'password': 'sp_user',
            'email': 'sp_user@test.example',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def _woow_html(self):
        res = self.url_open('/woow', timeout=30)
        self.assertEqual(res.status_code, 200, res.text[:200])
        return res.text

    def test_flag_on_injects_true(self):
        self.env['ir.config_parameter'].sudo().set_param(SHOW_PROJECTS_FLAG, 'True')
        self.authenticate('sp_user', 'sp_user')
        self.assertIn('"woow_show_projects": true', self._woow_html())

    def test_flag_off_injects_false(self):
        self.env['ir.config_parameter'].sudo().set_param(SHOW_PROJECTS_FLAG, 'False')
        self.authenticate('sp_user', 'sp_user')
        self.assertIn('"woow_show_projects": false', self._woow_html())
```

> `SHOW_PROJECTS_FLAG` 已在檔案頂端 import（Task 1）。`json` import 本 class 未直接用，可省；保留亦無害 — 若 lint 抱怨 unused，刪 `import json` 那行。

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestWoowSessionInfoShowProjects --stop-after-init`
Expected: FAIL — HTML 不含 `"woow_show_projects"`（尚未注入）。

- [ ] **Step 3: 實作注入**

`src/controllers/paas.py` 的 `paas_app()`，把原本：

```python
        session_info = request.env['ir.http'].session_info()
        return request.render(
            'woow_paas_platform.paas_app',
            {'session_info': session_info}
        )
```

改為：

```python
        # shallow copy：不 mutate 全域 ir.http.session_info()，只在 /woow 這頁附掛。
        # #275 cosmetic gate — 每次 render 即時讀一次 config_parameter（無快取，
        # 單次頁面載入成本可忽略）；router + nav 同步共用此注入值。
        session_info = dict(request.env['ir.http'].session_info())
        session_info['woow_show_projects'] = _show_projects(request.env)
        return request.render(
            'woow_paas_platform.paas_app',
            {'session_info': session_info}
        )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestWoowSessionInfoShowProjects --stop-after-init`
Expected: PASS（flag on→true、off→false 各一）。

- [ ] **Step 5: Commit**

```bash
git add src/controllers/paas.py src/tests/test_show_projects_flag.py
git commit -m "feat: #275 /woow session_info 注入 woow_show_projects（+ HttpCase）"
```

---

### Task 3: Settings 開關（欄位 + view）+ 卸載清理 lockstep + 版號

**Files:**
- Modify: `src/hooks.py`（`_CONFIG_PARAMETERS`，line 123-145）
- Modify: `src/models/res_config_settings.py`（`woow_portal_project_settings_editable` 之後，line 121 後）
- Modify: `src/views/res_config_settings_views.xml`（Portal block，line 114-119）
- Modify: `src/__manifest__.py`（version，line 3）
- Modify: `src/tests/test_show_projects_flag.py`（append lockstep class）

**Interfaces:**
- Consumes：`hooks._CONFIG_PARAMETERS`（list）、`woow_paas_platform.show_projects` key。
- Produces：Settings → Woow PaaS → Portal 有「Show Projects」勾選；卸載時清 config_parameter。

- [ ] **Step 1: 寫失敗測試**

Append 到 `src/tests/test_show_projects_flag.py`：

```python
from odoo.addons.woow_paas_platform import hooks  # noqa: E402


class TestShowProjectsConfigLockstep(TransactionCase):
    """卸載清理 lockstep：show_projects 須在 hooks._CONFIG_PARAMETERS
    （CLAUDE.md 硬性要求；比照 test_deploy_env_config）。"""

    def test_show_projects_in_config_parameters_cleanup_list(self):
        self.assertIn(
            'woow_paas_platform.show_projects', hooks._CONFIG_PARAMETERS)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags /woow_paas_platform:TestShowProjectsConfigLockstep --stop-after-init`
Expected: FAIL — `AssertionError: 'woow_paas_platform.show_projects' not found in [...]`。

- [ ] **Step 3a: hooks 登記**

`src/hooks.py` 的 `_CONFIG_PARAMETERS`，在 `'woow_paas_platform.portal_project_settings_editable',`（line 138）之後加一行：

```python
    'woow_paas_platform.show_projects',
```

- [ ] **Step 3b: res_config 欄位**

`src/models/res_config_settings.py`，在 `woow_portal_project_settings_editable` 欄位（結束於 line 121）之後插入：

```python
    # Portal (/woow) Project visibility（#275 cosmetic gate，非安全邊界）
    woow_show_projects = fields.Boolean(
        string='Show Projects in /woow',
        config_parameter='woow_paas_platform.show_projects',
        help='When ON, /woow shows the Projects / AI-assistant (projects, '
             'tasks, chat) nav + routes. Default OFF: Projects are hidden '
             '(cosmetic feature gate, same build per-env parameter). NOT a '
             'security boundary — see _show_projects in controllers/paas.py.',
    )
```

- [ ] **Step 3c: settings view**

`src/views/res_config_settings_views.xml`，Portal block（line 114-119）內，於 `woow_portal_project_settings_editable` 的 `</setting>` 之後、`</block>` 之前插入：

```xml
                        <setting string="Show Projects (/woow)"
                                 help="Show the Projects / AI-assistant nav + routes in /woow. OFF = hidden (cosmetic per-env gate, #275). Not a security boundary.">
                            <field name="woow_show_projects"/>
                        </setting>
```

- [ ] **Step 3d: 版號 bump**

`src/__manifest__.py:3`，`"version": "18.0.1.0.43"` → `"version": "18.0.1.0.44"`。

- [ ] **Step 4: 跑測試 + 模組載入確認通過**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestShowProjectsConfigLockstep --stop-after-init`
Expected: PASS（lockstep 綠；`-u` 讓新 view/field 載入，view XML 有錯會在此炸出）。

- [ ] **Step 5: repo-lint 確認**

Run: `python3 scripts/audit/repo-lint.py`
Expected: PASS。

- [ ] **Step 6: Commit**

```bash
git add src/hooks.py src/models/res_config_settings.py src/views/res_config_settings_views.xml src/__manifest__.py src/tests/test_show_projects_flag.py
git commit -m "feat: #275 show_projects Settings 開關 + 卸載清理 lockstep + 版號 .44"
```

---

### Task 4: 前端同步 reader `showProjects()`

**Files:**
- Create: `src/static/src/paas/core/platform_config.js`

**Interfaces:**
- Produces：`export function showProjects(): boolean`。Task 5（router）、Task 6（nav）import。

> 前端無 JS 單元 harness → 本 task 的行為驗證併入 Task 7 headed E2E（repo 慣例；見 `owl-template-crash-class-syntax.md`「XML/Python 綠 ≠ render 成功」）。本 task 僅驗 import 解析 + assets build。

- [ ] **Step 1: 建 reader**

Create `src/static/src/paas/core/platform_config.js`：

```javascript
/** @odoo-module **/

/**
 * #275 cosmetic feature gate — 同步讀 bootstrap（paas_app 把 woow_show_projects
 * 注入 odoo.__session_info__）。router 與 nav 共用此單一同步來源，避免 async
 * /api/me 造成的 redirect race / Projects 閃現。
 *
 * fail-closed：缺 key / 非 true → false（隱藏），與後端 _show_projects 對齊。
 * 直讀 global 與既有 ai_service.js:149 / AiChat.js:573（bare `odoo.*`）慣例一致，
 * 不 import @web/session（assets_paas 以 web.assets_backend 為基，不保證該 ESM
 * module 在 app bundle 內）。
 */
export function showProjects() {
    return odoo?.__session_info__?.woow_show_projects === true;
}
```

- [ ] **Step 2: assets build 確認**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init`
Expected: 無 assets 編譯錯誤、模組載入乾淨（新檔被 `woow_paas_platform/static/src/paas/**/*.js` glob 納入 bundle，無需改 manifest）。

- [ ] **Step 3: Commit**

```bash
git add src/static/src/paas/core/platform_config.js
git commit -m "feat: #275 前端 showProjects() 同步 reader（platform_config）"
```

---

### Task 5: router 對 project 路由 fail-closed redirect gate

**Files:**
- Modify: `src/static/src/paas/core/router.js`

**Interfaces:**
- Consumes：Task 4 `showProjects()`。
- Produces：flag OFF 時，`parseRoute()` 把 project routeName 一律導到 `dashboard`。

> 行為驗證併入 Task 7 headed E2E。

- [ ] **Step 1: import + 常數**

`src/static/src/paas/core/router.js`，在 `import { reactive } from "@odoo/owl";`（line 2）之後加：

```javascript
import { showProjects } from "./platform_config";
```

在 `routes` 陣列（結束於 line 39）之後、`export const router = reactive({`（line 41）之前加：

```javascript
// #275：flag OFF 時這些 routeName 一律 redirect 到 dashboard（cosmetic gate）。
// `projects` 與 `ai-assistant/projects` 共用 `ai-projects` routeName（見 routes 註解）。
const PROJECT_ROUTE_NAMES = new Set([
    "ai-projects", "project-detail", "ai-project-kanban",
    "ai-tasks", "ai-task-detail", "ai-chat",
]);
```

- [ ] **Step 2: gate 方法 + 兩個 tail 呼叫**

在 `parseRoute()` 內、pattern-match 分支的 `return;`（line 107，`if (match) {...}` 尾端）之前插入 `this._applyProjectGate();`，即：

```javascript
                    } else if (route.routeName === 'ai-project-kanban') {
                        this.params.id = match[1];
                    }
                    this._applyProjectGate();
                    return;
```

在 exact-match 分支尾端（line 115）`this.current = route ? route.routeName : "not-found";` 之後加一行：

```javascript
        const route = routes.find(r => r.path === path);
        this.current = route ? route.routeName : "not-found";
        this._applyProjectGate();
    },
```

在 `parseRoute()` method 之後（`getRouteFromHash()` 之前）新增 method：

```javascript
    _applyProjectGate() {
        // #275：flag OFF 時把 project 路由收斂到 dashboard。同步 gate → 直接
        // hash 載入 /woow#/projects 也不會閃現 project 頁。改 hash 只是同步 URL；
        // this.current 已即時設為 dashboard，hashchange 再跑一次為冪等。
        if (!showProjects() && PROJECT_ROUTE_NAMES.has(this.current)) {
            this.current = "dashboard";
            this.params = {};
            if (window.location.hash !== "#/dashboard") {
                window.location.hash = "#/dashboard";
            }
        }
    },
```

- [ ] **Step 3: assets build 確認**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init`
Expected: 無編譯錯誤。

- [ ] **Step 4: Commit**

```bash
git add src/static/src/paas/core/router.js
git commit -m "feat: #275 router 對 project 路由 fail-closed redirect gate"
```

---

### Task 6: Sidebar / BottomNav 依 `showProjects` 隱藏 Projects nav

**Files:**
- Modify: `src/static/src/paas/layout/sidebar/Sidebar.js`
- Modify: `src/static/src/paas/layout/bottom_nav/BottomNav.js`
- Modify: `src/static/src/paas/layout/bottom_nav/BottomNav.xml`

**Interfaces:**
- Consumes：Task 4 `showProjects()`。
- 註：`Sidebar.xml` **不改**（已用 `t-elif="showItem(item)"`）。

> 行為驗證併入 Task 7 headed E2E（含**手機視窗 resize** 才會出現 BottomNav）。

- [ ] **Step 1: Sidebar.js**

`src/static/src/paas/layout/sidebar/Sidebar.js`，在 `import { profileService } from "../../services/profile_service";`（line 3）之後加：

```javascript
import { showProjects } from "../../core/platform_config";
```

`navItems` 的 projects 項（line 21）由：

```javascript
        { path: "projects", name: "Projects", icon: "folder" },
```

改為：

```javascript
        { path: "projects", name: "Projects", icon: "folder", requiresProjects: true },
```

`showItem`（line 36-38）由：

```javascript
    showItem(item) {
        return !item.adminOnly || this.isAdmin;
    }
```

改為：

```javascript
    showItem(item) {
        return (!item.adminOnly || this.isAdmin)
            && (!item.requiresProjects || showProjects());
    }
```

- [ ] **Step 2: BottomNav.js**

`src/static/src/paas/layout/bottom_nav/BottomNav.js`，在 `import { Component } from "@odoo/owl";`（line 2）之後加：

```javascript
import { showProjects } from "../../core/platform_config";
```

`navItems` 的 projects 項（line 11）由：

```javascript
        { path: "projects", name: "Projects", icon: "folder" },
```

改為：

```javascript
        { path: "projects", name: "Projects", icon: "folder", requiresProjects: true },
```

在 `navItems` 陣列（line 14 `];`）之後、`isActive(item)`（line 16）之前新增 method：

```javascript
    showItem(item) {
        return !item.requiresProjects || showProjects();
    }
```

- [ ] **Step 3: BottomNav.xml**

`src/static/src/paas/layout/bottom_nav/BottomNav.xml`，`<a>` 元素（line 6）由：

```xml
                <a t-att-class="'o_woow_bottom_nav_item' + (isActive(item) ? ' active' : '')"
                   t-on-click="() => props.router.navigate(item.path)">
```

改為（加 `t-if="showItem(item)"`）：

```xml
                <a t-if="showItem(item)"
                   t-att-class="'o_woow_bottom_nav_item' + (isActive(item) ? ' active' : '')"
                   t-on-click="() => props.router.navigate(item.path)">
```

> owl 守線：`t-if` + `t-att-class` 於 plain DOM `<a>` 併存合法（無 `t-att-` on component、無 event modifier 陷阱）。

- [ ] **Step 4: assets build + repo-lint（owl-template check）**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init` 然後 `python3 scripts/audit/repo-lint.py`
Expected: 無編譯錯誤；repo-lint owl-template check 過。

- [ ] **Step 5: Commit**

```bash
git add src/static/src/paas/layout/sidebar/Sidebar.js src/static/src/paas/layout/bottom_nav/BottomNav.js src/static/src/paas/layout/bottom_nav/BottomNav.xml
git commit -m "feat: #275 Sidebar/BottomNav 依 showProjects 隱藏 Projects nav"
```

---

### Task 7: Service 詳情頁 Project 交叉入口 gating

**Files:**
- Modify: `src/static/src/paas/pages/service/ServiceDetailPage.js`（`router` import 在 line 19）
- Modify: `src/static/src/paas/pages/service/ServiceDetailPage.xml`（Support Project 按鈕組，line 54-70）
- Modify: `src/static/src/paas/pages/service/tabs/McpServersTab.js`（`router` import 在 line 7）
- Modify: `src/static/src/paas/pages/service/tabs/McpServersTab.xml`（Go to Project Settings 按鈕，line 18-22）

**Interfaces:**
- Consumes：Task 4 `showProjects()`。

**Why:** service/mcp 路由不在 `PROJECT_ROUTE_NAMES`，flag OFF 時 service 頁仍可達。其 project 交叉入口若不 gate：「Create Support Project」會在 prod **建出隱藏 project 資料**（超出 cosmetic、違背「Project 不上線」）；「Support Project」/「Go to Project Settings」點了 dead-end 回 dashboard。router gate 只接住導航、擋不掉「建資料」與「按鈕可見」。（其餘 `navigate("projects"...)` 呼叫點都在 project 頁**內部**，flag OFF 時那些頁已被 router 擋掉、到不了，無需 gate。）

> 仍屬 cosmetic 前端（只隱藏按鈕；後端 create endpoint 不動——flag OFF 時無 UI 路徑觸發）。行為驗證併入 Task 8 headed E2E。

- [ ] **Step 1: ServiceDetailPage.js — import + getter**

在 `import { router } from "../../core/router";`（line 19）之後加：

```javascript
import { showProjects } from "../../core/platform_config";
```

在 `openSupportProject()`（line 181）之前插入 getter：

```javascript
    get projectsVisible() {
        return showProjects();  // #275：flag OFF 時隱藏 project 交叉入口
    }
```

- [ ] **Step 2: ServiceDetailPage.xml — 外包 `<t t-if="projectsVisible">`**

把現有兩顆按鈕（line 54-70）：

```xml
                        <!-- Support Project Button -->
                        <WoowButton
                            t-if="state.supportProject"
                            variant="'secondary'"
                            size="'md'"
                            onClick.bind="openSupportProject">
                            <WoowIcon name="'support_agent'" size="18"/>
                            Support Project
                        </WoowButton>
                        <WoowButton
                            t-elif="!state.supportProjectLoading"
                            variant="'secondary'"
                            size="'md'"
                            onClick.bind="showCreateProject">
                            <WoowIcon name="'add_circle'" size="18"/>
                            Create Support Project
                        </WoowButton>
```

改為（外包一層 `<t t-if="projectsVisible">`，內部 t-if/t-elif 鏈不變、兩顆按鈕維持相鄰無註解夾中間）：

```xml
                        <!-- Support Project 按鈕組 — #275：flag OFF 整組隱藏
                             （避免 dead-end 回 dashboard + prod 建出隱藏 project） -->
                        <t t-if="projectsVisible">
                            <WoowButton
                                t-if="state.supportProject"
                                variant="'secondary'"
                                size="'md'"
                                onClick.bind="openSupportProject">
                                <WoowIcon name="'support_agent'" size="18"/>
                                Support Project
                            </WoowButton>
                            <WoowButton
                                t-elif="!state.supportProjectLoading"
                                variant="'secondary'"
                                size="'md'"
                                onClick.bind="showCreateProject">
                                <WoowIcon name="'add_circle'" size="18"/>
                                Create Support Project
                            </WoowButton>
                        </t>
```

- [ ] **Step 3: McpServersTab.js — import + getter**

在 `import { router } from "../../../core/router";`（line 7）之後加：

```javascript
import { showProjects } from "../../../core/platform_config";
```

在 `goToProjectSettings()`（line 40）之前插入 getter：

```javascript
    get projectsVisible() {
        return showProjects();  // #275
    }
```

- [ ] **Step 4: McpServersTab.xml — 外包 `<t t-if="projectsVisible">`**

把（line 18-22）：

```xml
                        <WoowButton variant="'primary'" size="'md'"
                                    onClick.bind="goToProjectSettings">
                            <WoowIcon name="'arrow_forward'" size="18"/>
                            Go to Project Settings
                        </WoowButton>
```

改為：

```xml
                        <!-- #275：flag OFF 隱藏（MCP-via-project 入口一併不可達，intended） -->
                        <t t-if="projectsVisible">
                            <WoowButton variant="'primary'" size="'md'"
                                        onClick.bind="goToProjectSettings">
                                <WoowIcon name="'arrow_forward'" size="18"/>
                                Go to Project Settings
                            </WoowButton>
                        </t>
```

- [ ] **Step 5: assets build + repo-lint owl check**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init` 然後 `python3 scripts/audit/repo-lint.py`
Expected: 無編譯錯誤；owl-template check 過。

- [ ] **Step 6: Commit**

```bash
git add src/static/src/paas/pages/service/ServiceDetailPage.js src/static/src/paas/pages/service/ServiceDetailPage.xml src/static/src/paas/pages/service/tabs/McpServersTab.js src/static/src/paas/pages/service/tabs/McpServersTab.xml
git commit -m "feat: #275 Service 詳情頁 Project 交叉入口一併 gate（防 flag OFF 建隱藏 project）"
```

---

### Task 8: headed E2E 驗收（flag ON/OFF + deep-link redirect）

**Files:** 無 code commit（repo 前端驗證慣例＝ad-hoc headed playwright，非 committed 測試）。產物＝截圖 + DOM/console 證據。

**前置：** 把本 branch 部署到可訪問的 /woow（`k8s-dev-sandbox` 或 `deploy-stg`）。切 flag 用 odoo shell：`env['ir.config_parameter'].sudo().set_param('woow_paas_platform.show_projects', 'True'|'False')` 後 `env.cr.commit()`。

> 依 `woow_playwright_e2e_gotchas`：OWL click 逾時改 `eval el.click()`；深 hash 路由用 in-app 點擊或直接改 `window.location.hash`；用 curl 取 session 注入瀏覽器免打密碼。BottomNav 需把視窗 resize 到手機寬（如 390px）。

- [ ] **Step 1: flag ON（stg 樣態）— Projects 應可見**

set flag=True → 開 `/woow`：
- 桌面側欄有「Projects」項（`.o_woow_nav_item` 含 Projects）。
- resize 390px：底部導航有「Projects」。
- 導到 `#/projects`：正常渲染 project 列表頁（非 dashboard）。
- 開 cloud service 詳情頁：header 有「Support Project」或「Create Support Project」；MCP Servers tab（若有綁 project）有「Go to Project Settings」。
- console 無 `OwlError`。

- [ ] **Step 2: flag OFF（prod 預設）— Projects 應隱藏 + redirect**

set flag=False → reload `/woow`：
- 桌面側欄**無** Projects 項。
- resize 390px：底部導航**無** Projects。
- 直接 `window.location.hash = "#/projects"` → 被 redirect 到 `#/dashboard`，畫面顯 dashboard。
- **deep-link bookmark** 逐一驗被 redirect 到 dashboard：`#/projects/123`、`#/ai-assistant/projects`、`#/ai-assistant/projects/1`、`#/ai-assistant/tasks`、`#/ai-assistant/tasks/1`、`#/ai-assistant/chat/1`。
- **Service 頁交叉入口**：開 cloud service 詳情頁 → header **無**「Support Project」/「Create Support Project」；「MCP Servers」tab **無**「Go to Project Settings」（建 project 的 UI 路徑不存在 → 不會在 prod 建隱藏資料）。
- console 無 `OwlError`。

- [ ] **Step 3: 記錄證據**

截圖（ON 側欄 / OFF 側欄 / OFF deep-link→dashboard）+ console log 存查；於 PR 描述附上。

---

## Self-Review

**1. Spec coverage**（對照 design doc 各節）：
- D1 獨立 fail-closed flag → Task 1（helper）+ Task 3（欄位/hooks）。✓
- D2 cosmetic-only → 全計畫不碰後端 API；Task 5/6 只前端。✓
- D3 session_info 同步注入 → Task 2。✓
- §3 邊界 → helper docstring（Task 1 Step 3）+ 欄位 help（Task 3）。✓
- §4.2 reader/router/Sidebar/BottomNav → Task 4/5/6。✓
- §5 測試（matrix / session_info / lockstep）→ Task 1/2/3；deep-link E2E → Task 8。✓
- §6 部署（stg 設 flag）→ Task 8 前置 + Global Constraints。✓
- §7 不做 + Project 交叉入口 → Global Constraints + Task 7（ServiceDetailPage/McpServersTab gate；design §7 已修正：非「Dashboard only no-op」）。✓
- 補漏：settings view（design 未列）→ Task 3c。✓；manifest bump → Task 3d。✓；Service 交叉入口（對抗覆核發現）→ Task 7。✓

**2. Placeholder scan：** 無 TBD/TODO；每個 code step 有完整 code。✓

**3. Type/name consistency：**
- `SHOW_PROJECTS_FLAG` / `_show_projects` — Task 1 定義、Task 2 使用，一致。✓
- session_info key `woow_show_projects` — Task 2 注入、Task 4 reader 讀、Task 2 測試斷言，一致。✓
- `showProjects()` — Task 4 export、Task 5/6 import，一致。✓
- `PROJECT_ROUTE_NAMES` 6 值與 router.js 實際 routeName（`ai-projects`/`project-detail`/`ai-project-kanban`/`ai-tasks`/`ai-task-detail`/`ai-chat`）一致。✓
- `requiresProjects` 標記 — Sidebar/BottomNav navItems 與各自 `showItem` 一致。✓
- config key `woow_paas_platform.show_projects` — helper / 欄位 config_parameter / hooks list / lockstep 測試，四處一致。✓
- `projectsVisible` getter（Task 7）— ServiceDetailPage / McpServersTab 各自 class getter，模板 `t-if="projectsVisible"` 引用一致。✓
