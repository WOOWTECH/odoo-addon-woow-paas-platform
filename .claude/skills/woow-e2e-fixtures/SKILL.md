---
name: woow-e2e-fixtures
description: "woow 專屬的沙盒 fixture / clone helper（三支腳本）：e2e-seed（tests/e2e/skill）、e2e-seed-reset-button（tests/e2e/service-reset-button）、seed-from-stg（stg DB + filestore clone）。三支都作用在「已存在」的沙盒上。建立／銷毀沙盒、跑測試、延長 TTL 一律用 /woow-dev:k8s-dev-sandbox。"
usage: |
  Use this skill when:
  - User mentions e2e skill fixtures / reset-button fixtures / seed-from-stg（woow 專屬）

  建立 / 管理沙盒 / 跑測試 / 延長 TTL（create/destroy/list/status/logs/build/test/extend）
  → 改用 /woow-dev:k8s-dev-sandbox（`create`/`destroy` 必帶 --context；需要被測 repo 的子命令帶 --repo）
  Keywords: e2e, seed, fixture, seed-from-stg, sandbox, 沙盒, 測試資料
---

# woow E2E Fixtures — 沙盒 fixture / clone helper（三支）

> **⚠️ 沙盒生命週期與 test／extend 都在 woow-dev plugin**：`create` / `destroy` / `list` / `status` / `logs` / `build` / `test` / `extend` 一律 `/woow-dev:k8s-dev-sandbox <command>`（需帶 `--repo "$(git rev-parse --show-toplevel)"` 與 `--context`）。
> 2026-06 先移生命週期；**2026-09-04 再退役 `woow-test.sh` 與 `woow-extend.sh`**——前者與 plugin `test.sh` 逐行等價（plugin 版多 `--context` 守衛），後者寫的是舊 annotation key `sandbox.woow.tw/expires`，TTL cronjob 讀的是 `dev-sandbox.io/expires`，會印綠燈但沙盒照舊時間被刪。
> 同日本 skill 由 `k8s-dev-sandbox` **改名為 `woow-e2e-fixtures`**：剩下的三支腳本都不管沙盒生命週期了，而舊名與 plugin skill 同名（skill 清單裡兩筆 `k8s-dev-sandbox`，只靠 `woow-dev:` 前綴區分）。
> 舊 `woow-test.sh --e2e-seed` 的「測試通過後自動 seed」改由 plugin `test.sh` 的 post-test hook 承接：本 repo 的 [`.sandbox/post-test.sh`](../../../.sandbox/post-test.sh)（opt-in，帶 `WOOW_E2E_SEED=1` 才 seed）。
> 設計與實作：`docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-{design,implementation}.md`；退役稽核：`.scratch/k8s-dev-sandbox-skill-retirement-audit.md`（未 tracked）。

## 三支 helper（非 plugin 子命令，直接 `bash` 跑）

| 腳本 | 用途 | 使用者 |
| --- | --- | --- |
| `scripts/e2e-seed.sh <slug> [--context <ctx>] [--reset\|--purge]` | 把 e2e skill fixture（project + draft skill）冪等 seed 進已存在的沙盒；`--name`／`--version` 預設必須與 spec 的 `SKILL_E2E_NAME` 對齊 | `tests/e2e/skill`、`.sandbox/post-test.sh` |
| `scripts/e2e-seed-reset-button.sh <slug> [--manifest <path>\|--purge]` | Reset 按鈕可見性 e2e 的 fixture（1 workspace ＋ 4 個代表模板的 `state=running` CloudService），輸出 manifest JSON 給 spec 讀（#464）。路徑登記在 repo-lint `PROVISION_SENTINEL_ALLOWLIST`，搬動要同步 | `tests/e2e/service-reset-button` |
| `scripts/seed-from-stg.sh` | 從 stg 環境 clone DB + filestore 到本地沙盒（woow-k3s 專屬；⚠ `kubectl exec`／`cp` 拉大檔會尾端截斷，見 `docs/tracking/pitfalls/kubectl-exec-stdout-tail-truncation-large-stream.md`） | 手動 |

前兩支 `source .claude/skills/lib/kube-context.sh`（#32）驗 `--context`，白名單在 `.sandbox.yml` 的 `allowed_kube_contexts[_<task>]`；`PROJECT_ROOT` 由 `scripts/../../../..` 推導，**搬目錄要保持同一深度**（`.claude/skills/<name>/scripts/` 這一層）。

### 測試通過後自動 seed（取代 `woow-test.sh --e2e-seed`）

```bash
WOOW_E2E_SEED=1 bash "${CLAUDE_PLUGIN_ROOT}/skills/k8s-dev-sandbox/scripts/test.sh" <slug> \
    --repo "$(git rev-parse --show-toplevel)" --context <ctx>
```

（上面這行的 `skills/k8s-dev-sandbox/` 是 **woow-dev plugin 的** skill，不是本 skill。）

plugin `test.sh` 在測試通過、rollout 完成後執行 `<repo>/.sandbox/post-test.sh`（傳入 `NAMESPACE`／`DB_NAME`／`SLUG`／`MODULE`／`REPO_ROOT`／`ODOO_DEPLOY`）；hook 沒看到 `WOOW_E2E_SEED=1` 就印一行略過並 exit 0。

## 重要提醒

- **專案根目錄**：腳本需在專案根目錄下執行。
- **沙盒須先存在**：三支都作用在「已存在」的沙盒上；建立請用 `/woow-dev:k8s-dev-sandbox create --context <ctx> --repo "$(git rev-parse --show-toplevel)"`。
- **完成通知**：`say -r 180 "沙盒操作完成了"`。
