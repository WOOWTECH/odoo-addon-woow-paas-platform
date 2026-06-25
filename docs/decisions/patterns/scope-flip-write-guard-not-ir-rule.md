# Pattern: scope / tenancy flip 的不可變必須靠 `write()` guard，不是 ir.rule

> 適用：一個 model 有「**選擇可見範圍 / 租戶歸屬**」的欄位（`scope` system/user、`project_id`、`company_id`、`public`），其中某個值是**更廣可見 / 更高權限**（如 `scope='system'` = 全平台可讀）。該欄位對 `base.group_user` 是可寫的（因為同一個欄位在正常情境下要能被使用者設定／在自己的 row 上 CRUD）。要防「使用者把自己 row 從窄 scope **翻**成寬 scope（→ 跨租戶外洩 / 提權）」**不能只靠 ir.rule** —— Odoo 的 record rule write 權限檢查評估的是**寫入前 (pre-write) 的 row 狀態**，flip 動作在 pre-write 仍是合法的窄 scope row，rule 放行，改完才變寬 scope，已經太遲。正解：在 model `write()` override 內加 guard，非 superuser 且非授權 group 變更該欄位 → `AccessError`。

## When to use

當下列條件**同時**成立：

1. **存在 scope-selecting / tenancy 欄位**：`scope`（system/user）、`project_id` / `workspace_id`（NULL = 全域）、`company_id`、`public` 之類，欄位值決定 row 的可見範圍或歸屬租戶，且**某個值明顯更寬 / 更高權**。
2. **該欄位對 `base.group_user` 物理可寫**：model-level `ir.model.access.csv` 給了 `perm_write=1`（常見，因為使用者要能管理自己 scope 內的 row），且欄位沒有 `readonly` 或 `groups=` field-mask 完全擋掉寫入。
3. **窄→寬 的 flip 是提權 / 跨租戶外洩**：把 `scope='user'`（綁自己 project）翻成 `scope='system'`（全平台 pod 載入）、或把 `project_id` 設成 `NULL`（unbound / 全域可見）、或 `public=True`，會讓原本租戶私有的 row 變成別人也讀得到。

→ 三者成立 → ir.rule **守不住**：寫 rule 看 pre-write row（仍是合法窄 scope，user 對它有 write）→ 放行 → flip 落地。必須在 `write()` 攔 vals。

**不適用**：

- 欄位對 `base.group_user` 根本不可寫（`readonly=True` 或 `groups='base.group_system'` field-mask 完全擋掉）→ 已經沒有 flip 面，不需 guard（但要 grep 確認 model-level access + 所有 create/write 路徑，不要憑感覺）。
- flip 不造成提權 / 外洩（兩個值同 scope 等價）→ 純資料欄位，不需 guard。
- 建立時就決定、之後不可改的欄位 → 用 create-time 驗證（`@api.constrains` / create override）即可，write guard 是為了擋「既有 row 被改」。

## Why ir.rule does NOT work here

Odoo record rule 對 `write` 的判定：**對 recordset 當前（pre-write）狀態**套用 rule domain，命中才允許這次 write。所以對「flip」這個動作：

```
row 現況: scope='user', project_id=42  (alice 是 project 42 owner)
alice 執行: row.write({'scope': 'system', 'project_id': False})
ir.rule (skill_rule_user_admin, domain ['&',('scope','=','user'),(project walk owner/admin)]):
  → 對 pre-write row 評估：scope='user' ✓ + alice 是 owner ✓ → 命中 → write 放行
  → write 落地後 row 變 scope='system' (全平台可讀)
  → 太遲了。rule 從沒看過 post-write 狀態。
```

加再多「`scope='user'` AND-leg」到 write rule 都沒用，因為 leg 是對 **pre-write** 算的。這是 record rule 機制的本質，不是 domain 寫錯。

## What it looks like

### 反例 — 以為 ir.rule 的 scope leg 能擋 flip

```xml
<!-- ir_rules.xml -->
<record id="skill_rule_user_admin" model="ir.rule">
    <field name="domain_force">['&amp;', ('scope', '=', 'user'),
        ('project_id.project_access_ids', 'any', [('user_id','=',user.id),('role','in',['owner','admin'])])]</field>
    <field name="perm_write" eval="True"/>
</record>
```

**問題**：alice（project owner）對自己 `scope='user'` 的 skill 有 write。她 `write({'scope':'system','project_id':False})`，rule 對 pre-write（仍 user-scope、她仍 owner）放行 → flip 成功 → 該 skill 變全平台 pod 可載入。測試 `assertRaises(AccessError)` 紅：`AccessError not raised`。

### 正解 — `write()` guard 攔 vals

```python
# models/skill.py
from odoo.exceptions import AccessError

def write(self, vals):
    # scope / project_id 對非 admin 不可變更：擋窄→寬 flip（跨租戶外洩）。
    # ir.rule 擋不住——它對 pre-write 狀態判定，flip 前 row 仍是合法 user-scope。
    if ('scope' in vals or 'project_id' in vals) \
            and not self.env.su \
            and not self.env.user.has_group('base.group_system'):
        for rec in self:
            if 'scope' in vals and vals['scope'] != rec.scope:
                raise AccessError("Only administrators can change a skill's scope.")
            if 'project_id' in vals and vals.get('project_id') != rec.project_id.id:
                raise AccessError("Only administrators can change a skill's project.")
    return super().write(vals)
```

### 三個關鍵細節

1. **比對「是否真的改值」，不是「vals 是否含 key」**：`vals['scope'] != rec.scope` —— 若使用者 write 一個與現值相同的 scope（UI 整批送欄位常見），不該誤擋。只攔「實際變更」。
2. **`self.env.su` + `has_group('base.group_system')` 雙閘放行**：sudo 路徑（upload pipeline、demo seed、migration）與真 admin 要能正常設 scope/project。guard 只擋「非 sudo 的一般 user」。
3. **create 不受此 guard**：建立時走 `@api.constrains`（如 D4：`scope='user'` ⟺ `project_id` NOT NULL）驗一致性即可。guard 是專為「既有 row 被 write 改」設計；建立一個 `scope='system'` row 由 create-direction 的 ACL（該 group 無 create-applicable rule 命中 system row）擋。

## Why this pattern

- **守在 ORM 寫入這層、不靠 rule 機制做不到的事**：ir.rule 的設計就是 pre-write 判定；要表達「這個欄位的**值轉移**受限」必須在 `write()`。這不是繞過 rule，是補 rule 表達不了的維度。
- **與 create-direction ACL 互補**：create 方向用 ACL / constrains（擋「無中生有一個寬 scope row」），write 方向用 guard（擋「把窄 row 翻寬」）。兩個方向都要守，缺一邊就有洞。
- **測試可直接鎖**：`with_user(non_admin).write({'scope':'system'})` → `assertRaises(AccessError)`，是明確的 cross-tenant 提權負向測試，不像 ir.rule 那樣容易「綠了但其實沒擋」。

## Anti-patterns（具體別做）

- ❌ **靠 write rule 的 `('scope','=','user')` AND-leg 擋 flip**：leg 對 pre-write 算，flip 前 row 合法 → 放行。AND-leg 的作用是「不讓 user rule 誤吃 system row（讓 user 看不到 / 改不到別 scope 的 row）」，**不是**擋 flip——兩個目的別混為一談。
- ❌ **guard 只檢查 `'scope' in vals` 不比對值**：UI 整批送欄位（含未改的 scope）會誤擋正常 write。必須 `vals['scope'] != rec.scope`。
- ❌ **忘了放行 sudo / admin**：upload pipeline、demo data、migration 都走 sudo create/write 設 scope；guard 沒放行 `env.su` 會把自己的合法路徑也擋死。
- ❌ **把 guard 寫在 controller / wizard 而非 model `write()`**：那只守一條入口，odoo shell / RPC / 其他 controller 仍能 flip。guard 要在 model layer。

## 落地（本 epic）

- spec `docs/plans/2026-Q2/2026-05-30-skill-dual-scope-design.md` §② ACL — 明列「scope-flip 防護靠 `write()` guard，不是 ir.rule」（commit `b01d8d4` 把設計從「ir.rule AND-leg」更正為「write guard」）。
- impl commit `ec6e007` — `fix: skill dual-scope test failures (snapshot env, scope-flip write guard, SSE6 subset)`（`src/models/skill.py` write() guard）。
- 暴露點：`TestSkillDualScopeAcl.test_group_user_cannot_flip_user_skill_to_system` —— 最初設計靠 ir.rule，實機跑出 `AccessError not raised` 才發現 rule 擋不住、改 write guard。

## Invariants（後人改本區前必看）

- scope-flip 防護**必須**在 model `write()`，不可退回 ir.rule —— record rule 對 pre-write 判定，結構上擋不住 flip。
- guard 比對「值是否真的改變」（`vals[k] != rec.<k>`），不可只看「key 是否在 vals」—— 否則整批 write 誤擋。
- guard 必須放行 `self.env.su` 與 `base.group_system` —— upload pipeline / demo seed / migration / admin 走這條設 scope。
- create-direction 由 ACL + `@api.constrains` 守，write-direction 由本 guard 守 —— 兩個方向缺一不可。

## See also

- 互補 ACL pattern：[[mcp-acl-6-rule-split]] —— 同 model 混 system + user scope 時的 ir.rule 拆法（create direction 提權面）。本 pattern 是該 split 的「write-direction 補強」：6-rule split 守 create + 可見性，write guard 守 flip。兩者一組才完整。
- 同 family（不能只靠 user-facing / rule layer 守 invariant）：[[constraint-input-trust-expansion]]（attacker-writable m2m + constrains 推導 authority）、[[invariant-recompute-on-orm-write-hook]]（cross-record derived flag 要在 create/write hook 兜底）。三者共通主旨：**安全 / 不變式守線必須在 ORM 寫入這層，不能假設上層（rule / view / transition method）擋得住**。
- 上下文：[[two-layer-acl-lockstep]] —— 本 pattern 是「ir.rule 這層擋不住、必須加第二層 write guard」的具體 instance；改任一層時兩層要 lock-step。
- 原始來源（合成材料、不複製）：spec / impl commit / 測試暴露點 —— 路徑見 `docs/tracking/followups/follow-ups.md` skill dual-scope 段。
