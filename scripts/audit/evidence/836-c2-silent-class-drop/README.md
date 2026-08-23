# #836 段一：量測與 RED 的證據包

issue [#836](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/836)
第一段（範圍獨立確認 ／ RED ／ `--update-baseline` 連帶影響）的**原始輸出與可重跑探針**。

- 量測基底：`16b5fc04ec7e3adcf3a0b02602b70213761f9a11`（＝當時的 `origin/develop`）
- 日期：2026-08-22
- 被測物：`scripts/audit/test_collection_audit.py` 的 C2（`--check-baseline`）
  ＋ 它借用的 `scripts/audit/repo-lint.py` 判準
- 對應的 RED：`scripts/audit/red_836_silent_class_drop.py`（**故意是紅的**，exit 1）
  —— 🔴 **段二已移除該檔**，見下方「段二之後」。

## 段二之後（2026-08-23，基底 `e3ba0d0d`）

段一那支 RED 的觸發器（`def test*` 包在 class body 的 `if` / `try` 裡）已被 **#850**
（PR #867，merge `91fb7df6`）修掉。它在新基底上**仍然 exit 1**，但**紅在別的分支上**：
那兩個 class 已經進了 `expected`（條數 1），而 `check_baseline()` PASS 時本來就不列名
⇒ 它的「輸出沒提到這個名字」照樣成立，失敗訊息（「條數被算成 0 之後…」）卻是假的。
**顏色對、診斷錯**——比預期中的「變綠」更難察覺（原始輸出見段二報告）。

⇒ 段二用 non-`def` 形態（`test_x = lambda`／`setattr`／decorator，#850 明文標為範圍外
的那批）重建成 `scripts/audit/test_836_zero_count_class_named.py`，並刪掉舊檔——
留著會是一支**必定 ValueError** 的死檔（下方 §「`probes/` 的注意事項」同理）。

🔴 **`probes/` 三支與本目錄所有 `evidence/*.txt` 一律不動**，理由同下：它們是
`16b5fc04` 當時的原始輸出與產出它們的那一版程式。其中 `t1_partition.py` /
`t1_p9_p10.py` 寫的是 `expected, excluded = audit.scan_expected(ROOT)`——
#836 段二把回傳改成 **3-tuple**（多了「條數 0」那個容器）之後，**這兩支在現行樹上
會丟 `ValueError`**。那是刻意的：把它們改成能跑，就把「當時量到什麼」這件事改掉了。
要重跑請複製一份改，不要就地改原件。

🔴 **段二開 PR 前要決定這批是否保留**——這批證據檔會讓 PR 變大。
現在照搬進分支是因為**保全優先於整潔**（判別式只活在 scratchpad 等於沒有耐久載體，
同 #844 的教訓）。保留／搬走／裁掉由 leader1 決定，接手的人不用自己判斷。

## 讀什麼

| 檔 | 是什麼 |
| --- | --- |
| `report-段一.md` | 完整交付報告：三件事的實際輸出 ＋ 主張對證據表 ＋ What I did NOT verify ＋ 邊界 |
| `predictions.md` | 🔴 **跑之前**釘死的 10 個預測值。它的價值全在時序——不要事後改寫成結果 |
| `evidence/` | 下面三支探針與 RED 的原始 stdout |
| `probes/` | 三支可重跑的探針（原件，見下方注意事項）|

## `evidence/` 逐檔

| 檔 | 由誰產生 | 內容 |
| --- | --- | --- |
| `evidence-t1-universe.txt` | `probes/t1_universe.py` | P1 全集 737 ／ P2 baseline 檔 653 行、0 重複 key |
| `evidence-t1-partition.txt` | `probes/t1_partition.py` | G1/G2 自我檢查 ＋ P3–P7（分割 737 = 653 + 6 + 56 + 22） |
| `evidence-t1-p9p10.txt` | `probes/t1_p9_p10.py` | P9／P10：拿 S56／S22 去驅動**真的** C2 的執行期觀測 |
| `p9_output.txt` / `p10_output.txt` | 同上 | 那兩次 `check_baseline()` 的完整 stdout |
| `c2_baseline_now.txt` | `test_collection_audit.py --check-baseline` | C2 在 `16b5fc04` 的現況輸出（PASS 653 項、註記 6 條） |
| `S56.txt` | `probes/t1_partition.py` | 走到 `if n:` 那格被靜默丟棄的 56 個 ident |
| `S22.txt` | 同上 | 走到「非 tagged 且 own 為空」那格被靜默丟棄的 22 個 ident |
| `X6.txt` | 同上 | 現行 `excluded`（有列名的）6 個 ident |

⚠ `S56.txt` 已與 leader1 的獨立量測做過集合比對（雙向差皆空、比對器有陽性對照）。
`S22.txt` **尚未**與他的清單逐條對過——詳見報告 §5。

## `probes/` 的注意事項

三支探針**刻意保留原件不改**：它們就是產出上面那批 `evidence/` 的那一版。
代價是裡面寫死了當時 worktree 的絕對路徑：

```python
ROOT = pathlib.Path("/Users/eugene/Documents/woow/AREA-odoo/woow-addons/wt-836")
```

在別的 checkout 重跑，改那一行即可（三支各一處）。

`t1_partition.py` / `t1_p9_p10.py` 有兩個一定要保留的機制，改動前先讀報告 §1：

1. **共用同一顆 `facts`**。`scan_expected()` 一次執行裡 `_test_tree_facts` 會被呼叫
   **2 次**，兩次 parse 出的是**不同的 AST node 物件**。不 memoize 的話 node identity
   一個都對不上，會靜默拿到 `0`——而 `0` 看起來像「這一格沒命中」，不像「我的 key 對不上」。
2. **node identity 當 key，而不是全域打樁**。`_test_own_test_methods` 全 repo 有 **6 個**
   呼叫點，全域打樁會把不屬於那一格的呼叫算進來（leader1 實測會得到 89 而不是 22）。
   `_expected_methods` 只有 1 個呼叫點，打它才是乾淨的。

## 沒有搬進來的

`proto_red.py` / `proto_controls.py` 兩支原型——它們的內容已經整併進
`scripts/audit/red_836_silent_class_drop.py`，留著會變成第二份會漂移的副本。

Refs #836 #546 #838 #844
