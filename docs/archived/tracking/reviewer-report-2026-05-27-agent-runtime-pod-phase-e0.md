---
name: reviewer-report-2026-05-27-agent-runtime-pod-phase-e0
phase: reviewer
team: agent-runtime-pod
reviewer: agent-runtime-pod-reviewer
scope: operator unit only（extra/paas-operator/）— cross-repo chart/cluster lock-step caveats only
diff_base: b46f0af
branch: feature/agent-pod-phase-e0
head: 3d7e083
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
test_report_ref: docs/archived/tracking/test-coverage-agent-runtime-phase-e0-2026-05-27.md
caqa_ref: docs/archived/tracking/caqa-report-2026-05-27-agent-runtime-pod-phase-e0.md
created: 2026-05-27
verdict: APPROVE（與 CAQA PASS WITH CAVEATS 對齊；明文 LLM key 5 點不洩漏獨立重驗清白；key-present 鎖死、key-absent 負分支經 mutation 證實未鎖→headline caveat E0-R-1 建議本輪 tester 補；3 caveat deferral 合理）
related:
  - docs/archived/tracking/reviewer-report-2026-05-25-agent-runtime-pod-phase-d.md
  - docs/archived/tracking/caqa-report-2026-05-27-agent-runtime-pod-phase-e0.md
  - docs/decisions/patterns/credential-claim-test-fixture-coupling.md
  - docs/tracking/pitfalls/cross-repo-artifact-landing-assumption.md
status: archived
archived-from: docs/tracking/reviewer-report-2026-05-27-agent-runtime-pod-phase-e0.md
archived: 2026-07-19
archived-reason: "一次性 reviewer 驗收報告，cycle 已收官；證據錨點引用已改指本處"
---

# Reviewer Report — Agent Runtime Pod Phase E0（F-PILOT-4 registry pull / F-PILOT-8 LLM credential，operator green）

> **§0 啟動自檢通過**：`pwd` = woow_paas_platform--featureagent-pod；`git remote -v` → `origin` git-prod.woowtech.io/odoo-addons/woow_paas_platform（確認本 repo）；`git fetch origin` 後 branch = `feature/agent-pod-phase-e0`、HEAD = `3d7e083`；commit chain 76836b8 red → e2a3d7b mock-fix → 3d7e083 green，皆與 brief 吻合。**不信 stale local develop**：Phase E0 乾淨 base = `b46f0af`（Merge PR #74，76836b8 的 parent），`b46f0af..HEAD` = 3 commit / 3 檔 / +245-2。init-agent-5 note 已讀。

## 1. Final Verdict

**APPROVE** — operator 本輪 green 可 merge。verdict 與 CAQA **PASS WITH CAVEATS 對齊**，**4 方（CAQA security-auditor + 本輪 security-auditor + pr-test-analyzer + code-reviewer）verdict 無方向性分歧**（全 proceed to Reporter）→ 不觸發紅線。**0 blocker、0 major**。

operator 兩條 provisioning 路徑正確且 scope 收斂：
- **F-PILOT-4 registry pull** ✓：cold-start 呼 `k8s_service.ensure_registry_secret(ns)`（複用 `helm.py:649`，與 marketplace `create_namespace` 同一函式同紀律，非重寫）；`_build_install_values` forward `imagePullSecrets:[{name: settings.registry_secret_name}]`（**只帶 secret 名、不帶 creds**）。
- **F-PILOT-8 per-tenant LLM Secret** ✓：`_provision_llm_secret` keyless 早退 → 有 api_key 才建 `{release}-llm`（per-tenant 隔離 INV-2 成立，release+ns 皆 keyed by hash，無 code path 讓 B 的 key 落 A）。
- **明文 LLM api_key 不洩漏（HIGH self-check）獨立重驗清白** ✓（5 點，見 §4）。

**headline caveat（E0-R-1，leader brief 重點查核結果）**：`_provision_llm_secret` 的 conditional-create **key-present 分支鎖死**（test_T81b + test_T83），但 **key-absent 負分支未鎖死**——pr-test-analyzer 以 mutation 證實（刪掉 `if not request.api_key: return` 後全 43 test 仍綠）。正中本 epic 反覆踩的 `credential-claim-test-fixture-coupling` pitfall。**不擋 merge**（impl 經 4 方直讀驗證正確、是一行 fail-safe guard、worst-case 是 echo agent 永不讀的空 secret、phase 為 operator-unit/inert），但**強烈建議本輪 tester 補一條 ~5 行 negative test**（cheap，且關掉一個 documented recurrent pitfall）。

## 2. Diff Summary

| 項目 | 值 |
|------|----|
| scope | operator unit only（`extra/paas-operator/`）；cross-repo chart（T4.2b/T8.2）/ cluster（T4.3/T4.4）/ F-PILOT-10 真 LLM **不在 review 範圍**（caveat 標記） |
| base | `b46f0af`（#74 merge，非 stale）|
| diff | `b46f0af..HEAD` = 3 commit / 3 檔 / +245-2 |

- `extra/paas-operator/src/models/schemas.py`（+13）— `SpawnOrAttachRequest` 加 `api_key: Optional[str]` + `llm_model_config: Optional[Dict[str,Any]]`（後者刻意避 pydantic 保留字 `model_config` + `model_` protected-namespace warning）
- `extra/paas-operator/src/api/agent_runtimes.py`（+41/-2）— `imagePullSecrets` forward + 新 `_provision_llm_secret`（keyless 早退、`{release}-llm`、stringData、`paas-managed` label）+ cold-start 順序 `_ensure_namespace`→`ensure_registry_secret`→`_provision_llm_secret`→sign→install
- `extra/paas-operator/tests/test_agent_runtimes.py`（+193）— `TestPilotGapRegistrySecret`（T4.1a/b）+ `TestPilotGapLlmCredential`（T8.1a/T8.1b/T8.3）

## 3. 委派工具 raw 輸出摘要（multi-agent，operator green 完整 review）

> 縮減理由：3 檔 / +245 的小聚焦 diff，CAQA 已用 `sc:analyze` + security-auditor 過完整四面向。我聚焦 brief 兩大重點（HIGH 明文 key 洩漏 + key-present/absent 兩分支）並 honor「完整 review」，dispatch 3 agent 並行。

| Agent | Verdict | 重點 |
|---|---|---|
| `security-auditor` | NO LEAK / proceed to Reporter | **5 點明文 key 不洩漏獨立重驗清白**（2 點對 pinned pydantic 2.10.6 實測）；`agent_runtimes.py` **零 logger 呼叫**；apply_manifest 0600 tmpfile + argv 只有檔名；KubectlException/HelmException `str()` message-only；E0-CAVEAT-1/3 同意、severity 正確 |
| `pr-review-toolkit:pr-test-analyzer` | approve-with-caveat | **mutation 證實 key-absent 負分支未鎖死**（刪 guard → 43 test 全綠）；key-present（T81b）+ 隔離（T83 兩負 leg 真實）鎖死、非 mock-theater（distinctive key literal 不會 false-match）；deferral well-documented；gap rated MINOR 4/10 |
| `pr-review-toolkit:code-reviewer` | approved with caveats / 0 blocker 0 major | `_provision_llm_secret` 單一職責正確、`json.dumps` 安全、cold-start 順序正確 idempotent retry；2 minor = imagePullSecrets 無條件 forward（kubelet-benign）/ orphan on partial-failure（paas-managed 可回收）；`llm_model_config` 命名正確 |

**4 方 verdict 無方向性分歧**：全 proceed to Reporter。

## 4. HIGH self-check 獨立重驗 — 明文 LLM api_key 5 點不洩漏（CAQA 自查清白，我複驗）

security-auditor 對**實際 code**（非 CAQA 斷言）逐點重驗，**全清白**，2 點對 pinned pydantic 2.10.6 實測：

| # | 點 | 重驗結果 |
|---|----|---------|
| 1 | 無 `str/repr/.dict()/.model_dump()` of request | ✓ `model_dump()` 全 src 僅 `main.py:119/132` 對 `ErrorResponse`（非 request）；`request.api_key` 只在 `agent_runtimes.py:170,172` 被讀；**`agent_runtimes.py` 零 logger 呼叫** |
| 2 | apply_manifest 不把明文進 argv/log | ✓ `helm.py:973-981` `yaml.safe_dump` 寫 `NamedTemporaryFile`（**0o600**）→ `kubectl apply -f <file>`；`logger.info(Executing: ...)` 只 log 檔案路徑；`finally` unlink |
| 3 | FastAPI exception handler generic 不 echo body | ✓ `main.py:111-133` catch-all 回 hardcoded「Internal server error」+ class name；不 echo request body |
| 4 | `KubectlException.__str__` message-only | ✓ `helm.py:30-47` `super().__init__(self.message)`；`command`/`stderr` 存屬性但 `str()` 不含；LLM-secret call chain 不 log `e.stderr`/`e.command` |
| 5 | `api_key` 無約束 → 無 422 echo | ✓ `schemas.py:398-401` `Optional[str]`；實測：他欄位 type-fail 時 valid string api_key **不** 出現在 `str(e)`/`e.errors()`；唯一可能 = caller 把自己的 key 包成 malformed structure 看到自己的值回 echo（**非第三方洩漏 vector**）|

→ **明文租戶 LLM key 不進 operator log/exception/repr/argv**。CAQA 的 affirmative-no-finding 確認，並補深度（零 logger 呼叫 + pydantic 實測）。

## 5. Key-present / key-absent 兩分支查核（leader brief 重點 + credential-claim-test-fixture-coupling pitfall）

`_provision_llm_secret` 的 conditional-create：
```python
if not request.api_key:
    return                                          # key-ABSENT 分支：不建 secret
string_data = {"LLM_API_KEY": request.api_key}      # key-PRESENT 分支：建 secret
...apply_manifest(...)
```

| 分支 | 鎖死狀態 | 證據 |
|------|---------|------|
| **key-PRESENT** | ✅ 鎖死 | `test_T81b`（body 帶 api_key → 斷言 `{release}-llm` provisioned + key 在 mock_calls，plain/base64）；`test_T83`（per-tenant 隔離，A 的 key 不入 B、反之，兩負 leg 真實）|
| **key-ABSENT** | ❌ **未鎖死（E0-R-1）** | `_SPAWN_BODY` 無 api_key → 早退被**附帶**執行，但**無 test 斷言 secret 不存在**。grep `assert_not_called` 僅 line 590（auth-block 測試，無關）。**pr-test-analyzer mutation 證實**：刪 `if not request.api_key: return` → 全 43 test 仍綠（impl 已還原，git diff clean）|

**判斷**：E0-R-1 是 documented recurrent pitfall（`credential-claim-test-fixture-coupling`，本 epic Phase B/C/D 已踩）的再現，但此處是 **missing-negative** 形態而非 false-green coupling。**MINOR / 不擋 merge**：
1. impl 經 4 方直讀驗證正確（guard 在且正確）；
2. worst-case regression benign（echo agent 拿到 `LLM_API_KEY: null` 空 secret、永不讀；無洩漏、無 crash、INV-2 不破）；
3. phase operator-unit/inert（本輪無 cluster wiring）。

**建議**：keyless（key-absent）是當前 echo pilot 的**主路徑**，且補 negative test 僅 ~5 行（同 test 檔、同 method-agnostic mock-inspection style：`assert f"{release}-llm" not in str(mock_k8s.mock_calls)`）。**強烈建議本輪 tester 補**（cheap + 關掉 documented pitfall），而非延後遺失。不採 needs-revision 整輪退回（impl 無 defect、僅缺 test-lock）。

## 6. CAQA Cross-Check（3 caveat / 6 info / 1 out-of-scope follow-up）

**3 caveat — 全同意，deferral 合理**：

| # | CAQA 判定 | Reviewer 立場 | 確認依據 |
|---|---|---|---|
| E0-CAVEAT-1 orphan 明文 LLM Secret on teardown（operator-owned）| caveat，路由 planner，bind F-PILOT-10 hard gate | **同意 deferral 合理** | security-auditor 確認：`delete_runtime` 只 helm uninstall、`reap` 只 scale-0、operator 全程無 `kubectl delete ns`；out-of-band 無 owner-ref → orphan。但 **keyless 階段 orphan 邏輯上不可能**（無 api_key → 不建 secret），真 LLM 上線（F-PILOT-10）前必收 + 屆時 holistic teardown design → 本輪不修合理 |
| E0-CAVEAT-2 chart `LLM_API_KEY` secretKeyRef 必須 optional（cross-repo）| caveat，lock-step paas-charts | **operator 端契約正確** | conditional-create（operator 無 key 不建）+ optional-ref（chart）是正確 pattern，與 Phase D charts `optional:true` 一致。失效模式（chart 漏 optional → keyless pod `CreateContainerConfigError`）全在 cross-repo，operator 無法防。記給 paas-charts ✓ |
| E0-CAVEAT-3 registry creds blast radius（cross-repo + defense-in-depth）| caveat，lock-step + 建議 pull-only-scoped | **operator 無新洩漏面確認** | security-auditor 確認 `imagePullSecrets` forward + `ensure_registry_secret` 與 marketplace path **byte-identical**（`helm.py:645` 同函式），非新行為；blast-radius 框架準確（防線=chart SA-minimal RBAC，失效=平台級）。pull-only-scoped 為 registry IAM 層 defense-in-depth ✓ |

**6 info**：全同意，無異議。**HIGH self-check**：見 §4，獨立重驗清白並補深度。**out-of-scope follow-up**（`helm.py:77` argv log 含 gap C 的 `--password` repo-read token，非本輪 E0 diff）：同意非 E0 阻斷/caveat，leader 自決路由。

**反對：0 條。** CAQA 分級嚴格、scope 嚴守、無浮報。

## 7. CAQA 未抓 / 補充的新發現（無 high-severity）

| # | 來源 | Sev | 摘要 | 方向 |
|---|---|---|---|---|
| **E0-R-1** | pr-test-analyzer（mutation）| **minor（headline）** | key-absent 負分支未鎖死（mutation 證實刪 guard 不紅）；正中 `credential-claim-test-fixture-coupling` pitfall。CAQA 已委派 reviewer 查 → 我確認：**缺，且 mutation 證實** | 本輪 tester 補 ~5 行 negative：keyless spawn → `assert f"{release}-llm" not in str(mock_k8s.mock_calls)` |
| E0-R-2 | code-reviewer（= CAQA info 確認）| minor | `imagePullSecrets` 無條件 forward → registry 未配置時指向不存在 secret | kubelet-benign（anonymous-pull fallback）；optional 防禦：gate on `ensure_registry_secret` 回傳 |
| E0-R-3 | pr-test-analyzer | low（test robustness）| substring-match（`str(mock_calls)`）無法證明 key 落在 `{release}-llm` 的 stringData 而非他 call；single-secret-per-spawn 下足夠，未來 multi-secret 可能弱化 | 如未來一次 spawn 多 secret，改 per-call 斷言 |

E0-R-2/R-3 皆不擋 merge。code-reviewer 的「orphan on partial-failure」屬 E0-CAVEAT-1 子集（paas-managed labelled → reapable）。

## 8. Plan Alignment

implementation 對齊 design §8.1（F-PILOT-4 chart 端 SA-RBAC invariant + operator forward 名不 forward creds）/ §8.2（F-PILOT-8 operator 建 per-release Secret、key 隨 request、secretKeyRef 屬 chart 面）。operator 做的正是 plan 所述，無走偏。**plan-completeness gap**（非 impl 走偏）：design 的 per-release Secret 生命週期隱含假設 ns teardown 回收它，但 operator 從不刪 ns → E0-CAVEAT-1，CAQA 已路由 planner，我 concur。

## 9. 同源 code path 檢查

- **registry secret provisioning**：唯一另一 caller 為 marketplace `helm.py:645`（`create_namespace_with_quota`）→ agent cold-start mirror 之，consistent，無漏 path。
- **LLM Secret**：唯一「out-of-band + 不被 teardown 回收 + per-release 明文」點，**unique 非 copied-everywhere**（n8n secret chart-owned 故 uninstall 回收；registry secret shared/idempotent 故 orphan benign）→ E0-CAVEAT-1 非同源複製缺陷。
- **明文 key 流向**：`request.api_key` → `string_data` → `apply_manifest`（0600 tmpfile）；無第二條洩漏路徑（agent_runtimes.py 零 logger）。
- **imagePullSecrets shape**：`[{name}]` 為 K8s 標準慣例；chart-side（paas-charts）shape match 本 repo 不可驗 → cross-repo 依賴，integration 時確認（E0-CAVEAT-2/3 同 lock-step）。

## 10. Known Pitfalls 對照

| Pitfall / pattern | 對照結果 |
|---|---|
| [[credential-claim-test-fixture-coupling]] | **再現（E0-R-1）** — optional→consumed credential（api_key）開新路徑，key-present 鎖死但 key-absent 負分支未鎖（mutation 證實）；建議本輪補 negative test |
| [[cross-repo-artifact-landing-assumption]] | **無新違反** — E0-CAVEAT-2（optional:true）+ E0-CAVEAT-3（SA-RBAC）皆落 paas-charts，明標 cross-repo lock-step；GA/pilot e2e 前 hard gate |

## 11. Open Questions for Leader / Recommendation

- **verdict：APPROVE** → leader 可 recruit reporter。
- **E0-R-1（headline，建議本輪收）**：請 leader 決定——(a) **建議**：快速 tester 補一條 ~5 行 key-absent negative test（cheap，關掉 documented recurrent pitfall，keyless 是當前主路徑），可隨 reporter cycle 或一次 tester touch；(b) 接受為 logged follow-up 綁 `credential-claim-test-fixture-coupling` pitfall doc。我不採 needs-revision 整輪退回（impl 無 defect、僅缺 test-lock）。
- **3 caveat 帶入 reporter / flip-gate**：E0-CAVEAT-1（orphan → planner + bind F-PILOT-10 hard gate）、E0-CAVEAT-2（chart secretKeyRef optional:true → paas-charts lock-step）、E0-CAVEAT-3（registry SA-minimal RBAC + 建議 pull-only-scoped → paas-charts lock-step）。
- **out-of-scope follow-up**（leader 自決）：`helm.py:77` argv log 含 gap C repo-read token（非本輪 E0），redact argv / file·env 傳 token。
- **無紅線觸發**：reviewer verdict（APPROVE）= CAQA（PASS WITH CAVEATS）；4 方無方向性分歧；明文 key 5 點清白；新發現無 high-severity 安全洞 → 不 escalate verdict / 不 escalate planner（E0-R-1 為 test-lock 缺口非 design 漏洞；E0-CAVEAT-1 plan-completeness CAQA 已路由 planner）。

## 12. Sign-off

- **Verdict**：**APPROVE** — operator green 可 merge。
- **HIGH 明文 key**：5 點獨立重驗清白（含 pydantic 實測 + 零 logger 呼叫）。
- **兩分支查核**：key-present 鎖死 ✓；key-absent 負分支未鎖（mutation 證實）→ E0-R-1 建議本輪 tester 補。
- **CAQA cross-check**：3 caveat 全同意 + deferral 合理（反對 0）；6 info 同意；新增 E0-R-1~R-3 皆 minor/low。
- **4 方 multi-agent**：verdict 無方向性分歧。
- **Recommendation**：proceed to Reporter；leader 決 E0-R-1 收法（建議本輪 tester 補）+ 3 caveat 帶入 paas-charts lock-step / F-PILOT-10 hard gate / planner（orphan lifecycle）。
