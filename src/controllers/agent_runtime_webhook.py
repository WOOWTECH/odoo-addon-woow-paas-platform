"""Inbound paas-operator → Odoo state-update webhook (design §5.5).

POST /api/agent-runtimes/webhook/state-update
  header: X-PaaS-Signature: [sha256=]<hex hmac of the raw body>
  body  : {project_hash, new_state, ts, reason?, pod_ws_url?, spawn_age_ms?}

HMAC-SHA256 verified against the ``agent-runtime-webhook-secret`` injected from
the K8s Secret. Dual-env rotation overlap (§6.6): the signature must match the
CURRENT secret (``AGENT_RUNTIME_WEBHOOK_SECRET``) *or* the PREVIOUS one
(``AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS``), mirroring the skill-bundle
dual-secret fallback. NB the current secret keeps the bare name (no ``_CURRENT``
suffix) for backward-compat with the Phase C read site — the asymmetry is
intentional, not an oversight. On success the matching ``agent_session`` (by
project_namespace_hash) has its state + sync timestamps updated. Bad / missing
signature → 401; unknown hash → 404.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime

from odoo import fields, http
from odoo.http import request, route, Controller

_logger = logging.getLogger(__name__)

_ENV_WEBHOOK_SECRET = "AGENT_RUNTIME_WEBHOOK_SECRET"
_ENV_WEBHOOK_SECRET_PREVIOUS = "AGENT_RUNTIME_WEBHOOK_SECRET_PREVIOUS"


def _verify_hmac(body: bytes, signature_header: str, secret: str) -> bool:
    if not secret or not signature_header:
        return False
    provided = signature_header
    if provided.startswith('sha256='):
        provided = provided[len('sha256='):]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided)


def _verify_hmac_dual(body: bytes, signature_header: str) -> bool:
    """Accept a signature matching the current OR previous webhook secret.

    Rotation overlap (§6.6): during a secret rollover both secrets are valid;
    a body signed with either must verify. An empty / unset secret is skipped
    by ``_verify_hmac`` so an unconfigured slot never widens acceptance.
    """
    current = os.environ.get(_ENV_WEBHOOK_SECRET, '')
    previous = os.environ.get(_ENV_WEBHOOK_SECRET_PREVIOUS, '')
    return (
        _verify_hmac(body, signature_header, current)
        or _verify_hmac(body, signature_header, previous)
    )


def _parse_event_ts(ts):
    """Parse an ISO 8601 'Z' timestamp into a naive UTC datetime."""
    if not ts:
        return fields.Datetime.now()
    try:
        cleaned = ts.replace('Z', '').split('.')[0]
        return datetime.strptime(cleaned, '%Y-%m-%dT%H:%M:%S')
    except (ValueError, AttributeError):
        return fields.Datetime.now()


class AgentRuntimeWebhookController(Controller):
    """Receives pod lifecycle state updates from paas-operator."""

    @route(
        '/api/agent-runtimes/webhook/state-update',
        type='http', auth='none', methods=['POST'],
        csrf=False, save_session=False, readonly=False,
    )
    def state_update(self, **kwargs):
        body = request.httprequest.get_data()
        signature = request.httprequest.headers.get('X-PaaS-Signature', '')

        if not _verify_hmac_dual(body, signature):
            _logger.warning('Agent-runtime webhook: invalid/missing HMAC signature.')
            return http.Response('Unauthorized', status=401, content_type='text/plain')

        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return http.Response('Bad Request', status=400, content_type='text/plain')

        project_hash = payload.get('project_hash')
        session = request.env['woow_paas_platform.agent_session'].sudo().search([
            ('project_namespace_hash', '=', project_hash),
        ], limit=1)
        if not session:
            return http.Response('Not Found', status=404, content_type='text/plain')

        vals = {
            'last_synced_at': fields.Datetime.now(),
            'last_state_event_ts': _parse_event_ts(payload.get('ts')),
        }
        if payload.get('new_state'):
            vals['state'] = payload['new_state']
        if payload.get('pod_ws_url'):
            vals['pod_ws_url'] = payload['pod_ws_url']
        session.write(vals)

        return http.Response(
            json.dumps({'status': 'ok'}), status=200,
            content_type='application/json',
        )
