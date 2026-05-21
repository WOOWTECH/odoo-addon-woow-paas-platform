# PR Description Draft — `refactor(mcp): drop D5 invariant + retire sidecar auto-create + redesign user-scope MCP URL`

> Title 為英文（git commit convention）。本檔內容（下方 PR body）為 zh-tw 繁體中文 — 直接 paste 到 PR description 即可。

**Title**: `refactor(mcp): drop D5 invariant + retire sidecar auto-create + redesign user-scope MCP URL`

**Branch**: `feature/refactor-user-scope-mcp`
**Closes**: #49 + #51
**Cycle**: 4（continuation of cycle 2 [[mcp-project-decouple]] schema decouple）

---

## Summary

本 PR 把 user-scope MCP 從「物理層仍綁 `cloud_service` 的 sidecar」變成「schema 與 ACL 兩平面完全 decouple」— 拔 D5 invariant、拆 cloud_service 與 controller 兩路的 sidecar auto-create、保留 sidecar 物理機制但改 user opt-in（UI 拆「Add Sidecar MCP」+「Add External MCP」兩按鈕）、新增 `sidecar_backed` boolean field 區分 row 物理依賴關係、cleanup hook filter 升級對 sidecar-backed only、`workspace_user` × 2 條 ir.rule 改 dual-leg domain 給 unbound project + follower 可讀 external MCP、`ai_assistant._check_project_access` controller layer 對齊 ir.rule leg 2 follower check 防 IDOR partial fix、加 minimal SSRF guard 拒 cloud metadata IPv4 + IPv6 link-local + 非 http(s) scheme。**合 issue #49 (F22 cluster) + #51 (F23 unbound project ACL)** 一輪。

## Scope

| Issue | 對應 cluster | Status |
|-------|-------------|--------|
| **[#49](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/49)** | F22 — D5 invariant 重設計 cluster (OR-2 rebind race + OR-4 endpoint manual D5 query + OR-5 sidecar auto-create + R-4 ORM constraint) | ✅ closed by this PR |
| **[#51](https://git-prod.woowtech.io/odoo-addons/woow_paas_platform/issues/51)** | F23 — Unbound project ACL spec gap (OR-7 / R-4 partial) | ✅ closed by this PR（合 #49） |

OQ-3 拍板 (a) 合輪：本 PR 與 #51 共 PR / 共 review；OQ-7 拍板 YES 擴入 `ai_assistant.py:_check_project_access` 對齊；OQ-8 拍板 (c) message_partner_ids walk。

## 動作摘要（44 AC 群組）

| 群組 | AC # | 動作 |
|------|------|------|
| **Schema / Constraint** | #1-#6 | D5 整段刪 / `sidecar_backed` 新 boolean field（default False, readonly）/ `auto_created` field 保留為 deprecated（OQ-2 implementation 拍板 (b)） |
| **Auto-create 兩路拆** | #7-#12 | `cloud_service._model_auto_create_mcp_server` + 3 call sites 刪 / `paas._auto_create_mcp_server` + 3 call sites 刪 |
| **URL helper 改投** | #13-#15 | `_model_build_mcp_endpoint_url` 保留為 cron / non-request helper / `_build_mcp_endpoint_url` 改名 `_compute_sidecar_url_hint`（給新 sidecar-hint endpoint） |
| **Sidecar 物理保留** | #16-#20 | `_build_mcp_sidecar_config` / `client.patch_sidecar` / `cloud_app_template` 7 mcp_* fields / paas-operator / demo data 全不動 |
| **Cleanup hook filter 升級** | #21-#24 | `_do_cleanup` step 1 + `unlink()` override 兩條 filter 從 `scope=='user'` 改 `scope=='user' AND sidecar_backed`；PR #47 既有 9 case fixture 條件式餵 `sidecar_backed=True` 維持全綠 |
| **OR-4 endpoint cleanup debt 清** | #25-#26 | `_update_support_project` unbind path D5 manual query 刪；R-4 ORM constraint 不補 |
| **新 sidecar-hint endpoint** | #27-#29 | `POST /api/support-projects/<id>/mcp-servers action=sidecar-hint` 給 UI shortcut 預填；套同 owner/admin gate + 統一 `Project not found or access denied` 防 id enumeration |
| **Minimal SSRF check** | #30-#32 | `_validate_mcp_url` 拒 `169.254.169.254` / IPv6 link-local / `fd00:ec2::254` / 非 http(s) scheme；保留 cluster.local；stdlib only |
| **Frontend 拆兩按鈕** | #33-#34 | `McpServersSection` 「Add Sidecar MCP」（僅 bound service + `template.mcp_enabled=True` 時顯示）+「Add External MCP」（永遠顯示） |
| **Migration `18.0.1.0.25`** | #3, #35-#37 | Idempotent SQL backfill `auto_created → sidecar_backed`；orphan archive row 保留現狀；version bump |
| **ACL dual-leg（合 #51）** | #38-#41 | `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` 改 dual-leg（leg 1 bound walk + leg 2 unbound carve-out via `message_partner_ids`）；其餘 4/6 條不動 |
| **ai_assistant 兩層對齊** | #42 | `_check_project_access` unbound carve-out 從 `return True` 改 follower check（與 ir.rule leg 2 對齊） |
| **明文不擴** | #43-#44 | `_check_task_access` (F4) / `_list_projects` (F10) / `discuss_channel.py` AI chat / `mcp_server_views.xml` 主 layout — 不動 |

## ACL OR-X9 blast radius 摘要（reviewer special attention）

`workspace_user` × 2 改 dual-leg 是本 PR 最高 blast radius — domain 在 4 project state × 2 sub-models × 4 perm direction = 32 conceptual cells。實作 `test_mcp_acl_unbound_isolation.py` 12 method 對應 self-document method name（cell map 詳見 CAQA report §2）；HC-3 round 2 補加 H.3b / H.4b + cross-ws W/U 4 explicit test 鎖 perm direction matrix。

**MAJOR 1 重炸防線**：dual-leg domain 外層 AND `('scope', '=', 'user')` 是核心守線 — 任何把外層 AND 拆掉、把 leg 2 改 always-true、漏改 mcp_tool sub-model parity = 重炸 [[system-mcp-create-acl-bypass]] MAJOR 1。**H.11 / H.12 explicit reproducer**（Charlie ORM `create({'scope': 'system'})` + sub-model 對等 → 必 `AccessError`）— 後續任何 PR 動 dual-leg shape 立刻 trip。

## AC #42 ai_assistant carve-out 已 fix project path / task+list path follow-up 標記

| 修法 | Surface | Status |
|------|---------|--------|
| `ai_assistant._check_project_access` (project path) | 從 `return True` 改 `return user_partner in project.message_partner_ids` | ✅ fix (AC #42) |
| `ai_assistant._check_task_access` (task path) | F4 — 仍 leaky；plan AC #43 明文 out of scope | ⏭ F-X2 cluster follow-up |
| `ai_assistant._list_projects` (list path) | F10 — sudo + empty domain leak；plan AC #43 明文 out of scope | ⏭ F-X2 cluster follow-up |
| `paas._resolve_project_access` (paas.py controller layer mirror) | HC-2 hotfix round 2 — follower check 對齊 + `_SyntheticAccess` docstring「lock-step」承諾 | ✅ closed in PR round 2 |

## Test plan

### Test 結果

```
569 tests / 8 failed / 28 errors = 36 baseline 紅持平 / 0 MCP red / 0 HC red
```

- **44 / 44 AC 全綠**（plan AC #1-#42；#43/#44 是「不做」型，本就不該有 test）
- **6 個新 test files + 4 existing test files extend**：
  - `test_mcp_d5_removal.py` — D5 拔除 + unbound user-scope MCP（A 段 4 test）
  - `test_mcp_sidecar_backed_lifecycle.py` — sidecar-backed lifecycle filter（B 段 4 test paired fixture）
  - `test_mcp_sidecar_hint_endpoint.py` — sidecar-hint endpoint（E 段 9 test，含 3 條今天就 GREEN 的 regression lock 鎖「設計刻意不 enforce」邊界）
  - `test_mcp_external_url_creation.py` — SSRF minimal check（F 段 6 test + HC-1 round 2 補 4 test `TestLegacySsrfCheckOnMcpUrlCreate`）
  - `test_mcp_acl_unbound_isolation.py` — ACL dual-leg + ai_assistant carve-out（H 段 12 method matrix + HC-2 round 2 補 3 test `TestPaasEndpointUnboundNonFollower` + HC-3 round 2 補 4 test perm direction）
  - `test_mcp_project_decouple_migration_25.py` — migration 18.0.1.0.25 backfill + idempotent（G 段 3 test）
- **PR #47 既有 ACL regression 9 test 全綠**（leg 1 bound 路徑 unchanged）
- **PR #47 BLOCKER 2 cleanup hook 既有 9 case** — fixture 條件式餵 `sidecar_backed=True` 維持全綠
- **3 條今天就 GREEN 的 regression lock 設計強度 STRONG**（`assertEqual` 嚴格鎖 enum / `assertRaises` 緊縮 scope）— design intent 鎖「URL ↔ flag 一致性 endpoint 故意不 enforce」+「sidecar-hint non-member 統一 `Project not found or access denied` 防 id enumeration」

### 36 pre-existing 紅 out of scope

`git stash → re-run baseline → 同 reds reproduce` 驗證方法學健全（cycle 2 F13 pattern 對齊）：

- 28 errors + 8 failed = 36 reds
- 多為 subscription journal / TWD currency / smart_home demo user / B1 task ACL deactivation timing（cycle 2 F13 cluster 已 surface）
- **本 PR diff 0 改動這些 file → 無 blast radius**

驗證指令：

```bash
./.claude/skills/k8s-dev-sandbox/scripts/test.sh feature-refactor-user-scope-mcp --module woow_paas_platform
```

## Caveats（hard / soft）

### Hard caveats（已 closed in PR round 2）

| Caveat | 根因 | Closure |
|--------|------|---------|
| **HC-1** Legacy `_create_mcp_server` / `_update_mcp_server` SSRF guard 漏裝 | plan AC #30 only spec 新端點；legacy service-centric endpoint same-source code path 沒列 | **closed**：`paas.py:1353` + `:1387-1390` 兩處加 `_validate_mcp_url` call（validate-then-create order） + 4 test `TestLegacySsrfCheckOnMcpUrlCreate` 鎖 |
| **HC-2** `paas.py:_resolve_project_access` 對 unbound project blanket `_SyntheticAccess(ROLE_OWNER)` | 本 PR 收緊 ai_assistant.py 而 paas.py 同 unbound carve-out surface 沒同步 → silent IDOR divergence | **closed**：`paas.py:1495-1519` 加 follower check + `_SyntheticAccess` docstring 「lock-step」承諾 + 3 test `TestPaasEndpointUnboundNonFollower` 鎖 |
| **HC-3** OR-X9 32-case write/unlink direction 對 unbound implicit-only | H.1-H.6 只測 read direction；UF/UN × S × W/U 沒 explicit assertion | **closed**：`test_mcp_acl_unbound_isolation.py:438-526` 加 4 explicit test（Dave / Bob × write / unlink）+ `assertRaises(AccessError)` 緊縮 scope |

### Soft caveats（reporter phase 收尾 / follow-up）

CAQA 4 caveat + reviewer 20 SC + round 2 1 new MINOR (SC-6b) = 共 25 條 — 全部分配如下：

| 嚴重度 | 內容 | 落點 |
|-------|------|------|
| Medium | M1 cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap | F-X1 dedicated PR |
| Medium | M2 `_check_project_access` bound-but-no-workspace blanket allow + SC-6b paas.py 同邊 drift + F4 task path + F10 list path 整理 | F-X2 cluster PR with F4 / F10 |
| Low | L1 codebase-wide comment ref noise debt（4 處 PR47/53 殘留）| F-X3 reporter / batch |
| Low | L2 URL helper duplication `_compute_sidecar_url_hint` vs `_model_build_mcp_endpoint_url`（含 SC-13 dead code 評估）| F-X4 follow-up |
| Low | L3 SSRF error format IPv4 vs IPv6 微不一致 | F-X5 future PR |
| Trivial | L4 `test_mcp_d5_removal.py` D5 stale comment + SC-12 stale module docstring | F-X6 reporter / batch |
| Medium | OQ-2 (b) keep deprecated `auto_created` — drop column 等 F-X1 完成 | F-X7 future PR |
| Low | tester surface `scripts/test-addon.sh` worktree env gap | F-X8 dev script PR |
| Minor | SC-14 (`write_actions` naming) | future PR rename |
| Minor | SC-15~SC-17 test coverage gaps | batch with cycle 1 F8 + cycle 2 F17 + cycle 3 F27 |
| Minor | SC-18 SSRF empty host / port 0 / userinfo edge case | #52 / F25 follow-up |

Doc updates by reporter phase（**已完成於本 PR commit chain**）：

- **新 decision doc**：`docs/decisions/refactor/mcp-url-redesign.md`
- **更新既有 decision doc**：`docs/decisions/refactor/mcp-project-decouple.md` Inv #3/#4/#5/#7 + Cycle 4 cross-reference
- **新 pattern docs**：
  - `docs/decisions/patterns/two-layer-acl-lockstep.md`
  - `docs/decisions/patterns/unbound-project-acl-dual-leg.md`
  - `docs/decisions/patterns/sidecar-backed-flag-lifecycle.md`
- **更新既有 pattern doc**：`docs/decisions/patterns/mcp-acl-6-rule-split.md` 升級為 dual-leg variant（SC-1）
- **新 pitfall docs**：
  - `docs/tracking/pitfalls/two-layer-acl-asymmetric-deny.md`
  - `docs/tracking/pitfalls/legacy-endpoint-scope-blindness.md`
- **更新 follow-ups**：`docs/tracking/followups/follow-ups.md` — F22/F23 RESOLVED + F-X1~F-X8 新增 + SC-cluster batch

## Open follow-ups（給未來 PR 作者）

| F# | Severity | 主題 |
|----|----------|------|
| F-X1 | medium | Cron `_cron_retry_mcp_sync` 對新 sidecar-backed row gap — 改 query `sidecar_backed` 或廢除 cron |
| F-X2 | medium | `ai_assistant.py` controller 整體 IDOR cleanup — bound-no-workspace + F4 task path + F10 list path |
| F-X3 | low | Codebase-wide comment ref noise cleanup（PR #47 / PR #53 殘留）|
| F-X4 | low | URL helper duplication 收乾（含 SC-13 dead code 評估）|
| F-X5 | low | SSRF error message IPv4 vs IPv6 統一格式 |
| F-X6 | trivial | test docstring stale cleanup（D5 reference 移除）|
| F-X7 | medium | Drop deprecated `auto_created` column（依賴 F-X1 先完成）|
| F-X8 | low | `scripts/test-addon.sh` worktree env setup gap |

詳細 [[follow-ups]] §Cycle 4。

## ⚠️ 不可逆 / 半可逆動作清單

| 動作 | 可逆性 | 緩解 |
|------|-------|------|
| D5 constraint method 整段刪 | **可逆**（git revert）| 無 DB-level constraint 對應 |
| Migration `18.0.1.0.25` SQL backfill | **可逆**（idempotent；反向 SQL `UPDATE SET sidecar_backed=FALSE WHERE auto_created=TRUE` 即可）| 既有 row 行為不變 |
| Auto-create 兩 method 整段刪 | **半可逆**（git revert 後既有 sidecar-backed records functional preserved；但 new n8n service deploy 後不會 auto-create — user 必須走 UI shortcut）| Onboarding doc 已 surface（OR-X2 已轉成 reporter follow-up） |
| `workspace_user` × 2 ACL 改 dual-leg | **可逆**（XML revert + module -u 套用）| ir.rule noupdate=0 包裝 |
| `_check_project_access` unbound carve-out 改 follower check | **可逆**（git revert）| revert 後 IDOR surface 復活，需評估 attack vector |

**無嚴格不可逆動作。**

## Side effects 評估

| 影響面 | 描述 |
|--------|------|
| **既有 stg sandbox n8n service** | `auto_created=True` row migration 後 `sidecar_backed=True`；service 仍在 → URL 仍 valid → AI chat 仍 work（行為**不變**） |
| **新 n8n service deploy** | 不再自動建 user-scope MCP record；user 必須點「Add Sidecar MCP」按鈕（一鍵 add + 預填）— **onboarding flow 改變** |
| **既有 user-managed external MCP**（如有）| 不受影響（service 拆時 cleanup filter 不會誤殺）|
| **ACL 對 unbound project + non-follower internal user** | 從 blanket allow 收緊為「Project not found or access denied」— security 收緊，可能影響 internal admin / dev workflow（如有依賴 unbound access 的 cron / script，需明示走 sudo） |
| **paas-operator side / helm chart / Cloudflare route** | 完全不動 — sidecar 物理機制保留 |
| **frontend `cloud_service.js` legacy endpoint** | 仍主動使用 `_create_mcp_server` / `_update_mcp_server`（HC-1 已加 SSRF guard） |

## 部署檢查

### 升級前

無 pre-deploy SQL check（cycle 2 已 setup migration 18.0.1.0.24 path；本 cycle 是其上的 incremental refactor）。

### 升級後驗證

```sql
-- 1. sidecar_backed field 已 backfill
SELECT
    COUNT(*) AS total_user_mcp,
    SUM(CASE WHEN sidecar_backed THEN 1 ELSE 0 END) AS sidecar_backed_count,
    SUM(CASE WHEN auto_created AND NOT sidecar_backed THEN 1 ELSE 0 END) AS auto_created_no_sidecar_backed
FROM woow_paas_platform_mcp_server
WHERE scope = 'user';
-- expect: auto_created_no_sidecar_backed = 0（migration backfill 已 cover 全部）

-- 2. D5 constraint 已不存在於 Python（不會出現在 DB constraint）
-- 直接 grep source: assert NOT in src/models/mcp_server.py 內 `_check_user_scope_requires_bound_project`

-- 3. ir.rule dual-leg 已套用
SELECT id, name, domain_force
FROM ir_rule
WHERE id IN (
    SELECT res_id FROM ir_model_data
    WHERE module='woow_paas_platform'
      AND name IN ('mcp_server_rule_workspace_user',
                   'mcp_tool_rule_workspace_user')
);
-- expect: 2 rows，domain_force 內含 'message_partner_ids' substring
```

### Rollback（emergency）

整包退版到 18.0.1.0.24：

```bash
# 1. revert merge commit
# 2. git checkout v18.0.1.0.24
# 3. -u woow_paas_platform
```

Rollback 後既有 `sidecar_backed=TRUE` row 不會被使用（程式碼不 reference），functional preserve；D5 constraint 由 `_check_user_scope_requires_bound_project` 恢復；ACL 退回 single-leg。

## 主要 commits

> 由 git commit-generator phase 在 release 前填入。預期 commit chain：

1. `<sha>` test: TDD red 6 new test files + extend 4 existing
2. `<sha>` refactor(mcp): drop D5 invariant + auto-create teardown
3. `<sha>` feat(mcp): sidecar_backed field + cleanup filter rewrite
4. `<sha>` feat(mcp): sidecar-hint endpoint + URL helper rename
5. `<sha>` feat(mcp): minimal SSRF check `_validate_mcp_url`
6. `<sha>` feat(security): ACL dual-leg domain + ai_assistant carve-out
7. `<sha>` feat(mcp): migration 18.0.1.0.25 backfill
8. `<sha>` feat(frontend): McpServersSection 拆兩按鈕 + sidecar flow
9. `<sha>` fix(security): HC-1 legacy SSRF guard + HC-2 paas.py lock-step + HC-3 perm direction test
10. `<sha>` docs(refactor): cycle 4 durable docs（decision + 3 patterns + 2 pitfalls + follow-ups update）
