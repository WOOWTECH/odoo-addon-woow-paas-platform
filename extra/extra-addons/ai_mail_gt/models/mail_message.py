from odoo import models, fields


class MailMessage(models.Model):
    _inherit = 'mail.message'

    ai_message_ids = fields.Many2many('ai.message', 'ai_message_mail_message_rel', string="AI Messages")

    def _cleanup_side_records(self):
        super()._cleanup_side_records()
        self.ai_message_ids.unlink()
