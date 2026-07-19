# Seed 平台預設 Agent Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Seed a real, published, tenant-neutral platform-default `agent_template` so a fresh environment is usable out of the box, and auto-point the platform-default config_parameter at it on install (without clobbering an admin's choice).

**Architecture:** A new XML data record `agent_template_default` (public + published + unbound, `model_config_id` → existing `ai_config_woowbot`) ships with the module. A new idempotent helper `_ensure_default_agent_template(env)` in `hooks.py`, called from the existing `post_init_hook`, points `ir.config_parameter['woow_paas_platform.default_agent_template_id']` at the seed only when that parameter is currently empty. The #112 resolver fallback (already shipped) then serves it.

**Tech Stack:** Odoo 18 addon (XML data records, Python init hooks, `TransactionCase` tests).

**Spec:** `docs/decisions/patterns/seed-agent-template-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/data/agent_template_default.xml` | **New.** Declares the single seed record `agent_template_default`. |
| `src/__manifest__.py` | Register the new data file (after `ai_assistant_data.xml` so the `ai_config_woowbot` ref resolves) + bump version. |
| `src/hooks.py` | New `_ensure_default_agent_template(env)` helper; call it from existing `post_init_hook`. |
| `src/tests/test_default_agent_template.py` | Append seed-group tests (spec S-1..S-4). |

Nothing else changes: `agent_template` model, ir.rule, ir.model.access, migrations, and the existing `agent_template_demo.xml` are all untouched. The `default_agent_template_id` config_parameter is already in `hooks._CONFIG_PARAMETERS` (uninstall cleanup covered); seed records are module data (removed on uninstall by `ir.model.data`).

## How to run the tests

These are Odoo `TransactionCase` tests tagged `post_install`. The seed-record test needs the new data file loaded, so always update the module (`-u`) with `--test-enable`.

**Canonical (Odoo CLI, from the odoo-server root):**
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSeededDefaultAgentTemplate \
  --stop-after-init 2>&1 | grep -iE "FAIL|ERROR|Ran |tests? in|test_"
```
Swap the `:Class` suffix per task (or drop it for the whole module). For a single method: `:TestSeededDefaultAgentTemplate.test_seed_record_is_resolver_qualified`.

**k8s-dev-sandbox alternative** (per CLAUDE.md, if running inside a sandbox for this branch — it does `-u` + `--test-enable` internally):
```bash
./.claude/skills/k8s-dev-sandbox/scripts/test.sh <sandbox-name> --module woow_paas_platform
```

> Note: `post_init_hook` runs on every `-u`. After Task 2 it will auto-point the param **only if unset**; tests set/own their own param values and `TransactionCase` rolls back per test, so this does not interfere.

---

## Task 1: Seed the platform-default agent template

**Files:**
- Create: `src/data/agent_template_default.xml`
- Modify: `src/__manifest__.py` (data list after line 50; version line 3)
- Test: `src/tests/test_default_agent_template.py`

- [ ] **Step 1: Write the failing test**

Append a new test class to `src/tests/test_default_agent_template.py` (after the existing `TestDefaultAgentTemplateResolution` class). This is spec **S-1** — the seed must meet the resolver's exact qualification AND the tenant-neutral safety line:

```python
@tagged('post_install', '-at_install')
class TestSeededDefaultAgentTemplate(TransactionCase):
    """Seed group (spec S-1..S-3): the shipped agent_template_default record
    and the post_init auto-point helper."""

    def test_seed_record_is_resolver_qualified(self):
        """S-1: agent_template_default exists and is public + published +
        unbound + bound to ai_config_woowbot, with NO user-scope skill/mcp
        (tenant-neutral — safe to broadcast as the cluster-wide default)."""
        tmpl = self.env.ref(
            'woow_paas_platform.agent_template_default',
            raise_if_not_found=False,
        )
        self.assertTrue(tmpl, "seed record agent_template_default must exist")
        self.assertTrue(tmpl.is_public, "seed must be public")
        self.assertEqual(tmpl.state, 'published', "seed must be published")
        self.assertFalse(tmpl.project_ids, "seed must be unbound (tenant-neutral)")
        self.assertFalse(tmpl.skill_ids, "seed must bind no user-scope skills")
        self.assertFalse(tmpl.mcp_server_ids, "seed must bind no user-scope mcp")
        self.assertEqual(
            tmpl.model_config_id,
            self.env.ref('woow_paas_platform.ai_config_woowbot'),
            "seed must reuse the existing WoowBot ai.config",
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSeededDefaultAgentTemplate.test_seed_record_is_resolver_qualified \
  --stop-after-init 2>&1 | grep -iE "FAIL|ERROR|AssertionError|Ran "
```
Expected: FAIL — `AssertionError: seed record agent_template_default must exist` (the xmlid does not resolve yet).

- [ ] **Step 3: Create the seed data file**

Create `src/data/agent_template_default.xml`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<odoo>
    <!-- SEED.2: the platform-default agent_template — a real, published,
         tenant-neutral system template that ships with the module so a fresh
         environment is usable out of the box.
         - is_public=True + state='published' + NO project_ids → meets the
           resolver's qualification (public + published + unbound).
         - NO skill_ids / mcp_server_ids → tenant-neutral: broadcasting it via
           .sudo() to every project that lacks its own template leaks no tenant
           user-scope binding (system-scope skills auto-load in the pod anyway).
         - model_config_id → ai_config_woowbot (shared placeholder ai.config;
           admin supplies the real api_key there, on a group_system field).
         - noupdate="1": admin curation of system_prompt survives module update.
         - xmlid is a frozen contract: woow_paas_platform.agent_template_default.
         Distinct from agent_template_demo (the inert draft placeholder), which
         is left untouched. -->
    <data noupdate="1">
        <record id="agent_template_default" model="woow_paas_platform.agent_template">
            <field name="name">Default Agent Template</field>
            <field name="title">Woow Assistant</field>
            <field name="description">Platform-default agent-runtime template. Tenant-neutral (unbound, no user-scope skills/mcp) so it is safe as the cluster-wide fallback for any project without its own template. Admin supplies the real LLM api_key on the bound ai.config (WoowBot Config).</field>
            <field name="system_prompt">You are the Woow PaaS platform assistant, running inside a project's agent runtime. Help the user operate their workspace: answer questions about the platform, run the skills available to you, and assist with their tasks. Be concise, professional, and supportive. Act only within the scope of the current project. When you are unsure or lack a capability, say so honestly rather than guessing or fabricating an answer.</field>
            <field name="is_public" eval="True"/>
            <field name="state">published</field>
            <field name="model_config_id" ref="ai_config_woowbot"/>
        </record>
    </data>
</odoo>
```

- [ ] **Step 4: Register the data file in the manifest**

In `src/__manifest__.py`, the data list currently has (around line 50):
```python
        "data/agent_session_cron.xml",
        "data/agent_template_demo.xml",
        "data/demo_system_skill.xml",
```
Insert the new file immediately after `agent_template_demo.xml`:
```python
        "data/agent_session_cron.xml",
        "data/agent_template_demo.xml",
        "data/agent_template_default.xml",
        "data/demo_system_skill.xml",
```
(Order matters: it must come after `data/ai_assistant_data.xml` at line 29, which seeds `ai_config_woowbot` — it does, so the `ref="ai_config_woowbot"` resolves.)

- [ ] **Step 5: Bump the manifest version**

In `src/__manifest__.py` line 3, change:
```python
    "version": "18.0.1.0.29",
```
to:
```python
    "version": "18.0.1.0.30",
```

- [ ] **Step 6: Run the test to verify it passes**

Run:
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSeededDefaultAgentTemplate.test_seed_record_is_resolver_qualified \
  --stop-after-init 2>&1 | grep -iE "FAIL|ERROR|Ran "
```
Expected: PASS (`Ran 1 test ... OK`). The `-u` loads the new data file, so the xmlid resolves and all assertions hold. If you see a `ValidationError` at load time, the seed violates a constraint — re-check `is_public=True` (required so `_check_visibility_consistency` is skipped for an unbound published record).

- [ ] **Step 7: Commit**

```bash
git add src/data/agent_template_default.xml src/__manifest__.py src/tests/test_default_agent_template.py
git commit -m "feat: seed 平台預設 agent template (public+published+unbound)"
```

---

## Task 2: Auto-point the platform default on install (idempotent, no clobber)

**Files:**
- Modify: `src/hooks.py` (add helper; call from `post_init_hook` at line ~142-158)
- Test: `src/tests/test_default_agent_template.py` (same `TestSeededDefaultAgentTemplate` class)

- [ ] **Step 1: Write the failing tests**

Add two methods to `TestSeededDefaultAgentTemplate` (spec **S-2** and **S-3**):

```python
    def test_ensure_default_sets_param_when_unset(self):
        """S-2: with the config_parameter unset, the helper points it at the
        seeded template."""
        from odoo.addons.woow_paas_platform.hooks import (
            _ensure_default_agent_template,
        )
        # truly unset: remove any existing param row
        self.env['ir.config_parameter'].sudo().search(
            [('key', '=', _PARAM)]
        ).unlink()
        _ensure_default_agent_template(self.env)
        tmpl = self.env.ref('woow_paas_platform.agent_template_default')
        stored = self.env['ir.config_parameter'].sudo().get_param(_PARAM)
        self.assertEqual(
            str(stored), str(tmpl.id),
            "helper must point the unset default at the seeded template",
        )

    def test_ensure_default_does_not_clobber_existing(self):
        """S-3: with the config_parameter already set, the helper leaves it
        untouched (respects an admin's manual choice)."""
        from odoo.addons.woow_paas_platform.hooks import (
            _ensure_default_agent_template,
        )
        other = self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': 'Admin Picked Template',
            'is_public': True, 'state': 'published',
        })
        self.env['ir.config_parameter'].sudo().set_param(_PARAM, str(other.id))
        _ensure_default_agent_template(self.env)
        stored = self.env['ir.config_parameter'].sudo().get_param(_PARAM)
        self.assertEqual(
            str(stored), str(other.id),
            "helper must not overwrite an already-set default",
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSeededDefaultAgentTemplate.test_ensure_default_sets_param_when_unset \
  --stop-after-init 2>&1 | grep -iE "FAIL|ERROR|ImportError|Ran "
```
Expected: FAIL/ERROR — `ImportError: cannot import name '_ensure_default_agent_template'` (helper does not exist yet).

- [ ] **Step 3: Add the helper to hooks.py**

In `src/hooks.py`, add this function immediately above `post_init_hook` (after the `_CONFIG_PARAMETERS` list, ~line 140):

```python
def _ensure_default_agent_template(env):
    """Point the platform-default agent template at the seeded system template
    on first install, WITHOUT clobbering an admin's existing choice.

    Idempotent: only sets the config_parameter when it is currently empty, so
    re-running -u (post_init fires on every update) never overrides a value the
    admin picked in Settings. The seeded template is tenant-neutral (public,
    published, unbound, no user-scope skills/mcp) so broadcasting it as the
    cluster-wide default is safe.
    """
    ICP = env['ir.config_parameter'].sudo()
    key = 'woow_paas_platform.default_agent_template_id'
    if ICP.get_param(key):
        return  # admin already chose (or a prior install set it) — don't clobber
    template = env.ref(
        'woow_paas_platform.agent_template_default', raise_if_not_found=False,
    )
    if template:
        ICP.set_param(key, str(template.id))
        _logger.info(
            "Auto-pointed platform default agent template -> %s (id=%s)",
            template.name, template.id,
        )
```

- [ ] **Step 4: Call the helper from post_init_hook (at the TOP, not the end)**

⚠️ The existing `post_init_hook` has TWO early `return`s (`if not company: return` and `if company.sh_journal_id: return`). On an existing/updated environment the journal is usually already set, so the function returns early — appending the call at the end would silently skip it. Put the call **first**, so it always runs regardless of journal state.

In `src/hooks.py`, the existing `post_init_hook` begins:
```python
def post_init_hook(env):
    """Ensure subscription journal is configured on the main company."""
    company = env.ref('base.main_company', raise_if_not_found=False)
```
Update the docstring and insert the call as the first statement of the body:
```python
def post_init_hook(env):
    """Auto-point the platform default agent template, then ensure the
    subscription journal is configured on the main company."""
    _ensure_default_agent_template(env)
    company = env.ref('base.main_company', raise_if_not_found=False)
```
(The two concerns are independent; ordering the agent-template call first guarantees it runs even when the journal logic early-returns.)

- [ ] **Step 5: Run the tests to verify they pass**

Run:
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestSeededDefaultAgentTemplate \
  --stop-after-init 2>&1 | grep -iE "FAIL|ERROR|Ran "
```
Expected: PASS (`Ran 3 tests ... OK` — S-1 + S-2 + S-3).

- [ ] **Step 6: Commit**

```bash
git add src/hooks.py src/tests/test_default_agent_template.py
git commit -m "feat: post_init auto-point platform default agent template (no clobber)"
```

---

## Task 3: End-to-end guard — seeded default resolves for an unbound project

**Files:**
- Test: `src/tests/test_default_agent_template.py` (existing `TestDefaultAgentTemplateResolution` class — reuses its `_resolve` / `_set_default` / `_make_project` helpers)

This is spec **S-4**: a regression guard proving the seed → resolver wiring stays correct. It needs no new production code (Task 1's seed + the #112 resolver already satisfy it); it locks the contract so a future change to the seed (e.g. accidentally binding a project) is caught.

- [ ] **Step 1: Write the test**

Add this method to the existing `TestDefaultAgentTemplateResolution` class:

```python
    def test_seed_default_resolves_end_to_end(self):
        """S-4: the shipped agent_template_default, when set as the platform
        default, is returned by _resolve_agent_template for an unbound project
        (seed meets the resolver's public+published+unbound qualification)."""
        seed = self.env.ref('woow_paas_platform.agent_template_default')
        self._set_default(seed.id)
        project = self._make_project('Seed Resolve Project')
        self.assertEqual(
            self._resolve(project), seed,
            "an unbound project must fall back to the seeded platform default",
        )
```

- [ ] **Step 2: Run the test**

Run:
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform:TestDefaultAgentTemplateResolution.test_seed_default_resolves_end_to_end \
  --stop-after-init 2>&1 | grep -iE "FAIL|ERROR|Ran "
```
Expected: PASS (`Ran 1 test ... OK`). It passes without new code — this is a characterization/guard test.

- [ ] **Step 3: Run the full module test suite (regression)**

Run the whole module's tests to confirm nothing regressed (especially the existing D-1..D-6 and S-1 settings test):
```bash
./odoo-bin -c odoo.conf -u woow_paas_platform --test-enable \
  --test-tags /woow_paas_platform \
  --stop-after-init 2>&1 | grep -iE "FAIL|ERROR|Ran "
```
Expected: PASS — no failures/errors across the module.

- [ ] **Step 4: Commit**

```bash
git add src/tests/test_default_agent_template.py
git commit -m "test: guard seeded default agent template resolves end-to-end"
```

---

## Done criteria

- [ ] `src/data/agent_template_default.xml` ships a public + published + unbound template bound to `ai_config_woowbot`.
- [ ] `_ensure_default_agent_template` points the default config_parameter at the seed on install only when unset; never clobbers an admin's choice.
- [ ] Tests S-1..S-4 pass; full `woow_paas_platform` suite stays green.
- [ ] Manifest version bumped to `18.0.1.0.30`; new data file registered after `ai_assistant_data.xml`.
- [ ] `agent_template` model / ir.rule / ir.model.access / migrations / `agent_template_demo.xml` untouched.
