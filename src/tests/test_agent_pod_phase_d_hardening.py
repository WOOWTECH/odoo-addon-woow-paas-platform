"""TDD red phase — Phase D WS1 hardening (F-PC-2 / F-PC-3 / F-PC-5 / F-PA-14).

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md WS1 (caveats C-3 / C-5 /
       N-1 + HC-2 bootstrap bypass). F-PC-1/F-PC-4/charts are host-side; F-PC-6
       (async-gen aclose) lives in test_agent_pod_phase_d_sse_aclose.py.

Acceptance criteria covered:

F-PC-2  webhook receiver dual-secret (C-3, §6.6). A body signed with the
        PREVIOUS webhook secret must verify (rotation overlap). Today the
        receiver reads only ``AGENT_RUNTIME_WEBHOOK_SECRET`` (current) →
        previous-signed body is 401 (RED). Fix mirrors
        ``agent_runtime_skill.py:62-80`` dual-env fallback + new env
        ``AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS`` (frozen name).

F-PC-3  agent_session namespace secret from ENV, fail-closed (C-5, D-2).
        The hash must derive from ``AGENT_RUNTIME_NAMESPACE_SECRET`` (frozen
        name); a MISSING env must fail-closed (raise) instead of silently
        using the source-public ``_DEFAULT_SECRET`` fallback (which lets
        anyone reading the source compute namespace hashes).

F-PC-5  agent_runtime_client typed error (N-1). On a 4xx/5xx operator
        response ``_request`` must raise a typed error carrying the operator
        ``detail`` rather than silently returning the error dict / leaking a
        cryptic JSONDecodeError.

F-PA-14 project bootstrap honours an explicit owner-partner hint even via the
        sudo path (HC-2). A trusted ``sudo().create(...)`` carrying a
        ``bootstrap_owner_partner_id`` context hint must give that partner an
        ``owner`` project_access row — without breaking "a bare SUPERUSER
        create does NOT auto-own". care [[constraint-input-trust-expansion]].

Red expectation today (impl NOT started):
* F-PC-2 previous-signed → 401 (RED, want 200); current/none unchanged.
* F-PC-3 env-secret hash ≠ stored hash (uses _DEFAULT_SECRET) → RED;
         missing env does NOT raise → RED.
* F-PC-5 4xx/5xx returns dict silently (no raise) → RED.
* F-PA-14 sudo+hint create leaves the hinted partner with no owner row → RED.

Fixture / env assumptions:
* OQ#1 ruling (b): secrets injected as K8s Secret env. Tests set os.environ
  for the in-process controller/model and restore via addCleanup. Frozen env
  names: AGENT_RUNTIME_WEBHOOK_SECRET[_PREVIOUS], AGENT_RUNTIME_NAMESPACE_SECRET.
* Tests that create an agent_session set AGENT_RUNTIME_NAMESPACE_SECRET in
  setUp so they stay green AFTER the F-PC-3 fail-closed fix lands (forward
  compatible — see report note on the existing agent_session-creating tests).

Boundary (intentionally NOT here):
* operator-side webhook_dispatcher / fail-closed (block ① / F-PC-4 pytest).
* live pod WS connect kwarg compat → pilot gate.
"""
import hashlib
import hmac
import json
import os
import uuid
from unittest.mock import MagicMock, patch

from odoo import SUPERUSER_ID
from odoo.tests.common import HttpCase, TransactionCase, tagged

# Frozen env names (plan §"軟契約 env 名" table).
_ENV_WEBHOOK_CURRENT = "AGENT_RUNTIME_WEBHOOK_SECRET"
_ENV_WEBHOOK_PREVIOUS = "AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS"
_ENV_NAMESPACE_SECRET = "AGENT_RUNTIME_NAMESPACE_SECRET"

_CLIENT_MOD = "odoo.addons.woow_paas_platform.services.agent_runtime_client"
_SESSION_MOD = "odoo.addons.woow_paas_platform.models.agent_session"


def _env_guard(test, key, value):
    """Set an env var for the in-process controller/model, restore on teardown.

    ``value=None`` removes the var.
    """
    prev = os.environ.get(key)

    def _restore():
        if prev is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prev

    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    test.addCleanup(_restore)


# --------------------------------------------------------------------------- #
# F-PC-2 — webhook receiver dual-secret (current + previous rotation overlap)   #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseDWebhookDualSecret(HttpCase):
    """F-PC-2 — previous-secret-signed webhook must verify (§6.6 rotation)."""

    _ENDPOINT = '/api/agent-runtimes/webhook/state-update'
    _CURRENT = "phase-d-webhook-current-aaaa"
    _PREVIOUS = "phase-d-webhook-previous-bbbb"

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]
        _env_guard(self, _ENV_WEBHOOK_CURRENT, self._CURRENT)
        _env_guard(self, _ENV_WEBHOOK_PREVIOUS, self._PREVIOUS)
        # forward-compat with the F-PC-3 fail-closed fix (this test creates a
        # session, which will require the namespace secret once F-PC-3 lands).
        _env_guard(self, _ENV_NAMESPACE_SECRET, "phase-d-namespace-secret-cccc")
        self.project = self.env['project.project'].sudo().create(
            {'name': f'whd-{self.suffix}'})
        self.session = self.env['woow_paas_platform.agent_session'].sudo().create({
            'project_id': self.project.id,
            'state': 'spawning',
        })
        self.hash = self.session.project_namespace_hash

    def _post(self, secret):
        body = json.dumps({
            'project_hash': self.hash,
            'new_state': 'active',
            'ts': '2026-05-24T00:00:00Z',
        }).encode()
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return self.url_open(
            self._ENDPOINT, data=body,
            headers={'Content-Type': 'application/json',
                     'X-PaaS-Signature': sig},
            timeout=15,
        )

    def test_FPC2_previous_secret_signed_accepted(self):
        self.assertTrue(self.hash, "fixture: session must have a namespace hash")
        resp = self._post(self._PREVIOUS)
        self.assertEqual(
            resp.status_code, 200,
            "F-PC-2: a webhook signed with the PREVIOUS secret must verify "
            "during rotation overlap (dual-env fallback, §6.6). Today the "
            "receiver only checks AGENT_RUNTIME_WEBHOOK_SECRET (current) so "
            "the previous-signed body is rejected. Got %s." % resp.status_code,
        )

    def test_FPC2_current_secret_still_accepted(self):
        """Regression: current-secret-signed stays 200."""
        resp = self._post(self._CURRENT)
        self.assertEqual(resp.status_code, 200,
                         "current-secret webhook must stay 200, got %s."
                         % resp.status_code)

    def test_FPC2_unknown_secret_rejected(self):
        """Regression: a body signed with neither secret is still 401."""
        resp = self._post("totally-wrong-secret")
        self.assertEqual(resp.status_code, 401,
                         "a body signed with neither secret must be 401, got %s."
                         % resp.status_code)


# --------------------------------------------------------------------------- #
# F-PC-3 — agent_session namespace secret from ENV, fail-closed                  #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseDNamespaceSecretEnv(TransactionCase):
    """F-PC-3 — namespace hash derives from env secret; missing env → raise."""

    def _make_project(self):
        return self.env['project.project'].sudo().create(
            {'name': f'nsenv-{uuid.uuid4().hex[:6]}'})

    def test_FPC3_hash_uses_env_secret_not_default(self):
        import importlib
        mod = importlib.import_module(_SESSION_MOD)
        env_secret = "phase-d-env-namespace-secret-zzzz"
        _env_guard(self, _ENV_NAMESPACE_SECRET, env_secret)
        # ensure the legacy ir.config_parameter is NOT what's used.
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.agent_namespace_secret', '')

        project = self._make_project()
        session = self.env['woow_paas_platform.agent_session'].sudo().create({
            'project_id': project.id,
        })
        expected = mod._compute_project_namespace_hash(
            project.id, env_secret.encode())
        self.assertEqual(
            session.project_namespace_hash, expected,
            "F-PC-3: the namespace hash must be derived from the injected "
            "AGENT_RUNTIME_NAMESPACE_SECRET env. Today _get_namespace_secret "
            "ignores the env and falls back to the source-public "
            "_DEFAULT_SECRET, so the stored hash does not match the "
            "env-derived one.",
        )

    def test_FPC3_missing_env_fails_closed(self):
        # neither the env nor the legacy ICP param is set.
        _env_guard(self, _ENV_NAMESPACE_SECRET, None)
        self.env['ir.config_parameter'].sudo().set_param(
            'woow_paas_platform.agent_namespace_secret', '')
        project = self._make_project()
        with self.assertRaises(Exception, msg=(
            "F-PC-3: with no AGENT_RUNTIME_NAMESPACE_SECRET configured, "
            "creating an agent_session must FAIL CLOSED (raise) instead of "
            "silently computing the hash with the source-public "
            "_DEFAULT_SECRET fallback.")):
            self.env['woow_paas_platform.agent_session'].sudo().create({
                'project_id': project.id,
            })


# --------------------------------------------------------------------------- #
# F-PC-5 — agent_runtime_client raises a typed error on operator 4xx/5xx        #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseDClientTypedError(TransactionCase):
    """F-PC-5 — operator error responses must surface as raised typed errors."""

    def setUp(self):
        super().setUp()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param('woow_paas_platform.operator_url', 'http://operator.test:8000')
        ICP.set_param('woow_paas_platform.operator_api_key', 'k')
        import importlib
        self.mod = importlib.import_module(_CLIENT_MOD)
        self.client = self.mod.get_agent_runtime_client(self.env)
        self.assertIsNotNone(self.client, "fixture: client must be constructible")

    def _resp(self, status, payload):
        resp = MagicMock()
        resp.status_code = status
        resp.ok = 200 <= status < 300
        resp.json.return_value = payload
        resp.text = json.dumps(payload)
        return resp

    def test_FPC5_4xx_raises_with_operator_detail(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._resp(
                401, {'detail': 'invalid api key'})
            with self.assertRaises(Exception, msg=(
                "F-PC-5: a 401 from the operator must raise a typed error "
                "(today _request returns resp.json() unconditionally → the "
                "error dict is swallowed silently).")) as cm:
                self.client.get_status('pa1234567890abcd')
            self.assertIn(
                'invalid api key', str(cm.exception).lower(),
                "F-PC-5: the raised error must carry the operator 'detail'.",
            )

    def test_FPC5_5xx_raises_with_status(self):
        with patch.object(self.mod, 'requests', create=True) as mock_requests:
            mock_requests.request.return_value = self._resp(
                500, {'detail': 'internal boom'})
            with self.assertRaises(Exception, msg=(
                "F-PC-5: a 500 from the operator must raise a typed error, "
                "not silently return the body dict.")) as cm:
                self.client.reap('pa1234567890abcd')
            msg = str(cm.exception).lower()
            self.assertTrue(
                '500' in msg or 'internal boom' in msg,
                "F-PC-5: the raised 5xx error must carry the status/detail "
                "(got %r)." % str(cm.exception),
            )


# --------------------------------------------------------------------------- #
# F-PA-14 — project bootstrap honours an explicit owner-partner hint via sudo   #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseDBootstrapOwnerHint(TransactionCase):
    """F-PA-14 — sudo create + owner hint must bootstrap that owner (HC-2)."""

    def setUp(self):
        super().setUp()
        if 'woow_paas_platform.project_access' not in self.env.registry:
            self.skipTest("project_access model not installed")
        self.PAccess = self.env['woow_paas_platform.project_access']

    def _owner_rows(self, project, partner):
        return self.PAccess.sudo().search_count([
            ('project_id', '=', project.id),
            ('partner_id', '=', partner.id),
            ('role', '=', 'owner'),
        ])

    def test_FPA14_sudo_create_with_hint_bootstraps_owner(self):
        partner = self.env['res.partner'].sudo().create(
            {'name': f'owner-hint-{uuid.uuid4().hex[:6]}'})
        project = self.env['project.project'].sudo().with_context(
            bootstrap_owner_partner_id=partner.id,
        ).create({'name': f'fpa14-{uuid.uuid4().hex[:6]}'})
        self.assertTrue(
            self._owner_rows(project, partner),
            "F-PA-14: a trusted sudo().create carrying "
            "bootstrap_owner_partner_id must give that partner an 'owner' "
            "project_access row. Today the SUPERUSER branch skips bootstrap "
            "entirely, so the real human owner gets no access row.",
        )

    def test_FPA14_sudo_create_without_hint_no_auto_owner(self):
        """Regression: a bare sudo create must NOT auto-own (HC-2 intent)."""
        superuser_partner = self.env.ref('base.user_root').partner_id
        project = self.env['project.project'].sudo().create(
            {'name': f'fpa14n-{uuid.uuid4().hex[:6]}'})
        self.assertFalse(
            self._owner_rows(project, superuser_partner),
            "Regression: without an owner hint a SUPERUSER create must NOT "
            "make __system__ an owner (the fix must stay a hint, not a "
            "blanket sudo-bootstrap).",
        )

    def test_FPA14_normal_user_hint_ignored_self_owns(self):
        """Regression: a normal (non-trusted) create IGNORES the owner hint.

        The hint is trusted ONLY on the SUPERUSER path — a genuine root env
        (uid == 1), NOT ``.sudo()`` (#547: sudo leaves uid untouched, see
        ``odoo/models.py:6290-6291``). A normal caller must not be able to
        assign 'owner' to an arbitrary partner via
        ``bootstrap_owner_partner_id``. A normal user's create owns ITSELF and
        the hinted third-party partner gets no owner row. This locks the
        /simplify tightening — without it, reverting the hint to "wins on any
        path" would not go red.
        """
        suffix = uuid.uuid4().hex[:6]
        victim = self.env['res.partner'].sudo().create({'name': f'victim-{suffix}'})
        user = self.env['res.users'].sudo().create({
            'name': f'fpa14-normal-{suffix}',
            'login': f'fpa14normal_{uuid.uuid4().hex[:8]}',
            'groups_id': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('project.group_project_manager').id,
            ])],
        })
        project = self.env['project.project'].with_user(user).with_context(
            bootstrap_owner_partner_id=victim.id,
        ).create({'name': f'fpa14u-{suffix}'})
        self.assertTrue(
            self._owner_rows(project, user.partner_id),
            "F-PA-14 guard: a normal user's create must own ITSELF.",
        )
        self.assertFalse(
            self._owner_rows(project, victim),
            "F-PA-14 guard: a normal user must NOT assign 'owner' to an "
            "arbitrary partner via bootstrap_owner_partner_id — the hint is "
            "trusted only on a genuine root env (uid == 1), not on .sudo().",
        )

    def test_FPA14_non_root_sudo_create_self_owns(self):
        """#547 — a NON-ROOT user's ``.sudo().create()`` must own ITSELF.

        This locks the branch selector in ``project_project.create()``
        (``if self.env.uid == SUPERUSER_ID:``). It is a *branch selector*, not a
        bypass guard: ``sudo()`` sets ``env.su`` but leaves ``uid`` alone
        (``odoo/models.py:6290-6291`` — "The superuser mode does not change the
        current user"), so a tenant's ``.sudo().create()`` must fall into the
        else-branch and take ``env.user.partner_id`` as owner. Flipping the
        selector to ``env.is_superuser()`` routes it into the SUPERUSER branch,
        which returns early with no hint → the project gets ZERO project_access
        rows.

        Call-order matters and IS the discriminating part:
        ``with_user(non-root)`` sets ``su=False`` (``odoo/api.py`` only
        auto-sets ``su`` when ``uid == SUPERUSER_ID``); the following
        ``.sudo()`` flips ``su`` back to True while ``uid`` stays that user.
        Reversing it — or calling ``.sudo()`` on the bare ``TransactionCase``
        env, whose uid is already 1 — makes ``sudo()`` a no-op
        (``odoo/models.py``: ``if flag == self.env.su: return self``) and the
        test degenerates into one that passes under BOTH predicates. The three
        older F-PA-14 tests have exactly that shape, which is why none of them
        covers this line.
        """
        suffix = uuid.uuid4().hex[:6]
        user = self.env['res.users'].sudo().create({
            'name': f'fpa14-sudo-{suffix}',
            'login': f'fpa14sudo_{uuid.uuid4().hex[:8]}',
            'groups_id': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('project.group_project_manager').id,
            ])],
        })
        # Premise assertion: this test's discriminating power rests entirely on
        # `user` being non-root. If a later edit swaps the fixture for
        # with_user(1) — or drops with_user() so uid falls back to the env
        # default — the assertions below would keep passing for the wrong
        # reason. Assert the premise, not just the conclusion.
        self.assertNotEqual(
            user.id, SUPERUSER_ID,
            "premise broken: the fixture user must NOT be root, otherwise "
            "both uid-comparison and is_superuser() take the same branch and "
            "this test silently degenerates into a tautology.",
        )

        scoped = self.env['project.project'].with_user(user)
        self.assertFalse(
            scoped.env.su,
            "premise broken: with_user(non-root) must leave su=False before "
            "the .sudo() below flips it — otherwise the su transition under "
            "test never happens.",
        )
        project = scoped.sudo().create({'name': f'fpa14s-{suffix}'})
        self.assertEqual(
            project.create_uid.id, user.id,
            "sudo() must not change create_uid (odoo/models.py:5130 does "
            "vals.setdefault('create_uid', self.env.uid) and sudo() leaves uid "
            "alone) — if this fails the whole premise of this test is void.",
        )
        self.assertTrue(
            self._owner_rows(project, user.partner_id),
            "#547: a non-root .sudo().create() must give the CALLER an 'owner' "
            "project_access row. If this goes red, the branch selector in "
            "project_project.create() was probably switched to "
            "env.is_superuser() — which sends every tenant sudo create into "
            "the SUPERUSER branch and leaves the project with no access row "
            "at all (invisible / unreachable / undeletable).",
        )
