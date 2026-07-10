"""#327: 退點視窗改以 sh_date_of_next_payment 推導。

涵蓋：
1. auto-expire 交互護欄（釘死長壽 in_progress 成因＝上游 == miss；fix 不得破壞）
2. 長壽 in_progress 退比例（#327 核心，Task 2 red→green）
3. 守則 d（單次退點 ∈ [0, price]）、trial / 無限 cycle 邊界
"""
from datetime import date, timedelta

from unittest.mock import patch

from odoo.tests.common import TransactionCase


class TestAutoExpireInteraction(TransactionCase):
    """釘死上游 _sh_auto_expired_subscription 的 == 等值語意。

    這是長壽 in_progress 訂閱能存在（並命中退 0 bug）的 code-level 成因，
    也是本 fix「不碰 sh_end_date → auto-expire 行為零改變」的護欄。

    上游 method 在命中時會 _sh_send_subscription_email，比照上游
    test_sub010_cron_auto_expire 以 patch mock 掉 email 副作用；cron 在
    另一 recordset 改 state，呼叫後需 invalidate_recordset 重讀。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription'].sudo()
        cls.partner = cls.env['res.partner'].sudo().create({'name': 'AExp P'})

    def _in_progress_sub(self, end_offset_days):
        today = date.today()
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_plan_price': 100.0, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=30),
            'sh_end_date': today + timedelta(days=end_offset_days),
            'sh_date_of_next_payment': today + timedelta(days=15),
            'state': 'in_progress',
        })

    def test_auto_expire_hits_on_exact_end_date(self):
        """sh_end_date == today → in_progress 訂閱被 expire（無子約）。"""
        sub = self._in_progress_sub(end_offset_days=0)
        with patch.object(type(sub), '_sh_send_subscription_email',
                          return_value=None):
            self.Sub._sh_auto_expired_subscription()
        sub.invalidate_recordset()
        self.assertEqual(sub.state, 'expire')

    def test_auto_expire_misses_when_past_end_date(self):
        """sh_end_date < today → == 比對 miss → 訂閱維持 in_progress。

        這正是長壽 in_progress 的成因：一旦錯過 end_date 當天，之後
        today > end_date，== 永遠 False，訂閱靠 next_payment 續期長存。
        """
        sub = self._in_progress_sub(end_offset_days=-5)
        with patch.object(type(sub), '_sh_send_subscription_email',
                          return_value=None):
            self.Sub._sh_auto_expired_subscription()
        sub.invalidate_recordset()
        self.assertEqual(sub.state, 'in_progress')


class TestRefundWindowFromNextPayment(TransactionCase):
    """#327: 退點視窗跟隨 sh_date_of_next_payment（day-based cycle 使斷言穩定）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Sub = cls.env['sh.subscription.subscription'].sudo()
        cls.partner = cls.env['res.partner'].sudo().create({'name': 'RWin P'})

    def _sub(self, start_off, next_pay_off, end_off=None, price=100.0,
             cycle_days=30):
        """day-based cycle（sh_recurrency=cycle_days）→ cycle_days 固定、斷言穩定。

        end_off 預設 None（新公式不看 sh_end_date）；核心回歸測試明確傳
        過期 end_off 以證明「凍結 end_date 不再導致退 0」。
        """
        today = date.today()
        vals = {
            'sh_partner_id': self.partner.id,
            'sh_plan_price': price, 'sh_unit': 'day', 'sh_recurrency': cycle_days,
            'sh_start_date': today + timedelta(days=start_off),
            'sh_date_of_next_payment': today + timedelta(days=next_pay_off),
        }
        if end_off is not None:
            vals['sh_end_date'] = today + timedelta(days=end_off)
        return self.Sub.new(vals)

    # --- #327 核心：長壽 in_progress 活過凍結 end_date 仍退比例 --------------- #

    def test_long_lived_past_frozen_end_date_refunds_prorata(self):
        """凍結 sh_end_date 早已過，但 next_payment 在未來（已續期）→ 退剩餘比例。

        舊公式看 sh_end_date（過期）→ max(0, end−today)=0 → 退 0（bug）。
        新公式看 [next_payment−cycle, next_payment) → 退比例。
        """
        # cycle_days=30, next_payment=+15 → cycle_start=-15, remaining=15 → 50
        sub = self._sub(start_off=-90, next_pay_off=15, end_off=-60,
                        price=100.0, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 50.0)

    def test_refund_full_at_cycle_start(self):
        """today == cycle_start（剛續期）→ 全額退。"""
        # next_payment=+30, cycle=30 → cycle_start=today, remaining=30 → 100
        sub = self._sub(start_off=-30, next_pay_off=30, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 100.0)

    def test_refund_zero_at_next_payment_boundary(self):
        """today == next_payment（週期用盡、下期扣款日到）→ remaining 0 → 退 0。"""
        sub = self._sub(start_off=-30, next_pay_off=0, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_clamped_when_today_past_next_payment(self):
        """today > next_payment（cron 落後未推進）→ clamp 0，不為負。"""
        sub = self._sub(start_off=-40, next_pay_off=-3, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    # --- 守則 d：單次退點 ∈ [0, price]，不跨期回溯多退 ---------------------- #

    def test_refund_never_exceeds_one_cycle_price(self):
        """異常：today 落在更早週期（next_payment 遠在未來）→ 仍 clamp 到 price。"""
        # next_payment=+100 但 cycle 只有 30 → 不得退超過一週期價
        sub = self._sub(start_off=-5, next_pay_off=100, price=100.0, cycle_days=30)
        self.assertLessEqual(sub._prorata_unused_refund(), 100.0)

    # --- 邊界：trial guard / 缺欄位 / 無法解析 cycle ------------------------ #

    def test_refund_zero_during_trial(self):
        """today < sh_start_date（trial / 未開始）→ 0。"""
        sub = self._sub(start_off=7, next_pay_off=7, cycle_days=30)
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_next_payment_missing(self):
        """無 sh_date_of_next_payment → 0（新視窗來源缺失）。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id, 'sh_plan_price': 100.0,
            'sh_unit': 'day', 'sh_recurrency': 30,
            'sh_start_date': today - timedelta(days=5),
            'sh_date_of_next_payment': False,
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_cycle_unresolvable(self):
        """sh_unit 非 day/week/month/year → _cycle_delta None → 0。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id, 'sh_plan_price': 100.0,
            'sh_unit': False, 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=5),
            'sh_date_of_next_payment': today + timedelta(days=15),
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    def test_refund_zero_when_recurrency_zero(self):
        """sh_recurrency=0（壞資料）→ _cycle_delta None → 0（否則 cycle_days
        會塌成 0、退全額；plan-review advisory）。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id, 'sh_plan_price': 100.0,
            'sh_unit': 'day', 'sh_recurrency': 0,
            'sh_start_date': today - timedelta(days=5),
            'sh_date_of_next_payment': today + timedelta(days=15),
        })
        self.assertEqual(sub._prorata_unused_refund(), 0)

    # --- month-based relativedelta 路徑（範圍斷言，不依賴執行日期天數）------- #

    def test_month_cycle_prorata_is_within_bounds(self):
        """月繳（relativedelta 月路徑）中途 → 0 < refund <= price（不炸、不越界）。"""
        today = date.today()
        sub = self.Sub.new({
            'sh_partner_id': self.partner.id, 'sh_plan_price': 300.0,
            'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': today - timedelta(days=120),
            'sh_end_date': today - timedelta(days=90),          # 凍結 end 已過
            'sh_date_of_next_payment': today + timedelta(days=10),
        })
        refund = sub._prorata_unused_refund()
        self.assertGreater(refund, 0.0)
        self.assertLessEqual(refund, 300.0)
