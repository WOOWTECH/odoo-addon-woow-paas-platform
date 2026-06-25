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
| [`ccpm-v1/`](ccpm-v1/) | 舊 ccpm v1 的 project context 文件（v2 不再維護） |
| `plans/` | 退役的 plan（按 `<year>/` 切分）（首次歸檔 plan 時建立） |

## 命名

- 保留原檔名（含原日期前綴）
- 不重新命名以維持 git history 連續性

## 寫作慣例

- **不要修改 archived 內檔案**（除非補 frontmatter 標 `status: archived`）
- 若有引用方仍指向 archived 內檔案，CI link check 例外清單已涵蓋
- archive 動作需在 PR 描述說明原因（哪個 decision 推翻 / 哪個 feature 下架）

## CI 例外

`docs/archived/` 已加入 `.lychee.toml` 例外清單（broken link / orphan 都不檢）。
