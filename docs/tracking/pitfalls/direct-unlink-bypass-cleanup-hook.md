# Pitfall: Direct `.unlink()` 繞 lifecycle cleanup → child wrapper zombie

> 認為 cleanup hook 在 lifecycle method（如 `_do_cleanup`）內就夠了 — 但 controller / cron / admin manual 路徑可以 direct call `.unlink()`，**完全繞過** lifecycle hook，讓 wrapper-class child rows 留 DB。`@api.constrains` 也救不了，因為 parent FK 是 `ondelete='set null'`，child 自身欄位沒變動，constrain 不 fire。

## Symptom

- Parent model（如 `cloud_service`）有 lifecycle method（如 `_do_cleanup`）負責「先清 wrapper-class child rows → 再刪 parent」
- Lifecycle path（cron / subscription）正常運作，user-scope wrapper child（如 user-scope `mcp_server`）正確被清
- **但 controller / 其他 caller 直接 `service.unlink()` 不走 lifecycle** — wrapper child 留 DB，URL 指向已不存在的 sidecar = zombie
- Symptom 不會立刻浮：DB row 還在、ACL chain `project_id.cloud_service_id` 走到 NULL 在 search 時 silent 過濾掉、admin / sudo 路徑才看得到
- 直到 user 在 chat 點 tool call → sidecar URL 404 → AI chat 回覆「執行 tool 但無 output」 — 比 fast-fail 更難 debug

**本 case 的 surface**：

```bash
$ grep -rn "service\.unlink()" src/
src/controllers/paas.py:1953  # delete endpoint, controller 自己 helm uninstall + service.unlink()
src/controllers/paas.py:1966  # PaaSOperatorError 404 case, 同 pattern
src/controllers/paas.py:2156  # workspace teardown, 同 pattern
src/models/mcp_server.py:787  # cron self-unlink for stuck-deleting state
```

4 條 path 都繞 `_do_cleanup` — round 1 reviewer BLOCKER 2 抓到。

## Why it happens

### 4 條 caller 都選擇 direct unlink 是有理由的

- **controller delete endpoint**（`paas.py:1953`）：UI 想細控 helm uninstall + state 進度回 client，自己接管 helm 後 `service.unlink()` 收尾
- **404 case**（`paas.py:1966`）：sidecar 已被 operator 端清掉（404），只剩 DB row 要清，呼叫 `_do_cleanup` 反而重試 helm 多此一舉
- **workspace teardown**（`paas.py:2156`）：一次 batch 拆整個 workspace 的 helm，省 N 次 RPC
- **cron self-unlink**（`mcp_server.py:787`）：state='deleting' 卡 N 分鐘，cron 認定壞掉只清 DB 不再呼叫 helm

每個都是合理 design choice，但**集合起來形成 cleanup hook 的覆蓋盲區**。

### 為什麼 `@api.constrains` 救不了

直觀想：mcp_server 已有 `@api.constrains('scope', 'project_id')` 守 D5 invariant（user-scope MCP 必須掛 bound project，即 `project_id.cloud_service_id IS NOT NULL`）— 為什麼 service 刪掉後 `project.cloud_service_id` 變 NULL，constraint 不 fire？

**根因**：Odoo `@api.constrains(*fields)` 只在**被 watch 的欄位寫入時** trigger：

```
service.unlink()
  → SQL: DELETE FROM cloud_service WHERE id = X
  → SQL cascade (set null): UPDATE project_project SET cloud_service_id=NULL WHERE cloud_service_id=X
  → mcp_server 自身欄位：scope='user' （沒變）/ project_id 仍指向同個 project （沒變）
  → @api.constrains('scope', 'project_id') 不 fire（self 沒寫入）
```

雖然 mcp_server 的 `project_id.cloud_service_id` chain 評估後從 service 變 NULL，但 **Odoo constrains
只 watch 第一層欄位**，不會 cascade trigger 跨 FK 的 transitive 變動。

**`@api.constrains('scope', 'project_id', 'project_id.cloud_service_id')` 寫法（type-design 提的 Option C）
不可靠**：Odoo 18 dotted-path constrains 對深層 chain 變動的觸發行為**沒文件保證**（reviewer round 1
verdict 已明文 reject 此方向）— 試過會發現 cross-model write 不 fire。

### 為什麼 cleanup hook 在 `_do_cleanup` 不夠

`_do_cleanup` 是社會契約（plan / docstring 寫「所有 service teardown 必須走 `_do_cleanup`」），
不是 ORM 技術強制。**caller 不一定 follow** — 4 條 path 都選擇 direct unlink 各有理由（見上）。

實際上 reviewer round 1 grep `service\.unlink\(\)` 才抓到 — CAQA 的 §同源 code path 檢查 **只 verify
`_do_cleanup` 自身順序對**（line 219-234），沒驗證**所有 delete path 都經 `_do_cleanup`**。

### 為什麼 schema FK cascade 也不行

`project.cloud_service_id` 是 `ondelete='set null'`（[[cloud-service-project-decouple]] Inv #2）
— project 與 service 是「可獨立存在」的雙向解耦設計，不能改 cascade（會把刪 service 連坐刪 project，違反前次 refactor 的目標）。

`mcp_server.project_id` 是 `ondelete='cascade'` — 但 cascade 只在 `project.unlink()` 才 fire，
`service.unlink()` 不會觸發 project 刪除（set null 路徑下 project 還活著）。

→ schema 層解不了。

## How to avoid

### 1. Override `unlink()` in parent — 套 [[cloud-service-unlink-override-cleanup]] pattern

```python
def unlink(self):
    """Cleanup wrapper-class child resources BEFORE deleting parent.
    Catches every unlink path — no caller needs to remember _do_cleanup."""
    user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
        lambda s: s.scope == 'user'
    )
    if user_mcps:
        user_mcps.sudo().unlink()
    return super().unlink()
```

ORM-layer atomic — caller 不需要記得呼叫 `_do_cleanup` 才行。詳見 [[cloud-service-unlink-override-cleanup]]。

### 2. Keep `_do_cleanup` step 1 unchanged（不能拔，順序 matter）

`unlink()` override 與 `_do_cleanup` 都 cleanup user-scope MCP。看似冗餘，但時機不同：

- `_do_cleanup` step 1：在 helm uninstall **之前**清，避免 time window race（[[mcp-project-decouple]] Inv #4）
- `unlink()` override：在 `super().unlink()` **之前**清，catch direct-unlink path

兩者都是 idempotent — `_do_cleanup` 跑完後 unlink override 跑到空 recordset，no-op。

### 3. Regression test 鎖 direct-unlink path（不是只測 `_do_cleanup`）

```python
# Test（示意，非逐字）：src/tests/test_mcp_integration.py 的
# TestMCPServerCascadeDelete.test_blocker2_unlink_override_cleans_user_mcp_without_do_cleanup
# （⚠ **不在** test_mcp_project_lifecycle.py，該檔 blocker2 相關為 0 次）
def test_blocker2_unlink_override_cleans_user_mcp_without_do_cleanup(self):
    """Caller直接 service.unlink() 不經 _do_cleanup — user-scope MCP 仍須被清。"""
    service = self.Service.create({
        'name': 'Direct Unlink Service',
        'helm_release_name': 'direct-unlink',
        'helm_namespace': 'paas-ws-du',
    })
    project = self.env['project.project'].sudo().create({
        'name': f'{service.name} Project',
        'cloud_service_id': service.id,
    })
    user_mcp = self.McpServer.sudo().create({
        'url': 'http://direct-unlink-mcp.paas-ws-du.svc.cluster.local:3000/mcp',
        'scope': 'user',
        'project_id': project.id,
    })
    mcp_id = user_mcp.id

    service.unlink()  # NOT via _do_cleanup

    self.assertFalse(self.McpServer.sudo().browse(mcp_id).exists())
```

只測 `_do_cleanup` 不夠 — controller direct call path 必須有獨立 case。

### 4. PR review 加 grep 對齊（reviewer phase）

```bash
# 任何加 parent model 的 PR review 都跑：
grep -rn "<parent_model>\.unlink()" src/

# 期望結果：所有 hit 都 acceptable —
#  - lifecycle method 內呼叫 super().unlink() ✅
#  - 走 lifecycle method (_do_cleanup → super().unlink()) ✅
#  - direct call 但 parent 有 unlink() override ✅
#  - direct call 沒 override → ❌ 補 pattern
```

對應到 [[mcp-project-decouple]] §Round 2 改動敘事 §3「同源 code path 檢查必須 walk 全 unlink/delete path」教訓。

### 5. 標 invariant 在 parent docstring

```python
class CloudService(models.Model):
    """Cloud service model.

    INVARIANT: any code path that calls ``service.unlink()`` must rely on
    the ``unlink()`` override to clean user-scope MCP children — do NOT
    add a parallel cleanup loop in callers (drift risk). If you need
    custom cleanup timing (e.g., helm uninstall before MCP unlink),
    call ``service._do_cleanup()`` instead of ``service.unlink()``.
    """
```

未來 maintainer 在 child code review 時看到 `service.unlink()` 直接呼叫不會疑慮 — docstring 已 surface invariant。

## Related

- [[cloud-service-unlink-override-cleanup]] — 本 pitfall 的解法 pattern
- [[mcp-project-decouple]] §Invariants #1 + #4 + §Round 2 改動敘事 — 本次 BLOCKER 2 fix 落地點
- [[cross-workspace-task-data-leak]] §Why it happens — 「assertion + audit 走獨立 cross-check 才補上」的同質教訓（CAQA 視角盲區）
- Code 引用：
  - `src/models/cloud_service.py:1176-1201` — `unlink()` override（BLOCKER 2 fix）
  - `src/models/cloud_service.py:1119-1171` — `_do_cleanup` lifecycle path
  - `src/controllers/paas.py:1953, 1966, 2156` — 3 條 controller direct unlink path（被 override catch）
  - `src/models/mcp_server.py:787` — cron self-unlink path（被 override catch）
  - `src/tests/test_mcp_project_lifecycle.py::test_blocker2_unlink_override_*` — regression
- Commit / report：
  - commit `cf34a65` — `fix: cloud_service unlink override + cleanup helm fail state`
  - `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md §BLOCKER 2` — round 1 抓到的完整證據鏈
- Odoo 上游證據：
  - `addons/base/models/ir_actions.py` etc. — Odoo ORM `unlink()` override + `super()` 是 idiomatic 改寫點
  - Odoo `@api.constrains` doc — constraint trigger 文件（明文不 cover cross-model FK transitive 變動）
