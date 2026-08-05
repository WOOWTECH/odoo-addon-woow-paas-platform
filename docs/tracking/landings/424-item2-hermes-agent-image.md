# project 424 item2 hermes agent image

> 自 Claude memory 遷移（2026-08-05）。原 slug `project_424_item2_hermes_agent_image`。盤點依據見 [memory-inventory-2026-08-05](../memory-inventory-2026-08-05.md)。
>
> 原 description："#424② hermes-agent OAuth patch 轉 build-time image：PR woow-paas/paas-odoo-ci#33，待 Eugene dispatch CI（含人工核可 gate）才有 image"

2026-07-27 完成 #424 第②項的 image 側：把 Elmo 的 MCP OAuth 熱修
（`Woow_hermes_agent_docker_compose_all@e4732383`）轉成 build-time patch。
**PR = `woow-paas/paas-odoo-ci#33`，已 merge**（main @ `0b7a3cae`，2026-07-27）。期間撞 gitea ns pgpool CrashLoop 全站 500 逾一小時——leader 複驗（ls-remote＋tea）證實推送/開 PR 在故障前即成功；single-branch clone 的 push 不建 remote-tracking ref，勿當失敗證據。

**② 已全程閉環（2026-07-27）**：CI run 1190（dispatch via API＋核可 issue #34 eugene `/approve`）→ image 已上 registry，pin 座標
`jcr-prod.woowtech.io/woow-paas-docker-local/hermes-agent:20260727.1502@sha256:ce3c49a7…f00f`（amd64，digest 直驗 registry 非轉抄 CI log）。①（CS 化）引用此座標。
dispatch 備忘：API `POST /actions/workflows/build-hermes-agent-image.yml/dispatches`（inputs.tag=UTC `YYYYMMDD.HHMM`）；gate 前置已確認 `PROD_APPROVERS=eugene`＋`ALLOW_SELF_APPROVE=true`；1.06GB layer 過不了 CF，推送只能走 CI 內網（勿本機 push）。

**放置地選 `paas-odoo-ci` 而非派工單首選的 `woow-mcp-server`**：後者是 MCP admin
monorepo，塞純 `FROM`+`RUN` 衍生 image 要動 uv workspace members、寫死的
`/healthz:8080` smoke、寫死的 `${app}-mcp-admin` 推送命名。前者已有 `open-design/`
這個同樣 vendored 自 Elmo compose repo 的第三方衍生 image 先例，慣例一比一沿用。
參見 [[project_opendesign_cs_onboarding_380]]。

**Elmo 原腳本照抄會從第一天就失效**（動工時已漂移，非未來風險）：
目標從 `index-*.js` 變成 code-split 出去的 `McpPage-*.js`；minified 識別字
`W`/`G` 變 `b`/`g`。故錨點改為掃 assets + 字元類捕捉識別字，牙留在語義簽名
（`ghost:!0` + ``size:`sm` `` + ``title:`Authenticate with OAuth` ``）上。
詳見 [[reference_hermes_agent_web_dist_patch_constraints]]。
