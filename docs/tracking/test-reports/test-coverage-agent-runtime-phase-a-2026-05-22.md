---
name: test-coverage-agent-runtime-phase-a-2026-05-22
phase: red-explained
team: feature-agent
test_explainer: agent-runtime-pod-test-explainer
plan_ref: docs/plans/2026-Q2/agent-runtime-pod-plan.md
design_ref: docs/plans/2026-Q2/agent-runtime-pod-design.md
created: 2026-05-22
updated: 2026-05-22
status: ready-for-implementation
related:
  - docs/decisions/refactor/cloud-service-project-decouple.md
  - docs/decisions/refactor/mcp-project-decouple.md
  - docs/decisions/refactor/mcp-url-redesign.md
  - docs/decisions/patterns/mcp-acl-6-rule-split.md
  - docs/decisions/patterns/unbound-project-acl-dual-leg.md
  - docs/decisions/patterns/two-layer-acl-lockstep.md
  - docs/decisions/patterns/sidecar-backed-flag-lifecycle.md
  - docs/decisions/patterns/cloud-service-unlink-override-cleanup.md
  - docs/tracking/test-reports/test-coverage-mcp-project-decouple.md
  - docs/tracking/test-reports/test-coverage-mcp-refactor-2026-05-20.md
---

# Test Coverage Report — Agent Runtime Pod Phase A（人類可讀版）

> 這份報告**不是** plan AC 的逐條翻譯（plan 是 source-of-truth）。
> 它解釋：**「為何 Phase A 這樣測、為何不那樣測」**，餵給 reviewer / CAQA / stakeholder 對齊測試意圖，避免 reviewer 看完 test 但 miss tester 的 scope rationale。
>
> **reviewer special attention**：
> 1. 第 3 章 cell-map 矩陣的 **implicit-through** 標記 — 沒寫成獨立 assertion，但走 walk path / behaviour chain 間接守住的 section
> 2. 第 5 章 **3 條 regression lock**（C.4 / R.3 / R.9）— **不是漏寫的 red**，是刻意鎖既有綠線不被本 epic 順手破壞
> 3. 第 7 章 **Q1 / Q2 leader sign-off** — T.5 test 拆 5 method 是 2026-05-22 hybrid 拍板的直接落地

---

## 1. TL;DR

本 Phase A 把 **`project.cloud_service_id`（M2O）升 `cloud_service_ids`（M:N）** + 加 4 個新 Odoo model（`agent_template` / `agent_session` / `skill` / `project_access`）+ 加 additive 欄位（`external_mcp_allowed` / `agent_template_id` / `use_agent_runtime`） — **LangGraph runtime 還沒搬 pod**（既有 `ai_client.py` 路徑全保留），但 schema 已經為 Phase B-G 鋪好。

對應 test：**6 個新 test 檔，54 個 test method**（對應 49 AC：A×9 + T×5 + C×5 + N×12 + R×9 + M×9）。Phase A TDD red phase 跑 41 cases（method 因 SKIP gate 不全進統計）：

| 狀態 | 數量 | 含義 |
|---|---:|---|
| **FAIL**（預期 red） | 23 | new field / new model / new constraint / new migration / new ACL walk — 全部都是 implementation 還沒落地的預期紅 |
| **ERROR**（setUp 階段拋出） | 4 | new model 不存在 → `setUpClass` raise `RuntimeError` fail-fast；對 implementer 是 prerequisite signal |
| **SKIP**（prereq 缺失） | 16 | 14 條依賴 `project.cloud_service_ids` M2M 欄位、2 條依賴 4 個新 model — prereq 落地後會自動進入測試 |
| **PASS**（regression lock 今天就綠） | 2 | C.4 + R.3 + R.9 三條「鎖既有綠線」中，部分 assertion 在 refactor branch 已 fix 範圍綠（見 §5） |
| **既有 refactor branch test**（PR #47 + PR #54） | 37/37 | **全綠**；本 Phase A 對 mcp_server schema 零變動（見 §1.3.4 D4 守線確認），ACL walk path 改 M2M 但 outer AND `scope='user'` 不動 |

紅燈集中在 4 大塊：（1）`project.cloud_service_ids` M2M 欄位 + workspace_ids computed M2M（A 段）；（2）4 個新 model + 跨 model SQL constraint + bootstrap hook（N 段）；（3）ACL dual-leg walk path 改 M2M（R 段）；（4）migration `18.0.1.0.26/` 目錄 + manifest version bump（M 段）。

**全部紅燈都是 plan 預期 red 的 boundary**，**implementation 跑完一輪後若全綠**，等於 Phase A 49 AC 全達成。

---

## 2. Scope（Phase A 在測什麼、不測什麼）

### 在 Phase A scope 內

| 維度 | 範圍 |
|---|---|
| Schema break change | `project.cloud_service_id`（M2O）→ `cloud_service_ids`（M:N）+ 刪 `UNIQUE` constraint + through table `project_cloud_service_rel` |
| Schema 連鎖修改 | `cloud_service.project_ids` One2many → Many2many reverse（同一 relation table）；`task.primary_cloud_service_id` related → 獨立 M2O + computed default + drift handler；`task.extra_cloud_service_ids` domain 拆 same-workspace 限制 |
| Additive 欄位 | `project.external_mcp_allowed` / `project.agent_template_id` / `project.use_agent_runtime`（**dormant**） |
| 新 model | `woow_paas_platform.agent_template` / `agent_session` / `skill` / `project_access` |
| 新 ACL | 4 個新 model × CSV row in `ir.model.access.csv`；mcp_server / mcp_tool 既有 dual-leg ir.rule 內 walk path 改 M2M（保留 outer AND `scope='user'`） |
| Cleanup 語意延伸 | `cloud_service._do_cleanup` + `unlink()` filter 加 URL-prefix match（M:N 下不誤殺其他 cloud_service 的 sidecar MCP） |
| Migration | `src/migrations/18.0.1.0.26/{pre,post}-migrate.py` 跑 §7.0.2 6-step + §7.0.7 Step 7-10；既有 1:1 binding backfill；既有 project bootstrap `project_access` owner row；idempotent |

### 明示 OUT OF Phase A scope（留給 Phase B-G）

| 未測項目 | 留給 | 理由 |
|---|---|---|
| LangGraph runtime image / Dockerfile / Helm chart | **Phase B** | Phase A 還沒拆 runtime 出 Odoo process；既有 `ai_client.py` 同步路徑保留 |
| paas-operator `/api/agent-runtimes/*` endpoints | **Phase C** | Phase A 對 paas-operator 零變動 |
| WS envelope schema / jwt sign+verify / jti replay defense | **Phase B + C** | Phase A 無 transport 層改動 |
| Pilot project chat invoke → pod path（E2E） | **Phase D** | `use_agent_runtime` flag dormant default False，Phase A 沒人開 |
| `agent_template` ↔ `project` × `skill` × `mcp_server` 跨 model 完整 binding 矩陣 | **Phase B/C test scope expansion** | Phase A 只覆蓋 `_check_visibility_consistency` + `_check_mcp_subset` 兩條 constraint；完整 cross-ref semantics 等模型有實際 fixture 才測 |
| 4 個新 model 的 per-rule 行為矩陣（admin/user/guest × CRUD） | **Phase B test scope expansion** | Phase A 只覆蓋 `ir.model.access.csv` row registration（R.8）；ir.rule 行為矩陣等模型 populate 真實 fixture 才有測試價值 |
| Subprocess sandbox / NetworkPolicy egress hybrid scheme 行為 | **Phase B + Phase E** | Phase A 只加 `external_mcp_allowed` 欄位（default False）；NetworkPolicy 渲染邏輯落在 Phase B helm chart |
| Audit log structured JSON / Prometheus metric scraping | **Phase G** | Phase A 無 observability 新增 |
| `default_ai_assistant_id` config_parameter fallback / `ai_client.py` LangGraph 刪除 | **Phase F destructive** | Phase A 只**保留**，不動；§7.0.7 Step 10 明示 |

### 也不測但 reviewer 要對照看的（結構性 / 間接覆蓋）

| 未測項目 | 對應替代覆蓋路徑 |
|---|---|
| `task.workspace_id` deprecation 策略（保 related 還是改 computed M2M） | tester 故意留實作 latitude（design §7.0.3 + plan Step 5 留 §7 拍板）；A.2 `workspace_ids` 已是新 canonical store，足夠驗 ACL chain |
| `mcp_server.url` snapshot 演算法本身 | refactor branch 已 lock；本 Phase A 不改 `_model_build_mcp_endpoint_url`；C.* 用 `_sidecar_url()` helper mirror 同形狀，URL 改演算法會在 C.2 assertion 被抓 |
| Helm CLI 真實呼叫 | C.* 在 `_do_cleanup` raise 時 swallow（unit-test env 無 helm）；只驗 ORM-level filter 結果，不驗 helm 物理 uninstall |
| `agent_template` / `skill` / `agent_session` view XML form | Module install / 沙盒部署即可暴露 XML load failure；無需 unit test |
| project_access bootstrap 漏單 partner（cloud_service 無 owner） | post-migrate.py 用 `cloud_service.workspace_id.access_ids` 或 `project.create_uid` partner 雙路 fallback；漏單會在 log 顯示 warn（plan risk row） |

---

## 3. Cell Map — Test ↔ Design §x.x（54 test method × 49 AC）

> **legend**：
> - **explicit** = test docstring 直接引用該 design §（assertion 一一對應）
> - **implicit-through** = test 透過 walk path / behaviour chain 間接覆蓋（無獨立 assertion 但跑 walk 等同驗）
> - **regression lock** = 今天就 GREEN，鎖既有綠線不被破壞

### 3.1 A 段 — `test_agent_pod_phase_a_project_schema.py`（11 method / A.1-A.9）

| Test method | AC | Design § (explicit) | Design § (implicit-through) | Coverage type |
|---|---|---|---|---|
| `test_project_has_cloud_service_ids_m2m_field` | A.1 | §1.3.1 row 1（M2O → M2M）+ §7.0.2 Step 1（CREATE relation table）+ §7.0.2 Step 6（share storage with cloud_service.project_ids） | — | **explicit** |
| `test_project_has_workspace_ids_computed_m2m` | A.2 (shape) | §1.3.1 row 3（related M2O → computed M2M）+ §7.0.2 Step 5（aggregate from cloud_service_ids.workspace_id） | §2.7.3（NetworkPolicy egress 跨 workspace 渲染必須讀 workspace_ids stored）；§1.3.2（ACL chain 走 workspace_ids） | **explicit + implicit-through** |
| `test_workspace_ids_aggregates_across_cloud_services` | A.2 (behaviour) | §1.3.1 row 3 — aggregate semantic | §2.7.3 — cross-workspace project 蘊含 NetworkPolicy 必須兩個 workspace namespace 都開 | **implicit-through** |
| `test_unique_cloud_service_id_constraint_removed` | A.3 (constraint drop) | §1.3.1 row 1 + §7.0.2 Step 3（DROP CONSTRAINT） | — | **explicit** |
| `test_two_cloud_services_on_one_project` | A.3 (M:N semantics) | §1.3.1 row 1 — M:N two-member | §1.3.2（agent pod 物理邊界 = project，M:N 是其前提）；§2.7.3（多 cloud_service binding 是 per-project pod 服務多 workspace 的根據）| **implicit-through** |
| `test_cross_workspace_cloud_service_binding` | A.9 | §1.3.2（cross-workspace project）+ §1.3.1 row 3 | §2.7.3（NetworkPolicy egress 跨 workspace 渲染前提）；§4.7（primary_workspace 計費歸屬概念前提） | **explicit + implicit-through** |
| `test_legacy_cloud_service_id_reads_first_of_m2m` | A.4 | §7.0.2 Step 4（deprecation；getter override 走 cloud_service_ids[:1]） | — | **explicit** |
| `test_write_legacy_cloud_service_id_syncs_m2m` | A.5 | §7.0.2 Step 4（write() override 同步 dual-write；idempotent） | — | **explicit** |
| `test_external_mcp_allowed_field` | A.6 | §6.3（Hybrid external MCP scheme）+ §7.0.7 Step 8（default backfill FALSE） | — | **explicit** |
| `test_agent_template_id_field` | A.7 | §4.1（agent_template binding via project.agent_template_id）+ §1.3.1 row 6 | — | **explicit** |
| `test_use_agent_runtime_field` | A.8 | §7.1 Phase A + Phase D bullet（flag dormant in Phase A、flipped in Phase D pilot） | §7.1 Phase F（destructive cleanup 會刪 use_agent_runtime field） | **explicit + implicit-through** |

### 3.2 T 段 — `test_agent_pod_phase_a_task_schema.py`（9 method / T.1-T.5）

| Test method | AC | Design § (explicit) | Design § (implicit-through) | Coverage type |
|---|---|---|---|---|
| `test_primary_cloud_service_id_not_related` | T.1 | §7.0.3 + §1.3.1 row 4（從 related 改獨立 M2O） | — | **explicit** |
| `test_primary_cloud_service_id_user_writable` | T.2 | §7.0.3 + §1.3.1 row 4（user 在 multi-binding project 內手動選 primary） | — | **explicit** |
| `test_extra_cloud_service_ids_domain_no_workspace_clause` | T.3 | §7.0.3 + §1.3.1 row 5（拆 same-workspace 限制） | — | **explicit** |
| `test_extra_cloud_service_ids_allows_cross_workspace` | T.4 | §7.0.3 — domain 改 `parent.project_id.cloud_service_ids.ids` | §1.3.2（cross-workspace project 蘊含 task 跟著跨）| **explicit + implicit-through** |
| `test_primary_default_first_of_m2m_on_create` | T.5(a) | **§7.0.3 hybrid spec — Q2 (c) SIGNED-OFF 2026-05-22**（auto-default first） | — | **explicit** |
| `test_primary_user_override_survives_write` | T.5(b) | §7.0.3 hybrid spec — user-overridable | §4.7（pattern alignment with `agent_session.primary_workspace_id`）| **explicit + implicit-through** |
| `test_primary_domain_limits_to_project_cloud_service_ids` | T.5(b) | §7.0.3 hybrid spec — domain scope to `project_id.cloud_service_ids` | — | **explicit** |
| `test_primary_drift_preserve_when_still_in_new_set` | T.5(c) | §7.0.3 hybrid spec — drift preserve leg | — | **explicit** |
| `test_primary_drift_reset_when_evicted_from_new_set` | T.5(c) | §7.0.3 hybrid spec — drift reset leg（no orphan FK） | — | **explicit** |

### 3.3 C 段 — `test_agent_pod_phase_a_cloud_service_mn.py`（4 method / C.1-C.5）

| Test method | AC | Design § (explicit) | Design § (implicit-through) | Coverage type |
|---|---|---|---|---|
| `test_cloud_service_project_ids_is_many2many` | C.1 (shape) | §1.3.1 row 2（One2many → Many2many reverse）+ §7.0.2 Step 6（shared storage） | — | **explicit** |
| `test_two_projects_can_bind_same_cloud_service` | C.1 (M:N behaviour) | §1.3.1 row 2 — M:N reverse semantics | §1.3.2（project 跨 cloud_service 共享是 per-project pod 容量規劃的前提）| **implicit-through** |
| `test_do_cleanup_unlinks_only_own_sidecar_mcp` | C.2 + C.4 + C.5 | §7.0.4（filter 加 URL-prefix match）+ [[mcp-project-decouple]] Inv #4（cleanup hook 順序）+ [[mcp-url-redesign]] Inv #9（sidecar-backed lifecycle filter） | §1.3.3 row 4（M:N 下不誤殺其他 cloud_service 的 sidecar MCP） | **explicit + implicit-through** |
| `test_unlink_override_narrows_same_way` | C.3 | §7.0.4 + [[cloud-service-unlink-override-cleanup]] pattern（caller-agnostic safety net；unlink() override 與 `_do_cleanup` 必對等） | — | **explicit** |

### 3.4 N 段 — `test_agent_pod_phase_a_new_models.py`（13 method / N.1-N.12）

| Test method | AC | Design § (explicit) | Design § (implicit-through) | Coverage type |
|---|---|---|---|---|
| `test_agent_template_has_required_fields` | N.1 | §4.1（schema field 列表） | §4.6（skill / MCP binding 兩層 merge 路徑前提是這些欄位）| **explicit + implicit-through** |
| `test_agent_template_visibility_consistency` | N.2 | §4.1 `_check_visibility_consistency` | §4.2（agent_template ↔ project cardinality） | **explicit + implicit-through** |
| `test_agent_template_mcp_subset_constraint` | N.3 | §4.1 `_check_mcp_subset` | §4.6（merge 路徑 invariant — template 不能跨 project 借 MCP） | **explicit + implicit-through** |
| `test_agent_session_required_fields` | N.4 | §2.12 (model schema) + §4.7（primary_workspace_id）+ §4.8（quota_override_*） | §2.10（pod label/selector schema 反映於 session schema）| **explicit + implicit-through** |
| `test_agent_session_unique_project` | N.5 (project leg) | §2.12 SQL constraint（UNIQUE per-project pod）| §1.2（per-project pivot — 物理邊界）| **explicit + implicit-through** |
| `test_agent_session_unique_namespace_hash` | N.5b (namespace_hash leg) | §2.12 + §2.15 risk row 4（namespace_hash 衝突 80-bit birthday bound）| §2.7（namespace 模型 — 唯一性是 K8s namespace key 的前提）| **explicit + implicit-through** |
| `test_agent_session_namespace_hash_shape` | N.6 | §2.7（hmac_sha256(project_id, secret)[:10] → base32 16 chars 演算法） | §6.6（dual-secret 是 cluster-managed，test 只驗 shape）| **explicit + implicit-through** |
| `test_skill_required_fields` | N.7 | §3.2 (model schema) | §3.5（lazy fetch 前提 bundle_attachment_id 存在）；§3.8（bundle_sha256 是 signature/trust 基礎） | **explicit + implicit-through** |
| `test_skill_unique_name_version_per_project` | N.8 | §3.2 SQL constraint（per-project versioning） | §3.4（versioning Q2 拍板用 — UNIQUE 是版本管理基礎）| **explicit + implicit-through** |
| `test_project_access_required_fields` | N.9 | §4.3（schema — partner_id + role + user_id computed from partner_id；D3=A idiom）| §1.3.2（ACL 兩層 — project_access 不蘊含 workspace_access） | **explicit + implicit-through** |
| `test_project_access_unique_partner_per_project` | N.10 | §4.3 SQL constraint | — | **explicit** |
| `test_project_access_last_owner_cannot_be_unlinked` | N.11 | §4.3 `_check_at_least_one_owner` | §4.3（project 必有 ≥1 owner — ACL invariant）| **explicit + implicit-through** |
| `test_project_create_bootstraps_owner_project_access` | N.12 | §4.3（auto bootstrap owner）+ §7.0.7 Step 9（既有 project 補 owner） | §1.3.2（first-class ACL 在 first-class model 下的根基）| **explicit + implicit-through** |

### 3.5 R 段 — `test_agent_pod_phase_a_acl.py`（8 method / R.1-R.9）

| Test method | AC | Design § (explicit) | Design § (implicit-through) | Coverage type |
|---|---|---|---|---|
| `test_mcp_server_rule_workspace_user_walks_cloud_service_ids` | R.1 + R.4 | §7.0.5（dual-leg domain walk path M2O → M2M；leg 1 + leg 2）+ [[mcp-url-redesign]] Inv #11（outer AND `scope='user'` 守線；MAJOR 1 防 system row create direction） | §1.3.4（D4 invariant 不變；只 walk path cardinality 改 M2M） | **explicit + implicit-through** |
| `test_mcp_tool_rule_workspace_user_walks_cloud_service_ids` | R.2 | §7.0.5（sub-model 對等改）+ [[mcp-acl-6-rule-split]]（B1 sub-model parity） | [[cross-workspace-task-data-leak]] B1 pitfall（漏 sub-model 對等 = B1 復活）| **explicit + implicit-through** |
| `test_other_four_rules_intact` | R.3 | §7.0.5 — system_readonly × 2 + admin_full × 2 不動 + [[mcp-acl-6-rule-split]] pattern parity | — | **regression lock**（今天就綠） |
| `test_mcp_acl_rule_count_parity` | R.9 | §7.0.5 — meta-lock 6 rule count parity；Phase A walk-path update **不准** 順手刪 rule | [[mcp-acl-6-rule-split]] invariant — 6-rule 對等是 ACL pattern 完整性 | **regression lock**（今天就綠） |
| `test_bound_walk_member_can_read_bound_project_mcp` | R.5 | §7.0.5 leg 1 — bound walk integration | §1.3.2（workspace_access → project member 推導鏈仍走 cloud_service.workspace_id 為起點，M:N 後改走 cloud_service_ids 但 workspace 邊界守住）| **explicit + implicit-through** |
| `test_unbound_walk_follower_only` | R.6 | §7.0.5 leg 2 carve-out + [[unbound-project-acl-dual-leg]] pattern；`message_partner_ids` follower-only | — | **explicit** |
| `test_cross_workspace_project_mcp_visible_to_both_workspaces` | R.7 | **§1.3.2（M:N OR semantics — cross-workspace project；Q1 SIGNED-OFF 2026-05-22）** + §7.0.5 leg 1 walk M2M | §2.7.3（NetworkPolicy egress 必須兩個 workspace namespace 都開 — ACL 與 NetworkPolicy lockstep）| **explicit + implicit-through** |
| `test_user_group_access_row_present_for_each_new_model` | R.8 | §4.1 + §2.12 + §3.2 + §4.3（4 new model × `ir.model.access.csv` row）| [[two-layer-acl-lockstep]]（CSV row 是兩層 ACL lockstep 的最外層 gate）| **explicit + implicit-through** |

### 3.6 M 段 — `test_agent_pod_phase_a_migration.py`（9 method / M.1-M.9）

| Test method | AC | Design § (explicit) | Design § (implicit-through) | Coverage type |
|---|---|---|---|---|
| `test_pre_migrate_file_exists` | M.1 | §7.0.2 Step 1（SQL CREATE for relation table） | — | **explicit** |
| `test_post_migrate_file_exists` | M.2 | §7.0.2 Step 2-4 + §7.0.7 Step 7-10 | — | **explicit** |
| `test_manifest_version_bumped` | M.9 | §7.1 Phase A acceptance（manifest 18.0.1.0.25 → 18.0.1.0.26） | — | **explicit** |
| `test_step2_backfill_existing_1to1_binding` | M.3 | §7.0.2 Step 2（INSERT ON CONFLICT DO NOTHING） | — | **explicit** |
| `test_step3_drops_unique_cloud_service_constraint` | M.4 | §7.0.2 Step 3（ALTER TABLE DROP CONSTRAINT IF EXISTS） | — | **explicit** |
| `test_step4_cloud_service_id_column_kept` | M.5 | §7.0.2 Step 4（deprecated 一個版本；不 drop column）| §7.1 Phase F（drop column 留 phase F 才做）| **explicit + implicit-through** |
| `test_step8_external_mcp_allowed_default_false` | M.6 | §7.0.7 Step 8（idempotent default backfill）+ §6.3（conservative closed-egress default） | — | **explicit + implicit-through** |
| `test_step9_project_access_bootstrap_for_existing_project` | M.7 | §7.0.7 Step 9 + §4.3（bootstrap owner from cloud_service.workspace_id.access_ids 或 create_uid partner fallback） | §1.3.2（first-class ACL 對既有 project 不能斷層 — migration 是 backfill 機制）| **explicit + implicit-through** |
| `test_post_migrate_is_idempotent` | M.8 | §7.0.2（migration 必 idempotent — `ON CONFLICT DO NOTHING` / `DROP CONSTRAINT IF EXISTS` / `UPDATE ... WHERE ... IS NULL`） | §7.9 risk row（重跑常見 — test runner / 手動 upgrade）| **explicit + implicit-through** |

### 3.7 Cell-map coverage 統計

| Coverage type | Count |
|---|---:|
| **Explicit cell** | 38 |
| **Explicit + implicit-through cell** | 14 |
| **Pure implicit-through cell** | 2 |
| **Regression lock**（今天就綠） | 2 |
| **Total test methods** | **54** |
| **Total AC covered** | **49** |

**implicit-through 集中在哪**：
- **§1.3.2 ACL 兩層**（first-class model 推導鏈）— 多條 N / R 段 test 透過 behaviour 間接覆蓋
- **§2.7.3 NetworkPolicy egress 跨 workspace**（A.2 + A.9 behaviour 是其 schema 前提）
- **§4.7 primary_workspace 計費歸屬** + **§4.8 quota_override**（透過 N.4 field shape 間接驗）
- **§1.3.3 refactor branch 引用**（R 段 walk path M2M migration 是直接落地，但底層守線在此 section）
- **[[two-layer-acl-lockstep]] / [[mcp-acl-6-rule-split]] / [[unbound-project-acl-dual-leg]]**（decision pattern 引用 — R / C 段透過 walk path 間接守住）

---

## 4. 為何測這、為何不測那 — 6 段 group rationale

### A 段（project schema）— 為何測 + 為何不測

**為何測**：M:N 是本 epic per-project pivot 的物理前提。schema 漏掉 = 所有後續（new model / ACL / migration / Phase B-G）全踩穿。A.1（M2M shape）+ A.3（UNIQUE drop）+ A.9（cross-workspace binding）是 schema break change 的三條 acid test — 任何一條 fail = M:N 沒落地。A.4 / A.5（legacy field 保 read + dual-write）是「不 drop column 一個版本」的 deprecation 策略落地驗證，避免外部 caller 突然失能。

**為何沒測**：
- **`task.workspace_id` deprecation 策略** — 留實作 latitude（保 related 還是改 computed M2M，design §7.0.3 留 §7 拍板，本 Phase A 不鎖）；A.2 已 assert `workspace_ids` 是新 canonical store，足夠
- **`agent_template_id` 的 ondelete 策略** — 留 implementation 判斷（set null / restrict / cascade 各有 trade-off，design §4.1 不字面鎖）；A.7 只 assert field shape + comodel_name
- **`use_agent_runtime` 的 toggle 行為** — Phase D 才測；A.8 只 assert default False（flag dormant）

### T 段（task schema）— 為何測 + 為何不測

**為何測**：`task.primary_cloud_service_id` 是現有「跨 workspace task domain」風暴的 epicenter。**Q2 leader sign-off 2026-05-22 hybrid (c)** 拍板「auto-default + user override + drift handler」三件套後，T.5 拆 5 個 method 一一鎖：(a) default first / (b) user override 持久化 / (b) domain scope / (c) drift preserve / (c) drift reset — **少一條 leg 沒鎖 = hybrid 任一面沒落地都會在 Phase D 用戶面看到 orphan primary FK**。

**為何沒測**：
- **`incompatible_extra_cloud_service_ids` computed** — refactor branch 既有 behaviour，不在 Phase A scope（docstring 明示）
- **`task.workspace_id` deprecation 連鎖** — 與 A 段同；留 §7 拍板
- **drift 同時 evict primary + extra** — 設計上 extras 是獨立 set，evict 一邊不蘊含另一邊；不寫合併 case 是 keep test boundary 清楚（避免複合 fixture）

### C 段（cloud_service M:N reverse + cleanup）— 為何測 + 為何不測

**為何測**：M:N 下 cleanup 語意問題（單一 cloud_service unlink 不該整個 project 連坐清 MCP）是 **B1-style 跨 svc collateral leak** 的潛在火源。C.2 / C.5 用 dual-svc fixture（project 綁 svc_A + svc_B，各有 sidecar MCP + 一個 external MCP）驗證「unlink svc_A 只清 svc_A 的 sidecar MCP」— 任一 leg 失準（漏 URL-prefix filter / unlink override 用 loose filter）= refactor branch [[mcp-url-redesign]] Inv #9 在 M:N 場景失守。C.3 鎖 `unlink()` override 與 `_do_cleanup` 對等（[[cloud-service-unlink-override-cleanup]] pattern 的 caller-agnostic safety net）。C.4 external MCP 守線是 refactor branch test 的 M:N 場景延伸（refactor branch 已 lock external 路徑，本 Phase A 鎖「M:N 下 external 仍綠」）。

**為何沒測**：
- **`sidecar_backed=False` external MCP 在 1:N 下的 preservation** — refactor branch `test_mcp_sidecar_backed_lifecycle.py` 已 lock，本 Phase A 不重測
- **Helm uninstall 物理路徑** — 明示 mocked；只驗 ORM filter outcome（unit-test env 無 helm CLI，cleanup 路徑會 raise → docstring 明示 swallow）
- **URL prefix 演算法的字面字串** — 用 `_sidecar_url()` helper mirror production shape；URL 改演算法會在 assertion 全紅，不需單獨 assert prefix 字串

### N 段（4 new models）— 為何測 + 為何不測

**為何測**：4 個新 model 的 schema 對齊 design §x.x 是 Phase B-G 所有 cross-ref 的物理前提。N.1 / N.4 / N.7 / N.9 直接 assert model 在 registry（不在 → `RuntimeError` fail-fast；對 implementer 是 prerequisite signal）。N.2 / N.3 / N.11 三條 `@api.constrains` 是「model body 不能只是 stub」的可執行 lock — 漏一條 constraint = Phase B-G 後續 cross-model assertion 失準。N.5 + N.10 + N.8 是 SQL-level constraint（UNIQUE / IntegrityError 路徑）— 比 model-level constraint 更難用 ORM monkey-patch 繞過。N.6 hash shape 是 K8s namespace 命名的物理前提（hmac-sha256 base32 16 chars 是 80-bit collision bound 的根據）。N.12 是「first-class ACL 對 new project 不能斷層」的 bootstrap acid test。

**為何沒測**：
- **`agent_template` ↔ `project` × `skill` × `mcp_server` 完整 binding 矩陣** — 等 Phase B/C 模型實際 populate 真實 fixture 才有測試價值；Phase A 只覆蓋 constraint method 的 boundary
- **`agent_session.spawn_at` / `last_active` / `reaped_since` 的 cron 流動** — Phase D/E pilot observation 才有 lifecycle event 流動；Phase A 只 assert field shape
- **`skill.bundle_attachment_id` upload pipeline** — Phase B/C controller scope；Phase A 只覆蓋 model schema
- **`project_access.user_id` compute 從 partner_id 推導的 cron 路徑** — N.9 assert `getattr(fld, 'compute', None)` 即可；不模擬 user.partner_id mutation
- **4 個新 model 的 per-rule × per-role 行為矩陣** — 詳見第 6 章；Phase A 只 lock CSV row registration（R.8）

### R 段（ACL dual-leg M2M walk）— 為何測 + 為何不測

**為何測**：R.1 / R.2 改 walk path M2O → M2M 是本 epic 對 mcp_server 的**唯一動作**（[[mcp-url-redesign]] Inv #11 「filter 條件擴維度，不破 dual-leg shape」明文允許）。R.4（outer AND `scope='user'` 守線）內嵌在 R.1 同一 test，是 [[system-mcp-create-acl-bypass]] PR #47 reviewer round 1 抓到的攻擊面 — **任何 leg 2 寫成「對 new row 永真」會立刻打開同一條 privilege escalation**（base.group_user user ORM create `scope='system'` 成功 → 全 instance 可讀）。R.5 / R.6 是 dual-leg shape integration test（leg 1 bound + leg 2 follower carve-out）。R.7 是 **Q1 leader sign-off 2026-05-22 cross-workspace OR semantics 落地** — project 綁 svc_A (W1) + svc_B (W2) → W1 + W2 兩方 member 都可讀，這是 M:N walk 必然推論。R.8 是 4 個新 model 的 ACL CSV gate registration（避免 model 落地但 user 完全打不開 — Odoo 預設無 CSV row = silent deny）。

**為何沒測**：
- **MAJOR 1 system-create guard 完整 regression** — refactor branch `test_mcp_acl_unbound_isolation.py:H.11/H.12` 已 lock；本 Phase A 透過 R.1 / R.4 assertion（outer AND `scope='user'` 守線）做 equivalent 等效鎖，**不重測**
- **admin_full preservation** — refactor branch `test_admin_can_create_system_mcp_via_backend` 已 lock；R.3 只 assert rule 仍存在 + `perm_read=True`，不重測 admin behaviour
- **新 4 model × per-rule × per-role × CRUD 矩陣**（理論 4 × 4 × 4 = 64 case） — Phase A 只覆蓋 CSV row registration（R.8）；ir.rule 的 perm matrix 等模型有真實 fixture（Phase B/C）才測

### M 段（migration）— 為何測 + 為何不測

**為何測**：migration `18.0.1.0.26/` 是 schema break change 的唯一 deploy 路徑。M.1 / M.2 / M.9（檔案存在 + manifest bump）是「migration 會跑」的 sine qua non — 若 manifest 沒 bump，Odoo loader 跳過整個目錄，所有後續 step 都不會執行。M.3-M.7 鎖每 step 的 post-condition：(a) 既有 binding backfill 不丟、(b) UNIQUE 刪掉、(c) legacy column 保留、(d) `external_mcp_allowed` default 撥 FALSE、(e) `project_access` bootstrap 有 owner。M.8 idempotent 是 Odoo `-u woow_paas_platform` 重跑 / test runner 重複呼叫的常見場景 — **不 idempotent = 第二次跑炸 = staging 升版滾雪球**。

**為何沒測**：
- **Step 5（task.workspace_id deprecation）** — 留實作 latitude（docstring 明示）；A.2 已 assert workspace_ids 是新 canonical store
- **Step 7（CREATE TABLE for new models）** — Odoo `--init` 自動處理；不需手寫 SQL，無 test boundary
- **Step 6（cloud_service.project_ids M2M reverse）** — C.1 已直接 assert ORM-level shape；不重測 migration step
- **Step 10（default ai.assistant fallback）** — Phase F destructive cleanup 才動；本 Phase A 明示「保留 fallback，不自動轉 agent_template」（design §7.5）
- **post-migrate rollback SQL** — design §7.9 risk row 列為「git revert + 手動 rollback SQL」；不在 Phase A test scope（rollback 是 emergency runbook 不是 routine flow）

---

## 5. Regression Lock — 今天就 GREEN 的 3 條（reviewer 不要當漏網）

> **reviewer 警示**：以下 3 條 test 在 41 個 cases 跑出來的 2 PASS / 部分 PASS 統計裡屬於 GREEN，是**刻意設計的 regression lock**，不是漏掉的 red。

### Lock #1 — `test_other_four_rules_intact` (R.3)

**位置**：`test_agent_pod_phase_a_acl.py:287`

**行為**：refactor branch 既有 6-rule ACL split 中 4 條不動的 rule（`mcp_server_rule_system_readonly` / `mcp_tool_rule_system_readonly` / `mcp_server_rule_admin_full` / `mcp_tool_rule_admin_full`）必須繼續存在 + `perm_read=True`。

**為何今天 GREEN**：refactor branch（PR #47 + PR #54）已落地這 4 條 rule；本 Phase A walk-path 改動只動 2/6 條（workspace_user × 2 的 dual-leg domain），其他 4 條 design §1.3.3 明示「不動」。

**為何保留**：Phase A implementation 改 2 條 rule 的 domain 時，**故意不准順手 refactor 6-rule split**（例如把 system_readonly 合進 admin_full、或反過來拆更細）。任何 rule 缺失 = 立刻 fail = 違反 [[mcp-acl-6-rule-split]] pattern parity invariant。

### Lock #2 — `test_mcp_acl_rule_count_parity` (R.9)

**位置**：`test_agent_pod_phase_a_acl.py:309`

**行為**：mcp_server + mcp_tool 兩 model 上的 ir.rule 總數 ≥ 6（6-rule split parity）。

**為何今天 GREEN**：refactor branch 6-rule 已落地；本 Phase A 不新增 / 不刪除 rule，只動 2 條的 domain。

**為何保留**：與 Lock #1 對稱 — 防 Phase A walk-path PR 在 diff 中順手把某條 rule 整段刪掉（例如「leg 2 carve-out 在 M:N 下不需要」這種 reasoning 錯誤）。**Phase A 對 ACL 的允許動作限於「leg 內字串改」，不准動 rule 數量**。

### Lock #3 — `test_do_cleanup_unlinks_only_own_sidecar_mcp` 中的 C.4 external MCP leg

**位置**：`test_agent_pod_phase_a_cloud_service_mn.py:303`（同一 test 內最後一條 assertion）

**行為**：dual-fixture 中 `sidecar_backed=False` 的 external MCP 在 svc_A `_do_cleanup` 後**必須仍存在**。

**為何今天 GREEN**：refactor branch [[mcp-url-redesign]] Inv #9 已 lock — `_do_cleanup` filter 已是 `s.scope == 'user' and s.sidecar_backed`；external MCP（`sidecar_backed=False`）已被 filter 拒絕。本 Phase A 加 URL-prefix match 是**更窄**的 filter（從 `True` → `True AND URL-prefix match`），external 路徑仍綠。

**為何保留**：Phase A walk-path PR 改 filter 時**不准順手把 `sidecar_backed` flag 從 filter 拿掉**。任何 implementation 寫成「M:N 下不需要 sidecar_backed 過濾，URL-prefix 已經夠了」的版本，會把 external MCP 一起清掉 → 立刻違反 Inv #9 + Phase A 紅 → 立刻可見。

### 為何這 3 條 lock 重要

這三條與 [[refactor-mcp]] pattern reports §7 的 sidecar-hint 3 條 regression lock 同源 — 都是「**實作做下去之後容易順手破壞**的設計約定」。tester 寫成今天就 GREEN，是因為：

1. **如果只在 implementation 做完才寫**：reviewer 看不出設計意圖、容易被「這條本來就該過」蓋過去
2. **如果寫成今天紅、明天綠**：tester 與 reviewer 會以為實作要主動處理這個 boundary，反而誤導
3. **寫成今天綠、明天綠**：等於明示「這條 boundary 是『fall-through / 既有狀態必須保留』」 — implementation 唯一該做的是**不要主動破壞**

---

## 6. Fail Reason 對齊 Design — 為何 fail = design 故意要求的 new behavior

下表整理 23 條 FAIL 的 fail reason 與 design 章節對應，幫 implementer 對齊「fail 不是 bug，是 design 要求的 new behaviour 還沒寫」。

### A 段 FAIL (8/11)

| Test method | Fail reason | Design 故意要求 |
|---|---|---|
| `test_project_has_cloud_service_ids_m2m_field` | `cloud_service_ids` not in `_fields` | §1.3.1 row 1 — 從 M2O 升 M2M |
| `test_project_has_workspace_ids_computed_m2m` | `workspace_ids` not in `_fields` | §1.3.1 row 3 — 從 related M2O 升 computed M2M |
| `test_unique_cloud_service_id_constraint_removed` | `IntegrityError` on second project | §7.0.2 Step 3 — DROP UNIQUE constraint |
| `test_external_mcp_allowed_field` | `external_mcp_allowed` not in `_fields` | §6.3 — additive admin opt-in gate |
| `test_agent_template_id_field` | `agent_template_id` not in `_fields` | §4.1 — agent_template binding |
| `test_use_agent_runtime_field` | `use_agent_runtime` not in `_fields` | §7.1 Phase A — flag dormant default False |
| `test_legacy_cloud_service_id_reads_first_of_m2m` | Returns raw column, not `cloud_service_ids[:1]` | §7.0.2 Step 4 — getter override |
| `test_write_legacy_cloud_service_id_syncs_m2m` | write() does not propagate to M2M | §7.0.2 Step 4 — write() override dual-write |

### T 段 FAIL (9/9，全紅)

| Test method | Fail reason | Design 故意要求 |
|---|---|---|
| `test_primary_cloud_service_id_not_related` | `related='project_id.cloud_service_id'` still present | §7.0.3 — 改獨立 M2O |
| `test_primary_cloud_service_id_user_writable` | `AccessError` on writing related field | §7.0.3 — 允許 user override |
| `test_extra_cloud_service_ids_domain_no_workspace_clause` | Domain literal `('workspace_id', '=', workspace_id)` still present | §7.0.3 — 拆 same-workspace 限制 |
| `test_extra_cloud_service_ids_allows_cross_workspace` | Cross-workspace addition blocked by stale domain | §7.0.3 |
| `test_primary_default_first_of_m2m_on_create` | Empty default (no computed default) | §7.0.3 hybrid (a) — Q2 SIGNED-OFF |
| `test_primary_user_override_survives_write` | Either related rejects write OR write resets on read | §7.0.3 hybrid (b) |
| `test_primary_domain_limits_to_project_cloud_service_ids` | Domain string absent | §7.0.3 hybrid (b) |
| `test_primary_drift_preserve_when_still_in_new_set` | No drift handler | §7.0.3 hybrid (c) |
| `test_primary_drift_reset_when_evicted_from_new_set` | No drift handler — orphan FK | §7.0.3 hybrid (c) |

### C 段 FAIL (4/4，全紅；C.4 leg PASS — 見 §5 Lock #3)

| Test method | Fail reason | Design 故意要求 |
|---|---|---|
| `test_cloud_service_project_ids_is_many2many` | Type still `one2many` | §1.3.1 row 2 — Many2many reverse |
| `test_two_projects_can_bind_same_cloud_service` | `IntegrityError` from UNIQUE | §1.3.1 row 2 + §7.0.2 Step 3 |
| `test_do_cleanup_unlinks_only_own_sidecar_mcp` | svc_B's sidecar MCP wiped by loose filter | §7.0.4 — URL-prefix match |
| `test_unlink_override_narrows_same_way` | unlink() override uses loose filter | §7.0.4 — caller-agnostic safety net |

### N 段 FAIL (13/13，4 條 ERROR via `setUpClass`)

| Test method | Fail reason | Design 故意要求 |
|---|---|---|
| **`agent_template` 模型** | `RuntimeError` in `setUpClass` — model not in registry | §4.1 — new model |
| **`agent_session` 模型** | `RuntimeError` in `setUpClass` | §2.12 — new model |
| **`skill` 模型** | `RuntimeError` in `setUpClass` | §3.2 — new model |
| **`project_access` 模型** | `RuntimeError` in `setUpClass` | §4.3 — new model |
| N.2 / N.3 / N.5 / N.5b / N.6 / N.8 / N.10 / N.11 / N.12 | depend on model presence；各 constraint / bootstrap 行為 | 對應 §4.1 / §4.3 / §2.12 / §3.2 |

### R 段 FAIL (4/8；R.3 + R.9 PASS — 見 §5 Lock #1+#2；R.5/R.6/R.7 ERROR if A.1 missing)

| Test method | Fail reason | Design 故意要求 |
|---|---|---|
| `test_mcp_server_rule_workspace_user_walks_cloud_service_ids` | Domain still walks `cloud_service_id` (M2O) | §7.0.5 — walk path M2O → M2M |
| `test_mcp_tool_rule_workspace_user_walks_cloud_service_ids` | Same as above, sub-model | §7.0.5 — sub-model parity |
| `test_bound_walk_member_can_read_bound_project_mcp` | SKIP if A.1 missing；否則 W1 walk path 失準 | §7.0.5 leg 1 + §1.3.2 |
| `test_unbound_walk_follower_only` | SKIP if A.1 missing；否則 leg 2 carve-out 失準 | §7.0.5 leg 2 |
| `test_cross_workspace_project_mcp_visible_to_both_workspaces` | SKIP if A.1 missing；否則 M2M OR semantics 失準 | §1.3.2（Q1 SIGNED-OFF）+ §7.0.5 |
| `test_user_group_access_row_present_for_each_new_model` | 4 model 在 csv 無 row | §4.1 / §2.12 / §3.2 / §4.3 — CSV gate |

### M 段 FAIL (9/9，全紅)

| Test method | Fail reason | Design 故意要求 |
|---|---|---|
| `test_pre_migrate_file_exists` | `18.0.1.0.26/pre-migrate.py` 不存在 | §7.0.2 Step 1 |
| `test_post_migrate_file_exists` | `18.0.1.0.26/post-migrate.py` 不存在 | §7.0.2 Step 2-4 + §7.0.7 Step 7-10 |
| `test_manifest_version_bumped` | manifest 仍是 `18.0.1.0.25` | §7.1 Phase A |
| `test_step2_backfill_existing_1to1_binding` | migration 未跑 → relation table 無 row | §7.0.2 Step 2 |
| `test_step3_drops_unique_cloud_service_constraint` | UNIQUE 仍在 | §7.0.2 Step 3 |
| `test_step4_cloud_service_id_column_kept` | migration 未跑（隱含 fail-fast）；落地後 column 應仍存在 | §7.0.2 Step 4 |
| `test_step8_external_mcp_allowed_default_false` | column 不存在；落地後應 default FALSE | §7.0.7 Step 8 |
| `test_step9_project_access_bootstrap_for_existing_project` | SKIP if N.9 missing；落地後應 bootstrap owner | §7.0.7 Step 9 |
| `test_post_migrate_is_idempotent` | migration 未跑；落地後第二次跑必不爆 | §7.0.2（idempotent invariant） |

---

## 7. Q1 / Q2 Leader Sign-off 對應（2026-05-22）

> design §7.10 + §7.0.3 補拍紀錄 — 本 Phase A test scope 對 leader sign-off 的直接落地。

### Q1 — Cross-workspace M:N walk OR semantics（§1.3.2）

> **SIGNED-OFF 2026-05-22**: project 綁 svc_A (W1) + svc_B (W2) 後，user-scope MCP 透過 dual-leg leg 1 walk M2M 時，**兩個 workspace 的 member 都應該能讀**（OR semantics，非單一 workspace 限制）。

**直接對應 test**：`test_cross_workspace_project_mcp_visible_to_both_workspaces` (R.7)

**檢驗角度**：
- Alice (W1 owner) 讀 cross-workspace project mcp → **必須 success**（leg 1 W1 walk）
- Bob (W2 owner) 讀同一 mcp → **也必須 success**（leg 1 W2 walk — Odoo M2M domain 自動 OR）
- 任何 implementation 寫成「只允許一個 workspace」（例如把 cloud_service_ids 改成 cloud_service_ids[:1]）會立即紅

### Q2 — `task.primary_cloud_service_id` hybrid 行為（§7.0.3）

> **SIGNED-OFF 2026-05-22 (Q2 = hybrid (c))**: 三件套：
> - (a) default = `project_id.cloud_service_ids[:1]` (id ASC) — 1-binding project auto-correct
> - (b) user-overridable via task form；domain scope to `project_id.cloud_service_ids`
> - (c) drift handler: 既有 primary 仍 ∈ new set → 保留；否則 → reset to new first

**直接對應 test**（5 method，全紅）：

| Sign-off leg | Test method |
|---|---|
| (a) default first of M2M | `test_primary_default_first_of_m2m_on_create` |
| (b) user override 持久化 | `test_primary_user_override_survives_write` |
| (b) domain scope | `test_primary_domain_limits_to_project_cloud_service_ids` |
| (c) drift preserve | `test_primary_drift_preserve_when_still_in_new_set` |
| (c) drift reset | `test_primary_drift_reset_when_evicted_from_new_set` |

**檢驗角度**：
- Phase A implementer 不可只實作 (a) 跳過 (b)/(c)；不可只實作 (c) 跳過 (b)；缺一條 leg = 對應 test 紅
- design §4.7 `agent_session.primary_workspace_id` 採同 pattern — 之後 Phase 落地時應沿用此 idiom

### 其他 5 條 §7.10 sign-off（不直接對應 Phase A test）

| § | Sign-off | 對應 Phase |
|---|---|---|
| §7.1 (7-phase 切片) | 2026-05-22 | A-G 全 phase（本 report 即 Phase A 落地） |
| §7.2 (cross-repo CI dispatch) | 2026-05-22 | Phase B（image build CI）+ Phase G（Fleet bundle） |
| §7.3 (dual-write window 6-12 週) | 2026-05-22 | Phase D + E + F observation |
| §7.4 (ai_client.py 刪除時機) | 2026-05-22 | Phase F destructive cleanup |
| §7.6 (Fleet bundle dev→stg→prod) | 2026-05-22 | Phase G |

**Phase A 不直接對應**，但本 epic implementation flow 全依賴；本 report 列出供 reviewer 對齊 Phase A → B-G hand-off。

---

## 8. Fixture / 環境隱性假設（reviewer 必看）

不寫 test，但 test 依賴這些條件 — implementer 動 production code 時碰到這些假設要警覺：

1. **`@tagged('post_install', '-at_install')`** — 所有 Phase A test 都標此 tag，addon 載完才跑（new constraint / ACL 已套）。Migration test 用 `importlib.util.spec_from_file_location` 動態 load `18.0.1.0.26/pre-migrate.py` + `post-migrate.py`（dot-separated 不是 Python identifier，必須動態 load）。

2. **Refactor branch invariant guard** — 各檔 `setUpClass` 顯式檢查 `mcp_server.project_id` + `mcp_server.sidecar_backed` 欄位存在；不存在 → `RuntimeError` fail-fast。**這是「PR #54 必須已 merge develop」的可執行斷言**。若 implementer 在 rebase 失準（漏 cherry-pick refactor 變更）→ 整個 Phase A test suite 在 setUpClass 立即停下，紅相 signal 明確。

3. **`cloud_service_ids` prereq SKIP gate** — 多條 T / C / R test 在 setUp 第一行就檢查 `if 'cloud_service_ids' not in self.Project._fields: self.skipTest(...)`。**這是「A 段 schema 先做、其他連鎖測試自然能跑」的依賴管理**；implementer 應先讓 A.1 綠，14 條 SKIP 自動轉 FAIL/PASS 才有真實測試 signal。

4. **`_sidecar_url()` helper mirror production shape** — `test_agent_pod_phase_a_cloud_service_mn.py:62` 的 helper 必須與 `cloud_service._model_build_mcp_endpoint_url` 同型。若 implementer 在 production 改 URL 演算法（例如改 port / 改 path），helper 須同步調整；不調整會讓 C.* 全紅但 fail reason 與 design intent 對不上。

5. **`base32` 字符集 `[a-z2-7]`** — N.6 用 regex `^[a-z2-7]{16}$` 驗 hash shape。implementer 用大寫 base32（`[A-Z2-7]`）會紅；混 base64（`+ /`）也會紅。design §2.7 明示「lower-cased base32」。

6. **`_new_internal_user()` helper** — `test_agent_pod_phase_a_acl.py:79` 建 internal user 自動加 `base.group_user`。**這是 Odoo ACL test 的 idiom — implementer 不必動 helper**，但 Phase B/C 加新 model rule 時應沿用此 idiom 建測試 user。

7. **Migration test 用 `cr.execute` raw SQL** — `test_agent_pod_phase_a_migration.py:207` 直接 `DROP TABLE IF EXISTS project_cloud_service_rel` 模擬 pre-state；這是 migration test idiom（與 [[test-coverage-mcp-project-decouple]] §B 同源），**implementer 改 production migration 不可改變 raw SQL 接口**（必須仍是 `migrate(cr, version)` 純函數）。

8. **`message_subscribe` follower carve-out fixture** — `test_agent_pod_phase_a_acl.py:180` 對 unbound project 顯式 `message_subscribe(partner_ids=...)` 餵 `message_partner_ids` 給 leg 2 carve-out。**leg 2 domain 必須走 `('project_id.message_partner_ids', 'in', [user.partner_id.id])`**；若 implementer 改成走 `project_access` 直接判斷會破 [[unbound-project-acl-dual-leg]] pattern，R.6 立即紅。

---

## 9. 對 reviewer 的提醒（PR diff 對照導讀）

讀 Phase A PR diff 時對照本報告：

### 9.1 `src/models/project_project.py` 改動

- 新 `cloud_service_ids = Many2many(..., relation='project_cloud_service_rel', column1='project_id', column2='cloud_service_id')` → 對 A.1
- 新 `workspace_ids = Many2many(..., compute='_compute_workspace_ids', store=True)` → 對 A.2
- `cloud_service_id` 加 deprecated note，**不**刪除 → 對 A.4 / M.5
- write() override 同步 dual-write → 對 A.5
- 新 `external_mcp_allowed = Boolean(default=False)` → 對 A.6 / M.6
- 新 `agent_template_id = Many2one('woow_paas_platform.agent_template')` → 對 A.7
- 新 `use_agent_runtime = Boolean(default=False)` → 對 A.8
- create() override 內 bootstrap owner project_access row → 對 N.12

### 9.2 `src/models/project_task.py` 改動

- `primary_cloud_service_id`：從 `related='project_id.cloud_service_id'` 改 **獨立 Many2one + computed default** + domain → 對 T.1 / T.2 / T.5 (b) domain
- `extra_cloud_service_ids` domain：從 `[('workspace_id', '=', workspace_id)]` 改 `[('id', 'in', parent.project_id.cloud_service_ids.ids)]` → 對 T.3 / T.4
- write() / create() override 處理 drift handler（preserve OR reset）→ 對 T.5 (c)

### 9.3 `src/models/cloud_service.py` 改動

- `project_ids`：從 `One2many('project.project', 'cloud_service_id')` 改 `Many2many('project.project', relation='project_cloud_service_rel', column1='cloud_service_id', column2='project_id')` → 對 C.1
- `_do_cleanup` filter：加 URL-prefix match `s.url.startswith(f'http://{self.helm_release_name}-mcp.{self.helm_namespace}.svc.cluster.local')` → 對 C.2 / C.5
- `unlink()` override：同 filter（caller-agnostic safety net） → 對 C.3
- **filter `sidecar_backed` flag 不可拿掉**（external MCP 守線；見 §5 Lock #3）→ 對 C.4

### 9.4 `src/models/agent_template.py`（新檔）

- field 列表（name / system_prompt / model_config_id / skill_ids / mcp_server_ids / project_ids / is_public / state）→ 對 N.1
- `_check_visibility_consistency` constraint：private 無 project_ids 不能 publish → 對 N.2
- `_check_mcp_subset` constraint：bound mcp_server_ids ⊆ project ∪ scope='system' → 對 N.3

### 9.5 `src/models/agent_session.py`（新檔）

- field 列表（project_id with ondelete='cascade' / project_namespace_hash / state / helm_release_name / helm_namespace / workspace_ids_snapshot / agent_template_id / spawn_at / last_active / reaped_since / primary_workspace_id / quota_override_cpu / quota_override_memory_gi / quota_override_storage_gi）→ 對 N.4
- UNIQUE(project_id) SQL constraint → 對 N.5
- UNIQUE(project_namespace_hash) SQL constraint → 對 N.5b
- `project_namespace_hash` compute：`hmac_sha256(project_id, secret)[:10]` → base32 lower 16 chars → 對 N.6

### 9.6 `src/models/skill.py`（新檔）

- field 列表（name / version / project_id with ondelete='cascade' / bundle_attachment_id / bundle_sha256 / is_latest / state）→ 對 N.7
- UNIQUE(project_id, name, version) SQL constraint → 對 N.8

### 9.7 `src/models/project_access.py`（新檔）

- field 列表（project_id / partner_id / user_id computed from partner_id / role Selection: owner/admin/user/guest）→ 對 N.9
- UNIQUE(project_id, partner_id) SQL constraint → 對 N.10
- `_check_at_least_one_owner` constraint：last owner unlink raise ValidationError → 對 N.11

### 9.8 `src/security/ir.model.access.csv` 改動

- 加 4 行：`agent_template` / `agent_session` / `skill` / `project_access` 各對 `base.group_user` 至少 `perm_read=True` → 對 R.8

### 9.9 `src/security/ir_rules.xml` 改動

- `mcp_server_rule_workspace_user` domain：leg 1 walk path 從 `project_id.cloud_service_id.workspace_id.access_ids.user_id` 改 `project_id.cloud_service_ids.workspace_id.access_ids.user_id`；leg 2 carve-out 從 `('project_id.cloud_service_id', '=', False)` 改 `('project_id.cloud_service_ids', '=', False)` → 對 R.1 / R.4 / R.5 / R.6 / R.7
- `mcp_tool_rule_workspace_user` domain：同上 + 加 `server_id.` 前綴 → 對 R.2
- **不准動其他 4 條 rule**（system_readonly × 2 + admin_full × 2）→ 對 R.3 / R.9 regression lock（見 §5）

### 9.10 `src/migrations/18.0.1.0.26/`（新目錄）

- `pre-migrate.py` 含 `migrate(cr, version)`：`CREATE TABLE IF NOT EXISTS project_cloud_service_rel ...` → 對 M.1
- `post-migrate.py` 含 `migrate(cr, version)`：6-step + Step 7-10 → 對 M.2 / M.3 / M.4 / M.5 / M.6 / M.7 / M.8
- 全部 SQL 必 idempotent（`ON CONFLICT DO NOTHING` / `DROP CONSTRAINT IF EXISTS` / `UPDATE ... WHERE ... IS NULL`） → 對 M.8

### 9.11 `src/__manifest__.py`

- `version` 從 `18.0.1.0.25` 升 `18.0.1.0.26` → 對 M.9
- `data` 加 4 個新 model 的 view XML + ir_rules.xml 補新 model 4 rule × 4 model = 16 rule

---

## 10. 不在本次 scope（明示與 plan 對齊）

- LangGraph runtime image / `runtime/transport/*` / `runtime/skills/*` → **Phase B**
- paas-operator `/api/agent-runtimes/*` endpoints → **Phase C**
- WS envelope schema / jwt sign+verify / jti replay defense / dual-secret rotation → **Phase B + C**
- Pilot project chat invoke via pod path → **Phase D**
- Gradual rollout / Admin Dashboard widget → **Phase E**
- `ai_client.py` LangGraph 移除 / `discuss_channel.message_post` 同步分支移除 / `langchain_*` external_dependencies 拔 → **Phase F**
- Fleet bundle dev→stg→prod / Prometheus + Grafana alert rules → **Phase G**
- `_check_task_access` / `_list_projects` 跨 tenant 列舉 → **[[mcp-url-redesign]] F4 / F10 follow-up**（refactor branch 已記載）
- 完整 SSRF 加固（DNS rebinding / 內網黑名單） → **follow-up #52**（refactor branch 已記載）
- gVisor / Kata for marketplace skill → **v2 評估**
- Redis jti cache / langchain-mcp-adapters Odoo 端拔 → **v2**

---

## 11. Hand-off

**Audience**:
- **Implementer**：對照 §9（reviewer 對照點）動 production code；每動一處檔，對應 §3 cell-map 的 explicit row 立刻能看到「fail → green」transition。注意 §5 三條 regression lock — 不要順手破壞。
- **Reviewer**：對照本檔 §9 PR diff 導讀；特別注意 §5 「今天就綠」的 3 條 lock 不是漏寫的 red、§7 兩條 leader sign-off (Q1 / Q2) 的直接落地檢驗。
- **CAQA**：對照 §3 implicit-through cell 補 invariants verify list；特別是 §1.3.4 D4 invariant 守線 + [[two-layer-acl-lockstep]] 兩層 ACL lockstep + §2.7.3 NetworkPolicy egress 與 ACL chain 對齊。
- **Stakeholder / future onboarding**：讀 §1 TL;DR + §2 Scope + §7 leader sign-off + §10 「不在本次 scope」 — 5 分鐘理解 Phase A 在驗什麼、什麼留給 Phase B-G。

**Reasoning gap 收尾**（Phase A red phase 後狀態）：

- ✅ Q1 cross-workspace M:N OR semantics → R.7 直接落地
- ✅ Q2 task.primary_cloud_service_id hybrid → T.5 拆 5 method 落地
- ✅ Phase A scope 邊界明示 → §2 + §10 雙路徑（在 scope 列表 + 不在 scope 列表）
- ✅ Regression lock 3 條 → §5 專章 narrative，避免 reviewer 誤判為漏寫的 red
- ⏭ 4 個新 model 的 per-rule × per-role × CRUD 矩陣（理論 64 case）→ 留 Phase B/C test scope expansion，本 Phase A 只覆蓋 CSV row registration（R.8）— **accepted 設計選擇**，視覺上 model 落地時 fixture 才有意義
- ⏭ `task.workspace_id` deprecation 策略 → 留 implementer latitude（design §7.0.3 + plan Step 5 留 §7 拍板），A.2 已 assert `workspace_ids` 是新 canonical store — **accepted 設計選擇**

Phase A red phase 後**無 outstanding reasoning gap**。Coverage 邊界已收攏到 §10 「不在本次 scope」表內列出的「Phase B-G 領域 + follow-up」，每類都有對應的非 Phase A 覆蓋路徑。

---

## See also

- [[agent-runtime-pod-design]] — 本 Phase A 對應 design doc（4794 行，§1-§7 全 signed-off）
- [[agent-runtime-pod-plan]] — 7-phase implementation plan
- [[cloud-service-project-decouple]] — service/project 解耦 D1 invariant（M:N 將 update 字面）
- [[mcp-project-decouple]] — MCP D4 invariant（不動）+ Inv #4 cleanup hook 順序（M:N 延伸）
- [[mcp-url-redesign]] — D5 移除 + sidecar_backed + Inv #9/#10/#11 canonical home（dual-leg shape 不破，walk path M2M）
- [[mcp-acl-6-rule-split]] — 6-rule split parity pattern（R.3 / R.9 regression lock 依據）
- [[unbound-project-acl-dual-leg]] — leg 2 follower carve-out pattern
- [[two-layer-acl-lockstep]] — workspace_access + project_access 兩層 ACL lockstep
- [[sidecar-backed-flag-lifecycle]] — sidecar_backed lifecycle filter pattern
- [[cloud-service-unlink-override-cleanup]] — unlink() override + _do_cleanup caller-agnostic safety net pattern
- [[test-coverage-mcp-project-decouple]] — refactor branch test report pattern reference
- [[test-coverage-mcp-refactor-2026-05-20]] — refactor branch test report pattern reference（含 OR-X9 32-case matrix + regression lock narrative）
- [[cross-workspace-task-data-leak]] — B1 pitfall（sub-model 對等 leak；R.2 sub-model parity 守線根源）
- [[system-mcp-create-acl-bypass]] — PR #47 round 1 抓到的 ACL bypass（R.1 outer AND `scope='user'` 守線根源）
