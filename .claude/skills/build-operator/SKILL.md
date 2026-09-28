---
name: woow:build-operator
description: Build and publish paas-operator image by creating an operator-v* git tag to trigger CI.
when_to_use: 使用者要 build／publish／release paas-operator image（「build operator」「發佈 operator」「operator 發版」）。/woow:build-operator [--skip-push] [--dry-run]
---

# Build & Publish paas-operator

透過建立 `operator-v{YYYYMMDD.HHMM}` git tag 觸發 CI 建置 paas-operator Docker image。

版號使用 UTC 時間戳，永遠遞增，不需手動管理。

> **tag 觸發是有效的**（2026-08-06 全量複驗：遠端每個 `operator-v*` tag 都有對應的 `event=push`、
> `path=…@refs/tags/operator-v<版>` run）。若你讀到「tag 不觸發、必須改用 `workflow_dispatch`」的
> 說法，那是已被推翻的舊結論——連同它的兩個假陰性探針，見
> [`docs/guides/deployment/build-operator-deploy-stg.md`](../../../docs/guides/deployment/build-operator-deploy-stg.md) ①。

## 參數

| 參數 | 必填 | 預設值 | 說明 |
|------|------|--------|------|
| `--skip-push` | 否 | - | 只建立 tag 不 push（用於本機測試） |
| `--dry-run` | 否 | - | 只顯示會做什麼，不實際執行 |

腳本實作：[`scripts/build-operator.sh`](scripts/build-operator.sh)（涵蓋 Step 1-5；Step 6 的驗證仍須自己做）。

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

CI workflow `.gitea/workflows/build-paas-operator.yml` 偵測到 `operator-v*` tag 後自動建置並推送 image
（`Resolve image tag` 從 `GITHUB_REF` 剝掉 `operator-v` 前綴當 image tag）。

⚠ **`git push` 回 0 只代表遠端收到 tag，不代表 run 建立了。** 別在這裡就宣告「CI 已觸發」——做 Step 6。

### Step 6: 驗證 CI run 真的建立（不可省略）

**🔴 不要用 `tea actions runs list` 或 `runs?limit=N` 的第一筆判斷。** 那兩個探針都會給假陰性：
列表按 **run id 遞減**排序而非執行時間，`N` 又是全 repo 所有 workflow 共用的視窗（每個 PR 都會觸發
數條 lint/gate workflow，很快把 build run 擠出去）；而 `tea` 表格的 WORKFLOW 欄印的是 **commit 訊息
不是 workflow 檔名**、BRANCH 欄對 tag run 是**空的**，看起來與「別人 merge develop 觸發的 run」一模一樣。
**唯一有鑑別力的是 API 的 `path` 欄位。**

```bash
tea api -r odoo-addons/woow_paas_platform "repos/{owner}/{repo}/actions/runs?limit=50" \
| python3 -c "
import json,sys
want = 'refs/tags/operator-v${VERSION}'
hits = [r for r in json.load(sys.stdin)['workflow_runs'] if want in (r.get('path') or '')]
for r in hits:
    print(r['id'], r['event'], r['status'], r['path'])
if not hits:
    print('尚未在本頁出現 —— 翻下一頁（&page=2）或稍候重查，不要立刻重推 tag / 補 dispatch')
"
```

拿到 `<RUN_ID>` 後**再往下查 job 層**看成敗（run 層 `conclusion` 在列表回應中常為 `null`，
`tea actions runs list` 的 STATUS 欄對失敗的 run 一樣顯示 `completed`）：

```bash
tea api -r odoo-addons/woow_paas_platform "repos/{owner}/{repo}/actions/runs/<RUN_ID>/jobs"
# 逐 job 看 conclusion == success

tea actions runs logs <RUN_ID> --repo odoo-addons/woow_paas_platform | grep 'digest:'
# 成功會印：${VERSION}: digest: sha256:… size: …
```

### Step 7: 輸出後續步驟

```
✅ CI run <RUN_ID> 已建立並 success：operator-v${VERSION}
   image digest: sha256:…

後續步驟（手動）：
1. 更新 Fleet values 的 image.tag：
   - paas-app-bundle-cd/paas-operator/values-prod.yaml     → tag: "${VERSION}"
   - paas-app-bundle-cd/paas-operator-stg/values-stg.yaml  → tag: "${VERSION}"
2. Commit + push paas-app-bundle-cd 的 main branch → Fleet 自動同步
3. 驗 rollout（deployment image == 新 tag、pod imageID digest == 上面那顆）
```

Fleet 落點細節（GitRepo 名稱、watch 的 branch、push 權限陷阱）與 rollout / F6 env guard 驗證法，
見 [`docs/guides/deployment/build-operator-deploy-stg.md`](../../../docs/guides/deployment/build-operator-deploy-stg.md) ③④。

### Step 8: 完成通知

```bash
say -r 180 "主人，operator ${VERSION} 建好了"
```

## 全 `CACHED` 不等於空跑

build log 每一層印 `#N CACHED`、push 時每一層印 `Layer already exists`——**看起來就像什麼都沒做，
但那是正常的**：act-runner layer cache 熱，且同一份程式碼先前建過（同一顆 commit 重 build 時整條
命中，數秒完成）。**不要據此判定 build 失敗或拿到舊碼，更不要因此重推 tag 或補一次 dispatch。**

要分辨「真的建到我要的那份碼」，用 digest，不要用 build 秒數或 CACHED 行數：

| 想確認的事 | 怎麼看 |
|---|---|
| registry 上的 `<tag>` 就是這次 build 的產物 | build log 的 `<tag>: digest: sha256:…` 即權威 |
| 跑著的 pod 已經換成新 image | `kubectl get pod -o jsonpath='{..imageID}'` 的 digest **等於**上面那顆 ⇒ 已生效；不等於 ⇒ 還沒 rollout |
| 這顆 image 裡的程式碼是哪一版 | 看 **tag → commit**（`git rev-list -n1 operator-v<版>`）。**digest 不同 ⇏ 程式碼不同**，反推會得到錯誤結論 |

必要時用該 image 起一顆一次性 pod 直接讀檔，那是最硬的證據。

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
    → .gitea/workflows/build-paas-operator.yml（push trigger，tags: operator-v*）
      → docker build + push paas-operator:20260403.1800 + :latest
  → 【驗證】API 查 path 含 refs/tags/operator-v20260403.1800 的 run → job conclusion → log digest
  → 【部署】paas-app-bundle-cd 改 image.tag → Fleet 同步 → 驗 pod imageID digest
```

想對**同一顆 commit** 重 build 又不想再打一個 tag 時，才用 `workflow_dispatch`（補救手段，非必要路徑）：

```bash
tea actions workflows dispatch build-paas-operator.yml --repo odoo-addons/woow_paas_platform \
  --login <tea login 名稱> --ref develop --input tag=<YYYYMMDD.HHMM>
```

`inputs.tag` 在 `Resolve image tag` 中優先於 git ref，冪等。要把 dispatch 釘在 tag 上時，`--ref`
必須寫全名 `refs/tags/operator-v<版>`（只給 tag 名會被拒：ref doesn't exist）。

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
| 「CI 好像沒觸發」 | **多半是查法錯，不是真的沒觸發**（歷史上兩次誤判都是這樣） | 照 Step 6 用 API `path` 欄位查；**先確認查法對了再考慮重推 tag**，否則會做出多餘的第二次 build |
| run 查不到，但 tag 確實在遠端 | `runs?limit=N` 視窗太小／不是時間序 | 翻頁（`&page=2…`）或按 run id 直查 `/actions/runs/{id}`；別用 `tea actions runs list` 的第一筆 |
| run 看起來是「別人的 merge」 | `display_title`／`tea` 的 WORKFLOW 欄印的是 commit 訊息，tag 與該 commit 的 branch run 標題必然相同 | 看 API 的 `path` 欄位，它明寫 `@refs/tags/operator-v…` |
| build 全 `CACHED`、幾秒就結束 | act-runner cache 熱＋同一份碼建過，**正常** | 比對 log 的 `digest:` 與 pod imageID digest，別看秒數 |
| Working tree dirty | 有未 commit 的變更 | 先 commit 或 stash |
| 未 push 的 commit | CI 用 remote 程式碼 | 先 `git push` 再執行 |
