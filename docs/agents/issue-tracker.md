# Issue tracker: Gitea（tea CLI）

本 repo 的 issues／PRs 活在 Gitea：`git-prod.woowtech.io` 的 `odoo-addons/woow_paas_platform`。
一律用 `tea` CLI 操作。`gh-origin`（GitHub）僅為 mirror，不在上面開 issue／PR。

## 慣例

- **語言**：Issue／PR／留言一律 zh-tw 繁體中文。
- **建 issue**：`tea issues create --repo odoo-addons/woow_paas_platform --title "..." --description "..."`。
- **讀 issue（含留言）**：`tea issues <n> --comments --repo odoo-addons/woow_paas_platform`。
- **留言**：`tea comment --repo <repo> <n> "<body>"`——**flags 在前、body 最後**，否則會開 editor 掛住；多行 body 用 quoted heredoc 存變數再傳。
- **關 issue**：`tea issues close <n> --repo <repo>`。合併 PR 時用 description 內 `Closes #<n>`（僅 target＝預設分支 `develop` 時自動生效）；僅參照用 `Refs #<n>`。
- **label 慣例**：work-state 六選一（見 `triage-labels.md`），待拍板＝`needs-decision`，以 issue 為權威載體（列全：`tea issues list --labels needs-decision`）。
- **陷阱**：`tea actions runs list` 只列 completed，查 CI 狀態改用 Gitea API `/actions/runs`；tea config 的 token 是 OAuth2、不能拿去直打 REST API。

## PRs as a request surface

**No.**（若日後要把外部 PR 納入 triage 佇列，把本行改為 yes。）

## 當 skill 說「publish to the issue tracker」

建一張 Gitea issue（`tea issues create`，zh-tw）。

## 當 skill 說「fetch the relevant ticket」

跑 `tea issues <n> --comments --repo odoo-addons/woow_paas_platform`。

## Wayfinding operations

供 `/wayfinder` 使用。**map** 是一張 issue，**child ticket** 是它的子 issue。本 repo 的 Gitea 已開 issue dependencies（`tea api repos/odoo-addons/woow_paas_platform` → `internal_tracker.enable_issue_dependencies: true`），blocking 走原生依賴，UI 的「Blocked by」看得到。以下指令一律帶 `--repo odoo-addons/woow_paas_platform`（省略不寫）。

- **Map**：一張 issue，label `wayfinder:map`，body 放 Notes / Decisions-so-far / Fog。`tea issues create --title "<map name>" --description "<body>" --labels wayfinder:map`。label 不存在先建：`tea labels create --name wayfinder:map --color 5319e7`；child 用的 `wayfinder:research`／`wayfinder:prototype`／`wayfinder:grilling`／`wayfinder:task` 同法。
- **Child ticket**：description 第一行 `Part of #<map>`，labels `wayfinder:<type>`（research／prototype／grilling／task）。`tea issues create --title "<ticket name>" --description "$(printf 'Part of #%s\n\n%s' "$MAP" "$BODY")" --labels wayfinder:task`。認領後 assign 給驅動的人。
- **Blocking**：Gitea 原生 issue dependency。加：`tea api -X POST repos/odoo-addons/woow_paas_platform/issues/<child>/dependencies -F index=<blocker>`；查：`tea api repos/odoo-addons/woow_paas_platform/issues/<child>/dependencies`（回傳擋住它的 issue 陣列；`…/issues/<n>/blocks` 回傳它擋住的）。全部 blocker 關閉即解封。
- **Frontier query**：`tea issues list --keyword "Part of #<map>" --state open --output json --fields index,title,labels,assignees`（`keyword` 搜 title＋body），逐張再查 dependencies，丟掉「有任一 open blocker」或「已有 assignee」的，剩下依 map body 的列表順序取第一張。
- **Claim**：`tea issues edit <n> --add-assignees <帳號>`（自己的帳號看 `tea logins list` 的 USER 欄）——這是 session 的第一個寫入。
- **Resolve**：`tea comment <n> "<answer>"`（flags 在前、body 最後，見上方陷阱）→ `tea issues close <n>` → 在 map 的 Decisions-so-far 追加一行「gist＋連結」：先 `tea issues <map> --output json` 讀出 body，整段改寫後 `tea issues edit <map> --description "<new body>"`。
