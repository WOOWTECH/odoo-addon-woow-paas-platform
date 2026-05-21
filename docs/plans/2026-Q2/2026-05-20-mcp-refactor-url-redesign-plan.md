# Plan: MCP user-scope URL 重設計 + sidecar / D5 invariant 重新審視 + unbound project ACL（合 #51）

**Date**: 2026-05-20
**Status**: plan — signed off (2026-05-20)
**Issues**:
- [#49 refactor: 重新審視 sidecar MCP auto-create + D5 invariant + user-scope MCP URL 機制](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/49)
- [#51 unbound project ACL（OR-7 / R-4 partial）](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/51) — **合輪**（OQ-3 拍板 (a)，2026-05-20）
**Companion design**: `docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md`
**Branch (proposed)**: `feature/refactor-user-scope-mcp`（已建）
**Cluster**: F22（OR-2 / OR-4 / OR-5 / R-4） + F23（OR-7 unbound project ACL，合 #51）

> User 拍板 design 推薦的 **B 案**（保留 sidecar + 不自動建 MCP record + UI shortcut）+ OQ-3 (a) 合 #51 一輪。本 plan AC / affected files / test scope 反映合 #51 後的擴充 scope。OR-7 sub-decision 採 (c) message_partner_ids walk（design 「OR-7 三 options brainstorm」段已敘事）。
>
> ai_assistant.py `_check_project_access` unbound carve-out 擴入 = **YES**（OQ-7 user wave 2 拍板，2026-05-20）— AC #42 active；task / list path 留 follow-up（AC #43）。

## Context

承 design doc 拍板 B 案後的具體實作 spec。schema 解耦動作（D5 拔 / `_model_auto_create_mcp_server` 拆 / `_auto_create_mcp_server` 拆 / OR-4 endpoint manual query 拆）+ 新 UI flow（sidecar-hint endpoint + 兩按鈕 McpServersSection）+ migration（`auto_created` rename）。

不可破的 invariants（Design 已敘事）：
- PR #47 Inv #1 `mcp_server.project_id ondelete='cascade'` 不動
- PR #47 Inv #2 D4 不動
- PR #47 Inv #5 ACL 6-rule split — **2/6 條動 dual-leg**（`workspace_user` + `mcp_tool_workspace_user`）；4/6 條（`system_readonly` × 2 + `admin_full` × 2）不動
- PR #47 Inv #6 / #8 不動（與 user-scope 拆綁無關）
- [[cloud-service-project-decouple]] Inv #2 service ↔ project `ondelete='set null'` 不動

新增 invariants（design 已列）：
- **New Inv #9**：sidecar-backed user-scope MCP 在 `_do_cleanup` + `unlink()` override 兩條路徑被清掉，filter 必須包含 `sidecar_backed=True`
- **New Inv #10**：UI shortcut「Add Sidecar MCP」create 時必須帶 `sidecar_backed=True`；endpoint 不接受繞 path
- **New Inv #11**：`workspace_user` 與 `mcp_tool_workspace_user` rule domain 必須是 dual-leg（bound + unbound carve-out via `message_partner_ids`）。任何把 leg 2 改 single leg 或拔 leg 2 = unbound project user-scope MCP 不可見（B 案核心需求破功）；任何把 leg 2 改 always-true / 改其他 walk = ACL leak（B1-class）

## Acceptance criteria

每條對應一個可被 test 抓住的行為。Test 對 AC 的映射在「Test scope」段落。

### Schema / Constraint

1. `mcp_server.py:_check_user_scope_requires_bound_project`（line 127-139）整段刪除。**D5 不再存在於 codebase**。
2. `mcp_server` 新增（或 rename） `sidecar_backed` boolean field（default False，readonly=True，help='MCP record was populated from a sidecar URL hint and is tied to the backing cloud service lifecycle'）。
3. Migration `18.0.1.0.25/post-migrate.py` 把既有 `auto_created=True` 的 row 設成 `sidecar_backed=True`（SQL UPDATE），idempotent。
4. 既有 `auto_created` field 處置（implementation choice，design 偏好）：
   - **a)** Drop field（migration drop column）— 若 rename
   - **b)** Keep field 為 deprecated（不寫入，只讀 — 給 audit）— 若想留向後相容
   - Plan 推薦 **a)** rename；implementation 階段如發現 grep 範圍超過 5 處 cross-module 引用，回退 b)（design Caveat 2）
5. D4 invariant（`_check_scope_project_consistency`，line 111-125）**不動**。
6. `mcp_server.scope='user'` 可掛 unbound project（`project.cloud_service_id IS NULL`）— ORM create / write 不 raise；migration 既有 row 不影響。

### Model auto-create 拆除（cloud_service.py + paas.py）

7. `cloud_service.py:_model_auto_create_mcp_server`（line 970-1013）整段刪除。
8. `cloud_service.py` 內 3 個 call sites 全部刪除：
   - line 849 (`_model_check_release_status` → state='running' 轉換)
   - line 914 (`_model_init_n8n` 成功後)
   - line 940 (`_model_init_odoo` 成功後)
9. `controllers/paas.py:_auto_create_mcp_server`（line 2601-2660）整段刪除。
10. `controllers/paas.py` 內 3 個 call sites 全部刪除：
    - line 2395 (`_check_service_status` → state='running' 轉換)
    - line 2493 (`_run_post_deploy_init` n8n 成功後)
    - line 2560 (`_run_post_deploy_init` odoo 成功後)
11. 一個 cloud service 從 `deploying / initializing` 轉 `running`（or 重新 deploy）**不會**產生新的 `mcp_server` row。
12. n8n template 初始化（owner setup + API key）成功**不會**產生新的 `mcp_server` row。

### URL helper 保留但只給 UI shortcut 用

13. `cloud_service.py:_model_build_mcp_endpoint_url`（line 1015-1036）**保留**（algorithm 不變），但**不再被 model context 內任何方法呼叫**。
14. `controllers/paas.py:_build_mcp_endpoint_url`（line 2662-2696）**改名** `_compute_sidecar_url_hint`（語意 self-documenting），**功能不變**。
15. URL 算法本身（cluster.local fallback + subdomain fallback + localhost fallback）**不變**。

### Sidecar 物理機制保留

16. `cloud_service.py:_build_mcp_sidecar_config`（line 671-735）整段**不動**。
17. `cloud_service.py:478` install-time `client.patch_sidecar(...)` call **不動**。
18. `cloud_app_template.py:120-156` 7 mcp_* fields 全部**不動**。
19. `extra/paas-operator/src/api/releases.py` `patch_sidecar` endpoint + `_create_sidecar_service_and_route` + release teardown sidecar cleanup 全部**不動**。
20. demo data `cloud_app_templates.xml:89-100` n8n template mcp_* 全部**不動**。

### Cleanup hook + unlink override filter 改寫

21. `cloud_service._do_cleanup`（line 1119-1174）step 1 filter 從 `lambda s: s.scope == 'user'` 改為 `lambda s: s.scope == 'user' and s.sidecar_backed`。
22. `cloud_service.unlink()` override（line 1176-1201）filter 同步改寫（同 #21）。
23. 順序不變（[[mcp-project-decouple]] Inv #4 / [[cloud-service-unlink-override-cleanup]] pattern）— filter 內動作仍是 sudo().unlink()。
24. 既有 PR #47 BLOCKER 2 regression test（`test_blocker2_unlink_override_*`）必須繼續 pass — 加 fixture 餵 `sidecar_backed=True` 的 user-scope MCP（與既有測 fixture 對等）。

### Endpoint OR-4 cleanup debt 收

25. `controllers/paas.py:_update_support_project`（line 1504-1556）內 unbind path 的 D5 manual query（line 1528-1547）**整段刪除**。`vals['cloud_service_id'] = False` 直接 write，**不檢查 lingering user-scope MCP**。
26. R-4（model-layer `@api.constrains('cloud_service_id')` on `project.project`）**不補**。本 PR 不新增 project model 的 constraint。

### 新 endpoint：sidecar-hint

27. `controllers/paas.py:/api/support-projects/<int:project_id>/mcp-servers` 端點新增 `action='sidecar-hint'`：
    - 從 `project.cloud_service_id` 找 service；若 service is None 或 `service.template.mcp_enabled=False` → `{success: False, error: 'No sidecar available for this project'}`
    - 呼叫 `self._compute_sidecar_url_hint(service, service.template_id)` 算 URL
    - 回 `{success: True, data: {url, transport, api_key, suggested_name}}` — `transport` 來自 `template.mcp_transport`、`api_key` 來自 `service.mcp_auth_token`、`suggested_name = f"{service.name} MCP"`
28. `action='create'` action（既有 `_create_project_mcp_server`）接受新 payload field `sidecar_backed` (bool, default False)，寫入 mcp_server。**Endpoint 不 enforce「sidecar_backed=True ⇔ URL 是 cluster.local」一致性**（user 可主動填 sidecar_backed=False 但 URL 是 cluster.local，反之亦然；frontend 負責 mental model 一致）。
29. ACL：sidecar-hint action 只給 `access.role in (ROLE_OWNER, ROLE_ADMIN)`（與 create 同層級權限）。non-member / 不存在 project 統一 `Project not found or access denied` 防 enum（與 PR #53 AC #6 / #11 對齊）。

### Minimal SSRF check（design Caveat 3）

30. `_create_project_mcp_server` / `_update_project_mcp_server` 對 user-provided `url` payload，validate：
    - 拒絕 `169.254.169.254`（cloud metadata IP，IPv4）
    - 拒絕 IPv6 link-local（`fe80::/10` 起頭 + AWS IMDSv2 `fd00:ec2::254`）
    - 接受 scheme `http://` 與 `https://`（其餘 scheme 拒絕）
    - 接受 hostname 不做進一步 internal-network filter（cluster.local 必須允許 / 其餘 internal-network filter 留 #52 follow-up）
31. 拒絕時回 `{success: False, error: 'URL host not allowed: <host>'}`。
32. Validation 用 `urllib.parse.urlparse` + `ipaddress` module；無外部 lib dependency。

### Frontend

33. `src/static/src/paas/pages/project-detail/sections/McpServersSection.{js,xml}`：
    - 拆既有「Add MCP Server」按鈕為兩個按鈕：「Add Sidecar MCP」+「Add External MCP」
    - 「Add Sidecar MCP」**僅當** `project.cloud_service_id` 存在 + `project.cloud_service.template.mcp_enabled=True` 時顯示；其餘狀態 hidden
    - 「Add External MCP」永遠顯示（unbound project 也顯示）
    - 「Add Sidecar MCP」點擊流程：呼叫 `sidecar-hint` endpoint → 拿 url / transport / api_key / suggested_name 預填 form modal → user 確認後 endpoint `action=create` 帶 `sidecar_backed=True` commit
    - 「Add External MCP」點擊流程：空白 form modal → user 自己填 → endpoint `action=create` 帶 `sidecar_backed=False` commit
34. `src/static/src/paas/services/support_service.js`：
    - 新增 `fetchSidecarHint(projectId)` method（呼叫 sidecar-hint endpoint）
    - 既有 `createProjectMcpServer(projectId, payload)` 接受新 payload field `sidecar_backed`

### Migration

35. `src/migrations/18.0.1.0.25/post-migrate.py` 新檔，順序：
    1. **Idempotent SQL**：`UPDATE woow_paas_platform_mcp_server SET sidecar_backed = auto_created WHERE auto_created IS TRUE` — backfill 既有 row
    2. （若 implementation 選 a) drop `auto_created` field）：`ALTER TABLE woow_paas_platform_mcp_server DROP COLUMN auto_created` — schema clean up
    3. （否則 b) keep deprecated）：no-op
    4. 既有 PR #47 archived `auto_created=True AND active=False AND project_id IS NULL` 的 orphan row：**保留**現狀（active=False 持續 archive；不主動 unarchive，不主動 delete）
36. Migration **idempotent**：重跑 `-u woow_paas_platform` 不會 raise / 不會重複 backfill / 不會破壞已 archive 的 row。
37. `__manifest__.py` version bump `18.0.1.0.24` → `18.0.1.0.25`。

### ACL domain 改動（合 #51 — OR-7 採 (c) message_partner_ids walk）

38. `src/security/ir_rules.xml` `mcp_server_rule_workspace_user` domain 從 single leg 改 **dual-leg**：

    ```xml
    <!-- 改前（PR #47 落地）-->
    <field name="domain_force">[('scope', '=', 'user'), ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)]</field>

    <!-- 改後（本 PR 合 #51）-->
    <field name="domain_force">['&amp;',
        ('scope', '=', 'user'),
        '|',
        ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
        '&amp;',
        ('project_id.cloud_service_id', '=', False),
        ('project_id.message_partner_ids', 'in', [user.partner_id.id])
    ]</field>
    ```

    Leg 1（bound project，沿用原 walk）+ leg 2（unbound carve-out via message_partner_ids，OR-7 (c) sub-pick）。Perm `1,1,1,1` 不變。

39. `src/security/ir_rules.xml` `mcp_tool_rule_workspace_user` domain 對等改 **dual-leg**（前綴 `server_id.`，sub-model 對等原則 [[mcp-acl-6-rule-split]]）：

    ```xml
    <field name="domain_force">['&amp;',
        ('server_id.scope', '=', 'user'),
        '|',
        ('server_id.project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
        '&amp;',
        ('server_id.project_id.cloud_service_id', '=', False),
        ('server_id.project_id.message_partner_ids', 'in', [user.partner_id.id])
    ]</field>
    ```

40. `src/security/ir_rules.xml` `mcp_server_rule_system_readonly` / `mcp_tool_rule_system_readonly` / `mcp_server_rule_admin_full` / `mcp_tool_rule_admin_full` 4 條 **不動**（PR #47 Inv #6 / #8 仍 enforce；leg-split 已守 system 與 admin direction）。

41. **R-4（model-layer `@api.constrains` on `project.project.cloud_service_id`）不補**。D5 拔了，沒 invariant 可守；OR-2 race 自然消失；OR-4 endpoint manual query 也消失（AC #25 已敘事）。

### ai_assistant.py `_check_project_access` 擴入（OQ-7 拍板 YES，2026-05-20）

42. `src/controllers/ai_assistant.py:_check_project_access`（line 43-61）unbound carve-out 從 `return True` 改：

    ```python
    if not project.cloud_service_id:
        # Unbound project — aligned with ir.rule workspace_user leg 2 (OR-7 (c))
        user_partner = request.env.user.partner_id
        return user_partner in project.message_partner_ids
    ```

    對齊 ir.rule leg 2 — 兩層 ACL 完全一致防 IDOR partial fix。

43. **task path**（`_check_task_access` / F4）+ **list path**（`_list_projects` / F10）本 PR **不擴**（design Out of scope 明文，獨立 attack surface — 留 follow-up）。

### Not-touched 明文（防 implementation 越界）

44. `src/models/discuss_channel.py` AI chat consumer 鏈（`_get_user_mcp_tools`）**不改**（PR #47 已 walk 走 `task.project_id.mcp_server_ids`，不再經 service path；本 PR 拆 D5 不影響此鏈）。
45. `src/controllers/ai_assistant.py:_get_user_mcp_tools_for_channel` 同 #44。
46. `src/views/mcp_server_views.xml` form view **不動**（既有 `project_id` 欄位仍 valid；`sidecar_backed` 加為 readonly invisible 欄位足夠，無需 layout 改）。
47. `src/controllers/ai_assistant.py:_check_task_access`（F4 task path）+ `_list_projects`（F10 list path）**不擴**（AC #43）。

## Affected files (cross-reference design)

Design 已列。Implementation 階段照 design「Affected files」段落 + 本 plan AC 區交叉執行。

### 新增

- `src/migrations/18.0.1.0.25/post-migrate.py`
- `src/tests/test_mcp_d5_removal.py`（AC #6 + 部分 #21 / #22 / #25 regression）
- `src/tests/test_mcp_sidecar_backed_lifecycle.py`（AC #21-24 cleanup hook / unlink override filter）
- `src/tests/test_mcp_sidecar_hint_endpoint.py`（AC #27 / #28 / #29 / #30-32）
- `src/tests/test_mcp_external_url_creation.py`（AC #6 unbound project create + AC #30-32 SSRF）
- `src/tests/test_mcp_acl_unbound_isolation.py`（AC #38-43 ACL dual-leg domain + ai_assistant carve-out — **新檔；合 #51 後追加**）

### 修改

| File | AC |
|---|---|
| `src/models/mcp_server.py` | #1, #2, #4, #5（不動）, #6 |
| `src/models/cloud_service.py` | #7, #8, #13, #21, #22 |
| `src/models/cloud_app_template.py` | #18（不動，only verified） |
| `src/controllers/paas.py` | #9, #10, #14, #25, #27, #28, #30, #31 |
| `src/security/ir_rules.xml` | #38, #39, #40（合 #51 後動的 — 2/6 rule dual-leg + 4/6 不動 verified） |
| `src/controllers/ai_assistant.py` | #42（OQ-7 拍板 YES，2026-05-20） |
| `src/static/src/paas/pages/project-detail/sections/McpServersSection.js` | #33 |
| `src/static/src/paas/pages/project-detail/sections/McpServersSection.xml` | #33 |
| `src/static/src/paas/services/support_service.js` | #34 |
| `src/__manifest__.py` | #37（version bump） |

### Migration

- `src/migrations/18.0.1.0.25/post-migrate.py` — AC #3, #35, #36

## Test scope（給 tester）

### 必加 regression（per AC）

依賴環境：`docker-desktop` k8s context（與 PR #47 / PR #53 同 setup）；不需要實 helm install。

#### A. D5 拔除 + unbound user-scope MCP（`test_mcp_d5_removal.py`）

| Test | AC | 內容 |
|---|---|---|
| `test_user_scope_mcp_can_have_unbound_project` | #6 | 建 unbound project（`cloud_service_id=False`）+ create user-scope MCP（`scope='user'`, `project_id=unbound.id`）— **不 raise** ValidationError |
| `test_d5_constraint_method_no_longer_exists` | #1 | grep `mcp_server.py` 內容，assert `_check_user_scope_requires_bound_project` not in source |
| `test_d4_invariant_still_enforced` | #5 | 既有 `_check_scope_project_consistency` 仍守 D4：scope='user' + project_id=None → raise；scope='system' + project_id 設定 → raise |
| `test_existing_pr47_orphan_archive_row_preserved` | #35.4 | Fixture: 建一個 `active=False, scope='user', project_id=None`（模擬 PR #47 migration 留下的 archived row）；跑 18.0.1.0.25 migration → row 仍 exist + active=False（不 unarchive、不 delete） |

#### B. Sidecar backed lifecycle filter（`test_mcp_sidecar_backed_lifecycle.py`）

| Test | AC | 內容 |
|---|---|---|
| `test_do_cleanup_unlinks_sidecar_backed_only` | #21 | 建 service + bound project + 2 user-scope MCP（一個 `sidecar_backed=True`，一個 `sidecar_backed=False` external URL）；call `service._do_cleanup()`；assert sidecar_backed=True 已 unlink、external 仍 exists |
| `test_unlink_override_unlinks_sidecar_backed_only` | #22 | 同 setup，改 call `service.unlink()`；同 assertion |
| `test_pr47_blocker2_regression_with_sidecar_backed_fixture` | #24 | PR #47 既有 `test_blocker2_unlink_override_cleans_user_mcp_without_do_cleanup` 加 `sidecar_backed=True` fixture，必須繼續綠 |
| `test_pr47_blocker2_regression_does_not_touch_system` | #24 | PR #47 既有 `test_blocker2_unlink_override_skips_system_mcp` 不變（system 仍不應被 cleanup hook 動到） |

#### C. Auto-create 拆除 verification（extend `test_mcp_integration.py`）

| Test | AC | 內容 |
|---|---|---|
| `test_service_state_transition_running_does_not_create_mcp` | #7-#12 | 建 mcp_enabled template service + bound project；mock `_model_check_release_status` cron path 觸發 state='running'；assert 該 project 的 mcp_server_ids 為空 |
| `test_n8n_init_success_does_not_create_mcp` | #8 (line 914) / #10 (line 2493) | mock n8n init 成功；assert 沒新 mcp_server row |
| `test_odoo_init_success_does_not_create_mcp` | #8 (line 940) / #10 (line 2560) | mock odoo init 成功；assert 沒新 mcp_server row |
| `test_old_auto_create_methods_removed` | #7, #9 | grep `cloud_service.py` 與 `paas.py`，assert `_model_auto_create_mcp_server` / `_auto_create_mcp_server` not in source |

#### D. OR-4 endpoint cleanup（extend `test_project_detail_endpoints.py` 既有 file）

| Test | AC | 內容 |
|---|---|---|
| `test_unbind_succeeds_with_user_mcp_present` | #25 | 建 bound project + user-scope MCP（`sidecar_backed=False` external URL）；call `_update_support_project(cloud_service_id=False)`；assert 成功（不 raise D5 violation，不被 manual query 擋） |
| `test_unbind_succeeds_with_sidecar_backed_mcp_present` | #25 | 同上但 `sidecar_backed=True`；assert unbind 成功（**Caveat：sidecar-backed MCP 在 unbind 後 URL 變 cluster.local 但 service 還在 — 不是 zombie，是 hybrid weird state；可由 user 自己刪 MCP 或重新 rebind 修正**） |
| `test_existing_or4_manual_query_path_removed` | #25 | grep `paas.py` `_update_support_project`，assert 「lingering user-scope MCP」query 邏輯已刪 |

#### E. Sidecar-hint endpoint（`test_mcp_sidecar_hint_endpoint.py`）

| Test | AC | 內容 |
|---|---|---|
| `test_sidecar_hint_returns_correct_url_for_bound_n8n_project` | #27 | 建 n8n service + bound project；call endpoint action=sidecar-hint；assert response `{success: True, data: {url: ..., transport: ..., api_key: ..., suggested_name: ...}}` |
| `test_sidecar_hint_fails_for_unbound_project` | #27 | unbound project；call endpoint；assert `{success: False, error: 'No sidecar available for this project'}` |
| `test_sidecar_hint_fails_for_service_without_mcp_enabled` | #27 | bound project + service.template.mcp_enabled=False；call endpoint；同上 error |
| `test_sidecar_hint_acl_non_member` | #29 | Alice 非 workspace member；call endpoint；assert `{success: False, error: 'Project not found or access denied'}` |
| `test_sidecar_hint_acl_member_non_admin` | #29 | Member role；assert `Permission denied`（與 create action 同層級） |
| `test_create_with_sidecar_backed_true_sets_flag` | #28 | call action=create payload 含 `sidecar_backed=True`；assert mcp_server.sidecar_backed=True |
| `test_create_with_sidecar_backed_false_sets_flag` | #28 | 同上 False；assert flag=False |
| `test_create_endpoint_does_not_enforce_url_consistency` | #28 | sidecar_backed=True + URL 是 https://external.com → 寫入成功（**故意不 enforce**，frontend 負責 mental model）；反之亦然 |

#### F. SSRF minimal check（`test_mcp_external_url_creation.py`）

| Test | AC | 內容 |
|---|---|---|
| `test_url_rejects_aws_metadata_ipv4` | #30, #31 | create payload url=`http://169.254.169.254/latest/`；assert `{success: False, error: ...}` |
| `test_url_rejects_ipv6_link_local` | #30, #31 | create payload url=`http://[fe80::1]/`；assert reject |
| `test_url_rejects_imds_ipv6` | #30, #31 | create payload url=`http://[fd00:ec2::254]/`；assert reject |
| `test_url_rejects_unsupported_scheme` | #30, #31 | `ftp://example.com/` / `file:///etc/passwd`；assert reject |
| `test_url_accepts_external_https` | #30 | `https://mcp.example.com/sse`；assert success |
| `test_url_accepts_cluster_local` | #30 | `http://release-mcp.namespace.svc.cluster.local:3000/mcp`；assert success（必須允許，sidecar-backed URL 正是此形態） |

#### G. Migration（extend `test_mcp_project_decouple_migration.py` 或新檔）

| Test | AC | 內容 |
|---|---|---|
| `test_migration_backfills_sidecar_backed_from_auto_created` | #3 | Fixture：建 `auto_created=True` row（直接 SQL bypass ORM）；跑 18.0.1.0.25 migration；assert `sidecar_backed=True` |
| `test_migration_idempotent` | #36 | 連跑兩次 migration；assert 第二次無 error、無重複寫入 |
| `test_migration_preserves_pr47_orphan_archive` | #35.4 | 建 archived orphan row；跑 migration；assert row 仍 exist + active=False（與 A.4 重複，可選擇放任一檔） |

#### H. ACL dual-leg domain + ai_assistant unbound carve-out（`test_mcp_acl_unbound_isolation.py`，**合 #51 新增**）

依賴環境：套 PR #47 `test_mcp_acl_isolation.py` Alice/Bob 雙 workspace 樣板 + extend「unbound project」case。

| Test | AC | 內容 |
|---|---|---|
| `test_owner_can_read_own_unbound_project_external_mcp` | #38, #39, #6 | Alice (W1) 建 unbound project（Alice 為 follower）+ 在該 project 上 create scope='user' MCP（external URL）。`with_user(alice)` `search` 找得到該 mcp_server；`browse(id).read()` 成功 |
| `test_workspace_member_can_read_unbound_project_external_mcp_if_follower` | #38, #39 | Alice 把 Charlie（W1 內 member）加進 project follower（`message_subscribe`）。`with_user(charlie)` 可讀 |
| `test_workspace_member_cannot_read_unbound_project_external_mcp_if_not_follower` | #38, #39 | W1 內 Dave 不是 project follower。`with_user(dave)` `search([('id', '=', mcp.id)])` → 空 recordset；`browse(mcp.id).read()` → raise AccessError |
| `test_cross_workspace_user_cannot_read_unbound_project_external_mcp` | #38, #39 | Bob (W2) 不在 W1 unbound project 的 message_partner_ids 內。`with_user(bob)` `search` → 空；`browse.read` → raise AccessError |
| `test_mcp_tool_unbound_parity` | #39 | mcp_tool 對等 case：Alice unbound project + external MCP + 加 mcp_tool；Dave (non-follower W1) `with_user(dave)` `search([('id', '=', tool.id)])` → 空 |
| `test_mcp_tool_cross_workspace_unbound_isolation` | #39 | Bob (W2) `with_user(bob)` 對 unbound W1 project 內 mcp_tool → 空 / raise AccessError |
| `test_check_project_access_unbound_follower` | #42 | Alice in message_partner_ids → `_check_project_access` 回 True |
| `test_check_project_access_unbound_non_follower` | #42 | Dave not in message_partner_ids → `_check_project_access` 回 False |
| `test_endpoint_get_unbound_project_non_follower` | #42 | Dave 對 unbound project endpoint `action=get` → `{success: False, error: 'Project not found or access denied'}` |
| `test_acl_dual_leg_bound_path_preserved` | #38 | PR #47 既有 9-test regression 全綠（leg 1 對 bound project 行為 unchanged） |

### 既有 regression（必須繼續綠）

- `test_mcp_integration.py` 全 suite（PR #47 留下的）
- `test_mcp_project_lifecycle.py` 8 case + `test_blocker2_*` 兩 case（AC #24 強守）
- `test_mcp_acl_isolation.py` 9 case — **leg 1 對 bound project 行為 unchanged 必須綠**（test H. `test_acl_dual_leg_bound_path_preserved` 額外鎖此）
- `test_mcp_chat_consumption.py`（AI chat 鏈）
- `test_mcp_project_decouple_migration.py`（PR #47 migration）
- `test_project_detail_endpoints.py`（PR #53 endpoint，特別是 unbind path AC 應 surface「D5 violation」case 已 obsolete — 改測「unbind succeeds」）

### 刻意不測

- 不測 SSRF check 的「internal network filter」（cluster.local 必須允許 → 不能 blanket ban internal; 完整 SSRF 守在 #52 follow-up）
- 不測 `_check_task_access` unbound carve-out 行為（AC #44 明文 task path 不擴 — F4 follow-up）
- 不測 `_list_projects` 跨 tenant 列行為（AC #44 明文 list path 不擴 — F10 follow-up）
- 不測 `_build_mcp_sidecar_config` 改動（明文不動）
- 不測 `_build_mcp_endpoint_url` algorithm（明文不動，只是改名 / 改 expose）

### Tester 用 fixture 建議

- 復用 PR #47 `test_mcp_integration.py` 內 n8n template fixture（`mcp_enabled=True`, `mcp_sidecar_image=...`）
- ACL fixture 套 PR #47 `test_mcp_acl_isolation.py` Alice/Bob 雙 workspace 樣板
- `sidecar_backed` flag fixture 建議從 PR #47 `auto_created` fixture migrate（值對應替換）

## Migration path

### Forward（18.0.1.0.24 → 18.0.1.0.25）

1. `__manifest__.py` version bump `18.0.1.0.25`
2. `-u woow_paas_platform` 觸發 `src/migrations/18.0.1.0.25/post-migrate.py`
3. Migration 順序：
   - **(a) Backfill**：`UPDATE woow_paas_platform_mcp_server SET sidecar_backed = TRUE WHERE auto_created = TRUE` — 既有 `auto_created=True` row 全部 mark sidecar_backed=True（保留 PR #47 archived orphan row 的 active=False 狀態 — UPDATE 不會碰 active flag）
   - **(b) Drop `auto_created` column**（若 implementation choice a）：`ALTER TABLE woow_paas_platform_mcp_server DROP COLUMN IF EXISTS auto_created`
   - **(b') 保留 `auto_created` column**（若 implementation choice b deprecated）：no-op
4. ORM load 後：D5 constraint 不再存在（Python 已刪）；新 `sidecar_backed` field 載入。
5. View load 後：mcp_server_views.xml 內 `auto_created` 引用如有（implementation 階段 grep verify）改成 `sidecar_backed` 或移除。

### Rollback

**單一動作**：整包 revert addon 到 18.0.1.0.24（git checkout 上版 tag + `-u`）。**不支援個別 column rollback**（與 PR #47 D3=A 假設一致）。

Rollback 限制：

- 18.0.1.0.25 跑過後，若 implementation 選 a) drop `auto_created`，rollback 後該 column 不存在 → 18.0.1.0.24 程式碼 read `auto_created` field 會 raise `KeyError: 'auto_created'`
- **Workaround**：rollback 流程需先 SQL `ALTER TABLE woow_paas_platform_mcp_server ADD COLUMN auto_created BOOLEAN DEFAULT FALSE; UPDATE woow_paas_platform_mcp_server SET auto_created = sidecar_backed`
- **若 implementation 選 b) keep deprecated**：rollback 無此痛點（column 仍存在）

**建議 implementation 階段先評估 grep `auto_created` 全 codebase + cross-module 影響範圍**：
- 若範圍 < 5 處 + 全在本 module → choose a) rename，簡單
- 若範圍跨 module（不太可能但驗證）→ choose b) keep deprecated

**rollback 假設前提**（與 PR #47 一致）：dev-sandbox + stg only，無正式 prod data；不走兩階段 deprecate。

### 跨 version 對齊（18.0.1.0.23 / 24 / 25）

| Version | 動作 | 與本 PR 關係 |
|---|---|---|
| 18.0.1.0.23 | B1 fix migration（task ACL + project ACL std rule deactivate） | 不動，與本 PR 無交集 |
| 18.0.1.0.24 | PR #47 MCP `cloud_service_id → project_id` decouple + 6-rule split | 不動，本 PR 不 touch ir_rules / migration .24 |
| 18.0.1.0.25 | **本 PR** D5 拔 / auto-create 拆 / sidecar_backed rename | 新增 |

## Open risks

| ID | Risk | 對應 owner |
|---|---|---|
| **OR-X1** | 既有 stg sandbox 既部署 n8n service：本 PR migration 後 `auto_created=True` row 已被 backfill 成 `sidecar_backed=True`，但 service 本身可能在升 18.0.1.0.25 後做 redeploy / state transition → 因 auto-create 拆了，**不會**新建 row。User 預期「我的 n8n 既有 MCP 仍在 → AI chat 仍 work」；驗證重點是「既有 row 行為不變」。 | tester（regression test G） |
| **OR-X2** | n8n template demo data 不動，但 `_model_auto_create_mcp_server` 拆了 → **new** n8n service deploy 後不會 auto-create MCP record（與 PR #47 行為不同）。 user 必須走 UI shortcut「Add Sidecar MCP」才有 MCP。**Onboarding doc / changelog 須明示**。 | reporter phase（doc onboarding flow change） |
| **OR-X3** | Frontend McpServersSection 拆兩按鈕後 UI dispatching logic：「Add Sidecar MCP」按鈕 visibility 依賴 `project.cloud_service.template.mcp_enabled`。Frontend 須拿到此資訊；既有 `_format_project_detail` payload 是否包含 template.mcp_enabled？**Implementation 階段 verify**；若缺，endpoint payload 須擴。 | implementation |
| **OR-X4** | `sidecar_backed` flag 是 user-modifiable 嗎？form view 預設 readonly；但 admin 後台可以改嗎？Test `test_create_endpoint_does_not_enforce_url_consistency` 已 cover endpoint 不 enforce；但 admin 直接 ORM write `sidecar_backed=True` 不會被擋。**Decision**：保留 admin override 能力（PR #47 admin_full rule 允許）；frontend 不暴露 toggle。 | implementation |
| **OR-X5** | URL allowlist 範圍（design Caveat 3 / AC #30）：本 PR 只做 minimal SSRF check（metadata IP + IPv6 link-local + scheme）。完整 SSRF 防禦留 #52 follow-up。**reporter phase 須在 follow-ups.md F25-related entry 標 partial coverage**。 | reporter |
| **OR-X6** | ~~Caveat 1 leader 拍板路徑~~ — **Resolved 2026-05-20**（雙 wave）：OQ-3 拍 (a) 合 #51；OQ-7 拍 ai_assistant.py 擴入 YES（wave 2）；OQ-8 拍 OR-7 leg 2 = (c) message_partner_ids walk（wave 2）。Blast radius 已升級為 ACL MAJOR — 已轉化進 OR-X9 + AC #38-43 + test scope H。**caqa + reviewer 階段須跑 32-case ACL regression matrix**（OR-X9 詳述）。 | resolved |
| **OR-X7** | PR #47 Inv #7「URL 是 snapshot」未動：B 案 sidecar-backed MCP 透過 UI shortcut 寫入時 URL 仍是 snapshot — service rename / namespace 變動後 URL stale。**承襲 PR #47 §Risk #4 caveat**，本 PR 不主動處理。 | future（依 PR #47 invariant） |
| **OR-X8** | `_compute_sidecar_url_hint` 在 controller 內、`_model_build_mcp_endpoint_url` 在 model 內 — 兩個方法重複實作 URL algorithm（PR #47 已有此分裂）。本 PR 不收乾此 duplication（F19 cluster code clarity follow-up 處理）。 | future（F19） |
| **OR-X9** | **ACL domain 改動 blast radius** — workspace_user + mcp_tool_workspace_user 改 dual-leg。Domain 在 4 種 project state（bound / unbound-follower / unbound-non-follower / archived）× 2 sub-models（mcp_server / mcp_tool）× 4 perm direction（R/W/C/U）下行為矩陣 = 32 cases。Any leg 失準（leg 1 OR 永真 / leg 2 OR 永真 / sub-model 對等漏 / perm direction 混淆）= B1-class leak 復活（[[system-mcp-create-acl-bypass]] / [[cross-workspace-task-data-leak]]）。**reviewer 階段必跑全 ACL regression matrix**，特別檢查 perm_create direction（用 leg 2 對 new row 為真會否 privilege escalation）。本 plan AC #38 / #39 用 `'&', leg, leg'` 寫法限制 leg 之間用 AND 連接 scope='user'，每條 leg 自己 OR — domain shape 與既有 dual-leg pattern 一致。Tester 必加 case 守 leg 2 對 new system-scope row 是否會永真 problem（同 MAJOR 1 思路） | caqa + reviewer |

## Implementation phasing

預期 phase 順序（由 phase-leader 在 recruit 後啟動）：

1. **planner**（本 phase）— 雙檔 sign-off（含 OQ-1 / OQ-2 / OQ-3 / OQ-4 / OQ-5 / OQ-6 與使用者 / leader 對焦）
2. **tester (TDD red)** — 寫 test suite A/B/C/D/E/F/G + extend 既有 regression（AC 對應全 cover）
3. **test-explainer** — test report
4. **implementation (TDD green)** —
   - Schema clean up（`mcp_server.py` D5 拔 / `auto_created` rename / `sidecar_backed` add）
   - `cloud_service.py` auto-create 拆 + filter 改寫
   - `controllers/paas.py` auto-create 拆 + OR-4 cleanup + sidecar-hint endpoint + SSRF check
   - Migration `18.0.1.0.25/post-migrate.py`
   - Frontend McpServersSection 拆兩按鈕 + support_service.js sidecar-hint method
5. **caqa** — invariants verify（PR #47 8 條 + 新加 #9 / #10 / #11 + Inv #5 update）；OR-X1~X9 risk 過一遍；**special attention to OR-X9 ACL dual-leg 32-case regression matrix**（4 project state × 2 sub-models × 4 perm direction）
6. **reviewer** — `/pr-review-toolkit:review-pr`（多 agent 平行 review；**特別關注 OR-X9 ACL dual-leg domain blast radius — 5-agent split：code-reviewer 看 domain shape / pr-test-analyzer 跑 32-case matrix 覆蓋 / silent-failure-hunter 看 ai_assistant carve-out 是否 silently 對 task path 漏掉 / type-design-analyzer 評估 dual-leg shape vs `'|', leg1, '&', leg2_a, leg2_b` AND/OR precedence / comment-analyzer 對 [[mcp-acl-6-rule-split]] doc 是否需要 update**）
7. **reporter** — durable docs：
   - 更新 [[mcp-project-decouple]] §Invariants：D5（Inv #3）刪除 → 寫 deprecation note 留考古；新增 Inv #9 / #10 / #11；Inv #5 update（workspace_user + mcp_tool_workspace_user 改 dual-leg + 改寫 ACL 對等性 invariant 條目反映 4/6 不動 + 2/6 dual-leg）
   - 新增 `docs/decisions/patterns/sidecar-backed-mcp-lifecycle.md`（New Inv #9 / #10 抽 pattern，若被未來 refactor 借鑑）
   - 新增 `docs/decisions/patterns/unbound-project-acl-dual-leg.md`（ACL dual-leg domain pattern，OR-7 (c) 落地，與 [[cloud-service-project-decouple]] D3=A 既有 idiom 對齊，供未來 ACL refactor 借鑑）
   - 更新 [[follow-ups]] **F22 + F23 status → resolved**（#49 + #51 共 PR 同時收）；surface OR-X5 partial SSRF coverage 進 F25 cluster；**F4 partial 標註**（project path 已 fix，task path / `_list_projects` 仍 leak — 留 follow-up issue）
   - **同時 close issue #49 + issue #51**（gitea：comment 包含本 PR link + plan + design link）
   - PR description draft（必含 ACL OR-X9 blast radius 摘要 + AC #42 ai_assistant carve-out 已 fix project path / task+list path follow-up 標記）

## Hand-off 給 tester

實作前先讀：

- Companion design：`docs/plans/2026-Q2/2026-05-20-mcp-refactor-url-redesign-design.md`（拍板的 B 案完整 tradeoff + invariant 改動敘事）
- PR #47 invariants：`docs/decisions/refactor/mcp-project-decouple.md` §Invariants（特別是 #4 順序 / #5 6-rule / #7 URL snapshot）
- Pattern reference：
  - `docs/decisions/patterns/cloud-service-unlink-override-cleanup.md`（AC #21-24 cleanup filter 改寫沿用 pattern）
  - `docs/decisions/patterns/mcp-acl-6-rule-split.md`（合 #51 後 ACL domain 改 dual-leg 對等性原則 — AC #38 / #39 落地直接套用此 pattern）
  - `docs/decisions/refactor/cloud-service-project-decouple.md` §Q1（既有 ir.rule `message_partner_ids` walk idiom — AC #38 leg 2 與 D3=A 相同設計選擇）
- B1 pitfall: `docs/tracking/pitfalls/cross-workspace-task-data-leak.md`（任何 ACL 改動參照）
- F22 cluster：`docs/tracking/followups/follow-ups.md` F22

新增 test 文件（6 個 — 合 #51 後 +1）：
- `src/tests/test_mcp_d5_removal.py`
- `src/tests/test_mcp_sidecar_backed_lifecycle.py`
- `src/tests/test_mcp_sidecar_hint_endpoint.py`
- `src/tests/test_mcp_external_url_creation.py`
- `src/tests/test_mcp_acl_unbound_isolation.py`（**合 #51 後新增**，AC #38-43 ACL dual-leg domain + ai_assistant carve-out）
- 既有 `test_mcp_integration.py` + `test_mcp_project_lifecycle.py` + `test_mcp_acl_isolation.py` + `test_project_detail_endpoints.py` extend

Test 對 AC 的映射在「Test scope」段。**ACL 改動是 MAJOR 級 blast radius**（OR-X9），tester 須 special attention：
- AC #38 / #39 dual-leg domain 在 4 project state × 2 sub-models × 4 perm direction = 32 cases 覆蓋（不需要 32 個 test method，但每條 test 須明示 cover 哪個 state × direction）
- 既有 `test_mcp_acl_isolation.py` 9 case 對 bound project 行為**不能 break**（test H `test_acl_dual_leg_bound_path_preserved` 是 explicit 守線）
- AC #42 ai_assistant carve-out 拍板 YES（OQ-7 wave 2）— 新增 3 條 ai_assistant test cover「ir.rule + controller 兩層對齊」

## Sign-off log

| Wave | Date | OQ | 拍板者 | 拍板結論 |
|---|---|---|---|---|
| 1 | 2026-05-20 | OQ-1（URL 三方案 A/B/C） | user | **B**（保留 sidecar + UI shortcut + 不自動建 MCP record） |
| 1 | 2026-05-20 | OQ-3（Caveat 1 ACL domain 處理） | user | **(a) 合 #51 一輪**（本 PR + #51 共 PR / 共 review） |
| 1 | 2026-05-20 | OQ-5（既有 auto_created records migration） | user | **(c) 保留 + rename flag** 為 `sidecar_backed` |
| 1 | 2026-05-20 | OQ-6（UI 拆兩按鈕 vs 統一 toggle） | user | **拆兩按鈕**（Add Sidecar MCP / Add External MCP 並列） |
| 1 | 2026-05-20 | OQ-2（auto_created rename 細節） | leader | implementation 階段判斷（design 偏好 rename，grep > 5 cross-module 回退 deprecated） |
| 1 | 2026-05-20 | OQ-4（URL allowlist 範圍） | leader | implementation 階段判斷（design 偏好 minimal SSRF check：cloud metadata IP + IPv6 link-local + scheme allowlist） |
| 2 | 2026-05-20 | OQ-7（ai_assistant.py `_check_project_access` 擴入） | user | **YES**（minimal 擴入 project path 一個 method；task / list path 留 follow-up） |
| 2 | 2026-05-20 | OQ-8（OR-7 unbound ACL leg 2 三 options） | user | **(c) message_partner_ids walk**（與 [[cloud-service-project-decouple]] D3=A 既有 idiom 對齊） |

**Plan + design 雙 wave 全 sign-off。Tester 可開動全 AC（含 #42 ai_assistant carve-out）。**
