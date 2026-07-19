# Pitfall：bare global `odoo.__session_info__` 在 boot 後被 session service 刪除——runtime 直讀永遠 undefined

> **一句話**：前端 reader 直讀 bare global `odoo.__session_info__` 取後端注入的 flag，但 Odoo 18 的 session service 於 boot 時消費該物件後即 `delete odoo.__session_info__`（防洩漏）→ runtime（component render / router 判斷時）永遠讀到 `undefined`，flag 恆 falsy → **靜默失效**（不噴錯、無 console warning）；XML / Python 測試全綠，唯 headed E2E 才測得出。
>
> **來源**：`docs/plans/2026-Q3/2026-07-06-woow-project-hide-toggle-design.md` §4.2 B1 與 §10 (extracted 2026-07-19)
>
> **發現場景**：#275 `show_projects` fail-closed flag；修復 commit `6d4c1bd0`

## Symptom

- 後端確實把 flag 注入 `session_info`（`src/views/paas_app.xml:17` 內嵌 `<script>` 設 `odoo.__session_info__ = <json>`，page source 看得到值）。
- 前端 reader `odoo?.__session_info__?.woow_show_projects === true` 卻**永遠回 false**——flag ON 時 Projects 導航照樣隱藏。
- 無任何錯誤訊息：optional chaining 把 `undefined` 靜默吸收成 falsy，console 乾淨。
- **測試全綠假象**：XML 注入測試（驗 page source 有值）綠、Python controller 測試綠——它們驗的都是「注入時」的狀態，不是「render 時」的可讀性。

## Root Cause

Odoo 18 前端 bootstrap 流程中，session service 在 boot 階段讀取 `odoo.__session_info__`、把內容存進 `@web/session` 的 `session` 物件後，隨即 **`delete odoo.__session_info__`**（防資訊洩漏的刻意設計）。

時序：

```
page load → <script> 設 odoo.__session_info__（此刻 page source / 早期讀取可見）
         → OWL boot → session service 消費並 delete bare global
         → component render / router 判斷（← reader 在此刻執行，bare global 已不存在）
```

「page source 看得到」與「runtime 讀得到」是兩回事——bare global 只在 boot 前的短暫窗口存在。

## Fix

改讀 session service 的持久落點 `@web/session`：

```javascript
import { session } from "@web/session";
// session.woow_show_projects 帶正確值
```

現行 code：`src/static/src/paas/core/platform_config.js:3`（import）＋ `:13-16`（警示註解，即本 pitfall 的 in-code 指針）。

`@web/session` 已實測確在 `assets_paas` bundle 內（headed E2E 驗 moduleCount 1310 含 `@web/session`），standalone `/woow` app 可安全 import——「不保證 ESM 在 bundle」的疑慮（plan-reviewer 曾據此建議改 bare global，反被 E2E 推翻）不成立。

## Prevention

- **`/woow` 前端讀 session 注入值一律走 `import { session } from "@web/session"`**，勿直讀 bare `odoo.__session_info__`。已有現成範例可抄：`platform_config.js`。
- **前端 reader 的 runtime 可讀性只有 headed render 測得出**：XML/Python 綠只證明「注入成功」，不證明「render 時讀得到」。凡新增「後端注入 → 前端同步讀取」的 flag，驗收必含一次 headed E2E（flag ON 時 UI 真的出現）。
- Review 紅旗：前端 code 出現 `odoo.__session_info__`（或 `odoo?.__session_info__`）即應攔——目前 `src/static/src/paas/` 內僅存於警示註解，無實讀。
