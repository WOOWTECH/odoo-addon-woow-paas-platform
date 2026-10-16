"""#1680 opt-in 真鏈路：後台管理員 RPC／背景重試 → 真 Odoo → 真 operator HTTP → 真 K8s。

不 mock client、namespace 解析或 operator；唯一的測試框架調整是讓 Odoo 的對外請求守衛放行
兩個隔離 namespace 的 quota 讀寫 URL（以及刻意連不上的那個埠，用來驗真的斷線）。不取代
headed 後台 smoke。

fixture（測試外預先建立，見 PR 描述）：
- A ``dev-sandbox-quota1680-1680a0a0``：operator 的 SA 可讀寫 resourcequotas／limitranges；
- B ``dev-sandbox-quota1680-1680b0b0``：SA 只能讀 resourcequotas ⇒ 推送時 operator 回真的 500。
兩者的受管 quota 起始為 cpu 8、memory 12Gi、pvc 10、storage 100Gi、ephemeral 4Gi／80Gi。
"""
from datetime import timedelta
import json
import os
import time
import unittest
import uuid
from urllib.parse import urlsplit

from freezegun import freeze_time

from odoo import fields
from odoo.tests.common import HttpCase, _super_send, tagged

from ..services.paas_operator import PaaSOperatorAPIError, get_paas_operator_client

_OPERATOR_URL = os.environ.get('QUOTA1680_OPERATOR_URL', '').rstrip('/')
_UNREACHABLE_URL = os.environ.get('QUOTA1680_UNREACHABLE_URL', '').rstrip('/')
_PREFIX = 'dev-sandbox-quota1680-'
_HASH_A, _HASH_B = '1680a0a0', '1680b0b0'
_NS_A, _NS_B = _PREFIX + _HASH_A, _PREFIX + _HASH_B
_WS = 'woow_paas_platform.workspace'
_BASELINE = ('8', '12Gi', '100Gi')
_EPHEMERAL = {'ephemeral_storage_request': '4Gi', 'ephemeral_storage_limit': '80Gi'}


# -standard：需要隔離的真 operator fixture，CI 的 `/woow_paas_platform` 不收；
# 只在明確 `--test-tags quota1680_live` 時才跑。
@tagged('-standard', 'post_install', '-at_install', 'quota1680_live')
class TestWorkspaceQuotaSyncLive1680(HttpCase):

    @classmethod
    def _request_handler(cls, session, request, **kwargs):
        actual = urlsplit(request.url)
        for base in filter(None, (_OPERATOR_URL, _UNREACHABLE_URL)):
            for namespace in (_NS_A, _NS_B):
                expected = urlsplit(base + '/api/namespaces/' + namespace + '/quota')
                if (request.method in ('GET', 'PUT') and actual.scheme == expected.scheme
                        and actual.netloc == expected.netloc and actual.path == expected.path):
                    return _super_send(session, request, **kwargs)
        return super()._request_handler(session, request, **kwargs)

    def setUp(self):
        super().setUp()
        if not _OPERATOR_URL:
            if os.environ.get('REQUIRE_QUOTA1680_LIVE') == '1':
                self.fail('QUOTA1680_OPERATOR_URL required for the real quota sync gate')
            raise unittest.SkipTest('Explicit isolated real operator fixture required')
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.namespace_prefix', _PREFIX)
        params.set_param('woow_paas_platform.operator_url', _OPERATOR_URL)
        # API key 以檔案傳入，不出現在 job 設定或 log。
        with open(os.environ['QUOTA1680_API_KEY_FILE'], encoding='utf-8') as key_file:
            params.set_param('woow_paas_platform.operator_api_key', key_file.read().strip())
        # 叢集狀態跨測試存在：A 每次先回到起始值（fixture 重置，直接打 operator、不經平台）。
        self.client = get_paas_operator_client(self.env)
        self.client.update_namespace_quota(_NS_A, *_BASELINE, **_EPHEMERAL)
        self._managed(_NS_A)

    # ------------------------------------------------------------------ helpers
    def _workspace(self, ws_hash):
        owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Live sync owner', 'login': 'live-sync-%s' % uuid.uuid4().hex,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        return self.env[_WS].create({'name': 'Live quota sync 1680', 'owner_id': owner.id,
                                     'ws_hash': ws_hash})

    def _call_kw(self, method, args, kwargs=None):
        return self.url_open('/web/dataset/call_kw', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': _WS, 'method': method, 'args': args, 'kwargs': kwargs or {}},
        }), headers={'Content-Type': 'application/json'}).json()

    def _managed(self, namespace, settle=True):
        # 推送後 quota controller 非同步更新 status.hard；等到 available（hard==spec）再比對。
        managed = None
        for _attempt in range(20):
            managed = self.client.get_namespace_quota(namespace, include_managed=True)['managed_quota']
            if managed.get('available') or not settle:
                return managed
            time.sleep(0.5)
        self.fail('managed quota did not settle: %r' % managed)

    def _ws(self, workspace):
        workspace.invalidate_recordset()
        return workspace.sudo()

    def _cron(self):
        self.env[_WS]._cron_reconcile_workspace_quotas()

    def _exhaust(self, workspace):
        """背景重試入口（ir.cron 方法）在每次到期時跑一次，直到額度用完。"""
        for _attempt in range(5):
            due = self._ws(workspace).quota_next_retry_at
            self.assertTrue(due, '每次失敗都排了下一次自動重試')
            with freeze_time(due):
                self._cron()
        self.assertEqual(self._ws(workspace).quota_sync_state, 'exhausted')

    # ------------------------------------------------------------------ tests
    def test_versioned_push_and_a_late_request_on_the_real_cluster(self):
        workspace = self._workspace(_HASH_A)
        planned = self._managed(_NS_A)['resource_version']
        self.authenticate('admin', 'admin')
        response = self._call_kw('set_quota_minimums', [[workspace.id]], {'cpu': 18})
        self.assertNotIn('error', response, response)
        self.assertEqual(response['result']['state'], 'synced', response)
        after = self._managed(_NS_A)
        self.assertEqual(after['spec_hard']['limits.cpu'], '18')
        ws = self._ws(workspace)
        self.assertFalse(ws.quota_sync_unguarded)
        self.assertEqual(json.loads(ws.quota_expected_tokens)[_NS_A], after['sync_token'])

        # 以設定前讀到的版本規劃的舊請求這時才到：真 apiserver 拒絕，不覆寫。
        with self.assertRaises(PaaSOperatorAPIError) as late:
            self.client.update_namespace_quota(
                _NS_A, '10', '12Gi', '100Gi', **_EPHEMERAL,
                quota_guard={'sync_token': uuid.uuid4().hex, 'expected_resource_version': planned})
        self.assertEqual(late.exception.status_code, 409)
        final = self._managed(_NS_A)
        self.assertEqual(final['spec_hard']['limits.cpu'], '18')
        self.assertEqual(final['sync_token'], after['sync_token'])

        # 定期讀回驗證：遠端與平台記錄一致 ⇒ 不寫（resourceVersion 不變）。
        with freeze_time(fields.Datetime.now() + timedelta(hours=6, minutes=1)):
            self._cron()
        self.assertEqual(self._managed(_NS_A)['resource_version'], final['resource_version'])
        self.assertEqual(self._ws(workspace).quota_sync_state, 'synced')

    def test_uncommitted_higher_remote_value_is_fenced_without_lowering(self):
        workspace = self._workspace(_HASH_A)
        self.authenticate('admin', 'admin')
        self._call_kw('set_quota_minimums', [[workspace.id]], {'cpu': 18})
        # 另一筆交易把 cpu 推到 30 之後回捲：叢集留下平台不認得的 token 與較高的值。
        current = self._managed(_NS_A)
        foreign = uuid.uuid4().hex
        self.client.update_namespace_quota(
            _NS_A, '30', '12Gi', '100Gi', **_EPHEMERAL,
            quota_guard={'sync_token': foreign, 'expected_resource_version': current['resource_version']})
        response = self._call_kw('action_retry_quota_sync', [[workspace.id]])
        self.assertNotIn('error', response, response)
        fenced = self._managed(_NS_A)
        self.assertEqual(fenced['spec_hard']['limits.cpu'], '30', '不降回已提交的 18')
        self.assertNotEqual(fenced['sync_token'], foreign)
        ws = self._ws(workspace)
        self.assertEqual(json.loads(ws.quota_expected_tokens)[_NS_A], fenced['sync_token'])
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertIn('limits.cpu: cluster 30, target 18', ws.quota_pending_decrease)

    def test_real_500_backs_off_exhausts_and_manual_retry_resets_the_budget(self):
        workspace = self._workspace(_HASH_B)
        before = self._managed(_NS_B)
        self.authenticate('admin', 'admin')
        response = self._call_kw('set_quota_minimums', [[workspace.id]], {'cpu': 18})
        self.assertNotIn('error', response, response)
        ws = self._ws(workspace)
        self.assertEqual(ws.quota_sync_state, 'pending')
        self.assertIn('operator_error: 500', ws.quota_sync_reason)
        self._exhaust(workspace)
        self.assertEqual(self._ws(workspace).quota_retry_count, 5)
        self.assertEqual(self._managed(_NS_B)['spec_hard'], before['spec_hard'])
        response = self._call_kw('action_retry_quota_sync', [[workspace.id]])
        self.assertNotIn('error', response, response)
        self.assertEqual(response['result']['params']['type'], 'warning')
        ws = self._ws(workspace)
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('pending', 0))
        self.assertTrue(ws.quota_next_retry_at)

    def test_disconnect_and_missing_operator_then_manual_recovery(self):
        if not _UNREACHABLE_URL:
            self.fail('QUOTA1680_UNREACHABLE_URL required (a port with nothing listening)')
        workspace = self._workspace(_HASH_A)
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.operator_url', _UNREACHABLE_URL)
        self.authenticate('admin', 'admin')
        self._call_kw('set_quota_minimums', [[workspace.id]], {'cpu': 20})
        ws = self._ws(workspace)
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('quota_unreadable', ws.quota_sync_reason)
        self._exhaust(workspace)
        params.set_param('woow_paas_platform.operator_url', '')
        self._call_kw('action_retry_quota_sync', [[workspace.id]])
        ws = self._ws(workspace)
        self.assertEqual(ws.quota_sync_state, 'pending')
        self.assertIn('operator_not_configured', ws.quota_sync_reason)
        params.set_param('woow_paas_platform.operator_url', _OPERATOR_URL)
        response = self._call_kw('action_retry_quota_sync', [[workspace.id]])
        self.assertEqual(response['result']['params']['type'], 'success', response)
        self.assertEqual(self._managed(_NS_A)['spec_hard']['limits.cpu'], '20')
        ws = self._ws(workspace)
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('synced', 0))
