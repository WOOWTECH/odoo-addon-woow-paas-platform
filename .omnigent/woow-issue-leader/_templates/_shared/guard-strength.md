## 守線有兩種強度

- **可靠（比對工具名／結構化參數）**：`read_only_os`（唯讀 worker 完全不能寫檔——這是
  唯讀保證的**唯一一層**；唯讀 worker 一樣有 `sys_os_*` 工具）、`P-path`（**只掛在
  implement worker**：檔案工具的寫入路徑必須落在工單指定的 worktree 底下）、`purpose`
  檢查。這些不看 shell 字串，繞不過。
- **best-effort（比對 shell 指令字串）**：`P-merge`／`P-approve`／`P-tag`／`P-escape`／
  `P-board` 的 shell 那一側、`P-kubectl`（**只掛在 `ops_k8s`**）。**已知可被 shell 轉義與
  變數展開繞過**，它們防的是**手滑**，不是刻意規避。

守線是安全網，不是授權邊界——**規則本身才是**。
