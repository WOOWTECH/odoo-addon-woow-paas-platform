import logging

from odoo import _, SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError

_logger = logging.getLogger(__name__)

# #279 tenant-isolation reach sync — states in which an agent ns exists and still
# carries reach-ws-* labels, so a bind/unbind must re-render them. Excludes 'absent'
# (no ns yet → cold start provisions both legs) and 'spawn_failed' (no live ns).
_REACH_STATES = ('spawning', 'active', 'degraded', 'reaped')
# Advisory-lock classifier ('REAC' as int4) serializing a project's bind-path reach
# reconcile against the enforcing cron (§3.4/§3.5.1 TOCTOU over-delete window).
_REACH_LOCK_NS = 0x52454143


class ProjectProject(models.Model):
    # #698：掛上綁定守衛 mixin（唯一實作，四個入口共用）。它自己 override
    # write/create，所以本 class **不需要**、也不應該再手動呼叫守衛。
    # `_inherit` 一旦是 list，Odoo 就**不再**自動從它推出 `_name`
    # （`_build_model` 只在 `_inherit` 是字串時代入）⇒ 少了 `_name` 會被當成
    # 一個全新 model，直接撞 'use the same table and columns'。
    _name = 'project.project'
    _inherit = ['project.project',
                'woow_paas_platform.cloud_service_binding_guard']

    # 載體＝M:N；`cloud_service_id` 只作為觸發器（它是 cloud_service_ids[:1]
    # 的 stored compute，寫它會經 inverse 轉成內層 M:N write）。
    _cs_binding_fields = ('cloud_service_ids',)
    _cs_binding_triggers = ('cloud_service_id',)

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
                    _("Skill bindings can only be edited on a dedicated "
                      "template — use 'Create Dedicated Template' first."))
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
                _("Only administrators can create a dedicated agent template."))
        if self.agent_template_binding_status == 'dedicated':
            raise UserError(_("This project already has a dedicated template."))
        source = self.resolved_agent_template_id
        if not source:
            raise UserError(
                _("No agent template resolves for this project — configure "
                  "the platform default in Settings → Woow PaaS first."))
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
        # ⚠ The branch below selects on ``env.uid``, NOT on ``env.is_superuser()``
        #   — and ``sudo()`` does NOT reach the SUPERUSER branch. Odoo 18
        #   ``odoo/models.py``'s ``BaseModel.sudo()`` docstring, verbatim
        #   (symbol, not line number — this box carries two distinct odoo trees
        #   whose copies of this file differ by 16 lines, so a line number is
        #   only ever right for one of them and the doc never says which):
        #     "The superuser mode does not change the current user, and simply
        #      bypasses access rights checks."
        #   So ``sudo()`` sets ``env.su`` and leaves ``uid`` alone; only a real
        #   root env (uid == 1) takes the SUPERUSER branch. An earlier version of
        #   this comment listed "sudo" as an entry point of that branch — it was
        #   wrong, and it is the kind of wrong that gets "fixed" by making the
        #   code match the comment (see the ⚠ note below the else-branch).
        # - SUPERUSER path — a genuine root env (uid == 1): cron / install /
        #   migration, i.e. ``api.Environment(cr, SUPERUSER_ID, {})``. Only an
        #   EXPLICIT ``bootstrap_owner_partner_id`` hint — set by a trusted
        #   caller naming the real human owner — bootstraps; a bare SUPERUSER
        #   create with NO hint does NOT auto-own.
        # - Everything else, INCLUDING every ``.sudo()`` call from a tenant
        #   request (``ai_assistant.py`` self-service project create): the
        #   authenticated user owns the project. The hint is NOT honoured here —
        #   a non-trusted caller must not be able to assign ownership to an
        #   arbitrary partner via the context.
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
            # ⚠ LOAD-BEARING: switching the branch selector above to
            #   ``env.is_superuser()`` would route every tenant ``.sudo()``
            #   create into the SUPERUSER branch, which returns early without a
            #   hint — the tenant's own project would get ZERO project_access
            #   rows (invisible / unreachable / undeletable; see
            #   src/controllers/paas.py:353). This line is the only place in the
            #   repo that grants a tenant the 'owner' row.
            #   Locked by test_FPA14_non_root_sudo_create_self_owns.
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
                        _("無法刪除專案「%s」：其 AI agent 執行環境拆除失敗（%s）。"
                          "為避免雲端資源變成孤兒，刪除已中止；請稍後再試或聯絡管理員。")
                        % (project.name, exc)
                    )
        return super().unlink()

    # ------------------------------------------------------------------ #
    # #279 tenant-isolation — push reachable-workspace set to agent pod   #
    # ------------------------------------------------------------------ #
    def _reconcile_agent_runtime_reach(self, old_ws_ids):
        """After a binding mutation, push this project's reachable-workspace set to
        its agent pod: leg-1 (pod egress allowlist) + leg-2 (agent-ns reach labels),
        via the operator ``/sync-workspaces`` contract.

        ``old_ws_ids`` = the workspace-id set BEFORE the mutation. A GRANT (a newly
        reachable workspace) is fail-loud — a raise rolls the caller's txn back so the
        DB binding never commits without the cluster reach landing (client retry in
        agent_runtime_client covers a lost-response-after-success). A REVOKE-only
        change is best-effort: the DB commits and the enforcing cron (§3.5.1) is the
        authoritative converger; with leg-1-first the revoke is effective the instant
        egress re-renders even if leg-2 lags.
        """
        self.ensure_one()
        if not self.use_agent_runtime:
            return
        new_ws_ids = set(self.workspace_ids.ids)
        old = set(old_ws_ids or ())
        added, removed = new_ws_ids - old, old - new_ws_ids
        if not added and not removed:
            return
        Session = self.env['woow_paas_platform.agent_session'].sudo()
        session = Session.search([
            ('project_id', '=', self.id),
            ('state', 'in', _REACH_STATES),
            ('project_namespace_hash', '!=', False),
        ], limit=1)
        if not session:
            return  # no live/recent ns → cold start provisions both legs atomically
        from ..services.agent_runtime_client import get_agent_runtime_client
        client = get_agent_runtime_client(self.env)
        if not client:
            return
        # Serialize this project's bind against the enforcing cron to close the TOCTOU
        # over-delete window (the cron reads desired, then reconciles LIVE labels — a
        # grant committing mid-cadence must not be clobbered). Txn-level lock
        # auto-releases on commit/rollback; per-project → contention only on
        # same-project concurrent bind+cron (rare).
        self.env.cr.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (_REACH_LOCK_NS, self.id))
        try:
            client.sync_workspaces(
                session.project_namespace_hash, self.workspace_ids.ids,
                require_release=bool(added),
            )
        except Exception as exc:
            if added:
                raise  # GRANT: fail-loud → roll back the binding
            _logger.warning(
                "reach revoke best-effort failed, project %s: %s", self.id, exc)

    def write(self, vals):
        """Reconcile agent-pod reach after a cloud_service binding change (#279
        Path A). ``workspace_ids`` is a stored compute over
        ``cloud_service_ids.workspace_id``, so binding/unbinding a service here
        shifts the reachable set and must re-render both isolation legs. Captured
        per-project BEFORE ``super().write`` so the delta reflects this write only.
        """
        tracked = 'cloud_service_ids' in vals
        old = {p.id: set(p.workspace_ids.ids) for p in self} if tracked else {}
        res = super().write(vals)
        if tracked:
            for project in self:
                project._reconcile_agent_runtime_reach(old.get(project.id, set()))
        return res

    @api.model
    def _cron_reconcile_reach(self):
        """#279 O6 — authoritative periodic reconciler for agent-pod reach.

        Ground truth = Odoo. For every agent-runtime project with a reach-bearing
        session, push its CURRENT reachable-workspace set to the operator (idempotent
        add-missing + delete-stale), converging any drift a best-effort revoke or a
        lost sync left behind. This is the correctness FLOOR for the write-path's
        best-effort revokes — so it ENFORCES by default (§3.5.1); dry-run is only an
        explicit ops opt-out (`woow_paas_platform.reach_reconciler_enforce=False`)
        for a single soak cadence and MUST be flipped on before #280.

        Per-project isolation (one transient error can't abort the cadence);
        `require_release=False` (a reconcile must not fail-loud on a reaped pod); and a
        per-project SESSION-level advisory lock (acquired + released per project via
        pg_advisory_unlock) + a fresh re-read of `workspace_ids` immediately before
        each sync — this closes the TOCTOU window where the cron would clobber a grant
        that committed between its snapshot and the operator's live-label read (§3.5.1
        [F cron #1]). A session lock (not xact) is used because the cron does no DB
        writes — nothing to commit to drop an xact lock, and committing mid-cron is
        forbidden inside a test cursor.
        """
        ICP = self.env['ir.config_parameter'].sudo()
        enforce = ICP.get_param(
            'woow_paas_platform.reach_reconciler_enforce', 'True'
        ).strip().lower() not in ('false', '0', 'no', 'off', '')
        client = None
        if enforce:
            from ..services.agent_runtime_client import get_agent_runtime_client
            client = get_agent_runtime_client(self.env)
            if not client:
                _logger.warning(
                    "reach reconciler: operator unconfigured — skipping cadence")
                return False
        Session = self.env['woow_paas_platform.agent_session'].sudo()
        sessions = Session.search([
            ('state', 'in', _REACH_STATES),
            ('project_namespace_hash', '!=', False),
            ('project_id.use_agent_runtime', '=', True),
        ])
        work = [(s.project_namespace_hash, s.project_id.id) for s in sessions]
        Project = self.env['project.project'].sudo()
        for project_hash, project_id in work:
            # SESSION-level advisory lock: acquired + released per project via explicit
            # unlock (finally), so it releases promptly WITHOUT a commit. The cron does
            # no DB writes, so there is nothing to commit to drop an xact lock — and
            # committing mid-cron is forbidden inside a test cursor. Same lock space as
            # the bind path's pg_advisory_xact_lock, so the two still serialize.
            self.env.cr.execute(
                "SELECT pg_advisory_lock(%s, %s)", (_REACH_LOCK_NS, project_id))
            try:
                project = Project.browse(project_id)
                project.invalidate_recordset(['workspace_ids'])   # fresh re-read
                desired = project.workspace_ids.ids
                if enforce:
                    client.sync_workspaces(
                        project_hash, desired, require_release=False)
                else:
                    _logger.info(
                        "reach reconciler DRY-RUN: project %s → %s",
                        project_id, desired)
            except Exception:
                _logger.exception(
                    "reach reconciler: project %s failed", project_id)
            finally:
                self.env.cr.execute(
                    "SELECT pg_advisory_unlock(%s, %s)", (_REACH_LOCK_NS, project_id))
        return True

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
