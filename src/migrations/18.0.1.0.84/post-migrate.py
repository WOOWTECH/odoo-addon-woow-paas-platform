# -*- coding: utf-8 -*-
"""#489：部署／升級 age timeout 上線——把當下轉移中的服務重新錨定。

本版新增的 age timeout 用 `max(deployed_at, last_upgraded_at)` 當起算點。合併前
有三條轉移入口不記錨（`action_redeploy` 的 upgrade 分支、`_reset_admin_password`、
controller 的 rollback），所以升上來的那一刻，經那些路徑進入 `deploying` /
`upgrading` 的服務，錨會是**首裝**時間 → `-u` 後第一次 cron 就把**正在正常部署**
的服務判成逾時。而誤殺不可自癒：poll 只撈轉移狀態，寫進 `error` 之後沒有任何
路徑會把它 poll 回 `running`。

本檔給那一批一個新鮮的起點，把那個窗口降到零。代價是真正已經卡住的那批會晚
一個門檻週期才被抓到（一次性）。

⚠ 目錄版號必須**大於**升級前的 `__manifest__.py` 版號，否則整支靜默 no-op
（Odoo 只跑 `latest_version < 目錄版號` 的 migration）。本檔配 manifest 由
`18.0.1.0.83` bump 到 `18.0.1.0.84`；兩者必須同 PR 一起改。

實作循 repo 的 delegate-to-model 慣例（`.79` 現例）：邏輯放 model，migration 只
負責呼叫，這樣它能被單元測試直接驗（見 `TestMigrationReAnchor489`）。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.cloud_service']._woow_migrate_refresh_transition_anchor_489()
