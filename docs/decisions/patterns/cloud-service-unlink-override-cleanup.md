# Pattern: Parent `unlink()` override + child cleanup（covers all caller paths）

> 適用：parent model 被刪時，**有 wrapper-class child rows 需要先清掉**，而 FK ondelete 又**不能設 cascade**（因為 child 的「擁有者」其實是另一個 model）。

## When to use

當三個條件同時成立：

1. **Parent 與某個 child 是 logical owner 關係**（child 物理上是 parent 的某種 wrapper / sidecar / proxy — 沒 parent 就沒物理基礎），
2. **但刪 parent 不會連坐刪掉中介 model**（child 真正的 FK 父是另一個 model — 例如本 codebase `mcp_server.project_id` 是 `ondelete='cascade'` 但掛在 `project.project` 上；而刪 `cloud_service` 時 `project.project` 照活），
3. **caller 可能透過多條 path 觸發 parent 刪除**（lifecycle method / controller direct call / cron / admin manual ORM），canonical lifecycle method 無法強制所有 caller 走。

**典型情境（本 codebase 真實 case）**：

```
cloud_service ═══ M:N: project_cloud_service_rel ═══► project.project
   （刪 service 只拆 rel 列，project 存活）              │
   sidecar URL = http://{release}-mcp.{ns}.svc…        │ mcp_server.project_id
   ← 這條 URL 就是「這列 MCP 屬於哪個 service」的認領依據  │ ondelete='cascade'
                            ─── user-scope + sidecar_backed 的 MCP 才是 wrapper
```

`cloud_service.unlink()` 不會連坐刪 project，也不會 trigger `mcp_server.project_id` cascade（cascade 只在 `project.unlink()` 才 fire）— 結果 cloud_service 走了，sidecar-backed user-scope mcp_server row 留在 DB，URL 指向已不存在的 sidecar = zombie。

> ⚠ **關於 `project.cloud_service_id` 的 `ondelete='set null'`（2026-08 實測補記）**：
> 今天 `cloud_service ↔ project` 已是 **M:N**（`project_cloud_service_rel`，`cloud_service.py` 的
> `project_ids` ／ `project_project.py` 的 `cloud_service_ids`），單數 `cloud_service_id` 退化成
> `compute` + `inverse` + `store=True` 的 deprecated 欄位（`project_project.py` 的 `cloud_service_id`，
> 欄位定義內**沒有**再宣告 `ondelete`）。
> 這一度讓人以為「set null 已不存在」，但**「欄位定義沒寫 `ondelete`」≠「DB FK 不是 SET NULL」**。
>
> **量出來了：FK 仍然是 `ON DELETE SET NULL`。**
> 量測方法（自己重量一次，別信這段文字會永遠成立）：
> ```sql
> SELECT c.conname, c.confdeltype
> FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
> JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
> WHERE t.relname='project_project' AND c.contype='f'
>   AND a.attname='cloud_service_id';
> -- → project_project_cloud_service_id_fkey | n     （confdeltype 'n' = SET NULL）
> ```
> 欄位本身也還在且 nullable。原因：`cloud_service_id` 仍是 `store=True` 的 Many2one 且**沒有**
> `ondelete=`，Odoo 對非 required 的 stored M2O schema sync 預設就建 `ON DELETE SET NULL`；
> migration `18.0.1.0.26` 又是**刻意保留** column（`post-migrate.py` 的 `Keep the cloud_service_id column`）、
> 全程沒有任何 drop/alter FK，所以升版路徑也維持同一個語意。
> ⇒ **develop 上仍在主張 set null 的那幾處都是對的，不要「順手修正」它們**：
> `CloudService.unlink` 的 docstring、`test_mcp_integration.py` 四處斷言訊息，
> **以及本 doc 自己 §為什麼不靠 `@api.constrains` 兜底 的第一條**（`service.unlink()` → `cloud_service_id`
> 變 NULL）——盤這種清單時最容易漏掉的就是自己這一份，那正是 #547 的根因形狀。
>
> 話雖如此，本 pattern 需要的前提比 FK 語意弱得多，只有一句：**刪 service 不會連坐刪 project**
> —— 這句無論 FK 是 set-null 還是只拆 M:N rel 列都成立，寫處方時優先靠它。
> ⚠ 上面的量測跑在一個 fresh install 的 DB 上；既有環境若有人手動改過 FK，以你自己重跑的結果為準。

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
        service, workspace teardown cascade, manual ORM ops — without forcing
        callers to remember to call ``_do_cleanup`` first. 刪掉 service 之後
        project 仍然存活，``mcp_server.project_id`` 的 cascade 不會 fire，
        所以 sidecar-backed user-scope MCP row 會留下指向已消失的 sidecar
        — D5 violation + zombie URL。

        兩層收窄缺一不可：
        - ``sidecar_backed`` — 使用者自帶的 external MCP（URL 與本 service
          無物理關係）必須存活。
        - URL prefix — M:N 下同一個 project 可綁多個 cloud_service，拆掉
          service A 不能 collateral-unlink service B 的 sidecar MCP。

        Idempotent against ``_do_cleanup``: when both run, the second one
        finds the matching recordset already empty.
        """
        # ⚠ 節錄：真實碼在同一個迴圈裡還有 F-ENV-5 helm fail-loud guard，
        #    迴圈外還有 #318 訂閱結算 / #278 quota 回推 / #279 workspace
        #    reconcile。見下方「這段 code fence 省略了什麼」。
        for service in self:
            user_mcps = service._collect_sidecar_user_mcps(
                service._sidecar_url_prefix()
            )
            if user_mcps:
                user_mcps.sudo().unlink()
                _logger.info(
                    "[CLOUD_SVC] unlink() unlinked %s user-scope MCP "
                    "server(s) for service id=%s", len(user_mcps), service.id,
                )
        return super().unlink()

    def _collect_sidecar_user_mcps(self, url_prefix):
        """M:N-safe 認領：只收「本 service 的 sidecar」那幾列。"""
        self.ensure_one()
        candidates = self.project_ids.mapped('mcp_server_ids').filtered(
            lambda s: s.scope == 'user' and s.sidecar_backed
        )
        if url_prefix is None:
            return candidates          # 無 helm 身分（pre-deploy fixture）才退回寬 filter
        return candidates.filtered(
            lambda s: (s.url or '').startswith(url_prefix)
        )
```

**注意是 `for service in self:` 逐筆做，不是整個 recordset 一次 filter** — URL prefix 是 per-record 的（`_sidecar_url_prefix()` 內含 `ensure_one()`），批次刪多個 service 時每筆各自認領。

**這段 code fence 省略了什麼**（改真實 `unlink()` 前務必讀原檔 `src/models/cloud_service.py` 的 `CloudService.unlink`）：

- **F-ENV-5 helm fail-loud guard** 在**同一個** `for service in self:` 迴圈內、且在 MCP cleanup **之前**：`if service.helm_release_name and service.helm_namespace:` → `if not service._cleanup_helm(): raise UserError(...)`。**下方路徑表裡的 `Workspace.unlink`（workspace teardown）刻意路由到這裡就是為了借這段 guard 拆 helm**（DB FK cascade 會繞過 Python unlink）。刪掉它＝workspace 拆除時 helm release 與 namespace 會活下來繼續計費。
- **#318 訂閱結算**：`super().unlink()` **前**把仍 `in_progress` 的關聯訂閱捕獲成 `subs_to_settle`，**後**逐一 `_settle_on_entity_delete`（取消＋按比例退點）。放在後面是刻意的——helm fail-loud raise 時整筆 rollback，訂閱不動。
- **#278 ns quota**：`super().unlink()` 前把 `(workspace, helm_namespace)` 捕獲成 `quota_targets`，事後以存活集 `_push_ns_quota` 重算。
- **#279 workspace reach reconcile**：前捕獲 `project → workspace_ids` 舊值（`affected` / `old`），事後 `_reconcile_agent_runtime_reach`。

### 2. Keep the canonical lifecycle method's cleanup step intact

```python
def _do_cleanup(self):
    """Helm uninstall — permanently remove the service.

    Order: unlink user-scope MCP → helm uninstall → state='deleting'.
    Re-ordering opens a time window where mcp_server rows reference a
    torn-down sidecar URL (silent network failures in AI chat tool calls).
    """
    self.ensure_one()
    # Step 1 — unlink sidecar-backed user MCP（與 unlink() override 共用
    # helper，不要各寫一份 filter：兩邊漂移就是 collateral-unlink 的來源）
    user_mcps = self._collect_sidecar_user_mcps(self._sidecar_url_prefix())
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
# Test（示意，非逐字）：src/tests/test_mcp_integration.py 的
# TestMCPServerCascadeDelete。真實檔另有 workspace / template 的 setUpClass、
# sidecar_backed 條件式餵料，且 unlink 後一律**先存 id 再 browse 重讀**
# （unlink 過的 recordset 自身 exists() 不可靠）。
def test_blocker2_unlink_override_cleans_user_mcp_without_do_cleanup(self):
    """Direct service.unlink() (not via _do_cleanup) must still clean
    user-scope MCP — covers controller paths that call unlink() directly
    after their own helm uninstall."""
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

    self.assertFalse(
        self.McpServer.sudo().browse(mcp_id).exists(),
        "user-scope MCP must be unlinked by unlink() override "
        "even when caller bypasses _do_cleanup",
    )

def test_blocker2_unlink_override_skips_system_mcp(self):
    """System-scope MCP must NOT be touched by the unlink() override
    (it belongs to platform admin, not to the service being deleted)."""
    service = self.Service.create({'name': 'System Safe Service', ...})
    system_mcp = self.McpServer.sudo().create({
        'name': 'Platform System MCP',
        'scope': 'system',        # ◄── 無 project_id，不綁任何 service
    })
    sys_id = system_mcp.id

    service.unlink()

    self.assertTrue(self.McpServer.sudo().browse(sys_id).exists())
```

四面守 — 少任何一層 filter 都會 red：忘 `scope == 'user'` → 砍到 system MCP；忘 `sidecar_backed` → 砍到使用者自帶的 external MCP；忘 URL prefix → M:N 下砍到同 project 其他 service 的 sidecar MCP；filter 過嚴 → 自己的 sidecar MCP 變孤兒。

## Why this pattern

### 為什麼不只在 lifecycle method 內 cleanup

Canonical lifecycle method（這裡是 `_do_cleanup`）的調用樹是**社會契約**，不是技術強制。
每一條 path 都該走 `_do_cleanup` — 但實際上：

刪除 CloudService row 的執行路徑如下（**人工判讀，非單一 grep 可得**——
`cloud_service.py` 那條的行內容是 `self.unlink()`，任何「行內容含 service」的過濾都抓不到它；
起點可以用 `grep -rn --include='*.py' '\.unlink()' src/ | grep -v '^src/tests/'`
拿到一份超集，再人工挑出「刪的是 CloudService row」的那幾條）。
**這裡刻意不寫條數**：可達與否取決於下表「可達性」欄位那些**上游 selector**，
改一個字條數就變，而寫死的數字不會自己更新（本表上一版寫「5 條實際執行路徑」時
`_poll_and_update_status` 那條其實已經走不到）。

| 位置（符號名為主鍵） | 做什麼 | 可達性 |
|---|---|---|
| `controllers/paas.py` `_delete_service` | delete endpoint，自己拆完 helm 後 `service.unlink()`；`PaaSOperatorError` 404 分支同形（同一支裡兩處） | 可達 |
| `controllers/paas.py` `_update_service_status` | status poll 撞 404 且 `state == 'deleting'` → 收屍 | **可達** — 它有兩個生產呼叫端，`_list_services` 的 `transitional_states` **含** `'deleting'`（走得到）、`_get_service` 的 gate **不含**（走不到）。一個入口帶得進來就算可達 |
| `models/cloud_service.py` `_poll_and_update_status` | 與上一列同形：輪詢撞 404 且 `state == 'deleting'` → `self.unlink()` | **今天走不到** — 唯一的生產呼叫端 `_cron_check_deploying_services` 的 search domain **不含** `'deleting'` |
| `models/workspace.py` `Workspace.unlink` | workspace teardown：`service_ids.sudo().unlink()` | 可達 |
| `models/cloud_service.py` 的 `workspace_id` 欄位 `ondelete='cascade'` | **DB 層 cascade，繞過 Python `unlink()`** ⇒ 這條**不會**觸發本 pattern 的 child cleanup，`grep '.unlink()'` 也永遠抓不到它 | 只在 `Workspace.unlink` 被繞過時（raw SQL / 上游 cascade chain）才走到 |

⚠ **判可達性要自己追上游 selector 的 state 集合，不要讀 in-code 註解**（註解一樣會過期）。
`_update_service_status` 與 `_poll_and_update_status` 這兩列就是對照組：兩支 code 結構完全同形
（404 ＋ `state == 'deleting'` → unlink），差別只在誰把 `'deleting'` 放進了自己的 selector。
⚠ 而且要追**每一個**呼叫端，不是第一個：`_update_service_status` 的兩個呼叫端裡只有 `_list_services`
帶得進 `'deleting'`，只看 `_get_service` 會得出與事實相反的結論。
⚠ 也不要把 `grep '.unlink()'` 的命中數當分母 — 最後一列根本不經 Python `unlink()`。

這些 caller 都選擇「自己拆 helm + `service.unlink()`」而非「`service._do_cleanup()` 接管整套」—
原因各有理由（controller 已知 helm 狀態想細控 / cron 在 helm 已 404 後只想清 DB row /
workspace teardown 刻意路由到 `CloudService.unlink()` 借它的 helm fail-loud guard，
見 `Workspace.unlink` 的 docstring）。**強制全 caller 走 `_do_cleanup`** 等於
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
user_mcps = service._collect_sidecar_user_mcps(service._sidecar_url_prefix())
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

### 變體 A：child 跨多個 parent（M:N）— **本 codebase 就是這一型**

若 child 透過中介 model 被多個 parent 共享，cleanup **必須有認領依據**，否則拆 parent A 會
collateral-unlink parent B 的 child。

一般解是數 parent reference：

```python
affected_children = record.child_ids.filtered(
    lambda c: len(c.parent_ids) == 1  # only this parent
)
```

本 codebase 用**更強的認領依據**：child 的 URL 本身就編碼了 owner
（`http://{release}-mcp.{ns}.svc.cluster.local`），所以直接比 URL prefix，不必數 reference
—— 見上方 `_collect_sidecar_user_mcps`。
⚠ 這裡**不能**用 reference counting：svc_A、svc_B 各自的 MCP row 都只掛在同一個 project 上，
`len(parent_ids)` 恆為 1，擋不住連坐。

`cloud_service ←→ project` 自 Phase A（migration `18.0.1.0.26`）起是 **M:N**
（rel table `project_cloud_service_rel`），單數 `cloud_service_id` 只是 deprecated stored
compute。**不要再照舊 1:0..1 的假設寫寬 filter。**

（嚴重性限定：Phase A schema 已允許 multi-bind，但 **production 綁定路徑尚未開放** ——
`cloud_service_ids` 的寫入點今天全部在 `src/tests/`，唯一的 production 寫入
`_inverse_cloud_service_id`（`project_project.py:140-151`）是 `[(6, 0, [svc.id])]` 取代整組，
無任何 view 曝露該欄位。所以連坐是**結構性風險**、有測試鎖著，不是現行可達的線上事故。）

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
- [[cloud-service-project-decouple]] §Invariants #2 — ⚠ 該條已被 Phase A M:N migration superseded（見該檔開頭 banner）：現況是 M:N rel table，不再是「帶 `ondelete='set null'` 的實體 M2O 欄位」。**但這不等於 DB FK 已不是 SET NULL**，見本檔 §When to use 的補記。
- [[sidecar-backed-flag-lifecycle]] — `sidecar_backed` 這個 schema flag 的契約（為什麼 lifecycle-created 與 user-managed row 同表要靠它分流）；**兩份 doc 的 filter 規範必須一致**，見該檔 §See also 的收斂規範
- 關鍵 code：
  - `src/models/cloud_service.py` 的 `CloudService.unlink` — override 落地（含 helm fail-loud guard 與 #318/#278/#279）
  - 同檔 `_collect_sidecar_user_mcps` / `_sidecar_url_prefix` — 兩條 path 共用的認領邏輯
  - 同檔 `_do_cleanup` — lifecycle path（step 1 呼叫同一組 helper）
  - `src/tests/test_mcp_integration.py` 的 `test_blocker2_unlink_override_cleans_user_mcp_without_do_cleanup` / `test_blocker2_unlink_override_skips_system_mcp` — 正反兩面（**不在** `test_mcp_project_lifecycle.py`）
  - `src/tests/test_mcp_sidecar_backed_lifecycle.py` — `sidecar_backed` 那一層 filter 的正反鎖
  - `src/tests/test_agent_pod_phase_a_cloud_service_mn.py` — M:N 下的 URL-prefix 認領（external MCP 存活）
  - ⚠ 已知殘留：`test_mcp_project_lifecycle.py` 與 `test_mcp_integration.py` 有數處**斷言失敗訊息**仍逐字複述只按 `scope == 'user'` 過濾的舊 filter，並指向 `cloud_service.py:1176-1201` / `:1192` 這類失效行號。斷言本體正確、字串未更新——別把 red 訊息當契約讀。
- 上游 Odoo 原理：
  - Odoo ORM `unlink()` 是 instance method，subclass override + `super().unlink()` 是 idiomatic 改寫點
  - `@api.constrains` 只在自身欄位 ORM write 時 fire — 不適用於跨 model FK `set null` 觸發的 transitive 變動
