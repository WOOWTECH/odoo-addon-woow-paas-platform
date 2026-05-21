---
name: plan-agent-runtime-pod-design
status: in-review (§1.2 rework + §2 rework + §1.3 + §7.0 prereq signed-off; §3 draft submitted)
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
| MCP D4/D5 invariants | scope='user' iff project_id non-null；user-scope 必須掛綁 cloud_service 的 project | `src/models/mcp_server.py:111-139` + `docs/plans/2026-Q2/2026-05-18-mcp-project-decouple-plan.md` |
| MCP cleanup ordering | `cloud_service._do_cleanup`：先 unlink user-scope MCP → helm uninstall → state='deleting' | `src/models/cloud_service.py:1119-1201` |
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

- 既有 `mcp_server` model 有 D4 / D5 invariants（不可動）：
  - D4：`scope='user'` iff `project_id IS NOT NULL`
  - D5：user-scope MCP 必須掛在綁 `cloud_service` 的 project
- 既有 cleanup ordering（`cloud_service._do_cleanup`，`docs/plans/2026-Q2/2026-05-18-mcp-project-decouple-plan.md` 已 sign-off）：unlink user-scope MCP → helm uninstall → state='deleting'
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
- **不動 `mcp_server` schema**（保持 D4/D5 invariants，不違反 task brief constraint）
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
MCP Servers (project.cloud_service_ids 內各 cloud_service 的 sidecar，跨 workspace；schema 不動)
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
- agent pod **能訪問的 cloud_service / MCP**：透過該 project 的 `cloud_service_ids` 集合 → 並集 of 各 cloud_service 的 mcp_server（含 sidecar-backed 與 external）
- agent pod **NetworkPolicy egress**：必須允許 egress 到該 project `cloud_service_ids` 的所有 workspace namespace（K8s `namespaceSelector` 用 `workspace_id` label match 該 project workspaces 集合）

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
- tool._run(args) → subprocess.run([runtime, entrypoint], input=json.dumps(args), timeout=timeout_seconds, env=<whitelist>, cwd=`/skills/<name>/<version>/`)
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
2. Pod 在 `/skills/<name>/<version>/` PVC subdir 找 cache → 命中則 verify `bundle.sha256` matches expected → use
3. cache miss：pod 呼 Odoo endpoint `GET /api/agent-runtimes/skill-bundle?project_hash={hash}&skill_name={name}&version={ver}` （新增 controller endpoint）
   - Odoo 端：query `skill` model + project_access check（pod 的請求需帶 short-lived bearer token，§5 protocol）→ return ir.attachment binary
4. Pod 寫到 `/skills/<name>/<version>/bundle.tar.gz` → verify SHA256 → extract → cache marker
5. Subprocess invoke from `/skills/<name>/<version>/`

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

**Mount layout**（per-project pod）：

```
/skills/                                              ← read-only mountpoint
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

- `/skills/` 是 PVC subdir（在 `/workspace/project-{pid}/skills/`）
- 不是獨立 PVC（避免多 mount 複雜度）
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
| **執行位置** | agent pod 內 subprocess（fork from LangGraph process） | 獨立 MCP server pod（cloud_service sidecar，跨 namespace） |
| **通訊** | subprocess.run with stdin/stdout JSON | MCP protocol over HTTP/SSE（langchain-mcp-adapters） |
| **生命週期** | per-invocation 短生命（subprocess 起 → 跑 → 死） | long-lived sidecar service（隨 cloud_service 起伏） |
| **跨對話** | 同 pod 內所有 thread 共用 cache（PVC） | 同 cloud_service 內所有 project 共用 sidecar |
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

> 5 條核心拍板需求，phase-planner 推薦已 inline 在 §3.3 / §3.4 / §3.8 / §3.9 / §3.10 給出。

1. **[需 leader 拍板] Storage backend（§3.3）**：採 phase-planner 推薦 **(a) ir.attachment**（簡單、對齊既有 pattern、25 MB bundle limit）？或 (b) OCI image registry / (c) S3-compatible？
2. **[需 leader 拍板] Versioning 模型（§3.4）**：採 phase-planner 推薦 **immutable bundle + version alias**（每 upload 新 row、`is_latest` boolean、archived 30 day cron 清 binary）？或 mutable 覆蓋？
3. **[需 leader 拍板] Signature / digest（§3.8）**：採 phase-planner 推薦 **v1 SHA256 digest（integrity）only，無 cryptographic signature**？或 v1 就要 GPG / cosign 簽章？
4. **[需 leader 拍板] Sandbox profile（§3.9）**：採 phase-planner 推薦 **v1 bundle SKILL.md declare 純 metadata，執行限制 100% 由 host enforce**？或 declare 可放寬限制（更寬鬆但增加 attack surface）？
5. **[需 leader 拍板] Skill / MCP namespace（§3.10）**：採 phase-planner 推薦 **LangGraph 層統一抽象為 tool；物理 / Odoo model / ACL / namespace 全分離；tool name prefix 區分**？或共用 namespace 抽象（強迫 ACL 合併、生命週期妥協）？

**§3 額外探出來但不阻塞 §3 sign-off 的事項**（不需立刻拍板，§4~§6 各節再處理）：

- 每 project skill 數量上限 / quota（§3.12 risk）— §4 與 per-project agent quota override 一起設計
- Skill bundle 含 `requirements.txt` 是否做 auto pip install — §4 蘊含 multi-skill 依賴衝突問題；v1 不做（runtime pre-baked 通用依賴）— §6 安全章再評估
- archived skill 30 day 後 cron 真刪 binary 的 cron schedule — §7 implementation 階段細化
- agent_template ↔ skill binding cardinality — §4 拍板（與 §4 預告 bullets 一致）
- skill 對 langchain 版本鎖定 — §4 與 agent_template metadata 一起設計
- LangGraph thread checkpoint 內 tool_call args 是否快取 skill 內部 state（影響 reproducibility）— §4 / §5 拍板

## §4 Agent template & binding model (agent ↔ skills ↔ MCPs) — TODO（預告 bullets）

> §4 draft 未啟動，等 §2 + §1.3 sign-off 後展開。下列為 per-project pivot 蘊含的 §4 範圍預告：

**§4 預期涵蓋**：

- **project_access role 對 agent invoke 的權限差別**：
  - admin role 可：配置 agent_template binding、調整 agent quota override、設 per-project secret（API key 等）、reset workspace state、撤銷 user role
  - user role 可：invoke agent（送 chat message）、讀自己 thread 的 history、上傳自家 skill（若 v1 允許）
  - guest role 可：唯讀觀察（看其他 user 與 agent 的對話）— 是否要進 v1 留 open
  - non-member：spawn-or-attach 之前在 Odoo controller 端被擋（既有 `_check_channel_access` + 新加 `_check_project_access` 串接）
- **agent_template ↔ project binding cardinality**（暫 open）：
  - 選項 A：M:N — 一個 project 可用多個 template，一個 template 可被多 project 共享（runtime 看 invoke 時用哪個）
  - 選項 B：1:N (template:project) — template 是 global，project 選一個用
  - 選項 C：1:1 — 每個 project 有自己的 template，不共享（最隔離但難維護）
  - phase-planner 傾向 A，但要 §4 拍板
- **per-project shared memory 模型**：
  - 跨 thread 共享：`/workspace/project-{pid}/shared/`（所有 project member 共讀）
  - thread-private：`/workspace/project-{pid}/threads/{channel_id}/`（LangGraph thread checkpoint）
  - 跨 project 共享：v1 不做（trust 跨 project 物理隔離）
  - admin / user role 對 shared memory 寫權限差別：admin 可清空 / 修改、user 唯讀？或都可寫？— §4 拍板
- **agent_template + skill / mcp_server binding 路徑**：
  - skill：`agent_template.skill_ids` (M2M to `woow_paas_platform.skill`)？skill 是 project-level 還是 template-level？
  - mcp_server：agent runtime 看 `project.mcp_server_ids`（既有路徑）；agent_template 是否再加 own MCP whitelist 限制？
- **per-project agent quota override**：
  - 默認從 `ir.config_parameter` 拿
  - admin role + project plan 可動態調整 override（存 `project.agent_quota_*` field？或新增 `agent_session.quota_override_*` field？— **不動 project schema** 偏好後者）
- **計費歸屬（與 §2.8.1 surface 對齊）**：
  - project.primary_workspace_id 概念：建 project 時鎖 first cloud_service 的 workspace 為 primary
  - 主費用走 primary；secondary workspace 不計費，只享 read access
  - 或：M:N project 跨 workspace 時，計費分攤 — 各 workspace 按比例分擔 — 太複雜，§4 拍板

## §5 Transport (Odoo ↔ Pod, WS/SSE protocol) — TODO

## §6 Sandbox security model (NetworkPolicy / RBAC / seccomp) — TODO

## §7 Migration path (LangGraph 從 Odoo 搬遷的階段策略) — TODO（含 prerequisite section）

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

- `primary_cloud_service_id` 從 `related` 改 **獨立 Many2one**（user 在 multi-binding project 內手動選 primary），或計算 from `project_id.cloud_service_ids[:1]`
- `extra_cloud_service_ids` domain 拆掉 `workspace_id =` 限制（project 跨 workspace 後 task 跟著跨），改 `domain="[('id', 'in', parent.project_id.cloud_service_ids.ids)]"` 限制 task 只能選 project 已綁的 cloud_service

具體選哪個 §7 final draft 拍板（這是 §1.3 提到的 task connect change，**留 §7**）。

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

### §7.1 ~ §7.N — TODO

LangGraph 程式碼搬家順序、Odoo addon 內 `ai_client.py` / `discuss_channel.py` / `controllers/ai_assistant.py` 改動順序、跨 repo CI trigger 機制（§1.2 跨 repo coordination 預告）、`langchain_*` Odoo 依賴清理時機 — 全部留 §7 draft 階段展開。

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
- `docs/plans/2026-Q2/2026-05-18-mcp-project-decouple-plan.md` — MCP D4/D5 invariants 來源
- `docs/decisions/patterns/odoo-addon-std-rule-symmetric-deactivation.md`
- `docs/decisions/patterns/odoo-std-field-no-override.md`

### External（將於 §5/§6 細化時用 context7 確認版本）

- LangGraph thread / checkpoint model
- `langchain-mcp-adapters` MultiServerMCPClient
- FastAPI WebSocket + Odoo SSE generator interop
- K8s NetworkPolicy K3s reply-traffic workaround（已 patch 在 dev-sandbox chart）
