# Pitfall: agent-runtime 在 stg 啟用的 5 層 gap chain（skill 找不到 → 端到端打通）

> 來源：stg debug session 2026-05-31。症狀是 `stg.woowtech.io/woow` 的 AI 任務 chat「找不到 skill / 看不到 agent-runtime pod」，使用者已知 #104 merged 卻無效。逐層追下來發現是 **5 個獨立的啟用條件全部沒到位**，且每修好一層才會暴露下一層 —— 任何單層修復都「看起來沒動靜」，因為前一層還擋著。最終 task 4 chat 收到「agent runtime round-trip 已接通」確認端到端打通。
>
> 同 epic 既有 e2e gap chain（pilot / F-PILOT-10）記錄在 [[agent-runtime-pilot-e2e-gap-chain]] / [[agent-runtime-fpilot10-cluster-e2e-gap-chain]]；本篇是 **stg 環境啟用** 視角的 gap chain（環境設定缺漏，非 code bug）。
>
> **第 6 層（2026-06-01）**：下方 5 層讓 *pod round-trip* 通；要讓 operator→Odoo 的 §5.5 **狀態回寫 webhook** 也通（`agent_session.state` 不卡 `absent`），還需另 3 個部署層（operator→Odoo NetworkPolicy ingress、webhook secret 兩端對齊、operator 探索指對顆），且接通過程踩到一次「helm upgrade 衝突 → create.sh 刪 namespace → DB 全失」的 hazard——見 [[agent-runtime-webhook-statesync-deploy-hazards]]。

## Symptom

- `/woow` 任務 chat 發訊息，AI 用 Odoo 內建 LLM 回（legacy path），**完全沒碰** agent-runtime pod / skills.json / 指定的 skill（如 test-echo）。
- 全 cluster `kubectl get pods -A | grep agent-runtime` **零個 pod**；無 `paas-ws-*-pa-*` namespace。
- 「PR 說修完了卻無效」：#104/#106 是 **operator-only 修復**（檔案全在 `extra/paas-operator/`，Odoo addon 一行沒動），但 stg 跑的 operator image 早於 PR merge。
- 修一層、重試、冒出**完全不同**的錯（template → namespace secret → connection refused），每個錯都是「下一層」。

## Why it happens — 5 層 gap，每層獨立 fail-closed

agent-runtime 是 **flag-gated dual-mode rollout**（`ai_assistant.py` §5.6），預設全關；每層設計成缺就 fail-closed。stg 從沒完整啟用過 → 五層全空，逐層暴露：

| # | Gap | 症狀訊號 | 根因 | 修法 |
|---|-----|---------|------|------|
| 1 | **operator image 過舊** | chat 走 legacy（`POST .../v1/chat/completions` 直打 LLM）| stg operator `20260527.1214` < #104/#106 merge（5/31）。修復全在 operator，重 build **Odoo** image 沒用 | 重 build operator（`operator-v*` tag → CI）+ bump CD `paas-app-bundle-cd/paas-operator-stg/values-stg.yaml` tag → Fleet sync |
| 2 | **`project.use_agent_runtime=False`** | 同上，legacy path | per-project toggle 預設 False（`project_project.py:69`）；欄位只在 **Odoo 原生 project 表單**（`project_views.xml`），`/woow` 前台沒有 | Odoo 後台勾選 / DB write |
| 3 | **project 無 agent_template** | 紅字 `No agent template configured` | `_resolve_agent_template`（`ai_assistant.py:755`）：project 沒綁 published template + 沒 `agent_template_id` → return None，**spawn 前**就擋 | template 設 `published` + `project_ids` 綁該 project |
| 4 | **`AGENT_RUNTIME_NAMESPACE_SECRET` 未注入** | 紅字 `namespace secret is not configured ... fail-closed` | chart 有 env（`odoo-deployment.yaml:108`，`optional:true`）但 secret 缺 key → env 空 → `agent_session._get_namespace_secret()` fail-closed。create.sh 從沒產這個 secret 值 | patch secret 加 key（驗證）+ create.sh 從 env 注入（持久） |
| 5 | **NetworkPolicy 沒開 agent pod 路徑** | `[Errno 111] Connect refused 10.43.x:8000` | chart 有 `allowAgentRuntimePods` flag（`network-policy.yaml:48/96`，用 `paas-managed:true` 開雙向）但 stg 沒啟用；Odoo egress 的 `ipBlock 0.0.0.0/0 except 10.0.0.0/8` 把 pod CIDR 排除 | patch NP（驗證）+ create.sh `--set networkPolicy.allowAgentRuntimePods=true`（持久） |

> 註（2026-06-01）：gap #3「project 無 agent_template」對**新環境**已緩解——module 自 fresh install 起 seed 一筆 tenant-neutral 平台預設（`agent_template_default`，public+published+unbound）並由 `post_init` 自動指向 `default_agent_template_id`，resolver 第 4 條 fallback 直接生效 → 不再需要逐 project 手動綁 template（admin 只需補 `ai_config_woowbot` 的 api_key）。見 [`docs/superpowers/specs/2026-06-01-seed-agent-template-design.md`](../../superpowers/specs/2026-06-01-seed-agent-template-design.md)。上方 5 層 gap chain 為歷史 debug 紀錄，保留不動。

### 為什麼難 debug：refused ≠ timeout 的關鍵分辨

Gap 5 一度誤導：NetworkPolicy 擋通常是 **timeout**（封包丟棄），但這裡是 **Connection refused**。差點排除 NP 嫌疑。實證才釐清：

- agent pod **自連** `127.0.0.1` / `0.0.0.0` / **自己 pod IP** `:8000` 全 OK → app 與 bind 正常，pod `1/1 Ready`（readinessProbe tcpSocket:8000 過）。
- 但 **任何其他 pod**（Odoo、operator）連 pod IP 或 service IP 都 refused。
- 結論：壞在「跨 namespace pod 之間」這層，唯一管制就是 NetworkPolicy。**K3s 的 NP enforcer 對被擋流量回 RST（refused）而非 drop（timeout）** —— 不能用「refused 所以不是 NP」推翻。

## How to avoid / 重現 checklist

要在新環境（stg / 新 sandbox）跑通 agent-runtime（任一 skill 的 pod round-trip），**5 層全要到位**，建議照順序逐層驗（每層過了才驗下一層）：

1. **operator image 含修復**：`kubectl -n paas-stg-system get deploy paas-operator-stg -o jsonpath='{...image}'` 的 tag ≥ 目標 PR；不是看 Odoo image。operator 修復走 `operator-v*` tag + `paas-app-bundle-cd` CD bump（**不是** deploy-stg，那是 build Odoo）。
2. **flag 開**：目標 project `use_agent_runtime=True`（Odoo 後台 project 表單，非 `/woow`）。
3. **template 解析得到**：該 project 有 `published` 且 `project_ids` 含它的 agent_template（或設 `agent_template_id`）。system-scope skill（如 test-echo）會 union 進每個 pod，**不需綁 template**，但 template 本身仍必須存在否則 spawn 前就擋。
4. **Odoo 端 secret 注入**：`AGENT_RUNTIME_NAMESPACE_SECRET`（namespace hash）+ `AGENT_RUNTIME_AUTH_SECRET_CURRENT`（**必須等於** operator `paas-operator-stg-agent-runtime-auth/current`，pod 用它驗 Odoo 簽的 skill-bundle token，不一致 → 401）。容器內驗：`echo ${#AGENT_RUNTIME_NAMESPACE_SECRET}` 應 >0。
5. **NetworkPolicy 開 agent pod 雙向**：`networkPolicy.allowAgentRuntimePods=true`（用 `paas-managed:true` label 比對動態 `paas-ws-*-pa-*` ns）。驗：從 Odoo pod `python3 -c "socket.create_connection((svc_ip,8000))"` 應 OK。

> 持久化：#4 + #5 已寫進 `create.sh` stg mode（PR #110）。deploy-stg 前須 `export AGENT_RUNTIME_AUTH_SECRET_CURRENT=... AGENT_RUNTIME_NAMESPACE_SECRET=...`，否則 deploy-stg 印警告、agent-runtime 路徑失敗（legacy chat 仍正常）。

## Related

- 持久化 PR：#110 `fix/stg-agent-runtime-enablement`（create.sh NP flag + env-sourced secrets；deploy-stg.sh 缺 env 警告；deploy-stg SKILL.md 前置條件章）。
- operator 修復：#104（skill snapshot delivery）+ #106（mcp snapshot delivery），落 `extra/paas-operator/`，本次重 build 為 `paas-operator:20260531.0744`。
- 同 epic e2e gap chain（互補視角）：
  - [[agent-runtime-pilot-e2e-gap-chain]] —— pilot 階段 e2e gap。
  - [[agent-runtime-fpilot10-cluster-e2e-gap-chain]] —— F-PILOT-10 cluster e2e gap。
  - 三者共同 root：agent-runtime 是多層 fail-closed 系統，**單層修復不可觀測**，必須逐層驗到端到端。
- 同 family（plan/假設 vs 實況，動手才現形）：
  - [[cross-repo-artifact-landing-assumption]] —— chart 落點假設錯；本篇是「環境啟用條件假設已到位、實際全空」。
- 關鍵 code 落點：dual-mode gate `src/controllers/ai_assistant.py:384`（`use_agent_runtime` 分支）/ `:755`（`_resolve_agent_template`）/ `:802`（system skill union 進 snapshot）；`src/models/agent_session.py`（`_get_namespace_secret` fail-closed）；chart `charts/odoo-dev-sandbox/templates/{odoo-deployment,secrets,network-policy}.yaml`。
- 部署機制：operator 走 `paas-app-bundle-cd`（Fleet），**非** deploy-stg；deploy-stg 走 CI build Odoo image + create.sh（見 `CLAUDE.md` §Deploy Staging）。

## 本機 dev-sandbox（docker-desktop）變體 — 2026-06-05 portal E2E 重演

同一條 chain 在**本機 dev sandbox** 重演（portal-task-agent-chat E2E），逐關訊號與 stg 略有不同，對照如下：

| 對應 stg gap | 本機訊號 | 根因（本機版） | 處置 |
|---|---|---|---|
| （前置，stg 沒有）| module 根本沒裝（`ir_module_module` 無 `ai_base_gt` row）| Floor `.env` 相對路徑掛到空 ext-addons 目錄 | 見 [[floor-env-relative-path-empty-ext-addons]]（修 `.env` 絕對路徑 + 重建）|
| #4 namespace secret 未注入 | chat 回 `Pod spawn failed: Agent runtime namespace secret is not configured`（fail-closed 正確發聲）| sandbox secret **沒有任何 agent-runtime key**（chart env `optional: true` 故 pod 照常啟動；F-GC-2：create.sh fresh `--set` 不帶 agentRuntime）| `kubectl patch secret` 補 `agent-runtime-namespace-secret` + restart odoo |
| #1 operator image 過舊 | chat 回 `paas-operator POST /spawn-or-attach → 404: Not Found` | 本機 `paas-system` 的 `paas-operator:latest` 是 90 天前本地 build（早於 agent-runtime epic）| 本機升級需 rebuild image + helm upgrade 補 agent-runtime env/creds（chart repo Gitea token 等）— 屬環境 provisioning |
| #2/#3/#5 | 未走到（被 #1 擋住）| — | — |

**結論／紀律**：本機 dev sandbox 的 E2E **預期只驗到 spawn 邊界**（UI 渲染、ACL 鏈、SSE 開啟、錯誤呈現、composer 恢復）；完整 agent 串流的 E2E 正規路徑是 merge 後 deploy-stg（stg operator 由 Fleet 管理、五層已驗）。在本機追完整串流 = 重新 provision 整套 operator 環境，投資報酬極低，**果斷停在 spawn 邊界並記錄**。
