---
name: caqa-report-2026-05-27-agent-runtime-pod-phase-e0
phase: caqa
team: feature-agent
analyst: agent-gaps-caqa
epic: agent-runtime-pod
cycle: Phase E0（pilot-revealed production gap — F-PILOT-4 registry pull / F-PILOT-8 LLM credential）
scope: operator unit only（extra/paas-operator/）— cross-repo chart/cluster NOT analysed (lock-step caveats only)
diff_base: b46f0af
branch: feature/agent-pod-phase-e0
head: 3d7e083
commits: [76836b8 red, e2a3d7b mock-fix, 3d7e083 src green]
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md  # §Phase E0
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md  # §8
input_report: docs/tracking/test-reports/test-coverage-agent-runtime-phase-e0-2026-05-27.md
created: 2026-05-27
status: pass-with-caveats
verdict: PASS WITH CAVEATS
counts: { blocker: 0, caveat: 3, info: 6, out_of_scope_followup: 1 }
tools: [sc:analyze, security-auditor, direct-diff-read]
related:
  - docs/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md
  - docs/decisions/patterns/credential-claim-test-fixture-coupling.md
  - docs/tracking/pitfalls/cross-repo-artifact-landing-assumption.md
---

# CAQA Report — Agent Runtime Pod Phase E0（F-PILOT-4 registry pull / F-PILOT-8 LLM credential，operator green）

> **§0 啟動自檢通過**：repo = woow_paas_platform（origin git-prod.woowtech.io/odoo-addons/woow_paas_platform）/ branch = `feature/agent-pod-phase-e0` / HEAD = `3d7e083`（operator src green）/ diff base `b46f0af`（#74 merge，非 stale，含 plan §Phase E0 + design §8）。commit chain 76836b8 red → e2a3d7b mock-fix → 3d7e083 green。
>
> **scope（嚴守）**：只分析 **operator 這輪 green**（`extra/paas-operator/`：`agent_runtimes.py` + `schemas.py`）。cross-repo（paas-charts T4.2b SA-minimal RBAC / T8.2 secretKeyRef·optional / image T8.4）+ 整合（cluster T4.3/T4.4）+ F-PILOT-10 真 LLM **不在直接分析範圍** —— 但跨 repo lock-step 依賴標為 caveat。
>
> **方法**：`/sc:analyze` 四面向框架 + security-auditor 深審雙 secret 洩漏面（角色委派 #1 + #4）+ 27→3 檔 green diff 逐行直讀，三方三角覆蓋 quality/security/performance/architecture。

## Verdict

**PASS WITH CAVEATS** — 0 blocker / 3 caveat（1 operator-owned + 2 cross-repo lock-step）/ 6 info / 1 out-of-scope follow-up。

operator 兩條 provisioning 路徑（F-PILOT-4 registry pull secret forward + F-PILOT-8 per-tenant LLM Secret）**正確且 scope 收斂**：helm values 只帶 secret **名**（creds 不入 values/argv）、明文 key 只落在 Secret 本體（**經獨立驗證不洩進 log/exception/repr**）、per-tenant 隔離成立、複用既有 `ensure_registry_secret`（無新洩漏面）。**0 operator-introduced blocker**。3 caveat 中唯一 operator-owned 是 orphan-secret 生命週期（per-tenant 圈限、非跨租戶 → medium）；另 2 條是 cross-repo lock-step（chart 端 SA-RBAC + secretKeyRef·optional）—— 適合帶入 reviewer + 跨 repo 對齊，**不阻斷本輪 operator green**。

---

## 四面向結果

### Security（核心面向，逐 leader focus 對齊）

- **[focus F-PILOT-4 — registry creds 洩漏面] operator 無新增洩漏面，blast radius 全押 cross-repo 防線。**
  - `spawn_or_attach` cold-start 呼 `k8s_service.ensure_registry_secret(namespace)`（`agent_runtimes.py:217`）—— **複用** `helm.py:649` 既有（marketplace `create_namespace` 同一函式，E0 只多一個 caller），把平台級 jcr-prod dockerconfigjson creds 落租戶 ns。`_build_install_values` forward `"imagePullSecrets":[{"name": settings.registry_secret_name}]`（`agent_runtimes.py:117`）—— **只帶 secret 名、不帶 creds**；creds 僅存於 Secret 本體、僅 kubelet 拉 image 時用。**正確的 shape**。
  - design §8.1 的防線「pod SA 無 secrets get/list/watch RBAC → pod 讀不到」是 **chart 端 invariant（T4.2b，cross-repo）**。**operator 本輪不引入任何 額外 洩漏面**（平台 creds 複製進每個租戶 ns 是 imagePullSecrets 固有 pattern，非新行為，且與 marketplace 既有紀律一致）。
  - **blast radius（防線失效時）**：若 cross-repo SA-RBAC 缺失/落後 → 被攻陷或惡意 agent pod 用其 SA token `get` 自己 ns 的 `paas-registry` → 取得平台級 jcr-prod creds → 可拉（若 creds 含寫權則可推/污染）registry 任意 image = **平台級** blast radius。→ 見 E0-CAVEAT-3。
- **[focus F-PILOT-8 — LLM key per-tenant 隔離 + 路徑洩漏面] 隔離成立、明文 key 路徑經獨立驗證清白。**
  - per-tenant 隔離（T8.3）：Secret 名 `f"{release}-llm"`（`agent_runtimes.py:179`），release=`agent-pa-{project_hash}`、ns 亦 keyed by hash → 租戶 A 的 `agent-pa-{hashA}-llm` 落 ns `…pa-{hashA}`，**無任何 code path 讓 B 的 key 落 A 的 Secret**。INV-2 成立。
  - **明文 api_key 洩漏面（HIGH-severity 自查 + auditor 獨立複驗，雙清白）**：(1) 全 `src/` 無 `str(request)`/`repr(request)`/`.dict()`/`.model_dump()`；(2) `apply_manifest`（`helm.py:959`）只 `kubectl apply -f <0600 tmpfile>`，**stringData 明文不入 argv/log**；(3) FastAPI 例外 handler（`main.py:123-133`）回 generic「Internal server error」**不 echo body**；(4) `KubectlException.__str__` = message-only（不含 command/stderr）；(5) `api_key` 為無約束 `Optional[str]` → 不會觸發 echo 其值的 422 ValidationError。→ **明文 key 不進 operator log**。✓ 列為 affirmative no-finding（info）。
- **[focus 契約 — 無 api_key 不建 secret → chart secretKeyRef 必須 optional] operator 端正確，失效模式全在 cross-repo。**
  - `_provision_llm_secret` 首行 `if not request.api_key: return`（`agent_runtimes.py:170`）→ 無 key 的 agent（echo，永不呼 LLM）**不建 Secret**。正確。
  - **契約對不對**：conditional-create（operator）+ optional-ref（chart）是正確 pattern（不該強塞 secret 給不需 LLM 的 agent），且**與 Phase D charts `optional:true` pattern 一致**（我 Phase D 分析過的 5 個 agent-runtime env secretKeyRef）。**契約 sound**。
  - **gap**：若 chart 漏 `optional:true`（T8.2 不落地）→ pod `secretKeyRef` 指向不存在的 `{release}-llm` → kubelet `CreateContainerConfigError` → **keyless（echo pilot）pod 永遠起不來**。operator 無法防 → 見 E0-CAVEAT-2。
- **[E0-CAVEAT-1 / operator-owned] orphan plaintext-key Secret on teardown。** `{release}-llm` 由 `apply_manifest`（kubectl apply）**out-of-band 建、無 owner-reference**，但 `delete_runtime`（`agent_runtimes.py:324-327`）只 `helm uninstall`、`reap`（`:266`）只 scale-0，**operator 全程無 `kubectl delete namespace`**（auditor sweep 確認零 ns-delete）→ helm uninstall 不回收此 Secret → **明文租戶 key 殘留 ns**直到外部回收。**severity = caveat（medium）非 blocker**：是租戶自己的 key 在租戶自己的 ns（per-tenant 圈限、INV-2 不破、無跨租戶曝露）；但屬真實 hygiene 債（長壽 ns 累積明文憑證、繞過了本應清掉它的生命週期）。**方向**：teardown 刪 ns（一次收全部）/ 或 `delete_runtime` 顯式 `kubectl delete secret {release}-llm` / 或讓 chart 接管此 Secret 使 `uninstall` 回收（皆方向、非 patch）。

### Quality

- [info] `agent_runtimes.py:160-185` `_provision_llm_secret` 乾淨、單一職責、keyless 早退、與 `_ensure_namespace` 對稱。`json.dumps(request.llm_model_config)` 在 pydantic `Dict[str, Any]`（源自 JSON request）上安全。
- [info] `schemas.py:393-405` inline 註解明確警示 field 名為 `llm_model_config` **非** `model_config`（pydantic-reserved BaseModel 屬性）—— 防 footgun 的 defensive doc，reviewer 看到非隨意命名。good。
- [info] `agent_runtimes.py:117` `imagePullSecrets` **無條件**寫入 install values，即使 `ensure_registry_secret` 因 registry 未配置（`helm.py:649` `if not settings.registry_server...: return None`）而未建 Secret → pod imagePullSecrets 指向不存在 secret（kubelet 略過、fallback anonymous pull）。私有 image 場景外（dev/test）的輕微不一致，非 security、非 blocker。

### Performance

- **無發現。** 兩個新呼叫是 cold-start 路徑一次性 kubectl apply（非 hot path、無 loop、無 N+1）；`_provision_llm_secret` 對 keyless agent 早退。cold-start 中途失敗的 partial state（ns + secret 已建、無 helm release）因 kubectl apply idempotent → 下次 spawn 重試覆蓋，benign。

### Architecture

- [info] `agent_runtimes.py:216-218` cold-start 順序（`_ensure_namespace` → `ensure_registry_secret` → `_provision_llm_secret` → sign → install）正確：所有前置在 `helm install` 前落地，鏡像 marketplace `create_namespace`→`ensure_registry_secret` 紀律。架構一致。
- [info] api_key 隨 spawn request（非 operator 反查 Odoo DB）—— 架構正確、解耦 operator↔Odoo DB（design §8.2）。隱含前提：Odoo→operator HTTP channel 須 TLS/in-cluster（部署面、非 operator code）。
- [caveat 根因→歸入 E0-CAVEAT-1] `agent_runtimes.py:175` LLM Secret **out-of-band（apply_manifest、無 helm ownership / owner-reference）** 是 orphan 的架構根因。registry secret 同 out-of-band 但 idempotent/shared 故 orphan benign；LLM secret 帶明文且 per-release 故 orphan matters。方向同 E0-CAVEAT-1。

---

## Plan alignment check

implementation **對齊** design §8.1（F-PILOT-4 chart 端 SA-RBAC invariant + operator forward 名不 forward creds）/ §8.2（F-PILOT-8 operator runtime 建 per-release Secret、key 隨 request、secretKeyRef 不落明文 pod spec 屬 chart 面）。operator 做的正是 plan 所述，無走偏。

**一個 plan-completeness gap（非 implementation 走偏）**：design 的 per-release Secret 生命週期**隱含假設 ns teardown 會回收它**，但 operator `delete_runtime` 只 helm uninstall、從不刪 ns → plan 的「leak 圈限租戶」成立，但其隱含的「teardown 清掉 Secret」**不成立**。屬 plan-vs-impl 生命週期落差，**經 leader 路由 planner**（E0-CAVEAT-1）。

---

## 同源 code path 檢查（secret-provisioning sweep）

auditor sweep 全 operator secret-provisioning 點：
1. `helm.py:649` `ensure_registry_secret` — 同一 dockerconfigjson，marketplace `create_namespace`（`helm.py:645`）+ 本輪 agent path（`agent_runtimes.py:217`）**雙 caller 同紀律**，E0 無弱化。✓
2. `helm.py:1148` `patch_secret` — `n8n_init.py:158` 寫 n8n key，只 log secret **名** 不 log 值，同（可接受）紀律。✓
3. `mcp_invalidator.py` / `releases.py` `apply_manifest` — ConfigMap/Service 無 secret material。N/A
4. **E0 LLM Secret 是唯一「out-of-band + 不被 teardown 回收 + per-release 明文」的點 —— unique，非 copied-everywhere pattern**（n8n secret 是 chart-owned 故 uninstall 回收；registry secret 是 shared/idempotent 故 orphan benign）。✓ 確認 E0-CAVEAT-1 不是同源複製缺陷。

**out-of-scope 同源觀察（明標非 E0 finding）**：`helm.py:77`（及 `:481/:1121`）`logger.info(f"Executing: {' '.join(cmd)}")` log 完整 argv → helm install/upgrade 的 `--password <agent_runtime_repo_read_token>` 落 INFO log。此為 gap C 的 Gitea **repo-read token**（`df6348e` 引入，**非本輪 E0 diff**、非租戶 LLM key），屬 credential-in-logs hygiene。列 follow-up（見下），**不是 E0 阻斷/caveat**。

---

## Known pitfalls 對照

- **`credential-claim-test-fixture-coupling`**：E0 加 `api_key`/`llm_model_config` 為 optional request field 並開新 credential 路徑 → 該 pattern 預警 optional→consumed credential 會 forward-couple test fixture。**reviewer 須確認** E0 test（`test_agent_runtimes.py`）同時驗 key-present（建 Secret）+ key-absent（不建 Secret）兩分支，鎖死 conditional-create 契約（測試報告 §2 列 T8.1b + 隔離 T8.3，但 key-absent 分支覆蓋由 reviewer 核）。
- **`cross-repo-artifact-landing-assumption`**：E0-CAVEAT-2（`optional:true`）+ E0-CAVEAT-3（SA-RBAC）皆落 **paas-charts**、非本 repo。符合該 pitfall「跨 repo 勘查實體落點」教訓 —— 無新違反，但兩者皆 GA 前須跨 repo 確認落地的 hard lock-step gate。

---

## Blocker 清單（給 leader / implementation）

**無。** operator 本輪 green 無 introduced blocker。（嚴守 severity 紀律：orphan 為 per-tenant 圈限 → caveat；registry 複製為固有 pattern 無新 operator 面 → caveat。）

---

## Caveat 清單（給 leader 決定是否帶入 reviewer / planner）

| # | 類型 | file:line | issue | 方向（非 patch） | 阻斷性 |
|---|------|-----------|-------|------------------|--------|
| **E0-CAVEAT-1** | operator-owned（security/lifecycle） | `agent_runtimes.py:175` + `:324-327` | orphan 明文 LLM Secret on teardown（helm uninstall 不回收 out-of-band Secret、operator 從不刪 ns）；per-tenant 圈限非跨租戶 | teardown 刪 ns / 或顯式 delete secret / 或 chart 接管。**plan-completeness gap → 路由 planner** | 不阻斷本輪 |
| **E0-CAVEAT-2** | cross-repo lock-step | `agent_runtimes.py:170` + chart T8.2 | operator 無 api_key 不建 Secret → chart `LLM_API_KEY` secretKeyRef **必須 `optional:true`**，否則 keyless（echo）pod `CreateContainerConfigError` 起不來 | 跨 repo 確認 chart-side `optional:true` 落地（與 Phase D optional:true pattern 一致） | 不阻斷 operator green，**阻 pilot e2e** |
| **E0-CAVEAT-3** | cross-repo lock-step + defense-in-depth | `agent_runtimes.py:117` + chart T4.2b | 平台 registry creds 複製進每個租戶 ns，唯一防線 = chart SA-minimal RBAC；防線失效 = 平台級 blast radius | GA 前確認 chart SA 無 secrets get/list/watch；**建議 registry creds pull-only-scoped（registry IAM、非 operator code）作 defense-in-depth** | 不阻斷本輪 |

**out-of-scope follow-up（非 E0、leader 自行決定路由）**：`helm.py:77` argv log 含 `--password` repo-read token（gap C `df6348e`）—— credential-in-logs，建議 redact argv 或改 file/env 傳 token。

---

## 工具 raw 輸出摘要

- **/sc:analyze**（四面向框架，quality/perf/arch 加重）：Quality 3 info（單一職責乾淨 / 命名防 pydantic 雷 / imagePullSecrets 無條件寫入輕微不一致）；Performance 無發現（cold-start 非 hot path）；Architecture out-of-band provisioning 為 orphan 根因 + cold-start 順序正確 + api_key 隨 request 解耦 DB。
- **security-auditor**（雙 secret 洩漏面 + 同源 sweep + plan alignment）：0 operator blocker；F-PILOT-4 無新洩漏面、forward 名正確、防線失效 = 平台級 blast radius、建議 pull-only creds；F-PILOT-8 per-tenant 隔離 sound、**明文 key 路徑獨立複驗清白**（5 點）、orphan lifecycle = 唯一 operator-owned caveat、optional:true 契約失效全在 cross-repo；同源掃出 `helm.py:77` repo-token argv log（非 E0）。
- **direct diff read**（b46f0af..3d7e083，3 檔）：schemas 2 field（`api_key` + `llm_model_config` 避保留字）；agent_runtimes imagePullSecrets forward（名）+ `_provision_llm_secret`（keyless 早退、`{release}-llm`、stringData、`paas-managed` label）+ cold-start 4 步順序；`ensure_registry_secret` 複用 helm.py:649 非重寫。

---

## Hand-off

- **狀態**：Phase E0 operator green CAQA = **PASS WITH CAVEATS**（0 blocker / 3 caveat / 6 info / 1 out-of-scope follow-up）。四面向全覆蓋（Security 核心 + Quality 3 / Performance 無發現 / Architecture）。
- **可推 reviewer**：無 operator blocker，3 caveat 皆不阻斷本輪 operator green。
- **需 leader / planner 動作**：
  1. **E0-CAVEAT-1（operator-owned，最該收）**：orphan 明文 LLM Secret 生命週期 → 路由 planner（plan-completeness）；本輪可帶 caveat 進 reviewer，fix 方向已列。
  2. **E0-CAVEAT-2 / E0-CAVEAT-3（cross-repo lock-step）**：跨 repo 對齊 paas-charts —— T8.2 secretKeyRef `optional:true`（否則 echo pilot pod 起不來）+ T4.2b SA-minimal RBAC（registry creds 唯一防線）。GA / pilot e2e 前必確認落地。
  3. **defense-in-depth 建議**：platform registry creds pull-only-scoped（registry IAM）。
  4. **out-of-scope follow-up**：`helm.py:77` repo-read token argv log redact（gap C，非 E0）。
  5. **reviewer 提醒**：核 E0 test 是否覆蓋 key-present + key-absent 兩分支（credential-claim-test-fixture-coupling）。
- **next**：回報 leader → reviewer。impl 若變更 operator test surface 與測試報告 cell-map 不一致 → 退回 explainer。
