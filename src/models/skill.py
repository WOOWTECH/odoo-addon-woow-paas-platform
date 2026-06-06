from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError


class Skill(models.Model):
    _name = 'woow_paas_platform.skill'
    _description = 'Project-scoped Agent Skill Bundle'
    _order = 'scope, project_id, name, version desc'

    name = fields.Char(
        string='Skill Name',
        required=True,
        index=True,
        help='Slug from SKILL.md frontmatter; unique per (project, version).',
    )
    title = fields.Char(string='Display Title')
    description = fields.Text()

    version = fields.Char(
        string='Version',
        required=True,
        default='0.1.0',
        help='Semver; bundle is immutable per (name, version).',
    )
    is_latest = fields.Boolean(
        string='Is Latest Published',
        default=False,
        index=True,
    )
    is_effective = fields.Boolean(
        string='Active in Pod',
        compute='_compute_is_effective',
        help="Loaded into the owning project's agent pod? user-scope: member "
             "of agent_session._effective_skill_ids(project) — ownership "
             "alone does NOT activate a skill (anti-pattern #3); system: "
             "published (auto-loaded platform-wide).",
    )

    scope = fields.Selection(
        selection=[('system', 'System'), ('user', 'User')],
        string='Scope',
        default='user',
        required=True,
        index=True,
        help="system = platform-wide (loaded into every project's pod); "
             "user = bound to a single project.",
    )

    project_id = fields.Many2one(
        'project.project',
        ondelete='cascade',
        index=True,
        help='Set for user-scope skills; NULL for system-scope skills.',
    )

    # Guest project members may read the metadata (name / version / state)
    # but NOT the bundle binary itself. Field-level ``groups=`` masks this
    # M2O for any user lacking group_system (the ``project_access`` walk
    # at row level is broader — guest qualifies for row R but not for
    # binary download).
    bundle_attachment_id = fields.Many2one(
        'ir.attachment',
        string='Bundle Storage',
        ondelete='restrict',
        groups='base.group_system',
    )
    bundle_sha256 = fields.Char(
        string='SHA256 Digest',
        required=True,
        readonly=True,
        index=True,
    )
    bundle_size_kb = fields.Integer(readonly=True)
    skill_metadata = fields.Text(
        string='Parsed SKILL.md frontmatter (JSON)',
        readonly=True,
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
        readonly=True,
    )
    published_at = fields.Datetime(
        string='Published At',
        readonly=True,
        help='Timestamp of the most recent publish; drives is_latest recompute.',
    )

    uploaded_by_uid = fields.Many2one(
        'res.users',
        required=True,
        readonly=True,
        default=lambda self: self.env.uid,
    )
    uploaded_at = fields.Datetime(
        readonly=True,
        default=fields.Datetime.now,
    )

    def init(self):
        # UNIQUE(project_id, name, version) cannot dedupe system skills:
        # Postgres treats NULL project_id as distinct. Use two partial unique
        # indexes (replaces the old _sql_constraints UNIQUE). IF NOT EXISTS so
        # this is idempotent on every -u / fresh install.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS skill_uniq_user_proj_name_version
            ON woow_paas_platform_skill (project_id, name, version)
            WHERE project_id IS NOT NULL
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS skill_uniq_system_name_version
            ON woow_paas_platform_skill (name, version)
            WHERE project_id IS NULL
        """)

    @api.constrains('scope', 'project_id')
    def _check_scope_project_consistency(self):
        # D4 invariant (mirrors mcp_server): scope='user' iff project_id NOT NULL.
        for record in self:
            if record.scope == 'user' and not record.project_id:
                raise ValidationError(
                    "User-scope skill '%s' must be linked to a project "
                    "(scope='user' requires project_id)." % record.name
                )
            if record.scope == 'system' and record.project_id:
                raise ValidationError(
                    "System-scope skill '%s' must not be linked to a project "
                    "(scope='system' requires project_id=NULL)." % record.name
                )

    # ------------------------------------------------------------------ #
    # HC-4 — bundle attachment anchoring                                  #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        skills = super().create(vals_list)
        for skill in skills:
            skill._anchor_bundle_attachment()
            # Drift 防護（spec §①）：直接 create state=published（繞過
            # action_publish）→ 補 published_at（必須在 recompute 之前，
            # 否則排序錯）+ 重算 is_latest 不變式。
            if skill.state == 'published':
                if not skill.published_at:
                    skill.published_at = fields.Datetime.now()
                skill._recompute_is_latest()
        return skills

    def write(self, vals):
        # scope / project_id are immutable for non-admins: an internal user with
        # write on their user-scope skill must not be able to flip it to
        # scope='system' (platform-public → cross-tenant leak). ir.rule alone
        # can't block this — Odoo checks the write rule against the PRE-write
        # state (still scope='user'), so the transition would slip through.
        if ('scope' in vals or 'project_id' in vals) and not self.env.su \
                and not self.env.user.has_group('base.group_system'):
            for skill in self:
                if 'scope' in vals and vals['scope'] != skill.scope:
                    raise AccessError(
                        "Only administrators can change a skill's scope."
                    )
                if 'project_id' in vals \
                        and vals.get('project_id') != skill.project_id.id:
                    raise AccessError(
                        "Only administrators can change a skill's project."
                    )
        # Re-point (admin path): capture pre-write group keys — after
        # super().write the record sits in the NEW group and the OLD group
        # can no longer be derived from self (spec §1 / Lens-2 finding).
        regroup = 'scope' in vals or 'project_id' in vals
        old_groups = []
        if regroup:
            old_groups = [
                (s._is_latest_group_domain(), s.scope, s.project_id.id)
                for s in self
            ]
        result = super().write(vals)
        if 'bundle_attachment_id' in vals:
            for skill in self:
                skill._anchor_bundle_attachment()
        # Drift 防護（spec §①）：vals 含 state → 補 published_at（若結果為
        # published 且缺）+ recompute。_recompute_is_latest 內部 write 的 vals
        # 是 {'is_latest': ...}，不含 state → 不踩此條件，自然不遞迴。
        if 'state' in vals:
            for skill in self:
                if skill.state == 'published' and not skill.published_at:
                    skill.published_at = fields.Datetime.now()
                skill._recompute_is_latest()
                # #111: state change (publish/deprecate/archive) alters which
                # skills load into running pods → push fresh snapshot best-
                # effort. _recompute_is_latest writes only is_latest (no
                # 'state') so it never re-enters this branch (no recursion).
                self.env['woow_paas_platform.agent_session'] \
                    ._push_snapshot_to_running_pods(
                        'skill', skill.scope, skill.project_id.id or None)
        if regroup:
            Session = self.env['woow_paas_platform.agent_session']
            for skill, (old_domain, old_scope, old_pid) in zip(
                    self, old_groups):
                if skill._is_latest_group_domain() == old_domain:
                    continue  # value written but group unchanged
                self._recompute_is_latest_for(old_domain)
                skill._recompute_is_latest()
                # System flip (either direction) touches every pod; a
                # user→user move touches the old and the new project pods.
                # _recompute_is_latest_for writes only is_latest → never
                # re-enters this branch (no recursion).
                if 'system' in (old_scope, skill.scope):
                    Session._push_snapshot_to_running_pods('skill', 'system')
                else:
                    Session._push_snapshot_to_running_pods(
                        'skill', 'user', old_pid)
                    Session._push_snapshot_to_running_pods(
                        'skill', 'user', skill.project_id.id)
        return result

    def _anchor_bundle_attachment(self):
        """Stamp ``res_model + res_id + public=False`` on the bundle
        attachment so the dedicated ``ir.attachment`` ir.rule and Odoo's
        ``/web/content`` controller gate it correctly. HC-4 attack vector
        is the side-channel binary stream through ``/web/content``; without
        anchoring the attachment row, Odoo's std fallback (template-level
        admin-only) masks Charlie by accident — defeating the actual rule
        we ship.
        """
        self.ensure_one()
        att = self.bundle_attachment_id
        if not att:
            return
        updates = {}
        if att.res_model != self._name:
            updates['res_model'] = self._name
        if att.res_id != self.id:
            updates['res_id'] = self.id
        if att.public:
            updates['public'] = False
        if updates:
            att.sudo().write(updates)

    # ------------------------------------------------------------------ #
    # Lifecycle transitions (admin backend) — maintain is_latest invariant#
    # ------------------------------------------------------------------ #
    def action_publish(self):
        """draft / deprecated → published. Stamp published_at and recompute
        the (project, name) is_latest pointer."""
        self.ensure_one()
        if self.state not in ('draft', 'deprecated'):
            raise UserError(
                "Skill '%s' v%s cannot be published from state '%s' "
                "(only draft / deprecated)."
                % (self.name, self.version, self.state)
            )
        self.write({
            'state': 'published',
            'published_at': fields.Datetime.now(),
        })
        self._recompute_is_latest()

    def action_deprecate(self):
        """published → deprecated. Recompute hands is_latest to the next
        newest published sibling (or clears it)."""
        self.ensure_one()
        if self.state != 'published':
            raise UserError(
                "Skill '%s' v%s cannot be deprecated from state '%s' "
                "(only published)." % (self.name, self.version, self.state)
            )
        self.write({'state': 'deprecated'})
        self._recompute_is_latest()

    def action_archive_skill(self):
        """Any non-archived state → archived (terminal). Recompute is_latest.

        Named ``action_archive_skill`` (not ``action_archive``) to avoid
        shadowing Odoo BaseModel.action_archive (active=False toggle); skill
        has no ``active`` field.
        """
        self.ensure_one()
        if self.state == 'archived':
            raise UserError(
                "Skill '%s' v%s is already archived."
                % (self.name, self.version)
            )
        self.write({'state': 'archived'})
        self._recompute_is_latest()

    def action_open_upload_wizard(self):
        """Open the system-skill upload wizard.

        Bound to a ``display="always"`` button in the skill list-view header
        so the empty-state list (``create="false"``, no record to select) still
        exposes an upload entry point — the binding contextual action only
        surfaces in the post-selection ⚙ Actions menu (F-SKU-1 / issue #102).
        Returns the action dict directly (no XML id resolved at list-arch load
        time); safe to call on an empty recordset.
        """
        return {
            'type': 'ir.actions.act_window',
            'name': 'Upload Skill',
            'res_model': 'woow_paas_platform.skill_upload_wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def _compute_is_effective(self):
        # One resolve+search per project (not per row) — N+1 guard.
        Session = self.env['woow_paas_platform.agent_session']
        effective_by_project = {}
        for skill in self:
            if skill.scope == 'system':
                skill.is_effective = skill.state == 'published'
                continue
            project = skill.project_id
            if not project:
                skill.is_effective = False
                continue
            if project.id not in effective_by_project:
                effective_by_project[project.id] = set(
                    Session._effective_skill_ids(project).ids)
            skill.is_effective = skill.id in effective_by_project[project.id]

    def _is_latest_group_domain(self):
        """Domain for the bucket this skill competes in for the is_latest
        pointer. System skills group platform-wide per name (project_id NULL);
        user skills group per (project_id, name)."""
        self.ensure_one()
        if self.scope == 'system':
            return [
                ('scope', '=', 'system'),
                ('project_id', '=', False),
                ('name', '=', self.name),
            ]
        return [
            ('scope', '=', 'user'),
            ('project_id', '=', self.project_id.id),
            ('name', '=', self.name),
        ]

    def _recompute_is_latest(self):
        """Invariant: at most one is_latest=True per (scope, project, name)
        group — recompute the group this skill currently sits in."""
        self.ensure_one()
        self._recompute_is_latest_for(self._is_latest_group_domain())

    @api.model
    def _recompute_is_latest_for(self, group_domain):
        """Group-key-explicit recompute. Needed for scope/project re-point:
        after write the record is in the NEW group, so the OLD group's
        winner can only be re-elected from a domain captured pre-write."""
        group = self.sudo().search(group_domain)
        published = group.filtered(lambda s: s.state == 'published')
        winner = published.sorted(
            key=lambda s: (s.published_at or datetime.min, s.id),
            reverse=True,
        )[:1]
        for skill in group:
            desired = skill.id == winner.id
            if skill.is_latest != desired:
                skill.write({'is_latest': desired})
