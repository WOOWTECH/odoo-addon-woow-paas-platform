"""18.0.1.0.40 — backfill drift/webhook-reaped session 的 reaped_since。

write() invariant（reaped_since 有值 ⟺ state='reaped'）加入後，新進入 reaped 的
session 都會被補值。本 migration 修復升級前已存在、由 drift/webhook 推成 reaped 卻
reaped_since=NULL 的列（否則它們永久免疫 long-term GC）。冪等。
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.agent_session']._backfill_reaped_since_orphans()
