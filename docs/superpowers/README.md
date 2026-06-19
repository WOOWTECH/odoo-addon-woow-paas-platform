# superpowers/ — 工具自管理區

**obra/superpowers plugin** 的 brainstorming / writing-plans skill 會自動寫入這裡。**不是主題類目錄**，類似 `.claude/prds/_drafts/` 的角色。

## 為什麼存在

obra/superpowers 是 Plugin 形式安裝（`~/.claude/plugins/cache/claude-plugins-official/superpowers/`），SessionStart hook 自動注入 system prompt。其 skill 預設輸出位置寫死在 `docs/superpowers/`，無法 redirect。

## 子目錄

| 路徑 | 內容 |
|------|------|
| `specs/` | `brainstorming` skill 輸出：`YYYY-MM-DD-<topic>-design.md` |
| `plans/` | `writing-plans` skill 輸出：`YYYY-MM-DD-<feature>.md` |

## 入版控原因

- 保留探索痕跡（rejected options、persona 觀點）
- 多 agent 同步時可回查
- 與 `.claude/prds/_drafts/` 同性質

## promote 規則

- **進行中 / 短期參考** → 留在這
- **已落地、含長期價值** → 用 `/docs:promote superpowers:<file>` 升級到主結構：
  - 含 trade-off 論述 → `docs/decisions/`
  - 完整 design+impl plan → `docs/plans/<quarter>/`
  - 含 pitfall / incident → `docs/tracking/pitfalls/`
- **過期、未落地** → 留原處，archive 邏輯延後設計

## promote 後

- 原檔頂端加 `> Promoted to: docs/<path>` 註記
- **不刪原檔**（保留探索脈絡）
- 主結構新檔 frontmatter 加 `source: docs/superpowers/specs/<file>.md`

## CI 例外

`docs/superpowers/` 已加入 `.lychee.toml` 例外清單（broken link 與 orphan 皆不檢，本目錄在 .lychee.toml exclude_path 被整段排除）。
