---
title: Platform Default Agent Template — Implementation Plan
category: archived
status: archived
created: 2026-06-01
archived: 2026-07-19
archived-from: docs/superpowers/plans/2026-06-01-default-agent-template.md
archived-reason: "PR #112 已 merge、逐步 TDD 過程紀錄的知識已入 code＋測試；機制的現行權威描述在 docs/guides/development/agent-runtime-chat-architecture.md（#111 refactor 後的實際落點），對應 seed plan 亦已在 docs/archived/superpowers/plans/ 立下先例"
---

# Platform Default Agent Template — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let any project with no agent_template of its own fall back to a single platform-wide default template (set in Settings), instead of failing the agent-runtime chat with `No agent template configured`.

**Architecture:** A `res.config.settings` Many2one (`config_parameter`) stores the default template id — same pattern as the existing `Default AI Configuration` / `Default AI Assistant`. `_resolve_agent_template` (controller) gains a 4th, last-priority fallback that reads this config_parameter and re-validates `is_public AND published` before use. No `agent_template` model / migration / seed / ir.rule changes.

**Tech Stack:** Odoo 18, Python, `res.config.settings`, `ir.config_parameter`, Odoo `TransactionCase`, `unittest.mock.patch`.

**Spec:** `docs/archived/superpowers/specs/2026-06-01-default-agent-template-design.md`

**Branch:** `feat/default-agent-template` (already created from develop; spec doc already committed there).

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/models/res_config_settings.py` | Settings field for the platform default template | Add 1 Many2one field |
| `src/views/res_config_settings_views.xml` | Settings UI row (AI Assistant Configuration block) | Add 1 dropdown row |
| `src/hooks.py` | Uninstall cleanup of config_parameter keys | Add 1 list entry |
| `src/controllers/ai_assistant.py` | `_resolve_agent_template` fallback chain | Add 4th fallback branch |
| `src/tests/test_default_agent_template.py` | Tests for settings round-trip + resolution fallback | New file |
| `src/tests/__init__.py` | Test registration | Add 1 import |

---

## Task 1: Settings field + view row + uninstall cleanup

The Settings dropdown that lets an admin pick the platform default template, plus the uninstall-cleanup entry. These three edits form one logical unit (the "Default Agent Template" setting) and ship in one commit.

**Files:**
- Modify: `src/models/res_config_settings.py` (after `woow_ai_assistant_id`, ends line 47)
- Modify: `src/views/res_config_settings_views.xml` (inside `block name="ai_assistant"`, after the `woow_ai_assistant_id` row ending line 111, before the Test Connection `div` at line 112)
- Modify: `src/hooks.py` (`_CONFIG_PARAMETERS` list, after `default_ai_assistant_id` line 128)
- Test: `src/tests/test_default_agent_template.py` (new)

- [ ] **Step 1: Register the new test module**

Add to `src/tests/__init__.py` immediately after line 97 (`from . import test_skill_upload_wizard`) and before the `.integration` line:

```python
# feat: platform default agent template (Settings fallback)
from . import test_default_agent_template
```

- [ ] **Step 2: Write the failing test for the settings round-trip**

Create `src/tests/test_default_agent_template.py`:

```python
"""Platform default agent template — Settings field + resolution fallback.

Spec: docs/archived/superpowers/specs/2026-06-01-default-agent-template-design.md
Plan: docs/archived/superpowers/plans/2026-06-01-default-agent-template.md

Covers:
  S-1  res.config.settings.woow_default_agent_template_id round-trips through
       ir.config_parameter 'woow_paas_platform.default_agent_template_id'.
  D-1  project with no binding + valid default → resolution returns default.
  D-2  project with its own published template + default set → returns own.
  D-3  default points at an unpublished template → treated as unset (None).
  D-4  default points at a non-public template → treated as unset (None).
  D-5  config_parameter holds a stale/garbage value → no crash, returns None.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase

_PARAM = 'woow_paas_platform.default_agent_template_id'


class TestDefaultAgentTemplateSettings(TransactionCase):
    """S-1: the Settings Many2one persists to ir.config_parameter."""

    def test_s1_settings_field_round_trips_to_config_parameter(self):
        tmpl = self.env['woow_paas_platform.agent_template'].create({
            'name': 'Default Woow Template',
            'is_public': True,
            'state': 'published',
        })
        settings = self.env['res.config.settings'].create({
            'woow_default_agent_template_id': tmpl.id,
        })
        settings.execute()
        stored = self.env['ir.config_parameter'].sudo().get_param(_PARAM)
        self.assertEqual(
            str(stored), str(tmpl.id),
            "Settings field must persist the template id to the "
            "'%s' config_parameter." % _PARAM,
        )
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init 2>&1 | grep -iE "test_s1|Invalid field|FAIL|ERROR"`
Expected: FAIL — `Invalid field 'woow_default_agent_template_id'` (field not defined yet).

- [ ] **Step 4: Add the Settings field**

In `src/models/res_config_settings.py`, after the `woow_ai_assistant_id` field block (the closing `)` on line 47), insert:

```python
    woow_default_agent_template_id = fields.Many2one(
        'woow_paas_platform.agent_template',
        string='Default Agent Template',
        config_parameter='woow_paas_platform.default_agent_template_id',
        help='Platform-wide fallback template used by any project that has no '
             'template of its own (agent-runtime pod chat).',
    )
```

- [ ] **Step 5: Add the view row**

In `src/views/res_config_settings_views.xml`, inside `<block ... name="ai_assistant">`, after the `woow_ai_assistant_id` row (closing `</div>` on line 111) and before the `<div class="mt8">` Test Connection block (line 112), insert:

```xml
                                <div class="row mt8">
                                    <label for="woow_default_agent_template_id" class="col-lg-3 o_light_label"/>
                                    <field name="woow_default_agent_template_id"
                                           class="col-lg-6"/>
                                </div>
```

- [ ] **Step 6: Add the uninstall cleanup entry**

In `src/hooks.py`, in the `_CONFIG_PARAMETERS` list, after `'woow_paas_platform.default_ai_assistant_id',` (line 128), insert:

```python
    'woow_paas_platform.default_agent_template_id',
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags woow_paas_platform --stop-after-init 2>&1 | grep -iE "test_s1|FAIL|ERROR|passed"`
Expected: `test_s1_settings_field_round_trips_to_config_parameter` PASS, no errors.

> Note: the `-u woow_paas_platform` is required on this run because the view XML changed (module data reload).

- [ ] **Step 8: Commit**

```bash
git add src/models/res_config_settings.py src/views/res_config_settings_views.xml src/hooks.py src/tests/__init__.py src/tests/test_default_agent_template.py
git commit -m "feat: add platform Default Agent Template setting"
```

---

## Task 2: Resolution fallback in `_resolve_agent_template`

The core behaviour: when no project-bound template resolves, fall back to the Settings default — last priority (project binding always wins), re-validating `is_public AND published`, fail-loud on anything unqualified.

**Files:**
- Modify: `src/controllers/ai_assistant.py` — `_resolve_agent_template` (currently lines 755-768)
- Test: `src/tests/test_default_agent_template.py` (append a second test class)

- [ ] **Step 1: Write the failing tests for the fallback (D-1 … D-5)**

Append to `src/tests/test_default_agent_template.py`:

```python
class TestDefaultAgentTemplateResolution(TransactionCase):
    """D-1..D-5: _resolve_agent_template platform-default fallback.

    The method lives on the controller and reads ``request.env``; we
    instantiate the controller and patch its module-level ``request`` so
    ``request.env`` is the test env (the method calls ``.sudo()`` itself).
    """

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self._ai_assistant = ai_assistant
        self.controller = ai_assistant.AIAssistantController()
        self.AgentTemplate = self.env['woow_paas_platform.agent_template']
        self.ICP = self.env['ir.config_parameter'].sudo()

    def _resolve(self, project):
        """Call _resolve_agent_template with request.env bound to test env."""
        class _Req:
            env = self.env
        with patch.object(self._ai_assistant, 'request', _Req()):
            return self.controller._resolve_agent_template(project)

    def _set_default(self, template_id):
        self.ICP.set_param(_PARAM, str(template_id))

    def _make_project(self, name='Resolve Test Project'):
        return self.env['project.project'].create({'name': name})

    def test_d1_no_binding_valid_default_returns_default(self):
        tmpl = self.AgentTemplate.create({
            'name': 'Default Woow Template',
            'is_public': True, 'state': 'published',
        })
        self._set_default(tmpl.id)
        project = self._make_project()
        self.assertEqual(
            self._resolve(project), tmpl,
            "An unbound project must fall back to the platform default.",
        )

    def test_d2_project_binding_wins_over_default(self):
        own = self.AgentTemplate.create({
            'name': 'Own Template', 'is_public': True, 'state': 'published',
            'project_ids': [(6, 0, [])],
        })
        default = self.AgentTemplate.create({
            'name': 'Default Woow Template',
            'is_public': True, 'state': 'published',
        })
        project = self._make_project()
        own.write({'project_ids': [(4, project.id)]})
        self._set_default(default.id)
        self.assertEqual(
            self._resolve(project), own,
            "A project's own bound template must win over the platform default.",
        )

    def test_d3_unpublished_default_treated_as_unset(self):
        tmpl = self.AgentTemplate.create({
            'name': 'Draft Default', 'is_public': True, 'state': 'draft',
        })
        self._set_default(tmpl.id)
        project = self._make_project()
        self.assertIsNone(
            self._resolve(project),
            "An unpublished default must be treated as no default (fail-loud).",
        )

    def test_d4_non_public_default_treated_as_unset(self):
        # A non-public published template must bind a project (constraint),
        # so bind it to an unrelated project, then point the default at it.
        other_project = self._make_project('Other Project')
        tmpl = self.AgentTemplate.create({
            'name': 'Private Default', 'is_public': False, 'state': 'published',
            'project_ids': [(4, other_project.id)],
        })
        self._set_default(tmpl.id)
        project = self._make_project('Consumer Project')
        self.assertIsNone(
            self._resolve(project),
            "A non-public default must be treated as no default.",
        )

    def test_d5_garbage_config_value_does_not_crash(self):
        self._set_default('not-a-number')
        project = self._make_project()
        self.assertIsNone(
            self._resolve(project),
            "A non-numeric config value must be treated as no default, not crash.",
        )

    def test_d5b_deleted_template_id_does_not_crash(self):
        tmpl = self.AgentTemplate.create({
            'name': 'Doomed Default', 'is_public': True, 'state': 'published',
        })
        deleted_id = tmpl.id
        tmpl.unlink()
        self._set_default(deleted_id)
        project = self._make_project()
        self.assertIsNone(
            self._resolve(project),
            "A config id pointing at a deleted template must resolve to None.",
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `./odoo-bin -c odoo.conf --test-enable --test-tags woow_paas_platform --stop-after-init 2>&1 | grep -iE "test_d1_no_binding|test_d3_unpub|test_d5_garbage|FAIL|ERROR"`
Expected: D-1 FAILS (resolution returns None instead of the default — fallback not implemented). D-3/D-4/D-5 happen to pass already (no default branch yet → always None) — that's fine; D-1 + D-2 are the ones that must flip.

- [ ] **Step 3: Implement the 4th fallback branch**

In `src/controllers/ai_assistant.py`, replace the body of `_resolve_agent_template` (lines 755-768) with:

```python
    def _resolve_agent_template(self, project):
        """Resolve the agent_template for a project (prefer published).

        Order: project-bound published → project-bound any →
        project.agent_template_id → platform default (Settings
        ``default_agent_template_id``). The platform default is the LAST
        fallback so a project's own binding always wins (design 決策 A).
        """
        AgentTemplate = request.env['woow_paas_platform.agent_template'].sudo()
        template = AgentTemplate.search([
            ('project_ids', 'in', project.id),
            ('state', '=', 'published'),
        ], limit=1)
        if not template:
            template = AgentTemplate.search([
                ('project_ids', 'in', project.id),
            ], limit=1)
        if not template and project.agent_template_id:
            template = project.agent_template_id
        if not template:
            # ④ platform default from Settings (config_parameter). Last
            # fallback so a project's own binding wins. config_parameter has
            # no DB constraint, so re-validate is_public + published here — a
            # stale / unpublished / non-public / garbage value is treated as
            # "no default" (fail-loud via the no_template branch upstream).
            default_id = request.env['ir.config_parameter'].sudo().get_param(
                'woow_paas_platform.default_agent_template_id'
            )
            if default_id:
                try:
                    candidate = AgentTemplate.browse(int(default_id)).exists()
                except (ValueError, TypeError):
                    candidate = AgentTemplate.browse()  # empty recordset
                if candidate and candidate.is_public \
                        and candidate.state == 'published':
                    template = candidate
        return template or None
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags woow_paas_platform --stop-after-init 2>&1 | grep -iE "test_d1|test_d2|test_d3|test_d4|test_d5|FAIL|ERROR|passed"`
Expected: all `test_d1`…`test_d5b` PASS, no errors.

- [ ] **Step 5: Commit**

```bash
git add src/controllers/ai_assistant.py src/tests/test_default_agent_template.py
git commit -m "feat: fall back to platform default agent template in resolution"
```

---

## Task 3: Full module test run + PR

- [ ] **Step 1: Run the full module test suite**

Run: `./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable --test-tags woow_paas_platform --stop-after-init 2>&1 | tail -30`
Expected: no failures/errors introduced (existing agent_template / enablement tests still green; new `test_default_agent_template` green).

- [ ] **Step 2: Push and open PR to develop**

```bash
git push -u origin feat/default-agent-template
tea pr create --base develop --head feat/default-agent-template \
  --title "feat: 平台預設 agent template（Settings fallback）" \
  --description "新增平台級預設 agent template：沒有自綁 template 的 project 自動 fallback。Settings → AI Assistant Configuration 新增 Default Agent Template 下拉（config_parameter，與 default AI config/assistant 同 pattern）；_resolve_agent_template 加最後順位 fallback（project 綁定優先，resolution 端重驗 is_public+published，fail-loud）。不動 agent_template model / migration / seed / ir.rule。Spec: docs/archived/superpowers/specs/2026-06-01-default-agent-template-design.md"
```

---

## Self-Review

- **Spec coverage:** S-1 (Settings round-trip) → Task 1; D-1..D-5 (resolution + fail-loud + garbage) → Task 2 (D-5 split into D-5 garbage-string + D-5b deleted-id for the two防呆 paths in the spec's D-5 row); uninstall cleanup → Task 1 Step 6; view row → Task 1 Step 5. All spec sections mapped.
- **Placeholder scan:** none — every step has concrete code/commands.
- **Type consistency:** field name `woow_default_agent_template_id` and param key `woow_paas_platform.default_agent_template_id` identical across settings model, view, hooks, tests, and resolution. Method `_resolve_agent_template(project)` signature unchanged. Controller class `AIAssistantController`, module path `odoo.addons.woow_paas_platform.controllers.ai_assistant`.
- **Ambiguity:** D-4 note clarifies why the non-public template must bind an unrelated project (the `_check_visibility_consistency` constraint forbids a non-public non-draft template with no projects).
