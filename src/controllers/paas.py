import logging
import traceback
from odoo.http import request, route, Controller

from ..models.workspace_access import (
    ROLE_OWNER, ROLE_ADMIN, ROLE_USER,
    ASSIGNABLE_ROLES,
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
