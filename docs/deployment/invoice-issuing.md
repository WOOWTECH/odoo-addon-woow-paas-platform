# ECPay E-Invoice Issuing in PaaS

> **Audience:** Ops、部署工程師、第一次讀 `_create_topup_invoice` /
> `ecpay_invoice_cron` 的維護者。

PaaS 的開票流程刻意與上游 `ecpay_invoice_tw` 的 inline `action_post()` hook
解耦：所有 PaaS 開立的 ECPay 電子發票都走自家的非同步 cron
(`woow_paas_platform.ecpay_invoice_cron`)，並透過
`account.move.woow_skip_upstream_hook` 旗標阻擋上游 hook 在同一張
`account.move` 上重複開票。

## TL;DR

1. **三種 invoice，三種行為**：手動儲值 → cron 開；auto-topup → cron 開；
   訂閱 cycle → 不開（已在儲值時開過）。
2. **`woow_skip_upstream_hook=True` 是隔離旗標**：阻止上游
   `ecpay_invoice_tw` 在 `_pay_from_wallet` 的同一個 transaction 內
   inline 呼叫 `create_ecpay_invoice`（會在失敗時 rollback wallet 扣款）。
3. **預期 `company.auto_invoice='manual'`**，但即使 ops 改成
   `'automatic'`，PaaS 仍可正常運作（skip flag 已短路上游 hook）。

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ User clicks "Add Credits"  /  subscription cron triggers shortfall │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ wallet.topup record (source='manual' or 'auto')                  │
│   .charge_via_token() → ECPay charge → wallet credit             │
│   ._ensure_topup_invoice() → account.move (out_invoice)          │
│       woow_invoice_issue_state = 'pending'                       │
│       woow_skip_upstream_hook  = True                            │
└──────────────────────────────────────────────────────────────────┘
                            │
                            │ (action_post fires; upstream hook
                            │  short-circuited because skip=True)
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ ir.cron 'Issue pending ECPay e-invoices' (every 5 min)           │
│   _cron_issue_pending_invoices() picks up posted moves           │
│   _issue_one() → with_context(woow_force_issue=True)             │
│                  .create_ecpay_invoice()                         │
└──────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ ECPay 電子發票開立 → woow_invoice_issue_state = 'issued'         │
│ topup.invoice_issued_at 同步寫入                                  │
└──────────────────────────────────────────────────────────────────┘
```

訂閱 cycle invoice 走獨立分支：

```
sh_subscription cron → _get_or_create_cycle_invoice()
   account.move (out_invoice)
     woow_invoice_issue_state = 'not_required'   ← cron 永遠不會撿到
     woow_skip_upstream_hook  = True              ← 上游 hook 也不會跑
   ↓
   _pay_from_wallet() → 完整由 wallet 結算（儲值時已開過 ECPay 發票）
```

## 三種 Invoice 對照

### 1. 手動儲值（user 點 "Add Credits"）

| 項目 | 值 |
|------|---|
| Trigger | `wallet.topup(source='manual')` → `charge_via_token()` |
| `woow_invoice_issue_state` | `pending` |
| `woow_skip_upstream_hook` | `True` |
| 由誰開立 ECPay 發票 | `ecpay_invoice_cron`（每 5 分鐘） |
| Source | `src/models/wallet_topup.py:434` (`_create_topup_invoice`) |

### 2. Auto-topup（訂閱週期 wallet 不足，自動補差額）

| 項目 | 值 |
|------|---|
| Trigger | 訂閱 cron → `_attempt_auto_topup` → `wallet.topup(source='auto')` |
| `woow_invoice_issue_state` | `pending` |
| `woow_skip_upstream_hook` | `True` |
| 由誰開立 ECPay 發票 | 同手動儲值（`ecpay_invoice_cron`） |
| 金額處理 | `math.ceil()` 到整數 TWD（ECPay 限制 + 金額對帳一致性） |
| Source | `src/models/wallet_topup.py:91` (`create_auto_topup`)，<br>`src/models/sh_subscription_fix.py:380` (`_attempt_auto_topup` shim) |

> Path B 的核心：auto-topup 走與手動儲值**完全對稱**的 `wallet.topup` 流程，
> 不再用獨立的 ad-hoc 開票路徑。`_attempt_auto_topup` 目前是 thin
> deprecation shim（`src/models/sh_subscription_fix.py:380`），新程式碼應直接
> 用 `wallet.topup.create_auto_topup()` + `charge_via_token()`。

### 3. 訂閱 Cycle Invoice（訂閱週期帳單）

| 項目 | 值 |
|------|---|
| Trigger | `sh_subscription` cron → `_get_or_create_cycle_invoice()` |
| `woow_invoice_issue_state` | `not_required` |
| `woow_skip_upstream_hook` | `True` |
| 由誰開立 ECPay 發票 | **沒人**（不需要） |
| 為什麼不開 | 由 wallet 結算，wallet 點數於儲值/auto-topup 時已開過 ECPay 發票，再開一次會雙重計稅 |
| Odoo invoice 仍會建立 | ✅ 作為內部會計分錄 |
| Source | `src/models/sh_subscription_fix.py:108` (`_get_or_create_cycle_invoice`) |

> 設計原則參見
> [`docs/architecture/billing-philosophy.md`](../architecture/billing-philosophy.md)
> 五大原則第 3、4 點。

## 為什麼需要 `woow_skip_upstream_hook`?

上游 `ecpay_invoice_tw` 在 `account.move.action_post()` 內**同步**呼叫
`create_ecpay_invoice()`（當 `company.auto_invoice='automatic'` 時），
若 ECPay API 出錯會 raise `UserError`，**整個外層 transaction 會 rollback**
─ 包括 `_pay_from_wallet` 已經完成的 wallet 點數扣款。這會造成兩種災難：

1. **使用者被扣點數但沒有 invoice**（rollback 失敗或部分提交時的 race）
2. **重複扣款 / 重複開票**（cron 與 inline hook 互相干擾）

PaaS 的解法（Task 2.1，`src/models/account_move.py:12`）：

```python
def create_ecpay_invoice(self):
    if (self.woow_skip_upstream_hook
            and not self.env.context.get('woow_force_issue')):
        return  # short-circuit upstream inline call
    return super().create_ecpay_invoice()
```

| 呼叫者 | `skip` flag | `woow_force_issue` ctx | 結果 |
|--------|-------------|------------------------|------|
| 上游 inline hook（`action_post` 內） | True | 缺 | **跳過** ✅ |
| `ecpay_invoice_cron._issue_one` | True | 有（`with_context(woow_force_issue=True)`） | **執行** ✅ |
| 第三方 / 人工開單（無 PaaS flag） | False | 缺 | **執行** ✅（不影響上游既有行為） |

詳見 design：`docs/plans/2026-05-03-invoice-issue-fix-path-b.md` Task 2.1 / 2.2。

> ⚠️ **Refund (out_refund) not yet covered.** Upstream's `action_post` also calls
> `run_refund()` inline when `company.auto_invoice='automatic'`. Our override
> only intercepts `create_ecpay_invoice`. If/when refund/折讓 logic is added,
> mirror the skip-flag pattern for `run_refund`.

## 如果 ops 把 `company.auto_invoice='automatic'` 會怎樣？

PaaS 仍可正常運作。

- **PaaS 開立的 invoice**（手動儲值、auto-topup、cycle）都帶
  `woow_skip_upstream_hook=True`，上游 `ecpay_invoice_tw` 的 inline hook
  會被短路，不會嘗試開立電子發票或 rollback transaction。
- **Async cron** 透過 `with_context(woow_force_issue=True)` 開後門
  繞過自己的 short-circuit，照常完成開立。
- **第三方 / 人工建立的 invoice**（沒設 PaaS flag）行為不變，
  上游 hook 該怎麼跑就怎麼跑（保持向後相容）。

> 預設 `company.auto_invoice='manual'`（上游 default）即可，PaaS 的設計
> 不依賴它。Task 2.1 的 override 就是給未來上游或人為改設定時的雙重保險。

## Operational Considerations

### Cron 排程

| Cron | 模型/方法 | 間隔 | XML |
|------|-----------|------|-----|
| Issue pending ECPay e-invoices | `woow_paas_platform.ecpay_invoice_cron._cron_issue_pending_invoices` | 5 min | `src/data/ecpay_invoice_cron.xml` |
| Cleanup stale pending topups | `woow.wallet.topup._cron_cleanup_pending_topups` | 10 min | `src/data/cron_topup.xml` |

### Monitoring

Splunk / Datadog query keys（structured prefix in logger output）：

| Prefix | 涵蓋 |
|--------|------|
| `[ECPAY_INV]` | 開立成功、失敗、retry、max retry 達到 |
| `[TOPUP]` | 儲值的 wallet 入帳、invoice 建立 |
| `[TOPUP_AUTO]` | auto-topup 的 ECPay 扣款流程 |
| `[AUTO_TOPUP]` | `_attempt_auto_topup` shim 委派紀錄 |
| `[INV]` | `_pay_from_wallet` 結算紀錄 |
| `[SUB_FIX]` | 訂閱 auto-invoice cron 流程 |

DB 監控指標（Path B 部署後預期變化）：

- `woow.wallet.topup` 行數會出現 step-change：auto-topup 從這版開始也會
  寫入 `wallet.topup`（`source='auto'`），這是 Path B 的設計（symmetric
  with manual），**不是異常**。
- `account.move.woow_invoice_issue_state` 分布：`not_required`（cycle）
  應為主要、`issued`（topup）應穩定增加；`failed` / `error` 應極少。

### Failure Modes & Retry

`ecpay_invoice_cron._record_failure`（`src/models/ecpay_invoice_cron.py:92`）：

| 嘗試次數 | `woow_invoice_issue_state` | 下次 retry 間隔 | 備註 |
|---------|---------------------------|----------------|------|
| 1（首次失敗） | `failed` | +5 min | `woow_invoice_next_retry_at` 寫入 |
| 2 | `failed` | +30 min | |
| 3 | `error` | — | `mail.activity` 派給 `account.group_account_manager` |

每次失敗都會把錯誤訊息寫進 `woow_invoice_last_error`（最多 1000 字元，
方便 admin 排查）。

### Manual Recovery

`account.move.action_woow_retry_ecpay_invoice`
（`src/models/account_move_invoice_status.py:34`）：

- 權限：`account.group_account_manager`（accounting manager）
- 行為：reset retry tracking（`woow_invoice_retry_count`、
  `woow_invoice_last_error`、`woow_invoice_next_retry_at`），把
  `woow_invoice_issue_state` 拉回 `pending`，立即同步呼叫
  `_issue_one(self)` 重試一次。
- 安全檢查：拒絕對 `issued` / `not_required` / 非 `posted` 的 move 重試
  （raise `UserError`）。

CLI 重試（緊急情境，例如批次重新觸發）：

```bash
odoo shell -d <db_name> << 'EOF'
moves = env['account.move'].search([
    ('woow_invoice_issue_state', '=', 'error'),
])
for m in moves:
    m.write({
        'woow_invoice_issue_state': 'pending',
        'woow_invoice_retry_count': 0,
        'woow_invoice_last_error': False,
        'woow_invoice_next_retry_at': False,
    })
env.cr.commit()
print(f"Reset {len(moves)} moves to pending; cron will retry within 5 min")
EOF
```

## Reference

- 設計哲學：[`docs/architecture/billing-philosophy.md`](../architecture/billing-philosophy.md)
- 實作 plan：[`docs/plans/2026-05-03-invoice-issue-fix-path-b.md`](../plans/2026-05-03-invoice-issue-fix-path-b.md)
- 程式碼進入點：
  - `src/models/wallet_topup.py:411` (`_ensure_topup_invoice`)
  - `src/models/wallet_topup.py:434` (`_create_topup_invoice`)
  - `src/models/account_move.py:12` (`create_ecpay_invoice` override / Task 2.1)
  - `src/models/ecpay_invoice_cron.py:64` (`_issue_one` / Task 2.2)
  - `src/models/sh_subscription_fix.py:108` (`_get_or_create_cycle_invoice` / Task 3.1)
  - `src/models/sh_subscription_fix.py:380` (`_attempt_auto_topup` deprecation shim / Task 4.3)
  - `src/models/account_move_invoice_status.py` (PaaS 旗標欄位 + manual retry button)
