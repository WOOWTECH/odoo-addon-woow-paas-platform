# Pitfall: agent-runtime cold-start spawn ~36s 撞 client timeout —— 根因是 operator CPU limit 250m throttle（**不是**同步等 pod-ready）

> 來源：stg verify session 2026-06-02。全新 project（無既有 helm release）的第一則 AI 訊息 → `spawn_or_attach` 在 Odoo client 端 30s 後 `requests.ReadTimeout` →（修前）SSE 回 `spawn_failed`。最容易誤判成「operator `/spawn-or-attach` 同步等 pod-ready 才回」。逐層 live 排查後確認 **operator 並不等 ready**；真因是 cold-start path 連續 fork ~5 個 helm/kubectl subprocess，每個被 operator pod 的 **CPU limit `250m`** throttle 到 5–17s、累加 ~36s，超過 client `_TIMEOUT=30`。
>
> 與相鄰 pitfall 區分：[[agent-runtime-stg-enablement-gap-chain]] 是「**從沒啟用過**」的 5 層 fail-closed gap；[[agent-runtime-llm-key-after-coldstart-stuck-echo]] 是「啟用全到位但 pod keyless → 永久 echo」。**本篇相反**：啟用全到位、功能完全正常（LLM 回應、state-sync、skill 都通），純粹是 **cold-start 太慢撞 timeout**（latency / perf 類，非 fail-closed、非缺 key）。

## Symptom

- **全新 project**（namespace/release 不存在）的第一則訊息 → 修前 client `_TIMEOUT=30` < operator cold-start ~36s → `ReadTimeout` → SSE `spawn_failed`。
- **attach 既有 active pod 很快**（~5s，`spawn_age_ms≈4861`）——只有 cold-start 慢，所以問題只在「新 project 第一次」。
- 重送 / retry 後就成功（pod 已起來 → 走 attach 路徑），呈現「首訊息報錯、再送即成」的 UX。
- 旁證：連單純 attach 的數次 `kubectl get` 也要 3–5s（同一個 throttle 拖慢所有 subprocess）。
- **（僅 odoo-shell 測試情境）** 在 shell 跑 spawn 撞 timeout → 未捕捉例外 → 交易 rollback → `agent_session` 沒 commit，但 operator 已用 deterministic hash 建了 pod → operator 回送 state webhook 因 Odoo 查無 session 而 **404** → 留下孤兒空 namespace。**正式 SSE 流程不會**（見下「正式 vs 測試」）。

## Why it happens — 不是等 ready，是 CPU throttle

**程式上 operator 不同步等 pod-ready**（`extra/paas-operator/src/api/agent_runtimes.py`）：

- `helm install` **無 `--wait`/`--atomic`**（`extra/paas-operator/src/services/helm.py:146-181`）→ 提交 manifest 即返回。
- `agent_runtimes.py:389` 註解寫「Readiness poll」，實際只是**單次非阻塞** `k8s_service.get_pods(...)` list。
- cold-start 完成 install 後直接 `pod_state="spawning"` 立即返回（`agent_runtimes.py:328`）。

**真因 = cold-start path 連續 subprocess × operator pod CPU limit 250m**。stg operator log 實測時序（cold-start hash `ym2xmihgms5zkqed`）：

| 步驟（cold-start path） | 對應碼 | 耗時 |
| --- | --- | --- |
| `kubectl apply`（`_ensure_namespace`） | `agent_runtimes.py:307` | ~0.6s |
| `kubectl apply`（`ensure_registry_secret`） | `:308` | ~5.6s |
| `kubectl apply`（`_provision_llm_secret`） | `:309` | ~6.5s |
| `helm install`（含 Gitea `--repo` chart fetch） | `:315` | ~17.4s |
| `get_pods`（line 389 單次 list） | `:390` | ~1s |
| **合計** | | **~36s** |

每個 helm/kubectl 都是 Go 二進位（API discovery + TLS），cold-start 瞬間 fork 多個 → 被 operator pod 的 `resources.limits.cpu: 250m` throttle。**佐證為 pod limit 而非節點競爭**：operator pod 平時 CPU 僅 ~11m，所在節點 CPU 用量 0–1%、餘裕極大。

**核心 mismatch**：Odoo client（修前）`_TIMEOUT=30`（`src/services/agent_runtime_client.py`）< operator cold-start wall time ~36s。

## 正式 SSE vs odoo-shell 測試（避免把測試孤兒誤判成 prod bug）

| | spawn timeout 後 | `agent_session` | 結果 |
| --- | --- | --- | --- |
| **正式 SSE**（`_stream_agent_runtime`，`ai_assistant.py:696`） | exception 被 `try/except` catch → 回 SSE `spawn_failed`，request 正常 return | Odoo **commit** cursor → session（`absent`）**持久化** | operator pod 起來 fire webhook → 找得到 session → 轉 `active` → 下一則訊息 attach 成功。**無孤兒**。 |
| **odoo-shell** | 未捕捉例外 → 交易 **rollback** | session **沒 commit** | operator 已用 deterministic hash（`hmac_sha256(project_id, secret)`，`src/models/agent_session.py:22`）建 pod → webhook 查無 session → **404** + 孤兒空 ns。重建並 commit 同 project 的 session（同 hash）可收編。 |

## 診斷 recipe（共用叢集只做 read-only）

1. **operator log 看 cold-start 時序**：`kubectl --context woow-k3s -n paas-stg-system logs <operator-pod> --timestamps | grep -vE 'version --short' | grep -E 'Executing:|spawn-or-attach'` → 看 `kubectl apply` / `helm install` 之間的時戳差，每步 5–17s = throttle 命中。
2. **operator deployment CPU limit**：`kubectl --context woow-k3s -n paas-stg-system get deploy paas-operator-stg -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}'` → `250m` = 命中；`1` = 已是 1000m（K8s 把 `1000m` 正規化顯示為 `1`）。
3. **確認是 pod limit 非節點競爭**：`kubectl top pod <operator-pod> -n paas-stg-system`（平時 ~11m）+ `kubectl top nodes`（0–1%）。
4. **量 cold-start**：odoo shell 建全新 project + `env.cr.commit()` 後計時 `client.spawn_or_attach(...)`；對照 attach 既有 pod（~5s）。

## Fix

兩層，**都已 merge + 驗證（2026-06-02）**：

### ① Client 層治標 —— #123（已併入 `develop`）

`src/services/agent_runtime_client.py`：per-call timeout `_SPAWN_TIMEOUT=90` + `_SPAWN_MAX_RETRIES=2`（backoff 2s）+ WS connect bounded retry + `spawning` 進度事件。把「30s 失敗」變成「~36s **成功** + 進度提示」（stg 實測 36.1s 第一次即成功，不再 `ReadTimeout`）。仍慢，故需 ②。

### ② Operator 層根治 —— PR `woow-paas/paas-app-bundle-cd#1`（已 merge + Fleet sync）

operator `resources.limits.cpu` **250m → 1000m**，改兩檔：

- `paas-operator-stg/values-stg.yaml`（stg）
- `paas-operator/values-prod.yaml`（prod）

⚠ operator 由 **`paas-app-bundle-cd` Fleet 管**，CPU 改 **必走 CD repo values**——`kubectl patch` 會被 Fleet 還原（見 [[agent-runtime-webhook-statesync-deploy-hazards]] / `reference_agent_runtime_stg_verify` §5.5）。`1000m` 只「抬高」throttle 上限、不降 request（仍 50m）、不碰 memory / image / chart，節點 CPU 餘裕大故零風險。

**驗證結果**：Fleet sync 後 operator pod rollout 帶新 limit（K8s 顯示 `1`）→ **stg cold-start `spawn_or_attach` 36.1s → 11.3s（3.2× 加速，<30s）**；prod 兩 replica（`paas-operator`@`paas-system`）也同步到 1000m。

## 清理注意（測試殘留）

- odoo-shell 刪測試 session 的 `unlink` 可能與 reap 觸發的 webhook（別的 worker 正寫同一 row）併發 → `psycopg2 SerializationFailure: could not serialize access due to concurrent update`。解：`env.cr.rollback()` 後 retry（reap/webhook 落定後即無併發），或刪前 `time.sleep` 緩衝。
- operator `delete`（client `DELETE /{hash}`）**只 uninstall helm release、保留空 namespace**（與 reaped 同模式，供 resume）。刪掉 project 後該空 ns 變無主 orphan（inert、零負載），測試後需手動 `kubectl --context woow-k3s delete ns paas-ws-stg-pa-<hash>`（刪前確認 `get pods,deploy` 為空 + `helm list -n` 無 release）。

> 記憶體索引：[[reference_agent_runtime_stg_verify]]（含 deploy-stg 啟用 recipe + 本根因/修復的精簡版）。
