---
name: reviewer-report-2026-05-27-agent-runtime-fpilot10-code-prs
phase: reviewer
team: agent-runtime-pod
reviewer: agent-runtime-pod-reviewer
cycle: F-PILOT-10（real LLM — 三份 PR 完整 review）
prs:
  - { repo: paas-agent-runtime, number: 6, base: main, head_ref: pr6, kind: code-image }
  - { repo: woow_paas_platform, number: 76, base: develop, head: feature/f-pilot-10-client-llm-cred, kind: code-client }
  - { repo: woow_paas_platform, number: 77, base: develop, head: docs/agent-runtime-teardown-lifecycle, kind: docs }
analyzed_via: "temp clone /tmp/fp10-review-ar (PR#6) + floor read-only git show/diff + /tmp/fp10-pr76 dump (PR#76) + floor working-tree read (PR#77=current branch) — floor working tree 未動（保持 planner PR#77 branch）"
caqa_ref: docs/tracking/caqa-report-2026-05-27-agent-runtime-fpilot10-code-prs.md
created: 2026-05-27
status: approved
verdict: APPROVE（pilot stg merge；與 CAQA PASS WITH CAVEATS 對齊；3-agent + CAQA verdict 無方向性分歧；必查 empty-bearer lock-step 確認做對；C10-1 SSRF=leader cross-repo NP hard gate；新增 R10-1 observability 為 production-rollout gate follow-up）
related:
  - docs/tracking/caqa-report-2026-05-27-agent-runtime-fpilot10-code-prs.md
  - docs/tracking/reviewer-report-2026-05-27-agent-runtime-pod-phase-e0.md
  - docs/decisions/patterns/credential-claim-test-fixture-coupling.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
---

# Reviewer Report — F-PILOT-10 real-LLM（image PR#6 + client PR#76 + docs PR#77）

> **floor 保護**：全程未動 floor working tree（保持 planner PR#77 branch `docs/agent-runtime-teardown-lifecycle`）。PR#6 用 temp clone `/tmp/fp10-review-ar`（branch `pr6`，`git diff main...pr6`）；PR#76 用 floor read-only `git show origin/<branch>:<file>` + dump 到 `/tmp/fp10-pr76`；PR#77 = 當前 checkout branch，直讀 `git diff origin/develop...HEAD`。3 個 review sub-agent 皆嚴格指示不得 checkout/switch/reset floor。本報告為 docs/tracking/ 下新 untracked 檔（不混入 PR#77 tracked，留 reporter 統一 commit）。

## 1. Final Verdict

**APPROVE**（pilot stg merge）— 三份 PR 皆可進。verdict 與 CAQA **PASS WITH CAVEATS 對齊**，**3 code-agent（code-reviewer / pr-test-analyzer / silent-failure-hunter）+ CAQA verdict 無方向性分歧**（全 proceed to Reporter）→ 不觸發紅線。**0 unconditional blocker、0 退回任何 agent**。

**context 錨定**：F-PILOT-10 是 **pilot stg 驗證、非 production 對外**。據此分級——pilot 階段 0 blocker；數條 caveat 屬「真 LLM 對外（production rollout）前必收」的 gate item，本輪不阻 pilot merge。

**必查結果（headline，credential-claim-test-fixture-coupling pitfall）✓ 做對了**：PR#6 `test_cache.py` empty-bearer 硬化 lock-step **完整且 genuine**——`test_fetch_bundle_fail_closed_when_token_absent` 明確斷言空 token → `urlopen` **不被呼叫**（`pytest.fail` if called）+ `raises SkillFetchError`，happy-path 另測 Bearer header。**forward-coupling 獨立驗證乾淨**：test_loader 全 mock `ensure_skill`（或設 token），test_langgraph_runtime 從不碰 `_fetch_bundle` → **無其他 fixture 依賴舊 empty-token→401 路徑**。對比 Phase E0 的 key-absent 缺口，本輪是此 pattern 做對的範例。

核心控制 verified（複核 CAQA，不重做深掃）：
- **SSRF guard 用法正確（C10-1）** ✓：`_llm_stream` 的 `api_base` + `_build_mcp_tools` 的每個 MCP url **都過 `validate_url`**（兩個租戶可控 URL sink 皆 load-bearing）。殘留（DNS-to-metadata / redirect / trailing-dot）是 validator 內部限制（#52 in-progress defense-in-depth）+ 主防線 cross-repo NetworkPolicy → **leader hard gate**（見 §5）。
- **key-absent → echo fallback** ✓：`if not api_key: _echo_stream` → 無 LLM init、無 crash、只 echo user 自己訊息（不洩 env/credential）。
- **agent loop bound（C10-3）** ✓：`_MAX_TOOL_ROUNDS=8`；MCP `result_str` 無 cap（subprocess skill 有 256KB cap）為 perf caveat。
- **credential 不入 log** ✓：langgraph_runtime.py 零 logging、api_key 只傳 `ChatOpenAI(api_key=)`；client `_build_llm_credential` 無 api_key logging。
- **cross-PR contract coherent** ✓：client 送 `{model,api_base,temperature,max_tokens}` + `api_key`；pod `_load_llm_model_config` 讀同 4 key，defaults 兩端一致（gpt-4o-mini / api.openai.com/v1 / 0.7 / 4096），無 field drift。

## 2. PR 範圍

| PR | repo | branch | kind | scope |
|----|------|--------|------|-------|
| #6 | paas-agent-runtime | pr6 → main | code-image | 真 LLM runtime（langgraph_runtime +271 / loader +34 / cache +6 / envelope / ws_server）+ 5 test 檔（+912 total）|
| #76 | woow_paas_platform | feature/f-pilot-10-client-llm-cred → develop | code-client | client-side LLM credential（agent_runtime_client +58 / ai_assistant +15 / ws-protocol doc / 2 test）|
| #77 | woow_paas_platform | docs/agent-runtime-teardown-lifecycle → develop | docs | teardown lifecycle（design §8.4 +115 / plan Phase E0-T +52，docs-only sign-off）|

## 3. 委派工具 raw 輸出摘要（3 code-agent；security 由 CAQA 深審，複核不重做深掃）

> 縮減理由（記錄）：CAQA 已用 security-auditor + 19-payload SSRF battery 深審 security 面（SSRF / command-injection / credential-leak / DoS bound）。據 brief「你複核、不重做深掃」，我**不** re-dispatch security-auditor，改派 3 個 CAQA 未覆蓋面向的 agent（quality / test / silent-failure）+ 自讀複核 security。

| Agent | Verdict | 重點（additive to CAQA） |
|---|---|---|
| `pr-review-toolkit:code-reviewer` | approved with caveats / 0 blocker | agent loop 是 langchain canonical idiom（context7 驗 `gathered` 累積 + `bind_tools` + sync func via `ainvoke` threadpool）；cross-PR contract coherent 無 field drift；`_build_llm_credential` 正確。新：MAX_TOOL_ROUNDS 耗盡無 final answer（minor）+ 無對應 test（minor）+ loader 疑似 dead eager-builder（info）|
| `pr-review-toolkit:pr-test-analyzer` | approved with caveats | **必查 CONFIRMED**：empty-bearer lock-step genuine + forward-coupling 乾淨（test_loader mock ensure_skill / 設 token，test_langgraph 不碰 _fetch_bundle）；agent loop 行為測在 ChatOpenAI 邊界（對的 seam，非 mock-theater）；gaps G1 MAX_TOOL_ROUNDS=8 未測（4/10）+ G2 bad-JSON model_overrides 未測（3/10）皆 follow-up |
| `pr-review-toolkit:silent-failure-hunter` | no pilot blocker | **systemic 新發現**：langgraph_runtime.py **零 server-side observability**（audit_logger/logging 未用；client `_logger` 在 bad-JSON 路徑 defined-but-unused）→ 8 條 degradation 路徑（echo-fallback / config downgrade / tool failure / round exhaustion / MCP skip / overrides drop / keyless-vs-misconfigured）pod log 全不可見。pilot 可接受、**production rollout MAJOR**。另確認 MCP `get_tools()`/`validate_url` 失敗**正確 fail-loud**（#5b 做得好）|

**3-agent + CAQA verdict 無方向性分歧**：全 proceed to Reporter / approved with caveats。

## 4. CAQA Cross-Check（複核 security 深審，逐 caveat：同意 / 補充 / 反駁）

| # | CAQA 判定 | Reviewer 立場 | 依據 |
|---|---|---|---|
| **C10-1 SSRF**（conditional escalation → NP gate）| caveat + hard cross-repo NP gate | **同意，複核 PR#6 用法正確** | `validate_url` 在 api_base + 每個 MCP url **load-bearing**（自讀確認）；殘留 DNS/redirect/trailing-dot 是 validator 內部 + 主防線 cross-repo NP。**非 code-merge blocker for pilot**；真 cluster 部署前 leader 須向 paas-charts 確認 NP backstop（CNI enforce egress NP + default-deny 排 link-local/metadata），未確認→升 HIGH（見 §5）|
| C10-2 interpreter allowlist | caveat（v1 author 可信）| **同意** | sha256-pinned 但 author-controlled；v1 trust 內可接受；authorship 開放前收。**真 LLM 對外前建議收** |
| C10-3 agent loop byte cap | caveat（真 LLM 對外前收）| **同意 + 補充** | 確認 MCP `result_str` 無 cap（langgraph_runtime `_run_tool`）；與 R10-1 observability 相關（cost 放大不可見）。**真 LLM 對外前收** |
| C10-4 MCP transport allowlist | caveat | **同意** | `if not url: continue` safe-by-omission 確認；補 explicit `transport in {http,sse}` 更穩。follow-up |
| C10-5 model_overrides key allowlist | info→low | **同意** | client merge 無 key allowlist，但 pod `_load_llm_model_config` 只讀 4 key → 額外 key pod-side inert。低 follow-up |
| 4 info（SHARED_SECRET dead path / credential-at-rest / trailing-dot parity / redirect）| info | **同意** | SHARED_SECRET fallback dead（Odoo A-strict 401 fail-closed 非 bypass），建議移除 → 併 R10-4 |

**反駁：0 條。** CAQA security 深審紮實、severity 校準合理（trailing-dot 從 auditor 近-blocker 降 info/parity 的校準我同意）。

## 5. C10-1 SSRF — leader cross-repo hard gate（複核確認，非本輪 code blocker）

- **PR#6 code 端正確**：兩個租戶可控 URL sink（LLM `api_base`、MCP server `url`）**都過 `validate_url`**；`cache.py` 的 `urlopen` 打 admin-controlled `ODOO_BASE_URL`（正確地不過 tenant-URL validate）。**無未驗租戶-URL sink**（複核 CAQA 同源 sink 盤點，確認）。
- **殘留（#52 in-progress，當 in-progress 看待）**：validator 不 resolve DNS（DNS-name-to-metadata）+ HTTP client 未關 redirect-following + trailing-dot parity → image agent #52 補 defense-in-depth（關 redirect + trailing-dot 正規化）。
- **主防線 = cross-repo NetworkPolicy（paas-charts，leader 另派）**：**真 cluster 部署 F-PILOT-10 前**，leader 須向 paas-charts 取得確認：(a) production CNI 真 enforce egress NP（Calico/Cilium ✓；flannel 不 enforce → NP no-op）；(b) agent-runtime pod egress default-deny 且 link-local/metadata 不在 allow CIDR。**確認到位 → 維持 caveat；未確認/不成立 → 升 HIGH，退回 image agent（#52）+ paas-charts（補 NP）**。
- **pilot stg 判斷**：mock 層綠、尚未上真 cluster（NP 確認本就是 cluster 驗收一環）；woow self-hosted k3s 很可能無 cloud IMDS（metadata-theft 大幅 moot，內網 RFC1918 SSRF 仍在）→ **非 pilot code-merge blocker**，是 deploy gate。complete 同意 CAQA conditional-escalation 框架。

## 6. CAQA 未深入 / 補充的新發現（R10-1~R10-4；無 pilot blocker）

| # | 來源 | pilot / production | 位置 | 摘要 | 方向 |
|---|---|---|---|---|---|
| **R10-1**（headline 新發現）| silent-failure-hunter | acceptable / **MAJOR** | `langgraph_runtime.py`（全檔零 log/audit）+ client `agent_runtime_client.py:58-64` | 真 LLM degradation 路徑（echo-fallback / config downgrade / tool failure / round exhaustion / MCP skip / overrides drop）**無 server-side observability**；`audit_logger`（TOOL_UNAVAILABLE/ERROR 已定義）+ logging 未用 → rollout 時這些在 pod log 全不可見（misconfig real-LLM 靜默降級成 echo 無訊號）| **production rollout 前 mandatory**：degradation 路徑補 `emit_event`/`_logger.warning`（echo-fallback / bad-config / tool-error / max-rounds 各一）|
| R10-2 | code-reviewer + pr-test-analyzer | minor | `langgraph_runtime.py` `_llm_stream` loop | MAX_TOOL_ROUNDS=8 耗盡 → 無 final natural-language answer（dangling tool frames + done）+ 無 test 覆蓋此 path | follow-up：耗盡時 emit synthetic「已達工具上限」text_chunk + 補 test |
| R10-3 | pr-test-analyzer | minor | client `_build_llm_credential` | bad-JSON `model_overrides` guard 存在但無 test（CL.6b 只測 valid merge）| follow-up：補 bad-JSON 負例（cheap，與 CL.6b 同位）|
| R10-4 | code-reviewer | info | loader.py eager builder + `cache.py:74` SHARED_SECRET | 疑似 dead code（新 lazy flow 只用 `build_lazy_skill_tool`；eager `build_skill_tool`/`get_skill_tool`/`_TOOL_CACHE` 無 caller）+ SHARED_SECRET fallback dead path | cleanup pass 確認/移除（非阻斷）|

**關鍵判斷**：R10-1~R10-4 **無一 high-severity 安全洞、無一 pilot blocker**。R10-1 是最有價值新發現（CAQA credential-not-in-log 是「不洩漏」、此處是「degradation 不可見」的 operability gap，互補非重複），但其嚴重度錨在 **production rollout**，pilot stg 可接受 → **不觸發「CAQA 漏 high-severity → escalate」紅線**，列 production-rollout gate follow-up。

## 7. PR#77 docs 一致性核（lightweight，docs-only sign-off）

- **design §8.4**（+115）：holistic teardown lifecycle，grounding（3 類 out-of-band resource 無 owner-ref 不被 helm uninstall 回收）+ 生命週期錨定（secret = ns/release 壽命非 pod 壽命，reap 不清）+ **4 拍板 sign-off**：D1-b（隨 ns teardown 清非 reap 清）/ D2-a（`delete_runtime` = helm uninstall + kubectl delete ns 雙步冪等）/ Gap2（GC 擴掃 paas-managed 無 release 孤兒 ns）/ StorageClass=`longhorn` Retain（delete ns → PV 保留 Released state 可救）。§2.6 原 `longhorn-delete` 方案 X 正確標 **SUPERSEDED 2026-05-27** 交叉引用 §8.4.5。
- **plan Phase E0-T**（+52）：6 條 TDD red 入口（T10.1-T10.6）對應 §8.4.9 AC + 3 invariant（INV-1 無殘留明文 secret / INV-2 reap 不清 / INV-3 PV Retain recoverable）+ 跨 repo workstream（WS-10a operator delete ns / WS-10b GC 擴掃 / WS-10c chart longhorn）+ **impl gate（不可逆 delete ns + chart SC 落地前回 leader）**+ rollback。
- **一致性**：design ↔ plan ↔ CAQA E0-CAVEAT-1 三方對齊；hard gate 定位一致（echo keyless 零 orphan 非 blocker、真 LLM 上線前必收）。**docs 一致性 PASS** ✓。是 planner 對我 Phase E0 reviewer + CAQA E0-CAVEAT-1 的正確回應。

## 8. Plan Alignment

兩 code PR 對齊 F-PILOT-10 pilot-pinned 方向（mock TDD 5 切片 + 4 拍板 + Gap-3 hybrid）：§6 key-absent→echo / present→ChatOpenAI via validated api_base；§4a（text payload）vs §4b（tool_* top_level）frame split；§3.5 lazy-fetch（register 不 fetch）；§8.2 credential 隨 spawn body。PR#76 收掉 F-PILOT-8 client-side 鏈漏（Odoo 經 spawn 送 api_key）。**無 implementation 走偏**（E0-CAVEAT-1 teardown 由 PR#77 design 處理，不在兩 code PR scope）。

## 9. 同源 code path 檢查（複核 CAQA + 補充）

- **outbound HTTP sink（3）**：ChatOpenAI（validated api_base）/ MultiServerMCPClient（validated url）/ cache `urlopen`（admin `ODOO_BASE_URL`，正確不過 tenant-validate）→ 無未驗租戶-URL sink。
- **subprocess spawn（2）**：`build_lazy_skill_tool._run` + 既有 path 皆走 `spawn_skill_subprocess` 單一 chokepoint（list-args + stdin JSON，LLM kwargs → `input_args` → stdin 非 argv）→ 無第二注入路徑。
- **credential-passing path（1）**：只有 `_build_llm_credential` → spawn body；`llm_model_config` 命名正確避 operator-side pydantic 保留字。
- **observability sink（0）**：langgraph_runtime degradation 路徑無 log/audit sink（R10-1 根因）。

## 10. Known Pitfalls 對照

| Pitfall / pattern | 對照結果 |
|---|---|
| [[credential-claim-test-fixture-coupling]] | **做對了** — empty-bearer 硬化 lock-step genuine（negative 明確斷言 urlopen 不呼）+ forward-coupling 乾淨（無其他 fixture 依賴舊路徑）；PR#76 client `_build_llm_credential` 的 CL.2 lock-step（model_config_id=None）為 Odoo-client analog，亦正確 |
| [[constraint-input-trust-expansion]] | `model_overrides`（owner/admin 可寫）merge 無 client key allowlist（C10-5），但 pod-side inert（只讀 4 key）→ 低風險 |
| [[cross-repo-artifact-landing-assumption]] | C10-1 主防線 NP 落 paas-charts（cross-repo）；PR#77 teardown WS-10c chart SC 亦 cross-repo → leader 跨 repo gate（§5 + PR#77 impl gate）|

## 11. Caveat 收 / follow-up 拆分（給 leader；context = pilot stg 非 production）

**本輪 pilot merge：0 mandatory blocker。** 三份 PR 可進。

| 階段 gate | items |
|---|---|
| **真 cluster 部署前（leader cross-repo gate）** | **C10-1**：leader 向 paas-charts 確認 NP egress backstop（CNI enforce + default-deny 排 link-local/metadata）；未確認→升 HIGH 退 image #52 + paas-charts。建議 image agent 順手 #52 關 redirect + trailing-dot（便宜 defense-in-depth）|
| **真 LLM 對外前（production rollout gate）** | **R10-1**（degradation observability — 最該收）、**C10-3**（agent loop byte cap）、**C10-2**（interpreter allowlist）|
| **follow-up（較低）** | C10-4（MCP transport allowlist）、C10-5（model_overrides key allowlist）、R10-2（max-rounds signal + test）、R10-3（bad-JSON overrides test）、R10-4（dead code: loader eager-builder + SHARED_SECRET fallback cleanup）|

**durable 建議**：R10-1 / C10-1 / C10-2 / C10-3 進 `docs/tracking/followups/follow-ups.md`（F-PILOT-10 cycle，標 production-rollout gate）。

## 12. Open Questions for Leader / Recommendation

- **verdict：APPROVE**（pilot stg）→ leader 可推 reporter（3 份 PR）。
- **無紅線觸發**：reviewer verdict（APPROVE）= CAQA（PASS WITH CAVEATS）；3-agent 無方向性分歧；必查 empty-bearer lock-step 確認做對；新發現 R10-1~R10-4 無 high-severity 安全洞（R10-1 是 production-rollout operability gate，非 pilot security blocker）→ 不 escalate verdict / 不退回 agent。
- **C10-1 是唯一 leader 必動項**：真 cluster 部署前向 paas-charts 取得 NP backstop 確認（§5）。
- **無 request changes / 無退回 agent**：兩 code PR 正確實作 pilot-pinned design，docs PR sign-off 一致。
- **temp 材料**：`/tmp/fp10-review-ar`（PR#6 clone）+ `/tmp/fp10-pr76`（PR#76 dump）為 review 暫存，reporter 收尾後可清。floor working tree 全程未動。

## 13. Sign-off

- **Verdict**：**APPROVE**（pilot stg merge）— 3 份 PR 可進。
- **必查（credential-claim-test-fixture-coupling）**：empty-bearer lock-step **做對了**（genuine + forward-coupling 乾淨），對比 Phase E0 key-absent 缺口是正面範例。
- **CAQA cross-check**：C10-1~C10-5 + 4 info 全同意（反駁 0）；security 深審複核確認 PR#6 validate_url 用法 load-bearing。
- **新增**：R10-1（observability，production-rollout gate）+ R10-2/3/4（minor/info follow-up）。
- **3-agent**：verdict 無方向性分歧。
- **PR#77 docs**：一致性 PASS（design §8.4 ↔ plan E0-T ↔ CAQA E0-CAVEAT-1 對齊）。
- **Recommendation**：proceed to Reporter（3 PR）；leader 真 cluster 部署前收 C10-1 NP backstop（cross-repo gate）；R10-1/C10-2/C10-3 列真 LLM 對外前 production-rollout gate；其餘 follow-up。
