"""#1433：「回復版本」不得把平台管的管理員帳密一起回復；#1434：ns 的 ephemeral 額度。

#1433 R8 重現（open-design、code-server）：純 ``helm rollback`` 把目標 revision 的
``config.sensitive.admin_password`` 一起回復 ⇒ 已撤銷的舊密碼復活、目前的密碼失效，
平台顯示的帳號也與實際不一致。修法：有平台憑證的模板請 operator 改走「保留這些路徑」
的回復（operator 端行為見 extra/paas-operator/tests/test_rollback_preserve_1433.py）。

本檔守平台這一半：

1. 送給 operator 的保留路徑與**供給鏈同源**（has_admin_gui、gateway、帳號路徑），
   不是 slug 名單；沒有平台憑證的模板照舊走純 rollback（不帶新欄位）。
2. operator 回報的 chart 版本落到 ``helm_chart_version``，下一次修改配置不會把剛回復
   的 chart 又升回去。
3. （#1434）建 ns／重算 quota 時一併送 ephemeral-storage 額度。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision
from .ns_quota_fixture import answer_quota_reads

_CONTROLLER_CLIENT = (
    'odoo.addons.woow_paas_platform.controllers.paas.get_paas_operator_client'
)
USERNAME_PATH = 'authProxy.basicAuth.username'


@tagged('post_install', '-at_install')
class TestRollbackPreservesCredentials1433(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.admin_user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Rollback creds workspace',
            'owner_id': self.admin_user.id,
        })

    def _template(self, slug, **vals):
        base = {
            'name': 'Rollback %s' % slug,
            'slug': slug,
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.3.0',
            'mcp_enabled': False,
            'helm_default_values': '{}',
        }
        base.update(vals)
        return self.env['woow_paas_platform.cloud_app_template'].create(base)

    def _service(self, template, release):
        return _internal_provision(
            self.env['woow_paas_platform.cloud_service'].with_context(
                mcp_admin_password='current-password-0000',
            )).create({
                'name': 'Svc %s' % release,
                'workspace_id': self.workspace.id,
                'template_id': template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-rb1433',
                'helm_release_name': release,
                'helm_chart_version': '0.3.0',
                'helm_values': json.dumps({}),
            })

    def _rollback(self, service, result=None):
        client = MagicMock()
        client.rollback_release.return_value = result if result is not None else {
            'message': 'ok'}
        with patch(_CONTROLLER_CLIENT, return_value=client), \
                patch('odoo.addons.woow_paas_platform.controllers.paas.request',
                      new=MagicMock(env=self.env(user=self.admin_user))):
            response = self.controller._rollback_service(service, revision=3)
        return response, client

    # ---- 保留路徑的來源 ---------------------------------------------------

    def test_credential_template_preserves_password_and_username(self):
        template = self._template(
            'rb-od', has_admin_gui=True, admin_password_carrier='helm_secret',
            admin_username_helm_path=USERNAME_PATH)
        service = self._service(template, 'svc-rb-od')

        response, client = self._rollback(service, result={
            'message': 'ok', 'mode': 'upgrade_preserving', 'chart_version': '0.2.0'})

        self.assertTrue(response.get('success'), response)
        kwargs = client.rollback_release.call_args.kwargs
        self.assertEqual(kwargs['chart'], 'oci://example/rb-od')
        self.assertEqual(kwargs['revision'], 3)
        self.assertIn('config.sensitive', kwargs['preserve_value_paths'])
        self.assertIn(USERNAME_PATH, kwargs['preserve_value_paths'])
        self.assertEqual(service.state, 'upgrading')

    def test_password_only_template_preserves_sensitive_channel(self):
        """code-server 形：只有密碼、沒有帳號路徑。"""
        template = self._template(
            'rb-cs', has_admin_gui=True, admin_password_carrier='helm_secret')
        service = self._service(template, 'svc-rb-cs')

        _response, client = self._rollback(service)

        self.assertEqual(
            client.rollback_release.call_args.kwargs['preserve_value_paths'],
            ['config.sensitive'])

    def test_template_without_platform_credentials_is_plain_rollback(self):
        template = self._template('rb-plain', has_admin_gui=False)
        service = self._service(template, 'svc-rb-plain')

        response, client = self._rollback(service)

        self.assertTrue(response.get('success'), response)
        kwargs = client.rollback_release.call_args.kwargs
        self.assertIsNone(kwargs['preserve_value_paths'])
        self.assertIsNone(kwargs['chart'])
        self.assertEqual(service._rollback_preserved_value_paths(), [])

    def test_gateway_key_is_preserved_when_chart_has_gateway(self):
        template = self._template(
            'rb-gw', has_admin_gui=True, admin_password_carrier='helm_secret')
        service = self._service(template, 'svc-rb-gw')
        with patch.object(type(service), '_chart_has_gateway', return_value=True):
            paths = service._rollback_preserved_value_paths()
        self.assertEqual(paths, ['config.sensitive', 'auth.apiServerKey'])

    # ---- 帳本 -----------------------------------------------------------

    def test_reported_chart_version_is_recorded(self):
        """保留憑證的回復是一次 upgrade：記下目標版本，下次修改配置不會再升回去。"""
        template = self._template(
            'rb-ver', has_admin_gui=True, admin_password_carrier='helm_secret')
        service = self._service(template, 'svc-rb-ver')

        self._rollback(service, result={
            'message': 'ok', 'mode': 'upgrade_preserving', 'chart_version': '0.2.0'})

        self.assertEqual(service.helm_chart_version, '0.2.0')

    def test_plain_rollback_leaves_chart_version_alone(self):
        template = self._template('rb-ver-plain', has_admin_gui=False)
        service = self._service(template, 'svc-rb-ver-plain')

        self._rollback(service, result={'message': 'Rollback was a success!'})

        self.assertEqual(service.helm_chart_version, '0.3.0')


@tagged('post_install', '-at_install')
class TestNamespaceEphemeralQuota1434(TransactionCase):

    def setUp(self):
        super().setUp()
        admin = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Ephemeral quota workspace', 'owner_id': admin.id,
        })
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Eph tpl', 'slug': 'eph-tpl-1434', 'category': 'ai_llm',
            'helm_repo_url': '', 'helm_chart_name': 'oci://example/eph',
            'helm_chart_version': '0.1.0', 'mcp_enabled': False,
            'helm_default_values': '{}',
        })
        self.Svc = self.env['woow_paas_platform.cloud_service']

    def _svc(self, release, state='running'):
        return _internal_provision(self.Svc).create({
            'name': release, 'workspace_id': self.workspace.id,
            'template_id': self.template.id, 'state': state,
            'helm_namespace': 'paas-ws-eph1434', 'helm_release_name': release,
        })

    def test_floor_for_small_workspace(self):
        self._svc('svc-eph-a')
        self.assertEqual(self.Svc._compute_ns_ephemeral_quota(self.workspace),
                         ('4Gi', '80Gi'))

    def test_scales_with_live_services_and_ignores_deleting(self):
        for i in range(3):
            self._svc('svc-eph-%d' % i)
        self._svc('svc-eph-gone', state='deleting')
        self.assertEqual(self.Svc._compute_ns_ephemeral_quota(self.workspace),
                         ('6Gi', '120Gi'))

    def test_push_ns_quota_sends_ephemeral(self):
        svc = self._svc('svc-eph-push')
        client = MagicMock()
        answer_quota_reads(client)
        with patch('odoo.addons.woow_paas_platform.services.paas_operator'
                   '.get_paas_operator_client', return_value=client):
            svc._push_ns_quota(self.workspace, 'paas-ws-eph1434')
        kwargs = client.update_namespace_quota.call_args.kwargs
        self.assertEqual(kwargs['ephemeral_storage_request'], '4Gi')
        self.assertEqual(kwargs['ephemeral_storage_limit'], '80Gi')
