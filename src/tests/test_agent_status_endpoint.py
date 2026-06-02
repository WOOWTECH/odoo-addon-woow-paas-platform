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
import os
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
        # A real internal user for per-project ACL: the default TransactionCase
        # user is OdooBot, and message_subscribe is a no-op for the system
        # partner — so "subscribe self.env.user" would NOT grant access. A real
        # follower exercises _check_project_access's unbound (follower) leg.
        self.member = self.env['res.users'].sudo().create({
            'name': 'PP Member',
            'login': 'pp_member',
            'email': 'pp_member@example.test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.member_env = self.env(user=self.member)

    # -- helpers ---------------------------------------------------------

    def _call(self, project_id=0, env=None):
        """Call api_ai_agent_status with request.env bound to a test env.

        ``project_id`` selects per-project scope; ``env`` lets ACL tests call
        as a non-member user (``self.env(user=other)``).
        """
        test_env = env or self.env

        class _Req:
            env = test_env

        with patch.object(self._ai_assistant, 'request', _Req()):
            return self.controller.api_ai_agent_status(project_id=project_id)

    def _call_pod(self, project_id=0, env=None):
        """Call api_ai_agent_pod_status with request.env bound to a test env."""
        test_env = env or self.env

        class _Req:
            env = test_env

        with patch.object(self._ai_assistant, 'request', _Req()):
            return self.controller.api_ai_agent_pod_status(project_id=project_id)

    def _make_accessible_project(self):
        """Unbound project ``self.member`` can access (follower leg of
        ``_check_project_access``) — isolates readiness logic from ACL. Pair
        with ``env=self.member_env`` on the call."""
        project = self.env['project.project'].sudo().create({
            'name': 'PP Test Project',
        })
        project.message_subscribe(partner_ids=[self.member.partner_id.id])
        return project

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

    # == Per-project scope (kanban pill) =================================
    # project_id selects per-project readiness: use_agent_runtime flag +
    # _resolve_agent_template (project binding → platform default) + key +
    # operator. Mirrors the gates _stream_agent_runtime walks before spawn.

    def test_pp1_project_ready_via_agent_template_id(self):
        """use_agent_runtime + project-bound template (path ③) + key +
        operator → ready; surfaces that project's template/model."""
        self._set_operator(configured=True)
        cfg = self._make_config(with_key=True)
        tmpl = self.AgentTemplate.create({
            'name': 'Proj Bound Agent',
            'is_public': True,
            'state': 'published',
            'model_config_id': cfg.id,
        })
        project = self._make_accessible_project()
        project.write({'use_agent_runtime': True, 'agent_template_id': tmpl.id})
        res = self._call(project_id=project.id, env=self.member_env)
        self.assertTrue(res['success'])
        data = res['data']
        self.assertEqual(data['scope'], 'project')
        self.assertTrue(data['ready'])
        self.assertTrue(data['enabled'])
        self.assertEqual(data['template_name'], 'Proj Bound Agent')
        self.assertEqual(data['model_name'], 'gpt-4o-mini')
        self.assertEqual(data['reason'], '')

    def test_pp2_project_runtime_disabled(self):
        """use_agent_runtime off wins precedence — even with operator set."""
        self._set_operator(configured=True)
        project = self._make_accessible_project()
        project.write({'use_agent_runtime': False})
        res = self._call(project_id=project.id, env=self.member_env)
        self.assertTrue(res['success'])
        data = res['data']
        self.assertEqual(data['scope'], 'project')
        self.assertFalse(data['ready'])
        self.assertFalse(data['enabled'])
        self.assertEqual(data['reason'], '此專案未啟用 Agent runtime')

    def test_pp3_project_no_template(self):
        """Enabled but no binding and no platform default → '無 Agent Template'."""
        self._set_operator(configured=True)
        self.ICP.set_param(_DEFTMPL, '')  # kill the ④ platform-default fallback
        project = self._make_accessible_project()
        project.write({'use_agent_runtime': True})
        res = self._call(project_id=project.id, env=self.member_env)
        self.assertTrue(res['success'])
        data = res['data']
        self.assertFalse(data['ready'])
        self.assertTrue(data['enabled'])
        self.assertEqual(data['template_name'], '')
        self.assertEqual(data['reason'], '無 Agent Template')

    def test_pp4_cross_user_access_denied(self):
        """A non-member user must be denied — per-project status can't leak
        another tenant's template/model (B1-class guard, mirrors ir.rule)."""
        project = self._make_accessible_project()  # follower = self.member
        project.write({'use_agent_runtime': True})
        outsider = self.env['res.users'].sudo().create({
            'name': 'PP Outsider',
            'login': 'pp_outsider',
            'email': 'pp_outsider@example.test',
        })
        res = self._call(project_id=project.id, env=self.env(user=outsider))
        self.assertFalse(res['success'])
        self.assertEqual(res['error'], 'Access denied')

    # == Live pod lifecycle (stage 2) ====================================

    def test_pod1_no_session_is_absent(self):
        """Project that never chatted → no agent_session → 'absent', and the
        status poll must NOT create a session row (side-effect-free)."""
        self._set_operator(configured=True)
        project = self._make_accessible_project()
        Session = self.env['woow_paas_platform.agent_session'].sudo()
        res = self._call_pod(project_id=project.id, env=self.member_env)
        self.assertTrue(res['success'])
        self.assertEqual(res['data']['state'], 'absent')
        self.assertFalse(
            Session.search([('project_id', '=', project.id)]),
            'status poll must not create an agent_session',
        )

    def test_pod2_active_via_operator(self):
        """With a session + operator reporting active → state 'active'."""
        from odoo.addons.woow_paas_platform.services import (
            agent_runtime_client as arc_mod,
        )
        project = self._make_accessible_project()
        # agent_session.create() derives the namespace hash from
        # AGENT_RUNTIME_NAMESPACE_SECRET and raises (fail-closed) if unset, so
        # inject a test value for the create. The endpoint never re-derives —
        # it reads the stored hash — so the operator call is what we mock below.
        with patch.dict(os.environ, {"AGENT_RUNTIME_NAMESPACE_SECRET": "pp-test-secret"}):
            self.env['woow_paas_platform.agent_session'].sudo().create({
                'project_id': project.id,
            })

        class _FakeClient:
            def get_status(self, _hash):
                return {'state': 'active', 'pod_ws_url': 'ws://pod/x'}

        with patch.object(
            arc_mod, 'get_agent_runtime_client', return_value=_FakeClient(),
        ):
            res = self._call_pod(project_id=project.id, env=self.member_env)
        self.assertTrue(res['success'])
        self.assertEqual(res['data']['state'], 'active')
        self.assertEqual(res['data']['pod_ws_url'], 'ws://pod/x')

    def test_pod3_operator_unconfigured_is_unknown(self):
        """Operator unconfigured degrades to 'unknown' (HTTP 200), not a 500."""
        self._set_operator(configured=False)
        project = self._make_accessible_project()
        res = self._call_pod(project_id=project.id, env=self.member_env)
        self.assertTrue(res['success'])
        self.assertEqual(res['data']['state'], 'unknown')
