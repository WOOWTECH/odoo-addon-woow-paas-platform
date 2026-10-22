"""#1781 opt-in 真鏈路：後台管理員 RPC → 真 Odoo → 真 operator HTTP → 真 K8s ResourceQuota。

不 mock client、namespace 解析或 operator；唯一的測試框架調整是讓 Odoo 的對外請求守衛放行
隔離 namespace 的 quota 讀寫 URL。不取代 headed 後台 smoke。

fixture（測試外預先建立，見 PR 描述）：
- namespace ``dev-sandbox-1781-quota-1781a0a0``：12 個永遠 Pending 的 PVC（StorageClass 不存在，
  不建 PV，但 ResourceQuota 真的把它們算進 used）——對應 prod ``paas-ws-7948b087`` 的
  PVC hard=12、used=12；
- 兩顆 operator：``QUOTA1781_OPERATOR_URL`` 是本分支（支援 ``pvc_limit``），
  ``QUOTA1781_LEGACY_OPERATOR_URL`` 是 #1781 之前的 image（GET 沒有能力鍵、PUT 忽略 pvc_limit）。
  兩者共用只對 fixture namespace 有 resourcequotas／limitranges 讀寫權的 ServiceAccount。
"""
import json
import os
import time
import unittest
import uuid
from urllib.parse import urlsplit

from odoo.tests.common import HttpCase, _super_send, tagged

from ..services.paas_operator import get_paas_operator_client

_OPERATOR_URL = os.environ.get('QUOTA1781_OPERATOR_URL', '').rstrip('/')
_LEGACY_URL = os.environ.get('QUOTA1781_LEGACY_OPERATOR_URL', '').rstrip('/')
_PREFIX = 'dev-sandbox-1781-quota-'
_HASH = '1781a0a0'
_NS = _PREFIX + _HASH
_WS = 'woow_paas_platform.workspace'
#: 工作區沒有服務 ⇒ 公式下限 8／8Gi／100Gi；ephemeral 下限 4Gi／80Gi
_BASELINE = ('8', '8Gi', '100Gi')
_EPHEMERAL = {'ephemeral_storage_request': '4Gi', 'ephemeral_storage_limit': '80Gi'}


# -standard：需要隔離的真 operator fixture，CI 的 `/woow_paas_platform` 不收；
# 只在明確 `--test-tags quota1781_live` 時才跑。
@tagged('-standard', 'post_install', '-at_install', 'quota1781_live')
class TestWorkspaceQuotaPvcLive1781(HttpCase):

    @classmethod
    def _request_handler(cls, session, request, **kwargs):
        actual = urlsplit(request.url)
        for base in filter(None, (_OPERATOR_URL, _LEGACY_URL)):
            expected = urlsplit(base + '/api/namespaces/' + _NS + '/quota')
            if (request.method in ('GET', 'PUT') and actual.scheme == expected.scheme
                    and actual.netloc == expected.netloc and actual.path == expected.path):
                return _super_send(session, request, **kwargs)
        return super()._request_handler(session, request, **kwargs)

    def setUp(self):
        super().setUp()
        if not (_OPERATOR_URL and _LEGACY_URL):
            if os.environ.get('REQUIRE_QUOTA1781_LIVE') == '1':
                self.fail('QUOTA1781_OPERATOR_URL and QUOTA1781_LEGACY_OPERATOR_URL required')
            raise unittest.SkipTest('Explicit isolated real operator fixture required')
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.namespace_prefix', _PREFIX)
        # API key 以檔案傳入，不出現在 job 設定或 log。
        with open(os.environ['QUOTA1781_API_KEY_FILE'], encoding='utf-8') as key_file:
            params.set_param('woow_paas_platform.operator_api_key', key_file.read().strip())
        self._use(_OPERATOR_URL)
        # 叢集狀態跨測試存在：每次先回到起始值 PVC 上限 12（直接打新 operator、不經平台）。
        self.client.update_namespace_quota(_NS, *_BASELINE, pvc_limit='12', **_EPHEMERAL)
        start = self._managed()
        self.assertEqual(start['spec_hard']['persistentvolumeclaims'], '12')
        self.assertEqual(start['used']['persistentvolumeclaims'], '12', 'fixture：12 個 PVC 佔滿')

    # ------------------------------------------------------------------ helpers
    def _use(self, url):
        self.env['ir.config_parameter'].sudo().set_param('woow_paas_platform.operator_url', url)
        self.client = get_paas_operator_client(self.env)

    def _workspace(self):
        owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Live pvc owner', 'login': 'live-pvc-%s' % uuid.uuid4().hex,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        return self.env[_WS].create({'name': 'Live quota pvc 1781', 'owner_id': owner.id,
                                     'ws_hash': _HASH})

    def _call_kw(self, method, args, kwargs=None):
        return self.url_open('/web/dataset/call_kw', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': _WS, 'method': method, 'args': args, 'kwargs': kwargs or {}},
        }), headers={'Content-Type': 'application/json'}).json()

    def _managed(self):
        # 推送後 quota controller 非同步更新 status.hard；等到 available（hard==spec）再比對。
        managed = None
        for _attempt in range(20):
            managed = self.client.get_namespace_quota(_NS, include_managed=True)['managed_quota']
            if managed.get('available'):
                return managed
            time.sleep(0.5)
        self.fail('managed quota did not settle: %r' % managed)

    def _ws(self, workspace):
        workspace.invalidate_recordset()
        return workspace.sudo()

    # ------------------------------------------------------------------ tests
    def test_legacy_operator_keeps_blocking_and_never_touches_the_quota(self):
        self._use(_LEGACY_URL)
        before = self._managed()
        self.assertNotIn('supports_pvc_limit', before, '前提：這顆是 #1781 之前的 operator')
        workspace = self._workspace()
        self.authenticate('admin', 'admin')
        response = self._call_kw('set_quota_minimums', [[workspace.id]], {'cpu': 18})
        self.assertNotIn('error', response, response)
        ws = self._ws(workspace)
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('would_lower: persistentvolumeclaims', ws.quota_sync_reason)
        after = self._managed()
        self.assertEqual(after['resource_version'], before['resource_version'], '沒有任何寫入')
        self.assertEqual(after['spec_hard']['persistentvolumeclaims'], '12')

    def test_admin_raise_is_synced_and_the_twelve_pvcs_keep_their_limit(self):
        self.assertIs(self._managed()['supports_pvc_limit'], True)
        workspace = self._workspace()
        self.authenticate('admin', 'admin')
        response = self._call_kw('set_quota_minimums', [[workspace.id]], {'cpu': 18})
        self.assertNotIn('error', response, response)
        self.assertEqual(response['result']['state'], 'synced', response)
        after = self._managed()
        self.assertEqual(after['spec_hard']['limits.cpu'], '18')
        self.assertEqual(after['spec_hard']['persistentvolumeclaims'], '12')
        self.assertEqual(after['hard']['persistentvolumeclaims'], '12')
        self.assertEqual(after['used']['persistentvolumeclaims'], '12')
        self.assertEqual(after['spec_hard']['limits.ephemeral-storage'], '80Gi')
        ws = self._ws(workspace)
        self.assertEqual(json.loads(ws.quota_expected_tokens)[_NS], after['sync_token'])
        self.assertFalse(ws.quota_pending_decrease)

    def test_blocked_workspace_recovers_by_manual_retry_after_the_operator_upgrade(self):
        """部署順序的實演：舊 operator 下 blocked → 換成新 operator → 手動重試一次 → synced。"""
        self._use(_LEGACY_URL)
        workspace = self._workspace()
        self.authenticate('admin', 'admin')
        self._call_kw('set_quota_minimums', [[workspace.id]], {'cpu': 18})
        self.assertEqual(self._ws(workspace).quota_sync_state, 'blocked')
        self._use(_OPERATOR_URL)
        response = self._call_kw('action_retry_quota_sync', [[workspace.id]])
        self.assertNotIn('error', response, response)
        self.assertEqual(response['result']['params']['type'], 'success', response)
        after = self._managed()
        self.assertEqual(after['spec_hard']['limits.cpu'], '18')
        self.assertEqual(after['spec_hard']['persistentvolumeclaims'], '12')
        ws = self._ws(workspace)
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertFalse(ws.quota_sync_reason)

    def test_cluster_pvc_below_ten_is_raised_to_the_baseline(self):
        # 叢集 PVC 上限 8 時已有 12 個 PVC 也不會被刪，只是不能再建；平台送基準 10。
        current = self._managed()
        self.client.update_namespace_quota(
            _NS, *_BASELINE, pvc_limit='8', **_EPHEMERAL,
            quota_guard={'sync_token': uuid.uuid4().hex,
                         'expected_resource_version': current['resource_version']})
        self.assertEqual(self._managed()['spec_hard']['persistentvolumeclaims'], '8')
        workspace = self._workspace()
        self.authenticate('admin', 'admin')
        response = self._call_kw('action_retry_quota_sync', [[workspace.id]])
        self.assertNotIn('error', response, response)
        self.assertEqual(self._managed()['spec_hard']['persistentvolumeclaims'], '10')
        self.assertEqual(self._ws(workspace).quota_sync_state, 'synced')
