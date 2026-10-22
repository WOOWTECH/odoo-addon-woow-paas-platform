"""Dormant transport journal building block; deliberately absent from __init__.

No Odoo hook, RPC, queue, migration, connection factory or dispatch is installed.
Only trusted future dispatch code may construct these objects. Python objects
are not authority tokens. SMTP acceptance is not delivery/publication evidence.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import re
import smtplib
import time
from uuid import UUID


# Reviewable proposed PostgreSQL schema, NOT executed on import or construction.
# No FK to mail.mail or uncommitted Odoo records: independent commits must not
# wait for locks held by the outer sending transaction.
SCHEMA = (
    """CREATE TABLE llm_notice_transport_attempt (
        attempt_uuid TEXT PRIMARY KEY,
        campaign_fingerprint TEXT NOT NULL,
        environment TEXT NOT NULL CHECK (environment IN ('stg', 'prod')),
        recipient_key TEXT NOT NULL,
        destination TEXT NOT NULL,
        destination_revision INTEGER NOT NULL CHECK (destination_revision > 0),
        payload_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('prepared', 'unknown', 'accepted'))
    )""",
    """CREATE TABLE llm_notice_transport_acceptance (
        attempt_uuid TEXT PRIMARY KEY REFERENCES llm_notice_transport_attempt(attempt_uuid),
        accepted_at_us BIGINT NOT NULL CHECK (accepted_at_us > 0),
        data_code INTEGER NOT NULL CHECK (data_code = 250)
    )""",
)


@dataclass(frozen=True)
class Attempt:
    attempt_uuid: str
    campaign_fingerprint: str
    environment: str
    recipient_key: str
    destination: str
    destination_revision: int
    payload_sha256: str

    def __post_init__(self):
        if (not isinstance(self.attempt_uuid, str)
                or str(UUID(self.attempt_uuid)) != self.attempt_uuid
                or self.environment not in ('stg', 'prod')
                or not isinstance(self.recipient_key, str)
                or not re.fullmatch(r'(?:paas|legacy):[A-Za-z0-9_-]{1,64}', self.recipient_key)
                or type(self.destination_revision) is not int
                or not 0 < self.destination_revision <= 2147483647
                or not isinstance(self.destination, str)
                or not re.fullmatch(r'[A-Za-z0-9_.+%-]+@[A-Za-z0-9.-]+', self.destination)):
            raise ValueError('invalid_transport_attempt')
        for digest in (self.campaign_fingerprint, self.payload_sha256):
            if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                raise ValueError('invalid_transport_digest')


class Journal:
    """DB-API journal; factory MUST open a fresh dedicated PG connection per call.

    Never pass an Odoo cursor/connection or a factory returning the outer one.
    Schema provisioning, DB roles and migration approval are intentionally absent.
    No connection or schema access occurs until an explicit method call.
    """

    def __init__(self, open_connection):
        self._open_connection = open_connection

    @contextmanager
    def _transaction(self):
        connection = self._open_connection()
        try:
            cursor = connection.cursor()
            try:
                yield cursor
                connection.commit()
            finally:
                cursor.close()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def prepare(self, attempt):
        """Commit a frozen snapshot; same UUID with different content is denied."""
        if type(attempt) is not Attempt:
            raise ValueError('invalid_transport_attempt')
        values = tuple(getattr(attempt, field) for field in Attempt.__dataclass_fields__)
        with self._transaction() as cursor:
            cursor.execute(
                """INSERT INTO llm_notice_transport_attempt
                   (attempt_uuid, campaign_fingerprint, environment, recipient_key,
                    destination, destination_revision, payload_sha256, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'prepared')
                   ON CONFLICT (attempt_uuid) DO NOTHING""", values)
            stored, _status = self._load(cursor, attempt.attempt_uuid)
            if stored != attempt:
                raise ValueError('attempt_snapshot_conflict')

    @staticmethod
    def _load(cursor, attempt_uuid):
        cursor.execute(
            """SELECT attempt_uuid, campaign_fingerprint, environment, recipient_key,
                      destination, destination_revision, payload_sha256, status
               FROM llm_notice_transport_attempt WHERE attempt_uuid = %s""", (attempt_uuid,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError('attempt_not_committed')
        return Attempt(*row[:-1]), row[-1]

    def attempt(self, attempt_uuid):
        with self._transaction() as cursor:
            return self._load(cursor, attempt_uuid)[0]

    def _claim(self, attempt_uuid):
        # CAS commits before any SMTP command. Crash/failure remains unknown.
        with self._transaction() as cursor:
            cursor.execute(
                """UPDATE llm_notice_transport_attempt SET status = 'unknown'
                   WHERE attempt_uuid = %s AND status = 'prepared'""", (attempt_uuid,))
            if cursor.rowcount != 1:
                raise ValueError('attempt_not_retryable')

    def _accept(self, attempt_uuid):
        """Internal transport-only entrypoint, not an RPC/imported-proof API."""
        observed_at_us = time.time_ns() // 1000
        with self._transaction() as cursor:
            _attempt, status = self._load(cursor, attempt_uuid)
            if status not in ('unknown', 'accepted'):
                raise ValueError('attempt_not_claimed')
            cursor.execute(
                """INSERT INTO llm_notice_transport_acceptance
                   (attempt_uuid, accepted_at_us, data_code) VALUES (%s, %s, 250)
                   ON CONFLICT (attempt_uuid) DO NOTHING""",
                (attempt_uuid, observed_at_us))
            cursor.execute(
                "UPDATE llm_notice_transport_attempt SET status = 'accepted' WHERE attempt_uuid = %s",
                (attempt_uuid,))


class _AcceptanceMixin:
    """Observe smtplib's real protocol path, below Odoo's test-mode bypass.

    One committed attempt per session. No second send, retry, or certificate/TLS
    policy is supplied here. Future dispatch must configure its normal secure
    connection and must use this session only for the bound attempt.
    """

    def __init__(self, *, journal, attempt_uuid, **kwargs):
        # host is intentionally not accepted: construction cannot connect/send.
        if kwargs.get('host'):
            raise ValueError('connect_requires_explicit_dispatch')
        self._journal = journal
        self._attempt = journal.attempt(attempt_uuid)
        self._claimed = False
        self._rcpt_accepted = False
        self._in_sendmail = False
        kwargs.setdefault('local_hostname', 'localhost')  # no constructor DNS lookup
        super().__init__(**kwargs)

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        if isinstance(to_addrs, str):
            to_addrs = [to_addrs]
        else:
            to_addrs = list(to_addrs)
        if to_addrs != [self._attempt.destination]:
            raise ValueError('single_bound_envelope_required')
        if (not isinstance(msg, bytes)
                or hashlib.sha256(msg).hexdigest() != self._attempt.payload_sha256):
            raise ValueError('payload_snapshot_mismatch')
        if self._claimed:
            raise ValueError('attempt_not_retryable')
        self._journal._claim(self._attempt.attempt_uuid)
        self._claimed = True
        self._in_sendmail = True
        try:
            return super().sendmail(from_addr, to_addrs, msg, mail_options, rcpt_options)
        finally:
            self._in_sendmail = False
            self._rcpt_accepted = False

    def rcpt(self, recip, options=()):
        if not self._in_sendmail or recip != self._attempt.destination:
            raise ValueError('unbound_rcpt')
        code, response = super().rcpt(recip, options)
        self._rcpt_accepted = code in (250, 251)
        return code, response

    def data(self, msg):
        if (not self._in_sendmail or not self._rcpt_accepted
                or not isinstance(msg, bytes)
                or hashlib.sha256(msg).hexdigest() != self._attempt.payload_sha256):
            raise ValueError('unbound_data')
        code, response = super().data(msg)
        if code == 250:
            # Synchronous independent commit, before Odoo postprocessing/QUIT.
            # Failure propagates; no success is invented and retry stays blocked.
            self._journal._accept(self._attempt.attempt_uuid)
        return code, response


class AcceptanceSMTP(_AcceptanceMixin, smtplib.SMTP):
    """Dormant explicit-session building block for SMTP/STARTTLS dispatch."""


class AcceptanceSMTPSSL(_AcceptanceMixin, smtplib.SMTP_SSL):
    """Dormant explicit-session building block for implicit TLS dispatch."""
