#!/usr/bin/python3
# @Time    : 2023-09-15
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _


class product_template(models.Model):
    _inherit = "product.template"

    def _compute_length_uom(self):
        self.length_uom_name = self._get_length_uom_name_from_ir_config_parameter()

    height = fields.Float("Height")
    length = fields.Float("Length")
    width = fields.Float("Width")
    length_uom_name  = fields.Char(string='Unit of length', compute="_compute_length_uom")
