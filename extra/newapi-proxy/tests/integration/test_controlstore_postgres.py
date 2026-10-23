"""Real PostgreSQL acceptance, serial execution in an EMPTY dedicated scratch DB.

PROXY_CONTROL_TEST_DSN is scratch administrator DSN, with CREATEROLE and SET ROLE
permissions. REQUIRE_PROXY_CONTROL_TEST=1 turns missing DSN into collection error.
No databases are created/dropped. Exact randomized fixture roles and the schema
created by this fixture alone are cleaned up; a preexisting schema is refused.
"""
import asyncio
import os
from pathlib import Path
import secrets
from uuid import uuid4

import pytest

_DSN = os.environ.get('PROXY_CONTROL_TEST_DSN')
if not _DSN:
    if os.environ.get('REQUIRE_PROXY_CONTROL_TEST') == '1':
        pytest.fail('PROXY_CONTROL_TEST_DSN required', pytrace=False)
    pytest.skip('PROXY_CONTROL_TEST_DSN absent', allow_module_level=True)

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from newapi_proxy.controlstore import ControlStoreError, PostgresControlStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope='module')
def database():
    suffix = uuid4().hex[:16]
    owner = 'pc_test_owner_' + suffix
    roles = ['pc_test_' + name + '_' + suffix for name in ('a', 'b', 'c')]
    password = secrets.token_hex(24)
    created, installed = [], False
    with psycopg.connect(_DSN, autocommit=True) as admin:
        if admin.execute("SELECT to_regnamespace('proxy_control')").fetchone()[0] is not None:
            pytest.fail('scratch DB has preexisting proxy_control schema; refusing to modify')
        dbname = admin.execute('SELECT current_database()').fetchone()[0]
        try:
            admin.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE').format(sql.Identifier(owner)))
            created.append(owner)
            admin.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(sql.Identifier(dbname), sql.Identifier(owner)))
            admin.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
            try:
                migration = Path(__file__).parents[2] / 'sql' / 'controlstore-v1.sql'
                admin.execute(migration.read_text(), prepare=False)
                installed = True
            finally:
                if admin.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
                    admin.execute('ROLLBACK')
                admin.execute('RESET ROLE')
            admin.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(sql.Identifier(dbname), sql.Identifier(owner)))
            for role in roles:
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}').format(sql.Identifier(role), sql.Literal(password)))
                created.append(role)
                admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(dbname), sql.Identifier(role)))
                admin.execute(sql.SQL('GRANT USAGE ON SCHEMA proxy_control TO {}').format(sql.Identifier(role)))
                admin.execute(sql.SQL('''GRANT EXECUTE ON FUNCTION proxy_control.acquire(text,text),
                    proxy_control.begin_attempt(text,text,integer,integer),
                    proxy_control.finish_attempt(text,text,text,integer,text),
                    proxy_control.read_events(text,bigint,bigint,integer), proxy_control.ping(text) TO {}''').format(sql.Identifier(role)))
            yield admin, roles, [make_conninfo(_DSN, user=r, password=password) for r in roles]
        finally:
            admin.execute('RESET ROLE')
            if installed:
                admin.execute('DROP SCHEMA proxy_control CASCADE')
            for role in reversed(created):
                admin.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
                admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@pytest.fixture
def control(database):
    admin, roles, dsns = database
    suffix = uuid4().hex[:16]
    env, other, scope = 'test_' + suffix, 'other_' + suffix, 'scope_' + suffix
    admin.execute('INSERT INTO proxy_control.rate_scope(scope) VALUES (%s)', (scope,))
    admin.execute('INSERT INTO proxy_control.environment(env) VALUES (%s),(%s)', (env, other))
    for role, bound in zip(roles, [env, env, other]):
        admin.execute('''INSERT INTO proxy_control.role_binding(role_name,env,scope) VALUES (%s,%s,%s)
            ON CONFLICT(role_name) DO UPDATE SET env=excluded.env,scope=excluded.scope''', (role, bound, scope))
    return admin, roles, dsns, env, other, scope


def stores(c, timeout=5):
    return [PostgresControlStore(dsn, env, timeout) for dsn, env in zip(c[2], [c[3], c[3], c[4]])]


def test_runtime_grants_and_forgery(control):
    c = control
    with psycopg.connect(c[2][0], autocommit=True) as runtime:
        for query in ['SELECT * FROM proxy_control.event',
                      'UPDATE proxy_control.environment SET highwater=0',
                      'DELETE FROM proxy_control.event',
                      "INSERT INTO proxy_control.rate_scope(scope) VALUES ('forged')",
                      'CREATE TABLE proxy_control.forged(x integer)',
                      'SELECT proxy_control.binding(%s)']:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute(query, (c[3],) if '%s' in query else None)
        with pytest.raises(psycopg.errors.RaiseException):
            runtime.execute('SELECT proxy_control.acquire(%s,%s)', (c[4], 'conversion'))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            runtime.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(c[1][1])))
        assert runtime.execute('SELECT proxy_control.ping(%s)', (c[3],)).fetchone()[0]
        assert runtime.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone() == (False, False, False)
        assert not runtime.execute("SELECT has_schema_privilege('proxy_control','CREATE')").fetchone()[0]
        assert not runtime.execute("""SELECT EXISTS (SELECT FROM pg_proc p,
            LATERAL aclexplode(coalesce(p.proacl, acldefault('f',p.proowner))) a
            WHERE p.oid='proxy_control.binding(text)'::regprocedure
            AND a.grantee=0 AND a.privilege_type='EXECUTE')""").fetchone()[0]


def test_future_functions_default_to_no_public_execute(database):
    admin, _, dsns = database
    owner = admin.execute("SELECT nspowner::regrole::text FROM pg_namespace WHERE nspname='proxy_control'").fetchone()[0]
    admin.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
    try:
        admin.execute("CREATE FUNCTION proxy_control.future_private() RETURNS integer LANGUAGE sql AS 'SELECT 42'")
    finally:
        admin.execute('RESET ROLE')
    try:
        with psycopg.connect(dsns[0], autocommit=True) as runtime:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime.execute('SELECT proxy_control.future_private()')
    finally:
        admin.execute('DROP FUNCTION proxy_control.future_private()')


def test_shared_budget_across_roles_and_restart(control):
    async def scenario():
        # Keep each wave below the port-forward/CI connection capacity while
        # still racing distinct logins against the same locked rate scope.
        ss = stores(control, timeout=12)
        values = []
        for wave in range(7):
            values.extend(await asyncio.gather(
                *(ss[(wave * 10 + i) % 3].acquire('conversion') for i in range(10))
            ))
        assert values.count(0) == 60
        assert all(1 <= v <= 60 for v in values if v)
        for s in ss:
            await s.aclose()
        assert 1 <= await stores(control)[0].acquire('conversion') <= 60
    run(scenario())


@pytest.mark.parametrize('priority,cap', [('conversion', 60), ('free_meter', 50), ('other', 40), ('balance', 30)])
def test_database_clock_boundary(control, priority, cap):
    admin, _, _, _, _, scope = control
    admin.execute('''UPDATE proxy_control.rate_scope SET last_clock=statement_timestamp(),
        slots=array_fill(statement_timestamp(), ARRAY[%s]) WHERE scope=%s''', (cap, scope))
    assert 1 <= run(stores(control)[0].acquire(priority)) <= 60
    admin.execute('''UPDATE proxy_control.rate_scope SET last_clock=statement_timestamp(),
        slots=array_fill(statement_timestamp()-interval '60 seconds', ARRAY[%s]) WHERE scope=%s''', (cap, scope))
    assert run(stores(control)[1].acquire(priority)) == 0
    assert admin.execute('SELECT cardinality(slots) FROM proxy_control.rate_scope WHERE scope=%s', (scope,)).fetchone()[0] == 1


def test_backwards_clock(control):
    control[0].execute('''UPDATE proxy_control.rate_scope SET last_clock=statement_timestamp()+interval '5 minutes',
        slots=ARRAY[statement_timestamp()+interval '5 minutes'] WHERE scope=%s''', (control[5],))
    assert run(stores(control)[0].acquire('conversion')) == 60
    assert control[0].execute('SELECT cardinality(slots) FROM proxy_control.rate_scope WHERE scope=%s', (control[5],)).fetchone()[0] == 1


def test_begin_replay_exact_outcome_idempotency_and_conflict(control):
    async def scenario():
        a, b, _ = stores(control)
        attempt = str(uuid4())
        assert await a.begin(attempt, 1, 1) == 1
        with pytest.raises(ControlStoreError, match='rejected'):
            await b.begin(attempt, 1, 1)
        await asyncio.gather(*(s.finish(attempt, 'forwarded', 200, 'redacted-safe-id') for s in [a, b] * 5))
        for payload in [('timeout', None, None), ('forwarded', 201, 'redacted-safe-id'), ('forwarded', 200, None)]:
            with pytest.raises(ControlStoreError, match='rejected'):
                await a.finish(attempt, *payload)
        with pytest.raises(ControlStoreError, match='rejected'):
            await a.finish(str(uuid4()), 'not_sent')
        page = await a.read(0)
        assert page['highwater'] == 2
        assert [e['kind'] for e in page['events']] == ['intent', 'outcome']
    run(scenario())


def test_fixed_head_late_outcome_and_fresh_counter(control):
    async def scenario():
        a = stores(control)[0]
        attempt = str(uuid4())
        assert await a.begin(attempt, 1, 10) == 1
        head = (await a.read(0, limit=1))['highwater']
        await a.aclose()
        b = stores(control)[1]
        assert await b.begin(str(uuid4()), 2, 20) == 2
        await b.finish(attempt, 'timeout')
        old = await b.read(0, through=head)
        assert old['highwater'] == 1 and len(old['events']) == 1
        late = await b.read(head)
        assert [e['seq'] for e in late['events']] == [2, 3]
        assert late['events'][1]['attempt_id'] == attempt and late['events'][1]['result'] == 'timeout'
        assert (await b.read(3))['events'] == []
    run(scenario())


def test_rollback_and_blocked_uncommitted_writer(control):
    async def scenario():
        writer = await psycopg.AsyncConnection.connect(control[2][0])
        cursor = await writer.execute('SELECT proxy_control.begin_attempt(%s,%s,%s,%s)', (control[3], str(uuid4()), 1, 1))
        assert (await cursor.fetchone())[0] == 1
        s = stores(control)[1]
        pending_read = asyncio.create_task(s.read(0))
        pending_write = asyncio.create_task(s.begin(str(uuid4()), 2, 2))
        try:
            for _ in range(150):
                blocked = control[0].execute("SELECT count(*) FROM pg_stat_activity WHERE usename=%s AND wait_event_type='Lock'", (control[1][1],)).fetchone()[0]
                if blocked == 2:
                    break
                await asyncio.sleep(0.01)
            assert blocked == 2 and not pending_read.done() and not pending_write.done()
            await writer.rollback()
            assert await pending_write == 1
            assert (await pending_read)['highwater'] in (0, 1)
            page = await s.read(0)
            assert page['highwater'] == 1 and [e['seq'] for e in page['events']] == [1]
        finally:
            await writer.close()
            await asyncio.gather(pending_read, pending_write, return_exceptions=True)
    run(scenario())


@pytest.mark.parametrize('query,args', [
    ('SELECT proxy_control.acquire(%s,%s)', ('unknown',)),
    ('SELECT proxy_control.begin_attempt(%s,%s,%s,%s)', ('not-a-uuid', 1, 1)),
    ('SELECT proxy_control.begin_attempt(%s,%s,%s,%s)', (str(uuid4()), 0, 1)),
    ('SELECT proxy_control.begin_attempt(%s,%s,%s,%s)', (str(uuid4()), 1, 156250001)),
    ('SELECT proxy_control.finish_attempt(%s,%s,%s,%s,%s)', (str(uuid4()), 'forwarded', None, None)),
    ('SELECT proxy_control.finish_attempt(%s,%s,%s,%s,%s)', (str(uuid4()), 'timeout', 200, None)),
    ('SELECT proxy_control.finish_attempt(%s,%s,%s,%s,%s)', (str(uuid4()), 'forwarded', 99, None)),
    ('SELECT proxy_control.read_events(%s,%s,%s,%s)', (-1, None, 1)),
    ('SELECT proxy_control.read_events(%s,%s,%s,%s)', (0, None, 101)),
    ('SELECT proxy_control.read_events(%s,%s,%s,%s)', (0, 1, 1)),
])
def test_sql_input_validation(control, query, args):
    with psycopg.connect(control[2][0], autocommit=True) as runtime:
        with pytest.raises(psycopg.errors.RaiseException):
            runtime.execute(query, (control[3], *args))


@pytest.mark.parametrize('receipt', ['nap_secret', 'sk-secret', 'Bearer.secret', 'a' * 64,
    'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature'])
def test_sql_rejects_credential_receipts(control, receipt):
    attempt = str(uuid4())
    run(stores(control)[0].begin(attempt, 1, 1))
    with psycopg.connect(control[2][0], autocommit=True) as runtime:
        with pytest.raises(psycopg.errors.RaiseException):
            runtime.execute('SELECT proxy_control.finish_attempt(%s,%s,%s,%s,%s)', (control[3], attempt, 'forwarded', 200, receipt))


def test_missing_binding_unknown_version_and_missing_sequence(control):
    admin, roles, _, env, _, _ = control
    admin.execute('DELETE FROM proxy_control.role_binding WHERE role_name=%s', (roles[0],))
    with pytest.raises(ControlStoreError, match='rejected'):
        run(stores(control)[0].read(0))
    admin.execute('DELETE FROM proxy_control.schema_version')
    try:
        with pytest.raises(ControlStoreError, match='rejected'):
            run(stores(control)[1].read(0))
        assert not run(stores(control)[1].ping())
    finally:
        admin.execute('INSERT INTO proxy_control.schema_version VALUES (true,1)')
    run(stores(control)[1].begin(str(uuid4()), 1, 1))
    admin.execute('DELETE FROM proxy_control.event WHERE env=%s AND seq=1', (env,))
    with pytest.raises(ControlStoreError, match='rejected'):
        run(stores(control)[1].read(0))


def test_invalid_scope_does_not_read_as_empty(control):
    control[0].execute('UPDATE proxy_control.rate_scope SET slots=ARRAY[NULL]::timestamptz[] WHERE scope=%s', (control[5],))
    with pytest.raises(ControlStoreError, match='rejected'):
        run(stores(control)[0].read(0))
    assert not run(stores(control)[0].ping())


def test_failed_connection_bounded_and_sanitized(control):
    dsn = make_conninfo(control[2][0], host='/tmp/pc-nonexistent-' + uuid4().hex, password='sensitive-test-password')
    async def scenario():
        started = asyncio.get_running_loop().time()
        s = PostgresControlStore(dsn, control[3], 0.1)
        with pytest.raises(ControlStoreError) as error:
            await s.acquire('conversion')
        assert asyncio.get_running_loop().time() - started < 1
        assert error.value.kind in ('unavailable', 'timeout')
        assert 'sensitive' not in repr(error.value) and dsn not in repr(s)
    run(scenario())


@pytest.mark.parametrize("lost_begin_reply", [False, True])
def test_correlated_http_deliveries_share_real_durable_begin(control, lost_begin_reply):
    """Actual PG transaction gate plus signed HTTP: no second quota POST."""
    import json

    from proxy_harness import ok
    from test_proxy_control_flow import configured

    async def scenario():
        a, b, _ = stores(control)
        first, _ = configured()
        second, _ = configured()
        first.proxy.control, second.proxy.control = a, b
        attempt = str(uuid4())
        body = json.dumps({"id": 123, "action": "add_quota", "mode": "add",
                           "value": 15625, "woow_conversion_attempt_id": attempt}).encode()
        started, release = asyncio.Event(), asyncio.Event()

        async def hold(req):
            started.set()
            await release.wait()
            return ok(request_id="pg-once")

        first.upstream.on("POST", "/api/user/manage", hold)
        if lost_begin_reply:
            begin = a.begin

            async def lose_reply(*args):
                await begin(*args)
                raise ControlStoreError("timeout")

            a.begin = lose_reply

        async def deliver(h):
            headers = h.sign_headers("POST", "/api/user/manage", body=body, identity="ap")
            return await h.acall("POST", b"/api/user/manage", headers=headers, body=body)

        if lost_begin_reply:
            assert (await deliver(first)).proxy_error == "attempt_unknown"
            assert (await deliver(second)).proxy_error == "attempt_unknown"
            assert not first.upstream.calls("POST") and not second.upstream.calls("POST")
        else:
            task = asyncio.create_task(deliver(first))
            try:
                await asyncio.wait_for(started.wait(), 3)
                assert (await deliver(second)).proxy_error == "attempt_unknown"
            finally:
                release.set()
            assert (await task).status == 200
            assert len(first.upstream.calls("POST")) == 1 and not second.upstream.calls("POST")
        events = (await b.read(0))["events"]
        mine = [e for e in events if e["attempt_id"] == attempt]
        assert [e["kind"] for e in mine] == (["intent"] if lost_begin_reply else ["intent", "outcome"])
        if not lost_begin_reply:
            assert mine[1]["result"] == "forwarded"
        await a.aclose()
        await b.aclose()
    run(scenario())
