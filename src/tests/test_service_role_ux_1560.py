"""#1560：工作區 user／guest 在服務頁的體驗（2026-10-07 第二階段實測 N5／N6／N10）。

* **N5**：user／guest 打開服務頁，管理員帳密、變更方案、自訂網域、重新啟動、回復版本、
  刪除、略過試用全部照樣顯示、可按，按了才被 API 拒絕。修法：detail payload 帶
  ``viewer_can_manage``（後端 ``_can_manage_service``，與各寫入端點的 role gate 同一個
  判準），前端只讀它（fail-closed）。
* **N6**：同一個「權限不足」情境，rollback／reset-admin-password／admin-credentials
  對 user／guest 回「找不到工作區，或沒有存取權限」——他們明明看得到工作區。修法：
  先驗成員資格、再驗角色，角色不足回「權限不足」（與 update／delete／restart 同款）。
  非成員照舊回「找不到工作區」。
* **N10**：「重設管理員密碼」一按就直接重設。修法：先開確認框（ResetAdminPasswordModal）。
* 回復版本對話框的狀態文字（helm 原文）翻成中文。

前端部分沿用 ``test_admin_credentials_tenant_set`` 的靜態讀檔契約慣例。
"""
import json
import os
import re
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN, ROLE_GUEST, ROLE_USER,
)
from ..models.cloud_service import _internal_provision

_OPERATOR_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAAS = os.path.join(_SRC, 'static', 'src', 'paas')

PERMISSION_DENIED = 'Permission denied'
NOT_FOUND = 'Workspace not found or access denied'


def _read(*parts):
    with open(os.path.join(_PAAS, *parts), encoding='utf-8') as handle:
        return handle.read()


class _RoleFixture(TransactionCase):
    """一台 running 的 has_admin_gui 服務（可 Reset），工作區有 admin／user／guest 三個成員。"""

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Role UX 1560 workspace',
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Role UX 1560',
            'slug': 'role-ux-1560',
            'category': 'mcp',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/mcp-server',
            'helm_chart_version': '0.1.5',
            'mcp_enabled': False,
            'has_admin_gui': True,
            'helm_default_values': json.dumps({}),
        })
        self.service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].with_context(
                mcp_admin_password='old-password-1560',
            )).create({
                'name': 'Role UX svc',
                'workspace_id': self.workspace.id,
                'template_id': self.template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-test-1560',
                'helm_release_name': 'role-ux-1560',
            })
        self.admin = self._make_member('role-1560-admin', ROLE_ADMIN)
        self.user = self._make_member('role-1560-user', ROLE_USER)
        self.guest = self._make_member('role-1560-guest', ROLE_GUEST)
        self.outsider = self._make_user('role-1560-outsider')

    def _make_user(self, login):
        return self.env['res.users'].create({
            'name': login, 'login': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })

    def _make_member(self, login, role):
        user = self._make_user(login)
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': role,
        })
        return user

    def _as(self, user):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=self.env(user=user)),
        )

    def _secret_password(self):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', self.service.id)], limit=1).admin_password


@tagged('post_install', '-at_install')
class TestViewerCanManagePayload1560(_RoleFixture):
    """N5 的後端半邊：detail payload 告訴前端呼叫者能不能操作。"""

    def _get(self, user):
        with self._as(user):
            return self.controller.api_workspace_service(
                self.workspace.id, self.service.id, action='get')

    def test_admin_can_manage(self):
        result = self._get(self.admin)
        self.assertTrue(result.get('success'), result)
        self.assertIs(result['data'].get('viewer_can_manage'), True)

    def test_user_and_guest_cannot_manage(self):
        for member in (self.user, self.guest):
            with self.subTest(login=member.login):
                result = self._get(member)
                self.assertTrue(result.get('success'), result)
                self.assertIs(result['data'].get('viewer_can_manage'), False)

    def test_predicate_matches_the_write_gate(self):
        """判準與寫入端點的 role gate 同一個：update 對 user 回權限不足、對 admin 放行到業務層。"""
        with self._as(self.user):
            denied = self.controller.api_workspace_service(
                self.workspace.id, self.service.id, action='update', values={})
        self.assertEqual(denied.get('error'), PERMISSION_DENIED)
        for role, expected in ((ROLE_ADMIN, True), ('owner', True),
                               (ROLE_USER, False), (ROLE_GUEST, False)):
            with self.subTest(role=role):
                self.assertIs(
                    PaasController._can_manage_service(MagicMock(role=role)), expected)
        self.assertFalse(PaasController._can_manage_service(False))

    def test_list_payload_does_not_claim_management(self):
        """list 不帶這個欄位（前端 fail-closed，缺席＝不能管理）。"""
        with self._as(self.admin):
            result = self.controller.api_workspace_services(self.workspace.id, action='list')
        self.assertTrue(result.get('success'), result)
        for item in result['data']:
            self.assertNotIn('viewer_can_manage', item)


@tagged('post_install', '-at_install')
class TestPermissionDeniedWording1560(_RoleFixture):
    """N6：角色不足回「權限不足」，非成員才回「找不到工作區」；兩者都不碰 operator。"""

    def _rollback(self, user):
        with self._as(user):
            return self.controller.api_service_rollback(
                self.workspace.id, self.service.id, revision=1)

    def _reset(self, user):
        with self._as(user):
            return self.controller.api_service_reset_admin_password(
                self.workspace.id, self.service.id)

    def _set_credentials(self, user):
        with self._as(user):
            return self.controller.api_service_set_admin_credentials(
                self.workspace.id, self.service.id, password='Str0ng-Tenant-Pass!')

    @patch(_OPERATOR_CLIENT)
    def test_members_without_manage_role_get_permission_denied(self, get_client):
        get_client.return_value = MagicMock()
        for member in (self.user, self.guest):
            for name, call in (('rollback', self._rollback), ('reset', self._reset),
                               ('admin-credentials', self._set_credentials)):
                with self.subTest(login=member.login, endpoint=name):
                    result = call(member)
                    self.assertFalse(result.get('success'))
                    self.assertEqual(result.get('error'), PERMISSION_DENIED)
        get_client.assert_not_called()
        self.assertEqual(self._secret_password(), 'old-password-1560',
                         '被拒的請求不得換掉密碼')

    @patch(_OPERATOR_CLIENT)
    def test_non_member_still_gets_not_found(self, get_client):
        get_client.return_value = MagicMock()
        for name, call in (('rollback', self._rollback), ('reset', self._reset),
                           ('admin-credentials', self._set_credentials)):
            with self.subTest(endpoint=name):
                result = call(self.outsider)
                self.assertFalse(result.get('success'))
                self.assertEqual(result.get('error'), NOT_FOUND)
        get_client.assert_not_called()

    def test_admin_passes_the_role_gate(self):
        """對照組：admin 走得過 role gate（rollback 進到 _rollback_service）。"""
        with patch.object(type(self.controller), '_rollback_service',
                          return_value={'success': True}) as rollback:
            result = self._rollback(self.admin)
        self.assertEqual(result, {'success': True})
        rollback.assert_called_once()


@tagged('post_install', '-at_install')
class TestServicePageRoleContract1560(TransactionCase):
    """N5／N10／回復版本中文化的前端契約（靜態讀檔）。"""

    MANAGER_HANDLERS = (
        'showSkipTrial', 'showResetConfirm', 'showAdminCredentials', 'showChangePlan',
        'showDomainModal', 'showRestart', 'showRollback', 'showDelete',
    )

    def setUp(self):
        super().setUp()
        self.page_js = _read('pages', 'service', 'ServiceDetailPage.js')
        self.page_xml = _read('pages', 'service', 'ServiceDetailPage.xml')

    def _manager_block(self):
        start = self.page_xml.find('<t t-if="canManageService">')
        self.assertGreaterEqual(start, 0, '找不到 canManageService 的管理按鈕區塊')
        end = self.page_xml.find('</t>', self.page_xml.find('onClick.bind="showDelete"'))
        self.assertGreater(end, start)
        return self.page_xml[start:end]

    def test_manager_buttons_are_inside_the_gate(self):
        block = self._manager_block()
        for handler in self.MANAGER_HANDLERS:
            with self.subTest(handler=handler):
                self.assertIn('onClick.bind="%s"' % handler, block)
        self.assertNotIn('onClick.bind="openWebUI"', block, '開啟網頁介面是所有成員都能用的')

    def test_gate_reads_the_payload_field_fail_closed(self):
        match = re.search(r'get canManageService\(\)\s*\{(.*?)\n    \}', self.page_js, re.S)
        self.assertTrue(match, '找不到 canManageService getter')
        self.assertIn('viewer_can_manage === true', match.group(1))

    def test_reset_button_opens_a_confirmation_first(self):
        self.assertNotIn('onClick.bind="resetAdminPassword"', self.page_xml,
                         'N10：Reset 不得一按就直接重設')
        self.assertIn('<ResetAdminPasswordModal', self.page_xml)
        modal_xml = _read('components', 'modal', 'ResetAdminPasswordModal.xml')
        self.assertIn('onClick.bind="onReset"', modal_xml)
        self.assertIn('onClick="props.onClose"', modal_xml)

    def test_tabs_do_not_offer_editing_to_viewers(self):
        for parts in (('pages', 'service', 'tabs', 'ConfigurationTab.js'),
                      ('pages', 'service', 'tabs', 'AddonsTab.js')):
            with self.subTest(file=parts[-1]):
                source = _read(*parts)
                start = source.find('get canEdit() {')
                self.assertGreaterEqual(start, 0, '找不到 canEdit')
                head = source[start:start + 400]
                self.assertIn('this.props.canManage !== true', head,
                              'role 判斷要排在 canEdit 最前面')
        for tab in ('ConfigurationTab', 'LogsTab', 'AddonsTab'):
            with self.subTest(tab=tab):
                match = re.search(r'<%s\b(?:(?!/>).)*?/>' % tab, self.page_xml, re.S)
                self.assertIsNotNone(match, '找不到 %s 元件' % tab)
                self.assertIn('canManage="canManageService"', match.group(0))

        logs_js = _read('pages', 'service', 'tabs', 'LogsTab.js')
        self.assertIn('onMounted(() => { if (this.props.canManage) this.poller.start(); });',
                      logs_js, 'viewer 不得啟動容器日誌輪詢')

    def test_rollback_dialog_translates_helm_status(self):
        js = _read('components', 'modal', 'RollbackModal.js')
        xml = _read('components', 'modal', 'RollbackModal.xml')
        self.assertIn('t-esc="statusLabel(revision.status)"', xml)
        self.assertNotIn('t-esc="revision.status"', xml)
        self.assertIn('revisionDescription(revision.description)', xml)
        for status in ('deployed', 'superseded', 'failed'):
            self.assertRegex(js, r'\b%s: _t\(' % status)

    def test_new_strings_are_translated(self):
        po = os.path.join(_SRC, 'i18n', 'zh_TW.po')
        code = {e['src']: e['value'] for e in PoFileReader(po) if e.get('type') == 'code'}
        for src in (
            'Deployed', 'Superseded', 'Update failed', 'In progress', 'Unknown',
            'Installed', 'Updated', 'Rolled back to revision %s',
            'Reset the admin password of this service?',
            'A new password is generated and shown only once. The current password '
            'stops working right away.',
            'The service does not restart. Devices that are already signed in stay '
            'signed in.',
            'The service does not restart. Devices signed in with the current password '
            'are signed out, and two-factor sign-in is turned off.',
            'The service restarts to apply the new password (about a minute); work in '
            'progress is interrupted.',
            'The Gateway API Key is replaced too; update it wherever you configured it '
            '(e.g. your MCP server).',
            'Resetting...', 'Reset Admin Password', 'Permission denied',
        ):
            with self.subTest(src=src):
                self.assertTrue(code.get(src), 'zh_TW.po 缺中文：%r' % src)
