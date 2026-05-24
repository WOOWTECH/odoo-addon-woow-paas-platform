---
name: caqa-report-2026-05-25-agent-runtime-pod-phase-d
phase: caqa
team: feature-agent
analyst: agent-runtime-pod-caqa
epic: agent-runtime-pod
cycle: Phase D（enable-gate caveat hardening）
diff_base: a9a4d1f
branch: feature/agent-pod-phase-d
commits: [5398f92 red, 8d152f7 fix, e2f1624 feat, e94991d test]
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
input_report: docs/tracking/test-reports/test-coverage-agent-runtime-phase-d-2026-05-24.md
prior_caqa: docs/tracking/caqa-report-2026-05-24-agent-runtime-pod-phase-c.md
created: 2026-05-25
status: pass-with-caveats
verdict: PASS WITH CAVEATS
counts: { blocker: 0, caveat: 2, info: 9, carried_forward: 3 }
tools: [security-auditor, architect-reviewer, direct-diff-read]
related:
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/decisions/patterns/phase-versioned-test-assertion.md
---

# CAQA Report — Agent Runtime Pod Phase D（enable-gate caveat hardening）

> Phase D 是 **Phase C CAQA caveat 的逐條 remediation**：把 flip `use_agent_runtime` flag 前的所有 CODE 缺陷（runtime / security / leak）關掉，備好 admin 啟用面 + cross-repo wiring。**不 flip flag、不起 pod、不部署**。所有改動 flag-gated（default False，從未部署）。
>
> 本報告驗證兩件事：**(A) 每條 fix 真的關掉它的目標 caveat**；**(B) fix 本身有沒有引入新風險**（特別是 F-PC-4 global fail-closed 的 blast radius、HC-2 收緊的 trust boundary 殘留）。
>
> **方法**：本輪以角色委派優先序 #3/#4 的兩個 specialist（security-auditor 深審 HC-2 / F-PC-4 / F-PC-3；architect-reviewer 審 token_use 3-端對稱 + dual-secret pattern + 漂移）+ 對 27 檔 diff 逐檔直讀，三方三角覆蓋 quality/security/performance/architecture 四面向。

## Verdict

**PASS WITH CAVEATS** — 0 blocker / 2 caveat（net-new）/ 9 info / 3 carried-forward（待 disposition 確認）。

11 條 Phase D must-fix **全部正確關掉其目標 caveat**，無一走偏；fix 皆 fail-closed、additive、無結構漂移。2 條 net-new caveat **都不是 code 缺陷**，而是 cross-process / cross-repo 的「部署 invariant 未被 code 強制」風險，且全 flag-gated → 適合帶入 reviewer + 收進 Phase D flip-gate / runbook，**不阻斷**。與 Phase A/C verdict 同調。

---

## 四面向結果

### Quality — 無 blocker，3 info

- [info] `src/controllers/ai_assistant.py:738-742` — **F-PC-6 aclose 的 best-effort except 落 `_logger.debug`**。符合 leader brief「/simplify ② logged 非靜默吞」（確實有 log，非 silent `pass`）。但 debug level 在 production（INFO/WARNING）不可見 → 若 pod 普遍 close 失敗（systemic），這條訊號會被吞沒。方向：若要觀測 close-handshake 健康度，升 `warning`；若視為良性收尾雜訊，維持 debug 可接受。**非缺陷**。
- [info] `src/services/agent_runtime_client.py:39-54` — **F-PC-5 把 operator `detail` surface 到 SSE error envelope**（`str(AgentRuntimeOperatorError)` → `ai_assistant.py` `'error': str(exc)` → 瀏覽器）。這正是 F-PC-5 的目的（surface 真因），但意味 operator 內部訊息（如「Operator API key not configured」）會吐給終端使用者。對 internal PaaS 可接受的 debuggability/info-disclosure 取捨；只要 operator `detail` 不放敏感內容即無虞。記錄為 bounded info-disclosure。
- [info] `src/models/agent_session.py:121` — **`_get_namespace_secret()` 在 `create()` 頂端無條件呼叫**，先於「是否真的需要算 hash」判斷。任何 `agent_session` create（即使 vals 已自帶 hash / 無 project_id）都會觸發 fail-closed raise。這是 report §10 記載的「forward-coupling blast radius」（intentional，impl 已在 3 個既有 session-creating test setUp 補設 env）。實務上唯一的 production create 路徑（`_project_hash` lazy-create）本就需要 secret，故無害；屬「偏嚴」而非 bug。

**整體**：fix 的 code comment 品質優異 —— 每條都把 caveat 編號 + 「為何這樣修」寫進註解（HC-2 trust 來源、namespace 單 secret 不 rotation、webhook 裸名刻意不對稱、F-PC-6 suspended-generator 機制），這對後手維護者是 self-documenting 的 caveat lineage。

### Security — 無 blocker，1 caveat + 2 info（核心面向，逐 focus 點對齊 leader brief）

- **[focus 2 — HC-2 收緊] 無發現缺陷，trust boundary 結構性 airtight。** `src/models/project_project.py:129-135`：SUPERUSER 分支僅在 context 明示 `bootstrap_owner_partner_id` 才 bootstrap；normal-user 分支用 `env.user.partner_id`、**完全不讀 hint**。我 grep 全 `src/` + `extra/`：`bootstrap_owner_partner_id` **production 零 set-site**（只有 test 設、只有此處讀）。security-auditor 二審確認兩個 production `.sudo().create(project)` 點（`ai_assistant.py:1217` `_create_project`、`paas.py` create 路徑）都從固定 allowlist 組 vals、**都不把該 key 寫進 context**。normal-user 分支無法被誘導吃 hint；bogus/型別錯 hint 由 `browse(...).exists()` + `if not partner` 安全擋（非 int 在 `browse` fail-loud，非靜默誤植）。→ **B1 lineage 維持關閉，無 residual normal-user trust path（含間接路徑）**。
- **[focus 1 — F-PC-4 fail-closed] 無發現缺陷，token-forgery primitive 完整堵住。** `extra/paas-operator/src/main.py:93-98`：unset key 由 pass-through 改 503。`status` + `JSONResponse` import 齊（`main.py:6-8`），不會 NameError。豁免集 `["/health","/docs","/redoc","/openapi.json"]` exact-match 且在 key 檢查**之前**（`:83`）→ k8s liveness 不受影響。二審確認：**global `@app.middleware("http")` 在 routing 前跑 → 7 個 router（含新 `agent_runtimes`）全覆蓋、無遺漏；無 `app.mount` sub-app、無 WS route、無 per-router `Depends` 旁路**。503 被 client `_request`（`resp.ok` False → raise）當硬失敗、不 retry-as-success。→ **「operator 成 token 簽發者後 fail-open = 跨租戶 token 偽造原語」確實堵住**。
- **[focus 3 — F-PC-3 fail-closed] 無發現缺陷，同源 hash 單一來源確認。** `src/models/agent_session.py:130-145`：移除 `_DEFAULT_SECRET` 公開 fallback、改讀 env、缺即 `raise UserError`、不再讀 ICP；`hooks.py` 同步移除 `_CONFIG_PARAMETERS` 條目。**無 silent 公開 fallback 殘留**（grep 全 repo 僅 test 出現舊名）。同源檢查見下方專段：`_project_hash` 不自算 hash、全系統單一計算點。
- **[focus 4 — F-PC-2 dual-secret] 無發現缺陷。** `agent_runtime_webhook.py:45-57` `_verify_hmac_dual` = current OR previous；空 secret 被 `_verify_hmac`（`if not secret: return False`）擋 → 未設的 `_PREVIOUS` slot **不會放寬接受**（dual pattern 的關鍵安全不變式）；`hmac.compare_digest` timing-safe 保留。current 維持裸名 `AGENT_RUNTIME_WEBHOOK_SECRET`（刻意不對稱，符合 brief，**未判 bug**）。
- **[focus 8 — F-PC-7 token_use 3-端] 無發現缺陷，3 端對稱。** issuer（`jwt_signer.py` 簽 `ws`/`skill_bundle`，claim 在 HMAC-signed payload 內 → 不可竄改）/ Odoo skill-bundle verify（`agent_runtime_skill.py:105` assert `=='skill_bundle'`）/ pod WS verify（cross-repo，PR #3 A 嚴格，given）。跨 kind 雙向拒絕對稱。**deploy-order 風險見 D-CAVEAT-2**。
- **[D-CAVEAT-1] `agent_session.py:130-145` + `agent_runtime_webhook.py:93` — namespace hash 現依賴「同一 secret 跨所有進程一致」這條未被 code 強制的部署 invariant。** secret 從 source-public default 改為 per-process env 後，failure mode 從「靜默用公開 default」變成「Odoo web worker / cron worker / operator 三進程中任一拿到不同或空值 → hash 靜默分歧 → webhook 永遠 404、或同 project 在不同 worker 算出不同 hash」。**charts `optional: true` 放大此風險**：partial 部署（secret 只加到部分進程）不會在 deploy 時炸，會靜默分歧到首次使用才現形。module 註解（`:13-18`）只警告 rotation drift，未點明「跨進程同值」要求。flag-gated（從未部署）+ 屬 config invariant 非可利用 code 缺陷 → caveat 非 blocker。**方向（給 planner / Phase D flip-gate）**：單一 K8s Secret 同值掛入所有 Odoo pod + operator；考慮在 hash 消費點加 startup/health 一致性斷言。
- [info] `agent_session.py:139` 用 `UserError` 作 fail-closed 例外型別 —— 語意上這是 server misconfig 非 user 錯，但功能上 fail-closed 成立（abort create、不算 hash、一路 propagate 到 SSE bridge 轉 `spawn_failed`，未被 swallow）。型別選擇為 nit，不影響安全。
- [info] operator 端 hash 是否同 secret+algo **不在本 4 檔 scope**（Odoo 端已確認單源）。若 operator 曾自行 recompute hash 而非把 Odoo 送的 `project_hash` 當 opaque，會成第 4 個同源面 —— 留待後續一行確認。

### Performance — 無 blocker，無 net-new 退化，1 info

- **F-PC-6 aclose**：每輪對話 drain loop `finally` 多一次 `loop.run_until_complete(agen.aclose())` —— 成本可忽略，且這是**修掉**每輪洩一條 pod 連線的 resource leak（淨正向，效能改善）。
- **F-PC-2 dual-secret**：previous-secret 路徑多一次 HMAC 計算，且僅在 current 不中時才算（`or` short-circuit）—— 可忽略。
- **F-PC-4 middleware**：hot-path 多一個 branch，無量級變化。
- [info] **Phase C P-1（SSE worker-pin）未在 Phase D 處理** —— SSE 長連線對 Odoo worker 模型（prefork vs async/gevent）的相容/佔用，需 live load 才能驗，屬 pilot-gate（report §9）。確認其為刻意延後而非遺漏（見 plan alignment）。
- 無新 N+1 / 無 hot-path 退化；drift cron（Phase C，5min）未動。

### Architecture — 無 blocker，1 caveat + 4 info

- **[focus 8 — token_use 3-端 coherence] 內部一致且雙向對稱**（architect-reviewer 確認）：每個消費端 assert 自己預期的 kind，跨 kind 雙向拒；claim 在 signed payload 內不可竄改；責任歸位正確（allowlist 落在**消費**端 controller，非推回 issuer → 真 defense-in-depth，每端獨立失效）。
- **[D-CAVEAT-2] token_use deploy-order 僅 runbook 記載、無 code gate**（plan `:470`/`:482`）。約束「operator 簽 token_use 100% 先於 pod 切 A 嚴格」在正常 forward path 已關閉（operator 自 Phase C 已 merged 簽 token_use），**但**：(1) 無任何 code 在 ordering 違反時拒啟動/拒 flip；(2) ws_token exp 3600s → pre-token_use operator 簽的在途 token 有最長 1h 窗口被 A 嚴格 pod 誤殺；(3) **operator 反向 rollback（降回 token_use baseline 以下、而 pod 仍 A 嚴格）目前未記載且不安全**。flag-gated + pilot 單 project 圈住 blast radius → caveat 非 blocker。**方向**：operator `/health` 暴露「signs token_use: true」供 flip-gate checklist 機器可驗，把 `:482` 的人工「確認後」轉成可檢查訊號；runbook 補記反向 rollback 危害。
- **[focus 4 — dual-secret split] 原則一致、套用一致**：「驗證 in-flight 訊息的 secret 可 dual rotation（auth/webhook）；推導持久 identity 的 secret 必單一（namespace，rotation 會漂移成孤兒 pod）」分類乾淨，無「該 dual 卻 single」或反之。skip-empty 與 `compare_digest` 三個 verify 點（auth/webhook/skill-bundle）**uniform**。
- [info] design §3.4（`design:1602`）仍描述 skill-bundle 端有 `SHARED_SECRET` raw-token fallback，但 A 嚴格 impl（`agent_runtime_skill.py:62-106`）**刻意移除**該分支（raw secret 無法帶 token_use claim，會被 `:105` 拒）→ 設計文件已 stale 且自相矛盾。**併入 planner 既有的 design 補寫**（Phase C 我已 escalate planner 改 §2.15/A-1/C-3；此條同批處理）。
- [info] operator 自身 `verify_token`（`jwt_signer.py:155-181`）不 assert token_use —— 今天正確（operator 是 issuer 非 consumer，無跨 kind 混淆通道）；僅在未來某 phase 讓 operator **消費**這些 token 時才需補 kind 參數。
- [info] 兩個 dual-secret family 的 current 命名不同（auth `_CURRENT` 後綴 vs webhook 裸名）—— documented back-compat，對未來「統一 env 名」refactor 是 load-bearing。
- [info] stdlib HS256 decode 在 operator / Odoo 兩處刻意三重化（避免 PyJWT 依賴、確保 claim shape 跨進程一致，`jwt_signer.py:13-14`）—— 有意識的 cross-process 取捨，非意外 copy-paste。

---

## Plan alignment check

Phase D **精準對應** input cell-map 的 11 條 must-fix，無走偏。逐條 vs 我 Phase C CAQA caveat 的 close 狀態：

| Phase C caveat | Phase D 對應 | 狀態 |
|----------------|--------------|------|
| C-1 缺 token_use claim | F-PC-7（+ Phase C C-1 hardening 已 merged） | ✅ 關閉，3 端對稱 |
| C-2 operator fail-open | F-PC-4 | ✅ 關閉，fail-closed 完整 |
| C-3 webhook single-secret | F-PC-2 | ✅ 關閉，dual-secret |
| C-5 namespace 公開 fallback | F-PC-3 | ✅ 關閉（**設計從「auto-gen」pivot 成「fail-closed 要求 env 注入」，比我原 C-5 的 auto-gen 建議更佳** —— 無 idempotency 風險） |
| A-1 websockets extra_headers latent break | F-PC-1（`<14` pin）+ connect 仍用 `extra_headers`（一致） | ✅ 關閉靜態半；runtime 半屬 pilot-gate |
| P-2 async-gen 無 aclose | F-PC-6 | ✅ 關閉，無 WS leak |
| （新）N-1 typed error | F-PC-5 | ✅ |
| （新）HC-2/D-3 bootstrap owner | F-PA-14 | ✅ guard invariant |

**走偏/缺口檢查**：未發現 implementation 偏離 plan 拍板方向。F-PC-3 的設計 pivot（auto-gen → fail-closed）是**對我 C-5 原方向的改良**（planner 在 Phase C escalation 後拍板），不算走偏。

**carried-forward（Phase C caveat 未進 Phase D scope，請 leader 確認是「刻意延後」非「靜默丟棄」）**：
1. **C-4 webhook replay/reorder → stale state**（caveat 級）：Phase D 的 webhook 改動只動 HMAC verify（single→dual），**未碰 state-write 邏輯**。`agent_runtime_webhook.py:80-88` 仍無 monotonic guard —— `last_state_event_ts` 欄位有寫入但**未用於擋 stale**：reorder/retry 的舊事件會覆蓋成過期 state。HMAC-auth 圈住外部 replay，但 operator 自身 retry（3x backoff）+ 網路 reorder 是真實觸發。**修的 enabler 欄位已存在**（`last_state_event_ts`），未來補一個「新事件 ts ≤ 既有則跳過 state 寫入」即可。屬 carried-forward，非 Phase D 引入。
2. **C-6 operator 端 project_hash 授權**（info 級）：trust model 是 operator 信任已認證的 Odoo（Odoo 先過 project_access gate 才呼叫），故 operator 端只驗 hash 格式不驗 ownership —— 低優先，未在 Phase D scope。
3. **P-1 SSE worker-pin**（info 級）：pilot-gate（需 live load）。

---

## 同源 code path 檢查

- **project hash 計算（F-PC-3 核心）**：grep `_compute_project_namespace_hash` / `_get_namespace_secret` / `_project_hash` 全 `src/`。結論：**全系統單一 hash 計算點** —— `agent_runtime_client.py:101-107` `_project_hash` **不自算**，而是 `Session.search`/`create` 後讀 `session.project_namespace_hash`，把計算 delegate 回 `AgentSession.create`（`agent_session.py:119-127`）→ `_get_namespace_secret` → env。**無第二套 algo、無殘留 fallback、無分歧 secret 讀取**。這是最乾淨的同源結果（不是「兩處都對」而是「根本只有一處」）。
- **HC-2 hint set-site**：grep `bootstrap_owner_partner_id` 全 `src/`+`extra/`+`tests/` → production **零 set-site**，consumer 唯 `project_project.py:130`。無複製錯誤可循。
- **fail-open / pass-through pattern**：grep operator middleware → 單一 auth 旁路點（`main.py` middleware），無第二 auth shortcut；F-PC-4 是唯一 gate。
- **空-secret 放寬不變式**：webhook `_verify_hmac`（`return False`）/ skill-bundle / jwt_signer 三處 verify 對空 secret 一致 fail-closed，無一處因空 secret 而誤放（同源安全紀律一致）。

---

## Known pitfalls 對照

- **`cross-workspace-task-data-leak.md`（B1）**：HC-2 owner-bootstrap 是 B1 lineage 風險點。Phase D 關掉「sudo 無條件 auto-own」+ normal 分支忽略 hint → ownership 不可偽造。**無重蹈跡象**。
- **`constraint-input-trust-expansion.md`（HC-2 pattern）**：F-PA-14 是教科書級套用 —— authority 來自 trusted source（明示 hint），untrusted 分支（normal user）忽略 context。**無違反**。給 planner 的 forward-looking invariant（見下）即此 pattern 的延伸守則。
- **`phase-versioned-test-assertion.md`**：Phase D test（`d3aa60e` 已使 Phase A M.9 version-agnostic 的延續）—— 本輪 diff 的 test 未見硬編 schema 快照值致脆，採存在性/行為斷言。**無重蹈**。
- **`ci-workflow-container-missing-node.md`**：cross-repo CI（paas-agent-runtime PR #3）—— 本 repo Odoo CI 不受影響；F-PC-7 cross-repo green 由該 repo agent 盤證，不在本 repo CI。

---

## Blocker 清單（給 leader / implementation）

**無。** Phase D 11 條 must-fix 全正確關閉目標 caveat，0 code 缺陷。

---

## Caveat 清單（給 leader 決定是否帶入 reviewer + Phase D flip-gate）

| # | 面向 | file:line | issue | 方向（非 patch） | 阻斷性 |
|---|------|-----------|-------|------------------|--------|
| **D-CAVEAT-1** | Security / Arch（部署 invariant） | `agent_session.py:130-145` + `agent_runtime_webhook.py:93` + charts `optional:true` | namespace secret 改 per-process env 後，三進程（Odoo web/cron worker + operator）須同值，否則 hash 靜默分歧 → webhook 永遠 404；`optional:true` 使 partial 部署不在 deploy 時炸 | 單一 K8s Secret 同值掛入全部 Odoo pod + operator；hash 消費點加 startup/health 一致性斷言 | flag-gated，**不阻斷**，帶入 flip-gate |
| **D-CAVEAT-2** | Arch（cross-repo ops） | plan `:470`/`:482`；window per `jwt_signer.py:74,33` | token_use deploy-order（operator 簽 → pod A 嚴格）僅 runbook 記載無 code gate；1h 在途 token 窗口；operator 反向 rollback 未記載 | operator `/health` 暴露 `signs token_use:true` 供 checklist 機器驗；runbook 補反向 rollback 危害 | flag-gated + pilot 圈住，**不阻斷**，帶入 flip-gate |

**carried-forward（非本 diff 引入，請 leader 確認 disposition）**：C-4 webhook reorder→stale state（caveat 級，enabler 欄位已存在）、C-6 operator project_hash authz（info）、P-1 SSE worker-pin（info，pilot-gate）。

**給 planner 的 forward-looking invariant（HC-2，務必落 design / pattern doc）**：
> `bootstrap_owner_partner_id` 是 **superuser-only 的 out-of-band channel**；其值必須 **server-authoritative**（來自 provisioning record 或已認證真人自己的 partner_id），**任何 controller 不得從 `kw`/`params`/inbound `context` 填入**。未來 phase 接 operator provisioning 真 set-site 時，違反此條 = 把 bootstrap 便利退化成 ownership-assignment 原語（B1 復發）。

---

## 工具 raw 輸出摘要

- **security-auditor**（HC-2 + F-PC-4 + F-PC-3 + 同源）：3 fix 全 sound，0 blocker。HC-2 無 indirect 漏洞（兩 production sudo-create 點都不寫 hint key）；F-PC-4 coverage 完整（無 sub-app/WS/per-router 旁路）；F-PC-3 同源單一 hash 源確認、`UserError` 不被 swallow。1 caveat = cross-process 同值 invariant（= D-CAVEAT-1）。給 planner 的 forward invariant 同上。
- **architect-reviewer**（token_use 3-端 + dual-secret + 漂移）：3-端 coherent 雙向對稱、claim 不可竄改；dual-secret split 原則一致、skip-empty + compare_digest uniform；additive 無結構漂移。1 caveat = deploy-order 無 code gate（= D-CAVEAT-2）。3 info = design §3.4 SHARED_SECRET stale doc / operator verify_token 不 assert token_use（今天正確）/ 命名不對稱 documented。
- **direct diff read**（27 檔，quality + performance 主審）：F-PC-1 pin 與 `extra_headers` 一致；F-PC-5 typed error clean（detail surface = bounded info-disclosure info）；F-PC-6 aclose logged-not-silent（debug level info）；charts `optional:true` × F-PC-3 組合正確（未啟用不炸、啟用缺 secret 才 fail-closed）。

---

## Hand-off

- **狀態**：Phase D CAQA = **PASS WITH CAVEATS**（0 blocker / 2 net-new caveat / 9 info / 3 carried-forward）。
- **可推 reviewer**：無阻斷項，2 caveat 皆 flag-gated 部署/cross-repo invariant，適合帶入 reviewer 並收進 **Phase D flip-gate / runbook**。
- **需 leader/planner 動作**：
  1. D-CAVEAT-1 + D-CAVEAT-2 收進 Phase D flip-gate 檢查項（部署同值 secret + token_use 簽發確認 + 反向 rollback 危害）。
  2. 確認 carried-forward C-4 / C-6 / P-1 是刻意延後（pilot-gate / accepted-risk）非靜默丟棄；C-4 的 enabler 欄位（`last_state_event_ts`）已就位，未來補 monotonic guard 即可。
  3. planner 既有 design 補寫批次（§2.15 / A-1 / C-3）順手 reconcile design §3.4 stale `SHARED_SECRET` fallback 描述。
  4. HC-2 forward-looking invariant 落 design / `constraint-input-trust-expansion.md`，鎖未來 operator provisioning set-site。
- **next**：回報 leader → reviewer（5-agent）→ reporter 收口。
