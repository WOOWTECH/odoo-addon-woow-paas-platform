---
title: memory 索引重指與 file-history 全域盤點（整併後稽核）
category: tracking
status: active
created: 2026-08-05
updated: 2026-08-05
related: [docs/tracking/memory-inventory-2026-08-05.md, docs/tracking/memory-migrated-index-2026-08-05.md, scripts/audit/memory-integrity.py]
---

# memory 索引重指與 file-history 全域盤點

2026-08-05 的 memory 整併（commit `363d25f2`，103 篇搬進 `docs/`）之後的兩次稽核。
**兩件事都是零訊號的**：整併當下沒有任何錯誤訊息，是使用者隨口問了一句
「你的 memory.md 在哪？」才被翻出來。

## 1. 索引斷鏈與孤兒（已修）

| | 稽核前 | 修復後 |
| --- | --- | --- |
| 斷鏈（索引連到、檔案不存在） | 60 | 0 |
| 孤兒（檔案存在、索引沒連到） | 27 | 0 |
| 連結總數 | 156 | 188（65 條指向 repo docs） |

**成因**：搬進 `docs/` 時檔名同時被改寫（`project_501_global_cap_landing.md` →
`501-global-cap-landing.md`），但留在 `MEMORY.md` 的行沒跟著改。整併當下的完備性
檢查只覆蓋「搬走的檔」，沒覆蓋「**留下的行對搬走的檔的引用**」。

**修復方式**：映射來源不是檔名猜測——每個搬進 docs 的檔 header 都自述
「原 slug `xxx`」（103 檔，與 commit 宣稱的篇數吻合），用它建權威映射，再與機械
slugify 交叉比對，兩法一致。

兩段指標的處理**刻意不同**，因為實況相反：

- `## 近期 landing`：103 篇是**刻意**搬走的（為瘦身每 session 的 context），原索引
  行已逐字保存於 `memory-migrated-index-2026-08-05.md` ⇒ 只改指標，不把 bullet 搬回來。
- `## Odoo 框架陷阱`：這批**根本沒被搬走**，24 篇檔案實體仍在 memory 目錄，但
  `MEMORY-odoo.md` 被刪時它們的索引行沒有任何載體承接 ⇒ 從備份逐字恢復 18 條 bullet。

另有 5 篇是真正的入口全失（檔案在、索引行不在任何載體），其中 1 篇從未進過索引。

## 2. file-history 全域盤點

> 🔴 **本節的數字是快照值，量於 2026-08-05 17:08，且用的是腳本的舊機制**（當時
> `latest_snapshots()` 只比對 mtime 最新的一份快照）。腳本其後改成
> `all_snapshots()`／比對所有歷史快照，**同一份 memory 目錄的結果因此完全不同**
> ——見下方 §2.2。**不要引用本節數字**，要當前結論請自己重量：
> `python3 scripts/audit/memory-integrity.py --truncation`。

當時（舊機制）量到：

- 掃描範圍：335 個 session 目錄、6335 個快照，1271 個對應到 memory 檔
- 72 個 memory 檔有快照可比對
- **真正的整段消失：0 個**。7 檔各差 1 行，逐一看全是改寫

⚠ 這個結論的前提是「有快照可比對的 72 檔」。**124 檔中有 52 檔不在比對範圍內**——
對那 52 檔，本次盤點不是「沒有遺失」，是**沒有檢查**。

⚠⚠ 上面那句原本寫的是「52 檔**沒有任何快照**」，那個歸因是錯的，而且錯得有代表性：
其中 **45 檔是因為 frontmatter 的 `name:` 與檔名對不上而被靜默跳過**（例
`reference_kubectl_woow_k3s.md` 的 `name: kubectl-woow-k3s`），它們**永遠不會被檢查**，
不是「還沒有快照」。兩者在腳本輸出上**完全不可區分**——只印得出「其中 72 檔有快照
可比對」，看的人自然把差額歸因成前者。詳見腳本 docstring 的「已知限制」段與 issue #595。

### 2.1 被推翻的兩個診斷

leader4 通報「`feedback_absence_conclusion_scope_not_universe.md` 被吃掉 21 行」，
實測**不成立**：

1. 該段在現況檔行號 329 完好存在，關鍵字命中數與快照逐項相同；全檔逐行集合差顯示
   快照有而現況沒有的只有 1 行，且是 frontmatter 的 `modified:` 時間戳。
2. 通報所依據的量測（327 行 / mtime 13:11）在通報時已過期——實際是 367 行 / 15:35。
   該段**曾**被吃掉，但已被救回，救援者還把事件經過寫成了該檔末的「附記」。

同一輪通報中另一個數字「117 行被吃掉」是 hash 配反造成的——拿 `MEMORY.md` 的快照
去 diff 某個 topic 檔。**假數字看起來與真數字沒有差別**。

### 2.2 改用「比對所有歷史快照」後的結果（量於 2026-08-05 18:05）

| | 舊機制（只比最新快照） | 現行（比對所有快照） |
| --- | --- | --- |
| exit code | 0 | **1** |
| ≥3 行的 finding | 0 筆 | **10 筆**（合計 99 行） |

差異來源是機制而非資料：舊機制對**遺失後又有新編輯**的檔案必然失效（新快照已是缺失
狀態，拿缺失比缺失差 0），現行版逐一比對所有快照取差異最大者。代價是把「歷史上存在過、
後來有意改寫或搬移」的內容也一併報出來——腳本 docstring 明說這是「寧可誤報也不漏報」
的刻意取捨。

🔑 **判讀時最容易出錯的一點：「整段消失」與「內容被搬到別的檔」在單檔比對下輸出完全
相同。** 判定某筆 finding 是不是真的遺失，必須先問「這些行是不是在 memory 目錄的別處
還活著」。這 10 筆逐字比對的結果是 99 行中 **2 行**在別的 memory 檔裡逐字存在（例
`任何聽起來合理的解釋都會被接受` 現活在
`feedback_completeness_check_output_which_not_how_many.md`）；至於「改寫後搬移」，
我試過兩種關鍵片段探針，因為門檻與 markdown 剝除方式不同而得到互相矛盾的數字，
**這個判定目前不好機械化**（列為 issue #595 的一部分）。

## 3. 落到機械 gate

`scripts/audit/memory-integrity.py` —— 兩條 check 都做過陽性對照：注入斷鏈／孤兒／
截短檔案，三種缺陷全部亮紅、exit 1，還原後回綠。

> 🔴 **本段原本還寫著「真實 memory exit 0」，那句在寫下時成立、現在是假的。**
> 量於 **2026-08-05 18:05**（腳本 252 行、`all_snapshots()` 版本）：真實 memory 目錄
> 是 **exit 1**，`index-links` 全綠而 `silent-truncation` 報 10 筆 ≥3 行。成因與判讀
> 見 §2.2——**不是新的遺失，是機制換了之後把歷史改寫也一併報出來**。
>
> ⚠ 這裡刻意不把數字改成「10 筆」了事：那個 10 一樣會過期。**任何時候要知道當前
> 狀態，自己重量**（下方指令，約 10 秒），並用 §2.2 的判讀方式決定每筆是不是真遺失。
> 這份 doc 記的是**判準與機制**，不是某一刻的計數。

```bash
python3 scripts/audit/memory-integrity.py              # 兩個 check
python3 scripts/audit/memory-integrity.py --links      # 只驗索引
python3 scripts/audit/memory-integrity.py --truncation # 只驗遺失
```

會產出假數字的坑寫在腳本 docstring 裡（撰稿時三個，後來補到四個——**以腳本裡那份
為準，不要引用本行的數量**），其中最關鍵的是：**判準是「整段消失」不是「有差異」**。
未經參照 normalize 時 38 檔報有差異，逐一查全是 wiki link 改寫成 docs 路徑——只看
行數會把 38 個全部誤判成遺失。

腳本另有「已知限制」段，列出兩個**輸出上看不出來**的覆蓋缺口（`name:` 對不上的 45 檔、
跨檔搬移與遺失不可區分）——判讀 finding 前必讀。

查法本身記在 memory `reference_find_concurrent_file_writer_via_file_history`。
