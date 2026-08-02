---
allowed-tools: Bash, Read, Grep
---

# Deploy: Tag & Trigger paas-odoo Build

This command reads the version from `src/__manifest__.py`, creates a git tag, and pushes it to trigger the paas-odoo image build pipeline.

## 前置條件（一次性設定）

`woow_paas_platform` repo 的 Gitea Settings → Actions → Secrets 需設定 `CI_GITEA_TOKEN`，該 token 的擁有者須同時具備 `odoo-addons` 和 `woow-paas` org 的 Write 權限（scope: `repository: Read and Write`）——cross-repo dispatch 需要。CI 觸發失敗時先檢查這裡。

> **Note**: Git tag 直接使用 `__manifest__.py` 的完整版號加 `v` 前綴，不做拆解。paas-odoo image tag 使用時間戳（`YYYYMMDD.HHmm`），因為它是多個 addon 的組合打包，不應綁定單一 addon 的版本。

## Steps

### 1. Extract Version

- Read `src/__manifest__.py` and extract the `version` value (e.g., `18.0.1.0.5`)
- Construct the tag name: `v{version}` (e.g., `v18.0.1.0.5`)

### 2. Pre-flight Checks

Run ALL checks below. If any fail, stop and report.

**Check tag not already exists:**

```bash
# Local
git tag -l "v{version}"

# Remote
git ls-remote --tags origin "refs/tags/v{version}"
```

If tag already exists (either local or remote), abort:

```
❌ Tag v{version} already exists. Bump the version in src/__manifest__.py first.
```

**Check working tree is clean:**

```bash
git status --porcelain
```

If there are uncommitted changes, abort:

```
❌ Working tree is not clean. Commit or stash your changes first.
```

**Check current branch:**

```bash
git branch --show-current
```

Display the current branch name. Warn (but do not block) if not on `main` or `develop`:

```
⚠️ Current branch is {branch}, not main/develop. Proceed? (yes/no)
```

### 3. Confirm with User

Display a summary and ask for confirmation before proceeding:

```
📦 Release Summary
─────────────────────────────
  Addon version : {version}
  Git tag       : v{version}
  Branch        : {current_branch}
  Latest commit : {short_sha} {commit_subject}
─────────────────────────────

🔗 Trigger Flow:
  1. Push tag v{version} to origin
  2. Gitea workflow trigger-odoo-build.yml fires
  3. Calls paas-odoo-ci → build-image.yml (addon_ref=v{version})
  4. paas-odoo-ci clones addon at ref v{version}
  5. Builds & pushes paas-odoo:{timestamp} image to JCR

Proceed with release? (yes/no)
```

Only proceed with explicit 'yes'.

### 4. Create & Push Tag

```bash
git tag v{version}
git push origin v{version}
```

### 5. Output Result

```
✅ Tag v{version} pushed to origin

📋 What happens next:
  1. Gitea CI: trigger-odoo-build.yml → dispatches paas-odoo-ci
  2. paas-odoo-ci: builds Odoo image with this addon version
  3. Image pushed to JCR: woow-paas-docker-local/paas-odoo:{timestamp}

🔍 Check build status:
  - woow_paas_platform CI: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/actions
  - paas-odoo-ci CI:       https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/actions

✅ Build 驗收（CI 綠燈不等於 image 裝對版本）：
  paas-odoo-ci build log 必須出現這三行，且**每一行都對上本次版號**：
    Cloning woow_paas_platform at ref: v{version}          ← 看 at ref:，不是 at HEAD
    Cloned addon version: {version} (from ref: ...)        ← source 端
    Image addon version: {version} (matches clone, ...)    ← image 端（CI 已 fail-closed 比對）
  ⚠ matches clone 只證明 image == clone，**不證明 clone 是你要的那個 ref**——
    addon_ref 填錯時三行會一致地顯示錯的版號、gate 全綠，只有 at ref: 露餡。
    fail-closed 只擋「留空」，擋不了「填錯」。
  ⚠ 錨到的只是版號這一層。詳見 SOP §5.1.4「已知極限」。
```

### 6. 若 CI 失敗需要重跑

**這條路徑上的 tag 已經存在，重推 tag 不會重新觸發**——改用 `workflow_dispatch` 手動跑 paas-odoo-ci 的 `build-image.yml`，且**必須自己帶 `addon_ref`**（自動路徑由 `trigger-odoo-build.yml` 代填，人工路徑沒有人代填）：

- `addon_ref` = `v{version}`（本次發版 tag）
- `tag` = 新的 `YYYYMMDD.HHMM`

留空會被 paas-odoo-ci 擋下（fail-closed，`exit 1`，issue #46）。**這是設計行為，不是 CI 壞掉**——補上 `addon_ref` 重跑即可。

> 為什麼要擋：留空時 CI 會 clone 本 repo 預設分支（`develop`）的**當下 HEAD**，烤出不可重現、含未發版 commit 的 image，而 CI 全綠、image tag 與 digest 都是新的，完全沒有訊號。完整說明見 [`docs/guides/deployment/prod-release-sop.md`](../../../docs/guides/deployment/prod-release-sop.md) §5.1.4。
