from odoo import api, fields, models
from odoo.tools import float_compare


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

    # Recomputed on every read. Safe at N<~50 plans; convert to stored if N grows.
    is_best_value = fields.Boolean(
        string='Best Value',
        compute='_compute_is_best_value',
        help='True when this plan has the highest bonus/credits ratio among active plans.',
    )

    @api.depends('credits', 'bonus', 'active')
    def _compute_is_best_value(self):
        active_plans = self.search([('active', '=', True)])
        ratios = [
            (p.bonus / p.credits) for p in active_plans if p.credits
        ]
        best_ratio = max(ratios) if ratios else 0.0
        for rec in self:
            ratio = (rec.bonus / rec.credits) if rec.credits else 0.0
            rec.is_best_value = (
                ratio > 0
                and float_compare(ratio, best_ratio, precision_digits=6) == 0
            )
