"""Tests for wallet.topup model schema changes (Path B)."""
from odoo.tests.common import TransactionCase


class TestWalletTopupModel(TransactionCase):
    def test_model_exists_and_has_required_fields(self):
        model = self.env['woow.wallet.topup']
        fields = model._fields
        for name in (
            'partner_id', 'plan_id', 'amount', 'credits', 'bonus',
            'currency_id', 'state', 'payment_transaction_id',
            'wallet_transaction_id', 'failure_code', 'failure_message',
            'invoice_carrier_type', 'invoice_carrier', 'invoice_tax_id',
            'invoice_title', 'invoice_lovecode',
            'invoice_account_move_id', 'invoice_issued_at',
        ):
            self.assertIn(name, fields, f"missing field {name}")

    def test_state_selection_values(self):
        model = self.env['woow.wallet.topup']
        selection = dict(model._fields['state'].selection)
        self.assertEqual(
            set(selection.keys()),
            {'draft', 'pending', 'done', 'failed', 'cancelled'},
        )

    def test_source_field_exists_with_manual_default(self):
        """wallet.topup must have source field defaulting to 'manual'."""
        topup = self.env['woow.wallet.topup']
        self.assertIn('source', topup._fields)
        field = topup._fields['source']
        self.assertEqual(field.default(topup), 'manual')
        valid_values = [v for v, _label in field.selection]
        self.assertIn('manual', valid_values)
        self.assertIn('auto', valid_values)

    def test_create_topup_defaults_source_to_manual(self):
        """Omitting source on create() must yield 'manual' (real ORM path, not just introspection)."""
        partner = self.env['res.partner'].create({'name': 'Test'})
        twd = self.env.ref('base.TWD')
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': partner.id,
            'amount': 50.0,
            'credits': 50.0,
            'currency_id': twd.id,
        })
        self.assertEqual(topup.source, 'manual')

    def test_plan_id_is_optional_for_auto_source(self):
        """Auto topups don't have a fixed plan; plan_id must be nullable."""
        plan_field = self.env['woow.wallet.topup']._fields['plan_id']
        self.assertFalse(plan_field.required, "plan_id must be optional for auto-topup")

    def test_create_auto_topup_without_plan(self):
        """An auto-topup record can be created without a plan_id."""
        partner = self.env['res.partner'].create({'name': 'Test'})
        twd = self.env.ref('base.TWD')
        topup = self.env['woow.wallet.topup'].create({
            'partner_id': partner.id,
            'amount': 50.0,
            'credits': 50.0,
            'currency_id': twd.id,
            'source': 'auto',
        })
        self.assertEqual(topup.source, 'auto')
        self.assertFalse(topup.plan_id)
