"""Real wallet/accounting reversals; failures must undo every local effect.

HttpCase supplies the registry TestCursor for the independent service boundary.
These tests exercise posting/reconciliation without external HTTP. Real commit
and competing worker coverage lives with the outbox committed fixture.
"""
import uuid
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import HttpCase, new_test_user, tagged

from ..models.llm_common import _llm_internal
from ..models.llm_credit_attempt import _ISSUE_CAPABILITY, _issue_cr
from ..models.llm_credit_order import ORDER_MODEL, _credit_internal
from ..models.llm_event import LlmIncident


@tagged('post_install', '-at_install', 'llm_credit')
class TestLlmCreditVoid(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Order = cls.env[ORDER_MODEL].sudo()
        cls.Event = cls.env['woow_paas_platform.llm_event'].sudo()
        cls.partner = cls.env['res.partner'].create({'name': 'LLM void fixture'})
        cls.account = _llm_internal(cls.env['woow_paas_platform.llm_account']).create({
            'partner_id': cls.partner.id,
        })
        _llm_internal(cls.account).write({'state': 'active', 'newapi_user_id': 4808})
        cls.operator = new_test_user(
            cls.env, login='llm_void_%s' % uuid.uuid4().hex,
            groups='base.group_user,base.group_system', email='llm-void@example.com')
        cls.operator._llm_set_named_human_eligible(True)
        company = cls.env.company.sudo()
        journal = cls.env['account.journal'].sudo().search([
            ('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))], limit=1)
        if not journal:
            journal = cls.env['account.journal'].sudo().create({
                'name': 'LLM Void Bank', 'type': 'bank', 'code': 'LV08',
                'company_id': company.id,
            })
        company.sh_paid_subscription_journal = journal
        if not cls.env['account.journal'].sudo().search([
                ('company_id', '=', company.id), ('type', '=', 'sale')], limit=1):
            cls.env['account.journal'].sudo().create({
                'name': 'LLM Void Sales', 'type': 'sale', 'code': 'LVS8',
                'company_id': company.id,
            })
        cls.partner.add_wallet_points(1000, description='LLM void fixture')

    def setUp(self):
        super().setUp()
        incident = patch.object(type(self.Event), '_llm_incident', return_value=False)
        self.incident = incident.start()
        self.addCleanup(incident.stop)

    def _order(self):
        return self.Order._llm_create_paid_order(self.account, 100, str(uuid.uuid4()))

    def _claim(self, order):
        event = _llm_internal(self.Event).create({
            'account_id': self.account.id, 'partner_id': self.partner.id,
            'action': 'convert', 'result': 'started',
            'target_model': ORDER_MODEL, 'target_id': order.id,
        })
        _credit_internal(order)._transition(
            'sending', attempt=1, convert_event_id=event.id,
            lease_until=fields.Datetime.now() + timedelta(minutes=5))
        self.env.flush_all()
        row = {'id': order.id, 'account_id': self.account.id, 'attempt': 1,
               'newapi_user_id': order.newapi_user_id, 'quota': order.quota}
        attempt_uuid = _issue_cr(self.env.cr, row, event.id, _ISSUE_CAPABILITY)
        return {'id': order.id, 'account_id': self.account.id, 'attempt': 1,
                'event_id': event.id, 'attempt_uuid': attempt_uuid}

    def _snapshot(self):
        self.env.flush_all()
        self.env.invalidate_all()
        result = {'balance': self.partner.wallet_balance}
        for model in ('account.move', 'account.payment', 'wallet.transaction'):
            result[model] = self.env[model].sudo().search([
                ('partner_id', '=', self.partner.id)]).ids
        return result

    def _assert_failed(self, order, claim, before, event_result='not_executed'):
        # The service catches the injected failure. No assertRaises savepoint
        # can conceal a leaked refund/payment from the implementation itself.
        self.assertFalse(self.Order._llm_worker_void_pending(claim, event_result=event_result))
        self.assertEqual(self._snapshot(), before)
        order.invalidate_recordset()
        self.assertEqual(order.state, 'void_failed')
        self.assertFalse(order.void_credit_note_id)
        self.assertFalse(order.void_wallet_transaction_id)
        self.assertEqual(self.Event.browse(order.convert_event_id).result, event_result)
        self.incident.assert_called_once()
        self.assertEqual(self.incident.call_args.args, (LlmIncident.VOID_FAILED,))
        self.assertEqual(self.incident.call_args.kwargs['order_id'], order.id)
        self.assertTrue(self.incident.call_args.kwargs['needs_human'])

    def test_user_void_refunds_once_and_fully_settles_zero_tax_credit_note(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        self.assertTrue(self.Order._llm_worker_void_pending(claim))
        after = self._snapshot()
        order.invalidate_recordset()
        self.assertEqual(order.state, 'voided')
        self.assertEqual(after['balance'], before['balance'] + 100)
        note, refund = order.void_credit_note_id, order.void_wallet_transaction_id
        self.assertEqual((refund.transaction_type, refund.amount), ('refund', 100))
        self.assertEqual(refund.partner_id, self.partner)
        self.assertEqual((note.move_type, note.state, note.payment_state),
                         ('out_refund', 'posted', 'paid'))
        self.assertEqual((note.amount_total, note.amount_tax, note.amount_residual), (100, 0, 0))
        self.assertFalse(note.invoice_line_ids.tax_ids)
        self.assertEqual(note.reversed_entry_id, order.invoice_id)
        self.assertEqual(note.woow_llm_credit_order_id, order)
        self.assertEqual(note.woow_invoice_issue_state, 'not_required')
        self.assertTrue(note.woow_skip_upstream_hook)
        self.assertEqual(set(after['account.move']) - set(before['account.move']),
                         {note.id} | set(self.env['account.payment'].browse(
                             list(set(after['account.payment']) - set(before['account.payment']))
                         ).move_id.ids))
        self.assertEqual(len(set(after['account.payment']) - set(before['account.payment'])), 1)
        self.assertEqual(set(after['wallet.transaction']) - set(before['wallet.transaction']),
                         {refund.id})
        self.assertEqual(self.Event.browse(order.convert_event_id).result, 'not_executed')
        self.assertFalse(self.Order._llm_worker_void_pending(claim))
        self.assertEqual(self._snapshot(), after)
        self.incident.assert_not_called()

    def test_upstream_rejection_still_counts_after_successful_void(self):
        order = self._order()
        claim = self._claim(order)
        self.assertTrue(self.Order._llm_worker_void_pending(claim, event_result='failed'))
        self.env.invalidate_all()
        self.assertEqual(order.state, 'voided')
        self.assertEqual(self.Event.browse(order.convert_event_id).result, 'failed')

    def test_upstream_rejection_still_counts_when_local_void_fails(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        with patch.object(type(self.env['account.move']), 'action_post',
                          side_effect=UserError('injected posting failure')):
            self._assert_failed(order, claim, before, event_result='failed')

    def test_refund_exception_after_wallet_mutation_rolls_everything_back(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        original = type(self.partner).refund_wallet_points

        def fail(partner, *args, **kwargs):
            original(partner, *args, **kwargs)
            raise UserError('injected after real refund')

        with patch.object(type(self.partner), 'refund_wallet_points', fail):
            self._assert_failed(order, claim, before)

    def test_partial_wallet_refund_is_rejected_and_rolled_back(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        original = type(self.partner).refund_wallet_points

        def short(partner, amount, *args, **kwargs):
            return original(partner, amount - 1, *args, **kwargs)

        with patch.object(type(self.partner), 'refund_wallet_points', short):
            self._assert_failed(order, claim, before)

    def test_credit_note_creation_failure_rolls_back_wallet_credit(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        with patch.object(type(self.env['account.move']), 'create',
                          side_effect=UserError('injected credit note failure')):
            self._assert_failed(order, claim, before)

    def test_post_failure_rolls_back_created_credit_note_and_wallet(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        with patch.object(type(self.env['account.move']), 'action_post',
                          side_effect=UserError('injected posting failure')):
            self._assert_failed(order, claim, before)

    def test_false_settlement_after_real_payment_rolls_back_all_effects(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        Sub = type(self.env['sh.subscription.subscription'])
        original = Sub._woow_settle_refund_credit_note

        def false_result(records, *args, **kwargs):
            original(records, *args, **kwargs)
            return False

        with patch.object(Sub, '_woow_settle_refund_credit_note', false_result):
            self._assert_failed(order, claim, before)

    def test_reconcile_exception_rolls_back_posted_refund_and_payment(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        Line = type(self.env['account.move.line'])
        original = Line.reconcile

        def fail(lines):
            original(lines)
            raise UserError('injected after real reconciliation')

        with patch.object(Line, 'reconcile', fail):
            self._assert_failed(order, claim, before)

    def test_terminal_write_failure_rolls_back_all_accounting(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        original = type(self.Order)._transition

        def fail(records, state, **values):
            if state == 'voided':
                raise UserError('injected final ledger write')
            return original(records, state, **values)

        with patch.object(type(self.Order), '_transition', fail):
            self._assert_failed(order, claim, before)

    def test_stale_attempt_wrong_owner_or_event_cannot_refund(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        for key in ('id', 'account_id', 'attempt', 'event_id'):
            with self.subTest(key=key):
                self.assertFalse(self.Order._llm_worker_void_pending(
                    dict(claim, **{key: claim[key] + 100000})))
        self.assertFalse(self.Order._llm_worker_void_pending(
            dict(claim, attempt_uuid=str(uuid.uuid4()))))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(order.state, 'sending')
        self.incident.assert_not_called()

    def test_expired_lease_or_verifier_ownership_cannot_refund(self):
        order = self._order()
        claim = self._claim(order)
        before = self._snapshot()
        _credit_internal(order).write({'lease_until': fields.Datetime.now() - timedelta(seconds=1)})
        self.env.flush_all()
        self.assertFalse(self.Order._llm_worker_void_pending(claim))
        _credit_internal(order).write({
            'lease_until': fields.Datetime.now() + timedelta(minutes=5),
            'verify_token': 'other-verifier',
            'verify_lease_until': fields.Datetime.now() + timedelta(minutes=5),
        })
        self.env.flush_all()
        self.assertFalse(self.Order._llm_worker_void_pending(claim))
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(order.state, 'sending')
        self.incident.assert_not_called()

    def test_manual_and_dr_cancel_without_wallet_or_voucher_mutation(self):
        origin = self._order()
        Runtime = type(self.env['woow_paas_platform.llm_runtime_state'])
        for source in ('manual', 'dr'):
            with self.subTest(source=source), patch.object(Runtime, '_llm_read',
                                                          return_value={'dr_mode': True}):
                if source == 'dr':
                    _credit_internal(origin)._transition('sending')
                    _credit_internal(origin)._transition('credited')
                    _credit_internal(origin)._transition('credit_lost')
                values = {
                    'account_id': self.account.id, 'partner_id': self.partner.id,
                    'newapi_user_id': self.account.newapi_user_id, 'source': source,
                    'points': 100, 'quota': 1562500, 'client_request_id': str(uuid.uuid4()),
                    'reason': 'Test operator verified credit correction',
                }
                values.update({'requested_by_uid': self.operator.id} if source == 'manual'
                              else {'dr_origin_order_id': origin.id})
                order = _credit_internal(self.Order).create(values)
                claim = self._claim(order)
                before = self._snapshot()
                self.assertTrue(self.Order._llm_worker_void_pending(claim))
                self.assertEqual(self._snapshot(), before)
                order.invalidate_recordset()
                self.assertEqual(order.state, 'cancelled')
                self.assertFalse(order.void_credit_note_id)
                self.assertFalse(order.void_wallet_transaction_id)
                self.assertEqual(self.Event.browse(order.convert_event_id).result, 'not_executed')
        self.incident.assert_not_called()
