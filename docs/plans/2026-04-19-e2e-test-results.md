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
| 1 | ~~Session loss after 3DS bind redirect~~ ✅ **FIXED** | Root cause: Odoo's `_save_session` post-dispatch hook minted a fresh `Set-Cookie: session_id` on the anon session created by ECPay's cross-site POST, overwriting the user's authenticated cookie. Fix (commit `41a9e18`): added `save_session=False` to the bouncer route decorator on `api_payment_method_bind_result`, mirroring upstream `payment_mollie.mollie_return_from_checkout` and `payment_aps.aps_return_from_checkout`. Verified on staging image `paas-odoo-stg:20260420.0638`: `curl -i` against the bouncer (via Cloudflare and via port-forward) returns 200 + HTML+JS redirect with **no `Set-Cookie: session_id` header**; browser navigation through the bouncer lands on `/woow#/settings/payment-methods?bind_status=ok` with session intact (sidebar + admin nav rendered, no `/web/login` bounce) and "Card bound successfully" banner shown. Caveat: ECPay's real server-side POST currently returns 502 at the Cloudflare tunnel layer — separate infra issue, unrelated to the session fix (proven by GET/browser-initiated POST to the same URL working). Regression test: `TestPaymentMethodsApi.test_bouncer_does_not_overwrite_session_cookie`. |
| 2 | ~~3 HttpCase test failures~~ ✅ **FIXED** | Root cause: in Odoo 18 HttpCase, `self.env.user` defaults to `__system__` (uid=1, partner=OdooBot, id=2) but `self.authenticate('admin', 'admin')` puts the HTTP session on admin (uid=2, partner=Administrator, id=3). Three tests created `payment.token` records with `partner_id=self.env.user.partner_id.id` (OdooBot) while the endpoints filter on `request.env.user.partner_id.id` (Administrator) — tokens were invisible, yielding `body['success']=False` / empty list / member_id mismatch. Fix (commit `39ee823`): introduce `self.partner = self.env.ref('base.partner_admin')` in `setUp` and replace 3 uses of `self.env.user.partner_id` with `self.partner`. Endpoint code unchanged — it was correct. Also `c850fad` exports `post_init_hook` from `src/__init__.py` (prerequisite: `post_init_hook` was declared in manifest but never imported, breaking fresh DB installs which these HttpCase tests need). Re-ran on a fresh `test_phase2c` DB in staging pod (image `paas-odoo-stg:20260420.0638`): `0 failed, 0 error(s) of 7 tests`. Later extended to 9 tests by `3bf0bf8` (contract pin-down) and again to 16 overall with `TestProfileApi` in #6 — all green. |
| 3 | ~~Set Default UI badge~~ ✅ **OBSOLETE — no change needed** | On 2026-04-20 re-verified on staging (`paas-odoo-stg:20260420.0638`) that `PaymentMethodsListPage.xml:64,70` already gates `Default` badge on `t-if="card.is_default"` and hides `Set Default` button on `t-if="!card.is_default"` — conditions were correct from commit `13d63a2` onward. Combined with the backend fallback landed in `984af0e`, the admin's default card correctly renders the badge and suppresses the button. The original row reflected a stale pre-fallback observation; no frontend change required. |
| 4 | ~~upstream `payment_ecpay_ecpg`: phone sanitization in `_send_payment_request`~~ ✅ **FIXED** | Forwarded to ecpay agent → upstream commit `38fcf13` extracted shared `sdk/utils.sanitize_phone_for_ecpay`. paas-odoo-ci synced (commit `8be9603`) and built as `paas-odoo-stg:20260420.0419`. Re-tested on staging with `partner.phone='+886-912-345-678'`: TX 19 `TOPUPC511D1713E80453` state=`done`, wallet credited 574 — **workaround no longer required**. |
| 5 | `_handle_invoice_creation` doesn't pay from wallet | After successful auto-topup, wallet has 574 but new invoice stays `payment_state=not_paid`. The `sh_subscription_wallet` mixin has its own deduct flow but isn't wired to invoice posting. Out of Phase 2b scope but blocks the "complete" subscription flow. |
| 6 | ~~`/woow#/settings/profile` profile page~~ ✅ **FIXED** | Delivered a `/settings/profile` SPA page where the logged-in user can edit name/email/phone, rewired the Header to consume `profileService` so it displays real partner data (replacing the hardcoded "Sarah Wilson" + "PRO PLAN" placeholder), and added a Profile card to Settings above Payment Methods. Primary commits: `61ccd41` (`/api/me` GET/update endpoints + HttpCase tests), `3bf0bf8` (full-replace contract + 2 edge-case tests), `f89491d` (ProfilePage + profileService wired to `/api/me`), `f46f133` (code-review refactor), `e6d2945` (Header reads real partner; Profile card in Settings), `876cdcd` (dedup concurrent `fetch()` via `_fetchPromise`), `0ec2789` (constrain Header avatar img + drop orphan `user_plan` rule). Released as `v18.0.1.0.9` → `v18.0.1.0.10` (the latter carrying the avatar CSS fix from `0ec2789`). Verified on staging image `paas-odoo-stg:20260420.2251`: all 9 E2E checks passed — login OK, Header shows `Administrator` reactively updating after name edit, Settings renders Profile card above Payment Methods, `#/settings/profile` pre-fills from partner, invalid-email + empty-name show inline validation without hitting server, valid save round-trips through `/api/me` and DB write verified via `SELECT id, name, email, phone FROM res_partner WHERE id=3;` → `3 \| Administrator \| admin@example.com \| +886-912-345-678`. Design: `docs/plans/2026-04-20-settings-profile-design.md`; Plan: `docs/plans/2026-04-20-settings-profile-implementation.md`. Regression test: `TestProfileApi` (9 HttpCase tests in `src/tests/test_profile_api.py`), all green on fresh DB. |

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

### ~~#3 — Set Default UI badge~~ (obsolete, no pickup)

Verified 2026-04-20 on staging: the XML conditions were already correct from
commit `13d63a2`; the row was a stale pre-backend-fallback observation. No
implementation needed. Left here for historical context.

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

### ~~#6 — `/woow#/settings/profile` 個人資料頁面~~ (completed, no pickup)

Shipped on 2026-04-21 as release `v18.0.1.0.10`. See Phase 2c row #6
above for the commits/image tag. Follow-ups (if any) should be new
sections.
