---
name: reviewer-report-2026-05-24-agent-runtime-pod-phase-c
phase: reviewer
team: agent-runtime-pod
reviewer: agent-runtime-pod-reviewer
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
test_report_ref: docs/archived/tracking/test-coverage-agent-runtime-phase-c-2026-05-24.md
caqa_ref: docs/archived/tracking/caqa-report-2026-05-24-agent-runtime-pod-phase-c.md
created: 2026-05-24
verdict: APPROVE WITH CAVEATS（與 CAQA PASS WITH CAVEATS 對齊；C-1 disputed item 已由 hardening commit 7daf1f4 閉合；無 CAQA 漏抓的 high-severity；新增 5 條 caveat 進 Phase D gate / follow-up）
related:
  - docs/archived/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md
  - docs/archived/tracking/caqa-report-2026-05-24-agent-runtime-pod-phase-c.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/tracking/pitfalls/plan-vs-odoo-schema-mismatch.md
status: archived
archived-from: docs/tracking/reviewer-report-2026-05-24-agent-runtime-pod-phase-c.md
archived: 2026-07-19
archived-reason: "一次性 reviewer 驗收報告，cycle 已收官；證據錨點引用已改指本處"
---

# Reviewer Report — Agent Runtime Pod Phase C（epic wiring 收口：operator endpoints + skill-bundle + SSE bridge）

## 1. Final Verdict

**APPROVE WITH CAVEATS** — 不擋 merge 到 develop。整段 Phase C **躲在 `project.use_agent_runtime`（default False）之後出貨即 inert**（`project_project.py:65` 定義 + gate `ai_assistant.py:395`，SSE.1 test 鎖定 default），無 live pod 流量。0 blocker。verdict 與 CAQA **PASS WITH CAVEATS 對齊**，無方向性歧異 → 不觸發紅線。

核心防線 verified ✓：
- **C-1 disputed item 已閉合**：CAQA report §7 把 C-1（token scope claim）留在「security-auditor 判 blocker vs CAQA 判 caveat」的待裁決狀態。**leader 已裁決走 hardening**（commit `7daf1f4` + test `1305b77`，CAQA 從未審過此 commit）。我獨立驗證 hardening 正確且 fail-closed（見 §5）→ dispute **mooted**，不需再上呈 blocker/caveat 二選一。
- **HC-2 token authoritative（第三度）** verified ✓：skill-bundle endpoint 用 verified token claim `project_id` 解析（`agent_runtime_skill.py:109/113-117`），絕不信 query `project_hash`；token 為 project A 簽就撈不到 project B。
- **HC-4 未被繞過** verified ✓：skill-bundle 的 `sudo()` 是 machine-auth 獨立路徑（HS256 shared secret + token project_id 為界），非 user-session `/web/content` 側通道；anchor stamping + ir.attachment deny rule 對 user 完整。
- **B1 project_access gate 在 spawn 之前** verified ✓：`_check_project_access`（`ai_assistant.py:577`）在 spawn（`:610`）之前，順序 CSRF→channel→flag→project_access→spawn。
- **cross-repo frozen contract（skill-bundle ⇄ Phase B `cache.py`）逐欄位精確相符** verified ✓（見 §11），且 C-1 新增的 403 path 對 contract 無破壞。
- **resume token 無 race** verified ✓：`_resume_pod` sign→set_env→scale-1 順序（`agent_runtimes.py:76→81→84`）。
- **dual-secret 不放行 expired token** verified ✓（exp 在 signature match 之後，兩端一致）。
- **migration .27 additive/idempotent** verified ✓（`ADD COLUMN IF NOT EXISTS` nullable，欄位型態與 `agent_session.py:89-96` 相符，無 phantom field）。

**與 CAQA 的差異（我方額外價值）**：縮減版 multi-agent 對 CAQA 未深入的「error-surfacing / async-gen lifecycle / 未接線 utility robustness / test 命名契約」面向獨立過了一輪，新增 5 條 caveat（N-1~N-5，§7）。**其中無一是 CAQA 漏抓的 high-severity 安全洞** → 不觸發「CAQA 漏 high-severity → escalate leader」紅線。新發現全為 robustness / observability / test 衛生，且全 flag-gated 或未接線，性質為 **Phase D enablement gate / follow-up**，不改 merge 判斷。

## 2. Diff Summary

### Base 校正（與 Phase A 同坑，已避開）

| 項目 | 值 |
|------|----|
| 本地 `develop` ref | 落後（未含 Phase A merge #58）→ **不可用 `develop..HEAD`**（會把整個 Phase A 89 檔帶進來重審） |
| Phase C 乾淨 base | `9479cb2`（Merge PR #58）— 與 CAQA §1 一致 |
| Phase C 真實 diff | `9479cb2..HEAD` = 12 commit / 29 檔 / +3980 |
| 3 個 impl commit | `6abb490`（operator endpoints）/ `ec226b7`（Odoo wiring）/ `7daf1f4`（**C-1 token_use hardening — CAQA 後才落地**） |

### Phase C impl 檔（非 test）

**paas-operator（FastAPI / py3.11）**：
- `extra/paas-operator/src/services/jwt_signer.py`（+181）— stdlib HS256 雙 token（`sign_ws_token` / `sign_bundle_token`），C-1 加 `token_use` claim；`verify_token`（dual-secret + jti cache，operator 自用 helper）
- `extra/paas-operator/src/api/agent_runtimes.py`（+227）— 7 endpoint（spawn-or-attach / reap / resume / invalidate-mcp / sync-workspaces / delete / status），additive router
- `extra/paas-operator/src/services/webhook_dispatcher.py`（+52）— 出站 HMAC 簽發 + 5xx retry（**未接線**，見 N-2）
- `extra/paas-operator/src/services/mcp_invalidator.py`（+43）— ConfigMap patch
- `extra/paas-operator/src/services/helm.py`（+37）— `get_deployment_replicas` + `set_deployment_env` 純 additive
- `extra/paas-operator/src/main.py`（+11）/ `config.py`（+5）/ `models/schemas.py`（+39）— router 掛載 + settings + request schema

**Odoo（py / Odoo 18）**：
- `src/controllers/agent_runtime_skill.py`（+142）— skill-bundle endpoint（HC-2 token-authoritative + C-1 token_use verify）
- `src/controllers/agent_runtime_webhook.py`（+93）— 入站 state-update webhook（HMAC verify）
- `src/services/agent_runtime_client.py`（+92）— Odoo→operator HTTP client
- `src/controllers/ai_assistant.py`（+257）— SSE dual-mode：flag gate + `_stream_agent_runtime` WS↔SSE bridge
- `src/models/agent_session.py`（+49）— Phase C 欄位（`last_synced_at`/`last_state_event_ts`）+ `_cron_drift_detection`
- `src/migrations/18.0.1.0.27/post-migrate.py`（+28）— 2 欄位 `ADD COLUMN IF NOT EXISTS`
- `src/data/agent_session_cron.xml`（+16）— drift cron（5min interval）+ `__manifest__.py` 版號 bump

### Phase C test 檔
- `src/tests/test_agent_pod_phase_c_skill_bundle.py`（+504）— skill-bundle + HC-2（SB.4/SB.4b）+ C-1 token_use（6 method）
- `src/tests/test_agent_pod_phase_c_client_webhook.py`（+394）— client + webhook HMAC
- `src/tests/test_agent_pod_phase_c_sse_gateway.py`（+233）— SSE dual-mode gate（SSE.1~SSE.5）
- `extra/paas-operator/tests/test_agent_runtimes.py`（+641）— operator endpoints + jwt_signer + dual-secret
- `src/tests/test_agent_pod_phase_a_migration.py`（M.9，+46）— 版號無關化，與 Phase C bump 共存

## 3. 委派工具 raw 輸出摘要（縮減版 multi-agent）

**縮減理由（記錄判斷）**：CAQA 已用 **security-auditor（26 tool_uses）+ architect-reviewer（26 tool_uses）** 對 security 攻擊面與 cross-repo architecture 過了完整一輪（含 context7 驗 websockets 版面、frozen contract 逐欄位）。重複 dispatch 全 5-agent 對這兩面向是 token 浪費。故我聚焦 **CAQA 未深入的三面向**，且把 C-1 hardening（CAQA 沒看過）列為每 agent 的 priority。委派 3 agent 並行：

- **`pr-review-toolkit:code-reviewer`**（28 tool_uses）→ verdict **APPROVED WITH CAVEATS / 0 blocker**。
  - 獨立驗證 C-1 hardening **正確 fail-closed**（exact-string allowlist，absent/forged/type-confusion 全 403）；HC-2 無跨 project leak；resume 順序正確 + 無 body 的 `/resume` 簽出 project_id=None token 為 **fail-safe**（skill-bundle 端 401，且 `client.resume()` 無 non-test caller）。
  - 新抓 N-1（client `_request` 無 `raise_for_status`，info/minor）。對 CAQA 8 caveat **無 severity 異議**。
- **`pr-review-toolkit:silent-failure-hunter`**（12 tool_uses）→ verdict **needs revision before flag flip**（3 MAJOR + 2 MINOR + 1 INFO）。
  - MAJOR：N-1（operator error 被洗成 cryptic WS error）、P-2 加強（async-gen 未 `aclose()` → **每條路徑含正常 done 都洩 WS** 至 ping_timeout）、N-2（webhook_dispatcher 忽略 httpx connection error → retry 形同虛設 + 耗盡後 silent give-up）。
  - MINOR：Q-4（`new_state` 無 enum 預檢→500）、C-7（`str(exc)` 洩漏 cluster DNS 到 browser）。INFO：cron drift try/except **做對了**。
  - 立場：3 MAJOR 須進「flag flip 前 must-fix gate，不能當 loose follow-up」。
- **`pr-review-toolkit:pr-test-analyzer`**（21 tool_uses）→ **merge-acceptable**。
  - 確認 C-1（真負例：固定 project_id+secret 只變 token_use 斷言 403）/ HC-2（SB.4/SB.4b 雙向）/ frozen raw-bytes（SB.1 含 `assertNotEqual base64` 防禦性負例）/ dual-secret no-second-chance / flag gate（SSE.1/4/5）**全部 behavioral locked，非 mock-theater**。
  - 新抓 N-4（operator namespace/release 命名契約 **未 assert**，dead `_NS`/`_RELEASE` 常數 + 命名缺 `pa-` 分隔符）、N-5（SHA256-over-raw-bytes 僅隱含未直接 assert）。webhook replay/dual-secret 未測（與 CAQA C-3/C-4 對齊）。

## 4. Multi-agent severity 分歧處理（誠實揭露，不粉飾）

**唯一分歧**：N-1 / N-2 / P-2 的 severity 標籤。

| | code-reviewer | silent-failure-hunter |
|---|---|---|
| N-1 client `raise_for_status` | info/minor（flag-gated，Phase D follow-up） | MAJOR（flag-on 即每個 operator error 被洗成錯誤訊息；must-fix gate） |
| P-2 async-gen `aclose()` | minor（CAQA P-2，drain fix） | MAJOR（每條路徑含正常 done 都洩 WS） |
| N-2 dispatcher retry | （未列；屬未接線 utility） | MAJOR（retry 形同虛設 + silent give-up） |

**Reviewer 仲裁**：兩 agent 的**結論一致**（merge inert OK、無當前 production 影響、皆 flag-gated），分歧只在**時間視角**——code-reviewer 標「當前 0 影響」，silent-failure-hunter 標「flag-on 時的影響」。這不是方向性 dispute，無須 escalate leader。我採**雙方交集**：
- 當前 severity（merge 判斷）：**caveat，不擋 inert merge** → 採 code-reviewer 視角。
- 但這三條是**真實 error-surfacing 缺口**，flag-on 時會狠咬 → 採 silent-failure-hunter 的原則：列入 **Phase D enablement gate（flag flip 前 must-fix）**，**不**丟進 loose follow-up backlog。

我未對任何 item 擅自降級以迴避 escalation：「flag-gated inert code 的 MAJOR error-handling 缺口」在 reviewer taxonomy = 「Phase D gate caveat」，與 CAQA 整體 posture 及 code-reviewer 一致。silent-failure-hunter 的較強框架已逐字記於上，leader 可逕取其判斷。

## 5. C-1 hardening 閉合驗證（CAQA disputed item，CAQA 未審）

CAQA report §7 把 C-1 留待 leader 裁決（security-auditor 判 blocker、CAQA 判 caveat）。leader 裁決走 hardening。我獨立驗證 commit `7daf1f4`：

**簽發端（`jwt_signer.py`）**：
- `sign_ws_token`（:68-91）→ payload 帶 `"token_use": "ws"`（:82），無 project_id。
- `sign_bundle_token`（:94-119）→ payload 帶 `"token_use": "skill_bundle"`（:111），帶 authoritative project_id + workspace_ids。

**驗證端（`agent_runtime_skill.py:105`）**：
```python
if claims.get('token_use') != 'skill_bundle':
    return self._forbidden()   # 403
```
- 順序正確：在 signature+exp verify（`:97 _verify_skill_bundle_token`）**之後**才檢 token_use → forged token_use 必先過 HS256 簽章（攻擊者無 secret 偽造不了）。
- **exact-string allowlist，fail-closed**：`token_use` 缺（None）/ ws / 偽造值 / 型別混淆（list/int）全部 `!= 'skill_bundle'` → 403。
- 攻擊鏈切斷：ws-scope token **無法**被重放為 bundle fetch（skill-bundle 方向關閉）。
- 對 frozen contract 無破壞：新 403 被 Phase B `cache.py:83 except HTTPError` 統一吃成 `SkillFetchError`，與既有 4xx 行為一致（§11）。

**測試鎖定**：`test_C1_ws_token_use_rejected`（:439）/ `test_C1_missing_token_use_rejected`（:457）/ `test_C1_forged_token_use_rejected`（:473）/ `test_C1_skill_bundle_token_use_accepted`（:488）+ 簽發端 :194/:209。皆為真負例（固定 project_id+secret 只變 token_use）。

**結論**：C-1 disputed 在 **implementation 層面已閉合**（既非「接受 caveat」亦非「block」，而是修掉）。skill-bundle 方向 airtight。**唯一剩餘半邊為 cross-repo**：Phase B pod WS verifier 須對稱拒絕 `token_use != 'ws'`（或缺 channel_id）的 token——此為 Phase B 的對稱補丁，見 §13 cross-repo 守線。

## 6. CAQA Cross-Check（8 caveat 逐條：同意 / 補充 / 反對）

| # | CAQA 判定 | Reviewer 立場 | 補充 |
|---|---|---|---|
| C-1 | caveat（disputed）| **同意 → 已閉合** | hardening 7daf1f4 正確 fail-closed（§5）；dispute mooted；剩 cross-repo WS 半邊（§13） |
| C-2 operator auth fail-open（`main.py:89-91`）| caveat | **同意，升 Phase D gate** | operator 是 token 簽發者，fail-open 時可為任意 project_id 簽 properly-scoped token → 跨租戶 bundle 外洩原語；C-1 對此**無解**（拿到的是目標 project 的合法 token），故 C-2 才是跨 project 風險真正放大器 |
| C-3 webhook 單 secret（`agent_runtime_webhook.py:62`）| caveat（plan 偏離）| **同意，escalate planner** | design §5.3「webhook 同理」要求 dual-secret loop 未落地；test 忠實鎖了偏離後的 impl（pr-test-analyzer：唯一「green ≠ 對齊 design」處） |
| C-4 webhook 無 replay/freshness | caveat | **同意** | cron 自癒；Phase D 補簽章涵蓋 `ts` 拒舊或單調 sequence |
| C-5 namespace secret 硬編碼 fallback + 缺 auto-gen（`agent_session.py:11,122-127`）| caveat（plan gap）| **同意，escalate planner** | design §2.15 規定的 post_init random 生成未實作；只洩 namespace 命名（K8s RBAC 仍 gate），故非 blocker |
| C-6 operator 盲信 `project_hash` 格式（`agent_runtimes.py:38`）| caveat | **同意** | list-arg 排除 shell injection；疊 C-2 放大；Phase D 邊界驗 `^[a-z2-7]{16}$` |
| A-1 `websockets.connect(extra_headers=)` ≥14 移除（`ai_assistant.py:650`）| caveat（plan-level）| **同意，升 Phase D must-fix + escalate planner** | flag-on + websockets≥14 → 首次 connect `TypeError` → **每條對話失敗**；`requirements.txt:10` 釘 `>=12.0` 無上界；design §5.6 同寫 extra_headers（plan-level defect 忠實複製）；context7 已驗 12/14/16 版面 |
| P-1 SSE worker-pin（`ai_assistant.py:635`）| caveat | **同意** | design §6.8 認列（pool=66）；Phase D size worker pool + 確保 pod 必 `done`/`fatal` 收尾 |
| P-2 async-gen 未 `aclose()`（`ai_assistant.py:678-729`）| caveat | **同意 + 補充加重** | CAQA 框為「client 斷線後殘留」；**實則每條路徑（含正常 done break at :712）relay 都停在 `yield`(:671) 未跑 `return`(:673) 即 abandon → `async with websockets.connect` 的 `__aexit__` 不執行 → WS 必洩至 ping_timeout(≤60s)**。範圍比 CAQA 描述廣 |

6 info（C-7 / Q-1 / Q-2 / Q-3 / Q-4 / P-3 / P-4）：全同意 CAQA 判定，無異議。**反對：0 條**。CAQA 的 caveat 分級嚴格、無浮報；8 caveat 全為合理 follow-up / Phase D gate，**無一應擋 inert merge**。

## 7. CAQA 未抓的新發現（N-1~N-5；無 high-severity 安全洞）

| # | Sev（當前/flag-on）| 位置 | 摘要 | 方向 |
|---|---|---|---|---|
| N-1 | minor / **MAJOR** | `agent_runtime_client.py:32-33` | `_request` 無 `raise_for_status`，直接 `resp.json()`。operator 回 JSON 錯誤（401 bad-key `main.py:95` / 500 helm）→ 不 raise → `runtime_info` 變錯誤 dict → `pod_ws_url=None` → `websockets.connect(None)` → 被 `:721` 廣捕 → browser 收到 **cryptic WS error 而非真因**；operator 的 `detail` 全丟失 | `_request` 加 `raise_for_status` 或顯式 status 檢查，拋 typed error 帶 operator `detail` |
| N-2 | info（未接線）/ MAJOR | `webhook_dispatcher.py:41-52` | retry 只看 `status_code>=500`，**忽略 httpx ConnectError/Timeout**（最常見的瞬斷零 retry，docstring 過度承諾）；耗盡 3 次後 silent return 5xx。**註**：grep 確認 operator 內**無 production caller**（只有 def + test :553/:586）→ Phase C 不可達；inbound webhook receiver（Odoo `@route`）才是 wired 的實際路徑 | wrap `httpx.RequestError` 視為 retryable + 耗盡後 `logger.error` 可告警；Phase D 接線前修 |
| N-3 | minor | `ai_assistant.py:625,726` | C-7 加強：`str(exc)` 可能含 pod 內部 ws:// cluster DNS（`agent-pa-{hash}.paas-ws-pa-{hash}.svc...`）洩到 browser；server 端 `_logger.exception` 已對（保留） | client 端給泛化訊息 + correlation id，細節只進 log |
| N-4 | minor（test 衛生）| `tests/test_agent_runtimes.py:291-292` | operator namespace/release 命名契約**未 assert**：`_NS`/`_RELEASE` 常數定義但零 usage（dead），且常數 `paas-ws-pa1234567890abcd` **缺 `pa-` 分隔符**。**註**：impl 命名跨檔一致（`agent_runtimes.py:38` + `mcp_invalidator.py:18` 皆 `paas-ws-pa-{hash}`，grep 確認）→ 純 dead 常數 sloppy，**非 impl bug**；但 hash→namespace→RBAC 邊界值無迴歸保護 | 補 `_names()` call-args 斷言（cheap，且鎖安全相關值） |
| N-5 | info | `test_..._skill_bundle.py` SB.1 | SHA256-over-raw-bytes invariant 僅隱含（fixture 設 `bundle_sha256=sha256(raw)` + SB.1 驗 raw 相等），無直接 `sha256(resp.content)==skill.bundle_sha256` 斷言 | 加一行斷言廉價硬化 contract（pod 端責任，acceptable） |

**關鍵判斷**：N-1~N-5 **無一是 high-severity 安全洞**。安全邊界（C-1 / HC-2 / HC-4 / B1 / frozen contract / dual-secret）三 agent + 我自讀全部確認守住。N-1/N-2/N-3 為 error-surfacing / 觀測性，N-4/N-5 為 test 衛生 + coverage，全 flag-gated 或未接線。→ **不觸發「CAQA 漏 high-severity → escalate leader」紅線**。

## 8. Plan Alignment

Implementation 緊貼 design §2.11 / §2.11.1 / §5.3 / §5.5 / §5.6 / §6.6：stdlib HS256 雙 token、token-authoritative HC-2 skill-bundle、env-before-scale resume 順序、webhook-primary + cron-fallback、Bearer-header（非 query）token transport、auth-path dual-secret rotation。`ai_assistant.py` SSE branch 逐行對齊 §5.6 pseudocode（gate 順序 + pre-fetch-ORM-before-generator cursor pattern）。**C-1 hardening 是 plan 的乾淨 superset**（補上 design §6.6 未要求、但廉價的 scope assertion）。

**plan/design 偏離（須 escalate planner via leader）**：
1. **A-1** — design §5.6 line 2887 與 impl 同用 `extra_headers`（websockets ≥14 移除）。design 為源。
2. **C-3** — design §5.3 line 2676「webhook 同理」要求 dual-secret，impl 略去。design 為源。
3. **C-5** — design §2.15 line 1344 規定 migration 首次部署 auto-gen namespace random secret，impl 未實作。design 為源。

**plan 文字 stale 但 impl 正確（不需 escalate）**：Q-3（`_persist_ai_response` 傳 context 非 template）；SSE「no project / flag False」走 legacy fall-through（SSE.4/SSE.5 鎖定）。

## 9. 同源 code path 檢查

- **`token_use` 強制**：本 diff 唯一 sink 為 skill-bundle endpoint（`agent_runtime_skill.py:105`）；WS 側對稱檢查在 Phase B（cross-repo，§13）。
- **`extra_headers`**：全 addon 唯一真 call site `ai_assistant.py:650`（`n8n_init.py`/`mcp_server.py` 的同名是無關 local 變數）→ A-1 孤立未擴散。
- **dual-secret verify pattern（3 實作）**：operator `verify_token`（sig+exp+jti，`jwt_signer.py:155`）、Odoo `_verify_skill_bundle_token`（sig+exp，**刻意無 jti** — per-pod-lifetime token 正確，`agent_runtime_skill.py:62`）、webhook `_verify_hmac`（**單 secret，C-3 odd-one-out**，`agent_runtime_webhook.py:30`）。pattern 對 2/3，漏 webhook。
- **`auth='none' + sudo()` machine-auth**：skill + webhook 兩 endpoint 皆 crypto-verify 後才 sudo，且從已驗 credential 解權威 id（非 untrusted param）→ pattern 一致。
- **namespace 命名**：`paas-ws-pa-{hash}` 跨 `agent_runtimes.py:38` + `mcp_invalidator.py:18` + `agent_session.py` docstring 一致（grep 確認），唯 test 常數 dead+malformed（N-4）。
- **`raise_for_status` 缺漏**：`agent_runtime_client._request:33` 一處；既有 `paas_operator` client 亦同 pattern（pre-existing，本 diff 不擴大）。

## 10. Known Pitfalls 對照

| Pitfall / pattern | 對照結果 |
|---|---|
| [[constraint-input-trust-expansion]]（HC-2/HC-3）| **未回歸** — skill-bundle 用 verified token claim project_id，絕不信 attacker `project_hash`；C-1 hardening 再加 token_use 二層 |
| [[attachment-row-level-acl-and-anchor-stamping]]（HC-4）| **未繞過** — machine-auth `sudo()` 獨立路徑；anchor stamping + ir.attachment deny rule 對 user 完整；Phase B `cache.py:13` 明文「假設 attachment 已 anchor」對齊 |
| [[cross-workspace-task-data-leak]]（B1）| **未重蹈** — `_check_project_access` gate 在 spawn 之前；`invoker_role` server-side 解析非 client 輸入 |
| [[plan-vs-odoo-schema-mismatch]] | **未重蹈** — migration .27 欄位與 model 精確相符，無 phantom field |
| CI container missing-node | **不適用** — 本 diff 無動 Gitea Actions workflow |

## 11. Frozen Contract 獨立逐欄位覆核（priority check）

Odoo `agent_runtime_skill.py` ⇄ Phase B `paas-agent-runtime/runtime/skills/cache.py`：

| 契約元素 | Odoo endpoint | Phase B consumer | Match |
|---|---|---|---|
| URL path | `/api/agent-runtimes/skill-bundle`(:87) | `{base}/api/agent-runtimes/skill-bundle`(cache.py:78) | ✓ |
| Query params | reads `skill_name`/`version`(:115-116)，`project_hash` 收不用 | sends `project_hash`/`skill_name`/`version`(cache.py:75-77) | ✓ 名稱一致 |
| Token env | 驗 `AGENT_RUNTIME_AUTH_SECRET_*` | `SKILL_BUNDLE_TOKEN`(cache.py:74) ← operator 注入(`agent_runtimes.py:64`) | ✓ |
| Auth header | `Authorization: Bearer`(:92-95) | `Authorization: Bearer {token}`(cache.py:79) | ✓ |
| Response body | `base64.b64decode(datas)` → **RAW bytes** `make_response`(:125-126) | `resp.read()`→raw→`sha256`(cache.py:82,88) | ✓ raw（非 base64/JSON wrapper） |
| Content-Type | `mimetype or 'application/gzip'`(:129) | `tarfile mode="r:gz"`(cache.py:100) | ✓ |
| Status | 200 / 401 / **403（C-1 新）** / 404 | 任何 4xx/5xx → `SkillFetchError`(cache.py:83-84) | ✓ C-1 的 403 被 pod 廣捕 |

「raw bytes vs base64」陷阱處理正確：endpoint 回應**前** b64-decode 成 raw，pod `resp.read()+sha256` 對的是同一份 raw（SHA 算的也是這份）→ 無破壞 fetch/verify 的漂移。**C-1 hardening 對 contract 無破壞**（新 403 與既有 4xx 同被 pod 端統一處理）。

## 12. Phase D Enablement Gate vs Follow-up 拆分（給 leader）

**我方判定：0 應擋 inert merge。** 以下為 flag flip 前的 gate / 之後的 follow-up 建議：

**Phase D enablement gate（`use_agent_runtime` flip 前 must-fix）**：
- **A-1** — websockets `extra_headers` ≥14 移除：flag-on 即**每條對話失敗**（escalate planner，design 為源）
- **C-2** — operator auth fail-open：operator 是 token 簽發者，跨租戶 bundle 外洩原語
- **C-3** — webhook dual-secret：rotation 窗口可用性 gap（escalate planner，design 為源）
- **N-1** — client `raise_for_status`：否則 operator error 無法正確 surface（error 通道兩方向皆失能，疊 N-2）
- **P-2** — async-gen `aclose()`：每條對話洩 WS 連線

**Follow-up（enablement 後 / 較低優先）**：
- C-4（webhook replay）、C-5（namespace secret auto-gen，escalate planner）、C-6（project_hash 格式驗）、P-1（worker pool sizing=66）
- N-2（dispatcher retry，接線前修）、N-3（exc 洩漏泛化）、N-4（namespace call-args 斷言）、N-5（sha256 直接斷言）
- info：C-7 / Q-1（utcnow）/ Q-2 / Q-4 / P-3 / P-4

**durable 建議**：A-1 / C-2 / C-3 / C-5 / N-1 / P-2 進 `docs/tracking/followups/follow-ups.md`（Phase C cycle，標 enablement gate）。

## 13. Open Questions for Leader / Escalations

- **OQ-R-1（escalate planner via leader）**：design §5.6（`extra_headers`，A-1）+ §5.3（webhook dual-secret「同理」，C-3）+ §2.15（namespace secret auto-gen，C-5）三處 design 文字為源、impl 偏離/未實作。建議 leader 轉 planner 同步修 design + 排 Phase D。CAQA §10 已提同方向，我 concur。
- **OQ-R-2（cross-repo 守線）**：C-1 hardening 只關閉 skill-bundle 方向。請 Phase B 確認 pod WS verifier 對稱拒絕 `token_use != 'ws'`（或缺 channel_id）的 token——對稱半邊在 Phase B image，本 diff 不可見。
- **OQ-R-3（multi-agent severity 視角）**：silent-failure-hunter 主張 N-1/N-2/P-2 為「flag flip 前 must-fix gate，不可丟 loose follow-up」。我採其原則（已列入 §12 gate）。leader 若認為應更激進（例如要求本 cycle 即修 N-1/P-2 再 merge），請回填——但我方判斷為**不需**，inert merge 無風險，gate 在 enablement 即可。
- **無紅線觸發**：reviewer verdict（APPROVE WITH CAVEATS）= CAQA verdict（PASS WITH CAVEATS），無方向性歧異；新發現無 high-severity 安全洞。故**不 escalate leader 仲裁 verdict**。

## 14. Sign-off

- **Verdict**：**APPROVE WITH CAVEATS** — proceed to Reporter，merge Phase C 到 develop（inert / flag-gated）。
- **C-1 disputed**：implementation 層面已閉合（7daf1f4 正確 fail-closed），dispute mooted。
- **CAQA cross-check**：8 caveat 全同意（反對 0），新增 N-1~N-5；**無 CAQA 漏抓的 high-severity**。
- **Plan alignment**：緊貼 design；3 處 design-source 偏離（A-1/C-3/C-5）escalate planner。
- **Recommendation**：proceed to Reporter；Reporter 收尾 SC/caveat follow-up + durable A-1/C-2/C-3/C-5/N-1/P-2；leader 排 Phase D enablement gate；cross-repo 請 Phase B 補 WS 側 token_use 對稱檢查。
