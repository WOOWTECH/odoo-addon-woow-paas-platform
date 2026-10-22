"""Slice B through signed HTTP; synthetic upstream and control store, no Odoo."""
import asyncio
import json
import logging

import httpx
import pytest
from newapi_proxy.controlstore import ControlStoreError
from proxy_harness import Harness, asgi_call, ok
from test_proxy_control_flow import FakeControl, configured

ATTEMPT = "8a2e668b-2756-479e-afcc-2aa19b9b25f5"
FIELD = "woow_conversion_attempt_id"
PATH = "/api/user/manage"
LEGACY = {"id": 123, "action": "add_quota", "mode": "add", "value": 15625}


def credit(h, **kwargs):
    return h.signed("POST", PATH, identity="ap", body={**LEGACY, FIELD: ATTEMPT}, **kwargs)


def test_signed_caller_id_reaches_audit_but_not_upstream(caplog):
    caplog.set_level(logging.INFO)
    h, store = configured()
    h.upstream.on("POST", PATH, ok(request_id="credit-receipt"))
    res = credit(h)
    assert res.status == 200
    assert store.intents == {ATTEMPT: (123, 15625)}
    assert store.finished == {ATTEMPT: ("forwarded", 200, "credit-receipt")}
    assert json.loads(h.upstream.calls("POST")[0].content) == LEGACY
    assert ATTEMPT in caplog.text


class DedupeControl(FakeControl):
    """Model v1 begin refusal, including a commit whose reply is lost."""

    async def begin(self, attempt, target, value):
        await asyncio.sleep(0)
        self.calls.append(("begin", attempt))
        if self.fail == "begin":
            raise ControlStoreError("unavailable")
        if attempt in self.intents:
            raise ControlStoreError("rejected")
        self.intents[attempt] = (target, value)
        if self.fail == "begin_reply":
            raise ControlStoreError("timeout")
        return len(self.intents)


def setup():
    h, _ = configured()
    store = DedupeControl()
    h.proxy.control = store
    h.upstream.on("POST", PATH, ok(request_id="one-credit"))
    return h, store


def assert_unknown(res):
    assert res.status == 503
    assert res.proxy_error == "attempt_unknown"
    assert res.json["reason"] == "begin_unconfirmed"


@pytest.mark.parametrize("change", [{}, {"value": 1}, {"id": 124}])
def test_duplicate_with_new_nonce_never_posts_or_finishes_again(change):
    h, store = setup()
    h.upstream.on("GET", "/api/user/124", ok({
        "id": 124, "username": "wsab0000000124", "role": 1, "group": "default",
    }))
    assert credit(h).status == 200
    original = dict(store.finished)
    res = h.signed("POST", PATH, identity="ap", body={**LEGACY, **change, FIELD: ATTEMPT})
    assert_unknown(res)
    assert len(h.upstream.calls("POST")) == 1
    assert store.finished == original
    assert len([c for c in store.calls if c[0] == "finish"]) == 1


@pytest.mark.parametrize("failure", ["begin", "begin_reply"])
def test_unavailable_or_ambiguous_begin_is_unknown_and_never_posts(failure):
    h, store = setup()
    store.fail = failure
    assert_unknown(credit(h))
    assert not h.upstream.calls("POST") and not store.finished
    if failure == "begin_reply":
        store.fail = None
        assert_unknown(credit(h))
        assert store.intents == {ATTEMPT: (123, 15625)}
        assert not h.upstream.calls("POST") and not store.finished


def test_disabled_control_store_is_unknown_without_any_upstream_call():
    h = Harness()
    assert_unknown(credit(h))
    assert not h.upstream.requests


@pytest.mark.parametrize("failure", ["acquire", "finish"])
def test_prebegin_failure_or_lost_not_sent_receipt_cannot_authorize_retry(failure):
    h, store = setup()
    store.fail = failure
    if failure == "finish":
        store.refuse_at = 2
    assert_unknown(credit(h))
    assert not h.upstream.calls("POST")
    assert not store.finished


def test_confirmed_fresh_not_sent_can_keep_legacy_budget_response():
    h, store = setup()
    store.refuse_at = 2
    res = credit(h)
    assert res.status == 429 and res.proxy_error == "budget"
    assert store.finished == {ATTEMPT: ("not_sent", None, None)}
    assert not h.upstream.calls("POST")
    assert_unknown(credit(h))
    assert not h.upstream.calls("POST")


def request(h, *, body=None, nonce=None):
    payload = json.dumps(body or {**LEGACY, FIELD: ATTEMPT}).encode()
    headers = h.sign_headers("POST", PATH, body=payload, identity="ap", nonce=nonce)
    return payload, headers


def test_two_replicas_same_id_during_first_post_have_one_durable_intent_and_post():
    async def scenario():
        first, store = setup()
        second, _ = setup()
        second.proxy.control = store
        started, release = asyncio.Event(), asyncio.Event()
        async def hold(req):
            started.set()
            await release.wait()
            return ok(request_id="once")
        first.upstream.on("POST", PATH, hold)
        body, headers = request(first)
        task = asyncio.create_task(first.acall("POST", PATH.encode(), headers=headers, body=body))
        try:
            await asyncio.wait_for(started.wait(), 1)
            body2, headers2 = request(second)
            assert_unknown(await second.acall("POST", PATH.encode(), headers=headers2, body=body2))
            assert not store.finished
        finally:
            release.set()
        assert (await task).status == 200
        assert len(first.upstream.calls("POST")) + len(second.upstream.calls("POST")) == 1
        assert len(store.intents) == 1
        assert len([c for c in store.calls if c[0] == "finish"]) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("loss", ["finish", "timeout", "cancel", "response"])
def test_lost_outcome_or_response_does_not_allow_second_post(loss):
    h, store = setup()
    if loss == "finish":
        store.fail = "finish"
    elif loss in ("timeout", "cancel"):
        def fail(req):
            if loss == "cancel":
                raise asyncio.CancelledError()
            raise httpx.ReadTimeout("synthetic", request=req)
        h.upstream.on("POST", PATH, fail)
    if loss == "response":
        class LostResponse(Exception):
            pass
        async def lose_reply(scope, receive, send):
            async def lost(message):
                raise LostResponse()
            await h.app(scope, receive, lost)
        payload, headers = request(h)
        with pytest.raises(LostResponse):
            asyncio.run(asgi_call(lose_reply, "POST", PATH.encode(), b"", headers, payload))
    elif loss == "cancel":
        with pytest.raises(asyncio.CancelledError):
            credit(h)
    else:
        res = credit(h)
        assert res.status == (504 if loss == "timeout" else 200)
    original = dict(store.finished)
    store.fail = None
    assert_unknown(credit(h))
    assert store.finished == original
    assert len(h.upstream.calls("POST")) == 1


@pytest.mark.parametrize("value", [None, True, 3, [], {}, ATTEMPT.upper(),
                                     "8a2e668b-2756-179e-afcc-2aa19b9b25f5", ATTEMPT + "\n"])
def test_invalid_metadata_never_enters_audit_or_store(value, caplog):
    caplog.set_level(logging.INFO)
    h, store = setup()
    res = h.signed("POST", PATH, identity="ap", body={**LEGACY, FIELD: value})
    assert res.status == 403
    assert not store.calls and not h.upstream.requests
    assert ATTEMPT not in caplog.text


@pytest.mark.parametrize("key,identity,mode", [("web", "ap", "add"), ("cron", "bg", "subtract"),
                                               ("cron", "user", "add"), ("cron", "op", "add")])
def test_metadata_is_only_allowed_on_cron_ap_add(key, identity, mode):
    h, store = setup()
    res = h.signed("POST", PATH, key=key, identity=identity,
                   body={**LEGACY, "mode": mode, FIELD: ATTEMPT})
    assert res.status in (400, 403)
    assert not store.calls and not h.upstream.requests


def test_tampered_hmac_and_invalid_policy_do_not_adopt_caller_id(caplog):
    caplog.set_level(logging.INFO)
    h, store = setup()
    payload, headers = request(h)
    res = h.call("POST", PATH.encode(), headers=headers, body=payload.replace(b"15625", b"15626"))
    assert res.status == 401
    res = h.signed("POST", PATH, identity="ap", body={**LEGACY, FIELD: ATTEMPT, "extra": 1})
    assert res.status == 403
    assert not store.calls and not h.upstream.requests
    assert ATTEMPT not in caplog.text


def test_nonce_replay_does_not_start_another_attempt():
    h, store = setup()
    nonce = "a" * 22
    assert credit(h, nonce=nonce).status == 200
    assert_unknown(credit(h, nonce=nonce))
    assert len(h.upstream.calls("POST")) == 1
    assert len([c for c in store.calls if c[0] == "begin"]) == 1


def test_legacy_four_field_requests_keep_proxy_generated_ids_and_no_store_support():
    h, store = setup()
    assert h.signed("POST", PATH, identity="ap", body=LEGACY).status == 200
    assert ATTEMPT not in store.intents
    assert json.loads(h.upstream.calls("POST")[0].content) == LEGACY
    h.proxy.control = None
    assert h.signed("POST", PATH, identity="ap", body=LEGACY).status == 200


@pytest.mark.parametrize("blocked", ["inflight", "hourly_budget", "precheck_response", "replay_store"])
def test_prebegin_rejection_is_unknown_and_leaves_no_durable_not_sent(blocked):
    h, store = setup()
    if blocked == "inflight":
        h.proxy.inflight.limits["ap"] = 0
    elif blocked == "hourly_budget":
        h.proxy.budget.reserve(h.proxy.budget.cap, h.mono)
    elif blocked == "precheck_response":
        h.upstream.on("GET", "/api/user/123", httpx.Response(429))
    else:
        h.replay_store.fail = "unavailable"
    res = credit(h)
    assert_unknown(res)
    assert not store.intents and not store.finished and not h.upstream.calls("POST")


def test_unsigned_attempt_header_is_not_an_audit_identity():
    h, store = setup()
    assert h.signed("POST", PATH, identity="ap", body=LEGACY,
                    headers=[("X-Woow-Conversion-Attempt-Id", ATTEMPT)]).status == 200
    assert ATTEMPT not in store.intents


def test_other_routes_and_duplicate_json_cannot_adopt_caller_id(caplog):
    caplog.set_level(logging.INFO)
    h, store = setup()
    assert h.signed("POST", "/api/user/", identity="ap", body={FIELD: ATTEMPT}).status == 403
    payload = json.dumps({**LEGACY, FIELD: ATTEMPT})[:-1] + ',"value":15625}'
    assert h.signed("POST", PATH, identity="ap", body=payload.encode()).status == 400
    assert not store.calls and not h.upstream.requests
    assert ATTEMPT not in caplog.text


@pytest.mark.parametrize("shared_replay", [False, True])
def test_lost_http_response_same_nonce_is_unknown_without_changing_audit(shared_replay):
    first, store = setup()
    nonce = "r" * 22
    payload, headers = request(first, nonce=nonce)

    class LostResponse(Exception):
        pass

    async def lose_reply(scope, receive, send):
        async def lost(message):
            raise LostResponse()
        await first.app(scope, receive, lost)

    with pytest.raises(LostResponse):
        asyncio.run(asgi_call(lose_reply, "POST", PATH.encode(), b"", headers, payload))
    assert len(first.upstream.calls("POST")) == 1
    original_intents, original_finished = dict(store.intents), dict(store.finished)
    original_calls = list(store.calls)
    assert original_finished == {ATTEMPT: ("forwarded", 200, "one-credit")}
    retry = first
    if shared_replay:
        retry, _ = setup()
        retry.proxy.control = store
        retry.proxy.shared_replay = first.replay_store
    assert_unknown(retry.call("POST", PATH.encode(), headers=headers, body=payload))
    assert store.intents == original_intents and store.finished == original_finished
    assert store.calls == original_calls
    assert len(first.upstream.calls("POST")) == 1
    if shared_replay:
        assert not retry.upstream.requests


def test_legacy_same_nonce_replay_remains_auth_error():
    h, store = setup()
    nonce = "l" * 22
    assert h.signed("POST", PATH, identity="ap", body=LEGACY, nonce=nonce).status == 200
    original = dict(store.finished)
    res = h.signed("POST", PATH, identity="ap", body=LEGACY, nonce=nonce)
    assert res.status == 401 and res.proxy_error == "auth"
    assert store.finished == original and len(h.upstream.calls("POST")) == 1


@pytest.mark.parametrize("failure", ["signature", "timestamp", "missing_sig"])
def test_other_auth_failures_with_metadata_are_not_hidden_as_unknown(failure):
    h, store = setup()
    nonce = "s" * 22
    assert credit(h, nonce=nonce).status == 200
    payload, headers = request(h, nonce=nonce)
    if failure == "signature":
        payload = payload.replace(b"15625", b"15626")
    elif failure == "timestamp":
        headers = h.sign_headers("POST", PATH, body=payload, identity="ap", nonce=nonce, ts=h.now - 100)
    else:
        headers = [(k, v) for k, v in headers if k.lower() != "x-woow-proxy-sig"]
    res = h.call("POST", PATH.encode(), headers=headers, body=payload)
    assert res.status == 401 and res.proxy_error == "auth"
    assert len(h.upstream.calls("POST")) == 1
    assert len([c for c in store.calls if c[0] == "begin"]) == 1
