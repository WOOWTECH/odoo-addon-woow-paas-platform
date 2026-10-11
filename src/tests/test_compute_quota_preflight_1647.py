# -*- coding: utf-8 -*-
"""#1647：升級前檢查工作區的 compute 配額——放不下就不套用，並說清楚差多少。

2026-10-10 正式站 omnigent.woowtech.io：namespace 的 ``limits.cpu`` 上限 12，server 與同
namespace 的 PG 被調成各 8。Deployment 是 ``Recreate``，舊 pod 先被砍、新 pod 被 quota
拒絕，剩 0 個 pod 近 6 小時，平台紀錄一直是 running。

operator 端（extra/paas-operator ``compute_quota.py``）在 helm 套用任何東西之前，用同一份
dry-run manifest 算出這次升級放不放得進 ResourceQuota；確定放不下回 409＋
``workspace_compute_quota_exceeded``。本檔守平台這一半：

* 四條升級路徑都請 operator 檢查（Redeploy、儲存設定、換方案、保留憑證的回復），停權中的
  服務與 kill switch 關閉時不請；
* 被擋下時 DB／state／帳務完全不動，租戶與管理員各自看到帶數字的訊息；
* **只認明說的 409＋error_code**——預檢逾時、連不上、舊 operator、別種錯誤都不是「放不下」，
  儲存設定照舊進背景 upgrade（pitfall ``operator-route-none-conflates-disabled-and-failed``）。

換方案的那一組在 ``test_change_plan_reapply_1158.py``（共用它的 fixture）。

mock namespace（docs/decisions/patterns/unittest-mock-dual-namespace-patch.md）：controller
頂部 import 的 ``get_paas_operator_client``、model 函式內 import 的同名 helper（來源模組）、
背景執行緒函式內 import 的 ``PaaSOperatorClient``——各自 patch。
"""
import contextlib
import json
import logging
import os
import re
import uuid
from unittest.mock import MagicMock, patch

from odoo.exceptions import AccessError, UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import mute_logger
from odoo.tools.translate import PoFileReader

from odoo.addons.woow_paas_platform.controllers.paas import PaasController
from odoo.addons.woow_paas_platform.services.paas_operator import (
    COMPUTE_QUOTA_ERROR_CODE,
    COMPUTE_QUOTA_SYNC_PREFLIGHT_TIMEOUT,
    OPERATOR_BUSY_ERROR_CODE,
    STORAGE_EXPANSION_ERROR_CODE,
    PaaSOperatorAPIError,
    PaaSOperatorConnectionError,
    PaaSOperatorTimeoutError,
)
from ..models.cloud_service import _internal_provision

_CONTROLLER = 'odoo.addons.woow_paas_platform.controllers.paas'
_SERVICES = 'odoo.addons.woow_paas_platform.services.paas_operator'
_MODEL = 'odoo.addons.woow_paas_platform.models.cloud_service'
_MODEL_CLIENT = f'{_SERVICES}.get_paas_operator_client'
_BG_CLIENT = f'{_SERVICES}.PaaSOperatorClient'
_SWITCH = 'woow_paas_platform.compute_quota_precheck_enabled'
_NAMESPACE = 'paas-ws-ab6564da'
# 被擋下的路徑本來就會 log（warning／背景失敗的 error）；測試裡不讓它洗版。
_quiet = mute_logger(_CONTROLLER, _MODEL)


def _shortfall(resource='limits.cpu', phase='steady', **numbers):
    kind, dimension = resource.split('.', 1)
    entry = {
        'resource': resource, 'dimension': dimension, 'kind': kind,
        'phase': phase, 'workload': None,
        'hard': '12', 'used': '12', 'release_current': '4', 'other': '8',
        'required': '8', 'available': '4', 'short_by': '4',
    }
    entry.update(numbers)
    return entry


def _quota_detail(*shortfalls):
    """operator 409 的 ``detail``（事故的數字：上限 12、其他 workload 8、這個服務要 8）。"""
    return {
        'error_code': COMPUTE_QUOTA_ERROR_CODE,
        'namespace': _NAMESPACE, 'release': 'svc-1647',
        'quota': f'{_NAMESPACE}-quota', 'quota_source': 'live',
        'shortfalls': list(shortfalls) if shortfalls else [_shortfall()],
        'workloads': [{'kind': 'Deployment', 'name': 'svc-1647-omnigent',
                       'strategy': 'Recreate', 'replicas': 1,
                       'per_pod': {'limits.cpu': '8', 'limits.memory': '8Gi'}}],
        'consumers': [{'owner': 'StatefulSet/pg', 'pods': 1, 'value': '8'}],
        'message': '升級前檢查發現工作區的資源配額放不下這次變更，已停止升級（沒有套用任何變更）：…',
    }


def _refused(detail=None):
    return PaaSOperatorAPIError(
        message='API error', status_code=409, detail=detail or _quota_detail())


class _NonClosingCursor:
    """背景執行緒的 ``odoo.registry(db).cursor()`` 導回測試 transaction（同 #1552 測試）。"""

    def __init__(self, cr):
        self._cr = cr

    def __enter__(self):
        return self._cr

    def __exit__(self, *exc_info):
        return False


class _SyncThread:

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class _Fixture:
    """一位只有 ``base.group_user`` 的租戶（自建 workspace ⇒ owner）＋一台 running 的服務。

    controller 的測試以**租戶身分**呼叫（``service.with_user(tenant)``、request 也是他）：
    production 的 reconfigure／rollback 就是這個身分，superuser 量不到 ir.rule 與欄位 groups。
    """

    def setUp(self):
        super().setUp()
        login = 'tenant_1647_%s' % uuid.uuid4().hex[:8]
        self.tenant = self.env['res.users'].sudo().create({
            'name': '1647 tenant', 'login': login, 'password': login,
            'email': '%s@test.example' % login,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].with_user(
            self.tenant).create({'name': '1647 Workspace'})
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': '1647 Template',
            'slug': 'compute-quota-1647',
            'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/compute-quota-1647',
            'helm_chart_version': '1.0.0',
            'workload_type': 'container',
            'post_deploy_init_type': 'none',
            'mcp_enabled': False,
        })
        self.service = self._make_service(self.template, 'svc-1647', {
            'config': {'nonSensitive': {'model': 'm1'}}})

    def _make_service(self, template, release, values):
        service = _internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': release,
            'workspace_id': self.workspace.id,
            'template_id': template.id,
            'state': 'running',
            'helm_namespace': _NAMESPACE,
            'helm_release_name': release,
            'helm_values': json.dumps(values),
        })
        service.write({'helm_chart_version': template.helm_chart_version, 'helm_revision': 4})
        return service

    def _reload(self, service=None):
        self.env.invalidate_all()
        service = service or self.service
        return service.browse(service.id)

    @staticmethod
    def _client():
        client = MagicMock()
        client.base_url = 'http://operator.test'
        client._session.headers = {'X-API-Key': 'k'}
        client.get_release.return_value = {'name': 'svc-1647', 'revision': 4}
        client.get_revisions.return_value = {'revisions': []}
        client.upgrade_preflight.return_value = {'ok': True, 'expansions': []}
        client.upgrade_release.return_value = {'revision': 5, 'chart_version': '1.0.0'}
        client.rollback_release.return_value = {'message': 'ok', 'mode': 'rollback'}
        return client

    def _tenant_request(self):
        return patch(f'{_CONTROLLER}.request', new=MagicMock(env=self.env(user=self.tenant)))

    def _update(self, client, service=None, **kwargs):
        """以租戶身分走 ``_update_service``；背景執行緒同步跑、寫回測試 transaction。"""
        service = service or self.service.with_user(self.tenant)
        values = kwargs.pop('values', {})
        fake_registry = MagicMock()
        fake_registry.cursor.return_value = _NonClosingCursor(self.env.cr)
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                self._tenant_request(), \
                patch(_BG_CLIENT, return_value=client), \
                patch(f'{_CONTROLLER}.threading.Thread', _SyncThread), \
                patch('odoo.registry', return_value=fake_registry):
            return PaasController()._update_service(
                service, values=values, version=None, **kwargs)

    def _rollback(self, client):
        with patch(f'{_CONTROLLER}.get_paas_operator_client', return_value=client), \
                self._tenant_request():
            return PaasController()._rollback_service(
                self.service.with_user(self.tenant), revision=3)

    def _redeploy(self, client):
        with patch(_MODEL_CLIENT, return_value=client):
            return self.service.action_redeploy()

    def _suspend(self):
        self.service.sudo().write({'state': 'suspended', 'pre_suspend_state': 'running'})

    def _switch_off(self, value='False'):
        self.env['ir.config_parameter'].sudo().set_param(_SWITCH, value)

    def _alerts(self):
        Monitor = self.env['woow_paas_platform.platform_monitor']
        return self.env['mail.activity'].sudo().search(
            [('summary', '=', Monitor.SUMMARY_COMPUTE_QUOTA_REJECTED)])


# ====================================================================== #
#  訊息                                                                    #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestComputeQuotaMessages1647(_Fixture, TransactionCase):

    def test_tenant_message_says_what_is_needed_and_what_is_short(self):
        message = self.service._compute_quota_error_message(_quota_detail())
        self.assertIn('does not have enough CPU quota', message)
        self.assertIn('the service needs 8 vCPU', message)
        self.assertIn('the workspace limit is 12 vCPU', message)
        self.assertIn('are using 8 vCPU', message)
        self.assertIn('short by 4 vCPU', message)
        self.assertIn('Nothing was changed and the service keeps running', message)
        self.assertIn('contact support to raise the workspace quota', message)

    def test_tenant_message_hides_cluster_names(self):
        """租戶看不到（也不需要）namespace、quota 物件名與其他 workload 的名字。"""
        message = self.service._compute_quota_error_message(_quota_detail())
        for leaked in (_NAMESPACE, '-quota', 'ResourceQuota', 'StatefulSet', 'svc-1647-omnigent'):
            self.assertNotIn(leaked, message)

    def test_tenant_message_in_the_requested_language(self):
        self.env['res.lang']._activate_lang('zh_TW')
        message = self.service._compute_quota_error_message(_quota_detail(), lang='zh_TW')
        self.assertIn('這個工作區的CPU配額不足以套用這次變更', message)
        self.assertIn('這個服務需要 8 vCPU', message)
        self.assertIn('工作區上限是 12 vCPU', message)
        self.assertIn('目前佔用 8 vCPU', message)
        self.assertIn('還差 4 vCPU', message)
        self.assertIn('沒有套用任何變更', message)
        english = self.service._compute_quota_error_message(_quota_detail(), lang='en_US')
        self.assertIn('the service needs 8 vCPU', english)

    def test_rolling_shortfall_explains_the_overlap(self):
        detail = _quota_detail(_shortfall(
            phase='rolling', workload='Deployment/svc-1647-omnigent',
            required='2', used='12', short_by='2'))
        message = self.service._compute_quota_error_message(detail)
        self.assertIn('the old and the new instance run side by side', message)
        self.assertIn('needs 2 vCPU more on top of the 12 vCPU already in use', message)
        self.assertIn('short by 2 vCPU', message)
        self.assertNotIn('svc-1647-omnigent', message)

    def test_memory_amounts_are_shown_in_gib_and_mib(self):
        detail = _quota_detail(_shortfall(
            'limits.memory', required='8Gi', hard='8Gi', other='1.5Gi', short_by='512Mi'))
        message = self.service._compute_quota_error_message(detail)
        self.assertIn('does not have enough memory quota', message)
        self.assertIn('needs 8 GiB', message)
        self.assertIn('are using 1.5 GiB', message)
        self.assertIn('short by 512 MiB', message)
        self.assertNotIn('vCPU', message)

    def test_ephemeral_storage_has_a_readable_name(self):
        detail = _quota_detail(_shortfall(
            'limits.ephemeral-storage', required='8Gi', hard='80Gi', other='76Gi',
            short_by='4Gi'))
        self.assertIn('does not have enough temporary disk space quota',
                      self.service._compute_quota_error_message(detail))

    def test_each_resource_is_explained_once_and_at_most_two(self):
        """平台的 quota 把 limits 與 requests 設成同一個值：兩者一起不夠時，CPU 只講一次。"""
        detail = _quota_detail(
            _shortfall('limits.cpu'), _shortfall('limits.memory', required='8Gi', hard='8Gi',
                                                 other='1Gi', short_by='1Gi'),
            _shortfall('requests.cpu', required='2'),
            _shortfall('limits.ephemeral-storage', required='8Gi'))
        message = self.service._compute_quota_error_message(detail, with_ending=False)
        self.assertEqual(message.count('does not have enough'), 2)
        self.assertEqual(message.count('enough CPU quota'), 1)
        self.assertIn('the service needs 8 vCPU', message, '先出現的 limits.cpu 那一筆')
        self.assertIn('enough memory quota', message)
        self.assertNotIn('temporary disk space', message)

    def test_refusal_without_numbers_still_explains_itself(self):
        detail = dict(_quota_detail(), shortfalls=[])
        message = self.service._compute_quota_error_message(detail)
        self.assertIn('does not have enough CPU or memory quota', message)
        self.assertIn('Nothing was changed', message)

    def _downgrade_detail(self, **numbers):
        """operator 在換方案降級時回的 shortfall：新舊並存放不下、``reason`` 標明是降級。"""
        return dict(_quota_detail(_shortfall(
            'limits.memory', phase='rolling', workload='Deployment/svc-1647-omnigent',
            reason='rolling_on_downgrade', required='2Gi', used='8Gi', hard='8Gi',
            short_by='2Gi', other='0Mi', **numbers)), quota_source='pending')

    def test_downgrade_shortfall_does_not_tell_the_tenant_to_delete_services(self):
        """降到較小方案、更換服務時新舊並存才放不下：原因不是「其他服務佔太多」。

        一般的結尾（停用或刪除不需要的服務、請客服調高配額）對降級是錯的指引——單一服務
        的工作區沒有東西可刪，而調高配額與降級相反。租戶自己沒有出口：請他聯絡客服。
        """
        message = self.service._compute_quota_error_message(
            self._downgrade_detail(), operation='change_plan')
        self.assertIn('The memory quota of the new plan cannot hold this service while it is '
                      'being replaced', message)
        self.assertIn('needs 2 GiB more on top of the 8 GiB already in use', message)
        self.assertIn('the workspace limit under the new plan would be 8 GiB', message)
        self.assertIn('short by 2 GiB', message)
        self.assertIn('The plan was not changed and nothing was charged. Please contact '
                      'support to switch to this plan.', message)
        for wrong in ('Stop or delete services', 'raise the workspace quota',
                      'does not have enough'):
            self.assertNotIn(wrong, message)
        for leaked in (_NAMESPACE, 'ResourceQuota', 'svc-1647-omnigent', 'rolling_on_downgrade'):
            self.assertNotIn(leaked, message)

    def test_downgrade_message_in_traditional_chinese(self):
        self.env['res.lang']._activate_lang('zh_TW')
        message = self.service._compute_quota_error_message(
            self._downgrade_detail(), operation='change_plan', lang='zh_TW')
        self.assertIn('新方案的記憶體配額放不下更換中的這個服務', message)
        self.assertIn('換到新方案後工作區上限會是 8 GiB', message)
        self.assertIn('還差 2 GiB', message)
        self.assertIn('方案沒有變更，也沒有扣款。請聯絡客服協助更換到這個方案。', message)
        self.assertNotIn('請停用或刪除', message)

    def test_downgrade_ending_only_when_every_shortfall_is_a_downgrade(self):
        """同時還有一筆一般的放不下（其他 workload 佔著）：那一筆的出口仍是釋放資源。"""
        detail = self._downgrade_detail()
        detail['shortfalls'].insert(0, _shortfall('limits.cpu'))
        message = self.service._compute_quota_error_message(detail, operation='change_plan')
        self.assertIn('does not have enough CPU quota', message)
        self.assertIn('The memory quota of the new plan cannot hold this service', message)
        self.assertIn('Stop or delete services you no longer need', message)
        self.assertNotIn('contact support to switch to this plan', message)

    def test_rolling_shortfall_without_the_reason_keeps_the_ordinary_wording(self):
        """沒有 ``reason``（上限沒有變小）或 reason 掛在別種 phase 上：照一般的說明。"""
        plain = self._downgrade_detail()
        del plain['shortfalls'][0]['reason']
        message = self.service._compute_quota_error_message(plain, operation='change_plan')
        self.assertIn('does not have enough memory quota', message)
        self.assertIn('Stop or delete services you no longer need', message)

        steady = dict(_quota_detail(_shortfall(reason='rolling_on_downgrade')))
        message = self.service._compute_quota_error_message(steady, operation='change_plan')
        self.assertIn('does not have enough CPU quota', message)
        self.assertNotIn('contact support to switch to this plan', message)

    def test_change_plan_ending_talks_about_the_charge(self):
        message = self.service._compute_quota_error_message(
            _quota_detail(), operation='change_plan')
        self.assertIn('The plan was not changed and nothing was charged.', message)
        self.assertNotIn('Nothing was changed and the service keeps running', message)

    def test_without_ending_only_the_resource_sentences_remain(self):
        message = self.service._compute_quota_error_message(_quota_detail(), with_ending=False)
        self.assertTrue(message.endswith('(short by 4 vCPU).'), message)
        self.assertNotIn('contact support', message)

    def test_other_errors_are_not_this_message(self):
        for detail in (
            None, '', 'Helm 升級失敗：找不到指定版本的 chart',
            {'error_code': STORAGE_EXPANSION_ERROR_CODE, 'reason': 'capacity'},
            {'error_code': OPERATOR_BUSY_ERROR_CODE, 'message': '部署服務忙碌'},
            {'message': 'no code'}, ['list'],
        ):
            with self.subTest(detail=detail):
                self.assertIsNone(self.service._compute_quota_error_message(detail))
                self.assertIsNone(self.service._compute_quota_error_message(
                    detail, audience='admin', operation='redeploy'))

    def test_admin_message_has_the_cluster_facts(self):
        message = self.service._compute_quota_error_message(
            _quota_detail(), audience='admin', operation='redeploy')
        self.assertIn('The redeploy was stopped before anything was applied', message)
        self.assertIn(
            'limits.cpu: needs 8 vCPU, limit 12 vCPU, in use 12 vCPU (this service 4 vCPU, '
            'other workloads 8 vCPU), short by 4 vCPU', message)
        self.assertIn('Namespace %s, ResourceQuota %s-quota.' % (_NAMESPACE, _NAMESPACE), message)
        self.assertIn('Largest other consumers: StatefulSet/pg (8).', message)
        self.assertIn('Raise the workspace quota or free resources', message)

    def test_admin_message_does_not_name_the_system_parameter(self):
        """開關的名字只進 log 與 README。排隊的 redeploy 已改寫租戶版（見
        ``test_queued_redeploy_refusal_shows_the_tenant_message``）；這條是縱深——管理員版
        萬一又被帶到租戶面前，至少不洩漏平台的設定鍵。"""
        message = self.service._compute_quota_error_message(
            _quota_detail(), audience='admin', operation='redeploy')
        self.assertNotIn('compute_quota_precheck_enabled', message)
        self.assertNotIn('system parameter', message.lower())

    def test_background_failure_message_uses_the_creators_language(self):
        """背景 PATCH 才被擋的後備路徑：背景執行緒沒有語言，照服務建立者的語言組。"""
        english = self.service._upgrade_failure_message(_refused())
        self.assertIn('does not have enough CPU quota', english)
        self.assertIn('short by 4 vCPU', english)
        self.assertIn('Submit the configuration again to retry', english)
        self.assertNotIn('Nothing was changed', english, '包裝句已經有出口，不重複結尾')

        self.env['res.lang']._activate_lang('zh_TW')
        self.service.create_uid.sudo().lang = 'zh_TW'
        chinese = self.service._upgrade_failure_message(_refused())
        self.assertIn('配額不足以套用這次變更', chinese)
        self.assertIn('還差 4 vCPU', chinese)


# ====================================================================== #
#  Redeploy（後台，管理員）                                                #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestRedeployComputeQuotaPreflight1647(_Fixture, TransactionCase):

    def test_redeploy_asks_the_operator_to_check(self):
        client = self._client()
        self._redeploy(client)
        self.assertIs(client.upgrade_release.call_args.kwargs['check_compute_quota'], True)

    def test_suspended_service_is_not_checked(self):
        """停權中的服務 upgrade 後會被 scale 回 0：manifest 的 replicas 不是它真正的需求。"""
        self._suspend()
        client = self._client()
        self._redeploy(client)
        self.assertIs(client.upgrade_release.call_args.kwargs['check_compute_quota'], False)

    def test_switch_off_stops_asking(self):
        self._switch_off()
        client = self._client()
        self._redeploy(client)
        self.assertIs(client.upgrade_release.call_args.kwargs['check_compute_quota'], False)

    @_quiet
    def test_refusal_is_a_user_error_and_nothing_is_recorded(self):
        client = self._client()
        client.upgrade_release.side_effect = _refused()
        before = (self.service.state, self.service.helm_revision, self.service.last_upgraded_at,
                  self.service.error_message)
        # 裸 try/except：assertRaises 會把區塊包進 savepoint 回捲，之後量到的是區塊之前的
        # 世界（pitfall odoo-assertraises-savepoint-rollback），證明不了「沒有寫入」。
        try:
            self._redeploy(client)
        except UserError as exc:
            message = str(exc.args[0])
        else:
            self.fail('operator 回 409 workspace_compute_quota_exceeded 時 Redeploy 必須中止')
        self.assertIn('The redeploy was stopped before anything was applied', message)
        self.assertIn('limits.cpu: needs 8 vCPU, limit 12 vCPU', message)
        self.assertIn('short by 4 vCPU', message)
        self.assertIn(_NAMESPACE, message)
        self.assertNotIn('compute_quota_precheck_enabled', message)
        client.upgrade_release.assert_called_once()
        svc = self._reload()
        self.assertEqual(
            (svc.state, svc.helm_revision, svc.last_upgraded_at, svc.error_message), before)
        self.assertFalse(self._alerts(), '管理員已經在對話框看到數字，不另發 activity')

    @_quiet
    def test_refusal_stays_a_plain_user_error_and_carries_the_operator_detail(self):
        """Odoo 18 的 web client 以例外的**完整類別名稱**挑對話框（``error_dialogs.js`` 只登記
        ``odoo.exceptions.UserError`` 等幾個名字）：換成 ``UserError`` 的子類，管理員看到的
        會是帶 traceback 的「Odoo Server Error」。所以型別不變，operator 的 detail 掛在例外上
        給不該轉述管理員版的呼叫端用。"""
        client = self._client()
        client.upgrade_release.side_effect = _refused()
        try:
            self._redeploy(client)
        except UserError as exc:
            error = exc
        else:
            self.fail('前提：Redeploy 被擋下')
        self.assertIs(type(error), UserError)
        self.assertEqual(error.compute_quota_detail, _quota_detail())

    @contextlib.contextmanager
    def _cron_boundaries(self):
        """``_cron_run_requested_redeploys`` 自己 commit／rollback，而 TransactionCase 的 cursor
        兩者都不准呼叫 ⇒ 以 savepoint 模擬同樣的邊界：commit 之後的 rollback 回不到更早，
        rollback 丟掉上一次 commit 之後的寫入（含還沒 flush 的）。"""
        cr = self.env.cr
        cr.execute('SAVEPOINT cron_1647')

        def commit():
            cr.flush()
            cr.execute('RELEASE SAVEPOINT cron_1647')
            cr.execute('SAVEPOINT cron_1647')

        def rollback():
            cr.clear()
            cr.execute('ROLLBACK TO SAVEPOINT cron_1647')

        with patch.object(cr, 'commit', commit), patch.object(cr, 'rollback', rollback):
            yield
        cr.execute('RELEASE SAVEPOINT cron_1647')

    def _run_queued_redeploy(self, client):
        """管理員在 init lease 期間按了 Redeploy（已登記意圖），lease 結束後由 cron 執行。"""
        self.service.sudo().write({'init_redeploy_requested': True})
        with patch(_MODEL_CLIENT, return_value=client), self._cron_boundaries():
            self.env['woow_paas_platform.cloud_service']._cron_run_requested_redeploys()
        return self._reload()

    def test_queued_redeploy_refusal_shows_the_tenant_message(self):
        """排隊的 Redeploy 被擋下時原因寫進 ``error_message``——那一欄租戶在服務頁看得到。

        ``_redeploy`` 的 UserError 是管理員版（namespace、ResourceQuota 名、佔用最多的其他
        workload）：它只能進 log 與管理員 activity，``error_message`` 要是租戶版。
        """
        client = self._client()
        client.upgrade_release.side_effect = _refused()
        before = (self.service.state, self.service.helm_revision, self.service.last_upgraded_at)
        with self.assertLogs(_MODEL, level=logging.ERROR) as captured:
            svc = self._run_queued_redeploy(client)

        client.upgrade_release.assert_called_once()
        message = svc.error_message
        self.assertIn('The queued redeploy did not run:', message)
        self.assertIn('does not have enough CPU quota', message)
        self.assertIn('the service needs 8 vCPU', message)
        self.assertIn('short by 4 vCPU', message)
        self.assertIn('Nothing was changed and the service keeps running', message)
        for leaked in (_NAMESPACE, 'Namespace', 'ResourceQuota', '-quota', 'StatefulSet',
                       'Largest other consumers', 'limits.cpu', 'svc-1647-omnigent',
                       'compute_quota_precheck_enabled', 'The redeploy was stopped'):
            self.assertNotIn(leaked, message)
        self.assertEqual((svc.state, svc.helm_revision, svc.last_upgraded_at), before)
        self.assertFalse(svc.init_redeploy_requested, '被拒的意圖清掉，不每分鐘重撞')

        # 管理員版沒有消失：在 log 裡……
        log = '\n'.join(captured.output)
        self.assertIn('Namespace %s, ResourceQuota %s-quota.' % (_NAMESPACE, _NAMESPACE), log)
        self.assertIn('StatefulSet/pg (8)', log)
        # ……也在管理員的 activity 裡（登記意圖的管理員此刻不在對話框前）。
        activities = self._alerts()
        self.assertTrue(activities)
        note = str(activities[0].note)
        self.assertIn('id=%s' % self.service.id, note)
        self.assertIn('queued_redeploy', note)
        self.assertIn(_NAMESPACE, note)
        self.assertIn('StatefulSet/pg (8)', note)

    @_quiet
    def test_queued_redeploy_message_is_in_the_creators_language(self):
        """cron 沒有租戶的語言：整句（含開頭）照服務建立者的語言組。"""
        self.env['res.lang']._activate_lang('zh_TW')
        self.service.create_uid.sudo().lang = 'zh_TW'
        client = self._client()
        client.upgrade_release.side_effect = _refused()
        svc = self._run_queued_redeploy(client)
        self.assertIn('排定的重新部署沒有執行：', svc.error_message)
        self.assertIn('這個工作區的CPU配額不足以套用這次變更', svc.error_message)
        self.assertIn('還差 4 vCPU', svc.error_message)
        self.assertNotIn(_NAMESPACE, svc.error_message)

    @_quiet
    def test_other_queued_redeploy_refusals_keep_their_message(self):
        """別種拒絕照舊原樣寫進 error_message，也不開配額告警。"""
        client = self._client()
        client.upgrade_release.side_effect = PaaSOperatorAPIError(
            message='API error', status_code=500, detail='Helm 升級失敗：找不到指定版本的 chart')
        svc = self._run_queued_redeploy(client)
        self.assertEqual(
            svc.error_message,
            'The queued redeploy did not run: Upgrade failed: Helm 升級失敗：找不到指定版本的 chart')
        self.assertFalse(self._alerts())

    def test_refusal_log_names_the_kill_switch(self):
        """開關的名字不進訊息（上一條），但管理員誤判時要找得到——寫在 log。"""
        client = self._client()
        client.upgrade_release.side_effect = _refused()
        with self.assertLogs(_MODEL, level=logging.WARNING) as captured:
            try:
                self._redeploy(client)
            except UserError:
                pass
            else:
                self.fail('前提：Redeploy 被擋下')
        self.assertIn(_SWITCH, '\n'.join(captured.output))

    def test_other_operator_errors_keep_their_message(self):
        for detail, expected in (
            ('Helm 升級失敗：找不到指定版本的 chart', 'Upgrade failed: Helm 升級失敗：找不到指定版本的 chart'),
            ({'error_code': OPERATOR_BUSY_ERROR_CODE, 'message': '部署服務忙碌'},
             'Upgrade failed: 部署服務忙碌'),
        ):
            with self.subTest(detail=detail):
                client = self._client()
                client.upgrade_release.side_effect = PaaSOperatorAPIError(
                    message='API error', status_code=500, detail=detail)
                try:
                    self._redeploy(client)
                except UserError as exc:
                    self.assertEqual(str(exc.args[0]), expected)
                else:
                    self.fail('operator 失敗時 Redeploy 必須中止')

    def test_tenant_still_cannot_redeploy(self):
        """陽性對照的反面：加了預檢不代表放寬入口——有寫入權的租戶仍被 group gate 擋下。"""
        client = self._client()
        with patch(_MODEL_CLIENT, return_value=client), self.assertRaises(AccessError):
            self.service.with_user(self.tenant).action_redeploy()
        client.get_release.assert_not_called()
        client.upgrade_release.assert_not_called()


# ====================================================================== #
#  儲存設定（reconfigure，租戶）                                           #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestReconfigureComputeQuotaPreflight1647(_Fixture, TransactionCase):

    def test_preflight_and_background_upgrade_get_the_same_arguments(self):
        client = self._client()
        result = self._update(client, values={})
        self.assertTrue(result.get('success'), result)
        client.upgrade_preflight.assert_called_once()
        client.upgrade_release.assert_called_once()
        pre = dict(client.upgrade_preflight.call_args.kwargs)
        self.assertEqual(pre.pop('timeout'), COMPUTE_QUOTA_SYNC_PREFLIGHT_TIMEOUT)
        self.assertEqual(COMPUTE_QUOTA_SYNC_PREFLIGHT_TIMEOUT, 15,
                         '前端這條 RPC 的預算是 30 秒，同步預檢不得吃掉大半')
        self.assertEqual(pre, client.upgrade_release.call_args.kwargs,
                         '預檢與真正的 upgrade 必須是同一組參數，否則預檢沒有意義')
        self.assertIs(pre['check_compute_quota'], True)
        self.assertNotIn('pending_quota', pre, '儲存設定不改 ns quota')
        self.assertTrue(pre['reset_values'])
        self.assertEqual(pre['version'], '1.0.0')
        names = [c[0] for c in client.method_calls
                 if c[0] in ('upgrade_preflight', 'upgrade_release')]
        self.assertEqual(names, ['upgrade_preflight', 'upgrade_release'])
        self.assertEqual(self._reload().state, 'upgrading')

    @_quiet
    def test_refusal_changes_nothing(self):
        client = self._client()
        client.upgrade_preflight.side_effect = _refused()
        before = self._reload()
        snapshot = (before.state, before.sudo().helm_values, before.error_message,
                    before.last_upgraded_at, before.helm_revision, before.init_generation,
                    before.upgrade_failed_at)

        result = self._update(client, values={})

        self.assertFalse(result.get('success'), result)
        self.assertEqual(result['data']['code'], COMPUTE_QUOTA_ERROR_CODE)
        self.assertEqual(result['data']['shortfalls'], _quota_detail()['shortfalls'])
        self.assertIn('does not have enough CPU quota', result['error'])
        self.assertIn('short by 4 vCPU', result['error'])
        self.assertIn('Nothing was changed and the service keeps running', result['error'])
        self.assertNotIn(_NAMESPACE, result['error'])
        client.upgrade_release.assert_not_called()
        after = self._reload()
        self.assertEqual(
            (after.state, after.sudo().helm_values, after.error_message, after.last_upgraded_at,
             after.helm_revision, after.init_generation, after.upgrade_failed_at), snapshot)
        self.assertEqual(after.state, 'running')

    @_quiet
    def test_refusal_restores_the_addon_selection(self):
        """改 addon 的 reconfigure 會先寫 odoo_addon_ids：被擋下時要還原，不留半套變更。"""
        odoo_template = self.env['woow_paas_platform.cloud_app_template'].search(
            [('slug', '=', 'odoo')], limit=1)
        self.assertTrue(odoo_template, '前提：seed 的 odoo template 存在（addon 分支只認 slug）')
        service = self._make_service(
            odoo_template, 'svc-1647-odoo', json.loads(odoo_template.helm_default_values or '{}'))
        addon = self.env['woow_paas_platform.cloud_odoo_addon'].sudo().create({
            'name': '1647 addon', 'technical_name': 'addon_1647_probe',
            'source_type': 'third_party'})
        generation = service.init_generation
        client = self._client()
        client.upgrade_preflight.side_effect = _refused()

        result = self._update(client, service=service, odoo_addon_ids=[addon.id])

        self.assertFalse(result.get('success'), result)
        self.assertEqual(result['data']['code'], COMPUTE_QUOTA_ERROR_CODE)
        # 預檢送出去的 values 已經帶著新勾選（預檢看的是「這次要套用的」）……
        sent = client.upgrade_preflight.call_args.kwargs['values']
        self.assertEqual(sent['addons']['selected'], ['addon_1647_probe'])
        # ……被擋下之後 DB 回到原樣。
        after = self._reload(service)
        self.assertFalse(after.odoo_addon_ids)
        self.assertEqual(after.state, 'running')
        self.assertEqual(after.init_generation, generation, '沒有開新一代 init')
        self.assertFalse(after.addon_update_pending)
        client.upgrade_release.assert_not_called()

    @_quiet
    def test_inconclusive_preflight_still_upgrades(self):
        """只認明說的 409＋error_code。其他一切都不是「配額放不下」，照舊進背景 upgrade。"""
        cases = {
            'old operator 404': PaaSOperatorAPIError('API error', status_code=404, detail='Not Found'),
            'old operator 405': PaaSOperatorAPIError('API error', status_code=405,
                                                     detail='Method Not Allowed'),
            'render failure 500': PaaSOperatorAPIError('API error', status_code=500,
                                                       detail='Helm 升級失敗：…'),
            'busy 503': PaaSOperatorAPIError(
                'API error', status_code=503,
                detail={'error_code': OPERATOR_BUSY_ERROR_CODE, 'message': '部署服務忙碌'}),
            'immutable field 409': PaaSOperatorAPIError(
                'API error', status_code=409, detail='升級前檢查發現下列欄位建立後不能修改…'),
            'disk expansion 409': PaaSOperatorAPIError(
                'API error', status_code=409,
                detail={'error_code': STORAGE_EXPANSION_ERROR_CODE, 'reason': 'capacity'}),
            'quota code on a non-409': PaaSOperatorAPIError(
                'API error', status_code=500, detail=_quota_detail()),
            'timeout': PaaSOperatorTimeoutError('Request to PaaS Operator timed out'),
            'unreachable': PaaSOperatorConnectionError('Failed to connect to PaaS Operator'),
        }
        for label, exc in cases.items():
            with self.subTest(case=label):
                self.service.sudo().write({'state': 'running'})
                client = self._client()
                client.upgrade_preflight.side_effect = exc
                result = self._update(client, values={})
                self.assertTrue(result.get('success'), result)
                client.upgrade_preflight.assert_called_once()
                client.upgrade_release.assert_called_once()
                self.assertEqual(self._reload().state, 'upgrading')
                self.assertFalse(self._alerts())

    def test_suspended_service_is_not_prechecked(self):
        self._suspend()
        client = self._client()
        result = self._update(client, values={})
        self.assertTrue(result.get('success'), result)
        client.upgrade_preflight.assert_not_called()
        self.assertIs(client.upgrade_release.call_args.kwargs['check_compute_quota'], False)
        self.assertEqual(self._reload().state, 'suspended')

    def test_switch_off_skips_the_precheck(self):
        for value in ('False', '0', 'off'):
            with self.subTest(value=value):
                self.service.sudo().write({'state': 'running'})
                self._switch_off(value)
                client = self._client()
                result = self._update(client, values={})
                self.assertTrue(result.get('success'), result)
                client.upgrade_preflight.assert_not_called()
                self.assertIs(
                    client.upgrade_release.call_args.kwargs['check_compute_quota'], False)

    @_quiet
    def test_refusal_that_only_shows_up_in_the_background_is_reported(self):
        """後備路徑：同步預檢通過（或沒問到），背景的 PATCH 才被 operator 擋下。"""
        client = self._client()
        client.upgrade_release.side_effect = _refused()
        result = self._update(client, values={})
        self.assertTrue(result.get('success'), result)
        svc = self._reload()
        self.assertEqual(svc.state, 'error')
        self.assertTrue(svc.upgrade_failed_at, '出口＝重新送出修改配置（#1552）')
        self.assertIn('does not have enough CPU quota', svc.error_message)
        self.assertIn('short by 4 vCPU', svc.error_message)
        self.assertIn('Submit the configuration again to retry', svc.error_message)
        self.assertNotIn(_NAMESPACE, svc.error_message)


# ====================================================================== #
#  回復版本（rollback，租戶）                                              #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestRollbackComputeQuotaPreflight1647(_Fixture, TransactionCase):

    def test_rollback_asks_the_operator_to_check(self):
        client = self._client()
        result = self._rollback(client)
        self.assertTrue(result.get('success'), result)
        self.assertIs(client.rollback_release.call_args.kwargs['check_compute_quota'], True)
        self.assertEqual(self._reload().state, 'upgrading')

    def test_switch_off_stops_asking(self):
        self._switch_off()
        client = self._client()
        self._rollback(client)
        self.assertIs(client.rollback_release.call_args.kwargs['check_compute_quota'], False)

    @_quiet
    def test_refusal_is_explained_and_nothing_is_recorded(self):
        client = self._client()
        client.rollback_release.side_effect = _refused()
        result = self._rollback(client)
        self.assertFalse(result.get('success'), result)
        self.assertEqual(result['data']['code'], COMPUTE_QUOTA_ERROR_CODE)
        self.assertEqual(result['data']['shortfalls'], _quota_detail()['shortfalls'])
        self.assertIn('does not have enough CPU quota', result['error'])
        self.assertIn('Nothing was changed and the service keeps running', result['error'])
        self.assertNotIn('Rollback failed', result['error'])
        svc = self._reload()
        self.assertEqual(svc.state, 'running')
        self.assertFalse(svc.last_upgraded_at)
        self.assertFalse(svc.error_message)
        [activity] = self._alerts()[:1]
        self.assertIn('rollback', activity.note)

    @_quiet
    def test_structured_detail_is_not_printed_raw(self):
        """dict 形的 detail（例 503 operator_busy）取它的 message，不把整個 dict 印給租戶。"""
        client = self._client()
        client.rollback_release.side_effect = PaaSOperatorAPIError(
            'API error', status_code=503,
            detail={'error_code': OPERATOR_BUSY_ERROR_CODE, 'message': '部署服務忙碌，請稍後再試'})
        result = self._rollback(client)
        self.assertFalse(result.get('success'), result)
        self.assertIn('部署服務忙碌，請稍後再試', result['error'])
        self.assertNotIn('error_code', result['error'])
        self.assertNotIn('{', result['error'])

    @_quiet
    def test_string_detail_is_still_shown(self):
        client = self._client()
        client.rollback_release.side_effect = PaaSOperatorAPIError(
            'API error', status_code=500, detail='Helm 回復失敗：等待資源就緒逾時。')
        result = self._rollback(client)
        self.assertIn('Rollback failed: Helm 回復失敗：等待資源就緒逾時。', result['error'])


# ====================================================================== #
#  kill switch                                                             #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestComputeQuotaPrecheckSwitch1647(_Fixture, TransactionCase):

    def test_unset_means_on(self):
        self.assertFalse(self.env['ir.config_parameter'].sudo().get_param(_SWITCH),
                         '前提：這個 key 沒有預設資料')
        self.assertTrue(self.service._compute_quota_check_enabled())
        self.assertTrue(self.service._compute_quota_check_requested())

    def test_only_explicit_false_values_turn_it_off(self):
        for value, expected in (
            ('false', False), ('False', False), ('0', False), ('no', False), (' OFF ', False),
            ('true', True), ('True', True), ('1', True), ('yes', True),
        ):
            with self.subTest(value=value):
                self._switch_off(value)
                self.assertIs(self.service._compute_quota_check_enabled(), expected)
                self.assertIs(self.service._compute_quota_check_requested(), expected)

    def test_halted_service_is_never_requested(self):
        self._suspend()
        self.assertTrue(self.service._compute_quota_check_enabled())
        self.assertFalse(self.service._compute_quota_check_requested())
        # state 被改寫成別的值（#482 之前的存量、復權失敗寫成 error…）時仍然算停權中。
        self.service.sudo().write({'state': 'upgrading'})
        self.assertFalse(self.service._compute_quota_check_requested())

    def test_key_is_registered_for_uninstall(self):
        """「模組常數＋get_param()」寫法 repo-lint 掃不到——漏登記時卸載後留孤兒紀錄。"""
        from ..hooks import _CONFIG_PARAMETERS
        from ..models.cloud_service import _COMPUTE_QUOTA_PRECHECK_PARAM

        self.assertEqual(_COMPUTE_QUOTA_PRECHECK_PARAM, _SWITCH)
        self.assertIn(_SWITCH, _CONFIG_PARAMETERS)


@tagged('post_install', '-at_install')
class TestOperatorClientComputeQuotaWire1647(TransactionCase):
    """送給 operator 的 body：旗標只在要求檢查時出現。

    發版順序是 operator 先、平台後，但反過來（新平台＋舊 operator）也要安全：不檢查時 body
    與加入本功能前逐鍵相同；「平台不送旗標」（停權、kill switch）指的就是這一格。
    """

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.services.paas_operator import PaaSOperatorClient
        self.client = PaaSOperatorClient('http://operator.test', 'k')

    def _body(self, method, *args, **kwargs):
        with patch.object(self.client, '_request', return_value={}) as request:
            getattr(self.client, method)(*args, **kwargs)
        return request.call_args.kwargs

    def test_upgrade_body_only_carries_the_flag_when_asked(self):
        plain = self._body('upgrade_release', 'ns', 'svc', values={'a': 1}, reset_values=True)
        self.assertEqual(plain['data'], {
            'reset_values': True, 'reuse_values': False, 'values': {'a': 1}})
        off = self._body('upgrade_release', 'ns', 'svc', values={'a': 1}, reset_values=True,
                         check_compute_quota=False)
        self.assertEqual(off['data'], plain['data'])
        on = self._body('upgrade_release', 'ns', 'svc', values={'a': 1}, reset_values=True,
                        check_compute_quota=True)
        self.assertEqual(on['data'], dict(plain['data'], check_compute_quota=True))

    def test_preflight_body_carries_flag_pending_quota_and_timeout(self):
        plain = self._body('upgrade_preflight', 'ns', 'svc', reset_values=True)
        self.assertEqual(plain['data'], {'reset_values': True, 'reuse_values': False})
        pending = {'cpu_limit': '24', 'memory_limit': '48Gi', 'storage_limit': '100Gi'}
        asked = self._body('upgrade_preflight', 'ns', 'svc', reset_values=True,
                           check_compute_quota=True, pending_quota=pending, timeout=15)
        self.assertEqual(asked['data'], {
            'reset_values': True, 'reuse_values': False,
            'check_compute_quota': True, 'pending_quota': pending})
        self.assertEqual(asked['timeout'], 15)
        self.assertGreater(plain['timeout'], asked['timeout'], '不給 timeout＝一般 helm 操作的預算')

    def test_rollback_body_only_carries_the_flag_when_asked(self):
        plain = self._body('rollback_release', 'ns', 'svc', revision=3)
        self.assertEqual(plain['data'], {'revision': 3})
        asked = self._body('rollback_release', 'ns', 'svc', revision=3, chart='oci://x/c',
                           preserve_value_paths=['config.sensitive'], check_compute_quota=True)
        self.assertEqual(asked['data'], {
            'revision': 3, 'chart': 'oci://x/c', 'preserve_value_paths': ['config.sensitive'],
            'check_compute_quota': True})


# ====================================================================== #
#  ns quota 的預演（換方案扣款前要交給預檢的「將推送的 quota」）            #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestComputeNsQuotaOverrides1647(_Fixture, TransactionCase):

    #: operator ``NamespaceQuotaUpdateRequest``（extra/paas-operator ``src/models/schemas.py``）
    #: 的欄位；``pending_quota`` 以它解析，鍵對不上 operator 會回 422。
    QUOTA_FIELDS = {'cpu_limit', 'memory_limit', 'storage_limit',
                    'ephemeral_storage_request', 'ephemeral_storage_limit'}

    def setUp(self):
        super().setUp()
        self.Svc = self.env['woow_paas_platform.cloud_service']
        self.service.sudo().write(
            {'allocated_vcpu': 4, 'allocated_ram_gb': 8.0, 'allocated_storage_gb': 40})
        self.other = self._make_service(self.template, 'svc-1647-other', {})
        self.other.sudo().write(
            {'allocated_vcpu': 2, 'allocated_ram_gb': 4.0, 'allocated_storage_gb': 20})

    def test_without_overrides_nothing_changes(self):
        baseline = self.Svc._compute_ns_quota(self.workspace)
        self.assertEqual(self.Svc._compute_ns_quota(self.workspace, allocated_overrides=None),
                         baseline)
        self.assertEqual(self.Svc._compute_ns_quota(self.workspace, allocated_overrides={}),
                         baseline)
        self.assertEqual(
            self.Svc._compute_ns_quota(self.workspace, allocated_overrides={self.service.id: {}}),
            baseline)

    def test_override_predicts_the_quota_after_the_write(self):
        """預演＝「寫進 DB 之後會算出來的值」。⛔ 不在測試裡重寫倍數：改問 production 兩次。"""
        change = {'allocated_vcpu': 8, 'allocated_ram_gb': 16.0}
        baseline = self.Svc._compute_ns_quota(self.workspace)
        predicted = self.Svc._compute_ns_quota(
            self.workspace, allocated_overrides={self.service.id: change})
        self.assertNotEqual(predicted, baseline, '前提：這個變更大到會改變 quota')
        self.assertEqual(predicted[2], baseline[2], '沒給的欄位（storage）沿用 DB 的值')

        self.env.invalidate_all()
        self.assertEqual(
            (self.service.allocated_vcpu, self.service.allocated_ram_gb), (4, 8.0),
            '預演只在記憶體內，不寫 DB')
        self.assertEqual(self.Svc._compute_ns_quota(self.workspace), baseline)

        self.service.sudo().write(change)
        self.assertEqual(self.Svc._compute_ns_quota(self.workspace), predicted)

    def test_override_only_touches_the_named_service(self):
        change = {'allocated_vcpu': 8}
        mine = self.Svc._compute_ns_quota(
            self.workspace, allocated_overrides={self.service.id: change})
        theirs = self.Svc._compute_ns_quota(
            self.workspace, allocated_overrides={self.other.id: change})
        self.assertNotEqual(mine, theirs, '同一個值套在不同服務上，總和不同')
        unknown = self.Svc._compute_ns_quota(
            self.workspace, allocated_overrides={-1: {'allocated_vcpu': 999}})
        self.assertEqual(unknown, self.Svc._compute_ns_quota(self.workspace),
                         '不在這個 workspace 的服務 id 不影響結果')

    def test_payload_has_exactly_the_operator_fields(self):
        payload = self.Svc._ns_quota_payload(self.workspace)
        self.assertEqual(set(payload), self.QUOTA_FIELDS)
        cpu, memory, storage = self.Svc._compute_ns_quota(self.workspace)
        eph_request, eph_limit = self.Svc._compute_ns_ephemeral_quota(self.workspace)
        self.assertEqual(payload, {
            'cpu_limit': cpu, 'memory_limit': memory, 'storage_limit': storage,
            'ephemeral_storage_request': eph_request, 'ephemeral_storage_limit': eph_limit})
        self.assertTrue(all(isinstance(v, str) and v for v in payload.values()), payload)

    def test_payload_is_what_push_ns_quota_sends(self):
        """預檢看到的 pending quota 與之後真正推送的是同一組值。"""
        client = self._client()
        with patch(_MODEL_CLIENT, return_value=client):
            self.Svc._push_ns_quota(self.workspace, _NAMESPACE)
        args, kwargs = client.update_namespace_quota.call_args
        payload = self.Svc._ns_quota_payload(self.workspace)
        self.assertEqual(args, (_NAMESPACE, payload['cpu_limit'], payload['memory_limit'],
                                payload['storage_limit']))
        self.assertEqual(kwargs, {
            'ephemeral_storage_request': payload['ephemeral_storage_request'],
            'ephemeral_storage_limit': payload['ephemeral_storage_limit']})

    def test_payload_with_overrides_follows_the_override(self):
        change = {'allocated_vcpu': 8, 'allocated_ram_gb': 16.0}
        payload = self.Svc._ns_quota_payload(
            self.workspace, allocated_overrides={self.service.id: change})
        cpu, memory, storage = self.Svc._compute_ns_quota(
            self.workspace, allocated_overrides={self.service.id: change})
        self.assertEqual(
            (payload['cpu_limit'], payload['memory_limit'], payload['storage_limit']),
            (cpu, memory, storage))


# ====================================================================== #
#  管理員告警                                                              #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestComputeQuotaAdminAlert1647(_Fixture, TransactionCase):

    @_quiet
    def test_tenant_refusals_open_one_activity_with_one_line_per_reason(self):
        """租戶自己調不了配額：告警給管理員。同一台同一個原因只佔一行，不洗版。

        被擋下之後服務的 state 不變，租戶可以立刻再按（或用 RPC 重送）「儲存設定」——每按
        一次就追加一行的話，同一張 activity 會被同一句話洗成幾百行。
        """
        self.assertFalse(self._alerts(), '前提：還沒有這張告警')
        client = self._client()
        client.upgrade_preflight.side_effect = _refused()

        self._update(client, values={})
        first = self._alerts()
        self.assertTrue(first, '被擋下的儲存設定要開一張 activity 給管理員')
        marker = 'id=%s' % self.service.id
        note = str(first[0].note)
        self.assertEqual(note.count(marker), 1)
        self.assertIn('reconfigure', note)
        self.assertIn(_NAMESPACE, note)
        self.assertIn('limits.cpu needs 8 / limit 12 / other workloads 8 / short by 4', note)
        self.assertIn('StatefulSet/pg (8)', note)
        self.assertIn('rejected before anything was applied or charged', note)

        for _attempt in range(3):
            self._update(client, values={})
        again = self._alerts()
        self.assertEqual(len(again), len(first), '同 summary 未處理前不重建')
        for activity in again:
            self.assertEqual(str(activity.note).count(marker), 1,
                             '同一台、同一個原因連續被擋 3 次仍然只有 1 行')
        self.assertEqual(client.upgrade_preflight.call_count, 4, '前提：四次都真的被擋下')

        # 原因變了（差的數字不同）才是新的一行。
        client.upgrade_preflight.side_effect = _refused(
            _quota_detail(_shortfall(required='10', short_by='6')))
        self._update(client, values={})
        changed = str(self._alerts()[0].note)
        self.assertEqual(changed.count(marker), 2)
        self.assertIn('limits.cpu needs 10 / limit 12 / other workloads 8 / short by 6', changed)

    def _probe_activities(self, summary):
        return self.env['mail.activity'].sudo().search([('summary', '=', summary)])

    def test_same_line_is_recognised_after_the_note_was_sanitised(self):
        """note 是 HTML、寫入時會被 sanitize（跳脫的寫法會變）：去重比的是純文字。

        ``"``、``<``、``&``、``→`` 都是既有告警行（#1415／#1513）會出現的字元。
        """
        Monitor = self.env['woow_paas_platform.platform_monitor']
        summary = 'PaaS monitor: 1647 dedupe probe'
        line = 'svc-a (id=1, namespace x) → plan "Pro <4 vCPU> & more": data 20Gi→30Gi'
        self.assertTrue(Monitor._alert(summary, 'What happened:', append_line=line))
        for _attempt in range(2):
            self.assertFalse(Monitor._alert(summary, 'What happened:', append_line=line))
        self.assertFalse(Monitor._alert(summary, 'What happened:', append_line='svc-b (id=2)'))
        activities = self._probe_activities(summary)
        self.assertTrue(activities)
        for activity in activities:
            self.assertEqual(Monitor._note_paragraphs(activity.note),
                             ['What happened:', line, 'svc-b (id=2)'])

    def test_appended_lines_are_capped(self):
        """行數有上限：滿了之後只補一行「還有更多」，其餘寫 log（不無聲消失）。"""
        from ..models.platform_monitor import ALERT_MAX_APPENDED_LINES

        Monitor = self.env['woow_paas_platform.platform_monitor']
        summary = 'PaaS monitor: 1647 cap probe'
        total = ALERT_MAX_APPENDED_LINES + 10
        monitor_log = 'odoo.addons.woow_paas_platform.models.platform_monitor'
        with self.assertLogs(monitor_log, level=logging.WARNING) as captured:
            for index in range(total):
                Monitor._alert(summary, 'What happened:', append_line='service %d' % index)
        activities = self._probe_activities(summary)
        self.assertTrue(activities)
        expected = ['service %d' % index for index in range(ALERT_MAX_APPENDED_LINES)]
        for activity in activities:
            paragraphs = Monitor._note_paragraphs(activity.note)
            self.assertEqual(paragraphs[0], 'What happened:')
            self.assertEqual(paragraphs[1:-1], expected)
            self.assertIn('More entries were not listed here', paragraphs[-1])
            self.assertIn(str(ALERT_MAX_APPENDED_LINES), paragraphs[-1])
            self.assertEqual(len(paragraphs), 1 + ALERT_MAX_APPENDED_LINES + 1)
        # 被略過的每一行都留在 log 裡。
        log = '\n'.join(captured.output)
        for index in range(ALERT_MAX_APPENDED_LINES, total):
            self.assertIn('not appended: service %d' % index, log)

    def test_alert_is_raised_with_elevated_rights_from_the_model(self):
        """提權只在 model 的 ``_alert_compute_quota_rejection``；租戶身分呼叫也寫得進去。"""
        self.service.with_user(self.tenant)._alert_compute_quota_rejection(
            'change_plan', _quota_detail())
        [activity] = self._alerts()[:1]
        self.assertIn('change_plan', activity.note)

    def test_alert_failure_does_not_break_the_caller(self):
        Monitor = type(self.env['woow_paas_platform.platform_monitor'])
        with patch.object(Monitor, '_alert', side_effect=RuntimeError('boom')), \
                mute_logger('odoo.addons.woow_paas_platform.models.platform_monitor'):
            self.service._alert_compute_quota_rejection('reconfigure', _quota_detail())
        self.assertFalse(self._alerts())


# ====================================================================== #
#  譯文                                                                    #
# ====================================================================== #

@tagged('post_install', '-at_install')
class TestComputeQuotaTranslations1647(TransactionCase):

    NEW_MESSAGES = (
        'This workspace does not have enough %(resource)s quota for this change: the service '
        'needs %(required)s, the workspace limit is %(hard)s and other workloads in the '
        'workspace are using %(other)s (short by %(short)s).',
        'This workspace does not have enough %(resource)s quota for this change: while the '
        'service is being replaced the old and the new instance run side by side, which needs '
        '%(required)s more on top of the %(used)s already in use; the workspace limit is '
        '%(hard)s (short by %(short)s).',
        'This workspace does not have enough CPU or memory quota for this change.',
        'Nothing was changed and the service keeps running with its current settings. Stop or '
        'delete services you no longer need in this workspace and try again, or contact '
        'support to raise the workspace quota.',
        'The plan was not changed and nothing was charged. Stop or delete services you no '
        'longer need in this workspace and try again, or contact support to raise the '
        'workspace quota.',
        'CPU',
        'memory',
        'temporary disk space',
        'The redeploy was stopped before anything was applied: the workspace quota cannot fit '
        'this service after the upgrade.',
        '%(key)s: needs %(required)s, limit %(hard)s, in use %(used)s (this service %(own)s, '
        'other workloads %(other)s), short by %(short)s',
        'Namespace %(ns)s, ResourceQuota %(quota)s. Largest other consumers: %(consumers)s.',
        'Raise the workspace quota or free resources in the namespace, then redeploy.',
        '%(name)s (id=%(id)s, namespace %(ns)s, %(operation)s): %(shortfalls)s. Largest other '
        'consumers: %(consumers)s.',
        'Changes were rejected before anything was applied or charged because the CPU / '
        'memory quota of the workspace cannot fit the service after the change. Tenants who '
        'requested a change were told to free resources or contact support. Raise the quota '
        'of that workspace or free resources in its namespace, then ask the tenant to try '
        'again (for a queued redeploy: redeploy the service again):',
        # review 之後補的（降級的說明、排隊 redeploy 的租戶版、告警行數上限）。
        'The %(resource)s quota of the new plan cannot hold this service while it is being '
        'replaced: the old and the new instance run side by side for a moment, which needs '
        '%(required)s more on top of the %(used)s already in use, but the workspace limit '
        'under the new plan would be %(hard)s (short by %(short)s).',
        'The plan was not changed and nothing was charged. Please contact support to switch '
        'to this plan.',
        'The queued redeploy did not run: %(reason)s',
        'More entries were not listed here (the limit of %(limit)s lines was reached); see '
        'the server log.',
    )

    def _entries(self):
        po = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'i18n', 'zh_TW.po')
        return {e['src']: e['value'] for e in PoFileReader(po)}

    def test_new_messages_have_traditional_chinese(self):
        entries = self._entries()
        for src in self.NEW_MESSAGES:
            with self.subTest(src=src[:40]):
                self.assertTrue(entries.get(src), 'zh_TW.po 缺翻譯：%r' % src)

    def test_translations_say_quota_consistently(self):
        """這個數字在畫面上一律叫「配額」（ResourceQuota），不混用「額度」。"""
        entries = self._entries()
        for src in self.NEW_MESSAGES:
            with self.subTest(src=src[:40]):
                self.assertNotIn('額度', entries.get(src) or '')
        # 英文講到 quota 的句子（佔位符 %(quota)s 不算），中文都要是「配額」。
        quota_sentences = [
            src for src in self.NEW_MESSAGES if re.search(r'(?<!\()\bquota\b', src)]
        self.assertGreaterEqual(len(quota_sentences), 8)
        for src in quota_sentences:
            with self.subTest(src=src[:40]):
                self.assertIn('配額', entries.get(src) or '')

    def test_placeholders_survive_translation(self):
        entries = self._entries()
        for src in self.NEW_MESSAGES:
            with self.subTest(src=src[:40]):
                self.assertEqual(
                    sorted(re.findall(r'%\(\w+\)s', entries.get(src) or '')),
                    sorted(re.findall(r'%\(\w+\)s', src)))
