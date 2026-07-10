"""#330 定價 seed 重定（30 檔）＋ plan taxonomy schema 測試。

對應總表 checklist G1–G6（G4 前端 picker 冒煙不在本 PR scope）：
  Task 1: TestPlanTaxonomySchema — service_type Selection 3 值＋條件式 guard
          （orphan 放行）＋ ac_max_access_seats Integer default 0
設計依據：docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md（拍板記錄）
＋ docs/plans/2026-Q3/2026-07-10-subscription-plan-matrix.md（30 檔明細）。
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestPlanTaxonomySchema(TransactionCase):
    """B1/B2 schema：service_type（3 值＋guard、orphan 放行）＋ ac_max_access_seats。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Plan = cls.env['sh.subscription.plan']
        cls.Template = cls.env['woow_paas_platform.cloud_app_template']

    def _make_plan(self, name, **extra):
        vals = {
            'name': name,
            'sh_plan_price': 100.0,
            'sh_duration': 1,
            'sh_unit': 'month',
            'sh_no_of_billing_cycle': -1,
        }
        vals.update(extra)
        return self.Plan.create(vals)

    def _make_cs_template(self, name):
        return self.Template.create({
            'name': name,
            'helm_repo_url': 'https://charts.example.test',
            'helm_chart_name': 'dummy',
            'helm_chart_version': '1.0.0',
        })

    # ---- service_type 欄位形狀（OQ-A 拍板：3 值、MCP 不設第 4 值） ----

    def test_service_type_is_selection_with_exactly_three_values(self):
        field = self.Plan._fields['service_type']
        self.assertEqual(field.type, 'selection')
        self.assertEqual(
            {key for key, _label in field.selection},
            {'cloud_service', 'smart_home', 'security_access'},
        )

    # ---- ac_max_access_seats（OQ-B 拍板定名；MAX 語意、default 0） ----

    def test_ac_max_access_seats_integer_default_zero(self):
        field = self.Plan._fields['ac_max_access_seats']
        self.assertEqual(field.type, 'integer')
        plan = self._make_plan('AC Default Plan 330')
        self.assertEqual(plan.ac_max_access_seats, 0)

    # ---- 條件式 guard（§3.3）：存值須與 owning template family 一致 ----

    def test_guard_rejects_service_type_mismatching_template_family(self):
        plan = self._make_plan('Guard Mismatch Plan 330')
        template = self._make_cs_template('Guard CS Template 330')
        template.subscription_plan_ids = [(4, plan.id)]
        with self.assertRaises(ValidationError):
            plan.service_type = 'smart_home'

    def test_guard_accepts_service_type_matching_template_family(self):
        plan = self._make_plan('Guard Match Plan 330')
        template = self._make_cs_template('Guard CS Template Match 330')
        template.subscription_plan_ids = [(4, plan.id)]
        plan.service_type = 'cloud_service'
        self.assertEqual(plan.service_type, 'cloud_service')

    def test_guard_allows_orphan_plan_any_value(self):
        """orphan（從未掛任何 template M2M）放行—— families 為空分支。
        AnythingLLM 2 筆 seed 即此形狀，backfill 不可炸。"""
        for value in ('cloud_service', 'smart_home', 'security_access'):
            plan = self._make_plan('Guard Orphan Plan 330 %s' % value)
            plan.service_type = value  # 不得 raise
            self.assertEqual(plan.service_type, value)
