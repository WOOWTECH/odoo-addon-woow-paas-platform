"""CRON-only, bounded audit reads stay local and fail closed."""

import pytest

from newapi_proxy.controlstore import ControlStoreError
from proxy_harness import Harness


class FakeControl:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def read(self, after, through, limit):
        self.calls.append((after, through, limit))
        if self.error:
            raise ControlStoreError(self.error)
        return {"env": "llmdev", "highwater": through if through is not None else 2,
                "events": [{"seq": 1, "kind": "intent", "attempt_id": "test-attempt"}]}


def test_audit_read_requires_control_store_and_never_calls_upstream():
    h = Harness(PROXY_AP_TOKEN="")
    missing = h.signed("GET", "/_control/audit", [("after", "0")])
    assert missing.status == 503 and missing.json["reason"] == "control_store_disabled"
    control = FakeControl()
    h.proxy.control = control
    result = h.signed("GET", "/_control/audit", [("after", "0"), ("through", "2"), ("limit", "1")])
    assert result.status == 200 and result.json["data"]["env"] == "llmdev"
    assert result.headers["cache-control"] == "no-store"
    assert control.calls == [(0, 2, 1)]
    assert h.upstream.requests == []


@pytest.mark.parametrize("identity,key,expected", [
    ("user", "cron", 403), ("ap", "cron", 403),
    ("op", "cron", 403), ("bg", "cron", 403),
    ("anon", "web", 403),
])
def test_audit_read_denies_other_identities_and_web_key(identity, key, expected):
    h = Harness()
    control = FakeControl()
    h.proxy.control = control
    result = h.signed("GET", "/_control/audit", [("after", "0")], identity=identity, key=key)
    assert result.status == expected
    assert not control.calls and not h.upstream.requests


@pytest.mark.parametrize("query", [
    [], [("through", "1")], [("after", "00")], [("after", "-1")],
    [("after", "9223372036854775808")], [("after", "1"), ("through", "0")],
    [("after", "0"), ("limit", "0")], [("after", "0"), ("limit", "101")],
    [("after", "0"), ("unknown", "1")],
])
def test_audit_read_invalid_cursors_never_touch_store(query):
    h = Harness()
    control = FakeControl()
    h.proxy.control = control
    result = h.signed("GET", "/_control/audit", query)
    assert result.status == 400
    assert not control.calls and not h.upstream.requests


def test_audit_read_store_failure_is_unavailable_not_empty():
    h = Harness()
    control = FakeControl("timeout")
    h.proxy.control = control
    result = h.signed("GET", "/_control/audit", [("after", "0")])
    assert result.status == 503 and result.json["reason"] == "control_store"
    assert control.calls == [(0, None, 100)]
    assert not h.upstream.requests
