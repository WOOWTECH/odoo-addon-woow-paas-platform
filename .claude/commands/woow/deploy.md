---
allowed-tools: Bash, Read, Grep
---

# Deploy: Tag & Trigger paas-odoo Build

This command reads the version from `src/__manifest__.py`, creates a git tag, and pushes it to trigger the paas-odoo image build pipeline.

## 前置條件（一次性設定）

`woow_paas_platform` repo 的 Gitea Settings → Actions → Secrets 需設定 `CI_GITEA_TOKEN`，該 token 的擁有者須同時具備 `odoo-addons` 和 `woow-paas` org 的 Write 權限（scope: `repository: Read and Write`）——cross-repo dispatch 需要。CI 觸發失敗時先檢查這裡。

> **Note**: Git tag 直接使用 `__manifest__.py` 的完整版號加 `v` 前綴，不做拆解。paas-odoo image tag 使用時間戳（`YYYYMMDD.HHmm`），因為它是多個 addon 的組合打包，不應綁定單一 addon 的版本。

## Steps

### 0. ⛳ 先把版號 bump 上 develop —— 一張 `release/*` 分支的發版 PR（#691）

🔴 **#691 起一般 PR 不動 manifest 版號**，版號只由兩類 PR 帶：帶 migration 的 PR，以及
**發版 PR**。所以打 tag 之前，`origin/develop` 的版號必須先由一張發版 PR 升上去；
`develop` 有分支保護，不能直接 push。

```bash
# 1) 取號：嚴格大於當下 develop（元組比較，不是字串）
git fetch --no-tags origin develop
CURRENT=$(git show origin/develop:src/__manifest__.py | grep -oE '"version": *"[^"]+"' | grep -oE '[0-9][0-9.]*')
echo "develop 現值 = ${CURRENT}"
# NEW = CURRENT 末段 +1；⚠ 若此刻有帶 migration 的 PR 在飛且已取號，要避開它
#      （撞號時後 merge 的那張會被 manifest-version-gate 判紅，可見不靜默）

# 2) 分支名**必須** release/<新版號> —— gate 就是靠這個前綴認出發版 PR
git switch -c "release/${NEW}" origin/develop
#    改 src/__manifest__.py 的 version 成 ${NEW}
git commit -am "release: ${NEW}"
git push -u origin "release/${NEW}"
tea pr create --title "release: ${NEW}" --base develop   # description 用 zh-tw
```

**為什麼是分支名而不是標題前綴**：本 repo 的 PR 標題一律被冠上
「⛔ 勿代 merge｜待 Eugene 覆核 — 」前綴，`title.startswith("release:")` 對真正的發版 PR
是假陰性；放寬成「標題含 `release:`」又讓任何人打幾個字就能豁免規則 A。分支名不被裝飾，
是唯一結構性的訊號。判準與訊息見 [`scripts/ci/manifest-version-gate.py`](../../../scripts/ci/manifest-version-gate.py)。

⛔ **發版 PR 一樣不代 merge**（同下方 Step 6 的原則）。merge 進 develop、且本地
`git pull` 拿到那個版號之後，才進 Step 1。

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
❌ Tag v{version} already exists.
   → develop 的版號還停在已發過的值。回 Step 0 開一張 release/<新版號> 的發版 PR
     把版號升上去、merge 之後再回來（#691：不要在這裡直接改 manifest 就打 tag，
     那個值不在 develop 上，image 會烤到與 develop 不一致的碼）。
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
  4. paas-odoo-ci 驗 addon_ref → 開 prod approval issue → ⏸ 停在這裡，尚未 build
  5. 授權者在該 issue 留言 /approve → 觸發新的一次 run（見 Step 6）
  6. clone addon at ref v{version} → build → 版號驗證 → push 到 JCR

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
  2. paas-odoo-ci phase 1: 驗 addon_ref → 開 approval issue → 結束（⚠ 還沒 build）
  3. ⏸ 等授權者留言 /approve 放行 —— 見 Step 6
  4. paas-odoo-ci phase 2: clone → build → 版號驗證 → push
  5. Image pushed to JCR: woow-paas-docker-local/paas-odoo:{timestamp}

🔍 Check build status:
  - woow_paas_platform CI: https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/actions
  - paas-odoo-ci CI:       https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/actions

✅ Build 驗收（CI 綠燈不等於 image 裝對版本）：
  ⚠ 這三行只出現在 **phase 2**（核可後那次 run）的 log —— phase 1 的 log 裡沒有，
    在 phase 1 找不到不是 CI 壞掉，是還沒 build（見 Step 6）。
  paas-odoo-ci build log 必須出現這三行，且**每一行都對上本次版號**：
    Cloning woow_paas_platform at ref: v{version}          ← 看 at ref:，不是 at HEAD
    Cloned addon version: {version} (from ref: ...)        ← source 端
    Image addon version: {version} (matches clone, ...)    ← image 端（CI 已 fail-closed 比對）
  ⚠ matches clone 只證明 image == clone，**不證明 clone 是你要的那個 ref**——
    addon_ref 填錯時三行會一致地顯示錯的版號、gate 全綠，只有 at ref: 露餡。
    fail-closed 只擋「留空」，擋不了「填錯」。
  ⚠ 錨到的只是版號這一層。詳見 SOP §5.1.4「已知極限」。
```

### 6. ⏸ Prod build 人工核可 gate（push tag 後**不會**自動 build）

push tag 只跑完 **phase 1**：paas-odoo-ci 驗過 `addon_ref` 後，開一張
`[prod-approval:build-image.yml] Build .../paas-odoo:{timestamp}` issue 就**結束**——
job 立刻完成、不佔 runner、**image 還沒 build**。要有人放行才會觸發 **phase 2** 真正 build。

> ⚠ **phase 1 的 success ≠ 發版完成。** 兩個 phase 都是 `build-image.yml` 的 run、
> 都顯示綠燈，只看「paas-odoo-ci 有沒有剛跑完的 build-image」必然誤判。

**放行方式**：`PROD_APPROVERS`（paas-odoo-ci 的 repo variable）清單內的人，在該 issue 留言
**完整且僅有** `/approve`（trim 前後空白後精確相等，夾在句子裡不算）。中止用 `/reject`。

- `ALLOW_SELF_APPROVE`（repo variable）為 `true` 時允許 tag pusher 自核；預設 `false` 走兩人四眼
- **逾時 180 分鐘**，逾時後由 `close-expired-approvals.yml`（每 15 分鐘 schedule）自動收掉；
  逾時就得改走 Step 7 的 `workflow_dispatch` 重來
- issue body 帶 `APPROVAL_SEAL`（HMAC），phase 2 會校驗完整性 ⇒ 不要手改 body

> ⛔ **agent 不代簽。** prod 人工核可與 CD PR merge 同級，屬人類職責（同
> [`prod-release-sop.md`](../../../docs/guides/deployment/prod-release-sop.md) §5.1.1 的原則）。
> **即使 `ALLOW_SELF_APPROVE=true`、且 agent 手上的 CLI 憑證正好是授權者本人**，也一樣不代簽——
> 技術上做得到不等於被授權。agent 跑到這裡就停手回報，等人放行後再接續盯 phase 2。

**哪些路徑會被擋**：

| 路徑 | gate | agent 自動化 |
|---|:---:|---|
| push `stg-*` tag | ❌ 跳過 | ✅ 完全自動 |
| `workflow_dispatch` 帶 `image_name=paas-odoo-stg` | ❌ 跳過 | ✅ 完全自動 |
| push `v*` tag（**本命令這條**） | ✅ 擋 | ⚠️ 需人工 `/approve` |
| `workflow_dispatch` 帶 `image_name=paas-odoo`／未指定 | ✅ 擋 | ⚠️ 需人工 `/approve` |

**放行後怎麼確認 phase 2 是真的 build**：放行時 bot 會留言，而**那則 bot 留言本身也是
`issue_comment`**，會再觸發一個驗不過即結束的 no-op run —— 它同樣是 `build-image.yml`、
同樣 success，在 run 列表上與真 build 長得一模一樣。判別看 **step 級 conclusion**
（`Fetch custom addons`／`Build and tag image`／`Verify image addon version`／`Push image`
是否 success；no-op 這些會是 `skipped`），不是 run 級 status。

```bash
# 列 step 級結果（run index 取自 run URL 尾碼）
tea api "repos/woow-paas/paas-odoo-ci/actions/runs/{run}/jobs"
```

> gate 由 paas-odoo-ci #66 引入（2026-08-07），2026-08-16 發版 v18.0.1.0.147 首次完整實測。

### 7. 若 CI 失敗需要重跑

**這條路徑上的 tag 已經存在，重推 tag 不會重新觸發**——改用 `workflow_dispatch` 手動跑 paas-odoo-ci 的 `build-image.yml`，且**必須自己帶 `addon_ref`**（自動路徑由 `trigger-odoo-build.yml` 代填，人工路徑沒有人代填）：

- `addon_ref` = `v{version}`（本次發版 tag）
- `tag` = 新的 `YYYYMMDD.HHMM`

⚠ **這條人工路徑一樣會撞到 Step 6 的核可 gate**（`image_name` 未指定即視為 prod）——
dispatch 後只會再開一張新的 approval issue，**仍須有人 `/approve` 才會 build**。

留空會被 paas-odoo-ci 擋下（fail-closed，`exit 1`，issue #46）。**這是設計行為，不是 CI 壞掉**——補上 `addon_ref` 重跑即可。

> 為什麼要擋：留空時 CI 會 clone 本 repo 預設分支（`develop`）的**當下 HEAD**，烤出不可重現、含未發版 commit 的 image，而 CI 全綠、image tag 與 digest 都是新的，完全沒有訊號。完整說明見 [`docs/guides/deployment/prod-release-sop.md`](../../../docs/guides/deployment/prod-release-sop.md) §5.1.4。
