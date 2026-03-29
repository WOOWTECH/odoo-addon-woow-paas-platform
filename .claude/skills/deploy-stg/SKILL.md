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

觸發 Gitea CI 建置 paas-odoo staging image，等待建置完成後，部署到 K8s dev sandbox。

## 參數

| 參數 | 必填 | 預設值 | 說明 |
|------|------|--------|------|
| `addon_ref` | 否 | 最新 git tag 或 HEAD short SHA | woow_paas_platform 的 git ref（tag 或 branch） |
| `--slug <name>` | 否 | `stg` | Sandbox 識別名稱，namespace = `dev-sandbox-{slug}` |
| `--skip-build` | 否 | - | 跳過 CI 建置，直接用 registry 中已存在的 image 部署 |
| `--image <full>` | 否 | - | 直接指定完整 image（跳過 CI 建置），格式: `registry/repo:tag` |

## 前置條件

- `paas-odoo-ci` repo 已 clone 到本機（預設從 PROJECT_ROOT 推算，或設 `CI_REPO_PATH` 環境變數指定）
- `kubectl` 已連線到目標 K8s cluster
- `helm` 3.13+ 已安裝
- `curl`、`jq`、`git` 已安裝

## 執行流程

### Step 1: 檢查前置條件

```bash
# 檢查必要工具
for cmd in curl jq kubectl helm git; do
  command -v "$cmd" >/dev/null || { echo "Missing: $cmd"; exit 1; }
done

# 檢查 CI repo 存在
CI_REPO_PATH="${CI_REPO_PATH:-$(cd "$PROJECT_ROOT/../../woow-paas-repo/paas-odoo-ci" && pwd)}"
if [ ! -d "$CI_REPO_PATH/.git" ]; then
  echo "Error: CI repo not found at $CI_REPO_PATH"
  exit 1
fi
```

### Step 2: 解析參數

```bash
PROJECT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
cd "$PROJECT_ROOT"

# 若未指定 addon_ref，取最新 git tag；若無 tag 則用 HEAD short SHA
ADDON_REF="${1:-$(git describe --tags --abbrev=0 2>/dev/null || git rev-parse --short HEAD)}"
SLUG="${SLUG:-stg}"

# 產生 image tag（stg- 前綴 + 時間戳）
IMAGE_TAG="stg-$(date -u +'%Y%m%d.%H%M')"
```

### Step 3: 確認參數（顯示給使用者確認）

顯示以下資訊並等待使用者確認後再繼續：

```
Deploy Staging Configuration:
  Addon Ref:  ${ADDON_REF}
  Image Tag:  ${IMAGE_TAG}
  Slug:       ${SLUG}
  Namespace:  dev-sandbox-${SLUG}
  CI Repo:    /Users/eugene/Documents/woow/woow-paas-repo/paas-odoo-ci
  Trigger:    git tag ${IMAGE_TAG} + push
```

### Step 4: 觸發 CI Build（via git tag）

若使用者指定 `--skip-build` 或 `--image`，跳過此步驟。

透過在 `paas-odoo-ci` repo push `stg-*` tag 觸發 Gitea CI workflow：

```bash
CI_REPO_PATH="${CI_REPO_PATH:-$(cd "$PROJECT_ROOT/../../woow-paas-repo/paas-odoo-ci" && pwd)}"

git -C "$CI_REPO_PATH" fetch origin main --quiet
git -C "$CI_REPO_PATH" tag -f "$IMAGE_TAG" origin/main
git -C "$CI_REPO_PATH" push origin "$IMAGE_TAG" --force

echo "CI build triggered: tag ${IMAGE_TAG} pushed"
echo "Monitor: https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/actions"
```

CI workflow 偵測到 `stg-*` tag 後自動 build `paas-odoo-stg` image。

### Step 5: 等待 Image 建置完成

輪詢 Docker Registry（JFrog JCR）檢查 image 是否已推送。每 30 秒檢查一次，最多等待 15 分鐘。

```bash
REGISTRY="https://jcr-prod.woowtech.io"
IMAGE_PATH="woow-paas-docker-local/paas-odoo-stg"
MAX_ATTEMPTS=30
INTERVAL=30

echo "Waiting for image ${IMAGE_PATH}:${IMAGE_TAG} ..."

for i in $(seq 1 $MAX_ATTEMPTS); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
    -u "${JCR_USER:-admin}:${JCR_PASS}" \
    "${REGISTRY}/v2/${IMAGE_PATH}/manifests/${IMAGE_TAG}" \
    -H "Accept: application/vnd.docker.distribution.manifest.v2+json")

  if [ "$HTTP" = "200" ]; then
    echo "Image ready! (attempt ${i}/${MAX_ATTEMPTS})"
    break
  fi

  if [ "$i" = "$MAX_ATTEMPTS" ]; then
    echo "Timeout: image not found after $((MAX_ATTEMPTS * INTERVAL))s"
    echo "Check CI status: ${GITEA_URL}/${REPO}/actions"
    exit 1
  fi

  echo "  Attempt ${i}/${MAX_ATTEMPTS}: not ready (HTTP ${HTTP}), waiting ${INTERVAL}s..."
  sleep $INTERVAL
done
```

> **注意**：若 JFrog JCR registry 需要認證，需設定 `JCR_USER` 和 `JCR_PASS` 環境變數。
> 若 registry 允許匿名讀取 manifest，可移除 `-u` 參數。

### Step 6: 部署到 Sandbox

使用 k8s-dev-sandbox 的 create.sh 部署，傳入完整 image reference。

```bash
FULL_IMAGE="jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:${IMAGE_TAG}"

bash .claude/skills/k8s-dev-sandbox/scripts/create.sh \
  --branch "${SLUG}" \
  --image "${FULL_IMAGE}"
```

這會：
- 建立 namespace `dev-sandbox-${SLUG}`
- 以 CI 模式（非 hostPath）部署 Odoo
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

# 跳過建置，用已存在的 image
/woow:deploy-stg --skip-build --image jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:stg-20260329.1400

# 銷毀 staging sandbox
/k8s-dev-sandbox destroy stg
```

## 疑難排解

| 問題 | 可能原因 | 解決方式 |
|------|----------|----------|
| git push tag 失敗 | CI repo 未 clone 或 remote 設定錯誤 | 確認 `paas-odoo-ci` repo 路徑和 git remote |
| Image 輪詢 timeout | CI build 失敗或時間過長 | 到 Gitea UI 檢查 workflow run 狀態 |
| Registry 查詢 401 | JCR 需認證 | 設定 `JCR_USER` / `JCR_PASS` 環境變數 |
| Helm install 失敗 | kubectl 未連線或權限不足 | 確認 kubeconfig 和 RBAC |
| Pod CrashLoopBackOff | Odoo 啟動失敗 | `kubectl logs -n dev-sandbox-stg <pod>` 檢查日誌 |
