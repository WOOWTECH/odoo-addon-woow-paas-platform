"""Unregistered Odoo 18/PG boundary. No hook, dispatch or acceptance writer.

Only future trusted server code may supply registry, environment construction,
runtime environment and the Odoo current_test probe. None are RPC/context input.
A successful preflight is a snapshot check, not permission to publish/send.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import re

from .llm_price_notice_transport import Attempt, Journal, _matches_mime_snapshot


BINDING_SCHEMA = """CREATE TABLE llm_notice_odoo_mail_binding (
    attempt_uuid TEXT PRIMARY KEY REFERENCES llm_notice_transport_attempt(attempt_uuid),
    database_name TEXT NOT NULL,
    mail_id INTEGER NOT NULL CHECK (mail_id > 0),
    obligation_id INTEGER NOT NULL CHECK (obligation_id > 0),
    UNIQUE (database_name, mail_id),
    UNIQUE (database_name, obligation_id)
)"""  # Proposed migration only; never executed here. No FK to auto-deleted mail.


@dataclass(frozen=True)
class MailBinding:
    """One committed mail row bound to one immutable synthetic outbox row."""

    attempt: Attempt
    database_name: str
    mail_id: int
    obligation_id: int

    def __post_init__(self):
        if (type(self.attempt) is not Attempt
                or not isinstance(self.database_name, str) or not self.database_name
                or type(self.mail_id) is not int or self.mail_id <= 0
                or type(self.obligation_id) is not int or self.obligation_id <= 0):
            raise ValueError('invalid_mail_binding')


class OdooJournal(Journal):
    """Adds immutable mail identity to a previously committed attempt."""

    def bind(self, binding):
        if type(binding) is not MailBinding:
            raise ValueError('invalid_mail_binding')
        with self._transaction() as cursor:
            stored, status = self._load(cursor, binding.attempt.attempt_uuid)
            if stored != binding.attempt or status != 'prepared':
                raise ValueError('attempt_not_ready_or_mismatched')
            cursor.execute(
                """INSERT INTO llm_notice_odoo_mail_binding
                   (attempt_uuid, database_name, mail_id, obligation_id)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (attempt_uuid) DO NOTHING""",
                (stored.attempt_uuid, binding.database_name, binding.mail_id, binding.obligation_id))
            if self._binding(cursor, stored) != binding:
                raise ValueError('mail_binding_conflict')

    @staticmethod
    def _binding(cursor, attempt):
        cursor.execute(
            """SELECT database_name, mail_id, obligation_id
               FROM llm_notice_odoo_mail_binding WHERE attempt_uuid = %s""",
            (attempt.attempt_uuid,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError('mail_binding_not_committed')
        return MailBinding(attempt, *row)

    def ready_binding(self, attempt_uuid):
        with self._transaction() as cursor:
            attempt, status = self._load(cursor, attempt_uuid)
            if status != 'prepared':
                raise ValueError('attempt_not_ready')
            return self._binding(cursor, attempt)


class _CursorView:
    """Journal closes its cursor view before commit; keep Odoo cursor alive."""

    def __init__(self, cursor):
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def close(self):
        pass


class _CursorConnection:
    def __init__(self, cursor):
        self.raw_cursor = cursor

    def cursor(self):
        return _CursorView(self.raw_cursor)

    def commit(self):
        self.raw_cursor.commit()

    def rollback(self):
        self.raw_cursor.rollback()

    def close(self):
        self.raw_cursor.close()


class RegistryConnectionFactory:
    """Explicit factory for Journal; constructor has zero database effects.

    Checks actual PG backend/database, not just Python cursor identity. Rejects
    Odoo test cursors sharing the caller transaction. Never commits/closes outer.
    registry.cursor() ownership and SQL execution are trusted server dependencies.
    """

    def __init__(self, registry, outer_cursor):
        self._registry = registry
        self._outer = outer_cursor

    def __call__(self):
        self._outer.execute('SELECT current_database(), pg_backend_pid()')
        outer_database, outer_pid = self._outer.fetchone()
        cursor = self._registry.cursor()
        if cursor is self._outer:
            raise ValueError('independent_cursor_required')
        independent_backend = False
        try:
            cursor.execute('SELECT current_database(), pg_backend_pid()')
            database, pid = cursor.fetchone()
            independent_backend = (type(pid) is int and type(outer_pid) is int
                                   and pid > 0 and outer_pid > 0 and pid != outer_pid)
            if (not isinstance(outer_database, str) or not outer_database
                    or database != outer_database or cursor.dbname != database
                    or self._outer.dbname != database
                    or type(pid) is not int or type(outer_pid) is not int
                    or pid <= 0 or outer_pid <= 0 or pid == outer_pid):
                raise ValueError('independent_same_database_backend_required')
            return _CursorConnection(cursor)
        except BaseException:
            # Only a proven independent backend is ours to close. If the probe
            # fails or returns invalid PIDs, ownership is unknown: leave disposal
            # to the registry owner, as close could affect the caller transaction.
            if independent_backend:
                cursor.close()
            raise

    @contextmanager
    def committed_environment(self, environment_on_cursor):
        connection = self()
        try:
            env = environment_on_cursor(connection.raw_cursor)
            if env.cr is not connection.raw_cursor:
                raise ValueError('fresh_environment_cursor_required')
            yield env
        finally:
            # Odoo Cursor.close rolls back read transaction and releases pool slot.
            connection.close()


class OdooMailBoundary:
    """Validates committed ORM rows and transport-ready bytes; never sends.

    open_committed_environment must use RegistryConnectionFactory above, with a
    fresh api.Environment(cr, authorised_service_uid, clean_context). No outer
    recordset/cache, caller-supplied SQL or request context may be reused.
    test_mode must read actual Odoo modules.module.current_test (convert to bool).
    """

    def __init__(self, journal, open_committed_environment, *, runtime_environment, test_mode):
        self._journal = journal
        self._open_environment = open_committed_environment
        self._environment = runtime_environment
        self._test_mode = test_mode

    def _check_source(self, binding, payload, envelope):
        if self._test_mode() is not False:
            raise ValueError('odoo_test_bypass_forbidden')
        if type(binding) is not MailBinding or binding.attempt.environment != self._environment:
            raise ValueError('runtime_environment_mismatch')
        attempt = binding.attempt
        if type(envelope) is not tuple or envelope != (attempt.destination,):
            raise ValueError('single_final_envelope_required')
        if not _matches_mime_snapshot(payload, attempt.payload_sha256):
            raise ValueError('final_mime_snapshot_mismatch')
        with self._open_environment() as env:
            if env.cr.dbname != binding.database_name:
                raise ValueError('odoo_database_mismatch')
            mail = env['mail.mail'].browse(binding.mail_id).exists()
            obligation = env['woow_paas_platform.llm_price_notice_outbox'].browse(
                binding.obligation_id).exists()
            if len(mail) != 1 or len(obligation) != 1:
                raise ValueError('committed_source_missing')
            # Narrow initial subset: direct, plain single email_to only. Partner
            # expansion and cc are excluded; actual final SMTP envelope checked too.
            if (mail.email_to != attempt.destination or mail.email_cc or mail.recipient_ids):
                raise ValueError('odoo_mail_recipient_mismatch')
            publication = obligation.publication_id
            recipient = obligation.recipient_id
            if len(publication) != 1 or len(recipient) != 1:
                raise ValueError('committed_source_missing')
            campaign = publication.campaign_id
            if len(campaign) != 1 or len(recipient.campaign_id) != 1:
                raise ValueError('committed_source_missing')
            raw = publication.public_payload_json
            if type(raw) is not str or len(raw) > 1048576:
                raise ValueError('odoo_obligation_snapshot_mismatch')
            try:
                public_payload = json.loads(raw)
            except (TypeError, ValueError):
                raise ValueError('odoo_obligation_snapshot_mismatch') from None
            if (attempt.environment != 'stg' or campaign.environment != 'stg'
                    or publication.environment != 'stg'
                    or not campaign.version.startswith('rehearsal-')
                    or not campaign.body_text.startswith('【合成測試，不代表正式公告】')
                    or not re.fullmatch(r'(?:paas|legacy):synthetic(?:_[A-Za-z0-9_-]{1,54})?',
                                        attempt.recipient_key)
                    or not re.fullmatch(r'[A-Za-z0-9_.+%-]+@(?:[A-Za-z0-9-]+\.)*example\.test',
                                        attempt.destination)
                    or obligation.state != 'pending' or publication.withdrawn_at
                    or recipient.campaign_id != campaign
                    or campaign.fingerprint != attempt.campaign_fingerprint
                    or publication.campaign_fingerprint != campaign.fingerprint
                    or obligation.campaign_fingerprint != campaign.fingerprint
                    or hashlib.sha256(raw.encode('utf-8')).hexdigest() != publication.payload_sha256
                    or obligation.payload_sha256 != publication.payload_sha256
                    or type(public_payload) is not dict
                    or public_payload.get('version') != campaign.version
                    or public_payload.get('body_text') != campaign.body_text
                    or obligation.recipient_key != attempt.recipient_key
                    or recipient.recipient_key != attempt.recipient_key
                    or obligation.destination_snapshot != attempt.destination
                    or recipient.destination_snapshot != attempt.destination
                    or obligation.destination_revision != attempt.destination_revision
                    or recipient.destination_revision != attempt.destination_revision):
                raise ValueError('odoo_obligation_snapshot_mismatch')
        # No mail.state, message_id, notification state or success_pids access.

    def bind(self, binding, *, payload, envelope):
        """Validate committed source before independently persisting binding."""
        self._check_source(binding, payload, envelope)
        self._journal.bind(binding)

    def preflight(self, binding, *, payload, envelope):
        """Re-read committed binding/source; returns no SMTP/acceptance capability."""
        if self._test_mode() is not False:
            raise ValueError('odoo_test_bypass_forbidden')
        if type(binding) is not MailBinding:
            raise ValueError('invalid_mail_binding')
        stored = self._journal.ready_binding(binding.attempt.attempt_uuid)
        if stored != binding:
            raise ValueError('mail_binding_snapshot_mismatch')
        self._check_source(stored, payload, envelope)
        return stored


def odoo_boundary(outer_env, *, service_uid, runtime_environment):
    """Explicit future server entrypoint; does not open cursors or send mail.

    service_uid/environment must come from reviewed server configuration, never
    request context. No sudo: the chosen user must have explicit read access to
    committed mail/obligations. Actual Odoo test mode is read at each preflight.
    """
    if type(service_uid) is not int or service_uid <= 0 or runtime_environment not in ('stg', 'prod'):
        raise ValueError('invalid_odoo_boundary_configuration')
    from odoo import api
    from odoo.modules import module

    factory = RegistryConnectionFactory(outer_env.registry, outer_env.cr)
    return OdooMailBoundary(
        OdooJournal(factory),
        lambda: factory.committed_environment(lambda cr: api.Environment(cr, service_uid, {})),
        runtime_environment=runtime_environment,
        test_mode=lambda: bool(module.current_test),
    )
