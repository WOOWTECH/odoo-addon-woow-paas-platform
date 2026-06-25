---
name: woow:build-operator
description: Build and publish paas-operator image by creating an operator-v* git tag to trigger CI.
usage: |
  Use when:
  (1) User wants to build/publish a new paas-operator image
  (2) User wants to release a new version of paas-operator
  (3) User says "build operator", "發佈 operator", "operator 發版"

  Invoke with: /woow:build-operator [--skip-push] [--dry-run]
  Keywords: operator, build, publish, release, 發佈, 發版, image
---

# Build & Publish paas-operator

透過建立 `operator-v{YYYYMMDD.HHMM}` git tag 觸發 CI 建置 paas-operator Docker image。

版號使用 UTC 時間戳，永遠遞增，不需手動管理。

## 參數

| 參數 | 必填 | 預設值 | 說明 |
|------|------|--------|------|
| `--skip-push` | 否 | - | 只建立 tag 不 push（用於本機測試） |
| `--dry-run` | 否 | - | 只顯示會做什麼，不實際執行 |

## 前置條件

- 目前在 `main` 或 `develop` branch
- working tree 乾淨（`extra/paas-operator/` 下無未 commit 的變更）
- git remote 已設定
- 本機沒有未 push 的 commit（CI clone remote 程式碼）

## 執行流程

### Step 1: 檢查前置條件

```bash
# 檢查未 push 的 commit
AHEAD=$(git rev-list --count @{upstream}..HEAD 2>/dev/null || echo "0")
if [ "$AHEAD" -gt 0 ]; then
  echo "⚠️  本機有 ${AHEAD} 個未 push 的 commit"
  git log --oneline @{upstream}..HEAD
  # 提示使用者先 push
fi
```

### Step 2: 產生版號

```bash
VERSION=$(date -u +'%Y%m%d.%H%M')
TAG="operator-v${VERSION}"
```

### Step 3: 檢查 tag 是否已存在

```bash
if git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "Tag $TAG already exists, waiting 1 minute..."
  sleep 60
  VERSION=$(date -u +'%Y%m%d.%H%M')
  TAG="operator-v${VERSION}"
fi
```

### Step 4: 確認（顯示給使用者）

```
Build Operator Configuration:
  Version:     ${VERSION}
  Git Tag:     operator-v${VERSION}
  Image Tag:   ${VERSION} + latest
  Branch:      $(git branch --show-current)
  Commit:      $(git rev-parse --short HEAD)
```

### Step 5: 建立 tag 並 push

```bash
git tag "operator-v${VERSION}"
git push origin "operator-v${VERSION}"
```

CI workflow `.gitea/workflows/build-paas-operator.yml` 偵測到 `operator-v*` tag 後自動建置並推送 image。

### Step 6: 輸出後續步驟

```
✅ CI 已觸發：operator-v${VERSION}

後續步驟（手動）：
1. 等 CI 完成（檢查 Gitea Actions）
2. 更新 Fleet values 的 image.tag：
   - paas-app-bundle-cd/paas-operator/values-prod.yaml     → tag: "${VERSION}"
   - paas-app-bundle-cd/paas-operator-stg/values-stg.yaml  → tag: "${VERSION}"
3. Commit + push paas-app-bundle-cd → Fleet 自動同步
```

### Step 7: 完成通知

```bash
say -r 180 "主人，operator ${VERSION} 開始建置了"
```

## 快速指令範例

```bash
# 標準用法（自動產生時間戳版號）
/woow:build-operator

# Dry run（預覽）
/woow:build-operator --dry-run
```

## CI 流程

```
/woow:build-operator
  → 產生版號：20260403.1800
  → git tag operator-v20260403.1800
  → git push origin operator-v20260403.1800
    → .gitea/workflows/build-paas-operator.yml
      → docker build + push paas-operator:20260403.1800 + :latest
```

## Image 命名規則

| 項目 | 格式 | 範例 |
|------|------|------|
| Git tag | `operator-v{YYYYMMDD.HHMM}` | `operator-v20260403.1800` |
| Docker image tag | `{YYYYMMDD.HHMM}` | `20260403.1800` |
| Registry (internal) | `artifactory-jcr-*.jfrog.svc.cluster.local/woow-paas-docker-local/paas-operator` | - |

## 疑難排解

| 問題 | 可能原因 | 解決方式 |
|------|----------|----------|
| Tag already exists | 同一分鐘內重複執行 | 等 1 分鐘後重試 |
| Push rejected | 無 push 權限 | 確認 git remote 權限 |
| CI not triggered | Workflow 未匹配 tag | 確認 tag 格式為 `operator-v*` |
| Working tree dirty | 有未 commit 的變更 | 先 commit 或 stash |
| 未 push 的 commit | CI 用 remote 程式碼 | 先 `git push` 再執行 |
