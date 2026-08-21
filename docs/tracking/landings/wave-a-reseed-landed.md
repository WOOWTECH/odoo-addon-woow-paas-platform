# project wave a reseed landed

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_wave_a_reseed_landed`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："Wave A re-seed 機制件全鏈收官——三 PR merged（.54/.55/.56）、#332 關；撞車協調共識與 Wave B 銜接點"

**Wave A re-seed 收官（2026-07-13）**：三 PR 序列 merged——**#341**（.54：storefront_hidden＋max_per_partner 限一＋HA PaaS 0 元 seed＋封存 Hermes 雙軌，Refs #336）→ **#344**（.55：MCP Manage 骨架 is_active=False 待 image，Refs #338）→ **#346**（.56：cs_* 規格映射 runtime overlay，**Closes #332 已關**）。develop=.56。leader 三輪獨立容器驗收全綠（29/12/21 tests）＋rebase 後 patch 等價驗證；plan-review APPROVED plan 在 `docs/archived/superpowers/plans/2026-07-12-wave-a-reseed-mechanisms.md`。

**與 leader3 #343 撞車協調共識**：版號隊列 A2=.55/A3=.56/**#343=.57**（版號序=merge 序、manifest 不可倒退）；#343 複用我方 `plan_ha_paas_monthly` xmlid＋`max_per_partner`/`_find_active_for_partner`（advisory lock 留 provision 並發 backstop）；storefront_hidden（plan 層）與 is_hidden（服務實體鏈）互補共存；#338 剩餘範疇（真 image 接線＋設定 UI）歸 leader3 隊對位。

**Follow-up**：~~#345~~ ✅ 已收官（07-14 **PR #353 merged**、develop=.63=b37b513c、issue 隨 Closes 自動關）：三工作項全落地——test_field_exists_default_zero 改直驗不變式（order-independent）＋cloud_app_ha_paas seed 補 max_per_partner=1（noupdate=0 一行、無需 migration）＋create() 補 (template_id,partner_id) pg_advisory_xact_lock 修 check-after-act 並發繞過（INSERT→lock→count 於 READ COMMITTED 正確：輸家取鎖時贏家必已 commit、新 snapshot 見其列；排序去重防 lock-ordering 死鎖；max_per_partner=0 常態零鎖開銷；泛化 N 上限無法用 partial unique index 表達故不複用 #343 做法）。mason345（ccou recruit）交付、雙 leader 驗收（leader2 object 級＋leader3 diff review）、sandbox 實跑 13/13＋15/15 綠。**執行教訓**：teammate auto-compact 後 scope 膨脹（想起 sandbox 做真升級 e2e）——leader 及時裁定攔停（noupdate=0 一行補值升級語意由 #350 先例背書、advisory lock 無 schema 變更，加碼風險>價值）；版號隊列實戰=.62 被 #351 佔、teammate 自行 fetch 查證讓號 .63 正確。#342（change-plan 後 CS 規格 re-apply，Refs #332）：07-14 Sonar 查證定案（change plan 不觸發 redeploy、4 入口只動計費層），方案 A 提案已回填 issue，實作 ≈M 2-3 人日待排期。

**Wave B 銜接**：輸入=正式 214 manifest（`docs/plans/2026-Q3/2026-07-12-cloud-services-214-manifest.md`，機械驗證 100%：6 base＋208 變體、AC=+N×600/+N×4800、×8 全域唯 CF ×9）；B1/B6 消滅；**B7＝SA 四級對映（1/3/6/10 組 200/600/1200/2000×9 vs 現有兩級 5/10 App 1000/2000）待 Eugene 拍**——拍了才寫 Wave B plan。base reprice 目標：Odoo 1500/4500/7500（分級 1-3/4-7/8-11）、n8n 三→四級 750/2250/4500/7500、SH 方案二 400→300、MCP 年 30000→24000、年繳 ×10→×8 全改。

**教訓（TCC 撤銷事故）**：Maestri app 的 macOS「文件」授權被撤銷＝全終端 Documents EPERM、新 recruit claude 起不來；修法=系統設定勾回/重啟 app。應變=plan 寫 scratchpad、reviewer `--add-dir`＋**heredoc stdin**（`--disallowedTools` 是 variadic 會吞 prompt 參數）。

關聯：[[pricing-gap-analysis-20260709]]、[[maestri-recruit-resume-pitfall]]（執行型 recruit 標準 command 首次實戰成功——全新 session、零殭屍、三 PR 一氣呵成）。
