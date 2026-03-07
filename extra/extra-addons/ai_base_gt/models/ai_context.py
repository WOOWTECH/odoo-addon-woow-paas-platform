from odoo import fields, models


class AIContext(models.Model):
    _name = 'ai.context'
    _description = "AI Context"

    name = fields.Char(string="Name", required=True)
    context = fields.Text(string="Context", required=True)
