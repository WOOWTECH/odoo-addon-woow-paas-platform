"""Draft-only price notice ledger. No approval, delivery or activation authority.

Rows are immutable snapshots, including unresolved recipient obligations. A
correction requires a new campaign version with its own complete audience.
Fingerprints identify draft content; they are not trusted publication receipts.
This ledger deliberately does not import or enable the dormant notice checker.
"""
import hashlib
import json
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
_RECIPIENT = 'woow_paas_platform.llm_price_notice_recipient'
_VERSION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}\Z')
_KEY = re.compile(r'(?:paas|legacy):[A-Za-z0-9_-]{1,64}\Z')
_SHA256 = re.compile(r'[a-f0-9]{64}\Z')


def _require_admin(records):
    if not (records.env.su or records.env.user.has_group('base.group_system')):
        raise AccessError(_('Only system administrators may create price notice drafts.'))


def _text(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


class LlmPriceNoticeCampaign(models.Model):
    _name = _CAMPAIGN
    _description = 'AI price notice draft campaign'
    _rec_name = 'version'
    _order = 'id desc'

    version = fields.Char(required=True, readonly=True, copy=False, index=True)
    body_text = fields.Text(required=True, readonly=True, copy=False)
    catalog_fingerprint = fields.Char(required=True, readonly=True, copy=False)
    fingerprint = fields.Char(required=True, readonly=True, copy=False)
    environment = fields.Selection(
        [('stg', 'Staging'), ('prod', 'Production')], required=True, readonly=True, copy=False)
    state = fields.Selection(
        [('draft', 'Draft')], required=True, readonly=True, default='draft', copy=False)

    _sql_constraints = [  # noqa: RUF012 - Odoo declarative metadata
        ('version_env_uniq', 'UNIQUE(environment, version)',
         'A campaign version must be unique within its environment.'),
        ('draft_only', "CHECK(state = 'draft')", 'Campaigns must remain drafts.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        _require_admin(self)
        prepared = []
        allowed = {'version', 'body_text', 'catalog_fingerprint', 'environment'}
        for vals in vals_list:
            if set(vals) != allowed:
                raise ValidationError(_('Supply only the complete draft campaign snapshot.'))
            if (not _text(vals['version'], _VERSION)
                    or not _text(vals['catalog_fingerprint'], _SHA256)
                    or vals['environment'] not in ('stg', 'prod')
                    or not isinstance(vals['body_text'], str)
                    or not vals['body_text'].strip()):
                raise ValidationError(_('Invalid draft campaign snapshot.'))
            # Bind the exact body, catalog identifier, environment and version.
            payload = json.dumps(vals, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
            prepared.append(dict(vals, state='draft', fingerprint=hashlib.sha256(
                payload.encode('utf-8')).hexdigest()))
        # Caller defaults cannot inject states, metadata or future evidence fields.
        return super(LlmPriceNoticeCampaign, self.with_context({})).create(prepared)

    def write(self, vals):
        raise AccessError(_('Price notice campaign snapshots are immutable.'))

    def unlink(self):
        raise AccessError(_('Price notice campaign obligations cannot be deleted.'))

    def copy(self, default=None):
        raise AccessError(_('Create an explicit new campaign version and audience.'))


class LlmPriceNoticeRecipient(models.Model):
    _name = _RECIPIENT
    _description = 'AI price notice recipient obligation'
    _rec_name = 'recipient_key'
    _order = 'id'

    campaign_id = fields.Many2one(
        _CAMPAIGN, required=True, readonly=True, copy=False, ondelete='restrict', index=True)
    recipient_key = fields.Char(required=True, readonly=True, copy=False, index=True)
    destination_snapshot = fields.Char(readonly=True, copy=False, trim=False,
                                      groups='base.group_system')
    destination_revision = fields.Integer(required=True, readonly=True, copy=False)
    state = fields.Selection([
        ('pending', 'Unresolved: destination recorded'),
        ('missing_address', 'Unresolved: missing address'),
    ], required=True, readonly=True, copy=False)

    _sql_constraints = [  # noqa: RUF012 - Odoo declarative metadata
        ('recipient_uniq', 'UNIQUE(campaign_id, recipient_key)',
         'A recipient obligation already exists for this campaign.'),
        ('revision_positive', 'CHECK(destination_revision > 0)',
         'Destination revisions must be positive.'),
        ('destination_state',
         ("CHECK((state = 'missing_address' AND destination_snapshot IS NULL) OR "
          "(state = 'pending' AND length(btrim(destination_snapshot)) > 0 "
          "AND destination_snapshot IS NOT NULL))"),
         'A missing address must remain an unresolved obligation.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        _require_admin(self)
        prepared = []
        required = {'campaign_id', 'recipient_key', 'destination_revision'}
        for vals in vals_list:
            if not required <= set(vals) or set(vals) - (required | {'destination_snapshot'}):
                raise ValidationError(_('Supply only the recipient obligation snapshot.'))
            if (type(vals['campaign_id']) is not int or vals['campaign_id'] <= 0
                    or not _text(vals['recipient_key'], _KEY)
                    or type(vals['destination_revision']) is not int
                    or not 0 < vals['destination_revision'] <= 2147483647):
                raise ValidationError(_('Invalid recipient obligation snapshot.'))
            campaign = self.env[_CAMPAIGN].browse(vals['campaign_id']).exists()
            if not campaign or campaign.state != 'draft':
                raise ValidationError(_('An existing draft campaign is required.'))
            destination = vals.get('destination_snapshot', False)
            if destination is not False and destination is not None and not isinstance(destination, str):
                raise ValidationError(_('Invalid destination snapshot.'))
            # Keep the supplied snapshot verbatim; blank input never drops a recipient.
            destination = destination if destination and destination.strip() else False
            prepared.append(dict(vals, destination_snapshot=destination,
                                 state='pending' if destination else 'missing_address'))
        return super(LlmPriceNoticeRecipient, self.with_context({})).create(prepared)

    def write(self, vals):
        raise AccessError(_('Recipient identity and destination snapshots are immutable.'))

    def unlink(self):
        raise AccessError(_('Price notice recipient obligations cannot be deleted.'))

    def copy(self, default=None):
        raise AccessError(_('Create an explicit recipient obligation for a new campaign.'))
