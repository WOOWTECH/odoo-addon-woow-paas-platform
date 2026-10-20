"""Structured, redacted logging (DESIGN §1.4.2-8, §5.5).

The proxy never logs headers or bodies. Access lines carry method, route
template, status, duration, ``X-Oneapi-Request-Id``, synthetic IP, identity and
kid; audit lines (reveal, add_quota, bg) add the token / target id and value.
As a second line of defence every emitted line — including uvicorn's — passes
through ``redact()``, which masks PATs, ``sk-`` keys, bearer values, JWTs,
64-hex digests and the literal configured secrets.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Iterable

_PATTERNS = (
    (re.compile(r"nap_[A-Za-z0-9]{8,}"), "nap_<redacted>"),
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "sk-<redacted>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer <redacted>"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "<jwt>"),
    (re.compile(r"\b[0-9a-fA-F]{64}\b"), "<hex64>"),
)

ACCESS_LOGGER = "newapi_proxy.access"
AUDIT_LOGGER = "newapi_proxy.audit"
APP_LOGGER = "newapi_proxy"


class Redactor:
    def __init__(self, literals: Iterable[str] = ()):
        # Longest first so a secret that contains another is masked whole.
        self.literals = sorted({s for s in literals if s}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for literal in self.literals:
            if literal in text:
                text = text.replace(literal, "<secret>")
        for pattern, replacement in _PATTERNS:
            text = pattern.sub(replacement, text)
        return text


class JsonFormatter(logging.Formatter):
    """One JSON object per line; dict messages are merged, text is redacted."""

    def __init__(self, redactor: Redactor):
        super().__init__()
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
        }
        if isinstance(record.msg, dict):
            payload.update(record.msg)
        else:
            payload["msg"] = record.getMessage()
        if record.exc_info and record.exc_info[0] is not None:
            # Exception *type* only: httpx/h11 messages can carry header values.
            payload["exc_type"] = record.exc_info[0].__name__
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), default=str)
        return self.redactor(line)


def configure(level: str, secrets: Iterable[str] = ()) -> Redactor:
    """Route the proxy's and uvicorn's loggers through the redacting formatter."""
    redactor = Redactor(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(redactor))
    for name in (APP_LOGGER, "uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(level.upper())
    # httpx/httpcore debug lines include request lines; keep them quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").disabled = True
    return redactor


def access(**fields) -> None:
    logging.getLogger(ACCESS_LOGGER).info({"event": "request", **fields})


def audit(action: str, **fields) -> None:
    logging.getLogger(AUDIT_LOGGER).info({"event": "audit", "action": action, **fields})


def event(name: str, level: int = logging.INFO, **fields) -> None:
    logging.getLogger(APP_LOGGER).log(level, {"event": name, **fields})
