# project subscription integration（已封存）

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_subscription_integration`。盤點依據見 [memory-inventory-2026-08-05](../../tracking/memory-inventory-2026-08-05.md)。
>
> 原 description：Cloud Service 與 Smart Home 訂閱整合功能 — 架構決策與實作狀態

Cloud Service 和 Smart Home 各自 1:1 對應一個 sh.subscription.subscription 實體。

**Why:** 使用者需要按實體計費，subscription 控制服務的可用性（到期 → 寬限期 → 暫停 → 清理）。

**How to apply:**
- Branch: `feat/subscription-integration` (8 commits, 46 files, +4125 lines)
- 使用 sh_subscription + sh_subscription_wallet 原生功能，不 fork
- Template ↔ Plan 透過 Many2many (`subscription_plan_ids`) 連結
- Product 與 Plan 是 1:1（`product.template.sh_subscription_plan_id`）
- CloudService 暫停 = Helm scale replicas=0；SmartHome 暫停 = tunnel ingress 改 http_status:503
- 寬限期天數和清理月數可在 Settings → Woow PaaS 設定
- Plan change = close old sub + refund prorated points to wallet + create new sub
- 定價: n8n 199/1990, PostgreSQL 99/990, Odoo 499/4990, AnythingLLM 299/2990, Smart Home 100/1000（月/年）
- 所有 plan 有 7 天免費試用，不要求綁卡
- 狀態: 尚未合併到 develop，需要 dev sandbox 整合測試
