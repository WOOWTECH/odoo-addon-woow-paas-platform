# Pitfall：被 XML `<button type="object">` 綁著的 method，回傳 dict 會被 web client 當成 action 執行

> **一句話**：改一個 model method 的回傳型別時，Python 呼叫端的完整盤點**看不到 XML 按鈕這一端**（`grep 'method_name('` 抓不到 `name="method_name"`）；而 Odoo backend 的 `doActionButton` 對回傳值做的是「是 object 就當 action 執行」，所以把回傳值從 `None` 改成結構化 dict 會讓按鈕在 runtime throw，Python 測試全綠也照樣炸。
>
> **發現場景**：#554 讓 `action_sync_tools` / `action_test_connection` 回傳可判讀的失敗值時，評估 (a) raise vs (b) 回傳值的取捨。

## Symptom

改動看起來完全內聚——只動 model method 的回傳值、只改 controller 的判讀，Python 測試全綠。但 Odoo backend form 上那顆按鈕點下去會噴：

```
The ActionManager service can't handle actions of type undefined
```

或者（改成往外 raise 的版本）跳出錯誤對話框，而**方法在 except 分支剛寫進去的稽核紀錄消失了**——DB 裡查不到 `state='error'`，使用者連「失敗過」都看不到。

兩種症狀都不會出現在 Python 測試裡，因為測試呼叫的是 method 本身或 JSON-RPC controller，不會走 `doActionButton`。

## Root Cause

### ① 回傳 dict ＝ 宣告一個 action

`addons/web/static/src/webclient/actions/action_service.js` 的 `doActionButton`：

```js
action = await keepLast.add(callProm);
action = action && typeof action === "object"
    ? action
    : { type: "ir.actions.act_window_close" };
```

回傳 object 被原封不動送進 `doAction`，缺 `type` 就在同檔 throw（`The ActionManager service can't handle actions of type ${action.type}`）。bool / None / 字串則落進 else 分支，被安全地轉成 `act_window_close`。

### ② 往外 raise ＝ 連稽核紀錄一起回捲

例外逸出到 JSON-RPC dispatcher 之後，`odoo/service/model.py` 的 `retrying()` 走的是：

```python
except Exception:
    env.reset()
    env.registry.reset_changes()
    raise          # ← 不會走到下面的 env.cr.commit()
...
if not env.cr.closed:
    env.cr.commit()
```

而 `odoo/sql_db.py` 的 `Cursor.close(rollback=True)` 預設回捲。⇒ **整筆交易不 commit**，包含 `except` 分支裡剛寫的 `state='error'` / `state_message`。

controller 路徑不會有這個問題（controller 接住例外、正常 return dict ⇒ request 正常結束 ⇒ commit），所以**同一個修法在兩條呼叫路徑上的後果相反**——這正是它容易漏掉的原因。

### ③ 盤點工具本身有盲區

`action_sync_tools` 的非測試呼叫端有三個：`paas.py` 兩處 ＋ `mcp_server_views.xml` 一處。前兩個 `grep 'action_sync_tools()'` 就找得到；第三個長這樣：

```xml
<button name="action_sync_tools" type="object" string="Sync Tools" class="btn-primary"/>
```

⇒ **做了完整的 Python 呼叫端盤點，仍然會漏掉它。**

## Fix

要讓呼叫端能判讀成敗，同時不破壞按鈕、又保住稽核紀錄：

1. **回傳 bool**（`True`＝成功），不回 dict。
2. **錯誤訊息不放進回傳值**，讓呼叫端讀 model 上剛寫好的欄位（本案是 `state_message`，`_format_mcp_server` 本來就在回傳這欄）。
3. **不改成往外 raise。**
4. 需要回捲「中途寫到一半的資料」但保留稽核紀錄時，用 `with self.env.cr.savepoint():` **只包住該回捲的區段**，稽核 write 放在 savepoint **外**：

```python
try:
    with self.env.cr.savepoint():
        asyncio.run(self._async_sync_tools())   # 中途失敗 → 只回捲這段寫入
    self.write({'state': 'connected', ...})
    return True
except Exception as e:
    self.write({'state': 'error', 'state_message': str(e)})   # savepoint 外 → 留得住
    return False
```

`_FlushingSavepoint`（`odoo/sql_db.py`）進入時 `cr.flush()`（先前的寫入先落地，不被連坐回捲）、rollback 時 `cr.clear()` 再 `ROLLBACK TO SAVEPOINT`。

現行 code：`src/models/mcp_server.py` 的 `action_sync_tools` / `action_test_connection`（docstring 內含本 pitfall 的 in-code 指針）。

## Prevention

- **改 model method 的回傳型別前，先 grep 它的名字有沒有出現在 XML `<button name="...">` 裡**，而不只是 grep Python 呼叫端。
- 判斷「回傳 bool 安不安全」有現成的陽性對照可用：若既有碼在成功路徑已經 `return True`、而那顆按鈕長期沒炸過，就是實證。
- 同一個 method 被 controller（例外會被接住 ⇒ commit）與 XML 按鈕（例外逸出 ⇒ 回捲）兩條路徑呼叫時，**任何「靠拋例外傳遞失敗」的設計都要分別推演兩條路徑的後果**。
