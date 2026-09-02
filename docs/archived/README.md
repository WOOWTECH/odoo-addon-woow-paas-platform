# archived/ — 歷史檔案

**不再使用但保留考古**。對應功能下架、決策被推翻、外部工具版本汰換的檔案歸這。

## 進入條件

- 對應功能下架、決策被推翻、工具汰換
- 從其他類別移入（不直接建檔）

## 退出條件

- **永久保留**，不刪不退

## 子目錄

| 路徑 | 內容 |
|------|------|
| [`ccpm-v2/`](ccpm-v2/README.md) | 退役的 ccpm v2 歷史產出：23 份 PRD ＋ 23 個 epic（2026-08-07 停用，見 #631） |
| [`ccpm-v1/`](ccpm-v1/) | 更早一代 ccpm v1 的 project context 文件 |
| [`superpowers/`](superpowers/README.md) | 退役的 superpowers 探索文件（被推翻 / 被取代 / 知識已轉入主 docs），逐檔原因見其 README |
| [`plans/`](plans/README.md) | 已收官 epic 的 plan（按 `<year>/` 切分），逐檔去向見其 README |
| [`tracking/`](tracking/README.md) | 已收官的一次性報告（CAQA / reviewer / test coverage / deliverables），逐檔原因見其 README |
| [`reference/`](reference/) | 被取代的 spec（如早期整站 API 設計稿 `woowtech-paas-api-specification.md`） |

## 看板快照（top-level 檔）

`paas-issue-memory` note（PaaS 上線推進看板）與其前身 `dispatcher-board` 的全文存檔。issue-leader role 規定大修剪前全文存 `docs/archived/`；檔名後綴即存檔時點（`pre-clean`＝清已關 issue 前、`pre-gate`＝加 gate 段前、`pre-trim`＝大幅精簡前、`pre-v176-landing`＝v176 landing 落板前）。內容原樣保留；其中 3 檔（dispatcher-board、08-27 pre-trim、08-28 pre-clean）首行 `[N lines total]` 是 `maestri note read` 的輸出標頭，一併保留。

| 檔案 | 存檔時點 |
|------|----------|
| `dispatcher-board-2026-08-01_08-05.md` | 2026-08-01 08:05，舊看板 `dispatcher-board` |
| `paas-issue-memory-2026-08-16-pre-clean.md` | 2026-08-16 pre-clean |
| `paas-issue-memory-2026-08-21-pre-clean.md` | 2026-08-21 pre-clean |
| `paas-issue-memory-2026-08-22-pre-clean.md` | 2026-08-22 pre-clean |
| `paas-issue-memory-2026-08-23-pre-clean.md` | 2026-08-23 pre-clean |
| `paas-issue-memory-2026-08-24-pre-clean.md` | 2026-08-24 pre-clean |
| `paas-issue-memory-2026-08-24-pre-gate.md` | 2026-08-24 pre-gate |
| `paas-issue-memory-2026-08-27-pre-v176-landing.md` | 2026-08-27 pre-v176-landing |
| `paas-issue-memory-2026-08-27-pre-trim.md` | 2026-08-27 pre-trim（該輪削 70%，見 08-28 檔頁首「本輪修剪紀錄」） |
| `paas-issue-memory-2026-08-28-pre-clean.md` | 2026-08-28 pre-clean |

## 命名

- 保留原檔名（含原日期前綴）
- 不重新命名以維持 git history 連續性

## 寫作慣例

- **不要修改 archived 內檔案**（除非補 frontmatter 標 `status: archived`）
- 若有引用方仍指向 archived 內檔案，CI link check 例外清單已涵蓋
- archive 動作需在 PR 描述說明原因（哪個 decision 推翻 / 哪個 feature 下架）

## CI 例外

`docs/archived/` 已加入 `.lychee.toml` 例外清單（broken link / orphan 都不檢）。
