# Pattern: bundled attachment defense in depth — anchor stamping + `ir.attachment` row-level ir.rule

> 適用：自家 model 透過 `ir.attachment` 存 binary payload（特別是「敏感」或「subprocess 執行的 code / RCE-class」內容，如 skill bundle、agent template payload、user secrets blob）。**field-level `groups='base.group_system'` mask 不夠** — 它只擋 ORM read 該 field，attacker 仍可透過 Odoo std `/web/content/<id>` HTTP endpoint 直接 enumerate attachment id 下載；endpoint 走 `ir.attachment._check` Python delegation 邏輯，會回頭評估「`res_model` 對應 model 上 user 是否可讀 record」— 若該 model 對 user 有合法 R 動作（如 skill `R only` for project member），delegation 通過 → attachment 通過 → bytes 串流出去。**必須 defense in depth 兩層**：(1) anchor stamping（create/write override 確保 attachment `res_model + res_id + public=False` 三件套都正確），(2) `ir.attachment` row-level ir.rule 對 `res_model='<my_model>'` 直接 base.group_user deny + base.group_system admin_full。

## When to use

當下列條件同時成立：

1. **自家 model 有 `Many2one('ir.attachment')` field 存 binary payload**（如 `skill.bundle_attachment_id`）
2. **Binary payload 對 attacker 有實質價值**（敏感資料 / subprocess 執行的 code / signature material / secrets / 任何「拿到 raw bytes 即攻擊有意義」的內容）
3. **自家 model 的 row-level ACL 對某 user role 允許 read**（如 `skill_rule_project_user` perm_read=True 給 project member）— attacker 透過 model 對 attachment 推導通行
4. **Odoo std `/web/content` / `/web/image` HTTP endpoint 仍 active**（未自寫 controller bypass 或 disable std endpoint）— `/web/content` 是 Odoo 預設，不會 disable

**典型情境（本 codebase 真實 case，2026-05-23 Phase A reviewer round 1 HC-4）**：

```
skill model
  ├── bundle_attachment_id Many2one('ir.attachment', groups='base.group_system')
  │     ◄── field-level mask 擋 ORM read field
  │
  ├── ir.rule skill_rule_project_user  perm_read=True
  │     domain ('project_id.project_access_ids', 'any',
  │             [('user_id', '=', user.id), ('role', '=', 'user')])
  │     ◄── attacker (project user) 對 skill row 可讀
  │
  └── (Phase B/C 後 sudo upload pipeline)
        ir.attachment row 存實際 bundle zip / tar.gz bytes

Attacker chain (Phase B/C bundle 上 production 後 active):
  1. Attacker enumerate skill row via ORM → 取 skill metadata (skill name, version, sha256)
  2. Attacker enumerate sequential ir.attachment.id (整數，前端 devtools / log / mail trace 可見)
  3. Attacker GET /web/content/<att_id>?download=1
  4. Endpoint 走 ir.attachment._check delegation：
     attachment row res_model='woow_paas_platform.skill' + res_id=skill.id
     → delegate to skill model.check_access_rule('read', skill.id)
     → skill_rule_project_user perm_read=True → allow
     → attachment.read allow → stream bundle bytes
  5. Attacker 拿到 bundle bytes → reverse engineer → craft malicious bundle (same name + sha256)
  6. Upload back via 合法 upload API (assuming attacker 對自己 project 有 skill upload 權)
  7. Phase B/C+ skill activation: agent runtime lazy fetch → subprocess 執行 attacker payload → RCE on agent pod
```

**Why field-level mask 不夠**：

- `groups='base.group_system'` 阻擋 `skill.bundle_attachment_id` field read / write — base.group_user 跑 `skill_id.bundle_attachment_id` returns False
- 但 attacker 不需要透過 skill field 拿 attachment id — id 是 sequential int，enumerate 即可
- /web/content endpoint 完全不走 skill model — 直接 query ir.attachment by id，依 attachment.res_model 推導回 skill ACL → field-level mask 不在 path 上

**不適用**：

- Binary payload 對任何人 public visible（如 marketplace asset / 公開教學影片） — 不需 row-level ACL
- 自家 model 無 row-level ACL for base.group_user（只 group_system 看）— 直接靠 base ACL chain 已擋
- Attachment 是 inline data URL / 不存 `ir.attachment` table（用 `fields.Binary` 直接欄位內 base64）— 沒 attachment row 攻擊面
- 自家 model `ir.rule` 已 deny read for base.group_user — delegation 那邊已 deny，attachment delegation 自動 deny

## What it looks like

### 1. Layer 1 — Anchor stamping（create + write override）

```python
class Skill(models.Model):
    _name = 'woow_paas_platform.skill'

    bundle_attachment_id = fields.Many2one(
        'ir.attachment',
        string='Bundle Attachment',
        groups='base.group_system',  # field-level mask (necessary but insufficient)
        ondelete='restrict',
        help='Skill bundle binary; managed via API upload endpoint (Phase B-C). '
             'Direct backend form attach blocked by group mask + by Layer 2 ir.attachment ir.rule '
             '— see docs/decisions/patterns/attachment-row-level-acl-and-anchor-stamping.md',
    )

    @api.model_create_multi
    def create(self, vals_list):
        skills = super().create(vals_list)
        for skill in skills:
            skill._anchor_bundle_attachment()
        return skills

    def write(self, vals):
        result = super().write(vals)
        if 'bundle_attachment_id' in vals:
            for skill in self:
                skill._anchor_bundle_attachment()
        return result

    def _anchor_bundle_attachment(self):
        """Stamp delegation target on attachment row.

        Defense in depth Layer 1 for HC-4 attack chain:
          - `res_model='woow_paas_platform.skill'` ensures /web/content delegation hits
            this model's ir.rule (Layer 2)
          - `res_id=skill.id` ensures delegation evaluates against the right skill row
            (not the original res_id from prior `mail.message` attachment, etc.)
          - `public=False` blocks /web/image / public attachment endpoints that bypass ir.rule

        NB destructive: if admin re-assigns an existing non-skill attachment (e.g. one
        already res_model='mail.message') to skill.bundle_attachment_id, this overwrites
        `res_model` — that severs the original mail.message linkage. By design — admin
        path is curating; non-admin path is blocked by group mask + Layer 2 ir.rule.
        """
        self.ensure_one()
        att = self.bundle_attachment_id
        if not att:
            return
        updates = {}
        if att.res_model != self._name:
            updates['res_model'] = self._name
        if att.res_id != self.id:
            updates['res_id'] = self.id
        if att.public:
            updates['public'] = False
        if updates:
            att.sudo().write(updates)
```

**Why `att.sudo().write(updates)`**：

- `att.res_model` / `att.res_id` / `att.public` 是 attachment field — base.group_user 受 Layer 2 ir.rule 限制（domain `res_model != skill` 對 post-write state `res_model = skill` 為 False → deny write）
- Stamping 屬於 system-level metadata curation — sudo 是必要（attachment 可能不在 caller scope，需 admin bypass）

**Why 對 missing field skip anchor**：

- `if not att: return` 早退；`self.bundle_attachment_id` 在 base.group_user context 因 field-level mask returns False → 不可能進 anchor path
- Anchor 僅在 sudo / group_system path 觸發（API upload endpoint / cron / migration / admin backend form）
- 這是預期：anchor 是「admin / system 設定 attachment 時的 stamping」，不是「user 設定」

### 2. Layer 2 — `ir.attachment` row-level ir.rule（兩條一組）

```xml
<!-- ir_rules.xml -->

<!-- Layer 2a: base.group_user row-level deny for skill bundle attachment -->
<record id="ir_attachment_rule_skill_bundle_user_scope" model="ir.rule">
    <field name="name">Skill bundle attachment: deny row-level for base.group_user</field>
    <field name="model_id" ref="base.model_ir_attachment"/>
    <field name="domain_force">[('res_model', '!=', 'woow_paas_platform.skill')]</field>
    <field name="groups" eval="[(4, ref('base.group_user'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>

<!-- Layer 2b: base.group_system admin_full bypass (mirror pattern of mcp-acl-6-rule-split) -->
<record id="ir_attachment_rule_skill_bundle_admin_full" model="ir.rule">
    <field name="name">Skill bundle attachment: admin full access for base.group_system</field>
    <field name="model_id" ref="base.model_ir_attachment"/>
    <field name="domain_force">[(1, '=', 1)]</field>
    <field name="groups" eval="[(4, ref('base.group_system'))]"/>
    <field name="perm_read" eval="True"/>
    <field name="perm_write" eval="True"/>
    <field name="perm_create" eval="True"/>
    <field name="perm_unlink" eval="True"/>
</record>
```

**Domain shape 解讀**：

- Layer 2a `('res_model', '!=', 'woow_paas_platform.skill')` — base.group_user 對 attachment row：
  - non-skill attachment（res_model 是 `mail.message` / `product.product` / 自家其他 model / NULL）→ domain True → R/W/C/U=True，**既有 behavior 不變**
  - skill bundle attachment（res_model='woow_paas_platform.skill'）→ domain False → 全 deny
- Layer 2b `[(1, '=', 1)]` 永真 for base.group_system — bypass Layer 2a 鎖
- `base.group_system` 透過 Odoo `_group_implied_ids` 自動繼承 `base.group_user`；per-group rule **OR semantics** → group_system user 跑 OR(Layer 2a, Layer 2b) = OR(False, True) = True → allow

**Why per-group OR semantics 不會誤放 base.group_user**：

- base.group_user 不在 group_system 群 → 不評估 Layer 2b（only Layer 2a domain matters）
- Layer 2a 對 skill attachment domain False → deny ✓

### 3. Write direction attack reproducer

```python
def test_attacker_cannot_hijack_attachment_to_skill_via_write(self):
    """Attacker creates non-skill attachment then writes res_model='skill' to hijack."""
    att = self.env['ir.attachment'].with_user(self.charlie).create({
        'name': 'evil.bin',
        'datas': base64.b64encode(b'malicious'),
        'res_model': 'mail.message',  # legit start
        'res_id': self.legit_message.id,
    })
    # Charlie tries to flip res_model
    with self.assertRaises(AccessError):
        att.with_user(self.charlie).write({'res_model': 'woow_paas_platform.skill'})

def test_attacker_cannot_create_attachment_with_skill_res_model(self):
    """Attacker creates new attachment directly with res_model='skill'."""
    with self.assertRaises(AccessError):
        self.env['ir.attachment'].with_user(self.charlie).create({
            'name': 'evil.zip',
            'datas': base64.b64encode(b'malicious'),
            'res_model': 'woow_paas_platform.skill',
            'res_id': 9999,
        })
```

**Why Odoo evaluates ir.rule write direction against post-write state**：

- `att.write({'res_model': 'woow_paas_platform.skill'})` → ORM evaluates rule against post-write `res_model='skill'` → Layer 2a domain `res_model != skill` 對 post-write state False → deny ✓
- 同樣對 create direction — post-create state evaluation → 同樣 deny ✓

### 4. Test 必加 4-case matrix

| 編號 | Attack / regression case | Expected | Defense |
|------|--------------------|----------|---------|
| HC4.1 | base.group_user ORM `bundle_attachment.read(['datas'])` | `AccessError` | Layer 2a domain False → ir.rule deny |
| HC4.2 | project_access role='admin' (still no base.group_system) read | `AccessError` | project-level admin ≠ platform admin；仍走 base.group_user rule chain → deny |
| HC4.3 | base.group_system admin read | success + bytes | Layer 2b admin_full True → allow |
| HC4.4 | HTTP `GET /web/content/<bundle_id>?download=1` as base.group_user | 403 | /web/content 走 `ir.attachment._check('read')` → ir.rule deny |

**HC4.1 + HC4.4 是兩個 attack path（ORM + HTTP），HC4.2 鎖「project-level role hierarchy 不能升級到 platform-level」，HC4.3 鎖 admin bypass — 缺任一條 future regression 可不通 review**。

### 5. 補完整 matrix（建議）

- **HC4.5**: write direction hijack — attacker write attachment res_model='skill' (見 §3 上面 reproducer)
- **HC4.6**: create direction hijack — attacker create attachment with res_model='skill' (見 §3 上面 reproducer)
- **HC4.7**: anchor stamping — admin create skill with attachment（res_model='mail.message' 原 stamp）→ create override 後 attachment.res_model='woow_paas_platform.skill'
- **HC4.8**: anchor stamping — admin re-assign attachment to skill via write → `_anchor_bundle_attachment` 觸發 + stamp 正確（covers EC-4a destructive overwrite case）
- **HC4.9**: `public=True` bypass — attacker create attachment with `public=True` + write res_model='skill'（or hijack public attachment to skill）→ Layer 2a write deny `(public=True, res_model=skill)` 同樣 res_model 條件 deny ✓；anchor stamping 對 admin path 強制 `public=False`

## Why this pattern

### 為什麼 anchor + ir.rule 兩層皆必要

兩層各擋不同 attack surface：

| Attack | Anchor only | ir.rule only | Both |
|--------|------------|--------------|------|
| Attacker GET `/web/content/<att_id>` for legit skill bundle | **Pass** — anchor 確保 res_model='skill'，但 /web/content delegation 走 skill model ACL → user 對 skill R only → attachment R allow | **Deny** — ir.rule Layer 2a 直接擋 base.group_user | Deny |
| Attacker write attachment.res_model='skill' to hijack | **Pass** — anchor 不阻擋 attacker write attachment field | **Deny** — write direction post-state evaluation domain False | Deny |
| Admin assign existing mail.message attachment to skill | Skill `_anchor_bundle_attachment` re-stamps `res_model='skill'` — `/web/content` delegation 現在指 skill | Without anchor 重 stamp，attachment.res_model 仍是 'mail.message' → ir.rule Layer 2a `res_model != skill` 對 `res_model='mail.message'` True → allow → **delegation back to mail.message** → mail.message ACL evaluate → 視 mail.message ir.rule 可能 allow → bundle bytes leak | Both — anchor 強制 stamp + rule 守 delegation target correctness |
| Attacker create attachment res_model=NULL + 隨後 admin set bundle_attachment_id | `_anchor_bundle_attachment` re-stamp res_model→'skill' → 旁路：admin 只看到 attachment id 已有，但實際 res_model 之前 NULL，Layer 2a 對 NULL evaluate undefined → 邊界 | ir.rule alone 不 cover anchor missing case；attachment res_model=NULL 走 fallback admin-only mask | Both — anchor 在 admin path 把 NULL → 'skill' 補齊，Layer 2a 對 post-stamp 'skill' deny base.group_user ✓ |

**只用 Layer 1 不夠**：anchor 是 system-level metadata stamping，沒有 row-level access enforcement — base.group_user 仍透過 /web/content 走 delegation 取 skill ACL allow → bytes 出去。

**只用 Layer 2 不夠**：若 attachment 沒 stamp（res_model 是別 model / NULL / public=True），Layer 2a `res_model != skill` 對 non-skill 值為 True → allow → bypass。

**兩層配合**：

- Anchor 確保 attachment row 帶有 `res_model='skill'` stamp — Layer 2a 才能正確 deny
- Layer 2 確保 base.group_user 對 stamped attachment 直接 row-level deny — 不需要走 delegation
- Defense in depth：anchor 是 attachment 的 metadata correctness，rule 是 access enforcement，互不重複

### 為什麼必須 grep verify Odoo 18 base + bundled addons 0 std rule on `ir.attachment` for base.group_user

對齊 [[odoo-addon-std-rule-symmetric-deactivation]] pattern 紅線：

- pattern trigger 條件：「對既有有 std rule on base.group_user 的 model 加新自家 rule」→ 必須 deactivate std rule
- 本案 ir.attachment 有 std CSV ACL（`access_ir_attachment_group_user` 1,1,1,1）但 **無 std ir.rule** → pattern 不 trigger → **不需要 deactivate**

**Independent grep verify 必須做**（不能信 implementation claim）：

```bash
# 1. base/security/ 內所有 ir.attachment 引用
grep -rn "ir.attachment" odoo/addons/base/security/
# expect: 1 hit — base/security/ir.model.access.csv 內 CSV row only

# 2. base_security.xml 內 ir.attachment ir.rule
grep -nE "(ir.attachment|model_ir_attachment)" odoo/addons/base/security/base_security.xml
# expect: 0 hit

# 3. 廣 grep — Odoo 全 addons 對 ir.attachment 的 ir.rule
grep -rEn "(model_ir_attachment|res_model.*'ir.attachment'|model.*'ir.attachment')" \
    odoo/addons/*/security/*.xml \
    odoo/addons/*/data/*.xml | grep -iE "rule|domain"
# expect: 0 hit

# 4. 任何 third-party addon 加裝 ir.attachment ir.rule for base.group_user
grep -rn "ir.attachment" path/to/third-party-addons/*/security/
# expect: 視 third-party 情境，需個別 audit
```

**Result 必須 0 std ir.rule on ir.attachment for base.group_user** — 否則新加 Layer 2a 與既有 std rule 走 per-group OR → 既有 std rule domain True → 整 OR True → Layer 2a deny 無效 → bypass。

對齊 [[cross-workspace-task-data-leak]] B1 root cause：「對 base.group_user 第一次加 ir.rule 必須對等加 group_system admin_full bypass」— Layer 2b 是這個 mirror。

### 為什麼 Layer 2a `perm_read/write/create/unlink` 都 True

`('res_model', '!=', 'woow_paas_platform.skill')` 對 non-skill attachment 評估 True → R/W/C/U 全 True → **既有 base.group_user 對任意 non-skill attachment 操作不變**。對 skill attachment 評估 False → deny 全 direction。

**Why 不直接設 `perm_read=False` 永遠 deny base.group_user**？:

- base.group_user 需要對 non-skill attachment R/W/C/U（mail.message attachment、product image 等 std Odoo behavior）
- 用 `perm_read=True` + domain filter 是 Odoo std pattern — `('res_model', '!=', SENSITIVE_MODEL)` 表達「除了某個 model 之外都 OK」

### 為什麼對 `ir.attachment._search` Python delegation 仍 active 不影響本 pattern

Odoo 18 `ir.attachment._search` (`addons/base/models/ir_attachment.py`) 對非 sudo context：

1. 先走 ORM `_search` evaluate ir.rule（Layer 2 在這裡 evaluate）
2. 後走 Python `_check(operation, value=values)` delegation — 對 `res_model='X' + res_id=Y` attachment 詢問 model X 對 user 是否 allow Y

對 skill bundle attachment：

- Layer 2a 已 deny base.group_user → ORM `_search` 在 step 1 就 deny → 不進 step 2 delegation ✓
- 對 sudo context：`is_superuser() return super()._search(...)` — sudo 直接 bypass，attachment delegation 不 evaluate ✓
- 即使未來 Odoo 改 step ordering（step 2 在前）→ skill model ACL `skill_rule_project_user` perm_read=True → delegation allow → attachment allow → 防線崩塌 → **Layer 1 anchor 不能單獨守，Layer 2 是必要**

**結論**：anchor 守 delegation target correctness（res_model 正確），rule 守 row-level access — 即使 Odoo internal 改變 ordering，兩層獨立有效。

## Anti-patterns（後人改本區前 STOP）

| 反 pattern | 問題 |
|-----------|------|
| 「只用 `groups='base.group_system'` field-level mask」 | HC-4 attack — `/web/content/<id>` enumerate id 旁路；mask 不在 path 上 |
| 「只用 attachment ir.rule 但 anchor stamping 漏 res_model」 | 既有 attachment 沒 stamp 或 stamp 為其他 model → Layer 2a `res_model != skill` 對 stamp 值 True → allow → bypass |
| 「只用 attachment ir.rule 但漏 `public=False` stamp」 | attacker set `public=True` 經 `/web/image` 或其他 public endpoint 旁路 ir.rule |
| 「Anchor 只在 create override 漏 write override」 | Admin re-assign attachment via `write({'bundle_attachment_id': new_att.id})` → 新 attachment 沒被 anchor → stamp 不對 → ir.rule 失效 |
| 「漏加 `ir_attachment_rule_skill_bundle_admin_full` group_system bypass」 | 對 base.group_user 第一次加 attachment ir.rule 漏對等 group_system → admin 後台 form 對 skill bundle attachment 進不去 → silent functional regression（重蹈 [[cross-workspace-task-data-leak]] B1） |
| 「沒 grep verify Odoo 18 base + bundled + third-party 0 std rule on ir.attachment for base.group_user」 | per-group OR semantics → 既有 std rule allow → Layer 2a deny 失效（重蹈 [[mcp-acl-6-rule-split]] OR-bypass 教訓） |
| 「對 N 個 model 各自加 N 條 attachment ir.rule but 漏統一 admin_full bypass rule」 | 維護 N pairs 容易漏一條；建議 admin_full rule 寫成 `[(1, '=', 1)]`（不限 res_model），一條覆蓋所有自家 attachment model |
| 「`_anchor_bundle_attachment` 在 base.group_user context 沒 sudo write」 | base.group_user 受 Layer 2a 限制無法寫 attachment field → anchor silent fail；必須 `att.sudo().write(updates)` |
| 「Anchor stamping 沒 `if updates:` guard 直接 write」 | 每次 read skill 都觸發 attachment write → 無謂 write traffic + 觸發 attachment write audit log noise |
| 「Anchor 對 missing field 報 error 而非 silent return」 | `if not att: return` 是必要的 — anchor 是 stamp，not 是 validation；validation 走 `@api.constrains` |
| 「Sub-model（如 skill bundle 內含的 sub-attachment）沒對等改 ir.rule」 | Sub-model 攻擊面 — sub-attachment 沒 row-level deny → bypass parent；若架構有 sub-attachment，**必須** 對等加 ir.rule（與 [[cross-workspace-task-data-leak]] sub-model parity 紅線一致） |
| 「Anchor stamping 對 admin 改寫 attachment res_model 沒 docstring warn destructive overwrite」 | EC-4a — admin 不知道 anchor 會切斷 mail.message attachment linkage；docstring 必須明示「destructive — 切斷原 res_model linkage」|

## Test scaffolding

### 必加 4-case matrix（見 §4 上面）

ORM read deny + role-promote regression + group_system regression + HTTP /web/content side-channel — 4 條最小集。

### Hijack reproducer（建議補強）

- create direction hijack（HC4.6）
- write direction hijack（HC4.5）
- `public=True` bypass（HC4.9）

### Test 路徑與檔名建議

- `src/tests/test_<model>_attachment_acl.py` — 對應 attachment ir.rule 行為矩陣
- 每條 test method 對應 cell map 編號 `test_HC4_<num>_<short_desc>`

### Independent grep verify 必加 CI / pre-merge check

```bash
# Pre-merge / release checklist：對 Odoo upgrade 後重 grep verify 0 std rule
grep -rEn "(model_ir_attachment|model.*'ir.attachment')" \
    /path/to/odoo/addons/*/security/*.xml \
    /path/to/odoo/addons/*/data/*.xml | grep -iE "rule"
# expect: 0 hit；Odoo 升版若加 std rule → 立刻評估是否需 deactivate
```

加進 deployment runbook 與 [[odoo-addon-std-rule-symmetric-deactivation]] pattern 對齊。

## See also

- 真實落地 case：
  - **Phase A HC-4**：`docs/archived/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md` §14.3 — skill bundle attachment row-level deny + anchor stamping closure
  - **Phase A HC-4**：`docs/archived/tracking/reviewer-report-2026-05-22-agent-runtime-pod-phase-a.md` §3.1 HC-4 + §12.3 closure 證據 + §12.4 EC-4a/b/c/d/e/f edge cases
  - **Phase A HC-4**：`docs/archived/tracking/caqa-report-2026-05-22-agent-runtime-pod-phase-a.md` §14.4 — independent grep verify Odoo 18 base 0 std rule on ir.attachment for base.group_user
- 互補 pattern：
  - [[odoo-addon-std-rule-symmetric-deactivation]] — 第一次加 base.group_user ir.rule 在某 model 上的 pattern；本 pattern 是該 pattern 的 attachment 版本 — 守 std rule 紅線 + 補 admin_full bypass
  - [[mcp-acl-6-rule-split]] — 6-rule split per model triplet；本 pattern 是「2-rule split for ir.attachment per sensitive model」（user_scope_deny + admin_full）— 抽象 invariant 一致
  - [[two-layer-acl-lockstep]] — 同 invariant 在 ir.rule + controller helper 兩層必須對齊；本 pattern 是「同 invariant 在 anchor (metadata stamping) + ir.rule (access enforcement) 兩層」— 不同 axis 的 defense in depth
  - [[unbound-project-acl-dual-leg]] — dual-leg domain 表達多種 row 可見性；本 pattern domain 更簡單（`!=` 二元 negation）
- 對應 root-cause pitfall：
  - [[cross-workspace-task-data-leak]] B1 — 第一次加 base.group_user ACL 必須對等加 admin_full bypass；本 pattern Layer 2b 是該 root cause 的對 ir.attachment 版本
- Reporter follow-up（EC-3/EC-4 surface 但不擋 merge）：
  - `docs/tracking/followups/follow-ups.md` F-PA-13~16 — HC-1 / HC-2 / EC-3a controller sudo lock-step / EC-4a anchor docstring 警告
- 關鍵 code：
  - `src/models/skill.py:_anchor_bundle_attachment` — Layer 1 anchor canonical implementation
  - `src/security/ir_rules.xml` `ir_attachment_rule_skill_bundle_user_scope` + `ir_attachment_rule_skill_bundle_admin_full` — Layer 2 ir.rule 兩條一組
  - `src/tests/test_agent_pod_phase_a_attachment_acl.py` — HC4.1-4.4 4 cell map
- Odoo 上游原理：
  - `ir.attachment._check(operation, value)` — Python delegation 邏輯，對 `res_model + res_id` set 的 attachment 回頭評估 target model ACL
  - `/web/content/<id>` controller `binary.py:content_common` — 走 `attachment._check('read')` → ir.rule + Python delegation
  - `/web/image/<id>` controller — 對 `public=True` 跳過 ir.rule（per `_get_serve_attachment` logic）— 故 Layer 1 anchor 強制 `public=False` 必要
  - per-group rule **OR semantics**：base.group_system user (繼承 base.group_user) 評估 OR(Layer 2a, Layer 2b) = OR(False, True) = True
