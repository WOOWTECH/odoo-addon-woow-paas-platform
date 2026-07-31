# -*- coding: utf-8 -*-
"""#497：#482 合併前就卡死的停權服務——收斂回 `suspended` 並收回 workload。

#493 從源頭修掉了「停權服務被 redeploy／reconfigure 汙染 state」，但那只對**新**
操作有效。合併前已經被汙染的既有 row 仍在，而 #489／#491 的 age timeout 一上線就會
把其中 `deploying` / `upgrading` 那批逐一打成 `error`。那批的 `pre_suspend_state`
仍在＝它其實還在停權中，於是撞上 lifecycle cron 的分支落差：訂閱已恢復者走分支 2，
而分支 2 要求 `state == 'suspended'` 才復權 → **租戶付了錢、服務永遠回不來**。

本檔把那批收斂回 `suspended`，讓分支 2（本版一併放寬成讀 `_is_halted()`）能接手。
收斂時一併把 workload 收回停機——`_do_suspend` 對 `state == 'suspended'` 會早退，
所以先寫 state 再指望別人收 workload 是行不通的（唯一的 scale 路徑會被關掉）。

⚠ 目錄版號必須 **≤ `__manifest__.py` 版號**且 **> 升級前的版號**，否則整支靜默
no-op（Odoo 只跑 `latest_version < 目錄版號 <= 新版號` 的 migration）。本檔配 manifest
由 `18.0.1.0.86` bump 到 `18.0.1.0.87`；兩者必須同 PR 一起改。

實作循 repo 的 delegate-to-model 慣例（`.79` / `.84` 現例）：邏輯放 model，migration
只負責呼叫，這樣它能被單元測試直接驗（見 `TestConvergeStuckSuspended497`）。
"""
from odoo import api, SUPERUSER_ID


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    env['woow_paas_platform.cloud_service']._woow_migrate_converge_stuck_suspended_497()
