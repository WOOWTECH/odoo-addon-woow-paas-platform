"""Verify ecpay_invoice_cron can issue PaaS-flagged invoices via context bypass."""
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestEcpayCronForceIssue(TransactionCase):

    def setUp(self):
        super().setUp()
        # Force company into automatic mode so upstream hook would fire on action_post
        self.env.company.auto_invoice = 'automatic'
        self.partner = self.env['res.partner'].create({'name': 'Test'})

    def _make_paas_flagged_posted_invoice(self):
        """Create a posted out_invoice with PaaS skip flag + pending issue state."""
        product = self.env['product.product'].search([], limit=1)
        if not product:
            product = self.env['product.product'].create({'name': 'Test Product'})
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'woow_skip_upstream_hook': True,
            'woow_invoice_issue_state': 'pending',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': 'Test',
                'quantity': 1,
                'price_unit': 100.0,
            })],
        })
        # Posting will trigger upstream hook; our Task 2.1 override short-circuits
        inv.action_post()
        return inv

    def test_cron_issues_paas_flagged_invoice(self):
        """End-to-end: action_post must short-circuit upstream, but cron's force_issue bypass MUST reach upstream.

        Without the woow_force_issue context bypass, Task 2.1's short-circuit
        would silently drop the cron's call and PaaS invoices would never be
        issued.
        """
        cron = self.env['woow_paas_platform.ecpay_invoice_cron']
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            # 1. action_post fires upstream hook (auto_invoice='automatic') →
            #    Task 2.1 override short-circuits → upstream NOT called
            inv = self._make_paas_flagged_posted_invoice()
            # 2. cron explicitly opens the back door → upstream IS called
            cron._issue_one(inv)
            upstream.assert_called_once()
