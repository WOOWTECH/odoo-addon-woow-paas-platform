"""#1637（使用者 2026-10-10「依性質跟使用需求按需分配，最大給到 30G」＋同日核可的容量研究）：方案規格。

`data/subscription_products.xml` 與 generator 產的 `subscription_products_variants.xml` 都是
noupdate=1，既有 DB 的方案不會隨 -u 改值，只能靠本 migration：
- Odoo（含 +N 存取席位變體）：T1 記憶體 2 → 3 GB、workers 2 → 3；T2 磁碟 30 → 25（整台總量）、
  workers 3 → 4；T3 磁碟 40 → 30、workers 4 → 5。
- n8n（含變體）：T1–T4 磁碟 10／20／30／40 → 5／10／15／20；T1 記憶體 1 → 2 GB。
- Hermes MCP 三階（含變體）cs_vcpu／cs_ram_gb／cs_storage_gb 清成 0（規格回到範本值）。
- Code Server 記憶體 4 → 8 GB、磁碟 25 → 20；pi Coding Agent 記憶體 4 → 8 GB、磁碟 20 → 30；
  EMQX 磁碟 10 → 3（整台總量）；LiteLLM 記憶體 2 → 4 GB、磁碟 10 → 5；Omnigent 磁碟 10 → 5。
只改仍是舊 seed 值的方案（後台手改過的不覆蓋）；價格不變。範本（cloud_app_templates.xml）在
noupdate=0 塊，-u 會直接重套 PVC 大小、min_storage_gb 與說明，不需要這裡處理。既有服務的磁碟
只擴不縮（Redeploy／換方案以開通時的 helm_values 為底），不受影響。

⚠ 目錄版號必須**大於**升級前的 `__manifest__.py` 版號，否則整支靜默 no-op。本檔配 manifest
bump 到 `18.0.1.0.225`（.222＝release #1636、.223＝#1635 Nextcloud、.224＝#1628 omnigent；本 PR
疊在 #1635 上）。若 develop 先有別的 migration／release 用掉這個號，要一起改名。

邏輯放 model（delegate-to-model 慣例），本檔只負責呼叫，單元測試直接驗那個 method。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['sh.subscription.plan']._woow_migrate_spec_1637()
