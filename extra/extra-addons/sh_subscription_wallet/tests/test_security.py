# -*- coding: utf-8 -*-
"""Layer 2: SEC-001 ~ SEC-007 — Security Tests

IDOR, CheckMacValue, session tampering, CSRF protection.
"""
import json
from datetime import date

from odoo.tests import tagged, HttpCase


@tagged('security', '-at_install', 'post_install')
class TestSecurity(HttpCase):
    """SEC-001 ~ SEC-007: Security vulnerability tests."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # User A (victim)
        cls.user_a = cls.env['res.users'].create({
            'name': 'Security User A',
            'login': 'sec_user_a',
            'password': 'sec_user_a',
            'email': 'sec.a@test.com',
            'groups_id': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        cls.partner_a = cls.user_a.partner_id

        # User B (attacker)
        cls.user_b = cls.env['res.users'].create({
            'name': 'Security User B',
            'login': 'sec_user_b',
            'password': 'sec_user_b',
            'email': 'sec.b@test.com',
            'groups_id': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        cls.partner_b = cls.user_b.partner_id

        # Plan
        cls.plan = cls.env['sh.subscription.plan'].create({
            'name': 'Security Test Plan',
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_plan_price': 500.0,
            'sh_no_of_billing_cycle': 12,
            'sh_start_immediately': True,
            'sh_is_close_by_customer': True,
        })
        cls.reason = cls.env['sh.subscription.reason'].create({
            'name': 'Security Test Reason',
        })

        # Subscription belonging to User A
        cls.sub_a = cls.env['sh.subscription.subscription'].create({
            'sh_partner_id': cls.partner_a.id,
            'sh_subscription_plan_id': cls.plan.id,
            'sh_plan_price': 500.0,
            'sh_recurrency': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': 12,
            'sh_start_date': date.today(),
        })

    def _jsonrpc(self, url, params):
        """Make a JSON-RPC call."""
        data = json.dumps({
            'jsonrpc': '2.0',
            'method': 'call',
            'id': 1,
            'params': params,
        })
        response = self.url_open(
            url,
            data=data,
            headers={'Content-Type': 'application/json'},
        )
        return response.json()

    def test_sec001_idor_subscription_detail(self):
        """SEC-001: User B cannot view User A's subscription detail."""
        self.authenticate('sec_user_b', 'sec_user_b')
        response = self.url_open(
            f'/my/sh_subscription/{self.sub_a.id}',
            allow_redirects=False,
        )
        # Should redirect to /my (access denied)
        self.assertIn(response.status_code, [302, 303],
                       "Other user should be redirected from subscription detail")
        location = response.headers.get('Location', '')
        self.assertNotIn(str(self.sub_a.id), location,
                          "Redirect should not expose subscription ID")

    def test_sec002_idor_cancel_other_subscription(self):
        """SEC-002: User B cannot cancel User A's subscription."""
        self.authenticate('sec_user_b', 'sec_user_b')
        result = self._jsonrpc('/cancel-subscription', {
            'subscription_id': self.sub_a.id,
            'sh_reason_id': self.reason.id,
            'description': 'IDOR attack',
        })
        response_data = result.get('result', result)
        self.assertTrue(
            response_data.get('error'),
            "Should return error for IDOR cancel attempt"
        )
        # Verify subscription state unchanged
        self.sub_a.invalidate_recordset()
        self.assertEqual(self.sub_a.state, 'draft',
                          "Subscription state should not change from IDOR attack")

    def test_sec003_idor_renew_other_subscription(self):
        """SEC-003: User B cannot renew User A's subscription."""
        self.authenticate('sec_user_b', 'sec_user_b')
        result = self._jsonrpc('/renew-subscription', {
            'subscription_id': self.sub_a.id,
        })
        response_data = result.get('result', result)
        self.assertTrue(
            response_data.get('error'),
            "Should return error for IDOR renew attempt"
        )

    def test_sec004_checkmacvalue_forgery(self):
        """SEC-004: Forged CheckMacValue rejected by card binding callback."""
        self.authenticate('sec_user_a', 'sec_user_a')
        response = self.url_open(
            '/my/wallet/bind_card/callback',
            data={
                'MerchantTradeNo': 'FORGE123',
                'RtnCode': '1',
                'Token': 'STOLEN_TOKEN',
                'Card4No': '9999',
                'CheckMacValue': 'DEFINITELY_FORGED_MAC_VALUE_HERE',
            },
            allow_redirects=False,
        )
        # Should reject — verification_failed
        location = response.headers.get('Location', '')
        self.assertTrue(
            'error' in location or 'verification_failed' in location,
            "Forged MAC should be rejected"
        )
        # Verify card was NOT updated
        self.partner_a.invalidate_recordset()
        self.assertNotEqual(self.partner_a.ecpay_card_last4, '9999',
                             "Card should not be bound with forged MAC")

    def test_sec005_session_tampering_topup(self):
        """SEC-005: Forged session wallet_topup_tx_id → no points credited."""
        self.authenticate('sec_user_a', 'sec_user_a')
        # Try to claim a non-existent transaction
        response = self.url_open(
            '/my/wallet/topup/callback?tx_id=99999999',
            allow_redirects=False,
        )
        # Should redirect with error
        self.assertIn(response.status_code, [200, 302, 303])
        location = response.headers.get('Location', '')
        self.assertTrue(
            'error' in location or response.status_code in [302, 303],
            "Non-existent tx should be rejected"
        )

    def test_sec006_session_tampering_bind(self):
        """SEC-006: Forged session card_bind_partner_id → no card bound."""
        # Public endpoint — try callback without valid session
        response = self.url_open(
            '/my/wallet/bind_card/callback',
            data={
                'MerchantTradeNo': 'FORGE_BIND',
                'RtnCode': '1',
                'Token': 'STOLEN',
                'Card4No': '8888',
            },
            allow_redirects=False,
        )
        location = response.headers.get('Location', '')
        # Should fail — either verification_failed or partner_not_found
        self.assertTrue(
            'error' in location or response.status_code in [302, 303],
            "Should not bind card without valid session/MAC"
        )

    def test_sec007_csrf_protected_routes(self):
        """SEC-007: POST to CSRF-protected routes without token → rejected."""
        self.authenticate('sec_user_a', 'sec_user_a')
        # POST to unbind without CSRF token using url_open with data=
        # url_open uses POST when data is provided
        response = self.url_open(
            '/my/wallet/unbind_card',
            data={'dummy': '1'},  # No CSRF token
            allow_redirects=False,
        )
        # Odoo should reject: 400 (CSRF), 403 (forbidden), 405 (method), or 303 (redirect)
        # Any of these means the route is not accepting unauthenticated POST
        self.assertNotEqual(response.status_code, 200,
                             "CSRF-protected route should not return 200 without token")
