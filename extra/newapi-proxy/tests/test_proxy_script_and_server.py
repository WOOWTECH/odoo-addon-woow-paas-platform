"""scripts/signed_request.py stays in lockstep with the proxy; raw HTTP end to end.

The raw-socket tests run the real uvicorn server (h11) on 127.0.0.1 so that the
request target reaches the proxy exactly as sent: no client library gets a
chance to normalise ``..`` or percent-escapes first.
"""

import argparse
import asyncio
import importlib.util
import json
import pathlib
import socket
import threading
import time

import pytest
import uvicorn

from newapi_proxy import signing
from proxy_harness import CRON_KID, CRON_SECRET, NOW, Harness, ok

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "signed_request.py"


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
    for args in [
        ("GET", "/api/status", "", b"", "1800000000", "anon", ""),
        ("POST", "/api/token/", "", b'{"a":1}', "1800000001", "user", "198.18.17.5"),
        ("GET", "/api/user/search", "keyword=wsab0000000001&p=1", b"", "1800000002", "ap", ""),
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
    res = h.call("GET", b"/api/user/search", b"keyword=wsab0000000001&p=1", list(headers.items()))
    assert res.status == 200, res.body


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
def server():
    h = Harness()
    h.upstream.on("GET", "/api/status", ok({"version": "test"}))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(h.app, http="h11", lifespan="on", log_config=None, access_log=False, proxy_headers=False)
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
