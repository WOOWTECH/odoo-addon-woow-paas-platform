# -*- coding: utf-8 -*-
"""#336：plan 前台可見性控制（add-only 擴充，taxonomy §3.5 佈局）。

storefront_hidden 刻意不用 Odoo `active`：active=False 會被 active_test
全域過濾，帶隱藏 plan 的既存訂閱在 cron／結算 search 會被靜默漏掉。
"""
import logging
import re

from odoo import fields, models

_logger = logging.getLogger(__name__)

# 席數變體 xmlid 慣例（scripts/seed/generate_plan_variants.py 產出）：
# 變體 {base}_ac{n}_{cycle} ←→ base {base}_{cycle}（cycle ∈ monthly/yearly）。
# greedy .+ 回溯到最右的 _ac{n}_ marker；SA 的 `_access_` 因 ac 後接非數字不誤匹配。
_VARIANT_XMLID_RE = re.compile(
    r'^(?P<base>.+)_ac\d+_(?P<cycle>monthly|yearly)$')
_SEED_MODULE = 'woow_paas_platform'


class ShSubscriptionPlanVisibility(models.Model):
    _inherit = 'sh.subscription.plan'

    storefront_hidden = fields.Boolean(
        string='前台隱藏',
        default=False,
        help='勾選後此方案不出現在 /woow 前台任何購買選項；'
             '既存訂閱的計費、續期、退點完全不受影響。',
    )

    def _attach_variants(self):
        """把 self 內的隱藏席數變體以 xmlid 反查歸戶到各自 base plan。

        回傳 ``{base_plan_id: [{'seats', 'plan_id', 'price'}, ...]}``（seats 升序）。
        變體判別＝``ac_max_access_seats > 0 ∧ storefront_hidden``（Wave B generator
        產出）。base↔變體無 runtime FK，僅靠 generator xmlid 慣例
        ``{base}_ac{n}_{cycle}`` ↔ ``{base}_{cycle}``（R8 xmlid 定位鐵律）。

        呼叫慣例：``template.subscription_plan_ids._attach_variants()``（每 request
        一次）；base 反解走 ir.model.data、不依賴 base 是否在 self 內。

        fail-loud 邊界（運行時寬容）：變體 xmlid 缺失／不符 ``_ac{n}_{cycle}`` 慣例／
        base 反解不出存在 record → ``_logger.error`` ＋跳過該筆（端點不炸，選購
        可用性優先）；測試層 test_wave_c_variants_attach_330 全庫斷言 208 變體零
        跳過、皆掛可見 base，把慣例釘死。
        """
        variants = self.filtered(
            lambda p: p.ac_max_access_seats > 0 and p.storefront_hidden)
        if not variants:
            return {}

        imd = self.env['ir.model.data'].sudo()
        # 1) 批次反查變體 xmlid（單次 in-query，禁 N+1）
        variant_rows = imd.search([
            ('model', '=', 'sh.subscription.plan'),
            ('module', '=', _SEED_MODULE),
            ('res_id', 'in', variants.ids),
        ])
        xmlid_by_res = {row.res_id: row.name for row in variant_rows}

        # 2) parse → 重組 base xmlid，收集待反解的 base xmlid 集合
        base_xmlid_by_variant = {}
        base_xmlids = set()
        for variant in variants:
            name = xmlid_by_res.get(variant.id)
            if not name:
                _logger.error(
                    '_attach_variants：變體 plan id=%s 無 %s xmlid，跳過',
                    variant.id, _SEED_MODULE)
                continue
            match = _VARIANT_XMLID_RE.match(name)
            if not match:
                _logger.error(
                    '_attach_variants：變體 xmlid %s 不符 _ac{n}_{cycle} 慣例，跳過',
                    name)
                continue
            base_xmlid = '%s_%s' % (match.group('base'), match.group('cycle'))
            base_xmlid_by_variant[variant.id] = base_xmlid
            base_xmlids.add(base_xmlid)

        if not base_xmlids:
            return {}

        # 3) 批次反解 base xmlid → base plan id（單次 in-query）
        base_rows = imd.search([
            ('model', '=', 'sh.subscription.plan'),
            ('module', '=', _SEED_MODULE),
            ('name', 'in', list(base_xmlids)),
        ])
        base_id_by_xmlid = {row.name: row.res_id for row in base_rows}

        # 4) group by base、seats 升序
        result = {}
        for variant in variants:
            base_xmlid = base_xmlid_by_variant.get(variant.id)
            if not base_xmlid:
                continue  # 已於步驟 2 log
            base_id = base_id_by_xmlid.get(base_xmlid)
            if not base_id:
                _logger.error(
                    '_attach_variants：變體 %s 反解 base xmlid %s 無對應 record，跳過',
                    xmlid_by_res.get(variant.id), base_xmlid)
                continue
            result.setdefault(base_id, []).append({
                'seats': variant.ac_max_access_seats,
                'plan_id': variant.id,
                'price': variant.sh_plan_price,
            })
        for entries in result.values():
            entries.sort(key=lambda entry: entry['seats'])
        return result

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
