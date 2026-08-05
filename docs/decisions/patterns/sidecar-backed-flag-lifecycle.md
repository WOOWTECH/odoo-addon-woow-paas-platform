# Pattern: schema flag-driven lifecycle filter contract（`sidecar_backed` / `auto_created` 類）

> 適用：同 model 內既有「系統 / lifecycle 建立的 row」也有「user-managed row」，兩種 row 有不同 cleanup / sync 行為。用 schema boolean flag 標誌物理依賴關係，cleanup filter + create path 必須對等遵守 flag 契約 — 漏一邊即 zombie row 或誤殺 user data。

## When to use

當下列條件同時成立：

1. **同 model 內既有「系統建立 + lifecycle-coupled」row 也有「user-managed + lifecycle-independent」row**（如 `mcp_server` 既有 sidecar wrapper、也有 user 自填 URL）
2. **某類 row 物理上依賴 backing resource**（cluster.local URL / sidecar pod / cron schedule 等），backing resource 拆了 row 變 zombie
3. **同類 row 又對 user 是 functional**（不可直接 ORM constraint 禁止）
4. **Cleanup 邏輯必須區分 row 類型**：backing 拆了清 lifecycle-coupled，**保留** user-managed
5. **Create path 多個 entry**（lifecycle auto-create / UI shortcut / endpoint API / form view） — 每個 entry 必須設對 flag

**典型情境（本 codebase 真實 case，2026-05-21 cycle 4 [[mcp-url-redesign]]）**：

```
mcp_server  scope='user'
  ├── sidecar_backed=True  ─ URL 從 service.helm_release_name + namespace 算
  │                          service 拆了 → URL zombie → cleanup hook 必清
  │
  └── sidecar_backed=False ─ user 自填 URL（external endpoint）
                             service 拆了 → URL 仍 valid → cleanup hook 必保留
```

PR #47 cycle 用 `auto_created` 表達「lifecycle 自動建」— 但語意不夠精確（只說「怎麼建」，沒說「物理依賴什麼」）。Cycle 4 加 `sidecar_backed` 純表「物理是否依賴 sidecar」，cleanup filter 改用此 flag。

**不適用**：

- Model 只有單一 row 類型（沒 split） — 不需要 flag
- 兩類 row lifecycle 完全相同（沒不同 cleanup 規則） — 不需要 flag
- Row 類型差異透過 inheritance / delegation 表達（如 multi-table inheritance） — 該用更強的 polymorphism mechanism，不是 flag
- backing resource 拆了**任何** row 都要清（如 cascade delete 語意） — 直接設 FK `ondelete='cascade'`，不需 flag

## What it looks like

### 1. Field 定義（readonly + default False）

```python
class McpServer(models.Model):
    _name = 'woow_paas_platform.mcp_server'

    sidecar_backed = fields.Boolean(
        default=False,
        readonly=True,
        help='Whether this MCP record was populated from a backing service sidecar '
             'URL hint. Row is tied to the backing cloud_service lifecycle: cleanup '
             'when service is removed. user-provided external URLs (sidecar_backed=False) '
             'are NOT cleaned up.'
    )
```

**`readonly=True` 重要性**：form view backend admin 不該 toggle（toggle 會錯亂 lifecycle contract）；UI shortcut endpoint 與 frontend 內部 set，user 看不到 raw toggle。

### 2. Cleanup filter 兩條 path 對等改寫

⚠ **本節是本 codebase 的現行實作節錄（含 M:N 收窄），不是最小示意。** flag 維度本身在別的 codebase 可以單獨成立；本 codebase 因為 `project ↔ cloud_service` 是 M:N，另需第二維（見下方 Phase A 補記）。

```python
class CloudService(models.Model):
    _name = 'woow_paas_platform.cloud_service'

    def _do_cleanup(self):
        """Step 1: unlink sidecar-backed user-scope MCP before helm uninstall."""
        self.ensure_one()
        url_prefix = self._sidecar_url_prefix()
        user_mcps = self._collect_sidecar_user_mcps(url_prefix)   # ◄── 不要自己寫 lambda
        if user_mcps:
            user_mcps.sudo().unlink()
        # Step 2: helm uninstall
        # Step 3: state='deleting'

    def unlink(self):
        """Catches direct service.unlink() bypassing _do_cleanup."""
        for service in self:                                       # ◄── M:N 必須 per-record
            user_mcps = service._collect_sidecar_user_mcps(
                service._sidecar_url_prefix()                      # ◄── 兩 path 走同一 helper
            )
            if user_mcps:
                user_mcps.sudo().unlink()
        return super().unlink()

    def _collect_sidecar_user_mcps(self, url_prefix):
        """flag 先、prefix 後：兩層都不可省。"""
        self.ensure_one()
        candidates = self.project_ids.mapped('mcp_server_ids').filtered(
            lambda s: s.scope == 'user' and s.sidecar_backed       # ◄── 第一維：flag 契約
        )
        if url_prefix is None:            # 無 sidecar identity 才退回廣義 filter
            return candidates
        return candidates.filtered(
            lambda s: (s.url or '').startswith(url_prefix)         # ◄── 第二維：ownership
        )
```

（上面是**節錄**：真實的 `unlink()`（`src/models/cloud_service.py:3098-3179`）在同一個 `for service in self:` 迴圈裡還有 F-ENV-5 helm fail-loud guard，迴圈外還有 #318 訂閱結算 / #278 quota 回推 / #279 workspace reconcile。要改那支 method 前請讀原檔，不要拿本節當完整實作。）

[[cloud-service-unlink-override-cleanup]] pattern 的延伸 — filter 條件擴**兩個**維度（flag + URL ownership），pattern 本身不破。⚠ 兩維度是 AND 不是二選一：只留 prefix 會誤殺「external 但 URL 剛好是 in-cluster」的合法 row（見下方「endpoint 不 enforce」）；只留 flag 會在 M:N 下誤殺別的 service 的 sidecar MCP。

### 3. Create path 必設 flag

```python
# Endpoint `paas.py:_create_project_mcp_server`
def _create_project_mcp_server(self, project_id, params):
    # ... validate ...
    vals = {
        'name': params['name'],
        'url': params['url'],
        'scope': 'user',
        'project_id': project_id,
        'sidecar_backed': bool(params.get('sidecar_backed', False)),  # ◄── endpoint 收 payload
    }
    return McpServer.create(vals)
```

```javascript
// Frontend `McpServersSection.js:submitForm`
submitForm(formData) {
    const payload = {
        name: this.state.formName,
        url: this.state.formUrl,
        sidecar_backed: !!this.state.isSidecarFlow,  // ◄── UI flow 區分
    };
    // sidecar flow（點「Add Sidecar MCP」按鈕走 sidecar-hint prefill）
    // external flow（點「Add External MCP」按鈕空白 form）
    return this.support.createProjectMcpServer(this.projectId, payload);
}
```

### 4. Test 雙 fixture paired 驗證

```python
class TestMcpSidecarBackedLifecycle(TransactionCase):
    def _seed_service_with_dual_user_mcps(self):
        """Seed one sidecar-backed + one external MCP on the same service."""
        service = self._make_service()
        project = self._make_project(cloud_service_id=service.id)
        sidecar_mcp = self._make_mcp(
            project_id=project.id, scope='user',
            url=f"http://{service.helm_release_name}-mcp.{service.helm_namespace}.svc.cluster.local:3000/mcp",
            sidecar_backed=True,
        )
        external_mcp = self._make_mcp(
            project_id=project.id, scope='user',
            url="https://mcp.example.com/sse",
            sidecar_backed=False,
        )
        return service, sidecar_mcp, external_mcp

    def test_do_cleanup_unlinks_sidecar_backed_only(self):
        service, sidecar_mcp, external_mcp = self._seed_service_with_dual_user_mcps()
        service._do_cleanup()
        self.assertFalse(sidecar_mcp.exists(),
                         "sidecar-backed must be cleaned when service torn down")
        self.assertTrue(external_mcp.exists(),
                        "external MCP must be preserved (URL physically independent)")

    def test_unlink_override_unlinks_sidecar_backed_only(self):
        """Same paired assertion via unlink() override path — caller-agnostic safety net."""
        # ...
```

**雙 fixture paired** 是核心 — 單一 fixture 永遠分不清「沒清是因為 filter 對 / 還是因為根本沒進 cleanup」。

### 5. Migration backfill 既有 row

```python
# src/migrations/18.0.1.0.25/post-migrate.py
def migrate(cr, version):
    # Backfill: legacy auto_created=True row 都是 sidecar-backed 來源
    # 加 sidecar_backed=FALSE guard 是 idempotent — 重跑不重複寫
    cr.execute("""
        UPDATE woow_paas_platform_mcp_server
        SET sidecar_backed = TRUE
        WHERE auto_created = TRUE AND sidecar_backed = FALSE
    """)
```

## Why this pattern

### 為什麼用 flag 而非「URL 形態 sniff」

直覺反對：「為什麼不直接看 URL 是不是 cluster.local，自動分類？」

**反對 sniff 的理由**：

1. **Stale state**：service rename / namespace 變動後，sidecar URL 仍然是 cluster.local 形態但 backing service 已沒 reference — sniff 無法區分「fresh sidecar」與「stale snapshot」
2. **User 可手動填 cluster.local**：[[mcp-url-redesign]] plan AC #28 明示「endpoint 故意不 enforce URL ↔ flag 一致性」— user 把 sidecar URL 貼到 External form 是合法 UX，sniff 會把它錯歸 sidecar-backed → 後續 service 刪時誤殺。（2026-07 起此「不 enforce」僅剩 `sidecar_backed=False` 方向仍成立，見下方反轉補記）
3. **Cross-cluster URL 形式可能變**：未來支援 multi-cluster，cluster.local 不再唯一 → sniff 規則要追著改

**Flag 是 explicit declaration**：「這 row 物理依賴 backing service」是 create-time 決定的事實，不該 runtime 推斷。

### 為什麼 cleanup filter 兩條 path 都要對等改

[[cloud-service-unlink-override-cleanup]] pattern：parent `unlink()` override 是 caller-agnostic safety net，`_do_cleanup` 是 lifecycle-aware ordering — **兩者功能重疊但時機不同**，**兩條 path 都會被 call**。

任一 path 漏 filter：

- `_do_cleanup` 漏 → cron / subscription_lifecycle_mixin 走的 lifecycle path 誤殺 external MCP
- `unlink()` override 漏 → controller direct unlink path / workspace teardown 誤殺

雙 path 對等改是 [[direct-unlink-bypass-cleanup-hook]] 教訓的延續 — 不靠 caller social contract，靠 model self-responsibility。

### 2026-05 Phase A 補記：M:N 之後 filter 要再窄一維

`cloud_service.project_ids` 已從 One2many 改為 **Many2many**（`project_cloud_service_rel`，
`src/models/cloud_service.py:332-339`；反向 `project.cloud_service_ids`，
`src/models/project_project.py:20-29`）。同一個 project 可同時綁 svc_A 與 svc_B，
`self.project_ids.mapped('mcp_server_ids')` 因此會撈到**別的 service 的** sidecar MCP。

只用 flag 的 filter 在 M:N 下＝拆 svc_A 會連坐清掉 svc_B 仍在服務中的 sidecar MCP。
落地做法是加第二維 **URL ownership**：`_sidecar_url_prefix()`
（`http://{helm_release_name}-mcp.{helm_namespace}.svc.cluster.local`，
`cloud_service.py:3196-3212`）＋ `_collect_sidecar_user_mcps()`（`:3181-3194`），
兩條 path 共用（call site：`:2660-2661` 與 `:3159-3161`）。

⚠ **#271 的 in-cluster invariant 不是這一維的保證。** 它讓 sidecar_backed row 至少一定是
in-cluster URL，縮小了 prefix filter 漏抓的面；但 invariant **不保證 URL 屬於本 service** ——
constraint 只看 scheme + `.svc.cluster.local` 結尾，**不比對 release / namespace**
（判準全文在 `src/models/mcp_server.py:150` 的 `return parsed.scheme == 'http' and host.endswith('.svc.cluster.local')`；
而 prefix filter 要求 `startswith('http://{release}-mcp.{ns}.svc.cluster.local')`，嚴格得多）。
使用者在 Sidecar flow 貼別的 service 的 in-cluster URL、或 service rename 後，該 row 仍會被
prefix filter 漏掉變 orphan —— 這是**已知殘留缺口**，與下面的 `url_prefix is None` fallback 並列。
（healer `_heal_sidecar_backed_non_cluster_url` 也救不了：它只把非 in-cluster 的 row 反手設成
`sidecar_backed=False`，連 prefix 都不看。）

⚠ 殘留缺口 2：`url_prefix is None`（service 無 helm identity）時仍退回廣義 filter
（`cloud_service.py:3190-3191`），此路徑在 M:N 下仍會誤殺他人 row。新增 cleanup path
時**不要**複製這個 fallback 的語意當成安全預設。

### 為什麼 endpoint 不 enforce「flag ⇔ URL 形態一致」

design intent：UI shortcut 是**UX 模型**而非**強制契約**。User 可以從 sidecar form 改 URL 為 external、也可以從 external form 貼 cluster.local URL — frontend 負責 mental model，endpoint 不擋。

**Inv #10 強調**：UI shortcut「Add Sidecar MCP」create path **必帶 `sidecar_backed=True`** — 但這是「**flow** 內部 set」而非「**URL 形態** validate」。兩個 button 對應兩個 flow，flow 決定 flag value。

實際 design 後果：

- User 從 Sidecar form 改 URL 為 external，flag 仍 True → service 拆時 cleanup 誤清 external — **user 自找的**（design tradeoff，由 plan §AC #28 明示）
- User 從 External form 貼 cluster.local URL，flag False → service 拆時保留 zombie URL — user 須自己處理

兩個 case 都比「endpoint 強制 enforce」對 UX 友善 — user 對 ownership 有掌控。

### 2026-07 反轉補記（#271）：`sidecar_backed=True` 方向已改為硬 enforce

上一節的「endpoint 不 enforce」設計在 #271（PR #308）落地後**部分反轉**：

- **`sidecar_backed=True` ⟹ URL 必須 in-cluster** 現在是 model 層硬 invariant：
  `@api.constrains('sidecar_backed', 'url')` `_check_sidecar_url_in_cluster`
  （`src/models/mcp_server.py:152`）強制 http scheme + hostname 以
  `.svc.cluster.local` 結尾，**無 su / group bypass**（對 sudo 寫入同樣生效）。
  判斷邏輯單一來源 `_is_in_cluster_url()`（`src/models/mcp_server.py:136`），
  controller 層 friendly error 與 migration healer
  `_heal_sidecar_backed_non_cluster_url`（`src/models/mcp_server.py:172`）共用同一函式。
- 反轉理由：sidecar-backed row 掛 public hostname 會讓 in-cluster agent
  data-plane 撞 CF Access login page，且 teardown 的 URL-prefix filter 會 miss
  該 row 變 orphan — 不再是「user 自找的」tradeoff，而是安全 / lifecycle 破口。
  詳見 `docs/plans/2026-Q3/2026-07-06-271-internal-mcp-bypass-guard-design.md` §3（Invariant）/ §4（設計，含 healer 決策）。
- **`sidecar_backed=False` 方向不變**：user 在 External form 貼 cluster.local
  URL 仍合法（service 拆時保留 zombie URL、user 自理）— 上一節的 design intent
  對 external MCP 仍成立。「不 enforce」如今是**單向**的。

PR #47 用 `auto_created` 表「自動建」。Cycle 4 拆 auto-create 後：

- B 案下沒有「自動建」path 了 — `auto_created` 字面語意 stale
- 既有 row 仍有 `auto_created=True`（PR #47 留下）— 不能 rename + drop column（10+ regression test 引用，rename 會炸）

兩個並存：

- `auto_created` — pre-existing field，**deprecated**，cron `_cron_retry_mcp_sync` 仍 query 它（M1 follow-up）
- `sidecar_backed` — **new field**，cleanup filter 與 UI shortcut 對齊，未來新增 entry point 一律用此

長期 cleanup path：M1 follow-up（cron query 改 `sidecar_backed`）完成後，`auto_created` column 可安全 drop。

## Anti-patterns（後人改本區前 STOP）

| 反 pattern | 問題 |
|-----------|------|
| 「Cleanup filter 漏 `sidecar_backed` 條件」 | 誤殺 user-managed external MCP（user 抱怨） |
| 「`_do_cleanup` 改 filter，`unlink()` override 忘改」 | Controller direct unlink path 誤殺 external / 誤殺他 service 的 sidecar row |
| 「cleanup filter 只用 `sidecar_backed`、不 narrow URL prefix」 | M:N 下拆 svc_A 連坐清掉 svc_B 仍在用的 sidecar MCP。⚠ **嚴重性限定：Phase A schema 已允許 multi-bind，但 production 綁定路徑尚未開放** —— `cloud_service_ids` 的寫入點今天全部在 `src/tests/`，唯一的 production 寫入 `_inverse_cloud_service_id`（`project_project.py:140-151`）是 `[(6, 0, [svc.id])]` 取代整組，且無任何 view 曝露該欄位。所以這是**結構性風險**（測試鎖著：`test_agent_pod_phase_a_cloud_service_mn.py`），不是現行可達的線上事故 |
| 「Create path 漏設 flag（如 endpoint payload 不收）」 | UI shortcut row 變 zombie（service 拆後 URL 失效但 row 留） |
| 「Form view 讓 user 直接 toggle `sidecar_backed`」 | User 錯誤 toggle 後 cleanup 行為亂掉 — readonly + UI flow 內部 set 才對 |
| 「Endpoint enforce URL ↔ flag 一致性」（僅指 `sidecar_backed=False` 方向） | 違反 [[mcp-url-redesign]] design intent — frontend 負責 mental model。⚠ `sidecar_backed=True` 方向自 #271 起**必須** enforce（model constraint 已存在，勿移除），見「2026-07 反轉補記」 |
| 「URL sniff 推斷 flag 而非 explicit」 | Stale state + user override 場景錯誤歸類 |
| 「同 model 加第二個類似 flag（如 `auto_created` + `sidecar_backed` 並存）」 | 短期 maintainability debt — 長期須清理至 single flag |

## 後人改本區前 checklist

加 / 改本 model 的 create path 前，依序檢查：

1. **新 create entry**（如新 endpoint / cron / migration）→ 必須決定 row 是 sidecar-backed 或 external，**explicit set flag**
2. **新 cleanup path**（如新 lifecycle trigger） → filter **必須包含 `sidecar_backed=True`** 條件，**且**以本 service 的 `_sidecar_url_prefix()` narrow —— 直接呼叫 `_collect_sidecar_user_mcps()`，**不要自己寫 lambda**
3. **改 cleanup filter** → `_do_cleanup` + `unlink()` override **兩條對等改**；對等的做法是**共用同一個 helper**，且 `unlink()` 是 multi-record，必須 per-record loop（每個 service 各自的 prefix）
4. **改 field readonly 屬性** → 評估 form view backend admin 是否會被 toggle；若否，保留 readonly；若是，加 ACL `admin_full` rule explicit allow
5. **重新理解 flag 語意** → 對齊 [[mcp-url-redesign]] Inv #9 / #10 — 物理依賴關係不可由 runtime sniff 推斷
6. **跑 paired regression test**：`test_*_unlinks_sidecar_backed_only` + `test_*_preserves_external` 同 fixture paired 驗證

## See also

- [[mcp-url-redesign]] §Invariants Inv #9 + Inv #10 — 本 pattern 的 invariant lock
- [[mcp-project-decouple]] §Invariants #4 — cleanup hook 順序（與本 pattern 的 filter 條件配合）
- [[cloud-service-unlink-override-cleanup]] — 雙 path cleanup idiom（本 pattern 是其延伸：filter 條件擴一個維度）
- [[direct-unlink-bypass-cleanup-hook]] — 沒走 unlink override pattern 會踩的 zombie row 坑
- `docs/plans/2026-Q3/2026-07-06-271-internal-mcp-bypass-guard-design.md` §3/§4 — #271 in-cluster 硬 enforce 反轉的設計依據
- 關鍵 code：
  - `src/models/mcp_server.py` `sidecar_backed` field — readonly Boolean
  - `src/models/mcp_server.py:152` `_check_sidecar_url_in_cluster` — #271 硬 constraint（sidecar_backed=True ⟹ in-cluster URL，無 bypass）
  - `src/models/cloud_service.py:3181-3194` `_collect_sidecar_user_mcps` + `:3196-3212` `_sidecar_url_prefix` — 雙 path 共用的 filter（flag **AND** URL prefix）
  - `src/models/cloud_service.py:2660-2661`（`_do_cleanup`）與 `:3159-3161`（`unlink()` per-record loop）— 兩條 call site
  - `src/controllers/paas.py:_create_project_mcp_server` — endpoint payload 寫入
  - `src/static/src/paas/pages/project-detail/sections/McpServersSection.js:submitForm` — frontend flow 區分
  - `src/migrations/18.0.1.0.25/post-migrate.py` — idempotent backfill `auto_created → sidecar_backed`
  - `src/tests/test_mcp_sidecar_backed_lifecycle.py` — paired fixture 鎖雙路徑兩方向（⚠ 該檔部分斷言失敗訊息仍逐字複述**沒有 prefix 維度**的舊 filter 字串，斷言本體正確、字串未更新，別把它當契約讀）
  - `src/tests/test_agent_pod_phase_a_cloud_service_mn.py` — M:N 下不跨 service 連坐（`test_do_cleanup_unlinks_only_own_sidecar_mcp` / `test_unlink_override_narrows_same_way`）
- [[cloud-service-unlink-override-cleanup]] 與本檔的**收斂規範（兩份不得各寫各的）**：雙 path 一律
  `service._collect_sidecar_user_mcps(service._sidecar_url_prefix())`；語意 =
  `scope == 'user'` **AND** `sidecar_backed` **AND** `url.startswith(prefix)`。
  本 codebase 的 M:N 收窄機制是 **URL ownership prefix**，**不是** reference counting
  （`len(c.parent_ids) == 1` 在此處是錯的：svc_A、svc_B 各自的 MCP row 都只掛在同一個 project 上，
  reference count 恆為 1，擋不住連坐）。
