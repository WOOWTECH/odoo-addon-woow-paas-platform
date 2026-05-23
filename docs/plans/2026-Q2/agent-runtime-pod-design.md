---
name: plan-agent-runtime-pod-design
status: signed-off (design + plan complete; ready for tester hand-off)
created: 2026-05-21
updated: 2026-05-21
phase: planning
team: feature-agent
planner: agent-runtime-pod-planner (phase-planner)
related:
  - docs/decisions/refactor/cloud-service-project-decouple.md
  - docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md
  - docs/decisions/patterns/paas-workspace-member-acl-tightening.md
  - docs/decisions/patterns/odoo-std-field-no-override.md
  - docs/plans/2026-Q2/2026-05-18-mcp-project-decouple-plan.md
  - charts/odoo-dev-sandbox/  # K8s sandbox helm chart 範例
  - extra/paas-operator/      # 既有 FastAPI + helm wrapper
---

# Agent Runtime Pod — Design

> **Status**：Phase 1（§1 review）— 等 leader sign-off 後推進 §2~§7。
>
> 本 doc 為 living document，逐節 sign-off 後累加。§2~§7 目前只有標題佔位。

## Context

### Goal

把 PaaS 的 AI agent runtime（LangGraph reasoning loop + skill executor + MCP client）從
Odoo Python process 搬到 per-conversation 的 sandbox pod，解決「skill 含 script 要安全
執行」的核心問題，並把 Odoo 退化為 metadata store + transport gateway。

### Pre-aligned conclusion（前一個 session）

**Branch B + LangGraph in pod, v1**：

```
Odoo (chat UI + history + metadata)
  ↕ WS / SSE
Agent Pod (per conversation, LONG-LIVED)
  ├ LangGraph reasoning loop
  ├ Skill executor (in-process subprocess, sandboxed by pod boundary)
  ├ MCP client (langchain-mcp-adapters)
  ├ /workspace PVC（跨呼叫 state）
  ├ /skills/<name>/（lazy fetch + cache）
  └ non-root / seccomp / NetworkPolicy / Quota
       ↓ egress (whitelist)
MCP Servers (現有獨立 pod，不動)
```

### Codebase 校準（這次 review 期間實際 grep / read）

以下是後續 review 的事實基礎，**所有 §1.1 findings 都基於這個校準**：

| 主題 | 校準事實 | 來源 |
|------|---------|------|
| Frontend chat transport | 用 `EventSource` 訂閱 SSE，無 WS client | `src/static/src/paas/services/ai_service.js:130` |
| AI 同步路徑（非 SSE） | `discuss.channel.message_post` override 同步 in-process call LangChain | `src/models/discuss_channel.py:55-118` |
| AI SSE 路徑 | `/api/ai/stream/<channel_id>` controller 內 `asyncio.run` LangGraph | `src/controllers/ai_assistant.py:353-544` |
| LangGraph build | `_build_agent_graph` / `_async_agent_invoke` / `_async_agent_stream` 直接在 Odoo addon 內 | `src/models/ai_client.py:231-409` |
| Odoo bus 使用 | 全 repo 唯一 `bus.bus._sendone` 在 `discuss_channel.py:248`（給 Odoo Discuss UI） | grep |
| MCP config 介面 | `mcp_server._get_mcp_client_config()` 已是純 dict（`transport`/`url`/`headers`） | `src/models/mcp_server.py:141-159` |
| MCP D4 invariant（active）| `scope='user'` iff `project_id IS NOT NULL`；user-scope MCP 是 **project 一等公民**（owned by project，不再依附 cloud_service）| `src/models/mcp_server.py:111-139` + `docs/decisions/refactor/mcp-project-decouple.md` |
| MCP D5 invariant（**deprecated — refactor branch cycle 4 拔除**）| ~~user-scope MCP 必須掛在綁 cloud_service 的 project~~ 已刪 — unbound project + user-scope MCP 是合法狀態；外部 URL MCP 不依附任何 cloud_service | `docs/decisions/refactor/mcp-url-redesign.md` §Invariants |
| MCP cleanup ordering | `cloud_service._do_cleanup`：先 unlink **`sidecar_backed=True`** user-scope MCP → helm uninstall → state='deleting'；external (`sidecar_backed=False`) MCP **不動** | `src/models/cloud_service.py:1119-1201` + Inv #9 |
| K8s namespace pattern | 既有 cloud_service 每 service 一個 helm release，共用 per-workspace namespace + ResourceQuota（3× sum of `allocated_*` 欄位） | `src/models/cloud_service.py:431-453` |
| StorageClass 默認 | `ir.config_parameter` `woow_paas_platform.storage_class_name` = `longhorn-delete`（**reclaim policy = Delete**） | `src/models/cloud_service.py:393-399` |
| Suspend pattern | `_do_suspend` / `_do_resume` = scale to 0/1 replicas（保留 PVC） | `src/models/cloud_service.py:1042-1117` |
| TTL pattern（既有） | dev-sandbox helm chart 用 namespace annotation + 每小時 CronJob `kubectl delete namespace` | `charts/odoo-dev-sandbox/templates/ttl-cronjob.yaml` |
| NetworkPolicy pattern（既有） | dev-sandbox default-deny + allow-internal，egress except RFC1918（K3s reply-traffic workaround 已 patch） | `charts/odoo-dev-sandbox/templates/network-policy.yaml` |
| paas-operator API surface | helm-centric：create/get/upgrade/delete/rollback release、scale、patch_sidecar、create_namespace、create_route、create_tunnel；**無 agent/runtime 系列** | `extra/paas-operator/src/api/*.py` |

---

## §1 Architecture (high-level)

### §1.0 Initial proposal（來自 task brief）

如上 Context 區的 ASCII art，核心設計決定：

1. Odoo 退化為 metadata store + WS gateway，不再跑 LangGraph
2. Agent Pod scope = conversation；first message 觸發 paas-operator 起 pod；idle TTL reap，PVC 保留 state，resume 重起 restore
3. Skill exec 沒有 cross-pod RPC，**直接 in-process subprocess**（但仍 sandbox 邊界 — pod 級 isolation）
4. MCP 不動：仍是獨立 pod，連線從 "Odoo→MCP" 變成 "Agent Pod→MCP"
5. 新增 helm chart：`agent-runtime`
6. 新增 Odoo models：`skill`、`agent_session`、`agent_template`
7. LangGraph 程式碼從 Odoo addon 搬到獨立 repo（agent pod image source）

### §1.1 Review findings（Phase 1 — 本次產出）

針對 task brief 列的 7 個 review 問題，逐項給 **Finding → Gap/Risk → 修正提案**。

修正提案中標註 **[需 leader 拍板]** 的，phase-planner 不自行拍板，由 leader 轉達使用者。

---

#### Q1：Odoo bus 模組能否撐 WS gateway？

**Finding**

- Odoo 18 `bus.bus` 模組設計目的是 **「Odoo backend → browser」單向 push**（longpolling 或 SSE chunked transfer，不是 raw WebSocket upgrade）。其角色是「Odoo 內部 channel 向已連線 browser 廣播」。
- PaaS 自有 OWL app（`/woow`）的 chat UI **完全 bypass `bus.bus`**：frontend 用 `EventSource` 訂閱 `/api/ai/stream/<channel_id>`（`ai_service.js:130`）。
- 全 repo 唯一 `bus.bus._sendone` 在 `discuss_channel.py:248`，是給 Odoo 原生 Discuss UI 即時更新用，跟 PaaS chat UI 不相干。
- §1 ASCII art 寫 "WS / SSE" 把兩段 transport 混在一起，但實際有兩段獨立通訊：
  - (A) Browser ↔ Odoo
  - (B) Odoo ↔ Agent Pod

**Gap / Risk**

- 「Odoo bus 撐 WS gateway」假設兩個段都靠 bus — 但 bus 不適合任一段：
  - 對 (A)：bus 比現有 SSE 沒帶來任何 advantage，反而 lock-in 進 Odoo Discuss UI 的 assumption（partner/channel/message 模型），與 PaaS chat UI 的乾淨抽象衝突
  - 對 (B)：bus 是 Odoo 內部 channel，無法跟外部 service（pod）雙向通訊。要硬接需要 Odoo cron worker poll pod 再 forward 進 bus，是 anti-pattern + 高 latency
- §1 沒區分這兩段 → 真要落地時會撞牆

**修正提案**

- **(A) Browser ↔ Odoo：保留既有 SSE EventSource，不動 frontend**
  - 既有 `/api/ai/stream/<channel_id>` controller 已是 SSE generator
  - 改寫 generator 內部 — 從「`asyncio.run` LangGraph」變成「open WS client to agent pod, forward chunks as SSE events」
  - frontend `ai_service.js` 零變更
- **(B) Odoo ↔ Pod：新建 WS client (outbound from Odoo controller)**
  - 在 SSE generator 內用 `websockets` 套件 open outbound WS to `ws://agent-runtime-{conv}.{ns}.svc.cluster.local/v1/chat`
  - Pod 端是 WS server（FastAPI WebSocket route）
  - WS message schema：JSON envelope `{type: 'user_message'|'tool_call'|'tool_result'|'text_chunk'|'error', payload: {...}}`
- **完全廢除 §1 「Odoo bus 模組」字眼**，改寫為「Odoo controller 同時扮演 WS client (to pod) + SSE server (to browser)」

**Open question for leader**

- **[需 leader 拍板]** 是否接受「frontend 完全不動，只在 Odoo controller 內換 backend impl」這個策略？這會讓 migration 切片更小（frontend 零回歸風險），代價是 Odoo controller 仍要消耗 1 條 worker per active conversation（為 SSE generator 阻塞）— 對 Odoo worker pool 是 baseline 100 active = 100 worker slot。

---

#### Q2：PVC 跨 pod 重啟 restore 與既有 quota / TTL 衝突？

**Finding**

- 既有 cloud_service 部署用 `client.create_namespace(cpu_limit, memory_limit, storage_limit)` — quota 是 **per-workspace namespace**，limits = 3× sum of `allocated_*` 欄位（最低 8 CPU / 8 Gi / 100 Gi）
- StorageClass 預設 `longhorn-delete` — reclaim policy = **Delete**（PV 隨 PVC 刪）
- 既有 TTL 機制（dev-sandbox helm chart）：namespace annotation `sandbox.woow.tw/expires` + 每小時 CronJob `kubectl delete namespace` — **整個 namespace 砍掉**（含所有 PVC + PV）
- 既有 cloud_service `_do_suspend` / `_do_resume`：scale to 0 / 1 replicas，pod 拆但 PVC + Service 保留，state 留存 — **這才是真正能跨 pod 重啟 restore 的 pattern**

**Gap / Risk**

- §1 寫「idle TTL reap，PVC 保留 state，resume 重起 restore」— 但既有 namespace-TTL pattern 是 **delete namespace**，這跟「PVC 保留」直接矛盾
- 若 PVC 用 `longhorn-delete` SC + namespace 整個 delete → PV 也刪 → **state 永久 lose**
- 若 agent pod 自己一個 namespace（per-conversation），整個 namespace TTL 又無法保留 PVC；若 agent pod 跟 cloud_service 共用 namespace，又會壓縮既有 workspace quota

**修正提案**

- **粒度決定**：agent pod **不獨立 namespace**，而是共用 per-workspace 的 `paas-agent-ws{N}` namespace（或更激進：跟 cloud_service 共用 `paas-prod-ws{N}`）
- **Reap 機制 = scale to 0**（仿 `cloud_service._do_suspend`），非 delete pod / delete namespace
  - Pod-level annotation `agent.woow.tw/idle-since=<ts>` + CronJob 比對 `>30min` → patch `replicas=0`
  - Resume = patch `replicas=1`（或外部觸發，由 paas-operator 暴露 endpoint）
  - Pod 拆 → PVC 保留 → LangGraph thread checkpoint 保留
- **長期 GC**：超過長 TTL（建議 30 天）未活躍 → helm uninstall（PVC 一起回收）
- **StorageClass 選擇**：
  - 選項 X：sticky 用 `longhorn-delete`（reclaim Delete），accept state 隨 long-TTL 一起釋放（30 天無互動 = 用戶放棄 conversation）— 簡單
  - 選項 Y：新增 `longhorn-retain` SC + 設 `agent.woow.tw/storage-class` config param — state 不會被 delete，但要手動 GC 失聯 PV — 複雜
- **Quota 配額**：
  - 把 agent-runtime 的 resources 計入 workspace 的 `allocated_vcpu` / `allocated_ram_gb` / `allocated_storage_gb`（或新增 `agent_*` 欄位），讓 `create_namespace` 的 3× quota 計算自動 cover
  - 若 agent-runtime 跟 cloud_service 共用 namespace，quota 自然合併；若獨立 namespace，要在 quota 計算公式加入 agent 配額

**Open questions for leader**

- **[需 leader 拍板]** Reclaim policy 走 X（簡單，長 TTL 後 state lose）或 Y（複雜，state 持久）？
- **[需 leader 拍板]** agent-runtime 是 (a) 共用 cloud_service 的 workspace namespace，還是 (b) 獨立 `paas-agent-ws{N}` namespace？前者 quota 與 NetworkPolicy 跟 cloud_service 緊耦合，後者乾淨但 quota 計算邏輯要動。

---

#### Q3：每對話一個 pod — 100 active conversation 的容量 / cold start UX / keep-warm pool？

**Finding**

- 100 個活躍 conversation = **100 pod** baseline
- 每 pod resource baseline（LangGraph + langchain-mcp-adapters + python runtime + minimal model init）保守估：
  - **request: 200m CPU / 256 Mi RAM；limit: 1 CPU / 1 Gi RAM**
  - 100 pod 純 idle ≈ 20 vCPU request / 25 Gi RAM request
  - 100 PVC × 1 Gi = 100 Gi storage
- 既有 woow-k3s cluster 容量（init-agent-4 note 未提供具體上限）— 需要 leader 給數據
- Cold start：first message → spawn pod → wait ready：
  - K8s scheduler: 1-3s
  - Image pull (cached): 0.5-2s
  - Container start + python import + LangGraph init: 3-8s
  - **總 cold start = 5-15s** — 使用者體感「卡住」
- 既有 cloud_service 部署：post-deploy init 靠 cron 每 60s 輪詢 → 不適用 chat first-message UX

**Gap / Risk**

- §1 沒講 pod 數量上限、quota 警戒、cold start 處理
- 「first message 觸發 spawn」沒講是同步阻塞還是非同步先回「啟動中...」
- 沒講 keep-warm pool 是否做 — 沒做 = 每次 cold start 都 5-15s；做了 = 額外 idle baseline cost

**修正提案**

- **粒度三選一**（同 Q7，這裡先 framing）：
  - **方案 A：per-conversation（§1 原案）**
    - state 完全隔離；100 conv = 100 pod
    - 適合 trust model 最強（不同 conv 之間不能洩漏）
  - **方案 B：per-user（推薦預設）**
    - 1 user 1 pod，內部用 LangGraph `thread_id`（= `channel_id`）切多 conversation
    - 100 active conv 落在約 10-30 active user → 10-30 pod
    - state 仍隔離（LangGraph checkpoint 是 thread-namespace 的）
    - 跨 conv 共享 LangGraph runtime / model loading → 資源利用率高
    - 風險：同 user 的 conv 之間有 in-process side channel（同 python process），對「user 自己的不同 chat」這風險可接受
  - **方案 C：per-(user, agent_template)**
    - 介於 A/B 之間。1 user 用 3 個不同 agent_template = 3 pod
- **Cold start UX**：
  - 同步阻塞，超時 15s → 給「啟動中，請稍候並重整」訊息
  - 進階：first message 進入後立刻先在 SSE 推一個 `{type: 'status', stage: 'spawning_runtime'}` 事件 → frontend 顯示進度條 → pod ready 後再推 actual response
- **Keep-warm pool**：v1 **不做**（避免 idle baseline cost 翻倍），v2 評估
- **Quota 警戒**：在 paas-operator 新增 `GET /agent-runtimes/{workspace}/usage` endpoint，回 active pod 數 / 累計 PVC 用量 / 預估月 cost；Odoo 端在 dashboard 顯示「your AI runtime 用量」

**Open questions for leader**

- **[需 leader 拍板]** 粒度 A / B / C？（與 Q7 是同一個拍板，這裡並列方便比較）
- **[需 leader 拍板]** Cold start UX 走「同步阻塞 + 超時 fallback」還是「立刻回 spawning event + 真實回應後到」？

---

#### Q4：subprocess sandbox in pod — securityContext？跟 cloud_service 既有 chart 一致嗎？

**Finding**

- K8s pod 的 securityContext **自動繼承到所有 process（含 subprocess）** — 設在 pod-level 就涵蓋 LangGraph main + skill subprocess
- 既有 cloud_service helm install 大多用 Bitnami chart（odoo / postgres / n8n），這些 chart 預設用 fixed UID（1001 non-root）但 **不一定 enforce seccomp / readOnlyRootFilesystem / dropAllCapabilities**
- subprocess 在 pod 內物理共享：
  - 同 cgroup（CPU / memory limits）— skill 跑滿 CPU 會卡 LangGraph
  - 同 mount namespace（看到 /workspace、/skills、/tmp 一樣）
  - 同 network namespace（同一條 NetworkPolicy egress）
  - 同 PID namespace（互可見）
- subprocess 在 pod 內邏輯共享：
  - 同 python process 的 env vars（含 OPENAI_API_KEY、MCP api_key）
  - 同 /workspace PVC 內容（含 LangGraph thread checkpoint）

**Gap / Risk**

- §1「subprocess sandboxed by pod boundary」這句話**等於「subprocess 不會跨 pod 影響別人」，不等於「subprocess 不會在 pod 內亂搞」**
- 真正的攻擊面：
  - prompt injection → LLM 被誘導 call skill → skill 被「迫」做不該做的事
  - skill 可能：讀 env vars 拿 OPENAI key、讀 /workspace 拿其他 user/conv state、跑滿 CPU starve LangGraph main、走 NetworkPolicy 允許的 egress 把資料外送
- §1 沒講 trust model — skill 是 user-trusted 還是 marketplace third-party？兩種隔離強度差很多
- 「跟 cloud_service 既有 securityContext 一致」**是錯方向**：cloud_service 用 Bitnami chart 的鬆 baseline 是因為要相容多種 application；agent-runtime 是自家 image，可以也應該設 stricter baseline

**修正提案**

- **明確 trust model**（v1 收斂）：
  - **v1：所有 skill = user-trusted**（user 自己上傳的 skill bundle，user 對自己 workspace 內的洩漏負責）
  - **v2：marketplace skill** 再評估強隔離（gVisor / Kata containers / 子 pod）
- **pod-level securityContext baseline**（**比 cloud_service 嚴格**）：
  ```yaml
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  containers:
  - securityContext:
      allowPrivilegeEscalation: false
      readOnlyRootFilesystem: true
      capabilities:
        drop: ["ALL"]
  ```
- **filesystem 隔離**：
  - rootfs read-only（image 內容不改）
  - `/tmp` 用 emptyDir tmpfs（subprocess 暫存）
  - `/workspace` mount PVC，subdir 按 conv（per-conv pod）或按 thread（per-user pod）
  - `/skills` mount emptyDir，lazy fetch + cache
- **subprocess spawn 約束**（runtime 層，非 pod 層）：
  - 用 `subprocess.run(..., env={安全白名單}, ...)` **不 pass-through 敏感 env vars**
  - 用 `resource.setrlimit` 在 spawn 前限 CPU time / memory / file count
  - 用 `subprocess.run(timeout=N)` 預設超時
  - skill 拿 MCP / OpenAI 等資源**不直接讀 env**，而是透過 LangGraph 提供的 tool API 間接調用（API 層做 auth + scope check）
- **NetworkPolicy egress**（pod-level，subprocess 繼承）：
  - allow：DNS（53）、cluster MCP namespace、OpenAI/AI provider 白名單 IP/domain（CIDR or FQDN egress）
  - 其餘 default deny（含 RFC1918 與其它 internal services）
  - 共用：skill subprocess 與 MCP egress 同一條規則（差別只在 LangGraph 邏輯層誰可調用哪 tool）

**Open questions for leader**

- **[需 leader 拍板]** v1 trust model 收斂到「skill = user-trusted」是否接受？這意味著「v1 不開放 marketplace skill」— 或者要從 day 1 就為 marketplace 預留隔離 hook？

---

#### Q5：MCP endpoint binding — pod-readable 改造範圍？sidecar discovery？

**Finding**

- 既有 `mcp_server` model 有 **D4 invariant（不可動）**：
  - D4：`scope='user'` iff `project_id IS NOT NULL`
  - ~~D5：user-scope MCP 必須掛在綁 `cloud_service` 的 project~~ **已被 refactor branch (cycle 4 / `mcp-url-redesign`) 整段刪除**；unbound project + user-scope MCP 是合法狀態
  - **user-scope MCP 是 project 一等公民**（owned by project，不再依附 cloud_service）；`sidecar_backed` boolean flag 區分物理是否依賴 backing service sidecar（**實作細節**，schema 不變）
- 既有 cleanup ordering（`cloud_service._do_cleanup`，refactor branch cycle 4 已落地）：unlink `sidecar_backed=True` user-scope MCP → helm uninstall → state='deleting'；external MCP (`sidecar_backed=False`) 不動
- `_get_mcp_client_config()` 已是純 dict 介面（`{transport, url, headers}`），完全 portable 給 pod 用
- MCP URL 是 cluster-internal DNS（`http://release-mcp.{ns}.svc.cluster.local:port/path`） — agent pod 在 K8s 內可直達，但**跨 namespace 需要 NetworkPolicy egress 允許**

**Gap / Risk**

- §1「MCP endpoint binding 改成 pod-readable」沒明確說 pod 怎麼讀。三個常見路徑各有問題：
  - **(a) Pod 透過 paas-operator 或 Odoo API 即時查 mcp_server**：每次 tool call 多一跳 5-50ms latency；Odoo 變成 SPOF；負載放大
  - **(b) Pod 啟動時把 mcp config snapshot 注入 env / ConfigMap**：靜態，conv 進行中 mcp_server 改動不反映；最致命：cloud_service delete → mcp sidecar 拆 → agent pod cache 還有舊 URL → tool call 404 / 連線錯誤（破壞既有 cleanup ordering 的保護意圖）
  - **(c) Pod 訂閱 Odoo bus 變更 event**：bus 是 Odoo 內部 channel，跨 process 不適用
- §1 沒講 NetworkPolicy 跨 namespace egress 開法
- §1 沒講「在 cloud_service cleanup 時，已有的 agent pod 怎麼辦」（既有 cleanup 只關 sidecar，不關 agent pod — 因為 agent pod 還不存在）

**修正提案**

- **採 (b) 改良版：snapshot + 顯式 invalidation**
  - **Pod 啟動時**：Odoo 把當前 user/workspace 可用的 MCP servers 序列化成 JSON，透過 paas-operator 注入 pod 為 ConfigMap → mount 到 `/etc/agent-runtime/mcp.json`
  - **Pod runtime**：啟動讀一次到 memory，後續不再 query Odoo
  - **失效機制**：當 `cloud_service._do_cleanup` 或 `mcp_server` 變更時：
    - Odoo 透過 paas-operator 新 endpoint `POST /agent-runtimes/{workspace}/invalidate-mcp` 通知該 workspace 所有 agent pod
    - 收到 invalidate 的 pod 把對應 MCP server 從 in-memory list 拿掉（不重啟），同時把後續對該 server 的 tool call 改 reject
    - 用戶下次 first message 啟新 thread / 新 pod 時拿到 fresh snapshot
  - **更激進選項**：cleanup 時 paas-operator 直接 `kubectl delete pod` 對應 workspace 的 agent pod（強迫 cold start）— 簡單但 UX 較 disruptive
- **不動 `mcp_server` schema**（保持 D4 invariant；D5 已被 refactor branch 拔，本 epic 不重新引入）
- **NetworkPolicy 跨 namespace egress**：
  - agent-runtime 的 egress allowlist 用 `namespaceSelector + matchLabels { woow.tw/workspace-id: <id> }` 限定到自己 workspace 的 cloud_service namespace
  - 在 `create_namespace` 時自動寫入 label，agent-runtime helm chart 用 helm template 渲染 namespaceSelector
- **不需要 sidecar discovery**：MCP URL 從 Odoo 推（snapshot），不是 pod 主動發現

**Open questions for leader**

- **[需 leader 拍板]** Invalidation 走 「軟通知 + drop from memory」 還是 「硬重啟 pod」？前者實作較複雜（Pod 端要實作 invalidation API），後者一行 `kubectl delete pod` 但中斷正在進行的 conversation。
- **[需 leader 拍板]** 新增 paas-operator endpoint `POST /agent-runtimes/{workspace}/invalidate-mcp` 與 `DELETE /agent-runtimes/{workspace_id}` — task brief 寫「不要擅自擴大 paas-operator refactor」，這算合理擴展嗎？

---

#### Q6：LangGraph 程式碼搬家切割線？

**Finding**

- 目前 LangGraph / LangChain 相關程式碼分布在 Odoo addon 三個地方：
  - `src/models/ai_client.py:231-409` — `_build_agent_graph` / `_async_agent_invoke` / `_async_agent_stream`（純 LangGraph runtime）
  - `src/models/ai_client.py:96-126` — `AIClient.from_assistant`（Odoo recordset 取 config）
  - `src/models/ai_client.py:456-489` — `_build_mcp_server_config` / `_build_prefixed_enabled_names`（Odoo recordset → MCP config）
  - `src/models/discuss_channel.py:55-258` — `message_post` override + `_post_ai_reply` 同步 in-process LangChain call
  - `src/controllers/ai_assistant.py:353-544` — SSE generator 內 `asyncio.run` LangGraph
- 一個 single python module `ai_client.py` 同時混了「純 runtime」「LangChain client wrapper」「Odoo ORM integration」三層

**Gap / Risk**

- §1「LangGraph 從 Odoo addon 搬到獨立 repo」沒講搬完後 Odoo 剩什麼、新 repo 拿什麼
- 沒講 `discuss_channel.message_post` override 的同步 LangChain 路徑要不要保留
- 沒講 Odoo 端要不要清掉對 `langchain_core` / `langgraph` 的依賴
- 沒給新 repo 名稱 / 形狀（init-agent-4 寫不要擅自跨 repo 建 code，新 repo 形狀要 leader 確認）

**修正提案**

- **切割線**：以「需不需要 Odoo ORM」為準
  - **搬到新 repo（agent-runtime image source）**：
    - LangGraph runtime（StateGraph、ToolNode、tools_condition、`_build_agent_graph`）
    - LangChain client wrapper（chat_completion、stream、`_translate_exception`）
    - MCP client integration（`langchain_mcp_adapters` 直接使用）
    - Skill executor（in-process subprocess + sandbox 約束）
    - Transport layer（FastAPI WS server + HTTP fallback）
  - **留在 Odoo addon**：
    - `ai.assistant` / `ai.config` / `mcp_server` / `mcp_tool` model（schema 不動）
    - **新增** `_to_runtime_config()` method 於 `ai.assistant` / `ai.config` / `mcp_server`（純 serializer，把 ORM record 變成 dict）
    - Odoo 端的 `AIClient.from_assistant` 簡化為「丟 config dict 給 paas-operator → 起 agent pod」
    - `discuss_channel.message_post` override 的同步 LangChain 分支 **刪掉**（被 §1 sign-off 的「Odoo 退化為 metadata + gateway」蘊含）
    - `controllers/ai_assistant.py` SSE generator **改寫**為「open WS to agent pod, forward chunks as SSE events」
- **依賴清理**：
  - Odoo addon `__manifest__.py` 不再間接依賴 `langchain_core` / `langgraph` / `langchain_mcp_adapters`
  - **但短期保留**：`langchain_mcp_adapters` Odoo 端仍需要（給 `mcp_server.action_sync_tools` 用 — 拉 tool schema 進 Odoo metadata）
  - 完整切除 langchain Odoo 依賴需等到 `mcp_server` tool discovery 改成 agent-runtime 一側做
- **新 repo 名稱建議**：`agent-runtime`（與 task brief §1 第 5 點「新增 helm chart `agent-runtime`」對齊；與 init-agent-4 「woow-paas-*」家族並列為新成員）

**Open questions for leader**

- **[需 leader 拍板]** 新 repo 名稱（推薦 `agent-runtime`，是否同意？）。命名要對齊 init-agent-4 表中的「woow-paas-cd / woow-paas-charts / woow-paas-platform / woow-paas-ci」家族嗎？若是，建議改 `woow-paas-agent-runtime`
- **[需 leader 拍板]** 新 repo 是否走「Gitea + CI auto-build image + helm chart 同 repo」pattern（仿 paas-odoo-ci）？還是 image build / chart 分 repo？
- **[需 leader 拍板]** `discuss_channel.message_post` 同步 LangChain 分支刪除是不可逆動作（Odoo std field 不改但 override behavior 移除），確認可以執行？

---

#### Q7：conversation scope 的具體粒度？

**Finding**

- 既有資料結構：`discuss.channel` ↔ `project.task` (1:1 via `task.channel_id`) ↔ `project.project` ↔ `cloud_service`
- MCP server 在 project 層（user-scope）；ai.assistant 是 global / system 層
- 一個 user 可以在自己 workspace 內開多個 task，每個 task 配一個 channel → 多個 conversation
- LangGraph 內建 `thread_id` 概念，可以一個 graph instance 跑多 thread（thread = isolated state，checkpoint 分 namespace）

**Gap / Risk**

- 「per conversation」字面 = per channel，但 user 多 channel = pod 數爆炸（同 Q3）
- §1 沒明確「resume 重起 restore」的 state 範圍（thread-level？session-level？）
- 沒講「同 user 跨 conv 共享 LangGraph memory」是 feature 還是 anti-feature

**修正提案**

- **粒度三選一**（與 Q3 同一個拍板）：

  | 方案 | Pod scope | LangGraph thread | PVC layout | Pod 數（100 active conv 估） | Trust 隔離強度 |
  |------|----------|------------------|-----------|---------------|----------------|
  | **A** | per-conversation | 1 graph 1 thread | `/workspace/conv-{channel_id}/` | 100 | 最強（不同 conv 物理分離） |
  | **B**（推薦） | per-user | 1 graph N thread | `/workspace/user-{uid}/threads/{channel_id}/` | 10-30 | 強（不同 user 物理分離；同 user 不同 conv in-process 邏輯分離） |
  | **C** | per-(user, agent_template) | 1 graph N thread | `/workspace/user-{uid}/agent-{aid}/threads/{channel_id}/` | 30-60 | 強+（同 user 不同 agent 物理分離） |

- **推薦方案 B**：
  - 對應 LangGraph thread 概念，PVC 利用率最高
  - `channel_id` 直接 1:1 對應 LangGraph `thread_id`
  - Resume 邏輯簡單：pod 啟動 → load checkpoint by thread_id → 繼續
  - Cold start 攤提：第 N+1 個 conv 不需要 cold start，因為 user 的 pod 已 alive
  - 既有 ACL（workspace_access）天然對齊 — 一個 pod 服務一個 user
- 方案 B 的代價：
  - 同一 user 同時開 5 個 chat 並行進行（罕見但會發生）需要 LangGraph 支援多 thread concurrent execution（langgraph 本身支援，但要驗 langchain-mcp-adapters 是否 thread-safe）
  - In-process side channel：user 自己的兩個 chat 之間理論上可被同 process 內的 LangGraph 邏輯互相窺視 — 對「同 user 之間」這風險可接受
- 若 leader 要 trust model 最強：選 A（per-conversation）但接受 Q3 的 100 pod 容量壓力
- 若 leader 要「跨 conv 共享 LangGraph long-term memory」：選 B 並設計 `/workspace/user-{uid}/shared/` subdir

**Open questions for leader**

- **[需 leader 拍板]** A / B / C 哪個？（與 Q3 是同一個拍板）
- **[需 leader 拍板]** 是否需要「跨 conv 共享 long-term memory」這個 feature？若需要，選 B + shared subdir；若不需要，A/B/C 皆可。

---

### §1.2 §1 修正後的架構（sign-off 版 — per-project pivot）

> ✓ **§1.2 sign-off 2026-05-21**（第二輪 pivot）。粒度從前一輪 sign-off 的 **per-user** 改為 **per-project**，理由：使用者要求 project 升格 first-class entity + 1..N cloud_service multi-workspace binding。其它兩條 (trust model / repo split) 維持不變。
>
> ⚠️ **§1.3 預告**：本 pivot 蘊含 project model 變動（first-class entity + multi cloud_service binding + project_access model），相關紅線確認見 §1.3 與本 phase 末段 escalation。

**已 sign-off 的主決定（§1.2 — 第二輪）**：

| 決定 | 結果 | 來源 |
|------|------|------|
| **Pod scope 粒度** | **per-project**。1 project 1 pod，pod 服務同 project 內所有 user / 所有 conversation；內部 LangGraph `thread_id = channel_id` 分隔多 conversation；state PVC 用 `/workspace/project-{project_id}/threads/{channel_id}/`。**取代之前 sign-off 的 per-user 方案。** | leader 2026-05-21 pivot |
| **v1 Trust model** | **user-trusted**。所有 v1 skill 視為 user 自上傳，pod-level securityContext + subprocess rlimit 為足夠隔離。Marketplace skill 留 v2 評估強隔離（gVisor / Kata）。**Project 內多 user 共享同 pod 的 trust 邊界**：同 project member 視為互相信任（同 project_access）；跨 project 物理隔離 | §1.1 Q4 推薦（per-project 蘊含 same-project member co-trust） |
| **新 repo 拆分** | **image build repo**：新建 `woow-paas-agent-runtime`（與既有 `woow-paas-platform` / `woow-paas-ci` 家族對齊命名）。**Helm chart**：寫入既有 `woow-paas-charts` repo（與 cd / charts 家族對齊）— **跨 repo 版本同步是 trade-off，使用者已 accept** | §1.1 Q6 推薦修正 |

**§2~§7 待拍板項目**（不在 §1.2 sign-off 範圍）：

- §2 unlock：Reap 策略 ✓ (signed-off 2026-05-21 — scale-to-0 + 長 TTL helm uninstall)、StorageClass ✓ (signed-off — longhorn-delete)、Namespace 模型（per-project pivot 後重寫 §2.7，命名 `paas-pa-{project_hash}`）
- §1.3 unlock（**新增**）：Project first-class model（project ACL via project_access、project ↔ cloud_service M:N、跨 workspace cloud_service binding）— 詳見下節
- §4 unlock：agent_template 怎麼 bind 到 project、project member 怎麼 invoke agent（admin/user 權限差別）、per-project agent quota override
- §5 unlock：frontend SSE 保留策略、WS protocol schema、Odoo controller WS client 實作形狀
- §6 unlock：Cold start UX、subprocess 額外限制細節
- §7 unlock：跨 repo CI 互相觸發策略、`discuss_channel.message_post` 同步分支刪除時機、Odoo `langchain_*` 依賴清理、**project schema migration（cloud_service_id Many2one → Many2many）**

**修正後架構（依 §1.2 第二輪 sign-off 決定畫定 — per-project pivot）**：

```
Browser
  │  EventSource (SSE) ── 不變
  ▼
Odoo controller (/api/ai/stream/<channel_id>)
  │  - 解 user identity + channel → project_id
  │  - project_access ACL gate (member?)
  │  - 序列化 ai.assistant + ai.config + project.mcp_server_ids → runtime_config dict
  │  - compute project_namespace_hash via hmac_sha256(project_id, secret)
  │  - 呼叫 paas-operator POST /agent-runtimes/spawn-or-attach
  │  - 收 pod WS URL，open outbound WS
  │  ↕  inbound SSE chunks ← outbound WS messages
  │
  ▼ (新增) Outbound WS client
Agent Pod (per project, long-lived; ns=paas-ws-pa-{hash}, release=agent-pa-{hash})
  │  - FastAPI WebSocket server (/v1/chat)
  │  - LangGraph runtime（StateGraph + ToolNode）
  │  - thread_id = channel_id → checkpoint isolation
  │  - Skill executor (subprocess + rlimit + env whitelist)
  │  - MCP client (langchain-mcp-adapters), config 從 /etc/agent-runtime/mcp.json
  │  - /workspace PVC (per-project) /workspace/project-{pid}/threads/{channel_id}/
  │  - /workspace/project-{pid}/shared/ — 跨 thread 共享 memory（§3/§4 細化）
  │  - securityContext: non-root + readOnlyRootFs + dropAllCaps + seccompRuntimeDefault
  │  - NetworkPolicy: egress = DNS + (project.workspace_ids 集合的所有 cloud_service ns) + AI provider whitelist
  │
  ↓ egress (whitelist, per project.workspace_ids)
MCP Servers (project 一等公民)
  ├ sidecar-backed (sidecar_backed=True)：物理位於 project.cloud_service_ids 內 cloud_service 的 sidecar pod（跨 workspace）
  └ external (sidecar_backed=False)：任意 URL（cluster.local 內外皆可）
  schema 不動（refactor branch 已 finalized）
AI Provider (OpenAI-compatible)
```

新增的 paas-operator endpoints（在 task brief constraint 內，task brief 明確允許「新增 agent-runtime helm chart」蘊含 operator 配合；hash-based URL keys 避免 project_id 序號洩漏）：

- `POST /agent-runtimes/spawn-or-attach` — 起 pod 或附掛到既有 pod（idempotent，per-project；body 含 `project_hash` + `project_workspace_ids`）
- `POST /agent-runtimes/{project_hash}/invalidate-mcp` — 通知 project pod refresh MCP snapshot
- `POST /agent-runtimes/{project_hash}/reap` — scale to 0（被 CronJob 呼叫）
- `POST /agent-runtimes/{project_hash}/resume` — scale to 1（被 spawn-or-attach 在 pod=0 時呼叫；通常 spawn-or-attach 直接內含此邏輯）
- `POST /agent-runtimes/{project_hash}/sync-workspaces` — body `{project_workspace_ids}` → helm upgrade 重渲染 NetworkPolicy egress（project.cloud_service_ids 變動時觸發）
- `DELETE /agent-runtimes/{project_hash}` — helm uninstall（長 TTL GC 用）
- `GET /agent-runtimes/{project_hash}/status` — pod state / last_active

新增 Odoo models（task brief §1 第 6 點原本列了 3 個，per-project pivot 後 +1）：

- `woow_paas_platform.agent_template` — agent 設定模板（取代部份 ai.assistant 的角色，待 §4 細化）
- `woow_paas_platform.agent_session` — **per-project** pod lifecycle 紀錄（含 `project_id` + `project_namespace_hash` + state + timestamps）— 不存 message history
- `woow_paas_platform.skill` — skill bundle metadata（uri / version / hash / sandbox_profile / belongs_to_template）— 待 §3 細化
- `woow_paas_platform.project_access` — **per-project first-class ACL**（仿 workspace_access；admin / user / guest role）— §1.3 + §4 細化

本 epic 動的部份（per-project pivot 蘊含）：

- `project.project` schema：`cloud_service_id` (M2O) → `cloud_service_ids` (M2M)、`workspace_id` (related) → `workspace_ids` (computed M2M) — **詳見 §1.3 + §7**
- `project.task` schema：`primary_cloud_service_id` / `extra_cloud_service_ids` 連鎖修改 — **詳見 §7**
- `ir.rule` `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` dual-leg domain：walk path 從 M2O 改 M2M（Inv #11 守線 — 不動 dual-leg shape / 外層 AND `scope='user'`）

不動的部份（已 sign-off / 列為 constraint / refactor branch 已 finalized）：

- `mcp_server` / `mcp_tool` schema（D4 invariant 守；D5 已被 refactor branch 拔；本 epic 不重新引入）
- `mcp_server.sidecar_backed` flag 與相關 Inv #9 / #10（refactor branch 已落地）
- 6-rule ACL split 的整體 shape 與 Inv #11 外層 AND `scope='user'` 防線（dual-leg domain 內 walk path 改 M2M 是 explicit allowed by Inv #11「filter 條件擴維度」）
- `cloud_service` 1:N cleanup ordering（D1，refactor branch 已落地）— 本 epic 延伸到 M:N 的語意調整見 §7
- `discuss.channel` / `mail.message` Odoo std model
- Odoo `base.group_user` 對 std model 的 ACL（[[odoo-addon-std-rule-symmetric-deactivation]] pattern）
- Browser SSE EventSource transport（frontend 零變更）

### §1.2 跨 repo coordination note（§7 才展開細節）

> 本 note 為 §1.2 確認版的衍生 caveat，**細節留 §7 migration path 處理**，§1 不展開。

由於 §1.2 sign-off 採「image build repo 與 helm chart repo 拆開」：

- **`woow-paas-agent-runtime`**（新建）：image source（Dockerfile / LangGraph runtime / FastAPI WS server / skill executor / requirements）+ CI build & push image
- **`woow-paas-charts`**（既有）：新增 `charts/agent-runtime/` helm chart（template + values）

兩 repo 各自有 CI / 各自 tag — **chart 引用的 image tag 與 image source 的 tag 必須協調一致**，否則：

- chart 升版指向不存在的 image tag → 部署失敗
- image 升版但 chart 未跟上 → 新 feature / breaking change 沒被部署觸發

可選策略（§7 拍板）：

- **(a) 統一版本號**：兩 repo 共用 semver，agent-runtime 升 image 時 同步 PR chart 升版（手動 / bot 兩種）
- **(b) Cross-repo CI trigger**：仿既有 `woow_paas_platform → paas-odoo-ci` 的 Gitea API dispatch pattern — agent-runtime push tag → 觸發 charts CI 自動 bump image tag
- **(c) Chart 用浮動 tag**（如 `latest` / `main`）— **不推薦**，因 helm rollback / 版本追蹤會失準

phase-planner 推薦（§7 才拍板）：(b) cross-repo trigger，與既有 paas-odoo-ci pattern 對齊。

額外要 §7 處理：

- 兩 repo 建立時的 secret / token 設定（仿 `woow_paas_platform` 對 `paas-odoo-ci` 的 `CI_GITEA_TOKEN`）
- 兩 repo 與 `woow-paas-cd` 的 Rancher Fleet bundle 對接（chart 升版觸發 fleet 同步 dev / stg / prod）

### §1.3 Project First-class Model（per-project pivot 蘊含的 project 升格）

> 本節是 §1.2 per-project pivot 蘊含的 model 變動 narrative。**implementation 細節留 §4（agent template binding）+ §7（migration phase）展開**；本節定 scope / 引用 refactor branch 已 fix 範圍 / 本 epic 延伸範圍。

#### §1.3.1 Project 升格的 schema 衝擊

per-project pod 蘊含 project 必須是 first-class entity（與 workspace 同級）：

| 既有（refactor branch 已 finalize 2026-05-21） | 本 epic 延伸（M:N + first-class） |
|---|---|
| `project.cloud_service_id = Many2one(..., ondelete='set null')` + SQL `UNIQUE(cloud_service_id)`（1:1） | **改 Many2many**：`project.cloud_service_ids = Many2many('woow_paas_platform.cloud_service', relation='project_cloud_service_rel')`；刪 `UNIQUE` constraint；新 through table |
| `cloud_service.project_ids = One2many('project.project', 'cloud_service_id')`（1:N） | **改 Many2many 反向**：`cloud_service.project_ids = Many2many(...)` 走同一個 relation table（雙向 M2M）|
| `project.workspace_id = related('cloud_service_id.workspace_id', store=True)`（單 workspace） | **改 computed Many2many**：`project.workspace_ids = fields.Many2many('woow_paas_platform.workspace', compute='_compute_workspace_ids', store=True)`，aggregate from `cloud_service_ids.workspace_id` |
| `task.primary_cloud_service_id = related('project_id.cloud_service_id')` | **重新定義**：保留 `primary_cloud_service_id`，從 `related` 改成獨立 `Many2one`（user 在 multi-binding project 內手動選 primary）；或計算 first by order — §7 拍板 |
| `task.extra_cloud_service_ids` 的 `domain="[('workspace_id', '=', workspace_id)]"` same-workspace 限制 | **拆掉**（project 跨 workspace 後 task 不再受同 workspace 限制）；改成 `domain="[('id', 'in', parent.project_id.cloud_service_ids.ids)]"` 或類似 |
| `project` 自身無 ACL field — 沿用「透過 cloud_service → workspace_id → workspace_access」推導 | **新增 model `woow_paas_platform.project_access`**（仿 `workspace_access`）：`project_id` + `user_id` + `role`（admin / user / guest），承載 first-class ACL |

**對 mcp_server D4 invariant**：**不變**（`scope='user' iff project_id IS NOT NULL`）— project schema 改 M:N 不影響 D4，D4 只 check project_id 存不存在，不關心 project 綁幾個 cloud_service。

#### §1.3.2 ACL 兩層：workspace_access vs project_access

新 ACL model 與既有並存：

```
res.users
   │
   ├── workspace_access (workspace_id, user_id, role)
   │     └── 沿用既有 — workspace member 權限 / 計費 / quota / cloud_service 操作
   │
   └── project_access (project_id, user_id, role)  [本 epic 新增]
         └── project member 權限 / agent runtime invoke / agent template binding
```

**兩層 ACL 的關係**（本節定 baseline，細節 §4 細化）：

- workspace_access **不蘊含** project_access（在 first-class model 下，project 是獨立邊界）
- project_access **不蘊含** workspace_access（project 可跨 workspace；project member 可能不是任一綁定 workspace 的 member）
- agent pod **物理邊界 = project**：pod 服務 project 內 user，跨 project 物理隔離
- agent pod **能訪問的 MCP**：透過該 project 的 `mcp_server_ids`（project 一等公民集合）→ 含兩種 row：
  - **sidecar-backed** (`sidecar_backed=True`)：URL 指向 project.cloud_service_ids 內某 cloud_service sidecar；NetworkPolicy egress 須開到該 cloud_service 的 namespace
  - **external** (`sidecar_backed=False`)：URL 指向任意（cluster.local 內外）；NetworkPolicy egress 須允許對應目標
- agent pod **NetworkPolicy egress**：(a) 允許 egress 到該 project `cloud_service_ids` 的所有 workspace namespace（給 sidecar-backed MCP 用，K8s `namespaceSelector` 用 `workspace_id` label match）；(b) 允許 egress 到 external MCP 白名單目標（給 external MCP 用，§6 細節）

##### §1.3.2 — ACL Invariants（本 epic 引入）

> 編號 `Inv A1`、`Inv A2` ...（`A` = agent-runtime epic）獨立於 mcp-* doc 的 Inv #1~#11，避免與 refactor branch invariants 混淆。

- **Inv A1 — agent_template cross-project write 必須 grantor 雙重驗（ir.rule 'any' + Python @api.constrains）**：M2M `project_ids` 上 ir.rule 對 write 動作評估「任一現存 element 滿足 user domain」即放行（Odoo ORM 內建 M2M write 行為），attacker 可透過 `write({'project_ids': [(4, victim)]})` 由「自己有 admin/owner 的 A」誘騙 ir.rule pass 後實際把 template 綁到 victim B → cross-project leak。**ir.rule 不夠**，必須加 `agent_template._check_project_ids_grantor_authority` Python constraint：每個 new `project_id` element 都驗 `env.user` 在該 project 有 admin/owner role；SUPERUSER + `base.group_system` 直接 bypass（admin / migration / cron 合法跨 project 寫入路徑保留）。違反 invariant 行為：把 constraint 拔掉 / 改 sudo() bypass / 漏 group_system 細粒度 check — 重炸 HC-3 (PR #54 HC-2 同 family — `constraint-input-trust-expansion` pattern)。

  此 pattern 已抽到 `docs/decisions/patterns/constraint-input-trust-expansion.md`（本 epic Phase A 補 `@api.constrains` 作為 first instance；PR #54 HC-2 `_resolve_project_access` 對 unbound non-follower 收緊是同 family 的第二個 instance）。

- **Inv A2 — skill bundle attachment 必須 defense in depth 兩層（anchor stamping + `ir.attachment` row-level ir.rule）**：`skill.bundle_attachment_id` 是 `groups='base.group_system'` field-level mask（B-1.SK-6 path a），但 field-level mask **只擋 ORM read 該 field**；attacker (base.group_user + project_access role=user) 仍可 enumerate `ir.attachment.id` 整數，透過 Odoo std `/web/content/<id>?download=1` HTTP endpoint 直接下載 — endpoint 走 `ir.attachment._check` Python delegation，對 `res_model='woow_paas_platform.skill' + res_id=skill_id` attachment 回頭評估 skill model ACL（user 對 skill `skill_rule_project_user` perm_read=True allow）→ delegation 通過 → bundle bytes 串流 → **subprocess 執行 attacker reverse-engineered payload → RCE on agent pod**。

  **Defense in depth 兩層**必須同時存在：
  - **Layer 1 — Anchor stamping**：`skill.create()` / `skill.write({'bundle_attachment_id': ...})` 兩 path 都呼叫 `_anchor_bundle_attachment`，強制 attachment 三件套 stamping（`res_model='woow_paas_platform.skill'` + `res_id=skill.id` + `public=False`）— ensure delegation target correct + 不旁路 `/web/image` 等 public endpoint
  - **Layer 2 — `ir.attachment` row-level ir.rule**：兩條一組 — `ir_attachment_rule_skill_bundle_user_scope` (base.group_user) domain `[('res_model', '!=', 'woow_paas_platform.skill')]` R/W/C/U=True（對 skill bundle attachment row-level deny，對既有 non-skill attachment 不變動）+ `ir_attachment_rule_skill_bundle_admin_full` (base.group_system) domain `[(1, '=', 1)]` R/W/C/U=True（per-group OR semantics admin bypass，對齊 [[cross-workspace-task-data-leak]] B1 root cause「第一次加 base.group_user ir.rule 對等加 group_system admin_full bypass」）

  違反 invariant 行為：(1) 只用 field-level group mask 沒 row-level ir.rule — `/web/content` enumeration bypass；(2) 只用 ir.rule 沒 anchor stamping — attachment `res_model` 未對 → Layer 2a domain 對 non-skill 值 True → bypass；(3) 漏 admin_full group_system bypass — 對 base.group_user 第一次加 ir.attachment ir.rule 與 [[cross-workspace-task-data-leak]] B1 root cause 同源，重蹈 admin 後台 form 進不去 silent functional regression；(4) Layer 2 `perm_read/write/create/unlink` 沒全 True — 對 base.group_user 既有 non-skill attachment 操作收緊 silent regression；(5) anchor create override 漏 write override — admin re-assign attachment via write 漏 anchor → stamp 不對 → Layer 2 失效。

  此 pattern 已抽到 `docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md`；對 Odoo 18 base + bundled addons 必須 grep verify **0 std ir.rule on ir.attachment for base.group_user**（[[odoo-addon-std-rule-symmetric-deactivation]] 紅線 — Phase A CAQA round 3 §14.4 已 independent grep verified）才能加 Layer 2 不撞 OR-bypass 災情；本 epic Phase A 補的 2 條 `ir_attachment_rule_skill_bundle_*` 是該 pattern 的 first instance。

> **Inv A1 + Inv A2 合起來對應**：CAQA report `docs/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md` §14.7 invariant verify table 中 **「Phase A #8 (HC-3 grantor authority + HC-4 attachment row-level)」**新加 invariant — 三件套（(a) agent_template grantor authority constraint + (b) skill bundle attachment row-level deny + (c) anchor stamping ensure delegation target correct）；reporter phase 把 (a) 落在 Inv A1、(b)+(c) 落在 Inv A2，便於 future maintainer 兩條獨立查。

#### §1.3.3 對 refactor branch 已 fix 範圍的明確引用

refactor branch `feature/refactor-user-scope-mcp`（commit a66ffe6）已落地下列變更（本 epic 直接繼承，**不重做**）：

| 範圍 | refactor branch 已做 | 本 epic 範圍 |
|------|--------------------|-------------|
| **D5 invariant** | 整段刪除（`_check_user_scope_requires_bound_project` 拔）；unbound project + user-scope MCP 是合法狀態 | 不動 — 本 epic 繼承 |
| **`sidecar_backed` boolean field** | 新增於 `mcp_server`；標 row 物理是否依賴 backing service sidecar | 不動 — 本 epic 繼承 |
| **6-rule ACL split (dual-leg variant)** | `mcp_server_rule_*` + `mcp_tool_rule_*` × 3 對 = 6 條 rule；`workspace_user` × 2 是 dual-leg（bound walk OR unbound+follower carve-out） | **延伸**：dual-leg domain 內 leg 1 從 `project_id.cloud_service_id.workspace_id.access_ids.user_id` (M2O walk) 改 `project_id.cloud_service_ids.workspace_id.access_ids.user_id` (M2M walk)；leg 2 carve-out 從 `('project_id.cloud_service_id', '=', False)` 改成 `('project_id.cloud_service_ids', '=', False)` — **本 epic migration 必動**，但 invariant Inv #11 字面（dual-leg + 外層 AND `scope='user'`）守不變 |
| **`_do_cleanup` filter** | `lambda s: s.scope == 'user' and s.sidecar_backed`；只清 sidecar-backed user-scope MCP | **延伸**：1:N → M:N 後 `self.project_ids` 仍工作（M2M reverse 也走 mapped）；filter 條件不變；但 **cleanup 語意要重審** — M:N 下 cloud_service unlink 時不該整個 project 連坐 unlink（其他 cloud_service 還在用），只清 **「URL 對應該 cloud_service sidecar」的 user-scope MCP** — 詳見 §7 prerequisite |
| **`auto_created` field** | deprecated（仍存在 + 標 deprecation note）；M1 cron 仍走舊 query | 不動 — 本 epic 繼承 |
| **新 invariants Inv #9 / #10 / #11** | 全部 final，canonical home 在 `docs/decisions/refactor/mcp-url-redesign.md` | 不動 — 本 epic 繼承；ACL domain 改 M:N walk 在 Inv #11 「禁止動作」明文允許範圍（filter 條件擴維度，不破 dual-leg shape） |
| **`mcp_server.url` snapshot** | snapshot 性質不變；演算法 `_model_build_mcp_endpoint_url` 留 cloud_service.py | 不動 — 本 epic 繼承 |

**本 epic 在 refactor branch 1:N 基礎上延伸到 M:N 的清單**（§7 落地）：

- `project.cloud_service_id` (M2O) → `cloud_service_ids` (M2M) + 刪 `UNIQUE` constraint + through table migration
- `cloud_service.project_ids` 走 M2M reverse（同一 relation table）
- `project.workspace_id` (related M2O) → `workspace_ids` (computed Many2many)
- `task.primary_cloud_service_id` / `task.extra_cloud_service_ids` 連鎖（§7 拍板細節）
- 6-rule ACL `workspace_user` × 2 dual-leg domain 內 M2O walk → M2M walk（保留外層 AND `scope='user'` 鎖 MAJOR 1 防線）
- `_do_cleanup` / `unlink()` 語意從 1:N（cloud_service teardown → 該 cloud_service 對應的 user-scope MCP）延伸到 M:N（只清 URL 對應該 cloud_service sidecar 的 user-scope MCP，其他綁同 project 但 URL 對應別 cloud_service 的 MCP 保留）— 細節 §7

#### §1.3.4 mcp_server D4 invariant 在 per-project pivot 下的守線確認

> task brief constraint：「不要動 `mcp_server` 既有 schema（除非 §1 review 認定必須，需 leader 確認）」

**確認**：本 epic **不動 mcp_server schema**，亦不破壞 D4 invariant：

| 檢查 | 結論 |
|------|------|
| `mcp_server.project_id` 欄位本身 | 不動（仍 `Many2one('project.project', ondelete='cascade')`）|
| `mcp_server.scope` 欄位 | 不動 |
| D4 (`scope='user' ⇔ project_id IS NOT NULL`) | **不變**（與 project 綁幾個 cloud_service 無關）|
| `_check_scope_project_consistency` constraint method body | 不動 |
| `_check_user_scope_requires_bound_project` (D5) | **已被 refactor branch 整段刪**（cycle 4 finalized）；本 epic 不重新引入 |
| 6-rule ACL count parity（mcp_server ↔ mcp_tool 對等） | 守不變 |
| dual-leg ACL shape Inv #11 | 守不變（外層 AND `scope='user'`、leg 1 bound walk、leg 2 unbound carve-out 結構不變；只 walk path 內的 cloud_service field cardinality 改 M2M）|

**結論**：本 epic 對 mcp_server 的影響限於「ir.rule domain 內 walk path 從 M2O 改 M2M」+「`_do_cleanup` filter 語意從 1:N 延伸到 M:N」— 屬於 schema-adjacent 的 walk-path adjustment + cleanup-semantic 延伸，**不算動 mcp_server schema**（無欄位變動 / 無 constraint method body 變動）。

---

## §2 Pod lifecycle (cold start / idle reap / resume / quota)

> **Status**：Phase 1 — §2 rework after per-project pivot, awaiting sign-off
> 依賴：§1.2 第二輪 sign-off（粒度 = **per-project**，trust = user-trusted，repo split = `woow-paas-agent-runtime` + `woow-paas-charts`）+ §1.3 Project First-class Model（project schema M:N + project_access ACL）
> §2 內 5/6 條 (namespace prefix + hash 設計) 已 sign-off：採 `paas-ws-pa-{hash}` 命名 + hmac_sha256 hash + hash 存 `agent_session.project_namespace_hash`。

### §2.0 Scope（§2 處理什麼、不處理什麼）

**§2 處理**：

- Pod state machine（spawning / active / reaped / absent / failure modes）— **ownership = project**
- Cold start sequence（first message → pod ready）— resolve `project_id` from channel → pod 識別
- Idle reap mechanism（active → reaped）— project-level idle 判定
- Resume sequence（reaped → active）— project state restore
- Long-term GC（reaped → absent）
- Quota 計算公式（**per-project 配額**，不再吃 workspace baseline）
- Namespace 模型（**`paas-ws-pa-{project_hash}`** — paas-operator 零變動）
- Hash 設計（hmac_sha256 + base32 + 存 `agent_session.project_namespace_hash`）
- Pod 識別 schema（labels / annotations，以 project-id 為主鍵）

**§2 不處理**（留後續章節）：

- WS / SSE protocol schema → §5
- LangGraph thread checkpoint 內部結構 → §3 / §4（skill / agent template binding）
- NetworkPolicy / securityContext 細節 → §6（含跨 workspace egress allowlist — project.workspace_ids 集合 → namespaceSelector）
- 既有 ai_client / discuss_channel 改動順序 → §7
- project schema migration（M2O → M2M）的 dual-write / backfill 順序 → §7 prerequisite
- project_access ACL admin/user 對 agent invoke 的權限差別 → §4
- per-project shared memory across threads → §3 / §4

### §2.1 Pod lifecycle state machine

```
                        ┌──────────────┐
                        │  [ABSENT]    │
                        │  never spawned, or長 TTL GC 已 helm uninstall │
                        └──────┬───────┘
                               │ first message in any conversation of this user
                               ▼
                        ┌──────────────┐         spawn timeout / helm fail
                        │ [SPAWNING]   │ ───────────────────────────────────────►  ┌────────────────┐
                        │ helm install │                                            │ [SPAWN_FAILED] │
                        │ pod scheduling                                            │ requires retry │
                        └──────┬───────┘                                            └────────────────┘
                               │ pod ready (FastAPI /healthz 200, WS server listening)
                               ▼
   ┌───────────►   ┌──────────────────────┐
   │  resume       │      [ACTIVE]        │ ◄────────── next message
   │  (scale 1)    │  pod running         │
   │               │  LangGraph runtime up│
   │               │  WS server accepting │
   │               └──────┬───────────────┘
   │                      │ idle > T_idle (default 30 min)
   │                      │   (no open WS conn AND no message processed)
   │                      ▼
   │               ┌──────────────────────┐
   │               │      [REAPED]        │ ── still REAPED after T_gc (default 30 day)
   │               │  scale 0             │ ────────────────────────────────────►  ┌──────────────┐
   │               │  PVC retained        │                                         │  [ABSENT]    │
   │               │  ConfigMap retained  │                                         │  helm uninstall  │
   └───── next message ───┘                                                          └──────────────┘
                                                                                          (PVC fate: depends on StorageClass policy — see §2.6)

   Failure modes (any state):
     [ACTIVE] → [DEGRADED]   ← pod alive but WS 連不上 / health probe fail
                              ↓ kubectl restart pod → back to [SPAWNING]
     [REAPED] → orphan PVC   ← scale-0 succeeded but namespace 已被 GC（不該發生，但需要 reconciler 偵測）
```

**States 定義**：

| State | 描述 | K8s 物理表徵 | Odoo 端 `agent_session.state` |
|-------|------|-------------|-------------------------------|
| `ABSENT` | 此 user 尚未啟動 agent pod，或長 TTL 已被 helm uninstall | helm release 不存在 | `absent` 或無記錄 |
| `SPAWNING` | helm install 進行中或 pod 啟動中 | helm release status=`pending-install` / pod phase=`Pending` | `spawning` |
| `ACTIVE` | Pod healthy，WS server 接受連線 | helm release status=`deployed` + pod phase=`Running` + WS readiness probe 通過 | `active` |
| `REAPED` | Idle reap：scale to 0 | helm release exists，Deployment.spec.replicas=0 | `reaped` |
| `SPAWN_FAILED` | helm install fail 或 pod crash loop > N 次 | helm release status=`failed` 或 pod phase=`Failed` | `spawn_failed` |
| `DEGRADED` | Pod 存在但 health probe fail | pod 存在但 readiness probe failing | `degraded` |

**State 轉換的觸發者**：

- `ABSENT → SPAWNING`：Odoo controller 收 first message，呼叫 `POST /agent-runtimes/spawn-or-attach`
- `SPAWNING → ACTIVE`：paas-operator 輪詢 pod readiness（既有 `cloud_service` deploy/initialize 已實作此 pattern）
- `ACTIVE → REAPED`：Reaper CronJob 比對 `agent.woow.tw/last-active` annotation 與 T_idle，超過則呼叫 scale_release(replicas=0)
- `REAPED → ACTIVE`（**resume**）：next message → `POST /agent-runtimes/spawn-or-attach` 偵測到 helm release 存在但 replicas=0 → scale_release(replicas=1)
- `REAPED → ABSENT`：Long-term GC CronJob 比對 `agent.woow.tw/reaped-since` 與 T_gc，超過則呼叫 `DELETE /agent-runtimes/{workspace}/{user}` → helm uninstall

### §2.2 Cold start sequence（first message → pod ready）

```
                                                            ┌─────────────────────────┐
                                                            │  paas-operator           │
                                                            │  (FastAPI HTTP service)  │
                                                            └─────────────────────────┘
                                                                       ▲
                                                                       │ (HTTP)
   Browser           Odoo controller                                   │
   ───────           ───────────────                                   │
      │                  │                                             │
      │  GET /api/ai/    │                                             │
      │  stream/{ch_id}  │                                             │
      ├─────────────────▶│                                             │
      │                  │ ① validate CSRF + workspace_access          │
      │                  │ ② resolve user_id / project_id / channel    │
      │                  │   + project_access check (project member?)  │
      │                  │   + compute project_namespace_hash          │
      │                  │ ③ pre-fetch ai.assistant / config /         │
      │                  │   user-scope MCPs → runtime_config dict     │
      │                  │                                             │
      │                  │ ④ POST /api/agent-runtimes/spawn-or-attach  │
      │                  ├────────────────────────────────────────────▶│
      │                  │                                             │
      │                  │           ⑤ lookup helm release             │
      │                  │           "agent-runtime-{workspace}-{uid}" │
      │                  │           "agent-pa-{hash}" in ns           │
      │                  │           "paas-ws-pa-{hash}" (§2.7)        │
      │                  │                                             │
      │                  │           ⑥ if NOT_FOUND → helm install     │
      │                  │           - 同步寫 mcp.json ConfigMap        │
      │                  │           - helm install agent-runtime chart│
      │                  │           - 等 pod ready (poll 1s / max 30s)│
      │                  │                                             │
      │                  │           ⑦ if REAPED (replicas=0) → scale 1│
      │                  │           - kubectl scale ... replicas=1     │
      │                  │           - 等 pod ready (poll 1s / max 15s)│
      │                  │                                             │
      │                  │           ⑧ if ACTIVE → 直接 return         │
      │                  │                                             │
      │                  │  200 OK { pod_ws_url, pod_state, age_ms }   │
      │                  │◄────────────────────────────────────────────┤
      │                  │                                             │
      │                  │ ⑨ open outbound WS to pod_ws_url            │
      │                  ├──── WS handshake ──────────────────────────▶│
      │                  │                                             │  (WS connect → Agent Pod)
      │                  │ ⑩ send {type: "user_message", channel_id,   │
      │                  │   body: "...", history: [...]} on WS        │
      │                  │                                             │
      │                  │ ⑪ for each WS message:                      │
      │                  │   forward → SSE event                       │
      │  data: {chunk}   │                                             │
      │◄─────────────────┤                                             │
      │  data: {tool_call│                                             │
      │◄─────────────────┤                                             │
      │  ...             │                                             │
      │  data: {done}    │                                             │
      │◄─────────────────┤                                             │
      │                  │ ⑫ WS close → SSE close                      │
```

**Timing budget（cold start total）**：

| Phase | 上限 | 累計 | 備註 |
|-------|-----|------|------|
| ① ~ ③ Odoo validate + ORM pre-fetch | 50 ms | 50 ms | sudo 操作不重 |
| ④ HTTP call to paas-operator | 30 ms | 80 ms | cluster-internal |
| ⑥ helm install + image pull (cached) + pod schedule | 3-8 s | 3-8 s | 視 image pull cache 命中 |
| ⑥ FastAPI WS server 啟動 + LangGraph runtime init | 2-5 s | 5-13 s | python import + model bind |
| ⑦ resume from REAPED 路徑（取代 ⑥） | 2-5 s | 2-5 s | 不重新 helm install |
| ⑧ ACTIVE 直接 return | 0 ms | 80 ms | 既有 pod 直接附掛 |
| ⑨ outbound WS handshake | 50-200 ms | + handshake | 含 TCP + WS upgrade |
| ⑩ user_message processed → first chunk | 500-2000 ms | + LLM latency | LLM provider 主導 |

- **Cold start (ABSENT → ACTIVE)**：5-15s（hit image pull cache）/ 15-60s（cold image pull）
- **Warm start (REAPED → ACTIVE)**：2-7s
- **Hot path (ACTIVE 直接 attach)**：< 1s + LLM latency

**UX 策略（§2 範圍內）**：

- Odoo controller 在 ④ 之前先送一個 SSE event `{type: "status", stage: "preparing"}` 給 browser，frontend 顯示「準備中」
- 若 ⑥/⑦ 等待 > 3s，再送 `{type: "status", stage: "spawning_runtime", elapsed_ms: N}` 更新進度
- 若超時（cold start > 60s / warm start > 15s）→ 送 `{type: "error", code: "spawn_timeout"}` 並關閉 SSE，frontend 提示「請重新整理頁面」
- **Cold start UX 細部行為 (Q3 第 8 條未拍板) 留 §5 細化** — §2 只承諾「會送 status event」這個 contract

### §2.3 Idle reap（active → reaped）

**目標**：避免 idle pod 長期佔 quota（特別是 RAM — LangGraph + python runtime 不釋放）

**Heartbeat 機制**（pod 端）：

- FastAPI WS server 在以下事件後更新 pod 自己 annotation `agent.woow.tw/last-active=<iso8601>`：
  - 收到 WS connect / disconnect
  - 收到 user_message
  - tool_call / tool_result 完成
- 更新 annotation 不是熱路徑（每 N 個 event 才寫一次，或定時 batch flush）— 避免 etcd 寫入壓力
- 若 30s 內無任何 event → annotation 不更新（自然衰減）
- Pod 需要 RBAC permission `patch namespaces`，由 helm chart 安裝時建 ServiceAccount + Role（仿 odoo-dev-sandbox `ttl-cronjob.yaml` 的 RBAC pattern）

**Reaper CronJob**（cluster 端）：

- 每 5 分鐘執行（schedule `*/5 * * * *`）
- 掃描 `agent-runtime-*` 標記的 Deployment / namespace
- 比對 `agent.woow.tw/last-active` 與 T_idle
- 過期 → 呼叫 paas-operator `POST /agent-runtimes/{workspace}/{user}/reap` → 內部 call `scale_release(replicas=0)`
- 同時寫 annotation `agent.woow.tw/reaped-since=<iso8601>` 給長 TTL GC 用
- CronJob 部署在 `paas-system` namespace（不在 agent-runtime namespace 內），用 ClusterRole 跨 namespace 操作（仿 odoo-dev-sandbox ttl-cronjob 的 ClusterRoleBinding pattern）

**T_idle 配置**：

- 預設 30 min（透過 `ir.config_parameter` `woow_paas_platform.agent_idle_reap_seconds=1800`）
- 可 per-workspace 覆寫（agent_template / workspace setting）— 留 §4 細化
- 短於 5 min 不建議（CronJob 5 min 週期會 race）

**Reaper 等冪性 (idempotency)**：

- Reaper 在 ACTIVE 狀態時呼叫 scale_release(0)；既為 REAPED 則 noop（HTTP 200，告知 already_reaped）
- 多個 CronJob 同時觸發（理論上 1 job 5min），用 `singleton` schedule mode 避免

### §2.4 Resume sequence（reaped → active）

```
Browser → Odoo controller → paas-operator POST /spawn-or-attach
                              │
                              ▼
                       lookup helm release
                       │ status = deployed
                       │ replicas = 0
                       ▼
                       call scale_release(replicas=1)
                              │
                              ▼
                       既有 pod template re-create pod
                       │ - PVC 自動 re-mount
                       │ - ConfigMap 自動 re-mount
                       │ - Service 自動 endpoint update（沒 break）
                              │
                              ▼
                       poll pod readiness (max 15s)
                       │ - FastAPI startup
                       │ - LangGraph runtime restore from checkpoint
                       │   - 讀 /workspace/project-{pid}/threads/ 內所有 thread
                       │   - lazy load when first message references thread_id
                              │
                              ▼
                       return pod_ws_url to Odoo controller
```

**State restore semantics**：

- LangGraph thread checkpoint 存在 PVC `/workspace/project-{pid}/threads/{channel_id}/` 之下
- Pod 啟動時不 eagerly load 所有 thread；first message of conversation X 來時 lazy load thread X 的 checkpoint
- 若 PVC 因 storageClass 政策被 release / delete（見 §2.6）→ 等同 cold restart，user 體感「對話從頭開始」
- mcp.json ConfigMap 在 resume 時可能 stale（若 cloud_service 在 reap 期間有變更）— 解法：resume 時 paas-operator 同步 rewrite ConfigMap 後再 scale 1（§5 細化 invalidation 邏輯）

### §2.5 Long-term GC（reaped → absent）

**目的**：避免 永遠 REAPED 的 user 佔 PVC / helm release object

**GC CronJob**（cluster 端）：

- 每天 00:00 執行（schedule `0 0 * * *`）
- 掃描所有 `agent-runtime-*` helm release
- 若 `replicas=0` AND `agent.woow.tw/reaped-since` 距今 > T_gc → helm uninstall
- T_gc 預設 30 day（`ir.config_parameter` `woow_paas_platform.agent_gc_seconds=2592000`）

**Helm uninstall 副作用**：

- 所有 K8s resources 都刪（Deployment / Service / ConfigMap / ServiceAccount / RoleBinding）
- PVC 因 helm chart 的 `helm.sh/resource-policy: keep` 或不加 annotation 行為不同：
  - 不加 → PVC 一起刪 → PV 行為依 StorageClass reclaim policy
  - 加 `keep` → PVC 保留，但變孤兒（沒 helm release 管理它）→ 需要 Odoo / 額外 cleaner reconciler 處理

**StorageClass 與 GC 行為交叉表**（這是 Q5 的核心）：

| StorageClass | helm uninstall + PVC keep | helm uninstall + PVC 不 keep |
|-------------|--------------------------|--------------------------|
| `longhorn-delete` (reclaim=Delete) | 孤兒 PVC，需手動清；PV 隨 PVC delete | PVC 刪 → PV 刪 → 資料 lose（簡單） |
| `longhorn-retain` (reclaim=Retain) | 孤兒 PVC + 孤兒 PV，需手動清兩個 | PVC 刪 → PV 保留為 Released 狀態，需手動 GC |

**§2.6 將 phase-planner 推薦的方案在這裡 inline**：用 `longhorn-delete` + PVC 不 keep（**方案 X**）— 簡單，30 day 後 user state lose 是 acceptable trade-off。

### §2.6 StorageClass 選擇（Q5 拍板用）

**phase-planner 推薦**：**方案 X（longhorn-delete + PVC 不 keep）**

| 方案 | StorageClass | helm `resource-policy` | 30-day 之後 state | 維運成本 |
|------|-------------|----------------------|------------------|---------|
| **X**（推薦） | `longhorn-delete`（既有） | 不加 | 隨 helm uninstall 一起 lose | 零（K8s 自動 GC PV） |
| **Y** | 新增 `longhorn-retain` SC | `keep` | 保留為孤兒 PVC + PV | 高（要寫 reconciler 定期掃孤兒）|
| **Z** | `longhorn-delete` | `keep` | 孤兒 PVC，PV 隨 PVC delete | 中（PVC 永遠 bound，要寫單獨 PVC cleanup） |

**選 X 的理由**：

- 既有 `cloud_service` 已採 `longhorn-delete`，agent-runtime 對齊免新增 SC
- 30 day 無互動 = 使用者實質放棄該 user/conversation；state lose 是 acceptable UX
- 若 user 在 31 day 後回來，視同 cold start，LangGraph thread 從零開始 — 跟「沒記憶的 chatbot」沒差，符合一般使用者預期
- Skill 上傳的 `/workspace/project-{pid}/` 內容若有重要資料，可走 export feature（§3/§4 細化）— 不是 PVC 內取回

**選 Y 的反對理由**：

- 30 day 後仍未活躍的 user 通常是真的放棄；保留 state 是 dead weight
- 寫 reconciler 多一份維運（既有 paas-operator 沒 reconciler pattern，加這個 = 新 surface area）

**選 Z 的反對理由**：

- 半套：state 保留但 helm release 不在了，restore 路徑要新建（重 helm install 還是要拉新 PVC）— 複雜度沒比 Y 低多少

### §2.7 Namespace 模型（per-project pivot — sign-off 版）

> ✓ **SIGNED-OFF 2026-05-21**：採 `paas-ws-pa-{project_hash}` 命名 + paas-operator code 零變動 + hash 設計如下。

**Namespace 命名**：`paas-ws-pa-{project_hash}`

**Hash 設計（sign-off）**：

```python
import base64, hashlib, hmac

def compute_project_namespace_hash(project_id: int, secret: bytes) -> str:
    """Return 16-char URL-safe base32 hash of project_id.

    - HMAC-SHA256 with cluster-side secret (不外洩)
    - 取前 10 bytes → base32 → 16 chars
    - 不洩露 project_id（attacker 無 secret 無法反推）
    - 雙向查表透過 Odoo 端 agent_session.project_namespace_hash field 存
    """
    digest = hmac.new(secret, str(project_id).encode(), hashlib.sha256).digest()
    return base64.b32encode(digest[:10]).decode('ascii').rstrip('=').lower()
```

**完整 namespace 名稱長度**：`paas-ws-pa-` (11 chars) + 16 hash chars = **27 chars** ≪ K8s 上限 63 chars ✓

**Secret 來源**：`ir.config_parameter` `woow_paas_platform.agent_namespace_secret`（cluster 內生成，不外洩；rotate 機制 §7 拍板）

**Hash 雙向查表**：

- 正向（project_id → hash）：呼 `compute_project_namespace_hash(project_id, secret)`，無 cache
- 反向（hash → project_id）：透過 Odoo 端 `agent_session.project_namespace_hash` field（unique index）→ `read project_id`

**為什麼採 hash 而非直接用 project_id**：

- 不洩露 cluster 內部 project_id 序號（namespace 名稱可能會出現在 K8s log / dashboard / kubectl output）
- 防止 attacker 透過 namespace 名稱 enumerate project 數量 / 推測活躍度
- 與 既有 `cloud_service.reference_id`（UUID）的 anti-enumeration 設計理念對齊

#### §2.7.1 paas-operator 零變動分析

既有 `paas-operator/src/config.py:18` 設 `namespace_prefix = "paas-ws-"`，`paas-operator/src/services/helm.py:23-26` 強制 prefix check：

```python
def _validate_namespace(namespace: str):
    if not namespace.startswith(settings.namespace_prefix):
        raise ValueError(f"Namespace must start with '{settings.namespace_prefix}'")
```

新命名 `paas-ws-pa-{hash}`：

- 以 `paas-ws-` 開頭 ✓ → `_validate_namespace` 通過
- 無 prefix config 變動需求
- `create_namespace` endpoint 直接接受
- 既有 `release.py` helm operations 也直接適用（namespace 是 user-supplied parameter）

**結論**：paas-operator code **零變動**（仍可走 task brief constraint 「不擅自擴大 paas-operator refactor」）。本 epic 新增的 paas-operator endpoints 全部在 **新建 router** `/api/agent-runtimes/...`（§2.11），不動既有 namespace_prefix / `_validate_namespace` 邏輯。

#### §2.7.2 對比之前 per-user 方案

| 比較項 | per-user（之前 sign-off） | per-project（本次 pivot） |
|--------|-----------------------|------------------------|
| Namespace 命名 | `paas-ws-agent-{workspace_id}` | `paas-ws-pa-{project_hash}` |
| 一個 namespace 內 pod 數 | N user pods | 1 project pod |
| Quota 邊界 | workspace agent quota / N user | project agent quota / 1 pod |
| Trust 邊界 | 跨 user 物理隔離 | 跨 project 物理隔離（同 project 多 user 共享 pod） |
| paas-operator 影響 | 零（`paas-ws-agent-*` 滿足 prefix） | 零（`paas-ws-pa-*` 滿足 prefix） |
| NetworkPolicy egress 目標 | workspace 內 cloud_service ns | project.workspace_ids 集合的所有 cloud_service ns（跨 workspace） |

#### §2.7.3 NetworkPolicy egress 跨 workspace（per-project 新挑戰）

per-project pod 由於 project.cloud_service_ids 可跨 workspace，egress allowlist 必須動態 reflect project 綁定的 workspaces：

- Helm chart 渲染時拿 project.workspace_ids 集合 → 渲染多條 `namespaceSelector matchExpressions { workspace_id IN [w1, w2, ...] }`
- 當 project.cloud_service_ids 改變（add/remove cloud_service）→ paas-operator 觸發 helm upgrade with new egress allowlist（或直接 patch NetworkPolicy CR）— 細節 §6
- 既有 dev-sandbox helm chart 已 demo `allowEgressToNamespaces` array values 渲染 pattern，直接複用

### §2.8 Quota 計算（per-project pivot — sign-off 版）

**per-project agent 配額（新增）**：

per-project pod 蘊含 quota 邊界 = project（不再吃 workspace baseline）。`ir.config_parameter` 新增三項，預設值給 v1 baseline：

| Config key | 默認值 | 描述 |
|-----------|--------|------|
| `woow_paas_platform.agent_quota_cpu_per_project` | `2` | CPU cores 上限（單 project pod 主要工作負載 + 1 倍 burst） |
| `woow_paas_platform.agent_quota_memory_gi_per_project` | `2` | RAM Gi 上限 |
| `woow_paas_platform.agent_quota_storage_gi_per_project` | `2` | Storage Gi 上限（PVC：threads + shared memory + skill cache） |

**create agent namespace 時**：paas-operator `POST /api/namespaces` 對應 `paas-ws-pa-{hash}` namespace，傳這三個 limit。

**per-pod 預設 resources**（helm chart values）：

```yaml
agentRuntime:
  resources:
    requests:
      cpu: 200m
      memory: 256Mi
    limits:
      cpu: 1000m
      memory: 1Gi
  pvc:
    size: 1Gi
```

**單 project 容量上限**（per-pod limit × namespace quota cap）：

- 1 project = 1 pod，pod CPU limit 1000m ≪ namespace cap 2000m → 不會撞 quota 上限
- PVC `1Gi default ≪ namespace storage cap 2Gi` → 預留 1Gi 給 skill cache / shared memory 成長
- 第 2 個 pod（理論上不該發生 — 同 project 只該 1 個 pod）若 race 起 → 撞 cap，paas-operator scheduler 拒絕

**超出配額時行為**：

- 同 project 試圖第二次 spawn（concurrent race） → paas-operator `spawn-or-attach` 內部 lock 拒絕第二個（idempotent path 直接 attach 既有 pod，不會真的炸 quota）
- PVC 滿（skill 寫太多到 `/workspace`） → pod 內部 IO error，agent log 標 `disk_full`；用戶可 reset workspace（§3 細化）

**Quota override**：

- project 升級 plan / project_access role admin 可動態調整（透過 Odoo `project_access` role check + config_parameter override）
- 細節 §4 與 agent_template / project plan binding 一起設計

#### §2.8.1 Project 跨 workspace 的計費歸屬

per-project 蘊含的計費問題（**留 §4 處理**，§2 只 surface）：

- project 的 agent quota cost 歸給哪個 workspace？
- 多 workspace 綁定時，誰付？
- 一個 user 是 workspace A member + workspace B member，project 跨兩 workspace — agent invoke 行為計入哪邊？
- phase-planner 初步傾向：「project 有 primary_workspace_id（user 建 project 時選的第一個 cloud_service 的 workspace）」，計費歸 primary_workspace；secondary workspace 只享 read access — 但這要 §4 與 project_access role 一起拍板。

### §2.9 Idle reap & GC 配置（Q4 拍板用）

**phase-planner 推薦**：

- **Reap 策略 = scale-to-0 + 長 TTL helm uninstall**（即 §2.3 + §2.5 已 inline 描述的方案）
- T_idle = 30 min（default）
- T_gc = 30 day（default）
- Reaper CronJob 5 min 週期，GC CronJob 1 day 週期

**選此方案理由**：

- 完全對齊既有 `cloud_service._do_suspend` / `_do_resume` 的 pattern — paas-operator `scale_release` 既有 endpoint 直接用，零新增 k8s 操作
- PVC 與 ConfigMap 保留在 REAPED 期間（scale-0 不 unmount PVC），resume restore 邏輯簡單
- GC 階段才 helm uninstall（觸發 PVC 隨 StorageClass policy 走）

**替代方案（不推薦但列出讓 leader 看 trade-off）**：

| 替代 | 描述 | 優點 | 缺點 |
|------|------|-----|------|
| **B: 短 TTL helm uninstall（無 scale-0）** | idle → 直接 helm uninstall | 簡單，1 個狀態（active / absent） | 每次 resume 都 cold start 5-15s（高 UX cost），PVC 政策直接決定 state 命運 |
| **C: 純 scale-0，無 GC** | 永遠 REAPED 不釋放 | 永久 state 保留 | helm release / PVC 永遠累積，cluster object 爆炸 |

### §2.10 Pod 識別 / label / selector schema（per-project pivot — sign-off 版）

**Helm release name**：`agent-pa-{project_hash}`（例：`agent-pa-jbswy3dpehpk3pxp`）

**Namespace name**：`paas-ws-pa-{project_hash}`（例：`paas-ws-pa-jbswy3dpehpk3pxp`）

> hash = `hmac_sha256(project_id, secret)[:10] → base32 → 16 chars`（§2.7 sign-off）

**Labels**（pod / deployment / service / configmap）：

```yaml
labels:
  app.kubernetes.io/name: agent-runtime
  app.kubernetes.io/instance: agent-pa-{project_hash}
  app.kubernetes.io/component: runtime    # 或 ttl-check / mcp-config
  app.kubernetes.io/managed-by: Helm
  agent.woow.tw/project-hash: "{project_hash}"
  # 注意：project-id 本身**不放 K8s label**（避免 enumerate / 序號洩漏）
  # project_id ↔ hash 對應只在 Odoo agent_session.project_namespace_hash 內查
```

**Annotations**（namespace + deployment + pod）：

```yaml
annotations:
  agent.woow.tw/last-active: "2026-05-21T10:30:00Z"     # pod 自己 patch
  agent.woow.tw/spawn-at: "2026-05-21T09:00:00Z"         # helm install 時設
  agent.woow.tw/reaped-since: "2026-05-21T11:00:00Z"     # reaper scale-0 時設；resume 時清空
  agent.woow.tw/runtime-version: "1.0.0"                  # image tag
  agent.woow.tw/workspace-ids: "1,5,12"                   # project.workspace_ids 序列化（給 NetworkPolicy 渲染參考；變動時 helm upgrade rewrite）
```

**Selector for paas-operator lookup**：

```
labelSelector: app.kubernetes.io/instance=agent-pa-{project_hash},app.kubernetes.io/name=agent-runtime
```

**Selector for reaper CronJob 全掃**：

```
labelSelector: app.kubernetes.io/name=agent-runtime
```

### §2.11 New paas-operator endpoints（per-project pivot — sign-off 版）

URL 路徑改用 `project_hash` 為主鍵（**不放 project_id**，避免序號洩漏）：

- `POST /api/agent-runtimes/spawn-or-attach` — body `{project_hash, project_workspace_ids: [w1, w2], mcp_snapshot, ai_provider_config, agent_template_id}` → return `{pod_ws_url, pod_state, spawn_age_ms}`
  - 內部邏輯：lookup helm release `agent-pa-{project_hash}` in ns `paas-ws-pa-{project_hash}` → install / scale-1 / no-op → poll readiness → return
  - **`project_workspace_ids` 用途**：渲染 NetworkPolicy egress allowlist（哪些 workspace ns 可達）；同時寫入 annotation `agent.woow.tw/workspace-ids`
- `POST /api/agent-runtimes/{project_hash}/reap` — return `{action: "reaped" | "already_reaped" | "not_found"}`
  - 內部：`scale_release(release_name=agent-pa-{hash}, replicas=0)` + patch annotation `reaped-since`
- `DELETE /api/agent-runtimes/{project_hash}` — helm uninstall（長 TTL GC 用）
- `POST /api/agent-runtimes/{project_hash}/invalidate-mcp` — 通知 pod refresh mcp.json snapshot（細節 §5）
- `POST /api/agent-runtimes/{project_hash}/sync-workspaces` — body `{project_workspace_ids: [w1, w2, w5]}` → helm upgrade 重寫 NetworkPolicy egress allowlist（當 project.cloud_service_ids 變動時 Odoo 觸發）
- `GET /api/agent-runtimes/{project_hash}/status` — return `{state, last_active, spawn_age_ms, pod_ws_url}`（dashboard / debug 用）

**[需 Odoo 端配合] paas-operator 收到 project_hash 時的反向查詢**：

paas-operator **不需要**反向查 project_id（純走 hash 識別 namespace / release）。Odoo 端在發 spawn-or-attach 之前已知 project_id 與 hash，所以 paas-operator API surface 不洩漏 project_id。

**ConfigMap (`mcp.json`) 寫入流程**：

- `spawn-or-attach` 內，helm install 之前先 `kubectl create configmap agent-pa-{hash}-mcp --from-literal=mcp.json=<serialized> -n paas-ws-pa-{hash}`
- helm chart values 引用該 ConfigMap mount 到 pod `/etc/agent-runtime/mcp.json`
- Resume / sync-workspaces 時若 ConfigMap 已存在且 mcp_snapshot 變動 → `kubectl patch configmap`
- Helm uninstall 不刪 ConfigMap（mcp.json ConfigMap 由 paas-operator 直接管理）— 或反過來：寫入 chart 內讓 helm 管，invalidation 走 `helm upgrade --set mcpSnapshot=...` — **待 §5 拍板**

#### §2.11.1 既有 paas-operator router 影響

新 endpoints 全部歸入 **新建 router** `extra/paas-operator/src/api/agent_runtimes.py`，main.py 加 `app.include_router(agent_runtimes.router)`。

**既有 router 不動**：
- `releases.py` — 既有 helm CRUD（agent_runtimes 新 router **內部呼用** 既有 `helm_service.install / get / upgrade / scale / uninstall`，不需新 helm primitive）
- `namespaces.py` — 既有 `create_namespace` 可直接用於 `paas-ws-pa-{hash}` namespace（prefix check ✓）
- `routes.py` / `tunnels.py` — agent-runtime 不暴露 ingress（純 cluster-internal WS），不動

### §2.12 New Odoo model: `agent_session`（per-project pivot — sign-off 版）

```python
class AgentSession(models.Model):
    _name = 'woow_paas_platform.agent_session'
    _description = 'Per-project agent runtime pod lifecycle record'

    # Identity (per-project, not per-user)
    project_id = fields.Many2one(
        'project.project',
        required=True,
        index=True,
        ondelete='cascade',
    )

    # Hash for K8s namespace / helm release identification
    # Computed once at spawn-or-attach; stored for reverse lookup hash → project_id
    project_namespace_hash = fields.Char(
        string='K8s Namespace Hash',
        readonly=True,
        index=True,
        help='hmac_sha256(project_id, secret)[:10] → base32 16 chars; '
             'used to derive helm release name agent-pa-{hash} and '
             'namespace paas-ws-pa-{hash}',
    )

    # Lifecycle
    state = fields.Selection([
        ('absent', 'Absent'),
        ('spawning', 'Spawning'),
        ('active', 'Active'),
        ('reaped', 'Reaped'),
        ('degraded', 'Degraded'),
        ('spawn_failed', 'Spawn Failed'),
    ], default='absent', index=True)

    # K8s tracking
    helm_release_name = fields.Char(readonly=True)  # agent-pa-{hash}
    helm_namespace = fields.Char(readonly=True)      # paas-ws-pa-{hash}
    pod_ws_url = fields.Char(readonly=True)
    runtime_version = fields.Char()                   # image tag

    # Multi-workspace tracking (per-project shared across workspaces)
    workspace_ids_snapshot = fields.Char(
        readonly=True,
        help='Comma-separated workspace_id list when spawn-or-attach last called; '
             '與 project.workspace_ids 不同步時需 sync-workspaces 觸發 helm upgrade',
    )

    # Timestamps
    spawn_at = fields.Datetime(readonly=True)
    last_active = fields.Datetime(readonly=True)
    reaped_since = fields.Datetime(readonly=True)

    # Metadata
    agent_template_id = fields.Many2one('woow_paas_platform.agent_template')
    mcp_snapshot_hash = fields.Char(readonly=True, help='SHA of mcp.json content; invalidation 用')
    last_error = fields.Text(readonly=True)

    _sql_constraints = [
        ('unique_project',
         'UNIQUE(project_id)',
         'One agent session per project (per-project pod model).'),
        ('unique_namespace_hash',
         'UNIQUE(project_namespace_hash)',
         'Namespace hash must be unique (collision rare with hmac-sha256/10-byte but enforce).'),
    ]
```

- **不存 message history**（仍在 `mail.message`）— `agent_session` 純 lifecycle metadata
- **不存 user_id**：per-project pod 服務 project member 集合（透過 project_access 推導），不掛特定 user；user invoke 行為記在 `mail.message` 既有 `author_id` 上
- **同步點**：paas-operator state transition 透過 webhook 或 Odoo 端輪詢同步到 `agent_session.state`（細節 §5 拍板，§2 只定義 schema）
- **Hash 計算時機**：`@api.model_create_multi` 在 create 時 compute 並寫入 `project_namespace_hash`（hash 是 project_id 的函數，project lifecycle 內穩定）
- **workspace_ids_snapshot drift detection**：cron 每小時掃 `agent_session` 對比當前 `project.workspace_ids` 與 `workspace_ids_snapshot`，不一致則呼 `POST /agent-runtimes/{hash}/sync-workspaces` 重渲染 NetworkPolicy

### §2.13 Acceptance criteria（§2 範圍 — per-project pivot）

下列陳述使用者能做到 / 系統會自動完成，後續 tester / implementation 對應為 test case：

1. **首次對話啟動 pod**：project member 開新 task / channel 並送第一條訊息（project 尚未有 agent pod）→ 15s 內收到 AI 回應（image cached）/ 60s 內（cold image pull）
2. **既有 pod 即時回應**：project ACTIVE pod 上任一 member 送訊息 → < 1s 收到首個 chunk（不含 LLM latency）
3. **同 project 多 user / 多 conversation 切換**：同 project 內 user A 在 task X 對話後，user B 切到 task Y，task Y 對話狀態獨立（thread_id 隔離），**pod 不重新 cold start**（同 pod 服務）
4. **idle reap 自動觸發**：project 內所有 member 30 min 無互動後，pod 被 scale 到 0；該 project namespace 內 CPU/RAM consumption 變 0
5. **resume 自動觸發**：project member 在 reap 後再次送訊息 → 7s 內收到回應；既有 thread checkpoint 保留（對話歷史不消失，LangGraph state 從 PVC restore）
6. **長 TTL GC**：project 30 day 完全未互動 → 系統自動 helm uninstall；下次任一 member 再來等同 cold start + thread state 從零
7. **per-project quota enforced**：單 project pod 的 CPU/memory/storage 超出 namespace quota（不該發生但理論可能 — 例 skill 寫滿 PVC） → pod 內部寫 IO error，agent log 標 quota event
8. **跨 project 隔離**：project A 的 pod 物理上在 ns `paas-ws-pa-{hashA}`，與 project B 的 ns `paas-ws-pa-{hashB}` 物理分離；project A member 看不到 project B 的 PVC / mcp.json / WS endpoint
9. **跨 workspace egress 正確**：project 綁 cloud_service ∈ workspace W1 + W2 → pod NetworkPolicy egress 允許 ns `paas-ws-{W1}` + `paas-ws-{W2}`，拒絕到其他 workspace ns
10. **project.cloud_service_ids 變動同步**：user 在 Odoo 後台對 project add/remove cloud_service → paas-operator 收 `sync-workspaces` → pod NetworkPolicy egress 1 min 內更新
11. **`agent_session` lifecycle 追蹤**：Odoo 後台 `woow_paas_platform.agent_session` model 可看到每個 project 的當前 state、`project_namespace_hash`、`workspace_ids_snapshot` 與 timestamps
12. **Hash 反查正確**：給定 namespace 名稱 `paas-ws-pa-{hash}` 可在 Odoo `agent_session` 透過 `project_namespace_hash` field 反查 project_id（unique index 確保）
13. **無 dangling resources**：helm uninstall 後同 namespace 內無 orphan PVC / ConfigMap / ServiceAccount
14. **project_access ACL gating**：non-member 無法透過 `/api/ai/stream/<channel_id>` 觸發 spawn-or-attach（既有 `_check_channel_access` 加 project_access check — §4 細化）

刻意不測 / 留後續：

- WS protocol message schema 正確性（§5）
- securityContext 細項（§6）
- 既有 SSE controller 改寫的回歸（§5 + §7）
- skill bundle download / cache（§3）

### §2.14 Affected files / new files（§2 範圍預測 — per-project pivot）

**新建（落地時）**：

- `woow-paas-charts/charts/agent-runtime/` — helm chart（template + values + values-stg + values-prod）
- `woow-paas-charts/charts/agent-runtime/templates/`：
  - `namespace.yaml`（仿 dev-sandbox；命名 `paas-ws-pa-{hash}`）
  - `deployment.yaml`（pod template）
  - `service.yaml`（ClusterIP for WS）
  - `configmap-mcp.yaml`（mcp.json 注入 — 也可選 paas-operator 直接管）
  - `serviceaccount.yaml` + `role.yaml` + `rolebinding.yaml`（給 pod patch 自己 annotation）
  - `resource-quota.yaml`、`network-policy.yaml`（egress allowlist 以 project.workspace_ids 集合渲染 — 細節 §6）
- `paas-cluster-tools/agent-runtime-reaper/cronjob.yaml`（reaper 在 `paas-system` ns；放 cluster tools repo 或 charts repo TBD）
- `paas-cluster-tools/agent-runtime-gc/cronjob.yaml`（長 TTL GC）

**修改（落地時）**：

- `extra/paas-operator/src/api/agent_runtimes.py`（新檔，新 router；hash-based URL keys）
- `extra/paas-operator/src/main.py`（include 新 router）
- `extra/paas-operator/src/models/schemas.py`（新 schemas，含 `project_hash` / `project_workspace_ids` 入參）
- `src/models/agent_session.py`（新 Odoo model — per-project, with `project_namespace_hash`）
- `src/models/project_access.py`（新 Odoo model — first-class project ACL，仿 workspace_access）
- `src/models/__init__.py`（import）
- `src/security/ir.model.access.csv`（新 agent_session + project_access ACL）
- `src/security/ir_rules.xml`（新 project_access rules；agent_session 走 project_access 推導）
- `src/views/agent_session_views.xml` + `project_access_views.xml`（後台 view，供管理員看 lifecycle / ACL）
- `src/data/agent_session_cron.xml`（drift detection cron — `workspace_ids_snapshot` vs `project.workspace_ids` 比對 → sync-workspaces）

**§7 prerequisite 範圍（不在 §2 直接落地，但本 epic 必做）**：

- `src/models/project_project.py`：`cloud_service_id` (M2O) → `cloud_service_ids` (M2M) + 刪 `UNIQUE` constraint + `workspace_id` (related) → `workspace_ids` (computed M2M)
- `src/models/cloud_service.py`：`project_ids` (One2many) → `Many2many` reverse；`_do_cleanup` filter 語意延伸到 M:N
- `src/models/project_task.py`：`primary_cloud_service_id` / `extra_cloud_service_ids` 連鎖修改
- `src/models/mcp_server.py`：**不動**（D4 守線；schema 不變，僅 ACL domain walk path 改 M2M）
- `src/security/ir_rules.xml`：`mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` dual-leg domain 內 `cloud_service_id` → `cloud_service_ids` M2M walk（Inv #11 守線）
- 上述 5 個 file 改動 + DB migration script — **詳見 §7 migration phase**

**不動**（保護 invariants）：

- `mcp_server` / `mcp_tool` schema（D4 守；本 epic 對 mcp_server 只動 ACL domain walk path、不動欄位）
- `cloud_service` 既有 deploy / quota 公式（agent quota 是新增獨立 quota，不混 cloud_service quota）
- 任何 Odoo std field 型態（除了 project / task 既有 M2O field 改 M2M 屬於本 epic explicit in-scope；無動 std field 型態）
- `ir.model.access.csv` 對 `base.group_user` 的 std model ACL（保護 [[odoo-addon-std-rule-symmetric-deactivation]] pattern）
- refactor branch 的 D5 removal / sidecar_backed / 6-rule ACL split / Inv #9/#10/#11（本 epic 繼承不改）

### §2.15 Risks & rollback strategy（per-project pivot）

| Risk | 嚴重度 | Mitigation | Rollback |
|------|-------|-----------|---------|
| pod crash loop 把 project namespace quota 吃滿 | 中 | per-pod resources.limits + project agent_quota（不混 workspace quota） | scale 0 該 pod；project quota 仍 reserved 但不阻塞 cloud_service |
| reaper CronJob race（同 project 被多 job 同時 reap） | 低 | scale_release(0) 等冪；annotation patch 用 RFC 6902 strategic merge | 無影響 |
| Helm install 失敗導致 state 卡在 SPAWNING | 中 | spawn timeout 60s + retry 機制；超過 3 次標 SPAWN_FAILED | 手動 `helm uninstall` 釋放 release name；下次 project member 訊息重試 |
| Hash collision（不同 project_id 算出同 hash） | 極低 | hmac-sha256 取 10 bytes = 80 bits；2^40 project 才有 50% birthday collision；`agent_session.project_namespace_hash` UNIQUE constraint 在 collision 時 raise，迫使重算或人工干預 | 改 secret 重算（涉及 cluster-wide hash 重新生成 — 不可逆事件級操作；rotate 機制 §7 細化）|
| Secret leak → attacker 可預測 hash → enumerate namespace | 高 | secret 存 `ir.config_parameter` + `groups='base.group_system'`；不寫入 git；migration 第一次部署時自動生成 random | rotate secret（同上）；attacker 已 enumerate 的部分 namespace 名稱無 K8s read permission 仍無法 access |
| ConfigMap mcp.json 與實際 mcp_server 不同步 | 中 | resume 時主動 patch ConfigMap；§5 invalidation 機制兜底；drift cron 每小時掃 | paas-operator API `POST /invalidate-mcp` 強制重寫 |
| 長 TTL GC 誤刪正在活躍 pod | 高 | GC 只挑 REAPED >= T_gc；ACTIVE 不會被掃到 | helm uninstall 後 project member 再送訊息會冷啟新 pod；thread state lose（acceptable for 30day+ idle） |
| paas-operator down → Odoo 無法 spawn pod | 高 | Odoo 端 retry + circuit breaker；既有 cloud_service 已有 timeout pattern | paas-operator 重啟；Odoo 端 fallback「服務暫不可用」訊息 |
| project.cloud_service_ids 變動但 NetworkPolicy 未同步 → tool call 連不上新 cloud_service MCP | 中 | drift detection cron 每小時掃 `workspace_ids_snapshot` vs `project.workspace_ids` → 不一致觸發 sync-workspaces；Odoo 端 write hook 即時觸發 | 手動 `kubectl edit networkpolicy ...` 或 helm upgrade |
| 跨 workspace cloud_service binding 引入 ACL 混淆（user 是 W1 member 但 project 跨 W1+W2） | 中 | project_access ACL 與 workspace_access 解耦（§1.3 / §4）；agent invoke 走 project_access check（不依賴 workspace_access） | project_access 在 Odoo 端撤銷 user role；agent pod **不感知 user ACL 變動**，下次訊息來時 Odoo 端 gate 直接擋 |
| Quota 升 / 降 plan 時，REAPED pod 被 evict | 低 | K8s ResourceQuota 對 REAPED pod 不消耗 CPU/RAM（replicas=0）；只 PVC 占 storage | 升 plan 後 PVC 容量自動釋放餘量；降 plan 若 storage 用滿，標 quota_warning |

### §2.16 Open questions for leader sign-off（§2 新產出 — 全部已 sign-off）

> 三條核心拍板需求 + 兩條 per-project pivot 額外拍板。**全部已 sign-off 2026-05-21**。

1. ~~**[需 leader 拍板] Reap 策略（§2.9）**~~ **[SIGNED-OFF 2026-05-21]** scale-to-0（T_idle=30 min）+ 長 TTL helm uninstall（T_gc=30 day）
2. ~~**[需 leader 拍板] StorageClass（§2.6）**~~ **[SIGNED-OFF 2026-05-21]** 方案 X（`longhorn-delete` + PVC 不 keep；30 day 後 state lose acceptable）
3. ~~**[需 leader 拍板] Namespace 模型（§2.7）**~~ **[SUPERSEDED 2026-05-21 by per-project pivot]** 原 per-user 方案 (b) `paas-ws-agent-{workspace_id}` 廢；改 per-project namespace `paas-ws-agent-{workspace_id}` → `paas-ws-pa-{project_hash}`（仍滿足 `paas-ws-` prefix → paas-operator code 零變動）
4. ~~**[需 leader 拍板 — per-project 新增] Namespace prefix（§2.7）**~~ **[SIGNED-OFF 2026-05-21]** 採 `paas-ws-pa-{hash}` 保留 `paas-ws-` prefix（paas-operator namespace_prefix config 零變動）
5. ~~**[需 leader 拍板 — per-project 新增] Hash 設計 + 存哪（§2.7 + §2.12）**~~ **[SIGNED-OFF 2026-05-21]** `hmac_sha256(project_id, secret)[:10] → base32 → 16 chars`；hash 存 `agent_session.project_namespace_hash` field（**不動 project schema**）；secret 從 `ir.config_parameter woow_paas_platform.agent_namespace_secret`

**§2 額外探出來但不阻塞 §2 sign-off 的事項**（不需立刻拍板，§3~§7 各節再處理）：

- ConfigMap (`mcp.json`) 由 paas-operator 直接管 vs helm chart 管 — §5 拍板
- Odoo `agent_session` 與 paas-operator 的同步方式（webhook / 輪詢 / SSE） — §5 拍板
- `agent.woow.tw/last-active` 寫入頻率調優（避免 etcd 壓力） — §6 拍板
- per-project agent quota 可否被 project plan / agent_template / project_access role 動態 override — §4 拍板
- ACL `mcp_server_rule_workspace_user` dual-leg domain 從 M2O walk 改 M2M walk 的具體 expression — §7 migration phase 拍板（refactor branch 已落地 dual-leg shape，本 epic 延伸 walk path cardinality；Inv #11 守線確認）
- task model `primary_cloud_service_id` / `extra_cloud_service_ids` 連鎖修改的最終 schema 形狀 — §7 拍板（保留 primary 概念 / domain 拆掉 same-workspace 限制 / 是否引入 task-level cloud_service binding 改 nullable）

## §3 Skill bundle format & registry

> **Status**：Phase 1 — §3 draft submitted, awaiting sign-off
> 依賴：§1.2 第二輪 sign-off（per-project pivot）、§1.3 project_access ACL、§2.12 agent_session model
> 5 條 [需 leader 拍板] 留 §3.13 escalate（已預警：storage backend、versioning、signature、sandbox profile、skill/mcp 共用 namespace 與否）

### §3.0 Scope（§3 處理什麼、不處理什麼）

**§3 處理**：

- Skill bundle 物理結構（SKILL.md / scripts/ / assets/ — 對齊 Claude Code skill convention）
- Odoo `woow_paas_platform.skill` registry model 與 ACL
- Storage backend（v1 backend 拍板 + 留 v2 hooks）
- Versioning 模型（immutable bundle + alias）
- Lazy fetch + cache（pod 端 mount 策略）
- Per-project skill 配置（skill ↔ project binding；project_access role 影響）
- Skill source（v1 user upload；v2 marketplace hooks）
- Signature / trust（v1 user-trusted，但 integrity 用 SHA256 digest）
- Sandbox profile 在 skill bundle 內的角色（metadata-only or executable constraint）
- Skill vs MCP narrative（兩條 tool dispatch path 並存的釐清）

**§3 不處理**（留後續章節）：

- Skill 的 LangGraph tool wrapper 實作細節 → §4（與 agent_template binding 一起）
- Subprocess sandbox 細部約束（rlimit / env whitelist / network policy） → §6
- Marketplace skill 物理機制（registry server / federation） → v2 不在本 epic
- Skill 與 mcp_server 的 invocation budget 互相影響（token budget / rate limit） → §4 or §6
- Skill bundle CI/CD（自動 lint / test / publish） → 不在本 epic scope

### §3.1 Skill bundle 結構（對齊 Claude Code skill convention）

Skill bundle 是一個 directory，物理結構：

```
my-skill/
├── SKILL.md            # required — frontmatter + body
├── scripts/            # optional — executable entrypoints
│   ├── main.py
│   └── helper.sh
├── assets/             # optional — static templates / docs / fixtures
│   └── template.txt
├── requirements.txt    # optional — python deps（v1 不支援自動 pip install，保留 metadata）
└── README.md           # optional — human-facing doc（與 SKILL.md body 不同）
```

**`SKILL.md` 結構**（與 Claude Code skill convention 對齊）：

```markdown
---
name: my-skill-slug                              # required, kebab-case, unique per project
description: 一句話描述 skill 做什麼，給 LLM 看  # required, ≤ 200 chars
entrypoint: scripts/main.py                      # optional, default scripts/main.py
runtime: python3.11                              # optional, default python3.11
parameters:                                      # optional, JSON Schema for tool args
  type: object
  properties:
    query:
      type: string
      description: Search query
  required: [query]
timeout_seconds: 30                              # optional, default 30; host enforced 上限 §6 拍板
max_output_kb: 64                                # optional, default 64; host enforced
allowed_egress: false                            # optional, default false; v1 hint only（§3.9）
metadata:                                        # optional, free-form metadata
  category: data-processing
  author: <user_display>
---

# my-skill

human-readable description (markdown body)
…
```

**LangGraph tool binding**（pod 端執行邏輯，§4 細化）：

- agent runtime 在 invoke skill 前，把 SKILL.md frontmatter parse 成 dict
- 用 `langchain_core.tools.StructuredTool` 包裝（schema 來自 `parameters` 或預設 single-string `input`）
- tool name = SKILL.md `name`；tool description = SKILL.md `description`
- tool._run(args) → subprocess.run([runtime, entrypoint], input=json.dumps(args), timeout=timeout_seconds, env=<whitelist>, cwd=`/workspace/project-{pid}/skills/cache/<name>/<version>/`)（實體 PVC path；== cache.py extract 目標，canonical 見 §6.2）
- stdout → tool result；stderr → log；exit_code != 0 → tool_error event（§5 protocol）

### §3.2 Odoo `woow_paas_platform.skill` registry model

```python
class Skill(models.Model):
    _name = 'woow_paas_platform.skill'
    _description = 'Project-scoped agent skill bundle'
    _order = 'project_id, name, version desc'

    # Identity
    name = fields.Char(
        string='Skill Name',
        required=True,
        index=True,
        help='Slug from SKILL.md frontmatter name; unique per (project_id, version)',
    )
    title = fields.Char(
        string='Display Title',
        compute='_compute_title_from_metadata',
        store=True,
    )
    description = fields.Text(
        string='Description',
        help='From SKILL.md frontmatter description',
    )

    # Versioning
    version = fields.Char(
        string='Version',
        required=True,
        default='0.1.0',
        help='Semver; bundle is immutable per (name, version)',
    )
    is_latest = fields.Boolean(
        string='Is Latest Published',
        default=False,
        index=True,
        help='Computed alias — points to most recent state=published row for (project_id, name)',
    )

    # Project binding (per-project pivot)
    project_id = fields.Many2one(
        'project.project',
        required=True,
        ondelete='cascade',
        index=True,
        help='Owner project; skill is invokable only by this project agent pod',
    )

    # Bundle physical reference
    bundle_attachment_id = fields.Many2one(
        'ir.attachment',
        string='Bundle Storage',
        ondelete='restrict',
        help='Zip / tarball of bundle directory; one row per (skill, version)',
    )
    bundle_sha256 = fields.Char(
        string='SHA256 Digest',
        required=True,
        readonly=True,
        index=True,
        help='Computed at upload; pod verifies on fetch; rotate when bundle re-uploaded',
    )
    bundle_size_kb = fields.Integer(
        string='Bundle Size (KB)',
        readonly=True,
    )

    # Metadata parsed from SKILL.md frontmatter
    skill_metadata = fields.Text(
        string='Parsed SKILL.md frontmatter (JSON)',
        readonly=True,
    )

    # Lifecycle
    state = fields.Selection([
        ('draft', 'Draft'),
        ('published', 'Published'),
        ('deprecated', 'Deprecated'),
        ('archived', 'Archived'),
    ], default='draft', required=True, index=True)

    # Audit
    uploaded_by_uid = fields.Many2one(
        'res.users',
        required=True,
        readonly=True,
        default=lambda self: self.env.uid,
    )
    uploaded_at = fields.Datetime(
        readonly=True,
        default=fields.Datetime.now,
    )

    _sql_constraints = [
        ('unique_name_version_per_project',
         'UNIQUE(project_id, name, version)',
         'Skill name + version must be unique per project.'),
    ]
```

**ACL**（與 project_access role 對齊 §1.3 / §4）：

- `project_access` role `admin`：CRUD + state transition（draft → published → deprecated）
- `project_access` role `user`：read + draft create（自己 upload 的 skill）；不能 publish 別人的 skill
- `project_access` role `guest`：read-only（看名稱 / description / version，不能 download bundle）
- non-member：完全不可見

**新增 `ir.rule`**：

| Rule name | Group | Domain | Perms |
|-----------|-------|--------|-------|
| `skill_rule_project_admin` | base.group_user | `project_id.project_access_ids` walk（role=admin / owner，user_id=user.id） | R/W/C/U=True |
| `skill_rule_project_user` | base.group_user | `project_id.project_access_ids` walk（role=user，user_id=user.id） | R=True, W/C=True (only own draft), U=False |
| `skill_rule_project_guest` | base.group_user | `project_id.project_access_ids` walk（role=guest，user_id=user.id） | R=True only（且不可 read `bundle_attachment_id`，through binary field group restrict） |
| `skill_rule_admin_full` | base.group_system | `[(1, '=', 1)]` | R/W/C/U=True（後台維運） |

**對等 invariant**（仿 mcp-acl-6-rule-split pattern）：bundle attachment 的 ACL 必須對等 — `ir.attachment` 對 `res_model='woow_paas_platform.skill'` 的 rule 走相同 project_access walk（細節 §6 ACL 章）。

### §3.3 Storage backend（Q1 拍板用）

三選項 trade-off：

| 方案 | 物理 | v1 上手 cost | bundle 大小上限 | OCI semantic? | 維運 | 推薦 |
|------|------|-------------|----------------|--------------|------|------|
| **(a) ir.attachment**（**推薦**）| Odoo 內建（DB blob 或 filestore） | 零（Odoo 既有）| 預設 `ir.config_parameter` `web.binary_max_upload_size`（25 MB 預設）；可調 | 否 | 與 Odoo backup 一致；fetch 走 Odoo HTTP endpoint | ✓ |
| (b) OCI image registry | 包成 OCI image push 到既有 JCR | 高（要寫 push/pull 邏輯）| OCI 沒限制；JCR 配額由 admin 設 | 是 | 多一條 JCR 維運線；要管 image tag GC | ✗（留 v2）|
| (c) S3-compatible 物件儲存 | 外部 S3 endpoint | 中（新 credentials + SDK）| 物件儲存無實質限制 | 否 | 多一條 credentials 維運線 | ✗ |

**phase-planner 推薦 (a) ir.attachment**：

- 對齊既有 Odoo addon storage pattern（`ai_assistant` 已用 ir.attachment 存 chat upload）
- bundle 大小限制（單檔 25 MB）對 skill bundle 足夠（skill 預期 < 1 MB；25 MB 容忍 assets 含 binary fixture）
- Odoo backup / migration / GC 全部沿用既有機制
- ACL 走 ir.attachment standard，配對 §3.2 skill rule

**fetch 流程**（pod 端）：

1. Pod 啟動 + 收到 first tool_call referencing skill `<name>@<version>`
2. Pod 在 `/workspace/project-{pid}/skills/cache/<name>/<version>/` PVC subdir 找 cache → 命中則 verify `bundle.sha256` matches expected → use
3. cache miss：pod 呼 Odoo endpoint `GET /api/agent-runtimes/skill-bundle?project_hash={hash}&skill_name={name}&version={ver}` （新增 controller endpoint）
   - Odoo 端：query `skill` model + project_access check（pod 的請求需帶 short-lived bearer token，§5 protocol）→ return ir.attachment binary
4. Pod 寫到 `/workspace/project-{pid}/skills/cache/<name>/<version>/bundle.tar.gz` → verify SHA256 → extract → cache marker
5. Subprocess invoke from `/workspace/project-{pid}/skills/cache/<name>/<version>/`（== subprocess_runner cwd §6.4）

**Pod 對 Odoo 的 fetch auth**：透過 spawn-or-attach 時 Odoo 傳給 pod 的 short-lived signed token（內含 project_id + workspace_ids + 有效期 1h）— 細節 §5。

### §3.4 Versioning（Q2 拍板用）

**phase-planner 推薦：immutable bundle + version alias**

- 上傳新版 = 建 **新 `skill` row**（不同 `version`），不覆蓋舊 row
- `(project_id, name, version)` UNIQUE constraint（§3.2）保證一個 version 只有一份 bundle
- 舊版 row state 可從 `published` 轉 `deprecated`（仍可 invoke，UI 標記告警）→ `archived`（state=archived，搜索默認隱藏；不可 invoke）
- `is_latest` boolean 動態 maintain：每次 publish 新 version 時，把舊 `is_latest=True` 設 False，新 row 設 True；用 `@api.model_create_multi` + `write` hook + computed
- LangGraph tool reference 走 `skill_name@version`；若 reference `skill_name@latest` → runtime 查 `is_latest=True` row
- agent_template binding（§4）可選 `pin to version` or `follow latest`

**mutable 方案的反對**：

- 上傳新 bundle 覆蓋舊 → pod cache 與 Odoo 端 SHA256 不一致 → 強制 cache invalidation
- 中間態：pod 正執行 tool_call 用 v1 bundle，Odoo 端剛更新 v1.1 → pod 用 v1 跑完然後下次 v1.1 → 行為不一致
- LangGraph thread checkpoint 內存 tool_call args + version — mutable 下 replay tool_call 行為改變，影響 reproducibility

**immutable 方案的 caveat**：

- DB 持續成長（永久保留所有 version）→ §3.12 risk 提到
- mitigation：archived state 走 ir.attachment 標記 deprecation；30 day 後可由 cron 真刪 bundle binary（保留 metadata row）— §3.12 細化

### §3.5 Lazy fetch / cache 策略

**Mount layout**（per-project pod；canonical path 對齊 §6.2 filesystem mount table）：

```
/workspace/project-{pid}/skills/cache/                ← skill bundle cache root (PVC subdir)
├── <skill_name_1>/
│   ├── <version_a>/
│   │   ├── SKILL.md
│   │   ├── scripts/
│   │   └── assets/
│   ├── <version_b>/
│   │   └── ...
│   └── <version_a>.sha256                            ← integrity marker
├── <skill_name_2>/
│   └── ...
```

**Storage backend on PVC**：

- **Canonical 實體 path** = `/workspace/project-{pid}/skills/cache/<name>/<version>/`（與 §6.2 mount table + §6.4 subprocess_runner cwd 一致；cache.py extract 目標 == subprocess cwd）
- 本 doc 早期段落（§1.2 task brief 引述 ASCII / 概念敘述）用 `/skills/` 作 mountpoint 抽象簡寫；**實體 PVC 無獨立 `/skills/` K8s mount**，一律映射到 `/workspace/project-{pid}/skills/cache/`
- 不是獨立 PVC（與 `threads/` `shared/` 同 PVC 不同 subdir，避免多 mount 複雜度）
- subdir 內每 `<skill>/<version>/` 是 lazy fetch + extract 後留下

**Mount strategy 比較**：

| 方案 | 持久 | Pod 重啟 | Cold start | 推薦 |
|------|------|---------|------------|------|
| **PVC subdir**（**推薦**） | 是 | reap/resume 後保留 | 第一次 invoke 各 skill 才 fetch | ✓ |
| ConfigMap | 否（K8s ConfigMap 1 MB 上限）| 重啟保留 | 啟動時全部 mount | ✗（大 skill 不適合） |
| emptyDir tmpfs | 否 | 重啟丟 | 每次重啟都重 fetch | ✗（reap 後 resume 要重抓） |
| initContainer fetch | 是 | 重啟保留 | spawn 時 eagerly fetch 全部 skill = 慢 cold start | ✗ |

**Fetch policy**：

- 首次 invoke `<skill>@<version>` → cache miss → fetch → verify SHA256 → extract → invoke
- Subsequent invoke 同 version → cache hit → invoke
- 不同 version → 平行 cache，各佔 subdir
- archived skill state 在 Odoo 端 reject fetch（pod 回 tool_error event）

**cache eviction policy**：

- v1 不 evict（PVC 配額 2 Gi default 足夠 100+ skill bundle）
- 若 PVC 撞滿 → pod 內 IO error，agent log 標 disk_full；用戶手動 reset workspace 或升 PVC quota
- v2 評估 LRU eviction

**Skill 共享 PVC 與 thread checkpoint 衝突？**：

- `/workspace/project-{pid}/threads/` vs `/workspace/project-{pid}/skills/` — 同 PVC 不同 subdir，無衝突
- 同 project 多 pod（不該發生 — §2.7 一個 project 一 pod）的話會 race，但 spawn-or-attach idempotent 保證最多 1 pod

### §3.6 Per-project skill 配置

**Binding**（與 agent_template 細節 §4）：

- skill 屬於 project（`skill.project_id`）— 跨 project 不共享
- agent_template 內 reference skill name（M:N 透過 `agent_template.skill_ids = Many2many('woow_paas_platform.skill')`） — §4 細化
- pod 啟動時，Odoo 把該 project 的 published skill list snapshot 到 mcp.json 同 ConfigMap（或獨立 `skills.json`）內：

```json
{
  "skills": [
    {"name": "my-skill", "version": "0.1.0", "sha256": "...", "fetch_uri": "..."},
    {"name": "data-search", "version": "1.2.3", "sha256": "...", "fetch_uri": "..."}
  ]
}
```

- mcp.json 與 skills.json 在 pod 端**統一抽象**為 LangGraph 可用 tool list（§3.10）

**Skill 衝突命名**：

- 同 project 內 `(name, version)` UNIQUE，無衝突
- 跨 project 同 name 不同 project — 各自獨立（pod 物理隔離，看不到對方 skill）

### §3.7 Skill source

**v1**：

- 唯一 source = **user upload to Odoo**
- 路徑：
  - UI：Project Detail page 新增 「Skills」section（仿既有 MCP Servers section pattern）
  - 上傳 zip / tarball → Odoo controller 端 unzip + validate SKILL.md frontmatter + compute SHA256 + create `skill` row
  - 上傳 endpoint：`POST /api/support-projects/<id>/skills` action=upload（multipart form）
- Validation：
  - SKILL.md required + valid YAML frontmatter
  - `name` slug format（regex `^[a-z][a-z0-9-]*$`）
  - `description` ≤ 200 chars
  - bundle size ≤ 25 MB（沿用 Odoo binary upload 限制）
  - scripts/ 內 executable bit / shebang（v1 不檢查，留 v2）

**v2 hooks（不在本 epic implementation）**：

- Marketplace skill registry（external skill source）
- Skill auto-update（marketplace publish 觸發 cluster pull）
- Skill federation（cross-org skill 共享）— marketplace 蘊含

### §3.8 Signature / trust（Q3 拍板用）

**phase-planner 推薦：v1 SHA256 digest（integrity）only，無 cryptographic signature（trust）**

| 維度 | v1 | v2 |
|------|----|-----|
| **Integrity（防偷天換日）** | SHA256 digest 在 upload 時計算，pod fetch 後驗 | 同 v1 |
| **Trust（誰簽的）** | 不簽（user-trusted model） | GPG / cosign 簽 marketplace skill |
| **Tamper detection** | digest 不符 → reject fetch | 同 v1 + 簽章 invalid → reject |

**為什麼 v1 需要 SHA256 但不需要 trust**：

- **Integrity reason**：bundle 從 Odoo → pod 走 cluster network；若 ir.attachment 被 admin / SQL 直接動到 binary 內容但 SHA256 沒對應更新 → pod 跑被改的 bundle 但 Odoo log 沒紀錄；SHA256 digest 在 fetch path verify 是廉價防線
- **Trust 不需要 reason**：v1 skill = user 自己上傳，user 對自己上傳的 bundle 負責；同 project member 視為 co-trusted（§1.3 user-trusted model）；marketplace skill 才需要簽章證明來源
- **DB 內 `bundle_sha256` field 計算時機**：upload 時，Odoo controller 端 `hashlib.sha256(bundle_zip_bytes).hexdigest()`；ir.attachment 寫入後即 readonly
- **驗證時機**：pod fetch bundle → 解壓前 verify；不符 → tool_error event + Odoo log warning

### §3.9 Sandbox profile（Q4 拍板用）

**phase-planner 推薦：v1 bundle declare = metadata only；執行限制 100% 由 host enforce**

| 維度 | bundle SKILL.md 內 declare | Host pod-level enforce |
|------|---------------------------|----------------------|
| `timeout_seconds` | hint（用戶填）→ subprocess.run timeout 參數 | host cap（如 60s）優先 |
| `max_output_kb` | hint → buffer cut-off | host cap（如 256 KB）優先 |
| `allowed_egress: true` | hint → 不影響 NetworkPolicy（NetworkPolicy 是 pod-level） | 100% host enforce — bundle declare 不能放寬 |
| `requires_root: true` | **不允許**（v1 直接 reject upload）| pod runAsNonRoot=true 強制 |
| `parameters` JSON Schema | 用於 LangGraph tool schema | runtime 用，host 不檢 |

**為什麼 v1 bundle declare 不能放寬 host limit**：

- bundle 是 user-trusted 但**不是 admin-trusted**：user 上傳的 bundle 不能 escalate 自己權限
- 例：user 上傳 `allowed_egress: true` 不代表 NetworkPolicy 開洞；NetworkPolicy 是 admin（agent-runtime helm chart）控制
- bundle declare 在 v1 純粹是 **runtime hint + UI display + LangGraph schema**

**為什麼 declare 仍有價值（不直接 ignore）**：

- LangGraph tool schema 需要 `parameters`（不然 LLM 用 default single-string，行為退化）
- UI 顯示 `description / timeout / category` 提升 user 配置體驗
- v2 marketplace skill 可能引入 declared sandbox profile（trust based on signed bundle 後）— v1 留 hook

### §3.10 Skill vs MCP narrative（Q5 拍板用）

> 「Skill 與 MCP 是否共用 invocation pipeline / namespace」是 §3 留 §3.10 + §3.13 Q5 的關鍵設計問題。

**phase-planner 推薦：兩者在 LangGraph 層統一抽象為 tool；物理上分離；namespace 上分離**

| 維度 | Skill | MCP |
|------|-------|-----|
| **執行位置** | agent pod 內 subprocess（fork from LangGraph process） | 獨立服務 — **兩種形態**：(i) `sidecar_backed=True` → cloud_service sidecar pod（跨 namespace；隨 cloud_service 起伏）；(ii) `sidecar_backed=False` → external URL（任意，cluster.local 內外） |
| **通訊** | subprocess.run with stdin/stdout JSON | MCP protocol over HTTP/SSE（langchain-mcp-adapters） |
| **生命週期** | per-invocation 短生命（subprocess 起 → 跑 → 死） | long-lived service — sidecar-backed 隨 cloud_service；external 完全獨立 |
| **跨對話** | 同 pod 內所有 thread 共用 cache（PVC） | 全平台共用同一 mcp_server row；sidecar-backed 物理上由 cloud_service 內所有 project 共用 sidecar；external 由 url 提供方決定 |
| **Trust model** | user-trusted bundle（§1.2 sign-off） | external service trust（既有 mcp_server / mcp_tool ACL + Inv #11 dual-leg） |
| **Schema source** | SKILL.md frontmatter `parameters` JSON Schema | MCP `tools/list` 動態 discovery（既有 `mcp_server.action_sync_tools`） |
| **部署 / 註冊** | bundle upload to Odoo → `skill` row | helm deploy cloud_service + sidecar → `mcp_server` row（auto-create 已退場，UI shortcut 手動加） |
| **Invocation path** | LangGraph `StructuredTool._run()` → subprocess | LangGraph tool（langchain-mcp-adapters 包裝）→ MCP HTTP |
| **資源限制** | host pod cgroup（同 LangGraph main 共享）| MCP server 自己的 pod cgroup |
| **失敗模式** | subprocess timeout / non-zero exit → `tool_error` event | HTTP timeout / 5xx → `tool_error` event（既有 `_async_agent_stream` handle） |
| **Odoo model** | `woow_paas_platform.skill`（本 epic 新增） | `woow_paas_platform.mcp_server` + `mcp_tool`（refactor branch finalized） |
| **ACL** | project_access（§3.2 4 rule，含 admin/user/guest/admin_full） | 6-rule split + Inv #11 dual-leg（refactor branch） |

**統一抽象點**：LangGraph runtime 把兩者統一為 `BaseTool` 子類，agent 不感知差別 — LLM call `tool_X(args)` → runtime 解析是 skill 還是 mcp → dispatch 對應 invoker（subprocess 或 HTTP）。

**Namespace 分離原因**：

- 物理隔離：skill 在 agent pod 內 subprocess，mcp 在獨立 pod — K8s 層自然分離，不存在共用 namespace 概念
- Trust 不同：skill 是 user-trusted bundle，mcp 是 external service（不同信任模型）
- 生命週期不同：skill 是 per-invocation 短生命，mcp 是 long-lived — 共用 namespace 抽象會強迫一個適應另一個（妥協）
- ACL 不同：skill 對應 project_access 4 rule，mcp 對應 6-rule split — 共用會強迫 rule 合併 = ACL surface 變大

**反對共用 namespace 的具體 implication**：

- 若強迫 mcp_tool 跟 skill 在 LangGraph 內共名稱空間（e.g. tool name conflict），會撞既有 `langchain-mcp-adapters` 的 `tool_name_prefix=True` pattern
- prefix scheme：skill → `skill_<name>`，mcp → `mcp_<server>_<tool>` — runtime 用 prefix 區分；name conflict 自動避免

### §3.11 Affected files / new files（§3 範圍預測）

**新建（落地時）**：

- `src/models/skill.py` — `woow_paas_platform.skill` model
- `src/security/ir.model.access.csv` — 加 skill rows（admin/user/guest/admin_full）
- `src/security/ir_rules.xml` — 加 4 條 skill rule（仿 mcp 6-rule split pattern，但只 4 rule，無 sub-model 對等需求）
- `src/views/skill_views.xml` — 後台 form/list view
- `src/static/src/paas/pages/project-detail/sections/SkillsSection.{js,xml}` — 前端 section（仿 McpServersSection pattern）
- `src/static/src/paas/services/skill_service.js` — fetchSkills / uploadSkill / publishSkill API client
- `src/controllers/skills.py`（新檔，或加到既有 paas.py）— upload / list / download / state transition endpoints
- agent-runtime image source（`woow-paas-agent-runtime` repo）：
  - `runtime/skills/loader.py` — SKILL.md parse + StructuredTool wrap
  - `runtime/skills/cache.py` — lazy fetch + SHA256 verify + extract
  - `runtime/skills/subprocess_runner.py` — subprocess.run wrapper with timeout / env whitelist

**修改（落地時）**：

- `src/__manifest__.py` — bump version + data 加 skill views/security
- `src/models/__init__.py` — import skill
- `src/controllers/paas.py`（若 skill endpoint 內嵌）— 加 routes
- agent-runtime image：`runtime/agent_dispatcher.py`（新檔）— 統一 dispatch skill vs mcp tool

**不動**（保護 invariants）：

- `mcp_server` / `mcp_tool` schema 與 ACL（refactor branch finalized；skill 是並列 model，不互動）
- `cloud_service` 既有 lifecycle
- project schema 在 §3 範圍內不再動（§7 prereq 已處理）

### §3.12 Risks

| Risk | 嚴重度 | Mitigation | Rollback |
|------|-------|-----------|---------|
| User upload 大 bundle 灌爆 Odoo DB | 中 | ir.attachment 25 MB 限制 + per-project skill quota（如 50 個 skill / project，§3.13 拍板）| admin 後台 archive 舊 skill |
| Skill version 累積無止盡 | 低 | archived state 30 day 後 cron 真刪 binary（保留 metadata row 供 audit） | 手動 unarchive 暫不刪 |
| Skill 內 malicious script | 中 | v1 user-trusted accept；pod-level sandbox（§6 securityContext + NetworkPolicy）擋擴散範圍 | reset workspace；archive skill |
| SHA256 collision（理論可能） | 極低 | SHA256 抗碰撞強度足夠（2^128 birthday bound） | 重新 upload bundle 改 version |
| Fetch 失敗（pod 連不上 Odoo / Odoo 端 ACL 拒絕） | 中 | 3 次 retry + 指數 backoff；tool_error event 通知 LLM | user 手動 retry；admin 看 Odoo log |
| 同 skill 同 version concurrent upload race | 低 | `(project_id, name, version)` UNIQUE constraint；後到者 raise IntegrityError | 後到者 client 看 error 改 version |
| skill 跟 mcp_tool 名稱衝突（同 LangGraph namespace） | 低 | tool name prefix scheme（skill_*, mcp_*）天然分離 | runtime warn + skip duplicate |
| SKILL.md frontmatter 格式錯（user upload）| 低 | upload controller 端 YAML parse + JSON Schema validate → reject with detailed error | upload 失敗 frontend 顯示具體錯 |
| Bundle 解壓觸發 zip bomb / path traversal | 中 | upload server-side：sandbox extract + 限制總大小 + reject 含 `..` 的 archive entry | reject upload + log warning |
| Skill 上傳 user 離職 → skill 變 orphan | 低 | `uploaded_by_uid` 是 audit field 不影響 ownership；project_access admin 可繼續維護 | admin 直接編輯 / archive |

### §3.13 Open questions for leader sign-off（§3 escalation）

> 5 條核心拍板需求，phase-planner 推薦已 inline 在 §3.3 / §3.4 / §3.8 / §3.9 / §3.10 給出。**全部 sign-off 2026-05-21**。

1. ~~**[需 leader 拍板] Storage backend（§3.3）**~~ **[SIGNED-OFF 2026-05-21]** (a) ir.attachment（25 MB bundle limit、Odoo backup/GC 共用）
2. ~~**[需 leader 拍板] Versioning 模型（§3.4）**~~ **[SIGNED-OFF 2026-05-21]** immutable bundle + `is_latest` alias + archived 30 day cron 清 binary
3. ~~**[需 leader 拍板] Signature / digest（§3.8）**~~ **[SIGNED-OFF 2026-05-21]** v1 SHA256 digest (integrity) only，無 cryptographic signature（GPG / cosign 留 v2 marketplace）
4. ~~**[需 leader 拍板] Sandbox profile（§3.9）**~~ **[SIGNED-OFF 2026-05-21]** v1 bundle SKILL.md declare = metadata only；執行限制 100% host enforce（pod-level securityContext + NetworkPolicy + rlimit）
5. ~~**[需 leader 拍板] Skill / MCP namespace（§3.10）**~~ **[SIGNED-OFF 2026-05-21]** LangGraph 統一 BaseTool 抽象；物理 / Odoo model / ACL / namespace 全分離；tool name prefix `skill_*` vs `mcp_<server>_*` 區分

**§3 額外探出來但不阻塞 §3 sign-off 的事項**（不需立刻拍板，§4~§6 各節再處理）：

- 每 project skill 數量上限 / quota（§3.12 risk）— §4 與 per-project agent quota override 一起設計
- Skill bundle 含 `requirements.txt` 是否做 auto pip install — §4 蘊含 multi-skill 依賴衝突問題；v1 不做（runtime pre-baked 通用依賴）— §6 安全章再評估
- archived skill 30 day 後 cron 真刪 binary 的 cron schedule — §7 implementation 階段細化
- agent_template ↔ skill binding cardinality — §4 拍板（與 §4 預告 bullets 一致）
- skill 對 langchain 版本鎖定 — §4 與 agent_template metadata 一起設計
- LangGraph thread checkpoint 內 tool_call args 是否快取 skill 內部 state（影響 reproducibility）— §4 / §5 拍板

## §4 Agent template & binding model (agent ↔ skills ↔ MCPs)

> **Status**：Phase 1 — §4 draft submitted, awaiting sign-off
> 依賴：§1.3 project first-class（project_access ACL）、§2.12 agent_session model、§3 skill registry
> 5 條 [需 leader 拍板] 留 §4.11 escalate（已預警：template-project cardinality、權限粒度、shared memory 寫權限、計費歸屬；額外發現第 5 條 — agent_template 與既有 ai.assistant 並存策略）

### §4.0 Scope（§4 處理什麼、不處理什麼）

**§4 處理**：

- `woow_paas_platform.agent_template` Odoo model（取代部份 ai.assistant 的角色，但**不取代 ai.assistant 本身** — §4.11 Q5 拍板共存策略）
- agent_template ↔ project cardinality 拍板
- `woow_paas_platform.project_access` Odoo model（first-class ACL，仿 workspace_access pattern）
- Agent invoke 權限矩陣（admin / user / guest / non-member × 動作）
- Per-project shared memory 模型（thread-private + shared subdir + skill cache 寫權限）
- Skill / MCP binding 兩層 merge 路徑（per-project + per-agent-template）
- 計費歸屬（與 §2.8.1 對齊；primary_workspace 概念落地點）
- Per-project agent quota override（存 agent_session field，**不動 project schema**）

**§4 不處理**（留後續章節）：

- WS / SSE protocol schema → §5
- LangGraph thread checkpoint 內部結構與 cross-thread state shape → §5（與 transport 一起）
- Skill subprocess sandbox 細部約束 → §6
- 既有 `ai_assistant.py` controller / `discuss_channel.py` `message_post` override 改動順序 → §7
- ai.config / ai_client.py 重寫切割線（§1.1 Q6 已 sign-off）→ §7
- Migration（既有 `default_ai_assistant_id` config_parameter 怎麼搬到 project-level binding） → §7

### §4.1 `woow_paas_platform.agent_template` Odoo model

```python
class AgentTemplate(models.Model):
    _name = 'woow_paas_platform.agent_template'
    _description = 'Agent runtime template (system prompt + tool binding)'
    _order = 'is_public desc, name'

    # Identity
    name = fields.Char(
        string='Template Name',
        required=True,
        index=True,
        help='Slug; unique across instance for system / public templates, '
             'unique per (project_ids bound set) for private templates',
    )
    title = fields.Char(string='Display Title')
    description = fields.Text()

    # LangGraph runtime config
    system_prompt = fields.Text(
        string='System Prompt',
        help='Base system prompt prepended to all LangGraph thread; '
             'cloud_service context (§1.1 discuss_channel._get_cloud_service_context) '
             'still injected at runtime per channel',
    )
    model_config_id = fields.Many2one(
        'ai.config',
        string='LLM Provider Config',
        required=True,
        help='Reference to existing ai.config (OpenAI-compatible)',
    )
    model_overrides = fields.Text(
        string='Model Param Overrides (JSON)',
        help='Optional JSON: { "model": "...", "temperature": 0.7, "max_tokens": 4096 } '
             'overrides ai.config defaults for this template',
    )

    # Tool binding (subset within project scope; §4.6 merge logic)
    skill_ids = fields.Many2many(
        'woow_paas_platform.skill',
        relation='agent_template_skill_rel',
        string='Bound Skills',
        help='Empty = inherit all published skills from bound project(s); '
             'non-empty = whitelist subset',
    )
    mcp_server_ids = fields.Many2many(
        'woow_paas_platform.mcp_server',
        relation='agent_template_mcp_server_rel',
        string='Bound MCP Servers',
        help='Empty = inherit all project mcp_server_ids + scope=system; '
             'non-empty = whitelist subset (must be subset of project allowed set)',
    )

    # Project binding (M:N — §4.2 sign-off)
    project_ids = fields.Many2many(
        'project.project',
        relation='agent_template_project_rel',
        string='Bound Projects',
        help='Which projects can use this template; empty + is_public=False = orphan / draft only',
    )
    is_public = fields.Boolean(
        string='Public (System) Template',
        default=False,
        help='If True, ignore project_ids ACL — all project member can use; '
             'system templates managed by admin (base.group_system)',
    )

    # Lifecycle
    state = fields.Selection([
        ('draft', 'Draft'),
        ('published', 'Published'),
        ('deprecated', 'Deprecated'),
        ('archived', 'Archived'),
    ], default='draft', required=True, index=True)

    # Audit
    created_by_uid = fields.Many2one(
        'res.users',
        readonly=True,
        default=lambda self: self.env.uid,
    )
    created_at = fields.Datetime(readonly=True, default=fields.Datetime.now)

    @api.constrains('is_public', 'project_ids')
    def _check_visibility_consistency(self):
        """is_public=False AND empty project_ids = orphan template (only draft allowed)."""
        for t in self:
            if not t.is_public and not t.project_ids and t.state != 'draft':
                raise ValidationError(
                    "Private template '%s' must bind at least one project "
                    "before publish." % t.name
                )

    @api.constrains('mcp_server_ids', 'project_ids')
    def _check_mcp_subset(self):
        """Bound mcp_server must be subset of (project.mcp_server_ids ∪ scope=system)."""
        for t in self:
            if not t.mcp_server_ids:
                continue
            allowed_user_scope = t.project_ids.mapped('mcp_server_ids').ids
            allowed_system = self.env['woow_paas_platform.mcp_server'].sudo().search([
                ('scope', '=', 'system'),
            ]).ids
            allowed_set = set(allowed_user_scope) | set(allowed_system)
            for mcp in t.mcp_server_ids:
                if mcp.id not in allowed_set:
                    raise ValidationError(
                        "MCP server '%s' is not bound to any of template's projects "
                        "and is not system-scope." % mcp.name
                    )

    @api.constrains('project_ids')
    def _check_project_ids_grantor_authority(self):
        """HC-3 fix — grantor (current env.user) must have admin/owner role on every new project_id.

        Without this Python constraint, an attacker who is admin on project A can:
          1. Create template T with project_ids=[A] (ir.rule allows: A is current user's domain)
          2. write(T, project_ids=[(4, victim_B)]) — ir.rule 'any' over M2M still evaluates True
             because A ∈ project_ids satisfies admin walk → write proceeds
          3. T now bound to B → cross-project leak (T usable by B members via agent_template_rule_project_*)
        Pattern alignment: same shape as `project_access._check_at_least_one_owner` — Python
        constraint covers ir.rule blind spot when M2M domain evaluation does not enforce
        per-element grantor authority. Reporter phase should抽 pattern
        `constraint-input-trust-expansion` (same family as PR #54 HC-2 silent IDOR).
        """
        SUPERUSER_ID = 1
        if self.env.uid == SUPERUSER_ID or self.env.user.has_group('base.group_system'):
            return  # bypass — admin / migration / cron all need to write across projects
        grantor_partner = self.env.user.partner_id
        for t in self:
            for project in t.project_ids:
                has_authority = self.env['woow_paas_platform.project_access'].sudo().search_count([
                    ('project_id', '=', project.id),
                    ('partner_id', '=', grantor_partner.id),
                    ('role', 'in', ('owner', 'admin')),
                ]) > 0
                if not has_authority:
                    raise ValidationError(
                        "Cannot bind template '%s' to project '%s': "
                        "you must have admin or owner role on the target project."
                        % (t.name, project.name)
                    )
```

**ACL**（4 rule split，對齊 §3.2 skill pattern）：

| Rule | Group | Domain | Perms |
|------|-------|--------|-------|
| `agent_template_rule_public_readonly` | base.group_user | `is_public=True` | R only |
| `agent_template_rule_project_admin` | base.group_user | `project_ids.project_access_ids` walk（role=admin/owner，user_id=user.id） | R/W/C/U |
| `agent_template_rule_project_user` | base.group_user | `project_ids.project_access_ids` walk（role=user，user_id=user.id） | R only |
| `agent_template_rule_admin_full` | base.group_system | `[(1, '=', 1)]` | R/W/C/U |

**對等 invariant**：仿 [[mcp-acl-6-rule-split]] pattern — agent_template 是 model owner，其 M2M 透過 model 本身的 ACL 控制（不需 sub-model rule 對等，因為 M2M 不是 sub-model）。

### §4.2 agent_template ↔ project cardinality（Q1 拍板用）

**phase-planner 推薦：A — M:N**

三選項 trade-off：

| 方案 | Cardinality | 共享性 | 配置成本 | Per-project customization | 推薦 |
|------|-------------|-------|---------|--------------------------|------|
| **A (推薦)** | template **M:N** project | 同 template 給多 project 用；同 project 用多 template（user 切換 agent mode）| 中 — 透過 admin 配置 binding | 透過 model_overrides + per-project skill subset 補足 | ✓ |
| B | template **1:N** project（template 是 global / per-org） | 同 template 多 project 用（單向 fanout）；同 project 只一 template | 低 — 不分 per-project | 不可 — project 不能客製 | ✗ |
| C | template **1:1** project | 每 project 一 template（緊耦合）| 高 — 每新 project 都要建 template | 完全 isolated | ✗ |

**選 A 的理由**：

- 對齊既有 ai.assistant 設計理念（ai.assistant 是 global，project 引用 single via `default_ai_assistant_id` config — A 升級為 explicit M:N）
- 系統可有「公司預設 agent」(`is_public=True`) + 每 project 可自建「team-specific agent」(`project_ids` M:N binding)
- M:N 是「super-set」— 將來若要退到 1:N 比 1:1 升 M:N 簡單

**選 B 的反對**：

- 同 project 只能一 template → 用戶要切換「研究 agent」vs「coding agent」要去 project setting 改 template → 破壞「agent 是工具，多種工具並存」的心智模型
- 無 per-project customization → 多租戶需求迅速崩潰

**選 C 的反對**：

- 每 project 強迫建 template → onboarding 摩擦大
- 跨 project 共享 template 不可能 → 無公司預設 agent 概念
- ai.assistant 既有設計也是 global，C 反而退化

**M:N constraint**（§4.1 已寫）：

- template.mcp_server_ids 必須是 project allowed set 的 subset（cross-project user-scope MCP 不能透過 template 借用）
- template.skill_ids 同理（跨 project 不共享 skill）

### §4.3 `woow_paas_platform.project_access` Odoo model

對齊既有 `workspace_access` 結構（line by line）：

```python
class ProjectAccess(models.Model):
    _name = 'woow_paas_platform.project_access'
    _description = 'Project Member Access (first-class)'
    _order = 'role desc, create_date desc'
    _rec_name = 'partner_id'

    # Core fields
    project_id = fields.Many2one(
        'project.project',
        required=True,
        ondelete='cascade',
        index=True,
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Member',
        required=True,
        ondelete='cascade',
        index=True,
        help='Partner reference (not user_id) for consistency with workspace_access D3=A idiom',
    )
    user_id = fields.Many2one(
        'res.users',
        string='User',
        compute='_compute_user_id',
        store=True,
        index=True,
        help='Derived from partner_id; used in ir.rule walk',
    )

    # Role (對齊 workspace_access ROLE_SELECTION)
    role = fields.Selection([
        ('owner', 'Owner'),
        ('admin', 'Admin'),
        ('user', 'User'),
        ('guest', 'Guest'),
    ], string='Role', required=True, default='user', index=True)

    # Audit
    invited_by_uid = fields.Many2one(
        'res.users',
        readonly=True,
        default=lambda self: self.env.uid,
    )
    invited_at = fields.Datetime(readonly=True, default=fields.Datetime.now)

    _sql_constraints = [
        ('unique_partner_per_project',
         'UNIQUE(project_id, partner_id)',
         'A partner can have only one access record per project.'),
    ]

    @api.depends('partner_id')
    def _compute_user_id(self):
        """Map partner_id → user_id; partner without user → null (treat as orphan)."""
        for rec in self:
            users = self.env['res.users'].sudo().search([
                ('partner_id', '=', rec.partner_id.id),
            ], limit=1)
            rec.user_id = users.id if users else False

    @api.constrains('role', 'project_id')
    def _check_at_least_one_owner(self):
        """Each project must have at least one role=owner project_access row."""
        for rec in self:
            if rec.role != 'owner':
                continue
            other_owners = self.search_count([
                ('project_id', '=', rec.project_id.id),
                ('role', '=', 'owner'),
                ('id', '!=', rec.id),
            ])
            # 不阻止 create；阻止「unlink 最後一個 owner」(in unlink override)
```

**Role hierarchy**（沿用 workspace_access ROLE_HIERARCHY）：

```python
ROLE_HIERARCHY = ['guest', 'user', 'admin', 'owner']  # low → high
ASSIGNABLE_ROLES_BY_OWNER = ['admin', 'user', 'guest']
ASSIGNABLE_ROLES_BY_ADMIN = ['user', 'guest']  # admin 不能升別人為 admin/owner
```

**Project 自動 bootstrap owner**：

- `project_project.create()` override：建 project 時自動建 project_access row with `role=owner, partner_id=env.user.partner_id`
- 這在 §7 migration 對既有 project 做 backfill（既有 project 沒 project_access，migration 拿 cloud_service.workspace_id.access_ids 內 role=owner 的 partners 灌進去）

**ACL**（仿 workspace_access pattern，4 rule + sub-model 對等）：

| Rule | Group | Domain | Perms |
|------|-------|--------|-------|
| `project_access_rule_member_read` | base.group_user | `project_id.project_access_ids` 內 partner_id=user.partner_id | R only |
| `project_access_rule_owner_admin_manage` | base.group_user | `project_id.project_access_ids` 內 partner_id=user.partner_id AND role IN ('owner', 'admin') | R/W/C/U（admin 不能 unlink owner — Python constraint 守） |
| `project_access_rule_self` | base.group_user | `partner_id=user.partner_id`（自己看自己） | R only |
| `project_access_rule_admin_full` | base.group_system | `[(1, '=', 1)]` | R/W/C/U |

### §4.4 Agent invoke 權限矩陣（Q2 拍板用）

**phase-planner 推薦**：

| Action | owner | admin | user | guest | non-member |
|--------|-------|-------|------|-------|-----------|
| **chat / invoke** |  |  |  |  |  |
| Invoke agent (send message → trigger spawn-or-attach) | ✓ | ✓ | ✓ | ✗ | ✗ |
| Create new thread / channel | ✓ | ✓ | ✓ | ✗ | ✗ |
| Read own thread history | ✓ | ✓ | ✓ | ✓ | ✗ |
| Read others' thread (same project) | ✓ | ✓ | ✗ | ✓ | ✗ |
| Delete own thread | ✓ | ✓ | ✓ | ✗ | ✗ |
| Delete others' thread | ✓ | ✓ | ✗ | ✗ | ✗ |
| **skill / tool** |  |  |  |  |  |
| Upload draft skill (state=draft) | ✓ | ✓ | ✓ | ✗ | ✗ |
| Publish skill (state=published) | ✓ | ✓ | ✗ | ✗ | ✗ |
| Archive skill | ✓ | ✓ | ✗ | ✗ | ✗ |
| **template / binding** |  |  |  |  |  |
| Create / modify template | ✓ | ✓ | ✗ | ✗ | ✗ |
| Modify template ↔ project binding | ✓ | ✓ | ✗ | ✗ | ✗ |
| Modify template.mcp_server_ids / skill_ids | ✓ | ✓ | ✗ | ✗ | ✗ |
| **project / quota** |  |  |  |  |  |
| Set agent quota override（§4.8）| ✓ | ✓ | ✗ | ✗ | ✗ |
| Reset workspace state（清 PVC） | ✓ | ✓ | ✗ | ✗ | ✗ |
| Add / remove project member | ✓ | ✓ (cannot remove owner) | ✗ | ✗ | ✗ |
| Change member role | ✓ (any) | ✓ (limited to user/guest assign) | ✗ | ✗ | ✗ |
| Modify project.cloud_service_ids binding | ✓ | ✓ | ✗ | ✗ | ✗ |
| **mcp / external integration** |  |  |  |  |  |
| Add sidecar MCP / external MCP（§3.10 + refactor branch UI shortcut）| ✓ | ✓ | ✓ (own create) | ✗ | ✗ |
| Modify others' MCP | ✓ | ✓ | ✗ | ✗ | ✗ |

**Guest read-only 細部**：

- 看 channel / thread history 不需要「send message 權限」— guest 可在 SSE EventSource 訂閱看流但 POST 訊息端點 reject
- 看 skill / template name 但不能 download bundle binary（透過 ir.attachment ACL → bundle_attachment 走 group restrict）
- 看 mcp_server name 但不能看 api_key（既有 `groups='base.group_system'` 守住）
- 不能觸發 spawn-or-attach（pod 不為 guest 起來；若 guest 是 project 內唯一線上 user，pod 不 active — 這是 acceptable 行為）

**Non-member**：完全不可見（既有 `_check_channel_access` + 新加 `_check_project_access` 串接擋下；§7 落地時改）

### §4.5 Per-project shared memory model（Q3 拍板用）

**PVC layout**（per-project pod）：

```
/workspace/project-{pid}/
├── threads/
│   └── {channel_id}/
│       ├── checkpoint.json          ← LangGraph thread state
│       └── tool_invocations.jsonl   ← audit log
├── shared/
│   ├── memory.json                  ← LLM 自主寫的 long-term memory
│   ├── notes/                       ← user 上傳檔案 / 跨 thread 引用
│   └── exports/                     ← skill 產出檔
└── skills/
    └── cache/<name>/<version>/      ← §3.5 skill bundle cache
```

**寫權限矩陣（phase-planner 推薦）**：

| 路徑 | owner | admin | user | guest | runtime auto |
|------|-------|-------|------|-------|--------------|
| `threads/{channel_id}/` | invoker 自己寫（thread = invoker 的 chat）；owner/admin 可 unlink 整 thread | 同 owner | 同上（只自己） | ✗（不能 invoke） | ✓（per tool_call append） |
| `shared/memory.json` | R/W | R/W | R only | R only | ✓（LLM 工具 set_memory） |
| `shared/notes/` | R/W/U | R/W/U | R/W (own files) | R only | — |
| `shared/exports/` | R/W/U | R/W/U | R | R only | ✓ (skill 產出寫入) |
| `skills/cache/` | — (不直接動) | — | — | — | ✓（runtime 自動 lazy fetch） |

**為什麼 user 對 shared/memory.json 只可讀**：

- LLM 自主 long-term memory 是 agent runtime 行為（透過 tool 寫入），user 不該手動編輯（會打亂 agent context coherence）
- 例外：admin 可手動 reset memory（透過 admin tool，§4.4 reset workspace state）

**為什麼 guest 對 shared/exports/ 只可讀**：

- guest 可看 skill 產出（用於 review / 學習）
- 不能 invoke = 不能觸發 skill = 不會寫入 exports（runtime 不為 guest 起 tool）

**Enforcement 機制**：

- Odoo controller 層：在送 message / spawn-or-attach / fetch-file endpoint check project_access role
- Pod 層：runtime 收 WS message 帶 invoker role (in spawn-or-attach payload 內已 freeze 該 thread 的 invoker)，tool dispatch 前 check role × tool permission；不過 v1 收斂為 **「role check 在 Odoo 端 gate，pod 端視為 trusted invoke」**（pod 不重複 check role；避免 race）
- 例外：reset workspace state（admin only）是 pod 端 admin endpoint，需獨立 auth check

### §4.6 Skill / MCP binding 兩層 merge 路徑

**兩層 binding source**：

```
Layer 1: per-project
  - project.skill_ids (M2M via §3.2 skill.project_id reverse)
    [但 skill model 是 M2O project_id, 所以是 reverse O2M]
  - project.mcp_server_ids (refactor branch 既有 O2M)

Layer 2: per-agent-template
  - template.skill_ids (M2M)
  - template.mcp_server_ids (M2M)
```

**Merge logic（pod 啟動時）**：

```python
def resolve_runtime_tools(project, template):
    # MCP servers
    if template.mcp_server_ids:
        mcp_set = template.mcp_server_ids
        # constraint already enforced: mcp_set ⊆ project user-scope ∪ scope=system (§4.1)
    else:
        mcp_set = project.mcp_server_ids | env['woow_paas_platform.mcp_server'].search([
            ('scope', '=', 'system'), ('active', '=', True),
        ])
        # 預設 = 所有 project 可見的 MCP

    # Skills
    if template.skill_ids:
        skill_set = template.skill_ids.filtered(
            lambda s: s.project_id == project and s.state == 'published'
        )
    else:
        skill_set = env['woow_paas_platform.skill'].search([
            ('project_id', '=', project.id),
            ('state', '=', 'published'),
            ('is_latest', '=', True),
        ])
        # 預設 = 該 project 所有 latest published skill

    return mcp_set, skill_set
```

**送進 pod 的 ConfigMap snapshot**：

```json
{
  "mcp_servers": [
    {"name": "...", "url": "...", "transport": "...", "headers": {...}, "scope": "user|system"}
  ],
  "skills": [
    {"name": "...", "version": "...", "sha256": "...", "fetch_uri": "...", "metadata": {...}}
  ],
  "system_prompt": "...",
  "model_config": {...}
}
```

**Template 切換的 thread 影響**：

- 同 channel 切 template → 不清 thread checkpoint（thread state 保留）
- LangGraph 用新 template 的 system_prompt + tool set 繼續 thread
- LLM context 內已有歷史 turn（用舊 template 跑的）— 新 template 對歷史 turn 的 tool_call 可能 reference 不存在的 tool → runtime 標 `tool_unavailable` event，user 看 warning
- v1 簡單做法：UI 切 template 時 show warning「將使用新 agent 模式繼續對話，部分歷史工具可能不可用」

### §4.7 計費歸屬 — primary_workspace 概念（Q4 拍板用）

**Problem statement**：project.cloud_service_ids 可跨 workspace（§1.3）→ agent quota 費用算給誰？

**phase-planner 推薦：(b) 存 `agent_session.primary_workspace_id`（agent_session-level snapshot；不污染 project schema）**

三選項 trade-off：

| 方案 | 存哪 | 動 project schema | drift（project.workspace_ids 變動）處理 | 推薦 |
|------|------|------------------|----------------------------------|------|
| (a) `project.primary_workspace_id` field | project 上 | 是（本 epic 已 in-scope，但仍是擴大）| project owner 手動改；不一致時用戶可見 | ✗（避免擴大 project schema） |
| **(b)（推薦）** `agent_session.primary_workspace_id` | agent_session 上 | 否 | spawn-or-attach 時 freeze；workspace_ids 變動透過 sync-workspaces 觸發 admin 確認 | ✓ |
| (c) Derived: `project.workspace_ids[0]` | 不存 | 否 | first ordering 不穩定（M2M 順序非保證） | ✗ |

**選 (b) 的具體機制**：

- `agent_session` 新增 field `primary_workspace_id = fields.Many2one('woow_paas_platform.workspace')`
- spawn-or-attach 時 Odoo 端：若 agent_session 已有 `primary_workspace_id` → 用既有；否則 default 取 `project.cloud_service_ids[0].workspace_id`
- admin 在 Odoo project setting UI 可顯式選 / 改（透過 form view）
- 改 primary 變動是計費動作 → 觸發 audit log + workspace owner 通知

**費用模型（v1 baseline）**：

- agent quota cost = `primary_workspace.plan.agent_quota_cost_per_unit × usage_units`
- usage_units 計算：pod active hours（粗糙 v1）；token consumption（精細 v2）
- secondary workspace（project.workspace_ids 內非 primary 的）只享 NetworkPolicy egress access；零分攤
- billing cycle 月結，agent quota cost 加進 primary_workspace 的 monthly invoice（沿用 sh_subscription_wallet）

**Drift 處理**：

- `project.workspace_ids` 變動（add / remove cloud_service 蘊含 add / remove workspace）→ 透過 §2.12 cron drift detection 已偵測
- 若 primary_workspace 被 remove from project.workspace_ids（cloud_service 全部跨 workspace remove 後）→ pod 在 active 期間繼續用既有 primary（billing 不中斷）；下次 reap → resume cycle 時，admin 必須改 primary（不然 spawn-or-attach reject with `primary_workspace_no_longer_bound`）

**Edge case**：

- Project 第一次建立時還沒綁任何 cloud_service → `primary_workspace_id = NULL` → agent 不可 spawn（除非綁至少一個 cloud_service）— v1 enforce
- Project 跨 workspace 但 primary_workspace 退訂 PaaS → 仍走 primary（fail-safe to user notification）；admin 必須改

### §4.8 Per-project agent quota override

**設計（不動 project schema，存 agent_session）**：

```python
# agent_session 上 (§2.12 model 補充 fields)
quota_override_cpu = fields.Integer(
    string='CPU Quota Override (cores)',
    help='Default = ir.config_parameter.agent_quota_cpu_per_project; '
         'override only set by owner/admin via UI',
)
quota_override_memory_gi = fields.Integer(string='Memory Quota Override (Gi)')
quota_override_storage_gi = fields.Integer(string='Storage Quota Override (Gi)')

@api.depends('quota_override_cpu')
def _compute_effective_quota_cpu(self):
    """Use override if set, else ir.config_parameter default."""
    default_cpu = int(self.env['ir.config_parameter'].sudo().get_param(
        'woow_paas_platform.agent_quota_cpu_per_project', '2',
    ))
    for rec in self:
        rec.effective_quota_cpu = rec.quota_override_cpu or default_cpu
```

**Override 設置流程**：

- owner / admin 在 Odoo project setting UI 顯式設 override（透過 form view）
- 修改後寫 audit log；通知 primary_workspace owner（影響計費）
- next spawn-or-attach 時 paas-operator 收到新 namespace ResourceQuota（透過 update_namespace endpoint — 新增 if needed）
- 既有 ACTIVE pod 不熱更新 quota（K8s ResourceQuota 是 namespace-level，新 limits 只對新 pod 生效）

**Override 受 plan 上限約束**：

- `primary_workspace.plan.agent_max_quota_cpu` 是硬上限
- override 超過 plan max → form view validation reject
- plan 升級時自動放寬上限；plan 降級時若既有 override > new max → cron warning + admin 手動調

### §4.9 Affected files / new files（§4 範圍預測）

**新建（落地時）**：

- `src/models/agent_template.py` — §4.1 model
- `src/models/project_access.py` — §4.3 model
- `src/views/agent_template_views.xml` — backend form / list
- `src/views/project_access_views.xml` — backend form / list
- `src/security/ir.model.access.csv` — agent_template + project_access ACL rows
- `src/security/ir_rules.xml` — agent_template 4 rule + project_access 4 rule
- `src/static/src/paas/pages/project-detail/sections/AgentTemplateSection.{js,xml}` — frontend
- `src/static/src/paas/pages/project-detail/sections/ProjectAccessSection.{js,xml}` — frontend
- `src/static/src/paas/services/agent_template_service.js` — API client
- `src/static/src/paas/services/project_access_service.js` — API client
- `src/controllers/projects.py`（或 paas.py 內）— agent_template + project_access endpoints

**修改（落地時）**：

- `src/__manifest__.py` — bump version + data 加 views/security
- `src/models/__init__.py` — import
- `src/models/agent_session.py`（§2.12 補加 fields）：
  - `primary_workspace_id`
  - `quota_override_cpu` / `quota_override_memory_gi` / `quota_override_storage_gi`
  - `effective_quota_*` computed
  - `agent_template_id` 已在 §2.12 列出，不變
- `src/models/project_project.py`（§7 prereq 已動 schema；本 §4 不再加 field）：
  - 加 `project_access_ids = One2many('woow_paas_platform.project_access', 'project_id')` 反向關聯
  - 加 `_default_create_project_access` hook（create project 時自動建 owner role）
- `src/controllers/ai_assistant.py`（§7 範圍，但 §4 ACL gate 在此 controller 加）：
  - `_check_channel_access` 串接 `_check_project_access`（既有 method 已 fallback workspace_access，新加 project_access leg）

**不動**（保護 invariants）：

- `mcp_server` / `mcp_tool` schema / ACL（refactor branch finalized）
- `cloud_service` lifecycle
- workspace_access model（並存，§4.3 project_access 是新增 first-class ACL；workspace_access 仍處理 workspace 自身權限）
- 既有 ai.assistant model（**重要**：§4.11 Q5 拍板共存策略）

### §4.10 Risks

| Risk | 嚴重度 | Mitigation | Rollback |
|------|-------|-----------|---------|
| 既有 ai.assistant 與新 agent_template 雙寫漂移 | 高 | §4.11 Q5 拍板共存策略；migration 階段固定 mapping | 兩 model 並存，rollback 切回 ai.assistant single source |
| Template 修改影響所有 binding project（live update） | 中 | UI 顯示 binding count；改 published template 觸發 confirmation；audit log | template 改回；既有 pod 不熱重載（next spawn 才生效） |
| Template 引用 archived skill / deprecated mcp_server | 中 | runtime invoke 時 filter archived/deprecated；標 `tool_unavailable` event | template owner 手動 unbind |
| Project_access 與 workspace_access 不一致導致 ACL 混亂 | 中 | 兩層 ACL 解耦（§1.3）；agent invoke 只看 project_access；workspace 操作只看 workspace_access | project_access 顯式管理；不假設從 workspace_access 推導 |
| 最後一個 owner 被 unlink 導致 project 孤立 | 中 | project_access unlink override：check role=owner count > 1 才允許 unlink | admin 後台手動補 |
| primary_workspace 退訂 PaaS 但 project 還活著 | 中 | spawn-or-attach reject + 通知 owner 改 primary | 改 primary or unbind 該 workspace |
| Guest invoke 嘗試 bypass ACL | 高 | ACL 兩層 lock-step（Odoo controller + pod 端 invoke gate）；參考 [[two-layer-acl-lockstep]] pattern | 補 ACL；audit log |
| 不同 template 切換 thread 後 LLM 行為混亂 | 低 | UI warning；historical tool_call 標 `tool_unavailable` event | user 重啟 thread |
| agent_template 升 deprecated 但 binding project 仍在用 | 中 | UI 顯示 deprecated 標記；不強制中斷既有 pod；新 spawn-or-attach reject | unbind / 換 template |
| Quota override drift（plan 降級後 override > max） | 低 | cron 每天掃；超限給 warning email 給 admin | admin 手動調 override |

### §4.11 Open questions for leader sign-off（§4 escalation）

> 5 條核心拍板需求。phase-planner 推薦已 inline 在 §4.2 / §4.4 / §4.5 / §4.7 給出；第 5 條（既有 ai.assistant 並存策略）是 §4 探出來的額外拍板。**全部 sign-off 2026-05-22**。

1. ~~**[需 leader 拍板] Agent template ↔ project cardinality（§4.2）**~~ **[SIGNED-OFF 2026-05-22]** A: M:N（template 可給多 project 用；project 可選多 template）
2. ~~**[需 leader 拍板] Agent invoke 權限矩陣（§4.4）**~~ **[SIGNED-OFF 2026-05-22]** owner/admin 全權；user invoke + thread CRUD + draft skill upload；guest read-only；non-member 完全阻擋
3. ~~**[需 leader 拍板] Shared memory 寫權限（§4.5）**~~ **[SIGNED-OFF 2026-05-22]** threads = invoker / memory.json admin R/W + user R / notes/exports admin R/W/U + user R/W own + guest R
4. ~~**[需 leader 拍板] 計費歸屬 primary_workspace（§4.7）**~~ **[SIGNED-OFF 2026-05-22]** (b) 存 `agent_session.primary_workspace_id`（不動 project schema）+ spawn-or-attach freeze + admin UI 改 + drift 偵測
5. ~~**[需 leader 拍板 — §4 新發現] agent_template 與既有 ai.assistant 並存策略**~~ **[SIGNED-OFF 2026-05-22]** (i) 並存 + `default_ai_assistant_id` config_parameter fallback（agent_template 未設時走舊路徑）

**§4 額外探出來但不阻塞 §4 sign-off 的事項**（不需立刻拍板，§5~§7 各節再處理）：

- LangGraph thread checkpoint 切 template 時的 schema migration（同 thread 切不同 template，tool set 變動的記憶處理）— §5 拍板
- `agent.assistant.context_id` 既有的 system prompt source（PR #46 之前）vs 新 `agent_template.system_prompt` 取代邏輯 — §7 拍板
- Pod 端如何即時收到 template 改動（hot reload vs cold reload）— §5 拍板（與 sync-workspaces 類似機制）
- ai.config 內 api_key 的 group_system 守線在 per-project agent_template binding 下不變（user 看不到 api_key）— 確認 §1.3 invariant 守住，**不阻塞** § 4 sign-off
- Backend admin 後台對 system template (is_public=True) 的特殊 UI — 留 implementation 階段
- agent_template `model_overrides` JSON validation schema — 留 implementation 階段

## §5 Transport (Odoo ↔ Pod, WS/SSE protocol)

> **Status**：Phase 1 — §5 draft submitted, awaiting sign-off
> 依賴：§1.2 第二輪 sign-off (frontend SSE 不動)、§2.11 paas-operator endpoints、§4 agent_template + project_access
> 5 條 [需 leader 拍板] 留 §5.11 escalate（已預警：envelope schema 版本、auth 機制、mcp.json reload、agent_session state sync、額外 WS connection 生命週期）

### §5.0 Scope（§5 處理什麼、不處理什麼）

**§5 處理**：

- 兩段 transport 架構（Browser ↔ Odoo SSE 不動；Odoo ↔ Pod outbound WS 新增）
- WS message envelope JSON schema + 15 types + 序列保證
- Odoo ↔ Pod auth 機制（HS256 bearer token + shared secret）
- ConfigMap mcp.json reload 策略（hot reload via inotify + WS push）
- agent_session ↔ paas-operator state sync 機制（webhook + 輪詢 fallback）
- Odoo `controllers/ai_assistant.py` SSE generator rewrite pseudocode
- Tool set 變動處理（§4.6 蘊含的 `tool_unavailable` event 細化）
- Pod 端 FastAPI WS server 結構 + concurrent connection 處理

**§5 不處理**（留後續章節）：

- NetworkPolicy / securityContext 細節 → §6
- WS 流量加密細節（TLS terminator）→ §6 + deployment
- Subprocess sandbox 細部約束 → §6
- 既有 `ai_assistant.py` controller 改動順序 + `discuss_channel.py` 同步分支刪除時機 → §7
- 跨 Odoo worker 內 WS connection share（worker pool sizing 影響）→ deployment / SRE

### §5.1 兩段 transport 架構

```
┌────────────┐         ┌──────────────────────────────┐         ┌────────────────────┐
│  Browser   │  SSE    │  Odoo controller             │  WS     │  Agent Pod         │
│ (EventSource)        │  (/api/ai/stream/<channel>)  │         │  (FastAPI WS:8000) │
│            │◄────────┤  ↕ async_to_sync 橋接        │◄───────►│  /v1/chat          │
└────────────┘         └──────────────────────────────┘         └────────────────────┘
   (A) 不動               (B) 新增 outbound WS client                (B) 新增 inbound WS server
```

**段 (A) Browser ↔ Odoo — SSE EventSource（不動，**§1.2 第二輪 sign-off**）**：

- 既有 `/api/ai/stream/<channel_id>?csrf_token=...` SSE endpoint 不變
- `src/static/src/paas/services/ai_service.js:130` 客戶端 `EventSource` 不變
- 既有 `_sse_error_response` / `data: {chunk}\n\n` / `data: {done: true}\n\n` event 格式 **保留**（v1 不引入新 event type 到 SSE 層 — pod → Odoo 的新 type（如 `tool_unavailable`）在 Odoo controller 內 forward to SSE 時包成既有 schema 內可呈現的事件）
- 為什麼 frontend 不動：(1) §1.2 第二輪 sign-off 守住；(2) Odoo Discuss UI 與 PaaS chat UI 都用 SSE，統一 transport 簡化 frontend；(3) browser WS 對 SSE 沒帶來新 capability for our use case

**段 (B) Odoo ↔ Pod — Outbound WS（新增）**：

- 新建：Odoo controller 在 SSE generator 內以 client 角色 open outbound WS to `ws://agent-pa-{hash}-svc.paas-ws-pa-{hash}.svc.cluster.local:8000/v1/chat`
- Pod 端：FastAPI `@app.websocket("/v1/chat")` route 作 server
- 雙向：Odoo → Pod 傳 user message / cancel / template_update / mcp_invalidate / skill_invalidate / ping；Pod → Odoo 傳 text_chunk / tool_call / tool_result / tool_error / tool_unavailable / status / error / done / pong
- 為什麼 (B) 用 WS 而非 SSE 或 HTTP polling：bidirectional 必要（pod 也要 push template_update / mcp_invalidate 到 pod 對話進行中時即時觸發 — 雖然 v1 也可改成 ConfigMap inotify，但 WS 同 channel 簡化 transport 層）

**為什麼用 cluster.local DNS 直連 pod，不過 paas-operator proxy**：

- pod IP 不穩定（reap/resume cycle 後 IP 變）— Service ClusterIP 穩定，命名走 `agent-pa-{hash}-svc.paas-ws-pa-{hash}.svc.cluster.local`
- 不要 paas-operator 代理：avoid extra hop latency；paas-operator 不該變成 stateful proxy
- Odoo 在 cluster 內（同 K8s）— 直達 cluster.local DNS

### §5.2 WS message envelope schema（Q1 拍板用）

**Envelope 結構**（JSON over WS frame，UTF-8 text frame）：

```json
{
  "version": "1.0",                      // protocol version (frozen string)
  "type": "<message_type>",              // see types table below
  "request_id": "uuid-v4",               // unique per user_message; all related events share same id
  "correlation_id": "uuid-v4 | null",    // optional; for tool_call chains
  "sequence_number": 1,                  // monotonic int per request_id (0-indexed within a request)
  "timestamp": "2026-05-22T10:30:00Z",   // ISO 8601 UTC
  "payload": { ... }                     // type-specific schema
}
```

**Message types（v1.0 — frozen after sign-off）**：

| Direction | Type | Payload 必填 | 用途 |
|-----------|------|-------------|------|
| **Odoo → Pod** |  |  |  |
| → | `user_message` | `channel_id`, `body`, `history`, `template_id`, `invoker_role`, `invoker_user_id` | 用戶發送訊息觸發 LangGraph |
| → | `cancel` | `request_id_target` | 取消正在進行的 request |
| → | `template_update` | `template_id`, `template_snapshot` | 通知 pod template 配置變動（hot reload） |
| → | `mcp_invalidate` | `mcp_snapshot_hash` | 通知 pod 重新 parse mcp.json（hot reload） |
| → | `skill_invalidate` | `skill_snapshot_hash` | 通知 pod 重新 fetch skill list |
| → | `ping` | `nonce` | heartbeat ping |
| **Pod → Odoo** |  |  |  |
| ← | `text_chunk` | `content` | LLM 文字流片段 |
| ← | `tool_call` | `tool_name`, `tool_args`, `tool_call_id` | LangGraph tool 開始 |
| ← | `tool_result` | `tool_call_id`, `result` | tool 成功完成 |
| ← | `tool_error` | `tool_call_id`, `error`, `code` | tool 執行失敗 |
| ← | `tool_unavailable` | `tool_name`, `reason` ∈ {archived, removed_from_template, mcp_offline}, `recovery_hint` | tool 在當前 context 不可用（§5.7） |
| ← | `status` | `stage` ∈ {spawning, fetching_skill, ws_connected, llm_thinking}, `elapsed_ms` | 進度 update |
| ← | `error` | `error`, `code`, `fatal: bool` | 致命錯誤；fatal=True 後關 WS |
| ← | `done` | `full_response`, `usage` (tokens), `message_id` | response 完成 |
| ← | `pong` | `nonce` | heartbeat reply |

**序列保證**：

- `request_id`：unique per user_message；所有 response chunks 都帶相同 request_id
- `correlation_id`：optional；同一 tool_call 鏈內保持（用於 multi-step tool 追蹤）
- `sequence_number`：monotonic int per request_id（從 0 開始遞增），防 out-of-order delivery 或重複；Odoo 收 envelope 時根據 sequence_number 排序 / 去重
- `version`：protocol version；v1 = `"1.0"`，後續 v1.1 / v2 需向後相容（new types 可加，既有 types schema 不刪 field）

**Error envelope 細部**：

```json
// pod 端致命錯誤（如 LangGraph runtime crash）
{
  "version": "1.0",
  "type": "error",
  "request_id": "...",
  "payload": {
    "error": "LangGraph runtime panic: ...",
    "code": "runtime_panic",      // enum: runtime_panic / llm_provider_error / mcp_connection_error / skill_subprocess_timeout / auth_expired
    "fatal": true                  // true = 關 WS；false = 可繼續
  }
}
```

**Heartbeat 策略**：

- Odoo 每 30s 送 `ping`，Pod 60s 內沒回 `pong` → Odoo 關 WS + 標 reap-or-restart
- Pod 每 30s 接收 ping 計時器 reset；接收 user_message / send chunk 也算 alive
- 防 idle reaper 誤判：active WS connection 算 active；reap 只看 last_active annotation（§2.3）

### §5.3 Odoo ↔ Pod auth（Q2 拍板用）

**phase-planner 推薦：(b) HS256 bearer token + shared secret + spawn-or-attach 時簽發**

三選項 trade-off：

| 方案 | 機制 | 安全性 | 實作 cost | Scoping（per-project）| 推薦 |
|------|------|-------|----------|----------------------|------|
| (a) Static API key | helm chart 注入單一 cluster secret | 弱（leak 一次 → 全 cluster 失守） | 最低 | 不行（同 key 全 pod 共用）| ✗ |
| **(b)（推薦）** Bearer token | spawn-or-attach 時 paas-operator 用 shared secret 簽 HS256 JWT；含 project_hash + workspace_ids + exp | 強（leak → 一個 pod 一段時間）| 中 | 是（token 內 claim project_hash） | ✓ |
| (c) mTLS | Pod 與 Odoo 各持證書 | 最強 | 高（cert lifecycle / rotation / CA） | 是（cert CN claim） | ✗（v1 過度）|

**選 (b) 的具體機制**：

```
spawn-or-attach 流程內：
   1. Odoo controller 呼 paas-operator POST /spawn-or-attach
   2. paas-operator:
      - 起 pod / resume / attach（§2.11 既有）
      - 用 shared secret (cluster Secret `agent-runtime-auth`) 簽 JWT:
        payload = {
          "iss": "paas-operator",
          "aud": "agent-runtime",
          "project_hash": "...",
          "workspace_ids": [...],
          "channel_id": ...,           # invocation-level scope
          "invoker_user_id": ...,
          "invoker_role": "...",       # admin/user/guest
          "exp": now + 1h,
          "iat": now,
          "jti": uuid-v4               # 防 replay
        }
        algo = HS256
   3. paas-operator return { pod_ws_url, auth_token: <jwt>, pod_state, ... }
   4. Odoo controller open WS：
        ws.connect(pod_ws_url + "?token=" + jwt)
      OR header: Authorization: Bearer <jwt>
   5. Pod 端 FastAPI WS handler 用 shared secret 驗 token signature + exp + project_hash 對應自己
```

**Token scope**：

- 一個 token 只 valid for 一個 (project_hash, channel_id, invoker) tuple — 不能 cross-project / cross-channel
- exp 1 小時；過期 → ws 關閉 + Odoo 重 spawn-or-attach 拿新 token
- 不重用：一個 token 配一個 WS connection（jti 防 replay）

**Shared secret 管理**：

- K8s Secret `agent-runtime-auth-secret`，namespace `paas-system`
- paas-operator + 所有 agent pod 都 mount 同一 secret
- agent pod helm chart values 引用該 secret（不寫死在 image）
- Rotate：admin 手動 — 改 secret + bump paas-operator + bump 所有 pod 的 helm release（cycle reap → resume 用新 secret）
- v1 沒 auto rotation（留 v2）

**Odoo 端不需 shared secret**：Odoo 不簽 token（paas-operator 簽），只 forward；所以 Odoo 不接觸 secret — 縮小 attack surface

### §5.4 ConfigMap mcp.json hot reload vs cold reload（Q3 拍板用）

**phase-planner 推薦：(a) Hot reload via WS push + inotify fallback**

三選項：

| 方案 | 機制 | 延遲 | UX | 風險 | 推薦 |
|------|------|------|-----|------|------|
| **(a)（推薦）** Hot reload (WS push primary + inotify fallback) | Odoo write hook → paas-operator sync-workspaces → WS `mcp_invalidate` push → pod runtime 重 parse；inotify watcher 作 60s 內兜底 | < 5s（WS 路徑）；60s（inotify 兜底） | active session 不中斷 | mid-flight tool_call 仍走舊 snapshot（acceptable）| ✓ |
| (b) Cold reload via pod restart | sync-workspaces → paas-operator scale_release 0 → 1 重啟 pod | 7-15s（resume cycle）| active session 中斷重連 | mid-flight tool_call lose；user 感受 spawning event | ✗ |
| (c) Pure inotify | 只靠 K8s ConfigMap mount 後 inotify | 30-60s（kubelet sync）| 不中斷 | 大延遲對「user 改 cloud_service 後立即測試 agent」UX 差 | ✗ |

**選 (a) 的具體流程**：

```
變動觸發路徑：
   1. Odoo: project.cloud_service_ids / agent_template / mcp_server 變動
      → write hook 呼 paas-operator POST /agent-runtimes/{hash}/sync-workspaces
                                            or /invalidate-mcp
   2. paas-operator:
      a. patch ConfigMap `agent-pa-{hash}-mcp` with new mcp snapshot
      b. patch ConfigMap annotation `agent.woow.tw/snapshot-hash` = new SHA256
      c. 主動 WS push to pod (paas-operator 自己 open transient WS to pod) 
         或：postFile a flag → Odoo 在下次 user_message envelope 內帶 mcp_invalidate
   3. Pod 端：
      a. WS handler 收到 mcp_invalidate envelope → reload mcp.json + update in-memory tool_set
      b. 若漏接（paas-operator → pod WS 失敗）→ inotify on /etc/agent-runtime/mcp.json mount path 作 60s 內兜底
   4. mid-flight tool_call：
      - 用舊 snapshot 完成本 invocation（避免 mid-flight state 混亂）
      - Next invocation 走新 snapshot
```

**Hot reload 的邊界 condition**：

- mcp_server 被 archive 時，pod 內仍有 active reference（mid-flight）→ tool_call 完成；next 用新 snapshot → emit `tool_unavailable`
- 新 mcp_server 添加 → 新 snapshot 含 + LangGraph 下次 resolve_tool 時動態 register
- agent_template 切換 → 同樣走 `template_update` envelope，pod 端切 template 配置 + tool set

**為什麼不純走 inotify**：

- K8s ConfigMap mount 在 worker node kubelet 級 cache + sync 大約 30-60s 才反映 — UX 太慢
- 對於「user 改 cloud_service 後立即測試 agent」場景，60s 等待不可接受
- WS push 路徑能在 5s 內生效（cluster.local 直連 + asyncio）

**為什麼 inotify 作 fallback 必要**：

- WS push 路徑萬一失敗（pod restart 中 / paas-operator 短暫 down），inotify 兜底保證最終一致
- ConfigMap mount 本身就是 hot reload 機制；不用白不用

### §5.5 agent_session ↔ paas-operator state sync（Q4 拍板用）

**phase-planner 推薦：(a) Webhook primary + (b) 輪詢 fallback**

State sync 是**雙向**：

- Pod state machine 變化（spawning → active → reaped → ...）→ 同步到 Odoo `agent_session.state`
- Odoo agent_session 設定變動（quota override / template binding）→ pod runtime 拿到（透過 spawn-or-attach 重新注入或 hot reload）

**主路徑：Webhook（paas-operator → Odoo）**：

```
paas-operator 端：
   - spawn / reap / GC 任一狀態變動後，callback Odoo controller
   - Endpoint: POST {odoo_base_url}/api/agent-runtimes/webhook/state-update
   - Payload:
     {
       "project_hash": "...",
       "new_state": "active|reaped|spawn_failed|degraded",
       "ts": "ISO 8601",
       "reason": "spawn complete | idle reap | helm fail | ...",
       "pod_ws_url": "...",       // optional, only for active state
       "spawn_age_ms": 5234       // optional
     }
   - Auth: HMAC-SHA256 with shared secret `agent-runtime-webhook-secret`
     - Header: X-PaaS-Signature: hmac(payload_body, secret)
     - 防 spoofing

Odoo 端：
   - controller endpoint：/api/agent-runtimes/webhook/state-update (auth=none, type=json)
   - verify HMAC signature
   - 找 agent_session by project_namespace_hash
   - update state + last_synced_at + last_state_event_ts
```

**Fallback：Odoo cron 輪詢**：

```
Cron schedule: every 5 min
Logic:
   - search agent_session where state IN (spawning, active, reaped, degraded) AND
            last_synced_at < now - 10 min  # webhook 漏網時兜底
   - for each: call paas-operator GET /agent-runtimes/{hash}/status
   - update agent_session.state + last_synced_at
```

**為什麼不選 (c) SSE**：

- Odoo 端用 SSE listener 需要 long-lived worker thread — 占用 worker pool（每 worker 服務一個 paas-operator SSE 連線）
- paas-operator 端 SSE source 也要維持 N 條長連線（給 N 個 Odoo workers）
- 複雜度高且不解決 webhook delivery failure（fallback 仍需要輪詢）

**Webhook delivery failure 處理**：

- HMAC 驗 fail → 401 reject + paas-operator log warn
- 5xx 回應 → paas-operator retry 3 次 exponential backoff
- 仍失敗 → cron 輪詢兜底
- Drift detection：cron 比對 paas-operator 端 ground truth vs Odoo agent_session.state；不一致 → Odoo 端更新 + audit log

### §5.6 SSE generator rewrite（Odoo controller）

當前 `controllers/ai_assistant.py:353-544` 的 SSE generator 內 `asyncio.run` LangGraph 整段 **改寫**。

**新版 pseudocode**：

```python
@route('/api/ai/stream/<int:channel_id>', auth='user', methods=['GET'], type='http')
def api_ai_stream(self, channel_id, **kwargs):
    """SSE endpoint — Odoo ↔ Pod transport bridge (rewritten for agent-runtime pod)."""

    # ① validate CSRF + channel access (既有)
    csrf_token = kwargs.get('csrf_token', '')
    if not csrf_token or not request.validate_csrf(csrf_token):
        return self._sse_error_response('Invalid request', 'csrf_error')

    channel = request.env['discuss.channel'].sudo().browse(channel_id)
    if not channel.exists() or not self._check_channel_access(channel):
        return self._sse_error_response('Access denied', 'access_denied')

    # ② resolve project_id (新加)
    task = request.env['project.task'].sudo().search([
        ('channel_id', '=', channel_id),
    ], limit=1)
    if not task or not task.project_id:
        return self._sse_error_response('Channel not bound to a project', 'no_project')
    project = task.project_id

    # ③ project_access check (新加 — § 4)
    if not self._check_project_access(project):
        return self._sse_error_response('Project access denied', 'project_access_denied')

    # ④ resolve agent_template (per-project default; fallback default_ai_assistant_id, § 4.11 Q5)
    template = self._resolve_agent_template(project)
    if not template:
        return self._sse_error_response('No agent template configured', 'no_template')

    # ⑤ pre-fetch ORM data BEFORE entering the generator (cursor 仍 valid)
    invoker = request.env.user
    invoker_role = self._get_project_access_role(project, invoker)
    mcp_snapshot = self._build_mcp_snapshot(project, template)
    skill_snapshot = self._build_skill_snapshot(project, template)
    history = channel._get_chat_history(limit=20)
    user_message = self._get_latest_user_message(channel)
    request_id = str(uuid.uuid4())

    # ⑥ call paas-operator spawn-or-attach
    from ..services.agent_runtime_client import get_agent_runtime_client
    client = get_agent_runtime_client(request.env)
    try:
        runtime_info = client.spawn_or_attach(
            project=project,
            channel_id=channel_id,
            invoker_user_id=invoker.id,
            invoker_role=invoker_role,
            agent_template=template,
            mcp_snapshot=mcp_snapshot,
            skill_snapshot=skill_snapshot,
        )
    except RuntimeError as exc:
        return self._sse_error_response(f'Pod spawn failed: {exc}', 'spawn_failed')

    pod_ws_url = runtime_info['pod_ws_url']
    auth_token = runtime_info['auth_token']

    # ⑦ open outbound WS + relay envelopes as SSE events
    db_name = request.env.cr.dbname
    uid = request.env.uid

    def generate():
        """SSE generator — bridges pod WS events to browser SSE."""
        import asyncio
        import websockets

        async def relay():
            """Connect WS, send user_message, yield response envelopes."""
            async with websockets.connect(
                pod_ws_url,
                extra_headers={'Authorization': f'Bearer {auth_token}'},
                ping_interval=30,    # heartbeat
                ping_timeout=60,
            ) as ws:
                # Send user_message envelope
                await ws.send(json.dumps({
                    'version': '1.0',
                    'type': 'user_message',
                    'request_id': request_id,
                    'sequence_number': 0,
                    'timestamp': datetime.utcnow().isoformat() + 'Z',
                    'payload': {
                        'channel_id': channel_id,
                        'body': user_message,
                        'history': history,
                        'template_id': template.id,
                        'invoker_role': invoker_role,
                        'invoker_user_id': invoker.id,
                    },
                }))

                # Receive envelopes until done / fatal error
                async for msg in ws:
                    yield json.loads(msg)
                    envelope = json.loads(msg)
                    if envelope['type'] == 'done':
                        return
                    if envelope['type'] == 'error' and envelope['payload'].get('fatal'):
                        return

        # Sync iterator over async generator (Odoo worker is sync)
        loop = asyncio.new_event_loop()
        try:
            agen = relay()
            full_response = ''
            saved_message_id = None
            while True:
                try:
                    envelope = loop.run_until_complete(agen.__anext__())
                except StopAsyncIteration:
                    break

                # Forward envelope as SSE event (transform to legacy SSE schema)
                if envelope['type'] == 'text_chunk':
                    chunk = envelope['payload']['content']
                    full_response += chunk
                    yield f'data: {json.dumps({"chunk": chunk, "done": False})}\n\n'
                elif envelope['type'] in ('tool_call', 'tool_result', 'tool_error', 'tool_unavailable'):
                    yield f'data: {json.dumps({**envelope, "done": False})}\n\n'
                elif envelope['type'] == 'status':
                    yield f'data: {json.dumps({**envelope, "done": False})}\n\n'
                elif envelope['type'] == 'done':
                    # Persist AI response to mail.message (既有邏輯沿用)
                    saved_message_id = self._persist_ai_response(
                        db_name, uid, channel_id, full_response, template,
                    )
                    yield f'data: {json.dumps({
                        "chunk": "",
                        "done": True,
                        "full_response": full_response,
                        "message_id": saved_message_id,
                    })}\n\n'
                    break
                elif envelope['type'] == 'error':
                    yield f'data: {json.dumps({"error": envelope["payload"]["error"], "done": True})}\n\n'
                    break
        except Exception as exc:
            _logger.exception('SSE-WS bridge failed for channel %s: %s', channel_id, exc)
            yield f'data: {json.dumps({"error": str(exc), "done": True})}\n\n'
        finally:
            loop.close()

    return Response(
        generate(),
        content_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )
```

**新增的 Odoo helper**（在 `src/services/agent_runtime_client.py`）：

```python
class AgentRuntimeClient:
    """HTTP client to paas-operator for agent-runtime ops."""

    def spawn_or_attach(self, project, channel_id, invoker_user_id,
                        invoker_role, agent_template, mcp_snapshot, skill_snapshot):
        """POST /api/agent-runtimes/spawn-or-attach.

        Returns: { pod_ws_url, auth_token, pod_state, spawn_age_ms }
        """
        ...

    def reap(self, project_hash):
        ...

    def invalidate_mcp(self, project_hash):
        ...
```

**注意**：原 `discuss_channel.py:55-118` 的 `message_post` override 同步 LangChain 分支將被 **刪除**（§7 prereq 已列明；本 §5 不重複）。SSE generator 唯一觸發 AI 回應的入口。

### §5.7 Tool set 變動處理（§4.6 `tool_unavailable` event 細化）

**三個變動情境**：

1. **Template 切換**（user 在 chat 中切 agent_template）
2. **Skill upgrade / archive**（owner 在 Odoo 改 skill state）
3. **MCP invalidation** — 三子情境：
   - (a) **sidecar-backed MCP 物理 teardown**：cloud_service `_do_cleanup` → 對應 `sidecar_backed=True` MCP unlink + sidecar pod 拆 → 該 MCP URL 死
   - (b) **external MCP 變動**：user 在 Odoo UI 改 mcp_server.url / headers / archive → 跑相同 invalidate-mcp 路徑（refactor branch cycle 4 拔 D5 後 external MCP 是合法獨立 row）
   - (c) **project.cloud_service_ids 變動**：add/remove cloud_service → 連帶觸發 sidecar-backed MCP 可達性變動（egress allowlist 同步透過 `sync-workspaces` endpoint）

**LangGraph thread checkpoint 結構**（pod runtime 內，存 PVC）：

```python
# /workspace/project-{pid}/threads/{channel_id}/checkpoint.json
{
    "thread_id": "channel-42",
    "messages": [
        HumanMessage(...),
        AIMessage(content="...", tool_calls=[{"id": "tc-1", "name": "search_docs", ...}]),
        ToolMessage(tool_call_id="tc-1", content="..."),
        AIMessage(content="..."),
    ],
    "template_id_when_created": 5,
    "tool_set_snapshot_hash": "abc123",  # SHA of resolve_runtime_tools output at thread create
    ...
}
```

**Tool resolve 時點**：

- LangGraph runtime 每收到 LLM 返回的 tool_call → resolve_tool(name) against current snapshot
- 若 `name` 存在當前 snapshot：dispatch（subprocess for skill / HTTP for mcp）
- 若不存在：emit `tool_unavailable` envelope，**不阻斷 LangGraph stream**

**`tool_unavailable` envelope payload**：

```json
{
  "version": "1.0",
  "type": "tool_unavailable",
  "request_id": "...",
  "payload": {
    "tool_name": "search_docs",
    "tool_call_id": "tc-1",
    "reason": "removed_from_template | archived | mcp_offline | template_mismatch",
    "recovery_hint": "Tool 'search_docs' was removed from current agent template. Switch back to previous template or contact admin.",
    "context": {
      "current_template_id": 6,
      "thread_template_id_when_created": 5,
      "current_snapshot_hash": "def456",
      "thread_snapshot_hash": "abc123"
    }
  }
}
```

**Template 切換 specific**：

- Odoo controller 在 `user_message` envelope 內帶 `template_id`
- Pod 收到後 compare with thread's `template_id_when_created`
- 不一致：用新 template 的 system_prompt + tool_set 繼續 thread；但 history 內既有 tool_call references 可能 `tool_unavailable`
- frontend 收到 `tool_unavailable` → UI 顯示「該工具在當前模式不可用」chip
- LLM 看到 ToolMessage with empty result（或 system message hint）後決定如何回應

**Skill upgrade specific（active thread 內遇到舊版 skill）**：

- Default behavior：active thread 內 reference 走 thread-local snapshot（thread create 時的 version）— 不熱替換
- 新 spawn-or-attach（下次 conv）才走新 version
- 例外：admin 顯式呼 `force_refresh_snapshot` endpoint → 即時更新 thread snapshot

**MCP invalidation specific（cloud_service teardown 進行中）**：

- 既有 `cloud_service._do_cleanup`：unlink sidecar-backed MCP → helm uninstall → state='deleting'（§7 prereq 延伸 M:N）
- 在 helm uninstall 後，pod 內 mcp.json 還有舊 URL（未 reload）
- 下次 tool_call reference 該 MCP → langchain-mcp-adapters connect fail → emit `tool_unavailable` with reason `mcp_offline`
- 同時 paas-operator 觸發 `invalidate_mcp` WS push → pod runtime 移除舊 mcp_server from snapshot

### §5.8 Pod 端 WS server 結構

**FastAPI WebSocket route**（image source `runtime/transport/ws_server.py`）：

```python
# pseudocode in agent-runtime image
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException

from runtime.transport.auth import verify_token
from runtime.transport.envelope import format_envelope
from runtime.langgraph_runtime import LangGraphRuntime  # 全域單例
from runtime.state.mcp_reloader import McpReloader     # inotify watcher

app = FastAPI()

# Global runtime (module-level singleton)
runtime = LangGraphRuntime()
mcp_reloader = McpReloader('/etc/agent-runtime/mcp.json')
mcp_reloader.start()  # background inotify task

@app.on_event('startup')
async def startup():
    await runtime.initialize()  # load LangGraph + langchain-mcp-adapters

@app.websocket('/v1/chat')
async def websocket_chat(ws: WebSocket, token: str = Query(...)):
    # ① verify auth token
    try:
        claims = verify_token(token)  # HS256, returns {project_hash, channel_id, invoker_role, ...}
    except Exception as e:
        await ws.close(code=4401, reason=f'Auth failed: {e}')
        return

    # ② verify token's project_hash matches own pod
    if claims['project_hash'] != settings.PROJECT_HASH:
        await ws.close(code=4403, reason='Token project mismatch')
        return

    await ws.accept()

    try:
        # ③ receive first envelope (must be user_message)
        msg_text = await ws.receive_text()
        envelope = json.loads(msg_text)
        if envelope.get('type') != 'user_message':
            await ws.send_json(format_envelope(
                'error', payload={'error': 'expected user_message', 'code': 'protocol_violation', 'fatal': True},
                request_id=envelope.get('request_id', 'unknown'),
            ))
            await ws.close(code=4400)
            return

        request_id = envelope['request_id']
        payload = envelope['payload']
        channel_id = payload['channel_id']
        thread_id = f'channel-{channel_id}'

        # ④ stream LangGraph events as WS envelopes
        seq = 0
        async for event in runtime.stream(
            thread_id=thread_id,
            user_message=payload['body'],
            history=payload['history'],
            template_id=payload['template_id'],
            invoker_role=payload['invoker_role'],
        ):
            seq += 1
            await ws.send_json(format_envelope(
                event.type,
                payload=event.payload,
                request_id=request_id,
                sequence_number=seq,
            ))

            # listen for cancel envelope concurrently (race)
            # — implementation detail: use asyncio.create_task + cancel propagation

        # ⑤ send done
        await ws.send_json(format_envelope(
            'done',
            payload={'full_response': event.full_response, 'usage': event.usage},
            request_id=request_id,
            sequence_number=seq + 1,
        ))

    except WebSocketDisconnect:
        # Client closed; LangGraph thread checkpoint already persisted to PVC
        pass
    except Exception as exc:
        await ws.send_json(format_envelope(
            'error',
            payload={'error': str(exc), 'code': 'runtime_panic', 'fatal': True},
            request_id=envelope.get('request_id', 'unknown'),
        ))
    finally:
        try:
            await ws.close()
        except Exception:
            pass
```

**並發處理**：

- FastAPI + uvicorn + asyncio 天然支援多個 WS connection 同時
- 一個 pod 同時服務多個 WS connection（同 project 內多 user / 多 thread 並行）
- LangGraph thread isolation 透過 `thread_id`（= channel_id）
- 同 thread 不允許並行 invoke（用 in-process per-thread asyncio.Lock）— 同 channel 同時兩個 WS request 第二個會 wait
- 跨 thread 並行 OK

**Module-level shared state**：

- `LangGraphRuntime` 是 module 級單例（避免每 connection 重建 graph）
- `mcp_snapshot` 是 module 級 in-memory dict（inotify watcher 更新）
- `skill_snapshot` 同上
- 所有 thread-specific state（messages / tool_call history）走 LangGraph checkpoint store（thread_id 分隔）

**Heartbeat 整合**：

- FastAPI WebSocket 內建 ping/pong（`ping_interval=30, ping_timeout=60` 從 client 端設）
- 不需 application-level ping/pong（v1 簡單）

**Resource limits per connection**：

- 每 WS connection 不開 thread；asyncio 單 event loop
- LangGraph subprocess invoke：每 tool_call 起一個 subprocess（§3 / §6 細節）
- 防 memory exhaust：pod-level cgroup limits（§6）

### §5.9 Affected files / new files（§5 範圍預測）

**新建（agent-runtime image source，`woow-paas-agent-runtime` repo）**：

- `runtime/transport/ws_server.py` — FastAPI WebSocket route
- `runtime/transport/auth.py` — HS256 token verify
- `runtime/transport/envelope.py` — JSON envelope serializer + type enum
- `runtime/state/mcp_reloader.py` — ConfigMap inotify watcher
- `runtime/state/template_reloader.py` — Template hot reload handler
- `runtime/state/skill_reloader.py` — Skill snapshot reloader
- `runtime/langgraph_runtime.py` — Module-level LangGraph singleton + stream

**新建（Odoo addon）**：

- `src/services/agent_runtime_client.py` — HTTP/WS client wrapper to paas-operator + pod
- `src/controllers/agent_runtime_webhook.py` — paas-operator → Odoo webhook receiver
- `src/security/ir_rules.xml` — webhook endpoint sudo (auth=none)

**新建（paas-operator）**：

- `extra/paas-operator/src/services/jwt_signer.py` — HS256 token signer
- `extra/paas-operator/src/services/webhook_dispatcher.py` — outbound webhook + retry
- `extra/paas-operator/src/api/agent_runtimes.py` — 既有（§2.11）內加 sign + webhook 邏輯
- `extra/paas-operator/src/services/mcp_invalidator.py` — patch ConfigMap + WS push

**修改（Odoo addon）**：

- `src/controllers/ai_assistant.py` — SSE generator 重寫（§5.6 pseudocode）
- `src/controllers/__init__.py` — 加 agent_runtime_webhook import
- `src/models/agent_session.py` — 加 `last_synced_at` / `last_state_event_ts` fields
- `src/models/__init__.py` — import agent_runtime_client（service）
- `src/__manifest__.py` — bump version + 加 controller XML
- `requirements.txt` — 加 `websockets` (client)；`PyJWT` (HS256 sign — paas-operator 端)

**修改（image / chart）**：

- `woow-paas-charts/charts/agent-runtime/templates/secret.yaml` — `agent-runtime-auth-secret` 跨 namespace 拉取
- `woow-paas-charts/charts/agent-runtime/templates/deployment.yaml` — env `SHARED_SECRET` from secret mount
- `woow-paas-charts/charts/agent-runtime/templates/service.yaml` — ClusterIP for WS

**不動**（保護 invariants）：

- 既有 `src/static/src/paas/services/ai_service.js` 內 EventSource 邏輯 — frontend 零變更
- `src/static/src/paas/pages/ai-chat/AiChatPage.js` SSE consumer — 零變更
- 既有 `_sse_error_response` helper — 沿用
- `mcp_server` schema / ACL（refactor branch finalized）
- 既有 SSE event schema 對 frontend（`{chunk, done, full_response, message_id, error, warning}`）— 沿用

### §5.10 Risks

| Risk | 嚴重度 | Mitigation | Rollback |
|------|-------|-----------|---------|
| WS connection drop mid-stream → SSE generator hang / Odoo worker 卡住 | 高 | ping/pong heartbeat（30s ping，60s pong timeout）；asyncio.wait_for 包關鍵 await；Odoo worker thread timeout 30 min（既有設定）| 重連；frontend 收到 SSE error event 後 user 手動 retry |
| Odoo worker pool 容量：每 active conv 吃 1 worker | 高 | worker pool sizing 算清楚（既有 Odoo `workers` 設定）；100 active conv ≈ 100 worker；不夠時擴容 | 既有 Odoo worker pool 設定增加 |
| WS auth token 過期但 conv 還在進行 | 中 | 1h exp + Odoo 端 detect 401 後 re-spawn-or-attach 拿新 token 自動續 | user 重整頁面（極端情境）|
| ConfigMap inotify delay (kubelet sync ~60s) | 中 | WS push 主路徑 < 5s；inotify 只兜底 | 60s 後最終一致 |
| Pod LangGraph 全域 graph state 跨 thread 污染 | 高 | langgraph-checkpoint per-thread namespace；tool_dispatcher 用 thread_id 隔離 stdout/stderr | thread 重啟（不破壞 PVC 既有 checkpoint）|
| WS heartbeat 失敗導致 false reap | 中 | reaper 看 `last-active` annotation（pod 自己 patch），WS heartbeat 失敗本身不觸發 reap | active session 由 WS 計時延展 last-active |
| 重複 user_message envelope（user retry）| 低 | request_id 去重（pod 端 cache request_id 1 小時內）| 第二次 request_id 同 → reject + 回 cached result（idempotent）|
| Webhook delivery 失敗（paas-operator → Odoo）| 中 | retry 3 次 exponential backoff；cron 輪詢兜底 | drift detection cron 補狀態 |
| 大 tool_result payload 超過 WS message size | 低 | WS frame default max 1 MB；超過 chunk 化 by request_id + sequence_number | 超大 payload 用 ir.attachment 引用 + URL fallback |
| Cancel envelope race（Odoo 送 cancel 但 pod 已完成）| 低 | cancel idempotent；pod 收到時 noop if request_id 已 done | 不影響正確性 |
| Webhook HMAC secret leak | 高 | rotate secret + 重啟 paas-operator + agent pod；audit log | 同 §5.3 shared secret rotate |
| WS message 順序錯亂（asyncio 並發）| 中 | sequence_number monotonic + Odoo 端排序 buffer（短延遲）| sequence_number 0 → N 連續驗證 |
| SSE-WS 橋接的 event loop 不乾淨關閉 | 中 | try/finally + loop.close()；超時強制 cancel | 既有 SSE generator pattern 已測 |

### §5.11 Open questions for leader sign-off（§5 escalation）

> 5 條核心拍板需求。phase-planner 推薦已 inline 在 §5.2 / §5.3 / §5.4 / §5.5 給出；額外第 5 條（WS connection 生命週期）是 §5.8 探出來的設計選擇。**全部 sign-off 2026-05-22**（doc-wide consistency pass 完成後 batch）。

1. ~~**[需 leader 拍板] WS envelope schema v1.0 frozen（§5.2）**~~ **[SIGNED-OFF 2026-05-22]** 15 types（Odoo→Pod 6 + Pod→Odoo 9）+ envelope 結構 (version/type/request_id/correlation_id/sequence_number/timestamp/payload) frozen for v1
2. ~~**[需 leader 拍板] Odoo ↔ Pod auth（§5.3）**~~ **[SIGNED-OFF 2026-05-22]** (b) HS256 bearer token + spawn-or-attach 簽發 + 1h exp + shared secret in K8s Secret
3. ~~**[需 leader 拍板] ConfigMap reload 策略（§5.4）**~~ **[SIGNED-OFF 2026-05-22]** (a) hot reload via WS push 主 + inotify 60s 兜底
4. ~~**[需 leader 拍板] State sync 機制（§5.5）**~~ **[SIGNED-OFF 2026-05-22]** (a) webhook + (b) cron 輪詢 fallback
5. ~~**[需 leader 拍板 — §5 新發現] WS connection 生命週期**~~ **[SIGNED-OFF 2026-05-22]** 每 user_message 一個短連 WS connection（spawn-or-attach 拿 token → open WS → done → close）— 對比表保留歷史紀錄：

  - 短連線優：implementation 簡單（既有 SSE 一對一 mapping）；token scope tight；無 mid-life connection state 問題
  - 短連線劣：每 user_message 都重建 WS（增加 setup latency 50-200ms）；spawn-or-attach 每次都呼（pod 端內部 attach idempotent）
  - 長連線優：低 latency continuous chat；setup cost 攤提
  - 長連線劣：連線中 token 過期處理 / cleanup race / pod reap during connection 都要顯式處理；複雜度高

**§5 額外探出來但不阻塞 §5 sign-off**（§6/§7 處理）：

- WS 流量 TLS encryption（cluster-internal 是否強制 TLS or plain ws://）— §6 安全章
- Token replay 攻擊深度防護（jti 已有，但需要 Pod 端 cache size）— §6
- LangGraph tool_call subprocess 的 stdout 限制（max_output_kb）細節 — §6
- Skill subprocess 對 mcp_snapshot 的讀取權限 — §6（subprocess env / cgroup share 問題）
- 既有 `discuss_channel.py:55-118` `message_post` override 同步分支刪除時機 — §7
- 既有 `ai_client.py` 整檔切割線（pure runtime 搬到 image source 後 Odoo 端剩什麼）— §7
- 既有 SSE chunks 與新 envelope 的 mapping table（兼容 frontend 字面 `{chunk, done, full_response}`）— implementation 階段

## §6 Sandbox security model (NetworkPolicy / RBAC / seccomp / TLS / rlimit / SSRF)

> **Status**：Phase 1 — §6 draft submitted, awaiting sign-off
> 依賴：§1.2 trust model (user-trusted)、§2.7 paas-ws-pa-{hash} namespace、§3.9 sandbox profile (bundle declare = metadata only / host 100% enforce)、§4.3 project_access ACL、§5.3 HS256 auth
> 6 條 [需 leader 拍板] 留 §6.13 escalate（已預警 5 條 + 額外 RBAC granularity）

### §6.0 Scope（§6 處理什麼、不處理什麼）

**§6 處理**：

- Pod-level securityContext baseline（runAsNonRoot / readOnlyRootFs / seccomp / dropAllCaps）— **比既有 cloud_service Bitnami chart 嚴格**（§1.1 Q4 sign-off 蘊含）
- Filesystem 隔離（rootfs ro + /tmp tmpfs + /workspace PVC subdir + /etc/agent-runtime ConfigMap mount + secret mount）
- NetworkPolicy egress 三類 allowlist（DNS + sidecar-backed MCP 跨 ns + external MCP 白名單機制）
- Subprocess spawn 約束（env whitelist + rlimit baseline + timeout default + skill 拿資源走 LangGraph tool API 間接）
- Cluster-internal TLS 強制與否
- Token replay defense（jti cache + shared secret rotation 配套）
- SSRF guard（延伸 refactor branch minimal 防護 + 是否再加 egress enforcement）
- Odoo worker pool sizing（對應 §5.6 SSE generator 1 worker per active conv 阻塞）
- RBAC（pod ServiceAccount + paas-operator ServiceAccount 邊界）
- Audit logging（pod 結構化 JSON + Odoo audit trail）

**§6 不處理**（留後續章節）：

- Secret rotation 自動化流程細節 → implementation
- Pod log 收集 stack（Loki / Elastic / Vector）→ deployment / SRE
- Cluster admin policy（K8s admission controller / OPA / Kyverno）→ SRE
- 完整 SSRF 加固（DNS rebinding / RFC1918 internal）→ refactor branch follow-up #52
- 既有 cloud_service helm chart securityContext review → 不在 scope（與 agent-runtime 解耦）
- Marketplace skill trust 強隔離（gVisor / Kata）→ v2

### §6.1 Pod-level securityContext baseline

**Pod spec**（agent-runtime helm chart `templates/deployment.yaml`）：

```yaml
spec:
  template:
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001          # PVC mount ownership
        seccompProfile:
          type: RuntimeDefault   # docker/containerd default seccomp profile
      containers:
        - name: agent-runtime
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
              add: []            # 不需要任何 capability
            # runAsNonRoot 從 pod-level 繼承
          resources:
            requests:
              cpu: 200m
              memory: 256Mi
            limits:
              cpu: 1000m
              memory: 1Gi
```

**對比既有 cloud_service Bitnami chart baseline**：

| Setting | Bitnami（既有 cloud_service） | agent-runtime（本 epic） | 差異理由 |
|---------|--------------------------|------------------------|---------|
| `runAsNonRoot` | 通常 `true` | `true` | 一致 |
| `runAsUser` | 1001 (Bitnami convention) | 10001 (避免衝突) | 區分 |
| `seccompProfile` | 不設（user values 自加）| `RuntimeDefault` (強制) | 嚴格 |
| `readOnlyRootFilesystem` | 不設 | `true` | 嚴格 |
| `allowPrivilegeEscalation` | 不設（K8s default true） | `false` | 嚴格 |
| `capabilities.drop` | 不設 | `["ALL"]` | 嚴格 |

**為什麼比 cloud_service 嚴格**：

- cloud_service 用 Bitnami chart 是因為相容多種 application（Odoo / n8n / PostgreSQL 等），baseline 偏寬以避免破壞 application
- agent-runtime 是自家 image（`woow-paas-agent-runtime`），可以也應該設 stricter baseline
- v1 trust model = user-trusted，但 skill subprocess 仍要 defense in depth（防 prompt injection 誘導 skill 做不該做的事）

**Helm chart 不允許 user override 這些 securityContext**：

- helm values 不暴露 `securityContext.*` 為 user-settable
- 只允許 `agentRuntime.resources` (CPU/memory) 與 `agentRuntime.image.tag` 兩個 user-settable
- 保證 admin cluster-wide baseline 一致

### §6.2 Filesystem 隔離

**Mount layout**（pod 內 view）：

```
/                                      ← rootfs (read-only, image content)
├── /etc/agent-runtime/                ← ConfigMap mount (read-only)
│   ├── mcp.json                       ← per-project MCP snapshot
│   ├── template.json                  ← per-project agent_template snapshot
│   └── skills.json                    ← per-project skill list
├── /run/secrets/                      ← K8s secrets mount (read-only)
│   └── agent-runtime-auth-secret      ← shared secret for HS256 (file-mounted, not env)
├── /tmp/                              ← emptyDir tmpfs (read-write, ephemeral)
│                                        ← subprocess HOME / temporary files
├── /workspace/                        ← PVC mount (read-write, persistent)
│   └── project-{pid}/
│       ├── threads/{channel_id}/      ← LangGraph thread checkpoints
│       ├── shared/                    ← cross-thread memory + notes + exports
│       └── skills/cache/<name>/<ver>/ ← skill bundle cache (§3.5)
└── /var/log/agent-runtime/            ← emptyDir for application logs (read-write, ephemeral)
                                       ← container log stream 自動收 stdout
```

**Volume types**：

| Mount | Type | RW | Persistence | 內容 |
|-------|------|----|----|------|
| `/` | image rootfs | RO | per-image | python / libs / agent-runtime code |
| `/etc/agent-runtime/` | ConfigMap projected | RO | 隨 ConfigMap | MCP / template / skill snapshots |
| `/run/secrets/` | Secret projected | RO | 隨 Secret | shared HS256 secret |
| `/tmp/` | emptyDir + medium=Memory | RW | pod 內，restart 失 | subprocess scratch / Python temp |
| `/workspace/` | PVC | RW | 跨 pod restart | thread checkpoints / shared memory / skill cache |
| `/var/log/agent-runtime/` | emptyDir | RW | pod 內 | application log (also goes to stdout) |

**subprocess fs view**（skill subprocess 繼承 pod mount）：

- skill 看到的 `/` 與 main runtime 相同（無 chroot）
- skill 寫 `/workspace/project-{pid}/skills/cache/<own>/output/`（runtime 設 cwd）
- skill 不能寫 `/etc/agent-runtime/`（RO）
- skill 不能讀 `/run/secrets/`（透過 subprocess env whitelist 防止，§6.4）

**PVC subdir isolation**：

- 同 project 內所有 thread 共用 `/workspace/project-{pid}/threads/` 但不同 channel_id subdir
- runtime 不對 PVC 做 ACL（filesystem-level）— project_access 在 Odoo 端 gate
- subprocess fs ACL 不額外加（v1 trust model 接受 cross-thread leak 風險）

### §6.3 NetworkPolicy egress 細部（Q1 拍板用）

**Three egress allowlist 類別**：

| 類別 | 目標 | 鎖定機制 | 變動頻率 |
|------|-----|---------|---------|
| (1) DNS | `kube-system` ns (UDP/TCP 53) | namespaceSelector + port | 永久 static |
| (2) Sidecar-backed MCP | project.workspace_ids 對應的 cloud_service ns | namespaceSelector + `woow.tw/workspace-id` label | 隨 project.cloud_service_ids 變動 |
| (3) External MCP + AI provider | 任意外網 URL（含 cluster.local 外） | **白名單機制（Q1 拍板）** | 隨 project 配置 |

**NetworkPolicy yaml skeleton**（helm chart 渲染）：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: agent-pa-{hash}-egress
  namespace: paas-ws-pa-{hash}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: agent-runtime
  policyTypes: [Egress]
  egress:
    # (1) DNS
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
    # (2) Sidecar-backed MCP — workspace_ids 集合渲染為 N 條 rule
    {{- range .Values.agentRuntime.workspaceIds }}
    - to:
        - namespaceSelector:
            matchLabels:
              woow.tw/workspace-id: {{ . | quote }}
      # MCP sidecar 通常 port 3000 / 3001
      ports:
        - protocol: TCP
          port: 3000
        - protocol: TCP
          port: 3001
    {{- end }}
    # (3) External egress — 依 hybrid scheme 決定（Q1）
    {{- if .Values.agentRuntime.externalEgress.enabled }}
    - to:
        - ipBlock:
            cidr: 0.0.0.0/0
            except:
              - 10.0.0.0/8       # cluster pod CIDR
              - 172.16.0.0/12
              - 192.168.0.0/16
              - 169.254.0.0/16   # link-local (含 cloud metadata 169.254.169.254)
              - 100.64.0.0/10    # CGNAT (Tailscale 等)
              - 127.0.0.0/8      # loopback
      ports:
        - protocol: TCP
          port: 443
        - protocol: TCP
          port: 80
    {{- end }}
```

**External MCP 白名單機制三選項**：

| 方案 | 機制 | 細粒度 | 維運 cost | 推薦 |
|------|------|-------|---------|------|
| (a) Per-project ConfigMap FQDN whitelist | helm chart 渲染 NetworkPolicy 對個別 FQDN allow | 最細（per-URL） | 高（NetworkPolicy 不支援原生 FQDN，需 egress gateway like Cilium or fork policy）| ✗ v1 過度 |
| (b) Cluster-level admin allowlist | global allowlist，user 不可加 | 粗（cluster 一致）| 低 | ✗ 不符合 project 自治 |
| **(c)（推薦）** Hybrid: project-level boolean + global RFC1918 deny | `project.external_mcp_allowed = Boolean`，True → 渲染 ipBlock allow 0.0.0.0/0 minus RFC1918 + cloud metadata；False → 純 sidecar-backed only | 中（per-project on/off）| 低 | ✓ |

**選 (c) 的具體機制**：

- Odoo `project.external_mcp_allowed` boolean field（**不算動 project schema 紅線嗎？** — 是 additive field，本 epic in-scope 蘊含）
- 預設 False（保守）
- admin 在 Odoo project setting 顯式打開（需顯式同意 SSRF 風險）
- 打開後：渲染 NetworkPolicy egress (3) rule 允許 0.0.0.0/0 minus RFC1918 + cloud metadata + port 80/443
- 改變該 boolean → 觸發 `sync-workspaces` endpoint → helm upgrade NetworkPolicy

**為什麼 (a) per-FQDN allowlist 不可行（v1）**：

- K8s NetworkPolicy 原生不支援 FQDN（只 IP / CIDR / podSelector / namespaceSelector）
- FQDN egress 需要：
  - CNI 支援 FQDN policy（Cilium 支援；Calico 部分支援；K3s default Flannel 不支援）
  - Egress gateway / proxy（Squid / Istio egress gateway）— 多一條維運線
- 本 cluster (woow-k3s) 用 Flannel — 不支援
- 留 v2 評估升級 CNI 或加 egress gateway

**為什麼 (b) cluster-level 不行**：

- 不同 project 有不同 external 需求（A 用 OpenAI，B 用 Anthropic，C 用內部 LLM）
- cluster-level allowlist 無法區分 per-project
- 與 project 一等公民理念衝突

### §6.4 Subprocess spawn 約束（Q2 拍板用）

**phase-planner 推薦 rlimit baseline**：

| Resource | 值 | 對應 SKILL.md frontmatter | 備註 |
|----------|---|--------------------------|------|
| `RLIMIT_CPU` | 30s | （無對應；bundle 不可放寬）| CPU time hard limit |
| `RLIMIT_AS` (virtual memory) | 512 MiB | （無對應）| Address space limit |
| `RLIMIT_NOFILE` | 64 | （無對應）| Open file descriptors |
| `RLIMIT_NPROC` | 16 | （無對應）| Child procs / threads per user |
| `RLIMIT_FSIZE` | 100 MiB | （無對應）| Max single file write |
| `subprocess.run(timeout=)` | 60s | `timeout_seconds`（bundle hint，host cap）| Wall clock |
| stdout max | 256 KiB | `max_output_kb`（bundle hint，host cap）| stdout buffer cut-off |

**subprocess spawn pseudocode**（agent-runtime image `runtime/security/subprocess_runner.py`）：

```python
import os
import resource
import subprocess
import signal

# Constants — cannot be widened by SKILL.md bundle declare
_HOST_HARD_RLIMITS = {
    'cpu_seconds': 30,
    'rss_bytes': 512 * 1024 ** 2,
    'nofile': 64,
    'nproc': 16,
    'fsize_bytes': 100 * 1024 ** 2,
}
_HOST_WALL_TIMEOUT_SEC = 60
_HOST_MAX_STDOUT_BYTES = 256 * 1024

def _set_rlimits():
    """Apply rlimits in child process before exec."""
    r = _HOST_HARD_RLIMITS
    resource.setrlimit(resource.RLIMIT_CPU,    (r['cpu_seconds'], r['cpu_seconds']))
    resource.setrlimit(resource.RLIMIT_AS,     (r['rss_bytes'],    r['rss_bytes']))
    resource.setrlimit(resource.RLIMIT_NOFILE, (r['nofile'],       r['nofile']))
    resource.setrlimit(resource.RLIMIT_NPROC,  (r['nproc'],        r['nproc']))
    resource.setrlimit(resource.RLIMIT_FSIZE,  (r['fsize_bytes'],  r['fsize_bytes']))

def _safe_env(skill_meta):
    """Build env whitelist for skill subprocess. NO secrets pass-through."""
    return {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'HOME': '/tmp',
        'LANG': 'en_US.UTF-8',
        'PROJECT_HASH': os.environ['PROJECT_HASH'],   # readable for self-id
        # explicit NOT passed (defense in depth, env whitelist):
        # OPENAI_API_KEY / MCP api_key / SHARED_SECRET / DATABASE_URL / ...
    }

def spawn_skill_subprocess(skill_path, args, skill_meta, invoker_role):
    """Spawn skill with hard rlimit + env whitelist + wall timeout."""
    env = _safe_env(skill_meta)
    bundle_timeout = min(
        skill_meta.get('timeout_seconds', _HOST_WALL_TIMEOUT_SEC),
        _HOST_WALL_TIMEOUT_SEC,
    )
    try:
        proc = subprocess.run(
            [skill_path, *args],
            env=env,
            preexec_fn=_set_rlimits,         # Unix only; agent-runtime 跑 Linux pod
            timeout=bundle_timeout,
            stdin=subprocess.PIPE,
            input=json.dumps(skill_meta.get('input_args', {})),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            cwd=f'/workspace/project-{os.environ["PROJECT_ID"]}/skills/cache/{skill_meta["name"]}/{skill_meta["version"]}',
        )
        # Cut stdout to host max (bundle declare cannot raise)
        bundle_max = min(
            (skill_meta.get('max_output_kb', 256) * 1024),
            _HOST_MAX_STDOUT_BYTES,
        )
        stdout = proc.stdout[:bundle_max]
        return {
            'exit_code': proc.returncode,
            'stdout': stdout,
            'stderr': proc.stderr[:8192],   # stderr capped harder
            'truncated_stdout': len(proc.stdout) > bundle_max,
        }
    except subprocess.TimeoutExpired:
        # subprocess.run timeout 已 SIGKILL；rlimit CPU 30s 也會自殺
        return {'exit_code': -1, 'stdout': b'', 'stderr': b'TIMEOUT', 'timeout': True}
```

**Skill 拿 MCP / AI 的 indirect 機制**：

- skill **不能直接呼** MCP API 或 OpenAI API（env 無 api_key）
- skill 透過 stdin 接 args；透過 stdout return result
- LangGraph runtime 把 skill 包成 `BaseTool`；runtime 主動 dispatch（先 call skill → skill stdout 帶結果 → runtime 再決定下步驟）
- skill 想要「呼 MCP」？skill 用 LangGraph runtime 提供的「subprocess 通訊協議」回 `{call_mcp: <name>, args: {...}}` → runtime parse 後 dispatch；但這變成 nested invocation，**v1 不支援**（單純 single-shot skill）
- v2 評估 nested skill→MCP invocation 路徑

**為什麼 env 完全不 pass-through secrets**：

- prompt injection 防護：LLM 被誘導 call skill，skill 若有 OPENAI_API_KEY → 可能上傳外送
- skill 在 trust 模型內是 user-trusted，但 defense in depth：secret 是 platform-level，不該被 user-bundle 接觸

### §6.5 Cluster-internal TLS（Q3 拍板用）

**phase-planner 推薦：(b) v1 不強制 cluster-internal TLS；依賴 NetworkPolicy + namespace 物理隔離 + 對外強制 HTTPS**

兩選項：

| 方案 | Cluster-internal 流量 | 對外流量 | 實作 cost | 推薦 |
|------|---------------------|---------|---------|------|
| (a) 強制 TLS（mTLS or one-way）| Odoo↔Pod / pod↔mcp_server / pod↔AI_provider 全 TLS | 同 (b) | 高（cert lifecycle / rotation / CA / SAN）| ✗ |
| **(b)（推薦）** 不強制 cluster-internal TLS | ws:// + http:// cluster-internal；NetworkPolicy + ns 物理隔離 | external MCP / AI provider 強制 HTTPS（refactor branch SSRF guard scheme check 已 enforce）| 低 | ✓ |

**選 (b) 的理由**：

- K8s NetworkPolicy + namespace 物理隔離 + CNI 級的 cluster pod CIDR 隔離已提供足夠 isolation for v1
- mTLS 維運 cost 高（cert lifecycle / rotation / CA / SAN）— 對於 v1 trust model（user-trusted skill + admin-managed infra）是 over-engineering
- Cluster-internal 流量在 worker node 上的 CNI plugin 級別本就難被嗅探（前提：cluster admin policy 沒被 compromise）
- 對外（external MCP / AI provider）強制 HTTPS — 由 `mcp_server.url` scheme check 與 `_validate_mcp_url` 既有邏輯 enforce

**(b) 加固點**：

- shared secret 走 K8s Secret + mount as file（不寫 env 防止 process inspection leak）
- HS256 token in WS URL query string 不適合（log 會記）— 改用 `Authorization: Bearer <token>` header（既有 §5.3 推薦）
- paas-operator → Odoo webhook 走 HTTPS（既有 cloudflare ingress / cluster ingress LB）
- 對 external MCP / AI provider 強制 HTTPS — refactor branch `_validate_mcp_url` 拒非 http(s) scheme（既有）；本 epic 再加 pod 端 outbound check（defense in depth）

**v2 path to TLS**（不在本 epic）：

- 引入 service mesh（Istio / Linkerd）→ 自動 mTLS
- 或：手動 cert-manager + envoy sidecar
- 配套 cert rotation 自動化

### §6.6 Token replay defense（Q4 拍板用）

**phase-planner 推薦：(a) in-memory LRU TTL cache per pod + 雙 secret window rotation**

**jti cache 三選項**：

| 方案 | 機制 | 跨 pod 一致 | Pod restart 後 | 實作 cost | 推薦 |
|------|------|-----------|--------------|---------|------|
| **(a)（推薦）** In-memory LRU TTL per pod | Python `cachetools.TTLCache(maxsize=10000, ttl=65*60)` | 否（pod 間獨立）| Cache reset；short replay window risk | 低 | ✓ |
| (b) Redis shared across pods | 跨 pod 一致 | 是 | 持久 | 高（多 1 service 依賴 + 認證 + 容量） | ✗ |
| (c) ConfigMap pod-local persist | K8s API call 太慢 for hot path | 否 | 持久 | 高 | ✗ |

**選 (a) 的具體機制**：

```python
# agent-runtime image runtime/security/jti_cache.py
from cachetools import TTLCache
from threading import Lock

# 1 hour token exp + 5 min clock skew buffer = 65 min cache TTL
_JTI_CACHE = TTLCache(maxsize=10_000, ttl=65 * 60)
_JTI_LOCK = Lock()

def check_and_record_jti(jti: str) -> bool:
    """Return True if jti is fresh; False if already seen (replay)."""
    with _JTI_LOCK:
        if jti in _JTI_CACHE:
            return False
        _JTI_CACHE[jti] = True
        return True
```

**Replay window risk on pod restart**：

- pod restart 後 cache 重置 → 已用過 jti 可被 replay 一次
- 但：token exp = 1h，pod restart 多在 reap (≥30 min idle) 才發生 — restart 時所有 active token 多已過期
- v1 接受此 risk（trust model + 1h exp 期短）
- v2 評估 Redis（若 high-frequency replay attack 出現）

**Shared secret rotation 配套（v1 admin 手動 + 雙 secret window）**：

K8s Secret yaml：
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: agent-runtime-auth-secret
  namespace: paas-system
type: Opaque
data:
  current: <base64 of current HS256 secret>
  previous: <base64 of previous HS256 secret>  # nil 或同 current 在第一次部署時
```

**Rotation 流程**：

1. **Phase 0**（穩定態）：secret 內 `current=X`, `previous=X`（值相同）；paas-operator 簽 token 用 X；pod 驗 token 試 X
2. **Phase 1**（admin 開始 rotation）：admin 生成 Y → 改 K8s Secret 為 `current=Y, previous=X` → `kubectl rollout restart deployment/paas-operator`
3. **Phase 2**（active token 過期窗口，1h）：
   - paas-operator 新簽 token 用 Y
   - 既有 active token（用 X 簽）仍 valid（pod 端驗時試 current=Y fail → fall back try previous=X succeed）
   - 既有 active connection 不中斷
4. **Phase 3**（過期窗口結束後）：admin 改 K8s Secret 為 `current=Y, previous=Y` → rollout restart → 完成 rotation

**Pod 端驗證 pseudocode**：

```python
# agent-runtime image runtime/transport/auth.py
import jwt

def verify_token(token: str) -> dict:
    """Try current secret first; fallback to previous (during rotation window)."""
    secrets = read_secrets_from_mount()  # /run/secrets/agent-runtime-auth-secret/
    current = secrets['current']
    previous = secrets.get('previous', current)

    last_err = None
    for sec in (current, previous):
        try:
            claims = jwt.decode(token, sec, algorithms=['HS256'])
            if not check_and_record_jti(claims['jti']):
                raise ValueError('Token replay detected (jti reused)')
            return claims
        except jwt.InvalidSignatureError as e:
            last_err = e
            continue
        except jwt.ExpiredSignatureError:
            raise   # exp check fail — no point trying previous secret
    raise ValueError(f'Token signature invalid against both current/previous: {last_err}')
```

### §6.7 SSRF guard

**refactor branch 已有 minimal SSRF guard（保留繼承）**：

`controllers/paas.py._validate_mcp_url` 已 enforce：

- 拒 169.254.169.254 (AWS/GCP/Azure IMDSv1 IPv4)
- 拒 IPv6 link-local (fe80::/10)
- 拒 fd00:ec2::254 (AWS IMDSv2 IPv6)
- 拒非 http(s) scheme
- **允許 cluster.local**（sidecar-backed URL 形態必須允許）

**本 epic 補強（defense in depth）**：

- Pod 端 outbound MCP / AI provider HTTP client 在 connection 前**再次** SSRF check
  - location：agent-runtime image `runtime/security/url_validator.py`
  - 在 `langchain-mcp-adapters` 建 `MultiServerMCPClient` 前 validate config['url']
  - 在 `langchain_openai.ChatOpenAI` 建 client 前 validate `api_base_url`
- 不擴大 SSRF allowlist（既有 minimal 足）
- 配合 §6.3 NetworkPolicy egress hybrid scheme：external MCP 必須通過 `project.external_mcp_allowed=True`
  + URL 通過 SSRF check + DNS resolve 結果不能落 RFC1918 / cloud metadata IP

**本 epic 不做**（留 follow-up #52）：

- DNS rebinding 防護：時間差 attack（first resolve to public IP，second resolve to private IP）
- TOCTOU race：URL validate 通過後實際 connect 時 DNS 變動
- 完整 RFC1918 deny on egress（K8s NetworkPolicy 已 except RFC1918 to cluster pod CIDR，但 worker node DNS / hostNetwork 等 corner case 仍可能繞）

### §6.8 Odoo worker pool sizing（Q5 拍板用）

**問題敘述**：

§5.6 SSE generator 設計為「open WS to pod → relay envelopes」— 每 active conversation 阻塞 1 Odoo worker thread（generator 內 asyncio loop run）。

**phase-planner 推薦公式**：

```
required_workers = peak_concurrent_active_conv + baseline_workers
                 = N + 16
```

| 變數 | 預設值 | 解釋 |
|------|-------|------|
| `peak_concurrent_active_conv` | 50（v1 SLO 上限）| 100 user × 50% 同時 chat 假設 |
| `baseline_workers` | 16 | admin login / cron / RPC / OAuth / other API |
| 推薦 | 66 | v1 deployment baseline |

**警告機制**：

- Odoo log warn if active SSE generator count > `0.8 * workers`：
  ```
  Odoo cron 每 30s 檢查 active SSE generator count（透過 controller-level counter）
  超過 0.8 * workers → log warn + 寫 ir.config_parameter telemetry
  ```
- Dashboard widget（後台 Settings → Woow PaaS）顯示：
  - Active SSE conn count
  - Worker utilization %
  - Queue depth（pending SSE requests）

**Worker 不夠時行為**：

- 第 N+1 個 SSE 請求進來時，werkzeug 在 worker pool 排隊
- 超過 Odoo `proxy_read_timeout`（default 60s）→ ingress 端 timeout
- Frontend 收到 SSE 連線失敗 → 顯示「服務忙碌中，稍後再試」

**Deployment 拍板**：

- helm chart values 暴露 `odoo.workers` 為 helm parameter
- v1 baseline 66；隨 active conv 增長線性升
- 實際 N 值 deployment 階段拍板（不在 §6 scope）

**長期 path（v2）**：

- 引入 async controller（Odoo 18 有部分 async 支援）— 不需要 1 worker per conv
- 或：SSE relay 拉到獨立 sidecar（Odoo 不直接維持長連線；sidecar 接 SSE + paas-operator 端 WS）

### §6.9 RBAC

**Pod ServiceAccount 權限**（agent-runtime helm chart `templates/role.yaml`）：

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: agent-pa-{hash}
  namespace: paas-ws-pa-{hash}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: agent-pa-{hash}-pod-self
  namespace: paas-ws-pa-{hash}
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    resourceNames:
      - "agent-pa-{hash}-mcp"
      - "agent-pa-{hash}-template"
      - "agent-pa-{hash}-skills"
    verbs: ["get", "watch"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    resourceNames: ["agent-pa-{hash}"]
    verbs: ["patch"]   # 自己 patch 自己 annotation last-active
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: agent-pa-{hash}-pod-self
  namespace: paas-ws-pa-{hash}
subjects:
  - kind: ServiceAccount
    name: agent-pa-{hash}
    namespace: paas-ws-pa-{hash}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: agent-pa-{hash}-pod-self
```

**為什麼 pod 不允許 list other pods / read other secrets / 動 namespace**：

- 最小權限原則
- pod 內 LangGraph runtime + skill subprocess 都不需要這些
- 若 future feature 需要（如「skill 互相 invoke」）再評估

**paas-operator ServiceAccount 對 agent ns 的權限**（既有 helm.py `helm_service` + `k8s_service` 走 service account）：

- create/delete/get/list/patch namespace（已有，限定 `paas-ws-` prefix）
- helm release CRUD（透過 helm CLI binding to operator's SA）
- create/patch ConfigMap（用於 spawn-or-attach 寫 mcp.json / template.json / skills.json）
- patch Deployment replicas（scale to 0/1，§2.3 reaper / resume）
- patch namespace annotations（TTL: `sandbox.woow.tw/expires`，§2.5 GC）
- **不允許**：
  - `pods/exec` / `pods/log`（v1 不開 admin shell；v2 評估）
  - 跨 cluster admin operation

**ServiceAccount token mount 政策**：

- pod 不需要 ServiceAccount token （default automountServiceAccountToken=true 是大坑）
- helm chart `deployment.yaml` 設 `automountServiceAccountToken: false`
- 純 token replay 風險：若 pod 被入侵，attacker 拿不到 SA token → 不能呼 K8s API
- runtime 自己 watch ConfigMap 用 inotify 不用 K8s API（不需要 SA token）

**Q6 — Pod RBAC granularity（額外拍板）**：

phase-planner 推薦的 RBAC 是最小：pod 只能 read 自己 ConfigMap + patch 自己 Deployment annotation。三選項：

| 方案 | Pod RBAC | 風險 | 推薦 |
|------|---------|------|------|
| (i) 最小（推薦）| read own ConfigMap + patch own Deployment annotation；無 SA token mount | 最低 | ✓ |
| (ii) 中等 | + read own pod status + list own pods | 中（pod 可探測自己狀態） | ✗ 不必要 |
| (iii) 寬鬆 | + read other ConfigMap in same ns | 高（cross-project ConfigMap leak） | ✗ |

### §6.10 Audit logging

**Pod 端結構化 JSON log**（stdout，K8s logging stack 收集）：

```json
{
  "ts": "2026-05-22T10:30:45.123Z",
  "level": "info",
  "event": "tool_invoke",
  "project_hash": "jbswy3dpehpk3pxp",
  "channel_id": 42,
  "request_id": "uuid-v4",
  "invoker_user_id": 105,
  "invoker_role": "user",
  "tool_name": "search_docs",
  "tool_type": "skill",     // or "mcp"
  "duration_ms": 234,
  "status": "ok"            // or "error", "timeout", "unavailable"
}
```

**事件 types**：

| Event | trigger | Pod or Odoo |
|-------|---------|-------------|
| `pod_spawn` | pod startup 完成 | Pod |
| `pod_reap` | scale to 0 完成 | paas-operator |
| `pod_resume` | scale to 1 完成 | paas-operator |
| `ws_connect` / `ws_disconnect` | WS handshake | Pod |
| `user_message_received` | first envelope | Pod |
| `tool_invoke` | LangGraph dispatch | Pod |
| `tool_unavailable` | tool resolve fail | Pod |
| `mcp_invalidate_received` / `template_update_received` | WS hot reload event | Pod |
| `auth_failure` | token verify fail | Pod |
| `replay_attempt_blocked` | jti cache hit | Pod |
| `subprocess_timeout` / `subprocess_oom` | skill rlimit hit | Pod |
| `error` | runtime exception | Pod |

**Odoo 端 audit log**：

- Webhook signature verify fail → log warn + counter（既有 Odoo log infra）
- project_access role 變動 → mail.message audit trail（沿用 Odoo mail.thread audit pattern）
- agent quota override 變動 → audit
- spawn-or-attach call → audit (debug level)
- secret rotation phase change → log critical

**不在本 §6 scope（→ deployment / SRE）**：

- cluster logging stack 細節（Loki / Elastic / Vector / Fluentd）
- log retention policy
- log aggregation / dashboard / alerting rule
- PII redaction in audit log

### §6.11 Affected files / new files（§6 範圍預測）

**新建（agent-runtime image source）**：

- `runtime/security/subprocess_runner.py` — rlimit + env whitelist + timeout
- `runtime/security/url_validator.py` — pod 端 SSRF check (defense in depth)
- `runtime/security/jti_cache.py` — TTLCache + lock
- `runtime/security/audit_logger.py` — structured JSON logger to stdout
- `runtime/transport/auth.py` — already in §5.9；本節補 dual-secret rotation 支援

**新建（agent-runtime helm chart `woow-paas-charts/charts/agent-runtime/templates/`）**：

- `network-policy.yaml` — egress 三類 allowlist（DNS + sidecar-backed + external hybrid）
- `service-account.yaml` — pod ServiceAccount
- `role.yaml` — minimal RBAC（read own ConfigMap + patch own Deployment annotation）
- `role-binding.yaml` — bind SA to Role
- `secret.yaml` — `agent-runtime-auth-secret` 包 current/previous 雙 key（從 cluster Secret reference）

**修改（Odoo addon）**：

- `src/models/project_project.py` — 加 `external_mcp_allowed = fields.Boolean(default=False)`（§6.3 hybrid scheme）
- `src/models/__init__.py` — 確認 import
- `src/views/project_external_mcp_setting.xml` — admin UI 顯式同意 SSRF risk + 切換 boolean
- `src/security/ir.model.access.csv` — project external_mcp_allowed 屬於 project admin（透過 project_access 控）

**修改（paas-operator）**：

- `extra/paas-operator/src/api/agent_runtimes.py` — sync-workspaces 內讀 project.external_mcp_allowed + 渲染對應 NetworkPolicy values
- `extra/paas-operator/src/services/jwt_signer.py` — dual-secret rotation 支援（read current + previous from Secret）
- `extra/paas-operator/src/services/audit_logger.py` — webhook outbound + audit emit

**不動**（保護 invariants）：

- 既有 `controllers/paas.py._validate_mcp_url` SSRF guard（refactor branch cycle 4 finalized）
- `mcp_server` / `mcp_tool` schema / ACL
- `cloud_service` 既有 securityContext（cloud_service 用 Bitnami chart 不收緊；agent-runtime 自家設 stricter）
- Odoo std field

### §6.12 Risks

| Risk | 嚴重度 | Mitigation | Rollback |
|------|-------|-----------|---------|
| Odoo worker pool 不夠 → SSE 排隊 / timeout | 高 | warn at 0.8 utilization；Dashboard 顯示；helm chart 暴露 `odoo.workers` 可調 | helm upgrade 加 worker；長期 path 走 v2 async controller |
| Subprocess fork bomb → cgroup limit hit → pod crash | 中 | RLIMIT_NPROC=16 + cgroup memory limit；pod restart 自動 | 持續 crash → admin 手動 reset workspace（清 PVC） |
| RLIMIT_NOFILE 太低（64）→ skill 開檔失敗 | 低 | 大多 skill 不需要 > 64 file；可 per-skill SKILL.md request raise（host cap 仍 in place）| 提高 baseline if real bottleneck |
| External MCP allowlist 漏網 → SSRF | 高 | hybrid scheme `external_mcp_allowed=False` default；pod 端 url validate；NetworkPolicy ipBlock except RFC1918 | admin 改 external_mcp_allowed=False；audit log review |
| jti cache pod restart → short replay window | 中 | token exp 1h + pod restart 主要在 reap (≥30 min idle)；active token 多已過期 | v2 評估 Redis |
| Shared secret 過期未 rotate → spawn-or-attach 失敗 | 高 | dual-secret window；admin alert 30 day 提醒 | 緊急 rotation（流程 §6.6） |
| Audit log 失敗（stdout 滿 / collector down）| 中 | container log stream fallback；K8s logging stack alert | logging stack 自己的 SRE |
| NetworkPolicy K3s reply-traffic bug | 中 | dev-sandbox chart 已 patch（[[mcp-url-redesign]] 已記）；agent-runtime chart 沿用 | helm upgrade |
| Subprocess child 透過 RLIMIT_NPROC 自殺 | 低 | 16 child procs/threads 對 skill 足夠；rare case 提高 | per-skill RLIMIT raise |
| Pod RBAC patch annotation 太常 → etcd 壓力 | 中 | last-active annotation 30s 內 throttle；high-traffic project 才頻繁 | throttle interval up |
| ServiceAccount token mount → token leak | 高 | `automountServiceAccountToken: false`；pod 不需要 SA token | helm chart enforce |
| ConfigMap update lag 60s → mid-flight stale | 中 | WS push 主路徑 < 5s；inotify 兜底；mid-flight tool_call 完成走舊（§5.7） | hot reload |
| Subprocess stdout 超過 256 KiB cut-off | 低 | bundle hint `max_output_kb` <= 256；超過 truncate + log warn | per-skill request raise |
| Subprocess timeout 超過 30s CPU + 60s wall | 低 | RLIMIT_CPU + wall timeout 雙保險；skill 應分多 invocation | per-skill request raise |
| Pod admin endpoint（reset workspace etc）被誤觸 | 中 | admin endpoint 走獨立 auth + project_access role check | audit log 反查 |
| 雙 secret rotation 中途新 token 拒舊 pod | 低 | rotation 期間 pod 端嘗 current then previous；ws connection 短連（§5.11 sign-off）→ rotation 不破現有 conn | rotation flow §6.6 |
| External MCP URL DNS rebinding attack | 中 | v1 不防（follow-up #52）；mitigation：pod 端 cache DNS result 短期；連續 connect 用同 IP | follow-up #52 |
| project_access role 變動延遲反映到 pod | 低 | role check 在 Odoo controller 端 gate（每 user_message 一次）；pod 端不 cache role；隨即生效 | OK |
| Audit log PII 漏洩（user_id / message body）| 中 | structured JSON log redact body；user_id 保留（內部 ID）；mail.message body 仍在 Odoo DB | log redaction policy |

### §6.13 Open questions for leader sign-off（§6 escalation）

> 6 條核心拍板需求。phase-planner 推薦已 inline 在 §6.3 / §6.4 / §6.5 / §6.6 / §6.8 / §6.9 給出。**全部 sign-off 2026-05-22**。

1. ~~**[需 leader 拍板] External MCP allowlist 機制（§6.3）**~~ **[SIGNED-OFF 2026-05-22]** (c) Hybrid: `project.external_mcp_allowed` boolean + 0.0.0.0/0 minus RFC1918+cloud metadata egress + port 80/443
2. ~~**[需 leader 拍板] Subprocess rlimit baseline 數值（§6.4）**~~ **[SIGNED-OFF 2026-05-22]** CPU 30s / RSS 512 MiB / NOFILE 64 / NPROC 16 / FSIZE 100 MiB / wall 60s / stdout 256 KiB
3. ~~**[需 leader 拍板] Cluster-internal TLS 強制與否（§6.5）**~~ **[SIGNED-OFF 2026-05-22]** (b) v1 不強制 cluster-internal TLS + 對外 HTTPS 強制（refactor branch SSRF guard 已 enforce）
4. ~~**[需 leader 拍板] Token replay defense + secret rotation（§6.6）**~~ **[SIGNED-OFF 2026-05-22]** (a) in-memory `TTLCache(maxsize=10_000, ttl=65*60)` per pod + 雙 secret window 4-phase 手動 rotation
5. ~~**[需 leader 拍板] Odoo worker pool sizing（§6.8）**~~ **[SIGNED-OFF 2026-05-22]** baseline 公式 `peak_concurrent_active_conv + 16`；v1 預設 66 workers；warn at 0.8 utilization + dashboard widget；deployment 階段定實際 N
6. ~~**[需 leader 拍板 — 新增] Pod RBAC granularity（§6.9）**~~ **[SIGNED-OFF 2026-05-22]** (i) 最小 — read own ConfigMap + patch own Deployment annotation；`automountServiceAccountToken: false`

**§6 額外探出來但不阻塞 §6 sign-off**（§7 / implementation / deployment 處理）：

- `project.external_mcp_allowed` field 加在 project 上是 additive schema change（不算 task brief 紅線「動 project schema」） — §7 prereq 已蘊含可加 additive field
- pod log collection stack（Loki / Elastic / Vector）— deployment / SRE
- 對 LangGraph runtime in-process 共享 mcp_snapshot 的 thread-safety — implementation 階段（asyncio + threading 邊界）
- Subprocess child 對 PVC `/workspace/project-{pid}/shared/` 寫權限與 `invoker_role` 對應的 enforcement（§4.5 寫權限矩陣的 pod 端 implementation 細節）— implementation
- Audit log retention / SIEM 整合 — SRE
- 完整 SSRF 加固（DNS rebinding / TOCTOU）— refactor branch follow-up #52
- gVisor / Kata for marketplace skill（v2）— 不在本 epic

## §7 Migration path (LangGraph 從 Odoo 搬遷的階段策略)

> **Status**：Phase 1 — §7 full draft submitted, awaiting sign-off
> §7.0 prerequisite 已 sign-off 2026-05-21；§7.0.7 補上 additive schema 集成（含 §6 sign-off 的 `project.external_mcp_allowed`）；§7.1 - §7.10 為本次新加 full draft
> 5 條 [需 leader 拍板] 留 §7.10 escalate（migration phase 切片 / cross-repo CI trigger / dual-write window / ai_client.py 刪除時機 / Fleet bundle 對接時機）

> §7 完整 draft 留後續 phase；本節先 inline 寫 **prerequisite section**（per-project pivot 對 §7 的硬約束）。

### §7.0 Prerequisite（本 epic implementation 落地前必須完成）

#### §7.0.1 上游 refactor merge

> **[SIGNED-OFF 2026-05-21 — 策略 A; PR #54 merged at 047ebf8]**
> `refactor(mcp): drop D5 invariant + retire sidecar auto-create + redesign user-scope MCP URL` 已 merge develop。本 epic design 階段繼續推 §3~§7 不需立刻 rebase；implementation phase 啟動時再 rebase `feature/agent-pod` 取得 refactor 變更。

**`feature/refactor-user-scope-mcp` branch 必須先 merge `develop`** — 本 epic 的所有 design 假設都建立在以下 refactor branch 已 finalize 的事實上：

- D5 invariant 已拔（mcp_server.py `_check_user_scope_requires_bound_project` 整段刪）
- `sidecar_backed` boolean field 已加在 mcp_server model
- `cloud_service._do_cleanup` 與 `unlink()` override filter 都已改寫為 `lambda s: s.scope == 'user' and s.sidecar_backed`
- 6-rule ACL split（dual-leg variant）已落地：`mcp_server_rule_*` + `mcp_tool_rule_*` × 3 對 = 6 rule，含 `workspace_user` × 2 的 dual-leg domain（bound walk OR unbound carve-out）
- 新 invariants Inv #9 / Inv #10 / Inv #11 已 final canonical home 在 `docs/decisions/refactor/mcp-url-redesign.md`

**未 merge 之前**：~~本 epic implementation 不能開動（會跟 refactor branch 內容衝突）~~ **[N/A 2026-05-21]** — refactor 已 merge develop (047ebf8)。

**本 epic branch 策略**：

- ~~推薦 option A：implementation 階段 rebase `feature/agent-pod` 到含 refactor 變更的 develop~~ **[SIGNED-OFF 2026-05-21 — 策略 A]** 採用
- ~~替代 option B：直接從 `feature/refactor-user-scope-mcp` 開新 branch `feature/agent-pod-impl`~~ 不採用

#### §7.0.2 Project schema migration（M2O → M2M）— §7 必做

從 refactor branch 1:N 狀態 → 本 epic M:N 目標狀態，schema migration 必須走 **dual-write + backfill** 序列（不可一刀 drop column）：

**目標 schema**（本 epic 落地後）：

```python
class ProjectProject(models.Model):
    _inherit = 'project.project'

    # 新欄位（本 epic 新加）
    cloud_service_ids = fields.Many2many(
        'woow_paas_platform.cloud_service',
        relation='project_cloud_service_rel',
        column1='project_id',
        column2='cloud_service_id',
        string='Cloud Services',
    )
    workspace_ids = fields.Many2many(
        'woow_paas_platform.workspace',
        compute='_compute_workspace_ids',
        store=True,
    )

    # 舊欄位（保留 deprecated 一個版本 — phase-out plan 見下）
    cloud_service_id = fields.Many2one(  # DEPRECATED
        'woow_paas_platform.cloud_service',
        ondelete='set null',
        help='DEPRECATED — use cloud_service_ids; kept for backward compat one version',
    )
```

**Migration step（version `18.0.1.0.26` 假設）**：

1. **Step 1 — 新 M2M relation table create**（`pre-migrate.py`）：
   ```sql
   CREATE TABLE IF NOT EXISTS project_cloud_service_rel (
       project_id INT NOT NULL REFERENCES project_project(id) ON DELETE CASCADE,
       cloud_service_id INT NOT NULL REFERENCES woow_paas_platform_cloud_service(id) ON DELETE CASCADE,
       PRIMARY KEY (project_id, cloud_service_id)
   );
   ```
2. **Step 2 — backfill 既有 1:1 binding 到 M2M table**：
   ```sql
   INSERT INTO project_cloud_service_rel (project_id, cloud_service_id)
   SELECT id, cloud_service_id
   FROM project_project
   WHERE cloud_service_id IS NOT NULL
   ON CONFLICT DO NOTHING;
   ```
3. **Step 3 — 刪 `UNIQUE(cloud_service_id)` SQL constraint**：
   ```sql
   ALTER TABLE project_project DROP CONSTRAINT IF EXISTS project_project_unique_cloud_service;
   ```
4. **Step 4 — 留 `cloud_service_id` column 為 deprecated**（**不 drop**）：
   - 既有 read path 仍可用 `project.cloud_service_id` 拿 primary（取 `cloud_service_ids[:1]` 邏輯）
   - 既有 write path（如 PR #47 PR #53 內的 controller / cron）短期可繼續寫 `cloud_service_id` field，但 model `write()` override 同步 `cloud_service_ids`
   - 下一個版本（`18.0.1.0.27`+）才 drop column
5. **Step 5 — `workspace_id` (related M2O) → `workspace_ids` (computed M2M)**：
   - 不 drop `workspace_id` field，改 deprecated（同上策略）
   - 加新 `workspace_ids` computed
   - 既有 `task.workspace_id = related('project_id.workspace_id')` 短期仍可工作（拿 first workspace）
6. **Step 6 — `cloud_service.project_ids` 從 One2many 改 Many2many reverse**（同一 relation table）

**Migration regression test 範圍**：

- Backfill 後既有 record 的 `cloud_service_ids` 集合 = {`cloud_service_id`}（1 元素 set）
- 刪 constraint 後可手動加第 2 cloud_service to `cloud_service_ids`（但 `cloud_service_id` 不變）
- `project.cloud_service_id = X` write 觸發 model override 同步 `cloud_service_ids` add X（idempotent）
- `cloud_service_id` 讀取邏輯 = `cloud_service_ids[:1].id`（model property override，避免兩個 field source of truth）

#### §7.0.3 Task model 連鎖修改

`project.task` 既有：

- `primary_cloud_service_id = related('project_id.cloud_service_id')`
- `extra_cloud_service_ids = Many2many(..., domain="[('workspace_id', '=', workspace_id)]")`

新 schema 下：

- **`primary_cloud_service_id`** — **[SIGNED-OFF 2026-05-22 — hybrid (c)]** 獨立 `Many2one`；default 從 `project_id.cloud_service_ids[:1]` (M2M order by id ASC)；user 可在 task form 改（domain `[('id', 'in', project_id.cloud_service_ids.ids)]`）；`project.cloud_service_ids` 變動時若既有 primary 仍 ∈ new set 則保留，否則 reset to first
- **`extra_cloud_service_ids`** — domain 拆掉 `workspace_id =` 限制（project 跨 workspace 後 task 跟著跨），改 `domain="[('id', 'in', parent.project_id.cloud_service_ids.ids)]"` 限制 task 只能選 project 已綁的 cloud_service

> **§7.0.3 ambiguity 補拍 2026-05-22**（tester re-engage Q2）：採 hybrid (c) auto-default + user override，對齊同 epic §4.7 `agent_session.primary_workspace_id` pattern。Tester T.5 test 對應加 user override case。Q1 cross-ws M:N walk OR semantics（tester R.7 不變）見 §1.3.2 implicit。

#### §7.0.4 `cloud_service._do_cleanup` 從 1:N 延伸到 M:N

refactor branch 已落地 1:N 版本（cloud_service unlink → 該 cloud_service 對應的 user-scope sidecar-backed MCP 全清）：

```python
# refactor branch 當前（1:N）
user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
    lambda s: s.scope == 'user' and s.sidecar_backed
)
```

**M:N 下的語意問題**：

- `self.project_ids` 現在是 M:N reverse — 同一 project 可能還綁其他 cloud_service（其他 cloud_service 還活著）
- 不該整個 project 連坐 unlink 所有 user-scope MCP — 只該清「URL 對應該 cloud_service sidecar」的 user-scope MCP
- `mcp_server.url` 是 snapshot（refactor branch invariant），URL 內含 `helm_release_name + helm_namespace`
- 解法：filter 加 URL 對 match：

```python
# 本 epic 改寫（M:N）
this_sidecar_url_prefix = f'http://{self.helm_release_name}-mcp.{self.helm_namespace}.svc.cluster.local'
user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
    lambda s: (
        s.scope == 'user'
        and s.sidecar_backed
        and (s.url or '').startswith(this_sidecar_url_prefix)
    )
)
```

- 同時 update [[cloud-service-project-decouple]] D1 invariant + [[mcp-project-decouple]] Inv #4 + [[mcp-url-redesign]] Inv #9 對 M:N 場景的字面描述（**這 3 個 decision doc 都要 update**）
- regression test：既有 test `test_mcp_sidecar_backed_lifecycle.py` 加 case「project 綁 cloud_service A + B，A unlink → 只清 A 的 sidecar-backed MCP，B 的保留」

#### §7.0.5 ACL dual-leg domain walk path 改 M2M

`security/ir_rules.xml` 內 `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` 兩條 dual-leg 的 leg 1 bound walk 必須改 M2M：

```xml
<!-- 改前（M2O walk，refactor branch 當前）-->
<field name="domain_force">[
    '&',
    ('scope', '=', 'user'),
    '|',
    ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
    '&',
    ('project_id.cloud_service_id', '=', False),
    ('project_id.message_partner_ids', 'in', [user.partner_id.id]),
]</field>

<!-- 改後（M2M walk，本 epic 目標）-->
<field name="domain_force">[
    '&',
    ('scope', '=', 'user'),
    '|',
    ('project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id),
    '&',
    ('project_id.cloud_service_ids', '=', False),
    ('project_id.message_partner_ids', 'in', [user.partner_id.id]),
]</field>
```

**Inv #11 守線確認**：

- 外層 AND `scope='user'` ✓ 不動
- 雙腿結構 ✓ 不變（仍是 leg 1 bound walk OR leg 2 unbound carve-out）
- walk path cardinality 從 M2O 改 M2M — Inv #11 明文「filter 條件擴維度可預期變動，不破壞 pattern 本身」allowed
- mcp_server ↔ mcp_tool 兩條 rule 必須**對等改**（sub-model 對等原則）

#### §7.0.6 與 `woow-paas-cd` Fleet bundle 對接

- 新 `woow-paas-agent-runtime` image build 經 `woow-paas-charts/charts/agent-runtime/` chart 釋出
- `woow-paas-cd` Fleet bundle 需新增 `agent-runtime` 部署 spec — bundle 設定誰來建（DevOps 或 phase-implementation 階段）
- 不在本 epic phase-planner scope

#### §7.0.7 Additive schema changes（彙整本 epic 全部 additive）

獨立於 §7.0.2 project M2O→M2M（break change），下列是 additive — 既有 record 不影響：

| 模型 | 動作 | §引用 |
|------|------|------|
| `project.project` | 加 `external_mcp_allowed = Boolean(default=False)` | §6.3 |
| `project.project` | 加 `project_access_ids = One2many('project_access', 'project_id')` 反向 | §4.3 |
| `agent_template` | 新 model（M:N to project / skill / mcp_server） | §4.1 |
| `agent_session` | 新 model（per-project lifecycle + `project_namespace_hash` + `primary_workspace_id` + `quota_override_*`） | §2.12 + §4.7 + §4.8 |
| `skill` | 新 model（per-project + `bundle_attachment_id` + `bundle_sha256` + state） | §3.2 |
| `project_access` | 新 model（partner_id + role + project_id） | §4.3 |
| `cloud_service` | 加 `project_ids = Many2many(... project_cloud_service_rel ...)` reverse（同 §7.0.2 step 6 的 relation table） | §1.3.1 + §7.0.2 |

**Migration 落地動作（post-migrate.py 補步驟）**：

- **Step 7**：CREATE TABLE for new models（Odoo `--init` 自動，無需手寫 SQL）
- **Step 8**：`UPDATE project_project SET external_mcp_allowed = FALSE WHERE external_mcp_allowed IS NULL`（idempotent default backfill）
- **Step 9**：既有 project bootstrap `project_access`（取既有 cloud_service.workspace_id 的 owner partner，建 owner role row；若 project 已 unbound 從任一綁定 cloud_service workspace 取）
- **Step 10**：既有 default ai.assistant 若 referenced by `default_ai_assistant_id` config_parameter → 維持 fallback，**不**自動轉成 agent_template（§7.5）

### §7.1 Migration phase 切片（Q1 拍板用）

從現狀（Odoo addon 內 LangGraph + ai_client.py 同步路徑）到目標（agent-runtime pod 內 LangGraph + WS transport），**phase-planner 推薦 7 個 phase**（每 phase 獨立可 ship + rollback）：

```
Phase A (schema + models) ─→ Phase B (image + chart, no deploy) ─→ Phase C (operator + odoo client)
                                                                            │
Phase G (Fleet + prod) ─← Phase F (dependency cleanup) ─← Phase E (gradual rollout) ─← Phase D (pilot + dual-write start)
```

#### Phase A — Schema migration + new models

**Goal**：把 project schema 從 M2O 升到 M:N + 新增 agent_template / agent_session / skill / project_access models，但**還沒**拆 LangGraph runtime 出去。

**Scope（ship 什麼）**：

- §7.0.2 6-step project schema migration（M2O `cloud_service_id` → M:N `cloud_service_ids`）
- §7.0.3 task model 連鎖修改（primary_cloud_service_id 從 related 改獨立 M2O + extra domain 拆 same-workspace 限制）
- §7.0.4 `cloud_service._do_cleanup` 1:N → M:N filter 改寫
- §7.0.5 ACL dual-leg walk path M2O → M2M
- §7.0.7 全部 additive schema（external_mcp_allowed + agent_template / agent_session / skill / project_access models + project_access bootstrap）
- 既有 ai_client.py + discuss_channel.message_post 同步分支 **不動**（仍走舊路徑）

**不 ship**：

- Agent runtime pod
- WS transport
- paas-operator agent-runtime endpoints

**Acceptance criteria（簡列；對照 §7.7 hand-off 詳列）**：

- 既有 ai_client.py 路徑 100% backward compat（regression test 全綠）
- project 可手動加多 cloud_service via M:N
- project_access 對既有 project 自動 bootstrap owner role
- mcp_server / mcp_tool ACL dual-leg domain M2M walk 通過 refactor branch existing test 9-12 case

**Rollback**：

- git revert + post-migrate.py rollback SQL（DROP project_cloud_service_rel table + 重建 UNIQUE constraint + drop new model tables）
- 既有 ai_client.py 同步路徑無動，roll back 即還原

#### Phase B — Image source + helm chart in repo（不 deploy）

**Goal**：建 `woow-paas-agent-runtime` repo + helm chart in `woow-paas-charts/charts/agent-runtime/`，CI 跑 lint/test/build/push image to JCR，**不 deploy 到任何 cluster**。

**Scope**：

- 新建 `woow-paas-agent-runtime` repo（structure: `runtime/{transport,langgraph,security,skills,state}/`，Dockerfile，requirements.txt，tests/）
- LangGraph runtime / WS server / subprocess runner / SSRF check / jti cache / audit logger — code lift from既有 ai_client.py + 新加 transport / security layer
- 新建 `woow-paas-charts/charts/agent-runtime/`（templates: namespace / deployment / service / configmap-mcp / network-policy / serviceaccount / role / rolebinding / secret）
- CI on tag push: build image → push JCR with tag → §7.2 cross-repo dispatch

**不 ship**：

- 不部署到任何 cluster
- Odoo 不 reference image / chart（純倉庫存在）

**Acceptance criteria**：

- image build CI 通過
- helm chart lint + `helm template` 渲染通過
- 本地測試 image 啟動 + WS server respond `/v1/chat` 連線（mock token）

**Rollback**：

- tag delete + image registry delete tag（無 deploy 引用）

#### Phase C — paas-operator agent-runtime router + Odoo client side stub

**Goal**：開通 paas-operator agent-runtime endpoints + Odoo 端 `agent_runtime_client.py` service + webhook receiver controller。**還沒** flip SSE generator 走新 path（Odoo 內仍走 ai_client.py 同步）。

**Scope**：

- paas-operator: 新建 `extra/paas-operator/src/api/agent_runtimes.py`（§2.11 endpoints + §5.3 jwt signer + §5.5 webhook dispatcher + §6.3 NetworkPolicy update path）
- paas-operator: 加 `agent-runtime-auth-secret` K8s Secret mount + dual-secret rotation 支援
- Odoo: 新建 `src/services/agent_runtime_client.py` (HTTP/WS client wrapper)
- Odoo: 新建 `src/controllers/agent_runtime_webhook.py` (state-update receiver with HMAC verify)
- Odoo: 加 `requirements.txt` `websockets` + `PyJWT`
- 既有 ai_client.py + SSE generator **不改**

**不 ship**：

- frontend 任何變動
- SSE generator 切換邏輯

**Acceptance criteria**：

- paas-operator unit test `POST /api/agent-runtimes/spawn-or-attach` 在 mock K8s 下成功 sign JWT 並返回 pod_ws_url（即使 pod 不存在）
- Odoo `agent_runtime_client` 可從 dev shell 呼到 paas-operator 並收 token
- Webhook receiver 驗 HMAC + 寫 audit log

**Rollback**：

- paas-operator endpoint 改 410 Gone（無 caller）
- Odoo 端 service code 留 dormant

#### Phase D — Pilot + dual-write start（per-project flag）

**Goal**：第一個 pilot project（admin-controlled flag）切換到 agent-runtime pod；非 pilot project 仍走舊 ai_client.py 同步。

**Scope**：

- `project.project` 加 `use_agent_runtime = Boolean(default=False)` field（**§7.1 Q1 拍板**：是否加此 toggle field；推薦加）
- `controllers/ai_assistant.py` SSE generator 內加 if-else：
  ```python
  if project.use_agent_runtime:
      # 走 §5.6 新版（spawn-or-attach + WS）
  else:
      # 走既有 ai_client.py 同步分支（保留）
  ```
- 第一個 pilot project（admin 在 Odoo UI 顯式開 flag）→ 走 pod
- monitor: cold start latency / error rate / WS connection health / worker pool util
- pilot 至少 2 週觀察期 + 至少 3 個 active conversation

**不 ship**：

- 非 pilot project 自動切換
- Odoo dependency 清理

**Acceptance criteria**：

- pilot project: chat invoke 成功 → 收 SSE chunks → 與 ai.assistant 既有 path 行為等效（功能對等）
- 非 pilot project: 行為零變動（regression test 綠）
- 監控 dashboard: pilot 對應 namespace 出現 active pod + last-active annotation 流動

**Rollback**：

- pilot project `use_agent_runtime = False`（一次 click）→ 立即 fallback 到 ai_client.py 同步
- agent pod 自然 reap（30 min idle）

#### Phase E — Gradual rollout

**Goal**：admin 漸進 enable `use_agent_runtime` for additional projects；最終 100% project on agent-runtime。

**Scope**：

- Admin Dashboard widget：批次 enable / disable `use_agent_runtime` per project
- 監控 metric: rollout 進度 + error rate per project
- Phase E 期間預期持續 4-8 週（依 production project 數量）

**不 ship**：

- 自動 enable（admin 仍須顯式批准）

**Acceptance criteria**：

- ≥ 80% project on agent-runtime + observation period ≥ 2 weeks 無 regression
- 100% project on agent-runtime 達 milestone

**Rollback**：

- per-project disable flag（同 Phase D 機制）
- 若大量 project 同時遇問題 → admin 觸發 cluster-wide global toggle（新 config_parameter `force_disable_agent_runtime`）

#### Phase F — Old path cleanup（destructive）

**Goal**：在 Phase E 全部 100% project on agent-runtime + 觀察期至少 4 週後，刪除舊路徑 code。

**Scope（破壞性）**：

- 刪 `discuss_channel.message_post` override 內 LangChain 同步分支（§1.1 Q6 sign-off 蘊含）
- 刪 `ai_client.py` 內 LangGraph 相關 method（`_build_agent_graph` / `_async_agent_invoke` / `_async_agent_stream`）
- 改 `ai_client.py` 為純 metadata serializer（保留 `from_assistant` / `_build_mcp_server_config` 等 ORM-bound helper；移除 streaming/tool-call 邏輯）
- 從 Odoo `__manifest__.py` 移除 `external_dependencies`：`langchain_core` / `langgraph` / `langchain_openai`
- 保留 `langchain_mcp_adapters`（`mcp_server.action_sync_tools` cron 仍需要 — 留 v2 處理）
- 移除 `project.use_agent_runtime` flag（不再有 dual-mode）
- 移除 fallback `default_ai_assistant_id` config_parameter 邏輯（強制 agent_template 必設）— **§7.1 Q1 + Q3 拍板**

**不 ship**：

- 改前端
- 動 mcp_server schema

**Acceptance criteria**：

- Odoo container image 體積測量縮小 ≥ 30%
- 既有 chat 場景無 regression（all green)
- Audit log 確認無人 hit old path（log 含 `path: legacy_sync` 標記，0 count after Phase F）

**Rollback**（破壞性 — 必須 Phase D-E 觀察期長且穩定）：

- git revert Phase F commit
- 重新加 `external_dependencies` + pip install
- `use_agent_runtime` flag 重新加回 + default True 保 prod project state

#### Phase G — Fleet bundle + production deploy

**Goal**：把 agent-runtime helm chart + paas-operator 新版 透過 `woow-paas-cd` Fleet bundle deploy 到 production cluster。

**Scope**：

- `woow-paas-cd` Fleet bundle 新增 `agent-runtime` 部署 spec（reference `woow-paas-charts/charts/agent-runtime`）
- paas-operator 新版 deployment via 既有 cd path
- dev → stg → prod 階段 rollout（仿既有 deployment pattern）
- 建立 Prometheus alert rules：cold start latency p95 / WS error rate / worker pool util / quota usage
- 建立 Grafana dashboard：per-namespace agent pod state / per-project active conv

**不 ship**：

- v2 feature（marketplace skill / async controller / Redis jti cache）

**Acceptance criteria**：

- production cluster agent-runtime 部署成功
- monitoring stack 收到 metrics + alert 觸發測試通過
- 第一個 prod project 在 production agent-runtime 上 chat invoke 成功

**Rollback**：

- Fleet bundle revert（K8s GitOps 反向 sync）
- 既有 production cloud_service 不受影響（不在同 namespace）

### §7.2 Cross-repo CI trigger（Q2 拍板用）

**phase-planner 推薦：(b) Cross-repo Gitea API dispatch（仿既有 `paas-odoo-ci` pattern）**

三選項：

| 方案 | 機制 | 觸發延遲 | 維運 | 推薦 |
|------|------|---------|------|------|
| (a) 統一版本號 + 手動 PR | image push 後 maintainer 手動 PR 到 charts bump tag | 高（人為） | 低（無自動化）| ✗ 不夠紀律 |
| **(b)（推薦）** Cross-repo dispatch | image push tag → Gitea API dispatch to charts repo workflow → auto commit bump | < 5min | 中（兩 workflow 維護） | ✓ 對齊 `paas-odoo-ci` pattern |
| (c) 浮動 tag (`latest` / `main`) | chart 引用 `latest` tag；image push 自動拉新 | 即時 | 低 | ✗ rollback / 版本追蹤失準 |

**選 (b) 的具體流程**（仿 `woow_paas_platform → paas-odoo-ci` pattern）：

```
woow-paas-agent-runtime push tag v1.0.0
   ↓ .gitea/workflows/build-and-trigger.yml
   1. docker build & push jcr-prod.woowtech.io/woow-paas-docker-local/agent-runtime:1.0.0
   2. Gitea API: POST /repos/woow/woow-paas-charts/dispatches
       body: { event_type: bump-agent-runtime, image_tag: 1.0.0 }

woow-paas-charts 收 dispatch event
   ↓ .gitea/workflows/bump-agent-runtime-image.yml
   1. checkout main branch
   2. sed -i "s/^  tag: .*/  tag: 1.0.0/" charts/agent-runtime/values.yaml
   3. git commit -m "chore(agent-runtime): bump image tag to 1.0.0"
   4. git push origin main
   5. (optional) Gitea API: POST /repos/woow/woow-paas-cd/dispatches
       event_type: sync-fleet, chart: agent-runtime
```

**Secret 需求**：

- `woow-paas-agent-runtime` Gitea secret `CI_GITEA_TOKEN`：權限 `woow-paas-charts:Read+Write` + `woow-paas-cd:Write`
- `woow-paas-charts` Gitea secret `CI_GITEA_TOKEN`：權限 `woow-paas-cd:Write`（鏈式触發 fleet sync）

**Versioning convention**：

- Image tag = semver `1.0.0`（不用日期時間戳，與 paas-odoo image tag pattern 區分）
- Chart version = semver `1.0.0` 與 image tag 同步（chart deps 用 helm dependency chart）
- Git tag on agent-runtime repo = `v1.0.0`
- Git tag on charts repo（charts 升版時打 tag）= `agent-runtime-v1.0.0`

### §7.3 Dual-write window（Q3 拍板用）

**phase-planner 推薦：dual-write 期 = Phase D 開始到 Phase F 結束，總計 6-12 週**

**Phase D-E 期間（dual-write active）**：

- Odoo 內 ai_client.py 同步路徑 + agent-runtime pod 路徑 **並存**
- 透過 `project.use_agent_runtime` flag per-project 切換
- 既有 default ai.assistant 路徑透過 `default_ai_assistant_id` config_parameter fallback

**Phase F 開始（dual-write 結束）**：

- 100% project on agent-runtime ≥ 4 週
- 監控確認 0 行為差異 + error rate stable
- 拍板「可以刪舊 path」

**為什麼 6-12 週**：

- 4 週 pilot（Phase D）
- 2-4 週 gradual rollout（Phase E）
- 4 週 final observation 後才 Phase F destructive cleanup
- 總計 dual-write 期 ≥ 10 週 ≤ 16 週

**Cron 衝突防護**：

- Dual-write 期間，agent_session lifecycle cron + ai.assistant cron 並存
- Phase F 後 ai.assistant cron 不再被 trigger（因 use_agent_runtime 100% True；flag 刪除前後 cron 自然 noop）

**Audit metric**：

- 自 Phase D 起，SSE generator 內 log `path: agent_runtime` or `path: legacy_sync`（含 project_id）
- Phase F 觸發條件：`legacy_sync` count = 0 持續 4 週

### §7.4 Odoo dependency 清理（Q4 拍板用 — Phase F 時機）

**phase-planner 推薦：Phase F destructive cleanup（與 §7.1 Phase F + §7.3 結束同步）**

**Phase F 移除清單**：

| Package | 從 Odoo 移除 | 保留位置 | 理由 |
|---------|-------------|---------|------|
| `langchain_core` | ✓ 從 `__manifest__.py external_dependencies` 移除 | agent-runtime image | LangGraph runtime 100% 搬到 pod |
| `langgraph` | ✓ 同上 | agent-runtime image | 同上 |
| `langchain_openai` | ✓ 同上 | agent-runtime image | 同上 |
| `langchain_mcp_adapters` | ✗ **保留**（短期） | Odoo + agent-runtime image 並存 | `mcp_server.action_sync_tools` cron 仍跑 |

**`langchain_mcp_adapters` v2 path**：

- v2 目標：tool discovery 也搬到 agent-runtime image
- 拔法：新增 paas-operator endpoint `/api/agent-runtimes/discover-mcp-tools?url=...`，Odoo cron 呼此 endpoint 而非自跑
- 完整 Odoo langchain 拔乾淨 → Odoo container image 體積再縮小 ~150 MB

**Phase F 觸發條件**：

- §7.3 dual-write 結束（100% project on agent-runtime ≥ 4 週）
- Phase F PR 包：
  - 刪 `discuss_channel.py:55-118` 同步分支
  - 刪 `ai_client.py` LangGraph 相關 method
  - 改 `ai_client.py` 為純 metadata serializer
  - 從 `__manifest__.py` 移除上述 3 package
  - 刪 `use_agent_runtime` flag
  - 刪 `default_ai_assistant_id` config_parameter fallback 邏輯

**Rollback**（destructive change，謹慎）：

- Phase F 是 destructive；rollback 需 git revert Phase F commit
- 但 Phase D-E 觀察 ≥ 6 週 + Phase F 後監控 4 週 = 確認 stable 才繼續
- 若 Phase F 後 4 週內出問題（罕見）→ revert（不算太晚）

### §7.5 ai.assistant ↔ agent_template 並存 migration

**遵循 §4.11 Q5 sign-off (i) 並存策略**：

**Migration 規則**：

- 既有 `default_ai_assistant_id` config_parameter **保留作 fallback**（直到 Phase F 才刪）
- Project 第一次設 `agent_template_id` 時：
  - **不自動** copy ai.assistant.context_id 到 agent_template.system_prompt
  - admin 在 Odoo UI 顯式選擇：
    - (a) 從既有 default ai.assistant context_id 「import」（一鍵 copy）
    - (b) 從零開始撰寫新 system_prompt
- 既有 ai.assistant continue work for non-PaaS Discuss UI（Odoo 原生入口）— v1 不動

**Phase 對應**：

- Phase A：agent_template model 建立，但既有 project 全沒設 `agent_template_id`
- Phase D：pilot project admin 手動建 agent_template + 設 `project.agent_template_id`
- Phase E：admin per-project 建立 + bind agent_template
- Phase F：`default_ai_assistant_id` fallback 刪 → 強制每個 project 必設 agent_template_id（migration script 對未設者 bootstrap default agent_template）

**Phase F 強制 migration**：

```python
# Step 11 of post-migrate.py (Phase F version, '18.0.1.0.30' or 類似)
# 對所有未設 agent_template_id 的 project，建一個 system default agent_template
default_assistant_id = env['ir.config_parameter'].sudo().get_param('woow_paas_platform.default_ai_assistant_id')
if default_assistant_id:
    default_assistant = env['ai.assistant'].sudo().browse(int(default_assistant_id))
    if default_assistant.exists():
        system_template = env['woow_paas_platform.agent_template'].sudo().create({
            'name': 'system-default',
            'title': default_assistant.name,
            'system_prompt': default_assistant.context_id.context if default_assistant.context_id else '',
            'model_config_id': default_assistant.config_id.id,
            'is_public': True,
            'state': 'published',
        })
        env.cr.execute(
            "UPDATE project_project SET agent_template_id = %s WHERE agent_template_id IS NULL",
            (system_template.id,),
        )
```

### §7.6 Fleet bundle 對接（Q5 拍板用）

**phase-planner 推薦：Phase G — dev → stg → prod 階段 rollout（仿既有 paas-odoo deploy pattern）**

**Phase G deploy 序**：

```
1. dev cluster (docker-desktop / dev-sandbox):
   - paas-operator new version deploy (內含 agent-runtime router)
   - agent-runtime helm chart 安裝（不主動拉 pod；spawn-or-attach 才起）
   - woow-paas-platform addon 升版 (Phase A schema migration + Phase D pilot toggle)
   - Pilot project flag enable + 測試 cold start / WS / RBAC
   - 至少 1 週觀察

2. stg cluster (woow-k3s stg):
   - 同 dev 序，但 production-like 環境
   - 至少 2 週觀察 + 模擬 user / multi-project scenario

3. prod cluster (woow-k3s prod):
   - 同 stg 序
   - Phase E gradual rollout per-project 開始
```

**Fleet bundle spec 變動**：

`woow-paas-cd/bundles/agent-runtime/fleet.yaml`：

```yaml
namespace: paas-system  # operator 自己
helm:
  releaseName: paas-operator-v2  # 含 agent-runtime router 的新版
  chart: ./charts/paas-operator
  version: 2.0.0
  values:
    image:
      tag: 2.0.0
    agentRuntime:
      enabled: true
      sharedSecretRef: agent-runtime-auth-secret
```

agent-runtime helm chart 本身**不在 Fleet 中部署**（per-project 動態起 pod via paas-operator），所以 Fleet 主要 deploy：

- paas-operator new version
- agent-runtime helm chart 註冊到 helm repository（讓 paas-operator 能 pull）
- Cluster-wide K8s Secret `agent-runtime-auth-secret`（內含 current/previous keys）

**Rollback**：

- Fleet 反向 sync 到 paas-operator v1（無 agent-runtime router）
- 既有 ai_client.py 路徑無影響

### §7.7 Implementation phase hand-off

本 design 完成 + sign-off 後，下游 phase teammate hand-off 清單：

#### Hand-off to phase-tester

**Test scope per phase**：

| Phase | Test 範圍 | Test type | 重點 case |
|-------|-----------|----------|---------|
| A | project schema migration + new models | unit + migration | M:N backfill correctness / project_access bootstrap / additive field default |
| B | image build + helm chart lint | CI smoke | image starts; WS server responds |
| C | paas-operator endpoint + Odoo client | integration | spawn-or-attach mock K8s; webhook HMAC verify |
| D | pilot dual-write | E2E | use_agent_runtime=True → WS path; =False → legacy path |
| E | gradual rollout | E2E + load | 100 active conv + worker pool util; multi-project NetworkPolicy egress |
| F | destructive cleanup | regression | post-cleanup invoke 與 Phase E 行為等效 |
| G | Fleet deploy + production | smoke | dev → stg → prod 各層 deploy gate |

**Critical regression**（必鎖）：

- mcp_server / mcp_tool 6-rule ACL (refactor branch existing 9-12 case)
- D4 invariant (scope='user' iff project_id non-null)
- cleanup ordering Inv #9 (sidecar-backed filter)
- 既有 ai.assistant Discuss UI path (non-PaaS chat) 不 break

**新加 regression**：

- WS envelope sequence_number monotonic
- jti replay reject
- dual-secret rotation 4-phase transition
- NetworkPolicy egress hybrid (sidecar-backed cross-ns + external 0.0.0.0/0)
- subprocess rlimit enforce
- per-project quota override
- project_access role × agent invoke permission matrix（§4.4 表）

#### Hand-off to phase-test-explainer

- 拿 phase-tester 產出 → 寫 coverage report `docs/tracking/test-reports/test-coverage-agent-runtime-{date}.md`
- 對齊 [[mcp-project-decouple]] / [[mcp-url-redesign]] cell-map pattern

#### Hand-off to phase-implementation

- 依本 design + `agent-runtime-pod-plan.md` 寫 code
- **Branch 策略**：採 §7.0.1 SIGNED-OFF 策略 A — rebase `feature/agent-pod` 到 develop（已 merge refactor）
- **Phase 順序**：A → B → C → D → E → F → G（不可跳）
- **跨 repo coordination**：phase B 在 `woow-paas-agent-runtime` 新 repo；phase B 同時也建 `woow-paas-charts/charts/agent-runtime/`
- **Decision doc updates**（reporter phase）：phase A 更新 `cloud-service-project-decouple.md` D1 invariant 字面（M:N 場景）+ `mcp-project-decouple.md` Inv #4 + `mcp-url-redesign.md` Inv #9

#### Hand-off to phase-caqa

- 14 invariants verify（沿用 PR #47 / PR #54 CAQA template）
- 新加 invariant verify：
  - per-project namespace hash collision 不發生
  - WS envelope version frozen
  - jti cache size 不爆
  - Audit log structured JSON parse-able
- 對齊 [[two-layer-acl-lockstep]] pattern 檢查兩層 ACL（ir.rule + Odoo controller `_check_project_access`）lock-step

#### Hand-off to phase-reviewer

- multi-agent review（5-agent pattern，已驗證兩次有效）
- 同源 code path 檢查必走 grep
- 重點 hot zone：
  - Schema migration 同源 code path（grep `cloud_service_id` walk path）
  - ACL dual-leg domain（grep `workspace_user` rule）
  - subprocess sandbox（grep `subprocess.run`）
  - NetworkPolicy template render（grep `namespaceSelector`）

#### Hand-off to phase-reporter

- Finalize decision docs：
  - 更新 `docs/decisions/refactor/cloud-service-project-decouple.md` Invariant #2 字面（cloud_service ↔ project 從 1:N → M:N）
  - 更新 `docs/decisions/refactor/mcp-project-decouple.md` Inv #4 字面（cleanup filter M:N URL prefix match）
  - 新增 `docs/decisions/refactor/agent-runtime-pod-architecture.md` — Phase G ship 時建
  - 抽 pattern：`per-project-namespace-hash-scheme` / `dual-secret-rotation-4-phase` / `bundle-declare-host-enforce`
- follow-ups 列入 `docs/tracking/followups/follow-ups.md`：langchain-mcp-adapters 拔（v2）/ Redis jti cache（v2）/ DNS rebinding（#52）/ gVisor marketplace（v2）

### §7.8 Affected / new / deleted files 彙整

**新建（全 epic）**：

新 repo `woow-paas-agent-runtime`：
- `runtime/transport/{ws_server,auth,envelope}.py`
- `runtime/langgraph_runtime.py`
- `runtime/skills/{loader,cache,subprocess_runner}.py`
- `runtime/security/{subprocess_runner,url_validator,jti_cache,audit_logger}.py`
- `runtime/state/{mcp_reloader,template_reloader,skill_reloader}.py`
- `Dockerfile` + `requirements.txt` + `tests/`

新 `woow-paas-charts/charts/agent-runtime/`：
- `Chart.yaml` + `values.yaml`
- `templates/{namespace,deployment,service,configmap-mcp,network-policy,serviceaccount,role,rolebinding,secret}.yaml`

Odoo addon 新檔：
- `src/models/{agent_template,agent_session,skill,project_access}.py`
- `src/views/{agent_template,agent_session,skill,project_access}_views.xml`
- `src/services/agent_runtime_client.py`
- `src/controllers/{agent_runtime_webhook,skills}.py`
- `src/migrations/18.0.1.0.26/post-migrate.py`（Phase A schema migration）
- `src/migrations/18.0.1.0.30/post-migrate.py`（Phase F migration — system default agent_template bootstrap）
- `src/static/src/paas/pages/project-detail/sections/{AgentTemplateSection,ProjectAccessSection,SkillsSection,ExternalMcpAllowedSection}.{js,xml}`
- `src/static/src/paas/services/{agent_template,project_access,skill}_service.js`
- `src/data/{agent_session_cron,skill_cleanup_cron}.xml`
- `src/security/ir_rules.xml`（agent_template + project_access + skill 各 4 rule）

paas-operator 新檔：
- `extra/paas-operator/src/api/agent_runtimes.py`
- `extra/paas-operator/src/services/{jwt_signer,webhook_dispatcher,mcp_invalidator}.py`

**修改（全 epic）**：

Odoo addon 修改：
- `src/models/project_project.py`（加 `cloud_service_ids` M2M + `workspace_ids` + `external_mcp_allowed` + `project_access_ids` reverse + `agent_template_id` M2O + `use_agent_runtime` boolean (Phase D-F)）
- `src/models/project_task.py`（primary_cloud_service_id 改獨立 + extra domain 拆 same-workspace）
- `src/models/cloud_service.py`（_do_cleanup filter URL prefix match + project_ids 改 M2M reverse）
- `src/models/discuss_channel.py`（Phase D-E 加 use_agent_runtime fallback；Phase F 刪同步分支）
- `src/models/ai_client.py`（Phase D 加 dual-mode；Phase F 刪 LangGraph methods）
- `src/controllers/ai_assistant.py`（SSE generator rewrite per §5.6 + Phase D dual-mode）
- `src/security/ir_rules.xml`（mcp_server / mcp_tool dual-leg walk path M2M）
- `src/security/ir.model.access.csv`（加 new models access rows）
- `src/__manifest__.py`（bump version + Phase F 移除 langchain external_dependencies）

paas-operator 修改：
- `extra/paas-operator/src/main.py`（include agent_runtimes router）
- `extra/paas-operator/src/models/schemas.py`（agent-runtime request/response schemas）

**刪除（Phase F）**：

- `src/models/ai_client.py` 內 `_build_agent_graph` / `_async_agent_invoke` / `_async_agent_stream` 三 method（保留檔案，只刪 method body）
- `src/models/discuss_channel.py` `message_post` override 內 `_post_ai_reply` 同步調用分支
- `src/models/project_project.py` 內 `use_agent_runtime` field（Phase F dual-mode 移除）

### §7.9 Risks & rollback strategy（per-phase）

| Phase | 主 Risk | Mitigation | Rollback 策略 |
|-------|---------|-----------|--------------|
| A | M:N migration backfill 漏；既有 user-scope MCP 在 schema 改後失效 | post-migrate.py defensive + dry-run on stg first；既有 refactor branch test fixture 覆蓋 | post-migrate rollback SQL（DROP relation table + 重建 UNIQUE constraint）；既有 ai_client.py 不動，無 path 中斷 |
| B | image build / chart template error | CI gate；helm template render 必過 | tag delete + image registry tag delete |
| C | paas-operator endpoint bug；JWT 簽 / 驗 mismatch | unit test + integration test mock K8s | endpoint 改 410 Gone；Odoo client 端 dormant |
| D | pilot project chat 失敗 → user 受影響 | pilot 只挑 admin-controlled internal project；2 週 observation；use_agent_runtime flag 立即 disable | `project.use_agent_runtime = False` → 立即 fallback |
| E | 多 project 同時撞 worker pool / quota / network issue | 漸進 rollout；admin dashboard 顯示 metric；warn at 0.8 util | per-project disable；最壞 cluster-wide `force_disable_agent_runtime` config_parameter |
| F | destructive cleanup 後 4 週才發現問題 | Phase E 觀察 ≥ 4 週 + Phase F 後監控 4 週；audit log `legacy_sync` count = 0 持續 4 週才執行 Phase F | git revert（destructive，但 Phase F PR 是單一 PR，revert clean） |
| G | Fleet deploy 順序錯（先升 Odoo addon 但 paas-operator 未升）→ Odoo 呼新 endpoint 不存在 | dev → stg 各層強制順序 gate；Fleet bundle 設 dependency | Fleet 反向 sync |

**跨 phase 共通 risk**：

- agent-runtime image bug 致 cold start fail → user 完全無法 chat：
  - mitigation：Phase D-E 期間 fallback flag；Phase F-G 後 fallback 拔掉前要極穩定
- shared secret 過期未 rotate → 全 cluster spawn-or-attach 失敗：
  - mitigation：§6.6 雙 secret window；admin 30 day 提前 alert
- 跨 repo CI dispatch race（agent-runtime push 但 charts 還在 build 舊版本）→ deploy 不一致：
  - mitigation：charts CI 等 image tag exists 才 commit bump；超時 fail loud

### §7.10 Open questions for leader sign-off（§7 escalation）

> 5 條核心拍板需求。phase-planner 推薦已 inline 在 §7.1 / §7.2 / §7.3 / §7.4 / §7.6 給出。**全部 sign-off 2026-05-22 — 本 epic design phase 收尾**。

1. ~~**[需 leader 拍板] Migration phase 切片粒度（§7.1）**~~ **[SIGNED-OFF 2026-05-22]** 7 phase（A schema → B image → C operator → D pilot → E rollout → F cleanup → G fleet）
2. ~~**[需 leader 拍板] Cross-repo CI trigger（§7.2）**~~ **[SIGNED-OFF 2026-05-22]** (b) Gitea API dispatch + semver image tag（仿 paas-odoo-ci pattern）
3. ~~**[需 leader 拍板] Dual-write window 長度（§7.3）**~~ **[SIGNED-OFF 2026-05-22]** 6-12 週（Phase D 4w + E 2-4w + F 前 4w final observation；觸發條件 `legacy_sync count = 0 持續 4 週`）
4. ~~**[需 leader 拍板] `ai_client.py` 刪除時機（§7.4）**~~ **[SIGNED-OFF 2026-05-22]** Phase F destructive cleanup；`langchain_mcp_adapters` 暫保留（v2 拔）
5. ~~**[需 leader 拍板] Fleet bundle 對接時機（§7.6）**~~ **[SIGNED-OFF 2026-05-22]** Phase G dev → stg → prod 階段（每階段 ≥ 1-2 週觀察）

**§7 額外探出來但不阻塞 §7 sign-off**（implementation 階段處理）：

- Phase A 內具體 migration version number（`18.0.1.0.26` 是假設）— implementation 階段對齊 git history
- Phase D pilot project 選擇（admin 指定 / 自選 / 全 default False 漸進 opt-in）— admin 拍板
- Phase E rollout 排程（每週放 N 個 project）— admin 拍板
- Phase F PR 結構（單一大 PR 還是拆 langchain dep / sync branch / flag 三個 PR）— implementation 階段判斷
- `agent.woow.tw/` annotation domain 是否註冊（K8s admission webhook 不檢查 unknown annotation domain，免註冊）— 不阻塞
- Fleet bundle 內 paas-operator 版本與 woow-paas-platform 版本 dependency 順序（Fleet 預設 alphabetical）— deployment 階段

---

## Open questions for leader sign-off（§1 彙整）

> phase-planner 不替使用者拍板，以下全部回 leader 轉達。括號內為 phase-planner 建議。

### 架構粒度與隔離

1. **Pod scope 粒度**（Q3 + Q7）：~~A per-conversation / **B per-user**（推薦）/ C per-(user, agent_template)？~~ ~~[SIGNED-OFF 2026-05-21] B per-user~~ **[SUPERSEDED 2026-05-21 by per-project]** — leader 二次 pivot 改 per-project（同 project 多 user / 多 chat 共享同 pod，project 升 first-class，1..N cloud_service multi-workspace binding；見 §1.2 第二輪 sign-off 表 + §1.3）
2. **Trust model**（Q4）：~~v1 收斂「skill = user-trusted」可以嗎？（marketplace skill 留 v2）~~ **[SIGNED-OFF 2026-05-21] user-trusted**（marketplace skill 留 v2 評估 gVisor / Kata；per-project 蘊含 same-project member co-trust）
3. **跨 conversation 共享 long-term memory**（Q7）：~~要 feature 嗎？~~ **[SIGNED-OFF 2026-05-21 — per-project]** per-project pod 蘊含同 project 內多 conversation 在同 LangGraph runtime（不同 thread_id 預設隔離 thread checkpoint；shared memory 走 PVC `/workspace/project-{id}/shared/` subdir，§3/§4 細化）

### 資源與生命週期

4. **Reap 策略**（Q2）：「scale to 0（保留 PVC）+ 長 TTL helm uninstall」可以嗎？
5. **StorageClass**（Q2）：選 X（用既有 `longhorn-delete`，長 TTL 後 state lose）or Y（新增 retain SC，state 持久但要手動 GC）？
6. **Namespace 共用**（Q2）：(a) agent pod 共用 cloud_service 的 workspace namespace，or (b) 獨立 `paas-agent-ws{N}` namespace？

### Transport

7. **frontend 不動**（Q1）：~~「保留既有 SSE EventSource，只改 Odoo controller backend」可以嗎？（代價：Odoo worker per active conv）~~ **[SIGNED-OFF 2026-05-21 — per-project]** 保留既有 SSE EventSource；Odoo controller 內換 backend (open WS to per-project pod, forward chunks as SSE)；frontend 零變更
8. **Cold start UX**（Q3）：「同步阻塞 15s timeout fallback」vs「立刻回 spawning event + 真實回應後到」？

### MCP / cleanup ordering

9. **MCP invalidation 方式**（Q5）：軟通知（drop from in-memory list）or 硬重啟 pod？
10. **新 paas-operator endpoints**（Q5）：`/agent-runtimes/...` 系列是否 in scope？

### 跨 repo / 命名

11. **新 repo 名稱**（Q6）：~~`agent-runtime` or `woow-paas-agent-runtime`（對齊 init-agent-4 家族）？~~ **[SIGNED-OFF 2026-05-21]** image build repo 用 `woow-paas-agent-runtime`（對齊 init-agent-4「woow-paas-*」家族）
12. **新 repo 形狀**（Q6）：~~image build + helm chart 同 repo（仿 paas-odoo-ci）or 分 repo？~~ **[SIGNED-OFF 2026-05-21]** image build 在 `woow-paas-agent-runtime`；helm chart 寫入既有 `woow-paas-charts`（與 cd / charts 家族對齊；跨 repo 版本同步 trade-off 已 accept，細節 §7 拍板）
13. **刪除 `discuss_channel.message_post` 同步 LangChain 分支**（Q6）：可以執行嗎？（不可逆動作）

### 命名與 doc 對齊（不阻塞）

14. **本 doc 命名**：依 docs/README.md 既有 plan 命名規則應為 `2026-05-21-agent-runtime-pod-design.md`（帶日期前綴），目前依 task brief 用無日期版本，要對齊嗎？

---

## References

### Codebase pointers（review 期間使用）

- `src/controllers/ai_assistant.py` — AI chat / SSE entry
- `src/models/ai_client.py` — LangGraph + MCP integration（將被切割）
- `src/models/ai_assistant.py`、`src/models/ai_config.py` — AI metadata（保留）
- `src/models/mcp_server.py`、`src/models/mcp_tool.py` — MCP registry（保留 schema）
- `src/models/cloud_service.py` — 既有 helm release lifecycle（reference pattern for agent-runtime）
- `src/models/discuss_channel.py` — message_post override（同步分支將被刪）
- `src/static/src/paas/services/ai_service.js` — frontend SSE EventSource
- `extra/paas-operator/src/` — FastAPI + helm wrapper（新增 agent-runtime endpoints）
- `charts/odoo-dev-sandbox/` — helm chart template（reference for agent-runtime chart）

### Decision / pattern pointers

- `docs/decisions/refactor/cloud-service-project-decouple.md` — CloudService / Project invariants
- `docs/plans/2026-Q2/2026-05-18-mcp-project-decouple-plan.md` — MCP D4 invariant 來源（plan doc 當時亦含 D5，已被後續 `docs/decisions/refactor/mcp-url-redesign.md` cycle 4 拔除）
- `docs/decisions/refactor/mcp-url-redesign.md` — D5 拔除 + `sidecar_backed` flag + dual-leg ACL（Inv #9/#10/#11）canonical home
- `docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md`
- `docs/decisions/patterns/odoo-std-field-no-override.md`

### External（將於 §5/§6 細化時用 context7 確認版本）

- LangGraph thread / checkpoint model
- `langchain-mcp-adapters` MultiServerMCPClient
- FastAPI WebSocket + Odoo SSE generator interop
- K8s NetworkPolicy K3s reply-traffic workaround（已 patch 在 dev-sandbox chart）
