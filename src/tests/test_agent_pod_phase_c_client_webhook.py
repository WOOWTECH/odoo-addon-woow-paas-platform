"""TDD red phase — Phase C 塊③: agent_runtime_client + webhook receiver + drift cron.

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase C Tasks 10-12
Design:
  - §5.5 agent_session ↔ paas-operator state sync
         (webhook primary HMAC-SHA256 + cron 輪詢 fallback)
  - §5.6 SSE generator uses get_agent_runtime_client(env)
  - §2.11 operator endpoints the client wraps

Acceptance criteria covered:

CL.1  ``get_agent_runtime_client(env)`` returns a configured client when
      operator_url + operator_api_key are set; ``None`` when unconfigured
      (mirrors existing ``get_paas_operator_client``).
CL.2  ``client.spawn_or_attach(...)`` POSTs to
      ``{operator_url}/api/agent-runtimes/spawn-or-attach`` with the
      ``X-API-Key`` header and returns the parsed JSON dict.
CL.3  ``client.reap(project_hash)`` POSTs to ``/{hash}/reap``.
CL.4  ``client.sync_workspaces(hash, ids)`` POSTs ``project_workspace_ids``
      to ``/{hash}/sync-workspaces``.
CL.5  ``client.get_status(project_hash)`` GETs ``/{hash}/status``.

WH.1  Webhook with a VALID HMAC-SHA256 signature → 200; the matching
      ``agent_session`` row's ``state`` + ``last_synced_at`` +
      ``last_state_event_ts`` are updated.
WH.2  Webhook with an INVALID signature → 401; the session is untouched.
WH.3  Webhook with NO signature header → 401.
WH.4  Webhook for an unknown ``project_hash`` → 404 (no session created).

CR.1  A drift-detection ``ir.cron`` bound to ``agent_session`` exists and
      is active (5-minute fallback poll, §5.5).
CR.2  Triggering the cron with a STALE active session
      (last_synced_at < now-10min) calls the operator client's
      ``get_status`` to re-sync.

Red expectation today (Phase C impl has NOT started):

* CL.* → RED: in-function ``import`` of the not-yet-created
  ``services.agent_runtime_client`` raises ModuleNotFoundError *inside the
  test call* → clean per-test FAIL (does NOT break collection — the file's
  top-level imports are all stdlib + odoo test base).
* WH.* → RED: controller route
  ``/api/agent-runtimes/webhook/state-update`` does not exist → 404 for
  every request, so the 200 / 401 / 404 expectations fail (404≠expected).
* CR.* → RED: no drift ir.cron record yet → search returns empty.

Fixture / env assumptions:

* Phase A new models (agent_session) in registry.
* operator_url / operator_api_key set via ir.config_parameter for client
  tests (existing keys).
* Webhook HMAC secret read by the receiver from the INJECTED ENV (OQ#1
  ruling (b) — K8s Secret env injection, design §5.3 commit f5333c3). The
  env var NAME is impl-defined; the placeholder below re-aligns if impl picks
  a different name. In the dev sandbox / stg the helm chart injects it from the
  K8s Secret; here the test sets os.environ for the in-process controller and
  restores it via addCleanup. (operator_url / operator_api_key for the client
  tests stay as ir.config_parameter — those are existing Odoo settings, not
  the new shared secret.)

Boundary (intentionally NOT tested here):

* paas-operator endpoint internals — block ① pytest.
* skill-bundle endpoint — block ②.
* SSE generator dual-mode wiring — block ④.
* webhook retry/backoff on the *sender* side — that is paas-operator's
  ``webhook_dispatcher`` (block ① W.2); here we test the *receiver*.
"""
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo.tests.common import HttpCase, TransactionCase, tagged


_WEBHOOK_SECRET = "phase-c-webhook-secret-abcdef"
# OQ#1 ruling (b): K8s Secret env injection. Env var NAME is impl-defined —
# this placeholder re-aligns when impl enters.
_ENV_WEBHOOK_SECRET = "AGENT_RUNTIME_WEBHOOK_SECRET"
# Forward-compat with the Phase D F-PC-3 fail-closed fix: creating an
# agent_session now requires the injected namespace secret env.
_ENV_NAMESPACE_SECRET = "AGENT_RUNTIME_NAMESPACE_SECRET"
_NAMESPACE_SECRET = "phase-c-namespace-secret"
_CLIENT_MOD = "odoo.addons.woow_paas_platform.services.agent_runtime_client"


# --------------------------------------------------------------------------- #
# CL.* — agent_runtime_client HTTP wrapper                                      #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCAgentRuntimeClient(TransactionCase):
    """CL.1-CL.5 — Odoo → paas-operator HTTP client wrapper."""

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'test-api-key')
        # spawn_or_attach resolves the project hash by creating an agent_session,
        # which now requires the namespace secret env (F-PC-3 fail-closed).
        os.environ[_ENV_NAMESPACE_SECRET] = _NAMESPACE_SECRET
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)

    def _import_client(self):
        """Import the (not-yet-created) client module.

        Red today → convert the ModuleNotFoundError into a clean ``self.fail``
        (AssertionError) with a not-implemented message, so the red reads as
        "feature missing" rather than a raw collection/import ERROR.
        """
        import importlib
        try:
            return importlib.import_module(_CLIENT_MOD)
        except ImportError as exc:
            self.fail(
                "services.agent_runtime_client not implemented yet (%s). "
                "Plan §Phase C Task 10 — Odoo → paas-operator HTTP client." % exc
            )

    def _fake_response(self, status=200, payload=None):
        resp = MagicMock()
        resp.status_code = status
        resp.ok = 200 <= status < 300
        resp.json.return_value = payload or {}
        resp.content = json.dumps(payload or {}).encode()
        return resp

    def test_CL1_factory_returns_configured_or_none(self):
        mod = self._import_client()
        client = mod.get_agent_runtime_client(self.env)
        self.assertIsNotNone(
            client,
            "CL.1: get_agent_runtime_client must return a client when "
            "operator_url + operator_api_key are configured.",
        )
        # Unconfigured → None
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.operator_url', '',
        )
        self.assertIsNone(
            mod.get_agent_runtime_client(self.env),
            "CL.1: unconfigured operator_url must yield None.",
        )

    def test_CL2_spawn_or_attach_posts_with_api_key(self):
        mod = self._import_client()
        client = mod.get_agent_runtime_client(self.env)
        with patch.object(mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._fake_response(
                200, {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                      'pod_state': 'active', 'spawn_age_ms': 10},
            )
            mock_requests.post.return_value = mock_requests.request.return_value
            project = self.env['project.project'].sudo().create(
                {'name': f'cl2-{uuid.uuid4().hex[:6]}'})
            result = client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='user',
                # model_config_id=None keeps this on the no-credential path so
                # CL.2 asserts only the X-API-Key header (F-PILOT-8 forward-
                # coupling, [[credential-claim-test-fixture-coupling]]).
                agent_template=MagicMock(id=1, model_config_id=None),
                mcp_snapshot={}, skill_snapshot=[],
            )
            self.assertEqual(result['pod_ws_url'], 'ws://pod')
            # Inspect the outbound HTTP call (request OR post)
            call = (mock_requests.request.call_args
                    or mock_requests.post.call_args)
            blob = json.dumps([call.args, call.kwargs], default=str)
            self.assertIn('spawn-or-attach', blob,
                          "CL.2: must hit /api/agent-runtimes/spawn-or-attach")
            self.assertIn('test-api-key', blob,
                          "CL.2: must send the X-API-Key header value")

    def test_CL3_reap_posts(self):
        mod = self._import_client()
        client = mod.get_agent_runtime_client(self.env)
        with patch.object(mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._fake_response(
                200, {'action': 'reaped'})
            mock_requests.post.return_value = mock_requests.request.return_value
            client.reap('pa1234567890abcd')
            call = (mock_requests.request.call_args or mock_requests.post.call_args)
            blob = json.dumps([call.args, call.kwargs], default=str)
            self.assertIn('pa1234567890abcd/reap', blob)

    def test_CL4_sync_workspaces_posts_ids(self):
        mod = self._import_client()
        client = mod.get_agent_runtime_client(self.env)
        with patch.object(mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._fake_response(200, {})
            mock_requests.post.return_value = mock_requests.request.return_value
            client.sync_workspaces('pa1234567890abcd', [3, 5, 8])
            call = (mock_requests.request.call_args or mock_requests.post.call_args)
            blob = json.dumps([call.args, call.kwargs], default=str)
            self.assertIn('sync-workspaces', blob)
            self.assertIn('project_workspace_ids', blob)

    def test_CL5_get_status_gets(self):
        mod = self._import_client()
        client = mod.get_agent_runtime_client(self.env)
        with patch.object(mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._fake_response(
                200, {'state': 'active', 'pod_ws_url': 'ws://pod'})
            mock_requests.get.return_value = mock_requests.request.return_value
            result = client.get_status('pa1234567890abcd')
            self.assertEqual(result['state'], 'active')
            call = (mock_requests.request.call_args or mock_requests.get.call_args)
            blob = json.dumps([call.args, call.kwargs], default=str)
            self.assertIn('pa1234567890abcd/status', blob)

    # CL.6 / CL.7 — F-PILOT-8 LLM credential on the spawn body (design §8.2).
    # The credential path is hardened forward: every fixture that builds a real
    # spawn body now exercises ``agent_template.model_config_id`` — see
    # [[credential-claim-test-fixture-coupling]] (CL.2's mock template carries
    # ``model_config_id=None`` lock-step so it stays on the no-credential path).

    def _make_ai_config(self, **overrides):
        vals = {
            'name': f'cfg-{uuid.uuid4().hex[:6]}', 'type': 'chatgpt',
            'api_key': 'sk-llm-secret', 'model': 'gpt-4o-mini',
            'temperature': 0.5, 'max_tokens': 1234,
            'api_base_url': 'https://llm.example/v1',
        }
        vals.update(overrides)
        return self.env['ai.config'].sudo().create(vals)

    def test_CL6_spawn_or_attach_carries_llm_credential(self):
        """CL.6: spawn body carries api_key + llm_model_config resolved from the
        template's ai.config (mirrors ai_client.from_assistant: model /
        api_base_url / temperature / max_tokens)."""
        mod = self._import_client()
        client = mod.get_agent_runtime_client(self.env)
        config = self._make_ai_config()
        template = self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': f'tmpl-{uuid.uuid4().hex[:6]}',
            'model_config_id': config.id,
        })
        with patch.object(mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._fake_response(
                200, {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                      'pod_state': 'active', 'spawn_age_ms': 10})
            mock_requests.post.return_value = mock_requests.request.return_value
            project = self.env['project.project'].sudo().create(
                {'name': f'cl6-{uuid.uuid4().hex[:6]}'})
            client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='owner', agent_template=template,
                mcp_snapshot={}, skill_snapshot=[])
            call = (mock_requests.request.call_args or mock_requests.post.call_args)
            body = call.kwargs.get('json') or {}
            self.assertEqual(
                body.get('api_key'), 'sk-llm-secret',
                "CL.6: spawn body must carry the tenant LLM api_key (F-PILOT-8)")
            cfg = body.get('llm_model_config') or {}
            self.assertEqual(cfg.get('model'), 'gpt-4o-mini')
            self.assertEqual(cfg.get('api_base'), 'https://llm.example/v1')
            self.assertEqual(cfg.get('temperature'), 0.5)
            self.assertEqual(cfg.get('max_tokens'), 1234)

    def test_CL6b_model_overrides_merge_on_top(self):
        """CL.6b: agent_template.model_overrides (JSON) overrides ai.config."""
        mod = self._import_client()
        client = mod.get_agent_runtime_client(self.env)
        config = self._make_ai_config()
        template = self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': f'tmpl-{uuid.uuid4().hex[:6]}',
            'model_config_id': config.id,
            'model_overrides': json.dumps({'model': 'gpt-4o', 'temperature': 0.1}),
        })
        with patch.object(mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._fake_response(
                200, {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                      'pod_state': 'active', 'spawn_age_ms': 10})
            mock_requests.post.return_value = mock_requests.request.return_value
            project = self.env['project.project'].sudo().create(
                {'name': f'cl6b-{uuid.uuid4().hex[:6]}'})
            client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='owner', agent_template=template,
                mcp_snapshot={}, skill_snapshot=[])
            call = (mock_requests.request.call_args or mock_requests.post.call_args)
            cfg = (call.kwargs.get('json') or {}).get('llm_model_config') or {}
            self.assertEqual(cfg.get('model'), 'gpt-4o', "override wins")
            self.assertEqual(cfg.get('temperature'), 0.1, "override wins")
            self.assertEqual(cfg.get('max_tokens'), 1234, "un-overridden kept")

    def test_CL7_spawn_or_attach_omits_credential_without_config(self):
        """CL.7 (拍板: key absent → fallback echo): a template with no bound
        ai.config sends NO api_key / llm_model_config → operator skips the LLM
        Secret → pod falls back to echo."""
        mod = self._import_client()
        client = mod.get_agent_runtime_client(self.env)
        template = self.env['woow_paas_platform.agent_template'].sudo().create(
            {'name': f'tmpl-noconfig-{uuid.uuid4().hex[:6]}'})
        with patch.object(mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._fake_response(
                200, {'pod_ws_url': 'ws://pod', 'auth_token': 't',
                      'pod_state': 'active', 'spawn_age_ms': 10})
            mock_requests.post.return_value = mock_requests.request.return_value
            project = self.env['project.project'].sudo().create(
                {'name': f'cl7-{uuid.uuid4().hex[:6]}'})
            client.spawn_or_attach(
                project=project, channel_id=1, invoker_user_id=2,
                invoker_role='owner', agent_template=template,
                mcp_snapshot={}, skill_snapshot=[])
            call = (mock_requests.request.call_args or mock_requests.post.call_args)
            body = call.kwargs.get('json') or {}
            self.assertIsNone(body.get('api_key'),
                              "CL.7: no api_key when template has no ai.config")
            self.assertIsNone(body.get('llm_model_config'),
                              "CL.7: no llm_model_config without a credential")


# --------------------------------------------------------------------------- #
# WH.* — webhook receiver (HMAC-SHA256 verify → state sync)                     #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCWebhookReceiver(HttpCase):
    """WH.1-WH.4 — POST /api/agent-runtimes/webhook/state-update."""

    _ENDPOINT = '/api/agent-runtimes/webhook/state-update'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.agent_session' not in cls.env.registry:
            raise RuntimeError(
                "Phase C block③ prerequisite: agent_session not registered."
            )
        cls.Session = cls.env['woow_paas_platform.agent_session']
        cls.Project = cls.env['project.project']

    def _set_env(self, key, value):
        """Set an env var for the in-process controller, restore on teardown."""
        prev = os.environ.get(key)
        os.environ[key] = value
        self.addCleanup(
            lambda: os.environ.__setitem__(key, prev) if prev is not None
            else os.environ.pop(key, None)
        )

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        # OQ#1 ruling (b): receiver reads the HMAC secret from the injected ENV
        # (K8s Secret env injection). Env var name is an impl-defined placeholder.
        self._set_env(_ENV_WEBHOOK_SECRET, _WEBHOOK_SECRET)
        # Creating the session below now requires the namespace secret env
        # (F-PC-3 fail-closed).
        self._set_env(_ENV_NAMESPACE_SECRET, _NAMESPACE_SECRET)
        self.project = self.Project.sudo().create({'name': f'wh-{self.suffix}'})
        # agent_session auto-computes project_namespace_hash on create.
        self.session = self.Session.sudo().create({
            'project_id': self.project.id,
            'state': 'spawning',
        })
        self.hash = self.session.project_namespace_hash

    def _post_signed(self, payload, secret=None, sign=True):
        body = json.dumps(payload).encode()
        headers = {'Content-Type': 'application/json'}
        if sign:
            sig = hmac.new(
                (secret or _WEBHOOK_SECRET).encode(), body, hashlib.sha256,
            ).hexdigest()
            headers['X-PaaS-Signature'] = sig
        return self.url_open(self._ENDPOINT, data=body, headers=headers, timeout=15)

    def test_WH1_valid_hmac_updates_session(self):
        self.assertTrue(self.hash, "fixture: session must have a namespace hash")
        resp = self._post_signed({
            'project_hash': self.hash,
            'new_state': 'active',
            'ts': '2026-05-24T00:00:00Z',
            'reason': 'spawn complete',
            'pod_ws_url': 'ws://pod.local:8080',
            'spawn_age_ms': 5234,
        })
        self.assertEqual(
            resp.status_code, 200,
            "WH.1: valid HMAC webhook must return 200, got %s (endpoint "
            "%s not implemented yet)." % (resp.status_code, self._ENDPOINT),
        )
        self.session.invalidate_recordset()
        self.assertEqual(
            self.session.state, 'active',
            "WH.1: session.state must be updated to the webhook's new_state.",
        )
        self.assertTrue(
            self.session.last_synced_at,
            "WH.1: last_synced_at must be stamped on webhook receipt.",
        )
        self.assertTrue(
            self.session.last_state_event_ts,
            "WH.1: last_state_event_ts must be stamped from the event ts.",
        )

    def test_WH2_invalid_hmac_rejected(self):
        resp = self._post_signed(
            {'project_hash': self.hash, 'new_state': 'active',
             'ts': '2026-05-24T00:00:00Z'},
            secret='wrong-secret',
        )
        self.assertEqual(
            resp.status_code, 401,
            "WH.2: invalid HMAC must be 401, got %s." % resp.status_code,
        )
        self.session.invalidate_recordset()
        self.assertEqual(
            self.session.state, 'spawning',
            "WH.2: a rejected webhook must NOT mutate the session state.",
        )

    def test_WH3_missing_signature_rejected(self):
        resp = self._post_signed(
            {'project_hash': self.hash, 'new_state': 'active',
             'ts': '2026-05-24T00:00:00Z'},
            sign=False,
        )
        self.assertEqual(
            resp.status_code, 401,
            "WH.3: missing X-PaaS-Signature must be 401, got %s."
            % resp.status_code,
        )

    def test_WH4_unknown_hash_not_found(self):
        resp = self._post_signed({
            'project_hash': 'pa' + uuid.uuid4().hex[:14],
            'new_state': 'active',
            'ts': '2026-05-24T00:00:00Z',
        })
        self.assertEqual(
            resp.status_code, 404,
            "WH.4: a webhook for an unknown project_hash must be 404 "
            "(no agent_session). Got %s." % resp.status_code,
        )


# --------------------------------------------------------------------------- #
# CR.* — drift detection cron (5-min fallback poll)                             #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCDriftCron(TransactionCase):
    """CR.1-CR.2 — agent_session drift detection cron (§5.5 fallback)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.agent_session' not in cls.env.registry:
            raise RuntimeError(
                "Phase C block③ prerequisite: agent_session not registered."
            )

    def _find_drift_cron(self):
        """Locate the agent_session drift ir.cron (model-bound)."""
        return self.env['ir.cron'].sudo().search([
            ('model_id.model', '=', 'woow_paas_platform.agent_session'),
        ], limit=1)

    def test_CR1_drift_cron_exists_and_active(self):
        cron = self._find_drift_cron()
        self.assertTrue(
            cron,
            "CR.1: no ir.cron bound to woow_paas_platform.agent_session. "
            "Plan §Phase C Task 12 — src/data/agent_session_cron.xml drift "
            "detection cron (every 5 min).",
        )
        self.assertTrue(cron.active, "CR.1: drift cron must be active.")

    def test_CR2_cron_calls_get_status_for_stale_session(self):
        cron = self._find_drift_cron()
        self.assertTrue(
            cron,
            "CR.2 prerequisite: drift cron must exist (see CR.1).",
        )
        # Configure operator so the client is constructible.
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'k')

        # Forward-compat with the F-PC-3 fail-closed fix: creating an
        # agent_session now requires the injected namespace secret env.
        os.environ[_ENV_NAMESPACE_SECRET] = _NAMESPACE_SECRET
        self.addCleanup(os.environ.pop, _ENV_NAMESPACE_SECRET, None)

        project = self.env['project.project'].sudo().create(
            {'name': f'cr2-{uuid.uuid4().hex[:6]}'})
        stale = self.env['woow_paas_platform.agent_session'].sudo().create({
            'project_id': project.id,
            'state': 'active',
        })
        # Make it stale (> 10 min since last sync).
        stale.sudo().write({
            'last_synced_at': datetime.utcnow() - timedelta(minutes=30),
        })

        import importlib
        mod = importlib.import_module(_CLIENT_MOD)
        fake_client = MagicMock()
        fake_client.get_status.return_value = {
            'state': 'reaped', 'pod_ws_url': False,
        }
        with patch.object(mod, 'get_agent_runtime_client',
                          return_value=fake_client):
            cron.sudo().method_direct_trigger()

        self.assertTrue(
            fake_client.get_status.called,
            "CR.2: drift cron must call agent_runtime_client.get_status "
            "for sessions whose last_synced_at is older than 10 min.",
        )
