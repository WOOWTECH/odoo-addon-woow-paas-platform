"""Skill bundle download endpoint for agent-runtime pods (design §2.11 / §3.3).

Frozen contract (paas-agent-runtime image ``runtime/skills/cache.py``):

    GET /api/agent-runtimes/skill-bundle
          ?project_hash={hash}&skill_name={name}&version={ver}
    header: Authorization: Bearer {SKILL_BUNDLE_TOKEN}
    → 200 returns the RAW bundle bytes (the pod does resp.read() then sha256);
      NOT base64, NOT a JSON wrapper.
    → any 4xx/5xx raises SkillFetchError pod-side.

Auth: HS256 token verified against the shared secret injected from the K8s
Secret (env ``AGENT_RUNTIME_AUTH_SECRET_CURRENT`` / ``_PREVIOUS``; OQ#1 ruling
(b) + §6.6 dual-secret rotation window). The token's ``project_id`` claim is
AUTHORITATIVE (HC-2 [[constraint-input-trust-expansion]]) — the query
``project_hash`` is never trusted for project resolution.

Dual-scope (2026-05-30): a scope='system' skill (project_id NULL) is
fetchable by ANY valid skill_bundle token — system skills are platform-
curated and intentionally global. The project-bound leg stays strict
(token project_id authoritative); project rows shadow system on collision.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

from odoo import http
from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)

_ENV_SECRET_CURRENT = "AGENT_RUNTIME_AUTH_SECRET_CURRENT"
_ENV_SECRET_PREVIOUS = "AGENT_RUNTIME_AUTH_SECRET_PREVIOUS"


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _decode_verified_hs256(token: str, secret: str):
    """Return the claims dict if the HS256 signature checks out, else None."""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError:
        return None
    signing_input = (header_b64 + "." + payload_b64).encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    try:
        actual = _b64url_decode(signature_b64)
    except Exception:
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    try:
        return json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None


def _verify_skill_bundle_token(token: str):
    """Verify a SKILL_BUNDLE_TOKEN against current then previous secret (§6.6).

    Returns the claims dict, or None if the signature is invalid against both
    secrets or the token is expired.
    """
    for secret in (
        os.environ.get(_ENV_SECRET_CURRENT, ""),
        os.environ.get(_ENV_SECRET_PREVIOUS, ""),
    ):
        if not secret:
            continue
        claims = _decode_verified_hs256(token, secret)
        if claims is None:
            continue
        if "exp" in claims and int(time.time()) >= int(claims["exp"]):
            return None
        return claims
    return None


class AgentRuntimeSkillController(Controller):
    """Serves published skill bundles to authenticated agent-runtime pods."""

    @route(
        '/api/agent-runtimes/skill-bundle',
        type='http', auth='none', methods=['GET'],
        csrf=False, save_session=False,
    )
    def skill_bundle(self, **kwargs):
        auth_header = request.httprequest.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return self._unauthorized()
        token = auth_header[len('Bearer '):].strip()

        claims = _verify_skill_bundle_token(token)
        if not claims:
            return self._unauthorized()

        # C-1 hardening (defense in depth): an authentic token is only usable
        # here if it was minted for the skill-bundle scope. A ws-scope token,
        # an unstamped token, or a forged token_use is rejected (allowlist,
        # fail-closed) so a ws token can never be replayed as a bundle fetch.
        if claims.get('token_use') != 'skill_bundle':
            return self._forbidden()

        # HC-2: the token's project_id is authoritative; the query param is not.
        project_id = claims.get('project_id')
        if not project_id:
            return self._unauthorized()

        candidates = request.env['woow_paas_platform.skill'].sudo().search([
            '&', '&',
            ('name', '=', kwargs.get('skill_name')),
            ('version', '=', kwargs.get('version')),
            '|', ('project_id', '=', project_id), ('scope', '=', 'system'),
        ])
        # HC-2: the token's project_id is authoritative for the bound leg.
        # On a (name, version) collision the project-owned row wins over system.
        skill = candidates.filtered(lambda s: s.project_id.id == project_id)[:1] \
            or candidates.filtered(lambda s: s.scope == 'system')[:1]
        if not skill or skill.state != 'published':
            return self._not_found()

        attachment = skill.bundle_attachment_id
        if not attachment or not attachment.datas:
            return self._not_found()

        raw_bytes = base64.b64decode(attachment.datas)
        return request.make_response(
            raw_bytes,
            headers=[
                ('Content-Type', attachment.mimetype or 'application/gzip'),
                ('Content-Length', str(len(raw_bytes))),
                ('Cache-Control', 'no-store'),
            ],
        )

    def _unauthorized(self):
        return http.Response('Unauthorized', status=401, content_type='text/plain')

    def _forbidden(self):
        return http.Response('Forbidden', status=403, content_type='text/plain')

    def _not_found(self):
        return http.Response('Not Found', status=404, content_type='text/plain')
