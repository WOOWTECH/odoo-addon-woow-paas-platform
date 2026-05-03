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
