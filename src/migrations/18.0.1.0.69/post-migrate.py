# -*- coding: utf-8 -*-
"""18.0.1.0.69 — #366：清訂閱方案 product 誤掛的 15% 預設稅（升級軌 force-write）。

既有 DB（stg/prod）的 Odoo company 從未正式設定（殘留 My Company/US/15% 預設
銷項稅），不同時期建的訂閱 product 因此稅待遇不一（237/251 掛稅、卡片價≠實扣價）。
pts 交易不應課稅（稅在儲值端處理）。

subscription_products*.xml 的 product record 是 noupdate=1——sticky flag 讓 -u
對既有 record 的更新被靜默 skip（見 docs/tracking/pitfalls/
seed-noupdate-sticky-template-links.md），故既有 product 的稅務清除必走本
migration force-write（新裝走 seed 明寫空 taxes_id，雙軌一致）。

寫值邏輯集中在 product.template._woow_clear_subscription_product_taxes_366
（供單元測試直驅，循 .52/.54/.59 delegate 慣例）。冪等；post-migrate 於 -u data
載入之後執行。公司 account_sale_tax_id 刻意不動（公司層級、會波及官網 ERP）。
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['product.template']._woow_clear_subscription_product_taxes_366()
