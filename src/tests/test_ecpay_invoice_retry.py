"""Tests for the ECPay e-invoice issuing cron retry behaviour.

Why we patch `ecpay_invoice_tw.AccountMove.create_ecpay_invoice` rather than
the SDK level: we do not want this suite to be coupled to ECPay HTTP
internals — the contract we depend on is that `create_ecpay_invoice` either
flips `uniform_state` to 'invoiced' on success or raises on failure. Anything
deeper is ecpay_invoice_tw's responsibility (and is covered by the optional
`ecpay_real` integration suite).

唯一例外是 `test_already_invoiced_without_number_is_refused_not_stamped`：
它要證明的正是「上游守衛真的被走到、且對外呼叫 0 次」，所以那一支**不 mock**
`create_ecpay_invoice`，改把 spy 放在 ECPay 的 HTTP 邊界。
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
# ECPay SDK 的 HTTP 邊界。只有「要證明外呼 0 次」的那一支 spy 這一層——
# ⛔ 不可 mock 我們自己的 `_issue_one`，那樣只會驗到守衛被放行、驗不到副作用。
ECPAY_HTTP_PATH = (
    'odoo.addons.ecpay_invoice_tw.sdk.ecpay_main.'
    'ECPay_Invoice_Send.CheckOut'
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

    def test_already_invoiced_without_number_is_refused_not_stamped(self):
        """`uniform_state='invoiced'` 但沒有發票號碼 ⇒ 記錄失敗，⛔ 不得標成 issued。

        舊契約是「cron 偵測到 `uniform_state == 'invoiced'` 就把我們的欄位同步成
        `issued`」。#864 review BLOCK 推翻了它：**沒有打過 ECPay 卻寫 issued 就是
        靜默假成功**，而且那道短路把上游 `create_ecpay_invoice()` 自己的重複守衛
        （`uniform_state == 'invoiced' or ecpay_invoice_id`，
        `ecpay_invoice_tw/models/account_invoice.py:221`）擋在外面、永遠不可達。

        `_issue_one()` 移除短路後，這一格會真的走進上游、被上游守衛 raise，於是
        收斂成「記錄失敗」——狀態誠實、人看得到、且沒有第二張真發票。

        ⚠ 這一支刻意**不 mock** `create_ecpay_invoice`：要驗的就是那道守衛真的被
        走到（`assertIn('不可重複開立')` 即判別式，換成別的失敗會紅）。spy 只放在
        ECPay 的 HTTP 邊界，用來證明整條路徑對外呼叫 0 次。
        """
        self.move.write({'uniform_state': 'invoiced'})
        http_calls = []

        def _spy(*args, **kwargs):
            http_calls.append(kwargs.get('Invoice_Method'))
            return {'RtnCode': 1, 'RtnMsg': 'ok', 'InvoiceNo': 'XX00000000'}

        with patch(ECPAY_HTTP_PATH, new=_spy):
            self.cron._cron_issue_pending_invoices()
        self.move.invalidate_recordset()
        self.assertEqual(http_calls, [], "⛔ 不得對 ECPay 送出任何外呼")
        self.assertNotEqual(
            self.move.woow_invoice_issue_state, 'issued',
            "⛔ 未實際開票不得 stamp issued（靜默假成功）",
        )
        self.assertEqual(self.move.woow_invoice_issue_state, 'failed')
        self.assertIn(
            '不可重複開立', self.move.woow_invoice_last_error or '',
            "失敗原因必須來自上游重複守衛（證明它現在真的可達）",
        )
        self.assertFalse(
            self.move.ecpay_invoice_id.name,
            "⛔ 不得憑空生出發票號碼",
        )

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
