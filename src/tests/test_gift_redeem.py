"""Unit tests for loyalty.card gift-redeem → eWallet 灌點 核心邏輯。

Smart Home「買主機半年免費」epic Phase 1（TDD red）。
設計依據：docs/plans/2026-Q2/2026-06-25-smart-home-half-year-gift-points-{design,implementation}.md

核心：extend 原生 loyalty.card 加 _redeem_to_wallet(partner) / _find_redeemable(code)。
- 兌換 = 對該 card row FOR UPDATE NOWAIT 鎖 → validate（gift_card program、
  points>0、active、未過期）→ partner.add_wallet_points(points) 灌進 ewallet 錢包
  → 作廢卡（points=0、partner_id=兌換者）。
- 「已用」= points 歸 0（loyalty.card 無 state 欄位，前例 test_dashboard_summary.py:74）。
"""
from datetime import timedelta
from unittest.mock import patch

import psycopg2

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestGiftRedeem(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 兌換者：fresh partner，錢包初始空（redeem 自身的 add_wallet_points
        # 會 lazy 建出 ewallet loyalty.card，故不預先 seed，讓 happy path 連帶
        # 驗證首次灌點自動建卡）。
        cls.partner = cls.env['res.partner'].sudo().create({
            'name': 'Gift Redeem Tester',
        })
        # gift_card program：search-first（sandbox 種子環境有 id 1）else create，
        # 對齊 repo 既有 journal/product fixture 慣例（test_account_move_pay_from_wallet.py）。
        cls.gift_program = cls.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'gift_card')], limit=1,
        )
        if not cls.gift_program:
            cls.gift_program = cls.env['loyalty.program'].sudo().create({
                'name': 'Test Gift Cards',
                'program_type': 'gift_card',
            })

    def _make_gift_card(self, code, points=100.0, **vals):
        """建一張 gift_card 的 loyalty.card。"""
        return self.env['loyalty.card'].sudo().create({
            'program_id': self.gift_program.id,
            'code': code,
            'points': points,
            **vals,
        })

    # --- happy path -------------------------------------------------------

    def test_redeem_credits_wallet_and_voids_card(self):
        card = self._make_gift_card('GIFT-HAPPY-001', points=600.0)
        granted = card._redeem_to_wallet(self.partner)

        self.assertEqual(granted, 600.0, "回傳實際入帳點數")
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self.partner.wallet_balance, 600.0, "錢包餘額 +600")
        wallet_card = self.partner.sudo().get_wallet_loyalty_card()
        self.assertEqual(wallet_card.points, 600.0, "後端 ewallet 卡 points=600")
        # 原 gift 卡作廢。先 invalidate 強制從 DB 重讀，驗證作廢「真的落地」
        # 而非僅 cache（兌換是計費相關，斷言 DB truth 比斷言 cache 強）。
        card.invalidate_recordset(['points', 'partner_id'])
        self.assertEqual(card.points, 0.0, "gift 卡 points 歸 0（已用）")
        self.assertEqual(card.partner_id, self.partner, "記錄兌換者供稽核")

    # --- _find_redeemable -------------------------------------------------

    def test_find_redeemable_by_code(self):
        card = self._make_gift_card('GIFT-FIND-001')
        found = self.env['loyalty.card']._find_redeemable('GIFT-FIND-001')
        self.assertEqual(found, card)

    def test_find_redeemable_unknown_code_returns_empty(self):
        found = self.env['loyalty.card']._find_redeemable('NO-SUCH-CODE')
        self.assertFalse(found)

    def test_find_redeemable_ignores_non_gift_program(self):
        # 錢包本身的 ewallet 卡（program_type='ewallet'）不可被當禮品碼撈到，
        # 否則使用者能把自己的錢包「重兌」一次。
        ewallet_prog = self.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'ewallet')], limit=1,
        )
        self.assertTrue(
            ewallet_prog,
            "sh_subscription_wallet 應 seed 一個 ewallet program",
        )
        self.env['loyalty.card'].sudo().create({
            'program_id': ewallet_prog.id,
            'code': 'EW-NOT-A-GIFT-001',
            'points': 50.0,
        })
        found = self.env['loyalty.card']._find_redeemable('EW-NOT-A-GIFT-001')
        self.assertFalse(found, "ewallet 卡不該被禮品兌換查到")

    # --- validate 分流 ----------------------------------------------------

    def test_redeem_already_used_raises(self):
        card = self._make_gift_card('GIFT-USED-001', points=0.0)
        with self.assertRaises(ValidationError):
            card._redeem_to_wallet(self.partner)

    def test_redeem_expired_raises(self):
        yesterday = fields.Date.today() - timedelta(days=1)
        card = self._make_gift_card(
            'GIFT-EXPIRED-001', points=100.0, expiration_date=yesterday,
        )
        with self.assertRaises(ValidationError):
            card._redeem_to_wallet(self.partner)

    def test_redeem_today_expiration_allowed(self):
        """邊界：到期日 == 今天 仍可兌（程式用 < today 判過期，當天放行）。"""
        today = fields.Date.today()
        card = self._make_gift_card(
            'GIFT-TODAY-001', points=100.0, expiration_date=today,
        )
        granted = card._redeem_to_wallet(self.partner)
        self.assertEqual(granted, 100.0)
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self.partner.wallet_balance, 100.0)

    def test_redeem_inactive_raises(self):
        card = self._make_gift_card('GIFT-INACTIVE-001', points=100.0)
        card.active = False
        with self.assertRaises(ValidationError):
            card._redeem_to_wallet(self.partner)

    def test_double_redeem_blocked_by_points_zero(self):
        """同卡兌兩次：第一次成功歸 0，第二次被 points=0 擋。"""
        card = self._make_gift_card('GIFT-DOUBLE-001', points=300.0)
        card._redeem_to_wallet(self.partner)
        with self.assertRaises(ValidationError):
            card._redeem_to_wallet(self.partner)

    # --- 並行守衛（mock FOR UPDATE NOWAIT 鎖衝突）------------------------

    def test_concurrent_redeem_rejected(self):
        """模擬另一交易已持鎖：FOR UPDATE NOWAIT 拋 LockNotAvailable → UserError。

        註：此為 mock 驗例外處理路徑。真實雙 HTTP request 並行兌同卡（驗只一個成功、
        另一個被鎖拒或讀到 points=0）需 Docker 隔離 transaction，待 Phase 5 sandbox e2e 補。
        """
        card = self._make_gift_card('GIFT-CONCURRENT-001', points=100.0)
        orig = self.env.cr.execute

        def fake(query, params=None, *args, **kwargs):
            if 'FOR UPDATE NOWAIT' in query:
                raise psycopg2.errors.LockNotAvailable()
            return orig(query, params, *args, **kwargs)

        with patch.object(type(self.env.cr), 'execute', side_effect=fake):
            with self.assertRaises(UserError):
                card._redeem_to_wallet(self.partner)
        # 卡未被動到
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(card.points, 100.0)
        self.assertEqual(self.partner.wallet_balance, 0.0)
