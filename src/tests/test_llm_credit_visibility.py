"""D19: conversion vouchers stay private across tenant HTTP and native routes."""
import io
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from odoo import fields
from odoo.addons.payment import utils as payment_utils
from odoo.exceptions import AccessError
from odoo.tests.common import HttpCase, new_test_user, tagged
from odoo.tools.pdf import PdfFileWriter

from ..controllers.billing import _invoice_payability_fields
from ..models.llm_common import _llm_internal
from ..models.llm_credit_order import _credit_internal


class _CreditVisibilityFixture:

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:10]
        cls.owner = new_test_user(
            cls.env, login='visibility_owner_' + suffix, password='visibility-test',
            groups='base.group_portal', name='Conversion voucher owner')
        cls.other = new_test_user(
            cls.env, login='visibility_other_' + suffix, password='visibility-test',
            groups='base.group_portal', name='Other voucher tenant')
        cls.accountant = new_test_user(
            cls.env, login='visibility_accountant_' + suffix, password='visibility-test',
            groups='account.group_account_manager', name='Voucher accountant')
        cls.partner = cls.owner.partner_id
        # Use the installed company's accounting setup, as the payment tests
        # do. AccountTestInvoicingHttpCommon creates a second company before
        # this fixture runs; sh_subscription requires sh_journal_id at create
        # time, so that upstream fixture cannot initialize this addon stack.
        company = cls.env.company.sudo()
        journal = cls.env['account.journal'].sudo().search([
            ('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))], limit=1)
        if not journal:
            journal = cls.env['account.journal'].sudo().create({
                'name': 'LLM visibility payment', 'type': 'bank', 'code': 'LLMVP',
                'company_id': company.id,
            })
        company.sh_paid_subscription_journal = journal
        if not cls.env['account.journal'].sudo().search([
                ('company_id', '=', company.id), ('type', '=', 'sale')], limit=1):
            cls.env['account.journal'].sudo().create({
                'name': 'LLM visibility sales', 'type': 'sale', 'code': 'LLMVS',
                'company_id': company.id,
            })
        cls.revenue = cls.env['account.account'].sudo().search([
            ('company_ids', 'in', company.ids), ('account_type', '=', 'income')], limit=1)
        if not cls.revenue:
            cls.revenue = cls.env['account.account'].sudo().create({
                'name': 'LLM visibility revenue', 'code': 'LLMREV',
                'account_type': 'income', 'company_ids': [(6, 0, company.ids)],
            })
        # Wallet settlement is final; mirror a direct bank/cash payment,
        # independent of account_accountant's outstanding-payment defaults.
        journal.inbound_payment_method_line_ids[:1].payment_account_id = journal.default_account_id
        cls.partner.add_wallet_points(500, description='Visibility fixture opening balance')
        cls.account = _llm_internal(cls.env['woow_paas_platform.llm_account']).create({
            'partner_id': cls.partner.id,
        })
        _llm_internal(cls.account).write({'state': 'active', 'newapi_user_id': 4808})
        cls.order = cls.env['woow_paas_platform.llm_credit_order']._llm_create_paid_order(
            cls.account, 100, 'visibility-' + suffix)
        cls.internal = cls.order.invoice_id
        cls.ordinary = cls._invoice()
        cls.foreign = cls._invoice(partner=cls.other.partner_id)
        # A future void worker stamps the same marker on its reversal. Use a
        # real posted credit note, including the wallet return link.
        cls.credit_note = cls._invoice(move_type='out_refund', internal=True)
        cls.partner.add_wallet_points(100, description='Ledger refund description stays intact')
        cls.refund_txn = cls.env['wallet.transaction'].search([
            ('partner_id', '=', cls.partner.id)], order='id desc', limit=1)
        _credit_internal(cls.order).write({
            'void_credit_note_id': cls.credit_note.id,
            'void_wallet_transaction_id': cls.refund_txn.id,
        })
        # Even an unpaid intermediate voucher must never become actionable.
        cls.unpaid_internal = cls._invoice(internal=True)
        cls.vouchers = cls.internal | cls.credit_note | cls.unpaid_internal
        for invoice in cls.vouchers | cls.ordinary:
            invoice._portal_ensure_token()

    @classmethod
    def _invoice(cls, partner=None, move_type='out_invoice', internal=False):
        values = {
            'partner_id': (partner or cls.partner).id,
            'move_type': move_type,
            'invoice_date': fields.Date.today(),
            'woow_skip_upstream_hook': True,
            'invoice_line_ids': [(0, 0, {
                'name': 'D19 private voucher' if internal else 'Ordinary tenant invoice',
                'quantity': 1, 'price_unit': 25,
                'account_id': cls.revenue.id,
                'tax_ids': [(5, 0, 0)],
            })],
        }
        if internal:
            values['woow_llm_credit_order_id'] = cls.order.id
        invoice = cls.env['account.move'].create(values)
        invoice.action_post()
        return invoice

    def _login(self, user=None):
        self.authenticate((user or self.owner).login, 'visibility-test')

    def _rpc_body(self, route, **params):
        return self.url_open(route, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params,
        }), headers={'Content-Type': 'application/json'}).json()

    def _rpc(self, route, **params):
        body = self._rpc_body(route, **params)
        self.assertNotIn('error', body, body)
        return body['result']

    def _get(self, route):
        return self.url_open(route, allow_redirects=False)


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditVisibility(_CreditVisibilityFixture, HttpCase):

    def test_woow_list_count_status_and_foreign_rows(self):
        self._login()
        result = self._rpc('/api/billing/invoices')
        self.assertEqual(result['total'], 1)
        self.assertEqual([row['id'] for row in result['data']], [self.ordinary.id])
        self.assertTrue(result['data'][0]['payable_via_woow'])
        for status in ('paid', 'draft', 'waiting', 'overdue'):
            result = self._rpc('/api/billing/invoices', status=status)
            self.assertFalse(set(row['id'] for row in result['data']) & set(self.vouchers.ids))
            self.assertEqual(result['total'], len(result['data']))

    def test_woow_detail_and_pdf_hide_vouchers_and_foreign_ids(self):
        self._login()
        missing = self._rpc('/api/billing/invoices/999999999')
        for invoice in self.vouchers | self.foreign:
            self.assertEqual(self._rpc('/api/billing/invoices/%s' % invoice.id), missing)
            self.assertEqual(self._get('/api/billing/invoices/%s/pdf' % invoice.id).status_code, 404)
        self.assertTrue(self._rpc('/api/billing/invoices/%s' % self.ordinary.id)['success'])
        response = self._get('/api/billing/invoices/%s/pdf' % self.ordinary.id)
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertTrue(response.content)

    def test_payment_blocks_private_vouchers_before_paid_state_and_preserves_ordinary_payment(self):
        self._login()
        balance = self.partner.wallet_balance
        for invoice in self.vouchers:
            result = self._rpc('/api/billing/invoices/%s/pay' % invoice.id)
            self.assertFalse(result['success'])
            self.assertEqual(result['code'], 'invoice_not_payable')
            self.assertEqual(_invoice_payability_fields(invoice), {
                'payable_via_woow': False, 'pay_blocked_reason': 'invoice_not_payable'})
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self.partner.wallet_balance, balance)
        missing = self._rpc('/api/billing/invoices/999999999/pay')
        self.assertEqual(self._rpc('/api/billing/invoices/%s/pay' % self.foreign.id), missing)
        result = self._rpc('/api/billing/invoices/%s/pay' % self.ordinary.id)
        self.assertTrue(result['success'], result)
        self.ordinary.invalidate_recordset(['payment_state'])
        self.assertEqual(self.ordinary.payment_state, 'paid')

    def test_notifications_hide_unpaid_vouchers_and_map_conversion_wallet_activity(self):
        self._login()
        result = self._rpc('/api/notifications')
        self.assertTrue(result['success'], result)
        items = result['data']['items']
        self.assertEqual({row['id'] for row in items if row['type'] == 'invoice_unpaid'},
                         {'invoice_unpaid:%s' % self.ordinary.id})
        messages = {row['id']: row['message'] for row in items if row['type'] == 'wallet_activity'}
        self.assertEqual(messages['wallet_activity:%s' % self.order.wallet_transaction_id.id],
                         'AI 額度轉換 %s' % self.order.name)
        self.assertEqual(messages['wallet_activity:%s' % self.refund_txn.id],
                         'AI 額度轉換未完成，點數退回')

    def test_wallet_descriptions_are_mapped_without_mutating_ledger_or_other_tenants(self):
        self._login()
        original = self.order.wallet_transaction_id.description
        data = self._rpc('/api/billing/transactions')['data']
        descriptions = {row['id']: row['description'] for row in data}
        self.assertEqual(descriptions[self.order.wallet_transaction_id.id],
                         'AI 額度轉換 %s' % self.order.name)
        self.assertEqual(descriptions[self.refund_txn.id], 'AI 額度轉換未完成，點數退回')
        self.assertEqual(self.order.wallet_transaction_id.description, original)
        self.assertEqual(self.refund_txn.description, 'Ledger refund description stays intact')
        self._login(self.other)
        self.assertFalse(set(row['id'] for row in self._rpc('/api/billing/transactions')['data'])
                         & set(descriptions))

    def test_dashboard_activity_hides_conversion_voucher_and_keeps_owner_scope(self):
        ordinary_description = 'Ordinary wallet activity stays unchanged'
        self.partner.add_wallet_points(3, description=ordinary_description)
        self.other.partner_id.add_wallet_points(7, description='Other tenant wallet activity')
        raw_conversion = self.order.wallet_transaction_id.description
        self.assertIn(self.internal.name, raw_conversion)

        self._login()
        result = self._rpc('/api/dashboard/summary')
        self.assertTrue(result['success'], result)
        descriptions = [row['description'] for row in result['data']['recent_activity']]
        self.assertIn('AI 額度轉換 %s' % self.order.name, descriptions)
        self.assertIn('AI 額度轉換未完成，點數退回', descriptions)
        self.assertIn(ordinary_description, descriptions)
        self.assertNotIn(raw_conversion, descriptions)
        self.assertNotIn('Other tenant wallet activity', descriptions)
        self.assertEqual(self.order.wallet_transaction_id.description, raw_conversion)

        self._login(self.other)
        result = self._rpc('/api/dashboard/summary')
        self.assertTrue(result['success'], result)
        other_descriptions = [row['description'] for row in result['data']['recent_activity']]
        self.assertIn('Other tenant wallet activity', other_descriptions)
        self.assertNotIn(ordinary_description, other_descriptions)
        self.assertNotIn('AI 額度轉換 %s' % self.order.name, other_descriptions)
        self.assertNotIn('AI 額度轉換未完成，點數退回', other_descriptions)
        self.assertNotIn(raw_conversion, other_descriptions)


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditNativePortal(_CreditVisibilityFixture, HttpCase):

    def test_native_list_and_counter_exclude_invoice_and_void_note(self):
        # Portal users cannot read/search the system-only marker directly.
        # Ordinary caller-scoped searches must still work; ir.rule compiles
        # its own marker predicate under sudo, independently of field ACLs.
        tenant_moves = self.env['account.move'].with_user(self.owner)
        with self.assertRaises(AccessError):
            tenant_moves.check_field_access_rights('read', ['woow_llm_credit_order_id'])
        visible = tenant_moves.search([
            ('id', 'in', (self.vouchers | self.ordinary | self.foreign).ids),
        ])
        self.assertEqual(visible.ids, self.ordinary.ids)
        self._login()
        response = self._get('/my/invoices')
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertIn(self.ordinary.name, response.text)
        for invoice in self.vouchers:
            self.assertNotRegex(response.text, r'/my/invoices/%s(?:[?"\s<]|$)' % invoice.id)
        counters = self._rpc('/my/counters', counters=['invoice_count'])
        self.assertEqual(counters['invoice_count'], 1)

    def test_native_detail_token_and_official_download_reject_vouchers(self):
        for user in (self.owner, self.other, False):
            if user:
                self._login(user)
            else:
                self.authenticate(None, None)
            for invoice in self.vouchers:
                for query in ('', '?access_token=%s' % invoice.access_token,
                              '?access_token=%s&report_type=pdf&download=true' % invoice.access_token,
                              '?access_token=%s&report_type=html' % invoice.access_token):
                    response = self._get('/my/invoices/%s%s' % (invoice.id, query))
                    self.assertEqual(response.status_code, 303, response.text[:300])
                    self.assertEqual(response.headers['Location'], '/my')

    def test_native_ordinary_invoice_and_public_token_download_still_work(self):
        self._login()
        self.assertEqual(self._get('/my/invoices/%s' % self.ordinary.id).status_code, 200)
        payment_page = self._get('/my/invoices/%s?payment=true' % self.ordinary.id)
        self.assertEqual(payment_page.status_code, 200, payment_page.text[:300])
        self.assertIn('id="pay_with"', payment_page.text)
        # account_payment's route must still reject a tampered custom amount
        # before rendering the invoice or offering a payment.
        rejected = self._get('/my/invoices/%s?payment=true&amount=1&payment_token=invalid' %
                             self.ordinary.id)
        self.assertEqual(rejected.status_code, 303, rejected.text[:300])
        self.assertEqual(rejected.headers['Location'], '/my')
        self.authenticate(None, None)
        for query in ('?access_token=%s', '?access_token=%s&report_type=pdf&download=true'):
            response = self._get('/my/invoices/%s%s' % (
                self.ordinary.id, query % self.ordinary.access_token))
            self.assertEqual(response.status_code, 200, response.text[:300])
            self.assertTrue(response.content)

    def test_invoice_transaction_rejects_vouchers_but_creates_ordinary_draft(self):
        provider = self.env['payment.provider'].create({
            'name': 'D19 local draft transaction', 'code': 'none',
            'company_id': self.env.company.id,
        })
        method = self.env.ref('payment.payment_method_card')
        params = {
            'provider_id': provider.id, 'payment_method_id': method.id,
            'token_id': None, 'amount': 25, 'flow': 'direct',
            'tokenization_requested': False, 'landing_route': '/my/invoices',
        }
        Transaction = self.env['payment.transaction']
        domain = [('invoice_ids', 'in', self.vouchers.ids)]
        before = Transaction.search_count(domain)
        for user in (self.owner, self.other, False):
            if user:
                self._login(user)
            else:
                self.authenticate(None, None)
            for invoice in self.vouchers:
                body = self._rpc_body('/invoice/transaction/%s' % invoice.id,
                                      access_token=invoice.access_token, **params)
                self.assertIn('error', body, body)
                self.assertEqual(body['error']['data']['name'], 'odoo.exceptions.MissingError')
        self.assertEqual(Transaction.search_count(domain), before)
        self._login()
        result = self._rpc('/invoice/transaction/%s' % self.ordinary.id,
                           access_token=self.ordinary.access_token, **params)
        transaction = Transaction.search([('reference', '=', result['reference'])])
        self.assertEqual(len(transaction), 1)
        self.assertEqual(transaction.invoice_ids, self.ordinary)
        self.assertEqual(transaction.state, 'draft')
        self.assertEqual(transaction.partner_id, self.partner)
        self.assertEqual(transaction.amount, 25)

    def test_native_payment_links_reject_vouchers_and_preserve_ordinary_invoice(self):
        def payment_url(invoice):
            # The native utility needs a request environment only while
            # signing. HTTP requests below run against the real controllers.
            with patch.object(payment_utils, 'request', SimpleNamespace(env=self.env)):
                token = payment_utils.generate_access_token(
                    invoice.partner_id.id, 25.0, invoice.currency_id.id)
            return '/payment/pay?invoice_id=%s&amount=25&access_token=%s' % (invoice.id, token)

        for user in (self.owner, self.other, False):
            if user:
                self._login(user)
            else:
                self.authenticate(None, None)
            missing = self._get('/payment/pay?invoice_id=999999999&amount=25&access_token=invalid')
            foreign = self._get('/payment/pay?invoice_id=%s&amount=25&access_token=invalid' %
                                self.foreign.id)
            self.assertEqual(foreign.status_code, missing.status_code)
            for invoice in self.vouchers:
                response = self._get(payment_url(invoice))
                self.assertEqual(response.status_code, missing.status_code)
                self.assertGreaterEqual(response.status_code, 400)
                self.assertNotIn(invoice.name, response.text)
        for user in (self.owner, False):
            if user:
                self._login(user)
            else:
                self.authenticate(None, None)
            response = self._get(payment_url(self.ordinary))
            self.assertEqual(response.status_code, 200, response.text[:300])
            self.assertIn(self.ordinary.name, response.text)

    def test_direct_report_pdf_html_and_batch_reject_internal_vouchers(self):
        self._login()
        for converter in ('pdf', 'html'):
            for ids in ([str(move.id) for move in self.vouchers]
                        + ['%s,%s' % (self.ordinary.id, self.internal.id)]):
                response = self._get('/report/%s/account.account_invoices/%s' % (converter, ids))
                self.assertEqual(response.status_code, 403, response.text[:300])
        response = self._get('/report/pdf/account.account_invoices/%s' % self.ordinary.id)
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertTrue(response.content)

    def test_direct_pdf_rejects_cached_attachment_before_serving_bytes(self):
        writer = PdfFileWriter()
        writer.add_blank_page(width=200, height=200)
        content = io.BytesIO()
        writer.write(content)
        report = self.env.ref('account.account_invoices')
        report.write({'attachment': "'d19-cached-invoice.pdf'", 'attachment_use': True})
        for invoice in self.internal | self.credit_note:
            self.env['ir.attachment'].create({
                'name': 'd19-cached-invoice.pdf', 'raw': content.getvalue(),
                'res_model': 'account.move', 'res_id': invoice.id,
                'mimetype': 'application/pdf',
            })
        self._login()
        for invoice in self.internal | self.credit_note:
            response = self._get('/report/pdf/account.account_invoices/%s' % invoice.id)
            self.assertEqual(response.status_code, 403, response.text[:300])
            self.assertNotEqual(response.content, content.getvalue())

    def test_global_rule_cannot_be_or_bypassed_and_internal_accountants_keep_access(self):
        # A second broad group rule must not undo the subtractive global rule.
        self.env['ir.rule'].create({
            'name': 'D19 test broad portal group rule',
            'model_id': self.env['ir.model']._get_id('account.move'),
            'groups': [(4, self.env.ref('base.group_portal').id)],
            'domain_force': '[(1, "=", 1)]',
            'perm_read': True, 'perm_write': False,
            'perm_create': False, 'perm_unlink': False,
        })
        for invoice in self.vouchers:
            with self.assertRaises(AccessError):
                invoice.with_user(self.owner).read(['name'])
            self.assertEqual(invoice.with_user(self.accountant).read(['name'])[0]['name'], invoice.name)
        self._login(self.accountant)
        for invoice in self.vouchers:
            response = self._get('/report/pdf/account.account_invoices/%s' % invoice.id)
            self.assertEqual(response.status_code, 200, response.text[:300])
            self.assertTrue(response.content)
