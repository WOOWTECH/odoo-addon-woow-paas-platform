# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.


from odoo import api, fields, models
from odoo.exceptions import UserError


class ResCompany(models.Model):
    _inherit = 'res.company'

    sh_invoice_generated = fields.Selection([("draft", "Draft"), ("post", "Post"), (
        "paid", "Paid")], string="Invoice Generated", default='draft', required=True)
    sh_renewal_days = fields.Integer(
        string='Renewal days', default=1, required=True)
    sh_trial_period_setting = fields.Boolean(string="Trial Period Setting")
    sh_journal_id = fields.Many2one(
        comodel_name="account.journal", string="Payment Method", domain="[('type', 'in', ['sale'])]", required=True)
    sh_default_attribute_id = fields.Many2one(
        comodel_name="product.attribute", string="Product Attribute", required=True, compute='_compute_default_attribute')
    sh_paid_subscription_journal = fields.Many2one(
        comodel_name="account.journal", string="Paid Payment Journal", domain="[('type', 'in', ['bank','cash'])]")
    sh_invoice_email = fields.Boolean(string="Invoice Email")


    def _compute_default_attribute(self):
        default_attribute = self.env.ref(
            'sh_subscription.product_attribute_subscription')
        if default_attribute:
            self.sh_default_attribute_id = default_attribute.id
        else:
            self.sh_default_attribute_id = False



class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    sh_invoice_generated = fields.Selection(
        string="Invoice Generated", related='company_id.sh_invoice_generated', readonly=False)

    sh_renewal_days = fields.Integer(
        string='Renewal days',  related='company_id.sh_renewal_days', readonly=False)
    sh_trial_period_setting = fields.Boolean(
        related='company_id.sh_trial_period_setting', string="Trial Period Setting", readonly=False)
    sh_journal_id = fields.Many2one(
        comodel_name="account.journal", related='company_id.sh_journal_id', string="Payment Method", readonly=False)
    sh_default_attribute_id = fields.Many2one(
        comodel_name="product.attribute", string="Product Attribute", related='company_id.sh_default_attribute_id',ondelete="restrict")
    sh_invoice_email = fields.Boolean(
        string="Invoice Email", related='company_id.sh_invoice_email', readonly=False)
    sh_paid_subscription_journal = fields.Many2one(
        comodel_name="account.journal", string="Paid Payment Journal", related='company_id.sh_paid_subscription_journal', readonly=False)

    @api.onchange('sh_renewal_days')
    def onchange_sh_renewal_days(self):
        if self.sh_renewal_days < 0:
            raise UserError('you can not Enter Renewal days Negative.')
