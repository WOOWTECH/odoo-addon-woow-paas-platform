---
title: 發佈到 Production 的標準作業流程（SOP）
category: guides
status: active
created: 2026-06-14
updated: 2026-06-14
tags: [paas, release, production, fleet, gitops, helm, ci-cd, sop]
related:
  - docs/guides/deployment/agent-runtime-image-upgrade-sop.md
  - docs/guides/deployment/troubleshooting.md
  - docs/guides/deployment/k8s-setup.md
  - https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd
  - https://git-prod.woowtech.io/woow-paas/woow-paas-charts
  - https://git-prod.woowtech.io/woow-paas/paas-odoo-ci
  - https://git-prod.woowtech.io/woow-paas/paas-agent-runtime
---

# 發佈到 Production 的標準作業流程（SOP）

> **本文件給誰**：① 人類 release 操作者；② **未來自動化部署的 harness agent**（步驟刻意寫成可機器執行：每個 pin 點都標到 `repo/檔案:行號`、每步都附可貼上的指令與「閉環驗證」指令）。
>
> **核心一句話**：CI 只**產出** image / chart，**不上線**；上 prod 的**唯一動作**是改 `paas-app-bundle-cd`（Rancher Fleet GitOps repo）裡的 pin 值並 merge `main`，Fleet 自動 `helm upgrade`。
>
> **驗證紀律**：不信任工具回傳的「成功」字串。關鍵步驟一律用 `git show` / `helm ... list` / `kubectl ... get` / **pod imageID（digest）** 閉環實證（prod image tag 是 mutable date-tag，**認 digest 不認 tag**）。
>
> **資料來源與時點**：本文件事實於 **2026-06-14** 經三方交叉驗證：① 各 repo agent 口述、② `kubectl --context woow-k3s` 叢集實況、③ clone `paas-app-bundle-cd` / `woow-paas-charts` 實際檔案。版本矩陣（§2）為當下快照，**會隨發版漂移，用前先用 §2 的指令重新產生**。

---

## 1. 系統全貌：repos / images / charts / CD

### 1.1 角色分工（5 個 repo + 上游）

| Repo | git-prod URL | 職責 | 產出 | CI 觸發 |
|------|-------------|------|------|---------|
| **woow_paas_platform**（本 repo） | `odoo-addons/woow_paas_platform` | Odoo 18 addon 原始碼 + `extra/paas-operator/` operator 原始碼 | ① 觸發 paas-odoo image build；② **直接 build operator image** | push `v*` tag → 觸發 odoo build；push `operator-v*` tag / 改 `extra/paas-operator/**` → build operator image |
| **paas-odoo-ci** | `woow-paas/paas-odoo-ci` | Build Odoo 應用 image（clone 本 addon at tag 後打包進 Bitnami Odoo image） | `paas-odoo` / `paas-odoo-stg` / `cloud-odoo` image | 由 woow_paas_platform 跨-repo `workflow_dispatch`；或 push `v*`/`stg-*`/`cloud-v*` tag |
| **paas-agent-runtime** | `woow-paas/paas-agent-runtime` | agent-runtime（per-project workspace pod）原始碼 | `agent-runtime` image | push `vX.Y.Z` tag → build + 對 charts repo 開 auto-bump PR |
| **woow-paas-charts** | `woow-paas/woow-paas-charts` | Helm **chart** 倉庫（只發 chart，不發 app） | `paas-operator` / `agent-runtime` / `vibe-kanban` chart | bump `Chart.yaml#version` → merge `main` → publish 到 Gitea Helm registry |
| **paas-app-bundle-cd** | `woow-paas/paas-app-bundle-cd` | **Rancher Fleet GitOps CD repo**（唯一上 prod 的入口） | Fleet 監控此 repo → `helm upgrade` | merge `main` → Fleet 自動 reconcile（無自動 promote） |

### 1.2 各 component 的 image 來源 / chart 來源 / CD bundle 對照

| Component | image 來源 | chart 來源 | Fleet bundle（prod） | prod namespace |
|-----------|-----------|-----------|---------------------|----------------|
| **Odoo（前台 /woow、後台 /odoo、portal /my）** | paas-odoo-ci → `paas-odoo` | **上游 Bitnami OCI** `odoo`（CD `fleet.yaml` 直接引用） | `paas-app-bundle-paas-odoo` | `paas-system` |
| **paas-operator** | woow_paas_platform `extra/paas-operator/` | woow-paas-charts `charts/paas-operator` | `paas-app-bundle-paas-operator` | `paas-system` |
| **PostgreSQL** | 上游 `bitnamilegacy/postgresql` | （隨 Bitnami Odoo chart 的 subchart） | （同 paas-odoo bundle） | `paas-system` |
| **Cloudflare Tunnel** | 上游 `cloudflare/cloudflared` | **上游** `cloudflare-tunnel-remote`（CD `fleet.yaml` 直接引用，**未 pin 版本**） | `paas-app-bundle-cloudflare` | `paas-cloudflare-system` |
| **agent-runtime** | paas-agent-runtime → `agent-runtime` | woow-paas-charts `charts/agent-runtime` | **不由 Fleet 直裝**：由 operator runtime 動態 `helm install`（且 **prod 尚未啟用**，見 §5.3） | `paas-ws-<hash>`（每 project 一個） |
| **vibe-kanban** | 上游 | woow-paas-charts `charts/vibe-kanban` | **prod 無 bundle = 目前未部署 prod** | — |

### 1.3 資料流（從改 code 到上 prod）

```
┌─ App image 路徑（odoo） ───────────────────────────────────────────────┐
│ woow_paas_platform: bump __manifest__.py → push v* tag                   │
│   └─ .gitea/workflows/trigger-odoo-build.yml                             │
│        └─ dispatch paas-odoo-ci build-image.yml (tag=YYYYMMDD.HHMM)      │
│             └─ build & push  jcr-prod.../paas-odoo:YYYYMMDD.HHMM         │
└─────────────────────────────────────────────────────────────────────────┘
┌─ Operator image 路徑 ────────────────────────────────────────────────────┐
│ woow_paas_platform: push operator-v{YYYYMMDD.HHMM} tag                    │
│   └─ .gitea/workflows/build-paas-operator.yml                            │
│        └─ build & push  jcr-prod.../paas-operator:YYYYMMDD.HHMM          │
└─────────────────────────────────────────────────────────────────────────┘
┌─ Chart 路徑（operator / agent-runtime / vibe-kanban） ────────────────────┐
│ woow-paas-charts: bump Chart.yaml#version → merge main                    │
│   └─ publish-charts.yaml → Gitea Helm registry（版本已存在則冪等 skip）   │
└─────────────────────────────────────────────────────────────────────────┘
                              │  以上產出 artifact，尚未上線
                              ▼
┌─ CD 路徑（唯一上 prod 入口）─────────────────────────────────────────────┐
│ paas-app-bundle-cd: 改 pin 值（image.tag in values / chart version in    │
│   fleet.yaml）→ PR → merge main                                          │
│     └─ Rancher Fleet 偵測 git 變更（~30–60s lag）→ helm upgrade          │
│          └─ woow-k3s cluster：paas-system / paas-cloudflare-system        │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 當前 prod 版本矩陣（2026-06-14 live 驗證）

> ⚠️ 這是**快照**。發版後務必用本節最後的指令重新產生，別照抄。
> ⚠️ prod image tag 是 **mutable date-tag**（可能被重指）→ **判版本看 digest，不看 tag**。

### Prod（cluster `woow-k3s`）

| Component | NS | image:tag | digest（sha256，前綴） | chart | chart 版本 | pin 位置（paas-app-bundle-cd） |
|-----------|----|-----------|----------------------|-------|-----------|-------------------------------|
| paas-odoo | `paas-system` | `jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo:20260426.1204` | `0c899562…ebd6` | Bitnami `odoo`（OCI） | `28.2.10` | tag→`paas-odoo/values-prod.yaml`（image 區塊）；chart→`paas-odoo/fleet.yaml` `helm.version` |
| paas-operator | `paas-system` | `…/paas-operator:20260525.0054` | `2b49e9eb…51c5` | `paas-operator` | **`0.1.8`** | tag→`paas-operator/values-prod.yaml:8`；chart→`paas-operator/fleet.yaml` `version` |
| postgresql | `paas-system` | `bitnamilegacy/postgresql:17.6.0-debian-12-r4` | `92635613…0a8d` | （subchart） | — | `paas-odoo/values-prod.yaml`（postgresql 區塊） |
| cloudflared | `paas-cloudflare-system` | `cloudflare/cloudflared:latest` ⚠️ | `6b599ca3…31b0` | `cloudflare-tunnel-remote` | **未 pin（浮動 latest）** ⚠️ | `cloudflare/fleet.yaml`（無 `version` 行） |

### Stg 對照（同 cluster，靠 namespace 區隔）

| Component | NS | image:tag | chart 版本 |
|-----------|----|-----------|-----------|
| paas-operator-stg | `paas-stg-system` | `…/paas-operator:20260614.0841` | `0.1.14` |
| cloudflared-stg | `paas-stg-cloudflare-system` | `cloudflare/cloudflared:latest` | 未 pin |
| odoo（stg） | — | **不在此 CD**：stg odoo 走 `dev-sandbox-stg`（見 [deploy-stg](../../../CLAUDE.md)） | — |

> **觀察**：prod operator（chart `0.1.8` / image `20260525.0054`）**落後 stg**（chart `0.1.14` / image `20260614.0841`）。stg 是領先邊（已帶 agent-runtime / skill sidecar / security-access 等）。prod operator values **無 `agentRuntime` 區塊** → **agent-runtime 在 prod 尚未啟用**（見 §5.3）。

### 重新產生版本矩陣（一鍵）

```bash
for ns in paas-system paas-cloudflare-system paas-stg-system paas-stg-cloudflare-system; do
  echo "==== $ns ===="
  kubectl --context woow-k3s -n "$ns" get pods \
    -o jsonpath='{range .items[*]}{range .status.containerStatuses[*]}{.image}{"  →  "}{.imageID}{"\n"}{end}{end}'
done
# chart 釘版：直接看 CD repo
git -C /tmp/paas-app-bundle-cd pull 2>/dev/null || git clone https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd.git /tmp/paas-app-bundle-cd
grep -rnE 'version:|tag:' /tmp/paas-app-bundle-cd/*/fleet.yaml /tmp/paas-app-bundle-cd/*/values-prod.yaml
```

---

## 3. 前置條件與存取

| 項目 | 設定 | 驗證指令 |
|------|------|---------|
| **kubectl prod** | context `woow-k3s`（需 Tailscale VPN；設定見 memory `reference_kubectl_woow_k3s`） | `kubectl --context woow-k3s get ns \| grep paas` |
| **Git 平台 CLI** | `tea`（git-prod.woowtech.io），需設 **default login** 否則 jq 會炸（見 memory `reference_deploy_stg_gotchas`） | `tea login list` |
| **本 repo 發版** | `/woow:deploy`（自動讀版號、打 tag、push） | — |
| **operator image 發版** | `build-operator` skill（建 `operator-v{YYYYMMDD.HHMM}` tag 觸發 CI） | — |

### 站點 URL

| 環境 | 主站（Cloudflare tunnel） | 前台 PaaS | Odoo 後台 | Portal |
|------|--------------------------|-----------|-----------|--------|
| **prod** | `https://paas.woowtech.io/` | `/woow` | `/odoo` | `/my` |
| **stg** | `https://stg.woowtech.io/` | `/woow` | `/odoo` | `/my` |

---

## 4. 核心心智模型（agent 必讀）

1. **CI ≠ 上線**。CI 綠燈只代表 image / chart 已 build 並 push 到 registry。上 prod 是另一個獨立動作。
2. **唯一上 prod 的動作 = 改 `paas-app-bundle-cd` 的 pin 值 + merge `main`**。Fleet 監控此 repo（GitRepo `paas-app-bundle` in `fleet-local`），偵測到 `main` 變更後 ~30–60s 自動 `helm upgrade`。**沒有手動 Rancher UI 步驟**（若有舊文件/舊認知說要去 Rancher 點 upgrade，那是過時的）。
3. **pin 在兩個地方，分清楚**：
   - **image tag** → `<component>/values-<env>.yaml` 的 `image.tag`
   - **chart version** → `<component>/fleet.yaml` 的 `helm.version` 行（operator 與 odoo OCI **同一 shape**；odoo 的 `helm.chart` 只指向 OCI 來源 `oci://…/odoo`，版本仍在獨立的 `version:` 行，**不是內嵌在 chart 字串裡**）
4. **改 image 與改 chart 是兩件事**。只改 image tag（values）不動 chart；但若新版需要 chart 把新設定 passthrough 成 env，**values-only 無效，必須先 bump chart**（見 §9 坑 #5）。
5. **prod image tag 可變** → 驗證一律比對 **pod imageID（digest）**，不是看 tag 字串。
6. **prod 與 stg 同一 cluster、靠 namespace 區隔**。tunnel / 金鑰 / namespacePrefix 等 **prod 專屬欄位**搬版時不可被 stg 值覆蓋（見 §6）。

---

## 5. 各 component 上 prod SOP

> 通用前置：先確認要上的 image **已在 JCR**、chart **已在 Gitea Helm registry**。指向不存在的版本 → Fleet `helm install` 直接失敗（§9 坑 #1）。

### 5.1 Odoo（paas-odoo）上 prod

**何時**：本 addon（`src/`）改動要上 prod。

```
步驟                                                      驗證
────────────────────────────────────────────────────── ──────────────────────────────
1. 本 repo bump src/__manifest__.py version              grep version src/__manifest__.py
   （格式 18.0.{major}.{minor}.{patch}，目前 18.0.1.0.39）
2. commit "release: 18.0.1.0.XX" → push
3. git tag v18.0.1.0.XX && git push origin v18.0.1.0.XX  （= /woow:deploy 自動化此段）
   → 觸發 trigger-odoo-build.yml → dispatch paas-odoo-ci
4. 等 paas-odoo-ci build 完成，取得 image tag            CI log 的 image_tag=YYYYMMDD.HHMM
   （= UTC date，非 addon 版號）                          或 tea 查 paas-odoo-ci actions
5. 在 paas-app-bundle-cd 開 branch，改                     git -C cd diff
   paas-odoo/values-prod.yaml 的 image.tag = 新 tag
6. tea PR（zh-tw）→ merge main                            tea pr list
7. 等 Fleet reconcile（~30–60s）                          見 §7
8. 驗證 prod pod 跑新 digest                              見 §7「pod digest」
```

> **odoo chart（Bitnami `28.2.10`）** 平常不動；只有要升 Odoo 框架本身時才改 `paas-odoo/fleet.yaml` 的 `helm.version`（高風險，需先 stg / sandbox 驗）。

### 5.2 paas-operator 上 prod

**注意**：operator 有**兩個版號軸** — image（來源 `extra/paas-operator/`）與 chart（來源 `woow-paas-charts`）。

```
步驟                                                      驗證
────────────────────────────────────────────────────── ──────────────────────────────
A. image：本 repo 改 extra/paas-operator/ →
   用 build-operator skill 建 operator-v{YYYYMMDD.HHMM}
   → CI build-paas-operator.yml → push paas-operator:{tag}  tea 查 CI；image 在 JCR
B. chart（若有改 chart）：woow-paas-charts bump
   charts/paas-operator/Chart.yaml#version → merge main
   → publish-charts.yaml 發到 Gitea Helm registry          helm registry 有新版本
C. paas-app-bundle-cd：
   - 改 paas-operator/values-prod.yaml:8  image.tag
   - 改 paas-operator/fleet.yaml          version（若 B 有改）
   建議：把 **stg 已驗證過的值**（paas-operator-stg/）搬過來
D. tea PR → merge main → Fleet reconcile
E. 驗證：operator pod digest + readyReplicas=2/2          見 §7
```

> **lockstep**：若版本動到 drain/probe/preStop 契約或新 env passthrough，**image 與 chart 必須同 PR 一起 bump**，否則 crashloop（§9 坑 #5/#6）。

### 5.3 agent-runtime（prod 尚未啟用）

- **現況**：prod operator `values-prod.yaml` **無 `agentRuntime` 區塊** → agent-runtime 在 **prod 未啟用**，目前只到 stg。要上 prod 需先補 prod `agentRuntime`（imageTag + chartVersion + probeStyle + skillRunner…）與對應 operator 設定，並走 stg 啟用 gap chain 逐層驗證。
- **升版（既存 workspace 不自動換 image，須手動 reap）**：完整程序見 **[agent-runtime image 升版 SOP](agent-runtime-image-upgrade-sop.md)**（operator/platform 視角）與 paas-agent-runtime repo `docs/runbooks/image-upgrade-sop.md`（runtime 視角）。**勿在本文件重抄**。
- 關鍵差異：agent-runtime chart **不由 Fleet 直裝**，而是 operator runtime 在 spawn 時動態 `helm install`；Fleet 只透過 operator 的 env（`AGENT_RUNTIME_IMAGE_TAG` / `AGENT_RUNTIME_CHART_VERSION`）把版本傳進去。

### 5.4 Cloudflare Tunnel

- chart `cloudflare-tunnel-remote` 與 image `cloudflared` **皆未 pin（浮動 latest）**。平常不需動。
- tunnel token 由 secret 注入（prod `paas-cloudflare-fleet-secrets`、stg `paas-stg-cloudflare-fleet-secrets`，皆在 `fleet-local`）。**改 secret 後 Fleet 不會自動同步，須 force sync**（§7）。
- **prod / stg tunnel 不可混用**：prod tunnelId `328c3a28-…`、stg `55953bfa-…`。
- ⚠️ 浮動 latest 是已知風險（§10）：upstream 改版可能無預警影響 prod，升級前建議先在 stg 觀察。

### 5.5 vibe-kanban

- chart 存在於 woow-paas-charts（`0.2.0`），但 **prod / stg Fleet 皆無對應 bundle = 目前未透過此 CD 部署**。要上 prod 須在 paas-app-bundle-cd 新增 `vibe-kanban/{fleet.yaml,values-prod.yaml}` + `fleet-local` secret（流程見 CD repo README「新增 App」）。

---

## 6. stg → prod promote 通則

prod **無自動 promote**，是手動把 stg 驗證過的值搬到 prod：

1. **先在 stg 完整驗證**（`paas-operator-stg/` 等），確認 pod 健康、端到端功能正常。
2. 把 stg 已驗證的 **image.tag / chart version** 搬到對應 prod 檔（`paas-operator/values-prod.yaml` + `fleet.yaml`）。
3. **保留 prod 專屬欄位、不可被 stg 值覆蓋**：
   - `namespacePrefix`：prod `paas-ws-` / stg `paas-ws-stg-`
   - `replicaCount`：prod 2
   - `pvcGracePeriodDays`：prod **90**（dev/stg 為 7）
   - prod `tunnelId` / `zoneId` / 各 prod secret
   - prod operator 啟用 agent-runtime 時的 webhook / auth secret（見 **paas-app-bundle-cd repo 的 `CLAUDE.md`「Agent-runtime 共用 secret」節**：secret `paas-operator-agent-runtime-auth`，含 current / previous / webhook-current 三 key —— **注意此 anchor 在 CD repo 而非本 repo 的 CLAUDE.md**）
4. tea PR（zh-tw）→ merge `main` → Fleet 自動 `helm upgrade`。

---

## 7. 驗證（verify）recipes

```bash
CTX=woow-k3s

# (1) pod 跑的 image + digest（認 digest 不認 tag）
kubectl --context $CTX -n paas-system get pods \
  -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{range .status.containerStatuses[*]}{.image}{"  "}{.imageID}{"\n"}{end}{end}'

# (2) deployment 是否 ready
kubectl --context $CTX -n paas-system get deploy paas-odoo paas-operator \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,DESIRED:.spec.replicas,IMAGE:.spec.template.spec.containers[*].image'

# (3) Fleet GitRepo 是否同步到最新 commit、是否全 ready
kubectl --context $CTX -n fleet-local get gitrepo paas-app-bundle -o wide
#   COMMIT 欄要等於 paas-app-bundle-cd main 的 HEAD；STATUS 應為空（全 ready）

# (4) 各 bundle ready 狀態
kubectl --context $CTX -n fleet-local get bundles

# (5) Fleet 不監控 fleet-local secret：改 secret 後必 force sync（讀現值 +1，非占位符）
#     bundle 實名（不含 -cd 後綴）：prod=paas-app-bundle-paas-operator / stg=paas-app-bundle-paas-operator-stg
#     其它 component 換對應 bundle 名（先 `kubectl -n fleet-local get bundles` 查）
BUNDLE=paas-app-bundle-paas-operator
CUR=$(kubectl --context $CTX -n fleet-local get bundle "$BUNDLE" -o jsonpath='{.spec.forceSyncGeneration}'); CUR=${CUR:-0}
kubectl --context $CTX -n fleet-local patch bundle "$BUNDLE" \
  --type=merge -p "{\"spec\":{\"forceSyncGeneration\":$((CUR+1))}}"
#   權威食譜另見 paas-app-bundle-cd repo 的 CLAUDE.md force-sync 節

# (6) 站點 smoke test
curl -sSI https://paas.woowtech.io/woow | head -1
curl -sSI https://paas.woowtech.io/odoo | head -1
```

---

## 8. Rollback 通則

Fleet GitOps 的 rollback = **把 pin 值改回上一個已知良好版本**：

1. 在 paas-app-bundle-cd 把 `image.tag`（和/或 `fleet.yaml` chart version）改回前一版（**用前一版的 digest 反查 tag** 以防 mutable tag 漂移）。
2. merge `main` → Fleet 自動 `helm upgrade` 回退。
3. 緊急時（等不及 PR 流程）可 `git revert` 觸發 commit 後直接 merge。
4. 驗證 pod digest 已回到舊版（§7）。

> agent-runtime 的「換版」不適用一般 rollback（既存 pod 凍結 tag，須 DELETE reap），見 §5.3 連結。

---

## 9. Gotchas / 坑（彙整，含來源）

| # | 坑 | 後果 | 防法 |
|---|----|------|------|
| 1 | bump pin 前未確認 chart 在 Gitea registry、image 在 JCR | Fleet `helm install` 直接失敗 | 先驗 artifact 存在再改 pin |
| 2 | 改 `fleet-local` secret 後只 rollout restart | secret 不同步（pod 讀的是 Helm release 建的 secret，非 fleet-local） | 必 **force sync**（§7 (5)） |
| 3 | prod / stg tunnelId、金鑰混用 | 連到錯環境 | prod `328c3a28-…` / stg `55953bfa-…`，搬版保留 prod 專屬欄位（§6） |
| 4 | 看 image tag 判版本 | prod 是 mutable date-tag，可能被重指 → 誤判 | 看 **pod imageID（digest）** |
| 5 | 新設定只改 values，chart 沒 wire 成 env | values-only 無效 | 先 bump operator chart 把設定 passthrough 成 env |
| 6 | agent-runtime image / chart 沒 lockstep 同 PR bump | sidecar / agent crashloop（appVersion 指到無 bash bridge 的舊 image） | imageTag + chartVersion 同 PR；契約變更先知會 charts 端 |
| 7 | 改 chart 內容但沒 bump `Chart.yaml#version` | publish CI 冪等 skip → Fleet 永遠裝舊版 | 改 chart 必 bump version |
| 8 | operator helm `repoUrl` 用 in-cluster `gitea-http:3000` | helm 抓 index/tarball 跨 host 不帶 basic-auth → 401 | operator 取 chart 用公開 host `git-prod.woowtech.io`（注意：Fleet 自身在叢集內、用 in-cluster URL 取 chart 是正常的，兩者別混） |
| 9 | tag prefix 寫錯（paas-odoo-ci 僅以 `startsWith('stg-')` 判環境、**無 approval gate**） | prod image 被污染 | 發版用 skill / `/woow:deploy`，勿手打 tag |
| 10 | 手動 `helm uninstall` agent-runtime release | 繞過 operator owner → state 不一致 | 走 operator DELETE 端點（§5.3 連結） |

---

## 10. Known issues / 本次稽核發現（2026-06-14）

> 以下為整理 SOP 時發現、**非緊急**（prod 服務正常運行中）但建議追蹤 / 發 issue 的項目。

1. **Fleet bundle `paas-app-bundle-paas-operator` 卡在 `Modified`（0/1，GitRepo 整體 4/5）**
   - 現象：live `paas-system/paas-operator-registry` secret 有 `registry-server` / `registry-username`（有人手動補回），但 Fleet desired state 缺這兩個 key → Fleet 永久報 drift（預設不自動回滾）。
   - 當前影響：**無**（operator pods 2/2 Running、12 天 0 restart；operator 自身 image pull 用的是另一個 secret `jfrog-registry`；且 prod agent-runtime 未啟用）。
   - 潛在風險：GitRepo 卡 `Modified` 會遮蔽未來真實失敗；若做 drift-correction / force sync 把手動補的值蓋掉，等 prod 啟用 agent-runtime 時租戶 pod 會 ImagePullBackOff。
   - 建議：在 paas-app-bundle-cd 補齊 operator-registry secret 的 desired 定義（或調整 chart 渲染），讓 bundle 回 ready。
   - 追蹤 issue：[paas-app-bundle-cd#18](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/18)

2. **Cloudflare chart / image 未 pin（浮動 `latest`）**（prod + stg）：upstream 改版無預警影響 prod。建議 pin 明確 chart version + image tag。追蹤 issue：[paas-app-bundle-cd#19](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/19)

3. **paas-odoo-ci 無 prod approval gate**：workflow 僅以 `startsWith(ref_name,'stg-')` 判環境，無環境保護 / 人工核可。誤推 `v*` tag 即進 prod build。建議加 branch protection / environment approval。追蹤 issue：[paas-odoo-ci#1](https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/issues/1)

4. **文件 drift（小）**：`paas-app-bundle-cd/README.md` 只描述 `paas-odoo` 且寫 `values.yaml`，實際檔名為 `values-prod.yaml`、且已有 operator/cloudflare 等多 component。建議更新 README。追蹤 issue：[paas-app-bundle-cd#20](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/20)

5. **認知 drift（已澄清，記錄供後人）**：曾有「odoo CD 完全手動、改 `resources/private/paas-odoo.md` + Rancher UI 點 upgrade」的說法 —— 經查 Fleet GitRepo / bundle / CD repo README，**odoo 確實由 Fleet GitOps 管理**（bundle `paas-app-bundle-paas-odoo`），該手動說法為**過時錯誤**。

6. **charts repo README drift**：`woow-paas-charts/README.md` 宣稱 paas-operator CD「已改為 Fleet GitRepo 直接從 OCI registry 拉取」，但實際 `paas-app-bundle-cd/paas-operator/fleet.yaml` 仍用傳統 helm repo（in-cluster Gitea `…/api/packages/woow-paas/helm`，非 OCI）。可能誤導 agent 以為 operator chart 走 OCI。建議修 charts README 對齊實況。追蹤 issue：[woow-paas-charts#26](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/issues/26)

---

## 11. 自動化 checklist（給 harness agent）

以「把某 component 新版上 prod」為例，可機器執行的最小步驟：

```
[ ] 0. 前置：kubectl --context woow-k3s 可連；tea login 已設 default
[ ] 1. 確認目標 image 已在 JCR（CI 綠燈 + registry 查得到 tag）
[ ] 2.（若改 chart）確認 chart 已在 Gitea Helm registry，且 Chart.yaml#version 已 bump
[ ] 3. clone/pull paas-app-bundle-cd，改對應 <component>/values-<env>.yaml image.tag
       （和/或 fleet.yaml version）；保留 §6 prod 專屬欄位
[ ] 4. 自我核對 diff：只動 pin 值，未誤改 namespace/tunnel/prefix/replica
[ ] 5. tea PR（zh-tw 描述）→ 等審核 → merge main
[ ] 6. 等 Fleet reconcile：kubectl -n fleet-local get gitrepo paas-app-bundle
       → COMMIT == CD main HEAD 且 STATUS 空
[ ] 7. 閉環驗證：pod imageID(digest) == 預期新版；deploy readyReplicas 滿
[ ] 8. 站點 smoke：curl -I https://paas.woowtech.io/woow|/odoo|/my
[ ] 9.（agent-runtime 換版）另走 §5.3 連結的 reap SOP，勿用一般 rollback/restart
[ ] 10. 任一步失敗 → §8 rollback（改回前一版 digest 對應 tag → merge）
```

---

## 相關文件

- **agent-runtime image 升版**：[agent-runtime-image-upgrade-sop.md](agent-runtime-image-upgrade-sop.md)
- **部署疑難排解**：[troubleshooting.md](troubleshooting.md)
- **K8s 環境設定**：[k8s-setup.md](k8s-setup.md)
- **本 repo 發版 / deploy-stg / build image**：`../../../CLAUDE.md`（Release & Build / Deploy Staging 章節）
- **CD repo**：<https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd>（含各 component fleet.yaml / values）
- **Charts repo**：<https://git-prod.woowtech.io/woow-paas/woow-paas-charts>
- **kubectl woow-k3s 連線**：memory `reference_kubectl_woow_k3s`
