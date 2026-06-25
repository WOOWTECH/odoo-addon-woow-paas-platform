# Pattern: Addon 不 override Odoo std field 型態 — 用原生型態 + 前端 adapter 或新欄位 inherit

> 適用：addon 想對 Odoo std model 的 std field（如 `project.project.description`、`sale.order.note` 等）的呈現 / 寫入做變動，且現有 std 型態（`fields.Html` / `fields.Selection` / `fields.Char(translate=True)`）與 addon 需求不完全契合時。

## When to use

任何符合下列**任一**條件的 PR：

1. 你想在 addon 內把 std `fields.Html` override 成 `fields.Text`（或反向），只為了讓 test assertion / API response shape 更乾淨。
2. 你想在 addon 內把 std `fields.Selection` 的 selection list 完全替換（不是 inherit 加 selection_add）。
3. 你想在 endpoint / controller 層 strip / wrap std field 的 output 形式，讓「對前端看到的」與「對 std backend view 看到的」不一致。
4. 你發現 std field 型態與 addon 想要的呈現有 mismatch（如 `description` 帶 `<p>...</p>` wrapper 讓 plain-text assertion 過不去）。

**典型場景**：

- 加 inline rich-text edit UI 在 `/woow` 自家 SPA 內，但 std backend 仍用 std `fields.Html`
- API response 想回乾淨的 plain text，但 std field 是 `fields.Html`
- 多租戶 SaaS 想把 std field 收成 PaaS-specific shape

**不適用**：

- 純加新欄位（`paas_xxx`）不動 std field —— 不在本 pattern 警告範圍，符合本 pattern 第 4 條建議
- 用 inherit 加 `selection_add` / `compute` —— inheritance idiom，本 pattern 不反對
- Model 完全是 addon 自家定義（不 inherit std model）—— 無 std field 概念

## What it looks like

### 反例（避免）

```python
# src/models/project_project.py — DON'T
class ProjectProject(models.Model):
    _inherit = "project.project"

    # Override std fields.Html → fields.Text 為了 "讓 plain text 顯示乾淨"
    description = fields.Text(string="Description")
```

```python
# src/controllers/paas.py — DON'T
def _format_project(self, project):
    return {
        "description": _strip_html(project.description),  # 改變 std field 對前端的形態
    }
```

### 正例（建議路徑，按優先順序）

**選項 1：保留 std field 型態，前端 adapter**

```python
# Backend: std field 不動
class ProjectProject(models.Model):
    _inherit = "project.project"
    # description 維持 std fields.Html(sanitize=True)
```

```js
// Frontend: 既有 safeHtml idiom 處理
import { safeHtml } from "../../services/html_sanitize";

class BasicInfoSection extends Component {
    get descriptionMarkup() {
        return safeHtml(this.props.project.description);  // markup() 給 t-out
    }
}
```

**選項 2：加新欄位（與 std 並存）**

```python
class ProjectProject(models.Model):
    _inherit = "project.project"

    paas_description = fields.Text(  # 自家 type、自家 semantic
        string="PaaS Description",
        help="PaaS-specific short description (plain text, no HTML).",
    )
```

附加：通常 paas_description 是 compute / related 或獨立 field，不取代 description。

**選項 3：endpoint output 提供 raw + derived**

```python
def _format_project(self, project):
    return {
        "description": project.description,                # std HTML — 主 source of truth
        "description_text": _html_to_text(project.description),  # 衍生欄位，frontend 自選
    }
```

衍生欄位不破壞 single source of truth；name 上明示「derived」。

## Why this pattern

### 「能用原生最好，要擴充再 inherit 加新欄位」（拍板原則）

Odoo std field 是 framework 的 contract，型態變動會影響：

| 影響面 | 細節 |
|---|---|
| **Backend std view** | Odoo std form view 對 `fields.Html` 用 HTML widget；override 成 `fields.Text` 會破 widget rendering 或顯示 raw `<p>` tags |
| **既有 row 資料** | 升級時 std HTML 內容（如其他 module 寫入的 `<p>` 段落）顯示成 plain text `<p>...</p>` literal |
| **Odoo 版本升級** | Odoo 18 → 19 升級時，若 std `description` 被 upstream 改 attribute（如加 `translate=True`），addon override 要每次重 maintain |
| **跨 addon 相容** | 其他 module（如 `project_enterprise`）依賴 `description` 為 `fields.Html` 做 chatter / activity rendering，override 成 `fields.Text` 會 break |
| **Test infra** | Odoo std test fixture 寫入 `description` 預設可能含 HTML，addon override 成 Text 後 std test 可能 fail |

→ override std field 型態屬「**invasive**」修改，blast radius 大但 surface 看似只有 1 line code change。

### Considered options（D10 拍板原文）

本 pattern 是 issue #48 D10 拍板的 generalisation。原 incident（`docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` §Decisions D10）三選項 trade-off：

| Option | 為何沒選 |
|---|---|
| **A**：override `description = fields.Text`（讓 plain-text assertion 過） | 改 std 欄位型態屬 invasive 修改：(a) Odoo std project form view 仍 expect HTML widget，可能 break；(b) 既有 row 含 HTML 會直接顯示 raw tags；(c) Odoo 升級時 std 改 Html 又要 maintain override；(d) 其他 module 依賴 `description` 為 Html 也可能 break；(e) 違反「能用原生最好」原則 |
| **B**：加 `paas_description` 新欄位（與 std description 並存） | Schema +1 column；雖無破壞但 PaaS-specific 概念（為什麼這個 module 要自己一個 description？）需先決定 design；rich-text editor 跟既有 `TaskDetailPage` description idiom 一致更乾淨，無需另開 column |
| **C**：endpoint 層 strip / wrap HTML（讓 plain-text test 過但 schema 不動） | 違反 single source of truth — endpoint 與 std form view 看到的 description 形式不同；可能 break std backend view；維護成本高（每次 std 升級都要 review strip 邏輯是否還合用） |

→ 最終採用「**std HTML field 保留 + frontend `safeHtml` adapter + rich-text editor**」（選項 1）。

### Test assertion 形態 mismatch 的處理

若 implementation 階段發現 test assertion 寫 plain-text 期望（`assertEqual(desc, "Hello world")`），但 std `fields.Html` 回 `<p>Hello world</p>`：

- **不要**為了讓 assertion 過而改 schema
- 改 assertion 期望 HTML 包裝（`assertIn("Hello world", desc)` 或 `assertEqual(desc, "<p>Hello world</p>")`）
- 或在 helper 內 `_html_to_text(desc)` 後 assert

Test 形態服從 schema，不是反過來。

## See also

- D10 原始 decision（含 implementation 階段一度走 A reject 的脈絡）：`docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` §Decisions D10 + §Options table line 68-70
- 既有 idiom 參考：`src/static/src/paas/services/html_sanitize.js`（DOMPurify wrapper + OWL markup）；`src/static/src/paas/pages/task-detail/TaskDetailPage.js:41`（既有 task description read 路徑用 safeHtml）
- 對應實作落點：`src/static/src/paas/pages/project-detail/sections/BasicInfoSection.js`（issue #48 內 std HTML + rich-text editor + safeHtml seed）
- 相關 follow-up：[[follow-ups]] F28 — description rich-text editor 升級（execCommand → ProseMirror / TipTap），與本 pattern 一致（未來框架升級不需要動 schema）
- 相關 pitfall：本 pattern 反面案例若 surface，記到 `docs/tracking/pitfalls/`
