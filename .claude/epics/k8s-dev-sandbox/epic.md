---
name: k8s-dev-sandbox
status: backlog
created: 2026-02-28T09:41:51Z
progress: 0%
prd: .claude/prds/k8s-dev-sandbox.md
github: https://github.com/WOOWTECH/odoo-addon-woow-paas-platform/issues/117
---

# Epic: k8s-dev-sandbox

## Overview

將現有的 Docker Compose 開發環境方案擴展至 Kubernetes，透過 Helm Chart 在 K3s/K8s 上部署完整的 Odoo 開發沙盒。此 epic 產出一個 Claude Code skill（`/k8s-sandbox`），shell 腳本層處理邏輯，Helm Chart 層處理 K8s 資源編排。

核心策略：**複用現有模式**。branch slug 邏輯沿用 `setup-worktree-env.sh`，Helm chart 模式參考 `extra/paas-operator/helm/`，Odoo 配置沿用 `odoo.conf.template`。

## Architecture Decisions

| 決策 | 選擇 | 理由 |
|------|------|------|
| 部署工具 | Helm 3 | 與 PaaS Operator 一致，支援 values 覆蓋 |
| Namespace 策略 | 每個沙盒一個 namespace | 天然隔離，易於清理（刪除整個 namespace） |
| TTL 實現 | K8s CronJob + namespace annotation | 不需要額外 controller，CronJob 檢查 annotation 即可 |
| 本地程式碼掛載 | hostPath PV（K3s） | K3s 支援 local-path provisioner，最簡單 |
| CI/CD 程式碼 | Build Docker image | 無狀態，可重現 |
| DB 管理 | Chart 內建 PostgreSQL StatefulSet | 每沙盒獨立 DB，不依賴共享服務 |
| 模板引擎 | Helm `_helpers.tpl` | 沿用 PaaS Operator 的命名慣例 |

## Technical Approach

### Helm Chart (`charts/odoo-dev-sandbox/`)

核心 chart 結構，透過 `values.yaml` 控制行為：

```yaml
# values.yaml 關鍵配置
sandbox:
  mode: local          # local | ci
  ttl: "168h"          # 7 天
  branch: ""           # 自動偵測

odoo:
  image: odoo:18.0
  devMode: true
  adminPassword: admin
  extraAddonsPath: /mnt/extra-addons-ext

postgres:
  enabled: true
  image: postgres:15
  password: odoo

nginx:
  enabled: true

pgadmin:
  enabled: false       # 預設關閉
```

**Local mode**：hostPath 掛載 worktree `src/` → Pod `/mnt/extra-addons/woow_paas_platform`
**CI mode**：自定義 image 包含 addon 程式碼

### Shell Scripts（`scripts/k8s-sandbox-*.sh`）

腳本負責：
1. 偵測 branch 並生成 slug（沿用現有 `setup-worktree-env.sh` 的邏輯）
2. 計算 namespace 名稱（`dev-sandbox-{slug}`）
3. 呼叫 `helm install/upgrade/uninstall`
4. 等待 Pod ready 並顯示存取資訊
5. TTL 管理（annotation 操作）

### Dockerfile.dev

從 `odoo:18.0` 基底，COPY addon 程式碼進去：

```dockerfile
FROM odoo:18.0
COPY src/ /mnt/extra-addons/woow_paas_platform/
COPY extra/extra-addons/ /mnt/extra-addons-ext/
```

### Skill 定義（`.claude/skills/k8s-dev-sandbox.md`）

Claude Code skill 作為入口，解析用戶命令並調用對應的 shell script。

## Implementation Strategy

### Phase 1: Helm Chart + 核心腳本（Task 1-4）
建立可工作的 Helm Chart，實現 `create` / `destroy` / `list` 基本流程。

### Phase 2: 完整 Skill + 進階功能（Task 5-8）
加入 Claude Code skill 定義、TTL 管理、CI/CD mode、測試命令。

### Phase 3: 文件 + 驗證（Task 9-10）
文件化、端到端驗證。

## Task Breakdown Preview

- [ ] **Task 1: Helm Chart 基礎結構** — Chart.yaml, values.yaml, _helpers.tpl, 基本模板（namespace, secrets）
- [ ] **Task 2: 核心服務模板** — Odoo Deployment + Service, PostgreSQL StatefulSet + Service, PVC, ConfigMap (odoo.conf)
- [ ] **Task 3: 網路層模板** — Nginx Deployment + ConfigMap, Ingress, NetworkPolicy, ResourceQuota
- [ ] **Task 4: 建立/銷毀腳本** — k8s-sandbox-create.sh, k8s-sandbox-destroy.sh（含 branch slug 邏輯、helm install/uninstall、Pod readiness 檢查）
- [ ] **Task 5: 列表/狀態/日誌腳本** — k8s-sandbox-list.sh, k8s-sandbox-status.sh, k8s-sandbox-logs.sh
- [ ] **Task 6: TTL 管理** — ttl-cronjob.yaml 模板 + k8s-sandbox-extend.sh（annotation-based TTL）
- [ ] **Task 7: CI/CD 模式** — Dockerfile.dev, values-ci.yaml, create.sh 的 `--image` 參數支援
- [ ] **Task 8: Claude Code Skill 定義** — .claude/skills/k8s-dev-sandbox.md（命令路由 + 參數解析）
- [ ] **Task 9: 測試命令** — k8s-sandbox-test.sh（在 Pod 中執行 Odoo test suite）
- [ ] **Task 10: 文件與驗證** — README 更新、CLAUDE.md 更新、端到端驗收測試

## Dependencies

### 前置條件
- K3s 或 K8s cluster 已就緒並可透過 kubectl 存取
- Helm 3.13+ 已安裝
- 現有 `extra/paas-operator/helm/` 作為 Helm 模式參考

### 內部依賴
- Task 2, 3 依賴 Task 1（chart 基礎）
- Task 4 依賴 Task 2（需要可部署的 chart）
- Task 5, 6 依賴 Task 4（需要已建立的沙盒）
- Task 7 獨立（Dockerfile + CI values）
- Task 8 依賴 Task 4, 5（需要腳本已就緒）
- Task 9 依賴 Task 4
- Task 10 依賴所有其他 Task

### 外部依賴
- Docker registry（僅 CI/CD mode 需要）
- DNS 配置（僅 Ingress 需要，可選）

## Success Criteria (Technical)

| 指標 | 驗收方式 |
|------|---------|
| `./scripts/k8s-sandbox-create.sh` 建立完整沙盒 | 手動在 K3s 上執行，Odoo 可存取 |
| 多沙盒並行 | 同時建立 2 個不同 branch 的沙盒，互不干擾 |
| 本地程式碼掛載 | 修改 `src/` 後 Pod 內即時反映 |
| TTL 自動清理 | 設定短 TTL 後觀察自動刪除 |
| `/k8s-sandbox up` 一鍵完成 | Skill 執行後 < 5 分鐘可用 |
| `destroy` 完全清理 | namespace 和所有資源完全刪除 |

## Estimated Effort

| Phase | Tasks | 預估 |
|-------|-------|------|
| Phase 1: Helm Chart + 核心腳本 | Task 1-4 | 1 週 |
| Phase 2: 完整功能 | Task 5-8 | 1 週 |
| Phase 3: 文件與驗證 | Task 9-10 | 2-3 天 |
| **Total** | 10 Tasks | **~2.5 週** |

Critical path: Task 1 → Task 2 → Task 4 → Task 8

## Tasks Created

- [ ] #121 - Helm Chart 基礎結構 (parallel: false)
- [ ] #123 - 核心服務模板 Odoo + PostgreSQL (parallel: true, depends: #121)
- [ ] #125 - 網路層模板 Nginx + Ingress + NetworkPolicy (parallel: true, depends: #121)
- [ ] #127 - 建立/銷毀腳本 create + destroy (parallel: false, depends: #123)
- [ ] #129 - 列表/狀態/日誌腳本 (parallel: true, depends: #127)
- [ ] #122 - TTL 自動清理管理 (parallel: true, depends: #127)
- [ ] #124 - CI/CD 模式支援 (parallel: true, depends: #121)
- [ ] #126 - Claude Code Skill 定義 (parallel: false, depends: #127, #129)
- [ ] #128 - 測試執行命令 (parallel: true, depends: #127)
- [ ] #130 - 文件與端到端驗證 (parallel: false, depends: all)

Total tasks: 10
Parallel tasks: 6 (#123, #125, #129, #122, #124, #128)
Sequential tasks: 4 (#121, #127, #126, #130)
Estimated total effort: ~42-56 hours
