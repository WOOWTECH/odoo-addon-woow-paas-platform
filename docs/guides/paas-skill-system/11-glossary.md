---
title: PaaS Skill 術語表
category: guides
status: active
created: 2026-05-25
updated: 2026-06-09
tags: [paas, skill-system, glossary, reference]
related:
  - README.md
  - 02-conversation-streaming.md
  - 06-pod-internals.md
---

# PaaS Skill 術語表

> 快速查詢 PaaS Skill 系統的專有名詞。依英文／技術詞排序。

## 🔖 內部代號速查

> 文件裡會出現 `F-PC-7`、`HC-2`、`SC-5` 這類代號——它們是開發過程的 finding / caveat 編號（`F-…` 追蹤項、`SC-…` spec 缺陷、`D-CAVEAT-…` Phase D 注意事項）。**新人不用背，看到時來這查即可。**

| 代號 | 意思 |
|---|---|
| **C-1** | `token_use` 跨 scope 防禦：ws_token 不可拿去打 `/skill-bundle`，否則 403 |
| **HC-2** | 權威身份原則：以**已驗證的 JWT claims**（`project_id` / invoker 等）為準，忽略 URL 或 payload 的同名值 |
| **F-PC-6 / P-2** | WS 橋接的連線洩漏防護：對話結束要 `aclose()` 收掉 WS，否則每輪漏一條 pod 連線 |
| **F-PC-7** | Pod 端 `token_use=='ws'` 嚴格驗證（缺或非 ws 即拒）。**翻 enable-gate 前須先 merge + 部署** |
| **F-PC-1~7** | Phase D 修補的一組 enable-gate caveat（本指南具體用到 F-PC-6、F-PC-7） |
| **F-PA-14** | owner-bootstrap context hint 僅 SUPERUSER 可讀，防 HTTP 注入冒充 owner |
| **F-SK-*** | skill-list cycle 6 的 finding 編號（is_latest drift fix + e2e）。F-SK-* 定義見 [`docs/tracking/followups/follow-ups.md`](../../tracking/followups/follow-ups.md) |
| **D-CAVEAT-1** | namespace secret **fail-closed**：未設值直接 `UserError`，不退化成弱 hash |
| **D-CAVEAT-2** | 跨 repo 部署順序風險：F-PC-7 未先部署 → 1h 在途 token 空窗 |
| **SC-4** | mcp / template reloader 缺 tool_set 失效 hook（改 snapshot 可能 silently 不生效） |
| **SC-5** | （已解）舊 langgraph stub 與 ws_server 都送 `done` 的雙 done；真 `stream()` 不送 done，由 ws_server 唯一合成 |
| **SC-7** | （舊 subprocess-tool 模型）skill tool 依 `(name, version, invoker_role)` 快取做 role 隔離；新模型 skill 不再是 per-skill tool |

## A

**AGENT_RUNTIME_AUTH_SECRET_CURRENT / _PREVIOUS**
簽／驗 `SKILL_BUNDLE_TOKEN` 與 `ws_token` 的 HS256 **雙 secret**（Odoo 與 operator 共用）。輪換時新 token 用 CURRENT 簽，在途舊 token 由 PREVIOUS 驗，達零停機。

**AGENT_RUNTIME_NAMESPACE_SECRET**
算 project namespace hash 的 HMAC secret。**fail-closed**：未設值時 Odoo 直接拋 `UserError`，不做 auto-gen fallback（D-CAVEAT-1，`agent_session.py:130-145`）。

**AGENT_RUNTIME_WEBHOOK_SECRET / _PREVIOUS**
驗 Pod→Odoo webhook 的 HMAC 雙 secret。注意 current 版是**裸名**（無 `_CURRENT` 後綴）。

**AGENT_RUNTIME_SKILL_RUNNER_ENABLED**
agent container 的 gate env（Drop B）：`true` 才暴露 `bash:execute` 並起 `bash_bridge` sidecar。由 operator 依 `skillRunner.enabled` 顯式設定，**預設 false（fail-closed）**。見 [skill-runner（bash sidecar）](12-skill-runner-bash-sidecar.md)。

**agent-runtime**
Pod 內負責載入、執行 Skills 與跑對話 agent loop 的 runtime 程序。程式碼在 `woow-paas-agent-runtime` repo。

**agent_template**
每個 project 綁定的 agent 範本（`woow_paas_platform.agent_template`），含 `mcp_server_ids`、skills、`state`。對話時 `_resolve_agent_template` 優先取 `state=published` 的範本；project 無自綁時 fallback 到平台預設（Settings 的 `default_agent_template_id`；module 內建 seed `agent_template_default`，public+published+unbound）。見 [Agent 對話與串流](02-conversation-streaming.md)。

**AgentSession**
Odoo model，記錄一個 project 對應的 Pod 狀態（namespace hash、state、`pod_ws_url`、`last_synced_at`）。

**AIClient**
Odoo 內封裝 LangChain `ChatOpenAI` 的 client（`src/models/ai_client.py`），是 **legacy 路徑**的 LLM 執行端。見 [Agent 對話與串流](02-conversation-streaming.md)。

**API_KEY**
paas-operator 的認證金鑰。Odoo 呼叫 operator 時透過 `X-API-Key` header 傳遞。

**audit_logger**
Pod 內把稽核事件以一行 JSON 印到 **stdout**（K8s log 收集）。必填 `ts`/`level`/`event`/`project_hash`，15 種 event 凍結；對 mail body 做 **PII redaction**（`security/audit_logger.py`）。

## B

**bash:execute**
Claude Code 模型的通用 shell tool（`bash:execute(cmd, timeout_seconds?)`）：full shell、可 `pip install`，cwd 預設 `/workspace`，回 `{stdout, stderr, exit_code, truncated}`。在異 UID 的 `bash_bridge` sidecar 內執行。

**bash_bridge / skill-runner**
agent pod 內的 sidecar container（agent-runtime image ≥ `1.4.0`），用異於 agent 的 UID（`runAsUser=10002`）+ PID ns 隔離跑 `bash:execute` 的指令。見 [skill-runner（bash sidecar）](12-skill-runner-bash-sidecar.md)。

## C

**Claude Code 模型**
skill 執行模型（2026-06-09 STG 上線、prod 待 flip）：skill = SKILL.md 指令 + 選配檔案；agent 用 `skill:load_skill` 取回指令、用 `bash:execute` 自行執行，取代「每個 skill = 固定 entrypoint 的 StructuredTool」的舊 subprocess-tool 模型。

## D

**dual-write**
Phase E 策略：新舊路徑同時寫、觀察一致性後才正式切換。見 [Agent Runtime 演進史 (Phase A~G)](10-roadmap-phases.md)。

## E

**enable-gate**
控制整套 agent-runtime 是否對使用者開啟的開關。Phase D 完成時仍**關閉**。

**envelope**
Odoo↔Pod WebSocket 的訊息格式，含 `version` / `type` / `request_id` / `sequence_number` / `timestamp` / `payload`。去程 `user_message`，回程 `text_chunk` / `tool_*` / `status` / `done` / `error`（共 15 種 type 凍結）。見 [agent-runtime Pod 內部](06-pod-internals.md)。

## H

**Helm**：K8s 套件管理器。operator 用它 install/upgrade/uninstall agent-runtime。
**HMAC**：Hash-based Message Authentication Code。用於 namespace hash 與 webhook 簽章。

## I

**is_latest**：Skill 欄位，標記同一 `name` 的最新版本（Boolean, indexed）。

## J

**JWT**：JSON Web Token。`SKILL_BUNDLE_TOKEN` 與 `ws_token` 都是 HS256 JWT。
**jti**：JWT ID，防 replay。**兩端各有一個 in-memory cache**：operator 簽 `SKILL_BUNDLE_TOKEN`（`jwt_signer.py` `_JTI_SEEN`，65 分），與 Pod 驗 `ws_token`（`security/jti_cache.py`，`TTLCache(10000, 65min)`，per-pod、重啟重置）。

## L

**langchain / langgraph**：LLM 對話與 tool calling 框架。**legacy** 路徑跑在 Odoo（`ai_client.py`：`ChatOpenAI` + `langchain-mcp-adapters` + `langgraph`）；**agent-runtime** 路徑在 Pod（`runtime/langgraph_runtime.py`，同樣 stack；agent loop 已上線，skill 執行為 Claude Code 模型 `load_skill` + `bash:execute`）。見 [Agent 對話與串流](02-conversation-streaming.md) · [agent-runtime Pod 內部](06-pod-internals.md)。

**load_skill（skill:load_skill）**：Claude Code 模型的 tool，agent 判斷要用某 skill 時呼叫 → `ensure_skill_cached` lazy fetch+extract → 回 `{name, description, body, workdir, files[]}` 供 agent 依指令用 `bash:execute` 執行。

## M

**mcp_snapshot**：Odoo 傳給 Pod 的 MCP server 設定快照 `{servers:{name:{url, transport}}}`（`_build_mcp_snapshot`）。

## N

**Namespace (K8s)**：隔離單位，命名 `paas-ws-pa-{hash}`。
**NetworkPolicy**：控制 Pod 網路出口的白名單，限定可訪問哪些 workspace。

## O

**Odoo Base URL**：Odoo 實例 URL，注入 Pod（`ODOO_BASE_URL`），供 Pod 回呼與下載 bundle。

## P

**paas-operator**：FastAPI 服務，Odoo 與 K8s 之間的中介層。
**Pod (agent-runtime)**：K8s 容器；一個 project 對應一個 Pod。
**project hash / project_namespace_hash**：`base32( HMAC-SHA256(project_id, secret)[:10] ).lower()` → **16 字元小寫**（字元集 `a-z2-7`）。**不是 8 字元 hex**（`agent_session.py:22-31`）。
**project_id**：Odoo `project.project` 的 ID；JWT 中的權威身份（HC-2）。

## R

**Reap**：把 Pod scale 到 0，省資源、保留 PVC 快取。
**reloader / 熱重載**：Pod 內監看 ConfigMap 掛載目錄 `/etc/agent-runtime/`，把 Odoo 推來的 `skills.json` / `mcp.json` / `template.json` 原子換進 in-memory snapshot，免重啟。觸發：WS push 為主、inotify 目錄事件為輔、啟動讀一次。見 [agent-runtime Pod 內部](06-pod-internals.md)。
**Resume**：把 Pod 從 0 喚醒到 1；會先重簽 token、patch env 再 scale。

## S

**Skill**：最小能力單位——SKILL.md 指令 + 選配 bundle 檔案（Claude Code 模型；舊版才是「+ 固定 entrypoint 程式」）。
**SKILL_BUNDLE_TOKEN**：HS256 JWT，Pod 用它向 Odoo 驗證身份並下載 bundle，預設效期 7 天（issue #155），`token_use=skill_bundle`。
**Skill Bundle**：Skill 的封包格式（tar.gz），存於 `ir.attachment`；tar root 直接含 `SKILL.md`，不可包 wrapper dir。
**skill-roster**：progressive disclosure 機制——啟動時把 effective skills 的 `name + description` 組成清單注入 system_context，全文等 agent 呼 `load_skill` 才載入（cap 約 50 條 / 4KB）。
**skill_snapshot**：Odoo 傳給 Pod 的已發布 skills 清單（`name`/`version`/`sha`/`description`，`_build_skill_snapshot`）。
**spawn_or_attach**：核心冪等入口，依 helm release 狀態分 cold start / resume / attach。
**SSE (Server-Sent Events)**：對話串流回瀏覽器的協定，端點 `GET /api/ai/stream/{channel_id}`，`content_type=text/event-stream`。見 [Agent 對話與串流](02-conversation-streaming.md)。
**StructuredTool**：langchain 的 tool 包裝。**舊 subprocess-tool 模型**才把每個 skill 包成 `StructuredTool`；Claude Code 模型已改用通用 `skill:load_skill` + `bash:execute`，skill 不再各自是 tool。
**subprocess isolation**：Pod 執行 skill bash 的隔離方式（`subprocess_runner.py` 的 `_safe_env` / `_set_rlimits` / `_capped_*`）：硬 rlimit（CPU / AS / NOFILE / NPROC / FSIZE）、wall timeout、**env 白名單**（只放 PATH/HOME/LANG/PROJECT_HASH，不漏任何 secret）、stdout 截斷。Drop B 起在**異 UID（`runAsUser=10002`）的 `bash_bridge` sidecar** 內執行（PID ns 隔離，見 skill-runner）。
**sync-workspaces**：helm upgrade 重渲染 NetworkPolicy egress，不重啟 Pod。

## T

**token_use**：JWT claim，標明用途（`skill_bundle` / `ws`）；跨 scope 防禦的核心（C-1 / F-PC-7）。

## U

**url_validator (SSRF guard)**：Pod 建 MCP client 前再驗一次 URL（defense in depth）：拒 non-http(s)、link-local（`169.254/16`、`fe80::/10`）、雲端 IMDS、進位繞過；放行 `*.svc.cluster.local`（`security/url_validator.py`）。
**use_agent_runtime**：`project.project` 旗標，決定對話走 **legacy**（Odoo 跑 LLM）或 **agent-runtime**（Pod 跑）。預設 `False`，Phase E 對 pilot project 開。見 [Agent 對話與串流](02-conversation-streaming.md)。

## V

**/v1/chat**：Pod 的 WebSocket endpoint（port 8000）。Odoo 的 `_stream_agent_runtime` 連到這裡、送 `user_message` envelope。關閉碼：`4401` auth 失敗 / `4403` project_hash 不符 / `4400` protocol violation。見 [agent-runtime Pod 內部](06-pod-internals.md)。

## W

**webhook**：Pod 向 Odoo 回報狀態的機制，HMAC-SHA256 簽章（`X-PaaS-Signature: sha256=...`）。
**ws_token**：per-invocation JWT，用於 Odoo↔Pod 的 WebSocket 認證，`token_use=ws`（`spawn_or_attach` 回傳時叫 `auth_token`）。
**workspace_ids**：Skill bundle token 內的 claim，Pod 的 NetworkPolicy egress 白名單。

## X

**X-PaaS-Signature**：webhook 的 HMAC 簽章 header，格式 `sha256=<hex>`。

## 🔗 相關文件

- [PaaS Skill 系統總覽](README.md)
- [Agent 對話與串流](02-conversation-streaming.md) · [agent-runtime Pod 內部](06-pod-internals.md)
- [Skill Bundle 與 JWT 認證](04-skill-bundle-jwt.md)
- [Agent Runtime Pod 部署機制](07-deployment.md)
- [Skill 系統安全模型](09-security-model.md)
- [Webhook 回呼與 ws_token](08-webhook-ws-token.md)
