from odoo import api, fields, models
from odoo.exceptions import ValidationError


class AgentTemplate(models.Model):
    _name = 'woow_paas_platform.agent_template'
    _description = 'Agent Runtime Template (system prompt + tool binding)'
    _order = 'is_public desc, name'

    name = fields.Char(
        string='Template Name',
        required=True,
        index=True,
    )
    title = fields.Char(string='Display Title')
    description = fields.Text()

    system_prompt = fields.Text(
        string='System Prompt',
        help='Base system prompt prepended to all LangGraph threads using '
             'this template.',
    )
    model_config_id = fields.Many2one(
        'ai.config',
        string='LLM Provider Config',
        ondelete='restrict',
    )
    model_overrides = fields.Text(
        string='Model Param Overrides (JSON)',
        help='Optional JSON overriding ai.config defaults '
             '({"model": "...", "temperature": 0.7, "max_tokens": 4096}).',
    )

    skill_ids = fields.Many2many(
        'woow_paas_platform.skill',
        relation='agent_template_skill_rel',
        column1='template_id',
        column2='skill_id',
        string='Bound Skills',
    )
    mcp_server_ids = fields.Many2many(
        'woow_paas_platform.mcp_server',
        relation='agent_template_mcp_server_rel',
        column1='template_id',
        column2='mcp_server_id',
        string='Bound MCP Servers',
    )
    project_ids = fields.Many2many(
        'project.project',
        relation='agent_template_project_rel',
        column1='template_id',
        column2='project_id',
        string='Bound Projects',
    )
    is_public = fields.Boolean(
        string='Public (System) Template',
        default=False,
    )

    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('published', 'Published'),
            ('deprecated', 'Deprecated'),
            ('archived', 'Archived'),
        ],
        default='draft',
        required=True,
        index=True,
    )

    created_by_uid = fields.Many2one(
        'res.users',
        readonly=True,
        default=lambda self: self.env.uid,
    )
    created_at = fields.Datetime(
        readonly=True,
        default=fields.Datetime.now,
    )

    @api.constrains('is_public', 'project_ids', 'state')
    def _check_visibility_consistency(self):
        """Private templates (is_public=False) without bound projects cannot
        leave draft state — they are orphan / unaddressable.
        """
        for t in self:
            if not t.is_public and not t.project_ids and t.state != 'draft':
                raise ValidationError(
                    "Private template '%s' must bind at least one project "
                    "before publishing." % (t.name or t.id)
                )

    @api.constrains('project_ids')
    def _check_project_ids_grantor_authority(self):
        """HC-3 fix — cross-project write-injection guard.

        Attack: project A admin creates template T bound to A, then writes
        ``T.project_ids = [(4, B)]`` to extend to a project they lack
        admin/owner role on. ir.rule's M2M walk passes per-row because A is
        still in T.project_ids — the new B leg is silently allowed.

        Constraint: every project bound to T must have ``env.user`` in
        project_access with role IN ('admin', 'owner'). Sudo / SUPERUSER /
        ``base.group_system`` bypass for legitimate seeding paths (cron,
        migration, paas-operator webhook, backend admin curation).
        """
        if self.env.is_superuser() or self.env.user.has_group('base.group_system'):
            return
        user = self.env.user
        for tmpl in self:
            for proj in tmpl.project_ids:
                authorized = proj.project_access_ids.filtered(
                    lambda r: r.user_id == user and r.role in ('owner', 'admin')
                )
                if not authorized:
                    raise ValidationError(
                        "Cannot bind agent template '%s' to project '%s': "
                        "current user must have owner/admin role on every "
                        "bound project."
                        % (tmpl.name or tmpl.id, proj.display_name or proj.id)
                    )

    @api.constrains('mcp_server_ids', 'project_ids')
    def _check_mcp_subset(self):
        """Bound MCP servers must be a subset of (project user-scope ∪
        scope='system'). Prevents a template from referencing user-scope MCP
        rows from projects it isn't bound to.
        """
        Mcp = self.env['woow_paas_platform.mcp_server']
        for t in self:
            if not t.mcp_server_ids:
                continue
            allowed_user_scope = set(t.project_ids.mapped('mcp_server_ids').ids)
            allowed_system = set(
                Mcp.sudo().search([('scope', '=', 'system')]).ids
            )
            allowed = allowed_user_scope | allowed_system
            for mcp in t.mcp_server_ids:
                if mcp.id not in allowed:
                    raise ValidationError(
                        "MCP server '%s' is not bound to any of template "
                        "'%s' projects and is not system-scope."
                        % (mcp.name, t.name or t.id)
                    )
