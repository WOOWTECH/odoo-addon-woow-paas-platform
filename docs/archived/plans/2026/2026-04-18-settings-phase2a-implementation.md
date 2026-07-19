---
status: archived
archived-from: docs/plans/2026-Q2/2026-04-18-settings-phase2a-implementation.md
archived: 2026-07-19
archived-reason: "已落地（Settings UI build phase）；現況=src/static/src/paas"
---

# Settings Payment Methods UI (Phase 2a) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire the `payment_ecpay_ecpg` addon into our OWL app so users can bind, list, delete, and set default credit cards from `#/settings/payment-methods`. No auto-topup in this phase (Phase 2b).

**Architecture:**
- Backend wrapper in `woow_paas_platform` that directly imports `payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient` and exposes `/api/billing/payment-methods/*` endpoints under our own URL namespace. The wrapper builds its own `ReturnURL`/`OrderResultURL` so that after 3DS the user lands back on `/woow#/settings/payment-methods?bind_status=ok`, instead of ecpay's default portal page.
- Frontend: two OWL pages (`PaymentMethodsListPage`, `AddPaymentMethodPage`), with the ECPay JS SDK dynamically injected on the Add page only (jQuery → node-forge → `sdk-1.0.0.js`). 3DS is handled by a full-window `window.location.href = three_d_url` redirect per the decision (X).

**Tech Stack:** Odoo 18, OWL 2, Python 3.12, ECPay ECPG 2.0 SDK (Python + JS), SCSS.

**Out of scope (Phase 2b or later):**
- Auto-topup from bound card (`_attempt_auto_topup` fallback in subscription cron)
- GetMemberBindCard reconcile with ECPay (we only read local `payment.token`)
- Separate "set as default" without also changing Odoo's core `property_payment_token_id` (we use whatever the ecpay addon provides)

**Dependencies:**
- `payment_ecpay_ecpg` addon must be installed and `payment.provider.enable_ecpg=True` on at least one ECPay provider.
- `payment_ecpay_ecpg` is already bundled into `paas-odoo-ci` commit `f32c677` (confirmed 2026-04-19).

---

## Task 1: Depend on `payment_ecpay_ecpg` and add soft-import guard

**Rationale:** The wrapper endpoints need `ECPGClient`. We must declare the dep in `__manifest__.py` and add a guard for environments without the addon (local dev without ECPG, upgrade scenarios).

**Files:**
- Modify: `src/__manifest__.py` (add `payment_ecpay_ecpg` to `depends`)

**Step 1: Verify current `depends` list**

```bash
grep -A6 "'depends':" src/__manifest__.py
```

Expected: `['base', 'web', 'mail', 'portal', 'account', 'payment', 'sale_management', 'website', 'website_sale', 'sh_subscription', 'sh_subscription_wallet', 'payment_ecpay']` (or similar — check for exact list).

**Step 2: Add `payment_ecpay_ecpg` after `payment_ecpay`**

Edit `src/__manifest__.py` so `depends` includes `payment_ecpay_ecpg`. Keep alphabetical order inside logical grouping.

Example after:
```python
'depends': [
    'base', 'web', 'mail',
    'portal', 'account', 'payment',
    'sale_management', 'website', 'website_sale',
    'sh_subscription', 'sh_subscription_wallet',
    'payment_ecpay', 'payment_ecpay_ecpg',
],
```

**Step 3: Verify the addon exists in both repos**

```bash
test -d ../../../woow-paas-repo/WOOWTECH/ecpay_odoo18/payment_ecpay_ecpg && echo "ecpay addon OK"
test -d ../../../woow-paas-repo/paas-odoo-ci/addons/payment_ecpay_ecpg && echo "CI bundled OK"
```

Both should echo OK.

**Step 4: Commit**

```bash
git add src/__manifest__.py
git commit -m "chore: depend on payment_ecpay_ecpg for ECPG binding wrapper"
```

---

## Task 2: Backend wrapper — `prepare_bind` endpoint

**Rationale:** We call ECPay's `GetTokenbyBindingCard` directly via `ECPGClient`, passing our own URLs so 3DS lands back on OWL. Replaces the stub at `src/controllers/billing.py:119` (which currently returns `ecpay_required` error).

**Files:**
- Modify: `src/controllers/billing.py`
- Test: `src/tests/test_payment_methods_api.py` (create)

**Step 1: Write the failing test**

Create `src/tests/test_payment_methods_api.py`:

```python
"""Tests for /api/billing/payment-methods/* wrapper endpoints."""
from unittest.mock import patch, MagicMock

from odoo.tests.common import HttpCase


class TestPaymentMethodsApi(HttpCase):

    def setUp(self):
        super().setUp()
        self.provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not self.provider:
            self.skipTest("ECPay provider not configured in this DB")
        self.provider.write({
            'enable_ecpg': True,
            'MerchantID': '3002607',
            'HashKey': 'pwFHCqoQZGmho4w6',
            'HashIV': 'EkRm7iFT261dpevs',
            'state': 'test',
        })
        self.authenticate('admin', 'admin')

    def test_prepare_bind_returns_token(self):
        """prepare_bind calls ECPGClient.get_token_by_binding_card and returns
        token + member_id + trade_no + merchant_id + sandbox."""
        fake_result = {
            'RtnCode': 1, 'RtnMsg': 'Success',
            'Token': 'FAKE_TOKEN_ABC123',
            'TokenExpireDate': '2026/12/31 23:59:59',
        }
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.get_token_by_binding_card',
            return_value=fake_result,
        ) as mock_get_token:
            response = self.url_open(
                '/api/billing/payment-methods/prepare-bind',
                data='{"jsonrpc":"2.0","method":"call","params":{}}',
                headers={'Content-Type': 'application/json'},
            )
            body = response.json()['result']

        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.assertEqual(body['data']['token'], 'FAKE_TOKEN_ABC123')
        self.assertIn('member_id', body['data'])
        self.assertIn('trade_no', body['data'])
        self.assertEqual(body['data']['merchant_id'], '3002607')
        self.assertTrue(body['data']['sandbox'])
        # Verify our custom URLs were passed to SDK
        kwargs = mock_get_token.call_args.kwargs
        self.assertIn('/woow#/settings/payment-methods', kwargs['order_result_url'])

    def test_prepare_bind_no_provider_returns_error(self):
        """If no ECPG-enabled provider exists, endpoint returns provider_not_found."""
        self.provider.enable_ecpg = False
        response = self.url_open(
            '/api/billing/payment-methods/prepare-bind',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'provider_not_found')
```

Add `from . import test_payment_methods_api` to `src/tests/__init__.py`.

**Step 2: Run the test (expect FAIL — endpoint not implemented)**

Trust the structure; tests run via the project's staging/docker infra.

**Step 3: Implement `prepare_bind` endpoint**

In `src/controllers/billing.py`, find the existing stub section (starts around line 88 with the `# ==================== Payment Method API ====================` comment). Keep `api_payment_methods` (the GET list, will be rewired in Task 4). Replace `api_payment_method_add` (currently returns `ecpay_required`) with a new richer endpoint.

First, near the imports at the top of `billing.py`, add a helper:

```python
from datetime import datetime


def _find_ecpg_provider(env):
    """Return the first ECPay provider with enable_ecpg=True, or False."""
    return env['payment.provider'].sudo().search(
        [('code', '=', 'ECPay'), ('enable_ecpg', '=', True)], limit=1,
    )
```

Then replace the old `api_payment_method_add` with:

```python
    @route("/api/billing/payment-methods/prepare-bind", auth="user", methods=["POST"], type="json")
    def api_payment_method_prepare_bind(self, **kw) -> dict[str, Any]:
        """Step 1 of binding flow. Returns ECPay Token for JS SDK to collect the card.

        3DS OrderResultURL is set to our OWL Payment Methods page so the user lands
        back on `#/settings/payment-methods?bind_status=ok&trade_no=...` after ECPay
        completes the binding.
        """
        from odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client import ECPGClient, ECPGError

        provider = _find_ecpg_provider(request.env)
        if not provider:
            return {
                'success': False,
                'error': 'No ECPay provider with ECPG enabled',
                'code': 'provider_not_found',
            }

        partner = request.env.user.partner_id
        member_id = provider._ecpg_build_member_id(partner)
        trade_no = request.env['payment.transaction']._ecpg_build_bind_trade_no()

        client = ECPGClient(
            provider.MerchantID, provider.HashKey, provider.HashIV,
            sandbox=(provider.state != 'enabled'),
        )
        # Use web.base.url (not request.httprequest.url_root) so ECPay always gets
        # https:// even when the app is behind a TLS-terminating proxy.
        base = (request.env['ir.config_parameter'].sudo().get_param('web.base.url')
                or request.httprequest.url_root).rstrip('/')
        consumer_info = {
            'MerchantMemberID': member_id,
            'Email': partner.email or '',
            'Phone': partner.phone or '',
            'Name': partner.name or '',
            'CountryCode': '158',
        }
        order_info = {
            'MerchantTradeDate': datetime.now().strftime('%Y/%m/%d %H:%M:%S'),
            'MerchantTradeNo': trade_no,
            'TotalAmount': '5',
            'TradeDesc': '綁卡驗證',
            'ItemName': '綁卡',
            # Reuse ecpay's bind_callback so it persists payment.token for us.
            'ReturnURL': f'{base}/payment/ecpg/bind_callback',
        }
        try:
            result = client.get_token_by_binding_card(
                consumer_info=consumer_info,
                order_info=order_info,
                order_result_url=(
                    f'{base}/woow#/settings/payment-methods'
                    f'?bind_status=ok&trade_no={trade_no}'
                ),
            )
        except ECPGError as e:
            _logger.exception('ECPG prepare_bind failed: %s', e)
            return {'success': False, 'error': str(e), 'code': 'sdk_error'}

        if result.get('RtnCode') != 1 or not result.get('Token'):
            return {
                'success': False,
                'error': result.get('RtnMsg') or 'Unknown ECPay error',
                'code': 'ecpay_rejected',
            }

        return {
            'success': True,
            'data': {
                'token': result['Token'],
                'expire_date': result.get('TokenExpireDate'),
                'member_id': member_id,
                'trade_no': trade_no,
                'merchant_id': provider.MerchantID,
                'sandbox': provider.state != 'enabled',
            },
        }
```

Preserve the old `api_payment_method_add` route as a deprecated alias that just calls `api_payment_method_prepare_bind`, or simply delete it — the existing frontend only calls `/api/billing/payment-methods/add` via `billingService.addPaymentMethod()`, and that method is about to be rewired in Task 5. **Delete the old stub** to avoid confusion.

**Step 4: Run the test (expect PASS)**

**Step 5: Commit**

```bash
git add src/controllers/billing.py src/tests/test_payment_methods_api.py src/tests/__init__.py
git commit -m "feat: add /api/billing/payment-methods/prepare-bind wrapping ECPG GetToken"
```

---

## Task 3: Backend wrapper — `create-bind` endpoint

**Rationale:** After the JS SDK returns a `BindCardPayToken`, the frontend POSTs it to us. We call `ECPGClient.create_bind_card` and surface the `three_d_url` so OWL can redirect.

**Files:**
- Modify: `src/controllers/billing.py`
- Modify: `src/tests/test_payment_methods_api.py` (add test)

**Step 1: Write the failing test**

Append to `test_payment_methods_api.py`:

```python
    def test_create_bind_returns_three_d_url(self):
        """create_bind calls CreateBindCard and returns ThreeDURL if 3DS required."""
        fake_result = {
            'RtnCode': 1, 'RtnMsg': 'Success',
            'BindCardID': 'BC12345',
            'ThreeDInfo': {'ThreeDURL': 'https://3ds.example/verify?id=xyz'},
        }
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.create_bind_card',
            return_value=fake_result,
        ):
            partner = self.env.user.partner_id
            member_id = self.provider._ecpg_build_member_id(partner)
            response = self.url_open(
                '/api/billing/payment-methods/create-bind',
                data=('{"jsonrpc":"2.0","method":"call","params":{'
                      f'"member_id":"{member_id}",'
                      '"trade_no":"BIND0001",'
                      '"bind_card_pay_token":"PAYTOKEN_XYZ"}}'),
                headers={'Content-Type': 'application/json'},
            )
            body = response.json()['result']

        self.assertTrue(body['success'])
        self.assertEqual(body['data']['three_d_url'], 'https://3ds.example/verify?id=xyz')
        self.assertEqual(body['data']['bind_card_id'], 'BC12345')

    def test_create_bind_member_id_mismatch(self):
        """Member ID from request must match the expected one for the current user."""
        response = self.url_open(
            '/api/billing/payment-methods/create-bind',
            data=('{"jsonrpc":"2.0","method":"call","params":{'
                  '"member_id":"WRONG",'
                  '"trade_no":"BIND0001",'
                  '"bind_card_pay_token":"PAYTOKEN_XYZ"}}'),
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'member_id_mismatch')
```

**Step 2: Implement the endpoint**

Add to `src/controllers/billing.py` right after `api_payment_method_prepare_bind`:

```python
    @route("/api/billing/payment-methods/create-bind", auth="user", methods=["POST"], type="json")
    def api_payment_method_create_bind(
        self,
        member_id: str | None = None,
        trade_no: str | None = None,
        bind_card_pay_token: str | None = None,
        **kw,
    ) -> dict[str, Any]:
        """Step 2 of binding flow. Consumes BindCardPayToken from the JS SDK and
        returns ThreeDURL (if 3DS is required) so the frontend can redirect."""
        from odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client import ECPGClient, ECPGError

        if not (member_id and trade_no and bind_card_pay_token):
            return {'success': False, 'error': 'missing parameters', 'code': 'missing_params'}

        provider = _find_ecpg_provider(request.env)
        if not provider:
            return {'success': False, 'error': 'No ECPG provider', 'code': 'provider_not_found'}

        partner = request.env.user.partner_id
        expected = provider._ecpg_build_member_id(partner)
        if member_id != expected:
            return {'success': False, 'error': 'member_id does not match session',
                    'code': 'member_id_mismatch'}

        client = ECPGClient(
            provider.MerchantID, provider.HashKey, provider.HashIV,
            sandbox=(provider.state != 'enabled'),
        )
        try:
            result = client.create_bind_card(
                member_id=member_id, bind_card_pay_token=bind_card_pay_token,
            )
        except ECPGError as e:
            _logger.exception('ECPG create_bind failed: %s', e)
            return {'success': False, 'error': str(e), 'code': 'sdk_error'}

        if result.get('RtnCode') != 1:
            return {
                'success': False,
                'error': result.get('RtnMsg') or 'ECPay rejected binding',
                'code': 'ecpay_rejected',
            }

        three_d_info = result.get('ThreeDInfo') or {}
        return {
            'success': True,
            'data': {
                'three_d_url': three_d_info.get('ThreeDURL') or '',
                'bind_card_id': result.get('BindCardID') or '',
            },
        }
```

**Step 3: Run tests (expect PASS)**

**Step 4: Commit**

```bash
git add src/controllers/billing.py src/tests/test_payment_methods_api.py
git commit -m "feat: add /api/billing/payment-methods/create-bind returning ThreeDURL"
```

---

## Task 4: Backend wrapper — list / delete / set-default (rewire existing stubs)

**Rationale:** The existing stubs in `billing.py` (`api_payment_methods`, `api_payment_method_delete`, `api_payment_method_set_default`) read from `res.partner.ecpay_card_*` fields. Rewire them to query `payment.token` with `ecpg_bindcard_id` and call the ecpay addon's helpers (or the SDK directly for delete).

**Files:**
- Modify: `src/controllers/billing.py` (replace 3 existing endpoint bodies)
- Modify: `src/tests/test_payment_methods_api.py` (add tests)

**Step 1: Write tests**

```python
    def test_list_cards_returns_payment_tokens(self):
        """List cards reads from payment.token with ecpg_bindcard_id set."""
        token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.env.user.partner_id.id,
            'payment_method_id': self.env.ref(
                'payment.payment_method_card', raise_if_not_found=False
            ).id if self.env.ref('payment.payment_method_card', raise_if_not_found=False) else False,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_TEST_1',
            'ecpg_bindcard_id': 'BC_TEST_1',
            'ecpg_member_id': 'M_1',
            'ecpg_card_expire': '1226',
            'ecpg_issuing_bank': 'TEST BANK',
        })
        response = self.url_open(
            '/api/billing/payment-methods',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertTrue(body['success'])
        ids = [c['id'] for c in body['data']]
        self.assertIn(token.id, ids)
        entry = next(c for c in body['data'] if c['id'] == token.id)
        self.assertEqual(entry['last4'], '4242')
        self.assertEqual(entry['expire'], '12/26')  # formatted MM/YY
        self.assertEqual(entry['bank'], 'TEST BANK')

    def test_delete_archives_token(self):
        token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.env.user.partner_id.id,
            'payment_method_id': self.env.ref('payment.payment_method_card').id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_TO_DELETE',
            'ecpg_bindcard_id': 'BC_TO_DELETE',
            'ecpg_member_id': 'M_1',
        })
        # Patch the SDK call that runs on archive
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.delete_member_bind_card',
            return_value={'RtnCode': 1},
        ):
            response = self.url_open(
                f'/api/billing/payment-methods/{token.id}/delete',
                data='{"jsonrpc":"2.0","method":"call","params":{}}',
                headers={'Content-Type': 'application/json'},
            )
        body = response.json()['result']
        self.assertTrue(body['success'])
        token.invalidate_recordset()
        self.assertFalse(token.active)
```

**Step 2: Implement rewired endpoints**

Replace the body of `api_payment_methods` (the existing list endpoint at `billing.py:~90`):

```python
    @route("/api/billing/payment-methods", auth="user", methods=["POST"], type="json")
    def api_payment_methods(self, **kw) -> dict[str, Any]:
        """List the current user's ECPG-bound cards (reads payment.token locally)."""
        partner = request.env.user.partner_id
        tokens = request.env['payment.token'].sudo().search([
            ('partner_id', '=', partner.id),
            ('ecpg_bindcard_id', '!=', False),
            ('active', '=', True),
        ])
        default_token_id = False
        if 'property_payment_token_id' in partner._fields:
            default_token = partner.with_company(
                partner.company_id or request.env.company
            ).sudo().property_payment_token_id
            default_token_id = default_token.id if default_token else False

        def _format_expire(raw: str) -> str:
            # raw is "YYMM" or "MMYY"; ECPay sends MMYY (e.g. "1226" = Dec 2026)
            if not raw or len(raw) != 4:
                return raw or ''
            return f"{raw[:2]}/{raw[2:]}"

        return {
            'success': True,
            'data': [{
                'id': t.id,
                'last4': (t.payment_details or '').replace('*', '').strip(),
                'expire': _format_expire(t.ecpg_card_expire or ''),
                'bank': t.ecpg_issuing_bank or '',
                'type': 'credit_card',
                'is_default': t.id == default_token_id,
            } for t in tokens],
        }
```

Replace `api_payment_method_delete`:

```python
    @route("/api/billing/payment-methods/<int:method_id>/delete",
           auth="user", methods=["POST"], type="json")
    def api_payment_method_delete(self, method_id: int, **kw) -> dict[str, Any]:
        token = request.env['payment.token'].sudo().browse(method_id)
        if not token.exists() or not token.ecpg_bindcard_id:
            return {'success': False, 'error': 'Token not found', 'code': 'not_found'}
        if token.partner_id != request.env.user.partner_id:
            return {'success': False, 'error': 'Not owner', 'code': 'not_owner'}
        # Archive triggers payment.token._handle_archiving() which calls ECPay DeleteMemberBindCard.
        token.active = False
        return {'success': True}
```

Replace `api_payment_method_set_default`:

```python
    @route("/api/billing/payment-methods/<int:method_id>/set-default",
           auth="user", methods=["POST"], type="json")
    def api_payment_method_set_default(self, method_id: int, **kw) -> dict[str, Any]:
        token = request.env['payment.token'].sudo().browse(method_id)
        if not token.exists() or not token.ecpg_bindcard_id:
            return {'success': False, 'error': 'Token not found', 'code': 'not_found'}
        if token.partner_id != request.env.user.partner_id:
            return {'success': False, 'error': 'Not owner', 'code': 'not_owner'}
        partner = token.partner_id
        if 'property_payment_token_id' in partner._fields:
            partner.with_company(
                partner.company_id or request.env.company
            ).sudo().property_payment_token_id = token
        return {'success': True}
```

**Note:** The old controller stubs' route patterns use `<string:method_id>`; switch to `<int:method_id>` because token IDs are integers.

**Step 3: Run tests (expect PASS)**

**Step 4: Commit**

```bash
git add src/controllers/billing.py src/tests/test_payment_methods_api.py
git commit -m "refactor: rewire payment-methods list/delete/set-default onto payment.token"
```

---

## Task 5: Frontend — rewire `billing_service.js` to new endpoints

**Rationale:** `billing_service.js` already has `fetchPaymentMethods`, `addPaymentMethod`, `deletePaymentMethod`, `setDefaultPaymentMethod`. We replace `addPaymentMethod` (which previously POSTed card data to `/add`) with two new methods: `preparePaymentMethodBind` and `createPaymentMethodBind`, matching the two-step ECPG flow. Existing three methods keep their public names.

**Files:**
- Modify: `src/static/src/paas/services/billing_service.js`

**Step 1: Replace `addPaymentMethod` with two methods**

In `billing_service.js`, locate `async addPaymentMethod(cardData)` (around line 242). Remove it. Add:

```javascript
    /**
     * Step 1 of ECPG binding flow. Returns ECPay Token + member_id + trade_no.
     * @returns {Promise<{success: boolean, data?: Object, error?: string, code?: string}>}
     */
    async preparePaymentMethodBind() {
        try {
            const result = await jsonRpc("/api/billing/payment-methods/prepare-bind", {});
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error, code: result.code };
        } catch (err) {
            return { success: false, error: err.message || "Prepare bind failed" };
        }
    },

    /**
     * Step 2 of ECPG binding flow. Consumes BindCardPayToken from the JS SDK.
     * Returns { three_d_url, bind_card_id }. Frontend must redirect if three_d_url is non-empty.
     * @param {Object} payload - { member_id, trade_no, bind_card_pay_token }
     * @returns {Promise<{success: boolean, data?: Object, error?: string, code?: string}>}
     */
    async createPaymentMethodBind(payload) {
        try {
            const result = await jsonRpc("/api/billing/payment-methods/create-bind", payload);
            if (result.success) {
                return { success: true, data: result.data };
            }
            return { success: false, error: result.error, code: result.code };
        } catch (err) {
            return { success: false, error: err.message || "Create bind failed" };
        }
    },
```

**Step 2: Verify existing methods still match new endpoints**

`fetchPaymentMethods` already POSTs to `/api/billing/payment-methods` (unchanged — Task 4 rewired the backend, not the URL). Response shape changed: items now carry `{id, last4, expire, bank, type, is_default}` instead of the old shape. Check if any consumer of `billing.paymentMethods` reads removed fields. Currently only `ManagePaymentMethodsModal.js` reads them:

```bash
grep -n "paymentMethods\[" src/static/src/paas/components/modal/ManagePaymentMethodsModal.js
```

Update `ManagePaymentMethodsModal.js` / `.xml` to use the new field names **only if reconnected** — it's currently unused since Phase 1 (Task 1 confirmed). Leave it for now; Task 10 handles reconnection.

**Step 3: Commit**

```bash
git add src/static/src/paas/services/billing_service.js
git commit -m "feat: add preparePaymentMethodBind/createPaymentMethodBind to billing service"
```

---

## Task 6: Frontend — add routes for payment methods pages

**Rationale:** Router declares two new routes: `#/settings/payment-methods` (list) and `#/settings/payment-methods/add` (binding). `root.xml` maps each to its page component.

**Files:**
- Modify: `src/static/src/paas/core/router.js`
- Modify: `src/static/src/paas/root.js`
- Modify: `src/static/src/paas/root.xml`
- Modify: `src/static/src/paas/layout/sidebar/Sidebar.js`

**Step 1: Add routes to `router.js`**

Add after `settings` entry:
```javascript
    { path: "settings/payment-methods", name: "Payment Methods", routeName: "settings-payment-methods" },
    { path: "settings/payment-methods/add", name: "Add Payment Method", routeName: "settings-payment-methods-add" },
```

**Step 2: Import pages in `root.js`**

Add imports (placeholder — pages don't exist yet; Tasks 7 & 8 create them):
```javascript
import { PaymentMethodsListPage } from "./pages/settings/PaymentMethodsListPage";
import { AddPaymentMethodPage } from "./pages/settings/AddPaymentMethodPage";
```

Register in `static components`:
```javascript
        PaymentMethodsListPage,
        AddPaymentMethodPage,
```

**Step 3: Add route handlers in `root.xml`**

Insert before the final `<t t-else=""><EmptyState/></t>`:

```xml
            <t t-elif="router.current === 'settings-payment-methods'">
                <PaymentMethodsListPage/>
            </t>
            <t t-elif="router.current === 'settings-payment-methods-add'">
                <AddPaymentMethodPage/>
            </t>
```

**Step 4: Update Sidebar `isActive` to keep "Settings" highlighted on sub-routes**

In `Sidebar.js`, locate the `if (item.path === "settings")` block — if missing, add it:
```javascript
        if (item.path === "settings") {
            return current === "settings"
                || current === "settings-payment-methods"
                || current === "settings-payment-methods-add";
        }
```

If no such block exists yet, place it before the final `return current === item.path;`.

**Step 5: Commit**

This commit will fail to load the OWL app until Tasks 7 & 8 are done (because the imports reference non-existent modules). Commit **AFTER** Tasks 7 & 8 if you want to keep the tree buildable at every commit. Otherwise commit here and fix forward.

**Decision**: Defer commit to end of Task 8 (we commit routes + both pages together in Task 8). Keep changes uncommitted for now.

---

## Task 7: Frontend — create `PaymentMethodsListPage`

**Rationale:** Lists bound cards, offers Add / Delete / Set Default actions. On mount, reads URL query `bind_status=ok` to show a success toast after 3DS redirect.

**Files:**
- Create: `src/static/src/paas/pages/settings/PaymentMethodsListPage.js`
- Create: `src/static/src/paas/pages/settings/PaymentMethodsListPage.xml`
- Create: `src/static/src/paas/pages/settings/PaymentMethodsListPage.scss`

**Step 1: Create `PaymentMethodsListPage.js`**

```javascript
/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

/**
 * PaymentMethodsListPage — lists ECPG-bound cards, supports delete & set default.
 * On mount, reads URL query for post-3DS status (`bind_status=ok`) to surface a toast.
 */
export class PaymentMethodsListPage extends Component {
    static template = "woow_paas_platform.PaymentMethodsListPage";
    static components = { WoowCard, WoowButton, WoowIcon };
    static props = {};

    setup() {
        this.router = router;
        this.billing = useState(billingService);
        this.state = useState({
            bindSuccess: false,
            deleting: null,       // token id being deleted
            settingDefault: null, // token id being set default
            error: null,
        });

        onMounted(async () => {
            // Detect post-3DS redirect
            const hash = window.location.hash;
            if (hash.includes("bind_status=ok")) {
                this.state.bindSuccess = true;
                // Clean the query so reload doesn't retrigger
                const cleanHash = hash.split("?")[0];
                window.history.replaceState(null, "", cleanHash);
            }
            await billingService.fetchPaymentMethods();
        });
    }

    get cards() {
        return this.billing.paymentMethods;
    }

    get isLoading() {
        return this.billing.paymentMethodsLoading;
    }

    goToAdd() {
        this.router.navigate("settings/payment-methods/add");
    }

    async onDelete(tokenId) {
        if (!confirm("Remove this card?")) return;
        this.state.deleting = tokenId;
        this.state.error = null;
        const result = await billingService.deletePaymentMethod(tokenId);
        if (!result.success) {
            this.state.error = result.error || "Delete failed";
        }
        this.state.deleting = null;
    }

    async onSetDefault(tokenId) {
        this.state.settingDefault = tokenId;
        this.state.error = null;
        const result = await billingService.setDefaultPaymentMethod(tokenId);
        if (result.success) {
            await billingService.fetchPaymentMethods();
        } else {
            this.state.error = result.error || "Set default failed";
        }
        this.state.settingDefault = null;
    }

    dismissBindSuccess() {
        this.state.bindSuccess = false;
    }
}
```

**Step 2: Create `PaymentMethodsListPage.xml`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
    <t t-name="woow_paas_platform.PaymentMethodsListPage">
        <div class="o_woow_payment_methods_page">
            <div class="o_woow_page_header">
                <div class="o_woow_breadcrumb">
                    <a href="#/settings">Settings</a>
                    <span class="o_woow_breadcrumb_sep">›</span>
                    <span>Payment Methods</span>
                </div>
                <h1>Payment Methods</h1>
                <p>Manage credit cards that auto-topup your wallet when balance runs low.</p>
            </div>

            <t t-if="state.bindSuccess">
                <div class="o_woow_success_banner" role="alert">
                    <WoowIcon name="'check_circle'" size="24"/>
                    <span>Card bound successfully.</span>
                    <button class="o_woow_success_close" t-on-click="dismissBindSuccess">
                        <WoowIcon name="'close'" size="16"/>
                    </button>
                </div>
            </t>

            <t t-if="state.error">
                <div class="o_woow_error_banner" role="alert">
                    <WoowIcon name="'error'" size="20"/>
                    <span t-esc="state.error"/>
                </div>
            </t>

            <t t-if="isLoading">
                <div class="o_woow_loading">Loading payment methods…</div>
            </t>

            <t t-else="">
                <t t-if="cards.length === 0">
                    <WoowCard shadow="true" class="'o_woow_payment_methods_empty'">
                        <div class="o_woow_empty_body">
                            <WoowIcon name="'credit_card_off'" size="48"/>
                            <h3>No cards bound yet</h3>
                            <p>Add a credit card to enable auto-topup for subscription renewals.</p>
                            <WoowButton variant="'primary'" size="'md'" onClick.bind="goToAdd">
                                + Add Payment Method
                            </WoowButton>
                        </div>
                    </WoowCard>
                </t>

                <t t-else="">
                    <div class="o_woow_payment_methods_list">
                        <t t-foreach="cards" t-as="card" t-key="card.id">
                            <WoowCard shadow="true" class="'o_woow_payment_method_card'">
                                <div class="o_woow_card_body">
                                    <div class="o_woow_card_info">
                                        <WoowIcon name="'credit_card'" size="28"/>
                                        <div class="o_woow_card_meta">
                                            <div class="o_woow_card_number">
                                                •••• <t t-esc="card.last4"/>
                                            </div>
                                            <div class="o_woow_card_details">
                                                <span t-esc="card.bank"/>
                                                <span t-if="card.expire">• Exp <t t-esc="card.expire"/></span>
                                                <span t-if="card.is_default" class="o_woow_default_badge">Default</span>
                                            </div>
                                        </div>
                                    </div>
                                    <div class="o_woow_card_actions">
                                        <WoowButton
                                            t-if="!card.is_default"
                                            variant="'secondary'"
                                            size="'sm'"
                                            disabled="state.settingDefault === card.id"
                                            onClick="() => this.onSetDefault(card.id)">
                                            Set Default
                                        </WoowButton>
                                        <WoowButton
                                            variant="'danger'"
                                            size="'sm'"
                                            disabled="state.deleting === card.id"
                                            onClick="() => this.onDelete(card.id)">
                                            Remove
                                        </WoowButton>
                                    </div>
                                </div>
                            </WoowCard>
                        </t>
                    </div>

                    <div class="o_woow_payment_methods_add">
                        <WoowButton variant="'primary'" size="'md'" onClick.bind="goToAdd">
                            + Add Another Card
                        </WoowButton>
                    </div>
                </t>
            </t>
        </div>
    </t>
</templates>
```

**Step 3: Create `PaymentMethodsListPage.scss`**

```scss
.o_woow_payment_methods_page {
    padding: 1.5rem 2rem;
    max-width: 60rem;
    margin: 0 auto;

    .o_woow_page_header {
        margin-bottom: 1.5rem;

        .o_woow_breadcrumb {
            font-size: 0.8125rem;
            color: $woow-text-secondary;
            margin-bottom: 0.5rem;

            a {
                color: $woow-text-secondary;
                text-decoration: none;
                &:hover { color: $woow-primary; }
            }
            .o_woow_breadcrumb_sep {
                margin: 0 0.5rem;
                opacity: 0.6;
            }
        }

        h1 {
            font-size: 1.75rem;
            font-weight: 600;
            margin: 0 0 0.5rem 0;
            color: $woow-text-primary;
        }
        p {
            margin: 0;
            color: $woow-text-secondary;
        }
    }
}

.o_woow_success_banner {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    background: #d1fae5;
    border: 1px solid #6ee7b7;
    border-radius: 0.5rem;
    margin-bottom: 1rem;
    color: #065f46;

    .o_woow_success_close {
        margin-left: auto;
        border: none;
        background: transparent;
        cursor: pointer;
        color: inherit;
    }
}

.o_woow_error_banner {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.75rem 1rem;
    background: #fee2e2;
    border: 1px solid #fca5a5;
    border-radius: 0.5rem;
    margin-bottom: 1rem;
    color: #991b1b;
}

.o_woow_loading {
    padding: 3rem;
    text-align: center;
    color: $woow-text-secondary;
}

.o_woow_payment_methods_empty {
    .o_woow_empty_body {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 0.75rem;
        padding: 3rem 1.5rem;
        text-align: center;

        h3 {
            margin: 0;
            color: $woow-text-primary;
        }
        p {
            margin: 0 0 0.5rem 0;
            color: $woow-text-secondary;
        }
    }
}

.o_woow_payment_methods_list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-bottom: 1.5rem;
}

.o_woow_payment_method_card {
    .o_woow_card_body {
        display: flex;
        align-items: center;
        gap: 1rem;
        padding: 1rem 1.25rem;
    }
    .o_woow_card_info {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        flex: 1;
    }
    .o_woow_card_meta {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
    }
    .o_woow_card_number {
        font-family: monospace;
        font-size: 1rem;
        font-weight: 600;
        color: $woow-text-primary;
    }
    .o_woow_card_details {
        display: flex;
        gap: 0.5rem;
        font-size: 0.8125rem;
        color: $woow-text-secondary;
    }
    .o_woow_default_badge {
        padding: 0.125rem 0.5rem;
        background: rgba($woow-primary, 0.1);
        color: $woow-primary;
        border-radius: $woow-radius-full;
        font-size: 0.75rem;
        font-weight: 500;
    }
    .o_woow_card_actions {
        display: flex;
        gap: 0.5rem;
    }
}

.o_woow_payment_methods_add {
    display: flex;
    justify-content: center;
}
```

**Step 4: Do NOT commit yet — Task 8 adds the second page and we commit both together.**

---

## Task 8: Frontend — create `AddPaymentMethodPage`

**Rationale:** Dynamically loads jQuery → node-forge → ECPay SDK, calls `prepare-bind`, renders the SDK card form into `<div id="ECPayPayment">`, invokes `getBindCardPayToken`, POSTs to `create-bind`, redirects to ThreeDURL.

**Files:**
- Create: `src/static/src/paas/pages/settings/AddPaymentMethodPage.js`
- Create: `src/static/src/paas/pages/settings/AddPaymentMethodPage.xml`
- Create: `src/static/src/paas/pages/settings/AddPaymentMethodPage.scss`

**Step 1: Create `AddPaymentMethodPage.js`**

```javascript
/** @odoo-module **/
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowButton } from "../../components/button/WoowButton";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

/**
 * SDK script URLs. Order matters: jquery → node-forge → ecpay sdk.
 * These are ECPay's canonical CDN URLs (see payment_ecpay_ecpg/views/payment_ecpg_templates.xml).
 */
const SDK_SCRIPTS = [
    "https://code.jquery.com/jquery-3.7.1.min.js",
    "https://cdn.jsdelivr.net/npm/node-forge@0.7.0/dist/forge.min.js",
    "https://ecpg.ecpay.com.tw/Scripts/sdk-1.0.0.js",
];

/**
 * Inject <script> tags sequentially (ECPay SDK requires strict load order).
 * @returns {Promise<void>}
 */
async function loadEcpaySdk() {
    for (const url of SDK_SCRIPTS) {
        if (document.querySelector(`script[src="${url}"]`)) continue;
        await new Promise((resolve, reject) => {
            const s = document.createElement("script");
            s.src = url;
            s.async = false;
            s.onload = resolve;
            s.onerror = () => reject(new Error(`Failed to load ${url}`));
            document.head.appendChild(s);
        });
    }
    if (typeof window.ECPay === "undefined") {
        throw new Error("ECPay SDK did not expose window.ECPay after load");
    }
}

export class AddPaymentMethodPage extends Component {
    static template = "woow_paas_platform.AddPaymentMethodPage";
    static components = { WoowCard, WoowButton, WoowIcon };
    static props = {};

    setup() {
        this.router = router;
        this.state = useState({
            phase: "loading",     // loading | ready | submitting | error
            error: null,
            bindContext: null,    // {token, member_id, trade_no, merchant_id, sandbox}
        });

        onMounted(async () => {
            try {
                // Step 1: load SDK scripts
                await loadEcpaySdk();

                // Step 2: ask backend for a Token
                const prep = await billingService.preparePaymentMethodBind();
                if (!prep.success) {
                    throw new Error(prep.error || "Prepare bind failed");
                }
                this.state.bindContext = prep.data;

                // Step 3: initialize SDK env + render card form into #ECPayPayment
                const envi = prep.data.sandbox ? "Stage" : "Prod";
                await new Promise((resolve, reject) => {
                    window.ECPay.initialize(envi, 1, (errMsg) => {
                        if (errMsg) reject(new Error("SDK init failed: " + errMsg));
                        else resolve();
                    });
                });
                await new Promise((resolve, reject) => {
                    window.ECPay.addBindingCard(prep.data.token, "zh-TW", (errMsg) => {
                        if (errMsg) reject(new Error("Card form failed: " + errMsg));
                        else resolve();
                    });
                });
                this.state.phase = "ready";
            } catch (err) {
                console.error("AddPaymentMethodPage setup error:", err);
                this.state.phase = "error";
                this.state.error = err.message || String(err);
            }
        });

        onWillUnmount(() => {
            // Best-effort: clear the SDK's DOM container so re-entries start clean.
            const el = document.getElementById("ECPayPayment");
            if (el) el.innerHTML = "";
        });
    }

    goBack() {
        this.router.navigate("settings/payment-methods");
    }

    async onConfirm() {
        if (this.state.phase !== "ready") return;
        this.state.phase = "submitting";
        this.state.error = null;

        try {
            // Get BindCardPayToken from SDK
            const payToken = await new Promise((resolve, reject) => {
                window.ECPay.getBindCardPayToken((result, errMsg) => {
                    if (errMsg) return reject(new Error("PayToken failed: " + errMsg));
                    const t = result && result.BindCardPayToken;
                    if (!t) return reject(new Error("Invalid BindCardPayToken"));
                    resolve(t);
                });
            });

            // Send to backend
            const ctx = this.state.bindContext;
            const result = await billingService.createPaymentMethodBind({
                member_id: ctx.member_id,
                trade_no: ctx.trade_no,
                bind_card_pay_token: payToken,
            });
            if (!result.success) {
                throw new Error(result.error || "Bind rejected");
            }

            // Handle 3DS redirect
            if (result.data.three_d_url) {
                window.location.href = result.data.three_d_url;
                return; // page unloads
            }
            // No 3DS (unusual as of 2025-08 when 3DS 2.0 is mandatory)
            this.router.navigate("settings/payment-methods?bind_status=ok");
        } catch (err) {
            console.error("AddPaymentMethodPage confirm error:", err);
            this.state.phase = "ready";
            this.state.error = err.message || String(err);
        }
    }
}
```

**Step 2: Create `AddPaymentMethodPage.xml`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<templates xml:space="preserve">
    <t t-name="woow_paas_platform.AddPaymentMethodPage">
        <div class="o_woow_add_payment_method_page">
            <div class="o_woow_page_header">
                <div class="o_woow_breadcrumb">
                    <a href="#/settings">Settings</a>
                    <span class="o_woow_breadcrumb_sep">›</span>
                    <a href="#/settings/payment-methods">Payment Methods</a>
                    <span class="o_woow_breadcrumb_sep">›</span>
                    <span>Add</span>
                </div>
                <h1>Add Payment Method</h1>
                <p>Enter your card details. 3D Secure verification may be required.</p>
            </div>

            <WoowCard shadow="true" class="'o_woow_add_card_container'">
                <div class="o_woow_add_card_body">
                    <t t-if="state.phase === 'loading'">
                        <div class="o_woow_add_loading">
                            <WoowIcon name="'sync'" size="24" class="'o_woow_spin'"/>
                            <span>Loading payment form…</span>
                        </div>
                    </t>

                    <t t-if="state.phase === 'error'">
                        <div class="o_woow_add_error" role="alert">
                            <WoowIcon name="'error'" size="24"/>
                            <div class="o_woow_add_error_text">
                                <strong>Failed to load payment form.</strong>
                                <p t-esc="state.error"/>
                            </div>
                        </div>
                        <WoowButton variant="'secondary'" size="'md'" onClick.bind="goBack">
                            Back
                        </WoowButton>
                    </t>

                    <!-- SDK renders card form here. ID is required by ECPay SDK; do not rename. -->
                    <div id="ECPayPayment"
                         t-att-class="state.phase === 'error' ? 'o_woow_sdk_container d-none' : 'o_woow_sdk_container'"></div>

                    <t t-if="state.phase === 'ready' or state.phase === 'submitting'">
                        <t t-if="state.error">
                            <div class="o_woow_inline_error" role="alert">
                                <WoowIcon name="'error'" size="16"/>
                                <span t-esc="state.error"/>
                            </div>
                        </t>
                        <div class="o_woow_add_actions">
                            <WoowButton
                                variant="'secondary'"
                                size="'md'"
                                disabled="state.phase === 'submitting'"
                                onClick.bind="goBack">
                                Cancel
                            </WoowButton>
                            <WoowButton
                                variant="'primary'"
                                size="'md'"
                                disabled="state.phase === 'submitting'"
                                onClick.bind="onConfirm">
                                <t t-if="state.phase === 'submitting'">Verifying…</t>
                                <t t-else="">Bind Card</t>
                            </WoowButton>
                        </div>
                    </t>
                </div>
            </WoowCard>
        </div>
    </t>
</templates>
```

**Step 3: Create `AddPaymentMethodPage.scss`**

```scss
.o_woow_add_payment_method_page {
    padding: 1.5rem 2rem;
    max-width: 48rem;
    margin: 0 auto;

    .o_woow_page_header {
        margin-bottom: 1.5rem;

        .o_woow_breadcrumb {
            font-size: 0.8125rem;
            color: $woow-text-secondary;
            margin-bottom: 0.5rem;

            a {
                color: $woow-text-secondary;
                text-decoration: none;
                &:hover { color: $woow-primary; }
            }
            .o_woow_breadcrumb_sep { margin: 0 0.5rem; opacity: 0.6; }
        }
        h1 {
            font-size: 1.75rem;
            font-weight: 600;
            margin: 0 0 0.5rem 0;
            color: $woow-text-primary;
        }
        p { margin: 0; color: $woow-text-secondary; }
    }
}

.o_woow_add_card_container {
    .o_woow_add_card_body {
        padding: 1.5rem;
    }
}

.o_woow_sdk_container {
    min-height: 24rem;
    margin-bottom: 1rem;
}

.o_woow_add_loading {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
    padding: 3rem;
    color: $woow-text-secondary;
}

.o_woow_spin {
    animation: woow-spin 1s linear infinite;
}

@keyframes woow-spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
}

.o_woow_add_error {
    display: flex;
    gap: 0.75rem;
    padding: 1rem;
    background: #fee2e2;
    border: 1px solid #fca5a5;
    border-radius: 0.5rem;
    color: #991b1b;
    margin-bottom: 1rem;

    .o_woow_add_error_text {
        flex: 1;
        strong { display: block; margin-bottom: 0.25rem; }
        p { margin: 0; font-size: 0.875rem; }
    }
}

.o_woow_inline_error {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    background: #fee2e2;
    border: 1px solid #fca5a5;
    border-radius: 0.375rem;
    color: #991b1b;
    font-size: 0.875rem;
    margin-bottom: 1rem;
}

.o_woow_add_actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.75rem;
}
```

**Step 4: Commit Task 6 + 7 + 8 together**

```bash
git add src/static/src/paas/core/router.js \
        src/static/src/paas/root.js \
        src/static/src/paas/root.xml \
        src/static/src/paas/layout/sidebar/Sidebar.js \
        src/static/src/paas/pages/settings/PaymentMethodsListPage.js \
        src/static/src/paas/pages/settings/PaymentMethodsListPage.xml \
        src/static/src/paas/pages/settings/PaymentMethodsListPage.scss \
        src/static/src/paas/pages/settings/AddPaymentMethodPage.js \
        src/static/src/paas/pages/settings/AddPaymentMethodPage.xml \
        src/static/src/paas/pages/settings/AddPaymentMethodPage.scss
git commit -m "feat: add Payment Methods list + Add pages with ECPay SDK integration"
```

---

## Task 9: Enable Payment Methods card in SettingsPage

**Rationale:** Remove the `o_woow_settings_card_disabled` class, wire `onClick` to navigate to `#/settings/payment-methods`, drop the "Coming Soon" badge.

**Files:**
- Modify: `src/static/src/paas/pages/settings/SettingsPage.js`
- Modify: `src/static/src/paas/pages/settings/SettingsPage.xml`

**Step 1: Add navigation in `SettingsPage.js`**

Replace the class body:
```javascript
/** @odoo-module **/
import { Component } from "@odoo/owl";
import { WoowCard } from "../../components/card/WoowCard";
import { WoowIcon } from "../../components/icon/WoowIcon";
import { router } from "../../core/router";

export class SettingsPage extends Component {
    static template = "woow_paas_platform.SettingsPage";
    static components = { WoowCard, WoowIcon };
    static props = {};

    setup() {
        this.router = router;
    }

    goToPaymentMethods() {
        this.router.navigate("settings/payment-methods");
    }
}
```

**Step 2: Update `SettingsPage.xml`**

Replace the Payment Methods card block:

```xml
                <WoowCard shadow="true" class="'o_woow_settings_card'">
                    <div class="o_woow_settings_card_body"
                         style="cursor: pointer;"
                         t-on-click="goToPaymentMethods">
                        <div class="o_woow_settings_card_icon">
                            <WoowIcon name="'credit_card'" size="32"/>
                        </div>
                        <div class="o_woow_settings_card_text">
                            <div class="o_woow_settings_card_title_row">
                                <h3>Payment Methods</h3>
                            </div>
                            <p>Manage credit cards that auto-topup your wallet when balance runs low.</p>
                        </div>
                        <div class="o_woow_settings_card_arrow">
                            <WoowIcon name="'chevron_right'" size="24"/>
                        </div>
                    </div>
                </WoowCard>
```

Removed:
- `o_woow_settings_card_disabled` class
- "Coming Soon" badge span

Added:
- `style="cursor: pointer;"` on the clickable body (acceptable inline style for single property; matches pattern used in `AddFundsModal.xml` for the hover affordance)
- `t-on-click="goToPaymentMethods"`
- `chevron_right` trailing icon to signal navigation

**Step 3: Update `SettingsPage.scss`**

Add styles for the trailing chevron:

```scss
.o_woow_settings_card {
    &_arrow {
        margin-left: auto;
        color: $woow-text-secondary;
    }
    &:hover {
        background: rgba($woow-primary, 0.03);
    }
}
```

Keep the existing `_disabled` rule in case a future Phase-2b placeholder reuses it.

**Step 4: Commit**

```bash
git add src/static/src/paas/pages/settings/SettingsPage.js \
        src/static/src/paas/pages/settings/SettingsPage.xml \
        src/static/src/paas/pages/settings/SettingsPage.scss
git commit -m "feat: link Settings card to Payment Methods page"
```

---

## Task 10: Reconnect `ManagePaymentMethodsModal` to BillingOverview

**Rationale:** The Phase 1 design kept `ManagePaymentMethodsModal` as an unused file for Phase 2 pickup. Re-import it into `BillingOverviewPage` as a quick-access modal, update the modal to use the new `billingService` API shape (`id`, `last4`, `is_default`).

**Files:**
- Modify: `src/static/src/paas/components/modal/ManagePaymentMethodsModal.js` (update field names)
- Modify: `src/static/src/paas/components/modal/ManagePaymentMethodsModal.xml` (update field names)
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.js` (import + state)
- Modify: `src/static/src/paas/pages/billing/BillingOverviewPage.xml` (add "Manage" button)

**Step 1: Update `ManagePaymentMethodsModal.js`**

The existing modal reads `method.last4`, `method.is_default`, `method.type`, `method.id`. The new API already returns `{id, last4, is_default, type}`. Two adjustments:

1. Delete the `AddCardModal` and `CardAddedSuccessModal` sub-modal wiring — in Phase 2 we no longer use them; binding happens in a full page.
2. Change the `onAddCard` handler from opening `AddCardModal` to navigating to `#/settings/payment-methods/add`.

Full replacement:

```javascript
/** @odoo-module **/
import { Component, useState, onMounted } from "@odoo/owl";
import { WoowButton } from "../button/WoowButton";
import { WoowIcon } from "../icon/WoowIcon";
import { billingService } from "../../services/billing_service";
import { router } from "../../core/router";

/**
 * ManagePaymentMethodsModal — quick-access modal from BillingOverview.
 * Lists bound cards, supports delete. Adding a card routes to the full page.
 */
export class ManagePaymentMethodsModal extends Component {
    static template = "woow_paas_platform.ManagePaymentMethodsModal";
    static components = { WoowButton, WoowIcon };
    static props = {
        onClose: { type: Function },
        onUpdated: { type: Function },
    };

    setup() {
        this.router = router;
        this.billing = useState(billingService);
        this.state = useState({
            deleting: false,
            deleteError: null,
        });

        onMounted(() => {
            billingService.fetchPaymentMethods();
        });
    }

    get methods() {
        return this.billing.paymentMethods;
    }

    get defaultMethod() {
        return this.methods.find((m) => m.is_default) || null;
    }

    get otherMethods() {
        return this.methods.filter((m) => !m.is_default);
    }

    get isLoading() {
        return this.billing.paymentMethodsLoading;
    }

    onBackdropClick(ev) {
        if (ev.target === ev.currentTarget && !this.state.deleting) {
            this.props.onClose();
        }
    }

    onAddCard() {
        this.props.onClose();
        this.router.navigate("settings/payment-methods/add");
    }

    async onDeleteMethod(methodId) {
        if (this.state.deleting) return;
        if (!confirm("Are you sure you want to remove this payment method?")) return;

        this.state.deleting = true;
        this.state.deleteError = null;

        const result = await billingService.deletePaymentMethod(methodId);

        if (result.success) {
            this.props.onUpdated();
        } else {
            this.state.deleteError = result.error || "Failed to remove card";
        }

        this.state.deleting = false;
    }
}
```

**Step 2: Update `ManagePaymentMethodsModal.xml`**

Remove the `AddCardModal` + `CardAddedSuccessModal` sub-modal blocks at the end. Keep the main modal content. Adjust type badge lookup:

```xml
<span class="o_woow_manage_pm_type_badge">
    <t t-esc="defaultMethod.type === 'credit_card' ? 'CARD' : (defaultMethod.type || '').toUpperCase()"/>
</span>
```

Do the same for `otherMethods` loop.

Verify the final `<t t-if="state.showAddCardModal">` and `<t t-if="state.showCardAddedSuccess">` blocks are REMOVED (no longer in state).

**Step 3: Wire modal into BillingOverviewPage**

In `BillingOverviewPage.js`, add import + state:
```javascript
import { ManagePaymentMethodsModal } from "../../components/modal/ManagePaymentMethodsModal";
```

Register in `static components = { ..., ManagePaymentMethodsModal }`.

Add state:
```javascript
this.state = useState({
    ...existing,
    showManagePaymentMethods: false,
});
```

Add methods:
```javascript
openManagePaymentMethods() {
    this.state.showManagePaymentMethods = true;
}
closeManagePaymentMethods() {
    this.state.showManagePaymentMethods = false;
}
onPaymentMethodsUpdated() {
    // refresh wallet card info if it depends on cardBound
    billingService.fetchWallet();
}
```

In `BillingOverviewPage.xml`, add a "Manage" button next to `getPaymentMethodLabel()` rendering (in the Subscription & Payment card). Add the modal at the end of the root template:

```xml
<t t-if="state.showManagePaymentMethods">
    <ManagePaymentMethodsModal
        onClose.bind="closeManagePaymentMethods"
        onUpdated.bind="onPaymentMethodsUpdated"/>
</t>
```

Inline Manage button (within the Subscription card body, near the `getPaymentMethodLabel()` line):

```xml
<WoowButton
    variant="'link'"
    size="'sm'"
    onClick.bind="openManagePaymentMethods">
    Manage
</WoowButton>
```

**Step 4: Commit**

```bash
git add src/static/src/paas/components/modal/ManagePaymentMethodsModal.js \
        src/static/src/paas/components/modal/ManagePaymentMethodsModal.xml \
        src/static/src/paas/pages/billing/BillingOverviewPage.js \
        src/static/src/paas/pages/billing/BillingOverviewPage.xml
git commit -m "feat: reconnect ManagePaymentMethodsModal to BillingOverview for quick access"
```

---

## Task 11: Update design doc and mark Phase 2a shipped

**Files:**
- Modify: `docs/plans/2026-04-16-settings-payment-methods-design.md`

**Step 1: Update Phase 2 heading**

Mark the "Phase 2a — Payment Methods UI" subsection as ✅ shipped with the commit range from this plan's execution, and note open items left for Phase 2b.

**Step 2: Commit**

```bash
git add docs/plans/2026-04-16-settings-payment-methods-design.md
git commit -m "docs: mark Phase 2a as shipped"
```

---

## Manual verification checklist (before opening PR)

Deploy staging via `/deploy-stg` (Phase 2a needs `payment_ecpay_ecpg` in the image — already bundled in paas-odoo-ci `f32c677`). Then:

- [ ] In Odoo backend, enable `payment.provider` ECPay, check `Enable ECPG 站內付綁卡`, save. Verify `enable_ecpg=True`.
- [ ] `#/settings` — Payment Methods card no longer shows "Coming Soon"; clicking it navigates to `#/settings/payment-methods`.
- [ ] `#/settings/payment-methods` — empty state shows "No cards bound yet" + "Add Payment Method" button.
- [ ] Click Add → navigates to `#/settings/payment-methods/add`; ECPay SDK card form renders inside `<div id="ECPayPayment">`.
- [ ] Fill test card `4311-9522-2222-2222`, CVC `222`, any future date; click Bind Card.
- [ ] 3DS page loads, enter `1234`, confirm.
- [ ] Browser returns to `#/settings/payment-methods?bind_status=ok&...`; success banner shows; card appears in list with last 4 digits `2222`.
- [ ] Set Default → button disappears on that card, Default badge appears.
- [ ] Remove → card disappears from list.
- [ ] From `#/billing`, click "Manage" next to Subscription & Payment; modal opens showing bound cards.
- [ ] In modal, click "Add Payment Method" → modal closes, navigates to `#/settings/payment-methods/add`.
- [ ] Delete button in modal also removes a card correctly.
- [ ] Logout → login as another user; their payment-methods page does NOT show first user's cards.

## Reference: Phase 2b hand-off

What's left for Phase 2b:

- `_attempt_auto_topup(partner, shortfall)` helper that picks the default `payment.token` and calls `/api/ecpg/charge_with_token`. On 3DS redirect, log + notify (cannot drive 3DS from a cron).
- Plug into `_sh_create_auto_invoice` insufficient-balance branch: try auto-topup first, then fall back to `_notify_low_balance` if it fails or no default token.
- New config parameter `woow_paas_platform.auto_topup_amount` (how much to add when balance is low).
- Tests for the auto-topup path with mocked `charge_with_token`.
