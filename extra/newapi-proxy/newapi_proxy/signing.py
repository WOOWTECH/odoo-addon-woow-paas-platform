"""HMAC request signature (DESIGN §1.4.2-1, N23).

The key never travels in a header. Odoo signs::

    method "\\n" canonical_path "\\n" canonical_query "\\n"
    hex(sha256(body)) "\\n" ts "\\n" identity "\\n" client_ip

with HMAC-SHA256 under the secret of ``X-Woow-Proxy-Kid`` and sends the
lowercase hex digest as ``X-Woow-Proxy-Sig``. ``client_ip`` is the
``X-Woow-Client-IP`` value for identity ``user`` and the empty string for every
other identity (which must not send that header).

``canonical_query`` is the query pairs sorted by key, joined as ``k=v`` with
``&`` and no percent-encoding (the proxy only accepts characters that never
need it). Odoo should send exactly that string as the query.

This module is the reference implementation; ``scripts/signed_request.py``
carries a stdlib-only copy that the unit tests keep in lockstep.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Iterable

KID_HEADER = "x-woow-proxy-kid"
TS_HEADER = "x-woow-proxy-ts"
SIG_HEADER = "x-woow-proxy-sig"
IDENTITY_HEADER = "x-woow-identity"
CLIENT_IP_HEADER = "x-woow-client-ip"
READ_TIMEOUT_HEADER = "x-woow-read-timeout"

#: |now - ts| allowed, seconds.
MAX_SKEW_SECONDS = 30
#: A signature is accepted once within this window (per replica).
REPLAY_TTL_SECONDS = 60


def canonical_query(pairs: Iterable[tuple[str, str]]) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def string_to_sign(
    method: str,
    canonical_path: str,
    canonical_query_string: str,
    body: bytes,
    ts: str,
    identity: str,
    client_ip: str,
) -> bytes:
    return "\n".join(
        [
            method,
            canonical_path,
            canonical_query_string,
            hashlib.sha256(body).hexdigest(),
            ts,
            identity,
            client_ip,
        ]
    ).encode("ascii")


def sign(secret: bytes, message: bytes) -> str:
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def signature_matches(secret: bytes, message: bytes, presented: str) -> bool:
    return hmac.compare_digest(sign(secret, message), presented)


def signed_headers(
    kid: str,
    secret: bytes,
    method: str,
    path: str,
    query_pairs: Iterable[tuple[str, str]],
    body: bytes,
    identity: str,
    client_ip: str,
    ts: int,
) -> dict[str, str]:
    """Headers a caller adds to a request (reference client helper)."""
    ts_s = str(int(ts))
    message = string_to_sign(method, path, canonical_query(query_pairs), body, ts_s, identity, client_ip)
    headers = {
        "X-Woow-Proxy-Kid": kid,
        "X-Woow-Proxy-Ts": ts_s,
        "X-Woow-Proxy-Sig": sign(secret, message),
        "X-Woow-Identity": identity,
    }
    if client_ip:
        headers["X-Woow-Client-IP"] = client_ip
    return headers
