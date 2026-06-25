from __future__ import annotations

from typing import Any, Union

from odoo import models, fields, api
from odoo.exceptions import ValidationError
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

    _sql_constraints = [
        ('unique_slug', 'UNIQUE(slug)', 'Slug must be unique.'),
    ]

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

    # Cloud Services
    service_ids = fields.One2many(
        'woow_paas_platform.cloud_service',
        'workspace_id',
        string='Cloud Services',
        help='Cloud services deployed in this workspace',
    )

    # Computed fields
    member_count = fields.Integer(
        string='Member Count',
        compute='_compute_member_count',
        store=True
    )

    @api.depends('access_ids')
    def _compute_member_count(self) -> None:
        for workspace in self:
            workspace.member_count = len(workspace.access_ids)

    @api.model_create_multi
    def create(self, vals_list: list[dict[str, Any]]) -> Workspace:
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

    def _generate_slug(self, name: str) -> str:
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

    def action_archive(self) -> None:
        """Archive the workspace"""
        self.write({'state': 'archived'})

    def action_restore(self) -> None:
        """Restore archived workspace"""
        self.write({'state': 'active'})

    def unlink(self):
        """Tear down external-resource (Cloudflare tunnel) children through
        their own ORM unlink() BEFORE the DB FK cascade fires, so each child's
        F-ENV-4 fail-loud cleanup guard actually runs.

        SecurityAccess / SmartHome bind ``workspace_id`` with
        ``ondelete='cascade'``; a DB-level cascade executes a raw SQL DELETE and
        BYPASSES the Python ``unlink()`` override on those children (same ORM
        behaviour the ``cloud-service-unlink-override-cleanup`` pattern works
        around). Without this, hard-deleting a workspace would drop SA/SmartHome
        rows that still hold a LIVE, billed Cloudflare tunnel with no DB anchor
        left to drive a retry — a permanent orphan (pitfall:
        cf-ns-orphan-no-account-gc, F-ENV-5).

        Children are resolved by search (no explicit One2many to SA/SmartHome on
        Workspace) and unlinked with ``sudo()`` so cleanup completes regardless
        of the caller's row-level ACL (caller authorisation is enforced upstream
        at the controller). A child whose tunnel cleanup fails raises UserError;
        we deliberately let it PROPAGATE so the whole workspace deletion rolls
        back and the child + its tunnel survive for a retry — never swallow it.

        ``ondelete='cascade'`` is kept as a DB-integrity safety net for paths
        that bypass this override entirely (raw SQL / an upstream cascade chain).

        NOTE: CloudService (helm release / namespace) shares the SAME
        cascade-bypass exposure but is deliberately NOT routed here yet. Its
        helm uninstall lives in ``_do_cleanup()`` (NOT ``unlink()``), and that
        helm-failure path is not yet fail-loud — so routing CloudService
        correctly requires ALSO making ``_do_cleanup`` raise + keep the row on
        helm failure, not merely calling ``unlink()``. (``CloudService.unlink``
        only does sidecar-MCP row cleanup; the locked
        ``test_delete_workspace_preserves_mcp_via_orphaned_project`` verifies
        project/MCP row survival, NOT helm artifact cleanup, so it does not
        block routing.) Tracked as F-ENV-5 residual — see follow-ups.
        """
        for workspace in self:
            self.env['woow_paas_platform.security_access'].sudo().search(
                [('workspace_id', '=', workspace.id)]
            ).unlink()
            self.env['woow_paas_platform.smart_home'].sudo().search(
                [('workspace_id', '=', workspace.id)]
            ).unlink()
        return super().unlink()

    def check_user_access(self, user: Any = None, required_role: str | None = None) -> Union[Any, bool]:
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

    def get_user_role(self, user: Any = None) -> str | None:
        """Get the role of a user in this workspace"""
        self.ensure_one()
        user = user or self.env.user
        access = self.env['woow_paas_platform.workspace_access'].search([
            ('workspace_id', '=', self.id),
            ('user_id', '=', user.id),
        ], limit=1)
        return access.role if access else None
