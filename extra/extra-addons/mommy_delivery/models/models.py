#!/usr/bin/python3
# @Time    : 2023-09-11
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _
from autils.string import randomstr


class stock_picking(models.Model):
    _inherit = "stock.picking"

    def _make_carrier_origin(self):
        """
        make carrier origin
        """
        for picking in self:
            picking.carrier_origin = randomstr.generate(12)

    carrier_origin = fields.Char("Carrier Origin")
    goods_name = fields.Char("Goods Name")

    def get_sender(self):
        """
        get sender type
        """
        sender_type = self.carrier_id.sender_type
        if sender_type == "fixed":
            return self.carrier_id.fixed_sender
        elif sender_type == "stock":
            return self.location_id.warehouse_id.partner_id
        else:
            return self.env.company.partner_id

    def get_reference(self):
        cf = self.carrier_id.customer_reference
        if cf == "origin":
            return self.origin
        elif cf == "name":
            return self.name
        else:
            return f"{self.origin}-{self.name}"

    def action_compare(self):
        """
        compare the delivery carrier rate
        """
        action = self.env.ref("mommy_delivery.action_delivery_comparison").read()[0]
        return action
