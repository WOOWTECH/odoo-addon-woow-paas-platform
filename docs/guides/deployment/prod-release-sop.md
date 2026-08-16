---
title: 發佈到 Production 的標準作業流程（SOP）
category: guides
status: active
created: 2026-06-14
updated: 2026-08-17
tags: [paas, release, production, fleet, gitops, helm, ci-cd, sop]
related:
  - docs/tracking/pitfalls/ci-addon-ref-empty-drifting-default-branch.md
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
>
> **2026-06-21 更新**：operator **#207/#208**（#207 mcp route delete 對齊 `generate_subdomain`；#208 PVC `orphaned-at` label 改 epoch 秒 + `validate_label_value` fail-loud）已發佈 **stg**（chart `0.1.15` / image `20260620.1406`，digest `1b322c8a…c1fbc`，端到端驗證通過），**現為 prod 待 promote 候選**（prod 仍 `0.1.8` / `20260525.0054`，落後）。本次 refresh 同步了 §2 stg 列、§5.2.1 worked example、§9 坑 #11（無回歸超集）、§10.1 drift 狀態（live 複驗仍 `Modified`）。
>
> **2026-06-24 更新**：naming env-token **L1-L3**（PR #214）已上 **stg** 並 live 驗證（operator chart `0.1.16` / image `20260624.1022`、`PAAS_ENV=stg`、ns label `paas.woowtech.io/env=stg`、Odoo SA `canonical_name=paas-stg-sa-`；當時 prod 未動仍 `0.1.8` / `20260525.0054`——已於隔日 promote，見下）。**§6 新增「兩端 env 一致」檢查（F-ENV-1b）**：promote operator/addon 到 prod 時，operator `PAAS_ENV`（Fleet `config.paasEnv`）與 Odoo `deploy_env` config_parameter **必須同為 `prod`**，否則 split-env（ns label 與 CF 命名對 env 各說各話）。§5.2.1 gate 同步加 `paasEnv: "prod"` 檢查。
>
> **2026-06-25 更新**：① **prod operator 已 promote 到 `0.1.16` / image `20260624.1022`（digest `9b937dbb…6b16`，helm rev 42，2026-06-25 06:03 UTC，paas-cd PR #27）** —— 不只追上、且已與 stg **同版**（stg 亦 `0.1.16` / `20260624.1022`，rev 65）。§2 矩陣與 §5.2.1 worked example 的「prod 仍 0.1.8」前提已**過時**（promote 在他處完成）。② **§10.1 issue #18（Fleet `paas-operator-registry` drift）已解決**（亦即 follow-ups **F-ENV-6**）：live secret 上殘留的 `registry-server`/`registry-username` 兩個 key 經 paas-charts 複核確認**零 consumer**（chart 自 0.1.6 起就單 key 模型、server/username 一直走 plaintext deployment env），移除後 bundle 收斂 `ready:1`、GitRepo `5/5`。原 pitfall/F-ENV-6 的「split-values 非 deep-merge 吃掉 server」框架為**誤診**，已於對應 pitfall doc 更正。
>
> **2026-07-25 更新（§5.1 大改）**：prod odoo `.75→.78` rollout 後複驗，發現 §5.1 步驟表與實際做法嚴重脫節，本次補齊四件事：① **S1 陷阱**——`paas-odoo/values-prod.yaml` 同時有 `image.tag` 與 `image.digest`，Bitnami common 以 `repository@digest` 渲染、**digest 優先於 tag**，**只改 tag 是 no-op 且會偽裝成發版成功**（§5.1 步驟 5 修正，§8 rollback 同步修正）；② **CD 兩段式 module upgrade 正式入表**——Bitnami chart `ODOO_SKIP_MODULES_UPDATE` 預設 `yes`，換 image 後 boot **不會**跑 `-u`、`noupdate=0` 的 seed XML 不會 reload，故**有 seed 變更的發版必須跑 `-u`**：PR-A（`"no"`）→ 驗證 → PR-B（`"yes"`）；③ **`-u` 曝險窗上限 = 驗證通過後 ≤30 分鐘**，加上收官三重確認；④ **發版前 DB snapshot 正式入表**（步驟 5，排在 promote 之前）—— prod DB 無離線備份（`backups.longhorn.io` 空、backuptarget 未設），snapshot 是**唯一**還原錨點，且**必須驗 `readyToUse=true`**（曾有 `markRemoved=true` 的假錨點），**由人類操作者親跑**。新增 §5.1.1 / §5.1.2 / §5.1.3。
>
> **2026-08-08 更新（S1 陷阱補第三處 pin）**：**`paas-odoo` 的 image pin 是三處，不是兩處** —— `values-prod.yaml` 的 `module-version-gate` initContainer 另有一個**寫死的 `image: …@sha256:…`**。本次把「兩者同改」全面改為「三處同進同退」（§2 pin 位置欄、§4 心智模型 3、§5.1 S1 陷阱、§5.1 步驟表步驟 6、§5.1.4 已知極限表、§5.1.5 ①、§8 rollback 步驟 1／2、§9 新增坑 #21、§11 checklist step 3／10），並補上兩件原本沒寫的事：① **漏改 gate 是與 S1 原型不同的失效形態** —— image 真的換了、三方 digest 閉環全過，但 gate 拿**自己那顆舊 image** 的版號去比對而判定「版本一致」，安靜跳過該跑的 `-u` ⇒ 新 code 跑在舊 schema 上；② **rollback 方向是它的鏡像** ⇒ 舊 code 跑在新 schema 上，且 §8 原本的 `grep -E '(tag|digest):'` 對 gate 那一行**零命中**（前綴是 `image:`），已修正。③ 新增**發版後唯一接得住此缺陷的檢查**（§5.1.5 ①、§11 step 7）：同一顆 pod 上 gate initContainer 與主容器的 imageID 比對，**2026-08-08 於 prod `paas-system` 實跑驗證＋陽性對照**。⚠ 該 jsonpath **必須**帶 `[?(@.name=="module-version-gate")]` 篩選器 —— `paas-odoo` 有兩個 initContainer（另一個是 `busybox` 的 `patch-odoo-conf`），寫成 `initContainerStatuses[*]` 會讓那一行永遠含 busybox digest ⇒ **連 pin 完全同步時都報紅**，等於做出一個沒有鑑別力、幾次之後就被當噪音忽略的檢查。
>
> **這個縫是怎麼長出來的（時間線）**：S1 陷阱段寫於 **2026-07-25**（`.75→.78` rollout 後的 §5.1 大改），當時 `values-prod.yaml` 確實**只有兩處** pin，「兩者同改」在那個時點是完整的。`module-version-gate` 是 **2026-08-01** 才引入的（`paas-app-bundle-cd#77` A′、PR#81／#82；本 repo landing 見 [`docs/tracking/landings/cd77-module-version-gate.md`](../../tracking/landings/cd77-module-version-gate.md)），它新增了第三處寫死的 pin —— **而 S1 段沒有跟著回頭更新**。上方 2026-07-25 那條註記刻意保留原文不改：它記錄的是「當時為真」，抹掉它就看不出這條縫的成因。CD repo 那側同步得比較完整（`docs/promote-acceptance-checklist.md` §1b、`values-prod.yaml` 該行上方註解、promote commit `9ee287e` 的 body 都寫了「三處同步」），**落差只發生在本 repo 的 SOP** ⇒ 兩份文件對同一個動作給出不同步數的指引，照 SOP 做的人會漏。

---

## 1. 系統全貌：repos / images / charts / CD

### 1.1 角色分工（5 個 repo + 上游）

| Repo | git-prod URL | 職責 | 產出 | CI 觸發 |
|------|-------------|------|------|---------|
| **woow_paas_platform**（本 repo） | `odoo-addons/woow_paas_platform` | Odoo 18 addon 原始碼 + `extra/paas-operator/` operator 原始碼 | ① 觸發 paas-odoo image build；② **直接 build operator image** | push `v*` tag → 觸發 odoo build；push `operator-v*` tag / 改 `extra/paas-operator/**` → build operator image |
| **paas-odoo-ci** | `woow-paas/paas-odoo-ci` | Build Odoo 應用 image（clone 本 addon at `addon_ref` 後打包進 Bitnami Odoo image） | `paas-odoo` / `paas-odoo-stg` / `cloud-odoo` image | 由 woow_paas_platform 跨-repo `workflow_dispatch`（`trigger-odoo-build.yml` 恆帶 `addon_ref`）；或 push `v*`/`stg-*`/`cloud-v*` tag。**⚠ 人工 `workflow_dispatch` 重跑必填 `addon_ref=v{版號}`**——留空的失效形態見 §9 坑 #12 |
| **paas-agent-runtime** | `woow-paas/paas-agent-runtime` | agent-runtime（per-project workspace pod）原始碼 | `agent-runtime` image | push `vX.Y.Z` tag → build + 對 charts repo 開 auto-bump PR |
| **woow-paas-charts** | `woow-paas/woow-paas-charts` | Helm **chart** 倉庫（只發 chart，不發 app） | `paas-operator` / `agent-runtime` / `vibe-kanban` chart | bump `Chart.yaml#version` → merge `main` → publish 到 Gitea Helm registry |
| **paas-app-bundle-cd** | `woow-paas/paas-app-bundle-cd` | **Rancher Fleet GitOps CD repo**（唯一上 prod 的入口） | Fleet 監控此 repo → `helm upgrade` | merge `main` → Fleet 自動 reconcile（無自動 promote） |

### 1.2 各 component 的 image 來源 / chart 來源 / CD bundle 對照

| Component | image 來源 | chart 來源 | Fleet bundle（prod） | prod namespace |
|-----------|-----------|-----------|---------------------|----------------|
| **Odoo（前台 /woow、後台 /odoo、portal /my）** | paas-odoo-ci → `paas-odoo` | **上游 Bitnami OCI** `odoo`（CD `fleet.yaml` 直接引用） | `paas-app-bundle-paas-odoo` | `paas-system` |
| **paas-operator** | woow_paas_platform `extra/paas-operator/` | woow-paas-charts `charts/paas-operator` | `paas-app-bundle-paas-operator` | `paas-system` |
| **PostgreSQL** | 上游 `bitnamilegacy/postgresql` | （隨 Bitnami Odoo chart 的 subchart） | （同 paas-odoo bundle） | `paas-system` |
| **Cloudflare Tunnel** | 上游 `cloudflare/cloudflared:2026.3.0`（已 pin） | **上游** `cloudflare-tunnel-remote`（CD `fleet.yaml` 引用，**已 pin `0.1.2`**） | `paas-app-bundle-cloudflare` | `paas-cloudflare-system` |
| **agent-runtime** | paas-agent-runtime → `agent-runtime` | woow-paas-charts `charts/agent-runtime` | **不由 Fleet 直裝**：由 operator runtime 動態 `helm install`（**prod 已啟用 + LLM 端到端驗證 2026-06-27**，見 §5.3） | `paas-ws-<hash>`（每 project 一個） |
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

## 2. 當前 prod 版本矩陣（2026-06-14 建立 · 2026-06-21 · 2026-06-25 · 2026-06-26 · 2026-07-10 複驗）

> ⚠️ 這是**快照**。發版後務必用本節最後的指令重新產生，別照抄。
> ⚠️ prod image tag 是 **mutable date-tag**（可能被重指）→ **判版本看 digest，不看 tag**。

### Prod（cluster `woow-k3s`）

| Component | NS | image:tag | digest（sha256，前綴） | chart | chart 版本 | pin 位置（paas-app-bundle-cd） |
|-----------|----|-----------|----------------------|-------|-----------|-------------------------------|
| paas-odoo | `paas-system` | `…/paas-odoo:20260710.0902`（addon `18.0.1.0.51`；#318/#322 訂閱聯動 + #324 計費雙缺口；commit 281cbae） | `4412ed69…c764`（manifest；pod imageID 因 prod+stg 共 cluster containerd digest dedup 報 stg repo 別名 `paas-odoo-stg:20260710.0349`，內容同此 digest） | Bitnami `odoo`（OCI） | `28.2.10` | image→`paas-odoo/values-prod.yaml` **三處**（`image.tag`、`image.digest`、`module-version-gate` initContainer 的 `image`，§5.1 S1 陷阱）；chart→`paas-odoo/fleet.yaml` `helm.version` |
| paas-operator | `paas-system` | `…/paas-operator:8577944`（short-SHA 非時間戳；#317 PVC keep install-time post-renderer，PR #321） | `35f0d1de…6885` | `paas-operator` | **`0.1.17`**（#315 limitranges RBAC，嚴格超集疊 0.1.16 上） | tag→`paas-operator/values-prod.yaml:8`；chart→`paas-operator/fleet.yaml` `version` |
| postgresql | `paas-system` | `bitnamilegacy/postgresql:17.6.0-debian-12-r4` | `92635613…0a8d` | （subchart） | — | `paas-odoo/values-prod.yaml`（postgresql 區塊） |
| cloudflared | `paas-cloudflare-system` | `cloudflare/cloudflared:2026.3.0` | `6b599ca3…31b0` | `cloudflare-tunnel-remote` | **`0.1.2`**（已 pin） | tag→`cloudflare/values-prod.yaml:8`；chart→`cloudflare/fleet.yaml` `version`（issue #19 已於 `a76517e` pin） |

### Stg 對照（同 cluster，靠 namespace 區隔）

| Component | NS | image:tag | chart 版本 |
|-----------|----|-----------|-----------|
| paas-operator-stg | `paas-stg-system` | `…/paas-operator:8577944`（digest `35f0d1de…6885`，與 prod 同版 parity） | `0.1.17` |
| cloudflared-stg | `paas-stg-cloudflare-system` | `cloudflare/cloudflared:2026.3.0` | `0.1.2`（已 pin） |
| odoo（stg） | — | **不在此 CD**：stg odoo 走 `dev-sandbox-stg`（見 [deploy-stg](../../../CLAUDE.md)） | — |

> **觀察（2026-07-10）：odoo `.48→.51` prod rollout 完成。** prod odoo 由 `20260708.0051`（18.0.1.0.48）升至 **`20260710.0902`（18.0.1.0.51，含 #318/#322 訂閱聯動 + #324 計費雙缺口）**；migration `-u` 0 error、pod digest `4412ed69…c764` 閉環、站點 `/woow /odoo /my /web/login` 全 200（詳見 memory `project_prod_rollout_20260710_51`）。**operator 本輪未動**：現役 **`8577944` / chart `0.1.17`**（`.49/.51` 沒碰 `extra/paas-operator`）——**取代下方 2026-06-25 觀察的 `0.1.16` / `20260624.1022` 快照**；自 #317 PVC keep（PR #321）起 prod/stg operator 仍維持同版 parity（皆 `8577944` / `0.1.17`，digest `35f0d1de…6885`）。**agent-runtime**：operator env 已帶完整 `AGENT_RUNTIME_*`（pin `1.4.2` / chart `0.4.3`、skillRunner on），約 2026-06-26 起 prod **enabled-dormant**（zero 租戶 spawn）——舊「prod 未啟用」認知作廢。

> **觀察（2026-06-25）**：prod operator 已 promote 到 chart `0.1.16` / image `20260624.1022`，**與 stg 同版**（含 **#207/#208** teardown 孤兒修復 + D1-D6 agent 生命週期 + epoch PVC GC 等）。先前「prod 落後 stg」的缺口已消除。**agent-runtime 在 prod 啟用屬 values 層（非 image/chart）**：~~2026-06-25 四向複驗仍未啟用~~ → **2026-06-26 已啟用並端到端驗證**：CD `paas-operator/values-prod.yaml` 加 `config.agentRuntime`（PR #33）→ operator 13 `AGENT_RUNTIME_*` env；`paas-odoo/values-prod.yaml` 加 5 `AGENT_RUNTIME_*` extraEnvVars（PR #34，secret `paas-odoo-agent-runtime`，auth/webhook 與 operator byte-match）；E2E 實證 spawn cold-start 8.5s → agent pod 2/2 Running → state-sync webhook 雙向 200（`spawning→active`）→ cleanup。**prod Bitnami chart `networkPolicy` 預設 allowExternal+allowExternalEgress 已通、無需改**（與 stg dev-sandbox 收緊 NP 不同）。`default_enable_agent_runtime=True`（新 project 自動開）。**LLM key 已設並端到端驗證（2026-06-27，REAL_LLM）**：`ai.config` id=3 WoowBot(MiniMax-M2.7) + `default_ai_assistant_id=2`/`default_ai_config_id=3`，live cold-start E2E WS 回真 LLM（見 §5.3）。詳見 plan `docs/plans/2026-Q2/2026-06-26-prod-agent-runtime-enablement.md`（含執行偏差）。落後幅度大時 promote 仍須先過 §9 坑 #11 的**無回歸超集 gate**。

> **觀察（2026-06-26）：prod PaaS addon 首次安裝上線。** 此前 prod `paas-system/paas-odoo` 的 `bitnami_odoo` DB **從未裝過 `woow_paas_platform`**（只有 37 Odoo core module、`/woow` 一直 404，非回歸；本 §2 與 §1.2「paas-odoo 提供 /woow」的假設此前其實**不成立**——prod 只跑 Odoo core/官網）。2026-06-26 經 **one-off install Job** 首裝成功：因 Bitnami chart 的 `ODOO_SKIP_MODULES_UPDATE=yes`、且 `--update=all` **不裝 uninstalled**、`--init=all` 只在 fresh DB 跑，既存 DB 安裝特定 addon 必須 `odoo -c /bitnami/odoo/conf/odoo.conf -d bitnami_odoo -i woow_paas_platform --stop-after-init`（scale 主 deploy 0 釋放 RWO PVC → Job → scale 1）。結果：module **37→81**、`woow_paas_platform`+6 depends 全 `installed`、`/woow` 上線（**303**）、core 無損、post_init 設 `default_agent_template_id`。執行 runbook：[`docs/plans/2026-Q2/2026-06-26-prod-paas-first-install-plan.md`](../../plans/2026-Q2/2026-06-26-prod-paas-first-install-plan.md)。pre-install snapshot `paas-odoo-pg-preinstall-20260626-0830` 保留。**注意**：① 換 image tag 與「addon 已 install」是兩件事——換 image 只換檔案、`-i` 才建 schema；② 判 prod odoo 版本看 deploy `.spec...image` + manifest digest（pod imageID 會報 stg 別名）；③ 10 個 ecpay/訂閱金流 cron 已 active，**ecpay 已設測試模式**（2026-06-26，見下「ECPay 金流」觀察）。詳見 memory `prod-odoo-paas-not-installed` / `prod-odoo-bitnami-module-mgmt`。

> **觀察（2026-06-26）後續：登入頁修復 + PaaS Operator 連線設定。** 首裝後撞兩個 gap，已修復：
> ① **登入頁表單隱形**——one-off install Job 留下 5 筆孤兒 asset attachment（`web.assets_frontend_minimal` / `web.assets_web` 等，DB 有記錄但 filestore 實體檔遺失）→ bundle 回 **500** → login `<form class="oe_login_form d-none">` 的 `d-none` 沒被 JS 移除 → 表單永遠隱形（curl HTML 端 200 完整、但瀏覽器空白）。修法：odoo shell 掃 `ir.attachment` url like `/web/assets/%`、對遺失檔者 `unlink()`+`env.cr.commit()`，下次請求自動重生（實測 500→200）。**預防**：one-off Job 須掛與 running deploy 同一 data PVC，或裝完清孤兒 asset。
> ② **PaaS Operator 連線**——prod Odoo 端原本**完全沒設**（CD `paas-app-bundle-cd` 沒設、addon 也不從 env 讀；pod 裡 `PAAS_OPERATOR_*` 是 K8s service-discovery 自動注入非設定，`res_config_settings.py:8-15` 從 `ir.config_parameter` 讀）。已寫 config：`operator_url=http://paas-operator`（同 ns service 短名）+ api-key（從 `paas-operator-secret` 灌，44 字元）。實測 `health_check()` healthy（helm v3.13.3）、authed probe 回 **403 業務驗證**（「Namespace must start with 'paas-ws-'」≠401 → key 正確）。**至此 prod PaaS 平台核心鏈路 Odoo→operator→K8s 打通、可運作。** 其餘部署 config（`namespace_prefix`/`deploy_env`/`paas_domain`）consumer 皆有正確 fallback（`paas-ws-`/`prod`/`woowtech.io`，見 `paas.py:117,2003`、`smart_home.py:155`、`security_access.py:210`），未寫 DB 也安全運作。**ECPay 金流已設測試模式（2026-06-26）**：`payment.provider`(ECPay) state `disabled→test`、憑證沿用 addon seed 的綠界官方測試特店 `3002607`（`IS_TEST_MERCHANT=True`，信用卡測試；HashKey/HashIV 各 16 碼測試值，未動）、`ecpay_domain` 佔位 `https://your_domain_name/`→`https://paas.woowtech.io/`（= `web.base.url`，ECPay 回調 base）；`topup_payment_enabled` 已開 `True`（2026-06-26，第二道閘，`billing.py:565` 用 `str2bool` gate）→ **兩道閘全開，/woow 儲值 checkout 走實際綠界測試付款**（測試卡 `4311-9522-2222-2222`、效期任意未來、安全碼 `222`）。**AI provider 憑證已設並驗證（2026-06-27）**：`default_ai_config_id=3`/`default_ai_assistant_id=2` + WoowBot Config api_key（MiniMax-M2.7）；legacy /woow chat 與 agent pod 兩路皆活證回真 LLM（見 §5.3）。

### 重新產生版本矩陣（一鍵）

```bash
for ns in paas-system paas-cloudflare-system paas-stg-system paas-stg-cloudflare-system; do
  echo "==== $ns ===="
  kubectl --context woow-k3s -n "$ns" get pods \
    -o jsonpath='{range .items[*]}{range .status.initContainerStatuses[*]}[init] {.image}{"  →  "}{.imageID}{"\n"}{end}{range .status.containerStatuses[*]}{.image}{"  →  "}{.imageID}{"\n"}{end}{end}'
done
# chart 釘版：直接看 CD repo
git -C /tmp/paas-app-bundle-cd pull 2>/dev/null || git clone https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd.git /tmp/paas-app-bundle-cd
grep -rnE 'version:|tag:|digest:|image:' /tmp/paas-app-bundle-cd/*/fleet.yaml /tmp/paas-app-bundle-cd/*/values-prod.yaml
```

> 🔴 **上面兩條的 `initContainerStatuses` 與 `digest:|image:` 都是必要的，不是順手加的**：`paas-odoo` 的 image pin 有**三處**（§5.1 S1 陷阱），第三處是 `module-version-gate` **initContainer** 寫死的 `image:`。
> 舊版本這兩條指令**對它同時盲**——pod 側只列 `containerStatuses`（照不到 init 容器實際跑的那顆）、檔案側只 grep `version:|tag:`（gate 那行的前綴是 `image:`，`digest:` 也一併漏）。**盤點指令漏掉某個 pin，比沒有盤點指令更糟**：它會產生一份看起來完整的清單。
>
> ⚠ **這裡的 `initContainerStatuses[*]` 是對的，但不要把這個寫法搬去做「gate 與主容器是否同一顆」的比對。** 兩者目的不同：
>
> | 用途 | 寫法 | 為什麼 |
> |------|------|--------|
> | **盤點**（本節） | `initContainerStatuses[*]` | 要列出**全部** init 容器 —— `paas-odoo` 有兩個（`patch-odoo-conf` 用 `busybox:1.37`、`module-version-gate` 用 paas-odoo），**busybox 那顆也是一個 pin**，本來就該出現在清單裡 |
> | **相等性比對**（§5.1.5 ①） | `initContainerStatuses[?(@.name=="module-version-gate")]` | 用 `[*]` 會把 busybox 的 digest 一起印進同一行 ⇒ **與主容器永遠不可能逐字相同 ⇒ 永遠報紅**，連 pin 完全同步時都紅 |

---

## 3. 前置條件與存取

| 項目 | 設定 | 驗證指令 |
|------|------|---------|
| **Tailscale VPN** | 連 woow-k3s 前必須 active（斷線會在數分鐘後才以 kubectl timeout 浮現，半途斷線最痛） | `tailscale status >/dev/null 2>&1 && echo OK \|\| echo "VPN 未連"` |
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
   - **image tag** → `<component>/values-<env>.yaml` 的 `image.tag`。⚠️ **`paas-odoo` 的 image pin 是三處**（`image.tag` ＋ `image.digest` ＋ `module-version-gate` initContainer 寫死的 `image`），**三處必須同進同退**：digest 優先於 tag（只改 tag = no-op）；gate 那顆漏改則 gate 讀舊版號、安靜跳過該跑的 `-u`（§5.1 S1 陷阱，含三種漏法的失效形態表）
   - **chart version** → `<component>/fleet.yaml` 的 `helm.version` 行（operator 與 odoo OCI **同一 shape**；odoo 的 `helm.chart` 只指向 OCI 來源 `oci://…/odoo`，版本仍在獨立的 `version:` 行，**不是內嵌在 chart 字串裡**）
4. **改 image 與改 chart 是兩件事**。只改 image tag（values）不動 chart；但若新版需要 chart 把新設定 passthrough 成 env，**values-only 無效，必須先 bump chart**（見 §9 坑 #5）。
5. **prod image tag 可變** → 驗證一律比對 **pod imageID（digest）**，不是看 tag 字串。
6. **prod 與 stg 同一 cluster、靠 namespace 區隔**。tunnel / 金鑰 / namespacePrefix 等 **prod 專屬欄位**搬版時不可被 stg 值覆蓋（見 §6）。

---

## 5. 各 component 上 prod SOP

> 通用前置：先確認要上的 image **已在 JCR**、chart **已在 Gitea Helm registry**。指向不存在的版本 → Fleet `helm install` 直接失敗（§9 坑 #1）。

### 5.1 Odoo（paas-odoo）上 prod

**何時**：本 addon（`src/`）改動要上 prod。

> **🔴 步驟 0：pre-flight 起點實查——發版單本身是要被查證的對象，不是依據**
>
> 發版單（或前一輪留下的 note）寫的「prod 現役版本」「本次要發什麼」**都是快照，會過期**。三次實例各壞在不同地方：
>
> | 壞在哪 | 實例 | 後果 |
> |---|---|---|
> | **起點版本抄舊 note** | 發版單寫「現役 `.73`」，實為 `.75`（熱修已上線） | 範圍多算 9 個 commit、把已上線的東西列為本次交付、回滾指令指到錯的版本 |
> | **只查發版單點名的 pin** | 發版單把某個 operator 修復列為本次交付，實際它走 operator 管線早已獨立上線 | 誤列（若照著回滾會動到不該動的東西） |
> | **漏列另一條管線** | 發版單 §2 只寫 image，漏了 operator **chart**；而該 chart 版本才補上新商品開通所需的 ClusterRole 權限 | 租戶付款後開通必敗，且無訊號 |
>
> ⇒ **pre-flight 三條，缺一不可：**
>
> 1. **起點一律以 live 實查為準**：prod DB `ir_module_module.latest_version` ＋ pod imageID **digest**，兩者交叉。**不要用發版單、note 或前一輪的紀錄當起點。**
> 2. 🔴 **獨立查 CD repo 的近期 commit，而不只查發版單點名的那些 pin**。「照發版單逐項驗收」**擋不掉誤列**——被誤列的那一項在發版單上看起來完全正常，只有比對 CD main 的實際歷史才會發現它早就上線了（或根本屬於另一條管線）。做法：`git -C <cd> log --oneline -15 main` 逐條對照本次範圍。
> 3. **本 repo 有兩條獨立發版線**（`src/**` → paas-odoo image；`extra/paas-operator/**` → operator image；operator chart 又是第三個軸）。**「merge 進 develop」≠「隨下次 paas-odoo 發版上線」**——按變更路徑分流，不要按 commit 落在哪個版號區間分流。
>
> （第 2 條的價值在於：即使該現象**已經記在文件裡**，那一輪仍是靠實查 CD main HEAD 才發現的。⇒ 知道有這回事不能取代每次實查。）

> **🔴 S1 陷阱：paas-odoo 的 image pin 有 <ins>三處</ins>，必須「同進同退」**
>
> `paas-odoo/values-prod.yaml` 裡指向 paas-odoo image 的 pin **是三處，不是兩處**：
>
> | # | 位置 | 檔內形態 | 誰讀它 |
> |---|------|---------|--------|
> | ① | `image.tag` | `  tag: "YYYYMMDD.HHMM"` | 名義上的版本標示；**渲染時被 ② 蓋掉，本身不決定跑什麼** |
> | ② | `image.digest` | `  digest: "sha256:…"` | **主容器**真正跑的那顆（Bitnami common 以 `repository@digest` 渲染，**digest 優先於 tag**；prod Deployment 的實際 image 字串**完全沒有 tag 段**） |
> | ③ | `initContainers[].image`（`name: module-version-gate`） | `    image: <registry>/<repo>@sha256:…`　**整串寫死**，前綴是 `image:` 而**不是** `digest:` | **`module-version-gate` initContainer 自己**跑的那顆 |
>
> ③ 為什麼是寫死的、不是 Helm 模板：原本寫成 `{{ include "odoo.image" . }}` 讓它自動等於主容器（改一處就好），但 **Fleet 會在 Helm 之前對 values 檔再做一層自己的 Go template 前處理**，把 Helm 的雙大括號吃掉（連 YAML 註解裡的也算）——實測 bundle 直接 `Ready=False`（`failed to parse helm values template`）。所以只能寫死，**這是 cd#77 A′ 唯一新增的「要記得同步」的點**，CD repo 已寫在該行上方註解並列入其 `docs/promote-acceptance-checklist.md` **§1b**。
>
> **🔴 漏改哪一處，失效形態不同——但三種形態的訊號全都是綠的（PR merged ✅ / Fleet reconcile 綠 ✅ / bundle ready ✅ / 站點 200 ✅）：**
>
> | 漏了誰 | 主容器 image | gate image | DB 版號 | 機制 | 結果 |
> |--------|-------------|-----------|--------|------|------|
> | **promote 只改 ①**（漏 ②③） | **舊** | 舊 | 舊 | digest 優先於 tag ⇒ 主容器根本沒換 image | 整個 promote 是 **no-op** |
> | **promote 改 ①②、漏 ③** | 新 | **舊** | **舊** | gate 讀的是**自己那顆（舊）image** 內 `__manifest__.py` 的版號，與 DB 的舊版號**相等** ⇒ 判定「版本一致」而 `skip` 掉該跑的 `-u`；主容器的 `ODOO_SKIP_MODULES_UPDATE=yes` 也**不會**補跑 | **新 code 跑在舊 schema／舊 seed 上**（要等功能驗收才看得出來） |
> | **rollback 只退 ①②、漏 ③**（§8） | 舊 | **新** | **新** | 上一列的鏡像：gate 讀自己那顆（新）image 的版號，與 promote 時已推進的 DB 版號**相等** ⇒ 同樣 `skip` | **舊 code 跑在新 schema／新 seed 上** |
>
> 注意第二、三列**不是** S1 原本那個 no-op：image 確實換了、pod imageID digest 確實是新的，**「CI digest ＝ pod imageID ＝ CD pin」三方閉環照樣全過**——漏的是 gate，而 gate 的失效方式是「安靜地判定不必做事」，不是報錯。
>
> → 下表**步驟 6 必須 ①②③ 三處同改**，diff 裡要同時看到 `tag:`／`digest:`／gate 的 `image:` **三行**；**rollback（§8）必須三處同退**，理由見上表第三列。

> **⚙️ module upgrade：先判斷 seed 變更落在哪個 addon，再決定走哪條路**
>
> prod odoo 是 **Bitnami chart**，`ODOO_SKIP_MODULES_UPDATE` 預設 **`yes`** → **換 image 後 boot 不會自動跑 `-u`** → **`noupdate=0` 的 seed XML data 不會 reload**（新 template、chart pin、方案價格等 seed 變更**全部不生效**：image 換了、版號跳了，功能卻像沒改）。
>
> **自 2026-08-01 起，`woow_paas_platform` 這一個模組已由 CD 的 `module-version-gate` initContainer 自動處理**（`woow-paas/paas-app-bundle-cd#77` A′、PR#81／#82）。gate 在主容器啟動前比對 **gate 自己那顆 image 內**的 `__manifest__.py` 版號與 DB `ir_module_module.latest_version`，不一致就跑 `odoo-bin -u woow_paas_platform --stop-after-init`，跑完再讀一次 DB 版號複驗，不相等就 `exit 1` 讓 pod 卡在 Init（詳見 §5.1.5）。
>
> 🔴 **「gate 自己那顆」不是修辭**：gate 的 `image:` 是 values 檔裡**第三處獨立的 pin**（S1 陷阱 ③），與主容器的 `image.digest` 是兩個各自為政的字串。**下面所有「gate 會自己處理」的結論，隱含前提都是「gate 的 image 已跟主容器一起 promote 到同一顆」** —— 前提不成立時，gate 比對的是舊 image 的版號，會安靜地判定「不必升級」。
>
> 🔴 **但兩者的涵蓋範圍不同，不能把 gate 讀成「兩段式作廢」**：
>
> | | 涵蓋範圍 |
> |---|---|
> | `module-version-gate` | **只有 `woow_paas_platform` 一個模組**（指令裡寫死模組名） |
> | `ODOO_SKIP_MODULES_UPDATE` | Bitnami entrypoint 的**全域**開關，涵蓋所有已安裝模組 |
>
> ⇒ **判準：發版前看 seed diff 落在哪個 addon。**
>
> - **變更全部落在 `woow_paas_platform/` 內** → gate 自己會處理，**不需要** PR-A／PR-B 的 `SKIP` 來回，也就沒有 §5.1.2 的曝險窗。照舊 SOP 多開那兩張 PR，等於**白開一個 ≤30 分鐘的曝險窗**。
>   > 🔴 **這一條有一個必須自己成立的前提：gate 的 image（S1 陷阱 ③）也被一起 promote 了。** 漏掉它時，這裡的「gate 自己會處理」會直接變成假話——而且是**最安靜的那種假**：gate 有跑、log 說 `skip`、pod 正常起來、三方 digest 閉環全過，只有 DB 版號還停在舊版。**把 ③ 當成「跟 `image.digest` 同一個動作的一部分」，不要當成 gate 的內部細節。**
> - **變更落在相依 addon**（`sh_subscription` 系列、`ecpay` 系列等，見 `src/__manifest__.py` 的 `depends`）→ **gate 不會碰它們**，仍須走下方兩段式。
> - **判斷方法**：`git diff <上一版 tag>..<本次 tag> --name-only | grep -v '^src/'`——命中非本 addon 的 addon 路徑就走兩段式；不確定就當作要走（多跑一次 `-u` 是冪等的，漏跑是靜默失效）。
>
> ⚠ **未驗**：`SKIP="no"` 時 Bitnami entrypoint **實際會升哪些模組**（全部已安裝模組？只有 `-u` 清單？）**沒有人實測過**。上表的「涵蓋所有已安裝模組」是依 entrypoint 語意的推斷，不是實測結論。真要依賴這條做決策前請先實測並回填。
>
> **兩段式（相依 addon 有 seed 變更時）**：
> **PR-A（promote）**：換 `image.tag` ＋ `image.digest` ＋ **gate initContainer 的 `image`**（三處同改，S1 陷阱），並設 `ODOO_SKIP_MODULES_UPDATE="no"`（boot 自動跑 `-u`）
> → **驗證** → **PR-B（收官）**：切回 `"yes"`。
>
> 兩張 PR 之間是**曝險窗，有硬性時限**，見 §5.1.2。

```
步驟                                                      驗證
────────────────────────────────────────────────────── ──────────────────────────────
0. pre-flight 起點實查（見上方「步驟 0」引言）           DB latest_version + pod digest
   ⚠ 不要用發版單/note 當起點；另獨立掃 CD 近期 commit    git -C cd log --oneline -15 main
1. 本 repo bump src/__manifest__.py version              grep version src/__manifest__.py
   （格式 18.0.{major}.{minor}.{patch}；現行 prod 版號
     見 §2 版本矩陣——勿在本表硬編，會腐化）
2. commit "release: 18.0.1.0.XX" → push
3. git tag v18.0.1.0.XX && git push origin v18.0.1.0.XX  （= /woow:deploy 自動化此段）
   → 觸發 trigger-odoo-build.yml → dispatch paas-odoo-ci
4. 等 paas-odoo-ci build 完成，取得 image tag 與 digest  CI log 的 image_tag=YYYYMMDD.HHMM
   （tag = UTC date，非 addon 版號）                     ＋該 tag 的 sha256 digest
   ⚠ image_tag / digest 驗不出「烤到別的 ref」——         ＋build log 三行錨點都對上本次版號：
     必查 build log 的三行錨點，見 §5.1.4                at ref: / Cloned addon version: /
                                                         Image addon version:
5.〔人類操作者親跑〕建發版前 DB snapshot：pg + data      兩顆皆 readyToUse=true
   兩顆 PVC 都要，命名                                   且 markRemoved=false
   paas-odoo-{pg,data}-pre{版號}-{YYYYMMDD}              （只看 list 有東西會誤判）
   未確認 readyToUse 前不得進步驟 6 —— 見 §5.1.1
6. PR-A（promote）：paas-app-bundle-cd 開 branch，改     git -C cd diff 必須同時出現
   paas-odoo/values-prod.yaml 的 三處 image pin：        tag: / digest: / gate 的 image:
   ①image.tag ②image.digest ③module-version-gate         三行（gate 那行前綴是 image:
   initContainer 的 image（三處同改，缺一 = 靜默失效，     不是 digest:，grep 別漏）
   三種漏法各有不同失效形態，見上方 S1 陷阱表）
   ⚠ SKIP 開關「只在 seed 變更落到相依 addon 時」才設
   "no"；只動 woow_paas_platform 者由 module-version-gate
   自理，多設 = 白開曝險窗（判準見上方引言）
7. tea PR（zh-tw）→ merge main                           tea pr list
8. 等 Fleet reconcile（~30–60s）                         見 §7
   ⚠ 單副本 Recreate：本步必有 3–10 分停機、502 屬預期    §9 坑 #14
9. 驗證 prod pod 跑新 digest                             見 §7「pod digest」
   ＋ §5.1.5 三層驗收：①gate 有跑 ②-u 無錯               ③psql 讀回本次 seed 欄位
   （②的 Updating modules 輸出取不到，用替代證據鏈）      —— ③ 沒有任何自動化涵蓋
10. 站點 smoke：/woow /odoo /my                          見 §7 (6)；fail → §8 rollback
11.〔僅當步驟 6 設過 SKIP="no"〕PR-B 收官：改回 "yes"    驗證通過後 30 分鐘內 merge
    → merge main → 等 Fleet reconcile                    （曝險窗規則見 §5.1.2）
12.〔同上條件〕三重確認 SKIP 已回 "yes"                  見 §5.1.3 收官三重確認
    （deployment spec / pod env / Fleet 無 drift）
```

> **判斷要不要動 `ODOO_SKIP_MODULES_UPDATE`，看兩軸**（都要看 diff，不是憑印象）：
>
> | 有沒有 `noupdate=0` seed／schema 變更 | 變更落在哪 | 做法 |
> |---|---|---|
> | 沒有（純 Python / OWL 邏輯） | — | 不設 `"no"`、步驟 11/12 免做。⚠ 版號仍會 bump ⇒ gate 仍會跑一次 `-u`，那是冪等的、不影響 |
> | 有 | 只在 `src/`（＝`woow_paas_platform`） | **不設 `"no"`**——gate 自己會處理（§5.1.5）。多設一輪等於白開曝險窗 |
> | 有 | 含相依 addon（`sh_subscription`／`ecpay` 等） | 走完整兩段式（步驟 6 設 `"no"`、11/12 收官） |
>
> **不確定就當作「含相依 addon」走完整兩段式**——多跑一次 `-u` 是冪等的，漏跑是靜默失效。

> **odoo chart（Bitnami `28.2.10`）** 平常不動；只有要升 Odoo 框架本身時才改 `paas-odoo/fleet.yaml` 的 `helm.version`（**高風險**：schema / addon 相容性，須先 stg / sandbox 驗證穩定，**勿與 image bump 同 PR**）。

#### 5.1.1 🔴 發版前 DB snapshot（步驟 5；人類操作者親跑）

**為什麼是「正式步驟」而不是「保險」**：本 SOP 把 `-u` 正式入表後，**PR-A 一 merge 就會跑 schema migration**，而 **§8 rollback 只能退 image、退不了 schema**（image 換回舊版，DB 裡已經改掉的 schema / seed data 不會跟著回去）。沒有 DB 錨點的話，promote 就是一道**單向門**。

更關鍵的是：**prod DB 沒有離線備份**。2026-07-25 實查 —— `backups.longhorn.io` **無任何資源**、backuptarget **未設定**。也就是說 **snapshot 是唯一的還原錨點**，不是多一層保險。

**兩顆都要建**（只做 pg 會漏掉附件 / filestore，還原時湊不成一致狀態）：

| 對象 PVC | snapshot 名（本次實例） | 本次大小 |
|---------|----------------------|---------|
| PostgreSQL 資料　`pvc-9aa6ed19-5798-40b5-8689-7a7ab910a67d` | `paas-odoo-pg-pre78-20260725` | 200 MB |
| Odoo filestore data | `paas-odoo-data-pre78-20260725` | 13 MB |

**命名慣例**：`paas-odoo-{pg,data}-pre{目標版號}-{YYYYMMDD}`（例：2026-07-25 升 `.78` → `paas-odoo-pg-pre78-20260725`）。讓「這顆對應哪一次發版」自解釋 —— 事故當下沒有時間猜哪顆能用。

> **🔴 建完必須驗 `readyToUse=true` 才算數 ——「有一顆 snapshot」≠「有還原點」。**
>
> 血淚實例（2026-07-25）：`.78` 發版前查到「最新那顆」snapshot `paas-odoo-pg-pre75-20260723-1334`，實際是 **`markRemoved=true` / `readyToUse=false` ＝不可用**。當時真正可用的最新錨點是 07-20 的 `pre73`，schema 已經早於現行 prod。**只看 list 有沒有東西就會誤判成「有備份」**，必須逐顆讀 `readyToUse` / `markRemoved`。

```bash
# 參考查法（Longhorn 原生 snapshot CR；CR 實際位置以現場為準）
kubectl --context woow-k3s -n longhorn-system get snapshots.longhorn.io \
  -o custom-columns='NAME:.metadata.name,VOLUME:.spec.volume,READY:.status.readyToUse,REMOVED:.status.markRemoved' \
  | grep pre78     # ← 換成本次的 pre{版號}
#   兩顆都必須 READY=true 且 REMOVED=false
#   任一不符 = 本次發版沒有還原錨點 → STOP，不得進步驟 6
```

> **由人類操作者親跑，agent 不代勞。** prod DB 零離線備份，建 snapshot 屬不可逆風險操作，與 CI 的 `/approve` 人工核可、CD PR merge **同級**。
> agent 的職責僅止於：**確認兩顆 snapshot 都 `readyToUse=true` / `markRemoved=false` 之後**才可繼續步驟 6；沒確認到就 STOP、回報，不要自行往下走。
> （另一個結構性理由：Longhorn snapshot CR 不在本 repo kubectl 可操作的 7 個 namespace 內 —— 見 `CLAUDE.md`「共用叢集鐵律」，非平台 namespace 只可唯讀盤點、嚴禁寫入。）

#### 5.1.2 ⏱ `-u` 曝險窗（PR-A → PR-B 之間）：上限 30 分鐘

PR-A merge 後、PR-B merge 前，`ODOO_SKIP_MODULES_UPDATE="no"` 是 prod 的**宣告態**。這段期間 **任何 pod 重啟都會再跑一次 module update** —— OOM kill、節點驅逐 / 維護、probe 失敗重啟、手動 `rollout restart`、其它 PR 觸發的 Fleet upgrade，全部算數。也就是：**一次非預期、沒有人在旁邊看的第二次 `-u`**，migration 期間 worker 被佔住、服務降級或不可用，而且發生時間完全不受控。

> **規則：驗證通過後 ≤ 30 分鐘內 merge PR-B。**
> 這是**可稽核的硬上限**，不是「盡快」—— 超過 30 分鐘即視為破線，停下手邊其它事先把 PR-B 收掉，**不得放著過夜或跨班**。

實測基準（用 helm revision 時戳量，`helm -n paas-system history paas-odoo`）：

| 日期 | 曝險窗 | 佐證 | 判定 |
|------|-------|------|------|
| 2026-07-25 | **11 分鐘** | paas-odoo helm `rev57 09:07:21Z`（PR-A）→ `rev58 09:18:43Z`（PR-B） | ✅ 合格，即本上限的參考做法 |
| 2026-07-23 | **約 61 分鐘** | 同一量法 | ⚠️ 超標 —— 這次超標即為訂定本規則的動機 |

> 若 30 分鐘內驗證還沒做完（migration 特別久、或撞到需要調查的異常），**不要靠拖長窗口等它好**：先 merge PR-B 把宣告態收回 `"yes"`，再繼續查。收回 `"yes"` **不會**回退已經跑完的 migration，只是讓下一次 boot 不再自動 `-u`；真要退版走 §8。

#### 5.1.3 收官三重確認：`ODOO_SKIP_MODULES_UPDATE` 已回 `"yes"`

PR-B merge + Fleet reconcile 後**必做，三條缺一不可**。三條各證一件**不同**的事：

| # | 查什麼 | 證明什麼 |
|---|-------|---------|
| ① | deployment spec 的 env | **宣告態已生效到 workload** |
| ② | running pod 的實際 env | **現役 pod 真的吃到**（spec 對但 pod 還沒滾 → 這裡會不一致） |
| ③ | Fleet BundleDeployment `nonModified=true` | ①② 是 **git 宣告態**、不是手改 live 湊出來的漂移 |

```bash
CTX=woow-k3s

# ① deployment spec（宣告態）
kubectl --context $CTX -n paas-system get deploy paas-odoo \
  -o jsonpath='{range .spec.template.spec.containers[*].env[?(@.name=="ODOO_SKIP_MODULES_UPDATE")]}{.name}={.value}{"\n"}{end}'
#   期望：ODOO_SKIP_MODULES_UPDATE=yes

# ② running pod 的實際 env（現役 pod 真的吃到）
kubectl --context $CTX -n paas-system exec deploy/paas-odoo -- printenv ODOO_SKIP_MODULES_UPDATE
#   期望：yes

# ③ Fleet BundleDeployment 無 drift（證明 ①② 來自 git 宣告態）
kubectl --context $CTX get bundledeployment -A \
  -o custom-columns='NAME:.metadata.name,NONMODIFIED:.status.nonModified,READY:.status.ready' | grep paas-odoo
#   期望：NONMODIFIED=true（false = live 與 git 有 drift，須排查，不得視為收官完成）
```

**為什麼三條都要**：只驗 ① 會漏「pod 還沒滾、現役 pod 仍帶 `"no"`」；只驗 ①② 會漏「有人 `kubectl set env` 手改 live 湊出來的假綠」—— 那種狀態下一次 Fleet reconcile 會把它打回 `"no"`，曝險窗其實從沒關上。三條全過才算收官。

#### 5.1.4 🔴 build 產物驗收與 CI 重跑（步驟 4）

**要驗的不是「build 有沒有成功」，是「image 裡裝的是不是本次那個 ref 的 addon」。** 這兩件事在 CI 上完全無法互相佐證。（能驗到的是**版號**這一層，不是「內容完全正確」——見本節末「已知極限」。）

##### 為什麼 image_tag / digest 驗不出來

paas-odoo-ci 的 `build-image.yml` 吃一個 `addon_ref` 輸入決定 clone 本 repo 的哪個 ref。**正式發版路徑是安全的**：push `v*` tag → `.gitea/workflows/trigger-odoo-build.yml:14,26` 從 tag 名推導 `TAG_REF` 並恆帶 `addon_ref`，結構上不可能為空。

危險的是**人工 `workflow_dispatch` 重跑忘了填 `addon_ref`**（例如下方「CI 失敗要重跑」的情境）。留空時 CI 會 clone 本 repo 的**預設分支**，而本 repo 預設分支是 `develop`：

| 你以為 | 實際 |
|--------|------|
| image 內容 = `v18.0.1.0.XX` 那個 tag | image 內容 = **當下移動中的 `develop` HEAD** |
| 可重現（tag 是不可變錨點） | **不可重現**，且含尚未發版的 commit |
| 發版紀錄「image X ＝ v18.0.1.0.XX」成立 | 發版紀錄與 image 實際內容脫鉤，**溯源整條斷掉** |

而且這個「預設分支是什麼」**不在版控裡**——它是 Gitea 的 repo 設定，CI 零校驗。今天指向 `develop`，哪天被改成 `main` 就直接變成**版號倒退**（`main` 長期落後 `develop`），一樣沒有任何訊號。

**這條路徑上 CI 全綠、`image_tag` 是新的、`digest` 也是新的**（新 build 本來就產生新 digest）—— §9 坑 #4「看 tag 判版本 → 改看 digest」對這個缺陷**同樣無效**，因為兩者都只證明「有 build 出東西」，不證明「build 的是誰」。不驗 ref 的話，要到 pod 起來跑 `-u` 才會發現裝進去的是別的版本。

##### 驗收：build log 的三行錨點

```
# 在 paas-odoo-ci 的 build run log 裡找這三行（缺任一 = 不得進步驟 5）
Cloning woow_paas_platform at ref: v18.0.1.0.XX                      ← clone 的實際 ref，必須 == 本次發版 tag
Cloned addon version: 18.0.1.0.XX (from ref: v18.0.1.0.XX)           ← source 端：該 ref 的 src/__manifest__.py 版號
Image addon version: 18.0.1.0.XX (matches clone, ref: v18.0.1.0.XX)  ← image 端：從產出 image 反讀，CI 已比對通過
```

> **看 `at ref:` 這一行，不要看 `at HEAD`**——後者只說明 clone 完停在該 ref 的 HEAD，不告訴你 ref 是誰。
>
> ⚠ **第二行的字串在 2026-08-02 改過名**（paas-odoo-ci #50）：原本叫 `Built addon version:`，**該字串現在不會出現**。改名的理由正是它誤導——`baked` 讓人以為 image 端已經驗過了，其實那時只讀到 clone 原件。任何還寫著舊字串的文件或筆記都是過期的。

**三行各自證明什麼**（照著念，不要外推）：

| 行 | 層 | 證明 |
|---|---|---|
| `Cloning … at ref:` | source | clone 的是指定的那個 ref |
| `Cloned addon version:` | source | 該 ref 的 `src/__manifest__.py` 版號是這個數字 |
| `Image addon version:` | **image** | **產出 image 裡那份 addon source 的版號 == 上面 clone 側的版號** |

- **前兩行同源、不互相獨立**：都出自**同一次 `git clone`**。`Cloned addon version:` 讀的是 `/tmp/woow_paas_platform/src/__manifest__.py`（clone 原件，連 build context 裡那份複本都不是），step 跑在 `docker build` **之前**。它們合起來只涵蓋 source 端。
- **第三行才是 image 端錨點**（paas-odoo-ci #50 新增）：`Verify image addon version matches clone (image side)` step 用 `docker run --rm --entrypoint cat` 從**剛 build 出來的 image** 讀 `/opt/bitnami/odoo/custom-addons/woow_paas_platform/__manifest__.py`，與 clone 側版號比對。
- **第三行是 CI 側的 fail-closed gate，不是回報**：四種情況都 `exit 1` —— 版號不符／讀不到 image 內 manifest（＝`COPY` 路徑或 build context 錯）／讀得到但解析不出版號／clone 側基準缺失。**位置在 `Push image` 之前，所以不符的 image 根本進不了 registry。**

> 🔴 **三行都要跟「本次預期的版號」比，不是比「三行互不互相一致」。**
>
> `Image addon version:` 那句 `matches clone` 只證明 **image == clone**，**不證明 clone 就是你要的那個 ref**。
> 兩者正交：#50 擋的是「image 內容與 clone 不一致」，「clone 錯 ref」是 #46 的 `addon_ref` fail-closed 在擋。
>
> 具體會怎麼騙過你：如果 `addon_ref` 填成 `develop`（或打錯字填到別的存在的 ref），CI 會 clone 到它、
> image 也照著它烤——於是 `Cloned addon version:` 與 `Image addon version:` **完美相符、gate 全綠**，
> 但兩邊都是 `18.0.1.0.89`，而你要發的是 `.88`。這種情況下**唯一會露餡的是 `at ref:` 那一行**
> （顯示 `develop` 而不是 `v18.0.1.0.88`），以及把版號拿去跟發版單核對這個動作。
> `addon_ref` 的 fail-closed 只擋**留空**，擋不了**填錯**。

##### 已知極限：錨到的是「版號」這一層

image 端現在有錨點了（第三行），「烤錯 ref」與「addon 根本沒進 image」這兩類已由 CI 擋在 push 之前。但它比對的是**版號字串**，所以下面三件事**仍然證明不了**：

| 仍然證明不了 | 為什麼 | 由誰涵蓋 |
|---|---|---|
| registry 上**之後被 pin 的**是不是本次驗過的那一顆 | 驗的是 build 當下那顆 image，之後 CD 去 pin 哪一顆是另一件事 | **digest pin**（§5.1 步驟 6：`tag`／`digest`／gate `image` **三處**同改；§9 坑 #4、#21） |
| **版號以外的內容** | 同一個版號但檔案被動過（改了 code 沒 bump 版號），兩邊版號字串照樣相等 | 無自動檢查——靠 code review 與版號紀律 |
| **addon 以外的東西** | 只讀 `woow_paas_platform` 的 manifest；vendored `addons/`、`requirements-paas.txt` 不在比對範圍 | 無自動檢查 |

**所以：三行錨點消滅的是「image 裡裝的是別的 ref 的 addon」這一類，不是「image 內容完全正確」。** 發版驗收時不要把前者的通過讀成後者的保證——那正是本節開頭那句「驗的不是 build 有沒有成功」想擋的思維錯誤，換一層再犯一次而已。

##### CI 失敗要重跑時

**重跑一律必填 `addon_ref`。** 目前 paas-odoo-ci 已對此 **fail-closed**：`addon_ref` 留空且非 `stg-*` 路徑 → job 直接 `exit 1`，要求呼叫端明示 ref（paas-odoo-ci issue #46）。所以重跑時看到 job 秒失敗並抱怨缺 `addon_ref`，那是**設計行為，不是 CI 壞掉**——補上 ref 重跑即可。

常見要重跑的情境與正確做法：

| 情境 | 做法 |
|------|------|
| build 本身失敗（依賴、registry、runner 問題） | Gitea UI 對 `build-image.yml` 做 `workflow_dispatch`，**`addon_ref` 填 `v{本次版號}`**、`tag` 填新的 `YYYYMMDD.HHMM` |
| approval gate 撞 Gitea 5xx 讓 job 陣亡 | 同上。注意 gate 可能**已經把 issue 建出來了**，先看有沒有孤兒 approval issue，別重複建 |
| 想重推 tag 觸發 | **不要**——`v*` tag 已存在，重推不會重新觸發，且 §9 坑 #9 的手打 tag 風險照樣存在。走 `workflow_dispatch` |
| job 在 `Verify image addon version matches clone (image side)` 掛掉 | **這不是漏填參數，補 `addon_ref` 重跑沒有用。** 它代表 image 裡的 addon 與本次 clone 的 source **真的不一致**——照錯誤訊息查 Dockerfile 的 `COPY` 來源／build context／`.dockerignore`／layer cache。壞 image 已被擋在 `Push image` 之前，registry 乾淨，不必清理 |

> 兩道 fail-closed 要分清楚：**`Validate addon ref`（#46）失敗＝你少給了輸入**，補上重跑即可；**`Verify image addon version …`（#50）失敗＝build 出來的東西真的不對**，重跑只會再失敗一次。

> ⚠ `stg-*` 路徑（`/woow:deploy-stg`）**刻意保留** 留空 → `develop` 的 fallback：`deploy-stg.sh:298` 建 tag 時從不傳 `addon_ref`，完全依賴這個 fallback。fail-closed 只作用在非 `stg-*` 路徑。

失效形態的完整敘事見 [`docs/tracking/pitfalls/ci-addon-ref-empty-drifting-default-branch.md`](../../tracking/pitfalls/ci-addon-ref-empty-drifting-default-branch.md)。

#### 5.1.5 🔴 `-u` 真的生效了嗎：三層驗收（缺一層就有靜默失效）

「模組升級成功」不是一個信號，是**三件會各自獨立失敗的事**。三層的失效形態都是靜默的，而且**上層通過不蘊含下層通過**：

| 層 | 問的問題 | 誰負責 | 不驗會怎樣 |
|---|---|---|---|
| ① | `-u` **有沒有被觸發** | `module-version-gate`（cd#77，2026-08-01 上 prod）**——前提：gate 自己的 image 已與主容器同步，見 S1 陷阱 ③** | image 換了、版號跳了、功能像沒改 |
| ② | `-u` **跑完有沒有出錯** | 人工看 boot log 的證據鏈 | migration 半途失敗但 pod 起來了 |
| ③ | **seed 值有沒有真的寫進 DB** | **人工 psql 讀回**（本節重點） | ①② 全綠、pod 正常、seed 值仍是舊的 |

##### ① gate 做了什麼、以及它的 post-verify 只保證什麼

gate 是 `paas-odoo` deployment 的 **initContainer**（不是 sidecar、不是 postStart），所以它在主容器啟動**之前**跑完：

1. 讀 **gate 自己這顆 image 內**（＝ S1 陷阱 ③ 那個寫死的 pin，**不是**主容器那顆）`__manifest__.py` 的版號、讀 DB `ir_module_module.latest_version`（只認 `state='installed'`）；
2. 兩者相等 → `skip`；DB 無 installed 記錄 → `skip`；**讀不到 image 版號 → `exit 1`**（寧可擋住 pod 也不靜默跳過）；
3. 不相等 → `odoo-bin -u woow_paas_platform --stop-after-init`；
4. **post-verify**：重讀 DB 版號，**不等於 image 版號就 `exit 1`**，讓 pod 卡在 Init。

> gate 自己的註解把理由寫得很清楚：**對「側效果本身」取證，而不是對「我跑過那個指令」取證**——`odoo-bin` 回 0 只證明「`-u` 沒失敗」，不證明「DB 版號真的推進」。這個心智模型值得往外套用。

> **🔴 gate 唯一驗不到的東西，就是「gate 自己是不是對的那一顆」。**
>
> 上面每一步的比對基準都是 **gate 自己 image 的版號**。所以 gate 真正保證的不變式是：
>
> ```
> pod 起得來  ⟹  DB latest_version == 【gate 那顆 image】的 manifest 版號
> ```
>
> 而發版要的是 `DB latest_version == 【主容器那顆 image】的 manifest 版號`。**兩者只有在 S1 陷阱 ③ 與 ② 指向同一顆 digest 時才等價** —— 而這件事**沒有任何 runtime 檢查**（gate 讀不到主容器的 image 字串，Fleet／Helm 也不比對這兩個值）。②③ 分岔時 gate 不會報錯，它會忠實地拿錯的基準做出「一致，`skip`」的正確推論。
>
> ⇒ **主守線在「改 values 檔的那一刻」**：三處同改（§5.1 步驟 6）＋ CD repo `docs/promote-acceptance-checklist.md` §1b 的 grep 檢查。
>
> **本節 ①②③ 三層驗收接不住它**——① 問的是「gate 有沒有跑」，而 gate 跑了、也做出了 `skip` 的決定；②③ 都建立在「`-u` 該跑」的前提上，前提被 `skip` 掉時它們是**空真**（沒有 `Updating modules` 可看、seed 值本來就沒被要求改）。
>
> **但發版後仍有一條補得回來的檢查**：`skip` 掉時，同一顆 pod 上 **`module-version-gate` 這個 initContainer** 與主容器的 imageID 會**分岔**，直接比對即可（②③ 同步時兩者必然相同）：
>
> ```bash
> # 同一顆 pod 上，gate 與主容器跑的是不是同一個 digest
> kubectl --context woow-k3s -n paas-system get pods -l app.kubernetes.io/name=odoo \
>   -o jsonpath='{range .items[*]}{.metadata.name}{"\n  gate: "}{.status.initContainerStatuses[?(@.name=="module-version-gate")].imageID}{"\n  main: "}{.status.containerStatuses[*].imageID}{"\n"}{end}'
> # 兩行 digest 必須逐字相同；不同 = 命中本節這個缺陷（§9 坑 #21）
> ```
>
> 🔴 **`[?(@.name=="module-version-gate")]` 這個篩選器是必要的，寫成 `initContainerStatuses[*]` 會做出一個永遠報紅的檢查。** 這顆 pod 有**兩個** initContainer —— `patch-odoo-conf`（`busybox:1.37`）與 `module-version-gate`。`[*]` 會把兩顆的 imageID 都印進同一行，於是那一行**永遠**含 busybox 的 digest，**與 `main:` 永遠不可能逐字相同**。
> 這不是「等分岔時才會誤判」：**pin 完全同步（＝應該全綠）的狀態下它就已經在報紅**，從一開始就沒有鑑別力。而一個永遠紅燈的檢查跑幾次就會被當噪音忽略，最後等同不存在 —— 對一條掛著「發版後唯一接得住此缺陷」名義的檢查來說，那是最壞的結局。
>
> **實測（2026-08-08，prod `paas-system`，pod `paas-odoo-744b4fc67-t4w8l`，三處 pin 同步狀態）**：
>
> ```
> paas-odoo-744b4fc67-t4w8l
>   gate: jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo@sha256:2a14f05086a2…405f
>   main: jcr-prod.woowtech.io/woow-paas-docker-local/paas-odoo@sha256:2a14f05086a2…405f
> ```
>
> **陽性對照（同次實跑）**：把篩選條件換成不存在的 `no-such-container`，輸出為 `[]`（空）。⇒ 篩選器確實在篩，上面 `gate:` 那行有值是**真的匹配到**，不是「反正兩邊都印出來」。
> （對照下，錯誤的 `[*]` 版本在同一顆 pod 上印出的 `init:` 行是 `busybox@sha256:9532d8c3… <paas-odoo digest>` 兩個值。）
>
> 這個比對是**相對**的（比兩者是否相等，不是比對某個絕對值），所以不受 §2 提到的「prod+stg 共 cluster、containerd digest dedup 報 stg repo 別名」干擾——真的是同一顆時，兩邊會報同一個別名。
>
> ⚠ **判讀前先確認兩行都有值。** 兩行**皆空**不是「相等 ⇒ 通過」，是探針壞了（label selector 沒選到 pod、jsonpath 打錯、ns 錯）—— 空字串等於空字串會給出一個假綠，而這正是本節在講的那類失效換一層再犯一次。上面的陽性對照就是為了把「有值」與「篩對了」分開證明。

##### ③ 🔴 post-verify 不涵蓋 seed 值——這兩件事會分離

**post-verify 只保證 `latest_version` 推進，不保證任何一筆 `noupdate=0` 的 seed 值被寫入。** 兩者可以一個成立、另一個不成立：

- 若 `ir_model_data.noupdate` 對某批 seed 仍是 `true`（歷史 DB 的 sticky flag，見 [`seed-noupdate-sticky-template-links.md`](../../tracking/pitfalls/seed-noupdate-sticky-template-links.md)），ORM 會**靜默 skip** 掉那些 record 的更新；
- 而模組版號**照樣推進** ⇒ post-verify 通過 ⇒ pod 正常起來。

**實測坐實（PR#561，2026-08-03）**：在還原成 sticky 狀態的 DB 上跑第一次 `-u`——**`exit 0`、log 中 `ERROR`/`CRITICAL` 各 0 筆、四個目標欄位仍是空字串**；跑第二次才寫進去。也就是說，**這個失效沒有任何紅燈**。

⇒ **所以本節要求第三層驗收：發版後人工 psql 讀回本次變更的關鍵 seed 欄位，確認真的是新值。**

> ⚠ **查詢寫進 `.sql` 檔再 `-f`，不要用 `psql -c "…"` 內嵌。** SQL 裡的單引號（`''`、`'<<EMPTY>>'`）
> 要穿過 `kubectl exec -- bash -c '…'` 的外層單引號，逐層轉義極易出錯，而**錯法的症狀是
> `ERROR: syntax error` 或欄位被當識別字**，看起來像 DB 有問題。下面這個形態是實跑驗證過的：

```bash
CTX=woow-k3s
POD=paas-odoo-postgresql-0

# 1) 本地寫查詢（換成本次實際變更的 model / 欄位）
cat > /tmp/seed-verify.sql <<'SQL'
SELECT slug, COALESCE(NULLIF(helm_chart_version,''),'<<EMPTY>>') AS ver, is_active
  FROM woow_paas_platform_cloud_app_template ORDER BY slug;
SQL

# 2) 送進 pod 再用 -f 執行（避開多層引號轉義）
kubectl --context $CTX cp /tmp/seed-verify.sql paas-system/$POD:/tmp/seed-verify.sql
kubectl --context $CTX exec -n paas-system $POD -- bash -c \
  'PGPASSWORD=$(cat $POSTGRES_PASSWORD_FILE) psql -U $POSTGRES_USER -d $POSTGRES_DATABASE -f /tmp/seed-verify.sql'
```

若讀回仍是舊值，**先查 sticky flag 再決定怎麼做**（不要直接重跑一次了事——要知道是不是命中本節這個形態）：

```sql
SELECT noupdate, count(*) FROM ir_model_data
 WHERE module='woow_paas_platform' AND model='<本次變更的 model>' GROUP BY noupdate;
-- 全部 f = 沒有 sticky flag，值沒進去的原因在別處，往 -u 的 log 查
-- 出現 t   = 命中本節描述的形態，需要第二次 -u（檔尾 healer 會在第一次 -u 清掉 flag）
```

> **「pod 起來了」與「gate log 說 module update 完成」都不能取代這一層。** 三個信號證明三件不同的事，第 ③ 層沒有任何自動化涵蓋。

##### ② `-u` 的過程證據：bitnami 的輸出拿不到，改用替代證據鏈

**⚠ Bitnami odoo 的 `Updating modules` 步驟輸出不進 stdout，pod 內也沒有 log 檔**（`.80`／`.88` 兩次發版各撞一次）。所以**「在 boot log 裡找 `Running migration [18.0.1.0.XX>]` 字樣」這種驗收要求根本取不到**，不要再花時間找，也不要因為找不到就判定 migration 沒跑。

可用的替代證據鏈（四條合起來的證據力等價）：

| 證據 | 讀什麼 |
|---|---|
| `-u` 起訖 | boot log 的 `Updating modules` → `** Odoo setup finished! **`（實測量級：兩版 88 秒、八版 90 秒——**耗時與版數不成正比**，主要是模組載入的固定成本，別拿耗時推斷「有沒有真的升」） |
| 無殘留 migration | registry 行的 **`N modules loaded in X s, 0 queries`**——`0 queries` 代表這一輪只 load registry、沒有再寫 schema |
| 版號推進 | DB `ir_module_module.latest_version` == 本次版號且 `state='installed'` |
| 收官輪對照 | 第二輪（gate `skip` 或 `SKIP=yes`）的 boot log **不應再出現 `Updating modules`**，且 `0 queries` 仍成立 |

> **`0 queries` 是「這一輪沒再跑 migration」的硬證據**，配合 digest 不變一起看。收官後的第二輪 pod 若仍出現 `Updating modules`，代表宣告態沒收回去（走 §5.1.3 三重確認排查）。

> ⚠ **boot log 的 Traceback 不等於發版失敗，但要看時間戳。** 已知的固定噪音：`ir_cron` 的 `Base: Auto-vacuum internal data` 跑 `_file_gc` 會噴 `FileNotFoundError`／`PermissionError`（追蹤 issue #503，根因是 filestore root-owned）。**判定手法＝看 Traceback 的時間戳是否落在 `Registry loaded` 之後**——之後的屬於 cron 噪音，之前的才和 `-u` 有關。這是每天固定出現的噪音，所以「boot log 0 筆 Traceback」這條驗收**每次都要人工重判**，不能寫成機械斷言。

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

#### 5.2.1 Worked example：operator #207/#208 promote 到 prod（stg 已驗證，可機器執行）

> ⚠️ **本例版本已成歷史（2026-06-25）**：此 promote 已完成，prod operator 現為 `0.1.16` / `20260624.1022`（見 §2）。下列 `NEW_TAG=20260620.1406` / `0.1.15` 等是**當時**的值，**保留作可機器執行的 recipe 範本**——下次 promote 時把 `NEW_TAG`/`NEW_CHART`/`NEW_DIGEST` 換成新的 stg 驗過值即可，步驟與 gate 不變。
>
> 情境：stg 已跑 chart `0.1.15` + image `20260620.1406`（#207 mcp route 命名 + #208 PVC `orphaned-at` epoch），端到端驗證通過；要把同一組 pin 搬到 prod（prod 當時為 `0.1.8` / `20260525.0054`）。下列為**可貼上執行**的片段，每步含 fail-closed 退出；實際 promote 仍須過審核（§6 + §11）。

```bash
set -euo pipefail
CTX=woow-k3s
WPP=$(git rev-parse --show-toplevel)                       # 本 repo（woow_paas_platform）；或填絕對路徑
CD=/tmp/paas-app-bundle-cd
NEW_TAG=20260620.1406; NEW_CHART=0.1.15; NEW_DIGEST=sha256:1b322c8a   # 要 promote 的、stg 驗過的值

# ── 0a. 前置 gate：VPN + kubectl ──
tailscale status >/dev/null 2>&1 || { echo "STOP: Tailscale VPN 未連"; exit 1; }
kubectl --context $CTX get ns paas-system >/dev/null || { echo "STOP: 連不到 woow-k3s"; exit 1; }

# ── 0b. 🔒 GATE 無回歸超集（§9 坑 #11）— 別把 prod 推回更舊的 operator ──
git -C "$CD" pull -q 2>/dev/null || git clone -q https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd.git "$CD"
git -C "$WPP" fetch --tags -q
PROD_TAG=$(grep -E '^[[:space:]]*tag:' "$CD/paas-operator/values-prod.yaml" | head -1 | tr -d ' "' | cut -d: -f2)
PROD_COMMIT=$(git -C "$WPP" rev-list -n1 "operator-v${PROD_TAG}" 2>/dev/null || true)   # operator-v{tag} 指向來源 commit
NEW_COMMIT=$(git -C "$WPP" rev-list -n1 "operator-v${NEW_TAG}")
[ -n "$PROD_COMMIT" ] && { git -C "$WPP" merge-base --is-ancestor "$PROD_COMMIT" "$NEW_COMMIT" \
  && echo "OK: 新 image ⊇ 舊（無回歸）" || { echo "STOP: 新版非舊版超集，會回歸"; exit 1; }; }
# chart 超集：確認舊版的 env wiring 沒被新版砍（例 skillRunner passthrough）
git -C /tmp/woow-paas-charts pull -q 2>/dev/null || git clone -q https://git-prod.woowtech.io/woow-paas/woow-paas-charts.git /tmp/woow-paas-charts
grep -q AGENT_RUNTIME_SKILL_RUNNER_ENABLED /tmp/woow-paas-charts/charts/paas-operator/templates/deployment.yaml \
  || { echo "STOP: chart $NEW_CHART 缺既有 env wiring（疑似回歸）"; exit 1; }

# ── 1. lock-step：chart 0.1.15 必須「先」在 registry 且 Fleet 解得到 ──
#    為何 chart-first 安全：0.1.15 cron 才看得懂 epoch；舊 cron（0.1.14）把 epoch 當非數字字串 → skip 不誤刪
#    （向後相容、僅 parsing 變更非 API 契約）。鐵證用「stg 已在跑 0.1.15」= Fleet 已成功解析（免 registry 認證）：
kubectl --context $CTX -n fleet-local get bundle paas-app-bundle-paas-operator-stg \
  -o jsonpath='{.status.summary.ready}/{.status.summary.desiredReady}' | grep -q '^1/1$' \
  || { echo "STOP: stg operator bundle 非 1/1，chart $NEW_CHART 未確認可解析"; exit 1; }
#    （泛用情境若 chart 尚未在任何環境：認證查 helm registry，需 PACKAGE_TOKEN：
#     curl -fsS -H "Authorization: token $PACKAGE_TOKEN" https://git-prod.woowtech.io/api/v1/packages/woow-paas/helm/paas-operator/$NEW_CHART ）

# ── 2. 確認要 promote 的就是 stg 驗過那顆 digest（認 digest 不認 tag；先等 rollout 穩定再讀）──
kubectl --context $CTX -n paas-stg-system rollout status deploy/paas-operator-stg --timeout=60s
kubectl --context $CTX -n paas-stg-system get pod -l app.kubernetes.io/name=paas-operator \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' | grep -q "$NEW_DIGEST" \
  || { echo "STOP: stg pod digest 不含 $NEW_DIGEST"; exit 1; }

# ── 3. 改 prod pin（只動 paas-operator/，絕不碰 paas-operator-stg/）──
cd "$CD" && git switch -c "fix/operator-prod-bump-${NEW_CHART//./_}"
sed -i.bak -E "s/^([[:space:]]*version: )\"0\.1\.8\"/\\1\"${NEW_CHART}\"/" paas-operator/fleet.yaml
sed -i.bak -E "s/(tag: )\"20260525\.0054\"/\\1\"${NEW_TAG}\"/" paas-operator/values-prod.yaml
rm -f paas-operator/*.bak
# 保留 prod 專屬欄位（§6）：以下必須仍是 prod 值，且不可誤帶 stg-only 的 agentRuntime 區塊
grep -q 'namespacePrefix: "paas-ws-"' paas-operator/values-prod.yaml \
 && grep -q 'pvcGracePeriodDays: 90'  paas-operator/values-prod.yaml \
 && grep -q 'replicaCount: 2'         paas-operator/values-prod.yaml \
 && ! grep -q 'agentRuntime:'         paas-operator/values-prod.yaml \
 || { echo "STOP: prod 專屬欄位被污染或誤帶 agentRuntime"; exit 1; }
# config.paasEnv（≥ chart 0.1.16）：若 prod values 已含 paasEnv，務必是 "prod"（誤帶 "stg" → prod ns 被標 env=stg）。
# 舊 prod（< 0.1.16，無 paasEnv 鍵）此檢查略過——grep 命中 0 行即視為「尚無此鍵」，不擋。
if grep -qE '^\s*paasEnv:' paas-operator/values-prod.yaml; then
  grep -qE '^\s*paasEnv: *"?prod"?\s*$' paas-operator/values-prod.yaml \
    || { echo "STOP: prod config.paasEnv 非 'prod'（split-env 風險，見 §6.4 兩端一致）"; exit 1; }
fi
# 🔒 兩端一致（§6.4 / F-ENV-1b）：promote operator 後另須確認 prod Odoo 的 deploy_env 也是 prod（或空）。
# operator: kubectl --context $CTX -n paas-system exec deploy/paas-operator -- printenv PAAS_ENV  # 期望 prod
[ -z "$(git diff --name-only -- paas-operator-stg/)" ] || { echo "STOP: 誤動了 stg 目錄"; exit 1; }

# ── 4. PR → merge main → 等 Fleet reconcile → 閉環驗證 ──
git add paas-operator/fleet.yaml paas-operator/values-prod.yaml
git commit -m "fix: operator prod bump ${NEW_TAG} + chart ${NEW_CHART}（#207/#208）"
git push -u origin "fix/operator-prod-bump-${NEW_CHART//./_}"; tea pr create   # 經審核 merge main
HEAD_SHA=$(git -C "$CD" rev-parse HEAD)                      # 等 Fleet 抓到本 commit（≤5 分）
timeout 300 bash -c "until kubectl --context $CTX -n fleet-local get gitrepo paas-app-bundle -o jsonpath='{.status.commit}' | grep -q '$HEAD_SHA'; do sleep 10; done" \
  || echo "WARN: 5 分內未見 Fleet 同步到本 commit，手查 gitrepo/bundle"
kubectl --context $CTX -n paas-system rollout status deploy/paas-operator --timeout=180s
kubectl --context $CTX -n paas-system get pod -l app.kubernetes.io/name=paas-operator \
  -o jsonpath='{range .items[*]}{.status.containerStatuses[0].imageID}{"\n"}{end}' | grep -q "$NEW_DIGEST" \
  || { echo "FAIL: prod pod digest 未更新到 $NEW_DIGEST → 走 §8 rollback"; exit 1; }
# bundle 應為 ready:1（§10.1 issue #18 的既有 registry-server/username drift 已於 2026-06-25 清除）——
#   summary=$(kubectl --context $CTX -n fleet-local get bundle paas-app-bundle-paas-operator -o jsonpath='{.status.summary}')
#   現在任何 modified 都視為「本次造成」→ 排查/rollback。若再見 paas-operator-registry 的 registry-server/username=null
#   復現，代表那兩個殘留 key 被重新手動 apply 回去（非 chart 產出），照 §10.1 修法再移除即可。

# ── 5. #208 端到端鐵證（prod 有真租戶 → 只用隔離 fixture，絕不碰 paas-ws-<真實租戶>）──
#   test ns 用 prod 前綴 paas-ws-208proof（非 paas-ws-stg-），裝一個含 helm.sh/resource-policy:keep standalone PVC 的真 helm release，
#   kubectl exec <operator pod> -- sh -c 'curl -s -X DELETE -H "X-API-Key:$API_KEY" http://localhost:8000/api/releases/paas-ws-208proof/<release>'，
#   驗 `kubectl --context $CTX get pvc -A -l paas.woowtech.io/orphaned-at` 命中且值純數字 epoch，最後 `kubectl delete ns paas-ws-208proof` 收尾。
#   完整 recipe：memory reference_operator_pvc_orphan_label_e2e_recipe。
```

### 5.3 agent-runtime（prod 已啟用 + LLM 端到端驗證）

- **現況（2026-06-27）**：prod **已啟用 agent-runtime 並端到端驗證回真 LLM**。operator `values-prod.yaml` 已有 `config.agentRuntime` 區塊（`paas-app-bundle-cd#33`：imageTag `1.4.2` + chartVersion `0.4.3` + probeStyle httpGet + externalEgress + skillRunner）；odoo 5 `AGENT_RUNTIME_*` env（`#34`，secretKeyRef→`paas-odoo-agent-runtime`）；`default_enable_agent_runtime=True`（新 project 自動開）。**LLM**：`ai.config` WoowBot Config（id=3）設 `api_key` + `type=openai_compatible` + `model=MiniMax-M2.7` + `api_base_url`，`default_ai_assistant_id=2`/`default_ai_config_id=3`；一把 key 餵兩路——legacy /woow chat（`ai_assistant_woowbot`→config 3）與 agent pod（`agent_template_default` id=2→`model_config_id=3`）。**live E2E（2026-06-27）**：cold-start spawn 9.5s → pod `agent-runtime:1.4.2` 2/2 Running → WS 往返回 `"Paris is the capital of France."`（`REAL_LLM` 非 echo）→ operator delete + Odoo unlink + 刪 ns 清理回 0 tenant ns。prod 實渲染 agent NP egress `0.0.0.0/0:[443,80]` except 私網段，MiniMax public CF IP 放行。詳見 plan `docs/plans/2026-Q2/2026-06-26-prod-agent-runtime-enablement.md` 與 memory `prod-odoo-paas-not-installed`。**未做 follow-ups**（F-LLM-1/2/3：endpoint 命名 hygiene + key 輪替 + 綁無效 template 的 echo 暗路 fail-loud guard）見 `docs/tracking/followups/follow-ups.md`。
- **升版（既存 workspace 不自動換 image，須手動 reap）**：完整程序見 **[agent-runtime image 升版 SOP](agent-runtime-image-upgrade-sop.md)**（operator/platform 視角）與 paas-agent-runtime repo `docs/runbooks/image-upgrade-sop.md`（runtime 視角）。**勿在本文件重抄**。
- 關鍵差異：agent-runtime chart **不由 Fleet 直裝**，而是 operator runtime 在 spawn 時動態 `helm install`；Fleet 只透過 operator 的 env（`AGENT_RUNTIME_IMAGE_TAG` / `AGENT_RUNTIME_CHART_VERSION`）把版本傳進去。

### 5.4 Cloudflare Tunnel

- chart `cloudflare-tunnel-remote`（`0.1.2`）與 image `cloudflared`（`2026.3.0`）**現皆已 pin**（issue #19 於 commit `a76517e` 修復；取代先前浮動 `latest`）。平常不需動；要升版改 `cloudflare/fleet.yaml` `version` + `cloudflare/values-prod.yaml:8` `image.tag`，先 stg 觀察。
- tunnel token 由 secret 注入（prod `paas-cloudflare-fleet-secrets`、stg `paas-stg-cloudflare-fleet-secrets`，皆在 `fleet-local`）。**改 secret 後 Fleet 不會自動同步，須 force sync**（§7）。
- **prod / stg tunnel 不可混用**：prod tunnelId `328c3a28-…`、stg `55953bfa-…`。

### 5.5 vibe-kanban

- chart 存在於 woow-paas-charts（`0.2.0`），但 **prod / stg Fleet 皆無對應 bundle = 目前未透過此 CD 部署**。要上 prod 須在 paas-app-bundle-cd 新增 `vibe-kanban/{fleet.yaml,values-prod.yaml}` + `fleet-local` secret（流程見 CD repo README「新增 App」）。

---

## 6. stg → prod promote 通則

prod **無自動 promote**，是手動把 stg 驗證過的值搬到 prod：

1. **先在 stg 完整驗證**（`paas-operator-stg/` 等），確認 pod 健康、端到端功能正常。
2. 把 stg 已驗證的 **image.tag / chart version** 搬到對應 prod 檔（`paas-operator/values-prod.yaml` + `fleet.yaml`）。
3. **保留 prod 專屬欄位、不可被 stg 值覆蓋**：
   - `namespacePrefix`：prod `paas-ws-` / stg `paas-ws-stg-`
   - `config.paasEnv`（→ operator env `PAAS_ENV`；L2 ns env label `paas.woowtech.io/env`）：prod `prod` / stg `stg`。搬版務必確認 prod 值為 `prod`，誤帶 stg 的 `stg` → prod operator 把租戶 ns 標成 `env=stg`、誤導孤兒清理判 env。
   - `replicaCount`：prod 2
   - `pvcGracePeriodDays`：prod **90**（dev/stg 為 7）
   - prod `tunnelId` / `zoneId` / 各 prod secret
   - prod operator 啟用 agent-runtime 時的 webhook / auth secret（見 **paas-app-bundle-cd repo 的 `CLAUDE.md`「Agent-runtime 共用 secret」節**：secret `paas-operator-agent-runtime-auth`，含 current / previous / webhook-current 三 key —— **注意此 anchor 在 CD repo 而非本 repo 的 CLAUDE.md**）
4. **🔒 兩端 env 一致檢查（F-ENV-1b，promote operator 或 addon 到 prod 必檢）**：env token 由**兩個各自獨立設定的端**決定，兩端必須**同為 `prod`**，否則 split-env：
   - **operator 端** `PAAS_ENV`（來源 Fleet `paas-operator/values-prod.yaml` 的 `config.paasEnv`）→ 決定 ns label `paas.woowtech.io/env` 的值。
   - **Odoo addon 端** `woow_paas_platform.deploy_env`（config_parameter）→ 決定 SecurityAccess `canonical_name`（prod=空 token `paas-sa-` / stg=`paas-stg-sa-`，連帶 CF tunnel name / DNS comment / Access policy name）。
   - 不一致的後果：operator 把 ns 標 `env=prod`、但 Odoo 把 CF 命名算成 `paas-stg-sa-`（或反之）→ ns label 與 CF 命名對 env 各說各話、辨識性破功、孤兒清理誤判。
   - 檢查（promote 後驗）：
     ```bash
     kubectl --context woow-k3s -n paas-system exec deploy/paas-operator -- printenv PAAS_ENV   # 期望：prod
     # Odoo 端：Settings → Woow PaaS → Deploy Environment = Production（或查 ir.config_parameter
     #   woow_paas_platform.deploy_env = prod 或空——prod 預設空 token 等同 prod，但顯式 prod 較不易誤判）
     ```
   - 註：prod deploy_env 顯式設 `prod`（非留空）較不易在日後誤判；stg 已由 `.sandbox.yml` `config_params_stg` 自動設（F-ENV-1）。
5. tea PR（zh-tw）→ merge `main` → Fleet 自動 `helm upgrade`。

---

## 7. 驗證（verify）recipes

> **🔴 驗收通則：某個信號「沒有對象可驗」時，要如實回報「沒有可抽驗的實例」——不是打勾，也不是略過。**
>
> 實例：某輪發版的驗收項「既有 cloud service 未受損」，當時 prod 的 cloud service 是 **0 筆** ⇒ 這一項**無對象可驗**。
>
> 為什麼要特別寫成規則：**「沒有東西可驗」與「驗過了、沒問題」在勾選表上長得一模一樣**——都是一個打勾。
> 差別只存在於執行者當下的腦子裡，一旦回報出去就永久消失，而下一個人會把那個勾讀成「這條有被證明過」。
>
> ⇒ 三選一，**不得只寫「✅」**：
> - `✅ 已驗（N 個實例，逐一列出）`
> - `⊘ 無對象可驗（母體為 0，說明為什麼是 0）` ← 本節要求的形態
> - `⚠ 未驗（說明原因與風險）`
>
> 這條**不限於發版**：任何「對既有資料無副作用」「不影響現有租戶」類的驗收，母體都可能是空的。
> 母體為 0 時該信號的**鑑別力也是 0**——它沒有排除任何東西，紀錄要如實反映這件事。
> （同源紀律：陰性結論要配陽性對照；集合比較在空集合上恆真。）

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

# (6) 站點 smoke test（pass=HTTP 2xx/3xx；fail=4xx/5xx/000 → 重試 ≤3 次仍 fail 則立即走 §8 rollback）
for path in /woow /odoo /my; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://paas.woowtech.io${path}" || echo 000)
  case "$code" in 2??|3??) echo "OK   $path → $code";; *) echo "FAIL $path → $code";; esac
done
```

---

## 8. Rollback 通則

Fleet GitOps 的 rollback = **把 pin 值改回上一個已知良好版本**：

1. **查出前一版 pin**（values 檔的 git 歷史就是版本台帳）：
   ```bash
   CD=/tmp/paas-app-bundle-cd; COMPONENT=paas-odoo    # 或 paas-operator / cloudflare
   git -C $CD log -p -2 -- $COMPONENT/values-prod.yaml | grep -E '^[+-] *(tag|digest|image):'
   # 或讀 values 檔內保留的「取代 …（sha256:…）」註解，取上一版 tag + digest
   ```
   > 🔴 **`image:` 這個 alternation 是必要的，不是保險**：gate initContainer 的 pin（S1 陷阱 ③）在檔裡的形態是 `    image: <registry>/<repo>@sha256:…`，**前綴是 `image:` 而不是 `digest:`**。舊版本 grep 只寫 `(tag|digest)`，**對 gate 那一行完全零命中** —— 而「grep 沒印出來」在事故當下會被讀成「沒有這個 pin 要退」。（實測 `git log -p -2` 於 CD `9ee287e`：舊 pattern 只印 4 行 `tag:`/`digest:`；加 `image:` 後多印出 gate 的兩行。）
   > 副作用是 `postgresql.image` 區塊若同期被動過也會被印出來 —— 看縮排與上下文區分即可，**寧可多印也不要漏印**。
2. 在 paas-app-bundle-cd 把 `image.tag`（和/或 `fleet.yaml` chart version）改回前一版（**用前一版的 digest 反查確認 tag** 以防 mutable tag 漂移）。
   > 🔴 **`paas-odoo` 的 image pin 有三處（§5.1 S1 陷阱），rollback 必須 ①`image.tag` ②`image.digest` ③`module-version-gate` 的 `image` <ins>三處同退</ins>。**
   >
   > - **只退 ①**（漏 ②③）= **rollback 根本沒發生**（digest 優先於 tag），但 PR merged / Fleet 綠 / bundle ready 會讓它看起來成功。
   > - **退了 ①②、漏 ③** = **rollback 只退了一半，而且是最危險的那一半**：主容器回到舊 code，gate 卻還是新版那顆 —— gate 讀自己（新）image 的版號，與 promote 時**已經被推進**的 DB 版號**相等** ⇒ 判定「版本一致」而 `skip`。於是 **舊 code 跑在新 schema／新 seed 上**，而 pod 起得來、站點 200、三方 digest 閉環照樣全過。這是 promote 方向那個缺陷的**鏡像**，同樣沒有紅燈。
   >
   > 事故當下最不需要的就是這種假訊號 —— 而它比 promote 時更毒：rollback 是「已經出事了才做的動作」，此時再吃一次靜默失效，等於在錯誤的假設上繼續排查。
   >
   > ⚠ **三處都退對之後會發生什麼**（先知道，才不會把它誤判成 rollback 失敗）：gate 這時讀到的是**舊**版號、DB 是**新**版號，兩者不等 ⇒ **gate 會觸發一次 `-u`**（用舊 image 的 addon 檔案跑），rollback 這一輪因此也會有 migration 等級的停機，不是純換 image。
   > 依 Odoo `load_module_graph` 的語意，`-u` 跑完會把 `latest_version` 寫成**該次載入的 manifest 版號**（即舊版號）⇒ gate 的 post-verify 應會通過、pod 起得來；而**降版方向通常沒有 migration script，schema 不會跟著回退**（與下方「image 退版 ≠ DB 退版」一致）。**此段為機制推斷、未在 prod 或沙盒實測** —— 真的要靠它之前先在沙盒跑一次降版驗證，別在事故現場第一次驗。
3. merge `main` → Fleet 自動 `helm upgrade` 回退。
4. 緊急時（等不及 PR 流程）可 `git revert` 觸發 commit 後直接 merge。
5. 驗證 pod digest 已回到舊版（§7）。

> **🔴 image 退版 ≠ DB 退版。** 上述步驟只把 image pin 改回舊版，**不會回退已經跑完的 `-u` migration**（schema 與 `noupdate=0` seed data 已經改掉了）。**DB / schema 層的還原走 §5.1.1 的發版前 snapshot** —— 那是 prod 唯一的還原錨點（prod DB 無離線備份）。若該次發版**沒有** `readyToUse=true` 的 snapshot，就**沒有 DB 退路**，只能往前修；這正是 §5.1.1 把 snapshot 列為 promote 前必要步驟的原因。

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
| 9 | tag prefix 寫錯（paas-odoo-ci 僅以 `startsWith('stg-')` 判環境） | prod image 被污染 | 發版用 skill / `/woow:deploy`，勿手打 tag。（prod build 另有人工 approval gate 擋在 build/push 之前，見 §10 第 3 點——但 gate 擋的是「這次要不要 build」，擋不掉「prefix 把環境判錯」） |
| 10 | 手動 `helm uninstall` agent-runtime release | 繞過 operator owner → state 不一致 | 走 operator DELETE 端點（§5.3 連結） |
| 11 | 把比 prod 現役**更舊或非超集**的 image/chart 推上 prod | 靜默回歸：prod 失去現役版本已有的修復（如 operator D1-D6 生命週期 teardown、Access 契約） | promote 前過超集 gate：`git merge-base --is-ancestor <現役來源 commit> <新來源 commit>`；manifest/版號單調不退；chart 用 `git log` 確認模板 wiring（如 `AGENT_RUNTIME_SKILL_RUNNER_ENABLED` env passthrough）沒被砍（見 §5.2.1 step 0） |
| 12 | 人工 `workflow_dispatch` 重跑 `build-image.yml` 忘填 `addon_ref` | image 從**移動中的 `develop` HEAD** 烤出來——不可重現、含未發版 commit、發版紀錄與 image 內容脫鉤；「預設分支是誰」不在版控裡（Gitea repo 設定、CI 零校驗），被改成 `main` 就直接變版號倒退。**CI 全綠、`image_tag` 與 `digest` 都是新的 → 坑 #4 的 digest 驗法對此完全無效**，要到 pod 跑 `-u` 才現形 | 重跑必填 `addon_ref=v{版號}`；驗 build log **三行錨點**（`at ref:`／`Cloned addon version:`／`Image addon version:`，§5.1.4——前兩行同源屬 source 端，第三行才是 image 端）。**三行都要跟本次預期版號比，不是比「三行互不互相一致」**——填錯 ref 時三行會一致地顯示錯的版號。paas-odoo-ci 有兩道 fail-closed：`addon_ref` 留空且非 `stg-*` → `exit 1`（#46，擋本列這個缺陷）；image 版號與 clone 不符 → `exit 1` 且擋在 push 之前（#50，擋的是另一類）。`stg-*` 的 `develop` fallback 刻意保留，`deploy-stg` 依賴它 |
| 13 | 用 `tea actions runs list` 判斷 CI 有沒有被觸發 | 它**只列出部分 completed run**，會漏掉實際已成功的那一次 → 誤判「CI 沒觸發」→ 刪 tag 重推 ＋ 手動 dispatch ＝ **同一版被 build 三次**（實例：多出兩個 build，其中一個卡在 approval 要另外 `/reject` 清掉） | 判「有沒有觸發」不要用 `runs list`。改用 Gitea API 查全狀態，或直接看 approval issue／registry 有沒有出現預期 tag。**任何「查不到＝不存在」的結論都要換一種信號複驗**（同族：`tea pulls list` 也會回空表假陰性） |
| 14 | 把發版當下的短暫 502 或「pod 三分鐘才 ready」當成異常 | 誤判成回歸、觸發不必要的 rollback | `paas-odoo` 是**單副本 ＋ `updateStrategy: Recreate`** ⇒ **每一輪 CD merge 必有 3–10 分鐘停機**（含收官那輪），期間 502 是預期。另外 startupProbe `initialDelaySeconds=180` ⇒ **即使不跑 `-u` 也要 ~3 分鐘才 Ready**，那是 probe 延遲主導、不是 boot 慢。⇒ 發版挑離峰；收官 PR 也算一次停機，要一併算進窗口 |
| 15 | 請乾淨端（人類 `!`）跑 git 指令時沒帶 `git -C <repo>` | 對方的 cwd 不一定是你以為的那個 repo → **在別的 repo 誤 commit**（實例：本該進 CD repo 的 commit 落到 `woow_paas_platform` 的 develop 上，內容還是無關的設定檔） | **給別人執行的 git 指令一律帶 `git -C <絕對路徑>`，不依賴 cwd。** 誤 commit 的補救是 `git reset --soft HEAD~1`＋`git restore --staged`（本例零損失，但那是運氣） |
| 16 | 用裸 SQL 盤點 template 曝光數，發現與前台不符就以為回歸 | 誤判：DB 15 筆 vs 前台 12 張，實為 12 顯示 ＋ 1 hidden ＋ 2 `is_active=false` | **裸 SQL 不會排除 inactive，Odoo ORM `search` 會。** 盤點曝光數的 SQL 必須自己帶條件；⚠ 欄位名是 **`is_active` 不是 `active`**，且 `is_hidden` 對既有 row 可能是 **`NULL` 不是 `false`**（比較時用 falsy 語意，別寫 `= false`） |
| 17 | 把 Longhorn snapshot 當成「有備份」 | snapshot **存在同一顆 volume 內** ⇒ volume 毀就一起沒。它只救得了邏輯錯誤（誤刪、migration 寫壞），救不了儲存層故障 | snapshot 是**還原錨點**不是備份（§5.1.1）。另：volume 上有 `recurring-job-group/default: enabled` label **不代表機制存在**——曾實查 prod **0 個 recurring job 物件、0 個 backup、backup-target 未設定**，label 只是個標籤。**「label 存在 ≠ 機制存在」是最容易誤判成「有保護」的形態**（追蹤 issue #504） |
| 18 | 讓 agent 代按 prod build 的 approval gate | 繞過 human-in-the-loop。實例：agent 的輸入緩衝殘留「幫我留 `/approve`」並主動詢問要不要代按，**被系統的 NOT-USER-INPUT 標記證明無人類授權**才攔下。**反面先例（2026-08-16 發版 `v18.0.1.0.146`）**：agent 在 Eugene **即時明確授權**下於 `paas-odoo-ci#117` 代留 `/approve`，事後才發現本列明文禁止（**無授權例外**）。裁決：結果照單全收（image 版號驗證全過）、**禁令維持絕對、不加例外**——授權證明人類同意，閘要的是人類執行；未來一律 Eugene 本人留言。`paas-odoo-ci#117` 上有對應透明留言 | **prod approval gate 只能人類本人按**，agent 一律不得代 approve（即使 agent 手上有可用 token、即使 `ALLOW_SELF_APPROVE=true`、**即使人類當場即時明確授權**——見左欄 2026-08-16 反面先例與裁決）。留言內容 trim 後須**精確等於** `/approve`；**最保險是網頁點**。CLI 語法坑：`tea comment <repo> <idx>` **錯**（repo 字串會被當 index 去 ParseInt），正解 `tea comments --repo <owner/repo> <idx> "/approve"`（複數、`--repo` 走 flag）。⏱ gate **逾時 180 分鐘**後該 run 會以 failure 結束（曾實際發生，`/approve` 沒送出去）——恢復走 §5.1.4「CI 失敗要重跑時」的 `workflow_dispatch`（**必填 `addon_ref`**），不要用刪 tag 再重推的方式繞 |
| 19 | 相信另一條 lane（CD agent／teammate）回報的「已建 branch／已 push／已 merge」 | 實例：同一 session 兩次 fabrication（假 commit、假 branch＋假 diff，磁碟／local／remote 全查無）；另一次「已 merge PR」首報實為 open | **寫入類主張一律用自己的工具鏈獨立查證**：`git ls-remote`／`git worktree list`／`tea pr <n>`／`git show origin/main:<file>`。「寫入成功」的回報與「查證過的事實」是兩件事 |
| 20 | 以為 agent 能自動化整條發版鏈 | harness 的 auto-mode classifier 會擋掉其中幾步（實測：`longhorn-system` 的 `apply`＝建 snapshot、prod postgres 的 `psql exec`），流程會卡在中間 | 這些**由人類以 `!` 親跑**（也符合共用叢集 scope 鐵律：`longhorn-system` 不在本 repo 可操作的 namespace 內）。agent 可做的是**唯讀 cross-check**（`get snapshot` 讀 `readyToUse`）。⚠ 但 `kubectl exec deploy/paas-odoo -- odoo shell`（唯讀 search/read）是**通過**的 ⇒ DB 層的驗證多半可以自己來，不必每次都找人 |
| 21 | promote／rollback 改了 `image.tag`＋`image.digest`，**漏掉 `module-version-gate` initContainer 那個寫死的 `image`**（S1 陷阱 ③） | **與坑 #4 是不同的失效形態，#4 的 digest 驗法對它完全無效**：image 真的換了、pod imageID digest 真的是新的、三方閉環全過 —— 但 gate 比對的是**自己那顆舊 image** 的版號，與同樣是舊的 DB 版號相等 ⇒ 安靜 `skip` 掉該跑的 `-u`（主容器 `ODOO_SKIP_MODULES_UPDATE=yes` 也不補跑）⇒ **新 code 跑在舊 schema／舊 seed 上**，要到功能驗收才現形。rollback 方向為鏡像：**舊 code 跑在新 schema 上** | 改 values 時三處同改／同退（§5.1 步驟 6、§8 步驟 2）；grep 要含 `image:`（gate 那行的前綴不是 `digest:`）；CD repo `docs/promote-acceptance-checklist.md` §1b 有對應檢查。**叢集沒有任何 runtime 檢查涵蓋這一項**（gate 讀不到主容器的 image 字串、Fleet／Helm 也不比對這兩個值），主守線在改檔那一刻。發版後唯一接得住它的是**同一顆 pod 上 gate initContainer 與主容器的 imageID 比對**（§5.1.5 ①，2026-08-08 prod 實測；jsonpath 必須用 `[?(@.name=="module-version-gate")]` 篩選，寫 `[*]` 會因另一個 busybox init 容器而永遠報紅） |

---

## 10. Known issues / 本次稽核發現（2026-06-14）

> 以下為整理 SOP 時發現、**非緊急**（prod 服務正常運行中）但建議追蹤 / 發 issue 的項目。
>
> **✅ 全數收斂（2026-06-25 複驗）**：本節 6 條稽核發現均已 resolved / clarified —— #18 Fleet drift（殘留 secret key 移除）、#19 cloudflare pin、paas-odoo-ci#1 prod approval gate、#20 CD README、#26 charts README、#5 認知 drift 澄清。各條 inline 標記修復出處與複驗結果。

1. ~~**Fleet bundle `paas-app-bundle-paas-operator` 卡在 `Modified`**~~ **✅ 已解決（2026-06-25）**
   - 現象（歷史）：live `paas-system/paas-operator-registry` secret 有 `registry-server` / `registry-username` 兩個 key，但 Fleet desired state 缺這兩個 key → Fleet 永久報 drift（`modified:1` / `ready:0`，patch `{"data":{"registry-server":null,"registry-username":null}}`）。
   - **根因（經 paas-charts 複核 + live 證據更正）**：chart `secret-registry.yaml` **自 0.1.6 起就是單 key 模型**（只渲染 `registry-password`），`registry.server` / `registry.username` 一直走 **plaintext deployment env**（live `REGISTRY_SERVER=jcr-prod.woowtech.io` / `REGISTRY_USERNAME=woow-prod` 皆為 env value，非 secretKeyRef）。那兩個 secret key **從來不是這顆 chart 產生的**，是 pre-0.1.6 / chart 外（手動 `kubectl apply`，secret 上有 `last-applied-configuration` annotation）殘留，且**零 consumer**（全叢集只有 operator deploy 引用此 secret 的 `registry-password`）。
   - **修復**：`kubectl -n paas-system patch secret paas-operator-registry --type=json -p='[{"op":"remove","path":"/data/registry-server"},{"op":"remove","path":"/data/registry-username"}]'`（移除殘留 key，收斂到 Fleet desired）→ bundle `ready:1`、GitRepo `5/5`、operator pod 0 restart、`REGISTRY_SERVER/USERNAME` plaintext env 不受影響。
   - 教訓：判 Fleet drift 方向看 `modifiedStatus.patch`（live→desired）；`null` = desired 要刪該 key。本案 desired（chart）才是對的、live 多了殘留 key，故修法是讓 live 收斂到 desired，**非**改 CD repo。詳見 pitfall `fleet-split-registry-values-merge-drift.md`（已更正誤診）。
   - 追蹤 issue：[paas-app-bundle-cd#18](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/18)（可關閉）

2. ~~**Cloudflare chart / image 未 pin（浮動 `latest`）**~~ **已解決（2026-06-21 複驗）**：prod + stg cloudflare chart 已 pin `0.1.2`、image 已 pin `cloudflared:2026.3.0`（commit `a76517e`，live pod 實跑 `2026.3.0`）。issue [paas-app-bundle-cd#19](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/19) 可關閉。

3. ~~**paas-odoo-ci 無 prod approval gate**…~~ **✅ 已解決（2026-06-25 複驗）**：`paas-odoo-ci/.gitea/workflows/build-image.yml`（branch `feat/prod-build-approval-gate` + `fix/codeowners-and-self-approve-toggle` merged）已加 prod **人工核可 gate**——build job 內「Wait for prod approval」step（非 stg 路徑才跑、**位在 build/push 之前**），由 `PROD_APPROVERS` 白名單成員留言 `/approve` 才放行。`scripts/wait-for-approval.sh` 全程 fail-closed（`set -euo pipefail`、`APPROVERS` 空→exit 1、BUILD_TAG 字元集擋 issue-body 注入、condition 只用 `github.*` context 防 shell injection bypass、`ALLOW_SELF_APPROVE` 非法值 fail-safe 回嚴格）。實際配置 `PROD_APPROVERS=eugene` / `ALLOW_SELF_APPROVE=true`（單人維運自核）。追蹤 issue：[paas-odoo-ci#1](https://git-prod.woowtech.io/woow-paas/paas-odoo-ci/issues/1)（已關閉）

4. ~~**文件 drift（小）**：`paas-app-bundle-cd/README.md` 只描述 `paas-odoo` 且寫 `values.yaml`…~~ **✅ 已解決（2026-06-25）**：paas-cd 已重寫 README（branch `docs/readme-multi-component-20` merged），涵蓋 5 component 結構表（paas-operator / paas-operator-stg / paas-odoo / cloudflare / cloudflare-stg + 各自 ns/env）、`values-<env>.yaml` 命名規則、版號 pin 規則（chart→`fleet.yaml#helm.version`、image→`values-<env>.yaml#image.tag`）、bundle 實名前綴 `paas-app-bundle`（無 `-cd`）caveat，逐項對齊實際 fleet.yaml 驗證。殘留一處 cloudflare「尚未 pin」過時註記（#19 已 merge、實際已 pin chart `0.1.2` + image `2026.3.0`）經 PR [paas-app-bundle-cd#28](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/pulls/28) 修正。追蹤 issue：[paas-app-bundle-cd#20](https://git-prod.woowtech.io/woow-paas/paas-app-bundle-cd/issues/20)（已關閉）

5. **認知 drift（已澄清，記錄供後人）**：曾有「odoo CD 完全手動、改 `resources/private/paas-odoo.md` + Rancher UI 點 upgrade」的說法 —— 經查 Fleet GitRepo / bundle / CD repo README，**odoo 確實由 Fleet GitOps 管理**（bundle `paas-app-bundle-paas-odoo`），該手動說法為**過時錯誤**。

6. ~~**charts repo README drift**：`woow-paas-charts/README.md` 宣稱 paas-operator CD 走 OCI…~~ **✅ 已解決（2026-06-25 複驗）**：`woow-paas-charts/README.md`（branch `docs/readme-operator-cd-oci-drift` merged）已更正為**傳統 Gitea Helm Repo（非 OCI）**——明寫 operator CD 由 `paas-app-bundle-cd/fleet.yaml` 從 in-cluster `…/api/packages/woow-paas/helm` 拉取，並加「尚未推送 OCI artifacts、請勿用 `oci://`」caveat。複驗無殘留 drift：`publish-charts.yaml` 確實只 `POST .../helm/api/charts`（classic helm repo、零 `oci` push）、operator `Chart.yaml` version `0.1.16` 與 CD pin 一致。追蹤 issue：[woow-paas-charts#26](https://git-prod.woowtech.io/woow-paas/woow-paas-charts/issues/26)（已關閉）

---

## 11. 自動化 checklist（給 harness agent）

以「把某 component 新版上 prod」為例，可機器執行的最小步驟：

```
[ ] 0. 前置：`tailscale status` 已連；kubectl --context woow-k3s 可連；tea login 已設 default
[ ] 0.5 pre-flight 起點實查（§5.1「步驟 0」）——不得用發版單/note 當起點
       起點 = prod DB latest_version ＋ pod imageID digest（兩者交叉）
       ＋ 獨立掃 CD repo 近期 commit（`git -C <cd> log --oneline -15 main`），
         不只查發版單點名的那些 pin —— 照發版單逐項驗收擋不掉「誤列」
       ＋ 按變更路徑分流兩條發版線（src/** vs extra/paas-operator/**；chart 是第三軸）
[ ] 1. 確認目標 image 已在 JCR（CI 綠燈 + registry 查得到 tag）
[ ] 1.5（paas-odoo）驗 image 裝的是本次那個 ref 的 addon（§5.1.4、§9 坑 #12）
       CI 綠燈、新 image_tag、新 digest **都證明不了這件事**
       build log 三行，**每一行都拿去跟本次版號核對**（不是核對「三行互相一致」）：
         `at ref:`               == v{本次版號}（看 at ref:，不是 at HEAD）
         `Cloned addon version:` == 本次版號          ← source 端
         `Image addon version:`  == 本次版號          ← image 端，句尾帶 matches clone
       ⚠ 這道現在是**複核、不再是唯一防線**：image 與 clone 不符 CI 自己會擋
         （#50 fail-closed，位在 push 之前 ⇒ 壞 image 進不了 registry）
       ⚠ 但 CI **擋不掉「ref 填錯」**——填錯時三行會一致地顯示錯的版號、gate 全綠；
         露餡的只有 `at ref:` 與「版號 vs 發版單」這兩個人工核對（§5.1.4）
       ⚠ 錨到的只是**版號**這一層：同版號改過檔案、addon 以外的內容都不在範圍
       任一對不上 = STOP，帶正確的 addon_ref=v{版號} 重跑，不得進 step 3
[ ] 2.（若改 chart）確認 chart 已在 Gitea Helm registry，且 Chart.yaml#version 已 bump
[ ] 2.5 無回歸超集 gate（§9 坑 #11）：新 image/chart 是 prod 現役的「超集」
       （`git merge-base --is-ancestor <現役來源 commit> <新來源 commit>`；版號單調不退；chart 模板 wiring 未砍）
[ ] 2.6（paas-odoo）發版前 DB snapshot —— 【人類操作者親跑，agent 不代勞】
       建 paas-odoo-{pg,data}-pre{版號}-{YYYYMMDD} 兩顆（PostgreSQL + filestore data）
       agent 職責僅止於：確認兩顆 readyToUse=true 且 markRemoved=false 才可繼續；
       未確認 = STOP，不得進 step 3（prod DB 無離線備份，snapshot 是唯一還原錨點，§5.1.1）
[ ] 3. clone/pull paas-app-bundle-cd，改對應 <component>/values-<env>.yaml image.tag
       （和/或 fleet.yaml version）；保留 §6 prod 專屬欄位
       paas-odoo：image pin 是【三處】，必須同改（§5.1 S1 陷阱）——
         ① image.tag  ② image.digest  ③ module-version-gate initContainer 的 image
         ③ 的前綴是 `image:` 不是 `digest:`，只 grep tag|digest 會零命中；
         漏 ②③ = promote no-op；漏 ③ = image 換了但 gate 讀舊版號而 skip 掉 -u
         ⇒ 新 code 跑在舊 schema 上，且三方 digest 閉環照樣全過（§9 坑 #21）
       paas-odoo 的 ODOO_SKIP_MODULES_UPDATE：**只在** seed 變更落到相依 addon 時才設 "no"（PR-A）
       只動 woow_paas_platform 者由 module-version-gate 自理，設了等於白開曝險窗（§5.1 引言判準）
[ ] 4. 自我核對 diff：只動 pin 值，未誤改 namespace/tunnel/prefix/replica
[ ] 5. tea PR（zh-tw 描述）→ 等審核 → merge main
[ ] 6. 等 Fleet reconcile：kubectl -n fleet-local get gitrepo paas-app-bundle
       → COMMIT == CD main HEAD 且 STATUS 空
[ ] 7. 閉環驗證：pod imageID(digest) == 預期新版；deploy readyReplicas 滿
       ＋（paas-odoo）**同一顆 pod 上 name=="module-version-gate" 那個
         initContainer 的 imageID 與 containerStatuses 的 imageID 逐字相同**
         —— 這是發版後唯一接得住「漏改 gate image」的檢查
         （§9 坑 #21；三層驗收接不住它，指令見 §5.1.5 ①）
         🔴 jsonpath 必須帶 [?(@.name=="module-version-gate")] 篩選器：
            pod 有兩個 initContainer（另一個是 busybox 的 patch-odoo-conf），
            寫 initContainerStatuses[*] 會永遠含 busybox digest ⇒ 永遠報紅
         🔴 兩行皆空 ≠ 通過（＝探針壞了）；先確認兩行都有值再判讀
[ ] 8. 站點 smoke：/woow /odoo /my 回 HTTP 2xx/3xx（4xx/5xx/000 重試 ≤3 次仍失敗 → 立即 §8 rollback）
[ ] 7.5（paas-odoo）§5.1.5 三層驗收 —— 三件會各自獨立失敗的事，上層過不蘊含下層過
       ① -u 有被觸發：gate 的 post-verify（DB latest_version 推進；不推進 pod 會卡 Init）
       ② -u 跑完沒出錯：`Updating modules` → `Odoo setup finished!` ＋ registry 行 `0 queries`
          ⚠ bitnami 的 Updating modules 輸出**不進 stdout**，找不到 `Running migration` 字樣是正常的
          ⚠ Traceback 要看時間戳：落在 `Registry loaded` 之後的是 ir_cron 固定噪音（issue #503）
       ③ 🔴 seed 值真的寫進 DB：**人工 psql 讀回本次變更的欄位**，確認是新值
          ①② 全綠但 ③ 失敗是可能的（sticky ir_model_data.noupdate ⇒ 版號推進但 record 靜默 skip）
          這一層**沒有任何自動化涵蓋**，不能用「pod 起來了」或「gate log 說完成」代替
[ ] 8.5（僅當步驟 3 設過 SKIP="no"）驗證通過後「≤30 分鐘內」merge PR-B 把
       ODOO_SKIP_MODULES_UPDATE 收回 "yes"（曝險窗，§5.1.2），再過 §5.1.3 收官三重確認：
       deployment spec / running pod env / Fleet BundleDeployment nonModified=true
       ⚠ 收官那輪 merge 也是一次 Recreate ⇒ 再一次 3–10 分停機，要算進窗口（§9 坑 #14）
[ ] 9.（agent-runtime 換版）另走 §5.3 連結的 reap SOP，勿用一般 rollback/restart
[ ] 10. 任一步失敗 → §8 rollback（改回前一版 digest 對應的 tag + digest + gate image
        【三處同退】；漏 ③ ＝ 舊 code 跑在新 schema 上，且看起來一樣成功 → merge）
        已跑過 -u 者：image 退版「不會」回退 schema，DB 層還原走 §5.1.1 的 pre-release snapshot
[ ] 11. 回報：每一項驗收標明 ✅已驗（列實例數）／⊘無對象可驗（說明母體為何是 0）／⚠未驗（說明原因）
        🔴 **不得只寫「✅」**——「沒有東西可驗」與「驗過了」在勾選表上長得一模一樣，
        差別只存在於執行者當下的腦子裡，回報出去就永久消失（§7 驗收通則）
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
