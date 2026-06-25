"""Integration test: real call against ECPay stage API.

Runs end-to-end through `account.move.create_ecpay_invoice` with no SDK
mocking, exercising the same code path the production cron uses. Use this
to catch contract drift between the addon and ecpay_invoice_tw / ECPay
that mock-based unit tests can't see (e.g. a renamed field, a stricter
validation, an SDK behaviour change after upgrade).

## Skipped by default

The class is tagged `-standard, ecpay_real` so it is excluded from the
default `--test-tags standard` test run. To execute, name the tag explicitly:

    odoo --test-tags ecpay_real -u woow_paas_platform \\
         --stop-after-init -d <db> -c /etc/odoo/odoo.conf

## Prerequisites

The Odoo company on the running database must have valid ECPay sandbox
credentials. Without them `setUpClass` raises SkipTest. Configure via
Settings → Companies (or directly in the DB):

    ecpay_demo_mode    = True
    ecpay_MerchantID   = <stage merchant id>
    ecpay_HashKey      = <stage hash key>
    ecpay_HashIV       = <stage hash iv>
    seller_Identifier  = <8-digit Taiwan tax id>

## Side effects

Each run creates a real invoice on https://einvoice-stage.ecpay.com.tw
under the configured MerchantID. ECPay's stage backend retains them; this
is acceptable for a sandbox. The Odoo `account.move` is rolled back via
TransactionCase, but the ECPay-side record is not — there is no public
"void test invoice" API on stage.
"""
import unittest

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('-standard', '-at_install', 'post_install', 'ecpay_real')
class TestEcpayInvoiceReal(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        missing = []
        if not company.ecpay_demo_mode:
            missing.append('ecpay_demo_mode=True')
        if not company.ecpay_MerchantID:
            missing.append('ecpay_MerchantID')
        if not company.ecpay_HashKey:
            missing.append('ecpay_HashKey')
        if not company.ecpay_HashIV:
            missing.append('ecpay_HashIV')
        if missing:
            raise unittest.SkipTest(
                "ECPay sandbox credentials missing on res.company: %s. "
                "See module docstring for setup." % ', '.join(missing)
            )

    def _make_partner(self):
        return self.env['res.partner'].create({
            'name': 'EcpayRealTest Customer',
            'street': '台北市信義區信義路五段7號',
            'phone': '0912345678',
            'email': 'ecpayrealtest@example.com',
            'woow_invoice_carrier_type': 'mobile',
            # `/ABC+123` is documented in the ECPay sandbox docs as a
            # safe-to-issue test mobile barcode.
            'woow_invoice_carrier': '/ABC+123',
        })

    def _make_product(self):
        return self.env['product.product'].create({
            'name': 'EcpayRealTest Service',
            'type': 'service',
        })

    def _make_pending_invoice(self, partner, product):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_origin': 'ECPAY-REAL-TEST',
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': 'Real ECPay issue test',
                'quantity': 1,
                'price_unit': 100,
            })],
            'woow_invoice_issue_state': 'pending',
            'carrierType': '3',
            'input_carrier_num': partner.woow_invoice_carrier,
        })
        move.action_post()
        return move

    def test_cron_issues_real_invoice_via_ecpay_stage(self):
        partner = self._make_partner()
        product = self._make_product()
        move = self._make_pending_invoice(partner, product)

        self.env['woow_paas_platform.ecpay_invoice_cron']._cron_issue_pending_invoices()

        move.invalidate_recordset()
        self.assertEqual(
            move.woow_invoice_issue_state, 'issued',
            "Cron should mark issued; last_error=%s" % (move.woow_invoice_last_error or '<none>'),
        )
        self.assertEqual(move.uniform_state, 'invoiced')
        self.assertTrue(move.ecpay_invoice_id, "Should link to uniform.invoice record")
        self.assertTrue(
            move.ecpay_invoice_id.IIS_Number or move.ecpay_invoice_id.name,
            "Stage should return an invoice number",
        )

    def test_manual_retry_issues_real_invoice(self):
        # Drive the move into 'error' state, then exercise the manual retry
        # button against the real API.
        partner = self._make_partner()
        product = self._make_product()
        move = self._make_pending_invoice(partner, product)
        move.write({
            'woow_invoice_issue_state': 'error',
            'woow_invoice_retry_count': 3,
            'woow_invoice_last_error': 'simulated prior failure',
        })

        move.action_woow_retry_ecpay_invoice()

        move.invalidate_recordset()
        self.assertEqual(move.woow_invoice_issue_state, 'issued')
        self.assertEqual(move.woow_invoice_retry_count, 0)
        self.assertFalse(move.woow_invoice_last_error)
        self.assertTrue(move.ecpay_invoice_id)
