"""Dormant public notice rows. No approval, publication or withdrawal writer.

The existing draft campaign is never rendered directly. A later reviewed
publisher must introduce a controlled insert/withdraw path and independent
visibility evidence; these rows alone do not start the seven-day clock.
"""
import hashlib
import json

from odoo import api, fields, models
from odoo.exceptions import AccessError

from ..services.llm_price_notice_public import project_public_notices


_CAMPAIGN = 'woow_paas_platform.llm_price_notice_campaign'
_PUBLICATION = 'woow_paas_platform.llm_price_notice_publication'
_READ_FLAG = 'llm_price_notice_public_read_enabled'
_RUNTIME_ENV = 'llm_price_notice_runtime_environment'


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
