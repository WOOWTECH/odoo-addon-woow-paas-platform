"""#1634（使用者 2026-10-10）：Nextcloud 整台磁碟 96 → 20G，兩個方案的 cs_storage_gb 80 → 20。

`data/subscription_products.xml` 為 noupdate=1，既有 DB 的方案不會隨 -u 改值，只能靠本
migration；只改仍是舊 seed 值（80）的方案（後台手改過的不覆蓋）。範本 `cloud_app_nextcloud`
在 noupdate=0 塊，-u 會直接重套 PVC 大小、min_storage_gb 與說明，不需要這裡處理。
價格不變；既有 Nextcloud 服務（正式站 0 台）的磁碟只擴不縮，不受影響。

⚠ 目錄版號必須**大於**升級前的 `__manifest__.py` 版號，否則整支靜默 no-op。本檔配
manifest 由 `18.0.1.0.221` bump 到 `18.0.1.0.223`（跳過 .222：未合併的 PR #1628 已佔用；
`scripts/ci/manifest-version-gate.py` 規則 B 只要求嚴格大於 develop）。若 develop 先有別的
migration／release 用掉這個號，要一起改名。

邏輯放 model（delegate-to-model 慣例），本檔只負責呼叫，單元測試直接驗那個 method。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sh.subscription.plan']._woow_migrate_nextcloud_storage_1634()
