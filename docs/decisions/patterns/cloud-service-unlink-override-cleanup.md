# Pattern: Parent `unlink()` override + child cleanup（covers all caller paths）

> 適用：parent model 被刪時，**有 wrapper-class child rows 需要先清掉**，而 FK ondelete 又**不能設 cascade**（因為 child 的「擁有者」其實是另一個 model）。

## When to use

當三個條件同時成立：

1. **Parent 與某個 child 是 logical owner 關係**（child 物理上是 parent 的某種 wrapper / sidecar / proxy — 沒 parent 就沒物理基礎），
2. **但 schema FK 對 parent 設了 `ondelete='set null'`**（child 真正的 FK 父是另一個 model — 例如本 codebase `mcp_server.project_id`，而 `project.cloud_service_id` 又是 `set null`），
3. **caller 可能透過多條 path 觸發 parent 刪除**（lifecycle method / controller direct call / cron / admin manual ORM），canonical lifecycle method 無法強制所有 caller 走。

**典型情境（本 codebase 真實 case）**：

```
cloud_service ───── ondelete='set null' ────► project.project
                                                   │
                            mcp_server.project_id  │ ondelete='cascade'
                            ─── user-scope MCP 是 sidecar wrapper, URL 從 cloud_service 算
```

`cloud_service.unlink()` 不會連坐刪 project（`set null`），也不會 trigger `mcp_server.project_id` cascade（cascade 只在 `project.unlink()` 才 fire）— 結果 cloud_service 走了，user-scope mcp_server row 留在 DB，URL 指向已不存在的 sidecar = zombie。

**不適用**：

- Child 與 parent 是 strict cascade 語意（直接設 `ondelete='cascade'` 即可）
- Parent 只有單一 lifecycle method 為唯一 delete entry（直接在 lifecycle method 內 cleanup 就夠）
- 跨 module 觸發 delete（需走 register_hook / mail.thread integration 等更重的機制）

## What it looks like

### 1. Override `unlink()` in parent model

```python
class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'

    def unlink(self):
        """Cleanup wrapper-class child resources BEFORE deleting parent.

        Catches every unlink path — controllers calling ``service.unlink()``
        directly after their own helm uninstall, cron taking down a stuck
        service, manual ORM ops — without forcing callers to remember to call
        ``_do_cleanup`` first. ``project.project.cloud_service_id`` is
        ``ondelete='set null'`` ([[cloud-service-project-decouple]] Inv #2),
        so a naked ``unlink()`` would leave ``project_id`` populated on
        user-scope MCP rows while their backing sidecar is gone — D5
        violation + zombie URL.

        Idempotent against ``_do_cleanup``: when both run, the second one
        finds the user-scope MCP recordset already empty.
        """
        user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
            lambda s: s.scope == 'user'
        )
        if user_mcps:
            user_mcps.sudo().unlink()
            _logger.info(
                "[CLOUD_SVC] unlink() unlinked %s user-scope MCP server(s) "
                "for service ids=%s", len(user_mcps), self.ids,
            )
        return super().unlink()
```

### 2. Keep the canonical lifecycle method's cleanup step intact

```python
def _do_cleanup(self):
    """Helm uninstall — permanently remove the service.

    Order: unlink user-scope MCP → helm uninstall → state='deleting'.
    Re-ordering opens a time window where mcp_server rows reference a
    torn-down sidecar URL (silent network failures in AI chat tool calls).
    """
    # Step 1 — unlink user-scope MCP (same code as unlink() override)
    user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
        lambda s: s.scope == 'user'
    )
    if user_mcps:
        user_mcps.sudo().unlink()
    # Step 2 — helm uninstall
    # Step 3 — write state
```

**不要把 cleanup 邏輯只放在 `unlink()` 抽掉 `_do_cleanup` 的 step 1** —
lifecycle path 需要在 helm uninstall **之前**就清掉 MCP（避免 time window race，
見 [[mcp-project-decouple]] Inv #4 長註解）。`unlink()` override 是 caller-agnostic
safety net；`_do_cleanup` 是 lifecycle-aware 順序保證。兩者**功能重疊但時機不同**。

### 3. Regression test 鎖正反兩面

```python
def test_blocker2_unlink_override_cleans_user_mcp_without_do_cleanup(self):
    """Direct service.unlink() (not via _do_cleanup) must still clean
    user-scope MCP — covers controller paths that call unlink() directly
    after their own helm uninstall."""
    service = self._make_service()
    project = self._make_project(cloud_service_id=service.id)
    user_mcp = self._make_mcp(project_id=project.id, scope='user')

    service.unlink()  # NOT via _do_cleanup

    self.assertFalse(
        user_mcp.exists(),
        "user-scope MCP must be unlinked by unlink() override "
        "even when caller bypasses _do_cleanup",
    )

def test_blocker2_unlink_override_skips_system_mcp(self):
    """System-scope MCP must NOT be touched by the unlink() override
    (it belongs to platform admin, not to the service being deleted)."""
    service = self._make_service()
    system_mcp = self._make_mcp(scope='system')  # not bound to this service

    service.unlink()

    self.assertTrue(system_mcp.exists())
```

雙 test 守正反 — partial-correct fix（forget `.filtered(scope='user')`）會 red。

## Why this pattern

### 為什麼不只在 lifecycle method 內 cleanup

Canonical lifecycle method（這裡是 `_do_cleanup`）的調用樹是**社會契約**，不是技術強制。
4 條 path 都該走 `_do_cleanup` — 但實際上：

```bash
$ grep -rn "service\.unlink()" src/
src/controllers/paas.py:1953   # controller delete endpoint, calls unlink() directly after own helm uninstall
src/controllers/paas.py:1966   # PaaSOperatorError 404 case, same pattern
src/controllers/paas.py:2156   # workspace teardown, same pattern
src/models/mcp_server.py:787   # cron self-unlink for stuck-deleting state
```

4 個 caller 都選擇「自己拆 helm + `service.unlink()`」而非「`service._do_cleanup()` 接管整套」—
原因各有理由（controller 已知 helm 狀態想細控 / cron 在 helm 已 404 後只想清 DB row /
workspace teardown 已經一次 batch 拆 helm）。**強制全 caller 走 `_do_cleanup`** 等於
要求他們接受 `_do_cleanup` 的 state machine（state='deleting' 寫入 / step 2 helm 重試 /
log message format），對 caller 是過度耦合。

**Override `unlink()` 把 child cleanup 收斂到 ORM-layer atomic 動作裡**，
每個 caller 只負責自己的 helm side-effects，cleanup 是 parent 的 self-responsibility。

### 為什麼不靠 `@api.constrains` 兜底

`mcp_server.py` 已有 `@api.constrains('scope', 'project_id')` 守 D5 invariant
（user-scope MCP 必須掛 bound project）。但這條 constraint **救不了 parent-field
變動觸發的 leak**：

- `service.unlink()` → `project.cloud_service_id` 變 NULL（`set null`）
- mcp_server 自身欄位**沒變**（scope 還是 'user'，project_id 還是同個 project）
- `@api.constrains('scope', 'project_id')` **不 fire**（Odoo constrains 只在自身欄位 write 時觸發）
- → constraint hole 不擋這條 path

詳見 [[direct-unlink-bypass-cleanup-hook]] pitfall §Why @api.constrains can't catch this。

### 為什麼是 `unlink()` 而非 `write()` 或 `before_unlink` signal

- **Override `unlink()`** 是 Odoo idiom — `super().unlink()` 之前是 「parent 即將消失」的最後機會
- **No Odoo `pre_unlink` signal** — 不像 Django 有 pre_delete signal
- **Override `write()`** 不適用 — 觸發點不對（write 不會刪 row）

順序：先 cleanup → 再 super().unlink() → parent row 消失。順序錯（先 super → 再 cleanup）
會炸：parent 沒了，`self.project_ids` 走不到，cleanup 無法定位 child。

### Idempotency 設計

`_do_cleanup` 與 `unlink()` 兩條 path 都會 cleanup。Cron / lifecycle path 走 `_do_cleanup`
（step 1 unlink → step 2 helm → step 3 state），最後 cron / controller 在另一輪 trigger
`service.unlink()`，`unlink()` override 再跑一次 cleanup —
**這時 `user_mcps` recordset 是空的**（_do_cleanup 已清過）：

```python
user_mcps = self.project_ids.mapped('mcp_server_ids').filtered(
    lambda s: s.scope == 'user'
)
if user_mcps:  # ← 空 recordset 直接 skip，no-op
    user_mcps.sudo().unlink()
```

兩條 path 各自 self-sufficient + idempotent — 並聯運作不打架。

### 為什麼 `sudo()` cleanup（不是 caller-context）

`unlink()` 是 public method — caller 可能是 internal user（不一定有 workspace ACL）。
若不 sudo，cleanup 可能因 cross-workspace ACL 漏看 child row（caveat C1，CAQA 已 surface）。
Belt-and-braces sudo 確保 cleanup 完整 — child 是 parent 的 self-responsibility，不該被
caller-context ACL 卡。

## Common variations

### 變體 A：child 跨多個 parent（M2M 反向）

若 child 透過 M2M 連到 parent，cleanup 邏輯要先檢查「**只剩這個 parent reference 還是還有其他**」：

```python
def unlink(self):
    for record in self:
        affected_children = record.child_ids.filtered(
            lambda c: len(c.parent_ids) == 1  # only this parent
        )
        if affected_children:
            affected_children.unlink()
    return super().unlink()
```

本 codebase `cloud_service ←→ project` 是 1:0..1（不是 M2M），這個變體用不到 — 但其他 model
有 M2M 關聯時要記得加 filter。

### 變體 B：cleanup 失敗要 fail-fast 而非 swallow

本 codebase 的 `unlink()` override 沒包 try/except —
cleanup 失敗會 propagate 給 caller，整個 transaction rollback。這是**故意設計**：

- Cleanup 失敗 = D5 invariant 即將被打破（user-scope MCP 會留 zombie）
- 比起讓 caller 拿到「unlink success but child zombie」的 silent failure，
  fail-fast 讓 caller 重試或上 alert 更安全

若你的 child cleanup 失敗可以接受 best-effort（如純 audit log row），那 try/except + warning log
也 OK — 但要寫進 invariant doc 明示「cleanup 是 best-effort」，否則後人不知道是 quiet failure。

## See also

- [[mcp-project-decouple]] §Invariants #1 + #4 — 本 pattern 的 invariant lock 點
- [[direct-unlink-bypass-cleanup-hook]] — 沒有這個 pattern 會踩的坑
- [[cloud-service-project-decouple]] §Invariants #2 — `cloud_service ↔ project` `ondelete='set null'` 的根因
- 關鍵 code：
  - `src/models/cloud_service.py:1176-1201` — `unlink()` override 落地
  - `src/models/cloud_service.py:1119-1171` — `_do_cleanup` lifecycle path（step 1 與 override 共享 cleanup 邏輯）
  - `src/tests/test_mcp_project_lifecycle.py::test_blocker2_unlink_override_*` — 正反兩面 regression test
- 上游 Odoo 原理：
  - Odoo ORM `unlink()` 是 instance method，subclass override + `super().unlink()` 是 idiomatic 改寫點
  - `@api.constrains` 只在自身欄位 ORM write 時 fire — 不適用於跨 model FK `set null` 觸發的 transitive 變動
