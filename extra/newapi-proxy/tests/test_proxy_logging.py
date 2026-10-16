"""Log redaction and audit lines (DESIGN §1.4.2-8, §5.5; LLM-01a acceptance 7)."""

import json
import logging
import re

import httpx
import pytest

from newapi_proxy import logs
from proxy_harness import (
    AP_TOKEN,
    CRON_SECRET,
    NOW,
    OP_TOKEN,
    PASSWORD,
    PROOF,
    REVEALED_KEY,
    USER_JWT,
    USER_PAT,
    USERNAME,
    VALKEY_PASSWORD,
    WEB_SECRET,
    Harness,
    ok,
)

GREP = [re.compile(r"nap_[A-Za-z0-9]{40,}"), re.compile(r"sk-[A-Za-z0-9]{20,}")]
NEW_PAT = "nap_" + "FreshlyMintedUp0000aaaa1111bbbb2222cccc333"


@pytest.fixture
def stdout_logs(capsys):
    names = (logs.APP_LOGGER, "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore")
    saved = {
        n: (logging.getLogger(n).handlers[:], logging.getLogger(n).propagate, logging.getLogger(n).level) for n in names
    }
    yield capsys
    for n, (handlers, propagate, level) in saved.items():
        logger = logging.getLogger(n)
        logger.handlers, logger.propagate, logger.level = handlers, propagate, level
        logger.disabled = False


def run_everything(h):
    """Exercise every route that carries a secret in a header, body or response."""
    h.upstream.on("POST", "/api/user/login", ok({"access_token": USER_JWT, "session": {"sid": "s-1"}}))
    h.upstream.on("POST", "/api/verify", ok({"proof_token": PROOF}))
    h.upstream.on("POST", "/api/user/access_tokens", ok({"token": NEW_PAT, "item": {"id": 3}}))
    h.upstream.on("GET", "/api/token/42", ok({"id": 42, "name": "wp_7_ab12", "created_time": NOW - 3}))
    h.upstream.on("POST", "/api/token/42/key", ok({"key": REVEALED_KEY}))
    h.upstream.on("GET", "/api/token/43", ok({"id": 43, "name": "wp_8_ab12", "created_time": NOW - 180}))
    h.upstream.on("GET", "/api/user/123", ok({"id": 123, "username": "wsab0000000123", "role": 1, "group": "default"}))
    results = [
        h.signed("GET", "/api/status"),
        h.user("POST", "/api/user/login", auth=None, body={"username": USERNAME, "password": PASSWORD}),
        h.user(
            "POST",
            "/api/verify",
            auth=USER_JWT,
            body={
                "method": "password",
                "scope": "access_token.generate",
                "context": {"scopes": ["api_key:read"], "expires_at": NOW + 86400},
                "password": PASSWORD,
            },
        ),
        h.user(
            "POST",
            "/api/user/access_tokens",
            auth=USER_JWT,
            headers=[("X-Security-Proof", PROOF)],
            body={"name": "woow-paas-up-42-202610161200", "scopes": ["api_key:read"], "expires_at": NOW + 86400},
        ),
        h.user("POST", "/api/token/42/key", key="web"),
        h.user("POST", "/api/token/43/key", key="web"),  # denied: 3 minutes old
        h.signed(
            "POST",
            "/api/user/",
            identity="ap",
            body={"username": USERNAME, "password": PASSWORD, "display_name": "PaaS 42", "role": 1},
        ),
        h.signed(
            "POST",
            "/api/user/manage",
            identity="ap",
            body={"id": 123, "action": "add_quota", "mode": "add", "value": 15625},
        ),
        h.signed(
            "POST",
            "/api/user/manage",
            identity="ap",
            body={"id": 123, "action": "add_quota", "mode": "override", "value": 1},
        ),
        # bad signature with a secret-looking kid, replay, upstream error with secret-bearing text
        h.call(
            "GET",
            b"/api/status",
            b"",
            [
                ("X-Woow-Proxy-Kid", "x"),
                ("X-Woow-Proxy-Ts", "1"),
                ("X-Woow-Proxy-Sig", "f" * 64),
                ("X-Woow-Identity", "anon"),
            ],
        ),
    ]

    def boom(request):
        raise httpx.ReadError(f"reset while sending Authorization: Bearer {USER_PAT}")

    h.upstream.on("GET", "/api/token/9", boom)
    results.append(h.user("GET", "/api/token/9"))
    # every signature value that actually went over the wire
    sigs = [v for headers in h.sent for k, v in headers if k == "X-Woow-Proxy-Sig"]
    assert len(sigs) >= 10
    return results, sigs


def test_logs_never_contain_secrets_and_audit_every_reveal(stdout_logs):
    h = Harness()
    logs.configure("debug", h.settings.secrets)
    results, sigs = run_everything(h)
    out = stdout_logs.readouterr().out
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert lines, "expected log output"

    for pattern in GREP:
        assert not pattern.search(out), pattern.pattern
    for secret in (
        WEB_SECRET,
        CRON_SECRET,
        AP_TOKEN,
        OP_TOKEN,
        USER_PAT,
        NEW_PAT,
        USER_JWT,
        PROOF,
        PASSWORD,
        REVEALED_KEY,
        VALKEY_PASSWORD,
        *sigs,
    ):
        assert secret not in out
    assert not re.search(r"\b[0-9a-f]{64}\b", out)  # no signature or digest of any kind

    access = [line for line in lines if line.get("event") == "request"]
    assert {line["route"] for line in access} >= {"anon.status", "user.login", "user.token.reveal", "ap.user.manage"}
    for line in access:
        assert set(line) >= {"method", "route", "status", "ms", "request_id", "ip", "identity", "kid"}
        assert "/api/token/42" not in json.dumps(line)  # templates, not concrete paths

    reveals = [line for line in lines if line.get("event") == "audit" and line["action"] == "reveal"]
    # one line before the call ("sending") and one with the outcome
    assert [(r["token_id"], r["result"]) for r in reveals] == [(42, "sending"), (42, "forwarded"), (43, "denied")]
    for r in reveals:
        assert r["kid"] == "web-2026a" and r["ip"].startswith("198.18.")
    quota = [line for line in lines if line.get("event") == "audit" and line["action"] == "add_quota"]
    assert [(q.get("target_user_id"), q.get("value"), q["result"], q.get("reason")) for q in quota] == [
        (123, 15625, "sending", None),
        (123, 15625, "forwarded", None),
        (123, 1, "denied", "manage_mode"),  # the override attempt: against whom, for how much
    ]


def test_redactor_masks_known_shapes():
    redact = logs.Redactor(["literal-secret-value-0123456789abcdef"])
    text = (
        f"a {AP_TOKEN} b sk-{'a' * 48} c Bearer abc.def-ghi d {USER_JWT} e {'0' * 64} "
        "f literal-secret-value-0123456789abcdef"
    )
    masked = redact(text)
    for needle in (AP_TOKEN, "sk-" + "a" * 48, "abc.def-ghi", USER_JWT, "0" * 64, "literal-secret-value"):
        assert needle not in masked


def test_formatter_logs_exception_type_only(stdout_logs):
    logs.configure("info", [])
    try:
        raise ValueError(f"header value was {USER_PAT}")
    except ValueError:
        logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
    out = stdout_logs.readouterr().out
    assert "ValueError" in out
    assert USER_PAT not in out
    assert "header value was" not in out
