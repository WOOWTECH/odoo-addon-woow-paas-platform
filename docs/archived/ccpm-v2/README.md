# ccpm v2 — 歷史產出（2026-08-07 退役）

**ccpm（[automazeio/ccpm](https://github.com/automazeio/ccpm)）已於 2026-08-07 停用**（issue #631）。
本目錄保存 ccpm v2 時期的全部歷史產出，**唯讀考古用**，不再新增、不再維護。

## 為什麼退役

- `.claude/epics/` 底下**沒有任何 active epic**，最後一次異動是 **2026-05-19**（近三個月前）
- 實際 PM 早已走 **Gitea issues（`tea` CLI）**，近期工作（#598 / #614 / #615 / #621 / #626 / #627…）全部 issue-driven
- 工具（skill symlink、`/brainstorm-sc-to-ccpm`、使用指南）與 issue-driven 實況脫節，留著只會誤導

## 內容

| 路徑 | 內容 | 原位置 |
|------|------|--------|
| [`prds/`](prds/) | 23 份 PRD | `.claude/prds/` |
| [`epics/`](epics/) | 23 個 epic（含 task、`updates/` 執行進度） | `.claude/epics/archived/`（22 個）＋ `.claude/epics/.archived/`（1 個：`standalone-owl-app-shell`） |

兩個來源 archive 目錄在此**合併**（已驗證無同名 epic、無同名檔案）。搬移全程用 `git mv`，
git 歷史可透過 `git log --follow <path>` 連續追溯。

## 閱讀須知

- 內文的 `.claude/prds/…`、`.claude/epics/…` 路徑引用是**當時的實況**，那些目錄現已不存在——對照時請自行映射到本目錄
- 內文引用的 `github.com/WOOWTECH/odoo-addons/issues/<N>` 是**舊 GitHub mirror**，多半已 404；現行 issue tracker 是 Gitea（`git-prod.woowtech.io`）
- 本目錄在 `.lychee.toml` 的 `exclude_path` 涵蓋範圍內（`docs/archived`），CI link check 不掃

## 相關

- ccpm **v1** 的 project context 文件在 [`../ccpm-v1/`](../ccpm-v1/)（更早一代，v2 上線時就已 archive）
- 現行 PM 流程：Gitea issues，見 [`docs/agents/issue-tracker.md`](../../agents/issue-tracker.md)
