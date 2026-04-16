"""Add a dedup field to sh.subscription.subscription for low-balance warnings."""
from odoo import fields, models


class ShSubscriptionWarningField(models.Model):
    _inherit = 'sh.subscription.subscription'

    paas_last_low_balance_warned_for = fields.Date(
        string='Last Low-Balance Warning Renewal Date',
        help='Date of the upcoming renewal for which a low-balance email was '
             'last sent. Prevents duplicate warnings per renewal cycle.',
        copy=False,
    )
