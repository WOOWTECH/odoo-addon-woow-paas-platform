# plans/ — 過程紀錄

**design + implementation 雙檔模式**的工作計畫。啟動 epic / 大功能前寫，作為執行依據。

## 進入條件

- 啟動一個 **epic 或大功能** 前
- 內容含 step-by-step 執行序列（不只是設計討論）

## 退出條件

- epic 完成且回顧已 promote 到 `decisions/` 或 `tracking/` → 移到 `archived/plans/<year>/`
- 跨年的 plan 留在原 quarter，不重歸檔

## 子目錄

| 路徑 | 內容 |
|------|------|
| [`2026-Q1/`](2026-Q1/) | 2026 年 1-3 月計畫 |
| [`2026-Q2/`](2026-Q2/) | 2026 年 4-6 月計畫（含 wallet-billing、subscription-integration、refactor 等） |
| [`journal/`](journal/) | 過程側記（非完整 plan，是「邊做邊記」） |

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
