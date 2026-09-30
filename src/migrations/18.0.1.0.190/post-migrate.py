"""#1257：post-deploy init 執行管控上線過渡（Q15＋Q26）。

- 回填「欠 init」：`upgrading`＋`addon_update_pending`、`pending`／`deploying` 且 init
  類型不是 none、`initializing`（Q26）。`error` 的列不動。
- 替每台 `initializing` 寫合成 legacy attempt＋1200s lease，`init_retries` 歸 0
  （Q15）：舊 process 可能還有在途的 remote exec，合成 lease 讓新 code 在它到期前
  不重送；legacy 到期不計入 unknown 額度。
- log 列出 init 類型不是 none 的停權服務，交人工抽查（停權期間被吃掉的 init 無資料
  可推回來）。

⚠ 目錄版號必須**大於**升級前的 `__manifest__.py` 版號，否則整支靜默 no-op。本檔配
manifest 由 `18.0.1.0.189` bump 到 `18.0.1.0.190`（`scripts/ci/manifest-version-gate.py`
規則 B）；若 develop 先有別的 migration／release 用掉這個號，要一起改名。

邏輯放 model（delegate-to-model 慣例），本檔只負責呼叫，單元測試直接驗那個 method
（見 `TestInitMigration1257`）。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.cloud_service']._woow_migrate_init_execution_control_1257()
