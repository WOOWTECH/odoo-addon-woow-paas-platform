from __future__ import annotations

import ipaddress
import json
import logging
import threading
import traceback
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from odoo import fields
from odoo.exceptions import ValidationError
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


# Cloud metadata endpoints must never be reachable from a user-provided
# MCP URL. cluster.local resolves into pod-network IPs and must stay
# allowed (sidecar-backed URL form).
_SSRF_BLOCKED_IPV4 = frozenset({
    '169.254.169.254',  # AWS / GCP / Azure IMDSv1
})
_SSRF_BLOCKED_IPV6 = frozenset({
    'fd00:ec2::254',  # AWS IMDSv2 IPv6
})
_SSRF_ALLOWED_SCHEMES = frozenset({'http', 'https'})


PORTAL_SETTINGS_FLAG = 'woow_paas_platform.portal_project_settings_editable'


def _portal_project_settings_editable(env) -> bool:
    """Feature flag（2026-06-03 議題 H）：portal project settings 預設唯讀。
    Param 缺值 = off（fail-closed）。ai_assistant.py import 共用 —
    two-layer-acl-lockstep：三支寫入 endpoint 必須同一開關。"""
    return (env['ir.config_parameter'].sudo().get_param(
        PORTAL_SETTINGS_FLAG) or '').strip().lower() in ('true', '1')


SHOW_PROJECTS_FLAG = 'woow_paas_platform.show_projects'


def _show_projects(env) -> bool:
    """Cosmetic feature gate（#275）：/woow Projects/AI-assistant 可見性。
    缺值 = off（fail-closed）。**非安全邊界** — 只驅動前端 nav + router
    redirect；後端 project/task/chat API 不受此開關影響（對照
    _portal_project_settings_editable 才是 ACL 寫入守衛）。"""
    return (env['ir.config_parameter'].sudo().get_param(
        SHOW_PROJECTS_FLAG) or '').strip().lower() in ('true', '1')


def _validate_mcp_url(url: str, sidecar_backed: bool = False) -> str | None:
    """Minimal SSRF guard: reject non-http(s) schemes + cloud metadata IPs.

    #271: when *sidecar_backed* is True, additionally require an in-cluster
    sidecar DNS URL (http + '.svc.cluster.local'); a public hostname on a
    sidecar-backed row makes the in-cluster agent data-plane hit the CF
    Access login page and makes teardown's URL-prefix filter miss the row.
    """
    if not url:
        return 'URL is required'
    try:
        parsed = urlparse(url)
    except ValueError:
        return f'URL host not allowed: {url}'
    scheme = (parsed.scheme or '').lower()
    if scheme not in _SSRF_ALLOWED_SCHEMES:
        return (
            f'URL scheme not allowed: {parsed.scheme!r} '
            '(only http/https accepted)'
        )
    host = (parsed.hostname or '').strip()
    if not host:
        return f'URL host not allowed: {url}'
    if host in _SSRF_BLOCKED_IPV4:
        return f'URL host not allowed: {host}'
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.is_link_local:
            return f'URL host not allowed: [{host}]'
        if str(ip).lower() in _SSRF_BLOCKED_IPV6:
            return f'URL host not allowed: [{host}]'
    if sidecar_backed and not request.env[
        'woow_paas_platform.mcp_server'
    ]._is_in_cluster_url(url):
        return (
            'Sidecar-backed MCP URL must be in-cluster (http scheme + '
            'hostname ending in .svc.cluster.local, e.g. '
            'http://<release>-mcp.<namespace>.svc.cluster.local:3001/mcp); '
            f'got: {url}'
        )
    return None


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
        # shallow copy：不 mutate 全域 ir.http.session_info()，只在 /woow 這頁附掛。
        # #275 cosmetic gate — 每次 render 即時讀一次 config_parameter（無快取，
        # 單次頁面載入成本可忽略）；router + nav 同步共用此注入值。
        session_info = dict(request.env['ir.http'].session_info())
        session_info['woow_show_projects'] = _show_projects(request.env)
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
            'tags': self._safe_json_load(template.tags, [], 'tags', template.name),
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
            data['helm_default_values'] = self._safe_json_load(
                template.helm_default_values, {}, 'helm_default_values', template.name)
            data['helm_value_specs'] = self._parse_helm_value_specs(template)
            data['full_description'] = template.full_description or ''
        return data

    # ==================== Odoo Addons API ====================

    @route("/api/cloud/odoo-addons/sync", auth="user", methods=["POST"], type="json")
    def api_odoo_addons_sync(self, **kw: Any) -> dict[str, Any]:
        """Sync addon records from catalog.json (admin only)."""
        if not request.env.user.has_group('base.group_system'):
            return {'success': False, 'error': 'Admin access required'}
        result = request.env['woow_paas_platform.cloud_odoo_addon'].sync_from_catalog()
        return {'success': True, 'data': result}

    @route("/api/cloud/odoo-addons", auth="user", methods=["POST"], type="json")
    def api_odoo_addons(self, template_id: int | None = None, category: str | None = None, **kw: Any) -> dict[str, Any]:
        """List available Odoo addons, optionally filtered by template or category."""
        CloudOdooAddon = request.env['woow_paas_platform.cloud_odoo_addon']

        domain = [('active', '=', True)]
        if template_id:
            # Show addons linked to this template OR addons with no template restriction
            domain = [
                ('active', '=', True),
                '|',
                ('template_ids', 'in', [template_id]),
                ('template_ids', '=', False),
            ]
        if category:
            domain.append(('category', '=', category))

        addons = CloudOdooAddon.search(domain)
        data = []
        for addon in addons:
            data.append({
                'id': addon.id,
                'name': addon.name,
                'technical_name': addon.technical_name,
                'description': addon.description or '',
                'category': addon.category or 'other',
                'icon': addon.icon or '',
                'source_type': addon.source_type,
                'is_default': addon.is_default,
                'depends_on': [
                    {'id': d.id, 'technical_name': d.technical_name}
                    for d in addon.depends_on
                ],
            })

        return {'success': True, 'data': data, 'count': len(data)}

    # ==================== Cloud Services API ====================

    @route("/api/cloud-services/templates/<int:template_id>/plans", auth="user", methods=["POST"], type="json")
    def api_template_plans(self, template_id: int, **kw: Any) -> dict[str, Any]:
        """Get available subscription plans for a template."""
        CloudAppTemplate = request.env['woow_paas_platform.cloud_app_template']
        template = CloudAppTemplate.browse(template_id)
        if not template.exists() or not template.is_active:
            return {'success': False, 'error': 'Template not found'}

        plans = []
        for plan in template.sudo().subscription_plan_ids:
            plans.append({
                'id': plan.id,
                'name': plan.name,
                'price': plan.sh_plan_price,
                'duration': plan.sh_duration,
                'unit': plan.sh_unit,
                'has_trial': plan.sh_trial,
                'trial_duration': plan.sh_trial_duration if plan.sh_trial else 0,
                'trial_unit': plan.sh_trial_unit if plan.sh_trial else None,
            })

        return {'success': True, 'data': plans}

    @route("/api/cloud-services/services/<int:service_id>/change-plan", auth="user", methods=["POST"], type="json")
    def api_service_change_plan(self, service_id: int, plan_id: int | None = None, **kw: Any) -> dict[str, Any]:
        """Change subscription plan for a cloud service."""
        if not plan_id:
            return {'success': False, 'error': 'plan_id is required'}

        CloudService = request.env['woow_paas_platform.cloud_service']
        service = CloudService.browse(service_id)
        if not service.exists():
            return {'success': False, 'error': 'Service not found'}

        workspace = service.workspace_id
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {'success': False, 'error': 'Admin access required'}

        Plan = request.env['sh.subscription.plan'].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {'success': False, 'error': 'Plan not found'}
        # Plan must belong to THIS service's template — symmetric with the
        # create path's gate (api_deploy_service: `plan not in
        # template.subscription_plan_ids`). The role check above only gates who
        # may change the plan, not whether the plan belongs here; without this
        # an owner could switch to another template's (or an orphan) plan,
        # producing a subscription/invoice line inconsistent with the service's
        # template. Lock-step with the SA-side change-plan guard.
        svc_template = service.template_id.sudo()
        if svc_template.subscription_plan_ids and plan not in svc_template.subscription_plan_ids:
            return {'success': False,
                    'error': 'Plan not available for this template'}

        old_sub = service.sudo().subscription_id
        _logger.info(
            "[CLOUD_SVC] change_plan request service=%s new_plan=%s old_sub=%s",
            service_id, plan_id, old_sub.id if old_sub else None,
        )
        if not old_sub or old_sub.state != 'in_progress':
            return {'success': False, 'error': 'No active subscription'}

        # During trial, sh_end_date is legitimately NULL (billing period
        # hasn't started yet). Strict date completeness is only meaningful
        # for active subs — see the SH-side counterpart in smart_home.py
        # for the full rationale. `_compute_unused_refund` is already
        # trial-aware (returns 0), so the rest of the flow is safe.
        if not old_sub.sh_trial:
            if not old_sub.sh_end_date or not old_sub.sh_start_date:
                return {'success': False, 'error': 'Subscription dates are incomplete'}

        # Refund: defer to the model method instead of recomputing the
        # proration inline. The previous inline form
        #   refund = price * remaining_days / total_days
        # would have crashed on trial subs (sh_end_date is None) once the
        # date guard above was relaxed; the model method is the SH side's
        # canonical calculator and is already covered by
        # test_change_plan_refund.py for the trial / partial / end-of-cycle
        # cases. SH at smart_home.py:167 already routes through it; this
        # change brings CS to parity.
        refund = old_sub._compute_unused_refund()

        partner = old_sub.sh_partner_id
        _logger.info(
            "[CLOUD_SVC] change_plan refund calc service=%s start=%s end=%s price=%s refund=%s partner=%s",
            service_id, old_sub.sh_start_date, old_sub.sh_end_date,
            old_sub.sh_plan_price, refund, partner.id,
        )

        # Refund unused points (#318: refund-typed so reconciliation can tell
        # it from a top-up; carries subscription_id for audit trail)
        if refund > 0:
            partner.sudo().refund_wallet_points(
                amount=refund,
                description="Plan change refund: %s" % old_sub.sh_subscription_plan_id.name,
                subscription=old_sub,
            )
            partner.invalidate_recordset(['wallet_balance'])
            _logger.info(
                "[CLOUD_SVC] change_plan refund credited service=%s partner=%s amount=%s",
                service_id, partner.id, refund,
            )

        # Close old subscription
        old_sub.sudo().write({
            'state': 'close',
            'sh_reason': 'Plan changed to: %s' % plan.name,
        })

        # Create new subscription
        service._create_and_confirm_subscription(partner, plan)
        _logger.info(
            "[CLOUD_SVC] change_plan done service=%s old_sub=%s new_sub=%s",
            service_id, old_sub.id,
            service.subscription_id.id if service.subscription_id else None,
        )

        return {
            'success': True,
            'data': {
                'refund_amount': refund,
                'new_plan': plan.name,
                'subscription': service._get_subscription_info(),
            },
        }

    @route("/api/cloud-services/services/<int:service_id>/skip-trial-estimate", auth="user", methods=["POST"], type="json")
    def api_service_skip_trial_estimate(self, service_id: int, **kw: Any) -> dict[str, Any]:
        """Return the upcoming-invoice breakdown for skip-trial confirmation.

        Side-effect-free: computes untaxed / tax / total + wallet sufficiency
        without mutating the subscription. Used by the confirm modal so the
        displayed total matches what `api_service_skip_trial` will charge.
        """
        CloudService = request.env['woow_paas_platform.cloud_service']
        service = CloudService.browse(service_id)
        if not service.exists():
            return {'success': False, 'error': 'Service not found'}

        workspace = service.workspace_id
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {'success': False, 'error': 'Admin access required'}

        try:
            estimate = service._skip_trial_estimate()
        except Exception as e:
            _logger.error(
                "Error estimating skip-trial for service %s: %s", service_id, e,
            )
            return {'success': False, 'error': str(e)}

        return {'success': True, 'data': estimate}

    @route("/api/cloud-services/services/<int:service_id>/skip-trial", auth="user", methods=["POST"], type="json")
    def api_service_skip_trial(self, service_id: int, **kw: Any) -> dict[str, Any]:
        """Skip trial period and start paid billing immediately."""
        CloudService = request.env['woow_paas_platform.cloud_service']
        service = CloudService.browse(service_id)
        if not service.exists():
            return {'success': False, 'error': 'Service not found'}

        workspace = service.workspace_id
        access = workspace.check_user_access(request.env.user)
        if not access or access.role not in [ROLE_OWNER, ROLE_ADMIN]:
            return {'success': False, 'error': 'Admin access required'}

        try:
            invoice_info = service._skip_trial()
        except Exception as e:
            _logger.error("Error skipping trial for service %s: %s", service_id, e)
            return {'success': False, 'error': str(e)}

        return {
            'success': True,
            'data': {
                'invoice': invoice_info,
                'subscription': service._get_subscription_info(),
            },
        }

    @route("/api/workspaces/<int:workspace_id>/services", auth="user", methods=["POST"], type="json")
    def api_workspace_services(self, workspace_id: int, action: str = 'list', template_id: int | None = None, name: str | None = None, values: dict[str, Any] | None = None, odoo_addon_ids: list[int] | None = None, **kw: Any) -> dict[str, Any]:
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
            plan_id = kw.get('plan_id')
            return self._create_service(workspace, template_id, name, values, odoo_addon_ids, plan_id=plan_id)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    @route("/api/workspaces/<int:workspace_id>/services/<int:service_id>", auth="user", methods=["POST"], type="json")
    def api_workspace_service(self, workspace_id: int, service_id: int, action: str = 'get', values: dict[str, Any] | None = None, version: str | None = None, odoo_addon_ids: list[int] | None = None, **kw: Any) -> dict[str, Any]:
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
            try:
                service._check_operation_allowed(operation='write')
            except Exception as e:
                return {'success': False, 'error': str(e)}
            return self._update_service(service, values, version, odoo_addon_ids)
        elif action == 'delete':
            if access.role not in [ROLE_OWNER, ROLE_ADMIN]:
                return {'success': False, 'error': 'Permission denied'}
            try:
                service._check_operation_allowed(operation='write')
            except Exception as e:
                return {'success': False, 'error': str(e)}
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

    # ==================== Addon Module Operations ====================

    @route(
        '/api/cloud-services/services/<int:service_id>/check-addon-removal',
        type='json', auth='user', methods=['POST'],
    )
    def api_check_addon_removal(self, service_id):
        """Check if removed addons can be safely deselected."""
        params = request.get_json_data()
        removed_addon_ids = params.get('removed_addon_ids', [])

        if not removed_addon_ids:
            return {'success': True, 'modules': {}}

        service = request.env['woow_paas_platform.cloud_service'].browse(service_id)
        if not service.exists():
            return {'success': False, 'error': 'Service not found'}

        workspace = service.workspace_id
        access = workspace.check_user_access(request.env.user)
        if not access:
            return {'success': False, 'error': 'Access denied'}

        if service.state != 'running':
            return {'success': False, 'error': f'Service must be running to check module states (current: {service.state})'}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': 'PaaS Operator not configured'}

        CloudOdooAddon = request.env['woow_paas_platform.cloud_odoo_addon']
        addons = CloudOdooAddon.browse(removed_addon_ids).exists()
        if not addons:
            return {'success': True, 'modules': {}}

        tech_names = [a.technical_name for a in addons]
        name_to_id = {a.technical_name: a.id for a in addons}

        try:
            result = client.get_odoo_module_states(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                module_names=tech_names,
            )
        except Exception as e:
            return {'success': False, 'error': f'Failed to query module states: {e}'}

        if not result.get('success'):
            return {'success': False, 'error': result.get('error', 'Unknown error')}

        modules = {}
        for name, info in result.get('modules', {}).items():
            modules[name] = {
                'addon_id': name_to_id.get(name),
                'state': info.get('state', 'unknown'),
                'dependents': info.get('dependents', []),
            }

        return {'success': True, 'modules': modules}

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

        # Legacy service-centric write endpoint must honour the same portal
        # settings flag as the project-centric route (two-layer-acl-lockstep:
        # 三支寫入 endpoint 必須同一開關). Without this gate, flag-off lets a
        # workspace owner/admin RPC past read-only via the legacy route.
        write_actions = ('create', 'update', 'delete', 'sync', 'test')
        if action in write_actions:
            if not _portal_project_settings_editable(request.env):
                return {'success': False, 'error': 'Permission denied'}

        if action == 'create':
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
        # User-scope MCP now hangs off project, not service (D6 keeps the
        # frontend tab on the service page; query walks service.project_ids).
        project = service.sudo().project_ids[:1]
        if not project:
            return {'success': True, 'data': []}
        servers = project.mcp_server_ids.filtered(
            lambda s: s.scope == 'user' and s.active
        )
        return {
            'success': True,
            'data': [self._format_mcp_server(s) for s in servers],
        }

    def _create_mcp_server(self, service, params: dict) -> dict[str, Any]:
        name = params.get('name', '').strip()
        url = params.get('url', '').strip()
        if not name or not url:
            return {'success': False, 'error': 'Name and URL are required'}
        # legacy create never stores sidecar_backed → validate as external.
        url_error = _validate_mcp_url(url, sidecar_backed=False)
        if url_error:
            return {'success': False, 'error': url_error}

        project = service.sudo().project_ids[:1]
        if not project:
            return {
                'success': False,
                'error': 'No support project bound to this service.',
            }

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.create({
            'name': name,
            'url': url,
            'transport': params.get('transport', 'sse'),
            'api_key': params.get('api_key', ''),
            'headers_json': params.get('headers_json', ''),
            'description': params.get('description', ''),
            'scope': 'user',
            'project_id': project.id,
        })
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _update_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': 'server_id is required'}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.project_id.cloud_service_id.id != service.id:
            return {'success': False, 'error': 'MCP Server not found'}

        if 'url' in params:
            url_error = _validate_mcp_url(
                (params.get('url') or '').strip(),
                sidecar_backed=server.sidecar_backed,
            )
            if url_error:
                return {'success': False, 'error': url_error}

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
        if not server.exists() or server.project_id.cloud_service_id.id != service.id:
            return {'success': False, 'error': 'MCP Server not found'}

        server.unlink()
        return {'success': True}

    def _sync_mcp_server(self, service, params: dict) -> dict[str, Any]:
        server_id = params.get('server_id')
        if not server_id:
            return {'success': False, 'error': 'server_id is required'}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.project_id.cloud_service_id.id != service.id:
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
        if not server.exists() or server.project_id.cloud_service_id.id != service.id:
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
            'sidecar_backed': bool(server.sidecar_backed),
        }

    # ==================== Project-centric Support API (D4 / OR-6) ====================

    _PROJECT_NOT_FOUND_ERROR = 'Project not found or access denied'

    class _SyntheticAccess:
        """Stand-in access record for unbound projects.

        Issued only after the unbound carve-out in
        ``_resolve_project_access`` has already verified that the current
        user is a project follower — the role is granted to a
        scope-resolved user, not to anyone authenticated. Mirrors the
        follower carve-out in ``ai_assistant.py:_check_project_access``;
        both layers must stay in lock-step, otherwise the rule layer
        denies but the controller layer still serves the row.
        """
        def __init__(self, role):
            self.role = role

    def _resolve_project_access(self, project_id: int):
        """Resolve a project + workspace access record for the current user.

        Returns ``(project, access)`` when the user can reach the project,
        or ``(None, None)`` for every deny path — the unified ``Project
        not found or access denied`` error keeps id enumeration closed.

        Unbound projects (no cloud_service / workspace) require the
        current user to be in ``project.message_partner_ids`` (follower
        carve-out, in lock-step with ``ai_assistant.py``).
        """
        Project = request.env['project.project'].sudo()
        project = Project.browse(project_id)
        if not project.exists():
            return None, None
        workspace = project.cloud_service_id.workspace_id
        if not workspace:
            user_partner = request.env.user.partner_id
            if user_partner in project.message_partner_ids:
                return project, self._SyntheticAccess(role=ROLE_OWNER)
            return None, None
        access = workspace.check_user_access(request.env.user)
        if not access:
            return None, None
        return project, access

    @route(
        "/api/support-projects/<int:project_id>",
        auth="user", methods=["POST"], type="json",
    )
    def api_support_project_detail(
        self,
        project_id: int,
        action: str = 'get',
        **kw: Any,
    ) -> dict[str, Any]:
        """Project detail operations (get / update).

        URL is hyphenated (``/api/support-projects/...``) to avoid the
        ``/api/support/projects/<wid>`` slash pattern in ``ai_assistant.py``
        which scopes to a workspace, not a project — see Plan D4 / OR-6.
        """
        project, access = self._resolve_project_access(project_id)
        if not project or not access:
            return {'success': False, 'error': self._PROJECT_NOT_FOUND_ERROR}

        if action == 'get':
            return {'success': True, 'data': self._format_project_detail(project)}
        if action == 'update':
            if not _portal_project_settings_editable(request.env):
                return {'success': False, 'error': 'Permission denied'}
            if access.role not in (ROLE_OWNER, ROLE_ADMIN):
                return {'success': False, 'error': 'Permission denied'}
            return self._update_support_project(project, kw)
        return {'success': False, 'error': f'Unknown action: {action}'}

    def _format_project_detail(self, project) -> dict[str, Any]:
        servers = project.mcp_server_ids.filtered(
            lambda s: s.scope == 'user' and s.active
        )
        service = project.cloud_service_id
        return {
            'id': project.id,
            'name': project.name,
            'description': project.description or '',
            'cloud_service_id': service.id if service else False,
            'cloud_service_name': service.name if service else '',
            'cloud_service_mcp_enabled': bool(
                service and service.template_id.mcp_enabled
            ),
            # #271: sidecar flow only valid once the service has an in-cluster
            # identity (helm release + namespace). Frontend gates the
            # 'Add Sidecar MCP' button on this so an undeployed service can't
            # prefill a public sidecar URL that violates the in-cluster
            # invariant (same gate as the two sidecar URL builders).
            'cloud_service_deployed': bool(
                service and service.helm_release_name and service.helm_namespace
            ),
            'workspace_id': project.workspace_id.id if project.workspace_id else False,
            'workspace_name': project.workspace_id.name if project.workspace_id else '',
            'mcp_server_ids': [self._format_mcp_server(s) for s in servers],
            'portal_editable': _portal_project_settings_editable(request.env),
            'task_count': project.task_count,
            'open_task_count': project.open_task_count,
            'created_date': project.create_date.isoformat() if project.create_date else None,
        }

    def _update_support_project(self, project, params: dict) -> dict[str, Any]:
        vals: dict[str, Any] = {}
        if 'name' in params:
            name = (params.get('name') or '').strip()
            if not name:
                return {'success': False, 'error': 'Name cannot be empty'}
            vals['name'] = name
        if 'description' in params:
            vals['description'] = params.get('description') or ''

        if 'cloud_service_id' in params:
            target_raw = params.get('cloud_service_id')
            if target_raw:
                CloudService = request.env['woow_paas_platform.cloud_service'].sudo()
                target = CloudService.browse(int(target_raw))
                if not target.exists():
                    return {'success': False, 'error': 'Cloud Service not found'}
                current_ws = project.workspace_id
                if not current_ws or target.workspace_id.id != current_ws.id:
                    return {
                        'success': False,
                        'error': 'Cloud Service must belong to the same workspace.',
                    }
                vals['cloud_service_id'] = target.id
            else:
                vals['cloud_service_id'] = False

        if not vals:
            return {'success': True, 'data': self._format_project_detail(project)}

        try:
            project.sudo().write(vals)
        except ValidationError as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'data': self._format_project_detail(project)}

    @route(
        "/api/support-projects/<int:project_id>/mcp-servers",
        auth="user", methods=["POST"], type="json",
    )
    def api_support_project_mcp_servers(
        self,
        project_id: int,
        action: str = 'list',
        **kw: Any,
    ) -> dict[str, Any]:
        """Project-centric MCP CRUD (Plan D4 / AC #4-7)."""
        project, access = self._resolve_project_access(project_id)
        if not project or not access:
            return {'success': False, 'error': self._PROJECT_NOT_FOUND_ERROR}

        if action == 'list':
            return self._list_project_mcp_servers(project)

        # sidecar-hint is read-only but mirrors create's owner/admin gate
        # (it is the prefill backing for the Add Sidecar MCP shortcut).
        write_actions = (
            'create', 'update', 'delete', 'sync', 'test', 'sidecar-hint',
        )
        if action in write_actions:
            if not _portal_project_settings_editable(request.env):
                return {'success': False, 'error': 'Permission denied'}
            if access.role not in (ROLE_OWNER, ROLE_ADMIN):
                return {'success': False, 'error': 'Permission denied'}

        if action == 'sidecar-hint':
            return self._compute_project_sidecar_hint(project)
        if action == 'create':
            return self._create_project_mcp_server(project, kw)
        if action == 'update':
            return self._update_project_mcp_server(project, kw)
        if action == 'delete':
            return self._delete_project_mcp_server(project, kw)
        if action == 'sync':
            return self._sync_project_mcp_server(project, kw)
        if action == 'test':
            return self._test_project_mcp_server(project, kw)
        return {'success': False, 'error': f'Unknown action: {action}'}

    def _compute_project_sidecar_hint(self, project) -> dict[str, Any]:
        """Return prefill payload for the Add Sidecar MCP UI shortcut, or
        an error when the project has no mcp-enabled backing service."""
        service = project.cloud_service_id
        if not service or not service.template_id.mcp_enabled:
            return {
                'success': False,
                'error': 'No sidecar available for this project',
            }
        template = service.template_id
        return {
            'success': True,
            'data': {
                'url': self._compute_sidecar_url_hint(service),
                'transport': template.mcp_transport or 'streamable_http',
                'api_key': service.mcp_auth_token or '',
                'suggested_name': f'{service.name} MCP',
            },
        }

    def _list_project_mcp_servers(self, project) -> dict[str, Any]:
        servers = project.mcp_server_ids.filtered(
            lambda s: s.scope == 'user' and s.active
        )
        return {
            'success': True,
            'data': [self._format_mcp_server(s) for s in servers],
        }

    def _create_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        name = (params.get('name') or '').strip()
        url = (params.get('url') or '').strip()
        if not name or not url:
            return {'success': False, 'error': 'Name and URL are required'}
        url_error = _validate_mcp_url(
            url, sidecar_backed=bool(params.get('sidecar_backed', False)),
        )
        if url_error:
            return {'success': False, 'error': url_error}

        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        try:
            server = McpServer.create({
                'name': name,
                'url': url,
                'transport': params.get('transport', 'sse'),
                'api_key': params.get('api_key', ''),
                'headers_json': params.get('headers_json', ''),
                'description': params.get('description', ''),
                'scope': 'user',
                'project_id': project.id,
                # #271: URL↔flag consistency IS now enforced — see
                # _validate_mcp_url(sidecar_backed=...) above + the model
                # @api.constrains _check_sidecar_url_in_cluster.
                'sidecar_backed': bool(params.get('sidecar_backed', False)),
            })
        except ValidationError as e:
            return {'success': False, 'error': str(e)}
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _resolve_project_mcp(self, project, params: dict):
        server_id = params.get('server_id')
        if not server_id:
            return None, {'success': False, 'error': 'server_id is required'}
        McpServer = request.env['woow_paas_platform.mcp_server'].sudo()
        server = McpServer.browse(int(server_id))
        if not server.exists() or server.project_id.id != project.id:
            return None, {'success': False, 'error': 'MCP Server not found'}
        return server, None

    def _update_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        server, error = self._resolve_project_mcp(project, params)
        if error:
            return error
        if 'url' in params:
            url_error = _validate_mcp_url(
                (params.get('url') or '').strip(),
                sidecar_backed=server.sidecar_backed,
            )
            if url_error:
                return {'success': False, 'error': url_error}
        vals: dict[str, Any] = {}
        for field_name in ('name', 'url', 'transport', 'api_key', 'headers_json', 'description'):
            if field_name in params:
                vals[field_name] = params[field_name]
        if vals:
            try:
                server.write(vals)
            except ValidationError as e:
                return {'success': False, 'error': str(e)}
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _delete_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        server, error = self._resolve_project_mcp(project, params)
        if error:
            return error
        server.unlink()
        return {'success': True}

    def _sync_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        server, error = self._resolve_project_mcp(project, params)
        if error:
            return error
        try:
            server.action_sync_tools()
        except Exception as e:
            _logger.warning('MCP sync failed for server %s: %s', server.name, e)
            return {'success': False, 'error': f'Sync failed: {e}'}
        return {'success': True, 'data': self._format_mcp_server(server)}

    def _test_project_mcp_server(self, project, params: dict) -> dict[str, Any]:
        server, error = self._resolve_project_mcp(project, params)
        if error:
            return error
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

    @staticmethod
    def _safe_json_load(raw: Any, default: Any, field_label: str, ctx: str = '') -> Any:
        """Parse a JSON-in-Text field, returning ``default`` instead of raising.

        Seed/admin-editable JSON fields (tags, helm_default_values) live in XML and
        are prone to the same line-wrap-inside-a-string defect that broke
        helm_value_specs. These are consumed on hot read paths (marketplace listing,
        template detail) with NO surrounding try/except, so one malformed row would
        500 the whole endpoint for every user. Degrade to ``default`` + a loud log on
        the display paths; deploy paths (which feed Helm) should fail loud instead.
        """
        if not raw:
            return default
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            _logger.error(
                "Invalid JSON in %s%s; falling back to default",
                field_label, f' ({ctx})' if ctx else '',
            )
            return default

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
            # Defense-in-depth: a stray literal newline inside a JSON string value
            # (e.g. a seed XML that wrapped a long label/description across lines)
            # makes strict json.loads raise. Silently returning {} here would nuke the
            # ENTIRE Application Settings section — including required fields the user
            # must fill — so the service can never launch. Degrade to a tolerant parse
            # (strict=False allows control chars) so the section still renders, and log
            # loudly so the malformed seed data gets noticed and cleaned.
            try:
                specs = json.loads(template.helm_value_specs, strict=False)
                _logger.error(
                    "helm_value_specs for template %s contains control characters "
                    "inside string values; parsed with strict=False as a fallback. "
                    "Fix the seed data so JSON strings are not split across lines.",
                    template.name,
                )
                return specs
            except (json.JSONDecodeError, TypeError):
                _logger.error("Invalid helm_value_specs for template %s", template.name)
                return {}

    @staticmethod
    def _sanitize_helm_values(values: dict, template: Any) -> dict:
        """Remove secret values from helm_values before exposing in API responses.

        Replaces values under 'secret' keys with masked versions to prevent
        leaking credentials (e.g., N8N_API_KEY) through the service API.
        """
        if not values:
            return values

        def _mask_secrets(d: dict, depth: int = 0) -> dict:
            result = {}
            for key, val in d.items():
                if key == 'secret' and isinstance(val, dict):
                    # Mask all values under 'secret' keys
                    result[key] = {
                        k: f'{str(v)[:8]}...' if v and len(str(v)) > 8 else '***'
                        for k, v in val.items()
                    }
                elif isinstance(val, dict) and depth < 5:
                    result[key] = _mask_secrets(val, depth + 1)
                else:
                    result[key] = val
            return result

        return _mask_secrets(values)

    # _build_mcp_sidecar_config moved to CloudService model

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

        # Update status for services in transitional states
        transitional_states = ('deploying', 'initializing', 'upgrading', 'deleting')
        for svc in services:
            if svc.state in transitional_states:
                try:
                    self._update_service_status(svc)
                except Exception as e:
                    _logger.warning("Failed to update status for service %s: %s", svc.name, e)

        # Re-read after status updates
        services = CloudService.search([
            ('workspace_id', '=', workspace.id),
        ])

        data = [self._format_service(svc) for svc in services]

        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    def _create_service(self, workspace: Any, template_id: int | None, name: str | None, values: dict[str, Any] | None, odoo_addon_ids: list[int] | None = None, plan_id: int | None = None) -> dict[str, Any]:
        """Create a new cloud service."""
        if not template_id:
            return {'success': False, 'error': 'Template ID is required'}
        if not name:
            return {'success': False, 'error': 'Service name is required'}
        if not plan_id:
            return {'success': False, 'error': 'Subscription plan is required'}

        name = name.strip()
        if not name:
            return {'success': False, 'error': 'Service name is required'}

        CloudAppTemplate = request.env['woow_paas_platform.cloud_app_template']
        CloudService = request.env['woow_paas_platform.cloud_service']

        template = CloudAppTemplate.browse(template_id)
        if not template.exists() or not template.is_active:
            return {'success': False, 'error': 'Template not found'}

        # Validate subscription plan
        Plan = request.env['sh.subscription.plan'].sudo()
        plan = Plan.browse(plan_id)
        if not plan.exists():
            return {'success': False, 'error': 'Subscription plan not found'}
        # Verify plan belongs to this template
        if template.sudo().subscription_plan_ids and plan not in template.sudo().subscription_plan_ids:
            return {'success': False, 'error': 'Plan not available for this template'}

        # Always generate reference_id server-side (never accept from frontend)
        reference_id = str(uuid.uuid4())
        # Generate hash-based names for cross-sandbox uniqueness
        from ..services.naming import make_namespace, make_service_subdomain
        ns_prefix = request.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.namespace_prefix', 'paas-ws-'
        )
        subdomain = make_service_subdomain(workspace.slug, reference_id, name)
        helm_release_name = f"svc-{reference_id[:8]}"
        helm_namespace = make_namespace(workspace.slug, prefix=ns_prefix)

        # Filter user values to only allowed keys, then merge with defaults
        # Note: silently filter on create (frontend may send defaults alongside user values)
        # helm_default_values feeds Helm directly, so fail loud on malformed JSON here
        # (unlike the display paths which degrade) rather than deploy a broken service.
        try:
            default_values = json.loads(template.helm_default_values) if template.helm_default_values else {}
        except (json.JSONDecodeError, TypeError):
            _logger.error("Invalid helm_default_values for template %s; blocking create", template.name)
            return {'success': False, 'error': 'Template misconfigured (invalid default values). Please contact support.'}
        filtered_user_values, _rejected = self._filter_allowed_helm_values(values, template)

        # Extract _init.* keys (not Helm values, used for post-deploy init)
        init_params = {}
        helm_only_values = {}
        for key, val in filtered_user_values.items():
            if key.startswith('_init.'):
                init_params[key[len('_init.'):]] = val
            else:
                helm_only_values[key] = val

        # Validate n8n password if template requires init
        if template.post_deploy_init_type == 'n8n':
            pwd = init_params.get('owner_password', '')
            if len(pwd) < 8:
                return {'success': False, 'error': 'Password must be at least 8 characters'}
            if not any(c.isupper() for c in pwd):
                return {'success': False, 'error': 'Password must contain at least one uppercase letter'}
            if not any(c.islower() for c in pwd):
                return {'success': False, 'error': 'Password must contain at least one lowercase letter'}
            if not any(c.isdigit() for c in pwd):
                return {'success': False, 'error': 'Password must contain at least one number'}

        # Validate Odoo administrator password
        if template.slug == 'odoo':
            pwd = helm_only_values.get('odooPassword', '')
            if not pwd or len(pwd) < 8:
                return {'success': False, 'error': 'Administrator password must be at least 8 characters'}

        nested_user_values = self._unflatten_dotpath_keys(helm_only_values)
        merged_values = self._deep_merge(default_values, nested_user_values)
        _logger.info(
            "Merged helm values keys for service '%s' (template: %s): %s",
            name,
            template.name,
            sorted(merged_values.keys()),
        )

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
            # Create service record in pending state (cron picks up 'pending')
            service_vals = {
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
            }
            # Store init params for post-deploy initialization
            if init_params.get('owner_email'):
                service_vals['n8n_owner_email'] = init_params['owner_email']
            if init_params.get('owner_password'):
                service_vals['n8n_owner_password'] = init_params['owner_password']

            # Resolve Odoo addon selection (with dependency expansion)
            resolved_addon_ids = []
            if odoo_addon_ids and template.slug == 'odoo':
                CloudOdooAddon = request.env['woow_paas_platform.cloud_odoo_addon']
                selected = CloudOdooAddon.browse(odoo_addon_ids).filtered('active')
                # Expand dependencies recursively
                all_addons = selected
                queue = list(selected)
                while queue:
                    addon = queue.pop(0)
                    for dep in addon.depends_on:
                        if dep not in all_addons:
                            all_addons |= dep
                            queue.append(dep)
                resolved_addon_ids = all_addons.ids
                service_vals['odoo_addon_ids'] = [(6, 0, resolved_addon_ids)]

            service = CloudService.create(service_vals)

            # Create subscription for this service
            partner = workspace.owner_id.partner_id
            _logger.info(
                "[CLOUD_SVC] deploy_service created service=%s workspace=%s template=%s partner=%s plan=%s",
                service.id, workspace.id, template.id, partner.id, plan.id,
            )
            service._create_and_confirm_subscription(partner, plan)

            # Build expose configuration before spawning background thread
            expose_config = None
            if template.ingress_enabled:
                expose_config = {
                    'enabled': True,
                    'subdomain': subdomain,
                }

            # Store deploy params and trigger cron for background deployment
            deploy_params = {}
            if expose_config:
                deploy_params['expose_config'] = expose_config
            if mcp_api_key:
                deploy_params['mcp_api_key'] = mcp_api_key
            if deploy_params:
                service.write({'deploy_params': json.dumps(deploy_params)})

            # Trigger the deploy cron to pick up this service ASAP
            cron = request.env.ref(
                'woow_paas_platform.ir_cron_deploy_cloud_services',
                raise_if_not_found=False,
            )
            if cron:
                cron._trigger()

            return {
                'success': True,
                'data': self._format_service(service),
            }

        except Exception as e:
            _logger.error("Error creating service: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': 'An error occurred while creating the service.'}

    # _deploy_service_background removed — deployment is now handled by
    # ir.cron (CloudService._cron_process_pending_deploys) to avoid
    # ConnectionPool lock contention in Odoo 18 workers=0 mode.

    def _get_service(self, service: Any) -> dict[str, Any]:
        """Get service details, updating status from operator if needed."""
        # Check if we need to poll operator for status
        if service.state in ['deploying', 'upgrading', 'initializing']:
            self._update_service_status(service)

        return {
            'success': True,
            'data': self._format_service(service, include_details=True),
        }

    def _update_service(self, service: Any, values: dict[str, Any] | None, version: str | None, odoo_addon_ids: list[int] | None = None) -> dict[str, Any]:
        """Update/upgrade a service."""
        if service.state in ['pending', 'deleting', 'error']:
            return {'success': False, 'error': f'Cannot update service in {service.state} state'}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': 'PaaS Operator not configured'}

        try:
            # Update Odoo addon selection if provided
            addon_changed = False
            if odoo_addon_ids is not None and service.template_id.slug == 'odoo':
                old_addon_ids = set(service.odoo_addon_ids.ids)
                CloudOdooAddon = request.env['woow_paas_platform.cloud_odoo_addon']
                selected = CloudOdooAddon.browse(odoo_addon_ids).filtered('active')
                # Expand dependencies
                all_addons = selected
                queue = list(selected)
                while queue:
                    addon = queue.pop(0)
                    for dep in addon.depends_on:
                        if dep not in all_addons:
                            all_addons |= dep
                            queue.append(dep)
                service.write({'odoo_addon_ids': [(6, 0, all_addons.ids)]})
                addon_changed = set(all_addons.ids) != old_addon_ids

                # Server-side validation: block removal of installed modules
                removed_ids = old_addon_ids - set(all_addons.ids)
                if removed_ids and service.state == 'running':
                    removed_addons = CloudOdooAddon.browse(list(removed_ids)).exists()
                    if removed_addons:
                        tech_names = [a.technical_name for a in removed_addons]
                        try:
                            check_result = client.get_odoo_module_states(
                                namespace=service.helm_namespace,
                                release_name=service.helm_release_name,
                                module_names=tech_names,
                            )
                            if check_result.get('success'):
                                safe_states = {'uninstalled', 'uninstallable', 'not_found'}
                                for name, info in check_result.get('modules', {}).items():
                                    if info.get('state') not in safe_states:
                                        # Rollback the addon write
                                        service.write({'odoo_addon_ids': [(6, 0, list(old_addon_ids))]})
                                        return {
                                            'success': False,
                                            'error': f"Cannot remove '{name}': module is {info.get('state')} in Odoo. "
                                                     f"Please deactivate it in Odoo first.",
                                        }
                        except Exception as e:
                            # Fail-closed: rollback and reject
                            service.write({'odoo_addon_ids': [(6, 0, list(old_addon_ids))]})
                            _logger.warning("Failed to verify module states for addon removal: %s", e)
                            return {
                                'success': False,
                                'error': 'Cannot verify module states. Please try again later.',
                            }

            # Filter user values to only allowed keys, reject unauthorized
            existing_values = json.loads(service.helm_values) if service.helm_values else {}
            filtered_user_values, rejected_keys = self._filter_allowed_helm_values(values, service.template_id)
            if rejected_keys:
                return {'success': False, 'error': f'Unauthorized configuration keys: {", ".join(rejected_keys)}'}
            # _init.* are one-time provisioning params consumed at creation (stored on
            # the service record, e.g. n8n_owner_password). They are NOT Helm values and
            # must never be merged into helm_values on reconfigure — doing so would
            # persist the admin password in plaintext and echo it back unmasked via the
            # service-detail API (_sanitize_helm_values only masks 'secret' dicts).
            filtered_user_values = {k: v for k, v in filtered_user_values.items() if not k.startswith('_init.')}
            nested_user_values = self._unflatten_dotpath_keys(filtered_user_values)
            merged_values = self._deep_merge(existing_values, nested_user_values)

            # Rebuild or remove addon initContainer helm values when addon selection changed
            if addon_changed:
                if service.odoo_addon_ids:
                    image_vals = merged_values.get('image', {})
                    registry = image_vals.get('registry', '')
                    repo = image_vals.get('repository', '')
                    tag = image_vals.get('tag', 'latest')
                    image_ref = f"{registry}/{repo}:{tag}" if registry else f"{repo}:{tag}"
                    addon_values = service._build_odoo_addon_helm_values(image_ref)
                    merged_values = self._deep_merge(merged_values, addon_values)
                else:
                    # All addons deselected — remove addon-specific helm values
                    service._remove_addon_helm_values(merged_values)

            # Transition to upgrading BEFORE calling operator so the
            # frontend gets an immediate response with state='upgrading'.
            service.write({
                'state': 'upgrading',
                'helm_values': json.dumps(merged_values),
                'helm_chart_version': version or service.helm_chart_version,
                'last_upgraded_at': datetime.now(),
                'error_message': False,
                'addon_update_pending': addon_changed,
            })
            response_data = self._format_service(service)

            # Capture values for the background thread (no ORM access).
            db_name = request.env.cr.dbname
            service_id = service.id
            upgrade_kwargs = dict(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
                chart=service.template_id.helm_chart_name,
                values=merged_values,
                version=version,
            )
            operator_url = client.base_url
            operator_api_key = client._session.headers.get('X-API-Key', '')

            def _bg_upgrade():
                """Run helm upgrade in a background thread with its own cursor."""
                import odoo
                from ..services.paas_operator import PaaSOperatorClient
                bg_client = PaaSOperatorClient(operator_url, operator_api_key)
                try:
                    release_info = bg_client.upgrade_release(**upgrade_kwargs)
                    with odoo.registry(db_name).cursor() as cr:
                        env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                        svc = env['woow_paas_platform.cloud_service'].browse(service_id)
                        if svc.exists() and svc.state == 'upgrading':
                            svc.write({
                                'helm_revision': release_info.get('revision', svc.helm_revision + 1),
                            })
                except Exception as exc:
                    _logger.error("Background upgrade failed for service %s: %s", service_id, exc)
                    try:
                        with odoo.registry(db_name).cursor() as cr:
                            env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                            svc = env['woow_paas_platform.cloud_service'].browse(service_id)
                            if svc.exists() and svc.state == 'upgrading':
                                detail = getattr(exc, 'detail', None) or str(exc)
                                svc.write({
                                    'state': 'error',
                                    'error_message': f'Upgrade failed: {detail}',
                                })
                    except Exception:
                        _logger.exception("Failed to write error state for service %s", service_id)

            thread = threading.Thread(target=_bg_upgrade, daemon=True)
            thread.start()

            return {
                'success': True,
                'data': response_data,
            }

        except PaaSOperatorConnectionError:
            return {'success': False, 'error': 'Unable to connect to deployment service'}

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
                # Filter to Running-phase pods only — after helm upgrade,
                # old pods may be Terminating (phase=Succeeded/Failed) and
                # would cause the readiness check to fail permanently.
                active_pods = [
                    p for p in pods if p.get('phase') == 'Running'
                ]
                all_ready = all(
                    '/' in pod.get('ready', '0/0')
                    and pod.get('ready', '0/0').split('/')[0] == pod.get('ready', '0/0').split('/')[1]
                    for pod in active_pods
                ) if active_pods else (not pods)

                if all_ready:
                    template = service.template_id
                    needs_init = (
                        template.post_deploy_init_type
                        and template.post_deploy_init_type != 'none'
                    )

                    if needs_init and original_state == 'deploying':
                        # Pods ready but need post-deploy init → transition to initializing
                        service.write({
                            'state': 'initializing',
                            'helm_revision': helm_revision,
                            'error_message': False,
                        })
                        self._run_post_deploy_init(service)

                    elif needs_init and original_state == 'initializing':
                        # Already initializing, retry if needed
                        self._run_post_deploy_init(service)

                    elif needs_init and original_state == 'upgrading' and service.addon_update_pending:
                        # Addon selection changed during upgrade → need module list refresh
                        service.write({
                            'state': 'initializing',
                            'helm_revision': helm_revision,
                            'error_message': False,
                            'init_retries': 0,
                            'init_error': False,
                        })
                        self._run_post_deploy_init(service)

                    else:
                        # No init needed or already done → running.
                        service.write({
                            'state': 'running',
                            'helm_revision': helm_revision,
                            'error_message': False,
                        })
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

    def _run_post_deploy_init(self, service: Any) -> None:
        """Execute post-deploy initialization for a service.

        Called when pods are ready but the application needs initialization
        (e.g., n8n owner setup + API key generation).

        On success: transitions to 'running' and auto-creates MCP server.
        On failure: increments retry counter; after 5 retries → 'error' state.
        """
        template = service.template_id

        if template.post_deploy_init_type == 'n8n':
            client = get_paas_operator_client(request.env)
            if not client:
                _logger.warning("PaaS Operator not configured, cannot initialize n8n")
                return

            owner_email = service.n8n_owner_email or template.post_deploy_init_email or 'admin@woowtech.io'
            owner_password = service.n8n_owner_password
            if not owner_password:
                # Fallback for services created before this feature
                owner_password = 'W' + str(uuid.uuid4()).upper()

            try:
                result = client.init_n8n(
                    namespace=service.helm_namespace,
                    release_name=service.helm_release_name,
                    owner_email=owner_email,
                    owner_password=owner_password,
                )

                if result.get('success'):
                    real_api_key = result['api_key']

                    # Check if sidecar was properly restarted
                    if not result.get('pod_restarted'):
                        _logger.warning(
                            "n8n init for service %s: API key updated in Secret but pod was NOT restarted. "
                            "MCP sidecar may still use the old placeholder key until next pod restart.",
                            service.name,
                        )

                    # Update helm_values to replace the UUID placeholder with the real API key
                    update_vals = {
                        'n8n_api_key': real_api_key,
                        'state': 'running',
                        'init_retries': 0,
                        'init_error': False,
                    }
                    if template.mcp_api_key_helm_path and service.helm_values:
                        try:
                            current_values = json.loads(service.helm_values)
                            api_key_nested = self._unflatten_dotpath_keys(
                                {template.mcp_api_key_helm_path: real_api_key}
                            )
                            updated_values = self._deep_merge(current_values, api_key_nested)
                            update_vals['helm_values'] = json.dumps(updated_values)
                        except (json.JSONDecodeError, TypeError):
                            _logger.warning("Failed to update helm_values with real API key for service %s", service.name)
                    service.write(update_vals)
                    _logger.info(
                        "n8n init succeeded for service %s, API key and helm_values updated",
                        service.name,
                    )
                else:
                    retries = (service.init_retries or 0) + 1
                    error_msg = result.get('error', 'Unknown error')
                    vals = {
                        'init_retries': retries,
                        'init_error': error_msg,
                    }
                    if retries >= 5:
                        vals.update({
                            'state': 'error',
                            'error_message': f'n8n initialization failed after {retries} retries: {error_msg}',
                        })
                    service.write(vals)
                    _logger.warning(
                        "n8n init attempt %d failed for service %s: %s",
                        retries, service.name, error_msg,
                    )

            except Exception as e:
                retries = (service.init_retries or 0) + 1
                vals = {
                    'init_retries': retries,
                    'init_error': str(e),
                }
                if retries >= 5:
                    vals.update({
                        'state': 'error',
                        'error_message': f'n8n initialization failed: {str(e)}',
                    })
                service.write(vals)
                _logger.warning(
                    "n8n init exception (attempt %d) for service %s: %s",
                    retries, service.name, e,
                )

        elif template.post_deploy_init_type == 'odoo':
            client = get_paas_operator_client(request.env)
            if not client:
                _logger.warning("PaaS Operator not configured, cannot initialize Odoo module list")
                return

            try:
                result = client.init_odoo(
                    namespace=service.helm_namespace,
                    release_name=service.helm_release_name,
                )

                if result.get('success'):
                    service.write({
                        'state': 'running',
                        'init_retries': 0,
                        'init_error': False,
                        'addon_update_pending': False,
                    })
                    _logger.info(
                        "Odoo init succeeded for service %s: added=%d, updated=%d",
                        service.name,
                        result.get('modules_added', 0),
                        result.get('modules_updated', 0),
                    )
                else:
                    retries = (service.init_retries or 0) + 1
                    error_msg = result.get('error', 'Unknown error')
                    vals = {
                        'init_retries': retries,
                        'init_error': error_msg,
                    }
                    if retries >= 5:
                        vals.update({
                            'state': 'error',
                            'error_message': f'Odoo initialization failed after {retries} retries: {error_msg}',
                        })
                    service.write(vals)
                    _logger.warning(
                        "Odoo init attempt %d failed for service %s: %s",
                        retries, service.name, error_msg,
                    )

            except Exception as e:
                retries = (service.init_retries or 0) + 1
                vals = {
                    'init_retries': retries,
                    'init_error': str(e),
                }
                if retries >= 5:
                    vals.update({
                        'state': 'error',
                        'error_message': f'Odoo initialization failed: {str(e)}',
                    })
                service.write(vals)
                _logger.warning(
                    "Odoo init exception (attempt %d) for service %s: %s",
                    retries, service.name, e,
                )

    def _compute_sidecar_url_hint(self, service: Any) -> str:
        """Compute the MCP endpoint URL for a service's sidecar.

        Prefers the cluster-internal K8s service address: it stays in-cluster,
        never traverses the CF edge, and so cannot hit CF Access (#271). Falls
        back to subdomain, then to a localhost placeholder the user must edit.

        NOTE (W0 #283, 2026-07-05): the operator DOES publish a public
        ``{subdomain}-mcp`` CF route for the sidecar (see #301) — the earlier
        "CF only routes the main app port, not the sidecar" rationale was wrong.
        That public route is dead weight (the agent data-plane uses this
        in-cluster hint); prefer in-cluster so the agent path never regresses.
        """
        template = service.template_id
        endpoint_path = template.mcp_endpoint_path or '/mcp'
        sidecar_port = template.mcp_sidecar_port or 3001

        # Prefer the Kubernetes internal service URL: in-cluster, off the CF
        # edge, so it never hits CF Access (#271) and carries no public attack
        # surface. (W0 #283, 2026-07-05: the operator DOES build a public
        # {subdomain}-mcp CF route — see #301 — so this is NOT "the sidecar
        # port isn't routed"; it's "in-cluster is the correct path".)
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
        # Map 'pending' to 'deploying' for UI — pending is an internal
        # state used by the cron as a pick-up lock; users should see 'deploying'.
        ui_state = 'deploying' if service.state == 'pending' else service.state

        data = {
            'id': service.id,
            'name': service.name,
            'state': ui_state,
            'subdomain': service.subdomain or '',
            'custom_domain': service.custom_domain or '',
            'error_message': service.error_message or '',
            'has_project': bool(service.project_ids),
            'template': {
                'id': service.template_id.id,
                'name': service.template_id.name,
                'slug': service.template_id.slug or '',
                'category': service.template_id.category,
                'helm_value_specs': self._parse_helm_value_specs(service.template_id),
            },
            'helm_revision': service.helm_revision,
            'created_date': service.create_date.isoformat() if service.create_date else None,
            'deployed_at': service.deployed_at.isoformat() if service.deployed_at else None,
            'odoo_addons': [
                {'id': a.id, 'name': a.name, 'technical_name': a.technical_name}
                for a in service.odoo_addon_ids
            ] if service.odoo_addon_ids else [],
            'subscription': service._get_subscription_info(),
        }

        if include_details:
            data.update({
                'helm_namespace': service.helm_namespace,
                'helm_release_name': service.helm_release_name,
                'helm_chart_version': service.helm_chart_version,
                'helm_values': self._sanitize_helm_values(
                    json.loads(service.helm_values) if service.helm_values else {},
                    service.template_id,
                ),
                'internal_port': service.internal_port,
                'allocated_vcpu': service.allocated_vcpu,
                'allocated_ram_gb': service.allocated_ram_gb,
                'allocated_storage_gb': service.allocated_storage_gb,
                'last_upgraded_at': service.last_upgraded_at.isoformat() if service.last_upgraded_at else None,
            })

            # Expose n8n login credentials if applicable
            template = service.template_id
            if template.post_deploy_init_type == 'n8n' and template.post_deploy_init_email:
                data['n8n_credentials'] = {
                    'email': template.post_deploy_init_email,
                    'info': 'Password was auto-generated during deployment. Check service logs or redeploy to reset.',
                }

        return data
