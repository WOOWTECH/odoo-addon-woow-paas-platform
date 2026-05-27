# Skill 後台管理 UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為 `woow_paas_platform.skill` 補上對齊 MCP pattern 的 Odoo 後台管理 UI（list + form + menu），含完整 state lifecycle transition 與 `is_latest` 不變式維護。

**Architecture:** 在 `skill.py` 補三個 transition method（`action_publish` / `action_deprecate` / `action_archive_skill`）與 `_recompute_is_latest` helper，state 改 readonly、新增 `published_at`；新增 `skill_views.xml`（list/form/action/menu），menu 限 `base.group_system`、禁後台 create。model 行為走 TDD，view 為宣告式變更 + smoke test 驗證載入與 admin-only。

**Tech Stack:** Odoo 18、Python（`odoo.tests.common.TransactionCase`）、Odoo XML views。

**Spec:** `docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md`

---

## 如何跑測試（後續 task 引用）

本專案測試透過 Docker worktree 環境。canonical 指令：

```bash
./scripts/test-addon.sh
```

此腳本會 `-u` 更新 `woow_paas_platform` 模組（套用新欄位 / view）並跑模組全部測試。TDD 時在輸出中關注本計畫新增的 test class（`TestSkillAdminLifecycle` / `TestSkillAdminViews`）的 PASS/FAIL/ERROR。

若你的環境可直接存取 `odoo-bin`（在 odoo-server root，worktree 已 mount），可加速只跑單一 class：

```bash
./odoo-bin -c odoo.conf -u woow_paas_platform \
  --test-enable --test-tags /woow_paas_platform:TestSkillAdminLifecycle \
  --stop-after-init
```

> Odoo 特性：新增 `published_at` 欄位或改 view 後，**必須 `-u` 更新模組**才會反映到 DB schema / UI；上面兩種指令都已含 `-u`。

---

## File Structure

| 檔案 | 責任 | 動作 |
|------|------|------|
| `src/models/skill.py` | skill model + lifecycle 行為 | 修改 |
| `src/views/skill_views.xml` | 後台 list / form / action / menu | 新增 |
| `src/__manifest__.py` | 模組 data 清單註冊 view | 修改 |
| `src/tests/test_skill_admin_lifecycle.py` | transition + `is_latest` 不變式行為測試 | 新增 |
| `src/tests/test_skill_admin_views.py` | view/menu 註冊 + admin-only smoke 測試 | 新增 |
| `src/tests/__init__.py` | 註冊兩個新 test module | 修改 |
| `docs/guides/paas-skill-system/10-roadmap-phases.md` | 修正 Phase A 後台 view doc drift | 修改 |

> **命名注意（plan 對 spec 的精化）**：spec §① 寫的 archive method 名為 `action_archive`，但 Odoo `BaseModel` 內建 `action_archive`（設 `active=False`）。skill 無 `active` 欄位，直接 override 會 shadow 標準語意。故本 plan 統一命名為 **`action_archive_skill`**，三處（test / model / view）一致。

---

## Task 1: Model lifecycle transitions + `is_latest` 不變式（TDD）

**Files:**
- Create: `src/tests/test_skill_admin_lifecycle.py`
- Modify: `src/tests/__init__.py`
- Modify: `src/models/skill.py`

- [ ] **Step 1: 寫 failing test**

Create `src/tests/test_skill_admin_lifecycle.py`:

```python
"""TDD — skill admin backend lifecycle transitions + is_latest invariant.

Spec: docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md §①
"""
import hashlib
import uuid

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSkillAdminLifecycle(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.skill' not in cls.env.registry:
            raise RuntimeError(
                "prerequisite: 'woow_paas_platform.skill' not registered."
            )
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'Skill-LC %s' % uuid.uuid4().hex[:8]}
        )

    def _make_skill(self, name, version, state='draft'):
        """Create a skill with the required bundle_sha256 (default draft)."""
        digest = hashlib.sha256(
            ('%s-%s' % (name, version)).encode()
        ).hexdigest()
        return self.Skill.sudo().create({
            'name': name,
            'version': version,
            'project_id': self.project.id,
            'bundle_sha256': digest,
            'state': state,
        })

    def _latest_count(self, name):
        return self.Skill.sudo().search_count([
            ('project_id', '=', self.project.id),
            ('name', '=', name),
            ('is_latest', '=', True),
        ])

    # ---- action_publish ------------------------------------------------ #
    def test_publish_from_draft_sets_latest(self):
        skill = self._make_skill('alpha', '1.0.0')
        skill.action_publish()
        self.assertEqual(skill.state, 'published')
        self.assertTrue(skill.is_latest)
        self.assertTrue(skill.published_at, "published_at must be stamped")

    def test_publish_second_version_steals_latest(self):
        v1 = self._make_skill('beta', '1.0.0')
        v1.action_publish()
        v2 = self._make_skill('beta', '2.0.0')
        v2.action_publish()
        self.assertTrue(v2.is_latest)
        self.assertFalse(v1.is_latest, "older version must lose is_latest")
        self.assertEqual(self._latest_count('beta'), 1)

    # ---- action_deprecate ---------------------------------------------- #
    def test_deprecate_latest_reassigns_to_next_published(self):
        v1 = self._make_skill('gamma', '1.0.0')
        v1.action_publish()
        v2 = self._make_skill('gamma', '2.0.0')
        v2.action_publish()  # v2 is latest
        v2.action_deprecate()
        self.assertEqual(v2.state, 'deprecated')
        self.assertFalse(v2.is_latest)
        self.assertTrue(v1.is_latest, "is_latest must fall back to v1")
        self.assertEqual(self._latest_count('gamma'), 1)

    def test_deprecate_only_published_clears_latest(self):
        skill = self._make_skill('delta', '1.0.0')
        skill.action_publish()
        skill.action_deprecate()
        self.assertFalse(skill.is_latest)
        self.assertEqual(self._latest_count('delta'), 0)

    # ---- action_archive_skill ------------------------------------------ #
    def test_archive_clears_latest_and_recomputes(self):
        v1 = self._make_skill('eps', '1.0.0')
        v1.action_publish()
        v2 = self._make_skill('eps', '2.0.0')
        v2.action_publish()  # v2 latest
        v2.action_archive_skill()
        self.assertEqual(v2.state, 'archived')
        self.assertFalse(v2.is_latest)
        self.assertTrue(v1.is_latest)
        self.assertEqual(self._latest_count('eps'), 1)

    # ---- illegal transition guards ------------------------------------- #
    def test_publish_from_archived_raises(self):
        skill = self._make_skill('zeta', '1.0.0')
        skill.action_archive_skill()
        with self.assertRaises(UserError):
            skill.action_publish()

    def test_deprecate_from_draft_raises(self):
        skill = self._make_skill('eta', '1.0.0')
        with self.assertRaises(UserError):
            skill.action_deprecate()

    # ---- invariant ----------------------------------------------------- #
    def test_invariant_at_most_one_latest_per_group(self):
        v1 = self._make_skill('theta', '1.0.0')
        v2 = self._make_skill('theta', '2.0.0')
        v3 = self._make_skill('theta', '3.0.0')
        v1.action_publish()
        v2.action_publish()
        v3.action_publish()
        v3.action_deprecate()   # published now {v1, v2} → v2 latest
        v1.action_archive_skill()  # published now {v2} → v2 latest
        self.assertLessEqual(self._latest_count('theta'), 1)
        self.assertTrue(v2.is_latest, "v2 must be the surviving latest")
```

- [ ] **Step 2: 把 test module 註冊到 `src/tests/__init__.py`**

Edit `src/tests/__init__.py` — 在 enablement import 後加一行：

```python
from . import test_agent_pod_phase_d_enablement            # WS3 UI + seed
from . import test_skill_admin_lifecycle                   # skill backend lifecycle
```

- [ ] **Step 3: 跑測試確認紅**

Run: `./scripts/test-addon.sh`
Expected: `TestSkillAdminLifecycle` 全紅 — `action_publish` / `action_deprecate` / `action_archive_skill` 不存在（AttributeError），且 `published_at` 欄位不存在。

- [ ] **Step 4: 實作 model 變更**

在 `src/models/skill.py` 做三處修改。

**(a) import `UserError`** — 把第 1 行：

```python
from odoo import api, fields, models
```

改為：

```python
from odoo import api, fields, models
from odoo.exceptions import UserError
```

**(b) `state` 加 `readonly`，並在其後新增 `published_at`** — 把現有 `state` 欄位區塊：

```python
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('published', 'Published'),
            ('deprecated', 'Deprecated'),
            ('archived', 'Archived'),
        ],
        default='draft',
        required=True,
        index=True,
    )
```

改為：

```python
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('published', 'Published'),
            ('deprecated', 'Deprecated'),
            ('archived', 'Archived'),
        ],
        default='draft',
        required=True,
        index=True,
        readonly=True,
    )
    published_at = fields.Datetime(
        string='Published At',
        readonly=True,
        help='Timestamp of the most recent publish; drives is_latest recompute.',
    )
```

**(c) 在檔尾（`_anchor_bundle_attachment` method 之後）新增 transition methods + helper：**

```python

    # ------------------------------------------------------------------ #
    # Lifecycle transitions (admin backend) — maintain is_latest invariant#
    # ------------------------------------------------------------------ #
    def action_publish(self):
        """draft / deprecated → published. Stamp published_at and recompute
        the (project, name) is_latest pointer."""
        self.ensure_one()
        if self.state not in ('draft', 'deprecated'):
            raise UserError(
                "Skill '%s' v%s cannot be published from state '%s' "
                "(only draft / deprecated)."
                % (self.name, self.version, self.state)
            )
        self.write({
            'state': 'published',
            'published_at': fields.Datetime.now(),
        })
        self._recompute_is_latest(self.project_id.id, self.name)

    def action_deprecate(self):
        """published → deprecated. Recompute hands is_latest to the next
        newest published sibling (or clears it)."""
        self.ensure_one()
        if self.state != 'published':
            raise UserError(
                "Skill '%s' v%s cannot be deprecated from state '%s' "
                "(only published)." % (self.name, self.version, self.state)
            )
        self.write({'state': 'deprecated'})
        self._recompute_is_latest(self.project_id.id, self.name)

    def action_archive_skill(self):
        """Any non-archived state → archived (terminal). Recompute is_latest.

        Named ``action_archive_skill`` (not ``action_archive``) to avoid
        shadowing Odoo BaseModel.action_archive (active=False toggle); skill
        has no ``active`` field.
        """
        self.ensure_one()
        if self.state == 'archived':
            raise UserError(
                "Skill '%s' v%s is already archived."
                % (self.name, self.version)
            )
        self.write({'state': 'archived'})
        self._recompute_is_latest(self.project_id.id, self.name)

    def _recompute_is_latest(self, project_id, name):
        """Invariant: at most one is_latest=True per (project_id, name),
        and it is the most-recently-published (published_at desc, id desc)
        skill in state 'published'."""
        epoch = fields.Datetime.to_datetime('1970-01-01 00:00:00')
        group = self.sudo().search([
            ('project_id', '=', project_id),
            ('name', '=', name),
        ])
        published = group.filtered(lambda s: s.state == 'published')
        winner = published.sorted(
            key=lambda s: (s.published_at or epoch, s.id),
            reverse=True,
        )[:1]
        for skill in group:
            desired = bool(winner) and skill.id == winner.id
            if skill.is_latest != desired:
                skill.is_latest = desired
```

- [ ] **Step 5: 跑測試確認綠**

Run: `./scripts/test-addon.sh`
Expected: `TestSkillAdminLifecycle` 全 PASS（8 個 test method）。

- [ ] **Step 6: Commit**

```bash
git add src/models/skill.py src/tests/test_skill_admin_lifecycle.py src/tests/__init__.py
git commit -m "feat(skill): add lifecycle transition methods + is_latest invariant"
```

---

## Task 2: 後台 view + manifest 註冊 + smoke test

**Files:**
- Create: `src/tests/test_skill_admin_views.py`
- Modify: `src/tests/__init__.py`
- Create: `src/views/skill_views.xml`
- Modify: `src/__manifest__.py:39`

- [ ] **Step 1: 寫 failing smoke test**

Create `src/tests/test_skill_admin_views.py`:

```python
"""Smoke — skill admin backend views registered + admin-only menu.

Spec: docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md §②③
"""
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSkillAdminViews(TransactionCase):

    def test_skill_action_registered(self):
        action = self.env.ref('woow_paas_platform.action_skill')
        self.assertEqual(
            action.res_model, 'woow_paas_platform.skill',
            "action_skill must target the skill model",
        )
        self.assertIn(
            "'create': False", action.context or '',
            "action context must disable backend create",
        )

    def test_skill_menu_admin_only(self):
        menu = self.env.ref('woow_paas_platform.menu_skills')
        group_system = self.env.ref('base.group_system')
        self.assertIn(
            group_system, menu.groups_id,
            "menu_skills must be restricted to base.group_system",
        )
```

- [ ] **Step 2: 把 smoke test 註冊到 `src/tests/__init__.py`**

Edit `src/tests/__init__.py` — 在 Task 1 加的 lifecycle import 後加一行：

```python
from . import test_skill_admin_lifecycle                   # skill backend lifecycle
from . import test_skill_admin_views                       # skill backend views smoke
```

- [ ] **Step 3: 跑測試確認紅**

Run: `./scripts/test-addon.sh`
Expected: `TestSkillAdminViews` 兩個 test 紅 — `env.ref('woow_paas_platform.action_skill')` / `menu_skills` 找不到 xmlid（view 未建、manifest 未註冊）。

- [ ] **Step 4: 建立 view 檔**

Create `src/views/skill_views.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <!-- Skill List View -->
    <record id="view_skill_list" model="ir.ui.view">
        <field name="name">woow_paas_platform.skill.list</field>
        <field name="model">woow_paas_platform.skill</field>
        <field name="arch" type="xml">
            <list string="Skills" create="false">
                <field name="project_id"/>
                <field name="name"/>
                <field name="version"/>
                <field name="state" widget="badge"
                       decoration-success="state == 'published'"
                       decoration-warning="state == 'deprecated'"
                       decoration-muted="state == 'archived'"
                       decoration-info="state == 'draft'"/>
                <field name="is_latest" widget="boolean_toggle" readonly="1"/>
                <field name="bundle_size_kb"/>
                <field name="uploaded_by_uid"/>
                <field name="uploaded_at"/>
            </list>
        </field>
    </record>

    <!-- Skill Form View -->
    <record id="view_skill_form" model="ir.ui.view">
        <field name="name">woow_paas_platform.skill.form</field>
        <field name="model">woow_paas_platform.skill</field>
        <field name="arch" type="xml">
            <form string="Skill" create="false">
                <header>
                    <button name="action_publish"
                            type="object"
                            string="Publish"
                            class="btn-primary"
                            icon="fa-cloud-upload"
                            invisible="state not in ('draft', 'deprecated')"/>
                    <button name="action_deprecate"
                            type="object"
                            string="Deprecate"
                            class="btn-secondary"
                            icon="fa-archive"
                            invisible="state != 'published'"/>
                    <button name="action_archive_skill"
                            type="object"
                            string="Archive"
                            class="btn-secondary"
                            icon="fa-trash"
                            invisible="state == 'archived'"/>
                    <field name="state" widget="statusbar"
                           statusbar_visible="draft,published,deprecated,archived"/>
                </header>
                <sheet>
                    <div class="oe_title">
                        <h1><field name="name" readonly="1"/></h1>
                        <h3><field name="version" readonly="1"/></h3>
                    </div>
                    <group>
                        <group string="Identity">
                            <field name="title" readonly="1"/>
                            <field name="is_latest" readonly="1"/>
                            <field name="project_id" readonly="1"/>
                        </group>
                        <group string="Bundle">
                            <field name="bundle_sha256" readonly="1"/>
                            <field name="bundle_size_kb" readonly="1"/>
                            <field name="uploaded_by_uid" readonly="1"/>
                            <field name="uploaded_at" readonly="1"/>
                            <field name="published_at" readonly="1"/>
                        </group>
                    </group>
                    <group string="Description">
                        <field name="description" readonly="1" nolabel="1"/>
                    </group>
                    <notebook>
                        <page string="Metadata" name="metadata">
                            <field name="skill_metadata" readonly="1" nolabel="1"/>
                        </page>
                    </notebook>
                </sheet>
            </form>
        </field>
    </record>

    <!-- Skill Action -->
    <record id="action_skill" model="ir.actions.act_window">
        <field name="name">Skills</field>
        <field name="res_model">woow_paas_platform.skill</field>
        <field name="view_mode">list,form</field>
        <field name="context">{'create': False}</field>
        <field name="help" type="html">
            <p class="o_view_nocontent_smiling_face">
                No skills yet
            </p>
            <p>
                Skills are project-scoped agent bundles uploaded via the
                agent-runtime API. Manage their publish / deprecate / archive
                lifecycle here.
            </p>
        </field>
    </record>

    <!-- Menu under Woow PaaS (admin only) -->
    <menuitem
        id="menu_skills"
        name="Skills"
        parent="menu_woow_paas_platform_root"
        action="action_skill"
        groups="base.group_system"
        sequence="60"/>
</odoo>
```

- [ ] **Step 5: 在 manifest 註冊 view**

Edit `src/__manifest__.py` — 把第 39 行：

```python
        "views/mcp_server_views.xml",
```

改為（在其後加一行）：

```python
        "views/mcp_server_views.xml",
        "views/skill_views.xml",
```

- [ ] **Step 6: 跑測試確認綠（同時驗證 XML 載入無誤）**

Run: `./scripts/test-addon.sh`
Expected: `TestSkillAdminViews` 兩個 test PASS；模組 `-u` 載入 `skill_views.xml` 無 XML/欄位錯誤（form 引用的 `published_at` 已於 Task 1 建立）。`TestSkillAdminLifecycle` 維持綠。

- [ ] **Step 7: Commit**

```bash
git add src/views/skill_views.xml src/__manifest__.py src/tests/test_skill_admin_views.py src/tests/__init__.py
git commit -m "feat(skill): add admin backend list/form/menu views"
```

---

## Task 3: 修正 roadmap doc drift

**Files:**
- Modify: `docs/guides/paas-skill-system/10-roadmap-phases.md:36`

背景：經查 `src/views/` 內 agent_template / agent_session / project_access / skill **四個 model 皆無任何 view**，故原文「+ 4 個後台 view」為錯誤聲稱。skill 後台 view 由本分支補上，其餘三個仍無 view。

- [ ] **Step 1: Edit doc**

Edit `docs/guides/paas-skill-system/10-roadmap-phases.md` — 把第 36 行：

```markdown
| **A** | schema M:N 遷移（`cloud_service_id`→`cloud_service_ids`）+ 新增 4 個 model（agent_template / agent_session / skill / project_access）+ 16 條 ACL + 4 個後台 view | ✅ |
```

改為：

```markdown
| **A** | schema M:N 遷移（`cloud_service_id`→`cloud_service_ids`）+ 新增 4 個 model（agent_template / agent_session / skill / project_access）+ 16 條 ACL（**未含後台 view**；skill 後台管理 view 於 `feat/skill-list` 補上，見 `docs/superpowers/specs/2026-05-27-skill-admin-backend-ui-design.md`） | ✅ |
```

- [ ] **Step 2: Commit**

```bash
git add docs/guides/paas-skill-system/10-roadmap-phases.md
git commit -m "docs(skill): correct Phase A backend-view drift in roadmap"
```

---

## Self-Review

**1. Spec coverage:**

| Spec 段 | 對應 task |
|---------|-----------|
| ① Model（`published_at` / state readonly / `_recompute_is_latest` / 3 transition） | Task 1 |
| ② View（list / form / action / menu） | Task 2 Step 4 |
| ③ 安全（menu `group_system`、禁 create、既有 ACL 不動） | Task 2（smoke 驗 menu admin-only + action create:False） |
| ④ Manifest 註冊 | Task 2 Step 5 |
| ⑤ Test | Task 1（行為）+ Task 2（smoke） |
| ⑥ Doc 修正 | Task 3 |

全段覆蓋，無遺漏。

**2. Placeholder scan:** 無 TBD / TODO；所有 test 與 impl code 完整給出。

**3. Type consistency:** `action_publish` / `action_deprecate` / `action_archive_skill` / `_recompute_is_latest` / `published_at` 在 test、`skill.py`、`skill_views.xml` 三處命名一致；xmlid `action_skill` / `menu_skills` / `view_skill_list` / `view_skill_form` 一致。archive method 統一為 `action_archive_skill`（已避開 spec 原文 `action_archive` 的 BaseModel shadow 風險）。

**4. 已知邊界 / 待實作時驗證（來自 spec §「實作注意」）：**
- `group_system` 對 skill 既有 project-based `ir.rule` 的可見性 — list 要全域顯示跨 project，須確認 admin 不被 record rule 擋。Odoo system group 通常 bypass record rule；Task 2 Step 6 載入後可在後台手動確認列表能看到不同 project 的 skill。
- `is_latest` 雙寫漂移（既有上傳 API 直建 published 會繞過 `_recompute_is_latest`）為 spec 列明的 follow-up，**不在本 plan 範圍**。
