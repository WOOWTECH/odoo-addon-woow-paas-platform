from odoo import models, fields, Command
from odoo.addons.ai_base_gt.models.tools import after_commit


class AIMessage(models.Model):
    _inherit = 'ai.message'

    mail_message_ids = fields.Many2many('mail.message', 'ai_message_mail_message_rel', string="Mail Messages")

    def _post_create_hook(self):
        super()._post_create_hook()
        for message in self:
            mail_thread, mail_message = self.sudo()._get_thread_and_message_from_context()
            if not mail_thread or not mail_message:
                continue
            if message.message_type == 'prompt':
                mail_message.ai_message_ids = [Command.link(message.id)]
                message._mail_message_replace_record_tag(mail_thread, mail_message)
            elif message.message_type == 'response' and message.content:
                message._post_message_after_commit(mail_thread, mail_message)

    def _mail_message_replace_record_tag(self, mail_thread, mail_message):
        new_body = self._replace_record_tag(mail_message.body, self.message_record_ids)
        mail_thread._message_update_content(mail_message, new_body, mark_as_edited=False)
        mail_thread._message_update_content_after_commit(mail_message.id, new_body, mark_as_edited=False)

    @after_commit(wait=True)
    def _post_message_after_commit(self, mail_thread, mail_message):
        """Post the AI response message to the mail thread"""
        if mail_thread._name != 'discuss.channel':
            # To notify all participants in the prompt message
            partner_ids = (mail_message.author_id | mail_message.partner_ids).ids
        else:
            # In discuss channel, all participants are notified by default
            partner_ids = None

        # Post AI response to the thread
        response_msg = mail_thread.with_user(self.thread_id.ai_user_id).with_context(
            mail_create_nosubscribe=True
        ).message_post(
            body=self.content_html,
            author_id=self.author_id.id,
            message_type=mail_message.message_type,
            subtype_id=mail_message.subtype_id.id,
            partner_ids=partner_ids,
        )
        response_msg.ai_message_ids = [Command.link(self.id)]

    def _get_thread_and_message_from_context(self):
        mail_thread_model = self.env.context.get('mail_thread_model')
        mail_thread_id = self.env.context.get('mail_thread_id')
        mail_message_id = self.env.context.get('mail_message_id')

        if not all([mail_thread_model, mail_thread_id, mail_message_id]):
            return None, None

        return (
            self.env[mail_thread_model].browse(mail_thread_id),
            self.env['mail.message'].browse(mail_message_id)
        )
