# project 275 project hide toggle landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_275_project_hide_toggle_landing`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。

#275 /woow Project 功能隱藏開關 **已完成並 merge**（PR #310 → develop，merge commit `073819b`，2026-07-07）。

**是什麼**：cosmetic feature flag `woow_paas_platform.show_projects`（fail-closed，缺值＝隱藏）。flag OFF 時 /woow 隱藏 Projects/AI-assistant nav + router redirect project 路由到 dashboard + Service 詳情頁 project 交叉入口一併 gate。**只前端 cosmetic，後端 API 不動、非安全邊界**（對照 `portal_project_settings_editable` 才是 ACL 寫入守衛，兩者正交勿混）。

**⚠️ 上線約束（非從 code 顯而易見）**：
- **prod**：**不設** param → fail-closed → Projects 隱藏（本階段 Project 不上線，2026-07-01 會議決議）。
- **stg**：需設 `woow_paas_platform.show_projects=True` 才會顯示 Projects（否則 stg 也會隱藏）。可從 /woow Settings→Woow PaaS→Portal「Show Projects」開關，或 `ir.config_parameter` seed。

**Why**：驗證方式（Odoo 測試 6/6 + 對抗 4 維 review + headed E2E）與過程中 headed E2E 抓到的 reader bug（bare `odoo.__session_info__` boot 後被刪）見 [[reference_odoo_session_info_deleted_after_boot]]。設計/計畫：`docs/superpowers/{specs,plans}/2026-07-06-woow-project-hide-toggle-*.md`。

**How to apply**：deploy 到 stg 後若 Projects 沒出現，先查此 config_parameter 是否 True（+ Odoo 重啟/registry cache：跨 process 改 param 需重啟才生效，實測 ormcache 不跨 process 失效）。
