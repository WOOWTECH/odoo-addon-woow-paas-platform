from odoo import models, fields, api
from odoo.exceptions import AccessError, ValidationError
import re

from .workspace_access import ROLE_OWNER, ROLE_HIERARCHY


class Workspace(models.Model):
    _name = 'woow_paas_platform.workspace'
    _description = 'PaaS Workspace'
    _order = 'create_date desc'

    # Basic Information
    name = fields.Char(string='Name', required=True, index=True)
    description = fields.Text(string='Description')
    slug = fields.Char(
        string='Slug',
        index=True,
        help='URL-friendly unique identifier'
    )

    # Ownership
    owner_id = fields.Many2one(
        'res.users',
        string='Owner',
        required=True,
        default=lambda self: self.env.user,
        ondelete='restrict',
        index=True
    )

    # State
    state = fields.Selection([
        ('active', 'Active'),
        ('archived', 'Archived'),
    ], string='State', default='active', required=True, index=True)

    # Access Control
    access_ids = fields.One2many(
        'woow_paas_platform.workspace_access',
        'workspace_id',
        string='Access Controls'
    )

    # Computed fields
    member_count = fields.Integer(
        string='Member Count',
        compute='_compute_member_count',
        store=True
    )

    @api.depends('access_ids')
    def _compute_member_count(self):
        for workspace in self:
            workspace.member_count = len(workspace.access_ids)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('slug'):
                vals['slug'] = self._generate_slug(vals.get('name', ''))
        workspaces = super().create(vals_list)
        # Create owner access for each workspace
        for workspace in workspaces:
            self.env['woow_paas_platform.workspace_access'].create({
                'workspace_id': workspace.id,
                'user_id': workspace.owner_id.id,
                'role': ROLE_OWNER,
            })
        return workspaces

    def _generate_slug(self, name):
        """Generate a URL-friendly slug from name"""
        if not name:
            return ''
        # Convert to lowercase and replace spaces/special chars with hyphens
        slug = re.sub(r'[^\w\s-]', '', name.lower())
        slug = re.sub(r'[-\s]+', '-', slug).strip('-')
        # Ensure uniqueness
        base_slug = slug
        counter = 1
        while self.search_count([('slug', '=', slug)]) > 0:
            slug = f"{base_slug}-{counter}"
            counter += 1
        return slug

    def action_archive(self):
        """Archive the workspace"""
        self.write({'state': 'archived'})

    def action_restore(self):
        """Restore archived workspace"""
        self.write({'state': 'active'})

    def check_user_access(self, user=None, required_role=None):
        """
        Check if user has access to this workspace.

        Args:
            user: Target user (defaults to current user)
            required_role: Minimum role required ('guest', 'user', 'admin', 'owner')

        Returns:
            workspace_access record if authorized, False otherwise

        Role hierarchy (low to high): guest < user < admin < owner
        User's role must be >= required_role to pass the check.
        """
        if not self:
            return False
        self.ensure_one()
        user = user or self.env.user
        access = self.env['woow_paas_platform.workspace_access'].search([
            ('workspace_id', '=', self.id),
            ('user_id', '=', user.id),
        ], limit=1)

        if not access:
            return False

        if required_role:
            if ROLE_HIERARCHY.index(access.role) < ROLE_HIERARCHY.index(required_role):
                return False

        return access

    def get_user_role(self, user=None):
        """Get the role of a user in this workspace"""
        self.ensure_one()
        user = user or self.env.user
        access = self.env['woow_paas_platform.workspace_access'].search([
            ('workspace_id', '=', self.id),
            ('user_id', '=', user.id),
        ], limit=1)
        return access.role if access else None
