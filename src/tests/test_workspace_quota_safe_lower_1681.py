"""#1681／#1682 共用：同步時的安全降額（``_quota_sync(allow_lower=...)``）。

外部可觀察行為：同步入口 → operator 收到的 PUT 與叢集最後的值、後台看得到的同步狀態。
故障與用量只在 operator 邊界注入（``ns_quota_fixture``：``used``／``drop_used``／
``guarded=False``）。管理員確認流程見 ``test_workspace_quota_lower_confirm_1681``。
"""
from unittest.mock import patch

from odoo.tests.common import tagged

from ..models.workspace_quota_policy import _internal_quota_policy_write
from .ns_quota_fixture import FakeQuotaOperator, build_hard
from .test_workspace_quota_floor_1679 import _CLIENT
from .test_workspace_quota_sync_1680 import _SyncFixture

# 公式目標（見 _QuotaFloorFixture）：12 cores／12Gi／100Gi；ephemeral 4Gi／80Gi。
HIGH = build_hard('30', '40Gi', '300Gi', '4Gi', '80Gi')


class _SafeLowerFixture(_SyncFixture):

    def setUp(self):
        super().setUp()
        self.operator.live[self.ns] = dict(HIGH)

    def _sync(self, trigger='lifecycle', **kwargs):
        with patch(_CLIENT, return_value=self.client):
            return self.workspace.sudo()._quota_sync(trigger, **kwargs)

    def _live(self, key):
        return self.operator.live[self.ns][key]


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaSafeLowerSync1681(_SafeLowerFixture):

    def test_lowers_every_resource_to_target_when_usage_fits(self):
        self.operator.used[self.ns] = {'requests.cpu': '4', 'limits.cpu': '8',
                                       'requests.memory': '6Gi', 'limits.memory': '10Gi',
                                       'requests.storage': '60Gi'}
        self._sync(allow_lower=True)
        self.assertEqual(self.operator.puts[-1], (self.ns, '12', '12Gi', '100Gi', '4Gi', '80Gi'))
        self.assertEqual(self.operator.live[self.ns],
                         build_hard('12', '12Gi', '100Gi', '4Gi', '80Gi'))
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertFalse(ws.quota_pending_decrease)
        self.assertTrue(self.operator.guards[-1]['sync_token'])

    def test_without_allow_lower_nothing_is_lowered(self):
        self._sync(allow_lower=False)
        self.assertEqual(self._live('limits.cpu'), '30')
        self.assertIn('limits.cpu: cluster 30, target 12', self._ws().quota_pending_decrease)

    def test_limits_usage_above_target_keeps_cluster_quota_and_is_retryable(self):
        # requests 只佔 4，但 limits 佔 20 > 12：降下去會讓 rollout 被 admission 拒絕。
        self.operator.used[self.ns] = {'requests.cpu': '4', 'limits.cpu': '20'}
        self._sync(allow_lower=True)
        self.assertEqual(self._live('limits.cpu'), '30')
        self.assertEqual(self._live('requests.cpu'), '30')
        # 全有或全無：memory／storage 雖然安全也不降。
        self.assertEqual(self._live('limits.memory'), '40Gi')
        self.assertEqual(self._live('requests.storage'), '300Gi')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('unsafe_decrease: limits.cpu used_exceeds', ws.quota_sync_reason)
        self.assertIn('limits.cpu: cluster 30, target 12', ws.quota_pending_decrease)
        self.assertTrue(ws.quota_next_retry_at, '用量可能釋放：要排有限次重試')
        self.assertNotEqual(ws.quota_synced_fingerprint, ws.quota_attempt_fingerprint)

    def test_retry_reads_usage_again_and_lowers_once_it_is_released(self):
        self.operator.used[self.ns] = {'limits.cpu': '20'}
        self._sync(allow_lower=True)
        self.assertEqual(self._ws().quota_sync_state, 'blocked')
        self.operator.used[self.ns] = {'limits.cpu': '12'}  # 等於目標＝安全
        self._sync('retry', allow_lower=True)
        self.assertEqual(self._live('limits.cpu'), '12')
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_requests_usage_above_target_blocks_even_when_limits_fit(self):
        self.operator.used[self.ns] = {'requests.memory': '16Gi', 'limits.memory': '8Gi'}
        self._sync(allow_lower=True)
        self.assertEqual(self._live('limits.memory'), '40Gi')
        self.assertIn('requests.memory used_exceeds', self._ws().quota_sync_reason)

    def test_retained_pvc_storage_usage_blocks_storage_decrease(self):
        # 服務刪了但 PVC 保留：requests.storage 仍佔 250Gi。
        self.operator.used[self.ns] = {'requests.storage': '250Gi'}
        self._sync(allow_lower=True)
        self.assertEqual(self._live('requests.storage'), '300Gi')
        self.assertIn('requests.storage used_exceeds', self._ws().quota_sync_reason)

    def test_increase_goes_out_while_a_decrease_is_blocked(self):
        self.operator.live[self.ns] = build_hard('8', '40Gi', '100Gi', '4Gi', '80Gi')
        self.operator.used[self.ns] = {'limits.memory': '30Gi'}
        self._sync(allow_lower=True)
        self.assertEqual(self._live('limits.cpu'), '12')
        self.assertEqual(self._live('limits.memory'), '40Gi')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        # 增額已落地：遠端 token 是平台認得的，下一次不需圍籬。
        self.assertEqual(self.operator.tokens[self.ns],
                         ws._quota_expected_tokens()[self.ns])

    def test_missing_usage_fails_closed_without_writing(self):
        self.operator.drop_used[self.ns] = {'requests.storage'}
        self._sync(allow_lower=True)
        self.assertEqual(self.operator.live[self.ns], HIGH)
        self.assertEqual(self.operator.puts, [], '值與叢集相同、token 相符：不需寫入')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('requests.storage used_missing', ws.quota_sync_reason)

    def test_unreadable_usage_fails_closed(self):
        self.operator.used[self.ns] = {'limits.cpu': 'n/a'}
        self._sync(allow_lower=True)
        self.assertEqual(self._live('limits.cpu'), '30')
        self.assertIn('limits.cpu used_invalid', self._ws().quota_sync_reason)

    def test_legacy_operator_never_lowers(self):
        self.operator = FakeQuotaOperator({self.ns: dict(HIGH)}, guarded=False)
        self.client = self.operator.bind()
        self._sync(allow_lower=True)
        self.assertEqual(self.operator.live[self.ns], HIGH)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('operator_unsupported', ws.quota_sync_reason)


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaLowerAuthorization1681(_SafeLowerFixture):
    """``allow_lower=None``（預設）：只有管理員確認過、仍等於目前版本與目標的降額才放行。"""

    def _authorize(self, target=None, version=None):
        ws = self._ws()
        target = target or self.Service.sudo()._ns_quota_payload(ws)
        key = ws._quota_lower_authorization_key(
            ws.quota_target_version if version is None else version, target)
        _internal_quota_policy_write(ws).write({'quota_lower_authorization': key})

    def test_default_does_not_lower_without_confirmation(self):
        self._sync('retry')
        self.assertEqual(self._live('limits.cpu'), '30')

    def test_confirmed_target_is_lowered_and_the_confirmation_is_used_up(self):
        self._authorize()
        self._sync('retry')
        self.assertEqual(self._live('limits.cpu'), '12')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertFalse(ws.quota_lower_authorization)
        # 叢集被別人調高後，同一個目標不能再沿用已用完的確認。
        self.operator.live[self.ns] = dict(HIGH)
        self._sync('retry')
        self.assertEqual(self._live('limits.cpu'), '30')

    def test_confirmation_for_another_target_does_not_authorize_this_one(self):
        other = dict(self.Service.sudo()._ns_quota_payload(self._ws()), cpu_limit='10')
        self._authorize(target=other)
        self._sync('retry')
        self.assertEqual(self._live('limits.cpu'), '30')

    def test_confirmation_for_an_older_policy_version_does_not_authorize(self):
        self._authorize(version=self._ws().quota_target_version - 1)
        self._sync('retry')
        self.assertEqual(self._live('limits.cpu'), '30')

    def test_confirmed_decrease_still_rechecks_usage(self):
        self._authorize()
        self.operator.used[self.ns] = {'limits.cpu': '20'}
        self._sync('retry')
        self.assertEqual(self._live('limits.cpu'), '30')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertTrue(ws.quota_lower_authorization, '仍保留，用量釋放後的重試還能用')
        self.operator.used[self.ns] = {}
        self._sync('retry')
        self.assertEqual(self._live('limits.cpu'), '12')

    def test_context_cannot_forge_a_confirmation(self):
        with patch(_CLIENT, return_value=self.client):
            self.workspace.sudo().with_context(
                allow_lower=True, woow_ws_quota_policy_write=True,
                quota_lower_authorization='x')._quota_sync('retry')
        self.assertEqual(self._live('limits.cpu'), '30')
