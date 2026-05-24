---
name: reviewer-report-2026-05-25-agent-runtime-pod-phase-d
phase: reviewer
team: agent-runtime-pod
reviewer: agent-runtime-pod-reviewer
diff_base: a9a4d1f
branch: feature/agent-pod-phase-d
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
test_report_ref: docs/tracking/test-reports/test-coverage-agent-runtime-phase-d-2026-05-24.md
caqa_ref: docs/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md
created: 2026-05-25
status: approved
verdict: APPROVE（與 CAQA PASS WITH CAVEATS 對齊；5-agent verdict 無方向性分歧；HC-2 B1 獨立確認 airtight；0 blocker；2 caveat + carried-forward 收 Phase D flip-gate runbook）
related:
  - docs/tracking/reviewer-report-2026-05-24-agent-runtime-pod-phase-c.md
  - docs/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md
  - docs/decisions/patterns/constraint-input-trust-expansion.md
  - docs/tracking/pitfalls/cross-workspace-task-data-leak.md
  - docs/decisions/patterns/phase-versioned-test-assertion.md
---

# Reviewer Report — Agent Runtime Pod Phase D（enable-gate caveat hardening）

## 1. Final Verdict

**APPROVE** — proceed to Reporter，merge Phase D 到 develop。整段 flag-gated（`use_agent_runtime` default False）出貨即 inert。**0 blocker**。verdict 與 CAQA **PASS WITH CAVEATS 對齊**，**5-agent verdict 無方向性分歧**（全 proceed to Reporter）→ 不觸發紅線。

Phase D 是我 Phase C reviewer report + CAQA 提出的 enablement-gate caveat 的**逐條 remediation**。11 條 must-fix 全部正確關閉目標 caveat，無走偏、無回歸、fix 本身無新風險。**最敏感的 HC-2（B1 同源 owner-bootstrap）由 security-auditor 獨立確認 airtight**（並延伸驗到 Odoo framework context-merge substrate，refute 任何 residual normal-user / indirect injection 路徑）。

核心 hardening verified ✓（我自讀 diff + 5-agent 交叉確認）：
- **HC-2 / F-PA-14（B1-class，最敏感）** ✓：SUPERUSER 分支僅在明示 `bootstrap_owner_partner_id` hint 才 bootstrap；normal-user 分支用 `env.user.partner_id`、**完全不讀 hint**。grep 確認 production **零 set-site**（只 test 設、`project_project.py:130` 讀）；兩 production `sudo().create(project)`（`ai_assistant.py:1217`）vals 由 fixed allowlist 組、不寫 hint key 進 context；bogus/型別錯 hint 由 `browse().exists()` fail-safe。**B1 lineage 維持關閉**。
- **F-PC-4 operator fail-closed** ✓：unset key 由 pass-through 改 503；global `@app.middleware` 覆蓋 7 router 無旁路（無 sub-app/WS/per-router Depends）；health/docs exact-match 豁免在 key 檢查之前。token-forgery primitive 堵住。
- **F-PC-3 namespace fail-closed** ✓：移除 `_DEFAULT_SECRET` 公開 fallback，缺 env 即 `raise UserError`；全系統單一 hash 計算點（`_project_hash` delegate 回 `AgentSession.create`，不自算）。
- **F-PC-2 webhook dual-secret** ✓：`_verify_hmac_dual` = current OR previous；空 secret 被 `_verify_hmac` 的 `if not secret: return False` 擋 → 未設 `_PREVIOUS` slot **不放寬接受**；`compare_digest` timing-safe 保留；current 維持裸名（刻意不對稱，docstring 明載）。
- **F-PC-6 SSE aclose** ✓：`finally` 補 `loop.run_until_complete(agen.aclose())` unwind suspended generator → `async with websockets.connect` 的 `__aexit__` 跑 WS close（修每輪洩一條 pod 連線）；`agen` 永遠 bound（`relay()` 構造不 raise）；best-effort except logged（非 silent pass）。
- **F-PC-5 typed error** ✓：`AgentRuntimeOperatorError` on `not resp.ok` 帶 operator detail → 修我 Phase C N-1（operator error 被洗成 cryptic WS error）。
- **F-PC-1 websockets pin** ✓：`>=12.0,<14` 關閉 A-1 靜態半（`extra_headers` 在 14 改名）。
- **F-PC-7 token_use 3-端對稱** ✓：issuer 簽 claim 在 signed payload 不可竄改 / Odoo skill-bundle assert `=='skill_bundle'`（Phase C C-1 hardening）/ pod WS verify cross-repo（PR #3 A 嚴格，given）。
- **enablement surface** ✓：project toggle（default False）+ `project_views.xml` xpath inherit + demo template `noupdate="1"`，inert（demo template 綁無 project，resolver `('project_ids','in',project.id)` 撈不到 → 不可達）。

## 2. Diff Summary

### Base 校正（與 Phase A/C 同坑，已避開）

| 項目 | 值 |
|------|----|
| 本地 `develop` ref | 落後（develop..HEAD = 38 commit）→ **不可用 `develop..HEAD`** |
| Phase D 乾淨 base | `a9a4d1f`（最後一個 Phase C commit — 我的 Phase C reviewer report）— 與 CAQA `diff_base` 一致 |
| Phase D 真實 diff | `a9a4d1f..HEAD` = 4 commit / 29 檔 / +1224/-43 |
| commits | `5398f92`（red test）/ `8d152f7`（fix F-PC-1~6 + F-PA-14 + charts）/ `e2f1624`（feat enablement surface）/ `e94991d`（test fixtures + F-PA-14 guard） |

### impl 檔（按 hardening item）
- `extra/paas-operator/src/main.py`（+11）— F-PC-4 unset-key → 503 fail-closed
- `src/controllers/agent_runtime_webhook.py`（+31）— F-PC-2 `_verify_hmac_dual`（current OR previous）
- `src/models/agent_session.py`（+30）— F-PC-3 namespace secret 改 env-only fail-closed raise
- `src/controllers/ai_assistant.py`（+16）— F-PC-6 SSE aclose best-effort
- `src/services/agent_runtime_client.py`（+25）— F-PC-5 `AgentRuntimeOperatorError` typed error
- `requirements.txt`（+6）— F-PC-1 `websockets>=12.0,<14`
- `src/models/project_project.py`（+21）— HC-2/F-PA-14 sudo-only hint + normal-user-hint-ignored + enablement toggle
- `charts/odoo-dev-sandbox/templates/odoo-deployment.yaml`（+36）— 5 agent-runtime secret secretKeyRef `optional:true`
- `src/views/project_views.xml`（+19）+ `src/data/agent_template_demo.xml`（+16）— enablement surface
- `src/hooks.py`（+3）/ `src/__manifest__.py`（+2）— `_CONFIG_PARAMETERS` 移除舊 ICP 條目 + 版號

### test 檔
- `src/tests/test_agent_pod_phase_d_hardening.py`（+341）— F-PC-1~6 + F-PA-14 共 10 caveat guard
- `src/tests/test_agent_pod_phase_d_sse_aclose.py`（+262）— F-PC-6 fake-WS close 行為斷言
- `src/tests/test_agent_pod_phase_d_enablement.py`（+86）— toggle + demo template
- `extra/paas-operator/tests/test_auth_fail_closed.py`（+102）— F-PC-4 503 + 豁免
- `tests/test_phase_d_packaging.py`（+90）— F-PC-1 pin（已實跑 green）
- `extra/paas-operator/tests/test_agent_runtime_env.sh`（+82）— charts env 注入
- fixture 改動（~6 operator + 3 Odoo forward-coupling）— 詳見 §6 scrutiny

## 3. 委派工具 raw 輸出摘要（5-agent，per leader brief）

| Agent | tool_uses | Verdict | 重點 |
|---|---|---|---|
| `pr-review-toolkit:code-reviewer` | 30 | APPROVED WITH CAVEATS / 0 blocker 0 major | 8 hardening 全正確；**HC-2 ownership 問題調查清楚**（sudo-create-no-owner 是 Phase A 既有行為，access 走 workspace membership 非 owner row → 無回歸無 gap）；demo template inert 確認；3 info |
| `pr-review-toolkit:silent-failure-hunter` | 19 | 0 blocker/major silent failure | fix 皆 sound 無 re-introduce；1 minor = F-PC-6 `_logger.debug` level（systemic close 失敗 prod 不可見，建議升 info/warning）；fixtures 測真路徑非 mask |
| `pr-review-toolkit:pr-test-analyzer` | 22 | proceed to Reporter | **F-PA-14 10/10**（attacker 真負例 `assertFalse victim 無 owner row`）；每條 fix 有 weaken→red 負例；F-PC-6 fake-WS-close-on-live-loop 是真行為探針非 mock-theater；fixtures 合法 adaptation 非 erosion；1 follow-up = F-PC-2 empty-slot 未獨立 assert |
| `security-auditor` | 32 | APPROVED WITH CAVEATS / 0 blocker | **HC-2 獨立 CONFIRMED AIRTIGHT**：refute 任何 residual normal-user 路徑；延伸驗 Odoo `request.env.context` 不 merge request body（indirect injection 唯一可能藏身處）→ 確認關閉；1 caveat = cross-process secret（=D-CAVEAT-1）；1 optional = int-coerce hint |
| `pr-review-toolkit:comment-analyzer` | 20 | 0 critical（6 comment 全準確） | 1 major doc-sufficiency = **HC-2 read-site comment 未警告 future set-site 風險**（描述了尚不存在的 "trusted caller"，B1 風險其實在未寫的 set-site）；2 minor = charts partial-deploy 警告 / websockets floor re-pin 警告 |

**5-agent verdict 無方向性分歧**：全部 proceed to Reporter / APPROVE。對核心（HC-2 airtight / F-PC-4 fail-closed 完整 / fix 無 re-introduce silent failure / test 真行為鎖定）verdict 一致正面。無 multi-agent 內部歧見需 escalate。

## 4. HC-2（B1 同源）深度驗證 — security-auditor 獨立確認 airtight

最敏感項，三方獨立覆核（我自讀 + CAQA security-auditor + 本輪 security-auditor）結論一致 **airtight，無 exploitable cross-tenant primitive**：

```python
if self.env.uid == SUPERUSER_ID:
    hint_partner_id = self.env.context.get('bootstrap_owner_partner_id')
    if not hint_partner_id: return projects        # 無 hint → 不 auto-own
    partner = self.env['res.partner'].browse(hint_partner_id).exists()
else:
    partner = self.env.user.partner_id              # normal user 忽略 hint，owns 自己
if not partner: return projects
```

- **normal-user 分支不讀 hint**：self-service `with_context(bootstrap_owner_partner_id=victim)` create owns 呼叫者，**非** victim。鎖於 `test_FPA14_normal_user_hint_ignored_self_owns`（雙斷言：caller 自 own + victim **無** owner row）。
- **production 零 set-site**：grep `*.py/*.xml/*.js/*.sh` 全 `src/`+`extra/`+`scripts/`+`data/` → 唯一非 test 命中為 read-site `project_project.py:130`。兩 production sudo-create（`ai_assistant.py:1217` `_create_project` vals 由 allowlist 組、`paas.py:1506` 是 browse/update 非 create）皆不寫 hint 進 context。
- **indirect injection 關閉**（本輪 security-auditor 新增的 framework 層驗證，CAQA 未做到此深度）：Odoo 18 `request.env.context` 由 server 算的 `session.context` 組，**非** request body；`JsonRPCDispatcher` 把 JSON `params` 放進 `request.params` 為 plain kwargs，attacker 的 `context` key **不 merge** 進 `request.env.context`；repo controller 只用 fixed literal key 呼 `with_context`（`from_paas_chat`/`mail_create_nosubscribe`/`skip_ai_reply`），無 `with_context(**kw)` 傳播。→ 無 inherited context 把 hint 帶進 downstream sudo create。
- **bogus hint fail-safe**：`browse(non_existent).exists()` → 空 → `if not partner: return`；非 int 在 create fail-loud（非靜默誤植），且無 untrusted set-site 可達。

**HC-2 ownership 問題澄清（code-reviewer 調查）**：`_create_project` sudo create 無 hint → 不 bootstrap owner row。經查此為 **Phase A 既有行為**（base `a9a4d1f` 已是 `if SUPERUSER_ID: return`），Phase D 只精煉 SUPERUSER 分支加 hint 能力，hintless path byte-for-byte 等同 → **無回歸**。亦非功能 gap：`_check_project_access`（`ai_assistant.py:53-58`）對 bound project gate 於 **workspace membership** 而非 owner row → 建立者經 workspace 仍有 access。owner row 是獨立的 ownership-tracking 關注點，與 access control 解耦。

## 5. CAQA Cross-Check

CAQA verdict = **PASS WITH CAVEATS**（0 blocker / 2 net-new caveat / 9 info / 3 carried-forward）。逐項立場：

**11 must-fix close 狀態**：全同意 CAQA「全正確關閉目標 caveat」。特別認可 **F-PC-3 的設計 pivot**（我 Phase C 原建議 C-5 auto-gen → planner 拍板改 fail-closed env 注入）——CAQA 評為「比 auto-gen 更佳（無 idempotency 風險）」，我 concur，這是對我原方向的改良非走偏。

**2 net-new caveat**（皆同意，非 blocker，收 flip-gate runbook）：
| # | CAQA 判定 | Reviewer 立場 |
|---|---|---|
| D-CAVEAT-1 cross-process namespace secret 同值 invariant（+charts `optional:true` 放大 partial 部署風險）| caveat，flag-gated | **同意** — deployment invariant 非 code 缺陷；單一 K8s Secret 同值掛全 Odoo pod + operator；hash 消費點加 startup/health 一致性斷言 |
| D-CAVEAT-2 token_use deploy-order 無 code gate + 1h 在途窗口 + operator rollback 未記載 | caveat，flag-gated + pilot 圈住 | **同意** — operator `/health` 暴露 `signs token_use:true` 供 checklist 機器驗；runbook 補反向 rollback 危害 |

**9 info**：全同意，無異議（含 F-PC-6 debug level、F-PC-5 detail surface bounded info-disclosure、`UserError` 型別 nit、design §3.4 SHARED_SECRET stale doc 等）。

**反對：0 條。** CAQA 分級嚴格、無浮報。

## 6. CAQA 未深入 / 補充的新發現（R-1~R-5；無 high-severity，皆 reporter 收尾 / flip-gate）

| # | 來源 agent | Sev | 位置 | 摘要 | 方向 |
|---|---|---|---|---|---|
| R-1 | comment-analyzer | minor（doc-sufficiency）| `project_project.py:122-125` | HC-2 read-site comment 描述了**尚不存在的 "trusted caller"**；B1 真正風險在未寫的 future set-site，comment 未警告下一個 set-site 作者「值必須 server-authoritative、絕不從 request 來」| **在 code comment 補 SET-SITE invariant**（與 CAQA 給 planner 的 forward-invariant 同源，但 comment-analyzer 主張除 design doc 外**也要寫進 read-site 旁**）|
| R-2 | silent-failure-hunter | minor | `ai_assistant.py:741` | F-PC-6 aclose 的 best-effort except 落 `_logger.debug`；systemic close-handshake 失敗在 prod（INFO/WARNING）不可見（**CAQA 已列 info，此為再確認**）| 升 `info`/`warning`；impact bounded（內層 `loop.close()` 仍回收 socket）|
| R-3 | pr-test-analyzer | minor（test）| `test_..._hardening.py` F-PC-2 群 | empty-PREVIOUS-slot 不放寬 invariant **code 正確但無獨立負例**；未來 refactor 拿掉 empty-guard 不會 red | 補一條 `empty PREVIOUS + 任意 sig → 401` 負例 |
| R-4 | security-auditor | optional（defensive）| `project_project.py:133` | `bootstrap_owner_partner_id` 未 `int()`-coerce 即 `browse()`；今天 unreachable（無 untrusted set-site）| 一行 `int()` guard 防未來 careless trusted caller 傳 string |
| R-5 | code-reviewer | info（cosmetic）| `agent_template_demo.xml:13` | demo template header comment 寫 "unpublished(draft)" 但設 `is_public=True`；內部一致無害（constraint 只擋 `is_public=False`+unbound+non-draft）| 一行 comment 措辭 tidy |

**關鍵判斷**：R-1~R-5 **無一 high-severity，無一 code 缺陷**（R-1/R-5 doc/comment、R-2 log level、R-3 test 補強、R-4 defensive micro-hardening）。最敏感的 HC-2 經 security-auditor 比 CAQA 更深的 framework-substrate 分析**確認 airtight**。→ **不觸發「CAQA 漏 high-severity → escalate leader」紅線**。R-1 與 CAQA 給 planner 的 HC-2 forward-looking invariant 同源（CAQA 已要求落 design / `constraint-input-trust-expansion.md`），comment-analyzer 補充「也寫進 read-site comment」。

## 7. carried-forward 確認（CAQA 請 leader disposition）

CAQA 標記 3 條 Phase C caveat 未進 Phase D scope，請 leader 確認「刻意延後」非「靜默丟棄」。我 concur 其分類：
1. **C-4 webhook replay/reorder → stale state**（caveat 級）：Phase D webhook 改動只動 HMAC verify（single→dual），**未碰 state-write**；`agent_runtime_webhook.py:80-88` 仍無 monotonic guard，reorder/retry 舊事件可覆蓋成過期 state（HMAC 圈外部 replay，但 operator 自身 3x retry + 網路 reorder 為真實觸發）。**enabler 欄位 `last_state_event_ts` 已就位**，未來補「新事件 ts ≤ 既有則跳過」即可。屬 carried-forward，非 Phase D 引入。
2. **C-6 operator project_hash authz**（info 級）：trust model = operator 信任已過 project_access gate 的 Odoo，故只驗格式不驗 ownership；pilot-gate 低優先。
3. **P-1 SSE worker-pin**（info 級）：需 live load 驗，pilot-gate（design §6.8 認列 pool=66）。

## 8. Plan Alignment

Phase D 精準對應 input cell-map 11 條 must-fix，無走偏。F-PC-3 設計 pivot（auto-gen → fail-closed）是 planner 在我 Phase C escalation 後拍板的改良。**design 文件待補（escalate planner，承接 Phase C）**：design §3.4 仍描述 skill-bundle 端 `SHARED_SECRET` raw-token fallback，但 A 嚴格 impl 已刻意移除（raw secret 無 token_use claim 會被拒）→ 文件 stale 自相矛盾，併入 planner 既有 design 補寫批次（§2.15 / A-1 / C-3 同批）。

## 9. 同源 code path 檢查

- **HC-2 hint set-site**：grep 全樹 → production 零 set-site，consumer 唯 `project_project.py:130`；無複製錯誤；唯一其他 owner-grant 點 `migrations/18.0.1.0.26/post-migrate.py` 由 `message_partner_ids`/`create_uid`（install context）derive，非 request-reachable。
- **namespace hash 計算**：全系統單一點（`_project_hash` delegate 回 `AgentSession.create`，不自算第二套 algo）；無殘留公開 fallback（grep 僅 test 出現舊名）。
- **空-secret 放寬不變式**：webhook `_verify_hmac` / skill-bundle / jwt_signer 三處 verify 對空 secret 一致 fail-closed。
- **fail-open pattern**：operator 單一 auth 旁路點（`main.py` middleware），F-PC-4 是唯一 gate，無第二 shortcut。
- **`with_context(**kw)` 傳播**：全 `src/controllers/` 無命中（indirect injection 關閉）。

## 10. Known Pitfalls 對照

| Pitfall / pattern | 對照結果 |
|---|---|
| [[cross-workspace-task-data-leak]]（B1）| **無重蹈** — HC-2 關掉 sudo 無條件 auto-own + normal 分支忽略 hint → ownership 不可偽造；security-auditor 獨立 refute residual 路徑 |
| [[constraint-input-trust-expansion]]（HC-2 pattern）| **教科書級套用** — authority 來自 trusted source（明示 hint），untrusted 分支忽略 context；forward-invariant 待落 design（R-1 / CAQA planner hand-off）|
| [[phase-versioned-test-assertion]] | **無重蹈** — Phase D test 採存在性/行為斷言，無硬編 schema 快照致脆 |
| CI container missing-node | **不適用** — 本 diff 無動 Gitea Actions；F-PC-7 cross-repo green 由 paas-agent-runtime PR #3 該 repo 盤證 |

## 11. Open Questions for Leader / Escalations

- **OQ-D-1（escalate planner via leader，承接 Phase C）**：design §2.15 / A-1 / C-3 補寫批次順手 reconcile §3.4 stale `SHARED_SECRET` fallback 描述；並把 HC-2 forward-looking invariant（`bootstrap_owner_partner_id` 必 server-authoritative、任何 controller 不得從 `kw`/`params`/inbound context 填）落 design + `constraint-input-trust-expansion.md`，鎖未來 operator provisioning 真 set-site。
- **OQ-D-2（leader disposition）**：確認 carried-forward C-4 / C-6 / P-1 為刻意延後（pilot-gate / accepted-risk）非靜默丟棄；C-4 enabler 欄位已就位。
- **OQ-D-3（Phase D flip-gate runbook）**：D-CAVEAT-1（單一 K8s Secret 同值掛全 Odoo pod + operator）+ D-CAVEAT-2（token_use 簽發確認 + 反向 rollback 危害）收進 flip-gate 檢查項。
- **無紅線觸發**：reviewer verdict（APPROVE）= CAQA verdict（PASS WITH CAVEATS）；5-agent 無方向性分歧；新發現無 high-severity 安全洞 → **不 escalate leader 仲裁 verdict / 不 escalate planner 設計問題**（HC-2 是正確套用既有 pattern，非 design 漏洞）。

## 12. Sign-off

- **Verdict**：**APPROVE** — proceed to Reporter，merge Phase D 到 develop（inert / flag-gated）。
- **HC-2 B1**：三方獨立確認 airtight（security-auditor 延伸 framework-substrate 驗證 refute indirect injection）。
- **11 must-fix**：全正確關閉目標 caveat，無回歸、fix 無 re-introduce silent failure。
- **CAQA cross-check**：2 caveat + 9 info 全同意（反對 0）；新增 R-1~R-5 皆 reporter 收尾 / flip-gate，無 high-severity。
- **5-agent**：verdict 無方向性分歧，全 proceed to Reporter。
- **Recommendation**：proceed to Reporter；Reporter 收尾 R-1~R-5（HC-2 set-site comment / F-PC-6 log level / F-PC-2 empty-slot 負例 / int-coerce / demo comment）+ durable D-CAVEAT-1/2 + carried-forward C-4 進 `follow-ups.md`；leader 排 Phase D flip-gate runbook（OQ-D-3）+ confirm carried-forward disposition（OQ-D-2）+ planner design 補寫（OQ-D-1）。
