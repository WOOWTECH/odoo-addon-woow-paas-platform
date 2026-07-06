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
  3. Calls paas-odoo-ci → build-image.yml
  4. paas-odoo-ci clones addon at tag v{version}
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
```
