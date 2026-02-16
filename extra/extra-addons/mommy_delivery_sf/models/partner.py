#!/usr/bin/python3
# @Time    : 2023-06-09
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _


class res_partner(models.Model):
    _inherit="res.partner"

    sf_month_code = fields.Char("SF Month Code")