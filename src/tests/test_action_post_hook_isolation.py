"""Verify woow_skip_upstream_hook + woow_force_issue context behave correctly.

⚠ #787：開閘的判準已從「truthy」改成**模組私有 sentinel 的物件身分**，所以本檔
的「cron 路徑」那一格必須走 `_forced_ecpay_issue()` helper。裸
`with_context(woow_force_issue=True)` 從此不開閘、而且會拋 `UserError`——那條路
由 `test_ecpay_force_issue_ctx_forgery_787.py` 的 G 組守。
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase

from ..models.account_move import _forced_ecpay_issue


class TestCreateEcpayInvoiceShortCircuit(TransactionCase):

    def setUp(self):
        super().setUp()
        # Force company into automatic mode so upstream hook would fire
        self.env.company.auto_invoice = 'automatic'
        self.partner = self.env['res.partner'].create({'name': 'Test'})

    def _make_invoice(self, skip_flag):
        product = self.env['product.product'].search([], limit=1)
        if not product:
            product = self.env['product.product'].create({'name': 'Test Product'})
        return self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'woow_skip_upstream_hook': skip_flag,
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': 'Test',
                'quantity': 1,
                'price_unit': 100.0,
            })],
        })

    def test_flag_true_short_circuits_without_calling_super(self):
        """flag=True + no force ctx → return early, upstream not called."""
        inv = self._make_invoice(skip_flag=True)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            inv.create_ecpay_invoice()
            upstream.assert_not_called()

    def test_flag_false_calls_upstream(self):
        """flag=False → super() runs normally."""
        inv = self._make_invoice(skip_flag=False)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            inv.create_ecpay_invoice()
            upstream.assert_called()

    def test_force_issue_context_bypasses_skip(self):
        """flag=True + sentinel 開閘 → upstream called (cron's path)."""
        inv = self._make_invoice(skip_flag=True)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            _forced_ecpay_issue(inv).create_ecpay_invoice()
            upstream.assert_called()

    def test_action_post_with_skip_flag_does_not_invoke_upstream(self):
        """The whole point: action_post() must NOT trigger upstream issuance."""
        inv = self._make_invoice(skip_flag=True)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            inv.action_post()
            upstream.assert_not_called()

    def test_force_issue_explicit_false_still_short_circuits(self):
        """woow_force_issue=False (explicitly) should still short-circuit (only True bypasses)."""
        inv = self._make_invoice(skip_flag=True)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            inv.with_context(woow_force_issue=False).create_ecpay_invoice()
            upstream.assert_not_called()

    def test_action_post_without_skip_flag_invokes_upstream(self):
        """Positive control for test_action_post_with_skip_flag_does_not_invoke_upstream.

        When skip_flag=False AND company.auto_invoice='automatic', action_post
        MUST trigger upstream's create_ecpay_invoice. Without this positive
        control, the negative test could pass even if Task 2.1's short-circuit
        were broken (e.g. if upstream's filter unexpectedly excluded our test
        invoice for unrelated reasons).
        """
        inv = self._make_invoice(skip_flag=False)
        with patch(
            'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove'
            '.create_ecpay_invoice',
            autospec=True,
        ) as upstream:
            inv.action_post()
            upstream.assert_called()
