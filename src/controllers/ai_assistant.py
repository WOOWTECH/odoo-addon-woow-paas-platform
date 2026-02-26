from __future__ import annotations

import base64
import html
import json
import logging
from typing import Any

from markupsafe import escape
from werkzeug.wrappers import Response

from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)


class AiAssistantController(Controller):
    """Controller for AI assistant and support API endpoints."""

    # ==================== Helpers ====================

    MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB

    def _check_channel_access(self, channel):
        """Verify the current user has access to the channel.

        Returns True if the user's partner is a member of the channel.
        """
        user_partner = request.env.user.partner_id
        return user_partner in channel.channel_partner_ids

    def _check_workspace_access(self, workspace):
        """Verify the current user is a member of the workspace.

        Returns True if an access record exists for the current user.
        """
        return request.env['woow_paas_platform.workspace_access'].sudo().search_count([
            ('workspace_id', '=', workspace.id),
            ('user_id', '=', request.env.user.id),
        ]) > 0

    def _check_project_access(self, project):
        """Verify the current user has access to the project via cloud service.

        Returns True if the project has no cloud service (global) or the user
        is a member of the workspace that owns the cloud service.
        """
        if not project.cloud_service_id or not project.cloud_service_id.workspace_id:
            return True
        return self._check_workspace_access(project.cloud_service_id.workspace_id)

    def _check_task_access(self, task):
        """Verify the current user has access to the task's project.

        Returns True if the task has no project/cloud_service or the user
        is a member of the associated workspace.
        """
        if not task.project_id:
            return True
        return self._check_project_access(task.project_id)

    def _sse_error_response(self, error: str, error_code: str) -> Response:
        """Build a structured SSE error response (HTTP 200)."""
        _logger.warning('SSE error [%s]: %s (user=%s)', error_code, error, request.env.user.login)
        return Response(
            'data: ' + json.dumps({
                'error': error,
                'error_code': error_code,
                'done': True,
            }) + '\n\n',
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            },
            status=200,
        )

    # ==================== AI Provider / Agent API ====================

    @route('/api/ai/providers', auth='user', methods=['POST'], type='json')
    def api_ai_providers(self, **kwargs: Any) -> dict[str, Any]:
        """List all active AI configurations.

        Returns config information with api_key explicitly excluded.
        """
        configs = request.env['ai.config'].sudo().search([
            ('active', '=', True),
        ])
        data = [{
            'id': c.id,
            'name': c.name,
            'model_name': c.model or '',
            'is_active': c.active,
        } for c in configs]
        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    @route('/api/ai/agents', auth='user', methods=['POST'], type='json')
    def api_ai_agents(self, **kwargs: Any) -> dict[str, Any]:
        """List all AI assistants.

        Returns:
            dict: Response with assistant list.
        """
        assistants = request.env['ai.assistant'].sudo().search([])
        data = [{
            'id': a.id,
            'name': a.name,
            'agent_display_name': a.name,
            'description': a.description or '',
            'config_id': a.config_id.id if a.config_id else None,
            'config_name': a.config_id.name if a.config_id else None,
            'avatar_color': '#875A7B',
            'is_default': False,
        } for a in assistants]
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
        if not self._check_channel_access(channel):
            return {'success': False, 'error': 'Access denied'}

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

        # Collect all AI assistant partner IDs to identify AI messages
        ai_partner_ids = set(
            request.env['ai.assistant'].sudo().search([]).mapped('partner_id.id')
        )
        # Also include partner_root for backward compatibility with old messages
        ai_partner_ids.add(request.env.ref('base.partner_root').id)

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

            is_ai = msg.author_id.id in ai_partner_ids
            data.append({
                'id': msg.id,
                'body': html.unescape(msg.body) if is_ai and msg.body else (msg.body or ''),
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
        if not self._check_channel_access(channel):
            return {'success': False, 'error': 'Access denied'}

        message = channel.with_context(from_paas_chat=True).message_post(
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
        if not self._check_channel_access(channel):
            return Response(
                json.dumps({'success': False, 'error': 'Access denied'}),
                content_type='application/json',
                status=403,
            )

        uploaded_file = kwargs.get('file')
        if not uploaded_file:
            return Response(
                json.dumps({'success': False, 'error': 'No file uploaded'}),
                content_type='application/json',
                status=400,
            )

        file_data = uploaded_file.read(self.MAX_UPLOAD_SIZE + 1)
        if len(file_data) > self.MAX_UPLOAD_SIZE:
            return Response(
                json.dumps({'success': False, 'error': 'File too large (max 10 MB)'}),
                content_type='application/json',
                status=413,
            )
        file_name = uploaded_file.filename

        attachment = request.env['ir.attachment'].sudo().create({
            'name': file_name,
            'datas': base64.b64encode(file_data),
            'res_model': 'discuss.channel',
            'res_id': channel_id,
        })

        # Post a message with the attachment (escape filename to prevent XSS)
        message = channel.message_post(
            body=f'Uploaded: {escape(file_name)}',
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
        # Validate CSRF token (GET endpoints bypass Odoo's automatic check)
        csrf_token = kwargs.get('csrf_token', '')
        if not csrf_token or not request.validate_csrf(csrf_token):
            return self._sse_error_response('Invalid request', 'csrf_error')

        channel = request.env['discuss.channel'].sudo().browse(channel_id)
        if not channel.exists():
            return self._sse_error_response('Channel not found', 'channel_not_found')
        if not self._check_channel_access(channel):
            return self._sse_error_response('Access denied', 'access_denied')

        # Find the assistant for this channel
        assistant = self._get_channel_assistant(channel)
        if not assistant:
            return self._sse_error_response('No AI assistant available', 'no_agent')

        from ..models.ai_client import AIClient, AIClientError

        try:
            client = AIClient.from_assistant(assistant)
        except AIClientError as exc:
            _logger.error('AI client error for assistant %s in channel %s: %s', assistant.name, channel_id, exc.message)
            return self._sse_error_response('AI provider not configured', 'provider_not_configured')

        # Get the latest user message (exclude messages from the assistant's partner)
        assistant_partner_id = assistant.partner_id.id
        last_message = request.env['mail.message'].sudo().search([
            ('res_id', '=', channel_id),
            ('model', '=', 'discuss.channel'),
            ('message_type', '=', 'comment'),
            ('author_id', '!=', assistant_partner_id),
        ], order='id desc', limit=1)

        if not last_message:
            return self._sse_error_response('No user message found', 'no_message')

        user_message = last_message.body or ''

        # Pre-fetch ORM data before entering the generator
        system_prompt = ''
        if assistant.context_id:
            system_prompt = assistant.context_id.context or ''

        # Append cloud service context if the channel is linked to a task
        # whose project is bound to a cloud service
        cloud_context = self._get_cloud_service_context_for_channel(channel)
        if cloud_context:
            system_prompt = (system_prompt + '\n\n' + cloud_context).strip()

        history = channel._get_chat_history(limit=20)
        messages = client.build_messages(
            system_prompt=system_prompt,
            history=history,
            user_message=user_message,
        )
        root_partner_id = assistant_partner_id

        # Pre-capture DB info for use inside the generator where the
        # original request cursor may no longer be valid.
        db_name = request.env.cr.dbname
        uid = request.env.uid
        context = dict(request.env.context)

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

            # Persist the AI response BEFORE sending the done signal so
            # that the message is guaranteed to be in the database when
            # the frontend receives the done event.
            saved_message_id = None
            warning = None
            if full_response:
                try:
                    import odoo
                    from odoo import api as odoo_api
                    registry = odoo.registry(db_name)
                    with registry.cursor() as new_cr:
                        new_env = odoo_api.Environment(new_cr, uid, context)
                        new_channel = new_env['discuss.channel'].browse(channel_id)
                        msg = new_channel.with_context(
                            mail_create_nosubscribe=True,
                            skip_ai_reply=True,
                        ).message_post(
                            body=full_response,  # Store raw Markdown from AI
                            message_type='comment',
                            subtype_xmlid='mail.mt_comment',
                            author_id=root_partner_id,
                        )
                        saved_message_id = msg.id
                except Exception as exc:
                    _logger.exception(
                        'Failed to post AI response to channel %s: %s',
                        channel_id, exc,
                    )
                    warning = 'AI 回覆已生成但無法儲存，請重新整理頁面確認。'

            # Send the done signal (with message_id when available)
            done_payload = {
                'chunk': '',
                'done': True,
                'full_response': full_response,
            }
            if saved_message_id:
                done_payload['message_id'] = saved_message_id
            if warning:
                done_payload['warning'] = warning
            yield f'data: {json.dumps(done_payload)}\n\n'

        return Response(
            generate(),
            content_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
                'X-Accel-Buffering': 'no',
            },
        )

    def _get_channel_assistant(self, channel):
        """Find the appropriate AI assistant for a channel.

        Only returns an assistant if the channel is linked to a task with
        ``ai_auto_reply`` enabled, ensuring we don't reply on channels
        that haven't opted in.

        Args:
            channel: A ``discuss.channel`` recordset.

        Returns:
            An ``ai.assistant`` record or None.
        """
        task = request.env['project.task'].sudo().search([
            ('channel_id', '=', channel.id),
            ('ai_auto_reply', '=', True),
        ], limit=1)
        if not task:
            return None

        return self._get_default_assistant()

    def _get_default_assistant(self):
        """Get the system default AI assistant from Settings."""
        assistant_id = request.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_ai_assistant_id',
        )
        if assistant_id:
            try:
                assistant = request.env['ai.assistant'].sudo().browse(int(assistant_id))
                if assistant.exists():
                    return assistant
            except (ValueError, TypeError):
                _logger.warning('Invalid default_ai_assistant_id value: %r', assistant_id)
        return None

    def _get_cloud_service_context_for_channel(self, channel) -> str:
        """Build cloud service context for AI system prompt.

        Looks up the task linked to this channel, then the project's
        cloud service, and assembles relevant context.

        Returns:
            A formatted context string, or empty string if not applicable.
        """
        task = request.env['project.task'].sudo().search([
            ('channel_id', '=', channel.id),
        ], limit=1)
        if not task or not task.project_id or not task.project_id.cloud_service_id:
            return ''

        service = task.project_id.cloud_service_id
        template = service.template_id

        parts = ['## Cloud Service Information']
        if template:
            parts.append(f'- Application: {template.name}')
            if template.category:
                parts.append(f'- Category: {template.category}')
            if template.description:
                parts.append(f'- Description: {template.description}')

        parts.append(f'- Service Name: {service.name}')
        parts.append(f'- Status: {service.state or "unknown"}')

        if service.subdomain:
            parts.append(f'- URL: https://{service.subdomain}')
        if service.error_message:
            parts.append(f'- Error: {service.error_message}')

        if service.helm_values:
            parts.append(f'- Configuration (Helm Values):\n```json\n{service.helm_values}\n```')

        return '\n'.join(parts)

    # ==================== AI Connection Status ====================

    @route('/api/ai/connection-status', auth='user', methods=['POST'], type='json')
    def api_ai_connection_status(self, **kwargs: Any) -> dict[str, Any]:
        """Check the AI configuration connection status.

        Returns:
            dict: Connection status with config and model info.
        """
        assistant = self._get_default_assistant()
        if not assistant or not assistant.config_id:
            return {
                'success': True,
                'data': {
                    'connected': False,
                    'provider_name': '',
                    'model_name': '',
                },
            }
        config = assistant.config_id
        return {
            'success': True,
            'data': {
                'connected': bool(config.api_key),
                'provider_name': config.name,
                'model_name': config.model or '',
            },
        }

    # ==================== Support Stats API ====================

    @route('/api/support/stats', auth='user', methods=['POST'], type='json')
    def api_support_stats(self, **kwargs: Any) -> dict[str, Any]:
        """Get task statistics for the current user.

        Returns:
            dict: Task stats (total, active, completion percentage).
        """
        # Scope stats to projects the current user has access to
        user = request.env.user
        accessible_projects = request.env['project.project'].sudo().search([
            ('cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
        ])
        task_domain = [('project_id', 'in', accessible_projects.ids)]
        Task = request.env['project.task'].sudo()
        total = Task.search_count(task_domain)
        done = Task.search_count(task_domain + [('stage_id.name', 'in', ('Done', 'Cancelled'))])
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
        [
            '/api/support/projects',
            '/api/support/projects/<int:workspace_id>',
            '/api/support/cloud-services/<int:cloud_service_id>/project',
        ],
        auth='user', methods=['POST'], type='json',
    )
    def api_support_projects(
        self,
        workspace_id: int = 0,
        cloud_service_id: int = 0,
        action: str = 'list',
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Project CRUD operations, optionally scoped to a cloud service or workspace.

        Args:
            workspace_id: Legacy workspace ID (0 = all projects).
            cloud_service_id: The cloud service ID (0 = all projects).
            action: Operation type ('list', 'create', 'update', 'delete').

        Returns:
            dict: Response with project data.
        """
        cloud_service = None
        workspace = None

        if cloud_service_id:
            cloud_service = request.env['woow_paas_platform.cloud_service'].sudo().browse(cloud_service_id)
            if not cloud_service.exists():
                return {'success': False, 'error': 'Cloud Service not found'}
            if not self._check_workspace_access(cloud_service.workspace_id):
                return {'success': False, 'error': 'Access denied'}
        elif workspace_id:
            workspace = request.env['woow_paas_platform.workspace'].sudo().browse(workspace_id)
            if not workspace.exists():
                return {'success': False, 'error': 'Workspace not found'}
            if not self._check_workspace_access(workspace):
                return {'success': False, 'error': 'Access denied'}

        if action == 'list':
            return self._list_projects(workspace, cloud_service)
        elif action == 'create':
            if not cloud_service and not workspace:
                return {'success': False, 'error': 'cloud_service_id is required for create'}
            return self._create_project(kwargs, cloud_service=cloud_service, workspace=workspace)
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
            if not self._check_workspace_access(workspace):
                return {'success': False, 'error': 'Access denied'}

        if action == 'list':
            return self._list_tasks(workspace, kwargs)
        elif action == 'create':
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
        if not self._check_task_access(task):
            return {'success': False, 'error': 'Access denied'}

        if action == 'get':
            return self._get_task(task)
        elif action == 'update':
            return self._update_task(task, kwargs)
        elif action == 'delete':
            return self._delete_task(task)
        else:
            return {'success': False, 'error': f'Unknown action: {action}'}

    # ==================== Private: Project Helpers ====================

    _DEFAULT_STAGES = [
        {'name': 'New', 'sequence': 1},
        {'name': 'In Progress', 'sequence': 5},
        {'name': 'Done', 'sequence': 10},
    ]

    def _get_project_stages(self, project_id: int) -> list[dict]:
        """Get stages for a project, sorted by sequence.

        Auto-creates default stages (New, In Progress, Done) when none exist.

        Args:
            project_id: The project.project ID.

        Returns:
            list: Stage dicts with id, name, and sequence.
        """
        stages = request.env['project.task.type'].sudo().search(
            [('project_ids', 'in', [project_id])],
            order='sequence asc',
        )
        if not stages:
            stages = self._ensure_default_stages(project_id)
        return [{'id': s.id, 'name': s.name, 'sequence': s.sequence} for s in stages]

    def _ensure_default_stages(self, project_id: int):
        """Create default stages for a project that has none.

        Reuses existing stages by name when possible to avoid duplicates.
        """
        TaskType = request.env['project.task.type'].sudo()
        result = TaskType
        for stage_def in self._DEFAULT_STAGES:
            existing = TaskType.search([('name', '=', stage_def['name'])], limit=1)
            if existing:
                if project_id not in existing.project_ids.ids:
                    existing.write({'project_ids': [(4, project_id)]})
                result |= existing
            else:
                result |= TaskType.create({
                    'name': stage_def['name'],
                    'sequence': stage_def['sequence'],
                    'project_ids': [(4, project_id)],
                })
        return result.sorted('sequence')

    def _list_projects(self, workspace=None, cloud_service=None) -> dict[str, Any]:
        """List projects, optionally filtered by cloud service or workspace."""
        domain = []
        if cloud_service:
            domain.append(('cloud_service_id', '=', cloud_service.id))
        elif workspace:
            domain.append(('cloud_service_id.workspace_id', '=', workspace.id))
        projects = request.env['project.project'].sudo().search(domain)
        data = [{
            'id': proj.id,
            'name': proj.name,
            'description': proj.description or '',
            'cloud_service_id': proj.cloud_service_id.id if proj.cloud_service_id else None,
            'cloud_service_name': proj.cloud_service_id.name if proj.cloud_service_id else '',
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

    def _create_project(self, params: dict, cloud_service=None, workspace=None) -> dict[str, Any]:
        """Create a new project bound to a cloud service."""
        name = (params.get('name') or '').strip()
        if not name:
            return {'success': False, 'error': 'Project name is required'}

        vals = {
            'name': name,
            'description': (params.get('description') or '').strip(),
        }

        if cloud_service:
            # Check 1:1 constraint before attempting create
            if cloud_service.project_ids:
                return {'success': False, 'error': 'This Cloud Service already has a Support Project'}
            vals['cloud_service_id'] = cloud_service.id
        elif workspace:
            # Legacy: find cloud_service_id from params if provided
            cs_id = params.get('cloud_service_id')
            if cs_id:
                vals['cloud_service_id'] = int(cs_id)

        project = request.env['project.project'].sudo().create(vals)
        return {
            'success': True,
            'data': {
                'id': project.id,
                'name': project.name,
                'description': project.description or '',
                'cloud_service_id': project.cloud_service_id.id if project.cloud_service_id else None,
                'cloud_service_name': project.cloud_service_id.name if project.cloud_service_id else '',
                'workspace_id': project.workspace_id.id if project.workspace_id else None,
                'workspace_name': project.workspace_id.name if project.workspace_id else '',
                'task_count': 0,
                'created_date': project.create_date.isoformat() if project.create_date else None,
            },
        }

    def _update_project(self, params: dict) -> dict[str, Any]:
        """Update an existing project."""
        project_id = params.get('project_id')
        if not project_id:
            return {'success': False, 'error': 'project_id is required'}

        try:
            pid = int(project_id)
        except (ValueError, TypeError):
            return {'success': False, 'error': 'Invalid project_id'}

        project = request.env['project.project'].sudo().browse(pid)
        if not project.exists():
            return {'success': False, 'error': 'Project not found'}
        if not self._check_project_access(project):
            return {'success': False, 'error': 'Access denied'}

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

        try:
            pid = int(project_id)
        except (ValueError, TypeError):
            return {'success': False, 'error': 'Invalid project_id'}

        project = request.env['project.project'].sudo().browse(pid)
        if not project.exists():
            return {'success': False, 'error': 'Project not found'}
        if not self._check_project_access(project):
            return {'success': False, 'error': 'Access denied'}

        project.write({'active': False})
        return {'success': True, 'data': {'id': project.id}}

    # ==================== Private: Task Helpers ====================

    def _list_tasks(self, workspace, params: dict) -> dict[str, Any]:
        """List tasks, optionally filtered by workspace."""
        project_id = params.get('project_id')
        domain = []
        if workspace:
            domain.append(('project_id.cloud_service_id.workspace_id', '=', workspace.id))
        if project_id:
            try:
                domain.append(('project_id', '=', int(project_id)))
            except (ValueError, TypeError):
                return {'success': False, 'error': 'Invalid project_id'}

        tasks = request.env['project.task'].sudo().search(domain)
        data = [self._serialize_task(task) for task in tasks]
        return {
            'success': True,
            'data': data,
            'count': len(data),
        }

    def _create_task(self, workspace, params: dict) -> dict[str, Any]:
        """Create a new task in a project."""
        name = (params.get('name') or '').strip()
        project_id = params.get('project_id')

        if not name:
            return {'success': False, 'error': 'Task name is required'}
        if not project_id:
            return {'success': False, 'error': 'project_id is required'}

        try:
            pid = int(project_id)
        except (ValueError, TypeError):
            return {'success': False, 'error': 'Invalid project_id'}

        project = request.env['project.project'].sudo().browse(pid)
        if not project.exists():
            return {'success': False, 'error': 'Project not found'}
        if not self._check_project_access(project):
            return {'success': False, 'error': 'Access denied'}

        vals = {
            'name': name,
            'project_id': project.id,
            'description': (params.get('description') or '').strip(),
        }

        if params.get('priority'):
            vals['priority'] = str(params['priority'])
        if params.get('date_deadline'):
            vals['date_deadline'] = params['date_deadline']

        if params.get('stage_id'):
            try:
                stage_id_int = int(params['stage_id'])
            except (ValueError, TypeError):
                return {'success': False, 'error': 'Invalid stage_id'}
            stage = request.env['project.task.type'].sudo().browse(stage_id_int)
            if stage.exists():
                vals['stage_id'] = stage.id

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
            try:
                vals['stage_id'] = int(params['stage_id'])
            except (ValueError, TypeError):
                return {'success': False, 'error': 'Invalid stage_id'}
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

    def _serialize_task(self, task, include_children=True) -> dict:
        """Serialize a project.task record to a dict."""
        data = {
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
            'parent_id': task.parent_id.id if task.parent_id else None,
        }
        if include_children:
            data['child_ids'] = [
                self._serialize_task(child, include_children=False)
                for child in task.child_ids
            ]
        return data
