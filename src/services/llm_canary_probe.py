"""Bounded, secret-safe public Qwen probe for the LLM-14 canary.

The public entry point accepts only the two configured HTTPS API bases. A small
isolated child owns requests' DNS/connect/TLS/read work so the parent can enforce
one wall-clock deadline even when a server keeps a response alive by slow-dripping
bytes. The API key is sent only over the child's stdin and remains in process
memory; it is never included in argv, environment, files, logs, or result data.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ALLOWED_BASE_URLS = frozenset({
    "https://newapi.woowtech.io/v1",
    "https://newapi-stg.woowtech.io/v1",
})
MODEL = "qwen3-4b-aicoder"
PROMPT = "Reply with the single word: pong."
MAX_TOKENS = 32
MAX_RESPONSE_BYTES = 16 * 1024
MAX_TOTAL_SECONDS = 10.0
MAX_CHILD_SECONDS = 8.0
PARENT_CLEANUP_SECONDS = 1.0
REAP_SECONDS = 1.0
_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9]{16,128}\Z", re.ASCII)
_RESULT_CODES = frozenset({
    "ok", "invalid_input", "deadline", "timeout", "tls_error", "redirect",
    "body_too_large", "invalid_response", "http_error", "transport_error",
    "child_error", "cleanup_error",
})
_CHILD_RESULT_CODES = _RESULT_CODES - {"deadline", "child_error", "cleanup_error"}
_CHILD_MAX_OUTPUT = 512
_CHILD_MAX_INPUT = 4096


@dataclass(frozen=True)
class ProbeResult:
    code: str
    latency_ms: int
    status: int | None


def _valid_deadline(deadline: object) -> bool:
    if type(deadline) not in (int, float):
        return False
    try:
        return math.isfinite(deadline) and deadline > time.monotonic()
    except OverflowError:
        return False


def _valid_api_key(api_key: object) -> bool:
    return isinstance(api_key, str) and _API_KEY_RE.fullmatch(api_key) is not None


def _valid_base_url(base_url: object) -> bool:
    if not isinstance(base_url, str) or base_url not in ALLOWED_BASE_URLS:
        return False
    try:
        parsed = urlsplit(base_url)
        return (parsed.scheme == "https" and parsed.hostname is not None
                and parsed.username is None and parsed.password is None
                and parsed.path == "/v1" and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def probe(base_url: str, api_key: str, *, deadline: float) -> ProbeResult:
    """Call the fixed Qwen chat endpoint and return only a fixed code/latency/status.

    ``deadline`` is an absolute ``time.monotonic()`` value. The helper will use
    no more than eight seconds for its child, reserving time for kill/reap and
    its caller. Invalid input is rejected before creating a subprocess.
    ``cleanup_error`` means child exit could not be confirmed within that budget;
    callers must treat it as failure, never as a successfully cleaned timeout.
    """
    started = time.monotonic()
    if not _valid_base_url(base_url) or not _valid_api_key(api_key):
        return ProbeResult("invalid_input", 0, None)
    if not _valid_deadline(deadline):
        return ProbeResult("deadline", 0, None)

    payload = {
        "base_url": base_url,
        "api_key": api_key,
        "model": MODEL,
        "prompt": PROMPT,
        "max_tokens": MAX_TOKENS,
        "verify": True,
    }
    return _spawn_child(payload, deadline=deadline, started=started)


def _spawn_child(payload: dict, *, deadline: float, started: float | None = None) -> ProbeResult:
    """Private process seam; tests use it with a synthetic loopback endpoint."""
    started = time.monotonic() if started is None else started
    effective_deadline = min(deadline, started + MAX_TOTAL_SECONDS)
    remaining = min(
        MAX_CHILD_SECONDS,
        effective_deadline - time.monotonic() - PARENT_CLEANUP_SECONDS,
    )
    if remaining <= 0:
        return ProbeResult("deadline", 0, None)
    try:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        return ProbeResult("invalid_input", 0, None)
    if len(encoded) > _CHILD_MAX_INPUT:
        return ProbeResult("invalid_input", 0, None)

    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "--child"]
    proc = None
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd="/",
            close_fds=True,
            start_new_session=True,
        )
        remaining = min(
            MAX_CHILD_SECONDS,
            effective_deadline - time.monotonic() - PARENT_CLEANUP_SECONDS,
        )
        if remaining <= 0:
            raise subprocess.TimeoutExpired(command, 0)
        stdout, _ = proc.communicate(input=encoded, timeout=remaining)
    except subprocess.TimeoutExpired:
        if proc is not None and not _kill_and_reap(proc):
            return ProbeResult("cleanup_error", _elapsed_ms(started), None)
        return ProbeResult("timeout", _elapsed_ms(started), None)
    except (OSError, ValueError):
        if proc is not None and not _kill_and_reap(proc):
            return ProbeResult("cleanup_error", _elapsed_ms(started), None)
        return ProbeResult("child_error", _elapsed_ms(started), None)

    status = None
    code = "child_error"
    if proc.returncode == 0 and len(stdout) <= _CHILD_MAX_OUTPUT:
        try:
            result = json.loads(stdout.decode("ascii"))
            if (isinstance(result, dict) and set(result) == {"code", "status"}
                    and isinstance(result.get("code"), str)
                    and result.get("code") in _CHILD_RESULT_CODES
                    and (result.get("status") is None or
                         (type(result.get("status")) is int
                          and 100 <= result["status"] <= 599))):
                code = result["code"]
                status = result["status"]
        except (ValueError, UnicodeDecodeError):
            pass
    return ProbeResult(code, _elapsed_ms(started), status)


def _kill_and_reap(proc: subprocess.Popen) -> bool:
    """Best-effort cleanup with finite waits; never expose process errors."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.communicate(timeout=REAP_SECONDS)
        return True
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        return False
    except (OSError, ValueError):
        return False
    finally:
        # Even if the OS refused kill/reap, release this parent's pipe handles.
        # Linux's child watchdog still limits the surviving child independently.
        for name in ("stdin", "stdout", "stderr"):
            pipe = getattr(proc, name, None)
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _child_request(payload: dict, timeout: float) -> dict:
    """Perform one bounded HTTP request. Private seam used by loopback tests."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    if not isinstance(payload, dict):
        return {"code": "invalid_input", "status": None}
    base_url = payload.get("base_url")
    api_key = payload.get("api_key")
    model = payload.get("model")
    prompt = payload.get("prompt")
    max_tokens = payload.get("max_tokens")
    verify = payload.get("verify", True)
    if (not isinstance(base_url, str) or not base_url.startswith("https://")
            or not _valid_api_key(api_key) or model != MODEL or prompt != PROMPT
            or type(max_tokens) is not int or max_tokens != MAX_TOKENS
            or not (verify is True or isinstance(verify, str))
            or type(timeout) not in (int, float) or not math.isfinite(timeout)
            or timeout <= 0):
        return {"code": "invalid_input", "status": None}

    session = requests.Session()
    session.trust_env = False
    retry = Retry(total=0, connect=0, read=0, redirect=0, status=0, other=0,
                  raise_on_redirect=False, raise_on_status=False)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    response = None
    try:
        response = session.post(
            base_url + "/chat/completions",
            json={"model": MODEL, "messages": [{"role": "user", "content": PROMPT}],
                  "max_tokens": MAX_TOKENS, "stream": False},
            headers={"Authorization": "Bearer " + api_key,
                     "Accept": "application/json", "Connection": "close"},
            timeout=(min(2.0, timeout), min(2.0, timeout)),
            allow_redirects=False,
            stream=True,
            verify=verify,
        )
        status = response.status_code
        if 300 <= status < 400:
            return {"code": "redirect", "status": status}
        if status != 200:
            return {"code": "http_error", "status": status}
        chunks = []
        size = 0
        # One extra decoded byte is sufficient to detect overflow; avoid
        # buffering an additional full chunk past the 16 KiB response ceiling.
        for chunk in response.iter_content(chunk_size=MAX_RESPONSE_BYTES + 1):
            if not chunk:
                continue
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                return {"code": "body_too_large", "status": status}
            chunks.append(chunk)
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {"code": "invalid_response", "status": status}
        choices = data.get("choices") if isinstance(data, dict) else None
        if (not isinstance(choices, list) or not choices or
                not isinstance(choices[0], dict) or
                not isinstance(choices[0].get("message"), dict) or
                not isinstance(choices[0]["message"].get("content"), str) or
                not choices[0]["message"]["content"].strip()):
            return {"code": "invalid_response", "status": status}
        return {"code": "ok", "status": status}
    except requests.exceptions.SSLError:
        return {"code": "tls_error", "status": None}
    except requests.exceptions.RequestException:
        return {"code": "transport_error", "status": None}
    except Exception:  # noqa: BLE001 - Never propagate a message that may contain request data.
        return {"code": "transport_error", "status": None}
    finally:
        if response is not None:
            response.close()
        session.close()


def _arm_child_deadline(seconds: float = MAX_CHILD_SECONDS) -> None:
    # The parent normally enforces a shorter deadline. This Linux watchdog
    # also bounds the request if its Odoo parent is killed and the child orphaned.
    if sys.platform.startswith("linux"):
        signal.setitimer(signal.ITIMER_REAL, seconds)


def _child_main() -> int:
    _arm_child_deadline()
    try:
        raw = sys.stdin.buffer.read(_CHILD_MAX_INPUT + 1)
        if len(raw) > _CHILD_MAX_INPUT:
            result = {"code": "invalid_input", "status": None}
        else:
            payload = json.loads(raw.decode("ascii"))
            result = _child_request(payload, MAX_CHILD_SECONDS)
        safe = {"code": result.get("code"), "status": result.get("status")}
        if safe["code"] not in _CHILD_RESULT_CODES:
            safe = {"code": "invalid_input", "status": None}
    except Exception:  # noqa: BLE001 - The child protocol must never emit exception text.
        safe = {"code": "invalid_input", "status": None}
    sys.stdout.write(json.dumps(safe, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__" and sys.argv[1:] == ["--child"]:
    raise SystemExit(_child_main())
