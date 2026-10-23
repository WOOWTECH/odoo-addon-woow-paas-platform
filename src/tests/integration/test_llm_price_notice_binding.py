"""A committed Odoo outbox row is the one journal binding source.

Scratch DDL belongs only to this isolated CI database. This test does not
connect to SMTP, dispatch mail, or make a formal notice available.
"""
import hashlib
import os
import time
import uuid
from unittest.mock import patch

import odoo
import psycopg2
from odoo import api
from odoo.modules.registry import Registry
from odoo.tests.common import BaseCase, get_db_name, tagged

from ...services.llm_price_notice import RecipientSnapshot, SEVEN_DAYS, prepare_candidate
from ...services.llm_price_notice_odoo_adapter import (
    BINDING_SCHEMA,
    MailBinding,
    OdooJournal,
    OdooMailBoundary,
    RegistryConnectionFactory,
)
from ...services.llm_price_notice_transport import Attempt, SCHEMA


CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
RECIPIENT = 'woow_paas_platform.llm_price_notice_recipient'
PUBLICATION = 'woow_paas_platform.llm_price_notice_publication'
OUTBOX = 'woow_paas_platform.llm_price_notice_outbox'
RUNTIME_KEY = 'llm_price_notice_runtime_environment'
PAYLOAD = b'To: one@example.test\r\n\r\nSynthetic staging journal probe\r\n'
BEFORE = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1'}}}}
AFTER = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1.3'}}}}
TABLES = ('llm_notice_odoo_mail_binding', 'llm_notice_transport_acceptance',
          'llm_notice_transport_attempt')


@tagged('-standard', '-at_install', 'post_install', 'llm_price_notice_binding_pg')
class TestLlmPriceNoticeOutboxBindingPG(BaseCase):
    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.version = 'rehearsal-' + uuid.uuid4().hex
        self.campaign_id = None
        self.mail_ids = []
        self._lock_cursor = self.registry.cursor()
        self._lock_cursor.execute(
            "SELECT pg_advisory_lock(hashtext('llm_price_notice_journal_rollback_test'))")
        self._lock_cursor.fetchone()
        self.addCleanup(self._release_lock)
        with self.registry.cursor() as cr:
            for table in TABLES:
                cr.execute('SELECT to_regclass(%s)', (table,))
                self.assertIsNone(cr.fetchone()[0], 'journal table already exists: ' + table)
            for statement in (*SCHEMA, BINDING_SCHEMA):
                cr.execute(statement)
        self.addCleanup(self._cleanup)
        body = '【合成測試，不代表正式公告】\noutbox binding PG。'
        not_before = int(time.time()) + SEVEN_DAYS + 120
        row = RecipientSnapshot('paas:synthetic_one', 'one@example.test', 1)
        proposal = prepare_candidate(
            version=self.version, environment='stg', body_text=body,
            before=BEFORE, after=AFTER, recipients=(row,),
            now=int(time.time()), not_before=not_before)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            config = env['ir.config_parameter'].sudo()
            self.original_runtime = config.get_param(RUNTIME_KEY)
            config.set_param(RUNTIME_KEY, 'stg')
            campaign = env[CAMPAIGN].create({
                'version': self.version, 'body_text': body,
                'catalog_fingerprint': proposal.catalog_sha256, 'environment': 'stg',
            })
            self.campaign_id = campaign.id
            env[RECIPIENT].create({
                'campaign_id': campaign.id, 'recipient_key': row.key,
                'destination_snapshot': row.destination, 'destination_revision': 1,
            })
            with patch.dict(os.environ, {'WOOW_LLM_PRICE_NOTICE_REHEARSAL': 'llmdev-synthetic-v1'}):
                publication = env[PUBLICATION]._llm_publish_synthetic_rehearsal(
                    campaign.id, BEFORE, AFTER, not_before)
            outbox = env[OUTBOX].search([('publication_id', '=', publication.id)])
            self.assertEqual(len(outbox), 1)
            self.outbox_id = outbox.id
            self.campaign_fingerprint = campaign.fingerprint
            self.mail_ids.append(self._create_mail(env))

    @staticmethod
    def _create_mail(env):
        return env['mail.mail'].create({
            'subject': 'Synthetic staging notice journal probe',
            'body_html': '<p>【合成測試，不代表正式公告】</p>',
            'email_from': 'notice@example.test',
            'email_to': 'one@example.test',
            'auto_delete': False,
        }).id

    def _release_lock(self):
        try:
            self._lock_cursor.execute(
                "SELECT pg_advisory_unlock(hashtext('llm_price_notice_journal_rollback_test'))")
            self._lock_cursor.fetchone()
        finally:
            self._lock_cursor.close()

    def _cleanup(self):
        with self.registry.cursor() as cr:
            cr.execute('DROP TABLE IF EXISTS llm_notice_odoo_mail_binding')
            cr.execute('DROP TABLE IF EXISTS llm_notice_transport_acceptance')
            cr.execute('DROP TABLE IF EXISTS llm_notice_transport_attempt')
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            env['mail.mail'].browse(self.mail_ids).exists().unlink()
            if self.campaign_id:
                cr.execute('DELETE FROM woow_paas_platform_llm_price_notice_outbox '
                           'WHERE publication_id IN (SELECT id FROM '
                           'woow_paas_platform_llm_price_notice_publication WHERE campaign_id = %s)',
                           (self.campaign_id,))
                cr.execute('DELETE FROM woow_paas_platform_llm_price_notice_publication '
                           'WHERE campaign_id = %s', (self.campaign_id,))
                cr.execute('DELETE FROM woow_paas_platform_llm_price_notice_recipient '
                           'WHERE campaign_id = %s', (self.campaign_id,))
                cr.execute('DELETE FROM woow_paas_platform_llm_price_notice_campaign '
                           'WHERE id = %s', (self.campaign_id,))
            if hasattr(self, 'original_runtime'):
                config = env['ir.config_parameter'].sudo()
                if self.original_runtime is False:
                    config.search([('key', '=', RUNTIME_KEY)]).unlink()
                else:
                    config.set_param(RUNTIME_KEY, self.original_runtime)

    def test_committed_outbox_is_required_and_distinct_attempts_cannot_rebind_it(self):
        outer = self.registry.cursor()
        try:
            factory = RegistryConnectionFactory(self.registry, outer)
            journal = OdooJournal(factory)
            boundary = OdooMailBoundary(
                journal,
                lambda: factory.committed_environment(
                    lambda cr: api.Environment(cr, odoo.SUPERUSER_ID, {})),
                runtime_environment='stg',
                # No SMTP occurs; this exercises committed ORM reads only.
                test_mode=lambda: False)
            attempt = Attempt(
                str(uuid.uuid4()), self.campaign_fingerprint, 'stg',
                'paas:synthetic_one', 'one@example.test', 1,
                hashlib.sha256(PAYLOAD).hexdigest())
            binding = MailBinding(attempt, get_db_name(), self.mail_ids[0], self.outbox_id)
            journal.prepare(attempt)
            boundary.bind(binding, payload=PAYLOAD, envelope=(attempt.destination,))
            self.assertEqual(boundary.preflight(
                binding, payload=PAYLOAD, envelope=(attempt.destination,)), binding)
            with self.registry.cursor() as cr:
                self.mail_ids.append(self._create_mail(api.Environment(cr, odoo.SUPERUSER_ID, {})))
            duplicate_attempt = Attempt(
                str(uuid.uuid4()), self.campaign_fingerprint, 'stg',
                'paas:synthetic_one', 'one@example.test', 1,
                hashlib.sha256(PAYLOAD).hexdigest())
            journal.prepare(duplicate_attempt)
            duplicate = MailBinding(duplicate_attempt, get_db_name(),
                                    self.mail_ids[1], self.outbox_id)
            with self.assertRaises(psycopg2.errors.UniqueViolation):
                journal.bind(duplicate)
            with self.registry.cursor() as verify:
                verify.execute('SELECT count(*) FROM llm_notice_odoo_mail_binding '
                               'WHERE database_name = %s AND obligation_id = %s',
                               (get_db_name(), self.outbox_id))
                self.assertEqual(verify.fetchone()[0], 1)
                verify.execute('SELECT count(*) FROM llm_notice_transport_acceptance')
                self.assertEqual(verify.fetchone()[0], 0)
        finally:
            outer.close()

    def test_uncommitted_mail_cannot_be_bound_from_an_independent_cursor(self):
        outer = self.registry.cursor()
        try:
            uncommitted_mail_id = self._create_mail(api.Environment(
                outer, odoo.SUPERUSER_ID, {}))
            factory = RegistryConnectionFactory(self.registry, outer)
            journal = OdooJournal(factory)
            boundary = OdooMailBoundary(
                journal,
                lambda: factory.committed_environment(
                    lambda cr: api.Environment(cr, odoo.SUPERUSER_ID, {})),
                runtime_environment='stg', test_mode=lambda: False)
            attempt = Attempt(
                str(uuid.uuid4()), self.campaign_fingerprint, 'stg',
                'paas:synthetic_one', 'one@example.test', 1,
                hashlib.sha256(PAYLOAD).hexdigest())
            binding = MailBinding(attempt, get_db_name(),
                                  uncommitted_mail_id, self.outbox_id)
            journal.prepare(attempt)
            with self.assertRaisesRegex(ValueError, 'committed_source_missing'):
                boundary.bind(binding, payload=PAYLOAD, envelope=(attempt.destination,))
            outer.rollback()
            with self.registry.cursor() as verify:
                verify.execute('SELECT count(*) FROM llm_notice_odoo_mail_binding '
                               'WHERE obligation_id = %s', (self.outbox_id,))
                self.assertEqual(verify.fetchone()[0], 0)
                verify.execute('SELECT count(*) FROM mail_mail WHERE id = %s',
                               (uncommitted_mail_id,))
                self.assertEqual(verify.fetchone()[0], 0)
        finally:
            outer.close()
