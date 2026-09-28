---
name: woow:deploy-stg
description: Deploy a staging paas-odoo instance. Triggers CI to build a staging image, waits for completion, then deploys to a K8s dev sandbox.
when_to_use: 使用者要部署／建置 staging paas-odoo（「deploy stg」「部署 STG」「建 staging」）。/woow:deploy-stg [addon_ref] [--slug <name>] [--skip-build] [--image <full-image>]
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
| `--post-check-timeout <s>` | 否 | `300` | addon 版號 post-check 等待 DB 收斂的秒數（見 Step 8） |
| `--context <name>` | 否 | `woow-k3s` | kubectl context。**必須在 `.sandbox.yml` 的 `allowed_kube_contexts_stg` 內**（見下） |

### kubectl context 守衛（#32）

1. **本腳本不改動全域 kubectl context**：切換走一份只含 `current-context` 的
   私有 KUBECONFIG overlay，`~/.kube/config` 全程不動。
2. **`--context` 的值受值域白名單管制**：允許值
   來自 repo 根 `.sandbox.yml` 的 `allowed_kube_contexts_stg`（目前 `[woow-k3s]`）。
   可用性檢查（存在於 kubeconfig、連得上）驗不出「這個 context 是不是這次任務該用的
   那一個」—— `docker-desktop` 全過，卻是 stg 部署最不該打的叢集（#722）。

真的要打別的叢集：改 `.sandbox.yml`（會過 review），或 `SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1`
明示覆寫（會大聲警告）。實作在 `.claude/skills/lib/kube-context.sh`，測試
`.claude/skills/lib/kube-context_test.sh`。

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

**Agent-runtime（test-echo skill / agent pod）所需 env（選用，但要用就必帶）：**

若 stg 需要在任務 chat 內 spawn agent-runtime pod（執行 skill）並讓 `agent_session.state` 正確同步，**只需供值一個 env**：`AGENT_RUNTIME_NAMESPACE_SECRET`。它是 Odoo 端專屬、無法從 operator 取，因此同時當作「啟用 agent-runtime」的開關。

供值兩種方式（擇一）：

```bash
# 方式 1（推薦，一次性）：onboard 進 user-scope profile —— 之後任何 checkout / worktree
# 跑 deploy-stg 都自動帶值（create.sh 載入 ~/.sandbox/{project-key}/config.env）
/woow-dev:k8s-dev-sandbox onboard --repo "$(git rev-parse --show-toplevel)" \
  --set AGENT_RUNTIME_NAMESPACE_SECRET=<odoo 端固定值>

# 方式 2（每次）：export 後再跑 deploy-stg
# （未供值 → 整個 agent-runtime 區塊跳過，legacy AI chat 不受影響）
export AGENT_RUNTIME_NAMESPACE_SECRET=<odoo 端固定值>
```

`JCR_USER` / `JCR_PASS`（registry pull 憑證）同理可 onboard 進 profile。

AUTH 與 WEBHOOK 兩把 secret **必須與 operator byte-match**，因此 `create.sh` stg mode 會在部署前**自動從 operator 的 canonical secret**（`paas-stg-system/paas-operator-stg-agent-runtime-auth`，keys `current` / `webhook-current`）讀取注入 —— 部署者不用記、也不會與 operator 漂移（漂移 = skill-bundle / state-update webhook 401 → `agent_session.state` 卡 `absent`）。需覆寫時才 export：

```bash
# 罕用：只有要刻意用與 operator 不同的值時才設（否則留空讓它自動對齊）
export AGENT_RUNTIME_AUTH_SECRET_CURRENT=<override>   # 預設自動 = operator current
export AGENT_RUNTIME_WEBHOOK_SECRET=<override>          # 預設自動 = operator webhook-current
```

> 設了 `AGENT_RUNTIME_NAMESPACE_SECRET` 但 operator secret 抓不到 auth/webhook 時，`create.sh` 會在**任何 helm 操作前 fail-fast**（不會半套部署出 401 的 state-sync）。完全不設 namespace secret → agent-runtime 區塊跳過、legacy AI chat 正常。
> 對應 NetworkPolicy（`networkPolicy.allowAgentRuntimePods=true`，開放 odoo ↔ `paas-ws-*-pa-*` pod 雙向流量）已在 `create.sh` stg mode 寫死，無需手動設定。

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

# 真網路 auth probe：tea login list 只讀本機設定檔，token 過期／CF Access 攔截時照樣綠。
# CF Access 依出口 IP 決定放行與否，probe 不能靠偵測 CF 頁，只能正向驗 JSON 的 login 欄位。
if ! tea api /api/v1/user 2>/dev/null | jq -er '.login' >/dev/null; then
  echo "Error: git-prod auth probe failed（token 過期或 CF Access 攔截）"
  echo "人工處理後再跑；分流表見 docs/guides/development/tea-cli-write-gotchas.md"
  exit 1
fi
```

### Step 1.5: 本機 checkout vs 這次要部署的內容（#723）

`deploy-stg.sh` 在 Prerequisites 之後自動比對 `@{upstream}...HEAD` 的**兩個方向**
（先 `git fetch --quiet origin`，再 `git rev-list --left-right --count`），不需手動執行：

- **領先 > 0** → 警告未 push 的 commit 不會進 image（列最新 10 個；超過會明說截了幾個）
- **落後 > 0** → 印落後數與**兩邊的 manifest 版號**。remote 版號一律走
  `git show <upstream>:src/__manifest__.py`，**不讀 working tree**
- **落後不阻擋部署**：CI 吃 remote，image 是對的，壞的只是部署者的預期——別拿本機版號
  推論 post-check 會是什麼結果。這是資訊不是錯誤，做成 gate 只會被繞過
- `--image` 覆寫時不比版號，改講「部署內容由那顆 image 決定，與本機 checkout 無關」
- upstream 不是 `origin/develop` 時額外點出「stg build 一律烤 develop」

為什麼要兩個方向：單向的 `git rev-list --count @{upstream}..HEAD` 只算得到領先，本機比
remote 舊時 `AHEAD=0`、整道檢查全綠、零訊號——部署者會以為「我看到的 code ＝ 部上去的 code」。

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

> ⚠️ **tag-push 觸發的 run 可能完全不出現在 runs / tasks API**（實證：輪詢多次皆空，
> build 其實 2 分鐘內就完成並 push image）。**API 查無 ≠ 未觸發，嚴禁據此重推 tag**
> （會 triple build）。輪詢多次皆空時改看 ground truth：
>
> ```bash
> # runner 實跑什麼＋是否 Job succeeded（決定性信號）
> kubectl --context woow-k3s -n gitea logs act-runner-runner-0 -c runner --tail=20
> # 或 grep image push 實證
> kubectl --context woow-k3s -n gitea logs act-runner-runner-0 -c runner --tail=400 | grep "paas-odoo-stg:${DOCKER_TAG}"
> ```
>
> 另注意：runner 全叢集僅 1 個 slot，若有 prod build 卡 approval gate 會佔住 runner
> 讓 stg build 長時間 queued（paas-odoo-ci#30）——同樣別重推，先看 runner log 在忙什麼。

> 🔴 **agent harness 直接跑完整 `deploy-stg.sh`（不帶 `--image`，讓它自己建 tag ＋
> 輪詢 CI）常在 `exit 5` 結束、且極難正確診斷**（實測白跑近半小時）。三個誤診特徵缺一都會判錯：
>
> 1. `deploy-stg.sh` 裡 **grep 不到任何 `exit 5`** ⇒ 不是 script 自己退的
> 2. `POLL_TIMEOUT=900`（15 分）卻跑了 29 分鐘 ⇒ 也不是 script 自己 timeout（真
>    timeout 會印 `Timeout: CI not completed after 900s` 並 `exit 1`）
> 3. log 停在 `Main branch SHA: …` 之後沒有下一行，看起來像卡在建 tag，但 tag 其實
>    已建好、CI 也已成功
>
> **根因**：上面 Step 5 輪詢迴圈裡**反覆執行的** `sleep` 被 harness 砍掉。⚠ 單發
> 前景 `sleep` 能正常跑完，所以「先跑個 sleep 探針測測看」會得到 `exit 0`，反而
> 誤導你以為完整跑法沒問題。
>
> **正解**：不要讓 agent 自己跑輪詢迴圈，改走兩段式——先建 tag 觸發 CI，自己用
> `tea api` 查 `status=="success"`（查法同上一個 callout），確認後再用
> `--image <full-image>` 收尾（該路徑不經過 Step 5 的 poll 迴圈）。
>
> **`exit 5` ≠ 沒有副作用**：那次已留下建好的 tag 與一次成功的 CI build ⇒ 收尾
> 直接補 `--image`，**不要重跑**整支 script（會觸發 triple build）。
>
> 🔴 **macOS 沒有 `setsid`**：想真 detach 而寫 `nohup setsid bash … &` 會**靜默**
> 變成 `nohup: setsid: No such file or directory` 且整個沒啟動。真 detach 改用
> `python3 -c "import subprocess; subprocess.Popen([...], start_new_session=True)"`。
>
> ⚠ **plugin PR 合併 ≠ 你本機生效**：`claude plugin update` 只比對 `plugin.json` 的
> `version`，沒 bump 就冪等 skip（回報成功但 cache 一行沒動）。症狀：create 階段仍固定
> 卡約 5 分鐘（印 `Pods did not become ready within 300s` 之後才收斂）就是還在跑舊版——
> 走 `uninstall` + `install`，並驗 `installed_plugins.json` 的 `gitCommitSha`。
>
> 另注意：`condition met` 那幾行**可能是舊 pod** 的（deployment 策略是
> `Recreate`，該時點舊 pod 還在服務）。要確認新 pod 起來了看 Step 8 的 post-check，
> 別看這幾行。

### Step 6: 部署到 Sandbox（委派 woow-dev plugin）

deploy-stg **不再自帶 create.sh**，改委派給 woow-dev plugin 的 `/woow-dev:k8s-dev-sandbox`（同一套引擎，避免重複程式）。解析 plugin `create.sh` 的邏輯在 [`scripts/plugin-resolve.sh`](scripts/plugin-resolve.sh)，依序：`$WOOWPRD_PLUGIN_ROOT`（env 覆寫，語意＝**plugin 根**，指到 repo 根也接得住）→ `plugins-local`（放的是整個 plugin repo，故多一層 `plugins/<plugin>/`）→ marketplace cache（`cache/*/woow-dev/*`，相容舊名 `woowprd`；多版本取最新）→ cache 根遞迴 fallback（不綁 plugin 名）；全部落空才 fail-loud。

> 🔴 **兩種 base 形狀不共用同一段相對路徑**（#728）：cache 底下的 `<mkt>/<plugin>/<ver>/` 已經是 plugin 根，`plugins-local/<repo>/` 卻是 marketplace 根。#728 之前只有一段 plugin 根形狀的相對路徑，於是 `plugins-local` 的 dev copy **永不命中**、一路落到 cache —— 在 plugins-local 改完程式跑 deploy-stg，跑的其實是 cache 舊版，而 helm 綠、部署成功、行為是舊的。**要確認這次跑的是哪一份，只能看 `Delegating sandbox creation to woow-dev plugin:` 那行印出的路徑。**
> 反向陷阱同樣要記住：把 `plugins/<plugin>/` 一律併進相對路徑，會把 cache 那兩條（目前多數人唯一在用的路徑）全部打壞。守衛在 `tests/test-resolve-plugin-create-sh.sh` 的 B 組。

```bash
FULL_IMAGE="jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo-stg:${DOCKER_TAG}"

# deploy-stg.sh 解析後等同：
bash "<plugin>/skills/k8s-dev-sandbox/scripts/create.sh" \
  --repo "$(git rev-parse --show-toplevel)" \
  --context "${KUBE_CONTEXT}" \
  --branch "${SLUG}" \
  --image "${FULL_IMAGE}" \
  --mode stg
```

這會：
- 建立 namespace `dev-sandbox-${SLUG}`（managed-label 前綴為 plugin native 的 `dev-sandbox.io`，用 `/woow-dev:k8s-dev-sandbox list/status/extend` 管理）
- 以 STG 模式部署 Odoo（addons 路徑：`/opt/bitnami/odoo/custom-addons/`）
- 套用 NetworkPolicy 和 ResourceQuota
- stg 平台特化（networkPolicy / storageClass / JCR registry / agent-runtime secret 從 operator 撈）走 repo 的 `.sandbox/pre-install.sh`；operator/ECPay/AI/journal seed 走 `.sandbox/post-create.sh`

> **前置**：需安裝 woow-dev plugin（或設 `WOOWPRD_PLUGIN_ROOT`）。`JCR_USER`/`JCR_PASS`、`AGENT_RUNTIME_*`、`PAAS_OPERATOR_NAMESPACE` 等 env 由 deploy-stg 環境繼承（或由 onboard profile 經 create.sh 載入），經 plugin create.sh 傳入 `.sandbox/` hook。

### Step 7: Odoo 系統參數（已宣告式化，自動）

`web.base.url` + `web.base.url.freeze` 的 seed 已從 deploy-stg 移出，宣告在本 repo
`.sandbox.yml` 的 `config_params_stg`：

```yaml
config_params_stg:
  web.base.url: "https://{{slug}}.woowtech.io"
  web.base.url.freeze: "True"
```

由 plugin `create.sh` 在 Step 6 部署時自動 **diff-aware** 套用：值沒變不動（零多餘
restart）、值變了 upsert + rollout restart（解 `ir.config_parameter` in-process cache）、
套用失敗 create **非零退出**（fail-loud，deploy-stg `set -e` 接力傳播）。

> 為什麼需要這組參數：fresh DB 沒有 `web.base.url` 時，Odoo 用 request host 但不帶
> scheme → 丟給 ECPay 的 OrderResultURL 變 `http://...` → 3DS 完成後 HTTPS→HTTP 的
> POST 被 Chrome 擋 Mixed Content，使用者無法完成綁卡/加值。`freeze=True` 防止 Odoo
> 在第一次 authenticated request 時改回去。

### Step 8: 驗證部署 + addon 版號 post-check gate（#665）

`deploy-stg.sh` 收尾會列 pod，然後跑 **addon 版號 post-check**
（`scripts/verify-addon-version.sh`）。不一致 ⇒ 整支 script 非 0 退出，且**不會**印
`Staging deployment complete!`。

**先講結論：換 image ✅ 會升 addon。這道 gate 驗的不是「有沒有升」，而是「升級有沒有
收斂完成」。**

底下三條原始碼觀察都是**真的**，別把它們一起丟掉：

- plugin `create.sh` 只下 `--set odoo.updateModules=web`（odoo deployment args 恆為
  `-u web`，不含 `woow_paas_platform`）
- `create.sh` 的 `-i <modules>` install 分支只在 DB **未初始化**時跑，既有 stg DB 整段被 skip
- `.sandbox/post-create.sh` 是純 psql seed，不碰 `ir_module_module`

⇒ 沒有任何一步**顯式**對 `woow_paas_platform` 下 `-u`。**但這不等於它不會被升**——漏掉的
是 Odoo 的 **reverse-dependency 傳播**：

| 環節 | 憑據（Odoo 18 原始碼，本機樹 `AREA-odoo/odoo-18.0/`） |
|---|---|
| CLI `-u` 走的就是 `button_upgrade()` | `odoo/modules/loading.py:458-462` |
| `button_upgrade()` 把**依賴被升模組的模組**一併標 `to upgrade` | `odoo/addons/base/models/ir_module.py:706-714`（查 `dependency.name == 當前模組名` → 取 `dep.module_id`） |
| 而且是**遞移**的（`while` 迴圈取閉包，非只一層） | 同上 `:699-712` |
| `woow_paas_platform` 是 `web` 的 dependent | `src/__manifest__.py` `depends` 直接含 `web`；**且**經 `project` / `bus` 兩條間接鏈同樣到 `web` |

⇒ `-u web` 連帶升 `woow_paas_platform`，**不需要 `stg-upgrade-N` Job**。因為直接鏈與間接
鏈同時成立，就算哪天有人把 `web` 從 `depends` 拿掉，傳播照樣經 `project` / `bus` 生效——
這個結論不是靠單一一行 manifest 撐著的。

**那 gate 還有什麼用？** 傳播會被觸發 ≠ 升級會收斂完成。仍然會靜默失效的形態：模組卡在
`to upgrade`、migration 中途炸掉、模組被反安裝、psql 探針壞掉回空值——這些一樣是 helm 綠燈、
pod `1/1 Running`、`kubectl logs` 乾淨。**gate 驗的是終態，不是路徑。**

<details>
<summary>⚠ 2026-08-11 之前這裡寫的是相反的結論（保留原文，因為這個坑值得看得見）</summary>

原文：

> ~~⇒ 對既有 sandbox，整條路徑沒有任何一步對 `woow_paas_platform` 下 `-u`。過去幾次「換~~
> ~~image 就升上去了」是**僥倖**——前一輪 upgrade Job 把模組留在 `state='to upgrade'`，~~
> ~~Odoo 的 registry load 會把它撿起來跑完。前一輪若乾淨收在 `installed`，同樣的 deploy~~
> ~~就**靜默不升**：helm 綠燈、pod `1/1 Running`、`kubectl logs` 乾淨，DB 版號停在舊版。~~

**這個結論在 2026-08-11 被一次乾淨的對照實驗推翻**：起點 DB `installed | .123`（無殘留
`to upgrade` 可被撿）→ 換 image → 無任何人為介入 → DB `installed | .124`。

⚠ **錯在哪一層很重要**：上面那三條 `create.sh` 原始碼觀察**本身是對的**，錯的是從它們推出
的**因果**——把「沒人顯式下 `-u`」等同於「不會升」，漏掉 reverse-dependency 傳播這一環。
所以修正保留了觀察、只換掉推論。

⚠ 同日早上曾把「新 pod 啟動 61 秒時 `to upgrade | .121`」判讀成「昨晚 Job 的殘留」。不是——
那是這次 `-u web` 傳播標記後、升級**進行中**的快照。

📌 **這個說法在本 repo 已被講反三次**（2026-06-02 F-DT-4 原報 → 同日 8 分鐘後自己撤回；
2026-08-03 本 skill 與 `stg-upgrade-recipe-dev-sandbox.md`；2026-08-11 早上 issue #665 與
本檔）。要再寫「換 image 不會升 addon」之前，請先跑一次上表的原始碼查證。

</details>

**判準**：

| | 值 |
|---|---|
| 期望 | **pod 正在跑的 image 內** `custom-addons/<module>/__manifest__.py` 的 `version` |
| 實際 | DB `ir_module_module.latest_version`，且 `state` 必須是 `installed` |

期望值取自 image 而不是本機 working tree，是因為判準要問的是「跑著的 code 與 DB 有沒有
對上」；而且 stg 的 CI 一律 clone `develop`，拿 working tree 當基準在 feature branch 上
會天天誤報。本機 repo 版號仍會印出來當**參考值**（非判準）；本機落後 remote 時會多印
一行落差（#723）——落後情境下那個裸版號與本次部署無關，卻長得像判準。

失敗時**只報紅、不自動補跑升級**——自動 `-u` 會與正在服務的 pod 搶同一顆 DB 做
migration，而手動 upgrade Job 有 ResourceQuota 與 filestore RWO nodeSelector 兩個非顯然
的陷阱。

**紅燈時的處置順序（別直接跳去跑 Job）**：

1. **先等**：`-u web` 的傳播升級要跑完才會落到 `installed`。rollout 剛收斂就查會抓到
   `to upgrade` 的**進行中**快照——那不是失敗。單獨重跑 post-check 確認。
2. **再看 log**：`kubectl --context woow-k3s -n dev-sandbox-stg logs deploy/stg-odoo-dev-sandbox-odoo`
   找 `Running migration [<ver>>] post-migrate` 與 `Modules loaded.`。傳播有被觸發、但
   migration 途中炸了，錯誤會在這裡。
3. **確認傳播鏈還在**：`src/__manifest__.py` 的 `depends`（直接 `web`，或間接經 `project` /
   `bus`）。整條斷了才是「真的沒被升」。
4. **最後才是** 手動 upgrade Job，照
   [`docs/guides/deployment/stg-db-manual-upgrade-recipe.md`](../../../docs/guides/deployment/stg-db-manual-upgrade-recipe.md)。
   它在傳播沒發生、或要單獨升某個模組時仍然有用，**但不是 deploy-stg 的必要步驟**。

**單獨執行**（不重跑部署，隨時可查 stg 現況）：

```bash
# 預設：拿 pod 正在跑的 image 當期望值
.claude/skills/deploy-stg/scripts/verify-addon-version.sh --slug stg --context woow-k3s --timeout 0

# 問「我手上這份 code 有沒有上到 stg」：改用本機 manifest 當期望值
.claude/skills/deploy-stg/scripts/verify-addon-version.sh --slug stg --context woow-k3s \
  --manifest src/__manifest__.py --timeout 0
```

exit code：`0` 一致、`1` 版號判定失敗、`2` 取樣／環境錯誤（**取樣不到 ≠ 沒問題**）。

**gate 自己的自測**：每次跑 post-check 都會先跑一次判定層自測
（`tests/test-verify-addon-version.sh`，純函式、零外部相依），自測紅或自測檔不見就直接
判失敗。同一支測試也由 `repo-lint` workflow 在 PR 上跑。

```bash
bash .claude/skills/deploy-stg/tests/test-verify-addon-version.sh
```

### Step 9: 完成通知

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

# 銷毀 staging sandbox（in-repo skill 已無 destroy，走 plugin）
/woow-dev:k8s-dev-sandbox destroy stg --context woow-k3s
```

## 疑難排解

| 問題 | 可能原因 | 解決方式 |
|------|----------|----------|
| `No tea login configured` | 未設定 tea login | `tea login add --name gitea --url https://git-prod.woowtech.io --token <your-token>` |
| `git-prod auth probe failed` | token 過期或 CF Access 攔截（`tea login list` 綠不代表 API 打得通） | 401 → 重發 token；CF Access → 瀏覽器重登；分流表見 `docs/guides/development/tea-cli-write-gotchas.md` |
| 建立 tag 失敗（tea api 回傳錯誤） | Token 權限不足或 repo 不存在 | 確認 token 有 `repo` scope，確認 CI_REPO 路徑正確 |
| Tag already exists | 同名 tag 已存在 | 使用 `--tag` 指定不同名稱，或手動到 Gitea 刪除舊 tag |
| CI run not found（持續等待） | CI workflow 未被觸發 | 到 Gitea UI 檢查 workflow 設定是否正確匹配 `stg-*` tag |
| CI status: failure | CI build 失敗 | 到 Gitea UI 檢查 workflow run 日誌 |
| CI 輪詢 timeout | CI build 時間過長 | 使用 `--timeout` 增加等待時間，或到 Gitea UI 檢查 |
| Helm install 失敗 | kubectl 未連線或權限不足 | 確認 kubeconfig 和 RBAC |
| Pod CrashLoopBackOff | Odoo 啟動失敗 | `kubectl logs -n dev-sandbox-stg <pod>` 檢查日誌 |
| `addon 版號 post-check 失敗（#665）`，DB 版號落後 | **不是**「deploy-stg 沒有升 addon 的步驟」——`-u web` 會經 reverse-dependency 傳播升它（見 Step 8）。落後代表**升級沒收斂**：還在跑、migration 炸了、或模組被卡住 | 照 Step 8「紅燈時的處置順序」：先重跑 post-check 確認不是進行中快照 → 查 odoo pod log 的 `post-migrate` / `Modules loaded.` → 確認 `depends` 傳播鏈未斷 → 都排除了才跑手動 upgrade Job |
| post-check 說 `找不到可用的 odoo pod` | rollout 還沒收斂到本次的 image，或 image 字串不符 | 等 rollout 完成後單獨重跑 post-check；**不要**當成「沒問題」略過 |
| post-check 說 `讀不到 image 內的 manifest` | 該 image 的 addons 路徑不是預設值 | 用 `--addons-path` 指定；讀不到一律當失敗，不會 fallback |
| `判定層自測未通過` | gate 的比較器本身壞了 | 跑 `bash .claude/skills/deploy-stg/tests/test-verify-addon-version.sh` 看細節；在修好之前 post-check 的綠燈都不可信 |
