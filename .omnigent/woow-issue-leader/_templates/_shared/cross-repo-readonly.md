## 跨 repo（唯讀）

別的 repo **只能唯讀**，而且**只能用絕對路徑**：

- ✅ `cat` / `grep` / `rg` / `ls` 帶**絕對路徑**（例如
  `grep -rn '_inherit' /home/elmo/woow-repos/<repo>/src/`）。這不是 working-dir 變更，
  不會被守線攔。
- ⛔ **不得用 `cd` 或 `git -C`**。**你這個角色沒有掛 `block_working_dir_changes`，所以這
  是紀律規則、不是機器擋的**——但搬動 cwd 會讓相對路徑的結論指到別的 repo、事後無法重現，
  而你的產出就是那些結論。統一用絕對路徑，結論才對得起來。
  （對照：`impl_*` 實作 worker **有**掛這條 policy，它們的 `cd`／`git -C` 是真的會被 DENY。）
- 需要別 repo 的 **git 歷史**（log / blame / diff）或 **CI 狀態**時，回報給上層（上層會加
  `needs-info` 交人決定），⛔ 不要自己想辦法繞過，也不要要求另起 runner。
