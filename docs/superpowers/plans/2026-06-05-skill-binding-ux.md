# Project Skill Binding UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 後台 project form 一頁完成 skill「歸屬 + 生效」兩層管理、skill form 開放 admin 編輯 scope/project、/woow 前台 project 設定頁以 feature flag 唯讀化。

**Architecture:** 不動 skill↔project m2o schema。生效層走既有 `agent_template.skill_ids` m2m（抽 `_effective_skill_ids` 共用 helper 防公式分叉）；共用 template 以單一 `copy(default=)` copy-on-write fork；前台唯讀由 `ir.config_parameter` flag 同時 gate 三支寫入 endpoint 與 OWL 渲染（two-layer lock-step）。

**Tech Stack:** Odoo 18（Python model/constraint/ir.rule、XML view、TransactionCase/HttpCase）、OWL 前端。

**Spec:** `docs/superpowers/specs/2026-06-05-skill-binding-ux-design.md`（含安全 invariants §6 — 實作全程必守）。

---

## 前置（已完成，執行者驗證即可）

- [ ] 確認 pattern doc 在本 branch：`ls docs/decisions/patterns/resource-ownership-vs-template-activation.md` → 存在（floor 已 rebase 到 develop `c8c42f0`）。
- [ ] 確認測試環境：`./.claude/skills/k8s-dev-sandbox/scripts/list.sh` 有可用 sandbox，或先 `create.sh --context docker-desktop`。注意 Floor `.env` 的路徑一律絕對路徑（floor-env-relative-path pitfall）。

**全模組測試指令**（以下各 Task 的「跑測試」步驟皆指此，可加 `--test-tags` 過濾）：

```bash
./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> --module woow_paas_platform
```

單類別過濾（直接在 sandbox odoo 容器內跑時）：`--test-tags /woow_paas_platform:TestClassName`

---

## 檔案結構與任務相依

| Task | 檔案 | 依賴 | 可平行 wave |
|---|---|---|---|
| 1 | `src/models/agent_session.py` + `src/tests/test_skill_binding_admin.py`(new) | — | W1 |
| 2 | `src/models/skill.py`（regroup write） | — | W1 |
| 3 | `src/models/skill.py`（is_effective） | 1, 2 | W2 |
| 4 | `src/wizard/skill_upload_wizard.py` + `src/views/skill_views.xml`（wizard view + form view） | — | W1 |
| 5 | `src/models/project_project.py`（computed/fork/wizard action） | — | W1 |
| 6 | `src/models/agent_template.py`（write push hook） | — | W1 |
| 7 | `src/views/project_views.xml` + `src/tests/test_project_form_mcp_skill_tab.py` | 3, 5 | W2 |
| 8 | flag 基建：`src/models/res_config_settings.py` + `src/views/res_config_settings_views.xml` + `src/hooks.py` | — | W1 |
| 9 | `src/controllers/paas.py` gates + `src/tests/test_portal_settings_flag.py`(new) + 既有兩 fixture setUp | — | W1 |
| 10 | `src/controllers/ai_assistant.py` gate | 9（import helper） | W2 |
| 11 | 前端 `ProjectDetailPage.{js,xml}` + `BasicInfoSection.{js,xml}` | — | W1 |
| 12 | docs 更新（spec §5 七檔） | 1–11 | W3 |
| 13 | 全量驗證 + 前端 sandbox 手動驗證 | 全部 | W3 |

> 平行注意：同檔案的 Task 不可平行（2/3 同 `skill.py`；4 內兩個 view record 同檔）。平行執行用 worktree isolation。

---

### Task 1: `_effective_skill_ids` helper + snapshot 重構

**Files:**
- Modify: `src/models/agent_session.py:298-315`
- Create: `src/tests/test_skill_binding_admin.py`

- [ ] **Step 1: 寫 characterization test（鎖 snapshot 現行為）+ helper 紅測試**

建立 `src/tests/test_skill_binding_admin.py`：

```python
from odoo.tests import TransactionCase, tagged


def _mk_skill(env, **vals):
    base = {
        'name': 'sk', 'version': '1.0.0',
        'bundle_sha256': 'a' * 64, 'scope': 'system',
    }
    base.update(vals)
    return env['woow_paas_platform.skill'].sudo().create(base)


@tagged('post_install', '-at_install')
class TestEffectiveSkillHelper(TransactionCase):
    """§2.1 — _effective_skill_ids 單一公式來源 + snapshot 重構不變式。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Session = cls.env['woow_paas_platform.agent_session']
        cls.project = cls.env['project.project'].sudo().create(
            {'name': 'FX Project'})
        # system published / system draft
        cls.sys_pub = _mk_skill(cls.env, name='sys-pub', state='published')
        cls.sys_draft = _mk_skill(cls.env, name='sys-draft')
        # user-scope：一顆會被 template 綁、一顆不綁
        cls.user_bound = _mk_skill(
            cls.env, name='user-bound', scope='user',
            project_id=cls.project.id, state='published')
        cls.user_unbound = _mk_skill(
            cls.env, name='user-unbound', scope='user',
            project_id=cls.project.id, state='published')
        # 同名遮蔽：bound 的 'sys-pub' user 版要遮蔽 system 版
        cls.shadow = _mk_skill(
            cls.env, name='sys-pub', version='2.0.0', scope='user',
            project_id=cls.project.id, state='published')
        cls.template = cls.env['woow_paas_platform.agent_template'].sudo() \
            .create({
                'name': 'FX Template',
                'project_ids': [(6, 0, [cls.project.id])],
                'skill_ids': [(6, 0, [cls.user_bound.id, cls.shadow.id])],
            })

    def test_snapshot_characterization(self):
        """重構前後 _build_skill_snapshot 輸出不變（spec §4.5 不變式）。"""
        snap = self.Session._build_skill_snapshot(self.project, self.template)
        names = [(e['name'], e['version']) for e in snap]
        # bound 在前（user-bound, sys-pub@2.0.0），system 補後且同名遮蔽
        self.assertIn(('user-bound', '1.0.0'), names)
        self.assertIn(('sys-pub', '2.0.0'), names)       # bound shadow 版
        self.assertNotIn(('sys-pub', '1.0.0'), names)    # system 版被遮蔽
        self.assertNotIn(('user-unbound', '1.0.0'), names)  # 歸屬≠生效
        self.assertNotIn(('sys-draft', '1.0.0'), names)  # draft 不進 pod
        for entry in snap:
            self.assertEqual(
                set(entry), {'name', 'version', 'sha256', 'description'})

    def test_effective_skill_ids_helper(self):
        """helper 回 recordset，與 snapshot 同一公式。"""
        eff = self.Session._effective_skill_ids(self.project)
        self.assertIn(self.user_bound, eff)
        self.assertIn(self.shadow, eff)
        self.assertNotIn(self.user_unbound, eff)
        self.assertNotIn(self.sys_draft, eff)
        # system 同名被 bound 遮蔽
        self.assertNotIn(self.sys_pub, eff)
```

- [ ] **Step 2: 跑測試確認 helper 紅、characterization 綠**

Run: 測試指令（`--test-tags /woow_paas_platform:TestEffectiveSkillHelper`）
Expected: `test_snapshot_characterization` PASS；`test_effective_skill_ids_helper` FAIL（`AttributeError: _effective_skill_ids`）

- [ ] **Step 3: 實作 helper 並重構 snapshot**

`src/models/agent_session.py` — 將 `_build_skill_snapshot`（:298-315）改為：

```python
    @api.model
    def _effective_skill_ids(self, project, template=None):
        """Effective skills for a project's pod, as a recordset:
        (resolved-template bound, state='published') ∪ (scope='system',
        state='published', same-name shadowed by bound). Single source of
        truth — `_build_skill_snapshot` serialises THIS recordset and the
        skill.is_effective compute does membership against it; a second
        copy of the formula is the ownership-vs-activation anti-pattern #3
        drift (see docs/decisions/patterns/
        resource-ownership-vs-template-activation.md)."""
        Skill = self.env['woow_paas_platform.skill'].sudo()
        if template is None:
            template = self._resolve_agent_template(project)
        bound = (template.skill_ids if template else Skill.browse()) \
            .filtered(lambda s: s.state == 'published')
        system = Skill.search([
            ('scope', '=', 'system'),
            ('state', '=', 'published'),
            ('name', 'not in', bound.mapped('name')),
        ])
        return bound + system

    @api.model
    def _build_skill_snapshot(self, project, template):
        """Serialise the effective skill set for the pod (name/version/
        sha256/description; system shadowed by same-name bound)."""
        return [
            {'name': skill.name, 'version': skill.version,
             'sha256': skill.bundle_sha256,
             'description': skill.description or ''}
            for skill in self._effective_skill_ids(project, template=template)
        ]
```

- [ ] **Step 4: 跑測試確認兩個都綠**（characterization 守住重構不變式）

- [ ] **Step 5: Commit**

```bash
git add src/models/agent_session.py src/tests/test_skill_binding_admin.py
git commit -m "refactor: extract _effective_skill_ids as single skill-activation formula"
```

---

### Task 2: skill.write() — scope/project 變更的 is_latest 雙組重算 + snapshot push

**Files:**
- Modify: `src/models/skill.py:150-187`（write）、`:294-308`（recompute）
- Test: `src/tests/test_skill_binding_admin.py`（追加）

- [ ] **Step 1: 追加紅測試**

`src/tests/test_skill_binding_admin.py` 末尾追加：

```python
from unittest.mock import patch


@tagged('post_install', '-at_install')
class TestSkillRepoint(TransactionCase):
    """§1 — admin 改 scope/project 時新舊兩組 is_latest 都重算 + push。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.proj_a = cls.env['project.project'].sudo().create({'name': 'RP A'})
        cls.proj_b = cls.env['project.project'].sudo().create({'name': 'RP B'})

    def _mk(self, **vals):
        return _mk_skill(self.env, **vals)

    def test_repoint_recomputes_old_and_new_group(self):
        # 舊組（proj_a, 'mv'）兩版：v2 是 is_latest
        v1 = self._mk(name='mv', version='1.0.0', scope='user',
                      project_id=self.proj_a.id, state='published')
        v2 = self._mk(name='mv', version='2.0.0', scope='user',
                      project_id=self.proj_a.id, state='published')
        v1._recompute_is_latest()
        self.assertTrue(v2.is_latest)
        # v2 搬去 proj_b → 舊組 winner 應回到 v1；新組 winner = v2
        v2.write({'project_id': self.proj_b.id})
        self.assertTrue(v1.is_latest, "old group must re-elect its winner")
        self.assertTrue(v2.is_latest, "new group winner is the mover")

    def test_repoint_pushes_snapshot_to_both_projects(self):
        sk = self._mk(name='mv2', scope='user',
                      project_id=self.proj_a.id, state='published')
        with patch.object(
            type(self.env['woow_paas_platform.agent_session']),
            '_push_snapshot_to_running_pods',
        ) as push:
            sk.write({'project_id': self.proj_b.id})
        pushed = {c.args[:3] for c in push.call_args_list}
        self.assertIn(('skill', 'user', self.proj_a.id), pushed)
        self.assertIn(('skill', 'user', self.proj_b.id), pushed)

    def test_scope_flip_by_admin_pushes_system_wide(self):
        sk = self._mk(name='mv3', scope='user',
                      project_id=self.proj_a.id, state='published')
        with patch.object(
            type(self.env['woow_paas_platform.agent_session']),
            '_push_snapshot_to_running_pods',
        ) as push:
            sk.write({'scope': 'system', 'project_id': False})
        self.assertIn(
            ('skill', 'system'), {c.args[:2] for c in push.call_args_list})
```

- [ ] **Step 2: 跑測試確認紅**（`test_repoint_recomputes_old_and_new_group` FAIL：v1.is_latest 仍 False）

- [ ] **Step 3: 實作**

`src/models/skill.py` — `_recompute_is_latest` 拆出可帶 explicit domain 的入口（`:294-308` 替換）：

```python
    def _recompute_is_latest(self):
        """Invariant: at most one is_latest=True per (scope, project, name)
        group — recompute the group this skill currently sits in."""
        self.ensure_one()
        self._recompute_is_latest_for(self._is_latest_group_domain())

    @api.model
    def _recompute_is_latest_for(self, group_domain):
        """Group-key-explicit recompute. Needed for scope/project re-point:
        after write the record is in the NEW group, so the OLD group's
        winner can only be re-elected from a domain captured pre-write."""
        group = self.sudo().search(group_domain)
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

`write()`（`:150-187`）— 在既有 guard 之後、`super().write` 之前 capture，state 分支之後加 regroup 分支：

```python
    def write(self, vals):
        if ('scope' in vals or 'project_id' in vals) and not self.env.su \
                and not self.env.user.has_group('base.group_system'):
            for skill in self:
                if 'scope' in vals and vals['scope'] != skill.scope:
                    raise AccessError(
                        "Only administrators can change a skill's scope."
                    )
                if 'project_id' in vals \
                        and vals.get('project_id') != skill.project_id.id:
                    raise AccessError(
                        "Only administrators can change a skill's project."
                    )
        # Re-point (admin path): capture pre-write group keys — after
        # super().write the record sits in the NEW group and the OLD group
        # can no longer be derived from self (spec §1 / Lens-2 finding).
        regroup = 'scope' in vals or 'project_id' in vals
        old_groups = []
        if regroup:
            old_groups = [
                (s._is_latest_group_domain(), s.scope, s.project_id.id)
                for s in self
            ]
        result = super().write(vals)
        if 'bundle_attachment_id' in vals:
            for skill in self:
                skill._anchor_bundle_attachment()
        if 'state' in vals:
            for skill in self:
                if skill.state == 'published' and not skill.published_at:
                    skill.published_at = fields.Datetime.now()
                skill._recompute_is_latest()
                self.env['woow_paas_platform.agent_session'] \
                    ._push_snapshot_to_running_pods(
                        'skill', skill.scope, skill.project_id.id or None)
        if regroup:
            Session = self.env['woow_paas_platform.agent_session']
            for skill, (old_domain, old_scope, old_pid) in zip(
                    self, old_groups):
                if skill._is_latest_group_domain() == old_domain:
                    continue  # value written but group unchanged
                self._recompute_is_latest_for(old_domain)
                skill._recompute_is_latest()
                # System flip (either direction) touches every pod; a
                # user→user move touches the old and the new project pods.
                # _recompute_is_latest_for writes only is_latest → never
                # re-enters this branch (no recursion).
                if 'system' in (old_scope, skill.scope):
                    Session._push_snapshot_to_running_pods('skill', 'system')
                else:
                    Session._push_snapshot_to_running_pods(
                        'skill', 'user', old_pid)
                    Session._push_snapshot_to_running_pods(
                        'skill', 'user', skill.project_id.id)
        return result
```

- [ ] **Step 4: 跑測試確認綠**（含既有 `TestSkillDualScopeAcl`、`test_skill_admin_lifecycle` 全綠 — 守 guard / lifecycle 不回歸）

- [ ] **Step 5: Commit**

```bash
git add src/models/skill.py src/tests/test_skill_binding_admin.py
git commit -m "feat: recompute both is_latest groups and push snapshots on skill re-point"
```

---

### Task 3: skill `is_effective` computed 欄位

**Files:**
- Modify: `src/models/skill.py`（欄位區，`is_latest` 之後）
- Test: `src/tests/test_skill_binding_admin.py`（追加）

- [ ] **Step 1: 追加紅測試**

```python
@tagged('post_install', '-at_install')
class TestSkillIsEffective(TransactionCase):
    """§2.1 — is_effective 與 _effective_skill_ids 同源（anti-pattern #3
    mitigation）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.env['project.project'].sudo().create({'name': 'FE'})
        cls.bound = _mk_skill(cls.env, name='fe-bound', scope='user',
                              project_id=cls.project.id, state='published')
        cls.unbound = _mk_skill(cls.env, name='fe-unbound', scope='user',
                                project_id=cls.project.id, state='published')
        cls.sys_pub = _mk_skill(cls.env, name='fe-sys', state='published')
        cls.sys_draft = _mk_skill(cls.env, name='fe-sys-draft')
        cls.env['woow_paas_platform.agent_template'].sudo().create({
            'name': 'FE Tpl',
            'project_ids': [(6, 0, [cls.project.id])],
            'skill_ids': [(6, 0, [cls.bound.id])],
            'state': 'published',
        })

    def test_is_effective_matrix(self):
        self.assertTrue(self.bound.is_effective)      # bound published
        self.assertFalse(self.unbound.is_effective)   # 歸屬≠生效
        self.assertTrue(self.sys_pub.is_effective)    # system published
        self.assertFalse(self.sys_draft.is_effective)  # draft
```

- [ ] **Step 2: 跑測試確認紅**（`is_effective` 欄位不存在 → Invalid field）

- [ ] **Step 3: 實作欄位**（`skill.py`，加在 `is_latest` 欄位後）

```python
    is_effective = fields.Boolean(
        string='Active in Pod',
        compute='_compute_is_effective',
        help="Loaded into the owning project's agent pod? user-scope: member "
             "of agent_session._effective_skill_ids(project) — ownership "
             "alone does NOT activate a skill (anti-pattern #3); system: "
             "published (auto-loaded platform-wide).",
    )
```

method 區（`_is_latest_group_domain` 前）：

```python
    def _compute_is_effective(self):
        # One resolve+search per project (not per row) — N+1 guard.
        Session = self.env['woow_paas_platform.agent_session']
        effective_by_project = {}
        for skill in self:
            if skill.scope == 'system':
                skill.is_effective = skill.state == 'published'
                continue
            project = skill.project_id
            if not project:
                skill.is_effective = False
                continue
            if project.id not in effective_by_project:
                effective_by_project[project.id] = set(
                    Session._effective_skill_ids(project).ids)
            skill.is_effective = skill.id in effective_by_project[project.id]
```

- [ ] **Step 4: 跑測試確認綠**

- [ ] **Step 5: Commit**

```bash
git add src/models/skill.py src/tests/test_skill_binding_admin.py
git commit -m "feat: add skill.is_effective compute backed by _effective_skill_ids"
```

---

### Task 4: wizard 支援 user-scope 上傳 + skill form 開放 scope/project（admin）

**Files:**
- Modify: `src/wizard/skill_upload_wizard.py`
- Modify: `src/views/skill_views.xml`（wizard form `:150-184`、skill form `:63-68`）
- Modify: `src/models/skill.py:259-275`（`action_open_upload_wizard` 轉發 context）
- Test: `src/tests/test_skill_binding_admin.py`（追加）+ Modify: `src/tests/test_skill_admin_views.py`

- [ ] **Step 1: 追加紅測試（wizard）**

```python
from odoo.exceptions import UserError


@tagged('post_install', '-at_install')
class TestWizardUserScope(TransactionCase):
    """§2.2 — wizard per-scope 上傳與查重。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['woow_paas_platform.skill_upload_wizard']
        cls.proj = cls.env['project.project'].sudo().create({'name': 'WZ'})
        cls.proj2 = cls.env['project.project'].sudo().create({'name': 'WZ2'})
        cls.bundle = 'aGVsbG8='  # b64("hello")

    def _wiz(self, **vals):
        base = {'name': 'wz-skill', 'version': '1.0.0',
                'bundle_file': self.bundle}
        base.update(vals)
        return self.Wizard.create(base)

    def test_user_scope_upload_binds_project(self):
        action = self._wiz(scope='user', project_id=self.proj.id) \
            .action_upload()
        skill = self.env['woow_paas_platform.skill'].browse(action['res_id'])
        self.assertEqual(skill.scope, 'user')
        self.assertEqual(skill.project_id, self.proj)
        self.assertEqual(skill.state, 'draft')

    def test_user_scope_requires_project(self):
        with self.assertRaises(UserError):
            self._wiz(scope='user').action_upload()

    def test_system_scope_rejects_project(self):
        with self.assertRaises(UserError):
            self._wiz(scope='system', project_id=self.proj.id).action_upload()

    def test_dedupe_is_per_scope_and_project(self):
        self._wiz(scope='user', project_id=self.proj.id).action_upload()
        # 同 project 同名版 → 擋
        with self.assertRaises(UserError):
            self._wiz(scope='user', project_id=self.proj.id).action_upload()
        # 不同 project 同名版 → 放行
        self._wiz(scope='user', project_id=self.proj2.id).action_upload()
        # system 同名版（user 已存在）→ 放行（不同 scope 桶）
        self._wiz(scope='system').action_upload()

    def test_default_scope_stays_system_without_context(self):
        self.assertEqual(self._wiz().scope, 'system')
```

- [ ] **Step 2: 跑測試確認紅**（wizard 無 `scope` 欄位）

- [ ] **Step 3: 實作 wizard**

`src/wizard/skill_upload_wizard.py` 全檔改為：

```python
import base64
import hashlib

from odoo import fields, models
from odoo.exceptions import UserError


class SkillUploadWizard(models.TransientModel):
    _name = 'woow_paas_platform.skill_upload_wizard'
    _description = 'Upload Skill Bundle'

    MAX_BUNDLE_SIZE = 10 * 1024 * 1024  # 10 MB

    name = fields.Char(
        string='Skill Name',
        required=True,
        help='Slug; must match the name in the bundle SKILL.md.',
    )
    version = fields.Char(string='Version', required=True, default='0.1.0')
    title = fields.Char(string='Display Title')
    description = fields.Text(string='Description')
    # default 'system' keeps the Configuration-menu entry behaviour; the
    # project-form button overrides via default_scope/default_project_id
    # context (spec §2.2).
    scope = fields.Selection(
        selection=[('system', 'System'), ('user', 'User')],
        string='Scope',
        required=True,
        default='system',
    )
    project_id = fields.Many2one(
        'project.project',
        string='Project',
        help='Required for user-scope skills; must stay empty for system.',
    )
    bundle_file = fields.Binary(string='Skill Bundle (.tar.gz)', required=True)
    bundle_filename = fields.Char(string='Filename')

    def action_upload(self):
        """Store the uploaded bundle bytes as a draft skill (system or
        user scope). Odoo never parses the bundle: decode, size-cap,
        sha256 over RAW bytes, persist anchored attachment + draft skill.
        """
        self.ensure_one()
        if self.scope == 'user' and not self.project_id:
            raise UserError("User-scope skills must be linked to a project.")
        if self.scope == 'system' and self.project_id:
            raise UserError(
                "System-scope skills must not be linked to a project.")
        raw = base64.b64decode(self.bundle_file)
        if len(raw) > self.MAX_BUNDLE_SIZE:
            raise UserError("Bundle exceeds the 10 MB limit.")

        bundle_sha256 = hashlib.sha256(raw).hexdigest()
        bundle_size_kb = -(-len(raw) // 1024)  # ceil division

        Skill = self.env['woow_paas_platform.skill'].sudo()
        # Per-scope dedupe mirrors the two partial unique indexes
        # (skill.py init): system → (name, version) WHERE project NULL;
        # user → (project_id, name, version).
        dedupe_domain = [
            ('name', '=', self.name),
            ('version', '=', self.version),
            ('project_id', '=',
             self.project_id.id if self.scope == 'user' else False),
        ]
        if Skill.search(dedupe_domain, limit=1):
            raise UserError(
                "%s skill '%s' v%s already exists; bump the version."
                % (self.scope.capitalize(), self.name, self.version)
            )

        attachment = self.env['ir.attachment'].sudo().create({
            'name': self.bundle_filename or '%s-%s.tar.gz' % (
                self.name, self.version),
            'datas': self.bundle_file,
            'mimetype': 'application/gzip',
        })
        skill = Skill.create({
            'scope': self.scope,
            'project_id': self.project_id.id if self.scope == 'user' else False,
            'state': 'draft',
            'name': self.name,
            'version': self.version,
            'title': self.title or self.name,
            'description': self.description or '',
            'bundle_sha256': bundle_sha256,
            'bundle_size_kb': bundle_size_kb,
            'bundle_attachment_id': attachment.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'woow_paas_platform.skill',
            'res_id': skill.id,
            'view_mode': 'form',
            'target': 'current',
        }
```

- [ ] **Step 4: wizard view + skill form view**

`src/views/skill_views.xml` — wizard form（`:155-183` 的 `<form string="Upload Skill">` 區塊）`<group>` 第一組改為：

```xml
                <group>
                    <group>
                        <field name="name"/>
                        <field name="version"/>
                        <field name="scope"/>
                        <field name="project_id"
                               invisible="scope != 'user'"
                               required="scope == 'user'"/>
                    </group>
                    <group>
                        <field name="title"/>
                    </group>
                </group>
```

同檔 wizard form 開頭 `<p class="text-muted">` 文案改為：

```xml
                <p class="text-muted">
                    Upload a <code>.tar.gz</code> skill bundle. Name / version
                    must match the bundle's SKILL.md. The skill is created as
                    <b>draft</b> — publish it afterwards. User-scope skills
                    bind to a single project; system skills load platform-wide.
                </p>
```

skill form（`:63-68` Identity group）改為（雙節點 groups pattern，model write guard 仍是真正防線）：

```xml
                        <group string="Identity">
                            <field name="title" readonly="1"/>
                            <field name="is_latest" readonly="1"/>
                            <field name="scope" groups="base.group_system"/>
                            <field name="project_id"
                                   groups="base.group_system"
                                   invisible="scope != 'user'"
                                   required="scope == 'user'"/>
                            <field name="project_id" readonly="1"
                                   groups="!base.group_system"/>
                        </group>
```

`src/models/skill.py` `action_open_upload_wizard`（`:259-275`）的 return dict 加 context 轉發（讓 project form 的按鈕能預填）：

```python
        return {
            'type': 'ir.actions.act_window',
            'name': 'Upload Skill',
            'res_model': 'woow_paas_platform.skill_upload_wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                k: v for k, v in self.env.context.items()
                if k in ('default_scope', 'default_project_id')
            },
        }
```

- [ ] **Step 5: 更新 view 守線測試**

`src/tests/test_skill_admin_views.py` 追加（class 尾）：

```python
    def test_skill_form_scope_project_editable_for_admin(self):
        """§1 — form 對 admin 暴露 scope + 條件式 project_id（存在性斷言，
        不鎖整段 arch — phase-versioned-test-assertion 紀律）。"""
        arch = self.env.ref('woow_paas_platform.view_skill_form').arch
        self.assertIn('name="scope"', arch)
        self.assertIn('groups="base.group_system"', arch)
        self.assertIn("invisible=\"scope != 'user'\"", arch)
        self.assertIn("required=\"scope == 'user'\"", arch)

    def test_wizard_form_exposes_scope_project(self):
        arch = self.env.ref(
            'woow_paas_platform.view_skill_upload_wizard_form').arch
        self.assertIn('name="scope"', arch)
        self.assertIn('name="project_id"', arch)
```

- [ ] **Step 6: 跑測試確認綠**（含既有 `test_skill_upload_wizard.py` 全綠 — 既有 system 上傳行為不回歸）

- [ ] **Step 7: Commit**

```bash
git add src/wizard/skill_upload_wizard.py src/views/skill_views.xml \
        src/models/skill.py src/tests/test_skill_binding_admin.py \
        src/tests/test_skill_admin_views.py
git commit -m "feat: wizard user-scope upload and admin-editable skill scope/project"
```

---

### Task 5: project 的 resolved template computed + fork action + wizard 入口

**Files:**
- Modify: `src/models/project_project.py`（欄位 + methods，`agent_template_id` 之後）
- Test: `src/tests/test_skill_binding_admin.py`（追加）

- [ ] **Step 1: 追加紅測試**

```python
from odoo.exceptions import AccessError


@tagged('post_install', '-at_install')
class TestDedicatedTemplateFork(TransactionCase):
    """§2.3 — copy-on-write fork 與 resolved 狀態。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.proj = cls.env['project.project'].sudo().create({'name': 'FK'})
        cls.other = cls.env['project.project'].sudo().create({'name': 'FK2'})
        cls.sys_skill = _mk_skill(cls.env, name='fk-sys', state='published')
        cls.other_skill = _mk_skill(
            cls.env, name='fk-other', scope='user',
            project_id=cls.other.id, state='published')
        # 共用 template：綁兩個 project、綁 system + other 的 user skill
        cls.shared = cls.env['woow_paas_platform.agent_template'].sudo() \
            .create({
                'name': 'FK Shared',
                'project_ids': [(6, 0, [cls.proj.id, cls.other.id])],
                'skill_ids': [(6, 0, [cls.sys_skill.id, cls.other_skill.id])],
                'state': 'published',
            })

    def test_binding_status_shared_then_dedicated(self):
        self.assertEqual(self.proj.agent_template_binding_status, 'shared')
        self.proj.action_create_dedicated_agent_template()
        self.proj.invalidate_recordset()
        self.assertEqual(self.proj.agent_template_binding_status, 'dedicated')

    def test_fork_filters_foreign_user_scope(self):
        self.proj.action_create_dedicated_agent_template()
        fork = self.proj.resolved_agent_template_id
        self.assertNotEqual(fork, self.shared)
        self.assertIn(self.sys_skill, fork.skill_ids)       # system 帶入
        self.assertNotIn(self.other_skill, fork.skill_ids)  # 他租戶過濾
        self.assertEqual(fork.project_ids, self.proj)
        self.assertEqual(fork.state, 'published')
        self.assertFalse(fork.is_public)
        # 來源未被改動
        self.assertIn(self.other_skill, self.shared.skill_ids)

    def test_fork_requires_admin(self):
        user = self.env['res.users'].sudo().create({
            'name': 'FK User', 'login': 'fk_user',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.proj.with_user(user).action_create_dedicated_agent_template()

    def test_fork_blocked_when_already_dedicated(self):
        self.proj.action_create_dedicated_agent_template()
        self.proj.invalidate_recordset()
        with self.assertRaises(UserError):
            self.proj.action_create_dedicated_agent_template()

    def test_inverse_skill_binding_writes_through_on_dedicated(self):
        self.proj.action_create_dedicated_agent_template()
        self.proj.invalidate_recordset()
        own = _mk_skill(self.env, name='fk-own', scope='user',
                        project_id=self.proj.id, state='published')
        fork = self.proj.resolved_agent_template_id
        self.proj.resolved_template_skill_ids = [(4, own.id)]
        self.assertIn(own, fork.skill_ids)

    def test_inverse_rejected_on_shared(self):
        with self.assertRaises(UserError):
            self.proj.resolved_template_skill_ids = [(4, self.sys_skill.id)]
```

- [ ] **Step 2: 跑測試確認紅**（欄位/方法不存在）

- [ ] **Step 3: 實作**（`src/models/project_project.py`，import 區補 `from odoo.exceptions import AccessError, UserError`；欄位加在 `agent_template_id`〔`:75-79`〕之後）

```python
    # ------------------------------------------------------------------ #
    # Skill binding UX（spec 2026-06-05）— resolved template 顯示 + fork  #
    # ------------------------------------------------------------------ #
    resolved_agent_template_id = fields.Many2one(
        'woow_paas_platform.agent_template',
        string='Resolved Agent Template',
        compute='_compute_resolved_agent_template',
        help='Template the agent runtime resolves for this project '
             '(project-bound published → any bound → manual M2O → '
             'platform default).',
    )
    agent_template_binding_status = fields.Selection(
        selection=[
            ('dedicated', 'Dedicated'),
            ('shared', 'Shared'),
            ('platform_default', 'Platform Default'),
            ('none', 'None'),
        ],
        string='Template Binding Status',
        compute='_compute_resolved_agent_template',
    )
    resolved_template_skill_ids = fields.Many2many(
        'woow_paas_platform.skill',
        string='Template Bound Skills',
        compute='_compute_resolved_template_skills',
        inverse='_inverse_resolved_template_skills',
        help='skill_ids of the resolved template. Editable only when the '
             'template is dedicated to this project — compute+inverse '
             'instead of a related chain through the non-stored computed '
             'M2O (spec §2.3 implementation note).',
    )
```

methods（`_compute_workspace_id` 之後）：

```python
    def _compute_resolved_agent_template(self):
        Session = self.env['woow_paas_platform.agent_session']
        default_raw = self.env['ir.config_parameter'].sudo().get_param(
            'woow_paas_platform.default_agent_template_id') or ''
        default_id = int(default_raw) if default_raw.isdigit() else 0
        for project in self:
            template = Session._resolve_agent_template(project)
            project.resolved_agent_template_id = template or False
            if not template:
                project.agent_template_binding_status = 'none'
            elif template.id == default_id:
                project.agent_template_binding_status = 'platform_default'
            elif template.project_ids == project:
                project.agent_template_binding_status = 'dedicated'
            else:
                # bound to several projects, or an unbound manual pick —
                # treat both as shared (read-only + fork offered).
                project.agent_template_binding_status = 'shared'

    def _compute_resolved_template_skills(self):
        for project in self:
            template = project.resolved_agent_template_id
            project.resolved_template_skill_ids = \
                template.skill_ids if template else False

    def _inverse_resolved_template_skills(self):
        for project in self:
            if project.agent_template_binding_status != 'dedicated':
                raise UserError(
                    "Skill bindings can only be edited on a dedicated "
                    "template — use 'Create Dedicated Template' first.")
            project.resolved_agent_template_id.skill_ids = [
                (6, 0, project.resolved_template_skill_ids.ids)]

    def action_create_dedicated_agent_template(self):
        """Copy-on-write fork (spec §2.3). ONE copy(default=) call: m2m
        fields default to copy=True, so a bare copy() would leak the
        source's project_ids/skill_ids/mcp_server_ids into the fork and
        trip _check_project_ids_grantor_authority / subset constraints on
        an intermediate state. default= makes every constraint validate
        the FINAL values at create time (anti-pattern #4: no sudo bypass).
        """
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(
                "Only administrators can create a dedicated agent template.")
        if self.agent_template_binding_status == 'dedicated':
            raise UserError("This project already has a dedicated template.")
        source = self.resolved_agent_template_id
        if not source:
            raise UserError(
                "No agent template resolves for this project — configure "
                "the platform default in Settings → Woow PaaS first.")
        allowed_skills = source.skill_ids.filtered(
            lambda s: s.scope == 'system'
            or (s.scope == 'user' and s.project_id == self))
        allowed_mcp = source.mcp_server_ids.filtered(
            lambda m: m.scope == 'system'
            or (m.scope == 'user' and m.project_id == self))
        fork = source.copy(default={
            'name': "%s — %s" % (source.name, self.name),
            'project_ids': [(6, 0, [self.id])],
            'skill_ids': [(6, 0, allowed_skills.ids)],
            'mcp_server_ids': [(6, 0, allowed_mcp.ids)],
            'is_public': False,
            'state': 'published',
        })
        # Belt-and-braces: resolution order ① already prefers the fork
        # (project-bound published), the M2O makes the pick explicit.
        self.agent_template_id = fork
        Session = self.env['woow_paas_platform.agent_session']
        Session._push_snapshot_to_running_pods('skill', 'user', self.id)
        Session._push_snapshot_to_running_pods('mcp', 'user', self.id)
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_open_skill_upload_wizard(self):
        """Project-form entry: upload wizard prefilled as a user-scope
        skill owned by this project (spec §2.1)."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Upload Skill',
            'res_model': 'woow_paas_platform.skill_upload_wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_scope': 'user',
                'default_project_id': self.id,
            },
        }
```

- [ ] **Step 4: 跑測試確認綠**（含 `test_agent_pod_phase_a_acl_new_models`、seed/default template 相關測試全綠 — 平台預設不被動到）

- [ ] **Step 5: Commit**

```bash
git add src/models/project_project.py src/tests/test_skill_binding_admin.py
git commit -m "feat: resolved-template compute and copy-on-write dedicated fork on project"
```

---

### Task 6: agent_template 綁定變更 push snapshot

**Files:**
- Modify: `src/models/agent_template.py`（`_check_skill_subset` 之後加 write）
- Test: `src/tests/test_skill_binding_admin.py`（追加）

- [ ] **Step 1: 追加紅測試**

```python
@tagged('post_install', '-at_install')
class TestTemplateBindingPush(TransactionCase):
    """§2.3 連帶 — template 綁定變更 best-effort push（#111 語意）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.proj = cls.env['project.project'].sudo().create({'name': 'TP'})
        cls.skill = _mk_skill(cls.env, name='tp-sys', state='published')
        cls.tpl = cls.env['woow_paas_platform.agent_template'].sudo().create({
            'name': 'TP Tpl',
            'project_ids': [(6, 0, [cls.proj.id])],
            'state': 'published',
        })

    def test_skill_binding_change_pushes(self):
        with patch.object(
            type(self.env['woow_paas_platform.agent_session']),
            '_push_snapshot_to_running_pods',
        ) as push:
            self.tpl.write({'skill_ids': [(4, self.skill.id)]})
        self.assertIn(
            ('skill', 'user', self.proj.id),
            {c.args[:3] for c in push.call_args_list})

    def test_non_binding_write_does_not_push(self):
        with patch.object(
            type(self.env['woow_paas_platform.agent_session']),
            '_push_snapshot_to_running_pods',
        ) as push:
            self.tpl.write({'description': 'no push'})
        push.assert_not_called()
```

- [ ] **Step 2: 跑測試確認紅**

- [ ] **Step 3: 實作**（`agent_template.py` 檔尾加）

```python
    def write(self, vals):
        result = super().write(vals)
        # Binding change alters what loads into running pods → best-effort
        # push (#111 semantics: per-pod failure logged, never aborts).
        # Projects resolving via the manual M2O are included; the unbound
        # platform default is not walked (it binds system resources only —
        # skill-state pushes already cover system-wide changes; next chat
        # attach refresh is the fallback).
        if 'skill_ids' in vals or 'mcp_server_ids' in vals:
            Session = self.env['woow_paas_platform.agent_session']
            for template in self:
                projects = template.project_ids | self.env[
                    'project.project'].sudo().search([
                        ('agent_template_id', '=', template.id)])
                for project in projects:
                    if 'skill_ids' in vals:
                        Session._push_snapshot_to_running_pods(
                            'skill', 'user', project.id)
                    if 'mcp_server_ids' in vals:
                        Session._push_snapshot_to_running_pods(
                            'mcp', 'user', project.id)
        return result
```

- [ ] **Step 4: 跑測試確認綠**

- [ ] **Step 5: Commit**

```bash
git add src/models/agent_template.py src/tests/test_skill_binding_admin.py
git commit -m "feat: push pod snapshots when template skill/mcp bindings change"
```

---

### Task 7: project form view — Skills 區塊強化 + Agent Template 小節

**Files:**
- Modify: `src/views/project_views.xml:75-89`
- Modify: `src/tests/test_project_form_mcp_skill_tab.py`

- [ ] **Step 1: 追加紅測試**（`test_project_form_mcp_skill_tab.py` class 尾）

該檔 `setUpClass`（`:22-28`）已備好 `cls.tree`（`get_view` 合成 arch 的 lxml etree）— 新測試直接用 `self.tree`。注意：`get_view` 對 admin 環境會保留 `groups=` 節點並附 attribute，斷言屬性存在即可。

```python
    def test_skill_block_has_admin_upload_button(self):
        """§2.1 — Upload 按鈕（groups=base.group_system）。"""
        button = self.tree.xpath(
            "//page[@name='settings']"
            "//button[@name='action_open_skill_upload_wizard']")
        self.assertTrue(button, "project form must expose Upload Skill")
        self.assertEqual(button[0].get('groups'), 'base.group_system')

    def test_skill_embedded_list_shows_is_effective(self):
        self.assertTrue(self.tree.xpath(
            "//field[@name='skill_ids']//field[@name='is_effective']"))

    def test_skill_embedded_list_still_lifecycle_safe(self):
        """守 F-SKU-1：create/delete 仍鎖死（不因本次強化鬆動）。"""
        node = self.tree.xpath("//field[@name='skill_ids']/list")[0]
        self.assertEqual(node.get('create'), 'false')
        self.assertEqual(node.get('delete'), 'false')

    def test_agent_template_section_present(self):
        """§2.3 — resolved template 顯示 + fork 按鈕 + 條件唯讀 m2m。"""
        self.assertTrue(self.tree.xpath(
            "//field[@name='resolved_agent_template_id']"))
        fork = self.tree.xpath(
            "//button[@name='action_create_dedicated_agent_template']")
        self.assertTrue(fork)
        self.assertEqual(fork[0].get('groups'), 'base.group_system')
        m2m = self.tree.xpath(
            "//field[@name='resolved_template_skill_ids']")
        self.assertTrue(m2m)
        self.assertEqual(m2m[0].get('groups'), 'base.group_system')
```

- [ ] **Step 2: 跑測試確認紅**

- [ ] **Step 3: 實作 view**（`project_views.xml` `:75-89` Skills 區塊整段替換）

```xml
                <separator string="Skills"/>
                <!-- 歸屬列表（ownership）：create/delete 仍鎖死（F-SKU-1：
                     bundle-backed 只能走 wizard）。Upload 按鈕（admin）開
                     wizard 預填本 project；is_effective 標示「真生效」—
                     歸屬 ≠ 生效（ownership-vs-activation anti-pattern #3
                     的 UI mitigation）。 -->
                <button name="action_open_skill_upload_wizard" type="object"
                        string="Upload Skill" class="btn-primary"
                        icon="fa-cloud-upload"
                        groups="base.group_system"/>
                <field name="skill_ids"
                       nolabel="1">
                    <list create="false" delete="false">
                        <field name="name"/>
                        <field name="version"/>
                        <field name="state" widget="badge"
                               decoration-success="state == 'published'"
                               decoration-warning="state == 'deprecated'"
                               decoration-muted="state == 'archived'"
                               decoration-info="state == 'draft'"/>
                        <field name="is_latest" widget="boolean_toggle" readonly="1"/>
                        <field name="is_effective" readonly="1"/>
                        <field name="uploaded_at"/>
                    </list>
                </field>
                <separator string="Agent Template"/>
                <!-- 生效層（activation）：resolved template 顯示；dedicated
                     才可編輯綁定（compute+inverse 把守，view readonly 是 UX
                     層）；shared / platform_default 走 copy-on-write fork。
                     groups=base.group_system 是 UI 收斂非安全邊界 — model
                     層授權沿用 agent_template ir.rule（spec §2 語意釐清）。 -->
                <group>
                    <group>
                        <field name="resolved_agent_template_id" readonly="1"/>
                        <field name="agent_template_binding_status" readonly="1"/>
                    </group>
                    <group>
                        <button name="action_create_dedicated_agent_template"
                                type="object"
                                string="Create Dedicated Template"
                                class="btn-primary"
                                groups="base.group_system"
                                invisible="agent_template_binding_status in ('dedicated', 'none')"/>
                    </group>
                </group>
                <div class="alert alert-info" role="alert"
                     invisible="agent_template_binding_status != 'none'">
                    No agent template resolves for this project. Configure the
                    platform default in Settings → Woow PaaS first.
                </div>
                <field name="resolved_template_skill_ids"
                       widget="many2many_tags"
                       groups="base.group_system"
                       readonly="agent_template_binding_status != 'dedicated'"
                       options="{'no_create': True}"
                       domain="['|', ('scope', '=', 'system'),
                                '&amp;', ('scope', '=', 'user'),
                                ('project_id', '=', id)]"/>
```

- [ ] **Step 4: 跑測試確認綠**（含該檔既有 10 個測試全綠）

- [ ] **Step 5: Commit**

```bash
git add src/views/project_views.xml src/tests/test_project_form_mcp_skill_tab.py
git commit -m "feat: project form skills upload entry, effectiveness column and template section"
```

---

### Task 8: feature flag 基建（config param + Settings UI + 卸載清理）

**Files:**
- Modify: `src/models/res_config_settings.py`
- Modify: `src/views/res_config_settings_views.xml`
- Modify: `src/hooks.py:123-140`

- [ ] **Step 1: 加 settings 欄位**（`res_config_settings.py`，比照 `:84` `topup_payment_enabled` pattern）

```python
    woow_portal_project_settings_editable = fields.Boolean(
        string='Portal Project Settings Editable',
        config_parameter='woow_paas_platform.portal_project_settings_editable',
        help='When ON, /woow portal owners/admins can edit project name/'
             'description, cloud-service binding and user-scope MCP servers. '
             'Default OFF: portal project settings are read-only and managed '
             'from the backend (2026-06-03 meeting, topic H).',
    )
```

- [ ] **Step 2: Settings view**（`res_config_settings_views.xml` 用現代 `<block>/<setting>` 結構 — 在 `</block>`〔`:97`，Credit Top-up Plans block 結尾〕之後、`<block title="AI Assistant Configuration">`〔`:98`〕之前插入獨立 block）

```xml
                    <block title="Portal" name="portal_settings">
                        <setting string="Portal Project Settings Editable"
                                 help="Allow /woow portal owners/admins to edit project settings (name/description/binding/MCP). OFF = read-only portal (meeting 2026-06-03 topic H).">
                            <field name="woow_portal_project_settings_editable"/>
                        </setting>
                    </block>
```

- [ ] **Step 3: hooks 清理清單**（`src/hooks.py` `_CONFIG_PARAMETERS`，`topup_payment_enabled` 行後加）

```python
    'woow_paas_platform.portal_project_settings_editable',
```

- [ ] **Step 4: 跑既有 hooks/settings 相關測試確認綠**

- [ ] **Step 5: Commit**

```bash
git add src/models/res_config_settings.py src/views/res_config_settings_views.xml src/hooks.py
git commit -m "feat: add portal_project_settings_editable feature flag"
```

---

### Task 9: paas.py flag gates + `portal_editable` payload + HttpCase 守線

**Files:**
- Modify: `src/controllers/paas.py`（`:1541-1547`、`:1549-1569`、`:1627-1631`，module-level helper）
- Create: 追加於 `src/tests/test_portal_settings_flag.py`
- Modify: `src/tests/test_project_detail_endpoints.py:102`、`src/tests/test_project_mcp_project_centric.py:98`（setUp 開 flag）

- [ ] **Step 1: 建紅測試** `src/tests/test_portal_settings_flag.py`：

```python
"""Flag-off（預設）портal 唯讀守線 — HttpCase endpoint-level。

orm-vs-endpoint-acl-test-coverage pattern：controller 層守線必須打真 HTTP
endpoint，ORM with_user 測不到 controller sudo/gate 行為。
重用 test_project_detail_endpoints 的完整 fixture（DRY）。
"""
from odoo.tests import tagged

from .test_project_detail_endpoints import TestProjectDetailFixture


@tagged('post_install', '-at_install')
class TestPortalSettingsFlagOff(TestProjectDetailFixture):
    """旗標未設（fail-closed off）：owner/admin 也不可寫；get/list 照常。"""

    def setUp(self):
        super().setUp()
        # 本 fixture 的 setUp 會開 flag（讓既有正向測試保值）；本 class 驗
        # 預設 off 行為 → 明確關閉。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'False')

    def _url_mcp(self, project_id):
        return f'/api/support-projects/{project_id}/mcp-servers'

    def test_get_still_works_and_reports_readonly(self):
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_detail(self.project_w1_basic.id),
                         {'action': 'get'})
        self.assertTrue(res['success'])
        self.assertFalse(res['data']['portal_editable'])

    def test_update_denied_for_owner(self):
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_detail(self.project_w1_basic.id),
                         {'action': 'update', 'name': 'NEW'})
        self.assertFalse(res['success'])
        self.assertEqual(res['error'], 'Permission denied')
        self.assertEqual(self.project_w1_basic.name, 'PD W1 Basic Project')

    def test_mcp_create_denied_for_owner(self):
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_mcp(self.project_w1_basic.id), {
            'action': 'create', 'name': 'X',
            'url': 'https://mcp.example.com/sse',
        })
        self.assertFalse(res['success'])
        self.assertEqual(res['error'], 'Permission denied')

    def test_mcp_list_still_works(self):
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_mcp(self.project_w1_with_mcp.id),
                         {'action': 'list'})
        self.assertTrue(res['success'])

    def test_support_projects_update_denied_cross_endpoint(self):
        """BLOCKER 守線：ai_assistant 的 /api/support/projects update 同受
        flag gate（two-layer-acl-lockstep 第三支 endpoint）。"""
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post('/api/support/projects', {
            'action': 'update',
            'project_id': self.project_w1_basic.id,
            'name': 'BYPASS',
        })
        self.assertFalse(res['success'])
        self.assertEqual(self.project_w1_basic.name, 'PD W1 Basic Project')

    def test_flag_on_restores_update(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
        self.authenticate('pd_alice', 'pd_alice')
        res = self._post(self._url_detail(self.project_w1_basic.id),
                         {'action': 'update', 'name': 'Edited'})
        self.assertTrue(res['success'])
        self.assertTrue(res['data']['portal_editable'])
```

- [ ] **Step 2: 跑測試確認紅**（update 仍成功、`portal_editable` key 不存在）

- [ ] **Step 3: 實作 paas.py**

module-level（class 外、`_validate_mcp_url` 附近）：

```python
PORTAL_SETTINGS_FLAG = 'woow_paas_platform.portal_project_settings_editable'


def _portal_project_settings_editable(env) -> bool:
    """Feature flag（2026-06-03 議題 H）：portal project settings 預設唯讀。
    Param 缺值 = off（fail-closed）。ai_assistant.py import 共用 —
    two-layer-acl-lockstep：三支寫入 endpoint 必須同一開關。"""
    return (env['ir.config_parameter'].sudo().get_param(
        PORTAL_SETTINGS_FLAG) or '').strip().lower() in ('true', '1')
```

`api_support_project_detail`（`:1543-1546`）update 分支改為：

```python
        if action == 'update':
            if not _portal_project_settings_editable(request.env):
                return {'success': False, 'error': 'Permission denied'}
            if access.role not in (ROLE_OWNER, ROLE_ADMIN):
                return {'success': False, 'error': 'Permission denied'}
            return self._update_support_project(project, kw)
```

`_format_project_detail` return dict 加一行（`'task_count'` 前）：

```python
            'portal_editable': _portal_project_settings_editable(request.env),
```

`api_support_project_mcp_servers`（`:1630-1631`）gate 改為：

```python
        if action in write_actions:
            if not _portal_project_settings_editable(request.env):
                return {'success': False, 'error': 'Permission denied'}
            if access.role not in (ROLE_OWNER, ROLE_ADMIN):
                return {'success': False, 'error': 'Permission denied'}
```

- [ ] **Step 4: 既有 fixture 開 flag（保值）**

`src/tests/test_project_detail_endpoints.py` `TestProjectDetailFixture.setUp`（`:103` `super().setUp()` 後）與 `src/tests/test_project_mcp_project_centric.py` `TestProjectMcpFixture.setUp`（`:98`）各加：

```python
        # skill-binding-ux：флag-on 保留既有正向行為守線（flag-off denied
        # 守線在 test_portal_settings_flag.py）。
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.portal_project_settings_editable', 'True')
```

- [ ] **Step 5: 跑測試**：新檔 5 紅轉綠（cross-endpoint 那條仍紅 — Task 10 收）+ `test_project_detail_endpoints` / `test_project_mcp_project_centric` 全綠

- [ ] **Step 6: Commit**

```bash
git add src/controllers/paas.py src/tests/test_portal_settings_flag.py \
        src/tests/test_project_detail_endpoints.py \
        src/tests/test_project_mcp_project_centric.py
git commit -m "feat: gate portal project/mcp write endpoints behind settings flag"
```

---

### Task 10: ai_assistant.py `_update_project` flag gate（BLOCKER 修正）

**Files:**
- Modify: `src/controllers/ai_assistant.py:1573-1577`
- Test: `src/tests/test_portal_settings_flag.py::test_support_projects_update_denied_cross_endpoint`（Task 9 已寫）

- [ ] **Step 1: 確認該測試仍紅**

- [ ] **Step 2: 實作**：`ai_assistant.py` import 區加：

```python
from .paas import _portal_project_settings_editable
```

`_update_project`（`:1573`）開頭 `denied` 檢查後加：

```python
        denied = self._portal_write_denied()
        if denied:
            return denied
        # skill-binding-ux flag（spec §3 BLOCKER 修正）：本 endpoint 與
        # paas.py 兩支同 gate — 漏這支 = 前台唯讀被 API 繞過
        # （two-layer-acl-lockstep）。create/delete 刻意不 gate（不屬
        # 「project 設定頁」範圍，會議未決議）。
        if not _portal_project_settings_editable(request.env):
            return {'success': False, 'error': 'Permission denied'}
```

- [ ] **Step 3: 跑測試確認綠** + grep 防 import 循環：`python3 -c "import ast"` 不必；直接以模組更新跑 `test_portal_settings_flag` 全綠即證明 import 無循環。另跑 `test_project_decouple_frontend_endpoint`（T1 create 必須仍綠 — create 不 gate）與 `test_portal_agent_endpoints`（share-denied 語意不變）。

- [ ] **Step 4: Commit**

```bash
git add src/controllers/ai_assistant.py
git commit -m "fix: gate ai_assistant project update behind portal settings flag"
```

---

### Task 11: 前端唯讀化（flag off 條件渲染）

**Files:**
- Modify: `src/static/src/paas/pages/project-detail/ProjectDetailPage.xml:40-52`
- Modify: `src/static/src/paas/pages/project-detail/sections/BasicInfoSection.js:12-15`
- Modify: `src/static/src/paas/pages/project-detail/sections/BasicInfoSection.xml:35-43,89-100`

- [ ] **Step 1: ProjectDetailPage.xml** — sections 區塊（`:40-52`）改為：

```xml
                <div class="o_woow_project_detail__sections">
                    <BasicInfoSection
                        project="state.project"
                        editable="!!state.project.portal_editable"
                        onUpdated="(updated) => this.onProjectUpdated(updated)"/>

                    <!-- 會議 2026-06-03 議題 H：flag off（預設）時 binding 與
                         MCP section 整個不渲染；flag on 完整恢復（spec §3）。 -->
                    <t t-if="state.project.portal_editable">
                        <CloudServiceBindingSection
                            project="state.project"
                            onUpdated="(updated) => this.onProjectUpdated(updated)"/>

                        <McpServersSection
                            project="state.project"
                            onListChanged="(mcps) => this.onMcpListChanged(mcps)"/>
                    </t>
                </div>
```

- [ ] **Step 2: BasicInfoSection.js** — props（`:12-15`）改為：

```javascript
    static props = {
        project: { type: Object },
        onUpdated: { type: Function },
        editable: { type: Boolean, optional: true },
    };
```

- [ ] **Step 3: BasicInfoSection.xml** — name 的編輯鈕（`:38-41`）與 description 的編輯鈕（`:95-98`）各包 `t-if`：

```xml
                        <button t-if="props.editable"
                                class="o_woow_project_detail__edit_btn"
                                t-on-click="startEditName">
                            <WoowIcon name="'edit'" size="16"/>
                        </button>
```

```xml
                        <button t-if="props.editable"
                                class="o_woow_project_detail__edit_btn"
                                t-on-click="startEditDescription">
                            <WoowIcon name="'edit'" size="16"/>
                        </button>
```

- [ ] **Step 4: 手動驗證**（sandbox）：flag off → `#/projects/{id}` 只見 Basic Info（無編輯鈕、無 binding/MCP section）；`set_param ... 'True'` 後 reload → 三 section 與編輯鈕恢復。截圖留檔。

- [ ] **Step 5: Commit**

```bash
git add src/static/src/paas/pages/project-detail/
git commit -m "feat: render portal project settings read-only when flag is off"
```

---

### Task 12: docs 更新（spec §5 全清單）

**Files:** 七檔（見 spec §5 表）。每檔動作：

- [ ] `docs/decisions/patterns/resource-ownership-vs-template-activation.md`：
  1. 錨點表行號核對更新（`skill.py` write/recompute 段、`project_views.xml` Skills 區塊行號位移）。
  2. 「生效集合公式」節後加一段：「**Push 觸發點**（2026-06-05 skill-binding-ux 起）：skill `state` 變更（既有）、skill `scope`/`project_id` re-point（新舊 project / system-wide）、template `skill_ids`/`mcp_server_ids` 變更（綁定 projects + 手動 M2O pointer）。」
  3. Anti-pattern #3 條目尾補：「UI mitigation 實例：project form 歸屬列表的 `is_effective` computed 欄（`_effective_skill_ids` 同源），明確標示真生效。」
- [ ] `docs/decisions/refactor/mcp-skill-two-layer-config-entry.md:31`：「（會議議題 H：MCP 設定拿掉、名稱/描述唯讀 — 另案）」→「已落地（2026-06-05 skill-binding-ux）：前台唯讀由 `woow_paas_platform.portal_project_settings_editable` flag gate（預設 off），三支寫入 endpoint + OWL 渲染 lock-step。」
- [ ] `docs/plans/2026-Q2/2026-05-19-project-detail-page-design.md` 檔頭加：「> **Superseded（partial）2026-06-05**：D10/AC#1-3/#14/#16 的前台編輯行為改由 portal_project_settings_editable flag gate，預設 off（唯讀）。flag on 時本設計行為完整保留。見 `docs/superpowers/specs/2026-06-05-skill-binding-ux-design.md`。」
- [ ] `docs/plans/2026-Q2/2026-05-30-skill-dual-scope-design.md:200` 前台建 user skill 段尾加：「（2026-06-05 更新：user skill 建立改走後台 wizard〔scope/project 擴充〕，前台上傳不做 — 見 skill-binding-ux spec。）」
- [ ] `docs/plans/2026-Q2/2026-05-30-skill-upload-phase2-design.md:155` 前台上傳 bullet 尾加：「（2026-06-05：本項由 skill-binding-ux 決議取消 — 後台 wizard 已支援 user-scope。）」
- [ ] `docs/guides/paas-skill-system/03-skill-design.md:138-142` 上傳者表 user 列改：「admin（後台 wizard — project form『Upload Skill』入口或 Configuration 選單）」。
- [ ] `docs/tracking/followups/follow-ups.md` 開頭 State 段下加條目：
  - `F-SBX-1`：portal settings flag（`portal_project_settings_editable`，預設 off）— 未來開放翻 Settings 參數即可；開放前須確認會議決議。
  - `F-SBX-2`：MCP inline form 無 sync/test 鈕 — admin 在 project form 建 MCP 後須跳獨立 view sync 才達 `connected`（legacy chat 才吃得到）。
  - `F-SBX-3`：project owner 經 RPC 仍可寫 dedicated template（`agent_template_rule_project_admin` 既有授權）— 是否收緊待決策（**對應 Gitea issue：建立後補號**）。
- [ ] **Commit**

```bash
git add docs/
git commit -m "docs: align knowledge base with skill binding UX read-only portal"
```

---

### Task 13: 全量驗證

- [ ] **Step 1: 全模組測試**

```bash
./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> --module woow_paas_platform
```

Expected: 0 failed（特別關注：`test_skill_dual_scope`、`test_skill_admin_lifecycle`、`test_skill_upload_wizard`、`test_agent_pod_phase_a_acl_new_models`、`test_mcp_*` 系列、`test_default_agent_template`、`test_portal_agent_endpoints` — 全部不可回歸）

- [ ] **Step 2: 前端 e2e（playwright-cli，`--headed`）**：後台 admin 流程 — project form Upload Skill → publish → Create Dedicated Template → 綁定 m2m → `is_effective` 變 ✓；前台 flag off 唯讀（無編輯鈕/section）。
- [ ] **Step 3: 對抗式審查**（spec §6 checklist 逐條 + security / reuse 兩 lens reviewer）。
- [ ] **Step 4: 完整串流驗證**：merge 後 develop 跑 `/woow:deploy-stg`（本機 operator 過舊只能驗到 spawn 邊界 — gap-chain pitfall）。

---

## Self-review 紀錄

- Spec 覆蓋：§1→Task 2/4、§2.1→Task 1/3/4/7、§2.2→Task 4、§2.3→Task 5/6/7、§3→Task 8/9/10/11、§4→各 task 測試 + Task 13、§5→Task 12、§6→全程 + Task 13 Step 3。
- 無 placeholder；型別/方法名跨 task 一致（`_effective_skill_ids` / `_recompute_is_latest_for` / `resolved_agent_template_id` / `agent_template_binding_status` / `portal_editable`）。
