# Pattern: 6-rule split for mixed-scope model（`system_readonly` + `workspace_user` + `admin_full`）

> 適用：同一 model 內有**兩種 row scope**（platform-level public-readable + tenant-level workspace-scoped），且需在 `base.group_user` 開放 CRUD。Single dual-leg rule **不夠**（create direction privilege escalation surface）— 必須按 perm direction 拆 3 條 + sub-model 對等鏡像 = 共 6 條。
>
> **2026-05-21 更新（cycle 4 [[mcp-url-redesign]] 落地）**：`workspace_user` × 2 條 rule 升級為 **dual-leg variant**（合 #51） — leg 1 bound walk（原樣）+ leg 2 unbound carve-out via `message_partner_ids`。詳見下方 §Dual-leg variant 段 + [[unbound-project-acl-dual-leg]] pattern。其餘 4/6 條（`system_readonly` × 2 + `admin_full` × 2）**不動**。

## When to use

當下列條件**全部**成立：

1. Model 有 `scope`-like 二元欄位（如 `scope='system' | 'user'`，或 `is_public bool` 等），不同 scope 的 row **可見性 / 可寫性** 不同。
2. 某 scope（通常是 `system` / `public`）對所有 `base.group_user` **read 可見**（platform-level 公共資源）。
3. 另一 scope（通常是 `user` / `tenant`）對 workspace member **CRUD 可變**（tenant-level 私有資源）。
4. 後台 admin (`group_system`) 需要對所有 scope 全 CRUD（特別是 system / public 的維護）。
5. Model 有 sub-model（child）透過 FK 反向綁定（如 `mcp_tool.server_id → mcp_server`），sub-model 沒自己的 scope 欄位但要繼承 parent 的 scope 視角。

**典型情境（本 codebase 真實 case）**：

```
mcp_server  scope='system'  ─ admin 後台管理，所有 internal user 透過 ai.assistant.mcp_server_ids 消費
            scope='user'    ─ workspace 內 sidecar 的 wrapper，workspace member 自家管理

mcp_tool    server_id ───►  mcp_server
            ← sub-model，沒有自己的 scope 欄位，透過 server_id.scope 反向決定可見性
```

**不適用**：

- 全 model row 同 scope（用 [[paas-workspace-member-acl-tightening]] 三件套即可，single rule per model）
- 沒有 admin 後台維護需求（不需要 `admin_full` rule — 但少 model 真的沒這需求，通常是 missing case）
- Model 在 Odoo std model 上（套 [[odoo-addon-std-rule-symmetric-deactivation]]，要額外 deactivate std rule）

## What it looks like

### Per-model triplet（3 條 rule for parent）

```xml
<!-- 1. system-scope read-only (all internal users) -->
<record id="mcp_server_rule_system_readonly" model="ir.rule">
    <field name="name">McpServer: system-scope read-only (all internal users)</field>
    <field name="model_id" ref="model_woow_paas_platform_mcp_server"/>
    <field name="domain_force">[('scope', '=', 'system')]</field>
    <field name="groups" eval="[(4, ref('base.group_user'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="False"/>
    <field name="perm_create" eval="False"/>
    <field name="perm_unlink" eval="False"/>
</record>

<!-- 2. workspace-scope CRUD (workspace members on own user-scope) — dual-leg as of cycle 4 -->
<record id="mcp_server_rule_workspace_user" model="ir.rule">
    <field name="name">McpServer: workspace member CRUD on user-scope</field>
    <field name="model_id" ref="model_woow_paas_platform_mcp_server"/>
    <!--
        Dual-leg (cycle 4):
        - 外層 AND ('scope','=','user') 鎖 MAJOR 1 防線（system create direction deny）
        - 內層 OR: leg 1 bound walk OR leg 2 unbound + follower carve-out
        詳見 [[unbound-project-acl-dual-leg]] pattern + [[mcp-url-redesign]] Inv #11
    -->
    <field name="domain_force">['&amp;',
        ('scope', '=', 'user'),
        '|',
        ('project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id),
        '&amp;',
        ('project_id.cloud_service_ids', '=', False),
        ('project_id.message_partner_ids', 'in', [user.partner_id.id])
    ]</field>
    <field name="groups" eval="[(4, ref('base.group_user'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>

<!-- 3. admin full CRUD (group_system bypass) -->
<record id="mcp_server_rule_admin_full" model="ir.rule">
    <field name="name">McpServer: admin full CRUD (group_system bypass)</field>
    <field name="model_id" ref="model_woow_paas_platform_mcp_server"/>
    <field name="domain_force">[(1, '=', 1)]</field>
    <field name="groups" eval="[(4, ref('base.group_system'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>
```

### Sub-model mirror（再 3 條 rule，透過 `server_id` 反向）

```xml
<record id="mcp_tool_rule_system_readonly" model="ir.rule">
    <field name="domain_force">[('server_id.scope', '=', 'system')]</field>
    <!-- perms / groups 同 mcp_server_rule_system_readonly -->
</record>

<record id="mcp_tool_rule_workspace_user" model="ir.rule">
    <!-- Dual-leg sub-model mirror（cycle 4），所有 leaf 前綴 server_id. -->
    <field name="domain_force">['&amp;',
        ('server_id.scope', '=', 'user'),
        '|',
        ('server_id.project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id),
        '&amp;',
        ('server_id.project_id.cloud_service_ids', '=', False),
        ('server_id.project_id.message_partner_ids', 'in', [user.partner_id.id])
    ]</field>
    <!-- perms / groups 同 mcp_server_rule_workspace_user -->
</record>

<record id="mcp_tool_rule_admin_full" model="ir.rule">
    <field name="domain_force">[(1, '=', 1)]</field>
    <!-- perms / groups 同 mcp_server_rule_admin_full -->
</record>
```

### ACL CSV

```csv
# parent
access_mcp_server_user,mcp_server user,model_woow_paas_platform_mcp_server,base.group_user,1,1,1,1
access_mcp_server_admin,mcp_server admin,model_woow_paas_platform_mcp_server,base.group_system,1,1,1,1

# sub-model
access_mcp_tool_user,mcp_tool user,model_woow_paas_platform_mcp_tool,base.group_user,1,1,1,1
access_mcp_tool_admin,mcp_tool admin,model_woow_paas_platform_mcp_tool,base.group_system,1,1,1,1
```

CSV 給 `1,1,1,1` 是 hard cap，ir.rule 細控 row-level（per-scope split 用 record rule 表達是 Odoo idiom）。

## Why this pattern

### 為什麼不用 single dual-leg rule（原 plan 設計 + MAJOR 1 fix 理由）

Round 1 plan 用 single dual-leg rule：

```xml
<record id="mcp_server_rule_workspace_member" model="ir.rule">
    <field name="domain_force">['|', ('scope', '=', 'system'), ('project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id)]</field>
    <field name="groups" eval="[(4, ref('base.group_user'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>
```

CAQA 把這個設計 verify 為 ✅（Invariant #6 「system leg always-true 是故意設計」）。
**Reviewer round 1 MAJOR 1 抓到**：always-true leg 對 read 是設計，但對 create 是**privilege
escalation surface**：

- ORM `env['mcp_server'].create({'scope': 'system', ...})` 時，Odoo 對 new record 套 record rule
- Leg 1 `('scope', '=', 'system')` 對 new row 為真 → rule allow create
- D4 constraint 允許 `scope='system' + project_id=NULL` → ORM 層通過
- 新 row 立刻被 same rule 的 read leg 1 永真 → 全 instance 可讀
- Admin 在 `ai.assistant.mcp_server_ids` 後台不慎選了此 row → 所有 user chat trigger attacker endpoint

詳見 [[system-mcp-create-acl-bypass]]。

### 為什麼是 split 而非加 perm CSV gate

**Option B（CSV `perm_create=0`）行不通**：CSV access right 是 model-level binary cap，無法表達
「user 可建 `scope='user'` / 不可建 `scope='system'`」這種 row-level 區分。把 CSV user row
`perm_create=0` 會堵掉**所有** create — 含合法的 user-scope create — 等於把 row-level
語意決定塞錯 layer。

**Option C（accept as design + 標 invariant 不擋）也不行**：CAQA Inv #6 的「always-true leg 是 intentional」
前提是「system MCP 只有 admin / sudo create」— 但**沒 enforce 這個前提**。Accept = 把假設當現實，
留 known security hole 給 reviewer 標 MAJOR；違反 [[cross-workspace-task-data-leak]] 教訓
「ACL 補丁不解 root cause」。

**Split 把 read direction 與 create direction 分到不同 rule** — record rule 直接 enforce「per-scope
perm 不同」是 Odoo idiom。既有 codebase pattern `workspace_rule_member_read`（read-only）+
`workspace_rule_anyone_create`（create-only）已是 split idiom（見 `src/security/ir_rules.xml`）。

> ⚠ 兩處更正（#658）：這兩個名字原本是 `workspace_rule_member_access` /
> `workspace_rule_create`，已由 `src/migrations/18.0.1.0.131/post-migrate.py` 退役改名；
> 且原本那條 `workspace_rule_member_access` **不是 read-only**（`perm_read` 與 `perm_write`
> 都是 True、domain 無 role 條件，任何 guest 成員都能寫 workspace 本體——那正是 #658 的洞）。
> 引用它當「read/create split」的樣板在當時就不準確；現在的 `workspace_rule_member_read`
> 才真的是 read-only。行號指標一併移除（會漂）。

### 為什麼需要第 3 條 `admin_full` rule（不能省）

直觀想：admin 在 CSV 上有 `access_mcp_server_admin` 走 `group_system`，
RWCU 都是 `1,1,1,1` — 為什麼還需要 `admin_full` ir.rule？

**Odoo per-group rule OR semantics**（`ir_rule.py:140-168`）：

- `group_system` 透過 `_group_implied_ids` 繼承 `group_user`
- Admin 的 effective groups = `{group_system, group_user, ...}`
- Read domain = `OR_per_group(rules for each effective group)`
- 但 **每個 group 的 rules 是內部 OR** — `system_readonly.perm_create=False` 套在 `group_user`
  上 = group_user 的 effective create 為 `False`
- Admin 透過 group_user 那條 path 失敗 → 後台 form `[Save]` 時 Odoo 對該 user 評估，
  其中一條 fail → AccessError

實際 reproducible 症狀：admin 後台「Apps → MCP Server → Create」按 save → `AccessError: You are not
allowed to create...`。

**`admin_full` 套 `group_system` + `domain=[(1,'=',1)]` short-circuit deny** — admin 透過 group_system
那條 path 全 pass，其他 path 失敗也無妨（per-group OR = 只要一條 group 全 pass 就 pass）。

**省略 `admin_full` 的失敗模式（partial failure）**：
- Admin read 後台 list 正常（system_readonly perm_read=True）
- Admin create 後台 form 失敗 → 後台維護路徑壞掉
- 比「全部壞掉」更難 debug — admin 看得到 list 但點 create 失敗，第一反應會找 form / view 問題
  而非 rule 問題

### B1 sub-model parity（為什麼 mcp_tool 要對等 3 條）

[[cross-workspace-task-data-leak]] B1 pitfall：parent rule 加了但 sub-model rule 漏 = sub-model leak。
本 case `mcp_tool` 沒有自己的 `scope` 欄位，透過 `server_id` 反向走 — 但 ir.rule 仍要鏡像：

- 漏 `mcp_tool_rule_system_readonly` → user 可對既有 system MCP 偽造 tool 定義（perm_create
  on mcp_tool via base CSV `1,1,1,1` + 無 rule deny）
- 漏 `mcp_tool_rule_workspace_user` → cross-workspace user 可 search 別人 workspace 的 tool
- 漏 `mcp_tool_rule_admin_full` → admin 後台維護 system MCP 時加 tool 失敗

3 條 mcp_server rule + 3 條 mcp_tool rule = 6 條對等鏡像。**rule count parity 是 invariant**
— 任何 PR 改 ir_rules.xml 內 mcp_server 數量 ≠ mcp_tool 數量視為 MAJOR until proven safe
（reporter 列入 follow-up R2-2 — automated lint check 是 future hardening）。

### Domain walk path 一致性

Sub-model domain 必須走 `server_id` 反向 — 即 parent domain 前綴 `server_id.`：

```python
# parent
domain = [('scope', '=', 'system')]

# sub-model
domain = [('server_id.scope', '=', 'system')]
```

```python
# parent workspace chain — dual-leg as of cycle 4 (見 [[unbound-project-acl-dual-leg]])
domain = [
    '&', ('scope', '=', 'user'),
    '|', ('project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id),
    '&', ('project_id.cloud_service_ids', '=', False),
         ('project_id.message_partner_ids', 'in', [user.partner_id.id]),
]

# sub-model — dual-leg sub-model 對等 (server_id. 前綴)
domain = [
    '&', ('server_id.scope', '=', 'user'),
    '|', ('server_id.project_id.cloud_service_ids.workspace_id.access_ids.user_id', '=', user.id),
    '&', ('server_id.project_id.cloud_service_ids', '=', False),
         ('server_id.project_id.message_partner_ids', 'in', [user.partner_id.id]),
]
```

任何 sub-model domain 與 parent domain 不對稱 = ACL 行為 drift（cross-workspace user 透過 sub-model
路徑可能看到不該看的 row）。

**Dual-leg 對稱原則**：parent 加 leg 2 unbound carve-out 時，sub-model 兩個 leaf 都要前綴 `server_id.`
— 漏一邊 = leg 2 walk 走錯 model → 完全不工作或意外 always-true。

## Test scaffolding（必備 regression）

每個 6-rule split 都要對應 **6 conditions × 3 perm directions** 至少的 regression test，避免 partial-correct
fix 漏網。本 codebase（commit `97b5313` tester port + regression）已落 9 條：

| Test | 守護 |
|---|---|
| `test_user_cannot_create_system_mcp_via_orm` | `system_readonly.perm_create=False` 守住 base.group_user create direction（MAJOR 1 invariant） |
| `test_user_cannot_create_mcp_tool_for_system_server` | sub-model 對等（B1） |
| `test_user_cannot_write_or_unlink_system_mcp` | `system_readonly` perm_write / perm_unlink false |
| `test_sudo_can_create_system_mcp` | `_model_auto_create_mcp_server` + demo data 路徑保留 |
| `test_admin_can_create_system_mcp_via_backend` | `admin_full` 真的生效（partial failure prevention） |
| `test_user_can_still_create_user_scope_in_own_workspace` | Over-correction guard — split 後合法 user-scope create 不被連坐堵 |
| `test_alice_cannot_read_bob_user_mcp_via_orm` | `workspace_user.domain` 跨 workspace deny |
| `test_alice_cannot_read_bob_user_mcp_tool` | sub-model 對等 deny |
| `test_system_mcp_readable_by_all_users` | `system_readonly` always-true 是 intentional design |

**Cycle 4 dual-leg variant 補 12 條（`test_mcp_acl_unbound_isolation.py` H 段）**：

| Test | 守護 |
|---|---|
| H.1 / H.2 | leg 2 unbound carve-out allow（owner / follower 兩條） |
| H.3 / H.4 | leg 2 unbound deny（non-follower / cross-workspace 兩條 — leg 2 邏輯 acid test）|
| H.5 / H.6 | sub-model parity（mcp_tool 對等 dual-leg）|
| H.7 / H.8 / H.9 | controller layer mirror（[[two-layer-acl-lockstep]]）|
| H.10 | leg 1 bound walk regression preserved |
| **H.11 / H.12** | **MAJOR 1 重炸防線**：外層 AND `scope='user'` 鎖 system create direction，Charlie 對 ORM `create({'scope': 'system'})` 必 `AccessError`（sub-model 對等）|
| H.3b / H.4b (HC-3 round 2) | perm direction matrix — Dave / Bob write/unlink unbound external MCP 必 `AccessError` |

少任何一條 = partial-correct fix（fix 一邊漏另一邊）可能逃過 review。

## 後人改本區前 checklist

改 `ir_rules.xml` 內任何 mcp_server / mcp_tool rule 前，依序檢查：

1. **要加新 rule 嗎？** → 同時加 parent + sub-model 對等 → mcp_server rule count == mcp_tool rule count
2. **要改既有 rule 的 domain 嗎？** → parent + sub-model 同步改（sub-model 前綴 `server_id.`）
3. **要改 `workspace_user` 的 dual-leg 嗎？**（cycle 4 升級）→
   - 外層 AND `('scope', '=', 'user')` **不能拆**（拆 = 重炸 MAJOR 1）
   - leg 1 bound walk **不能改 always-true**（改 = cross-workspace leak）
   - leg 2 unbound carve-out **不能改 always-true**（改 = 跨 workspace user 讀 unbound）
   - leg 2 必須 mirror controller layer carve-out（[[two-layer-acl-lockstep]] — `ai_assistant._check_project_access` + `paas._resolve_project_access`）
4. **要改 perm_* 嗎？** → 想清楚 read / write / create / unlink 4 direction 各自的攻擊面（特別是 always-true domain leg + perm_create=True 組合）
5. **想合併回 dual-leg single rule？** → STOP，重炸 reviewer MAJOR 1（[[system-mcp-create-acl-bypass]]）
6. **想拿掉 `admin_full` rule？** → STOP，admin 後台維護路徑會壞（partial failure mode）
7. **跑 21 regression test**：原 9 (PR #47) + dual-leg 12 (H.1–H.12 cycle 4) + perm direction 4 (H.3b/H.4b round 2)；`docker compose run --rm web odoo -d ... --test-tags "/woow_paas_platform:TestMcpAclCrossWorkspaceIsolation,TestMcpAclUnboundIsolation"` — 全綠才能 commit

## See also

- [[mcp-project-decouple]] §Invariants #5 + #6 + #8 — 本 pattern 的 invariant lock（cycle 2 落地點）
- [[mcp-url-redesign]] §Invariants Inv #11 — cycle 4 dual-leg 升級的 invariant + 4 禁止動作
- [[unbound-project-acl-dual-leg]] — `workspace_user` × 2 dual-leg 升級的 detailed pattern + 12 H-method matrix
- [[two-layer-acl-lockstep]] — controller layer 與 ir.rule 對齊（leg 2 mirror）
- [[paas-workspace-member-acl-tightening]] — single-scope 模型的 3 件套（本 pattern 的「mixed scope」變體）
- [[odoo-addon-std-rule-symmetric-deactivation]] — Odoo std model 的對等 deactivation（mcp 是自家 model 不適用，但「ACL 對等」原則一樣）
- [[system-mcp-create-acl-bypass]] — 不走本 pattern 會踩的 MAJOR 1 漏洞
- [[cross-workspace-task-data-leak]] — B1 sub-model parity 教訓的根源案例
- 關鍵 code：
  - `src/security/ir_rules.xml` `mcp_server_rule_workspace_user` + `mcp_tool_rule_workspace_user` — cycle 4 dual-leg domain
  - `src/security/ir_rules.xml` `mcp_server_rule_system_readonly` + `mcp_tool_rule_system_readonly` + `mcp_server_rule_admin_full` + `mcp_tool_rule_admin_full` — 4/6 不動 rule
  - `src/security/ir.model.access.csv:14,16` — CSV `1,1,1,1` 為 hard cap
  - `src/migrations/18.0.1.0.24/post-migrate.py:160-180` — 舊 dual-leg rule retire（step 5，cycle 2）
  - `src/tests/test_mcp_acl_isolation.py` — 9 regression cases（cycle 2）
  - `src/tests/test_mcp_acl_unbound_isolation.py` — 12 H-method matrix + HC-3 round 2 perm direction（cycle 4）
- 上游 Odoo 原理：
  - `addons/base/models/ir_rule.py:140-168` — per-group rule OR semantics（admin_full 必要的根因）
  - Odoo `_group_implied_ids` — group_system 繼承 group_user 的機制
