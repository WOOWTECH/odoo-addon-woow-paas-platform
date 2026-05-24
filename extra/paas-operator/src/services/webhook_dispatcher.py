"""Outbound pod→Odoo state-update webhook dispatcher (design §5.5).

Signs the JSON body with HMAC-SHA256 under ``agent-runtime-webhook-secret`` and
POSTs to the Odoo receiver, retrying up to 3x with exponential backoff on 5xx.
"""
import hashlib
import hmac
import json
import logging
import time

import httpx

logger = logging.getLogger(__name__)

_WEBHOOK_PATH = "/api/agent-runtimes/webhook/state-update"
_MAX_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 1


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
