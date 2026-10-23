"""Offline Odoo SMTPConnection-shaped protocol tests; no Odoo or network."""
import copy
from email.generator import BytesGenerator
from email.message import EmailMessage
import hashlib
import importlib
import io
from pathlib import Path
import smtplib
import sys
from types import ModuleType
import unittest
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
package = ModuleType('notice_smtp_services')
package.__path__ = [str(ROOT / 'src/services')]
sys.modules[package.__name__] = package
a = importlib.import_module(package.__name__ + '.llm_price_notice_odoo_adapter')
s = importlib.import_module(package.__name__ + '.llm_price_notice_odoo_smtp')
t = importlib.import_module(package.__name__ + '.llm_price_notice_transport')


def make_message():
    message = EmailMessage()
    message['From'] = 'sender@example.test'
    message['To'] = 'recipient@example.test'
    message['Subject'] = 'Synthetic notice'
    message.set_content('No real recipient or delivery.\n')
    return message


def serialized(message):
    with io.BytesIO() as sink:
        BytesGenerator(sink).flatten(copy.copy(message), linesep='\r\n')
        return sink.getvalue()


class ProtocolSMTP(smtplib.SMTP):
    def __init__(self):
        super().__init__(local_hostname='localhost')
        self.mail_calls = 0
        self.rcpt_code = 250
        self.data_code = 250
        self.data_calls = 0
        self.payload = None

    def ehlo_or_helo_if_needed(self):
        pass

    def mail(self, sender, options=()):
        self.mail_calls += 1
        return 250, b'synthetic'

    def rcpt(self, recipient, options=()):
        return self.rcpt_code, b'synthetic'

    def data(self, payload):
        self.data_calls += 1
        self.payload = payload
        return self.data_code, b'synthetic'

    def _rset(self):
        pass


class OdooShapedConnection:
    """Delegates instance method setters just like live SMTPConnection."""

    def __init__(self, inner):
        self._inner = inner
        self.from_filter = False
        self.smtp_from = False


def _delegate(name):
    return property(lambda self: getattr(self._inner, name),
                    lambda self, value: setattr(self._inner, name, value))


for _name in ('send_message', 'sendmail', 'rcpt', 'data'):
    setattr(OdooShapedConnection, _name, _delegate(_name))


class Journal:
    def __init__(self):
        self.status = 'prepared'
        self.receipts = 0
        self.fail_accept = False

    def _claim(self, attempt_uuid):
        if self.status != 'prepared':
            raise ValueError('attempt_not_retryable')
        self.status = 'unknown'

    def _accept(self, attempt_uuid):
        if self.fail_accept:
            raise RuntimeError('synthetic_commit_failure')
        self.status = 'accepted'
        self.receipts += 1


class Boundary:
    def __init__(self, binding):
        self.binding = binding
        self._journal = Journal()
        self.calls = 0
        self.deny = False

    def preflight(self, binding, *, payload, envelope):
        self.calls += 1
        if self.deny:
            raise ValueError('source_changed')
        if (binding != self.binding or envelope != (binding.attempt.destination,)
                or hashlib.sha256(payload).hexdigest() != binding.attempt.payload_sha256):
            raise ValueError('source_mismatch')


class TestBoundNoticeSMTP(unittest.TestCase):
    def setUp(self):
        self.message = make_message()
        payload = serialized(self.message)
        attempt = t.Attempt(str(uuid4()), 'a' * 64, 'stg', 'paas:synthetic',
                            'recipient@example.test', 1, hashlib.sha256(payload).hexdigest())
        self.binding = a.MailBinding(attempt, 'synthetic_odoo', 41, 17)
        self.boundary = Boundary(self.binding)
        self.inner = ProtocolSMTP()
        self.connection = OdooShapedConnection(self.inner)
        self.session = s.BoundNoticeSMTP(self.connection, boundary=self.boundary,
                                         binding=self.binding)

    def send(self, *, recipients=None, message=None):
        return self.session.send_message(
            message or self.message, 'sender@example.test',
            recipients if recipients is not None else ['recipient@example.test'])

    def test_real_send_message_serialization_single_rcpt_and_data_250(self):
        with self.assertRaises(AttributeError):
            self.session.sendmail
        self.assertEqual(self.send(), {})
        self.assertEqual(self.inner.payload, serialized(self.message))
        self.assertEqual(self.inner.data_calls, 1)
        self.assertEqual(self.boundary.calls, 1)
        self.assertEqual((self.boundary._journal.status, self.boundary._journal.receipts),
                         ('accepted', 1))
        # Instance substitutions were restored on Odoo's delegated object.
        self.assertEqual(self.connection.sendmail.__func__, smtplib.SMTP.sendmail)
        with self.assertRaisesRegex(ValueError, 'single_use'):
            self.send()

    def test_rcpt_refusal_never_reaches_data_or_acceptance(self):
        self.inner.rcpt_code = 550
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            self.send()
        self.assertEqual(self.inner.data_calls, 0)
        self.assertEqual((self.boundary._journal.status, self.boundary._journal.receipts),
                         ('unknown', 0))

    def test_data_refusal_remains_unknown(self):
        self.inner.data_code = 554
        with self.assertRaises(smtplib.SMTPDataError):
            self.send()
        self.assertEqual((self.boundary._journal.status, self.boundary._journal.receipts),
                         ('unknown', 0))

    def test_changed_final_mime_and_extra_recipient_deny_before_smtp(self):
        changed = make_message()
        changed.replace_header('Subject', 'Changed')
        with self.assertRaisesRegex(ValueError, 'final_mime_snapshot'):
            self.send(message=changed)
        self.assertEqual(self.inner.mail_calls, 0)
        self.assertEqual(self.boundary._journal.status, 'prepared')

        fresh = s.BoundNoticeSMTP(self.connection, boundary=self.boundary,
                                  binding=self.binding)
        with self.assertRaisesRegex(ValueError, 'single_bound_envelope'):
            fresh.send_message(self.message, 'sender@example.test',
                               ['recipient@example.test', 'other@example.test'])
        self.assertEqual(self.inner.mail_calls, 0)
        self.assertEqual(self.boundary._journal.status, 'prepared')

    def test_committed_source_preflight_denies_before_claim_or_mail(self):
        self.boundary.deny = True
        with self.assertRaisesRegex(ValueError, 'source_changed'):
            self.send()
        self.assertEqual(self.inner.mail_calls, 0)
        self.assertEqual(self.boundary._journal.status, 'prepared')

    def test_synthetic_success_without_data_is_not_acceptance(self):
        self.connection.send_message = lambda *args: {}
        with self.assertRaisesRegex(ValueError, 'acceptance_not_observed'):
            self.send()
        self.assertEqual(self.boundary.calls, 0)
        self.assertEqual(self.boundary._journal.status, 'prepared')

    def test_data_250_journal_commit_error_never_looks_successful(self):
        self.boundary._journal.fail_accept = True
        with self.assertRaisesRegex(RuntimeError, 'commit_failure'):
            self.send()
        self.assertEqual((self.boundary._journal.status, self.boundary._journal.receipts),
                         ('unknown', 0))
        self.assertEqual(self.connection.data.__func__, ProtocolSMTP.data)


if __name__ == '__main__':
    unittest.main()
