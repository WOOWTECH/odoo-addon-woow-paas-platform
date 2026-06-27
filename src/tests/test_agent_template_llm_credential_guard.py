"""F-LLM-3: a published, project-bound agent_template must carry a usable LLM
credential, else the agent pod silently echoes (no LLM Secret provisioned).

Companion to test_agent_template_default_bind_guard.py: that guard stops binding
the *platform default itself*; this one stops binding *any other* keyless
template that would shadow the default in ``_resolve_agent_template`` (leg 2) and
make every bound project's pod fall back to echo with no error surface.

  L-1  published + bound + no model_config_id            → ValidationError
  L-2  published + bound + model_config_id w/o api_key    → ValidationError
  L-3  published + bound + keyed model_config_id          → allowed
  L-4  published + UNbound + no credential                → allowed (system default)
  L-5  draft + bound + no credential                      → allowed (work in progress)
  L-6  draft→published WHILE bound, keyless               → ValidationError (publish trigger)
"""
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestAgentTemplateLlmCredentialGuard(TransactionCase):
    """agent_template._check_published_bound_has_llm_credential."""

    def setUp(self):
        super().setUp()
        # sudo: the bind-time grantor-authority constraint bypasses for
        # superuser, so the only constraint exercised here is the LLM-credential
        # guard (which intentionally does NOT bypass superuser).
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        self.AiConfig = self.env['ai.config'].sudo()

    def _make_project(self, name='LLM Guard Project'):
        return self.env['project.project'].sudo().create({'name': name})

    def _make_config(self, with_key=True, name='LLM Guard Config'):
        return self.AiConfig.create({
            'name': name,
            'type': 'chatgpt',
            'api_key': 'sk-test-llm-guard' if with_key else False,
            'model': 'gpt-4o-mini',
        })

    # ---- negative: must fail loud --------------------------------------- #
    def test_l1_published_bound_without_config_raises(self):
        tmpl = self.AgentTemplate.create({
            'name': 'Keyless No-Config', 'is_public': True, 'state': 'published',
        })
        project = self._make_project()
        with self.assertRaises(ValidationError) as cm:
            tmpl.write({'project_ids': [(4, project.id)]})
        self.assertIn('echo', str(cm.exception).lower())

    def test_l2_published_bound_with_keyless_config_raises(self):
        config = self._make_config(with_key=False)
        tmpl = self.AgentTemplate.create({
            'name': 'Keyless Config', 'is_public': True, 'state': 'published',
            'model_config_id': config.id,
        })
        project = self._make_project()
        with self.assertRaises(ValidationError):
            tmpl.write({'project_ids': [(4, project.id)]})

    def test_l6_publish_while_bound_keyless_raises(self):
        # draft+bound is allowed; flipping to published while still keyless +
        # bound must fail at the publish write (state is a constraint trigger).
        tmpl = self.AgentTemplate.create({
            'name': 'Draft Then Publish', 'is_public': True, 'state': 'draft',
        })
        project = self._make_project()
        tmpl.write({'project_ids': [(4, project.id)]})  # draft+bound: OK
        with self.assertRaises(ValidationError):
            tmpl.write({'state': 'published'})

    # ---- positive: must NOT raise --------------------------------------- #
    def test_l3_published_bound_with_keyed_config_ok(self):
        config = self._make_config(with_key=True)
        tmpl = self.AgentTemplate.create({
            'name': 'Keyed Config', 'is_public': True, 'state': 'published',
            'model_config_id': config.id,
        })
        project = self._make_project()
        tmpl.write({'project_ids': [(4, project.id)]})  # must not raise
        self.assertIn(project, tmpl.project_ids)

    def test_l4_published_unbound_without_config_ok(self):
        # unbound system / default template: echo-fallback is the documented
        # behaviour, no shadowing of a tenant — guard must stay out.
        tmpl = self.AgentTemplate.create({
            'name': 'Unbound System', 'is_public': True, 'state': 'published',
        })
        self.assertEqual(tmpl.state, 'published')
        self.assertFalse(tmpl.project_ids)

    def test_l5_draft_bound_without_config_ok(self):
        # work-in-progress: bound but not yet published — admin may still be
        # configuring; guard only enforces at publish-while-bound.
        tmpl = self.AgentTemplate.create({
            'name': 'Draft Bound', 'is_public': True, 'state': 'draft',
        })
        project = self._make_project()
        tmpl.write({'project_ids': [(4, project.id)]})  # must not raise
        self.assertIn(project, tmpl.project_ids)
