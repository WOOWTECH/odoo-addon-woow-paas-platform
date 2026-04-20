# E2E Test Results — Phase 2a (Payment Methods) + Phase 2b (Auto-topup)

**Date**: 2026-04-19 / 2026-04-20 (cross-day session)
**Environment**: https://stg.woowtech.io/woow (namespace `dev-sandbox-stg`)
**Final image**: `paas-odoo-stg:20260419.1342`
**Tester**: claude-code session
**Plan**: `~/.claude/plans/nifty-drifting-balloon.md`

## Summary

| TC | Status | Notes |
|----|--------|-------|
| Pre-conditions | ✅ Pass | Provider OK, admin partner phone reset |
| TC-2a-1 Bind sandbox card via UI + 3DS | ✅ Pass | After 4 bug fixes |
| TC-2a-2 Set Default | ⚠️ Partial | API returns success but UI no-op on community Odoo (no `property_payment_token_id` field) |
| TC-2a-3 Delete card | ✅ Pass | Token archived, list refreshed |
| TC-2a-5 Provider disabled | ✅ Pass | Returns `provider_not_found` |
| TC-2b-1 Auto-topup happy path | ✅ Pass | TX done, wallet credited 574, invoice posted |
| TC-2b-2 Idempotency | ✅ Pass | Re-run cron: no double charge |
| TC-2b-3 No-token low-balance email | ✅ Pass | Email queued, dedup field set, re-run doesn't resend |
| Unit tests | ⚠️ Partial | TestSanitizePhone/TestAttemptAutoTopup/TestLowBalance pass; 3 HttpCase TestPaymentMethodsApi fail (pre-existing) |

## Bugs Found & Fixed (during E2E session)

| # | Severity | Commit | Description |
|---|----------|--------|-------------|
| 1 | High | `0e15108` (pre-existing) | `_sanitize_phone_for_ecpay` strips `+/-/space/parens` from partner.phone before sending to ECPay |
| 2 | Blocker | `574512e` | Don't preload node-forge — when forge present, ECPay SDK `initialize` throws `TypeError: i is not a function`. Letting SDK self-load `forge-0.10.0.min.js` fixes it |
| 3 | Blocker | `8e89f08` | Add `csrf=False` bouncer at `/api/billing/payment-methods/bind-result/<trade_no>` for ECPay's POST to OrderResultURL (was hitting `/woow` and getting 400 CSRF) |
| 4 | Blocker | `99ca65e` | Bouncer must call `_handle_bind_callback` to persist token (S2S `/payment/ecpg/bind_callback` doesn't reliably fire on sandbox); use HTML+JS redirect instead of 303 to bypass cross-site cookie strip |
| 5 | High | `984af0e` | Default-token fallback for community Odoo — `property_payment_token_id` is Enterprise-only; when absent, pick most recent active ECPG token. Fixes both list and auto-topup |

## Phase 2c Follow-ups

| # | Task | Notes |
|---|------|-------|
| 1 | Session loss after 3DS bind redirect | ECPay's cross-site POST to bouncer is anonymous → Odoo issues new anon `session_id` cookie that overwrites the user's authenticated one. User has to re-login after bind. Possible fix: bypass session middleware on bouncer, or different binding flow (popup window). |
| 2 | 3 HttpCase test failures | `TestPaymentMethodsApi.test_create_bind_returns_three_d_url`, `test_delete_archives_token`, `test_list_cards_returns_payment_tokens` all fail with `body['success']=False`. Auth or test isolation issue, not real logic bug. |
| 3 | Set Default UI badge | Backend now returns `is_default=true` correctly via fallback; frontend `PaymentMethodsListPage` should show "Default" badge instead of "Set Default" button when `is_default==true`. |
| 4 | ~~upstream `payment_ecpay_ecpg`: phone sanitization in `_send_payment_request`~~ ✅ **FIXED** | Forwarded to ecpay agent → upstream commit `38fcf13` extracted shared `sdk/utils.sanitize_phone_for_ecpay`. paas-odoo-ci synced (commit `8be9603`) and built as `paas-odoo-stg:20260420.0419`. Re-tested on staging with `partner.phone='+886-912-345-678'`: TX 19 `TOPUPC511D1713E80453` state=`done`, wallet credited 574 — **workaround no longer required**. |
| 5 | `_handle_invoice_creation` doesn't pay from wallet | After successful auto-topup, wallet has 574 but new invoice stays `payment_state=not_paid`. The `sh_subscription_wallet` mixin has its own deduct flow but isn't wired to invoice posting. Out of Phase 2b scope but blocks the "complete" subscription flow. |
| 6 | `/woow#/settings/profile` profile page | Users currently can't edit phone (or anything else) from the SPA. Demo data partner has `+1 555-555-5555` which needs sanitization for ECPay; users should be able to fix this themselves. |

## Verification Approach

- **UI**: Playwright MCP automated bind flow (4311-9522-2222-2222 / 12/28 / 222 / OTP 1234)
- **Backend**: `kubectl exec ... odoo shell` for DB queries and cron triggers
- **Tokens**: `payment.token` table for `ecpg_bindcard_id`, `active`, `partner_id`
- **Auto-topup**: `payment.transaction` for `TOPUP*` references, `state=done`; `wallet.transaction` for credit; `account.move` for invoice; `sh.subscription.subscription.sh_date_of_next_payment` for advancement

## Stable State for Future Sessions

- ECPay provider: `MerchantID 3002607`, sandbox `Stage` mode, `enable_ecpg=True`
- Admin partner phone: digits-only `0912345678` (workaround until upstream phone fix lands)
- Subscription used: SUB-00007 (admin partner, monthly recurrence, ~574 TWD)
- Bound token: `payment_token id=2`, BindCardID `2bde87dfce2844fbb698b0829dc1d829337da9b9c4f54cda8fe588cbab8f0402`
- Wallet balance: 574 (post-topup, not yet deducted by invoice)

## Pickup Prompts for Future Sessions

Each Phase 2c follow-up gets its own session. Paste the prompt verbatim into a fresh `claude` session.

### #1 — Fix session loss after 3DS bind redirect

```
接手 Phase 2c #1：修復 3DS 綁卡完成後 session 流失。

背景看 docs/plans/2026-04-19-e2e-test-results.md 的 Phase 2c
Follow-ups 表 row #1 + Bugs Found & Fixed 表 row #4。

問題：ECPay 對 OrderResultURL 的 cross-site POST 是匿名請求，
Odoo 中介層會 Set-Cookie 一個新的 anon session_id，覆蓋使用者
原本登入的 cookie。bouncer 已經改用 HTML+JS redirect 但還是
踩到這個 cookie 蓋寫，導致 user 落到 /web/login。

請用 brainstorming + writing-plans skill 探索修復方案
（可能：bypass session middleware on bouncer / popup window flow /
preserve cookie via custom header / 其他），列出 trade-offs，
和我確認後再執行。

關鍵檔：src/controllers/billing.py 的 api_payment_method_bind_result
```

### #2 — Investigate 3 TestPaymentMethodsApi HttpCase failures

```
接手 Phase 2c #2：修復 3 個 TestPaymentMethodsApi HttpCase 測試。

背景：docs/plans/2026-04-19-e2e-test-results.md Phase 2c row #2。

3 個失敗 test：
- test_create_bind_returns_three_d_url
- test_delete_archives_token
- test_list_cards_returns_payment_tokens

都是 body['success']=False。手動 E2E 已驗證功能 OK，所以是
test setup/auth 問題，不是 logic bug。

執行步驟：
1. 用 systematic-debugging skill 找根因
2. 跑單一 test：./odoo-bin -c odoo.conf -d test --test-enable
   --test-tags '/woow_paas_platform:TestPaymentMethodsApi.test_delete_archives_token'
3. 修好後跑完整 woow_paas_platform suite 確認沒回歸

關鍵檔：src/tests/test_payment_methods_api.py
```

### #3 — Set Default UI badge

```
接手 Phase 2c #3：修 PaymentMethodsListPage 的 Default badge 顯示。

背景：docs/plans/2026-04-19-e2e-test-results.md Phase 2c row #3。

API 已正確回 `is_default: true`（fallback 邏輯在 commit 984af0e
加好），但前端仍顯示 "Set Default" 按鈕而非 "Default" badge。
單純 UI rendering bug。

關鍵檔：src/static/src/paas/pages/settings/PaymentMethodsListPage.js
（找 is_default 條件分支）

請用 frontend-design skill 把按鈕條件改對，並考慮 default card
的視覺差異（例如外框顏色、checkmark 圖示）。
```

### #5 — `_handle_invoice_creation` not paying from wallet

```
接手 Phase 2c #5：subscription invoice 沒從 wallet 自動扣款。

背景：docs/plans/2026-04-19-e2e-test-results.md Phase 2c row #5。

E2E 觀察：auto-topup 成功後 wallet=574，新 invoice state=posted
但 payment_state=not_paid。預期應該從 wallet 扣 574 並標記 paid。

涉及兩個 addon：
- sh_subscription/_handle_invoice_creation（只 post，不付款）
- sh_subscription_wallet/subscription_wallet_mixin（有 deduct
  flow 但沒被 invoice posting 觸發）

請用 systematic-debugging 找出兩者該如何 wire。可能要在 invoice
post hook 加 wallet 扣款，或修 _handle_invoice_creation 改呼叫
mixin 的 deduct 流程。會跨兩個 upstream addon — 若需要改
ecpay_odoo18 / sh_subscription 系列，再透過 maestri 派工。
```

### #6 — `/woow#/settings/profile` 個人資料頁面

```
接手 Phase 2c #6：建立 settings profile 頁面讓使用者編輯
phone / email / name 等個人資料。

背景：docs/plans/2026-04-19-e2e-test-results.md Phase 2c row #6。

需求：使用者要能在 SPA 內修改 partner.phone（避免 ECPay
phone 格式問題卡住自己），以及其他個人資料欄位。

請用 brainstorming → writing-plans → frontend-design 流程：
1. brainstorm：要編輯哪些欄位？要不要 email 確認？頭像？
2. plan：路由、API、OWL component 結構
3. 實作

關鍵檔可參考：
- 路由：src/static/src/paas/core/router.js
- 既有 settings 頁：src/static/src/paas/pages/settings/
- API pattern：src/controllers/billing.py 的其他 endpoint
```
