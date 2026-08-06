# Pattern: PaaS workspace member ACL — `base.group_user` 收緊 idiomatic 做法

> 適用：把任一 model（自家 model 或 Odoo std model）的可見性收緊到「同 workspace 的 access_id 成員」。

## When to use

- 新 model 隸屬於某個 `woow_paas_platform.workspace`（透過 `workspace_id` 或多層關聯鏈）。
- 既有 model 缺 `ir.rule`，只靠 controller endpoint 過濾隔離 — 但有 ORM 路徑（M2M widget、view domain、Odoo 標準 form）會繞過 controller。
- 在 addon 內收緊 Odoo std model（如 `project.project`、`project.task`）對 PaaS 租戶的可見性。

**特別重要的觸發點**：當 PR 同時碰到 ACL CSV + ir_rules.xml，要 mentally check 是不是套到 Odoo std model；若是，必須額外走 [[odoo-addon-std-rule-symmetric-deactivation]] checklist。

## What it looks like

### 三件套

#### 1. ACL CSV：把 RWCU 放出來

對 self-owned model（addon 自家 model）：

```csv
access_cloud_service_user,woow_paas_platform.cloud_service.user,model_woow_paas_platform_cloud_service,base.group_user,1,1,1,1
```

對 Odoo std model：

```csv
access_project_project_internal,project.project internal user,project.model_project_project,base.group_user,1,1,1,1
access_project_task_internal,project.task internal user,project.model_project_task,base.group_user,1,1,1,1
```

#### 2. ir.rule：用 workspace.access_ids 把權限框回 member

```xml
<record id="cloud_service_rule_workspace_member" model="ir.rule">
    <field name="name">CloudService: workspace member access only</field>
    <field name="model_id" ref="model_woow_paas_platform_cloud_service"/>
    <field name="domain_force">[('workspace_id.access_ids.user_id', '=', user.id)]</field>
    <field name="groups" eval="[(4, ref('base.group_user'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>
```

**Domain 關鍵字**：`workspace_id.access_ids.user_id` — 走 `workspace → access_ids (one2many to workspace.access) → user_id`。本 addon 對 workspace member 的真實 source of truth 是 `workspace.access` model（見 `src/models/workspace_access.py`），不是 `workspace.message_partner_ids` 或 `workspace.create_uid`。

#### 3.（若 model 是 Odoo std）對等 deactivate std rule

走 [[odoo-addon-std-rule-symmetric-deactivation]] 流程。Self-owned model 通常**沒**配套 std rule，這步可跳過 — 但要 verify。

### Bound vs unbound 雙腿（適用於可選綁 workspace 的 model）

像 `project.project` 可綁可不綁 `cloud_service`（→ workspace），需要 dual-leg domain：

```python
[
    '|', ('cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),  # bound leg
    '&', ('cloud_service_id', '=', False),                                     # unbound...
    '|', ('message_partner_ids', 'in', [user.partner_id.id]),                  # ...visible to follower
         ('create_uid', '=', user.id)                                          # ...or creator
]
```

**`create_uid` 那條腿不可省**：使用者剛建好自己的 unbound project，followers 還沒加，否則立刻看不到自己建的東西 — 是嚴重 UX bug。本 refactor Open Q1 的 `message_partner_ids + create_uid` 雙腿 ([[cloud-service-project-decouple]] §9) 即為此而設。

### Implied groups 涵蓋

```xml
<field name="groups" eval="[(4, ref('base.group_user')), (4, ref('project.group_project_user'))]"/>
```

當 user 帶有 `project.group_project_user`（Odoo 對 project 操作的「管理者」group），自家 rule 必須也套在這個 group，否則 OR-combined 之後該 group 的 std rule 會旁路自家 restrictive rule。

**Checklist**：套到 `base.group_user` 的 rule 通常也要套到 `_group_implied_ids` 反向鏈上的 specialized groups。對 project 而言是 `project.group_project_user`；對 sale 而言是 `sales_team.group_sale_salesman` 之類；不同 model 要對 case-by-case 看。

## Why this pattern

### `workspace.access` 才是 source of truth

過去的 PaaS workspace member 判斷有 3 個 candidate：
- `workspace.owner_id`（單一 owner，不夠用）
- `workspace.message_partner_ids`（Odoo `mail.thread` 的 follower）
- **`workspace.access_ids` →`woow_paas_platform.workspace_access` model**

只有第三個是 PaaS 業務語意上的「workspace 成員 + 角色」（owner / admin / member 等）。前兩個是 Odoo 通用機制，用了會在 invite / kick / role-change 場景下與業務邏輯打架。

**Invariant**：本 addon 所有 workspace-scoped ir.rule 的 domain 一律走 `access_ids.user_id`，不走 `owner_id` / `message_partner_ids`。

### Controller filter 不夠

D2=A 的 Task form M2M widget 為例：使用者在 task 上點「+ Add extra service」，OWL 元件直接走 Odoo 標準 form RPC，**完全繞過自家 controller**。controller filter 只防得了 `/api/...` 路徑；ir.rule 防得了所有 ORM 路徑。

Plan §D8=A 的論證：當未來引入任何 M2M widget / view ref / 標準 Odoo 元件存取本 model 時，沒 ir.rule 就立刻 leak。**ir.rule 是 root-cause-correct 解，controller filter 只是表層 hint**。

## Test scaffolding（必備 regression）

每個 workspace-scoped ACL 都要對應 cross-workspace deny + own-workspace allow 兩條 regression test。範本：`src/tests/test_cross_workspace_task_isolation.py` 與 `src/tests/test_cloud_service_acl.py`。

關鍵 fixture 元素：
- **兩個非重疊 workspace 的 internal user**（Alice 屬 W1、Bob 屬 W2、互不為對方 workspace 的 access_ids）
- **bound 路徑用 `Service.sudo().create()` 建好 W2 的物件**，然後用 `with_user(alice)` 驗證 search / browse.read 是空 / raise AccessError
- **own-workspace sanity**：Bob `with_user` 自家物件仍然看得到（防止 over-correction）

兩條都加，沒這條 sanity 線，未來人改 ACL 改成 blanket deny 不會被測出來。

## See also

- [[odoo-addon-std-rule-symmetric-deactivation]] — 若 model 是 Odoo std，必須對等拔 std rule
- [[cloud-service-project-decouple]] §Invariants — 該 refactor 的不變式清單，**並非全部關於 ACL**；
  其中含直接約束本 pattern 的條目（Odoo std rule 維持 `active=false`、`base.group_user` 的 RWCU 必須
  配 ir.rule 框住），動 project / task 的 ACL 前整段過一遍（刻意不寫條數——寫下就會漂，見 #615）
- [[cross-workspace-task-data-leak]] — 漏掉本 pattern 第 3 步（std rule deactivation）的後果
- 關鍵 code 樣板：
  - `src/security/ir.model.access.csv:26-27`（CSV row 對 Odoo std model）
  - `src/security/ir.model.access.csv:7`（CSV row 對自家 model）
  - `src/security/ir_rules.xml:65-117`（3 條 scoped ir.rule 完整範例）
  - `src/tests/test_cloud_service_acl.py`（self-owned model regression）
  - `src/tests/test_cross_workspace_task_isolation.py`（std model regression）
