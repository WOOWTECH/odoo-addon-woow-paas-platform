import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.services.agent_runtime_client import (
    AgentRuntimeOperatorError,
)

_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'
_CLIENT_PATH = (
    "odoo.addons.woow_paas_platform.services."
    "agent_runtime_client.get_agent_runtime_client"
)


@tagged('post_install', '-at_install')
class TestGcReaped(TransactionCase):
    def setUp(self):
        super().setUp()
        patcher = patch.dict(
            os.environ, {_ENV_NAMESPACE_SECRET: 'unit-test-secret-0123456789'})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.agent_gc_seconds', '2592000')
        self.project = self.env['project.project'].sudo().create({'name': 'GC'})
        self.session = self.Session.create({'project_id': self.project.id})
        self.session.write({'state': 'reaped'})
        self.hash = self.session.project_namespace_hash

    def _set_reaped_age(self, seconds):
        self.session.write({
            'reaped_since': fields.Datetime.now() - timedelta(seconds=seconds)})

    def test_gc_expired_reaped(self):
        self._set_reaped_age(31 * 24 * 3600)  # 31 天 > 30
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_called_once_with(self.hash)
        self.assertEqual(self.session.state, 'absent')

    def test_skips_fresh_reaped(self):
        self._set_reaped_age(24 * 3600)  # 1 天 < 30
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_not_called()
        self.assertEqual(self.session.state, 'reaped')

    def test_skips_non_reaped(self):
        self.session.write({'state': 'active'})
        self._set_reaped_age(31 * 24 * 3600)
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_not_called()

    def test_skips_null_reaped_since(self):
        self.session.write({'reaped_since': False})
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_not_called()
        self.assertEqual(self.session.state, 'reaped')

    def test_best_effort_on_teardown_failure(self):
        self._set_reaped_age(31 * 24 * 3600)
        client = MagicMock()
        client.delete.side_effect = AgentRuntimeOperatorError("operator 5xx")
        with patch(_CLIENT_PATH, return_value=client):
            # 不可拋例外（cron 不能整批炸）
            self.Session._cron_gc_reaped()
        self.assertEqual(self.session.state, 'reaped')  # 留 reaped 隔天重試

    def test_t_gc_config_respected(self):
        self._set_reaped_age(2 * 3600)  # 2h
        self.ICP.set_param('woow_paas_platform.agent_gc_seconds', '3600')  # 1h
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_gc_reaped()
        client.delete.assert_called_once()  # 2h > 1h → GC
        self.assertEqual(self.session.state, 'absent')
