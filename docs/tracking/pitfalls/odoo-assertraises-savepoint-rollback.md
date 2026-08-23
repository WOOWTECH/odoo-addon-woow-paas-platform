# Pitfall：`assertRaises` 把 with 區塊包進 savepoint —— 例外之後的 DB 斷言，量的是一個被回捲的世界

> **一句話**：Odoo 的 `BaseCase.assertRaises` 在進入 with 區塊**之前**開一顆 flushing
> savepoint，例外一穿出來就 `ROLLBACK TO SAVEPOINT` ＋ `cr.clear()`。回捲的邊界**恰好
> 是 with 區塊本身** ⇒ 所有「例外之後這一列 DB 應該長怎樣」的斷言，讀到的都是區塊
> **之前**的值。這會以兩個**方向相反**的方式害人：回捲後的值剛好等於期望值 ⇒ **假綠**；
> 修法自己寫進 DB 的東西也在區塊內 ⇒ 那條測試**永遠不可能綠**，而它紅的訊息看起來
> 完全合理（「值沒寫進去」），會把人騙去改一個沒有錯的實作。
>
> **來源**：#822（2026-08-22）／PR #851（已併入 develop）。修法 commit `a1441764`
> （`assertRaises` → 裸 try/except）。機制敘述的**更正**是 `751e6255`（#822 本體，
> 19:53:27）與 `9f37de75`（#732，19:49:07）。
> ⚠ 廣播到 #732 的第一顆 commit `68966a50`（19:31:02）把**錯的**機制敘述
> （`cr.savepoint(flush=False)`）原封帶了過去，18 分鐘後才被 `9f37de75` 蓋掉——
> 引用時別停在 `68966a50`。
>
> **守線**：pattern-guard `test-discipline` class（`^src/tests/.*\.py$`）。
> repo-lint gate **已評估否決**，理由見文末「為什麼機械化狀態不是 repo-lint gate」。

## Symptom

寫了一組「例外路徑的補償行為」測試，形狀是：

```python
with self.assertRaises(AccessError):
    record.action_something()          # ← 內部寫了 DB，然後丟例外
self.assertEqual(record_reread().state, 'pending')      # ← 量的是回捲後的世界
self.assertIn('orphan', record_reread().error_message)  # ← 同上
```

它會用兩種**方向相反**的方式壞掉，而且兩種的表面症狀都不像是這個原因：

- **(i) 假綠**：測試通過，但它什麼都沒驗到。因為回捲後那一列退回區塊**之前**的值，
  而你期望的剛好就是那個值——例如「補償成功後應該回到 `pending`」，而 `pending` 正是
  該 model `state` 欄位的 `default=`，也就是 fixture 建那一列時的初始值。
  ⇒ RED 階段跑出來是**綠的**，於是「守線已成立」被寫進報告。
- **(ii) 結構上不可能綠**：測試恆紅。修法**已經正確地**把 `state` / `error_message`
  寫進去了，但那次 write 也在區塊內 ⇒ 一起被回捲 ⇒ 斷言永遠讀不到。
  訊息長這樣：

  ```
  AssertionError: 'pending' == 'pending'                     # 「不得被推回 pending」——它根本沒被改過
  AssertionError: 'unreferenced-tunnel' not found in ''      # error_message 是空的
  ```

  —— 讀起來就是「你的修法沒把值寫進去」。**這句話是假的，而它完全沒有辦法從訊息
  本身分辨出來**，所以下一步會去改一個沒有錯的實作（把 write 挪位置、加 flush、
  改成 sudo…），越改越遠。

⚠ 兩者的共同前提是「斷言的那個值是**在區塊內**被寫的」。同一份測試裡對 mock 的斷言
（`client.delete_tunnel.call_count`）**不會**受影響——mock 是 Python 物件，不在交易裡。
所以一個測試方法可以「一半的斷言有效、另一半是幻覺」，而輸出看不出分界。

## 機制

🔴 **這一段請自己去讀原始碼再表述**，理由見下面「誘餌」那節——這個坑的第一版描述
（#822 初稿、`a1441764` 的 commit message 裡還留著）就是照抄一個 grep 命中而寫錯的。

以下每一條都是在**宿主 checkout** `AREA-odoo/odoo-18.0/`（`odoo/tests/common.py`
sha256 `8e3b9007…`、`odoo/sql_db.py` sha256 `9a815873…`）上逐行讀出來的。

### 1. `assertRaises` 開的是 flushing savepoint（`flush=True`，預設值）

`odoo/tests/common.py::BaseCase._assertRaises` 的第一件事（`# ←` 是本文的註記，原碼沒有；
其餘逐字）：

```python
with ExitStack() as init:
    if hasattr(self, 'env'):
        init.enter_context(self.env.cr.savepoint())      # ← 沒有傳參數
        if issubclass(exception, AccessError):
            # The savepoint() above calls flush(), which leaves the
            # record cache with lots of data.  This can prevent
            # access errors to be detected. In order to avoid this
            # issue, we clear the cache before proceeding.
            self.env.cr.clear()
```

`odoo/sql_db.py::BaseCursor.savepoint` 的簽名是 `def savepoint(self, flush=True)`，
`flush` 為真時回 `_FlushingSavepoint`。⇒ **`assertRaises` 走的是 `flush=True` 那一支。**

### 2. 「進場先 flush」就是回捲邊界的來源

`odoo/sql_db.py::_FlushingSavepoint`（同樣，`# ←` 是本文的註記）：

```python
class _FlushingSavepoint(Savepoint):
    def __init__(self, cr):
        cr.flush()                    # ← 先把「區塊之前」的待寫入推進 DB
        super().__init__(cr)          # ← 然後才 SAVEPOINT "<uuid>"

    def rollback(self):
        self._cr.clear()              # ← 丟掉 ORM cache 與待重算欄位
        super().rollback()            # ← ROLLBACK TO SAVEPOINT "<uuid>"

    def _close(self, rollback):
        try:
            if not rollback:
                self._cr.flush()
        except Exception:
            rollback = True
            raise
        finally:
            super()._close(rollback)  # ← rollback 後 RELEASE SAVEPOINT
```

⇒ **savepoint 標記被放在「區塊之前的寫入已經落地」之後**。這就是為什麼回捲邊界
不多不少剛好是 with 區塊：區塊之前寫的活下來，區塊之內寫的全部消失。

`cr.clear()` 不只是 SQL 層的事。`odoo/sql_db.py::BaseCursor.clear` 做兩件事：先轉呼
`odoo/api.py::Transaction.clear`（它的內容就是 `self.cache.clear()` ＋
`self.tocompute.clear()`），**然後自己再**做一次 `self.precommit.clear()`——後面這步
不在 `Transaction.clear` 裡面。⇒ 連「還沒 flush、只存在於 cache／towrite 的值」與
排隊中的 precommit hook 都一起沒了，所以事後補一次 `flush_all()` 也救不回來
（`flush_all` 要靠 `cache.get_dirty_fields()` 才知道有什麼要寫，而 dirty 標記剛剛被
`Cache.clear()` 一起清掉了）。

### 3. 回捲發生在 unittest 判定例外型別**之前**

`_assertRaises` 用 `inner.push(init.pop_all())` 把 savepoint 的 `__exit__` **搬到**
`super().assertRaises(...)` 之後註冊，而 `ExitStack` 是 LIFO 退場 ⇒ savepoint 先回捲，
unittest 的 `_AssertRaisesContext.__exit__` 後判型別。

實測（復刻同一套 `ExitStack` 交接跑出來的退場順序）：

```
--- 丟出「符合」的例外 ---            --- 丟出「不符合」的例外（測試會紅） ---
savepoint.__enter__                   savepoint.__enter__
assertRaises.__enter__                assertRaises.__enter__
body: write to DB                     body: write to DB
savepoint.__exit__ rollback=True      savepoint.__exit__ rollback=True
assertRaises.__exit__ (exc=ValueError) assertRaises.__exit__ (exc=KeyError)
```

⇒ **例外型別對不對，跟回不回捲無關**。連「丟錯例外、測試紅了」的那一次，區塊內的
寫入也一樣不見了——所以想靠「先讓它紅一次看看 DB 裡有什麼」來 debug 也量不到。

### 4. `AccessError` 路徑會**多做一次** `cr.clear()`

見 §1 引的那段：進場那次 flush 會把大量資料留在 record cache 裡，可能讓本該發生的
AccessError 檢查命中快取而不發生，所以 `issubclass(exception, AccessError)` 時會**在
savepoint 之後、區塊之前**再 `self.env.cr.clear()` 一次。

這是**額外**的一次，跟 §2 回捲時 `_FlushingSavepoint.rollback()` 裡那次是兩回事：

| 時機 | 誰做的 | 條件 |
| --- | --- | --- |
| 進入 with 區塊前 | `BaseCase._assertRaises` 自己 | 只有 `AccessError`（及其子類） |
| 例外穿出、回捲時 | `_FlushingSavepoint.rollback` | 所有例外型別 |

⇒ 測 `AccessError` 時，區塊**開始前**你的 ORM cache 就已經是空的了。若測試在
`with` 之前先用 su 身分讀過某個欄位打算「等一下比對」，那個快取在這裡就被清掉——
與欄位遮罩相關的假綠（見 [sudo-getter-acl-cache-priming-false-green](sudo-getter-acl-cache-priming-false-green.md)）
在 `AccessError` 這一支的行為因此和其他例外型別不同。

## 正解

「例外之後 DB 長怎樣」的斷言，一律改成裸 `try` / `except`，並在**沒有丟例外**的那一路
（落到 `try` 之後，或寫成 `else:` 分支，兩者等價）用 `self.fail(...)` 點名前提不成立：

```python
def _provision_expect_access_error(self):
    """以租戶身分呼叫 action_provision，要求它丟 AccessError，回傳該例外。"""
    try:
        self._as_tenant().action_provision()
    except AccessError as exc:
        return exc
    self.fail('前提不成立：預期 tunnel_token 欄位遮罩丟 AccessError，'
              '但 action_provision 正常返回了 ⇒ 先查身分／ACL，'
              '本條的結論在這個前提下沒有意義。')
```

實例：`src/tests/test_sa_provision_orphan_tunnel_822.py:_provision_expect_access_error`。

三個不能省的細節：

1. **`self.fail(...)` 那一支不能省。** 少了它，「例外根本沒丟出來」會安靜地變成
   「例外丟了而且被接住了」——那是把 assertRaises 的假綠換一種形狀留下來。
2. **回傳例外物件**，讓呼叫端還能對訊息做斷言（等價於 `assertRaises(...) as ctx`
   的 `ctx.exception`）。
3. **`except` 只接你要的那一型**。接 `Exception` 會把「丟了別的例外」也吞掉。

## 什麼時候 `assertRaises` 仍然是對的

不要把這條讀成「本 repo 禁用 `assertRaises`」——實測 develop `d6e17a89` 上，`src/tests/`
＋ `scripts/` 的 291 個 `.py` 裡有 358 個 `with self.assertRaises(...)` 區塊，絕大多數
沒有問題。它**只在**下列兩個條件同時成立時才是錯的工具：

- 區塊內的程式碼會**寫 DB**，而且
- 區塊**之後**你要斷言那些寫入的結果。

只要斷言的對象是下面這些，`assertRaises` 完全正常：

- 例外本身（型別、訊息、`ctx.exception`）；
- mock 的呼叫紀錄（`call_count` / `call_args`）——它們不在交易裡；
- 區塊**之前**就寫好的狀態（那些在進場 flush 時已經落地，回捲碰不到它們）；
- 「區塊內什麼都沒被寫進去」這種**否定式**斷言——但要注意回捲之後它是**恆真**的
  （不管實作對不對都會通過），鑑別力是零，別把它當守線。

還有一個相反方向的用途：`assertRaises` 的 savepoint 本來就是**為了保護 cursor** 而
存在的。若被測路徑在丟例外**之前**已經送出過 SQL，PostgreSQL 的 transaction 會進入
aborted 狀態，後續任何查詢都會 `InFailedSqlTransaction`；那種情況下你**需要**一顆
savepoint。#822 不需要，是因為那條路徑的 `AccessError` 在任何 SQL 之前就丟出來了。
⇒ 改成裸 try/except 之前，先確認被測例外是不是「SQL 之後才丟」的那種；是的話，
正解是自己開一顆 `cr.savepoint()` 包住**只有會 abort 的那一段**，而不是包住整個斷言。

## 這個坑是怎麼被抓到的

值得記下來的不是結論，是**發現的路徑**——它靠的是一個「差 1」：

- #822 的 RED 階段先**逐條寫下預測**：16 條測試中應該有 **8 條**紅。
- 實際跑出來是 **7 條**紅。
- 差的那一條是 **A4**（`test_A4_reclaimed_failure_leaves_a_retryable_state`）。它期望
  補償成功之後記錄回到 `'pending'`；回捲之後那一列**剛好就是** `'pending'`（fixture
  初始值）⇒ 型 (i) 假綠。
- 追那一條時才連帶發現 **E1**（`test_E1_unreclaimed_tunnel_is_recorded_and_not_pushed_back_to_pending`）
  是型 (ii)：它要驗的 `state` / `error_message` 是補償**寫進去的**，改成 try/except 之前
  無論實作對不對都不可能綠。
- 改成裸 try/except 之後：RED = **8F / 0E of 16**，與預測集合逐條相同。

🔴 **可帶走的是那個判準**：「預測 N 條紅、實際 N-1 條紅」不是雜訊，是有東西在說謊。
如果當時只看「有紅有綠、看起來合理」，A4 會以「守線已成立」的身分留在報告裡，
E1 則會被當成實作 bug 一路改下去。

## ⚠ 誘餌：`flush=False` 真的在 `common.py` 裡，但不在這條呼叫鏈上

同一份 `odoo/tests/common.py` 裡**確實有**一顆 `self.cr.savepoint(flush=False)` ——
它在 `warmup` 裝飾器裡，跟 `assertRaises` 一點關係都沒有：

```python
@decorator
def warmup(func, *args, **kwargs):
    ...
    with contextlib.closing(self.cr.savepoint(flush=False)):
        func(*args, **kwargs)
```

`grep -n "savepoint" odoo/tests/common.py` 會同時吐出 `_assertRaises` 那行（不帶參數）
和這行（帶 `flush=False`）。挑到後者、然後據此推論「所以尚未 flush 的 towrite 被丟掉」
——那個推論**是錯的**，而且它得出的結論（假綠 ＋ 永紅）**恰好是對的**，所以沒有任何
症狀會提醒你推導壞了。#822 初版就是這樣寫的，`a1441764` 的 commit message 裡還留著
那句錯的敘述（`751e6255` 才更正，刻意不改歷史 commit）。

🔴 **而它已經傳出去過一次**：19:31 的 `68966a50` 把同一段敘述廣播進 #732 的測試檔
docstring，18 分鐘後才由 `9f37de75` 蓋掉。⇒ 結論對的錯誤推導**傳得比更正快**，因為
沒有人在下游會撞到症狀。這也是本檔開頭那句「機制請自己讀原始碼」的來由。

⚠ 更陰的是：當時還做了一個「對照實驗」——把同一次 write 挪到 with 區塊**之前**，
值就存活了。那個觀察**兩種假說都能解釋**（`flush=False` 與 `flush=True` 都預測它存活），
零鑑別力，卻讀起來很像實證。**有鑑別力的只有原始碼那一行。**

### 引用行號時：本機兩棵 Odoo 18 的行號**不一致，而且不一致的位置不固定**

同一個 `odoo/tests/common.py`，宿主 checkout 與 paas-odoo image 內那份 **sha256 不同**
（101663 bytes vs 101423 bytes），而：

| 錨點 | 宿主 `AREA-odoo/odoo-18.0/` | image `paas-odoo:20260818.0400` |
| --- | --- | --- |
| `def _assertRaises` | 472 | 472 |
| `init.enter_context(self.env.cr.savepoint())` | 476 | 476 |
| `self.env.cr.clear()`（AccessError 專屬） | 482 | 482 |
| **`self.cr.savepoint(flush=False)`（誘餌）** | **2372** | **2368** |

⇒ 「兩邊行號一樣」**不能**當成「同一份檔」的證據：這兩棵樹在你關心的那一段剛好一致、
在誘餌那一顆差 4 行。引用第三方原始碼一律用**符號指認**
（`odoo/tests/common.py::BaseCase._assertRaises`）；真要寫行號，必須同時寫明是哪一棵樹。

## 守線／為什麼機械化狀態不是 repo-lint gate

**已做的**：pattern-guard 的 `test-discipline` class（pattern `^src/tests/.*\.py$`）
docs 清單加一行指向本檔 ⇒ 編輯任何 `src/tests/*.py` 時，本 session 首次觸碰會被推播。

**repo-lint gate：評估後否決。** 理由不是「懶」，是這條判準的關鍵資訊在靜態層不存在：

> 要判斷一個斷言危不危險，必須知道**它讀的那個值是不是在 with 區塊內被寫的**。
> 而寫入發生在被測方法內部（`action_provision` → `_internal_identity(...).write(...)`），
> 離 with 區塊三層以上，**靜態 AST 不可能知道**。

實測兩種寫得出來的判準各自會長成什麼樣（develop `d6e17a89`，在 `src/tests/` ＋
`scripts/` 的 291 個 `.py` 上跑 AST；判準與數字都可原地重算——⚠ base 前進時要重跑，
這一輪從 `d9bad876` rebase 到 `d6e17a89` 檔數就從 288 變 291，只是四個命中數剛好沒動）：

| 判準 | 命中面積 | 對 #822 那 5 條測試（`a1441764` 之前的版本） |
| --- | --- | --- |
| **窄**：`with self.assertRaises(...)` 之後、**同一層語句串**裡還有 `self.assert*` / `self.fail` | 358 個區塊中 **93** 個（35 個檔） | **5 條全部漏掉** |
| **寬**：同一個 test method 內、區塊結束之後的任何 `self.assert*` / `self._assert*` | 343 個 method 中 **123** 個（45 個檔）；其中尾段還含 ORM 讀取的 32 個 | 5 條全中 |

兩個方向都必然錯：

- **假陰性**（窄判準）：`with self.assertRaises(...)` 在 #822 那五條裡是**巢在
  `with patch(MOCK_GET_CLIENT, ...)` 裡面**的，尾隨斷言落在**外面那一層**語句串
  ⇒ 「同一層」這個最直觀的 AST 寫法**一條都抓不到催生本 doc 的那個案子**（A2 / A3
  另外還多一層：斷言包在 `self._assert_no_orphan_tunnel(client)` 這個 helper 裡，
  呼叫點上連 `self.assertXxx(` 都看不到）。
- **假陽性**（寬判準）：面積跳到 123 個 method，而「區塊後有 ORM 斷言」根本不等於錯
  ——讀的是區塊**之前**就寫好的狀態時完全正確，那正是 flushing savepoint 刻意保住
  的東西。最好的反例就在同一個檔裡：**A1** 也會被這個判準命中，而 A1 是對的
  （它斷言 `create_tunnel.call_count`、例外訊息、以及「`tunnel_id` 沒落地」——最後
  那條正是上面說的恆真否定式）。**gate 分不出 A1 與 A4，而那正是唯一需要被分出來的
  那一刀。**

⇒ 一個抓不到催生它的那個案子的 gate（窄），與一個對一百多個正確用法喊錯的 gate（寬），
兩個都會讓人以為守住了。照
[odoo-object-button-return-value-is-an-action](odoo-object-button-return-value-is-an-action.md)
的先例辦：**必然大量漏抓（或大量誤報）的 gate，其假安全感比沒有 gate 更糟**。
這一格留 pattern-guard，不做 repo-lint check。

（若未來要重開這個評估：可行的方向不是「偵測危險形狀」，而是「偵測**正解形狀**是否
被繞過」——例如某個測試檔一旦宣告採用裸 try/except 慣例，就禁止它再出現
`with self.assertRaises`。那是 per-file opt-in 的白名單型 gate，判準是純語法的，
不需要知道誰寫了 DB。代價是它只保護有 opt-in 的檔案，對新檔零覆蓋。）

## 相關

- [httpcase-orm-flush-ordering-raw-sql](httpcase-orm-flush-ordering-raw-sql.md) —— 同樣是
  「ORM 的寫入時機與你以為的不一樣」，但方向相反：那邊是 buffered write **晚**落地
  蓋掉 raw SQL，這邊是區塊內的落地被**整段撤銷**。
- [sudo-getter-acl-cache-priming-false-green](sudo-getter-acl-cache-priming-false-green.md)
  —— ORM cache 讓 ACL 測試假綠；本檔 §4 的「AccessError 專屬 `cr.clear()`」正是 Odoo
  自己為了同一個問題做的補償。
- [assertlogs-string-level-shadowed-by-odoo](assertlogs-string-level-shadowed-by-odoo.md)
  —— 同一族：**unittest 的斷言 API 在 Odoo 進程裡語意被改寫**，而測試訊息看起來像
  被測 code 壞掉。
