"""Verify control-store ordering through the real signed proxy pipeline."""
import asyncio
import time

import httpx
import pytest

from newapi_proxy.controlstore import ControlStoreError
from newapi_proxy.settings import SettingsError, load_settings
from proxy_harness import Harness, NOW, base_env, ok


class FakeControl:
    def __init__(self):
        self.calls = []
        self.fail = None
        self.refuse_at = None
        self.acquired = 0
        self.intents = {}
        self.finished = {}
        self.available = True

    async def acquire(self, priority):
        self.calls.append(('acquire', priority))
        if self.fail == 'acquire':
            raise ControlStoreError('unavailable')
        self.acquired += 1
        return 17 if self.refuse_at == self.acquired else 0

    async def begin(self, attempt, target, value):
        self.calls.append(('begin', attempt))
        if self.fail == 'begin':
            raise ControlStoreError('unavailable')
        assert attempt not in self.intents
        self.intents[attempt] = (target, value)
        return len(self.intents)

    async def finish(self, attempt, result, status, receipt):
        self.calls.append(('finish', result))
        if self.fail == 'finish':
            raise ControlStoreError('unavailable')
        self.finished[attempt] = (result, status, receipt)

    async def ping(self):
        return self.available

    async def aclose(self):
        pass


def configured():
    h = Harness()
    store = FakeControl()
    h.proxy.control = store
    h.upstream.on('GET', '/api/user/123', ok({
        'id': 123, 'username': 'wsab0000000123', 'role': 1, 'group': 'default',
    }))
    return h, store


def credit(h):
    return h.signed('POST', '/api/user/manage', identity='ap', body={
        'id': 123, 'action': 'add_quota', 'mode': 'add', 'value': 15625,
    })


def test_commit_intent_before_credit_and_charge_precheck_and_write():
    h, store = configured()
    def upstream(request):
        assert len(store.intents) == 1
        assert store.acquired == 2
        assert not store.finished
        return ok(request_id='credit-receipt')
    h.upstream.on('POST', '/api/user/manage', upstream)
    res = credit(h)
    assert res.status == 200
    assert [c[0] for c in store.calls] == ['acquire', 'begin', 'acquire', 'finish']
    assert list(store.finished.values()) == [('forwarded', 200, 'credit-receipt')]
    assert len(h.upstream.requests) == 2


@pytest.mark.parametrize('failure,read_count', [('acquire', 0), ('begin', 1)])
def test_unavailable_before_send_never_calls_write(failure, read_count):
    h, store = configured()
    store.fail = failure
    res = credit(h)
    assert res.status == 503 and res.proxy_error == 'busy'
    assert len(h.upstream.requests) == read_count
    assert all(r.method == 'GET' for r in h.upstream.requests)
    assert h.proxy.budget.used(h.mono) == 0


@pytest.mark.parametrize('refuse_at', [1, 2])
def test_refusal_covers_precheck_and_actual_write_without_false_execution(refuse_at):
    h, store = configured()
    store.refuse_at = refuse_at
    res = credit(h)
    assert res.status == 429 and res.proxy_error == 'budget'
    assert res.headers['retry-after'] == '17'
    assert len(h.upstream.requests) == refuse_at - 1
    assert all(r.method == 'GET' for r in h.upstream.requests)
    assert h.proxy.budget.used(h.mono) == 0
    if refuse_at == 2:
        assert list(store.finished.values()) == [('not_sent', None, None)]


def test_outcome_store_failure_preserves_success_and_pending_intent(caplog):
    h, store = configured()
    store.fail = 'finish'
    h.upstream.on('POST', '/api/user/manage', ok(request_id='credited-once'))
    res = credit(h)
    assert res.status == 200 and res.proxy_error is None
    assert len(store.intents) == 1 and not store.finished
    assert len(h.upstream.requests) == 2
    assert 'audit_outcome_pending' in caplog.text


@pytest.mark.parametrize('exc,code,kind', [
    (httpx.ConnectError, 502, 'connect'), (httpx.ReadError, 502, 'reset'),
    (httpx.ReadTimeout, 504, 'timeout'),
])
def test_transport_outcomes_are_not_retried(exc, code, kind):
    h, store = configured()
    def fail(request):
        raise exc('synthetic', request=request)
    h.upstream.on('POST', '/api/user/manage', fail)
    res = credit(h)
    assert res.status == code and res.proxy_error == kind
    assert list(store.finished.values()) == [(kind, None, None)]
    assert len(h.upstream.requests) == 2


def test_cancel_after_send_keeps_durable_intent_without_inventing_outcome():
    h, store = configured()
    def cancel(request):
        raise asyncio.CancelledError()
    h.upstream.on('POST', '/api/user/manage', cancel)
    with pytest.raises(asyncio.CancelledError):
        credit(h)
    assert len(store.intents) == 1 and not store.finished
    assert len(h.upstream.requests) == 2


@pytest.mark.parametrize('path,query,priority', [
    ('/api/user/', [], 'balance'),
    ('/api/user/123', [], 'conversion'),
    ('/api/log/', [('type', '2'), ('group', 'free')], 'free_meter'),
    ('/api/log/', [], 'other'),
])
def test_priority_comes_from_validated_route(path, query, priority):
    h, store = configured()
    h.signed('GET', path, query_pairs=query, identity='ap')
    assert store.calls == [('acquire', priority)]


def test_other_credentials_and_public_readiness_do_not_spend_ap_budget():
    h, store = configured()
    h.user('GET', '/api/token/1')
    h.signed('GET', '/api/option/', identity='op')
    assert not store.calls
    res = h.admin(b'/readyz')
    assert res.json['control_store'] == 'ok'
    store.available = False
    h.mono += 10
    assert h.admin(b'/readyz').status == 503


def test_slow_store_is_bounded_and_no_upstream_request_starts():
    h, store = configured()
    async def stuck(priority):
        await asyncio.sleep(5)
        return 0
    store.acquire = stuck
    start = time.monotonic()
    res = credit(h)
    assert res.status == 503 and time.monotonic() - start < 1.2
    assert not h.upstream.requests


@pytest.mark.parametrize('overrides', [
    {'PROXY_CONTROL_DSN': 'dbname=private'},
    {'PROXY_CONTROL_ENVIRONMENT': 'llmdev'},
    {'PROXY_CONTROL_DSN': 'dbname=x', 'PROXY_CONTROL_ENVIRONMENT': 'unsafe.env'},
])
def test_partial_or_invalid_control_config_refuses_start(overrides):
    with pytest.raises(SettingsError):
        load_settings(base_env(**overrides), NOW)


def test_control_settings_redact_dsn_and_disabled_state_is_explicit():
    dsn = 'dbname=private user=test password=sensitive-value'
    settings, _ = load_settings(base_env(PROXY_CONTROL_DSN=dsn, PROXY_CONTROL_ENVIRONMENT='llmdev'), NOW)
    assert settings.control_environment == 'llmdev'
    assert dsn in settings.secrets and dsn not in repr(settings)
    h = Harness()
    assert h.admin(b'/readyz').json['control_store'] == 'disabled'


def test_credential_shaped_receipt_never_enters_persistent_evidence():
    h, store = configured()
    h.upstream.on('POST', '/api/user/manage', ok(request_id='nap_' + 'x' * 40))
    assert credit(h).status == 200
    assert list(store.finished.values()) == [('forwarded', 200, None)]
