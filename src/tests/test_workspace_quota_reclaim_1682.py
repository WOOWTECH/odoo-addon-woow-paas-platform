"""#1682 服務刪除或降級後安全自動收回額度。

外部可觀察行為：服務生命週期入口（刪除、清理、換方案）與背景重試入口 → operator 收到的
請求、叢集最後的值、後台看得到的同步狀態與重試額度。operator 只在 HTTP 邊界以有狀態的假
operator 取代（``ns_quota_fixture``）。
"""
from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time

from odoo.tests.common import tagged

from ..services.paas_operator import PaaSOperatorAPIError
from .ns_quota_fixture import build_hard
from .test_workspace_quota_floor_1679 import _CLIENT
from .test_workspace_quota_sync_1680 import _SyncFixture


class _LifecycleFixture(_SyncFixture):

    def _cleanup(self, service):
        """``subscription_lifecycle_cron`` 每天對已取消且到期、尚未被收掉的服務做的那一步。"""
        with patch(_CLIENT, return_value=self.client):
            service._do_cleanup()

    def _deleting_service(self, release, namespace=None):
        """已在 deleting 的服務：不算進公式，所以清理它是「目標不變」的生命週期事件。"""
        service = self._service(release, vcpu=2, ram=2.0, storage=10)
        service.sudo().write({'state': 'deleting', 'helm_namespace': namespace or self.ns})
        return service

    def _legacy_namespace(self, pvc=None):
        """工作區裡第二個 namespace（舊服務住的地方）。"""
        other = self.ns + '-legacy'
        self.operator.live[other] = build_hard('12', '12Gi', '100Gi', '4Gi', '80Gi')
        if pvc:
            self.operator.live[other]['persistentvolumeclaims'] = pvc
        legacy = self._service('svc-1682-legacy', vcpu=1, ram=1.0, storage=10)
        legacy.sudo().write({'helm_namespace': other})
        return other

    def _exhaust(self):
        """管理員提高 → operator 一直 500 → 背景重試 5 次後耗盡。"""
        self.operator.put_error = PaaSOperatorAPIError('API error: boom', status_code=500)
        self._set(cpu=18)
        for _attempt in range(5):
            with freeze_time(self._ws().quota_next_retry_at):
                self._background()
        ws = self._ws()
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('exhausted', 5))

    def _exhausted_notices(self):
        monitor = self.env['woow_paas_platform.platform_monitor']._singleton()
        return len(monitor.message_ids.filtered(
            lambda m: 'stopped after 5 automatic retries' in (m.body or '')))


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaLifecycleRetryBudget1682(_LifecycleFixture):
    """目標不變的生命週期事件不重置自動重試額度（#1682 留言 22779）。"""

    def _assert_still_exhausted_after(self, event, extra_puts):
        notices = self._exhausted_notices()
        puts = len(self.operator.puts)
        event()
        # 下一輪背景（子集事件排了全集對帳）：最多再試一次，不會重新拿到 5 次。
        self._background()
        ws = self._ws()
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('exhausted', 5))
        self.assertFalse(ws.quota_next_retry_at, '耗盡後不排自動重試')
        self.assertEqual(len(self.operator.puts), puts + extra_puts)
        for days in (1, 2):
            with freeze_time(ws.quota_last_attempt_at + timedelta(days=days)):
                self.assertEqual(self._background()['state'], 'skipped')
        self.assertEqual(len(self.operator.puts), puts + extra_puts, '之後不再自動重試')
        self.assertEqual(self._exhausted_notices(), notices, '不是新的耗盡，不重複通知')

    def test_single_namespace_same_target_cleanup_keeps_an_exhausted_budget(self):
        stale = self._deleting_service('svc-1682-stale')
        self._exhaust()
        # 同目標的單一 namespace 事件：生命週期推一次，背景不需要對帳。
        self._assert_still_exhausted_after(lambda: self._cleanup(stale), extra_puts=1)

    def test_multi_namespace_same_target_cleanup_keeps_an_exhausted_budget(self):
        self._legacy_namespace()
        stale = self._deleting_service('svc-1682-stale')
        self._exhaust()
        fingerprint = self._ws().quota_attempt_fingerprint
        # 生命週期只推自己的 namespace（1 次），下一輪背景以全集重評（2 個 namespace 各 1 次）。
        self._assert_still_exhausted_after(lambda: self._cleanup(stale), extra_puts=3)
        self.assertEqual(self._ws().quota_attempt_fingerprint, fingerprint,
                         '指紋不在子集與全集之間來回')

    def test_repeated_daily_cleanups_do_not_add_retries(self):
        self._legacy_namespace()
        stale = self._deleting_service('svc-1682-stale')
        self._exhaust()
        puts = len(self.operator.puts)
        for _day in range(3):
            self._cleanup(stale)
            self._background()
            ws = self._ws()
            self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('exhausted', 5))
        self.assertEqual(len(self.operator.puts), puts + 3 * 3,
                         '每次事件固定 1＋全集 2 次，不再多出 5 次重試')

    def test_subset_success_is_not_shown_as_a_synced_workspace_until_the_full_check(self):
        self._legacy_namespace(pvc='12')  # 這個 namespace 永久阻擋
        stale = self._deleting_service('svc-1682-stale')
        self._set(cpu=18)
        self.assertEqual(self._ws().quota_sync_state, 'blocked')
        self._cleanup(stale)
        self.assertEqual(self._ws().quota_sync_status, 'pending',
                         '只同步了子集：不能顯示整個工作區已同步')
        self.assertNotEqual(self._background()['state'], 'skipped', '排了全集對帳')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('would_lower: persistentvolumeclaims', ws.quota_sync_reason)
        self.assertEqual(self._background()['state'], 'skipped', '全集評估過一次就不再對帳')
