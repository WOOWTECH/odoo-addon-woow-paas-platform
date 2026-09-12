---
name: impl-pi-implement-contract
description: pi 實作 worker 的工單執行流程：待在 worktree、跑綠 gate、zh-tw commit、用 tea 開 PR。
---

# impl-pi-implement-contract

## 什麼時候用

拿到 `purpose: implement` 的工單時，照這條走完。內容與 `impl-cc-implement-contract` 對齊
——刻意分成兩份，是因為來源池的 skill name 必須全域唯一（`discover_host_skills` 是
first-wins 去重，同名後者會被吃掉）。

## 步驟

1. **確認 worktree**。開工先 `cd <工單給的 worktree 絕對路徑>`；`pwd` 對照工單給的
   路徑，branch 對照工單。不對就停下來回報，⛔ 不要另開。
   `block_working_dir_changes` 的 `allowed_dirs` 只含該 worktree：相對路徑的 cd、cd 到別處、
   `git -C <別處>`、`git worktree add|move|remove` 都會被 DENY。跑子目錄的測試把路徑當
   參數傳（`python -m pytest src/tests/test_x.py`），⛔ 不要 `cd src && pytest`。
2. **確認整合分支**（`develop` / `main` / `master` 因 repo 而異，⛔ 不要寫死）。
3. **只動契約點名的檔案**。動之前先讀。
4. **跑綠**：測試、lint、typecheck。指令與輸出留著。
   ⚠ 本 repo 的 Odoo 測試一律在**沙盒 odoo pod 內**跑；repo 裡沒有 `odoo-bin`。
5. **commit**：zh-tw，前綴十選一。⛔ 不要加「Generated with …」字樣。
6. **push branch**（⛔ 不是受保護分支、⛔ 不 force-push），用 `tea` 開 PR，標題前綴
   `⛔ 勿代 merge｜待 <覆核者> 覆核 — `，描述貼真實輸出，關聯當下就寫。

## 本 repo 特有的地雷

- ⛔ **不要動 `src/__manifest__.py` 的版號**（`manifest-version-gate` 會擋）。
- PR 的 CI 綠燈**不含 Odoo 測試**——動 `src/` 要自己在沙盒跑完並貼進 PR。
- 改 `extra/paas-operator/` 前先看 `paas-operator-dev` skill：**integration tests 沒有 CI
  runner**，合併前必須本機跑真 K8s（⚠ helm 必須 3.13.3，不可用系統 helm）。
- 動過 `docs/` 就跑 `python3 scripts/audit/gen-docs-index.py` 重產 `docs/index.md`。
