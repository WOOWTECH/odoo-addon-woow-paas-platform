---
title: 「一個 workspace 一個 namespace」架構調整 — 偵察分析與 decision matrix
category: scout
status: draft
created: 2026-07-02
updated: 2026-07-02
meeting: 2026-07-01 上線前功能聚焦（議題 5「Namespace 合併」，決議 pending；會議記錄存於 repo 外 footage/meeting/）
related:
  - docs/plans/2026-Q2/2026-06-21-naming-env-token-prod-stg-plan.md
  - docs/plans/2026-Q2/2026-06-25-l4-reconciler-design.md
  - docs/plans/2026-Q2/agent-runtime-pod-design.md
  - docs/tracking/pitfalls/cf-ns-orphan-no-account-gc.md
  - docs/tracking/pitfalls/reaped-since-single-path-stamp-gc-immunity.md
  - docs/decisions/refactor/cloud-service-project-decouple.md
---

# 「一個 workspace 一個 namespace」架構調整 — 偵察分析與 decision matrix

> **性質：偵察（scout）文件，非決策。** 本文是動工前的衝擊分析與打分，尚未拍板。若日後成案，應 promote 成 `docs/plans/` 雙檔（design + implementation）；若拍板不做或做法定案，結論落 `docs/decisions/`。

## 0. TL;DR

**提案**（Elmo，2026-07-01 會議議題 5）：一個 PaaS workspace（裡面可以放 cloud service / Security Access / Smart Home）**只用一顆 k8s namespace**，該 workspace 的每個 cloud service type 都灌進這顆 ns；跨 workspace 的 ns 之間網路隔離、同 ns 內自然互通。

**核心發現 ①——marketplace 的 cloud services 其實「已經」這樣做了**：部署程式碼從第一版起就是 per-workspace 共用 ns（先是 `paas-ws-{workspace.id}`，2026-03-01 起改 `paas-ws-{md5(slug)[:8]}`，粒度從未變過）；live stg 上一顆 `paas-ws-stg-86f34bd4` 就同時住著 **odoo + n8n + 3 個 mcp-server（5 個 release、3 種 type）**。「每個 service 自己一顆 ns」的印象來自另外兩個地方：**agent runtime**（per-project 獨立 ns）與 **prod 手動搬遷存量**（`woowtech-odoo`、`woowtech-n8n` 每服務一顆，共約 29 顆、不在 operator 管轄內）——會議是在「per-service」的前提下討論這個提案的，該前提對 marketplace 路徑不成立（§2）。

**核心發現 ②——所以提案的真正工程 scope 比想像小、也跟想像不同**：
- marketplace cloud services：**零工程**（已是提案形態）；
- **agent runtime 是唯一要搬的**（per-project `paas-ws-pa-{hash}` → 併入 workspace ns），全部改動與 4 個硬 blocker（§6）都集中在這；
- Security Access / Smart Home：**完全沒有 k8s 資源**（純 Cloudflare tunnel、connector 在使用者硬體/家中），零影響；
- prod 手動存量：正解是**收編進 marketplace**（收編當下自然落入 workspace ns），不是在手動軌道內另做一次 ns 合併；
- 「跨 workspace 網路隔離」**現狀並不存在單一保證**（隔離三態混雜，§2.3-1）——這是提案的前提配套，也是兩案共同欠的地基。

13 維度 decision matrix：**現狀 40 vs 提案 40，總分打平**——但分數結構不對稱：提案贏在結構性維度（quota 可控、計費歸屬、擴增性、互通性），現狀贏在風險維度（blast radius、名稱衝突、K8s API 層安全、遷移成本）。打平意味著決策取決於配套是否入 scope、而非粒度本身；會議「pending、不擋 7 月底上線」的決議與本分析一致（Project 先隱藏 → agent per-project ns 成長凍結）。但 **§7 第 0 段的 6 項 P0 與粒度無關、不應跟著 pending**——其中「租戶隔離補鋪」與「PVC 誤標」屬上線前資安/資料安全 gap。

## 1. 名詞與提案定義

- **workspace**：PaaS 的租戶容器——一個 workspace 裡可以放多個 cloud service（odoo / n8n / mcp-server…各種 type）、Security Access、Smart Home，並可掛 project（agent runtime 的宿主）。
- **方案一（現狀）**：混合粒度——marketplace cloud services 共用 per-workspace ns；agent runtime 每個 project 一顆獨立 ns；SA / SmartHome 無 k8s 資源；prod 另有手動搬遷存量（per-service ns，§2.1）。
- **方案二（提案）**：每個 workspace 恰好一顆 ns，該 workspace 所有工作負載（所有 cloud service type + 所有 project 的 agent runtime pod）同 ns；跨 workspace deny、同 ns 內互通。**評分前提：「跨 workspace deny」的 NetworkPolicy 配套隨提案一起落地**（查核確認此前提若抽掉，方案二網路安全分數不成立）。

**會議語境**：Elmo 提案的資安自我說服理由是「同一個 workspace 代表使用者已知這幾個服務在同一區網 / 同一企業」；Eugene 的顧慮是會影響命名方式、效益不一定夠大、「拆開較安全」。會議決議 pending、優先度低、不擋 7 月底上線（待辦 #12）。本文即該評估的執行結果。

## 2. 現狀：四種 ns 粒度並存（2026-07-02 程式碼＋git 歷史＋live 三重驗證）

### 2.1 粒度盤點

| 工作負載 | 現狀 ns 粒度 | 證據 |
|---|---|---|
| **marketplace cloud services**（所有 type：odoo / n8n / mcp-server…） | **per-workspace 共用** `paas-ws-{md5(slug)[:8]}` | 程式碼唯一產生點 `src/controllers/paas.py:2047`（輸入只有 `workspace.slug`）；git 歷史 `31835ff`（2026-03-01）之前為 `paas-ws-{workspace.id}`，**粒度自始未變**；live：`helm list -n paas-ws-stg-86f34bd4` = `svc-06b55af1`(odoo-28.2.10) + `svc-5b78f97b`(n8n-2.0.1) + 3× mcp-server，5 release 同住一顆 ns。目前**只在 stg 活躍（prod 零 `paas-ws-*` CS ns）** |
| **agent runtime** | **per-project** `paas-ws-pa-{HMAC16}`，一 project 一 ns 一 pod | `extra/paas-operator/src/api/agent_runtimes.py:52-61`；live stg 11 顆 `paas-ws-stg-pa-*` |
| **prod 手動搬遷存量**（未走 marketplace） | **per-service** `{客戶}-{服務}`，同客戶多服務＝多顆 ns | live：`woowtech-odoo`+`woowtech-n8n`、`torchmedia`+`torchmedia-hermes`、5 顆 `{user}-hermes`…約 29 顆；kubectl/helm 直管、無統一 tenant label、**在 operator 的 `paas-ws-` prefix 守衛之外** |
| **Security Access / Smart Home** | **無 k8s ns**（每 instance 一條 dedicated CF tunnel；connector 跑在租戶自有硬體 / 使用者家中，流量不進 cluster） | `security_access.py` / `smart_home.py` 全檔零 k8s 呼叫；operator tunnels/access API 零 kubernetes 引用 |

### 2.2 「每服務自己一顆 ns」印象 vs 實作（本次分析的關鍵校正）

會議是在「目前每個 cloud service type 有自己的 ns」的前提下討論提案的。這個前提**對 prod 手動存量與 agent runtime 成立，對 marketplace 路徑不成立**——印象來源可考：

1. prod 上看得到的所有客戶服務（手動搬遷那批）確實每服務一顆 ns；
2. agent runtime 確實 per-project 一顆 ns；
3. 會議中「service build 時是不同 namespace」的語境是 Hermes MCP（cloud service，會落 `paas-ws-*`）↔ Hermes Agent 本體（在 `eugenechen-hermes`，手動軌道）——那兩個確實不同 ns，但推廣到「CS type 之間互相不同 ns」就與程式碼不符。

**含義**：提案對 marketplace CS 是「確認並守住現狀」，不是新工程；決策重心因此移到 agent runtime 併入與否（§4-§6）、以及 prod 存量的收編路徑（§7）。

### 2.3 兩個顛覆直覺的現狀事實

1. **租戶之間的網路隔離現狀是「三態混雜」，且平台層保證缺席（CS 側）**：operator `create_namespace` 只建 ns + ResourceQuota + registry secret（`extra/paas-operator/src/services/helm.py:737-811`），全 repo 無任何對 CS 租戶 ns 下 NetworkPolicy 的程式。Live 驗證三態：① marketplace CS ns 內存在 **chart 自帶** per-release NP，但語意等效放行（`svc-*-odoo` 的 ingress rule 只限 port、不限來源＝任何 ns 都能打 8069；egress allow-all）→ 跨租戶隔離實質缺席；② agent ns 有 per-instance 雙向 default-deny NP（chart 實檔驗證，§5-2），但 **11 顆中只有 6 顆有**（較舊 5 顆零 NP，chart 版本 drift）；③ prod 手動存量有 15 顆 ns 鋪了 `default-deny-external`（allow same-ns ingress / deny 跨 ns，約 2026-06-25 統一鋪設），**14 顆全裸**（全部 5 顆 hermes 系、`bnidistrict/changching/linyanshan/lisachuzhu/lyucijyun-odoo`、`woowtech-n8n`、`hermes`、`hermes-mcp-admin`、`woowhademo`）。**「跨 workspace deny」沒有單一保證來源、覆蓋有洞，是兩案共同欠的地基，與 ns 粒度無關。**
2. **agent→租戶服務互通現狀是 dead path**：NP 設計走 `namespaceSelector matchLabels woow.tw/workspace-id`（design.md:3486-3497），但 CS ns 從未被貼該 label（`build_managed_ns_labels` 只有 `paas-managed=true` + env，`helm.py:108-129`），且 `sync-workspaces` 端點在生產碼無 caller → project 改綁 cloud service 後 egress allowlist 不同步。**修法（貼 label + 接線）與粒度正交。**

### 2.4 其他關鍵事實

- `delete_namespace` 全 codebase 唯一生產 caller = agent teardown（`agent_runtimes.py:577-586`）；operator 連可刪 CS ns 的 API 端點都不存在（`namespaces.py` 僅 POST create）→ CS 孤兒 ns + quota + registry secret 永存、無 TTL。
- Quota：CS ns 有（`max(3×sum(allocated), 8CPU/8Gi/100Gi)`、`persistentvolumeclaims=10` 硬編碼、每次 deploy 重算但刪 service 不縮，`cloud_service.py:438-447` + `helm.py:770-787`）；agent ns **完全無 quota**（`_ensure_namespace` 只 apply label，`agent_runtimes.py:81-95`）；全 repo 無 LimitRange。**Live 佐證**：`paas-ws-stg-86f34bd4` PVC 已用 6/10（5 個 release）——PVC 上限分食是現在進行式；`paas-ws-stg-ad023482` 用量全零但殘留 12CPU quota ＝「服務刪光後 ns + quota 永存」的實例。
- project↔workspace 是 **M:N**（`project.workspace_ids` 是從 `cloud_service_ids` 算出的 computed M2M，`project_project.py:36-49`），且 unbound project 合法（decouple invariant D1）→「per-workspace ns」對跨 ws / unbound project 沒有唯一落點，是提案的 design gap。
- ns 上無任何 workspace/owner label；`ws_hash` = md5 單向、agent hash = HMAC 刻意反枚舉。但 agent ns 的 workspace ids 其實已持久化在 k8s 端（helm release-state Secret，`helm get values` 可還原）。
- Odoo 端 `ir.config_parameter namespace_prefix` 與 operator env `NAMESPACE_PREFIX` 是**兩個獨立配置源、無共同來源**——改 ns 模型時的 drift 風險點（live 證實 stg 兩端已同步為 `paas-ws-stg-`）。
- Cloudflare 命名與 ns 的耦合只有一處：CS 的 MCP subdomain 公式吃 ns 名（`generate_subdomain` = `{release}-{ns 去 paas-ws-}`，`cloudflare.py:804-816`；Odoo 端 `reconstruct_mcp_subdomain` 跨 repo byte-identical，`src/services/naming.py:61-80`）——**維持 `paas-ws-{md5(slug)[:8]}` scheme 不動就沒有命名連鎖**。docs 內既有「統一命名」分析（naming-env-token plan、L4 reconciler）全部是 prod/stg env-token 軸，per-workspace ns 粒度是新題。

## 3. 各子系統衝擊（若把 agent runtime 併入 workspace ns）

| 子系統 | 提案衝擊 |
|---|---|
| Cloud Service | **零工程**（已是提案形態）。被動衝擊：quota 被 agent 分食、PVC 誤標面擴大、可能被 agent teardown 整鍋端（§6） |
| Agent Runtime | **全部改動集中在這**：3 處 ns 推導 lock-step（`agent_runtimes.py:61`、`mcp_invalidator.py:18-22`、`skill_invalidator.py:22-26`）、API contract（reap/resume/status/invalidate/delete 全以 project_hash 單鍵導 ns）、cross-repo chart、DB migration（stored `helm_namespace`/`pod_ws_url` 全 stale）、teardown 重寫 |
| Security Access | 幾乎零。唯一守線：`canonical_name` 刻意不從 `namespace_prefix` 推導（`security_access.py:207-213`，identity-anchor 設計保證），ns 改制不得反向引入耦合 |
| Smart Home | 零。守線：「刪 ns ≠ teardown 租戶」——CF 資源不在 ns 裡，必須仍走 Odoo unlink guard（否則重演 [cf-ns-orphan pitfall](../tracking/pitfalls/cf-ns-orphan-no-account-gc.md)） |
| Cloudflare 整合 | agent runtime 零 CF 資源 → 併入本身零影響。風險僅在「順手改 ns 命名 scheme」→ MCP subdomain 跨 repo 契約連鎖（§2.4 末條）。**別動 `paas-ws-{md5(slug)[:8]}` 就安全** |

## 4. Decision Matrix（1–5 分，5 最好；查核校正後）

> Scope＝marketplace 路徑（方案一/二定義見 §1）。prod 手動存量不進 matrix：它的「合併」正解是收編進 marketplace（§7），屆時自動落入方案二形態。

| # | 維度 | 現狀 | 提案 | 關鍵理由 |
|---|---|:---:|:---:|---|
| 1 | 租戶網路安全性 | 2 | 3 | 現狀隔離三態混雜、平台層保證缺席（§2.3-1）；提案配套建 deny 後升級，但同 ns 把 agent 與 CS 攻擊面合併，抵銷部分紅利。**若只併 ns 不建 NP → 1 分** |
| 2 | 同 ws 服務互通性 | 2 | 4 | 現狀 agent→CS MCP egress 是 dead path＋綁定變更不同步（§2.3-2）；提案同 ns 用 podSelector 較簡單。但 agent egress default-deny 同 ns 也照擋——「同 ns 自然互通」對 agent **不成立**（§5-3），兩案都要動 NP。CS↔CS 互通已是現狀（同 ns 無有效阻擋） |
| 3 | 維護性（刪、改） | 3 | 3 | 現狀兩套模型雙寫、3 處 ns 推導副本、CS ns 永不刪；提案單一模型但失去「刪 ns 一鍋端」兜底。per-release GC 骨架已存在（CS `delete_release`），agent 真正缺的只是 `{release}-llm` Secret 刪除（§5-4） |
| 4 | 名稱/資源衝突 | 4 | 2 | **硬 blocker**：agent chart auth Secret 固定名 per-ns → 同 ns 第二個 project 的 agent `helm install` 直接撞 Helm ownership 失敗（fail-loud、裝不起來，§5-5）。PVC orphan 標記全 ns 一鍋端誤標面也擴大（現狀已是 bug） |
| 5 | Blast radius / 故障隔離 | 4 | 2 | `delete_runtime` 第二步刪整顆 ns（`agent_runtimes.py:577-586`）——漏改就是「刪一個 project = 清掉整個租戶」的保證事故；workspace ns 變單一故障域。另注意：per-project 物理隔離是 agent-runtime design 的 sign-off 條款，推翻需正式 decision doc |
| 6 | 擴增性（規模維運） | 3 | 4 | 現狀 ns 數 = O(ws) + O(近 30 天活躍 project)（reap 只 scale-to-0、GC 滯留 30 天）、兩套生命週期 runbook 雙寫；提案單一模型收斂 |
| 7 | K8s 平台限制 | 3 | 4 | 兩案都遠未觸頂（prod woow-k3s = 4-node HA embedded etcd）；提案 ns 更少、per-ns 固定物件（registry secret 等 ~3 件/ns）不重複。`--history-max` 未設是共同待修 |
| 8 | 硬體資源限制可控 | 2 | 4 | **現狀 agent ns 完全無 ResourceQuota = 每個 project 一個無上限逃逸口**；提案天然納入 workspace quota（quota 本來就按 workspace 總和訂）。但公式須納入 agent、補 LimitRange、spawn 加 quota 預檢，否則 quota 滿時 helm install 成功但 pod FailedCreate 靜默（撞 coldstart pitfall 觀測盲區） |
| 9 | 外部資源可溯源 | 3 | 3 | CF 命名/溯源與 ns 粒度**正交**：SA 全鏈 stamp `canonical_name`、CS/SM DNS 無 comment、CS 呼叫未傳 env token、L4 reconciler 只看 CF name 不看 ns——兩案同分。提案不修任何既有溯源缺口 |
| 10 | Odoo 消失時孤兒辨識 | 3 | 3 | agent ns 的 workspace ids 可由 helm release-state Secret 還原（§5-6），現狀差距被原分析高估；提案的 release 級殘留混在活 ns 裡反而更難認。實質改善要靠補 workspace-id label，兩案皆然 |
| 11 | 遷移成本/相容性 | 5 | 2 | 現狀零成本；提案是雙 repo + chart + DB migration + API contract 全鏈改動（好消息：100% 集中在 agent runtime）。可行路徑：new-only cutover（§7 第 2 段） |
| 12 | 計費/成本歸屬 | 2 | 4 | repo 零 metering（純 plan 預購制）；未來若做 per-ns 成本歸屬，「一 ns = 一租戶」天然對齊。M:N project 的成本分攤是 gap |
| 13 | K8s API 層安全邊界（Secret/SA/PSA）* | 4 | 2 | 明文 LLM Secret（`{release}-llm`）、n8n secret、helm release-state Secret（含完整 values）全是 ns-scoped——同 ns 後任一 workload 的 SA 若可讀 Secret 即一鍋端；PSA 的 ns 級差異化（agent 鎖 restricted vs 第三方 chart 需 baseline）喪失，未來上 PSA 的成本從貼 label 升級為 admission controller |
| | **總分** | **40** | **40** | |

\* 第 13 項為補完性評審新增，原 6 個分析員未評。

## 5. 反證查核的翻盤點（打分校正記錄）

原始打分有 5 處立論被自家查核推翻，本文分數已回填。記錄於此供日後重評時對照：

1. **「agent ns 無 quota → 提案容量失控」方向搞反**：無 quota 是**現狀**的缺口；提案反而讓 agent 落入 marketplace 既有 per-tenant quota 之下。
2. **「NP union 提權」不成立**：design.md:3471-3473 的 name-only podSelector 是過時 skeleton；chart 實檔（`woow-paas-charts/charts/agent-runtime` 0.4.3，自首 commit 起）的 NP podSelector = `app.kubernetes.io/name` + `app.kubernetes.io/instance: {release}`，per-instance。同 ns 多 agent 不會互相 union egress。
3. **「同 ns 自然互通」對 agent 不成立**：agent chart 是 Ingress+Egress 雙向 default-deny 且無 same-ns allow——合併 ns 後 agent egress 仍只有 TCP 3000/3001（且仍要 ns 貼 workspace-id label 才 match）。連帶「prompt-injection 下 agent 直連租戶 DB」的風險敘事也被推翻（除非 NP 改開）。
4. **「per-release GC 從零重寫」不成立**：CS `delete_release` 已是共用 ns 內的 per-release GC 骨架（helm uninstall + PVC orphan label）；agent 缺的只是 `{release}-llm` Secret 刪除（operator KubectlService 現無 delete secret 方法）。
5. **auth Secret 撞名後果修正**：不是「靜默互踩輪替 key → 間歇 401」，而是固定名撞 Helm ownership → **同 ns 第二個 agent release install 直接失敗**。對提案是更硬、但 fail-loud 的 blocker。
6. **「agent ns→workspace 對映僅存 Odoo DB」不成立**：workspace ids 持久化在該 ns 的 helm release-state Secret（`helm get values` 可還原），Odoo 消失後仍可反查（只是可發現性差）。
7. 另有一條 C-scale 查核把「方案一/方案二」指涉讀反（把現狀讀成提案），該 refuted 屬誤判，已在校準時排除。

## 6. 提案的硬 blockers（缺一不可，動工前必須解）

1. **agent chart auth Secret 改 release-scoped 命名**（cross-repo `woow-paas-charts`）——否則同 workspace 第二個 project 的 agent 裝不起來。
2. **`delete_runtime` 改 per-release teardown**：複用 CS `delete_release` 骨架 + 補 `{release}-llm` Secret 刪除；`delete_namespace` 從 agent DELETE 路徑移除，workspace teardown 做成獨立顯式端點（dry-run 列將刪資源 + 確認 token），過渡期給 `delete_namespace` 加「只准刪 `pa-` 前綴」型別 guard。
3. **quota 模型統一**：公式改 `3×sum(CS allocated) + sum(agent 配額)`（可啟用 dormant 的 `agent_session.quota_override_*`）、`persistentvolumeclaims=10` 參數化、補 LimitRange（否則未宣告 resources 的容器被 admission 拒建）、spawn 路徑加 quota 預檢與明確錯誤回報。
4. **M:N / unbound project 的 pod 落點拍板**（[plan-gap-stop-and-report](../decisions/patterns/plan-gap-stop-and-report.md) 紀律）：`primary_workspace_id` 升格為建立時 snapshot、不隨綁定漂移的 stored 欄位；或禁跨 ws 綁定；unbound project 明訂 fallback（保留 per-project ns？禁用 agent？）。

## 7. 建議路線（三段式）

### 第 0 段 — 與粒度解耦、現狀就該修的 P0

> 會議把整個 ns 議題 pending 掉了；本段各項**不應跟著 pending**——它們與粒度無關，其中隔離補鋪與 PVC 誤標屬上線前資安/資料安全 gap。

- **補齊租戶隔離的未覆蓋名單**（上線前資安 gap）：prod 手動存量的 14 顆全裸 ns（全部 hermes 系、5 顆 odoo、n8n 等，§2.3-1）比照既鋪的 15 顆補 `default-deny-external`；marketplace 側在 `paas-ws-*` ns 落地 **default-deny + allow-same-ns + 顯式開洞**（cloudflared ingress / operator / Odoo↔agent / DNS），並釐清 agent ns 6/11 有 NP 的 chart 版本 drift（舊 spawn 補渲染或 respawn）。開洞前先以 live cluster 釘死 cloudflared shared-tunnel pod 所在 ns。
- CS ns 補 `woow.tw/workspace-id` label（單一來源 `build_managed_ns_labels` 擴充；值用 raw workspace id，chart values 註解同步修正）——一次修好 MCP dead path、人工反查、NP selector 三件事。prod 手動存量順手補統一 tenant label（收編 marketplace 前的最低治理）。
- `list_pvcs` orphan 標記加 `app.kubernetes.io/instance={release}` selector——**資料刪除級 bug，今天同 workspace 多 service 就會誤標**（`releases.py:229-242` + `helm.py:1231-1251`）；並開 `woow-paas-charts` 驗證 pvc-cleanup cron 現行版是否檢查 Bound。
- `workspace.slug` write guard（identity-anchor，比照 SA `canonical_name` 的 [su-env-exempt-guard pattern](../decisions/patterns/su-env-exempt-guard-false-green.md)）——ns=md5(slug) 但 slug 現可任意改。
- `sync-workspaces` 接線（project 綁定變更 hook 或 drift cron）。
- operator helm install/upgrade 加 `--history-max`（建議 3–5）。

### 第 1 段 — 合併前置（§6 四個 blocker）

### 第 2 段 — 合併本體（把 agent runtime 併入 workspace ns）

- **new-only cutover**：新 spawn 一律落 workspace ns；既有 `pa-` ns 靠 idle reaper（5 分鐘 cron）+ 30 天 GC 自然 drain，不做 live 搬遷（helm 不支援 release 跨 ns 搬移；接受 `/workspace` PVC 狀態丟失）。
- ns 命名**維持 `paas-ws-{md5(slug)[:8]}` 不動**（避免 MCP subdomain / L4 parse 的跨 repo 契約連鎖）。
- 配一次性存量清點腳本（列 cluster 全部 `paas-ws-pa-*` 對 `agent_session` stored hash 取差集），遷移期孤兒不留長尾。
- rollout design 先寫：Odoo/operator/chart 三方 lock-step 順序、雙 config 來源（`ir.config_parameter` vs operator env）合流、回滾語意。

**決策提示**：做完第 0 段後現狀分數約升至 45（互通性、網路安全、溯源缺口都被修掉），屆時再評第 1、2 段是否仍值得——提案剩下的獨有紅利 = 單一生命週期模型 + quota 統一 + offboarding 可稽核刪除；獨有代價 = blast radius 集中 + Secret 同鍋 + 遷移工程。**時程上與會議決議對齊**：第 0 段建議併入 7 月底上線前收尾（資安地基）；第 1、2 段維持 pending，等 Project 解除隱藏（agent ns 開始成長）或 prod 存量收編 marketplace 排程時再啟動。

**prod 手動存量的路線**（不在 matrix scope，但屬同一決策脈絡）：存量手動服務的終局是 marketplace 化（會議議題 3 的 MCP monorepo 已在走）；收編當下自然落入 per-workspace ns，等於提案在存量側的實現路徑。**不建議在手動軌道內另做一次 ns 合併**（成本花在即將淘汰的軌道）。收編前最低治理：統一 tenant label + 補齊 default-deny-external。

## 8. 補充維度（原 rubric 沒想到的）

- **租戶 offboarding / 可稽核徹底刪除**（傾向提案）：現狀 CS ns 永不刪；提案讓「刪 workspace = 級聯 unlink + 刪整顆 ns」成為單一可稽核語意（GDPR-style 刪除證明的低成本布局），前提是 SM/SA 的 CF 資源仍走 Odoo unlink guard。
- **K8s API 層安全邊界**（傾向現狀，已入 matrix #13）。
- **Rollout/回滾時序**（提案必要配套，見 §7 第 2 段）。
- **workspace.slug 不可變性**（前置 blocker，兩案都該修）。
- **Observability / on-call 租戶歸因**（小幅傾向提案，效益等 label 補齊才兌現）。

## 9. Open questions

已由 2026-07-02 live 驗證關閉的項目：

- ~~cluster 層是否另掛 default-deny NP~~ → **已驗**：無 cluster 級；隔離三態混雜（§2.3-1），matrix #1 現狀 2 分維持。
- ~~stg 的 `namespace_prefix` 兩端是否同步~~ → **已驗**：live ns `paas-ws-stg-*` 證實同步為 `paas-ws-stg-`。
- ~~prod 是否已裝 CloudService model~~ → **已驗**：prod 零 `paas-ws-*` CS ns，marketplace 只在 stg 活躍；prod 客戶服務全在手動存量。
- ~~marketplace CS 是否真的共用 ns~~ → **已驗**（本次校正核心）：程式碼唯一產生點 + git 歷史自始 per-workspace + live 一顆 ns 5 release 3 type。

仍開放：

- **會議 6c「內網 Pod CIDR bypass」的技術前提待驗**：CF Access 的 IP bypass 看的是 CF 端看到的 connecting client IP——K3s 內 pod 打對外 hostname 通常經 cluster NAT 出口，CF 看到的是**出口公網 IP 而非 Pod IP**。若如此，bypass 白名單該預填的是 cluster 出口 IP（同為定值），Pod CIDR（RFC1918）可能永遠 match 不到。Access Control 實作前需以真流量驗證 `cf-connecting-ip`。
- agent `pa-` ns 的 NP 覆蓋 drift：11 顆中僅 6 顆（較新）有 NP、5 顆（29-31d 前 spawn）零 NP——確認是 chart 版本演進所致後，決定舊 release 補渲染還是等 GC 自然淘汰。另 agent ns 缺 `paas.woowtech.io/env` label（CS ns 有）——兩條 ns 建立路徑的 label drift 實例。
- pvc-cleanup cron（cross-repo）現行版刪除前是否檢查 PVC Bound？決定 `list_pvcs` 誤標 bug 的實際爆炸半徑。
- operator ClusterRole 實際 manifest（cross-repo）：README 宣稱「RBAC 層也 enforce prefix」，但 K8s RBAC `resourceNames` 不支援 prefix wildcard，存疑。
- prod 手動存量收編 marketplace 的排程與存量遷移設計（DB/filestore 搬遷、subdomain 沿用）——屬另一個 scout/plan 的題目。

## 附錄：分析產物與驗證記錄

- 原始事實調查（7 份 × file:line 證據）、13 維度打分原文、39 條查核 verdict 與補完性評審輸出留存於分析 session 的 scratchpad（`facts.json` / `impacts.json` / `verdicts.json` / `critic.json`）；本文為查核校正後的彙整版。
- Live 驗證（2026-07-02，`kubectl --context woow-k3s` / `helm` 唯讀）：`get ns --show-labels`（粒度盤點、label drift）、`helm list -n paas-ws-stg-86f34bd4`（5 release 3 type 同 ns 的決定性證據）、`get netpol -A` + 逐條 `-o yaml`（隔離三態：`default-deny-external` 語意 vs odoo chart NP 等效放行）、`get resourcequota -A`（PVC 6/10、殘留 quota 實例）、租戶 ns 與 NP 覆蓋名單取差集。
- Git 歷史驗證：`31835ff`（2026-03-01「refactor: use naming module」）——ns 由 `paas-ws-{workspace.id}` 改 `paas-ws-{md5(slug)[:8]}`，粒度自始 per-workspace。
- 會議來源：2026-07-01「上線前功能聚焦」議題 4（Hermes 連線）、5（Namespace 合併，pending）、6c（IP Bypass / 內網 Pod CIDR）、8（Project 隱藏）。
