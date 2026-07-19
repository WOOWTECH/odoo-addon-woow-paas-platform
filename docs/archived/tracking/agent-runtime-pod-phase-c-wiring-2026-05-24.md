---
name: agent-runtime-pod-phase-c-wiring-2026-05-24
phase: phase-c-completion
epic: agent-runtime-pod
team: agent-runtime-pod
reporter: agent-runtime-pod-reporter
created: 2026-05-24
verdict: APPROVE WITH CAVEATS（CAQA PASS WITH CAVEATS + reviewer APPROVE WITH CAVEATS，0 blocker；C-1 disputed item 經 hardening 7daf1f4 MOOTED）
cross_repo:
  - paas-agent-runtime（Phase B image repo；C-1 WS 側對稱補丁 PR #2 `ba6b417` + frozen contract `cache.py` consumer；引用以 repo 名 + repo-root 相對路徑標明）
related:
  - docs/plans/2026-Q2/agent-runtime-pod-design.md
  - docs/plans/2026-Q2/agent-runtime-pod-plan.md
  - docs/archived/tracking/caqa-report-2026-05-24-agent-runtime-pod-phase-c.md
  - docs/archived/tracking/reviewer-report-2026-05-24-agent-runtime-pod-phase-c.md
  - docs/archived/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md
  - docs/decisions/patterns/phase-versioned-test-assertion.md
  - docs/tracking/followups/follow-ups.md
status: archived
archived-from: docs/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md
archived: 2026-07-19
archived-reason: "agent-runtime epic phase durable 報告；epic 已收官（prod 已上線）"
---

# Phase C Durable — agent-runtime pod epic wiring 收口

> 本 doc 是 `agent-runtime-pod` epic 的 **Phase C 完成 durable**，由 epic reporter 寫於 woow_paas_platform（epic durable home）。Phase C 把 **Phase A schema**（M:N + 4 model + 16 rule）與 **Phase B image**（`paas-agent-runtime` repo 的 pod runtime）端到端接起來：operator 簽 token + 7 endpoint、Odoo skill-bundle endpoint + webhook + SSE bridge、migration `.27`。CAQA + reviewer 雙 **APPROVE WITH CAVEATS，0 blocker**，整段躲在 `project.use_agent_runtime`（default False）後出貨即 **inert**。
>
> 本 doc 不重複 caqa/reviewer report 的逐條 caveat（那兩份是 review 的 canonical home），也不重列 Phase D 待修清單（彙整在 [[follow-ups]] §Phase D enablement gate）。它記 **cross-phase learnings**：(1) C-1 disputed severity 的「第三條路」（hardening MOOTED）；(2) cross-repo C-1 token_use 三端對齊的 **sign-first-then-verify zero-downtime** rollout；(3) phase-versioned test 第二次撞牆 → 抽 [[phase-versioned-test-assertion]] pattern。

## 1. Phase C 交付 summary（4 塊 wiring + C-1 hardening）

| 項目 | 內容 |
|------|------|
| **乾淨 diff base** | `9479cb2`（Merge PR #58）— 本地 `develop` behind 20，**不可用 `develop..HEAD`**（會把 Phase A 89 檔帶進來重審；與 Phase A 同坑，CAQA/reviewer 皆已避開）|
| **Phase C diff** | `9479cb2..HEAD` = 12 commit / 29 檔 / +3980 |
| **3 個 impl commit** | `6abb490`（operator endpoints）/ `ec226b7`（Odoo wiring）/ `d3aa60e`（M.9 版號無關 test）|
| **C-1 hardening** | `7daf1f4`（token_use claim defense-in-depth，**CAQA 後才落地**）+ red test `1305b77` |
| **Verdict** | CAQA **PASS WITH CAVEATS** + reviewer **APPROVE WITH CAVEATS**，0 blocker；flag-gated inert |

### 4 塊 wiring（對應 design §）

| 塊 | 落地 | design § | 守住的線 |
|----|------|----------|---------|
| **operator endpoints** | `extra/paas-operator/` — `agent_runtimes.py` 7 endpoint（spawn-or-attach / reap / resume / invalidate-mcp / sync-workspaces / delete / status）**additive router**（既有 6 router 不動）；`jwt_signer.py` stdlib HS256 雙 token；`helm.py` +2 method（`get_deployment_replicas` / `set_deployment_env`）純 additive | §2.11 / §2.11.1 | operator 既有 router/helm 零改動 |
| **Odoo wiring** | `src/controllers/agent_runtime_skill.py`（skill-bundle endpoint，HC-2 token-authoritative）+ `agent_runtime_webhook.py`（入站 state-update HMAC verify）+ `src/services/agent_runtime_client.py`（Odoo→operator HTTP）| §5.3 / §6.6 | HC-2 / HC-4 / dual-secret |
| **SSE bridge** | `src/controllers/ai_assistant.py` SSE dual-mode：flag gate（`:395`）+ `_stream_agent_runtime` WS↔SSE relay | §5.6 | B1 project_access gate 在 spawn 之前 |
| **migration + cron** | `migrations/18.0.1.0.27/post-migrate.py`（2 欄 `ADD COLUMN IF NOT EXISTS` nullable）+ `agent_session.py`（`last_synced_at`/`last_state_event_ts` + `_cron_drift_detection`）+ drift cron（5min）| §5.5 | migration idempotent，無 phantom field |

> **Phase C inert 的物理依據**：`project_project.py:65` `use_agent_runtime = fields.Boolean(default=False)` + gate `ai_assistant.py:395` 僅 True 才入 pod branch（SSE.1 test 鎖定 default）。Phase C 出貨**不啟動 live pod 流量** → caveat 群全 flag-gated，無一擋 inert merge。

## 2. 雙 verdict + C-1 dispute 的「第三條路」（MOOTED）★

| Review | Verdict | 結構 |
|--------|---------|------|
| CAQA（agent-runtime-pod-caqa）| **PASS WITH CAVEATS** | 0 blocker / 8 caveat（含 C-1 **disputed**）/ 6 info |
| reviewer（agent-runtime-pod-reviewer）| **APPROVE WITH CAVEATS** | 8 CAQA caveat **全同意**（反對 0）+ 新 5（N-1~N-5）+ 6 info；0 blocker |

兩 verdict **方向一致**（PASS↔APPROVE WITH CAVEATS），無方向性歧異 → 不觸發紅線。reviewer 新增的 N-1~N-5 **無一 high-severity 安全洞**（全 error-surfacing / 觀測性 / test 衛生，flag-gated 或未接線）→ 不觸發「CAQA 漏 high-severity → escalate」紅線。

### C-1 disputed severity 怎麼收的（值得 durable 的 process 點）

C-1（token 無 explicit `token_use`/`scope` claim，ws vs skill_bundle 僅靠隱性 claim shape 區分）在 review 內走出 disputed severity 的**第三條路**——既非「接受 caveat」也非「block」，而是**在 reporter 之前補 hardening 把爭議消滅**：

1. **CAQA 不靜默降級**：security-auditor sub-agent 判 **blocker**，CAQA 自身嚴格分析判 **caveat（medium）**。CAQA 沒有偷偷選一個，而是 §7「Disputed severity 上呈 leader」把兩方論點全攤開，verdict 明寫「取決於 leader 對 C-1 的裁決」。
2. **leader 裁決走 hardening**：commit `7daf1f4`（+ red test `1305b77`）加 `token_use` claim 並在 verifier assert。**CAQA 從未審過此 commit**（在 CAQA report 之後落地）。
3. **reviewer 獨立驗證閉合**：reviewer §5 walk `7daf1f4`，確認 **fail-closed**——`agent_runtime_skill.py:105` 在 signature+exp verify **之後**才檢 `token_use != 'skill_bundle'`，exact-string allowlist 使 absent(None) / `ws` / 偽造值 / 型別混淆（list/int）**全 403**；forged token_use 必先過 HS256（攻擊者無 secret 偽造不了）。
4. **dispute mooted**：C-1 在 implementation 層已修掉，不需再回答「blocker 還是 caveat」。

> **learning**：reviewer 的 disputed-severity 流程不是只有「上呈 → leader 二選一」。當爭議點本身廉價可修（C-1 是 defense-in-depth，非架構改動），leader 的最佳解常是「**hardening 把它變成 non-issue**」，reviewer 再獨立 verify fail-closed。CAQA「不靜默降級、全揭露」是這條路能走通的前提——若 CAQA 偷偷把 blocker 壓成 caveat，leader 不會知道有東西該修。

### multi-agent severity 分歧（reviewer 內部，誠實揭露）

reviewer 的 silent-failure-hunter 對 N-1 / N-2 / P-2 標 **MAJOR**（flag-on 視角），code-reviewer 標 **minor**（當前 inert 視角）。reviewer 仲裁取**雙方交集**：當前不擋 inert merge（code-reviewer 視角）**+** 列入 Phase D enablement gate must-fix（silent-failure-hunter 原則，不丟 loose follow-up）。這是**時間視角差**非方向性 dispute，未 escalate leader。

## 3. 守線全過（逐項 evidence）

Phase C 是 epic 安全邊界最密集的一段（接 live pod 流量的所有 surface 都在此 wiring）。CAQA + reviewer 雙獨立覆核，三條既有安全防線 + 四條 Phase C 新契約**全部守住**：

| 守線 | 結果 | Evidence |
|------|------|---------|
| **HC-2 token authoritative（第三度落地）** | ✓ skill-bundle 用 verified token claim `project_id` 解析，**絕不**信 query `project_hash` | `agent_runtime_skill.py:109/113-117`；token 為 project A 簽就撈不到 project B；對齊 [[constraint-input-trust-expansion]] |
| **HC-4 未被 `sudo()` 繞過** | ✓ skill-bundle 的 `sudo()` 是 **machine-auth 獨立路徑**（HS256 shared secret + token project_id 為界），非 user-session `/web/content` 側通道 | anchor stamping + ir.attachment deny rule 對 user 完整未動；Phase B `cache.py:13`「假設 attachment 已 anchor」對齊 |
| **B1 project_access gate 在 spawn 之前** | ✓ `_check_project_access`（`ai_assistant.py:577`）在 spawn（`:610`）之前 | 順序 CSRF→channel→flag→project_access→spawn；`invoker_role` server-side 解析非 client 輸入；無 [[cross-workspace-task-data-leak]] 回歸 |
| **frozen contract 逐欄位精確相符** | ✓ Odoo skill-bundle endpoint ⇄ Phase B `cache.py` 7 元素逐欄位（URL/query/token env/auth header/raw-bytes body/content-type/status）| CAQA §2.4 + reviewer §11 **雙獨立覆核**；C-1 新增 403 path 被 `cache.py:83 except HTTPError` 統一吃成 `SkillFetchError`，對 contract 無破壞 |
| **resume token re-sign 無 race** | ✓ `_resume_pod` sign→set_env→scale-1 順序（`agent_runtimes.py:76→81→84`）| env patch **嚴格先於** scale-1，pod 重建即帶 fresh token |
| **dual-secret 不放行 expired token** | ✓ exp 在 signature match **之後**檢，兩端一致 | `jwt_signer.py:172` + `agent_runtime_skill.py:77`；previous secret 不給 expired token 第二次機會 |
| **migration `.27` additive/idempotent** | ✓ `ADD COLUMN IF NOT EXISTS` nullable，欄位型態與 `agent_session.py:89-96` 精確相符 | 無 [[plan-vs-odoo-schema-mismatch]]（phantom field=0）；既有 row 留 NULL 被 cron 視 stale 撿起 |

## 4. cross-repo C-1 token_use 三端對齊（sign-first-then-verify zero-downtime）★

這是 Phase C 最值得 durable 的工程點。C-1 hardening 不是單檔改動，而是 **跨 repo 三端**的 token 用途分離契約，且用了一個對 rolling deploy 安全的分段 rollout。

### 三端

| 端 | 角色 | 落地 | repo |
|----|------|------|------|
| **operator `jwt_signer`** | **簽** — `sign_ws_token` 帶 `token_use:"ws"`（無 project_id）；`sign_bundle_token` 帶 `token_use:"skill_bundle"`（+ authoritative project_id + workspace_ids）| `7daf1f4` | woow_paas_platform `extra/paas-operator/src/services/jwt_signer.py` |
| **Odoo skill-bundle endpoint** | **verify（bundle 方向）** — assert `token_use == 'skill_bundle'`，缺/偽造/型別混淆全 403 | `7daf1f4` | woow_paas_platform `src/controllers/agent_runtime_skill.py:105` |
| **pod WS verifier** | **verify（WS 方向）** — assert `token_use == 'ws'`（B 過渡語意）| **PR #2 `ba6b417`** | **`paas-agent-runtime` repo** `runtime/transport/auth.py` |

operator 一處簽、兩個方向各自 verify：ws-scope token **無法**被重放為 bundle fetch（skill-bundle 方向已 airtight，reviewer §5 verified）；bundle-scope token 缺 channel_id，正確的 WS handler 應 reject（WS 方向由 `ba6b417` 對稱補上）。

### sign-first-then-verify zero-downtime rollout

cross-repo + rolling deploy 下，**若 verifier 立刻嚴格要求 `token_use` claim，舊 operator 還沒簽 token_use 的 in-flight token 會全被 reject → 中斷**。三端分兩段收：

1. **sign-first**：operator 先開始簽 `token_use`（`7daf1f4`）。
2. **then-verify（B 過渡 / transitional）**：verifier 先用**寬鬆語意**——`token_use` 存在則 assert、**缺則放行**（backward compat）。`ba6b417` 的 commit message 自陳「**B transitional**」正是此語意（WS verifier 對缺 claim 的 in-flight token 不中斷）。
3. **A 嚴格收緊**：等 **operator prod 全部都簽了 `token_use`** 之後，再把 verifier 從 B 過渡切到 **A 嚴格（缺 → reject）**。此步列 **Phase D enablement gate**（見 [[follow-ups]]）——不能在 operator 還沒全簽前切，否則 rolling window 內舊 token 被拒。

> **learning（generalizable）**：**跨 repo / 跨 deploy-unit 收緊一個 security claim，要 sign-first-then-verify 分兩段**——簽發端先加欄位、驗證端先寬鬆（缺欄放行）過渡、待簽發端全量上線後再切嚴格。一步到位（verifier 直接嚴格）會在 rolling 窗口內把舊 token 全拒。這和「[[constraint-input-trust-expansion]]：security 決策改用 authoritative explicit claim」是同一個方向——C-1 把「ws vs skill_bundle 靠隱性 token shape 區分」升級成「explicit `token_use` claim」，是該 pattern 在 token 層的第三度應用（Phase A m2m grantor → Phase B WS payload→claims → Phase C token_use）。

## 5. phase-versioned test 第二次撞牆（M.9）→ 抽 pattern

Phase C 的 manifest 版號合法 bump（`18.0.1.0.26` → `.27`，plan §Phase C Task 13 / impl `ec226b7`）撞到 **Phase A 的 M.9 test 硬編了當時版號**：

```python
# 撞牆前（Phase A 簽核當下對，但鎖死「那一刻的精確值」）
self.assertIn('"version": "18.0.1.0.26"', content)   # ← Phase C bump .27 → test-vs-test red
```

`d3aa60e` 改成版號無關，鎖 test 的**真實 intent**而非當時值：

```python
# (a) Phase A migration dir 仍在 Odoo upgrade path 上（存在性）
self.assertTrue(os.path.isdir(_MIGRATION_DIR))
# (b) manifest version >= (18,0,1,0,26)（monotonic，不 pin）— 後續 phase 可 bump 更高
version_tuple = tuple(int(p) for p in match.group(1).split('.'))
self.assertGreaterEqual(version_tuple, (18, 0, 1, 0, 26))
```

**這是第二次**：Cycle 1 已 retire 過 `test_ac2`（硬編當時 UNIQUE constraint 形狀）。`d3aa60e` 的 docstring 自證「same model as Cycle 1 retire of test_ac2」。同一根因出現兩次 → 觸發抽 pattern：[[phase-versioned-test-assertion]]。

> **learning**：phase-N 的 test 若 assert「**當時 schema 的精確值**」（版號 `== "18.0.1.0.26"`、constraint 名、欄位 count），會與後續 phase 對同一 schema 的**合法演進**互斥，炸成 test-vs-test red——不是 regression，是 test 把「快照」誤當「不變式」。改用 **monotonic（`>=`）/ 存在性（dir/欄位在）/ 結構（parse 得出 tuple）** 斷言，鎖 test 的真實 intent。詳見 pattern doc。

## 6. cross-phase invariant 守線

| Invariant | Phase C 守線 | Evidence |
|-----------|------------|----------|
| **Inv A1**（[[constraint-input-trust-expansion]]）| ✓ 第三度兌現——skill-bundle `project_id`（HC-2 守線）+ C-1 `token_use`（hardening）皆 authoritative claim，不信 client query/shape | §3 + §4 |
| **Inv A2**（[[attachment-row-level-acl-and-anchor-stamping]]）| ✓ HC-4 未被 `sudo()` 繞過；pod 端零直連 `ir.attachment`（Phase B 守線延續）| §3；`cache.py` 透過 HTTP endpoint fetch，依賴 woow_paas_platform 側 anchor stamping |
| **B1**（[[cross-workspace-task-data-leak]]）| ✓ `_check_project_access` gate 在 spawn 之前；`invoker_role` server-side 解析 | §3 |
| **design §5.3 token claims 權威** | ✓ C-1 hardening 是其**乾淨 superset**（補上 §6.6 未要求但廉價的 scope assertion）| reviewer §8 |
| **frozen contract（skill-bundle ⇄ `cache.py`）** | ✓ 逐欄位精確相符 + C-1 的 403 對 contract 無破壞 | CAQA §2.4 + reviewer §11 |

> **跨 repo 連動兌現窗口**：Phase B report §6 + [[follow-ups]] PB-link-1 標的「Phase C wiring 前必確認 woow_paas_platform 側 anchor stamping + row-level ACL 落實」在本 phase 兌現——frozen contract 逐欄位驗證 + HC-4 machine-auth 路徑確認，證實 `cache.py` 的 attachment 假設成立。F-PA-15 / F-PA-16 的 controller lock-step 仍是 Phase D 開 admin UI / controller sudo path 時的前置（見 [[follow-ups]]）。

## 7. reporter phase 本 cycle 落地

- **本 report**：`docs/archived/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md`（Phase C 完成 durable + cross-phase learnings）
- **Phase D enablement gate 彙整**：`docs/tracking/followups/follow-ups.md` §Phase D enablement gate——把 CAQA 初列的 A-1/C-3/C-5（commit `3cab339`）擴充為 reviewer 最終分類（must-fix: A-1/C-2/C-3/N-1/P-2/C-1-A-嚴格；follow-up: C-4/C-5/C-6/P-1/N-2~N-5 + info）
- **新 pattern**：`docs/decisions/patterns/phase-versioned-test-assertion.md`（M.9 第二次撞牆抽）
- **CLAUDE.md**：testing pattern reference 進 Known patterns（見 §判斷）
- **cross-ref OQ 閉合**：OQ-R-1（design §5.6/§5.5/§2.15 已由 `3cab339` 補，A-1/C-3/C-5 plan-source 偏離同步）+ OQ-R-2（Phase B WS token_use 對稱補丁 PR #2 `ba6b417` 已 merge）皆 addressed

## See also

- Phase C review（合成來源，本 doc 引用不複製逐條 caveat）：
  - `docs/archived/tracking/caqa-report-2026-05-24-agent-runtime-pod-phase-c.md` §7 C-1 disputed / §8 caveat 清單 / §2.4 frozen contract
  - `docs/archived/tracking/reviewer-report-2026-05-24-agent-runtime-pod-phase-c.md` §5 C-1 hardening 閉合 / §11 frozen contract / §12 Phase D gate 拆分
- Epic 主文件：
  - `docs/plans/2026-Q2/agent-runtime-pod-design.md` §2.11（operator）/ §5.3 §5.6（wiring + SSE）/ §6.6（token verify）/ §2.15（C-5 namespace secret，`3cab339` 補）/ §1.3.2 Inv A1+A2
  - `docs/plans/2026-Q2/agent-runtime-pod-plan.md` §Phase C / §Phase D enable gate
- Phase A / Phase B durable（同 epic 前序階段）：
  - `docs/archived/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md` §4 HC-2 cross-phase 同型 + §6 cross-phase invariant
  - `docs/decisions/reviews/pr-description-draft-agent-runtime-pod-phase-a.md`
- Pattern doc（cross-phase 兌現 / 本 cycle 抽）：
  - [[constraint-input-trust-expansion]] — Phase C C-1 `token_use` 是其 token 層第三度應用（§4）
  - [[attachment-row-level-acl-and-anchor-stamping]] — Inv A2，HC-4 machine-auth 路徑守線（§3）
  - [[phase-versioned-test-assertion]] — M.9 第二次撞牆本 cycle 抽（§5）
- Cross-repo（Phase B image repo）：
  - `paas-agent-runtime` repo `runtime/transport/auth.py` + PR #2 `ba6b417`（C-1 WS 側對稱補丁，B transitional）
  - `paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` — Phase B → C/D carry-over canonical home
- Follow-up 追蹤：
  - `docs/tracking/followups/follow-ups.md` §Phase D enablement gate（F-PC-*）+ PB-link-1（跨 repo 前置依賴）
