import logging

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ShSubscriptionPlanServiceType(models.Model):
    """三大類 plan taxonomy discriminator（#330，OQ-A 拍板：3 值）。

    Add-only pattern（比照 sa_quota／sh_mcp，不覆寫 std 欄位）。MCP 類
    （Odoo/n8n MCP、Hermes）歸 cloud_service——MCP-ness 由
    cloud_app_template.category='mcp' 承載，不設第 4 值（拍板記錄見
    docs/plans/2026-Q3/2026-07-10-plan-taxonomy-pricing.md §3.1）。

    為何「存」而非從 template M2M 衍生：orphan plan（AnythingLLM 2 筆，
    無任何 template record）反查不出來；backfill 與前端 filter 需要純量。
    一致性由既有三條 template 互斥 constraint＋下方條件式 guard 兜底。
    """

    _inherit = 'sh.subscription.plan'

    # xmlid（template model）→ 該 family 對應的 service_type 值
    _TEMPLATE_FAMILY = {
        'woow_paas_platform.cloud_app_template': 'cloud_service',
        'woow_paas_platform.smart_home_template': 'smart_home',
        'woow_paas_platform.security_access_template': 'security_access',
    }

    service_type = fields.Selection(
        [('cloud_service', 'Cloud Service'),
         ('smart_home', 'Smart Home'),
         ('security_access', 'Security Access')],
        string='Service Type',
        help='Which of the three service families this plan belongs to. '
             'MCP offerings (Odoo/n8n MCP, Hermes) are cloud_service — '
             'MCP-ness is carried by cloud_app_template.category, not by '
             'a fourth enum value.',
    )

    # ------------------------------------------------------------------
    # #330 .52 migration 寫值表（升級 DB force-write；與
    # data/subscription_products.xml、data/subscription_template_links.xml
    # 的新裝 seed 同值——雙軌一致性，改一邊必改另一邊）
    # ------------------------------------------------------------------

    # D1 reprice＋D2 rename＋D3 quota：16 筆沿用 plan（xmlid 定位——
    # 撞價實例 R8：SA Entry Yearly 舊 1000＝5App 新月價，嚴禁價格/名稱 match）
    _PRICING_330_PLAN_WRITES = {
        'plan_odoo_monthly': {
            'name': 'Odoo Cloud T1（1–3 人）Monthly', 'sh_plan_price': 1000},
        'plan_odoo_yearly': {
            'name': 'Odoo Cloud T1（1–3 人）Yearly', 'sh_plan_price': 10000},
        'plan_n8n_monthly': {
            'name': 'n8n Cloud T1（1–3 人）Monthly', 'sh_plan_price': 1000},
        'plan_n8n_yearly': {
            'name': 'n8n Cloud T1（1–3 人）Yearly', 'sh_plan_price': 10000},
        'plan_smarthome_monthly': {
            'name': 'Smart Home Connect Monthly', 'sh_plan_price': 150},
        'plan_smarthome_yearly': {
            'name': 'Smart Home Connect Yearly', 'sh_plan_price': 1500},
        'plan_security_access_entry_monthly': {
            'name': 'Security Access 5 Application Monthly',
            'sh_plan_price': 1000, 'sa_max_routes': 5},
        'plan_security_access_entry_yearly': {
            'name': 'Security Access 5 Application Yearly',
            'sh_plan_price': 10000, 'sa_max_routes': 5},
        'plan_security_access_pro_monthly': {
            'name': 'Security Access 10 Application Monthly',
            'sh_plan_price': 2000},
        'plan_security_access_pro_yearly': {
            'name': 'Security Access 10 Application Yearly',
            'sh_plan_price': 20000},
        'plan_mcp_odoo_monthly': {
            'name': 'Odoo MCP Server Monthly', 'sh_plan_price': 3000},
        'plan_mcp_odoo_yearly': {
            'name': 'Odoo MCP Server Yearly', 'sh_plan_price': 30000},
        'plan_mcp_n8n_monthly': {
            'name': 'n8n MCP Server Monthly', 'sh_plan_price': 3000},
        'plan_mcp_n8n_yearly': {
            'name': 'n8n MCP Server Yearly', 'sh_plan_price': 30000},
        'plan_mcp_hermes_monthly': {
            'name': 'Hermes MCP Server T1（1–3 人）Monthly',
            'sh_plan_price': 1500},
        'plan_mcp_hermes_yearly': {
            'name': 'Hermes MCP Server T1（1–3 人）Yearly',
            'sh_plan_price': 15000},
    }

    # D4 backfill：既有 20 筆（含 4 筆 placeholder；anythingllm 為 orphan、
    # guard 放行）。新增 14 筆由 seed XML 自帶，免 backfill。
    _PRICING_330_SERVICE_TYPE = {
        'plan_odoo_monthly': 'cloud_service',
        'plan_odoo_yearly': 'cloud_service',
        'plan_n8n_monthly': 'cloud_service',
        'plan_n8n_yearly': 'cloud_service',
        'plan_postgresql_monthly': 'cloud_service',
        'plan_postgresql_yearly': 'cloud_service',
        'plan_anythingllm_monthly': 'cloud_service',
        'plan_anythingllm_yearly': 'cloud_service',
        'plan_mcp_odoo_monthly': 'cloud_service',
        'plan_mcp_odoo_yearly': 'cloud_service',
        'plan_mcp_n8n_monthly': 'cloud_service',
        'plan_mcp_n8n_yearly': 'cloud_service',
        'plan_mcp_hermes_monthly': 'cloud_service',
        'plan_mcp_hermes_yearly': 'cloud_service',
        'plan_smarthome_monthly': 'smart_home',
        'plan_smarthome_yearly': 'smart_home',
        'plan_security_access_entry_monthly': 'security_access',
        'plan_security_access_entry_yearly': 'security_access',
        'plan_security_access_pro_monthly': 'security_access',
        'plan_security_access_pro_yearly': 'security_access',
    }

    # E2 M2M：四個白名單有變動的 template → (6,0) 全量終態。
    # subscription_template_links.xml（noupdate=0）供新裝與非 sticky DB；
    # 本表覆蓋 sticky noupdate 被擋的升級 DB（migration force-write）。
    _PRICING_330_TEMPLATE_WIRING = {
        'cloud_app_odoo': (
            'plan_odoo_monthly', 'plan_odoo_yearly',
            'plan_odoo_t2_monthly', 'plan_odoo_t2_yearly',
            'plan_odoo_t3_monthly', 'plan_odoo_t3_yearly'),
        'cloud_app_n8n': (
            'plan_n8n_monthly', 'plan_n8n_yearly',
            'plan_n8n_t2_monthly', 'plan_n8n_t2_yearly',
            'plan_n8n_t3_monthly', 'plan_n8n_t3_yearly'),
        'cloud_app_mcp_hermes': (
            'plan_mcp_hermes_monthly', 'plan_mcp_hermes_yearly',
            'plan_mcp_hermes_t2_monthly', 'plan_mcp_hermes_t2_yearly',
            'plan_mcp_hermes_t3_monthly', 'plan_mcp_hermes_t3_yearly'),
        'smart_home_home_assistant': (
            'plan_smarthome_monthly', 'plan_smarthome_yearly',
            'plan_smarthome_mcp_monthly', 'plan_smarthome_mcp_yearly'),
    }

    @api.model
    def _woow_migrate_pricing_330(self):
        """#330 .52 post-migrate 主體（delegate 到 model method 供單元測試，
        比照 .42/.43 慣例）。冪等；一律 xmlid 定位；缺 xmlid log-skip 不
        abort（升級 DB 個別缺 seed 不應中斷整支 migration）。

        grandfathering 已拍板不需要（prod 無人用、stg 純測試）→ 直接
        force-write、無政策碼。

        E2 的 (6,0) 全量覆寫是刻意行為：noupdate=0 的
        subscription_template_links.xml 本來就在每次 -u 對非 sticky DB 做
        同樣的 (6,0) 重置；migration 若改「僅空時回填」（比照
        _ensure_default_template_links）會讓 sticky 與非 sticky DB 收斂到
        不同白名單。本次是定價重定的一次性 canonical reset，非常駐 heal。
        """

        def ref(suffix):
            record = self.env.ref(
                'woow_paas_platform.%s' % suffix, raise_if_not_found=False)
            if not record:
                _logger.warning(
                    'pricing-330 migration: xmlid %s 不存在，跳過', suffix)
            return record

        # D1 reprice＋D2 rename＋D3 quota（plan＋product 同步）
        for suffix, vals in self._PRICING_330_PLAN_WRITES.items():
            plan = ref(suffix)
            if plan:
                plan.write(vals)
            product = ref(suffix.replace('plan_', 'product_', 1))
            if product:
                product.write({
                    'list_price': vals['sh_plan_price'],
                    'name': vals['name'] + ' Subscription',
                })

        # D4 service_type backfill（既有 20 筆；force-write）
        for suffix, service_type in self._PRICING_330_SERVICE_TYPE.items():
            plan = ref(suffix)
            if plan:
                plan.service_type = service_type

        # E2 template M2M force-write（(6,0) 全量；缺任一 plan 則跳過該
        # template 留給 log，避免寫出半套白名單）
        for tpl_suffix, plan_suffixes in \
                self._PRICING_330_TEMPLATE_WIRING.items():
            template = ref(tpl_suffix)
            if not template:
                continue
            plans = [ref(s) for s in plan_suffixes]
            if not all(plans):
                continue
            template.write({
                'subscription_plan_ids': [(6, 0, [p.id for p in plans])],
            })

    def _owning_template_families(self):
        """回這支 plan 被掛在哪些 template family（service_type 值的集合）。

        空集合＝**orphan plan**：有 service_type、但不掛任何 template
        （如 AnythingLLM placeholder）。這個狀態是合法的，所以下面的
        constraint 放行它——但呼叫端若要求「plan 必須真的在某個 family 的
        白名單裡」，就得自己再問一次（#750 的 change-plan 守衛即是）。

        sudo：M2M 走訪落在三個 catalog template model 上，租戶身分是 portal
        （signup template user 只有 base.group_portal，見 tests/test_acl_p9.py），
        而 ir.model.access.csv 只給 security_access_template 兩列
        （base.group_user 唯讀、base.group_system 全權）⇒ portal 一列都沒有，
        未 sudo 的 search 會在 catalog model 上丟 AccessError，而不是求出答案。
        呼叫端負責先擋好「誰有資格問這個問題」。
        """
        self.ensure_one()
        return {
            st for model_name, st in self._TEMPLATE_FAMILY.items()
            if self.env[model_name].sudo().search_count(
                [('subscription_plan_ids', 'in', self.id)])
        }

    @api.constrains('service_type')
    def _check_service_type_matches_template_family(self):
        """條件式 guard（縱深防禦）：存下的 service_type 必須與 owning
        template family 一致；orphan plan（無任何 template 綁定，如
        AnythingLLM placeholder）放行，否則 backfill 會炸。

        主保證仍是三 create-flow 各掛自家 template＋既有三條互斥
        constraint；本 guard 抓 backfill 寫錯值、開發者手改、未來 seed
        掛錯 family。
        """
        for plan in self:
            families = plan._owning_template_families()
            if not families:
                continue  # orphan plan → 放行
            if plan.service_type and plan.service_type not in families:
                raise ValidationError(
                    _("Plan '%s' 的 service_type=%s 與其 owning template "
                      "family %s 不一致。plan 分類必須跟隨掛載它的 template "
                      "家族（cloud_app→cloud_service／smart_home→smart_home／"
                      "security_access→security_access）。")
                    % (plan.name, plan.service_type, sorted(families))
                )
