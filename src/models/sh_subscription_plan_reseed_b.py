# -*- coding: utf-8 -*-
"""Wave B base reprice migration delegate（Refs #330；正式 214 manifest 對齊）。

雙軌一致性：data/subscription_products.xml 是 noupdate=1——新裝 DB 吃新 seed、
已裝 DB（stg/prod）的既有 record 不會被 -u 更新，必須由本 delegate force-write。
兩軌同值，寫值表單一真相在本檔 _REPRICE（供單元測試直驅，循 .52/.54 慣例）。
"""
from odoo import models

_REPRICE = {
    # xmlid: dict(price=, name=None 或新名, quota=None 或 dict)
    'plan_odoo_monthly': {'price': 1500, 'name': 'Odoo 訂閱服務（1-3 人）'},
    'plan_odoo_yearly': {'price': 12000, 'name': 'Odoo 訂閱服務（1-3 人）'},
    'plan_odoo_t2_monthly': {'price': 4500, 'name': 'Odoo 訂閱服務（4-7 人）'},
    'plan_odoo_t2_yearly': {'price': 36000, 'name': 'Odoo 訂閱服務（4-7 人）'},
    'plan_odoo_t3_monthly': {'price': 7500, 'name': 'Odoo 訂閱服務（8-11 人）'},
    'plan_odoo_t3_yearly': {'price': 60000, 'name': 'Odoo 訂閱服務（8-11 人）'},
    'plan_n8n_monthly': {'price': 750, 'name': 'n8n 雲端訂閱服務（1 人）'},
    'plan_n8n_yearly': {'price': 6000, 'name': 'n8n 雲端訂閱服務（1 人）'},
    'plan_n8n_t2_monthly': {'price': 2250, 'name': 'n8n 雲端訂閱服務（3 人）'},
    'plan_n8n_t2_yearly': {'price': 18000, 'name': 'n8n 雲端訂閱服務（3 人）'},
    'plan_n8n_t3_monthly': {'price': 4500, 'name': 'n8n 雲端訂閱服務（6 人）'},
    'plan_n8n_t3_yearly': {'price': 36000, 'name': 'n8n 雲端訂閱服務（6 人）'},
    'plan_smarthome_yearly': {'price': 1200},
    'plan_smarthome_mcp_monthly': {'price': 300},
    'plan_smarthome_mcp_yearly': {'price': 2400},
    'plan_security_access_entry_monthly': {
        'price': 1200, 'name': '遠端安全連結訂閱服務（6 組）',
        'quota': {'sa_max_routes': 6, 'sa_max_access_policies': 6}},
    'plan_security_access_entry_yearly': {
        'price': 10800, 'name': '遠端安全連結訂閱服務（6 組）',
        'quota': {'sa_max_routes': 6, 'sa_max_access_policies': 6}},
    'plan_security_access_pro_monthly': {
        'price': 2000, 'name': '遠端安全連結訂閱服務（10 組）'},
    'plan_security_access_pro_yearly': {
        'price': 18000, 'name': '遠端安全連結訂閱服務（10 組）'},
    'plan_mcp_odoo_yearly': {'price': 24000},
    'plan_mcp_n8n_yearly': {'price': 24000},
}


class ShSubscriptionPlanReseedB(models.Model):
    _inherit = 'sh.subscription.plan'

    def _woow_migrate_reseed_b_330(self):
        """升級軌 force-write（xmlid 定位、冪等；noupdate XML 不回填既有 record）。"""
        for xmlid, spec in _REPRICE.items():
            plan = self.env.ref(f'woow_paas_platform.{xmlid}',
                                raise_if_not_found=False)
            if not plan:
                continue
            vals = {'sh_plan_price': spec['price']}
            if spec.get('name'):
                vals['name'] = spec['name']
            vals.update(spec.get('quota') or {})
            plan.write(vals)
            product = self.env.ref(
                f'woow_paas_platform.{xmlid.replace("plan_", "product_")}',
                raise_if_not_found=False)
            if product:
                product_vals = {'list_price': spec['price']}
                if spec.get('name'):
                    product_vals['name'] = spec['name'] + ' Subscription'
                product.write(product_vals)
