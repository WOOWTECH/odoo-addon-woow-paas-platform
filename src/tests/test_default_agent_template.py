"""Platform default agent template — Settings field + resolution fallback.

Spec: docs/superpowers/specs/2026-06-01-default-agent-template-design.md
Plan: docs/superpowers/plans/2026-06-01-default-agent-template.md

Covers:
  S-1  res.config.settings.woow_default_agent_template_id round-trips through
       ir.config_parameter 'woow_paas_platform.default_agent_template_id'.
  D-1  project with no binding + valid default → resolution returns default.
  D-2  project with its own published template + default set → returns own.
  D-3  default points at an unpublished template → treated as unset (None).
  D-4  default points at a non-public template → treated as unset (None).
  D-5  config_parameter holds a stale/garbage value → no crash, returns None.
  D-6  project-bound public template as default → refused (cross-tenant guard).

Fixture note: agent_template state is created/bound through ``.sudo()`` because
``agent_template._check_project_ids_grantor_authority`` requires the acting user
to hold owner/admin role on every bound project (and bypasses for superuser).
These tests exercise the *resolution* logic, not the bind-time ACL, so seeding
state as superuser is correct. Resolution itself is still driven through the
real ``request.env`` (non-sudo) — the controller method does its own ``.sudo()``.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

_PARAM = 'woow_paas_platform.default_agent_template_id'


@tagged('post_install', '-at_install')
class TestDefaultAgentTemplateSettings(TransactionCase):
    """S-1: the Settings Many2one persists to ir.config_parameter."""

    def test_s1_settings_field_round_trips_to_config_parameter(self):
        tmpl = self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': 'Default Woow Template',
            'is_public': True,
            'state': 'published',
        })
        settings = self.env['res.config.settings'].sudo().create({
            'woow_default_agent_template_id': tmpl.id,
        })
        settings.execute()
        stored = self.env['ir.config_parameter'].sudo().get_param(_PARAM)
        self.assertEqual(
            str(stored), str(tmpl.id),
            "Settings field must persist the template id to the "
            "'%s' config_parameter." % _PARAM,
        )


@tagged('post_install', '-at_install')
class TestDefaultAgentTemplateResolution(TransactionCase):
    """D-1..D-6: _resolve_agent_template platform-default fallback.

    The method lives on the controller and reads ``request.env``; we
    instantiate the controller and patch its module-level ``request`` so
    ``request.env`` is the test env (the method calls ``.sudo()`` itself).
    """

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self._ai_assistant = ai_assistant
        self.controller = ai_assistant.AiAssistantController()
        # sudo for fixture seeding (bypasses grantor-authority constraint on
        # project_ids — we are setting up state, not testing the bind ACL).
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()

    def _resolve(self, project):
        """Call _resolve_agent_template with request.env bound to test env."""
        test_env = self.env

        class _Req:
            env = test_env

        with patch.object(self._ai_assistant, 'request', _Req()):
            return self.controller._resolve_agent_template(project)

    def _set_default(self, template_id):
        self.ICP.set_param(_PARAM, str(template_id))

    def _make_project(self, name='Resolve Test Project'):
        return self.env['project.project'].sudo().create({'name': name})

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

    def test_d6_project_bound_public_default_is_refused(self):
        """SECURITY: a public+published template that is bound to a project
        must NOT be served as the global default — otherwise that tenant's
        user-scope MCP/skill bindings would broadcast cluster-wide into other
        tenants' pods (the snapshot builders do not filter by project). Such a
        default is treated as unqualified (None, fail-closed)."""
        bound_project = self._make_project('Owner Tenant Project')
        tmpl = self.AgentTemplate.create({
            'name': 'Tenant-Bound Public Template',
            'is_public': True, 'state': 'published',
            'project_ids': [(4, bound_project.id)],
        })
        self._set_default(tmpl.id)
        consumer = self._make_project('Consumer Project')
        self.assertIsNone(
            self._resolve(consumer),
            "A project-bound template must be refused as the global default "
            "even when public+published (cross-tenant MCP/skill leak guard).",
        )

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

    def test_post_init_pointed_default_at_seed_on_install(self):
        """S-5 (install wiring invariant): on a real install post_init_hook
        auto-pointed the LIVE default_agent_template_id config_parameter at the
        seeded template. Unlike S-2/S-3 (which unlink/set the param and call the
        helper directly), this reads the unmodified post-install value, so it
        guards the glue S-2..S-4 bypass: that post_init_hook actually calls the
        helper AND the data file loads before the hook (env.ref resolves). If
        this goes red while S-2/S-3 stay green, the helper call was dropped from
        post_init_hook or the seed no longer loads — i.e. the out-of-the-box
        contract silently regressed. TransactionCase rollback isolates it from
        sibling tests that mutate the same param.
        """
        seed = self.env.ref('woow_paas_platform.agent_template_default')
        stored = self.env['ir.config_parameter'].sudo().get_param(_PARAM)
        self.assertEqual(
            str(stored), str(seed.id),
            "post_init_hook must auto-point the platform default at the seed on "
            "a fresh install (out-of-the-box contract).",
        )
