# -*- coding: utf-8 -*-
"""#431：Hermes 重新上架——解除 6 筆 plan 前台隱藏 ＋ 補三階 cs_* 規格。

subscription_products.xml 是 noupdate=1，seed 軌只對 fresh DB 生效；既有 DB
（stg／prod）一律靠本 migration force-write。從 .53 以前升上來的 DB 會先跑
18.0.1.0.54（hide）再跑本檔（unhide），終態為可見。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Plan = env['sh.subscription.plan']
    Plan._woow_migrate_unhide_hermes_431()
    Plan._woow_migrate_hermes_cs_spec_431()
