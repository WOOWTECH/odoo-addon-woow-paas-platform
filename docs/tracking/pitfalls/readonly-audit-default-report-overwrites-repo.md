---
name: readonly-audit-default-report-overwrites-repo
description: 跑完「唯讀」盤點腳本後 repo 內已 commit 的報告變成空骨架——腳本自述的唯讀鐵律只涵蓋叢集側，--report 預設值直指 repo 檔案且無條件覆寫；對空集合環境跑一次即靜默洗檔，exit 0 無警告
metadata:
  type: pitfall
---

# Pitfall: 「唯讀」盤點腳本把 repo 內已 commit 的報告靜默洗成空骨架

> 來源：2026-07-25 #411 W5-pre gate 覆核。在 prod（零 CloudService、租戶 ns 空集合）跑
> `scripts/audit/audit_411_reset_values.py` 做覆核，跑完 `git status` 才發現
> `docs/tracking/reports/411-reset-values-audit.md` 從 **1054 行的 W4 stg 完整驗證版
> 變成 30 行空骨架**（已 commit 的內容）。用 `git checkout` 還原沒有損失，但 W5-post
> 還要再跑一次，同一顆地雷會再踩。

## Symptom

- 跑一支自述「唯讀盤點」的腳本，**exit 0、stderr 只有 `[done] 報告：<repo 路徑>`**，沒有任何警告。
- 跑完 `git status --porcelain` 冒出 ` M docs/tracking/reports/<report>.md`。
- diff 一看：整份被換成「目標 ns：0 / release：0 收集成功」的空骨架，原內容全失。
- 特別容易發生在**空集合環境**：腳本收集到 0 筆資料照樣渲染一份「合法但空」的報告蓋上去。

## Root cause

三件事疊加，每一件單獨看都無害：

1. **「唯讀」語意只涵蓋叢集側**。腳本 docstring 的鐵律段寫的是「只 `kubectl get/list` 與
   `helm list/get values`，零寫入」——那是對**叢集**的承諾。讀的人（含 agent）自然外推成
   「整支無副作用」，但它對**本機檔案**是寫入的。
2. **`--report` 預設值直指 repo 內的 committed 檔案**（`docs/tracking/reports/...`）。
   不帶任何旗標跑＝「更新那份報告」，而使用者的意圖往往只是「看一眼現況」。
3. **寫入是無條件覆寫**：不 append、不備份、不比對內容、不看是否為空集合、不問 git 狀態。
   有內容的 1054 行與空骨架 30 行在腳本眼裡是同一個 `write_text`。

空集合是最惡的觸發條件：資料為 0 時腳本不會失敗（也不該失敗），於是「什麼都沒查到」被
序列化成「權威報告」蓋掉「真的查到的東西」。

## Fix

`scripts/audit/audit_411_reset_values.py`（本 pitfall 同批）：

1. **預設輸出離開 repo**：`--report` 預設值改 `None` → 落點
   `default_report_path(env)`＝系統 tmp 的 `411-reset-values-audit-{env}.md`
   （`scripts/audit/audit_411_reset_values.py:468`），與 `--raw-dir` 落點語意一致。
   **要寫進 git 必須顯式給 `--report`**——覆寫 committed 檔案從此是明示動作，不是預設。
2. **預設檔名帶 env**：prod / stg / both 三份預設報告天然不互踩。
3. **跨環境覆寫 fail-loud**：`guard_report_target()`
   （`scripts/audit/audit_411_reset_values.py:484`）在**收集開始前**讀既有報告 header 的
   ``｜env=`x`｜`` 標記，與本次 `--env` 不同即 `SystemExit`，要 `--force` 才放行。
   抓不到標記（手寫檔／格式外）一律放行，不誤擋。
4. **docstring 鐵律段正名**：「唯讀**僅指叢集側**；`--report` / `--raw-dir` 指到的本機檔案
   會被無條件覆寫」。

驗證（純本地、假 `kubectl`/`helm`，零叢集連線）：重現條件（`--env prod`、零租戶 ns、
不給 `--report`）跑完 `git status --porcelain` 為空、報告落 tmp；顯式指 repo 報告
（env=stg）配 `--env prod` 則 exit 1 且檔案零位元變動。

## Guard

- 回歸測試：`scripts/audit/test_audit_411_classifier.py:195`（`TestReportTarget`）——
  預設落點不得在 repo 內、檔名須帶 env、header marker round-trip（render 格式漂移即紅）、
  mismatch fail-loud / `--force` 放行 / 無 marker 不誤擋。
- 無 lint gate：本 class 難以 grep 判定（要判「default 是否指向 repo 內路徑」需 AST +
  路徑語意），故留測試層守。

## Class 級教訓

寫或用「唯讀／盤點／稽核」腳本時：

1. **「唯讀」要指明對誰唯讀**。對叢集唯讀 ≠ 對 repo 唯讀。docstring 只寫一半，讀者會把
   保證外推到全部。
2. **會覆寫 committed 檔案的路徑，永遠不要當預設值**。預設落 tmp，進 git 靠顯式旗標；
   反過來（預設進 git、靠旗標保平安）等於把「保護」外包給每一次的人為記憶。
3. **空集合是最危險的成功路徑**。收集到 0 筆而仍照常輸出的程式，等於用「查無資料」覆蓋
   「已查到的資料」；輸出前至少要問「這次要蓋掉的東西，是不是比我手上的還多」。
4. 稽核類腳本跑完**一律 `git status --porcelain` 收尾**——這次就是靠它才發現。

## 相關

- 索引：[docs/tracking/pitfalls/README.md](README.md)
- 報告本體：`docs/tracking/reports/411-reset-values-audit.md`（W4 stg 完整驗證版）
- #411 reuse→reset 統一管線（W1–W4 已收官，W5 待排）
