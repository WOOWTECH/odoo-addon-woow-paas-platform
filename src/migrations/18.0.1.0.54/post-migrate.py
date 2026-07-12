# -*- coding: utf-8 -*-
"""#336 Wave A：封存 Hermes 6 筆（storefront_hidden force-write）。"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sh.subscription.plan']._woow_migrate_hide_hermes_336()
