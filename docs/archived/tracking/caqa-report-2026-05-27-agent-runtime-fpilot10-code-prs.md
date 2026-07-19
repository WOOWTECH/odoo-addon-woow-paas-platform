---
name: caqa-report-2026-05-27-agent-runtime-fpilot10-code-prs
phase: caqa
team: feature-agent
analyst: agent-gaps-caqa
epic: agent-runtime-pod
cycle: F-PILOT-10（real LLM — 兩個 code PR security analyze）
scope: 2 code PR（image PR#6 paas-agent-runtime + client PR#76 woow_paas_platform）；docs PR#77 不需 caqa
prs:
  - { repo: paas-agent-runtime, number: 6, base: main, head_ref: pr6, range: "main...pr6" }
  - { repo: woow_paas_platform, number: 76, base: develop, head: feature/f-pilot-10-client-llm-cred }
analyzed_via: "temp clone /tmp/fp10-ar (PR#6) + floor read-only git show/diff (PR#76) — floor working tree 未動（planner PR#77 branch docs/agent-runtime-teardown-lifecycle 保持）"
created: 2026-05-27
verdict: PASS WITH CAVEATS
counts: { blocker: 0, conditional_escalation: 1, caveat: 5, info: 4 }
tools: [security-auditor, direct-diff-read, ssrf-payload-battery]
related:
  - docs/archived/tracking/caqa-report-2026-05-27-agent-runtime-pod-phase-e0.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/decisions/patterns/credential-claim-test-fixture-coupling.md
status: archived
archived-from: docs/tracking/caqa-report-2026-05-27-agent-runtime-fpilot10-code-prs.md
archived: 2026-07-19
archived-reason: "一次性 CAQA 驗收報告，cycle 已收官；patterns/pitfalls 證據錨點引用已改指本處"
---

# CAQA Report — F-PILOT-10 real-LLM 兩個 code PR（image PR#6 + client PR#76）

> **scope**：F-PILOT-10 mock 層全綠，三 PR 待 review；本報告只分析兩個 **code** PR（docs PR#77 = 我 E0-CAVEAT-1 衍生的 teardown design，不需 caqa）。這是 **pod sandbox 安全核心（B-series）**：真 LLM（ChatOpenAI）+ agent tool-loop + MCP + skill-bundle fetch + client 端 LLM credential 注入。
>
> **威脅模型**：租戶可控 LLM config（api_base / model）+ MCP server url + skill snapshot；惡意租戶是真實 SSRF / sandbox-escape 對手。v1 trust model = user-trusted skill author + admin-managed infra。
>
> **floor 保護**：分析全程未動 floor working tree（planner PR#77 branch `docs/agent-runtime-teardown-lifecycle`）—— PR#6 用 temp clone `/tmp/fp10-ar`，PR#76 用 floor read-only `git show origin/<branch>:<file>` / `git diff origin/develop...origin/<branch>`。本報告為 docs/tracking/ 下**新 untracked 檔**，不混入 PR#77 tracked 內容（留 reporter 統一 commit）。
>
> **方法**：security-auditor 深審（含 19-payload SSRF 實測）+ 兩 PR diff 逐行直讀 + 我對 sub-agent 發現做獨立 severity 校準。

## Verdict

**PASS WITH CAVEATS** — 0 unconditional blocker / **1 conditional-escalation（SSRF，hard cross-repo gate）** / 5 caveat / 4 info。

四個 spec 宣稱的核心控制**全部驗證到位**：tool 執行 sandbox（**無 command injection** — list-args + stdin JSON + sha256-pinned entrypoint）、fail-closed（empty-bearer 拒）、credential 不入 log（**兩端清白**）、agent-loop DoS bound（MAX_TOOL_ROUNDS=8）。0 處發現可利用的硬阻斷。**唯一需 leader 拍板的是 SSRF guard 的殘留缺口** —— 它是 defense-in-depth 層、主防線是 cross-repo NetworkPolicy；**真 cluster 部署前必須確認 NP 擋 link-local，否則升 high**。

---

## 四面向結果

### Security（核心，逐 leader focus 對齊）

- **[focus SSRF — CONDITIONAL ESCALATION C10-1] url_validator 殘留 SSRF，severity 押 cross-repo NP backstop。**
  - **IP-literal 覆蓋紮實**（`url_validator.py`）：拒非 http(s) scheme、link-local（169.254/16 + fe80::/10）、AWS IMDSv2 IPv6（fd00:ec2::254）、**decimal/hex/octal/IPv4-mapped 繞過全擋**（`inet_aton` 正規化，HC-1）、`parsed.hostname` 無 userinfo `@` 繞過。19-payload 實測除下列外全擋。
  - **真正殘留 = DNS-to-metadata（已 documented，follow-up #52）**：validator 明示不 resolve DNS（`url_validator.py:13-14,35`）→ 租戶可控的 DNS 名解到 `169.254.169.254` / 內網 RFC1918 通過 literal 檢查，pod 連線時才解析命中。**這是真實可利用面**（attacker 控 DNS 名 → 指 metadata）。另 redirect-following（ChatOpenAI/httpx + MCP client 預設跟 3xx）= post-validation SSRF 同類。
  - **trailing-dot `169.254.169.254.`（auditor 標近-blocker，我校準為 info/parity）**：literal 確被當 hostname 放行（docstring「inet_aton 與 OS resolver 同套」claim 不精確），**但**獨立可利用性低 —— Linux glibc 對絕對式 numeric 走 DNS → NXDOMAIN → 連不到 metadata。屬 docstring parity nit，非獨立 exploit；真風險仍是上面的 DNS-name-to-metadata。
  - **為何是 caveat 非 unilateral blocker**：(1) url_validator 明示是 **defense in depth**（docstring），主 egress 控制是 **NetworkPolicy（cross-repo paas-charts §6）**；(2) F-PILOT-10 **mock 層綠、尚未上真 cluster**（NP 確認本就是接下來 cluster 驗收的一環）；(3) woow 跑 self-hosted k3s，**很可能無 cloud IMDS endpoint**（metadata-creds-theft 在此 infra 大幅 moot，但內網 RFC1918 SSRF 仍在）。
  - **HARD GATE（leader 必須取得 paas-charts 確認）**：真 cluster 部署 F-PILOT-10 前確認 (a) production CNI **真的 enforce egress NetworkPolicy**（Calico/Cilium 會、flannel **完全不 enforce → NP 變 no-op**）；(b) agent-runtime pod egress 是 default-deny 且 link-local/metadata **不在任何 allow CIDR**。**若 NP 不 backstop → 此項升 HIGH/blocker（退回 image agent + paas-charts）**。
  - **便宜的 defense-in-depth（建議 image agent 順手補，非阻斷）**：pod HTTP client 關掉 redirect-following；url_validator 補 trailing-dot 正規化；MCP/LLM client 改用「resolve 後再驗 IP」或限制 connect 目標。納入 #52 scope。
- **[focus fail-closed] 正確。** `cache.py:77-80` empty token → `raise SkillFetchError`（拒送空 Bearer，304ee13）✓。key-absent → `_echo_stream`（`langgraph_runtime.py:135-139`）只回 echo 使用者自己的訊息、**不洩任何 env/credential/內部狀態**，且 `_load_llm_model_config` 吞壞 JSON → 預設不 crash ✓。
- **[focus credential 不入 log] 兩端確認清白（affirmative no-finding）。**
  - image：4 個改動檔（langgraph_runtime / url_validator / cache / loader）**零 logging 語句**；api_key 只當 `_llm_stream` local 傳給 `ChatOpenAI(api_key=...)`，從不進 Event/payload/top_level frame；tool 例外 `str(exc)` 來自 subprocess dict / MCP（env-whitelist 已排除 secret）。
  - client（PR#76）：`_build_llm_credential` 無 logging；`_request` 不 log `json_body`；error 只帶 `(method, path, status, detail)` 無 body/key；controller `_logger.exception` + SSE surface 皆無 api_key。
- **[focus tool sandbox — command injection 風險 CLEARED]** `subprocess_runner.spawn_skill_subprocess`（Phase B 既有，本 PR 新餵 LLM args 進去）：`subprocess.run([skill_path, *args])` **list-args、無 shell=True、無字串插值**；LLM `**kwargs` → `input_args` → **stdin JSON（`input=`）非 argv**（`loader.py` → `subprocess_runner.py:83`）→ LLM 無法注入 argv/shell；entrypoint/runtime 來自 **sha256-verified** SKILL.md（`cache.ensure_skill` 先驗後解）；tarbomb/traversal 由 `filter="data"` 擋；rlimits（CPU/RSS/NOFILE/NPROC/FSIZE）+ env-whitelist（排除 secret）+ wall timeout + stdout cap 齊。**強 sandbox**。
- **[C10-2 / caveat] skill `runtime`/`entrypoint` sha256-pinned 但 author-controlled、未 interpreter-allowlist。** 惡意 skill author（**超出 v1 trust：author 可信**）可把 `runtime` 設成任意 PATH executable（如 `/bin/sh`）。v1 trust model 內可接受（rlimits + env-whitelist + readOnlyRootFs 圈住），但 authorship 一旦開放即潛在 escalation。方向：allowlist 已知 interpreter。
- **[C10-3 / caveat] agent loop 單請求內無 byte cap。** `full_response`（`langgraph_runtime.py:223 +=`）+ `messages`（每輪 append gathered AI msg + 每 tool_call 一個 ToolMessage）+ MCP tool `result_str`（`:303 json.dumps` 整個 result，**MCP ainvoke 回傳無 cap**，subprocess skill 結果有 256KB cap）→ 8 輪 × N tool_calls 可在記憶體膨脹並每輪重送 LLM。非 remote-crash，但惡意 MCP-server config 可驅動 memory/cost 放大。方向：cap 每 tool-result + 總量。
- **[C10-4 / caveat] MCP transport safe-by-omission。** `langgraph_runtime.py:188 if not url: continue` → stdio+command 型 MCP entry（無 url）被丟棄不 spawn（安全），但靠 url-presence 而非 explicit `transport in {http,sse}` allowlist。方向：補明確 transport 白名單更穩健（現況安全）。
- **[C10-5 / info→low] `model_overrides` 無 client-side key allowlist（constraint-input-trust-expansion）。** owner/admin 可寫的 JSON merge 進 model_config（`agent_runtime_client.py:60`）；pod 端 `_load_llm_model_config` 只讀 4 個已知 key → 額外 key **pod-side inert**（end-to-end 存而不讀）。低風險，建議 client 端 allowlist override key。
- **[info] `SHARED_SECRET` fallback（`cache.py:74`）dead path。** `SKILL_BUNDLE_TOKEN or SHARED_SECRET` → 若只有 SHARED_SECRET 會送出，但 Odoo skill-bundle A-strict（assert token_use=='skill_bundle'）回 401 → fail-closed（非 bypass）。footgun（未來 endpoint 放寬即活）→ 建議移除。
- **[info] credential-at-rest（pre-existing，非本 PR）。** `ai.config.api_key` 是明文 `Char`；明文經 spawn body 傳 operator（design §8.2 operator 不查 DB）+ per-release Secret。確認 v1 posture（Odoo K8s secret 非 vault）已接受。

### Quality

- [info] `_build_llm_credential`（`agent_runtime_client.py:42-65`）乾淨、註解完整（鏡像 `ai_client.from_assistant` 預設、F-PILOT-8 鏈、pydantic 保留字避雷 `llm_model_config`）。`_load_llm_model_config` 壞 JSON → 預設不 crash ✓。
- [info] code comment 優異 —— 把拍板（key absent=echo）、§4a/§4b frame split、§3.5 lazy-fetch、契約編號寫進註解，self-documenting。
- [info] redirect-following 未關（折入 C10-1 defense-in-depth）。

### Performance

- 主要 perf 面 = **C10-3 累積無 cap**（memory/cost 放大）。其餘為 per-request streaming，受 MAX_TOOL_ROUNDS=8 bound。MCP tool 結果無 cap 是放大主因。
- 註冊不 fetch（lazy）→ 每次 stream 重讀 snapshot 組 tool_set（無 stale，天然滿足 SC-4 reload），cost 可接受。

### Architecture

- [info] 兩 PR **對齊拍板方向**：§6 key-absent→echo / present→ChatOpenAI via validated api_base；§4a（text_chunk/done payload）vs §4b（tool_* 頂層 top_level）frame split；§3.5 lazy-fetch（`build_lazy_skill_tool` 註冊不 fetch）；§8.2 credential 隨 spawn body、operator 建 Secret。Gap-3 hybrid（single-string input fallback、description host-owned、rich parameters 延後）兩 repo 一致。**無走偏**。
- [info] url_validator 是 defense-in-depth 層（NP 為主控制）—— 架構合理 **前提是 NP 到位**（= C10-1 gate）。
- [info] 單一安全 subprocess chokepoint（2 spawn 點都走 `spawn_skill_subprocess`）+ 單一 credential-passing path（`_build_llm_credential` → spawn body）。乾淨。

---

## Plan alignment check

兩 PR 皆**對齊** F-PILOT-10 pilot-pinned 方向（mock TDD 5 切片 + 4 拍板），無 implementation 走偏。PR#76 收掉我記憶中標記的 F-PILOT-8 client-side 鏈漏（Odoo 經 spawn 送 api_key）。PR#6 實作 §5.8 真 LLM runtime。**無 plan-vs-impl 落差**（E0-CAVEAT-1 teardown 已由 PR#77 design 處理，不在本兩 PR scope）。

---

## 同源 code path 檢查

- **outbound HTTP sink（3）**：ChatOpenAI（validated api_base `:204`）、MultiServerMCPClient（validated url `:190`）、`cache.py:87` urlopen（打 admin-controlled `ODOO_BASE_URL`，非租戶 URL → 正確地**不**過 validate_url）。**無未驗的租戶-URL sink**。
- **subprocess spawn（2）**：`loader.py` 兩處都走 `spawn_skill_subprocess` 單一安全 chokepoint；runtime 內無其他 `subprocess`/`Popen`/`os.system`/`shell=True`。
- **credential-passing path（1）**：只有 `_build_llm_credential` → spawn body；`llm_model_config` 命名正確（避開 operator-side pydantic 保留字 `model_config`）。無第二條帶 key 路徑。

---

## Known pitfalls 對照

- **`credential-claim-test-fixture-coupling`**：`cache.py` empty-bearer 硬化正是此 pattern（secret/token optional→required fail-closed）→ pod-side 任何自建 skill-bundle path 的 fixture 現須供 `SKILL_BUNDLE_TOKEN` 或撞新 raise。**reviewer 須確認** PR#6 的 `tests/test_cache.py`（+73）已 lock-step、無 fixture 依賴舊的 empty-token→401 路徑（現提前 raise）。
- **`constraint-input-trust-expansion`**：`model_overrides`（owner/admin 可寫）merge 進 model_config 無 key allowlist（C10-5）；pod-side inert（只讀 4 key）→ 低風險，但 operator 不應盲信額外 key。
- 無 `cross-workspace-task-data-leak`（B1）/ ir.rule 對稱性回歸（本兩 PR 不動 project/task ACL）。

---

## Blocker 清單（給 leader / 對應 agent）

**0 unconditional blocker。** 但 **1 conditional escalation**：
- **C10-1（SSRF，→ image PR#6 url_validator + cross-repo paas-charts）**：DNS-to-metadata / redirect SSRF 殘留，literal guard 非完整防禦。**若 paas-charts 端 NetworkPolicy egress 未確認 backstop（CNI 不 enforce NP，或 egress 未 default-deny 排除 link-local/metadata）→ 升 HIGH/blocker，退回 image agent（補 url_validator + 關 redirect）+ paas-charts（補 NP）**。確認 backstop 到位 → 維持 caveat。**leader 須向 paas-charts 取得此確認再放行真 cluster 部署**。

## Caveat 清單（給 leader 決定帶不帶進 reviewer）

| # | repo / file:line | issue | 方向（非 patch） |
|---|------------------|-------|------------------|
| **C10-1** | image `url_validator.py` + paas-charts §6 NP | DNS-to-metadata + redirect SSRF；literal guard 非完整，押 NP backstop | hard gate 確認 NP 擋 link-local；image 順手關 redirect-following + trailing-dot 正規化（#52） |
| **C10-2** | image `loader.py` / `subprocess_runner.py:87` | skill `runtime`/`entrypoint` sha256-pinned 但 author-controlled、未 interpreter-allowlist | v1 可接受（author 可信）；authorship 開放前 allowlist interpreter |
| **C10-3** | image `langgraph_runtime.py:223,303` | agent loop full_response/messages/MCP result_str 無 byte cap → memory/cost 放大 | cap 每 tool-result + 總量 |
| **C10-4** | image `langgraph_runtime.py:191` | MCP transport safe-by-omission（url-presence 非 explicit allowlist） | 補 `transport in {http,sse}` 白名單 |
| **C10-5** | client `agent_runtime_client.py:60` | `model_overrides` 無 client-side key allowlist（inert pod-side，低） | client 端 allowlist override key |

**info（4）**：`cache.py:74` SHARED_SECRET fallback dead-path（建議移除）；credential-at-rest 明文（pre-existing，確認 v1 posture）；trailing-dot docstring parity 不精確（非獨立 exploit）；redirect-following 未關（折入 C10-1）。

---

## 工具 raw 輸出摘要

- **security-auditor**（雙 PR 深審 + 19-payload SSRF 實測 + ACL 證據）：0 blocker；SSRF literal 覆蓋紮實但 DNS/redirect/trailing-dot 殘留（押 NP）；fail-closed 正確；credential 兩端零 logging；tool sandbox 無 command injection（list-args + stdin JSON + sha256 pin + rlimits）；agent-loop 累積無 cap = perf caveat；runtime 未 allowlist = sandbox caveat。實證 `mcp_server.url` + `agent_template.model_overrides` 皆 owner/admin RWCU → SSRF 面租戶可達（驗證 url_validator 為 load-bearing）。
- **我的獨立校準**：trailing-dot 從 auditor 的「近-blocker」校準為 info/parity（Linux glibc 絕對式 numeric 走 DNS→NXDOMAIN，非獨立 exploit）；真 SSRF 殘留聚焦 documented #52 的 DNS-name-to-metadata；C10-1 定為 caveat + hard cross-repo NP gate（非 unilateral blocker，因 defense-in-depth + mock 未部署 + k3s 很可能無 IMDS）。
- **direct diff read**：PR#6 langgraph_runtime（LLM init / agent loop / MCP / tool exec）+ url_validator + cache + loader 逐行；PR#76 agent_runtime_client `_build_llm_credential` + ai_assistant `_build_skill_snapshot`。

---

## Hand-off

- **狀態**：F-PILOT-10 兩 code PR security CAQA = **PASS WITH CAVEATS**（0 unconditional blocker / 1 conditional-escalation / 5 caveat / 4 info）。四面向全覆蓋。floor working tree 未動。
- **需 leader 動作**：
  1. **C10-1 是關鍵拍板**：向 paas-charts 取得 NetworkPolicy egress backstop 確認（CNI enforce NP + egress default-deny 排除 link-local/metadata）**再放行真 cluster 部署**。確認到位 → caveat 進 reviewer；未確認/不成立 → 升 high 退回 image agent（補 url_validator 關 redirect + trailing-dot）+ paas-charts（補 NP）。建議無論如何讓 image agent 順手把 redirect-following 關掉 + trailing-dot 正規化（便宜 defense-in-depth，納 #52）。
  2. C10-2/3/4（image PR#6）+ C10-5（client PR#76）帶進 reviewer 評估；C10-3（accumulation cap）與 C10-2（interpreter allowlist）建議至少在真 LLM 對外開放前收。
  3. reviewer 提醒：核 PR#6 `test_cache.py` fixture 是否 lock-step empty-bearer 硬化（credential-claim-test-fixture-coupling）。
- **next**：回報 leader → 過了進 reviewer。
