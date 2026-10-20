"""Shared replay record in the release's Valkey (DESIGN §1.4.2-1).

Every replica records ``(kid, nonce)`` with ``SET <prefix><kid>:<nonce> 1 NX
EX <ttl>``: the first replica to see a nonce wins, any later copy — on any
replica — is a replay. The proxy logs in as its own ACL user, which may only
``SET`` keys under ``newapi-proxy:replay:`` and ``PING`` (chart
``templates/valkey.yaml``), so it can neither read nor change new-api's data.

Only the three commands above are needed, so this is a deliberately small
RESP2 client instead of a new dependency:

* a connection is used by one command at a time and goes back to the pool
  only after a clean reply; any error or timeout closes it, and after an
  error on a reused connection every idle one is dropped too (a Valkey
  restart leaves them all dead);
* every call is bounded by ``op_timeout`` (waiting for a pool slot, connect,
  AUTH and the reply together);
* nothing is retried: a ``SET`` whose reply was lost may have been applied,
  and retrying it would turn a fresh request into a false "replay". The
  caller fails closed instead (503 ``busy``) and the client re-sends with a
  new nonce;
* exception text from the socket or from Valkey never leaves this module:
  only a stable ``kind`` does (the password is an AUTH argument).
"""

from __future__ import annotations

import asyncio
from typing import Any

KEY_PREFIX = "newapi-proxy:replay:"

_CRLF = b"\r\n"


class ReplayStoreUnavailable(Exception):
    """No definite answer from Valkey. ``kind`` is a stable, value-free code."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class _ValkeyError(Exception):
    """A RESP error reply (``-ERR …``); the text is dropped."""


def encode_command(*args: bytes) -> bytes:
    out = [b"*%d\r\n" % len(args)]
    for arg in args:
        out.append(b"$%d\r\n%s\r\n" % (len(arg), arg))
    return b"".join(out)


async def read_reply(reader: asyncio.StreamReader) -> Any:
    """One RESP2 reply: simple string, error, integer or bulk string."""
    line = await reader.readuntil(_CRLF)
    kind, payload = line[:1], line[1:-2]
    if kind == b"+":
        return payload.decode("ascii", "replace")
    if kind == b"-":
        raise _ValkeyError()
    if kind == b":":
        return int(payload)
    if kind == b"$":
        length = int(payload)
        if length == -1:
            return None
        if not 0 <= length <= 1 << 20:
            raise ValueError("bulk length")
        data = await reader.readexactly(length + 2)
        if data[-2:] != _CRLF:
            raise ValueError("bulk terminator")
        return data[:-2]
    raise ValueError("unexpected reply type")


class _Connection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def command(self, *args: bytes) -> Any:
        self.writer.write(encode_command(*args))
        await self.writer.drain()
        return await read_reply(self.reader)

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001 - closing a broken socket
            pass


class ValkeyReplayStore:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        op_timeout: float = 0.5,
        max_connections: int = 16,
        key_prefix: str = KEY_PREFIX,
    ):
        self.host = host
        self.port = port
        self._username = username.encode("ascii")
        self._password = password.encode("ascii")
        self.op_timeout = op_timeout
        self.key_prefix = key_prefix
        self.max_connections = max_connections
        self._slots: asyncio.Semaphore | None = None
        self._idle: list[_Connection] = []

    def __repr__(self) -> str:  # never print the password
        return f"ValkeyReplayStore({self.host}:{self.port})"

    def _semaphore(self) -> asyncio.Semaphore:
        # Created lazily so it binds to the running loop.
        if self._slots is None:
            self._slots = asyncio.Semaphore(self.max_connections)
        return self._slots

    async def _open(self) -> _Connection:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        conn = _Connection(reader, writer)
        try:
            reply = await conn.command(b"AUTH", self._username, self._password)
        except _ValkeyError:
            conn.close()
            raise ReplayStoreUnavailable("auth") from None
        except BaseException:
            conn.close()
            raise
        if reply != "OK":
            conn.close()
            raise ReplayStoreUnavailable("auth")
        return conn

    def _drop_idle(self) -> None:
        idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()

    async def _run(self, *args: bytes) -> Any:
        try:
            async with asyncio.timeout(self.op_timeout):
                async with self._semaphore():
                    reused = bool(self._idle)
                    conn = self._idle.pop() if reused else await self._open()
                    try:
                        reply = await conn.command(*args)
                    except BaseException:
                        conn.close()
                        if reused:
                            self._drop_idle()
                        raise
                    self._idle.append(conn)
                    return reply
        except ReplayStoreUnavailable:
            raise
        except TimeoutError:
            raise ReplayStoreUnavailable("timeout") from None
        except _ValkeyError:
            raise ReplayStoreUnavailable("error") from None
        except (OSError, EOFError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            raise ReplayStoreUnavailable("connect") from None

    async def claim(self, key: str, ttl: int) -> bool:
        """Record ``key`` for ``ttl`` seconds. True: first use; False: replay."""
        reply = await self._run(
            b"SET", (self.key_prefix + key).encode("ascii"), b"1", b"NX", b"EX", str(int(ttl)).encode("ascii")
        )
        if reply == "OK":
            return True
        if reply is None:
            return False
        raise ReplayStoreUnavailable("protocol")

    async def ping(self) -> bool:
        try:
            return await self._run(b"PING") == "PONG"
        except ReplayStoreUnavailable:
            return False

    async def aclose(self) -> None:
        self._drop_idle()
