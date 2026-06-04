from odoo import SUPERUSER_ID, api, fields, models


class ProjectProject(models.Model):
    _inherit = 'project.project'

    # ------------------------------------------------------------------ #
    # M:N cloud_service binding (Phase A — agent-runtime-pod epic).      #
    # ------------------------------------------------------------------ #
    cloud_service_ids = fields.Many2many(
        comodel_name='woow_paas_platform.cloud_service',
        relation='project_cloud_service_rel',
        column1='project_id',
        column2='cloud_service_id',
        string='Cloud Services',
    )

    # Legacy M2O kept as a stored compute over cloud_service_ids[:1] (id ASC).
    # Backward-compat for callers that still read / write `cloud_service_id`;
    # writes flow back into the M:N via the inverse. The SQL column stays
    # populated so post-migrate backfill can seed cloud_service_ids from
    # existing rows.
    cloud_service_id = fields.Many2one(
        comodel_name='woow_paas_platform.cloud_service',
        string='Cloud Service (deprecated)',
        compute='_compute_cloud_service_id',
        inverse='_inverse_cloud_service_id',
        store=True,
        index=True,
        help='DEPRECATED — read returns cloud_service_ids[:1] (lowest id); '
             'write syncs the value into cloud_service_ids.',
    )

    # Aggregated workspace set across all bound cloud_services.
    workspace_ids = fields.Many2many(
        comodel_name='woow_paas_platform.workspace',
        compute='_compute_workspace_ids',
        store=True,
        string='Workspaces',
    )
    # Primary workspace = workspace of cloud_service_id (first by id ASC);
    # kept stored for ir.rule / view backward compat.
    workspace_id = fields.Many2one(
        comodel_name='woow_paas_platform.workspace',
        string='Workspace (primary)',
        compute='_compute_workspace_id',
        store=True,
    )

    mcp_server_ids = fields.One2many(
        'woow_paas_platform.mcp_server',
        'project_id',
        string='MCP Servers',
    )

    skill_ids = fields.One2many(
        'woow_paas_platform.skill',
        'project_id',
        string='Skills',
    )

    # Phase A additive fields.
    external_mcp_allowed = fields.Boolean(
        string='External MCP Egress Allowed',
        default=False,
        help='Admin gate for NetworkPolicy egress to external MCP endpoints '
             '(design §6.3 Q1 hybrid scheme). Conservative default — closed.',
    )
    use_agent_runtime = fields.Boolean(
        string='Use Agent Runtime Pod',
        default=False,
        help='Per-project toggle for the dual-mode rollout window '
             '(Phase D-F). Dormant in Phase A.',
    )
    agent_template_id = fields.Many2one(
        comodel_name='woow_paas_platform.agent_template',
        string='Agent Template',
        ondelete='set null',
    )
    project_access_ids = fields.One2many(
        'woow_paas_platform.project_access',
        'project_id',
        string='Project Member Access',
    )

    # ------------------------------------------------------------------ #
    # Computes                                                            #
    # ------------------------------------------------------------------ #
    @api.depends('cloud_service_ids')
    def _compute_cloud_service_id(self):
        for project in self:
            project.cloud_service_id = project.cloud_service_ids.sorted('id')[:1]

    def _inverse_cloud_service_id(self):
        """Legacy single-FK write semantics: setting the deprecated M2O
        replaces the M:N set (write rebinds the project to that single
        service, mirroring the pre-Phase-A 1:1 contract). Code that wants
        multi-bind must use ``cloud_service_ids`` directly.
        """
        for project in self:
            svc = project.cloud_service_id
            if svc:
                project.cloud_service_ids = [(6, 0, [svc.id])]
            else:
                project.cloud_service_ids = [(5, 0, 0)]

    @api.depends('cloud_service_ids.workspace_id')
    def _compute_workspace_ids(self):
        for project in self:
            project.workspace_ids = project.cloud_service_ids.mapped('workspace_id')

    @api.depends('cloud_service_id.workspace_id')
    def _compute_workspace_id(self):
        for project in self:
            project.workspace_id = project.cloud_service_id.workspace_id

    # ------------------------------------------------------------------ #
    # project_access bootstrap on create                                  #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        projects = super().create(vals_list)
        if 'woow_paas_platform.project_access' not in self.env.registry:
            return projects
        # Resolve the owner-partner to bootstrap (HC-2
        # [[constraint-input-trust-expansion]]): authority comes from a trusted
        # source, never inferred from an attacker-controllable context value.
        # - SUPERUSER path (sudo: paas-operator provisioning / cron / install /
        #   migration): only an EXPLICIT ``bootstrap_owner_partner_id`` hint —
        #   set by a trusted caller naming the real human owner — bootstraps;
        #   a bare SUPERUSER create with NO hint does NOT auto-own.
        # - Normal self-service create: the authenticated user owns the project.
        #   The hint is NOT honoured here — a non-trusted caller must not be able
        #   to assign ownership to an arbitrary partner via the context.
        #
        # ⚠ SET-SITE INVARIANT (for whoever adds the FIRST writer of this hint):
        #   There is NO production set-site today (only tests set it). When a
        #   future phase wires operator provisioning to bootstrap an owner, the
        #   hint value MUST be server-authoritative — derived from a provisioning
        #   record or the authenticated user's own partner_id — and MUST NEVER be
        #   filled from request `kw`/`params`/inbound `context`. Passing an
        #   attacker-controllable value here degrades bootstrap convenience into an
        #   ownership-assignment primitive (B1 relapse). See
        #   docs/decisions/patterns/constraint-input-trust-expansion.md
        #   §Forward-looking invariant.
        if self.env.uid == SUPERUSER_ID:
            hint_partner_id = self.env.context.get('bootstrap_owner_partner_id')
            if not hint_partner_id:
                return projects
            partner = self.env['res.partner'].browse(hint_partner_id).exists()
        else:
            partner = self.env.user.partner_id
        if not partner:
            return projects
        PAccess = self.env['woow_paas_platform.project_access']
        for project in projects:
            already = PAccess.sudo().search_count([
                ('project_id', '=', project.id),
                ('partner_id', '=', partner.id),
            ])
            if not already:
                PAccess.sudo().create({
                    'project_id': project.id,
                    'partner_id': partner.id,
                    'role': 'owner',
                })
        return projects

    def _portal_partner_can_access(self, partner):
        """Portal-leg project predicate (two-layer-acl-lockstep Layer 2).

        Mirror of ``project.project_project_rule_portal`` PLUS a deliberate
        task-leg extension: a partner shared on ANY active task of this
        project may read the project's agent status / chat with the project
        agent on that task. Required because chat & agent status are
        project-scoped while native share can be task-scoped. This widens
        ONLY the controller-computed agent endpoints — the project record
        itself stays invisible (native ir.rule untouched). See design spec
        §安全模型.
        """
        self.ensure_one()
        project = self.sudo()
        if project.privacy_visibility != 'portal':
            return False
        commercial = partner.commercial_partner_id
        allowed = self.env['res.partner'].sudo().search(
            [('id', 'child_of', commercial.id)])
        if project.message_partner_ids & allowed:
            return True
        if project.collaborator_ids.filtered(
                lambda c: c.partner_id in allowed and not c.limited_access):
            return True
        return bool(self.env['project.task'].sudo().search_count([
            ('project_id', '=', project.id),
            ('active', '=', True),
            ('message_partner_ids', 'in', allowed.ids),
        ]))
