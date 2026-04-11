# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class WalletTransaction(models.Model):
    _name = 'wallet.transaction'
    _description = '錢包交易記錄'
    _order = 'create_date desc, id desc'

    name = fields.Char(
        string="交易編號",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _('New'),
    )
    partner_id = fields.Many2one(
        'res.partner',
        string="客戶",
        required=True,
        index=True,
        ondelete='cascade',
    )
    transaction_type = fields.Selection([
        ('topup', '儲值'),
        ('deduct', '訂閱扣款'),
        ('refund', '退款'),
        ('card_charge', '信用卡扣款'),
        ('card_bind', '信用卡綁定'),
        ('card_unbind', '解除信用卡綁定'),
        ('adjustment', '調整'),
    ], string="交易類型", required=True, index=True)
    amount = fields.Float(
        string="金額",
        required=True,
        help="正數為增加，負數為扣除",
    )
    balance_after = fields.Float(
        string="交易後餘額",
        help="交易完成後的點數餘額",
    )
    description = fields.Text(
        string="描述",
    )

    # 關聯欄位
    subscription_id = fields.Many2one(
        'sh.subscription.subscription',
        string="關聯訂閱",
        index=True,
    )
    payment_transaction_id = fields.Many2one(
        'payment.transaction',
        string="關聯付款交易",
        index=True,
    )

    # 信用卡扣款相關
    card_charge_amount = fields.Float(
        string="信用卡扣款金額",
        help="當點數不足時，從信用卡扣款的金額",
    )
    card_charge_status = fields.Selection([
        ('pending', '待扣款'),
        ('success', '成功'),
        ('failed', '失敗'),
    ], string="信用卡扣款狀態")
    card_charge_error = fields.Text(
        string="扣款錯誤訊息",
    )

    # ECPay 相關
    ecpay_merchant_trade_no = fields.Char(
        string="ECPay 交易編號",
    )
    ecpay_trade_no = fields.Char(
        string="ECPay 綠界交易編號",
    )

    state = fields.Selection([
        ('draft', '草稿'),
        ('done', '完成'),
        ('failed', '失敗'),
        ('cancelled', '取消'),
    ], string="狀態", default='done', index=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'wallet.transaction'
                ) or _('New')
        return super().create(vals_list)

    @api.depends('name', 'transaction_type', 'amount')
    def _compute_display_name(self):
        """Odoo 18: 使用 _compute_display_name 取代已棄用的 name_get"""
        for record in self:
            type_name = dict(self._fields['transaction_type'].selection).get(
                record.transaction_type, ''
            )
            record.display_name = f"{record.name} - {type_name} ({record.amount:+.2f})"
