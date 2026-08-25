# -*- coding: utf-8 -*-
"""#750 第 2 項：SA 卡片規格的 `sa_max_access_policies` 0 顯示。

quota 語意（#743 拍板、`models/sh_subscription_plan_sa_quota.py` 逐字寫著）：

| 欄位 | 0 的意思 |
| --- | --- |
| `sa_max_access_policies` | **unlimited** |
| `sa_max_routes` | **none allowed**（相反） |

`_get_display_specs` 原本把兩個值塞進同一個 `'%s routes · %s policies'`，
policies 側的 0 因此被印成字面 0——與 unlimited 的語意相反。

⛔ **本檔只驗 policies 側**。routes 側顯示成什麼是 #770 的 quota 語意拍板題，
#750 刻意不代決 ⇒ 這裡把「routes 側維持印字面 0」寫成**守線**，讓「一個特判
同時蓋到兩個欄位」這種做錯法會紅。

Refs #750 #743 #770
"""
from odoo.tests.common import TransactionCase


class TestSaPoliciesQuotaDisplay750(TransactionCase):
    """`_get_display_specs` 的 security_access 分支：policies 側 0=unlimited。"""

    def _sa_plan(self, name, routes, policies):
        return self.env['sh.subscription.plan'].create({
            'name': name, 'sh_duration': 1, 'sh_unit': 'month',
            'sh_plan_price': 100, 'sh_no_of_billing_cycle': -1,
            'service_type': 'security_access',
            'sa_max_routes': routes,
            'sa_max_access_policies': policies,
        })

    def _spec_halves(self, plan):
        """回 (routes 側字串, policies 側字串)。

        用 `·` 拆開來斷言，是刻意的：本題的做錯法就是「兩個值擠在同一個字串、
        一個特判蓋到兩個」，分開比對才驗得到「兩側各自格式化」。
        """
        specs = plan._get_display_specs()
        self.assertEqual(
            len(specs), 1, 'SA plan 應恰有一條規格：%s' % specs)
        halves = [part.strip() for part in specs[0]['label'].split('·')]
        self.assertEqual(
            len(halves), 2,
            'SA 規格 label 應為「routes · policies」兩段：%s' % specs[0])
        return halves[0], halves[1]

    def test_policies_quota_zero_renders_unlimited(self):
        """policies quota=0 ⇒ 顯示 unlimited，不得印字面 0。"""
        plan = self._sa_plan('SA 750 Zero Policies', routes=3, policies=0)
        routes_half, policies_half = self._spec_halves(plan)
        self.assertIn(
            'unlimited', policies_half.lower(),
            '#743：sa_max_access_policies=0 ＝ unlimited，不能印字面 0（%s）'
            % policies_half)
        self.assertNotIn(
            '0', policies_half,
            'policies 側不該再出現任何字面 0（%s）' % policies_half)
        # 對照：routes 側該有的數字沒有被這個特判波及。
        self.assertEqual(routes_half, '3 routes')

    def test_policies_quota_positive_still_renders_number(self):
        """對照組：quota>0 仍印數字。

        沒有這條的話，「policies 一律印 unlimited」也會讓上面那條綠。
        """
        plan = self._sa_plan('SA 750 Six Policies', routes=3, policies=6)
        routes_half, policies_half = self._spec_halves(plan)
        self.assertEqual(policies_half, '6 policies')
        self.assertNotIn('unlimited', policies_half.lower())
        self.assertEqual(routes_half, '3 routes')

    def test_routes_zero_stays_literal_zero(self):
        """守線：routes 側的 0（語意＝none allowed）**不得**被順手改成
        unlimited。

        這是本單最容易做錯的一格——兩個值原本擠在同一個 label 字串，一個
        `or 'unlimited'` 就同時蓋到兩個欄位。routes 側該顯示成什麼繫於
        #770，#750 維持現狀印字面 0。
        """
        plan = self._sa_plan('SA 750 Zero Both', routes=0, policies=0)
        routes_half, policies_half = self._spec_halves(plan)
        self.assertEqual(
            routes_half, '0 routes',
            '#770：sa_max_routes=0 語意是 none allowed，本單維持印字面 0（%s）'
            % routes_half)
        self.assertNotIn('unlimited', routes_half.lower())
        # 同一個 label 裡的另一半仍要照 policies 的語意走。
        self.assertIn('unlimited', policies_half.lower())
