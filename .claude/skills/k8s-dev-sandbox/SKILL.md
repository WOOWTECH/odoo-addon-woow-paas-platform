---
name: k8s-dev-sandbox
description: "[大部分已移至 woow-dev plugin] woow 專屬的 K8s 沙盒 e2e / 測試工具。完整沙盒生命週期（建立/銷毀/列出/狀態/日誌/建構映像）請改用 /woow-dev:k8s-dev-sandbox。本 in-repo 版僅保留 test（沙盒內跑模組測試）、extend（TTL）+ 內部 e2e-seed / seed-from-stg helper。"
usage: |
  Use this skill when:
  - User wants to run module tests inside an existing woow sandbox (`test`)
  - User wants to extend a sandbox TTL (`extend`)
  - User mentions e2e skill fixtures / seed-from-stg（woow 專屬）

  建立 / 管理沙盒（create/destroy/list/status/logs/build）→ 改用 /woow-dev:k8s-dev-sandbox
  Invoke with: /k8s-dev-sandbox <test|extend> [args]
  Keywords: k8s, kubernetes, sandbox, 沙盒, e2e, test, seed
---

# K8s Dev Sandbox — woow 專屬 e2e / 測試工具（縮減版）

> **⚠️ 大部分已移至 woow-dev plugin（2026-06）**：完整沙盒生命週期管理（`create` / `destroy` / `list` / `status` / `logs` / `build` / `up` / `down`）已移植成通用 **`/woow-dev:k8s-dev-sandbox`**，本 in-repo 版的那些重複腳本**已移除**。
> **建立 / 管理沙盒請用 `/woow-dev:k8s-dev-sandbox <command>`**（需帶 `--repo "$(git rev-parse --show-toplevel)"` 與 `--context`）。
> `deploy-stg` 也已改為委派 plugin `create.sh`（見 `deploy-stg` skill）。
> **本 in-repo 版僅保留 woow 專屬、未進通用 plugin 的工具**（綁 `woow_paas_platform` model / stg 環境）：
> - `test` — 沙盒內跑模組測試（`scripts/woow-test.sh`）
> - `extend` — 延長 TTL（`scripts/woow-extend.sh`；供 `test --keep-on-fail`）
> - 內部 helper：`scripts/e2e-seed.sh`（tests/e2e/skill 的 skill fixture seeder）、`scripts/e2e-seed-reset-button.sh`（tests/e2e/service-reset-button 的 CloudService fixture seeder）、`scripts/seed-from-stg.sh`（從 stg clone DB + filestore）
> 設計與實作：`docs/plans/2026-Q2/2026-06-11-k8s-sandbox-plugin-port-{design,implementation}.md`。

## 子命令參考（in-repo 保留部分）

| 子命令                 | 說明                 | 對應腳本                  |
| ---------------------- | -------------------- | -------------------------- |
| `test <name> [opts]`   | 在沙盒中執行模組測試 | `scripts/woow-test.sh`   |
| `extend <name> [opts]` | 延長沙盒 TTL         | `scripts/woow-extend.sh` |

> 其餘子命令（`create` / `destroy` / `list` / `status` / `logs` / `build` / `up` / `down`）**已移除** → 請用 `/woow-dev:k8s-dev-sandbox`。

### `test <name> [--module <mod>] [--keep-on-fail]`

```bash
cd <PROJECT_ROOT> && bash .claude/skills/k8s-dev-sandbox/scripts/woow-test.sh <name> <args>
```

- `--module <name>` — 要測試的模組（預設 `woow_paas_platform`）
- `--keep-on-fail` — 測試失敗時延長沙盒 TTL 24h 以便除錯（呼叫 `woow-extend.sh`）

### `extend <name> [--ttl <duration>]`

```bash
cd <PROJECT_ROOT> && bash .claude/skills/k8s-dev-sandbox/scripts/woow-extend.sh <name> --ttl <duration>
```

- `--ttl <duration>` — Go duration，如 `24h` / `168h` / `0`（永不過期）

## 內部 helper（非直接子命令）

- `scripts/e2e-seed.sh <slug> [--reset|--purge]` — 把 e2e skill fixture seed 進「已存在」的沙盒（沙盒須先用 `/woow-dev:k8s-dev-sandbox create` 建好）。由 `woow-test.sh` 與 `tests/e2e/skill` 使用。
- `scripts/e2e-seed-reset-button.sh <slug> [--manifest <path>|--purge]` — 把 Reset 按鈕可見性 e2e 的 fixture（1 workspace ＋ 4 個代表模板的 `state=running` CloudService）seed 進已存在的沙盒，並輸出 fixture manifest JSON 給 spec 讀。由 `tests/e2e/service-reset-button` 使用（#464）。
- `scripts/seed-from-stg.sh` — 從 stg 環境 clone DB + filestore 到本地沙盒（woow-k3s 專屬）。

## 重要提醒

- **專案根目錄**：腳本需在專案根目錄下執行。
- **kubectl / Helm**：需有效叢集連線。
- **沙盒須先存在**：`test` / `extend` / `e2e-seed` 都作用在「已存在」的沙盒上；建立請用 `/woow-dev:k8s-dev-sandbox create --context <ctx> --repo "$(git rev-parse --show-toplevel)"`。
- **完成通知**：`say -r 180 "沙盒操作完成了"`。
