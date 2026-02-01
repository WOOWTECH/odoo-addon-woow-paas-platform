import logging
import traceback
from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)


class PaasController(Controller):
    @route("/woow", auth="user", website=False)
    def paas_app(self):
        session_info = request.env['ir.http'].session_info()
        return request.render(
            'woow_paas_platform.paas_app',
            {'session_info': session_info}
        )

    # ==================== Workspace API ====================

    @route("/api/workspaces", auth="user", methods=["POST"], type="json", csrf=False)
    def workspace_api(self, method='list', workspace_id=None, name=None, description=None, **kwargs):
        """
        Handle workspace operations via JSON-RPC.
        'method' field: 'list', 'create', 'get', 'update', 'delete'
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
        """Get all workspaces accessible by current user"""
        user = request.env.user
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        # Find all workspaces where user has access
        access_records = WorkspaceAccess.search([
            ('user_id', '=', user.id),
            ('workspace_id.state', '=', 'active'),
        ])

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
        """Create a new workspace"""
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
                    'role': 'owner',
                    'member_count': 1,
                    'is_owner': True,
                    'created_date': workspace.create_date.isoformat() if workspace.create_date else None,
                }
            }
        except Exception as e:
            _logger.error("Error creating workspace: %s\n%s", str(e), traceback.format_exc())
            return {'success': False, 'error': str(e)}

    def _get_workspace(self, workspace_id):
        """Get a specific workspace by ID"""
        if not workspace_id:
            return {'success': False, 'error': 'Workspace ID is required'}

        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access
        access = workspace.check_user_access(user)
        if not access:
            return {'success': False, 'error': 'Access denied'}

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
        """Update a workspace"""
        if not workspace_id:
            return {'success': False, 'error': 'Workspace ID is required'}

        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access (need admin or owner)
        access = workspace.check_user_access(user, required_role='admin')
        if not access:
            return {'success': False, 'error': 'Access denied'}

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
        """Delete (archive) a workspace"""
        if not workspace_id:
            return {'success': False, 'error': 'Workspace ID is required'}

        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Only owner can delete
        if workspace.owner_id.id != user.id:
            return {'success': False, 'error': 'Only the owner can delete a workspace'}

        workspace.action_archive()
        return {'success': True, 'message': 'Workspace archived'}

    # ==================== Workspace Members API ====================

    @route("/api/workspaces/members", auth="user", methods=["POST"], type="json", csrf=False)
    def workspace_members_api(self, method='list', workspace_id=None, access_id=None, email=None, role=None, **kwargs):
        """
        Handle workspace member operations via JSON-RPC.
        'method' field: 'list', 'invite', 'update_role', 'remove'
        """
        if not workspace_id:
            return {'success': False, 'error': 'Workspace ID is required'}

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
        """Get all members of a workspace"""
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access
        access = workspace.check_user_access(user)
        if not access:
            return {'success': False, 'error': 'Access denied'}

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
        """Invite a member to workspace"""
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access (need admin or owner to invite)
        access = workspace.check_user_access(user, required_role='admin')
        if not access:
            return {'success': False, 'error': 'Access denied. Only admins can invite members.'}

        email = (email or '').strip().lower()
        role = role or 'user'

        if not email:
            return {'success': False, 'error': 'Email is required'}

        if role not in ['admin', 'user', 'guest']:
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
            return {'success': False, 'error': str(e)}

    def _update_member_role(self, workspace_id, access_id, role):
        """Update a member's role"""
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access (need admin or owner)
        access = workspace.check_user_access(user, required_role='admin')
        if not access:
            return {'success': False, 'error': 'Access denied'}

        if not access_id:
            return {'success': False, 'error': 'Access ID is required'}

        target_access = WorkspaceAccess.browse(access_id)
        if not target_access.exists() or target_access.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Member not found'}

        if role not in ['admin', 'user', 'guest']:
            return {'success': False, 'error': 'Invalid role'}

        # Cannot change owner's role directly
        if target_access.role == 'owner':
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
        """Remove a member from workspace"""
        if not access_id:
            return {'success': False, 'error': 'Access ID is required'}

        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access (need admin or owner)
        access = workspace.check_user_access(user, required_role='admin')
        if not access:
            return {'success': False, 'error': 'Access denied'}

        target_access = WorkspaceAccess.browse(access_id)
        if not target_access.exists() or target_access.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Member not found'}

        # Cannot remove owner
        if target_access.role == 'owner':
            return {'success': False, 'error': 'Cannot remove the workspace owner'}

        target_access.unlink()
        return {'success': True, 'message': 'Member removed'}
