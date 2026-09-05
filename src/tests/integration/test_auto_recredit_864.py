"""#864 PR-F：BaseCase＋真 Registry cursor／真 40001 的 auto-recredit 守線。

本檔只 mock true-external ECPay query seam；candidate、incident transition、row
lock、wallet credit、reconstruct 與 cleanup 全走 production implementation。
"""
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

import odoo
import psycopg2
import requests
from odoo import api, fields
from odoo.exceptions import ValidationError
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged


@tagged('post_install', '-at_install', '-standard', 'auto_recredit_864')
class TestAutoRecreditPostgresCapability864(BaseCase):
    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.marker = 'F864%s' % uuid.uuid4().hex[:8].upper()
        self.addCleanup(self._purge)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            provider = env['payment.provider'].sudo().search([
                ('code', '=', 'ECPay'),
            ], limit=1)
            if not provider:
                self.skipTest('ECPay provider not configured in this DB')
            provider.write({
                'enable_ecpg': True,
                'state': 'test',
                'MerchantID': '3002607',
                'HashKey': 'pwFHCqoQZGmho4w6',
                'HashIV': 'EkRm7iFT261dpevs',
            })
            method = provider.payment_method_ids[:1] or env.ref(
                'payment.payment_method_unknown')
            if not method.active:
                cr.execute(
                    'UPDATE payment_method SET active = TRUE WHERE id = %s',
                    (method.id,))
            plan = env['woow_paas_platform.topup_plan'].sudo().create({
                'name': '%s plan' % self.marker,
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 5.0,
                'active': True,
            })
            self.provider_id = provider.id
            self.payment_method_id = method.id
            self.currency_id = env.ref('base.TWD').id
            self.plan_id = plan.id
            self.topup_product_preexisted = bool(
                env['product.product'].sudo().search([
                    ('default_code', '=', 'WALLET_TOPUP'),
                ], limit=1))
            cr.execute(
                'DELETE FROM woow_paas_platform_ecpg_query_circuit')
            cr.commit()

    def _purge(self):
        try:
            with self.registry.cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                cr.execute(
                    "SELECT id FROM woow_billing_incident "
                    "WHERE external_ref LIKE %s OR external_ref LIKE %s",
                    (self.marker + '%', 'invoice:' + self.marker + '%'))
                incident_ids = [row[0] for row in cr.fetchall()]
                if incident_ids:
                    for table, model_column in (
                            ('mail_activity', 'res_model'),
                            ('mail_followers', 'res_model'),
                            ('mail_message', 'model')):
                        cr.execute(
                            'DELETE FROM %s WHERE %s = %%s '
                            'AND res_id = ANY(%%s)' %
                            (table, model_column),
                            ('woow.billing.incident', incident_ids))
                    cr.execute(
                        'DELETE FROM woow_billing_incident '
                        'WHERE id = ANY(%s)', (incident_ids,))
                partners = env['res.partner'].sudo().search([
                    ('name', 'like', self.marker),
                ])
                if partners:
                    env['account.move'].sudo().search([
                        ('partner_id', 'in', partners.ids),
                    ]).unlink()
                    env['wallet.transaction'].sudo().search([
                        ('partner_id', 'in', partners.ids),
                    ]).unlink()
                    env['woow.wallet.topup'].sudo().search([
                        ('partner_id', 'in', partners.ids),
                    ]).unlink()
                    env['payment.transaction'].sudo().search([
                        ('partner_id', 'in', partners.ids),
                    ]).unlink()
                    env['payment.token'].sudo().with_context(
                        active_test=False).search([
                            ('partner_id', 'in', partners.ids),
                        ]).unlink()
                    env['loyalty.card'].sudo().search([
                        ('partner_id', 'in', partners.ids),
                    ]).unlink()
                    partners.unlink()
                env['woow_paas_platform.topup_plan'].sudo().browse(
                    self.plan_id).unlink()
                cr.execute(
                    'DELETE FROM woow_paas_platform_ecpg_query_circuit')
                cr.commit()
        finally:
            if not self.topup_product_preexisted:
                try:
                    with self.registry.cursor() as cr:
                        env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                        product = env['product.product'].sudo().search([
                            ('default_code', '=', 'WALLET_TOPUP'),
                        ])
                        if product:
                            env['account.move'].sudo().search([
                                ('invoice_line_ids.product_id',
                                 'in', product.ids),
                            ]).unlink()
                            product.unlink()
                        cr.commit()
                except Exception:
                    pass

    def _reference(self, suffix):
        reference = '%s%s' % (self.marker, suffix)
        self.assertLessEqual(len(reference), 20)
        return reference

    def _new_partner_token(self, env, suffix):
        partner = env['res.partner'].sudo().create({
            'name': '%s-%s' % (self.marker, suffix),
            'email': '%s-%s@example.test' % (self.marker, suffix),
        })
        token = env['payment.token'].sudo().create({
            'provider_id': self.provider_id,
            'partner_id': partner.id,
            'payment_method_id': self.payment_method_id,
            'payment_details': '**** **** **** 0864',
            'provider_ref': '%s-%s' % (self.marker, suffix),
            'ecpg_bindcard_id': '%s-%s' % (self.marker, suffix),
            'ecpg_member_id': str(partner.id),
        })
        return partner, token

    def _seed_branch_a(self, suffix, *, incident_overrides=None,
                       topup_state='failed', tx_state='done'):
        reference = self._reference(suffix)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner, token = self._new_partner_token(env, suffix)
            topup = env['woow.wallet.topup'].sudo().create({
                'partner_id': partner.id,
                'plan_id': self.plan_id,
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 5.0,
                'currency_id': self.currency_id,
                'state': topup_state,
                'source': 'manual',
                'failure_code': 'wallet_credit_failed',
            })
            tx = env['payment.transaction'].sudo().create({
                'provider_id': self.provider_id,
                'payment_method_id': self.payment_method_id,
                'partner_id': partner.id,
                'amount': 100.0,
                'currency_id': self.currency_id,
                'reference': reference,
                'token_id': token.id,
                'operation': 'offline',
                'woow_topup_id': topup.id,
            })
            cr.execute(
                'UPDATE payment_transaction SET state = %s WHERE id = %s',
                (tx_state, tx.id))
            topup.write({'payment_transaction_id': tx.id})
            cr.execute('SELECT pg_current_xact_id()::text')
            origin_xid = cr.fetchone()[0]
            snapshot = {
                'external_ref': reference,
                'kind': 'charged_not_credited',
                'partner_ref': partner.id,
                'payment_token_ref': token.id,
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 5.0,
                'currency': 'TWD',
                'trade_no': '%sTN' % self.marker,
                'rtn_code': '1',
                'failure_code': 'wallet_credit_failed',
                'source_model': 'woow.wallet.topup',
                'source_ref': topup.id,
                'origin_xid': origin_xid,
                'topup_plan_ref': self.plan_id,
                'topup_source': 'manual',
                'detail': 'PR-F integration charged-not-credited fixture.',
            }
            if incident_overrides:
                snapshot.update(incident_overrides)
            incident_id = env['woow.billing.incident']._log(snapshot)
            ids = (partner.id, token.id, topup.id, tx.id, incident_id)
            cr.commit()
        self._age_incident(reference, 16)
        return (reference, *ids)

    def _seed_branch_b(self, suffix):
        reference = self._reference(suffix)
        with self.registry.cursor() as setup_cr:
            env = api.Environment(setup_cr, odoo.SUPERUSER_ID, {})
            partner, token = self._new_partner_token(env, suffix)
            partner_id, token_id = partner.id, token.id
            setup_cr.commit()

        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            topup = env['woow.wallet.topup'].sudo().create({
                'partner_id': partner_id,
                'plan_id': self.plan_id,
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 5.0,
                'currency_id': self.currency_id,
                'state': 'failed',
                'source': 'manual',
                'failure_code': 'wallet_credit_failed',
            })
            tx = env['payment.transaction'].sudo().create({
                'provider_id': self.provider_id,
                'payment_method_id': self.payment_method_id,
                'partner_id': partner_id,
                'amount': 100.0,
                'currency_id': self.currency_id,
                'reference': reference,
                'token_id': token_id,
                'operation': 'offline',
                'woow_topup_id': topup.id,
            })
            topup.write({'payment_transaction_id': tx.id})
            cr.execute('SELECT pg_current_xact_id()::text')
            origin_xid = cr.fetchone()[0]
            source_ref = topup.id
            incident_id = env['woow.billing.incident']._log({
                'external_ref': reference,
                'kind': 'charged_not_credited',
                'partner_ref': partner_id,
                'payment_token_ref': token_id,
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 5.0,
                'currency': 'TWD',
                'trade_no': '%sTN' % self.marker,
                'rtn_code': '1',
                'failure_code': 'wallet_credit_failed',
                'source_model': 'woow.wallet.topup',
                'source_ref': source_ref,
                'origin_xid': origin_xid,
                'topup_plan_ref': self.plan_id,
                'topup_source': 'manual',
                'detail': 'PR-F integration rolled-back fixture.',
            })
            cr.rollback()
        finally:
            cr.close()
        self._age_incident(reference, 16)
        return reference, partner_id, token_id, source_ref, incident_id

    def _age_incident(self, reference, minutes):
        with self.registry.cursor() as cr:
            when = fields.Datetime.now() - timedelta(minutes=minutes)
            cr.execute(
                'UPDATE woow_billing_incident '
                'SET first_seen_at = %s, create_date = %s, '
                'next_query_at = NULL WHERE external_ref = %s',
                (when, when, reference))
            cr.commit()

    def _candidate(self, reference):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            rows = env['woow.wallet.topup']._auto_recredit_candidates()
        return next(row for row in rows if row['external_ref'] == reference)

    def _safe_lookup(self, merchant_trade_no):
        from odoo.addons.payment_ecpay_ecpg.sdk import (
            CreditTradeLookupState,
            ECPGCreditCloseRecord,
            ECPGCreditTradeDetail,
            ECPGCreditTradeLookup,
        )
        return ECPGCreditTradeLookup(
            merchant_trade_no=merchant_trade_no,
            state=CreditTradeLookupState.FOUND,
            detail=ECPGCreditTradeDetail(
                trade_id=864,
                amount=100,
                cls_amt=100,
                auth_time='2026/09/05 11:59:00',
                status='Captured',
                close_data=(ECPGCreditCloseRecord(
                    status='Captured',
                    amount=100,
                    date_time='2026/09/05 12:00:00',
                ),),
            ),
        )

    def _run_cron(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            result = env[
                'woow.wallet.topup'] \
                ._cron_auto_recredit_charged_not_credited()
            cr.commit()
            return result

    def _points(self, partner_id):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT COALESCE(SUM(points), 0) FROM loyalty_card '
                'WHERE partner_id = %s', (partner_id,))
            return float(cr.fetchone()[0])

    def _wallet_tx_count(self, partner_id):
        with self.registry.cursor() as cr:
            cr.execute(
                "SELECT COUNT(*) FROM wallet_transaction "
                "WHERE partner_id = %s AND transaction_type = 'topup'",
                (partner_id,))
            return cr.fetchone()[0]

    def _incident(self, reference):
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT state, skip_reason, query_attempts, '
                'wallet_transaction_ref, repair_topup_ref, next_query_at '
                'FROM woow_billing_incident WHERE external_ref = %s',
                (reference,))
            row = cr.fetchone()
            return row and dict(zip((
                'state', 'skip_reason', 'query_attempts',
                'wallet_transaction_ref', 'repair_topup_ref',
                'next_query_at'), row))

    def _mock_safe_query(self):
        def _query(_client, merchant_trade_no):
            return self._safe_lookup(merchant_trade_no)
        return patch(
            'odoo.addons.payment_ecpay_ecpg.sdk.'
            'ECPGClient.query_credit_trade',
            autospec=True,
            side_effect=_query,
        )

    def test_xid8_capability_probe_is_a_real_postgres_query(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            self.assertTrue(
                env['woow.wallet.topup']._auto_recredit_pg_capable())

    def test_branch_a_safe_safe_credits_exactly_once(self):
        reference, partner_id, _token, topup_id, _tx, _incident = (
            self._seed_branch_a('A1'))
        before = self._points(partner_id)
        with self._mock_safe_query() as query, patch(
                'requests.sessions.Session.request',
                side_effect=AssertionError('unexpected real network')) as network:
            summary = self._run_cron()
        self.assertEqual(query.call_count, 2)
        network.assert_not_called()
        self.assertEqual(summary['credited'], 1)
        self.assertEqual(self._points(partner_id) - before, 105.0)
        self.assertEqual(self._wallet_tx_count(partner_id), 1)
        row = self._incident(reference)
        self.assertEqual(row['state'], 'resolved')
        self.assertEqual(row['query_attempts'], 2)
        self.assertTrue(row['wallet_transaction_ref'])
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT state FROM woow_wallet_topup WHERE id = %s',
                (topup_id,))
            self.assertEqual(cr.fetchone()[0], 'done')

        with self._mock_safe_query() as second_query:
            self._run_cron()
        second_query.assert_not_called()
        self.assertEqual(self._wallet_tx_count(partner_id), 1)
        self.assertEqual(self._points(partner_id) - before, 105.0)

    def test_branch_a_cross_row_mismatch_fails_closed(self):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            other = env['res.partner'].sudo().create({
                'name': '%s-other' % self.marker,
            })
            other_id = other.id
            cr.commit()
        reference, partner_id, *_rest = self._seed_branch_a(
            'A2', incident_overrides={'partner_ref': other_id})
        with self._mock_safe_query() as query:
            summary = self._run_cron()
        self.assertEqual(query.call_count, 2)
        self.assertEqual(summary['needs_human'], 1)
        self.assertEqual(self._wallet_tx_count(partner_id), 0)
        self.assertEqual(self._points(partner_id), 0.0)
        self.assertEqual(self._incident(reference)['state'], 'needs_human')

    def test_branch_b_reconstructs_only_after_real_outer_rollback(self):
        reference, partner_id, _token_id, old_topup_id, _incident_id = (
            self._seed_branch_b('B1'))
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT 1 FROM woow_wallet_topup WHERE id = %s',
                (old_topup_id,))
            self.assertFalse(cr.fetchone(), '前置：outer rollback 必須真刪 source')
        before = self._points(partner_id)
        with self._mock_safe_query() as query, patch(
                'requests.sessions.Session.request',
                side_effect=AssertionError('unexpected real network')) as network:
            summary = self._run_cron()
        self.assertEqual(query.call_count, 2)
        network.assert_not_called()
        self.assertEqual(summary['reconstructed'], 1)
        self.assertEqual(self._points(partner_id) - before, 105.0)
        self.assertEqual(self._wallet_tx_count(partner_id), 1)
        row = self._incident(reference)
        self.assertEqual(row['state'], 'resolved')
        self.assertTrue(row['repair_topup_ref'])
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            cr.execute(
                'SELECT repair_incident_ref, invoice_account_move_id, '
                'payment_transaction_id FROM woow_wallet_topup '
                'WHERE id = %s', (row['repair_topup_ref'],))
            repair = cr.fetchone()
            self.assertTrue(repair[0])
            self.assertIsNone(repair[1])
            repaired_tx = env['payment.transaction'].sudo().browse(repair[2])
            try:
                repaired_tx._set_error('late provider error')
            except ValidationError:
                pass
            else:
                self.fail(
                    'reconstructed tx 的 late _set_error 必須 fail-loud')
            cr.execute(
                'SELECT state, kind FROM woow_billing_incident '
                'WHERE external_ref = %s', ('invoice:' + reference,))
            self.assertEqual(
                cr.fetchone(), ('needs_human', 'manual_invoice_stale'))

    def test_branch_b_missing_plan_fails_closed(self):
        reference, partner_id, *_rest = self._seed_branch_b('B2')
        with self.registry.cursor() as cr:
            cr.execute(
                'UPDATE woow_billing_incident SET topup_plan_ref = NULL '
                'WHERE external_ref = %s', (reference,))
            cr.commit()
        with self._mock_safe_query():
            summary = self._run_cron()
        self.assertEqual(summary['needs_human'], 1)
        self.assertEqual(self._wallet_tx_count(partner_id), 0)
        self.assertEqual(self._points(partner_id), 0.0)

    def test_shared_403_circuit_blocks_second_incident_without_http(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import (
            ECPGQueryForbiddenError,
        )
        first = self._seed_branch_a('C1')[0]
        second = self._seed_branch_a('C2')[0]
        TopupClass = type(self._model('woow.wallet.topup'))
        calls = []

        def _forbidden(_self, candidate, provider=None):
            calls.append(candidate['external_ref'])
            raise ECPGQueryForbiddenError('rate limited')

        with patch.object(
                TopupClass, '_auto_recredit_query', autospec=True,
                side_effect=_forbidden), patch(
                'requests.sessions.Session.request',
                side_effect=AssertionError('unexpected real network')) as network:
            summary = self._run_cron()
        network.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary['skipped_by_reason']['query_circuit_open'], 2)
        self.assertEqual(self._incident(first)['skip_reason'],
                         'query_circuit_open')
        self.assertEqual(self._incident(second)['skip_reason'],
                         'query_circuit_open')

    def _model(self, name):
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            return env[name]

    def test_possible_manual_credit_stops_before_query(self):
        reference, partner_id, *_rest = self._seed_branch_a('M1')
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            env['wallet.transaction'].sudo().create({
                'partner_id': partner_id,
                'transaction_type': 'adjustment',
                'amount': 105.0,
                'description': 'manual support adjustment',
                'balance_after': 105.0,
            })
            cr.commit()
        with self._mock_safe_query() as query:
            summary = self._run_cron()
        query.assert_not_called()
        self.assertEqual(summary['needs_human'], 1)
        self.assertEqual(
            self._incident(reference)['skip_reason'],
            'possible_manual_credit')

    def test_cleanup_preserves_unresolved_cni_and_records_skip(self):
        reference, _partner, _token, topup_id, tx_id, _incident = (
            self._seed_branch_a('K1', topup_state='pending',
                                tx_state='pending'))
        with self.registry.cursor() as cr:
            when = fields.Datetime.now() - timedelta(minutes=70)
            cr.execute(
                'UPDATE woow_wallet_topup SET create_date = %s '
                'WHERE id = %s', (when, topup_id))
            cr.commit()
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            TopupClass = type(env['woow.wallet.topup'])
            with patch.object(
                    TopupClass, '_cron_replay_callback_inbox',
                    return_value={}), patch.object(
                    TopupClass,
                    '_cron_auto_recredit_charged_not_credited',
                    return_value={}):
                env['woow.wallet.topup']._cron_cleanup_pending_topups()
                cr.commit()
        with self.registry.cursor() as cr:
            cr.execute(
                'SELECT state FROM woow_wallet_topup WHERE id = %s',
                (topup_id,))
            self.assertEqual(cr.fetchone()[0], 'pending')
            cr.execute(
                'SELECT state FROM payment_transaction WHERE id = %s',
                (tx_id,))
            self.assertEqual(cr.fetchone()[0], 'pending')
        self.assertEqual(
            self._incident(reference)['skip_reason'],
            'cleanup_waiting_auto_recredit')

    def test_manual_charge_freezes_cni_before_exception_and_survives_rollback(self):
        reference = self._reference('N1')
        with self.registry.cursor() as setup_cr:
            env = api.Environment(setup_cr, odoo.SUPERUSER_ID, {})
            partner, token = self._new_partner_token(env, 'N1')
            partner_id, token_id = partner.id, token.id
            setup_cr.commit()

        cr = self.registry.cursor()
        try:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            token = env['payment.token'].sudo().browse(token_id)
            topup = env['woow.wallet.topup'].sudo().create({
                'partner_id': partner_id,
                'plan_id': self.plan_id,
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 5.0,
                'currency_id': self.currency_id,
                'state': 'draft',
                'source': 'manual',
            })
            topup_id = topup.id
            topup._log_topup_intent(reference, token=token,
                                    charge_amount=100)
            TxClass = type(env['payment.transaction'])

            def _done_then_raise(tx_self):
                tx_self.state = 'done'
                raise RuntimeError('post-done failure')

            caught = None
            with patch.object(
                    TxClass, '_send_payment_request', autospec=True,
                    side_effect=_done_then_raise), patch(
                    'requests.sessions.Session.request',
                    side_effect=AssertionError('unexpected real network')) \
                    as network:
                try:
                    token._charge(
                        100, reference, self.currency_id,
                        purpose_field='woow_topup_id',
                        purpose_id=topup_id,
                    )
                except RuntimeError as exc:
                    caught = exc
            network.assert_not_called()
            self.assertTrue(
                getattr(caught, '_woow_charged_not_credited', False))
            cr.rollback()
        finally:
            cr.close()
        row = self._incident(reference)
        self.assertTrue(row)
        self.assertEqual(row['state'], 'open')
        with self.registry.cursor() as verify:
            verify.execute(
                'SELECT kind, origin_xid FROM woow_billing_incident '
                'WHERE external_ref = %s', (reference,))
            kind, origin_xid = verify.fetchone()
            self.assertEqual(kind, 'charged_not_credited')
            self.assertTrue(origin_xid)
            verify.execute(
                'SELECT 1 FROM woow_wallet_topup WHERE id = %s',
                (topup_id,))
            self.assertFalse(
                verify.fetchone(),
                'outer rollback 後只准 durable incident 存活',
            )

    def test_manual_network_timeout_keeps_intent_and_never_promotes_cni(self):
        reference = self._reference('N2')
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner, token = self._new_partner_token(env, 'N2')
            topup = env['woow.wallet.topup'].sudo().create({
                'partner_id': partner.id,
                'plan_id': self.plan_id,
                'amount': 100.0,
                'credits': 100.0,
                'bonus': 5.0,
                'currency_id': self.currency_id,
                'state': 'draft',
                'source': 'manual',
            })
            self.assertTrue(topup._log_topup_intent(
                reference, token=token, charge_amount=100))
            TxClass = type(env['payment.transaction'])
            caught = None
            with patch.object(
                    TxClass, '_send_payment_request', autospec=True,
                    side_effect=requests.exceptions.Timeout('timeout')), \
                    patch('requests.sessions.Session.request',
                          side_effect=AssertionError(
                              'unexpected real network')) as network:
                try:
                    token._charge(
                        100, reference, self.currency_id,
                        purpose_field='woow_topup_id',
                        purpose_id=topup.id,
                    )
                except requests.exceptions.Timeout as exc:
                    caught = exc
            network.assert_not_called()
            self.assertIsNotNone(caught)
            self.assertFalse(getattr(
                caught, '_woow_charged_not_credited', False))
            cr.rollback()
        with self.registry.cursor() as verify:
            verify.execute(
                'SELECT kind FROM woow_billing_incident '
                'WHERE external_ref = %s', (reference,))
            self.assertEqual(verify.fetchone()[0], 'intent')

    def test_stale_heartbeat_escalates_at_t2_without_query(self):
        reference = self._seed_branch_a('H1')[0]
        self._age_incident(reference, 241)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            env['ir.config_parameter'].sudo().set_param(
                'woow_paas_platform.auto_recredit_heartbeat_at',
                fields.Datetime.to_string(
                    fields.Datetime.now() - timedelta(minutes=31)),
            )
            with patch(
                    'odoo.addons.payment_ecpay_ecpg.sdk.'
                    'ECPGClient.query_credit_trade') as query:
                transitioned = env[
                    'sh.subscription.subscription'] \
                    ._escalate_stale_auto_recredit_heartbeat()
            cr.commit()
        query.assert_not_called()
        self.assertEqual(transitioned, 1)
        self.assertEqual(self._incident(reference)['state'], 'needs_human')

    def test_fresh_heartbeat_does_not_escalate_at_t2(self):
        reference = self._seed_branch_a('H2')[0]
        self._age_incident(reference, 241)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            env['ir.config_parameter'].sudo().set_param(
                'woow_paas_platform.auto_recredit_heartbeat_at',
                fields.Datetime.to_string(fields.Datetime.now()),
            )
            transitioned = env[
                'sh.subscription.subscription'] \
                ._escalate_stale_auto_recredit_heartbeat()
            cr.commit()
        self.assertEqual(transitioned, 0)
        self.assertEqual(self._incident(reference)['state'], 'open')

    def test_true_40001_is_recorded_then_next_round_credits_once(self):
        reference, partner_id, _token, _topup, _tx, _incident = (
            self._seed_branch_a('R1'))
        candidate = self._candidate(reference)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            partner = env['res.partner'].sudo().browse(partner_id)
            card = partner.get_wallet_loyalty_card()
            card_id = card.id
            cr.commit()
            PartnerClass = type(partner)
            original_get_card = PartnerClass.get_wallet_loyalty_card

        ready = threading.Event()
        updated = threading.Event()

        def _concurrent_update():
            cr = self.registry._db.cursor()
            try:
                self.assertTrue(ready.wait(timeout=30))
                cr.execute(
                    'UPDATE loyalty_card SET points = points + 0 '
                    'WHERE id = %s', (card_id,))
                cr.commit()
            finally:
                updated.set()
                cr.close()

        def _get_card_then_wait(record):
            result = original_get_card(record)
            ready.set()
            self.assertTrue(updated.wait(timeout=30))
            return result

        thread = threading.Thread(
            target=_concurrent_update, name='f864-card-40001')
        thread.start()
        before = self._points(partner_id)
        with patch.object(
                PartnerClass, 'get_wallet_loyalty_card', autospec=True,
                side_effect=_get_card_then_wait):
            with self.registry.cursor() as cr:
                env = api.Environment(cr, odoo.SUPERUSER_ID, {})
                result = env['woow.wallet.topup']._auto_recredit_credit(
                    candidate, 'captured_amount_confirmed',
                    self.provider_id, '3002607')
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ('failed', 'serialization_failure'))
        self.assertEqual(self._points(partner_id), before)
        self.assertEqual(self._wallet_tx_count(partner_id), 0)
        self.assertEqual(self._incident(reference)['skip_reason'],
                         'credit_failed')

        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            second = env['woow.wallet.topup']._auto_recredit_credit(
                candidate, 'captured_amount_confirmed',
                self.provider_id, '3002607')
        self.assertEqual(second[0], 'credited')
        self.assertEqual(self._points(partner_id) - before, 105.0)
        self.assertEqual(self._wallet_tx_count(partner_id), 1)
        self.assertEqual(self._incident(reference)['state'], 'resolved')

    def test_negative_control_without_independent_cursor_would_rollback(self):
        """永久 control：same-cursor rollback 必須刪列；mutation 另留紅證。"""
        marker = 'test:%s' % uuid.uuid4().hex
        with self.registry.cursor() as cr:
            cr.execute(
                "INSERT INTO woow_billing_incident (external_ref, kind, state, "
                "skip_count, attempts, query_attempts, first_seen_at, "
                "last_seen_at, occurrences, create_uid, write_uid, "
                "create_date, write_date) VALUES (%s, "
                "'charged_not_credited', 'open', 0, 0, 0, NOW(), NOW(), "
                "1, %s, %s, NOW(), NOW())",
                (marker, odoo.SUPERUSER_ID, odoo.SUPERUSER_ID))
            cr.rollback()
        with self.registry.cursor() as verify:
            verify.execute(
                'SELECT 1 FROM woow_billing_incident '
                'WHERE external_ref = %s', (marker,))
            self.assertFalse(verify.fetchone())
