# -*- coding: utf-8 -*-
# Copyright (C) Softhealer Technologies.

from odoo.tests.common import TransactionCase
from datetime import datetime

class TestAiSession(TransactionCase):

    def setUp(self):
        super(TestAiSession, self).setUp()
        self.llm = self.env['sh.ai.llm'].create({
            'name': 'Test Gemini',
            'sh_company': 'Google',
            'sh_model_code': 'gemini-1.5-flash',
        })
        self.user = self.env.user

    def test_session_creation(self):
        """Test creating a new chat session"""
        session_data = self.env['sh.ai.chat.session'].create_new_session()
        self.assertTrue(session_data['id'])
        self.assertTrue(session_data['access_token'])
        
        session = self.env['sh.ai.chat.session'].browse(session_data['id'])
        self.assertEqual(session.user_id, self.user)

    def test_find_by_token(self):
        """Test finding a session by its access token"""
        session = self.env['sh.ai.chat.session'].create({
            'name': 'Token Test',
            'user_id': self.user.id
        })
        token = session.access_token
        
        found_data = self.env['sh.ai.chat.session'].find_by_token(token)
        self.assertEqual(found_data['id'], session.id)

    def test_message_stats_aggregation(self):
        """Test that session-level stats are aggregated from messages"""
        session = self.env['sh.ai.chat.session'].create({
            'name': 'Stats Test',
            'llm_id': self.llm.id,
            'user_id': self.user.id
        })
        
        # Create message with stats
        self.env['sh.ai.chat.message'].create({
            'session_id': session.id,
            'message_type': 'assistant',
            'content': 'Hello',
            'prompt_tokens': 100,
            'completion_tokens': 50,
            'total_tokens': 150,
            'tool_call_count': 2,
            'execution_time': 1.5
        })
        
        # Create second message
        self.env['sh.ai.chat.message'].create({
            'session_id': session.id,
            'message_type': 'assistant',
            'content': 'World',
            'prompt_tokens': 200,
            'completion_tokens': 100,
            'total_tokens': 300,
            'tool_call_count': 3,
            'execution_time': 2.5
        })
        
        # Trigger computation
        session._compute_message_stats()
        
        self.assertEqual(session.total_prompt_tokens, 300)
        self.assertEqual(session.total_completion_tokens, 150)
        self.assertEqual(session.total_session_tokens, 450)
        self.assertEqual(session.total_tool_calls, 5)
        self.assertEqual(session.total_execution_time, 4.0)
