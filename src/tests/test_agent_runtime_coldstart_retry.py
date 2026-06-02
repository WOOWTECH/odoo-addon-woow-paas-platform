"""#123 cold-start retry: client timeout 分級 + spawn retry + WS connect retry."""
import os
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

_CLIENT_MOD = "odoo.addons.woow_paas_platform.services.agent_runtime_client"
_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'


@tagged('post_install', '-at_install')
class TestColdStartClientTimeout(TransactionCase):
    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        import importlib
        self.mod = importlib.import_module(_CLIENT_MOD)
        self.client = self.mod.get_agent_runtime_client(self.env)

    def _ok(self, payload=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.ok = True
        resp.json.return_value = payload or {}
        return resp

    def test_spawn_uses_90s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok(
                {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                 'pod_state': 'active', 'spawn_age_ms': 10})
            project = self.env['project.project'].sudo().create(
                {'name': f'to-{uuid.uuid4().hex[:6]}'})
            self.client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='user',
                agent_template=MagicMock(id=1, model_config_id=None),
                mcp_snapshot={}, skill_snapshot=[])
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 90)

    def test_invalidate_uses_10s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok({})
            self.client.invalidate_mcp('pa1234567890abcd', {})
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 10)

    def test_reap_keeps_default_30s_timeout(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._ok({})
            self.client.reap('pa1234567890abcd')
            self.assertEqual(mock_requests.request.call_args.kwargs.get('timeout'), 30)
