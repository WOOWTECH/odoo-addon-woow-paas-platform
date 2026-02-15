from markupsafe import escape
from odoo import models
from odoo.addons.ai_base_gt.models.tools import after_commit


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    def _message_post_after_hook(self, message, msg_vals):
        super()._message_post_after_hook(message, msg_vals)
        self.env['mail.ai'].sudo(False)._apply_logic(self, message, msg_vals)

    def _message_update_content(self, message, body, **kwargs):
        if not kwargs.get('mark_as_edited', True):
            if body is not None:
                # Hack to avoid marking the message as edited: write the new body first
                # then call the super method with body is None
                message.write({'body': escape(body)})
                return super()._message_update_content(message, None, **kwargs)
        return super()._message_update_content(message, body, **kwargs)

    @after_commit(wait=False)
    def _message_update_content_after_commit(self, mail_message_id, new_body, **kwargs):
        mail_message = self.env['mail.message'].browse(mail_message_id)
        self._message_update_content(mail_message, new_body, **kwargs)
