# -*- coding: utf-8 -*-
"""#332：CS 部署規格欄位（type-specific，taxonomy §3.2；OQ-H Option B）。

全欄 default 0＝「本 plan 未定義規格」→ 部署碼 fallback template.min_*，
既有 plan 行為零變。
"""
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class ShSubscriptionPlanCsSpec(models.Model):
    _inherit = 'sh.subscription.plan'

    cs_vcpu = fields.Integer(
        string='CS vCPU', default=0,
        help='部署主容器 CPU（顆）；0＝沿用 template 預設。')
    cs_ram_gb = fields.Float(
        string='CS RAM (GB)', default=0,
        help='部署主容器記憶體（GB）；0＝沿用 template 預設。')
    cs_storage_gb = fields.Integer(
        string='CS Storage (GB)', default=0,
        help='PVC 容量（GB）；0＝沿用 template 預設。')
    cs_odoo_workers = fields.Integer(
        string='Odoo workers', default=0,
        help='Odoo 專屬：worker process 數（tier-aware）；'
             '0＝沿用 template init script 預設（workers=0 threaded）。'
             'n8n 等單 process 服務一律留 0。')

    @api.constrains('cs_odoo_workers', 'cs_ram_gb')
    def _check_workers_require_ram(self):
        """workers>0 而 ram 未定＝worker 記憶體上限算不出來（OOM 風險）——禁。"""
        for plan in self:
            if plan.cs_odoo_workers > 0 and not plan.cs_ram_gb:
                raise ValidationError(_(
                    'cs_odoo_workers 大於 0 時必須同時設定 cs_ram_gb，'
                    '否則無法計算 per-worker 記憶體上限（OOM 風險）。'))
