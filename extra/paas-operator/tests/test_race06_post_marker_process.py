"""Independent process-crash proof for RACE-06's review-only adoption lane.

The fake provider is a real HTTP server in a separate process.  It durably
records the owned Access app, signals that point, then withholds the POST
response.  The caller is SIGKILLed in that post-effect/pre-receipt window.
Recovery uses the production CloudflareService readback method and must not
issue a second POST.  All resources are local to this test and are deleted.
"""
import asyncio
import json
import multiprocessing
import os
import signal
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from src.services.cloudflare import CloudflareService


pytestmark = pytest.mark.integration


APP_NAME = "b55-race06-owned"
HOSTNAME = "b55-race06-owned.example.invalid"


def _serve(port_pipe, committed, release, state_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _state(self):
            try:
                with open(state_path, encoding="utf-8") as stream:
                    return json.load(stream)
            except FileNotFoundError:
                return {"apps": [], "posts": 0}

        def _write(self, value):
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
            os.replace(tmp, state_path)

        def do_POST(self):
            state = self._state()
            state["posts"] += 1
            state["apps"].append({"id": "app-owned", "name": APP_NAME,
                                  "domain": HOSTNAME})
            self._write(state)
            committed.set()
            release.wait(20)
            try:
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True,
                    "result": state["apps"][-1]}).encode())
            except BrokenPipeError:
                pass

        def do_GET(self):
            state = self._state()
            body = {"success": True, "result": state["apps"],
                    "result_info": {"total_pages": 1}}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_DELETE(self):
            state = self._state()
            state["apps"] = []
            self._write(state)
            self.send_response(200)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_pipe.send(server.server_address[1])
    server.serve_forever()


def _uncertain_post(url):
    httpx.post(url, json={"name": APP_NAME, "domain": HOSTNAME}, timeout=30)


def test_post_marker_crash_is_resolved_by_review_without_second_post():
    ctx = multiprocessing.get_context("spawn")
    committed, release = ctx.Event(), ctx.Event()
    recv_port, send_port = ctx.Pipe(duplex=False)
    with tempfile.TemporaryDirectory(prefix="b55-race06-") as owned:
        state_path = os.path.join(owned, "provider.json")
        provider = ctx.Process(
            target=_serve,
            args=(send_port, committed, release, state_path),
            name="b55-race06-owned-provider")
        provider.start()
        port = recv_port.recv()
        base = f"http://127.0.0.1:{port}/client/v4"
        caller = ctx.Process(
            target=_uncertain_post,
            args=(f"{base}/accounts/test/access/apps",),
            name="b55-race06-uncertain-caller")
        caller.start()
        try:
            assert committed.wait(10), "provider effect did not commit"
            os.kill(caller.pid, signal.SIGKILL)
            caller.join(10)
            assert caller.exitcode == -signal.SIGKILL

            service = CloudflareService()
            service.BASE_URL = base
            service.account_id = "test"
            service.api_token = "owned-test-token"
            result = asyncio.run(service.review_access_app_exact(
                APP_NAME, HOSTNAME))
            assert result == {"status": "unique", "app_id": "app-owned",
                              "match_count": 1}
            with open(state_path, encoding="utf-8") as stream:
                assert json.load(stream)["posts"] == 1

            response = httpx.delete(
                f"{base}/accounts/test/access/apps/app-owned", timeout=5)
            assert response.status_code == 200
            with open(state_path, encoding="utf-8") as stream:
                assert json.load(stream)["apps"] == []
        finally:
            release.set()
            if caller.is_alive():
                caller.kill()
            caller.join(5)
            provider.terminate()
            provider.join(5)
