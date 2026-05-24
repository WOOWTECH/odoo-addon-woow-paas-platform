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

## Phase C — paas-operator endpoints + Odoo WS gateway + client/webhook

> **[Phase C 細化 2026-05-23 — Phase A/B merged, base 解鎖]** scope 對齊 leader Phase C 三大塊；補上原 task list 漏的 **skill bundle download endpoint**（Phase B image `cache.py` 假設存在）+ **SKILL_BUNDLE_TOKEN 簽發/注入** + **agent_session webhook-sync field**。

### Goal

開通三大塊：(1) paas-operator agent-runtime endpoints (2) Odoo controller WS gateway（SSE generator rewrite，§5.6） (3) agent_runtime_client + webhook + skill bundle endpoint。**SSE rewrite 含 `use_agent_runtime` flag gate（預設 False）— code ready but not activated**；Phase C **不做** Phase D pilot deployment（不 enable flag + monitor）。

### Phase C scope 邊界（vs Phase D）

- **Phase C ship**：WS gateway code（SSE rewrite + dual-mode flag 預設 False）+ paas-operator endpoints + client/webhook/skill-bundle endpoint + K8s Secret
- **Phase C 不做**（→ Phase D）：enable `use_agent_runtime` flag for pilot project / monitor cold start latency / 實際 pod 起在 production cluster
- 理由：code 與 activation 分離 — Phase C merge 後 production chat 行為零變動（flag False 走既有 ai_client.py），Phase D 才漸進 enable

### Tasks（implementation-level，依賴序 ①→④）

#### ① paas-operator agent-runtime endpoints（`extra/paas-operator/` — 只新增，不動既有 router）

1. `extra/paas-operator/src/services/jwt_signer.py`（新檔）— HS256 sign + dual-secret read from K8s Secret（§5.3 / §6.6）；**兩種 token scope**：
   - `sign_ws_token(project_hash, channel_id, invoker_user_id, invoker_role, exp=1h)` — Odoo controller open WS 用（per-invocation）
   - `sign_skill_bundle_token(project_hash, project_id, workspace_ids, exp=1h)` — 注入 pod env `SKILL_BUNDLE_TOKEN`（per-pod-lifetime；cache.py fetch 用）
2. `extra/paas-operator/src/services/webhook_dispatcher.py`（新檔）— pod→Odoo state-update outbound：HMAC-SHA256 sign + retry 3x exp backoff（§5.5）
3. `extra/paas-operator/src/services/mcp_invalidator.py`（新檔）— patch ConfigMap mcp.json + WS push to pod（§5.4）
4. `extra/paas-operator/src/models/schemas.py`（**既有檔，append** agent-runtime Pydantic models）— `SpawnOrAttachRequest` / `SpawnOrAttachResponse` / `SyncWorkspacesRequest` / `AgentRuntimeStatusResponse`
5. `extra/paas-operator/src/api/agent_runtimes.py`（新檔，新 router `prefix=/api/agent-runtimes`）：
   - `POST /spawn-or-attach` — body `{project_hash, project_id, channel_id, invoker_user_id, invoker_role, project_workspace_ids, agent_template_snapshot, mcp_snapshot, skill_snapshot}` → lookup helm release `agent-pa-{hash}` in ns `paas-ws-pa-{hash}` → install（含 helm values: image tag + pod env `ODOO_BASE_URL`/`PROJECT_HASH`/`SKILL_BUNDLE_TOKEN`/`SHARED_SECRET` ref）/ scale-1 / no-op → poll readiness → sign `ws_token` → return `{pod_ws_url, ws_token, pod_state, spawn_age_ms}`
   - `POST /{project_hash}/reap` — `scale_release(replicas=0)` + annotation `reaped-since`
   - `POST /{project_hash}/resume` — scale-1（or 內含 spawn-or-attach；resume 時 **重簽 SKILL_BUNDLE_TOKEN** 並 patch pod env，見 ⚠ escalation）
   - `POST /{project_hash}/invalidate-mcp` — call mcp_invalidator
   - `POST /{project_hash}/sync-workspaces` — body `{project_workspace_ids}` → helm upgrade 重渲染 NetworkPolicy egress
   - `DELETE /{project_hash}` — helm uninstall
   - `GET /{project_hash}/status` — return `{state, last_active, pod_ws_url, spawn_age_ms}`
6. `extra/paas-operator/src/main.py`（**既有檔**）— `app.include_router(agent_runtimes.router)`（只加一行 include；既有 releases/namespaces/tunnels/routes/init/odoo_modules router 不動）
7. K8s Secret `agent-runtime-auth-secret`（`paas-system` ns）含 `current` + `previous` keys（helm chart 落地 — Phase G Fleet；Phase C dev 手動 `kubectl create secret`）

#### ② Odoo skill bundle download endpoint + agent_session field（cross-repo wiring 對齊 Phase B cache.py）

8. `src/models/agent_session.py`（**既有檔，append** 2 field）— webhook state sync 需要：
   - `last_synced_at = fields.Datetime(readonly=True)`（webhook / cron 更新時戳）
   - `last_state_event_ts = fields.Datetime(readonly=True)`（最後一次 paas-operator state event ts，drift detection 用）
   - migration `18.0.1.0.27/` 加 column（additive，nullable，無 backfill）
9. `src/controllers/agent_runtime_skill.py`（新檔）— **Phase B `cache.py` 假設的 endpoint**：
   - `GET /api/agent-runtimes/skill-bundle?project_hash={hash}&skill_name={name}&version={ver}`（auth='none'，內部 verify `SKILL_BUNDLE_TOKEN` Bearer header；不走 Odoo session）
   - verify token（HS256 dual-secret，同 §5.3）→ decode project_id/workspace_ids → project_access 確認 skill ∈ project + state=published → return `ir.attachment` binary（`skill.bundle_attachment_id.datas`）
   - **token authoritative**（不信 query param project_hash — 對齊 HC-2 ruling [[constraint-input-trust-expansion]]）：以 token claims 的 project_id 為準，query param 僅 cross-check

#### ③ Odoo agent_runtime_client + webhook receiver

10. `src/services/agent_runtime_client.py`（新檔）— HTTP client wrapper to paas-operator：`spawn_or_attach()` / `reap()` / `resume()` / `invalidate_mcp()` / `sync_workspaces()` / `delete()` / `get_status()`；讀 `ir.config_parameter` operator_url + operator_api_key（沿用既有 `get_paas_operator_client` pattern）
11. `src/controllers/agent_runtime_webhook.py`（新檔）— `POST /api/agent-runtimes/webhook/state-update`（auth='none'，HMAC-SHA256 verify with `agent-runtime-webhook-secret`）→ 找 agent_session by `project_namespace_hash` → update `state` + `last_synced_at` + `last_state_event_ts`；invalid HMAC → 401 + log warn
12. `src/data/agent_session_cron.xml`（新檔）— drift detection cron（每 5 min：search agent_session state ∈ active/spawning/reaped/degraded AND last_synced_at < now-10min → call `get_status` 補同步）
13. `src/__manifest__.py`（**既有檔**）— bump version `18.0.1.0.26` → `18.0.1.0.27`；data 加 cron xml + migration

#### ④ Odoo controller WS gateway（SSE generator rewrite，§5.6）

14. `src/controllers/ai_assistant.py`（**既有檔**）— `/api/ai/stream/<channel_id>` SSE generator rewrite per §5.6：
    - 加 `if project.use_agent_runtime:` 分支（**flag gate，預設 False**）
      - True 分支：resolve project_id + project_access check → 序列化 agent_template/mcp/skill snapshot → `agent_runtime_client.spawn_or_attach()` → open outbound WS to `pod_ws_url`（帶 `ws_token`）→ relay 15-type envelopes as SSE events（envelope→既有 SSE schema 轉換；done 1:1，§5.6/§5.8）
      - False 分支：既有 ai_client.py 同步路徑（**不動**）
    - frontend `ai_service.js` EventSource **零變更**（§1.2 sign-off）
15. `requirements.txt`（**既有檔**）— 加 `websockets`（Odoo controller WS client）+ `PyJWT`（webhook/skill-bundle token verify）

### Test scope（Phase C）

**① paas-operator**：
- unit test：`spawn_or_attach` mock K8s（無真實 pod）— 簽 ws_token + skill_bundle_token + return mock URL
- unit test：jwt_signer 兩種 token scope sign+verify roundtrip；dual-secret current/previous fallback
- integration test：與 dev k8s 真起 pod 並 attach（既有 paas-operator integration test pattern，`-m integration`）
- jti cache replay：同 jti 第二次 verify 不 pass

**② skill bundle endpoint + agent_session field**：
- skill bundle download：valid SKILL_BUNDLE_TOKEN → return binary；token invalid/expired → 401；project_access 不符 → 403；skill not published → 404
- **token authoritative**：query param project_hash ≠ token claims project_id → 用 token claims（HC-2 ruling regression lock）
- agent_session migration 27：`last_synced_at` / `last_state_event_ts` column additive nullable（既有 row 不破）

**③ client + webhook**：
- Odoo `agent_runtime_client.spawn_or_attach` 從 dev shell call paas-operator 成功取 token
- Webhook receiver: HMAC valid → update agent_session.state + last_synced_at；invalid → 401 + log warn
- drift cron：last_synced_at stale → 補 get_status 同步

**④ SSE gateway rewrite**：
- `use_agent_runtime=False`（預設）→ 走既有 ai_client.py 同步路徑（**regression 全綠，production 行為零變動**）
- `use_agent_runtime=True`（test fixture 開）→ WS path：spawn-or-attach → open WS → relay 15-type envelope → SSE event 轉換（envelope→`{chunk}`/`{done}`）；done 1:1（pod 單一 producer）
- frontend `ai_service.js` EventSource 零變更（snapshot test 確認 SSE schema 不變）

### Dependencies

- Phase A merged develop (9479cb2) ✓ — 4 model + project_namespace_hash 可用
- Phase B image source merged (paas-agent-runtime PR #1) ✓ — image contract（`ODOO_BASE_URL`/`SKILL_BUNDLE_TOKEN`/`PROJECT_HASH` env + 15-type envelope + HS256 dual-secret + mcp.json ConfigMap）已凍結，Phase C 對齊
- 依賴序：① paas-operator → ② skill endpoint（cache.py 假設）→ ③ client/webhook → ④ SSE gateway（最後 wire，flag gate）

### Estimated effort

**1.5-2 週**（scope 含 SSE rewrite + skill bundle endpoint + 兩種 token；比原估 1 週擴）

### Risk

| Risk | Mitigation |
|------|-----------|
| paas-operator router 加新 endpoint 撞既有 namespace_prefix check | 確認 `paas-ws-pa-{hash}` 滿足 `paas-ws-` prefix（design §2.7 已確認） |
| JWT 簽 / 驗 mismatch（algo / claim shape）| Unit test cover full sign+verify roundtrip（兩種 token scope） |
| Webhook HMAC secret 未配置 | Phase C dev 手動 kubectl secret；helm chart 必填（Phase G）；paas-operator 啟動 fail loud |
| **SKILL_BUNDLE_TOKEN resume 後過期** → cache miss fetch 401 | ⚠ design clarification（見 escalation）— spawn-or-attach/resume 每次重簽 token + patch pod env；cache hit（PVC 保留）不受影響 |
| SSE rewrite merge 影響 production chat | `use_agent_runtime` flag 預設 False → flag gate；False 走既有路徑零變動 |
| skill bundle endpoint auth='none' 被濫用 | token authoritative（HC-2 ruling）+ HS256 verify + project_access check；query param 不信 |

### Rollback

- paas-operator router 改 410 Gone
- Odoo client / webhook / skill-bundle endpoint 留 dormant（無 caller — SSE flag False）
- SSE rewrite：flag False 即等同未改（既有路徑接手）

---

## Phase D — Enable-gate code-ready（must-fix close + admin 啟用面 + cross-repo wiring）

> **[範圍重定義 2026-05-24]** Phase A+B+C 主體已 ship（PR #59 merged → develop）。本 session = **code-ready**：把 flip `use_agent_runtime` flag 的**所有 CODE 前提**全 close。**不 flip flag、不起 pod、不部署、不做 2 週觀察** —— 那塊由使用者依下方 [§Pilot runbook](#pilot-runbook使用者自行執行) 自行執行。原「pilot + dual-write + 2 週觀察」內容已下沉成 runbook。

### Goal

1. enable-gate **must-fix（F-PC-1/2/4/5/6/7）全 close** —— flip 後 runtime 不炸、error 通道正常、跨租戶 token 原語封死、無 WS 洩漏。
2. **admin 啟用面 code 就緒** —— `use_agent_runtime` toggle UI + agent_template seed（不實際 bind）。
3. **cross-repo wiring 對齊** —— paas-agent-runtime verifier A 嚴格收緊。（Odoo K8s Secret env mount 落**本 repo** `charts/odoo-dev-sandbox`，屬 WS1，見 charts 條）

### Scope boundary（本 session **不**做）

- ❌ 不設 `use_agent_runtime=True`（不 flip）、不起 pod、不部署 cluster、不做 2 週 SLO 觀察。
- ❌ paas-operator **僅 F-PC-4** 准動既有 middleware（security hardening）；其餘對 operator 一律 router-additive。
- ❌ 相鄰模組 / 架構擴張（F-PA-14 以外的 bootstrap 重構、worker pool sizing impl…）→ 先回 leader，不擅擴 scope。
- ✅ secret 來源 = **K8s Secret env**（OQ#1 ruling (b)）；不新增 ir.config_parameter secret。

---

### Decisions（已拍板 2026-05-24，使用者全採 planner 推薦）

**D-1 · F-PC-4 = Global fail-closed.** operator `api_key` 未設 → **所有 router** 503（不只 agent-runtime router）。理由：operator fail-open 使其成 token 簽發者時可為任意 project_id 簽 scoped `skill_bundle_token`（跨租戶 bundle 外洩原語，follow-ups F-PC-4），且 fail-open 對 releases/namespaces/tunnels 等所有 router 都是未認證 K8s 操作 footgun。**已接受 blast radius**：~6 支既有 test fixture（`test_api.py` / `test_tunnels.py` / `test_scale.py` / `test_agent_runtimes.py` / `test_odoo_module.py` 的 `api_key=""`）+ integration `conftest.py:102 app_client`（`API_KEY=""`）全改設真 key 或改斷言 503；keyless dev/stg operator 終結。

**D-2 · F-PC-3 = K8s Secret env (b).** `agent_session` namespace secret 改讀 K8s Secret env，殺 `_DEFAULT_SECRET` 公開 fallback + 移除 ir.config_parameter read，缺 secret → **fail-closed**。與 OQ#1 constraint + 已 ship 的 skill-bundle/webhook env pattern 一致。**design §2.15 待 follow-up 改寫**（本 plan 不改 design，另開條目）。

**D-3 · F-PA-14 = 本次納入（最低優先）.** `project.sudo().create()` 跳 owner bootstrap fix；fix 須 care HC-2 family bootstrap 語義（[[constraint-input-trust-expansion]]），方向：`create` 接受顯式 owner-partner（context hint）讓 sudo path 也能 bootstrap，不破「SUPERUSER 不自動成 owner」原意。

### Rejected options（reporter 持久化用）

- **D-1 (ii) scoped fail-closed**（僅 agent-runtime router 503）—— 否決：blast radius 雖小，但保留其餘 router fail-open footgun，非乾淨終態。
- **D-2 (a) ir.config_parameter auto-gen**（design §2.15 原案）—— 否決：self-contained 但保留第二套 secret 機制（與 env 模式分歧）+ secret 落 Odoo DB。改採 env single-source。
- **D-3 剔除 F-PA-14 留獨立 cycle** —— 否決：cheap correctness + 趁在 area，一併納入。

### 軟契約 env 名（**凍結** · 本 repo `charts/odoo-dev-sandbox` 照此 mount）

Odoo deployment（`charts/odoo-dev-sandbox/templates/odoo-deployment.yaml`，WS1 charts 條）須注入的 K8s Secret env（**production Odoo CD `woow-paas-cd` 留 Phase G**）：

| env 變數 | 用途 | 狀態 | 消費端 |
|----------|------|------|--------|
| `AGENT_RUNTIME_AUTH_SECRET_CURRENT` | skill-bundle token 驗（HS256 current）| 既有（Phase C）| `agent_runtime_skill.py:33` |
| `AGENT_RUNTIME_AUTH_SECRET_PREVIOUS` | skill-bundle token 驗（rotation previous）| 既有（Phase C）| `agent_runtime_skill.py:34` |
| `AGENT_RUNTIME_WEBHOOK_SECRET` | webhook HMAC 驗（current）| 既有（Phase C，裸名）| `agent_runtime_webhook.py:27` |
| **`AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS`** | webhook HMAC 驗（previous，F-PC-2 新增）| **本 session 新增** | F-PC-2 webhook verify |
| **`AGENT_RUNTIME_NAMESPACE_SECRET`** | namespace hash HMAC（F-PC-3 (b)）| **本 session 新增** | F-PC-3 `agent_session` |

> **凍結理由**：
> - webhook **current 維持既有裸名** `AGENT_RUNTIME_WEBHOOK_SECRET`（不改 Phase C 已 ship 的讀取點），previous 用 `_PREVIOUS` 後綴 —— 刻意不對稱於 auth 的 `_CURRENT/_PREVIOUS`，換取向後相容。
> - namespace secret **刻意單一、不 dual-secret / 不 rotation**：一旦輪替，同 project 算出不同 hash → `paas-ws-pa-{hash}` namespace 漂移 → 既有 pod 變孤兒。故 namespace secret 必須穩定，與 auth/webhook（可輪替）的設計差異是**有意的**；缺即 fail-closed（flag-gated，未 spawn 不需 secret）。

---

### Workstream 1 — woow_paas_platform 本 repo（TDD，tester 可直接接）

> 涵蓋 must-fix（F-PC-1/2/4/5/6）+ 本次納入（F-PC-3 = D-2、F-PA-14 = D-3）+ charts env mount（本 repo `charts/odoo-dev-sandbox`，落點修正 2026-05-24）。每條給「先紅的斷言入口」+「fix 方向（不含 impl 細節）」；實際 test case 由 tester 展開（planner 不代寫）。

| # | caveat | 位置 | red test 入口（先紅斷言）| fix 方向 |
|---|--------|------|----------------------------|----------|
| **F-PC-1** | A-1 websockets kwarg | `ai_assistant.py:648-650`（`websockets.connect(... extra_headers=...)`）+ `requirements.txt:10` | 斷言 `requirements.txt` 的 `websockets` 釘**有上界 `<14`**（解析/regex test）；websockets ≥14 移除 `extra_headers` 改名 `additional_headers` | 釘 `websockets>=12.0,<14`；（選）connect 端依版本擇 kwarg（design §5.6 `_hdr_kw` 相容寫法）。**實機 WS connect 驗 → pilot gate**（無 pod 無法單測） |
| **F-PC-2** | C-3 webhook dual-secret | `agent_runtime_webhook.py:30-37,62` | webhook 收 **previous-secret 簽** 的 body → 現 **401（紅）** → 修後 200；current 仍 200；皆非 → 401 | `_verify_hmac` 改 try current → previous fallback，**複用 `agent_runtime_skill.py:62-80 _verify_skill_bundle_token` 的 dual-env pattern**；新增 env **`AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS`**（凍結名，→ WS2 charts mount） |
| **F-PC-4** | C-2 operator fail-open | `extra/paas-operator/src/main.py:89-91` | operator 單測：`api_key=""` 打**任一**受保護 route → 現 **pass-through（紅，應 503）** → 修後 503 | `api_key` 未設 → **global fail-closed 503（所有 router，D-1 定案）**；**同 PR 一併更新** ~6 支既有 fixture（`test_api.py`/`test_tunnels.py`/`test_scale.py`/`test_agent_runtimes.py`/`test_odoo_module.py` 設真 key 或斷言 503）+ integration `conftest.py:102 app_client` |
| **F-PC-5** | N-1 client raise_for_status | `agent_runtime_client.py:25-33`（`_request`）| mock operator 回 401/500 → 現 `_request` 回錯誤 dict / 拋 `JSONDecodeError`（紅）→ 修後拋 **typed error 帶 operator `detail`** | `_request` 加 status 檢查，4xx/5xx 拋自訂 typed exception 攜 detail；SSE 端（`:620,:721`）已有 except 接 → 確認 surface 真因非 cryptic |
| **F-PC-6** | P-2 async-gen aclose | `ai_assistant.py:678-729`（drain loop `finally` 只 `loop.close()`）| mock relay async-gen 記錄 `aclose()` 是否被 await；跑 bridge **done / error / 例外** 三路徑 → 現皆未 aclose（紅）→ 修後三路徑都先 aclose | drain loop `finally` 在 `loop.close()` 前補 `loop.run_until_complete(agen.aclose())` |
| **F-PC-3** (D-2) | C-5 namespace secret env | `agent_session.py:11,113-127`（`_DEFAULT_SECRET` + `_get_namespace_secret` + `create`）| env 設好 → hash 由 env secret 算（非 `_DEFAULT_SECRET`）；env **未設** → `create` **fail-closed raise**（現用公開 fallback silent 算 hash = 紅）| `_get_namespace_secret` 改讀 env **`AGENT_RUNTIME_NAMESPACE_SECRET`**（凍結名），缺 → raise；移除 `_DEFAULT_SECRET` + ir.config_parameter read；`src/hooks.py` `_CONFIG_PARAMETERS` 若列 `...agent_namespace_secret` 須移除 |
| **F-PA-14** (D-3) | HC-2 bootstrap bypass | `project_project.py:114-139`（create bootstrap）+ 觸發的 controller sudo-create 呼叫點 | `project.sudo().create({...})` 帶 owner-partner hint → 現跳 bootstrap、creator 無 owner row（紅）→ 修後該 partner 得 owner `project_access` | create 接受顯式 owner-partner（context hint，如 `bootstrap_owner_partner_id`）使 sudo path 也 bootstrap；不破「SUPERUSER 不自動成 owner」原意；care HC-2 [[constraint-input-trust-expansion]] |
| **charts** | secret env mount（**本 repo** chart，落點修正 2026-05-24）| `charts/odoo-dev-sandbox/templates/odoo-deployment.yaml` + 新 `charts/odoo-dev-sandbox/tests/test_agent_runtime_env.sh` | `helm template` render 的 odoo-deployment **缺凍結 5 env** → 紅（**bash 斷言，無 helm-unittest**）| odoo-deployment 注入凍結 5 env（既有 `env:`/`secretKeyRef:` pattern 可循，`:62`+；secret 來源 `templates/secrets.yaml`）；新建 `tests/test_agent_runtime_env.sh`（bash + `helm template`）。**production Odoo CD（`woow-paas-cd`）secret env → Phase G**（本次只動 dev/stg `odoo-dev-sandbox`）|

### Workstream 2 — cross-repo（paas-agent-runtime，需該 repo agent）

> **[落點修正 2026-05-24]** 原 charts 列已移 **WS1** —— Odoo deployment chart 在**本 repo** `charts/odoo-dev-sandbox`（非 `woow-paas-charts` repo，後者只有 agent-runtime pod chart）。WS2 cross-repo 僅剩 F-PC-7。

| # | repo / 位置 | red test 入口 | fix 方向 | 前置 |
|---|------------|--------------|----------|------|
| **F-PC-7** | `paas-agent-runtime` `runtime/transport/auth.py verify_token`（B transitional，commit `18d9602`）| token **缺 `token_use` claim** → 現放行（紅，A 嚴格應拒）→ 修後拋 `TokenUseError` | verifier 由 B 過渡（缺 claim 放行）切 **A 嚴格**（缺 → 拒），對稱於 Odoo skill-bundle 端（`agent_runtime_skill.py:105` 已 A 嚴格）| **已滿足**：operator 全簽 `token_use`（Phase C merged）→ 可切。deploy 順序見 runbook |

### Workstream 3 — admin 啟用面（code only，不實際 bind / 不 flip）

1. **admin UI toggle** — `src/views/project_views.xml`：project form 加 `use_agent_runtime` 顯示（field 已在 `project_project.py:63` Phase A ship，view 未顯示）。red test：view arch 含 `use_agent_runtime`（或沿用既有 field-存在斷言 + 加 arch smoke）。**只加此 toggle，不順手動 `external_mcp_allowed` 等相鄰 field（超 scope）**。
2. **agent_template seed** — 新 `src/data/agent_template_demo.xml`：seed 一個 **unpublished demo** agent_template，**不 bind 真 project**（leader：code 準備不實際 bind）+ 掛進 `__manifest__.py` data list。red test：demo record 安裝後存在。

---

### Task sequence + dependency graph

```
（前置 · 已完成）operator 全簽 token_use（Phase C merged）
        │
        ▼
[WS1 本 repo]  F-PC-1 / F-PC-4 / F-PC-5 / F-PC-6 / F-PA-14  ── 互相獨立，可平行
               F-PC-2 ───────┐
               F-PC-3 ───────┤（env 名已凍結）
               charts  ──────┤（odoo-dev-sandbox 照凍結 5 env 名 mount，本 repo）
[WS3 admin]  UI / seed ───────┤（additive，平行）
                              ▼
                       （WS1/WS3 全平行，無互鎖）

[WS2 F-PC-7]  paas-agent-runtime A 嚴格 ── 前置已滿足，與上述全平行（獨立 repo）
```

- **無硬 CODE 阻塞**：全部可平行 —— charts（本 repo `odoo-dev-sandbox`）照凍結 env 名 mount，不需等其餘 code 完成；F-PC-7 獨立 repo。
- **DEPLOYMENT 順序**（非 code 順序，進 runbook）：charts secret 先於 **dev/stg** Odoo redeploy；operator(簽 token_use) 先於 pod(A 嚴格) deploy。

### Test scope（給 tester）

- **既有 regression（必綠）**：`use_agent_runtime=False`（default）行為零變動 —— SSE 走既有 path（`test_agent_pod_phase_c_sse_gateway.py` SSE.1/SSE.5）。operator 既有 fixture 因 F-PC-4 global fail-closed 須隨改（已納 F-PC-4 同 PR，見 WS1）。
- **新行為（紅→綠）**：上方 WS1 + WS2 每列的 red test 入口。
- **刻意不測（pilot gate，無 pod 無法單測）**：F-PC-1 實機 WS connect（websockets 版本對）/ spawn-or-attach 真起 pod 端到端 / SLO metric —— 全列入 runbook flip 前 CAQA/reviewer re-verify，不在本 session 單測。

### Tester hand-off — red test 入口 checklist（單一入口）

> TDD：每項先寫紅 test → 實作 → 綠。詳細位置 / fix 方向見上方 WS1/WS2/WS3 表。標 ⚡ 者可平行起手（無硬 code 阻塞）。

| 項 | repo | red test 一句話入口 | WS |
|----|------|---------------------|----|
| ⚡ F-PC-1 | woow_paas_platform | `requirements.txt` websockets 無 `<14` 上界 → 紅 | WS1 |
| ⚡ F-PC-2 | woow_paas_platform | previous-secret 簽的 webhook body 得 401 → 紅 | WS1 |
| ⚡ F-PC-4 | paas-operator（本 repo `extra/`）| `api_key=""` 打受保護 route 仍 pass-through → 紅（+ 同 PR 改 ~6 fixture）| WS1 |
| ⚡ F-PC-5 | woow_paas_platform | operator 回 401/500 時 `_request` 不拋 typed error → 紅 | WS1 |
| ⚡ F-PC-6 | woow_paas_platform | bridge done/error/例外 三路徑 relay 未 aclose → 紅 | WS1 |
| ⚡ F-PC-3 | woow_paas_platform | namespace secret env 未設時 `create` 不 fail-closed（用公開 fallback）→ 紅 | WS1 |
| ⚡ F-PA-14 | woow_paas_platform | `sudo().create()` 帶 owner hint 仍跳 bootstrap → 紅 | WS1 |
| ⚡ admin UI | woow_paas_platform | project form view arch 不含 `use_agent_runtime` → 紅 | WS3 |
| ⚡ seed | woow_paas_platform | demo agent_template record 安裝後不存在 → 紅 | WS3 |
| ⚡ F-PC-7 | paas-agent-runtime | 缺 `token_use` claim 的 token 仍放行 → 紅 | WS2 |
| ⚡ charts | 本 repo `charts/odoo-dev-sandbox` | `helm template` odoo-deployment 缺凍結 5 env → 紅（bash 斷言，無 helm-unittest）| WS1 |

### Estimated effort

**code-ready**（不含 pilot 觀察期）。WS1 八項（F-PC-1/2/4/5/6 + F-PC-3 + F-PA-14 + charts；F-PC-4 落 `extra/paas-operator/`、charts 落 `charts/odoo-dev-sandbox/`，其餘 woow_paas_platform）+ WS3 兩項屬本 repo；WS2 僅 F-PC-7 一項跨 repo（paas-agent-runtime）。

---

### Pilot runbook（使用者自行執行）

> code-ready 完成 + flip gate 驗收通過後，由**使用者**執行；本 session 不代跑。
>
> **▶ 可執行 step-by-step checklist（動作 + 指令 + 驗收 + rollback）**：`docs/guides/deployment/agent-runtime-pilot-flip-gate-runbook.md`。下方為高層摘要 + caveat 論述源（runbook 引用此處、不複製）。

1. **前置**：cluster 可達（`kubectl config use-context <dev/stg>`）；K8s Secret 建好（凍結 5 env：`AGENT_RUNTIME_AUTH_SECRET_CURRENT`/`_PREVIOUS` + `AGENT_RUNTIME_WEBHOOK_SECRET`/`_PREVIOUS` + `AGENT_RUNTIME_NAMESPACE_SECRET`）；helm 部署 **dev/stg `odoo-dev-sandbox`**（已含 WS1 charts env mount）+ operator（全簽 `token_use`）+ paas-agent-runtime image（A 嚴格）。**production Odoo（`woow-paas-cd`）secret env mount 留 Phase G**。
2. **deploy 順序**：charts secret → Odoo redeploy（吃新 env）；operator(簽 token_use) **先於** pod(A 嚴格) deploy（否則在途舊 token 被 A 嚴格拒）。
3. **flip**：admin UI 對 **1 個 admin-controlled** pilot project 設 `use_agent_runtime=True`。
4. **端到端驗**：spawn-or-attach 真起 pod + WS relay + skill bundle fetch 端到端通。
5. **觀察 ≥ 2 週**：cold start p95 < 15s / WS error rate < 1% / worker pool util < 50%（撞 0.8 util 告警；p95 持續 > 15s → 暫停 advance）。
6. **rollback**：`use_agent_runtime=False`（admin UI 一鍵）→ pod 自然 reap（30 min idle）→ 既有 `ai_client.py` 路徑接手。

### ⚠ Flip-gate caveats（部署 invariant，code 不強制 — CAQA/reviewer Phase D D-CAVEAT-1/2）

這 2 條**不是 code 缺陷**（11 must-fix 全 close），而是「部署 / cross-repo ops invariant 未被 code 強制」的風險，flag-gated 故不阻 merge，但 **flip 前必照做**。

**D-CAVEAT-1 — namespace secret 須跨 3 進程同值（+ charts `optional:true` 放大 partial 部署風險）**
- 機制：F-PC-3 把 namespace secret 從公開 fallback 改 K8s Secret env `AGENT_RUNTIME_NAMESPACE_SECRET`（缺即 fail-closed）。failure mode 隨之從「靜默用公開 default」變成「**Odoo web worker / cron worker / operator 三進程中任一拿到不同或空值 → namespace hash 靜默分歧**」→ webhook 永遠 404、或同 project 在不同 worker 算出不同 `paas-ws-pa-{hash}` namespace。
- 放大點：`charts/odoo-dev-sandbox` 的 secretKeyRef 用 `optional: true` → **partial 部署（secret 只掛到部分進程）不會在 deploy 時炸**，會靜默分歧到首次使用才現形。
- **做**：單一 K8s Secret **同值**掛入**全部** Odoo pod（web + cron）+ operator；考慮在 hash 消費點加 startup/health 一致性斷言（把「靜默分歧」前移成「啟動即報」）。此 secret 刻意**不可輪替**（輪替 → hash 漂移 → 孤兒 pod，見 design §2.15）。

**D-CAVEAT-2 — token_use deploy-order + 1h 在途窗口 + operator 反向 rollback 危害**
- 約束：operator 簽 `token_use` 必 **100% 先於** pod 切 A 嚴格（否則 A 嚴格 pod 拒缺 claim 的在途 token）。正常 forward path 已關閉（operator 自 Phase C 已簽 `token_use`），**但 code 無 gate 在 ordering 違反時拒啟動**。
- 在途窗口：`ws_token` exp 3600s → pre-token_use operator 簽的 token 有**最長 1h** 被 A 嚴格 pod 誤殺的窗口。
- **反向 rollback 不安全且未記載**：operator 降回 token_use baseline 以下（不再簽 claim）、而 pod 仍 A 嚴格 → pod 拒所有新 token。**rollback operator 前必先把 pod verifier 退回 B transitional**。
- **做**：deploy 序固定 **operator（簽 token_use）→ pod（A 嚴格）**；operator `/health` 暴露 `signs token_use: true` 供下方 flip-gate checklist **機器可驗**（把 F-PC-7 的人工「確認後」轉成可檢查訊號）；rollback 反向操作。

### Flip gate 驗收（flip 前必過）

WS1+WS2 全 close 後、**第一次真 flip / spawn-or-attach 起 pod 前**，CAQA / reviewer 再 verify 一輪，特別：

- **F-PC-1** 實機 WS connect（websockets 版本對，connect 不 `TypeError`）
- **F-PC-5 + F-PC-6** error 通道 + WS lifecycle（operator error 正確 surface 真因 + 每條對話無 WS 洩漏至 ping_timeout）
- **F-PC-7** A 嚴格切換（operator prod 全簽 `token_use` 確認後）
- **F-PC-3** fresh install secret 非公開值 / 缺 `AGENT_RUNTIME_NAMESPACE_SECRET` 時 fail-closed

對應 `docs/tracking/followups/follow-ups.md` §Phase D enablement gate（must-fix / follow-up 兩層）。

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
   - **production Odoo deployment secret env mount**（將 Phase D 在 dev/stg `odoo-dev-sandbox` 已做的凍結 5 env 對齊到 production Odoo CD：`AGENT_RUNTIME_AUTH_SECRET_CURRENT`/`_PREVIOUS` + `AGENT_RUNTIME_WEBHOOK_SECRET`/`_PREVIOUS` + `AGENT_RUNTIME_NAMESPACE_SECRET`）—— 缺 `AGENT_RUNTIME_NAMESPACE_SECRET` 時 `agent_session` fail-closed（Phase D F-PC-3 (b)），故 prod 上線前此 env 必到位
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
