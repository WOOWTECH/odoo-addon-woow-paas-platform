"""Outbound pod→Odoo state-update webhook dispatcher (design §5.5).

Signs the JSON body with HMAC-SHA256 under ``agent-runtime-webhook-secret`` and
POSTs to the Odoo receiver, retrying up to 3x with exponential backoff on 5xx.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

_WEBHOOK_PATH = "/api/agent-runtimes/webhook/state-update"
_MAX_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 1
_ENV_WEBHOOK_SECRET = "AGENT_RUNTIME_WEBHOOK_SECRET"


def _http_post(url, data=None, headers=None, timeout=None):
    """Thin httpx POST wrapper (patched out in unit tests)."""
    return httpx.post(url, content=data, headers=headers, timeout=timeout)


def dispatch_state_update(odoo_base_url, payload, secret):
    """Sign + POST a state-update webhook, retrying 5xx with backoff.

    Returns the last HTTP response.
    """
    body = json.dumps(payload)
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-PaaS-Signature": "sha256=" + signature,
    }
    url = odoo_base_url.rstrip("/") + _WEBHOOK_PATH

    backoff = _BASE_BACKOFF_SECONDS
    response = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        response = _http_post(url, data=body, headers=headers, timeout=10)
        if response.status_code < 500:
            return response
        logger.warning(
            "Webhook delivery attempt %s/%s got %s for %s",
            attempt, _MAX_ATTEMPTS, response.status_code, url,
        )
        if attempt < _MAX_ATTEMPTS:
            time.sleep(backoff)
            backoff *= 2
    return response


def _webhook_secret() -> str:
    """Current outbound webhook signing secret (mirrors jwt_signer._current_secret)."""
    return os.environ.get(_ENV_WEBHOOK_SECRET, "")


def notify_state_update(odoo_base_url, project_hash, new_state, pod_ws_url=None):
    """Best-effort §5.5 state-update notify. No-op when unconfigured; never raises.

    Designed to run inside a FastAPI BackgroundTask: it must NEVER raise into the
    request path. Skips entirely when odoo_base_url or the webhook secret is unset
    (an unsigned webhook would 401 anyway).
    """
    secret = _webhook_secret()
    if not odoo_base_url or not secret:
        logger.info(
            "state-update notify skipped (odoo_base_url/secret unset) for %s -> %s",
            project_hash, new_state,
        )
        return
    payload = {
        "project_hash": project_hash,
        "new_state": new_state,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if pod_ws_url:
        payload["pod_ws_url"] = pod_ws_url
    try:
        resp = dispatch_state_update(odoo_base_url, payload, secret)
        if resp is not None and resp.status_code >= 400:
            logger.warning(
                "state-update notify got HTTP %s for %s -> %s",
                resp.status_code, project_hash, new_state,
            )
    except Exception:
        logger.warning(
            "state-update notify failed for %s -> %s", project_hash, new_state,
            exc_info=True,
        )
