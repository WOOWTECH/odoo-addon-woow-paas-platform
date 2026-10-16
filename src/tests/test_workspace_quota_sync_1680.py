"""#1680 Workspace quota 同步：版本防護、讀回、有限重試與手動恢復。

外部可觀察行為：管理員操作／背景重試入口 → operator 收到的請求（含 ``quota_guard``）與
叢集最後的值、後台看得到的同步狀態。operator 只在 HTTP 邊界以有狀態的假 operator 取代
（``ns_quota_fixture``；版本防護與遠端延遲都在那裡注入）。
"""
from datetime import timedelta
import threading
from unittest.mock import patch

from freezegun import freeze_time

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..services.paas_operator import (
    PaaSOperatorAPIError, PaaSOperatorConnectionError,
)
from ..services.workspace_quota import workspace_namespace
from .committed_cron_scope import CommittedCronScope
from .ns_quota_fixture import FakeQuotaOperator, build_hard
from .test_workspace_quota_floor_1679 import _CLIENT, _QuotaFloorFixture


class _SyncFixture(_QuotaFloorFixture):

    def _retry(self, user=None):
        with patch(_CLIENT, return_value=self.client):
            return self.workspace.with_user(user or self.admin).action_retry_quota_sync()

    def _remote_token(self):
        return self.operator.tokens.get(self.ns)

    def _background(self):
        """背景重試入口的單一工作區那一步（cron 對每個工作區各開一筆交易呼叫它）。"""
        with patch(_CLIENT, return_value=self.client):
            return self.workspace.sudo()._quota_reconcile_one()

    def _cron_triggers(self):
        cron = self.env.ref('woow_paas_platform.ir_cron_reconcile_workspace_quota')
        return self.env['ir.cron.trigger'].sudo().search([('cron_id', '=', cron.id)])

    def _audit_count(self):
        return self.env['woow_paas_platform.workspace_quota_audit'].sudo().search_count(
            [('workspace_ref', '=', self.workspace.id)])


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaGuardedSync1680(_SyncFixture):

    def test_push_carries_a_token_and_the_version_it_was_planned_on(self):
        planned = str(self.operator.rv[self.ns])
        self._set(cpu=18)
        guard = self.operator.guards[-1]
        self.assertEqual(guard['expected_resource_version'], planned)
        self.assertRegex(guard['sync_token'], r'^[a-f0-9]{32}$')
        self.assertEqual(self._remote_token(), guard['sync_token'])
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertFalse(ws.quota_sync_unguarded)

    def test_retry_with_matching_token_and_values_writes_nothing(self):
        self._set(cpu=18)
        puts = len(self.operator.puts)
        self._retry()
        self.assertEqual(len(self.operator.puts), puts)
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_legacy_operator_falls_back_to_unguarded_push_and_says_so(self):
        self.operator = FakeQuotaOperator(
            {self.ns: build_hard('12', '12Gi', '100Gi', '4Gi', '80Gi')}, guarded=False)
        self.client = self.operator.bind()
        self._set(cpu=18)
        self.assertEqual(self.operator.puts[-1], (self.ns, '18', '12Gi', '100Gi', '4Gi', '80Gi'))
        self.assertIsNone(self.operator.guards[-1], '舊 operator 不送防護欄位')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertTrue(ws.quota_sync_unguarded)
        self.assertIn('remote_guard_unavailable', ws.quota_sync_reason)

    def test_unknown_remote_token_is_fenced_and_a_higher_remote_value_is_not_lowered(self):
        """遠端有一個 DB 沒提交的較高值（交易回捲或舊請求晚到）：重推不能把它降回去。"""
        self._set(cpu=18)
        self.operator.live[self.ns] = build_hard('30', '12Gi', '100Gi', '4Gi', '80Gi')
        self.operator.tokens[self.ns] = 'deadbeef' * 4
        for _attempt in range(2):  # 手動重試兩次都一樣：不降、可見
            self._retry()
            self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '30')
            self.assertEqual(self.operator.live[self.ns]['requests.cpu'], '30')
            ws = self._ws()
            self.assertIn('limits.cpu: cluster 30, target 18', ws.quota_pending_decrease)
        # 第一次重試以新 token 圍籬（遠端 token 從此是平台認得的），第二次就不必再寫。
        self.assertNotEqual(self._remote_token(), 'deadbeef' * 4)
        self.assertEqual(self.operator.puts[-1][1], '30')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaUncertainResults1680(_SyncFixture):
    """結果不明（逾時、5xx、斷線）一律先讀回；不能假成功，也不能讓在途的舊請求成為最終值。"""

    def test_timeout_after_the_write_landed_is_read_back_as_synced(self):
        self.operator.lose_response = True
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertIn('read back: applied', ws.quota_sync_reason)
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '18')

    def test_request_still_in_flight_is_unconfirmed_and_can_never_land_after_a_newer_push(self):
        self.operator.hold_writes = True
        before = fields.Datetime.now()
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'unconfirmed')
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '12', '還沒落地')
        self.assertFalse(ws.quota_synced_version)
        self.assertGreaterEqual(ws.quota_next_retry_at, before + timedelta(minutes=1))
        self.assertTrue(self._cron_triggers().filtered(
            lambda t: t.call_at == ws.quota_next_retry_at), '有排背景重試')
        # 舊請求還卡著，管理員又提高到 20：新推送以同一版為前提，先落地。
        self.operator.hold_writes = False
        self._set(cpu=20)
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '20')
        # 卡住的 18 這時才真的執行：resourceVersion 已變，落不下。
        self.assertEqual(self.operator.land_held(), ['conflict'])
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '20')
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_in_flight_request_without_a_newer_push_is_fenced_by_the_next_retry(self):
        self.operator.hold_writes = True
        self._set(cpu=18)
        in_flight = self.operator.held[0][2]['sync_token']
        self.operator.hold_writes = False
        self._retry()  # 遠端 token 不是在途那一個 ⇒ 以新 token 重推（圍籬）
        self.assertNotEqual(self._remote_token(), in_flight)
        self.assertEqual(self.operator.land_held(), ['conflict'], '圍籬之後舊請求落不下')
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '18')

    def test_in_flight_request_that_lands_late_on_its_own_version_is_confirmed_by_read_back(self):
        self.operator.hold_writes = True
        self._set(cpu=18)
        self.operator.hold_writes = False
        self.assertEqual(self.operator.land_held(), ['applied'])
        puts = len(self.operator.puts)
        self._retry()  # 遠端正是在途那一個 token、值也是目前目標 ⇒ 確認即可，不必再寫
        self.assertEqual(len(self.operator.puts), puts)
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_timeout_with_unreadable_read_back_stays_unconfirmed(self):
        self.operator.hold_writes = True

        def read_fails_after_push(*_call):
            self.operator.get_error = PaaSOperatorConnectionError('Connection failed')
        self.operator.on_put = read_fails_after_push
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'unconfirmed')
        self.assertIn('read back failed', ws.quota_sync_reason)

    def test_5xx_that_actually_applied_is_read_back_as_synced(self):
        """operator 端 kubectl 逾時回 500，但寫入其實已套用。"""
        original = self.operator._write

        def write_then_fail(namespace, quota_args, guard):
            original(namespace, quota_args, guard)
            raise PaaSOperatorAPIError('API error: kubectl timed out', status_code=500)
        self.operator._write = write_then_fail
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertIn('operator_error: 500 (read back: applied)', ws.quota_sync_reason)

    def test_version_conflict_is_pending_and_scheduled_not_overwritten(self):
        self.operator.on_put = lambda namespace, *args: self.operator.touch(namespace)
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'pending')
        self.assertIn('quota_conflict', ws.quota_sync_reason)
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '12')
        self.assertTrue(ws.quota_next_retry_at)
        self.operator.on_put = None
        self._retry()
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '18')
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_unconfigured_operator_and_disconnect_are_pending_with_retry(self):
        with patch(_CLIENT, return_value=None):
            self.workspace.with_user(self.admin).set_quota_minimums(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'pending')
        self.assertIn('operator_not_configured', ws.quota_sync_reason)
        self.assertTrue(ws.quota_next_retry_at)
        self.operator.put_error = PaaSOperatorConnectionError('Connection failed')
        self._retry()
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'pending')
        self.assertIn('operator_error', ws.quota_sync_reason)
        self.assertTrue(ws.quota_next_retry_at)


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaRetryBudget1680(_SyncFixture):
    """自動重試有限且退避：1、5、15、60、240 分鐘，共 5 次，之後耗盡、只能手動。"""

    def _fail_with_500(self):
        self.operator.put_error = PaaSOperatorAPIError('API error: boom', status_code=500)

    def test_automatic_retries_back_off_then_exhaust_and_stop(self):
        self._fail_with_500()
        start = fields.Datetime.now()
        with freeze_time(start):
            self._set(cpu=18)
        ws = self._ws()
        self.assertEqual((ws.quota_retry_count, ws.quota_next_retry_at),
                         (0, start + timedelta(minutes=1)))
        audit = self._audit_count()
        clock = start
        for used, delay in enumerate((5, 15, 60, 240), start=1):
            with freeze_time(self._ws().quota_next_retry_at - timedelta(seconds=1)):
                self.assertEqual(self._background()['state'], 'skipped', '還沒到期不重試')
            clock = self._ws().quota_next_retry_at
            with freeze_time(clock):
                self._background()
            ws = self._ws()
            self.assertEqual(ws.quota_retry_count, used)
            self.assertEqual(ws.quota_next_retry_at, clock + timedelta(minutes=delay))
            self.assertEqual(ws.quota_sync_state, 'pending')
        puts = len(self.operator.puts)
        with freeze_time(self._ws().quota_next_retry_at):
            self._background()  # 第 5 次背景重試
        ws = self._ws()
        self.assertEqual(len(self.operator.puts), puts + 1)
        self.assertEqual(ws.quota_sync_state, 'exhausted')
        self.assertEqual(ws.quota_retry_count, 5)
        self.assertFalse(ws.quota_next_retry_at)
        self.assertIn('operator_error: 500', ws.quota_sync_reason, '最後的錯誤仍看得到')
        with freeze_time(clock + timedelta(days=2)):
            self.assertEqual(self._background()['state'], 'skipped', '耗盡後不再自動重試')
        self.assertEqual(len(self.operator.puts), puts + 1)
        self.assertEqual(self._audit_count(), audit, '背景重試不寫政策異動紀錄')
        monitor = self.env['woow_paas_platform.platform_monitor']._singleton()
        self.assertIn('stopped after 5 automatic retries', monitor.message_ids[:1].body)
        # 手動重試：重置額度並立即嘗試；仍失敗就重新開始退避。
        self._retry()
        ws = self._ws()
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('pending', 0))
        self.assertTrue(ws.quota_next_retry_at)
        self.operator.put_error = None
        self._retry()
        ws = self._ws()
        self.assertEqual((ws.quota_sync_state, ws.quota_retry_count), ('synced', 0))
        self.assertFalse(ws.quota_next_retry_at)

    def test_permanent_block_is_not_retried_automatically(self):
        self.operator.live[self.ns]['persistentvolumeclaims'] = '12'
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertFalse(ws.quota_next_retry_at, '永久阻擋不排自動重試')
        with freeze_time(fields.Datetime.now() + timedelta(days=1)):
            self.assertEqual(self._background()['state'], 'skipped')
        self._retry()  # 手動重試也不能繞過不降額
        self.assertEqual(self.operator.puts, [])
        self.assertEqual(self.operator.live[self.ns]['persistentvolumeclaims'], '12')
        self.assertIn('would_lower: persistentvolumeclaims', self._ws().quota_sync_reason)
        # 目標改變（多一台服務）時重新檢查一次，仍然擋下。
        self._service('svc-budget-b', vcpu=1, ram=1.0, storage=10)
        self._background()
        self.assertEqual(self.operator.puts, [])
        self.assertEqual(self._ws().quota_sync_state, 'blocked')

    def test_new_target_gets_a_fresh_budget_but_a_same_target_event_does_not(self):
        self._fail_with_500()
        self._set(cpu=18)
        for _i in range(2):
            with freeze_time(self._ws().quota_next_retry_at):
                self._background()
        self.assertEqual(self._ws().quota_retry_count, 2)
        # 目標改變（服務集合變了）：背景發現後以新目標重試，額度重置。
        self._service('svc-budget-c', vcpu=1, ram=1.0, storage=10)
        self._background()
        self.assertEqual(self._ws().quota_retry_count, 0)
        self.assertEqual(self.operator.puts[-1][1], '18')

    def test_synced_workspace_is_verified_periodically_and_quietly(self):
        self._set(cpu=18)
        puts, reads = len(self.operator.puts), len(self.operator.reads)
        self.assertEqual(self._background()['state'], 'skipped', '剛同步完不必讀')
        with freeze_time(fields.Datetime.now() + timedelta(hours=6, minutes=1)):
            self._background()
        self.assertGreater(len(self.operator.reads), reads, '定期讀回')
        self.assertEqual(len(self.operator.puts), puts, '一致就不寫')
        self.assertEqual(self._ws().quota_sync_state, 'synced')


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaConcurrentSync1680(TransactionCase):
    """多筆真交易（各自的 DB 連線、真 commit）＋ operator 邊界的可控延遲，驗證收斂。

    不是單線 mock 照順序呼叫：管理員推送在另一條執行緒裡、卡在 operator 回應之前持有工作區
    鎖；生命週期交易同時進來；遠端請求可以「晚到」。committed 的 fixture 由
    ``CommittedCronScope`` 記帳並在結束時精確刪除。
    """

    def setUp(self):
        super().setUp()
        self.scope = CommittedCronScope(self)
        self.addCleanup(self.scope.purge)
        self.admin_id = self.env.ref('base.user_admin').id
        with self.scope.track(), self.registry.cursor() as cr:
            env = self.scope.env(cr)
            workspace = env['woow_paas_platform.workspace'].create({'name': 'Quota 1680 concurrent'})
            self.ns = workspace_namespace(env, workspace)
            template = env['woow_paas_platform.cloud_app_template'].create({
                'name': 'Quota template 1680', 'slug': 'quota-1680-%s' % workspace.id,
                'helm_repo_url': 'https://charts.example.com', 'helm_chart_name': 'quota',
                'helm_chart_version': '1.0.0',
            })
            # 公式：vcpu 4 → 12 cores；ram 4 → 12Gi；storage 20 → 100Gi；1 台 → ephemeral 4Gi／80Gi。
            service = _internal_provision(env['woow_paas_platform.cloud_service']).create({
                'name': 'svc-1680', 'workspace_id': workspace.id, 'template_id': template.id,
                'helm_release_name': 'svc-1680', 'helm_namespace': self.ns, 'state': 'running',
                'allocated_vcpu': 4, 'allocated_ram_gb': 4.0, 'allocated_storage_gb': 20,
            })
            self.ws_id, self.service_id = workspace.id, service.id
            cr.commit()
        self.operator = FakeQuotaOperator({self.ns: build_hard('12', '12Gi', '100Gi', '4Gi', '80Gi')})
        client = self.operator.bind()
        patcher = patch(_CLIENT, return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        tracker = self.scope.track()
        tracker.__enter__()
        self.addCleanup(tracker.__exit__, None, None, None)

    # 每個 helper 都是一筆獨立交易：開新連線、做事、commit。
    def _admin_set(self, rollback=False, **values):
        with self.registry.cursor() as cr:
            env = self.scope.env(cr)
            result = env['woow_paas_platform.workspace'].browse(self.ws_id).with_user(
                self.admin_id).set_quota_minimums(**values)
            cr.rollback() if rollback else cr.commit()
            return result

    def _admin_retry(self):
        with self.registry.cursor() as cr:
            env = self.scope.env(cr)
            env['woow_paas_platform.workspace'].browse(self.ws_id).with_user(
                self.admin_id).action_retry_quota_sync()
            cr.commit()

    def _run_cron(self):
        with self.registry.cursor() as cr:
            self.scope.env(cr)['woow_paas_platform.workspace']._cron_reconcile_workspace_quotas()
            cr.commit()

    def _committed(self, *names):
        with self.registry.cursor() as cr:
            ws = self.scope.env(cr)['woow_paas_platform.workspace'].browse(self.ws_id)
            return tuple(ws[name] for name in names)

    def test_lifecycle_push_during_an_in_flight_admin_push_is_not_lost(self):
        """換方案的增額剛好撞上管理員推送：不在鎖外推送、不丟掉，背景同步收斂到兩者的結果。"""
        started, release = threading.Event(), threading.Event()

        def slow_operator(*_call):
            started.set()
            self.assertTrue(release.wait(30), 'test never released the operator')
        self.operator.on_put = slow_operator
        errors = []

        def admin_push():
            try:
                self._admin_set(cpu=18)
            except Exception as exc:  # noqa: BLE001 —— 讓主執行緒看到失敗原因
                errors.append(exc)
        thread = threading.Thread(target=admin_push)
        thread.start()
        try:
            self.assertTrue(started.wait(30), '管理員推送沒有到達 operator')
            # 管理員的請求卡在 operator、持有工作區鎖。換方案：服務升到 vcpu 8（公式 24）。
            with self.registry.cursor() as cr:
                env = self.scope.env(cr)
                service = env['woow_paas_platform.cloud_service'].browse(self.service_id)
                _internal_provision(service).write({'allocated_vcpu': 8})
                service._push_ns_quota(service.workspace_id, self.ns)
                cr.commit()
            self.assertEqual(len(self.operator.puts), 1, '生命週期沒有在鎖外推送')
        finally:
            self.operator.on_put = None
            release.set()
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '18', '管理員推送的是它的快照')
        self.assertTrue(self.scope.ids['ir.cron.trigger'], '忙碌的生命週期推送排了背景同步')
        self._run_cron()
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '24')
        self.assertEqual(self.operator.live[self.ns]['requests.cpu'], '24')
        self.assertEqual(self._committed('quota_sync_state', 'quota_floor_cpu'), ('synced', 18))

    def test_late_remote_completion_cannot_become_the_final_value(self):
        self.operator.hold_writes = True
        self._admin_set(cpu=18)  # 交易 1：請求卡在 operator、回應逾時
        self.assertEqual(self._committed('quota_sync_state')[0], 'unconfirmed')
        self.operator.hold_writes = False
        self._admin_set(cpu=20)  # 交易 2：較新的設定先落地
        self.assertEqual(self.operator.land_held(), ['conflict'], '交易 1 的請求晚到也落不下')
        self._run_cron()
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '20')
        self.assertEqual(self._committed('quota_sync_state', 'quota_synced_version'), ('synced', 2))

    def test_unconfirmed_push_is_fenced_by_the_background_retry(self):
        self.operator.hold_writes = True
        self._admin_set(cpu=18)
        self.operator.hold_writes = False
        next_retry = self._committed('quota_next_retry_at')[0]
        with freeze_time(next_retry):
            self._run_cron()
        self.assertEqual(self.operator.land_held(), ['conflict'])
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '18')
        self.assertEqual(self._committed('quota_sync_state')[0], 'synced')

    def test_remote_success_then_db_rollback_converges_without_lowering(self):
        """遠端已套用一個 DB 沒提交的較高值：照已提交政策重推等於降額 ⇒ 不降、看得到。"""
        self._admin_set(cpu=18)
        self._admin_set(rollback=True, cpu=30)  # 推送成功之後，交易失敗回捲
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '30')
        self.assertEqual(self._committed('quota_floor_cpu')[0], 18, 'DB 是已提交的權威政策')
        with freeze_time(fields.Datetime.now() + timedelta(hours=6, minutes=1)):
            self._run_cron()  # 定期讀回：遠端 token 不是平台預期的 ⇒ 圍籬重推
        self._admin_retry()  # 管理員手動重試也一樣
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '30', '不降回 18')
        state, pending = self._committed('quota_sync_state', 'quota_pending_decrease')
        self.assertEqual(state, 'synced')
        self.assertIn('limits.cpu: cluster 30, target 18', pending)
        tokens = self._committed('quota_expected_tokens')[0]
        self.assertIn(self.operator.tokens[self.ns], tokens, '遠端 token 已是平台認得的')
