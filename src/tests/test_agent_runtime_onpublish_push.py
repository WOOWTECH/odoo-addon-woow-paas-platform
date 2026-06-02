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
