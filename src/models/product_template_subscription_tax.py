# -*- coding: utf-8 -*-
"""#366：訂閱方案 product 一律無稅（pts 交易免稅，稅在儲值端處理）。

Layer A 升級軌 delegate：清 PaaS seed 訂閱 product 的 taxes_id（空集合，
非 0% 稅——後者發票仍出一列 0 稅）。subscription_products*.xml 是
noupdate=1，-u 不回填既有 record（sticky flag，見
docs/tracking/pitfalls/seed-noupdate-sticky-template-links.md），故既有 DB
（stg/prod）的稅務清除必走本 delegate force-write，由 migration
18.0.1.0.69/post-migrate.py 呼叫（比照 .52/.54/.59 reseed delegate 慣例，
寫值邏輯集中於 model method 供單元測試直驅）。

scope（Layer C）：xmlid 錨定（ir.model.data，module=woow_paas_platform、
model=product.product）＝嚴格限 PaaS seed 出的訂閱 product，不碰官網 ERP
product、更不動公司 account_sale_tax_id（那是公司層級、會波及整個 Odoo
company 含官網 ERP）。冪等（已空重跑為 no-op）。
"""
from odoo import models


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def _woow_clear_subscription_product_taxes_366(self):
        """清 PaaS 訂閱 product 的 taxes_id 為空集合，回傳被清的 template。

        一律 xmlid 定位（R8 鐵律；不以價格/名稱 match）。fresh DB 未載 seed
        時 ir.model.data 查無 → 回空 recordset、不炸。
        """
        imd = self.env['ir.model.data'].sudo().search([
            ('module', '=', 'woow_paas_platform'),
            ('model', '=', 'product.product'),
        ])
        products = self.env['product.product'].sudo().browse(
            [row.res_id for row in imd]).exists()
        templates = products.filtered('sh_product_subscribe').product_tmpl_id
        if templates:
            templates.write({'taxes_id': [(6, 0, [])]})
        return templates
