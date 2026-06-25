"""HttpCase：/api/billing/redeem-code endpoint（Smart Home 半年贈點 Phase 2）。

驗 endpoint 層：
- A 成功兌換 + 餘額確實增 + 卡作廢（DB truth）
- 安全契約：兌換一律灌進登入者錢包，無法指定他人 partner（loyalty_card.py SECURITY docstring）
- 同碼不可重兌（已用碼回 already_used）
- 亂碼回 code_not_found、過期碼回 expired
- 兌換是純發點，不觸發任何 ECPay 刷卡（無 payment.transaction）

並發鎖（locked）不在 HttpCase 驗（單 transaction）；model 層已由 test_gift_redeem mock 驗，
真並發待 Phase 5 sandbox e2e。
設計依據：docs/plans/2026-Q2/2026-06-25-smart-home-half-year-gift-points-*.md
"""
import json as json_lib
import uuid
from datetime import timedelta

from odoo import fields
from odoo.tests.common import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestRedeemCodeEndpoint(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        suffix = uuid.uuid4().hex[:8]
        cls.pwd = 'redeem_pwd'
        group_user = cls.env.ref('base.group_user')

        cls.user_a = cls.env['res.users'].create({
            'name': 'Redeem A %s' % suffix,
            'login': 'redeem_a_%s' % suffix,
            'password': cls.pwd,
            'groups_id': [(6, 0, [group_user.id])],
        })
        cls.user_b = cls.env['res.users'].create({
            'name': 'Redeem B %s' % suffix,
            'login': 'redeem_b_%s' % suffix,
            'password': cls.pwd,
            'groups_id': [(6, 0, [group_user.id])],
        })

        # gift_card program：search-first else create（對齊 test_gift_redeem.py）
        cls.gift_program = cls.env['loyalty.program'].sudo().search(
            [('program_type', '=', 'gift_card')], limit=1,
        )
        if not cls.gift_program:
            cls.gift_program = cls.env['loyalty.program'].sudo().create({
                'name': 'EP Gift Cards',
                'program_type': 'gift_card',
            })

    def _make_card(self, code, points=500.0, **vals):
        return self.env['loyalty.card'].sudo().create({
            'program_id': self.gift_program.id,
            'code': code,
            'points': points,
            **vals,
        })

    def _redeem(self, code):
        resp = self.url_open(
            '/api/billing/redeem-code',
            data=json_lib.dumps({'params': {'code': code}}),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        return resp.json()['result']

    # --- 成功 + 餘額增 + 卡作廢 -----------------------------------------
    def test_redeem_success_credits_wallet(self):
        card = self._make_card('EP-GIFT-OK', points=500.0)
        self.authenticate(self.user_a.login, self.pwd)
        res = self._redeem('EP-GIFT-OK')
        self.assertTrue(res['success'], res)
        self.assertEqual(res['data']['points_granted'], 500.0)
        self.assertEqual(res['data']['new_balance'], 500.0, '餘額確實 +500')
        card.invalidate_recordset(['points', 'partner_id'])
        self.assertEqual(card.points, 0.0, '卡作廢（DB truth）')
        self.assertEqual(card.partner_id, self.user_a.partner_id)

    # --- 安全契約：兌換只灌登入者，不污染他人 --------------------------
    def test_redeem_credits_only_authenticated_user(self):
        self._make_card('EP-GIFT-XUSER', points=300.0)
        self.authenticate(self.user_b.login, self.pwd)
        res = self._redeem('EP-GIFT-XUSER')
        self.assertTrue(res['success'], res)
        self.user_a.partner_id.invalidate_recordset(['wallet_balance'])
        self.user_b.partner_id.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self.user_b.partner_id.wallet_balance, 300.0)
        self.assertEqual(
            self.user_a.partner_id.wallet_balance, 0.0,
            '兌換絕不灌進非登入者錢包',
        )

    # --- 同碼不可重兌（A 兌過，B 再兌被 already_used 擋）----------------
    def test_redeem_used_code_rejected_for_second_user(self):
        self._make_card('EP-GIFT-ONCE', points=100.0)
        self.authenticate(self.user_a.login, self.pwd)
        self.assertTrue(self._redeem('EP-GIFT-ONCE')['success'])
        self.authenticate(self.user_b.login, self.pwd)
        res = self._redeem('EP-GIFT-ONCE')
        self.assertFalse(res['success'])
        self.assertEqual(res['code'], 'already_used')

    # --- 亂碼 ------------------------------------------------------------
    def test_redeem_unknown_code(self):
        self.authenticate(self.user_a.login, self.pwd)
        res = self._redeem('NO-SUCH-CODE-XYZ')
        self.assertFalse(res['success'])
        self.assertEqual(res['code'], 'code_not_found')

    # --- 過期 ------------------------------------------------------------
    def test_redeem_expired_code(self):
        yesterday = fields.Date.today() - timedelta(days=1)
        self._make_card('EP-GIFT-EXP', points=100.0, expiration_date=yesterday)
        self.authenticate(self.user_a.login, self.pwd)
        res = self._redeem('EP-GIFT-EXP')
        self.assertFalse(res['success'])
        self.assertEqual(res['code'], 'expired')

    # --- 兌換不觸發 ECPay 刷卡 ------------------------------------------
    def test_redeem_does_not_trigger_ecpay(self):
        self._make_card('EP-GIFT-NOECPAY', points=200.0)
        Tx = self.env['payment.transaction'].sudo()
        before = Tx.search_count([('partner_id', '=', self.user_a.partner_id.id)])
        self.authenticate(self.user_a.login, self.pwd)
        self.assertTrue(self._redeem('EP-GIFT-NOECPAY')['success'])
        after = Tx.search_count([('partner_id', '=', self.user_a.partner_id.id)])
        self.assertEqual(after, before, '兌換不得建立任何 payment.transaction')
