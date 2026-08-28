import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ChangeSubscriptionPlanWizard(models.TransientModel):
    _name = 'woow_paas_platform.change_plan_wizard'
    _description = 'Change Subscription Plan'

    cloud_service_id = fields.Many2one(
        'woow_paas_platform.cloud_service',
        string='Cloud Service',
    )
    smart_home_id = fields.Many2one(
        'woow_paas_platform.smart_home',
        string='Smart Home',
    )

    current_plan_id = fields.Many2one(
        'sh.subscription.plan',
        string='Current Plan',
        readonly=True,
    )
    current_price = fields.Float(
        string='Current Price',
        readonly=True,
    )

    new_plan_id = fields.Many2one(
        'sh.subscription.plan',
        string='New Plan',
        required=True,
    )
    new_price = fields.Float(
        related='new_plan_id.sh_plan_price',
        string='New Price',
    )

    refund_amount = fields.Float(
        string='Refund Points',
        compute='_compute_refund',
    )

    @api.depends('current_plan_id', 'cloud_service_id', 'smart_home_id')
    def _compute_refund(self):
        for wizard in self:
            sub = wizard._get_subscription()
            wizard.refund_amount = sub._compute_unused_refund() if sub else 0

    def _get_entity(self):
        return self.cloud_service_id or self.smart_home_id

    def _get_subscription(self):
        entity = self._get_entity()
        return entity.subscription_id if entity else None

    def action_change_plan(self):
        """Execute plan change: close old subscription, refund, create new."""
        self.ensure_one()
        entity = self._get_entity()
        old_sub = self._get_subscription()
        _logger.info(
            "[PLAN_CHG] action_change_plan entry entity=%s:%s old_sub=%s new_plan=%s refund=%s",
            entity._name if entity else None, entity.id if entity else None,
            old_sub.id if old_sub else None,
            self.new_plan_id.id, self.refund_amount,
        )

        if not entity or not old_sub:
            raise UserError(_("No active subscription found."))
        if old_sub.state != 'in_progress':
            raise UserError(_("Can only change plan on active subscriptions."))
        if self.new_plan_id == self.current_plan_id:
            raise UserError(_("New plan must be different from current plan."))

        # #773 降級前置合規 gate（第 4 條換方案路徑）。
        #
        # 為什麼 wizard 也要補：它走的是與三個 HTTP 端點**同一套**「退點 → 關舊訂閱 →
        # 建新訂閱」，只是入口在後台。只補三個端點的話，gate 的目的（付費界線生效）
        # 會被這條路徑直接繞過。
        #
        # ⚠ 這條路徑的既有防護與三個端點**不同**，敘述時不要混為一談：
        #   * plan 白名單閘：**完全沒有**（三個端點都有 template↔plan 歸屬檢查）。
        #   * role 閘：碼層沒有，但**間接存在**——`_create_and_confirm_subscription` 最後
        #     那次 `self.write({'subscription_id': ...})` 是**非 sudo** 的
        #     （`subscription_lifecycle_mixin.py:234`），會撞到 entity 的 ir.rule
        #     owner/admin 寫入閘（`ir_rules.xml:103` CS／`:175` SH）。
        #   ⇒ 同 workspace 的非 owner/admin 不是「換得成」，而是**先退了點、關了舊訂閱，
        #     才在最後一步炸 AccessError**；因為全鏈無 `cr.commit()`，交易會整包回捲。
        #     那是「先產生副作用再撤」的形狀，不是乾淨的拒絕。
        # ⇒ 把 gate 插在這裡（三道 UserError 之後、退點之前）順帶把那個形狀也修掉：
        #   不合規的換方案在**任何副作用發生前**就停住。
        #
        # 回錯形狀刻意與三個端點不同：wizard 是 TransientModel action，慣例是
        # `raise UserError`（同檔上方三道閘皆然），⛔ 不能照抄 controller 的
        # `{'success': False, ...}` dict——那在 wizard 裡會被當成 action 回傳值。
        denial = self.env['woow_paas_platform.security_access_access_policy']\
            ._seat_gate_denial(entity, self.new_plan_id)
        if denial:
            raise UserError(denial['error'])

        partner = old_sub.sh_partner_id

        # 1. Refund unused points to wallet (#318: refund-typed + subscription
        # link for reconciliation)
        if self.refund_amount > 0:
            partner.sudo().refund_wallet_points(
                amount=self.refund_amount,
                description="Plan change refund: %s -> %s" % (
                    self.current_plan_id.name, self.new_plan_id.name,
                ),
                subscription=old_sub,
            )
            partner.invalidate_recordset(['wallet_balance'])
            _logger.info(
                "[PLAN_CHG] refund partner=%s amount=%.2f old=%s new=%s",
                partner.id, self.refund_amount,
                self.current_plan_id.name, self.new_plan_id.name,
            )

        # 2. Close old subscription
        old_sub.sudo().write({
            'state': 'close',
            'sh_reason': 'Plan changed to: %s' % self.new_plan_id.name,
        })
        _logger.info(
            "[PLAN_CHG] closed old subscription=%s", old_sub.id,
        )

        # 3. Create new subscription with new plan
        entity._create_and_confirm_subscription(partner, self.new_plan_id)

        _logger.info(
            "[PLAN_CHG] done entity=%s:%s %s→%s refund=%.2f new_sub=%s",
            entity._name, entity.id,
            self.current_plan_id.name, self.new_plan_id.name,
            self.refund_amount,
            entity.subscription_id.id if entity.subscription_id else None,
        )

        return {'type': 'ir.actions.act_window_close'}
