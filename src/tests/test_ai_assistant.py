"""Tests for AI assistant integration with ai_base_gt."""
from unittest.mock import patch, MagicMock

from odoo.tests.common import TransactionCase


class TestAIAssistantIntegration(TransactionCase):
    """Test that AI assistant models and services work correctly
    after refactoring to use ai_base_gt."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Create a test AI config
        cls.ai_config = cls.env['ai.config'].create({
            'name': 'Test Config',
            'type': 'chatgpt',
            'api_key': 'sk-test-key-12345',
            'model': 'gpt-4o-mini',
            'temperature': 0.7,
            'max_tokens': 2000,
        })

        # Create a test AI context
        cls.ai_context = cls.env['ai.context'].create({
            'name': 'Test Context',
            'context': 'You are a test assistant.',
        })

        # Create a test AI assistant
        cls.ai_assistant = cls.env['ai.assistant'].create({
            'name': 'TestBot',
            'config_id': cls.ai_config.id,
            'context_id': cls.ai_context.id,
            'description': 'Test AI assistant',
        })

    def test_assistant_has_partner(self):
        """AI assistant should have an associated res.partner (via _inherits)."""
        self.assertTrue(self.ai_assistant.partner_id)
        self.assertEqual(self.ai_assistant.partner_id.name, 'TestBot')

    def test_config_has_api_base_url(self):
        """Our ai.config extension should provide the api_base_url field."""
        self.assertFalse(self.ai_config.api_base_url)
        self.ai_config.api_base_url = 'https://custom.api.example.com/v1'
        self.assertEqual(self.ai_config.api_base_url, 'https://custom.api.example.com/v1')

    def test_ai_client_from_assistant(self):
        """AIClient.from_assistant should create a client from an assistant record."""
        from ..models.ai_client import AIClient

        client = AIClient.from_assistant(self.ai_assistant)
        self.assertIsInstance(client, AIClient)

    def test_ai_client_from_assistant_no_config(self):
        """AIClient.from_assistant should raise if assistant has no config."""
        from ..models.ai_client import AIClient, AIClientError

        assistant_no_config = self.env['ai.assistant'].create({
            'name': 'NoConfigBot',
        })
        with self.assertRaises(AIClientError):
            AIClient.from_assistant(assistant_no_config)

    def test_ai_client_from_assistant_no_api_key(self):
        """AIClient.from_assistant should raise if config has no API key."""
        from ..models.ai_client import AIClient, AIClientError

        config_no_key = self.env['ai.config'].create({
            'name': 'No Key Config',
            'type': 'chatgpt',
            'model': 'gpt-4o-mini',
        })
        assistant = self.env['ai.assistant'].create({
            'name': 'NoKeyBot',
            'config_id': config_no_key.id,
        })
        with self.assertRaises(AIClientError):
            AIClient.from_assistant(assistant)

    def test_default_assistant_setting(self):
        """Settings should correctly store and retrieve default AI assistant."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.default_ai_assistant_id',
            str(self.ai_assistant.id),
        )
        param = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_ai_assistant_id',
        )
        self.assertEqual(int(param), self.ai_assistant.id)


class TestDiscussChannelAI(TransactionCase):
    """Test discuss.channel AI reply integration."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ai_config = cls.env['ai.config'].create({
            'name': 'Channel Test Config',
            'type': 'chatgpt',
            'api_key': 'sk-test-key-channel',
            'model': 'gpt-4o-mini',
            'temperature': 0.7,
            'max_tokens': 2000,
        })
        cls.ai_context = cls.env['ai.context'].create({
            'name': 'Channel Test Context',
            'context': 'You are a test assistant for channel testing.',
        })
        cls.ai_assistant = cls.env['ai.assistant'].create({
            'name': 'ChannelBot',
            'config_id': cls.ai_config.id,
            'context_id': cls.ai_context.id,
        })

    def test_detect_ai_assistant_mention(self):
        """_detect_ai_assistant should find assistant when @mentioned in body."""
        channel = self.env['discuss.channel'].create({
            'name': 'Test Channel',
            'channel_type': 'channel',
        })
        result = channel._detect_ai_assistant(f'Hello @{self.ai_assistant.name}, help me')
        self.assertTrue(result)
        self.assertEqual(result.id, self.ai_assistant.id)

    def test_detect_ai_assistant_no_mention(self):
        """_detect_ai_assistant should return empty when no mention found."""
        channel = self.env['discuss.channel'].create({
            'name': 'Test Channel 2',
            'channel_type': 'channel',
        })
        result = channel._detect_ai_assistant('Hello, no mention here')
        self.assertFalse(result)

    def test_get_auto_reply_assistant(self):
        """_get_auto_reply_assistant should return default assistant
        when channel is linked to a task with ai_auto_reply."""
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.default_ai_assistant_id',
            str(self.ai_assistant.id),
        )

        channel = self.env['discuss.channel'].create({
            'name': 'Auto Reply Channel',
            'channel_type': 'channel',
        })

        # Create a project and task linked to the channel
        project = self.env['project.project'].create({'name': 'Test Project'})
        task = self.env['project.task'].create({
            'name': 'Test Task',
            'project_id': project.id,
            'channel_id': channel.id,
            'ai_auto_reply': True,
        })

        result = channel._get_auto_reply_assistant()
        self.assertTrue(result)
        self.assertEqual(result.id, self.ai_assistant.id)

    def test_get_auto_reply_assistant_no_task(self):
        """_get_auto_reply_assistant should return empty when no linked task."""
        channel = self.env['discuss.channel'].create({
            'name': 'No Task Channel',
            'channel_type': 'channel',
        })
        result = channel._get_auto_reply_assistant()
        self.assertFalse(result)

    def test_get_chat_history(self):
        """_get_chat_history should return messages with correct roles."""
        channel = self.env['discuss.channel'].create({
            'name': 'History Channel',
            'channel_type': 'channel',
        })
        user_partner = self.env.user.partner_id
        ai_partner = self.ai_assistant.partner_id

        # Post a user message
        channel.message_post(
            body='User message',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=user_partner.id,
        )
        # Post an AI message
        channel.with_context(skip_ai_reply=True).message_post(
            body='AI response',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=ai_partner.id,
        )

        history = channel._get_chat_history(limit=10)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['role'], 'user')
        self.assertEqual(history[0]['content'], 'User message')
        self.assertEqual(history[1]['role'], 'assistant')
        self.assertEqual(history[1]['content'], 'AI response')

    @patch('odoo.addons.woow_paas_platform.models.ai_client.AIClient')
    def test_post_ai_reply(self, MockAIClient):
        """_post_ai_reply should generate and post AI response."""
        mock_client = MagicMock()
        mock_client.build_messages.return_value = [{'role': 'user', 'content': 'hello'}]
        mock_client.chat_completion.return_value = 'AI says hello back'
        MockAIClient.from_assistant.return_value = mock_client

        channel = self.env['discuss.channel'].create({
            'name': 'Reply Channel',
            'channel_type': 'channel',
        })

        user_msg = channel.with_context(skip_ai_reply=True).message_post(
            body='hello',
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=self.env.user.partner_id.id,
        )

        channel._post_ai_reply(self.ai_assistant, 'hello', user_msg)

        # Verify AI message was posted
        ai_messages = self.env['mail.message'].search([
            ('res_id', '=', channel.id),
            ('model', '=', 'discuss.channel'),
            ('author_id', '=', self.ai_assistant.partner_id.id),
        ])
        self.assertTrue(ai_messages)
        self.assertIn('AI says hello back', ai_messages[0].body)
