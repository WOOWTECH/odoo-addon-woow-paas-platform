import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ProjectTask(models.Model):
    _inherit = 'project.task'

    chat_enabled = fields.Boolean(
        string='Chat Enabled',
        default=False,
        help='Enable group chat for this task',
    )
    channel_id = fields.Many2one(
        'discuss.channel',
        string='Chat Channel',
        ondelete='set null',
        help='Discuss channel for task group chat',
    )
    ai_auto_reply = fields.Boolean(
        string='AI Auto Reply',
        default=True,
        help='Automatically trigger AI reply when messages are posted',
    )

    def _create_chat_channel(self):
        """Lazily create a discuss.channel for the task chat.

        Creates a channel named 'Task Chat: <task name>' and adds the
        task assigned users as channel members.  The channel reference is
        stored on the task so subsequent calls are idempotent.
        """
        self.ensure_one()
        if self.channel_id:
            return self.channel_id

        # Build the member list from assigned users
        member_ids = []
        if self.user_ids:
            member_ids = [
                (0, 0, {'partner_id': user.partner_id.id})
                for user in self.user_ids
                if user.partner_id
            ]

        channel = self.env['discuss.channel'].sudo().create({
            'name': f'Task Chat: {self.name}',
            'channel_type': 'channel',
            'channel_member_ids': member_ids,
        })

        self.sudo().write({'channel_id': channel.id})
        _logger.info(
            'Created chat channel %s (id=%s) for task %s (id=%s)',
            channel.name, channel.id, self.name, self.id,
        )
        return channel

    def write(self, vals):
        result = super().write(vals)
        # When chat_enabled is turned on, lazily create the channel
        if vals.get('chat_enabled'):
            for task in self:
                if not task.channel_id:
                    task._create_chat_channel()
        return result
