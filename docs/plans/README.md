# plans/ — 過程紀錄

**design + implementation 雙檔模式**的工作計畫。啟動 epic / 大功能前寫，作為執行依據。

## 進入條件

- 啟動一個 **epic 或大功能** 前
- 內容含 step-by-step 執行序列（不只是設計討論）

## 退出條件

- epic 完成且回顧已 promote 到 `decisions/` 或 `tracking/` → 移到 `archived/plans/<year>/`（2026-07-19 首批已執行：April–May 26 檔，見 [`../archived/plans/README.md`](../archived/plans/README.md)）
- **歸檔前必過 brace-aware inbound 掃描**——被 code / CLAUDE.md / hooks 以 `{design,implementation}` 花括號形式引用的檔 naive grep 抓不到，屬 load-bearing 不得歸檔
- 跨年的 plan 留在原 quarter，不重歸檔

## 子目錄

| 路徑 | 內容 |
|------|------|
| [`2026-Q2/`](2026-Q2/) | 2026 年 4-6 月計畫（含 wallet-billing、subscription-integration、refactor 等） |
| [`2026-Q3/`](2026-Q3/) | 2026 年 7-9 月計畫（含 netpol 租戶隔離、harness 蒸餾、定價 taxonomy/manifest、wave-c picker 等） |
| `journal/` | 過程側記（非完整 plan，是「邊做邊記」）；目前空——過往側記已隨對應 epic 歸檔 |

## 命名

- **雙檔模式**：`YYYY-MM-DD-<slug>-design.md` + `YYYY-MM-DD-<slug>-implementation.md`
  - design：背景、需求、選項、trade-off、決策
  - implementation：分階段執行清單、檔案改動清單、測試項目、驗證方式
- **單檔模式**（少見，用於小範圍 refactor）：`YYYY-MM-DD-<slug>.md`
- 季度切分以**檔名日期**為準，跨季 plan 選首日所屬季

## 寫作慣例

- design 階段先送 review，sign-off 後再寫 implementation
- frontmatter 帶 `status: draft | signed-off | in-progress | done`
- 每階段（phase）結束更新 `implementation.md` 的 progress checklist
- plan 引用 spec 或 decision 用相對路徑
- journal 不強制 frontmatter，保留輕量

## 與 ccpm / superpowers 的關係

- ccpm 的 epic / task 走 `.claude/epics/<name>/`，不放這 — 完成 archive 後透過 `/docs:promote` 把決策 promote
- superpowers writing-plans 輸出在 `docs/superpowers/plans/`，promote 後可進 `plans/<quarter>/`
