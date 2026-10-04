# -*- coding: utf-8 -*-
"""#1415：叢集儲存（Longhorn）排程額度不足時的租戶訊息、管理員告警與定時監控。

operator（``extra/paas-operator/src/services/storage_capacity.py``）在 helm install 前用
同一份 chart／values 渲染出這次要建的磁碟，照 Longhorn 排程規則模擬；確定排不下時回 409、
``detail.error_code == 'cluster_storage_capacity_exhausted'``，什麼都沒建。另有唯讀端點
``GET /api/cluster/storage-capacity`` 回報各節點額度。

守的事：

* 409 ⇒ ``error_message`` 是寫給租戶的說明（平台儲存空間暫時不足、沒有建立任何資源、
  管理員已收到通知、可刪除後稍後再開或聯絡客服；有訂閱時說明刪除會取消訂閱、試用期
  不扣款），依**服務建立者**的語言；同時給系統管理員開 activity（一台一行、未處理前追加）；
* 拒絕路徑與 #1348 一致：服務停在 error、訂閱不動、沒有扣點／發票；
* 其他錯誤（含 #1348 的工作區配額）不誤認成叢集額度、不告警；
* monitor cron：額度低於門檻或節點預訂 > 90% 才告警、附孤兒 PVC 佔用；同 summary 不重開；
  operator 太舊（404）／讀不到 Longhorn ⇒ 安靜略過；
* 新 config_parameter 有登記卸載清理。
"""
import json
import uuid
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import (
    CLUSTER_STORAGE_CAPACITY_ERROR_CODE,
    STORAGE_QUOTA_ERROR_CODE,
    _internal_provision,
)
from ..models.platform_monitor import CLUSTER_STORAGE_MIN_VOLUME_PARAM
from ..services.paas_operator import PaaSOperatorAPIError, PaaSOperatorConnectionError

_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)
GIB = 1024 ** 3

#: 10-04 事件當下的 409 detail（operator 單元測試以正式站 fixture 重算出的同一組數字）。
CAPACITY_DETAIL = {
    'error_code': CLUSTER_STORAGE_CAPACITY_ERROR_CODE,
    'message': 'Cluster storage (Longhorn) cannot schedule svc-1415-nextcloud-data (80Gi x2); '
               'largest new volume that still fits: 77.2Gi',
    'namespace': 'paas-ws-test',
    'unschedulable': [{'name': 'svc-1415-nextcloud-data', 'kind': 'PersistentVolumeClaim',
                       'size': '80Gi', 'size_bytes': 80 * GIB, 'storage_class': 'longhorn-delete',
                       'replicas': 2, 'replicas_placeable': 0}],
    'largest_placeable_volume': '77.2Gi',
    'largest_placeable_volume_bytes': 82852184064,
    'nodes': [{'name': 'ubuntuserver-31zix', 'limit': '1317.2Gi', 'scheduled': '1240Gi',
               'headroom': '77.2Gi', 'scheduled_ratio': 0.9414, 'schedulable': True}],
    'orphaned_pvcs': {'count': 55, 'requested': '632Gi', 'longhorn_scheduled': '1264Gi'},
}


def _monitor_activities(env, summary):
    monitor = env.ref('woow_paas_platform.platform_monitor_singleton')
    return env['mail.activity'].sudo().search([
        ('res_model', '=', 'woow_paas_platform.platform_monitor'),
        ('res_id', '=', monitor.id), ('summary', '=', summary)])


class _Base(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['res.lang']._activate_lang('zh_TW')
        self.Monitor = self.env['woow_paas_platform.platform_monitor']
        # 告警要有收件者：確保至少一位 active 內部系統管理員（沙盒 DB 已有 admin）。
        self.assertTrue(self.Monitor._alert_recipients()[1], 'fixture 前提：有系統管理員')


@tagged('post_install', '-at_install')
class TestClusterStorageDeployMessage1415(_Base):

    def setUp(self):
        super().setUp()
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Capacity 1415', 'slug': 'capacity-1415', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/capacity-1415',
            'helm_chart_version': '0.1.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })

    def _user(self, lang):
        user = self.env['res.users'].create({
            'name': 'Tenant 1415 %s' % lang,
            'login': 'tenant1415-%s-%s@tenant1415.tw' % (lang, uuid.uuid4().hex[:6]),
            'lang': lang, 'tz': 'Asia/Taipei',
        })
        user.groups_id = [(6, 0, [self.env.ref('base.group_user').id])]
        return user

    def _service(self, user, subscription_plan=None):
        ws = self.env['woow_paas_platform.workspace'].create({
            'name': 'WS 1415 %s' % uuid.uuid4().hex[:6], 'owner_id': user.id})
        svc = _internal_provision(
            self.env['woow_paas_platform.cloud_service'].with_user(user).sudo()
        ).create({
            'name': 'svc-1415', 'workspace_id': ws.id, 'template_id': self.tpl.id,
            'state': 'pending', 'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-1415-%s' % uuid.uuid4().hex[:6],
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        })
        svc = svc.with_env(self.env)
        if subscription_plan:
            svc._create_and_confirm_subscription(user.partner_id, subscription_plan)
        return svc

    def _trial_plan(self):
        # 訂閱建立需要 company.sh_journal_id（慣例鏡射 test_create_service_validation_rollback_520）。
        company = self.env.company.sudo()
        if not self.env['account.journal'].search([
            ('company_id', '=', company.id), ('type', '=', 'sale'),
        ], limit=1):
            self.env['account.journal'].sudo().create({
                'name': 'Test Sales 1415', 'code': 'TS1415', 'type': 'sale',
                'company_id': company.id})
        self.env['res.company'].sudo()._ensure_subscription_journal()
        self.assertTrue(company.sh_journal_id, 'fixture 前提：訂閱 journal')
        plan = self.env['sh.subscription.plan'].create({
            'name': 'Plan 1415', 'sh_plan_price': 100.0, 'sh_duration': 1, 'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1, 'sh_trial': True, 'sh_trial_duration': 7,
            'sh_trial_unit': 'day',
        })
        self.env['product.template'].sudo().create({
            'name': 'Plan 1415 P', 'type': 'service', 'sh_product_subscribe': True,
            'sh_subscription_plan_id': plan.id, 'list_price': 100.0,
        })
        return plan

    def _deploy_failing_with(self, service, detail, status=409):
        client = MagicMock()
        client.install_release.side_effect = PaaSOperatorAPIError(
            message='API error: %s' % detail, status_code=status, detail=detail)
        with patch(_CLIENT_PATH, return_value=client):
            service._execute_deploy()
        service.invalidate_recordset()
        self.assertTrue(client.install_release.called, '前提：install 真的被呼叫到')
        return service

    def test_tenant_sees_storage_shortage_in_their_language(self):
        svc = self._deploy_failing_with(self._service(self._user('zh_TW')), CAPACITY_DETAIL)
        self.assertEqual(svc.state, 'error')
        msg = svc.error_message
        self.assertIn('平台儲存空間暫時不足', msg)
        self.assertIn('80 GiB', msg)
        self.assertIn('沒有建立任何資源', msg)
        self.assertIn('系統管理員已收到通知', msg)
        self.assertIn('聯絡客服', msg)
        self.assertNotIn('Deployment failed', msg)
        self.assertNotIn('Longhorn', msg, '不對租戶講叢集元件名')
        self.assertNotIn('取消訂閱', msg, '沒有訂閱就不提訂閱')

    def test_english_creator(self):
        svc = self._deploy_failing_with(self._service(self._user('en_US')), CAPACITY_DETAIL)
        self.assertTrue(svc.error_message.startswith(
            'The platform is temporarily short of storage space and cannot create the disk '
            'this service needs (80 GiB).'))

    def test_detail_without_sizes_still_gets_the_explanation(self):
        detail = {'error_code': CLUSTER_STORAGE_CAPACITY_ERROR_CODE}
        svc = self._deploy_failing_with(self._service(self._user('en_US')), detail)
        self.assertIn('cannot create the disks this service needs.', svc.error_message)

    def test_trial_subscription_is_left_as_the_1348_path_leaves_it_and_nothing_is_charged(self):
        user = self._user('zh_TW')
        svc = self._service(user, subscription_plan=self._trial_plan())
        sub = svc.sudo().subscription_id
        self.assertEqual(sub.state, 'in_progress', 'fixture 前提')
        self.assertTrue(sub.sh_trial, 'fixture 前提：試用中')
        moves_before = self.env['account.move'].sudo().search_count(
            [('partner_id', '=', user.partner_id.id)])

        svc = self._deploy_failing_with(svc, CAPACITY_DETAIL)

        self.assertEqual(svc.state, 'error')
        self.assertIn('取消訂閱', svc.error_message)
        self.assertIn('試用期內不會扣款', svc.error_message)
        self.assertEqual(sub.state, 'in_progress', '與 #1348 拒絕路徑相同：訂閱不在這裡動')
        self.assertFalse(self.env['wallet.transaction'].sudo().search_count([
            ('partner_id', '=', user.partner_id.id), ('transaction_type', '=', 'deduct')]))
        self.assertEqual(self.env['account.move'].sudo().search_count(
            [('partner_id', '=', user.partner_id.id)]), moves_before)
        self.assertEqual(sub._compute_unused_refund(), 0, '試用期刪除：不扣也不退')

    def test_admins_get_one_activity_with_a_line_per_rejected_service(self):
        summary = self.Monitor.SUMMARY_STORAGE_REJECTED
        self.assertFalse(_monitor_activities(self.env, summary))
        first = self._deploy_failing_with(self._service(self._user('en_US')), CAPACITY_DETAIL)
        activities = _monitor_activities(self.env, summary)
        self.assertTrue(activities)
        self.assertIn('svc-1415-nextcloud-data 80Gi x2', activities[0].note)
        self.assertIn('77.2Gi', activities[0].note)
        self.assertIn('55 PVCs', activities[0].note)
        self.assertIn('id=%s' % first.id, activities[0].note)

        second = self._deploy_failing_with(self._service(self._user('en_US')), CAPACITY_DETAIL)
        again = _monitor_activities(self.env, summary)
        self.assertEqual(len(again), len(activities), '同 summary 未處理前不重開，只追加')
        self.assertIn('id=%s' % second.id, again[0].note)

    def test_other_errors_are_not_mistaken_for_capacity_and_do_not_alert(self):
        summary = self.Monitor.SUMMARY_STORAGE_REJECTED
        quota = {'error_code': STORAGE_QUOTA_ERROR_CODE, 'exhausted': ['persistentvolumeclaims'],
                 'usage': {}}
        svc = self._deploy_failing_with(self._service(self._user('en_US')), quota)
        self.assertTrue(svc.error_message.startswith('Workspace storage quota is full.'))
        svc = self._deploy_failing_with(
            self._service(self._user('en_US')),
            'Helm installation failed. Check operator logs for details.', status=500)
        self.assertIn('Helm installation failed', svc.error_message)
        self.assertNotIn('short of storage', svc.error_message)
        self.assertFalse(_monitor_activities(self.env, summary))


def _report(largest_gib=403.7, ratios=(0.72, 0.86, 0.69, 0.62), available=True):
    names = ['ubuntuserver-31zix', 'ubuntuserver-59mqj', 'ubuntuserver-pggrg',
             'ubuntuserver-z0v3w']
    if not available:
        return {'available': False, 'reason': 'longhorn_unreadable'}
    return {
        'available': True, 'storage_class': 'longhorn-delete', 'replicas': 2,
        'largest_placeable_volume_bytes': int(largest_gib * GIB),
        'largest_placeable_volume': '%sGi' % largest_gib,
        'nodes': [{'name': n, 'schedulable': True, 'scheduled_ratio': r,
                   'limit': '1317.2Gi', 'scheduled': '%sGi' % round(1317.2 * r, 1),
                   'headroom': '%sGi' % round(1317.2 * (1 - r), 1)}
                  for n, r in zip(names, ratios)],
        'orphaned_pvcs': {'count': 55, 'requested': '632Gi', 'longhorn_scheduled': '1264Gi'},
    }


@tagged('post_install', '-at_install')
class TestClusterStorageCapacityMonitor1415(_Base):

    def _run(self, report=None, side_effect=None):
        client = MagicMock()
        if side_effect is not None:
            client.get_cluster_storage_capacity.side_effect = side_effect
        else:
            client.get_cluster_storage_capacity.return_value = report
        with patch(_CLIENT_PATH, return_value=client):
            self.Monitor._cron_check_cluster_storage_capacity()
        return client

    def _activities(self):
        return _monitor_activities(self.env, self.Monitor.SUMMARY_STORAGE_LOW)

    def test_cron_record_is_wired(self):
        cron = self.env.ref('woow_paas_platform.ir_cron_check_cluster_storage_capacity')
        self.assertEqual(cron.model_id.model, 'woow_paas_platform.platform_monitor')
        self.assertEqual((cron.interval_number, cron.interval_type), (15, 'minutes'))

    def test_healthy_cluster_does_not_alert(self):
        client = self._run(_report())
        expected_sc = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.storage_class_name', 'longhorn-delete') or None
        client.get_cluster_storage_capacity.assert_called_once_with(storage_class=expected_sc)
        self.assertFalse(self._activities())

    def test_incident_state_alerts_with_nodes_and_orphans(self):
        # 10-04 事件當下：有備援的最大可放 62.2Gi、各節點 94–100%。
        self._run(_report(62.2, ratios=(0.9414, 0.9619, 0.9528, 0.9999)))
        activities = self._activities()
        self.assertTrue(activities)
        note = activities[0].note
        self.assertIn('62.2Gi', note)
        self.assertIn('below the 80 GiB', note)
        self.assertIn('ubuntuserver-z0v3w has 99%', note)
        self.assertIn('Orphaned PVCs', note)
        self.assertIn('1264Gi', note)

        self._run(_report(10.0, ratios=(0.99,) * 4))
        self.assertEqual(len(self._activities()), len(activities), '未處理前不重開')

    def test_node_ratio_alone_alerts(self):
        self._run(_report(300.0, ratios=(0.5, 0.5, 0.91, 0.5)))
        self.assertIn('ubuntuserver-pggrg has 91%', self._activities()[0].note)

    def test_threshold_parameter(self):
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(CLUSTER_STORAGE_MIN_VOLUME_PARAM, '500')
        self._run(_report(403.7))
        self.assertIn('below the 500 GiB', self._activities()[0].note)
        self._activities().unlink()
        ICP.set_param(CLUSTER_STORAGE_MIN_VOLUME_PARAM, '0')  # 停用該項
        self._run(_report(1.0))
        self.assertFalse(self._activities())
        ICP.set_param(CLUSTER_STORAGE_MIN_VOLUME_PARAM, 'lots')  # 非法值回預設 80
        self._run(_report(62.2))
        self.assertIn('below the 80 GiB', self._activities()[0].note)

    def test_old_operator_and_unreadable_longhorn_are_skipped_quietly(self):
        self._run(side_effect=PaaSOperatorAPIError('API error: Not Found', status_code=404,
                                                   detail='Not Found'))
        self._run(side_effect=PaaSOperatorConnectionError('down'))
        self._run(_report(available=False))
        self.assertFalse(self._activities())

    def test_no_operator_configured(self):
        with patch(_CLIENT_PATH, return_value=None):
            self.Monitor._cron_check_cluster_storage_capacity()
        self.assertFalse(self._activities())


@tagged('post_install', '-at_install')
class TestClusterStorageParamRegisteredForUninstall(TransactionCase):
    """「模組常數＋get_param()」寫法 repo-lint 掃不到，以測試守卸載清理登記。"""

    def test_param_is_in_config_parameters_list(self):
        from ..hooks import _CONFIG_PARAMETERS

        self.assertIn(CLUSTER_STORAGE_MIN_VOLUME_PARAM, _CONFIG_PARAMETERS)
