"""TDD red phase — Phase D F-PC-6 (P-2): SSE→pod WS bridge async-gen aclose.

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md WS1 F-PC-6 (caveat P-2).
Bug  : ``ai_assistant.py:678-729`` drains the ``relay()`` async generator with
       ``loop.run_until_complete(agen.__anext__())`` and, on the ``done`` /
       ``error`` branches, ``break``s out of the while loop — leaving ``agen``
       SUSPENDED at its ``yield`` inside ``async with websockets.connect()``.
       The ``finally`` only ``loop.close()``s; it never ``agen.aclose()``s, so
       the connection's ``__aexit__`` (the WS close) never runs → leaked pod
       connection per chat turn.

Fix direction: in the drain-loop ``finally``, before ``loop.close()``, run
``loop.run_until_complete(agen.aclose())`` so the suspended generator is
unwound and the ``async with`` closes the socket.

OBSERVABLE: we inject a fake ``websockets`` module whose connection records
whether its ``__aexit__`` ran (``closed``). Driving the real SSE route through
the bridge and inspecting that flag is a deterministic probe that does not
depend on GC timing.

Acceptance:
* AC.6a done branch   → RED today: ``done`` envelope yielded, drain breaks, gen
                        left suspended → connection NOT closed. Post-fix: closed.
* AC.6b error branch  → RED today: same leak on the ``error`` branch.
* AC.6c exception     → REGRESSION (green now & after): when iteration itself
                        raises, Python unwinds the ``async with`` natively, so
                        the socket IS closed even without aclose(). This locks
                        that the aclose() fix does not regress the path that
                        already releases the socket (and that aclose() on an
                        already-finalised gen is a harmless no-op).
        NOTE for the leader: the plan framed all three branches as leaking;
        analysis shows only done/error leak (suspended-at-yield). The exception
        branch auto-unwinds — writing it as a "red" would be a false red, so it
        is a green regression-guard here. (failure-mode honesty)

Fixture / env assumptions:
* Phase A field ``project.use_agent_runtime``; agent_template + project_access
  models present. Admin (group_system) drives the route so access checks pass.
* ``get_agent_runtime_client`` is patched to a fake whose ``spawn_or_attach``
  returns a pod_ws_url + auth_token, so the route enters the bridge without a
  live operator. ``websockets`` is replaced in ``sys.modules`` by a fake.
* Namespace secret env set (forward-compat with the F-PC-3 fail-closed fix —
  the bridge path does not create a session, but kept for symmetry/safety).

Boundary (intentionally NOT here):
* Real envelope semantics / 15-type relay correctness → pilot / live pod.
* spawn_or_attach client mechanics → block ③ / F-PC-5.
"""
import json
import re
import sys
import uuid

from odoo.tests.common import HttpCase, tagged

_CLIENT_MOD = "odoo.addons.woow_paas_platform.services.agent_runtime_client"
# error_codes emitted by _sse_error_response when the bridge is NOT reached;
# their presence means a setup failure, not the aclose red.
_SETUP_FAILURE_CODES = (
    'csrf_error', 'channel_not_found', 'access_denied',
    'project_access_denied', 'no_template', 'spawn_failed',
    'provider_not_configured', 'no_agent',
)


# --------------------------------------------------------------------------- #
# Fake websockets connection that records whether __aexit__ (close) ran.        #
# --------------------------------------------------------------------------- #
class _FakeWSConn:
    def __init__(self, frames=None, raise_on_iter=False):
        self._frames = list(frames or [])
        self._raise_on_iter = raise_on_iter
        self.closed = False
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        # A real websockets close AWAITS network I/O, so it can only complete
        # on a LIVE event loop. Modelling that await is what makes the leak
        # observable: without the aclose() fix the drain loop ``break``s with
        # the generator suspended, ``loop.close()``s, and the abandoned
        # generator is only finalised later by GC — at which point this await
        # can no longer run (loop closed) → ``closed`` stays False (the leak).
        # With the fix, ``loop.run_until_complete(agen.aclose())`` unwinds the
        # generator while the loop is still open → this await completes →
        # ``closed`` becomes True deterministically.
        import asyncio
        await asyncio.sleep(0)
        self.closed = True
        return False  # do not suppress

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._raise_on_iter:
            raise RuntimeError("simulated pod WS read failure")
        if self._frames:
            return self._frames.pop(0)
        raise StopAsyncIteration


def _make_fake_websockets():
    """Return a fake ``websockets`` module object tracking created conns."""
    import types
    mod = types.ModuleType("websockets")
    mod.created = []

    def connect(uri, *args, **kwargs):
        conn = mod._next_conn
        mod.created.append(conn)
        return conn

    mod.connect = connect
    mod._next_conn = None
    return mod


@tagged('post_install', '-at_install')
class TestPhaseDSseBridgeAclose(HttpCase):
    """F-PC-6 — the bridge must aclose() the relay generator (close the WS)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = cls.env.ref('base.user_admin')

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        # NB: the bridge path mocks get_agent_runtime_client, so no
        # agent_session is created here → no namespace secret needed.
        Project = self.env['project.project'].sudo()
        self.project = Project.create({'name': f'aclose-{self.suffix}'})
        # unbound project → _check_project_access needs admin as follower.
        self.project.message_subscribe(partner_ids=[self.admin.partner_id.id])
        self.project.write({'use_agent_runtime': True})
        # a resolvable agent_template bound to the project.
        self.template = self.env['woow_paas_platform.agent_template'].sudo().create({
            'name': f'tmpl-{self.suffix}',
            'is_public': True,
            'state': 'published',
            'project_ids': [(6, 0, [self.project.id])],
        })
        self.channel = self.env['discuss.channel'].sudo().create({
            'name': f'aclose-ch-{self.suffix}',
            'channel_type': 'channel',
            'channel_partner_ids': [(4, self.admin.partner_id.id)],
        })
        self.task = self.env['project.task'].sudo().create({
            'name': f'aclose-task-{self.suffix}',
            'project_id': self.project.id,
            'channel_id': self.channel.id,
        })

    # ----- helpers -------------------------------------------------------- #
    def _csrf_token(self):
        for page in ('/woow', '/web'):
            resp = self.url_open(page, timeout=15)
            if resp.status_code != 200:
                continue
            m = re.search(r'csrf_token[\"\']?\s*[:=]\s*[\"\']([^\"\']+)[\"\']',
                          resp.text)
            if m:
                return m.group(1)
        self.fail("csrf token could not be extracted (fixture issue, not the "
                  "aclose red).")

    def _install_fake_ws(self, conn):
        fake = _make_fake_websockets()
        fake._next_conn = conn
        prev = sys.modules.get('websockets')
        sys.modules['websockets'] = fake
        self.addCleanup(
            lambda: sys.modules.__setitem__('websockets', prev) if prev
            is not None else sys.modules.pop('websockets', None))
        return fake

    def _fake_client(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.spawn_or_attach.return_value = {
            'pod_ws_url': 'ws://pod.local:8080',
            'auth_token': 'tok-abc',
            'pod_state': 'active',
            'spawn_age_ms': 12,
        }
        return client

    def _drive(self, conn):
        """Run the SSE bridge once with the given fake connection; return resp."""
        from unittest.mock import patch
        fake = self._install_fake_ws(conn)
        self.authenticate('admin', 'admin')
        token = self._csrf_token()
        url = f'/api/ai/stream/{self.channel.id}?csrf_token={token}'
        with patch(_CLIENT_MOD + '.get_agent_runtime_client',
                   return_value=self._fake_client()):
            resp = self.url_open(url, timeout=25)
        return resp, fake

    def _assert_reached_bridge(self, resp):
        self.assertEqual(resp.status_code, 200, "SSE endpoint must be 200")
        for code in _SETUP_FAILURE_CODES:
            self.assertNotIn(
                '"error_code": "%s"' % code, resp.text,
                "F-PC-6 setup: the request did not reach the WS bridge "
                "(hit '%s'). Fix the fixture, this is not the aclose red. "
                "Body=%r" % (code, resp.text[:300]),
            )

    # ----- AC.6a done branch (RED) ---------------------------------------- #
    def test_FPC6a_done_branch_closes_connection(self):
        conn = _FakeWSConn(frames=[json.dumps({'type': 'done', 'payload': {}})])
        resp, fake = self._drive(conn)
        self._assert_reached_bridge(resp)
        self.assertTrue(fake.created, "fake websockets.connect was never called")
        self.assertTrue(
            fake.created[-1].closed,
            "F-PC-6 (done): after a 'done' envelope the drain loop breaks and "
            "leaves the relay generator suspended inside 'async with "
            "websockets.connect()'; the finally only loop.close()s, so the "
            "pod connection is NEVER closed (leak). The fix must "
            "loop.run_until_complete(agen.aclose()) before loop.close().",
        )

    # ----- AC.6b error branch (RED) --------------------------------------- #
    def test_FPC6b_error_branch_closes_connection(self):
        conn = _FakeWSConn(frames=[json.dumps(
            {'type': 'error', 'payload': {'error': 'boom', 'fatal': True}})])
        resp, fake = self._drive(conn)
        self._assert_reached_bridge(resp)
        self.assertTrue(fake.created, "fake websockets.connect was never called")
        self.assertTrue(
            fake.created[-1].closed,
            "F-PC-6 (error): the 'error' branch also breaks the drain loop "
            "with the generator suspended → connection leaked. aclose() in "
            "finally must close it.",
        )

    # ----- AC.6c exception branch (REGRESSION GREEN) ---------------------- #
    def test_FPC6c_exception_branch_already_closes(self):
        """Regression: a raised iteration unwinds async-with natively → closed.

        This is GREEN today and must stay green after the aclose() fix (which
        is a harmless no-op on an already-finalised generator).
        """
        conn = _FakeWSConn(raise_on_iter=True)
        resp, fake = self._drive(conn)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(fake.created, "fake websockets.connect was never called")
        self.assertTrue(
            fake.created[-1].closed,
            "Regression: when the WS read raises, Python unwinds the "
            "'async with' (runs __aexit__) → the socket is closed even today. "
            "The aclose() fix must not regress this.",
        )
