"""Raw request-target checks (DESIGN §1.4.2-5, 〔REV〕17).

The proxy works on the *undecoded* path (ASGI ``raw_path``) and query string.
Anything that would need decoding or normalising is rejected outright rather
than interpreted: the forwarded URL is rebuilt from a route template, so a
request can only ever reach a path the proxy itself spelled out.
"""

from __future__ import annotations

import re

MAX_PATH_LENGTH = 512
MAX_QUERY_LENGTH = 2048

_PATH_RE = re.compile(r"/[A-Za-z0-9/_.\-]*", re.ASCII)
_QUERY_RE = re.compile(r"[A-Za-z0-9_.\-=&:/]*", re.ASCII)
_QUERY_KEY_RE = re.compile(r"[a-z_]{1,32}", re.ASCII)


class CanonicalError(ValueError):
    """The request target is not in canonical form. ``reason`` is a stable code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _ascii(raw: bytes, what: str) -> str:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise CanonicalError(f"{what}_non_ascii") from None
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in text):
        raise CanonicalError(f"{what}_control")
    return text


def check_raw_path(raw: bytes | None) -> str:
    """Return the path if it is already canonical, else raise."""
    if raw is None:
        raise CanonicalError("path_missing")
    if len(raw) > MAX_PATH_LENGTH:
        raise CanonicalError("path_too_long")
    path = _ascii(raw, "path")
    if "%" in path:
        raise CanonicalError("path_percent")
    if "\\" in path:
        raise CanonicalError("path_backslash")
    if ";" in path:
        raise CanonicalError("path_semicolon")
    if ".." in path:
        raise CanonicalError("path_dotdot")
    if "//" in path:
        raise CanonicalError("path_double_slash")
    if not _PATH_RE.fullmatch(path):
        raise CanonicalError("path_chars")
    if any(segment == "." for segment in path.split("/")):
        raise CanonicalError("path_dot_segment")
    return path


def parse_query(raw: bytes) -> list[tuple[str, str]]:
    """Parse ``k=v&k=v``; keys unique, values never percent-encoded."""
    if raw == b"":
        return []
    if len(raw) > MAX_QUERY_LENGTH:
        raise CanonicalError("query_too_long")
    query = _ascii(raw, "query")
    if "%" in query:
        raise CanonicalError("query_percent")
    if "+" in query:
        raise CanonicalError("query_plus")
    if ";" in query:
        raise CanonicalError("query_semicolon")
    if not _QUERY_RE.fullmatch(query):
        raise CanonicalError("query_chars")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in query.split("&"):
        key, sep, value = part.partition("=")
        if not sep or "=" in value:
            raise CanonicalError("query_pair")
        if not _QUERY_KEY_RE.fullmatch(key):
            raise CanonicalError("query_key_format")
        if value == "":
            raise CanonicalError("query_empty_value")
        if key in seen:
            raise CanonicalError("query_duplicate_key")
        seen.add(key)
        pairs.append((key, value))
    return pairs
