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

## 2. file-history 全域盤點：零內容遺失

**截至 2026-08-05 17:08**（此結論會過期，重跑見下方腳本）：

- 掃描範圍：335 個 session 目錄、6335 個快照，1271 個對應到 memory 檔
- 72 個 memory 檔有快照可比對
- **真正的整段消失：0 個**。7 檔各差 1 行，逐一看全是改寫

⚠ 這個結論的前提是「有快照可比對的 72 檔」。**124 檔中有 52 檔沒有任何快照**——
對那 52 檔，本次盤點不是「沒有遺失」，是**沒有檢查**。

### 被推翻的兩個診斷

leader4 通報「`feedback_absence_conclusion_scope_not_universe.md` 被吃掉 21 行」，
實測**不成立**：

1. 該段在現況檔行號 329 完好存在，關鍵字命中數與快照逐項相同；全檔逐行集合差顯示
   快照有而現況沒有的只有 1 行，且是 frontmatter 的 `modified:` 時間戳。
2. 通報所依據的量測（327 行 / mtime 13:11）在通報時已過期——實際是 367 行 / 15:35。
   該段**曾**被吃掉，但已被救回，救援者還把事件經過寫成了該檔末的「附記」。

同一輪通報中另一個數字「117 行被吃掉」是 hash 配反造成的——拿 `MEMORY.md` 的快照
去 diff 某個 topic 檔。**假數字看起來與真數字沒有差別**。

## 3. 落到機械 gate

`scripts/audit/memory-integrity.py` —— 兩條 check 都做過陽性對照（注入斷鏈／孤兒／
截短檔案，三種缺陷全部亮紅、exit 1；真實 memory exit 0）。

```bash
python3 scripts/audit/memory-integrity.py              # 兩個 check
python3 scripts/audit/memory-integrity.py --links      # 只驗索引
python3 scripts/audit/memory-integrity.py --truncation # 只驗遺失
```

三個會產出假數字的坑寫在腳本 docstring 裡，其中最關鍵的是：**判準是「整段消失」
不是「有差異」**。未經參照 normalize 時 38 檔報有差異，逐一查全是 wiki link 改寫成
docs 路徑——只看行數會把 38 個全部誤判成遺失。

查法本身記在 memory `reference_find_concurrent_file_writer_via_file_history`。
