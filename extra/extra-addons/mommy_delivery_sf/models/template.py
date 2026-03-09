#!/usr/bin/python3
# @Time    : 2024-07-29
# @Author  : Kevin Kong (kfx2007@163.com)

from odoo import api, fields, models, _


class mommy_sf_template(models.Model):
    _name = "mommy.sf.template"
    _description = "SF E-Template"

    name = fields.Char("Name")
    code = fields.Char("Template Code")