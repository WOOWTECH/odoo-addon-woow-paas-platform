## ⛔ 不要離開你的 worktree

三條機器守線合起來把你的**檔案工具寫入**關在 worktree 裡：

- **`P-path`（可靠，比對工具參數）**：`sys_os_write`／`sys_os_edit`／`Write`／`Edit`…的
  `path`／`file_path` 必須以工單指定的 worktree 為前綴（相對形式 `paas/wt-<id>/…` 或絕對
  形式皆可），且不得含 `..`。寫到 sibling repo、寫到 repo root 的**主 checkout**，一律 DENY。
- **`worktree_guard`（可靠）**：絕對路徑或含 `..` 的寫入一律 DENY——與上一條合起來，實際上
  只剩「相對、且在 worktree 前綴底下」這一種寫法會過。
- **`block_working_dir_changes`（可靠）**：`allowed_dirs` 只含工單給的 **repo root 與
  worktree** 兩處，進不到這兩處或其子目錄的 `cd` / `pushd` / `popd` / `git -C <path>` 一律
  DENY；`git worktree` 子命令放行（你要自己 add）。

⚠ 三條都不看 **shell 重導向**：`echo x > <別的路徑>`、`sed -i`、heredoc 這種由 shell 寫檔的
路徑**沒有機器守線**——「只在 worktree 裡改檔」在那一側是紀律規則。

實務上這代表：

- runner 的 cwd 是六 repo 的共同祖先（`/home/elmo/woow-repos`），**不是**你的 worktree。
  開工第一件事：建 worktree（上面第 1 步），再 `cd <工單給的 worktree 絕對路徑>`
  （⛔ 相對路徑的 `cd` 會被擋）。不確定 shell 是否保留 cwd 時，每條指令都以
  `cd <worktree> && ` 開頭——多 cd 一次無害。
- 檔案讀寫工具的 `path` 一律相對、不含 `..`，基準是 harness 行程的 cwd（＝共同祖先），
  寫成 `paas/wt-<id>/src/x.py` 這種形式——前綴就是工單裡的 worktree 路徑去掉共同祖先。
- 要在子目錄跑測試就把路徑當參數傳（`python -m pytest src/tests/test_x.py`），
  ⛔ 不要 `cd src && pytest`。
- **跨 repo 只剩絕對路徑的唯讀**：`grep -rn x /home/elmo/woow-repos/<repo>/src/` 可以，
  `git -C <repo> log` 不行。需要別 repo 的 git 歷史或改動，一律回報給上層。
