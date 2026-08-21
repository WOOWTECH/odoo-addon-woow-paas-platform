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

## 命名

- 保留原檔名（含原日期前綴）
- 不重新命名以維持 git history 連續性

## 寫作慣例

- **不要修改 archived 內檔案**（除非補 frontmatter 標 `status: archived`）
- 若有引用方仍指向 archived 內檔案，CI link check 例外清單已涵蓋
- archive 動作需在 PR 描述說明原因（哪個 decision 推翻 / 哪個 feature 下架）

## CI 例外

`docs/archived/` 已加入 `.lychee.toml` 例外清單（broken link / orphan 都不檢）。
