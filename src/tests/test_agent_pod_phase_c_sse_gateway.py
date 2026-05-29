"""TDD red phase — Phase C 塊④: SSE generator dual-mode flag gate.

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase C Tasks 14-15
Design:
  - §5.6 SSE generator rewrite — ``if project.use_agent_runtime:`` branch
         (flag gate, default False); False → existing ai_client.py path;
         True → spawn_or_attach + relay 15-type envelopes as SSE events.
  - §1.2 frontend ai_service.js EventSource — ZERO change.

Acceptance criteria covered:

SSE.1  REGRESSION — ``project.use_agent_runtime`` defaults False (the gate
       is OFF by default → Phase C merge does not change production chat).
SSE.2  REGRESSION — frontend ``ai_service.js`` still builds the EventSource
       URL ``/api/ai/stream/${channelId}?csrf_token=...`` (the §1.2 / §5.6
       "frontend zero-change" contract — backend rewrite must keep the same
       SSE entry point + query shape).
SSE.3  REGRESSION — the SSE endpoint's CSRF gate stays FIRST: a request
       with no/invalid csrf_token returns the legacy ``csrf_error`` SSE
       frame (200 + text/event-stream) regardless of the runtime flag.
SSE.4  RED — with ``use_agent_runtime=True`` the endpoint takes the
       agent-runtime branch, NOT the legacy assistant path. Observable: the
       resulting SSE ``error_code`` is no longer the legacy ``no_agent``
       (it becomes a runtime-branch code such as ``no_template`` /
       ``project_access_denied`` / ``spawn_failed`` per §5.6 pseudocode).
SSE.5  REGRESSION — with ``use_agent_runtime=False`` (default) the endpoint
       keeps the legacy assistant path: a channel without an assistant
       yields ``no_agent`` (production behaviour unchanged).

Red expectation today (Phase C impl has NOT started):

* SSE.1 / SSE.2 / SSE.3 / SSE.5 → PASS today and MUST STAY GREEN post-impl
  (these lock the flag-default + frontend contract + csrf ordering + legacy
  path — i.e. "Phase C merge changes nothing while flag is False").
* SSE.4 → RED: today there is no ``use_agent_runtime`` branch, so a flag-True
  project still runs the legacy generator → ``no_agent`` → the assertion
  "error_code != no_agent" fails. Once the dual-mode branch ships, flag-True
  routes to the runtime path → a non-``no_agent`` code → green.

Why this design (failure-mode honesty):

* SSE.4 is a pure HTTP behavioural probe — it does NOT import or mock the
  block-③ client module, so its red points squarely at "the flag branch is
  missing", not at an unrelated unbuilt dependency.
* CSRF token is fetched from the rendered app bootstrap (deterministic in a
  test DB); the helper asserts a token was obtained so a csrf-extraction
  failure surfaces loudly rather than masquerading as the red.

Fixture / env assumptions:

* Phase A field ``project.use_agent_runtime`` present.
* Admin user (group_system) drives the SSE endpoint so channel/project
  access checks pass deterministically; channel has admin as a member and
  a task linking channel_id → channel + project_id → project.

Boundary (intentionally NOT tested here):

* Actual WS relay of 15-type envelopes → that path requires a live pod;
  Phase D pilot / paas-operator integration tests cover it. Here we only
  assert the *dispatch decision* (which branch the flag selects).
* spawn_or_attach client mechanics → block ③.
* Enabling the flag for a pilot project + cold-start latency → Phase D.
"""
import json
import os
import re
import uuid

from odoo.tests.common import HttpCase, TransactionCase, tagged


# --------------------------------------------------------------------------- #
# SSE.1 / SSE.2 — flag default + frontend contract (no HTTP needed)             #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSseFlagAndFrontendContract(TransactionCase):
    """SSE.1-SSE.2 — gate default + frozen frontend EventSource URL."""

    def test_SSE1_use_agent_runtime_defaults_false(self):
        project = self.env['project.project'].sudo().create(
            {'name': f'sse1-{uuid.uuid4().hex[:6]}'})
        self.assertIn(
            'use_agent_runtime', project._fields,
            "SSE.1: project.use_agent_runtime field missing (Phase A).",
        )
        self.assertFalse(
            project.use_agent_runtime,
            "SSE.1: use_agent_runtime MUST default False — Phase C merge "
            "keeps production chat on the legacy path until Phase D enables "
            "the flag per pilot project.",
        )

    def test_SSE2_frontend_eventsource_url_unchanged(self):
        """SSE.2: ai_service.js still builds /api/ai/stream/{id}?csrf_token=..."""
        js_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), '..',
            'static', 'src', 'paas', 'services', 'ai_service.js',
        ))
        with open(js_path, 'r', encoding='utf-8') as f:
            src = f.read()
        self.assertIn(
            '/api/ai/stream/', src,
            "SSE.2: frontend EventSource URL must remain /api/ai/stream/ "
            "(§5.6 frontend zero-change). The backend SSE rewrite must not "
            "move the entry point.",
        )
        self.assertIn(
            'csrf_token=', src,
            "SSE.2: EventSource URL must keep the csrf_token query param.",
        )

    def test_SSE6_skill_snapshot_carries_description(self):
        """SSE.6 (F-PILOT-10 Gap 3 hybrid): _build_skill_snapshot serialises
        published skills with name/version/sha256 + description (host-owned,
        lets the pod register a StructuredTool description WITHOUT fetching the
        bundle). lock-step guard on the spawn snapshot contract (credential-
        adjacent assembly). parameters/args_schema is intentionally absent
        (runtime-owned; see follow-ups F-PILOT-11)."""
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        project = self.env['project.project'].sudo().create(
            {'name': f'sse6-{uuid.uuid4().hex[:6]}'})
        published = self.env['woow_paas_platform.skill'].sudo().create({
            'name': 'sk-pub', 'version': '0.1.0', 'bundle_sha256': 'a' * 64,
            'project_id': project.id, 'state': 'published',
            'description': 'Fetches the weather for a city.',
        })
        # a draft skill must NOT leak into the snapshot
        self.env['woow_paas_platform.skill'].sudo().create({
            'name': 'sk-draft', 'version': '0.1.0', 'bundle_sha256': 'b' * 64,
            'project_id': project.id, 'state': 'draft',
            'description': 'not published',
        })
        template = self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': f'tmpl-{uuid.uuid4().hex[:6]}',
            'skill_ids': [(6, 0, (published + self.env['woow_paas_platform.skill']
                                  .sudo().search([('name', '=', 'sk-draft')])).ids)],
        })
        snapshot = AiAssistantController()._build_skill_snapshot(project, template)
        names = [e['name'] for e in snapshot]
        self.assertIn('sk-pub', names,
                      "published project skill must be in the snapshot")
        self.assertNotIn('sk-draft', names,
                         "SSE.6: draft skill must NOT be in the snapshot")
        entry = next(e for e in snapshot if e['name'] == 'sk-pub')
        self.assertEqual(entry['version'], '0.1.0')
        self.assertEqual(entry['sha256'], 'a' * 64)
        self.assertEqual(
            entry['description'], 'Fetches the weather for a city.',
            "SSE.6: snapshot must carry the skill description (Gap 3 hybrid)")


# --------------------------------------------------------------------------- #
# SSE.3 / SSE.4 / SSE.5 — dual-mode dispatch behaviour                          #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSseDualMode(HttpCase):
    """SSE.3-SSE.5 — csrf ordering + flag-keyed dispatch."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Project = cls.env['project.project']
        cls.Channel = cls.env['discuss.channel']
        cls.Task = cls.env['project.task']
        cls.admin = cls.env.ref('base.user_admin')

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        self.project = self.Project.sudo().create(
            {'name': f'sse-{self.suffix}'})
        # Channel with admin as member so _check_channel_access passes.
        self.channel = self.Channel.sudo().create({
            'name': f'sse-ch-{self.suffix}',
            'channel_type': 'channel',
            'channel_partner_ids': [(4, self.admin.partner_id.id)],
        })
        # Task binds channel → project so the SSE generator resolves project.
        self.task = self.Task.sudo().create({
            'name': f'sse-task-{self.suffix}',
            'project_id': self.project.id,
            'channel_id': self.channel.id,
        })

    # ----- helpers -------------------------------------------------------- #
    def _csrf_token(self):
        """Fetch a csrf token bound to the current authenticated session.

        Parsed from the rendered app bootstrap (odoo.csrf_token = "...").
        Asserts a token was found so a csrf-extraction problem surfaces
        loudly instead of being mistaken for the SSE red.
        """
        # Odoo csrf tokens are "<hmachex>o<expiryhex>" — capture the WHOLE
        # quoted value (a [0-9a-f]+ class truncates at the 'o' separator and
        # yields a malformed token that validate_csrf rejects).
        for page in ('/woow', '/web'):
            resp = self.url_open(page, timeout=15)
            if resp.status_code != 200:
                continue
            m = re.search(r'csrf_token[\"\']?\s*[:=]\s*[\"\']([^\"\']+)[\"\']',
                          resp.text)
            if m:
                return m.group(1)
        self.fail(
            "csrf token could not be extracted from /woow or /web — SSE "
            "behavioural tests need a valid token (this is a fixture issue, "
            "not the feature red).",
        )

    def _open_stream(self, csrf_token):
        url = f'/api/ai/stream/{self.channel.id}?csrf_token={csrf_token}'
        return self.url_open(url, timeout=20)

    def _first_sse_frame(self, resp):
        """Parse the first ``data: {json}`` SSE frame → dict."""
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith('data:'):
                return json.loads(line[len('data:'):].strip())
        return {}

    # ----- SSE.3 csrf gate first (regression) ----------------------------- #
    def test_SSE3_csrf_gate_first_regardless_of_flag(self):
        self.project.sudo().write({'use_agent_runtime': True})
        self.authenticate('admin', 'admin')
        # No csrf_token query param → must be rejected at the csrf gate.
        resp = self.url_open(
            f'/api/ai/stream/{self.channel.id}', timeout=15)
        self.assertEqual(resp.status_code, 200)
        frame = self._first_sse_frame(resp)
        self.assertEqual(
            frame.get('error_code'), 'csrf_error',
            "SSE.3: the CSRF gate must run FIRST — a request without a valid "
            "csrf_token returns 'csrf_error' before any flag/project "
            "resolution (security ordering preserved by the rewrite). Got "
            "frame=%r." % frame,
        )

    # ----- SSE.4 flag True → runtime branch (RED) ------------------------- #
    def test_SSE4_flag_true_takes_runtime_branch(self):
        self.project.sudo().write({'use_agent_runtime': True})
        self.authenticate('admin', 'admin')
        token = self._csrf_token()
        resp = self._open_stream(token)
        self.assertEqual(resp.status_code, 200)
        frame = self._first_sse_frame(resp)
        self.assertNotEqual(
            frame.get('error_code'), 'no_agent',
            "SSE.4: with use_agent_runtime=True the endpoint must take the "
            "agent-runtime branch (§5.6), NOT the legacy assistant lookup. "
            "Today there is no flag branch so a flag-True project still hits "
            "the legacy path → 'no_agent'. Post-impl the runtime branch "
            "yields a different code (no_template / project_access_denied / "
            "spawn_failed). Got frame=%r." % frame,
        )

    # ----- SSE.5 flag False → legacy path (regression) -------------------- #
    def test_SSE5_flag_false_keeps_legacy_path(self):
        # Default flag False (do not write it) — channel has no assistant.
        self.assertFalse(self.project.use_agent_runtime)
        self.authenticate('admin', 'admin')
        token = self._csrf_token()
        resp = self._open_stream(token)
        self.assertEqual(resp.status_code, 200)
        frame = self._first_sse_frame(resp)
        self.assertEqual(
            frame.get('error_code'), 'no_agent',
            "SSE.5: with use_agent_runtime=False (default) the endpoint must "
            "keep the legacy assistant path — a channel with no opted-in "
            "assistant yields 'no_agent'. This locks production behaviour "
            "unchanged while the flag is off. Got frame=%r." % frame,
        )
