"""Tests for wallet.topup.create_auto_topup factory method (Path B Phase 4)."""
from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestCreateAutoTopup(TransactionCase):

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test',
            'email': 'test@example.com',
        })

    def test_create_auto_topup_returns_topup_record(self):
        """create_auto_topup creates a draft topup with source=auto, no plan_id."""
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        self.assertEqual(topup.source, 'auto')
        self.assertEqual(topup.state, 'draft')
        self.assertEqual(topup.amount, 50.0)
        self.assertEqual(topup.credits, 50.0)
        self.assertEqual(topup.bonus, 0.0)
        self.assertFalse(topup.plan_id, "auto-topup must NOT pin a plan")
        self.assertEqual(topup.partner_id, self.partner)

    def test_create_auto_topup_inherits_partner_invoice_carrier(self):
        """The new topup carries the partner's invoice carrier preference."""
        # Use the same field names as res_partner_invoice.py
        # (woow_invoice_carrier_type / woow_invoice_carrier are the partner-side fields)
        if 'woow_invoice_carrier_type' in self.partner._fields:
            self.partner.write({
                'woow_invoice_carrier_type': 'mobile',
                'woow_invoice_carrier': '/ABC1234',
            })
        else:
            self.skipTest("Partner invoice carrier fields not present in this environment")

        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        self.assertEqual(topup.invoice_carrier_type, 'mobile')
        self.assertEqual(topup.invoice_carrier, '/ABC1234')

    def test_create_auto_topup_zero_amount_raises(self):
        """Negative or zero shortfall must be rejected."""
        with self.assertRaises(ValidationError):
            self.env['woow.wallet.topup'].create_auto_topup(
                partner=self.partner, amount=0,
            )

    def test_create_auto_topup_negative_amount_raises(self):
        """Negative amount must be rejected (defensive)."""
        with self.assertRaises(ValidationError):
            self.env['woow.wallet.topup'].create_auto_topup(
                partner=self.partner, amount=-10,
            )


class TestChargeViaToken(TransactionCase):
    """Tests for wallet.topup.charge_via_token (Path B Phase 4.2)."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({
            'name': 'Test',
            'email': 'test@example.com',
        })
        # wallet_balance is a non-stored compute on res.partner backed by
        # loyalty.card.points (sh_subscription_wallet). Writing it on create
        # is a no-op; credits must flow through add_wallet_points.
        self.partner.sudo().add_wallet_points(100.0, description='Test seed')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _setup_ecpg_token(self, bindcard_id):
        """Provision an ECPG-ready provider + token for self.partner."""
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not provider:
            self.skipTest("ECPay provider not configured in this DB")
        provider.write({
            'enable_ecpg': True,
            'MerchantID': '3002607',
            'HashKey': 'pwFHCqoQZGmho4w6',
            'HashIV': 'EkRm7iFT261dpevs',
            'state': 'test',
        })
        payment_method = self.env.ref(
            'payment.payment_method_card', raise_if_not_found=False,
        ) or self.env.ref('payment.payment_method_unknown')
        token = self.env['payment.token'].sudo().create({
            'provider_id': provider.id,
            'partner_id': self.partner.id,
            'payment_method_id': payment_method.id,
            'payment_details': '**** **** **** ' + bindcard_id[-4:],
            'provider_ref': bindcard_id,
            'ecpg_bindcard_id': bindcard_id,
            'ecpg_member_id': str(self.partner.id),
        })
        return provider, token

    def test_charge_via_token_no_token_returns_failure(self):
        """Calling charge_via_token without a valid token returns ('no_token', False)."""
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        ok, msg = topup.charge_via_token(token=None)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_token')

    def test_charge_via_token_inactive_token_returns_failure(self):
        """Inactive token must be rejected."""
        provider, token = self._setup_ecpg_token('AB12345678')
        token.active = False
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        ok, msg = topup.charge_via_token(token)
        self.assertFalse(ok)
        self.assertEqual(msg, 'no_token')

    def _patch_send_payment_request(self, side_effect=None, return_value=None):
        """Patch `_send_payment_request` on the live registry class.

        Odoo composes the model class at registry load via _inherit, so
        patching the upstream module path (odoo.addons.payment...) does
        nothing — the ECPG override at payment_ecpay_ecpg shadows it. The
        only reliable target is the actual class object in the registry.
        """
        TxModel = type(self.env['payment.transaction'])
        kwargs = {}
        if side_effect is not None:
            kwargs['side_effect'] = side_effect
        if return_value is not None:
            kwargs['return_value'] = return_value
        return patch.object(TxModel, '_send_payment_request', autospec=True, **kwargs)

    def test_charge_via_token_successful_charge_credits_wallet_and_creates_invoice(self):
        """End-to-end happy path: charge -> wallet credited -> invoice created with skip flag."""
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        # Mock _send_payment_request to (a) flip tx.state to 'done' and
        # (b) return an empty dict (no 3DS redirect).
        def _set_done(tx_self):
            tx_self.state = 'done'
            return {}

        with self._patch_send_payment_request(side_effect=_set_done):
            ok, msg = topup.charge_via_token(token)

        self.assertTrue(ok, f"Expected success, got: {msg}")
        self.assertEqual(msg, 'ok')
        self.assertEqual(topup.state, 'done')
        # Wallet credited
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self.partner.wallet_balance, 150.0)  # 100 + 50
        # Invoice created with skip flag
        self.assertTrue(topup.invoice_account_move_id)
        self.assertTrue(topup.invoice_account_move_id.woow_skip_upstream_hook)
        self.assertEqual(topup.invoice_account_move_id.woow_invoice_issue_state, 'pending')

    def test_charge_via_token_3ds_required_keeps_pending_state(self):
        """3DS redirect: state stays 'pending', not 'done', not 'failed'."""
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        with self._patch_send_payment_request(
            return_value={'redirect_url': 'https://3ds.example.com/...'},
        ):
            ok, msg = topup.charge_via_token(token)
        self.assertFalse(ok)
        self.assertEqual(msg, '3ds_required')
        self.assertEqual(topup.state, 'pending', "3DS state stays 'pending' (cron cannot complete)")

    def test_charge_via_token_tx_failed_marks_topup_failed(self):
        """When tx.state ends as 'error' or similar, topup goes to 'failed'."""
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        def _set_error(tx_self):
            tx_self.state = 'error'
            return {}

        with self._patch_send_payment_request(side_effect=_set_error):
            ok, msg = topup.charge_via_token(token)
        self.assertFalse(ok)
        self.assertEqual(msg, 'error')
        self.assertEqual(topup.state, 'failed')

    def test_charge_via_token_non_draft_state_returns_invalid_state(self):
        """Re-running charge_via_token on a non-draft topup must NOT create a 2nd transaction."""
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        topup.write({'state': 'pending'})  # simulate concurrent first caller
        ok, msg = topup.charge_via_token(token)
        self.assertFalse(ok)
        self.assertEqual(msg, 'invalid_state')

    def test_charge_via_token_links_payment_transaction_to_topup(self):
        """The created payment.transaction must have woow_topup_id set for 3DS resume."""
        provider, token = self._setup_ecpg_token('AB12345678')
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )

        def _set_done(tx_self):
            tx_self.state = 'done'
            return {}

        with self._patch_send_payment_request(side_effect=_set_done):
            ok, msg = topup.charge_via_token(token)
        self.assertTrue(ok)
        self.assertTrue(topup.payment_transaction_id)
        self.assertEqual(topup.payment_transaction_id.woow_topup_id.id, topup.id)


class TestLandmineGuards(TransactionCase):
    """Tests for the plan_id-nullable landmines fixed alongside charge_via_token."""

    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'Test'})

    def test_on_payment_done_handles_nullable_plan_id(self):
        """Wallet ledger description must NOT be 'Top-up: False' for auto-topups."""
        topup = self.env['woow.wallet.topup'].create_auto_topup(
            partner=self.partner, amount=50.0,
        )
        # Build a minimal payment.transaction for the call
        provider = self.env['payment.provider'].sudo().search(
            [('code', '=', 'ECPay')], limit=1,
        )
        if not provider:
            self.skipTest("ECPay provider not configured")
        payment_method = self.env.ref('payment.payment_method_card', raise_if_not_found=False) \
                         or self.env.ref('payment.payment_method_unknown')
        twd = self.env.ref('base.TWD')
        tx = self.env['payment.transaction'].sudo().create({
            'provider_id': provider.id,
            'payment_method_id': payment_method.id,
            'reference': 'TEST-LANDMINE-1',
            'amount': 50.0,
            'currency_id': twd.id,
            'partner_id': self.partner.id,
            'state': 'done',
        })
        topup._on_payment_done(tx)
        # Read the wallet transaction description back
        wtx = self.env['wallet.transaction'].search(
            [('partner_id', '=', self.partner.id)], order='id desc', limit=1,
        )
        self.assertTrue(wtx)
        self.assertNotIn('False', wtx.description,
                          f"Description must not contain 'False'; got: {wtx.description}")
        self.assertIn('Top-up', wtx.description)
