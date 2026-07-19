---
name: agent-runtime-pod-phase-d-enable-gate-2026-05-25
phase: phase-d-completion
epic: agent-runtime-pod
team: agent-runtime-pod
reporter: agent-runtime-pod-reporter
created: 2026-05-25
verdict: APPROVE（CAQA PASS WITH CAVEATS + reviewer APPROVE，0 blocker；5-agent 無方向性分歧；HC-2 security-auditor 獨立確認 airtight）
cross_repo:
  - paas-agent-runtime（F-PC-7 WS verifier A 嚴格在 `feature/f-pc-7-token-use-strict` `a8b3ce2`+`dabb275`，merge+deploy 屬 flip-gate；引用以 repo 名 + branch/commit 標明）
related:
  - docs/plans/2026-Q2/agent-runtime-pod-plan.md
  - docs/plans/2026-Q2/agent-runtime-pod-design.md
  - docs/archived/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md
  - docs/archived/tracking/reviewer-report-2026-05-25-agent-runtime-pod-phase-d.md
  - docs/archived/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/decisions/patterns/credential-claim-test-fixture-coupling.md
  - docs/tracking/pitfalls/cross-repo-artifact-landing-assumption.md
  - docs/tracking/followups/follow-ups.md
status: archived
archived-from: docs/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md
archived: 2026-07-19
archived-reason: "agent-runtime epic phase durable 報告；epic 已收官（prod 已上線）"
---

# Phase D Durable — agent-runtime pod enable-gate hardening（epic A-D code-ready 收口）

> 本 doc 是 `agent-runtime-pod` epic 的 **Phase D 完成 durable**，由 epic reporter 寫於 woow_paas_platform。Phase D 是 **Phase C enablement-gate caveat 的逐條 remediation** —— 把 flip `use_agent_runtime` flag 前的所有 **CODE 前提**關掉（runtime 不炸 / error 通道正常 / 跨租戶 token 原語封死 / 無 WS 洩漏 / namespace secret fail-closed），備好 admin 啟用面 + cross-repo wiring。**不 flip flag、不部署、不做 2 週觀察** —— 那塊由使用者依 plan §Pilot runbook 自跑。整段 flag-gated（default False），出貨即 inert。
>
> 本 doc 不重複 caqa/reviewer report 的逐條 finding（那兩份是 review canonical home），也不重列 flip-gate 清單（彙整在 [[follow-ups]] §Phase D enablement gate + plan §Pilot runbook）。它記 **epic-closure 視角**：(1) Phase D 把 epic 帶到 **A-D code-ready 完成**；(2) 3 條 takeaway 的 durable 落點；(3) [[constraint-input-trust-expansion]] 在 epic 內第 4 度兌現的 cross-phase 安全弧線；(4) 剩給使用者的 flip-gate。

## 1. Phase D 交付 summary

| 項目 | 內容 |
|------|------|
| **乾淨 diff base** | `a9a4d1f`（最後一個 Phase C commit）— 本地 `develop` 落後（`develop..HEAD` = 38 commit），**不可用**（與 Phase A/C 同坑，CAQA/reviewer 皆避開）|
| **Phase D diff** | `a9a4d1f..HEAD` = 4 commit / 29 檔 / +1224-43 |
| **4 commit** | `5398f92`（red test）/ `8d152f7`（fix F-PC-1~6 + F-PA-14 + charts）/ `e2f1624`（feat enablement surface）/ `e94991d`（test fixtures + F-PA-14 guard）|
| **Verdict** | CAQA **PASS WITH CAVEATS**（0 blocker / 2 net-new caveat / 9 info / 3 carried-forward）+ reviewer **APPROVE**（5-agent 無方向性分歧）|

### 11 must-fix 全正確關閉目標 caveat（無走偏、無回歸、fix 無新風險）

| must-fix | 關掉的 Phase C caveat | 落地 |
|----------|----------------------|------|
| **F-PC-1** websockets pin | A-1（`extra_headers` 在 ≥14 移除）| `requirements.txt` 釘 `>=12.0,<14`（保 `extra_headers` API；未採 dual-kwargs）|
| **F-PC-2** webhook dual-secret | C-3 | `_verify_hmac_dual` current OR previous；env current 裸名 + previous `_PREVIOUS`（刻意不對稱）；空 slot 不放寬 |
| **F-PC-3** namespace secret env | C-5 | **設計 pivot auto-gen→K8s Secret env(b)**：`AGENT_RUNTIME_NAMESPACE_SECRET` fail-closed，殺 `_DEFAULT_SECRET` + 移除 ICP read（CAQA 評為比 auto-gen 更佳 — 無 idempotency 風險）|
| **F-PC-4** operator fail-closed | C-2 | `api_key` 未設 → **global 503（所有 7 router）**；token-forgery 原語堵住（接受 ~6 fixture 改）|
| **F-PC-5** typed error | N-1 | `AgentRuntimeOperatorError` 帶 operator detail → SSE 真因 surface |
| **F-PC-6** SSE aclose | P-2 | `finally` 補 `agen.aclose()` unwind suspended generator → WS `__aexit__` 跑 close（修每輪洩一條 pod 連線）|
| **F-PC-7** token_use A 嚴格 | C-1 半邊 | **cross-repo** `paas-agent-runtime` `feature/f-pc-7-token-use-strict`（`a8b3ce2`+`dabb275`）；merge+deploy 屬 flip-gate（見 §5）|
| **F-PA-14** owner-bootstrap | HC-2 / D-3 | `project.create()` SUPERUSER 分支才讀 `bootstrap_owner_partner_id` hint，normal-user 忽略（B1 lineage 維持關閉）|

> **admin enablement surface（code 就緒、inert）**：`project_views.xml` 加 `use_agent_runtime` toggle（default False）+ `agent_template_demo.xml` seed draft demo template（綁無 project → resolver 撈不到 → 不可達）。

## 2. 雙 verdict + HC-2 airtight（security-auditor framework-substrate 深度）

- **CAQA PASS WITH CAVEATS**：11 must-fix 全正確關閉、0 code 缺陷；2 net-new caveat 都**不是 code 缺陷**而是「部署 / cross-repo invariant 未被 code 強制」（D-CAVEAT-1/2，§5）。
- **reviewer APPROVE**：5-agent（code-reviewer / silent-failure-hunter / pr-test-analyzer / security-auditor / comment-analyzer）**verdict 無方向性分歧**，全 proceed to Reporter；新增 R-1~R-5 **無一 high-severity / 無一 code 缺陷** → 不觸發紅線。
- **HC-2（B1 同源，最敏感）三方獨立確認 airtight**：security-auditor 比 CAQA 更深一層 —— 不只驗「normal-user 分支不讀 hint + production 零 set-site」，還驗到 **Odoo framework substrate**：`request.env.context` 由 server 算的 `session.context` 組、**非** request body，`JsonRPCDispatcher` 把 JSON `params` 放 `request.params` plain kwargs **不 merge** 進 `request.env.context`，且 repo controller 無 `with_context(**kw)` 傳播 → **indirect injection（唯一可能藏身處）關閉**。這是「驗到 attacker 連間接路徑都沒有」的深度，不是「看起來沒問題」。

## 3. 3 條 takeaway 的 durable 落點

Phase D 的 novel learnings 各有持久化家（沿 patterns / pitfalls）：

1. **HC-2 trust-expansion（/simplify 自抓）** → [[constraint-input-trust-expansion]] §Forward-looking invariant（既有 pattern doc 擴充）。Phase D owner-bootstrap hint 初版對 normal-user 也可讀，/simplify pass 自抓「normal-user 信任 context hint = ownership-assignment 原語」→ 收緊成 SUPERUSER-only。**forward-invariant**：`bootstrap_owner_partner_id` 須 superuser-only + server-authoritative，任何 controller 不得從 `kw`/`params`/inbound context 填入（鎖未來 operator provisioning 真 set-site）。read-site comment（`project_project.py:119-135`）+ pattern doc 雙落（R-1）。
2. **credential 硬化的 test-fixture forward-coupling** → [[credential-claim-test-fixture-coupling]]（新 pattern）。硬化共用 token/secret/api_key 路徑 → 所有自建該 credential 的 test fixture lock-step 補 claim/secret，否則紅（好）或假綠（壞）。Phase B `_sign` / Phase C client-webhook fixture / Phase D `e94991d`（~6 operator + 3 Odoo setUp）3 建立點。
3. **cross-repo artifact 落點假設** → [[cross-repo-artifact-landing-assumption]]（新 pitfall）。plan 假設 Odoo secret env mount 落 `woow-paas-charts`（pod chart 在那），勘查發現實際落本 repo `charts/odoo-dev-sandbox`（Odoo dev-sandbox chart 在 app repo）。impl 前勘查 catch。

## 4. cross-phase 安全弧線 — constraint-input-trust-expansion 第 4 度兌現

Phase A 抽的 [[constraint-input-trust-expansion]] pattern 在 epic 內**四度出現，每次技術 surface 不同、抽象同一**（security 決策不信 attacker-controllable input，改用 authoritative source）：

| Phase | instance | attacker input | authoritative source（修法）|
|-------|----------|----------------|------------------------------|
| **A** | HC-3 grantor authority | `agent_template.project_ids` m2m write | Python `@api.constrains` per-element grantor check |
| **B** | HC-2 ws_server | WS envelope `payload` | signed token `claims`（§5.3 權威）|
| **C** | C-1 token_use | ws vs skill_bundle 隱性 token shape | explicit `token_use` claim（sign-first-then-verify）|
| **D** | HC-2 / F-PA-14 owner-bootstrap | `bootstrap_owner_partner_id` context hint | SUPERUSER-only + server-authoritative（normal-user 忽略）|

這驗證 reporter durable 的真正價值：pattern doc 不只記錄過去，而是**每個後續 phase 的 reviewer active lens** —— CAQA Phase D 把它當 known-pitfall 對照（「F-PA-14 是教科書級套用」），security-auditor 拿它 refute residual 路徑。Phase A 投資的 pattern doc 在 B/C/D 三度兌現成「主動抓 + 主動守」的工具。

> **epic 里程碑**：Phase D 收口 = **agent-runtime-pod epic Phase A-D code-ready 完成**。schema（A）+ image（B）+ wiring（C）+ enable-gate hardening（D）全 ship 到 develop（inert / flag-gated）。flip / 部署 / 觀察 / Phase E-G（rollout / cleanup / production CD）後續。

## 5. 剩給使用者的 flip-gate（code 已 ready，這些是部署 / runtime 驗收）

Phase D 關掉所有 **code** 前提，但有 3 類不是 code 能保證的，**flip `use_agent_runtime=True` 前必過**（詳見 plan §Pilot runbook flip-gate caveats + [[follow-ups]] §Phase D enablement gate）：

1. **runtime flip-gate 驗收**：F-PC-1 實機 WS connect（websockets 版本對不 `TypeError`）/ F-PC-5+F-PC-6 error 通道 + WS lifecycle（live 驗 operator error surface + 無 WS 洩漏）—— 靜態 pin / aclose code 已就位，runtime 半需真起 pod。
2. **D-CAVEAT-1 namespace secret 跨 3 進程同值**：F-PC-3 改 env 後，Odoo web/cron worker + operator 三進程須同值，否則 hash 靜默分歧 → webhook 永遠 404；charts `optional:true` 使 partial 部署不在 deploy 時炸。單一 K8s Secret 同值掛全部 + hash 消費點加 startup/health 一致性斷言。
3. **D-CAVEAT-2 + F-PC-7 cross-repo deploy-order**：operator 簽 `token_use` 必先於 pod 切 A 嚴格（1h 在途 token 窗口；反向 rollback 不安全且需先退 pod 回 B transitional）。且 **F-PC-7 的 A 嚴格目前在 `paas-agent-runtime` `feature/f-pc-7-token-use-strict`（`a8b3ce2`+`dabb275`），本 reporter 在 clone 未見其 merge 到 main**（最後 main merge 為 PR #2 `ba6b417` B transitional）→ merge + deploy 是 flip-gate 的硬前提。operator `/health` 宜暴露 `signs token_use:true` 供 checklist 機器驗。

carried-forward（OQ-D-2 leader confirm 刻意延後，非靜默丟棄）：C-4 webhook monotonic guard（enabler `last_state_event_ts` 已就位）/ C-6 operator project_hash authz / P-1 SSE worker-pin（pilot-gate）。

## 6. reporter phase 本 cycle 落地

- **本 report**：`docs/archived/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md`
- **design reconcile（OQ-D-1）**：design §2.15（auto-gen→env(b) 定案）/ §5.5（C-3 webhook dual-secret ✓）/ §5.6（A-1 pin 實況）/ §3.4（SHARED_SECRET fallback 已移除）對齊 impl
- **pattern / pitfall**：[[constraint-input-trust-expansion]] 補 HC-2 forward-invariant（+ 4th instance）；新 [[credential-claim-test-fixture-coupling]]；新 [[cross-repo-artifact-landing-assumption]]
- **R-1 / R-5 comment**（leader 指派順手補，comment-only 無邏輯改）：`project_project.py` read-site SET-SITE invariant 警告 / `agent_template_demo.xml` `is_public` vs draft 措辭 tidy
- **flip-gate runbook（OQ-D-3）**：plan §Pilot runbook 加 D-CAVEAT-1/2 完整 callout
- **follow-ups**：State 段 Phase D 行 + OQ-D-2 carried-forward confirm（C-4/C-6/P-1）+ R-2/R-3/R-4 follow-up + takeaway cross-ref
- **CLAUDE.md**：評估是否加 takeaway reference（見回報判斷）

## See also

- Phase D review（合成來源，不複製逐條 finding）：
  - `docs/archived/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md` §四面向 + §Caveat（D-CAVEAT-1/2）+ §Hand-off
  - `docs/archived/tracking/reviewer-report-2026-05-25-agent-runtime-pod-phase-d.md` §4 HC-2 airtight + §6 R-1~R-5 + §11 escalations
- Epic 前序 durable（A/B/C）：
  - `docs/archived/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md` §4 HC-2 cross-phase 同型
  - `docs/archived/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md` §4 cross-repo token_use 三端 sign-first-then-verify
- Pattern / pitfall（本 cycle 兌現 / 抽）：
  - [[constraint-input-trust-expansion]] §Forward-looking invariant — 第 4 度（owner-bootstrap）+ HC-2 forward-invariant
  - [[credential-claim-test-fixture-coupling]] — 硬化 credential 路徑的 test-fixture forward-coupling（B/C/D 3 建立點）
  - [[cross-repo-artifact-landing-assumption]] — charts 落點 plan 假設錯誤（勘查 catch）
- Flip-gate / follow-up：
  - `docs/plans/2026-Q2/agent-runtime-pod-plan.md` §Pilot runbook（flip-gate caveats D-CAVEAT-1/2 + 驗收）
  - `docs/tracking/followups/follow-ups.md` §Phase D enablement gate（must-fix close 狀態 + carried-forward + R-2~R-4）
- Cross-repo（Phase B image repo）：
  - `paas-agent-runtime` repo `feature/f-pc-7-token-use-strict`（`a8b3ce2`+`dabb275`）— F-PC-7 WS verifier A 嚴格（merge+deploy 屬 flip-gate）
