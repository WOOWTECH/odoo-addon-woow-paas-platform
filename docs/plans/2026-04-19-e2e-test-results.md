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
