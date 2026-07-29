# #464 Reset 密碼按鈕可見性 — 四代表模板 headed E2E

> issue **#464**（#460 S3）｜spec：#460 的 spec comment「Testing Decisions」段｜受測修復：#460 S1（PR **#465**，develop `d368cb64`）
> 環境：`docker-desktop`／本機 ci-local sandbox `dev-sandbox-e2e464`（DB `woow_e2e464`）｜Odoo 18.0-20260723｜Playwright 1.62.0 Chromium `--headed`
> 執行：2026-07-29（本機時間 23:22–23:32）
> 狀態：**完成 — 四條驗收條件全 PASS，另加一道紅燈對照亦 PASS；帶四項環境／設計面發現**
>
> **一句話結論**：#460 S1 的雙邊修復在真瀏覽器上成立 —— `has_admin_gui=True` 的三個代表模板（hermes-agent／mcp-k8s／mcp-odoo）都渲染出 Reset 按鈕且可按，`has_admin_gui=False` 的 n8n 不渲染；把 payload 的 `has_admin_gui` 抽掉後同一頁的按鈕隨即消失，證明按鈕**確實由該欄位驅動**而非碰巧存在。

## 為什麼需要這份報告

#460 S1 已有 Python payload 合約測試（`src/tests/test_service_payload_has_admin_gui_462.py`）。但那層只證「後端把欄位放進 payload」；**「租戶真的按得到」是渲染層的事實**，只有真瀏覽器測得出來。#449 的教訓正是如此：後端 reset 一路修到 stg 綠燈，租戶卻因為前端 gate 不同步而按不到（[`449-password-carrier-stg-e2e-2026-07-29.md`](449-password-carrier-stg-e2e-2026-07-29.md) §8.2）。本輪補上那一層證據。

---

## 0. 受測標的與環境

| 項目 | 值 |
| --- | --- |
| 平台程式碼 | `origin/develop` `d368cb64`（含 #460 S1 merge PR #465） |
| 測試分支 | `test/464-reset-button-e2e` |
| Sandbox | `dev-sandbox-e2e464`，mode `ci-local`，TTL 24h |
| 映像 | `woow-e2e-464:local`（本機建，`FROM odoo:18.0` linux/arm64 原生 + addon + 8 個第三方相依） |
| 前台入口 | `http://localhost:8080/woow`（port-forward `svc/e2e464-odoo-dev-sandbox-nginx 8080:80`） |
| 登入 | `admin / admin`（workspace owner） |

**stg 為何不可用**：stg 平台 image 尚未含 S1（S1 於 2026-07-29 才 merge 進 develop），在 stg 上按鈕本來就不會出現 —— 在那裡跑會得到一個**看起來像失敗、其實是版本落後**的結果。故本輪在本機 sandbox 跑最新 develop。

### 為什麼不需要真部署

按鈕的判準是 service payload 的 `template.has_admin_gui`，**純前端渲染**。fixture 只要 DB 裡有 `state=running` 且掛對 template 的 `CloudService` 列即可 —— 毋須 operator、毋須真 Helm release、毋須租戶 namespace。這也是本輪能在本機 20 分鐘內跑完的原因。

---

## 1. 環境建置的兩個卡點（本機 sandbox 現況）

### 1.1 `docker-desktop` 的 local 模式（hostPath）已不可用

Docker Desktop 的 k8s node 現在是 **kind 風格容器** `desktop-control-plane`，實查其 mounts：

```
/etc/kind/hosts.toml -> /etc/containerd/certs.d/_default/hosts.toml
/lib/modules         -> /lib/modules
/var/lib/docker/volumes/48faca29.../\_data -> /var
```

```
$ docker exec desktop-control-plane ls /Users
ls: cannot access '/Users': No such file or directory
```

**沒有 macOS `/Users`** → local 模式的 hostPath 指不到 worktree。改走 **ci-local**：自建一顆把 addon 與相依 COPY 進 `/mnt/extra-addons` 的 image（chart 在 ci-local 下 `addons_path = /mnt/extra-addons`），用 `--image` 建 sandbox。

> 附帶發現：plugin `build.sh` 走的是本 repo 的 `Dockerfile.dev`，而該檔 `COPY extra/extra-addons/`，**這個目錄在本 repo 不存在**（`extra/` 下只有 `cloudflare/` 與 `paas-operator/`），且 base `odoo:18.0` 不含 `ai_base_gt` 等 6 個第三方相依。所以 `build.sh` 產的 image 對本 repo 直接可用性存疑，本輪自備 build context。

### 1.2 本機建的 image 要手動匯進 node 的 containerd

kind node 的 containerd 與 Docker daemon 的 image store 是**兩套**，`docker build` 出來的 tag 對 kubelet 不可見：

```
Warning  Failed  kubelet  Failed to pull image "woow-e2e-464:local": ...
  pull access denied, repository does not exist or may require authorization
```

解法（chart 的 `imagePullPolicy` 是 `IfNotPresent`，匯入後 kubelet 下一輪 backoff 就撿到）：

```bash
docker save woow-e2e-464:local | docker exec -i desktop-control-plane ctr -n k8s.io images import -
# 驗證
docker exec desktop-control-plane ctr -n k8s.io images ls | grep woow-e2e-464
#   docker.io/library/woow-e2e-464:local  ... 2.1 GiB  linux/arm64
```

匯入後事件轉為 `Container image "woow-e2e-464:local" already present on machine and can be accessed by the pod`。

---

## 2. Fixture

`.claude/skills/k8s-dev-sandbox/scripts/e2e-seed-reset-button.sh`（本 PR 新增，冪等、可 `--purge`）建：

- workspace `reset-button-e2e-ws`（id=1，owner = admin；`create()` 自動補 owner access）
- 四個 `state=running` 的 CloudService，各掛一個代表模板

seed 輸出的 manifest（也就是 spec 讀的 DB ground truth）：

```json
{"services": {
  "hermes-agent": {"has_admin_gui": true,  "service_id": 1, "template_id": 14, "template_name": "Hermes Agent"},
  "mcp-k8s":      {"has_admin_gui": true,  "service_id": 2, "template_id": 9,  "template_name": "Kubernetes MCP"},
  "mcp-odoo":     {"has_admin_gui": true,  "service_id": 3, "template_id": 6,  "template_name": "Odoo MCP Server"},
  "n8n":          {"has_admin_gui": false, "service_id": 4, "template_id": 2,  "template_name": "n8n"}},
 "workspace_id": 1, "workspace_name": "reset-button-e2e-ws"}
```

### 2.1 fixture 必須把 hermes-agent 解封（`is_hidden`）

`cloud_app_templates.xml` 的 hermes-agent seed 是 `is_hidden=True`（#424 待定價）。而服務詳情端點對 hidden 模板一律回 `Service not found`：

```python
# src/controllers/paas.py（api_workspace_service）
if (not service.exists() or service.workspace_id.id != workspace_id
        or service.template_id.is_hidden):
    return {'success': False, 'error': 'Service not found'}
```

→ 詳情頁根本開不了，按鈕可見性無從觀測。seed 因此把它翻成 `False`（stg 於 2026-07-28 為同樣理由做過等價的 DB 直改）。

**`is_hidden` 與 `has_admin_gui` 正交**：前者是定價／上架閘門（呈現層過濾，見 `HiddenChainMixin`），後者是能力判準。本輪測的是後者；前者是產品面的開關，不在 #464 範圍。副作用要記著：**hermes-agent 在解封定價之前，Reset 按鈕對租戶仍是不可達的 —— 但那是 `is_hidden` 擋的，不是本次修復沒做到。**

---

## 3. 端點側的 ground truth（跑 UI 之前先釘）

authenticated curl 直打詳情端點，四個服務的 `template.has_admin_gui`：

```
svc1  hermes-agent   state=running  template.has_admin_gui=True (bool)
svc2  mcp-k8s        state=running  template.has_admin_gui=True (bool)
svc3  mcp-odoo       state=running  template.has_admin_gui=True (bool)
svc4  n8n            state=running  template.has_admin_gui=False (bool)
```

型別是 `bool`（不是字串、不是 0/1），值與模板欄位一致 —— S1 後端側（`_format_service` 鏡射模板欄位、不做推導）在真 runtime 上成立。

---

## 4. 驗收結果 — 四條斷言

執行：

```bash
cd tests/e2e/service-reset-button
NODE_PATH=/opt/homebrew/lib/node_modules PLAYWRIGHT_HTML_OPEN=never \
  SANDBOX_BASE_URL=http://localhost:8080 DB_NAME=woow_e2e464 \
  playwright test reset-button-visibility.spec.ts --headed --reporter=list
```

原文輸出：

```
Running 5 tests using 1 worker

  ✓  1 … › hermes-agent → Reset 按鈕可見（#449／PR #455 新修族 —— 修好的重設能力必須對租戶可達） (59.0s)
  ✓  2 … › mcp-k8s → Reset 按鈕可見（#375 三型 MCP 遺漏族 —— 後端 reset 一直是好的、按鈕自上架就漏） (1.6m)
  ✓  3 … › mcp-odoo → Reset 按鈕可見（原 slug 白名單族 —— 本次收斂不得造成回歸） (57.8s)
  ✓  4 … › n8n → Reset 按鈕不可見（has_admin_gui=False —— 不得誤現一顆按了必然失敗的按鈕） (1.1m)
  ✓  5 … › 紅燈對照：payload 抽掉 has_admin_gui → hermes-agent 按鈕消失（fail-closed） (1.1m)

  5 passed (7.1m)
```

對照 #464 的驗收條件：

| # | 驗收條件 | 結果 | 截圖 |
| --- | --- | --- | --- |
| 1 | hermes-agent（#449 新修族）按鈕可見 | ✅ PASS | `hermes-agent-visible.png` |
| 2 | mcp-k8s（#375 遺漏族）按鈕可見 | ✅ PASS | `mcp-k8s-visible.png` |
| 3 | mcp-odoo（原白名單族）按鈕仍可見 —— 不回歸 | ✅ PASS | `mcp-odoo-visible.png` |
| 4 | n8n（`has_admin_gui=False`）按鈕不出現 | ✅ PASS | `n8n-absent.png` |
| 5 | playwright headed 執行 | ✅ Chromium `--headed`，五個 case 各留一張截圖 | `control-hermes-agent-field-stripped.png` |

截圖落地 `/tmp/reset-button-e2e-screenshots/`（不入 git，比照 `tests/e2e/skill` 既有慣例）。

### 4.1 每個 case 實際斷言了什麼

不只「按鈕在不在」，一次釘三件事：

1. **payload 合約**：`template` 物件有 `has_admin_gui`、型別為 `boolean`、值等於 DB ground truth，且 `template.slug` 是預期那個模板。
2. **對照組**：`.o_woow_service_header_actions` 內的「變更方案」按鈕可見 —— 沒有這道，「Reset 按鈕不可見」會被「頁面根本沒載入」假裝成通過。另斷言 `.o_woow_error_state` 數量為 0、標題文字等於服務名。
3. **受測面向**：Reset 按鈕 `toBeVisible()`＋`toBeEnabled()`（`state=running` ⇒ `canPerformActions` 為真，按鈕不只渲染出來、是真的可按）／或 `toHaveCount(0)`。

**期望值寫死在 spec、不從 fixture manifest 讀** —— 這樣「有人把 seed 的 `has_admin_gui` 改壞」會被這支測試抓到，而不是跟著一起漂。

### 4.2 紅燈對照（case 5）

四條「可見／不可見」全綠，仍不能排除「按鈕本來就無條件渲染、剛好 n8n 那頁壞掉」這種假綠。case 5 用 Playwright 攔截 hermes-agent 的詳情 payload，**只**刪掉 `template.has_admin_gui` 這個鍵、其餘原封不動：

- 同一個服務、同一頁、對照按鈕仍在（頁面確實載入）
- Reset 按鈕 `toHaveCount(0)`

兩張截圖並列即是證據：`hermes-agent-visible.png`（有 Reset Admin Password）vs `control-hermes-agent-field-stripped.png`（同頁、無該按鈕）。這同時也是 #460 spec 要求的 **fail-closed**（欄位缺失＝視同 False）在真瀏覽器上的實證。

---

## 5. 過程中踩到的三個 SPA／工具陷阱（寫進 spec 註解，避免重踩）

| 症狀 | 根因 | 處置 |
| --- | --- | --- |
| `beforeAll` hook 30s 逾時 | 單 DB 部署的登入頁**沒有** `input[name="db"]`，`page.fill` 用預設 30s actionability wait 把整個 hook 預算吃光（`.catch()` 攔得到錯誤但攔不到時間） | db 欄位改 `{ timeout: 2000 }` + catch；hook 內 `test.setTimeout(180_000)` |
| 第 2 個 case 起 `waitForResponse` 永遠不回 | 兩件事疊加：①service-detail → service-detail 的 hashchange 只換 props，OWL 沿用同一個 `ServiceDetailPage` 實例、`onMounted` 不重跑；②兩個 case 的 URL **只差 hash**，`page.goto` 在這種情況走 hash navigation、**不重新載入頁面** | 每個 case 之間先 `page.goto('about:blank')` 斷開，下一次 goto 才是真正的整頁導航 |
| `Cannot find module '@playwright/test'` | repo 無 `package.json`／`node_modules`，全域安裝的套件不在 spec 目錄的 Node 解析路徑上 | 執行時帶 `NODE_PATH=/opt/homebrew/lib/node_modules`（已寫進 README） |

> 順帶更新一條舊認知：`/woow` 的**深層 hash 路由可以直接 goto**（`router.init()` 載入時就 `parseRoute()`）。歷史上的「深路由 404」是 hash 少寫了 `/` —— `parseRoute` 做 `hash.slice(2)`，必須是 `#/workspace/...` 而非 `#workspace/...`。

---

## 6. 收尾

- `python3 scripts/audit/repo-lint.py` → 全部通過（controller-sudo / owl-template / data-noupdate / reuse-values / seed-topology / test-env-restore）
- sandbox `dev-sandbox-e2e464` 於驗收後銷毀，namespace 不存在（見 PR 描述的清理證明）

---

## 7. 結論

| # | #464 待證項 | 結論 |
| --- | --- | --- |
| 1 | hermes-agent 按鈕可見 | ✅ 可見且可按 |
| 2 | mcp-k8s 按鈕可見 | ✅ 可見且可按 |
| 3 | mcp-odoo 不回歸 | ✅ 仍可見且可按 |
| 4 | n8n 不誤現 | ✅ 不渲染，其餘 action 按鈕正常 |
| — | 按鈕確實由 `has_admin_gui` 驅動 | ✅ 抽掉欄位即消失（fail-closed） |

**#460 S1 對租戶可達性成立。** 剩餘的可達性缺口只剩產品面的 `is_hidden`（hermes-agent 待定價解封），與本次收斂無關。

### 建議後續

| 項目 | 建議 |
| --- | --- |
| `is_hidden` 與 CS 可視性（§2.1） | 現行語意是「hidden template 的既有服務連詳情頁都打不開」。對「先開通、後定價」的模板（hermes-agent）這代表租戶手上的服務會整個從前台消失。是否要把「不可訂購」與「既有實例不可見」拆成兩個開關，值得另案討論 —— 本輪只記錄，不在 #464 範圍 |
| 本機 sandbox local 模式（§1.1） | Docker Desktop 換 kind node 後 hostPath 路線已死。ci-local ＋ `ctr images import` 是可行替代，但每次改 addon 都要重 build 2GB image；若本機 e2e 會常態化，值得讓 plugin 的 `build.sh`／`create.sh` 直接支援「build 完自動 import 進 kind node」 |
| `Dockerfile.dev` 失效（§1.1 附註） | `COPY extra/extra-addons/` 指向不存在的目錄、base 也缺第三方相依。要嘛補齊、要嘛從 repo 移除以免誤導 |
