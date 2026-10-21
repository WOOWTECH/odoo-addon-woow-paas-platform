"""Real ORM persistence and PostgreSQL aggregate tests for dormant I2.

Ledger input uses transaction-local temporary tables: it exercises PostgreSQL's
SUM FILTER/text conversion without manufacturing invalid real financial rows.
Candidates/accounts use real ORM tables and ordinary TransactionCase rollback.
No upstream client, cron, incident or shared cluster operations are involved.
"""
from unittest.mock import patch

from odoo import api
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, tagged

from ..models import llm_reconcile_candidate as candidate
from ..models.llm_common import _llm_internal
from ..services.llm_reconcile_balance import BalanceSample


@tagged('post_install', '-at_install', 'llm_reconcile_candidate')
class TestLlmReconcileCandidate(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Model = self.env[candidate.MODEL].sudo()
        partner = self.env['res.partner'].create({'name': 'Owned I2 foundation fixture'})
        self.account = _llm_internal(self.env['woow_paas_platform.llm_account']).create({
            'partner_id': partner.id})
        _llm_internal(self.account).write({'state': 'active', 'newapi_user_id': 121222})
        self.env.flush_all()
        # These names intentionally shadow only the ledger reads in this
        # transaction. No real order/discrepancy rows or constraints are changed.
        self.env.cr.execute('''CREATE TEMP TABLE woow_paas_platform_llm_credit_order
            (account_id integer, source text, state text, quota bigint) ON COMMIT DROP''')
        self.env.cr.execute('''CREATE TEMP TABLE woow_paas_platform_llm_known_discrepancy
            (account_id integer, quota_delta bigint) ON COMMIT DROP''')
        patcher = patch.object(candidate, 'llm_unconfigured', return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Any accidental outward effect fails the test immediately.
        for model, name in (
                ('woow_paas_platform.llm_account', '_llm_client'),
                ('woow_paas_platform.llm_event', '_llm_incident'),
                ('woow_paas_platform.llm_event', '_llm_alert')):
            effect = patch.object(type(self.env[model]), name,
                                  side_effect=AssertionError('I2 foundation external effect'))
            effect.start()
            self.addCleanup(effect.stop)

    def _order(self, quota, state='credited', source='user', account_id=None):
        self.env.cr.execute('''INSERT INTO woow_paas_platform_llm_credit_order
            (account_id, source, state, quota) VALUES (%s, %s, %s, %s)''',
                            (account_id or self.account.id, source, state, quota))

    def _sample(self, total, at, count=10, Model=None):
        with patch.object(candidate.time, 'time', return_value=at):
            return (Model if Model is not None else self.Model)._observe(
                self.account.id, BalanceSample(total, 0, count, at))

    def test_sql_source_state_and_signed_discrepancy_are_exact(self):
        big = 2 ** 53 + 1
        for source in ('user', 'manual', 'dr', 'recharge'):
            for state in ('credited', 'sending', 'unknown', 'needs_review',
                          'credit_lost', 'paid', 'voided', 'void_failed', 'cancelled'):
                self._order(big, state, source)
        self._order(big, 'credited', 'not_a_valid_source')
        self._order(big, account_id=self.account.id + 1)
        for value in (big, big, -7):
            self.env.cr.execute('''INSERT INTO woow_paas_platform_llm_known_discrepancy
                (account_id, quota_delta) VALUES (%s, %s)''', (self.account.id, value))
        self.assertEqual(self.Model._ledger_bounds(self.account.id), (4 * big, 16 * big, 2 * big - 7))
        # PostgreSQL SUM(bigint) exceeds bigint itself and must remain exact.
        self._order(2 ** 63 - 1)
        self._order(2 ** 63 - 1)
        lower, upper, known = self.Model._ledger_bounds(self.account.id)
        self.assertEqual(lower, 4 * big + 2 * (2 ** 63 - 1))
        self.assertEqual(upper, 16 * big + 2 * (2 ** 63 - 1))
        self.assertEqual(known, 2 * big - 7)

    def test_database_reload_preserves_five_minute_baseline(self):
        big = 2 ** 53 + 1
        self.assertEqual(self._sample(big, 100).outcome, 'candidate_up')
        self.env.flush_all()
        self.env.invalidate_all()
        # Fresh Environment/recordsets over the same transaction prove state
        # comes from stored columns, not a process-local Candidate object.
        fresh = api.Environment(self.env.cr, self.env.uid, {})[candidate.MODEL].sudo()
        self.assertEqual(self._sample(big, 399, Model=fresh).outcome, 'candidate_up')
        decision = self._sample(big, 400, Model=fresh)
        self.assertEqual((decision.outcome, decision.severity), ('evidence_pending', 'none'))
        row = fresh.search([('account_id', '=', self.account.id)])
        self.assertEqual((row.first_quota, row.first_sampled_at), (big, 100))

    def test_changed_ledger_and_discrepancy_clear_candidate(self):
        self._sample(100_000, 100)
        self._order(100_000)
        self.assertEqual(self._sample(100_000, 400).outcome, 'clear')
        self.env.cr.execute('DELETE FROM woow_paas_platform_llm_credit_order')
        self.env.cr.execute('''INSERT INTO woow_paas_platform_llm_known_discrepancy
            VALUES (%s, %s)''', (self.account.id, 100_000))
        self.assertEqual(self._sample(100_000, 500).outcome, 'clear')

    def test_downward_recheck_restarts_with_traffic(self):
        self._order(100_000)
        self.assertEqual(self._sample(0, 100).outcome, 'candidate_down')
        self.assertEqual(self._sample(0, 400, count=11).outcome, 'candidate_down')
        self.assertEqual(self._sample(0, 700, count=11).outcome, 'mismatch')
        row = self.Model.search([('account_id', '=', self.account.id)])
        self.assertEqual(row.first_sampled_at, 400)

    def test_stale_sample_closed_account_and_unconfigured_are_explicit(self):
        self._sample(100_000, 100)
        with self.assertRaises(ValidationError):
            self._sample(100_000, 100)
        with patch.object(candidate.time, 'time', return_value=500):
            with self.assertRaises(ValidationError):
                self.Model._observe(self.account.id, BalanceSample(100_000, 0, 10, 400))
        _llm_internal(self.account).write({'state': 'closed'})
        self.assertIsNone(self._sample(100_000, 600))
        self.assertFalse(self.Model.search([('account_id', '=', self.account.id)]))
        evidence = self.env[candidate.ARCHIVE_MODEL].sudo().search([('account_ref', '=', self.account.id)])
        self.assertEqual(len(evidence), 1)
        self.assertEqual((evidence.reason, evidence.first_sampled_at), ('closed', 100))
        with patch.object(candidate, 'llm_unconfigured', return_value=True):
            self.assertIsNone(self.Model._observe(self.account.id, None))
        self.assertFalse(self.Model.search([('account_id', '=', self.account.id)]))

    def test_raw_crud_requires_unforgeable_capability(self):
        with self.assertRaises(AccessError):
            self.Model.create({'account_id': self.account.id,
                               'last_sampled_at': 100, 'outcome': 'clear'})
        self._sample(100_000, 100)
        row = self.Model.search([('account_id', '=', self.account.id)])
        with self.assertRaises(AccessError):
            row.write({'last_sampled_at': 200})
        with self.assertRaises(AccessError):
            row.unlink()

    def test_closed_pending_evidence_is_immutable_and_does_not_sample(self):
        big = 2 ** 53 + 1
        self._sample(big, 100)
        self._sample(big, 400)
        _llm_internal(self.account).write({'state': 'closed'})
        with patch.object(type(self.Model), '_ledger_bounds', side_effect=AssertionError('closed sampled')):
            self.assertIsNone(self.Model._observe(self.account.id, None))
            self.assertIsNone(self.Model._observe(self.account.id, None))
        rows = self.env[candidate.ARCHIVE_MODEL].sudo().search([('account_ref', '=', self.account.id)])
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows.first_quota, rows.first_sampled_at, rows.last_sampled_at), (big, 100, 400))
        self.assertEqual((rows.outcome, rows.i3_coverage), ('evidence_pending', 'unavailable'))
        self.assertEqual(rows.newapi_user_id, 121222)
        for records in (rows, candidate._cap(rows)):
            with self.assertRaises(AccessError):
                records.write({'outcome': 'clear'})
            with self.assertRaises(AccessError):
                records.unlink()
        with self.assertRaises(AccessError):
            self.env[candidate.ARCHIVE_MODEL].sudo().create({
                'account_ref': self.account.id, 'source_candidate_id': 99999,
                'reason': 'closed', 'outcome': 'clear', 'last_sampled_at': 100})

    def test_identity_change_archives_and_starts_new_baseline(self):
        self._sample(100_000, 100)
        self._sample(100_000, 400)
        # Defensive fixture corruption only: product identity is immutable.
        self.env.flush_all()
        self.env.cr.execute('UPDATE woow_paas_platform_llm_account SET newapi_user_id=%s WHERE id=%s',
                            (121223, self.account.id))
        self.account.invalidate_recordset()
        self.assertEqual(self._sample(100_000, 500).outcome, 'candidate_up')
        state = self.Model.search([('account_id', '=', self.account.id)])
        self.assertEqual((state.newapi_user_id, state.first_sampled_at), (121223, 500))
        evidence = self.env[candidate.ARCHIVE_MODEL].sudo().search([('account_ref', '=', self.account.id)])
        self.assertEqual((evidence.reason, evidence.newapi_user_id, evidence.first_sampled_at),
                         ('identity_changed', 121222, 100))

    def test_observed_closure_then_same_identity_reopen_does_not_reuse(self):
        self._sample(100_000, 100)
        _llm_internal(self.account).write({'state': 'closed'})
        self.Model._observe(self.account.id, None)
        # Owned fixture only; this is not a public reopen API or lifecycle change.
        _llm_internal(self.account).write({'state': 'active'})
        self.assertEqual(self._sample(100_000, 500).outcome, 'candidate_up')
        self.assertEqual(self.Model.search([('account_id', '=', self.account.id)]).first_sampled_at, 500)

    def test_legacy_identity_and_unknown_bounds_are_preserved_as_unknown(self):
        self._sample(100_000, 100)
        state = self.Model.search([('account_id', '=', self.account.id)])
        candidate._cap(state).write({'newapi_user_id': 0, 'lower_text': False, 'upper_text': False,
                                    'known_text': False})
        self.assertEqual(self._sample(100_000, 500).outcome, 'candidate_up')
        evidence = self.env[candidate.ARCHIVE_MODEL].sudo().search([('account_ref', '=', self.account.id)])
        self.assertEqual(evidence.reason, 'identity_unbound')
        self.assertFalse(evidence.newapi_user_id)
        self.assertFalse(evidence.lower_text)
        self.assertFalse(evidence.known_text)

    def test_archive_failure_retains_operational_candidate(self):
        self._sample(100_000, 100)
        _llm_internal(self.account).write({'state': 'closed'})
        Evidence = self.env[candidate.ARCHIVE_MODEL].sudo()
        with patch.object(type(Evidence), 'create', side_effect=RuntimeError('owned archive failure')):
            with self.assertRaises(RuntimeError):
                self.Model._observe(self.account.id, None)
        self.assertEqual(self.Model.search([('account_id', '=', self.account.id)]).first_sampled_at, 100)
        self.assertFalse(Evidence.search([('account_ref', '=', self.account.id)]))

    def test_retirement_failure_rolls_back_archive_and_preserves_candidate(self):
        self._sample(100_000, 100)
        _llm_internal(self.account).write({'state': 'closed'})
        with patch.object(type(self.Model), 'unlink', side_effect=RuntimeError('owned retire failure')):
            with self.assertRaises(RuntimeError):
                self.Model._observe(self.account.id, None)
        self.assertEqual(self.Model.search([('account_id', '=', self.account.id)]).first_sampled_at, 100)
        self.assertFalse(self.env[candidate.ARCHIVE_MODEL].sudo().search([('account_ref', '=', self.account.id)]))

    def test_exact_aggregate_text_and_account_deletion_fk_do_not_cascade(self):
        big = 2 ** 64 + 1
        with patch.object(type(self.Model), '_ledger_bounds', return_value=(big, big + 1, -big)):
            self._sample(0, 100)
        state = self.Model.search([('account_id', '=', self.account.id)])
        for model in (self.Model, self.env[candidate.ARCHIVE_MODEL]):
            self.assertEqual(model._fields['account_id'].ondelete, 'set null')
            self.env.cr.execute("""SELECT c.confdeltype FROM pg_constraint c
                JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=ANY(c.conkey)
                WHERE c.conrelid=%s::regclass AND c.contype='f' AND a.attname='account_id'""",
                                (model._table,))
            self.assertEqual(self.env.cr.fetchone()[0], 'n')
        _llm_internal(self.account).write({'state': 'closed'})
        self.Model._observe(self.account.id, None)
        evidence = self.env[candidate.ARCHIVE_MODEL].sudo().search([('account_ref', '=', self.account.id)])
        self.assertEqual((evidence.lower_text, evidence.upper_text, evidence.known_text),
                         (str(big), str(big + 1), str(-big)))
        self.assertFalse(state.exists())

    def test_new_account_does_not_inherit_unobserved_closed_history(self):
        self._sample(100_000, 100)
        old_id = self.account.id
        partner_id = self.account.partner_id.id
        _llm_internal(self.account).write({'state': 'closed'})
        # Supported lifecycle: reopen creates another local/remote identity.
        # There was no I2 observation while the historical account was closed.
        new = _llm_internal(self.env['woow_paas_platform.llm_account']).create({'partner_id': partner_id})
        _llm_internal(new).write({'state': 'active', 'newapi_user_id': 121224})
        with patch.object(candidate.time, 'time', return_value=500):
            result = self.Model._observe(new.id, BalanceSample(100_000, 0, 10, 500))
        self.assertEqual(result.outcome, 'candidate_up')
        self.assertEqual(self.Model.search([('account_id', '=', new.id)]).first_sampled_at, 500)
        self.assertEqual(self.Model.search([('account_id', '=', old_id)]).first_sampled_at, 100)
        self.assertIsNone(self.Model._observe(old_id, None))
        self.assertEqual(self.env[candidate.ARCHIVE_MODEL].sudo().search(
            [('account_ref', '=', old_id)]).first_sampled_at, 100)

    def test_upgrade_backfill_preserves_legacy_sample_without_inventing_identity(self):
        self._sample(100_000, 100)
        state = self.Model.search([('account_id', '=', self.account.id)])
        candidate._cap(state).write({'account_ref': 0, 'newapi_user_id': 0,
                                    'lower_text': False, 'upper_text': False, 'known_text': False})
        self.Model.flush_model()
        self.Model.init()
        state.invalidate_recordset()
        self.assertEqual(state.account_ref, self.account.id)
        self.assertEqual((state.first_quota, state.first_sampled_at), (100_000, 100))
        self.assertFalse(state.newapi_user_id)
        self.assertFalse(state.lower_text)
