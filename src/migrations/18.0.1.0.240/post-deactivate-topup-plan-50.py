"""下架 NT$ 50 儲值方案（既有資料庫）。

seed 的 `topup_plans.xml` 在 `noupdate="1"` 區塊裡：改 XML 只對全新安裝生效，
既有資料庫不會被更新，所以要在這裡停用。

為什麼下架：ECPay 的信用卡手續費是「費率」與「最低手續費 NT$5」取大者，刷 50 元
時手續費佔 10%。方案改為最低 NT$100（自動補值的下限見
`woow.wallet.topup._auto_topup_min_amount`）。

冪等：只在方案仍為啟用時寫入；使用者若自行重新上架，重跑不會又把它關掉
（判斷依據是本次 migration 自己留下的 `ir.config_parameter` 標記）。
"""
import logging

_logger = logging.getLogger(__name__)

_DONE_PARAM = 'woow_paas_platform.migration_18_0_1_0_240_topup_plan_50_retired'
_XMLID = ('woow_paas_platform', 'topup_plan_50')


def migrate(cr, version):
    if not version:
        return
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s", (_DONE_PARAM,))
    if cr.fetchone():
        _logger.info('[MIG-240] NT$50 topup plan already retired; skipping')
        return
    cr.execute(
        "SELECT res_id FROM ir_model_data WHERE module = %s AND name = %s "
        "AND model = 'woow_paas_platform.topup_plan'", _XMLID)
    row = cr.fetchone()
    if not row:
        _logger.info('[MIG-240] topup_plan_50 not present; nothing to retire')
        return
    cr.execute(
        "UPDATE woow_paas_platform_topup_plan SET active = FALSE "
        "WHERE id = %s AND active IS TRUE", (row[0],))
    changed = cr.rowcount
    cr.execute(
        "INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO NOTHING", (_DONE_PARAM, '1'))
    _logger.info(
        '[MIG-240] NT$50 topup plan retired (rows changed: %s)', changed)
