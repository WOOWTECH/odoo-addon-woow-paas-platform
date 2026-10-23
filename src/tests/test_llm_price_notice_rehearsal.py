"""Real ORM/PG checks for default-off, synthetic-only atomic notice rehearsal."""
import os
import time
import unittest
import uuid
from unittest.mock import patch

import psycopg2
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..models import llm_price_notice_publication as publication_module
from ..services.llm_price_notice import RecipientSnapshot, SEVEN_DAYS, prepare_candidate


CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
RECIPIENT = 'woow_paas_platform.llm_price_notice_recipient'
PUBLICATION = 'woow_paas_platform.llm_price_notice_publication'
OUTBOX = 'woow_paas_platform.llm_price_notice_outbox'
MARKER = '【合成測試，不代表正式公告】'
BEFORE = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1'}}}}
AFTER = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1.3'}}}}


@tagged('post_install', '-at_install')
class TestLlmPriceNoticeRehearsal(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex
        cls.admin = new_test_user(
            cls.env, login='notice_rehearsal_admin_' + suffix,
            groups='base.group_user,base.group_system', email='admin@example.test')
        cls.normal = new_test_user(
            cls.env, login='notice_rehearsal_user_' + suffix,
            groups='base.group_user', email='user@example.test')

    def setUp(self):
        super().setUp()
        self.config = self.env['ir.config_parameter'].sudo()
        self.config.set_param('llm_price_notice_runtime_environment', 'stg')
        self.config.set_param('llm_price_notice_public_read_enabled', '0')
        self.not_before = int(time.time()) + SEVEN_DAYS + 120

    def _campaign(self, *, environment='stg', address='one@example.test',
                  body=MARKER + '\n示範價格，沒有實際調價。'):
        version = 'rehearsal-' + uuid.uuid4().hex
        rows = (
            RecipientSnapshot('paas:synthetic_one', address, 1),
            RecipientSnapshot('legacy:synthetic_missing', None, 2),
        )
        proposal = prepare_candidate(
            version=version, environment=environment, body_text=body,
            before=BEFORE, after=AFTER, recipients=rows,
            now=int(time.time()), not_before=self.not_before)
        campaign = self.env[CAMPAIGN].with_user(self.admin).create({
            'version': version, 'body_text': body,
            'catalog_fingerprint': proposal.catalog_sha256,
            'environment': environment,
        })
        for row in rows:
            self.env[RECIPIENT].with_user(self.admin).create({
                'campaign_id': campaign.id, 'recipient_key': row.key,
                'destination_snapshot': row.destination,
                'destination_revision': row.revision,
            })
        return campaign

    def _publish(self, campaign, *, actor=None):
        with patch.dict(os.environ, {'WOOW_LLM_PRICE_NOTICE_REHEARSAL': 'llmdev-synthetic-v1'}):
            return self.env[PUBLICATION].with_user(actor or self.admin).\
                _llm_publish_synthetic_rehearsal(
                    campaign.id, BEFORE, AFTER, self.not_before)

    def _counts(self):
        return (self.env[PUBLICATION].sudo().search_count([]),
                self.env[OUTBOX].sudo().search_count([]))

    def test_default_off_and_wrong_runtime_environment_leave_no_rows(self):
        campaign = self._campaign()
        with patch.dict(os.environ, {'WOOW_LLM_PRICE_NOTICE_REHEARSAL': ''}):
            with self.assertRaises(AccessError):
                self.env[PUBLICATION].with_user(self.admin)._llm_publish_synthetic_rehearsal(
                    campaign.id, BEFORE, AFTER, self.not_before)
        self.config.set_param('llm_price_notice_runtime_environment', 'prod')
        with self.assertRaises(AccessError):
            self._publish(campaign)
        self.assertEqual(self._counts(), (0, 0))

    def test_synthetic_publication_and_every_obligation_share_one_transaction(self):
        campaign = self._campaign()
        with patch.object(type(self.env['mail.mail']), 'create') as mail_create, \
                patch.object(type(self.env['mail.mail']), 'send') as mail_send, \
                patch('requests.sessions.Session.request') as http_request:
            publication = self._publish(campaign)
            for spy in (mail_create, mail_send, http_request):
                spy.assert_not_called()
        self.assertEqual(self._counts(), (1, 2))
        rows = self.env[OUTBOX].sudo().search([('publication_id', '=', publication.id)])
        self.assertEqual(set(rows.mapped('state')), {'pending', 'missing_address'})
        self.assertEqual({row.campaign_fingerprint for row in rows}, {campaign.fingerprint})
        self.assertEqual({row.payload_sha256 for row in rows}, {publication.payload_sha256})
        self.assertEqual({row.destination_revision for row in rows}, {1, 2})
        missing = rows.filtered(lambda row: row.state == 'missing_address')
        self.assertFalse(missing.destination_snapshot)
        self.assertEqual(publication.environment, 'stg')

    def test_public_reader_remains_separately_default_off_and_labelled(self):
        publication = self._publish(self._campaign())
        reader = self.env[PUBLICATION].with_user(self.normal)
        self.assertEqual(reader._llm_public_notices(), [])
        self.config.set_param('llm_price_notice_public_read_enabled', '1')
        notices = reader._llm_public_notices()
        self.assertEqual(len(notices), 1)
        self.assertTrue(notices[0]['body_text'].startswith(MARKER))
        self.assertEqual(notices[0]['id'], publication._llm_public_notices()[0]['id'])
        self.assertNotIn('synthetic_one', repr(notices))

    def test_foreign_actor_and_context_cannot_publish_or_read_outbox(self):
        campaign = self._campaign()
        actor = self.env[PUBLICATION].with_user(self.normal).sudo(False).with_context(
            price_notice_internal=True, install_mode=True)
        with patch.dict(os.environ, {'WOOW_LLM_PRICE_NOTICE_REHEARSAL': 'llmdev-synthetic-v1'}):
            with self.assertRaises(AccessError):
                actor._llm_publish_synthetic_rehearsal(
                    campaign.id, BEFORE, AFTER, self.not_before)
        with self.assertRaises(AccessError):
            self.env[OUTBOX].with_user(self.normal).search([]).read(['id'])
        self.assertEqual(self._counts(), (0, 0))

    def test_prod_campaign_and_real_destination_cannot_be_published(self):
        for campaign in (
                self._campaign(environment='prod'),
                self._campaign(address='real@example.com'),
                self._campaign(body='Unlabelled synthetic text')):
            with self.subTest(campaign=campaign.id), self.assertRaises(ValidationError):
                self._publish(campaign)
        self.assertEqual(self._counts(), (0, 0))

    def test_candidate_mismatch_and_mid_insert_failure_roll_back_both_tables(self):
        campaign = self._campaign()
        with self.assertRaises(ValidationError):
            with patch.dict(os.environ, {'WOOW_LLM_PRICE_NOTICE_REHEARSAL': 'llmdev-synthetic-v1'}):
                self.env[PUBLICATION].with_user(self.admin)._llm_publish_synthetic_rehearsal(
                    campaign.id, AFTER, AFTER, self.not_before)
        original = publication_module._insert_rehearsal_outbox

        def fail_after_insert(*args):
            original(*args)
            raise RuntimeError('synthetic rollback probe')

        with patch.object(publication_module, '_insert_rehearsal_outbox', fail_after_insert):
            with self.assertRaisesRegex(RuntimeError, 'synthetic rollback probe'):
                self._publish(campaign)
        self.assertEqual(self._counts(), (0, 0))

    def test_reentry_and_direct_orm_mutation_are_denied(self):
        campaign = self._campaign()
        publication = self._publish(campaign)
        row = self.env[OUTBOX].sudo().search([('publication_id', '=', publication.id)], limit=1)
        for model, record in ((self.env[OUTBOX].sudo(), row),
                              (self.env[PUBLICATION].sudo(), publication)):
            with self.assertRaises(AccessError):
                model.create({'campaign_id': campaign.id})
            with self.assertRaises(AccessError):
                record.write({})
            with self.assertRaises(AccessError):
                record.unlink()
            with self.assertRaises(AccessError):
                record.copy()
        with (mute_logger('odoo.sql_db'),
              unittest.TestCase.assertRaises(self, (ValidationError, psycopg2.IntegrityError)),
              self.env.cr.savepoint()):
            self._publish(campaign)
        self.assertEqual(self._counts(), (1, 2))
