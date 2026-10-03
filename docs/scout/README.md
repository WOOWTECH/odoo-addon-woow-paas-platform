# scout/ — 決策前偵察

放**還沒成案**的探索產出：架構調整的衝擊分析、decision matrix、可行性調查。這裡的文件回答「如果我們做 X，會發生什麼？」——而不是「我們決定做 X，因為…」（那是 `decisions/`）也不是「我們要做 X，步驟如下」（那是 `plans/`）。

## 規則

- **檔名**：`YYYY-MM-DD-<slug>.md`（偵察有時效性，日期前綴必帶）
- **frontmatter**：`category: scout`、`status: draft | superseded`
- **生命週期**：拍板後 promote——成案 → `plans/<quarter>/` 雙檔；定案（含「決定不做」）→ `decisions/`；過時未拍板 → 標 `superseded` 或移 `archived/`。scout 不是文件的長期歸宿。

## 現有偵察

| 文件 | 主題 | 狀態 |
|---|---|---|
| [2026-07-02-ns-per-workspace-decision-matrix.md](2026-07-02-ns-per-workspace-decision-matrix.md) | 「每 workspace 一個 k8s namespace」架構調整衝擊分析 | draft |
| [2026-09-18-harumi-odoo-paas-migration-plan.md](2026-09-18-harumi-odoo-paas-migration-plan.md) | harumi-odoo（叢集上的 odoo-tenant 站）搬進平台成為 odoo `cloud_service`：DB／filestore／addon 缺口、路線比較與分階段計畫 | draft |
| [2026-09-18-odoo-bitnami-coupling-inventory.md](2026-09-18-odoo-bitnami-coupling-inventory.md) | 平台程式碼綁 Bitnami odoo chart／image 的耦合盤點：改用官方 `odoo:18.0` lineage 的改動範圍與分期 | draft |
| [2026-09-22-ecpay-callback-retry-policy.md](2026-09-22-ecpay-callback-retry-policy.md) | ECPay server-to-server 回呼在停機窗口內的重送政策（官方原文）、補救途徑、vendored addon／Odoo core／平台端重複回呼冪等性；含可抄進切換／搬遷 runbook 的「停機期間金流」段（#1163） | draft |
| [2026-10-03-odoo-n8n-admin-password-reset.md](2026-10-03-odoo-n8n-admin-password-reset.md) | Odoo／n8n 管理員忘記密碼的平台重設：Odoo exec ORM（#1186）、n8n 改 owner hash＋`mfa:disable`（不採 `user-management:reset`）、平台能力欄位與介面草稿、PR 拆法（#1370） | draft |
