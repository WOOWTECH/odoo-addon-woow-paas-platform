---
title: "租戶 ns baseline egress 收斂設計案（#514）"
category: plans
status: draft
created: 2026-08-22
updated: 2026-08-22
related:
  - docs/decisions/427-operator-ingress-baseline.md
  - docs/plans/2026-Q3/2026-07-04-netpol-tenant-isolation-280-279-operator-spec.md
  - docs/tracking/landings/404-apiserver-cidrs-landing.md
  - docs/tracking/landings/425-pg-netpol-hotfix.md
  - docs/tracking/pitfalls/netpol-enforced-except-fresh-pod-startup-window-k3s-kube-router.md
  - docs/archived/tracking/stg-sandbox-networkpolicy-cross-ns-block.md
---

# 租戶 ns baseline egress 收斂設計案（#514）

> **上游**：[#514](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/514) 待辦 1（盤點）＋ 2（設計）。待辦 3（stg 演練）／4（rollout）／5（驗收）不在本檔執行範圍，本檔只寫下步驟與回滾路徑。
> **本檔範圍**：只寫 design ＋ 盤點。**未改動任何 `.py` / `.xml`**。
> **本檔不含任何叢集寫入**：撰寫期間**未執行任何 kubectl**（唯讀或寫入皆無），所有叢集事實均引自 repo 內既有紀錄並標明出處。
> **base**：`security/514-baseline-egress` @ `8f5ec5be`（＝`origin/develop`，`git merge-base` 與 `rev-parse` 同值）。

---

## 0. TL;DR

租戶 ns 加 egress default-deny 的**技術形狀是清楚的**（既有 agent-runtime chart leg-1 已在 prod 跑同一套 `ipBlock 0.0.0.0/0 except 六私網段` ＋ DNS namespaceSelector），但**盤點結果顯示本案有三個結構性阻塞**，在拍板前無法收斂成一個可安全 rollout 的規則集：

1. **覆蓋度不可判定**：14 個在售 template 中，10 個的 chart NP egress 段在 `woow-paas-charts`（本 repo 不可存取）。K8s NetworkPolicy 是 **allow-union** 語意 —— chart 若已有 `allowAllEgress`，ns 級 deny 對它**完全無效**（安全性零進步）；chart 若沒有，那些服務會在加上 `Egress` policyType 的**當下全部斷線**。兩個結論的行動相反，而現在分不出是哪一個。→ §10 五道跨 repo 提問。
2. **兩個 template 明文要求 egress 全開**（ha-full 的 HACS→github.com、HAOS 的 HA 整合），已有 A/B 實測坐實。若 baseline 為它們開 allow-all-external，那條規則會 union 到**同 ns 每一顆 pod** 上 —— 包含 agent-runtime pod，**把它刻意預設 OFF 的 `externalEgress` 給打開**（安全回歸，§6 F-7）。
3. **apiserver 供給鏈斷點**：baseline 若要蓋 console 的 apiserver 出向，operator 端**今天沒有任何讀 apiserver CIDR 的程式碼**（值只存在 Odoo 側 `ir.config_parameter`）。供給鏈怎麼接是本檔列出的 **D-1 待拍板決策**，四個選項在 §9。

另有三條**已實證、會讓「小心翼翼寫對的規則」照樣炸掉**的地雷（§6）：K3s enforcer 的 **reply-path quirk**（加 egress 會打斷已經寫對的 ingress，stg 全站曾因此對外 502）、**`allow-same-ns` 是 ingress-only**（加 Egress 後 Odoo→自己的 DB 沒有任何 allow 覆蓋）、**回滾刪掉的 NP 會被下一次部署靜默復原**。

**建議**：本 PR 只落 design；跨 repo 五問回來、D-1～D-4 拍板後，再開 implementation。

### 待拍板決策速覽（詳見 §9）

| # | 決策 | 建議 |
| --- | --- | --- |
| D-1 | apiserver CIDR 供給鏈怎麼接到 operator | **選項 B**（operator 新增 `baseline_egress_apiserver_cidrs` setting，走既有 CD values 三 repo lock-step），理由見 §9.1 |
| D-2 | agent ns（`paas-ws-pa-*`）納不納入本案 | **納入但獨立開關**，理由見 §9.2 |
| D-3 | 「外網維持通」是 baseline 給，還是留給 per-chart | **baseline 給，但拆成獨立可關的一條**，理由見 §9.3 |
| D-4 | 逐 ns 開關住哪裡（operator setting / ns label / Odoo 參數） | **ns label**（唯一能做到「單 ns 開／關」且不必重啟 operator），理由見 §9.4 |

---

## 1. 範圍與方向定義

### 1.1 方向（本案只管 egress）

| 流向 | 方向（對租戶 ns 而言） | 本案管轄 |
| --- | --- | --- |
| operator → 租戶 pod（`kubectl exec` 做 post-deploy init） | **ingress** | ❌ 不管 |
| Odoo → operator → 租戶 pod | **ingress** | ❌ 不管 |
| cloudflared → 租戶 app pod（使用者網頁路徑） | **ingress** | ❌ 不管（但見 §6 F-1 reply-path） |
| Odoo → agent pod WS `/v1/chat` | **ingress** | ❌ 不管 |
| 租戶 pod → 外網 / 平台 / 叢集內 | **egress** | ✅ **本案標的** |

**證據**：operator 對租戶的所有動作走 kubectl（apiserver），init 也是 `kubectl exec` 進 pod 後在 pod 內打 loopback（`extra/paas-operator/src/services/odoo_init.py:168-182`、`extra/paas-operator/src/services/n8n_init.py:38,61`）。operator 全樹 grep `httpx`/`requests`/`aiohttp` 沒有任何指向租戶 `svc.cluster.local` 的呼叫，其自身出向只有 Cloudflare API 與平台 Odoo webhook（`extra/paas-operator/src/services/webhook_dispatcher.py:24-26,40`）。

> ⚠ **工單前提訂正**：派工單寫「issue #427 明確要求『租戶 ns → odoo webhook 回呼』要納入 baseline allow」。實查 `agent_runtime_webhook.py` 的**發送端是 operator**（`webhook_dispatcher.dispatch_state_update(odoo_base_url, ...)`，`extra/paas-operator/src/services/webhook_dispatcher.py:30-56`），operator 跑在 `paas-system` / `paas-stg-system`，**不在租戶 ns** ⇒ 這條是 operator 的 egress，不是 #514 標的。#427 決策文件實際留下的交棒句是「paas-odoo 的 ingress 仍是 from-less…egress 方向未動、租戶 ns baseline egress → #514」（`docs/decisions/427-operator-ingress-baseline.md:21`），沒有 webhook 字樣。
>
> 真正存在的「租戶 ns → 平台 Odoo」出向是**另一條**：agent-runtime pod 拉 skill-bundle（`GET /api/agent-runtimes/skill-bundle`，`src/controllers/agent_runtime_skill.py:5`），見 §3 W-2。

### 1.2 ns 範圍：`paas-ws-*` 有兩族，只有一族走 baseline

| ns 形狀 | 誰建 | 走不走 `_apply_baseline_netpol` |
| --- | --- | --- |
| `paas-ws-<ws_hash>` / `paas-ws-stg-<ws_hash>`（CloudService 租戶 ns） | `KubernetesService.create_namespace`（`extra/paas-operator/src/services/helm.py:1169-1281`） | ✅ 有（`helm.py:1266-1268`，且 `workspace_id is None` 時 warn+skip） |
| `paas-ws-pa-<hash>` / `paas-ws-stg-pa-<hash>`（agent-runtime per-project ns） | `agent_runtimes._ensure_namespace`（`extra/paas-operator/src/api/agent_runtimes.py:90-144`） | ❌ **完全沒有** —— 該函式只 apply Namespace / ResourceQuota / LimitRange 三件 |

🔴 **這是本案最容易只改一半的地方**：agent ns 的名字**字面落在 `paas-ws-*` glob 內**（`agent_runtimes.py:61-70` 由 `settings.namespace_prefix` 衍生），所以「按 ns 前綴佈規則」與「改 `_apply_baseline_netpol`」**不是同一個集合**。派工單說的標的是「`paas-ws-*` / `paas-ws-stg-*`」＝前者；派工單說的落點是 `_apply_baseline_netpol` ＝後者。→ **D-2 待拍板**。

---

## 2. 盤點表：16 個 marketplace template × 合法出向

**枚舉基準**：`src/data/cloud_app_templates.xml` 內 `<record model="woow_paas_platform.cloud_app_template">` 共 **16** 個（同檔另有 2 個 `<function>` 非 record，行 1549 / 1566）。`subscription_template_links{,_variants}.xml` 也有同 model 的 `<record>`，但全部是**重用既有 xml id 更新 `subscription_plan_ids`**，沒有第 17 個 slug。

**在售狀態**：14 個 `is_active=True`；`postgresql` 與 `redis` 為 `False`（仍列出，因為 `is_active` 是 Odoo 欄位、隨時可翻）。

**chart 歸屬**：4 個第三方（bitnami ×3、8gears ×1）、12 個自家（`oci://jcr-prod.woowtech.io/woow-paas-docker-local/*`，實體只有 6 支 chart：`mcp-server` ×7、`home-assistant`、`home-assistant-full`、`open-design`、`hermes-agent`、`haos`）。

> 📌 **本表的「證據」欄只能證明 seed／平台碼這一側。** 每個 chart 的 NP egress 段實體在 `woow-paas-charts`（本次不可存取）⇒ 表末 `chart NP?` 欄一律標 `?`，除非 seed 註解明文寫了。這正是 §10 五道跨 repo 提問的來源。

| # | slug（chart） | active | 合法出向標的 | 協定/port | 為什麼需要 | 證據 | chart NP? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `postgresql`（bitnami 18.8.5） | ❌ | seed 未指定任何出向（DB 是 listener） | — | standalone 無 replica 複寫 | `cloud_app_templates.xml:38-42` | ? |
| 2 | `n8n`（8gears 2.0.1，**第三方**） | ✅ | **① 任意公網 endpoint**（租戶 workflow 自填，本質不可枚舉）<br>② `config.database.type` 可選 `postgres`，但 seed **沒有**對應 host 鍵 | TCP/443、TCP/80<br>（②：TCP/5432，目標不明） | 「connect apps and services…280+ integrations」＝產品定義本身 | `:65-67`（產品定義）、`:94`（seed sqlite）、`:111-112`（可選 postgres） | ? |
| 3 | `redis`（bitnami 27.0.18） | ❌ | seed 未指定 | — | standalone | `:178-182` | ? |
| 4 | `odoo`（bitnami 28.2.10，**第三方**） | ✅ | **① 同 ns 的 postgresql**（`postgresql.enabled: true` subchart）<br>② 租戶自填 SMTP host | ①TCP/5432<br>②TCP/587（預設，可改） | ① 沒 DB 完全無法啟動；且 `customPostInitScripts.fix-admin-credentials.sh` 在**容器啟動期**用 psycopg2 直連 `ODOO_DATABASE_HOST`，`set -eo pipefail` ⇒ 連不上直接 exit 1<br>② mail 送信 | `:242`（`postgresql.enabled` ＋ 內嵌 psycopg2 腳本）、`:252-262`（smtpHost/Port/Protocol specs） | ? |
| 5-11 | `mcp-hermes` / `mcp-odoo` / `mcp-odoo-manage` / `mcp-n8n` / `mcp-k8s` / `mcp-vk` / `mcp-opendesign`（皆 `mcp-server` 0.1.5） | ✅×7 | **租戶在 pod admin GUI 自填的 `connection.*`**：<br>`mcp-k8s`→**外部** K8s 叢集 apiserver（kubeconfig）<br>`mcp-vk`→外部 Vibe Kanban 後端<br>`mcp-opendesign`→Open Design daemon（範例是**同 ns** ClusterIP `http://svc-xxxxxxxx:7457`） | 依租戶而定；`mcp-opendesign` 範例 TCP/7457 | 這三個 template 的 connection 欄位為必填、值由租戶提供 | `:570-576`（mcp-k8s 明文「以 kubeconfig 連接**外部** K8s 叢集」）、`:603-604`（vk）、`:641-648`（opendesign） | ? |
| | | | 🔴 **平台完全不供給也不持有這些值** ⇒ Odoo 側**無法枚舉**，任何「由平台產生 allowlist」的做法對它們天生失效 | | | `:315-318`（connection.* 是 GUI-owned） | |
| 12 | `ha-paas`（home-assistant 0.2.1） | ✅ | **本 repo 內完全空白** —— seed 無任何 `networkPolicy` 鍵、無出向線索 | 未查證 | HA 產品性質推測需要外網＋DNS，但**無證據** | seed 全欄位掃過無 networkPolicy（`:721-790`） | **?（最大空白）** |
| 13 | `open-design`（open-design 0.1.4） | ✅ | **① kube-apiserver**（console 跑 `kubectl exec` 進 daemon pod）<br>② Anthropic API（內建 Claude Code／OpenCode，console 可跑 `claude login`）<br>③ 同 ns daemon（console→daemon） | ①TCP/443（ClusterIP）＋**post-DNAT 後 TCP/6443 到 node IP**<br>②TCP/443（推定）<br>③TCP/7457 | ① console 是獨立 Deployment，靠 kubectl 定位 daemon pod<br>② 產品內建 AI coding agent | ①`src/models/cloud_service.py:1885-1922`（`_inject_apiserver_cidrs` path_map 含 `open-design`）<br>②`cloud_app_templates.xml:797-800, 833` | 有（`networkPolicy.apiServerCidrs`），其餘段 ? |
| 14 | `hermes-agent`（hermes-agent 0.2.0） | ✅ | **① kube-apiserver**（同 ② console，與 open-design 共用同一顆 console image）<br>② MiniMax / OpenRouter LLM API<br>③ **任意套件 registry**（租戶在容器內跑 `uv tool install` / `npm i -g` / `go install` / `cargo install`）<br>④ 任意外部 MCP server ＋ 其 OAuth authorization server | ①同上<br>②③④ TCP/443 | ① console 同構<br>② 租戶自帶 LLM key<br>③ **已交付的租戶功能**（第四執行面）<br>④ `_inject_dashboard_public_url` 注入的 `redirect_uri` 用途就是對外 OAuth | ①`cloud_service.py:1907-1912`（path_map 含 `hermes-agent`）<br>②`cloud_app_templates.xml:959, 1034-1037`<br>③`docs/decisions/hermes-agent-installer-landing-env.md:41-46`<br>④`cloud_service.py:1803-1812` | 同上 |
| 15 | `haos`（haos 0.1.2，KubeVirt VM） | ✅ | **① 外網**（HA 整合 API、更新檢查、HA Cloud）<br>② kube-dns<br>③ 同 ns：`xff-proxy` Deployment → `{release}-haos-vm` Service | ①TCP/443<br>②UDP+TCP/53<br>③TCP/8123 | ① chart `networkPolicy.allowAllEgress` 預設 true，**2026-07-28 W1 PoC 已做決定性 A/B：移除它 → TCP/443 被擋；還原 → 連通** | ①②`docs/plans/2026-Q3/2026-07-27-haos-kubevirt-v1-design.md:329,333,337`<br>③`cloud_app_templates.xml:1255-1260` | **有**（`enabled=true`，`ingressNamespaces` 刻意留空；egress 側 seed 未說） |
| 16 | `ha-full`（home-assistant-full 0.1.1） | ✅ | **外網 github.com**（HACS 下載）＋ HA 整合各自的外部 API | TCP/443 | seed 逐字：「`networkPolicy.allowAllEgress`：chart 預設 true 且**必須維持**（HACS 要從 github.com 下載；**DNS-only egress 會讓它變成裝不了東西的整合，且沒有清楚錯誤**）」 | `cloud_app_templates.xml:1492-1494` | **有**（`allowAllEgress: true`） |

### 2.1 明確**排除**的項目（防止過度列舉）

| 項目 | 為什麼不是 pod egress | 證據 |
| --- | --- | --- |
| container image pull（`jcr-prod` / `docker.io` / `ghcr.io` / `8gears`） | kubelet 在 **node network namespace** 發起，不受 pod NetworkPolicy 管 | `extra/paas-operator/src/services/helm.py:1502-1507`（operator 只放憑證進 ns） |
| n8n 的 MCP sidecar → n8n 主容器 | `N8N_API_URL=http://localhost:5678` ＝**同 pod loopback**，不過 NP | `src/models/cloud_service.py:2196-2201` |
| n8n / odoo 的 post-deploy init | operator `kubectl exec` 進 pod 後在 pod 內打 loopback，全程無租戶 pod 對外連線 | `extra/paas-operator/src/services/n8n_init.py:38,61`、`odoo_init.py:168-182` |
| `_inject_storage_class` / `_inject_image_pull_secrets` | 注入的是 StorageClass 名與 Secret 名，**不是位址** | `cloud_service.py:1669-1765` |
| `_inject_allowed_origins` | `config.allowedOrigins` 是**入向** Origin header 比對 | `cloud_service.py:1768-1802` |
| HAOS 的 VM 磁碟 image | seed 釘 `dataVolume.registry.pullMethod=node` ⇒ node 的 containerd 拉，非 pod 出向 | `cloud_app_templates.xml:1204-1211` |

---

## 3. 非主容器 workload 盤點（漏一類＝漏一整組故障）

ns 級規則的 `podSelector: {}` 選中 ns 內**每一顆 pod**，包含 chart podSpec 之外由 controller 生成的。逐類列出：

| id | workload | 類型 | 在哪個 ns | 出向標的 | port | 證據 |
| --- | --- | --- | --- | --- | --- | --- |
| **W-1** | `agent-runtime` pod（＋同 pod 的 `skillRunner` bash sidecar） | Deployment（chart 自帶） | `paas-ws-pa-*` / `paas-ws-stg-pa-*` | ① kube-dns ② 同 workspace CS ns 的 MCP ③ 外網 LLM（`externalEgress` 開時） | ①UDP+TCP/53 ②TCP/3000+3001 ③TCP/443+80 | `agent_runtimes.py:192`（`egressWorkspaceIds`）、`config.py:50-54`（`externalEgress` 預設 **OFF**）、`config.py:69`（skillRunner） |
| **W-2** | 同上 pod | — | 同上 | **平台 Odoo（skill-bundle fetch）** | prod TCP/**80**（svc；post-DNAT 後為容器埠，見 §6 F-3）／stg TCP/8069 | `agent_runtimes.py:147-162`（`_derive_odoo_namespace`）、`config.py:49`（stg 值逐字 `…dev-sandbox-stg.svc.cluster.local:8069`）、`src/controllers/agent_runtime_skill.py:5`（receiver route） |
| **W-3** | `console` Deployment（open-design 0.1.4 / hermes-agent 0.2.0，**同一顆 image**，不是 sidecar） | Deployment（chart 自帶） | CS 租戶 ns | **kube-apiserver**（跑 `kubectl exec` 進 daemon pod） | TCP/443（ClusterIP `10.43.0.1`）**＋ post-DNAT 後 TCP/6443 到 control-plane node IP** | `cloud_service.py:1885-1922`、`docs/reference/features/opendesign-console.md:137-145` |
| **W-4** | `virt-launcher-*` pod（HAOS 的 VM 實體，masquerade 網路 ⇒ VM 全部流量從這顆 pod IP 出去） | **KubeVirt controller 生成**，不在任何 chart podSpec | CS 租戶 ns | 外網（HA 整合／更新檢查／HA Cloud）＋ kube-dns | TCP/443、UDP+TCP/53 | `docs/plans/2026-Q3/2026-07-27-haos-kubevirt-v1-design.md:329-337` |
| **W-5** | `importer-*` pod（CDI DataVolume import） | **CDI controller 生成**，不在任何 chart podSpec | CS 租戶 ns | `pullMethod=node` 下**推定無** pod 出向；若漂移成 `source: http` 則是**外網大檔下載** | （http 模式）TCP/443 | `cloud_app_templates.xml:1204-1211`、`helm.py:1496-1507`（operator 把 registry secret merge 進 default SA，其存在理由就是「controller 生出來、不屬任何 chart podSpec 的 pod」） |
| **W-6** | `xff-proxy` Deployment（HAOS chart 0.1.2 的 XFF 剝除代理） | Deployment（chart 自帶） | CS 租戶 ns | **同 ns** ClusterIP `{release}-haos-vm` | TCP/8123 | `cloud_app_templates.xml:1255-1260` |
| **W-7** | MCP sidecar（`mcp-sidecar` 容器，operator 以 JSON patch append 進 n8n Deployment） | sidecar | CS 租戶 ns | **loopback**（不過 NP） | — | `extra/paas-operator/src/api/releases.py:1344-1371`、`cloud_service.py:2196-2201` |
| **W-8** | initContainers：`addon-selector`（Odoo，平台注入）／`seed-config`（HA、mcp-server 系列）／`seed-home`（open-design）／`authproxy-init`＋`cli-landing-init`（hermes-agent）／`guest-console-log`（KubeVirt 生成） | initContainer | CS 租戶 ns | **全部無 egress 需求**（純本機 `cp` / `sed` / `mkdir` / symlink / htpasswd 計算） | — | `cloud_service.py:1595-1616`（addon-selector）、`cloud_app_templates.xml:715-722`、`:817-827`、`docs/decisions/hermes-agent-installer-landing-env.md:470-477` |
| **W-9** | Job / CronJob / helm hook pod | — | — | **本 repo 內不存在**：`src/` 與 `extra/paas-operator/src/` 全樹 grep `Job` / `batch` / `cronjob` / `initContainers` 四種 pattern 對 operator 零命中 | — | `extra/paas-operator/src/`（目錄層級零結果；同目錄 `namespace` 有大量命中作陽性對照） |

> ⚠ **W-9 的一個未結項**：#427 交接留下「`podSelector: {}` 會連帶選中 **pvc-cleanup Job pod**（判斷無害但未實測）」（`docs/decisions/427-operator-ingress-baseline.md:21`）。該 Job 由 operator 的 PVC lifecycle 產生，但它跑在**哪個 ns** 本次未查證 —— 若在租戶 ns，它跑 `kubectl delete pvc` ⇒ **需要 apiserver egress**，會是本案的第二個 apiserver 消費者。**列入 §11 未查證清單**。

---

## 4. 叢集事實（規則形狀的前提，全部有 repo 內出處）

| 事實 | 值 | 出處 |
| --- | --- | --- |
| CNI / NP enforcer | flannel ＋ **K3s 內建 kube-router-based netpol controller**（無 Calico/Cilium） | `docs/tracking/pitfalls/netpol-enforced-except-fresh-pod-startup-window-k3s-kube-router.md:27` |
| pod CIDR | `10.42.0.0/16` | 同上（另有 seed 端 load-bearing 佐證：HA template 的 `haProxy.trustedProxies`，`cloud_app_templates.xml` ha-paas/ha-full 兩處＋測試常數釘住） |
| service CIDR | `10.43.0.0/16` | 同上（另有 code 佐證：`cloud_service.py:1869-1881` 把 k3s kubernetes Service ClusterIP `10.43.0.1` 寫成預設值） |
| node 私網 | `192.168.10.0/24`（4 節點） | 同上 |
| control-plane node IP | `192.168.10.21` / `.22` / `.23`（第 4 顆 worker IP repo 內未記載） | `docs/reference/features/opendesign-console.md:145,159`；**實際已設為 stg＋prod 參數值** `10.43.0.1/32,192.168.10.21/32,192.168.10.22/32,192.168.10.23/32`（`docs/tracking/landings/404-apiserver-cidrs-landing.md:10`） |
| 🔴 **NP egress 比對時機** | **post-DNAT** —— 連 ClusterIP 的封包在比對時目標已被 kube-proxy 改成真實 endpoint（backend pod IP 或 node IP） | `cloud_service.py:1869-1881`（docstring 逐字）、`docs/reference/features/opendesign-console.md:137-143` |
| 🔴 **post-DNAT 也適用於 port** | NP 的 port 必須寫**容器埠**，寫 Service 的 port 會全擋 | `docs/decisions/427-operator-ingress-baseline.md:17`（逐字：「NP 的 port 要寫 **8000**（容器埠，NP 是 post-DNAT，寫 service 的 80 會全擋）」） |
| NP-block 的失效形狀 | **`ECONNREFUSED` / TCP RST，1–2s 快失敗**，不是 timeout（k3s enforcer 用 REJECT） | `docs/tracking/landings/netpol-279-280-landing.md:26`、`docs/decisions/427-operator-ingress-baseline.md:17` |
| NP 對新建 pod 的編程空窗 | ingress-allow ≈ **1s**、egress-deny ≈ **2s**（個位數秒） | `docs/tracking/pitfalls/netpol-enforced-except-fresh-pod-startup-window-k3s-kube-router.md:21` |
| 租戶對外網的實際出口 | LAN gateway SNAT 後的 HINET 公網 IP `114.32.21.18`（4 節點同一出口）；bare metal、node EXTERNAL-IP `<none>`、**無雲端 IMDS** | `docs/decisions/agent-dataplane-in-cluster-no-cf-access-ip-bypass.md:40,44` |
| kubelet probe 來源 | 各 node 的 cni0 bridge IP `10.42.x.1`（**不是** node IP） | `docs/decisions/427-operator-ingress-baseline.md:28`（ingress 側實測；對 egress 不直接適用） |
| 平台 ns 的 label | **`paas-system` / `paas-stg-system` / `dev-sandbox-stg` 永遠沒有平台打的 label** —— 兩個 stamping 入口都在 `validate_namespace` 的 `namespace_prefix` 閘後面，平台 ns 一律不符 ⇒ 只剩 k8s 內建的 `kubernetes.io/metadata.name` 可選 | `helm.py:297-311, 1191`、`agent_runtimes.py:117`；文字佐證 `docs/decisions/427-operator-ingress-baseline.md:31` |
| 平台服務座標（prod） | operator svc `paas-operator` @ `paas-system` port 80（容器 8000）；Odoo svc `paas-odoo` @ `paas-system` port **80（非 8069）** | `docs/plans/2026-Q2/2026-06-26-prod-agent-runtime-enablement.md:14`（執行偏差段逐字：「prod odoo svc port **80**（非 8069）」；同檔 :238 的 8069 是**草稿期望值**，已被該行推翻） |
| 平台服務座標（stg，**與 prod 不對稱**） | operator svc `paas-operator-stg` @ `paas-stg-system` port 80；**平台 Odoo 不在平台 ns**，住在 `dev-sandbox-stg`（svc `stg-odoo-dev-sandbox-odoo:8069`／nginx `stg-odoo-dev-sandbox-nginx:80`） | `config.py:49`（逐字含 FQDN）、`docs/guides/testing/security-access-stg-e2e.md:16`、`docs/decisions/427-operator-ingress-baseline.md:15`（#427 動工前攔下的漏列 ①） |
| docker-desktop 的網段 / NP enforcement | **repo 內未記載**（唯一相關事實：其 k8s node 已改成 kind 風格容器 `desktop-control-plane`） | `docs/guides/testing/docker-desktop-ci-local-sandbox-image-import.md:9`；follow-ups.md:1212 另記「CNI 若不 enforce egress NP 則 NP 直接變 no-op」屬 HIGH |

---

## 5. 規則設計

### 5.1 設計原則

1. **沿用 ns 級 `podSelector: {}`**，不退化成 per-chart。理由與 #427 相同：per-chart 契約對第三方 chart 結構上不成立（`docs/decisions/427-operator-ingress-baseline.md:11`），且 n8n 的 MCP sidecar 是 operator JSON patch 進去的、零 chart NP，ns 級是唯一涵蓋得到的機制。W-4/W-5（KubeVirt / CDI controller 生成的 pod）同理 —— 它們不帶 chart selectorLabels，chart NP 選不到。
2. **平台 ns 一律用 `kubernetes.io/metadata.name`**，且**刻意不 AND env qualifier** —— 照抄既有 `allow-cloudflared` 的形狀（`helm.py:452-459, 1371-1378`）：ns 名稱本身就分 env（prod `paas-cloudflare-system` / stg `paas-stg-cloudflare-system`），**env-safe by construction**。這比 AND `paas.woowtech.io/env` 更安全，因為平台 ns 根本沒有那個 label（§4）。
3. **凡是用 workspace-id 類 label 選 ns 的規則，必須 AND env qualifier**（`_env_match_labels()`，`helm.py:443-449`）—— workspace int id 是 per-Odoo-DB、非叢集唯一，prod/stg 共用同一物理叢集。本設計的 egress 腿目前**沒有**這種規則（都走 metadata.name 或 ipBlock），但新增時必須遵守。
4. **每條規則獨立成一個 NetworkPolicy 物件**（沿用現行 bundle 的形狀），這樣「單條關掉」＝刪一個物件，而不是改一個大物件的 spec。

### 5.2 規則清單（草稿 YAML）

> 全部 `namespace: <tenant-ns>`、`labels: {paas-managed: "true"}`、`podSelector: {}`、`policyTypes: ["Egress"]`。以下只列 `metadata.name` 與 `spec.egress`。

**E-0　`paas-baseline-egress-default-deny`** — 這條是「開關」本身
```yaml
spec:
  podSelector: {}
  policyTypes: ["Egress"]
  # 無 egress key ＝ 全拒。這一條存在與否，決定整個 ns 是不是 default-deny egress。
```
> 對應盤點表：無（它是 deny 側）。**單 ns 回滾＝刪掉這一條**（§7.3）。

**E-1　`paas-baseline-egress-allow-dns`** — 對應：**所有** workload
```yaml
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - {protocol: UDP, port: 53}
        - {protocol: TCP, port: 53}
```
> 📌 **刻意只用 namespaceSelector、不加 `podSelector: {k8s-app: kube-dns}`**。派工單建議加 podSelector 收緊，但 **k3s CoreDNS pod 的實際 label 值本 repo 內未記載**（未查證），猜錯的失效形態是「整個 ns 所有 pod 名稱解析全掛」。repo 內既有、且已在 prod 跑的形狀就是 namespaceSelector-only（`docs/plans/2026-Q2/agent-runtime-pod-design.md:3476-3486`）。**收緊到 podSelector 列為 rollout 後的 follow-up**，前提是先用 `kubectl -n kube-system get pod -l k8s-app=kube-dns` 量到非空。

**E-2　`paas-baseline-egress-allow-same-ns`** — 對應：#4 odoo→postgresql、#5-11 mcp-opendesign→daemon、W-6 xff-proxy→VM、W-3 console→daemon
```yaml
  egress:
    - to:
        - podSelector: {}
```
> 🔴 **這條絕不能漏**：現行 `paas-baseline-allow-same-ns` 是 **Ingress-only**（`helm.py:1352` 把 `policyTypes` 寫死成 `["Ingress"]`）。加上 Egress policyType 之後，同 ns app→自己的 DB **沒有任何 egress allow 覆蓋**，而 `except 10.0.0.0/8` 又把 pod CIDR `10.42.0.0/16` 一起擋掉（§6 F-2）。

**E-3　`paas-baseline-egress-allow-cloudflared`** — 對應：**reply-path**，不是任何一列業務出向
```yaml
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: <_cloudflared_namespace() 的值>
      # 刻意不寫 ports：reply 封包的目標 port 是 cloudflared 側的 ephemeral port
```
> 🔴 **這條是 §6 F-1 的解藥**，直接鏡射既有 `allow-cloudflared` ingress 條的 namespaceSelector。`cf_ns` 為空時（dev/local）整條不渲染，與 ingress 側行為一致。

**E-4　`paas-baseline-egress-allow-apiserver`** — 對應：#13 open-design console、#14 hermes-agent console（＋可能的 pvc-cleanup Job，未查證）
```yaml
  egress:
    - to:
        - ipBlock: {cidr: 10.43.0.1/32}       # kubernetes Service ClusterIP
        - ipBlock: {cidr: 192.168.10.21/32}   # ← 這三個是 post-DNAT 後的真實目標
        - ipBlock: {cidr: 192.168.10.22/32}
        - ipBlock: {cidr: 192.168.10.23/32}
      ports:
        - {protocol: TCP, port: 443}
        - {protocol: TCP, port: 6443}
```
> 🔴 **值不可寫死進 code**（cluster-specific，#404 已立此規矩：「cluster-specific node /32 不進 code/git seed，由 ops 設參數」）。**供給鏈是 D-1 待拍板**。
> 📌 node IP 落在 `192.168.0.0/16`，會被 E-5 的 `except` 擋掉 ⇒ E-4 必須是**獨立的一條**（NP 是 allow-union，E-4 的 allow 不受 E-5 的 except 影響）。

**E-5　`paas-baseline-egress-allow-external`** — 對應：#2 n8n、#5-11 租戶自填 connection、#13 Anthropic、#14 LLM＋registry＋MCP OAuth、#15 HAOS、#16 ha-full HACS
```yaml
  egress:
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8        # 含 pod CIDR 10.42/16 與 svc CIDR 10.43/16
              - 172.16.0.0/12
              - 192.168.0.0/16    # 含 node 私網 192.168.10.0/24
              - 169.254.0.0/16    # link-local / cloud metadata（本叢集無 IMDS，保留與既有一致）
              - 100.64.0.0/10     # CGNAT
              - 127.0.0.0/8
      # 刻意不寫 ports —— 見下方說明
```
> 這個形狀**不是紙上設計**：prod 現場的 `agent-pa-*-allow` 已經在跑同一組 except 六段（`docs/tracking/landings/prod-odoo-paas-not-installed.md:21`），且有正向證據（MiniMax 解析到的 public CF IP 不落在 except 內 ⇒ 通）。可照抄的完整 YAML 在 `docs/plans/2026-Q2/agent-runtime-pod-design.md:3499-3516`。
>
> ⚠ **與既有 agent-runtime 版本的唯一差異：本設計不限 ports。** agent-runtime 限 `[443, 80]`，但租戶 template 有明確的非 443/80 需求：`mcp-k8s` 連外部叢集 apiserver（常見 6443）、`odoo` 的 SMTP（587）、`n8n` workflow 可連任意 port。限 443/80 的失效形態是「租戶填了正確設定卻連不上，且錯誤訊息是 connection refused」—— 正是本案最怕的形狀。**若要限 port，必須先拍板放棄哪些 use case。**

### 5.3 規則 ↔ 盤點表對照（每條規則都要對得回去）

| 規則 | 對應盤點列 | 漏了會壞什麼 |
| --- | --- | --- |
| E-1 DNS | 全部 | 整個 ns 所有 pod 名稱解析全掛 |
| E-2 same-ns | #4、#5-11(opendesign)、W-3、W-6 | 租戶 Odoo 開通即失敗（psycopg2 在啟動期 exit 1）；HAOS 網頁入口斷（xff-proxy 打不到 VM） |
| E-3 cloudflared | reply-path（非業務） | **租戶服務的公開網址全部 502**（見 F-1） |
| E-4 apiserver | #13、#14、（W-9?） | open-design／hermes-agent 的 console 開箱即壞（#404 的原始症狀） |
| E-5 外網 | #2、#5-11、#13、#14、#15、#16 | n8n 全部 workflow 紅、HACS 裝不了、LLM 全斷、`npm i -g` 全掛 |
| （無規則） | #1、#3（is_active=False，seed 無出向） | — |

---

## 6. 失效模式清單

> 排序依「症狀有多不像根因」——越往下越容易誤診。

### F-1　🔴 加 egress 會打斷**已經寫對的 ingress**（K3s reply-path quirk）

**機制**：K8s NetworkPolicy 理論上是 stateful、conntrack 應自動放行 established 連線的 reply，**但 K3s 內建 enforcer 對 return path 有 quirk，會把 reply 當新的 egress 重新檢查**。當 `ipBlock 0.0.0.0/0 except 10.0.0.0/8` 存在，reply 的目標（對方 pod IP，落在 `10.42.0.0/16`）被 except 擋掉 ⇒ SYN-ACK 被 REJECT。

**已發生過**：stg 的 `odoo-dev-sandbox` chart 就是這樣讓 `stg.woowtech.io` **外部完全無法訪問**。修法是「把 ingress allowlist mirror 到 egress section」（chart `network-policy.yaml` 增加 11 行）。

**租戶端症狀**：服務 pod `Running`、readiness 綠、log 乾淨，但**公開網址回 502**。從叢集內打 pod IP 通、從外面打不通。0–90ms 的 connection refused（不是 timeout）。

**本設計的對應**：**E-3**。任何未來新增的 ingress allow 條，都必須同步問「reply 走得回去嗎」。

**出處**：`docs/archived/tracking/stg-sandbox-networkpolicy-cross-ns-block.md:59-83, 121-122`（含逐字修法與三步驗證）；同一 quirk 在 `docs/archived/plans/2026/2026-05-06-pkce-oauth-migration.md:278, 285-297` 被獨立記錄第二次；`docs/tracking/pitfalls/agent-runtime-stg-enablement-gap-chain.md:26` 是第三次（症狀 `[Errno 111] Connect refused 10.43.x:8000`）。

### F-2　🔴 `allow-same-ns` 只有 ingress ⇒ 租戶 Odoo 開通即失敗

**機制**：現行四條 baseline NP 的 `policyTypes` 由單一 helper 寫死成 `["Ingress"]`（`helm.py:1352`）。只加 E-0 而忘了 E-2，同 ns 的 app→DB 沒有任何 egress allow，且 pod CIDR 被 E-5 的 except 擋掉。

**租戶端症狀**：Odoo template 部署後 pod **CrashLoopBackOff**，log 裡是 `fix-admin-credentials.sh` 的 psycopg2 連線錯誤（`set -eo pipefail` ⇒ exit 1）。這條在**容器啟動期**跑，比主程序更早 ⇒ 看起來像「image 壞了」。HAOS 則是網頁 502（xff-proxy 打不到 `-vm` Service）。

**本設計的對應**：**E-2**。

### F-3　🔴 port 寫成 Service port ⇒ 該條規則等於不存在

**機制**：NP 比對是 post-DNAT，port 也一樣。prod 平台 Odoo 的 svc port 是 **80**、operator 是 **80**（容器 8000）。照 svc port 寫 NP 會全擋。

**租戶端症狀**：規則看起來完全正確（`kubectl get netpol -o yaml` 讀起來合理），流量卻全被拒。

**本設計的對應**：E-1（53 是容器埠，安全）、E-4（6443 是 apiserver 真實埠）、E-5（不限 port）都已避開。**未來若為 E-2 或平台 ns 腿加 ports，必須查容器埠不是 svc port。**

### F-4　🔴 chart 的 allow-union 讓整個設計「無效」或「致命」，而兩者外觀相同

**機制**：K8s NP 是 allow-union。同一顆 pod 被多條 policy 選中時，effective allow ＝ 所有 policy 的 egress rules 聯集。
- chart 若已有 `allowAllEgress: true`（`ha-full` 確定是、`haos` 確定有 NP）⇒ ns 級 deny 對它**完全無效**，安全性零進步，但**看起來一切正常**（服務照跑）。
- chart 若完全沒有 egress NP（`n8n` 這類第三方 chart 必然沒有）⇒ E-0 一落地，該 pod 立刻 default-deny，只剩 baseline 給的那幾條。

**為什麼難診斷**：「規則生效了但沒用」與「規則生效且正確」在監控上是同一個畫面。要區分只能對**每一支 chart** 實際 `kubectl get netpol -n <ns>` 看 union 後的結果。

**本設計的對應**：無法在本 repo 解決 → §10 的五道跨 repo 提問，其中前三道（mcp-server / open-design＋hermes-agent / ha-paas）**必須有答案才能 rollout**。

### F-5　DNS 漏 ⇒ 整個 ns 全掛，但症狀是「每個服務各自壞掉」

**租戶端症狀**：所有服務同時開始報各種與網路無關的錯（`getaddrinfo failed`、`could not translate host name`、`ENOTFOUND`）。因為每個 app 的 DNS 失敗訊息長得都不一樣，第一時間不會被歸成同一件事。

**本設計的對應**：**E-1**。#279/#280 spec 早就把這條列成「做 egress baseline 之前必須先補齊的東西」（`docs/plans/2026-Q3/2026-07-04-netpol-tenant-isolation-280-279-operator-spec.md:79, 103`）。

### F-6　apiserver 只放 ClusterIP ⇒ console 開箱即壞（#404 原始症狀重演）

**機制**：post-DNAT ⇒ 連 `10.43.0.1:443` 的封包目標變成 control-plane node IP:6443。只放 `10.43.0.1/32` 會被靜默 deny。

**租戶端症狀**：open-design／hermes-agent 的**網頁終端開得起來但連不上 daemon**（`connect.sh` 的 kubectl 回錯）。服務本體正常，只有 console 這個功能壞。

**本設計的對應**：**E-4** 同時放 ClusterIP ＋ 三顆 node /32。

### F-7　⚠ ns 級 allow-external 會把 agent-runtime 刻意關閉的 `externalEgress` 打開

**機制**：`agent_runtime_external_egress_enabled` 預設 `False`（`config.py:50-54`，註解逐字「default OFF (closed egress = secure)」）。若 **D-2 決定把 agent ns 也納入 baseline**，E-5 會 union 到 agent pod 上 ⇒ 那個 kill switch 失效。

**為什麼難發現**：這是**安全回歸**，不是功能故障 —— 沒有任何錯誤訊息，agent pod 反而變得「更能連」。

**本設計的對應**：D-2 若選「納入」，E-5 對 agent ns 必須**不渲染**（或 podSelector 用 `matchExpressions` 排除 agent-runtime label）。⚠ 註：CS ns 與 agent ns 是**不同的 namespace**，只套在 CS ns 上時不會有這個問題。

### F-8　CDI importer pod 裸露在 ns 級 deny 之下，chart NP 救不了它

**機制**：`importer-*` 由 CDI controller 生成、不帶 chart 的 selectorLabels ⇒ chart NP 選不到它，但 ns 級 `podSelector: {}` 選得到。

**租戶端症狀**：新開通的 HAOS **卡在 DataVolume `ImportInProgress`**，VM 永遠不啟動，原因埋在 importer pod log 裡、**不指向根因**（seed 註解自己就這樣寫，`cloud_app_templates.xml:1209-1210`）。

**目前判斷**：`pullMethod=node` 下推定 importer 不需要對外出向（image 由 node containerd 拉），但**是否連 DNS 都不需要，未查證** → §10 提問四。

### F-9　用「起 pod 即 curl」驗證 ⇒ 系統性讀到假的「egress 沒 enforce」

**機制**：kube-router 對新建 pod 裝 per-pod firewall chain 有 ≈2s 延遲。

**症狀**：驗證通過（探針說「還是通的」），實際上規則已生效 —— 或反過來。

**本設計的對應**：§8 驗證計畫全程用 established pod。⚠ repo 內**有 5 處反例範本**（`docs/guides/deployment/troubleshooting.md:71-72` 等，`kubectl run curl-test --rm -it ... -- curl`），照抄會系統性讀到假訊號。

### F-10　🔴 回滾刪掉的 NP 會被下一次部署**靜默復原**

**機制**：`_apply_baseline_netpol` 由 `create_namespace` 呼叫（`helm.py:1266-1268`），而 Odoo **每次 CloudService 部署都會呼叫 `create_namespace`**（`cloud_service.py:1345-1352`），且它是純 `kubectl apply` 冪等。⇒ 人工 `kubectl delete netpol` 在該 workspace 下一次開通新服務時被重新 apply 回來。

**症狀**：回滾當下有效、幾小時後故障復發，且**沒有任何人做過部署以外的動作**。

**本設計的對應**：§7.3 的回滾必須**同時**關掉開關來源（不能只刪物件）。這也是 D-4 選 ns label 的主因 —— label 是 `create_namespace` 讀得到的狀態，刪 NP ＋ 改 label 才是完整回滾。
> 📌 對比：#427 的控制面 NP 走獨立 raw-manifest Fleet bundle，實測 `correctDrift` 未啟用 ⇒ 刪除**不會**被 self-heal（`docs/decisions/427-operator-ingress-baseline.md:11,17`）。**本案的 operator-applied NP 行為與它相反**，回滾 runbook 不能照抄 #427 的。

### F-11　env label key 是裸字串、重複 4 次

`"paas.woowtech.io/env"` 沒有模組級常數，在 `helm.py:428, 429, 449, 2253` 手寫四次（相對地 #279 的三個 label key 都有常數，`helm.py:377-384`）。新增 selector 時打錯一個字 ＝ 靜默 match 不到 ＝ 那條 allow 等於不存在。**本設計的 egress 腿都不用這個 key（走 metadata.name / ipBlock），但這是新增規則時的已知 drift 風險。**

### F-12　apiserver CIDR 有兩道「空值即靜默 no-op」的閘

`if apiserver_cidrs:`（呼叫端 `cloud_service.py:1519-1523`）＋ `if not paths or not cidrs: return values`（注入端 `:1913`）。參數被清空 ⇒ values 完全不出現 `networkPolicy` 鍵、chart 退回自己的預設（只有 ClusterIP）、**沒有任何錯誤**。D-1 若沿用同一形狀，必須決定「參數缺值時是退回內建預設，還是拒絕套用 E-4」——#404 的教訓是前者（`docs/tracking/landings/404-apiserver-cidrs-landing.md`）。

### F-13　injector 家族只在 install-time 跑，既存 release 不會補

`action_redeploy()` 走 `reuse_values` 不重跑 injector ⇒ 既存服務要把值顯式寫進 `svc.helm_values` 再 redeploy（`docs/tracking/landings/404-apiserver-cidrs-landing.md` 的「殘留 / 待辦」段）。若 D-1 選「沿用 Odoo injector 路徑」，**存量租戶不會自動獲得新規則**。

---

## 7. 預設關閉 ＋ 逐 ns 開啟 ＋ 單 ns 快速回滾

### 7.0 命名決定（避開超字串陷阱）

新規則一律用 **`paas-baseline-egress-` 前綴**，不是在既有名字後面加 `-egress` 後綴。理由：`paas-baseline-allow-same-ns-egress` 會被 `grep paas-baseline-allow-same-ns` 一起命中（`grep -w` 也救不了 —— `-` 不是 word 字元），回滾腳本用名字過濾時會**把 ingress 那條一起刪掉**。前綴式命名與現行四條（`paas-baseline-default-deny-ingress`、`paas-baseline-allow-same-ns`、`paas-baseline-allow-cloudflared`、`paas-baseline-allow-mcp-reach`）沒有任何超字串關係。

再加一道**不依賴名字**的選擇器：所有 egress 條額外帶 label `paas.woowtech.io/np-direction: egress`。回滾用 `-l` 選，不用名字比對。
> ⚠ 不能用 `paas-managed=true` 當回滾選擇器 —— 它同時貼在 Namespace / ResourceQuota / LimitRange / Secret，以及**現行四條 ingress NP** 上（`agent_runtimes.py:126-127,139-140,331-332`；`helm.py:1358-1362`）。

### 7.1 既有零件盤點（能沿用什麼）

| 零件 | 現況 | 本案用途 |
| --- | --- | --- |
| operator feature flag 慣例 | 3 個 `bool = False`（`cloudflare_enabled`、`agent_runtime_external_egress_enabled`、`agent_runtime_skill_runner_enabled`）＋ 1 個「空字串＝不轉發」的 `agent_runtime_probe_style` | Layer 0 總開關照抄 |
| settings 載入 | pydantic-settings `BaseSettings`，`case_sensitive=False`、無 `env_prefix` ⇒ env var ＝欄位名全大寫；**import-time 全域單例**（`config.py:228`） | 🔴 **settings 無法表達「逐 ns」** —— 改值必須重啟 operator pod |
| operator Deployment env | **不由本 repo 任何 chart 渲染**，住在跨 repo `paas-app-bundle-cd` 的 `values-{prod,stg}.yaml` `config.*`（`config.py:74-77, 109-112`） | 新增 setting ＝ **三 repo lock-step**（operator code → charts 的 env 孔 → CD values） |
| 逐 ns 套用原語 | `POST /api/namespaces/{name}/baseline-netpol`，一次一個 ns、by name、必帶 `workspace_id`，宣稱冪等（`api/namespaces.py:86-104`） | Layer 1 的補件路徑，沿用（可能要加欄位，見 7.2） |
| ns 枚舉原語 | `list_managed_namespaces`（`kubectl get ns -l paas-managed=true[,paas.woowtech.io/env=<env>]`，`PAAS_ENV` 有設時自動 env-scoped，`helm.py:2251-2256`） | rollout 波次的候選清單來源 |
| label 收斂原語 | `reconcile_ns_reach_labels`（**一條原子 kubectl label 同時 add+delete**，冪等、無變更不下指令，`helm.py:2416-2422`）；`stamp_managed_ns_labels` 則刻意 add-only | Layer 1 的 label 寫入照抄前者的形狀（後者做不到「關掉」） |
| 刪除 NP 的程式路徑 | **不存在** —— operator 全 src 只有兩個 `kubectl delete`（delete service / delete namespace），namespaces router 無 DELETE 端點 | 🔴 回滾目前**只能人工 kubectl**，且會被 F-10 復原 |

### 7.2 三層開關設計

```
Layer 0（環境級 kill switch）   settings.baseline_egress_enabled: bool = False
                                 └ False ⇒ 完全不渲染任何 egress 條（今天的行為，零風險）
Layer 1（逐 ns 啟用）            ns label  paas.woowtech.io/baseline-egress ∈ {absent | "off" | "enforce"}
                                 └ 只有 "enforce" 才渲染 egress 條
Layer 2（新建 ns 的預設值）      settings.baseline_egress_default_mode: str = "off"
                                 └ create_namespace 建新 ns 時把這個值寫進 Layer 1 的 label
```

**判定式**：`render_egress = settings.baseline_egress_enabled and ns_label(...) == "enforce"`

**為什麼 Layer 1 必須是 ns label（而不是 operator setting 或 Odoo 參數）**：
- operator setting 是 import-time 單例、粒度是「整個環境」，做不到「只開一個 ns」，改值要重啟 pod（`config.py:228`）。
- Odoo `ir.config_parameter` **沒有 per-workspace / per-namespace 的既存範例**（只有兩個 per-slug：`global_instance_cap.<slug>`、`sales_paused.<slug>`，`cloud_app_template.py:429-439`），且 Odoo 側的值傳不到 `create_namespace` 的重跑路徑。
- **ns label 是叢集上的持久狀態，`_apply_baseline_netpol` 每次被呼叫都讀得到** —— 這是唯一能同時解決「逐 ns」與 **F-10 靜默復原**的載體：把 label 改成 `off` 之後，下一次部署的 re-apply 會**自己不渲染** egress 條，而不是把它裝回來。

**實作影響**：`_apply_baseline_netpol` 目前完全不讀 ns 現況。要多一次 `kubectl get ns <ns> -o jsonpath={.metadata.labels...}`。讀失敗時**必須 fail-closed 成「不渲染 egress」**（讀不到 label ≠ 該 enforce）。

### 7.3 單 ns 快速回滾（兩步，缺一不可）

```bash
NS=paas-ws-stg-xxxxxxxx
# ⚠ 兩步都要做。只做第 2 步 ⇒ 下一次該 workspace 開通新服務時 NP 被 re-apply 回來（F-10）。

# 1) 先關掉來源（讓 re-apply 不再渲染）
kubectl --context woow-k3s -n "$NS" label ns "$NS" \
  paas.woowtech.io/baseline-egress=off --overwrite

# 2) 再刪掉現存物件（用 label 選，不用名字比對）
kubectl --context woow-k3s -n "$NS" delete networkpolicy \
  -l paas.woowtech.io/np-direction=egress
```
> 預期恢復時間：NP 刪除對 established pod 立即生效（無編程空窗 —— 空窗只發生在**新建** pod 側）。
> 全環境回滾＝把 Layer 0 翻回 `false` 並重啟 operator（CD values 改 → Fleet sync），再對受影響 ns 跑上面第 2 步。

---

## 8. 驗證計畫

### 8.1 三層，各驗不同的東西

| 層 | 驗什麼 | 現況 |
| --- | --- | --- |
| L-a 單元（operator pytest） | 送進 `apply_manifest` 的 dict 形狀正確 | 已有 `extra/paas-operator/tests/test_baseline_netpol.py`（純 manifest 斷言、mock 掉 apply）。**加 Egress 會讓至少 4 處變紅**：NP 名稱清單、`policyTypes == ["Ingress"]`（:78）、dev 情境 `len(m)==3`（:140）、docstring 自述的 ingress-only。🔴 **這些紅燈是既有的陽性對照，不要為了綠而放寬它們** —— 要改成「ingress 四條 policyTypes 仍是 `["Ingress"]`」＋「egress 條 policyTypes 是 `["Egress"]`」兩組獨立斷言 |
| L-b 真 K8s integration | 規則真的被 enforcer 執行 | 🔴 **目前對 NetworkPolicy 零覆蓋**（4 種 pattern 全零；同目錄 `namespace` 有 256 命中作為陽性對照，證明探針沒壞）。且 `tests/integration/conftest.py:127-140` 用 raw `kubectl create namespace` 建 `paas-ws-test-op-<uuid8>`，**完全繞過 `create_namespace()`** ⇒ 那些 ns 上沒有任何 baseline NP。要在這層驗，得先讓 fixture 走真正的 `create_namespace` |
| L-c dev sandbox e2e | 真實租戶 ns 上的端到端行為 | 沙盒只建 `dev-sandbox-{slug}` 一個 ns；租戶 `paas-ws-*` ns 是**沙盒裡的 Odoo 呼叫叢集內既有 operator** 建出來的（`.sandbox/post-create.sh:28,32-46`）。已有前例證明這條路走得通（`docs/tracking/landings/ha-paas-provision-audit-20260713.md:47,52,67`），但那顆租戶 ns 是**共用叢集資源、無 TTL**，測完要手動清 |

### 8.2 核心驗收：n8n pod → 平台 Odoo ClusterIP（含陽性對照）

**為什麼挑這條**：它就是 #514 issue 表格裡「#427 後仍 OPEN」那一格，且 n8n 是**零 chart NP 的第三方 chart** —— 只有 ns 級規則涵蓋得到它。用自家 chart 驗會分不清「chart 修好了」還是「baseline 修好了」（這正是 #427 驗收方法學的教訓，`docs/decisions/427-operator-ingress-baseline.md:19`）。

**探針紀律**（兩個已實證的地雷）：
- 🔴 **一律用 established pod**：`run` → `wait Ready` → **`sleep 45`** → `exec`。kube-router 對新建 pod 的 egress-deny 編程 ≈2s 才裝上，「起 pod 即 curl」會系統性讀到假的「還是通的」（`docs/tracking/pitfalls/netpol-enforced-except-fresh-pod-startup-window-k3s-kube-router.md:21,138-144`）。
- 🔴 **不用 `/dev/tcp`**：那是 bash-ism，很多容器的 `/bin/sh` 是 dash ⇒ **靜默假失敗**（#425 驗收時差點誤判斷線，`docs/tracking/landings/425-pg-netpol-hotfix.md`）。一律 `python3 -c "import socket; ..."`。
- 🔴 **`woow-test.sh` 在成功與失敗兩條路徑最後都會 `kubectl rollout restart` odoo**（`.claude/skills/k8s-dev-sandbox/scripts/woow-test.sh:193-196,225-228`）⇒ 每跑一次就把 Odoo pod 打回 fresh 狀態，**破壞 established-pod 前提**。NP 實驗期間不要跑它。

**探針腳本**（照抄用）：
```bash
CTX=--context woow-k3s
NS=<租戶 ns>                     # paas-ws-stg-xxxxxxxx
TARGET=<平台 Odoo ClusterIP>      # 由 kubectl -n paas-system get svc paas-odoo -o jsonpath 取得
PORT=<容器埠，不是 svc port>       # ⚠ post-DNAT，見 F-3

# 用「已存在的 n8n pod」當探針，天然是 established（不必另建 probe pod）
POD=$(kubectl $CTX -n "$NS" get pod -l app.kubernetes.io/name=n8n -o name | head -1)
kubectl $CTX -n "$NS" exec "$POD" -- python3 -c "
import socket,sys
s=socket.socket(); s.settimeout(6)
try:
    s.connect(('$TARGET', $PORT)); print('CONNECTED')
except Exception as e:
    print('BLOCKED:', type(e).__name__, e)
"
```
> ⚠ n8n image 內不一定有 `python3`。若無，改用 **另建的 established probe pod**（`kubectl run probe --image=curlimages/curl --command -- sleep 3600` → `wait Ready` → **`sleep 45`** → exec），並在報告中註明「探針是 probe pod 不是 n8n 本體」—— 兩者的 chart NP 覆蓋可能不同，這個差異必須寫出來而不是忽略。

**判定表**（🔴 **必須有第三格**：只有「通過／不通過」兩格會把「探針壞了」塞進通過）

| 觀測 | 判定 |
| --- | --- |
| 規則生效時 `BLOCKED: ... Connection refused`（1–2s 快失敗） | ✅ 符合預期（k3s enforcer 用 REJECT，見 §4） |
| 規則生效時 `BLOCKED: timeout`（6s 逾時） | ⚠ **不該出現** —— 本叢集 NP-block 回 RST 不是 drop。是路由／服務不存在／探針打錯位址，**不是** NP 生效的證據 |
| 規則關掉時 `CONNECTED` | ✅ **陽性對照成立**（證明探針本身有鑑別力） |
| 規則關掉時仍 `BLOCKED` | 🔴 **探針壞了或標的錯** —— 此時「規則生效時 BLOCKED」那格**不構成任何證據**，整組作廢重來 |
| 規則生效時 `CONNECTED` | ❌ 規則未生效（或被 chart 的 allow-union 蓋過，見 F-4） |

**陽性對照的操作**：用 §7.3 的兩步回滾把規則關掉、重測、再用 backfill endpoint 裝回來。⚠ operator **沒有移除端點**（`api/namespaces.py` 只有 create / quota / baseline-netpol，無 DELETE）⇒ 關掉這一步只能人工 kubectl。

### 8.3 必須同時驗的「不能壞」清單（負向驗收）

每一條都對應 §6 的一個失效模式。**只驗「n8n 打不到 odoo」而不驗這些，等於只驗了一半。**

| # | 驗什麼 | 怎麼驗 | 對應 |
| --- | --- | --- | --- |
| N-1 | 租戶服務的**公開網址**仍 200 | 從叢集外 `curl -o /dev/null -w '%{http_code}'` 該服務的 CF 網址，連續 3 次 | F-1（最容易被漏，因為它是 ingress 方向的症狀） |
| N-2 | 租戶 Odoo 能連自己的 DB | 部署一個**全新**的 odoo template，看它能不能起來（不是看既有的） | F-2（症狀在啟動期，既有 pod 看不出來） |
| N-3 | DNS 解析正常 | established pod 內 `python3 -c "import socket;print(socket.gethostbyname('kubernetes.default.svc.cluster.local'))"` | F-5 |
| N-4 | console 能 exec 進 daemon | open-design 服務的網頁終端實際敲一個指令 | F-6 |
| N-5 | 外網通 | established pod 內連一個**公網** host:443 | E-5 |
| N-6 | agent pod 的 `externalEgress` 仍是關的（若 D-2 選納入 agent ns） | 從 agent pod 打公網，**預期 BLOCKED** | F-7（這格預期是「不通」，別把它寫成成功條件的反面） |
| N-7 | 新開通 HAOS 的 DataVolume 能 `Succeeded` | 開一台新 HAOS，看 DataVolume 狀態 | F-8 |

### 8.4 環境注意事項

- **kube context 值域白名單**：`.sandbox.yml:30-33` —— local 只准 `docker-desktop`、stg 只准 `woow-k3s`、ci-local 兩者皆可。逃生門 `SANDBOX_ALLOW_ANY_KUBE_CONTEXT=1`。
- **本 repo 有 PreToolUse hook `kubectl-ns-guard.py`**：`--context woow-k3s` 下的寫入／exec 類 kubectl 必須帶 `-n` 且 ns 在白名單；**`default` ns 不在白名單** ⇒ pitfall 文件裡「從無關 `default` ns 起臨時 pod」的重現法在本 repo 會被擋下，要改在 `dev-sandbox-*` 或該租戶 ns 內起 probe。
- **docker-desktop 的 NP enforcement 本 repo 無任何記載**（§4）。在它上面得到的綠燈**不可外推到 woow-k3s**；反之亦然。
- **驗證環境的碼是不是你改的那份**：`create.sh` 已移到 woow-dev plugin，本 worktree 內查不到相關 gate 或 hostPath 凍結副本的記錄（`docs/guides/getting-started.md:221` 反而寫「hostPath 即時反映」）。**列入 §11 未查證**。

---

## 9. 待拍板決策

### 9.1 D-1：apiserver CIDR 的供給鏈怎麼接到 operator

**問題**：E-4 需要 cluster-specific 的 control-plane node IP。今天這個值**只存在 Odoo 側**（`ir.config_parameter woow_paas_platform.apiserver_cidrs`，`cloud_service.py:1866-1882`），而且它走的是「Odoo → helm values → **chart** 渲染 NP」這條路。**baseline NP 不是 chart 產物，是 operator 直接 `kubectl apply` 的** ⇒ 現有供給鏈結構上接不上。operator 端**今天沒有任何讀 apiserver CIDR 的程式碼**（`extra/paas-operator/src/` 全樹確認）。

| 選項 | 做法 | 代價 |
| --- | --- | --- |
| **A. Odoo 經 API 傳** | `create_namespace` 與 backfill endpoint 各加一個 `apiserver_cidrs: list[str]` 欄位，Odoo 呼叫時帶上 | 值的權威留在 Odoo（與 #404 一致）。但 `create_namespace` 有**非 Odoo 的呼叫者嗎**未查證；且 F-13 的「既存 release 不重跑」問題會複製過來 |
| **B. operator 新增 setting** | `baseline_egress_apiserver_cidrs: str = ""`（逗號分隔），走既有 CD values（`config.<camelCase>`）注入 | 三 repo lock-step（operator code → charts env 孔 → CD values）。值在兩個地方各存一份（Odoo 一份給 chart、operator 一份給 baseline）⇒ **會漂移**，且漂移是靜默的 |
| **C. operator 自己從叢集發現** | 每次 apply 前 `kubectl -n default get endpoints kubernetes -o jsonpath` 讀出真實 endpoint IP | 需要 operator RBAC 有 `default` ns 的 `endpoints get`（跨 repo 改 clusterrole）；每次 apply 多一次 API call。**但值永遠正確、加 node 不會靜默失效** |
| **D. 不做** | baseline 不蓋 apiserver，維持由 chart 的 `networkPolicy.apiServerCidrs` 管 | 只對 open-design／hermes-agent 有效（path_map 只有這兩個 slug）。E-0 一落地，**任何其他需要 apiserver 的 workload 立刻壞**（例如 W-9 的 pvc-cleanup Job，若它在租戶 ns） |

**建議：C 為主 ＋ B 為 override**（`setting 非空時用 setting，否則自動發現；兩者都拿不到 ⇒ fail-closed 不渲染 E-0`）。

理由：
1. `docs/reference/features/opendesign-console.md:145` 記載那三個 IP 的**取得方式就是** `kubectl -n default get endpoints kubernetes` —— C 只是把 ops 手動做的那一步自動化。
2. #404 訂下「cluster-specific 值不進 code、由 ops 設參數」的規矩，前提是**chart 無法自我發現**。operator 有 kubectl，前提不成立。
3. repo 內只記載 **3 個** control-plane node IP，但叢集有 **4 個 node**（§4）—— 第 4 顆是不是 worker、apiserver 有沒有跑在上面，**本次未查證**。手動維護的清單漏一顆的失效形態是 F-6（console 間歇性壞，看落在哪顆 node），而自動發現天然免疫。
4. B-only 的兩份值漂移是靜默的，正是本案最該避免的形狀。

### 9.2 D-2：agent ns（`paas-ws-pa-*`）納不納入本案

**問題**：agent ns 的名字落在 `paas-ws-*` glob 內，但它**不走 `_apply_baseline_netpol`**（§1.2）。派工單的標的（ns glob）與落點（那個函式）指的不是同一個集合。

| 選項 | 做法 | 代價 |
| --- | --- | --- |
| **A. 不納入** | 只改 `_apply_baseline_netpol`，agent ns 完全不動 | 最小風險。但 agent pod 的 egress 完全靠 chart leg-1，而 leg-1 的 env qualifier **operator 從未 forward**（`agent_runtimes.py` 送給 chart 的 `networkPolicy.*` 只有 `egressWorkspaceIds` 與 `odooNamespace` 兩個鍵）⇒ 跨環境 workspace-id 撞號的洞在 agent 側**沒被關上**。這是既有問題，不是本案造成的 |
| **B. 納入，共用同一組規則** | `_ensure_namespace` 也呼叫 `_apply_baseline_netpol` | 🔴 觸發 **F-7**：E-5 union 到 agent pod ⇒ `externalEgress` kill switch 失效（安全回歸） |
| **C. 納入，但獨立開關 ＋ agent ns 不渲染 E-5** | `_ensure_namespace` 呼叫時帶一個 flag，跳過 E-5（外網那條） | 實作稍複雜；但同時拿到「agent ns 也有 default-deny egress」與「不打開 externalEgress」 |

**建議：C**，但**排在 CS ns rollout 完成之後**當獨立波次。理由：agent ns 的 blast radius 與 CS ns 不同（它壞掉的是 AI agent 功能，不是租戶付費服務），且 chart leg-1 已經給了它一組 egress 規則，union 行為需要單獨評估。**若要縮小本 PR 範圍，選 A 也可接受，但必須在 issue 上明寫「agent ns 未收斂」不是遺漏而是分波。**

### 9.3 D-3：「外網維持通」由 baseline 給，還是留給 per-chart

| 選項 | 代價 |
| --- | --- |
| **A. baseline 給（E-5）** | 簡單、涵蓋所有 chart。但本案的安全價值縮成「只擋內網」—— SSRF 打公網仍通 |
| **B. 不給，留 per-chart** | 這正是 #514 存在的理由（第三方 chart 不會做）。`n8n` / `odoo`（bitnami）會當場全斷 |
| **C. baseline 給，但拆成獨立可關的一條** | 多一個開關要維護 |

**建議：C。** issue 待辦 2 逐字要求「外網維持通行（多數 app 需要）」，且 `ha-full` 與 `haos` 兩份 seed／設計文件都明文要求 egress 全開（其中 HAOS 有 A/B 實測）。#514 的核心價值在 issue 表格裡寫得很清楚 —— 要堵的是 **prod odoo ClusterIP 仍 OPEN** 那一格，不是外網。把 E-5 拆成獨立物件（而非併進 E-0 的 except），未來要收緊 port 或改成 per-workload 時可以單獨動它。

### 9.4 D-4：逐 ns 開關住哪裡

論證見 §7.2。**建議：ns label `paas.woowtech.io/baseline-egress`**（唯一能同時解決「單 ns 開關」與「F-10 靜默復原」的載體）。

⚠ 這個 label key 是新增的第 6 個平台 label。**必須加模組級常數**，不要重蹈 `"paas.woowtech.io/env"` 被裸字串重複 4 次的覆轍（F-11）。

