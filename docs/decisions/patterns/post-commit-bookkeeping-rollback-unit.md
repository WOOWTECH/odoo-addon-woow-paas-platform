---
title: "提交點之後的失敗是記帳失敗，不是操作失敗（回捲單位要對齊事實邊界）"
status: accepted
date: 2026-08-03
deciders: [platform, report-leader-agent-team]
tags: [invariant, transaction, savepoint, external-side-effect, credential, odoo]
supersedes: []
related:
  - docs/decisions/patterns/reset-async-budget-ladder.md
  - docs/decisions/patterns/two-stage-remote-provision-idempotency.md
  - docs/decisions/patterns/helm-reset-values-unified-pipeline.md
---

# 提交點之後的失敗是記帳失敗，不是操作失敗

## Context

本 repo 有一整族 handler 的形狀是：**先對外部系統下達不可回捲的變更，再把結果寫進 DB**。
（helm upgrade／Secret 換發／CF tunnel 建立／遠端帳號開通……外部那一段一旦成功，Odoo 的
transaction 回捲它一點辦法也沒有。）

這族 handler 的**提交點**＝外部系統回報成功的那一刻。`reset-async-budget-ladder` 已經為
`_reset_admin_password` 定義過這個提交點（C11′ commit-on-secret：operator 回 2xx ⇒ 新密碼
已是叢集權威值、不再自動回退）。

**但只定義提交點是不夠的——它還帶著一條義務，而那條義務當時沒有被寫下來。**

#525 B3 找到的就是那個缺口。`_reset_admin_password` 在提交點之後還有三步（憑證落庫 →
stamp `state`／`last_upgraded_at`／`helm_chart_version` → 停權回收），它們**任何一步拋例外**
都會一路傳到 controller 的 `except`，回租戶一句 `{'success': False, 'error': 'Reset failed…'}`。

那句話是假的。密碼已經在叢集上生效了。而平台的憑證一律**一次性**（刻意沒有回讀端點），
所以那把已生效的密碼**就此永久遺失**，租戶還會拿已失效的舊密碼繼續重試。

RED 階段的原始輸出把這個形狀釘死了（注入點在憑證落庫之後）：

```
AssertionError: None != 'mY3hlrbSCx_VdPuImP_HaG9cmKC0ilyR'
  : 交付給租戶的明文必須等於落庫（＝叢集）那把 …
    （回 {'success': False, 'error': 'Reset failed. Check operator logs for details.'}）
```

右值＝已經落庫（也就是叢集上生效）的新密碼；左值 `None`＝回應裡什麼都沒有。

**這是第二次了**（#461 定義提交點、#525 B3 補上它的義務），所以寫成 pattern。

## Decision

### 規則一：提交點之後的每一步都是「記帳」，不是「這次操作成功了沒」的判準

外部變更已經生效 ⇒ 操作**已經成功**。之後的落庫、stamp、清理都只是把這個既成事實記到平台這側。
記帳失敗要記錄、要能被維運看見，但**不得**翻轉回應語意。具體三條：

1. 記帳段的例外不外拋（`OperationalError` 除外，見下）。
2. 回應仍回 `success: True`，並且**必須交付**那些「只有這一次能交付」的產物（一次性明文憑證、
   一次性下載連結……）。
3. `_logger.exception` 留下明確可 grep 的痕跡，訊息要寫清楚「外部已生效、平台這側可能不同步」。

**`OperationalError` 必須往外拋**（#518）：並發／連線類 DB 例外（`SerializationFailure` 40001 等）
被吞掉的話 Odoo 的 `retrying()` 永遠看不到、不會以新 snapshot 重試；而且此刻 PG 交易已 aborted，
硬回一個 result 只會讓後續 flush 再炸。重播整個 handler 對本族 handler 是安全的——它會重跑一次
外部變更（再鑄一把新憑證重新下發），結果仍然一致。

### 規則二：回捲單位要對齊「事實邊界」，不是對齊「程式碼區塊」

記帳段內部往往不只一件事，而它們**與外部事實的關係不同**：

| 記帳項 | 與外部事實的關係 | 該不該一起回捲 |
| --- | --- | --- |
| 憑證落庫（DB 記下叢集現在持有的密碼） | **它就是那個事實在平台這側的副本** | ❌ 必須存活 |
| `state` / 時間戳 / 版本帳本 | 衍生記錄，錯了會自我收斂或可補 | ✅ 可整組回捲 |

⇒ **分開包 savepoint**，一個回捲單位一段。`_reset_admin_password` 的落地形態：

```python
# 提交點：operator 已回 2xx
try:
    with self.env.cr.savepoint():
        self._set_admin_password_secret(...)      # 回捲單位 1：與叢集一致的那個事實
except OperationalError:
    raise
except Exception:
    _logger.exception("… cluster and DB now disagree …")

try:
    with self.env.cr.savepoint():
        halted = self._is_halted()                 # 回捲單位 2：記帳，整組成立或整組不成立
        self.write({'state': …, 'last_upgraded_at': …, 'helm_chart_version': …})
        self._rescale_halted_after_upgrade(client)
except OperationalError:
    raise
except Exception:
    _logger.exception("… the reset itself stands …")

return new_password
```

把兩段包成**同一個** savepoint 就會犯下面那個錯：第二段失敗時把第一段的憑證一起回捲掉。

### 規則三：用 `cr.savepoint()`，不要用裸 `try`

Python 層例外會在 ORM cache 留下**待 flush 的髒值**。不清掉的話它會在本 request 稍後
（甚至 controller 之外的 commit）再炸一次 ⇒ 500 ＋ 框架回捲整筆交易 ＝ 正好是這條 pattern
要避免的形態，只是換一個時間點發生。`_FlushingSavepoint.rollback` 會先 `cr.clear()`
再 `ROLLBACK TO SAVEPOINT`（`odoo/sql_db.py`），把髒 cache 一併清掉。

附帶好處：`_FlushingSavepoint` 進入與正常離開時都會 `cr.flush()`，所以區塊內的寫入會**當場**
下到 DB，constraint 違反在這裡就爆，而不是拖到 request commit（那時已經沒有任何 handler
接得住）。

## Anti-pattern：無差別補 `cr.rollback()`

這族 handler 的外觀與 #520／#525 A 級那條 bug class **一模一樣**（HTTP 路徑、try 內有 DB 寫入、
except 吞掉後回 200 + `success: False`、無 rollback），所以很容易被當成同一種東西處理。

**但因果是反的。** 在提交點之後補 rollback，是把

- 「DB 與叢集**一致**，只是租戶不知情」（可回復：再操作一次）

換成

- 「DB 與叢集**不一致**」（不可回復：管理員登不進 admin GUI，而且下一次 redeploy 會拿 DB 的
  舊值去覆寫叢集）

判斷順序應該是：**先找提交點，再問這個 except 落在提交點的哪一側**。落在之前 ⇒ 是 #520 那條
bug class，該補 rollback／savepoint；落在之後 ⇒ 是本條 pattern。

## 這條成立的前提（過期時先檢查這幾條）

本 pattern 的處方依賴四個前提。**任何一條不成立，處方就要重寫，而不是照抄**：

1. **外部變更真的不可回捲**。若外部系統有補償交易（saga compensation）、或呼叫端能可靠地把它
   撤回，那正確處置可能是「撤回外部變更 ＋ 照常回失敗」，而不是本文的「認賠記帳、回成功」。
   `_reset_admin_password` 的情形是：2xx 之後沒有任何東西會把 Secret 換回去（這正是
   `reset-async-budget-ladder` 的 C11′ 所建立的），所以前提成立。
2. **有「只有這一次能交付」的產物**。一次性明文憑證是這條 pattern 的價值來源。若回應裡沒有這種
   產物（純狀態更新），翻轉成失敗的代價小得多，值不值得為它加兩段 savepoint 要另外衡量。
3. **記帳失敗是罕見的**。這條 pattern 讓記帳失敗變得**安靜**（只有 log）。若某個記帳項開始經常
   失敗，安靜就是掩蓋——那時該修的是那個記帳項，不是把它包得更深。
4. **回捲單位 1（憑證落庫）失敗仍是未解的**。此時叢集是新密碼、DB 是舊的。目前的處置是留
   ERROR log ＋ 仍把明文交付給租戶讓他至少登得進去，但下一次 redeploy 會拿 DB 舊值覆寫叢集。
   **這是已知缺口，不是已解決問題**（記在 #525 的 B3 留言）。要根治需要一條憑證對帳／補寫路徑。

## 怎麼守（測試紀律）

否定式斷言（「不得回捲」「不得回報失敗」）在這裡特別容易退化成恆真——被測程式一改成
「例外提早發生、根本沒到提交點」，斷言照樣綠。所以每條探針**都要連前提一起斷言**：

1. 注入的例外**確實被觸發**（`raised.call_count == 1`）。
2. **提交點確實被跨過**（落庫的憑證確實已經不是舊的）。少了這條，「回應帶得到密碼」在
   「例外提早發生」的情況下也會成立，斷言就失去指向性。
3. 另備**陽性對照**：提交點**之前**失敗（operator 回錯）時，端點仍須回 `success: False`
   且不落庫。沒有它，「必須回 `success: True`」可能只是因為端點恆為 True 而假綠。

落地位置：`src/tests/test_reset_admin_password_post_commit_525b3.py`（6 條，含 1 條陽性對照）。
四次獨立注入式驗證各自精準變紅，證明它們守的是不同東西。

## 覆蓋邊界

- 本文只處理「**外部變更已生效之後**」那一段。提交點**之前**的殘留仍歸 #520 那條 bug class
  （補 rollback／savepoint 撤回），兩者不可互換。
- 本文**不涵蓋**例外訊息的分類（`AccessError` / `MissingError` 不得原文回租戶）——那是獨立的
  外洩面，見 #553 與 #567。兩者常常同時出現在同一個 `except`，但判準無關。
- 沒有機械 gate。判準（哪一行是提交點、哪些寫入屬於哪個回捲單位）需要讀懂該 handler 的語意，
  寫成 AST 規則只會守到形態、守不到不變式。守線靠上面那組帶前提斷言的測試。
