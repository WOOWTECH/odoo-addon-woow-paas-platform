# -*- coding: utf-8 -*-
"""#332：CS 部署規格欄位（type-specific，taxonomy §3.2；OQ-H Option B）。

全欄 default 0＝「本 plan 未定義規格」→ 部署碼 fallback template.min_*，
既有 plan 行為零變。
"""
import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ShSubscriptionPlanCsSpec(models.Model):
    _inherit = 'sh.subscription.plan'

    cs_vcpu = fields.Integer(
        string='CS vCPU', default=0,
        help='CPU cores for the deployment main container; '
             '0 = fall back to the template default.')
    cs_ram_gb = fields.Float(
        string='CS RAM (GB)', default=0,
        help='Memory (GB) for the deployment main container; '
             '0 = fall back to the template default.')
    cs_storage_gb = fields.Integer(
        string='CS Storage (GB)', default=0,
        help='PVC size (GB); 0 = fall back to the template default.')
    cs_odoo_workers = fields.Integer(
        string='Odoo workers', default=0,
        help='Odoo only: number of worker processes (tier-aware); '
             '0 = fall back to the template init script default '
             '(workers=0, threaded). Always leave 0 for single-process '
             'services such as n8n.')

    @api.constrains('cs_odoo_workers', 'cs_ram_gb')
    def _check_workers_require_ram(self):
        """workers>0 而 ram 未定＝worker 記憶體上限算不出來（OOM 風險）——禁。"""
        for plan in self:
            if plan.cs_odoo_workers > 0 and not plan.cs_ram_gb:
                raise ValidationError(_(
                    'When cs_odoo_workers is greater than 0, cs_ram_gb must '
                    'be set as well; otherwise the per-worker memory limit '
                    'cannot be computed (OOM risk).'))

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
        self._woow_write_cs_spec(self._CS_SPEC_332)

    # #431：Hermes 重新上架時補的三階規格（同價位對齊 n8n；月繳／年繳同 tier
    # 一致；cs_odoo_workers 恆 0——非 Odoo 服務）。留 0 會 fallback
    # template.min_*（1c/1G/5Gi）→ 三階同規格不同價，正是 #431 要解的病灶。
    _CS_SPEC_HERMES_431 = {
        'plan_mcp_hermes_monthly': (1, 2.0, 20, 0),
        'plan_mcp_hermes_yearly': (1, 2.0, 20, 0),
        'plan_mcp_hermes_t2_monthly': (2, 2.0, 30, 0),
        'plan_mcp_hermes_t2_yearly': (2, 2.0, 30, 0),
        'plan_mcp_hermes_t3_monthly': (2, 4.0, 40, 0),
        'plan_mcp_hermes_t3_yearly': (2, 4.0, 40, 0),
    }

    def _woow_migrate_hermes_cs_spec_431(self):
        """#431 升級軌：Hermes 6 筆 backfill cs_*（xmlid 定位、冪等）。

        seed 軌（subscription_products.xml）是 noupdate=1，既有 DB 只能靠本
        method force-write。與 seed 為同源兩份手寫，
        test_hermes_seed_cs_specs／test_migration_79_force_writes_from_78_state
        兩側各自釘住同一組值。
        """
        self._woow_write_cs_spec(self._CS_SPEC_HERMES_431)

    # #1634（使用者 2026-10-10）：Nextcloud 整台磁碟 96 → 20G；方案 cs_storage_gb 由「使用者
    # 檔案 80」改成「整台總量 20」（CloudService._overlay_plan_storage 再扣掉固定開銷）。
    # (舊 seed 值, 新值)；與 subscription_products.xml 同源兩份手寫，test_nextcloud_storage_20g_1634
    # 兩側各自釘住。
    _NEXTCLOUD_STORAGE_1634 = {
        'plan_nextcloud_monthly': (80, 20),
        'plan_nextcloud_yearly': (80, 20),
    }

    def _woow_migrate_nextcloud_storage_1634(self):
        """#1634 升級軌：兩個 Nextcloud 方案 ``cs_storage_gb`` 80 → 20（xmlid 定位、冪等）。

        seed（subscription_products.xml）是 noupdate=1，既有 DB 只能靠本 method。只改仍是
        舊 seed 值的方案——後台手調過的不覆蓋、只 log。價格與 vCPU／RAM 不動。
        xmlid 不存在（fresh DB 未載 seed）時跳過不炸。
        """
        for xmlid, (old, new) in self._NEXTCLOUD_STORAGE_1634.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if not plan or plan.cs_storage_gb == new:
                continue
            if plan.cs_storage_gb != old:
                _logger.warning(
                    "#1634 %s cs_storage_gb=%s 不是舊 seed 值 %s（後台手調過），不覆蓋",
                    xmlid, plan.cs_storage_gb, old)
                continue
            plan.write({'cs_storage_gb': new})
            _logger.info("#1634 %s cs_storage_gb %s → %s", xmlid, old, new)

    def _woow_write_cs_spec(self, spec_by_xmlid):
        """共用 force-write：{xmlid: (vcpu, ram, storage, workers)}。

        一律 env.ref() xmlid 定位（R8 鐵律）；xmlid 不存在（fresh DB 未載 seed）
        時跳過不炸。
        """
        for xmlid, (vcpu, ram, storage, workers) in spec_by_xmlid.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if plan:
                plan.write({'cs_vcpu': vcpu, 'cs_ram_gb': ram,
                            'cs_storage_gb': storage,
                            'cs_odoo_workers': workers})
