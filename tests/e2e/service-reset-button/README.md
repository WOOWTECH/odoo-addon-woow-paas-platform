# Reset Admin Password 按鈕可見性 — Browser E2E

Issue: [#464](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/464)（#460 S3）
Spec: [#460](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/460) 的 spec comment「Testing Decisions」段
背景: [`docs/tracking/test-reports/449-password-carrier-stg-e2e-2026-07-29.md`](../../../docs/tracking/test-reports/449-password-carrier-stg-e2e-2026-07-29.md) §8.2
實測報告: [`docs/tracking/test-reports/464-reset-button-visibility-e2e-2026-07-29.md`](../../../docs/tracking/test-reports/464-reset-button-visibility-e2e-2026-07-29.md)

四條斷言（四個代表模板）：

| slug | has_admin_gui | 期望 | 代表的族群 |
| --- | --- | --- | --- |
| `hermes-agent` | True | 按鈕**可見** | #449／PR #455 新修族 |
| `mcp-k8s` | True | 按鈕**可見** | #375 三型 MCP 遺漏族 |
| `mcp-odoo` | True | 按鈕**可見** | 原 slug 白名單族（不回歸） |
| `n8n` | False | 按鈕**不出現** | 不支援重設的模板（不誤現） |

外加一道**紅燈對照**：攔截 hermes-agent 的 payload、只刪掉 `template.has_admin_gui`
這個鍵，斷言同一頁的按鈕隨即消失 —— 證明按鈕確實由該欄位驅動（而非無條件渲染），
同時實證 spec 要求的 fail-closed（欄位缺失＝視同 False）。

## 為什麼不需要真部署

按鈕的判準是 service payload 的 `template.has_admin_gui`（#460 S1），**純前端渲染**。
fixture 只要 DB 裡有 `state=running` 且掛對 template 的 `CloudService` 列即可 ——
毋須 operator、毋須真 Helm release、毋須租戶 namespace。

## 前置

1. **sandbox 跑著，且跑的是含 #460 S1 的程式碼**（`d368cb64` 之後的 develop）。

   > ⚠️ 本機 `docker-desktop` 的 **local 模式（hostPath）已不可用**：Docker Desktop
   > 的 k8s node 是 kind 風格容器 `desktop-control-plane`，裡面**沒有 macOS `/Users`**，
   > hostPath 指不到 worktree。改走 **ci-local**：自建一顆把 addon 與第三方相依
   > COPY 進 `/mnt/extra-addons` 的 image，再用 `--image` 建 sandbox。做法見實測報告 §1。

   ```bash
   /woow-dev:k8s-dev-sandbox create --context docker-desktop \
       --mode ci-local --image <本機 image tag> --branch <slug>
   ```

2. **seed fixture**（冪等，可重跑）：

   ```bash
   bash .claude/skills/k8s-dev-sandbox/scripts/e2e-seed-reset-button.sh <slug> --context docker-desktop
   ```

   會建 1 個 workspace（`reset-button-e2e-ws`，owner = admin）＋ 4 個 `state=running`
   的 CloudService，並把 fixture manifest 寫到 `/tmp/reset-button-e2e-fixture.json`
   （spec 讀這個檔拿 workspace / service id）。清除用 `--purge`。

   > `hermes-agent` 的 seed 值是 `is_hidden=True`（#424 待定價）。`is_hidden` 是
   > **定價／上架閘門**（呈現層過濾），與 `has_admin_gui` 正交，但服務詳情端點對
   > hidden 模板一律回 `Service not found` → 詳情頁開不了。seed 因此把它翻成 False，
   > 讓「按鈕可見性」這個受測面向能被單獨觀測（stg 於 2026-07-28 做過等價的 DB 直改）。

3. **取得 sandbox 對外 URL**：

   ```bash
   /woow-dev:k8s-dev-sandbox status <slug>
   # 或直接 port-forward（ci-local 無 ingress 時最省事）
   kubectl --context docker-desktop -n dev-sandbox-<slug> \
       port-forward svc/<slug>-odoo-dev-sandbox-odoo 8069:8069
   export SANDBOX_BASE_URL=http://localhost:8069
   ```

## 執行

本 repo 沒有 `package.json` / `node_modules`，runner 走全域安裝，並用 `NODE_PATH` 讓
spec 的 `import ... from '@playwright/test'` 解析得到（否則 `Cannot find module`）：

```bash
npm i -g @playwright/test && playwright install chromium   # 一次性

cd tests/e2e/service-reset-button
NODE_PATH=$(npm root -g) PLAYWRIGHT_HTML_OPEN=never \
  SANDBOX_BASE_URL=http://localhost:8080 DB_NAME=woow_<slug> \
  playwright test reset-button-visibility.spec.ts --headed --reporter=list
```

環境變數：

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `SANDBOX_BASE_URL` | `http://localhost:8069` | sandbox 對外 URL |
| `ADMIN_LOGIN` / `ADMIN_PASSWORD` | `admin` / `admin` | 登入帳密 |
| `DB_NAME` | （空） | 多 DB 部署時才需要填 login 頁的 db 欄位 |
| `RESET_BTN_E2E_FIXTURE` | `/tmp/reset-button-e2e-fixture.json` | seed 產的 manifest 路徑（**與 seed 腳本 `--manifest` 對齊**） |
| `RESET_BTN_E2E_SHOTS` | `/tmp/reset-button-e2e-screenshots` | 截圖落地目錄 |

## 截圖落地

每個 case 一張：`<slug>-visible.png` / `<slug>-absent.png`。

## 設計備註

- **期望值寫死在 spec**（不從 fixture manifest 讀）：這樣「有人把 seed 的
  `has_admin_gui` 改壞」會被這支測試抓到，而不是跟著一起漂。
- **對照按鈕**（`變更方案`，一定會渲染）在每個 case 都斷言可見：沒有這道，
  「按鈕不可見」的斷言會被「頁面根本沒載入」假裝成通過。
- **payload 交叉斷言**：同時攔 service payload 驗 `template.has_admin_gui` 存在、
  型別為 boolean、與 DB ground truth 一致 —— 把 S1 的後端補欄位與前端讀欄位
  兩段修復釘在同一條真瀏覽器路徑上。
- **hash 深路由可直接 goto**：`router.init()` 載入時就 `parseRoute()`。歷史上的
  「深路由 404」是 hash 少寫了 `/`（`parseRoute` 做 `hash.slice(2)`，必須是
  `#/workspace/...`）。同一個 page 內換服務則改寫 `location.hash` 觸發 hashchange。
