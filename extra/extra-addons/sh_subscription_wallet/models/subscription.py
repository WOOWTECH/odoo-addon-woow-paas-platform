# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
import logging

_logger = logging.getLogger(__name__)


class SubscriptionWallet(models.Model):
    """擴展訂閱，新增信用卡綁定驗證邏輯、顯示欄位、及錢包扣款排程"""
    _name = 'sh.subscription.subscription'
    _inherit = ['sh.subscription.subscription', 'subscription.wallet.mixin']

    # === 客戶信用卡相關欄位（related 顯示用）===
    partner_has_credit_card = fields.Boolean(
        string="已綁定信用卡",
        compute='_compute_partner_credit_card_info',
        store=False,
    )
    partner_card_last4 = fields.Char(
        string="信用卡末四碼",
        compute='_compute_partner_credit_card_info',
        store=False,
    )
    partner_card_expiry = fields.Char(
        string="信用卡有效期",
        compute='_compute_partner_credit_card_info',
        store=False,
    )
    partner_card_bound_date = fields.Datetime(
        string="綁定日期",
        compute='_compute_partner_credit_card_info',
        store=False,
    )

    # === 方案信用卡要求欄位 ===
    plan_require_credit_card = fields.Boolean(
        string="方案要求綁卡",
        related='sh_subscription_plan_id.require_credit_card',
        readonly=True,
        store=False,
    )

    @api.depends('sh_partner_id', 'sh_partner_id.ecpay_card_token',
                 'sh_partner_id.ecpay_card_last4', 'sh_partner_id.ecpay_card_expiry',
                 'sh_partner_id.ecpay_card_bound_date')
    def _compute_partner_credit_card_info(self):
        """計算客戶信用卡綁定資訊"""
        for subscription in self:
            partner = subscription.sh_partner_id
            if partner:
                subscription.partner_has_credit_card = partner.has_bound_credit_card()
                subscription.partner_card_last4 = partner.ecpay_card_last4 or ''
                subscription.partner_card_expiry = partner.ecpay_card_expiry or ''
                subscription.partner_card_bound_date = partner.ecpay_card_bound_date
            else:
                subscription.partner_has_credit_card = False
                subscription.partner_card_last4 = ''
                subscription.partner_card_expiry = ''
                subscription.partner_card_bound_date = False

    @api.constrains('sh_partner_id', 'sh_subscription_plan_id')
    def _check_credit_card_required(self):
        """驗證：若方案要求綁定信用卡，客戶必須已綁定"""
        for subscription in self:
            plan = subscription.sh_subscription_plan_id
            partner = subscription.sh_partner_id

            if plan and plan.require_credit_card:
                if partner and not partner.has_bound_credit_card():
                    raise ValidationError(_(
                        "訂閱方案「%s」要求綁定信用卡。\n"
                        "客戶「%s」尚未綁定信用卡，請先完成信用卡綁定後再建立訂閱。"
                    ) % (plan.name, partner.name))

    def action_check_credit_card(self):
        """檢查客戶是否已綁定信用卡"""
        self.ensure_one()
        partner = self.sh_partner_id
        if not partner:
            raise UserError(_("訂閱沒有關聯客戶"))

        if partner.has_bound_credit_card():
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('信用卡已綁定'),
                    'message': _('客戶 %s 已綁定信用卡（末四碼：%s）') % (
                        partner.name, partner.ecpay_card_last4 or '****'
                    ),
                    'type': 'success',
                    'sticky': False,
                }
            }
        else:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('信用卡未綁定'),
                    'message': _('客戶 %s 尚未綁定信用卡') % partner.name,
                    'type': 'warning',
                    'sticky': False,
                }
            }

    def _process_subscription_wallet_payment(self, amount=None):
        """
        處理訂閱錢包扣款 — 委派給 subscription.wallet.mixin

        Args:
            amount: 扣款金額，若未指定則使用方案價格

        Returns:
            dict: 扣款結果 {success, points_deducted, card_charged, error, transaction}
        """
        self.ensure_one()

        if amount is None:
            amount = self.sh_plan_price or 0

        if amount <= 0:
            return {
                'success': True,
                'points_deducted': 0,
                'card_charged': 0,
                'error': None,
                'transaction': False,
            }

        return self._process_wallet_payment(self, amount)

    # === Cron 排程方法 ===

    @api.model
    def _cron_wallet_charge(self):
        """
        每日排程：處理到期訂閱的錢包扣款

        尋找所有 state='in_progress' 且 sh_date_of_next_payment == today 的訂閱，
        依序執行錢包扣款（點數優先，不足刷卡）。
        """
        today = fields.Date.today()
        subscriptions = self.sudo().search([
            ('state', '=', 'in_progress'),
            ('sh_date_of_next_payment', '=', today),
        ])

        _logger.info(
            f"Wallet charge cron: found {len(subscriptions)} "
            f"subscriptions due for payment on {today}"
        )

        for subscription in subscriptions:
            try:
                result = subscription._process_subscription_wallet_payment()
                if result.get('success'):
                    _logger.info(
                        f"Wallet charge success for {subscription.name}: "
                        f"points={result.get('points_deducted', 0)}, "
                        f"card={result.get('card_charged', 0)}"
                    )
                else:
                    _logger.warning(
                        f"Wallet charge failed for {subscription.name}: "
                        f"{result.get('error')}"
                    )
            except Exception as e:
                _logger.exception(
                    f"Wallet charge error for subscription {subscription.name}"
                )

    @api.model
    def _cron_wallet_charge_retry(self):
        """
        每小時排程：重試失敗的錢包扣款

        尋找最近 24 小時內扣款失敗的交易記錄，重新嘗試扣款。
        適用情境：客戶剛儲值或剛綁定信用卡。
        """
        cutoff = fields.Datetime.now() - timedelta(hours=24)
        failed_transactions = self.env['wallet.transaction'].sudo().search([
            ('transaction_type', '=', 'deduct'),
            ('state', '=', 'failed'),
            ('create_date', '>=', cutoff),
            ('subscription_id', '!=', False),
        ])

        _logger.info(
            f"Wallet charge retry cron: found {len(failed_transactions)} "
            f"failed transactions to retry"
        )

        retried_subscription_ids = set()
        for tx in failed_transactions:
            subscription = tx.subscription_id
            if not subscription or subscription.id in retried_subscription_ids:
                continue
            if subscription.state != 'in_progress':
                continue

            retried_subscription_ids.add(subscription.id)
            try:
                result = subscription._process_subscription_wallet_payment()
                if result.get('success'):
                    _logger.info(
                        f"Wallet charge retry success for {subscription.name}"
                    )
                else:
                    _logger.info(
                        f"Wallet charge retry still failing for "
                        f"{subscription.name}: {result.get('error')}"
                    )
            except Exception as e:
                _logger.exception(
                    f"Wallet charge retry error for {subscription.name}"
                )
