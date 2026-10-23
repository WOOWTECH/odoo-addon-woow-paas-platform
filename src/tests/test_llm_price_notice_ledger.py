"""Synthetic draft ledger tests; never publish, send, or authorize a price change."""
import hashlib
import json
import unittest
import uuid
from unittest.mock import patch

import psycopg2
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from ..services.llm_price_notice import RecipientSnapshot, prepare_candidate

CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
RECIPIENT = 'woow_paas_platform.llm_price_notice_recipient'


@tagged('post_install', '-at_install')
class TestLlmPriceNoticeLedger(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex
        cls.admin = new_test_user(
            cls.env, login='notice_admin_' + suffix,
            groups='base.group_user,base.group_system', email='admin@example.test')
        cls.normal = new_test_user(
            cls.env, login='notice_normal_' + suffix,
            groups='base.group_user', email='normal@example.test')
        cls.portal = new_test_user(
            cls.env, login='notice_portal_' + suffix,
            groups='base.group_portal', email='portal@example.test')
        cls.manager = new_test_user(
            cls.env, login='notice_manager_' + suffix,
            groups='base.group_user,account.group_account_manager', email='manager@example.test')

    def _campaign_values(self, **changes):
        vals = {'version': 'test-' + uuid.uuid4().hex, 'body_text': 'Synthetic notice: 1 → 1.3',
                'catalog_fingerprint': 'a' * 64, 'environment': 'stg'}
        vals.update(changes)
        return vals

    def _campaign(self, **changes):
        return self.env[CAMPAIGN].with_user(self.admin).create(self._campaign_values(**changes))

    def _recipient_values(self, campaign, **changes):
        vals = {'campaign_id': campaign.id, 'recipient_key': 'paas:synthetic',
                'destination_revision': 1, 'destination_snapshot': 'recipient@example.test'}
        vals.update(changes)
        return vals

    def _recipient(self, campaign, **changes):
        return self.env[RECIPIENT].with_user(self.admin).create(
            self._recipient_values(campaign, **changes))

    def test_admin_and_superuser_create_and_read_drafts(self):
        campaign = self._campaign()
        obligation = self._recipient(campaign)
        self.assertEqual(campaign.read(['state'])[0]['state'], 'draft')
        self.assertEqual(obligation.read(['state'])[0]['state'], 'pending')
        root_campaign = self.env[CAMPAIGN].sudo().create(self._campaign_values())
        self.assertTrue(self.env[RECIPIENT].sudo().create(self._recipient_values(root_campaign)))

    def test_non_admin_cannot_crud_or_copy_even_with_forged_context(self):
        campaign = self._campaign()
        obligation = self._recipient(campaign)
        for user in (self.normal, self.portal, self.manager, self.env.ref('base.public_user')):
            for record, values in (
                    (campaign, self._campaign_values()),
                    (obligation, self._recipient_values(campaign))):
                model = self.env[record._name].with_user(user).sudo(False).with_context(
                    is_admin=True, price_notice_internal=True, install_mode=True)
                target = model.browse(record.id)
                with self.subTest(user=user.id, model=record._name):
                    with self.assertRaises(AccessError):
                        model.create(values)
                    with self.assertRaises(AccessError):
                        target.read(['state'])
                    with self.assertRaises(AccessError):
                        target.write({'state': 'draft'})
                    with self.assertRaises(AccessError):
                        target.unlink()
                    with self.assertRaises(AccessError):
                        target.copy()

    def test_snapshots_cannot_be_mutated_or_deleted_even_by_sudo(self):
        campaign = self._campaign()
        obligation = self._recipient(campaign)
        for record in (campaign, obligation):
            for actor_record in (record, record.sudo()):
                with self.assertRaises(AccessError):
                    actor_record.write({})
                with self.assertRaises(AccessError):
                    actor_record.unlink()
                with self.assertRaises(AccessError):
                    actor_record.copy()
        for vals in ({'campaign_id': self._campaign().id}, {'recipient_key': 'legacy:other'},
                     {'destination_snapshot': 'new@example.test'}, {'destination_revision': 2}):
            with self.assertRaises(AccessError):
                obligation.sudo().write(vals)
        for field in ('version', 'body_text', 'catalog_fingerprint', 'environment', 'state'):
            with self.assertRaises(AccessError):
                campaign.sudo().write({field: 'changed'})

    def test_unique_campaign_version_with_environment_scope(self):
        first = self._campaign()
        # Odoo assertRaises uses issubclass and cannot accept an exception tuple.
        # Raw unittest plus an explicit savepoint covers ORM and driver errors.
        with (
            mute_logger('odoo.sql_db'),
            unittest.TestCase.assertRaises(self, (ValidationError, psycopg2.IntegrityError)),
            self.env.cr.savepoint(),
        ):
            self._campaign(version=first.version)
        self.assertTrue(self._campaign(version=first.version, environment='prod'))

    def test_unique_recipient_is_scoped_to_campaign_including_batch(self):
        campaign = self._campaign()
        self._recipient(campaign)
        with (
            mute_logger('odoo.sql_db'),
            unittest.TestCase.assertRaises(self, (ValidationError, psycopg2.IntegrityError)),
            self.env.cr.savepoint(),
        ):
            self._recipient(campaign, destination_revision=2)
        second = self._campaign()
        self.assertTrue(self._recipient(second))
        values = self._recipient_values(campaign, recipient_key='legacy:duplicate')
        with (
            mute_logger('odoo.sql_db'),
            unittest.TestCase.assertRaises(self, (ValidationError, psycopg2.IntegrityError)),
            self.env.cr.savepoint(),
        ):
            self.env[RECIPIENT].with_user(self.admin).create([values, values])
        self.assertFalse(self.env[RECIPIENT].sudo().search([
            ('campaign_id', '=', campaign.id), ('recipient_key', '=', 'legacy:duplicate')]))

    def test_missing_addresses_remain_obligations(self):
        campaign = self._campaign()
        for index, value in enumerate((False, None, '', ' \t\n')):
            obligation = self._recipient(campaign, recipient_key=f'paas:missing_{index}',
                                         destination_snapshot=value)
            self.assertEqual(obligation.state, 'missing_address')
            self.assertFalse(obligation.destination_snapshot)
        omitted = self._recipient_values(campaign, recipient_key='legacy:missing')
        omitted.pop('destination_snapshot')
        self.assertEqual(self.env[RECIPIENT].with_user(self.admin).create(omitted).state,
                         'missing_address')
        self.assertEqual(self.env[RECIPIENT].sudo().search_count([
            ('campaign_id', '=', campaign.id)]), 5)

    def test_snapshot_is_exact_and_revision_is_explicit(self):
        campaign = self._campaign()
        destination = ' Synthetic.Recipient@example.test '
        obligation = self._recipient(campaign, destination_snapshot=destination, destination_revision=3)
        self.assertEqual(obligation.destination_snapshot, destination)
        self.assertEqual(obligation.destination_revision, 3)
        for revision in (0, -1, True, '1', 2147483648):
            with self.subTest(revision=revision), self.assertRaises(ValidationError):
                self._recipient(campaign, destination_revision=revision)
        with self.assertRaises(ValidationError):
            self._recipient(campaign, recipient_key='recipient@example.test')

    def test_caller_cannot_supply_evidence_or_state_or_metadata(self):
        campaign = self._campaign()
        for model, values in ((self.env[CAMPAIGN], self._campaign_values()),
                              (self.env[RECIPIENT], self._recipient_values(campaign))):
            for extra in ({'state': 'sent'}, {'sent_at': '2026-01-01'},
                          {'fingerprint': 'f' * 64}, {'create_uid': self.normal.id}):
                with self.subTest(model=model._name, extra=extra), self.assertRaises(ValidationError):
                    model.sudo().create(dict(values, **extra))
        model = self.env[CAMPAIGN].with_user(self.admin).with_context(
            default_state='published', default_create_uid=self.normal.id)
        created = model.create(self._campaign_values())
        self.assertEqual(created.state, 'draft')
        self.assertEqual(created.create_uid, self.admin)
        recipient = self.env[RECIPIENT].with_user(self.admin).with_context(
            default_state='sent', default_destination_snapshot='injected@example.test').create(
                self._recipient_values(campaign, destination_snapshot=False))
        self.assertEqual(recipient.state, 'missing_address')
        self.assertFalse(recipient.destination_snapshot)

    def test_fingerprint_binds_each_snapshot_component(self):
        campaign = self._campaign()
        self.assertEqual(len(campaign.fingerprint), 64)
        for index, changes in enumerate(({'body_text': 'Different synthetic body'},
                                         {'catalog_fingerprint': 'b' * 64}, {'environment': 'prod'},
                                         {'version': campaign.version + '-v2'})):
            values = self._campaign_values(version=campaign.version)
            values.update(changes)
            if changes.keys() <= {'body_text', 'catalog_fingerprint'}:
                # Same identity cannot be reused even for a changed payload.
                with (
                    mute_logger('odoo.sql_db'),
                    unittest.TestCase.assertRaises(self, (ValidationError, psycopg2.IntegrityError)),
                    self.env.cr.savepoint(),
                ):
                    self.env[CAMPAIGN].with_user(self.admin).create(values)
                values['version'] += f'-correction-{index}'
            changed = self.env[CAMPAIGN].with_user(self.admin).create(values)
            self.assertNotEqual(changed.fingerprint, campaign.fingerprint)
            expected = hashlib.sha256(json.dumps(
                values, ensure_ascii=True, sort_keys=True, separators=(',', ':')
            ).encode('utf-8')).hexdigest()
            self.assertEqual(changed.fingerprint, expected)

    def test_invalid_campaign_and_missing_parent_rejected(self):
        for changes in ({'body_text': '  '}, {'catalog_fingerprint': 'not-a-sha256'},
                        {'environment': 'unknown'}, {'version': ' '}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self._campaign(**changes)
        campaign = self._campaign()
        with self.assertRaises(ValidationError):
            self._recipient(campaign, campaign_id=2147483647)

    def test_create_has_no_mail_network_or_scheduling_side_effects(self):
        with patch.object(type(self.env['mail.mail']), 'create') as mail_create, \
                patch.object(type(self.env['mail.mail']), 'send') as mail_send, \
                patch.object(type(self.env['ir.cron']), 'create') as cron_create, \
                patch('requests.sessions.Session.request') as http_request:
            campaign = self._campaign()
            self._recipient(campaign)
            self._recipient(campaign, recipient_key='legacy:missing', destination_snapshot=False)
            for spy in (mail_create, mail_send, cron_create, http_request):
                spy.assert_not_called()
        self.assertEqual(campaign.state, 'draft')

    def test_private_candidate_preview_binds_actual_draft_and_obligations(self):
        before = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1'}}}}
        after = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1.3'}}}}
        rows = (RecipientSnapshot('paas:synthetic', 'one@example.test', 1),
                RecipientSnapshot('legacy:synthetic', None, 2))
        version = 'test-' + uuid.uuid4().hex
        body = '合成公告：價格調整'
        proposed = prepare_candidate(version=version, environment='stg', body_text=body,
                                     before=before, after=after, recipients=rows,
                                     now=1800000000, not_before=1800604800)
        campaign = self._campaign(version=version, body_text=body,
                                  catalog_fingerprint=proposed.catalog_sha256)
        self._recipient(campaign, recipient_key='paas:synthetic',
                        destination_snapshot='one@example.test')
        self._recipient(campaign, recipient_key='legacy:synthetic',
                        destination_snapshot=False, destination_revision=2)
        preview = campaign.with_user(self.admin)._llm_candidate_preview(
            before, after, now=1800000000, not_before=1800604800)
        self.assertEqual(preview, proposed)
        self.assertEqual(preview.campaign_fingerprint, campaign.fingerprint)
        self.assertEqual(preview.unresolved_count, 1)
        self.assertFalse(self.env['woow_paas_platform.llm_price_notice_publication'].sudo().search([
            ('campaign_id', '=', campaign.id)]))
        self._recipient(campaign, recipient_key='paas:joined_later',
                        destination_snapshot='two@example.test')
        changed = campaign.with_user(self.admin)._llm_candidate_preview(
            before, after, now=1800000000, not_before=1800604800)
        self.assertNotEqual(changed.fingerprint, preview.fingerprint)
        self.assertEqual(changed.recipient_count, 3)

    def test_candidate_preview_rejects_wrong_catalog_and_unprivileged_user(self):
        before = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1'}}}}
        after = {'default': {'vendor/model': {'mode': 'fixed', 'rates': {'per_call': '1.3'}}}}
        campaign = self._campaign()
        self._recipient(campaign)
        with self.assertRaises(ValidationError):
            campaign.with_user(self.admin)._llm_candidate_preview(
                before, after, now=1800000000, not_before=1800604800)
        with self.assertRaises(AccessError):
            campaign.with_user(self.normal).sudo(False).with_context(
                price_notice_approved=True)._llm_candidate_preview(
                    before, after, now=1800000000, not_before=1800604800)
