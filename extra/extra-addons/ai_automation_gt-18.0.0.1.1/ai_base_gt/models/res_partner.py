from odoo import api, fields, models


class ResPartner(models.Model):
    _inherit = 'res.partner'

    ai_assistant_ids = fields.One2many('ai.assistant', 'partner_id', string="AI Assistants")
    is_ai = fields.Boolean('Is AI', compute='_compute_is_ai', store=True, index=True)

    @api.depends('ai_assistant_ids')
    def _compute_is_ai(self):
        for r in self:
            r.is_ai = bool(r.ai_assistant_ids)
