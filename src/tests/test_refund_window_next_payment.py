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
