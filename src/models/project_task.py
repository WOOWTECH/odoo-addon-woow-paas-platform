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
    #  Linked Cloud Services (Phase A M:N hybrid — design §7.0.3)        #
    # ------------------------------------------------------------------ #
    workspace_id = fields.Many2one(
        'woow_paas_platform.workspace',
        related='project_id.workspace_id',
        store=True,
        index=True,
        string='Workspace',
    )
    # Independent stored M2O with computed default (id ASC over the project's
    # cloud_service_ids). `readonly=False` makes the compute act as a default —
    # explicit writes persist; drift resets only when the current pick is no
    # longer in the project's bound set.
    primary_cloud_service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        compute='_compute_primary_cloud_service_id',
        store=True,
        readonly=False,
        index=True,
        string='Primary Cloud Service',
        domain="[('id', 'in', project_id.cloud_service_ids.ids)]",
    )
    extra_cloud_service_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_service',
        relation='project_task_cloud_service_rel',
        column1='task_id',
        column2='cloud_service_id',
        # Limit to services bound to the task's project (M:N). The
        # secondary ``workspace_id in project_id.workspace_ids.ids`` clause
        # is functionally redundant — every member of cloud_service_ids
        # already has its workspace in workspace_ids — but keeps the
        # UI candidate list workspace-scoped for the legacy 1:N
        # ``test_task_service_extras`` regression.
        domain="[('id', 'in', project_id.cloud_service_ids.ids), "
               "('workspace_id', 'in', project_id.workspace_ids.ids)]",
        string='Extra Linked Services',
    )
    incompatible_extra_cloud_service_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_service',
        compute='_compute_incompatible_extra_cloud_service_ids',
        string='Incompatible Extra Services',
        help='Extras whose workspace no longer matches this task — UI flags '
             'them with a warning chip; server keeps them on cross-project moves.',
    )

    @api.depends('project_id.cloud_service_ids')
    def _compute_primary_cloud_service_id(self):
        """Default the primary to the first bound cloud_service (id ASC) and
        reset only when the existing pick drifted out of the project's set.

        - Empty / new task → primary = project.cloud_service_ids[:1].
        - User explicit write → respected (compute deps unchanged → no rerun).
        - project.cloud_service_ids changes:
            * existing primary still ∈ new set → preserved (user choice intact);
            * otherwise → reset to first.
        """
        for task in self:
            bound = task.project_id.cloud_service_ids
            current = task.primary_cloud_service_id
            if current and current in bound:
                # preserve — do not reassign
                continue
            task.primary_cloud_service_id = bound.sorted('id')[:1]

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

    def _portal_partner_can_access(self, partner):
        """Portal-leg access predicate — Python mirror of the native ir.rule
        ``project.project_task_rule_portal`` (two-layer-acl-lockstep Layer 2).

        Layer 1 (ir.rule, native, UNTOUCHED by this addon):
            project_id.privacy_visibility = 'portal' AND active
            AND (message_partner_ids child_of user.commercial_partner
                 OR project collaborator(limited_access=False) child_of it)

        Any change here MUST stay in lock-step with that rule (and vice
        versa) — silent divergence re-opens the IDOR surface. Positive-only:
        portal users must never fall through to the internal fail-open legs
        in the controller helpers (F4).
        """
        self.ensure_one()
        task = self.sudo()
        project = task.project_id
        if not project or project.privacy_visibility != 'portal' or not task.active:
            return False
        commercial = partner.commercial_partner_id
        allowed = self.env['res.partner'].sudo().search(
            [('id', 'child_of', commercial.id)])
        if task.message_partner_ids & allowed:
            return True
        return bool(project.collaborator_ids.filtered(
            lambda c: c.partner_id in allowed and not c.limited_access))
