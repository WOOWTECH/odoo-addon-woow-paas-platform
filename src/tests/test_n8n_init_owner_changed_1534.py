# -*- coding: utf-8 -*-
"""#1534：租戶在 n8n 改了 owner 密碼或開了 MFA，Redeploy 的 init 不能因此失敗。

operator（主修）：owner 已存在時不再用平台存的 owner 密碼登入，改 exec 進容器沿用／簽發
API key（extra/paas-operator 的單元＋integration 測試）。平台這側鎖：

- 送 ``owner_confirmed``：這台服務 init 成功過（有 ``n8n_api_key``）＝owner 是平台建立、之後歸
  租戶管理；沒確認過的才要 operator 核對 owner Email。
- 過渡期（平台先上、operator 還是舊版）：舊 operator 登入 401（MFA 是 code 998）而服務
  init 成功過 ⇒ 不計失敗額度、直接完成（API key 沿用上一次的）。沒確認過的照舊計額度。
- owner 類失敗用完額度時，error 訊息講清楚是 owner 帳號的問題，不是籠統的「初始化失敗」。

真 cursor 那幾支的 fixture 比照 ``test_init_execution_control_1257.TestInitLeaseRealCursor1257``
（claim／lease／CAS 必須跨連線成立，所以真的 commit、事後清掉）。
"""
import uuid
from unittest.mock import patch

from odoo import SUPERUSER_ID, api
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import INIT_FAILED_LIMIT, _internal_provision
from ..models.cloud_service_init_attempt import classify_init_result
from ..services.paas_operator import (
    INIT_N8N_CONNECT_TIMEOUT,
    INIT_N8N_READ_TIMEOUT,
    PaaSOperatorClient,
)
from .committed_cron_scope import CommittedCronScope
from .operator_contract import operator_client
from .test_init_execution_control_1257 import _make_service, _template_vals

SVC = 'woow_paas_platform.cloud_service'
_KEY = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0LTE1MzQifQ.c2lnbmF0dXJlLTE1MzQ'
# 舊 operator（9d66c1a）實際回的形狀：沙盒 n8n 2.35.3 改密碼／開 MFA 後重跑 init。
_OLD_OPERATOR_WRONG_PASSWORD = {
    'success': False, 'error_step': 'login', 'error_kind': 'failed',
    'error': '[login] Login returned HTTP 401: {"code":401,"message":"Wrong username or password. '
             'Do you have caps lock on?"}',
}
_OLD_OPERATOR_MFA = {
    'success': False, 'error_step': 'login', 'error_kind': 'failed',
    'error': '[login] Login returned HTTP 401: {"code":998,"message":"MFA Error"}',
}
_NEW_OPERATOR_OWNER_MISMATCH = {
    'success': False, 'error_step': 'owner_identity', 'error_kind': 'failed',
    'error': '[owner_identity] owner_mismatch: the n8n owner is not the account set at provisioning',
}


@tagged('post_install', '-at_install')
class TestN8nInitParams1534(TransactionCase):

    def setUp(self):
        super().setUp()
        tag = uuid.uuid4().hex[:8]
        template = self.env['woow_paas_platform.cloud_app_template'].create(
            _template_vals(tag, 'n8n'))
        workspace = self.env['woow_paas_platform.workspace'].create({'name': f'1534 WS {tag}'})
        self.svc = _make_service(self.env, workspace, template, f'n8n-{tag}', state='initializing')
        self.svc.sudo().write({'n8n_owner_email': 'owner-1534@example.invalid',
                               'n8n_owner_password': 'pw-1534'})

    def test_never_confirmed_service_asks_operator_to_check_the_email(self):
        params = self.svc._init_call_params('n8n')
        self.assertIs(params['owner_confirmed'], False)
        self.assertEqual(params['owner_email'], 'owner-1534@example.invalid')

    def test_service_with_a_previous_successful_init_is_confirmed(self):
        self.svc.sudo().write({'n8n_api_key': _KEY})
        self.assertIs(self.svc._init_call_params('n8n')['owner_confirmed'], True)

    def test_odoo_init_params_are_unchanged(self):
        self.assertNotIn('owner_confirmed', self.svc._init_call_params('odoo'))

    def test_client_sends_owner_confirmed_on_the_wire(self):
        client = PaaSOperatorClient('http://operator.test', 'k')
        with patch.object(PaaSOperatorClient, '_request', return_value={'success': True}) as req:
            client.init_n8n(namespace='ns', release_name='rel', owner_email='e@example.invalid',
                            owner_password='pw', owner_confirmed=True)
        args, kwargs = req.call_args
        self.assertEqual(args, ('POST', '/api/releases/ns/rel/init/n8n'))
        self.assertEqual(kwargs['data'], {'owner_email': 'e@example.invalid',
                                          'owner_password': 'pw', 'owner_confirmed': True})
        self.assertEqual(kwargs['timeout'], (INIT_N8N_CONNECT_TIMEOUT, INIT_N8N_READ_TIMEOUT))

    def test_client_default_is_not_confirmed(self):
        client = PaaSOperatorClient('http://operator.test', 'k')
        with patch.object(PaaSOperatorClient, '_request', return_value={'success': True}) as req:
            client.init_n8n(namespace='ns', release_name='rel', owner_email='e', owner_password='pw')
        self.assertIs(req.call_args.kwargs['data']['owner_confirmed'], False)

    def test_old_operator_login_rejections_classify_as_failed(self):
        """前提：舊 operator 的 401／998 在平台分類成 failed（才會走到額度判斷）。"""
        for result in (_OLD_OPERATOR_WRONG_PASSWORD, _OLD_OPERATOR_MFA):
            outcome = classify_init_result('n8n', result=result)
            self.assertEqual((outcome.outcome, outcome.error_step), ('failed', 'login'))


@tagged('post_install', '-at_install')
class TestN8nOwnerChangedRealCursor1534(TransactionCase):

    def setUp(self):
        super().setUp()
        self.tag = uuid.uuid4().hex[:10]
        self.scope = CommittedCronScope(self)
        self.addCleanup(self.scope.purge)
        with self.scope.track(), self.registry.cursor() as cr:
            env = api.Environment(cr, SUPERUSER_ID, {})
            self.scope.monitor_id = env['woow_paas_platform.platform_monitor'].create({
                'name': f'1534 own monitor {self.tag}'}).id
            workspace = env['woow_paas_platform.workspace'].create({
                'name': f'1534 Workspace {self.tag}'})
            template = env['woow_paas_platform.cloud_app_template'].create(
                _template_vals(self.tag, 'n8n'))
            service = _make_service(env, workspace, template, f'n8n-{self.tag}',
                                    state='initializing')
            now = service._init_db_now()
            service.write({
                'init_pending': True,
                'init_wait_since': now,
                'init_ready_observed': True,
                'init_ready_since': now,
                'init_queue_turn': now,
                'n8n_owner_email': f'owner-{self.tag}@example.invalid',
                'n8n_owner_password': f'pw-{self.tag}',
            })
            self.service_id = service.id
        self.enterContext(self.scope.isolated())

    def tearDown(self):
        try:
            self.assertFalse(self.scope.foreign_calls)
        finally:
            super().tearDown()

    def _env(self, cr):
        if cr.transaction is None:
            cr.transaction = api.Transaction(self.registry)
        return api.Environment(cr, SUPERUSER_ID, {})

    def _run(self, fn):
        with self.registry.cursor() as cr:
            env = self._env(cr)
            return fn(env[SVC].browse(self.service_id))

    def _entry(self, client):
        self._run(lambda svc: svc._run_post_deploy_init(self.scope.operator(client)))

    def _confirm(self):
        """模擬「上一代 init 成功過」：平台存著那時拿到的 key。"""
        self._run(lambda svc: svc.write({'n8n_api_key': _KEY}))

    def _read(self):
        return self._run(lambda svc: {
            'state': svc.state,
            'init_pending': svc.init_pending,
            'init_retries': svc.init_retries,
            'init_exit': svc.init_exit,
            'lease': svc.init_lease_until,
            'n8n_api_key': svc.n8n_api_key,
            'error_message': svc.error_message,
        })

    def _assert_confirmed_service_completes(self, result):
        self._confirm()
        client = operator_client(init_n8n=result)
        self._entry(client)
        state = self._read()
        self.assertEqual(client.init_n8n.call_args.kwargs['owner_confirmed'], True)
        self.assertEqual(
            (state['state'], state['init_pending'], state['init_retries'], state['lease']),
            ('running', False, 0, False), '不計額度、直接完成')
        self.assertEqual(state['n8n_api_key'], _KEY, 'API key 沿用上一次的')
        self.assertFalse(state['error_message'])

    def test_confirmed_service_completes_when_tenant_changed_the_password(self):
        self._assert_confirmed_service_completes(_OLD_OPERATOR_WRONG_PASSWORD)

    def test_confirmed_service_completes_when_tenant_turned_on_mfa(self):
        self._assert_confirmed_service_completes(_OLD_OPERATOR_MFA)

    def test_unconfirmed_login_rejection_still_counts(self):
        """沒確認過 owner：可能是別人搶先完成 setup，照舊計額度。"""
        client = operator_client(init_n8n=_OLD_OPERATOR_WRONG_PASSWORD)
        self._entry(client)
        state = self._read()
        self.assertEqual(client.init_n8n.call_args.kwargs['owner_confirmed'], False)
        self.assertEqual((state['state'], state['init_retries']), ('initializing', 1))

    def test_other_confirmed_failures_still_count(self):
        """只放過「登入 401」：同一步的其他錯誤（例如 429 太多次）、別步的失敗照舊計。"""
        self._confirm()
        client = operator_client(init_n8n={
            'success': False, 'error_step': 'login', 'error_kind': 'failed',
            'error': '[login] Login returned HTTP 429: Too many requests'})
        self._entry(client)
        self.assertEqual(self._read()['init_retries'], 1)

    def test_owner_identity_failures_end_with_a_clear_message(self):
        client = operator_client(init_n8n=_NEW_OPERATOR_OWNER_MISMATCH)
        for _ in range(INIT_FAILED_LIMIT):
            self._entry(client)
        state = self._read()
        self.assertEqual((state['state'], state['init_exit']), ('error', 'failed'))
        self.assertIn('n8n administrator account', state['error_message'])
        self.assertIn('still running', state['error_message'])
        self.assertNotIn('Initialization failed', state['error_message'])

    def test_generic_failures_keep_the_generic_message(self):
        client = operator_client(init_n8n={
            'success': False, 'error_step': 'create_api_key', 'error_kind': 'failed',
            'error': '[create_api_key] API key creation returned HTTP 403: forbidden'})
        for _ in range(INIT_FAILED_LIMIT):
            self._entry(client)
        self.assertIn('Initialization failed', self._read()['error_message'])

    def test_new_operator_success_is_unchanged(self):
        """新 operator 對既有 owner 回 success＋key：與 #1333 的成功路徑相同（等重啟後的 Pod）。"""
        self._confirm()
        client = operator_client(init_n8n={'success': True, 'api_key': _KEY})
        self._entry(client)
        state = self._read()
        self.assertEqual((state['state'], state['init_pending'], state['init_retries']),
                         ('initializing', False, 0))
