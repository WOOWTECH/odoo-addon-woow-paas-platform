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
            # bind the project so _check_skill_subset accepts its user-scope skills
            'project_ids': [(6, 0, [project.id])],
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
# SSE.8 / SSE.9 / SSE.10 — frontend polish contract (adversarial-review        #
#                          medium/low findings, source-level assertions)        #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSseFrontendPolishContract(TransactionCase):
    """SSE.8-SSE.10 — adversarial-review polish findings, asserted at the
    frontend source level (these are pure JS/manifest contracts that need no
    live pod). Same family as SSE.2's "frozen frontend contract" probe.

    SSE.8  (F1) — pod state → zh-tw label mapping is a SINGLE shared module
           (``services/agent_pod_state.js``), wired into web.assets_frontend,
           imported by BOTH the portal status component AND the /woow
           ProjectKanbanPage. No inline hand-written ``運行中``/``啟動中`` map
           may remain in those two consumers (duplicate-drift was the finding).
    SSE.9  (F2) — the portal task chat must wrap USER message html in the same
           ``safeHtml`` DOMPurify path /woow AiChat uses (defence-in-depth);
           AI messages keep going through ``parseMarkdown`` (already sanitised)
           and must NOT be double-wrapped.
    SSE.10 (F3) — ``openAiStream`` dispatch (the typed-event branch block, not
           just JSON.parse) is inside the try/catch so a handler-time exception
           routes to ``onParseError`` — restoring the original AiChat onmessage
           equivalence (handler faults counted toward parse-error recovery).
    """

    _BASE = ('..', 'static', 'src')

    def _read(self, *parts):
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), *parts))
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    # ----- SSE.8 (F1) shared pod-state label module ----------------------- #
    def test_SSE8_shared_pod_state_label_module(self):
        shared = self._read(
            *self._BASE, 'paas', 'services', 'agent_pod_state.js')
        self.assertIn(
            'export const POD_STATE_LABEL', shared,
            "SSE.8(F1): shared module must export POD_STATE_LABEL.")
        self.assertIn(
            'export function podStateLabel', shared,
            "SSE.8(F1): shared module must export podStateLabel(state) helper.")
        # the merged map must cover every state both consumers can show, incl.
        # spawning + unknown (ProjectKanbanPage's old inline map dropped these).
        for state in ('absent', 'spawning', 'active', 'reaped', 'unknown'):
            self.assertIn(
                state, shared,
                f"SSE.8(F1): shared map missing '{state}' state.")

        # both consumers import from the shared module ...
        portal = self._read(
            *self._BASE, 'portal', 'portal_agent_status.js')
        kanban = self._read(
            *self._BASE, 'paas', 'pages', 'project-kanban',
            'ProjectKanbanPage.js')
        for src, who in ((portal, 'portal_agent_status.js'),
                         (kanban, 'ProjectKanbanPage.js')):
            self.assertIn(
                'agent_pod_state', src,
                f"SSE.8(F1): {who} must import the shared agent_pod_state "
                "module instead of hand-rolling the label map.")
        # ... and no longer carry their OWN inline {active: "運行中"} map.
        self.assertNotIn(
            'const map = { active: "運行中"', kanban,
            "SSE.8(F1): ProjectKanbanPage must drop its inline pod-state map "
            "(the duplicate that drifted, missing spawning/unknown).")
        self.assertNotIn(
            'const POD_STATE_LABEL = {', portal,
            "SSE.8(F1): portal_agent_status must drop its inline "
            "POD_STATE_LABEL map and use the shared module.")

        # shared module is wired into the frontend bundle (portal uses it).
        manifest = self._read('..', '__manifest__.py')
        self.assertIn(
            'services/agent_pod_state.js', manifest,
            "SSE.8(F1): agent_pod_state.js must be listed in "
            "web.assets_frontend (the portal bundle), beside ai_stream.js.")

    # ----- SSE.9 (F2) portal user-message sanitize ------------------------ #
    def test_SSE9_portal_user_message_sanitized(self):
        chat = self._read(*self._BASE, 'portal', 'portal_task_chat.js')
        self.assertIn(
            'safeHtml', chat,
            "SSE.9(F2): portal task chat must import + use safeHtml for USER "
            "message html (defence-in-depth, same as /woow AiChat).")
        # the two user-message construction sites must wrap body in safeHtml:
        #   history mapping ``m.is_ai ? parseMarkdown(m.body) : <user branch>``
        #   send() push        ``html: <user branch>`` from res.data.body
        self.assertIn(
            'safeHtml(m.body)', chat,
            "SSE.9(F2): history user branch must be safeHtml(m.body), not raw "
            "m.body.")
        self.assertIn(
            'safeHtml(res.data.body)', chat,
            "SSE.9(F2): sent user message must be safeHtml(res.data.body), not "
            "raw res.data.body.")
        # AI messages stay on parseMarkdown (already sanitised) — must NOT be
        # double-wrapped in safeHtml.
        self.assertNotIn(
            'safeHtml(parseMarkdown', chat,
            "SSE.9(F2): AI markdown must not be double-sanitised "
            "(parseMarkdown already runs DOMPurify).")

    # ----- SSE.10 (F3) stream dispatch equivalence ------------------------ #
    def test_SSE10_stream_dispatch_inside_try_catch(self):
        stream = self._read(*self._BASE, 'paas', 'services', 'ai_stream.js')
        # The catch must route handler-time exceptions to onParseError ...
        self.assertIn(
            'handlers.onParseError?.(event.data, err)', stream,
            "SSE.10(F3): catch must call onParseError(event.data, err).")
        # ... AND the catch must come AFTER the whole dispatch block, so a
        # handler fault is caught too (original AiChat onmessage equivalence).
        # We anchor on the LAST dispatch branch's handler call (onDone) inside
        # the function body — NOT the JSDoc mention. The dispatch onDone call
        # is uniquely written as ``handlers.onDone?.(`` whereas the doc comment
        # writes ``onDone(data)``.
        dispatch_marker = 'handlers.onDone?.('
        self.assertIn(
            dispatch_marker, stream,
            "SSE.10(F3): expected the onDone dispatch branch "
            "(handlers.onDone?.(...)).")
        catch_call_idx = stream.index('handlers.onParseError?.(event.data, err)')
        dispatch_idx = stream.index(dispatch_marker)
        self.assertLess(
            dispatch_idx, catch_call_idx,
            "SSE.10(F3): the typed-event dispatch (through handlers.onDone) "
            "must sit INSIDE the same try whose catch calls onParseError — "
            "i.e. dispatch comes BEFORE the onParseError catch in source. Today "
            "the catch only guards JSON.parse and returns before dispatch, so a "
            "handler-time exception escapes uncounted. Restore the original "
            "AiChat onmessage equivalence by widening the try to cover dispatch.")


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
        # ai_auto_reply=False makes the "no opted-in assistant" premise explicit:
        # project.task.ai_auto_reply defaults to True, and the sandbox full-module
        # DB seeds default_ai_assistant_id (create.sh), so a default-True task
        # would resolve the seeded assistant → 'no_message' instead of the legacy
        # 'no_agent' this test locks (F-ARC-1 / gitea #131).
        self.task = self.Task.sudo().create({
            'name': f'sse-task-{self.suffix}',
            'project_id': self.project.id,
            'channel_id': self.channel.id,
            'ai_auto_reply': False,
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


# --------------------------------------------------------------------------- #
# SSE.7 — #129 error envelope → browser SSE forwarding (code + retryable)       #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSseErrorForwarding(TransactionCase):
    """SSE.7 — agent-runtime §4a error envelope must reach the browser with its
    additive ``code`` (→ error_code, friendly-message lookup) and ``retryable``
    (→ retry affordance). Two-layer lock-step with paas-agent-runtime #129: the
    runtime added these fields; the SSE gateway must forward them or the browser
    only ever sees the raw English ``error`` and cannot distinguish a retryable
    empty-response from a terminal failure.

    Pure-function probes on ``AiAssistantController._agent_error_sse_data`` — the
    full WS→SSE relay needs a live pod, but the mapping that introduced the gap
    is a pure dict transform and is unit-testable in isolation.
    """

    def _map(self, envelope):
        from odoo.addons.woow_paas_platform.controllers.ai_assistant import (
            AiAssistantController,
        )
        return AiAssistantController._agent_error_sse_data(envelope)

    def test_SSE7_empty_response_forwards_code_and_retryable(self):
        """llm_empty_response（#129 round-2 空串流）→ 帶 error_code + retryable=True。"""
        data = self._map({
            'type': 'error',
            'payload': {
                'error': 'LLM returned an empty response',
                'code': 'llm_empty_response',
                'fatal': True,
                'retryable': True,
            },
        })
        self.assertEqual(data['error'], 'LLM returned an empty response')
        self.assertEqual(
            data.get('error_code'), 'llm_empty_response',
            "SSE.7: runtime payload.code MUST forward as error_code so the "
            "frontend ERROR_MESSAGES lookup + friendly zh-tw message works. "
            "Got %r." % data,
        )
        self.assertIs(
            data.get('retryable'), True,
            "SSE.7: payload.retryable MUST forward so the browser shows 「重試」. "
            "Got %r." % data,
        )
        self.assertTrue(data['done'])

    def test_SSE7_non_retryable_error_omits_retryable(self):
        """llm_error（不可重試）→ 帶 error_code 但不帶 retryable（前端不誤導重試）。"""
        data = self._map({
            'type': 'error',
            'payload': {'error': 'boom', 'code': 'llm_error', 'fatal': True},
        })
        self.assertEqual(data.get('error_code'), 'llm_error')
        self.assertNotIn(
            'retryable', data,
            "SSE.7: when runtime omits retryable, the SSE frame must not invent "
            "it (additive-only). Got %r." % data,
        )

    def test_SSE7_missing_payload_degrades_safely(self):
        """缺 payload / 非 dict → 退回最小可見 frame，不 crash、不靜默。"""
        for envelope in ({'type': 'error'}, {'type': 'error', 'payload': None}, {}):
            data = self._map(envelope)
            self.assertEqual(data.get('error'), 'Agent runtime error')
            self.assertTrue(data['done'])
            self.assertNotIn('error_code', data)
            self.assertNotIn('retryable', data)
