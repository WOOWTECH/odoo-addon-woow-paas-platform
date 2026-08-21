# project meeting 20260710 issue landing

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_meeting_20260710_issue_landing`。盤點依據見 [memory-inventory-2026-08-05](../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description："2026-07-10 定價/上線對齊會（Elmo）衍生待辦 T1–T7 全數 issue 化（#335–#338、woow-mcp-server#12、GitHub manage#1）＋#273/#274 收斂關閉＋會議記錄補〈會後調查結論〉"

**2026-07-11 leader3 收口**。會議記錄=`0-0.footage/meeting/2026-07-10 paas 定價 plan和上線進度對齊 會議記錄.md`（已修 3 處誤導＋文末補〈會後調查結論〉：**不用 Product Combo 當訂閱載體**〔計費鏈讀取次數=0＋sale 路零元陷阱＋1:1 無 unique〕、**購買一律 /woow**、60+ 走普通 service product＋plan 雙軌 seed 放大）；共享追蹤=canvas note `issue-for-prod` 的 🆕 節。

**T→issue 對照（拍板全記錄）**：
- **T1** plan×商品建檔＝glue 拍板 issue **#335**（a 一訂閱+佈建器／b 純計費載體／c 第一階段只賣同家族——**待 Eugene 拍**；多服務×一訂閱撞 #318/#324 的 1:1 結算）；**re-seed epic 等 Elmo 60+ 列表到手再開**（避免空殼）
- **T2** 標準版 MCP patch＝**woow-mcp-server#12**（git-prod monorepo）。關鍵事實：Elmo 兩 patch **會議前已在 GitHub 上游 `WOOWTECH/woow_odoo_mcp_server` 預設分支**（5ad423d2 開關/5e8124d6 token/⚠️380a1783 **換 tuanle96 odoo-mcp 套件**→stg 驗證不可省）；monorepo `apps/odoo`＝vendored copy 非 submodule；殘決策=Eugene open PR phase0-stabilization 納不納＋三階段開關**平台租戶預設值**
- **T3** Manage MCP 上架＝**#338**（平台主，blocked-by #12 image；Manage 版=GitHub `woow_odoo_manage_mcp_server`，ivnvxd/mcp-server-odoo bundle、API Key 全權限）
- **T4** HA PaaS 0 元隱藏訂閱＝**#336 → PR #343 ✅ merged（2026-07-13、develop=d196e51、.57；與 #341 撞車二次收斂後落地——sold 沿用 #341 xmlid、orphan plan、max_per_partner 走 #345、advisory lock＋unique index 留並發 backstop）**：Mason plan（1135 行、5 視角壓測、miniclaude APPROVED）重啟後於 worktree 存活→Eugene 拍板 Q1 seed 自足 client/Q2 by-id 補擋/Q3 filter/**Q4 契約升 v1.1（枚舉 6 值＋409 兩分支）**→Mason2 實作 3 commits/.54/25 檔+2227；leader 隔離重跑 6 test classes=0 failed of 35；Q5 ECPay 0 元壓測=兩層防護（confirm 只建 draft＋skip_upstream_hook）無需產品決策；工作項 5 佔位（is_active=False＋TBD chart）等 Elmo image；偏離 plan 5 點皆有據（sudo 保留 group/readonly=False/update_env 帶 company/mixin 收 env/oob redirect）
- **T5** OAuth 串接包＝**#337** → **PR #339 ✅ merged（2026-07-12、develop=bd27096）**：2026-07-12 兩波拍板——① App 授權主線＝device flow（RFC 8628；先例＝component Companion App WebView 已用、無 PKCE 靠 device_code＋登入同意）繞開 redirect/custom-scheme 題；② **§7 開通契約 v1（contract-first，Eugene 因「md 沒有合約 Elmo 無法實作」推動）**＝scope 定名 `ha:provision`（App 唯一必要）＋`POST /api/ha-paas/provision`（冪等限一、202/200、error 可重試）＋`GET /api/ha-paas/status`（none/provisioning/ready/error、ha_url 僅 ready）＋輪詢 5s→30s/10min＋App 免知 workspace；**#336 依 §7 實作、變更雙向知會**；前置 **#340**（ops 建 public client〔grants=device_code+refresh〕＋stg 驗證＋redirect_uris required 能否留空＋憑證交付）＝Elmo 端到端平行開發不等平台
- **T6** survey per-tool 隔離＝**GitHub woow_odoo_manage_mcp_server#1**（排 #338 後）
- **T7** 架構圖不開 issue、掛 note

**N1 對位完成**：#331/#332 留言知會定調；**#273/#274 收斂關閉**（被 #332/#331＋PR #333 取代）。N2 prod .53 同日已收官。

**Wave A re-seed 機制件全鏈落地（2026-07-13）**：三 stacked PR 序列 merge 進 develop——**PR #341（.54，#336）→ PR #344（.55，#338 Manage 骨架 is_active=False 仍待 #12 image）→ PR #346（.56，Closes #332）**，`#332 自動關閉`。權威計畫＝`docs/archived/superpowers/plans/2026-07-12-wave-a-reseed-mechanisms.md`（miniclaude APPROVED）。follow-up＝**#342**（change plan 後 CS 規格 re-apply 殘段，backlog）。worktree/測試容器已清、三分支本地+remote 已刪。（inflight memory 已隨收官刪除。）

關聯：[[pricing-gap-analysis-20260709]]（Combo 考古/Fable 驗證/兩叉路全脈絡）
