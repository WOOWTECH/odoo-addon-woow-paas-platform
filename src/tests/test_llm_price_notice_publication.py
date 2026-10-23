"""Synthetic publication rows test the dormant read path; there is no runtime writer."""
import hashlib
import json
import uuid

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests.common import TransactionCase, new_test_user, tagged

CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
PUBLICATION = 'woow_paas_platform.llm_price_notice_publication'


@tagged('post_install', '-at_install')
class TestLlmPriceNoticePublication(TransactionCase):
    def setUp(self):
        super().setUp()
        suffix = uuid.uuid4().hex
        self.user = new_test_user(self.env, login='notice_public_' + suffix,
                                  groups='base.group_user', email='reader@example.test')
        self.campaign = self.env[CAMPAIGN].sudo().create({
            'version': 'stg-' + suffix, 'body_text': 'Synthetic approved notice text',
            'catalog_fingerprint': 'a' * 64, 'environment': 'stg',
        })
        self.config = self.env['ir.config_parameter'].sudo()
        self.config.set_param('llm_price_notice_public_read_enabled', '0')
        self.config.set_param('llm_price_notice_runtime_environment', '')

    def _payload(self):
        return {
            'id': 'pc_' + 'b' * 32, 'version': self.campaign.version,
            'title': 'Synthetic AI price announcement',
            'body_text': self.campaign.body_text, 'not_before': 1800000000,
            'changes': [{'model': 'vendor/model', 'mode': 'fixed',
                         'before': {'per_call': '1'}, 'after': {'per_call': '1.3'}}],
        }

    def _synthetic_row(self, **changes):
        """Test-only SQL fixture bypasses the disabled ORM writer."""
        raw = json.dumps(self._payload(), ensure_ascii=True, sort_keys=True)
        vals = {
            'campaign_id': self.campaign.id, 'campaign_fingerprint': self.campaign.fingerprint,
            'environment': 'stg', 'public_payload_json': raw,
            'payload_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest(),
        }
        vals.update(changes)
        self.env.cr.execute(
            'INSERT INTO woow_paas_platform_llm_price_notice_publication '
            '(campaign_id, campaign_fingerprint, environment, public_payload_json, '
            'payload_sha256, withdrawn_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
            (vals['campaign_id'], vals['campaign_fingerprint'], vals['environment'],
             vals['public_payload_json'], vals['payload_sha256'], vals.get('withdrawn_at')),
        )
        return self.env[PUBLICATION].sudo().browse(self.env.cr.fetchone()[0])

    def _enable(self):
        self.config.set_param('llm_price_notice_public_read_enabled', '1')
        self.config.set_param('llm_price_notice_runtime_environment', 'stg')

    def test_default_stays_empty_even_if_a_synthetic_row_exists(self):
        self._synthetic_row()
        self.assertEqual(self.env[PUBLICATION].with_user(self.user)._llm_public_notices(), [])
        self.config.set_param('llm_price_notice_public_read_enabled', '1')
        self.assertEqual(self.env[PUBLICATION].with_user(self.user)._llm_public_notices(), [])

    def test_enabled_reader_projects_only_public_fields(self):
        self._synthetic_row()
        self._enable()
        result = self.env[PUBLICATION].with_user(self.user)._llm_public_notices()
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]), {'id', 'version', 'title', 'body_text', 'not_before', 'changes'})
        self.assertNotIn('private', repr(result))
        self.assertEqual(result[0]['changes'][0]['after']['per_call'], '1.3')

    def test_draft_and_bad_binding_never_become_public(self):
        self._enable()
        self.assertEqual(self.env[PUBLICATION].with_user(self.user)._llm_public_notices(), [])
        self._synthetic_row(campaign_fingerprint='0' * 64)
        self.assertEqual(self.env[PUBLICATION].with_user(self.user)._llm_public_notices(), [])

    def test_wrong_environment_is_hidden(self):
        self._enable()
        self._synthetic_row(environment='prod')
        self.assertEqual(self.env[PUBLICATION].with_user(self.user)._llm_public_notices(), [])

    def test_withdrawn_publication_is_hidden(self):
        self._enable()
        self._synthetic_row(withdrawn_at=fields.Datetime.now())
        self.assertEqual(self.env[PUBLICATION].with_user(self.user)._llm_public_notices(), [])

    def test_tampered_payload_or_body_is_hidden(self):
        self._enable()
        self._synthetic_row(payload_sha256='0' * 64)
        self.assertEqual(self.env[PUBLICATION].with_user(self.user)._llm_public_notices(), [])

    def test_private_fields_in_stored_json_fail_closed(self):
        self._enable()
        payload = {**self._payload(), 'recipients': ['legacy:private']}
        raw = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        self._synthetic_row(public_payload_json=raw,
                            payload_sha256=hashlib.sha256(raw.encode('utf-8')).hexdigest())
        self.assertEqual(self.env[PUBLICATION].with_user(self.user)._llm_public_notices(), [])

    def test_tenant_cannot_read_and_no_orm_writer_exists_even_for_sudo(self):
        row = self._synthetic_row()
        with self.assertRaises(AccessError):
            self.env[PUBLICATION].with_user(self.user).search([]).read(['id'])
        for model in (self.env[PUBLICATION], self.env[PUBLICATION].sudo()):
            with self.assertRaises(AccessError):
                model.create({'campaign_id': self.campaign.id})
            with self.assertRaises(AccessError):
                model.browse(row.id).write({})
            with self.assertRaises(AccessError):
                model.browse(row.id).unlink()
            with self.assertRaises(AccessError):
                model.browse(row.id).copy()
