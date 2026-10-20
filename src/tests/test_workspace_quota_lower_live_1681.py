"""#1681 opt-in 真鏈路：後台管理員 RPC → 真 Odoo → 真 operator HTTP → 真 K8s ResourceQuota。

不 mock client、namespace 解析或 operator；唯一的框架調整是讓 Odoo 的對外請求守衛放行兩個
隔離 namespace 的 quota 讀寫 URL。用量（status.used）來自叢集上真的 pod 與 PVC。不取代
headed 後台 smoke。

fixture（測試外預先建立，見 PR 描述）：
- A ``dev-sandbox-1681-q-1681a0a0``：operator 的 SA 可讀寫 resourcequotas。裡面有一個
  pause pod（requests cpu 1／memory 1Gi，limits cpu 2／memory 12Gi）與一個永遠 Pending 的
  保留 PVC（requests.storage 150Gi，計入 quota）。
- B ``dev-sandbox-1681-q-1681b0b0``：SA 只能讀 ⇒ 推送時 operator 回真的 500。受管 quota cpu 18。
起始值 cpu 8（B 為 18）、memory 12Gi、pvc 10、storage 100Gi、ephemeral 4Gi／80Gi。
"""
import json
import os
import time
import unittest
import uuid
from urllib.parse import urlsplit

from freezegun import freeze_time

from odoo.tests.common import HttpCase, _super_send, tagged

from ..services.paas_operator import get_paas_operator_client

_OPERATOR_URL = os.environ.get('QUOTA1681_OPERATOR_URL', '').rstrip('/')
_PREFIX = 'dev-sandbox-1681-q-'
_HASH_A, _HASH_B = '1681a0a0', '1681b0b0'
_NS_A, _NS_B = _PREFIX + _HASH_A, _PREFIX + _HASH_B
_WS = 'woow_paas_platform.workspace'
_BASELINE = ('8', '12Gi', '100Gi')
_EPHEMERAL = {'ephemeral_storage_request': '4Gi', 'ephemeral_storage_limit': '80Gi'}


# -standard：需要隔離的真 operator fixture，CI 的 `/woow_paas_platform` 不收；
# 只在明確 `--test-tags quota1681_live` 時才跑。
@tagged('-standard', 'post_install', '-at_install', 'quota1681_live')
class TestWorkspaceQuotaLowerLive1681(HttpCase):

    @classmethod
    def _request_handler(cls, session, request, **kwargs):
        actual = urlsplit(request.url)
        if _OPERATOR_URL:
            for namespace in (_NS_A, _NS_B):
                expected = urlsplit(_OPERATOR_URL + '/api/namespaces/' + namespace + '/quota')
                if (request.method in ('GET', 'PUT') and actual.scheme == expected.scheme
                        and actual.netloc == expected.netloc and actual.path == expected.path):
                    return _super_send(session, request, **kwargs)
        return super()._request_handler(session, request, **kwargs)

    def setUp(self):
        super().setUp()
        if not _OPERATOR_URL:
            if os.environ.get('REQUIRE_QUOTA1681_LIVE') == '1':
                self.fail('QUOTA1681_OPERATOR_URL required for the real quota decrease gate')
            raise unittest.SkipTest('Explicit isolated real operator fixture required')
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.namespace_prefix', _PREFIX)
        params.set_param('woow_paas_platform.operator_url', _OPERATOR_URL)
        # API key 以檔案傳入，不出現在 job 設定或 log。
        with open(os.environ['QUOTA1681_API_KEY_FILE'], encoding='utf-8') as key_file:
            params.set_param('woow_paas_platform.operator_api_key', key_file.read().strip())
        # 叢集狀態跨測試存在：A 每次先回到起始值（fixture 重置，直接打 operator、不經平台）。
        self.client = get_paas_operator_client(self.env)
        self.client.update_namespace_quota(_NS_A, *_BASELINE, **_EPHEMERAL)
        self._managed(_NS_A)
        self.authenticate('admin', 'admin')

    # ------------------------------------------------------------------ helpers
    def _workspace(self, ws_hash):
        owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Live lower owner', 'login': 'live-lower-%s' % uuid.uuid4().hex,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        return self.env[_WS].create({'name': 'Live quota lower 1681', 'owner_id': owner.id,
                                     'ws_hash': ws_hash})

    def _call_kw(self, method, args, kwargs=None):
        response = self.url_open('/web/dataset/call_kw', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': _WS, 'method': method, 'args': args, 'kwargs': kwargs or {}},
        }), headers={'Content-Type': 'application/json'}).json()
        return response

    def _ok(self, method, workspace, **kwargs):
        response = self._call_kw(method, [[workspace.id]], kwargs)
        self.assertNotIn('error', response, response)
        return response['result']

    def _user_error(self, method, workspace, **kwargs):
        response = self._call_kw(method, [[workspace.id]], kwargs)
        self.assertIn('error', response, response)
        self.assertEqual(response['error']['data']['name'], 'odoo.exceptions.UserError', response)
        return response['error']['data']['message']

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

    # ------------------------------------------------------------------ tests
    def test_confirmed_decrease_lands_on_the_real_cluster(self):
        workspace = self._workspace(_HASH_A)
        self.assertEqual(self._ok('set_quota_minimums', workspace, cpu=18)['state'], 'synced')
        self.assertEqual(self._managed(_NS_A)['spec_hard']['limits.cpu'], '18')
        # 以完整目標判斷：只清 cpu，但 memory 目標（公式 8Gi）也低於叢集 12Gi，而 limits.memory
        # 已佔用 12Gi ⇒ 整筆拒絕（全有或全無）。
        blocked = self._ok('preview_quota_minimums', workspace, cpu=0)
        self.assertEqual(blocked['state'], 'rejected', blocked)
        self.assertIn('limits.memory used_exceeds', '; '.join(blocked['reasons']))
        # 管理員把 memory 最低保障設成 12Gi（不降），cpu 的降額就能單獨確認。
        self.assertEqual(self._ok('set_quota_minimums', workspace, memory_gi=12)['state'], 'synced')
        preview = self._ok('preview_quota_minimums', workspace, cpu=0)
        self.assertEqual(preview['state'], 'confirmation_required', preview)
        decreases = {d['key']: d for d in preview['decreases']}
        self.assertEqual(set(decreases), {'requests.cpu', 'limits.cpu'})
        # 真 pod 的用量：requests.cpu 1、limits.cpu 2
        self.assertEqual((decreases['limits.cpu']['cluster'], decreases['limits.cpu']['target'],
                          decreases['limits.cpu']['used']), ('18', '8', '2'))
        self.assertEqual(decreases['requests.cpu']['used'], '1')
        self.assertIn('not atomic with Kubernetes admission', preview['warning'])
        result = self._ok('set_quota_minimums', workspace, cpu=0,
                          confirmation=preview['confirmation'])
        self.assertEqual(result['state'], 'synced', result)
        after = self._managed(_NS_A)
        self.assertEqual((after['spec_hard']['requests.cpu'], after['spec_hard']['limits.cpu']),
                         ('8', '8'))
        self.assertEqual((after['hard']['requests.cpu'], after['hard']['limits.cpu']), ('8', '8'))
        self.assertEqual(after['spec_hard']['limits.memory'], '12Gi', '其他項不受影響')
        ws = self._ws(workspace)
        self.assertEqual(after['sync_token'], ws._quota_expected_tokens()[_NS_A])
        self.assertFalse(ws.quota_lower_authorization)

    def test_real_usage_blocks_unsafe_decreases_and_nothing_is_saved(self):
        workspace = self._workspace(_HASH_A)
        self._ok('set_quota_minimums', workspace, memory_gi=20, storage_gi=300)
        before = self._managed(_NS_A)
        self.assertEqual((before['spec_hard']['limits.memory'],
                          before['spec_hard']['requests.storage']), ('20Gi', '300Gi'))
        # limits.memory 已佔用 12Gi（requests 只有 1Gi）：降到 8Gi 會擋。
        memory = self._ok('preview_quota_minimums', workspace, memory_gi=0)
        self.assertEqual(memory['state'], 'rejected', memory)
        self.assertIn('limits.memory used_exceeds', '; '.join(memory['reasons']))
        self.assertNotIn('requests.memory', '; '.join(memory['reasons']))
        # 保留 PVC 佔 150Gi：storage 降到 100Gi 會擋。
        storage = self._ok('preview_quota_minimums', workspace, storage_gi=0)
        self.assertIn('requests.storage used_exceeds', '; '.join(storage['reasons']))
        message = self._user_error('set_quota_minimums', workspace, memory_gi=0, storage_gi=0)
        self.assertIn('cannot be lowered safely', message)
        ws = self._ws(workspace)
        self.assertEqual((ws.quota_floor_memory_gi, ws.quota_floor_storage_gi), (20, 300))
        after = self._managed(_NS_A)
        self.assertEqual(after['resource_version'], before['resource_version'], '沒有寫入叢集')
        # 降到剛好等於用量（12Gi）是安全的，真 admission 下也能落地。
        exact = self._ok('preview_quota_minimums', workspace, memory_gi=12)
        self.assertEqual(exact['state'], 'confirmation_required', exact)
        self._ok('set_quota_minimums', workspace, memory_gi=12,
                 confirmation=exact['confirmation'])
        self.assertEqual(self._managed(_NS_A)['spec_hard']['limits.memory'], '12Gi')

    def test_confirmed_decrease_survives_a_real_push_failure_and_retries(self):
        workspace = self._workspace(_HASH_B)
        self.assertEqual(self._managed(_NS_B)['spec_hard']['limits.cpu'], '18')
        preview = self._ok('preview_quota_minimums', workspace, cpu=0)
        # 工作區沒有最低保障：政策目標沒變 ⇒ 不需要確認，也不會降。
        self.assertEqual(preview['state'], 'no_change')
        self._ok('set_quota_minimums', workspace, cpu=20)  # 推送被 RBAC 拒：真 500
        preview = self._ok('preview_quota_minimums', workspace, cpu=0)
        self.assertEqual(preview['state'], 'confirmation_required', preview)
        result = self._ok('set_quota_minimums', workspace, cpu=0,
                          confirmation=preview['confirmation'])
        self.assertEqual(result['state'], 'pending', result)
        ws = self._ws(workspace)
        self.assertEqual(ws.quota_floor_cpu, 0, '合法且已確認的設定保留')
        self.assertTrue(ws.quota_lower_authorization)
        self.assertIn('operator_error: 500', ws.quota_sync_reason)
        due = ws.quota_next_retry_at
        self.assertTrue(due)
        self.assertEqual(self._managed(_NS_B)['spec_hard']['limits.cpu'], '18')
        with freeze_time(due):
            self.env[_WS]._cron_reconcile_workspace_quotas()
        ws = self._ws(workspace)
        self.assertEqual(ws.quota_retry_count, 1)
        self.assertIn('operator_error: 500', ws.quota_sync_reason)
        self.assertTrue(ws.quota_lower_authorization, '重試耗盡前一直保留，每次重試都重檢')
        self.assertEqual(self._managed(_NS_B)['spec_hard']['limits.cpu'], '18')
