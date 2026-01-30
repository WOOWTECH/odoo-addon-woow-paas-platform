import json
from odoo.http import request, route, Controller, Response


class PaasController(Controller):
    @route("/woow", auth="user", website=False)
    def paas_app(self):
        session_info = request.env['ir.http'].session_info()
        return request.render(
            'woow_paas_platform.paas_app',
            {'session_info': session_info}
        )

    def _json_response(self, data, status=200):
        """Helper to create JSON responses"""
        return Response(
            json.dumps(data),
            status=status,
            content_type='application/json'
        )

    def _error_response(self, message, status=400):
        """Helper to create error responses"""
        return self._json_response({'error': message}, status=status)

    # ==================== Workspace API ====================

    @route("/api/workspaces", auth="user", methods=["GET"], type="http", csrf=False)
    def get_workspaces(self):
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

        return self._json_response({
            'success': True,
            'data': workspaces,
            'count': len(workspaces),
        })

    @route("/api/workspaces", auth="user", methods=["POST"], type="json", csrf=False)
    def create_workspace(self):
        """Create a new workspace"""
        params = request.jsonrequest
        name = params.get('name', '').strip()
        description = params.get('description', '').strip()

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
            return {'success': False, 'error': str(e)}

    @route("/api/workspaces/<int:workspace_id>", auth="user", methods=["GET"], type="http", csrf=False)
    def get_workspace(self, workspace_id):
        """Get a specific workspace by ID"""
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return self._error_response('Workspace not found', 404)

        # Check access
        access = workspace.check_access(user)
        if not access:
            return self._error_response('Access denied', 403)

        return self._json_response({
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
        })

    @route("/api/workspaces/<int:workspace_id>", auth="user", methods=["PUT"], type="json", csrf=False)
    def update_workspace(self, workspace_id):
        """Update a workspace"""
        user = request.env.user
        params = request.jsonrequest
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access (need admin or owner)
        access = workspace.check_access(user, required_role='admin')
        if not access:
            return {'success': False, 'error': 'Access denied'}

        update_vals = {}
        if 'name' in params:
            name = params['name'].strip()
            if not name:
                return {'success': False, 'error': 'Workspace name cannot be empty'}
            update_vals['name'] = name
        if 'description' in params:
            update_vals['description'] = params['description'].strip()

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

    @route("/api/workspaces/<int:workspace_id>", auth="user", methods=["DELETE"], type="http", csrf=False)
    def delete_workspace(self, workspace_id):
        """Delete (archive) a workspace"""
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return self._error_response('Workspace not found', 404)

        # Only owner can delete
        if workspace.owner_id.id != user.id:
            return self._error_response('Only the owner can delete a workspace', 403)

        workspace.action_archive()
        return self._json_response({'success': True, 'message': 'Workspace archived'})

    # ==================== Workspace Members API ====================

    @route("/api/workspaces/<int:workspace_id>/members", auth="user", methods=["GET"], type="http", csrf=False)
    def get_workspace_members(self, workspace_id):
        """Get all members of a workspace"""
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return self._error_response('Workspace not found', 404)

        # Check access
        access = workspace.check_access(user)
        if not access:
            return self._error_response('Access denied', 403)

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

        return self._json_response({
            'success': True,
            'data': members,
            'count': len(members),
        })

    @route("/api/workspaces/<int:workspace_id>/members", auth="user", methods=["POST"], type="json", csrf=False)
    def invite_member(self, workspace_id):
        """Invite a member to workspace"""
        user = request.env.user
        params = request.jsonrequest
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access (need admin or owner to invite)
        access = workspace.check_access(user, required_role='admin')
        if not access:
            return {'success': False, 'error': 'Access denied. Only admins can invite members.'}

        email = params.get('email', '').strip().lower()
        role = params.get('role', 'user')

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

    @route("/api/workspaces/<int:workspace_id>/members/<int:access_id>", auth="user", methods=["PUT"], type="json", csrf=False)
    def update_member_role(self, workspace_id, access_id):
        """Update a member's role"""
        user = request.env.user
        params = request.jsonrequest
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return {'success': False, 'error': 'Workspace not found'}

        # Check access (need admin or owner)
        access = workspace.check_access(user, required_role='admin')
        if not access:
            return {'success': False, 'error': 'Access denied'}

        target_access = WorkspaceAccess.browse(access_id)
        if not target_access.exists() or target_access.workspace_id.id != workspace_id:
            return {'success': False, 'error': 'Member not found'}

        role = params.get('role')
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

    @route("/api/workspaces/<int:workspace_id>/members/<int:access_id>", auth="user", methods=["DELETE"], type="http", csrf=False)
    def remove_member(self, workspace_id, access_id):
        """Remove a member from workspace"""
        user = request.env.user
        Workspace = request.env['woow_paas_platform.workspace']
        WorkspaceAccess = request.env['woow_paas_platform.workspace_access']

        workspace = Workspace.browse(workspace_id)
        if not workspace.exists():
            return self._error_response('Workspace not found', 404)

        # Check access (need admin or owner)
        access = workspace.check_access(user, required_role='admin')
        if not access:
            return self._error_response('Access denied', 403)

        target_access = WorkspaceAccess.browse(access_id)
        if not target_access.exists() or target_access.workspace_id.id != workspace_id:
            return self._error_response('Member not found', 404)

        # Cannot remove owner
        if target_access.role == 'owner':
            return self._error_response('Cannot remove the workspace owner', 400)

        target_access.unlink()
        return self._json_response({'success': True, 'message': 'Member removed'})
