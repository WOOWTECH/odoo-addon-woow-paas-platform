#!/usr/bin/python3
# @Time    : 2023-09-11
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _

SENDER_TYPES = [
    ('company', "Using picking's comany address as sender address."),
    ('stock', 'Using warehouse address as sender address.'),
    ('fixed', 'Specify fixed partner as sender addres.')
]

CUSTOMER_REFERENCE = [
    ('origin', 'Using the origin as customer reference'),
    ('name', 'Using picking number as customer reference'),
    ('both', 'Using origin-name as customer referencez')
]


class delivery_carrier(models.Model):
    _inherit = "delivery.carrier"

    
    customer_reference = fields.Selection(
        CUSTOMER_REFERENCE, string='Customer Reference', default="origin")
    fixed_sender = fields.Many2one("res.partner", string="Fixed Sender")
    goods_name = fields.Char("Goods Name", default="Goods")
    sender_type = fields.Selection(
        SENDER_TYPES, string="Sender Type", default="company")

    def get_sender(self):
        """
        get sender type
        """
        sender_type = self.sender_type
        if sender_type == "fixed":
            return self.fixed_sender
        elif sender_type == "stock":
            return self.location_id.warehouse_id.partner_id
        else:
            return self.company_id.partner_id
    

    def install_more_provider(self):
        res = super().install_more_provider()
        res['domain'] = ['|', ['name', '=like', 'mommy_delivery_%']] + res['domain']
        return res
