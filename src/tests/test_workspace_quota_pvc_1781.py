"""#1781 Workspace quota 同步：PVC 上限也走不降額，解除 PVC 高於 10 的工作區推送被擋。

外部可觀察行為：管理員入口／手動重試／背景／開通／生命週期 → operator 收到的請求（含
``pvc_limit``）、叢集最後的值與後台同步狀態。operator 只在 HTTP 邊界以有狀態的假 operator 取代
（``ns_quota_fixture``；``pvc_capable`` 切換新舊 operator，``pvc_write_capable`` 模擬滾動更新時
GET 與 PUT 落在不同版本的 pod）。

實際案例：prod ``paas-ws-7948b087`` 的 PVC hard=12、used=12，#1680 起停在
``blocked: would_lower: persistentvolumeclaims``。
"""
from unittest.mock import MagicMock, patch

from odoo.tests.common import BaseCase, tagged

from ..services.paas_operator import PaaSOperatorClient
from .ns_quota_fixture import FakeQuotaOperator, build_hard
from .test_workspace_quota_floor_1679 import _CLIENT
from .test_workspace_quota_sync_1680 import _SyncFixture

#: 公式（s1 一台：vcpu 4、ram 4、storage 20）：12／12Gi／100Gi，ephemeral 4Gi／80Gi
_FORMULA = ('12', '12Gi', '100Gi', '4Gi', '80Gi')


class _PvcFixture(_SyncFixture):
    """叢集 PVC 上限 12（高於 operator 舊的固定值 10），operator 支援 ``pvc_limit``。"""

    def setUp(self):
        super().setUp()
        self.operator = FakeQuotaOperator(
            {self.ns: build_hard(*_FORMULA, pvc_limit='12')}, pvc_capable=True)
        self.client = self.operator.bind()

    def _pvc(self, namespace=None):
        return self.operator.live[namespace or self.ns]['persistentvolumeclaims']


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaPvcNeverLower1781(_PvcFixture):

    def test_push_keeps_the_cluster_pvc_and_is_no_longer_blocked(self):
        self._set(cpu=18)
        self.assertEqual(self.operator.puts[-1], (self.ns, '18', '12Gi', '100Gi', '4Gi', '80Gi'))
        self.assertEqual(self.operator.pvc_sent[-1], '12')
        self.assertEqual(self._pvc(), '12')
        guard = self.operator.guards[-1]
        self.assertEqual(set(guard), {'sync_token', 'expected_resource_version'})
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertFalse(ws.quota_sync_reason)
        self.assertIn('"persistentvolumeclaims": "12"', ws.quota_synced_hard)
        self.assertFalse(ws.quota_pending_decrease, 'PVC 基準 10 不是待處理降額')

    def test_cluster_pvc_at_or_below_ten_or_missing_pushes_ten(self):
        for pvc in ('8', '10', None):
            with self.subTest(pvc=pvc):
                live = build_hard('2', '2Gi', '20Gi', '4Gi', '80Gi')
                if pvc is None:
                    del live['persistentvolumeclaims']
                else:
                    live['persistentvolumeclaims'] = pvc
                self.operator.live[self.ns] = live
                self._retry()
                self.assertEqual(self.operator.pvc_sent[-1], '10')
                self.assertEqual(self._pvc(), '10')
                self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_unreadable_cluster_pvc_is_not_provable_and_nothing_is_sent(self):
        self.operator.live[self.ns]['persistentvolumeclaims'] = 'lots'
        self._set(cpu=18)
        self.assertEqual(self.operator.puts, [])
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('quota_not_provable', ws.quota_sync_reason)

    def test_matching_retry_writes_nothing(self):
        self._set(cpu=18)
        writes = len(self.operator.puts)
        self._retry()
        self.assertEqual(len(self.operator.puts), writes, '值與 token 都相符：不重推')
        self.assertEqual(self._ws().quota_sync_state, 'synced')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaPvcOldOperator1781(_PvcFixture):
    """舊 operator（GET 沒有能力鍵）：不送 pvc_limit，行為與 #1781 之前相同。"""

    def setUp(self):
        super().setUp()
        self.operator.pvc_capable = False

    def test_old_operator_still_blocks_and_gets_no_pvc_field(self):
        self._set(cpu=18)
        self.assertEqual(self.operator.puts, [], '整筆不送：送了就會被舊 operator 寫成 10')
        self.assertEqual(self._pvc(), '12')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('would_lower: persistentvolumeclaims', ws.quota_sync_reason)
        self.assertFalse(ws.quota_next_retry_at, '永久阻擋，不自動重試')

    def test_old_operator_with_ten_or_less_sends_no_pvc_field(self):
        self.operator.live[self.ns]['persistentvolumeclaims'] = '10'
        self._set(cpu=18)
        self.assertEqual(self.operator.pvc_sent, [None])
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_blocked_workspace_recovers_by_manual_retry_after_the_operator_upgrade(self):
        """prod paas-ws-7948b087 的路徑：先 blocked，operator 升級後手動重試一次就同步。"""
        self._set(cpu=18)
        self.assertEqual(self._ws().quota_sync_state, 'blocked')
        self.operator.pvc_capable = True
        self._retry()
        self.assertEqual(self._pvc(), '12')
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '18')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertFalse(ws.quota_sync_reason)


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaPvcRolloutMismatch1781(_PvcFixture):
    """滾動更新：GET 落在新 pod（回報支援），PUT 落在舊 pod（忽略 pvc_limit、寫 10）。"""

    def test_write_that_did_not_confirm_the_pvc_limit_is_not_reported_synced(self):
        self.operator.pvc_write_capable = False
        self._set(cpu=18)
        self.assertEqual(self.operator.pvc_sent[-1], '12')
        self.assertEqual(self._pvc(), '10', '舊 pod 已把它寫成 10（偵測，不是防止）')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('pvc_limit_not_confirmed', ws.quota_sync_reason)
        self.assertFalse(ws.quota_next_retry_at, '要人處理：叢集已低於原值，平台自己補不回來')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaPvcDeployAndLifecycle1781(_PvcFixture):
    """開通（POST）與生命週期（安全降額）與一般同步共用同一套合併。"""

    def setUp(self):
        super().setUp()
        self.pending = self._service('svc-1781-new', vcpu=4, ram=4.0, storage=20)
        self.pending.sudo().write({'state': 'pending'})

    def _deploy(self):
        with patch(_CLIENT, return_value=self.client):
            self.pending._execute_deploy()

    def test_deploy_on_an_existing_quota_keeps_the_cluster_pvc(self):
        self._deploy()
        self.client.install_release.assert_called()
        self.assertEqual(self.operator.creates[-1][:2], (self.ns, '24'))
        self.assertEqual(self.operator.pvc_sent[-1], '12')
        self.assertEqual(self._pvc(), '12')
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_first_deploy_creates_the_quota_with_the_baseline(self):
        del self.operator.live[self.ns]
        self.operator.namespace_exists[self.ns] = False
        self._deploy()
        self.assertEqual(self.operator.pvc_sent[-1], '10')
        self.assertTrue(self.operator.guards[-1]['expect_absent'])
        self.assertEqual(self._pvc(), '10')
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_deploy_with_an_old_operator_still_skips_the_quota_but_installs(self):
        self.operator.pvc_capable = False
        self._deploy()
        self.assertEqual(self.operator.creates, [])
        self.client.install_release.assert_called()
        self.assertEqual(self._pvc(), '12')
        self.assertIn('would_lower: persistentvolumeclaims', self._ws().quota_sync_reason)

    def test_lifecycle_safe_decrease_still_keeps_the_cluster_pvc(self):
        # 兩台 vcpu 4（s1＋pending）→ 公式 24；叢集 30 cores、用量 0：生命週期重算可以安全
        # 降到 24，PVC 留在 12。
        self.operator.live[self.ns] = build_hard('30', '24Gi', '120Gi', '4Gi', '80Gi',
                                                 pvc_limit='12')
        with patch(_CLIENT, return_value=self.client):
            self.s1._push_ns_quota(self.workspace, self.ns)
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '24')
        self.assertEqual(self._pvc(), '12')
        self.assertEqual(self._ws().quota_sync_state, 'synced')


@tagged('post_install', '-at_install')
class TestPvcLimitClientWire1781(BaseCase):
    """client 的 HTTP body：有 ``pvc_limit`` 才送（舊呼叫端的 body 不變）。"""

    def _body(self, call, **kwargs):
        response = MagicMock(status_code=200, content=b'{}')
        response.json.return_value = {}
        client = PaaSOperatorClient(base_url='http://paas-operator:8000', api_key='k')
        with patch('requests.Session.request', return_value=response) as request:
            getattr(client, call)(**kwargs)
        return request.call_args.kwargs['json']

    def test_put_quota_sends_pvc_limit_only_when_given(self):
        args = {'namespace': 'paas-ws-a1b2c3d4', 'cpu_limit': '8', 'memory_limit': '8Gi',
                'storage_limit': '100Gi'}
        self.assertEqual(self._body('update_namespace_quota', pvc_limit='12', **args)['pvc_limit'],
                         '12')
        self.assertNotIn('pvc_limit', self._body('update_namespace_quota', **args))

    def test_create_namespace_sends_pvc_limit_only_when_given(self):
        args = {'namespace': 'paas-ws-a1b2c3d4', 'cpu_limit': '8', 'memory_limit': '8Gi',
                'storage_limit': '100Gi', 'workspace_id': 7}
        body = self._body('create_namespace', pvc_limit='12', **args)
        self.assertEqual((body['pvc_limit'], body['name']), ('12', 'paas-ws-a1b2c3d4'))
        self.assertNotIn('pvc_limit', self._body('create_namespace', **args))
