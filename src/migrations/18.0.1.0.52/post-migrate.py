"""18.0.1.0.52 — #330 定價 seed 重定（30 檔）＋ plan taxonomy backfill。

雙軌一致性：data/subscription_products.xml 是 noupdate=1——新裝 DB 吃新
seed、已裝 DB（stg/prod）的既有 record 不會被 -u 更新，必須由本 migration
force-write。兩軌同值，寫值表單一真相在
models/sh_subscription_plan_service_type.py（供單元測試直驅，比照 .42/.43
delegate 慣例）：

  D1 reprice 16 plan sh_plan_price＋16 product list_price（32 筆）
  D2 rename 12 plan＋對應 product（＋4 筆 MCP 同名重寫、無視覺差異）
  D3 SA 5 Application 兩檔 sa_max_routes 3→5
  D4 service_type backfill 既有 20 筆（cloud_service 14／smart_home 2／
     security_access 4；anythingllm orphan 由 guard 放行）
  E2 template M2M (6,0) 全量 force-write（odoo/n8n/hermes↔6、SH↔4；
     covers sticky-noupdate 擋掉 subscription_template_links.xml 的 DB）

一律 env.ref() xmlid 定位（撞價實例：SA Entry Yearly 舊 1000＝5App 新月價，
嚴禁價格/名稱 match）。grandfathering 已拍板不需要→直接 force-write。
post-migrate 於 -u data 載入之後執行，新 plan xmlid 已存在。冪等。
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sh.subscription.plan']._woow_migrate_pricing_330()
