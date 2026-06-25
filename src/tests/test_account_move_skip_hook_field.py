"""Test the woow_skip_upstream_hook flag on account.move."""
from odoo.tests.common import TransactionCase


class TestAccountMoveSkipHook(TransactionCase):

    def test_field_exists_with_false_default(self):
        """woow_skip_upstream_hook must exist on account.move with default False."""
        move = self.env['account.move']
        self.assertIn('woow_skip_upstream_hook', move._fields)
        # default False so existing flows unchanged
        partner = self.env['res.partner'].create({'name': 'X'})
        rec = move.create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
        })
        self.assertFalse(rec.woow_skip_upstream_hook)

    def test_skip_flag_does_not_propagate_on_copy(self):
        """woow_skip_upstream_hook=True MUST NOT propagate via copy().

        Refund flows (account.move.copy/_reverse_moves) duplicate invoices.
        If the skip flag carried over, refund moves would silently bypass
        upstream's run_refund inline hook — but our override only intercepts
        create_ecpay_invoice, not run_refund. So refund moves MUST inherit
        skip_flag=False (the field default + copy=False ensures this) so
        they get normal upstream issuance.
        """
        partner = self.env['res.partner'].create({'name': 'CopyTest'})
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'woow_skip_upstream_hook': True,
            'invoice_line_ids': [(0, 0, {
                'name': 'Test',
                'quantity': 1,
                'price_unit': 100.0,
            })],
        })
        self.assertTrue(inv.woow_skip_upstream_hook,
                        "Setup precondition: original must have flag=True")
        duplicate = inv.copy()
        self.assertFalse(
            duplicate.woow_skip_upstream_hook,
            "copy=False on the field must prevent skip flag propagation",
        )
