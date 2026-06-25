"""Tests for the ECPay e-invoice issuing cron retry behaviour.

Why we patch `ecpay_invoice_tw.AccountMove.create_ecpay_invoice` rather than
the SDK level: we do not want this suite to be coupled to ECPay HTTP
internals — the contract we depend on is that `create_ecpay_invoice` either
flips `uniform_state` to 'invoiced' on success or raises on failure. Anything
deeper is ecpay_invoice_tw's responsibility (and is covered by the optional
`ecpay_real` integration suite).
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import fields as ofields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase

ECPAY_PATH = (
    'odoo.addons.ecpay_invoice_tw.models.account_invoice.'
    'AccountMove.create_ecpay_invoice'
)


def _fake_success(record):
    record.write({'uniform_state': 'invoiced'})


def _fake_fail(record):
    raise UserError("ECPay simulated failure")


class TestEcpayInvoiceRetry(TransactionCase):
    def setUp(self):
        super().setUp()
        self.partner = self.env['res.partner'].create({'name': 'CronUser'})
        self.product = self.env['product.product'].create({
            'name': 'Test Service',
            'type': 'service',
        })
        self.move = self._make_pending_invoice()
        self.cron = self.env['woow_paas_platform.ecpay_invoice_cron']

    def _make_pending_invoice(self):
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product.id,
                'name': 'Test',
                'quantity': 1,
                'price_unit': 100,
            })],
            'woow_invoice_issue_state': 'pending',
        })
        move.action_post()
        return move

    def test_pending_to_issued_on_success(self):
        with patch(ECPAY_PATH, new=_fake_success):
            self.cron._cron_issue_pending_invoices()
        self.move.invalidate_recordset()
        self.assertEqual(self.move.woow_invoice_issue_state, 'issued')
        self.assertFalse(self.move.woow_invoice_last_error)

    def test_failure_increments_retry_count_and_schedules_next(self):
        with patch(ECPAY_PATH, new=_fake_fail):
            self.cron._cron_issue_pending_invoices()
        self.move.invalidate_recordset()
        self.assertEqual(self.move.woow_invoice_issue_state, 'failed')
        self.assertEqual(self.move.woow_invoice_retry_count, 1)
        self.assertTrue(self.move.woow_invoice_next_retry_at)
        self.assertIn('ECPay simulated failure', self.move.woow_invoice_last_error)

    def test_max_retries_marks_error(self):
        # Simulate two prior failures so the next is the third (== max).
        self.move.write({'woow_invoice_retry_count': 2})
        with patch(ECPAY_PATH, new=_fake_fail):
            self.cron._cron_issue_pending_invoices()
        self.move.invalidate_recordset()
        self.assertEqual(self.move.woow_invoice_issue_state, 'error')
        self.assertEqual(self.move.woow_invoice_retry_count, 3)
        # next_retry cleared when we stop trying.
        self.assertFalse(self.move.woow_invoice_next_retry_at)

    def test_max_retries_creates_admin_activity(self):
        # Need at least one user in the manager group for activities to land.
        manager_group = self.env.ref(
            'account.group_account_manager', raise_if_not_found=False,
        )
        if not (manager_group and manager_group.users):
            self.skipTest("No account.group_account_manager users in this DB")
        self.move.write({'woow_invoice_retry_count': 2})
        with patch(ECPAY_PATH, new=_fake_fail):
            self.cron._cron_issue_pending_invoices()
        activities = self.env['mail.activity'].sudo().search([
            ('res_model', '=', 'account.move'),
            ('res_id', '=', self.move.id),
        ])
        self.assertTrue(activities, "Expected admin activity on max retries")

    def test_already_invoiced_is_synced_to_issued(self):
        # Pretend ecpay_invoice_tw's inline trigger or a previous run finished
        # the issuing — cron should detect it and synchronise our state field
        # without calling the SDK again.
        self.move.write({'uniform_state': 'invoiced'})
        calls = []

        def tracker(record):
            calls.append(record.id)

        with patch(ECPAY_PATH, new=tracker):
            self.cron._cron_issue_pending_invoices()
        self.move.invalidate_recordset()
        self.assertEqual(self.move.woow_invoice_issue_state, 'issued')
        self.assertEqual(calls, [], "Should not re-issue an already-invoiced move")

    def test_failed_not_picked_until_next_retry_due(self):
        # Push next_retry far into the future after a first failure.
        with patch(ECPAY_PATH, new=_fake_fail):
            self.cron._cron_issue_pending_invoices()
        self.move.invalidate_recordset()
        self.move.write({
            'woow_invoice_next_retry_at': ofields.Datetime.now() + timedelta(hours=1),
        })
        # Second cron tick: should skip because next_retry > now.
        calls = []

        def tracker(record):
            calls.append(record.id)
            raise UserError("would fail")

        with patch(ECPAY_PATH, new=tracker):
            self.cron._cron_issue_pending_invoices()
        self.move.invalidate_recordset()
        self.assertEqual(calls, [], "Should not retry before next_retry_at")
        self.assertEqual(self.move.woow_invoice_retry_count, 1)

    def test_manual_retry_resets_counters_and_issues(self):
        # Drive the move into 'error' state.
        self.move.write({
            'woow_invoice_issue_state': 'error',
            'woow_invoice_retry_count': 3,
            'woow_invoice_last_error': 'old failure',
        })
        with patch(ECPAY_PATH, new=_fake_success):
            self.move.action_woow_retry_ecpay_invoice()
        self.move.invalidate_recordset()
        self.assertEqual(self.move.woow_invoice_issue_state, 'issued')
        self.assertEqual(self.move.woow_invoice_retry_count, 0)
        self.assertFalse(self.move.woow_invoice_last_error)
