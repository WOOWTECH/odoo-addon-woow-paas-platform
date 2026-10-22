"""Offline protocol/transaction slice, not a PostgreSQL or Odoo integration test.

Real smtplib.sendmail runs against fake MAIL/RCPT/DATA replies; no sockets.
SQLite supplies real independent transactions, translating only %s placeholders.
"""
from dataclasses import replace
import hashlib
import importlib.util
from pathlib import Path
import smtplib
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'notice_transport_slice', ROOT / 'src/services/llm_price_notice_transport.py')
t = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = t
spec.loader.exec_module(t)
PAYLOAD = b'From: sender@example.test\r\nTo: recipient@example.test\r\n\r\nSynthetic\r\n'


class CursorAdapter:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, sql, params=()):
        return self.cursor.execute(sql.replace('%s', '?'), params)

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class ConnectionAdapter:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.execute('PRAGMA foreign_keys = ON')

    def cursor(self):
        return CursorAdapter(self.connection.cursor())

    def __getattr__(self, name):
        return getattr(self.connection, name)


class TestTransportJournal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'synthetic.sqlite')
        with sqlite3.connect(self.path) as connection:
            for statement in t.SCHEMA:
                connection.execute(statement)
            connection.execute('CREATE TABLE outer_work (value TEXT)')
        self.journal = t.Journal(lambda: ConnectionAdapter(self.path))
        self.attempt = t.Attempt(
            str(uuid4()), 'a' * 64, 'stg', 'paas:synthetic',
            'recipient@example.test', 1, hashlib.sha256(PAYLOAD).hexdigest())
        self.journal.prepare(self.attempt)
        self.no_socket = patch('socket.create_connection', side_effect=AssertionError('no_network'))
        self.no_socket.start()
        self.addCleanup(self.no_socket.stop)
        # Keep real SMTP.sendmail. Replace only wire protocol operations.
        for method, value in (
            ('ehlo_or_helo_if_needed', None), ('mail', (250, b'ok')),
            ('rcpt', (250, b'ok')), ('data', (250, b'accepted')),
            ('_rset', None), ('close', None),
        ):
            p = patch.object(smtplib.SMTP, method, return_value=value)
            setattr(self, method, p.start())
            self.addCleanup(p.stop)

    def session(self):
        return t.AcceptanceSMTP(journal=self.journal, attempt_uuid=self.attempt.attempt_uuid)

    def send(self, session=None, recipients=None, payload=PAYLOAD):
        session = session or self.session()
        return session.sendmail('sender@example.test',
                                recipients if recipients is not None else [self.attempt.destination],
                                payload)

    def rows(self):
        with sqlite3.connect(self.path) as connection:
            status = connection.execute(
                'SELECT status FROM llm_notice_transport_attempt').fetchone()[0]
            receipts = connection.execute(
                'SELECT attempt_uuid, accepted_at_us, data_code FROM llm_notice_transport_acceptance'
            ).fetchall()
        return status, receipts

    def test_data_250_records_independently_and_reopens(self):
        self.assertEqual(self.send(), {})
        status, receipts = self.rows()
        self.assertEqual(status, 'accepted')
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0][0], self.attempt.attempt_uuid)
        self.assertGreater(receipts[0][1], 0)
        self.assertEqual(receipts[0][2], 250)
        reopened = t.Journal(lambda: ConnectionAdapter(self.path))
        self.assertEqual(reopened.attempt(self.attempt.attempt_uuid), self.attempt)

    def test_rcpt_251_is_also_accepted_only_after_data(self):
        self.rcpt.return_value = (251, b'forwarded')
        self.send()
        self.assertEqual(self.rows()[0], 'accepted')

    def test_rcpt_failure_never_reaches_data_or_receipt(self):
        self.rcpt.return_value = (550, b'refused')
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send()
        self.data.assert_not_called()
        self.assertEqual(self.rows(), ('unknown', []))

    def test_data_failure_has_no_receipt_and_cannot_retry(self):
        self.data.return_value = (554, b'rejected')
        with self.assertRaises(smtplib.SMTPDataError):
            self.send()
        self.assertEqual(self.rows(), ('unknown', []))
        with self.assertRaisesRegex(ValueError, 'not_retryable'):
            self.send()

    def test_disconnect_remains_unknown(self):
        self.data.side_effect = smtplib.SMTPServerDisconnected('lost')
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.send()
        self.assertEqual(self.rows(), ('unknown', []))

    def test_timeout_remains_unknown(self):
        self.data.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.send()
        self.assertEqual(self.rows(), ('unknown', []))

    def test_mail_connection_failure_remains_unknown(self):
        self.mail.side_effect = smtplib.SMTPServerDisconnected('lost before RCPT')
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.send()
        self.assertEqual(self.rows(), ('unknown', []))

    def test_odoo_test_bypass_message_id_and_state_are_not_inputs(self):
        session = self.session()
        # Odoo's current_test path returns before smtp.send_message/sendmail.
        fabricated_mail = {'state': 'sent', 'message_id': '<synthetic@example.test>'}
        self.assertEqual(fabricated_mail['state'], 'sent')
        self.assertEqual(self.rows(), ('prepared', []))
        self.data.assert_not_called()
        # Even bypassing the base transport with a successful return is no proof.
        with patch.object(smtplib.SMTP, 'sendmail', return_value={}):
            self.send(session)
        self.assertEqual(self.rows(), ('unknown', []))

    def test_extra_or_wrong_recipients_rejected_before_smtp(self):
        for recipients in ([], ['other@example.test'],
                           [self.attempt.destination, 'other@example.test'],
                           [self.attempt.destination, self.attempt.destination]):
            with self.subTest(recipients=recipients), self.assertRaisesRegex(ValueError, 'single_bound'):
                self.send(recipients=recipients)
        self.mail.assert_not_called()
        self.assertEqual(self.rows(), ('prepared', []))

    def test_wrong_payload_rejected_before_smtp(self):
        for payload in (PAYLOAD + b'changed', PAYLOAD.decode()):
            with self.assertRaisesRegex(ValueError, 'payload_snapshot'):
                self.send(payload=payload)
        self.mail.assert_not_called()
        self.assertEqual(self.rows(), ('prepared', []))

    def test_unbound_protocol_calls_cannot_create_receipt(self):
        session = self.session()
        with self.assertRaisesRegex(ValueError, 'unbound_data'):
            session.data(PAYLOAD)
        with self.assertRaisesRegex(ValueError, 'unbound_rcpt'):
            session.rcpt(self.attempt.destination)
        self.assertEqual(self.rows(), ('prepared', []))

    def test_duplicate_receipt_preserves_first_observation(self):
        self.send()
        original = self.rows()
        with patch.object(t.time, 'time_ns', return_value=9999999999999999999):
            self.journal._accept(self.attempt.attempt_uuid)
        self.assertEqual(self.rows(), original)
        with self.assertRaisesRegex(ValueError, 'not_retryable'):
            self.send()
        self.data.assert_called_once()

    def test_competing_sessions_only_one_claim_can_send(self):
        first, second = self.session(), self.session()
        self.send(first)
        with self.assertRaisesRegex(ValueError, 'not_retryable'):
            self.send(second)
        self.data.assert_called_once()

    def test_unclaimed_attempt_cannot_be_accepted(self):
        with self.assertRaisesRegex(ValueError, 'not_claimed'):
            self.journal._accept(self.attempt.attempt_uuid)
        self.assertEqual(self.rows(), ('prepared', []))

    def test_prepare_idempotent_but_conflicting_snapshot_denied(self):
        self.journal.prepare(self.attempt)
        with self.assertRaisesRegex(ValueError, 'snapshot_conflict'):
            self.journal.prepare(replace(self.attempt, destination_revision=2))
        self.assertEqual(self.journal.attempt(self.attempt.attempt_uuid), self.attempt)

    def test_outer_rollback_does_not_erase_acceptance(self):
        # SQLite's database-wide write lock cannot model concurrent PG row locks.
        # Begin outer transaction before SMTP, write its unrelated work afterwards.
        outer = sqlite3.connect(self.path)
        self.addCleanup(outer.close)
        outer.execute('BEGIN')
        self.send()
        outer.execute("INSERT INTO outer_work VALUES ('rolled back')")
        outer.rollback()
        self.assertEqual(self.rows()[0], 'accepted')
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute('SELECT * FROM outer_work').fetchall(), [])

    def test_journal_failure_after_data_cannot_look_successful(self):
        real_factory = self.journal._open_connection

        class FailingCommit(ConnectionAdapter):
            def commit(self):
                raise RuntimeError('synthetic_commit_failure')

        def data_accepted(_msg):
            self.journal._open_connection = lambda: FailingCommit(self.path)
            return 250, b'accepted'

        self.data.side_effect = data_accepted
        with self.assertRaisesRegex(RuntimeError, 'commit_failure'):
            self.send()
        self.journal._open_connection = real_factory
        self.assertEqual(self.rows(), ('unknown', []))
        with self.assertRaisesRegex(ValueError, 'not_retryable'):
            self.send()

    def test_missing_uncommitted_attempt_and_constructor_host_denied(self):
        with self.assertRaisesRegex(ValueError, 'not_committed'):
            t.AcceptanceSMTP(journal=self.journal, attempt_uuid=str(uuid4()))
        with self.assertRaisesRegex(ValueError, 'explicit_dispatch'):
            t.AcceptanceSMTP(journal=self.journal, attempt_uuid=self.attempt.attempt_uuid,
                             host='smtp.example.test')

    def test_claim_commit_failure_prevents_smtp(self):
        class FailingCommit(ConnectionAdapter):
            def commit(self):
                raise RuntimeError('claim_commit_failed')

        session = self.session()
        self.journal._open_connection = lambda: FailingCommit(self.path)
        with self.assertRaisesRegex(RuntimeError, 'claim_commit_failed'):
            self.send(session)
        self.mail.assert_not_called()
        self.assertEqual(self.rows(), ('prepared', []))

    def test_construction_is_inert_for_both_transport_classes(self):
        for cls in (t.AcceptanceSMTP, t.AcceptanceSMTPSSL):
            session = cls(journal=self.journal, attempt_uuid=self.attempt.attempt_uuid)
            self.assertIsNone(session.sock)
        self.mail.assert_not_called()
        self.data.assert_not_called()
        self.assertEqual(self.rows(), ('prepared', []))

    def test_invalid_attempt_inputs(self):
        for changes in ({'destination': 'A <a@example.test>'}, {'destination': 'a@b\r\nBcc:x@y'},
                        {'destination_revision': True}, {'payload_sha256': 'forged'},
                        {'environment': 'other'}, {'attempt_uuid': 'not-a-uuid'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.attempt, **changes)


if __name__ == '__main__':
    unittest.main()
