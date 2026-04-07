---
name: woow:deploy-stg
description: Deploy a staging paas-odoo instance. Triggers CI to build a staging image, waits for completion, then deploys to a K8s dev sandbox.
usage: |
  Use when:
  (1) User wants to deploy a staging/STG environment for paas-odoo
  (2) User wants to trigger a CI build and deploy to sandbox
  (3) User says "deploy stg", "staging", "部署 STG", "建 staging"

  Invoke with: /woow:deploy-stg [addon_ref] [--slug <name>] [--skip-build] [--image <full-image>]
  Keywords: deploy, stg, staging, CI, build, sandbox, 部署, 測試環境
---

# Deploy Staging (paas-odoo)

透過 `tea` CLI 建立 tag 觸發 CI 建置 paas-odoo staging image，輪詢 CI 狀態等待建置完成後，部署到 K8s dev sandbox。

## 參數

| 參數 | 必填 | 預設值 | 說明 |
|------|------|--------|------|
| `addon_ref` | 否 | 最新 git tag 或 HEAD short SHA | woow_paas_platform 的 git ref（tag 或 branch） |
| `--slug <name>` | 否 | `stg` | Sandbox 識別名稱，namespace = `dev-sandbox-{slug}` |
| `--skip-build` | 否 | - | 跳過建立 tag，僅輪詢已存在 tag 的 CI 狀態 |
| `--image <full>` | 否 | - | 直接指定完整 image（跳過 CI 建置），格式: `registry/repo:tag` |
| `--ingress` | 否 | - | 啟用 Ingress（`{slug}.woowtech.io`） |

## 前置條件

- `tea` CLI 已安裝並設定 login（`tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>`）
- `kubectl` 已連線到目標 K8s cluster
- `helm` 3.13+ 已安裝
- `jq`、`git` 已安裝

**設定 tea login：**

```bash
# 產生 Gitea token: Gitea UI → Settings → Applications → Generate New Token
# scope: repo (讀寫 repo), write:packages (push image)
tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>
```

## 執行流程

### Step 1: 檢查前置條件

```bash
# 檢查必要工具
for cmd in tea jq kubectl helm git; do
  command -v "$cmd" >/dev/null || { echo "Missing: $cmd"; exit 1; }
done

# 檢查 tea login
if ! tea login list &>/dev/null; then
  echo "Error: No tea login configured"
  echo "Run: tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>"
  exit 1
fi
```

### Step 1.5: 檢查未 push 的 commit

CI 從 remote clone 程式碼，本機未 push 的 commit 不會被包含在 image 中。必須在觸發 CI 前檢查。

```bash
# 檢查當前 branch 是否有未 push 的 commit
AHEAD=$(git rev-list --count @{upstream}..HEAD 2>/dev/null || echo "0")
if [ "$AHEAD" -gt 0 ]; then
  echo "⚠️  本機有 ${AHEAD} 個未 push 的 commit："
  git log --oneline @{upstream}..HEAD
  echo ""
  echo "CI 會 clone remote 程式碼，這些 commit 不會被包含在 image 中。"
  # 提示使用者先 push，或由 Claude 自動 push 後繼續
fi
```

### Step 2: 解析參數

```bash
PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$PROJECT_ROOT"

# 若未指定 addon_ref，取最新 git tag；若無 tag 則用 HEAD short SHA
ADDON_REF="${1:-$(git describe --tags --abbrev=0 2>/dev/null || git rev-parse --short HEAD)}"
SLUG="${SLUG:-stg}"

# Git tag（觸發 CI 用，帶 stg- 前綴）
GIT_TAG="stg-$(date -u +'%Y%m%d.%H%M')"

# Docker image tag（CI strip stg- 後的結果）
DOCKER_TAG="${GIT_TAG#stg-}"    # → YYYYMMDD.HHMM
```

### Step 3: 確認參數（顯示給使用者確認）

顯示以下資訊並等待使用者確認後再繼續：

```
Deploy Staging Configuration:
  Addon Ref:    ${ADDON_REF}
  Git Tag:      ${GIT_TAG}
  Docker Tag:   ${DOCKER_TAG}
  Slug:         ${SLUG}
  Namespace:    dev-sandbox-${SLUG}
  Trigger:      Gitea API create tag ${GIT_TAG}
```

### Step 4: 觸發 CI Build（via tea CLI）

若使用者指定 `--skip-build` 或 `--image`，跳過此步驟。

透過 `tea api` 在 `paas-odoo-ci` repo 建立 `stg-*` tag 觸發 CI workflow：

```bash
CI_REPO="woow-paas/paas-odoo-ci"

# 取得 main branch 最新 commit SHA
MAIN_SHA=$(tea api -r "${CI_REPO}" "repos/{owner}/{repo}/branches/main" | jq -r '.commit.id')

# 建立 tag
tea api -X POST -r "${CI_REPO}" "repos/{owner}/{repo}/tags" \
  -f "tag_name=${GIT_TAG}" -f "target=${MAIN_SHA}"

echo "CI build triggered: tag ${GIT_TAG} created"
echo "Monitor: https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/actions"
```

CI workflow 偵測到 `stg-*` tag 後自動 build `paas-odoo-stg` image。

### Step 5: 等待 CI 完成（via tea CLI）

輪詢 Gitea Actions API 檢查 CI 任務狀態。每 30 秒檢查一次，最多等待 15 分鐘。

```bash
# 查詢 CI 任務狀態（篩選 head_branch 匹配 GIT_TAG）
STATUSES=$(tea api -r "${CI_REPO}" "repos/{owner}/{repo}/actions/tasks?limit=10" | \
  jq -r ".workflow_runs[] | select(.head_branch == \"${GIT_TAG}\") | .status")

# 判斷邏輯：
# - success      → 所有 task 完成，繼續部署
# - failure      → 印錯誤訊息並退出
# - cancelled    → 印錯誤訊息並退出
# - waiting/running/blocked → 繼續等待
# - (空)         → CI 尚未開始，繼續等待
```

### Step 6: 部署到 Sandbox

使用 k8s-dev-sandbox 的 create.sh 部署，傳入完整 image reference。

```bash
FULL_IMAGE="jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:${DOCKER_TAG}"

bash .claude/skills/k8s-dev-sandbox/scripts/create.sh \
  --branch "${SLUG}" \
  --image "${FULL_IMAGE}" \
  --mode stg
```

這會：
- 建立 namespace `dev-sandbox-${SLUG}`
- 以 STG 模式部署 Odoo（addons 路徑：`/opt/bitnami/odoo/custom-addons/`）
- 套用 NetworkPolicy 和 ResourceQuota

### Step 7: 驗證部署

```bash
NAMESPACE="dev-sandbox-${SLUG}"

echo "Checking deployment status..."
kubectl get pods -n "$NAMESPACE"
kubectl wait --for=condition=ready pod -l app=odoo -n "$NAMESPACE" --timeout=300s

echo "Staging deployed successfully!"
echo "  Namespace: ${NAMESPACE}"
echo "  Image:     ${FULL_IMAGE}"
```

### Step 8: 完成通知

```bash
say -r 180 "主人，staging 環境部署完成了"
```

## 快速指令範例

```bash
# 使用最新 tag 部署（最常見）
/woow:deploy-stg

# 指定 addon ref
/woow:deploy-stg v18.0.1.0.5

# 自訂 slug（namespace 為 dev-sandbox-stg-test）
/woow:deploy-stg --slug stg-test

# 跳過建置，用已存在的 image（docker tag 格式）
/woow:deploy-stg --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:20260329.1400

# 跳過建立 tag，輪詢已存在 git tag 的 CI 狀態（git tag 格式）
/woow:deploy-stg --skip-build --tag stg-20260329.1400

# 銷毀 staging sandbox
/k8s-dev-sandbox destroy stg
```

## 疑難排解

| 問題 | 可能原因 | 解決方式 |
|------|----------|----------|
| `No tea login configured` | 未設定 tea login | `tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>` |
| 建立 tag 失敗（tea api 回傳錯誤） | Token 權限不足或 repo 不存在 | 確認 token 有 `repo` scope，確認 CI_REPO 路徑正確 |
| Tag already exists | 同名 tag 已存在 | 使用 `--tag` 指定不同名稱，或手動到 Gitea 刪除舊 tag |
| CI run not found（持續等待） | CI workflow 未被觸發 | 到 Gitea UI 檢查 workflow 設定是否正確匹配 `stg-*` tag |
| CI status: failure | CI build 失敗 | 到 Gitea UI 檢查 workflow run 日誌 |
| CI 輪詢 timeout | CI build 時間過長 | 使用 `--timeout` 增加等待時間，或到 Gitea UI 檢查 |
| Helm install 失敗 | kubectl 未連線或權限不足 | 確認 kubeconfig 和 RBAC |
| Pod CrashLoopBackOff | Odoo 啟動失敗 | `kubectl logs -n dev-sandbox-stg <pod>` 檢查日誌 |
