## ⛔ 鐵律（不可協商）

⚠ **先讀這句**：下面 1–3 條**首先是紀律規則**，其次才有守線。對應的 `P-merge` /
`P-approve` / `P-tag` 是**對 shell 指令字串做比對**的 best-effort 守線——它們擋得掉常見寫
法，但**擋不住所有形式**（見下方「守線有兩種強度」）。⛔ **不要因為「沒被擋下來」就推論
這件事可以做。**

1. **不代 merge**——merge 是人工 gate，**絕對、不分 PR 類型**。`P-merge` 會擋
   `tea pr|pulls merge`、`gh pr merge`、`glab mr merge`、`gh api ... /merge` 等常見寫法。
2. **不代簽 approval**——prod build 的 `/approve` 只能授權者本人送出，**即使人類當場明
   確授權、即使手上憑證就是授權者本人也不代簽**。`P-approve` 擋含 `/approve` 的指令。
3. **不推 tag、不建 release**——`P-tag` 擋 `git push` 推 tag（`v<版號>` / `--tags` /
   `refs/tags/`）、`git tag <name>`、以及 forge 的 release／`git/refs` 建立。⚠ 它是字串比對：
   branch 名以 `<字>-v<數字>` 開頭（`1032-v18-upgrade`）或 `HEAD:v…` 也會被擋——branch 一律
   `<type>/<issue-id>-…`；只是要讀 tag 用 `git fetch --tags`／`git ls-remote --tags`／`git tag -l`。
4. **一律 zh-tw 繁體中文**：PR / Issue / Comment 描述、commit message、程式碼註解。技術
   詞彙保持 English。
5. **Gitea 一律用 `tea` CLI**（git-prod.woowtech.io）。`gh-origin` 只是 mirror。
6. **看板唯讀**：`.woow-issues/` 只有 `woow-issue-leader` 能寫。`P-board` 有兩側：比對
   **工具名**的那一側（`Write`／`Edit`／`sys_os_write`…）是可靠的；比對 **shell 字串**的
   那一側是 best-effort。要更新看板就把內容寫進回報，由上層代寫。
