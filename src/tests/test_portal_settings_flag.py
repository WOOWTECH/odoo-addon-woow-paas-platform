"""Flag-off（預設）portal 唯讀守線 — HttpCase endpoint-level。

orm-vs-endpoint-acl-test-coverage pattern：controller 層守線必須打真 HTTP
endpoint，ORM with_user 測不到 controller sudo/gate 行為。
重用 test_project_detail_endpoints 的完整 fixture（DRY）。
"""
from odoo.tests import tagged

from .test_project_detail_endpoints import TestProjectDetailFixture


@tagged('post_install', '-at_install')
class TestPortalSettingsFlagOff(TestProjectDetailFixture):
    """旗標未設（fail-closed off）：owner/admin 也不可寫；get/list 照常。"""

    def setUp(self):
        super().setUp()
        # 本 fixture 的 setUp 會開 flag（讓既有正向測試保值）；本 class 驗
        # 預設 off 行為 → 明確關閉。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'False')

    def _url_mcp(self, project_id):
        return f'/api/support-projects/{project_id}/mcp-servers'

    def test_get_still_works_and_reports_readonly(self):
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_detail(self.project_w1_basic.id),
                         {'action': 'get'})
        self.assertTrue(res['success'])
        self.assertFalse(res['data']['portal_editable'])

    def test_update_denied_for_owner(self):
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_detail(self.project_w1_basic.id),
                         {'action': 'update', 'name': 'NEW'})
        self.assertFalse(res['success'])
        self.assertEqual(res['error'], 'Permission denied')
        self.assertEqual(self.project_w1_basic.name, 'PD W1 Basic Project')

    def test_mcp_create_denied_for_owner(self):
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_mcp(self.project_w1_basic.id), {
            'action': 'create', 'name': 'X',
            'url': 'https://mcp.example.com/sse',
        })
        self.assertFalse(res['success'])
        self.assertEqual(res['error'], 'Permission denied')

    def test_mcp_list_still_works(self):
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_mcp(self.project_w1_with_mcp.id),
                         {'action': 'list'})
        self.assertTrue(res['success'])

    def test_support_projects_update_denied_cross_endpoint(self):
        """BLOCKER 守線：ai_assistant 的 /api/support/projects update 同受
        flag gate（two-layer-acl-lockstep 第三支 endpoint）。"""
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post('/api/support/projects', {
            'action': 'update',
            'project_id': self.project_w1_basic.id,
            'name': 'BYPASS',
        })
        self.assertFalse(res['success'])
        self.assertEqual(self.project_w1_basic.name, 'PD W1 Basic Project')

    def test_flag_on_restores_update(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_detail(self.project_w1_basic.id),
                         {'action': 'update', 'name': 'Edited'})
        self.assertTrue(res['success'])
        self.assertTrue(res['data']['portal_editable'])
