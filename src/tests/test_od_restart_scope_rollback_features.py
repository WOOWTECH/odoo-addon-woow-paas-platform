"""AI-OD-004-2 改金鑰只重啟 daemon、AI-OD-006-1 回復版本不關掉內建 MCP（商用驗收 open-design 244）。

- AI-OD-004-2：#1435 改金鑰後的補重啟對整個 release 下 rollout restart ⇒ 不吃金鑰的
  console Deployment 也被重建、進行中的 console 終端機（claude login 等）斷線。範本新增
  ``secret_change_restart_component``，平台只請 operator 重啟那個 component；operator 回
  404（沒有任何 workload 帶這個 label）時退回全部重啟。
- AI-OD-006-1：回復到 MCP 上線前的 revision，``mcp.enabled`` 跟著回到舊值、MCP 容器消失。
  範本新增 ``rollback_feature_switches``（開關 → 守衛），平台把它送給 operator
  （``preserve_when_equal``）：守衛（image）相同時開關維持目前值（operator 端行為見
  extra/paas-operator/tests/test_restart_scope_rollback_features_od.py）。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError
from ..models.cloud_service import _internal_provision
from .test_restart_and_secret_keys_1432 import _BG_CLIENT, _CONTROLLER, _Fixture, _SyncThread

FEATURES = {
    'mcp.enabled': ['image'],
    'omnigentHost.enabled': ['image'],
    'paseo.enabled': ['image'],
}


@tagged('post_install', '-at_install')
class TestSecretRestartComponentOD0042(_Fixture):

    def _scoped_template(self, slug, component='daemon'):
        template = self._template(slug, 'container')
        template.secret_change_restart_component = component
        return template

    def test_key_change_restarts_only_the_declared_component(self):
        service = self._service(template=self._scoped_template('od0042-scoped'))
        result, client = self._update(service, values={'auth.anthropicApiKey': 'sk-ant-new'})

        self.assertTrue(result.get('success'), result)
        client.restart_release.assert_called_once_with(
            namespace='paas-ws-r8life', release_name='svc-r8-life', component='daemon')
        names = [c[0] for c in client.method_calls
                 if c[0] in ('upgrade_release', 'restart_release')]
        self.assertEqual(names, ['upgrade_release', 'restart_release'])

    def test_no_matching_workload_falls_back_to_full_restart(self):
        """舊 chart 沒有 component label：operator 404 ⇒ 改全部重啟，新金鑰一定生效。"""
        service = self._service(template=self._scoped_template('od0042-fallback'))
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 3, 'chart_version': '0.1.0'}

        def _restart(namespace, release_name, component=None):
            if component:
                raise PaaSOperatorError('no daemon workload', status_code=404)
            return {'action': 'restart'}

        client.restart_release.side_effect = _restart
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                self._request_as(self.admin_user), \
                patch(_BG_CLIENT, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = self.controller._update_service(
                service, values={'auth.anthropicApiKey': 'sk-ant-new'}, version=None)

        self.assertTrue(result.get('success'), result)
        self.assertEqual(
            [c.kwargs for c in client.restart_release.call_args_list],
            [{'namespace': 'paas-ws-r8life', 'release_name': 'svc-r8-life',
              'component': 'daemon'},
             {'namespace': 'paas-ws-r8life', 'release_name': 'svc-r8-life'}])
        self.assertNotEqual(service.state, 'error')

    def test_other_operator_errors_do_not_fall_back(self):
        """500 等錯誤不是「沒有這個 component」：不改成全部重啟，照 #1435 記 log、不翻 error。"""
        service = self._service(template=self._scoped_template('od0042-500'))
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 3, 'chart_version': '0.1.0'}
        client.restart_release.side_effect = PaaSOperatorError('boom', status_code=500)
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                self._request_as(self.admin_user), \
                patch(_BG_CLIENT, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = self.controller._update_service(
                service, values={'auth.anthropicApiKey': 'sk-ant-new'}, version=None)
        self.assertTrue(result.get('success'), result)
        client.restart_release.assert_called_once()
        self.assertNotEqual(service.state, 'error')

    def test_non_secret_change_still_does_not_restart(self):
        service = self._service(template=self._scoped_template('od0042-nosecret'))
        result, client = self._update(service, values={'git.authorName': 'Bob'})
        self.assertTrue(result.get('success'), result)
        client.restart_release.assert_not_called()

    def test_component_must_be_a_label_value(self):
        for i, bad in enumerate(('Daemon', 'daemon,console', 'a=b', '-x', 'a b')):
            template = self._template('od0042-bad-%d' % i, 'container')
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                template.write({'secret_change_restart_component': bad})

    def test_open_design_seed_scopes_to_daemon(self):
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        self.assertEqual(template.secret_change_restart_component, 'daemon')


@tagged('post_install', '-at_install')
class TestRollbackFeatureSwitchesOD0061(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.admin_user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'OD-006 rollback workspace',
            'owner_id': self.admin_user.id,
        })

    def _template(self, slug, **vals):
        base = {
            'name': 'OD-006 %s' % slug,
            'slug': slug,
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.5.0',
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
                'helm_namespace': 'paas-ws-od006',
                'helm_release_name': release,
                'helm_chart_version': '0.5.0',
                'helm_values': json.dumps({}),
            })

    def _rollback(self, service):
        client = MagicMock()
        client.get_revisions.return_value = {'revisions': []}
        client.rollback_release.return_value = {
            'message': 'ok', 'mode': 'upgrade_preserving', 'chart_version': '0.5.0',
            'revision': 14, 'feature_paths_preserved': ['mcp.enabled'],
            'feature_paths_reverted': []}
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request',
                      new=MagicMock(env=self.env(user=self.admin_user))):
            response = self.controller._rollback_service(service, revision=6)
        return response, client

    def test_declared_switches_are_sent_with_credentials(self):
        template = self._template(
            'od006-creds', has_admin_gui=True, admin_password_carrier='helm_secret',
            rollback_feature_switches=json.dumps(FEATURES))
        service = self._service(template, 'svc-od006-creds')

        response, client = self._rollback(service)

        self.assertTrue(response.get('success'), response)
        kwargs = client.rollback_release.call_args.kwargs
        self.assertEqual(kwargs['preserve_when_equal'], FEATURES)
        self.assertIn('config.sensitive', kwargs['preserve_value_paths'])
        self.assertEqual(kwargs['chart'], 'oci://example/od006-creds')
        # 開關不是憑證：不進公開設定同步的排除清單
        self.assertNotIn('mcp.enabled', service._rollback_preserved_value_paths())

    def test_switches_alone_select_the_preserving_rollback(self):
        """沒有平台憑證的範本也要帶 chart（純 helm rollback 保留不了開關）。"""
        template = self._template(
            'od006-plain', has_admin_gui=False,
            rollback_feature_switches=json.dumps({'mcp.enabled': ['image']}))
        service = self._service(template, 'svc-od006-plain')

        response, client = self._rollback(service)

        self.assertTrue(response.get('success'), response)
        kwargs = client.rollback_release.call_args.kwargs
        self.assertIsNone(kwargs['preserve_value_paths'])
        self.assertEqual(kwargs['preserve_when_equal'], {'mcp.enabled': ['image']})
        self.assertEqual(kwargs['chart'], 'oci://example/od006-plain')

    def test_without_switches_request_is_unchanged(self):
        template = self._template('od006-none', has_admin_gui=False)
        service = self._service(template, 'svc-od006-none')

        _response, client = self._rollback(service)

        kwargs = client.rollback_release.call_args.kwargs
        self.assertIsNone(kwargs['preserve_when_equal'])
        self.assertIsNone(kwargs['chart'])

    def test_malformed_switches_are_rejected_on_write(self):
        bad_values = (
            'not json',
            '["mcp.enabled"]',
            '{"mcp..enabled": ["image"]}',
            '{"mcp.enabled": "image"}',
            '{"mcp.enabled": ["im age"]}',
            json.dumps({'f%d.enabled' % i: [] for i in range(17)}),
        )
        for i, bad in enumerate(bad_values):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self._template('od006-bad-%d' % i, rollback_feature_switches=bad)

    def test_open_design_seed_declares_mcp_and_omnigent_switches(self):
        template = self.env.ref('woow_paas_platform.cloud_app_opendesign')
        self.assertEqual(template._parse_rollback_feature_switches(), FEATURES)
        # 守衛路徑必須真的出現在範本預設值裡，否則永遠「相同」（兩邊都不存在）。
        defaults = json.loads(template.helm_default_values)
        self.assertIn('image', defaults)
        self.assertIn('enabled', defaults['mcp'])
        self.assertIn('enabled', defaults['omnigentHost'])
        self.assertIn('enabled', defaults['paseo'])
