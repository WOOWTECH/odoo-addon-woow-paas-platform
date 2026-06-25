from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


ROLE_OWNER = 'owner'
ROLE_ADMIN = 'admin'
ROLE_USER = 'user'
ROLE_GUEST = 'guest'

ROLE_SELECTION = [
    (ROLE_OWNER, 'Owner'),
    (ROLE_ADMIN, 'Admin'),
    (ROLE_USER, 'User'),
    (ROLE_GUEST, 'Guest'),
]

# Role hierarchy — owner > admin > user > guest. Only the grantor's own
# role determines which target roles they may assign (B-1 / PA-4 / PA-5).
# Admin cannot grant admin or owner; user / guest cannot grant anything.
ASSIGNABLE_ROLES_BY_ROLE = {
    ROLE_OWNER: [ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST],
    ROLE_ADMIN: [ROLE_USER, ROLE_GUEST],
    ROLE_USER: [],
    ROLE_GUEST: [],
}


class ProjectAccess(models.Model):
    _name = 'woow_paas_platform.project_access'
    _description = 'Project Member Access (first-class ACL)'
    _order = 'role desc, create_date desc'
    _rec_name = 'partner_id'

    project_id = fields.Many2one(
        'project.project',
        required=True,
        ondelete='cascade',
        index=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Member',
        required=True,
        ondelete='cascade',
        index=True,
    )
    user_id = fields.Many2one(
        'res.users',
        string='User',
        compute='_compute_user_id',
        store=True,
        index=True,
    )

    role = fields.Selection(
        ROLE_SELECTION,
        string='Role',
        required=True,
        default=ROLE_USER,
        index=True,
    )

    invited_by_uid = fields.Many2one(
        'res.users',
        readonly=True,
        default=lambda self: self.env.uid,
    )
    invited_at = fields.Datetime(
        readonly=True,
        default=fields.Datetime.now,
    )

    _sql_constraints = [
        (
            'unique_partner_per_project',
            'UNIQUE(project_id, partner_id)',
            'A partner can have only one access record per project.',
        ),
    ]

    @api.depends('partner_id')
    def _compute_user_id(self):
        for rec in self:
            user = self.env['res.users'].sudo().search(
                [('partner_id', '=', rec.partner_id.id)], limit=1,
            )
            rec.user_id = user.id if user else False

    def unlink(self):
        """Prevent unlinking the last owner row of any project."""
        for rec in self:
            if rec.role != ROLE_OWNER:
                continue
            other_owners = self.sudo().search_count([
                ('project_id', '=', rec.project_id.id),
                ('role', '=', ROLE_OWNER),
                ('id', '!=', rec.id),
            ])
            if not other_owners:
                raise ValidationError(
                    "Cannot remove the last owner of project '%s'. "
                    "Transfer ownership first." % (rec.project_id.display_name or rec.project_id.id)
                )
        return super().unlink()

    # ------------------------------------------------------------------ #
    # Role-hierarchy gate (B-1 / PA-5)                                    #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._check_assignable_role(
                vals.get('role'), vals.get('project_id'),
            )
        return super().create(vals_list)

    def write(self, vals):
        if 'role' in vals:
            for rec in self:
                self._check_assignable_role(
                    vals['role'], rec.project_id.id,
                )
        return super().write(vals)

    @api.model
    def _check_assignable_role(self, target_role, project_id):
        """Grantor may only assign target roles per ASSIGNABLE_ROLES_BY_ROLE.

        Bypassed for ``env.is_superuser()`` (covers uid == SUPERUSER_ID and
        any ``sudo()`` recordset — the project create bootstrap runs through
        sudo) and for ``base.group_system`` (parallel to the
        ``project_access_rule_admin_full`` ir.rule).
        """
        if not target_role or not project_id:
            return
        if self.env.is_superuser():
            return
        if self.env.user.has_group('base.group_system'):
            return
        grantor = self.sudo().search([
            ('project_id', '=', project_id),
            ('user_id', '=', self.env.uid),
        ], limit=1)
        grantor_role = grantor.role if grantor else None
        allowed = ASSIGNABLE_ROLES_BY_ROLE.get(grantor_role, [])
        if target_role not in allowed:
            raise AccessError(
                "User %s (role=%s) cannot assign role '%s' on project %s. "
                "Assignable roles for %s: %s."
                % (
                    self.env.user.display_name,
                    grantor_role or '<none>',
                    target_role,
                    project_id,
                    grantor_role or '<none>',
                    allowed,
                )
            )
