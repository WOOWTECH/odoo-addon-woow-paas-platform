# docs 索引

> 這是 **routing 入口**：先問「我現在想做什麼」，再往下鑽。
> 分類的定義與寫新檔該放哪，見 [`README.md`](README.md)。
> 本檔由 `scripts/audit/gen-docs-index.py` 產生，數字即時算出——**不要手改**。

目前共 **703** 份文件（含 `archived/` 324 份）。

## 我要照步驟做一件事

| 去哪 | 內容 | 篇數 |
|---|---|---|
| [`guides/deployment/`](guides/deployment/) | 上 prod／stg、發版、升版、image 重釘 | 21 |
| [`guides/testing/`](guides/testing/) | 跑測試、E2E、本機沙盒 | 8 |
| [`guides/development/`](guides/development/) | 開發期工具與架構 | 5 |

## 我要查一個事實或規格

| 去哪 | 內容 | 篇數 |
|---|---|---|
| [`reference/api/`](reference/api/) | HTTP / JSON API 端點 | 4 |
| [`reference/features/`](reference/features/) | 功能規格與產品行為 | 8 |
| [`reference/gotchas/`](reference/gotchas/) | 框架與基礎設施的行為陷阱（踩過才知道的那種） | 7 |
| [`reference/architecture/`](reference/architecture/) | 生命週期與架構圖 | 1 |

## 我想知道為什麼這樣設計

| 去哪 | 內容 | 篇數 |
|---|---|---|
| [`decisions/patterns/`](decisions/patterns/) | 可複用的設計 pattern（有 hook 強制閱讀） | 37 |
| [`decisions/philosophy/`](decisions/philosophy/) | 整體取向 | 1 |
| [`decisions/refactor/`](decisions/refactor/) | 重構決策 | 7 |
| [`decisions/reviews/`](decisions/reviews/) | PR review 裁決 | 8 |

## 我要啟動一個 epic / 大功能

| 去哪 | 內容 | 篇數 |
|---|---|---|
| [`plans/`](plans/) | 按季度分目錄，design + implementation 雙檔 | 85 |

## 我要查某張 issue 當初怎麼收的、或某個坑

| 去哪 | 內容 | 篇數 |
|---|---|---|
| [`tracking/landings/`](tracking/landings/) | 已收官 issue 的落地紀錄（檔名帶 issue 編號） | 71 |
| [`tracking/pitfalls/`](tracking/pitfalls/) | 撞牆時對照：症狀 → 根因 | 48 |
| [`tracking/followups/`](tracking/followups/) | 尚未收掉的追蹤項 | 2 |
| [`tracking/test-reports/`](tracking/test-reports/) | E2E / 測試報告 | 5 |

## 其他

| 去哪 | 內容 | 篇數 |
|---|---|---|
| [`archived/`](archived/) | 不再使用但保留考古（含 `archived/memory/`：已被其他載體取代的 memory） | 324 |
| [`scout/`](scout/) | 決策前偵察，拍板後 promote 到 plans/ 或 decisions/ | 10 |
| [`agents/`](agents/) | mattpocock-skills 工具自管理區 | 3 |
| [`superpowers/`](superpowers/) | 已退役 obra/superpowers plugin 的舊產出區（只剩歷史） | 1 |

> `docs/meeting/` 是指向 repo 外錄音目錄的 symlink，不在版控內，也不屬上述分類。
