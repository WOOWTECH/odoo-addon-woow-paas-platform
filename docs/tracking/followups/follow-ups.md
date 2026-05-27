# Follow-ups — PaaS refactor cycle 追蹤

> 本 doc 累積跨 refactor cycle 沒處理 / 刻意延後的事項。每條附 (severity, scope origin, 動手建議, 對誰處理)。Reporter 不解決，列冊讓未來 PR / issue 持續追蹤。

**State as of 2026-05-26**：
- Cycle 1（cloud-service ↔ project decouple, 2026-05-17）：F1-F11，B1 已 RESOLVED、PR APPROVED WITH CAVEATS
- Cycle 2（mcp cloud_service_id → project_id decouple, 2026-05-18）：F12-F20 新增，PR APPROVED WITH CAVEATS round 2，4 BLOCKER/MAJOR 全 close
- Cycle 3（Project Detail page, issue #48, 2026-05-20）：F21-F28 新增，APPROVED WITH CAVEATS，B-1 fresh-install fix verified；AC #28 reviewer E2E 完成 10/11 pass + 1 FAIL（E2E-G1，不擋 merge）；R-1 / R-2 hard caveats 收於 reporter phase（commit pending），R-3 / R-6 / E2E-G1 + 其他 polish 進 follow-up issue
- Cycle 4（MCP refactor URL redesign + unbound project ACL, issue #49 + #51, 2026-05-21）：**F22 + F23 RESOLVED**；F-X1~F-X8 新增；reviewer round 2 APPROVED WITH CAVEATS，3 hard caveat (HC-1/2/3) 全 closed in PR；1 new MINOR (SC-6b)；20 條 soft caveat (SC-1~SC-20) 分散 reporter inline / dedicated follow-up / batch 三類處理
- Cycle 5（Agent runtime pod Phase A — schema M:N + 4 new models + 16 rule + migration, 2026-05-22 → 2026-05-23）：CAQA round 1 BLOCKED（B-1：4 new model 無 ir.rule scope-restrict）→ impl round 2 補 16 rule + PA-5 Python gate + SK-6 path (a) field-level mask；CAQA round 2 APPROVED WITH CAVEATS；**B-1 closed + OQ-A-2 closed**；reviewer round 1 抓 4 HC + 28 SC（CAQA 漏抓 HC-1/HC-2/HC-3/HC-4）；leader 拍板 HC-3 + HC-4 hotfix in this PR，HC-1 + HC-2 標 reporter follow-up；impl round 3 補 `agent_template._check_project_ids_grantor_authority` + `skill._anchor_bundle_attachment` + `ir.attachment` row-level rule 兩條 + 8 test method；144/144 PASS + Full suite 664/0 fail；CAQA round 3 + reviewer round 2 APPROVED WITH CAVEATS；**HC-3 + HC-4 closed in PR；HC-1 + HC-2 + EC-3a + EC-4a → F-PA-13~16**；F-PA-1~F-PA-12 已於 round 2 reporter cycle 落地；18 invariants 守線（PR #47 + #54 共 11 條 + Phase A 7 條 + Phase A #8 新增）+ 2 new pattern doc（`constraint-input-trust-expansion` + `attachment-row-level-acl-and-anchor-stamping`）
- **agent-runtime-pod epic Phase B**（image source，**`paas-agent-runtime` repo**，2026-05-23）：14 production + 14 test + 11 commit + PR #1（CI green + merge 中）；CAQA Prism + reviewer Quorum 皆 **APPROVED WITH CAVEATS，0 BLOCKER**；HC-1（IP-literal SSRF）/ HC-2（ws_server claims authoritative — [[constraint-input-trust-expansion]] 的 image 端同型，pattern lens 兌現）/ subprocess rlimit real-trigger test / SC-7（role-scope tool cache）全 **round 2 內 resolved**；CI gate 三度價值（envelope 12→15 / cache path §3 vs §6 / checkout node-in-container）；carry-over SC-1~SC-9 + wiring assumptions 的 **canonical home = `paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md`**（cross-repo，不在本 doc 重列）；epic durable + pitfall 見本 repo `docs/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md` + `docs/tracking/pitfalls/ci-workflow-container-missing-node.md`
- **agent-runtime-pod epic Phase C**（epic wiring 收口：operator endpoints + skill-bundle + SSE bridge，2026-05-24）：`9479cb2..HEAD` = 12 commit / 29 檔 / +3980；3 impl commit（`6abb490` operator / `ec226b7` Odoo wiring / `d3aa60e` M.9 版號無關）+ C-1 hardening `7daf1f4`（+ red test `1305b77`）；CAQA **PASS WITH CAVEATS**（8 caveat + C-1 disputed + 6 info）+ reviewer **APPROVE WITH CAVEATS**（8 caveat 全同意 + 新 N-1~N-5；0 blocker）；**C-1 disputed severity（security-auditor 判 blocker vs CAQA 判 caveat）經 leader 裁決走 hardening `7daf1f4` MOOTED**（reviewer 獨立驗 fail-closed，既非接受 caveat 亦非 block，是修掉）；守線全過（HC-2 第三度 token-authoritative / HC-4 machine-auth 獨立路徑 / B1 gate 在 spawn 之前 / frozen contract 逐欄位 / resume race / dual-secret / migration idempotent）；cross-repo C-1 `token_use` **三端對齊**（operator 簽 + Odoo verify + pod WS verify `paas-agent-runtime` PR #2 `ba6b417`，**sign-first-then-verify zero-downtime**）；Phase-versioned test 第二次（M.9 `d3aa60e`，Cycle 1 retire test_ac2 是第一次）→ 抽 [[phase-versioned-test-assertion]]；OQ-R-1（design §5.6/§5.5/§2.15 `3cab339` 補 A-1/C-3/C-5）+ OQ-R-2（`ba6b417` merge）皆 addressed；**Phase D enablement gate must-fix（A-1/C-2/C-3/N-1/P-2/C-1-A 嚴格）見下方 §Phase D enablement gate**；epic durable 見 `docs/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md`
- **agent-runtime-pod epic Phase D**（enable-gate caveat hardening — must-fix close + admin 啟用面 + cross-repo wiring，2026-05-25）：`a9a4d1f..HEAD` = 4 commit（`5398f92` red / `8d152f7` fix F-PC-1~6+F-PA-14+charts / `e2f1624` feat enablement surface / `e94991d` test fixtures）/ 29 檔 / +1224-43；CAQA **PASS WITH CAVEATS**（0 blocker / 2 net-new caveat / 9 info / 3 carried-forward）+ reviewer **APPROVE**（5-agent 無方向性分歧，全 proceed to Reporter）；**must-fix 全正確關閉目標 caveat**（F-PC-1~6 + F-PA-14 在本 repo close；F-PC-7 A 嚴格 = `paas-agent-runtime` `feature/f-pc-7-token-use-strict`（`a8b3ce2` red + `dabb275` feat），report 以 cross-repo「given/盤證」處理，**merge 到 main + deploy-order 屬 flip-gate**；F-PC-3 設計 pivot auto-gen→**K8s Secret env(b)** fail-closed，CAQA 評為比 auto-gen 更佳）；**HC-2/F-PA-14 經 security-auditor 獨立確認 airtight**（延伸 Odoo framework-substrate 驗 `request.env.context` 不 merge request body → indirect injection 關閉）—— [[constraint-input-trust-expansion]] 第 4 度 instance（owner-bootstrap hint，/simplify 自抓收緊 normal-user 不讀 hint）；2 net-new caveat（D-CAVEAT-1 namespace secret 跨 3 進程同值 + charts `optional:true` partial 風險 / D-CAVEAT-2 token_use deploy-order + 1h 窗口 + operator rollback）收 plan §Pilot runbook flip-gate；R-1~R-5 reviewer 新發現無 high-severity（R-1/R-5 comment reporter 順手補、R-2/R-3/R-4 見下 follow-up）；OQ-D-1（design §2.15/§5.5/§5.6/§3.4 reconcile）+ OQ-D-2（C-4/C-6/P-1 刻意延後 confirm）+ OQ-D-3（flip-gate runbook）皆 addressed；**epic Phase A-D code-ready 完成**（flip / 部署 / 2 週觀察由使用者依 runbook 自跑）；takeaway durable：[[credential-claim-test-fixture-coupling]] pattern + [[cross-repo-artifact-landing-assumption]] pitfall + HC-2 forward-invariant 落 [[constraint-input-trust-expansion]]；epic durable 見 `docs/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md`
- **agent-runtime-pod epic Pilot e2e**（Step 6 flip 實跑 → transport round-trip 閉環，2026-05-26）：使用者 task 63 送 chat → odoo SSE→WS bridge → pod `/v1/chat` → echo `text_chunk` → odoo `_persist_ai_response`（channel 16 OdooBot 回覆）→ 前端顯示，**transport round-trip 完整閉環**（ws_token 三方同值 HS256 auth 驗過 invoker_role=owner）。實跑串出 **9-gap chain**（見 [[agent-runtime-pilot-e2e-gap-chain]] pitfall），cluster 側由 paas-platform 連續修復破關：**PR #66**(operator ns-prefix 衍生+paas-managed label) / **#67**(operator projectId forward) / **#68**(odoo websockets dep) / **#69**(odoo↔pod WS envelope 契約 doc 交 image team) + 一串 ephemeral patch 撐到閉環；2 process learning 抽 pattern（[[spawn-install-values-missed-forward]] + [[plan-gap-stop-and-report]]）；skill-bundle 實際 fetch 待真 LLM agent 行使（echo 純 ack 不用 skills、連通已證 pod→odoo 200 + endpoint 存在 + NP 雙向，非 infra gap）；**durable 回收清單見下方 Index F-PILOT-1~10**（分 Tier 1 ephemeral 會被 Fleet/restart 洗 / Tier 2 系統性 chart-operator 缺口每 project 都撞 / plan-level 設計拍板）。**durable 收尾（2026-05-26）**：軌 2 直接轉正項 **F-PILOT-1/2/3/5/6/7 全 durable**——operator code `PR #66/#67/#71`（ns-prefix 衍生 / projectId / odooNamespace forward，3 missed-forward + contract test 鎖）merge develop、operator image `7b49f70` 建成；odoo websockets `PR #68` + image `20260526.1221` bake；charts `0.1.1`（chart-side default 修）；paas-cd pin 4 項（operator-stg image `7b49f70` + `AGENT_RUNTIME_CHART_VERSION=0.1.1` + `AGENT_RUNTIME_REPO_URL` external + odoo image `20260526.1221`）→ survive `helm upgrade --reuse-values` / Fleet sync。**剩 F-PILOT-4(registry provision) + F-PILOT-8(LLM cred) plan-gap 走 design 拍板，F-PILOT-3 ns-重建邊界 informational LOW（見下方詳述）**
- **agent-runtime-pod epic Phase E0**（pilot-revealed production gap — F-PILOT-4 registry pull / F-PILOT-8 LLM credential，**operator green**，2026-05-27）：`b46f0af..HEAD` = 4 commit / 3 檔 / +245（`76836b8` red → `e2a3d7b` T4.1a mock-fix → `3d7e083` operator green → `cdf5368` E0-R-1 lock）；CAQA **PASS WITH CAVEATS**（0 blocker / 3 caveat / 6 info / 1 out-of-scope）+ reviewer **APPROVE**（4 方無方向性分歧）；**scope = operator unit only**（`extra/paas-operator/`），cross-repo chart/cluster 僅 lock-step caveat。operator 兩條 provisioning（F-PILOT-4 registry pull secret forward — 複用 `helm.py:649`、imagePullSecrets 只帶 secret 名不帶 creds / F-PILOT-8 per-tenant `{release}-llm` Secret — keyless 早退）**正確且 scope 收斂**；**明文 LLM key 5 點獨立重驗不洩漏**（零 logger / 0600 tmpfile / generic handler / KubectlException message-only / Optional[str] 無 422 echo）；per-tenant 隔離 INV-2 成立。**E0-R-1**（key-absent 負分支 missing-negative，mutation 證實刪 guard 全 43 test 仍綠）tester `cdf5368` 補 `test_T81c` mutation-resistant 鎖 → [[credential-claim-test-fixture-coupling]] §missing-negative 形態。**T4.1a** 撞錯 test mock 對象（`ensure_registry_secret` 屬 KubernetesService 非 HelmService）→ 停下修 **test**（`e2a3d7b`）非 bend impl（會 AttributeError 炸 prod）→ [[plan-gap-stop-and-report]] §impl/test 層延伸。3 caveat 路由：**E0-CAVEAT-1**（orphan 明文 LLM Secret on teardown）→ **F-PILOT-10 HARD gate**（echo 階段邏輯不可能、真 LLM 上線前必收 + holistic teardown-lifecycle design）；**E0-CAVEAT-2**（chart `LLM_API_KEY` secretKeyRef optional:true）+ **E0-CAVEAT-3**（registry creds blast radius / SA-minimal RBAC + 建議 pull-only-scoped）→ cross-repo lock-step **owner paas-charts**；out-of-scope helm.py argv repo-read token（gap C `df6348e`）→ **F-GC-3**。報告 commit + 2 pattern doc 更新（credential-claim + plan-gap-stop-and-report）見下

## Index

| # | Severity | Origin | Cycle | 主題 | Status |
|---|----------|--------|-------|------|--------|
| F1 | medium | refactor | 1 | ACL 對等性 checklist 持久化 | doc-only ✅（本 doc 已寫進 [[odoo-addon-std-rule-symmetric-deactivation]]） |
| F2 | medium | refactor | 1 | `_sanitize_helm_values` 同名異實重命名 | 待新 PR |
| F3 | medium | refactor | 1 | Cross-workspace incompatible extras 注 AI prompt | 待設計 |
| F4 | medium | pre-existing | 1 | `_check_project_access` / `_check_task_access` 對 unbound IDOR | 待新 PR |
| F5 | medium | refactor (deferred) | 1 | Migration xmlid drift 防呆 assertion | 使用者 accept 不加本 PR；待新 PR |
| F6 | medium | refactor | 1 | D9 階段 2 — sanitizer 升級 whitelist 模式 | 等 telemetry，待新 PR |
| F7 | minor | refactor | 1 | BottomNav mobile vs Sidebar D4 拆分後 UX 一致性 | 待設計 |
| F8 | minor | refactor (test) | 1 | Test hardening — AC#11 / AC#14 / B1 group_project_user / sanitizer fallback | 待新 PR |
| F9 | minor | refactor | 1 | Sanitizer / `_loadCloudServices` 等 silent fallback observability | 待新 PR |
| F10 | minor | pre-existing | 1 | `_list_projects` with sudo + empty domain 跨 tenant 列 | D5=A 放大；待新 PR |
| F11 | minor | refactor | 1 | Sentinel `***REDACTED***` i18n 化 | 由 F6 觸發後一起評估 |
| F12 | medium | pre-existing | 2 | Controller `helm_values` 漏 sanitize（違反 cycle 1 Inv #6） | 與 F2 合併 or 獨立 hotfix |
| F13 | medium | refactor | 2 | B1 task ACL deactivation 對齊 `18.0.1.0.23` pattern | next refactor cycle `18.0.1.0.25` |
| F14 | medium | refactor (caveat) | 2 | Cleanup hook `mapped` 路徑 belt-and-braces `sudo()`（CAQA C1） | 待新 PR |
| F15 | info | refactor | 2 | `_do_cleanup` retry-on-error semantics 文件化（R2-1） | doc invariant inline + [[mcp-project-decouple]] §Inv |
| F16 | info | refactor | 2 | `mcp_server` / `mcp_tool` rule-count parity lint check（R2-2） | 待新 PR |
| F17 | minor | refactor (test) | 2 | `state='error'` re-entry test case（R2-3） | 待新 PR |
| F18 | trivial | refactor | 2 | `_create_admin_user` helper docstring 過 cautious（R2-4） | reporter 順手清 or 留 |
| F19 | minor | refactor | 2 | Round 1 17 minor / info 集合（silent-failure / type-design / migration assertion）| 留 batch 處理 |
| F20 | minor | refactor (simplify) | 2 | Controller manual check helper 抽取 + `_get_user_mcp_tools` mixin | 待新 PR |
| F21 | medium | refactor (cycle 3) | 3 | URL pattern 統一 + service-centric MCP endpoint deprecation (OR-1 / OR-6) | gitea #50 ✓ |
| F22 | medium | refactor (cycle 3) | 3 | D5 invariant 重設計 cluster — sidecar MCP auto-create / rebind race / `@api.constrains` ORM enforcement (OR-2 / OR-4 / OR-5 / R-4) | ✅ **RESOLVED cycle 4** — see [[mcp-url-redesign]]，issue #49 closed |
| F23 | medium | spec gap (cycle 3) | 3 | Unbound project ACL — endpoint 對 unbound 路徑無防 enumeration (OR-7 / R-4 partial) | ✅ **RESOLVED cycle 4** — see [[mcp-url-redesign]]，issue #51 closed（合 PR #49） |
| F-X1 | medium | refactor (cycle 4) | 4 | Cron `_cron_retry_mcp_sync` 仍 query `auto_created=True`，新 sidecar-backed row 不掃 (CAQA M1) | dedicated PR — `mcp_server.py:214-217` |
| F-X2 | medium | pre-existing + refactor (cycle 4) | 4 | `ai_assistant._check_project_access` bound-but-no-workspace blanket allow (CAQA M2) + paas.py `_resolve_project_access` 與 ai_assistant.py drift edge case (reviewer SC-6b) + F4 task path + F10 list path 整理 | cluster PR with F4 / F10 |
| F-X3 | low | refactor (cycle 4) | 4 | Codebase-wide comment ref noise — 4 處 production code 殘留過去 PR plan/OQ/CAQA refs (CAQA L1, reviewer SC-19) | reporter phase / batch with F19 |
| F-X4 | low | refactor (cycle 4) | 4 | URL helper duplication — `_compute_sidecar_url_hint` (controller) + `_model_build_mcp_endpoint_url` (model) algorithm 重複 (CAQA L2, plan OR-X8) — 含 SC-13 dead code 評估 | F19 follow-up |
| F-X5 | low | refactor (cycle 4) | 4 | SSRF error message IPv4 vs IPv6 格式微不一致（IPv6 用 `[{host}]`、IPv4 raw） (CAQA L3) | future PR / 統一格式 |
| F-X6 | trivial | refactor (cycle 4) | 4 | `test_mcp_d5_removal.py:194-200` 註解寫「D5 active」但 D5 已拔 stale (CAQA L4, reviewer SC-10) | reporter 順手清 or batch |
| F-X7 | medium | refactor (cycle 4 deferred) | 4 | OQ-2 (b) keep deprecated `auto_created` field — 等 F-X1 cron 改 sidecar_backed 後 drop column 安全 | follow-up after F-X1 |
| F-X8 | low | tester surface (cycle 4) | 4 | `scripts/test-addon.sh` worktree env setup gaps surfaced during cycle 4 test runs | dev script PR / batch with F24 sub-(b) |
| SC-cluster | minor | reviewer (cycle 4) | 4 | Round 1 reviewer 20 條 soft caveat（SC-1~SC-20）— SC-1/SC-2 doc updates by reporter; SC-3~SC-6/SC-6b 已聚 F-X1/F-X2; SC-7/SC-8/SC-9/SC-10/SC-11/SC-12 doc clean by reporter; SC-13 → F-X4; SC-14 (`write_actions` naming) → future PR; SC-15~SC-18 test gaps → batch with cycle 1 F8 + cycle 2 F17 + cycle 3 F27; SC-19 → F-X3; SC-20 → F-X4/F-X5 | 分散處理 |
| F24 | trivial | doc (cycle 3) | 3 | Issue #48 carve-out comment（OR-3）+ worktree env gaps（OR-8）+ declaration↔migration pitfall（OR-9） | reporter phase ✅ (本批已落) |
| F25 | important+ | reviewer (cycle 3) | 3 | Frontend UX hardening + error handling — McpServersSection delete/sync/test 沒 else (R-3) + `_delete_project_mcp_server` 沒 try/except + update 路徑沒 savepoint parity (R-6) + CloudServiceBindingSection rebind-from-unbound dropdown 空 (E2E-G1) | Follow-up issue: #52 |
| F26 | minor | reviewer (cycle 3) | 3 | Polish cluster — `workspace_id` 缺 `readonly=True`（R-5）/ `support_service.js` optimistic merge stuck spinner（R-9）/ `ir_rules.xml:217-238` comment 改 zh-tw（R-10） | 新 polish issue 待開 |
| F27 | minor | reviewer (cycle 3) | 3 | Test gaps G1 / G3 / G6（R-7）— update whitelist 範圍 / sync/test ACL matrix / XSS sanitize 未測 | tester 補 red 或 follow-up issue |
| F28 | long-term | architectural (cycle 3) | 3 | Description rich-text editor 升級 — `document.execCommand` (legacy) → ProseMirror / TipTap (R-8) | architectural issue，等 editor 需求變寬時開 |
| F-PA-1 | medium | refactor (cycle 5) | 5 | `agent_session._get_namespace_secret` fallback default `_DEFAULT_SECRET` hardcoded — 公開 git repo 可預計算 hash (CAQA round 1 M-3) | install hook hard guard PR — `agent_session.py:7` |
| F-PA-2 | medium | refactor (cycle 5) | 5 | `cloud_service._sidecar_url_prefix` return None for missing helm metadata → broad filter 可能誤殺其他 service sidecar-backed MCP (CAQA round 1 M-4) | fixture / pre-deploy edge case PR — `cloud_service.py:1175-1194` |
| F-PA-3 | medium | refactor (cycle 5) | 5 | `project.cloud_service_id` stored compute 失去明示 `ondelete='set null'` — verify Odoo 18 default 後補明示 (CAQA round 1 M-5) | small PR — `project_project.py:18-27`；need Odoo 18 stored compute M2O FK default verify |
| F-PA-4 | medium | refactor (cycle 5 deferred) | 5 | Phase A task #14-18 五個 view file defer — agent_template_views / agent_session_views / skill_views / project_access_views / project_external_mcp_setting (含 SSRF risk 警示)；leader 拍板 OQ-A-3 = defer Phase B/C | Phase B/C admin UI batch |
| F-PA-5 | low | refactor (cycle 5) | 5 | `skill_rule_project_user` perm_write/perm_create=False — design §3.2 line 1528「user W/C only own draft」implementation 簡化為 conservative R only；ir.rule 無法表達「own draft」需 Python state guard (CAQA round 2 C-A-1) | Phase B/C 補時加 state='draft' + uploaded_by_uid=self Python gate |
| F-PA-6 | low | refactor (cycle 5) | 5 | `ASSIGNABLE_ROLES_BY_ROLE[ROLE_OWNER]` 包含 ROLE_OWNER；design §4.3 line 2090 不含 owner — implementation 寬鬆（owner 可邀請其他 owner）；business intent 確認 (CAQA round 2 C-A-2) | leader / planner 拍板 design 是否更新；implementation 若改窄需 Python gate 同步 |
| F-PA-7 | low | refactor (cycle 5) | 5 | `skill.bundle_attachment_id` `groups='base.group_system'` field-level mask — 同時擋 project owner/admin 透過 backend form 直接 attach；admin 必須走 ORM sudo / API endpoint upload (CAQA round 2 C-A-3) | Phase B/C bundle upload API + maintainer doc update |
| F-PA-8 | low | refactor (cycle 5) | 5 | `discuss_channel.py:262` comment stale — 寫「project's cloud_service_id」但 implementation 已用 `task.primary_cloud_service_id` hybrid spec (CAQA round 1 I-1) | reporter 順手清 or batch |
| F-PA-9 | low | refactor (cycle 5) | 5 | `cloud_service._do_cleanup` 沒明示 `self.ensure_one()` — multi-record 在 helper 內 raise 行為不清楚 (CAQA round 1 I-3) | small refactor PR — `cloud_service.py:1086-1101` |
| F-PA-10 | low | refactor (cycle 5) | 5 | `project_task.primary_cloud_service_id` docstring「The compute acts as a default」描述不精確 — Odoo readonly=False + computed 是 implicit default idiom (CAQA round 1 I-4) | reporter 順手清 docstring |
| F-PA-11 | low | refactor (cycle 5) | 5 | `project_project.create()` override SUPERUSER_ID skip → migration step 9 fallback to `create_uid` partner，demo / install hook project owner 可能為 OdooBot partner (CAQA round 1 I-5) | Phase B/C 評估 demo data project 預設 owner 政策 |
| F-PA-12 | low | refactor (cycle 5) | 5 | `project_access._compute_user_id` 用 `sudo().search([('partner_id', '=', X)], limit=1)` 無 deterministic ordering (CAQA round 1 I-6) | small PR — 加 `order='id ASC'` |
| F-PA-13 | medium | reviewer (cycle 5 round 1 HC-1) | 5 | `project_project._inverse_cloud_service_id` 用 `[(6, 0, [X.id])]` replace semantics — Phase A migration 後既有 1-service M:N row 沒實際 break；**Phase B/C 多 service binding active 前必修**（否則 controller `cloud_service_id = X` write silently drop 其他 binding） | Phase B/C 必修 PR — `project_project.py:88-99` + `paas.py:1594` + `ai_assistant.py:944` |
| F-PA-14 | medium | reviewer (cycle 5 round 1 HC-2) | 5 | `project.sudo().create()` (在 `ai_assistant.py:946`) bypass `project_project.create()` override 對 `SUPERUSER_ID` skip bootstrap → creator silently 無 `project_access` owner row → 對 4 個新 model 的 16 條 ir.rule 全 fail → creator **看不到自己 project** 上 agent_template / agent_session / skill / project_access；**Phase D-F use_agent_runtime active 前必修** | Phase D-F 必修 PR — `project_project.py:122` + `ai_assistant.py:946` |
| F-PA-15 | info | reviewer (cycle 5 round 2 EC-3a) | 5 | HC-3 hotfix `_check_project_ids_grantor_authority` 對 `is_superuser()` + `has_group('base.group_system')` bypass — Phase B/C 加 controller endpoint 用 `agent_template.sudo().write({'project_ids': ...})` path 不 enforce constraint，必須對齊 [[two-layer-acl-lockstep]] pattern 在 controller carve-out 補 grantor check | Phase B/C controller layer touch PR 提醒 — `agent_template.py:94-122` |
| F-PA-16 | info | reviewer (cycle 5 round 2 EC-4a) | 5 | `skill._anchor_bundle_attachment` 對既有 attachment 的 `res_model` overwrite 是 destructive — 若 admin (`base.group_system`) 把已存在的 `mail.message` attachment（`res_model='mail.message'`）assign 給 `skill.bundle_attachment_id`，anchor 把 attachment 的 `res_model` overwrite 成 `'woow_paas_platform.skill'`，切斷原 mail.message linkage；docstring 已警告但需 maintainer doc 補強 | reporter 順手 / Phase B/C bundle upload API PR — `skill.py:108-129` docstring |

下方逐條細節 + 觸發條件 + 建議落點。F1-F11 維持 cycle 1 原文。F12-F20 是 cycle 2。F21+ 是 cycle 3 (issue #48 Project Detail page)。F-X1+ 是 cycle 4 (MCP refactor URL redesign)。F-PA-1+ 是 cycle 5 (Agent runtime pod Phase A)。F-PA-13+ 是 cycle 5 round 1 reviewer 抓 CAQA 漏抓 4 HC 中 leader 拍板 defer 的 2 條（HC-1 / HC-2）+ round 2 hotfix 落地後 reviewer 獨立 walk 抽出的 2 條 edge case 提醒（EC-3a / EC-4a）。

---

## F1 — ACL 對等性 checklist 持久化（doc-only）

- **Severity**: medium（不處理會在後續類似 PR 再炸 B1-class leak）
- **Status**: ✅ 已透過本批 docs 處理：
  - Pattern: [[odoo-addon-std-rule-symmetric-deactivation]]
  - Pitfall: [[cross-workspace-task-data-leak]]
  - Decision invariant: [[cloud-service-project-decouple]] §Invariants 4-5
- **下一步**：[[propose-claude-md-known-patterns]]（提議 leader / 使用者把 pattern doc 引用加進 CLAUDE.md，讓未來 dev / agent 改 ACL 時自動 surface）

---

## F2 — `_sanitize_helm_values` 同名異實重命名 / 抽 utils

- **Severity**: medium（語意混淆風險，不是 functional bug）
- **Origin**: 本次 refactor 新加的 `models/discuss_channel.py:41` `_sanitize_helm_values`（AI prompt sanitizer，pattern + blacklist + `***REDACTED***`）與 pre-existing `controllers/paas.py:1460` `_sanitize_helm_values`（API response sanitize，`secret` key 下 truncate `[:8] + '...'`）同名異實
- **影響**：未來維護者 grep `_sanitize_helm_values` 拿到兩個結果，可能誤用一個 sanitizer 在錯誤的 surface（如把 API sanitizer 用在 AI prompt 上 → 漏 password / token；或把 AI sanitizer 用在 API response → 改變既有前端 behavior）
- **建議落點**：
  - 重命名 `models/discuss_channel.py::_sanitize_helm_values` → `_sanitize_helm_values_for_ai_prompt`，並同步調整 callers
  - 或抽出共用 module-level helper（如 `src/services/helm_values_sanitizer.py`），明確兩個 mode（`for_api_response` / `for_ai_prompt`），controller / discuss_channel 都改 import
- **何時**：下次任一邊 sanitizer 邏輯需要動的 PR 順手做（不需要單獨 PR）

---

## F3 — Cross-workspace incompatible extras 仍注入 AI prompt

- **Severity**: medium（非 secret leak，但違反多租戶資訊隔離精神）
- **Origin**: 本次 refactor 對 task cross-project move 設計成「extras 保留」（AC #13），但 `_get_cloud_service_context` 走 `sudo()` 取 extras（`src/models/discuss_channel.py:267-280`），sanitizer 只去 secret 不去 cross-workspace
- **觸發路徑**：
  1. 使用者 A 在 W1 為 task 加 W1 服務 X 為 extras
  2. Task 被 cross-project move 到 W2（B 的 project）
  3. W2 member 在該 task chatter 觸發 AI 自動回覆
  4. AI prompt 內包含 X 的 service.name / subdomain（公開域名）/ template name / sanitized helm_values
- **洩漏內容**：service.name（可能含租戶業務語意）、subdomain、template name、helm_values（secret 已 redact 但保留 chart 配置）
- **建議落點**：
  - `_get_cloud_service_context` 過濾 `incompatible_extra_cloud_service_ids`，cross-workspace extras 即使存在也不注入
  - 或對 cross-workspace extras 注入時降級為「placeholder 提示 + service name only」（無 helm_values / subdomain）
- **設計待議**：兩個方向各有 UX tradeoff（過濾 = AI 不知道有 extras / placeholder = 部分資訊但複雜），由使用者決定

---

## F4 — `_check_project_access` / `_check_task_access` 對 unbound project 全放行（IDOR）

- **Severity**: medium（pre-existing IDOR；本次 refactor **放大 surface**）
- **Origin**: `src/controllers/ai_assistant.py:43-61` 對 `project.cloud_service_id IS NULL` 直接 return True，跳過所有 access check
- **Pre-refactor 情況**：unbound project 前端阻擋無法建立，路徑無實際 trigger
- **Post-refactor 放大**：D5=A 拔前端 gate，unbound project 可正常建立 → IDOR surface 浮上水面
- **可觸發 endpoint**：
  - `api_support_projects(action='get/update/delete', task_id=X)` — 對猜中 ID 的 unbound project / task 可直接 R/U/D
  - `_list_projects` / `_list_tasks` 前端不顯示 unbound（但 API endpoint 知 ID 即可操作）
- **建議落點**：
  - `_check_project_access` / `_check_task_access` 對 unbound 改檢查 `project.message_partner_ids` / `create_uid`，與 ir.rule 對齊
  - 或統一改走 ORM 路徑（`with_user(env.user).project.browse(id).check_access('write')`），讓 ir.rule 自動把關
- **何時**：下個 security audit / IDOR cleanup PR；不阻擋本 refactor merge

---

## F5 — Migration xmlid drift 防呆 assertion（deferred）

- **Severity**: medium（隱性風險）
- **Origin**: 本次 refactor reviewer cleanup #6；使用者 Q1 拍板「accept 不加本 PR」
- **問題**：`src/migrations/18.0.1.0.23/post-migrate.py:93-107` 的 3-row UPDATE 無 row-count 防呆。若 Odoo 18.x 某 patch 改 `task_visibility_rule` 等 xmlid，UPDATE 命中 < 3 rows，rowcount 仍 success，B1 leak 靜默回來
- **建議落點**：
  ```python
  cr.execute("""
      SELECT COUNT(*) FROM ir_model_data
      WHERE module='project' AND model='ir.rule'
        AND name IN ('project_public_members_rule',
                     'task_visibility_rule',
                     'task_visibility_rule_project_user')
  """)
  actual = cr.fetchone()[0]
  if actual != 3:
      raise RuntimeError(
          f"Odoo std rule xmlid drift detected: found {actual}, expected 3. "
          "B1 leak might silently return — abort migration. "
          "See docs/tracking/pitfalls/cross-workspace-task-data-leak.md"
      )
  ```
- **何時**：下次 migration touch 或 Odoo 版本升級前必加
- **替代防線**：Pre-deploy verify SQL（已寫進 PR description 的 deploy checklist + [[odoo-addon-std-rule-symmetric-deactivation]]）— 但靠手動跑

---

## F6 — D9 階段 2：sanitizer 升級 whitelist 模式

- **Severity**: medium（pattern 比 whitelist 鬆，但本次先 ship pattern 是對的 — 詳見 [[cloud-service-project-decouple]] §7）
- **Origin**: plan D9 拍板「A → B」，本次只做 A
- **觸發條件**：
  - 階段 1 telemetry 累積夠 dataset（哪些 keys 實際出現在 helm_values、哪些被 redact）
  - Pen-test / security audit 標示「pattern 漏網」實例
  - 或新 chart 上線 PR review 時發現需要更嚴的策略
- **建議落點**：
  - 新 helper `_sanitize_helm_values_whitelist(helm_values_json, allowlist)`，allowlist 由 chart-specific 配置驅動
  - `_sanitize_obj` 改為「key 不在 allowlist → 整 value redact」而非「key 匹配 pattern → redact」
  - 過渡期可雙模式並存，A 與 B 對同一 input 跑後 log diff 找出 pattern 漏網案例
- **何時**：階段 1 已 ship 至少 N 個月 + 有 telemetry 數據後

---

## F7 — BottomNav mobile vs Sidebar D4 拆分後 UX 一致性

- **Severity**: minor（PaaS 主要 desktop）
- **Origin**: `src/static/src/paas/layout/bottom_nav/BottomNav.js:18-24` mobile bottom nav 仍把 `ai-projects` / `ai-project-kanban` 算進 AI 入口；desktop sidebar 已拆出獨立 `Projects` entry
- **症狀**：mobile 上點到 Project Kanban 會 highlight "AI" tab
- **建議**：BottomNav 加 `Projects` entry 對齊 Sidebar；或把 mobile AI tab 內 sub-menu 加上 Projects
- **何時**：mobile UX 下次 sprint，或 D4 sidebar 確認最終 UX 形態時一起調整

---

## F8 — Test hardening

- **Severity**: minor（implementation 都對；test 是「future regression 強守線」缺塊）
- **Origin**: pr-test-analyzer + reviewer R7 / R8
- **缺塊**：
  - **AC #11 domain test 是 string-grep stub**：只 grep `'workspace_id'` literal 出現於 `field.domain.repr()`，未跑真實 ORM filter — 未來 implementation 用 `[('workspace_id', '!=', False)]` 之類無意義 domain 也能過關
  - **AC #14 dedup test format-brittle**：用 `count('Primary Svc') == 1` 偵測 dedup，prompt 模板改字（如 `### Primary Service`）可能繞過 — implementation 用 `extras - primary` recordset 層級 dedup 是對的，但 test 不擋未來 implementation 改成字串層級操作
  - **B1 regression 沒涵蓋 `group_project_user` write/unlink leg via `task_visibility_rule_project_user`**：PaaS 不給租戶 group_project_user，但 paranoia 加 1 條 sibling test 防 future role grant
  - **AC #15 sanitizer 深度 >5 / 非 JSON helm_values fail-closed path 未被測**：implementation `_sanitize_helm_values` 已 fail-close 為 `''`，無 test 守
- **建議**：每條對應加 1 條 test。落在下個 touch testing 的 PR

---

## F9 — Silent fallback observability

- **Severity**: minor（不破功能，但 silent fallback 影響 debug 與 incident 反查）
- **Origin**: silent-failure-hunter R9
- **缺塊**：
  - `_sanitize_helm_values`（`src/models/discuss_channel.py:41-49`）JSON parse 失敗 silently 回 `''`，無 log。Helm chart 把 `helm_values` 存 non-JSON 時 AI 拿不到 context，使用者完全不知為什麼
  - `_sanitize_obj` depth > 5 silently 回 `_REDACTED` literal，與真實 redacted value 無辨識
  - `CreateProjectModal._loadCloudServices` catch 後沒 set `state.error`（不一致於 `_loadWorkspaces`），dropdown 靜默變空使用者誤判「無 service」
  - `_post_ai_reply` 外層 except 把 programming error 跟 transient AI failure 包成同一個 generic notification — 影響 incident triage
- **建議**：逐條加 `_logger.warning` + observable signal（log level 降到 warning 足夠，不是錯誤）
- **何時**：observability 下次 sprint 或下個 touch 對應檔案的 PR

---

## F10 — Pre-existing `_list_projects` with sudo + empty domain 跨 tenant 列

- **Severity**: minor（pre-existing 不在本 refactor 引入）
- **Origin**: reviewer 偽陽性 verification 順帶發現
- **狀況**：`_list_projects` 在某 endpoint 用 `domain=[]` + `sudo()` browse → 跨 tenant 列所有 project；前端 `SupportProjectsPage` 呼叫 `fetchAllProjects()` 走無 workspace 過濾的 endpoint
- **與本次 refactor 的關係**：D5=A 拔前端阻擋後**放大可見度**（以前看不到的 unbound project 現在會出現在頁面），但 leak shape 是 pre-existing 的 controller-level filter gap，不是本次 refactor 引入
- **建議**：`_list_projects` 改走 ORM 路徑用 `with_user(env.user)`，依靠 ir.rule（已收緊）自動隔離 — 一致於 [[paas-workspace-member-acl-tightening]] 的整體方針
- **何時**：與 F4 IDOR cleanup 同一個 PR 處理較自然（都是 ai_assistant.py controller）

---

## F11 — Sentinel `***REDACTED***` i18n 化

- **Severity**: minor（純 audit token）
- **Origin**: plan §Open Q4 / PR description follow-up
- **判斷**：本次拍板 hardcode `***REDACTED***` 是對的（[[cloud-service-project-decouple]] §9 Q4）— 它是 audit grep-able token，不是 user-facing 文案
- **何時觸發**：若 F6（D9 階段 2 whitelist）執行時順帶評估是否需要 i18n（whitelist 模式可能會把 redacted value 暴露給更多 surface）
- **建議**：F6 PR 內一併評估；若決定 i18n 化，要同步改 plan / test / pitfall doc 內所有 `***REDACTED***` literal 引用

---

---

# Cycle 2 — MCP `cloud_service_id → project_id` Decouple (2026-05-18)

> Cycle 2 完成於 2026-05-18，commit chain `5c5d980 → 9f147d8`。Reviewer round 2 APPROVED WITH CAVEATS。新 F12-F20 條目來自 CAQA C1 / F12 + round 1 verdict 17 minor + round 2 verdict 4 new minor + reporter durable docs 寫作時順帶整理。

## F12 — 控制器 `helm_values` 漏 sanitize（pre-existing，違反 cycle 1 Inv #6）

- **Severity**: medium
- **Origin**: CAQA `docs/decisions/reviews/caqa-2026-05-18.md §F12` out-of-scope finding（CAQA 跨 cycle 1 + cycle 2 漏抓兩次）
- **Location**: `src/controllers/ai_assistant.py:609-610` — SSE streaming path 注入 `service.helm_values` 到 AI system prompt **未走 `_sanitize_helm_values`**；對比 `src/models/discuss_channel.py:303` non-streaming path 有 sanitize
- **Violation**: cycle 1 Invariant #6（AI prompt 注入 `helm_values` 必須經 `_sanitize_helm_values`）
- **為何沒在本次擋下**：本次 implementation diff 未改此段 code，無新增 surface。本次 verdict 評估「是否引入 / 加劇」— pre-existing 不擋
- **觸發路徑**：user 開啟 SSE chat 且 task 連到 bound project + `chat_enabled=True` + `ai_auto_reply=True` 才會 fire（非任何 user 隨意觸發）
- **建議落點**：
  - 路徑 1：與 F2（同名異實重命名 / 抽 utils）合併處理，徹底解決
  - 路徑 2：若評估 AI chat 已上 stg / 即將 prod，獨立 hotfix PR 立刻修（機械式 1-line fix：`service.helm_values` → `_sanitize_helm_values(service.helm_values)` 並 import）
- **何時**：next AI chat 相關 PR 一併處理

## F13 — B1 task ACL deactivation 對齊 `18.0.1.0.23` pattern（next refactor cycle `18.0.1.0.25`）

- **Severity**: medium
- **Origin**: round 2 verdict 42 reds baseline survey（commit 9f147d8 §Out-of-scope 42 reds）
- **狀況**：`test_cross_workspace_task_isolation` AC4 與 `test_project_decouple` 在 round 2 baseline 仍 red — failure pattern 是 B1 task regression，root cause 在 migrations `18.0.1.0.22/0.23` std-rule deactivation timing，與本次 cycle 2 無關
- **下個 cycle 動作**：建立 `src/migrations/18.0.1.0.25/post-migrate.py` 對齊 `18.0.1.0.23` project_public_members_rule pattern — single UPDATE + IN clause deactivate task std rules
- **不擋 cycle 2**：reviewer round 2 已驗 baseline survey 方法學健全（git stash → re-run baseline → 同 reds reproduce），cycle 2 commit range 內 0 改動這些 file → 無 blast radius
- **何時**：next refactor cycle，把這 42 reds 收尾

## F14 — Cleanup hook `mapped` 路徑 belt-and-braces `sudo()`（CAQA C1）

- **Severity**: medium
- **Origin**: CAQA `docs/decisions/reviews/caqa-2026-05-18.md §C1`
- **Location**: `src/models/cloud_service.py:1134-1136` + `:1192-1194`（`_do_cleanup` step 1 + `unlink()` override）
- **現狀**：
  ```python
  user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
      lambda s: s.scope == 'user'
  )
  if user_mcps:
      user_mcps.sudo().unlink()
  ```
  `mapped('mcp_server_ids')` 走 ir.rule（如果 caller context 不是 sudo）— 目前 caller 都是 cron / subscription_lifecycle_mixin / controller 直 unlink，預期都是 sudo / admin context，但 `_do_cleanup` 與 `unlink()` 都是 public method
- **風險**：未來 callable surface 擴大（如 non-sudo caller）+ user 對某些 user-scope MCP 無 read 權限（cross-workspace edge case） → `mapped` 漏掉那些 row → 沒 unlink → 變 zombie
- **建議落點**：
  - A：`self.project_ids.mapped('mcp_server_ids').sudo().filtered(...).unlink()` — sudo mapped 結果
  - B：`self.sudo().project_ids.mapped('mcp_server_ids').filtered(...).sudo().unlink()` — sudo 整條 walk
- **何時**：next touch `cloud_service.py` cleanup path PR 順手做

## F15 — `_do_cleanup` retry-on-error semantics 文件化（R2-1）

- **Severity**: info（doc invariant only，無 code change）
- **Origin**: round 2 verdict R2-1（code-reviewer 提的 info caveat）
- **狀況**：subscription cron 在 cancel 後再 trigger `_do_cleanup` 是 retry-friendly 設計（state='error' 不擋）— 後人若加 `if state == 'error': return` short-circuit guard 會打破 auto-retry
- **建議落點**：
  - 在 [[mcp-project-decouple]] §Invariants 新增「`_do_cleanup` 必須 re-entry safe — `state='error'` 不該 short-circuit return」一條（已部分於 cycle 2 finalize 補 — see Inv #4 helm fail state ordering description）
  - 在 `cloud_service.py:1119` docstring 加 retry note
- **何時**：reporter inline 處理（cycle 2 完成）— 或下個 touch `_do_cleanup` PR

## F16 — `mcp_server` / `mcp_tool` rule-count parity lint check（R2-2）

- **Severity**: info
- **Origin**: round 2 verdict R2-2（code-reviewer 提）
- **狀況**：無 automated lint 檢查 `mcp_server` / `mcp_tool` rule-count parity。依賴人眼 / [[mcp-project-decouple]] §Invariant #5 doc。任何後人加第 4 對 rule 漏對等 = B1-style sub-model leak
- **建議落點**：
  - A：post-migrate 加 invariant assertion — `SELECT COUNT(*) FROM ir_rule WHERE model_id IN (mcp_server, mcp_tool) GROUP BY model_id` — 兩個 count 不等 raise
  - B：CI lint script grep `ir_rules.xml` 內 mcp_server vs mcp_tool record count
  - C：dev-only `make check-mcp-rule-parity` target
- **何時**：next hardening sprint / next ir_rules.xml touch PR

## F17 — `state='error'` re-entry test case（R2-3）

- **Severity**: minor
- **Origin**: round 2 verdict R2-3（pr-test-analyzer 提）
- **狀況**：`_do_cleanup` 在 `state='error'` 時 re-entry 行為未鎖 test — 後人若加 short-circuit guard（見 F15）會打破 retry，無 regression lock
- **建議落點**：`src/tests/test_mcp_project_lifecycle.py` 加 case：
  ```python
  def test_do_cleanup_reentry_after_error_state(self):
      """If _do_cleanup left state='error' (helm uninstall fail),
      a subsequent _do_cleanup call must retry (not short-circuit)."""
      service = self._make_service_in_error_state(...)
      # _do_cleanup again should retry helm uninstall, not no-op
      with patch(...) as mock_helm:
          service._do_cleanup()
          mock_helm.assert_called_once()
  ```
- **何時**：next test hardening PR

## F18 — `_create_admin_user` helper docstring 過 cautious（R2-4）

- **Severity**: trivial
- **Origin**: round 2 verdict R2-4（pr-test-analyzer 提）
- **狀況**：`_create_admin_user` helper 的 docstring 對 `(4, ...)` stale-cache 行為的 claim 是 unverified Odoo 民俗，但實際用 `(6, 0, ...)` 是 idiomatic — 無害 over-cautious
- **建議落點**：clean docstring，移除 unverified Odoo 民俗 claim，保留「use `(6, 0, ...)` 的 idiomatic reason」就好
- **何時**：reporter 順手清 or 留下個 touch 順手做（trivial — 不擋任何 PR）

## F19 — Round 1 17 minor / info 集合（cross-reference round 1 verdict）

- **Severity**: minor / info（mixed）
- **Origin**: `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md §Minor / Info`（commit `e9a5927`）— round 1 reviewer 5-agent 平行 review 提的 17 條 minor，本 verdict 不擋 round 2 approval
- **內容 cluster**（不在本 doc 重複列每條，避免 ephemeral 雜訊）：
  - **Silent failure** (silent-failure-hunter H2/H3/M1-M5)：cleanup step 1 unlink 無 try/except、SSE streaming MCP fail silent fallback、migration pre-flight assertion 缺失、cron retry 無 user notification — 7 條
  - **Type design** (type-design-analyzer F1/F5/F6/F7)：D4 partial SQL CHECK 雙保險、CSV 1,1,1,1 可緊縮、`auto_created` write protection、`scope` 二元假設 — 4 條
  - **Test coverage hole** (pr-test-analyzer)：archived project + user-scope MCP 場景、localhost URL fallback 場景、`_simulate_auto_create` helper drift — 3 條
  - **Code clarity** (code-reviewer)：Controller "MCP Server not found" 對 system-scope misleading、`self.project_ids[:1]` 非確定性 ordering — 2 條
  - **CAQA typo**：21 AC → 25 AC — ✅ DONE（reporter phase 已修）
- **建議落點**：
  - 多數 silent-failure cluster 等 F9 observability sprint 一併 batch
  - Type design 等 next mcp_server.py touch PR 順手評估
  - Test coverage 跟 F8 batch
  - Code clarity 等 next paas.py touch PR 順手做
- **何時**：分散 batch，不獨立 PR

## F20 — Controller manual check helper + `_get_user_mcp_tools` mixin abstraction（/simplify）

- **Severity**: minor
- **Origin**: round 1 verdict §`/simplify` 後是否還有可簡化處
- **狀況**：
  - Controller manual check pattern `if not server.exists() or server.project_id.cloud_service_id.id != service.id` 在 `paas.py:1335/1353/1366/1384` 重複 4 次 — 可抽 `_check_mcp_server_belongs_to_service(server, service)` helper（與 F19 minor #12 一併）
  - `_get_user_mcp_tools` 在 `discuss_channel.py:310` 與 `_get_user_mcp_tools_for_channel` 在 `ai_assistant.py:614` chain walk 邏輯一樣，可抽 mixin function。Controller path 自己重寫一遍是 cursor invalidation 考量（line 415 註解有提及）— **可不簡化，但 surface 此 duplication 給未來 maintainer**
- **建議落點**：next touch 對應 file 的 PR 順手做。Test #26（AC #14）已是 behavioural lock，helper-wrap-safe（helper 內部走舊鏈仍會被抓）— 抽 helper 不會破測試
- **何時**：next paas.py / ai_assistant.py 整理 PR

---

---

# Cycle 3 — Project Detail Page (Issue #48, 2026-05-20)

> Cycle 3 完成於 2026-05-20，final commit `32cf414`。Reviewer `approved with caveats`。R-1（doc dead refs）+ R-2（BasicInfoSection seed sanitize）由 reporter phase 收（commit pending，leader 統一 amend）。其餘 caveats 進 follow-up issue / 既有 #49 #50 #51。本 cycle 新加 F21-F28。OR-* 編號對應 spec `docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` §Open risks，R-* 編號對應 reviewer report `docs/tracking/reviewer-report-2026-05-20-project-detail-page-issue-48.md`。

## F21 — URL pattern 統一 + service-centric MCP endpoint deprecation（OR-1 / OR-6）

- **Severity**: medium（架構 debt，不擋 ship）
- **Origin**: spec §Open risks OR-1 + OR-6；CAQA 確認；reviewer follow-up alignment
- **OR-1**：本 cycle 新增 `/api/support-projects/<id>/mcp-servers` project-centric endpoint，但舊 service-centric MCP endpoint（`/api/cloud-services/<sid>/mcp-servers`）仍保留為 dead code。需要 deprecate 流程（grep call sites → 移除前端 / 後端引用 → drop endpoint）
- **OR-6**：URL pattern 不一致 — 部分 endpoint 用 `/api/workspaces/<wid>/...` nesting，部分用 root-level `/api/support-projects/<pid>/...`。建議全部統一到 `/api/workspaces` nesting
- **Status**: ✓ 已 surface 在 gitea #50（合併 OR-1 + OR-6）
- **何時**：#50 排程

## F22 — D5 invariant 重設計 cluster（OR-2 / OR-4 / OR-5 / R-4）

- **Severity**: medium（D5 是物理現實 invariant，目前靠 multi-layer enforcement，不夠收斂）
- **Origin**: spec §Open risks OR-2/4/5；reviewer R-4
- **D5 invariant**: user-scope MCP 必須掛 bound project（`project.cloud_service_id IS NOT NULL`）。目前 enforcement 分散三層：
  - **Endpoint manual query**（rebind / unbind 走 controller 手動 query 擋）— `paas.py` `_update_support_project` D5 leg（OR-4）
  - **ORM 沒 `@api.constrains`**（`project.project.cloud_service_id` 沒 model-layer enforcement，靠 controller / 前端 gate）— R-4
  - **Rebind race condition**（同時 rebind + create user MCP 可能繞過 D5）— OR-2
  - **Sidecar auto-create + URL 機制**（D5 violation 後的 sidecar lifecycle 沒設計完）— OR-5
- **Status**: ✓ 已 surface 在 gitea #49（合併 OR-2 / OR-4 / OR-5 / R-4 一次重設計）
- **建議**：#49 同 PR 內考慮加 `@api.constrains('cloud_service_id')` model-layer enforcement，把 D5 收斂到 ORM 層，endpoint manual query 變 redundant 可刪
- **何時**：#49 排程

## F23 — Unbound project ACL — 缺防 enumeration / spec gap（OR-7 / R-4 partial）

- **Severity**: medium（pre-existing IDOR 性質，與 F4 同源 — F4 是 ai_assistant.py controller，本條是 paas.py support-projects endpoint）
- **Origin**: spec §Open risks OR-7（spec deferred 不測 unbound）；CAQA §C-3b；reviewer
- **狀況**：endpoint `/api/support-projects/<id>` 對 unbound project（`cloud_service_id IS NULL`）路徑走 OR-7 carve-out 給 `_SyntheticAccess(ROLE_OWNER)`，**沒有**統一防 enum 訊息層（與 bound non-member 路徑不對稱）。任意 internal user brute-force id 都拿 ROLE_OWNER → 可讀整 payload + update name/description
- **OR-7 三選項**：
  - A：creator-only carve-out（`create_uid == user.id OR user.partner_id in message_partner_ids`）
  - B：unbound 一律走防 enum 訊息（與 bound non-member 一致）
  - C：unbound project 廢除（要求 user 必先 bind cloud service）
- **Status**: ✓ 已 surface 在 gitea #51（OR-7 ACL spec gap）+ partial 重疊到 #49（D5 重設計時一併）
- **何時**：#51 排程，與 F4 同一 PR 處理較自然

## F24 — Reporter phase 已落的 doc work（OR-3 / OR-8 / OR-9）

- **Severity**: trivial（doc-only）
- **Origin**: reviewer follow-up alignment 標 reporter phase
- **Status**: ✅ 本批 reporter phase 已落
- **明細**：
  - **OR-3** — issue #48 comment 澄清 D6 status carve-out（碰到 issue 字面誤會「Status 欄位」一事）：已在 reporter phase 之前 post on gitea #48 comment
  - **OR-8** — worktree env setup gaps：sub-(a) `hooks.py` `pycryptodomex` import 已在本 cycle implementation 內補；sub-(b) `PAAS_CI_ADDONS_PATH` / `requirements.txt` pre-install 等 dev env gap 屬 worktree-sync skill / dev script 範疇，本 cycle 不展開
  - **OR-9** — declaration data ↔ migration 1:1 對稱 hardening：本 cycle B-1 fresh-install 重蹈 task pair pattern 的根因；已補進 [[cross-workspace-task-data-leak]] §Hardening `declaration-data ↔ migration 1:1 對稱`
- **何時**：✅ 完成

## F25 — Frontend UX hardening + error handling cluster（R-3 / R-6 / E2E-G1）

- **Severity**: important+（UX silent failure / data-loading gap — issue #48 是 UI page，UI 功能 regression / silent failure 是主功能 regression）
- **Origin**: reviewer R-3 + R-6（silent-failure-hunter critical NEW + important）+ reviewer AC #28 E2E 2026-05-20 step 11 FAIL（E2E-G1）
- **R-3 細節**：`McpServersSection.js:136-161` 三個 method 都用 `if (result.success)` 沒 `else`。Backend 回 `{success: false}` 時 UI 不 surface error（最嚴重 deleteServer：row 仍在 UI 上但 user 以為已刪）。Real surface 場景：sync / test 在 sidecar 斷時 fall-through 至此
- **R-6 細節**：`_delete_project_mcp_server`（`paas.py:1653`）沒 try/except 包 unlink；`_update_project_mcp_server`（L1638）+ `_update_support_project`（L1504）沒 savepoint（vs `_create_project_mcp_server` 有 savepoint）
- **E2E-G1 細節**：`CloudServiceBindingSection.js` `loadAvailableServices` 對 unbound project（`workspace_id == null`）沒 fetch services → dropdown 只剩 "Unbound" 選項，無法在 Project Detail 頁完成 rebind from unbound state。Workaround：Create modal 可指定 `cloud_service`；Same-session unbind 後可走通（cached），reload 或從 list 頁進來踩到
- **Status**: Follow-up issue: #52 (已 post on gitea, 涵蓋 R-3 + R-6 + E2E-G1)
- **建議 fix direction (E2E-G1)**：A：`loadAvailableServices` 對 `workspace_id == null` fall-back fetch all-accessible-workspaces 同 Create modal 行為；B：UI 顯示 disabled dropdown + 提示文案
- **何時**：下一個 sprint 處理（leader 已 post issue #52）

## F26 — Polish cluster (R-5 / R-9 / R-10)

- **Severity**: minor（style / behavior polish，不擋 ship）
- **Origin**: reviewer R-5 / R-9 / R-10
- **明細**：
  - **R-5** — `project.project.workspace_id` 是 compute field 沒 `readonly=True`：silent write 不報錯但無效，misleading（type-design-analyzer）。1-line fix
  - **R-9** — `support_service.js:469-471 + 487-490` `updateProject` optimistic merge 在 `try` 內、`finally clear loading`：若 merge throw 會 stuck spinner（code-reviewer）。1-2 line fix
  - **R-10** — `ir_rules.xml:217-238` comment block 英文（CLAUDE.md 要求 zh-tw），其他相鄰 rule 都用 zh-tw（code-reviewer）。Style polish
- **建議落點**：新 polish issue「project-detail page polish — post-merge maintainability sweep」（reviewer report Soft caveat tracked 表已建議標題）
- **何時**：next paas.py / project.project / ir_rules.xml touch PR 順手做，或單獨 polish issue

## F27 — Test gaps G1 / G3 / G6（R-7）

- **Severity**: minor（implementation 都對；test 是「future regression 強守線」缺塊）
- **Origin**: reviewer R-7（pr-test-analyzer）
- **缺塊**：
  - **G1** — `update` action 的 whitelist scope / `project_id` 寫入是否被 reject 未測
  - **G3** — `sync` / `test` action 的完整 ACL matrix（owner / admin / member / non-member × success / fail）不完整
  - **G6** — XSS sanitize 在 description 寫入 + 讀取雙路徑未被 test 守
- **建議**：每條對應加 1-2 條 test
- **何時**：next test hardening PR；可與 cycle 1 F8 + cycle 2 F17 一併 batch

## F28 — Description rich-text editor 升級（long-term，R-8）

- **Severity**: long-term architectural
- **Origin**: reviewer R-8
- **狀況**：本 cycle `BasicInfoSection` 用 `document.execCommand("bold"/"italic"/...)` 實作最小 rich-text。API legacy，瀏覽器仍支援但 spec 已 deprecated。產品演進到需要 more 編輯能力（如 mention / embed / collaborative editing）時，需要切到 ProseMirror / TipTap / Lexical 之類正式 rich-text framework
- **與本 cycle 的關係**：本 cycle ship 的 execCommand 寫法**沒有錯**（minimum viable rich-text，符合 issue #48 scope），但記在 follow-up 為了告訴未來 maintainer「進一步功能需求要 framework 切換」
- **何時**：產品需求變寬時開 architectural issue 評估

---

---

# Cycle 4 — MCP refactor URL redesign + unbound project ACL (Issue #49 + #51, 2026-05-21)

> Cycle 4 完成於 2026-05-21，PR 預計 merge to develop。Reviewer round 2 APPROVED WITH CAVEATS — HC-1 / HC-2 / HC-3 三條 hard caveat 全 closed in PR；1 條 new MINOR (SC-6b)；20 條 soft caveat 分散處理。F22 + F23 **RESOLVED**（cycle 4 收尾，issue #49 + #51 closed）。本 cycle 新加 F-X1~F-X8 + SC-cluster batch。

## F-X1 — Cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap (CAQA M1)

- **Severity**: medium（UX caveat，非 security / data loss）
- **Origin**: CAQA `docs/tracking/caqa-report-2026-05-20-mcp-refactor.md` M1 + impl finding #4 + reviewer SC-3
- **Location**: `src/models/mcp_server.py:214-217`
- **狀況**：cron 仍 `search([('auto_created', '=', True), ('state', '=', 'draft')])`，行為差異：
  - **Legacy row**（PR #47 既有 `auto_created=True AND sidecar_backed=True`）→ cron 仍 retry sync ✓
  - **新 sidecar-backed row**（cycle 4 UI shortcut 創建，`auto_created=False AND sidecar_backed=True`）→ cron **不會** retry ✗
  - **External row**（user 自填 URL）→ cron 不掃（design intent — external 由 user 自管理）✓
- **風險**：sidecar K8s service / Cloudflare route 啟動可能 lag → user 點 button 後 first sync 失敗 → user 必須手動 retry（比 PR #47 auto-create + cron retry 雙保險差）
- **建議落點**：cron query 改 `sidecar_backed=True` 或廢除 cron（user UI 已有 sync button）
- **何時**：dedicated PR；F-X7 等此 PR 完成後 drop `auto_created` column 才安全

## F-X2 — `_check_project_access` workspace_id NULL edge case + paas.py drift cluster (CAQA M2 + SC-6b + F4 / F10)

- **Severity**: medium（pre-existing + cycle 4 silent 擴大 surface）
- **Origin**: CAQA M2 + reviewer SC-6 + reviewer round 2 NM-R1 (SC-6b) + cycle 1 F4 + cycle 1 F10
- **三個獨立但同 controller IDOR cleanup PR scope**：
  1. **`ai_assistant.py:_check_project_access:54-55`** — `if not project.cloud_service_id.workspace_id: return True` blanket allow（pre-existing，本 PR 不擴大 / 不收緊；實際 PaaS 平台幾乎不會發生 bound service 沒 workspace_id）
  2. **`paas.py:_resolve_project_access:1510-1515`** — `if not workspace:` 同 treatment 涵蓋 2 case：(i) unbound project（follower check ✓，HC-2 hotfix）；(ii) bound-but-no-workspace（**同樣 follower check**，silent 收緊比 ai_assistant.py 嚴）→ `_SyntheticAccess` docstring「lock-step with ai_assistant.py」承諾 drift（reviewer round 2 NM-R1）
  3. **`_check_task_access`** (F4 task path) + **`_list_projects`** (F10 list path) — cycle 1 留 follow-up，cycle 4 plan AC #43 明文 out of scope
- **解法建議**（reporter 階段 surface 三選一）：
  - (a) paas.py 拆 case：`if not project.cloud_service_id:` 走 follower check，`if not project.cloud_service_id.workspace_id:` 與 ai_assistant.py 對齊 blanket allow（最簡）
  - (b) ai_assistant.py 同步收緊 bound-no-workspace（最嚴，security 最強）
  - (c) docstring 補一句 caveat 明示「lock-step only for unbound; bound-no-workspace edge case left blanket-allow in ai_assistant.py per CAQA M2 follow-up」（最弱、doc-only）
- **建議落點**：cluster PR 統一處理 `ai_assistant.py` controller 整體 IDOR — F4 + F10 + F-X2 三者**同一 controller / 同一 attack surface 概念**，分批改 surface 容易再 drift
- **何時**：next security audit / IDOR cleanup PR

## F-X3 — Codebase-wide comment ref noise cleanup (CAQA L1 + reviewer SC-19)

- **Severity**: low（maintenance debt，非本 PR 引入）
- **Origin**: CAQA L1 + impl /simplify pass surface + reviewer SC-19 confirm
- **狀況**：4 處 production code 殘留過去 PR（PR #47 / PR #53）的 plan/OQ/CAQA refs：
  - `ir_rules.xml:251`「CAQA B-1 (re-loop 2026-05-20)」— PR #53 留下
  - `support_service.js:454`「Plan AC #17 / #21」— PR #53 留下
  - `McpServersTab.js:12`「Plan AC #22 / #24」— PR #47 / #53 留下
  - `html_sanitize.js:26`「CAQA C-3, re-loop 2026-05-20」— PR #53 留下
- **狀況補充**：本 PR 已 verify implementation /simplify pass 對 cycle 4 自己加的 plan/OQ refs 已清；tests/ 內可能更多但 reviewer SC-19 不要求清測試
- **建議落點**：codebase-wide grep cleanup — batch with cycle 2 F19 「code clarity」cluster
- **何時**：next polish PR / 順手清

## F-X4 — URL helper duplication 收乾 (CAQA L2 + plan OR-X8 + reviewer SC-13)

- **Severity**: low（plan 明文 defer）
- **Origin**: plan OR-X8 + CAQA L2 + reviewer SC-13（code-reviewer grep verify `_model_build_mcp_endpoint_url` no caller in `src/`）
- **狀況**：兩 method algorithm 完全重複（cluster.local / subdomain / localhost fallback）：
  - `cloud_service.py:_model_build_mcp_endpoint_url` (model side) — PR #47 留下；給 model context 用（cron / non-request caller）— **code-reviewer grep 顯示無 caller**，可能 dead code
  - `paas.py:_compute_sidecar_url_hint` (controller side) — cycle 4 改名（從 `_build_mcp_endpoint_url`）；給 controller context 用（sidecar-hint endpoint）
- **建議落點**（兩種路徑）：
  - (a) verify `_model_build_mcp_endpoint_url` 真的 dead code → 刪；controller side 改抽 module-level helper 共用
  - (b) 保留兩個 method 但抽 algorithm 到共用 helper（`_compute_mcp_url(release, namespace, port, path, subdomain, paas_domain)` module function），兩 method 都呼叫
- **何時**：next paas.py / cloud_service.py touch PR；可獨立做也可 batch with F-X3

## F-X5 — SSRF error message IPv4 vs IPv6 format 微不一致 (CAQA L3)

- **Severity**: low（不影響 security）
- **Origin**: CAQA L3 + reviewer SC-20
- **Location**: `src/controllers/paas.py:_validate_mcp_url:60, 64`
- **狀況**：IPv6 用 `[{host}]` 格式（line 60, 64），IPv4 用 raw host — 一致性微差
- **建議**：統一格式（如統一用 `repr(host)` 或統一加 brackets）
- **何時**：next paas.py touch PR / 順手清

## F-X6 — `test_mcp_d5_removal.py` 註解 stale (CAQA L4 + reviewer SC-10)

- **Severity**: trivial
- **Origin**: CAQA L4 + comment-analyzer M-R5 SC-10 confirm
- **Location**: `src/tests/test_mcp_d5_removal.py:194-200`
- **狀況**：`test_d4_user_scope_requires_project_id_after_refactor` 註解寫「Note: today the D5 constraint method is also live」但 post-impl D5 已拔 — 註解 slight stale；同檔 `test_project_mcp_project_centric.py:21-28` stale module docstring 引用已刪 `TestProjectMcpD5OrmConstraint`（reviewer SC-12）也屬於此 cluster
- **建議落點**：reporter 順手清 or batch with F-X3 codebase-wide cleanup
- **何時**：reporter phase / 順手 / batch

## F-X7 — OQ-2 (b) keep deprecated `auto_created` 後續 column drop (deferred)

- **Severity**: medium（schema cleanup debt）
- **Origin**: cycle 4 implementation phase + OQ-2 leader 拍板 (b) keep deprecated
- **狀況**：cycle 4 implementation phase grep `auto_created` 顯示 10+ regression test 引用 → leader 拍板 (b) keep deprecated（rename 會炸）；migration `18.0.1.0.25` 只 UPDATE backfill 不 DROP COLUMN；field 標 `string='Auto Created (deprecated)'` + help `'Deprecated; use sidecar_backed for new code paths'`
- **依賴**：F-X1（cron query 改 `sidecar_backed`）必須先完成
- **後續動作**：
  1. F-X1 cron PR merge 後 → grep cleanup：把 test fixture / 任何 production code 對 `auto_created` 的 reference 改 `sidecar_backed` 或刪
  2. 寫 migration `18.0.1.0.<future>/post-migrate.py`：`ALTER TABLE woow_paas_platform_mcp_server DROP COLUMN IF EXISTS auto_created`
  3. `mcp_server.py` field 定義刪除
- **何時**：F-X1 完成後的 next cleanup PR

## F-X8 — `scripts/test-addon.sh` worktree env setup gap (tester surface)

- **Severity**: low（dev env，非 production）
- **Origin**: cycle 4 tester phase 試 run 時 surfaces；不在 plan AC 範圍
- **狀況**：worktree 環境跑 `./scripts/test-addon.sh` 時對某些 setup case 有 gap（如 PAAS_CI_ADDONS_PATH / requirements.txt pre-install / 共用 PostgreSQL 切換 — 與 cycle 3 F24 sub-(b) 同 cluster）
- **建議落點**：dev script PR / batch with cycle 3 F24 sub-(b)（worktree-sync skill / dev script 範疇）
- **何時**：next dev tooling PR / 順手做

---

## Cycle 4 SC-cluster batch（reviewer round 1 20 條 soft caveat 收尾分布）

| SC | 內容 | 落點 |
|----|------|------|
| SC-1 | `docs/decisions/patterns/mcp-acl-6-rule-split.md` 5 段 update 為 dual-leg variant | **reporter phase inline** ✅（已更新） |
| SC-2 | `docs/decisions/refactor/mcp-project-decouple.md` Invariants 段：Inv #3 D5 deleted / Inv #4 cleanup filter sidecar_backed / Inv #5 dual-leg / Inv #7 URL helper rename / 新 Inv #9/#10/#11 | **reporter phase inline** ✅（已更新，Inv #9/#10/#11 canonical home 在 [[mcp-url-redesign]]） |
| SC-3 | CAQA M1 cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap | F-X1 ↗ |
| SC-4 | CAQA M2 `_check_project_access` bound-but-no-workspace blanket allow | F-X2 ↗ |
| SC-5 | `_list_projects` F10 sudo + empty domain leak | F10 / F-X2 cluster ↗ |
| SC-6 | `_check_task_access` propagation 與 plan AC #43 wording 不同步（silent 收緊 — security 角度正面） | F-X2 cluster ↗ |
| SC-6b | paas.py `_resolve_project_access` 對 bound-no-workspace edge case 與 ai_assistant.py drift | F-X2 cluster ↗ |
| SC-7 | `ir_rules.xml:145-155` workspace_user comment 改寫（leg 1 「sidecar-backed flow」標籤錯）| reporter phase / batch with F-X3 |
| SC-8 | `_check_project_access` docstring 補 task path mirror + bound-no-workspace edge case | reporter phase / batch with F-X3 |
| SC-9 | `auto_created` help text 補 actionable hint（指向 drop column TODO） | reporter phase / batch with F-X3 |
| SC-10 | `test_mcp_d5_removal.py:194-200` 移除「D5 active」過時假設 | F-X6 ↗ |
| SC-11 | `migrations/18.0.1.0.25/post-migrate.py:44-46` warning branch comment 改事實陳述 | reporter phase / batch with F-X3 |
| SC-12 | `test_project_mcp_project_centric.py:21-28` stale module docstring 拔 `TestProjectMcpD5OrmConstraint` 引用 | F-X6 ↗ |
| SC-13 | `cloud_service.py:_model_build_mcp_endpoint_url` dead code（code-reviewer grep no caller） | F-X4 ↗（URL helper duplication 一併處理） |
| SC-14 | `write_actions` 變數名含 `sidecar-hint` (read-only) 誤導 | future PR rename `gated_actions` 或拆兩 set |
| SC-15 | H.4 cross-workspace assertion 比 H.3 弱（缺 `browse().read()` AccessError leg） | follow-up test 補強 / batch with F27 |
| SC-16 | `test_url_rejects_unsupported_scheme` 沒鎖 error wording | follow-up test 補強 / batch with F27 |
| SC-17 | F group `_assert_rejected` IPv6 4-char host substring matcher loose | follow-up test 補強 / batch with F27 |
| SC-18 | SSRF empty host / port 0 / userinfo edge case 未測（by-design defer #52） | F25 / #52 follow-up |
| SC-19 | Codebase-wide comment ref noise debt（CAQA L1 confirm） | F-X3 ↗ |
| SC-20 | CAQA L2 URL helper duplication + L3 SSRF error format IPv4 vs IPv6 | F-X4 / F-X5 ↗ |

20 條 SC 全分配。SC-1 + SC-2 已 reporter phase inline 處理；SC-3~SC-6/SC-6b/SC-10/SC-12/SC-13/SC-19/SC-20 聚到 F-X1~F-X6；SC-14~SC-18 等 future PR / batch with cycle 1-3 既有 F8/F27 test gap cluster。

---

## F-PA-1 — `agent_session._get_namespace_secret` fallback `_DEFAULT_SECRET` hardcoded

- **Severity**: medium（security caveat；B-1 修了後攻擊面 severity 降，但 install 預設仍應 reject）
- **Origin**: CAQA round 1 M-3 — `src/models/agent_session.py:7` 寫死 `_DEFAULT_SECRET = b'woow-paas-agent-namespace-fallback-secret'`
- **影響**：公開 git repo → attacker 取得 default secret → 預計算 hash for any project_id；B-1 修了後 attacker 無法直接 ORM create agent_session（rule 擋），但可預測 victim project 的 K8s namespace 名稱（reconnaissance value）
- **建議落點**：
  - install hook 加 hard guard：`ir.config_parameter` 取 `woow_paas_platform.agent_namespace_secret`，若空 / 短於 32 bytes → raise `ValidationError`
  - 或改 default 為 `secrets.token_bytes(32).hex()` 隨機生成（per-install 不同），首次 install 寫入 config_parameter
- **何時**：dedicated PR 或 Phase B（paas-agent-runtime 建置時一併補）

---

## F-PA-2 — `cloud_service._sidecar_url_prefix` None fallback → broad-filter 誤殺風險

- **Severity**: medium（M:N 下 fixture / pre-deploy state 可能踩；production 影響低 — service deploy 後才有 sidecar-backed MCP）
- **Origin**: CAQA round 1 M-4 — `src/models/cloud_service.py:1175-1194` `_sidecar_url_prefix()` 對 service 沒 `helm_release_name` / `helm_namespace` 時 return `None`；`_collect_sidecar_user_mcps` 收到 None → 走 broad filter（清所有 candidate）
- **觸發路徑**：
  - project X 綁 service A（無 helm metadata，可能 pre-deploy / fixture）+ service B（有 helm metadata，已 deploy）
  - service A unlink → `_sidecar_url_prefix()` return None → broad filter → 清 X 上所有 sidecar-backed MCP，包含 service B 的 sidecar MCP
- **建議落點**：
  - fallback 改用 `cloud_service.id` 作為 namespace marker（不存在於其他 service URL）— 例如 `f'__service_{self.id}__'` 永不 match `s.url.startswith(...)`，等效於不清
  - 或改為 raise / warn 強制 helm metadata 一致性（service 必有 helm name / namespace）
- **何時**：cleanup safety net hardening PR / Phase B（paas-agent-runtime 建置時可同步檢視 fixture path）

---

## F-PA-3 — `project.cloud_service_id` stored compute 失去明示 `ondelete='set null'`

- **Severity**: medium（correctness uncertainty；不一定是 bug 但 best practice 應該保留明示）
- **Origin**: CAQA round 1 M-5 — `src/models/project_project.py:18-27` 改 `cloud_service_id` 從 plain Many2one with `ondelete='set null'` 到 stored compute + inverse，**失去明示 ondelete**
- **影響**：
  - Odoo 18 對 stored compute Many2one with `store=True` 的 FK constraint default ondelete 依賴 framework default fallback（未驗證 Odoo 18 對 stored compute 的具體行為）
  - 若 Odoo 18 默認 `set null` → 行為等價；若 `restrict` → cloud_service unlink 卡死
  - M:N relation table `project_cloud_service_rel` 已是 `ON DELETE CASCADE`（pre-migrate 明文），所以 unlink 物理上 OK；但 stored compute column 是否同步 recompute 是 Odoo internal 行為，需 verify
- **建議落點**：
  - verify Odoo 18 stored compute M2O FK constraint default ondelete（grep `Many2one.ondelete` 在 Odoo 18 `fields.py`）
  - 明示 `ondelete='set null'` 對齊舊版本契約：`fields.Many2one(..., compute='...', inverse='...', store=True, ondelete='set null')`
- **何時**：small clarity PR / 順手做

---

## F-PA-4 — Phase A 5 view files defer to Phase B/C

- **Severity**: medium（admin operational gap，本身非 security / data 風險）
- **Origin**: OQ-A-3 leader 拍板 defer — plan §Phase A task #14-18 列 5 個 view file，implementation 全沒做：
  - `src/views/agent_template_views.xml`
  - `src/views/agent_session_views.xml`
  - `src/views/skill_views.xml`
  - `src/views/project_access_views.xml`
  - `src/views/project_external_mcp_setting.xml`（含 SSRF risk 警示）
- **影響**：
  - Admin 後台無法直接管理 4 個新 model（必須走 ORM / DB）
  - `external_mcp_allowed` 無 admin UI 開關（必須 ORM `with_user(admin).project.write({'external_mcp_allowed': True})`）
  - `project_external_mcp_setting.xml` 含 SSRF risk 警示 — Phase A enable external_mcp_allowed 時 admin 看不到風險提示
- **建議落點**：
  - Phase B/C admin UI batch PR — 配套 16 ir.rule（已落地）+ 4 new model 後台 form / tree / search view
  - `project_external_mcp_setting.xml` 必含 zh-tw SSRF risk 警示文字（design 預期）
- **何時**：Phase B 或 Phase C admin UI batch

---

## F-PA-5 — `skill_rule_project_user` 「only own draft」design intent 簡化

- **Severity**: low（implementation 比 design 嚴；不是 leak surface）
- **Origin**: CAQA round 2 C-A-1 — design §3.2 line 1528 `skill_rule_project_user`「R=True, W/C=True (only own draft), U=False」；implementation 簡化為 R only，因 ir.rule 無法表達「own draft」需 Python state guard
- **影響**：
  - Phase A user role 無法在自己 draft skill 上 W/C（必須升 admin role）
  - 對 Phase A scope（schema-only，skill upload API 留 Phase B/C）影響 marginal
- **建議落點**：
  - Phase B/C skill upload API 同 PR 補 Python state guard：
    - `@api.constrains('state', 'uploaded_by_uid')` 或 `write()` override 對 user role：只允 `state='draft' AND uploaded_by_uid=self.env.uid`
    - rule `skill_rule_project_user` perm_write=True + perm_create=True；Python gate 守 row 內容
- **何時**：Phase B/C skill upload API PR 同 commit

---

## F-PA-6 — `ASSIGNABLE_ROLES_BY_ROLE[ROLE_OWNER]` 包含 ROLE_OWNER vs design 出入

- **Severity**: low（business intent 出入；implementation 寬鬆但合理）
- **Origin**: CAQA round 2 C-A-2 — `src/models/project_access.py:20-25` `ASSIGNABLE_ROLES_BY_ROLE[ROLE_OWNER] = [ROLE_OWNER, ROLE_ADMIN, ROLE_USER, ROLE_GUEST]`；design §4.3 line 2090 `ASSIGNABLE_ROLES_BY_OWNER = ['admin', 'user', 'guest']` 不含 owner
- **影響**：
  - implementation 允許 owner grant 另一 owner（multiple owner 是 design 允許的 case，line 2070 `other_owners` 概念）
  - design 漏寫或 implementation 過寬，需 leader / planner 拍板
- **建議落點**：
  - (a) update design §4.3 ASSIGNABLE_ROLES_BY_OWNER 加 owner（business intent confirm）
  - (b) update implementation 把 owner 拿掉，加 ownership transfer 專用 method（business intent confirm 「owner 必須 transfer 而非 grant」）
- **何時**：leader / planner 拍板後 doc-only 或 impl PR

---

## F-PA-7 — `skill.bundle_attachment_id` `groups='base.group_system'` 同擋 project admin

- **Severity**: low（design intent acceptable but maintainer documentation 不足）
- **Origin**: CAQA round 2 C-A-3 — `src/models/skill.py:42-47` `bundle_attachment_id = fields.Many2one('ir.attachment', groups='base.group_system')` SK-6 path (a)；field-level mask 同時擋 project owner/admin 透過 backend form 直接 attach
- **影響**：
  - Project owner/admin 必須走 ORM sudo / API endpoint upload bundle，不能用 Odoo backend form
  - design intent 是 protect bundle binary from non-system；implementation 選最嚴（合理）但 maintainer 看 form view 時會困惑
- **建議落點**：
  - Phase B/C skill upload API endpoint 補（必走 sudo create attachment + ORM create skill 兩步）
  - maintainer doc：`docs/decisions/patterns/skill-bundle-attachment-field-level-mask.md` 或 inline comment 補：「project admin 不能透過 backend form attach bundle，必須走 API」
- **何時**：Phase B/C skill upload API PR 同 commit + reporter 補 maintainer doc

---

## F-PA-8 — `discuss_channel.py:262` stale comment

- **Severity**: trivial (comment-only)
- **Origin**: CAQA round 1 I-1 — `src/models/discuss_channel.py:262` comment 寫「task's primary service (project's cloud_service_id) first」；implementation 已走 `task.primary_cloud_service_id` (hybrid spec)
- **建議落點**：reporter 順手清 — comment 改寫對齊 hybrid spec：「task's primary service (computed default + drift handler, see §7.0.3)」
- **何時**：reporter phase 或 batch

---

## F-PA-9 — `cloud_service._do_cleanup` 缺 ensure_one()

- **Severity**: low (defensive style)
- **Origin**: CAQA round 1 I-3 — `src/models/cloud_service.py:1086-1101` `_do_cleanup` 內走 helper `_sidecar_url_prefix()` / `_collect_sidecar_user_mcps()` 都 ensure_one；但 `_do_cleanup` 本身沒明示 single-record semantics
- **影響**：multi-record `_do_cleanup` 呼叫會在 helper 內 raise，行為意圖不清楚；後續 maintainer 看 method body 不知 single-record assumption
- **建議落點**：method 第一行加 `self.ensure_one()` 明示 — 與 helper 一致
- **何時**：small clarity PR / 順手做

---

## F-PA-10 — `primary_cloud_service_id` docstring 描述不精確

- **Severity**: trivial (docstring clarity)
- **Origin**: CAQA round 1 I-4 — `src/models/project_task.py:38-39, 74-89` `primary_cloud_service_id` docstring 寫「The compute acts as a default」描述不精確（Odoo readonly=False + computed 是 implicit default idiom）
- **建議落點**：docstring 改寫對齊 Odoo idiom：「Stored Many2one with computed default — `readonly=False` makes the compute act as initial value; explicit writes persist via the `inverse` lift, drift resets only when current pick is no longer in the project's bound set.」
- **何時**：reporter 順手清

---

## F-PA-11 — `project_project.create()` SUPERUSER_ID skip → OdooBot owner risk

- **Severity**: low (功能對齊 design，但 OdooBot owner 概念上奇怪)
- **Origin**: CAQA round 1 I-5 — `src/models/project_project.py:108-110` 對 `SUPERUSER_ID` skip bootstrap；migration step 9 補做但 fallback 到 `pp.create_uid`（可能是 OdooBot）
- **影響**：demo / install hook 創建的 project owner = OdooBot partner；design §4.3「real human owner」明文 — OdooBot 不算
- **建議落點**：
  - 評估是否 demo data project 預設不需 owner row（design intent 確認）
  - migration step 9 加 OdooBot detection：若 fallback uid = SUPERUSER_ID，skip + log warning「project_id=X demo / install hook created；no human owner — admin 手動指定」
- **何時**：Phase B/C 或 owner 政策 PR

---

## F-PA-12 — `project_access._compute_user_id` 無 deterministic ordering

- **Severity**: low (single partner 對應多 user 邏輯上不該發生，但 limit=1 沒明示 ordering)
- **Origin**: CAQA round 1 I-6 — `src/models/project_access.py:81-87` `_compute_user_id` 走 `sudo().search([('partner_id', '=', X)], limit=1)` 無 deterministic ordering
- **影響**：若同 partner_id 對應多 user 行（不該發生但 Odoo 不擋），limit=1 結果不確定
- **建議落點**：加 `order='id ASC'` 保 deterministic：`sudo().search([...], limit=1, order='id ASC')`
- **何時**：small clarity PR / 順手做

---

## F-PA-13 — `project_project._inverse_cloud_service_id` replace semantics silent drop M:N bindings

- **Severity**: medium（silent contract change + latent regression at Phase B/C multi-bind activation；CAQA 漏抓，reviewer round 1 抓出標 HC-1）
- **Origin**: reviewer round 1 HC-1（silent-failure-hunter C-2）— `src/models/project_project.py:88-99` `_inverse_cloud_service_id` 把 `cloud_service_id = X` write 透過 `[(6, 0, [X.id])]` replace 整個 `cloud_service_ids` M:N 集
- **狀況**：
  - Phase A migration 後既有 1-service M:N row 沒實際 break（每 row 1 service，replace = noop）
  - **Phase B/C user 透過 UI add 第二 service 後**，任何 controller `cloud_service_id = X` write 會 silently drop 其他 binding，無 log / 無 warning / 無 error
  - 下游影響：`workspace_ids` 收縮 / `task.primary_cloud_service_id` drift / `agent_template.project_ids` 評估改變 / ir.rule visibility 退化 / cleanup lifecycle 連帶錯亂
- **Caller surface（grep verified）**：
  - `src/controllers/paas.py:1594` `vals['cloud_service_id'] = target.id` in `_update_support_project`（rebind path）
  - `src/controllers/ai_assistant.py:944` `vals['cloud_service_id'] = int(cs_id)` in `_create_project`（create path）
- **本 PR 製造？**：是（M2O → stored compute + inverse 是本 PR 引入）；controller caller 是 pre-existing legacy write
- **leader 拍板**：(a) approve with hard caveat — Phase B/C multi-bind activation 前必修
- **建議落點**：
  - 路徑 1（推薦）：`_inverse_cloud_service_id` 加 `_logger.warning` 當既有 set 非空 replace；同時更新 `cloud_service_id` field `help=` 文字明示 "deprecated alias — write replaces full m:n set, prefer cloud_service_ids"
  - 路徑 2：改 additive `(4, X.id)` 語意 — 但變動 controller layer 既有 1-service rebind 行為（不是純 additive，會破 unbind 與 rebind 邏輯），需配套 controller layer 改 m:n 語意
- **何時**：**Phase B/C multi-bind activation 前必修**

---

## F-PA-14 — `project.sudo().create()` bypass `project_access` bootstrap → creator 看不到自己 project 上 4 model

- **Severity**: medium（latent functional regression — 不是 security IDOR，是 own-project visibility gap；CAQA 漏抓，reviewer round 1 抓出標 HC-2）
- **Origin**: reviewer round 1 HC-2（silent-failure-hunter C-1）— `src/models/project_project.py:122` create() override `if self.env.uid == SUPERUSER_ID: return projects` skip bootstrap；對 `ai_assistant.py:946` 唯一 production sudo create path（grep verified）→ bootstrap branch skip → 新 project 無 `project_access` owner row
- **狀況**：
  - Creator 仍可看到 project itself（透過 `project_rule_workspace_member` leg 2 carve-out `create_uid = user.id`）
  - 但對 4 個新 model（`agent_template` / `agent_session` / `skill` / `project_access`）的 16 條 ir.rule 全 walk `project_id.project_access_ids` → no row exists → creator 完全看不到自己 project 上的這些 records
  - **Phase A 沒人 active 4 model**（dormant）→ 無 visible impact today
  - **Phase B-D `use_agent_runtime` active 後 immediate functional regression** — creator 不能管理自己 project 的 templates / skills / sessions
- **與 F-PA-11（OdooBot owner edge case）區分**：F-PA-11 是 demo / install hook fallback；本條 F-PA-14 是 production controller 透過 `ai_assistant.py:946` 觸發的 silent visibility gap — 嚴重得多
- **本 PR 製造？**：是（SUPERUSER skip + 4 model ir.rule 是本 PR）；ai_assistant.py sudo create 是 pre-existing
- **leader 拍板**：(a) approve with hard caveat — Phase D-F `use_agent_runtime` activation 前必修
- **建議落點**：
  - 路徑 1（推薦）：`project_project.create()` 對「`is_superuser()` 但 `env.user.partner_id` 是 real human」case 仍走 bootstrap；透過 context flag（e.g. `_skip_project_access_bootstrap=True`）讓 cron/install/migration 顯式 opt-out
  - 路徑 2：controller fix — `ai_assistant.py:946` 改 `request.env['project.project'].with_user(env.user).create(vals)` 不走 sudo（需 verify create 對 std `project.project` 沒 ACL 阻擋 base.group_user — std `ir.model.access.csv access_project_project_internal` 已是 1,1,1,1 應該允許）
- **何時**：**Phase D-F use_agent_runtime active 前必修**

---

## F-PA-15 — controller sudo path 對 grantor authority constraint bypass — Phase B-E lock-step 提醒

- **Severity**: info（reviewer round 2 EC-3a — round 1 HC-3 hotfix 落地後 reviewer 獨立 walk surface 的 edge case；Phase A 沒 controller 走此 path，dormant）
- **Origin**: reviewer round 2 §12.4 EC-3a — HC-3 hotfix `_check_project_ids_grantor_authority` 對 `is_superuser()` 與 `has_group('base.group_system')` 兩條 bypass；Phase B/C 加 controller endpoint 用 `agent_template.sudo().write({'project_ids': ...})` 時，constraint 不 enforce — 必須在 controller layer 對齊 [[two-layer-acl-lockstep]] pattern 補 grantor check
- **狀況**：
  - Phase A grep verify `agent_template.*sudo\(\)\.write` in `src/controllers/` → 0 hit（dormant）
  - Phase B-E 預期加：admin UI form save / webhook endpoint / cron rebind — 任一 sudo path 都要 mirror Python grantor check
- **建議落點**：
  - Phase B-C controller layer 加 `_check_template_grantor_authority(template, project_ids)` helper（與 `agent_template._check_project_ids_grantor_authority` 雷同），controller sudo path 前 call
  - 對齊 [[two-layer-acl-lockstep]] Layer 1 (ir.rule + Python `@api.constrains`) + Layer 2 (controller helper) 三層 lock-step
- **何時**：Phase B/C 加 agent_template 相關 controller endpoint touch PR

---

## F-PA-16 — `skill._anchor_bundle_attachment` destructive overwrite — maintainer doc 補強

- **Severity**: info（reviewer round 2 EC-4a — round 1 HC-4 hotfix 落地後 reviewer 獨立 walk surface 的 edge case；docstring 已警告但 maintainer doc 補強需求）
- **Origin**: reviewer round 2 §12.4 EC-4a — `skill._anchor_bundle_attachment` 若 admin (`base.group_system`) 把已存在的 `mail.message` attachment（`res_model='mail.message'`）assign 給 `skill.bundle_attachment_id`，anchor 把 attachment 的 `res_model` overwrite 成 `'woow_paas_platform.skill'` → 切斷與原 mail.message 的 `res_model` 反向查路徑
- **狀況**：
  - 對 admin 路徑是預期破壞性 anchoring（admin 是 curating attachment ownership）
  - 但 admin 在 backend form attach 既存 attachment 時可能不知道 res_model 會被改寫 → silent doc gap
- **建議落點**：
  - `skill.py:108-129` `_anchor_bundle_attachment` docstring 已警告（NB destructive — 切斷 mail.message linkage）；建議補加 inline comment 在 attachment write 行明示
  - Phase B/C bundle upload API endpoint 補：upload pipeline 走「create new attachment」而非「re-assign existing」path，繞過 destructive overwrite scenario
  - reporter maintainer doc 補：`docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md` 已 list 此 anti-pattern；建議 admin backend form 加 view-level warning（Phase B/C view 補時順手）
- **何時**：reporter 順手清 / Phase B/C bundle upload API PR

---

## Phase B carry-over（cross-repo — `paas-agent-runtime`）

> Phase B（agent-runtime pod image source）的 code / test / CI / carry-over 全在 **`paas-agent-runtime` repo**。其 carry-over SC 的 **canonical home 是該 repo 的 `docs/PHASE_B_HANDOFF.md`**（不在本 woow_paas_platform follow-ups.md 重列每條，避免 cross-repo 雙寫漂移）。本段只記 **woow_paas_platform 側需要知道的連動點 + 指標**。

### PB-ref — Phase B carry-over canonical home

- **Canonical home**：`paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md`（§1 後續 phase 必補 / §2 Phase C wiring assumptions / §3 環境部署 note / §4 其餘 SC / §5 design 回標 planner）
- **Phase B epic durable**（woow_paas_platform 側）：`docs/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md`
- **carry-over SC 摘要**（細節見 PHASE_B_HANDOFF.md）：
  - **SC-4 / SC-5**（glue phase 必補）：mcp/template reloader 缺 tool_set 失效 hook / stub 雙 done（契約已由 planner 4e65966 裁定 ws_server 是 done 唯一 producer）
  - **SC-1 / SC-3 / SC-6 / SC-8 / SC-9** + audit_logger emit guard + base_reloader snapshot live dict：follow-up，不阻塞
- **狀態**：Phase B PR #1 merge 中；上述 SC 由 Phase C/D 接力，**追蹤責任在 paas-agent-runtime repo**（本段僅 epic 視角指標）

### PB-link-1 — F-PA-15 / F-PA-16 是 paas-agent-runtime `cache.py` 的跨 repo 前置依賴 ⚠

- **連動**：`paas-agent-runtime` repo `runtime/skills/cache.py` 假設 woow_paas_platform 側 skill bundle attachment 已 **anchor `res_model='woow_paas_platform.skill'` + `public=False`** + row-level ACL（Inv A2）落實；pod 守「絕不直連 `ir.attachment`」（grep 確認全 runtime 零直連）
- **woow_paas_platform 側對應 follow-up**：[[#F-PA-15]]（controller sudo path 對 `_check_project_ids_grantor_authority` lock-step）+ [[#F-PA-16]]（`_anchor_bundle_attachment` destructive overwrite doc）— 兩者落實狀態是 Phase C wiring 的前置條件
- **行動**：**Phase C wiring 啟動前，必須確認 woow_paas_platform 的 anchor stamping + row-level ACL（F-PA-15 / F-PA-16 + Phase A Inv A2 / HC-4 hotfix）已落 production**；否則 pod fetch bundle 時 attachment delegation target 不對 → 可能 fetch 失敗或繞過 row-level ACL
- **canonical**：`paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` §2「F-PA-16 連動（woow_paas_platform 側，跨 repo）」

### PB-link-2 — woow-paas-ci 待核對（push 前協調 ci agent）

- `paas-agent-runtime` repo `.gitea/workflows/build-and-trigger.yml` 假設 JCR push secret 名（`JCR_USERNAME` / `JCR_PASSWORD`）/ runner label / Gitea API host — 需與 woow-paas-ci agent 核對
- **canonical**：`paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` §3「woow-paas-ci 待核對」
- 與本 repo 既有 cross-repo CI 流程（CLAUDE.md §Release & Build paas-odoo Image）為**不同 pipeline**（paas-odoo image vs agent-runtime image），勿混

---

## Cross-cutting: 給未來 PR 作者的 hint

每個改動下列檔案的 PR，自查本 doc 對應條目：
- `src/security/ir_rules.xml` / `src/security/ir.model.access.csv` → F1 / F4 / F16 / F24 (declaration↔migration 對稱) / F26 (R-10 zh-tw comment) / F-X3 (cycle 4 comment ref noise — `ir_rules.xml:251` PR #53 ref) / cycle 5 16 new rule + project/task M:N walk + 2 ir.attachment rule（HC-4 hotfix `ir_attachment_rule_skill_bundle_*`）
- `src/migrations/<v>/post-migrate.py` → F5 (cycle 1 xmlid drift 防呆) / F13 (cycle 2 task ACL 對齊) / F-X7 (cycle 4 drop `auto_created` column after F-X1) / F-PA-11 (cycle 5 SUPERUSER bootstrap OdooBot owner detection)
- `src/models/discuss_channel.py` → F2 / F3 / F6 / F9 / F-PA-8 (cycle 5 stale comment line 262)
- `src/models/cloud_service.py` → F14 / F15 / F-X4 (URL helper duplication `_model_build_mcp_endpoint_url`) / F-PA-2 (cycle 5 `_sidecar_url_prefix` None fallback) / F-PA-9 (cycle 5 `_do_cleanup` ensure_one)
- `src/models/mcp_server.py` → F19 (type design / test coverage cluster) / F-X1 (cron `_cron_retry_mcp_sync` 改 sidecar_backed) / F-X7 (drop `auto_created` field)
- `src/models/project_project.py` → ~~F22 (D5 `@api.constrains`)~~ RESOLVED cycle 4 / F26 (R-5 `workspace_id` readonly) / F-PA-3 (cycle 5 cloud_service_id stored compute ondelete) / F-PA-11 (cycle 5 SUPERUSER bootstrap) / F-PA-13 (cycle 5 round 1 HC-1 inverse replace semantics) / F-PA-14 (cycle 5 round 1 HC-2 SUPERUSER skip → project_access bootstrap gap)
- `src/models/project_task.py` → F-PA-10 (cycle 5 primary_cloud_service_id docstring clarity)
- `src/models/project_access.py` → F-PA-6 (cycle 5 ASSIGNABLE_ROLES_BY_OWNER vs design) / F-PA-12 (cycle 5 `_compute_user_id` ordering)
- `src/models/skill.py` → F-PA-5 (cycle 5 `skill_rule_project_user` own draft Python guard) / F-PA-7 (cycle 5 `bundle_attachment_id` field-level mask doc) / F-PA-16 (cycle 5 round 2 `_anchor_bundle_attachment` destructive overwrite doc 補強)
- `src/models/agent_template.py` → F-PA-15 (cycle 5 round 2 EC-3a controller sudo path 對 `_check_project_ids_grantor_authority` bypass 提醒 — Phase B/C controller layer 補)
- `src/models/agent_session.py` → F-PA-1 (cycle 5 namespace_secret hardcode → install hook hard guard)
- `src/views/` (Phase A defer) → F-PA-4 (cycle 5 5 view files agent_template_views / agent_session_views / skill_views / project_access_views / project_external_mcp_setting incl. SSRF risk 警示)
- `src/controllers/ai_assistant.py` → F4 / F10 / F12 / F20 / F-X2 (bound-no-workspace edge case + drift cluster) / F-PA-14 (cycle 5 round 1 HC-2 `project.sudo().create()` bypass project_access bootstrap)
- `src/controllers/paas.py` → F19 / F20 / ~~F22 (D5 重設計)~~ RESOLVED cycle 4 / ~~F23 (unbound ACL)~~ RESOLVED cycle 4 / F25 (R-6 try/except + savepoint) / F-X2 (`_resolve_project_access` drift) / F-X4 (`_compute_sidecar_url_hint` duplication) / F-X5 (SSRF error format) / F-PA-13 (cycle 5 round 1 HC-1 `vals['cloud_service_id'] = X` write silent replace M:N)
- `src/tests/test_*.py` → F8 / F17 / F19 / F27 (G1 / G3 / G6 test gaps) / F-X6 (cycle 4 D5 stale comment) / SC-15~SC-17 (cycle 4 test gaps)
- `src/static/src/paas/layout/` → F7
- `src/static/src/paas/pages/project-detail/` → F25 (R-3 McpServersSection error display + E2E-G1 CloudServiceBindingSection rebind-from-unbound) / F28 (R-8 editor migration) / F-X3 (`McpServersTab.js:12` comment noise)
- `src/static/src/paas/services/support_service.js` → F26 (R-9 optimistic merge order) / F-X3 (`support_service.js:454` comment noise)
- `scripts/test-addon.sh` / dev tooling → F-X8 (worktree env gap)

## Phase D enablement gate — CAQA + reviewer Phase C 彙整（flag flip 前 must-fix）

**2026-05-24** · **狀態 OPEN** — 全部 flag-gated，**不阻 Phase C inert merge**（`use_agent_runtime=False` 不走 WS path）；**Phase D enable `use_agent_runtime` flag 前，must-fix 區必全 close**。來源：CAQA Phase C（A-1/C-3/C-5 plan-alignment，commit `3cab339` 初列 F-PC-1/2/3）+ reviewer Phase C（cross-check 8 caveat 全同意 + 新 N-1~N-5，§12 把 gate 精化為 **must-fix / follow-up 兩層**）。完整論述見 `docs/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md`。

**2026-05-24 更新 · Phase D code-ready 拍板（D-1/D-2/D-3，使用者 sign-off）**：本 gate 進 **code-ready 收口**（不 flip / 不部署 / 不做 2 週觀察 —— 那塊使用者自跑，見 plan §Pilot runbook）。**D-1** F-PC-4 = **global fail-closed（所有 router）**，接受 ~6 支 operator fixture 全改；**D-2** F-PC-3 **升 must-fix 等級納入**、走 K8s Secret env **`AGENT_RUNTIME_NAMESPACE_SECRET`**（殺 `_DEFAULT_SECRET`，缺即 fail-closed）→ **design §2.15 待 follow-up 改寫**（auto-gen→env，code 完成後另開，本次不改 design）；**D-3** F-PA-14 一併納入（最低優先，care HC-2 bootstrap 語義）。新增凍結 env：`AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS`（F-PC-2）+ `AGENT_RUNTIME_NAMESPACE_SECRET`（F-PC-3）。task 拆解 + 每條 red test 入口見 plan `docs/plans/2026-Q2/agent-runtime-pod-plan.md` Phase D（含 Tester hand-off checklist）。

**OQ 閉合狀態（review 後 leader 協調已 addressed）**：
- **OQ-R-1（plan-source 偏離 escalate planner）**：design §5.6（A-1 `extra_headers`）/ §5.5+§2772（C-3 webhook dual-secret）/ §2.15（C-5 namespace auto-gen）三處 design 文字為源、impl 偏離 → 已由 commit `3cab339` 補 ⚠ 標註 + 相容寫法（§5.6 `_hdr_kw`）→ **addressed**（design 同步完成；impl 端落實仍待 Phase D，見下）。
- **OQ-R-2（cross-repo C-1 WS 對稱半邊）**：Phase B pod WS verifier 對稱拒絕 `token_use != 'ws'` 已由 `paas-agent-runtime` repo PR #2 `ba6b417`（**B transitional** — 缺 claim 放行的過渡語意）merge → **addressed**（A 嚴格收緊見下方 F-PC-7；**[2026-05-26 更正] A 嚴格已 merge main PR #3 `bb31676` + build 進發佈 image，stg closed**）。

### must-fix（`use_agent_runtime` flip 前必全 close）

| # | caveat | 位置 | 為何 flip 前必修 | escalate |
|---|--------|------|----------------|----------|
| **F-PC-1** | A-1 websockets `extra_headers` | `ai_assistant.py:650` + `requirements.txt:10` | websockets ≥14 移除 `extra_headers` 改名 `additional_headers`；`>=12.0` 無上界 → flag-on + 裝 ≥14 即 **每條對話首次 connect `TypeError`** 失敗。**做**：釘 `websockets>=12.0,<14` 或 code 相容 both kwargs + 實機驗 WS connect | planner（design §5.6 為源，`3cab339` 已補相容寫法）|
| **F-PC-2** | C-3 webhook dual-secret | `agent_runtime_webhook.py:62` | 單 secret；rotation 過渡窗口（1h）reject `previous`-signed HMAC（cron 兜底但可用性 gap）。**做**：webhook verify 補 `current`/`previous` fallback（與 skill-bundle token verify 共用 pattern）| planner（design §5.5 為源，`3cab339` 已補 ⚠）|
| **F-PC-4** | C-2 operator auth fail-open | `extra/paas-operator/src/main.py:89-91` | `api_key` 未設時 middleware fail-open；Phase C 使 operator 成 **token 簽發者** → fail-open 時可為**任意 project_id** 簽 properly-scoped skill_bundle_token → **跨租戶 bundle 外洩原語**（C-1 對此無解，拿到的是目標 project 合法 token，故 C-2 才是跨 project 風險真正放大器）。**做**：`api_key` 未設 → fail closed / 503（**[2026-05-24 D-1 拍板] global，所有 router**）| — |
| **F-PC-5** | N-1 client `raise_for_status` | `agent_runtime_client.py:32-33` | `_request` 無 `raise_for_status` → operator error（401 bad-key / 500 helm）不 raise → `runtime_info` 變錯誤 dict → `pod_ws_url=None` → `websockets.connect(None)` 被 `:721` 廣捕 → browser 收 **cryptic WS error 而非真因**（error 通道失能，疊 N-2）。**做**：加 `raise_for_status` 或顯式 status 檢查，拋 typed error 帶 operator `detail` | — |
| **F-PC-6** | P-2 async-gen `aclose()` | `ai_assistant.py:678-729` | `finally` 只 `loop.close()` 未先 `loop.run_until_complete(agen.aclose())`；**每條路徑（含正常 done break）relay 都 abandon 在 `yield` → `async with websockets.connect` 的 `__aexit__` 不執行 → pod 側 WS 洩至 ping_timeout(≤60s)**（reviewer 比 CAQA 描述廣：非只 client 斷線）。**做**：drain loop 補 `aclose()` | — |
| **F-PC-7** ✅ **stg closed** | C-1 A 嚴格收緊（cross-repo）| `paas-agent-runtime` `runtime/transport/auth.py` | **[2026-05-26 更正]** A 嚴格（缺 `token_use` claim → reject）**已 merge main**（PR #3 `bb31676`，origin/main 已到 `3263352`/PR#4）+ **build 進所有發佈 image**（v1.0.0 / v1.0.1 echo 皆 **unconditional A 嚴格、無 env toggle**）+ **stg pilot 已驗運作**（pod 收到 user_message = WS accept 通過 `verify_token` = operator-stg 已簽 `token_use` empirical）→ stg 過渡放行已封死。原 2026-05-25「WS verifier 仍 B transitional / 待 operator prod 全簽」係 **stale clone 誤判**（B transitional 從沒 build 成 image）。**剩**：prod operator 全簽 `token_use` gate = 未來式（見 prod 上線 runbook） | cross-repo（stg 已 closed；prod gate 列 runbook）|

### follow-up（enable 後 / 較低優先）

> **[2026-05-25 Phase D 收口]** must-fix 已 **code-closed**：F-PC-1~6 + F-PA-14 在本 repo（`8d152f7`/`e2f1624`/`e94991d`，CAQA+reviewer 確認全正確關閉、HC-2 security-auditor 獨立 airtight）；**F-PC-7 A 嚴格（`paas-agent-runtime` `dabb275`）[2026-05-26 更正] 已 merge main（PR #3 `bb31676`，origin/main 已到 `3263352`/PR#4）+ build 進所有發佈 image（unconditional A 嚴格、無 toggle）+ stg pilot 已驗運作 → F-PC-7 stg closed**（2026-05-25「本 reporter 在 clone 未見 merge / 最後 main merge 為 PR #2 `ba6b417` B transitional」係 **stale clone 誤判**，B transitional 從沒 build 成 image）；**剩 prod operator 全簽 `token_use` gate = 未來式**（stg-pilot-only 階段無 prod agent pod，見 prod 上線 runbook）。剩餘 = **D-CAVEAT-1/2 部署 invariant**（非 code）。下列 C-4/C-6/P-1 **經 leader OQ-D-2 confirm 為刻意延後**（pilot-gate / accepted-risk），非靜默丟棄。

- **F-PC-3 (C-5) namespace secret auto-gen** — `agent_session.py:11,122-127` `_DEFAULT_SECRET` 硬編碼公開 fallback；design §2.15 規定的「migration 首次部署 auto-gen random」未實作 → fresh install silent 落公開 secret → `paas-ws-pa-{hash}` 可預測。**CAQA 初判 gate（F-PC-3）→ reviewer §6/§12 降 follow-up**：只洩 namespace 命名，K8s RBAC + 獨立簽章 secret（`agent-runtime-auth-secret`）兩層仍 gate 實際 access，故非即時洞。**仍建議 enable 前一併修**（design §2.15 規定）：fresh install auto-gen random 寫入 `ir.config_parameter woow_paas_platform.agent_namespace_secret`。escalate planner（design §2.15 為源，`3cab339` 已補 ⚠）。與 [[#F-PA-1]]（Phase A 同源 install hook hard guard）同一落點。 **[2026-05-24 D-2 拍板] 升 must-fix 納入 Phase D code-ready，機制改 env(b)**：不走 auto-gen / ir.config_parameter，改讀 K8s Secret env `AGENT_RUNTIME_NAMESPACE_SECRET`（缺即 fail-closed），與 skill-bundle/webhook env pattern 一致；**design §2.15 待改寫**（auto-gen→env）。詳見 plan Phase D WS1。
- **C-4** webhook replay/freshness（`agent_runtime_webhook.py:30-37,82`）— HMAC timing-safe（`hmac.compare_digest`）但無 nonce/timestamp 窗口；合法 POST 可 replay 翻 `agent_session.state`（cron 自癒）。Phase D webhook 改動只動 HMAC verify（single→dual），**未碰 state-write 邏輯**，故 C-4 carried-forward 非 Phase D 引入。**[2026-05-25 OQ-D-2 leader confirm 刻意延後]** 但 **enabler 欄位 `last_state_event_ts` 已就位**（Phase C 加，目前只 stamp 不擋 stale）→ 未來補「新事件 ts ≤ 既有則跳過 state 寫入」即可。方向：簽章涵蓋 `ts` 拒舊事件 / per project_hash 單調 `sequence_number`。
- **C-6** operator 盲信 `project_hash` 格式（`agent_runtimes.py:38` + `mcp_invalidator.py:18-35`）— `validate_namespace` 只檢 `paas-ws-` 前綴不檢 hash 尾（list-arg 排除 shell injection，疊 C-2 放大）。方向：邊界驗 `^[a-z2-7]{16}$`。
- **P-1** SSE worker-pin（`ai_assistant.py:635`）— 每 active conversation 佔 1 Odoo sync worker（design §6.8 認列 tradeoff，公式 `peak_concurrent_active_conv(50)+baseline(16)=66`）。方向：Phase D 依公式 size worker pool + 確保 pod envelope 必以 `done`/`fatal` 收尾（永遠串流的 pod 釘死 worker）。
- **N-2** dispatcher retry（`webhook_dispatcher.py:41-52`，**未接線** — grep 確認 operator 內無 production caller，只 def + test）— retry 只看 `status_code>=500`，忽略 httpx ConnectError/Timeout（最常見瞬斷零 retry）+ 耗盡後 silent give-up。**接線前修**：wrap `httpx.RequestError` 視 retryable + 耗盡 `logger.error`。
- **N-3** exc 洩漏泛化（`ai_assistant.py:625,726`，= CAQA C-7 加強）— `str(exc)` 可能含 pod 內部 `ws://` cluster DNS 洩到 browser；server 端 `_logger.exception` 已對（保留）。方向：client 端泛化訊息 + correlation id，細節只進 log。
- **N-4** operator namespace/release 命名契約未 assert（`tests/test_agent_runtimes.py:291-292`）— `_NS`/`_RELEASE` dead 常數 + 常數缺 `pa-` 分隔符（impl 命名跨檔一致 `paas-ws-pa-{hash}`，純 test 衛生非 impl bug）。方向：補 `_names()` call-args 斷言（鎖 hash→namespace→RBAC 邊界值）。
- **N-5** SHA256-over-raw-bytes invariant 僅隱含（`test_..._skill_bundle.py` SB.1）— 加一行 `sha256(resp.content)==skill.bundle_sha256` 直接斷言廉價硬化 contract（pod 端責任，acceptable）。
- **gap-B（flip-gate runbook）operator `/health` `signs token_use` 機器驗訊號** — CAQA/reviewer 建議 operator `/health` 暴露 `signs token_use: true`，把 pilot flip-gate runbook step 2 的人工「確認 operator 簽 `token_use`」轉成機器可驗訊號（D-CAVEAT-2）。**[2026-05-25 gap-B 拍板] 列 follow-up（Phase E+ fleet 規模補機器驗）**；屬 `paas-operator` 改動。**pilot fallback**：step 2 人工確認 operator 部署版本 ≥ `token_use` 簽發版本。詳 `docs/guides/deployment/agent-runtime-pilot-flip-gate-runbook.md` step 2 + §Gap。
- **gap-C（flip-gate runbook）secret 跨進程同值 startup 斷言** — D-CAVEAT-1：namespace/auth/webhook secret 改 per-process env 後，消費進程（Odoo web/cron worker + operator）任一不同值或空值 → namespace hash 靜默分歧（webhook 永遠 404 / 同 project 不同 worker 算出不同 `paas-ws-pa-{hash}`）；charts `optional:true` 使 partial 部署不在 deploy 時炸。方向：hash 消費點（`agent_session._get_namespace_secret` / `agent_runtime_webhook` verify）加 startup/health 一致性斷言，把靜默分歧前移成「啟動即報」。**[2026-05-25 gap-C 拍板] 列 follow-up（Phase E+ 補斷言）**。**pilot fallback**：runbook step 4 手動 `sha256sum` 兩端比對 + `printenv` 驗 5 env 非空。詳 runbook step 4 + §Gap。
- **info（6）**：C-7（exc 洩漏 SSE，併 N-3）/ Q-1（`datetime.utcnow()` deprecated，`ai_assistant.py:659`，方向 `datetime.now(timezone.utc)`）/ Q-2（HS256 verify 雙實作 — 跨 process/repo 無法共享 module，刻意非 DRY 違規）/ Q-4（`new_state` 寫入無 Selection 預檢 → out-of-enum 得 500 非 silent corruption）/ P-3（cron drift 5min sequential `get_status`）/ P-4（operator `verify_token` + jti cache 似未被 router 取用，operator 只簽 / pod 才驗 → 可移除或待用 helper）。

**gate 驗收**：Phase D 第一次 spawn-or-attach 真起 pod（enable flag）前，CAQA / reviewer 再 verify 一輪 — 特別 **F-PC-1 實機 WS connect**（websockets 版本對）/ **F-PC-5 + F-PC-6 error 通道 + WS lifecycle**（operator error 正確 surface + 每條對話無 WS 洩漏）/ **F-PC-7 A 嚴格**（**[2026-05-26 更正] stg 已 closed**：`dabb275` 已 merge main PR #3 `bb31676` + build 進發佈 image unconditional + stg pilot 已驗；**prod operator 全簽 `token_use` gate = 未來式**，移至 prod 上線 runbook）。對應 plan `agent-runtime-pod-plan.md` Phase D §「Phase D enable gate」+ §Pilot runbook flip-gate caveats。

### Phase D reviewer R-findings（R-2/R-3/R-4 — minor，非阻 merge）

reviewer Phase D §6 新發現，無一 high-severity / 無一 code 缺陷。**R-1**（HC-2 read-site SET-SITE invariant comment）+ **R-5**（demo template `is_public` vs draft comment tidy）已由 reporter 本 cycle **順手補 comment**（`8d152f7` 後的 docs commit）。以下 3 條是 code/test 改，**非 reporter 改**，Phase D pilot 前或 Phase E 補：

- **R-2** F-PC-6 aclose log level（`ai_assistant.py:741`）— best-effort except 落 `_logger.debug`，systemic close-handshake 失敗在 prod（INFO/WARNING）不可見。方向：升 `info`/`warning`（impact bounded — 內層 `loop.close()` 仍回收 socket）。CAQA 已列 info，reviewer R-2 再確認。
- **R-3** F-PC-2 empty-PREVIOUS 獨立負例（`test_agent_pod_phase_d_hardening.py` F-PC-2 群）— empty-slot 不放寬 invariant **code 正確但無獨立負例**，未來 refactor 拿掉 empty-guard 不會 red。方向：補一條 `empty PREVIOUS + 任意 sig → 401` 負例。
- **R-4** bootstrap hint int-coerce（`project_project.py:133`，security-auditor optional/defensive）— `bootstrap_owner_partner_id` 未 `int()`-coerce 即 `browse()`；今天 unreachable（無 untrusted set-site，見 [[constraint-input-trust-expansion]] §Forward-looking invariant）。方向：一行 `int()` guard 防未來 careless trusted caller 傳 string。
- **何時**：Phase D pilot 前或 Phase E（code/test PR，非 reporter）。

## F-GC-1 — SKILL_BUNDLE_TOKEN secretKeyRef hardening（gap C pilot）

- **Severity**: minor（security hardening；pilot 可接受現狀）
- **Origin**: agent-runtime-pod epic gap C（operator chart install + WS transport，2026-05-25，PR #63）
- **問題**: operator 把 `SKILL_BUNDLE_TOKEN` 以 **plain env value** 注入 pod（install 走 helm `values.env.SKILL_BUNDLE_TOKEN`、resume 走 `kubectl set env`）→ token 明文落 pod spec（`kubectl get pod -o yaml` / `describe` 可見）。
- **為何 pilot 接受**: token 短壽（exp 1h）+ 每次 resume 重簽重注 → 明文風險窗 ≤1h 且輪替；且 plain env 是 operator resume（`kubectl set env`）機制的硬相容需求 — secretKeyRef 會被 `kubectl set env` 的 plain value 覆蓋成 drift。leader 已認同 plain env、secretKeyRef 列此 follow-up。
- **hardening 方案（redesign，非 chart-only）**: operator 改為管 per-release K8s Secret（存 token）+ resume 時 patch 該 Secret；chart 改 `env.valueFrom.secretKeyRef`。需 operator + chart lock-step。
- **owner**: paas-platform（operator）+ paas-charts（chart）；**待 leader 排程**。
- 對照: [[agent-runtime-operator-chart-install-contract]] §Q2b / §替代方案、design §6.5。

## F-PILOT — agent-runtime pilot e2e durable 回收清單（2026-05-26）

> Step 6 flip 實跑閉環時打的 ephemeral patch + 揭露的系統性 chart/operator 缺口 + plan-level 設計缺口。**這份是分派藍圖**（leader 拿去派各 repo agent 轉正）。根因鏈見 [[agent-runtime-pilot-e2e-gap-chain]]、process learning 見 [[spawn-install-values-missed-forward]] + [[plan-gap-stop-and-report]]。

| # | tier | 主題 | lane | plan-level |
|---|------|------|------|-----------|
| F-PILOT-1 | 1 (ephemeral, Fleet sync 洗) | operator image f952f14 → pin | paas-cd | no |
| F-PILOT-2 | 1 (ephemeral, Fleet sync 洗) | operator repoUrl external → chart default | paas-charts(PR#3)+paas-cd | no |
| F-PILOT-3 | 1 → ✅ durable (image bake + pin; 殘留 ns-重建邊界 LOW) | odoo websockets → PR#68+rebuild+pin | paas-platform+deploy | no |
| F-PILOT-4 | 2 (系統性, 每 project 撞) | 動態 ns registry pull secret provision + imagePullSecrets forward | paas-platform(operator)+設計 | **YES** |
| F-PILOT-5 | 2 (系統性) | NP odooNamespace forward + chart default | paas-platform(operator)+paas-charts | minor |
| F-PILOT-6 | 2 (系統性) | chart probe /health→tcpSocket | paas-charts ± image | no |
| F-PILOT-7 | 2 (系統性) | chart strategy=Recreate (RWO PVC) | paas-charts | no |
| F-PILOT-8 | plan-level | LLM credential 注入 | 設計拍板 + image | **YES** |
| F-PILOT-9 | 3 (feature) | agent_template 管理 UI + 綁定 UI | paas-platform 未來 Phase | no |
| F-PILOT-10 | image lane | echo→真 LLM chat + skill-bundle fetch | paas-agent-runtime image team | no |

### Tier 1 — ephemeral，Fleet sync / pod restart 會洗掉（優先轉正保 pilot 穩定）

**F-PILOT-1** — operator image `f952f14` 由 `kubectl set image` 暫頂
- 問題: operator chart Fleet sync / helm-upgrade 用 chart image.tag 重設 → 回退舊 image → 撞回第 1 關（ns-prefix validate_namespace 500）+ 第 3 關（projectId）
- 修法: paas-cd 在 stg values pin `f952f14`（含 PR #66 + #67，已 merge develop）
- lane: **paas-cd** ｜ ⚠️ 與 F-PILOT-2 **必須原子化一起上**（merge PR#3 觸發 Fleet sync 時 image 沒同步 pin 就回退）

**F-PILOT-2** — operator `AGENT_RUNTIME_REPO_URL=external` 由 `kubectl set env` 暫頂
- 問題: 同上 Fleet sync 回退 → helm pull 撞 401（Gitea index tarball 公開 host vs operator 內部 DNS、helm 不跨 host 帶 basic-auth）
- 修法: paas-charts **PR #3** operator chart default repoUrl 改 external + paas-cd 移除 CD pin（環境無關值該繼承 chart default）
- lane: **paas-charts(PR#3) + paas-cd**

**F-PILOT-3** — odoo `websockets` 由 `pip install` 暫頂進 running pod
- 問題: odoo pod restart / redeploy 失去 pip install → `_stream_agent_runtime` import websockets ImportError → 「WS client unavailable」
- 修法: **PR #68** merge（requirements.txt 加 websockets）+ paas-odoo image 重建（image tag `20260526.1221` bake requirements）+ redeploy dev-sandbox-stg（paas-cd pin odoo image）
- lane: **paas-platform(PR#68) + deploy**
- ✅ durable 範圍: survive odoo pod restart / `helm upgrade --reuse-values` / Fleet sync（image 已 bake websockets，realistic 威脅消除）
- ⚠️ 殘留邊界（informational, LOW）: 全新 `create.sh` install（ns 重建）的 odoo image 來自 deploy-stg `--image` 參數、非 chart default（chart values.yaml odoo default = 通用 `odoo:18.0`、非 paas-odoo）。「終極 durable（survive ns 重建）」靠 deploy-stg 流程**永遠** build from develop（已含 PR#68）→ 每次 deploy-stg 都帶 websockets；chart `odoo:18.0` default 只在「不走 deploy-stg、裸 create.sh 不帶 --image」才會中，但那會部署無 woow_paas_platform addon 的通用 odoo（整個 app 不見、非僅 websockets），不是正常情境 → 無需緊急動作，正常 deploy-stg 已覆蓋
- lane（殘留）: **paas-platform/charts**（如要硬化：deploy-stg/create.sh 把 stg-mode odoo image default 指向最新 paas-odoo、而非 chart 通用 default）

### Tier 2 — 系統性 chart/operator 缺口（每個新 project 都撞；本 pilot 靠 patch 撐，operator 重 helm install 會洗、rollout-restart 保留）

**F-PILOT-4** — 動態 per-project ns 無 registry pull secret + chart 沒帶 imagePullSecrets ⚠️ **plan-level**
- 問題: agent-runtime 私有 image（jcr-prod）；operator `_ensure_namespace` 只建裸 labeled ns、無 dockerconfigjson；chart imagePullSecrets 預設空、operator 沒 forward → ImagePullBackOff 401（匿名 pull 被拒）
- 修法: operator cold-start 把 registry pull secret provision 進動態 ns + forward `imagePullSecrets`（chart 已支援 value）
- lane: **paas-platform(operator) + 設計拍板**: creds 來源（copy operator 自身 jfrog-registry / 新 config / 沿用 marketplace paas-registry 機制）+ 租戶 ns 放 registry creds 的安全
- plan-level: **YES**
- **[設計已拍 2026-05-26 · Track B]** creds 來源 = **沿用 marketplace `paas-registry`**（複用 operator 既有 `helm.py:649 ensure_registry_secret`，非 copy / 非新 config）；安全邊界 = **落租戶 ns 但收緊 pod SA/RBAC 不可讀 secret**（imagePullSecrets 只給 kubelet，pod process 讀不到）。design §8.1 / plan §Phase E0（WS-4a/b/c + T4.1-4.4；安全 INV-1 可測：pod 試讀 secret → Forbidden）。
- **[Phase E0 operator green 2026-05-27 · `3d7e083`]** operator 端**已落**：cold-start `k8s_service.ensure_registry_secret(ns)`（複用 `helm.py:649`，與 marketplace `create_namespace` byte-identical 同紀律）+ `_build_install_values` forward `imagePullSecrets:[{name: settings.registry_secret_name}]`（**只帶 secret 名、不帶 creds**）。CAQA PASS WITH CAVEATS / reviewer APPROVE，operator **無新增洩漏面**。**剩 cross-repo lock-step（owner paas-charts，GA / pilot-e2e 前 hard gate）= E0-CAVEAT-3**：chart **SA-minimal RBAC**（pod SA 無 `secrets` get/list/watch；INV-1 唯一防線，失效 = 平台級 jcr-prod creds blast radius）+ **建議 registry creds pull-only-scoped**（registry IAM 層 defense-in-depth，非 operator code）。

**F-PILOT-5** — NP `odooNamespace` 沒被 operator forward → chart 錯 default
- 問題: chart NP ingress(odoo→pod WS)+egress(pod→odoo skill-bundle)都用 `.Values.networkPolicy.odooNamespace`，default `paas-system`（placeholder）≠ 實際 odoo ns（dev-sandbox-stg）；operator `_build_install_values` 只 forward egressWorkspaceIds、沒 forward odooNamespace → pod↔odoo 兩向被 agent pod 自身 NP 擋（refused）
- 修法: operator forward `networkPolicy.odooNamespace` + chart 修錯 default（理想設 required）
- lane: **paas-platform(operator) + paas-charts** ｜ minor design: operator 取 odoo ns 來源（parse ODOO_BASE_URL FQDN vs 新 config）

**F-PILOT-6** — chart probe path ↔ image health endpoint 不匹配
- 問題: chart liveness/readiness `httpGet /health`，但 agent image 不 serve `/health`（404）→ readiness 永不 ready + liveness 殺 → CrashLoopBackOff → service 不 route
- 修法: chart 改 `tcpSocket:ws(8000)`（最穩，uvicorn listen 即過）；或對的 health path；或 image team 補 `/health`
- lane: **paas-charts ± image team**

**F-PILOT-7** — chart RollingUpdate + RWO PVC → re-spawn Multi-Attach deadlock
- 問題: deployment 預設 RollingUpdate，新 pod 起來前舊 pod 不釋放 RWO workspace PVC → 新 pod Multi-Attach 卡 ContainerCreating
- 修法: chart `strategy: Recreate`（image agent 已 ephemeral patch 驗證可解）
- lane: **paas-charts**

### Plan-level 設計拍板

**F-PILOT-8** — LLM credential 注入（真 LLM agent）⚠️ **plan-level**
- 問題: echo 收口不用 LLM；真 agent 要模型金鑰，但「金鑰怎麼到 pod」未設計（via skill-bundle？template model_config？專用 secret？）；綁 template model_config_id 後 echo 行為不變（echo 不行使 LLM）
- lane: **設計拍板 + paas-agent-runtime image team** ｜ echo → 真 LLM 前必拍
- plan-level: **YES**
- **[設計已拍 2026-05-26 · Track B]** 注入路徑 = **專用 per-release K8s Secret + secretKeyRef env**（非 plain env / 非 skill-bundle 夾帶；LLM key 長壽，比 `SKILL_BUNDLE_TOKEN`(F-GC-1) 更該收緊）；隔離 = **per-tenant key**（`agent_template.model_config_id → ai.config.api_key` 已關聯，洩漏圈限單租戶）。產品策略（自帶 key/代付/quota/輪替）留 design open、不阻 code。design §8.2 / plan §Phase E0（WS-8a/b/c + T8.1-8.4；INV：secretKeyRef 不落明文 + per-tenant 隔離）。
- **[Phase E0 operator green 2026-05-27 · `3d7e083`]** operator 端**已落**：`_provision_llm_secret`（**keyless 早退** → 有 api_key 才建 per-release `{release}-llm` Secret、stringData、`paas-managed` label；INV-2 per-tenant 隔離成立，release+ns 皆 keyed by hash）；api_key 隨 spawn request（解耦 operator↔Odoo DB）。**明文 key 5 點獨立重驗不洩漏**（`agent_runtimes.py` 零 logger / `apply_manifest` 0600 tmpfile argv 只有檔名 / generic exception handler 不 echo body / `KubectlException.__str__` message-only / `Optional[str]` 無 422 echo）。**E0-R-1**（key-absent 負分支 missing-negative，mutation 證實刪 guard 全 43 test 仍綠）已由 tester `cdf5368` `test_T81c` mutation-resistant lock 關閉 → 見 [[credential-claim-test-fixture-coupling]] §missing-negative 形態。**剩 cross-repo + lifecycle caveat**：
  - **E0-CAVEAT-2**（owner paas-charts lock-step）：chart `LLM_API_KEY` secretKeyRef **必須 `optional:true`**（與 Phase D charts `optional:true` pattern 一致）；否則 keyless（echo pilot 主路徑）pod `secretKeyRef` 指向不存在 `{release}-llm` → kubelet `CreateContainerConfigError` → **pod 起不來，阻 pilot e2e**。operator 無法防（conditional-create 契約正確，失效全在 chart 端）。
  - **E0-CAVEAT-1**（orphan 明文 LLM Secret on teardown）→ 見 **F-PILOT-10 HARD gate bind**（真 LLM 上線前必收）。

### Tier 3 / 其他 lane

**F-PILOT-9** — agent_template 管理 UI + project 綁定 UI（Phase D 故意延後）
- 問題: 無 backend view/menu/action 建/發佈/綁 agent_template；project form 只曝露 use_agent_runtime、沒 agent_template_id；pilot 靠 dev-mode generic view / ORM 種
- lane: **paas-platform 未來 Phase** ｜ severity low（pilot 可用 seed）

**F-PILOT-10** — echo → 真 LLM chat 邏輯 + skill-bundle 實際 fetch（image lane）
- 問題: image 1.0.1 echo 收口（收 user_message → 1 個 echo text_chunk，done 由 ws_server）；真 LLM 處理 + skill-bundle fetch 未實作
- 修法: 依 [[agent-runtime-ws-protocol]]（PR #69）契約實作 pod `/v1/chat` 回覆 + skill-bundle fetch
- lane: **paas-agent-runtime image team**（與 F-PILOT-8 LLM cred 連動）
- **[E0-CAVEAT-1 HARD gate bind 2026-05-27]** orphan 明文 LLM Secret on teardown：operator `delete_runtime` 只 `helm uninstall`、`reap` 只 scale-0，**全程無 `kubectl delete ns`**（auditor sweep 確認）→ out-of-band（`apply_manifest`，無 owner-reference）建的 `{release}-llm` 明文租戶 key **不被 helm uninstall 回收**，殘留 ns 直到外部清。**echo 階段邏輯上不可能 orphan**（無 api_key → `_provision_llm_secret` 早退不建 secret），故 Phase E0 **非 blocker**；但 **F-PILOT-10 真 LLM 上線（會帶真 api_key）前必收 = HARD gate（非 someday / optional）**。屆時做 **holistic teardown-lifecycle design**（LLM Secret + registry secret reap + ns-delete **一起設計**，**非** piecemeal 加一行 `kubectl delete secret`）；方向：teardown 刪 ns（一次收全部）/ 或 chart 接管 Secret 使 uninstall 回收。CAQA 已路由 **planner**（plan-completeness gap：design per-release Secret 生命週期隱含假設 ns teardown 回收，但 operator 從不刪 ns）。owner: paas-platform(operator) + planner（design §8.2 teardown 補寫）。

## F-GC-2 — deploy-stg/create.sh 重跑洗掉 odoo agent-runtime 啟用（durability）

- **Severity**: medium（durability；pilot 一次性 OK，但每次重 deploy-stg 後 odoo 端 agent-runtime 會被關掉）
- **Origin**: agent-runtime-pod epic gap C / odoo (i) pilot 部署（2026-05-25，PR #64 + deploy-stg 4a/4b）
- **問題**: odoo 端 agent-runtime 啟用走 (4b) `helm upgrade stg --reuse-values --set agentRuntime.* --set networkPolicy.allowAgentRuntimePods=true`（chart from-values，PR #64）。但 `create.sh`（k8s-dev-sandbox / deploy-stg 用）是 `helm upgrade $SLUG --set <stg-mode 值>` **fresh set、無 `--reuse-values`**（`create.sh:294`）→ **下次 deploy-stg/create.sh 重跑會把 agentRuntime（secret keys）+ allowAgentRuntimePods 洗回 chart 預設（空 / false）→ odoo 端 agent-runtime 被關掉、skill-bundle/auth/namespace secret env 消失**。
- **為何 pilot 接受**: 一次性 e2e 跑 4a→4b 即可、值撐到下次 deploy；每次重 deploy-stg 後重跑 (4b) 即恢復。secret 值刻意不進腳本/CI（敏感、使用者提供）是對的設計。
- **durable 修法（腳本層，非 chart-only）**: (i) odoo agent-runtime secret 改 **existingSecret**（pre-create K8s secret、像 operator 端 paas-cd 慣例）→ create.sh redeploy 不碰、secret 不落 helm values；(ii) `allowAgentRuntimePods=true`（非敏感）做成 create.sh stg-mode 預設 或 stg values file 固定套。
- **owner**: paas-platform（odoo-dev-sandbox chart / deploy-stg lane）+ paas-cd（secret 慣例）；**pilot 驗證後排程**。
- 對照: [[agent-runtime-operator-chart-install-contract]]（§附帶 WS transport / NP）、本檔 F-GC-1（同 pilot 另一 hardening）、F-PILOT-3（同源 deploy-stg stg-mode 重跑 durability；odoo 端另一軸 = image default，本條 = agentRuntime secret env + NP flag）。
- **狀態（2026-05-26 verify）**: **保留 open**。軌 2 durable 收尾（agent-runtime chart `0.1.1` / operator+paas-cd existingSecret / F-PILOT-3 odoo image）**皆未涵蓋本條** —— 實查 develop `create.sh` 仍為 fresh `--set`（HELM_INSTALL_ARGS 僅 `sandbox.branch/ttl/mode` 等，**無** agentRuntime / `secrets.existingSecret` / `allowAgentRuntimePods` / `--reuse-values` 任何 wiring；chart 雖具 `secrets.existingSecret` + `agentRuntime.*` + `allowAgentRuntimePods` 機制，但 stg-mode 部署流程未啟用），F-GC-2 技術結論在現況仍成立、durable 修法 (i)+(ii) 尚未實作 → **不標 RESOLVED**。

## F-GC-3 — `helm.py` argv log 含 repo-read token（credential-in-logs，E0 CAQA 同源掃出）

- **Severity**: low-medium（credential-in-logs hygiene；**非本輪 E0 diff 引入**、非租戶 LLM key）
- **Origin**: Phase E0 CAQA 同源 secret-provisioning sweep 順帶掃出（明標非 E0 阻斷 / 非 caveat）；token 由 gap C `df6348e` 引入
- **問題**: `helm.py:77`（及 `:481` / `:1121`）`logger.info(f"Executing: {' '.join(cmd)}")` log 完整 argv → `helm install/upgrade` 的 `--password <agent_runtime_repo_read_token>`（Gitea **repo-read token**）落 INFO log
- **方向**: redact argv 的 `--password` 值（log 時遮罩）/ 或改 file·env 傳 token 不進 argv
- **owner**: paas-platform(operator)；**非 GA hard gate**（repo-read token scope 有限），但 credential-in-logs 應排程清
- 對照: [[credential-claim-test-fixture-coupling]]（同 epic credential hygiene 紀律）；`docs/tracking/caqa-report-2026-05-27-agent-runtime-pod-phase-e0.md` §同源 code path 檢查 + §out-of-scope follow-up

## See also

- [[cloud-service-project-decouple]] §Consequences — cycle 1 決策後仍要持續注意的負面影響
- [[mcp-project-decouple]] §Consequences + §Round 2 改動敘事 — cycle 2 決策後仍要持續注意的負面影響
- [[mcp-url-redesign]] §Consequences + §Round 2 改動敘事 — cycle 4 決策後仍要持續注意的負面影響
- [[odoo-addon-std-rule-symmetric-deactivation]] — F1 / F13 的 doc 形式
- [[cross-workspace-task-data-leak]] §Hardening 建議 — F5 的更完整 context
- [[direct-unlink-bypass-cleanup-hook]] — F14 的根因 context
- [[system-mcp-create-acl-bypass]] — F16 的根因 context
- [[tag-scoped-test-misses-full-u-failures]] — F19 「test coverage hole」cluster 的根因 context
- [[two-layer-acl-asymmetric-deny]] — F-X2 的根因 context（cycle 4 HC-2）
- [[legacy-endpoint-scope-blindness]] — cycle 4 HC-1 的根因 context（plan AC scope blindness）
- [[constraint-input-trust-expansion]] — F-PA-15 的根因 context（cycle 5 round 1 HC-3 同 family — m2m write constraint grantor authority check；Phase B HC-2 是其 image 端同型 instance）
- [[attachment-row-level-acl-and-anchor-stamping]] — F-PA-16 的根因 context（cycle 5 round 1 HC-4 — bundle attachment defense in depth；Inv A2 = Phase B pod 零直連 ir.attachment 守線的 woow_paas_platform 側依賴）
- `docs/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md` — Phase B image source 完成 durable（cross-phase learnings：CI gate 三度價值 / HC-2 cross-phase 驗證 / 2 process learning）
- [[ci-workflow-container-missing-node]] — Phase B B-5 抽 pitfall（CI `container: <lang-image>` + checkout → node exit 127 false green）
- `docs/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md` — Phase C wiring 收口 durable（cross-phase learnings：C-1 disputed severity 第三條路 MOOTED / cross-repo `token_use` 三端 sign-first-then-verify zero-downtime / phase-versioned test 第二次）
- [[phase-versioned-test-assertion]] — Phase C M.9 第二次撞牆抽 pattern（phase test 勿硬編當時 schema 值，用 monotonic `>=` / 存在性斷言）
- `paas-agent-runtime` repo `docs/PHASE_B_HANDOFF.md` — Phase B → C/D carry-over canonical home（cross-repo；SC-1~SC-9 + wiring assumptions）
- `paas-agent-runtime` repo `runtime/transport/auth.py` + PR #2 `ba6b417` — Phase C C-1 WS 側對稱補丁（token_use B transitional，F-PC-7 A 嚴格收緊前置）
- `docs/tracking/agent-runtime-pod-phase-d-enable-gate-2026-05-25.md` — Phase D enable-gate hardening 收口 durable（epic A-D code-ready 完成；3 takeaway：forward-coupling test fixtures / cross-repo charts 落點 / HC-2 trust-expansion 第 4 度）
- [[credential-claim-test-fixture-coupling]] — Phase D takeaway 抽 pattern（硬化共用 token/secret/api_key 路徑 → 自建該 credential 的 test fixture 須 lock-step 補 claim；Phase B/C/D 3 建立點）
- [[cross-repo-artifact-landing-assumption]] — Phase D takeaway 抽 pitfall（plan 假設 artifact 落 repo X，實際落 repo Y；impl 前勘查 catch）
- `paas-agent-runtime` repo F-PC-7 WS verifier A 嚴格（`a8b3ce2`+`dabb275`）— **[2026-05-26 更正] 已 merge main（PR #3 `bb31676`，origin/main `3263352`/PR#4）+ build 進所有發佈 image（unconditional 無 toggle），stg closed**；prod operator 全簽 gate = 未來式（見 prod 上線 runbook）
- 原始 review（合成來源，本 doc 引用不複製）：
  - `docs/decisions/reviews/caqa-2026-05-16.md` §Caveat 清單（M1-M4 + L1-L3 — cycle 1）
  - `docs/decisions/reviews/caqa-2026-05-18.md` §Caveat / Out-of-scope（C1 + F12 — cycle 2）
  - `docs/decisions/reviews/pr-review-verdict-2026-05-17.md` §Recommendation（reporter follow-up 1-9 整合 — cycle 1）
  - `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md` §Minor / Info（17 條 round 1 minor — cycle 2）
  - `docs/decisions/reviews/pr-review-verdict-mcp-round2-2026-05-18.md` §新 minor / info（R2-1 ~ R2-4 — cycle 2 round 2）
  - `docs/decisions/reviews/pr-description-draft.md` §Follow-up（cycle 1 候選清單）
  - `docs/tracking/caqa-report-2026-05-20-project-detail-page-issue-48.md` §Blocker + §Caveat（B-1 + C-1..C-3b — cycle 3）
  - `docs/tracking/reviewer-report-2026-05-20-project-detail-page-issue-48.md` §Hard caveats + §Soft caveat tracked（R-1 ~ R-10 — cycle 3）
  - `docs/tracking/caqa-report-2026-05-20-mcp-refactor.md` §Medium / Low findings（M1 / M2 / L1-L4 — cycle 4）
  - `docs/tracking/reviewer-report-2026-05-20-mcp-refactor.md` §Hard Caveats + §Soft Caveats + §Appendix A round 2 closure（HC-1/2/3 + SC-1~SC-20 + NM-R1 — cycle 4）
  - `docs/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md` §6 Blocker + §7 Caveat + §13 Round 2 verify + §14 Round 3 HC-3/HC-4 verify（B-1 + M-1~M-5 + I-1~I-6 + C-A-1~C-A-3 — cycle 5）
  - `docs/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md` §3 4 HC Hard Caveats + §5 28 SC Soft Caveats + §12 Round 2 HC-3/HC-4 closure（HC-1~HC-4 + SC-1~SC-28 + EC-3a/EC-4a — cycle 5）
  - `docs/tracking/caqa-report-2026-05-24-agent-runtime-pod-phase-c.md` §7 C-1 disputed + §8 caveat（C-1~C-6/A-1/P-1/P-2 — Phase C）/ `docs/tracking/reviewer-report-2026-05-24-agent-runtime-pod-phase-c.md` §5 C-1 hardening + §6 cross-check + §7 N-1~N-5 + §12 Phase D gate（Phase C）
  - `docs/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md` §四面向 + §Caveat（D-CAVEAT-1/2 + 9 info + 3 carried-forward — Phase D）/ `docs/tracking/reviewer-report-2026-05-25-agent-runtime-pod-phase-d.md` §4 HC-2 airtight + §6 R-1~R-5 + §7 carried-forward（Phase D）
