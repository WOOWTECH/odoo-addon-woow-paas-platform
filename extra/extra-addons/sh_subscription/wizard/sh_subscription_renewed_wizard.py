# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models
from dateutil.relativedelta import relativedelta


class SubscriptionRenewedWizard(models.TransientModel):
    _name = 'sh.subscription.renewed.wizard'
    _description = 'Subscription Renewed Wizard'

    sh_company_id = fields.Many2one(
        comodel_name="res.company", string="Company", readonly=True, default=lambda self: self.env.company)

    def sh_subscription_close_renewed(self):
        try:
            res_ids = self._context.get('active_ids')
            subscriptions = self.env['sh.subscription.subscription'].browse(res_ids)
            
            for subscription in subscriptions:
                subscription.sh_renew_stage = 'not_time_to_renew'
                date_value = subscription.sh_end_date + relativedelta(days=1) if subscription.state == 'in_progress' else fields.Date.today()
                subscription.state = 'renewed' if subscription.state != 'in_progress' else subscription.state
                subscription.sh_renewed = True

                # Prepare subscription values based on conditions
                sub_vals = {
                    'sh_partner_id': subscription.sh_partner_id.id,
                    'product_id': subscription.product_id.id,
                    'sh_partner_invoice_id': subscription.sh_partner_invoice_id.id,
                    'sh_taxes_ids': subscription.sh_taxes_ids.ids,
                    'sh_qty': subscription.sh_qty,
                    'sh_subscription_plan_id': subscription.sh_subscription_plan_id.id,
                    'sh_plan_price': subscription.product_id.lst_price if subscription.sh_subscription_plan_id.sh_override_product or ('website_id' in self.env['sale.order']._fields and subscription.sh_order_ref_id.website_id) else subscription.sh_plan_price,
                    'sh_recurrency': subscription.sh_recurrency,
                    'sh_unit': subscription.sh_unit,
                    'sh_start_date': date_value,
                    'sh_no_of_billing_cycle': subscription.sh_no_of_billing_cycle,
                    'sh_source': subscription.sh_source,
                    'sh_order_ref_id': False,
                    'sh_subscription_ref': subscription.sh_subscription_ref,
                    'sh_subscription_id': subscription.id,
                    'sh_date_of_next_payment': date_value,
                }

                # Create the new subscription
                new_subscription = self.env["sh.subscription.subscription"].sudo().create(sub_vals)
                new_subscription._onchange_sh_trial_subcription_end_date()
                subscription._sh_send_subscription_email(False)
        
        except Exception as e:
            # Handle any exception that occurs during the process
            # Log the error or raise it, depending on the requirements
            self.env.cr.rollback()  # Rollback any partial transactions if needed
            raise e  # Re-raise the exception to signal the failure
