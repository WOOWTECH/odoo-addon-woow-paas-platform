---
name: K8s Dev Sandbox Manager
description: 管理 Kubernetes 開發沙盒環境，支援建立、銷毀、查看狀態、日誌、延長 TTL、測試及建構映像等操作。
usage: |
  Use this skill when:
  - User wants to create, destroy, or manage K8s dev sandboxes
  - User says "sandbox", "沙盒", "k8s sandbox", "dev sandbox"
  - User wants to quickly spin up/down a sandbox for the current branch

  Invoke with: /k8s-dev-sandbox <subcommand> [args]
  Keywords: k8s, kubernetes, sandbox, 沙盒, dev environment, 開發環境
---

# K8s Dev Sandbox Manager

管理 Kubernetes 開發沙盒環境的完整工具。透過子命令路由到對應的腳本。

## 子命令參考

| 子命令 | 說明 | 對應腳本 |
|--------|------|----------|
| `create` | 建立新沙盒（互動式） | `scripts/k8s-sandbox-create.sh` |
| `destroy <name>` | 銷毀指定沙盒 | `scripts/k8s-sandbox-destroy.sh` |
| `list` | 列出所有沙盒 | `scripts/k8s-sandbox-list.sh` |
| `status [<name>]` | 顯示沙盒詳細狀態 | `scripts/k8s-sandbox-status.sh` |
| `logs <name> [opts]` | 查看服務日誌 | `scripts/k8s-sandbox-logs.sh` |
| `extend <name> [opts]` | 延長沙盒 TTL | `scripts/k8s-sandbox-extend.sh` |
| `test <name> [opts]` | 在沙盒中執行測試 | `scripts/k8s-sandbox-test.sh` |
| `build [opts]` | 建構 CI 用 Docker 映像 | `scripts/k8s-sandbox-build.sh` |
| `up` | 快速建立當前分支的沙盒 | `scripts/k8s-sandbox-create.sh`（使用預設值） |
| `down` | 快速銷毀當前分支的沙盒 | `scripts/k8s-sandbox-destroy.sh --force` |
| （無子命令） | 顯示使用說明 | 顯示下方說明 |

## 執行流程

### 步驟 1：解析子命令

從使用者的 `/k8s-dev-sandbox` 呼叫中擷取子命令和參數。

- 子命令是第一個非選項參數（例如 `create`, `destroy`, `list` 等）
- 其餘參數原樣傳遞給對應腳本

### 步驟 2：根據子命令路由執行

#### `create` - 建立新沙盒

```bash
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-create.sh <args>
```

可用選項：
- `--branch <name>` - 覆蓋分支 slug（預設從 git 自動偵測）
- `--ttl <duration>` - 覆蓋 TTL（預設 168h = 7 天）
- `--image <tag>` - 使用 CI 模式與指定映像 tag
- `--values <file>` - 額外的 Helm values 檔案
- `--pgadmin` - 啟用 PgAdmin

#### `destroy <name>` - 銷毀沙盒

```bash
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-destroy.sh <name> <args>
```

可用選項：
- `--force` - 跳過確認提示

#### `list` - 列出所有沙盒

```bash
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-list.sh
```

#### `status [<name>]` - 顯示沙盒狀態

```bash
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-status.sh [<name>]
```

- 若未提供名稱，自動從當前分支偵測

#### `logs <name> [--service <svc>] [--follow]` - 查看日誌

```bash
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-logs.sh <name> <args>
```

可用選項：
- `--service <name>` - 指定服務：odoo（預設）、postgres、nginx
- `-f, --follow` - 即時追蹤日誌
- `--tail <n>` - 顯示行數（預設 100）

#### `extend <name> [--ttl <duration>]` - 延長 TTL

```bash
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-extend.sh <name> <args>
```

可用選項：
- `--ttl <duration>` - 新的 TTL 持續時間（例如 "168h"、"24h"、"0" 表示永不過期）

#### `test <name> [--module <mod>]` - 執行測試

```bash
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-test.sh <name> <args>
```

可用選項：
- `--module <name>` - 要測試的模組（預設 woow_paas_platform）
- `--keep-on-fail` - 測試失敗時延長沙盒 TTL 24 小時以便除錯

#### `build [--registry <reg>] [--push]` - 建構 Docker 映像

```bash
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-build.sh <args>
```

可用選項：
- `--registry <registry>` - Registry 前綴（例如 registry.example.com/）
- `--push` - 建構後推送映像

映像 tag 格式：`{registry}woow-odoo-dev:{branch-slug}-{commit-short}`

#### `up` - 快速建立

快速建立當前分支的沙盒，使用所有預設值，無需互動。

```bash
# 1. 取得當前分支
BRANCH=$(git branch --show-current)

# 2. 產生 slug（將 / 和特殊字元轉為 -，並截斷長度）
SLUG=$(echo "$BRANCH" | sed 's|[/_]|-|g' | sed 's|--*|-|g' | cut -c1-40 | sed 's|-$||')

# 3. 呼叫 create 腳本
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-create.sh --branch "$SLUG"
```

#### `down` - 快速銷毀

快速銷毀當前分支對應的沙盒，跳過確認提示。

```bash
# 1. 取得當前分支
BRANCH=$(git branch --show-current)

# 2. 產生 slug（與 up 相同邏輯）
SLUG=$(echo "$BRANCH" | sed 's|[/_]|-|g' | sed 's|--*|-|g' | cut -c1-40 | sed 's|-$||')

# 3. 呼叫 destroy 腳本（--force 跳過確認）
cd <PROJECT_ROOT> && bash scripts/k8s-sandbox-destroy.sh "$SLUG" --force
```

#### 無子命令 - 顯示使用說明

若使用者未提供子命令，顯示以下說明：

```
K8s Dev Sandbox Manager - 管理 Kubernetes 開發沙盒環境

使用方式：
  /k8s-dev-sandbox <subcommand> [args]

可用子命令：
  create              建立新沙盒（互動式）
  destroy <name>      銷毀指定沙盒
  list                列出所有沙盒
  status [<name>]     顯示沙盒詳細狀態
  logs <name> [opts]  查看服務日誌
  extend <name>       延長沙盒 TTL
  test <name> [opts]  在沙盒中執行測試
  build [opts]        建構 CI 用 Docker 映像
  up                  快速建立當前分支的沙盒（使用預設值）
  down                快速銷毀當前分支的沙盒

範例：
  /k8s-dev-sandbox up                              # 快速啟動
  /k8s-dev-sandbox create --ttl 48h --pgadmin      # 自訂建立
  /k8s-dev-sandbox status                          # 查看當前分支沙盒狀態
  /k8s-dev-sandbox logs epic-smarthome -f          # 即時追蹤日誌
  /k8s-dev-sandbox test epic-smarthome --module sale_management
  /k8s-dev-sandbox down                            # 快速關閉
```

### 步驟 3：執行腳本並顯示結果

1. 使用 Bash 工具在專案根目錄下執行對應腳本
2. 將腳本輸出完整呈現給使用者
3. 若腳本執行失敗（非零退出碼），顯示錯誤訊息並建議排查方式

### 步驟 4：完成通知

執行完成後，使用語音通知使用者：

```bash
say -r 180 "沙盒操作完成了"
```

## 重要提醒

- **專案根目錄**：所有腳本需在專案根目錄下執行
- **kubectl 權限**：需要有效的 Kubernetes 叢集連線（`kubectl` 已設定）
- **Helm**：部分操作需要 Helm 3.13+
- **分支偵測**：`up` / `down` 以及未提供名稱的指令會自動從 `git branch --show-current` 偵測
- **TTL 格式**：支援 Go duration 格式，如 `24h`、`168h`、`0`（永不過期）
- **timeout**：`create` 和 `test` 指令可能耗時較長，建議設定 timeout 為 300000ms（5 分鐘）
