# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models, api, _
from datetime import date
from dateutil.relativedelta import relativedelta
from calendar import monthrange
from odoo.exceptions import UserError
import base64

INTERVAL_FACTOR = {
    'day': 30.0,
    'week': 30.0 / 7.0,
    'month': 1.0,
    'year': 1.0 / 12.0,
}

PERIODS = {'day': 'Day(s)', 'week': 'Week(s)',
           'month': 'Month(s)', 'year': 'Year(s)'}


class SubscriptionSubscription(models.Model):
    _name = 'sh.subscription.subscription'
    _description = 'Subscription subscription'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    def _get_default_currency_id(self):
        return self.env.company.currency_id.id

    state = fields.Selection([
        ('draft', 'Draft'),
        ('in_progress', 'In Progress'),
        ('cancel', 'Cancelled'),
        ('close', 'Finished'),
        ('expire', 'Expired'),
        ('renewed', 'Renewed')
    ], string='State', readonly=True, copy=False, tracking=True, required=True, default='draft')
    name = fields.Char(string='Name', readonly=True, default=lambda self: _('New'))
    active = fields.Boolean(string="Active", default=True)
    sh_partner_id = fields.Many2one("res.partner", string="Customer Name")
    product_id = fields.Many2one("product.product", string="Product", domain="[('sh_product_subscribe','=', True)]")
    sh_partner_invoice_id = fields.Many2one("res.partner", string="Customer Invoice Address")
    sh_taxes_ids = fields.Many2many("account.tax", string="Taxes")
    sh_qty = fields.Float(string='Quantity', default=1)
    sh_subscription_plan_id = fields.Many2one("sh.subscription.plan", string="Subscription Plan")
    sh_plan_price = fields.Float(string='Price')
    currency_id = fields.Many2one("res.currency", string="Currency", default=_get_default_currency_id)
    sh_company_id = fields.Many2one("res.company", string="Company", readonly=True, default=lambda self: self.env.company)
    sh_recurrency = fields.Integer(string='Recurrency')
    sh_unit = fields.Selection([("day", "Day(s)"), ("week", "Week(s)"), ("month", "Month(s)"), ("year", "Year(s)")], string="Units")
    sh_start_date = fields.Date(string="Start Date", copy=False)
    sh_end_date = fields.Date(string="End Date", copy=False)
    sh_trial_end_date = fields.Date(string="Trial End Date", copy=False)
    sh_trial = fields.Boolean(string="Plan has trial period", copy=False)
    sh_trial_duration = fields.Integer(string='Trial duration', copy=False)
    sh_trial_unit = fields.Selection([("day", "Day(s)"), ("week", "Week(s)"), ("month", "Month(s)"), ("year", "Year(s)")], string="Unit", copy=False)
    sh_no_of_billing_cycle = fields.Integer(string='No of billing cycle')
    sh_source = fields.Selection([("manual", "Manual"), ("sales_order", "Sales Order")], string="Source", default='manual')
    sh_subscription_ref = fields.Char(string="Subscription Reference", copy=False)
    sh_date_of_next_payment = fields.Date(string="Date Of Next Payment")
    sh_subscription_id = fields.Many2one("sh.subscription.subscription", string="Subscription Id", copy=False)
    sh_invoice_count = fields.Integer(string="Subscription", compute='compute_view_invoice', default=0, copy=False)
    sh_order_ref_id = fields.Many2one("sale.order", string="Order Ref", readonly=True, copy=False)
    sh_renew_stage = fields.Selection([('not_time_to_renew', 'Not Time TO Renew'), ('time_to_renew', 'Time TO Renew')], string='Renew State', default='not_time_to_renew', copy=False)
    sh_last_payment_status = fields.Selection([('paid', 'Paid'), ('unpaid', 'Unpaid')], string='Invoice Status', compute='compute_sh_last_payment_status', readonly=True, copy=False, default='unpaid', search='search_sh_last_payment_status')
    sh_amount_due = fields.Float(string='Amount Due', compute='compute_sh_amount_due', search='search_sh_amount_due', copy=False)
    sh_reason = fields.Char(string='Cancel/Close Reason', copy=False)
    sh_renewed = fields.Boolean(string="Renewed")
    sh_recurring_monthly = fields.Float(compute='_compute_sh_recurring_monthly', string="Monthly Recurring Revenue", store=True)

    @api.depends('sh_plan_price', 'sh_recurrency', 'sh_unit')
    def _compute_sh_recurring_monthly(self):
        for sub in self:
            sub.sh_recurring_monthly = (sub.sh_plan_price * INTERVAL_FACTOR.get(sub.sh_unit, 0) / sub.sh_recurrency) if sub.sh_plan_price and sub.sh_recurrency else 0.0

    def _sh_send_subscription_email(self, attachment_id):
        self.ensure_one()
        template_id = self.env.ref('sh_subscription.sh_subscription_customer_email')
        if template_id:
            email_values = {'attachment_ids': [(6, 0, attachment_id.ids)]} if attachment_id else {}
            template_id.sudo().send_mail(self.id, force_send=True, email_values=email_values)

    def compute_sh_last_payment_status(self):
        for rec in self:
            last_payment_status = self.env['account.move'].search([('sh_subscription_id', '=', rec.id)], limit=1)
            rec.sh_last_payment_status = 'paid' if last_payment_status.amount_residual == 0 else 'unpaid'

    def compute_sh_amount_due(self):
        for rec in self:
            rec.sh_amount_due = sum(move.amount_residual for move in self.env['account.move'].search([('sh_subscription_id', '=', rec.id)]))

    def search_sh_last_payment_status(self, operator, value):
        """Optimized search using SQL subquery instead of O(n) iteration."""
        self.env.cr.execute("""
            SELECT DISTINCT sub.id
            FROM sh_subscription_subscription sub
            LEFT JOIN account_move inv ON inv.sh_subscription_id = sub.id
            WHERE (
                CASE WHEN %s = 'paid' THEN COALESCE(inv.amount_residual, 0) = 0
                     ELSE COALESCE(inv.amount_residual, 1) != 0
                END
            )
        """, (value,))
        subscription_ids = [row[0] for row in self.env.cr.fetchall()]
        return [('id', 'in', subscription_ids)]

    def search_sh_amount_due(self, operator, value):
        """Optimized search using SQL subquery instead of O(n) iteration."""
        self.env.cr.execute("""
            SELECT sub.id, COALESCE(SUM(inv.amount_residual), 0) as total_due
            FROM sh_subscription_subscription sub
            LEFT JOIN account_move inv ON inv.sh_subscription_id = sub.id
            GROUP BY sub.id
            HAVING (
                CASE WHEN %s = '=' THEN COALESCE(SUM(inv.amount_residual), 0) = %s
                     WHEN %s = '!=' THEN COALESCE(SUM(inv.amount_residual), 0) != %s
                     WHEN %s = '>' THEN COALESCE(SUM(inv.amount_residual), 0) > %s
                     WHEN %s = '<' THEN COALESCE(SUM(inv.amount_residual), 0) < %s
                     WHEN %s = '>=' THEN COALESCE(SUM(inv.amount_residual), 0) >= %s
                     ELSE COALESCE(SUM(inv.amount_residual), 0) <= %s
                END
            )
        """, (operator, value, operator, value, operator, value, operator, value, operator, value, operator, value))
        subscription_ids = [row[0] for row in self.env.cr.fetchall()]
        return [('id', 'in', subscription_ids)]

    def _subscription_renew_button_visible(self):
        current_date = fields.Date.today()
        for subscription in self.env['sh.subscription.subscription'].sudo().search([('state', '=', 'in_progress')]):
            if subscription.sh_end_date and subscription.sh_end_date - relativedelta(days=subscription.sh_company_id.sh_renewal_days) == current_date:
                subscription.sh_renew_stage = 'time_to_renew'

    def _subscription_email_subject(self):
        return {
            'draft': 'Waiting',
            'in_progress': 'Renewed' if self.sh_renewed else 'Active',
            'cancel': 'Cancelled',
            'close': 'Finished',
            'expire': 'Expired',
            'renewed': 'Renewed'
        }.get(self.state, '')

    def _calculate_trial_dates(self):
        if self.sh_subscription_id and self.sh_subscription_id.state == 'in_progress':
            self.sh_start_date = self.sh_subscription_id.sh_end_date + relativedelta(days=1)
        else:
            self.sh_start_date = date.today()
        self.sh_date_of_next_payment = self.sh_start_date

    def _update_trial_dates(self, temp_date):
        unit_deltas = {'day': relativedelta(days=self.sh_trial_duration),
                       'week': relativedelta(weeks=self.sh_trial_duration),
                       'month': relativedelta(months=self.sh_trial_duration),
                       'year': relativedelta(years=self.sh_trial_duration)}
        self.sh_start_date = temp_date + unit_deltas.get(self.sh_trial_unit, relativedelta())
        self.sh_date_of_next_payment = self.sh_start_date

    @api.onchange('sh_trial', 'sh_trial_duration', 'sh_trial_unit', 'sh_subscription_plan_id', 'sh_partner_id', 'product_id')
    def _onchange_sh_trial_subcription_start_date(self):
        self._calculate_trial_dates()
        sub_exists = self.env['sh.subscription.subscription'].sudo().search([
            ('sh_partner_id', '=', self.sh_partner_id.id),
            ('sh_subscription_plan_id', '=', self.sh_subscription_plan_id.id),
            ('product_id', '=', self.product_id.id)
        ])
        if sub_exists and sub_exists[-1].id != self.id:
            self.sh_trial = False
            self.sh_trial_end_date = False
        elif self.sh_trial:
            self._update_trial_dates(date.today() if not self.sh_subscription_id else self.sh_subscription_id.sh_end_date + relativedelta(days=1))
        elif self.sh_subscription_plan_id.sh_free_trial_for_current_month:
            date_value = fields.Date.today()
            trial_end_date = date_value.replace(day=monthrange(date_value.year, date_value.month)[1])
            self.sh_trial_end_date = trial_end_date
            self.sh_start_date = trial_end_date + relativedelta(days=1)
            self.sh_date_of_next_payment = self.sh_start_date + relativedelta(days=self.sh_subscription_plan_id.sh_billing_day_of_the_month - 1)

    @api.onchange('sh_start_date', 'sh_recurrency', 'sh_unit', 'sh_no_of_billing_cycle', 'sh_subscription_plan_id')
    def _onchange_sh_trial_subcription_end_date(self):
        if not self.sh_subscription_plan_id.sh_never_expire:
            cycle_deltas = {'day': relativedelta(days=self.sh_recurrency * self.sh_no_of_billing_cycle),
                            'week': relativedelta(weeks=self.sh_recurrency * self.sh_no_of_billing_cycle),
                            'month': relativedelta(months=self.sh_recurrency * self.sh_no_of_billing_cycle),
                            'year': relativedelta(years=self.sh_recurrency * self.sh_no_of_billing_cycle)}
            self.sh_end_date = self.sh_start_date + cycle_deltas.get(self.sh_unit, relativedelta()) - relativedelta(days=1)

    @api.onchange('sh_partner_id')
    def _onchange_sh_partner_id(self):
        self.sh_partner_invoice_id = self.sh_partner_id.child_ids.filtered(lambda x: x.type == 'invoice')[:1].id or self.sh_partner_id.id

    @api.onchange('sh_subscription_plan_id', 'sh_partner_id')
    def _onchange_sh_subscription_plan_id(self):
        sub_exists = self.env['sh.subscription.subscription'].sudo().search([
            ('sh_partner_id', '=', self.sh_partner_id.id),
            ('sh_subscription_plan_id', '=', self.sh_subscription_plan_id.id),
            ('product_id', '=', self.product_id.id)
        ])
        self.sh_recurrency = self.sh_subscription_plan_id.sh_duration
        self.sh_unit = self.sh_subscription_plan_id.sh_unit
        self.sh_trial = not (sub_exists and sub_exists[-1].id != self.id) and self.sh_subscription_plan_id.sh_trial
        self.sh_trial_duration = self.sh_subscription_plan_id.sh_trial_duration
        self.sh_trial_unit = self.sh_subscription_plan_id.sh_trial_unit
        self.sh_no_of_billing_cycle = -1 if self.sh_subscription_plan_id.sh_never_expire else self.sh_subscription_plan_id.sh_no_of_billing_cycle

    @api.onchange('product_id', 'sh_subscription_plan_id')
    def _onchange_sh_product_id(self):
        sub_exists = self.env['sh.subscription.subscription'].sudo().search([
            ('sh_partner_id', '=', self.sh_partner_id.id),
            ('product_id', '=', self.product_id.id),
            ('sh_subscription_plan_id', '=', self.sh_subscription_plan_id.id)
        ])
        self.sh_subscription_plan_id = self.product_id.sh_subscription_plan_id
        self.sh_taxes_ids = self.product_id.taxes_id.filtered_domain(self.env['account.tax']._check_company_domain(self.sh_company_id)).ids
        self.sh_recurrency = self.sh_subscription_plan_id.sh_duration
        self.sh_unit = self.sh_subscription_plan_id.sh_unit
        self.sh_trial = not (sub_exists and sub_exists[-1].id != self.id) and self.sh_subscription_plan_id.sh_trial
        self.sh_trial_duration = self.sh_subscription_plan_id.sh_trial_duration
        self.sh_trial_unit = self.sh_subscription_plan_id.sh_trial_unit
        self.sh_plan_price = self.product_id.sh_subscription_plan_id.sh_plan_price

    def _generate_invoice_vals(self):
        price = 0.0
        if self.product_id and self.product_id.sh_subscription_plan_id and self.product_id.sh_subscription_plan_id.sh_override_product :
            price = self.sh_plan_price
        else:
            price = self.product_id.list_price + (self.product_id.price_extra or 0.0)

        return {
            'sh_subscription_id': self.id,
            'partner_id': self.sh_partner_id.id,
            'invoice_date': fields.Date.today(),
            'currency_id': self.currency_id.id,
            'journal_id': self.sh_company_id.sh_journal_id.id,
            'move_type': 'out_invoice',
            'invoice_line_ids': [(0, 0, {
                'product_id': self.product_id.id,
                'name': self.product_id.display_name,
                'currency_id': self.currency_id.id,
                'quantity': self.sh_qty,
                'price_unit': price,
                'tax_ids': [(6, 0, self.sh_taxes_ids.ids)],
            })]
        }

    def _handle_invoice_creation(self, invoice_id):
        if self.sh_company_id.sh_invoice_generated == 'post':
            invoice_id.sudo().action_post()
        elif self.sh_company_id.sh_invoice_generated == 'paid':
            if not self.sh_company_id.sh_paid_subscription_journal:
                raise UserError(_("Default Journal not found."))
            invoice_id.action_post()
            journal = self.sh_company_id.sh_paid_subscription_journal
            payment_method_line = journal.inbound_payment_method_line_ids[:1]
            payment = self.env['account.payment'].sudo().create({
                'journal_id': journal.id,
                'amount': invoice_id.amount_total,
                'payment_type': 'inbound',
                'payment_method_line_id': payment_method_line.id if payment_method_line else False,
                'partner_type': 'customer',
                'partner_id': invoice_id.partner_id.id,
            })
            payment.action_post()
            # Reconcile payment with invoice for proper payment_state update
            receivable_lines = invoice_id.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
            )
            payment_lines = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
            )
            if receivable_lines and payment_lines:
                (receivable_lines + payment_lines).reconcile()

    def sh_subscription_confirm(self):
        self.ensure_one()
        if not self.active:
            raise UserError('You cannot confirm an inactive subscription.')

        self.state = 'in_progress'
        if fields.Date.today() >= self.sh_start_date:
            invoice_vals = self._generate_invoice_vals()
            invoice_id = self.env['account.move'].sudo().create(invoice_vals)
            self.compute_view_invoice()

            next_payment_date = self.sh_date_of_next_payment + \
                                relativedelta(days=self.sh_recurrency) if self.sh_unit == 'day' else \
                                self.sh_date_of_next_payment + \
                                relativedelta(weeks=self.sh_recurrency) if self.sh_unit == 'week' else \
                                self.sh_date_of_next_payment + \
                                relativedelta(months=self.sh_recurrency) if self.sh_unit == 'month' else \
                                self.sh_date_of_next_payment + \
                                relativedelta(years=self.sh_recurrency) if self.sh_unit == 'year' else False

            if next_payment_date:
                self.sh_date_of_next_payment = next_payment_date

            self._handle_invoice_creation(invoice_id)
            self._sh_send_subscription_email(False)
            action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_out_invoice_type")
            if len(invoice_id) == 1:
                form_view = [(self.env.ref('account.view_move_form').id, 'form')]
                action['views'] = form_view + [(state, view) for state, view in action['views'] if view != 'form']
                action['res_id'] = invoice_id.id
            return action

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals['name'] = self.env['ir.sequence'].next_by_code('sh.subscription.subscription.name') or _('New')
        return super().create(vals_list)

    def compute_view_invoice(self):
        for rec in self:
            rec.sh_invoice_count = self.env['account.move'].search_count([('sh_subscription_id', '=', rec.id)])

    def sh_action_view_invoice(self):
        self.ensure_one()
        invoices = self.env['account.move'].sudo().search([('sh_subscription_id', '=', self.id)])
        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_out_invoice_type")
        if len(invoices) > 1:
            action['domain'] = [('id', 'in', invoices.ids)]
        elif len(invoices) == 1:
            form_view = [(self.env.ref('account.view_move_form').id, 'form')]
            action['views'] = form_view + [(state, view) for state, view in action['views'] if view != 'form']
            action['res_id'] = invoices.id
        else:
            action = {'type': 'ir.actions.act_window_close'}
        return action

    def sh_generate_invoice(self):
        self.ensure_one()
        if fields.Date.today() < self.sh_start_date:
            raise UserError("You cannot create the invoice due to trial period.")

        invoice_vals = self._generate_invoice_vals()
        invoice_id = self.env['account.move'].sudo().create(invoice_vals)
        self.compute_view_invoice()
        next_payment_date = self.sh_date_of_next_payment + \
                            relativedelta(days=self.sh_recurrency) if self.sh_unit == 'day' else \
                            self.sh_date_of_next_payment + \
                            relativedelta(weeks=self.sh_recurrency) if self.sh_unit == 'week' else \
                            self.sh_date_of_next_payment + \
                            relativedelta(months=self.sh_recurrency) if self.sh_unit == 'month' else \
                            self.sh_date_of_next_payment + \
                            relativedelta(years=self.sh_recurrency) if self.sh_unit == 'year' else False

        if next_payment_date:
            self.sh_date_of_next_payment = next_payment_date

        self._handle_invoice_creation(invoice_id)
        pdf_content, _content_type = self.env['ir.actions.report'].sudo()._render_qweb_pdf(
            'account.account_invoices', invoice_id.id
        )
        invoice_data = base64.b64encode(pdf_content)
        attachment_id = self.env['ir.attachment'].sudo().create({
            'name': "Subscription Invoice",
            'type': 'binary',
            'datas': invoice_data,
            'mimetype': 'application/pdf',
            'res_id': invoice_id.id,
            'res_model': 'account.move'
        })
        self._sh_send_subscription_email(attachment_id)

        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_out_invoice_type")
        if len(invoice_id) == 1:
            form_view = [(self.env.ref('account.view_move_form').id, 'form')]
            action['views'] = form_view + [(state, view) for state, view in action['views'] if view != 'form']
            action['res_id'] = invoice_id.id
        return action

    @api.model
    def _sh_create_auto_invoice(self):
        subscriptions = self.env['sh.subscription.subscription'].sudo().search([('state', '=', 'in_progress')])
        for subscription in subscriptions:
            if subscription.sh_date_of_next_payment == fields.Date.today():
                invoice_vals = self._generate_invoice_vals()
                invoice_id = self.env['account.move'].sudo().create(invoice_vals)
                subscription.compute_view_invoice()

                next_payment_date = subscription.sh_date_of_next_payment + \
                                    relativedelta(days=subscription.sh_recurrency) if subscription.sh_unit == 'day' else \
                                    subscription.sh_date_of_next_payment + \
                                    relativedelta(weeks=subscription.sh_recurrency) if subscription.sh_unit == 'week' else \
                                    subscription.sh_date_of_next_payment + \
                                    relativedelta(months=subscription.sh_recurrency) if subscription.sh_unit == 'month' else \
                                    subscription.sh_date_of_next_payment + \
                                    relativedelta(years=subscription.sh_recurrency) if subscription.sh_unit == 'year' else False

                if next_payment_date:
                    subscription.sh_date_of_next_payment = next_payment_date

                subscription._handle_invoice_creation(invoice_id)
                pdf_content, _content_type = self.env['ir.actions.report'].sudo()._render_qweb_pdf(
                    'account.account_invoices', invoice_id.id
                )
                invoice_data = base64.b64encode(pdf_content)
                attachment_id = self.env['ir.attachment'].sudo().create({
                    'name': "Subscription Invoice",
                    'type': 'binary',
                    'datas': invoice_data,
                    'mimetype': 'application/pdf',
                    'res_id': invoice_id.id,
                    'res_model': 'account.move'
                })
                subscription._sh_send_subscription_email(attachment_id)

    @api.model
    def _sh_auto_expired_subscription(self):
        for subscription in self.env['sh.subscription.subscription'].sudo().search([('state', '=', 'in_progress')]):
            if subscription.sh_end_date and subscription.sh_end_date == fields.Date.today():
                subscription.state = 'renewed' if self.env['sh.subscription.subscription'].sudo().search([('sh_subscription_id', '=', subscription.id)]) else 'expire'
                subscription._sh_send_subscription_email(False)

    @api.model
    def _sh_auto_reminder_subscription(self):
        for subscription in self.env['sh.subscription.subscription'].sudo().search([('state', 'in', ['in_progress', 'expire'])]):
            for reminder in subscription.sh_subscription_plan_id.sh_reminder:
                reminder_date = subscription.sh_end_date + {
                    'day(s)': relativedelta(days=reminder.sh_reminder),
                    'week(s)': relativedelta(weeks=reminder.sh_reminder),
                    'month(s)': relativedelta(months=reminder.sh_reminder)
                }.get(reminder.sh_reminder_unit, relativedelta())
                if reminder_date == fields.Date.today() and reminder.sh_mail_template_id:
                    reminder.sh_mail_template_id.sudo().send_mail(subscription.id, force_send=True)
