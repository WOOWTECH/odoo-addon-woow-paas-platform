from __future__ import annotations

from odoo import models, fields, api
from odoo.exceptions import ValidationError


# Role constants
ROLE_OWNER = 'owner'
ROLE_ADMIN = 'admin'
ROLE_USER = 'user'
ROLE_GUEST = 'guest'

# Role hierarchy (low to high)
ROLE_HIERARCHY = [ROLE_GUEST, ROLE_USER, ROLE_ADMIN, ROLE_OWNER]

# Roles that can be assigned by admins (excludes owner)
ASSIGNABLE_ROLES = [ROLE_ADMIN, ROLE_USER, ROLE_GUEST]

# Role selection for fields.Selection
ROLE_SELECTION = [
    (ROLE_OWNER, 'Owner'),
    (ROLE_ADMIN, 'Admin'),
    (ROLE_USER, 'User'),
    (ROLE_GUEST, 'Guest'),
]


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
                owner_count = self.search_count([
                    ('workspace_id', '=', access.workspace_id.id),
                    ('role', '=', ROLE_OWNER),
                    ('id', '!=', access.id),
                ])
                if owner_count > 0:
                    raise ValidationError(
                        'A workspace can only have one owner. '
                        'Transfer ownership first before assigning a new owner.'
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
