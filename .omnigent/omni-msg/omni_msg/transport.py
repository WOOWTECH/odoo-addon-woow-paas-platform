"""JSON HTTP transport：不追 redirect、不重試不確定的 POST、不輸出 credential。"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_BYTES = 64 * 1024


class MessageError(Exception):
    """可安全回傳 CLI 的錯誤；不夾帶遠端 response body 或 credential。"""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def private_file(path: str | Path) -> str:
    """只讀目前使用者持有的 private regular file，不追 symlink。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or info.st_uid != os.getuid()):
                raise MessageError("private_file_required")
            content = stream.read(MAX_BYTES + 1)
            if len(content.encode("utf-8")) > MAX_BYTES:
                raise MessageError("config_too_large")
            return content
    except (OSError, UnicodeError) as exc:
        raise MessageError("private_file_unreadable") from exc


def decode_json(raw: str | bytes):
    """拒絕 duplicate key，避免 registry 或 ACK 的 first/last-wins 歧義。"""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def load_private_json(path: str | Path) -> dict:
    try:
        value = decode_json(private_file(path))
    except ValueError as exc:
        raise MessageError("invalid_config_json") from exc
    if not isinstance(value, dict):
        raise MessageError("invalid_config_json")
    return value


def validate_url(value: str, *, loopback: bool = False) -> str:
    """上游限定 HTTPS（本機 HTTP 例外）；CLI 只連 loopback bridge。"""
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise MessageError("invalid_url")
    try:
        url = urllib.parse.urlsplit(value)
        local = url.hostname in {"127.0.0.1", "localhost", "::1"}
        port = url.port
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or url.scheme not in {"http", "https"}
                or (url.scheme == "http" and not local)
                or (loopback and not local)
                or url.path not in {"", "/"} or port == 0):
            raise MessageError("invalid_url")
    except ValueError as exc:
        raise MessageError("invalid_url") from exc
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url: str, token: str, *, payload: dict | None = None,
                 timeout: float = 15) -> dict:
    """HTTP ACK 不等於完成；transport error 不能安全地重送。"""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "omni-msg"},
    )
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise MessageError("response_too_large", 502)
            result = decode_json(raw)
    except urllib.error.HTTPError as exc:
        # 不把 server 的 body（可能含 prompt／token）傳入 log。
        exc.close()
        raise MessageError(f"http_{exc.code}", exc.code) from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise MessageError("transport_unknown", 502) from exc
    if not isinstance(result, dict):
        raise MessageError("invalid_response", 502)
    return result


class OmnigentTransport:
    def __init__(self, server_url: str, token_file: str | Path, *, timeout: float = 15):
        self.url = validate_url(server_url)
        self.token = private_file(token_file).strip()
        if not self.token or any(c.isspace() for c in self.token):
            raise MessageError("invalid_upstream_token")
        self.timeout = timeout

    def deliver(self, session_id: str, text: str) -> tuple[str, str | None, str | None]:
        """回傳 delivery／item_id／error；unknown 一律人工對帳，不自動重送。"""
        endpoint = f"{self.url}/v1/sessions/{urllib.parse.quote(session_id, safe='')}/events"
        try:
            result = request_json(endpoint, self.token, timeout=self.timeout, payload={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            })
        except MessageError as exc:
            # 明確的驗權／驗值失敗才算 rejected；5xx／timeout 可能已經投遞。
            state = "rejected" if exc.status in {400, 401, 403, 404, 413, 422} else "unknown"
            return state, None, exc.code
        if result.get("denied") is True:
            return "rejected", None, "policy_denied"
        if result.get("queued") is True:
            item = result.get("item_id")
            return "accepted", item if isinstance(item, str) else None, None
        return "unknown", None, "unexpected_ack"
