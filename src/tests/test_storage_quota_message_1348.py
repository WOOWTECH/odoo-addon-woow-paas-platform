# -*- coding: utf-8 -*-
"""#1348：operator 回「工作區儲存配額已滿」時，租戶看到的是說得出原因的訊息。

刪除服務後 PVC 依 #317 保留，仍計入工作區 namespace 的 ResourceQuota；配額吃滿後
operator 原本只回「Helm installation failed. Check operator logs for details.」。
operator 改成在 helm install 前檢查、回 409 ＋ ``detail.error_code``
（``extra/paas-operator/src/services/storage_quota.py``）；平台在
``_execute_deploy`` 把它轉成租戶語言的說明（``_storage_quota_error_message``）。

守的事：

* 配額錯誤 ⇒ ``error_message`` 是說明文字（含用量、孤兒數、最早釋放日），而且
  依**服務建立者**的語言（cron 的 env 語言不是租戶的）；
* 其他 operator 錯誤 ⇒ 維持原本的「Deployment failed: …」，不被新分支吃掉。
"""
import json
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import STORAGE_QUOTA_ERROR_CODE, _internal_provision
from ..services.paas_operator import PaaSOperatorAPIError

_CLIENT_PATH = (
    'odoo.addons.woow_paas_platform.services.paas_operator'
    '.get_paas_operator_client'
)

# 2026-01-01 00:00:00 UTC ＋ 90 天（Asia/Taipei 仍是 2026-04-01）
_RELEASE_EPOCH = 1767225600 + 90 * 86400

_QUOTA_DETAIL = {
    'error_code': STORAGE_QUOTA_ERROR_CODE,
    'message': 'Workspace storage quota exhausted in paas-ws-test (persistentvolumeclaims)',
    'namespace': 'paas-ws-test',
    'exhausted': ['persistentvolumeclaims'],
    'usage': {'persistentvolumeclaims': {'used': '10', 'hard': '10'},
              'requests.storage': {'used': '95Gi', 'hard': '100Gi'}},
    'orphaned_pvc_count': 9,
    'orphaned_storage': '90Gi',
    'orphaned_releases': ['svc-a'],
    'next_release_epoch': _RELEASE_EPOCH,
}


@tagged('post_install', '-at_install')
class TestStorageQuotaDeployMessage1348(TransactionCase):

    def setUp(self):
        super().setUp()
        self.env['res.lang']._activate_lang('zh_TW')
        self.tpl = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Quota 1348', 'slug': 'quota-1348', 'helm_repo_url': '',
            'helm_chart_name': 'oci://example.com/quota-1348',
            'helm_chart_version': '0.1.0', 'workload_type': 'container',
            'post_deploy_init_type': 'none', 'mcp_enabled': False,
        })
        self.ws = self.env['woow_paas_platform.workspace'].create({'name': 'WS 1348'})

    def _service(self, user=None):
        Service = self.env['woow_paas_platform.cloud_service']
        if user:
            Service = Service.with_user(user).sudo()
        svc = _internal_provision(Service).create({
            'name': 'svc-1348', 'workspace_id': self.ws.id, 'template_id': self.tpl.id,
            'state': 'pending', 'helm_namespace': 'paas-ws-test',
            'helm_release_name': 'svc-1348',
            'helm_values': json.dumps({'config': {'nonSensitive': {}}}),
        })
        # 以測試（cron 同級）身分執行部署；create_uid 留在租戶身上。
        return svc.with_env(self.env)

    def _user(self, lang):
        return self.env['res.users'].create({
            'name': 'Tenant 1348 %s' % lang, 'login': 'tenant1348-%s@tenant1348.tw' % lang,
            'lang': lang, 'tz': 'Asia/Taipei',
        })

    def _deploy_failing_with(self, service, detail, status=409):
        client = MagicMock()
        client.install_release.side_effect = PaaSOperatorAPIError(
            message='API error: %s' % detail, status_code=status, detail=detail)
        with patch(_CLIENT_PATH, return_value=client):
            service._execute_deploy()
        service.invalidate_recordset()
        self.assertTrue(client.install_release.called, '前提：install 真的被呼叫到')
        return service

    def test_quota_error_is_explained_in_the_creators_language(self):
        svc = self._deploy_failing_with(self._service(self._user('zh_TW')), _QUOTA_DETAIL)
        self.assertEqual(svc.state, 'error')
        msg = svc.error_message
        self.assertIn('工作區儲存配額已滿', msg)
        self.assertIn('10／10', msg)
        self.assertIn('9 個', msg)
        self.assertIn('90Gi', msg)
        self.assertIn('2026-04-01', msg)
        self.assertNotIn('Helm installation failed', msg)

    def test_quota_error_in_english_for_an_english_creator(self):
        svc = self._deploy_failing_with(self._service(self._user('en_US')), _QUOTA_DETAIL)
        msg = svc.error_message
        self.assertIn('Workspace storage quota is full: 10 of 10 storage volumes', msg)
        self.assertIn('9 of them hold data of deleted services', msg)
        self.assertIn('2026-04-01', msg)

    def test_storage_capacity_variant_without_orphans(self):
        detail = dict(_QUOTA_DETAIL, exhausted=['requests.storage'], orphaned_pvc_count=0,
                      next_release_epoch=None)
        svc = self._deploy_failing_with(self._service(self._user('en_US')), detail)
        msg = svc.error_message
        self.assertIn('95Gi of 100Gi storage capacity', msg)
        self.assertNotIn('deleted services', msg, '沒有孤兒 PVC 就不提保留期')

    def test_detail_without_numbers_still_gets_the_explanation(self):
        """operator 讀不到用量時只帶 error_code：仍要給說明，不退回泛用訊息。"""
        detail = {'error_code': STORAGE_QUOTA_ERROR_CODE, 'exhausted': ['persistentvolumeclaims'],
                  'usage': {}}
        svc = self._deploy_failing_with(self._service(self._user('en_US')), detail)
        self.assertTrue(svc.error_message.startswith('Workspace storage quota is full.'))

    def test_other_operator_errors_keep_the_generic_message(self):
        svc = self._deploy_failing_with(
            self._service(self._user('en_US')),
            'Helm installation failed. Check operator logs for details.', status=500)
        self.assertEqual(svc.state, 'error')
        self.assertIn('Helm installation failed', svc.error_message)
        self.assertNotIn('storage quota', svc.error_message)

    def test_other_structured_errors_are_not_mistaken_for_quota(self):
        svc = self._deploy_failing_with(
            self._service(self._user('en_US')),
            {'message': 'boom', 'error_step': None, 'error_kind': 'unknown'}, status=500)
        self.assertNotIn('storage quota', svc.error_message)
