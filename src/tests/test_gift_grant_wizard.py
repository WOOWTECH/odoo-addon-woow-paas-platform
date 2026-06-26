"""Unit tests for gift_grant_wizard（Smart Home 半年贈點 Phase 3）。

admin 手動發點（免碼，給已知客戶）：走 partner.add_wallet_points 自帶
wallet_transaction ledger，非直接 write loyalty.card.points（保稽核一致）。
設計依據：docs/plans/2026-Q2/2026-06-25-smart-home-half-year-gift-points-*.md
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestGiftGrantWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env['res.partner'].sudo().create({'name': 'Grant Tester'})

    def _wizard(self, **vals):
        return self.env['woow_paas_platform.gift_grant_wizard'].create(vals)

    def test_grant_credits_wallet_and_writes_ledger(self):
        wiz = self._wizard(
            partner_id=self.partner.id, points=600.0, reason='半年贈點',
        )
        wiz.action_grant()
        self.partner.invalidate_recordset(['wallet_balance'])
        self.assertEqual(self.partner.wallet_balance, 600.0, '錢包 +600')
        # wallet_transaction ledger（稽核）
        tx = self.env['wallet.transaction'].sudo().search(
            [('partner_id', '=', self.partner.id)], order='id desc', limit=1,
        )
        self.assertEqual(tx.amount, 600.0)
        self.assertIn('半年贈點', tx.description or '')

    def test_grant_zero_or_negative_raises(self):
        with self.assertRaises(UserError):
            self._wizard(partner_id=self.partner.id, points=0.0).action_grant()
        with self.assertRaises(UserError):
            self._wizard(partner_id=self.partner.id, points=-50.0).action_grant()

    def test_partner_defaults_from_context(self):
        """從 res.partner Action 啟動時，active_id 預填 partner_id。"""
        wiz = self.env['woow_paas_platform.gift_grant_wizard'].with_context(
            active_id=self.partner.id, active_model='res.partner',
        ).create({'points': 100.0})
        self.assertEqual(wiz.partner_id, self.partner)

    def test_action_grant_returns_notification(self):
        wiz = self._wizard(partner_id=self.partner.id, points=50.0)
        res = wiz.action_grant()
        self.assertEqual(res.get('tag'), 'display_notification')
        self.assertEqual(res['params'].get('type'), 'success')
