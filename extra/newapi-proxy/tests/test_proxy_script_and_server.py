"""scripts/signed_request.py stays in lockstep with the proxy; raw HTTP end to end.

The raw-socket tests run the real uvicorn server (h11, with the proxy's
header-deadline protocol) on 127.0.0.1 so that the request target reaches the
proxy exactly as sent: no client library gets a chance to normalise ``..`` or
percent-escapes first. The last test runs ``python -m newapi_proxy`` itself:
two listeners, SIGTERM.
"""

import argparse
import asyncio
import importlib.util
import io
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from newapi_proxy import signing
from newapi_proxy.server import HeaderDeadlineH11Protocol
from proxy_harness import CRON_KID, CRON_SECRET, NOW, Harness, base_env, ok

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "signed_request.py"


def load_script():
    spec = importlib.util.spec_from_file_location("signed_request", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def script_args(**kw):
    defaults = dict(
        method="GET",
        url="http://proxy:8080/api/status",
        identity="anon",
        client_ip="",
        key_env="K",
        key_stdin=False,
        auth_stdin=False,
        auth_env="",
        proof_env="",
        json=None,
        body_stdin=False,
        ts_offset=0,
        unsigned=False,
        read_timeout=0,
        extra_header=[],
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def test_script_signing_matches_library():
    script = load_script()
    nonce = "N" * 22
    for args in [
        ("GET", "/api/status", "", b"", "1800000000", nonce, "cron-a", "anon", ""),
        ("POST", "/api/token/", "", b'{"a":1}', "1800000001", nonce, "web-a", "user", "198.18.17.5"),
        ("GET", "/api/user/search", "keyword=wsab0000000001&p=1", b"", "1800000002", nonce, "cron-a", "ap", ""),
    ]:
        assert script.string_to_sign(*args) == signing.string_to_sign(*args)
        assert script.sign(b"k" * 40, script.string_to_sign(*args)) == signing.sign(
            b"k" * 40, signing.string_to_sign(*args)
        )


def test_script_requests_are_accepted_by_the_proxy():
    script = load_script()
    h = Harness()
    env = {"K": f"{CRON_KID}:{CRON_SECRET},other:{'x' * 40}"}
    target, body, headers = script.build(
        script_args(url="http://proxy:8080/api/user/search?p=1&keyword=wsab0000000001", identity="ap"), env, NOW
    )
    assert target.endswith("/api/user/search?keyword=wsab0000000001&p=1")
    assert signing.NONCE_RE.fullmatch(headers["X-Woow-Proxy-Nonce"])
    res = h.call("GET", b"/api/user/search", b"keyword=wsab0000000001&p=1", list(headers.items()))
    assert res.status == 200, res.body
    # a second build signs with a new nonce: accepted too
    _, _, again = script.build(
        script_args(url="http://proxy:8080/api/user/search?p=1&keyword=wsab0000000001", identity="ap"), env, NOW
    )
    assert again["X-Woow-Proxy-Nonce"] != headers["X-Woow-Proxy-Nonce"]
    assert h.call("GET", b"/api/user/search", b"keyword=wsab0000000001&p=1", list(again.items())).status == 200


def test_script_reads_the_key_and_body_from_stdin():
    script = load_script()
    h = Harness()
    stdin = io.BytesIO(
        f"{CRON_KID}:{CRON_SECRET}\n".encode()
        + b'{"username":"wsab0000000001","password":"'
        + b"p" * 40
        + b'","display_name":"PaaS 1","role":1}'
    )
    target, body, headers = script.build(
        script_args(method="POST", url="http://proxy:8080/api/user/", identity="ap", key_stdin=True, body_stdin=True),
        {},
        NOW,
        stdin=stdin,
    )
    assert body.startswith(b'{"username"')
    res = h.call("POST", b"/api/user/", b"", list(headers.items()), body)
    assert res.status == 200, res.body


def test_script_reads_key_then_user_token_from_stdin():
    script = load_script()
    h = Harness()
    stdin = io.BytesIO(f"{CRON_KID}:{CRON_SECRET}\nnap_{'u' * 43}\n".encode())
    _, _, headers = script.build(
        script_args(
            url="http://proxy:8080/api/token/7",
            identity="user",
            client_ip="198.18.17.9",
            key_stdin=True,
            auth_stdin=True,
        ),
        {},
        NOW,
        stdin=stdin,
    )
    assert headers["Authorization"] == "Bearer nap_" + "u" * 43
    assert h.call("GET", b"/api/token/7", b"", list(headers.items())).status == 200


def test_script_never_takes_secrets_from_argv_and_masks_output():
    script = load_script()
    with pytest.raises(SystemExit):
        script.build(script_args(), {}, NOW)  # key env missing
    with pytest.raises(SystemExit) as info:
        script.build(script_args(), {"K": "kid:short"}, NOW)
    assert "short" not in str(info.value)
    masked = script.mask('{"data":{"key":"AbcdEfgh1234","token":"nap_' + "a" * 43 + '","x":"sk-' + "b" * 40 + '"}}')
    assert "AbcdEfgh1234" not in masked and "nap_aaaa" not in masked and "sk-bbbb" not in masked
    with pytest.raises(SystemExit):
        script.split_query("p=%31")


# --- real server ----------------------------------------------------------------


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(HeaderDeadlineH11Protocol, "header_timeout", 0.5)
    h = Harness()
    h.upstream.on("GET", "/api/status", ok({"version": "test"}))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        h.app,
        http=HeaderDeadlineH11Protocol,
        lifespan="on",
        log_config=None,
        access_log=False,
        proxy_headers=False,
        timeout_keep_alive=5,
    )
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(srv.serve(sockets=[sock])), daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started:
        assert time.time() < deadline, "server did not start"
        time.sleep(0.02)
    yield h, port
    srv.should_exit = True
    thread.join(timeout=10)
    sock.close()


def raw_request(port: int, request_line: bytes, headers: list[tuple[str, str]]) -> tuple[int, bytes]:
    data = request_line + b"\r\nHost: proxy\r\nConnection: close\r\n"
    for k, v in headers:
        data += f"{k}: {v}\r\n".encode("latin-1")
    data += b"\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(data)
        chunks = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = b"".join(chunks)
    status = int(response.split(b" ", 2)[1])
    return status, response.split(b"\r\n\r\n", 1)[1]


def test_real_server_signed_status(server):
    h, port = server
    headers = h.sign_headers("GET", "/api/status") + [("X-Forwarded-For", "114.32.21.18")]
    status, body = raw_request(port, b"GET /api/status HTTP/1.1", headers)
    assert status == 200, body
    assert h.upstream.requests[-1].headers["x-forwarded-for"] == "198.18.0.2"


@pytest.mark.parametrize(
    "target, sign_path",
    [
        (b"/api/token/%2e%2e/key", "/api/token/%2e%2e/key"),
        (b"/api/token/../key", "/api/token/../key"),
        (b"/api/token/1%2Fkey", "/api/token/1%2Fkey"),
        (b"//api/status", "//api/status"),
        (b"/api/status;x=1", "/api/status;x=1"),
        (b"http://evil.example/api/status", "/api/status"),
    ],
)
def test_real_server_keeps_raw_target_and_rejects_variants(server, target, sign_path):
    h, port = server
    headers = h.sign_headers("GET", sign_path, identity="user", client_ip="198.18.17.5")
    headers.append(("Authorization", "Bearer nap_" + "u" * 43))
    status, body = raw_request(port, b"GET " + target + b" HTTP/1.1", headers)
    assert status == 400, body
    assert json.loads(body)["proxy_error"] == "bad_request"
    assert h.upstream.requests == []


def closed_within(conn: socket.socket, seconds: float) -> bool:
    conn.settimeout(seconds)
    try:
        return conn.recv(1024) == b""
    except socket.timeout:
        return False


@pytest.mark.parametrize("prefix", [b"", b"GET /api/sta", b"GET /api/status HTTP/1.1\r\nHost: proxy\r\nX-Woow"])
def test_slow_or_silent_headers_are_dropped(server, prefix):
    """uvicorn has no header deadline of its own: a connection that never
    completes its headers would hold a limit_concurrency slot forever."""
    _h, port = server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        if prefix:
            conn.sendall(prefix)
        started = time.monotonic()
        assert closed_within(conn, 3)
        assert time.monotonic() - started < 2


def test_keep_alive_connection_gets_a_fresh_header_deadline(server):
    h, port = server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        for _ in range(2):
            time.sleep(0.3)  # each request well inside its own deadline
            data = b"GET /api/status HTTP/1.1\r\nHost: proxy\r\n"
            for k, v in h.sign_headers("GET", "/api/status"):
                data += f"{k}: {v}\r\n".encode()
            conn.sendall(data + b"\r\n")
            reply = b""
            while b"\r\n\r\n" not in reply or not reply.rstrip().endswith(b"}"):
                reply += conn.recv(65536)
            assert reply.startswith(b"HTTP/1.1 200"), reply
        # then a half-sent third request is cut off
        conn.sendall(b"GET /api/st")
        assert closed_within(conn, 3)


# --- the real process: two listeners, SIGTERM --------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_get(port: int, path: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def test_process_serves_proxy_and_admin_ports_and_stops_on_sigterm():
    port, admin_port, dead = free_port(), free_port(), free_port()
    env = {
        **{k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT")},
        **base_env(
            PROXY_REPLICAS="1",
            PROXY_VALKEY_ADDR="",
            PROXY_VALKEY_PASSWORD="",
            PROXY_UPSTREAM_URL=f"http://127.0.0.1:{dead}",
            PROXY_PORT=str(port),
            PROXY_ADMIN_PORT=str(admin_port),
        ),
        "PYTHONPATH": str(ROOT),
    }
    with subprocess.Popen(
        [sys.executable, "-m", "newapi_proxy"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ) as proc:
        try:
            deadline = time.time() + 15
            while True:
                try:
                    if http_get(admin_port, "/healthz") == 200:
                        break
                except OSError:
                    pass
                assert proc.poll() is None, "proxy exited early"
                assert time.time() < deadline, "admin port did not come up"
                time.sleep(0.1)
            assert http_get(port, "/healthz") == 401  # the proxy port has no open paths
            assert http_get(admin_port, "/readyz") == 503  # upstream refused: not ready
            assert http_get(admin_port, "/metrics") == 200
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    out = out.decode()
    assert '"event":"startup"' in out
    for secret in (CRON_SECRET,):
        assert secret not in out
