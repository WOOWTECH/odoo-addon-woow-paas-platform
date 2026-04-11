# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

import logging

from odoo import fields, models

_logger = logging.getLogger(__name__)

class SubscriptionWizard(models.TransientModel):
    _name = 'sh.subscription.cancle.wizard'
    _description = 'Subscription Cancel/Close Wizard'

    sh_subscription_reason_id = fields.Many2one(
        comodel_name="sh.subscription.reason", 
        string="Reason", 
        required=True
    )
    sh_description = fields.Text(string="Description")

    def sh_subscription_cancle_now(self):
        """Cancel or close subscription based on current state."""
        res_ids = self._context.get('active_ids', [])
        if not res_ids:
            return
        
        # Retrieve subscription records
        try:
            subscription = self.env['sh.subscription.subscription'].browse(res_ids)
        except Exception as e:
            # Handle any error that occurs during browsing records
            self.env.cr.rollback()
            raise e
        
        # Iterate over subscriptions and apply cancellation logic
        for sub in subscription:
            try:
                sub.sh_renew_stage = 'time_to_renew'
                if sub.state == 'draft':
                    sub.state = 'cancel'
                elif sub.state == 'in_progress':
                    sub.state = 'close'
                
                # Prepare reason with optional description
                reason = self.sh_subscription_reason_id.name
                if self.sh_description:
                    reason += f' {self.sh_description}'
                
                sub.sh_reason = reason
                sub._sh_send_subscription_email(False)

            except Exception as e:
                # Handle any error that occurs during processing a subscription
                _logger.warning("Failed to cancel subscription %s: %s", sub.id, str(e))
                self.env.cr.rollback()
                continue  # Continue processing other subscriptions
