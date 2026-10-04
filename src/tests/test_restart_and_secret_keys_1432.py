"""#1432 租戶「重新啟動」、#1435 改金鑰後重啟、#1455 金鑰「未設定」與清除。

R8 商用驗收：
- #1432：所有範本都沒有租戶可用的重新啟動入口（`action=restart` 回「未知的 action」）。
- #1435：「修改配置」改 OpenRouter／Anthropic 金鑰後 Secret 已是新值，但以
  secretKeyRef env 注入的主程式沒重啟，新金鑰不生效（chart 沒有 checksum/secret）。
- #1455：從沒設定過的金鑰也顯示成 ``********``；已設定的金鑰沒有辦法清除。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import ROLE_USER
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_MODEL_CLIENT = (
    'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
)
_BG_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.PaaSOperatorClient'

SPECS = {
    'required': [
        {'key': 'auth.openrouterApiKey', 'label': 'OpenRouter API Key', 'type': 'password'},
    ],
    'optional': [
        {'key': 'auth.anthropicApiKey', 'label': 'Anthropic API Key', 'type': 'password'},
        {'key': 'git.token', 'label': 'Git Token', 'type': 'password'},
        {'key': 'git.authorName', 'label': 'Git Author', 'type': 'text'},
    ],
}


class _SyncThread:
    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _Fixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.admin_user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'R8 lifecycle workspace',
            'owner_id': self.admin_user.id,
        })
        self.template = self._template('r8-life', 'container')

    def _template(self, slug, workload_type):
        return self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'R8 %s' % slug,
            'slug': slug,
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.1.0',
            'workload_type': workload_type,
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
            'helm_default_values': '{}',
            'helm_value_specs': json.dumps(SPECS),
        })

    def _service(self, values=None, state='running', template=None):
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-r8-life',
            'workspace_id': self.workspace.id,
            'template_id': (template or self.template).id,
            'state': state,
            'helm_namespace': 'paas-ws-r8life',
            'helm_release_name': 'svc-r8-life',
            'helm_chart_version': '0.1.0',
            'helm_values': json.dumps(values if values is not None else {
                'auth': {'openrouterApiKey': 'sk-or-current'},
                'git': {'authorName': 'Alice', 'token': 'ghp-current'},
            }),
        })

    def _request_as(self, user):
        return patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=user)))

    def _update(self, service, values, clear_keys=None):
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 3, 'chart_version': '0.1.0'}
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                self._request_as(self.admin_user), \
                patch(_BG_CLIENT, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = self.controller._update_service(
                service, values=values, version=None, clear_keys=clear_keys)
        return result, client


@tagged('post_install', '-at_install')
class TestTenantRestart1432(_Fixture):

    def _restart(self, user, service, client=None):
        client = client or MagicMock()
        with patch(_MODEL_CLIENT, return_value=client), self._request_as(user):
            result = self.controller.api_workspace_service(
                self.workspace.id, service.id, action='restart')
        return result, client

    def test_owner_restart_issues_rollout_and_locks_actions(self):
        service = self._service()
        result, client = self._restart(self.admin_user, service)

        self.assertTrue(result.get('success'), result)
        client.restart_release.assert_called_once_with(
            namespace='paas-ws-r8life', release_name='svc-r8-life')
        self.assertEqual(service.state, 'upgrading')
        self.assertTrue(service.last_upgraded_at)
        self.assertEqual(result['data']['state'], 'upgrading')

    def test_member_without_admin_role_is_denied(self):
        service = self._service()
        member = self.env['res.users'].create({
            'name': 'r8-member', 'login': 'r8-member-1432',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': member.id, 'role': ROLE_USER,
        })
        result, client = self._restart(member, service)
        self.assertFalse(result.get('success'))
        client.restart_release.assert_not_called()
        self.assertEqual(service.state, 'running')

    def test_only_running_service_can_restart(self):
        service = self._service(state='upgrading')
        result, client = self._restart(self.admin_user, service)
        self.assertFalse(result.get('success'))
        client.restart_release.assert_not_called()

    def test_vm_service_is_refused(self):
        vm_template = self._template('r8-life-vm', 'vm')
        service = self._service(template=vm_template)
        result, client = self._restart(self.admin_user, service)
        self.assertFalse(result.get('success'))
        client.restart_release.assert_not_called()

    def test_operator_failure_keeps_state(self):
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorError
        service = self._service()
        client = MagicMock()
        client.restart_release.side_effect = PaaSOperatorError('boom', status_code=500)
        result, _client = self._restart(self.admin_user, service, client=client)
        self.assertFalse(result.get('success'))
        self.assertEqual(service.state, 'running')

    def test_restart_capability_is_mirrored_in_payload(self):
        service = self._service()
        with self._request_as(self.admin_user):
            data = self.controller._format_service(service)
        self.assertTrue(data['template']['restart_supported'])
        vm = self._service(template=self._template('r8-life-vm2', 'vm'))
        with self._request_as(self.admin_user):
            data = self.controller._format_service(vm)
        self.assertFalse(data['template']['restart_supported'])


@tagged('post_install', '-at_install')
class TestSecretKeyStatesAndClear1455(_Fixture):

    def test_unset_keys_are_reported_as_not_set(self):
        service = self._service(values={
            'auth': {'openrouterApiKey': 'sk-or-current', 'anthropicApiKey': ''},
        })
        with self._request_as(self.admin_user):
            data = self.controller._format_service(service, include_details=True)
        self.assertEqual(data['password_specs_set'], {
            'auth.openrouterApiKey': True,
            'auth.anthropicApiKey': False,   # 空字串＝未設定
            'git.token': False,              # 從沒設定
        })
        # 只回布林，不回值
        self.assertNotIn('sk-or-current', json.dumps(data['password_specs_set']))

    def test_clear_removes_the_key(self):
        service = self._service()
        result, client = self._update(service, values={}, clear_keys=['git.token'])

        self.assertTrue(result.get('success'), result)
        stored = service._helm_values_dict()
        self.assertNotIn('token', stored.get('git', {}))
        self.assertEqual(stored['git']['authorName'], 'Alice')
        self.assertEqual(stored['auth']['openrouterApiKey'], 'sk-or-current')
        sent = client.upgrade_release.call_args.kwargs['values']
        self.assertNotIn('token', sent.get('git', {}))
        # #1435：清除也是憑證變更 ⇒ upgrade 之後重啟
        client.restart_release.assert_called_once_with(
            namespace='paas-ws-r8life', release_name='svc-r8-life')

    def test_clear_wins_over_a_value_in_the_same_request(self):
        service = self._service()
        result, _client = self._update(
            service, values={'git.token': 'ghp-new'}, clear_keys=['git.token'])
        self.assertTrue(result.get('success'), result)
        self.assertNotIn('token', service._helm_values_dict().get('git', {}))

    def test_required_key_cannot_be_cleared(self):
        service = self._service()
        before = service._helm_values_dict()
        result, client = self._update(
            service, values={}, clear_keys=['auth.openrouterApiKey'])
        self.assertFalse(result.get('success'))
        self.assertIn('auth.openrouterApiKey', result['error'])
        client.upgrade_release.assert_not_called()
        self.assertEqual(service._helm_values_dict(), before)
        self.assertEqual(service.state, 'running')

    def test_non_secret_key_cannot_be_cleared(self):
        service = self._service()
        result, client = self._update(service, values={}, clear_keys=['git.authorName'])
        self.assertFalse(result.get('success'))
        client.upgrade_release.assert_not_called()

    def test_clear_keys_must_be_a_list(self):
        service = self._service()
        result, client = self._update(service, values={}, clear_keys='git.token')
        self.assertFalse(result.get('success'))
        client.upgrade_release.assert_not_called()


@tagged('post_install', '-at_install')
class TestRestartAfterSecretChange1435(_Fixture):

    def test_changed_key_restarts_after_upgrade(self):
        service = self._service()
        result, client = self._update(service, values={'auth.anthropicApiKey': 'sk-ant-new'})
        self.assertTrue(result.get('success'), result)
        client.upgrade_release.assert_called_once()
        client.restart_release.assert_called_once()
        # 順序：先 upgrade（Secret 換新）再 restart
        names = [c[0] for c in client.method_calls
                 if c[0] in ('upgrade_release', 'restart_release')]
        self.assertEqual(names, ['upgrade_release', 'restart_release'])

    def test_non_secret_change_does_not_restart(self):
        service = self._service()
        result, client = self._update(service, values={'git.authorName': 'Bob'})
        self.assertTrue(result.get('success'), result)
        client.upgrade_release.assert_called_once()
        client.restart_release.assert_not_called()

    def test_same_value_does_not_restart(self):
        service = self._service()
        result, client = self._update(service, values={'git.token': 'ghp-current'})
        self.assertTrue(result.get('success'), result)
        client.restart_release.assert_not_called()

    def test_masked_echo_does_not_restart(self):
        """前端舊 bundle 把遮罩值送回來（#535）＝沒變更，不得觸發重啟。"""
        service = self._service()
        result, client = self._update(service, values={'git.token': '***'})
        self.assertTrue(result.get('success'), result)
        self.assertEqual(service._helm_values_dict()['git']['token'], 'ghp-current')
        client.restart_release.assert_not_called()

    def test_restart_failure_does_not_fail_the_update(self):
        service = self._service()
        client = MagicMock()
        client.upgrade_release.return_value = {'revision': 3, 'chart_version': '0.1.0'}
        client.restart_release.side_effect = RuntimeError('operator down')
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                self._request_as(self.admin_user), \
                patch(_BG_CLIENT, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread):
            result = self.controller._update_service(
                service, values={'auth.anthropicApiKey': 'sk-ant-new'}, version=None)
        self.assertTrue(result.get('success'), result)
        self.assertNotEqual(service.state, 'error')
