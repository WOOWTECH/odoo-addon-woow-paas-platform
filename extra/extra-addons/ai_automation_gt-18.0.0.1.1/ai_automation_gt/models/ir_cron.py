from odoo import models, fields, api, _


class IrCron(models.Model):
    _inherit = 'ir.cron'

    cron_state = fields.Selection([
        ('code', 'Code'),
        ('ai_action', 'AI Action')
    ], string='Action Type', compute='_compute_cron_state', inverse='_inverse_cron_state')

    @api.depends('state')
    def _compute_cron_state(self):
        for cron in self:
            cron.cron_state = cron.state

    @api.onchange('cron_state')
    def _inverse_cron_state(self):
        for cron in self:
            cron.state = cron.cron_state
