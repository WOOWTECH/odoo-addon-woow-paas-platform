"""The minimal RESP2 client for the shared replay record, against an in-process
fake Valkey that enforces the chart's ACL (SET under the prefix, PING).

The same client was also run against the real pinned Valkey image with the
chart's ``--user`` arguments (see the PR); this suite pins the behaviour that
matters for fail-closed replay protection.
"""

import asyncio

import pytest

from newapi_proxy.replaystore import KEY_PREFIX, ReplayStoreUnavailable, ValkeyReplayStore, encode_command

USER, PASSWORD = "newapi-proxy", "ProxyPassword0000111122223333"


class FakeValkey:
    """Speaks just enough RESP2: AUTH, SET k v NX EX n, PING, ACL-limited."""

    def __init__(self, mode: str = "ok"):
        self.mode = mode  # ok | silent | close_mid_reply | error_reply
        self.keys: dict[bytes, bytes] = {}
        self.connections = 0
        self.commands: list[list[bytes]] = []
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _read_command(self, reader) -> list[bytes]:
        header = await reader.readuntil(b"\r\n")
        assert header.startswith(b"*")
        args = []
        for _ in range(int(header[1:-2])):
            length = int((await reader.readuntil(b"\r\n"))[1:-2])
            args.append((await reader.readexactly(length + 2))[:-2])
        return args

    async def _client(self, reader, writer):
        self.connections += 1
        authed = False
        try:
            while True:
                args = await self._read_command(reader)
                self.commands.append(args)
                cmd = args[0].upper()
                if self.mode == "silent" and cmd != b"AUTH":
                    await reader.read()  # never answer; return once the client hangs up
                    return
                if self.mode == "close_mid_reply" and cmd == b"SET":
                    writer.write(b"$")
                    await writer.drain()
                    writer.close()
                    return
                if cmd == b"AUTH":
                    authed = args[1:] == [USER.encode(), PASSWORD.encode()]
                    writer.write(b"+OK\r\n" if authed else b"-WRONGPASS invalid username-password pair\r\n")
                elif not authed:
                    writer.write(b"-NOAUTH Authentication required.\r\n")
                elif self.mode == "error_reply":
                    writer.write(b"-ERR something " + PASSWORD.encode() + b"\r\n")
                elif cmd == b"PING":
                    writer.write(b"+PONG\r\n")
                elif cmd == b"SET" and args[3:4] == [b"NX"]:
                    if not args[1].startswith(KEY_PREFIX.encode()):
                        writer.write(b"-NOPERM No permissions to access a key\r\n")
                    elif args[1] in self.keys:
                        writer.write(b"$-1\r\n")
                    else:
                        self.keys[args[1]] = args[2]
                        writer.write(b"+OK\r\n")
                else:
                    writer.write(b"-NOPERM no permissions\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def run(coro):
    return asyncio.run(coro)


def store(port, password=PASSWORD, **kw):
    return ValkeyReplayStore("127.0.0.1", port, USER, password, op_timeout=kw.pop("op_timeout", 0.5), **kw)


def test_encode_command():
    assert encode_command(b"SET", b"k", b"1") == b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$1\r\n1\r\n"


def test_claim_once_then_replay_and_connection_reuse():
    async def scenario():
        fake = await FakeValkey().start()
        s = store(fake.port)
        try:
            first = await s.claim("cron-a:nonce1", 62)
            second = await s.claim("cron-a:nonce1", 62)
            other = await s.claim("cron-a:nonce2", 62)
            ping = await s.ping()
        finally:
            await s.aclose()
            await fake.stop()
        return first, second, other, ping, fake

    first, second, other, ping, fake = run(scenario())
    assert (first, second, other, ping) == (True, False, True, True)
    assert fake.connections == 1  # pooled
    assert [b"SET", (KEY_PREFIX + "cron-a:nonce1").encode(), b"1", b"NX", b"EX", b"62"] in fake.commands


def test_concurrent_claims_have_exactly_one_winner():
    async def scenario():
        fake = await FakeValkey().start()
        s = store(fake.port, max_connections=4)
        try:
            return await asyncio.gather(*[s.claim("k:same", 62) for _ in range(20)])
        finally:
            await s.aclose()
            await fake.stop()

    assert sum(run(scenario())) == 1


@pytest.mark.parametrize(
    "mode, password, kind, ping_ok",
    [
        ("ok", "WrongPassword000011112222333", "auth", False),
        ("silent", PASSWORD, "timeout", False),
        ("close_mid_reply", PASSWORD, "connect", True),  # only SET breaks
        ("error_reply", PASSWORD, "error", False),
    ],
)
def test_failures_are_unavailable_with_a_stable_kind_and_no_text(mode, password, kind, ping_ok):
    async def scenario():
        fake = await FakeValkey(mode).start()
        s = store(fake.port, password=password, op_timeout=0.3)
        try:
            with pytest.raises(ReplayStoreUnavailable) as info:
                await s.claim("k:n", 62)
            return info.value, await s.ping()
        finally:
            await s.aclose()
            await fake.stop()

    err, ping = run(scenario())
    assert err.kind == kind
    assert PASSWORD not in str(err) and PASSWORD not in repr(err)
    # raised "from None": no socket / Valkey exception (or its text) rides along
    assert err.__cause__ is None
    assert err.__suppress_context__ or err.__context__ is None
    assert ping is ping_ok


def test_key_outside_the_acl_prefix_is_refused():
    async def scenario():
        fake = await FakeValkey().start()
        s = ValkeyReplayStore("127.0.0.1", fake.port, USER, PASSWORD, key_prefix="elsewhere:")
        try:
            with pytest.raises(ReplayStoreUnavailable) as info:
                await s.claim("k:n", 62)
            return info.value.kind
        finally:
            await s.aclose()
            await fake.stop()

    assert run(scenario()) == "error"


def test_no_server_is_connect():
    async def scenario():
        fake = await FakeValkey().start()
        port = fake.port
        await fake.stop()
        with pytest.raises(ReplayStoreUnavailable) as info:
            await store(port).claim("k:n", 62)
        return info.value.kind

    assert run(scenario()) == "connect"


def test_dead_idle_connections_are_dropped_after_a_restart():
    async def scenario():
        fake = await FakeValkey().start()
        s = store(fake.port, max_connections=4)
        await asyncio.gather(*[s.claim(f"k:{i}", 62) for i in range(4)])
        pooled = len(s._idle)
        # "restart": every pooled connection dies
        for conn in list(s._idle):
            conn.writer.transport.abort()
        await asyncio.sleep(0.05)
        with pytest.raises(ReplayStoreUnavailable):
            await s.claim("k:after", 62)
        dropped = len(s._idle)
        fresh = await s.claim("k:after2", 62)
        await s.aclose()
        await fake.stop()
        return pooled, dropped, fresh

    pooled, dropped, fresh = run(scenario())
    assert pooled >= 1
    assert dropped == 0  # one failure on a reused connection drops all idle ones
    assert fresh is True


def test_repr_never_shows_the_password():
    assert PASSWORD not in repr(store(6379))
