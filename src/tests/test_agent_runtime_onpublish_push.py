"""#111 on-publish push: client invalidate_skills + agent_session 推送協調 +
skill/mcp 觸發 hook（best-effort）。"""
import os
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

_CLIENT_MOD = "odoo.addons.woow_paas_platform.services.agent_runtime_client"
_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'


@tagged('post_install', '-at_install')
class TestInvalidateSkillsClient(TransactionCase):
    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        import importlib
        self.mod = importlib.import_module(_CLIENT_MOD)
        self.client = self.mod.get_agent_runtime_client(self.env)

    def test_invalidate_skills_posts_list(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            resp = MagicMock()
            resp.status_code = 200
            resp.ok = True
            resp.json.return_value = {'patched': True}
            mock_requests.request.return_value = resp
            snap = [{'name': 'sk', 'version': '1.0.0', 'sha256': 'a' * 64,
                     'description': ''}]
            self.client.invalidate_skills('pa1234567890abcd', snap)
            call = mock_requests.request.call_args
            import json as _j
            blob = _j.dumps([call.args, call.kwargs], default=str)
            self.assertIn('pa1234567890abcd/invalidate-skills', blob)
            self.assertEqual(call.kwargs.get('json'), {'skill_snapshot': snap})
            self.assertEqual(call.kwargs.get('timeout'), 10)  # short timeout


@tagged('post_install', '-at_install')
class TestPushCoordination(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()

    def _active_session(self):
        project = self.env['project.project'].sudo().create(
            {'name': f'ps-{uuid.uuid4().hex[:6]}', 'use_agent_runtime': True})
        # 與 seeded platform default 解耦：明確綁一個 published template，讓
        # _resolve_agent_template 第一個 search 就命中。否則測試隱式依賴 seed
        # 的 agent_template_default + post_init_hook 設 default_agent_template_id，
        # seed 缺失/未發布/綁 project 時 push 不觸發 → 假紅（且原因誤導）。
        self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': f'tmpl-{uuid.uuid4().hex[:6]}',
            'is_public': True,
            'state': 'published',
            'project_ids': [(6, 0, [project.id])],
        })
        session = self.Session.create({'project_id': project.id})
        session.write({'state': 'active'})
        return project, session

    def test_system_scope_pushes_all_active(self):
        self._active_session()
        self._active_session()
        fake = MagicMock()
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=fake):
            self.Session._push_snapshot_to_running_pods('skill', 'system')
        self.assertEqual(fake.invalidate_skills.call_count, 2)

    def test_user_scope_pushes_only_that_project(self):
        p1, _ = self._active_session()
        self._active_session()
        fake = MagicMock()
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=fake):
            self.Session._push_snapshot_to_running_pods('skill', 'user', p1.id)
        self.assertEqual(fake.invalidate_skills.call_count, 1)

    def test_no_client_is_noop(self):
        self._active_session()
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=None):
            # must not raise
            self.Session._push_snapshot_to_running_pods('skill', 'system')

    def test_single_pod_failure_does_not_abort(self):
        self._active_session()
        self._active_session()
        fake = MagicMock()
        fake.invalidate_skills.side_effect = [Exception('boom'), {'ok': True}]
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=fake):
            self.Session._push_snapshot_to_running_pods('skill', 'system')
        self.assertEqual(fake.invalidate_skills.call_count, 2)  # 第一個失敗不中斷

    def test_reaped_session_not_pushed(self):
        project = self.env['project.project'].sudo().create(
            {'name': f'rp-{uuid.uuid4().hex[:6]}', 'use_agent_runtime': True})
        session = self.Session.create({'project_id': project.id})
        session.write({'state': 'reaped'})
        fake = MagicMock()
        with patch(_CLIENT_MOD + '.get_agent_runtime_client', return_value=fake):
            self.Session._push_snapshot_to_running_pods('skill', 'system')
        self.assertEqual(fake.invalidate_skills.call_count, 0)  # 只推 active


@tagged('post_install', '-at_install')
class TestSkillPublishTriggersPush(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')

    def test_system_skill_publish_calls_push(self):
        skill = self.env['woow_paas_platform.skill'].sudo().create({
            'name': f'sk-{uuid.uuid4().hex[:6]}', 'version': '1.0.0',
            'scope': 'system', 'state': 'draft', 'bundle_sha256': 'a' * 64})
        Session = self.env['woow_paas_platform.agent_session']
        with patch.object(type(Session), '_push_snapshot_to_running_pods') as push:
            skill.action_publish()
            push.assert_called()
            # scope='system' 傳遞正確
            args = push.call_args.args
            self.assertEqual(args[0], 'skill')
            self.assertEqual(args[1], 'system')

    def test_recompute_is_latest_does_not_trigger_push(self):
        """_recompute_is_latest 內部 write 的是 is_latest（不含 state）→ 不推送，
        不遞迴。"""
        skill = self.env['woow_paas_platform.skill'].sudo().create({
            'name': f'sk-{uuid.uuid4().hex[:6]}', 'version': '1.0.0',
            'scope': 'system', 'state': 'published', 'bundle_sha256': 'a' * 64})
        Session = self.env['woow_paas_platform.agent_session']
        with patch.object(type(Session), '_push_snapshot_to_running_pods') as push:
            skill._recompute_is_latest()
            push.assert_not_called()


@tagged('post_install', '-at_install')
class TestMcpChangeTriggersPush(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')

    def _mcp(self, **kw):
        vals = {'name': f'mcp-{uuid.uuid4().hex[:6]}', 'scope': 'system',
                'transport': 'sse', 'url': 'https://a.example/sse'}
        vals.update(kw)
        return self.env['woow_paas_platform.mcp_server'].sudo().create(vals)

    def test_url_change_calls_mcp_push(self):
        server = self._mcp()
        Session = self.env['woow_paas_platform.agent_session']
        with patch.object(type(Session), '_push_snapshot_to_running_pods') as push:
            server.write({'url': 'https://b.example/sse'})
            push.assert_called()
            args = push.call_args.args
            self.assertEqual(args[0], 'mcp')
            self.assertEqual(args[1], 'system')

    def test_non_snapshot_field_change_no_push(self):
        """state_message 不在 snapshot（snapshot 只有 name/url/transport）→ 不推。"""
        server = self._mcp()
        Session = self.env['woow_paas_platform.agent_session']
        with patch.object(type(Session), '_push_snapshot_to_running_pods') as push:
            server.write({'state_message': 'hello'})
            push.assert_not_called()
