#!/usr/bin/env python3
"""prod clone 備份組的叢集內接收端（#1206；pitfall kubectl-exec-stdout-tail-truncation 修法 1：pod-to-pod HTTP PUT）。

由 seed-from-prod.sh receive 以 ConfigMap 掛進沙盒的 receiver pod 執行（Odoo image 的 python3，只用標準庫）。
  PUT /<set>/<file>   Header X-Upload-Token 必須等於 env UPLOAD_TOKEN；串流寫入 /incoming/<set>/<file>.part，
                      寫完才 rename；回應 body＝"<bytes> <sha256>"。
  GET /healthz        200
只收單層檔名（db.dump、filestore.tar、manifest.json、attachments.txt、SHA256SUMS…），不收子目錄、不覆寫既有檔。
"""
import hashlib
import hmac
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = "/incoming"
TOKEN = os.environ["UPLOAD_TOKEN"]
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CHUNK = 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body):
        data = (body + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._reply(200 if self.path == "/healthz" else 404, "ok" if self.path == "/healthz" else "not found")

    def do_PUT(self):  # noqa: N802
        if not hmac.compare_digest(self.headers.get("X-Upload-Token", ""), TOKEN):
            return self._reply(403, "bad token")
        parts = self.path.strip("/").split("/")
        if len(parts) != 2 or not all(NAME_RE.match(p) for p in parts) or ".." in self.path:
            return self._reply(400, "path must be /<set>/<file>")
        directory = os.path.join(ROOT, parts[0])
        target = os.path.join(directory, parts[1])
        if os.path.exists(target):
            return self._reply(409, "exists")
        os.makedirs(directory, exist_ok=True)
        length = self.headers.get("Content-Length")
        chunked = self.headers.get("Transfer-Encoding", "").lower() == "chunked"
        digest, total = hashlib.sha256(), 0
        tmp = target + ".part"
        with open(tmp, "wb") as fh:
            if chunked:
                while True:
                    size = int(self.rfile.readline().strip().split(b";")[0], 16)
                    if size == 0:
                        self.rfile.readline()
                        break
                    remaining = size
                    while remaining:
                        buf = self.rfile.read(min(CHUNK, remaining))
                        if not buf:
                            raise ConnectionError("short chunk")
                        fh.write(buf)
                        digest.update(buf)
                        total += len(buf)
                        remaining -= len(buf)
                    self.rfile.readline()
            elif length is not None:
                remaining = int(length)
                while remaining:
                    buf = self.rfile.read(min(CHUNK, remaining))
                    if not buf:
                        os.unlink(tmp)
                        return self._reply(400, f"short body: missing {remaining} bytes")
                    fh.write(buf)
                    digest.update(buf)
                    total += len(buf)
                    remaining -= len(buf)
            else:
                os.unlink(tmp)
                return self._reply(411, "Content-Length or chunked required")
            fh.flush()
            os.fsync(fh.fileno())
        os.rename(tmp, target)
        self._reply(201, f"{total} {digest.hexdigest()}")

    def log_message(self, fmt, *args):  # 只記方法、路徑、狀態，不記 header（token）
        print("%s %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
