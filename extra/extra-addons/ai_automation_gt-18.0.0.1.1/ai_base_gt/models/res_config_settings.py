from odoo import models, fields


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    group_ai_advanced_setup_permissions = fields.Boolean(
        string="Advanced Setup",
        implied_group='ai_base_gt.group_ai_advanced_setup_permissions',
    )

    def set_values(self):
        had_advanced = self.env.user.has_group('ai_base_gt.group_ai_advanced_setup_permissions')
        super().set_values()
        if had_advanced and not self.group_ai_advanced_setup_permissions:
            self.env['ai.assistant.data.source'].sudo().search([])._compute_allow_crud()
