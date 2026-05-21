# Pattern: ir.rule dual-leg domain for optional-binding model

> 適用：model A 可選綁 model B（`A.b_id` 是 nullable M2O）；workspace 隔離透過 A→B→workspace chain 達成；但 unbound（`A.b_id IS NULL`） rows 仍須對某些 user 可見（如「creator 自己看得到」或「follower 可看」）— 需 dual-leg ir.rule domain：leg 1 bound walk + leg 2 unbound carve-out。

## When to use

當下列條件同時成立：

1. **Model 有 nullable M2O 到 workspace-bearing model**（如 `mcp_server.project_id → project.project`，而 `project.cloud_service_id → cloud_service → workspace` 是 nullable chain；或 `project.cloud_service_id` 自身 nullable）
2. **bound rows 透過 chain walk 做 workspace 隔離**（如 `project_id.cloud_service_id.workspace_id.access_ids.user_id = user.id`）
3. **unbound rows 不可一刀切 deny**（business 需求允許某些 user 看到 — creator / follower / workspace member 等）
4. **跨 workspace 仍須 deny**（unbound 不等於 public visible — 必須限縮給 specific user set）

**典型情境（本 codebase 真實 case）**：

- [[cloud-service-project-decouple]] D3=A — `project.project.cloud_service_id` nullable，unbound project 透過 `message_partner_ids + create_uid` 雙腿可見
- [[mcp-url-redesign]] — `mcp_server.scope='user'` 掛 unbound project 時，透過 `project_id.message_partner_ids` 給 follower 可讀
- 通用 pattern：所有「可選綁 workspace」的 self-owned model

**不適用**：

- Model 強制綁 workspace（M2O `required=True`）— single-leg walk 即可
- Unbound rows 完全 public visible（如 platform-level config）— 用 [[mcp-acl-6-rule-split]] 的 `system_readonly` 永真 leg pattern
- Unbound rows 完全不允許存在（如 PR #47 D5 invariant 設計）— 在 ORM 層拒絕，不需 ACL carve-out

## What it looks like

### Domain shape（Odoo polish notation）

```xml
<field name="domain_force">['&amp;',
    ('scope', '=', 'user'),                          ◄── 外層 AND，鎖 scope direction
    '|',                                              ◄── 內層 OR，bound/unbound 兩腿
    ('project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),  ◄── leg 1: bound walk
    '&amp;',                                          ◄── leg 2 內部 AND
    ('project_id.cloud_service_id', '=', False),     ◄── leg 2 unbound condition
    ('project_id.message_partner_ids', 'in', [user.partner_id.id])  ◄── leg 2 follower carve-out
]</field>
```

### Python expression（controller mirror — [[two-layer-acl-lockstep]]）

```python
def _check_project_access(self, project):
    if not project.cloud_service_id:
        # leg 2 mirror — unbound carve-out
        return request.env.user.partner_id in project.message_partner_ids
    # leg 1 mirror — bound walk
    workspace = project.cloud_service_id.workspace_id
    if not workspace:
        return True  # bound-but-no-workspace edge case (separate concern)
    return request.env.user in workspace.access_ids.mapped('user_id')
```

### Sub-model 對等改寫（mcp_tool 走 server_id 反向）

```xml
<record id="mcp_tool_rule_workspace_user" model="ir.rule">
    <field name="domain_force">['&amp;',
        ('server_id.scope', '=', 'user'),
        '|',
        ('server_id.project_id.cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
        '&amp;',
        ('server_id.project_id.cloud_service_id', '=', False),
        ('server_id.project_id.message_partner_ids', 'in', [user.partner_id.id])
    ]</field>
</record>
```

**Sub-model 必須對等改 dual-leg**（[[cross-workspace-task-data-leak]] B1 教訓 — sub-model 漏改 = leak）。

### Migration 對既有 single-leg rule

若 model 已有 single-leg rule，migration 改為 dual-leg：

```python
# src/migrations/18.0.1.0.25/post-migrate.py（範例 — 本 cycle 未走此路因為 PR #47 6-rule split 為新 rule，但對既有 module update path 適用）
def migrate(cr, version):
    # noupdate=0 flip 已由 ir_rules.xml 套用 — 本 migration 只負責對舊狀態 verify
    cr.execute("""
        SELECT id, domain_force FROM ir_rule
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module='woow_paas_platform'
              AND name IN ('mcp_server_rule_workspace_user',
                           'mcp_tool_rule_workspace_user')
        )
    """)
    for rid, domain in cr.fetchall():
        if 'message_partner_ids' not in (domain or ''):
            raise RuntimeError(
                f"Rule {rid} domain still single-leg — "
                "ir_rules.xml update did not propagate. "
                "See [[unbound-project-acl-dual-leg]]"
            )
```

## Why this pattern

### 為什麼是 dual-leg domain 而非兩條獨立 rule

**Option A（dual-leg domain）**：
```xml
<rule>
  domain = '&', scope='user', '|', leg1, leg2
  perms = 1,1,1,1
</rule>
```

**Option B（兩條獨立 rule）**：
```xml
<rule id="bound">  domain = scope='user' AND leg1   perms = 1,1,1,1 </rule>
<rule id="unbound"> domain = scope='user' AND leg2 perms = 1,1,1,1 </rule>
```

Odoo 兩條 same-group rule 用 OR 合併 → 邏輯等價。**選 A 的理由**：

1. **Rule count parity 容易維護**：[[mcp-project-decouple]] Inv #5 要求 `mcp_server` rule count ≡ `mcp_tool` rule count；A 維持 single rule per perm-direction，B 會把 count 翻倍
2. **後人改 perm direction 時不漏改**：A 改 perm_write=False 只動一處；B 要兩處同步改，一漏即 silent divergence
3. **Inline comment 維護單一根據地**：A 用一段 comment 解釋 dual-leg design intent；B 要兩處 comment 容易 drift
4. **Migration 改舊 rule 容易 verify**：A `grep "message_partner_ids" ir_rules.xml` 一行命中；B 兩條 rule 分散，verify SQL 比較囉嗦

### 為什麼外層 AND `scope='user'` 是 MAJOR 1 防線根基（不能拆）

[[system-mcp-create-acl-bypass]] pitfall：dual-leg domain 寫成 `'|', leg, leg'`（沒外層 AND `scope='user'` 鎖），對 new `scope='system'` row 在 create direction 會永真 → user 可 ORM `create({'scope': 'system'})`。

**外層 AND `scope='user'` 對 4 perm direction 行為**：

| Direction | new row scope='system' | new row scope='user' bound | new row scope='user' unbound + follower | new row scope='user' unbound + non-follower |
|-----------|----------------------|---------------------------|------------------------------------------|---------------------------------------------|
| read | block | leg 1 → allow if walk OK | leg 2 → allow if follower | deny |
| create | block | block if walk fail | allow | deny |
| write | block if old scope='system' | leg 1 walk | leg 2 follower | deny |
| unlink | 同上 | 同上 | 同上 | deny |

**禁止動作**：把外層 AND 改 OR（`'|', ('scope', '=', 'user'), ('scope', '=', 'system'), leg1, leg2`） → 立刻 system create direction permissive → MAJOR 1 重炸。

### 為什麼 leg 2 用 `message_partner_ids` 而非其他 candidate

[[cloud-service-project-decouple]] D3=A 已詳述（cycle 1 Q1 拍板原因）：

- **`user_ids`** — Odoo 18 `project.project` 沒這欄位（schema mismatch）
- **`favorite_user_ids` / `collaborator_ids`** — 是 user-level personal pref，不是 access semantics
- **`create_uid` only** — 限制太死，第二位 follower 看不到（workspace collaboration 退化）
- **`message_partner_ids` only** — Odoo 自動把 `create_uid.partner_id` 加進 `message_partner_ids`（不需 dual-leg `create_uid OR follower`）
- **`access_ids` 走 workspace** — 適用於 bound case；unbound 沒 workspace 走不到

`message_partner_ids` 是「Odoo std follower 模型」的延伸 — 與 std `project_public_members_rule`（雖 deactivated per [[cross-workspace-task-data-leak]]）的心智模型對齊；workspace 內 collaboration 透過 explicit `message_subscribe()` 表達。

### 為什麼 cross-workspace 仍 deny（不破 isolation）

跨 workspace user 評估 dual-leg：

- leg 1 false：`cloud_service_id.workspace_id.access_ids.user_id = user.id` — user 不在 victim workspace access_ids 內 → False
- leg 2 false：`project_id.message_partner_ids` 內 → user.partner_id 不在 → False
- OR 兩腿都 False → 整 domain False → deny

`message_partner_ids` 隔離單位是 partner，不是 workspace — 但跨 workspace 的 user 不會被 add 到別 workspace 的 project follower（business 約束），所以 cross-workspace 隔離自然 preserve。

## Anti-patterns

| 反 pattern | 問題 |
|-----------|------|
| `domain = '|', ('scope', '=', 'user'), ('scope', '=', 'system'), leg1, leg2` | 缺外層 AND → MAJOR 1 重炸 |
| `domain = ('scope', '=', 'user'), '|', leg1, leg2_a` （單腿 unbound 條件） | leg 2 沒 follower check → unbound project 全 internal user 可讀 → cross-workspace leak |
| `domain = ('scope', '=', 'user'), '|', leg1, '&', leg2_a, ('project_id.create_uid', '=', user.id)` | 只 creator 可見，limit too much — workspace collaboration 退化 |
| 改 parent rule 但忘改 sub-model rule | B1-style sub-model leak |
| 加新 perm direction（如 `perm_create=False` 收緊）但忘 update test | 後續 PR 改 perm 不會 trip test → silent regression |
| 外層 AND 條件不是 `scope='user'` 而是其他（如 `('active', '=', True)`） | 對「scope direction」失去鎖 — 可能與其他 carve-out 互動產生 unintended permissive case |

## Test scaffolding

### 必加 12-case matrix（4 project state × 3 sub-model × 4 perm direction）

實作上拆 12 個獨立 test method（不 parametrize，self-document method name）：

| H.# | 守護 cell |
|-----|----------|
| H.1 (test_owner_can_read_own_unbound) | UF × S × R — leg 2 allow owner |
| H.2 (test_follower_can_read_unbound) | UF × S × R — leg 2 allow follower |
| H.3 (test_workspace_member_non_follower_cannot_read_unbound) | UN × S × R — leg 2 deny non-follower **（acid test for leg 2 邏輯）** |
| H.4 (test_cross_workspace_user_cannot_read_unbound) | X × S × R — leg 1/leg 2 都 false |
| H.5 (test_mcp_tool_follower_unbound_parity) | UF × T × R — sub-model parity |
| H.6 (test_mcp_tool_cross_workspace_unbound_isolation) | X × T × R — sub-model parity |
| H.7-H.9 (controller carve-out tests) | Layer 2 mirror tests — [[two-layer-acl-lockstep]] |
| H.10 (test_acl_dual_leg_bound_path_preserved) | B × S × R — leg 1 unchanged regression |
| H.11 (test_workspace_user_rule_still_blocks_system_create_after_dual_leg) | * × S × C scope='system' — MAJOR 1 reproducer **（never allow create system row）** |
| H.12 (test_mcp_tool_workspace_user_rule_still_blocks_system_create_after_dual_leg) | * × T × C — sub-model parity for MAJOR 1 |

**H.11 / H.12 是 [[system-mcp-create-acl-bypass]] regression lock — 任何把外層 AND 拆掉的 commit 立刻 trip**。

### Round 2 補加 perm direction matrix（HC-3）

H 段 read direction 已 cover，但 write / unlink direction 對 unbound rows 之前是 implicit-only。Round 2 加：

| H.3b | UN × S × W — `assertRaises(AccessError)` on Dave write external unbound MCP |
| H.4b | X × S × U — `assertRaises(AccessError)` on Bob unlink Alice unbound MCP |

後續 PR 改 leg 2 `perm_write=True/False` 立刻 trip。

## See also

- [[mcp-url-redesign]] §Invariants Inv #11 — 本 pattern 的 invariant lock + 4 禁止動作
- [[mcp-acl-6-rule-split]] — 6-rule split per model triplet（本 pattern 升級為 dual-leg variant 後的 `workspace_user` × 2）
- [[two-layer-acl-lockstep]] — controller layer 對齊 leg 2 walk（Python mirror）
- [[paas-workspace-member-acl-tightening]] — 三件套 ACL 設計（single-leg variant）
- [[cloud-service-project-decouple]] §Q1 — `message_partner_ids` 選擇的 design rationale
- [[system-mcp-create-acl-bypass]] — 外層 AND 拆掉會踩的 pitfall
- [[cross-workspace-task-data-leak]] — sub-model 對等性原則
- 關鍵 code：
  - `src/security/ir_rules.xml` `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` — dual-leg domain 落地
  - `src/tests/test_mcp_acl_unbound_isolation.py` — 12 H-method matrix + H.11/H.12 MAJOR 1 reproducer + H.3b/H.4b HC-3 perm direction
- 上游 Odoo 原理：
  - Polish notation domain — `&` / `|` 前綴 + 2 operand；`('field.path', op, value)` leaf；nesting 必須明示
  - `message_partner_ids` 自動含 `create_uid.partner_id`（Odoo `mail.thread` mixin 預設行為）
