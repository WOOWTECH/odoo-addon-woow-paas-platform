# -*- coding: utf-8 -*-
from odoo import fields, models


class SubscriptionPlanWallet(models.Model):
    """擴展訂閱方案，新增強制綁定信用卡選項"""
    _inherit = 'sh.subscription.plan'

    require_credit_card = fields.Boolean(
        string="強制綁定信用卡",
        default=False,
        help="啟用後，客戶訂購此方案時必須先綁定信用卡才能完成訂閱。"
             "這可確保在點數不足時，系統能自動從信用卡扣款。"
    )
