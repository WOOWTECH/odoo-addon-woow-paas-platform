"""Auto-enable agent runtime on project creation.

決策來源：docs/plans/2026-Q2/2026-06-01-auto-enable-agent-runtime.md
Covers:
  T1   res.config.settings.woow_default_enable_agent_runtime round-trips through
       ir.config_parameter 'woow_paas_platform.default_enable_agent_runtime'.
  R-*  _agent_runtime_autoenable_ready fail-safe gate.
  C-*  _create_project auto-enables only when ready.
  B-*  backfill_existing_projects_use_agent_runtime one-time migration helper.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

_TOGGLE = 'woow_paas_platform.default_enable_agent_runtime'
_URL = 'woow_paas_platform.operator_url'
_KEY = 'woow_paas_platform.operator_api_key'
_DEFTMPL = 'woow_paas_platform.default_agent_template_id'


@tagged('post_install', '-at_install')
class TestAutoEnableToggleSetting(TransactionCase):
    """T1: the Settings Boolean persists to ir.config_parameter."""

    def test_t1_toggle_round_trips_to_config_parameter(self):
        settings = self.env['res.config.settings'].sudo().create({
            'woow_default_enable_agent_runtime': True,
        })
        settings.execute()
        stored = self.env['ir.config_parameter'].sudo().get_param(_TOGGLE)
        self.assertEqual(
            stored, 'True',
            "Settings field must persist True to the '%s' config_parameter."
            % _TOGGLE,
        )


@tagged('post_install', '-at_install')
class TestAutoEnableUninstallCleanup(TransactionCase):
    """The new config_parameter must be in hooks._CONFIG_PARAMETERS so an
    addon uninstall clears it (CLAUDE.md: config_parameter is not auto-cleaned).
    """

    def test_toggle_param_in_uninstall_cleanup_list(self):
        from odoo.addons.woow_paas_platform import hooks
        self.assertIn(
            _TOGGLE, hooks._CONFIG_PARAMETERS,
            "default_enable_agent_runtime must be in _CONFIG_PARAMETERS so "
            "uninstall removes it.",
        )


@tagged('post_install', '-at_install')
class TestAutoEnableReadiness(TransactionCase):
    """R-*: _agent_runtime_autoenable_ready — fail-safe gate matrix."""

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self.mod = ai_assistant
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()

    def _make_valid_default(self):
        tmpl = self.AgentTemplate.create({
            'name': 'Platform Default', 'is_public': True, 'state': 'published',
        })
        self.ICP.set_param(_DEFTMPL, str(tmpl.id))
        return tmpl

    def _set_ready(self):
        """All three readiness conditions satisfied."""
        self.ICP.set_param(_TOGGLE, 'True')
        self.ICP.set_param(_URL, 'http://paas-operator:8000')
        self.ICP.set_param(_KEY, 'secret-key')
        self._make_valid_default()

    def test_r1_all_conditions_met_is_ready(self):
        self._set_ready()
        self.assertTrue(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r2_toggle_off_not_ready(self):
        self._set_ready()
        self.ICP.set_param(_TOGGLE, 'False')
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r3_operator_url_missing_not_ready(self):
        self._set_ready()
        self.ICP.set_param(_URL, '')
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r4_operator_key_missing_not_ready(self):
        self._set_ready()
        self.ICP.set_param(_KEY, '')
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r5_no_default_template_not_ready(self):
        self._set_ready()
        self.ICP.set_param(_DEFTMPL, '')
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))

    def test_r6_invalid_default_template_not_ready(self):
        # draft template = not a valid platform default
        self.ICP.set_param(_TOGGLE, 'True')
        self.ICP.set_param(_URL, 'http://paas-operator:8000')
        self.ICP.set_param(_KEY, 'secret-key')
        draft = self.AgentTemplate.create({
            'name': 'Draft', 'is_public': True, 'state': 'draft',
        })
        self.ICP.set_param(_DEFTMPL, str(draft.id))
        self.assertFalse(self.mod._agent_runtime_autoenable_ready(self.env))


@tagged('post_install', '-at_install')
class TestAutoEnableOnCreate(TransactionCase):
    """C-*: _create_project flips use_agent_runtime only when ready."""

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self.mod = ai_assistant
        self.controller = ai_assistant.AiAssistantController()
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        # Fail-isolation: the sandbox full-module DB seeds operator_url/api_key/
        # default_agent_template_id globally (create.sh), so this test's assumed
        # "unconfigured" baseline does not hold unless we clear them. Tests that
        # need a ready platform call _set_ready() (F-ARC-1 / gitea #131).
        for _key in (_TOGGLE, _URL, _KEY, _DEFTMPL):
            self.ICP.set_param(_key, '')

    def _set_ready(self):
        self.ICP.set_param(_TOGGLE, 'True')
        self.ICP.set_param(_URL, 'http://paas-operator:8000')
        self.ICP.set_param(_KEY, 'secret-key')
        tmpl = self.AgentTemplate.create({
            'name': 'Platform Default', 'is_public': True, 'state': 'published',
        })
        self.ICP.set_param(_DEFTMPL, str(tmpl.id))

    def _create(self, name):
        test_env = self.env

        class _Req:
            env = test_env

        with patch.object(self.mod, 'request', _Req()):
            return self.controller._create_project({'name': name})

    def test_c1_ready_platform_auto_enables(self):
        self._set_ready()
        res = self._create('c1-ready')
        self.assertTrue(res['success'])
        project = self.env['project.project'].browse(res['data']['id'])
        self.assertTrue(
            project.use_agent_runtime,
            "On a ready platform a newly created project must auto-enable "
            "use_agent_runtime.",
        )

    def test_c2_toggle_off_does_not_enable(self):
        self._set_ready()
        self.ICP.set_param(_TOGGLE, 'False')
        res = self._create('c2-toggleoff')
        project = self.env['project.project'].browse(res['data']['id'])
        self.assertFalse(
            project.use_agent_runtime,
            "Toggle off → no auto-enable even if operator + default template "
            "are configured.",
        )

    def test_c3_not_ready_does_not_enable(self):
        # toggle on but operator unconfigured → fail-safe stays off
        self.ICP.set_param(_TOGGLE, 'True')
        res = self._create('c3-notready')
        project = self.env['project.project'].browse(res['data']['id'])
        self.assertFalse(
            project.use_agent_runtime,
            "Unready platform must NOT auto-enable (would guarantee "
            "spawn_failed/no_template).",
        )


@tagged('post_install', '-at_install')
class TestBackfillExistingProjects(TransactionCase):
    """B-*: backfill_existing_projects_use_agent_runtime (migration helper)."""

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self.mod = ai_assistant
        self.ICP = self.env['ir.config_parameter'].sudo()
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        # Fail-isolation against sandbox seed (see TestAutoEnableOnCreate.setUp):
        # b2 asserts a noop on an unconfigured platform; b1 calls
        # _make_ready_platform() to set up readiness (F-ARC-1 / gitea #131).
        for _key in (_TOGGLE, _URL, _KEY, _DEFTMPL):
            self.ICP.set_param(_key, '')

    def _make_ready_platform(self):
        # NOTE: backfill is toggle-INDEPENDENT (toggle is off at upgrade time);
        # readiness = operator configured + valid default template.
        self.ICP.set_param(_URL, 'http://paas-operator:8000')
        self.ICP.set_param(_KEY, 'secret-key')
        tmpl = self.AgentTemplate.create({
            'name': 'Platform Default', 'is_public': True, 'state': 'published',
        })
        self.ICP.set_param(_DEFTMPL, str(tmpl.id))

    def test_b1_ready_flips_existing_and_sets_toggle(self):
        self._make_ready_platform()
        proj = self.env['project.project'].sudo().create(
            {'name': 'b1-existing'})
        self.assertFalse(proj.use_agent_runtime)
        flipped = self.mod.backfill_existing_projects_use_agent_runtime(self.env)
        self.assertGreaterEqual(flipped, 1)
        self.assertTrue(
            proj.use_agent_runtime,
            "Backfill on a ready platform must flip existing projects.",
        )
        self.assertEqual(
            self.ICP.get_param(_TOGGLE), 'True',
            "Backfill must also turn the new-project default toggle ON so the "
            "create path stays consistent post-migration.",
        )

    def test_b2_not_ready_is_noop(self):
        # operator unconfigured → backfill must do nothing
        proj = self.env['project.project'].sudo().create(
            {'name': 'b2-existing'})
        flipped = self.mod.backfill_existing_projects_use_agent_runtime(self.env)
        self.assertEqual(flipped, 0)
        self.assertFalse(proj.use_agent_runtime)
