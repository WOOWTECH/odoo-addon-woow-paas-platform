# Pitfall: Floor 內 `.env` 相對路徑 → sandbox ext addons 掛到空目錄 → module 靜默裝不起來

> 來源：2026-06-05 portal-task-agent-chat E2E 前置。在 maestri Floor（`woow-addons/.maestri/floors/<floor>/`）跑 `create.sh --context docker-desktop` 建 dev sandbox，建立流程「成功」結束，但 `woow_paas_platform` 其實沒裝起來（`ir_module_module` 連 `ai_base_gt` 都沒有 row）。同一台機器上另一個 Floor 的 sandbox 卻正常。

## Symptom

- `create.sh` 結尾印出 `Sandbox created`，但建立 log 中段夾著一行容易滑過的
  `odoo.exceptions.UserError: 你嘗試安裝的模組 woow_paas_platform 需要依賴模組 ai_base_gt` + `Database initialization may have issues`（**warning 級，不中斷**）。
- DB 內 `ir_module_module`：`woow_paas_platform` / `project` / `portal` 全部 `uninstalled`；`ai_base_gt` **連 row 都沒有**（addons path 根本看不到它）。
- pod 內 `/mnt/extra-addons-ext/` 是空的；host 側被掛載的目錄也存在但是空的。
- 之後跑 `woow-test.sh` 會被 0-test false-green guard 擋下（見 [[sandbox-module-test-false-green]]），但 create 階段沒有等價的硬擋。

## Why it happens

`create.sh` 從 `.env` 讀 `PAAS_CI_ADDONS_PATH`（第三方 addons 來源，mount 進 `/mnt/extra-addons-ext`），**相對路徑以 PROJECT_ROOT（= 當前 repo 目錄）解析**：

```
.env（為主 repo 位置寫的）：PAAS_CI_ADDONS_PATH=../../../woow-paas-repo/paas-odoo-ci/addons

主 repo：  woow-addons/woow_paas_platform/            + ../../../ → /Users/eugene/Documents/woow/ ✅
Floor：    woow-addons/.maestri/floors/<floor>/        + ../../../ → .../woow-addons/ ❌（深三層）
```

兩個殺傷力疊加：

1. **Floor 比主 repo 深三層**，同一條相對路徑解析到完全不同的位置。
2. 錯誤位置 `woow-addons/woow-paas-repo/paas-odoo-ci/addons` **恰好存在但是空的**（歷史殘留目錄）→ create.sh 的「目錄存在性檢查」通過，不會觸發它本來就有的紅字警告，直接掛一個空目錄進去。

Odoo 起來後 addons_path 缺 `ai_base_gt` 等依賴 → `-i woow_paas_platform` 失敗 → script 把它當 warning 繼續走完 → 「看起來建好了」的壞 sandbox。

## 修法 / 防範

- **Floor 內的 `.env` 一律用絕對路徑**：
  `PAAS_CI_ADDONS_PATH=/Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci/addons`
  （本 Floor 已修並加註解；新開 Floor 時記得帶上。）
- **建完 sandbox 先驗 module 真的裝了**再開工（一行）：
  ```bash
  kubectl --context <ctx> exec -n <ns> <postgres-pod> -- \
    psql -U odoo -d <db> -t -c "select state from ir_module_module where name='woow_paas_platform';"
  # 期望 installed；空輸出 = addons path 壞掉
  ```
- 結構性收口（未排程）：create.sh 對「ext addons 目錄存在但為空」應視同不存在（紅字 + 中止），而不是只擋「目錄不存在」；與 init gate（#85A 查 `state='installed'` 自癒）同方向。

## 關聯

- [[sandbox-module-test-false-green]] — 同家族 test 階段守線（0 tests = fail）；本篇是 create 階段的上游變體。
- `docs/tracking/followups/follow-ups.md` §F-GC-2 — create.sh fresh `--set` 同時會洗掉 agentRuntime secret keys（本次 E2E 同場撞到的另一獨立 gap）。
