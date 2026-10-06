"""#1370／#1186：Odoo／n8n 管理員密碼重設（平台端）。

這兩種 app 的密碼權威在 app 自己的 DB，平台經 operator exec 腳本重設，**不** helm
upgrade、不重啟。本檔鎖：

- 能力欄位 ``app_admin_reset_kind`` 是唯一判準（admin_reset_supported、controller
  分派、payload 鏡射都讀它），與 has_admin_gui 互斥，seed 的 odoo／n8n 有開。
- model 方法 private（#357 C05）、只有 workspace admin 打得到。
- 結果分類：成功才落庫（Odoo→cloud_service_secret、n8n→n8n_owner_*）；
  failed／unknown／逾時都不落庫、回租戶看得懂的訊息。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, ValidationError
from odoo.service.model import get_public_method
from odoo.tests.common import TransactionCase

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import ROLE_ADMIN, ROLE_USER
from odoo.addons.woow_paas_platform.services.paas_operator import (
    PaaSOperatorAPIError,
    PaaSOperatorTimeoutError,
)
from ..models.cloud_service import _internal_provision

CLIENT_PATH = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'


class _AppAdminResetFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'App admin reset 1370',
        })
        Template = self.env['woow_paas_platform.cloud_app_template']
        common = {'category': 'web', 'helm_repo_url': '', 'helm_chart_version': '1.0.0'}
        self.odoo_template = Template.create(dict(
            common, name='Odoo 1370', slug='odoo-1370',
            # 刻意不用 woow-odoo chart 名：本檔測的是 kind 分派，不是 woow-odoo 的
            # 存量服務守門（那條由 _woow_odoo_legacy_release_error 自己的測試守）。
            helm_chart_name='oci://example/odoo-1370',
            app_admin_reset_kind='odoo_exec',
        ))
        self.n8n_template = Template.create(dict(
            common, name='n8n 1370', slug='n8n-1370',
            helm_chart_name='oci://example/n8n-1370',
            app_admin_reset_kind='n8n_exec',
        ))
        Service = _internal_provision(self.env['woow_paas_platform.cloud_service'])
        self.odoo_service = Service.create({
            'name': 'Odoo 1370', 'workspace_id': self.workspace.id,
            'template_id': self.odoo_template.id, 'state': 'running',
            'helm_namespace': 'paas-ws-test-1370', 'helm_release_name': 'odoo-1370',
        })
        self.n8n_service = Service.create({
            'name': 'n8n 1370', 'workspace_id': self.workspace.id,
            'template_id': self.n8n_template.id, 'state': 'running',
            'helm_namespace': 'paas-ws-test-1370', 'helm_release_name': 'n8n-1370',
        })
        self.n8n_service.sudo().write({
            'n8n_owner_email': 'old-owner@example.com',
            'n8n_owner_password': 'old-owner-password',
        })
        self.admin = self._make_member('app-reset-1370-admin', ROLE_ADMIN)

    def _make_member(self, login, role):
        user = self.env['res.users'].create({
            'name': login, 'login': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': role,
        })
        return user

    def _as(self, user):
        return patch(
            'odoo.addons.woow_paas_platform.controllers.paas.request',
            new=MagicMock(env=self.env(user=user)),
        )

    def _reset(self, service, user=None):
        with self._as(user or self.admin):
            return self.controller.api_service_reset_admin_password(self.workspace.id, service.id)

    def _secret(self, service):
        return self.env['woow_paas_platform.cloud_service_secret'].sudo().search(
            [('service_id', '=', service.id)], limit=1)


class TestAppAdminResetCapability1370(_AppAdminResetFixture):

    def test_kind_enables_reset_but_not_tenant_set(self):
        for template in (self.odoo_template, self.n8n_template):
            self.assertTrue(template.admin_reset_supported, template.name)
            self.assertFalse(template.admin_credentials_settable, template.name)
            self.assertFalse(template.has_admin_gui, template.name)

    def test_kind_none_keeps_previous_capability(self):
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Plain 1370', 'slug': 'plain-1370', 'category': 'web',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example/plain',
            'helm_chart_version': '1.0.0',
        })
        self.assertEqual(template.app_admin_reset_kind, 'none')
        self.assertFalse(template.admin_reset_supported)

    def test_kind_and_admin_gui_are_mutually_exclusive(self):
        with self.assertRaises(ValidationError):
            self.odoo_template.write({'has_admin_gui': True})

    def test_seed_enables_odoo_and_n8n(self):
        odoo = self.env.ref('woow_paas_platform.cloud_app_odoo')
        n8n = self.env.ref('woow_paas_platform.cloud_app_n8n')
        self.assertEqual(odoo.app_admin_reset_kind, 'odoo_exec')
        self.assertEqual(n8n.app_admin_reset_kind, 'n8n_exec')
        self.assertTrue(odoo.admin_reset_supported and n8n.admin_reset_supported)
        # omnigent（app_managed）：#1370 後續改走 omnigent_exec。
        omnigent = self.env.ref('woow_paas_platform.cloud_app_omnigent', raise_if_not_found=False)
        if omnigent:
            self.assertEqual(omnigent.app_admin_reset_kind, 'omnigent_exec')
            self.assertTrue(omnigent.admin_reset_supported)

    def test_payload_mirrors_kind(self):
        with self._as(self.admin):
            payload = self.controller._format_service(self.n8n_service, include_details=True)
        self.assertEqual(payload['template']['app_admin_reset_kind'], 'n8n_exec')
        self.assertTrue(payload['template']['admin_reset_supported'])
        self.assertNotIn('old-owner-password', json.dumps(payload))

    def test_model_method_is_not_remotely_callable(self):
        with self.assertRaises(AccessError):
            get_public_method(self.env['woow_paas_platform.cloud_service'], '_reset_app_admin_password')


class TestAppAdminResetFlow1370(_AppAdminResetFixture):

    @patch(CLIENT_PATH)
    def test_odoo_reset_stores_and_returns_the_new_password(self, get_client):
        client = get_client.return_value
        client.reset_odoo_admin_password.return_value = {
            'success': True, 'login': 'boss@example.com', 'second_factor_cleared': True}

        result = self._reset(self.odoo_service)

        self.assertTrue(result['success'], result)
        data = result['data']
        kwargs = client.reset_odoo_admin_password.call_args.kwargs
        self.assertEqual(kwargs['namespace'], 'paas-ws-test-1370')
        self.assertEqual(kwargs['release_name'], 'odoo-1370')
        self.assertEqual(data['admin_password'], kwargs['new_password'])
        self.assertEqual(self._secret(self.odoo_service).admin_password, kwargs['new_password'])
        self.assertEqual(data['admin_username'], 'boss@example.com')
        self.assertEqual(data['template']['app_admin_reset_kind'], 'odoo_exec')
        self.assertIn('two-factor', data['message'])
        # 不走 helm：既有 reset-config 路徑完全沒被碰，state 不變成 upgrading。
        client.reset_release_config.assert_not_called()
        client.reset_n8n_owner_password.assert_not_called()
        self.assertEqual(self.odoo_service.state, 'running')

    @patch(CLIENT_PATH)
    def test_n8n_reset_resyncs_owner_credentials_for_post_deploy_init(self, get_client):
        client = get_client.return_value
        client.reset_n8n_owner_password.return_value = {
            'success': True, 'login': 'renamed-owner@example.com', 'second_factor_cleared': False}

        result = self._reset(self.n8n_service)

        self.assertTrue(result['success'], result)
        new_password = client.reset_n8n_owner_password.call_args.kwargs['new_password']
        service = self.n8n_service.sudo()
        self.assertEqual(service.n8n_owner_password, new_password)
        # 租戶在 n8n 介面改過 email 的漂移也一併對齊（init 用它 POST /rest/login）。
        self.assertEqual(service.n8n_owner_email, 'renamed-owner@example.com')
        self.assertEqual(result['data']['admin_password'], new_password)
        self.assertEqual(result['data']['admin_username'], 'renamed-owner@example.com')
        self.assertNotEqual(new_password, 'old-owner-password')

    @patch(CLIENT_PATH)
    def test_script_refusal_is_actionable_and_stores_nothing(self, get_client):
        get_client.return_value.reset_odoo_admin_password.return_value = {
            'success': False, 'error_kind': 'failed', 'error_code': 'admin_inactive',
            'error_step': 'reset', 'error': 'archived'}

        result = self._reset(self.odoo_service)

        self.assertFalse(result['success'])
        self.assertIn('archived', result['error'])
        self.assertIn('not changed', result['error'])
        # #1186：告訴租戶能自己做的事（其他管理員重新啟用），否則開工單。
        self.assertIn('reactivate', result['error'])
        self.assertIn('support ticket', result['error'])
        self.assertFalse(self._secret(self.odoo_service).admin_password)
        self.assertNotIn('admin_password', json.dumps(result))

    @patch(CLIENT_PATH)
    def test_unknown_outcomes_never_claim_unchanged(self, get_client):
        client = get_client.return_value
        outcomes = [
            {'return_value': {'success': False, 'error_kind': 'unknown', 'error_step': 'exec'}},
            {'side_effect': PaaSOperatorTimeoutError('timeout')},
            {'side_effect': PaaSOperatorAPIError('boom', status_code=500, detail='x')},
        ]
        for outcome in outcomes:
            client.reset_n8n_owner_password.reset_mock(return_value=True, side_effect=True)
            client.reset_n8n_owner_password.configure_mock(**outcome)

            result = self._reset(self.n8n_service)

            self.assertFalse(result['success'], outcome)
            self.assertIn('unknown', result['error'], outcome)
            self.assertNotIn('not changed', result['error'], outcome)
            self.assertEqual(self.n8n_service.sudo().n8n_owner_password, 'old-owner-password')

    @patch(CLIENT_PATH)
    def test_old_operator_without_endpoint_says_not_available(self, get_client):
        get_client.return_value.reset_odoo_admin_password.side_effect = PaaSOperatorAPIError(
            'Not Found', status_code=404, detail='Not Found')

        result = self._reset(self.odoo_service)

        self.assertFalse(result['success'])
        self.assertIn('not available yet', result['error'])

    @patch(CLIENT_PATH)
    def test_requires_running_service(self, get_client):
        self.odoo_service.write({'state': 'suspended'})

        result = self._reset(self.odoo_service)

        self.assertFalse(result['success'])
        get_client.return_value.reset_odoo_admin_password.assert_not_called()

    @patch(CLIENT_PATH)
    def test_non_admin_member_is_denied(self, get_client):
        member = self._make_member('app-reset-1370-user', ROLE_USER)

        result = self._reset(self.n8n_service, user=member)

        self.assertFalse(result['success'])
        get_client.return_value.reset_n8n_owner_password.assert_not_called()
        self.assertEqual(self.n8n_service.sudo().n8n_owner_password, 'old-owner-password')

    @patch(CLIENT_PATH)
    def test_tenant_cannot_set_own_credentials(self, get_client):
        with self._as(self.admin):
            result = self.controller.api_service_set_admin_credentials(
                self.workspace.id, self.odoo_service.id, password='Tenant-Chosen-Pass-1')

        self.assertFalse(result['success'])
        self.assertIn('only be reset', result['error'])
        get_client.return_value.reset_odoo_admin_password.assert_not_called()


class TestOmnigentAdminReset1370(_AppAdminResetFixture):
    """#1370 後續：Omnigent 的 Reset 走 operator exec（omnigent_exec）。

    omnigent 的初始密碼照舊由 has_admin_gui 供給（app_managed），所以它是唯一允許
    app_admin_reset_kind 與 has_admin_gui 並存的組合；helm 那條 reset 仍必須擋。
    """

    def setUp(self):
        super().setUp()
        Template = self.env['woow_paas_platform.cloud_app_template']
        self.omni_template = Template.create({
            'name': 'Omnigent 1370', 'slug': 'omnigent-1370', 'category': 'web',
            'helm_repo_url': '', 'helm_chart_version': '1.0.0',
            'helm_chart_name': 'oci://example/omnigent-1370',
            'has_admin_gui': True, 'admin_password_carrier': 'app_managed',
            'app_admin_reset_kind': 'omnigent_exec',
        })
        Service = _internal_provision(self.env['woow_paas_platform.cloud_service'])
        self.omni_service = Service.create({
            'name': 'Omnigent 1370', 'workspace_id': self.workspace.id,
            'template_id': self.omni_template.id, 'state': 'running',
            'helm_namespace': 'paas-ws-test-1370', 'helm_release_name': 'omni-1370',
        })

    def test_capability(self):
        self.assertTrue(self.omni_template.admin_reset_supported)
        self.assertFalse(self.omni_template.admin_credentials_settable)

    def test_exception_is_limited_to_app_managed(self):
        for vals in ({'admin_password_carrier': 'helm_secret'},
                     {'admin_password_carrier': 'config_json'}):
            with self.assertRaises(ValidationError, msg=repr(vals)):
                with self.env.cr.savepoint():
                    self.omni_template.write(vals)
        # 其他 kind 仍與 has_admin_gui 互斥，即使是 app_managed。
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                self.omni_template.write({'app_admin_reset_kind': 'n8n_exec'})

    def test_helm_reset_path_still_refused(self):
        """model 第二道防線：app_managed 走 helm reset＝假裝成功，照擋。"""
        from odoo.exceptions import UserError
        with self.assertRaisesRegex(UserError, 'manages its own admin password'):
            self.omni_service._reset_admin_password()

    @patch(CLIENT_PATH)
    def test_reset_calls_omnigent_endpoint_and_stores_password(self, get_client):
        client = get_client.return_value
        client.reset_omnigent_admin_password.return_value = {
            'success': True, 'login': 'admin', 'second_factor_cleared': False}

        result = self._reset(self.omni_service)

        self.assertTrue(result['success'], result)
        data = result['data']
        kwargs = client.reset_omnigent_admin_password.call_args.kwargs
        self.assertEqual(kwargs['release_name'], 'omni-1370')
        self.assertEqual(data['admin_password'], kwargs['new_password'])
        self.assertEqual(self._secret(self.omni_service).admin_password, kwargs['new_password'])
        self.assertEqual(data['admin_username'], 'admin')
        self.assertEqual(data['template']['app_admin_reset_kind'], 'omnigent_exec')
        # 與上游 Members → Reset 一致：不登出既有 session、沒有 2FA——文案不能說會登出。
        self.assertIn('stay signed in', data['message'])
        self.assertNotIn('two-factor', data['message'])
        client.reset_release_config.assert_not_called()
        client.reset_odoo_admin_password.assert_not_called()
        client.reset_n8n_owner_password.assert_not_called()
        self.assertEqual(self.omni_service.state, 'running')

    @patch(CLIENT_PATH)
    def test_refusal_stores_nothing(self, get_client):
        get_client.return_value.reset_omnigent_admin_password.return_value = {
            'success': False, 'error_kind': 'failed', 'error_code': 'admin_missing'}
        before = self._secret(self.omni_service).admin_password

        result = self._reset(self.omni_service)

        self.assertFalse(result['success'])
        self.assertIn('could not be found', result['error'])
        self.assertEqual(self._secret(self.omni_service).admin_password, before)

    @patch(CLIENT_PATH)
    def test_admin_inactive_message_is_omnigent_specific(self, get_client):
        """Omnigent 的 admin_inactive＝被拿掉管理員角色，不能沿用 Odoo「封存／去 Odoo 啟用」文案。"""
        get_client.return_value.reset_omnigent_admin_password.return_value = {
            'success': False, 'error_kind': 'failed', 'error_code': 'admin_inactive'}
        before = self._secret(self.omni_service).admin_password

        result = self._reset(self.omni_service)

        self.assertFalse(result['success'])
        self.assertIn('no longer has administrator rights', result['error'])
        self.assertIn('Omnigent', result['error'])
        self.assertNotIn('archived', result['error'])
        self.assertNotIn('in Odoo', result['error'])
        self.assertEqual(self._secret(self.omni_service).admin_password, before)
