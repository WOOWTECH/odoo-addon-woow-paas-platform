# Skill Dual-Scope Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `woow_paas_platform.skill` dual-scope (system / user, mirroring `mcp_server`) so platform-wide system skills load into every project's agent-runtime pod, while project (user) skills stay scoped — with no upload UI yet (system skills seeded via a `data` record).

**Architecture:** Add a `scope` Selection + nullable `project_id` + D4 constraint to the skill model; replace the NULL-unsafe `UNIQUE(project_id,name,version)` with two partial unique indexes; make `is_latest` recompute scope-aware; mirror MCP's scope-split ir.rules; union published system skills into the pod snapshot + add a `scope='system'` fallback leg to the download endpoint; add a template subset constraint; migrate existing rows to `scope='user'`. Pure Odoo-side change — the `paas-agent-runtime` contract is untouched.

**Tech Stack:** Odoo 18, PostgreSQL (partial unique indexes), Python ORM, XML ir.rule/data, Odoo migration scripts.

**Spec:** `docs/plans/2026-Q2/2026-05-30-skill-dual-scope-design.md`

**Branch:** `feat/skill-dual-scope` (already created off `develop`; design doc committed at `142cf42`).

---

## File Structure

| File | Responsibility | Action |
|------|----------------|--------|
| `src/models/skill.py` | `scope` field, nullable `project_id`, D4 constraint, partial-index `init()`, scope-aware `_recompute_is_latest` | Modify |
| `src/models/project_project.py` | `skill_ids` reverse O2M | Modify |
| `src/models/agent_template.py` | `_check_skill_subset` constraint | Modify |
| `src/controllers/ai_assistant.py` | `_build_skill_snapshot` unions system skills | Modify |
| `src/controllers/agent_runtime_skill.py` | download dual-leg (project OR system) | Modify |
| `src/security/ir_rules.xml` | retire 3 project rules → 3 user-scope rules + system_readonly | Modify |
| `src/migrations/18.0.1.0.29/pre-migrate.py` | add `scope` column + backfill `'user'` | Create |
| `src/migrations/18.0.1.0.29/post-migrate.py` | drop old UNIQUE, create partial indexes, retire old rule xmlids | Create |
| `src/data/demo_system_skill.xml` | seed one `hello-world` system skill | Create |
| `src/__manifest__.py` | version → 18.0.1.0.29, register demo seed | Modify |
| `src/tests/test_skill_dual_scope.py` | all new dual-scope tests | Create |
| `src/tests/test_agent_pod_phase_c_sse_gateway.py` | SSE.6 count → membership | Modify |
| `src/tests/test_agent_pod_phase_a_acl_new_models.py` | SK-6 misleading message | Modify |
| `docs/guides/paas-skill-system/*` | system skill loading + cross-repo note | Modify |

**Test command (per CLAUDE.md):**
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform --stop-after-init
```
Targeted single class (faster during dev): append `:TestClassName` e.g. `--test-tags /woow_paas_platform:TestSkillDualScope`. In the Docker dev env use `./scripts/test-addon.sh` (runs the same with the worktree's DB).

---

## Task 1: Model — `scope` field, nullable `project_id`, D4 constraint

**Files:**
- Modify: `src/models/skill.py` (imports, fields, new `@api.constrains`)
- Test: `src/tests/test_skill_dual_scope.py`

- [ ] **Step 1: Write the failing tests**

Create `src/tests/test_skill_dual_scope.py`:

```python
import uuid

from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestSkillDualScope(TransactionCase):
    """Dual-scope schema: scope field, nullable project_id, D4 constraint."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.project = self.Project.sudo().create({'name': f'p-{self.suffix}'})

    def test_scope_field_exists_default_user(self):
        fields_ = self.Skill._fields
        self.assertIn('scope', fields_, "skill must declare a scope field")
        self.assertEqual(fields_['scope'].type, 'selection')
        self.assertEqual(fields_['scope'].default(self.Skill), 'user')

    def test_user_scope_requires_project(self):
        with self.assertRaises(ValidationError):
            self.Skill.sudo().create({
                'name': f'u-{self.suffix}', 'version': '0.1.0',
                'bundle_sha256': 'a' * 64, 'scope': 'user',
                # project_id intentionally omitted
            })

    def test_system_scope_forbids_project(self):
        with self.assertRaises(ValidationError):
            self.Skill.sudo().create({
                'name': f's-{self.suffix}', 'version': '0.1.0',
                'bundle_sha256': 'a' * 64, 'scope': 'system',
                'project_id': self.project.id,
            })

    def test_system_scope_creates_without_project(self):
        sk = self.Skill.sudo().create({
            'name': f'sys-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'a' * 64, 'scope': 'system',
        })
        self.assertFalse(sk.project_id)
        self.assertEqual(sk.scope, 'system')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillDualScope --stop-after-init`
Expected: FAIL — `scope` field missing / `user` create succeeds without project (no constraint yet).

- [ ] **Step 3: Add `ValidationError` import**

In `src/models/skill.py`, change the exceptions import (currently `from odoo.exceptions import UserError`):

```python
from odoo.exceptions import UserError, ValidationError
```

- [ ] **Step 4: Add the `scope` field and relax `project_id`**

In `src/models/skill.py`, replace the `project_id` field block (currently `skill.py:33-38`):

```python
    scope = fields.Selection(
        selection=[('system', 'System'), ('user', 'User')],
        string='Scope',
        default='user',
        required=True,
        index=True,
        help="system = platform-wide (loaded into every project's pod); "
             "user = bound to a single project.",
    )

    project_id = fields.Many2one(
        'project.project',
        ondelete='cascade',
        index=True,
        help='Set for user-scope skills; NULL for system-scope skills.',
    )
```

- [ ] **Step 5: Add the D4 constraint**

In `src/models/skill.py`, add this method (place it right after the field declarations, before `create`):

```python
    @api.constrains('scope', 'project_id')
    def _check_scope_project_consistency(self):
        # D4 invariant (mirrors mcp_server): scope='user' iff project_id NOT NULL.
        for record in self:
            if record.scope == 'user' and not record.project_id:
                raise ValidationError(
                    "User-scope skill '%s' must be linked to a project "
                    "(scope='user' requires project_id)." % record.name
                )
            if record.scope == 'system' and record.project_id:
                raise ValidationError(
                    "System-scope skill '%s' must not be linked to a project "
                    "(scope='system' requires project_id=NULL)." % record.name
                )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillDualScope --stop-after-init`
Expected: PASS (4 tests).

- [ ] **Step 7: Commit**

```bash
git add src/models/skill.py src/tests/test_skill_dual_scope.py
git commit -m "feat: add scope field + D4 constraint to skill model"
```

---

## Task 2: UNIQUE — two partial indexes via `init()`, drop `_sql_constraints`

**Files:**
- Modify: `src/models/skill.py` (remove `_sql_constraints`, add `init`)
- Test: `src/tests/test_skill_dual_scope.py`

- [ ] **Step 1: Write the failing tests**

Append to `TestSkillDualScope` in `src/tests/test_skill_dual_scope.py`:

```python
    def test_user_dup_same_project_name_version_rejected(self):
        self.Skill.sudo().create({
            'name': 'dup', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'user', 'project_id': self.project.id,
        })
        with self.assertRaises(Exception):  # noqa: BLE001  (IntegrityError)
            self.Skill.sudo().create({
                'name': 'dup', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
                'scope': 'user', 'project_id': self.project.id,
            })
            self.env.flush_all()

    def test_system_dup_same_name_version_rejected(self):
        self.Skill.sudo().create({
            'name': 'sysdup', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'system',
        })
        with self.assertRaises(Exception):  # noqa: BLE001
            self.Skill.sudo().create({
                'name': 'sysdup', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
                'scope': 'system',
            })
            self.env.flush_all()

    def test_cross_scope_same_name_version_allowed(self):
        sys_sk = self.Skill.sudo().create({
            'name': 'shared', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'system',
        })
        usr_sk = self.Skill.sudo().create({
            'name': 'shared', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
            'scope': 'user', 'project_id': self.project.id,
        })
        self.env.flush_all()
        self.assertTrue(sys_sk.id and usr_sk.id)
```

- [ ] **Step 2: Run tests to verify the cross-scope one fails**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillDualScope --stop-after-init`
Expected: `test_cross_scope_same_name_version_allowed` FAILS (old `UNIQUE(project_id,name,version)` treats `(NULL,'shared','0.1.0')` and `(proj,'shared','0.1.0')` as distinct — actually allowed — but `test_system_dup...` FAILS because old UNIQUE does NOT dedupe two `(NULL,'sysdup','0.1.0')` rows). Confirm `test_system_dup_same_name_version_rejected` fails (duplicate system rows both insert).

- [ ] **Step 3: Remove `_sql_constraints` and add `init()`**

In `src/models/skill.py`, delete the `_sql_constraints` block (currently `skill.py:92-98`) and add an `init` method in its place:

```python
    def init(self):
        # UNIQUE(project_id, name, version) cannot dedupe system skills:
        # Postgres treats NULL project_id as distinct. Use two partial unique
        # indexes (replaces the old _sql_constraints UNIQUE). IF NOT EXISTS so
        # this is idempotent on every -u / fresh install.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS skill_uniq_user_proj_name_version
            ON woow_paas_platform_skill (project_id, name, version)
            WHERE project_id IS NOT NULL
        """)
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS skill_uniq_system_name_version
            ON woow_paas_platform_skill (name, version)
            WHERE project_id IS NULL
        """)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillDualScope --stop-after-init`
Expected: PASS (all 7 tests). Also confirm the legacy `TestPhaseASkillModel.test_skill_unique_name_version_per_project` (N.8) still PASSES — the user-leg partial index preserves its behavior:
`--test-tags /woow_paas_platform:TestPhaseASkillModel`

- [ ] **Step 5: Commit**

```bash
git add src/models/skill.py src/tests/test_skill_dual_scope.py
git commit -m "feat: replace skill UNIQUE with NULL-aware partial indexes"
```

---

## Task 3: `is_latest` — scope-aware recompute

**Files:**
- Modify: `src/models/skill.py` (`_recompute_is_latest` + call sites)
- Test: `src/tests/test_skill_dual_scope.py`

- [ ] **Step 1: Write the failing test**

Append to `TestSkillDualScope`:

```python
    def test_system_is_latest_grouped_platform_wide(self):
        v1 = self.Skill.sudo().create({
            'name': 'syslatest', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'system', 'state': 'published',
        })
        v2 = self.Skill.sudo().create({
            'name': 'syslatest', 'version': '0.2.0', 'bundle_sha256': 'b' * 64,
            'scope': 'system', 'state': 'published',
        })
        v1.invalidate_recordset()
        v2.invalidate_recordset()
        self.assertFalse(v1.is_latest, "older system version loses is_latest")
        self.assertTrue(v2.is_latest, "newest published system version is latest")

    def test_system_and_user_is_latest_independent(self):
        sys_sk = self.Skill.sudo().create({
            'name': 'indep', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'scope': 'system', 'state': 'published',
        })
        usr_sk = self.Skill.sudo().create({
            'name': 'indep', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
            'scope': 'user', 'project_id': self.project.id, 'state': 'published',
        })
        sys_sk.invalidate_recordset()
        usr_sk.invalidate_recordset()
        self.assertTrue(sys_sk.is_latest, "system bucket has its own latest")
        self.assertTrue(usr_sk.is_latest, "user bucket has its own latest")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillDualScope --stop-after-init`
Expected: FAIL — current `_recompute_is_latest(project_id, name)` groups system skills by `project_id IS NULL` only coincidentally and is called with positional args; the call sites pass `skill.project_id.id` which is `False` for system → `('project_id','=',False)` but without the `scope='system'` qualifier it would also pull any `project_id IS NULL` user-row anomalies. The new tests lock the scope-aware grouping.

- [ ] **Step 3: Add `_is_latest_group_domain` and refactor `_recompute_is_latest`**

In `src/models/skill.py`, replace the entire `_recompute_is_latest` method (currently `skill.py:202-220`) with:

```python
    def _is_latest_group_domain(self):
        """Domain for the bucket this skill competes in for the is_latest
        pointer. System skills group platform-wide per name (project_id NULL);
        user skills group per (project_id, name)."""
        self.ensure_one()
        if self.scope == 'system':
            return [
                ('scope', '=', 'system'),
                ('project_id', '=', False),
                ('name', '=', self.name),
            ]
        return [
            ('scope', '=', 'user'),
            ('project_id', '=', self.project_id.id),
            ('name', '=', self.name),
        ]

    def _recompute_is_latest(self):
        """Invariant: at most one is_latest=True per (scope, project, name)
        group, and it is the most-recently-published (published_at desc,
        id desc) skill in state 'published'."""
        self.ensure_one()
        group = self.sudo().search(self._is_latest_group_domain())
        published = group.filtered(lambda s: s.state == 'published')
        winner = published.sorted(
            key=lambda s: (s.published_at or datetime.min, s.id),
            reverse=True,
        )[:1]
        for skill in group:
            desired = skill.id == winner.id
            if skill.is_latest != desired:
                skill.write({'is_latest': desired})
```

- [ ] **Step 4: Update all `_recompute_is_latest` call sites**

In `src/models/skill.py`, replace the 4 positional calls:

In `create` (currently `skill.py:114`):
```python
                skill._recompute_is_latest()
```
In `write` (currently `skill.py:129`):
```python
                skill._recompute_is_latest()
```
In `action_publish` (currently `skill.py:172`):
```python
        self._recompute_is_latest()
```
In `action_deprecate` (currently `skill.py:184`):
```python
        self._recompute_is_latest()
```
In `action_archive_skill` (currently `skill.py:200`):
```python
        self._recompute_is_latest()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillDualScope --stop-after-init`
Expected: PASS. Also re-run the legacy is_latest tests to confirm no regression:
`--test-tags /woow_paas_platform:TestSkillAdminLifecycle` and `--test-tags /woow_paas_platform:TestSkillIsLatestDriftRegression`

- [ ] **Step 6: Commit**

```bash
git add src/models/skill.py src/tests/test_skill_dual_scope.py
git commit -m "feat: scope-aware is_latest recompute for skill"
```

---

## Task 4: ACL — retire 3 project rules, add 3 user-scope + 1 system_readonly

**Files:**
- Modify: `src/security/ir_rules.xml` (lines 379-423 region)
- Test: `src/tests/test_skill_dual_scope.py`

- [ ] **Step 1: Write the failing tests**

Append a new ACL test class to `src/tests/test_skill_dual_scope.py`:

```python
@tagged('post_install', '-at_install')
class TestSkillDualScopeAcl(TransactionCase):
    """system_readonly + user-scope leg + scope-flip lock."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']
        cls.PAccess = cls.env['woow_paas_platform.project_access']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.alice = self.env['res.users'].sudo().create({
            'name': 'Alice', 'login': f'alice-{self.suffix}',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.project = self.Project.sudo().create({'name': f'p-{self.suffix}'})
        self.PAccess.sudo().create({
            'project_id': self.project.id,
            'partner_id': self.alice.partner_id.id, 'role': 'owner',
        })
        self.system_skill = self.Skill.sudo().create({
            'name': f'sys-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'a' * 64, 'scope': 'system', 'state': 'published',
        })
        self.user_skill = self.Skill.sudo().create({
            'name': f'usr-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'b' * 64, 'scope': 'user',
            'project_id': self.project.id,
        })

    def test_group_user_can_read_system_skill(self):
        rec = self.Skill.with_user(self.alice).search([
            ('id', '=', self.system_skill.id)])
        self.assertEqual(rec.ids, [self.system_skill.id],
                         "any internal user can read a system skill")

    def test_group_user_cannot_create_system_skill(self):
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.Skill.with_user(self.alice).create({
                'name': f'evil-{self.suffix}', 'version': '0.1.0',
                'bundle_sha256': 'c' * 64, 'scope': 'system',
            })

    def test_group_user_cannot_flip_user_skill_to_system(self):
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.user_skill.with_user(self.alice).write({
                'scope': 'system', 'project_id': False,
            })
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillDualScopeAcl --stop-after-init`
Expected: FAIL — `test_group_user_can_read_system_skill` fails (current 3 rules walk `project_id.project_access_ids`; system skill has NULL project → no rule matches → Alice cannot read it).

- [ ] **Step 3: Replace the 3 project rules + add system_readonly**

In `src/security/ir_rules.xml`, replace the block from `<!-- skill (design §3.2 — 4 rules) -->` through the end of `skill_rule_project_guest` (currently lines 379-412 — i.e. the comment plus the three `skill_rule_project_admin/user/guest` records, **keep `skill_rule_admin_full` at 414-423 untouched**) with:

```xml
        <!-- skill (dual-scope — design 2026-05-30) -->
        <!-- 3 user-scope rules (project_access walk AND scope='user') +     -->
        <!-- 1 system_readonly (all internal users read system skills) +      -->
        <!-- admin_full (unchanged below). Old skill_rule_project_* xmlids are -->
        <!-- retired in migration 18.0.1.0.29 (two-DELETE) to avoid OR-combine.-->

        <record id="skill_rule_user_admin" model="ir.rule">
            <field name="name">Skill: user-scope project admin/owner full CRUD</field>
            <field name="model_id" ref="model_woow_paas_platform_skill"/>
            <field name="domain_force">['&amp;', ('scope', '=', 'user'), ('project_id.project_access_ids', 'any', [('user_id', '=', user.id), ('role', 'in', ['owner', 'admin'])])]</field>
            <field name="groups" eval="[(4, ref('base.group_user'))]"/>
            <field name="perm_read" eval="True"/>
            <field name="perm_write" eval="True"/>
            <field name="perm_create" eval="True"/>
            <field name="perm_unlink" eval="True"/>
        </record>

        <record id="skill_rule_user_member" model="ir.rule">
            <field name="name">Skill: user-scope project user read-only</field>
            <field name="model_id" ref="model_woow_paas_platform_skill"/>
            <field name="domain_force">['&amp;', ('scope', '=', 'user'), ('project_id.project_access_ids', 'any', [('user_id', '=', user.id), ('role', '=', 'user')])]</field>
            <field name="groups" eval="[(4, ref('base.group_user'))]"/>
            <field name="perm_read" eval="True"/>
            <field name="perm_write" eval="False"/>
            <field name="perm_create" eval="False"/>
            <field name="perm_unlink" eval="False"/>
        </record>

        <record id="skill_rule_user_guest" model="ir.rule">
            <field name="name">Skill: user-scope project guest read-only</field>
            <field name="model_id" ref="model_woow_paas_platform_skill"/>
            <field name="domain_force">['&amp;', ('scope', '=', 'user'), ('project_id.project_access_ids', 'any', [('user_id', '=', user.id), ('role', '=', 'guest')])]</field>
            <field name="groups" eval="[(4, ref('base.group_user'))]"/>
            <field name="perm_read" eval="True"/>
            <field name="perm_write" eval="False"/>
            <field name="perm_create" eval="False"/>
            <field name="perm_unlink" eval="False"/>
        </record>

        <record id="skill_rule_system_readonly" model="ir.rule">
            <field name="name">Skill: system-scope read-only for all internal users</field>
            <field name="model_id" ref="model_woow_paas_platform_skill"/>
            <field name="domain_force">[('scope', '=', 'system')]</field>
            <field name="groups" eval="[(4, ref('base.group_user'))]"/>
            <field name="perm_read" eval="True"/>
            <field name="perm_write" eval="False"/>
            <field name="perm_create" eval="False"/>
            <field name="perm_unlink" eval="False"/>
        </record>
```

> Note: `skill_rule_admin_full` (group_system, `[(1,'=',1)]`) stays exactly as-is — it is the symmetric admin bypass required when adding base.group_user rules. The bundle `ir.attachment` rules (`ir_attachment_rule_skill_bundle_*`) also stay unchanged: system bundles remain pods-only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillDualScopeAcl --stop-after-init`
Expected: PASS. Then re-run the legacy ACL suite to confirm SK-1..8 still green (test skills default scope='user' so user-scope rules match identically):
`--test-tags /woow_paas_platform:TestPhaseAAclSkill`

- [ ] **Step 5: Commit**

```bash
git add src/security/ir_rules.xml src/tests/test_skill_dual_scope.py
git commit -m "feat: dual-scope ir.rules for skill (system_readonly + user leg)"
```

---

## Task 5: Template subset — `project.skill_ids` O2M + `_check_skill_subset`

**Files:**
- Modify: `src/models/project_project.py` (add `skill_ids` O2M)
- Modify: `src/models/agent_template.py` (add `_check_skill_subset`)
- Test: `src/tests/test_skill_dual_scope.py`

- [ ] **Step 1: Write the failing tests**

Append a new class to `src/tests/test_skill_dual_scope.py`:

```python
@tagged('post_install', '-at_install')
class TestSkillSubsetConstraint(TransactionCase):
    """Template bound skills ⊆ (project user skills ∪ system skills)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']
        cls.Template = cls.env['woow_paas_platform.agent_template']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.p1 = self.Project.sudo().create({'name': f'p1-{self.suffix}'})
        self.p2 = self.Project.sudo().create({'name': f'p2-{self.suffix}'})
        self.p1_skill = self.Skill.sudo().create({
            'name': f'p1s-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'a' * 64, 'scope': 'user', 'project_id': self.p1.id,
        })
        self.p2_skill = self.Skill.sudo().create({
            'name': f'p2s-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'b' * 64, 'scope': 'user', 'project_id': self.p2.id,
        })
        self.sys_skill = self.Skill.sudo().create({
            'name': f'sys-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'c' * 64, 'scope': 'system',
        })

    def test_project_skill_ids_reverse(self):
        self.assertIn(self.p1_skill, self.p1.skill_ids)
        self.assertNotIn(self.p2_skill, self.p1.skill_ids)

    def test_bind_cross_project_user_skill_rejected(self):
        with self.assertRaises(ValidationError):
            self.Template.sudo().create({
                'name': f't-{self.suffix}',
                'project_ids': [(6, 0, [self.p1.id])],
                'skill_ids': [(6, 0, [self.p2_skill.id])],  # belongs to p2, not bound
            })

    def test_bind_system_skill_allowed(self):
        tmpl = self.Template.sudo().create({
            'name': f't-{self.suffix}',
            'project_ids': [(6, 0, [self.p1.id])],
            'skill_ids': [(6, 0, [self.p1_skill.id, self.sys_skill.id])],
        })
        self.assertTrue(tmpl.id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillSubsetConstraint --stop-after-init`
Expected: FAIL — `project.skill_ids` does not exist; no subset constraint so cross-project binding is allowed.

- [ ] **Step 3: Add `skill_ids` reverse O2M to project**

In `src/models/project_project.py`, add right after the `mcp_server_ids` field (currently `project_project.py:50-54`):

```python
    skill_ids = fields.One2many(
        'woow_paas_platform.skill',
        'project_id',
        string='Skills',
    )
```

- [ ] **Step 4: Add `_check_skill_subset` to agent_template**

In `src/models/agent_template.py`, add right after `_check_mcp_subset` (currently ends at `agent_template.py:145`):

```python
    @api.constrains('skill_ids', 'project_ids')
    def _check_skill_subset(self):
        """Bound skills must be a subset of (project user-scope skills ∪
        scope='system'). Prevents a template from referencing user-scope skill
        rows from projects it isn't bound to.
        """
        Skill = self.env['woow_paas_platform.skill']
        for t in self:
            if not t.skill_ids:
                continue
            allowed_user_scope = set(t.project_ids.mapped('skill_ids').ids)
            allowed_system = set(
                Skill.sudo().search([('scope', '=', 'system')]).ids
            )
            allowed = allowed_user_scope | allowed_system
            for skill in t.skill_ids:
                if skill.id not in allowed:
                    raise ValidationError(
                        "Skill '%s' is not bound to any of template "
                        "'%s' projects and is not system-scope."
                        % (skill.name, t.name or t.id)
                    )
```

Confirm `ValidationError` is imported in `agent_template.py` (it is — `_check_mcp_subset` uses it). If not, add `from odoo.exceptions import ValidationError`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillSubsetConstraint --stop-after-init`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add src/models/project_project.py src/models/agent_template.py src/tests/test_skill_dual_scope.py
git commit -m "feat: skill subset constraint (bound ⊆ project ∪ system)"
```

---

## Task 6: Pod loading — snapshot union + download dual-leg

**Files:**
- Modify: `src/controllers/ai_assistant.py` (`_build_skill_snapshot`, currently `:785-804`)
- Modify: `src/controllers/agent_runtime_skill.py` (`:113-117` search)
- Test: `src/tests/test_skill_dual_scope.py`

- [ ] **Step 1: Write the failing tests**

Append a new class to `src/tests/test_skill_dual_scope.py`:

```python
@tagged('post_install', '-at_install')
class TestSkillPodLoading(TransactionCase):
    """system skills union into snapshot; download dual-leg."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Project = cls.env['project.project']
        cls.Template = cls.env['woow_paas_platform.agent_template']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.project = self.Project.sudo().create({'name': f'p-{self.suffix}'})
        self.proj_skill = self.Skill.sudo().create({
            'name': f'proj-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'a' * 64, 'scope': 'user',
            'project_id': self.project.id, 'state': 'published',
        })
        self.sys_skill = self.Skill.sudo().create({
            'name': f'sys-{self.suffix}', 'version': '0.1.0',
            'bundle_sha256': 'b' * 64, 'scope': 'system', 'state': 'published',
        })
        self.template = self.Template.sudo().create({
            'name': f't-{self.suffix}',
            'project_ids': [(6, 0, [self.project.id])],
            'skill_ids': [(6, 0, [self.proj_skill.id])],
        })

    def test_snapshot_unions_system_skill(self):
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        snap = AiAssistantController()._build_skill_snapshot(
            self.project, self.template)
        names = {e['name'] for e in snap}
        self.assertIn(self.proj_skill.name, names, "bound project skill present")
        self.assertIn(self.sys_skill.name, names, "system skill auto-unioned")

    def test_snapshot_project_name_shadows_system(self):
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        # a system skill sharing the bound project skill's NAME must be shadowed
        self.Skill.sudo().create({
            'name': self.proj_skill.name, 'version': '9.9.9',
            'bundle_sha256': 'c' * 64, 'scope': 'system', 'state': 'published',
        })
        snap = AiAssistantController()._build_skill_snapshot(
            self.project, self.template)
        same_name = [e for e in snap if e['name'] == self.proj_skill.name]
        self.assertEqual(len(same_name), 1, "project skill shadows system on name")
        self.assertEqual(same_name[0]['sha256'], 'a' * 64, "project version wins")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillPodLoading --stop-after-init`
Expected: FAIL — current `_build_skill_snapshot` only serialises `template.skill_ids`, so the system skill is absent.

- [ ] **Step 3: Modify `_build_skill_snapshot`**

In `src/controllers/ai_assistant.py`, replace the body of `_build_skill_snapshot` (the `return [...]` at `:797-804`) with:

```python
        Skill = self.env['woow_paas_platform.skill'].sudo()
        bound = template.skill_ids.filtered(lambda s: s.state == 'published')
        bound_names = set(bound.mapped('name'))
        # system skills auto-load into every pod; a bound project skill with the
        # same NAME shadows the system one (avoids pod _TOOL_CACHE name clash).
        system = Skill.search([
            ('scope', '=', 'system'),
            ('state', '=', 'published'),
            ('name', 'not in', list(bound_names)),
        ])
        return [
            {'name': skill.name, 'version': skill.version,
             'sha256': skill.bundle_sha256,
             'description': skill.description or ''}
            for skill in (bound + system)
        ]
```

(Leave the docstring `:786-796` unchanged.)

- [ ] **Step 4: Modify the download endpoint search to dual-leg**

In `src/controllers/agent_runtime_skill.py`, replace the search block (`:113-117`):

```python
        candidates = request.env['woow_paas_platform.skill'].sudo().search([
            '&', '&',
            ('name', '=', kwargs.get('skill_name')),
            ('version', '=', kwargs.get('version')),
            '|', ('project_id', '=', project_id), ('scope', '=', 'system'),
        ])
        # HC-2: the token's project_id is authoritative for the bound leg.
        # On a (name, version) collision the project-owned row wins over system.
        skill = candidates.filtered(lambda s: s.project_id.id == project_id)[:1] \
            or candidates.filtered(lambda s: s.scope == 'system')[:1]
```

Also update the docstring at the top of the file (`agent_runtime_skill.py:14-16`) — append after the existing HC-2 sentence:

```python
# Dual-scope (2026-05-30): a scope='system' skill (project_id NULL) is
# fetchable by ANY valid skill_bundle token — system skills are platform-
# curated and intentionally global. The project-bound leg stays strict
# (token project_id authoritative); project rows shadow system on collision.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestSkillPodLoading --stop-after-init`
Expected: PASS. Then run the download e2e suite:
`--test-tags /woow_paas_platform:TestSkillE2eLifecycleDownload` and `--test-tags /woow_paas_platform:TestAgentPodPhaseCSkillBundle`

- [ ] **Step 6: Commit**

```bash
git add src/controllers/ai_assistant.py src/controllers/agent_runtime_skill.py src/tests/test_skill_dual_scope.py
git commit -m "feat: pod loads system skills (snapshot union + download dual-leg)"
```

---

## Task 7: Migration 18.0.1.0.29 + manifest version bump

**Files:**
- Create: `src/migrations/18.0.1.0.29/pre-migrate.py`
- Create: `src/migrations/18.0.1.0.29/post-migrate.py`
- Modify: `src/__manifest__.py` (version)

> Migrations run only on `-u` against an EXISTING DB. Fresh installs get `scope` from the field default and the partial indexes from `init()` (Task 2), and the new rule xmlids load directly. This task makes the UPGRADE path correct.

- [ ] **Step 1: Bump manifest version**

In `src/__manifest__.py:3`:

```python
    "version": "18.0.1.0.29",
```

- [ ] **Step 2: Create pre-migrate (add column + backfill BEFORE ORM NOT NULL registration)**

Create `src/migrations/18.0.1.0.29/pre-migrate.py`:

```python
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Add skill.scope and backfill existing rows to 'user' BEFORE the ORM
    registers the required (NOT NULL) field — otherwise the upgrade aborts on
    pre-existing rows with NULL scope. Every live skill today is project-scoped,
    so 'user' is the correct backfill."""
    cr.execute("""
        ALTER TABLE woow_paas_platform_skill
        ADD COLUMN IF NOT EXISTS scope VARCHAR
    """)
    cr.execute("""
        UPDATE woow_paas_platform_skill
        SET scope = 'user'
        WHERE scope IS NULL
    """)
    _logger.info("skill.scope added + backfilled 'user' (%s rows)", cr.rowcount)
```

- [ ] **Step 3: Create post-migrate (drop old constraint, partial indexes, retire old rules)**

Create `src/migrations/18.0.1.0.29/post-migrate.py`:

```python
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Finalise skill dual-scope: drop the NULL-unsafe UNIQUE constraint,
    create the two partial unique indexes, and retire the legacy
    skill_rule_project_* ir.rules (two-DELETE — same pattern as 18.0.1.0.24)
    so they don't OR-combine with the new scope-split rules."""

    # 1. Drop the old UNIQUE(project_id, name, version) constraint.
    cr.execute("""
        ALTER TABLE woow_paas_platform_skill
        DROP CONSTRAINT IF EXISTS
            woow_paas_platform_skill_unique_name_version_per_project
    """)

    # 2. Create the NULL-aware partial unique indexes (idempotent; also created
    #    by skill.init() on every -u, but explicit here for upgrade clarity).
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS skill_uniq_user_proj_name_version
        ON woow_paas_platform_skill (project_id, name, version)
        WHERE project_id IS NOT NULL
    """)
    cr.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS skill_uniq_system_name_version
        ON woow_paas_platform_skill (name, version)
        WHERE project_id IS NULL
    """)

    # 3. Retire legacy skill rules. Their domains changed (added scope='user'
    #    leg) but they live in a noupdate block, so the XML can't overwrite them
    #    on -u. We replaced them with NEW xmlids (skill_rule_user_*) which load
    #    fresh; the old ones must be deleted or they OR-combine and re-open the
    #    create-direction leak. Two DELETEs (ir_model_data does not cascade).
    cr.execute("""
        DELETE FROM ir_rule
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'woow_paas_platform'
              AND model = 'ir.rule'
              AND name IN (
                'skill_rule_project_admin',
                'skill_rule_project_user',
                'skill_rule_project_guest'
              )
        )
    """)
    retired = cr.rowcount
    cr.execute("""
        DELETE FROM ir_model_data
        WHERE module = 'woow_paas_platform'
          AND model = 'ir.rule'
          AND name IN (
            'skill_rule_project_admin',
            'skill_rule_project_user',
            'skill_rule_project_guest'
          )
    """)
    _logger.info(
        "Retired legacy skill rules: %s ir_rule + %s ir_model_data rows",
        retired, cr.rowcount,
    )
```

- [ ] **Step 4: Verify the upgrade path on a sandbox (manual)**

> Migrations can't be unit-tested in-process. Verify against a DB that already has the old schema.

Run (Docker dev DB that was installed at ≤ 18.0.1.0.28):
```bash
./scripts/test-addon.sh   # or: ./odoo-bin -c odoo.conf -u woow_paas_platform --stop-after-init
```
Expected log lines:
- `skill.scope added + backfilled 'user' (N rows)`
- `Retired legacy skill rules: 3 ir_rule + 3 ir_model_data rows`

Then verify no orphaned constraint / rules:
```bash
# psql into the dev DB
psql -d <db> -c "SELECT conname FROM pg_constraint WHERE conname LIKE 'woow_paas_platform_skill_unique%';"   # expect 0 rows
psql -d <db> -c "SELECT indexname FROM pg_indexes WHERE tablename='woow_paas_platform_skill' AND indexname LIKE 'skill_uniq_%';"  # expect 2 rows
psql -d <db> -c "SELECT name FROM ir_model_data WHERE model='ir.rule' AND name LIKE 'skill_rule_%';"  # expect skill_rule_user_admin/member/guest, skill_rule_system_readonly, skill_rule_admin_full — NOT skill_rule_project_*
```

- [ ] **Step 5: Commit**

```bash
git add src/migrations/18.0.1.0.29/ src/__manifest__.py
git commit -m "feat: migration 18.0.1.0.29 for skill dual-scope schema + rule retirement"
```

---

## Task 8: Demo system skill seed

**Files:**
- Create: `src/data/demo_skills/hello-world-0.1.0.tar.gz` (source artifact, kept for reference)
- Create: `src/data/demo_system_skill.xml`
- Modify: `src/__manifest__.py` (register the data file)
- Test: `src/tests/test_skill_dual_scope.py`

> TRADE-OFF flagged for review: registered in `data` (not a `demo` key) with `noupdate="1"` so it reliably appears on stg even when demo data isn't enabled — mirroring the existing `data/agent_template_demo.xml` convention. Downside: it also seeds prod (a deletable sample). To keep prod clean instead, move this entry to a new `"demo": [...]` manifest key.

- [ ] **Step 1: Build the demo bundle and capture base64 + sha256**

Run:
```bash
cd /tmp && rm -rf hello-world && mkdir -p hello-world/src
cat > hello-world/SKILL.md <<'EOF'
---
name: hello-world
version: 0.1.0
description: A demo system skill that greets the user.
author: woow-platform
---
# Hello World

A minimal demo skill available to every project.
EOF
cat > hello-world/src/main.py <<'EOF'
def run(user_input: str = "") -> str:
    return f"Hello, {user_input or 'world'}!"
EOF
tar -czf hello-world-0.1.0.tar.gz hello-world
python3 -c "import hashlib,base64; b=open('/tmp/hello-world-0.1.0.tar.gz','rb').read(); print('SHA256=' + hashlib.sha256(b).hexdigest()); print('B64=' + base64.b64encode(b).decode())"
```
Copy the artifact into the repo and record the two printed values:
```bash
mkdir -p src/data/demo_skills && cp /tmp/hello-world-0.1.0.tar.gz src/data/demo_skills/
```

- [ ] **Step 2: Create the demo data XML**

Create `src/data/demo_system_skill.xml`, substituting `<<SHA256>>` and `<<B64>>` with the values printed in Step 1:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<odoo>
    <data noupdate="1">
        <!-- Demo/starter system skill: available to every project's pod.
             noupdate so admins may delete it; bundle bytes inlined as base64,
             sha256 must match the raw tar.gz bytes the pod fetches. -->
        <record id="demo_attachment_hello_world" model="ir.attachment">
            <field name="name">hello-world-0.1.0.tar.gz</field>
            <field name="mimetype">application/gzip</field>
            <field name="datas"><<B64>></field>
        </record>

        <record id="demo_skill_hello_world" model="woow_paas_platform.skill">
            <field name="name">hello-world</field>
            <field name="title">Hello World</field>
            <field name="description">A demo system skill that greets the user.</field>
            <field name="version">0.1.0</field>
            <field name="scope">system</field>
            <field name="state">published</field>
            <field name="bundle_sha256"><<SHA256>></field>
            <field name="bundle_attachment_id" ref="demo_attachment_hello_world"/>
            <field name="skill_metadata">{"name": "hello-world", "version": "0.1.0", "description": "A demo system skill that greets the user.", "author": "woow-platform"}</field>
        </record>
    </data>
</odoo>
```

> The `skill.create()` hook auto-stamps the attachment (`res_model`/`res_id`/`public=False`) and computes `is_latest` — no extra wiring needed.

- [ ] **Step 3: Register in the manifest**

In `src/__manifest__.py`, add to the `data` list right after `"data/agent_template_demo.xml",` (`:50`):

```python
        "data/demo_system_skill.xml",
```

- [ ] **Step 4: Write the verification test**

Append to `src/tests/test_skill_dual_scope.py`:

```python
@tagged('post_install', '-at_install')
class TestDemoSystemSkill(TransactionCase):
    """The seeded hello-world system skill is present + correctly shaped."""

    def test_demo_system_skill_seeded(self):
        sk = self.env.ref(
            'woow_paas_platform.demo_skill_hello_world', raise_if_not_found=False)
        self.assertTrue(sk, "demo hello-world skill must be seeded")
        self.assertEqual(sk.scope, 'system')
        self.assertFalse(sk.project_id)
        self.assertEqual(sk.state, 'published')
        self.assertTrue(sk.is_latest)
        self.assertTrue(sk.bundle_attachment_id, "bundle attachment linked")
        self.assertEqual(
            sk.bundle_attachment_id.res_model, 'woow_paas_platform.skill',
            "attachment anchored by skill.create() hook")
```

- [ ] **Step 5: Run install + test**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestDemoSystemSkill --stop-after-init`
Expected: PASS. Then open the Skills backend list on the dev env (`http://localhost`) and confirm `hello-world` appears with State=Published, Scope=System (no project).

- [ ] **Step 6: Commit**

```bash
git add src/data/demo_system_skill.xml src/data/demo_skills/hello-world-0.1.0.tar.gz src/__manifest__.py src/tests/test_skill_dual_scope.py
git commit -m "feat: seed hello-world demo system skill"
```

---

## Task 9: Update coupled existing tests

**Files:**
- Modify: `src/tests/test_agent_pod_phase_c_sse_gateway.py` (SSE.6)
- Modify: `src/tests/test_agent_pod_phase_a_acl_new_models.py` (SK-6 message)

- [ ] **Step 1: Fix SSE.6 — count → membership (system skills may co-exist)**

In `src/tests/test_agent_pod_phase_c_sse_gateway.py`, in `test_SSE6_skill_snapshot_carries_description`, replace the assertion block (currently from `self.assertEqual(len(snapshot), 1, ...)` through the final `entry[...]` assertions) with:

```python
        names = [e['name'] for e in snapshot]
        self.assertIn('sk-pub', names,
                      "published project skill must be in the snapshot")
        self.assertNotIn('sk-draft', names,
                         "SSE.6: draft skill must NOT be in the snapshot")
        entry = next(e for e in snapshot if e['name'] == 'sk-pub')
        self.assertEqual(entry['version'], '0.1.0')
        self.assertEqual(entry['sha256'], 'a' * 64)
        self.assertEqual(
            entry['description'], 'Fetches the weather for a city.',
            "SSE.6: snapshot must carry the skill description (Gap 3 hybrid)")
```

> Rationale: after Task 6 `_build_skill_snapshot` unions published system skills (incl. the Task 8 demo seed), so the exact-count assertion is wrong; membership is the correct invariant.

- [ ] **Step 2: Fix SK-6 misleading message (references a non-existent group)**

In `src/tests/test_agent_pod_phase_a_acl_new_models.py`, in `test_SK6_guest_cannot_read_bundle_attachment`, update the `assertRaises` message (currently `:614-616`) to describe the actual field mask:

```python
        with self.assertRaises(
            AccessError,
            msg="B1.SK-6 field: guest must NOT read bundle_attachment_id "
                "(field-level groups='base.group_system' mask).",
        ):
```

- [ ] **Step 3: Run both suites to verify they pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestPhaseCSseGateway --stop-after-init`
Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform:TestPhaseAAclSkill --stop-after-init`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/tests/test_agent_pod_phase_c_sse_gateway.py src/tests/test_agent_pod_phase_a_acl_new_models.py
git commit -m "test: update SSE.6 + SK-6 for skill dual-scope"
```

---

## Task 10: Full suite + docs

**Files:**
- Modify: `docs/guides/paas-skill-system/05-skill-runtime-lifecycle.md` (or 03-skill-design.md) — system skill loading
- Modify: `docs/guides/paas-skill-system/10-roadmap-phases.md` — dual-scope note

- [ ] **Step 1: Run the entire module test suite**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags /woow_paas_platform --stop-after-init`
Expected: PASS (all skill tests + the previously-coupled tests green). If anything red, fix before continuing — do not proceed with failing tests.

- [ ] **Step 2: Document system skill loading + cross-repo note**

In `docs/guides/paas-skill-system/03-skill-design.md`, add a short section after "🔄 發布生命週期" explaining scope:

```markdown
## 🌐 Scope：system vs user

| scope | project_id | 載入範圍 | 上傳者 |
|---|---|---|---|
| `system` | NULL | **所有 project 的 pod**（platform-curated） | admin（Phase 2） |
| `user` | 必填 | 僅該 project 的 pod | project owner/admin（Phase 2） |

system skill 由 Odoo 自動 union 進每個 project 的 `_build_skill_snapshot`，並透過下載端點的
`scope='system'` fallback leg 供應（pod 端 frozen contract 不變，wire format 相同）。

> **跨 repo 註記（給 `paas-agent-runtime`）**：skills.json snapshot 現在可能含 bundle 解析為
> system-scope（`project_id` NULL）的 entry。`{name, version, sha256}` 契約與 by-name fetch
> 完全不變，pod 無需改動。
```

- [ ] **Step 3: Update roadmap doc**

In `docs/guides/paas-skill-system/10-roadmap-phases.md`, append to the "目前狀態" section a bullet:

```markdown
- **Skill dual-scope（2026-05-30）**：skill 跟進 mcp_server 加上 `scope`（system/user）。system skill 平台級共用、所有 project 載入；上傳 pipeline 為 Phase 2。設計見 `docs/plans/2026-Q2/2026-05-30-skill-dual-scope-design.md`。
```

- [ ] **Step 4: Commit**

```bash
git add docs/guides/paas-skill-system/
git commit -m "docs: document skill dual-scope loading + cross-repo note"
```

---

## Self-Review Checklist (completed during plan authoring)

- **Spec coverage:** ① model → T1/T2/T3; ② ACL → T4; ③ pod loading → T6; ④ template subset → T5; ⑤ migration → T7; ⑥ tests → T1-T9; ⑦ demo seed → T8. All spec sections mapped.
- **Type/name consistency:** new rule xmlids `skill_rule_user_admin/member/guest` + `skill_rule_system_readonly` used identically in T4 (XML) and T7 (migration retire targets `skill_rule_project_*`). Method names `_is_latest_group_domain`/`_recompute_is_latest()` (no-arg) consistent across T3 call sites. `_build_skill_snapshot` signature unchanged. `project.skill_ids` (T5) consumed by `_check_skill_subset` (T5) and not elsewhere.
- **Placeholder scan:** demo bundle base64/sha256 are generated in T8 Step 1 and substituted in Step 2 (`<<B64>>`/`<<SHA256>>` are explicit substitution markers, not left-in placeholders).

## Out of scope (Phase 2 — separate plan)

Upload pipeline (`_parse_bundle`/`_create_from_bundle` with tar.gz safety), backend admin upload wizard (system skills), `/woow` frontend section (user skills). See design doc §Phase 2.
