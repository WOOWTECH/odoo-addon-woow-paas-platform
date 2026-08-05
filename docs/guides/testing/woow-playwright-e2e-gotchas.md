# reference woow playwright e2e gotchas

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_woow_playwright_e2e_gotchas`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：playwright 測 /woow OWL SPA 的可重用陷阱（click 逾時、hash 路由、goto 不重載、登入頁無 db 欄位、NODE_PATH）

用 playwright 對 `/woow` OWL SPA 跑 E2E 時實證的陷阱（2026-06-13 SA policy E2E ＋ 2026-07-29 #464 Reset 按鈕 E2E）：

**1. `playwright-cli click e<ref>` 的 actionability auto-wait 在 /woow 上必逾時（`TimeoutError: Timeout 5000ms exceeded`）→ click 根本沒派發。** 根因：playwright click 後等頁面「stable / networkidle」，但 /woow 有 `bus/websocket` + `/mail/data` long-poll 永遠不 idle → 5s 逾時且**沒按到**（modal 不動、無 error、無對應 API 請求）。**解：`playwright-cli eval "el => el.click()" e<ref>` 直接派 DOM click**。導航類 click 反而正常，會卡的是「觸發 async fetch 但不換頁」的按鈕。`fill`/`select` 不受影響。同理 `page.goto` 用 `waitUntil: 'domcontentloaded'`，別用預設 `load`/`networkidle`。

**2. ⚠️ 勘誤（2026-07-29 #464 實測）：深層 hash 路由「可以」直接 goto。** 舊記載說「直接 goto `#workspace/2/...` 會 404、必須 in-app 點擊」——真因是 **hash 少寫了 `/`**：`core/router.js` 的 `parseRoute()` 做 `window.location.hash.slice(2)`，所以必須是 `#/workspace/1/services/5`（`#workspace/...` 會被切成 `orkspace/...` → not-found）。`router.init()` 在載入時就 `parseRoute()`，帶正確 hash 直接 goto 完全可行。

**3. 同一 page 內換「同路由不同 id」的兩個坑疊在一起**（#464 卡最久的一題，症狀是 `waitForResponse` 永遠不回、或讀到上一筆的資料）：
   - service-detail → service-detail 的 **hashchange 只換 props**，OWL 沿用同一個元件實例、`onMounted` 不重跑 → 不發新的 API 請求。
   - 兩次的目標 URL **只差 hash**，`page.goto` 在這種情況走的是 hash navigation、**不會重新載入頁面** → 同樣不 remount。
   **解：每個 case 之間先 `page.goto('about:blank')`**，下一次 goto 才是真正的整頁導航。

**4. 單 DB 部署的 `/web/login` 沒有 `input[name="db"]`** → `page.fill('input[name="db"]', ...)` 會用預設 **30s** actionability wait 空等，把整個 `beforeAll` hook 的預算吃光（`.catch()` 攔得到錯誤但攔不到時間）。解：`{ timeout: 2000 }` + catch，並在 hook 內 `test.setTimeout(180_000)`（冷啟第一個 /woow request 要編 assets bundle，實測 ~60s）。

**5. repo 沒有 `package.json`／`node_modules`** → 全域裝的 `@playwright/test` 不在 spec 目錄的 Node 解析路徑上，spec 的 `import` 會 `Cannot find module`。解：執行時帶 `NODE_PATH=$(npm root -g)`。

**6. 免在瀏覽器打密碼：把 curl 認證好的 session 注入瀏覽器。** 先 `curl -c jar /web/session/authenticate {db, login, password}`，取 jar 內 `session_id`，`playwright-cli open` 後 `cookie-set session_id <val> --domain <host> --path / --httpOnly` → goto /woow 即已登入。同一把 session 也能繼續用 `web/dataset/call_kw` 查 DB ground truth 對照 UI。

**7. 寫「不可見」斷言必配一個對照組**：斷言某按鈕 `toHaveCount(0)` 之前，先斷言同容器內一個必然存在的元素可見（＋`.o_woow_error_state` 為 0），否則「頁面根本沒載入」會偽裝成通過。更強的做法是加一條**紅燈對照**：用 `page.route` 攔 payload、只刪掉受測欄位，斷言 UI 隨之改變——證明 UI 真的由該欄位驅動（#464 case 5）。

SA 相關證據鏈見 [[reference_security_access_stg_e2e]]；本機跑 sandbox 見 [[reference_docker_desktop_ci_local_sandbox_image_import]]。
