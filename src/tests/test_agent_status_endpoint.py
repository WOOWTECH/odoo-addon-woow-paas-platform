"""api_ai_agent_status — platform Agent runtime status endpoint (gitea #126).

The /api/ai/agent-status endpoint backs the Projects-overview "Agent" status
card. It is config-only: no operator /health ping, just fast DB/config reads
computed with the SAME primitives the auto-enable gate uses
(``_agent_runtime_autoenable_ready``), so the card stays in lock-step with
whether a newly created project would actually auto-enable agent runtime
(two-layer-acl-lockstep discipline).

Covers (issue #126 reason precedence, first failing wins, '' when ready):
  AS-1  ready=True — all four conditions met.
  AS-2  toggle off                  → reason 'Agent runtime 未啟用'.
  AS-3  no valid platform default   → reason '無預設 Agent Template'.
  AS-4  default template, no api_key → reason 'Template 未設 API key'.
  AS-5  operator unconfigured       → reason 'Operator 未設定'.

Harness note: like test_default_agent_template, the endpoint reads
``request.env``; we instantiate the controller and patch its module-level
``request`` so ``request.env`` is the test env (the endpoint calls ``.sudo()``
itself where it needs to). Fixtures seed agent_template via ``.sudo()`` to
bypass the bind-time grantor-authority constraint — we are setting up state,
not exercising that ACL.
"""
from unittest.mock import patch

from odoo.tests.common import TransactionCase, tagged

_TOGGLE = 'woow_paas_platform.default_enable_agent_runtime'
_URL = 'woow_paas_platform.operator_url'
_KEY = 'woow_paas_platform.operator_api_key'
_DEFTMPL = 'woow_paas_platform.default_agent_template_id'


@tagged('post_install', '-at_install')
class TestAgentStatusEndpoint(TransactionCase):
    """AS-1..AS-5: api_ai_agent_status data shape + reason precedence."""

    def setUp(self):
        super().setUp()
        from odoo.addons.woow_paas_platform.controllers import ai_assistant
        self._ai_assistant = ai_assistant
        self.controller = ai_assistant.AiAssistantController()
        # sudo for fixture seeding (bypasses grantor-authority on project_ids
        # and lets us seed a tenant-neutral platform default cleanly).
        self.AgentTemplate = self.env['woow_paas_platform.agent_template'].sudo()
        self.AiConfig = self.env['ai.config'].sudo()
        self.ICP = self.env['ir.config_parameter'].sudo()

    # -- helpers ---------------------------------------------------------

    def _call(self):
        """Call api_ai_agent_status with request.env bound to the test env."""
        test_env = self.env

        class _Req:
            env = test_env

        with patch.object(self._ai_assistant, 'request', _Req()):
            return self.controller.api_ai_agent_status()

    def _make_config(self, with_key=True):
        return self.AiConfig.create({
            'name': 'Agent Status Test Config',
            'type': 'chatgpt',
            'api_key': 'sk-test-agent-status' if with_key else False,
            'model': 'gpt-4o-mini',
        })

    def _make_default_template(self, with_key=True):
        """A valid tenant-neutral platform default (public, published, unbound)
        bound to an ai.config that does/doesn't carry an API key, and point the
        platform default config_parameter at it."""
        tmpl = self.AgentTemplate.create({
            'name': 'Platform Default Agent',
            'is_public': True,
            'state': 'published',
            'model_config_id': self._make_config(with_key=with_key).id,
        })
        self.ICP.set_param(_DEFTMPL, str(tmpl.id))
        return tmpl

    def _set_operator(self, configured=True):
        self.ICP.set_param(_URL, 'http://paas-operator:8000' if configured else '')
        self.ICP.set_param(_KEY, 'secret-key' if configured else '')

    def _set_enabled(self, enabled=True):
        self.ICP.set_param(_TOGGLE, 'True' if enabled else 'False')

    def _set_ready(self):
        """All four readiness conditions satisfied."""
        self._set_enabled(True)
        self._make_default_template(with_key=True)
        self._set_operator(configured=True)

    # -- AS-1: ready -----------------------------------------------------

    def test_as1_all_conditions_met_is_ready(self):
        self._set_ready()
        res = self._call()
        self.assertTrue(res['success'])
        data = res['data']
        self.assertTrue(data['ready'], "all four conditions met must be ready")
        self.assertTrue(data['enabled'])
        self.assertTrue(data['operator_configured'])
        self.assertEqual(data['template_name'], 'Platform Default Agent')
        self.assertEqual(data['model_name'], 'gpt-4o-mini')
        self.assertEqual(data['reason'], '', "ready state must carry no reason")

    # -- AS-2: disabled --------------------------------------------------

    def test_as2_toggle_off_reason_not_enabled(self):
        self._set_ready()
        self._set_enabled(False)
        res = self._call()
        self.assertTrue(res['success'])
        data = res['data']
        self.assertFalse(data['ready'])
        self.assertFalse(data['enabled'])
        self.assertEqual(
            data['reason'], 'Agent runtime 未啟用',
            "toggle off wins precedence even when everything else is configured",
        )

    # -- AS-3: no platform default template ------------------------------

    def test_as3_no_default_template_reason(self):
        self._set_enabled(True)
        self._set_operator(configured=True)
        # no default template configured at all
        self.ICP.set_param(_DEFTMPL, '')
        res = self._call()
        self.assertTrue(res['success'])
        data = res['data']
        self.assertFalse(data['ready'])
        self.assertTrue(data['enabled'])
        self.assertEqual(data['template_name'], '')
        self.assertEqual(data['model_name'], '')
        self.assertEqual(data['reason'], '無預設 Agent Template')

    # -- AS-4: template present but ai.config has no api_key -------------

    def test_as4_template_without_api_key_reason(self):
        self._set_enabled(True)
        self._set_operator(configured=True)
        self._make_default_template(with_key=False)
        res = self._call()
        self.assertTrue(res['success'])
        data = res['data']
        self.assertFalse(data['ready'])
        self.assertTrue(data['enabled'])
        # template is still a valid platform default → its name/model surface
        self.assertEqual(data['template_name'], 'Platform Default Agent')
        self.assertEqual(data['model_name'], 'gpt-4o-mini')
        self.assertEqual(data['reason'], 'Template 未設 API key')

    # -- AS-5: operator unconfigured -------------------------------------

    def test_as5_operator_unconfigured_reason(self):
        self._set_enabled(True)
        self._make_default_template(with_key=True)
        self._set_operator(configured=False)
        res = self._call()
        self.assertTrue(res['success'])
        data = res['data']
        self.assertFalse(data['ready'])
        self.assertTrue(data['enabled'])
        self.assertFalse(data['operator_configured'])
        self.assertEqual(data['reason'], 'Operator 未設定')
