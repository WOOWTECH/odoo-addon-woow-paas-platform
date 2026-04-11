# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # ECPay 信用卡 Token 欄位
    ecpay_card_token = fields.Char(
        string="ECPay 信用卡 Token",
        help="ECPay 定期定額授權的信用卡 Token",
        copy=False,
    )
    ecpay_card_last4 = fields.Char(
        string="信用卡末四碼",
        help="綁定的信用卡末四碼，僅供顯示用",
        copy=False,
    )
    ecpay_card_expiry = fields.Char(
        string="信用卡有效期",
        help="信用卡有效期限 MM/YY",
        copy=False,
    )
    ecpay_card_bound_date = fields.Datetime(
        string="信用卡綁定日期",
        copy=False,
    )

    # 錢包點數相關
    wallet_balance = fields.Float(
        string="錢包點數餘額",
        compute="_compute_wallet_balance",
        store=False,
        help="客戶的點數錢包餘額（來自 Loyalty 系統）",
    )
    wallet_transaction_ids = fields.One2many(
        'wallet.transaction',
        'partner_id',
        string="錢包交易記錄",
    )
    wallet_transaction_count = fields.Integer(
        string="交易記錄數",
        compute="_compute_wallet_transaction_count",
    )

    def _compute_wallet_balance(self):
        """計算錢包點數餘額（從 Loyalty 系統）"""
        wallet_program = self.env.ref(
            'sh_subscription_wallet.loyalty_program_wallet',
            raise_if_not_found=False
        )
        for partner in self:
            balance = 0.0
            if wallet_program:
                loyalty_cards = self.env['loyalty.card'].sudo().search([
                    ('partner_id', '=', partner.id),
                    ('program_id', '=', wallet_program.id),
                ])
                balance = sum(loyalty_cards.mapped('points'))
            partner.wallet_balance = balance

    def _compute_wallet_transaction_count(self):
        for partner in self:
            partner.wallet_transaction_count = len(partner.wallet_transaction_ids)

    def action_view_wallet_transactions(self):
        """查看錢包交易記錄"""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('錢包交易記錄'),
            'res_model': 'wallet.transaction',
            'view_mode': 'list,form',
            'domain': [('partner_id', '=', self.id)],
            'context': {'default_partner_id': self.id},
        }

    def action_bind_credit_card(self):
        """跳轉到信用卡綁定頁面"""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': '/my/wallet/bind_card',
            'target': 'self',
        }

    def action_unbind_credit_card(self):
        """解除信用卡綁定"""
        self.ensure_one()
        if not self.ecpay_card_token:
            raise UserError(_('尚未綁定信用卡'))

        self.write({
            'ecpay_card_token': False,
            'ecpay_card_last4': False,
            'ecpay_card_expiry': False,
            'ecpay_card_bound_date': False,
        })

        # 記錄解綁交易
        self.env['wallet.transaction'].create({
            'partner_id': self.id,
            'transaction_type': 'card_unbind',
            'amount': 0,
            'description': _('解除信用卡綁定'),
        })

        return True

    def has_bound_credit_card(self):
        """檢查是否已綁定信用卡"""
        self.ensure_one()
        return bool(self.ecpay_card_token)

    def get_wallet_loyalty_card(self):
        """取得錢包對應的 Loyalty Card"""
        self.ensure_one()
        wallet_program = self.env.ref(
            'sh_subscription_wallet.loyalty_program_wallet',
            raise_if_not_found=False
        )
        if not wallet_program:
            return False

        loyalty_card = self.env['loyalty.card'].sudo().search([
            ('partner_id', '=', self.id),
            ('program_id', '=', wallet_program.id),
        ], limit=1)
        if loyalty_card:
            return loyalty_card

        # 如果沒有，建立一張新的
        return self.env['loyalty.card'].create({
            'program_id': wallet_program.id,
            'partner_id': self.id,
            'points': 0,
        })

    def add_wallet_points(self, amount, description=None, payment_transaction=None):
        """增加錢包點數"""
        self.ensure_one()
        if amount <= 0:
            raise UserError(_('儲值金額必須大於 0'))

        loyalty_card = self.get_wallet_loyalty_card()
        if not loyalty_card:
            raise UserError(_('無法取得錢包點數卡'))

        # 增加點數
        loyalty_card.points += amount

        # 記錄交易
        self.env['wallet.transaction'].create({
            'partner_id': self.id,
            'transaction_type': 'topup',
            'amount': amount,
            'description': description or _('點數儲值'),
            'payment_transaction_id': payment_transaction.id if payment_transaction else False,
            'balance_after': loyalty_card.points,
        })

        _logger.info(f"Partner {self.id} wallet topup: +{amount} points, balance: {loyalty_card.points}")
        return True

    def deduct_wallet_points(self, amount, description=None, subscription=None):
        """扣除錢包點數（含 row-level lock 防止並發扣款）"""
        self.ensure_one()
        if amount <= 0:
            raise UserError(_('扣款金額必須大於 0'))

        loyalty_card = self.get_wallet_loyalty_card()
        if not loyalty_card:
            raise UserError(_('無法取得錢包點數卡'))

        # Flush ORM changes to DB before raw SQL to ensure consistency
        loyalty_card.flush_recordset(['points'])

        # Row-level lock 防止並發扣款競爭
        self.env.cr.execute(
            "SELECT points FROM loyalty_card WHERE id = %s FOR UPDATE",
            [loyalty_card.id]
        )
        row = self.env.cr.fetchone()
        current_points = row[0] if row else 0.0

        actual_deduct = min(amount, current_points)
        if actual_deduct <= 0:
            _logger.warning(
                f"Partner {self.id} wallet deduct: insufficient balance "
                f"(requested={amount}, available={current_points})"
            )
            return 0.0

        loyalty_card.points = current_points - actual_deduct
        # Flush immediately so DB reflects the deduction
        # (required when called inside a savepoint context)
        loyalty_card.flush_recordset(['points'])

        # 記錄交易
        self.env['wallet.transaction'].create({
            'partner_id': self.id,
            'transaction_type': 'deduct',
            'amount': -actual_deduct,
            'description': description or _('訂閱扣款'),
            'subscription_id': subscription.id if subscription else False,
            'balance_after': loyalty_card.points,
        })

        _logger.info(f"Partner {self.id} wallet deduct: -{actual_deduct} points, balance: {loyalty_card.points}")
        return actual_deduct
