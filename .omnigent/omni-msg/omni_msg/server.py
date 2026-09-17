"""Loopback HTTP adapter；credential 驗證先於任何 ledger 存取。"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .transport import MAX_BYTES, MessageError, decode_json


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, bridge):
        if address[0] != "127.0.0.1":
            raise MessageError("bridge_must_bind_loopback")
        self.bridge = bridge
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, format, *args):
        # 不記錄 URL、prompt、Authorization 或 request ID。
        pass

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        try:
            if self.headers.get("Origin") or self.headers.get("Transfer-Encoding"):
                raise MessageError("unsupported_request", 403)
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                raise MessageError("unauthorized", 401)
            bridge = self.server.bridge.snapshot()
            caller = bridge.authenticate(authorization[7:])
            if self.command == "GET" and self.path == "/v1/contacts":
                result = bridge.contacts(caller)
            elif self.command == "GET" and self.path.startswith("/v1/messages/"):
                result = bridge.check(caller, self.path.removeprefix("/v1/messages/"))
            elif self.command == "POST" and self.path == "/v1/messages":
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise MessageError("invalid_content_length") from exc
                if not 0 < size <= MAX_BYTES:
                    raise MessageError("invalid_body_size", 413)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise MessageError("incomplete_body")
                try:
                    payload = decode_json(raw)
                except (ValueError, UnicodeError) as exc:
                    raise MessageError("invalid_json") from exc
                result = bridge.send(caller, payload)
            else:
                raise MessageError("not_found", 404)
            self.respond(200, result)
        except MessageError as exc:
            self.respond(exc.status, {"error": exc.code})
        except (TimeoutError, ConnectionError):
            return
        except Exception:  # noqa: BLE001 — HTTP 邊界只回匿名錯誤，避免 exception 洩漏 credential。
            self.respond(500, {"error": "bridge_internal_error"})

    def respond(self, status: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (OSError, ConnectionError):
            pass
