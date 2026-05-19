# Pattern: Addon override Odoo std `ir.rule` — model + sub-model 對等 deactivation

> 適用：在 addon 內收緊 `base.group_user` 對 Odoo std model（如 `project.project`、`project.task`、`sale.order` 等）的 ACL 時。

## When to use

任何符合下列**全部**條件的 PR：

1. 在 `ir.model.access.csv` 給 `base.group_user`（或任何 Odoo 廣泛 implied group）新增 `project.project` / `project.task` / 其他 Odoo std model 的 RWCU 權限。
2. 在 `ir.rule` 新增「scope-restricted」rule 套同一個 group（如 workspace member only）。
3. 預期此新 rule 是該 group 對此 model 的**唯一**有效 read 規則。

**典型場景**：多租戶 PaaS 把 Odoo std model（project / sale / inventory）用在租戶情境，想把可見性收緊到自家 workspace / tenant。

**不適用**：
- 純加欄位或 view，未動 ACL（沒這個風險）。
- 用 dedicated security group（如自訂 `paas.group_tenant`）取代 `base.group_user`（這條路繞開 Odoo std group 的問題，但 scope 大；本 pattern 是 `base.group_user` 路線必要的安全配套）。
- Model 在 Odoo 端**完全沒有** std `ir.rule`（少見；若有疑問見下方 verification SQL）。

## What it looks like

3 個動作必須在同一 PR：

### (a) ACL CSV：把權限放上去

```csv
# src/security/ir.model.access.csv
access_project_project_internal,project.project internal user,project.model_project_project,base.group_user,1,1,1,1
access_project_task_internal,project.task internal user,project.model_project_task,base.group_user,1,1,1,1
```

### (b) 自家 ir.rule：把權限框回 workspace member

```xml
<!-- src/security/ir_rules.xml -->
<record id="project_rule_workspace_member" model="ir.rule">
    <field name="name">Project: workspace member (bound) or project member (unbound)</field>
    <field name="model_id" ref="project.model_project_project"/>
    <field name="domain_force">[
        '|', ('cloud_service_id.workspace_id.access_ids.user_id', '=', user.id),
        '&amp;', ('cloud_service_id', '=', False),
        '|', ('message_partner_ids', 'in', [user.partner_id.id]),
             ('create_uid', '=', user.id)
    ]</field>
    <field name="groups" eval="[(4, ref('base.group_user')), (4, ref('project.group_project_user'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="False"/>
</record>
```

注意：**groups 必須涵蓋所有 implied groups**（這裡同時套 `base.group_user` 與 `project.group_project_user`），否則該 user 帶有 project group 的時候，自家 rule 不 evaluate，只剩 std rule。

### (c) Migration：對等 deactivate Odoo std rules

```python
# src/migrations/<version>/post-migrate.py
def migrate(cr, version):
    if not version:
        return
    # 同 group 多 rule = OR；Odoo std rule 含 always-true leg
    # （'privacy_visibility != "followers"' 對 Odoo default 'employees' 永真），
    # 旁路自家 restrictive rule。唯一 root-cause-correct 解：deactivate std rule。
    # 必須對 model + 對等 sub-model 同步處理。
    cr.execute("""
        UPDATE ir_rule SET active = false
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'project'
              AND model = 'ir.rule'
              AND name IN (
                'project_public_members_rule',         -- project.project
                'task_visibility_rule',                 -- project.task (base.group_user)
                'task_visibility_rule_project_user'     -- project.task (group_project_user)
              )
        )
    """)
```

寫成 **single UPDATE + `IN` clause** 不是風格選擇，是 **invariant 強制機制**：未來任何人想拔某條 rule 都要對所有條目 review；分 3 條 UPDATE 容易漏一條，造成不對稱（B1 即此模式）。

### (d) Tests：對等 cross-workspace regression test

每個 deactivate 的 std rule，要有對應 cross-workspace deny + own-workspace allow 兩條 test。範本見 `src/tests/test_cross_workspace_task_isolation.py`。

## Why this pattern

### Odoo `ir.rule` 的 OR 語意

來源：`/Users/eugene/Documents/woow/AREA-odoo/odoo-18.0/odoo/addons/base/models/ir_rule.py:140-168`

```
# Per-group rules: expression.OR(group_domains)
# Global + group: expression.AND(global_domains + [expression.OR(group_domains)])
```

- 同 group 多條 rule = **OR**
- 跨 group rule = OR-combined
- 只有 global rule（無 groups）會 AND

→ **同 group 上有一條 permissive rule 含 always-true leg，所有 restrictive rule 都被旁路**。

### Odoo std rule 的 always-true leg

`project.project_public_members_rule` 與 `project.task_visibility_rule` 都用相同模式：

```
['|', ('privacy_visibility', '!=', 'followers'),    # ← always-true leg
      ('message_partner_ids', 'in', [user.partner_id.id])]
```

`privacy_visibility != 'followers'` 對 Odoo default `'employees'` / `'portal'` 永遠為 true。這條腿不是 bug，是 Odoo 對「員工專案預設可見」這個業務假設的實作 — 對純 Odoo project 應用是合理的，**對多租戶 PaaS 是 leak 來源**。

### 為什麼不能用 NOT clause 或別招

- **加 NOT 到自家 rule**：NOT 限的是自家 domain，OR-combined 後 std rule 那條腿仍會 match。
- **改 std rule domain**：跨模組改 Odoo 行為，比 deactivate 更侵入。
- **拉新 security group 取代 `base.group_user`**：要改 user 設定 / module install hook，scope 暴漲；且 Odoo 內部 view / wizard 預期 `base.group_user`。
- **完全靠 controller 端 sudo + 顯式 filter**：ORM 路徑（M2M widget、search domain、Odoo 標準 form）會繞過 controller。

→ deactivate 是唯一 root-cause-correct 解。

### 為什麼是「對等」— 不能只拔一條

Odoo std model 經常**配對出現**（project ↔ project.task、sale.order ↔ sale.order.line、etc.），每對都有獨立的 ir.rule 套在同 group。只拔 model 端的 rule，sub-model 端的 std rule 仍然 OR-旁路你自家的 sub-model rule。

**檢查 checklist**（每次套用此 pattern 都要走過）：

1. 該 model 有哪些 Odoo std rule？
2. 對等的 sub-model（line / child / event 等）有哪些 std rule？
3. 哪些 std rule 套 `base.group_user`？哪些套你會用 `with_user` 的其他 group？
4. 每條 std rule 的 domain 是否含 always-true leg？
5. 一次 deactivate 全部，single UPDATE + IN clause，避免漏一條。

對本 refactor 而言，正確答案是「拔 3 條」（B1 從拔 1 條漏到 3 條 — 詳見 [[cross-workspace-task-data-leak]]）。

### Migration verify SQL（pre-deploy 必跑）

```sql
-- 確認對應的 std rule 都已 active=false
SELECT d.name AS xmlid, r.name AS rule_name, r.active
FROM ir_rule r
JOIN ir_model_data d ON d.res_id = r.id AND d.model = 'ir.rule'
WHERE d.module = 'project'
  AND d.name IN (
    'project_public_members_rule',
    'task_visibility_rule',
    'task_visibility_rule_project_user'
  );
-- expect: 3 rows, all active = false
```

少於 3 row 代表 **Odoo 版本 patch 改了 xmlid**（UPDATE 的 WHERE 沒命中） — 必須立刻停下，重新對齊 std rule inventory，不可上 production。

### Rollback shape

**對等** reactivate；和 deactivate 一樣對等：

```sql
UPDATE ir_rule SET active = true
WHERE id IN (
    SELECT res_id FROM ir_model_data
    WHERE module = 'project'
      AND model = 'ir.rule'
      AND name IN (
        'project_public_members_rule',
        'task_visibility_rule',
        'task_visibility_rule_project_user'
      )
);
```

Idempotent；分次 partial rollback 是 anti-pattern（會留下不一致的中介狀態，比完全沒拔還難 debug）。

## See also

- [[cross-workspace-task-data-leak]] — B1 incident（這個 pattern 從這個 leak 抽煉而來）
- [[paas-workspace-member-acl-tightening]] — 完整收緊 `base.group_user` ACL 的 idiomatic Odoo addon 做法
- [[cloud-service-project-decouple]] §Decisions#8 — 為什麼 deactivation 是唯一 root-cause-correct 解的選擇邏輯
- 關鍵 code 樣板：
  - `src/migrations/18.0.1.0.23/post-migrate.py:93-107`（B1 fix 的 single UPDATE + IN clause）
  - `src/security/ir_rules.xml:65-117`（自家 3 條 scoped rule）
  - `src/tests/test_cross_workspace_task_isolation.py`（對等 regression test 樣板）
- 上游證據：
  - `addons/base/models/ir_rule.py:140-168`（OR 語意實作）
  - `addons/project/security/project_security.xml`（被 deactivate 的 std rule 定義）
