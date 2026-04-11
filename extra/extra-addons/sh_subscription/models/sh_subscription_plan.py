# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models, api
from odoo.exceptions import UserError

class SubscriptionPlan(models.Model):
    _name = 'sh.subscription.plan'
    _description = 'Subscription Plan'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    # Default currency based on the company's currency
    def _get_default_currency_id(self):
        return self.env.company.currency_id.id

    # Basic plan fields
    active = fields.Boolean(string="Active", default=True)
    name = fields.Char(string='Plan Name', required=True)
    company_id = fields.Many2one('res.company', required=True, index=True, default=lambda self: self.env.company)
    
    # Plan details
    sh_duration = fields.Integer(string='Duration', required=True)
    sh_unit = fields.Selection(
        [("day", "Day(s)"), ("week", "Week(s)"), ("month", "Month(s)"), ("year", "Year(s)")],
        required=True, default='day', string="Unit")
    sh_plan_price = fields.Float(string='Price', required=True)
    currency_id = fields.Many2one("res.currency", default=_get_default_currency_id)
    sh_company_id = fields.Many2one('res.company', readonly=True, default=lambda self: self.env.company)
    sh_never_expire = fields.Boolean(string="Never Expire")
    sh_no_of_billing_cycle = fields.Integer(string='No of billing cycle', required=True)
    sh_start_immediately = fields.Boolean(string="Start Immediately")
    sh_billing_day_of_the_month = fields.Integer(string='Billing day of the month', default=1)
    sh_trial = fields.Boolean(string="Plan has trial period")
    sh_trial_duration = fields.Integer(string='Trial duration')
    sh_trial_unit = fields.Selection(
        [("day", "Day(s)"), ("week", "Week(s)"), ("month", "Month(s)"), ("year", "Year(s)")],
        string="Unit")
    sh_free_trial_for_current_month = fields.Boolean(string="Free trial for current month")
    sh_is_close_by_customer = fields.Boolean(string="Is Closable By Customer")
    
    # Product details
    sh_override_product = fields.Boolean(string="Override Product Price")
    sh_description = fields.Text(string="Description")

    # Computed fields for counts
    sh_subscription_count = fields.Integer(string="Subscription", compute='_compute_view_subscription', default=0)
    sh_product_count = fields.Integer(string="Product", compute='_compute_view_product', default=0)
    sh_reminder = fields.Many2many("sh.reminder.template", string="Reminder")
    color = fields.Integer('Color Index', default=0)

    # Reset free trial if the unit is not 'month'
    @api.onchange('sh_unit')
    def _onchange_sh_unit(self):
        if self.sh_unit != 'month':
            self.sh_free_trial_for_current_month = False

    # Ensure billing day of the month is valid if free trial is selected
    @api.constrains('sh_billing_day_of_the_month')
    def _check_sh_billing_day_of_the_month(self):
        if self.sh_free_trial_for_current_month and self.sh_billing_day_of_the_month < 1:
            raise UserError("You cannot select less than 1 as the billing day.")

    # Update product prices based on plan price
    @api.onchange('sh_plan_price')
    def _onchange_sh_plan_price(self):
        if self.sh_plan_price:
            products = self.env['product.product'].search([('sh_subscription_plan_id', '=', self._origin.id)])
            products.write({'lst_price': self.sh_plan_price})

    # Ensure mutually exclusive trial options
    @api.onchange('sh_start_immediately')
    def _onchange_sh_start_immediately(self):
        if self.sh_start_immediately:
            self.sh_trial = self.sh_free_trial_for_current_month = False

    @api.onchange('sh_free_trial_for_current_month')
    def _onchange_sh_free_trial_for_current_month(self):
        if self.sh_free_trial_for_current_month:
            self.sh_trial = self.sh_start_immediately = False

    @api.onchange('sh_trial')
    def _onchange_sh_trial(self):
        if self.sh_trial:
            self.sh_start_immediately = self.sh_free_trial_for_current_month = False

    # Compute number of subscriptions linked to the plan
    def _compute_view_subscription(self):
        for rec in self:
            rec.sh_subscription_count = self.env['sh.subscription.subscription'].search_count(
                [('sh_subscription_plan_id', '=', rec.id)])

    # Action to view subscriptions linked to the plan
    def action_view_subscription(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("sh_subscription.sh_subscription_subscription_action")
        subscription = self.env['sh.subscription.subscription'].search([('sh_subscription_plan_id', '=', self.id)])
        
        if len(subscription) > 1:
            action['domain'] = [('id', 'in', subscription.ids)]
        elif subscription:
            action.update({
                'views': [(self.env.ref('sh_subscription.sh_subscription_subscription_form_view').id, 'form')],
                'res_id': subscription.id
            })
        action['context'] = {'active_test': False, 'create': False}
        return action

    # Compute number of products linked to the plan
    def _compute_view_product(self):
        for rec in self:
            rec.sh_product_count = self.env['product.product'].search_count(
                [('sh_subscription_plan_id', '=', rec.id)])

    # Action to view products linked to the plan
    def action_view_product(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("product.product_variant_action")
        product = self.env['product.product'].search([('sh_subscription_plan_id', '=', self.id)])
        
        if len(product) > 1:
            action['domain'] = [('id', 'in', product.ids)]
        elif product:
            action.update({
                'views': [(self.env.ref('product.product_normal_form_view').id, 'form')],
                'res_id': product.id
            })
        action['context'] = {'active_test': False, 'create': False}
        return action
