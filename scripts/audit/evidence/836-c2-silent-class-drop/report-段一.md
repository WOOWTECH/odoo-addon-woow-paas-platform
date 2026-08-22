# #836 第一段交付報告（dev-836 → leader1）

- 日期：2026-08-22
- 工作目錄：`/Users/eugene/Documents/woow/AREA-odoo/woow-addons/wt-836`
- 分支：`fix/836-c2-zero-count-excluded`，基底 `16b5fc04ec7e3adcf3a0b02602b70213761f9a11`
- 本段唯一 commit：`6a9209e9`（RED），工作區 porcelain 空
- ⛔ 本段**沒有動任何修法**。#836 的「靜默丟棄的 class 怎麼列」待 Eugene 裁決。
- ⛔ 本段**沒有貼任何留言到 #836**（現況仍只有 leader1 的 comment 8731，已現場查證）。
- 證據檔（同目錄）：`evidence-t1-universe.txt`／`evidence-t1-partition.txt`／
  `evidence-t1-p9p10.txt`／`evidence-t2-red.txt`／`c2_baseline_now.txt`／
  `p9_output.txt`／`p10_output.txt`／`S56.txt`／`S22.txt`／`X6.txt`／`predictions.md`
- 可重跑腳本：`t1_universe.py`／`t1_partition.py`／`t1_p9_p10.py`

---

## 0. 先講意外：C3 是**遲到的半套** backstop（issue body 的 C2 主張成立）

### 口徑（leader1 2026-08-22 釘定，之後任何文字一律照這版）

這是**補充**，不是第三個翻案。issue body 那句逐字是：

> 而 C2 的雙向差集**不會有任何反應**——baseline 沒有它、預測也沒有它，兩邊一致 ⇒ 報 PASS。

**它的主張綁在 C2 上**，而我的實測正好**證實**了它（C2 那條路徑無反應、C0/C4/C5 綠）。
我發現的是**另一支 gate**（C3、另一個 workflow、另一個觸發時機）構成一個遲到的 backstop。

- ✅ 正確寫法：「body 的 C2 主張成立；但另有一支 C3 在 merge 後會撿到，代價是指錯修法」
- ❌ 錯誤寫法：「body 講錯了」——不成立，而且會讓讀者以為前兩個翻案的可信度也要打折

⚠ 我上一版口頭轉述時寫成「issue body 主張這個縫在系統層面完全沒有訊號」，
**那是我把 body 的範圍講寬了**，已由 leader1 逐字核對 body 後更正。本節即更正後版本。

### 量測

RED 情境餵一份「洩漏的測試真的跑了」的合成 log 給四支 log 判準：
判別式是 `check_executed()` 的雙向差集（那個 class 在執行清單有、在 baseline 沒有）；
陰性對照是同一份 log 下 C0/C4/C5 不動；陽性對照是 `check_baseline()` 三個方向
（gone／added／shrunk）餵它該抓的差異都會紅，證明尺本身沒壞。

```
C0 rc=0   C4 rc=0   C5 rc=0
C3 rc=1   C3 FAIL — 以下 2 項在 實際執行清單 有、baseline 沒有（新增測試？還是 baseline 被刪行？）
```

**C3 紅了，而且不是誤紅是真紅**——那個 class 真的在執行清單裡、真的不在 baseline 裡。
所以我沒有停下來，判斷是「情境是對的，要收窄的是嚴重性論證的範圍」。

### 這個發現真正的價值（三點的組合，非「原來有訊號所以沒那麼嚴重」）

🔴 **(1) 粒度不同 ⇒ C3 只擋得住一半——它不是 C2 這個縫的完整 backstop。**
C3 是 **class 粒度**（`check_executed()` 呼叫 `extract_executed(log_path, "class")`）。
它擋「整個 class 消失」，擋不住「class 還在但條數被少算」：若判準 bug 把某個**仍留在
baseline** 的 class 從 3 條算成 1 條，C2（baseline 已重產）與 C3（只比 class 在不在）
**兩邊都綠**。
⇒ 這一點直接決定「有 C3 在，是不是就不用管 C2 這格」的答案是**不是**。

**(2) PR 期間全靜默。**

| gate | 觸發 | 對這個縫的反應 |
| --- | --- | --- |
| C2（`test-collection-audit.yml`） | `on: pull_request` —— **每張 PR** | **靜默 PASS**，一個字都不提 |
| C3（`odoo-tests.yml`） | `on: push develop` ＋ 每日排程，**刻意不掛 pull_request** | FAIL 並列名 |

⇒ 縫在整個 PR 生命週期沒有訊號——那正是唯一能擋住壞 merge 的時間點。

**(3) merge 後才紅，而且指錯修法。**
C3 撿到時印的是「新增測試？還是 baseline 被刪行？」——**兩個猜測都不對**。
那個紅燈的診斷成本落在下一個人身上，而且會把他導向錯的方向。

### 處置

- 我判定它**會動待裁天平**（不改甲乙丙丁的成本或可行性，改的是「這個縫損害多大」那一邊，
  相對抬高丙／丁），所以走回報路徑，**沒有自行貼成 issue 留言**。
- issue-leader 裁定**要轉 Eugene**；貼的方式是**一則完整留言**
  （8731 翻案 ＋ C3 補充 ＋ 裁示 ＋ 選定方案），**等裁示下來一起貼**。
- 現況：#836 仍只有 comment 8731（已現場查證）。

---

## 1. 任務一：範圍獨立確認 —— 10 個預測全中

### 做法（不重跑 leader1 的配方）

leader1 量的是**基數**（737／653／6，56 靠打樁、22 靠守恆＋複刻）。
我改用兩個他沒用到的觀測：

1. **磁碟上那份 committed baseline 檔**（`git show HEAD:scripts/audit/baselines/test-collection.txt`）
   —— 他用的是 `scan_expected()` 的回傳值，沒碰過這個檔。它是一次過去的
   `--update-baseline` 凍在 git 裡的獨立見證。
2. **把「守恆」升級成「同一性分割」** —— 基數守恆對「重複計數互相抵銷」是盲的。
   `S = U − E − X` **純靠集合減法**得出（完全不碰那個迴圈的分支邏輯），
   再驗 `E ⊆ U`、`X ⊆ U`、`E ∩ X = ∅`。

56／22 的切分同樣不重跑打樁：**先由減法得到 S，再預測它按性質切成 56+22**，
而不是用性質去定義集合（後者恆真、沒有可證偽性）。

預測在跑之前先寫進 `predictions.md` 釘死。

### 避開 leader1 踩過的坑

- `_expected_methods` 全 repo **只有 1 個呼叫點**（`scripts/audit/test_collection_audit.py:298`）
- `_test_own_test_methods` 有 **6 個**（audit `:230`／`:235`／`:289` ＋
  repo-lint `:6754`／`:7031`／`:7084`；定義在 `repo-lint.py:6685`）

⇒ 不做全域打樁，改用 **AST node 物件 identity 當 key**，呼叫點多寡與歸屬無關。

### leader1 警告的「靜默拿到 0」陷阱：實測為真

`scan_expected()` 一次執行裡 `_test_tree_facts` 被呼叫 **2 次**
（`check_test_module_collection` 一次、`scan_expected` 自己一次）。
兩次會 parse 出**不同的 node 物件**，node identity 不會相等。
處置：memoize 成同一顆 facts，並加兩格自我檢查（G1／G2，見下）。

### 實際輸出

`evidence-t1-universe.txt`：
```
files scanned      : 249
P1  |U|            : 737
P2  baseline 資料行 : 653  |E| keys: 653
     重複 key       : 0
```

`evidence-t1-partition.txt`：
```
G2  node-map 命中數 : 737  |U|(my walker): 737
G2  map ident 集 == U: True
G1  _expected_methods 被呼叫次數: 709  全部 id 都在我的 map 裡: True
    _test_tree_facts 被呼叫次數: 2

P4  |X| excluded    : 6  (list len: 6 )
P3  E.keys == expected.keys : True  |expected|: 653
P3b E 的數值也逐項相同      : True
P5  E ⊆ U: True  X ⊆ U: True  E ∩ X = ∅: True
P6  |S| = |U−E−X|   : 78
P7  tagged+standard (= `if n:` 那格) : 56
P7  非 tagged        (= own 為空那格) : 22
P7  既非上述兩者（不該存在）          : 0 []
```

`c2_baseline_now.txt`（C2 現況，`--check-baseline`，EXIT=0）：
```
C2 PASS：baseline 與 目前原始碼 完全一致（653 項）
C2 註記：以下 6 個 class 被排除在 baseline 之外（列名，不靜默丟棄）：
"  ~ " 行: 6   "      因為：" 行: 6
```

### 真正的預測（leader1 沒產出過的執行期觀測）

`evidence-t1-p9p10.txt`：
```
[P9] rc=1
[P9] '  + ' 行 = 56   '  - ' 行 = 0   '  ! ' 行 = 0
[P9] 註記 header: C2 註記：以下 6 個 class 被排除在 baseline 之外（列名，不靜默丟棄）：   '  ~ ' 行 = 6
[P9] '+' ident 集 == S56 逐字相同: True
[P9] 差異: []

[P10] rc=0
[P10] '  + ' 行 = 0   '  - ' 行 = 0   '  ! ' 行 = 0
[P10] 註記 header: C2 註記：以下 28 個 class 被排除在 baseline 之外（列名，不靜默丟棄）：   '  ~ ' 行 = 28
[P10] 假回傳命中次數: 22  命中的呼叫點行號: [289]
```

- **P9**：把 `_expected_methods`（唯一呼叫點）換成「只對 S56 成員回 1 元素假集」
  → C2 從 PASS 翻 FAIL，`+` 恰 **56** 行，ident 集與 S56 **逐字相同**（對稱差 `[]`），註記仍 6。
- **P10**：對 S22 的 node 回非空（用 frame 判呼叫點 ＋ node identity，只命中 `:289`）
  → 註記 **6 → 28**，雙向差集仍 PASS，假回傳命中 **22** 次。

### 沒預測到但掉出來的佐證

`_expected_methods` 被呼叫 **709** 次 = 653 + 56。
到得了那一行的，就是「進 baseline 的」加「在那格被丟掉的」。

### 結論

leader1 comment 8731 的數字全部成立：**737 ／ 653 ／ 6 ／ 78 = 56 + 22**。

---

## 2. 任務二：RED（commit `6a9209e9`）

檔案：`scripts/audit/red_836_silent_class_drop.py`，**exit 1**（如設計為紅）。

### 觸發器不是注入假 bug，是現存判準本來就有的盲點

- `repo-lint.py:6685 _test_own_test_methods()` 只看 class body **頂層**的 FunctionDef
- Odoo loader 看的是執行後的 class 物件：`odoo/tests/loader.py:30`
  `test_case_class.__dict__.items()` ＋ `:43` `method_name.startswith('test')`
  （讀本機 `/Users/eugene/Documents/woow/AREA-odoo/odoo-18.0/` 逐字確認）

⇒ `def test*` 包在 class body 的 `if`／`try` 裡：靜態算 0、執行期照跑。
整支 RED **零 monkeypatch**。用兩個不同 statement 型別（`if` 與 `try`）而非一個，
避免被讀成某種特殊語法的怪癖。

⚠ 這個盲點本身**不是** #836 要修的東西，它只是最省事的觸發器：
任何讓條數變 0 的判準 bug 都會走到同一個出口。

🔴 但它是**獨立於 #836 的判準缺陷**（#836 修「怎麼**列**」，修不到「怎麼**算**」）。
leader1 已回報 issue-leader 決定要不要另外開單。**我不動它，也不寫進段二的修法。**
現樹的苦主盤點見 §5（巢狀 statement 形態 0 個，但只涵蓋那個形態）。

### 實際輸出（`evidence-t2-red.txt`）

```
(A) 執行期對照 —— Odoo loader 篩子④ 會從這些 class 產生哪些測試？
    TestDroppedByIf    → ['test_leaked_if']
    TestDroppedByTry   → ['test_leaked_try']

(B) RED —— C2 對這棵樹說什麼？
    scan_expected() 預期清單 : ['test_leak.TestVisible']
    scan_expected() 排除清單 : []
    check_baseline() 回傳碼  : 0（0 ＝ PASS）
    | C2 PASS：baseline 與 目前原始碼 完全一致（1 項）
    輸出有沒有提到 TestDroppedByIf    : **沒有**
    輸出有沒有提到 TestDroppedByTry   : **沒有**
    ⇒ C2 報 PASS，而且一個字都沒提那兩個 class。**這個 PASS 就是紅燈。**

(C) 陽性對照 —— 同一支 check_baseline，餵它三種該抓到的差異
    baseline 多一項（該報 gone）        rc=1  '-' 行=1
    baseline 少一項（該報 added）       rc=1  '+' 行=1
    條數變少（該報 shrunk）              rc=1  '!' 行=1

(D) 旁證（不計分）—— 餵一份「洩漏的測試真的跑了」的 log 給其餘判準
    C0 哨兵        rc=0  C0 PASS：1 個完整性哨兵全部出現 ⇒ 收集範圍完整
    C3 執行清單      rc=1  C3 FAIL — 以下 2 項在 實際執行清單 有、baseline 沒有（新增測試？還是 baseline 被刪行？）：
    C4 skip      rc=0  skipped 總數=0
    C5 失敗集合      rc=0  C5 PASS：失敗集合與 baseline 相同（0 條已知紅燈，零新增）

🔴 RED（2 格）—— 這是 #836 落地前的預期狀態
EXIT=1
```

### 設計上的三個取捨

1. **(A) 這格為什麼存在**：整支 RED 的前提是「這個 class 真的有測試方法」。
   前提若只用嘴巴宣告，後面每一格都不算數 ⇒ 用 loader 的同一條判準把它**執行**出來。
   (A) 若失敗直接 `return 2`（樣本寫壞了，不是被測物有事）。
2. **RED 斷言刻意寫得比任何一個候選修法都窄**：只斷言「有 `def test*` 卻算 0 的 class
   必須出現在 C2 輸出某處」，只涉及 `if n:` 那一格。甲乙丙丁四個方案（含「只修一格」）
   都會讓它轉綠 ⇒ **不預判裁決**。22 那格只在 P10 示範，沒寫進斷言。
3. **檔名刻意不是 `test_*.py`**：`repo-lint.yml` 的「scripts/ 自我測試」步驟會 glob
   `scripts/audit/test_*.py` 並逐一 `python3 <檔>`，回非零就把 repo-lint 整條弄紅。
   一支預期為紅的檔進那個 glob 等於把 CI 綁成常態紅。

### 不誤紅既有判準（repo 層）

```
python3 scripts/audit/test_collection_audit.py --self-test   → SELF-TEST PASS，EXIT=0
python3 scripts/audit/test_collection_audit.py --all         → C2 PASS 653 項／註記 6，EXIT=0
python3 scripts/audit/repo-lint.py --all                     → 全部通過（32 條），EXIT=0
git status --porcelain                                       → 只多 red_836_silent_class_drop.py
```

---

## 3. 任務三：`--update-baseline` 連帶影響（#838）—— 五個檔零差異

先用 `git show HEAD:` 釘住基準（工作區與 HEAD 本來就一致：五檔 sha 相同）。

```
python3 scripts/audit/repo-lint.py --update-baseline           EXIT=0
  → baseline 已更新：controller-sudo.txt（15 檔）、data-noupdate.txt（21 檔）、
    seed-topology.txt（6 個計數）、repo-lint-registry.txt（69 個名字）
  → mtime 四檔全變（19:19），git status **空**

python3 scripts/audit/test_collection_audit.py --update-baseline  EXIT=0
  → test-collection.txt mtime 變（19:19:48），git status **空**

git diff --numstat -- scripts/audit/baselines/   → 空（零差異）
逐檔 sha 對 HEAD：controller-sudo / data-noupdate / repo-lint-registry /
                  seed-topology / test-collection  → 全部 相同=YES
```

**沒有任何一檔減少**（也沒有任何一檔增加）。

### 「零差異」配了陽性對照

否則它與「writer 根本沒寫」不可區分：

```
往 controller-sudo.txt 塞一行假的  → 注入後行數: 18，git diff --numstat: 1  0
重跑 repo-lint.py --update-baseline → 重跑後行數: 17，git status: ''（乾淨）
```

⇒ writer 真的在寫**內容**，不是只 touch。

### 兩點順帶釐清

1. 🔴 **`--update-baseline` 是同名旗標、兩支 script 行為不同**（2026-08-22 leader1 更正，
   工單原文的「一次重產全部四個」範圍寫太寬，只適用 `repo-lint.py`）：

   | script | 這支重產什麼 | 逐檔驗「只增不減」防的是 |
   | --- | --- | --- |
   | `scripts/audit/repo-lint.py --update-baseline` | `baselines/` 四檔（controller-sudo／data-noupdate／repo-lint-registry／seed-topology） | **連帶重產把別人合法的調降靜默帶進你的 commit** |
   | `scripts/audit/test_collection_audit.py --update-baseline` | **只有** `baselines/test-collection.txt` 一檔 | 沒有連帶問題；防的是**你自己這一檔被誤減** |

   我的 mtime 量測與這條相符（也是 leader1 用來獨立覆核 dev-798 那份 7 檔 sha256 的第二條證據）：
   跑完之後 `baselines/` 分成兩組——五個檔在同一分鐘被重寫（repo-lint 四檔 ＋
   `test-collection.txt`），而 `test-collection-known-failures.txt` 與
   `test-collection-sentinels.txt` 停在更早的 `18:40:20` 沒被碰。
   五 ＝ 四 ＋ 一，與「audit 只寫一檔」一致。

   （`test_collection_audit.py` 的 `BASELINE_HEADER` 自己就寫了
   「⚠ **不是** repo-lint.py --update-baseline —— 本檔不歸 repo-lint 的 baseline 機制管」。）

   ⚠ 段二動 baseline 時：**先認自己呼叫的是哪一支**，再套對應的檢查與理由。
2. **由此推導（未實測，因為本段⛔不動修法）**：#836 四個候選方案改的都是
   `excluded` ／ 輸出形狀，而 `write_baseline(path, expected)` 只寫 `expected`
   ⇒ 四個方案**都不會改動 `test-collection.txt` 的內容**。
   除非有人選擇把 0 條 class 以 `0\t<ident>` 寫進 baseline，
   但那會讓 C3 立刻對它報「baseline 有、執行清單沒有」。

⚠ **零差異的結論綁在 base `16b5fc04`。develop 前進後要重量。**

---

## 4. 主張對證據

| Claim | Command run | Key output line | Confidence |
| --- | --- | --- | --- |
| 範圍內 class 總數 737 | `python3 t1_universe.py`（自寫 plain-ast walker，249 檔） | `P1  \|U\| : 737` | verified |
| baseline 檔 653 筆、無重複 key | 同上（`git show HEAD:…/test-collection.txt`） | `P2 baseline 資料行 : 653  \|E\| keys: 653`／`重複 key : 0` | verified |
| baseline 檔與 `scan_expected()` 逐項相同 | `python3 t1_partition.py` | `P3 E.keys == expected.keys : True`／`P3b E 的數值也逐項相同 : True` | verified |
| excluded 6、E/X ⊆ U 且不交 | 同上 | `P4 \|X\| excluded : 6`；`P5 E ⊆ U: True  X ⊆ U: True  E ∩ X = ∅: True` | verified |
| 靜默總數 78（同一性分割，非僅基數） | 同上 | `P6 \|S\| = \|U−E−X\| : 78` | verified |
| 78 = 56 + 22，零例外 | 同上 | `P7 tagged+standard : 56`／`非 tagged : 22`／`既非上述兩者 : 0 []` | verified |
| node-identity key 機制沒壞（G1/G2） | 同上 | `G2 node-map 命中數 : 737 … map ident 集 == U: True`；`G1 … 全部 id 都在我的 map 裡: True` | verified |
| `_test_tree_facts` 一次執行被叫 2 次（leader1 警告的陷阱為真） | 同上 | `_test_tree_facts 被呼叫次數: 2` | verified |
| `_expected_methods` 唯一呼叫點／`_test_own_test_methods` 6 個 | `grep -rn '_expected_methods' scripts/`；`grep -rn '_test_own_test_methods' scripts/` | 前者 `:298` 一處；後者 `:230 :235 :289` ＋ repo-lint `:6754 :7031 :7084` | verified |
| S56 驅動真 C2 → 恰 56 個 `+`、ident 逐字相同 | `python3 t1_p9_p10.py` | `[P9] '  + ' 行 = 56`；`[P9] '+' ident 集 == S56 逐字相同: True`；`差異: []` | verified |
| S22 驅動真 C2 → 註記 6→28、差集仍 PASS | 同上 | `[P10] 註記 header：…以下 28 個 class…`；`[P10] rc=0`；`命中的呼叫點行號: [289]` | verified |
| C2 現況 PASS 653／註記 6 | `python3 scripts/audit/test_collection_audit.py --check-baseline` | `C2 PASS：…（653 項）`；`  ~ ` 行 6、`      因為：` 行 6 | verified |
| 洩漏的 class 真的有測試方法（loader 篩子④） | `python3 scripts/audit/red_836_silent_class_drop.py` | `TestDroppedByIf → ['test_leaked_if']` | verified |
| loader 判準是 `cls.__dict__` ＋ `startswith('test')` | `sed -n '40,50p' …/odoo-18.0/odoo/tests/loader.py` | `test_cases = test_case_class.__dict__.items()`；`if not method_name.startswith('test')` | verified |
| RED：C2 報 PASS 且輸出不提那兩個 class | `python3 scripts/audit/red_836_silent_class_drop.py` | `check_baseline() 回傳碼 : 0`；`輸出有沒有提到 TestDroppedByIf : **沒有**` | verified |
| 陽性對照三方向都紅 | 同上 (C) 格 | `rc=1 '-' 行=1`／`rc=1 '+' 行=1`／`rc=1 '!' 行=1` | verified |
| C0/C4/C5 不誤紅、C3 真紅 | 同上 (D) 格 | `C0 rc=0` `C4 rc=0` `C5 rc=0`；`C3 rc=1 … 在 實際執行清單 有、baseline 沒有` | verified |
| C2 掛 `pull_request`、C3 不掛 | `sed -n '1,60p' .gitea/workflows/test-collection-audit.yml`／`… odoo-tests.yml` | `on:` ＋ `pull_request:` ／ `刻意**不掛 pull_request**` ＋ `push: branches: - develop` | verified |
| C3 是 class 粒度 | 讀 `scripts/audit/test_collection_audit.py:600-608` | `actual = extract_executed(log_path, "class")` | verified |
| 加了 RED 檔後既有 gate 全綠 | `--self-test`／`--all`／`repo-lint.py --all` | `SELF-TEST PASS…`；`audit --all EXIT=0`；`repo-lint：全部通過`／`EXIT=0` | verified |
| 五個 baseline 檔 `--update-baseline` 後零差異、無減少 | `repo-lint.py --update-baseline` ＋ `test_collection_audit.py --update-baseline` | `git status --porcelain` 空；逐檔 `相同=YES`×5；兩者 `EXIT=0` | verified |
| 「零差異」不是 writer 沒寫 | 注入假行後重跑 | `注入後行數: 18 … 重跑後行數: 17  git status: ''` | verified |
| 兩套 baseline 機制互不相干 | `stat -f "%Sm %N" …/baselines/*.txt` | repo-lint 跑完 `test-collection*.txt` 仍是 `18:40:20` | verified |
| RED 已 commit、工作區乾淨 | `git log --oneline -2`／`git status --porcelain` | `6a9209e9 test: 為 #836 加一支 RED…`；porcelain 空 | verified |
| 本段沒有貼任何留言到 #836 | `tea api -X GET /repos/odoo-addons/woow_paas_platform/issues/836/comments` | `留言數: 1`／`id 8731 by eugene` | verified |
| S56 名單與 leader1 獨立量測**逐一相同** | leader1 拿 `836-measurements.json` 的 `path2_idents` 對我的 `S56.txt` 做集合比對 | `集合完全相同: True`／`只在我這邊: 0 []`／`只在你那邊: 0 []`／`比對器陽性對照（餵一個必不同的）: False` | verified（leader1 量測） |
| 「class body 巢狀 statement 裡的 `def test*`」現樹 **0 個苦主** | leader1 掃 251 檔（`if`／`try`／`for`／`while`／`with` 內的 `def test*`） | `掃描檔數: 251   命中: 0`；陽性對照 `['test_x']` ← 掃描器確實抓得到 | verified（leader1 量測，範圍見 §5） |
| `--update-baseline` 兩支 script 行為不同（audit 只寫一檔） | `stat -f "%Sm %N" …/baselines/*.txt`（我）＋ dev-798 的 7 檔 sha256（leader1 轉述） | 跑完分兩組：五檔同分鐘重寫、`test-collection-{sentinels,known-failures}.txt` 停在 `18:40:20` | verified |
| 四個候選修法都不會改 `test-collection.txt` | —（由 `write_baseline(path, expected)` 只寫 `expected` 推導） | — | **inferred** |

---

## 5. What I did NOT verify

### 已由 leader1 的獨立量測關掉的兩項（保留在此並標明是誰量的）

- ~~56／22 的逐一名單是否與 leader1 一致~~ → **S56 已 verified**。
  leader1 用他那份 `836-measurements.json` 的 `path2_idents` 對我的 `S56.txt` 做集合比對：
  兩邊各 56、`集合完全相同: True`、雙向差都是 `0 []`，且比對器有陽性對照
  （餵一個必不同的 → `False`）。兩組**獨立推導**的量測基數與名單都一致。
  ⚠ **S22 的名單仍未做集合比對**：leader1 貼出的證據只涵蓋 S56。
  我這邊對 S22 的支撐是「由減法得出 22 個」＋ P10 打樁恰好命中 22 次且全部落在
  `:289` 那一格——那證明**基數與所屬分支**，不證明**與 leader1 的 22 個是同一批**。
  名單在 `S22.txt`，要對隨時可給。
- ~~`if`／`try` 包起來的 `def test*` 現樹有沒有真實苦主~~ → **已掃，0 個**。
  leader1 掃 251 檔、命中 0，且有陽性對照（自造 class body 內 `if` 包 `test_x`
  → 掃描器回 `['test_x']`，證明掃得到）。
  🔴 **這個 0 有明確範圍，不可寫成「這個盲點沒有影響」**：掃的是**巢狀 statement 形態**
  （class body 的 `if`／`try`／`for`／`while`／`with` 裡的 `def test*`）。
  其他讓靜態算 0 而執行期會跑的途徑**沒掃**——decorator／metaclass 動態塞、
  `setattr`、`exec` 之類。
  ⇒ 正確結論是「**這個形態**現樹沒有苦主 ⇒ 該盲點目前是**潛伏**而非正在發作」，
  不是「不存在」。

### 仍然未驗

- **四個候選修法不會動 baseline 內容** —— 純推導（`write_baseline(path, expected)`
  只寫 `expected`）。沒實測，因為本段⛔不動修法。
- **C3 在真實 CI 對這個縫的行為** —— 我用的是自己造的合成 log，沒有真實 Odoo 全套 log。
  C3 的判準邏輯已驗，但「真實 log 的 Detailed Tests Report 會不會如我構造的那樣包含
  該 class」未實測。
- **S22 的 22 個名字與 leader1 的是否同一批** —— 見上。
- **`--update-baseline` 在別的 base 上的行為** —— 量的是 `16b5fc04` 這一顆。
  零差異的結論綁這個 base，develop 前進後要重量。
- **C3 那個「新增測試？還是 baseline 被刪行？」訊息在真實情境下會不會誤導維護者** ——
  我只讀了那行文字，沒有觀察過真人被它誤導。這是文字判讀，不是量測。
- **`--update-baseline` 兩支 script 差異的原始證據（dev-798 的 7 檔 sha256）** ——
  我沒讀那份量測檔，只有 leader1 的轉述 ＋ 我自己 mtime 分兩組的旁證。
- **openpets 通知** —— `Could not connect to /tmp/openpets-501.sock`，daemon 沒跑。
  非交付物，依 leader1 指示不再處理。

---

## 6. 邊界（本段刻意沒做的事）

- **不動修法**。「靜默丟棄的 class 怎麼列」是輸出契約取捨，待 Eugene 裁示（甲乙丙丁四案）。
- **不貼 #836**。裁示下來後由 leader1 以**一則完整留言**貼出
  （8731 翻案 ＋ C3 補充 ＋ 裁示 ＋ 選定方案）。現況 #836 仍只有 comment 8731。
- **不碰 RED 用的那個觸發器**。`_test_own_test_methods()` 只看 class body 頂層 FunctionDef
  而 loader 看執行後的 `cls.__dict__`——這是**獨立於 #836 的判準缺陷**
  （#836 修「怎麼**列**」，修不到「怎麼**算**」）。leader1 已回報 issue-leader
  決定要不要開單；**不寫進段二的修法**。
- **不動 manifest 版號**。
- 段二動 baseline 前：**先認自己呼叫的是哪一支 script**（§3 那張表）。
