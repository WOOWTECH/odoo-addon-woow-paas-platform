"""CloudAppTemplate.workload_type 守線（#423 W5a）。

Q3 拍板：VM 型判定用**顯式欄位**、不從 chart 嗅探。本檔釘住三件事：

1. 預設是 ``container`` → 既有 template 零行為改變；
2. 既有 seed 出來的 template 真的全部是 ``container``（不是靠「我沒改到」的
   運氣，而是可斷言的事實）；
3. Q4 拍板「v1 不預留 ``stopped`` state」—— ``state`` Selection 仍是 8 態。
"""
from odoo.tests.common import TransactionCase


class TestWorkloadTypeField(TransactionCase):
    """欄位本身的契約。"""

    def setUp(self):
        super().setUp()
        self.Template = self.env['woow_paas_platform.cloud_app_template']

    def test_default_is_container(self):
        """不指定時預設 container —— 這是「零行為改變」的根據。"""
        template = self.Template.create({
            'name': 'Default Probe',
            'slug': 'default-probe',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'probe',
            'helm_chart_version': '1.0.0',
        })

        self.assertEqual(template.workload_type, 'container')

    def test_vm_is_accepted(self):
        template = self.Template.create({
            'name': 'VM Probe',
            'slug': 'vm-probe',
            'helm_repo_url': 'https://charts.example.com',
            'helm_chart_name': 'haos',
            'helm_chart_version': '0.1.0',
            'workload_type': 'vm',
        })

        self.assertEqual(template.workload_type, 'vm')

    def test_selection_values_are_exactly_container_and_vm(self):
        """值集鎖死：多一個值就代表有人在沒改判定邏輯的情況下加了型態。"""
        field = self.Template._fields['workload_type']
        values = [v for v, _label in field.selection]

        self.assertEqual(sorted(values), ['container', 'vm'])

    def test_field_is_required(self):
        """required=True 讓「忘了設」在 create 當下就炸，而不是在 poll 時
        才以「服務永遠卡 deploying」的形式浮現。"""
        self.assertTrue(self.Template._fields['workload_type'].required)

    def test_seeded_vm_templates_are_exactly_haos(self):
        """seed 出來的 VM 型 template 恰為 ``haos``，其餘一律 container。

        原本這條寫的是「全部都是 container」（W5a 落地時 seed 還沒有 VM 型），
        #423 W5b 上架 HAOS 後改寫成白名單等值 —— 語意沒有變弱：它同時守住
        「既有 template 零行為改變」（required=True 對既有 row 由 Odoo 套
        default、不需 migration 的證據）與「沒有人順手把別的 template 標成
        vm」。用 assertEqual 而非 assertIn：多一個 VM 型 template 也要紅，
        因為 VM 路徑的就緒判定／啟停／quota 建模都是 per-template 調校過的。
        """
        vm_templates = self.Template.search([
            ('workload_type', '=', 'vm'),
        ])

        self.assertEqual(
            vm_templates.mapped('slug'), ['haos'],
            "seed 的 VM 型 template 應恰為 ['haos']，實際：%s"
            % vm_templates.mapped('slug'),
        )


class TestStoppedStateNotIntroduced(TransactionCase):
    """Q4 守線：v1 不預留 ``stopped`` state。

    欠費停權沿用既有 ``suspended`` 路徑映射 runStrategy Halted/Always
    （design §7.2 方案 C）。多一個 state 會讓計費、UI badge、以及所有
    state 過濾 domain 全部要跟著動——那是 v1.1 的事。
    """

    def test_state_selection_is_unchanged_eight_values(self):
        Service = self.env['woow_paas_platform.cloud_service']
        values = [v for v, _label in Service._fields['state'].selection]

        self.assertEqual(
            sorted(values),
            sorted([
                'pending', 'deploying', 'initializing', 'running',
                'error', 'upgrading', 'suspended', 'deleting',
            ]),
        )

    def test_stopped_is_not_a_state(self):
        Service = self.env['woow_paas_platform.cloud_service']
        values = [v for v, _label in Service._fields['state'].selection]

        self.assertNotIn(
            'stopped', values,
            "Q4 拍板 v1 不預留 stopped；要加請先回 design §7.3 走一輪",
        )
