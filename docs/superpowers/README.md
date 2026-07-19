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
- **已落地、仍是唯一權威來源**（被 CLAUDE.md / hooks / tests 引用為設計依據、含主 docs 沒有的規格或 trade-off 論述） → 用 `/docs:promote superpowers:<file>` 升級到主結構：
  - 含 trade-off 論述 → `docs/decisions/`
  - 完整 design+impl plan → `docs/plans/<quarter>/`
  - 含 pitfall / incident → `docs/tracking/pitfalls/`
- **已落地且知識已被主 docs 等值覆蓋、或被推翻/過時** → 移到 `docs/archived/superpowers/{plans,specs}/`，frontmatter 標 `status: archived` + `archived-reason`（指向現行權威），並在該區 README 補一行索引

## promote / archive 後（2026-07-19 起新政策）

- **原檔直接搬走（git mv），不留原地副本**——搬移即刪除，減少文件量；探索脈絡由 git history 與新位置 frontmatter 保留
- promote：新檔 frontmatter 加 `source: docs/superpowers/<sub>/<file>.md` + `promoted-at`
- archive：frontmatter 加 `archived-from` + `archived-reason`
- **同步改寫全 repo inbound 引用**（含 CLAUDE.md、`.claude/hooks/pattern-guard-map.json`、src/tests docstring）——搬移前先 `git grep <basename>` 建引用清單
- 2026-07-19 批次整理（一）：07-10（含）以前的 39 檔已全數搬出（29 promote / 10 archive），逐檔去向見 `docs/archived/superpowers/README.md` 與 git log
- 2026-07-19 批次整理（二）：07-13（含）以前的 3 檔搬出（336-ha-paas-provision 與 wave-b-reseed-generator promote 至 `docs/plans/2026-Q3/`、wave-a-reseed-mechanisms archive）

## CI 例外

`docs/superpowers/` 已加入 `.lychee.toml` 例外清單（broken link 與 orphan 皆不檢，本目錄在 .lychee.toml exclude_path 被整段排除）。
