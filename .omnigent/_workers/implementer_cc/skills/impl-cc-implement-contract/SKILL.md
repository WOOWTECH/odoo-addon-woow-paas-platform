---
name: impl-cc-implement-contract
description: claude-native 實作 worker 的工單執行流程：待在 worktree、跑綠 gate、zh-tw commit、用 tea 開 PR。
---

# impl-cc-implement-contract

## 什麼時候用

拿到 `purpose: implement` 的工單時，照這條走完。

## 步驟

1. **確認 worktree**。`pwd` 對照工單給的路徑；`git -C . rev-parse --abbrev-ref HEAD` 對照
   工單給的 branch。不對就停下來回報，⛔ 不要自己另開一個。
   ⛔ **不要 `cd`**——`block_working_dir_changes` 會 DENY 所有 `cd` / `git -C` /
   `git worktree`。runner 已經把 cwd 設在你的 worktree，跑子目錄的測試請把路徑當參數
   傳（`python -m pytest src/tests/test_x.py`），不要 `cd src && pytest`。
2. **確認整合分支**。`develop` / `main` / `master` 因 repo 而異——用
   `git symbolic-ref refs/remotes/origin/HEAD` 或直接看 repo 的 `CLAUDE.md`，⛔ 不要寫死。
3. **只動契約點名的檔案**。動之前先讀，不要憑檔名猜內容。
4. **跑綠**：跑你動到那部分的測試、lint、typecheck。指令與輸出留著，等下要貼進 PR。
   ⚠ 本 repo 的 Odoo 測試一律在**沙盒 odoo pod 內**跑（`/woow-dev:k8s-dev-sandbox test
   <slug> --module woow_paas_platform`），repo 裡沒有 `odoo-bin`／`odoo.conf`。
5. **commit**：zh-tw，前綴十選一（`feat:` `fix:` `docs:` `style:` `refactor:` `perf:`
   `test:` `chore:` `revert:` `release:`）。⛔ 不要加「Generated with …」字樣。
6. **push branch**（⛔ 不是受保護分支、⛔ 不 force-push），用 `tea` 開 PR：
   - 標題前綴 `⛔ 勿代 merge｜待 <覆核者> 覆核 — `
   - 描述全 zh-tw，**貼上第 4 步的真實指令與輸出**
   - 關聯**開單當下就寫**：`Closes #<n>`（會關閉）／`Refs #<n>`（只參照）

## 本 repo 特有的地雷

- ⛔ **不要動 `src/__manifest__.py` 的版號**。一般 PR 不動（必須等於 `origin/develop`），
  只有新增 `src/migrations/<X>/` 的 PR 與 `release/*` 發版 PR 才 bump。
  `manifest-version-gate` 是 required check，會擋。
- PR 的 CI 綠燈**不含 Odoo 測試**——動 `src/` 的 PR 要自己在沙盒跑完，把結果貼進 PR 描述。
- 新增／改名測試 class 後要更新 `scripts/audit/baselines/test-collection.txt`，
  ⚠ 用 `scripts/audit/test_collection_audit.py --update-baseline`，**不是**
  `repo-lint.py --update-baseline`。
- 動過 `docs/` 就跑 `python3 scripts/audit/gen-docs-index.py` 重產 `docs/index.md`
  （CI 的 `docs-index-sync` 會紅）。
