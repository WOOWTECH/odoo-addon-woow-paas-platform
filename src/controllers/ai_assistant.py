from __future__ import annotations

import base64
import json
import logging
from typing import Any

from werkzeug.wrappers import Response

from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)


class AiAssistantController(Controller):
    """Controller for AI assistant and support API endpoints."""

    # ==================== AI Provider / Agent API ====================

    @route('/api/ai/providers', auth='user', methods=['POST'], type='json')
    def api_ai_providers(self, **kwargs: Any) -> dict[str, Any]:
        """List all active AI providers.

        Returns provider information with api_key explicitly excluded
        for security.

        Returns:
            dict: Response with provider list (api_key hidden).
        """
        providers = request.env['woow_paas_platform.ai_provider'].sudo().search([
            ('is_active', '=', True),
        ])
        data = [{
            'id': p.id,
            'name': p.name,
            'api_base_url': p.api_base_url,
            'model_name': p.model_name,
            'max_tokens': p.max_tokens,
            'temperature': p.temperature,
            'is_active': p.is_active,
        } for p in providers]
        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    @route('/api/ai/agents', auth='user', methods=['POST'], type='json')
    def api_ai_agents(self, **kwargs: Any) -> dict[str, Any]:
        """List all AI agents.

        Returns:
            dict: Response with agent list.
        """
        agents = request.env['woow_paas_platform.ai_agent'].sudo().search([])
        data = [{
            'id': a.id,
            'name': a.name,
            'agent_display_name': a.agent_display_name or a.name,
            'system_prompt': a.system_prompt or '',
            'provider_id': a.provider_id.id if a.provider_id else None,
            'provider_name': a.provider_id.name if a.provider_id else None,
            'avatar_color': a.avatar_color or '#875A7B',
            'is_default': a.is_default,
        } for a in agents]
        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    # ==================== AI Chat API ====================

    @route('/api/ai/chat/history', auth='user', methods=['POST'], type='json')
    def api_ai_chat_history(
        self,
        channel_id: int = 0,
        limit: int = 50,
        before_id: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Get chat history for a channel.

        Args:
            channel_id: The discuss.channel ID.
            limit: Maximum number of messages to return.
            before_id: Only return messages with ID less than this (pagination).

        Returns:
            dict: Response with message list and attachments.
        """
        if not channel_id:
            return {'success': False, 'error': 'channel_id is required'}

        channel = request.env['discuss.channel'].sudo().browse(channel_id)
        if not channel.exists():
            return {'success': False, 'error': 'Channel not found'}

        domain = [
            ('res_id', '=', channel_id),
            ('model', '=', 'discuss.channel'),
            ('message_type', 'in', ['comment', 'notification']),
        ]
        if before_id:
            domain.append(('id', '<', before_id))

        messages = request.env['mail.message'].sudo().search(
            domain,
            order='id desc',
            limit=limit,
        )

        root_partner_id = request.env.ref('base.partner_root').id
        data = []
        for msg in reversed(messages):
            attachments = []
            for att in msg.attachment_ids:
                attachments.append({
                    'id': att.id,
                    'name': att.name,
                    'mimetype': att.mimetype,
                    'file_size': att.file_size,
                    'url': f'/web/content/{att.id}?download=true',
                })

            is_ai = msg.author_id.id == root_partner_id
            data.append({
                'id': msg.id,
                'body': msg.body or '',
                'author_id': msg.author_id.id if msg.author_id else None,
                'author_name': msg.author_id.name if msg.author_id else 'Unknown',
                'is_ai': is_ai,
                'date': msg.date.isoformat() if msg.date else None,
                'message_type': msg.message_type,
                'attachments': attachments,
            })

        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    @route('/api/ai/chat/post', auth='user', methods=['POST'], type='json')
    def api_ai_chat_post(
        self,
        channel_id: int = 0,
        body: str = '',
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Post a message to a channel.

        This triggers the discuss.channel message_post override which
        may generate an AI reply if an @mention is detected or
        auto-reply is enabled.

        Args:
            channel_id: The discuss.channel ID.
            body: The message body text.

        Returns:
            dict: Response with posted message data.
        """
        if not channel_id:
            return {'success': False, 'error': 'channel_id is required'}
        if not body or not body.strip():
            return {'success': False, 'error': 'body is required'}

        channel = request.env['discuss.channel'].sudo().browse(channel_id)
        if not channel.exists():
            return {'success': False, 'error': 'Channel not found'}

        message = channel.message_post(
            body=body.strip(),
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=request.env.user.partner_id.id,
        )

        return {
            'success': True,
            'data': {
                'id': message.id,
                'body': message.body or '',
                'author_id': message.author_id.id if message.author_id else None,
                'author_name': message.author_id.name if message.author_id else 'Unknown',
                'date': message.date.isoformat() if message.date else None,
            },
        }

    @route('/api/ai/chat/upload', auth='user', methods=['POST'], type='http')
    def api_ai_chat_upload(self, **kwargs: Any) -> str:
        """Upload a file attachment to a channel.

        Expects multipart form data with:
        - channel_id: The discuss.channel ID
        - file: The uploaded file
        - csrf_token: CSRF token

        Returns:
            JSON string with upload result.
        """
        try:
            channel_id = int(kwargs.get('channel_id', 0))
        except (TypeError, ValueError):
            return Response(
                json.dumps({'success': False, 'error': 'Invalid channel_id'}),
                content_type='application/json',
                status=400,
            )
        if not channel_id:
            return Response(
                json.dumps({'success': False, 'error': 'channel_id is required'}),
                content_type='application/json',
                status=400,
            )

        channel = request.env['discuss.channel'].sudo().browse(channel_id)
        if not channel.exists():
            return Response(
                json.dumps({'success': False, 'error': 'Channel not found'}),
                content_type='application/json',
                status=404,
            )

        uploaded_file = kwargs.get('file')
        if not uploaded_file:
            return Response(
                json.dumps({'success': False, 'error': 'No file uploaded'}),
                content_type='application/json',
                status=400,
            )

        file_data = uploaded_file.read()
        file_name = uploaded_file.filename

        attachment = request.env['ir.attachment'].sudo().create({
            'name': file_name,
            'datas': base64.b64encode(file_data),
            'res_model': 'discuss.channel',
            'res_id': channel_id,
        })

        # Post a message with the attachment
        message = channel.message_post(
            body=f'Uploaded: {file_name}',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=request.env.user.partner_id.id,
            attachment_ids=[attachment.id],
        )

        result = {
            'success': True,
            'data': {
                'message_id': message.id,
                'attachment': {
                    'id': attachment.id,
                    'name': attachment.name,
                    'mimetype': attachment.mimetype,
                    'file_size': attachment.file_size,
                    'url': f'/web/content/{attachment.id}?download=true',
                },
            },
        }

        return Response(
            json.dumps(result),
            content_type='application/json',
            status=200,
        )

    # ==================== SSE Streaming ====================

    @route('/api/ai/stream/<int:channel_id>', auth='user', methods=['GET'], type='http')
    def api_ai_stream(self, channel_id: int, **kwargs: Any):
        """Server-Sent Events endpoint for streaming AI responses.

        Retrieves the latest user message from the channel, generates
        a streaming AI response, and sends it back as SSE events.

        Args:
            channel_id: The discuss.channel ID.

        Returns:
            werkzeug.Response: SSE stream with text/event-stream content type.
        """
        sse_error_headers = {
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }

        channel = request.env['discuss.channel'].sudo().browse(channel_id)
        if not channel.exists():
            return Response(
                'data: ' + json.dumps({
                    'error': 'Channel not found',
                    'error_code': 'channel_not_found',
                    'done': True,
                }) + '\n\n',
                content_type='text/event-stream',
                headers=sse_error_headers,
                status=200,
            )

        # Find the agent for this channel
        agent = self._get_channel_agent(channel)
        if not agent:
            return Response(
                'data: ' + json.dumps({
                    'error': 'No AI agent available',
                    'error_code': 'no_agent',
                    'done': True,
                }) + '\n\n',
                content_type='text/event-stream',
                headers=sse_error_headers,
                status=200,
            )

        provider = agent.provider_id
        if not provider or not provider.is_active:
            return Response(
                'data: ' + json.dumps({
                    'error': 'AI provider not configured',
                    'error_code': 'provider_not_configured',
                    'done': True,
                }) + '\n\n',
                content_type='text/event-stream',
                headers=sse_error_headers,
                status=200,
            )

        # Get the latest user message
        last_message = request.env['mail.message'].sudo().search([
            ('res_id', '=', channel_id),
            ('model', '=', 'discuss.channel'),
            ('message_type', '=', 'comment'),
            ('author_id', '!=', request.env.ref('base.partner_root').id),
        ], order='id desc', limit=1)

        if not last_message:
            return Response(
                'data: ' + json.dumps({
                    'error': 'No user message found',
                    'error_code': 'no_message',
                    'done': True,
                }) + '\n\n',
                content_type='text/event-stream',
                headers=sse_error_headers,
                status=200,
            )

        user_message = last_message.body or ''

        # Pre-fetch ORM data before entering the generator to avoid
        # accessing the ORM after the request lifecycle has ended.
        from ..models.ai_client import AIClient, AIClientError

        history = channel._get_chat_history(limit=20)
        client = AIClient(
            api_base_url=provider.api_base_url,
            api_key=provider.api_key,
            model_name=provider.model_name,
            max_tokens=provider.max_tokens,
            temperature=provider.temperature,
        )
        messages = client.build_messages(
            system_prompt=agent.system_prompt or '',
            history=history,
            user_message=user_message,
        )
        root_partner_id = request.env.ref('base.partner_root').id

        def generate():
            full_response = ''
            try:
                for chunk in client.chat_completion_stream(messages):
                    full_response += chunk
                    event_data = json.dumps({'chunk': chunk, 'done': False})
                    yield f'data: {event_data}\n\n'
            except AIClientError as exc:
                error_data = json.dumps({'error': exc.message, 'done': True})
                yield f'data: {error_data}\n\n'
                return

            # Send the done signal
            done_data = json.dumps({'chunk': '', 'done': True, 'full_response': full_response})
            yield f'data: {done_data}\n\n'

            # Post the full AI response to the channel
            if full_response:
                try:
                    channel.with_context(mail_create_nosubscribe=True).message_post(
                        body=full_response,
                        message_type='comment',
                        subtype_xmlid='mail.mt_comment',
                        author_id=root_partner_id,
                    )
                except Exception:
                    _logger.exception('Failed to post AI response to channel %s', channel_id)
                    warn_data = json.dumps({
                        'warning': 'AI response was generated but could not be saved. Please refresh to check.',
                    })
                    yield f'data: {warn_data}\n\n'

        return Response(
            generate(),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            },
        )

    def _get_channel_agent(self, channel):
        """Find the appropriate AI agent for a channel.

        Only returns an agent if the channel is linked to a task with
        ``ai_auto_reply`` enabled, ensuring we don't reply on channels
        that haven't opted in.

        Args:
            channel: A ``discuss.channel`` recordset.

        Returns:
            A ``woow_paas_platform.ai_agent`` record or None.
        """
        task = request.env['project.task'].sudo().search([
            ('channel_id', '=', channel.id),
            ('ai_auto_reply', '=', True),
        ], limit=1)
        if not task:
            return None

        return request.env['woow_paas_platform.ai_agent'].get_default() or None

    # ==================== AI Connection Status ====================

    @route('/api/ai/connection-status', auth='user', methods=['POST'], type='json')
    def api_ai_connection_status(self, **kwargs: Any) -> dict[str, Any]:
        """Check the AI provider connection status.

        Returns:
            dict: Connection status with provider and model info.
        """
        provider = request.env['woow_paas_platform.ai_provider'].sudo().search([
            ('is_active', '=', True),
        ], limit=1)
        if not provider:
            return {
                'success': True,
                'data': {
                    'connected': False,
                    'provider_name': '',
                    'model_name': '',
                },
            }
        return {
            'success': True,
            'data': {
                'connected': True,
                'provider_name': provider.name,
                'model_name': provider.model_name,
            },
        }

    # ==================== Support Stats API ====================

    @route('/api/support/stats', auth='user', methods=['POST'], type='json')
    def api_support_stats(self, **kwargs: Any) -> dict[str, Any]:
        """Get task statistics for the current user.

        Returns:
            dict: Task stats (total, active, completion percentage).
        """
        tasks = request.env['project.task'].sudo().search([])
        total = len(tasks)
        done = len(tasks.filtered(
            lambda t: t.stage_id.name in ('Done', 'Cancelled')
        ))
        active = total - done
        completion = round((done / total) * 100) if total > 0 else 0
        return {
            'success': True,
            'data': {
                'total': total,
                'active': active,
                'completion': completion,
            },
        }

    # ==================== Support / Project API ====================

    @route(
        ['/api/support/projects', '/api/support/projects/<int:workspace_id>'],
        auth='user', methods=['POST'], type='json',
    )
    def api_support_projects(
        self,
        workspace_id: int = 0,
        action: str = 'list',
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Project CRUD operations, optionally scoped to a workspace.

        Args:
            workspace_id: The workspace ID (0 = all projects).
            action: Operation type ('list', 'create', 'update', 'delete').

        Returns:
            dict: Response with project data.
        """
        workspace = None
        if workspace_id:
            workspace = request.env['woow_paas_platform.workspace'].sudo().browse(workspace_id)
            if not workspace.exists():
                return {'success': False, 'error': 'Workspace not found'}

        if action == 'list':
            return self._list_projects(workspace)
        elif action == 'create':
            if not workspace:
                return {'success': False, 'error': 'workspace_id is required for create'}
            return self._create_project(workspace, kwargs)
        elif action == 'update':
            return self._update_project(kwargs)
        elif action == 'delete':
            return self._delete_project(kwargs)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    @route('/api/support/projects/<int:project_id>/stages', auth='user', methods=['POST'], type='json')
    def api_support_project_stages(
        self,
        project_id: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """List stages for a project.

        Args:
            project_id: The project.project ID.

        Returns:
            dict: Response with stage list sorted by sequence.
        """
        stages = self._get_project_stages(project_id)
        return {
            'success': True,
            'data': stages,
        }

    @route(
        ['/api/support/tasks', '/api/support/tasks/<int:workspace_id>'],
        auth='user', methods=['POST'], type='json',
    )
    def api_support_tasks(
        self,
        workspace_id: int = 0,
        action: str = 'list',
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Task list/create operations, optionally scoped to a workspace.

        Args:
            workspace_id: The workspace ID (0 = all tasks).
            action: Operation type ('list', 'create').

        Returns:
            dict: Response with task data.
        """
        workspace = None
        if workspace_id:
            workspace = request.env['woow_paas_platform.workspace'].sudo().browse(workspace_id)
            if not workspace.exists():
                return {'success': False, 'error': 'Workspace not found'}

        if action == 'list':
            return self._list_tasks(workspace, kwargs)
        elif action == 'create':
            if not workspace:
                return {'success': False, 'error': 'workspace_id is required for create'}
            return self._create_task(workspace, kwargs)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    @route('/api/support/tasks/detail/<int:task_id>', auth='user', methods=['POST'], type='json')
    def api_support_task_detail(
        self,
        task_id: int,
        action: str = 'get',
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Single task operations.

        Args:
            task_id: The project.task ID.
            action: Operation type ('get', 'update', 'delete').

        Returns:
            dict: Response with task data.
        """
        task = request.env['project.task'].sudo().browse(task_id)
        if not task.exists():
            return {'success': False, 'error': 'Task not found'}

        if action == 'get':
            return self._get_task(task)
        elif action == 'update':
            return self._update_task(task, kwargs)
        elif action == 'delete':
            return self._delete_task(task)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    # ==================== Private: Project Helpers ====================

    def _get_project_stages(self, project_id: int) -> list[dict]:
        """Get stages for a project, sorted by sequence.

        Args:
            project_id: The project.project ID.

        Returns:
            list: Stage dicts with id, name, and sequence.
        """
        stages = request.env['project.task.type'].sudo().search(
            [('project_ids', 'in', [project_id])],
            order='sequence asc',
        )
        return [{'id': s.id, 'name': s.name, 'sequence': s.sequence} for s in stages]

    def _list_projects(self, workspace) -> dict[str, Any]:
        """List projects, optionally filtered by workspace."""
        domain = []
        if workspace:
            domain.append(('workspace_id', '=', workspace.id))
        projects = request.env['project.project'].sudo().search(domain)
        data = [{
            'id': proj.id,
            'name': proj.name,
            'description': proj.description or '',
            'workspace_id': proj.workspace_id.id if proj.workspace_id else None,
            'workspace_name': proj.workspace_id.name if proj.workspace_id else '',
            'task_count': proj.task_count,
            'created_date': proj.create_date.isoformat() if proj.create_date else None,
        } for proj in projects]
        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    def _create_project(self, workspace, params: dict) -> dict[str, Any]:
        """Create a new project in a workspace."""
        name = (params.get('name') or '').strip()
        if not name:
            return {'success': False, 'error': 'Project name is required'}

        project = request.env['project.project'].sudo().create({
            'name': name,
            'description': (params.get('description') or '').strip(),
            'workspace_id': workspace.id,
        })
        return {
            'success': True,
            'data': {
                'id': project.id,
                'name': project.name,
                'description': project.description or '',
                'workspace_id': workspace.id,
                'workspace_name': workspace.name,
                'task_count': 0,
                'created_date': project.create_date.isoformat() if project.create_date else None,
            },
        }

    def _update_project(self, params: dict) -> dict[str, Any]:
        """Update an existing project."""
        project_id = params.get('project_id')
        if not project_id:
            return {'success': False, 'error': 'project_id is required'}

        project = request.env['project.project'].sudo().browse(int(project_id))
        if not project.exists():
            return {'success': False, 'error': 'Project not found'}

        vals = {}
        if 'name' in params:
            name = (params['name'] or '').strip()
            if name:
                vals['name'] = name
        if 'description' in params:
            vals['description'] = (params['description'] or '').strip()

        if vals:
            project.write(vals)

        return {
            'success': True,
            'data': {
                'id': project.id,
                'name': project.name,
                'description': project.description or '',
            },
        }

    def _delete_project(self, params: dict) -> dict[str, Any]:
        """Delete (archive) a project."""
        project_id = params.get('project_id')
        if not project_id:
            return {'success': False, 'error': 'project_id is required'}

        project = request.env['project.project'].sudo().browse(int(project_id))
        if not project.exists():
            return {'success': False, 'error': 'Project not found'}

        project.write({'active': False})
        return {'success': True, 'data': {'id': project.id}}

    # ==================== Private: Task Helpers ====================

    def _list_tasks(self, workspace, params: dict) -> dict[str, Any]:
        """List tasks, optionally filtered by workspace."""
        project_id = params.get('project_id')
        domain = []
        if workspace:
            domain.append(('project_id.workspace_id', '=', workspace.id))
        if project_id:
            domain.append(('project_id', '=', int(project_id)))

        tasks = request.env['project.task'].sudo().search(domain)
        data = [self._serialize_task(task) for task in tasks]
        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    def _create_task(self, workspace, params: dict) -> dict[str, Any]:
        """Create a new task in a workspace project."""
        name = (params.get('name') or '').strip()
        project_id = params.get('project_id')

        if not name:
            return {'success': False, 'error': 'Task name is required'}
        if not project_id:
            return {'success': False, 'error': 'project_id is required'}

        project = request.env['project.project'].sudo().browse(int(project_id))
        if not project.exists() or project.workspace_id.id != workspace.id:
            return {'success': False, 'error': 'Project not found in workspace'}

        vals = {
            'name': name,
            'project_id': project.id,
            'description': (params.get('description') or '').strip(),
        }

        if params.get('priority'):
            vals['priority'] = str(params['priority'])
        if params.get('date_deadline'):
            vals['date_deadline'] = params['date_deadline']

        if params.get('chat_enabled'):
            vals['chat_enabled'] = True
            vals['ai_auto_reply'] = params.get('ai_auto_reply', True)

        task = request.env['project.task'].sudo().create(vals)
        return {
            'success': True,
            'data': self._serialize_task(task),
        }

    def _get_task(self, task) -> dict[str, Any]:
        """Get task details."""
        return {
            'success': True,
            'data': self._serialize_task(task),
        }

    def _update_task(self, task, params: dict) -> dict[str, Any]:
        """Update an existing task."""
        vals = {}
        if 'name' in params:
            name = (params['name'] or '').strip()
            if name:
                vals['name'] = name
        if 'description' in params:
            vals['description'] = (params['description'] or '').strip()
        if 'stage_id' in params:
            vals['stage_id'] = int(params['stage_id'])
        if 'chat_enabled' in params:
            vals['chat_enabled'] = bool(params['chat_enabled'])
        if 'ai_auto_reply' in params:
            vals['ai_auto_reply'] = bool(params['ai_auto_reply'])

        if vals:
            task.write(vals)

        return {
            'success': True,
            'data': self._serialize_task(task),
        }

    def _delete_task(self, task) -> dict[str, Any]:
        """Delete (archive) a task."""
        task.write({'active': False})
        return {'success': True, 'data': {'id': task.id}}

    def _serialize_task(self, task) -> dict:
        """Serialize a project.task record to a dict."""
        return {
            'id': task.id,
            'name': task.name,
            'description': task.description or '',
            'project_id': task.project_id.id if task.project_id else None,
            'project_name': task.project_id.name if task.project_id else None,
            'chat_enabled': task.chat_enabled,
            'channel_id': task.channel_id.id if task.channel_id else None,
            'ai_auto_reply': task.ai_auto_reply,
            'stage_id': task.stage_id.id if task.stage_id else None,
            'stage_name': task.stage_id.name if task.stage_id else None,
            'priority': task.priority or '0',
            'date_deadline': task.date_deadline.isoformat() if task.date_deadline else None,
            'user_name': task.user_ids[0].name if task.user_ids else None,
            'user_ids': [u.id for u in task.user_ids] if task.user_ids else [],
            'created_date': task.create_date.isoformat() if task.create_date else None,
        }
