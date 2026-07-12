# -*- coding: utf-8 -*-
"""#336：plan 前台可見性控制（add-only 擴充，taxonomy §3.5 佈局）。

storefront_hidden 刻意不用 Odoo `active`：active=False 會被 active_test
全域過濾，帶隱藏 plan 的既存訂閱在 cron／結算 search 會被靜默漏掉。
"""
from odoo import fields, models


class ShSubscriptionPlanVisibility(models.Model):
    _inherit = 'sh.subscription.plan'

    storefront_hidden = fields.Boolean(
        string='前台隱藏',
        default=False,
        help='勾選後此方案不出現在 /woow 前台任何購買選項；'
             '既存訂閱的計費、續期、退點完全不受影響。',
    )

    def _woow_migrate_hide_hermes_336(self):
        """A5 封存 Hermes：升級軌 force-write（noupdate XML 不會回填既有 record）。

        一律 env.ref() xmlid 定位（R8 鐵律）；xmlid 不存在（fresh DB 未載 seed）
        時跳過不炸。冪等。
        """
        xmlids = [
            'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly',
            'plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly',
            'plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly',
        ]
        for xmlid in xmlids:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if plan:
                plan.storefront_hidden = True
