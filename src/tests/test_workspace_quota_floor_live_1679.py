"""#1679 opt-in 真鏈路：後台管理員 RPC → 真 Odoo → 真 operator HTTP → 真 K8s 提高 quota。

不 mock client、namespace 解析或 operator；唯一的測試框架調整是讓 Odoo 的對外請求守衛
放行這個隔離 namespace 的 quota 讀寫 URL。不取代另外要求的 headed 後台 smoke。

fixture（測試外預先建立）：namespace ``dev-sandbox-quota1679-1679cafe`` 內的受管 quota
``requests/limits.cpu=8``、``requests/limits.memory=12Gi``、``persistentvolumeclaims=10``、
``requests.storage=100Gi``、ephemeral ``4Gi``／``80Gi``；operator 的 SA 只有該 namespace
的 resourcequotas／limitranges 權限。
"""
import json
import os
import time
import unittest
import uuid
from urllib.parse import urlsplit

from odoo.tests.common import HttpCase, _super_send, tagged

from ..services.paas_operator import get_paas_operator_client

_OPERATOR_URL = os.environ.get('QUOTA1679_OPERATOR_URL', '').rstrip('/')
_WS_HASH = '1679cafe'
_NAMESPACE = 'dev-sandbox-quota1679-' + _WS_HASH
_WS = 'woow_paas_platform.workspace'


# -standard：需要隔離的真 operator fixture，CI 的 `/woow_paas_platform` 不收；
# 只在明確 `--test-tags quota1679_live` 時才跑（否則 skip 會讓 C4 零 skip 守線變紅）。
@tagged('-standard', 'post_install', '-at_install', 'quota1679_live')
class TestWorkspaceQuotaFloorLive1679(HttpCase):

    @classmethod
    def _request_handler(cls, session, request, **kwargs):
        expected = urlsplit(_OPERATOR_URL + '/api/namespaces/' + _NAMESPACE + '/quota')
        actual = urlsplit(request.url)
        if (_OPERATOR_URL and request.method in ('GET', 'PUT')
                and actual.scheme == expected.scheme and actual.netloc == expected.netloc
                and actual.path == expected.path):
            return _super_send(session, request, **kwargs)
        return super()._request_handler(session, request, **kwargs)

    def _call_kw(self, method, args, kwargs=None):
        return self.url_open('/web/dataset/call_kw', data=json.dumps({
            'jsonrpc': '2.0', 'method': 'call',
            'params': {'model': _WS, 'method': method, 'args': args, 'kwargs': kwargs or {}},
        }), headers={'Content-Type': 'application/json'}).json()

    def _managed(self):
        # 推送後 quota controller 非同步更新 status.hard；等到 available（hard==spec）再比對。
        client = get_paas_operator_client(self.env)
        managed = None
        for _attempt in range(20):
            managed = client.get_namespace_quota(_NAMESPACE, include_managed=True)['managed_quota']
            if managed.get('available'):
                return managed
            time.sleep(0.5)
        self.fail('managed quota did not settle: %r' % managed)

    def test_administrator_raises_real_namespace_quota(self):
        if not _OPERATOR_URL:
            if os.environ.get('REQUIRE_QUOTA1679_LIVE') == '1':
                self.fail('QUOTA1679_OPERATOR_URL required for real quota raise gate')
            raise unittest.SkipTest('Explicit isolated real operator fixture required')
        params = self.env['ir.config_parameter'].sudo()
        params.set_param('woow_paas_platform.namespace_prefix', 'dev-sandbox-quota1679-')
        params.set_param('woow_paas_platform.operator_url', _OPERATOR_URL)
        # API key 以檔案傳入，不出現在 job 設定或 log。
        with open(os.environ['QUOTA1679_API_KEY_FILE'], encoding='utf-8') as key_file:
            params.set_param('woow_paas_platform.operator_api_key', key_file.read().strip())
        owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Live floor owner', 'login': 'live-floor-%s' % uuid.uuid4().hex,
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        workspace = self.env[_WS].create({
            'name': 'Live quota floor 1679', 'owner_id': owner.id, 'ws_hash': _WS_HASH})

        before = self._managed()
        self.assertEqual(before['uid'], os.environ['QUOTA1679_QUOTA_UID'], '前提：fixture 受管 quota')
        self.assertEqual(before['spec_hard'], {
            'requests.cpu': '8', 'limits.cpu': '8', 'requests.memory': '12Gi',
            'limits.memory': '12Gi', 'persistentvolumeclaims': '10', 'requests.storage': '100Gi',
            'requests.ephemeral-storage': '4Gi', 'limits.ephemeral-storage': '80Gi'})

        self.authenticate('admin', 'admin')
        response = self._call_kw('set_quota_minimums', [[workspace.id]],
                                 {'cpu': 18, 'storage_gi': 150})
        self.assertNotIn('error', response, response)
        self.assertEqual(response['result']['state'], 'synced', response)

        after = self._managed()
        self.assertEqual(after['uid'], before['uid'], '同一個受管 quota 就地更新')
        # CPU 與 storage 提高到最低保障；memory 公式 8Gi 低於叢集 12Gi ⇒ 保留叢集值，
        # pvc 與 ephemeral 原樣保留（完整 payload，不只本次修改的欄位）。
        self.assertEqual(after['spec_hard'], dict(
            before['spec_hard'], **{'requests.cpu': '18', 'limits.cpu': '18',
                                    'requests.storage': '150Gi'}))
        row = self._call_kw('read', [[workspace.id]], {'fields': [
            'quota_formula_cpu', 'quota_target_cpu', 'quota_target_storage', 'quota_sync_state',
            'quota_target_version', 'quota_synced_version', 'quota_synced_at',
            'quota_pending_decrease', 'quota_audit_ids']})
        self.assertNotIn('error', row, row)
        row = row['result'][0]
        self.assertEqual((row['quota_formula_cpu'], row['quota_target_cpu'],
                          row['quota_target_storage']), ('8', '18', '150Gi'))
        self.assertEqual((row['quota_sync_state'], row['quota_target_version'],
                          row['quota_synced_version']), ('synced', 1, 1))
        self.assertTrue(row['quota_synced_at'])
        self.assertIn('requests.memory', row['quota_pending_decrease'])
        self.assertEqual(len(row['quota_audit_ids']), 1)

        # 人工降低目標：拒絕且不保存，叢集原封不動（同一個 resourceVersion）。
        lowered = self._call_kw('set_quota_minimums', [[workspace.id]], {'cpu': 10})
        self.assertEqual(lowered['error']['data']['name'], 'odoo.exceptions.UserError', lowered)
        final = self._managed()
        self.assertEqual(final['resource_version'], after['resource_version'])
        self.assertEqual(final['spec_hard'], after['spec_hard'])
        workspace.invalidate_recordset()
        self.assertEqual((workspace.quota_floor_cpu, workspace.quota_target_version), (18, 1))
