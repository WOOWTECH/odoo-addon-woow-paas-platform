import logging

from odoo import api, models

_logger = logging.getLogger(__name__)


class DiscussChannel(models.Model):
    _inherit = 'discuss.channel'

    def message_post(self, **kwargs):
        """Override message_post to detect AI @mentions and trigger AI replies.

        After the message is posted via super(), this method checks whether
        the message body contains an @agent_name mention.  If the channel is
        linked to a task with ``ai_auto_reply`` enabled or if an explicit
        @mention is found, it triggers an AI response and posts it back to
        the channel.

        The AI response is generated using the agent's configured provider
        and system prompt via :class:`~.ai_client.AIClient`.
        """
        message = super().message_post(**kwargs)

        # Only process regular user messages (not system / notification)
        if kwargs.get('message_type', 'comment') != 'comment':
            return message

        body = kwargs.get('body', '') or ''
        if not body:
            return message

        # Determine whether we should trigger AI
        agent = self._detect_ai_agent(body)
        if not agent:
            agent = self._get_auto_reply_agent()
        if not agent:
            return message

        # Generate and post the AI reply
        try:
            self._post_ai_reply(agent, body, message)
        except Exception:
            _logger.exception(
                'Failed to generate AI reply in channel %s (id=%s)',
                self.name, self.id,
            )

        return message

    def _detect_ai_agent(self, body: str):
        """Detect an @agent_name mention in the message body.

        Searches for any configured AI agent whose name appears in the
        message body as an @mention (e.g., ``@woowbot``).

        Args:
            body: The raw message body text / HTML.

        Returns:
            A ``woow_paas_platform.ai_agent`` recordset (single record)
            if a mention is detected, or an empty recordset otherwise.
        """
        AiAgent = self.env['woow_paas_platform.ai_agent'].sudo()
        agents = AiAgent.search([])
        for agent in agents:
            # Check both the technical name and display name
            if f'@{agent.name}' in body:
                return agent
            if agent.agent_display_name and f'@{agent.agent_display_name}' in body:
                return agent
        return AiAgent.browse()

    def _get_auto_reply_agent(self):
        """Return the default AI agent if auto-reply is enabled for this channel.

        Checks whether this channel is linked to a project task that has
        ``ai_auto_reply`` enabled.  If so, returns the default AI agent.

        Returns:
            A ``woow_paas_platform.ai_agent`` record or empty recordset.
        """
        AiAgent = self.env['woow_paas_platform.ai_agent'].sudo()

        # Find tasks linked to this channel with auto_reply enabled
        task = self.env['project.task'].sudo().search([
            ('channel_id', '=', self.id),
            ('ai_auto_reply', '=', True),
        ], limit=1)
        if not task:
            return AiAgent.browse()

        # Return the default agent
        default_agent = AiAgent.search([('is_default', '=', True)], limit=1)
        return default_agent

    def _post_ai_reply(self, agent, user_message: str, original_message):
        """Generate an AI response and post it back to the channel.

        Uses the agent's provider configuration to call the AI API, then
        posts the response as a new message in the channel and sends a
        bus notification.

        Args:
            agent: The ``woow_paas_platform.ai_agent`` record to use.
            user_message: The user's message text.
            original_message: The original ``mail.message`` record.
        """
        from .ai_client import AIClient, AIClientError

        provider = agent.provider_id
        if not provider or not provider.is_active:
            _logger.warning(
                'Agent %s has no active provider, skipping AI reply',
                agent.name,
            )
            return

        # Build conversation history from recent channel messages
        history = self._get_chat_history(limit=20)

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

        try:
            ai_response = client.chat_completion(messages)
        except AIClientError as exc:
            _logger.error(
                'AI client error for agent %s: %s',
                agent.name, exc.message,
            )
            ai_response = f'Sorry, I encountered an error: {exc.message}'

        if not ai_response:
            return

        # Post the AI reply as a message in the channel
        display_name = agent.agent_display_name or agent.name
        ai_message = self.with_context(mail_create_nosubscribe=True).message_post(
            body=ai_response,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.env.ref('base.partner_root').id,
        )

        # Send bus notification for real-time UI update
        channel_info = {
            'id': ai_message.id,
            'body': ai_response,
            'author': {
                'id': self.env.ref('base.partner_root').id,
                'name': display_name,
            },
            'agent_id': agent.id,
            'agent_name': display_name,
            'agent_color': agent.avatar_color or '#875A7B',
        }
        self.env['bus.bus']._sendone(
            self,
            'mail.message/insert',
            channel_info,
        )

        _logger.info(
            'AI agent %s replied in channel %s (id=%s)',
            agent.name, self.name, self.id,
        )

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

        root_partner_id = self.env.ref('base.partner_root').id
        history = []
        for msg in reversed(messages):
            role = 'assistant' if msg.author_id.id == root_partner_id else 'user'
            body = msg.body or ''
            if body:
                history.append({'role': role, 'content': body})
        return history
