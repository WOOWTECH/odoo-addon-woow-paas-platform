# -*- coding: utf-8 -*-
# Part of Softhealer Technologies.

from odoo import fields, models
class SubscriptionReason(models.Model):

    _name = 'sh.subscription.reason'
    _description = 'Subscription Reason'
    _order = "sequence"

    name = fields.Char(string='Reason', required=True)
    sequence = fields.Integer('Sequence', default=10)
