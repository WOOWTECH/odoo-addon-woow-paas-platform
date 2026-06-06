"""HS256 token signer / verifier for agent-runtime auth (design §5.3 / §6.6).

Two token scopes share one HS256 shared secret (K8s Secret
``agent-runtime-auth-secret`` → env ``AGENT_RUNTIME_AUTH_SECRET_CURRENT`` /
``_PREVIOUS``):

* ``ws_token`` — per-invocation; carries channel_id + invoker (Odoo controller
  forwards it to the pod WS; the pod verifies).
* ``skill_bundle_token`` — per-pod-lifetime; carries the *authoritative*
  project_id + workspace_ids the Odoo skill-bundle endpoint decodes (HC-2:
  the endpoint trusts the token, never the query param).

Pure stdlib HS256 (no PyJWT dependency) so the operator carries no extra crypto
dep and the claim shape matches what the pod / Odoo verify with stdlib too.

``verify_token`` is the operator-side helper (dual-secret rotation window +
in-process jti replay cache, §6.6). The pod ships its own verify in the
paas-agent-runtime image; this is the operator's own helper, unit-tested here.
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from threading import Lock

_ISS = "paas-operator"
_AUD = "agent-runtime"

# Replay-cache TTL is sized for the ws_token only (1h exp + 5 min clock-skew
# buffer = 65 min, §6.6). The operator's verify_token / jti cache verifies
# ws_tokens; the skill_bundle_token does NOT flow through this cache (it is
# verified Odoo-side, never re-presented to the operator), so the issue #155
# 7-day skill_bundle_token TTL does NOT widen this cache window and carries no
# memory-growth risk here. verify_token has no production caller in this repo
# today — it is the operator-side helper, unit-tested in test_agent_runtimes.py.
_JTI_TTL_SECONDS = 65 * 60
_JTI_SEEN: dict[str, float] = {}
_JTI_LOCK = Lock()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _encode_hs256(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (
        _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = hmac.new(
        secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return signing_input + "." + _b64url_encode(signature)


def _current_secret() -> str:
    return os.environ.get("AGENT_RUNTIME_AUTH_SECRET_CURRENT", "")


def _previous_secret() -> str:
    return os.environ.get("AGENT_RUNTIME_AUTH_SECRET_PREVIOUS", "")


def sign_ws_token(
    project_hash,
    channel_id,
    invoker_user_id,
    invoker_role,
    secret=None,
    exp_seconds=3600,
):
    """Sign a per-invocation WS token (no bundle-access project_id claim)."""
    secret = secret if secret is not None else _current_secret()
    now = int(time.time())
    payload = {
        "iss": _ISS,
        "aud": _AUD,
        "token_use": "ws",
        "project_hash": project_hash,
        "channel_id": channel_id,
        "invoker_user_id": invoker_user_id,
        "invoker_role": invoker_role,
        "iat": now,
        "exp": now + exp_seconds,
        "jti": uuid.uuid4().hex,
    }
    return _encode_hs256(payload, secret)


def sign_skill_bundle_token(
    project_hash,
    project_id,
    workspace_ids,
    secret=None,
    exp_seconds=3600,
):
    """Sign a per-pod-lifetime skill-bundle token.

    project_id + workspace_ids are the authoritative claims the Odoo
    skill-bundle endpoint decodes (HC-2). No per-invocation channel_id.
    """
    secret = secret if secret is not None else _current_secret()
    now = int(time.time())
    payload = {
        "iss": _ISS,
        "aud": _AUD,
        "token_use": "skill_bundle",
        "project_hash": project_hash,
        "project_id": project_id,
        "workspace_ids": list(workspace_ids or []),
        "iat": now,
        "exp": now + exp_seconds,
        "jti": uuid.uuid4().hex,
    }
    return _encode_hs256(payload, secret)


def _decode_verified(token: str, secret: str):
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


def _check_and_record_jti(jti: str) -> bool:
    """Return True if the jti is fresh; False if already seen (replay)."""
    now = time.time()
    with _JTI_LOCK:
        for seen, expiry in list(_JTI_SEEN.items()):
            if expiry <= now:
                del _JTI_SEEN[seen]
        if jti in _JTI_SEEN:
            return False
        _JTI_SEEN[jti] = now + _JTI_TTL_SECONDS
        return True


def verify_token(token, current=None, previous=None):
    """Verify an HS256 token against current then previous secret (§6.6).

    Raises ValueError on bad signature, expiry, or jti replay.
    """
    current = current if current is not None else _current_secret()
    previous = previous if previous is not None else _previous_secret()

    claims = None
    for secret in (current, previous):
        if not secret:
            continue
        decoded = _decode_verified(token, secret)
        if decoded is not None:
            claims = decoded
            break
    if claims is None:
        raise ValueError("Token signature invalid against current/previous secret")

    if "exp" in claims and int(time.time()) >= int(claims["exp"]):
        raise ValueError("Token expired")

    jti = claims.get("jti")
    if jti is not None and not _check_and_record_jti(jti):
        raise ValueError("Token replay detected (jti reused)")

    return claims
