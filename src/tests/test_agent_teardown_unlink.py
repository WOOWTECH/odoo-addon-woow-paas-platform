"""D2 — agent runtime teardown 孤兒防護（design §2.6）。

刪 project 時同步呼叫 operator DELETE 拆除 runtime（agent_session.project_id
是 ondelete='cascade'，不先拆除會留 helm 孤兒）；**失敗就阻塞刪除**
（synchronous，raise UserError），寧可不刪也不留孤兒。

註：workspace 刪除「不」孤兒化 agent pod —— workspace→cloud_service 雖 cascade，
但 cloud_service↔project 是 M2M，刪 workspace 不刪 project/session，故無 workspace
hook（schema 實證）。
"""
import os
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
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
class TestTeardownRuntime(TransactionCase):
    """Task 3 — agent_session._teardown_runtime()."""

    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.project = self.env['project.project'].sudo().create({'name': 'T'})
        self.session = self.Session.create({'project_id': self.project.id})

    def test_teardown_calls_client_delete_with_hash(self):
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.session._teardown_runtime()
        client.delete.assert_called_once_with(self.session.project_namespace_hash)

    def test_teardown_no_hash_is_noop(self):
        self.session.project_namespace_hash = False
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.session._teardown_runtime()
        client.delete.assert_not_called()

    def test_teardown_no_client_raises(self):
        """有 hash 但 operator 未設定 → 無法確認拆除 → 上拋（caller 阻塞）。"""
        with patch(_CLIENT_PATH, return_value=None):
            with self.assertRaises(AgentRuntimeOperatorError):
                self.session._teardown_runtime()

    def test_teardown_propagates_client_error(self):
        client = MagicMock()
        client.delete.side_effect = AgentRuntimeOperatorError("operator 5xx")
        with patch(_CLIENT_PATH, return_value=client):
            with self.assertRaises(AgentRuntimeOperatorError):
                self.session._teardown_runtime()


@tagged('post_install', '-at_install')
class TestProjectUnlinkTeardown(TransactionCase):
    """Task 4 — project.unlink() 同步拆除 + 失敗阻塞。"""

    def setUp(self):
        super().setUp()
        os.environ[_ENV_NAMESPACE_SECRET] = 'unit-test-secret-0123456789'
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)
        self.Session = self.env['woow_paas_platform.agent_session'].sudo()
        self.project = self.env['project.project'].sudo().create({'name': 'P'})
        self.session = self.Session.create({'project_id': self.project.id})
        self.hash = self.session.project_namespace_hash

    def test_unlink_success_tears_down_then_deletes(self):
        client = MagicMock()
        with patch(_CLIENT_PATH, return_value=client):
            self.project.unlink()
        client.delete.assert_called_once_with(self.hash)
        self.assertFalse(self.project.exists())
        self.assertFalse(self.session.exists())

    def test_unlink_teardown_failure_blocks_delete(self):
        client = MagicMock()
        client.delete.side_effect = AgentRuntimeOperatorError("operator down")
        with patch(_CLIENT_PATH, return_value=client):
            with self.assertRaises(UserError):
                self.project.unlink()
        # 阻塞：raise 發生在 super().unlink() 之前 → project / session 都還在
        self.assertTrue(self.project.exists())
        self.assertTrue(self.session.exists())

# 註：原 Task 5（workspace.unlink hook）已移除 —— 真實 schema 下刪 workspace
# 不會 PG-cascade 刪 project（project.workspace_id 是 computed，非 FK；
# workspace→cloud_service→project 末段是 M2M，刪 cloud_service 不刪 project），
# 故 workspace 刪除不會孤兒化 agent pod。詳見回報。
