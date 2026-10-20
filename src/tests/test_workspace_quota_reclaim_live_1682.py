"""#1682 opt-in 真鏈路：服務生命週期／後台／背景重試 → 真 Odoo → 真 operator HTTP → 真 K8s。

不 mock client、namespace 解析或 operator；唯一的測試框架調整是讓 Odoo 的對外請求守衛放行
fixture namespace 的 quota 讀寫、release 刪除（release 不存在 ⇒ 真 operator 回 404，
``_do_cleanup`` 視為已拆除）與 holder 的 scale。不取代 headed 後台 smoke。

fixture（測試外預先建立，見 PR 描述）：``dev-sandbox-quota1682-1682a0a0``，operator 的 SA
只有這個 namespace 的權限；``holder`` Deployment 的 pod 帶 requests／limits cpu 14、
memory 1Gi，nodeSelector 指向不存在的節點，所以永遠 Pending——不佔節點資源，但
ResourceQuota 照樣把它算進 used（模擬「刪除後還沒結束的 pod」）。
"""
from datetime import timedelta
import json
import os
import time
import unittest
import uuid
from urllib.parse import urlsplit

from freezegun import freeze_time

from odoo.tests.common import HttpCase, _super_send, tagged

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import get_paas_operator_client

_OPERATOR_URL = os.environ.get('QUOTA1682_OPERATOR_URL', '').rstrip('/')
_PREFIX = 'dev-sandbox-quota1682-'
_HASH = '1682a0a0'
_NS = _PREFIX + _HASH
_WS = 'woow_paas_platform.workspace'
_EPHEMERAL = {'ephemeral_storage_request': '4Gi', 'ephemeral_storage_limit': '80Gi'}
#: 兩台 vcpu 4／ram 4／storage 20 的公式；刪掉一台後是 12／12Gi／100Gi。
_BEFORE = ('24', '24Gi', '120Gi')
_KEYS = ('requests.cpu', 'limits.cpu', 'requests.memory', 'limits.memory', 'requests.storage')


# -standard：需要隔離的真 operator fixture，CI 的 `/woow_paas_platform` 不收；
# 只在明確 `--test-tags quota1682_live` 時才跑。
@tagged('-standard', 'post_install', '-at_install', 'quota1682_live')
class TestWorkspaceQuotaReclaimLive1682(HttpCase):

    @classmethod
    def _request_handler(cls, session, request, **kwargs):
        actual = urlsplit(request.url)
        base = urlsplit(_OPERATOR_URL) if _OPERATOR_URL else None
        allowed = {
            ('GET', '/api/namespaces/%s/quota' % _NS), ('PUT', '/api/namespaces/%s/quota' % _NS),
            ('POST', '/api/releases/%s/holder/scale' % _NS),
        }
        if base and actual.scheme == base.scheme and actual.netloc == base.netloc and (
                (request.method, actual.path) in allowed
                or (request.method == 'DELETE'
                    and actual.path.startswith('/api/releases/%s/live1682-' % _NS))):
            return _super_send(session, request, **kwargs)
        return super()._request_handler(session, request, **kwargs)

    def setUp(self):
        super().setUp()
        if not _OPERATOR_URL:
            if os.environ.get('REQUIRE_QUOTA1682_LIVE') == '1':
                self.fail('QUOTA1682_OPERATOR_URL required for the real quota reclaim gate')
            raise unittest.SkipTest('Explicit isolated real operator fixture required')
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.namespace_prefix', _PREFIX)
        params.set_param('woow_paas_platform.operator_url', _OPERATOR_URL)
        # API key 以檔案傳入，不出現在 job 設定或 log。
        with open(os.environ['QUOTA1682_API_KEY_FILE'], encoding='utf-8') as key_file:
            params.set_param('woow_paas_platform.operator_api_key', key_file.read().strip())
        # 叢集狀態跨測試存在：每次先回到起始值（fixture 重置，直接打 operator、不經平台）。
        self.client = get_paas_operator_client(self.env)
        self._hold(0)
        self.client.update_namespace_quota(_NS, *_BEFORE, **_EPHEMERAL)
        self._managed()
        owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Live reclaim owner', 'login': 'live-reclaim-%s' % uuid.uuid4().hex,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env[_WS].create({
            'name': 'Live quota reclaim 1682', 'owner_id': owner.id, 'ws_hash': _HASH})
        template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Live reclaim 1682', 'slug': 'live-reclaim-1682-%s' % uuid.uuid4().hex[:6],
            'helm_repo_url': 'https://charts.example.com', 'helm_chart_name': 'live',
            'helm_chart_version': '1.0.0',
        })
        self.services = [_internal_provision(self.env['woow_paas_platform.cloud_service']).create({
            'name': 'live1682-%s' % tag, 'workspace_id': self.workspace.id,
            'template_id': template.id, 'helm_namespace': _NS, 'state': 'running',
            # 叢集上不存在的 release：真 operator 回 404，清理視為已拆除後照常重算 quota。
            'helm_release_name': 'live1682-%s-%s' % (tag, uuid.uuid4().hex[:6]),
            'allocated_vcpu': 4, 'allocated_ram_gb': 4.0, 'allocated_storage_gb': 20,
        }) for tag in ('a', 'b')]

    # ------------------------------------------------------------------ helpers
    def _managed(self, settle=True):
        """受管 quota 讀數；推送後 quota controller 非同步更新 status，等到 available 再比對。"""
        managed = None
        for _attempt in range(40):
            managed = self.client.get_namespace_quota(_NS, include_managed=True)['managed_quota']
            if managed.get('available') or not settle:
                return managed
            time.sleep(0.5)
        self.fail('managed quota did not settle: %r' % managed)

    def _hold(self, replicas):
        """holder 的 pod 開／關，等 ResourceQuota 的 used 反映出來。"""
        self.client.scale_release('holder', _NS, replicas)
        want = '14' if replicas else '0'
        for _attempt in range(60):
            if self._managed()['used'].get('requests.cpu') == want:
                return
            time.sleep(0.5)
        self.fail('quota used did not reach requests.cpu=%s' % want)

    def _spec(self):
        hard = self._managed()['spec_hard']
        return tuple(hard[key] for key in _KEYS)

    def _ws(self):
        self.workspace.invalidate_recordset()
        return self.workspace.sudo()

    def _cleanup(self):
        """``subscription_lifecycle_cron`` 對到期服務做的那一步（真 operator 刪 release）。"""
        service = self.services[1]
        service._do_cleanup()
        self.assertEqual(service.state, 'deleting', '清理照常完成')

    def _cron_at(self, when):
        with freeze_time(when):
            self.env[_WS]._cron_reconcile_workspace_quotas()

    def _call_kw(self, method, args, kwargs=None):
        return self.url_open('/web/dataset/call_kw', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': _WS, 'method': method, 'args': args, 'kwargs': kwargs or {}},
        }), headers={'Content-Type': 'application/json'}).json()

    # ------------------------------------------------------------------ tests
    def test_cleanup_reclaims_on_the_real_cluster_without_confirmation(self):
        self.assertEqual(self._spec(), ('24', '24', '24Gi', '24Gi', '120Gi'))
        self._cleanup()
        self.assertEqual(self._spec(), ('12', '12', '12Gi', '12Gi', '100Gi'))
        managed = self._managed()
        self.assertEqual(managed['spec_hard']['persistentvolumeclaims'], '10', 'pvc 照舊')
        self.assertEqual((managed['spec_hard']['requests.ephemeral-storage'],
                          managed['spec_hard']['limits.ephemeral-storage']), ('4Gi', '80Gi'))
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertFalse(ws.quota_pending_decrease)
        self.assertEqual(json.loads(ws.quota_expected_tokens)[_NS], managed['sync_token'])

    def test_real_usage_blocks_the_decrease_until_it_is_released(self):
        self._hold(1)  # 刪除後還沒結束的 pod：requests／limits cpu 14 高於新目標 12
        self._cleanup()
        self.assertEqual(self._spec(), ('24', '24', '24Gi', '24Gi', '120Gi'),
                         '用量不足：一項都不降（memory 雖容得下也不單獨降）')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('unsafe_decrease', ws.quota_sync_reason)
        self.assertIn('requests.cpu', ws.quota_sync_reason)
        self.assertEqual(ws.quota_target_cpu, '12')
        due = ws.quota_next_retry_at
        self.assertTrue(due, '排了有限次自動重試')
        self._cron_at(due)  # 第一次重試：仍未釋放 ⇒ 重讀、仍擋下
        ws = self._ws()
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('blocked', 1))
        self.assertEqual(self._spec()[0], '24')
        self._hold(0)  # pod 結束、用量釋放
        self._cron_at(ws.quota_next_retry_at)
        self.assertEqual(self._spec(), ('12', '12', '12Gi', '12Gi', '100Gi'))
        ws = self._ws()
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('synced', 0))

    def test_a_raised_minimum_while_waiting_is_kept_and_the_rest_is_reclaimed(self):
        self._hold(1)
        self._cleanup()
        self.assertEqual(self._spec()[0], '24')
        # 等待期間管理員經後台提高 CPU 最低保障：立即推送，不需確認。
        self.authenticate('admin', 'admin')
        response = self._call_kw('set_quota_minimums', [[self.workspace.id]], {'cpu': 30})
        self.assertNotIn('error', response, response)
        self.assertEqual(self._spec()[:2], ('30', '30'))
        # 背景以最新政策收回其餘項目（CPU 用量 14 低於 30，memory／storage 容得下）。
        self._cron_at(self._ws().quota_last_attempt_at + timedelta(seconds=1))
        self.assertEqual(self._spec(), ('30', '30', '12Gi', '12Gi', '100Gi'))
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertEqual(ws.quota_floor_cpu, 30, '最低保障不被生命週期改寫')
