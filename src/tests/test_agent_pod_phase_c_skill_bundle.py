"""TDD red phase — Phase C 塊②: skill-bundle download endpoint + agent_session field.

Plan : docs/plans/2026-Q2/agent-runtime-pod-plan.md §Phase C Tasks 8-9
Design:
  - §2.11   skill bundle fetch is the Phase B ``cache.py`` assumed endpoint
  - §3.3     storage backend (ir.attachment binary)
  - §5.3     HS256 dual-secret token (skill_bundle_token scope)
  - §6.6     replay / dual-secret rotation window

FROZEN CONTRACT (leader cross-checked paas-agent-runtime runtime/skills/cache.py
``_fetch_bundle``) — zero guessing:

  GET {ODOO_BASE_URL}/api/agent-runtimes/skill-bundle
        ?project_hash={hash}&skill_name={name}&version={ver}
  header: Authorization: Bearer {SKILL_BUNDLE_TOKEN}
  → 200 returns the *raw* bundle bytes (``resp.read()``); the runtime then
    runs ``hashlib.sha256(data)`` against the expected sha — so the body MUST
    be the raw tar.gz bytes, NOT base64, NOT a JSON wrapper.
  → any 4xx/5xx raises SkillFetchError on the pod side.

Acceptance criteria covered:

AS.1  ``agent_session.last_synced_at`` Datetime field exists (readonly).
AS.2  ``agent_session.last_state_event_ts`` Datetime field exists (readonly).
AS.3  Both columns are additive + NULLABLE on the DB table (no NOT NULL,
      no backfill) — existing rows survive migration 27.
MIG.1 ``__manifest__.py`` version bumped 18.0.1.0.26 → 18.0.1.0.27 (without
      it Odoo ignores the 27 data + cron + migration).

SB.1  Valid SKILL_BUNDLE_TOKEN + published skill → 200 returning the RAW
      bundle bytes (== the attachment's decoded bytes, not base64).
SB.2  Missing / malformed Authorization header → 401.
SB.3  Expired token → 401.
SB.4  TOKEN AUTHORITATIVE (HC-2 ruling [[constraint-input-trust-expansion]]):
      project resolution uses the token's ``project_id`` claim, NOT the
      ``project_hash`` query param. A token for project A serving A's skill
      succeeds even when the query ``project_hash`` points at project B; and
      a token for project B requesting A's skill_name fails (B has no such
      skill) regardless of a query ``project_hash`` that names A.
SB.5  Skill not in the token's project → non-200 (403/404) — no cross-project
      bundle exfil.
SB.6  Skill present but state != 'published' (draft) → 404.

Red expectation today (Phase C impl has NOT started):

* AS.1 / AS.2 → RED: field missing from ``agent_session._fields``.
* AS.3 → RED: DB columns absent (information_schema query empty).
* MIG.1 → RED: manifest still declares 18.0.1.0.26.
* SB.* → RED: controller route ``/api/agent-runtimes/skill-bundle`` does
  not exist yet → Odoo returns 404 for every request, so the 200 / 401
  expectations all fail. Once the controller ships these become the green
  spec (HS256 verify + token-authoritative project resolution + published
  + raw-bytes body).

Fixture / env assumptions:

* Phase A new models in registry (skill + project_access + agent_session).
* Shared HS256 secret read by the endpoint from ir.config_parameter key
  ``woow_paas_platform.agent_runtime_auth_secret`` (current). This mirrors
  the existing operator_url / operator_api_key config_parameter pattern.
  → see Open question: if impl reads the secret from a mounted K8s Secret
    file instead, the secret-source line in setUp needs aligning (the route
    contract + token-authoritative behaviour are unaffected).
* Tokens are HS256-signed with pure stdlib here (hmac+base64) so the test
  does not depend on PyJWT being installed yet (PyJWT lands in block ④).

Boundary (intentionally NOT tested here):

* paas-operator-side token signing — block ① ``test_agent_runtimes.py``.
* HC-4 ``/web/content`` side-channel — already locked by
  ``test_agent_pod_phase_a_attachment_acl.py`` (this endpoint is the
  *legitimate* bundle path; HC-4 guards the illegitimate one).
* SHA256 integrity verification — pod-side ``cache.py`` responsibility.
* drift cron + webhook receiver — block ③.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid

from odoo.tests.common import HttpCase, TransactionCase, tagged


# --------------------------------------------------------------------------- #
# stdlib HS256 JWT signer (no PyJWT dependency)                                 #
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_hs256(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    seg = _b64url(json.dumps(header).encode()) + "." + _b64url(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64url(sig)


_AUTH_SECRET = "phase-c-skill-bundle-secret-0123456789"
_SECRET_PARAM = "woow_paas_platform.agent_runtime_auth_secret"


def _skill_bundle_token(project_hash, project_id, workspace_ids, secret,
                        exp_offset=3600):
    now = int(time.time())
    payload = {
        "iss": "paas-operator",
        "project_hash": project_hash,
        "project_id": project_id,
        "workspace_ids": list(workspace_ids),
        "iat": now,
        "exp": now + exp_offset,
        "jti": uuid.uuid4().hex,
    }
    return _sign_hs256(payload, secret)


# --------------------------------------------------------------------------- #
# AS.* / MIG.1 — agent_session new fields + migration version bump              #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCAgentSessionFields(TransactionCase):
    """AS.1-AS.3 + MIG.1 — webhook-sync field additions (block ②)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if 'woow_paas_platform.agent_session' not in cls.env.registry:
            raise RuntimeError(
                "Phase C block② prerequisite: 'woow_paas_platform."
                "agent_session' not registered (Phase A must be merged)."
            )
        cls.Session = cls.env['woow_paas_platform.agent_session']

    def test_AS1_last_synced_at_field(self):
        """AS.1: agent_session.last_synced_at Datetime (readonly)."""
        self.assertIn(
            'last_synced_at', self.Session._fields,
            "AS.1: agent_session.last_synced_at missing. Plan §Phase C "
            "Task 8 — webhook/cron 更新時戳.",
        )
        self.assertEqual(self.Session._fields['last_synced_at'].type, 'datetime')

    def test_AS2_last_state_event_ts_field(self):
        """AS.2: agent_session.last_state_event_ts Datetime (readonly)."""
        self.assertIn(
            'last_state_event_ts', self.Session._fields,
            "AS.2: agent_session.last_state_event_ts missing. Plan §Phase C "
            "Task 8 — 最後一次 paas-operator state event ts (drift detection).",
        )
        self.assertEqual(
            self.Session._fields['last_state_event_ts'].type, 'datetime',
        )

    def test_AS3_columns_additive_nullable(self):
        """AS.3: both columns exist + NULLABLE on the DB table (no backfill)."""
        self.env.cr.execute(
            """
            SELECT column_name, is_nullable
              FROM information_schema.columns
             WHERE table_name = 'woow_paas_platform_agent_session'
               AND column_name IN ('last_synced_at', 'last_state_event_ts')
            """
        )
        rows = {r[0]: r[1] for r in self.env.cr.fetchall()}
        self.assertIn(
            'last_synced_at', rows,
            "AS.3: column last_synced_at not created (migration 27 / ORM "
            "auto-add). Additive nullable column expected.",
        )
        self.assertIn('last_state_event_ts', rows)
        self.assertEqual(
            rows.get('last_synced_at'), 'YES',
            "AS.3: last_synced_at must be NULLABLE (additive, no backfill).",
        )
        self.assertEqual(rows.get('last_state_event_ts'), 'YES')

    def test_MIG1_manifest_version_bumped_to_27(self):
        """MIG.1: __manifest__.py declares 18.0.1.0.27."""
        manifest_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', '__manifest__.py')
        )
        with open(manifest_path, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn(
            '"version": "18.0.1.0.27"', content,
            "MIG.1: __manifest__.py must bump version to '18.0.1.0.27' "
            "(currently 18.0.1.0.26). Plan §Phase C Task 13 — without the "
            "bump Odoo ignores the new cron data + migration 27.",
        )


# --------------------------------------------------------------------------- #
# SB.* — skill bundle download endpoint (frozen contract)                       #
# --------------------------------------------------------------------------- #
@tagged('post_install', '-at_install')
class TestPhaseCSkillBundleEndpoint(HttpCase):
    """SB.1-SB.6 — GET /api/agent-runtimes/skill-bundle (Phase B cache.py contract)."""

    _ENDPOINT = '/api/agent-runtimes/skill-bundle'

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for required in (
            'woow_paas_platform.skill',
            'woow_paas_platform.project_access',
        ):
            if required not in cls.env.registry:
                raise RuntimeError(
                    f"Phase C block② prerequisite: model '{required}' not "
                    f"registered (Phase A must be merged)."
                )
        cls.Project = cls.env['project.project']
        cls.Skill = cls.env['woow_paas_platform.skill']
        cls.Attachment = cls.env['ir.attachment']

    def setUp(self):
        super().setUp()
        self.suffix = uuid.uuid4().hex[:8]

        # Endpoint reads the shared HS256 secret from this config_parameter.
        self.env['ir.config_parameter'].sudo().set_param(
            _SECRET_PARAM, _AUTH_SECRET,
        )

        # Two projects: A holds the published skill; B is the "other" project
        # used to prove the token (not the query param) decides resolution.
        self.project_a = self.Project.sudo().create({'name': f'C-A {self.suffix}'})
        self.project_b = self.Project.sudo().create({'name': f'C-B {self.suffix}'})

        self.bundle_bytes = (
            b'\x1f\x8b\x08RAW-skill-bundle-bytes-' + self.suffix.encode()
        )
        self.skill = self.Skill.sudo().create({
            'name': f'deploy-{self.suffix}',
            'version': '1.0.0',
            'project_id': self.project_a.id,
            'bundle_sha256': hashlib.sha256(self.bundle_bytes).hexdigest(),
            'state': 'published',
        })
        self.attachment = self.Attachment.sudo().create({
            'name': f'{self.skill.name}.tar.gz',
            'datas': base64.b64encode(self.bundle_bytes),
            'res_model': 'woow_paas_platform.skill',
            'res_id': self.skill.id,
            'mimetype': 'application/gzip',
            'public': False,
        })
        self.skill.sudo().write({'bundle_attachment_id': self.attachment.id})

        # A wrong-but-plausible namespace hash for the query param (token
        # authoritative → this must be ignored).
        self.wrong_hash = 'pa' + uuid.uuid4().hex[:14]

    def _url(self, project_hash, skill_name, version):
        from urllib.parse import urlencode
        q = urlencode({
            'project_hash': project_hash,
            'skill_name': skill_name,
            'version': version,
        })
        return f'{self._ENDPOINT}?{q}'

    def _get(self, url, token=None):
        headers = {}
        if token is not None:
            headers['Authorization'] = f'Bearer {token}'
        return self.url_open(url, headers=headers, timeout=15)

    # ----- SB.1 valid token → 200 raw bytes ------------------------------- #
    def test_SB1_valid_token_returns_raw_bundle_bytes(self):
        token = _skill_bundle_token(
            self.wrong_hash, self.project_a.id, [], _AUTH_SECRET,
        )
        resp = self._get(
            self._url(self.wrong_hash, self.skill.name, '1.0.0'), token,
        )
        self.assertEqual(
            resp.status_code, 200,
            "SB.1: valid SKILL_BUNDLE_TOKEN + published skill must return "
            "200. Got %s. Endpoint /api/agent-runtimes/skill-bundle not "
            "implemented yet (frozen contract — Phase B cache.py)."
            % resp.status_code,
        )
        self.assertEqual(
            resp.content, self.bundle_bytes,
            "SB.1: body must be the RAW bundle bytes (cache.py does "
            "resp.read() then sha256) — NOT base64, NOT a JSON wrapper.",
        )
        # Defensive: ensure it isn't accidentally the base64 representation.
        self.assertNotEqual(
            resp.content, base64.b64encode(self.bundle_bytes),
            "SB.1: body must not be base64-encoded — pod hashes raw bytes.",
        )

    # ----- SB.2 missing token → 401 --------------------------------------- #
    def test_SB2_missing_token_unauthorized(self):
        resp = self._get(
            self._url(self.wrong_hash, self.skill.name, '1.0.0'), token=None,
        )
        self.assertEqual(
            resp.status_code, 401,
            "SB.2: missing Authorization header must be 401, got %s."
            % resp.status_code,
        )

    # ----- SB.3 expired token → 401 --------------------------------------- #
    def test_SB3_expired_token_unauthorized(self):
        token = _skill_bundle_token(
            self.wrong_hash, self.project_a.id, [], _AUTH_SECRET,
            exp_offset=-30,
        )
        resp = self._get(
            self._url(self.wrong_hash, self.skill.name, '1.0.0'), token,
        )
        self.assertEqual(
            resp.status_code, 401,
            "SB.3: expired token must be 401, got %s." % resp.status_code,
        )

    # ----- SB.3b wrong-secret token → 401 --------------------------------- #
    def test_SB3b_bad_signature_unauthorized(self):
        token = _skill_bundle_token(
            self.wrong_hash, self.project_a.id, [], 'a-different-secret',
        )
        resp = self._get(
            self._url(self.wrong_hash, self.skill.name, '1.0.0'), token,
        )
        self.assertEqual(
            resp.status_code, 401,
            "SB.3b: token signed with an unknown secret must be 401 "
            "(HS256 verify against current/previous), got %s."
            % resp.status_code,
        )

    # ----- SB.4 token authoritative (HC-2) -------------------------------- #
    def test_SB4_token_authoritative_serves_token_project(self):
        """Token for project A serves A's skill even when query
        project_hash names something else — query param is NOT trusted."""
        token = _skill_bundle_token(
            self.wrong_hash, self.project_a.id, [], _AUTH_SECRET,
        )
        # query project_hash deliberately a different/fabricated hash
        resp = self._get(
            self._url('pa-some-other-project-hash', self.skill.name, '1.0.0'),
            token,
        )
        self.assertEqual(
            resp.status_code, 200,
            "SB.4: project resolution must use the token's project_id "
            "claim, not the query project_hash (HC-2). Got %s."
            % resp.status_code,
        )
        self.assertEqual(resp.content, self.bundle_bytes)

    def test_SB4b_query_param_cannot_redirect_to_foreign_project(self):
        """A token for project B (which has no such skill) requesting A's
        skill_name must fail — proving the query project_hash (even if it
        names A) cannot grant access to A's skill."""
        token_b = _skill_bundle_token(
            self.wrong_hash, self.project_b.id, [], _AUTH_SECRET,
        )
        # Compute A's plausible hash is unnecessary — token (project B) wins.
        resp = self._get(
            self._url(self.wrong_hash, self.skill.name, '1.0.0'), token_b,
        )
        self.assertNotEqual(
            resp.status_code, 200,
            "SB.4b: token for project B must NOT obtain project A's skill "
            "bundle regardless of query param (HC-2 token-authoritative). "
            "Got 200 — cross-project bundle exfil.",
        )

    # ----- SB.5 skill not in token's project → non-200 -------------------- #
    def test_SB5_skill_outside_token_project_denied(self):
        token_b = _skill_bundle_token(
            self.wrong_hash, self.project_b.id, [], _AUTH_SECRET,
        )
        resp = self._get(
            self._url(self.wrong_hash, self.skill.name, '1.0.0'), token_b,
        )
        self.assertIn(
            resp.status_code, (403, 404),
            "SB.5: skill not belonging to the token's project must be "
            "403/404 (no cross-project access). Got %s." % resp.status_code,
        )

    # ----- SB.6 unpublished skill → 404 ----------------------------------- #
    def test_SB6_unpublished_skill_not_found(self):
        self.skill.sudo().write({'state': 'draft'})
        token = _skill_bundle_token(
            self.wrong_hash, self.project_a.id, [], _AUTH_SECRET,
        )
        resp = self._get(
            self._url(self.wrong_hash, self.skill.name, '1.0.0'), token,
        )
        self.assertEqual(
            resp.status_code, 404,
            "SB.6: a skill in state != 'published' (draft) must be 404 "
            "(only published bundles are fetchable). Got %s."
            % resp.status_code,
        )
