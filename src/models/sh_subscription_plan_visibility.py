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

# #368：錨定 $ 只截 cycle 尾綴（group_key = base xmlid 去 _monthly/_yearly）
_CYCLE_SUFFIX_RE = re.compile(r'_(monthly|yearly)$')


class ShSubscriptionPlanVisibility(models.Model):
    _inherit = 'sh.subscription.plan'

    storefront_hidden = fields.Boolean(
        string='Hidden From Storefront',
        default=False,
        help='When ticked, this plan does not appear in any purchase option on '
             'the /woow front end; billing, renewal and refunds for existing '
             'subscriptions are completely unaffected.',
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

    def _group_keys(self):
        """回 {base_plan_id: group_key}。group_key = base xmlid 去 _monthly/_yearly
        尾綴（月/年同 tier 得同 group_key，前端據此配對 cycle toggle）。

        只對【可見 base】（ac_max_access_seats==0 ∧ not storefront_hidden）算，
        單次批次 ir.model.data 反查（禁 N+1）。無 xmlid／不符 _cycle 慣例 →
        _logger.error＋略過（該 plan 前端 fallback 用 id 當獨立 group，端點不炸）。
        """
        bases = self.filtered(
            lambda p: p.ac_max_access_seats == 0 and not p.storefront_hidden)
        if not bases:
            return {}
        imd = self.env['ir.model.data'].sudo()
        rows = imd.search([
            ('model', '=', 'sh.subscription.plan'),
            ('module', '=', _SEED_MODULE),
            ('res_id', 'in', bases.ids),
        ])
        result = {}
        for row in rows:
            gk = _CYCLE_SUFFIX_RE.sub('', row.name)
            if gk == row.name:
                _logger.error(
                    '_group_keys：base xmlid %s 無 _monthly/_yearly 尾綴，略過',
                    row.name)
                continue
            result[row.res_id] = gk
        return result

    def _get_display_specs(self, template=None):
        """前台卡片規格 [{'icon','label'}]，service_type 分流；一套元件走天下。

        CS：vCPU/RAM（plan.cs_* 為 0＝沿用 template 預設 → fallback template.min_*）。
        SA：routes/policies。SH：p2（mcp_integration 非空）顯示 MCP 加密、p1 無規格。
        變體規格恆等於同 tier base，故此 method 只需對 base 呼叫。
        """
        self.ensure_one()
        specs = []
        if self.service_type == 'cloud_service':
            vcpu = self.cs_vcpu or (template.min_vcpu if template else 0)
            ram = self.cs_ram_gb or (template.min_ram_gb if template else 0.0)
            if vcpu:
                specs.append({
                    'icon': 'memory',
                    'label': '%s vCPU · %s GB RAM' % (vcpu, ram),
                })
        elif self.service_type == 'security_access':
            # ⚠ 兩個值**分開**格式化，不共用一個特判——兩個欄位對 0 的語意
            # 相反（#743／sh_subscription_plan_sa_quota.py）：
            #   sa_max_routes          0 = none allowed
            #   sa_max_access_policies 0 = unlimited
            # 擠在同一個字串裡時，一個 `or 'unlimited'` 會同時蓋到兩個。
            routes_text = '%s routes' % self.sa_max_routes  # 0 維持字面 0
            # routes 側該顯示成什麼是 #770 的 quota 語意拍板題，#750 不代決。
            policies_text = (
                '%s policies' % self.sa_max_access_policies
                if self.sa_max_access_policies else 'unlimited policies')
            specs.append({
                'icon': 'route',
                'label': '%s · %s' % (routes_text, policies_text),
            })
        elif self.service_type == 'smart_home':
            if self.mcp_integration:
                specs.append({'icon': 'lock',
                              'label': 'Encrypted MCP connection'})
        return specs

    # #336 A5（hide，18.0.1.0.54）與 #431（unhide，18.0.1.0.79）共用的定位清單。
    _HERMES_PLAN_XMLIDS = [
        'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly',
        'plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly',
        'plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly',
    ]

    def _woow_migrate_hide_hermes_336(self):
        """A5 封存 Hermes：升級軌 force-write（noupdate XML 不會回填既有 record）。

        一律 env.ref() xmlid 定位（R8 鐵律）；xmlid 不存在（fresh DB 未載 seed）
        時跳過不炸。冪等。

        ⚠️ 不可刪除：18.0.1.0.54/post-migrate.py 仍在呼叫本 method，「從 .53 以前
        升上來」的 DB 會依序跑 .54（hide）→ .79（unhide），終態為 #431 的可見。
        #431 解封請走 _woow_migrate_unhide_hermes_431()，不要改本 method 的語意。
        """
        for xmlid in self._HERMES_PLAN_XMLIDS:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if plan:
                plan.storefront_hidden = True

    def _woow_migrate_unhide_hermes_431(self):
        """#431 解封 Hermes：升級軌 force-write（seed 軌只對 fresh DB 生效）。

        subscription_products.xml 是 noupdate=1，既有 DB（stg／prod）不會被 seed
        的 storefront_hidden 移除回填，只能由本 method 翻正。與
        _woow_migrate_hide_hermes_336() 是相反方向的兩個 method，各自對應自己的
        migration 版本，互不覆寫。冪等；xmlid 不存在時跳過不炸。
        """
        for xmlid in self._HERMES_PLAN_XMLIDS:
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if plan:
                plan.storefront_hidden = False
