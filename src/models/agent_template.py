from odoo import _, api, fields, models
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

    @api.constrains('project_ids')
    def _check_not_platform_default_when_bound(self):
        """The template configured as the platform-wide default
        (``ir.config_parameter['woow_paas_platform.default_agent_template_id']``)
        must stay project-unbound.

        A project-bound default is silently rejected by
        ``_platform_default_agent_template`` (cross-tenant MCP/skill leak guard,
        mirrored by ``_resolve_agent_template`` ④). That rejection has no error
        surface: it just makes the readiness gate's "valid default" condition
        False and the resolution fallback vanish, so auto-enable / default
        resolution quietly stop working for EVERY project cluster-wide. Failing
        loud at bind time keeps the seeded-default tenant-neutral invariant
        enforceable instead of leaving it to manifest as a silent outage.

        Not bypassed for superuser/admin on purpose: there is no legitimate path
        that binds a project to the configured default — the seed ships unbound,
        and any binder (UI, backend, sudo) is breaking the invariant. The
        reverse direction (selecting an already-bound template as the default)
        is guarded by the ``res.config.settings`` field domain.
        """
        default_raw = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_agent_template_id')
        if not default_raw:
            return
        try:
            default_id = int(default_raw)
        except (ValueError, TypeError):
            return
        for tmpl in self:
            if tmpl.id == default_id and tmpl.project_ids:
                raise ValidationError(
                    "Agent template '%s' is the platform-wide default "
                    "(Settings → Default Agent Template) and must stay "
                    "project-unbound: binding it to a project turns it into a "
                    "tenant-bound template, which silently disables the "
                    "agent-runtime default fallback for every project. Bind a "
                    "different template to this project, or change the platform "
                    "default first."
                    % (tmpl.name or tmpl.id)
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

    @api.constrains('skill_ids', 'project_ids')
    def _check_skill_subset(self):
        """Bound skills must be a subset of (project user-scope skills ∪
        scope='system'). Prevents a template from referencing user-scope skill
        rows from projects it isn't bound to.
        """
        Skill = self.env['woow_paas_platform.skill']
        for t in self:
            if not t.skill_ids:
                continue
            allowed_user_scope = set(t.project_ids.mapped('skill_ids').ids)
            allowed_system = set(
                Skill.sudo().search([('scope', '=', 'system')]).ids
            )
            allowed = allowed_user_scope | allowed_system
            for skill in t.skill_ids:
                if skill.id not in allowed:
                    raise ValidationError(
                        "Skill '%s' is not bound to any of template "
                        "'%s' projects and is not system-scope."
                        % (skill.name, t.name or t.id)
                    )

    @api.constrains('skill_ids')
    def _check_no_duplicate_skill_name(self):
        """Reject binding two skills that share the same ``name``.

        An agent pod loads skills by name (the snapshot wire contract keys off
        ``name``, not id/version/scope), so two same-named bindings collide:
        the pod can only keep one and the choice is opaque to the admin. Fail
        loud at bind time instead of silently dropping one downstream.

        Checks the FULL ``skill_ids`` set per template (not a write delta) so a
        same-name pair introduced via either leg of an M2M write is caught.

        NOT bypassed for superuser/admin on purpose — same rationale as
        ``_check_not_platform_default_when_bound``: there is no legitimate path
        that binds two same-named skills to one template, so a sudo / backend /
        cron binder hitting this is breaking the invariant, not exercising a
        privileged shortcut. The snapshot builder still dedupes defensively for
        any legacy stock rows that predate this guard (those cannot be created
        through the ORM once this constraint is live).

        The message lists only the conflicting name(s) plus unbind guidance; it
        deliberately does not reveal which skill id/sha would win or how the
        downstream dedup resolves the tie.
        """
        for t in self:
            name_counts = {}
            for skill in t.skill_ids:
                name_counts[skill.name] = name_counts.get(skill.name, 0) + 1
            duplicates = sorted(
                name for name, count in name_counts.items() if count > 1)
            if duplicates:
                raise ValidationError(_(
                    "Agent template '%(tmpl)s' binds multiple skills with the "
                    "same name: %(names)s. An agent pod loads skills by name, "
                    "so two same-named bindings would collide. Unbind all but "
                    "one version (keep the one you want the pod to load).",
                    tmpl=t.name or t.id,
                    names=', '.join(duplicates),
                ))

    def write(self, vals):
        result = super().write(vals)
        # Binding change alters what loads into running pods → best-effort
        # push (#111 semantics: per-pod failure logged, never aborts).
        # Projects resolving via the manual M2O are included; the unbound
        # platform default is not walked (it binds system resources only —
        # skill-state pushes already cover system-wide changes; next chat
        # attach refresh is the fallback).
        if 'skill_ids' in vals or 'mcp_server_ids' in vals:
            Session = self.env['woow_paas_platform.agent_session']
            for template in self:
                projects = template.project_ids | self.env[
                    'project.project'].sudo().search([
                        ('agent_template_id', '=', template.id)])
                for project in projects:
                    if 'skill_ids' in vals:
                        Session._push_snapshot_to_running_pods(
                            'skill', 'user', project.id)
                    if 'mcp_server_ids' in vals:
                        Session._push_snapshot_to_running_pods(
                            'mcp', 'user', project.id)
        return result
