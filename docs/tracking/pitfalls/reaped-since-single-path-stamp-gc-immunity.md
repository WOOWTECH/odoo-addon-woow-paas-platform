# Pitfall: 下游 gate 欄位只在「單一路徑」stamp → 其他路徑到達同狀態的 row 永久免疫回收

> 來源：agent 生命週期 deploy-stg e2e（2026-06-14）。修完 D1–D4 後在 stg 實證，發現 6 筆 `agent_session` 處於 `state='reaped'` 但 `reaped_since=False`（deployment `replicas=0`），它們**永遠不會被 D3 long-term GC 回收** → scale-to-0 的 namespace / PVC / 明文 LLM secret 永久累積（正是 D1–D3 要解的孤兒 / 計費外洩問題本身）。根因：`reaped_since` 只在 reaper 路徑 stamp，但 **drift / webhook 兩條路也能把 state 推成 `reaped`** 卻不 stamp，而 GC 用 `if not reaped_since: continue` gate → 那些 row 被永久 skip。**所有 mock 單元測試全綠**（它們都走 reaper 路徑建 reaped）；只有真 cluster e2e（真 operator status 同步 + legacy 資料）才暴露。修復 PR #200（develop）。

## Symptom

- stg `agent_session`：`state='reaped'`、`reaped_since=False`、對應 agent pod deployment `replicas=0`、namespace 仍 Active。
- 把 `agent_gc_seconds` 臨時調到很短跑 `_cron_gc_reaped()`，這些 row **完全不被回收**（也因此沒被「誤刪」——雙面刃，見下）。
- 對應 namespace / PVC / `{release}-llm` 明文 secret 永久殘留，沒有任何路徑會清。
- 單元 / 整合測試（mock client）**全綠** —— 完全測不到。

## Why it happens

### gate 欄位的「寫入點」與「消費點」不對稱

三條路徑都能讓 `agent_session.state` 變成 `reaped`，但只有一條 stamp `reaped_since`：

| 路徑 | 設 `state='reaped'` | 設 `reaped_since` |
|---|---|---|
| `_cron_idle_reaper`（D4 reaper） | ✅ | ✅ 同 vals 帶 `now` |
| `_cron_drift_detection`（§5.5 fallback poll） | ✅ 從 operator `get_status` 同步 | ❌ |
| webhook `state_update`（operator→Odoo 回呼） | ✅ 從 payload `new_state` 同步 | ❌ |

消費點 `_cron_gc_reaped` 用 `if not session.reaped_since: continue`（防禦：沒 reaped_since 不貿然刪）gate。於是「非 reaper 路徑進 reaped」的 row 永久卡在 GC 門外。

> 觸發條件在生產很常見：pod 被 operator 端 / 手動 / crash scale 到 0，或 D4 reaper 還沒存在的舊環境，drift/webhook 都會把 Odoo state 校正成 reaped 而不帶 reaped_since。stg 那 6 筆就是 reaper 上線前的舊 session，被 drift 從 stale 的 `active` 校正成 `reaped`。

### 為什麼 mock 測試遮蔽它

既有 `TestGcReaped` / `TestIdleReaper` 建 reaped session 一律走 reaper 路徑（或 setUp `write({'state':'reaped'})` 後緊接 `_set_reaped_age` 顯式塞 reaped_since）→ 測試世界裡 reaped 永遠帶 reaped_since。**沒有任何測試模擬「drift/webhook 只寫 state、不寫 reaped_since」** → 整條 gap 在 mock 層不存在。同 [[orm-test-green-endpoint-leak-blindness]] / [[sandbox-module-test-false-green]] 家族：**測試只走了 happy 路徑的那一條入口**。

### 防禦守衛是雙面刃

`if not reaped_since: continue` 本意是「沒時間戳不亂刪」（safe）。但它同時**靜默吞掉** gap：e2e 把 `agent_gc_seconds` 設 1 秒想驗 GC，這守衛剛好擋住那 6 筆不被誤刪 —— 救了場，也正是它讓 gap 從來沒人發現。**防禦性 skip 會把「資料不一致」偽裝成「正常跳過」。**

## How to avoid

### 1. 狀態轉移的不變式集中在 `write()`（+ `create()`），不要逐路徑各自 stamp

只要「進入某 state ⟹ 某欄位必須有值」是不變式，就放 model 的 `write()` override 一處強制，覆蓋所有現有與未來路徑。逐路徑補（reaper 記得、drift/webhook 忘記）必然 drift。同 [[scope-flip-write-guard-not-ir-rule]] 哲學：欄位轉移的 invariant 屬於 `write()`。本案修法：

```python
def write(self, vals):
    res = super().write(vals)
    new_state = vals.get('state')
    if new_state == 'reaped' and 'reaped_since' not in vals:
        self.filtered(lambda s: not s.reaped_since).write(
            {'reaped_since': fields.Datetime.now()})   # stamp-on-enter
    elif new_state and new_state != 'reaped':
        self.filtered(lambda s: s.reaped_since).write(
            {'reaped_since': False})                    # clear-on-leave（防 staleness 誤 GC）
    return res
```

`create()` 鏡像同段（create+write 雙向對稱，future-proof），對齊 [[su-env-exempt-guard-false-green]] 的「create+write 雙向覆蓋」紀律。

### 2. 加「下游 gate」時，audit 所有能到達 gating state 的寫入點

要在欄位 X 上加 `if not X: skip/deny` 之前，grep 出**所有**會設「觸發該 gate 的 state」的地方（reaper / drift / webhook / migration / 任何 `write({'state': ...})`），確認每條都維護 X。gate 與 stamp 必須 lock-step（同 [[two-layer-acl-lockstep]] 精神，換成 field-invariant 版）。

### 3. 既有列要 migration backfill

write 端補上後，**升級前已存在**的壞列不會自己好。補 `state='reaped' AND <field> IS NULL` 的 backfill migration（補 `now` 給寬限期、非推導舊時間，避免瞬間觸發下游）。寫 backfill 的 TransactionCase test 若用 raw SQL 製造壞列，先 `flush_recordset()` 再 raw SQL，否則 buffered ORM write 會蓋回 —— 見 [[httpcase-orm-flush-ordering-raw-sql]]（本案 test 已套用）。

### 4. 防禦守衛要留，但要有「不一致偵測」

`if not reaped_since: continue` 保留（belt-and-suspenders），但 invariant 上線後這分支理論上不該再命中；可考慮在命中時 `_logger.warning` 讓「殘留壞列」可觀測，而非靜默 skip。

## Related

- 修復：PR #200（`fix/drift-reaped-gc-eligibility` → develop），commits `ca88112`（write）/`bf55d1d`（migration 18.0.1.0.40）/`f105c8e`（create 對稱）
- 設計 / 計畫：`docs/archived/superpowers/specs/2026-06-14-drift-reaped-gc-eligibility-design.md`、`docs/archived/superpowers/plans/2026-06-14-drift-reaped-gc-eligibility.md`
- 同 e2e session 的姊妹發現（operator 跨元件）：[[agent-runtime-stg-enablement-gap-chain]] Gap #1「operator image 過舊」第二實例（D1 teardown 在 stg 因 operator 過舊而無效，bump `operator-v20260614.0841` 經 Fleet 修復）
- pattern：[[scope-flip-write-guard-not-ir-rule]]（invariant 屬 write）、[[su-env-exempt-guard-false-green]]（create+write 雙向對稱）、[[two-layer-acl-lockstep]]（gate 與來源 lock-step）
- false-green 家族：[[orm-test-green-endpoint-leak-blindness]]、[[sandbox-module-test-false-green]]（mock/單路徑綠 ≠ 真實多路徑正確）
- test 機制：[[httpcase-orm-flush-ordering-raw-sql]]（migration backfill test 的 flush ordering）
- 真實程式碼：`src/models/agent_session.py`（write/create/_cron_idle_reaper/_cron_drift_detection/_cron_gc_reaped）、`src/controllers/agent_runtime_webhook.py`（state_update）
