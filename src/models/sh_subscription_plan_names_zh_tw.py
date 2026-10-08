"""方案名稱中文化（10-08 使用者核可）：英文方案名與對應商品名改中文。

`sh.subscription.plan.name` 不是可翻譯欄位、`data/subscription_products.xml` 又是
noupdate=1 ⇒ 既有 DB 只能靠 migration 改（18.0.1.0.214）。新裝直接吃 seed 的新值。

規則（與 #330 .52 migration 同一套 R8 鐵律）：
- **xmlid 定位**，不以名稱或價格 match 找 record。
- **只改仍是舊英文原名的**：營運在後台手動改過名的不覆蓋。
- 商品名（product.template.name，可翻譯 jsonb）：en_US 一律改；其他已啟用語系的值
  若仍等於舊名也一併改（避免某語系殘留英文）。
- 冪等：第二次跑時名稱已是新值，什麼都不做。
- 只改名稱，不動價格、方案綁定與既有訂閱；已開出的帳單行是當時的文字快照，不會變。

商品名沿用全平台慣例「方案名＋' Subscription'」（例：Odoo 訂閱服務（1-3 人） Subscription），
HA PaaS 免費版例外（原本就沒有後綴）。
"""
import logging

from odoo import models

_logger = logging.getLogger(__name__)

MOD = 'woow_paas_platform'


class ShSubscriptionPlanNamesZhTw(models.Model):
    _inherit = 'sh.subscription.plan'

    # xmlid 尾綴（去掉 plan_／product_）→ (方案舊名, 方案新名, 商品舊名, 商品新名)
    _PLAN_NAMES_ZH_TW = {
        'postgresql_monthly': ('PostgreSQL Monthly', 'PostgreSQL 月繳',
            'PostgreSQL Monthly Subscription', 'PostgreSQL 月繳 Subscription'),
        'postgresql_yearly': ('PostgreSQL Yearly', 'PostgreSQL 年繳',
            'PostgreSQL Yearly Subscription', 'PostgreSQL 年繳 Subscription'),
        'anythingllm_monthly': ('AnythingLLM Monthly', 'AnythingLLM 月繳',
            'AnythingLLM Monthly Subscription', 'AnythingLLM 月繳 Subscription'),
        'anythingllm_yearly': ('AnythingLLM Yearly', 'AnythingLLM 年繳',
            'AnythingLLM Yearly Subscription', 'AnythingLLM 年繳 Subscription'),
        'smarthome_monthly': ('Smart Home Connect Monthly', '智慧家庭連線 月繳',
            'Smart Home Connect Monthly Subscription', '智慧家庭連線 月繳 Subscription'),
        'smarthome_yearly': ('Smart Home Connect Yearly', '智慧家庭連線 年繳',
            'Smart Home Connect Yearly Subscription', '智慧家庭連線 年繳 Subscription'),
        'smarthome_mcp_monthly': ('Smart Home + MCP Monthly', '智慧家庭＋MCP 月繳',
            'Smart Home + MCP Monthly Subscription', '智慧家庭＋MCP 月繳 Subscription'),
        'smarthome_mcp_yearly': ('Smart Home + MCP Yearly', '智慧家庭＋MCP 年繳',
            'Smart Home + MCP Yearly Subscription', '智慧家庭＋MCP 年繳 Subscription'),
        'ha_paas_monthly': ('HA PaaS Free', 'HA PaaS 免費版',
            'HA PaaS Free', 'HA PaaS 免費版'),
        'mcp_hermes_monthly': ('Hermes MCP Server T1（1–3 人）Monthly', 'Hermes MCP 伺服器 T1（1–3 人）月繳',
            'Hermes MCP Server T1（1–3 人）Monthly Subscription', 'Hermes MCP 伺服器 T1（1–3 人）月繳 Subscription'),
        'mcp_hermes_yearly': ('Hermes MCP Server T1（1–3 人）Yearly', 'Hermes MCP 伺服器 T1（1–3 人）年繳',
            'Hermes MCP Server T1（1–3 人）Yearly Subscription', 'Hermes MCP 伺服器 T1（1–3 人）年繳 Subscription'),
        'mcp_hermes_t2_monthly': ('Hermes MCP Server T2（3–6 人）Monthly', 'Hermes MCP 伺服器 T2（3–6 人）月繳',
            'Hermes MCP Server T2（3–6 人）Monthly Subscription', 'Hermes MCP 伺服器 T2（3–6 人）月繳 Subscription'),
        'mcp_hermes_t2_yearly': ('Hermes MCP Server T2（3–6 人）Yearly', 'Hermes MCP 伺服器 T2（3–6 人）年繳',
            'Hermes MCP Server T2（3–6 人）Yearly Subscription', 'Hermes MCP 伺服器 T2（3–6 人）年繳 Subscription'),
        'mcp_hermes_t3_monthly': ('Hermes MCP Server T3（6–10 人）Monthly', 'Hermes MCP 伺服器 T3（6–10 人）月繳',
            'Hermes MCP Server T3（6–10 人）Monthly Subscription', 'Hermes MCP 伺服器 T3（6–10 人）月繳 Subscription'),
        'mcp_hermes_t3_yearly': ('Hermes MCP Server T3（6–10 人）Yearly', 'Hermes MCP 伺服器 T3（6–10 人）年繳',
            'Hermes MCP Server T3（6–10 人）Yearly Subscription', 'Hermes MCP 伺服器 T3（6–10 人）年繳 Subscription'),
        'mcp_odoo_monthly': ('Odoo MCP Server Monthly', 'Odoo MCP 伺服器 月繳',
            'Odoo MCP Server Monthly Subscription', 'Odoo MCP 伺服器 月繳 Subscription'),
        'mcp_odoo_yearly': ('Odoo MCP Server Yearly', 'Odoo MCP 伺服器 年繳',
            'Odoo MCP Server Yearly Subscription', 'Odoo MCP 伺服器 年繳 Subscription'),
        'mcp_n8n_monthly': ('n8n MCP Server Monthly', 'n8n MCP 伺服器 月繳',
            'n8n MCP Server Monthly Subscription', 'n8n MCP 伺服器 月繳 Subscription'),
        'mcp_n8n_yearly': ('n8n MCP Server Yearly', 'n8n MCP 伺服器 年繳',
            'n8n MCP Server Yearly Subscription', 'n8n MCP 伺服器 年繳 Subscription'),
        'mcp_odoo_manage_monthly': ('Odoo Manage MCP Server Monthly', 'Odoo Manage MCP 伺服器 月繳',
            'Odoo Manage MCP Server Monthly Subscription', 'Odoo Manage MCP 伺服器 月繳 Subscription'),
        'mcp_odoo_manage_yearly': ('Odoo Manage MCP Server Yearly', 'Odoo Manage MCP 伺服器 年繳',
            'Odoo Manage MCP Server Yearly Subscription', 'Odoo Manage MCP 伺服器 年繳 Subscription'),
        'mcp_k8s_monthly': ('Kubernetes MCP Server Monthly', 'Kubernetes MCP 伺服器 月繳',
            'Kubernetes MCP Server Monthly Subscription', 'Kubernetes MCP 伺服器 月繳 Subscription'),
        'mcp_k8s_yearly': ('Kubernetes MCP Server Yearly', 'Kubernetes MCP 伺服器 年繳',
            'Kubernetes MCP Server Yearly Subscription', 'Kubernetes MCP 伺服器 年繳 Subscription'),
        'mcp_vk_monthly': ('Vibe Kanban MCP Server Monthly', 'Vibe Kanban MCP 伺服器 月繳',
            'Vibe Kanban MCP Server Monthly Subscription', 'Vibe Kanban MCP 伺服器 月繳 Subscription'),
        'mcp_vk_yearly': ('Vibe Kanban MCP Server Yearly', 'Vibe Kanban MCP 伺服器 年繳',
            'Vibe Kanban MCP Server Yearly Subscription', 'Vibe Kanban MCP 伺服器 年繳 Subscription'),
        'mcp_opendesign_monthly': ('Open Design MCP Server Monthly', 'Open Design MCP 伺服器 月繳',
            'Open Design MCP Server Monthly Subscription', 'Open Design MCP 伺服器 月繳 Subscription'),
        'mcp_opendesign_yearly': ('Open Design MCP Server Yearly', 'Open Design MCP 伺服器 年繳',
            'Open Design MCP Server Yearly Subscription', 'Open Design MCP 伺服器 年繳 Subscription'),
    }

    def _woow_migrate_plan_names_zh_tw(self):
        """回傳 (改了幾筆方案, 改了幾筆商品)。"""
        langs = [code for code, _name in self.env['res.lang'].get_installed()]
        plans = products = 0
        for sfx, (old, new, pold, pnew) in self._PLAN_NAMES_ZH_TW.items():
            plan = self.env.ref('%s.plan_%s' % (MOD, sfx), raise_if_not_found=False)
            if plan and plan.name == old:
                plan.sudo().write({'name': new})
                plans += 1
            product = self.env.ref('%s.product_%s' % (MOD, sfx), raise_if_not_found=False)
            if not product:
                continue
            tmpl = product.product_tmpl_id.sudo()
            updates = {}
            for lang in langs:
                if tmpl.with_context(lang=lang).name == pold:
                    updates[lang] = pnew
            if 'en_US' not in updates and tmpl.with_context(lang='en_US').name == pold:
                updates['en_US'] = pnew
            if updates:
                tmpl.update_field_translations('name', updates)
                products += 1
        _logger.info('[plan-names-zh-tw] renamed %s plans, %s products', plans, products)
        return plans, products
