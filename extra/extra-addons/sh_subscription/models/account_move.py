# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    sh_subscription_id = fields.Many2one(
        comodel_name="sh.subscription.subscription", string="subscription", groups="sh_subscription.group_user_sh_subscription")
