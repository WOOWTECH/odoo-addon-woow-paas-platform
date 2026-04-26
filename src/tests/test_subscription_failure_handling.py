"""T19 — subscription dunning behaviour for ``_sh_create_auto_invoice``.

Each test simulates one or more daily cron ticks against a single
subscription and asserts the invariants from
``docs/spec/subscription-failure-handling-gap.md``:

- T19a: single failure increments counter, doesn't cancel, doesn't advance
- T19b: failure then success on the same period reuses the existing
        invoice and resets the counter
- T19c: ``threshold`` consecutive failures cancel + send one email
- T19d: per-sub scope (failures don't bleed between subs)
- T19e: ``threshold=0`` disables auto-cancel
- T19f: cancelled subs are not retried even after wallet top-up
- T19g: cron's filter must reprocess past-due (``next_payment <= today``)
"""
from datetime import date, timedelta
from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestT19SubscriptionFailureHandling(TransactionCase):
    """Behaviour of the auto-invoice cron under the T19 dunning model."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription']
        cls.Move = cls.env['account.move']
        cls.Partner = cls.env['res.partner']

    # --- helpers --------------------------------------------------------- #

    def _make_partner(self, name, balance=0.0):
        return self.Partner.create({
            'name': name,
            'email': f'{name.lower().replace(" ", "_")}@example.test',
            'wallet_balance': balance,
        })

    def _make_sub_record(self, partner, price=100.0, due_today=True):
        """Create a sub via .new()-equivalent that bypasses upstream onchanges.

        We do NOT call ``sh_subscription_confirm`` (that creates an invoice
        eagerly when today >= start_date). Instead drop a record straight
        into ``in_progress`` with the dates we want to control.
        """
        today = date.today()
        sub = self.Sub.sudo().create({
            'sh_partner_id': partner.id,
            'sh_unit': 'month',
            'sh_recurrency': 1,
            'sh_plan_price': price,
            'sh_qty': 1,
            'sh_start_date': today - timedelta(days=1),
            'sh_end_date': today + timedelta(days=29),
            'sh_date_of_next_payment': today if due_today else today + timedelta(days=29),
            'state': 'in_progress',
            'sh_trial': False,
        })
        return sub

    def _set_threshold(self, value):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.max_consecutive_payment_failures', str(value),
        )

    def _run_cron(self):
        self.Sub._sh_create_auto_invoice()

    def _invoices_for(self, sub):
        return self.Move.search([('sh_subscription_id', '=', sub.id)])

    def _patch_settle(self, succeed: bool):
        """Force ``_try_settle_invoice`` to a deterministic outcome.

        Real settlement needs ECPay/wallet wiring that is out of scope for
        these unit tests; we want to exercise the dunning state machine,
        not the payment plumbing (which is covered elsewhere).
        """
        return patch.object(
            type(self.Sub),
            '_try_settle_invoice',
            lambda self, subscription, invoice: succeed,
        )

    # --- T19a ------------------------------------------------------------ #

    def test_T19a_single_failure_increments_counter_and_does_not_advance(self):
        partner = self._make_partner("T19a")
        sub = self._make_sub_record(partner)
        original_next = sub.sh_date_of_next_payment

        with self._patch_settle(succeed=False):
            self._run_cron()
        sub.invalidate_recordset()

        self.assertEqual(sub.woow_consecutive_payment_failures, 1)
        self.assertEqual(sub.state, 'in_progress')
        self.assertEqual(
            sub.sh_date_of_next_payment, original_next,
            "next_payment must NOT advance on failure (Decision 1)",
        )
        self.assertEqual(
            len(self._invoices_for(sub)), 1,
            "exactly one invoice should exist after one failed tick",
        )
        self.assertIsNotNone(sub.woow_last_payment_failure_at)

    # --- T19b ------------------------------------------------------------ #

    def test_T19b_failure_then_success_reuses_invoice_and_resets_counter(self):
        partner = self._make_partner("T19b")
        sub = self._make_sub_record(partner)
        original_next = sub.sh_date_of_next_payment

        with self._patch_settle(succeed=False):
            self._run_cron()
        sub.invalidate_recordset()
        self.assertEqual(sub.woow_consecutive_payment_failures, 1)
        self.assertEqual(len(self._invoices_for(sub)), 1)

        with self._patch_settle(succeed=True):
            self._run_cron()
        sub.invalidate_recordset()

        self.assertEqual(
            sub.woow_consecutive_payment_failures, 0,
            "counter must reset on successful payment",
        )
        self.assertEqual(
            len(self._invoices_for(sub)), 1,
            "the existing unpaid invoice should be REUSED, not duplicated",
        )
        self.assertGreater(
            sub.sh_date_of_next_payment, original_next,
            "next_payment must advance after a successful payment",
        )

    # --- T19c ------------------------------------------------------------ #

    def test_T19c_N_consecutive_failures_cancel_and_send_one_email(self):
        self._set_threshold(3)
        partner = self._make_partner("T19c")
        sub = self._make_sub_record(partner)

        with self._patch_settle(succeed=False):
            for _ in range(3):
                self._run_cron()
        sub.invalidate_recordset()

        self.assertEqual(sub.state, 'cancel')
        self.assertEqual(sub.woow_consecutive_payment_failures, 3)
        self.assertTrue(sub.woow_cancellation_notified)
        self.assertEqual(
            len(self._invoices_for(sub)), 1,
            "still exactly one invoice — no duplicates were created during retries",
        )

        cancellation_emails = self.env['mail.mail'].search([
            ('email_to', 'ilike', partner.email),
            ('subject', 'ilike', 'cancelled'),
        ])
        self.assertEqual(
            len(cancellation_emails), 1,
            "exactly one cancellation email should be queued",
        )

    # --- T19d ------------------------------------------------------------ #

    def test_T19d_failures_are_per_subscription(self):
        self._set_threshold(2)
        partner = self._make_partner("T19d")
        sub_failing = self._make_sub_record(partner, price=100)
        sub_paying = self._make_sub_record(partner, price=200)

        # Tick 1: A fails, B succeeds.
        with patch.object(
            type(self.Sub),
            '_try_settle_invoice',
            lambda self, subscription, invoice:
                subscription.id != sub_failing.id,
        ):
            self._run_cron()
        sub_failing.invalidate_recordset()
        sub_paying.invalidate_recordset()

        self.assertEqual(sub_failing.woow_consecutive_payment_failures, 1)
        self.assertEqual(sub_failing.state, 'in_progress')
        self.assertEqual(sub_paying.woow_consecutive_payment_failures, 0)
        self.assertEqual(sub_paying.state, 'in_progress')

        # Tick 2: A fails again → A crosses threshold; B keeps succeeding.
        with patch.object(
            type(self.Sub),
            '_try_settle_invoice',
            lambda self, subscription, invoice:
                subscription.id != sub_failing.id,
        ):
            self._run_cron()
        sub_failing.invalidate_recordset()
        sub_paying.invalidate_recordset()

        self.assertEqual(sub_failing.state, 'cancel')
        self.assertEqual(
            sub_paying.state, 'in_progress',
            "B must NOT be affected by A's failures",
        )
        self.assertEqual(sub_paying.woow_consecutive_payment_failures, 0)

    # --- T19e ------------------------------------------------------------ #

    def test_T19e_threshold_zero_disables_auto_cancel(self):
        self._set_threshold(0)
        partner = self._make_partner("T19e")
        sub = self._make_sub_record(partner)

        with self._patch_settle(succeed=False):
            for _ in range(10):
                self._run_cron()
        sub.invalidate_recordset()

        self.assertEqual(
            sub.state, 'in_progress',
            "threshold=0 must NOT trigger auto-cancel",
        )
        self.assertEqual(
            sub.woow_consecutive_payment_failures, 10,
            "counter still increments (informational) when threshold disabled",
        )
        self.assertFalse(sub.woow_cancellation_notified)
        cancellation_emails = self.env['mail.mail'].search([
            ('email_to', 'ilike', partner.email),
            ('subject', 'ilike', 'cancelled'),
        ])
        self.assertEqual(len(cancellation_emails), 0)

    # --- T19f ------------------------------------------------------------ #

    def test_T19f_cancelled_sub_is_not_retried_even_with_topup(self):
        self._set_threshold(2)
        partner = self._make_partner("T19f")
        sub = self._make_sub_record(partner)

        with self._patch_settle(succeed=False):
            for _ in range(2):
                self._run_cron()
        sub.invalidate_recordset()
        self.assertEqual(sub.state, 'cancel')
        invoice_count_at_cancel = len(self._invoices_for(sub))

        # Customer "tops up" — but Decision 4 says no in-place recovery.
        partner.wallet_balance = 10000
        with self._patch_settle(succeed=True):
            self._run_cron()
        sub.invalidate_recordset()

        self.assertEqual(
            sub.state, 'cancel',
            "cancelled sub must stay cancelled (Decision 4: no late recovery)",
        )
        self.assertEqual(
            len(self._invoices_for(sub)), invoice_count_at_cancel,
            "no new invoice should be created for a cancelled sub",
        )

    # --- T19g ------------------------------------------------------------ #

    def test_T19g_cron_filter_reprocesses_past_due_subs(self):
        """next_payment in the past must still be picked up (filter is <=)."""
        partner = self._make_partner("T19g")
        sub = self._make_sub_record(partner)
        # Simulate "missed yesterday" — next_payment stuck in the past.
        sub.sh_date_of_next_payment = date.today() - timedelta(days=3)

        with self._patch_settle(succeed=False):
            self._run_cron()
        sub.invalidate_recordset()

        self.assertEqual(
            sub.woow_consecutive_payment_failures, 1,
            "past-due sub must be processed; without `<=` filter the cron "
            "would silently skip and counter would stay at 0",
        )
