"""#1917 結果不明時的讀回：先比對 PVC 再看 token，#1680 之前的舊 pod 把 PVC 寫成 10 不能被記成 synced。

情境（#1683 D6 真鏈路）：工作區 PVC hard 12、used 12，叢集 quota 帶著前一次推送的 token T0。
operator 新舊 pod 並存，舊 pod 是 #1680 之前的版本（``paas-operator:20261018.0117``）：
GET 打到新 pod（回報 ``supports_pvc_limit``）⇒ 平台送 ``pvc_limit: 12`` 與新 token T1；
PUT／POST 打到舊 pod ⇒ 忽略版本防護與 ``pvc_limit``、PVC 寫成 10、token 仍是 T0；結果不明
（逾時／5xx）⇒ 讀回。修正前讀回因 token 不符（或沒有 ``sync_token`` 鍵）在比對 PVC 之前
就返回，之後背景重試讀到 10、送 ``max(10, 10)`` 記為 synced——PVC 靜默停在 10。

外部可觀察行為：管理員入口／背景重試／開通 → 叢集最後的值、後台同步狀態與 ERROR log。
operator 只在 HTTP 邊界以有狀態的假 operator 取代（``ns_quota_fixture`` 的
``legacy_write``／``legacy_read``）。
"""
from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time

from odoo import fields
from odoo.tests.common import tagged

from ..services.paas_operator import PaaSOperatorAPIError
from .test_workspace_quota_floor_1679 import _CLIENT
from .test_workspace_quota_pvc_1781 import _PvcFixture

_LOGGER = 'odoo.addons.woow_paas_platform.models.workspace_quota_policy'


class _Pre1680PodFixture(_PvcFixture):
    """PVC hard 12／used 12，先以新 operator 同步一次（叢集帶 token T0）。"""

    def setUp(self):
        super().setUp()
        self.operator.used[self.ns] = {'persistentvolumeclaims': '12'}

    def _establish_t0(self, sync):
        sync()
        self.assertEqual(self._ws().quota_sync_state, 'synced')
        self.assertEqual(self._pvc(), '12')
        return self.operator.tokens[self.ns]

    def _reads_after_the_write_hit_the_old_pod(self):
        def flip(*_call):
            self.operator.legacy_read = True
        self.operator.on_put = flip

    def _assert_pvc_loss_flagged(self, t0, logs):
        self.assertEqual(self._pvc(), '10', '舊 pod 已把它寫成 10（偵測，不是防止）')
        self.assertEqual(self.operator.tokens[self.ns], t0, '#1680 之前的 pod 不寫也不刪 token')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('pvc_limit_not_confirmed: sent 12', ws.quota_sync_reason)
        self.assertFalse(ws.quota_next_retry_at, '要人處理：叢集已低於原值，平台自己補不回來')
        self.assertTrue(any('pvc_limit=12' in line for line in logs.output), logs.output)

    def _assert_background_does_not_paper_over(self):
        """重現步驟 5：之後的背景同步不能讀到 10、送 10、記成 synced。"""
        self.operator.on_put = None
        self.operator.legacy_write = self.operator.legacy_read = False
        self.operator.lose_response = False
        writes = len(self.operator.puts) + len(self.operator.creates)
        with freeze_time(fields.Datetime.now() + timedelta(hours=6)):
            self._background()
        self.assertEqual(len(self.operator.puts) + len(self.operator.creates), writes)
        self.assertEqual(self._pvc(), '10')
        self.assertEqual(self._ws().quota_sync_state, 'blocked')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaPvcReadBackPre1680Pod1917(_Pre1680PodFixture):
    """PUT 路徑：寫入落在 #1680 之前的舊 pod，結果不明後讀回。"""

    def setUp(self):
        super().setUp()
        self.t0 = self._establish_t0(lambda: self._set(cpu=18))
        self.operator.legacy_write = True

    def test_timeout_read_back_on_a_new_pod_with_a_stale_token_flags_the_pvc(self):
        """讀回打到新 pod：token 是 T0 ≠ T1，但 PVC 已低於送出值。"""
        self.operator.lose_response = True
        with self.assertLogs(_LOGGER, level='ERROR') as logs:
            self._set(cpu=20)
        self.assertEqual(self.operator.pvc_sent[-1], '12')
        self._assert_pvc_loss_flagged(self.t0, logs)
        self._assert_background_does_not_paper_over()

    def test_timeout_read_back_on_the_old_pod_without_a_token_key_flags_the_pvc(self):
        """讀回也打到舊 pod：回應沒有 ``sync_token`` 鍵。"""
        self.operator.lose_response = True
        self._reads_after_the_write_hit_the_old_pod()
        with self.assertLogs(_LOGGER, level='ERROR') as logs:
            self._set(cpu=20)
        self._assert_pvc_loss_flagged(self.t0, logs)
        self._assert_background_does_not_paper_over()

    def test_5xx_read_back_flags_the_pvc(self):
        """operator 端寫入後回 500（kubectl 逾時之類），讀回打到新 pod。"""
        self.operator.lose_response = PaaSOperatorAPIError(
            'API error: kubectl timed out', status_code=500)
        with self.assertLogs(_LOGGER, level='ERROR') as logs:
            self._set(cpu=20)
        self._assert_pvc_loss_flagged(self.t0, logs)
        self._assert_background_does_not_paper_over()


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaPvcReadBackUnchanged1917(_Pre1680PodFixture):
    """PVC 仍等於送出值時，讀回行為不變。"""

    def setUp(self):
        super().setUp()
        self.t0 = self._establish_t0(lambda: self._set(cpu=18))

    def test_stale_token_with_the_pvc_intact_is_still_unconfirmed(self):
        """請求卡在 operator 裡（還沒寫）：token 不符、PVC 仍是 12 ⇒ 仍可能在途，可重試。"""
        self.operator.hold_writes = True
        self._set(cpu=20)
        self.assertEqual(self._pvc(), '12')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'unconfirmed')
        self.assertIn('operator_timeout', ws.quota_sync_reason)
        self.assertNotIn('pvc_limit_not_confirmed', ws.quota_sync_reason)
        self.assertTrue(ws.quota_next_retry_at)

    def test_read_back_without_a_token_key_with_the_pvc_intact_still_failed(self):
        """新 pod 寫入（PVC 12）但回應遺失，讀回打到舊 pod：讀不出 token ⇒ 仍是讀回失敗。"""
        self.operator.lose_response = True
        self._reads_after_the_write_hit_the_old_pod()
        self._set(cpu=20)
        self.assertEqual(self._pvc(), '12')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'unconfirmed')
        self.assertIn('read back failed', ws.quota_sync_reason)
        self.assertNotIn('pvc_limit_not_confirmed', ws.quota_sync_reason)
        self.assertTrue(ws.quota_next_retry_at)


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaPvcReadBackDeploy1917(_Pre1680PodFixture):
    """開通（POST ``create_namespace``）共用 ``_push_result``／``_read_back``。"""

    def setUp(self):
        super().setUp()
        self.pending = self._service('svc-1917-new', vcpu=4, ram=4.0, storage=20)
        self.pending.sudo().write({'state': 'pending'})
        self.t0 = self._establish_t0(self._retry)

    def test_deploy_timeout_read_back_with_a_stale_token_flags_the_pvc(self):
        self.operator.legacy_write = True
        self.operator.lose_response = True
        with self.assertLogs(_LOGGER, level='ERROR') as logs, \
                patch(_CLIENT, return_value=self.client):
            self.pending._execute_deploy()
        self.assertEqual(self.operator.pvc_sent[-1], '12')
        self.assertTrue(self.operator.creates, '這次寫入走開通路徑')
        self._assert_pvc_loss_flagged(self.t0, logs)
        self._assert_background_does_not_paper_over()
