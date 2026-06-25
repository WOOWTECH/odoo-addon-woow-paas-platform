"""Platform-default agent template must stay project-unbound (write-time guard).

Companion to test_default_agent_template.py D-6, which covers the *resolution*
side: a project-bound template is refused when served as the global default
(``_platform_default_agent_template`` fail-closed cross-tenant leak guard). The
problem is that guard is SILENT — it just makes the default fallback vanish, so
auto-enable / resolution quietly stops working cluster-wide with no error.

This module covers the *write* side: binding a project to the template currently
configured as the platform default must fail LOUD at write time, so the
tenant-neutral invariant of the seeded default cannot be broken by accident.

Background incident (stg, 2026-06-02): ``agent_template_default`` (id=2, the
configured platform default) had been bound to a project, which made
``_platform_default_agent_template`` reject it → readiness gate condition 3
stayed False → every newly created project silently failed to auto-enable
``use_agent_runtime``. See the seeded-default tenant-neutral invariant in
CLAUDE.md / docs/decisions/patterns.

  G-1  binding a project to the configured default template raises ValidationError
  G-2  binding a project to a non-default template is allowed
  G-3  with no default configured, binding any template is allowed
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged

_PARAM = 'woow_paas_platform.default_agent_template_id'


@tagged('post_install', '-at_install')
class TestDefaultAgentTemplateBindGuard(TransactionCase):
    """G-1..G-3: agent_template._check_not_platform_default_when_bound."""

    def setUp(self):
        super().setUp()
        # sudo seeding: the bind-time grantor-authority constraint bypasses for
        # superuser, so the only constraint under test here is the new
        # platform-default guard (which intentionally does NOT bypass superuser).
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()

    def _make_project(self, name='Bind Guard Project'):
        return self.env['project.project'].sudo().create({'name': name})

    def _make_template(self, name):
        return self.AgentTemplate.create({
            'name': name, 'is_public': True, 'state': 'published',
        })

    def test_g1_bind_project_to_platform_default_is_refused(self):
        default = self._make_template('Platform Default')
        self.ICP.set_param(_PARAM, str(default.id))
        project = self._make_project()
        with self.assertRaises(ValidationError) as cm:
            default.write({'project_ids': [(4, project.id)]})
        self.assertIn(
            'platform', str(cm.exception).lower(),
            "the guard must explain that the template is the platform default",
        )

    def test_g2_bind_project_to_non_default_template_is_allowed(self):
        default = self._make_template('Platform Default')
        other = self._make_template('Other Template')
        self.ICP.set_param(_PARAM, str(default.id))
        project = self._make_project()
        other.write({'project_ids': [(4, project.id)]})  # must not raise
        self.assertIn(
            project, other.project_ids,
            "binding a project to a non-default template must be allowed",
        )

    def test_g3_no_default_configured_allows_binding(self):
        # truly unset: remove any param row left by post_init auto-point
        self.ICP.search([('key', '=', _PARAM)]).unlink()
        tmpl = self._make_template('Some Template')
        project = self._make_project()
        tmpl.write({'project_ids': [(4, project.id)]})  # must not raise
        self.assertIn(
            project, tmpl.project_ids,
            "with no platform default set, binding must be unrestricted",
        )
