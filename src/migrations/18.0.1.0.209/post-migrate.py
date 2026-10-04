"""#1431：回填 cloud_service.first_running_at（服務第一次進 running 的時間）。

新欄位上線前沒有可靠的「曾經 running 過」紀錄，回填一律保守：只有明確還在開通中
（pending／deploying／initializing）、而且沒有任何曾經跑過跡象的服務留空，其餘全部視為
「曾經 running」——寧可照舊扣款、照舊保留 90 天，也不把用過的服務當成沒用過。規則與理由
見 `CloudService._woow_migrate_first_running_at_1431`。

⚠ 目錄版號必須**大於**升級前的 `__manifest__.py` 版號，否則整支靜默 no-op。本檔配
manifest 由 `18.0.1.0.208` bump 到 `18.0.1.0.209`（`scripts/ci/manifest-version-gate.py`
規則 B）；若 develop 先有別的 migration／release 用掉這個號，要一起改名。

邏輯放 model（delegate-to-model 慣例），本檔只負責呼叫，單元測試直接驗那個 method。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.cloud_service']._woow_migrate_first_running_at_1431()
