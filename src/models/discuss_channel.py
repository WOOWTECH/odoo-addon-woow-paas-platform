import logging

from odoo import models
from odoo.tools import html2plaintext

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    def message_post(self, **kwargs):
        """Override message_post to detect AI @mentions and trigger AI replies.

        After the message is posted via super(), this method checks whether
        the message body contains an @assistant_name mention.  If the channel
        is linked to a task with ``ai_auto_reply`` enabled or if an explicit
        @mention is found, it triggers an AI response and posts it back to
        the channel.

        The AI response is generated using ``AIClient.from_assistant()``
        backed by LangChain.
        """
        message = super().message_post(**kwargs)

        # Only process regular user messages (not system / notification)
        if kwargs.get('message_type', 'comment') != 'comment':
            return message

        # Skip when called from SSE handler to prevent duplicate AI replies
        if self.env.context.get('skip_ai_reply'):
            return message

        # Skip when called from PaaS chat API; the frontend handles AI
        # replies via SSE streaming so we must not block the HTTP response.
        if self.env.context.get('from_paas_chat'):
            return message

        # Skip AI-authored messages to prevent infinite recursion
        author_id = kwargs.get('author_id')
        ai_partner_ids = set(
            self.env['ai.assistant'].sudo().search([]).mapped('partner_id.id')
        )
        if author_id and author_id in ai_partner_ids:
            return message

        body = kwargs.get('body', '') or ''
        if not body:
            return message

        # Determine whether we should trigger AI
        assistant = self._detect_ai_assistant(body)
        if not assistant:
            assistant = self._get_auto_reply_assistant()
        if not assistant:
            return message

        # Generate and post the AI reply
        try:
            self._post_ai_reply(assistant, body, message)
        except Exception:
            _logger.exception(
                'Failed to generate AI reply in channel %s (id=%s)',
                self.name, self.id,
            )
            # Notify the user in channel that AI reply failed
            error_author_id = assistant.partner_id.id if assistant else self.env.ref('base.partner_root').id
            self.with_context(skip_ai_reply=True, mail_create_nosubscribe=True).message_post(
                body='⚠️ AI 助理回覆失敗，請稍後再試或聯繫管理員。',
                message_type='notification',
                subtype_xmlid='mail.mt_note',
                author_id=error_author_id,
            )

        return message

    def _detect_ai_assistant(self, body: str):
        """Detect an @assistant_name mention in the message body.

        Searches for any configured AI assistant whose partner name
        appears in the message body as an @mention (e.g., ``@WoowBot``).

        Args:
            body: The raw message body text / HTML.

        Returns:
            An ``ai.assistant`` recordset (single record) if a mention
            is detected, or an empty recordset otherwise.
        """
        Assistant = self.env['ai.assistant'].sudo()
        assistants = Assistant.search([])
        for assistant in assistants:
            if f'@{assistant.name}' in body:
                return assistant
        return Assistant.browse()

    def _get_auto_reply_assistant(self):
        """Return the default AI assistant if auto-reply is enabled for this channel.

        Checks whether this channel is linked to a project task that has
        ``ai_auto_reply`` enabled.  If so, returns the default AI assistant.

        Returns:
            An ``ai.assistant`` record or empty recordset.
        """
        Assistant = self.env['ai.assistant']

        task = self.env['project.task'].sudo().search([
            ('channel_id', '=', self.id),
            ('ai_auto_reply', '=', True),
        ], limit=1)
        if not task:
            return Assistant.browse()

        assistant_id = int(self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_ai_assistant_id', '0'))
        return Assistant.sudo().browse(assistant_id).exists() or Assistant.browse()

    def _post_ai_reply(self, assistant, user_message: str, original_message):
        """Generate an AI response and post it back to the channel.

        Uses ``AIClient.from_assistant()`` to call the AI API via LangChain,
        then posts the response as a new message authored by the assistant's
        partner.

        Args:
            assistant: The ``ai.assistant`` record to use.
            user_message: The user's message text.
            original_message: The original ``mail.message`` record.
        """
        from .ai_client import AIClient, AIClientError

        try:
            client = AIClient.from_assistant(assistant)
        except AIClientError as exc:
            raise ValueError(
                f'Assistant {assistant.name} configuration error: {exc.message}'
            ) from exc

        # Build conversation history from recent channel messages
        history = self._get_chat_history(limit=20)

        system_prompt = ''
        if assistant.context_id:
            system_prompt = assistant.context_id.context or ''

        # Append cloud service context if available
        cloud_context = self._get_cloud_service_context()
        if cloud_context:
            system_prompt = (system_prompt + '\n\n' + cloud_context).strip()

        messages = client.build_messages(
            system_prompt=system_prompt,
            history=history,
            user_message=user_message,
        )

        assistant_partner_id = assistant.partner_id.id

        mcp_tools = assistant.get_enabled_mcp_tools()

        try:
            ai_response = client.chat_completion_with_tools(messages, mcp_tools)
        except AIClientError as exc:
            _logger.error(
                'AI client error for assistant %s: %s (detail=%s)',
                assistant.name, exc.message, getattr(exc, 'detail', ''),
            )
            error_msg = f'⚠️ AI 助理回覆失敗：{exc.message}。請稍後再試。'
            self.with_context(skip_ai_reply=True, mail_create_nosubscribe=True).message_post(
                body=error_msg,
                message_type='notification',
                subtype_xmlid='mail.mt_note',
                author_id=assistant_partner_id,
            )
            return

        if not ai_response:
            return

        # Post the AI reply as a message in the channel
        ai_message = self.with_context(mail_create_nosubscribe=True).message_post(
            body=ai_response,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=assistant_partner_id,
        )

        # Send bus notification for real-time UI update
        channel_info = {
            'id': ai_message.id,
            'body': ai_response,
            'author': {
                'id': assistant_partner_id,
                'name': assistant.name,
            },
            'assistant_id': assistant.id,
            'assistant_name': assistant.name,
        }
        self.env['bus.bus']._sendone(
            self,
            'mail.message/insert',
            channel_info,
        )

        _logger.info(
            'AI assistant %s replied in channel %s (id=%s)',
            assistant.name, self.name, self.id,
        )

    def _get_cloud_service_context(self) -> str:
        """Build cloud service context for AI system prompt.

        Looks up the task linked to this channel, then the project's
        cloud service, and assembles relevant context.
        """
        task = self.env['project.task'].sudo().search([
            ('channel_id', '=', self.id),
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

    def _get_chat_history(self, limit: int = 20) -> list:
        """Retrieve recent chat messages from this channel.

        Args:
            limit: Maximum number of messages to retrieve.

        Returns:
            A list of dicts with 'role' and 'content' keys, ordered
            from oldest to newest.
        """
        messages = self.env['mail.message'].sudo().search([
            ('res_id', '=', self.id),
            ('model', '=', 'discuss.channel'),
            ('message_type', '=', 'comment'),
        ], order='id desc', limit=limit)

        # Identify AI partner IDs for role detection
        ai_partner_ids = set(
            self.env['ai.assistant'].sudo().search([]).mapped('partner_id.id')
        )
        ai_partner_ids.add(self.env.ref('base.partner_root').id)

        history = []
        for msg in reversed(messages):
            role = 'assistant' if msg.author_id.id in ai_partner_ids else 'user'
            body_text = html2plaintext(msg.body or '').strip()
            if body_text:
                history.append({'role': role, 'content': body_text})
        return history
