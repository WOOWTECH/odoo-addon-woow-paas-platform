---
title: 發佈到 Production 的標準作業流程（SOP）
category: guides
status: active
created: 2026-06-14
updated: 2026-06-25
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
>
> **2026-06-21 更新**：operator **#207/#208**（#207 mcp route delete 對齊 `generate_subdomain`；#208 PVC `orphaned-at` label 改 epoch 秒 + `validate_label_value` fail-loud）已發佈 **stg**（chart `0.1.15` / image `20260620.1406`，digest `1b322c8a…c1fbc`，端到端驗證通過），**現為 prod 待 promote 候選**（prod 仍 `0.1.8` / `20260525.0054`，落後）。本次 refresh 同步了 §2 stg 列、§5.2.1 worked example、§9 坑 #11（無回歸超集）、§10.1 drift 狀態（live 複驗仍 `Modified`）。
>
> **2026-06-24 更新**：naming env-token **L1-L3**（PR #214）已上 **stg** 並 live 驗證（operator chart `0.1.16` / image `20260624.1022`、`PAAS_ENV=stg`、ns label `paas.woowtech.io/env=stg`、Odoo SA `canonical_name=paas-stg-sa-`；當時 prod 未動仍 `0.1.8` / `20260525.0054`——已於隔日 promote，見下）。**§6 新增「兩端 env 一致」檢查（F-ENV-1b）**：promote operator/addon 到 prod 時，operator `PAAS_ENV`（Fleet `config.paasEnv`）與 Odoo `deploy_env` config_parameter **必須同為 `prod`**，否則 split-env（ns label 與 CF 命名對 env 各說各話）。§5.2.1 gate 同步加 `paasEnv: "prod"` 檢查。
>
> **2026-06-25 更新**：① **prod operator 已 promote 到 `0.1.16` / image `20260624.1022`（digest `9b937dbb…6b16`，helm rev 42，2026-06-25 06:03 UTC，paas-cd PR #27）** —— 不只追上、且已與 stg **同版**（stg 亦 `0.1.16` / `20260624.1022`，rev 65）。§2 矩陣與 §5.2.1 worked example 的「prod 仍 0.1.8」前提已**過時**（promote 在他處完成）。② **§10.1 issue #18（Fleet `paas-operator-registry` drift）已解決**（亦即 follow-ups **F-ENV-6**）：live secret 上殘留的 `registry-server`/`registry-username` 兩個 key 經 paas-charts 複核確認**零 consumer**（chart 自 0.1.6 起就單 key 模型、server/username 一直走 plaintext deployment env），移除後 bundle 收斂 `ready:1`、GitRepo `5/5`。原 pitfall/F-ENV-6 的「split-values 非 deep-merge 吃掉 server」框架為**誤診**，已於對應 pitfall doc 更正。

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
| paas-odoo | `paas-system` | `…/paas-odoo:20260710.0902`（addon `18.0.1.0.51`；#318/#322 訂閱聯動 + #324 計費雙缺口；commit 281cbae） | `4412ed69…c764`（manifest；pod imageID 因 prod+stg 共 cluster containerd digest dedup 報 stg repo 別名 `paas-odoo-stg:20260710.0349`，內容同此 digest） | Bitnami `odoo`（OCI） | `28.2.10` | tag→`paas-odoo/values-prod.yaml`（image 區塊）；chart→`paas-odoo/fleet.yaml` `helm.version` |
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
   （格式 18.0.{major}.{minor}.{patch}，目前 18.0.1.0.40）
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

> **odoo chart（Bitnami `28.2.10`）** 平常不動；只有要升 Odoo 框架本身時才改 `paas-odoo/fleet.yaml` 的 `helm.version`（**高風險**：schema / addon 相容性，須先 stg / sandbox 驗證穩定，**勿與 image bump 同 PR**）。

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
   git -C /tmp/paas-app-bundle-cd log -p -2 -- paas-operator/values-prod.yaml | grep -E '^[+-] *tag:'
   # 或讀 values 檔內保留的「取代 …（sha256:…）」註解，取上一版 tag + digest
   ```
2. 在 paas-app-bundle-cd 把 `image.tag`（和/或 `fleet.yaml` chart version）改回前一版（**用前一版的 digest 反查確認 tag** 以防 mutable tag 漂移）。
3. merge `main` → Fleet 自動 `helm upgrade` 回退。
4. 緊急時（等不及 PR 流程）可 `git revert` 觸發 commit 後直接 merge。
5. 驗證 pod digest 已回到舊版（§7）。

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
| 11 | 把比 prod 現役**更舊或非超集**的 image/chart 推上 prod | 靜默回歸：prod 失去現役版本已有的修復（如 operator D1-D6 生命週期 teardown、Access 契約） | promote 前過超集 gate：`git merge-base --is-ancestor <現役來源 commit> <新來源 commit>`；manifest/版號單調不退；chart 用 `git log` 確認模板 wiring（如 `AGENT_RUNTIME_SKILL_RUNNER_ENABLED` env passthrough）沒被砍（見 §5.2.1 step 0） |

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
[ ] 1. 確認目標 image 已在 JCR（CI 綠燈 + registry 查得到 tag）
[ ] 2.（若改 chart）確認 chart 已在 Gitea Helm registry，且 Chart.yaml#version 已 bump
[ ] 2.5 無回歸超集 gate（§9 坑 #11）：新 image/chart 是 prod 現役的「超集」
       （`git merge-base --is-ancestor <現役來源 commit> <新來源 commit>`；版號單調不退；chart 模板 wiring 未砍）
[ ] 3. clone/pull paas-app-bundle-cd，改對應 <component>/values-<env>.yaml image.tag
       （和/或 fleet.yaml version）；保留 §6 prod 專屬欄位
[ ] 4. 自我核對 diff：只動 pin 值，未誤改 namespace/tunnel/prefix/replica
[ ] 5. tea PR（zh-tw 描述）→ 等審核 → merge main
[ ] 6. 等 Fleet reconcile：kubectl -n fleet-local get gitrepo paas-app-bundle
       → COMMIT == CD main HEAD 且 STATUS 空
[ ] 7. 閉環驗證：pod imageID(digest) == 預期新版；deploy readyReplicas 滿
[ ] 8. 站點 smoke：/woow /odoo /my 回 HTTP 2xx/3xx（4xx/5xx/000 重試 ≤3 次仍失敗 → 立即 §8 rollback）
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
