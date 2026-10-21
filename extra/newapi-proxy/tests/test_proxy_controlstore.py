"""Offline validation, timeout/cancellation and uncertain-commit contracts."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from newapi_proxy import controlstore as mod
from newapi_proxy.controlstore import ControlStoreError, PostgresControlStore

ATTEMPT = str(uuid4())
SECRET = 'postgresql://private-user:private-password@secret-host/private-db'


def run(coro):
    return asyncio.run(coro)


class FakeConnection:
    def __init__(self, stage=None, failure=None, value=0):
        self.stage, self.failure, self.value = stage, failure, value
        self.calls = []
        self.commits = 0
        self.closed = False
        self.pgconn = SimpleNamespace(finish=self.close)

    def close(self):
        self.closed = True
        if self.stage == 'close':
            raise RuntimeError(SECRET)

    async def step(self, name):
        if self.stage == name:
            if self.failure:
                raise self.failure
            await asyncio.Event().wait()

    async def execute(self, query, values=None):
        self.calls.append((query, values))
        await self.step('sql')
        return self

    async def fetchone(self):
        await self.step('fetch')
        return (self.value,)

    async def commit(self):
        self.commits += 1
        await self.step('commit')


def install(monkeypatch, conn, stage=None):
    calls = []

    async def connect(dsn):
        calls.append(dsn)
        if stage == 'connect':
            await asyncio.Event().wait()
        return conn

    monkeypatch.setattr(mod.psycopg.AsyncConnection, 'connect', connect)
    return calls


@pytest.mark.parametrize('kwargs', [
    {'dsn': ''}, {'dsn': None}, {'environment': ''}, {'environment': 'bad/env'},
    {'environment': 'x\n'}, {'environment': 'x' * 65}, {'op_timeout': 0},
    {'op_timeout': float('nan')}, {'op_timeout': float('inf')}, {'op_timeout': True},
])
def test_constructor_validation(kwargs):
    with pytest.raises(ControlStoreError, match='^invalid$'):
        PostgresControlStore(**({'dsn': SECRET, 'environment': 'test'} | kwargs))


def test_repr_and_exception_are_value_free():
    s = PostgresControlStore(SECRET, 'private-env')
    assert repr(s) == 'PostgresControlStore(<redacted>)'
    assert str(ControlStoreError(SECRET)) == 'unavailable'


@pytest.mark.parametrize('method,args', [
    ('acquire', ('unknown',)), ('acquire', ([],)),
    ('begin', (ATTEMPT.upper(), 1, 1)), ('begin', ('not-uuid', 1, 1)),
    ('begin', ('00000000-0000-1000-8000-000000000000', 1, 1)),
    ('begin', (ATTEMPT, True, 1)), ('begin', (ATTEMPT, 0, 1)),
    ('begin', (ATTEMPT, 2147483648, 1)), ('begin', (ATTEMPT, 1, 0)),
    ('begin', (ATTEMPT, 1, 156250001)), ('begin', (ATTEMPT, 1, 1.0)),
    ('finish', (ATTEMPT, 'unknown')), ('finish', (ATTEMPT, 'forwarded', 99)),
    ('finish', (ATTEMPT, 'forwarded', 600)), ('finish', (ATTEMPT, 'forwarded', True)),
    ('finish', (ATTEMPT, 'forwarded', 200, 'unsafe\n')),
    ('finish', (ATTEMPT, 'forwarded', 200, 'x' * 129)),
    ('finish', (ATTEMPT, 'forwarded', None, '')),
    ('read', (-1,)), ('read', (True,)), ('read', (0, None, 0)),
    ('read', (0, None, 101)), ('read', (2, 1, 1)), ('read', (0, True, 1)),
])
def test_invalid_input_never_connects(monkeypatch, method, args):
    calls = install(monkeypatch, FakeConnection())
    with pytest.raises(ControlStoreError, match='^invalid$'):
        run(getattr(PostgresControlStore(SECRET, 'test'), method)(*args))
    assert calls == []


@pytest.mark.parametrize('method,args,value', [
    ('acquire', ('conversion',), 0), ('acquire', ('balance',), 60),
    ('begin', (ATTEMPT, 2147483647, 156250000), 1),
    ('finish', (ATTEMPT, 'forwarded', 599, 'redacted-id:1'), None),
    ('read', (0, None, 100), {'env': 'test', 'highwater': 0, 'events': []}),
    ('ping', (), True),
])
def test_parameterized_transaction_and_close(monkeypatch, method, args, value):
    conn = FakeConnection(value=value)
    calls = install(monkeypatch, conn)
    actual = run(getattr(PostgresControlStore(SECRET, 'test'), method)(*args))
    assert actual == value
    assert len(calls) == 1 and conn.commits == 1 and conn.closed
    assert conn.calls[:2] == [('SET LOCAL synchronous_commit = on', None),
                              ('SET LOCAL search_path = pg_catalog', None)]
    query, params = conn.calls[2]
    assert query.startswith('SELECT proxy_control.')
    assert params[0] == 'test'
    assert ATTEMPT not in query and SECRET not in query


@pytest.mark.parametrize('stage', ['connect', 'sql', 'fetch', 'commit'])
def test_timeout_covers_entire_operation_without_retry(monkeypatch, stage):
    conn = FakeConnection(stage=stage)
    calls = install(monkeypatch, conn, stage=stage)

    async def scenario():
        started = asyncio.get_running_loop().time()
        with pytest.raises(ControlStoreError, match='^timeout$'):
            await PostgresControlStore(SECRET, 'test', 0.02).acquire('conversion')
        assert asyncio.get_running_loop().time() - started < 0.3

    run(scenario())
    assert len(calls) == 1
    assert conn.closed is (stage != 'connect')


@pytest.mark.parametrize('stage', ['sql', 'commit'])
def test_failure_sanitized_and_unknown_commit_never_retried(monkeypatch, stage):
    conn = FakeConnection(stage=stage, failure=RuntimeError(SECRET))
    calls = install(monkeypatch, conn)
    with pytest.raises(ControlStoreError) as error:
        run(PostgresControlStore(SECRET, 'test').acquire('conversion'))
    assert error.value.kind == 'unavailable'
    assert SECRET not in repr(error.value)
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert len(calls) == 1 and conn.closed


def test_server_rejection_is_stable(monkeypatch):
    install(monkeypatch, FakeConnection(stage='sql', failure=mod.psycopg.errors.RaiseException(SECRET)))
    with pytest.raises(ControlStoreError, match='^rejected$'):
        run(PostgresControlStore(SECRET, 'test').acquire('conversion'))


@pytest.mark.parametrize('stage', ['sql', 'commit'])
def test_cancellation_propagates_and_closes(monkeypatch, stage):
    conn = FakeConnection(stage=stage)
    install(monkeypatch, conn)

    async def scenario():
        task = asyncio.create_task(PostgresControlStore(SECRET, 'test', 1).acquire('conversion'))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert conn.closed


def test_cleanup_error_does_not_mask_result(monkeypatch):
    conn = FakeConnection(stage='close')
    install(monkeypatch, conn)
    assert run(PostgresControlStore(SECRET, 'test').acquire('conversion')) == 0
    assert conn.closed


@pytest.mark.parametrize('value', [None, True, -1, 61, '0'])
def test_invalid_acquire_reply_fails_closed(monkeypatch, value):
    install(monkeypatch, FakeConnection(value=value))
    with pytest.raises(ControlStoreError, match='^protocol$'):
        run(PostgresControlStore(SECRET, 'test').acquire('conversion'))


def test_closed_store_and_ping_false(monkeypatch):
    calls = install(monkeypatch, FakeConnection())

    async def scenario():
        store = PostgresControlStore(SECRET, 'test')
        await store.aclose()
        await store.aclose()
        assert await store.ping() is False
        with pytest.raises(ControlStoreError, match='^closed$'):
            await store.acquire('conversion')

    run(scenario())
    assert not calls


@pytest.mark.parametrize('receipt', ['nap_secret', 'sk-secret', 'Bearer.secret', 'a' * 64,
    'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature'])
def test_credential_receipts_never_connect(monkeypatch, receipt):
    calls = install(monkeypatch, FakeConnection())
    with pytest.raises(ControlStoreError, match='invalid'):
        run(PostgresControlStore(SECRET, 'test').finish(ATTEMPT, 'forwarded', 200, receipt))
    assert not calls


@pytest.mark.parametrize('payload', [('forwarded', None, None), ('timeout', 200, None),
                                      ('not_sent', None, 'safe-id')])
def test_outcome_payload_consistency(monkeypatch, payload):
    calls = install(monkeypatch, FakeConnection())
    with pytest.raises(ControlStoreError, match='invalid'):
        run(PostgresControlStore(SECRET, 'test').finish(ATTEMPT, *payload))
    assert not calls


@pytest.mark.parametrize('page', [
    {'env': 'test', 'highwater': 1, 'events': []},
    {'env': 'test', 'highwater': 2, 'events': [{'seq': 1}, {'seq': 3}]},
    {'env': 'test', 'highwater': 2, 'events': [{'seq': 1}, {'seq': 1}]},
    {'env': 'test', 'highwater': 1, 'events': [{'seq': True}]},
    {'env': 'test', 'highwater': 1, 'events': [{'seq': '1'}]},
    {'env': 'forged', 'highwater': 0, 'events': []},
])
def test_invalid_page_fails_closed(monkeypatch, page):
    install(monkeypatch, FakeConnection(value=page))
    with pytest.raises(ControlStoreError, match='protocol'):
        run(PostgresControlStore(SECRET, 'test').read(0))


@pytest.mark.parametrize('stage', ['connect', 'sql', 'commit', 'cancel'])
def test_real_psycopg_stall_deadline(monkeypatch, tmp_path, stage):
    """Actual libpq over Unix socket: no database or network endpoint needed."""
    import struct

    def packet(kind, payload=b''):
        return kind + struct.pack('!I', len(payload) + 4) + payload

    async def scenario():
        stalled = asyncio.Event()
        handlers, writers, cancels = set(), [], []

        async def forbidden_cancel(self, **kwargs):
            cancels.append(kwargs)
            await asyncio.Event().wait()
        monkeypatch.setattr(mod._DeadlineConnection, 'cancel_safe', forbidden_cancel)

        async def peer(reader, writer):
            handlers.add(asyncio.current_task())
            writers.append(writer)
            try:
                size = struct.unpack('!I', await reader.readexactly(4))[0]
                await reader.readexactly(size - 4)
                if stage == 'connect':
                    stalled.set()
                    await reader.read()
                    return
                writer.write(packet(b'R', struct.pack('!I', 0))
                    + packet(b'S', b'client_encoding\x00UTF8\x00')
                    + packet(b'S', b'server_version\x0016.0\x00')
                    + packet(b'S', b'standard_conforming_strings\x00on\x00')
                    + packet(b'K', struct.pack('!II', 123, 456)) + packet(b'Z', b'I'))
                await writer.drain()
                while True:
                    kind = await reader.readexactly(1)
                    size = struct.unpack('!I', await reader.readexactly(4))[0]
                    payload = await reader.readexactly(size - 4)
                    if stage in ('sql', 'cancel') or (kind == b'Q' and payload == b'COMMIT\x00'):
                        stalled.set()
                        await reader.read()
                        return
                    if kind == b'Q':
                        writer.write(packet(b'C', payload.split(b' ')[0].rstrip(b'\x00') + b'\x00') + packet(b'Z', b'T'))
                    elif kind == b'P':
                        writer.write(packet(b'1'))
                    elif kind == b'B':
                        writer.write(packet(b'2'))
                    elif kind == b'D':
                        field = b'acquire\x00' + struct.pack('!IhIhih', 0, 0, 23, 4, -1, 0)
                        writer.write(packet(b'T', struct.pack('!h', 1) + field))
                    elif kind == b'E':
                        writer.write(packet(b'D', struct.pack('!hi', 1, 1) + b'0') + packet(b'C', b'SELECT 1\x00'))
                    elif kind == b'S':
                        writer.write(packet(b'Z', b'T'))
                    elif kind == b'X':
                        return
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(asyncio.current_task())

        server = await asyncio.start_unix_server(peer, path=str(tmp_path / '.s.PGSQL.5432'))
        s = PostgresControlStore(f'host={tmp_path} port=5432 user=test dbname=test sslmode=disable',
                                 'test', 2 if stage == 'cancel' else 0.08)
        started = asyncio.get_running_loop().time()
        try:
            if stage == 'cancel':
                task = asyncio.create_task(s.acquire('conversion'))
                await asyncio.wait_for(stalled.wait(), 0.5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ControlStoreError, match='^timeout$'):
                    await s.acquire('conversion')
                assert stalled.is_set()
            assert asyncio.get_running_loop().time() - started < 0.5
            assert not cancels
        finally:
            server.close()
            await server.wait_closed()
            for writer in writers:
                writer.close()
            if handlers:
                await asyncio.wait_for(asyncio.gather(*list(handlers)), 0.5)
    run(scenario())
