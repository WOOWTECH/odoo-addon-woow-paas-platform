## grep pattern 是資料、不是動作

- `block_working_dir_changes` 只掛 implement worker，屬可靠型守線；它切 shell
  字串時不理會引號。多 pattern 的 grep 交替式若在 pattern 放入 `cd <x>`、`pushd` 或
  `git -C <x>`，會被切成換目錄動作而 DENY。#1132 的 C1 已記錄單一 pattern 放行，
  不代表所有單一 pattern 都必然誤擋。
- `P-tag`／`P-escape` 掛所有角色，屬 best-effort 字串比對。pattern 中的 `git push`、
  `refs/tags`、`--tags`、`v<版號>`，或反斜線加敏感字根 kube／merge／approve／tag／
  push／board／woow-issues，依紀律視為會遭 DENY。
- 多 pattern 一律用 `grep -e <p1> -e <p2>`，或先逐行建立 pattern 檔，再用
  `grep -F -f <檔>`／`grep -E -f <檔>`。引號內不用 ASCII 豎線或反斜線豎線交替式；
  整條 shell 指令須單行、零反斜線，cwd 規則沿用既有片段。
- 要搜尋的字串本身若為守線敏感字，例如 `cd `、`git -C`、`git push`、`refs/tags`，
  只能放進 pattern 檔後用 `-f <檔>`；資料不得出現在 shell 指令字串。
- pattern 檔一律用編輯工具寫入：實作者只能寫指定 worktree；唯讀 worker 受
  `read_only_os` 限制，不能自行寫 scratch，須請有寫權者代寫到符合其角色寫權的位置，
  不因此取得任何寫權。
- 事前選用 `-e`／`-f` 是合規寫法；若字串守線已 DENY，立即停止並依原契約回報待人裁定，
  不改寫重試、不拆字繞過，也不得以本片段作為 DENY 後自行重試的理由。
