# -*- coding: utf-8 -*-
"""#1526：「回復版本」不降 chart、不還原已撤換的金鑰，DB 與叢集保持一致。

稽核（sbx-b #9／#16）：

1. **可降版**：.210 的回復照目標 revision 的 chart 版本重新渲染，工作區 admin 能把
   pi-agent 0.1.6 回復成 0.1.4（egress 變回全放行、沒有 ephemeral-storage 上限），帳本也
   寫成 0.1.4，之後的改帳密、修改配置都釘在舊版。
2. **還原已撤換的金鑰**：open-design 回復後 Secret 與 daemon 的 OpenRouter 金鑰回到已撤換
   的舊值 A；平台沒有回寫 DB 的 ``helm_values``，下一次「修改配置」又悄悄換回 B，daemon
   再重啟一次。#1433 只保留了管理員帳密。

修法（operator 端另見 ``extra/paas-operator/tests/test_rollback_chart_floor_1526.py`` 與
同名 integration）：

- chart 版本下限：目標 revision 的 chart 不得低於目前部署中的版本（operator 權威、平台預檢
  ＋對話框標示）。
- ``helm_value_specs`` 裡 ``type: password`` 的鍵列入保留路徑 ⇒ 回復後叢集上的金鑰就是 DB
  裡的那一把，下一次修改配置不會翻轉、不會多重啟。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from ..models.cloud_service import _internal_provision
from ..services.chart_version import (
    ROLLBACK_CHART_DOWNGRADE_ERROR_CODE,
    annotate_rollback_floor,
    chart_version_from_history,
    chart_version_not_lower,
    rollback_floor_violation,
    rollback_request_problem,
)
from ..services.paas_operator import PaaSOperatorAPIError

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
USERNAME_PATH = 'authProxy.basicAuth.username'
OD_SPECS = {
    'required': [],
    'optional': [
        {'key': 'auth.anthropicApiKey', 'label': 'Anthropic', 'type': 'password'},
        {'key': 'auth.openrouterApiKey', 'label': 'OpenRouter', 'type': 'password'},
        {'key': 'config.model', 'label': 'Model', 'type': 'text'},
        {'key': '_init.owner_password', 'label': 'Init', 'type': 'password'},
    ],
}


def _rev(number, version, status):
    return {'revision': number, 'chart': 'pi-agent-%s' % version, 'status': status,
            'updated': '2026-10-07T00:00:00Z', 'app_version': '1', 'description': ''}


# 稽核的形狀：rev1 0.1.4、rev2–3 0.1.6（rev3 部署中）。
AUDIT_HISTORY = [
    _rev(1, '0.1.4', 'superseded'),
    _rev(2, '0.1.6', 'superseded'),
    _rev(3, '0.1.6', 'deployed'),
]


@tagged('post_install', '-at_install')
class TestChartVersionFloorHelpers1526(TransactionCase):
    """純函式：版本解析、比較、對話框標示（與 operator 同規則）。"""

    def test_parse_history_chart_field(self):
        self.assertEqual(chart_version_from_history('pi-agent-0.1.6'), '0.1.6')
        self.assertEqual(chart_version_from_history('postgresql-18.8.5'), '18.8.5')
        self.assertEqual(chart_version_from_history('mcp-2-server-0.1.5'), '0.1.5')
        self.assertEqual(chart_version_from_history('x-chart-1.0.0-rc.1'), '1.0.0-rc.1')
        self.assertIsNone(chart_version_from_history('no-version-here'))
        self.assertIsNone(chart_version_from_history(None))

    def test_order(self):
        self.assertTrue(chart_version_not_lower('0.1.6', '0.1.4'))
        self.assertFalse(chart_version_not_lower('0.1.4', '0.1.6'))
        self.assertTrue(chart_version_not_lower('0.2.10', '0.2.9'), '數字比較，不是字串比較')
        self.assertFalse(chart_version_not_lower('1.0.0-rc.1', '1.0.0'))
        self.assertTrue(chart_version_not_lower('0.1.6', '0.1.6'))
        self.assertFalse(chart_version_not_lower(None, '0.1.6'), '未知一律不放行')
        self.assertFalse(chart_version_not_lower('weird', '0.1.6'))

    def test_annotate_marks_older_chart_revisions(self):
        annotated = annotate_rollback_floor(AUDIT_HISTORY)
        self.assertEqual(
            [(r['revision'], r['chart_version'], r['rollback_allowed']) for r in annotated],
            [(1, '0.1.4', False), (2, '0.1.6', True), (3, '0.1.6', True)])
        self.assertNotIn('rollback_allowed', AUDIT_HISTORY[0], '不得改呼叫端的物件')

    def test_failed_upgrade_does_not_raise_the_floor(self):
        history = [_rev(1, '0.1.6', 'deployed'), _rev(2, '0.1.7', 'failed')]
        self.assertIsNone(rollback_floor_violation(history, 1))

    def test_violation_for_explicit_and_previous_revision(self):
        self.assertEqual(rollback_floor_violation(AUDIT_HISTORY, 1), {
            'target_revision': 1, 'target_chart_version': '0.1.4',
            'current_chart_version': '0.1.6'})
        self.assertIsNone(rollback_floor_violation(AUDIT_HISTORY, 2))
        two = [_rev(1, '0.1.4', 'superseded'), _rev(2, '0.1.6', 'deployed')]
        self.assertEqual(rollback_floor_violation(two, None)['target_revision'], 1,
                         '不帶 revision＝上一版（與 operator／helm 同語意）')

    def test_unknown_revision_is_left_to_the_operator(self):
        self.assertIsNone(rollback_floor_violation(AUDIT_HISTORY, 9))

    # ---- DATA-ODOO-010-1：回復請求本身不成立 ----------------------------------

    def test_rollback_to_the_currently_deployed_revision_is_refused(self):
        """實測：revision＝目前版，helm 照樣回復到自己、產生新 revision、換掉 Pod。"""
        self.assertEqual(rollback_request_problem(AUDIT_HISTORY, 3), 'already_current')

    def test_zero_missing_and_negative_revisions_are_invalid(self):
        """AUDITFIX rollback-api-input-validation：0／缺值不再解讀成「上一版」。

        實測（DATA-ODOO-010-1、AI-PI-004-4、INFRA-EMQX-013-1）：誤送 0 會真的回復並重啟，
        pi 映像就這樣被退回舊版。API 語意改為「revision 必須是 ≥ 1 的整數」。
        """
        for requested in (None, 0, -1, True, '2'):
            with self.subTest(requested=requested):
                self.assertEqual(
                    rollback_request_problem(AUDIT_HISTORY, requested), 'invalid_revision')
        only_one = [_rev(1, '0.1.6', 'deployed')]
        self.assertEqual(rollback_request_problem(only_one, None), 'invalid_revision')

    def test_revision_missing_from_history_is_not_found(self):
        """AI-LLM-007-2：9999 不在歷史裡——平台先擋，不讓 helm 的英文原文透出。"""
        self.assertEqual(rollback_request_problem(AUDIT_HISTORY, 9999), 'not_found')
        self.assertEqual(rollback_request_problem(AUDIT_HISTORY, 9), 'not_found')

    def test_explicit_older_revision_is_allowed(self):
        self.assertIsNone(rollback_request_problem(AUDIT_HISTORY, 2))
        self.assertIsNone(rollback_request_problem(AUDIT_HISTORY, 1))

    def test_failed_latest_revision_can_roll_back_to_the_deployed_one(self):
        """升級失敗的 revision 狀態是 failed：回復到前一個 deployed 版本是合法的修復動作。"""
        history = [_rev(1, '0.1.6', 'deployed'), _rev(2, '0.1.7', 'failed')]
        self.assertIsNone(rollback_request_problem(history, 1))

    def test_unreadable_history_is_left_to_the_operator(self):
        self.assertIsNone(rollback_request_problem([], 3))
        self.assertIsNone(rollback_request_problem(None, 2))


@tagged('post_install', '-at_install')
class TestRollbackChartFloorAndKeys1526(TransactionCase):

    def setUp(self):
        super().setUp()
        self.controller = PaasController()
        self.admin_user = self.env.ref('base.user_admin')
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Rollback floor workspace',
            'owner_id': self.admin_user.id,
        })

    def _template(self, slug, **vals):
        base = {
            'name': 'Rollback 1526 %s' % slug,
            'slug': slug,
            'category': 'ai_llm',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example/%s' % slug,
            'helm_chart_version': '0.1.6',
            'mcp_enabled': False,
            'helm_default_values': '{}',
        }
        base.update(vals)
        return self.env['woow_paas_platform.cloud_app_template'].create(base)

    def _od_template(self, slug):
        return self._template(
            slug, has_admin_gui=True, admin_password_carrier='helm_secret',
            admin_username_helm_path=USERNAME_PATH, helm_value_specs=json.dumps(OD_SPECS))

    def _service(self, template, release, helm_values=None):
        service = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].with_context(
                mcp_admin_password='current-password-0000',
            )).create({
                'name': 'Svc %s' % release,
                'workspace_id': self.workspace.id,
                'template_id': template.id,
                'state': 'running',
                'helm_namespace': 'paas-ws-rb1526',
                'helm_release_name': release,
                'helm_chart_version': '0.1.6',
                'helm_values': json.dumps(helm_values or {}),
            })
        return service

    def _client(self, history=None, result=None):
        client = MagicMock()
        client.get_revisions.return_value = {
            'revisions': history if history is not None else AUDIT_HISTORY}
        client.rollback_release.return_value = result if result is not None else {
            'message': 'ok', 'mode': 'upgrade_preserving', 'chart_version': '0.1.6'}
        return client

    def _rollback(self, service, client, revision):
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request',
                      new=MagicMock(env=self.env(user=self.admin_user))):
            return self.controller._rollback_service(service, revision=revision)

    # ---- 1. chart 版本下限 -------------------------------------------------

    def test_older_chart_revision_is_refused_before_the_operator_call(self):
        service = self._service(self._od_template('rb1526-old'), 'svc-rb1526-old')
        client = self._client()

        response = self._rollback(service, client, 1)

        self.assertFalse(response.get('success'), response)
        self.assertIn('0.1.4', response['error'])
        self.assertIn('0.1.6', response['error'])
        client.rollback_release.assert_not_called()
        self.assertEqual(service.state, 'running', '被擋下的回復不得推進 state')
        self.assertEqual(service.helm_chart_version, '0.1.6', '帳本不得被寫成舊版')

    def test_operator_409_is_shown_as_the_same_message(self):
        """平台預檢放行（例如歷史在兩次呼叫之間變了），operator 仍擋下：訊息一致。"""
        service = self._service(self._od_template('rb1526-409'), 'svc-rb1526-409')
        client = self._client()
        client.rollback_release.side_effect = PaaSOperatorAPIError(
            'API error', status_code=409, detail={
                'error_code': ROLLBACK_CHART_DOWNGRADE_ERROR_CODE,
                'target_revision': 2, 'target_chart_version': '0.1.5',
                'current_chart_version': '0.1.6'})

        response = self._rollback(service, client, 2)

        self.assertFalse(response.get('success'), response)
        self.assertIn('0.1.5', response['error'])
        self.assertNotIn('error_code', response['error'], '不得把 operator 的原始 dict 丟給租戶')
        self.assertEqual(service.state, 'running')

    def test_unreadable_history_fails_closed(self):
        service = self._service(self._od_template('rb1526-nohist'), 'svc-rb1526-nohist')
        client = self._client()
        client.get_revisions.side_effect = PaaSOperatorAPIError(
            'API error', status_code=500, detail='boom')

        response = self._rollback(service, client, 2)

        self.assertFalse(response.get('success'), response)
        client.rollback_release.assert_not_called()

    def test_rollback_to_current_revision_is_refused_without_touching_helm(self):
        """DATA-ODOO-010-1：回復到目前版不得送給 operator（否則白白 helm 一輪、重建 Pod）。"""
        service = self._service(self._od_template('rb1526-cur'), 'svc-rb1526-cur')
        client = self._client()

        response = self._rollback(service, client, 3)

        self.assertFalse(response.get('success'), response)
        self.assertIn('currently deployed', response['error'])
        client.rollback_release.assert_not_called()
        self.assertEqual(service.state, 'running', '被擋下的回復不得推進 state')

    def test_zero_or_missing_revision_is_refused_without_touching_helm(self):
        """AUDITFIX：0／None 回結構化錯誤、不送 operator、state 不動（#1786 驗收）。"""
        service = self._service(self._od_template('rb1526-zero'), 'svc-rb1526-zero')
        for requested in (0, None, -3):
            with self.subTest(requested=requested):
                client = self._client()

                response = self._rollback(service, client, requested)

                self.assertFalse(response.get('success'), response)
                self.assertEqual(response.get('data', {}).get('code'), 'invalid_revision')
                self.assertNotIn('None', response['error'])
                client.get_revisions.assert_not_called()
                client.rollback_release.assert_not_called()
                self.assertEqual(service.state, 'running', '被擋下的回復不得推進 state')

    def test_unknown_revision_is_refused_with_a_localized_message(self):
        """AI-LLM-007-2：9999 → 平台自己的訊息（有 zh_TW 譯文），不是 helm 的英文原文。"""
        service = self._service(self._od_template('rb1526-9999'), 'svc-rb1526-9999')
        client = self._client()

        response = self._rollback(service, client, 9999)

        self.assertFalse(response.get('success'), response)
        self.assertEqual(response.get('data', {}).get('code'), 'revision_not_found')
        self.assertIn('9999', response['error'])
        self.assertNotIn('release history', response['error'])
        client.rollback_release.assert_not_called()
        self.assertEqual(service.state, 'running')

    def test_operator_400_detail_is_not_shown_verbatim(self):
        """歷史在兩次呼叫之間變了，operator 回 400：不把 operator／helm 的英文原文丟給租戶。"""
        service = self._service(self._od_template('rb1526-400'), 'svc-rb1526-400')
        client = self._client()
        client.rollback_release.side_effect = PaaSOperatorAPIError(
            'API error', status_code=400,
            detail='Revision 2 not found in release history')

        response = self._rollback(service, client, 2)

        self.assertFalse(response.get('success'), response)
        self.assertNotIn('release history', response['error'])
        self.assertEqual(response.get('data', {}).get('code'), 'revision_not_restorable')
        self.assertEqual(service.state, 'running')

    def test_api_refuses_zero_and_missing_revision_before_access_checks(self):
        """路由層：0／None／非數字一律回 invalid_revision，不進 _rollback_service。"""
        service = self._service(self._od_template('rb1526-api'), 'svc-rb1526-api')
        with patch.object(PaasController, '_rollback_service') as downstream, \
                patch(f'{_CONTROLLER}.request',
                      new=MagicMock(env=self.env(user=self.admin_user))):
            for requested in (0, None, -1, 'abc'):
                with self.subTest(requested=requested):
                    response = self.controller.api_service_rollback(
                        self.workspace.id, service.id, revision=requested)
                    self.assertFalse(response.get('success'), response)
                    self.assertEqual(
                        response.get('data', {}).get('code'), 'invalid_revision')
            downstream.assert_not_called()

    def test_same_chart_rollback_goes_through_and_records_the_version(self):
        service = self._service(self._od_template('rb1526-same'), 'svc-rb1526-same')
        client = self._client()

        response = self._rollback(service, client, 2)

        self.assertTrue(response.get('success'), response)
        self.assertEqual(client.rollback_release.call_args.kwargs['revision'], 2)
        self.assertEqual(service.state, 'upgrading')
        self.assertEqual(service.helm_chart_version, '0.1.6')

    def test_plain_rollback_reported_version_reaches_the_ledger(self):
        """純 rollback（沒有平台憑證的模板）回到較新的 revision：帳本跟著叢集走。"""
        service = self._service(self._template('rb1526-plain'), 'svc-rb1526-plain')
        service.sudo().write({'helm_chart_version': '0.1.4'})
        history = [_rev(1, '0.1.4', 'superseded'), _rev(2, '0.1.6', 'superseded'),
                   _rev(3, '0.1.4', 'deployed')]
        client = self._client(history=history, result={
            'message': 'Rollback was a success!', 'mode': 'rollback', 'chart_version': '0.1.6'})

        response = self._rollback(service, client, 2)

        self.assertTrue(response.get('success'), response)
        self.assertIsNone(client.rollback_release.call_args.kwargs['preserve_value_paths'])
        self.assertEqual(service.helm_chart_version, '0.1.6')

    def test_revisions_listing_is_annotated(self):
        service = self._service(self._od_template('rb1526-list'), 'svc-rb1526-list')
        client = self._client()
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request',
                      new=MagicMock(env=self.env(user=self.admin_user))):
            result = self.controller._get_service_revisions(service)
        self.assertTrue(result['success'], result)
        self.assertEqual([r['rollback_allowed'] for r in result['data']], [False, True, True])

    def _listing(self, operator_result):
        service = self._service(self._od_template('rb1526-hmax'), 'svc-rb1526-hmax')
        client = self._client()
        client.get_revisions.return_value = operator_result
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                patch(f'{_CONTROLLER}.request',
                      new=MagicMock(env=self.env(user=self.admin_user))):
            return self.controller._get_service_revisions(service)

    def test_revisions_listing_reports_the_history_limit(self):
        """INFRA-FIX-018-1／AI-HER-003-1：operator 回報的 helm --history-max 原樣交給對話框。"""
        result = self._listing({'revisions': AUDIT_HISTORY, 'history_max': 20})
        self.assertTrue(result['success'], result)
        self.assertEqual(result['history_max'], 20)

    def test_revisions_listing_from_an_older_operator_has_no_limit(self):
        """舊 operator 沒有 history_max ⇒ None（對話框不顯示那一行），清單照舊。"""
        result = self._listing({'revisions': AUDIT_HISTORY})
        self.assertTrue(result['success'], result)
        self.assertIsNone(result['history_max'])
        self.assertEqual(len(result['data']), 3)

    # ---- 2. password 型設定不跟著回復 ---------------------------------------

    def test_password_specs_join_the_preserved_paths(self):
        service = self._service(self._od_template('rb1526-od'), 'svc-rb1526-od')
        client = self._client()

        response = self._rollback(service, client, 2)

        self.assertTrue(response.get('success'), response)
        kwargs = client.rollback_release.call_args.kwargs
        self.assertEqual(kwargs['preserve_value_paths'], [
            'config.sensitive', USERNAME_PATH,
            'auth.anthropicApiKey', 'auth.openrouterApiKey'],
            '管理員帳密＋兩把 API 金鑰；text 欄位與 _init.* 不列入')
        self.assertEqual(kwargs['chart'], 'oci://example/rb1526-od')

    def test_password_only_template_switches_to_the_preserving_rollback(self):
        """n8n 形：沒有 admin GUI、只有 password knob（SMTP 密碼）⇒ 也要保留。"""
        template = self._template('rb1526-n8n', helm_value_specs=json.dumps({
            'required': [{'key': '_init.owner_password', 'label': 'Owner',
                          'type': 'password'}],
            'optional': [{'key': 'main.secret.N8N_SMTP_PASS', 'label': 'SMTP',
                          'type': 'password'}],
        }))
        service = self._service(template, 'svc-rb1526-n8n')
        client = self._client()

        self._rollback(service, client, 2)

        kwargs = client.rollback_release.call_args.kwargs
        self.assertEqual(kwargs['preserve_value_paths'], ['main.secret.N8N_SMTP_PASS'])
        self.assertEqual(kwargs['chart'], 'oci://example/rb1526-n8n')

    def test_unparseable_specs_fail_closed(self):
        template = self._od_template('rb1526-bad')
        service = self._service(template, 'svc-rb1526-bad')
        # 繞過 create 的檢查直接寫壞（模擬 seed 出錯）
        self.env.cr.execute(
            "UPDATE woow_paas_platform_cloud_app_template SET helm_value_specs = %s "
            "WHERE id = %s", ('{not json', template.id))
        template.invalidate_recordset(['helm_value_specs'])
        client = self._client()

        response = self._rollback(service, client, 2)

        self.assertFalse(response.get('success'), response)
        client.rollback_release.assert_not_called()

    # ---- 3. DB 與叢集一致：下一次修改配置不翻轉金鑰 ---------------------------

    def test_next_reconfigure_keeps_the_current_key_without_extra_restart(self):
        """回復保留的金鑰就是 DB 那一把 ⇒ 之後「修改配置」送出同一把、不判定為換金鑰。

        修正前的時序（sbx-b #16）：回復讓叢集回到舊金鑰 A、DB 仍是 B ⇒ 下一次修改配置
        把 B 送上去、Pod 又滾一次。現在叢集在回復後仍是 B（保留路徑），這裡驗平台側：
        保留清單含這把金鑰，而之後的 reconfigure 送的也是 DB 的 B、不觸發重啟。
        """
        current_key = 'sk-or-CURRENT-B'
        service = self._service(
            self._od_template('rb1526-sync'), 'svc-rb1526-sync',
            helm_values={'auth': {'openrouterApiKey': current_key},
                         'config': {'model': 'm1'}})
        client = self._client()
        self._rollback(service, client, 2)
        self.assertIn('auth.openrouterApiKey',
                      client.rollback_release.call_args.kwargs['preserve_value_paths'])
        self.assertEqual(service.sudo()._helm_values_dict()['auth']['openrouterApiKey'],
                         current_key, '回復不得改動 DB 裡的金鑰')

        service.write({'state': 'running'})
        upgrade_client = MagicMock()
        upgrade_client.upgrade_release.return_value = {'revision': 6, 'chart_version': '0.1.6'}
        upgrade_client.base_url = 'http://operator.test'
        upgrade_client._session.headers = {'X-API-Key': 'k'}
        fake_registry = MagicMock()
        fake_registry.cursor.return_value.__enter__.return_value = self.env.cr
        fake_registry.cursor.return_value.__exit__.return_value = False

        class _SyncThread:
            def __init__(self, target=None, daemon=None):
                self._target = target

            def start(self):
                self._target()

        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=upgrade_client), \
                patch(f'{_CONTROLLER}.request',
                      new=MagicMock(env=self.env(user=self.admin_user))), \
                patch('odoo.addons.woow_paas_platform.services.paas_operator'
                      '.PaaSOperatorClient', return_value=upgrade_client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread), \
                patch('odoo.registry', return_value=fake_registry):
            result = self.controller._update_service(
                service, values={'config.model': 'm2'}, version=None)

        self.assertTrue(result['success'], result)
        sent = upgrade_client.upgrade_release.call_args.kwargs['values']
        self.assertEqual(sent['auth']['openrouterApiKey'], current_key)
        upgrade_client.restart_release.assert_not_called()
