"""#864 PR-F：vendor contract、時間模型、typed error 與零外呼守線。"""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase


class TestAutoRecreditQueryGate864(TransactionCase):
    def setUp(self):
        super().setUp()
        self.Topup = self.env['woow.wallet.topup']
        self.TopupClass = type(self.Topup)
        self.IncidentClass = type(self.env['woow.billing.incident'])
        self.CircuitClass = type(
            self.env['woow_paas_platform.ecpg_query_circuit'])

    def _candidate(self, *, age_minutes=16, **overrides):
        now = fields.Datetime.now()
        values = {
            'id': 864001,
            'external_ref': 'TOPUP864QUERY000001',
            'partner_ref': 101,
            'payment_token_ref': 202,
            'amount': 100.0,
            'credits': 100.0,
            'bonus': 5.0,
            'currency': 'TWD',
            'trade_no': '240905000000001',
            'source_ref': 303,
            'origin_xid': '12345',
            'topup_plan_ref': 404,
            'topup_source': 'manual',
            'first_seen_at': now - timedelta(minutes=age_minutes),
            'query_attempts': 0,
            't1_overdue_at': False,
        }
        values.update(overrides)
        return values

    def _lookup(self, *, status='Captured', amount=100, cls_amt=100,
                close_status='Captured', close_amount=100,
                with_close=True, not_found=False):
        from odoo.addons.payment_ecpay_ecpg.sdk import (
            CreditTradeLookupState,
            ECPGCreditCloseRecord,
            ECPGCreditTradeDetail,
            ECPGCreditTradeLookup,
        )
        if not_found:
            return ECPGCreditTradeLookup(
                merchant_trade_no='TOPUP864QUERY000001',
                state=CreditTradeLookupState.NOT_FOUND,
                detail=None,
            )
        close_data = ()
        if with_close:
            close_data = (ECPGCreditCloseRecord(
                status=close_status,
                amount=close_amount,
                date_time='2026/09/05 12:00:00',
            ),)
        return ECPGCreditTradeLookup(
            merchant_trade_no='TOPUP864QUERY000001',
            state=CreditTradeLookupState.FOUND,
            detail=ECPGCreditTradeDetail(
                trade_id=864,
                amount=amount,
                cls_amt=cls_amt,
                auth_time='2026/09/05 11:59:00',
                status=status,
                close_data=close_data,
            ),
        )

    def _assessment(self, **kwargs):
        return self._lookup(**kwargs).assess_for_credit(
            expected_amount=100)

    def _provider(self):
        return SimpleNamespace(
            id=501,
            MerchantID='3002607',
            HashKey='pwFHCqoQZGmho4w6',
            HashIV='EkRm7iFT261dpevs',
            state='test',
        )

    def _orchestration_patches(self, provider, query_side_effect):
        """回傳供個別測試組合的 patchers；不 mock vendor enum/dataclass。"""
        return (
            patch.object(self.TopupClass, '_auto_recredit_enabled',
                         return_value=True),
            patch.object(self.TopupClass, '_auto_recredit_credit_cap_ok',
                         return_value=True),
            patch.object(self.TopupClass, '_auto_recredit_partner_daily_ok',
                         return_value=True),
            patch.object(self.TopupClass,
                         '_auto_recredit_possible_manual_credit',
                         return_value=False),
            patch.object(self.TopupClass, '_auto_recredit_provider',
                         return_value=provider),
            patch.object(self.TopupClass, '_auto_recredit_query',
                         side_effect=query_side_effect),
            patch.object(self.CircuitClass, '_blocked_until',
                         return_value=None),
            patch.object(self.CircuitClass, '_record_success',
                         return_value=None),
            patch.object(self.IncidentClass,
                         '_record_query_outcome_independent',
                         return_value=True),
        )

    def test_vendor_enum_mappings_are_exhaustive(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import (
            CreditGateReason, CreditGateVerdict,
        )
        self.assertEqual(
            set(self.Topup._AUTO_RECREDIT_VERDICT_ACTIONS),
            {item.value for item in CreditGateVerdict},
        )
        self.assertEqual(
            set(self.Topup._AUTO_RECREDIT_REASON_ACTIONS),
            {item.value for item in CreditGateReason},
        )

    def test_reason_action_matrix_is_closed_and_intentional(self):
        self.assertEqual(
            self.Topup._AUTO_RECREDIT_REASON_ACTIONS,
            {
                'captured_amount_confirmed': 'credit_after_second_query',
                'not_found_or_not_visible': 'retry_short',
                'awaiting_capture': 'retry_short',
                'adverse_lifecycle': 'needs_human',
                'adverse_close_record': 'needs_human',
                'amount_mismatch': 'needs_human',
                'closed_amount_mismatch': 'needs_human',
                'close_data_missing': 'retry_medium',
                'close_data_amount_mismatch': 'retry_medium',
                'unknown_lifecycle': 'retry_medium',
            },
        )

    def test_all_ten_vendor_reasons_map_to_the_expected_actions(self):
        cases = (
            (self._assessment(), 'credit_after_second_query'),
            (self._lookup(not_found=True).assess_for_credit(
                expected_amount=100), 'retry_until_t2'),
            (self._assessment(status='Authorized', cls_amt=0,
                              with_close=False), 'retry_until_t2'),
            (self._assessment(status='Canceled'), 'needs_human'),
            (self._assessment(close_status='Cancelled'), 'needs_human'),
            (self._assessment(amount=99, cls_amt=99), 'needs_human'),
            (self._assessment(cls_amt=99), 'needs_human'),
            (self._assessment(with_close=False), 'retry_until_t2'),
            (self._assessment(close_amount=99), 'retry_until_t2'),
            (self._assessment(status='FutureStatus'), 'retry_until_t2'),
        )
        reasons = set()
        for assessment, expected in cases:
            with self.subTest(reason=assessment.reason.value):
                reasons.add(assessment.reason.value)
                self.assertEqual(
                    self.Topup._auto_recredit_assessment_action(assessment),
                    expected,
                )
        self.assertEqual(len(reasons), 10)

    def test_mismatched_verdict_reason_pair_fails_closed(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import (
            CreditGateReason, CreditGateVerdict,
        )
        assessment = SimpleNamespace(
            verdict=CreditGateVerdict.SAFE,
            reason=CreditGateReason.ADVERSE_LIFECYCLE,
        )
        self.assertFalse(
            self.Topup._auto_recredit_assessment_action(assessment))

    def test_tilde_php_urlencode_contract_is_preserved(self):
        """Literal PHP urlencode＋官方 PHP AES vector，非 self round-trip。"""
        from odoo.addons.payment_ecpay_ecpg.sdk.aes_crypto import (
            _ecpay_urlencode, encrypt,
        )
        raw = '{"Name":"test!*\'()~value"}'
        self.assertEqual(
            _ecpay_urlencode(raw),
            '%7B%22Name%22%3A%22test%21%2A%27%28%29%7Evalue%22%7D',
        )
        self.assertEqual(
            encrypt(
                {'Name': "test!*'()~value"},
                'ejCk326UnaZWKisg',
                'q9jcZX8Ib9LM8wYk',
            ),
            'uvI4yrErM37XNQkXGAgRgBuDOiJoVs72Xn/rum9Ejl1DSna4HyLSoY7764Pm'
            'hTR7JXb9jJWLSjCGcZEDeFiABg==',
        )

    def test_t0_t1_and_t2_boundaries(self):
        now = datetime(2026, 1, 1, 12, 0)
        for minutes, expected in (
                (9, 't0'), (10, 'ready'), (16, 't1'), (240, 't2')):
            with self.subTest(minutes=minutes):
                candidate = {'first_seen_at': now - timedelta(minutes=minutes)}
                self.assertEqual(
                    self.Topup._auto_recredit_timing(candidate, now),
                    expected,
                )

    def test_t0_never_calls_query_and_schedules_exact_t0(self):
        candidate = self._candidate(age_minutes=9)
        expected = (
            fields.Datetime.to_datetime(candidate['first_seen_at'])
            + timedelta(minutes=10)
        )
        with patch.object(
                self.TopupClass, '_auto_recredit_enabled',
                return_value=True), patch.object(
                self.TopupClass, '_auto_recredit_credit_cap_ok',
                return_value=True), patch.object(
                self.TopupClass, '_auto_recredit_partner_daily_ok',
                return_value=True), patch.object(
                self.TopupClass, '_auto_recredit_possible_manual_credit',
                return_value=False), patch.object(
                self.TopupClass, '_auto_recredit_query') as query, patch.object(
                self.IncidentClass,
                '_record_query_outcome_independent',
                return_value=True) as record:
            self.assertEqual(
                self.Topup._auto_recredit_one(candidate),
                ('skipped', 'too_early'),
            )
        query.assert_not_called()
        self.assertEqual(record.call_args.kwargs['next_query_at'], expected)
        self.assertFalse(record.call_args.kwargs['increment_attempt'])

    def test_t2_never_calls_query_and_escalates(self):
        candidate = self._candidate(age_minutes=240)
        with patch.object(
                self.TopupClass, '_auto_recredit_query') as query, patch.object(
                self.TopupClass, '_auto_recredit_mark_terminal',
                return_value=('needs_human', 't2_expired')) as terminal:
            self.assertEqual(
                self.Topup._auto_recredit_one(candidate),
                ('needs_human', 't2_expired'),
            )
        query.assert_not_called()
        terminal.assert_called_once_with(candidate, 't2_expired')

    def test_t1_is_soft_safe_safe_still_queries_twice_and_credits(self):
        provider = self._provider()
        safe = self._assessment()
        candidate = self._candidate(age_minutes=16)
        patchers = self._orchestration_patches(
            provider, [(provider, safe), (provider, safe)])
        with patchers[0], patchers[1], patchers[2], patchers[3], \
                patchers[4], patchers[5] as query, patchers[6], patchers[7], \
                patchers[8] as record, patch.object(
                    self.TopupClass, '_auto_recredit_credit',
                    return_value=('credited',
                                  'captured_amount_confirmed')) as credit, \
                patch('requests.sessions.Session.request',
                      side_effect=AssertionError('unexpected real network')) \
                as network:
            result = self.Topup._auto_recredit_one(candidate)
        self.assertEqual(result, ('credited', 'captured_amount_confirmed'))
        self.assertEqual(query.call_count, 2)
        self.assertEqual(record.call_count, 2)
        self.assertTrue(all(
            call.kwargs['t1_overdue'] for call in record.call_args_list))
        credit.assert_called_once()
        network.assert_not_called()

    def test_second_query_unsafe_never_credits(self):
        provider = self._provider()
        safe = self._assessment()
        unsafe = self._assessment(status='Unauthorized')
        candidate = self._candidate()
        patchers = self._orchestration_patches(
            provider, [(provider, safe), (provider, unsafe)])
        with patchers[0], patchers[1], patchers[2], patchers[3], \
                patchers[4], patchers[5] as query, patchers[6], patchers[7], \
                patchers[8], patch.object(
                    self.TopupClass, '_auto_recredit_credit') as credit, \
                patch.object(
                    self.TopupClass, '_auto_recredit_mark_terminal',
                    return_value=('needs_human', 'query_unsafe')) as terminal:
            result = self.Topup._auto_recredit_one(candidate)
        self.assertEqual(query.call_count, 2)
        credit.assert_not_called()
        self.assertEqual(result, ('needs_human', 'query_unsafe'))
        self.assertEqual(
            terminal.call_args.args[2], 'adverse_lifecycle')

    def test_indeterminate_retries_after_t1_instead_of_returning(self):
        provider = self._provider()
        awaiting = self._assessment(
            status='To be captured', cls_amt=0, with_close=False)
        candidate = self._candidate(age_minutes=16)
        patchers = self._orchestration_patches(
            provider, [(provider, awaiting)])
        with patchers[0], patchers[1], patchers[2], patchers[3], \
                patchers[4], patchers[5] as query, patchers[6], patchers[7], \
                patchers[8] as record:
            result = self.Topup._auto_recredit_one(candidate)
        self.assertEqual(result, ('skipped', 'query_indeterminate'))
        query.assert_called_once()
        self.assertEqual(
            record.call_args.kwargs['reason'], 'awaiting_capture')
        self.assertTrue(record.call_args.kwargs['t1_overdue'])

    def test_first_403_clamps_next_query_to_t2(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import ECPGQueryForbiddenError
        candidate = self._candidate(age_minutes=230)
        provider = self._provider()
        blocked = fields.Datetime.now() + timedelta(minutes=30)
        deadline = (
            fields.Datetime.to_datetime(candidate['first_seen_at'])
            + timedelta(hours=4)
        )
        with patch.object(
                self.CircuitClass, '_record_forbidden',
                return_value=(1, blocked)), patch.object(
                self.IncidentClass,
                '_record_query_outcome_independent',
                return_value=True) as record:
            result = self.Topup._auto_recredit_handle_query_exception(
                candidate, provider, ECPGQueryForbiddenError('403'))
        self.assertEqual(result, ('skipped', 'query_circuit_open'))
        self.assertEqual(record.call_args.kwargs['next_query_at'], deadline)

    def test_repeated_403_claims_one_human_alert(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import ECPGQueryForbiddenError
        candidate = self._candidate()
        provider = self._provider()
        with patch.object(
                self.CircuitClass, '_record_forbidden',
                return_value=(2, fields.Datetime.now()
                              + timedelta(minutes=30))), patch.object(
                self.CircuitClass, '_claim_repeat_forbidden_alert',
                return_value=True), patch.object(
                self.IncidentClass,
                '_record_query_outcome_independent',
                return_value=True), patch.object(
                self.TopupClass, '_auto_recredit_mark_terminal',
                return_value=('needs_human', 'query_circuit_open')) as terminal:
            result = self.Topup._auto_recredit_handle_query_exception(
                candidate, provider, ECPGQueryForbiddenError('403'))
        self.assertEqual(result, ('needs_human', 'query_circuit_open'))
        terminal.assert_called_once()

    def test_typed_terminal_errors_do_not_retry(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import (
            ECPGConfigError,
            ECPGCreditTradeQueryError,
            ECPGProtocolError,
        )
        candidate = self._candidate()
        provider = self._provider()
        cases = (
            (ECPGConfigError('bad config'), 'query_config_error'),
            (ECPGProtocolError('bad protocol'), 'query_protocol_error'),
            (ECPGCreditTradeQueryError(
                'error', provider_code='error'), 'query_business_error'),
        )
        for exc, reason in cases:
            with self.subTest(reason=reason), patch.object(
                    self.IncidentClass,
                    '_record_query_outcome_independent',
                    return_value=True), patch.object(
                    self.TopupClass, '_auto_recredit_mark_terminal',
                    return_value=('needs_human', reason)) as terminal:
                result = self.Topup._auto_recredit_handle_query_exception(
                    candidate, provider, exc)
            self.assertEqual(result, ('needs_human', reason))
            terminal.assert_called_once_with(candidate, reason)

    def test_transient_and_timeout_are_durable_retries(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import ECPGTransientError
        candidate = self._candidate(query_attempts=2)
        provider = self._provider()
        for exc in (
                ECPGTransientError('500'),
                requests.exceptions.Timeout('timeout'),
                requests.exceptions.ConnectionError('offline')):
            with self.subTest(error=type(exc).__name__), patch.object(
                    self.IncidentClass,
                    '_record_query_outcome_independent',
                    return_value=True) as record:
                result = self.Topup._auto_recredit_handle_query_exception(
                    candidate, provider, exc)
            self.assertEqual(result, ('skipped', 'query_transient'))
            self.assertTrue(record.call_args.kwargs['increment_attempt'])

    def test_platform_constructs_stage_client_while_network_stays_blocked(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import ECPGClient
        provider = self._provider()
        candidate = self._candidate()
        seen = []

        def _capture(client, merchant_trade_no):
            seen.append((
                client._hosts['ecpayment'], merchant_trade_no,
            ))
            return self._lookup()

        with patch.object(
                ECPGClient, 'query_credit_trade', autospec=True,
                side_effect=_capture), patch(
                'requests.sessions.Session.request',
                side_effect=AssertionError('unexpected real network')) \
                as network:
            _provider, assessment = self.Topup._auto_recredit_query(
                candidate, provider)
        network.assert_not_called()
        self.assertTrue(assessment.is_safe_to_credit)
        self.assertEqual(seen, [(
            'https://ecpayment-stage.ecpay.com.tw',
            candidate['external_ref'],
        )])

    def test_low_level_network_sentinel_proves_invalid_mtn_never_calls_http(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import ECPGClient
        with patch(
                'requests.post',
                side_effect=AssertionError('network must stay mocked')) as post:
            with self.assertRaises(ValueError):
                ECPGClient(
                    '3002607', 'pwFHCqoQZGmho4w6',
                    'EkRm7iFT261dpevs',
                ).query_credit_trade('bad-mtn')
        post.assert_not_called()

    def test_vendor_error_taxonomy_stays_typed(self):
        from odoo.addons.payment_ecpay_ecpg.sdk import (
            ECPGCreditTradeQueryError,
            ECPGProtocolError,
            ECPGQueryForbiddenError,
            ECPGTransientError,
        )
        self.assertEqual(
            ECPGQueryForbiddenError('x').retry_after_seconds, 1800)
        self.assertFalse(ECPGCreditTradeQueryError(
            'x', provider_code='error').retryable)
        self.assertFalse(ECPGProtocolError('x').retryable)
        self.assertEqual(ECPGTransientError('x').category, 'transient')

    def test_named_partial_indexes_exist_after_module_init(self):
        """CI 先以 ``-i`` 建 fresh DB；本測證明兩個 init index 都已落地。

        這不是只驗 upgrade：若 account.move init 在 topup table 建立前跨表查詢，
        workflow 會先死在 Install step，根本到不了此 method。
        """
        for index_name in (
                'wallet_transaction_topup_payment_tx_uniq',
                'account_move_woow_ecpay_mtn_uniq'):
            with self.subTest(index=index_name):
                self.env.cr.execute(
                    'SELECT to_regclass(%s)', (index_name,))
                self.assertEqual(self.env.cr.fetchone()[0], index_name)

    def test_preflight_sql_is_exactly_three_read_only_selects(self):
        path = (
            Path(__file__).resolve().parents[2]
            / 'scripts' / '864-f-auto-recredit-preflight.sql'
        )
        statements = [
            chunk.strip() for chunk in path.read_text().split(';')
            if chunk.strip()
        ]
        self.assertEqual(len(statements), 3)
        for statement in statements:
            sql = '\n'.join(
                line for line in statement.splitlines()
                if not line.lstrip().startswith('--')
            ).lstrip()
            self.assertTrue(sql.upper().startswith('SELECT COUNT(*)'))
            self.assertNotRegex(
                sql.upper(),
                r'\b(INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE)\b',
            )

    def test_query_circuit_rejects_generic_orm_mutations(self):
        Circuit = self.env['woow_paas_platform.ecpg_query_circuit']
        with self.assertRaises(AccessError):
            Circuit.create({'scope_key': 'forged'})
        Circuit._record_forbidden('3002607', 1, 1800)
        scope_key = Circuit._scope_key('3002607')
        with self.env.registry.cursor() as cr:
            cr.execute(
                'SELECT id FROM woow_paas_platform_ecpg_query_circuit '
                'WHERE scope_key = %s', (scope_key,))
            circuit_id = cr.fetchone()[0]
            env = self.env(cr=cr)
            record = env['woow_paas_platform.ecpg_query_circuit'].browse(
                circuit_id)
            with self.assertRaises(AccessError):
                record.write({'blocked_until': fields.Datetime.now()})
            with self.assertRaises(AccessError):
                record.unlink()
            cr.execute(
                'DELETE FROM woow_paas_platform_ecpg_query_circuit '
                'WHERE id = %s', (circuit_id,))
            cr.commit()

    def test_audit_metadata_rejects_forged_context_but_internal_sentinel_works(self):
        from odoo.addons.woow_paas_platform.models.account_move_invoice_status import (
            _ecpay_mtn_write,
        )
        from odoo.addons.woow_paas_platform.models.wallet_topup import (
            _AUTO_RECREDIT_CAPABILITY,
            _AUTO_RECREDIT_SKIP_INVOICE_CTX,
        )
        from odoo.addons.woow_paas_platform.models.wallet_transaction import (
            _wallet_credit_metadata,
        )
        partner = self.env['res.partner'].sudo().create({
            'name': '864 audit metadata guard',
        })
        transaction = self.env['wallet.transaction'].sudo().create({
            'partner_id': partner.id,
            'transaction_type': 'adjustment',
            'amount': 1.0,
            'description': 'guard fixture',
            'balance_after': 1.0,
        })
        with self.assertRaises(AccessError):
            transaction.with_context(
                _woow_wallet_credit_metadata_write=True,
            ).write({'woow_credit_origin': 'auto_recredit'})
        _wallet_credit_metadata(transaction).write({
            'woow_credit_origin': 'auto_recredit',
            'woow_repair_incident_ref': 864,
        })
        self.assertEqual(transaction.woow_credit_origin, 'auto_recredit')

        topup = self.env['woow.wallet.topup'].sudo().create({
            'partner_id': partner.id,
            'amount': 1.0,
            'credits': 1.0,
            'bonus': 0.0,
            'currency_id': self.env.ref('base.TWD').id,
            'state': 'failed',
            'source': 'auto',
        })
        with self.assertRaises(AccessError):
            topup.with_context(
                _woow_auto_recredit_skip_invoice=True,
            ).write({'repair_incident_ref': 864})
        topup.with_context(**{
            _AUTO_RECREDIT_SKIP_INVOICE_CTX: _AUTO_RECREDIT_CAPABILITY,
        }).write({'repair_incident_ref': 864})
        self.assertEqual(topup.sudo().repair_incident_ref, 864)

        Move = self.env['account.move'].sudo()
        with self.assertRaises(AccessError):
            Move.with_context(_woow_ecpay_mtn_write=True).create({
                'move_type': 'out_invoice',
                'partner_id': partner.id,
                'invoice_date': fields.Date.today(),
                'woow_ecpay_mtn': 'TOPUP864FORGED0001',
            })
        move = Move.create({
            'move_type': 'out_invoice',
            'partner_id': partner.id,
            'invoice_date': fields.Date.today(),
        })
        with self.assertRaises(AccessError):
            move.with_context(_woow_ecpay_mtn_write=True).write({
                'woow_ecpay_mtn': 'TOPUP864FORGED0001',
            })
        _ecpay_mtn_write(move).write({
            'woow_ecpay_mtn': 'TOPUP864INTERNAL001',
        })
        self.assertEqual(
            move.sudo().woow_ecpay_mtn, 'TOPUP864INTERNAL001')

    def test_config_parameters_are_registered_for_uninstall(self):
        from odoo.addons.woow_paas_platform.hooks import _CONFIG_PARAMETERS
        self.assertIn(
            'woow_paas_platform.auto_recredit_enabled',
            _CONFIG_PARAMETERS,
        )
        self.assertIn(
            'woow_paas_platform.auto_recredit_heartbeat_at',
            _CONFIG_PARAMETERS,
        )
