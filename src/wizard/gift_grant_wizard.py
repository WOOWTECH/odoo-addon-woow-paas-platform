"""admin 手動贈送錢包點數（免碼，給已知客戶）。

Smart Home 半年贈點 Phase 3。走 partner.add_wallet_points 灌點，自帶
wallet_transaction ledger（稽核一致），不直接 write loyalty.card.points。
與 Phase 2 的自助兌換碼 endpoint（/api/billing/redeem-code）互補：兩條路徑
最終都進同一個 eWallet 錢包。
"""
from odoo import _, fields, models
from odoo.exceptions import UserError


class GiftGrantWizard(models.TransientModel):
    _name = 'woow_paas_platform.gift_grant_wizard'
    _description = '手動贈送錢包點數'

    partner_id = fields.Many2one(
        comodel_name='res.partner',
        string='客戶',
        required=True,
        default=lambda self: self.env.context.get('active_id'),
        help='從客戶表單的「動作」選單啟動時會自動帶入',
    )
    points = fields.Float(
        string='贈送點數',
        required=True,
        help='點數 ↔ 金額 1:1；半年份 = 目標 Smart Home plan 月費 × 6',
    )
    reason = fields.Char(
        string='事由',
        default='Smart Home 半年贈點',
        help='寫入 wallet_transaction ledger 供稽核',
    )

    def action_grant(self):
        """灌點進 partner 的 eWallet 錢包並寫 ledger，回 notification。"""
        self.ensure_one()
        if self.points <= 0:
            raise UserError(_("Gift points must be greater than 0."))
        partner = self.partner_id.sudo()
        # 走官方 API：自帶 wallet_transaction ledger + 寫 loyalty.card.points。
        partner.add_wallet_points(self.points, self.reason or _("Manual grant"))
        partner.invalidate_recordset(['wallet_balance'])
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'type': 'success',
                'title': _("贈點成功"),
                'message': _(
                    "已贈送 %(pts)s 點給 %(name)s（目前餘額 %(bal)s）"
                ) % {
                    'pts': self.points,
                    'name': partner.display_name,
                    'bal': partner.wallet_balance,
                },
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }
