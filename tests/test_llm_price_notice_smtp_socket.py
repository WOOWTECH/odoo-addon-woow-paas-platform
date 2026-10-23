"""Loopback-only SMTP socket checks; not a delivery receipt or Mailpit acceptance."""
import hashlib
import socket
import smtplib
import threading
import unittest

import test_llm_price_notice_odoo_smtp as fixture


class SingleMessageServer:
    """Own one loopback connection and at most one DATA transaction."""

    def __init__(self, journal, outcome='accept'):
        self.journal = journal
        self.outcome = outcome
        self.commands = []
        self.payload = None
        self.states = []
        self.errors = []
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(5)
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.thread.join(7)
        self.listener.close()
        if self.thread.is_alive():
            raise AssertionError('SMTP fake server did not stop')
        if self.errors:
            raise AssertionError(self.errors)

    def serve(self):
        try:
            with self.listener.accept()[0] as connection:
                connection.settimeout(5)
                with connection.makefile('rb') as reader:
                    connection.sendall(b'220 localhost synthetic\r\n')
                    while True:
                        line = reader.readline()
                        if not line:
                            return
                        self.commands.append(line)
                        verb = line.split(None, 1)[0].upper()
                        self.states.append((verb, self.journal.status,
                                            self.journal.receipts))
                        if verb in (b'EHLO', b'HELO', b'MAIL', b'RSET'):
                            connection.sendall(b'250 localhost\r\n')
                        elif verb == b'RCPT':
                            connection.sendall(b'550 refused\r\n' if self.outcome == 'rcpt'
                                               else b'250 recipient ok\r\n')
                        elif verb == b'DATA':
                            if self.payload is not None:
                                raise AssertionError('second DATA')
                            connection.sendall(b'354 send data\r\n')
                            chunks = []
                            while True:
                                chunk = reader.readline()
                                if not chunk:
                                    raise AssertionError('unterminated DATA')
                                if chunk == b'.\r\n':
                                    break
                                chunks.append(chunk[1:] if chunk.startswith(b'..') else chunk)
                            self.payload = b''.join(chunks)
                            self.states.append((b'BODY', self.journal.status,
                                                self.journal.receipts))
                            if self.outcome == 'disconnect':
                                return
                            connection.sendall(b'554 rejected\r\n' if self.outcome == 'data'
                                               else b'250 accepted\r\n')
                        elif verb == b'QUIT':
                            connection.sendall(b'221 bye\r\n')
                            return
                        else:
                            raise AssertionError(('unexpected SMTP command', line))
        except Exception as error:
            self.errors.append(repr(error))


class TestBoundNoticeSMTPSocket(unittest.TestCase):
    def setUp(self):
        setup = fixture.TestBoundNoticeSMTP()
        setup.setUp()
        self.message = setup.message
        # Exercise SMTP dot transparency while preserving the final MIME digest.
        self.message.set_content('Synthetic only.\n.leading dot\n..two dots\n')
        payload = fixture.serialized(self.message)
        from dataclasses import replace
        attempt = replace(setup.binding.attempt,
                          payload_sha256=hashlib.sha256(payload).hexdigest())
        self.binding = replace(setup.binding, attempt=attempt)
        self.boundary = fixture.Boundary(self.binding)
        self.expected = payload

    def send(self, inner, message=None, recipients=None):
        connection = fixture.OdooShapedConnection(inner)
        bound = fixture.s.BoundNoticeSMTP(connection, boundary=self.boundary,
                                          binding=self.binding)
        return bound.send_message(
            self.message if message is None else message, 'sender@example.test',
            ['recipient@example.test'] if recipients is None else recipients)

    def exercise(self, outcome, exception=None):
        journal = self.boundary._journal
        with SingleMessageServer(journal, outcome) as server:
            with smtplib.SMTP('127.0.0.1', server.port, timeout=5,
                              local_hostname='localhost') as inner:
                if exception is None:
                    self.assertEqual(self.send(inner), {})
                else:
                    with self.assertRaises(exception):
                        self.send(inner)
                self.assertEqual(inner.sendmail.__func__, smtplib.SMTP.sendmail)
                self.assertEqual(inner.rcpt.__func__, smtplib.SMTP.rcpt)
                self.assertEqual(inner.data.__func__, smtplib.SMTP.data)
        rcpts = [line for line in server.commands if line.upper().startswith(b'RCPT ')]
        self.assertEqual(rcpts, [b'rcpt TO:<recipient@example.test>\r\n'])
        for verb, status, receipts in server.states:
            if verb in (b'MAIL', b'RCPT', b'DATA', b'BODY'):
                self.assertEqual((status, receipts), ('unknown', 0))
        self.assertEqual(self.boundary.calls, 1)
        self.assertEqual((journal.status, journal.receipts),
                         ('accepted', 1) if outcome == 'accept' else ('unknown', 0))
        if outcome == 'rcpt':
            self.assertIsNone(server.payload)
            self.assertNotIn(b'data\r\n', server.commands)
        else:
            self.assertEqual(server.commands.count(b'data\r\n'), 1)
            self.assertEqual(server.payload, self.expected)
            self.assertEqual(hashlib.sha256(server.payload).hexdigest(),
                             self.binding.attempt.payload_sha256)

    def test_single_rcpt_and_data_250_accept_exact_final_mime(self):
        self.exercise('accept')

    def test_rcpt_550_stays_unknown_without_data(self):
        self.exercise('rcpt', smtplib.SMTPRecipientsRefused)

    def test_data_554_stays_unknown(self):
        self.exercise('data', smtplib.SMTPDataError)

    def test_disconnect_before_data_reply_stays_unknown(self):
        self.exercise('disconnect', smtplib.SMTPServerDisconnected)

    def test_early_rejections_send_no_smtp_transaction(self):
        cases = ('mime', 'recipients', 'preflight', 'claim')
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                message = fixture.make_message() if case == 'mime' else self.message
                recipients = (['recipient@example.test', 'other@example.test']
                              if case == 'recipients' else None)
                self.boundary.deny = case == 'preflight'
                if case == 'claim':
                    self.boundary._journal.status = 'unknown'
                before = self.boundary._journal.status
                reason = {'mime': 'final_mime_snapshot',
                          'recipients': 'single_bound_envelope',
                          'preflight': 'source_changed',
                          'claim': 'attempt_not_retryable'}[case]
                with SingleMessageServer(self.boundary._journal) as server:
                    with smtplib.SMTP('127.0.0.1', server.port, timeout=5,
                                      local_hostname='localhost') as inner:
                        # Complete greeting before checking application rejection.
                        inner.ehlo()
                        with self.assertRaisesRegex(ValueError, reason):
                            self.send(inner, message, recipients)
                self.assertEqual([line.split(None, 1)[0] for line in server.commands],
                                 [b'ehlo', b'QUIT'])
                self.assertIsNone(server.payload)
                self.assertEqual(self.boundary._journal.status, before)
                self.assertEqual(self.boundary._journal.receipts, 0)


if __name__ == '__main__':
    unittest.main()
