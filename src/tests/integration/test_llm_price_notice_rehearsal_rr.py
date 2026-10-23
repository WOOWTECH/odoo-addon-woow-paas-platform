"""Two real Odoo REPEATABLE READ transactions must not leave an outbox gap.

This test commits only synthetic example.test fixtures in an isolated CI DB and
removes them afterwards. A row lock without an UPDATE would fail both races.
"""
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


CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
RECIPIENT = 'woow_paas_platform.llm_price_notice_recipient'
PUBLICATION = 'woow_paas_platform.llm_price_notice_publication'
RUNTIME_KEY = 'llm_price_notice_runtime_environment'
BEFORE = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1'}}}}
AFTER = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1.3'}}}}


@tagged('post_install', '-at_install')
class TestLlmPriceNoticeRehearsalRR(BaseCase):
    def setUp(self):
        super().setUp()
        self.registry = Registry(get_db_name())
        self.version = 'rehearsal-' + uuid.uuid4().hex
        self.body = '【合成測試，不代表正式公告】\n雙交易測試。'
        self.not_before = int(time.time()) + SEVEN_DAYS + 120
        row = RecipientSnapshot('paas:synthetic_one', 'one@example.test', 1)
        proposal = prepare_candidate(
            version=self.version, environment='stg', body_text=self.body,
            before=BEFORE, after=AFTER, recipients=(row,),
            now=int(time.time()), not_before=self.not_before)
        with self.registry.cursor() as cr:
            env = api.Environment(cr, odoo.SUPERUSER_ID, {})
            config = env['ir.config_parameter'].sudo()
            self.original_runtime = config.get_param(RUNTIME_KEY)
            config.set_param(RUNTIME_KEY, 'stg')
            campaign = env[CAMPAIGN].create({
                'version': self.version, 'body_text': self.body,
                'catalog_fingerprint': proposal.catalog_sha256, 'environment': 'stg',
            })
            self.campaign_id = campaign.id
            env[RECIPIENT].create({
                'campaign_id': campaign.id, 'recipient_key': row.key,
                'destination_snapshot': row.destination, 'destination_revision': row.revision,
            })
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        with self.registry.cursor() as cr:
            cr.execute(
                'DELETE FROM woow_paas_platform_llm_price_notice_outbox '
                'WHERE publication_id IN (SELECT id FROM '
                'woow_paas_platform_llm_price_notice_publication WHERE campaign_id = %s)',
                (self.campaign_id,))
            cr.execute('DELETE FROM woow_paas_platform_llm_price_notice_publication '
                       'WHERE campaign_id = %s', (self.campaign_id,))
            cr.execute('DELETE FROM woow_paas_platform_llm_price_notice_recipient '
                       'WHERE campaign_id = %s', (self.campaign_id,))
            cr.execute('DELETE FROM woow_paas_platform_llm_price_notice_campaign '
                       'WHERE id = %s', (self.campaign_id,))
            config = api.Environment(cr, odoo.SUPERUSER_ID, {})['ir.config_parameter'].sudo()
            if self.original_runtime is False:
                config.search([('key', '=', RUNTIME_KEY)]).unlink()
            else:
                config.set_param(RUNTIME_KEY, self.original_runtime)

    def _env(self, cr):
        return api.Environment(cr, odoo.SUPERUSER_ID, {})

    def _prime_snapshot(self, cr):
        cr.execute('SELECT state FROM woow_paas_platform_llm_price_notice_campaign '
                   'WHERE id = %s', (self.campaign_id,))
        self.assertEqual(cr.fetchone()[0], 'draft')

    def _publish(self, cr):
        with patch.dict(os.environ, {'WOOW_LLM_PRICE_NOTICE_REHEARSAL': 'llmdev-synthetic-v1'}):
            return self._env(cr)[PUBLICATION]._llm_publish_synthetic_rehearsal(
                self.campaign_id, BEFORE, AFTER, self.not_before)

    def _late_recipient(self, cr):
        return self._env(cr)[RECIPIENT].create({
            'campaign_id': self.campaign_id,
            'recipient_key': 'legacy:synthetic_late',
            'destination_snapshot': 'late@example.test',
            'destination_revision': 1,
        })

    def test_stale_recipient_transaction_cannot_insert_after_publication(self):
        recipient_cr = self.registry.cursor()
        try:
            self._prime_snapshot(recipient_cr)
            with self.registry.cursor() as publisher_cr:
                self._publish(publisher_cr)
            with self.assertRaises(psycopg2.Error) as error:
                self._late_recipient(recipient_cr)
            self.assertEqual(error.exception.pgcode, '40001')
        finally:
            recipient_cr.rollback()
            recipient_cr.close()
        with self.registry.cursor() as verify:
            verify.execute('SELECT count(*) FROM woow_paas_platform_llm_price_notice_recipient '
                           'WHERE campaign_id = %s', (self.campaign_id,))
            self.assertEqual(verify.fetchone()[0], 1)
            verify.execute('SELECT count(*) FROM woow_paas_platform_llm_price_notice_outbox '
                           'WHERE publication_id IN (SELECT id FROM '
                           'woow_paas_platform_llm_price_notice_publication WHERE campaign_id = %s)',
                           (self.campaign_id,))
            self.assertEqual(verify.fetchone()[0], 1)

    def test_stale_publisher_transaction_cannot_miss_a_new_recipient(self):
        publisher_cr = self.registry.cursor()
        try:
            self._prime_snapshot(publisher_cr)
            with self.registry.cursor() as recipient_cr:
                self._late_recipient(recipient_cr)
            with self.assertRaises(psycopg2.Error) as error:
                self._publish(publisher_cr)
            self.assertEqual(error.exception.pgcode, '40001')
        finally:
            publisher_cr.rollback()
            publisher_cr.close()
        with self.registry.cursor() as verify:
            verify.execute('SELECT count(*) FROM woow_paas_platform_llm_price_notice_recipient '
                           'WHERE campaign_id = %s', (self.campaign_id,))
            self.assertEqual(verify.fetchone()[0], 2)
            verify.execute('SELECT count(*) FROM woow_paas_platform_llm_price_notice_publication '
                           'WHERE campaign_id = %s', (self.campaign_id,))
            self.assertEqual(verify.fetchone()[0], 0)
