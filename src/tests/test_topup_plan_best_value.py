from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestTopupPlanBestValue(TransactionCase):
    """`is_best_value` should be computed from the highest bonus/credits ratio."""

    def setUp(self):
        super().setUp()
        # Deactivate seed plans so this test is isolated.
        self.env['woow_paas_platform.topup_plan'].search([]).write({'active': False})
        self.Plan = self.env['woow_paas_platform.topup_plan']

    def _create(self, name, credits, bonus, active=True):
        return self.Plan.create({
            'name': name,
            'amount': credits,
            'credits': credits,
            'bonus': bonus,
            'active': active,
        })

    def test_highest_ratio_wins(self):
        p100 = self._create('P100', credits=100, bonus=5)     # 5%
        p500 = self._create('P500', credits=500, bonus=30)    # 6%
        p1000 = self._create('P1000', credits=1000, bonus=100)  # 10%
        self.assertFalse(p100.is_best_value)
        self.assertFalse(p500.is_best_value)
        self.assertTrue(p1000.is_best_value)

    def test_zero_bonus_never_best(self):
        # All plans have zero bonus → none is "best value".
        p_a = self._create('A', credits=100, bonus=0)
        p_b = self._create('B', credits=200, bonus=0)
        self.assertFalse(p_a.is_best_value)
        self.assertFalse(p_b.is_best_value)

    def test_inactive_plans_excluded_from_baseline(self):
        # An inactive plan with high bonus should NOT lift the best-value baseline.
        self._create('Retired', credits=100, bonus=50, active=False)  # 50% but retired
        active = self._create('Active', credits=100, bonus=10)         # 10%
        self.assertTrue(active.is_best_value)

    def test_tie_marks_all_winners(self):
        # If two active plans share the top ratio, both are flagged.
        a = self._create('A', credits=100, bonus=10)  # 10%
        b = self._create('B', credits=200, bonus=20)  # 10%
        self.assertTrue(a.is_best_value)
        self.assertTrue(b.is_best_value)
