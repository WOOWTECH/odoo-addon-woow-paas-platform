import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

_ENV_NAMESPACE_SECRET = 'AGENT_RUNTIME_NAMESPACE_SECRET'
_CLIENT_PATH = (
    "odoo.addons.woow_paas_platform.services."
    "agent_runtime_client.get_agent_runtime_client"
)


@tagged('post_install', '-at_install')
class TestRecordActivity(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.project = self.env['project.project'].sudo().create({'name': 'HB'})
        self.session = self.Session.create({'project_id': self.project.id})

    def test_stamps_when_last_active_empty(self):
        self.assertFalse(self.session.last_active)
        self.Session._record_activity(self.project)
        self.assertTrue(self.session.last_active)

    def test_stamps_when_stale(self):
        self.session.write({
            'last_active': fields.Datetime.now() - timedelta(seconds=120)})
        before = self.session.last_active
        self.Session._record_activity(self.project)
        self.assertGreater(self.session.last_active, before)

    def test_skips_when_recent(self):
        recent = fields.Datetime.now() - timedelta(seconds=5)
        self.session.write({'last_active': recent})
        self.Session._record_activity(self.project)
        self.assertEqual(self.session.last_active, recent)

    def test_noop_when_no_session(self):
        proj2 = self.env['project.project'].sudo().create({'name': 'NoSess'})
        # 不應拋例外（該 project 無 session）
        self.Session._record_activity(proj2)


@tagged('post_install', '-at_install')
class TestIdleReaper(TransactionCase):
    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.ICP.set_param('woow_paas_platform.agent_idle_reap_seconds', '1800')
        self.project = self.env['project.project'].sudo().create({'name': 'R'})
        self.session = self.Session.create({'project_id': self.project.id})
        self.session.write({'state': 'active'})

    def _set_idle(self, seconds):
        self.session.write({
            'last_active': fields.Datetime.now() - timedelta(seconds=seconds)})

    def test_reaps_expired_active(self):
        self._set_idle(3600)  # 1h > 30min
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_called_once_with(self.session.project_namespace_hash)
        self.assertEqual(self.session.state, 'reaped')
        self.assertTrue(self.session.reaped_since)

    def test_skips_fresh_active(self):
        self._set_idle(60)  # 1min < 30min
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_not_called()
        self.assertEqual(self.session.state, 'active')

    def test_skips_non_active(self):
        self.session.write({'state': 'reaped'})
        self._set_idle(3600)
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_not_called()

    def test_best_effort_on_reap_failure(self):
        from odoo.addons.woow_paas_platform.services.agent_runtime_client \
            import AgentRuntimeOperatorError
        self._set_idle(3600)
        client = MagicMock()
        client.reap.side_effect = AgentRuntimeOperatorError("operator 5xx")
        with patch(_CLIENT_PATH, return_value=client):
            # 不可拋例外（cron 不能整批炸）
            self.Session._cron_idle_reaper()
        self.assertEqual(self.session.state, 'active')  # 留 active 下輪重試

    def test_null_last_active_falls_back_to_spawn_at(self):
        # 模擬 legacy：last_active 空、spawn_at 久遠
        self.session.write({
            'last_active': False,
            'spawn_at': fields.Datetime.now() - timedelta(seconds=3600)})
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_called_once()
        self.assertEqual(self.session.state, 'reaped')

    def test_t_idle_config_respected(self):
        self._set_idle(120)  # 2min
        self.ICP.set_param('woow_paas_platform.agent_idle_reap_seconds', '60')
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.Session._cron_idle_reaper()
        client.reap.assert_called_once()  # 2min > 60s → reap
