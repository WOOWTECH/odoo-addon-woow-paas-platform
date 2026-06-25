# Pitfall: Dual-leg rule + `perm_create=True` → privilege escalation via always-true leg

> 把「`scope='system'` 永真 read leg」與「`scope='user' AND workspace chain` create leg」混在**單一 ir.rule** + `perm_create=True`，會讓任何 `base.group_user` 透過 ORM `create({'scope': 'system', ...})` 創建 platform-level row — 全 instance 可見的 phishing / forgery / DoS 攻擊面。Round 1 MAJOR 1 reviewer 抓到。

## Symptom

**Reproducer**：

```python
# 任何 internal user (base.group_user) 都能跑
env['woow_paas_platform.mcp_server'].create({
    'scope': 'system',
    'name': 'evil',
    'url': 'http://attacker.com/sse',
    'transport': 'sse',
})
# → 不 raise AccessError，row 創建成功
```

**Attack vectors**：

| Vector | 流程 | 觸發成本 |
|---|---|---|
| **Phishing URL via ai.assistant** | user create system MCP → admin 在 `ai.assistant.mcp_server_ids` 後台不慎選入 → 全 instance AI chat 走 attacker endpoint | social engineering admin |
| **Tool definition forgery** | user 對既有 system MCP 用 `env['mcp_tool'].create({'server_id': system_mcp.id, ...})` 加假 tool → AI behavior 改變 | 已知 system MCP id 即可（list 全 instance 可見） |
| **DoS** | 灌 N 條 system MCP row + tool row | 任何 base.group_user |

**外觀**：unit test + integration test 全綠（24/24 AC 通過 + CAQA APPROVED WITH CAVEATS 14 invariants 全 ✅）— **reviewer 走獨立 multi-agent 視角才 catch**。

## Why it happens

### Single dual-leg rule + always-true leg + perm_create=True 三條件同時成立

原始 plan 設計（round 1 implementation）：

```xml
<record id="mcp_server_rule_workspace_member" model="ir.rule">
    <field name="domain_force">['|', ('scope', '=', 'system'), ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)]</field>
    <field name="groups" eval="[(4, ref('base.group_user'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>
```

3 個條件同時成立：

1. **Leg 1 `('scope', '=', 'system')` 對 new row 永真** — ORM `create({'scope': 'system', ...})` 時 Odoo 對 new record 套 record rule 的 create domain check，new record 自身 `scope='system'`，leg 1 match
2. **`perm_create=True`** — rule 允許 create
3. **D4 Python constraint 允許 `scope='system' + project_id=NULL`** — ORM 層通過

→ AccessError 不 raise，row 立刻 commit。

第二輪 leak：

4. **新 row 立刻被 same rule 的 read leg 1 永真所有 base.group_user 可讀** — 等同 phishing URL 出現在 `ai.assistant.mcp_server_ids` domain (`[('scope', '=', 'system'), ('active', '=', True)]`) 內

### 為什麼 CAQA Inv #6 沒抓

CAQA 對本次 plan 的 Invariant #6 寫的是：

> system-scope leg 是故意 always-true。

CAQA 對這條 invariant verified ✅ — 但**只 verify 了 read leg always-true 是合理設計**（system MCP 是平台級工具，所有 internal user 透過 ai.assistant 用，這個設計沒問題）。

CAQA **漏抓**：`perm_create=True` 在 always-true leg 下的 implication —
「user 可創 scope='system' row」這個 create direction 後果。

**根因**：CAQA invariant template 把 ir.rule 視為單方向（permissive read filter），沒有對 4 個 perm direction（read / write / create / unlink）分別 evaluate always-true leg 的 implication。

**對未來 CAQA 的教訓**：always-true leg 必須**分 read direction 與 create direction 評估**：
- Read direction 永真可能是設計（system-scope 公共資源）
- Create direction 永真**永遠是漏洞**（user 自創 system row）
- Write/unlink direction 永真**通常是漏洞**（user 可改 system row）

[[mcp-project-decouple]] §Round 2 改動敘事 §3 已記錄此教訓。

### 為什麼 plan AC #21 沒抓

Plan AC 把 `mcp_server` user CSV row 從 `1,0,0,0` 提升為 `1,1,1,1`，reasoning 是「讓 user 在自家 workspace CRUD user-scope MCP」。**沒考慮 ir.rule 的 `perm_create=True` 同時對 system leg 也放行**。

AC 只規範 read leg 行為（AC #17 / #19 / #20）— write/create/unlink leg 行為**沒寫成 AC**，等於 TDD 流程內**沒地方會 fail**。

### 為什麼 25/25 AC 全綠也沒抓到

- AC #17/#18/#19 只測 cross-workspace read deny — 不測 cross-scope create deny
- AC #20 測 own-workspace user 可讀自家 user MCP（防 over-correction）— 也不測 create direction
- AC #21（後加 Gap 2）測 own-workspace CRUD — 仍是 user-scope 路徑，不是 system-scope create
- 整套 TDD 流程內沒 case 跑 `env.with_user(bob).env['mcp_server'].create({'scope': 'system', ...})`

→ AC 設計遺漏對應的 create-direction security AC，**整套 TDD 流程內沒地方會 fail**。Reviewer 走獨立 multi-agent code-reviewer 視角才補上。

### 為什麼難以肉眼看出（4 個檔案在 3 個 layer）

要同時看：

1. `ir.model.access.csv` 內 user row `1,1,1,1`
2. `ir_rules.xml` 內 single rule 的 dual-leg domain + `perm_create=True`
3. `mcp_server.py` 內 D4 constraint（`scope='system' + project_id=NULL` 是合法）
4. `ai_assistant.py` / 後台 view 中 `mcp_server_ids` domain（`scope='system' AND active=True` — system row 預設對 admin 可見）

4 個地方獨自看都合理 — 必須**同時在腦中組成攻擊鏈**才看得出 leak。Individual PR review 的視角看不到全貌。

## How to avoid

### 1. Split per perm direction — 套 [[mcp-acl-6-rule-split]] pattern

不要用 single dual-leg rule + 全 perm 開放。改成：

```xml
<!-- system-scope: read only -->
<record id="mcp_server_rule_system_readonly" model="ir.rule">
    <field name="domain_force">[('scope', '=', 'system')]</field>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="False"/>
    <field name="perm_create" eval="False"/>
    <field name="perm_unlink" eval="False"/>
</record>

<!-- user-scope: full CRUD in workspace -->
<record id="mcp_server_rule_workspace_user" model="ir.rule">
    <field name="domain_force">[('scope', '=', 'user'), ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id)]</field>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>

<!-- admin bypass for backend system MCP maintenance -->
<record id="mcp_server_rule_admin_full" model="ir.rule">
    <field name="domain_force">[(1, '=', 1)]</field>
    <field name="groups" eval="[(4, ref('base.group_system'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>
```

**Sub-model 對等**：mcp_tool 走 `server_id` 反向同樣拆 3 條 = 6 條總共。
詳見 [[mcp-acl-6-rule-split]]。

### 2. Migration 必須 retire 舊 dual-leg rule（避免 OR-combine 重新打開漏洞）

```python
# src/migrations/<v>/post-migrate.py
# 刪舊 dual-leg rule 的 row + 對應 ir_model_data
cr.execute("""
    DELETE FROM ir_rule WHERE id IN (
        SELECT res_id FROM ir_model_data
        WHERE module = 'woow_paas_platform' AND model = 'ir.rule'
          AND name IN ('mcp_server_rule_workspace_member', 'mcp_tool_rule_workspace_member')
    )
""")
cr.execute("""
    DELETE FROM ir_model_data WHERE module = 'woow_paas_platform' AND model = 'ir.rule'
      AND name IN ('mcp_server_rule_workspace_member', 'mcp_tool_rule_workspace_member')
""")
```

順序對：**rule rows via `res_id` lookup → ir_model_data pointers**。反過來 ir_model_data 先刪會 lose lookup key。

不 retire 舊 rule 的失敗模式：`-u woow_paas_platform` 重新載入時，舊 rule XML 已被刪但 DB 仍有 row — 與新 3 條 rule **OR-combine** 重新打開漏洞。

詳見 `src/migrations/18.0.1.0.24/post-migrate.py:160-180`。

### 3. Regression test 鎖 create direction（不只 read direction）

```python
def test_user_cannot_create_system_mcp_via_orm(self):
    """Base.group_user must not be able to create scope='system' MCP via ORM
    even though scope='system' is universally READABLE."""
    with self.assertRaises(AccessError):
        self.env['woow_paas_platform.mcp_server'].with_user(self.bob).create({
            'scope': 'system',
            'name': 'evil',
            'url': 'http://attacker.com',
            'transport': 'sse',
        })

def test_user_cannot_create_mcp_tool_for_system_server(self):
    """B1 sub-model parity — same deny for tool definition forgery."""
    system_mcp = self.env['woow_paas_platform.mcp_server'].sudo().create({
        'scope': 'system', 'name': 'legit-sys', 'url': '...', 'transport': 'sse',
    })
    with self.assertRaises(AccessError):
        self.env['woow_paas_platform.mcp_tool'].with_user(self.bob).create({
            'server_id': system_mcp.id, 'name': 'forged-tool', ...,
        })
```

Plus over-correction guard：

```python
def test_user_can_still_create_user_scope_in_own_workspace(self):
    """Defensive — split must not block legitimate user-scope create."""
    # Bob in his own workspace ...
    user_mcp = self.env['woow_paas_platform.mcp_server'].with_user(self.bob).create({
        'scope': 'user',
        'project_id': bob_project.id,
        'name': 'legit-user-mcp',
        'url': '...', 'transport': 'sse',
    })
    self.assertTrue(user_mcp.exists())
```

合計 9 regression cases 鎖 6-rule split + over-correction（[[mcp-acl-6-rule-split]] §Test scaffolding）。

### 4. PR review 對等性 checklist（per perm direction）

任何 PR 改 ir.rule 必跑：

1. 該 model 是否有 `scope`-like 二元欄位？
2. 是否存在 dual-leg domain（特別是 `'|', (literal field = literal value), (other leg)`）？
3. 若有，always-true leg 對 new row 是否永真？
4. 該 rule 的 `perm_create` 是否 True？
5. 若 (3) ∧ (4) 都 yes → 必須拆 rule（套 [[mcp-acl-6-rule-split]]）

CAQA 視角盲區：CAQA invariant template 須加「always-true leg × perm direction matrix」evaluation。Reporter 列為 R2-2 follow-up（rule-count parity lint）。

### 5. 對 model 加 docstring invariant

```python
class McpServer(models.Model):
    """MCP Server model.

    INVARIANT (MAJOR 1 fix): scope='system' row 只能由 sudo() / group_system
    admin backend / demo data XML 建立。Enforcement 透過 ir_rules.xml 的
    mcp_server_rule_system_readonly.perm_create=False（套 base.group_user）+
    mcp_server_rule_workspace_user domain 限 scope='user'。

    Future PR 要新增 system MCP create path，必須走 sudo() 或 group_system，
    並 update test_user_cannot_create_system_mcp_via_orm regression。
    """
```

對應 [[mcp-project-decouple]] §Invariants #8。

## 為何不合併進 [[cross-workspace-task-data-leak]]

兩個 pitfall 看似都是「OR-bypass + always-true leg」，但根因不同：

| Aspect | [[cross-workspace-task-data-leak]] (B1) | [[system-mcp-create-acl-bypass]] (MAJOR 1, 本文) |
|---|---|---|
| **Always-true leg 來源** | **Odoo std rule** 的隱含設計（`privacy_visibility != 'followers'` 對 default 'employees' 永真） | **自家 rule** 的明文設計（`scope='system'` 對 system row 永真） |
| **Leak 方向** | OR-bypass：std rule 永真 leg 旁路自家 restrictive rule（**read direction** leak） | Permission direction 混淆：always-true leg + `perm_create=True` 讓 user 自創 system row（**create direction** leak） |
| **Fix shape** | Deactivate std rule（root cause = std rule 不該對 PaaS 套到 group_user） | Rule split（root cause = read direction permissive 與 create direction restrictive 不該共用單一 rule） |
| **適用 model** | Odoo std model（project / task / sale 等）配套 PaaS ACL 收緊時 | 自家 mixed-scope model（同 model 內有 platform-level + workspace-level row） |

合併會混淆兩個獨立 fix recipe。**保持兩個 pitfall doc 分立**，但兩者都 cross-link 到對方（後人讀 B1 看到「always-true leg 還可能有 create direction leak — 另見 [[system-mcp-create-acl-bypass]]」）。

## Related

- [[mcp-acl-6-rule-split]] — 本 pitfall 的解法 pattern
- [[mcp-project-decouple]] §Invariants #5/#6/#8 + §Round 2 改動敘事 — 本次 MAJOR 1 fix 落地點
- [[cross-workspace-task-data-leak]] — 同質的 OR-bypass 教訓（read direction 版本，root cause 是 std rule 對 group_user 永真 leg）
- Code 引用：
  - `src/security/ir_rules.xml:134-216` — 6-rule split 落地
  - `src/migrations/18.0.1.0.24/post-migrate.py:160-180` — 舊 dual-leg rule retire
  - `src/tests/test_mcp_acl_isolation.py::test_user_cannot_create_system_mcp_via_orm` etc. — 9 regression cases
- Commit / report：
  - commit `fdf51fc` — `refactor: MCP ACL rule split for system create deny`
  - commit `4ff8f4b` — `docs(refactor): MAJOR 1 rule split fix - system MCP create deny`（plan + decision 同步）
  - `docs/decisions/reviews/pr-review-verdict-mcp-2026-05-18.md §MAJOR 1` — round 1 reviewer 完整攻擊鏈
- Odoo 上游證據：
  - `addons/base/models/ir_rule.py:140-168` — per-group rule OR semantics（同 group OR）
  - Odoo `_group_implied_ids` — group_system 繼承 group_user（決定 admin_full 必要性）
