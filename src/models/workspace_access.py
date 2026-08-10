from __future__ import annotations

from odoo import models, fields, api
from odoo.exceptions import AccessError, ValidationError


# Role constants
ROLE_OWNER = 'owner'
ROLE_ADMIN = 'admin'
ROLE_USER = 'user'
ROLE_GUEST = 'guest'

# Role hierarchy (low to high)
ROLE_HIERARCHY = [ROLE_GUEST, ROLE_USER, ROLE_ADMIN, ROLE_OWNER]

# Roles that can be assigned by admins (excludes owner). Used by the
# controller's payload-shape validation (paas.py _invite_member /
# _update_member_role) — independent of *who* is granting; see
# ASSIGNABLE_ROLES_BY_ROLE below for the grantor-aware check.
ASSIGNABLE_ROLES = [ROLE_ADMIN, ROLE_USER, ROLE_GUEST]

# Role selection for fields.Selection
ROLE_SELECTION = [
    (ROLE_OWNER, 'Owner'),
    (ROLE_ADMIN, 'Admin'),
    (ROLE_USER, 'User'),
    (ROLE_GUEST, 'Guest'),
]

# Grantor-role → assignable target roles (#556 B1). Mirrors
# project_access.ASSIGNABLE_ROLES_BY_ROLE (src/models/project_access.py)
# with ONE deliberate deviation: ROLE_ADMIN here includes ROLE_ADMIN itself
# (project_access's admin cannot grant admin). paas.py's _invite_member has
# always let an admin invite another admin; copying project_access's
# stricter table verbatim would break that existing product behaviour.
# It is not a privilege escalation — the grantor is already admin, so
# granting admin to someone else does not raise anyone above the grantor's
# own ceiling. Only owner may still grant owner. Do NOT "fix" this back to
# match project_access — see
# docs/tracking/landings/556-workspace-access-authz-landing.md.
ASSIGNABLE_ROLES_BY_ROLE = {
    ROLE_OWNER: [ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST],
    ROLE_ADMIN: [ROLE_ADMIN, ROLE_USER, ROLE_GUEST],
    ROLE_USER: [],
    ROLE_GUEST: [],
}


class WorkspaceAccess(models.Model):
    _name = 'woow_paas_platform.workspace_access'
    _description = 'Workspace User Access'
    _order = 'role desc, create_date desc'
    _rec_name = 'user_id'

    # Core fields
    workspace_id = fields.Many2one(
        'woow_paas_platform.workspace',
        string='Workspace',
        required=True,
        ondelete='cascade',
        index=True
    )
    user_id = fields.Many2one(
        'res.users',
        string='User',
        required=True,
        ondelete='cascade',
        index=True
    )

    # Role
    role = fields.Selection(
        ROLE_SELECTION,
        string='Role',
        required=True,
        default=ROLE_USER,
        index=True
    )

    # Related fields for convenience
    user_name = fields.Char(related='user_id.name', string='User Name', store=True)
    user_email = fields.Char(related='user_id.email', string='User Email', store=True)
    user_avatar = fields.Binary(related='user_id.avatar_128', string='Avatar')

    # Invitation fields
    invited_by_id = fields.Many2one(
        'res.users',
        string='Invited By',
        default=lambda self: self.env.user
    )
    invited_date = fields.Datetime(
        string='Invited Date',
        default=fields.Datetime.now
    )

    _sql_constraints = [
        ('unique_user_workspace', 'UNIQUE(workspace_id, user_id)',
         'A user can only have one access record per workspace.')
    ]

    @api.constrains('role', 'workspace_id')
    def _check_owner_count(self) -> None:
        """Ensure each workspace has exactly one owner"""
        for access in self:
            if access.role == ROLE_OWNER:
                # #556 B3: sudo() — this constraint must see every access row
                # in the workspace, not just the ones ir.rule lets the
                # caller see. It was previously unfiltered only by accident
                # (whoever could reach create() could already see every row
                # in the workspace); that accident is no longer guaranteed
                # once create()/write() are guarded below.
                owner_count = self.sudo().search_count([
                    ('workspace_id', '=', access.workspace_id.id),
                    ('role', '=', ROLE_OWNER),
                    ('id', '!=', access.id),
                ])
                if owner_count > 0:
                    raise ValidationError(
                        'A workspace can only have one owner. '
                        'Transfer ownership first before assigning a new owner.'
                    )

    # ------------------------------------------------------------------ #
    # Role-hierarchy + owner-row guard (#556 B1/B2)                       #
    # ------------------------------------------------------------------ #
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            # ⚠ Unlike project_access.py:134 (`if not target_role or not
            # project_id: return`), do NOT early-return when 'role' is
            # omitted from vals — that would let a create-direction payload
            # skip this guard entirely and fall back on nothing but the
            # ir.rule's create check (which, per #556's root cause, is
            # evaluated AFTER the row is already inserted and can be
            # self-satisfied by the very row being created). Both an
            # explicit role='admin' payload and an omitted-role payload
            # must go through the SAME check here.
            effective_role = vals.get('role') or ROLE_USER
            self._check_assignable_role(effective_role, vals.get('workspace_id'))
        return super().create(vals_list)

    def write(self, vals):
        # #556 write-pivot (found by independent verification, not in the
        # original analysis): workspace_id / user_id are membership
        # IDENTITY ANCHORS — no legitimate tenant flow ever re-points an
        # existing access row at a different workspace or a different
        # user (invite/role-change/remove all operate on `role` only, on
        # a row whose workspace_id/user_id stay fixed for its lifetime).
        # Without this, a caller who legitimately self-created their OWN
        # workspace (P6 bootstrap — they really are that row's owner) can
        # write([their_own_row_id], {'workspace_id': <victim_ws>, 'role':
        # 'admin'}) on their own row: ir.rule's write check evaluates only
        # the PRE-write state (still their own workspace, still owner ⇒
        # passes) and never re-evaluates the post-write target; and
        # `_check_assignable_role` below reads `rec.workspace_id.id` —
        # also the pre-write/SOURCE workspace, not the vals-supplied
        # target — so a self-owned row can be "moved" straight into a
        # victim workspace with an attacker-chosen role. This must run
        # first, before the source-workspace-scoped checks below, and
        # must compare the ACTUAL new value against the current one (not
        # merely whether the key is present) so a no-op write (UI
        # batch-send of unmodified fields) is not mistakenly blocked.
        if not self.env.is_superuser() and not self.env.user.has_group('base.group_system'):
            for rec in self:
                if 'workspace_id' in vals and vals['workspace_id'] != rec.workspace_id.id:
                    raise AccessError(
                        "Cannot re-point an access row (id=%s) to a "
                        "different workspace." % rec.id
                    )
                if 'user_id' in vals and vals['user_id'] != rec.user_id.id:
                    raise AccessError(
                        "Cannot re-point an access row (id=%s) to a "
                        "different user." % rec.id
                    )
        # Owner-row protection applies to every write on an owner row,
        # regardless of which fields are being changed — ir.rule cannot
        # express this because record rules evaluate the CALLER's pre-write
        # state, never the TARGET row's, so an admin's write({'role':
        # 'user'}) on the owner's own row would otherwise sail through the
        # manage rule outright (it only checks that the admin is
        # owner/admin, not that the row being touched belongs to someone
        # else who is the owner).
        for rec in self:
            self._check_owner_row_protected(rec)
        if 'role' in vals:
            for rec in self:
                self._check_assignable_role(vals['role'], rec.workspace_id.id)
        return super().write(vals)

    def unlink(self):
        """(#556 B2) Two invariants, checked per record:

        (a) a non-owner caller may not unlink a row whose role is currently
            'owner' — closes the demote-then-self-promote chain (an admin
            who cannot directly unlink the owner's row cannot open a
            0-owner window that way either);
        (b) the last owner row of a workspace cannot be removed at all,
            even by the owner themselves (mirrors project_access.unlink(),
            src/models/project_access.py:89-104) — checked only once (a)
            has cleared, so the AUTHORIZATION denial (AccessError) and the
            INVARIANT denial (ValidationError) stay distinguishable.
        """
        for rec in self:
            if rec.role != ROLE_OWNER:
                continue
            self._check_owner_row_protected(rec)
            other_owners = self.sudo().search_count([
                ('workspace_id', '=', rec.workspace_id.id),
                ('role', '=', ROLE_OWNER),
                ('id', '!=', rec.id),
            ])
            if not other_owners:
                raise ValidationError(
                    "Cannot remove the last owner of workspace '%s'. "
                    "Transfer ownership first." % (
                        rec.workspace_id.display_name or rec.workspace_id.id)
                )
        return super().unlink()

    @api.model
    def _check_assignable_role(self, target_role, workspace_id):
        """Grantor may only assign target roles per ASSIGNABLE_ROLES_BY_ROLE.

        Bypassed for ``env.is_superuser()`` (covers uid == SUPERUSER_ID and
        any ``sudo()`` recordset — the workspace create bootstrap and the
        HA PaaS auto-provision path both run through sudo) and for
        ``base.group_system`` (parallel to the
        ``workspace_access_rule_admin_full`` ir.rule). Role assignment via
        invite / role-change / bootstrap is a normal, gated product flow —
        this is the scope-flip-write-guard-not-ir-rule family (a legitimate
        change path that only needs gating), not the su-env-exempt-guard
        family (no legitimate change path at all); the su/group_system
        exemption is therefore correct here, not a hole.
        """
        if not workspace_id:
            return  # missing workspace_id is caught by required=True
        if self.env.is_superuser():
            return
        if self.env.user.has_group('base.group_system'):
            return
        grantor = self.sudo().search([
            ('workspace_id', '=', workspace_id),
            # env.uid, not context: call_kw lets the caller replace the
            # whole context wholesale (odoo/api.py::call_kw is
            # with_context(context) as a positional arg, not a merge), so a
            # context-sourced identity would be forgeable over RPC.
            ('user_id', '=', self.env.uid),
        ], limit=1)
        grantor_role = grantor.role if grantor else None
        allowed = ASSIGNABLE_ROLES_BY_ROLE.get(grantor_role, [])
        if target_role not in allowed:
            raise AccessError(
                "User %s (role=%s) cannot assign role '%s' on workspace %s. "
                "Assignable roles for %s: %s."
                % (
                    self.env.user.display_name,
                    grantor_role or '<none>',
                    target_role,
                    workspace_id,
                    grantor_role or '<none>',
                    allowed,
                )
            )

    @api.model
    def _check_owner_row_protected(self, rec):
        """A non-owner caller may not write/unlink a row whose CURRENT role
        is 'owner'. ir.rule cannot express this: the manage rule evaluates
        only the caller's own pre-write role, never the target row's, so
        without this an admin's write({'role': 'user'}) on the owner's row
        would pass the manage rule outright — the first step of a
        demote-then-self-promote chain into a transient 0-owner window.
        project_access has no equivalent guard; this is a deliberate
        addition, not parity with that model (see module docstring / #556
        landing doc for the three deviations from the project_access
        template).
        """
        if rec.role != ROLE_OWNER:
            return
        if self.env.is_superuser():
            return
        if self.env.user.has_group('base.group_system'):
            return
        caller_access = self.sudo().search([
            ('workspace_id', '=', rec.workspace_id.id),
            ('user_id', '=', self.env.uid),
        ], limit=1)
        if caller_access and caller_access.role == ROLE_OWNER:
            return
        raise AccessError(
            "Only the workspace owner may modify or remove the owner's own "
            "access row (workspace %s)." % rec.workspace_id.id
        )

    def transfer_ownership(self, new_owner_id: int) -> None:
        """Transfer ownership to another user"""
        self.ensure_one()
        if self.role != ROLE_OWNER:
            raise ValidationError('Only the current owner can transfer ownership.')

        new_owner_access = self.search([
            ('workspace_id', '=', self.workspace_id.id),
            ('user_id', '=', new_owner_id),
        ], limit=1)

        if not new_owner_access:
            raise ValidationError('The new owner must be an existing member of the workspace.')

        # Demote current owner to admin
        self.write({'role': ROLE_ADMIN})
        # Promote new owner
        new_owner_access.write({'role': ROLE_OWNER})
        # Update workspace owner_id
        self.workspace_id.write({'owner_id': new_owner_id})

    @api.model
    def get_role_permissions(self, role: str) -> dict[str, bool]:
        """Get permissions for a given role"""
        permissions = {
            ROLE_OWNER: {
                'can_view': True,
                'can_edit': True,
                'can_delete': True,
                'can_manage_members': True,
                'can_manage_workspace': True,
                'can_transfer_ownership': True,
            },
            ROLE_ADMIN: {
                'can_view': True,
                'can_edit': True,
                'can_delete': False,
                'can_manage_members': True,
                'can_manage_workspace': True,
                'can_transfer_ownership': False,
            },
            ROLE_USER: {
                'can_view': True,
                'can_edit': True,
                'can_delete': False,
                'can_manage_members': False,
                'can_manage_workspace': False,
                'can_transfer_ownership': False,
            },
            ROLE_GUEST: {
                'can_view': True,
                'can_edit': False,
                'can_delete': False,
                'can_manage_members': False,
                'can_manage_workspace': False,
                'can_transfer_ownership': False,
            },
        }
        return permissions.get(role, {})

    def get_permissions(self) -> dict[str, bool]:
        """Get permissions for this access record"""
        self.ensure_one()
        return self.get_role_permissions(self.role)
