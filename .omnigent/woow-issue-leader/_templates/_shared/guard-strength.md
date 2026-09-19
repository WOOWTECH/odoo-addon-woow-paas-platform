## 守線有兩種強度

- **可靠（比對工具名／結構化參數）**：`read_only_os`（唯讀 worker 完全不能寫檔——這是
  唯讀保證的**唯一一層**；唯讀 worker 一樣有 `sys_os_*` 工具）、`P-path`（**只掛在
  implement worker**：檔案工具的寫入路徑必須落在工單指定的 worktree 底下）、`purpose`
  檢查。這些不看 shell 字串，繞不過。
- **best-effort（比對 shell 指令字串）**：`P-merge`／`P-approve`／`P-tag`／`P-escape`／
  `P-board` 的 shell 那一側、`P-kubectl`（**只掛在 `ops_k8s`**）。**已知可被 shell 轉義與
  變數展開繞過**，它們防的是**手滑**，不是刻意規避。

守線是安全網，不是授權邊界——**規則本身才是**。

## Shell

- 須單行、零反斜線；反斜線接英數的跳脫序列（換行、tab）及續行禁。再含 kube/merge/approve/tag/push/board/woow-issues，P-escape DENY，無關動作(git merge-base、board-scan.py 路徑)。連要寫進文件的紀律文字也不要放進 shell 指令。
- heredoc、Python 腳本、多行留言由編輯工具落檔再單行執行；多行輸出分次 echo。無寫權者依角色契約交有權者代寫，不增權。
- 字串守線 DENY 即停；issue 留言附被擋指令原文與 DENY 訊息，依角色契約回報待人裁定；唯讀者依契約交 leader 代貼。禁改寫重試或拆字繞過；人判語法誤擋才單行零反斜線續作。
