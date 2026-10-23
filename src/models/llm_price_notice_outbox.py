"""Immutable synthetic rehearsal obligations; no delivery or pricing authority.

Only the staging rehearsal publisher inserts these rows. A later reviewed
dispatcher must establish its own committed binding before any SMTP attempt.
"""
from odoo import api, fields, models
from odoo.exceptions import AccessError


class LlmPriceNoticeOutbox(models.Model):
    _name = 'woow_paas_platform.llm_price_notice_outbox'
    _description = 'AI price notice synthetic rehearsal outbox'
    _rec_name = 'recipient_key'
    _order = 'id'

    publication_id = fields.Many2one(
        'woow_paas_platform.llm_price_notice_publication', required=True,
        readonly=True, copy=False, ondelete='restrict', index=True)
    recipient_id = fields.Many2one(
        'woow_paas_platform.llm_price_notice_recipient', required=True,
        readonly=True, copy=False, ondelete='restrict', index=True)
    recipient_key = fields.Char(required=True, readonly=True, copy=False)
    destination_snapshot = fields.Char(readonly=True, copy=False, trim=False,
                                      groups='base.group_system')
    destination_revision = fields.Integer(required=True, readonly=True, copy=False)
    campaign_fingerprint = fields.Char(required=True, readonly=True, copy=False)
    payload_sha256 = fields.Char(required=True, readonly=True, copy=False)
    state = fields.Selection([
        ('pending', 'Synthetic destination awaiting separate dispatch'),
        ('missing_address', 'Unresolved synthetic destination'),
    ], required=True, readonly=True, copy=False)

    _sql_constraints = [  # noqa: RUF012 - Odoo declarative metadata
        ('publication_recipient_uniq', 'UNIQUE(publication_id, recipient_key)',
         'Each synthetic recipient may have only one outbox obligation.'),
        ('recipient_once', 'UNIQUE(recipient_id)',
         'A recipient snapshot may have only one outbox obligation.'),
        ('revision_positive', 'CHECK(destination_revision > 0)',
         'Destination revisions must be positive.'),
        ('destination_state',
         "CHECK((state = 'missing_address' AND destination_snapshot IS NULL) OR "
         "(state = 'pending' AND destination_snapshot IS NOT NULL "
         "AND length(btrim(destination_snapshot)) > 0))",
         'A missing address remains an unresolved obligation.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        raise AccessError('Price notice outbox has no public writer.')

    def write(self, vals):
        raise AccessError('Price notice outbox snapshots are immutable.')

    def unlink(self):
        raise AccessError('Price notice outbox obligations cannot be deleted.')

    def copy(self, default=None):
        raise AccessError('Price notice outbox obligations cannot be copied.')
