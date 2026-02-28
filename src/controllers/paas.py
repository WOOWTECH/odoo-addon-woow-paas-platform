from __future__ import annotations

import hashlib
import json
import logging
import traceback
import uuid
from datetime import datetime
from typing import Any

from odoo.http import request, route, Controller

from ..models.workspace_access import (
    ROLE_OWNER, ROLE_ADMIN, ROLE_USER,
    ASSIGNABLE_ROLES,
)
from ..services.paas_operator import (
    get_paas_operator_client,
    PaaSOperatorError,
    PaaSOperatorConnectionError,
)

_logger = logging.getLogger(__name__)


class PaasController(Controller):
    @route("/woow", auth="user", website=False)
    def paas_app(self) -> Any:
        """
        Render the main PaaS application page.

        This endpoint serves the standalone OWL application at /woow.
        Requires authenticated user session.

        Returns:
            Response: Rendered QWeb template 'woow_paas_platform.paas_app'
                      with session_info context for frontend initialization.
        """
        session_info = request.env['ir.http'].session_info()
        return request.render(
            'woow_paas_platform.paas_app',
            {'session_info': session_info}
        )

    # ==================== Config API ====================

    @route("/api/config", auth="user", methods=["POST"], type="json")
    def api_config(self) -> dict[str, Any]:
        """
        Get PaaS platform configuration for frontend.

        Returns:
            dict: Configuration including:
                - success (bool): True
                - data (dict): Configuration values
                    - domain: Base domain for deployed services (e.g., woowtech.io)
        """
        IrConfigParameter = request.env['ir.config_parameter'].sudo()
        domain = IrConfigParameter.get_param('woow_paas_platform.paas_domain', 'woowtech.io')

        return {
            'success': True,
            'data': {
                'domain': domain,
            }
        }

    # ==================== Workspace API ====================

    @route("/api/workspaces", auth="user", methods=["POST"], type="json")
    def api_workspace(self, action: str = 'list', name: str | None = None, description: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        Handle workspace collection operations via JSON-RPC.

        Args:
            action (str): Operation type. One of:
                - 'list': Get all accessible workspaces
                - 'create': Create a new workspace
            name (str, optional): Workspace name (required for create)
            description (str, optional): Workspace description
            **kwargs: Additional parameters (ignored)

        Returns:
            dict: JSON response with structure:
                - success (bool): Whether the operation succeeded
                - data (dict|list): Result data (on success)
                - error (str): Error message (on failure)
                - count (int): Item count (for list operations)
        """
        if action == 'list':
            return self._list_workspaces()
        elif action == 'create':
            return self._create_workspace(name, description)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    @route("/api/workspaces/<int:workspace_id>", auth="user", methods=["POST"], type="json")
    def api_workspace_detail(self, workspace_id: int, action: str = 'get', name: str | None = None, description: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        Handle single workspace operations via JSON-RPC.

        Args:
            workspace_id (int): Target workspace ID (from URL path)
            action (str): Operation type. One of:
                - 'get': Get workspace details
                - 'update': Update workspace name/description
                - 'delete': Archive a workspace (soft delete)
            name (str, optional): Workspace name (optional for update)
            description (str, optional): Workspace description
            **kwargs: Additional parameters (ignored)

        Returns:
            dict: JSON response with structure:
                - success (bool): Whether the operation succeeded
                - data (dict): Result data (on success)
                - error (str): Error message (on failure)
        """
        if action == 'get':
            return self._get_workspace(workspace_id)
        elif action == 'update':
            return self._update_workspace(workspace_id, name, description)
        elif action == 'delete':
            return self._delete_workspace(workspace_id)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    def _list_workspaces(self) -> dict[str, Any]:
        """
        Get all workspaces accessible by the current user.

        Queries workspace_access records to find all workspaces where
        the current user has any access level and the workspace is active.

        Returns:
            dict: Response containing:
                - success (bool): True
                - data (list): List of workspace objects with:
                    - id, name, description, slug, state
                    - role: User's role in this workspace
                    - member_count: Total number of members
                    - is_owner: Whether current user owns this workspace
                    - created_date: ISO format creation timestamp
                - count (int): Number of workspaces
        """
        user = request.env.user
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        # Find all workspaces where user has access
        access_records = WorkspaceAccess.search([
            ('user_id', '=', user.id),
            ('workspace_id.state', '=', 'active'),
        ])

        # Prefetch related workspace data to avoid N+1 queries
        access_records.mapped('workspace_id')

        workspaces = []
        for access in access_records:
            ws = access.workspace_id
            workspaces.append({
                'id': ws.id,
                'name': ws.name,
                'description': ws.description or '',
                'slug': ws.slug,
                'state': ws.state,
                'role': access.role,
                'member_count': ws.member_count,
                'is_owner': ws.owner_id.id == user.id,
                'created_date': ws.create_date.isoformat() if ws.create_date else None,
            })

        return {
            'success': True,
            'data': workspaces,
            'count': len(workspaces),
        }

    def _create_workspace(self, name: str | None, description: str | None) -> dict[str, Any]:
        """
        Create a new workspace with the current user as owner.

        The workspace model automatically creates an owner access record
        for the current user during creation.

        Args:
            name (str): Workspace name (required, will be stripped)
            description (str): Workspace description (optional)

        Returns:
            dict: Response containing:
                - success (bool): True on success, False on error
                - data (dict): Created workspace object (on success)
                - error (str): Error message (on failure)

        Raises:
            Validation error if name is empty after stripping.
        """
        name = (name or '').strip()
        description = (description or '').strip()

        if not name:
            return {'success': False, 'error': 'Workspace name is required'}

        Workspace = request.env['woow_paas_platform.workspace']

        try:
            workspace = Workspace.create({
                'name': name,
                'description': description,
            })

            return {
                'success': True,
                'data': {
                    'id': workspace.id,
                    'name': workspace.name,
                    'description': workspace.description or '',
                    'slug': workspace.slug,
                    'state': workspace.state,
                    'role': ROLE_OWNER,
                    'member_count': 1,
                    'is_owner': True,
                    'created_date': workspace.create_date.isoformat() if workspace.create_date else None,
                }
            }
        except Exception as e:
            _logger.error("Error creating workspace: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': 'An error occurred while creating the workspace. Please try again.'}

    def _get_workspace(self, workspace_id: int) -> dict[str, Any]:
        """
        Get detailed information for a specific workspace.

        Verifies that the current user has access to the workspace
        before returning the data.

        Args:
            workspace_id (int): Target workspace ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Workspace details including:
                    - id, name, description, slug, state
                    - role: Current user's role
                    - member_count: Total members
                    - is_owner: Ownership flag
                    - owner: Owner details (id, name, email)
                    - created_date: ISO timestamp
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace ID is required' if workspace_id is None
            - 'Workspace not found or access denied' if ID doesn't exist or no access
        """
        if not workspace_id:
            return {'success': False, 'error': 'Workspace ID is required'}

        # Validate workspace_id is an integer
        try:
            workspace_id = int(workspace_id)
        except (TypeError, ValueError):
            return {'success': False, 'error': 'Workspace not found or access denied'}

        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = workspace.check_user_access(user) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        return {
            'success': True,
            'data': {
                'id': workspace.id,
                'name': workspace.name,
                'description': workspace.description or '',
                'slug': workspace.slug,
                'state': workspace.state,
                'role': access.role,
                'member_count': workspace.member_count,
                'is_owner': workspace.owner_id.id == user.id,
                'owner': {
                    'id': workspace.owner_id.id,
                    'name': workspace.owner_id.name,
                    'email': workspace.owner_id.email,
                },
                'created_date': workspace.create_date.isoformat() if workspace.create_date else None,
            }
        }

    def _update_workspace(self, workspace_id: int, name: str | None, description: str | None) -> dict[str, Any]:
        """
        Update workspace name and/or description.

        Requires admin or owner role to perform updates.
        Only provided fields (non-None) will be updated.

        Args:
            workspace_id (int): Target workspace ID
            name (str, optional): New workspace name (cannot be empty if provided)
            description (str, optional): New workspace description

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Updated workspace (id, name, description, slug)
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace ID is required' if workspace_id is None
            - 'Workspace not found or access denied' if ID doesn't exist or no access
            - 'Workspace name cannot be empty' if name is empty string
        """
        if not workspace_id:
            return {'success': False, 'error': 'Workspace ID is required'}

        # Validate workspace_id is an integer
        try:
            workspace_id = int(workspace_id)
        except (TypeError, ValueError):
            return {'success': False, 'error': 'Workspace not found or access denied'}

        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = workspace.check_user_access(user, required_role=ROLE_ADMIN) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        update_vals = {}
        if name is not None:
            name = name.strip()
            if not name:
                return {'success': False, 'error': 'Workspace name cannot be empty'}
            update_vals['name'] = name
        if description is not None:
            update_vals['description'] = description.strip()

        if update_vals:
            workspace.write(update_vals)

        return {
            'success': True,
            'data': {
                'id': workspace.id,
                'name': workspace.name,
                'description': workspace.description or '',
                'slug': workspace.slug,
            }
        }

    def _delete_workspace(self, workspace_id: int) -> dict[str, Any]:
        """
        Archive a workspace (soft delete).

        Only the workspace owner can delete/archive a workspace.
        This performs a soft delete by setting state to 'archived'
        rather than permanently removing the record.

        Args:
            workspace_id (int): Target workspace ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - message (str): 'Workspace archived' (on success)
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace ID is required' if workspace_id is None
            - 'Workspace not found or access denied' if ID doesn't exist or not owner
        """
        if not workspace_id:
            return {'success': False, 'error': 'Workspace ID is required'}

        # Validate workspace_id is an integer
        try:
            workspace_id = int(workspace_id)
        except (TypeError, ValueError):
            return {'success': False, 'error': 'Workspace not found or access denied'}

        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)

        # Combine existence and ownership check to prevent information leakage
        if not workspace.exists() or workspace.owner_id.id != user.id:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        workspace.action_archive()
        return {'success': True, 'message': 'Workspace archived'}

    # ==================== Workspace Members API ====================

    @route("/api/workspaces/<int:workspace_id>/members", auth="user", methods=["POST"], type="json")
    def api_workspace_members(self, workspace_id: int, action: str = 'list', email: str | None = None, role: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        Handle workspace member collection operations via JSON-RPC.

        Args:
            workspace_id (int): Target workspace ID (from URL path)
            action (str): Operation type - 'list' or 'invite'
            email (str, optional): User email to invite (for invite)
            role (str, optional): Role to assign ('admin', 'user', 'guest')

        Returns:
            dict: JSON response with success/error structure
        """
        if action == 'list':
            return self._list_members(workspace_id)
        elif action == 'invite':
            return self._invite_member(workspace_id, email, role)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    @route("/api/workspaces/<int:workspace_id>/members/<int:access_id>", auth="user", methods=["POST"], type="json")
    def api_workspace_member(self, workspace_id: int, access_id: int, action: str = 'update_role', role: str | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        Handle individual workspace member operations via JSON-RPC.

        Args:
            workspace_id (int): Target workspace ID (from URL path)
            access_id (int): Target access record ID (from URL path)
            action (str): Operation type - 'update_role' or 'remove'
            role (str, optional): New role to assign ('admin', 'user', 'guest')

        Returns:
            dict: JSON response with success/error structure
        """
        if action == 'update_role':
            return self._update_member_role(workspace_id, access_id, role)
        elif action == 'remove':
            return self._remove_member(workspace_id, access_id)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    def _list_members(self, workspace_id: int) -> dict[str, Any]:
        """
        Get all members of a workspace.

        Any user with access to the workspace can list its members.

        Args:
            workspace_id (int): Target workspace ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (list): List of member objects with:
                    - id: Access record ID
                    - user_id: User's ID
                    - name: User's display name
                    - email: User's email
                    - role: Member's role (owner/admin/user/guest)
                    - invited_by: Name of user who invited this member
                    - invited_date: ISO timestamp of invitation
                - count (int): Number of members

        Errors:
            - 'Workspace not found or access denied' if ID doesn't exist or no access
        """
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = workspace.check_user_access(user) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        members = []
        for member_access in workspace.access_ids:
            members.append({
                'id': member_access.id,
                'user_id': member_access.user_id.id,
                'name': member_access.user_name,
                'email': member_access.user_email,
                'role': member_access.role,
                'invited_by': member_access.invited_by_id.name if member_access.invited_by_id else None,
                'invited_date': member_access.invited_date.isoformat() if member_access.invited_date else None,
            })

        return {
            'success': True,
            'data': members,
            'count': len(members),
        }

    def _invite_member(self, workspace_id: int, email: str | None, role: str | None) -> dict[str, Any]:
        """
        Invite a new member to a workspace by email.

        Requires admin or owner role to invite. The target user must
        already exist in the system (registered Odoo user).

        Args:
            workspace_id (int): Target workspace ID
            email (str): Email address of user to invite
            role (str): Role to assign ('admin', 'user', 'guest')
                        Defaults to 'user' if not specified

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): New access record (id, user_id, name, email, role)
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace not found or access denied' if not admin/owner or workspace doesn't exist
            - 'Email is required' if email is empty
            - 'Invalid role' if role not in allowed values
            - 'No user found with email: {email}' if user doesn't exist
            - 'User is already a member of this workspace' if duplicate
        """
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = workspace.check_user_access(user, required_role=ROLE_ADMIN) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        email = (email or '').strip().lower()
        role = role or ROLE_USER

        if not email:
            return {'success': False, 'error': 'Email is required'}

        if role not in ASSIGNABLE_ROLES:
            return {'success': False, 'error': 'Invalid role'}

        # Find user by email
        ResUsers = request.env['res.users'].sudo()
        target_user = ResUsers.search([('email', '=ilike', email)], limit=1)

        if not target_user:
            return {'success': False, 'error': f'No user found with email: {email}'}

        # Check if already a member
        existing = WorkspaceAccess.search([
            ('workspace_id', '=', workspace_id),
            ('user_id', '=', target_user.id),
        ], limit=1)

        if existing:
            return {'success': False, 'error': 'User is already a member of this workspace'}

        try:
            new_access = WorkspaceAccess.create({
                'workspace_id': workspace_id,
                'user_id': target_user.id,
                'role': role,
                'invited_by_id': user.id,
            })

            return {
                'success': True,
                'data': {
                    'id': new_access.id,
                    'user_id': target_user.id,
                    'name': target_user.name,
                    'email': target_user.email,
                    'role': role,
                }
            }
        except Exception as e:
            _logger.error("Error inviting member: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': 'An error occurred while inviting the member. Please try again.'}

    def _update_member_role(self, workspace_id: int, access_id: int, role: str | None) -> dict[str, Any]:
        """
        Update a workspace member's role.

        Requires admin or owner role. Cannot modify the owner's role
        (use transfer ownership instead).

        Args:
            workspace_id (int): Target workspace ID
            access_id (int): Access record ID to update
            role (str): New role ('admin', 'user', 'guest')

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Updated access record (id, role)
                - error (str): Error message (on failure)

        Errors:
            - 'Workspace not found or access denied' if workspace doesn't exist or lacks permission
            - 'Access ID is required' if access_id is None
            - 'Member not found' if access record doesn't exist
            - 'Invalid role' if role not in allowed values
            - 'Cannot change owner role...' if trying to modify owner
        """
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = workspace.check_user_access(user, required_role=ROLE_ADMIN) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        if not access_id:
            return {'success': False, 'error': 'Access ID is required'}

        target_access = WorkspaceAccess.browse(access_id)
        if not target_access.exists() or target_access.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Member not found'}

        if role not in ASSIGNABLE_ROLES:
            return {'success': False, 'error': 'Invalid role'}

        # Cannot change owner's role directly
        if target_access.role == ROLE_OWNER:
            return {'success': False, 'error': 'Cannot change owner role. Use transfer ownership instead.'}

        target_access.write({'role': role})
        return {
            'success': True,
            'data': {
                'id': target_access.id,
                'role': role,
            }
        }

    def _remove_member(self, workspace_id: int, access_id: int) -> dict[str, Any]:
        """
        Remove a member from a workspace.

        Requires admin or owner role. Cannot remove the workspace owner.
        This permanently deletes the access record.

        Args:
            workspace_id (int): Target workspace ID
            access_id (int): Access record ID to remove

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - message (str): 'Member removed' (on success)
                - error (str): Error message (on failure)

        Errors:
            - 'Access ID is required' if access_id is None
            - 'Workspace not found or access denied' if workspace doesn't exist or lacks permission
            - 'Member not found' if access record doesn't exist
            - 'Cannot remove the workspace owner' if target is owner
        """
        if not access_id:
            return {'success': False, 'error': 'Access ID is required'}

        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)

        # Combine existence and access check to prevent information leakage
        access = workspace.check_user_access(user, required_role=ROLE_ADMIN) if workspace.exists() else False
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        target_access = WorkspaceAccess.browse(access_id)
        if not target_access.exists() or target_access.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Member not found'}

        # Cannot remove owner
        if target_access.role == ROLE_OWNER:
            return {'success': False, 'error': 'Cannot remove the workspace owner'}

        target_access.unlink()
        return {'success': True, 'message': 'Member removed'}

    # ==================== Cloud Templates API ====================

    @route("/api/cloud/templates", auth="user", methods=["POST"], type="json")
    def api_cloud_templates(self, category: str | None = None, search: str | None = None, **kw: Any) -> dict[str, Any]:
        """
        List available cloud application templates.

        Args:
            category (str, optional): Filter by category
            search (str, optional): Search in name/description

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (list): List of template objects
                - count (int): Number of templates
        """
        CloudAppTemplate = request.env['woow_paas_platform.cloud_app_template']

        domain = [('is_active', '=', True)]

        if category:
            domain.append(('category', '=', category))

        if search:
            search = search.strip()
            domain.append('|')
            domain.append(('name', 'ilike', search))
            domain.append(('description', 'ilike', search))

        templates = CloudAppTemplate.search(domain)

        data = []
        for tmpl in templates:
            data.append(self._format_template(tmpl))

        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    @route("/api/cloud/templates/<int:template_id>", auth="user", methods=["POST"], type="json")
    def api_cloud_template(self, template_id: int, **kw: Any) -> dict[str, Any]:
        """
        Get a single cloud application template by ID.

        Args:
            template_id (int): Template ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Template details
                - error (str): Error message (on failure)
        """
        CloudAppTemplate = request.env['woow_paas_platform.cloud_app_template']

        template = CloudAppTemplate.browse(template_id)
        if not template.exists() or not template.is_active:
            return {'success': False, 'error': 'Template not found'}

        return {
            'success': True,
            'data': self._format_template(template, include_values=True),
        }

    def _format_template(self, template: Any, include_values: bool = False) -> dict[str, Any]:
        """Format a template record for API response."""
        data = {
            'id': template.id,
            'name': template.name,
            'slug': template.slug or '',
            'description': template.description or '',
            'category': template.category,
            'tags': json.loads(template.tags) if template.tags else [],
            'monthly_price': template.monthly_price,
            'documentation_url': template.documentation_url or '',
            'default_port': template.default_port,
            'ingress_enabled': template.ingress_enabled,
            'min_vcpu': template.min_vcpu,
            'min_ram_gb': template.min_ram_gb,
            'min_storage_gb': template.min_storage_gb,
        }
        if include_values:
            data['helm_chart_name'] = template.helm_chart_name
            data['helm_chart_version'] = template.helm_chart_version
            data['helm_default_values'] = json.loads(template.helm_default_values) if template.helm_default_values else {}
            data['helm_value_specs'] = self._parse_helm_value_specs(template)
            data['full_description'] = template.full_description or ''
        return data

    # ==================== Cloud Services API ====================

    @route("/api/workspaces/<int:workspace_id>/services", auth="user", methods=["POST"], type="json")
    def api_workspace_services(self, workspace_id: int, action: str = 'list', template_id: int | None = None, name: str | None = None, values: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        """
        Handle cloud service operations for a workspace.

        Args:
            workspace_id (int): Workspace ID
            action (str): 'list' or 'create'
            template_id (int, optional): Template ID (for create)
            name (str, optional): Service name (for create)
            values (dict, optional): Helm values override (for create)

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict|list): Service(s) data
                - error (str): Error message (on failure)
        """
        # Validate workspace access
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found or access denied'}

        access = workspace.check_user_access(user)
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        if action == 'list':
            return self._list_services(workspace)
        elif action == 'create':
            # Only admin/owner can create services
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': 'Permission denied'}
            return self._create_service(workspace, template_id, name, values)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>", auth="user", methods=["POST"], type="json")
    def api_workspace_service(self, workspace_id: int, service_id: int, action: str = 'get', values: dict[str, Any] | None = None, version: str | None = None, **kw: Any) -> dict[str, Any]:
        """
        Handle operations on a specific cloud service.

        Args:
            workspace_id (int): Workspace ID
            service_id (int): Service ID
            action (str): 'get', 'update', or 'delete'
            values (dict, optional): Helm values (for update)
            version (str, optional): Chart version (for update)

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (dict): Service data
                - error (str): Error message (on failure)
        """
        # Validate workspace and service access
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        CloudService = request.env['woow_paas_platform.cloud_service']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found or access denied'}

        access = workspace.check_user_access(user)
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        service = CloudService.browse(service_id)
        if not service.exists() or service.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Service not found'}

        if action == 'get':
            return self._get_service(service)
        elif action == 'update':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': 'Permission denied'}
            return self._update_service(service, values, version)
        elif action == 'delete':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': 'Permission denied'}
            return self._delete_service(service)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>/rollback", auth="user", methods=["POST"], type="json")
    def api_service_rollback(self, workspace_id: int, service_id: int, revision: int | None = None, **kw: Any) -> dict[str, Any]:
        """
        Rollback a service to a previous revision.

        Args:
            workspace_id (int): Workspace ID
            service_id (int): Service ID
            revision (int, optional): Target revision number. If not provided or 0,
                rolls back to the previous revision.

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - message (str): Rollback confirmation
                - error (str): Error message (on failure)
        """
        # Validate revision parameter
        if revision is not None:
            try:
                revision = int(revision)
                if revision < 0:
                    return {'success': False, 'error': 'Revision must be a non-negative integer'}
            except (TypeError, ValueError):
                return {'success': False, 'error': 'Invalid revision number'}

        # Validate workspace and service access
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        CloudService = request.env['woow_paas_platform.cloud_service']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found or access denied'}

        access = workspace.check_user_access(user, required_role=ROLE_ADMIN)
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        service = CloudService.browse(service_id)
        if not service.exists() or service.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Service not found'}

        return self._rollback_service(service, revision)

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>/revisions", auth="user", methods=["POST"], type="json")
    def api_service_revisions(self, workspace_id: int, service_id: int, **kw: Any) -> dict[str, Any]:
        """
        Get revision history for a service.

        Args:
            workspace_id (int): Workspace ID
            service_id (int): Service ID

        Returns:
            dict: Response containing:
                - success (bool): True on success
                - data (list): List of revisions
                - error (str): Error message (on failure)
        """
        # Validate workspace and service access
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        CloudService = request.env['woow_paas_platform.cloud_service']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found or access denied'}

        access = workspace.check_user_access(user)
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        service = CloudService.browse(service_id)
        if not service.exists() or service.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Service not found'}

        return self._get_service_revisions(service)

    # ==================== MCP Server API (per-service) ====================

    @route(
        "/api/workspaces/<int:workspace_id>/services/<int:service_id>/mcp-servers",
        auth="user", methods=["POST"], type="json",
    )
    def api_service_mcp_servers(
        self,
        workspace_id: int,
        service_id: int,
        action: str = 'list',
        **kw: Any,
    ) -> dict[str, Any]:
        """CRUD operations for user-scope MCP servers on a cloud service."""
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        CloudService = request.env['woow_paas_platform.cloud_service']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found or access denied'}

        access = workspace.check_user_access(user)
        if not access:
            return {'success': False, 'error': 'Workspace not found or access denied'}

        service = CloudService.browse(service_id)
        if not service.exists() or service.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Service not found'}

        if action == 'list':
            return self._list_mcp_servers(service)
        elif action == 'create':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': 'Permission denied'}
            return self._create_mcp_server(service, kw)
        elif action == 'update':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': 'Permission denied'}
            return self._update_mcp_server(service, kw)
        elif action == 'delete':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': 'Permission denied'}
            return self._delete_mcp_server(service, kw)
        elif action == 'sync':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': 'Permission denied'}
            return self._sync_mcp_server(service, kw)
        elif action == 'test':
            return self._test_mcp_server(service, kw)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    def _list_mcp_servers(self, service) -> dict[str, Any]:
        servers = service.sudo().user_mcp_server_ids.filtered(lambda s: s.active)
        return {
            'success': True,
            'data': [self._format_mcp_server(s) for s in servers],
        }

    def _create_mcp_server(self, service, params: dict) -> dict[str, Any]:
        name = params.get('name', '').strip()
        url = params.get('url', '').strip()
        if not name or not url:
            return {'success': False, 'error': 'Name and URL are required'}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.create({
            'name': name,
            'url': url,
            'transport': params.get('transport', 'sse'),
            'api_key': params.get('api_key', ''),
            'headers_json': params.get('headers_json', ''),
            'description': params.get('description', ''),
            'scope': 'user',
            'cloud_service_id': service.id,
        })
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _update_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': 'server_id is required'}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.cloud_service_id.id != service.id:
            return {'success': False, 'error': 'MCP Server not found'}

        vals = {}
        for field in ('name', 'url', 'transport', 'api_key', 'headers_json', 'description'):
            if field in params:
                vals[field] = params[field]
        if vals:
            server.write(vals)
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _delete_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': 'server_id is required'}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.cloud_service_id.id != service.id:
            return {'success': False, 'error': 'MCP Server not found'}

        server.unlink()
        return {'success': True}

    def _sync_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': 'server_id is required'}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.cloud_service_id.id != service.id:
            return {'success': False, 'error': 'MCP Server not found'}

        try:
            server.action_sync_tools()
        except Exception as e:
            _logger.warning('MCP sync failed for server %s: %s', server.name, e)
            return {'success': False, 'error': f'Sync failed: {e}'}

        return {'success': True, 'data': self._format_mcp_server(server)}

    def _test_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': 'server_id is required'}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.cloud_service_id.id != service.id:
            return {'success': False, 'error': 'MCP Server not found'}

        try:
            server.action_test_connection()
        except Exception as e:
            return {'success': False, 'error': f'Connection test failed: {e}'}

        return {
            'success': True,
            'data': {
                'state': server.state,
                'state_message': server.state_message or '',
            },
        }

    @staticmethod
    def _format_mcp_server(server) -> dict[str, Any]:
        tools = []
        for t in server.tool_ids.filtered(lambda x: x.active):
            tools.append({
                'id': t.id,
                'name': t.name,
                'description': t.description or '',
            })
        return {
            'id': server.id,
            'name': server.name,
            'url': server.url,
            'transport': server.transport,
            'description': server.description or '',
            'state': server.state,
            'state_message': server.state_message or '',
            'tool_count': server.tool_count,
            'tools': tools,
            'last_sync': server.last_sync.isoformat() if server.last_sync else None,
        }

    # ==================== Cloud Service Helpers ====================

    @staticmethod
    def _unflatten_dotpath_keys(flat_dict: dict[str, Any]) -> dict[str, Any]:
        """Convert flat dot-path keys to nested dict structure.

        Example: {"a.b.c": 1, "a.b.d": 2} → {"a": {"b": {"c": 1, "d": 2}}}
        Keys without dots are kept as-is.
        """
        result = {}
        for key, value in flat_dict.items():
            parts = key.split('.')
            if len(parts) == 1:
                result[key] = value
                continue
            d = result
            for part in parts[:-1]:
                if part not in d or not isinstance(d[part], dict):
                    d[part] = {}
                d = d[part]
            d[parts[-1]] = value
        return result

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """Deep merge override into base. Override values win on conflict."""
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = PaasController._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _parse_helm_value_specs(self, template: Any) -> dict[str, list]:
        """Parse helm_value_specs JSON from a template record.

        Args:
            template: CloudAppTemplate record

        Returns:
            dict with 'required' and 'optional' lists, or empty dict if no specs
        """
        if not template or not template.helm_value_specs:
            return {}
        try:
            return json.loads(template.helm_value_specs)
        except (json.JSONDecodeError, TypeError):
            _logger.warning("Invalid helm_value_specs for template %s", template.name)
            return {}

    def _build_mcp_sidecar_config(self, template: Any, auth_token: str, api_key: str | None = None) -> dict[str, Any]:
        """Build MCP sidecar container config for PaaS Operator patch endpoint.

        Constructs the sidecar container spec from the template's MCP fields,
        merging any custom environment variables from `template.mcp_sidecar_env`
        with the required environment variables.

        Args:
            template: CloudAppTemplate record with MCP fields
            auth_token: Generated AUTH_TOKEN for sidecar authentication
            api_key: Optional API key for the main application (e.g., N8N_API_KEY)

        Returns:
            Dict matching the SidecarPatchRequest schema expected by PaaS Operator
        """
        sidecar_port = template.mcp_sidecar_port or 3000

        # Required env vars for the MCP sidecar
        env_vars = [
            {'name': 'MCP_MODE', 'value': 'http'},
            {'name': 'AUTH_TOKEN', 'value': auth_token},
            {'name': 'N8N_API_URL', 'value': f'http://localhost:{template.default_port}'},
            {'name': 'PORT', 'value': str(sidecar_port)},
        ]

        # Inject auto-generated API key for the main application
        if api_key and template.mcp_api_key_helm_path:
            env_name = template.mcp_api_key_helm_path.rsplit('.', 1)[-1]
            env_vars.append({'name': env_name, 'value': api_key})

        # Merge custom env vars from template (if any)
        if template.mcp_sidecar_env:
            try:
                custom_env = json.loads(template.mcp_sidecar_env)
                # custom_env is a dict like {"KEY": "VALUE"}
                # Required keys that should not be overridden
                reserved_keys = {'MCP_MODE', 'AUTH_TOKEN', 'PORT', 'N8N_API_URL'}
                if api_key and template.mcp_api_key_helm_path:
                    reserved_keys.add(template.mcp_api_key_helm_path.rsplit('.', 1)[-1])
                for key, value in custom_env.items():
                    if key not in reserved_keys:
                        env_vars.append({'name': key, 'value': str(value)})
            except (json.JSONDecodeError, TypeError):
                _logger.warning("Invalid MCP sidecar env JSON in template %s", template.name)

        container_spec = {
            'name': 'mcp-sidecar',
            'image': template.mcp_sidecar_image,
            'ports': [{'containerPort': sidecar_port}],
            'env': env_vars,
            'resources': {
                'requests': {'memory': '128Mi', 'cpu': '100m'},
                'limits': {'memory': '256Mi', 'cpu': '200m'},
            },
            'livenessProbe': {
                'httpGet': {'path': '/health', 'port': sidecar_port},
                'initialDelaySeconds': 15,
                'periodSeconds': 30,
            },
            'readinessProbe': {
                'httpGet': {'path': '/health', 'port': sidecar_port},
                'initialDelaySeconds': 10,
                'periodSeconds': 10,
            },
        }

        return {'container': container_spec}

    def _filter_allowed_helm_values(self, values: dict[str, Any] | None, template: Any) -> tuple[dict[str, Any], list[str]]:
        """
        Filter Helm values to only include keys allowed by template's value_specs.

        This prevents users from overriding critical system values like namespace,
        resource limits, or security settings that should be controlled by the template.

        Args:
            values (dict): User-provided Helm values
            template: CloudAppTemplate record with helm_value_specs

        Returns:
            tuple: (filtered_values dict, rejected_keys list)
        """
        if not values:
            return {}, []

        specs = self._parse_helm_value_specs(template)
        if not specs:
            # No specs defined - allow all values (backward compatibility)
            return values, []

        # Build set of allowed keys from specs
        allowed_keys = set()

        # Get keys from required and optional fields
        for field_list in [specs.get('required', []), specs.get('optional', [])]:
            for field in field_list:
                if isinstance(field, dict) and 'key' in field:
                    allowed_keys.add(field['key'])
                elif isinstance(field, str):
                    allowed_keys.add(field)

        if not allowed_keys:
            # No keys defined in specs - allow all
            return values, []

        # Filter values to only allowed keys
        filtered = {}
        rejected = []
        for key, value in values.items():
            if key in allowed_keys:
                filtered[key] = value
            else:
                rejected.append(key)

        if rejected:
            _logger.warning(
                "Rejected unauthorized Helm value keys %s for template %s",
                rejected, template.name
            )

        return filtered, rejected

    def _list_services(self, workspace: Any) -> dict[str, Any]:
        """List all services in a workspace."""
        CloudService = request.env['woow_paas_platform.cloud_service']

        services = CloudService.search([
            ('workspace_id', '=', workspace.id),
        ])

        data = []
        for svc in services:
            data.append(self._format_service(svc))

        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    def _create_service(self, workspace: Any, template_id: int | None, name: str | None, values: dict[str, Any] | None) -> dict[str, Any]:
        """Create a new cloud service."""
        if not template_id:
            return {'success': False, 'error': 'Template ID is required'}
        if not name:
            return {'success': False, 'error': 'Service name is required'}

        name = name.strip()
        if not name:
            return {'success': False, 'error': 'Service name is required'}

        CloudAppTemplate = request.env['woow_paas_platform.cloud_app_template']
        CloudService = request.env['woow_paas_platform.cloud_service']

        template = CloudAppTemplate.browse(template_id)
        if not template.exists() or not template.is_active:
            return {'success': False, 'error': 'Template not found'}

        # Always generate reference_id server-side (never accept from frontend)
        reference_id = str(uuid.uuid4())
        # Generate subdomain with salted hash: paas-{ws_id}-{hash(reference_id + name)[:8]}
        # Using reference_id as salt prevents subdomain guessing from service name alone
        salted_input = reference_id + name
        name_hash = hashlib.md5(salted_input.encode()).hexdigest()[:8]
        subdomain = f"paas-{workspace.id}-{name_hash}"
        helm_release_name = f"svc-{reference_id[:8]}"
        helm_namespace = f"paas-ws-{workspace.id}"

        # Filter user values to only allowed keys, then merge with defaults
        # Note: silently filter on create (frontend may send defaults alongside user values)
        default_values = json.loads(template.helm_default_values) if template.helm_default_values else {}
        filtered_user_values, _rejected = self._filter_allowed_helm_values(values, template)
        nested_user_values = self._unflatten_dotpath_keys(filtered_user_values)
        merged_values = self._deep_merge(default_values, nested_user_values)

        # Generate per-service API key for MCP sidecar ↔ main container communication
        mcp_api_key = None
        if template.mcp_enabled and template.mcp_api_key_helm_path:
            mcp_api_key = str(uuid.uuid4())
            # Inject into Helm values at the configured dot-path
            api_key_nested = self._unflatten_dotpath_keys(
                {template.mcp_api_key_helm_path: mcp_api_key}
            )
            merged_values = self._deep_merge(merged_values, api_key_nested)

        try:
            # Create service record in pending state
            service = CloudService.create({
                'workspace_id': workspace.id,
                'template_id': template.id,
                'name': name,
                'reference_id': reference_id,
                'state': 'pending',
                'subdomain': subdomain,
                'internal_port': template.default_port,
                'helm_release_name': helm_release_name,
                'helm_namespace': helm_namespace,
                'helm_values': json.dumps(merged_values),
                'helm_chart_version': template.helm_chart_version,
                'allocated_vcpu': template.min_vcpu,
                'allocated_ram_gb': template.min_ram_gb,
                'allocated_storage_gb': template.min_storage_gb,
            })

            # Get PaaS Operator client
            client = get_paas_operator_client(request.env)
            if not client:
                service.write({
                    'state': 'error',
                    'error_message': 'PaaS Operator not configured. Contact administrator.',
                })
                return {
                    'success': True,
                    'data': self._format_service(service),
                    'warning': 'PaaS Operator not configured',
                }

            try:
                # Create namespace if needed
                # Calculate total resource needs for ALL services in this workspace
                # Include all states except 'deleting' to account for deployed/pending resources
                all_services = CloudService.search([
                    ('workspace_id', '=', workspace.id),
                    ('state', '!=', 'deleting'),
                ])
                total_vcpu = sum(s.allocated_vcpu for s in all_services)
                total_ram = sum(s.allocated_ram_gb for s in all_services)
                total_storage = sum(s.allocated_storage_gb for s in all_services)
                # Each Helm chart may create additional PVCs (e.g. PostgreSQL sub-chart)
                # Use 3x headroom to account for sub-chart PVCs and overhead
                try:
                    client.create_namespace(
                        namespace=helm_namespace,
                        cpu_limit=str(max(total_vcpu * 3, 8)),
                        memory_limit=f"{max(int(total_ram * 3), 8)}Gi",
                        storage_limit=f"{max(total_storage * 3, 100)}Gi",
                    )
                except PaaSOperatorError as e:
                    # Namespace might already exist (409), which is fine
                    if e.status_code != 409:
                        _logger.error("Namespace creation failed: %s", str(e))
                        service.write({
                            'state': 'error',
                            'error_message': f'Failed to create namespace: {e.detail or e.message}',
                        })
                        return {
                            'success': True,
                            'data': self._format_service(service),
                            'warning': 'Namespace creation failed',
                        }

                # Build expose configuration for Cloudflare Tunnel
                expose_config = None
                _logger.info(
                    "Template %s (id=%d): ingress_enabled=%s, default_port=%s",
                    template.name, template.id, template.ingress_enabled, template.default_port
                )
                if template.ingress_enabled:
                    # Note: Don't pass service_port - let PaaS Operator auto-detect
                    # from K8s Service (which may map port 80 -> container port)
                    expose_config = {
                        'enabled': True,
                        'subdomain': subdomain,
                    }
                    _logger.info("Built expose_config: %s", expose_config)
                else:
                    _logger.info("Skipping expose_config: ingress_enabled is False")

                # Install Helm release
                release_info = client.install_release(
                    namespace=helm_namespace,
                    release_name=helm_release_name,
                    chart=template.helm_chart_name,
                    repo_url=template.helm_repo_url,
                    version=template.helm_chart_version,
                    values=merged_values,
                    create_namespace=True,  # Let operator handle namespace if needed
                    expose=expose_config,
                )

                # After Helm install, patch sidecar if MCP enabled
                if template.mcp_enabled and template.mcp_sidecar_image:
                    mcp_auth_token = str(uuid.uuid4())
                    sidecar_config = self._build_mcp_sidecar_config(template, mcp_auth_token, mcp_api_key)
                    try:
                        client.patch_sidecar(
                            namespace=helm_namespace,
                            release_name=helm_release_name,
                            sidecar_config=sidecar_config,
                        )
                        # Store auth_token in service for later MCP Server creation
                        service.write({'mcp_auth_token': mcp_auth_token})
                        _logger.info(
                            "MCP sidecar patched for service %s (release=%s)",
                            service.name, helm_release_name,
                        )
                    except PaaSOperatorError as e:
                        _logger.warning("Failed to patch MCP sidecar: %s", e)
                        # Don't fail the deployment, sidecar is optional

                # Update service state
                service.write({
                    'state': 'deploying',
                    'helm_revision': release_info.get('revision', 1),
                    'deployed_at': datetime.now(),
                })

            except PaaSOperatorConnectionError as e:
                _logger.error("Operator connection error: %s", str(e))
                service.write({
                    'state': 'error',
                    'error_message': 'Unable to connect to deployment service. Please try again later.',
                })

            except PaaSOperatorError as e:
                _logger.error("Operator error during deployment: %s", str(e))
                service.write({
                    'state': 'error',
                    'error_message': f'Deployment failed: {e.detail or e.message}',
                })

            return {
                'success': True,
                'data': self._format_service(service),
            }

        except Exception as e:
            _logger.error("Error creating service: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': 'An error occurred while creating the service.'}

    def _get_service(self, service: Any) -> dict[str, Any]:
        """Get service details, updating status from operator if needed."""
        # Check if we need to poll operator for status
        if service.state in ['deploying', 'upgrading']:
            self._update_service_status(service)

        return {
            'success': True,
            'data': self._format_service(service, include_details=True),
        }

    def _update_service(self, service: Any, values: dict[str, Any] | None, version: str | None) -> dict[str, Any]:
        """Update/upgrade a service."""
        if service.state in ['pending', 'deleting', 'error']:
            return {'success': False, 'error': f'Cannot update service in {service.state} state'}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': 'PaaS Operator not configured'}

        try:
            # Filter user values to only allowed keys, reject unauthorized
            existing_values = json.loads(service.helm_values) if service.helm_values else {}
            filtered_user_values, rejected_keys = self._filter_allowed_helm_values(values, service.template_id)
            if rejected_keys:
                return {'success': False, 'error': f'Unauthorized configuration keys: {", ".join(rejected_keys)}'}
            nested_user_values = self._unflatten_dotpath_keys(filtered_user_values)
            merged_values = self._deep_merge(existing_values, nested_user_values)

            # Upgrade release
            release_info = client.upgrade_release(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                chart=service.template_id.helm_chart_name,
                values=merged_values,
                version=version,
            )

            service.write({
                'state': 'upgrading',
                'helm_values': json.dumps(merged_values),
                'helm_revision': release_info.get('revision', service.helm_revision + 1),
                'helm_chart_version': version or service.helm_chart_version,
                'last_upgraded_at': datetime.now(),
                'error_message': False,
            })

            return {
                'success': True,
                'data': self._format_service(service),
            }

        except PaaSOperatorConnectionError:
            return {'success': False, 'error': 'Unable to connect to deployment service'}

        except PaaSOperatorError as e:
            _logger.error("Operator error during upgrade: %s", str(e))
            return {'success': False, 'error': f'Upgrade failed: {e.detail or e.message}'}

    def _delete_service(self, service: Any) -> dict[str, Any]:
        """Delete/uninstall a service."""
        if service.state == 'deleting':
            return {'success': False, 'error': 'Service is already being deleted'}

        client = get_paas_operator_client(request.env)
        if not client:
            # Block deletion - we cannot clean up K8s resources without operator
            _logger.error(
                "Cannot delete service %s: PaaS Operator not configured. "
                "Kubernetes resources will NOT be cleaned up.",
                service.name
            )
            return {
                'success': False,
                'error': 'PaaS Operator not configured. Cannot safely delete service without cleaning up Kubernetes resources. Contact administrator.',
            }

        try:
            service.write({'state': 'deleting'})

            # Uninstall Helm release (pass subdomain for Cloudflare cleanup)
            client.uninstall_release(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                subdomain=service.subdomain,
            )

            # Delete the record
            service.unlink()
            return {'success': True, 'message': 'Service deleted'}

        except PaaSOperatorConnectionError:
            service.write({
                'state': 'error',
                'error_message': 'Unable to connect to deployment service during deletion',
            })
            return {'success': False, 'error': 'Unable to connect to deployment service'}

        except PaaSOperatorError as e:
            # If release not found, still delete the record
            if e.status_code == 404:
                service.unlink()
                return {'success': True, 'message': 'Service deleted'}

            _logger.error("Operator error during uninstall: %s", str(e))
            service.write({
                'state': 'error',
                'error_message': f'Deletion failed: {e.detail or e.message}',
            })
            return {'success': False, 'error': f'Deletion failed: {e.detail or e.message}'}

    def _rollback_service(self, service: Any, revision: int | None) -> dict[str, Any]:
        """Rollback service to a previous revision."""
        if service.state in ['pending', 'deleting']:
            return {'success': False, 'error': f'Cannot rollback service in {service.state} state'}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': 'PaaS Operator not configured'}

        try:
            client.rollback_release(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                revision=revision,
            )

            service.write({
                'state': 'upgrading',
                'error_message': False,
            })

            return {'success': True, 'message': f'Rollback to revision {revision} initiated'}

        except PaaSOperatorConnectionError:
            return {'success': False, 'error': 'Unable to connect to deployment service'}

        except PaaSOperatorError as e:
            _logger.error("Operator error during rollback: %s", str(e))
            return {'success': False, 'error': f'Rollback failed: {e.detail or e.message}'}

    def _get_service_revisions(self, service: Any) -> dict[str, Any]:
        """Get revision history for a service."""
        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': 'PaaS Operator not configured'}

        try:
            result = client.get_revisions(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
            )

            return {
                'success': True,
                'data': result.get('revisions', []),
            }

        except PaaSOperatorConnectionError:
            return {'success': False, 'error': 'Unable to connect to deployment service'}

        except PaaSOperatorError as e:
            if e.status_code == 404:
                return {'success': True, 'data': []}
            return {'success': False, 'error': f'Failed to get revisions: {e.detail or e.message}'}

    def _update_service_status(self, service: Any) -> None:
        """Poll operator for service status and update record.

        Uses optimistic locking to prevent race conditions:
        - Re-fetches service before updating to get latest state
        - Only updates if service is still in expected state
        - Skips update if state changed (another process already updated)
        """
        client = get_paas_operator_client(request.env)
        if not client:
            return

        original_state = service.state
        service_id = service.id

        try:
            status = client.get_status(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
            )

            release = status.get('release', {})
            pods = status.get('pods', [])

            release_status = release.get('status', '')
            helm_revision = release.get('revision', service.helm_revision)

            # Re-fetch service to get latest state (optimistic locking)
            CloudService = request.env['woow_paas_platform.cloud_service']
            service = CloudService.browse(service_id)

            if not service.exists():
                _logger.debug("Service %s no longer exists, skipping status update", service_id)
                return

            # Only proceed if state hasn't changed since we started
            if service.state != original_state:
                _logger.debug(
                    "Service %s state changed from %s to %s, skipping update",
                    service_id, original_state, service.state
                )
                return

            # Determine new state based on release status and pod status
            if release_status == 'deployed':
                # Check if all pods are ready
                all_ready = all(
                    pod.get('phase') == 'Running' and '/' in pod.get('ready', '0/0')
                    and pod.get('ready', '0/0').split('/')[0] == pod.get('ready', '0/0').split('/')[1]
                    for pod in pods
                ) if pods else True

                if all_ready:
                    service.write({
                        'state': 'running',
                        'helm_revision': helm_revision,
                        'error_message': False,
                    })

                    # Auto-create MCP Server when transitioning to running
                    if original_state != 'running':
                        try:
                            self._auto_create_mcp_server(service)
                        except Exception as e:
                            _logger.warning(
                                "Auto-create MCP server failed for service %s: %s",
                                service.name, e,
                            )
                # else: still deploying/waiting for pods

            elif release_status == 'failed':
                service.write({
                    'state': 'error',
                    'helm_revision': helm_revision,
                    'error_message': release.get('description', 'Deployment failed'),
                })

            elif release_status in ['pending-install', 'pending-upgrade', 'pending-rollback']:
                # Still in progress
                pass

        except PaaSOperatorError as e:
            if e.status_code == 404:
                # Release not found - might have been deleted
                # Re-fetch to check current state
                CloudService = request.env['woow_paas_platform.cloud_service']
                service = CloudService.browse(service_id)
                if service.exists() and service.state == 'deleting':
                    service.unlink()
            else:
                _logger.warning("Error polling service status: %s", str(e))

        except Exception as e:
            _logger.warning("Error polling service status: %s", str(e))

    def _auto_create_mcp_server(self, service: Any) -> None:
        """Auto-create MCP Server record for a cloud service with MCP enabled.

        Called when a service transitions to 'running' state. Creates a
        user-scope MCP Server record linked to the cloud service and
        triggers tool discovery.

        Idempotent: skips creation if an auto-created record already exists.
        """
        template = service.template_id
        if not template.mcp_enabled or not template.mcp_sidecar_image:
            return

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()

        # Check if already exists (avoid duplicates on re-deploy/upgrade)
        existing = McpServer.search([
            ('cloud_service_id', '=', service.id),
            ('auto_created', '=', True),
        ], limit=1)
        if existing:
            _logger.debug(
                "MCP Server already exists for service %s (id=%s), skipping auto-create",
                service.name, existing.id,
            )
            return

        # Build MCP endpoint URL
        mcp_url = self._build_mcp_endpoint_url(service, template)

        # Create MCP Server record
        server = McpServer.create({
            'name': f"{service.name} MCP",
            'url': mcp_url,
            'transport': template.mcp_transport or 'streamable_http',
            'scope': 'user',
            'cloud_service_id': service.id,
            'auto_created': True,
            'api_key': service.mcp_auth_token,
            'description': f"Auto-created MCP server for {service.name}",
        })

        _logger.info(
            "Auto-created MCP Server '%s' (id=%s) for cloud service '%s'",
            server.name, server.id, service.name,
        )

        # Try to sync tools using safe method (keeps state as 'draft' on
        # failure so the cron retry mechanism can pick it up later).
        server.action_sync_tools_safe()

    def _build_mcp_endpoint_url(self, service: Any, template: Any) -> str:
        """Build the MCP endpoint URL for a cloud service sidecar.

        Constructs the URL using the service's subdomain and the PaaS
        domain from system configuration. Falls back to a Kubernetes
        internal service URL when no subdomain is available.

        Returns:
            str: The full MCP endpoint URL.
        """
        endpoint_path = template.mcp_endpoint_path or '/mcp'
        sidecar_port = template.mcp_sidecar_port or 3001

        # Prefer Kubernetes internal service URL (most reliable).
        # The Cloudflare tunnel only routes to the main application port,
        # not the sidecar port, so external URL via subdomain won't work
        # for the MCP sidecar without additional Ingress configuration.
        # Pattern: http://{release}-mcp.{namespace}.svc.cluster.local:{port}{path}
        if service.helm_release_name and service.helm_namespace:
            return (
                f"http://{service.helm_release_name}-mcp"
                f".{service.helm_namespace}.svc.cluster.local"
                f":{sidecar_port}{endpoint_path}"
            )

        # Fallback: construct from subdomain (user can update later)
        if service.subdomain:
            IrConfigParameter = request.env['ir.config_parameter'].sudo()
            paas_domain = IrConfigParameter.get_param(
                'woow_paas_platform.paas_domain', 'woowtech.io',
            )
            return f"https://{service.subdomain}.{paas_domain}{endpoint_path}"

        # Last resort: placeholder that the user must update
        return f"http://localhost:{sidecar_port}{endpoint_path}"

    def _format_service(self, service: Any, include_details: bool = False) -> dict[str, Any]:
        """Format a service record for API response."""
        data = {
            'id': service.id,
            'name': service.name,
            'state': service.state,
            'subdomain': service.subdomain or '',
            'custom_domain': service.custom_domain or '',
            'error_message': service.error_message or '',
            'has_project': bool(service.project_ids),
            'template': {
                'id': service.template_id.id,
                'name': service.template_id.name,
                'category': service.template_id.category,
                'helm_value_specs': self._parse_helm_value_specs(service.template_id),
            },
            'helm_revision': service.helm_revision,
            'created_date': service.create_date.isoformat() if service.create_date else None,
            'deployed_at': service.deployed_at.isoformat() if service.deployed_at else None,
        }

        if include_details:
            data.update({
                'helm_namespace': service.helm_namespace,
                'helm_release_name': service.helm_release_name,
                'helm_chart_version': service.helm_chart_version,
                'helm_values': json.loads(service.helm_values) if service.helm_values else {},
                'internal_port': service.internal_port,
                'allocated_vcpu': service.allocated_vcpu,
                'allocated_ram_gb': service.allocated_ram_gb,
                'allocated_storage_gb': service.allocated_storage_gb,
                'last_upgraded_at': service.last_upgraded_at.isoformat() if service.last_upgraded_at else None,
            })

        return data
