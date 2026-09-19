---
name: impl-cc-implement-contract
description: claude-native 實作 worker 的工單執行流程：待在 worktree、跑綠 gate、zh-tw commit、用 tea 開 PR。
---

# impl-cc-implement-contract

## 什麼時候用

拿到 `purpose: implement` 的工單時，照這條走完。

## 步驟

1. **建 worktree、再進去**。worktree 是你自己建的：
   `git -C <工單給的 repo root> worktree add <工單給的 worktree 路徑> -b <type>/<issue-id> origin/<整合分支>`
   （已存在就直接用）。接著 `cd <worktree 絕對路徑>`；`pwd` 對照工單給的路徑，
   `git rev-parse --abbrev-ref HEAD` 對照 branch。不對就停下來回報，⛔ 不要換路徑另開。
   `block_working_dir_changes` 的 `allowed_dirs` 只含 repo root 與該 worktree：相對路徑的
   cd、cd 到別處、`git -C <別處>` 都會被 DENY。⛔ 進 repo root 只為建 worktree，不要在主
   checkout 裡改檔。跑子目錄的測試把路徑當參數傳（`python -m pytest src/tests/test_x.py`），
   ⛔ 不要 `cd src && pytest`。
2. **確認整合分支**。`develop` / `main` / `master` 因 repo 而異——用
   `git symbolic-ref refs/remotes/origin/HEAD` 或直接看 repo 的 `CLAUDE.md`，⛔ 不要寫死。
3. **只動契約點名的檔案**。動之前先讀，不要憑檔名猜內容。
4. **跑綠**：跑你動到那部分的測試、lint、typecheck。指令與輸出留著，等下要貼進 PR。
   ⚠ 本 repo 的 Odoo 測試一律在**沙盒 odoo pod 內**跑（`/woow-dev:k8s-dev-sandbox test
   <slug> --module woow_paas_platform`），repo 裡沒有 `odoo-bin`／`odoo.conf`。
5. **commit**：zh-tw，前綴十選一（`feat:` `fix:` `docs:` `style:` `refactor:` `perf:`
   `test:` `chore:` `revert:` `release:`）。⛔ 不要加「Generated with …」字樣。
6. **push branch**（⛔ 不是受保護分支、⛔ 不 force-push），用 `tea` 開 PR：
   - PR 內文先寫進 worktree 內的檔案（⛔ 不要 commit 它），再於**同一個 shell 呼叫**裡先 cd
     進 worktree 才開 PR：
     `cd <worktree 絕對路徑> && tea pulls create --head <branch> --base <整合分支> --title "<標題>" --description "$(cat <PR 內文檔絕對路徑>)"`
   - ⛔ 不帶 `--repo owner/repo`（會被當成本地路徑、回 `local repository required`；tea 從
     worktree 的 origin remote 推斷 repo）。`--head`／`--base` 一律明寫。
   - `--title` 裡 ⛔ 不用 ASCII `&`、`|`、`;`（`block_working_dir_changes` 切指令時不看引號，
     會誤擋整條；全形 `｜` 沒事）。
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
