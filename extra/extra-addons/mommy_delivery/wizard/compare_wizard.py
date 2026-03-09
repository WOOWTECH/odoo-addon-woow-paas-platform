#!/usr/bin/python3
# @Time    : 2024-08-27
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _


class mommy_delivery_carrier_comparison_wizard(models.TransientModel):
    _name = "delivery.carrier.comparison.wizard"
    _description = "Carrier Comparison Wizard"

    def _generate_tmp_product(self, weight, length, width, height):
        """
        genterate tmp product for precompute.
        """
        comparison_product_id = self.env.ref("mommy_delivery.mommy_delivery_comparison_product")
        # set the lwh 
        comparison_product_id.update({
            "weight": weight,
            "length": length,
            "width": width,
            "height": height
        })
        return comparison_product_id.product_variant_id.id

    org_partner_id = fields.Many2one("res.partner",string="Sender")
    dest_partner_id = fields.Many2one("res.partner", string="Receiver")
    weight = fields.Float("Weight")
    length = fields.Float("Length")
    width = fields.Float("Width")
    height = fields.Float("Hight")
    lines = fields.One2many("delivery.carrier.comparison.wizard.line","wizard_id",string="Lines")

    @api.model
    def default_get(self, field_vals):
        """
        Override to compute default values
        """
        res = super().default_get(field_vals)
        res['dest_partner_id'] = self.active_records.partner_id.id
        res['org_partner_id'] = self.env.company.partner_id.id
        if self.active_model._name == "stock.picking":
            picking = self.active_records
            weight = sum(move.product_id.weight * move.product_uom_qty for move in picking.move_ids_without_package)
        if self.active_model._name == "sale.order":
            orders = self.active_records
            weight = sum(line.product_id.weight * line.product_uom_qty for line in orders.order_line)
        res['weight'] = weight
        return res

    def compute_comparison(self):
        """
        compute comparison
        """
        if self.dest_partner_id:
            carrier_obj = self.env['delivery.carrier'].sudo()
            carrier_ids = carrier_obj.search([])
            order_obj = self.env['sale.order'].sudo()
            product_id = self._generate_tmp_product(self.weight, self.length, self.width, self.height)
            # build the tmp order
            data = {
                "partner_id": self.dest_partner_id.id,
                "order_line": [(0,0, {
                    "product_id": product_id,
                    "product_uom_qty": 1,
                    "price_unit": 0,
                })]
            }
            tmp_order = order_obj.create(data)
            lines = [(5,)]
            for carrier in carrier_ids:
                res = getattr(carrier, f"{carrier.delivery_type}_rate_shipment")(tmp_order)
                if res['success']:
                    lines.append((0,0, {
                        "carrier_id": carrier.id,
                        "fee": res['price']
                    }))
            self.lines = lines
            tmp_order.unlink()


        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode":"form",
            "target": "new",
            "context": {
                "active_id": self._context.get("active_id"),
                "active_ids": self._context.get("active_ids"),
                "active_model": self._context.get("active_model")
            }
        }

    def action_confirm(self):
        pass


class mommy_delivery_carrier_comparison_wizard_line(models.TransientModel):
    _name = "delivery.carrier.comparison.wizard.line"
    _description = "Carrier Comparison Wizard Lines"

    carrier_id = fields.Many2one("delivery.carrier",string="Carrier")
    fee = fields.Float("Fee")
    wizard_id = fields.Many2one("delivery.carrier.comparison.wizard",string="Wizard")

    def action_update(self):
        """
        update the carrier in picking
        """
        self.ensure_one()
        if self.active_model._name == "stock.picking":
            self.active_records.carrier_id = self.carrier_id
        if self.active_model._name == "sale.order":
            self.active_records.set_delivery_line(self.carrier_id, self.fee)