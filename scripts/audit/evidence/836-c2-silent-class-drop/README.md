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
的那批）重建成 `scripts/audit/test_836_zero_count_class_named.py`，並把舊檔移出
`scripts/audit/`——留在那裡它會是一支診斷錯誤的紅燈，指著一個已經不存在的成因。
（它現在的實際行為逐支列在下方「逐檔實測」表，**不要**從這段推論。）

舊檔**內容一字不改**地保全進 `frozen/red_836_silent_class_drop.py.frozen`
（原位置：`scripts/audit/red_836_silent_class_drop.py`；由段一那顆
`test: 為 #836 加一支 RED——條數 0 的 class 從 C2 輸出裡整個消失` 加入）。

🔴 **出處刻意不寫 commit sha。** 兩個理由，第二個是實際踩到的：

1. 這張 PR 若被 squash 併入，那顆 sha 就不存在了。
2. **分支還沒推遠端之前，sha 本來就會變。** 本檔第一版寫的是 `a578e71f`，
   一次 `git rebase origin/develop` 之後那顆 commit 變成 `ceb5acef`——
   指標當場過期，而 `git cat-file -e a578e71f` **仍然回 0**（舊 object 還在本地
   object DB 裡、只是不在分支上）⇒ **驗證指標「還在不在」不會發現它已經錯了**。
   直接把 `a578e71f` 換成 `ceb5acef` 只是把同一個陷阱重新上膛。

⇒ 耐久的出處是**檔案本身在樹裡**（＋下方 sha256 對帳），不是任何一顆 commit 的 sha。

🔴 **凍結件與 `evidence/*.txt` 一律不動**：它們是 `16b5fc04` 當時的原始輸出，
以及**產出那批輸出的那一版程式**。把它們改成能跑，就把「當時量到什麼」這件事一起改掉了。
要重跑請**複製一份到別處**改 `ROOT` / `OUT` 再跑（見下方「凍結件」一節）。

## 讀什麼

| 檔 | 是什麼 |
| --- | --- |
| `report-段一.md` | 完整交付報告：三件事的實際輸出 ＋ 主張對證據表 ＋ What I did NOT verify ＋ 邊界 |
| `predictions.md` | 🔴 **跑之前**釘死的 10 個預測值。它的價值全在時序——不要事後改寫成結果 |
| `evidence/` | 下面三支探針與 RED 的原始 stdout |
| `frozen/` | 四個**凍結件**（三支探針原件＋段一那支 RED）與執行守衛 `ast.py`。🔴 不要直接執行，見下方 |

## `evidence/` 逐檔

| 檔 | 由誰產生 | 內容 |
| --- | --- | --- |
| `evidence-t1-universe.txt` | `frozen/t1_universe.py.frozen` | P1 全集 737 ／ P2 baseline 檔 653 行、0 重複 key |
| `evidence-t1-partition.txt` | `frozen/t1_partition.py.frozen` | G1/G2 自我檢查 ＋ P3–P7（分割 737 = 653 + 6 + 56 + 22） |
| `evidence-t1-p9p10.txt` | `frozen/t1_p9_p10.py.frozen` | P9／P10：拿 S56／S22 去驅動**真的** C2 的執行期觀測 |
| `p9_output.txt` / `p10_output.txt` | 同上 | 那兩次 `check_baseline()` 的完整 stdout |
| `c2_baseline_now.txt` | `test_collection_audit.py --check-baseline` | C2 在 `16b5fc04` 的現況輸出（PASS 653 項、註記 6 條） |
| `S56.txt` | `frozen/t1_partition.py.frozen` | 走到 `if n:` 那格被靜默丟棄的 56 個 ident |
| `S22.txt` | 同上 | 走到「非 tagged 且 own 為空」那格被靜默丟棄的 22 個 ident |
| `X6.txt` | 同上 | 現行 `excluded`（有列名的）6 個 ident |

⚠ `S56.txt` 已與 leader1 的獨立量測做過集合比對（雙向差皆空、比對器有陽性對照）。
`S22.txt` **尚未**與他的清單逐條對過——詳見報告 §5。

## 凍結件（`frozen/`）—— 🔴 不要直接執行

四個 `*.py.frozen` 是**凍結件**：內容與當初逐位元組相同（sha256 由
`scripts/audit/test_836_frozen_evidence_not_runnable.py` 釘死對帳，CI 每次跑）。

### P-C：它們寫死了兩個絕對路徑

```python
ROOT = pathlib.Path("/Users/eugene/Documents/woow/AREA-odoo/woow-addons/wt-836")
OUT  = pathlib.Path("/private/tmp/claude-501/-…-wt-836/256a4fa3-…/scratchpad/836")
```

| 常數 | 出現在 | 後果 |
| --- | --- | --- |
| `ROOT` | `t1_universe.py.frozen:4`、`t1_partition.py.frozen:4`、`t1_p9_p10.py.frozen:4` | 它們**不量自己所在那棵樹**，量的是 `wt-836` 當下的內容 |
| `OUT` | `t1_partition.py.frozen:108`、`t1_p9_p10.py.frozen:5` | 寫入某個**舊 session 的 scratchpad** |

🔴 **`ROOT` 指向的 `wt-836` 是一個 worktree，它會被清掉**（段二收尾後就沒有理由留著）。
清掉之後這些檔會以另一種訊息死掉，而那個訊息跟「東西壞了」不可區分。
🔴 **`OUT` 指向的是 `/private/tmp` 底下的 session scratchpad**，同樣會被清掉，
而且**在被清掉之前它是可寫的** —— 那才是危險的視窗。

（2026-08-23 實例：有人為了覆核跑了當時還叫 `frozen/t1_p9_p10.py.frozen` 的那支，
它覆寫了舊 session 的 `p9_output.txt` / `p10_output.txt`。內容還原得回去，
**mtime 還不回去**——那兩個檔現在是 `Aug 23 09:41`，兄弟檔全是 `Aug 22 19:xx`。
repo 內 `evidence/` 的 committed 副本沒被動到。）

### P-B：逐檔實測（2026-08-23，dev-836 在 `8947aa14` ＋ 本次變更上量的）

環境：本機 `python3` ＝ CPython 3.14.6（`python3 -VV`）。指令一律
`python3 <絕對路徑>`，`cwd` 為 repo 根。⚠ **每一列都是分開跑、分開記的**，
不要把任何兩列併成一句話讀——上一版 README 就是把「大聲失敗」和「安靜成功」
併成一句，抹掉的正好是危險的那半。

**(1) 習慣路徑（舊的 `probes/*.py` 與舊的 RED 位置）——四條全部：**

| 指令 | rc | 首行輸出 |
| --- | --- | --- |
| `python3 …/probes/t1_universe.py` | 2 | `can't open file '…/probes/t1_universe.py': [Errno 2] No such file or directory` |
| `python3 …/probes/t1_partition.py` | 2 | 同上（`t1_partition.py`） |
| `python3 …/probes/t1_p9_p10.py` | 2 | 同上（`t1_p9_p10.py`） |
| `python3 scripts/audit/red_836_silent_class_drop.py` | 2 | 同上（該路徑） |

**(2) 凍結件現在的路徑，逐支：**

| 檔 | rc | 實際行為 |
| --- | --- | --- |
| `frozen/t1_universe.py.frozen` | 97 | 撞上 `frozen/ast.py` 守衛，`import ast` 就死。**⚠ 沒有守衛時它是 rc=0**——印出一份看起來完全正常、實際來自 `wt-836` 的數字（這是本目錄最危險的一支，不是最會炸的那支） |
| `frozen/t1_partition.py.frozen` | 97 | 同上。**沒有守衛時**它是 rc=1，`ValueError: too many values to unpack (expected 2, got 3)`（#836 段二把 `scan_expected()` 改成 3-tuple）——大聲，但那是**運氣**：它的寫入點在 `:109-111`，只是剛好排在 `:62` 那個 `ValueError` 後面 |
| `frozen/t1_p9_p10.py.frozen` | 97 | ⛔ 本表作者（dev-836）**沒有執行它**：它 `:63`／`:81` 會寫進上面那個 `OUT`，而那個目錄現在還在（實測 `ls` 得到 12 個檔）⇒ 執行＝二次污染別人的 scratchpad。靜態確認：第一個 import 是 `ast`（AST 取得，非 grep），守衛在 `:2` 就生效，早於 `:63`。**這一列的 rc=97 是 leader1 做的受控實驗**（見下） |
| `frozen/red_836_silent_class_drop.py.frozen` | 97 | 撞上守衛。**注意它是 transitive 觸發**：它自己不 import `ast`，是 `import unittest` 把 `ast` 拉進來的（實測 `python3 -c "import unittest,sys; print('ast' in sys.modules)"` → `True`）。**沒有守衛時**它是 rc=1，`FATAL: 找不到 …/frozen/test_collection_audit.py`（`_load_audit()` 找同目錄的被測物，而它已經不在同目錄）⇒ 這一支有兩層獨立的大聲失敗 |
| `frozen/ast.py`（守衛自己） | 97 | 印出守衛訊息後 `SystemExit(97)` |

**(3) `evidence/*.txt` 當 Python 跑（抽樣兩支）：**

| 檔 | rc | 末行 |
| --- | --- | --- |
| `evidence/S56.txt` | 1 | `NameError: name 'test_addon_removal_fail_closed_477' is not defined` |
| `evidence/p9_output.txt` | 1 | `SyntaxError: invalid character '—' (U+2014)` |

⚠ 這一列是**抽樣**，不是十個 `.txt` 全跑過。

### P-A：兩層防線，怎麼擋的

1. **改名**：凍結件一律 `*.py.frozen`，本目錄除了守衛沒有任何 `*.py`
   ⇒ 習慣性的 `python3 <檔>.py` 直接 `No such file or directory`（上表 (1)）。
2. **同名遮蔽守衛 `frozen/ast.py`**：`python3 <某檔>` 會把該檔所在目錄放進
   `sys.path[0]`，而 `ast` 不在啟動時預載（實測 `'ast' in sys.modules` → `False`）
   ⇒ 從本目錄執行任何腳本時 `import ast` 先撿到守衛，第一行 import 就 `SystemExit(97)`，
   走不到任何寫入點。擋的是「刻意去跑 `.frozen`」與「把它改名回 `.py`」。

### `t1_p9_p10.py.frozen` 那一列的受控實驗（leader1，2026-08-23）

上一版這一列是**推論**（「守衛應該擋得住它」），因為執行它就會二次污染。leader1 把它
升級成量測，做法是**先備好回滾來源再跑**：

1. 對 `OUT` 底下 `p9_output.txt` / `p10_output.txt` 照 sha256；
2. 在守衛就位的情況下**真的執行** `frozen/t1_p9_p10.py.frozen`；
3. 結果 **rc=97**，stderr 首行 `FROZEN-EVIDENCE-GUARD-836`；
4. 跑完兩個檔的 sha256 與跑前**完全相同**（守衛在寫入前就把它擋掉了）。

回滾來源是本 repo `evidence/` 底下的 committed 副本 ⇒ 這個實驗可回滾。

⚠ 可回滾的**只有內容**。mtime 回不去——上一節那件事就是這樣留下痕跡的。
所以「先照 sha256 再跑」不是萬用許可證：它讓內容可回滾，不讓**帶外痕跡**可回滾。

同一輪 leader1 還量了兩個對照：

- **陰性對照**：移走守衛後 `t1_universe.py.frozen` 跑得起來，rc=0，印出 764／675
  ——也就是上表「沒有守衛時」那一欄的獨立複驗。
- **失效模式是真的**：`PYTHONSAFEPATH=1` 確實繞過守衛，rc=0。

🔴 **第 2 層有明確失效模式**：`python3 -P` / `PYTHONSAFEPATH=1` 會關掉 `sys.path[0]` 注入
（**已由 leader1 實測，不是推測**），未來的 Python 若預載 `ast` 也一樣 ⇒ 守衛**靜默不觸發**。所以
`scripts/audit/test_836_frozen_evidence_not_runnable.py` 有一格**活體 canary**：
真的去執行 `t1_universe.py.frozen`（靜態證明無寫入點），要求它非零**而且**
輸出含守衛標記 `FROZEN-EVIDENCE-GUARD-836`。只看 rc 會假綠——CI 上 `ROOT` 那個路徑
不存在，凍結件本來就會自己非零，**與守衛有沒有觸發不可區分**。

### 要重跑這些量測怎麼辦

**不要就地改**。複製一份到別處，改掉 `ROOT` / `OUT` 再跑。
改了原件，`evidence/*.txt` 就不再是它產出的東西，而那件事**沒有任何訊號**。

### `t1_partition.py.frozen` / `t1_p9_p10.py.frozen` 的兩個機制（要看內容時先讀）

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
