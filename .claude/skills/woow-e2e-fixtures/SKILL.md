---
name: woow-e2e-fixtures
description: "woow 專屬的沙盒 fixture / clone helper：e2e-seed（tests/e2e/skill）、e2e-seed-reset-button（tests/e2e/service-reset-button）、seed-from-prod＋neutralize-clone（prod clone 灌進 lockdown 平台沙盒並中和，#1206）。全部作用在「已存在」的沙盒上。建立／銷毀沙盒、跑測試、延長 TTL 一律用 /woow-dev:k8s-dev-sandbox。"
when_to_use: 使用者提到 e2e skill fixture、reset-button fixture、prod clone、中和（neutralize）、彩排沙盒灌資料。建立／銷毀沙盒、跑測試、延長 TTL 不在此，改用 /woow-dev:k8s-dev-sandbox。
---

# woow E2E Fixtures — 沙盒 fixture / clone helper

> **沙盒生命週期與 test／extend 都在 woow-dev plugin**：`create` / `destroy` / `list` / `status` / `logs` / `build` / `test` / `extend` 一律 `/woow-dev:k8s-dev-sandbox <command>`（需帶 `--repo "$(git rev-parse --show-toplevel)"` 與 `--context`）；本 skill 只放 fixture／clone helper，不管沙盒生命週期。
> ⚠ 延長 TTL 一律走 plugin `extend`：已退役的 `woow-extend.sh` 寫的是舊 annotation key `sandbox.woow.tw/expires`，TTL cronjob 讀的是 `dev-sandbox.io/expires`，會印綠燈但沙盒照舊時間被刪。
> 測試通過後自動 seed 由 plugin `test.sh` 的 post-test hook 承接：本 repo 的 [`.sandbox/post-test.sh`](../../../.sandbox/post-test.sh)（opt-in，帶 `WOOW_E2E_SEED=1` 才 seed）。
> 設計與實作：`docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-{design,implementation}.md`。

## helper（非 plugin 子命令，直接 `bash` 跑）

| 腳本 | 用途 | 使用者 |
| --- | --- | --- |
| `scripts/e2e-seed.sh <slug> [--context <ctx>] [--reset\|--purge]` | 把 e2e skill fixture（project + draft skill）冪等 seed 進已存在的沙盒；`--name`／`--version` 預設必須與 spec 的 `SKILL_E2E_NAME` 對齊 | `tests/e2e/skill`、`.sandbox/post-test.sh` |
| `scripts/e2e-seed-reset-button.sh <slug> [--manifest <path>\|--purge]` | Reset 按鈕可見性 e2e 的 fixture（1 workspace ＋ 4 個代表模板的 `state=running` CloudService），輸出 manifest JSON 給 spec 讀（#464）。路徑登記在 repo-lint `PROVISION_SENTINEL_ALLOWLIST`，搬動要同步 | `tests/e2e/service-reset-button` |
| `seed-from-stg`（舊入口） | **已退役（#1955）**：腳本已刪除，不保留提示入口；替代做法與歷史查閱方式見下方退役說明 | — |
| `scripts/seed-from-prod.sh receive\|restore\|stage-b\|gate\|close <slug> --context <ctx>` | prod clone 的**沙盒段**（#1206）：搬運洞（HTTP PUT receiver）、restore、Stage A／B、斷言 gate。只作用在 `SANDBOX_NEUTRALIZE_LOCKDOWN=1` 建的 platform 沙盒；取資料段由人執行 | 彩排（[runbook](../../../docs/guides/deployment/prod-clone-neutralization-runbook.md)） |
| `scripts/neutralize-clone.sh <slug> --context <ctx> [--db woow_prod] [--write-upgrade-marker]` | 中和：官方 `odoo neutralize --stdout`＋#1201 平台段＋`prod-clone/supplement.sql`＋沙盒密碼，同一個交易；結尾跑 `prod-clone/assertions.toml` 斷言 gate | `seed-from-prod.sh` |

### seed-from-stg 已退役（#1955）

2026-10-23 使用者選擇正式退役（[#1955](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/1955)）：舊來源 `dev-sandbox-stg` 已退役，`scripts/seed-from-stg.sh` 已刪除，不改寫成新 stg 來源。

- 一般 E2E 使用合成 fixture：`e2e-seed.sh`、`e2e-seed-reset-button.sh`。
- 需要 prod 資料形狀的彩排，走下方既有 prod clone 的授權、中和與銷毀流程；不是一般測試的預設資料來源。
- **prod clone 不能重現 stg 特有的資料或版本問題**；有此需求時另開 issue 討論來源與安全搬運方式。

舊流程僅供歷史查閱，不作為執行指引：`git show 686c1e2c:.claude/skills/woow-e2e-fixtures/scripts/seed-from-stg.sh`。

### prod clone（#1206）

- 規範正本 #1162 resolution（留言 14057）；操作順序、取資料 recipe、外連證據、銷毀證據全在 [prod clone 中和 runbook](../../../docs/guides/deployment/prod-clone-neutralization-runbook.md)。
- 斷言 fixture `scripts/prod-clone/assertions.toml` 是 gate 本身：改它要有人覆核。純函式測試 `python3 scripts/prod-clone/test_prod_clone_tool.py`。
- 第 0 條防呆（`prod-clone/lib.sh` `pc_target_guard`）：slug 必填、`dev-sandbox-*` 且 ≠ `dev-sandbox-stg`、ns label `app.kubernetes.io/name=odoo-dev-sandbox`、platform mode、`dev-sandbox.io/neutralize-lockdown=1`。

前兩支與 prod clone 兩支都 `source .claude/skills/lib/kube-context.sh`（#32）驗 `--context`，白名單在 `.sandbox.yml` 的 `allowed_kube_contexts[_<task>]`；`PROJECT_ROOT` 由 `scripts/../../../..` 推導，**搬目錄要保持同一深度**（`.claude/skills/<name>/scripts/` 這一層）。

### 測試通過後自動 seed

```bash
WOOW_E2E_SEED=1 bash "<woow-dev plugin 根>/skills/k8s-dev-sandbox/scripts/test.sh" <slug> \
    --repo "$(git rev-parse --show-toplevel)" --context <ctx>
```

（`skills/k8s-dev-sandbox/` 是 **woow-dev plugin 的** skill，不是本 skill。plugin 根在 marketplace cache `~/.claude/plugins/cache/*/woow-dev/<版本>/` 或 `plugins-local/`；`${CLAUDE_PLUGIN_ROOT}` 只在 plugin 自己的 skill 內展開，在本 repo 的 skill 裡是空字串，不要用。）

plugin `test.sh` 在測試通過、rollout 完成後執行 `<repo>/.sandbox/post-test.sh`（傳入 `NAMESPACE`／`DB_NAME`／`SLUG`／`MODULE`／`REPO_ROOT`／`ODOO_DEPLOY`）；hook 沒看到 `WOOW_E2E_SEED=1` 就印一行略過並 exit 0。

## 重要提醒

- **專案根目錄**：腳本需在專案根目錄下執行。
- **沙盒須先存在**：全部都作用在「已存在」的沙盒上；建立請用 `/woow-dev:k8s-dev-sandbox create --context <ctx> --repo "$(git rev-parse --show-toplevel)"`。
- **完成通知**：`say -r 180 "沙盒操作完成了"`。
