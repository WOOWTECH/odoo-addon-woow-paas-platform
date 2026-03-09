#!/usr/bin/python3
# @Time    : 2022-12-10
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _


class sf_delivery_cargo(models.Model):
    _name = "sf.delivery.cargo"
    _description = "SF delivery cargo description"

    name = fields.Char("name")