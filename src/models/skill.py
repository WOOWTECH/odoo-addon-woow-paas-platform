from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError


class Skill(models.Model):
    _name = 'woow_paas_platform.skill'
    _description = 'Project-scoped Agent Skill Bundle'
    _order = 'project_id, name, version desc'

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

    project_id = fields.Many2one(
        'project.project',
        required=True,
        ondelete='cascade',
        index=True,
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

    _sql_constraints = [
        (
            'unique_name_version_per_project',
            'UNIQUE(project_id, name, version)',
            'Skill name + version must be unique per project.',
        ),
    ]

    # ------------------------------------------------------------------ #
    # HC-4 — bundle attachment anchoring                                  #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        skills = super().create(vals_list)
        for skill in skills:
            skill._anchor_bundle_attachment()
        return skills

    def write(self, vals):
        result = super().write(vals)
        if 'bundle_attachment_id' in vals:
            for skill in self:
                skill._anchor_bundle_attachment()
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
        self._recompute_is_latest(self.project_id.id, self.name)

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
        self._recompute_is_latest(self.project_id.id, self.name)

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
        self._recompute_is_latest(self.project_id.id, self.name)

    def _recompute_is_latest(self, project_id, name):
        """Invariant: at most one is_latest=True per (project_id, name),
        and it is the most-recently-published (published_at desc, id desc)
        skill in state 'published'."""
        group = self.sudo().search([
            ('project_id', '=', project_id),
            ('name', '=', name),
        ])
        published = group.filtered(lambda s: s.state == 'published')
        winner = published.sorted(
            key=lambda s: (s.published_at or datetime.min, s.id),
            reverse=True,
        )[:1]
        for skill in group:
            # winner empty recordset → winner.id is False, which no
            # persisted id equals, so desired is correctly False for all.
            desired = skill.id == winner.id
            if skill.is_latest != desired:
                skill.write({'is_latest': desired})
