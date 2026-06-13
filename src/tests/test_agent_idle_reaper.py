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
