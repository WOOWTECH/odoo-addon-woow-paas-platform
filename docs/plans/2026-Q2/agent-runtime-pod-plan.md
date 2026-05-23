---
name: plan-agent-runtime-pod
status: draft (awaiting design doc sign-off + implementation start)
created: 2026-05-22
updated: 2026-05-22
phase: planning → implementation prep
team: feature-agent
planner: agent-runtime-pod-planner (phase-planner)
related:
  - docs/plans/2026-Q2/agent-runtime-pod-design.md
  - docs/decisions/refactor/cloud-service-project-decouple.md
  - docs/decisions/refactor/mcp-project-decouple.md
  - docs/decisions/refactor/mcp-url-redesign.md
  - docs/decisions/patterns/mcp-acl-6-rule-split.md
  - docs/decisions/patterns/two-layer-acl-lockstep.md
  - docs/decisions/patterns/unbound-project-acl-dual-leg.md
  - docs/decisions/patterns/sidecar-backed-flag-lifecycle.md
---

# Implementation Plan: Agent Runtime Pod

## Context

本 plan 對應 `docs/plans/2026-Q2/agent-runtime-pod-design.md` 的 §7.1 Phase A-G 切片，把 design 級決策展開為 implementation-level task breakdown。

**Design doc 全部 sign-off 狀態**（截至 2026-05-22）：

- §1.2 / §1.3 / §2 / §3 / §4 / §5 / §6 / §7.0 prereq — ✓ leader signed-off
- §7.1 - §7.10 full draft — pending leader sign-off

**本 plan 假設前提**：design doc §7.10 5 條 Open Questions 全採 phase-planner 推薦（7-phase 切片 / cross-repo dispatch / 6-12 週 dual-write / Phase F destructive cleanup / Phase G dev→stg→prod）。leader 拍板異動則本 plan 對應節更新。

## Scope summary

| 維度 | 內容 |
|------|------|
| 主目標 | 把 LangGraph agent runtime 從 Odoo Python process 搬到 per-project sandbox pod |
| 對應 design | §1-§7 完整 design doc |
| Phase 數 | 7（A schema → B image → C operator → D pilot → E rollout → F cleanup → G fleet） |
| Repos 影響 | `woow-paas-platform`（既有）+ `woow-paas-agent-runtime`（新建）+ `woow-paas-charts`（既有新增 chart）+ `woow-paas-ci`（增 dispatch workflow）+ `woow-paas-cd`（增 fleet bundle） |
| 主 deliverable | 每 project 1 agent pod；K8s native；user-trusted skill subprocess sandbox |
| 估時總計 | 9-13 週 implementation + 1-2 週 design sign-off buffer |

## Phase A — Schema migration + new models

### Goal

升級 project schema 從 M2O 到 M:N + 新增 4 個 Odoo model（agent_template / agent_session / skill / project_access）+ 加 additive field（external_mcp_allowed）。**LangGraph runtime 還沒搬 pod**（既有 ai_client.py 路徑全保留）。

### Tasks（具體 implementation-level）

**Schema migration（破壞性 — M:N）**：

1. `src/models/project_project.py`：
   - 加 `cloud_service_ids = fields.Many2many('woow_paas_platform.cloud_service', relation='project_cloud_service_rel', column1='project_id', column2='cloud_service_id')`
   - 加 `workspace_ids = fields.Many2many('woow_paas_platform.workspace', compute='_compute_workspace_ids', store=True)`
   - 標 `cloud_service_id` deprecated（保留 backward-compat read path）
   - override `write()` 同步：write `cloud_service_id = X` → 同時 add X to `cloud_service_ids`
   - override `cloud_service_id` getter：return `cloud_service_ids[:1].id`
   - 加 `external_mcp_allowed = fields.Boolean(default=False)`
   - 加 `agent_template_id = fields.Many2one('woow_paas_platform.agent_template')`
   - 加 `use_agent_runtime = fields.Boolean(default=False)`（Phase D-F 用）
2. `src/models/project_task.py`：
   - `primary_cloud_service_id`：從 `related('project_id.cloud_service_id')` 改 **獨立 Many2one + computed default** from `project_id.cloud_service_ids[:1]`
   - `extra_cloud_service_ids` domain：從 `[('workspace_id', '=', workspace_id)]` 改 `[('id', 'in', parent.project_id.cloud_service_ids.ids)]`
3. `src/models/cloud_service.py`：
   - `project_ids`：從 One2many 改 `Many2many('project.project', relation='project_cloud_service_rel', column1='cloud_service_id', column2='project_id')`
   - `_do_cleanup` filter：加 URL prefix match `s.url.startswith(this_sidecar_url_prefix)`（M:N invariant 對應）
   - `unlink()` override：同上 filter
4. `src/security/ir_rules.xml`：
   - `mcp_server_rule_workspace_user` / `mcp_tool_rule_workspace_user` dual-leg leg 1 walk path：`project_id.cloud_service_id.*` → `project_id.cloud_service_ids.*`
   - leg 2 unbound carve-out：`project_id.cloud_service_id = False` → `project_id.cloud_service_ids = False`
5. `src/migrations/18.0.1.0.26/pre-migrate.py`：
   - SQL：`CREATE TABLE IF NOT EXISTS project_cloud_service_rel ...`
6. `src/migrations/18.0.1.0.26/post-migrate.py`：
   - Step 1-6（design doc §7.0.2）
   - Step 7-10（design doc §7.0.7）

**新 models（additive）**：

7. `src/models/agent_template.py` — design doc §4.1 schema
8. `src/models/agent_session.py` — design doc §2.12 schema + §4 fields（primary_workspace_id / quota_override_*）
9. `src/models/skill.py` — design doc §3.2 schema
10. `src/models/project_access.py` — design doc §4.3 schema
11. `src/models/__init__.py` — imports

**ACL（new models）**：

12. `src/security/ir.model.access.csv` — 加 access rows for 4 new models（owner/admin/user/guest CSV gate）
13. `src/security/ir_rules.xml` — 加 4 rule × 4 new models = 16 rules（仿 mcp-acl-6-rule-split pattern）

**Views（backend）**：

14. `src/views/agent_template_views.xml`
15. `src/views/agent_session_views.xml`
16. `src/views/skill_views.xml`
17. `src/views/project_access_views.xml`
18. `src/views/project_external_mcp_setting.xml`（admin 開關 + SSRF risk 警示）

**Manifest**：

19. `src/__manifest__.py`：bump version `18.0.1.0.25` → `18.0.1.0.26`；加 data files

### Test scope（Phase A）

- Migration regression：既有 record `cloud_service_ids = {cloud_service_id}` 1-element set（backfill correctness）
- M:N add：`project.cloud_service_ids = [A, B]` → 通過 UNIQUE constraint 刪除後合法
- task.primary_cloud_service_id computed default 對 multi-binding project 取 first
- task.extra_cloud_service_ids domain 允許跨 workspace（M:N project 蘊含）
- cloud_service unlink: M:N 下只清 URL match 該 sidecar 的 user-scope MCP，其他保留
- ACL M2M walk: bound + unbound carve-out 12-case matrix 通過（refactor branch 既有 test 延伸）
- project_access bootstrap: 既有 project 自動有 owner row（從 cloud_service.workspace_id.owner partner）
- external_mcp_allowed default False；admin only 改

### Dependencies

- refactor branch `feature/refactor-user-scope-mcp` PR #54 已 merge develop（已完成 2026-05-21）
- branch 策略 A：`feature/agent-pod` rebase 到 含 PR #54 的 develop

### Estimated effort

**1-2 週**（5-10 工作日）

### Risk

| Risk | Mitigation |
|------|-----------|
| M:N backfill 漏 | post-migrate.py defensive + dry-run on stg |
| 既有 controller / cron walk path 漏改 | grep `cloud_service_id` 全 codebase + walk path 改 |
| project_access bootstrap 漏單 partner | post-migrate.py log warn for project without owner |
| ACL dual-leg M2M walk syntax 錯（odoo ORM 對 M2M `= False` 行為）| 單元測試 cover 12 case |

### Rollback

- `git revert` Phase A commit + run rollback SQL：
  ```sql
  DROP TABLE project_cloud_service_rel CASCADE;
  ALTER TABLE project_project ADD CONSTRAINT project_project_unique_cloud_service UNIQUE (cloud_service_id);
  DROP TABLE woow_paas_platform_agent_template CASCADE;
  -- ... 其他 new model
  ```
- 既有 ai_client.py / discuss_channel.py 不動，rollback 無影響

---

## Phase B — Image source + helm chart in repo（no deploy）

### Goal

新建 `woow-paas-agent-runtime` repo + helm chart in `woow-paas-charts/charts/agent-runtime/`；CI lint/test/build/push image to JCR；**不部署到任何 cluster**。

### Tasks

**新 repo `woow-paas-agent-runtime`**：

1. Repo skeleton：`runtime/`, `Dockerfile`, `requirements.txt`, `pytest.ini`, `tests/`, `.gitea/workflows/`
2. `runtime/transport/ws_server.py` — FastAPI WebSocket route `/v1/chat`（design doc §5.8 pseudocode）
3. `runtime/transport/auth.py` — HS256 token verify with dual-secret fallback（§6.6）
4. `runtime/transport/envelope.py` — JSON envelope serializer + 15 type enum（§5.2）
5. `runtime/langgraph_runtime.py` — Module-level singleton LangGraph + stream
6. `runtime/skills/loader.py` — SKILL.md parse + StructuredTool wrap
7. `runtime/skills/cache.py` — Lazy fetch + SHA256 verify + extract
8. `runtime/skills/subprocess_runner.py` — § 6.4 spawn pseudocode（env whitelist + rlimit）
9. `runtime/security/url_validator.py` — Pod-side SSRF check
10. `runtime/security/jti_cache.py` — TTLCache(10k, 65min) + lock
11. `runtime/security/audit_logger.py` — Structured JSON to stdout
12. `runtime/state/mcp_reloader.py` — ConfigMap inotify watcher
13. `runtime/state/template_reloader.py` — Template hot reload
14. `runtime/state/skill_reloader.py` — Skill snapshot reloader
15. `Dockerfile`：`FROM python:3.11-slim` + non-root user 10001 + COPY runtime/ + ENTRYPOINT uvicorn
16. `requirements.txt`：fastapi, uvicorn, websockets, PyJWT, cachetools, langchain-core, langgraph, langchain-openai, langchain-mcp-adapters

**helm chart `woow-paas-charts/charts/agent-runtime/`**：

17. `Chart.yaml`：name=agent-runtime, version=0.1.0, appVersion=1.0.0
18. `values.yaml`：image / resources / pvc / agentRuntime.workspaceIds / externalEgress.enabled / etc
19. `templates/namespace.yaml`（仿 dev-sandbox，命名 `paas-ws-pa-{hash}`）
20. `templates/deployment.yaml`（per §6.1 securityContext + §6.2 volume mounts + §6.9 SA + automountServiceAccountToken=false）
21. `templates/service.yaml`（ClusterIP for WS port 8000）
22. `templates/configmap-mcp.yaml`（mcp.json mount）
23. `templates/network-policy.yaml`（§6.3 三類 egress + hybrid external scheme）
24. `templates/serviceaccount.yaml` + `role.yaml` + `rolebinding.yaml`（§6.9 minimal RBAC）
25. `templates/secret.yaml`（reference cluster-wide `agent-runtime-auth-secret`）

**CI workflows**：

26. `woow-paas-agent-runtime/.gitea/workflows/test.yml` — pytest tests on PR
27. `woow-paas-agent-runtime/.gitea/workflows/build-and-trigger.yml` — push tag → build image → push JCR → Gitea API dispatch to `woow-paas-charts`

### Test scope（Phase B）

- Image build CI 通過
- `helm template charts/agent-runtime/` 渲染通過（values 完整）
- 本地 docker run: container starts + WS server respond port 8000
- Unit test: envelope serializer / auth verify / subprocess rlimit
- Integration test: mock WS client connect + send user_message → receive done envelope

### Dependencies

- Phase A 完成（new models available for ConfigMap mcp.json snapshot 結構參考）

### Estimated effort

**1 週**（3-5 工作日）

### Risk

| Risk | Mitigation |
|------|-----------|
| LangGraph code 從 ai_client.py lift 過程漏移植 | tester 寫 lift correctness regression（比對 input/output 等效） |
| Helm chart 渲染 corner case（workspaceIds empty / external disabled）| values 多 scenario lint + helm template 驗證 |
| Image 體積過大 | multi-stage Dockerfile + slim base |

### Rollback

- tag delete + image registry tag delete（無 cluster 引用）

---

## Phase C — paas-operator agent-runtime router + Odoo client side

### Goal

開通 paas-operator 新 endpoints + Odoo 端 `agent_runtime_client.py` service + webhook receiver。**還沒** flip SSE generator 切換邏輯。

### Tasks

**paas-operator**：

1. `extra/paas-operator/src/api/agent_runtimes.py`（新檔）：
   - `POST /api/agent-runtimes/spawn-or-attach`
   - `POST /api/agent-runtimes/{project_hash}/reap`
   - `POST /api/agent-runtimes/{project_hash}/resume`（or 內含於 spawn-or-attach）
   - `POST /api/agent-runtimes/{project_hash}/invalidate-mcp`
   - `POST /api/agent-runtimes/{project_hash}/sync-workspaces`
   - `DELETE /api/agent-runtimes/{project_hash}`
   - `GET /api/agent-runtimes/{project_hash}/status`
2. `extra/paas-operator/src/services/jwt_signer.py`（HS256 + dual-secret read from K8s Secret）
3. `extra/paas-operator/src/services/webhook_dispatcher.py`（HMAC-SHA256 sign + retry 3x exp backoff）
4. `extra/paas-operator/src/services/mcp_invalidator.py`（patch ConfigMap + WS push to pod）
5. `extra/paas-operator/src/models/schemas.py`（agent-runtime request/response Pydantic models）
6. `extra/paas-operator/src/main.py`：include 新 router
7. K8s Secret `agent-runtime-auth-secret`（在 `paas-system` ns）含 `current` + `previous` keys

**Odoo client side**：

8. `src/services/agent_runtime_client.py`（新檔）— HTTP client 包：`spawn_or_attach()` / `reap()` / `invalidate_mcp()` / `sync_workspaces()` / `delete()`
9. `src/controllers/agent_runtime_webhook.py`（新檔）— `POST /api/agent-runtimes/webhook/state-update`（HMAC verify + 更新 agent_session.state）
10. `src/__manifest__.py`：requirements.txt 加 `websockets`（client）
11. `src/data/agent_session_cron.xml` — drift detection cron（每 5 min 對比 paas-operator status）

### Test scope（Phase C）

- paas-operator unit test：`spawn_or_attach` mock K8s（無真實 pod）— 簽 JWT + return mock URL
- paas-operator integration test：與 dev k8s 真起 pod 並 attach
- Odoo `agent_runtime_client.spawn_or_attach` 從 dev shell call paas-operator 成功取 token
- Webhook receiver: HMAC valid → update agent_session.state；invalid → 401 + log warn
- jti cache 在 paas-operator 內：同 jti 第二次 sign 不被 verify pass（防 replay）

### Dependencies

- Phase B 完成（image push 到 JCR，paas-operator 在 spawn-or-attach 內 reference 該 image）

### Estimated effort

**1 週**（3-5 工作日）

### Risk

| Risk | Mitigation |
|------|-----------|
| paas-operator router 加新 endpoint 撞既有 namespace_prefix check | 確認 `paas-ws-pa-{hash}` 滿足 `paas-ws-` prefix（design doc §2.7 已確認） |
| JWT 簽 / 驗 mismatch（algo / claim shape）| Unit test cover full sign+verify roundtrip |
| Webhook HMAC secret 未配置 | helm chart 必填 + paas-operator 啟動 fail loud |

### Rollback

- paas-operator router 改 410 Gone
- Odoo client / webhook 留 dormant（不被 caller 引用）

---

## Phase D — Pilot + dual-write start

### Goal

第一個 pilot project（admin-controlled）切換到 agent-runtime pod；其他 project 仍走舊 ai_client.py 同步。觀察 ≥ 2 週。

### Tasks

1. `src/controllers/ai_assistant.py` SSE generator：加 `if project.use_agent_runtime` 分支
   - True 分支：design doc §5.6 pseudocode（spawn-or-attach + WS bridge）
   - False 分支：既有 ai_client.py 同步路徑（不動）
2. `src/services/agent_runtime_client.py`：integrate to `_post_ai_reply` path（discuss_channel.py 內加 dual-mode）
3. `src/views/project_views.xml`：admin 看得到 `use_agent_runtime` toggle（隱式於後台 form）
4. Admin 在 Odoo backend 對 1 個 pilot project 設 `use_agent_runtime = True`
5. Admin 建第一個 agent_template + bind 到 pilot project
6. Monitor dashboard 設定：metric scraping（cold start latency / WS error rate / worker pool util）

### Test scope（Phase D）

- E2E test: pilot project chat invoke → 收 SSE chunks → 與 ai.assistant 既有 path 行為等效
- Regression: 非 pilot project 行為零變動（all green）
- 觀察 metric: cold start p95 < 15s / WS error rate < 1% / worker pool util < 50%

### Dependencies

- Phase A + B + C 全部 ship + dev/stg cluster 跑通

### Estimated effort

**2 週**（pilot 觀察期）

### Risk

| Risk | Mitigation |
|------|-----------|
| Pilot project chat 完全失敗 → user 受影響 | Pilot 選 admin-controlled internal project；2 週 observation；use_agent_runtime flag 立即 toggle |
| Worker pool 撞滿（pilot 用太多 conv）| 監控 + admin alert at 0.8 util |
| Cold start latency 超 15s | metric warn；若持續 > 15s → 暫停 phase advance |

### Rollback

- `project.use_agent_runtime = False`（admin UI 一鍵）
- agent pod 自然 reap（30 min idle）
- 既有 ai_client.py 路徑接手

---

## Phase E — Gradual rollout

### Goal

漸進 enable `use_agent_runtime` flag for additional projects；最終 100% project on agent-runtime。

### Tasks

1. Admin Dashboard widget（既有 `dashboard.py` 內加）：
   - 批次 enable / disable per project
   - 顯示 active conv count / worker util / per-project error rate
2. Cluster-wide global toggle config_parameter `force_disable_agent_runtime`（緊急 fallback）
3. Phase E 起 cron 每天 audit log：`legacy_sync` count by project（追蹤剩餘 legacy path 使用）
4. Per-week rollout batches：admin 自選 N projects each week
5. Documentation：customer-facing migration note（user 不需做事，admin 透明切換）

### Test scope（Phase E）

- Load test: 100 active conv across 30 projects + worker pool 用量
- NetworkPolicy egress：跨 workspace project 真實 invoke sidecar-backed MCP + external MCP
- Per-project quota override：admin 改 override → next spawn-or-attach 收到新 ResourceQuota
- 觀察 metric: 達 80% rollout 後 stable ≥ 2 週

### Dependencies

- Phase D 觀察成功（cold start / error rate / worker pool 在 SLO 內）

### Estimated effort

**2-4 週**（漸進 + 觀察）

### Risk

| Risk | Mitigation |
|------|-----------|
| 多 project 撞 worker pool / quota / network | 漸進；admin dashboard 顯示 metric；warn at 0.8 util；緊急 global toggle |
| 跨 workspace cloud_service binding 觸發 NetworkPolicy 渲染失敗 | E2E test 涵蓋 multi-workspace project；sync-workspaces 失敗 alert |
| MCP invalidate 路徑漏（active pod 不收 invalidate event）| inotify 60s 兜底；audit `mcp_invalidate_received` event |
| Audit log volume 爆 | log retention policy；SRE 拍板 |

### Rollback

- 任 project disable flag
- 緊急 cluster-wide `force_disable_agent_runtime=True`
- 既有 ai_client.py 接手

---

## Phase F — Old path cleanup（destructive）

### Goal

100% project on agent-runtime 且 ≥ 4 週 observation 穩定後，刪舊 LangGraph path + dependency cleanup。

### Tasks

1. 刪 `src/models/discuss_channel.py:55-118` `message_post` override 內 `_post_ai_reply` 同步分支
2. 刪 `src/models/ai_client.py`：
   - `_build_agent_graph`
   - `_async_agent_invoke`
   - `_async_agent_stream`
   - `chat_completion_with_tools`
   - `chat_completion_stream_with_tools`
3. `src/models/ai_client.py` 改為純 metadata serializer：保留 `from_assistant` / `_build_mcp_server_config` / `_build_prefixed_enabled_names`
4. 從 `src/__manifest__.py` external_dependencies 移除：
   - `langchain_core`
   - `langgraph`
   - `langchain_openai`
   - 保留 `langchain_mcp_adapters`（mcp_server.action_sync_tools cron 仍需要；v2 才拔）
5. 刪 `src/models/project_project.py` `use_agent_runtime` field（不再 dual-mode）
6. 刪 `src/controllers/ai_assistant.py` SSE generator 內 if-else 分支（純走 agent-runtime path）
7. 刪 `default_ai_assistant_id` config_parameter fallback 邏輯
8. `src/migrations/18.0.1.0.30/post-migrate.py`：
   - 對未設 `agent_template_id` 的 project，bootstrap system default agent_template（從 `default_ai_assistant_id` context_id copy system_prompt）
9. `src/__manifest__.py`：bump version `18.0.1.0.29` → `18.0.1.0.30`

### Test scope（Phase F）

- Regression: 全 project chat 功能等同 Phase E（before destructive cleanup）
- Audit log 確認 `legacy_sync` count = 0 持續 4 週前才執行
- Odoo container image size 縮小 ≥ 30%
- `pip install` 不會缺 langchain_core / langgraph / langchain_openai（已從 external_dependencies 拔）
- migrate test: 未設 agent_template_id 的 project 自動 bootstrap system default
- Trip wire: 試圖呼 `_async_agent_invoke` raise AttributeError（已刪）

### Dependencies

- Phase E 100% rollout + 4 週 observation

### Estimated effort

**1 週**（謹慎 destructive PR）

### Risk

| Risk | Mitigation |
|------|-----------|
| Destructive cleanup 後遲發現 bug | Phase E 觀察期 ≥ 4 週 + audit log `legacy_sync` count 確認 = 0 持續 4 週 |
| `langchain_mcp_adapters` 留 Odoo 端混用版本（agent-runtime image 內也有）| 兩端 pin same version；CI 跨 repo dependency check |
| Phase F PR 規模大 → review 困難 | 拆 sub-PR：(a) 刪 message_post 同步 (b) 刪 ai_client.py LangGraph (c) 拔 external_dependencies (d) 刪 use_agent_runtime flag |

### Rollback

- `git revert` Phase F PR（destructive 但 atomic）
- 重新 `pip install langchain_core langgraph langchain_openai`
- `use_agent_runtime` flag 重加（default True 保 prod project state）

---

## Phase G — Fleet bundle + production deploy

### Goal

`woow-paas-cd` Fleet bundle 加 agent-runtime；dev → stg → prod 階段 rollout；建立 monitoring + alert。

### Tasks

1. `woow-paas-cd/bundles/agent-runtime/fleet.yaml`（新檔）：
   - paas-operator new version reference（含 agent-runtime router）
   - agent-runtime helm chart registry reference
   - K8s Secret `agent-runtime-auth-secret`（dual key）
2. Deploy 順序 enforcement（Fleet bundle dependency）：
   - paas-operator new version first
   - K8s Secret create
   - woow-paas-platform addon update（trigger Phase A schema migration）
   - 監控 ready
3. Monitoring stack（SRE coord）：
   - Prometheus scrape：paas-operator metrics + agent pod metrics
   - Grafana dashboard：per-namespace agent state / per-project active conv
   - Alert rules：cold start p95 > 20s / WS error > 5% / worker pool > 80% / quota usage > 90%
4. Dev → Stg → Prod rollout（每階段 ≥ 1-2 週觀察）

### Test scope（Phase G）

- Smoke test: 每階段 cluster deploy 後第一個 chat invoke 成功
- Monitor alert: 觸發測試（人工注入 error 確認 alert fire）
- Fleet bundle rollback: 在 dev 驗證 Fleet 反向 sync 機制
- Pre-prod: stg 跑 production-like load ≥ 2 週

### Dependencies

- Phase F destructive cleanup 完成
- SRE 監控配置 ready

### Estimated effort

**1-2 週**（dev → stg → prod 階段）

### Risk

| Risk | Mitigation |
|------|-----------|
| Fleet deploy 順序錯（addon 先升但 operator 未升）| Fleet bundle dependency；version 約束 |
| 監控 stack 未 ready → 部署後盲飛 | Phase G 不允許 prod deploy without 監控 stack ready |
| Prod deploy 後遭遇未模擬問題 | stg ≥ 2 週 observation；prod canary 部署（first 10% project） |

### Rollback

- Fleet 反向 sync 到 paas-operator v1（無 agent-runtime router）
- 既有 cloud_service 不受影響

---

## Hand-off checklist（phase-planner → tester → implementation → caqa → reviewer → reporter）

### To phase-tester

- 取得 design doc + 本 plan
- 每 phase 的 「Test scope」段落為起點
- Critical regression（refactor branch existing test 全綠保證）：
  - mcp_server / mcp_tool 6-rule ACL（9-12 case）
  - D4 invariant
  - cleanup ordering Inv #9
  - 既有 ai.assistant Discuss UI path（non-PaaS chat）
- 新加 regression：
  - WS envelope sequence_number monotonic
  - jti replay reject
  - dual-secret rotation 4-phase transition
  - NetworkPolicy egress hybrid
  - subprocess rlimit enforce
  - per-project quota override
  - project_access role × invoke 權限矩陣
- 對齊 [[two-layer-acl-lockstep]] 檢查
- 預估 test cases: ~80-100 cases（含 unit + integration + E2E + migration）

### To phase-test-explainer

- 拿 tester output → 寫 coverage report `docs/tracking/test-reports/test-coverage-agent-runtime-{date}.md`
- 對齊 [[mcp-project-decouple]] / [[mcp-url-redesign]] cell-map pattern
- 標 implicit-through cell 與 explicit cell 分開（rounding round 2 lesson）

### To phase-implementation

- 依本 plan Phase A → G 順序執行
- Branch 策略：A（rebase `feature/agent-pod` 到 develop，已 sign-off）
- Phase F 不可跳（destructive cleanup 必須 Phase D-E observation 期滿）
- 跨 repo coordination：Phase B 同時建 `woow-paas-agent-runtime` repo + `woow-paas-charts/charts/agent-runtime/`
- decision doc updates（Phase A 完成時）：
  - `cloud-service-project-decouple.md` D1 invariant 字面（M:N）
  - `mcp-project-decouple.md` Inv #4 字面（M:N URL prefix）
  - `mcp-url-redesign.md` Inv #9 字面（M:N）

### To phase-caqa

- 14 invariants verify（沿用 PR #47 / PR #54 CAQA template）
- 新加 invariants verify list：
  - per-project namespace hash collision 不發生（hmac-sha256 80-bit birthday bound）
  - WS envelope version frozen for v1.0
  - jti cache size 不爆（10k entries + 65 min TTL）
  - Audit log structured JSON parse-able + 無 PII leak
  - dual-secret rotation 4-phase 不中斷既有 active token
- 兩層 ACL lock-step（[[two-layer-acl-lockstep]]）：
  - ir.rule (mcp_server / mcp_tool / agent_template / skill / project_access) ↔ controller `_check_project_access`
- 重點 hot zone grep：
  - schema migration 同源 walk path
  - subprocess sandbox sandbox 機制
  - NetworkPolicy template render

### To phase-reviewer

- multi-agent review（5-agent pattern：code-reviewer / pr-test-analyzer / silent-failure-hunter / type-design-analyzer / comment-analyzer）— PR #47 + PR #54 已驗證有效
- 同源 code path 檢查必走 grep（不限 plan AC 範圍）
- Phase F 特別關注：cleanup 後是否漏移植 functionality 到 agent-runtime

### To phase-reporter

- Finalize decision docs（依 implementation 最終 commit hash）：
  - 更新 cloud-service-project-decouple.md / mcp-project-decouple.md / mcp-url-redesign.md
  - 新增 `docs/decisions/refactor/agent-runtime-pod-architecture.md`（Phase G ship 時建）
- 抽 pattern：
  - `per-project-namespace-hash-scheme.md`（hmac-sha256 + base32 + Odoo store mapping）
  - `dual-secret-rotation-4-phase.md`（K8s Secret current/previous + admin manual rotation）
  - `bundle-declare-host-enforce.md`（SKILL.md frontmatter metadata vs host pod-level enforce）
  - `cross-repo-ci-dispatch.md`（Gitea API dispatch pattern，已有 paas-odoo-ci 一份；本 epic 是第二份應用）
- follow-ups：
  - langchain-mcp-adapters Odoo 端拔（v2）
  - Redis jti cache（v2）
  - DNS rebinding 防護（refactor follow-up #52）
  - gVisor / Kata for marketplace skill（v2）
  - mcp_server.action_sync_tools 遷到 agent-runtime（v2）

---

## Test scope per phase（彙整）

| Phase | Unit | Integration | E2E | Migration | Load |
|-------|------|-------------|-----|-----------|------|
| A | new models / ACL rules / write override | post-migrate.py defensive | — | M2N backfill correctness | — |
| B | subprocess sandbox / envelope serializer / jti cache | mock WS client | image local docker run | — | — |
| C | jwt sign+verify / webhook HMAC | mock K8s spawn-or-attach | dev shell call | — | — |
| D | — | pilot end-to-end | pilot project chat (pod path) | — | pilot 3 conv 同時 |
| E | — | dual-mode mix | multi-project rollout | — | 100 active conv 跨 30 projects |
| F | — | post-cleanup regression | full migration to system default agent_template | Phase F migration script | — |
| G | — | Fleet deploy dependency | dev → stg → prod smoke | — | production canary 10% project |

---

## Migration / Deployment runbook（high level）

### Pre-Phase A

1. Confirm refactor branch PR #54 已 merge develop ✓
2. `feature/agent-pod` rebase to latest develop ✓
3. CI 全綠 (`tests/test_mcp_*` 等 refactor branch test 通過)

### Phase A deploy

1. 升 `__manifest__.py` 版本到 `18.0.1.0.26`
2. Stg cluster: `kubectl exec -it odoo-pod -- odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init`
3. 確認 post-migrate.py log 成功（backfill rows / new tables / project_access bootstrap）
4. 跑 Phase A test suite
5. 觀察 ≥ 1 週後 prod 升版

### Phase B-C deploy

- Phase B: image push 到 JCR + helm chart 寫入 repo（不部署）
- Phase C: paas-operator 升版 to dev cluster；Odoo addon 沒 functional change（just stub）

### Phase D deploy

1. Pilot project admin 在 Odoo UI 設 `use_agent_runtime = True` + 建 agent_template
2. 第一次 chat invoke → 觀察 paas-operator log + K8s pod 出現
3. 觀察 ≥ 2 週

### Phase E deploy

- 每週 admin 批次 enable N projects（從 small 到 large team）
- Dashboard 監控；觸發 disable 即可 fallback

### Phase F deploy

- 全 100% rollout + 觀察 ≥ 4 週
- audit log `legacy_sync` count = 0 持續 4 週
- 拍板「可 destructive cleanup」→ 升 `18.0.1.0.30` + 跑 migration

### Phase G deploy

- Fleet bundle 加入
- dev → stg → prod 各層 ≥ 1-2 週觀察
- Prod canary 10% projects → 50% → 100%

---

## Open questions / risks（cross-phase）

> 全部 Open Questions 對應 design doc §7.10。本 plan 預設採 phase-planner 推薦；leader 拍板異動則本 plan 對應更新。

1. **Migration phase 切片粒度** — 7-phase（推薦）vs 合併 / 再拆
2. **Cross-repo CI trigger** — Gitea dispatch（推薦）vs 手動 PR / 浮動 tag
3. **Dual-write window 長度** — 6-12 週（推薦）vs 4 週 / 16 週+
4. **`ai_client.py` 刪除時機** — Phase F（推薦）vs 永不刪 / v2 才刪
5. **Fleet bundle 對接時機** — Phase G dev→stg→prod 階段（推薦）vs 並行 / direct prod

**Cross-phase risks**：

- agent-runtime image bug 致 cold start fail：mitigation phase D-E fallback flag；Phase F-G 後 fallback 拔掉前要極穩定
- shared secret 過期未 rotate：§6.6 雙 secret window；admin 30 day 提前 alert
- 跨 repo CI dispatch race：charts CI 等 image tag exists 才 commit bump；超時 fail loud
- 多 phase 累積 effort：總計 9-13 週；admin 預留 buffer 至 16 週
- Phase F destructive 後 4 週才發現 bug：Phase E ≥ 4 週 observation；audit log 確認 legacy_sync = 0 持續 4 週
- Decision doc 更新落後 implementation：reporter phase 每 phase ship 時同步更新 decision doc

---

## References

### Design / decisions

- `docs/plans/2026-Q2/agent-runtime-pod-design.md`（本 plan 對應 design doc）
- `docs/decisions/refactor/cloud-service-project-decouple.md`（D1/Invariants）
- `docs/decisions/refactor/mcp-project-decouple.md`（PR #47 finalized Invariants）
- `docs/decisions/refactor/mcp-url-redesign.md`（PR #54 finalized Inv #9/#10/#11）
- `docs/decisions/patterns/mcp-acl-6-rule-split.md`（dual-leg variant）
- `docs/decisions/patterns/two-layer-acl-lockstep.md`
- `docs/decisions/patterns/unbound-project-acl-dual-leg.md`
- `docs/decisions/patterns/sidecar-backed-flag-lifecycle.md`

### Codebase pointers（implementation 時對照）

- `src/controllers/ai_assistant.py`（SSE generator rewrite target — §5.6）
- `src/models/ai_client.py`（LangGraph 搬出 target — Phase F destructive）
- `src/models/discuss_channel.py`（message_post override sync branch — Phase F destructive）
- `src/models/project_project.py`（schema migration target — Phase A）
- `src/models/cloud_service.py`（_do_cleanup M:N URL filter — Phase A）
- `src/models/mcp_server.py`（D4 invariant 守線；不動 schema）
- `extra/paas-operator/src/`（agent-runtime router target — Phase C）
- `charts/odoo-dev-sandbox/`（helm chart pattern reference for agent-runtime chart — Phase B）

### CI / deployment 既有 pattern

- `woow_paas_platform/.gitea/workflows/trigger-odoo-build.yml`（cross-repo dispatch pattern reference — Phase B/§7.2）
- `paas-odoo-ci/.gitea/workflows/build-image.yml`（image build pattern reference — Phase B）
- `extra/paas-operator/helm/`（paas-operator helm chart pattern reference）
