# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models
from odoo.exceptions import UserError

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # Adding a computed field for counting related subscriptions
    sh_subscription_count = fields.Integer(
        string="Subscription", 
        compute='_compute_subscription_count', 
        default=0, 
        groups="sh_subscription.group_user_sh_subscription"
    )

    def _compute_subscription_count(self):
        """Compute the number of subscriptions linked to the sale order."""
        for rec in self:
            rec.sh_subscription_count = self.env['sh.subscription.subscription'].search_count(
                [('sh_order_ref_id', '=', rec.id)]
            )

    def sh_action_view_subscription_sale_order(self):
        """
        Action to view subscriptions related to the sale order.
        Adjusts view depending on the number of subscriptions found.
        """
        try:
            self.ensure_one()  # Ensure the method is being called on a singleton record
            subscription = self.env['sh.subscription.subscription'].search(
                [('sh_order_ref_id', '=', self.id)]
            )
            action = self.env["ir.actions.actions"]._for_xml_id(
                "sh_subscription.sh_subscription_subscription_action"
            )

            if subscription:
                if len(subscription) == 1:
                    # If only one subscription, open the form view directly
                    action.update({
                        'views': [(self.env.ref('sh_subscription.sh_subscription_subscription_form_view').id, 'form')],
                        'res_id': subscription.id
                    })
                else:
                    # Otherwise, show a list of subscriptions
                    action['domain'] = [('id', 'in', subscription.ids)]
            else:
                action = {'type': 'ir.actions.act_window_close'}
            
            return action

        except UserError:
            # Handle the singleton error, log and provide feedback if necessary
            self.env.cr.rollback()
            raise
        except Exception as e:
            # General error handling
            self.env.cr.rollback()
            raise UserError(f"An unexpected error occurred: {e}")

    def action_confirm(self):
        """
        Override the confirm action to create subscriptions if the product is a service 
        and has a subscription plan.
        """
        super(SaleOrder, self).action_confirm()
        for order_ln in self.order_line:
            if order_ln.product_id.type == 'service' and order_ln.product_id.sh_product_subscribe:
                try:
                    subscription_vals = {
                        'sh_partner_id': self.partner_id.id,
                        'product_id': order_ln.product_id.id,
                        'sh_taxes_ids': [(6, 0, order_ln.tax_id.ids)],
                        'sh_qty': order_ln.product_uom_qty,
                        'sh_order_ref_id': self.id,
                        'sh_source': 'sales_order',
                        'sh_subscription_plan_id': order_ln.product_id.sh_subscription_plan_id.id,
                    }

                    subscription = self.env["sh.subscription.subscription"].sudo().create(subscription_vals)
                    subscription._onchange_sh_product_id()
                    subscription._onchange_sh_partner_id()
                    subscription._onchange_sh_subscription_plan_id()
                    subscription._onchange_sh_trial_subcription_start_date()
                    subscription._onchange_sh_trial_subcription_end_date()
                    subscription.sh_subscription_confirm()
                
                except Exception as e:
                    # Rollback and handle any error during subscription creation
                    self.env.cr.rollback()
                    raise UserError(f"Failed to create subscription: {e}")
