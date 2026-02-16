from odoo import api, fields, models, _
from odoo.tools import html2plaintext


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    channel_type = fields.Selection(selection_add=[('ai_chat', 'AI Conversation')],
                                    ondelete={'ai_chat': 'set chat'})

    def _message_post_after_hook(self, message, msg_vals):
        if self.channel_type == 'ai_chat' and not (self.message_ids - message):
            self.name = html2plaintext(message.body)[:100]
        return super()._message_post_after_hook(message, msg_vals)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('channel_type') == 'chat':
                partner_ids_cmd = vals.get('channel_partner_ids') or []
                partner_ids = [cmd[1] for cmd in partner_ids_cmd if cmd[0] == 4]
                partner_ids += [cmd[2] for cmd in partner_ids_cmd if cmd[0] == 6]

                member_ids_cmd = vals.get('channel_member_ids', [])
                partner_ids += [cmd[2]['partner_id'] for cmd in member_ids_cmd if cmd[0] == 0]

                chat_partners = self.env['res.partner'].browse(set(partner_ids)) - self.env.user.partner_id
                if chat_partners.filtered('is_ai'):
                    vals['channel_type'] = 'ai_chat'
                    vals['name'] = _('# New conversation')

        return super().create(vals_list)
