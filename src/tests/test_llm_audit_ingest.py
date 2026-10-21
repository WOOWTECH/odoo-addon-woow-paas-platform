"""Real ORM/page-transaction tests with synthetic audit responses only.

_ingest_in_env exercises the same page savepoint as production; test fixtures
stay in TransactionCase. No independent production tick/commit/HTTP is invoked.
A fresh Environment reload proves columns, not process-local cursor state.
"""
import time
import uuid
from unittest.mock import Mock, patch

from odoo import api
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, tagged

from ..models import llm_audit_ingest as ingest
from ..services import newapi_client as nac
from ..services.llm_audit_page import AuditPageError


def event(seq, *, attempt=None, quota=15625):
    return {'seq': seq, 'attempt_id': attempt or str(uuid.uuid4()), 'kind': 'intent',
            'at': '2026-10-21T01:02:03.123456Z', 'target_user_id': 121201, 'quota': quota}


@tagged('post_install', '-at_install', 'llm_audit_ingest')
class TestLlmAuditIngest(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Model = self.env[ingest.STREAM].sudo()
        self.Evidence = self.env[ingest.EVENT].sudo()
        self.client = Mock()
        self._patch(ingest, 'llm_unconfigured', return_value=False)
        self._patch(ingest, 'config_fingerprint', return_value='f' * 32)
        self.client_factory = self._patch(
            type(self.env['woow_paas_platform.llm_account']), '_llm_client', return_value=self.client)
        for method in ('_llm_incident', '_llm_alert'):
            self._patch(type(self.env['woow_paas_platform.llm_event']), method,
                        side_effect=AssertionError('Audit foundation must not emit incidents or alerts'))
        self.scope = 'owned-' + uuid.uuid4().hex

    def _patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _page(self, events, head, env=None):
        return nac.Ok(data={'env': env or self.scope, 'highwater': head, 'events': events})

    def _run(self, Model=None, scope=None):
        return (Model if Model is not None else self.Model)._ingest_in_env(
            scope or self.scope, time.monotonic() + 25)

    def _stream(self):
        return self.Model.search([('environment', '=', self.scope)])

    def test_fixed_highwater_pages_resume_from_persisted_columns(self):
        self.client.control_audit.return_value = self._page([event(i) for i in range(1, 101)], 101)
        result = self._run()
        self.assertEqual(result, {'after': 100, 'through': 101, 'snapshot_complete': False})
        self.assertEqual(self._stream().covered_seq, 0)
        self.env.flush_all()
        self.env.invalidate_all()
        fresh = api.Environment(self.env.cr, self.env.uid, {})[ingest.STREAM].sudo()
        self.client.control_audit.return_value = self._page([event(101)], 101)
        result = self._run(Model=fresh)
        self.assertEqual(result, {'after': 101, 'through': 101, 'snapshot_complete': True})
        self.assertEqual(self.client.control_audit.call_args.args, (100, 101, 100))
        self.assertIn('deadline', self.client.control_audit.call_args.kwargs)
        self.assertEqual(self._stream().covered_seq, 101)
        self.assertEqual(self.Evidence.search_count([('cursor_id', '=', self._stream().id)]), 101)
        self.client.control_audit.return_value = self._page([event(102)], 102)
        self._run()
        self.assertEqual(self.client.control_audit.call_args.args, (101, None, 100))

    def test_unavailable_disabled_or_invalid_first_page_has_no_durable_progress(self):
        values = (nac.NotExecuted(status=503), nac.Unknown(), self._page([], 1),
                  self._page([event(2)], 1), self._page([event(1)], 1, env='foreign'))
        for response in values:
            self.client.control_audit.return_value = response
            with self.assertRaises((ingest.AuditIngestError, AuditPageError)):
                self._run()
            self.assertFalse(self._stream())
        self.assertFalse(self.Evidence.search([('cursor_id.environment', '=', self.scope)]))

    def test_partial_snapshot_gap_and_head_drift_preserve_prior_page(self):
        self.client.control_audit.return_value = self._page([event(i) for i in range(1, 101)], 102)
        self._run()
        for response in (self._page([event(102)], 102), self._page([event(101), event(102)], 103)):
            self.client.control_audit.return_value = response
            with self.assertRaises(AuditPageError):
                self._run()
            self.assertEqual((self._stream().after_seq, self._stream().through_seq,
                              self._stream().covered_seq), (100, 102, 0))
            self.assertEqual(self.Evidence.search_count([('cursor_id', '=', self._stream().id)]), 100)

    def test_exact_replay_is_idempotent_conflicting_evidence_cannot_be_overwritten(self):
        value = event(1)
        self.client.control_audit.return_value = self._page([value], 1)
        self._run()
        state = self._stream()
        for conflict in (False, True):
            ingest._cap(state).write({'after_seq': 0, 'covered_seq': 0, 'snapshot_open': True})
            self.client.control_audit.return_value = self._page(
                [dict(value, quota=1) if conflict else value], 1)
            if conflict:
                with self.assertRaises(ingest.AuditIngestError):
                    self._run()
                self.assertEqual(state.after_seq, 0)
            else:
                self._run()
            rows = self.Evidence.search([('cursor_id', '=', state.id)])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows.quota, 15625)
            self.assertEqual(rows.at, '2026-10-21T01:02:03.123456Z')

    def test_insert_failure_rolls_back_evidence_and_cursor_together(self):
        self.client.control_audit.return_value = self._page([event(1), event(2)], 2)
        original = type(self.Evidence).create
        count = [0]

        def failing_create(records, values):
            count[0] += 1
            if count[0] == 2:
                raise RuntimeError('Owned second-row failure')
            return original(records, values)

        with patch.object(type(self.Evidence), 'create', failing_create):
            with self.assertRaises(RuntimeError):
                self._run()
        self.assertFalse(self._stream())
        self.assertFalse(self.Evidence.search([('cursor_id.environment', '=', self.scope)]))

    def test_unconfigured_and_expired_ticks_do_not_make_outbound_calls(self):
        with patch.object(ingest, 'llm_unconfigured', return_value=True):
            self.assertIsNone(self.Model._ingest_tick(self.scope))
            self.assertIsNone(self._run())
        with self.assertRaises(ingest.AuditIngestError):
            self.Model._ingest_in_env(self.scope, time.monotonic() - 1)
        self.client_factory.assert_not_called()
        self.assertFalse(self._stream())

    def test_environment_and_endpoint_binding_do_not_auto_reset(self):
        self.client.control_audit.return_value = self._page([event(1)], 1)
        self._run()
        calls = self.client.control_audit.call_count
        with patch.object(ingest, 'config_fingerprint', return_value='e' * 32):
            with self.assertRaises(ingest.AuditIngestError):
                self._run()
        self.assertEqual(self.client.control_audit.call_count, calls)
        other = 'owned-other-' + uuid.uuid4().hex
        self.client.control_audit.return_value = self._page([event(1)], 1, env=other)
        self._run(scope=other)
        self.assertEqual(self.Model.search([('environment', '=', other)]).after_seq, 1)
        self.assertNotEqual(self.Model.search([('environment', '=', other)]).id, self._stream().id)

    def test_rpc_crud_is_protected_and_evidence_has_no_cascade(self):
        with self.assertRaises(AccessError):
            self.Model.create({'environment': self.scope, 'fingerprint': 'x'})
        self.client.control_audit.return_value = self._page([event(1)], 1)
        self._run()
        state = self._stream()
        evidence = self.Evidence.search([('cursor_id', '=', state.id)])
        for records in (state, evidence):
            with self.assertRaises(AccessError):
                records.write({})
            with self.assertRaises(AccessError):
                records.unlink()
        self.assertEqual(self.Evidence._fields['cursor_id'].ondelete, 'restrict')

    def test_bigint_cursor_and_evidence_sequences_stay_exact_after_reload(self):
        start = 2 ** 53 + 1
        # Owned synthetic position; this is a field precision test, not proof
        # that the omitted historical events have been ingested.
        state = ingest._cap(self.Model).create({
            'environment': self.scope, 'fingerprint': 'f' * 32,
            'after_seq': start, 'through_seq': start, 'covered_seq': start})
        self.client.control_audit.return_value = self._page([event(start + 1)], start + 1)
        self._run()
        self.env.flush_all()
        self.env.invalidate_all()
        self.assertEqual(state.after_seq, start + 1)
        self.assertEqual(state.covered_seq, start + 1)
        self.assertEqual(self.Evidence.search([('cursor_id', '=', state.id)]).seq, start + 1)
