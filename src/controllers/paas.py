import hashlib
import json
import logging
import traceback
import uuid
from datetime import datetime

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
    def paas_app(self):
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

    @route("/woow/api/config", auth="user", methods=["POST"], type="json")
    def api_config(self):
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
    def workspace_api(self, method='list', workspace_id=None, name=None, description=None, **kwargs):
        """
        Handle workspace CRUD operations via JSON-RPC.

        This is the main entry point for all workspace-related API calls.
        The 'method' parameter determines which operation to perform.

        Args:
            method (str): Operation type. One of:
                - 'list': Get all accessible workspaces
                - 'create': Create a new workspace
                - 'get': Get a specific workspace by ID
                - 'update': Update workspace name/description
                - 'delete': Archive a workspace (soft delete)
            workspace_id (int, optional): Target workspace ID (required for get/update/delete)
            name (str, optional): Workspace name (required for create, optional for update)
            description (str, optional): Workspace description
            **kwargs: Additional parameters (ignored)

        Returns:
            dict: JSON response with structure:
                - success (bool): Whether the operation succeeded
                - data (dict|list): Result data (on success)
                - error (str): Error message (on failure)
                - count (int): Item count (for list operations)
        """
        if method == 'list':
            return self._list_workspaces()
        elif method == 'create':
            return self._create_workspace(name, description)
        elif method == 'get':
            return self._get_workspace(workspace_id)
        elif method == 'update':
            return self._update_workspace(workspace_id, name, description)
        elif method == 'delete':
            return self._delete_workspace(workspace_id)
        else:
            return {'success': False, 'error': f'Unknown method: {method}'}

    def _list_workspaces(self):
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

    def _create_workspace(self, name, description):
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

    def _get_workspace(self, workspace_id):
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

    def _update_workspace(self, workspace_id, name, description):
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

    def _delete_workspace(self, workspace_id):
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

    @route("/api/workspaces/members", auth="user", methods=["POST"], type="json")
    def workspace_members_api(self, method='list', workspace_id=None, access_id=None, email=None, role=None, **kwargs):
        """
        Handle workspace member management operations via JSON-RPC.

        This endpoint manages the access control for workspaces,
        allowing admins/owners to invite, update, and remove members.

        Args:
            method (str): Operation type. One of:
                - 'list': Get all members of a workspace
                - 'invite': Invite a new member by email
                - 'update_role': Change a member's role
                - 'remove': Remove a member from workspace
            workspace_id (int): Target workspace ID (required for all operations)
            access_id (int, optional): Target access record ID (for update_role/remove)
            email (str, optional): User email to invite (for invite)
            role (str, optional): Role to assign ('admin', 'user', 'guest')
            **kwargs: Additional parameters (ignored)

        Returns:
            dict: JSON response with structure:
                - success (bool): Whether the operation succeeded
                - data (dict|list): Result data (on success)
                - error (str): Error message (on failure)
                - count (int): Item count (for list operations)
        """
        if not workspace_id:
            return {'success': False, 'error': 'Workspace ID is required'}

        # Validate workspace_id is an integer
        try:
            workspace_id = int(workspace_id)
        except (TypeError, ValueError):
            return {'success': False, 'error': 'Workspace not found or access denied'}

        if method == 'list':
            return self._list_members(workspace_id)
        elif method == 'invite':
            return self._invite_member(workspace_id, email, role)
        elif method == 'update_role':
            return self._update_member_role(workspace_id, access_id, role)
        elif method == 'remove':
            return self._remove_member(workspace_id, access_id)
        else:
            return {'success': False, 'error': f'Unknown method: {method}'}

    def _list_members(self, workspace_id):
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

    def _invite_member(self, workspace_id, email, role):
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

    def _update_member_role(self, workspace_id, access_id, role):
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

    def _remove_member(self, workspace_id, access_id):
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

    @route("/woow/api/cloud/templates", auth="user", methods=["POST"], type="json")
    def api_cloud_templates(self, category=None, search=None, **kw):
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

    @route("/woow/api/cloud/templates/<int:template_id>", auth="user", methods=["POST"], type="json")
    def api_cloud_template(self, template_id, **kw):
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

    def _format_template(self, template, include_values=False):
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
            data['helm_value_specs'] = json.loads(template.helm_value_specs) if template.helm_value_specs else {}
            data['full_description'] = template.full_description or ''
        return data

    # ==================== Cloud Services API ====================

    @route("/woow/api/workspaces/<int:workspace_id>/services", auth="user", methods=["POST"], type="json")
    def api_workspace_services(self, workspace_id, action='list', template_id=None, name=None, reference_id=None, values=None, **kw):
        """
        Handle cloud service operations for a workspace.

        Args:
            workspace_id (int): Workspace ID
            action (str): 'list' or 'create'
            template_id (int, optional): Template ID (for create)
            name (str, optional): Service name (for create)
            reference_id (str, optional): Unique reference ID for subdomain generation
                (for create). If not provided, a UUID will be auto-generated.
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
            return self._create_service(workspace, template_id, name, reference_id, values)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    @route("/woow/api/workspaces/<int:workspace_id>/services/<int:service_id>", auth="user", methods=["POST"], type="json")
    def api_workspace_service(self, workspace_id, service_id, action='get', values=None, version=None, **kw):
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

    @route("/woow/api/workspaces/<int:workspace_id>/services/<int:service_id>/rollback", auth="user", methods=["POST"], type="json")
    def api_service_rollback(self, workspace_id, service_id, revision=None, **kw):
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

    @route("/woow/api/workspaces/<int:workspace_id>/services/<int:service_id>/revisions", auth="user", methods=["POST"], type="json")
    def api_service_revisions(self, workspace_id, service_id, **kw):
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

    # ==================== Cloud Service Helpers ====================

    def _filter_allowed_helm_values(self, values, template):
        """
        Filter Helm values to only include keys allowed by template's value_specs.

        This prevents users from overriding critical system values like namespace,
        resource limits, or security settings that should be controlled by the template.

        Args:
            values (dict): User-provided Helm values
            template: CloudAppTemplate record with helm_value_specs

        Returns:
            dict: Filtered values containing only allowed keys
        """
        if not values:
            return {}

        if not template.helm_value_specs:
            # No specs defined - allow all values (backward compatibility)
            return values

        try:
            specs = json.loads(template.helm_value_specs)
        except (json.JSONDecodeError, TypeError):
            _logger.warning("Invalid helm_value_specs for template %s", template.name)
            return values

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
            return values

        # Filter values to only allowed keys
        filtered = {}
        for key, value in values.items():
            if key in allowed_keys:
                filtered[key] = value
            else:
                _logger.debug(
                    "Filtered out non-allowed Helm value key '%s' for template %s",
                    key, template.name
                )

        return filtered

    def _list_services(self, workspace):
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

    def _create_service(self, workspace, template_id, name, reference_id, values):
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

        # Use provided reference_id or generate a new one
        if not reference_id:
            reference_id = str(uuid.uuid4())
        # Generate subdomain with salted hash: paas-{ws_id}-{hash(reference_id + name)[:8]}
        # Using reference_id as salt prevents subdomain guessing from service name alone
        salted_input = reference_id + name
        name_hash = hashlib.md5(salted_input.encode()).hexdigest()[:8]
        subdomain = f"paas-{workspace.id}-{name_hash}"
        helm_release_name = f"svc-{reference_id[:8]}"
        helm_namespace = f"paas-ws-{workspace.id}"

        # Filter user values to only allowed keys, then merge with defaults
        default_values = json.loads(template.helm_default_values) if template.helm_default_values else {}
        filtered_user_values = self._filter_allowed_helm_values(values, template)
        merged_values = {**default_values, **filtered_user_values}

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
                try:
                    client.create_namespace(
                        namespace=helm_namespace,
                        cpu_limit=str(template.min_vcpu * 2),  # Allow some headroom
                        memory_limit=f"{int(template.min_ram_gb * 2)}Gi",
                        storage_limit=f"{template.min_storage_gb * 2}Gi",
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

    def _get_service(self, service):
        """Get service details, updating status from operator if needed."""
        # Check if we need to poll operator for status
        if service.state in ['deploying', 'upgrading']:
            self._update_service_status(service)

        return {
            'success': True,
            'data': self._format_service(service, include_details=True),
        }

    def _update_service(self, service, values, version):
        """Update/upgrade a service."""
        if service.state in ['pending', 'deleting', 'error']:
            return {'success': False, 'error': f'Cannot update service in {service.state} state'}

        client = get_paas_operator_client(request.env)
        if not client:
            return {'success': False, 'error': 'PaaS Operator not configured'}

        try:
            # Filter user values to only allowed keys, then merge with existing
            existing_values = json.loads(service.helm_values) if service.helm_values else {}
            filtered_user_values = self._filter_allowed_helm_values(values, service.template_id)
            merged_values = {**existing_values, **filtered_user_values}

            # Upgrade release
            release_info = client.upgrade_release(
                namespace=service.helm_namespace,
                release_name=service.helm_release_name,
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

    def _delete_service(self, service):
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

    def _rollback_service(self, service, revision):
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

    def _get_service_revisions(self, service):
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

    def _update_service_status(self, service):
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

    def _format_service(self, service, include_details=False):
        """Format a service record for API response."""
        data = {
            'id': service.id,
            'name': service.name,
            'reference_id': service.reference_id,
            'state': service.state,
            'subdomain': service.subdomain or '',
            'custom_domain': service.custom_domain or '',
            'error_message': service.error_message or '',
            'template': {
                'id': service.template_id.id,
                'name': service.template_id.name,
                'category': service.template_id.category,
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
