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
