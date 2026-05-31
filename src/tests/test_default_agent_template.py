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
        self.controller = ai_assistant.AiAssistantController()
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
