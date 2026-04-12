from odoo import fields, models


class TopupPlan(models.Model):
    _name = 'woow_paas_platform.topup_plan'
    _description = 'Credit Top-up Plan'
    _order = 'sequence, id'

    name = fields.Char(string='Name', required=True)  # "NT$ 100 Credits"
    amount = fields.Float(string='Price (NT$)', required=True)  # purchase price
    credits = fields.Float(string='Credits', required=True)  # credits received
    bonus = fields.Float(string='Bonus Credits', default=0)  # extra bonus
    label = fields.Char(string='Badge Label')  # "BEST VALUE"
    category = fields.Selection([
        ('all', 'All'),
        ('recommended', 'Recommended'),
        ('large_volume', 'Large Volume'),
    ], string='Category', default='all')
    icon = fields.Char(string='Icon Name', default='toll')  # Material icon name
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
