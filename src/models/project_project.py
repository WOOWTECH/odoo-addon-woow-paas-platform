from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError


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

    # ------------------------------------------------------------------ #
    # Skill binding UX（spec 2026-06-05）— resolved template 顯示 + fork  #
    # ------------------------------------------------------------------ #
    resolved_agent_template_id = fields.Many2one(
        'woow_paas_platform.agent_template',
        string='Resolved Agent Template',
        compute='_compute_resolved_agent_template',
        help='Template the agent runtime resolves for this project '
             '(project-bound published → any bound → manual M2O → '
             'platform default).',
    )
    agent_template_binding_status = fields.Selection(
        selection=[
            ('dedicated', 'Dedicated'),
            ('shared', 'Shared'),
            ('platform_default', 'Platform Default'),
            ('none', 'None'),
        ],
        string='Template Binding Status',
        compute='_compute_resolved_agent_template',
    )
    resolved_template_skill_ids = fields.Many2many(
        'woow_paas_platform.skill',
        string='Template Bound Skills',
        compute='_compute_resolved_template_skills',
        inverse='_inverse_resolved_template_skills',
        help='skill_ids of the resolved template. Editable only when the '
             'template is dedicated to this project — compute+inverse '
             'instead of a related chain through the non-stored computed '
             'M2O (spec §2.3 implementation note).',
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
    # Skill binding UX（spec 2026-06-05 §2.3）— resolved template +       #
    # copy-on-write fork                                                   #
    # ------------------------------------------------------------------ #
    @api.depends('agent_template_id', 'agent_template_id.state',
                 'agent_template_id.project_ids')
    def _compute_resolved_agent_template(self):
        # Non-stored display field; depend at least on the explicit pick so
        # the fork written by action_create_dedicated_agent_template
        # (self.agent_template_id = fork) invalidates the cached resolution
        # in the same transaction. Shared→dedicated flips that come from
        # OTHER templates binding the project (search legs in
        # _resolve_agent_template) aren't auto-tracked — the form re-reads on
        # reload, which the fork action triggers (tag: reload).
        Session = self.env['woow_paas_platform.agent_session']
        default_raw = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_agent_template_id') or ''
        default_id = int(default_raw) if default_raw.isdigit() else 0
        for project in self:
            template = Session._resolve_agent_template(project)
            project.resolved_agent_template_id = template or False
            if not template:
                project.agent_template_binding_status = 'none'
            elif template.id == default_id:
                project.agent_template_binding_status = 'platform_default'
            elif template.project_ids == project:
                project.agent_template_binding_status = 'dedicated'
            else:
                # bound to several projects, or an unbound manual pick —
                # treat both as shared (read-only + fork offered).
                project.agent_template_binding_status = 'shared'

    @api.depends('resolved_agent_template_id')
    def _compute_resolved_template_skills(self):
        for project in self:
            template = project.resolved_agent_template_id
            project.resolved_template_skill_ids = \
                template.skill_ids if template else False

    def _inverse_resolved_template_skills(self):
        for project in self:
            if project.agent_template_binding_status != 'dedicated':
                raise UserError(
                    "Skill bindings can only be edited on a dedicated "
                    "template — use 'Create Dedicated Template' first.")
            project.resolved_agent_template_id.skill_ids = [
                (6, 0, project.resolved_template_skill_ids.ids)]

    def action_create_dedicated_agent_template(self):
        """Copy-on-write fork (spec §2.3). ONE copy(default=) call: m2m
        fields default to copy=True, so a bare copy() would leak the
        source's project_ids/skill_ids/mcp_server_ids into the fork and
        trip _check_project_ids_grantor_authority / subset constraints on
        an intermediate state. default= makes every constraint validate
        the FINAL values at create time (anti-pattern #4: no sudo bypass).
        """
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(
                "Only administrators can create a dedicated agent template.")
        if self.agent_template_binding_status == 'dedicated':
            raise UserError("This project already has a dedicated template.")
        source = self.resolved_agent_template_id
        if not source:
            raise UserError(
                "No agent template resolves for this project — configure "
                "the platform default in Settings → Woow PaaS first.")
        allowed_skills = source.skill_ids.filtered(
            lambda s: s.scope == 'system'
            or (s.scope == 'user' and s.project_id == self))
        allowed_mcp = source.mcp_server_ids.filtered(
            lambda m: m.scope == 'system'
            or (m.scope == 'user' and m.project_id == self))
        fork = source.copy(default={
            'name': "%s — %s" % (source.name, self.name),
            'project_ids': [(6, 0, [self.id])],
            'skill_ids': [(6, 0, allowed_skills.ids)],
            'mcp_server_ids': [(6, 0, allowed_mcp.ids)],
            'is_public': False,
            'state': 'published',
        })
        # Load-bearing: the source shared template still lists this project,
        # so two published templates now bind it. _resolve_agent_template's
        # generic search(limit=1) would tie-break by name — non-deterministic
        # w.r.t. intent. Setting the explicit M2O makes the fork win via the
        # resolver's explicit-pick leg WITHOUT rewriting the shared source's
        # project_ids (invariant 2: never write the shared template).
        self.agent_template_id = fork
        Session = self.env['woow_paas_platform.agent_session']
        Session._push_snapshot_to_running_pods('skill', 'user', self.id)
        Session._push_snapshot_to_running_pods('mcp', 'user', self.id)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_open_skill_upload_wizard(self):
        """Project-form entry: upload wizard prefilled as a user-scope
        skill owned by this project (spec §2.1)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Upload Skill',
            'res_model': 'woow_paas_platform.skill_upload_wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_scope': 'user',
                'default_project_id': self.id,
            },
        }

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

    def unlink(self):
        """刪 project 前同步拆除其 agent runtime（孤兒防護 §2.6）。

        ``agent_session.project_id`` 是 ``ondelete='cascade'``（DB 層，繞過
        ORM）：若不先拆除，刪 project 只刪掉 session row，cluster 上的 helm
        release / namespace（含明文 LLM Secret）會變成 Odoo 再也查不到的孤兒。
        故在此同步呼叫 operator DELETE，**失敗就阻塞刪除**（raise →
        ``super().unlink()`` 不執行、DB 不動），寧可不刪也不留孤兒。operator
        端對不存在的 release/namespace 冪等回成功，所以「早已沒有 pod」不會誤擋。

        agent_session 無 One2many 反向欄位，以 sudo search 找出（刪 project 的
        使用者未必對 session 有 ir.rule 可見性）。
        """
        from ..services.agent_runtime_client import AgentRuntimeOperatorError
        Session = self.env['woow_paas_platform.agent_session'].sudo()
        for project in self:
            for session in Session.search([('project_id', '=', project.id)]):
                try:
                    session._teardown_runtime()
                except AgentRuntimeOperatorError as exc:
                    raise UserError(
                        "無法刪除專案「%s」：其 AI agent 執行環境拆除失敗（%s）。"
                        "為避免雲端資源變成孤兒，刪除已中止；請稍後再試或聯絡管理員。"
                        % (project.name, exc)
                    )
        return super().unlink()

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

        Lock-step leg mapping (mirror project_security.xml):
        - follower leg → ``message_partner_ids & allowed`` (child_of(commercial)
          family — native ``project_project_rule_portal`` semantics).
        - collaborator leg → ``c.partner_id == partner`` EXACT, mirroring the
          native task-rule collaborator leg (partner_id == user.partner_id, NO
          family expansion); the family form was intra-tenant over-access.
        - task-leg extension → follower-style ``message_partner_ids in allowed``
          on the project's active tasks (intentional widen; keeps the native
          task follower child_of semantics, NOT the collaborator exact form).
        """
        self.ensure_one()
        project = self.sudo()
        if project.privacy_visibility != 'portal':
            return False
        commercial = partner.commercial_partner_id
        # child_of keeps active_test=True (more-deny vs native archived-inclusive
        # expansion — accepted safe-direction deviation; see task predicate).
        allowed = self.env['res.partner'].sudo().search(
            [('id', 'child_of', commercial.id)])
        if project.message_partner_ids & allowed:
            return True
        if project.collaborator_ids.filtered(
                lambda c: c.partner_id == partner and not c.limited_access):
            return True
        return bool(self.env['project.task'].sudo().search_count([
            ('project_id', '=', project.id),
            ('active', '=', True),
            ('message_partner_ids', 'in', allowed.ids),
        ]))
