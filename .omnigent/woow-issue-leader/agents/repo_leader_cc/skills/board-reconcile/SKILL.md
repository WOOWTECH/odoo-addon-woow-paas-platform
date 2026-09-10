---
name: board-reconcile
description: 看板 .woow-issues/board.md 的維護：只有 issue-leader 能寫，寫誰在做什麼、不寫流水帳。
---

# board-reconcile

## 為什麼只有你能寫

`.woow-issues/board.md` 是跨 issue 的**單一狀態面**。多人同時寫會漂，所以全樹只有
`woow-issue-leader` 有寫權；兩個 `repo_leader` 與全部 16 個 worker 都唯讀，由機器守線
**P-board** 強制（寫入／編輯工具與 shell 改寫都 DENY）。

下層要更新看板，就把內容寫進回報，由你代寫。

## 寫什麼

- **誰在做什麼**：issue 編號、派給哪個 `repo_leader` / worker、目前卡在哪。
- **待人工決策的項目**：等誰、等什麼、多久了。
- **待人工 merge 的 PR**：連結 ＋ 覆核狀態。

## ⛔ 不寫什麼

- 已完成的流水帳。做完就從看板移掉——歷史在 git log 與 issue 裡，看板只反映**現況**。
- 從別的地方複製過來、會自己漂掉的事實（測試數、版號、叢集狀態）。要嘛寫連結，要嘛註
  明量測時間。

## 對帳節奏

1. 每輪收斂子代回報後，順手更新一次。
2. 更新前先讀現況——⛔ 不要用你記憶中的版本覆蓋掉別輪寫進去的東西。
3. 看板膨脹到看不懂時，先把全文存底進 `docs/archived/`，再精簡；⛔ 不要直接刪。
