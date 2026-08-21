"""TDD — endpoint-level wiring for the unified system context.

Per docs/decisions/patterns/orm-vs-endpoint-acl-test-coverage.md:
builder-level green ≠ endpoint secure. These tests drive the real SSE
HTTP endpoint and assert (a) the legacy path consumes the unified
builder (project/task present, raw helm_values secrets absent — the
pre-existing leak at ai_assistant.py:1007-1008 stays dead), and
(b) non-channel-members are rejected.
"""
import json
import re
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import HttpCase, TransactionCase, tagged
from ..models.cloud_service import _internal_provision


@tagged('post_install', '-at_install')
class TestLegacySseContextWiring(HttpCase):

    def setUp(self):
        super().setUp()
        env = self.env
        suffix = uuid.uuid4().hex[:8]
        self.admin = env.ref('base.user_admin')

        self.ai_config = env['ai.config'].create({
            'name': f'Ctx Cfg {suffix}',
            'type': 'chatgpt',
            'api_key': 'sk-test-ctx',
            'model': 'gpt-4o-mini',
        })
        self.ai_context = env['ai.context'].create({
            'name': f'Ctx {suffix}',
            'context': 'You are a test assistant.',
        })
        self.assistant = env['ai.assistant'].create({
            'name': f'CtxBot{suffix}',
            'config_id': self.ai_config.id,
            'context_id': self.ai_context.id,
        })
        env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.default_ai_assistant_id',
            str(self.assistant.id))

        workspace = env['woow_paas_platform.workspace'].create(
            {'name': f'WS {suffix}'})
        template = env['woow_paas_platform.cloud_app_template'].sudo().create({
            'name': f'Tmpl {suffix}', 'slug': f'tmpl-{suffix}',
            'category': 'web',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'tmpl', 'helm_chart_version': '1.0.0',
            'min_vcpu': 1, 'min_ram_gb': 1.0, 'min_storage_gb': 1,
        })
        self.service = _internal_provision(env['woow_paas_platform.cloud_service'].sudo()).create({
            'name': f'Svc {suffix}',
            'workspace_id': workspace.id,
            'template_id': template.id,
            'subdomain': f'svc-{suffix}',
            'helm_values': json.dumps(
                {'db': {'password': 'leak-pw-9999'}}),
        })
        self.project = env['project.project'].sudo().create({
            'name': f'Ctx Project {suffix}',
            'description': '<p>endpoint project desc</p>',
            'cloud_service_id': self.service.id,
        })
        self.channel = env['discuss.channel'].sudo().create({
            'name': f'ch-{suffix}', 'channel_type': 'channel',
            'channel_partner_ids': [(4, self.admin.partner_id.id)],
        })
        self.task = env['project.task'].sudo().create({
            'name': f'Ctx Task {suffix}',
            'project_id': self.project.id,
            'channel_id': self.channel.id,
            'ai_auto_reply': True,
        })
        # a user message so the SSE endpoint finds something to answer
        self.channel.sudo().with_context(skip_ai_reply=True).message_post(
            body='hello agent', message_type='comment',
            author_id=self.admin.partner_id.id)

    def _csrf_token(self):
        """Whole-value capture — see test_agent_pod_phase_c_sse_gateway."""
        for page in ('/woow', '/web'):
            resp = self.url_open(page, timeout=15)
            if resp.status_code != 200:
                continue
            m = re.search(
                r'csrf_token["\']?\s*[:=]\s*["\']([^"\']+)["\']', resp.text)
            if m:
                return m.group(1)
        self.fail('csrf token could not be extracted (fixture issue)')

    def test_legacy_sse_system_prompt_unified_and_leak_free(self):
        from odoo.addons.woow_paas_platform.models.ai_client import AIClient

        captured = {}

        def fake_from_assistant(assistant):
            client = MagicMock()

            def build_messages(system_prompt='', history=None,
                               user_message=''):
                captured['system_prompt'] = system_prompt
                return []

            client.build_messages.side_effect = build_messages
            client.chat_completion_stream.return_value = iter(['ok'])
            return client

        self.authenticate('admin', 'admin')
        token = self._csrf_token()
        with patch.object(AIClient, 'from_assistant',
                          staticmethod(fake_from_assistant)):
            resp = self.url_open(
                f'/api/ai/stream/{self.channel.id}?csrf_token={token}',
                timeout=30)
        self.assertEqual(resp.status_code, 200)
        prompt = captured.get('system_prompt', '')
        # unified builder wired in
        self.assertIn('### Project', prompt)
        self.assertIn('### Task', prompt)
        # assistant context prefix preserved
        self.assertIn('You are a test assistant.', prompt)
        # the old raw helm_values dump is DEAD (no keys whitelisted)
        self.assertNotIn('leak-pw-9999', prompt)
        self.assertNotIn('- Configuration:', prompt)

    def test_non_member_denied(self):
        env = self.env
        env['res.users'].sudo().create({
            'name': 'Ctx Outsider', 'login': 'ctx_outsider',
            'password': 'ctx_outsider_pwd',
            'groups_id': [(6, 0, [env.ref('base.group_user').id])],
        })
        self.authenticate('ctx_outsider', 'ctx_outsider_pwd')
        token = self._csrf_token()
        resp = self.url_open(
            f'/api/ai/stream/{self.channel.id}?csrf_token={token}',
            timeout=30)
        self.assertEqual(resp.status_code, 200)  # SSE error frame, not 403
        self.assertIn('access_denied', resp.text)
        self.assertNotIn('Ctx Project', resp.text)


@tagged('post_install', '-at_install')
class TestRuntimeSystemContext(TransactionCase):
    """C path: per-turn system_context string + payload shape (additive)."""

    def setUp(self):
        super().setUp()
        env = self.env
        self.Session = env['woow_paas_platform.agent_session']
        self.project = env['project.project'].sudo().create({
            'name': 'RT Project',
            'description': '<p>rt project desc</p>',
        })
        self.channel = env['discuss.channel'].sudo().create({
            'name': 'rt-ch', 'channel_type': 'channel'})
        self.task = env['project.task'].sudo().create({
            'name': 'RT Task',
            'project_id': self.project.id,
            'channel_id': self.channel.id,
        })
        self.template = env['woow_paas_platform.agent_template'].sudo().create({
            'name': 'rt-tmpl',
            'system_prompt': 'BASE PROMPT — be helpful.',
        })

    def test_runtime_context_prefixes_template_system_prompt(self):
        ctx = self.Session._build_runtime_system_context(
            self.channel, self.template)
        self.assertTrue(ctx.startswith('BASE PROMPT — be helpful.'))
        self.assertIn('### Project', ctx)
        self.assertIn('- Name: RT Task', ctx)

    def test_runtime_context_without_template_prompt(self):
        self.template.sudo().write({'system_prompt': False})
        ctx = self.Session._build_runtime_system_context(
            self.channel, self.template)
        self.assertTrue(ctx.startswith('## Background Context'))

    def test_runtime_context_all_empty(self):
        self.template.sudo().write({'system_prompt': False})
        orphan = self.env['discuss.channel'].sudo().create({
            'name': 'rt-orphan', 'channel_type': 'channel'})
        self.assertEqual(
            self.Session._build_runtime_system_context(orphan, self.template),
            '')

    def test_user_message_payload_shape(self):
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        build = AiAssistantController._build_runtime_user_message_payload
        payload = build(7, 'hi', [{'role': 'user', 'content': 'x'}],
                        42, 'owner', 2, 'CTX STRING')
        self.assertEqual(payload['channel_id'], 7)
        self.assertEqual(payload['template_id'], 42)
        self.assertEqual(payload['system_context'], 'CTX STRING')
        # additive contract: falsy context → key OMITTED (old-pod shape)
        payload_empty = build(7, 'hi', [], 42, 'owner', 2, '')
        self.assertNotIn('system_context', payload_empty)
        self.assertEqual(
            set(payload_empty), {
                'channel_id', 'body', 'history', 'template_id',
                'invoker_role', 'invoker_user_id'},
            'payload base shape must stay identical to ws-protocol §3')
