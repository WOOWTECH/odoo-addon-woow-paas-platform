"""LLM-08: guarded ledger schema and atomic local wallet payment.

SQL negatives bypass ORM validation to verify durable database backstops.
Payment tests use real accounting; no HTTP or worker is exercised here.
"""
import json
import secrets
import select
import subprocess
import sys
import uuid
from datetime import timedelta
from unittest import TestCase
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo import fields, sql_db
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models.llm_common import LlmCronTick, LlmError, _llm_internal
from ..models.llm_credit_order import ORDER_MODEL, _credit_internal
from .llm_test_support import (
    TEST_TOS, CommittedLlmFixture, configure, env_for, publish_test_terms,
)


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditOrderSchema(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Order = cls.env[ORDER_MODEL]
        cls.operator = new_test_user(
            cls.env, login='credit_operator_%s' % uuid.uuid4().hex,
            groups='base.group_user,base.group_system', email='credit-operator@example.com')
        cls.partner = cls.env['res.partner'].create({'name': 'LLM credit schema'})
        cls.account = _llm_internal(cls.env['woow_paas_platform.llm_account']).create({
            'partner_id': cls.partner.id,
        })
        _llm_internal(cls.account).write({'state': 'active', 'newapi_user_id': 4108})

    def _values(self, **overrides):
        values = {
            'account_id': self.account.id, 'partner_id': self.partner.id,
            'newapi_user_id': 4108, 'source': 'user', 'points': 100,
            'quota': 1562500, 'client_request_id': str(uuid.uuid4()),
        }
        values.update(overrides)
        return values

    def _order(self, **overrides):
        return _credit_internal(self.Order.sudo()).create(self._values(**overrides))

    def test_acl_portal_none_system_and_account_manager_read_only(self):
        order = self._order()
        for group, readable in (
                ('base.group_portal', False), ('base.group_user', False),
                ('base.group_system', True), ('account.group_account_manager', True)):
            user = new_test_user(self.env, login='credit_%s' % uuid.uuid4().hex,
                                 groups=group)
            model = self.Order.with_user(user)
            self.assertEqual(model.check_access_rights('read', raise_exception=False), readable)
            for operation in ('create', 'write', 'unlink'):
                self.assertFalse(model.check_access_rights(operation, raise_exception=False))
            if readable:
                self.assertEqual(order.with_user(user).read(['points'])[0]['points'], 100)
            else:
                with self.assertRaises(AccessError):
                    order.with_user(user).read(['points'])

    def test_direct_mutations_rejected_even_with_sudo(self):
        order = self._order()
        with self.assertRaises(AccessError):
            self.Order.sudo().create(self._values())
        with self.assertRaises(AccessError):
            order.sudo().write({'last_error_code': 'llm_internal'})
        with self.assertRaises(AccessError):
            order.sudo().unlink()
        # A returned record never carries its creating capability.
        with self.assertRaises(AccessError):
            order.write({'last_error_code': 'llm_internal'})
        inherited = _credit_internal(_llm_internal(self.Order.sudo())).create(self._values())
        with self.assertRaises(AccessError):
            inherited.account_id.write({'state': 'suspended'})

    def test_json_context_and_shared_capability_cannot_authorize(self):
        order = self._order()
        for forged in (True, 1, 'true', {}, ['internal']):
            context = {'woow_llm_credit_write': forged, 'woow_llm_credit_transition': forged,
                       'woow_llm_internal_write': forged, 'module_uninstall': True}
            with self.assertRaises(AccessError):
                self.Order.sudo().with_context(**context).create(self._values())
            with self.assertRaises(AccessError):
                order.sudo().with_context(**context).write({'state': 'credited'})
            with self.assertRaises(AccessError):
                order.sudo().with_context(**context).unlink()
        with self.assertRaises(AccessError):
            _llm_internal(self.Order.sudo()).create(self._values())

    def test_int8_snapshots_round_trip_and_zero_presence(self):
        order = self._order()
        columns = ('quota_before', 'used_quota_before', 'request_count_before',
                   'verify_first_quota', 'verify_first_used_quota', 'verify_first_request_count')
        _credit_internal(order).write(dict.fromkeys(columns, 2 ** 40))
        self.env.flush_all()
        order.invalidate_recordset()
        for column in columns:
            self.assertEqual(order[column], 1099511627776)
            self.env.cr.execute(
                'SELECT data_type FROM information_schema.columns '
                'WHERE table_name = %s AND column_name = %s', (order._table, column))
            self.assertEqual(self.env.cr.fetchone()[0], 'bigint')
        self.assertEqual(order._fields['quota'].column_type, ('int8', 'int8'))
        _credit_internal(order).write(dict.fromkeys(columns, 0) | {
            'before_snapshot_present': True, 'before_sampled_at': '2031-01-01 00:00:00',
            'verify_first_sample_present': True, 'verify_first_sample_at': '2031-01-01 00:05:00',
            'verify_token': 'verifier-owned-lease', 'verify_lease_until': '2031-01-01 00:10:00',
        })
        self.env.flush_all()
        order.invalidate_recordset()
        self.assertTrue(order.before_snapshot_present)
        self.assertTrue(order.verify_first_sample_present)
        self.assertEqual(order.verify_first_quota, 0)
        self.assertEqual(order.verify_token, 'verifier-owned-lease')

    def test_frozen_identity_including_rate_and_origins(self):
        order = self._order()
        for field, value in {
            'name': 'FORGED', 'points': 99, 'quota': 1546875, 'source': 'manual',
            'account_id': self.account.id, 'partner_id': self.partner.id,
            'newapi_user_id': 99, 'client_request_id': 'different',
            'rate_r_snapshot': 31, 'q_snapshot': 15000, 'reason': 'replacement reason',
            'requested_by_uid': self.operator.id,
            'dr_origin_order_id': False, 'recharge_of_order_id': False,
        }.items():
            with self.subTest(field=field), self.assertRaises(AccessError):
                _credit_internal(order).write({field: value})

    def test_capability_cannot_bypass_state_machine_or_initial_state(self):
        order = self._order()
        with self.assertRaises(AccessError):
            _credit_internal(order).write({'state': 'sending'})
        with self.assertRaises(AccessError):
            order._transition('sending')
        with self.assertRaises(ValidationError):
            self._order(state='credited')
        with self.assertRaises(ValidationError):
            self._order(state='unknown')
        _credit_internal(order)._transition('sending')
        self.assertEqual(order.state, 'sending')

    def _sql_reject(self, order, _constraint=None, **values):
        """Reject raw SQL independently of Python guards; protect the cursor."""
        self.env.flush_all()
        assignments = ', '.join('"%s" = %%s' % key for key in values)
        with self.assertRaises(IntegrityError) as caught, self.env.cr.savepoint(), \
                mute_logger('odoo.sql_db'):
            self.env.cr.execute('UPDATE ' + order._table + ' SET ' + assignments + ' WHERE id = %s',
                                list(values.values()) + [order.id])
        if _constraint:
            self.assertEqual(caught.exception.diag.constraint_name, _constraint)

    def _raw_state(self, order, state):
        self.env.flush_all()
        self.env.cr.execute('UPDATE ' + order._table + ' SET state = %s WHERE id = %s',
                            (state, order.id))
        order.invalidate_recordset()

    def test_db_points_quota_rate_and_attempt_backstops(self):
        order = self._order()
        for values in ({'points': 0, 'quota': 0}, {'points': 10001, 'quota': 156265625},
                       {'quota': 1}, {'rate_r_snapshot': 31}, {'q_snapshot': 15624},
                       {'attempt': -1}, {'attempt': 7}, {'newapi_user_id': 0},
                       {'source': 'forged'}, {'state': 'forged'}, {'client_request_id': ' '}):
            with self.subTest(values=values):
                self._sql_reject(order, **values)
        _credit_internal(order).write({'attempt': 6})
        self.assertEqual(order.attempt, 6)

    def test_db_manual_requires_cap_reason_and_named_requester(self):
        order = self._order()
        uid = self.operator.id
        for values in ({'source': 'manual'},
                       {'source': 'manual', 'reason': 'short', 'requested_by_uid': uid},
                       {'source': 'manual', 'reason': 'long enough reason'},
                       {'source': 'manual', 'points': 1001, 'quota': 15640625,
                        'reason': 'long enough reason', 'requested_by_uid': uid}):
            self._sql_reject(order, **values)
        manual = self._order(source='manual', points=1000, quota=15625000,
                             reason='a named operator adjustment', requested_by_uid=uid)
        self.assertFalse(manual.invoice_id)
        self.assertFalse(manual.wallet_transaction_id)
        for xmlid in ('base.public_user', 'base.user_root', 'base.user_admin'):
            with self.assertRaises(ValidationError):
                self._order(source='manual', reason='a named operator adjustment',
                            requested_by_uid=self.env.ref(xmlid).id)
        unnamed = new_test_user(self.env, login='credit_shared_%s' % uuid.uuid4().hex,
                                groups='base.group_user', email=False)
        with self.assertRaises(ValidationError):
            self._order(source='manual', reason='a shared account is not a named requester',
                        requested_by_uid=unnamed.id)
        _llm_internal(self.account).write({'state': 'error'})
        with self.assertRaises(ValidationError):
            self._order(source='manual', reason='account is not eligible for manual credit',
                        requested_by_uid=uid)

    def test_manual_requester_eligibility_is_checked_only_at_creation(self):
        manual = self._order(source='manual', reason='approved before the requester was archived',
                             requested_by_uid=self.operator.id)
        self.operator.write({'active': False})
        _credit_internal(manual)._transition('sending')
        self.assertEqual(manual.state, 'sending')
        _credit_internal(manual)._transition('cancelled')
        self.assertEqual(manual.state, 'cancelled')
        self.assertEqual(manual.requested_by_uid, self.operator)
        with self.assertRaises(ValidationError):
            self._order(source='manual', reason='an archived requester cannot create new credit',
                        requested_by_uid=self.operator.id)

    def test_partner_account_must_match(self):
        other = self.env['res.partner'].create({'name': 'Other credit partner'})
        with self.assertRaises(ValidationError):
            self._order(partner_id=other.id)
        with self.assertRaises(ValidationError):
            self._order(newapi_user_id=9999)

    def test_db_partner_client_and_inflight_uniqueness(self):
        first = self._order()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
            self._order(client_request_id=first.client_request_id)
        second = self._order()
        # Several paid orders are allowed, but each in-flight combination conflicts.
        for state in ('sending', 'unknown', 'needs_review'):
            self._raw_state(first, state)
            for second_state in ('sending', 'unknown', 'needs_review'):
                self._sql_reject(second, state=second_state)
            self._raw_state(first, 'paid')

    def test_db_nonempty_upstream_request_claim_is_unique(self):
        first, second = self._order(), self._order()
        _credit_internal(first).write({'newapi_request_id': 'upstream-request-4108'})
        self._sql_reject(second, newapi_request_id='upstream-request-4108')
        _credit_internal(second).write({'newapi_request_id': False})
        self.assertFalse(second.newapi_request_id)

    def test_db_origin_required_and_wrong_source_rejected(self):
        order = self._order()
        self._sql_reject(order, source='dr')
        self._sql_reject(order, source='recharge')
        self._sql_reject(order, dr_origin_order_id=order.id)
        self._sql_reject(order, recharge_of_order_id=order.id)

    def test_dr_requires_mode_lost_origin_and_bounded_points(self):
        origin = self._order()
        self._raw_state(origin, 'credit_lost')
        values = {'source': 'dr', 'dr_origin_order_id': origin.id,
                  'reason': 'restore credit lost after disaster'}
        with patch.object(type(self.env['woow_paas_platform.llm_runtime_state']), '_llm_read',
                          return_value={'dr_mode': False}), self.assertRaises(ValidationError):
            self._order(**values)
        with patch.object(type(self.env['woow_paas_platform.llm_runtime_state']), '_llm_read',
                          return_value={'dr_mode': True}):
            with self.assertRaises(ValidationError):
                self._order(**(values | {'points': 101, 'quota': 1578125}))
            dr = self._order(**values)
            with self.assertRaises(IntegrityError), self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
                self._order(**values)
            self.assertEqual(dr.dr_origin_order_id, origin)
            other = self._order()
            with self.assertRaises(ValidationError):
                self._order(**(values | {'dr_origin_order_id': other.id}))

    def test_recharge_requires_matching_voided_user_and_unique_origin(self):
        origin = self._order()
        values = {'source': 'recharge', 'recharge_of_order_id': origin.id}
        with self.assertRaises(ValidationError):
            self._order(**values)
        self._raw_state(origin, 'voided')
        with self.assertRaises(ValidationError):
            self._order(**(values | {'points': 99, 'quota': 1546875}))
        recharge = self._order(**(values | {'state': 'credited'}))
        self.assertEqual(recharge.recharge_of_order_id, origin)
        self._raw_state(recharge, 'paid')
        _credit_internal(recharge)._transition('credited')
        self.assertEqual(recharge.state, 'credited')
        with self.assertRaises(ValidationError):
            _credit_internal(recharge)._transition('sending')
        with self.assertRaises(IntegrityError), self.env.cr.savepoint(), mute_logger('odoo.sql_db'):
            self._order(**values)

    def test_all_state_edges_and_invalid_edges(self):
        # Independent expected transition table from DESIGN §2.10, plus void retry.
        edges = {
            'paid': {'sending', 'needs_review', 'voided', 'void_failed'},
            'sending': {'paid', 'credited', 'unknown', 'needs_review', 'voided', 'void_failed'},
            'unknown': {'paid', 'credited', 'needs_review'},
            'needs_review': {'paid', 'credited', 'voided', 'void_failed'},
            'void_failed': {'voided', 'needs_review'},
            'credited': {'credit_lost'},
            'voided': set(), 'cancelled': set(), 'credit_lost': set(),
        }
        order = self._order()
        with patch.object(type(self.env['woow_paas_platform.llm_runtime_state']), '_llm_read',
                          return_value={'dr_mode': True}):
            for old, allowed in edges.items():
                if old == 'cancelled':  # user orders cannot enter this state even by SQL.
                    continue
                for new in edges:
                    self._raw_state(order, old)
                    if new in allowed:
                        _credit_internal(order)._transition(new)
                        self.assertEqual(order.state, new)
                    else:
                        with self.assertRaises(ValidationError):
                            _credit_internal(order)._transition(new)

    def test_source_specific_state_and_reversal_restrictions(self):
        user = self._order()
        self._sql_reject(user, state='cancelled')
        manual = self._order(source='manual', reason='operator-approved credit adjustment',
                             requested_by_uid=self.operator.id)
        for old in ('paid', 'sending', 'needs_review'):
            self._raw_state(manual, old)
            _credit_internal(manual)._transition('cancelled')
            self.assertEqual(manual.state, 'cancelled')
        self._sql_reject(manual, state='voided')
        self._sql_reject(manual, state='void_failed')
        self._sql_reject(manual, voided_at='2031-01-01 00:00:00')
        self._sql_reject(manual, invoice_id=0,
                         _constraint=manual._table + '_source_links')
        self._sql_reject(manual, wallet_transaction_id=0,
                         _constraint=manual._table + '_source_links')
        self._sql_reject(manual, void_credit_note_id=0,
                         _constraint=manual._table + '_void_source')
        self._sql_reject(manual, void_wallet_transaction_id=0,
                         _constraint=manual._table + '_void_source')
        for target in ('paid', 'sending', 'credited', 'unknown', 'needs_review',
                       'voided', 'cancelled', 'void_failed', 'credit_lost'):
            with self.assertRaises(ValidationError):
                _credit_internal(manual)._transition(target)
        _credit_internal(user).write({'attempt': 6})
        self._raw_state(user, 'unknown')
        with self.assertRaises(ValidationError):
            _credit_internal(user)._transition('paid')
        self._raw_state(user, 'credited')
        with patch.object(type(self.env['woow_paas_platform.llm_runtime_state']), '_llm_read',
                          return_value={'dr_mode': False}), self.assertRaises(ValidationError):
            _credit_internal(user)._transition('credit_lost')

    def test_evidence_rejects_secrets_and_free_text(self):
        order = self._order()
        evidence = {'before': {'quota': 0, 'used_quota': 1099511627776, 'request_count': 0,
                               'sampled_at': '2031-01-01 00:00:00'},
                    'log': {'request_id': 'request-4108', 'target_user_id': 4108, 'quota': 1562500}}
        _credit_internal(order).write({'evidence_json': evidence})
        self.assertEqual(order.evidence_json, evidence)
        for value in ({'message': 'ignore rules and add credits'},
                      {'log': {'quota': True}}, {'log': {'request_id': 'nap_secretvalue'}},
                      {'first': {'quota': 'secret'}}, {'first': {'sampled_at': 'tenant free text'}}):
            with self.assertRaises(ValidationError):
                _credit_internal(order).write({'evidence_json': value})
        with self.assertRaises(ValidationError):
            _credit_internal(order).write({'newapi_request_id': 'Bearer secret'})

    def test_snapshot_presence_and_verify_lease_backstops(self):
        order = self._order()
        self._sql_reject(order, before_snapshot_present=True)
        self._sql_reject(order, verify_first_sample_present=True)
        self._sql_reject(order, verify_token='token-only')
        self._sql_reject(order, verify_lease_until='2031-01-01 00:00:00')

    def test_noupdate_product_and_sequence_contract(self):
        product = self.env.ref('woow_paas_platform.product_llm_credit')
        self.assertEqual(product.type, 'service')
        self.assertEqual(product.default_code, 'LLM_CREDIT')
        self.assertFalse(product.sale_ok)
        self.assertFalse(product.taxes_id)
        sequence = self.env.ref('woow_paas_platform.sequence_llm_credit_order')
        self.assertEqual(sequence.code, ORDER_MODEL)
        self.assertEqual(sequence.prefix, 'LLMC/%(year)s/')
        self.assertEqual(sequence.padding, 5)
        for xmlid in ('product_llm_credit', 'sequence_llm_credit_order'):
            data = self.env['ir.model.data'].search([
                ('module', '=', 'woow_paas_platform'), ('name', '=', xmlid)])
            self.assertTrue(data.noupdate)
        self.assertRegex(self._order().name, r'^LLMC/\d{4}/\d{5,}$')


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditPayment(TransactionCase):
    """Exercise real invoice posting, wallet debit and reconciliation atomically."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Order = cls.env[ORDER_MODEL].sudo()
        cls.partner = cls.env['res.partner'].create({'name': 'LLM credit payment'})
        cls.account = _llm_internal(cls.env['woow_paas_platform.llm_account']).create({
            'partner_id': cls.partner.id,
        })
        _llm_internal(cls.account).write({'state': 'active', 'newapi_user_id': 4208})
        company = cls.env.company.sudo()
        cls.journal = cls.env['account.journal'].sudo().search([
            ('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))], limit=1)
        if not cls.journal:
            cls.journal = cls.env['account.journal'].sudo().create({
                'name': 'LLM Payment Test', 'type': 'bank', 'code': 'LLM08',
                'company_id': company.id,
            })
        company.sh_paid_subscription_journal = cls.journal
        # Keep a known sales journal; tests must not accidentally depend on a
        # payment-journal fallback also being a valid invoice journal.
        if not cls.env['account.journal'].sudo().search([
                ('company_id', '=', company.id), ('type', '=', 'sale')], limit=1):
            cls.env['account.journal'].sudo().create({
                'name': 'LLM Sales Test', 'type': 'sale', 'code': 'LLMS',
                'company_id': company.id,
            })
        cls.partner.sudo().add_wallet_points(500, description='LLM payment fixture')
        cls.partner.invalidate_recordset(['wallet_balance'])

    def _pay(self, points=100):
        return self.Order._llm_create_paid_order(self.account, points, str(uuid.uuid4()))

    def _snapshot(self):
        self.env.flush_all()
        self.partner.invalidate_recordset(['wallet_balance'])
        result = {'balance': self.partner.wallet_balance}
        for model in (ORDER_MODEL, 'account.move', 'account.payment', 'wallet.transaction'):
            result[model] = self.env[model].sudo().search([
                ('partner_id', '=', self.partner.id)]).ids
        return result

    def _assert_rolled_back(self, before, code='llm_internal_error'):
        from ..models.llm_common import LlmError

        # Bypass TransactionCase.assertRaises: its own savepoint could
        # roll back leaked writes and hide a broken payment savepoint.
        with TestCase.assertRaises(self, LlmError) as raised:
            self._pay()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.params, {})
        self.assertTrue(raised.exception.not_executed)
        self.assertEqual(self._snapshot(), before,
                         'Failure must leave no order, invoice, payment or wallet mutation')

    def test_100_points_posts_zero_tax_paid_invoice_and_one_exact_debit(self):
        before = self._snapshot()
        order = self._pay()
        invoice = order.invoice_id
        tx = order.wallet_transaction_id
        self.assertEqual(order.state, 'paid')
        self.assertEqual(order.points, 100)
        self.assertEqual(order.quota, 1562500)
        self.assertEqual((order.rate_r_snapshot, order.q_snapshot), (32, 15625))
        self.assertEqual(invoice.woow_llm_credit_order_id, order)
        self.assertEqual(invoice.move_type, 'out_invoice')
        self.assertEqual(invoice.state, 'posted')
        self.assertEqual(invoice.payment_state, 'paid')
        self.assertEqual(invoice.amount_total, 100)
        self.assertEqual(invoice.amount_tax, 0)
        self.assertEqual(invoice.woow_invoice_issue_state, 'not_required')
        self.assertTrue(invoice.woow_skip_upstream_hook)
        self.assertEqual(len(invoice.invoice_line_ids), 1)
        line = invoice.invoice_line_ids
        self.assertEqual(line.product_id, self.env.ref('woow_paas_platform.product_llm_credit'))
        self.assertEqual((line.quantity, line.price_unit), (100, 1))
        self.assertFalse(line.tax_ids)
        self.assertEqual(tx.partner_id, self.partner)
        self.assertEqual(tx.transaction_type, 'deduct')
        self.assertEqual(tx.amount, -100)
        after = self._snapshot()
        self.assertEqual(after['balance'], before['balance'] - 100)
        self.assertEqual(set(after['wallet.transaction']) - set(before['wallet.transaction']), {tx.id})
        with self.assertRaises(AccessError):
            order.write({'last_error_code': 'llm_internal_error'})

    def test_insufficient_balance_rolls_back(self):
        from ..models.llm_common import (
            API_ERROR_CODES, ERROR_CODES, ERR_INSUFFICIENT_BALANCE,
        )

        self.assertIn(ERR_INSUFFICIENT_BALANCE, API_ERROR_CODES)
        self.assertIn(ERR_INSUFFICIENT_BALANCE, ERROR_CODES)
        self.partner.sudo().deduct_wallet_points(470, 'LLM insufficient fixture')
        self._assert_rolled_back(self._snapshot(), code='insufficient_balance')

    def test_partial_deduction_rolls_back_even_if_helper_reports_success(self):
        partner_type = type(self.partner)
        original = partner_type.deduct_wallet_points

        deductions = []

        def short_debit(partner, amount, *args, **kwargs):
            result = original(partner, amount - 1, *args, **kwargs)
            deductions.append(result)
            return result

        before = self._snapshot()
        with patch.object(partner_type, 'deduct_wallet_points', short_debit):
            self._assert_rolled_back(before)
        self.assertEqual(deductions, [99], 'Injection must perform the real partial debit')

    def test_missing_payment_journal_rolls_back_after_real_deduction(self):
        company = self.env.company.sudo()
        company.sh_paid_subscription_journal = False
        # Exclude all fallback bank/cash journals, without deleting chart data.
        self.env['account.journal'].sudo().search([
            ('company_id', '=', company.id), ('type', 'in', ('bank', 'cash')),
        ]).write({'active': False})
        partner_type = type(self.partner)
        original = partner_type.deduct_wallet_points
        deductions = []

        def debit(partner, amount, *args, **kwargs):
            result = original(partner, amount, *args, **kwargs)
            deductions.append(result)
            return result

        before = self._snapshot()
        with patch.object(partner_type, 'deduct_wallet_points', debit):
            self._assert_rolled_back(before)
        self.assertEqual(deductions, [100], 'Missing journal must fail after the actual debit')

    def test_unexpected_tax_rolls_back(self):
        tax = self.env['account.tax'].sudo().create({
            'name': 'LLM unexpected tax', 'amount_type': 'percent', 'amount': 5,
            'type_tax_use': 'sale', 'price_include_override': 'tax_excluded',
        })
        move_type = type(self.env['account.move'])
        original = move_type._pay_from_wallet

        settled = []

        def inject_tax(move, *args, **kwargs):
            move.invoice_line_ids.write({'tax_ids': [(6, 0, tax.ids)]})
            result = original(move, *args, **kwargs)
            settled.append((move.amount_tax, move.payment_state))
            return result

        before = self._snapshot()
        with patch.object(move_type, '_pay_from_wallet', inject_tax):
            self._assert_rolled_back(before)
        self.assertEqual(settled, [(5, 'paid')], 'Injection must settle the taxed voucher')

    def test_reconcile_exception_rolls_back_real_posting_and_wallet_debit(self):
        from odoo.exceptions import UserError

        before = self._snapshot()
        with patch.object(type(self.env['account.move.line']), 'reconcile',
                          side_effect=UserError('private accounting failure')) as reconcile:
            self._assert_rolled_back(before)
        reconcile.assert_called_once()

    def test_silent_reconcile_failure_is_rejected(self):
        before = self._snapshot()
        with patch.object(type(self.env['account.move.line']), 'reconcile',
                          return_value=None) as reconcile:
            self._assert_rolled_back(before)
        reconcile.assert_called_once()

    def test_false_payment_result_rolls_back_otherwise_successful_payment(self):
        move_type = type(self.env['account.move'])
        original = move_type._pay_from_wallet

        settled = []

        def false_after_payment(move, *args, **kwargs):
            original(move, *args, **kwargs)
            settled.append(move.payment_state)
            return False

        before = self._snapshot()
        with patch.object(move_type, '_pay_from_wallet', false_after_payment):
            self._assert_rolled_back(before)
        self.assertEqual(settled, ['paid'], 'Failure must be injected after full settlement')

    def test_multiple_new_deductions_fail_closed(self):
        partner_type = type(self.partner)
        original = partner_type.deduct_wallet_points

        deductions = []

        def split_debit(partner, amount, *args, **kwargs):
            deductions.append(original(partner, amount - 1, *args, **kwargs))
            deductions.append(original(partner, 1, *args, **kwargs))
            return amount

        before = self._snapshot()
        with patch.object(partner_type, 'deduct_wallet_points', split_debit):
            self._assert_rolled_back(before)
        self.assertEqual(deductions, [99, 1], 'Injection must create two actual deductions')

    def test_operational_error_propagates_and_rolls_back(self):
        from psycopg2 import OperationalError

        move_type = type(self.env['account.move'])
        original = move_type._pay_from_wallet

        def conflict_after_payment(move, *args, **kwargs):
            original(move, *args, **kwargs)
            raise OperationalError('retry transaction')

        before = self._snapshot()
        with patch.object(move_type, '_pay_from_wallet', conflict_after_payment):
            with TestCase.assertRaises(self, OperationalError):
                self._pay()
        self.assertEqual(self._snapshot(), before)

    def test_automatic_company_never_calls_upstream_ecpay(self):
        self.env.company.sudo().write({'auto_invoice': 'automatic'})
        with patch(
                'odoo.addons.ecpay_invoice_tw.models.account_invoice.AccountMove.create_ecpay_invoice',
                side_effect=AssertionError('must not reach ECPay')) as upstream:
            order = self._pay()
        upstream.assert_not_called()
        self.assertEqual(order.invoice_id.payment_state, 'paid')
        self.assertEqual(self.env.company.auto_invoice, 'automatic')

    def test_shared_exclusion_domain_and_protected_relation(self):
        order = self._pay()
        Move = self.env['account.move'].sudo()
        domain = Move._llm_credit_move_domain()
        self.assertEqual(domain, [('woow_llm_credit_order_id', '=', False)])
        self.assertFalse(Move.search(domain + [('id', '=', order.invoice_id.id)]))
        ordinary = Move.create({'move_type': 'out_invoice', 'partner_id': self.partner.id})
        self.assertEqual(Move.search(domain + [('id', '=', ordinary.id)]), ordinary)
        field = Move._fields['woow_llm_credit_order_id']
        self.assertTrue(field.readonly)
        self.assertTrue(field.index)
        self.assertFalse(field.copy)
        self.assertEqual(field.ondelete, 'restrict')
        self.assertEqual(field.groups, 'base.group_system')


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditFoundation(TransactionCase):
    """New-operation policy and state read only local mirrors."""

    def setUp(self):
        super().setUp()
        from .llm_test_support import configure, TEST_TOS
        configure(self.env)
        self.partner = self.env['res.partner'].create({'name': 'LLM foundation'})
        self.account = _llm_internal(self.env['woow_paas_platform.llm_account']).create({
            'partner_id': self.partner.id,
        })
        _llm_internal(self.account).write({
            'state': 'active', 'newapi_user_id': 4208, 'tos_version': TEST_TOS,
        })
        self.Order = self.env[ORDER_MODEL]
        self.runtime = self.env['woow_paas_platform.llm_runtime_state']
        runtime_patch = patch.object(type(self.runtime), '_llm_read',
                                     return_value={'guard_tripped': False})
        self.guard = runtime_patch.start()
        self.addCleanup(runtime_patch.stop)

    def _order(self, account=None, state='paid'):
        account = account if account is not None else self.account
        order = _credit_internal(self.Order.sudo()).create({
            'account_id': account.id, 'partner_id': account.partner_id.id,
            'newapi_user_id': account.newapi_user_id, 'source': 'user',
            'points': 100, 'quota': 1562500, 'client_request_id': str(uuid.uuid4()),
        })
        if state != 'paid':
            _credit_internal(order)._transition(state)
        return order

    def test_new_operation_blocks_paid_conversion_switch_and_guard_before_remote_or_throttle(self):
        from ..models.llm_common import LlmError, PARAM_ORDERS_ENABLED
        Account = type(self.account)
        Event = type(self.env['woow_paas_platform.llm_event'])
        with patch.object(Account, '_llm_web_client') as client, \
                patch.object(Account, '_llm_up_token') as up, \
                patch.object(Event, '_llm_throttle_reserve') as throttle:
            for switched_off, tripped in ((True, False), (False, True)):
                self.env['ir.config_parameter'].sudo().set_param(
                    PARAM_ORDERS_ENABLED, 'False' if switched_off else 'True')
                self.guard.return_value = {'guard_tripped': tripped}
                with self.assertRaises(LlmError) as error:
                    self.account._llm_require_new_operation()
                self.assertEqual(error.exception.code, 'llm_guard')
            client.assert_not_called()
            up.assert_not_called()
            throttle.assert_not_called()

    def test_free_trial_ignores_paid_conversion_switch_and_guard(self):
        from ..models.llm_common import LlmError, PARAM_ORDERS_ENABLED
        Account = type(self.account)
        Event = type(self.env['woow_paas_platform.llm_event'])
        FreeUsage = type(self.env['woow_paas_platform.llm_free_usage'])
        Key = self.env['woow_paas_platform.llm_api_key']
        stop = LlmError('test_free_trial_continues')
        with patch.object(Account, '_llm_web_client') as client, \
                patch.object(Account, '_llm_up_token') as up, \
                patch.object(Event, '_llm_throttle_reserve') as throttle, \
                patch.object(FreeUsage, '_llm_assert_can_create', side_effect=stop) as usage:
            for switched_off, tripped in ((True, False), (False, True)):
                self.env['ir.config_parameter'].sudo().set_param(
                    PARAM_ORDERS_ENABLED, 'False' if switched_off else 'True')
                self.guard.return_value = {'guard_tripped': tripped}
                with self.assertRaises(LlmError) as error:
                    Key._llm_create_free_trial_key(
                        self.partner, str(uuid.uuid4()), self.env.user, None)
                self.assertEqual(error.exception.code, 'test_free_trial_continues')
            self.assertEqual(usage.call_count, 2)
            client.assert_not_called()
            up.assert_not_called()
            throttle.assert_not_called()

    def test_current_terms_and_active_state_remain_required(self):
        from ..models.llm_common import LlmError
        from .llm_test_support import publish_test_terms
        publish_test_terms(self.env, version='qa-foundation-v2', requires_reaccept=True)
        with self.assertRaises(LlmError) as error:
            self.account._llm_require_new_operation()
        self.assertEqual(error.exception.code, 'llm_tos_outdated')
        _llm_internal(self.account).write({'state': 'suspended'})
        with self.assertRaises(LlmError) as error:
            self.account._llm_require_new_operation()
        self.assertEqual(error.exception.code, 'llm_suspended')

    def test_review_mirror_is_scoped_and_does_not_block_new_key_gate(self):
        self._order()
        self.assertFalse(self.account._llm_has_pending_review())
        other = self.env['res.partner'].create({'name': 'Other foundation partner'})
        other_account = _llm_internal(self.env['woow_paas_platform.llm_account']).create({
            'partner_id': other.id,
        })
        _llm_internal(other_account).write({'state': 'active', 'newapi_user_id': 4209})
        self._order(other_account, 'needs_review')
        self.assertFalse(self.account._llm_has_pending_review())
        self._order(state='needs_review')
        self.assertTrue(self.account._llm_has_pending_review())
        self.account._llm_require_new_operation()

    def test_state_reports_review_and_guard_without_client_or_remote_call(self):
        self._order(state='needs_review')
        self.guard.return_value = {'guard_tripped': True}
        with patch.object(type(self.account), '_llm_client') as client, \
                patch.object(type(self.account), '_llm_web_client') as web:
            payload = self.account._llm_state_payload(self.partner)
            self.assertTrue(payload['pending_review'])
            self.assertEqual(payload['guard'], {'tripped': True})
            self.assertEqual(payload['account']['state'], 'active')
            no_account = self.account._llm_state_payload(
                self.env['res.partner'].create({'name': 'No LLM account'}))
            self.assertFalse(no_account['pending_review'])
            self.assertEqual(no_account['guard'], {'tripped': True})
            client.assert_not_called()
            web.assert_not_called()

    def test_switch_off_keeps_existing_key_visible_and_active(self):
        from ..models.llm_common import PARAM_ORDERS_ENABLED
        Key = self.env['woow_paas_platform.llm_api_key']
        key = _llm_internal(Key).create({
            'account_id': self.account.id, 'kind': 'free_trial', 'state': 'active',
            'newapi_name': 'wqa_foundation_key', 'newapi_token_id': 4208,
        })
        self.env['ir.config_parameter'].sudo().set_param(PARAM_ORDERS_ENABLED, 'False')
        with patch.object(type(self.account), '_llm_client') as client:
            payload = Key._llm_keys_payload(self.partner)
        self.assertEqual([item['id'] for item in payload['items']], [key.id])
        self.assertEqual(payload['items'][0]['status'], 'active')
        self.assertEqual(key.state, 'active')
        client.assert_not_called()

    def test_reserve_uses_shared_service_for_account_partner_and_window(self):
        module = 'odoo.addons.woow_paas_platform.models.llm_account'
        with patch(module + '.upcoming_renewal_requirements', return_value=([], 315.0)) as service:
            self.assertEqual(self.account._llm_reserve_needed(), 315.0)
            args, kwargs = service.call_args
            self.assertEqual(args, (self.env, self.partner))
            self.assertEqual(kwargs['days'], 7)
            self.assertEqual(self.account._llm_reserve_needed(days=3), 315.0)
            self.assertEqual(service.call_args.kwargs['days'], 3)


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditT1(HttpCase):
    """Real local T1 payment, replay, terms and rollback with no upstream call."""

    def setUp(self):
        super().setUp()
        configure(self.env)
        self.Order = self.env[ORDER_MODEL]
        self.password = 'llm-credit-%s' % uuid.uuid4().hex[:12]
        self.portal = new_test_user(
            self.env, login='llm_credit_%s' % uuid.uuid4().hex[:12],
            password=self.password, groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        self.partner = self.portal.partner_id
        self.account = _llm_internal(self.env['woow_paas_platform.llm_account']).create({
            'partner_id': self.partner.id,
        })
        _llm_internal(self.account).write({
            'state': 'active', 'newapi_user_id': 4408, 'tos_version': TEST_TOS,
        })
        company = self.env.company.sudo()
        journal = self.env['account.journal'].sudo().search([
            ('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))], limit=1)
        if not journal:
            journal = self.env['account.journal'].sudo().create({
                'name': 'LLM T1 payment', 'type': 'bank', 'code': 'LLMT1',
                'company_id': company.id,
            })
        company.sh_paid_subscription_journal = journal
        if not self.env['account.journal'].sudo().search([
                ('company_id', '=', company.id), ('type', '=', 'sale')], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'LLM T1 sales', 'type': 'sale', 'code': 'LLMTS',
                'company_id': company.id,
            })
        self.partner.sudo().add_wallet_points(500, description='LLM T1 fixture')
        self.partner.invalidate_recordset(['wallet_balance'])

    def _convert(self, points=100, request_id=None, confirm=False, acknowledge=True):
        return self.Order._llm_convert(
            self.partner, points, request_id or str(uuid.uuid4()), confirm,
            acknowledge, self.env.user, {}, '203.0.113.9')

    def _rpc(self, path, params=None):
        response = self.url_open(path, data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call', 'params': params or {}}),
            headers={'Content-Type': 'application/json'})
        body = response.json()
        self.assertNotIn('error', body, 'Unstructured JSON-RPC exception: %s' % body.get('error'))
        return body['result']

    def _code(self, callback):
        # TransactionCase/HttpCase.assertRaises adds its own savepoint. That
        # would hide a leaked T1 write, so call the plain unittest context.
        with TestCase.assertRaises(self, LlmError) as raised:
            callback()
        return raised.exception

    def _rows(self):
        return {
            model: self.env[model].sudo().search([('partner_id', '=', self.partner.id)]).ids
            for model in (ORDER_MODEL, 'account.move', 'wallet.transaction')
        }

    def _convert_events(self):
        return self.env['woow_paas_platform.llm_event'].sudo().search([
            ('account_id', '=', self.account.id), ('action', '=', 'convert')])

    def test_one_paid_order_and_exact_replay_without_second_debit(self):
        request_id = str(uuid.uuid4())
        before = self._rows()
        first = self._convert(request_id=request_id)
        order = self.Order.sudo().browse(first['order_id'])
        self.assertEqual((first['name'], first['state']), (order.name, 'paid'))
        self.assertTrue(order.acceptance_notice_pending)
        self.assertEqual((order.points, order.wallet_transaction_id.amount), (100, -100))
        self.assertEqual((order.invoice_id.amount_tax, order.invoice_id.payment_state), (0, 'paid'))
        event = self.env['woow_paas_platform.llm_event'].browse(order.convert_event_id)
        event.invalidate_recordset()
        self.assertEqual(event.result, 'started')
        self.assertTrue(event.credit_t1_pending)
        after = self._rows()
        self.assertEqual(len(after[ORDER_MODEL]) - len(before[ORDER_MODEL]), 1)
        self.assertEqual(len(after['wallet.transaction']) - len(before['wallet.transaction']), 1)
        # Idempotency wins before every new-order gate, even with malformed
        # points and a now-closed account.
        _llm_internal(self.account).write({'state': 'closed'})
        replay = self._convert(points=False, request_id=request_id, acknowledge=False)
        self.assertEqual(replay, first)
        self.assertEqual(self._rows(), after)
        self.assertEqual(len(self._convert_events()), 1)
        self.assertEqual(self._code(lambda: self._convert()).code, 'llm_closed')

    def test_local_rejections_do_not_reserve_or_debit(self):
        before = self._rows()
        for points, ack, code in ((False, True, 'llm_points_out_of_range'),
                                  (50.0, True, 'llm_points_out_of_range'),
                                  (49, True, 'llm_points_out_of_range'),
                                  (10001, True, 'llm_points_out_of_range'),
                                  (100, False, 'llm_no_refund_ack_required')):
            with self.subTest(points=points, ack=ack):
                self.assertEqual(self._code(
                    lambda: self._convert(points=points, acknowledge=ack)).code, code)
        self.assertEqual(self._rows(), before)
        self.assertFalse(self._convert_events())

    def test_pending_review_switch_and_runtime_guard_block_before_reservation(self):
        from ..models.llm_common import PARAM_ORDERS_ENABLED
        baseline = self._rows()
        self.env['ir.config_parameter'].sudo().set_param(PARAM_ORDERS_ENABLED, 'False')
        self.assertEqual(self._code(self._convert).code, 'llm_guard')
        self.env['ir.config_parameter'].sudo().set_param(PARAM_ORDERS_ENABLED, 'True')
        runtime = type(self.env['woow_paas_platform.llm_runtime_state'])
        with patch.object(runtime, '_llm_read', return_value={'guard_tripped': True}):
            self.assertEqual(self._code(self._convert).code, 'llm_guard')
        self.assertEqual(self._rows(), baseline)
        self.assertFalse(self._convert_events())
        pending = _credit_internal(self.Order.sudo()).create({
            'account_id': self.account.id, 'partner_id': self.partner.id,
            'newapi_user_id': self.account.newapi_user_id, 'source': 'user',
            'points': 100, 'quota': 1562500, 'client_request_id': str(uuid.uuid4()),
        })
        _credit_internal(pending)._transition('needs_review')
        baseline = self._rows()
        self.assertEqual(self._code(self._convert).code, 'llm_pending_review')
        self.assertEqual(self._rows(), baseline)
        self.assertFalse(self._convert_events())

    def test_terms_reaccept_blocks_before_throttle_and_payment(self):
        before = self._rows()
        publish_test_terms(self.env, version='qa-credit-t1-v2', requires_reaccept=True)
        rejected = self._code(self._convert)
        self.assertEqual((rejected.code, rejected.params['current_version']),
                         ('llm_tos_outdated', 'qa-credit-t1-v2'))
        self.assertEqual(self._rows(), before)
        self.assertFalse(self._convert_events())
        _llm_internal(self.account).write({'tos_version': 'qa-credit-t1-v2'})
        self.assertEqual(self._convert()['state'], 'paid')

    def test_nonblocking_new_terms_keep_existing_acceptance_usable(self):
        publish_test_terms(self.env, version='qa-credit-t1-nonblock',
                           requires_reaccept=False)
        self.assertFalse(self.Order._llm_credit_preview(self.partner, 100)['tos']['blocking'])
        self.assertEqual(self._convert()['state'], 'paid')

    def test_strict_reauth_boundary_and_real_1001_point_conversion(self):
        self.partner.sudo().add_wallet_points(2000, description='LLM T1 reauth fixture')
        self.authenticate(self.portal.login, self.password)
        def convert(points):
            return self._rpc('/api/llm/credit/convert', {
                'points': points, 'client_request_id': str(uuid.uuid4()),
                'accept_no_refund': True, 'confirm_reserve': False,
            })
        self.assertTrue(convert(1000)['success'])
        self.assertEqual(convert(1001)['code'], 'llm_reauth_required')
        self.assertTrue(self._rpc('/api/llm/account/reauth',
                                  {'password': self.password})['success'])
        accepted = convert(1001)
        self.assertTrue(accepted['success'], accepted)
        order = self.Order.sudo().browse(accepted['data']['order_id'])
        self.assertEqual((order.points, order.wallet_transaction_id.amount), (1001, -1001))

    def test_min_and_max_points_create_exact_local_payment(self):
        self.partner.sudo().add_wallet_points(10000, description='LLM T1 max fixture')
        self.authenticate(self.portal.login, self.password)
        minimum = self._rpc('/api/llm/credit/convert', {
            'points': 50, 'client_request_id': str(uuid.uuid4()),
            'accept_no_refund': True,
        })
        self.assertTrue(minimum['success'], minimum)
        self.assertTrue(self._rpc('/api/llm/account/reauth',
                                  {'password': self.password})['success'])
        maximum = self._rpc('/api/llm/credit/convert', {
            'points': 10000, 'client_request_id': str(uuid.uuid4()),
            'accept_no_refund': True,
        })
        self.assertTrue(maximum['success'], maximum)
        orders = self.Order.sudo().browse([
            minimum['data']['order_id'], maximum['data']['order_id']])
        self.assertEqual(sorted(orders.mapped('points')), [50, 10000])
        self.assertEqual(sorted(orders.mapped('quota')), [781250, 156250000])

    def test_preview_reports_exact_quota_and_terms_without_blocking(self):
        publish_test_terms(self.env, version='qa-credit-preview-v2', requires_reaccept=True)
        result = self.Order._llm_credit_preview(self.partner, 100)
        self.assertEqual(result['quota_after_points'], 100)
        self.assertEqual(result['balance_after'], result['wallet_balance'] - 100)
        self.assertTrue(result['tos']['blocking'])
        self.assertFalse(result['pending_review'])
        self.assertFalse(self._convert_events())

    def test_history_is_partner_scoped_and_exact_request_lookup(self):
        request_id = str(uuid.uuid4())
        created = self._convert(request_id=request_id)
        other = self.env['res.partner'].create({'name': 'Other LLM owner'})
        own = self.Order._llm_orders_payload(self.partner, client_request_id=request_id)
        self.assertEqual((own['total'], len(own['items'])), (1, 1))
        self.assertEqual(own['items'][0]['client_request_id'], request_id)
        self.assertEqual(own['items'][0]['poll_after_ms'], 3000)
        self.assertEqual(self.Order._llm_order_payload(self.partner, created['order_id'])['id'],
                         created['order_id'])
        self.assertEqual(self.Order._llm_orders_payload(other, client_request_id=request_id)['total'], 0)
        self.assertEqual(self._code(
            lambda: self.Order._llm_order_payload(other, created['order_id'])).code,
            'llm_order_not_found')

    def test_reserve_requires_explicit_confirmation(self):
        method = type(self.account)._llm_reserve_needed
        before = self._rows()
        with patch.object(type(self.account), '_llm_reserve_needed', return_value=450) as needed:
            rejected = self._code(self._convert)
            self.assertEqual(rejected.code, 'llm_reserve_confirm_required')
            self.assertEqual((rejected.params['needed_7d'], rejected.params['short_by']),
                             (450, 50))
            self.assertEqual(self._rows(), before)
            self.assertFalse(self._convert_events())
            self.assertEqual(self._convert(confirm=True)['state'], 'paid')
        self.assertTrue(needed.called)
        self.assertIsNotNone(method)

    def test_trigger_failure_rolls_back_payment_and_releases_reservation(self):
        before = self._rows()
        with patch.object(type(self.env['ir.cron']), '_trigger',
                          side_effect=RuntimeError('private cron failure')):
            with TestCase.assertRaisesRegex(self, RuntimeError, 'private cron failure'):
                self._convert()
        self.assertEqual(self._rows(), before)
        self.assertEqual(self._convert_events().mapped('result'), ['not_executed'])

    def test_http_routes_scope_history_and_ignore_identity_parameters(self):
        self.authenticate(self.portal.login, self.password)
        preview = self._rpc('/api/llm/credit/preview', {'points': 100})
        self.assertTrue(preview['success'], preview)
        self.assertEqual(preview['data']['quota_after_points'], 100)
        request_id = str(uuid.uuid4())
        created = self._rpc('/api/llm/credit/convert', {
            'points': 100, 'client_request_id': request_id,
            'accept_no_refund': True, 'confirm_reserve': False,
            'partner_id': 1, 'account_id': 1, 'newapi_user_id': 1,
        })
        self.assertTrue(created['success'], created)
        order_id = created['data']['order_id']
        self.assertEqual(self.Order.sudo().browse(order_id).partner_id, self.partner)
        listed = self._rpc('/api/llm/credit/orders', {
            'client_request_id': request_id, 'page': 1, 'limit': 1})
        self.assertEqual((listed['data']['total'], listed['data']['items'][0]['id']), (1, order_id))
        detail = self._rpc('/api/llm/credit/orders/%d' % order_id)
        self.assertEqual(detail['data']['client_request_id'], request_id)

        other = new_test_user(
            self.env, login='llm_credit_other_%s' % uuid.uuid4().hex[:10],
            password=self.password, groups='base.group_portal',
            context={'no_reset_password': True, 'mail_create_nosubscribe': True})
        self.authenticate(other.login, self.password)
        self.assertEqual(self._rpc('/api/llm/credit/orders', {
            'client_request_id': request_id})['data']['total'], 0)
        hidden = self._rpc('/api/llm/credit/orders/%d' % order_id)
        self.assertEqual(hidden['code'], 'llm_order_not_found')

    def test_credit_routes_reject_anonymous_and_valid_bearer_without_session(self):
        suffix = uuid.uuid4().hex[:8]
        client = self.env['woow_paas_platform.oauth_client'].sudo().create({
            'name': 'credit bearer %s' % suffix, 'client_id': 'credit-bearer-%s' % suffix,
            'client_type': 'public', 'require_pkce': True,
            'redirect_uris': 'http://localhost:8123/auth/callback',
            'scopes': 'workspace:read smarthome:read',
            'grant_types': 'authorization_code,refresh_token',
        })
        token = self.env['woow_paas_platform.oauth_token'].sudo().create({
            'access_token': secrets.token_urlsafe(36),
            'refresh_token': secrets.token_urlsafe(36),
            'scope': 'workspace:read smarthome:read',
            'expires_at': fields.Datetime.now() + timedelta(hours=1),
            'refresh_expires_at': fields.Datetime.now() + timedelta(days=30),
            'user_id': self.portal.id, 'client_id': client.id,
        })
        self.opener.cookies.clear()
        bearer = {'Authorization': 'Bearer %s' % token.access_token}
        self.assertEqual(self.url_open('/api/smarthome/workspaces',
                                       headers=bearer).status_code, 200)
        for path in ('/api/llm/credit/preview', '/api/llm/credit/convert',
                     '/api/llm/credit/orders', '/api/llm/credit/orders/1'):
            for auth in ({}, bearer):
                response = self.url_open(path, data=json.dumps({
                    'jsonrpc': '2.0', 'method': 'call', 'params': {
                        'points': 100, 'client_request_id': str(uuid.uuid4()),
                        'accept_no_refund': True,
                    }}), headers={'Content-Type': 'application/json'} | auth)
                body = response.json()
                self.assertNotIn('result', body, path)
                self.assertIn('error', body, path)
        self.assertFalse(self._convert_events())


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditRollback(TransactionCase):
    """A later outer rollback must release T1's committed throttle event."""

    def setUp(self):
        super().setUp()
        self.fixture = CommittedLlmFixture(self).start()
        self.account_id = self.fixture.activate(
            self.fixture.partner('LLM rollback %s' % uuid.uuid4().hex[:8]))
        self.Event = self.env['woow_paas_platform.llm_event']
        self._real_candidates = type(self.Event)._llm_credit_reservation_candidates
        candidate_patch = patch.object(type(self.Event), '_llm_credit_reservation_candidates',
                                       return_value=[self.account_id])
        candidate_patch.start()
        self.addCleanup(candidate_patch.stop)

    def _reap(self):
        return self.Event._llm_reap_credit_reservations(LlmCronTick(10, 10))

    def _result(self, event_id):
        with self.registry.cursor() as cr:
            cr.execute('SELECT result FROM woow_paas_platform_llm_event WHERE id = %s',
                       (event_id,))
            return cr.fetchone()[0]

    def test_rollback_after_local_success_finishes_reservation(self):
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env['woow_paas_platform.llm_account'].browse(self.account_id)
            event_id = env['woow_paas_platform.llm_event']._llm_throttle_reserve(
                account, 'convert', uid=env.uid)
            env[ORDER_MODEL]._llm_release_reservation_on_rollback(event_id)
            self.assertEqual(self._result(event_id), 'started')
            cr.rollback()
        self.assertEqual(self._result(event_id), 'not_executed')

    def test_successful_outer_commit_keeps_reservation_for_worker(self):
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env['woow_paas_platform.llm_account'].browse(self.account_id)
            event_id = env['woow_paas_platform.llm_event']._llm_throttle_reserve(
                account, 'convert', uid=env.uid)
            env[ORDER_MODEL]._llm_release_reservation_on_rollback(event_id)
        self.assertEqual(self._result(event_id), 'started')

    def test_live_t1_is_not_reaped_and_failed_callback_is_retried(self):
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env['woow_paas_platform.llm_account'].browse(self.account_id)
            event_id = env['woow_paas_platform.llm_event']._llm_throttle_reserve(
                account, 'convert', uid=env.uid, credit_t1_pending=True)
            env[ORDER_MODEL]._llm_release_reservation_on_rollback(event_id)
            self.assertEqual(self._reap(), 0, 'An active T1 still owns the reservation')
            with patch.object(type(self.Event), '_llm_event_finish', return_value=None):
                cr.rollback()
        self.assertEqual(self._result(event_id), 'started')
        self.assertEqual(self._reap(), 1)
        self.assertEqual(self._result(event_id), 'not_executed')
        self.assertEqual(self._reap(), 0)

    def test_bound_order_event_and_unmarked_worker_event_are_never_reaped(self):
        with self.registry.cursor() as cr:
            # Worker claim uses READ COMMITTED, unlike the original T1 cursor.
            cr.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            env = env_for(self.registry, cr)
            account = env['woow_paas_platform.llm_account'].browse(self.account_id)
            Event = env['woow_paas_platform.llm_event']
            bound = Event._llm_throttle_reserve(account, 'convert', credit_t1_pending=True)
            worker = Event._llm_throttle_reserve(account, 'convert')
            # Even a stale pending flag must not override durable ownership.
            cr.execute('UPDATE woow_paas_platform_llm_event SET target_model = %s, '
                       'target_id = %s WHERE id = %s', (ORDER_MODEL, 2147483646, bound))
            self.assertEqual(cr.rowcount, 1)
        self.assertEqual(self._reap(), 0)
        self.assertEqual((self._result(bound), self._result(worker)), ('started', 'started'))

    def test_committed_order_link_protects_reservation_before_worker_claim(self):
        order_ids = []

        def cleanup():
            with self.registry.cursor() as cr:
                cr.execute('DELETE FROM woow_paas_platform_llm_credit_order WHERE id = ANY(%s)',
                           (order_ids,))

        self.addCleanup(cleanup)
        with self.registry.cursor() as cr:
            env = env_for(self.registry, cr)
            account = env['woow_paas_platform.llm_account'].browse(self.account_id)
            _llm_internal(account).write({'state': 'active', 'newapi_user_id': 4898})
            # Establish T1's repeatable-read snapshot before reserving an
            # event on another connection. Its scalar reference is visible to the reaper
            # only after this outer transaction commits.
            cr.execute('SELECT id FROM woow_paas_platform_llm_event LIMIT 1')
            event_id = env['woow_paas_platform.llm_event']._llm_throttle_reserve(
                account, 'convert', credit_t1_pending=True)
            cr.execute('SELECT id FROM woow_paas_platform_llm_event WHERE id = %s', (event_id,))
            self.assertFalse(cr.fetchone(), 'T1 cannot see the independently inserted event')
            order = _credit_internal(env[ORDER_MODEL]).create({
                'account_id': account.id, 'partner_id': account.partner_id.id,
                'newapi_user_id': 4898, 'source': 'user', 'points': 100, 'quota': 1562500,
                'client_request_id': str(uuid.uuid4()), 'convert_event_id': event_id,
            })
            order_ids.append(order.id)
            self.assertEqual(order._fields['convert_event_id'].type, 'integer')
            cr.execute('SELECT 1 FROM pg_constraint WHERE conrelid = %s::regclass '
                       "AND contype = 'f' AND conname = %s",
                       (order._table, order._table + '_convert_event_id_fkey'))
            self.assertFalse(cr.fetchone(), 'A cross-snapshot audit reference must not have an FK')
        self.assertEqual(self._reap(), 0)
        self.assertEqual(self._result(event_id), 'started')
        self.assertNotIn(self.account_id, self._real_candidates(self.Event),
                         'Committed orders must not monopolize the bounded candidate batch')

    def test_killed_process_leaves_recoverable_unbound_reservation(self):
        # A disposable process reproduces the exact two-connection boundary:
        # outer transaction owns the shared lock, audit reservation commits
        # independently. SIGKILL cannot run an Odoo postrollback callback.
        program = '''
import json, sys, psycopg2
data = json.loads(sys.stdin.readline())
outer = psycopg2.connect(**data['connection'])
audit = psycopg2.connect(**data['connection'])
with outer.cursor() as cr:
    cr.execute('SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))',
               ('woow_llm_credit_t1:%d' % data['account_id'],))
with audit.cursor() as cr:
    cr.execute("INSERT INTO woow_paas_platform_llm_event "
               "(account_id, partner_id, action, result, severity, occurrences, "
               "credit_t1_pending, create_date, write_date, create_uid, write_uid) "
               "SELECT id, partner_id, 'convert', 'started', 'info', 1, true, "
               "now(), now(), 1, 1 FROM woow_paas_platform_llm_account "
               "WHERE id = %s RETURNING id", (data['account_id'],))
    event_id = cr.fetchone()[0]
audit.commit()
print(event_id, flush=True)
sys.stdin.readline()
'''
        process = subprocess.Popen([sys.executable, '-c', program], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            # Credentials stay in the private pipe; never argv, files or logs.
            process.stdin.write(json.dumps({
                'connection': sql_db.connection_info_for(self.env.cr.dbname)[1],
                'account_id': self.account_id,
            }) + '\n')
            process.stdin.flush()
            readable, _, _ = select.select([process.stdout], [], [], 15)
            self.assertTrue(readable, 'Disposable transaction did not become ready')
            event_id = int(process.stdout.readline().strip())
            self.assertEqual(self._reap(), 0)
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(self._reap(), 1)
            self.assertEqual(self._result(event_id), 'not_executed')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
