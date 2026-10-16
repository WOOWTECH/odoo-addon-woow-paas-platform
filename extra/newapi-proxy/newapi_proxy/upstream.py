"""Upstream (new-api) calls and transport-error classification (DESIGN §1.4.2-7).

* ``connect`` — the connection was never established: the request was
  certainly not sent (502, safe to resend).
* ``reset``   — the connection broke after the request may have been written
  (502, outcome unknown).
* ``timeout`` — no response within the read timeout (504, outcome unknown).

The proxy never retries (transport ``retries=0``, no redirects, no proxy env).
Writes use a connection that is closed after one exchange, so a stale pooled
socket can never turn a write into an "unknown". Exception *text* is dropped:
only the class name reaches the log.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

CONNECT = "connect"
RESET = "reset"
TIMEOUT = "timeout"


class UpstreamError(Exception):
    def __init__(self, kind: str, exc_type: str, detail: str = ""):
        super().__init__(kind)
        self.kind = kind
        self.exc_type = exc_type
        self.detail = detail


@dataclass
class UpstreamResponse:
    status: int
    headers: httpx.Headers
    body: bytes


def classify(exc: BaseException) -> str:
    # Order matters: ConnectTimeout/PoolTimeout are TimeoutExceptions and
    # ConnectError is a NetworkError.
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return CONNECT
    if isinstance(exc, httpx.TimeoutException):
        return TIMEOUT
    return RESET


class Upstream:
    def __init__(
        self,
        base_url: str,
        connect_timeout: float,
        max_response_bytes: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.connect_timeout = connect_timeout
        self.max_response_bytes = max_response_bytes
        common = dict(base_url=base_url, follow_redirects=False, trust_env=False)
        pooled = transport or httpx.AsyncHTTPTransport(
            retries=0,
            limits=httpx.Limits(max_connections=128, max_keepalive_connections=16, keepalive_expiry=4.0),
        )
        fresh = transport or httpx.AsyncHTTPTransport(
            retries=0, limits=httpx.Limits(max_connections=64, max_keepalive_connections=0)
        )
        self._pooled = httpx.AsyncClient(transport=pooled, **common)
        self._fresh = httpx.AsyncClient(transport=fresh, **common)

    async def aclose(self) -> None:
        await self._pooled.aclose()
        await self._fresh.aclose()

    async def send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        read_timeout: float,
        write: bool,
    ) -> UpstreamResponse:
        client = self._fresh if write else self._pooled
        timeout = httpx.Timeout(
            connect=self.connect_timeout, read=read_timeout, write=read_timeout, pool=self.connect_timeout
        )
        request = client.build_request(method, url, headers=headers, content=body, timeout=timeout)
        try:
            response = await client.send(request, stream=True)
        except Exception as exc:  # noqa: BLE001 - classified, text dropped
            raise UpstreamError(classify(exc), type(exc).__name__) from None
        try:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise UpstreamError(RESET, "ResponseTooLarge", "response_too_large")
                chunks.append(chunk)
        except UpstreamError:
            raise
        except Exception as exc:  # noqa: BLE001
            kind = classify(exc)
            # The headers already arrived: the request reached new-api.
            raise UpstreamError(RESET if kind == CONNECT else kind, type(exc).__name__) from None
        finally:
            await response.aclose()
        return UpstreamResponse(status=response.status_code, headers=response.headers, body=b"".join(chunks))
