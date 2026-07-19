---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-20-settings-profile-design.md
archived: 2026-07-19
archived-reason: "已落地（Settings UI build phase）；現況=src/static/src/paas"
---

# Phase 2c #6：/settings/profile 個人資料頁面

**日期**：2026-04-20
**狀態**：Design approved，待 writing-plans 產出實作計畫
**起源**：`docs/plans/2026-04-19-e2e-test-results.md` Phase 2c Follow-ups row #6
**直接驅動**：使用者需在 SPA 內修改 `res.partner.phone`，避免 demo data 的 `+1 555-555-5555` 卡住 ECPay 綁卡流程（phone 會被 `_sanitize_phone_for_ecpay` 清洗，但使用者仍需能改）

## 目標

1. 讓登入使用者透過 SPA 修改自己的 `name`、`email`、`phone`
2. Header 顯示實體使用者資料，移除寫死的 `Sarah Wilson / PRO PLAN / SW`
3. 為後續 Settings 擴充（地址、avatar 上傳、密碼等）打地基

## 非目標（out of scope）

- Avatar 上傳（只顯示，用 Odoo 內建 URL）
- 地址欄位
- 密碼修改
- Email 驗證流程
- 語言/時區偏好
- 並發寫入衝突處理（last-write-wins）
- Audit log
- res.users 層級欄位

## 範圍選擇

其他考慮過的範圍（未採用）：

- **外科手術**：只編輯 phone — 否決，Header 寫死的問題仍在，且未來仍要做
- **完整個人資料**：B + 地址 + avatar 上傳 — 否決，avatar 上傳與地址都有 non-trivial 複雜度，YAGNI
- **頂規**：+ 密碼/email 驗證/語言 — 否決，email 驗證需獨立 mail template 與 token 流程

**選定：MVP — `name`、`email`、`phone`、avatar 顯示**

## 架構決策

### 選擇：方案 1（`/api/me` 雙端點 + Form with Save button）

| 指標 | 方案 1 採用 | 方案 2 `action` dispatch | 方案 3 ext `api_config` + modal |
|------|-------------|---------------------------|-----------------------------------|
| 端點數 | 2（`/api/me`, `/api/me/update`） | 1（`action: get\|update`） | 0（沿用 `api_config`）|
| 與現有 controller 慣例 | 對齊 `payment_methods` 系列 | 對齊 `api_workspace` | 混用 |
| Security guard 集中 | Update endpoint 一處 | 分派到 action | 散落 |
| UX 形式 | Form with Save | Inline edit per field | Modal |
| 擴充性 | 高 | 中 | 低 |
| 獨立 hash route | 對齊 doc (`#/settings/profile`) | 對齊 | ✗ |

方案 1 勝。

### 慣例選擇

- **Endpoint 命名 `/api/me`**：業界 RESTful 慣例「我」= 當前使用者；比 `/api/profile` 更語意明確，且 `me` 暗示「不能指定 partner_id 改他人」
- **UI form + single Save**：對 3 個欄位過度設計 inline edit 會增加狀態機複雜度；form 的 validation 交互一次性呈現較清晰
- **reactive service pattern**：跟 `billingService` 一致，允許 Header + ProfilePage 共享 cached state 且自動 re-render

## Components

### Backend

**新檔 `src/controllers/profile.py`**（`ProfileController`）

```
POST /api/me          → api_me()
POST /api/me/update   → api_me_update(name, email, phone)
```

- 兩端點 `auth="user"`, `type="json"`
- 共用 `_me_guard()` helper 回傳 `request.env.user.partner_id.sudo()`
- Update 端點**永不**接受 body 來的 `partner_id`；總是寫入當前 session user 的 partner

回應格式：
```python
# /api/me
{
    "success": True,
    "data": {
        "id": 3,
        "name": "Administrator",
        "email": "admin@example.com",
        "phone": "+886-912-345-678",
        "avatar_url": "/web/image/res.partner/3/image_128",
    }
}

# /api/me/update on success
{"success": True, "data": {...same shape as /api/me...}}

# /api/me/update on error
{"success": False, "error": "Name is required", "code": "name_required"}
# possible codes: name_required, invalid_email
```

**Modify `src/controllers/__init__.py`**：加 `from . import profile`

### Frontend

**新檔**
- `src/static/src/paas/services/profile_service.js`
  - `reactive({ partner: null, loading: false, error: null })`
  - `fetch()` → POST `/api/me` → cache
  - `update(patch)` → POST `/api/me/update` → refresh cache
- `src/static/src/paas/pages/settings/ProfilePage.{js,xml,scss}`
  - 組件：Avatar + 大字姓名（唯讀展示區）
  - Form：name / email / phone 三個 `textbox`
  - Save / Cancel 按鈕
  - Inline per-field validation error
  - Save 成功 toast「Profile saved」

**Modify**
- `src/static/src/paas/core/router.js`：加 `{path: "settings/profile", routeName: "settings-profile"}`
- `src/static/src/paas/root.js`：import `ProfilePage`，加到 components
- `src/static/src/paas/root.xml`：新增 `<t t-elif="router.current === 'settings-profile'"><ProfilePage/></t>`
- `src/static/src/paas/layout/header/Header.js`：
  - `onMounted` 呼叫 `profileService.fetch()`
  - 新增 getter `get partnerName()` → `profileService.partner?.name || ""`
  - 新增 getter `get initials()` → 取 name 首字母最多 2 個
  - 新增 getter `get avatarUrl()` → `profileService.partner?.avatar_url || ""`
- `src/static/src/paas/layout/header/Header.xml`：
  - `<span class="o_woow_user_name">Sarah Wilson</span>` → `<span class="o_woow_user_name" t-esc="partnerName"/>`
  - **刪除** `<span class="o_woow_user_plan">PRO PLAN</span>`
  - avatar `<div>SW</div>` → 若有 avatarUrl 則 `<img/>`，否則 `<t t-esc="initials"/>`
- `src/static/src/paas/layout/header/Header.scss`（若有）：`.o_woow_user_plan` 樣式可移除
- `src/static/src/paas/pages/settings/SettingsPage.xml`：在 Payment Methods 卡片**之前**加 Profile 卡片（icon `person`，標題 `Profile`，描述 `Edit your name, email, and phone number.`，連 `#/settings/profile`）
- `src/static/src/paas/pages/settings/SettingsPage.js`：新 handler `goToProfile()` → `navigate("settings/profile")`
- `src/__manifest__.py`：新增 frontend 檔案到 asset bundle（若有新加檔）

## Data Flow

```
App load
  ↓ Header.onMounted
  profileService.fetch()
    → POST /api/me
    → Odoo 回 {name, email, phone, avatar_url}
  ↓ Header render
  name 取代 Sarah Wilson; avatar/initials 取代 SW; 無 PRO PLAN span

使用者進 Settings 頁
  → 看到 Profile card, 點擊
  → navigate("settings/profile")
  → ProfilePage.setup()
     讀 profileService.partner → 複製到 local form state
     (若 profileService 尚未 fetch，則先 fetch)

使用者編輯 + Save
  → client validate (name non-empty, email regex)
    若失敗 → inline error, 不發 request
  → profileService.update({name, email, phone})
    → POST /api/me/update
    → 後端 validate → partner.write(values) → 回新值
    → service.partner = 新值 (reactive)
    → Header 因 reactive 觸發 re-render (name/initials 更新)
  → ProfilePage 顯示 toast「Profile saved」; 留原頁

錯誤流:
  validation fail (client/server): inline error
  network/5xx: 紅色 banner + keep form editable
```

## Security

| 風險 | 防護 |
|------|------|
| 他人改他人 partner | Endpoint 不接受 body 的 partner_id；永寫 `request.env.user.partner_id` |
| 改不該改的 field | Update 只接受 name/email/phone 3 欄 whitelist，其他 kw 丟棄 |
| 未登入存取 | `auth="user"` |
| XSS via partner.name | OWL `t-esc` 自動 escape |
| Avatar URL 注入 | 組 URL 使用 `partner.id` (integer)，無字串拼接風險 |

## Testing

### Backend HttpCase（新 `src/tests/test_profile_api.py`）

依 Phase 2c #2 fix 過的 HttpCase 慣例，setUp 使用 `self.partner = self.env.ref('base.partner_admin')` 確保 `self.env.user` 與 HTTP session user 對齊。

1. `test_me_returns_current_partner` — POST /api/me → assert data.id == partner.id、data.name == partner.name、data.avatar_url format 正確
2. `test_update_saves_fields` — POST /api/me/update with new name/email/phone → assert success=True、partner.name/email/phone 已更新
3. `test_update_rejects_empty_name` — name="" → success=False, code="name_required"
4. `test_update_rejects_whitespace_only_name` — name="   " → code="name_required"
5. `test_update_rejects_invalid_email` — email="not-an-email" → code="invalid_email"
6. `test_update_accepts_empty_email` — email="" → success=True
7. `test_update_ignores_foreign_partner_id` — 即使 body 帶 `partner_id=999`，endpoint 仍寫入 current user 的 partner（驗證 endpoint 不接此欄位）

### Frontend

無 JS unit tests（沿用本 repo 既有慣例）。Manual E2E via Playwright / browser 驗證：

1. Login → Header 顯示真實名稱（非 Sarah Wilson）
2. 點 Settings → Profile 卡出現 → 點入 → 顯示 current partner 資料
3. 改 phone → Save → toast + Header 可能同步更新（若 name 沒動則不明顯，但 service 已更新）
4. 改 name → Save → Header name 立即變
5. 輸入無效 email → inline error，不送 request
6. 清空 name → Save disabled or inline error

## 驗收條件

| 項目 | 條件 |
|------|------|
| 後端 endpoint | `/api/me` 與 `/api/me/update` 上線；HttpCase 7 個測試全 pass |
| 前端路由 | `#/settings/profile` 載入 `ProfilePage`、不誤匹配 `settings-payment-methods` |
| Header | 顯示 `request.env.user.partner_id.name`；無 `Sarah Wilson`、無 `PRO PLAN` 字樣 |
| ProfilePage | 可讀、可改、可存 name/email/phone；validation 正確 |
| Settings 首頁 | Profile 卡片出現在 Payment Methods 之前 |
| Security | body 帶他人 partner_id 不生效（測試覆蓋） |

## 不在此範圍（明確排除，repeat）

- Avatar 上傳
- 地址欄位
- 密碼修改
- Email 驗證
- 語言/時區
- 並發寫入
- Audit log
- 其他 Phase 2c items（#5 wallet auto-deduct）
