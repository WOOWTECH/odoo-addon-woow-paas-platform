"""#1682 服務刪除或降級後安全自動收回額度。

外部可觀察行為：服務生命週期入口（刪除、清理、換方案）與背景重試入口 → operator 收到的
請求、叢集最後的值、後台看得到的同步狀態與重試額度。operator 只在 HTTP 邊界以有狀態的假
operator 取代（``ns_quota_fixture``）。
"""
from datetime import timedelta
import uuid
from unittest.mock import patch

from freezegun import freeze_time

from odoo.tests.common import TransactionCase, tagged

from ..services.paas_operator import PaaSOperatorAPIError
from ..services.workspace_quota import workspace_namespace
from .ns_quota_fixture import FakeQuotaOperator, build_hard
from .test_change_plan_reapply_1158 import _ChangePlanFixture
from .test_workspace_quota_floor_rpc_1679 import _WS, _QuotaRpcFixture
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


#: 刪除 sibling 之後的公式（s1 一台：vcpu 4、ram 4、storage 20 → 12／12Gi／100Gi；ephemeral 4Gi／80Gi）
_AFTER = ('12', '12Gi', '100Gi', '4Gi', '80Gi')
#: 刪除前（s1＋sibling 兩台 vcpu 4）：24／24Gi／120Gi；ephemeral 兩台仍是 floor 4Gi／80Gi
_BEFORE = ('24', '24Gi', '120Gi', '4Gi', '80Gi')
#: s1 一台實際佔用的配額：遠低於刪除後的目標
_FITTING_USED = {'requests.cpu': '4', 'limits.cpu': '4', 'requests.memory': '4Gi',
                 'limits.memory': '4Gi', 'requests.storage': '20Gi'}


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaLifecycleReclaim1682(_LifecycleFixture):
    """刪除／清理後公式下降：檢查完整用量，安全就自動收回，不需人工確認。"""

    def setUp(self):
        super().setUp()
        self.sibling = self._service('svc-1682-sibling', vcpu=4, ram=4.0, storage=20)
        self.operator.live[self.ns] = build_hard(*_BEFORE)
        self.operator.used[self.ns] = dict(_FITTING_USED)

    def _unlink(self, service=None):
        with patch(_CLIENT, return_value=self.client):
            (service or self.sibling).unlink()

    def _live(self, *keys):
        live = self.operator.live[self.ns]
        return tuple(live[key] for key in keys)

    def _assert_kept(self):
        """叢集原額度一項都沒降（全有或全無：不能只降通過檢查的那幾項）。"""
        self.assertEqual(self.operator.live[self.ns], build_hard(*_BEFORE))

    def test_deleting_a_service_reclaims_the_quota_without_confirmation(self):
        self._unlink()
        self.assertFalse(self.sibling.exists())
        self.assertEqual(self.operator.live[self.ns], build_hard(*_AFTER))
        self.assertEqual(self.operator.puts[-1], (self.ns,) + _AFTER)
        ws = self._ws()
        self.assertEqual((ws.quota_sync_state, ws.quota_sync_status), ('synced', 'synced'))
        self.assertFalse(ws.quota_pending_decrease)
        self.assertEqual(self._audit_count(), 0, '生命週期不是人工改 quota，不寫異動紀錄')

    def test_minimum_survives_and_still_bounds_the_reclaimed_target(self):
        self._set(cpu=30)
        self.assertEqual(self._live('limits.cpu'), ('30',))
        self._unlink()
        ws = self._ws()
        self.assertEqual(ws.quota_floor_cpu, 30, '最低保障不被刪除或改寫')
        self.assertEqual(self._live('requests.cpu', 'limits.cpu', 'requests.memory',
                                    'limits.memory', 'requests.storage'),
                         ('30', '30', '12Gi', '12Gi', '100Gi'))
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertEqual(self._audit_count(), 1, '只有管理員那一次')

    def test_expired_service_cleanup_reclaims_with_the_same_policy(self):
        self._cleanup(self.sibling)
        self.assertEqual(self.sibling.state, 'deleting')
        self.assertEqual(self.operator.live[self.ns], build_hard(*_AFTER))

    def test_emptied_workspace_is_reclaimed_to_the_formula_floor(self):
        self.operator.used[self.ns] = {key: '0' for key in _FITTING_USED}
        self._unlink(self.sibling | self.s1)
        self.assertEqual(self.operator.live[self.ns],
                         build_hard('8', '8Gi', '100Gi', '4Gi', '80Gi'))
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_unreleased_pods_keep_the_quota_then_it_is_reclaimed_when_released(self):
        # sibling 的 pod 還沒結束：requests.cpu 仍佔 14（高於新目標 12）。
        self.operator.used[self.ns].update({'requests.cpu': '14', 'limits.cpu': '14'})
        self._unlink()
        self.assertFalse(self.sibling.exists(), '刪除不被 quota 擋下')
        self._assert_kept()
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('unsafe_decrease', ws.quota_sync_reason)
        self.assertIn('requests.cpu', ws.quota_sync_reason)
        self.assertEqual(ws.quota_target_cpu, '12', '後台看得到最新目標')
        self.assertIn('limits.cpu: cluster 24, target 12', ws.quota_pending_decrease)
        self.assertIn('limits.memory: cluster 24Gi, target 12Gi', ws.quota_pending_decrease,
                      'memory 雖然安全，也跟著保留（不只檢查 CPU 而附帶降其他項）')
        self.assertTrue(ws.quota_next_retry_at, '用量可能稍後釋放：排有限次重試')
        # 第一次重試：仍未釋放 ⇒ 重新讀取、仍然擋下、消耗一格。
        with freeze_time(ws.quota_next_retry_at):
            self._background()
        ws = self._ws()
        self._assert_kept()
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('blocked', 1))
        # pod 結束、用量釋放 ⇒ 下一次重試收回。
        self.operator.used[self.ns] = dict(_FITTING_USED)
        with freeze_time(ws.quota_next_retry_at):
            self._background()
        ws = self._ws()
        self.assertEqual(self.operator.live[self.ns], build_hard(*_AFTER))
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('synced', 0))
        self.assertFalse(ws.quota_pending_decrease)

    def test_usage_that_never_releases_exhausts_and_can_be_retried_manually(self):
        self.operator.used[self.ns].update({'requests.cpu': '14', 'limits.cpu': '14'})
        self._unlink()
        for _attempt in range(5):
            with freeze_time(self._ws().quota_next_retry_at):
                self._background()
        ws = self._ws()
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('exhausted', 5))
        self._assert_kept()
        self.operator.used[self.ns] = dict(_FITTING_USED)
        self._retry()
        self.assertEqual(self.operator.live[self.ns], build_hard(*_AFTER))
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_limits_usage_is_checked_even_when_requests_fit(self):
        self.operator.used[self.ns]['limits.memory'] = '20Gi'
        self._unlink()
        self._assert_kept()
        self.assertIn('limits.memory', self._ws().quota_sync_reason)

    def test_retained_pvc_keeps_storage_and_the_whole_decrease(self):
        # 刪除的服務留下保留期 PVC：requests.storage 仍佔 110Gi（高於新目標 100Gi）。
        self.operator.used[self.ns]['requests.storage'] = '110Gi'
        self._unlink()
        self._assert_kept()
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('requests.storage', ws.quota_sync_reason)

    def test_missing_usage_never_lowers(self):
        self.operator.drop_used[self.ns] = {'requests.memory'}
        self._unlink()
        self._assert_kept()
        self.assertIn('used_missing', self._ws().quota_sync_reason)

    def test_unreadable_quota_never_lowers_and_never_blocks_deletion(self):
        self.operator.get_error = PaaSOperatorAPIError('API error: boom', status_code=500)
        self._unlink()
        self.assertFalse(self.sibling.exists())
        self.assertEqual(self.operator.puts, [])
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('quota_unreadable', ws.quota_sync_reason)
        self.operator.get_error = None
        with freeze_time(ws.quota_next_retry_at):
            self._background()
        self.assertEqual(self.operator.live[self.ns], build_hard(*_AFTER))

    def test_legacy_operator_never_lowers(self):
        self.operator.guarded = False
        self._unlink()
        self._assert_kept()
        self.assertIn('operator_unsupported', self._ws().quota_sync_reason)

    def test_failed_push_does_not_block_or_roll_back_deletion(self):
        self.operator.put_error = PaaSOperatorAPIError('API error: boom', status_code=500)
        self._unlink()
        self.assertFalse(self.sibling.exists())
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'pending')
        self.assertTrue(ws.quota_next_retry_at)

    def test_timeout_is_unconfirmed_and_converges_once_the_write_lands(self):
        self.operator.hold_writes = True
        self._unlink()
        self.assertFalse(self.sibling.exists())
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'unconfirmed', '逾時是結果待確認，不宣稱一定未更新')
        self.assertIn('operator_timeout', ws.quota_sync_reason)
        self.operator.hold_writes = False
        self.assertEqual(self.operator.land_held(), ['applied'])
        with freeze_time(ws.quota_next_retry_at):
            self._background()
        self.assertEqual(self.operator.live[self.ns], build_hard(*_AFTER))
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_a_newer_minimum_is_not_overwritten_by_a_late_decrease(self):
        self.operator.hold_writes = True
        self._unlink()  # 降到 12 的請求卡在 operator 裡
        self.operator.hold_writes = False
        self._set(cpu=30)  # 等待期間管理員提高
        self.assertEqual(self._live('limits.cpu'), ('30',))
        self.assertEqual(self.operator.land_held(), ['conflict'], '舊的降額請求晚到也落不下')
        self.assertEqual(self._live('limits.cpu'), ('30',))
        with freeze_time(self._ws().quota_next_retry_at or self._ws().quota_last_attempt_at):
            self._background()
        self.assertEqual(self._live('limits.cpu', 'limits.memory'), ('30', '12Gi'),
                         '背景以最新政策收回 memory，CPU 維持新的最低保障')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaDowngradeReclaim1682(_ChangePlanFixture, TransactionCase):
    """換方案降級：原有業務檢查照舊；降低的 quota 檢查用量、安全才收回。

    方案 B（vcpu 2、ram 4、storage 30）→ A（ram 2；儲存只擴不縮）：memory 目標 12Gi → 8Gi，
    CPU 8 與 storage 100Gi 都是公式下限、不變。
    """

    def _downgrade_setup(self, tag, used_memory='4Gi'):
        owner, svc = self._service(tag, plan=self.plan_b)
        ns = svc.helm_namespace
        operator = FakeQuotaOperator({ns: build_hard('8', '12Gi', '100Gi', '4Gi', '80Gi')})
        # 工作區推導出的 namespace 還沒建立（服務住在 fixture 指定的 namespace）。
        operator.namespace_exists[workspace_namespace(self.env, svc.workspace_id)] = False
        operator.used[ns] = {'requests.cpu': '2', 'limits.cpu': '2', 'requests.memory': '4Gi',
                             'limits.memory': used_memory, 'requests.storage': '30Gi'}
        client = operator.bind(self._client())
        return owner, svc, operator, client

    def _background(self, svc, client):
        with patch(_CLIENT, return_value=client):
            return svc.workspace_id.sudo()._quota_reconcile_one()

    def test_downgrade_keeps_the_business_check_and_reclaims_memory(self):
        owner, svc, operator, client = self._downgrade_setup('dn-ok')
        res = self._change(owner, svc, self.plan_a, client)
        self.assertTrue(res['success'], res.get('error'))
        pre = client.upgrade_preflight.call_args.kwargs
        self.assertEqual(pre['pending_quota']['memory_limit'], '8Gi',
                         '換方案的預檢照舊，拿的是換完之後的目標')
        live = operator.live[svc.helm_namespace]
        self.assertEqual((live['requests.memory'], live['limits.memory']), ('8Gi', '8Gi'))
        self.assertEqual((live['limits.cpu'], live['requests.storage']), ('8', '100Gi'))
        names = [c[0] for c in client.method_calls
                 if c[0] in ('upgrade_preflight', 'update_namespace_quota', 'upgrade_release')]
        self.assertEqual(names, ['upgrade_preflight', 'update_namespace_quota', 'upgrade_release'])

    def test_refused_downgrade_changes_nothing(self):
        owner, svc, operator, client = self._downgrade_setup('dn-deny')
        old_sub = svc.sudo().subscription_id
        client.upgrade_preflight.side_effect = PaaSOperatorAPIError(
            'API error: boom', status_code=500)
        res = self._change(owner, svc, self.plan_a, client)
        self.assertFalse(res['success'])
        self.assertEqual(svc.sudo().subscription_id, old_sub, '業務檢查擋下：不換訂閱')
        self.assertEqual(operator.puts, [])
        self.assertEqual(operator.live[svc.helm_namespace]['limits.memory'], '12Gi')

    def test_downgrade_waits_for_the_old_pods_before_reclaiming(self):
        # 舊 pod 還用 limits.memory 10Gi（高於新目標 8Gi）。
        owner, svc, operator, client = self._downgrade_setup('dn-wait', used_memory='10Gi')
        res = self._change(owner, svc, self.plan_a, client)
        self.assertTrue(res['success'], res.get('error'))
        client.upgrade_release.assert_called_once()
        self.assertEqual(operator.live[svc.helm_namespace]['limits.memory'], '12Gi')
        ws = svc.workspace_id.sudo()
        self.assertIn('unsafe_decrease', ws.quota_sync_reason or '')
        # 升級完成、舊 pod 結束 ⇒ 背景收回。
        operator.used[svc.helm_namespace]['limits.memory'] = '2Gi'
        for _round in range(2):  # 子集事件先排全集對帳，之後依退避重試
            with freeze_time(ws.quota_next_retry_at or ws.quota_last_attempt_at):
                self._background(svc, client)
            if operator.live[svc.helm_namespace]['limits.memory'] == '8Gi':
                break
        self.assertEqual(operator.live[svc.helm_namespace]['limits.memory'], '8Gi')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaReclaimRpc1682(_QuotaRpcFixture):
    """免確認降額只屬於伺服器內部的生命週期來源：真實登入的 RPC 偽裝不了。"""

    _FORGED = {'su': True, 'allow_lower': True, 'woow_quota_trigger': 'lifecycle',
               'woow_quota_lifecycle': True, 'woow_ws_quota_policy_write': True}

    def _error_name(self, response):
        self.assertIn('error', response, response)
        return response['error']['data']['name']

    def test_tenants_cannot_dress_a_quota_change_as_a_lifecycle_event(self):
        portal = self.env['res.users'].with_context(no_reset_password=True)._create_user_from_template({
            'name': 'Reclaim portal', 'login': 'reclaim-portal-%s' % uuid.uuid4().hex})
        portal.write({'password': portal.login})
        self.env['woow_paas_platform.workspace_access'].create({
            'workspace_id': self.workspace.id, 'user_id': portal.id, 'role': 'admin'})
        before = self._floors()
        for user in (self.owner, portal):
            self.authenticate(user.login, user.login)
            for model, method, args, kwargs in (
                (_WS, 'set_quota_minimums', [[self.workspace.id]], {'cpu': 1}),
                (_WS, 'set_quota_minimums', [[self.workspace.id]], {'cpu': 1, 'allow_lower': True}),
                (_WS, '_quota_sync', [[self.workspace.id], 'lifecycle'], {'allow_lower': True}),
                (_WS, 'write', [[self.workspace.id], {'quota_reconcile_pending': True}], {}),
                ('woow_paas_platform.cloud_service', '_push_ns_quota', [[]], {}),
            ):
                with self.subTest(user=user.login, method=method, kwargs=kwargs):
                    response = self._call_kw(model, method, args,
                                             dict(kwargs, context=self._FORGED))
                    self._error_name(response)
            with self.subTest(user=user.login, call='read'):
                self._error_name(self._call_kw(_WS, 'read', [[self.workspace.id]],
                                               {'fields': ['quota_reconcile_pending']}))
        self.assertEqual(self._floors(), before)
        self.assertEqual((self.operator.reads, self.operator.puts), ([], []))

    def test_administrator_lowering_still_needs_confirmation_under_a_forged_context(self):
        self.authenticate('admin', 'admin')
        raised = self._call_kw(_WS, 'set_quota_minimums', [[self.workspace.id]], {'cpu': 30})
        self.assertNotIn('error', raised, raised)
        self.assertEqual(self.operator.live[self.namespace]['limits.cpu'], '30')
        puts = len(self.operator.puts)
        for kwargs in ({'cpu': 0, 'context': self._FORGED},
                       {'cpu': 0, 'allow_lower': True, 'context': self._FORGED}):
            with self.subTest(kwargs=kwargs):
                self._error_name(self._call_kw(_WS, 'set_quota_minimums',
                                               [[self.workspace.id]], kwargs))
        self.assertEqual(self._floors()[0], 30, '未確認的降額不保存')
        self.assertEqual(len(self.operator.puts), puts)
        self.assertEqual(self.operator.live[self.namespace]['limits.cpu'], '30')
