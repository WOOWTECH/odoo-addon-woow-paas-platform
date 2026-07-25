# Domain Docs

Engineering skills 探索 codebase 前，先照本檔規則消費 domain 文件。

## 探索前先讀

- 根目錄 **`CONTEXT.md`**——本 repo 唯一 glossary（單一 context，無 CONTEXT-MAP）。
- **`docs/decisions/`**——本 repo 的決策文件（**不用 `docs/adr/`**）。讀與當前主題相關的 decision docs；
  新增 ADR 級決策也寫這裡（用 `/docs:new decisions <name>` 產 frontmatter，沿用六類結構）。

檔案不存在就靜默略過，不要主動建議補建——`/domain-modeling` 會在術語真正定案時才寫入。

## 使用 glossary 詞彙

輸出（issue 標題、重構提案、測試命名）提到 domain 概念時，用 `CONTEXT.md` 定義的詞，
不要漂移到 glossary 明列 _Avoid_ 的同義詞。需要的概念不在 glossary？先想是不是自己在發明
專案沒有的語言；確為真缺口就記下來交給 `/domain-modeling`。

## 決策衝突要明示

輸出若與 `docs/decisions/` 既有決策矛盾，明講「與某決策矛盾、為何值得重開」，不准靜默推翻。

## 術語晉升路徑

進行中案件的術語表先跟著該案 decision doc（例：#411）；詞條穩定後晉升進 `CONTEXT.md`。
