"""Public notice rows with only a default-off synthetic staging writer.

The existing draft campaign is never rendered directly. Rehearsal rows carry
no approval, delivery, withdrawal or production authority. A future reviewed
publisher needs trusted sources and independent visibility evidence.
"""
import hashlib
import json
import os
import re
import time

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from ..services.llm_price_notice_public import project_public_notices
from .llm_price_notice import _require_admin


_CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
_PUBLICATION = 'woow_paas_platform.llm_price_notice_publication'
_READ_FLAG = 'llm_price_notice_public_read_enabled'
_RUNTIME_ENV = 'llm_price_notice_runtime_environment'
_RECIPIENT = 'woow_paas_platform.llm_price_notice_recipient'
_OUTBOX_TABLE = 'woow_paas_platform_llm_price_notice_outbox'
_PUBLICATION_TABLE = 'woow_paas_platform_llm_price_notice_publication'
_CAMPAIGN_TABLE = 'woow_paas_platform_llm_price_notice_campaign'
_REHEARSAL_MARKER = '【合成測試，不代表正式公告】'
_SYNTHETIC_KEY = re.compile(r'(?:paas|legacy):synthetic(?:_[A-Za-z0-9_-]{1,54})?\Z')
_SYNTHETIC_ADDRESS = re.compile(r'[A-Za-z0-9_.+%-]+@(?:[A-Za-z0-9-]+\.)*example\.test\Z')


def _insert_rehearsal_outbox(cr, publication_id, campaign, rows, payload_sha256, actor_id):
    """Insert every synthetic obligation in the publisher's current transaction."""
    for row in rows:
        cr.execute(
            f'''INSERT INTO {_OUTBOX_TABLE}
                (publication_id, recipient_id, recipient_key, destination_snapshot,
                 destination_revision, campaign_fingerprint, payload_sha256, state,
                 create_uid, write_uid, create_date, write_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                        CURRENT_TIMESTAMP AT TIME ZONE 'UTC')''',
            (publication_id, row.id, row.recipient_key,
             row.destination_snapshot or None, row.destination_revision,
             campaign.fingerprint, payload_sha256,
             'pending' if row.destination_snapshot else 'missing_address',
             actor_id, actor_id))


class LlmPriceNoticePublication(models.Model):
    _name = _PUBLICATION
    _description = 'AI price notice publication (read-only, dormant)'
    _rec_name = 'campaign_id'
    _order = 'id desc'

    campaign_id = fields.Many2one(_CAMPAIGN, required=True, readonly=True,
                                  copy=False, ondelete='restrict', index=True)
    campaign_fingerprint = fields.Char(required=True, readonly=True, copy=False)
    environment = fields.Selection([('stg', 'Staging'), ('prod', 'Production')],
                                   required=True, readonly=True, copy=False, index=True)
    public_payload_json = fields.Text(required=True, readonly=True, copy=False)
    payload_sha256 = fields.Char(required=True, readonly=True, copy=False)
    withdrawn_at = fields.Datetime(readonly=True, copy=False)

    _sql_constraints = [  # noqa: RUF012 - Odoo declarative metadata
        ('campaign_once', 'UNIQUE(campaign_id)', 'A campaign may have only one publication row.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError('Price notice publication is disabled.')

    def write(self, vals):
        raise AccessError('Price notice publication is immutable.')

    def unlink(self):
        raise AccessError('Price notice publication cannot be deleted.')

    def copy(self, default=None):
        raise AccessError('Price notice publication cannot be copied.')

    @api.model
    def _llm_publish_synthetic_rehearsal(self, campaign_id, before, after, not_before):
        """Staging-only, default-off atomic publication for reserved test addresses.

        Caller-supplied prices and campaign rows have no legal/source authority.
        No SMTP dispatch, visibility receipt, seven-day clock or production
        approval can be derived from this rehearsal publication/outbox.
        """
        _require_admin(self)
        if (os.environ.get('WOOW_LLM_PRICE_NOTICE_REHEARSAL') != 'llmdev-synthetic-v1'
                or self.env['ir.config_parameter'].sudo().get_param(_RUNTIME_ENV) != 'stg'
                or type(campaign_id) is not int or campaign_id <= 0):
            raise AccessError('Synthetic price notice rehearsal is disabled.')
        with self.env.cr.savepoint():
            # Odoo's REPEATABLE READ needs a *written* conflict row. A lock
            # alone would let a waiting transaction query its old snapshot.
            # Recipient.create writes this same row before its publication
            # check; 40001 propagates for Odoo's whole-request retry.
            self.env.cr.execute(
                f'''UPDATE {_CAMPAIGN_TABLE}
                    SET write_date = CURRENT_TIMESTAMP AT TIME ZONE 'UTC'
                    WHERE id = %s RETURNING id''', (campaign_id,))
            if self.env.cr.fetchone() is None:
                raise ValidationError('A synthetic staging campaign is required.')
            campaign = self.env[_CAMPAIGN].browse(campaign_id).exists()
            campaign.invalidate_recordset(['write_date'])
            if (not campaign or campaign.environment != 'stg' or campaign.state != 'draft'
                    or not campaign.version.startswith('rehearsal-')
                    or not campaign.body_text.startswith(_REHEARSAL_MARKER)):
                raise ValidationError('Only a labelled staging rehearsal may be published.')
            rows = self.env[_RECIPIENT].search([('campaign_id', '=', campaign_id)], order='id')
            if not 0 < len(rows) <= 20:
                raise ValidationError('A small synthetic recipient audience is required.')
            for row in rows:
                if (not _SYNTHETIC_KEY.fullmatch(row.recipient_key)
                        or (row.destination_snapshot and not _SYNTHETIC_ADDRESS.fullmatch(
                            row.destination_snapshot))):
                    raise ValidationError('Only reserved synthetic recipients may be published.')
            candidate = campaign._llm_candidate_preview(
                before, after, now=int(time.time()), not_before=not_before)
            payload_sha256 = hashlib.sha256(candidate.payload_json.encode('utf-8')).hexdigest()
            self.env.cr.execute(
                f'''INSERT INTO {_PUBLICATION_TABLE}
                    (campaign_id, campaign_fingerprint, environment,
                     public_payload_json, payload_sha256, create_uid, write_uid,
                     create_date, write_date)
                    VALUES (%s, %s, 'stg', %s, %s, %s, %s,
                            CURRENT_TIMESTAMP AT TIME ZONE 'UTC',
                            CURRENT_TIMESTAMP AT TIME ZONE 'UTC') RETURNING id''',
                (campaign.id, campaign.fingerprint, candidate.payload_json,
                 payload_sha256, self.env.uid, self.env.uid))
            publication_id = self.env.cr.fetchone()[0]
            _insert_rehearsal_outbox(
                self.env.cr, publication_id, campaign, rows, payload_sha256, self.env.uid)
        return self.browse(publication_id)

    @api.model
    def _llm_public_notices(self):
        """Private read path for authenticated users, independent of AI activation."""
        config = self.env['ir.config_parameter'].sudo()
        if config.get_param(_READ_FLAG) != '1':
            return []
        runtime_env = config.get_param(_RUNTIME_ENV)
        if runtime_env not in ('stg', 'prod'):
            return []
        publications = self.sudo().search([
            ('environment', '=', runtime_env), ('withdrawn_at', '=', False),
        ], order='id desc', limit=10)
        public_rows = []
        for record in publications:
            campaign = record.campaign_id
            raw = record.public_payload_json
            if (not campaign or campaign.environment != runtime_env
                    or campaign.fingerprint != record.campaign_fingerprint
                    or type(raw) is not str or len(raw) > 1048576
                    or hashlib.sha256(raw.encode('utf-8')).hexdigest() != record.payload_sha256):
                return []
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                return []
            if (type(payload) is not dict or payload.get('version') != campaign.version
                    or payload.get('body_text') != campaign.body_text):
                return []
            public_rows.append(payload)
        return project_public_notices(public_rows)
