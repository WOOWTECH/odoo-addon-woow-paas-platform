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

    # 裁定①（2026-07-12 by Eugene）：T1 storage 採 matrix 值（odoo 20/n8n 10）＝
    # 刻意 tier 提升（>現行 min）。cs_vcpu/cs_ram_gb 兩維＝template.min_*。
    _CS_SPEC_332 = {
        'plan_odoo_monthly': (2, 2.0, 20, 2), 'plan_odoo_yearly': (2, 2.0, 20, 2),
        'plan_odoo_t2_monthly': (2, 4.0, 30, 3), 'plan_odoo_t2_yearly': (2, 4.0, 30, 3),
        'plan_odoo_t3_monthly': (3, 4.0, 40, 4), 'plan_odoo_t3_yearly': (3, 4.0, 40, 4),
        'plan_n8n_monthly': (1, 1.0, 10, 0), 'plan_n8n_yearly': (1, 1.0, 10, 0),
        'plan_n8n_t2_monthly': (1, 2.0, 20, 0), 'plan_n8n_t2_yearly': (1, 2.0, 20, 0),
        'plan_n8n_t3_monthly': (2, 2.0, 30, 0), 'plan_n8n_t3_yearly': (2, 2.0, 30, 0),
    }

    def _woow_migrate_cs_spec_332(self):
        """#332 升級軌：CS 12 筆 backfill cs_*（xmlid 定位、冪等）。

        一律 env.ref() xmlid 定位（R8 鐵律）；xmlid 不存在（fresh DB 未載 seed）
        時跳過不炸。與 XML fresh 軌（subscription_products.xml）為同源兩份手寫，
        test_xml_and_migration_tracks_identical 守其一致。
        """
        for xmlid, (vcpu, ram, storage, workers) in self._CS_SPEC_332.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if plan:
                plan.write({'cs_vcpu': vcpu, 'cs_ram_gb': ram,
                            'cs_storage_gb': storage,
                            'cs_odoo_workers': workers})
