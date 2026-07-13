"""18.0.1.0.59 — Wave B base reprice（正式 214 manifest 對齊；含 SA 四級 B7）。

雙軌一致性：data/subscription_products.xml 是 noupdate=1——新裝 DB 吃新 seed、
已裝 DB（stg/prod）的既有 record 不會被 -u 更新，必須由本 migration force-write。
兩軌同值，寫值表單一真相在 models/sh_subscription_plan_reseed_b.py（供單元測試
直驅，循 .52/.54 delegate 慣例）：

  base reprice 21 plan sh_plan_price＋21 product list_price
  改名 12 plan（odoo/n8n 各 6）＋ SA entry/pro（＋對應 product 同名）
  SA entry sa_max_routes 5→6、sa_max_access_policies 3→6

一律 env.ref() xmlid 定位（R8 鐵律；撞價實例多，嚴禁價格/名稱 match）。
post-migrate 於 -u data 載入之後執行。冪等（fresh XML 值＝migration 目標值，
重跑不變）。新增 6 base plan（n8n t4／SA r1·r3）與 208 變體屬純新增 record，
noupdate seed 自然帶入，不需 migration force-write。
"""

from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sh.subscription.plan']._woow_migrate_reseed_b_330()
