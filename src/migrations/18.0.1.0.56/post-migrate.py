# -*- coding: utf-8 -*-
"""#332 Wave A：CS 12 筆 plan 規格 backfill（cs_* 升級軌 force-write）。"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sh.subscription.plan']._woow_migrate_cs_spec_332()
