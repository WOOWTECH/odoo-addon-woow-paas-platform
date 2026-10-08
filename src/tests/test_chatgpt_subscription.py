"""LiteLLM「ChatGPT 訂閱」（平台端；chart 0.2.0、operator ``chatgpt-subscription`` 端點）。

租戶在服務頁用 device code 登入 ChatGPT Plus／Pro，讓 LiteLLM 的 ``chatgpt/`` provider 用訂閱
額度。operator exec 進 proxy 容器跑 chart 的 ``woow_chatgpt.py``；平台只轉送、不保存任何 token。
本檔鎖：

- 能力欄位 ``chatgpt_subscription_supported`` 是唯一判準（endpoint gate、model 防線、payload
  鏡射），seed 只有 LiteLLM 開。
- 授權：workspace owner／admin 可用；user／guest 拒絕；別的工作區的服務＝不存在；
  會動到東西的 action 過訂閱閘；服務要 running。model 方法 private。
- operator 各種結果對應成租戶看得懂的訊息與 error_code。
- 回應是白名單視圖（token 鍵、未知鍵都丟掉；登入網址只收 OpenAI 的）；一次性代碼
  ``user_code`` 不進任何 log。
"""
import json
import logging
import time
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.service.model import get_public_method
from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.models.workspace_access import (
    ROLE_ADMIN,
    ROLE_GUEST,
    ROLE_USER,
)
from odoo.addons.woow_paas_platform.services.paas_operator import (
    CHATGPT_SUBSCRIPTION_ADD_MODELS_TIMEOUT,
    CHATGPT_SUBSCRIPTION_TEST_TIMEOUT,
    CHATGPT_SUBSCRIPTION_TIMEOUT,
    PaaSOperatorAPIError,
    PaaSOperatorClient,
    PaaSOperatorTimeoutError,
)
from ..models.cloud_service import CHATGPT_SUBSCRIPTION_VERIFICATION_URL, _internal_provision

CLIENT_PATH = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'
REQUEST_PATH = 'odoo.addons.woow_paas_platform.controllers.paas.request'
USER_CODE = 'WOOW-77Q1Z'

CONNECTED = {
    'connected': True, 'email': 'tenant@example.com', 'plan_type': 'plus',
    'account_id_tail': '123456', 'access_token_expires_at': 1791622119,
    'subscription_active_until': None, 'persisted': True, 'pending': None,
    'models': ['chatgpt/gpt-5.4', 'chatgpt/gpt-5.5'],
}


def _pending(**overrides):
    pending = {
        'verification_url': 'https://auth.openai.com/codex/device',
        'user_code': USER_CODE,
        'expires_at': int(time.time()) + 900,
        'interval': 5,
    }
    pending.update(overrides)
    return pending


class _ChatgptFixture(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.owner = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'ChatGPT subscription WS', 'owner_id': self.owner.id,
        })
        self.template = self._template('litellm-chatgpt-test', supported=True)
        self.plain_template = self._template('plain-chatgpt-test', supported=False)
        self.service = self._service(self.template)

    def _template(self, slug, supported):
        return self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Template %s' % slug,
            'slug': slug,
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.2.0',
            'helm_default_values': '{}',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
            'chatgpt_subscription_supported': supported,
        })

    def _service(self, template, state='running', workspace=None):
        return _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'svc-%s' % template.slug,
            'workspace_id': (workspace or self.workspace).id,
            'template_id': template.id,
            'state': state,
            'helm_namespace': 'paas-ws-chatgpt-test',
            'helm_release_name': 'svc-%s' % template.slug,
            'helm_chart_version': '0.2.0',
            'helm_values': '{}',
        })

    def _member(self, login, role):
        user = self.env['res.users'].create({
            'name': login, 'login': login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': user.id, 'role': role,
        })
        return user

    def _as(self, user):
        return patch(REQUEST_PATH, new=MagicMock(env=self.env(user=user)))

    def _client(self, return_value=None, side_effect=None):
        client = MagicMock()
        if side_effect is not None:
            client.chatgpt_subscription.side_effect = side_effect
        else:
            client.chatgpt_subscription.return_value = (
                return_value if return_value is not None else {'success': True, 'data': CONNECTED})
        return client

    def _call(self, action, user=None, client=None, service=None, **kw):
        client = client or self._client()
        with patch(CLIENT_PATH, return_value=client), self._as(user or self.owner):
            result = self.controller.api_service_chatgpt_subscription(
                self.workspace.id, (service or self.service).id, action=action, **kw)
        return result, client


@tagged('post_install', '-at_install')
class TestChatgptSubscriptionCapability(_ChatgptFixture):

    def test_flag_defaults_off_and_payload_mirrors_it(self):
        self.assertFalse(self.plain_template.chatgpt_subscription_supported)
        plain = self._service(self.plain_template)
        with self._as(self.owner):
            on = self.controller._format_service(self.service, include_details=True)
            off = self.controller._format_service(plain)
        self.assertIs(on['template']['chatgpt_subscription_supported'], True)
        self.assertIs(off['template']['chatgpt_subscription_supported'], False)

    def test_only_litellm_seed_enables_it(self):
        litellm = self.env.ref('woow_paas_platform.cloud_app_litellm')
        self.assertTrue(litellm.chatgpt_subscription_supported)
        seeded = self.env['woow_paas_platform.cloud_app_template'].search([
            ('chatgpt_subscription_supported', '=', True),
            ('id', '!=', self.template.id),
        ])
        self.assertEqual(seeded.mapped('slug'), ['litellm'],
                         '只有內附 woow_chatgpt.py 的 LiteLLM chart 才能開這張卡片')

    def test_get_payload_carries_viewer_role(self):
        member = self._member('chatgpt-sub-viewer', ROLE_USER)
        for user, role in ((self.owner, 'owner'), (member, 'user')):
            with self._as(user):
                result = self.controller.api_workspace_service(
                    self.workspace.id, self.service.id, action='get')
            self.assertTrue(result['success'], result)
            self.assertEqual(result['data']['viewer_role'], role)
            self.assertIs(result['data']['template']['chatgpt_subscription_supported'], True)

    def test_model_method_is_not_remotely_callable(self):
        with self.assertRaises(AccessError):
            get_public_method(self.env['woow_paas_platform.cloud_service'], '_chatgpt_subscription')

    def test_model_method_refuses_unsupported_template(self):
        plain = self._service(self.plain_template)
        with patch(CLIENT_PATH) as get_client, self.assertRaises(UserError):
            plain._chatgpt_subscription('status')
        get_client.return_value.chatgpt_subscription.assert_not_called()


@tagged('post_install', '-at_install')
class TestChatgptSubscriptionEndpoint(_ChatgptFixture):

    def test_owner_and_admin_can_check_status(self):
        admin = self._member('chatgpt-sub-admin', ROLE_ADMIN)
        for user in (self.owner, admin):
            result, client = self._call('status', user=user)
            self.assertTrue(result['success'], result)
            self.assertEqual(result['data']['email'], 'tenant@example.com')
            self.assertEqual(result['data']['models'], CONNECTED['models'])
            client.chatgpt_subscription.assert_called_once_with(
                namespace='paas-ws-chatgpt-test', release_name=self.service.helm_release_name,
                action='status', payload=None)

    def test_user_and_guest_are_denied(self):
        for login, role in (('chatgpt-sub-user', ROLE_USER), ('chatgpt-sub-guest', ROLE_GUEST)):
            member = self._member(login, role)
            for action in ('status', 'start'):
                result, client = self._call(action, user=member)
                self.assertFalse(result['success'], (role, action))
                client.chatgpt_subscription.assert_not_called()

    def test_template_without_flag_is_refused(self):
        plain = self._service(self.plain_template)
        result, client = self._call('status', service=plain)
        self.assertFalse(result['success'])
        self.assertEqual(result['error_code'], 'not_supported')
        client.chatgpt_subscription.assert_not_called()

    def test_service_of_another_workspace_is_not_found(self):
        other_ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'Other ChatGPT WS', 'owner_id': self.owner.id})
        foreign = self._service(self.template, workspace=other_ws)
        result, client = self._call('status', service=foreign)
        self.assertFalse(result['success'])
        self.assertIn('not found', result['error'])
        client.chatgpt_subscription.assert_not_called()

    def test_requires_a_running_service(self):
        for state in ('upgrading', 'suspended', 'error'):
            self.service.write({'state': state})
            result, client = self._call('status')
            self.assertFalse(result['success'], state)
            client.chatgpt_subscription.assert_not_called()

    def test_write_actions_pass_the_subscription_gate(self):
        Service = type(self.env['woow_paas_platform.cloud_service'])
        with patch.object(Service, '_check_operation_allowed',
                          side_effect=UserError('Subscription expired')) as gate:
            for action in ('start', 'poll', 'add_models', 'test', 'disconnect'):
                result, client = self._call(action)
                self.assertFalse(result['success'], action)
                self.assertEqual(result['error'], 'Subscription expired')
                client.chatgpt_subscription.assert_not_called()
            gate.reset_mock()
            result, client = self._call('status')
        self.assertTrue(result['success'], '只讀的 status 不過訂閱閘')
        gate.assert_not_called()

    def test_unknown_action_is_rejected(self):
        result, client = self._call('restore')
        self.assertFalse(result['success'])
        client.chatgpt_subscription.assert_not_called()

    def test_add_models_and_test_map_actions_and_payloads(self):
        client = self._client({'success': True, 'data': {
            'added': ['chatgpt/gpt-5.4'], 'kept': ['chatgpt/gpt-5.5'], 'failed': []}})
        result, client = self._call('add_models', client=client,
                                    models=['gpt-5.4', 'chatgpt/gpt-5.5'])
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['added'], ['chatgpt/gpt-5.4'])
        kwargs = client.chatgpt_subscription.call_args.kwargs
        self.assertEqual(kwargs['action'], 'add-models')
        self.assertEqual(kwargs['payload'], {'models': ['gpt-5.4', 'chatgpt/gpt-5.5']})

        result, client = self._call('add_models')
        self.assertIsNone(client.chatgpt_subscription.call_args.kwargs['payload'],
                          '省略 models＝chart 的預設清單')

        client = self._client({'success': True, 'data': {
            'model': 'chatgpt/gpt-5.4', 'seconds': 2.1, 'reply': 'OK'}})
        result, client = self._call('test', client=client, model='chatgpt/gpt-5.4')
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['reply'], 'OK')
        self.assertEqual(client.chatgpt_subscription.call_args.kwargs['payload'],
                         {'model': 'chatgpt/gpt-5.4'})

    def test_invalid_model_names_never_reach_the_operator(self):
        for models in (['gpt 5'], ['../etc'], ['x' * 70], ['gpt-5.4\n'], ['-gpt'],
                       ['gpt-%d' % i for i in range(13)], 'gpt-5.4', [5]):
            result, client = self._call('add_models', models=models)
            self.assertFalse(result['success'], models)
            self.assertIn('Invalid model name', result['error'])
            client.chatgpt_subscription.assert_not_called()
        result, client = self._call('test', model='chatgpt/gpt;rm')
        self.assertFalse(result['success'])
        client.chatgpt_subscription.assert_not_called()

    def test_operator_outcomes_map_to_tenant_messages(self):
        cases = [
            ({'side_effect': PaaSOperatorAPIError('Not Found', status_code=404, detail='Not Found')},
             'not available yet', 'operator_unsupported'),
            ({'side_effect': PaaSOperatorTimeoutError('timeout')},
             'did not respond', 'exec_timeout'),
            ({'side_effect': PaaSOperatorAPIError('boom', status_code=500, detail='x')},
             'did not respond', 'operator_error'),
            ({'return_value': {'success': False, 'error_kind': 'not_executed',
                               'error_code': 'unsupported_chart'}},
             'Redeploy', 'unsupported_chart'),
            ({'return_value': {'success': False, 'error_kind': 'not_executed',
                               'error_code': 'pod_not_found'}},
             'not ready yet', 'pod_not_found'),
            ({'return_value': {'success': False, 'error_kind': 'failed',
                               'error_code': 'not_connected'}},
             'Connect your ChatGPT subscription first', 'not_connected'),
            ({'return_value': {'success': False, 'error_kind': 'failed',
                               'error_code': 'device_code_request_failed',
                               'data': {'status_code': 503}}},
             'sign-in code', 'device_code_request_failed'),
            ({'return_value': {'success': False, 'error_kind': 'failed',
                               'error_code': 'token_exchange_failed',
                               'data': {'state': 'error', 'status_code': 400}}},
             'Sign-in failed', 'token_exchange_failed'),
            ({'return_value': {'success': False, 'error_kind': 'failed',
                               'error_code': 'poll_failed', 'data': {'state': 'error'}}},
             'Sign-in failed', 'poll_failed'),
            ({'return_value': {'success': False, 'error_kind': 'unknown',
                               'error_code': 'exec_timeout'}},
             'did not respond', 'exec_timeout'),
            ({'return_value': {'success': False, 'error_kind': 'unknown', 'error_code': None}},
             'did not respond', 'bad_output'),
            ({'return_value': {'success': False, 'error_kind': 'failed',
                               'error_code': 'command_failed', 'error': 'boom'}},
             'could not complete', 'command_failed'),
            ({'return_value': {'success': False, 'error_kind': 'failed',
                               'error_code': 'db_delete_failed'}},
             'could not be removed', 'db_delete_failed'),
            # 平台不認得的碼（自由文字、未來新增的碼）不原樣轉給前端。
            ({'return_value': {'success': False, 'error_kind': 'failed',
                               'error_code': 'something new from the chart'}},
             'request failed', 'failed'),
        ]
        for outcome, needle, code in cases:
            client = MagicMock()
            client.chatgpt_subscription.configure_mock(**outcome)
            result, _client = self._call('poll', client=client)
            self.assertFalse(result['success'], outcome)
            self.assertIn(needle, result['error'], outcome)
            self.assertEqual(result['error_code'], code, outcome)

    def test_test_failure_shows_the_upstream_message(self):
        upstream = 'ChatGPT is not connected (請先在平台連接 ChatGPT 訂閱)'
        responses = (
            # operator #1547：test_failed，上游原文在 error 與 data.error。
            {'success': False, 'error_kind': 'failed', 'error_code': 'test_failed',
             'error': upstream, 'data': {'model': 'chatgpt/gpt-5.4', 'status_code': 401,
                                         'seconds': 0.2, 'error': upstream}},
            # 只有頂層 error 帶原文。
            {'success': False, 'error_kind': 'failed', 'error_code': 'test_failed',
             'error': upstream, 'data': {'model': 'chatgpt/gpt-5.4', 'status_code': 401}},
            # DESIGN §2 原稿的形狀：原文直接當 error_code。
            {'success': False, 'error_kind': 'failed', 'error_code': upstream,
             'data': {'model': 'chatgpt/gpt-5.4', 'status_code': 401}},
        )
        for response in responses:
            result, _client = self._call('test', client=self._client(response))
            self.assertFalse(result['success'])
            self.assertEqual(result['error_code'], 'test_failed')
            self.assertIn('HTTP 401', result['error'])
            self.assertIn(upstream, result['error'])
            self.assertNotIn('error', result.get('data', {}), '原文只放在訊息裡，不另外轉送')

    def test_add_models_partial_failure_lists_the_models(self):
        data = {'added': ['chatgpt/gpt-5.4'], 'kept': [], 'failed': ['chatgpt/gpt-9']}
        for code in ('models_failed', None, 'command_failed'):
            client = self._client({'success': False, 'error_kind': 'failed', 'error_code': code,
                                   'error': code, 'data': data})
            result, _client = self._call('add_models', client=client)
            self.assertFalse(result['success'], code)
            self.assertEqual(result['error_code'], 'models_failed', code)
            self.assertIn('chatgpt/gpt-9', result['error'], code)
            self.assertEqual(result['data']['added'], ['chatgpt/gpt-5.4'])

    def test_response_is_a_whitelisted_view_without_tokens(self):
        data = dict(CONNECTED, access_token='at-secret', refresh_token='rt-secret',
                    id_token='id-secret', persist_error='OperationalError', surprise='x',
                    pending=_pending(verification_url='https://evil.example/phish', interval=0))
        client = self._client({'success': True, 'data': data})
        result, _client = self._call('start', client=client)
        self.assertTrue(result['success'], result)
        dumped = json.dumps(result)
        for secret in ('at-secret', 'rt-secret', 'id-secret', 'surprise', 'persist_error',
                       'evil.example'):
            self.assertNotIn(secret, dumped)
        pending = result['data']['pending']
        self.assertEqual(pending['user_code'], USER_CODE)
        self.assertEqual(pending['verification_url'], CHATGPT_SUBSCRIPTION_VERIFICATION_URL)
        self.assertEqual(pending['interval'], 5, 'interval 0／缺席＝預設 5 秒')
        self.assertTrue(0 < pending['expires_in'] <= 900)

        client = self._client({'success': True, 'data': {
            'state': 'pending', 'pending': _pending(interval=1, expires_at=0)}})
        result, _client = self._call('poll', client=client)
        self.assertEqual(result['data']['pending']['interval'], 3, '下限 3 秒')
        self.assertEqual(result['data']['pending']['expires_in'], 0)

    def test_user_code_never_reaches_the_log(self):
        """整條鏈（真的 PaaSOperatorClient，只 mock HTTP session）都不把代碼寫進 log。"""
        real = PaaSOperatorClient('http://paas-operator.test', 'test-key')
        body = {'success': True, 'data': {'state': 'pending', 'connected': False,
                                          'pending': _pending()}}
        response = MagicMock(status_code=200, content=json.dumps(body).encode())
        response.json.return_value = body
        real._session = MagicMock()
        real._session.request.return_value = response
        with self.assertLogs('odoo.addons.woow_paas_platform', level=logging.DEBUG) as logs:
            result, _client = self._call('start', client=real)
            failing = self._client({'success': False, 'error_kind': 'failed',
                                    'error_code': 'upstream says %s' % USER_CODE,
                                    'data': {'status_code': 401, 'error': USER_CODE}})
            self._call('test', client=failing)
        self.assertEqual(result['data']['pending']['user_code'], USER_CODE)
        self.assertTrue(logs.output)
        self.assertNotIn(USER_CODE, '\n'.join(logs.output))

    def test_client_routes_and_timeout_budgets(self):
        client = PaaSOperatorClient('http://paas-operator.test', 'test-key')
        expected = {
            'status': CHATGPT_SUBSCRIPTION_TIMEOUT,
            'start': CHATGPT_SUBSCRIPTION_TIMEOUT,
            'poll': CHATGPT_SUBSCRIPTION_TIMEOUT,
            'disconnect': CHATGPT_SUBSCRIPTION_TIMEOUT,
            'add-models': CHATGPT_SUBSCRIPTION_ADD_MODELS_TIMEOUT,
            'test': CHATGPT_SUBSCRIPTION_TEST_TIMEOUT,
        }
        with patch.object(PaaSOperatorClient, '_request', return_value={'success': True}) as req:
            for action, timeout in expected.items():
                client.chatgpt_subscription('paas-ws-x', 'rel', action)
                args, kwargs = req.call_args
                self.assertEqual(args, (
                    'POST', '/api/releases/paas-ws-x/rel/chatgpt-subscription/%s' % action))
                self.assertEqual(kwargs['timeout'], timeout, action)
            req.reset_mock()
            with self.assertRaises(ValueError):
                client.chatgpt_subscription('paas-ws-x', 'rel', 'restore')
            req.assert_not_called()
        # operator（#1547）的 exec 逾時 30／60／150 秒＋找 pod 餘裕 10 秒必須蓋得過。
        self.assertGreater(CHATGPT_SUBSCRIPTION_TIMEOUT, 30 + 10)
        self.assertGreater(CHATGPT_SUBSCRIPTION_ADD_MODELS_TIMEOUT, 60 + 10)
        self.assertGreater(CHATGPT_SUBSCRIPTION_TEST_TIMEOUT, 150 + 10)
