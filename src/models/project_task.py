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

    # ------------------------------------------------------------------ #
    #  Linked Cloud Services (D2=A / D6=C / D7=A)                         #
    # ------------------------------------------------------------------ #
    workspace_id = fields.Many2one(
        'woow_paas_platform.workspace',
        related='project_id.workspace_id',
        store=True,
        index=True,
        string='Workspace',
    )
    primary_cloud_service_id = fields.Many2one(
        'woow_paas_platform.cloud_service',
        related='project_id.cloud_service_id',
        store=True,
        index=True,
        string='Primary Cloud Service',
    )
    extra_cloud_service_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_service',
        relation='project_task_cloud_service_rel',
        column1='task_id',
        column2='cloud_service_id',
        domain="[('workspace_id', '=', workspace_id)]",
        string='Extra Linked Services',
    )
    incompatible_extra_cloud_service_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_service',
        compute='_compute_incompatible_extra_cloud_service_ids',
        string='Incompatible Extra Services',
        help='Extras whose workspace no longer matches this task — UI flags '
             'them with a warning chip; server keeps them on cross-project moves.',
    )

    @api.depends('extra_cloud_service_ids.workspace_id', 'workspace_id')
    def _compute_incompatible_extra_cloud_service_ids(self):
        for task in self:
            ws = task.workspace_id
            if not ws:
                task.incompatible_extra_cloud_service_ids = task.extra_cloud_service_ids
            else:
                task.incompatible_extra_cloud_service_ids = (
                    task.extra_cloud_service_ids.filtered(
                        lambda s: s.workspace_id != ws
                    )
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
