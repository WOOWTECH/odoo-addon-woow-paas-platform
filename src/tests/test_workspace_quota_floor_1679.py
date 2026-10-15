"""#1679 Workspace quota 最低保障：目標 = max(公式, 最低保障)。

外部可觀察行為：管理員經 ``set_quota_minimums`` 設定 → operator 收到的 quota payload；
服務生命週期（刪除）重算 → operator 收到的 payload 不會把最低保障蓋掉。
operator 只在 HTTP 邊界以有狀態的假 operator 取代（``ns_quota_fixture``）。
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

from ..models.cloud_service import _internal_provision
from ..services.workspace_quota import workspace_namespace
from .ns_quota_fixture import FakeQuotaOperator, build_hard

_CLIENT = 'odoo.addons.woow_paas_platform.services.paas_operator.get_paas_operator_client'


@tagged('post_install', '-at_install')
class TestWorkspaceQuotaFloor1679(TransactionCase):

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
