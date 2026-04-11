import logging

from odoo import api, fields, models
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
            if not sub or not sub.sh_end_date or not sub.sh_start_date:
                wizard.refund_amount = 0
                continue
            total_days = (sub.sh_end_date - sub.sh_start_date).days or 1
            remaining_days = max(0, (sub.sh_end_date - fields.Date.today()).days)
            wizard.refund_amount = round(
                sub.sh_plan_price * remaining_days / total_days, 2
            )

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

        if not entity or not old_sub:
            raise UserError("No active subscription found.")
        if old_sub.state != 'in_progress':
            raise UserError("Can only change plan on active subscriptions.")
        if self.new_plan_id == self.current_plan_id:
            raise UserError("New plan must be different from current plan.")

        partner = old_sub.sh_partner_id

        # 1. Refund unused points to wallet
        if self.refund_amount > 0:
            partner.sudo().add_wallet_points(
                amount=self.refund_amount,
                description="Plan change refund: %s -> %s" % (
                    self.current_plan_id.name, self.new_plan_id.name,
                ),
            )
            _logger.info(
                "Refunded %.2f points to partner %s for plan change",
                self.refund_amount, partner.name,
            )

        # 2. Close old subscription
        old_sub.sudo().write({
            'state': 'close',
            'sh_reason': 'Plan changed to: %s' % self.new_plan_id.name,
        })

        # 3. Create new subscription with new plan
        entity._create_and_confirm_subscription(partner, self.new_plan_id)

        _logger.info(
            "Plan changed for %s id=%s: %s -> %s (refund=%.2f)",
            entity._name, entity.id,
            self.current_plan_id.name, self.new_plan_id.name,
            self.refund_amount,
        )

        return {'type': 'ir.actions.act_window_close'}
