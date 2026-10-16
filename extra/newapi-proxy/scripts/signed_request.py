#!/usr/bin/env python3
"""Send one HMAC-signed request to newapi-proxy (stdlib only).

For the LLM-01a stg checks: copy this file into a sandbox Odoo pod and run it
there (python3 is all it needs). Secrets never go on the command line:

* the signing key comes from an env var holding ``kid:secret[,kid:secret]``
  (the first entry signs) — default ``WOOW_NEWAPI_PROXY_AUTH_CRON``;
* a user ``Authorization`` value comes from the env var named by
  ``--auth-env`` (the value is the bare token; ``Bearer `` is added);
* a JSON body comes from ``--json`` (no secrets) or stdin (``--body-stdin``).

Examples::

    signed_request.py GET http://newapi-proxy.new-api-stg.svc.cluster.local:8080/api/status
    signed_request.py --identity ap GET '<proxy>/api/user/search?keyword=wsab0000000001'
    signed_request.py --ts-offset -31 GET <proxy>/api/status          # 401 (expired)
    signed_request.py --repeat 2 GET <proxy>/api/status                # second is 401 (replay)
    signed_request.py --extra-header 'X-Forwarded-For: 114.32.21.18' ...

Output: status, X-Woow-Proxy-Error, X-Oneapi-Request-Id and the response body
with every key/token/password-looking value masked (``--show-body`` turns the
masking off; do not use it where the terminal is logged).

The signing code mirrors newapi_proxy/signing.py; tests/test_proxy_script.py
keeps the two in lockstep.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_QUERY_OK = re.compile(r"[A-Za-z0-9_.\-=&:/]*")
_MASK_FIELDS = re.compile(
    r'("(?:key|token|access_token|proof_token|password|refresh_token)"\s*:\s*)"[^"]*"', re.IGNORECASE
)
_MASK_SHAPES = (re.compile(r"nap_[A-Za-z0-9]{8,}"), re.compile(r"sk-[A-Za-z0-9_-]{8,}"))


def canonical_query(pairs) -> str:
    return "&".join(f"{k}={v}" for k, v in sorted(pairs))


def string_to_sign(method, path, query, body, ts, identity, client_ip) -> bytes:
    return "\n".join([method, path, query, hashlib.sha256(body).hexdigest(), ts, identity, client_ip]).encode("ascii")


def sign(secret: bytes, message: bytes) -> str:
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def first_key(raw: str) -> tuple[str, bytes]:
    entry = raw.strip().split(",")[0].strip()
    kid, sep, secret = entry.partition(":")
    if not sep or not kid or len(secret) < 32:
        raise SystemExit("signing key env var is not kid:secret (value not shown)")
    return kid, secret.encode("ascii")


def split_query(raw_query: str) -> list[tuple[str, str]]:
    if raw_query == "":
        return []
    if not _QUERY_OK.fullmatch(raw_query):
        raise SystemExit("query has characters the proxy rejects (no %-encoding, +, ; allowed)")
    pairs = []
    for part in raw_query.split("&"):
        key, sep, value = part.partition("=")
        if not sep:
            raise SystemExit("query part without '='")
        pairs.append((key, value))
    return pairs


def mask(text: str) -> str:
    text = _MASK_FIELDS.sub(r'\1"<masked>"', text)
    for pattern in _MASK_SHAPES:
        text = pattern.sub("<masked>", text)
    return text


def build(args, environ, now: int):
    url = urllib.parse.urlsplit(args.url)
    path = url.path or "/"
    pairs = split_query(url.query)
    query = canonical_query(pairs)
    if args.body_stdin:
        body = sys.stdin.buffer.read()
    elif args.json is not None:
        body = args.json.encode("utf-8")
    else:
        body = b""
    client_ip = args.client_ip or ""
    ts = str(now + args.ts_offset)
    headers = {"Content-Type": "application/json"} if body else {}
    if not args.unsigned:
        key_raw = environ.get(args.key_env)
        if not key_raw:
            raise SystemExit(f"{args.key_env} is not set")
        kid, secret = first_key(key_raw)
        message = string_to_sign(args.method, path, query, body, ts, args.identity, client_ip)
        headers.update(
            {
                "X-Woow-Proxy-Kid": kid,
                "X-Woow-Proxy-Ts": ts,
                "X-Woow-Proxy-Sig": sign(secret, message),
                "X-Woow-Identity": args.identity,
            }
        )
        if client_ip:
            headers["X-Woow-Client-IP"] = client_ip
    if args.auth_env:
        token = environ.get(args.auth_env, "").strip()
        if not token:
            raise SystemExit(f"{args.auth_env} is not set")
        headers["Authorization"] = f"Bearer {token}"
    if args.proof_env:
        proof = environ.get(args.proof_env, "").strip()
        if not proof:
            raise SystemExit(f"{args.proof_env} is not set")
        headers["X-Security-Proof"] = proof
    if args.read_timeout:
        headers["X-Woow-Read-Timeout"] = str(args.read_timeout)
    for extra in args.extra_header:
        name, sep, value = extra.partition(":")
        if not sep:
            raise SystemExit("--extra-header wants 'Name: value'")
        headers[name.strip()] = value.strip()
    target = urllib.parse.urlunsplit((url.scheme, url.netloc, path, query, ""))
    return target, body, headers


def send(method: str, target: str, body: bytes, headers: dict, timeout: float):
    request = urllib.request.Request(target, data=body or None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - in-cluster URL
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("method")
    parser.add_argument("url")
    parser.add_argument("--identity", default="anon", choices=["anon", "ap", "op", "user", "bg"])
    parser.add_argument("--client-ip", default="")
    parser.add_argument("--key-env", default="WOOW_NEWAPI_PROXY_AUTH_CRON")
    parser.add_argument("--auth-env", default="")
    parser.add_argument("--proof-env", default="")
    parser.add_argument("--json", default=None)
    parser.add_argument("--body-stdin", action="store_true")
    parser.add_argument("--ts-offset", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--unsigned", action="store_true")
    parser.add_argument("--read-timeout", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=35.0)
    parser.add_argument("--extra-header", action="append", default=[])
    parser.add_argument("--show-body", action="store_true")
    args = parser.parse_args(argv)
    args.method = args.method.upper()

    target, body, headers = build(args, os.environ, int(time.time()))
    rc = 0
    for attempt in range(1, args.repeat + 1):
        status, resp_headers, resp_body = send(args.method, target, body, headers, args.timeout)
        lower = {k.lower(): v for k, v in resp_headers.items()}
        text = resp_body.decode("utf-8", "replace")
        print(
            json.dumps(
                {
                    "attempt": attempt,
                    "status": status,
                    "proxy_error": lower.get("x-woow-proxy-error"),
                    "request_id": lower.get("x-oneapi-request-id"),
                    "body": text if args.show_body else mask(text),
                },
                ensure_ascii=False,
            )
        )
        rc = 0 if status < 400 else 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
