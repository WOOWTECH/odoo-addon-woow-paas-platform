---
title: drift-reaped session 的 GC eligibility 修復設計
category: spec
status: approved
created: 2026-06-14
---

# drift-reaped session 的 GC eligibility 修復設計

> **Goal**：保證任何進入 `state='reaped'` 的 agent_session 都有 `reaped_since`，使 long-term GC（D3）能回收它。修復 drift / webhook 兩條「設 reaped 卻不寫 reaped_since」的路徑，並 backfill 既有列。

## 背景（2026-06-14 stg e2e 實證）

deploy-stg e2e 驗證 D1–D6 時發現：stg 上有 6 個 session 處於 `state='reaped'` 但 `reaped_since=False`（deployment replicas=0），它們**永遠不會被 GC 回收** → scale-to-0 的 namespace / PVC / 明文 LLM secret 永久累積（正是 D1–D3 要解的孤兒 / 計費外洩問題）。

根因是**同一類 bug 的兩個實例**：

| 路徑 | 行為 |
|---|---|
| `_cron_idle_reaper`（`agent_session.py:290-291`） | ✅ 設 `state='reaped'` **+ `reaped_since=now`** |
| `_cron_drift_detection`（`agent_session.py:~240`） | ❌ `vals['state']=status['state']`，從 operator 同步 state，**不寫 `reaped_since`** |
| webhook `state_update`（`agent_runtime_webhook.py:104`） | ❌ `vals['state']=payload['new_state']`，從 operator 回呼同步，**不寫 `reaped_since`** |

GC 的防禦 `if not session.reaped_since: continue`（`agent_session.py:312`，原意防「沒 reaped_since 不貿然刪」）對 drift/webhook-reaped 的列**永久 skip**。

> 諷刺：正是這個防禦守衛，在 e2e 把 `agent_gc_seconds` 臨時設 1 秒時，擋住了那 6 筆 legacy session 的誤刪 —— 它是雙面刃。

## 核心 invariant

**`reaped_since` 有值 ⟺ `state == 'reaped'`。**

集中在 `agent_session.write()` 強制 → 一處覆蓋所有路徑（reaper / drift / webhook / 未來新路徑），勝過逐路徑補（易漏，本 gap 就是逐路徑各自為政造成的）。對齊 repo 既有 pattern [`scope-flip-write-guard-not-ir-rule`](../../decisions/patterns/scope-flip-write-guard-not-ir-rule.md)：欄位轉移的不變式屬於 `write()`，不是分散在各 caller。

## 元件

### ① `agent_session.write()` override（新增）

model 目前只有 `create()` override（`agent_session.py:179`），新增 `write()`：

```python
def write(self, vals):
    res = super().write(vals)
    new_state = vals.get('state')
    if new_state == 'reaped' and 'reaped_since' not in vals:
        # drift / webhook 只帶 state → 補 reaped_since（不覆蓋既有值，保留原 reap 時間）
        stamp = self.filtered(lambda s: not s.reaped_since)
        if stamp:
            stamp.write({'reaped_since': fields.Datetime.now()})
    elif new_state and new_state != 'reaped':
        # 離開 reaped（resume → active / GC → absent）→ 清 reaped_since
        # 防「舊 reaped_since 殘留 → 重新 reap 後被立即誤 GC」的 staleness bug
        clear = self.filtered(lambda s: s.reaped_since)
        if clear:
            clear.write({'reaped_since': False})
    return res
```

- **不遞迴爆炸**：內層 `write({'reaped_since': ...})` / `write({'reaped_since': False})` 的 vals 無 `state` key → `new_state=None` → 兩個分支都不進 → 收斂。
- **reaper 不受影響**：reaper 的 vals 同時帶 `state='reaped'` + `reaped_since` → `'reaped_since' in vals` → stamp 分支跳過，保留 reaper 寫的值。
- **per-record**：用 `filtered` 處理混合 recordset（部分已有 reaped_since），不整批覆蓋。

### ② Migration `src/migrations/18.0.1.0.40/post-migrate.py`

backfill 既有列（對齊 repo migration 慣例：raw SQL、冪等、column-existence guard）：

```python
def migrate(cr, version):
    # state='reaped' 但 reaped_since 為 NULL → 補 now（給乾淨 T_gc 寬限期）
    cr.execute("""
        UPDATE woow_paas_platform_agent_session
        SET reaped_since = now() AT TIME ZONE 'UTC'
        WHERE state = 'reaped' AND reaped_since IS NULL
    """)
```

- 用 `now()`（非推導舊時間）→ backfill 後 T_gc（預設 30 天）才到期，**不會把 stg 那 6 筆瞬間 GC**。
- 冪等：再跑時 `reaped_since IS NULL` 已無列。
- 修復 stg 現存 9,10,12,13,14,15 六筆。

### ③ Manifest 版號

`18.0.1.0.39` → `18.0.1.0.40`（觸發 migration）。

## 測試（TDD，TransactionCase + mock）

1. write() 在 `state→reaped`（只帶 state，模擬 drift/webhook）時 stamp `reaped_since`。
2. write() **不覆蓋**既有 `reaped_since`（同一筆再被 drift 寫 state='reaped' → 保留原值）。
3. write() 在 `state` 離開 reaped（→active / →absent）時清空 `reaped_since`。
4. reaper 仍正確設 `reaped_since`（回歸）。
5. **整合守線**：把 session 用「只寫 state='reaped'」方式（drift-style）推成 reaped → 它變成 GC-eligible（`_cron_gc_reaped` 在超 T_gc 後會回收）。這是本 gap 的直接守線。
6. 離開 reaped 後 reaped_since 清空 → 重新 drift-reaped 拿到**新的** reaped_since（防 staleness 誤 GC）。
7. migration backfill：建一筆 `state='reaped'` + `reaped_since=NULL`（raw SQL 繞 ORM）→ 跑 migrate → 補上值。（或以 write 路徑等價驗證。）

## 不在範圍

- create() 設 reaped 的路徑（無 caller 直接 create reaped session，YAGNI；invariant 僅守 write）。
- GC fallback 改用 last_synced_at（改採「write 補 reaped_since」根治，不在 GC 端補貼）。
- operator 端行為。

## error handling / 安全

- write() override 純資料一致性，不涉 ACL / sudo。
- 不改 GC 的 best-effort 語意（teardown 失敗仍 log+skip）。
- migration 失敗 → Odoo 升級中止（fail-loud，正確）。

## Related

- 前置（已 merged）：D3 GC（`_cron_gc_reaped`）、D4 reaper（`reaped_since` 來源）
- 發現脈絡：2026-06-14 deploy-stg e2e（D1 operator bump `operator-v20260614.0841` 同場）
- pattern：[`scope-flip-write-guard-not-ir-rule`](../../decisions/patterns/scope-flip-write-guard-not-ir-rule.md)（欄位轉移 invariant 屬 write()）
- 既有元件：`agent_session.py`（reaper/drift/gc/create）、`agent_runtime_webhook.py`（state_update）
