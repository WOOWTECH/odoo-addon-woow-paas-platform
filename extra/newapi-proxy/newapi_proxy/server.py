"""Two uvicorn listeners in one process, each with its own connection limit.

* proxy port (``PROXY_PORT``, 8080): the signed pipeline, ``ProxyApp``;
* admin port (``PROXY_ADMIN_PORT``, 8081): ``/healthz``, ``/readyz``,
  ``/metrics`` (``AdminApp``) — kubelet probes keep working even when the
  proxy port is saturated, and the proxy port no longer serves any
  unauthenticated path at all.

uvicorn has no deadline for the request *headers*: a connection that sends
nothing, or one byte at a time, would hold a ``limit_concurrency`` slot
forever. ``HeaderDeadlineH11Protocol`` closes a connection whose next request
line + headers have not fully arrived ``HEADER_TIMEOUT`` seconds after the
connection opened or after the previous response finished. The body has its
own deadline in the app (``app.BODY_TIMEOUT``).

Only the proxy server handles SIGTERM / SIGINT (graceful: stop accepting,
finish in-flight calls for up to ``GRACEFUL_SHUTDOWN`` seconds, close the
upstream clients); the admin server just stops with it.
"""

from __future__ import annotations

import asyncio
import contextlib

import h11
import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from .app import AdminApp, ProxyApp
from .settings import Settings

#: Seconds a connection may take to deliver a request's line and headers.
HEADER_TIMEOUT = 5.0
#: >= the longest request (30 s read timeout, which bounds pre-checks and the
#: upstream call) plus margin; the chart's terminationGracePeriodSeconds is
#: preStop (5) + this + margin.
GRACEFUL_SHUTDOWN = 40
#: Admin port: probes, Odoo's health check and metrics only.
ADMIN_MAX_CONNECTIONS = 32


class HeaderDeadlineH11Protocol(H11Protocol):
    """H11Protocol that drops connections slow to send request headers."""

    header_timeout = HEADER_TIMEOUT

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self._arm_header_deadline()

    def on_response_complete(self) -> None:
        super().on_response_complete()
        self._arm_header_deadline()

    def connection_lost(self, exc) -> None:
        handle = getattr(self, "_header_deadline", None)
        if handle is not None:
            handle.cancel()
        super().connection_lost(exc)

    def _arm_header_deadline(self) -> None:
        handle = getattr(self, "_header_deadline", None)
        if handle is not None:
            handle.cancel()
        self._header_deadline = self.loop.call_later(self.header_timeout, self._header_deadline_expired)

    def _header_deadline_expired(self) -> None:
        self._header_deadline = None
        # IDLE: h11 is still waiting for (the rest of) a request line and
        # headers. Any later state means a request is being served.
        if self.conn.their_state is h11.IDLE and not self.transport.is_closing():
            self.transport.close()


class _AdminServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # The proxy server owns the signal handlers; this one stops with it.
        yield


def build_servers(
    settings: Settings, proxy_app: ProxyApp, admin_app: AdminApp
) -> tuple[uvicorn.Server, uvicorn.Server]:
    common = dict(
        host="0.0.0.0",
        http=HeaderDeadlineH11Protocol,
        loop="asyncio",
        log_config=None,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        date_header=False,
        timeout_keep_alive=5,
    )
    main = uvicorn.Server(
        uvicorn.Config(
            proxy_app,
            port=settings.port,
            lifespan="on",
            limit_concurrency=settings.max_connections,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN,
            **common,
        )
    )
    admin = _AdminServer(
        uvicorn.Config(
            admin_app,
            port=settings.admin_port,
            lifespan="off",
            limit_concurrency=ADMIN_MAX_CONNECTIONS,
            timeout_graceful_shutdown=2,
            **common,
        )
    )
    return main, admin


async def serve(main: uvicorn.Server, admin: uvicorn.Server) -> None:
    admin_task = asyncio.create_task(admin.serve())
    try:
        await main.serve()
    finally:
        admin.should_exit = True
        await admin_task
