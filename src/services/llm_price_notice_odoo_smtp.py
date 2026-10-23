"""Dormant, single-message adapter for Odoo's SMTPConnection wrapper.

This is not imported by the addon and has no product dispatcher or approval
policy. A caller holding a real session could send, so it must be introduced
only after a trusted, gated dispatcher owns a fresh dedicated
ir.mail_server.connect() session, holds the source writer interlock, and closes
that session after one message.
"""
import hashlib

from .llm_price_notice_odoo_adapter import MailBinding


class BoundNoticeSMTP:
    """Observe a single Odoo send_message at its real RCPT and DATA boundary.

    Odoo's SMTPConnection delegates methods to an internal smtplib object. The
    instance-only method substitutions below therefore reach smtplib.sendmail's
    calls to self.rcpt/self.data without changing any global or shared session.
    """

    def __init__(self, session, *, boundary, binding):
        if type(binding) is not MailBinding:
            raise ValueError('invalid_notice_mail_binding')
        self._session = session
        self._boundary = boundary
        self._binding = binding
        self._used = False

    def __getattr__(self, name):
        # Odoo _prepare_email_message reads these values; the caller owns QUIT.
        # Do not expose raw sendmail/rcpt/data through this wrapper.
        if name not in ('from_filter', 'smtp_from', 'quit'):
            raise AttributeError(name)
        return getattr(self._session, name)

    def send_message(self, message, from_addr=None, to_addrs=None,
                     mail_options=(), rcpt_options=()):
        if self._used:
            raise ValueError('notice_session_single_use')
        self._used = True
        session = self._session
        binding = self._binding
        attempt = binding.attempt
        journal = self._boundary._journal
        originals = {name: getattr(session, name) for name in ('sendmail', 'rcpt', 'data')}
        in_sendmail = False
        rcpt_seen = False
        rcpt_accepted = False
        data_seen = False
        accepted = False

        def sendmail(from_address, recipients, payload, options=(), recipient_options=()):
            nonlocal in_sendmail, rcpt_seen, rcpt_accepted, data_seen
            if in_sendmail or type(recipients) not in (list, tuple) or tuple(recipients) != (attempt.destination,):
                raise ValueError('single_bound_envelope_required')
            if (type(payload) is not bytes
                    or hashlib.sha256(payload).hexdigest() != attempt.payload_sha256):
                raise ValueError('final_mime_snapshot_mismatch')
            # Read independently committed mail, obligation and binding before
            # the irreversible claim or any SMTP MAIL/RCPT/DATA command.
            self._boundary.preflight(binding, payload=payload, envelope=(attempt.destination,))
            journal._claim(attempt.attempt_uuid)
            in_sendmail = True
            rcpt_seen = rcpt_accepted = data_seen = False
            try:
                return originals['sendmail'](
                    from_address, recipients, payload, options, recipient_options)
            finally:
                in_sendmail = False
                rcpt_accepted = False

        def rcpt(recipient, options=()):
            nonlocal rcpt_seen, rcpt_accepted
            if not in_sendmail or rcpt_seen or recipient != attempt.destination:
                raise ValueError('unbound_notice_rcpt')
            rcpt_seen = True
            code, response = originals['rcpt'](recipient, options)
            rcpt_accepted = code in (250, 251)
            return code, response

        def data(payload):
            nonlocal data_seen, accepted
            if (not in_sendmail or not rcpt_accepted or data_seen
                    or type(payload) is not bytes
                    or hashlib.sha256(payload).hexdigest() != attempt.payload_sha256):
                raise ValueError('unbound_notice_data')
            data_seen = True
            code, response = originals['data'](payload)
            if code == 250:
                # Independent commit before Odoo state/postprocessing/QUIT.
                journal._accept(attempt.attempt_uuid)
                accepted = True
            return code, response

        replacements = {'sendmail': sendmail, 'rcpt': rcpt, 'data': data}
        installed = []
        try:
            for name, replacement in replacements.items():
                setattr(session, name, replacement)
                installed.append(name)
            result = session.send_message(
                message, from_addr, to_addrs, mail_options, rcpt_options)
            if not accepted:
                raise ValueError('smtp_acceptance_not_observed')
            return result
        finally:
            for name in reversed(installed):
                setattr(session, name, originals[name])
