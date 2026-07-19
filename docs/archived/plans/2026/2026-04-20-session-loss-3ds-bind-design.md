---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-20-session-loss-3ds-bind-design.md
archived: 2026-07-19
archived-reason: "已落地；3DS 綁卡流程現況=src/controllers/billing.py"
---

# Phase 2c #1：修復 3DS 綁卡後 session 流失

**日期**：2026-04-20
**狀態**：Design approved，待 writing-plans 產出實作計畫
**相關檔案**：`src/controllers/billing.py:253-299`
**起源**：`docs/plans/2026-04-19-e2e-test-results.md` Phase 2c Follow-ups row #1
**上游參考**：`odoo-18.0/addons/payment_mollie/controllers/main.py:17-20`、`payment_aps/controllers/main.py:21-23`

## 起源

Phase 2a E2E 測試期間，Bug #3 (`8e89f08`) 與 #4 (`99ca65e`) 解決了：

1. ECPay 對 OrderResultURL 的 cross-site POST 打到 `/woow` 被 CSRF 擋（加 bouncer）
2. S2S `bind_callback` 在 sandbox 不穩定（bouncer 改成呼叫 `_handle_bind_callback` 並改用 HTML+JS redirect）

但現場仍觀察到：**使用者完成 3DS 綁卡後被踢回 `/web/login`**，必須重新登入才能回到 SPA。

## 根因分析

### 觸發路徑

```
Browser: session_id=ABC (使用者已登入)
   │
   │ window.location.href = three_d_url
   ▼
ECPay 3DS 頁面 (ecpg.ecpay.com.tw)
   │
   │ form.submit() to OrderResultURL (cross-site POST)
   ▼
Browser 送 request 時：SameSite=Lax 擋掉 session_id cookie
   │ (Cookie header 為空)
   ▼
Odoo bouncer (/api/billing/payment-methods/bind-result/<trade_no>)
   │ auth='public' → 建新匿名 session sid=XYZ
   │ _handle_bind_callback 持久化 token ✅
   │ 回 HTML+JS redirect 頁面
   │
   │ [BUG POINT] Odoo `post_dispatch → _save_session()`:
   │   cookie_sid = None (因 SameSite 擋)
   │   sess.sid = XYZ (新匿名 session)
   │   None != XYZ → 加 Set-Cookie: session_id=XYZ
   ▼
Browser 收到 response：session_id 被蓋成 XYZ（覆蓋原本的 ABC）
   │
   │ JS fires window.location.replace('/woow#...')
   ▼
Browser: same-origin GET /woow, 送 session_id=XYZ (anon)
   │
   ▼
/woow auth='user' → 無使用者 → 302 /web/login
```

### 關鍵 Odoo 原始碼

**`odoo-18.0/odoo/http.py:1979-1993`**：
```python
def _save_session(self):
    sess = self.session
    if not sess.can_save:        # ← save_session=False 從這裡早退
        return
    if sess.should_rotate:
        root.session_store.rotate(sess, self.env)
    elif sess.is_dirty:
        root.session_store.save(sess)
    cookie_sid = self.cookies.get('session_id')
    if sess.is_dirty or cookie_sid != sess.sid:
        self.future_response.set_cookie(
            'session_id', sess.sid, max_age=..., httponly=True,
        )
```

**`odoo-18.0/odoo/http.py:2191`**：
```python
self.request.session.can_save = routing.get('save_session', True)
```

即：`save_session=False` 會讓 `can_save=False`，`_save_session` 早退，response 不加 `Set-Cookie: session_id`。

### Odoo 慣例驗證

同樣的 cross-site redirect 情境，Odoo 官方 payment addon 的處理模式一致：

| Addon | 檔案 | 參數 |
|-------|------|------|
| payment_mollie | `controllers/main.py:17-20` | `save_session=False` |
| payment_aps | `controllers/main.py:21-23` | `save_session=False` |
| base_automation | `controllers/main.py:6` | `save_session=False` |

`payment_mollie` docstring 寫的原因與我們遇到的一字不差：「prevent Odoo from assigning a new session to the user if they are redirected to this route with a POST request. Indeed, as the session cookie is created without a `SameSite` attribute…」

## 解法選擇

### Approach A（採用）：`save_session=False`

在 bouncer route decorator 加 `save_session=False`。Odoo `post_dispatch` 不再對此 response 附 `Set-Cookie: session_id`，browser 保留原本的 auth session。

**Pros**
- 對齊 Odoo upstream payment addon 慣用法（mollie/aps 已在 production 多年）
- 零前端改動；bouncer 其餘邏輯（`_handle_bind_callback`、HTML+JS redirect）不動
- 一行 diff，易 review、易 revert
- 不依賴 user 身份 — `_handle_bind_callback` 用 `.sudo()`

**Cons**
- 仍依賴「cross-site POST → same-origin JS redirect」鏈條（但已是 Phase 2a bug fix 的既有設計，本次不改）

**Risk**：低。`save_session=False` 只影響 response 寫回 cookie，不影響 in-request `request.env` 運作。

### Approach B（否決）：Popup window 流程

前端改用 `window.open` 做 3DS，`postMessage` 回主視窗後關 popup。

**為何否決**
- 前後端大改，風險與測試成本遠高於 A
- iOS Safari popup / window.close 跨來源限制需要大量相容性測試
- 未實測 ECPay 3DS 頁在 popup 內行為
- 違反 YAGNI —「使用者不要被踢回登入」是 session bug，非 UX bug
- 若未來要做 popup UX 改造，開獨立 PR，不綁在 session fix 裡

## 設計細節

### 修改點

**`src/controllers/billing.py:253-259`**：

```python
@route(
    "/api/billing/payment-methods/bind-result/<string:trade_no>",
    auth="public",
    type="http",
    methods=["GET", "POST"],
    csrf=False,
    save_session=False,  # ← 新增
)
def api_payment_method_bind_result(self, trade_no, **kw):
    ...
```

### Docstring 更新

在既有 docstring 第 2 段（line 272-278）末尾加一段，說明 `save_session=False` 的用意與 upstream 依據：

> 3) 即使 bouncer 已用 JS redirect（見 point 2），Odoo 仍會在 response 附
>    Set-Cookie: session_id 給我們剛建立的匿名 session，進而蓋掉 browser
>    原本的使用者 session_id。`save_session=False` 指示 Odoo 不要對這個
>    response 寫回 session cookie。`payment_mollie`、`payment_aps` 等 Odoo
>    官方 payment 模組對 cross-site POST return 的處理方式一致。

### 資料流（修改後）

```
使用者 onConfirm → create-bind → window.location.href = three_d_url   (unchanged)
ECPay 3DS → cross-site POST → bouncer                                   (unchanged)

bouncer:
  _handle_bind_callback(kw) 持久化 token                                (unchanged)
  回 HTML+JS redirect                                                  (unchanged)
  [DIFF] session.can_save=False → _save_session 早退 → 無 Set-Cookie

Browser:
  保留原本 session_id=ABC（不被蓋）
  JS window.location.replace(spa_url) → same-origin GET 帶 ABC         (unchanged)
  /woow 認到使用者 → SPA 正常載入
```

### 錯誤處理

- `_handle_bind_callback` 失敗：既有 `try/except` + `_logger.exception` 不動
- 沒有 session cookie 進來的情境本來就由 `auth='public'` 處理，`save_session=False` 只影響寫回

## 測試策略

### 手動 E2E（必做 / staging）

1. Fresh login as admin user（確認 DevTools Application → Cookies 有 `session_id`，記下值）
2. 進 `#/settings/payment-methods` → Add payment method
3. 卡片表單填 sandbox 測試卡 `4311-9522-2222-2222 / 12/28 / 222`
4. 3DS OTP 輸入 `1234`
5. **預期**：
   - 直接回到 `#/settings/payment-methods?bind_status=ok&trade_no=BIND...`
   - **不**出現 `/web/login`
   - DevTools Application → Cookies 中 `session_id` 值**未改變**（仍是步驟 1 記下的值）
6. DevTools Network tab 檢查 bouncer request 的 response headers：**無** `Set-Cookie: session_id=...`
7. 卡片出現在列表，功能回歸：
   - Delete 卡片 ✅
   - Auto-topup 流程（`TC-2b-1` 再跑一次）✅

### 回歸測試

跑 `./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init`
- 既有 3 個 `TestPaymentMethodsApi` fails 為 pre-existing（Phase 2c #2 另案追）
- 本修改不應新增失敗

### 程式碼 review checkpoint

- docstring 明確指向 `payment_mollie`/`payment_aps` 作為 upstream 依據
- decorator 參數順序維持既有風格（auth / type / methods / csrf / save_session）

## 驗收條件

| 項目 | 驗收 |
|------|------|
| E2E bind flow | 使用者 3DS 完成後直接回 SPA，無需重新登入 |
| session_id cookie | bouncer response 不含 `Set-Cookie: session_id`；browser 端值不變 |
| Token 持久化 | `payment.token` 仍被 `_handle_bind_callback` 正常建立 |
| 既有測試 | `woow_paas_platform` suite 通過率不變（3 個 pre-existing fails 不列入 regression） |

## 不在此範圍（明確排除）

- Phase 2c #2：3 個 `TestPaymentMethodsApi` HttpCase fails（另案）
- Popup window UX 改造（若未來需要，開獨立設計 + PR）
- `/payment/ecpg/bind_callback` S2S endpoint（upstream addon 的，非本模組；且 S2S 本就無使用者 session，無此問題）
- 其他 ECPG endpoint session 稽核（目前僅 bouncer 是 cross-site POST entry，其他都是 `type='json'` + `auth='user'`）

## 後續 follow-ups（不阻擋此 fix）

- 若本 fix 在 staging 穩定後，可考慮提 upstream `payment_ecpay_ecpg` 的 `bind_confirm` endpoint 加上相同 `save_session=False`（目前 upstream 走 S2S + browser 分離的模式，browser 端是使用者自有 session，風險較低；但仍值得審視）
