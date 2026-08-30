"""Tests for /api/billing/payment-methods/* wrapper endpoints."""
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from odoo.modules import get_module_path
from odoo.tests.common import HttpCase, TransactionCase

from odoo.addons.woow_paas_platform.controllers.billing import (
    _sanitize_phone_for_ecpay,
)


class TestSanitizePhoneForEcpay(TransactionCase):
    """Unit tests for the phone sanitization helper."""

    def test_strips_plus_dash_space_parens(self):
        self.assertEqual(_sanitize_phone_for_ecpay('+1 555-555-5555'), '15555555555')
        self.assertEqual(_sanitize_phone_for_ecpay('(02) 2655-1775'), '0226551775')

    def test_returns_empty_for_none_or_blank(self):
        self.assertEqual(_sanitize_phone_for_ecpay(None), '')
        self.assertEqual(_sanitize_phone_for_ecpay(''), '')
        self.assertEqual(_sanitize_phone_for_ecpay('   '), '')

    def test_returns_empty_when_too_few_digits(self):
        # Less than 7 digits is unlikely to be a real phone; prefer empty.
        self.assertEqual(_sanitize_phone_for_ecpay('ext. 123'), '')

    def test_preserves_plausible_taiwan_mobile(self):
        self.assertEqual(_sanitize_phone_for_ecpay('0912-345-678'), '0912345678')


def _read_asset(*parts):
    path = Path(get_module_path('woow_paas_platform')).joinpath(*parts)
    return path.read_text(encoding='utf-8')


def _extract_js_block_span(source, anchor, start=0):
    """Return (body, opening brace, closing brace) after ``anchor``."""
    anchor_index = source.find(anchor, start)
    if anchor_index < 0:
        return '', -1, -1
    opening = source.find('{', anchor_index + len(anchor))
    if opening < 0:
        return '', -1, -1
    depth = 0
    for index in range(opening, len(source)):
        char = source[index]
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return source[opening + 1:index], opening, index
    return '', -1, -1


def _assigned_error_message(block):
    match = re.search(
        r'''this\.state\.error\s*=\s*_t\(\s*(["'])(?P<message>.*?)\1\s*\)''',
        block,
        re.DOTALL,
    )
    return match.group('message') if match else None


def _bind_frontend_contract(source):
    """Analyse callback control flow, not mere file-wide token presence."""
    callback, _, _ = _extract_js_block_span(source, 'onMounted(async () =>')
    guarded, _, _ = _extract_js_block_span(callback, 'if (bindStatus)')
    blocks = {}
    spans = {}
    for status in ('ok', 'failed', 'error'):
        block, opening, closing = _extract_js_block_span(
            guarded, f'if (bindStatus === "{status}")',
        )
        blocks[status] = block
        spans[status] = (opening, closing)

    unknown, unknown_opening, unknown_closing = _extract_js_block_span(
        guarded, 'else', max(spans['error'][1], 0),
    )
    cleanup_index = guarded.find('window.history.replaceState')
    branch_cleanup = {
        status: 'window.history.replaceState' in block
        for status, block in blocks.items()
    }
    branch_cleanup['unknown'] = 'window.history.replaceState' in unknown

    known_branch_cleanup = any(
        branch_cleanup[status] for status in ('ok', 'failed', 'error')
    )
    return {
        'ok_feedback': 'this.state.bindSuccess = true' in blocks['ok'],
        'failed_message': _assigned_error_message(blocks['failed']),
        'error_message': _assigned_error_message(blocks['error']),
        'unknown_message': _assigned_error_message(unknown),
        'cleanup_after_known': (
            cleanup_index > spans['error'][1] >= 0
            and not known_branch_cleanup
        ),
        'cleanup_common': (
            cleanup_index > unknown_closing >= unknown_opening >= 0
            and not any(branch_cleanup.values())
        ),
    }


class TestPaymentMethodBindFrontendContract(TransactionCase):
    """Source-level guard: this repo has no JavaScript runtime test framework."""

    def _sources(self):
        js = _read_asset(
            'static', 'src', 'paas', 'pages', 'settings',
            'PaymentMethodsListPage.js',
        )
        xml = ET.fromstring(_read_asset(
            'static', 'src', 'paas', 'pages', 'settings',
            'PaymentMethodsListPage.xml',
        ))
        return js, xml

    def test_failed_bind_status_has_visible_feedback(self):
        js, template = self._sources()
        contract = _bind_frontend_contract(js)
        self.assertIn(
            'Card binding failed', contract['failed_message'] or '',
            "bind_status=failed must assign a dedicated visible failure message",
        )
        error_branches = [
            node for node in template.iter('t')
            if node.get('t-if') == 'state.error'
            and any(child.get('role') == 'alert' for child in node.iter('div'))
        ]
        self.assertEqual(
            len(error_branches), 1,
            "The state.error feedback surface must remain a visible alert banner",
        )

    def test_error_bind_status_has_distinct_visible_feedback(self):
        js, _template = self._sources()
        contract = _bind_frontend_contract(js)
        self.assertIn(
            "couldn't confirm", contract['error_message'] or '',
            "bind_status=error must assign a visible processing-error message",
        )
        self.assertNotEqual(
            contract['error_message'], contract['failed_message'],
            "Business failure and processing error need distinguishable feedback",
        )

    def test_all_known_bind_statuses_share_query_cleanup(self):
        js, template = self._sources()
        contract = _bind_frontend_contract(js)
        self.assertTrue(contract['ok_feedback'])
        self.assertTrue(contract['failed_message'])
        self.assertTrue(contract['error_message'])
        self.assertTrue(
            contract['cleanup_after_known'],
            "ok/failed/error must reach one cleanup after their branch chain; "
            "cleanup inside the ok branch leaves failure callbacks in the URL",
        )
        success_branches = [
            node for node in template.iter('t')
            if node.get('t-if') == 'state.bindSuccess'
            and any(child.get('role') == 'alert' for child in node.iter('div'))
        ]
        self.assertEqual(len(success_branches), 1)

    def test_contract_probe_distinguishes_common_from_ok_only_cleanup(self):
        """Synthetic +/- controls keep this source guard from token-scan false green."""
        positive = '''
            onMounted(async () => {
                const bindStatus = q.bind_status;
                if (bindStatus) {
                    if (bindStatus === "ok") {
                        this.state.bindSuccess = true;
                    } else if (bindStatus === "failed") {
                        this.state.error = _t("Card binding failed.");
                    } else if (bindStatus === "error") {
                        this.state.error = _t("We couldn't confirm the card binding.");
                    } else {
                        this.state.error = _t("Unknown bind result.");
                    }
                    window.history.replaceState(null, "", cleanHash);
                }
            });
        '''
        ok_only_cleanup = '''
            onMounted(async () => {
                const bindStatus = q.bind_status;
                if (bindStatus) {
                    if (bindStatus === "ok") {
                        this.state.bindSuccess = true;
                        window.history.replaceState(null, "", cleanHash);
                    } else if (bindStatus === "failed") {
                        this.state.error = _t("Card binding failed.");
                    } else if (bindStatus === "error") {
                        this.state.error = _t("We couldn't confirm the card binding.");
                    } else {
                        this.state.error = _t("Unknown bind result.");
                    }
                }
            });
        '''
        self.assertTrue(_bind_frontend_contract(positive)['cleanup_common'])
        self.assertFalse(
            _bind_frontend_contract(ok_only_cleanup)['cleanup_after_known'],
            "Negative control has every expected token but cleanup is ok-only",
        )

    def test_unknown_bind_status_fails_closed_and_is_cleaned(self):
        js, _template = self._sources()
        contract = _bind_frontend_contract(js)
        self.assertIn(
            "couldn't confirm", contract['unknown_message'] or '',
            "Unknown callback statuses must fail closed to generic feedback",
        )
        self.assertTrue(
            contract['cleanup_common'],
            "Unknown callback values must reach the same query cleanup",
        )


class TestPaymentMethodBindResult(HttpCase):
    """Public-route contract for the browser-facing ECPG bind landing."""

    _CALLBACK_PATCH = (
        'odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback.'
        'ECPGCallback._handle_bind_callback'
    )

    def _post_bind_result(self, *, callback_result=None, callback_error=None):
        patch_kwargs = (
            {'side_effect': callback_error}
            if callback_error is not None
            else {'return_value': callback_result}
        )
        with patch(self._CALLBACK_PATCH, **patch_kwargs) as callback:
            response = requests.post(
                f"{self.base_url()}"
                "/api/billing/payment-methods/bind-result/BIND_PUBLIC_SEAM",
                data={'RtnCode': '1', 'RtnMsg': 'OK'},
                allow_redirects=False,
                timeout=10,
            )
        self.assertEqual(response.status_code, 200, response.text[:500])
        callback.assert_called_once()
        match = re.search(
            r'<script>window\.location\.replace\((?P<target>.+?)\);</script>',
            response.text,
        )
        self.assertIsNotNone(match, response.text[:500])
        target = json.loads(match.group('target'))
        fragment = urlsplit(target).fragment
        path, separator, query_string = fragment.partition('?')
        self.assertEqual(path, '/settings/payment-methods')
        self.assertEqual(separator, '?')
        return response, target, parse_qs(query_string)

    def test_bind_result_true_redirects_with_ok_public_result(self):
        """True / ALREADY_EXISTS remains a successful browser result."""
        internal_reason = 'ALREADY_EXISTS_INTERNAL_MARKER'
        response, target, query = self._post_bind_result(
            callback_result=(True, internal_reason),
        )

        self.assertEqual(query.get('bind_status'), ['ok'])
        self.assertEqual(query.get('bind_code'), ['binding_succeeded'])
        self.assertEqual(query.get('trade_no'), ['BIND_PUBLIC_SEAM'])
        self.assertNotIn(internal_reason, target)
        self.assertNotIn(internal_reason, response.text)

    def test_bind_result_false_redirects_with_failed_public_result(self):
        """Business rejection is visible without exposing its internal reason."""
        internal_reason = 'PARTNER_NOT_FOUND_INTERNAL_MARKER'
        response, target, query = self._post_bind_result(
            callback_result=(False, internal_reason),
        )

        self.assertEqual(query.get('bind_status'), ['failed'])
        self.assertEqual(query.get('bind_code'), ['binding_failed'])
        self.assertEqual(query.get('trade_no'), ['BIND_PUBLIC_SEAM'])
        self.assertNotIn(internal_reason, target)
        self.assertNotIn(internal_reason, response.text)

    def test_bind_result_none_redirects_with_error_public_result(self):
        """Parse/decrypt failure is a coarse processing error for the browser."""
        internal_reason = 'DECRYPT_FAIL_INTERNAL_MARKER'
        response, target, query = self._post_bind_result(
            callback_result=(None, internal_reason),
        )

        self.assertEqual(query.get('bind_status'), ['error'])
        self.assertEqual(query.get('bind_code'), ['processing_error'])
        self.assertEqual(query.get('trade_no'), ['BIND_PUBLIC_SEAM'])
        self.assertNotIn(internal_reason, target)
        self.assertNotIn(internal_reason, response.text)

    def test_bind_result_exception_still_lands_with_error_public_result(self):
        """Unexpected helper failure must land in the SPA, never a 500 page."""
        internal_reason = 'RTN_999_EXCEPTION_INTERNAL_MARKER'
        response, target, query = self._post_bind_result(
            callback_error=RuntimeError(internal_reason),
        )

        self.assertEqual(query.get('bind_status'), ['error'])
        self.assertEqual(query.get('bind_code'), ['processing_error'])
        self.assertEqual(query.get('trade_no'), ['BIND_PUBLIC_SEAM'])
        self.assertNotIn(internal_reason, target)
        self.assertNotIn(internal_reason, response.text)


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
        # self.env.user defaults to __system__ (partner id=2 OdooBot) in HttpCase,
        # but authenticate() made the HTTP session authenticate as admin (partner id=3).
        # Tests must use admin's partner when creating records referenced by the HTTP
        # endpoints, otherwise the endpoint's request.env.user.partner_id filter won't
        # match the fixture data.
        self.partner = self.env.ref('base.partner_admin')

    def test_prepare_bind_returns_token(self):
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
        mock_get_token.assert_called_once()
        kwargs = mock_get_token.call_args.kwargs
        # OrderResultURL must point at our bind-result bouncer (which 303's into
        # the OWL SPA hash route) — ECPay POSTs here so the SPA shell can't accept.
        self.assertIn('/api/billing/payment-methods/bind-result/', kwargs['order_result_url'])
        self.assertTrue(
            kwargs['order_result_url'].endswith(body['data']['trade_no']),
            f"trade_no should be the path tail, got {kwargs['order_result_url']!r}",
        )

    def test_prepare_bind_no_provider_returns_error(self):
        self.provider.enable_ecpg = False
        response = self.url_open(
            '/api/billing/payment-methods/prepare-bind',
            data='{"jsonrpc":"2.0","method":"call","params":{}}',
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'provider_not_found')

    def test_create_bind_returns_three_d_url(self):
        """create_bind calls CreateBindCard and returns ThreeDURL if 3DS required."""
        fake_result = {
            'RtnCode': 1, 'RtnMsg': 'Success',
            'BindCardID': 'BC12345',
            'ThreeDInfo': {'ThreeDURL': 'https://3ds.example/verify?id=xyz'},
        }
        member_id = self.provider._ecpg_build_member_id(self.partner)
        with patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.ecpg_client.ECPGClient.create_bind_card',
            return_value=fake_result,
        ) as mock_create:
            response = self.url_open(
                '/api/billing/payment-methods/create-bind',
                data=(
                    '{"jsonrpc":"2.0","method":"call","params":{'
                    f'"member_id":"{member_id}",'
                    '"trade_no":"BIND0001",'
                    '"bind_card_pay_token":"PAYTOKEN_XYZ"}}'
                ),
                headers={'Content-Type': 'application/json'},
            )
            body = response.json()['result']

        mock_create.assert_called_once()
        self.assertTrue(body['success'], f"Unexpected: {body}")
        self.assertEqual(body['data']['three_d_url'], 'https://3ds.example/verify?id=xyz')
        self.assertEqual(body['data']['bind_card_id'], 'BC12345')

    def test_create_bind_member_id_mismatch(self):
        """Member ID from request must match the expected one for the current user."""
        response = self.url_open(
            '/api/billing/payment-methods/create-bind',
            data=(
                '{"jsonrpc":"2.0","method":"call","params":{'
                '"member_id":"WRONG_MEMBER",'
                '"trade_no":"BIND0001",'
                '"bind_card_pay_token":"PAYTOKEN_XYZ"}}'
            ),
            headers={'Content-Type': 'application/json'},
        )
        body = response.json()['result']
        self.assertFalse(body['success'])
        self.assertEqual(body.get('code'), 'member_id_mismatch')

    def test_list_cards_returns_payment_tokens(self):
        """List cards reads from payment.token with ecpg_bindcard_id set."""
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_TEST_1',
            'ecpg_bindcard_id': 'BC_TEST_1',
            'ecpg_member_id': 'M_1',
            'ecpg_card_expire': '2612',  # YYMM (matches ecpay callback: f"{yy}{mm}")
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
        """Delete archives the payment.token (active=False), triggering ECPay delete via hook."""
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': self.provider.id,
            'partner_id': self.partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** 4242',
            'provider_ref': 'BC_TO_DELETE',
            'ecpg_bindcard_id': 'BC_TO_DELETE',
            'ecpg_member_id': 'M_1',
        })
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

    def test_bouncer_does_not_overwrite_session_cookie(self):
        """Bouncer at /api/billing/payment-methods/bind-result/<trade_no> must
        NOT set a new session_id cookie on its response.

        Why: ECPay POSTs cross-site to this URL after 3DS. SameSite=Lax strips
        the user's session_id from the request, so Odoo's default behavior is
        to mint a new anonymous session and Set-Cookie it back — overwriting
        the user's authenticated session_id in their browser. We use
        save_session=False on the route to prevent this. Upstream pattern:
        payment_mollie.mollie_return_from_checkout / payment_aps.aps_return_from_checkout.
        """
        import requests
        url = (
            f"{self.base_url()}"
            "/api/billing/payment-methods/bind-result/TEST_NO_SESSION"
        )
        with patch(
            'odoo.addons.payment_ecpay_ecpg.controllers.ecpg_callback.'
            'ECPGCallback._handle_bind_callback',
            return_value=(True, 'OK'),
        ):
            # Fresh requests.post() (not self.url_open) to isolate from
            # self.opener's authenticated cookies — simulates ECPay's
            # cookieless cross-site POST.
            response = requests.post(
                url,
                data={'RtnCode': '1', 'RtnMsg': 'OK'},
                allow_redirects=False,
                timeout=10,
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            'session_id', response.cookies,
            f"Bouncer must not Set-Cookie session_id "
            f"(got: {dict(response.cookies)})",
        )
        # Sanity: bouncer still serves the HTML+JS redirect page
        self.assertIn('window.location.replace', response.text)
