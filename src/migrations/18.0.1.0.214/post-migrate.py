"""方案名稱中文化（10-08 使用者核可）：27 個英文方案名與對應商品名改中文。

`sh.subscription.plan.name` 不可翻譯、`data/subscription_products.xml` 為 noupdate=1，
既有 DB 只能靠本 migration 改；只改仍是舊英文原名的 record（後台手改過的不覆蓋）。
規則與對照表見 `ShSubscriptionPlanNamesZhTw._woow_migrate_plan_names_zh_tw`。

⚠ 目錄版號必須**大於**升級前的 `__manifest__.py` 版號，否則整支靜默 no-op。本檔配
manifest 由 `18.0.1.0.212` bump 到 `18.0.1.0.214`（跳過 .213：release/18.0.1.0.213
發版 PR #1583 已佔用；`scripts/ci/manifest-version-gate.py` 規則 B 只要求嚴格大於
develop）。若 develop 先有別的 migration／release 用掉這個號，要一起改名。

邏輯放 model（delegate-to-model 慣例），本檔只負責呼叫，單元測試直接驗那個 method。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sh.subscription.plan']._woow_migrate_plan_names_zh_tw()
