# Issue tracker: Gitea（tea CLI）

本 repo 的 issues／PRs 活在 Gitea：`git-prod.woowtech.io` 的 `odoo-addons/woow_paas_platform`。
一律用 `tea` CLI 操作。`gh-origin`（GitHub）僅為 mirror，不在上面開 issue／PR。

## 慣例

- **語言**：Issue／PR／留言一律 zh-tw 繁體中文。
- **建 issue**：`tea issues create --repo odoo-addons/woow_paas_platform --title "..." --description "..."`。
- **讀 issue（含留言）**：`tea issues <n> --comments --repo odoo-addons/woow_paas_platform`。
- **留言**：`tea comment --repo <repo> <n> "<body>"`——**flags 在前、body 最後**，否則會開 editor 掛住；多行 body 用 quoted heredoc 存變數再傳。
- **關 issue**：`tea issues close <n> --repo <repo>`。合併 PR 時用 description 內 `Closes #<n>`（僅 target＝預設分支 `develop` 時自動生效）；僅參照用 `Refs #<n>`。
- **label 慣例**：`pending`＝待拍板項，以 issue 為權威載體（列全：`tea issues list --labels pending`）。
- **陷阱**：`tea actions runs list` 只列 completed，查 CI 狀態改用 Gitea API `/actions/runs`；tea config 的 token 是 OAuth2、不能拿去直打 REST API。

## PRs as a request surface

**No.**（若日後要把外部 PR 納入 triage 佇列，把本行改為 yes。）

## 當 skill 說「publish to the issue tracker」

建一張 Gitea issue（`tea issues create`，zh-tw）。

## 當 skill 說「fetch the relevant ticket」

跑 `tea issues <n> --comments --repo odoo-addons/woow_paas_platform`。
