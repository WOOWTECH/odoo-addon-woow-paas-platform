"""2026-10-14 政策 A：PaaS 標價含 5% 營業稅（取代 #366「點數訂閱免稅」）。

正式站唯讀盤點（2026-10-14）：唯一的銷項稅是 l10n_tw ``account.1_tw_tax_sale_5``（5% 外加，也是
公司預設）；339 個 seed 訂閱商品中 319 個掛它、20 個無稅；70 筆進行中訂閱有 48 筆稅快照帶它
⇒ 300 點方案實扣 315 點，與目錄價不符。

本 migration（post，於 -u data 載入之後執行；``data/paas_included_vat_sync.xml`` 在同一次 -u
已先收斂過商品，這裡再呼叫一次是冪等的保險）：

1. 建立／取得 PaaS 專用 5% 內含稅（由公司 5% 銷項稅複製，只改含稅旗標與名稱；公司預設稅與
   l10n 紀錄不動）。
2. seed 訂閱商品＋``WALLET_TOPUP`` 的該公司稅 → 只剩內含稅（含原本無稅的 20 個）。
3. 非終態（draft／in_progress）PaaS 訂閱的 ``sh_taxes_ids`` → 內含稅；已結束的訂閱、已過帳
   發票、付款與錢包紀錄一律不動。

公司沒有 5% 銷項稅可當來源時三步都跳過並記 warning（不憑空建稅）。邏輯放 model
（``models/paas_included_vat.py``，delegate-to-model 慣例），單元測試直驅那些 method。

⚠ 目錄版號必須**大於**升級前的 manifest 版號（.236＝release #1724），否則靜默 no-op；若
develop 先有別的 migration／release 用掉 .237，要一起改名。
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    templates = env['product.template']._woow_sync_paas_included_vat()
    stats = env['sh.subscription.subscription']._woow_convert_paas_subscriptions_to_included_vat()
    _logger.info(
        "[PAAS_VAT] migration 18.0.1.0.237 done: product templates changed=%s subscriptions=%s",
        len(templates), stats)
