"""#1679 Workspace quota 最低保障：目標 = max(公式, 最低保障)。

外部可觀察行為：管理員經 ``set_quota_minimums`` 設定 → operator 收到的 quota payload；
服務生命週期（刪除）重算 → operator 收到的 payload 不會把最低保障蓋掉。
operator 只在 HTTP 邊界以有狀態的假 operator 取代（``ns_quota_fixture``）。
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged
from odoo.tools import html2plaintext

from ..models.cloud_service import _internal_provision
from ..services.workspace_quota import workspace_namespace
from .committed_cron_scope import CommittedCronScope
from .ns_quota_fixture import FakeQuotaOperator, build_hard

_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'


class _QuotaFloorFixture(TransactionCase):
    """共用 fixture：一個工作區、一台存活服務（公式 12／12Gi／100Gi）、有狀態的假 operator。"""

    def setUp(self):
        super().setUp()
        self.admin = self.env.ref('base.user_admin')
        self.Service = self.env['woow_paas_platform.cloud_service']
        owner = self.env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Floor owner 1679', 'login': 'floor-owner-1679',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.workspace = self.env['woow_paas_platform.workspace'].create({
            'name': 'Floor 1679', 'owner_id': owner.id,
        })
        self.ns = workspace_namespace(self.env, self.workspace)
        self.template = self.env['woow_paas_platform.cloud_app_template'].create({
            'name': 'Floor template 1679', 'slug': 'floor-1679',
            'helm_repo_url': 'https://charts.example.com', 'helm_chart_name': 'floor',
            'helm_chart_version': '1.0.0',
        })
        # 公式：vcpu 4 → 12 cores；ram 4 → 12Gi；storage 20 → max(60, 100)=100Gi；1 台 → ephemeral 4Gi／80Gi。
        self.s1 = self._service('svc-floor-a', vcpu=4, ram=4.0, storage=20)
        self.operator = FakeQuotaOperator({self.ns: build_hard('12', '12Gi', '100Gi', '4Gi', '80Gi')})
        self.client = self.operator.bind()

    def _service(self, release, vcpu, ram, storage):
        return _internal_provision(self.Service).create({
            'name': release, 'workspace_id': self.workspace.id, 'template_id': self.template.id,
            'helm_release_name': release, 'helm_namespace': self.ns, 'state': 'running',
            'allocated_vcpu': vcpu, 'allocated_ram_gb': ram, 'allocated_storage_gb': storage,
        })

    def _set(self, **values):
        with patch(_CLIENT, return_value=self.client):
            return self.workspace.with_user(self.admin).set_quota_minimums(**values)

    def _ws(self):
        return self.workspace.sudo()


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaFloor1679(_QuotaFloorFixture):

    def test_minimum_above_formula_becomes_target_and_is_pushed(self):
        self._set(cpu=18)
        self.assertEqual(self.operator.puts[-1],
                         (self.ns, '18', '12Gi', '100Gi', '4Gi', '80Gi'))
        ws = self._ws()
        self.assertEqual((ws.quota_floor_cpu, ws.quota_floor_memory_gi, ws.quota_floor_storage_gi),
                         (18, 0, 0))
        self.assertEqual((ws.quota_formula_cpu, ws.quota_target_cpu), ('12', '18'))
        self.assertEqual(ws.quota_sync_state, 'synced')
        self.assertEqual(ws.quota_target_version, 1)
        self.assertEqual(ws.quota_synced_version, 1)

    def test_formula_above_minimum_wins(self):
        self._service('svc-floor-b', vcpu=4, ram=4.0, storage=20)  # 公式 cpu 24
        self.operator.live[self.ns] = build_hard('24', '24Gi', '120Gi', '4Gi', '80Gi')
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual((ws.quota_formula_cpu, ws.quota_target_cpu), ('24', '24'))
        # 目標等於叢集現況：不需要也不得推出任何低於現況的值。
        for put in self.operator.puts:
            self.assertEqual(put[1], '24')
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '24')
        self.assertEqual(ws.quota_sync_state, 'synced')

    def test_three_resources_are_independent_and_unset_follows_formula(self):
        self._set(cpu=18, memory_gi=0, storage_gi=300)
        self.assertEqual(self.operator.puts[-1],
                         (self.ns, '18', '12Gi', '300Gi', '4Gi', '80Gi'))
        ws = self._ws()
        self.assertEqual((ws.quota_target_cpu, ws.quota_target_memory, ws.quota_target_storage),
                         ('18', '12Gi', '300Gi'))
        self.assertFalse(ws.quota_floor_memory_gi)
        # 未設定要明確顯示，不能顯示成 0
        self.assertEqual((ws.quota_floor_cpu_display, ws.quota_floor_memory_display,
                          ws.quota_floor_storage_display),
                         ('18 cores', 'Not set (formula applies)', '300Gi'))

    def test_no_minimum_keeps_formula(self):
        with patch(_CLIENT, return_value=self.client):
            self.s1.unlink()
        # 沒有任何服務、沒有最低保障 → 公式下限 8／8Gi／100Gi；叢集現況較高 → 不推下降。
        for put in self.operator.puts:
            self.assertGreaterEqual(int(put[1]), 12)
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '12')

    def test_lifecycle_recompute_does_not_overwrite_minimum(self):
        sibling = self._service('svc-floor-c', vcpu=1, ram=1.0, storage=10)
        self.operator.live[self.ns] = build_hard('15', '15Gi', '100Gi', '4Gi', '80Gi')
        self._set(cpu=18)
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '18')
        with patch(_CLIENT, return_value=self.client):
            sibling.unlink()
        # 刪除後公式回到 12，但最低保障 18 不能被重算蓋掉。
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '18')
        self.assertEqual(self.operator.live[self.ns]['requests.cpu'], '18')
        self.assertEqual(self._ws().quota_target_cpu, '18')

    def test_minimum_does_not_touch_plan_billing_or_allocated(self):
        before = self.s1.read(['allocated_vcpu', 'allocated_ram_gb', 'allocated_storage_gb'])
        self._set(cpu=30, memory_gi=40, storage_gi=500)
        self.assertEqual(self.s1.read(['allocated_vcpu', 'allocated_ram_gb', 'allocated_storage_gb']),
                         before)
        self.assertEqual(self.operator.puts[-1],
                         (self.ns, '30', '40Gi', '500Gi', '4Gi', '80Gi'))


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaFloorSafety1679(_QuotaFloorFixture):
    """輸入驗證、人工降額拒絕、可見的同步狀態、版本防護與生命週期降額中間態。"""

    def test_invalid_minimums_are_rejected_and_zero_means_unset(self):
        from odoo.exceptions import ValidationError
        for value in (-1, '18Gi', 2.5, float('inf'), float('nan'), True, [18]):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self._set(cpu=value)
        self.assertEqual(self.operator.puts, [])
        self.assertFalse(self._ws().quota_floor_cpu)
        self._set(storage_gi=18.0)  # 整數值的 float 視同整數；低於公式 100Gi，目標不變
        self.assertEqual(self._ws().quota_floor_storage_gi, 18)

    def test_manual_target_decrease_is_rejected_and_not_saved(self):
        from odoo.exceptions import UserError
        self._set(cpu=18)
        puts = len(self.operator.puts)
        for values in ({'cpu': 15}, {'cpu': 0}):
            with self.subTest(values=values), self.assertRaises(UserError):
                self._set(**values)
        ws = self._ws()
        self.assertEqual((ws.quota_floor_cpu, ws.quota_target_version), (18, 1))
        self.assertEqual(len(self.operator.puts), puts)
        audit = self.env['woow_paas_platform.workspace_quota_audit'].sudo().search(
            [('workspace_ref', '=', self.workspace.id)])
        self.assertEqual(len(audit), 1)

    def test_lowering_minimum_below_formula_keeps_target_and_is_allowed(self):
        self._service('svc-floor-b', vcpu=4, ram=4.0, storage=20)  # 公式 cpu 24
        self.operator.live[self.ns] = build_hard('24', '24Gi', '120Gi', '4Gi', '80Gi')
        self._set(cpu=20)
        self._set(cpu=0)  # 清除後目標仍是公式 24，沒有下降
        ws = self._ws()
        self.assertFalse(ws.quota_floor_cpu)
        self.assertEqual((ws.quota_target_cpu, ws.quota_target_version), ('24', 2))

    def test_failure_keeps_minimum_and_shows_reason(self):
        from ..services.paas_operator import PaaSOperatorAPIError
        self.operator.put_error = PaaSOperatorAPIError('API error: boom', status_code=500)
        result = self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_floor_cpu, 18)
        self.assertEqual((ws.quota_sync_state, result['state']), ('pending', 'pending'))
        self.assertIn('operator_error: 500', ws.quota_sync_reason)
        self.assertFalse(ws.quota_synced_version)

    def test_timeout_is_marked_unconfirmed_not_success(self):
        from ..services.paas_operator import PaaSOperatorTimeoutError
        self.operator.put_error = PaaSOperatorTimeoutError('Request timeout')
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'unconfirmed')
        self.assertIn('operator_timeout', ws.quota_sync_reason)
        self.assertFalse(ws.quota_synced_version)

    def test_unreadable_cluster_blocks_push(self):
        from ..services.paas_operator import PaaSOperatorConnectionError
        self.operator.get_error = PaaSOperatorConnectionError('Connection failed')
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual((ws.quota_floor_cpu, ws.quota_sync_state), (18, 'blocked'))
        self.assertIn('quota_unreadable', ws.quota_sync_reason)
        self.assertEqual(self.operator.puts, [])

    def test_full_payload_is_checked_including_pvc_and_ephemeral(self):
        # 叢集的 pvc 被人工調到 12：operator 固定寫 10 會降低它 ⇒ 整筆不送。
        self.operator.live[self.ns]['persistentvolumeclaims'] = '12'
        self._set(cpu=18)
        self.assertEqual(self.operator.puts, [])
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('would_lower: persistentvolumeclaims', ws.quota_sync_reason)
        # ephemeral 叢集值較高：保留叢集值而不是推回公式 80Gi。
        self.operator.live[self.ns]['persistentvolumeclaims'] = '10'
        self.operator.live[self.ns]['limits.ephemeral-storage'] = '200Gi'
        self._set(cpu=19)
        self.assertEqual(self.operator.puts[-1],
                         (self.ns, '19', '12Gi', '100Gi', '4Gi', '200Gi'))

    def test_cluster_values_in_other_units_are_compared_numerically(self):
        self.operator.live[self.ns].update({'requests.cpu': '20000m', 'limits.memory': '13312Mi'})
        self._set(cpu=18)
        # 20000m＝20 cores、13312Mi＝13Gi，都高於目標 ⇒ 保留叢集的原字串，不降。
        self.assertEqual(self.operator.puts[-1][1:3], ('20000m', '13312Mi'))

    def test_old_response_cannot_mark_newer_setting_synced(self):
        from ..models.workspace_quota_policy import _internal_quota_policy_write

        def newer_setting_arrives(*_call):
            ws = self._ws()
            _internal_quota_policy_write(ws).write({'quota_target_version': ws.quota_target_version + 1})
        self.operator.on_put = newer_setting_arrives
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_target_version, 2)
        self.assertNotEqual(ws.quota_sync_state, 'synced')
        self.assertIn('superseded', ws.quota_sync_reason)
        self.assertFalse(ws.quota_synced_version)

    def test_confirmed_missing_namespace_saves_policy_for_first_deploy(self):
        self.s1.sudo().write({'state': 'pending'})
        del self.operator.live[self.ns]
        self.operator.namespace_exists[self.ns] = False
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual((ws.quota_floor_cpu, ws.quota_sync_state), (18, 'pending_first_deploy'))
        self.assertEqual((self.operator.puts, self.operator.creates), ([], []))

    def test_unknown_namespace_existence_is_not_treated_as_missing(self):
        del self.operator.live[self.ns]
        self.operator.namespace_exists[self.ns] = None
        self._set(cpu=18)
        ws = self._ws()
        self.assertEqual(ws.quota_sync_state, 'blocked')
        self.assertIn('managed_quota_not_found', ws.quota_sync_reason)
        self.assertEqual((self.operator.puts, self.operator.creates), ([], []))

    def test_lifecycle_decrease_is_visible_and_never_blocks_deletion(self):
        sibling = self._service('svc-floor-b', vcpu=4, ram=4.0, storage=20)
        self.operator.live[self.ns] = build_hard('24', '24Gi', '120Gi', '4Gi', '80Gi')
        with patch(_CLIENT, return_value=self.client):
            sibling.unlink()
        self.assertFalse(sibling.exists())
        self.assertEqual(self.operator.live[self.ns]['limits.cpu'], '24')
        ws = self._ws()
        self.assertIn('limits.cpu: cluster 24, target 12', ws.quota_pending_decrease)
        # operator 讀不到時刪除仍完成，且不送任何 quota
        from ..services.paas_operator import PaaSOperatorConnectionError
        self.operator.get_error = PaaSOperatorConnectionError('Connection failed')
        puts = len(self.operator.puts)
        with patch(_CLIENT, return_value=self.client):
            self.s1.unlink()
        self.assertFalse(self.s1.exists())
        self.assertEqual(len(self.operator.puts), puts)

    def test_change_is_audited_with_original_operator(self):
        self._set(cpu=18, storage_gi=300)
        audit = self.env['woow_paas_platform.workspace_quota_audit'].sudo().search(
            [('workspace_ref', '=', self.workspace.id)])
        self.assertEqual(len(audit), 1)
        self.assertEqual((audit.actor_uid, audit.actor_login, audit.target_version),
                         (self.admin.id, self.admin.login, 1))
        self.assertEqual(audit.old_values, 'cpu=-, memory=-, storage=-')
        self.assertEqual(audit.new_values, 'cpu=18, memory=-, storage=300Gi')
        monitor = self.env['woow_paas_platform.platform_monitor']._singleton()
        message = monitor.message_ids[:1]
        self.assertEqual(message.author_id, self.admin.partner_id)
        body = html2plaintext(message.body)
        self.assertIn('cpu=-, memory=-, storage=- -> cpu=18, memory=-, storage=300Gi', body)
        self.assertIn(self.admin.login, body)
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            audit.write({'new_values': 'forged'})
        with self.assertRaises(AccessError):
            audit.unlink()


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaFloorDeploy1679(_QuotaFloorFixture):
    """開通路徑（``_execute_deploy`` → ``create_namespace``）套用同一個政策。"""

    def setUp(self):
        super().setUp()
        self.template.sudo().write({'helm_chart_version': '1.0.0'})
        self.pending = self._service('svc-floor-new', vcpu=4, ram=4.0, storage=20)
        self.pending.sudo().write({'state': 'pending'})

    def _deploy(self):
        with patch(_CLIENT, return_value=self.client):
            self.pending._execute_deploy()

    def test_first_deploy_applies_minimum_saved_before_namespace_exists(self):
        del self.operator.live[self.ns]
        self.operator.namespace_exists[self.ns] = False
        self.s1.sudo().write({'state': 'pending'})
        self._set(cpu=30)
        self.assertEqual(self._ws().quota_sync_state, 'pending_first_deploy')
        self._deploy()
        # 兩台 vcpu 4 → 公式 24；最低保障 30 ⇒ 首次建立就是 30。
        self.assertEqual(self.operator.creates[-1][:2], (self.ns, '30'))
        self.client.install_release.assert_called()
        self.assertEqual(self._ws().quota_sync_state, 'synced')

    def test_deploy_on_existing_namespace_never_lowers_cluster(self):
        self.operator.live[self.ns] = build_hard('40', '12Gi', '100Gi', '4Gi', '80Gi')
        self._deploy()
        # 兩台 → 公式 24，但叢集是 40 ⇒ 保留 40，不能被開通的重套降下來。
        self.assertEqual(self.operator.creates[-1][:3], (self.ns, '40', '24Gi'))
        self.assertIn('limits.cpu: cluster 40, target 24', self._ws().quota_pending_decrease)

    def test_deploy_skips_quota_when_cluster_has_more_pvcs_but_still_installs(self):
        self.operator.live[self.ns]['persistentvolumeclaims'] = '12'
        self._deploy()
        self.assertEqual(self.operator.creates, [])
        self.client.install_release.assert_called()
        self.assertEqual(self.operator.live[self.ns]['persistentvolumeclaims'], '12')
        self.assertIn('would_lower: persistentvolumeclaims', self._ws().quota_sync_reason)

    def test_deploy_fails_when_namespace_quota_is_unreadable(self):
        from ..services.paas_operator import PaaSOperatorConnectionError
        self.operator.get_error = PaaSOperatorConnectionError('Connection failed')
        self._deploy()
        self.assertEqual(self.pending.state, 'error')
        self.assertIn('could not be verified', self.pending.error_message)
        self.assertEqual(self.operator.creates, [])
        self.client.install_release.assert_not_called()


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaFloorLocking1679(TransactionCase):
    """用真的第二、第三條 DB 連線驗 quota 鎖（不是 mock）。

    另一筆交易拿著工作區這一列（例如管理員正在推送）時，開通與生命週期路徑不等鎖，
    直接不讀、不送、也不寫這一列：鎖外推送可能蓋掉對方剛提高的值，寫這一列則會在對方
    commit 後 40001、在已呼叫 operator 之後整筆回捲。本交易快照比對方的 commit 舊也一樣。
    committed 的 fixture 由 ``CommittedCronScope`` 記帳並在結束時精確刪除。
    """

    def test_busy_workspace_is_neither_pushed_nor_written(self):
        scope = CommittedCronScope(self)
        self.addCleanup(scope.purge)
        with scope.track(), self.registry.cursor() as cr:
            workspace = scope.env(cr)['woow_paas_platform.workspace'].create(
                {'name': 'Floor 1679 lock'})
            ws_id, table = workspace.id, workspace._table
            cr.commit()
        namespace = 'dev-sandbox-1679-lock'
        operator = FakeQuotaOperator({namespace: build_hard('8', '8Gi', '100Gi', '4Gi', '80Gi')})
        client = operator.bind()

        with self.registry.cursor() as holder, self.registry.cursor() as contender:
            ws = scope.env(contender)['woow_paas_platform.workspace'].browse(ws_id)
            # 釘住 contender 的快照（REPEATABLE READ：第一個查詢就決定）；若實作不顧鎖仍寫這一列，
            # statement_timeout 讓它報錯而不是和 holder 互等到天荒地老。
            contender.execute(f'SELECT id FROM {table} WHERE id = %s', (ws_id,))
            contender.execute("SET LOCAL statement_timeout = '10s'")
            holder.execute(f'SELECT id FROM {table} WHERE id = %s FOR NO KEY UPDATE', (ws_id,))
            self.assertEqual(ws._quota_create_namespace(client, namespace),
                             (False, 'quota_sync_busy'), '別人拿著鎖：開通不送、待重試')
            with patch(_CLIENT, return_value=client):
                result = ws._quota_sync('lifecycle', namespaces=[namespace], nowait=True)
            self.assertEqual(result['state'], 'busy', '別人拿著鎖：生命週期不送、不寫')
            self.assertEqual((operator.reads, operator.creates, operator.puts), ([], [], []))
            holder.execute(f'UPDATE {table} SET quota_target_version = 1 WHERE id = %s', (ws_id,))
            holder.commit()
            self.assertEqual(ws._quota_create_namespace(client, namespace),
                             (False, 'quota_sync_busy'), '快照比別人的 commit 舊：40001 也算忙碌')
            self.assertEqual((operator.reads, operator.creates), ([], []))
            contender.commit()

        with self.registry.cursor() as fresh:
            fresh.execute(f'SELECT quota_sync_state, quota_target_version FROM {table} WHERE id = %s',
                          (ws_id,))
            self.assertEqual(fresh.fetchone(), (None, 1), '忙碌時沒有寫入同步狀態')
            ws = scope.env(fresh)['woow_paas_platform.workspace'].browse(ws_id)
            self.assertEqual(ws._quota_create_namespace(client, namespace), (True, None))
            self.assertEqual(operator.creates[-1][0], namespace)
            fresh.rollback()
