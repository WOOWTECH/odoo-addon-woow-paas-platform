---
title: 「每 workspace 一個 namespace」架構調整 — 偵察分析與 decision matrix
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

# 「每 workspace 一個 namespace」架構調整 — 偵察分析與 decision matrix

> **性質：偵察（scout）文件，非決策。** 本文是動工前的衝擊分析與打分，尚未拍板。若日後成案，應 promote 成 `docs/plans/` 雙檔（design + implementation）；若拍板不做或做法定案，結論落 `docs/decisions/`。

## 0. TL;DR

Live cluster 驗證（§3.0）後確認：平台實際有**兩條部署軌道**，會議與程式碼各說各的軌道——

- **軌道 A（marketplace 自動部署，paas-operator 管轄）**：**提案有一半已經是現狀**——cloud service 早就是 per-workspace namespace（同 workspace 所有 service 共用 `paas-ws-{md5(slug)[:8]}`，`src/controllers/paas.py:2047`；live 佐證 `paas-ws-stg-86f34bd4` 內 3 個 release 共存）；Security Access 與 Smart Home **完全沒有 k8s 資源**（純 Cloudflare tunnel、connector 在使用者硬體/家中）；agent runtime 沒有任何 Cloudflare 資源。「合併」真正要搬的**只有 agent runtime**（per-project ns `paas-ws-pa-{HMAC hash}`），其他四個子系統衝擊接近零。目前軌道 A **只在 stg 活躍**（prod 零 `paas-ws-*` CS ns）。
- **軌道 B（手動搬遷存量，kubectl/helm 直管）**：prod 實際客戶服務是 **per-service 一顆 ns**（`bnidistrict-odoo`、`alanlin-hermes`、`woowtech-n8n`… 約 29 顆），人類可讀命名、無統一 tenant label、`paas-ws-` prefix 之外（**operator 的 `validate_namespace` 守衛根本管不到**）。**2026-07-01 會議議題 5 的語境是這條軌道**——Eugene 說「service build 時是不同 namespace」對軌道 B 完全正確，對軌道 A 不成立。

13 維度 decision matrix（scope＝軌道 A）加總：**現狀 38 vs 提案 40**——提案方向正確、長期較優（單一生命週期模型、quota 天然對齊、offboarding 語意清晰），但差距比直覺小，勝負取決於 4 個硬 blocker（§6）是否入 scope。**會議「pending、不擋 7 月底上線」的決議與本分析一致**（Project 先隱藏 → agent per-project ns 的成長壓力短期凍結）。對軌道 B，「一 workspace 一 ns」的正解**不是在手動軌道內再做一次 ns 合併，而是收編進 marketplace**（收編當下自然落入 per-workspace ns）。

但有 **3 個與粒度無關、不應跟著 pending 的 P0**（§7 第 0 段）——其中「租戶隔離半鋪」（軌道 B 有 14 顆 ns 全裸、含全部 hermes 系）是**上線前資安 gap**。

## 1. 提案定義與會議語境

**提案來源**：2026-07-01「上線前功能聚焦」會議議題 5，Elmo 提案「PaaS 上一個 workspace 只用一個 namespace，其上所有 cloud service 都放進去」；他的資安自我說服理由是「同一個 workspace 代表使用者已知這幾個服務在同一區網 / 同一企業」。Eugene 的顧慮：會影響命名方式、不一定可行 / 效益不一定夠大、「拆開較安全、彈性更高」。**會議決議：pending、不擋 7 月底上線**（待辦 #12，優先度低）。本文即該評估的執行結果。

本文把提案正規化為兩個方案（scope＝軌道 A，marketplace 自動部署；軌道 B 見 §3.0）：

- **方案一（現狀）**：混合粒度——cloud services 共用 per-workspace ns；agent runtime 每個 project 一顆獨立 ns；SA / SmartHome 無 k8s 資源。
- **方案二（提案）**：每個 workspace 恰好一顆 ns，該 workspace 所有工作負載（全部 cloud services + 全部 project 的 agent runtime pod）同 ns；跨 workspace ns 間網路 deny、同 ns 內互通。**評分前提：「跨 workspace deny」的 NetworkPolicy 配套隨提案一起落地**（查核確認此前提若抽掉，方案二網路安全分數不成立）。

## 2. 方法論

53-agent 多階段 workflow：7 個 reader 逐檔調查五大子系統 → 6 個分析員按 13 維度打分 → **39 條關鍵 claim 逐一反證查核**（每條由獨立 skeptic 讀實際程式碼推翻，含 cross-repo `woow-paas-charts` chart 實檔與 live cluster 佐證）→ 1 個補完性評審找漏掉的維度與打分矛盾。本文分數為查核校正後的最終版；被推翻的原始立論記錄在 §5。

## 3. 現狀盤點（查核校正後）

### 3.0 Live cluster 實況與會議對照（2026-07-02 `woow-k3s` 唯讀驗證）

#### 兩條部署軌道

| | 軌道 A：marketplace 自動部署 | 軌道 B：手動搬遷存量 |
|---|---|---|
| ns 命名 | `paas-ws-{md5(slug)[:8]}`（CS）/ `paas-ws-pa-{HMAC16}`（agent） | `{客戶}-{服務}` 人類可讀（`bnidistrict-odoo`、`alanlin-hermes`、`woowtech-n8n`…） |
| 粒度 | CS＝per-workspace（live：`paas-ws-stg-86f34bd4` 內 3 release 共存）；agent＝per-project | **per-service 一顆 ns**；同客戶多服務＝多顆 ns（`torchmedia`+`torchmedia-hermes`、`woowtech-odoo`+`woowtech-n8n`） |
| 管轄 | paas-operator（`validate_namespace` prefix 守衛內） | kubectl/helm 直管——**在 `paas-ws-` 守衛之外，operator 管不到** |
| label / quota | `paas-managed=true`（+env）、ResourceQuota | 無統一 tenant label（混雜 `part-of`/`tenant`/`project`）、抽查無 quota |
| live 規模 | **只在 stg**：2 顆 CS ns + 11 顆 agent `pa-` ns；**prod 零**（佐證 pitfall「prod 未裝 CloudService model」） | **prod 約 29 顆**（14+ odoo、5 hermes、n8n、misc） |

#### 會議說法 vs 程式碼/live 證據

| 會議說法（議題 4/5） | 查核結果 |
|---|---|
| 「service build 時是不同 namespace，故需走外部連線」 | **對軌道 B 成立**（per-service ns、live 證實）；**對軌道 A 不成立**（CS 已 per-workspace 共用，`paas.py:2047`） |
| 「目前開兩個 namespace，預設會擋掉 namespace 間通訊」 | **半對**——隔離現狀是三態混雜（見 §3.2-1）：軌道 B 有 15 顆 ns 已鋪 `default-deny-external`（allow same-ns / deny 跨 ns ingress，約 2026-06-25 統一鋪設），**但 14 顆全裸**（全部 5 顆 hermes 系、`bnidistrict/changching/linyanshan/lisachuzhu/lyucijyun-odoo`、`woowtech-n8n`、`hermes`、`hermes-mcp-admin`、`woowhademo`）；軌道 A 的 CS ns chart NP 語意等效放行、agent ns 僅 6/11 有 NP |
| 「拆開較安全」 | **前提不完整**——拆開本身不帶來隔離，隔離靠 NP；NP 半鋪之下「拆開」與「合併」的安全差異主要在 k8s API 層（Secret/SA/PSA，matrix #13），網路層兩案都取決於 NP 配套 |
| 「效益不一定夠大」 | **證實**——matrix 38 vs 40（§4）；且互通紅利大半可在現粒度下用「label + NP + 接線」取得（§3.2-2） |
| 「會影響命名方式」 | **可收斂**——維持 `paas-ws-{md5(slug)[:8]}` scheme 不動就沒有命名連鎖（CF/MCP subdomain 公式與 ns 正交或吃 stored 值，§3.1 表末列） |

#### 對決策的三個含義

1. **會議 pending 的決議合理且被本分析支持**：Project 本階段隱藏（會議議題 8）→ agent runtime（綁 project）的 per-project ns 成長壓力短期凍結；軌道 A 合併的急迫性低。
2. **軌道 B 的「一 workspace 一 ns」正解是收編進 marketplace，不是在手動軌道內合併**：存量服務 marketplace 化（會議議題 3 的 MCP monorepo 已在走這條路）當下自然落入 per-workspace ns；在即將淘汰的軌道上花 ns 遷移成本是浪費。收編前的最低限度治理：補統一 tenant label + 補齊 `default-deny-external` 未覆蓋名單。
3. **會議議題 6c 的「內網 Pod CIDR bypass」與本題相交**：它讓「K3s 內的 MCP 摸開了 Access Control 的服務」在 **CF 層**解掉，進一步縮小「同 ns 內網互通」的必要性（實務上服務互連走 CF 對外 hostname，不走 cluster 內 FQDN）。技術疑點見 §9。

### 3.1 各子系統 ns 粒度與提案衝擊

| 子系統 | ns 粒度 | 提案衝擊 |
|---|---|---|
| Cloud Service | **已是 per-workspace**（`paas-ws-{ws_hash}`；release=`svc-{uuid8}` 天然不撞名） | 被動衝擊：quota 被 agent 分食、PVC 誤標面擴大、可能被 agent teardown 整鍋端 |
| Agent Runtime | per-project（`paas-ws-pa-{HMAC16}`，operator 自建、**無 quota**） | **全部改動集中在這**：3 處 ns 推導 lock-step（`agent_runtimes.py:61`、`mcp_invalidator.py:18-22`、`skill_invalidator.py:22-26`）、API contract（reap/resume/status/invalidate/delete 全以 project_hash 單鍵導 ns）、chart、DB migration（stored `helm_namespace`/`pod_ws_url` 全 stale）、teardown 重寫 |
| Security Access | 無 k8s ns（dedicated CF tunnel，connector 在租戶自有硬體） | 幾乎零。唯一守線：`canonical_name` 刻意不從 `namespace_prefix` 推導（`security_access.py:207-213`，identity-anchor 設計保證），ns 改制不得反向引入耦合 |
| Smart Home | 無 k8s ns（dedicated CF tunnel，connector 在使用者家中） | 零。守線：「刪 ns ≠ teardown 租戶」——CF 資源不在 ns 裡，必須仍走 Odoo unlink guard（否則重演 [cf-ns-orphan pitfall](../tracking/pitfalls/cf-ns-orphan-no-account-gc.md)） |
| Cloudflare 整合 | agent runtime 零 CF 資源；只有 CS 的 MCP subdomain 公式吃 ns 名（`generate_subdomain` = `{release}-{ns 去 paas-ws-}`，`cloudflare.py:804-816`） | 合併本身零影響。風險僅在「順手改 ns 命名 scheme」→ `reconstruct_mcp_subdomain`（`src/services/naming.py:61-80`）跨 repo byte-identical 契約連鎖。**維持 `paas-ws-{md5(slug)[:8]}` 不動就安全** |

### 3.2 兩個顛覆直覺的現狀事實

1. **租戶之間的網路隔離現狀是「三態混雜」，且平台層保證缺席（CS 側）**：operator `create_namespace` 只建 ns + ResourceQuota + registry secret（`extra/paas-operator/src/services/helm.py:737-811`），全 repo 無任何對 CS 租戶 ns 下 NetworkPolicy 的程式。Live 驗證（2026-07-02）三態：① 軌道 A CS ns 內存在 **chart 自帶** per-release NP，但語意等效放行（`svc-*-odoo` 的 ingress rule 只限 port、不限來源＝任何 ns 都能打 8069；egress allow-all）→ 跨租戶隔離實質缺席；② 軌道 A agent ns 有 per-instance 雙向 default-deny NP（chart 實檔驗證，見 §5-2），但 **11 顆中只有 6 顆有**（較舊 5 顆零 NP，chart 版本 drift）；③ 軌道 B 有 15 顆 ns 鋪了 `default-deny-external`（allow same-ns ingress / deny 跨 ns），**14 顆全裸**。結論不變且更具體：**「跨 workspace deny」沒有單一保證來源、覆蓋有洞，是兩案共同欠的地基，與 ns 粒度無關。**
2. **agent→租戶服務互通現狀是 dead path**：NP 設計走 `namespaceSelector matchLabels woow.tw/workspace-id`（design.md:3486-3497），但 CS ns 從未被貼該 label（`build_managed_ns_labels` 只有 `paas-managed=true` + env，`helm.py:108-129`），且 `sync-workspaces` 端點在生產碼無 caller（只有 client 方法與測試）→ project 改綁 cloud service 後 egress allowlist 不同步。**修法（貼 label + 接線）同樣與粒度正交。**

### 3.3 其他關鍵事實

- `delete_namespace` 全 codebase 唯一生產 caller = agent teardown（`agent_runtimes.py:577-586`）；operator 連可刪 CS ns 的 API 端點都不存在（`namespaces.py` 僅 POST create）→ CS 孤兒 ns + quota + registry secret 永存、無 TTL。
- Quota：CS ns 有（`max(3×sum(allocated), 8CPU/8Gi/100Gi)`、`persistentvolumeclaims=10` 硬編碼、每次 deploy 重算但刪 service 不縮，`cloud_service.py:438-447` + `helm.py:770-787`）；agent ns **完全無 quota**（`_ensure_namespace` 只 apply label，`agent_runtimes.py:81-95`）；全 repo 無 LimitRange。**Live 佐證**（2026-07-02）：`paas-ws-stg-86f34bd4` PVC 已用 6/10（3 個 release、36h 內）——PVC 上限分食風險是現在進行式；`paas-ws-stg-ad023482` 用量全零但殘留 12CPU quota ＝「服務刪光後 ns + quota 永存」的實例。
- project↔workspace 是 **M:N**（`project.workspace_ids` 是從 `cloud_service_ids` 算出的 computed M2M，`project_project.py:36-49`），且 unbound project 合法（decouple invariant D1）→「per-workspace ns」對跨 ws / unbound project 沒有唯一落點，是提案的 design gap。
- ns 上無任何 workspace/owner label；`ws_hash` = md5 單向、agent hash = HMAC 刻意反枚舉。但 agent ns 的 workspace ids 其實已持久化在 k8s 端（helm release-state Secret，`helm get values` 可還原——spawn 與 sync-workspaces 都寫入 `networkPolicy.egressWorkspaceIds`）。
- `ir.config_parameter namespace_prefix`（Odoo 端）與 operator `NAMESPACE_PREFIX` 是**兩個獨立配置源、無共同來源**——改 ns 模型時的 drift 風險點。
- docs 內既有「統一命名」分析（[naming-env-token plan](../plans/2026-Q2/2026-06-21-naming-env-token-prod-stg-plan.md)、[L4 reconciler design](../plans/2026-Q2/2026-06-25-l4-reconciler-design.md)）全部是 **prod/stg env-token 軸**，沒有 per-workspace ns 粒度的既有分析——本題是新題。

## 4. Decision Matrix（1–5 分，5 最好；查核校正後；scope＝軌道 A）

> 軌道 B 不進 matrix：它的「合併」正解是收編進 marketplace（§3.0 含義 2），屆時自動落入方案二在軌道 A 的形態。

| # | 維度 | 現狀 | 提案 | 關鍵理由 |
|---|---|:---:|:---:|---|
| 1 | 租戶網路安全性 | 2 | 3 | 現狀 CS ns 零 NP 裸奔（§3.2-1）；提案配套建 deny 後升級，但同 ns 把 agent 與 CS 攻擊面合併，抵銷部分紅利。**若只併 ns 不建 NP → 1 分** |
| 2 | 同 ws 服務互通性 | 2 | 4 | 現狀 MCP egress dead path + 綁定變更不同步（§3.2-2）；提案同 ns 用 podSelector 較簡單。但 agent egress default-deny 同 ns 也照擋——「同 ns 自然互通」對 agent **不成立**（§5-3），兩案都要動 NP |
| 3 | 維護性（刪、改） | 3 | 3 | 現狀兩套模型雙寫、3 處 ns 推導副本、CS ns 永不刪；提案單一模型但失去「刪 ns 一鍋端」兜底。per-release GC 骨架已存在（CS `delete_release`），agent 真正缺的只是 `{release}-llm` Secret 刪除（§5-4） |
| 4 | 名稱/資源衝突 | 4 | 2 | **硬 blocker**：agent chart auth Secret 固定名 per-ns → 同 ns 第二個 project 的 agent `helm install` 直接撞 Helm ownership 失敗（fail-loud、裝不起來，§5-5）。PVC orphan 標記全 ns 一鍋端誤標面擴大（現狀已是 bug） |
| 5 | Blast radius / 故障隔離 | 4 | 2 | `delete_runtime` 第二步刪整顆 ns——漏改就是「刪一個 project = 清掉整個租戶」的保證事故；workspace ns 變單一故障域。另注意：per-project 物理隔離是 agent-runtime design 的 sign-off 條款，推翻需正式 decision doc |
| 6 | 擴增性（規模維運） | 3 | 4 | 現狀 ns 數 = O(ws) + O(近 30 天活躍 project)（reap 只 scale-to-0、GC 滯留 30 天）、兩套生命週期 runbook 雙寫；提案單一模型收斂 |
| 7 | K8s 平台限制 | 3 | 4 | 兩案都遠未觸頂（prod woow-k3s = 4-node HA embedded etcd，repo 內有一手紀錄）；提案 ns 更少、per-ns 固定物件（registry secret 等 ~3 件/ns）不重複。`--history-max` 未設是共同待修 |
| 8 | 硬體資源限制可控 | 2 | 4 | **現狀 agent ns 完全無 ResourceQuota = 每個 project 一個無上限逃逸口**；提案天然納入 workspace quota（quota 本來就按 workspace 總和訂）。但公式須納入 agent、補 LimitRange、spawn 加 quota 預檢，否則 quota 滿時 helm install 成功但 pod FailedCreate 靜默（撞 coldstart pitfall 觀測盲區） |
| 9 | 外部資源可溯源 | 3 | 3 | CF 命名/溯源與 ns 粒度**正交**：SA 全鏈 stamp `canonical_name`、CS/SM DNS 無 comment、CS 呼叫未傳 env token、L4 reconciler 只看 CF name 不看 ns——兩案同分。提案不修任何既有溯源缺口 |
| 10 | Odoo 消失時孤兒辨識 | 3 | 3 | agent ns 的 workspace ids 可由 helm release-state Secret 還原（§5-6），現狀差距被原分析高估；提案的 release 級殘留混在活 ns 裡反而更難認。實質改善要靠補 workspace-id label，兩案皆然 |
| 11 | 遷移成本/相容性 | 5 | 2 | 現狀零成本；提案是雙 repo + chart + DB migration + API contract 全鏈改動（好消息：100% 集中在 agent runtime）。可行路徑：new-only cutover（§7 第 2 段） |
| 12 | 計費/成本歸屬 | 2 | 4 | repo 零 metering（純 plan 預購制）；未來若做 per-ns 成本歸屬，「一 ns = 一租戶」天然對齊。M:N project 的成本分攤是 gap |
| 13 | K8s API 層安全邊界（Secret/SA/PSA）* | 4 | 2 | 明文 LLM Secret（`{release}-llm`）、n8n secret、helm release-state Secret（含完整 values）全是 ns-scoped——同 ns 後任一 workload 的 SA 若可讀 Secret 即一鍋端；PSA 的 ns 級差異化（agent 鎖 restricted vs 第三方 chart 需 baseline）喪失，未來上 PSA 的成本從貼 label 升級為 admission controller |
| | **總分** | **38** | **40** | |

\* 第 13 項為補完性評審新增，原 6 個分析員未評。

## 5. 反證查核的翻盤點（打分校正記錄）

原始打分有 5 處立論被自家查核推翻，本文分數已回填。記錄於此供日後重評時對照：

1. **「agent ns 無 quota → 提案容量失控」方向搞反**：無 quota 是**現狀**的缺口；提案反而讓 agent 落入 marketplace 既有 per-tenant quota 之下。
2. **「NP union 提權」不成立**：design.md:3471-3473 的 name-only podSelector 是過時 skeleton；chart 實檔（`woow-paas-charts/charts/agent-runtime` 0.4.3，自首 commit 起）NP podSelector = `app.kubernetes.io/name` + `app.kubernetes.io/instance: {release}`，per-instance。同 ns 多 agent 不會互相 union egress。
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

- **補齊租戶隔離的未覆蓋名單**（上線前資安 gap）：軌道 B 的 14 顆全裸 ns（全部 hermes 系、5 顆 odoo、n8n 等，§3.0）比照既鋪的 15 顆補 `default-deny-external`；軌道 A 在 `paas-ws-*` ns 落地 **default-deny + allow-same-ns + 顯式開洞**（cloudflared ingress / operator / Odoo↔agent / DNS），並釐清 agent ns 6/11 有 NP 的 chart 版本 drift（舊 spawn 補渲染或 respawn）。開洞前先以 live cluster 釘死 cloudflared shared-tunnel pod 所在 ns。
- CS ns 補 `woow.tw/workspace-id` label（單一來源 `build_managed_ns_labels` 擴充；值用 raw workspace id，chart values 註解同步修正）——一次修好 MCP dead path、人工反查、NP selector 三件事。軌道 B 順手補統一 tenant label（收編 marketplace 前的最低治理）。
- `list_pvcs` orphan 標記加 `app.kubernetes.io/instance={release}` selector——**資料刪除級 bug，今天同 workspace 多 service 就會誤標**（`releases.py:229-242` + `helm.py:1231-1251`）；並開 `woow-paas-charts` 驗證 pvc-cleanup cron 現行版是否檢查 Bound。
- `workspace.slug` write guard（identity-anchor，比照 SA `canonical_name` 的 [su-env-exempt-guard pattern](../decisions/patterns/su-env-exempt-guard-false-green.md)）——ns=md5(slug) 但 slug 現可任意改。
- `sync-workspaces` 接線（project 綁定變更 hook 或 drift cron）。
- operator helm install/upgrade 加 `--history-max`（建議 3–5）。

### 第 1 段 — 合併前置（§6 四個 blocker）

### 第 2 段 — 合併本體

- **new-only cutover**：新 spawn 一律落 workspace ns；既有 `pa-` ns 靠 idle reaper（5 分鐘 cron）+ 30 天 GC 自然 drain，不做 live 搬遷（helm 不支援 release 跨 ns 搬移；接受 `/workspace` PVC 狀態丟失）。
- ns 命名**維持 `paas-ws-{md5(slug)[:8]}` 不動**（避免 MCP subdomain / L4 parse 的跨 repo 契約連鎖）。
- 配一次性存量清點腳本（列 cluster 全部 `paas-ws-pa-*` 對 `agent_session` stored hash 取差集），遷移期孤兒不留長尾。
- rollout design 先寫：Odoo/operator/chart 三方 lock-step 順序、雙 config 來源（`ir.config_parameter` vs operator env）合流、回滾語意。

**決策提示**：做完第 0 段後現狀分數約升至 43（互通性、網路安全、溯源缺口都被修掉），屆時再評第 1、2 段是否仍值得——提案剩下的獨有紅利 = 單一生命週期模型 + quota 統一 + offboarding 可稽核刪除；獨有代價 = blast radius 集中 + Secret 同鍋 + 遷移工程。**時程上與會議決議對齊**：第 0 段建議併入 7 月底上線前收尾（資安地基）；第 1、2 段維持 pending，等 Project 解除隱藏（agent ns 開始成長）或軌道 B 收編 marketplace 排程時再啟動。

**軌道 B 的路線**（不在 matrix scope，但屬同一決策脈絡）：存量手動服務的終局是 marketplace 化（會議議題 3 的 MCP monorepo 已在走）；收編當下自然落入 per-workspace ns，等於提案在軌道 B 的實現路徑。**不建議在手動軌道內另做一次 ns 合併**（成本花在即將淘汰的軌道）。收編前最低治理：統一 tenant label + 補齊 default-deny-external。

## 8. 補充維度（原 rubric 沒想到的）

- **租戶 offboarding / 可稽核徹底刪除**（傾向提案）：現狀 CS ns 永不刪；提案讓「刪 workspace = 級聯 unlink + 刪整顆 ns」成為單一可稽核語意（GDPR-style 刪除證明的低成本布局），前提是 SM/SA 的 CF 資源仍走 Odoo unlink guard。
- **K8s API 層安全邊界**（傾向現狀，已入 matrix #13）。
- **Rollout/回滾時序**（提案必要配套，見 §7 第 2 段）。
- **workspace.slug 不可變性**（前置 blocker，兩案都該修）。
- **Observability / on-call 租戶歸因**（小幅傾向提案，效益等 label 補齊才兌現）。

## 9. Open questions

已由 2026-07-02 live 驗證關閉的項目：

- ~~cluster 層是否另掛 default-deny NP~~ → **已驗**：無 cluster 級；隔離三態混雜（§3.2-1），matrix #1 現狀 2 分維持。
- ~~stg 的 `namespace_prefix` 兩端是否同步~~ → **已驗**：live ns `paas-ws-stg-*` 證實同步為 `paas-ws-stg-`。
- ~~prod 是否已裝 CloudService model~~ → **已驗**：prod 零 `paas-ws-*` CS ns，marketplace 只在 stg 活躍；prod 客戶服務全在軌道 B。

仍開放：

- **會議 6c「內網 Pod CIDR bypass」的技術前提待驗**：CF Access 的 IP bypass 看的是 CF 端看到的 connecting client IP——K3s 內 pod 打對外 hostname 通常經 cluster NAT 出口，CF 看到的是**出口公網 IP 而非 Pod IP**。若如此，bypass 白名單該預填的是 cluster 出口 IP（同為定值），Pod CIDR（RFC1918）可能永遠 match 不到。Access Control 實作前需以真流量驗證 `cf-connecting-ip`。
- agent `pa-` ns 的 NP 覆蓋 drift：11 顆中僅 6 顆（較新）有 NP、5 顆（29-31d 前 spawn）零 NP——確認是 chart 版本演進所致後，決定舊 release 補渲染還是等 GC 自然淘汰。另 agent ns 缺 `paas.woowtech.io/env` label（CS ns 有）——兩條 ns 建立路徑的 label drift 實例。
- pvc-cleanup cron（cross-repo）現行版刪除前是否檢查 PVC Bound？決定 `list_pvcs` 誤標 bug 的實際爆炸半徑。
- operator ClusterRole 實際 manifest（cross-repo）：README 宣稱「RBAC 層也 enforce prefix」，但 K8s RBAC `resourceNames` 不支援 prefix wildcard，存疑。
- 軌道 B 收編 marketplace 的排程與存量遷移設計（DB/filestore 搬遷、subdomain 沿用）——屬另一個 scout/plan 的題目。

## 附錄：分析產物與驗證記錄

- 原始事實調查（7 份 × file:line 證據）、13 維度打分原文、39 條查核 verdict 與補完性評審輸出留存於分析 session 的 scratchpad（`facts.json` / `impacts.json` / `verdicts.json` / `critic.json`）；本文為查核校正後的彙整版。
- Live 驗證（2026-07-02，`kubectl --context woow-k3s` 唯讀）：`get ns --show-labels`（兩軌道盤點、label drift）、`get netpol -A` + 逐條 `-o yaml`（隔離三態：`default-deny-external` 語意 vs odoo chart NP 等效放行）、`get resourcequota -A`（PVC 6/10、殘留 quota 實例）、租戶 ns 與 NP 覆蓋名單取差集。
- 會議來源：2026-07-01「上線前功能聚焦」議題 4（Hermes 連線）、5（Namespace 合併，pending）、6c（IP Bypass / 內網 Pod CIDR）、8（Project 隱藏）。
