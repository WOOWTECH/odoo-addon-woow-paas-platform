# reference cf zero trust seat semantics

> 自 Claude memory 遷移（2026-08-05）。原 slug `reference_cf_zero_trust_seat_semantics`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：CF Zero Trust seat 語意查證定案（2026-07-10）：per-ACCOUNT 共用池 50、達標=第 51 人認證當下硬擋（服務中斷非帳單風險）、不自動扣款、成本懸崖（升級疑全席計費 51 人=$357/月）、Access/Gateway 席位分計陷阱、service token 不佔 seat

**2026-07-10 兩輪查證定案**（subagent 研究＋Workflow wf_d4bdd77a-de2 五 agents；完整報告=session scratchpad `pricing-scout-report.md`＋`cf-50-behavior.md`）。Eugene dashboard 一手實證：Woowtech 帳號 3/50、11 個 Access 應用共用、team domain=woowtech.cloudflareaccess.com。

**Seat 語意**：per-**ACCOUNT**（=Cloudflare One account=Zero Trust organization），**非 per zone/domain**——全部 zone、全部 Access 應用共用同一池；1 Seat=1 End User，認證登入或裝置註冊時消耗、同一 user 多應用多登入只佔 1；**service token / tunnel token 機器路徑不佔 seat**（官方明文）；移除 user **立即**釋放。易混淆：(a) 網站 zone≠計費單位；(b) team domain（xxx.cloudflareaccess.com）與 account 1:1；(c) **Email Routing 是另一產品**（其位址上限才 per-zone）——repo plan 欄位 `sa_max_email_rules` 即 Email Routing quota，勿與 Access seat 混（命名陷阱）。

**達 50 行為（信心高，官方明文 "blocked" 雙線一致）**：第 51 位 unique user **認證當下被硬擋**＝**服務中斷風險而非帳單風險**；既有 50 人不受影響；**不自動扣款**（中高信心：升級前置綁卡＋主動購買語氣＋社群零扣款控訴；**維持不綁卡=結構性防護**）；即時解法唯二＝手動 revoke（立即釋放）或主動購買。seat expiration=衛生工具非救火（預設關、最短 1 月窗、每日跑一次）。

**成本懸崖（U7 未決，差 50 倍）**：升級 PAYG US$7/user/月疑為**全席計費**——第 51 人邊際成本 $357/月（51×7≈NT$11.4k）非 $7；「僅超額」替代模型待 CF support ticket 確認（**五問英文草稿在 cf-50-behavior.md §②A**）。**升級頭號陷阱**：付費後 **Access 與 Gateway 席位分開計數**（只買其一→「買了仍被擋」，社群假證據 de-confound 出的真教訓）；購後須實測一位新 user 認證驗收；縮席前必先移人。

**平台營運**：告警 40/45/48 三級（96% 時平台側軟擋 onboarding 防「第 51 人硬擋」直接發生在租戶面前）；每日 API seat 監測 cron（Odoo cron 基建）；產品守線=租戶機器流量走 token 路徑、Access seat 留給人；50 席內席位衛生每月價值≈NT$11k+ 遞延支出。

關聯：[[project_pricing_gap_analysis_20260709]]（AC 定價 500/seat 的成本基礎）、issue #274（AC 方案矩陣 B 路線+sa_max_access_emails 草案）。
