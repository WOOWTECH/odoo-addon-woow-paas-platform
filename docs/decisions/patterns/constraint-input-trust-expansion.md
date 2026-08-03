# Pattern: constraint / ir.rule 信任 attacker-controlled m2m input → 必加 grantor authority Python constraint

> 適用：model 上有「attacker 透過 ORM `write({'m2m_field': [(4, X)]})` 可加 row 的 Many2many field」，且 model 的 ir.rule / `@api.constrains` 用該 m2m field 做 subset / scope / visibility check。ir.rule 對 m2m write direction 評估「任一現存 element 滿足 user domain」即放行（Odoo ORM 內建 OR semantics）— attacker 用「自己已有合法 grantor 的 element」誘騙 rule pass 後實際把 row 寫到 victim 沒授權的 element 上 → silent IDOR-like 擴張 allowed set。**ir.rule 不夠**，必須加 Python `@api.constrains('m2m_field')`：每個 new element 都驗 `env.user` 對該 element 有 grantor authority（admin/owner role）。SUPERUSER + `base.group_system` bypass（合法 cron / migration / admin curation 跨 element 寫入路徑保留）。

## When to use

當下列條件同時成立：

1. **Model 有 Many2many field 對 attacker 可寫**（CSV ACL `perm_write=True` 或 ir.rule 對某 group 給 W 動作）— 典型如 `agent_template.project_ids`、`agent_template.mcp_server_ids`、`agent_template.skill_ids`、任何 binding M2M
2. **同 model 有 ir.rule / `@api.constrains` 用該 m2m field 做 access / subset / visibility / authority 推導**（如 `agent_template_rule_project_admin` domain `('project_ids.project_access_ids', 'any', [('user_id', '=', user.id), ('role', 'in', ['owner', 'admin'])])`、`agent_template._check_mcp_subset` 計算 `t.project_ids.mapped('mcp_server_ids')`）
3. **Attacker 對 m2m 內「至少一個 element」是合法 grantor**（如 attacker 是 project A 的 owner / admin）— 不是「完全沒授權」的 case
4. **m2m 對 model 的 visibility / behavior 有 fan-out 效果**（如「template 綁 [A, B]」表示「B 的 member 可讀 template」、「template 內的 MCP 可在 B 的 agent runtime active」）

**典型情境（本 codebase 真實 case，2026-05-22 Phase A reviewer HC-3）**：

```
agent_template
  ├── project_ids M2M project.project   ◄── attacker-writable
  ├── mcp_server_ids M2M mcp_server     ◄── 跟 project_ids fan-out
  └── ir.rule agent_template_rule_project_admin
        domain = ('project_ids.project_access_ids', 'any',
                  [('user_id', '=', user.id),
                   ('role', 'in', ['owner', 'admin'])])

Attacker (A admin):
  1. create T project_ids=[A]                         ← 通過（A 滿足 'any'）
  2. write T project_ids=[(4, victim_B)]              ← ir.rule 評估 [A, B]，A 仍滿足 'any' → write 通過
  3. T 現在綁 B                                       ← cross-project visibility leak
  4. Phase D activation：B admin 把 T 設成 agent_template_id
                                                       → attacker controlled system_prompt
                                                         + mcp_server_ids active on B's agent runtime
                                                       → cross-project agent runtime hijack
```

**Pattern 同 family（兼具歷史 context）**：

- PR #54 cycle 4 HC-2 `paas.py:_resolve_project_access` 對 unbound non-follower 不收緊 — Layer 2 carve-out 信任 attacker 已在自己 project 有 access 的事實，silent 把 ROLE_OWNER 給沒 access 的 unbound project。差別是「rule 信任 attacker-writable carve-out condition」vs「rule 信任 attacker-writable m2m membership」— 抽象起來都是「ACL 評估時信任 attacker 可影響的 input」
- agent_template `_check_mcp_subset` 對 attacker-writable `project_ids` 做 `t.project_ids.mapped('mcp_server_ids')` subset check — 同 m2m 邏輯：attacker 加 B 進 project_ids → B's user-scope MCP 跟著被視為合法 subset；雖然 HC-3 Python constraint 從 grantor authority 端切斷，但 reader 要明白 `_check_mcp_subset` **不能單獨依賴自己的 subset 邏輯擋 attack** — 必須與 grantor constraint 配合

**不適用**：

- m2m field 對 attacker 完全 read-only（如 `created_by_uid` related m2m）— write 不可達，不需 grantor check
- m2m 內 element 對 model 的 effect 是 cardinality-1 而非 fan-out（如 `default_skill_ids` 只給 default seeding，不影響 visibility）— attacker 加 element 沒提升攻擊面
- 已用「m2m relation table 的 ir.rule」表達 grantor check（Odoo 不支援，但若未來 Odoo 18+ 加 m2m relation table 級 rule，可改走那條路徑）
- Model 設計成「m2m 內 element 對任何 user public visible」（如 marketplace tag）— 沒 grantor concept

## What it looks like

### 1. Python `@api.constrains('m2m_field')` — 對每個 element 都驗 grantor

```python
class AgentTemplate(models.Model):
    _name = 'woow_paas_platform.agent_template'

    project_ids = fields.Many2many(
        'project.project',
        relation='agent_template_project_rel',
        string='Bound Projects',
    )

    @api.constrains('project_ids')
    def _check_project_ids_grantor_authority(self):
        """Grantor (current env.user) must have admin/owner role on every bound project.

        Without this Python constraint, ir.rule `'any'` operator over M2M evaluates
        OR-semantics: any single project in `project_ids` satisfying user's admin
        domain passes the rule — attacker on A can write project_ids=[(4, B)] using
        A as the satisfying element. ir.rule cannot enforce per-element grantor on
        M2M write direction, only Python constrain can.

        Pattern: constraint-input-trust-expansion. See pattern doc for the full
        attack chain and bypass logic.
        """
        # ⚠ bypass 判準必須是 env.is_superuser()，不可寫 env.uid == SUPERUSER_ID：
        # Odoo 的 sudo() 只把 env.su 設 True，**uid 不變** —— odoo/models.py:6290-6291
        # 的 sudo() docstring 逐字：「The superuser mode does not change the current
        # user, and simply bypasses access rights checks.」
        # 用 uid 比對會讓所有 .sudo() 合法寫入路徑掉進 grantor 檢查 → 假性 ValidationError。
        if self.env.is_superuser() or self.env.user.has_group('base.group_system'):
            return  # bypass — admin / migration / cron all need to write across projects

        grantor_partner = self.env.user.partner_id
        for tmpl in self:
            for project in tmpl.project_ids:
                has_authority = self.env['woow_paas_platform.project_access'].sudo().search_count([
                    ('project_id', '=', project.id),
                    ('partner_id', '=', grantor_partner.id),
                    ('role', 'in', ('owner', 'admin')),
                ]) > 0
                if not has_authority:
                    raise ValidationError(
                        "Cannot bind template '%s' to project '%s': "
                        "you must have admin or owner role on the target project."
                        % (tmpl.name, project.name)
                    )
```

### 2. ir.rule 與 Python constraint 對應 cell map

| Direction | ir.rule responsibility | Python constraint responsibility |
|-----------|----------------------|---------------------------------|
| **Read** | Filter visibility per current user role on each row | (no override) |
| **Write (m2m delta)** | OR-semantics over post-write m2m membership — **insufficient** | Per-element grantor authority — **closes ir.rule blind spot** |
| **Create** | 'any' over initial m2m — **insufficient** if attacker seeds with own grantable element + adds victim | Same constraint also triggers on create — covers create-time injection |
| **Unlink** | Per-row | (no override unless unlink path can carry escalation — usually not) |

**重點**：rule 與 constraint **不是 redundant**；是分工：

- Rule 守 row-level read visibility（matrix per-row）
- Constraint 守 m2m field-level write authority（matrix per-element），同時 cover create direction（避免 attacker create 時直接帶 victim element）

### 3. Test 三條獨立 lock

```python
def test_attacker_admin_on_A_cannot_extend_template_to_B(self):
    """HC-3 attack chain step 2 — attacker write project_ids=[(4, B)] raises ValidationError."""
    self.charlie.has_admin_on(self.project_a)  # setup: charlie is admin on A
    template = self._create_template(project_ids=[self.project_a.id]).with_user(self.charlie)
    with self.assertRaises(ValidationError):
        template.write({'project_ids': [(4, self.project_b.id)]})

def test_attacker_admin_on_both_A_B_can_extend(self):
    """Bypass path proof — legitimate cross-project binding by grantor of both."""
    self.charlie.has_admin_on(self.project_a)
    self.charlie.has_admin_on(self.project_b)
    template = self._create_template(project_ids=[self.project_a.id]).with_user(self.charlie)
    template.write({'project_ids': [(4, self.project_b.id)]})  # no raise

def test_group_system_can_extend_template_cross_project_without_grantor_role(self):
    """Bypass path proof — admin curation by base.group_system bypasses constraint."""
    eve = self._create_user_with_groups(['base.group_system'])
    template = self._create_template(project_ids=[self.project_a.id]).with_user(eve)
    template.write({'project_ids': [(4, self.project_b.id)]})  # no raise
```

**3 條最小集**：attack reproducer + grantor 雙重 authority 通行 + admin bypass 通行。第三條缺 = future regression 把 admin path 一起擋掉 / 與 ir.rule create direction 雙 deny → admin 後台 form / cron 寫不進去 silently。

## Why this pattern

### 為什麼 ir.rule 對 m2m write direction 不足

Odoo ORM 對 m2m field 的 ir.rule evaluation：

```
# write direction，post-write state evaluation
domain = ('project_ids.project_access_ids', 'any',
          [('user_id', '=', user.id), ('role', 'in', ('owner', 'admin'))])
```

`'any'` operator 對 m2m field 是 SQL `EXISTS` — 對 post-write 集合 `{A, B}`，只要任一 row 滿足內 sub-domain（user_id + role 同 row）即 True。

Attacker 對 A 是 admin → A 滿足 sub-domain → `'any'` True → rule pass → write 通過。但實際 m2m 已多 B → fan-out effect active。

**Why 不改 rule 改 'all'？**：

- Odoo ORM 不提供 `'all'` operator over m2m walk
- 即使 reverse domain 寫成 `('project_ids', 'not in', allowed_projects)` — `allowed_projects` 是 SQL sub-query 依賴 user，需要動態算；Odoo `noupdate=0` domain XML 表達不了
- `('project_ids', 'in', [allowed_ids])` 用 sub-select 表達 — 但 ir.rule domain XML 只能 static / pseudo-variable（`user.id` / `user.partner_id.id`），不支援 sub-query
- Python `@api.constrains` 才有完整 PL/Python 能算每 element

### 為什麼必須 cover create direction

Create direction 也走 `@api.constrains('project_ids')` trigger — Odoo 在 create 後同樣評估 constrains。

**Attack via create**（更陰）：

1. Attacker 對 A 是 admin，對 victim B 沒授權
2. ORM `create({'project_ids': [(6, 0, [A, B])], ...})` — 帶 B 進 create payload
3. ir.rule create direction：對 post-create m2m `{A, B}` 評估 `'any'` → A 滿足 → True → create 通過
4. Constraint trigger：loop `[A, B]` — B 不通過 → raise ValidationError → create rollback ✓

**若 constraint 只 watch write direction**（typical mistake — 用 `write()` override 而非 `@api.constrains`）→ create path 完全沒守 → 1-3 attack vector active。**用 `@api.constrains` 不用 `write()` override**：

- `@api.constrains` 同時 cover create + write
- `write()` override 只 cover write，要對等加 `create()` override，雙倍維護
- `@api.constrains` 走 Odoo std validation pipeline，與 `_check_visibility_consistency` / `_check_mcp_subset` 同層級

### 為什麼 SUPERUSER + `base.group_system` bypass

合法 cross-element write 路徑：

- **Migration**: `migrate(cr, version)` 走 SUPERUSER；可能要 seed initial templates 綁多 project
- **Cron**: 系統 cron 走 SUPERUSER，可能要 garbage collect / re-bind
- **paas-operator webhook**: Phase B-D 加入；走 sudo session（API key auth）
- **Admin backend form**: `base.group_system` user 透過 form view 直接 attach / detach project — admin 是「跨 project」curator，不可能對每 project 都拿 owner/admin role

**Bypass 路徑風險評估**：

- SUPERUSER bypass：判準是 `env.is_superuser()`（即 `env.su`），涵蓋兩種入口：uid 真的是 1 的 root env（install hook / migrate / `api.Environment(cr, SUPERUSER_ID, {})`），以及任何 `.sudo()` recordset。**`sudo()` 不改 uid，只把 `su` 設 True** —— `odoo/models.py:6290-6291` 的 docstring 逐字：「The superuser mode does not change the current user, and simply bypasses access rights checks.」；`odoo/api.py` 的 `__new__` 又有 `if uid == SUPERUSER_ID: su = True`，所以 `is_superuser()` 是 `uid == SUPERUSER_ID` 的**嚴格超集**。**不可**用 `env.uid == SUPERUSER_ID` 代替 —— 那會漏掉全部 `.sudo()` 路徑，把合法 seeding / bootstrap 一起擋掉（silent functional regression，見下方 Anti-patterns 表）。
  ⚠ 「cron 走 SUPERUSER」這句要小心：`ir.cron` 以 cron 記錄上的 `user_id` 執行，常是 admin（uid=2）而非 OdooBot（uid=1），那種情況兩種判準都不放行、只靠 `base.group_system` 那一腿過。
- `base.group_system` bypass：Odoo std `Settings` 群，比 `base.group_user` 高一階；一般 tenant user 沒 group_system；本 PaaS 平台 group_system 通常只給 platform admin（操作 backend）

**surface 有多窄，要看今天有沒有 tenant-reachable 的 sudo write-site**：把 bypass 從 `uid == 1` 放寬到 `is_superuser()`，等於把「任何 tenant 可觸發、且伺服器端有 `.sudo()` 的寫入路徑」都納入 bypass —— 而本 repo 的 tenant 請求**大量**在伺服器端走 `.sudo()`（例：`src/controllers/ai_assistant.py:1554` `request.env['project.project'].sudo().create(vals)`）。實查：今天**零個** tenant-reachable 的路徑會寫 `agent_template.project_ids`（`src/controllers/` 與 `src/services/` 內對 agent_template 的引用全是 resolve / read，無 `.sudo().write({'project_ids'…})`）。這是**今日事實不是結構保證**——新增這種 write-site 時必須重估。

**⚠ 兩種語意不要混用（`is_superuser()` vs `uid == SUPERUSER_ID`）**：

| 用途 | 正確判準 | 理由 |
|------|---------|------|
| **Bypass guard**（constraint / ACL 檢查要放行系統路徑） | `env.is_superuser()` | 必須涵蓋 `.sudo()`；漏掉 = 擋死合法 seeding / bootstrap |
| **Branch selector**（判「這個 env 背後有沒有真人」，決定記名給誰） | `env.uid == SUPERUSER_ID` | 要的是「真 root env」；`.sudo()` 背後仍有真人，其 `env.user` 才是該記名的對象 |

本 repo 的 bypass guard 都用 `is_superuser()`（`agent_template.py:108`、`project_access.py:136`）。
另有一處用 `uid == SUPERUSER_ID`：`project_project.py:314`（下方 §Forward-looking invariant 的 owner-bootstrap）。

> 🔴 **對那一行，本 doc 只下「載重」的判斷，不下「刻意」的判斷。**
>
> **載重是可證的**：`/woow` 自助建 project 走 `ai_assistant.py:1554` 的
> `project.project.sudo().create()`（uid 是租戶本人、su=True），現況落 else 分支
> → `:328 partner = self.env.user.partner_id` → `:341` 建 owner 列。若改成 `is_superuser()`
> 會落 if 分支、無 hint 即 `:317 return projects` ⇒ **租戶建的 project 一列 `project_access` 都沒有**
> （孤兒：看不到、進不去、刪不掉，後果見 `src/controllers/paas.py:353` 註解）。
> 而全 repo 建立 owner 列的地方就只有 `:341` 這一處。
>
> **「刻意」則未經證實**：同一個檔案 `:293-295` 的註解、以及
> `src/tests/test_agent_pod_phase_d_hardening.py` 的兩處 docstring / 斷言訊息，
> 原本都把 `.sudo()` 寫成「進得了 SUPERUSER 分支」——**三處印著相反的心智模型**
> （本次 #547 已一併更正這三處文字，但那只證明今天的文字對，不證明當初的判準是設計出來的）。
> 而且在本次補測之前，**沒有任何測試鎖住它**：F-PA-14 的三條測試裡，兩條在 uid 已是 1 的
> `TransactionCase` env 上呼叫 `.sudo()`（依 `odoo/models.py` 的 `if flag == self.env.su: return self`
> 是 no-op），一條是 `.with_user()` 不帶 `.sudo()`（su=False）——三條在兩種判準下都綠。
>
> ⇒ 它在行為上恰好是對的。**把良性巧合封成規範，就是下一輪的 #547**，所以本 doc 只把它列為
> 「改了會壞、動之前先看測試」，不把它當作「兩種語意」的正面示範。真正的守線是那條
> regression test（見 §Test scaffolding 的 HC3.7）。

### 為什麼比 [[two-layer-acl-lockstep]] 更窄但 cross-cut

[[two-layer-acl-lockstep]] 守的是「同一 ACL invariant 在 ir.rule + controller carve-out 兩層獨立實現，必須對等收緊」— 屬於「rule 與 controller helper 對齊」維度。

本 pattern 守的是「rule 對 attacker-writable m2m input 評估時的 OR-semantics 寬鬆化 hole」— 屬於「rule expression power 不足 → Python 補 PL/Python」維度。

兩 pattern **互補**：

- [[two-layer-acl-lockstep]] 假設 ir.rule **本身** 是 secure expression，controller layer 必須 mirror；
- 本 pattern 處理 ir.rule **本身 expression power 不足** 的 case，Python constraint 補 the missing direction

具體配合方式：

```
agent_template.project_ids 寫 attempt
  │
  ├── Layer 1 (ir.rule write direction)
  │     domain ('project_ids.project_access_ids', 'any', ...)
  │     ◄── OR-semantics 對 [A, B] 寬鬆化 (A 滿足即 pass)
  │     ◄── 本 pattern: ir.rule 不夠 → 加 Python @api.constrains
  │
  ├── Layer 2 (Python @api.constrains('project_ids'))
  │     ◄── per-element grantor check
  │     ◄── 對 [A, B] 各別驗，B 不通過 → raise
  │
  └── Layer 3 (controller carve-out, Phase B-E)
        ◄── 預期：Phase B-E 加 controller endpoint `POST /api/.../templates/<id>/bind`
            時，必須在 sudo path 之前再 mirror Python grantor check
            (對齊 [[two-layer-acl-lockstep]])
```

## Anti-patterns（後人改本區前 STOP）

| 反 pattern | 問題 |
|-----------|------|
| 「只加 ir.rule 對 m2m field write direction `'any'` walk」 | HC-3 attack chain — OR-semantics 寬鬆化 → 任 attacker 有合法 grantor element 即可加 victim element |
| 「Python constraint 用 `write()` override 而非 `@api.constrains`」 | Create direction 漏守 — attacker 透過 `create({'m2m_field': [(6, 0, [legit, victim])]})` 一招過 |
| 「Constraint loop 用 `tmpl.project_ids - tmpl._origin.project_ids`（delta-only）」 | 從 `_origin` 取 diff 看似聰明，但對 create direction 無 `_origin`；且 future regression：若 constraint 改成 delta-only，攻擊者改 `create + immediate write` 兩步同樣繞 — **always loop full m2m membership** |
| 「Constraint 漏 `is_superuser()` 或 `has_group('base.group_system')` bypass」 | Migration / cron / admin form 全擋住 — silent functional regression |
| 「Bypass 寫成 `env.uid == SUPERUSER_ID` 而非 `env.is_superuser()`」 | `sudo()` 不改 uid（只設 `env.su`；`odoo/models.py:6290-6291` docstring）⇒ 所有 `.sudo()` 合法路徑都繞不過 constraint。後果鏈：`project.project.sudo().create()` → bootstrap `project_access.sudo().create({'role':'owner'})` → grantor 查無 row → `ASSIGNABLE_ROLES_BY_ROLE.get(None, [])` = `[]` → `AccessError` → 租戶連 project 都建不了 |
| 「Constraint 用 `env.user.has_group('base.group_user')` 反向 deny」 | 任何 internal user 都過 group_user check（Odoo std），結果什麼都 deny — pattern 反向實作 |
| 「Constraint `search([(..., '=', user.id)], limit=1)` 沒走 `sudo()`」 | base.group_user 對 `project_access` 的 ir.rule 收緊 user 只看自己 row → constraint 評估 victim B 上 attacker 是否有 row 看到 empty → 誤判 deny 為 missing access (correct effect) — 但邊界情境若 ir.rule 對 `project_access` 改寬鬆，constraint 邏輯隨之變動 → **走 `sudo()` 鎖死 ground truth**。⚠ 2026-08 實況：本 doc 自稱的 canonical 實作 `src/models/agent_template.py:113-115` 用的是**未 sudo** 的 `proj.project_access_ids.filtered(...)`（O2M 讀取，受 `project_access` 的 ir.rule 影響）——**這一列是 doc 對 code 的要求，code 尚未照做**，方向與本 doc 其他條相反，別誤讀成現況描述 |
| 「`is_public=True` 路徑漏走 constraint」 | `is_public` template 本意是「ignore project_ids」，但若 attacker set `is_public=True` + 帶 victim project 進 `project_ids` — `_check_visibility_consistency` 允許（is_public=True 不要求 project_ids），但 grantor constraint 對非空 `project_ids` 仍應評估 — 否則 attacker 用 is_public 旁路 |
| 「在 `_check_mcp_subset` 或其他 `_check_*_subset` 內預期 `project_ids` 已被驗證」 | 兩 constraint Odoo `@api.constrains` 不保證 evaluation order — `_check_mcp_subset` 評估時若 `project_ids` 已含 victim，subset 結果含 victim's MCP → 即使 grantor constraint 後 raise，rollback timing 內 subset error message 可能 leak「victim project 上有 MCP X」訊息 — 細節：**raise 必須 raise 不可信任的 element 名，不可 leak 衍生資訊** |

## Test scaffolding

### 必加 4-case matrix

| 編號 | Direction | Grantor on A | Grantor on B | Expected |
|------|-----------|--------------|--------------|----------|
| HC3.1 | write `[(4, B)]` to T(project_ids=[A]) | admin/owner | none | `ValidationError` |
| HC3.2 | write `[(4, B)]` to T(project_ids=[A]) | admin/owner | admin/owner | success |
| HC3.3 | **非 root user** 的 `.with_user(attacker).sudo()` write same as HC3.1 | admin/owner on A only | none | success (bypass) |
| HC3.4 | base.group_system user write same as HC3.1 | (n/a) | (n/a) | success (bypass) |

**HC3.1 是 reproducer，HC3.2/3/4 鎖 bypass path** — 後續 PR 若把 bypass 改窄（如拿掉 group_system bypass）立刻 trip HC3.4。

⚠ **HC3.3 必須用「真實非 root user ＋ `.sudo()`」建構。** 若直接在 `TransactionCase` 的 `self.env`（uid 本來就是 1，`odoo/tests/common.py` 的 `api.Environment(cls.cr, odoo.SUPERUSER_ID, {})`）上呼叫 `.sudo()`，那是 no-op（`odoo/models.py` 的 `sudo()`: `if flag == self.env.su: return self`），`uid == SUPERUSER_ID` 與 `is_superuser()` 兩種寫法都會通過 → **這條 test 零鑑別力**，擋不住把 bypass 寫回 uid 比對的 regression。順序是關鍵：`with_user(非 root)` 會設 `su=False`（`api.py` 只有 uid==1 才自動 su），接著 `.sudo()` 把 su 打回 True 而 uid 仍是那個 user。現行 `src/tests/test_agent_pod_phase_a_acl_new_models.py` 的 HC3.3 正是無鑑別力的形狀，待收緊。

**HC3.7（branch selector 的守線，#547 補）**：以**非 root user** 走 `.with_user(u).sudo().create()` 建 project，斷言 `u` 拿到 `role='owner'` 的 `project_access` 列。這條鎖的是 `project_project.py:314` 那個 branch selector —— 把它改成 `is_superuser()` 會讓租戶自助建的 project 零 owner 列。**測試本身要斷言前提** `u.id != SUPERUSER_ID`：這條測試的鑑別力完全建立在 `u` 是非 root 上，前提一旦被改掉（改成 `with_user(1)` 或直接 `.sudo()` 不帶 `with_user`）就會靜默退化成恆真。落地見 `src/tests/test_agent_pod_phase_d_hardening.py` 的 `test_FPA14_non_root_sudo_create_self_owns`。

### 補完整 matrix（建議但非必要）

- **HC3.5**: create direction `create({'project_ids': [(6, 0, [A, B])]})` with attacker only-admin-on-A → expect `ValidationError`（攻擊 via create + grantor 同樣擋）
- **HC3.6**: write `[(3, B)]` (unlink B) 由 attacker 對 B 沒 grantor → expect success（unlink 不擴 attack surface，constraint 不該 over-block）— **note**：Odoo `@api.constrains` 對 unlink direction 一般不 trigger，但 `(3, X)` 透過 write 進入 → constraint 評估 post-write m2m（不含 B）→ B 不在 loop → 自然 pass；HC3.6 鎖 unlink 不被誤擋

### Test 路徑與檔名建議

- `src/tests/test_<model>_grantor_authority.py`（自家命名）
- 每條 test method 對應 cell map 編號 — `test_<HC#>_<short_desc>` 命名 self-document

## Forward-looking invariant — bootstrap / provisioning owner hint（Phase D HC-2 / F-PA-14）★

本 pattern 在 epic 內第四度出現（Phase A HC-3 m2m grantor → Phase B HC-2 WS payload→claims → Phase C C-1 token_use → **Phase D F-PA-14 owner-bootstrap hint**），每次技術 surface 不同、抽象同一。Phase D 的 instance 是 `project.project.create()` 的 owner-bootstrap：

```python
# NOTE: 這裡是 branch selector 不是 bypass guard，所以判準是 uid 比對而非
# is_superuser()（見上方「兩種語意不要混用」）。sudo() 背後有真人時必須落
# else 分支，讓他自己成為 owner —— /woow 自助建 project 正是走
# ai_assistant.py:1554 的 project.project.sudo().create()。
# ⚠ 這一行是**載重**的（改成 is_superuser() 會讓租戶建的 project 零 owner 列），
#   但「當初是刻意設計成兩種語意」未經證實。改動前先看 HC3.7 那條 regression test。
if self.env.uid == SUPERUSER_ID:
    hint_partner_id = self.env.context.get('bootstrap_owner_partner_id')
    if not hint_partner_id: return projects   # 無 hint → 不 auto-own
    partner = self.env['res.partner'].browse(hint_partner_id).exists()
else:
    partner = self.env.user.partner_id        # normal user 忽略 hint，owns 自己
```

**/simplify 自抓的收緊**：初版 bootstrap hint 對 normal-user 也可讀（「方便」），/simplify pass 自抓「normal-user 信任 context hint = ownership-assignment 原語」→ 收緊成 **SUPERUSER 分支才讀 hint，normal-user 分支完全忽略**。這正是本 pattern 的核心動作：security 決策（誰成為 owner）不信 attacker-controllable input（context hint），改用 authoritative source（SUPERUSER 明示 / 已認證 user 自己的 partner_id）。

**Forward-looking invariant（後人新增 set-site 前必看 — 鎖未來 operator provisioning）**：

> `bootstrap_owner_partner_id` 是 **superuser-only 的 out-of-band channel**；其值必須 **server-authoritative**（來自 provisioning record，或已認證真人自己的 partner_id），**任何 controller 不得從 `kw` / `params` / inbound `context` 填入**。
>
> 今日 production **零 set-site**（`git grep -n bootstrap_owner_partner_id -- src/` 全部命中：production 只有 `project_project.py:315` 這個 read-site 與 `:295` 的說明註解，其餘全在 `src/tests/`）。未來某 phase 接 operator provisioning 真 set-site 時，**違反此條 = 把 bootstrap 便利退化成 ownership-assignment 原語（B1 復發）**。set-site 作者必須確認 hint 來源是 server 算的（非 request body）—— Odoo 18 `request.env.context` 不 merge request body（`JsonRPCDispatcher` 把 JSON `params` 放 `request.params` plain kwargs，不進 `request.env.context`），但若 controller 顯式 `with_context(bootstrap_owner_partner_id=kw[...])` 把 untrusted 值傳下去就破功。

read-site comment（`project_project.py:304-313`，`⚠ SET-SITE INVARIANT` 那段）已就地警告此 invariant（[[two-layer-acl-lockstep]] 精神：knowledge 同時落 pattern doc + code-site）。

## See also

- 真實落地 case：
  - **Phase D HC-2 / F-PA-14**（owner-bootstrap hint，4th instance）：`docs/archived/tracking/caqa-report-2026-05-25-agent-runtime-pod-phase-d.md` §Security focus 2 + §Caveat「forward-looking invariant」/ `docs/archived/tracking/reviewer-report-2026-05-25-agent-runtime-pod-phase-d.md` §4（security-auditor 獨立確認 airtight，延伸 framework-substrate 驗 indirect injection 關閉）
  - **Phase C C-1**（token_use claim，token 層 3rd instance）：`docs/archived/tracking/agent-runtime-pod-phase-c-wiring-2026-05-24.md` §4
  - **Phase B HC-2**（WS payload→claims，image 端 2nd instance）：`docs/archived/tracking/agent-runtime-pod-phase-b-image-source-2026-05-23.md` §4
  - **Phase A HC-3**：`docs/archived/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md` §14.2 — agent_template grantor authority constraint closure
  - **Phase A HC-3**：`docs/archived/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md` §3.1 HC-3 + §12.2 closure 證據
  - **Phase A reviewer EC-3a**：`docs/archived/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md` §12.4 — Phase B-E controller sudo path lock-step 提醒
- 同 family pitfall（PR #54 cycle 4 HC-2）：
  - `docs/tracking/pitfalls/two-layer-acl-asymmetric-deny.md` — 同 family「ACL 信任 attacker-controlled input」的 controller layer variant
  - `docs/decisions/refactor/mcp-url-redesign.md` §HC-2 — silent IDOR via 兩層 carve-out divergence
- 互補 pattern：
  - [[two-layer-acl-lockstep]] — 同 invariant 在 ir.rule + controller helper 兩層必須對齊收緊；本 pattern 處理「ir.rule 本身 expression power 不足」的維度（Python 補 m2m write direction OR-semantics gap）
  - [[unbound-project-acl-dual-leg]] — dual-leg domain 表達 bound / unbound 兩種 row 可見性；不解 m2m write direction 的 attacker-controlled input 問題
  - [[mcp-acl-6-rule-split]] — 6-rule split / dual-leg 都在 rule 層；本 pattern 在 constraint 層補位
- 對應 known pitfall（root cause family）：
  - [[cross-workspace-task-data-leak]] B1 — CSV ACL 開放 + 漏 ir.rule scope-restrict；本 pattern family 更狹窄：rule 加了但 expression 對 m2m write direction 寬鬆化
- 關鍵 code：
  - `src/models/agent_template.py:_check_project_ids_grantor_authority` — HC-3 fix canonical implementation
  - `src/security/ir_rules.xml` `agent_template_rule_project_admin` — Layer 1 ir.rule（OR-semantics 不足的 baseline）
  - `src/tests/test_agent_pod_phase_a_acl_new_models.py` — HC3.1-3.4 4 cell map
- Odoo 上游原理：
  - `@api.constrains` 同時 cover create + write，evaluate 在 transaction commit 前
  - m2m `'any'` operator → SQL `EXISTS` — OR-semantics
  - m2m relation table 沒 ir.rule（Odoo 18 不支援）— controller layer carve-out 需自己守
