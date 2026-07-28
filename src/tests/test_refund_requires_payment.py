"""#324 part (b): 退點前提＝本訂閱有成功扣款（done deduct）。

堵住套利路徑：訂閱→付款靜默失敗（服務照跑）→刪服務→退未賺得點。
never-paid（只有 failed deduct 或無交易）刪服務退 0；真已付（有 done
deduct）仍按剩餘比例退（#318 happy path 不回歸）。

#437：本檔**不讀真實日曆**，全程跑在固定測試時鐘下（見 ``_today_patcher``）。
原本 fixture 用 ``date.today() ± 15 天`` 建視窗、斷言退 50.0，但實作（#327）的
週期長度是 ``sh_date_of_next_payment`` **前一個月的實際天數**（28/29/30/31），
只有**偶數**長度才存在「整數天＝剛好一半」的解（28→14、30→15；29 / 31 無解）
——實測一年 365 天裡有 244 天會反推出奇數週期而紅（48.39 / 53.57 / 51.72
!= 50.0），剩下 121 天綠。這是「假綠家族」的另一型：綠不代表對，是今天剛好。

修法是釘住時鐘、讓「中點退一半」這個語意恆成立，**不動實作**——實作用
next_payment 反推視窗是 #327 刻意為之（涵蓋 sh_end_date 已凍結過期的 cycle-2+
長壽訂閱）。跨四種週期長度的不變式另見 ``TestProrataAcrossCycleLengths``。
"""
from datetime import date, timedelta
from unittest.mock import patch

from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.tests.common import TransactionCase

_PLAN_PRICE = 100.0

# 固定測試時鐘：next_payment 的前一個月是 4 月（30 天）⇒ 實作反推的視窗恰 30
# 天，today 落在正中（剩 15 天）⇒ 純公式恆為 100.0 × 15 / 30 = 50.0。
_NEXT_PAYMENT = date(2026, 5, 16)
_CYCLE_START = date(2026, 4, 16)   # == _NEXT_PAYMENT − relativedelta(months=1)
_FROZEN_TODAY = date(2026, 5, 1)   # == _CYCLE_START + 15 天 == _NEXT_PAYMENT − 15 天


def _today_patcher(day):
    """回一個把 ``fields.Date.today()`` 釘在 ``day`` 的 patcher（不引 freezegun）。

    ``_prorata_unused_refund`` 唯一的時間來源就是 ``fields.Date.today()``，patch
    它即可讓測試脫離真實日曆。可當 context manager（``with _today_patcher(d):``）
    或在 ``setUp`` 用 ``start()`` + ``addCleanup(stop)``。

    **單一 target 就夠**：這裡 patch 的是 class attribute
    ``odoo.fields.Date.today``，而所有 ``from odoo import fields`` 的 module 共用
    同一個 ``fields`` module object 與同一個 ``Date`` class，rebind 一次全部看得
    到。這與 ``docs/decisions/patterns/unittest-mock-dual-namespace-patch.md``
    講的情境不同——那是 module-level symbol 被 ``from m import name`` 複製進 N
    個 namespace，才需要 N 個 patch target。

    **fail-loud 保證**：本檔所有情境日期都在過去，patch 一旦失效（例如實作改
    成直接呼叫 ``datetime.date.today()``、繞過 ``fields.Date``），真實 today 早
    已越過 next_payment → clamp 後退 0 ≠ 斷言值 → 立刻紅，不會靜默退化成
    「mock 變死碼」的假綠。
    """
    return patch.object(fields.Date, 'today', staticmethod(lambda *args: day))


class TestRefundRequiresPayment(TransactionCase):
    def setUp(self):
        super().setUp()
        patcher = _today_patcher(_FROZEN_TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTx = self.env['wallet.transaction'].sudo()
        self.partner = self.env['res.partner'].create({'name': 'Gate Partner'})

    def _mid_cycle_sub(self):
        """固定時鐘下、today 正好落在「當期視窗」正中點的 in_progress 訂閱。

        這裡的「視窗」是實作（#327）從 ``sh_date_of_next_payment`` **反推**的
        ``[next_payment − relativedelta(months=1), next_payment)``，**不是**
        ``[sh_start_date, sh_end_date]``。舊 docstring 寫的「[start, end] 中點」
        在實作的座標系裡是錯的，正是 #437 的成因——兩套座標系剛好對得上時綠、
        對不上時紅。
        """
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_plan_price': _PLAN_PRICE, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': _CYCLE_START,
            'sh_end_date': _NEXT_PAYMENT,
            'sh_date_of_next_payment': _NEXT_PAYMENT,
            'state': 'in_progress',
        })

    def _seed_deduct(self, sub, state):
        return self.WalletTx.create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -_PLAN_PRICE, 'subscription_id': sub.id, 'state': state,
        })

    def test_frozen_clock_puts_today_at_exact_window_midpoint(self):
        """fixture 自我守衛：時鐘真的被釘住，且反推視窗恰 30 天、today 是正中點。

        改 ``_FROZEN_TODAY`` / ``_NEXT_PAYMENT`` 而破壞這個前提時，先在這裡炸並
        講清楚原因，而不是讓下游丟出無從解讀的 ``48.39 != 50.0``（#437）。
        """
        self.assertEqual(
            fields.Date.today(), _FROZEN_TODAY,
            '測試時鐘沒被釘住——patch target 失效，下面的斷言都不可信')
        self.assertEqual(
            _NEXT_PAYMENT - relativedelta(months=1), _CYCLE_START,
            '_CYCLE_START 必須等於實作反推的視窗起點')
        self.assertEqual(
            (_NEXT_PAYMENT - _CYCLE_START).days, 30,
            '視窗長度必須是偶數天才湊得出「剛好一半」；29 / 31 天無整數解')
        self.assertEqual(
            (_NEXT_PAYMENT - _FROZEN_TODAY).days, 30 // 2,
            'today 必須落在視窗正中點，50.0 這個斷言值才有語意')

    def test_never_paid_refunds_zero(self):
        """完全無扣款交易 → 退 0（服務照跑但從未成功付款）。"""
        sub = self._mid_cycle_sub()
        self.assertEqual(sub._compute_unused_refund(), 0)

    def test_only_failed_deduct_refunds_zero(self):
        """只有 failed deduct（#324 類）→ 退 0。"""
        sub = self._mid_cycle_sub()
        self._seed_deduct(sub, 'failed')
        self.assertEqual(sub._compute_unused_refund(), 0)

    def test_done_deduct_refunds_prorata(self):
        """有 done deduct（真已付）→ 按剩餘比例退（中點＝退半價）。"""
        sub = self._mid_cycle_sub()
        self._seed_deduct(sub, 'done')
        self.assertEqual(sub._compute_unused_refund(), _PLAN_PRICE / 2)

    def test_prorata_formula_still_reachable_without_gate(self):
        """純公式方法本身不受 gate 影響（Task 2 的抽取仍在）。"""
        sub = self._mid_cycle_sub()
        self.assertEqual(sub._prorata_unused_refund(), _PLAN_PRICE / 2)

    def test_settle_on_delete_never_paid_credits_zero(self):
        """never-paid in_progress 訂閱刪除結算：取消但退 0（不得退未賺得點）。"""
        sub = self._mid_cycle_sub()          # 無 done deduct
        credited = sub._settle_on_entity_delete()
        self.assertEqual(sub.state, 'cancel')  # 仍取消以止付
        self.assertEqual(credited, 0.0)        # 但不退未賺得點
        self.assertFalse(self.WalletTx.search([
            ('partner_id', '=', self.partner.id),
            ('transaction_type', '=', 'refund')]))

    def test_manual_invoice_pay_stamps_subscription_and_enables_refund(self):
        """#324 F1 回歸：手動付款端點 invoice._pay_from_wallet()（不帶
        subscription）付掉訂閱週期發票時，deduct 要 stamp 上 subscription_id，
        退點 gate 才認得 → 真已付的週期刪服務仍按比例退（不因手動付款誤退 0）。"""
        company = self.env.company.sudo()
        journal = self.env['account.journal'].sudo().search(
            [('company_id', '=', company.id), ('type', 'in', ('bank', 'cash'))],
            limit=1)
        if not journal:
            self.env['account.journal'].sudo().create({
                'name': 'Test Bank 324', 'type': 'bank', 'code': 'TB324',
                'company_id': company.id})
        sub = self._mid_cycle_sub()
        invoice = self.env['account.move'].sudo().create({
            'move_type': 'out_invoice',
            'partner_id': self.partner.id,
            'sh_subscription_id': sub.id,
            'invoice_line_ids': [(0, 0, {
                'name': 'cycle', 'quantity': 1, 'price_unit': _PLAN_PRICE,
                'tax_ids': [(5, 0, 0)],
            })],
        })
        self.partner.sudo().add_wallet_points(_PLAN_PRICE, description='seed')
        self.partner.invalidate_recordset(['wallet_balance'])

        invoice._pay_from_wallet()  # 手動付款路徑：不帶 subscription

        # deduct 已被 stamp → gate 認得 → 退點回純比例（非 0）
        self.assertTrue(sub._has_paid_current_cycle())
        self.assertEqual(sub._compute_unused_refund(), _PLAN_PRICE / 2)


class TestProrataAcrossCycleLengths(TransactionCase):
    """#437：同一組退點不變式在 28 / 29 / 30 / 31 天四種日曆週期下都要成立。

    ``_prorata_unused_refund`` 的週期長度＝``sh_date_of_next_payment`` 前一個月
    的實際天數，四種長度在一年裡都會出現，所以逐一模擬「今天」跑一遍——這就是
    「不再日期敏感」的證明，而不是靠今天恰好是好日子。

    斷言刻意用**邊界與單調性**（整期未用→全退、期末→退 0、逾期→仍 0、逐日嚴格
    遞減、恆 ∈ [0, plan 價]），而不是把實作公式抄一遍反算期望值——後者是拿實作
    驗實作，公式寫錯也測不出來。
    """

    def setUp(self):
        super().setUp()
        self.Sub = self.env['sh.subscription.subscription'].sudo()
        self.WalletTx = self.env['wallet.transaction'].sudo()
        self.partner = self.env['res.partner'].create({'name': 'Cycle Partner'})

    def _long_lived_sub(self, cycle_start, next_payment):
        """cycle-2+ 的長壽 in_progress 訂閱：``sh_end_date`` 凍結在遠古。

        遞迴計費只推進 ``sh_date_of_next_payment``、永不推進 ``sh_end_date``，
        所以當期視窗只能靠 next_payment 反推——這正是 #327 的正題。把
        sh_end_date 放在遠古，任何「偷看 sh_end_date」的回歸都會讓下面的
        「整期未用→全退」直接掉到 0。
        """
        return self.Sub.create({
            'sh_partner_id': self.partner.id,
            'sh_plan_price': _PLAN_PRICE, 'sh_unit': 'month', 'sh_recurrency': 1,
            'sh_start_date': cycle_start - relativedelta(months=6),
            'sh_end_date': cycle_start - relativedelta(months=5),
            'sh_date_of_next_payment': next_payment,
            'state': 'in_progress',
        })

    def _assert_cycle_invariants(self, expected_days, cycle_start, next_payment):
        # 情境自我守衛：日期真的長出預期的週期長度（選錯日期先炸在這裡）
        self.assertEqual(
            next_payment - relativedelta(months=1), cycle_start,
            '情境日期選錯：cycle_start 必須等於 next_payment 退一個月')
        self.assertEqual(
            (next_payment - cycle_start).days, expected_days,
            '情境日期選錯：這組日期長不出 %d 天的週期' % expected_days)

        sub = self._long_lived_sub(cycle_start, next_payment)
        self.assertLess(
            sub.sh_end_date, cycle_start,
            '前提：sh_end_date 必須早已凍結過期，才能證明視窗跟著 next_payment')

        # (1) 整期都還沒用 → 全退一期 plan 價（同時證明沒去看過期的 sh_end_date）
        with _today_patcher(cycle_start):
            self.assertEqual(sub._prorata_unused_refund(), _PLAN_PRICE)

        # (2) 期末當天 → 退 0（沒有未使用的部分）
        with _today_patcher(next_payment):
            self.assertEqual(sub._prorata_unused_refund(), 0.0)

        # (3) 已逾期 → clamp 下界，仍是 0（不退負、不跨期回溯）
        with _today_patcher(next_payment + timedelta(days=3)):
            self.assertEqual(sub._prorata_unused_refund(), 0.0)

        # (4) 期間內逐日：恆 ∈ [0, plan 價] 且每過一天嚴格遞減
        previous = None
        for offset in range(expected_days + 1):
            day = cycle_start + timedelta(days=offset)
            with self.subTest(cycle_days=expected_days, today=day.isoformat()):
                with _today_patcher(day):
                    value = sub._prorata_unused_refund()
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, _PLAN_PRICE)
                if previous is not None:
                    self.assertLess(value, previous, '每過一天未使用比例必須下降')
                previous = value

        # (5) #324 gate 與日期正交：沒有 done deduct → 期間內任何一天都退 0
        for day in (cycle_start, next_payment - timedelta(days=1)):
            with self.subTest(gate='never_paid', today=day.isoformat()):
                with _today_patcher(day):
                    self.assertEqual(sub._compute_unused_refund(), 0)

        # (6) 有 done deduct → gate 放行，回純公式值
        self.WalletTx.create({
            'partner_id': self.partner.id, 'transaction_type': 'deduct',
            'amount': -_PLAN_PRICE, 'subscription_id': sub.id, 'state': 'done',
        })
        with _today_patcher(cycle_start):
            self.assertEqual(sub._compute_unused_refund(), _PLAN_PRICE)

        # (7) 偶數週期才存在「整數天正中點」；此時語意值必為半價（#437 的原斷言）
        if expected_days % 2 == 0:
            with _today_patcher(cycle_start + timedelta(days=expected_days // 2)):
                self.assertEqual(sub._prorata_unused_refund(), _PLAN_PRICE / 2)

    def test_cycle_28_days_common_february(self):
        """平年 2 月 → 反推週期 28 天（此長度下 15 天剩餘會算出 53.57，非 50）。"""
        self._assert_cycle_invariants(28, date(2026, 2, 10), date(2026, 3, 10))

    def test_cycle_29_days_leap_february(self):
        """閏年 2 月 → 反推週期 29 天（奇數，永遠湊不出剛好半價）。"""
        self._assert_cycle_invariants(29, date(2024, 2, 10), date(2024, 3, 10))

    def test_cycle_30_days(self):
        """4 / 6 / 9 / 11 月 → 反推週期 30 天（唯一讓舊 fixture 綠的長度）。"""
        self._assert_cycle_invariants(30, date(2024, 4, 16), date(2024, 5, 16))

    def test_cycle_31_days(self):
        """31 天月 → 反推週期 31 天（奇數；舊 fixture 在此算出 48.39 → #437）。"""
        self._assert_cycle_invariants(31, date(2025, 7, 10), date(2025, 8, 10))
