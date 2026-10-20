"""Body policies (DESIGN §1.4.2-5, §3.3): only the fields PaaS sends, exact types."""

import json

import pytest

from newapi_proxy.policy import UP_MAX_LIFETIME
from proxy_harness import NOW, PASSWORD, PROOF, USER_JWT, USERNAME, Harness


@pytest.fixture
def h():
    return Harness()


def sent_json(h):
    assert len(h.upstream.requests) == 1
    return json.loads(h.upstream.requests[0].content)


# --- POST /api/user/ ----------------------------------------------------------

USER_CREATE = {"username": USERNAME, "password": PASSWORD, "display_name": "PaaS 42", "role": 1}


def test_user_create_happy_path_is_rebuilt(h):
    res = h.signed("POST", "/api/user/", identity="ap", body=dict(reversed(list(USER_CREATE.items()))))
    assert res.status == 200
    assert sent_json(h) == USER_CREATE
    assert h.upstream.requests[0].headers["content-type"] == "application/json"


@pytest.mark.parametrize(
    "patch",
    [
        {"group": "default"},
        {"quota": 1},
        {"admin_permissions": {"channel.read": True}},
        {"status": 1},
        {"Role": 10},  # case variant: new-api decodes JSON case-insensitively
        {"ROLE": 1},
        {"username": "wp0000000042"},  # prod prefix on the stg proxy
        {"username": "root"},
        {"username": "wsab000000042"},  # 9 digits
        {"username": "wsab0000000042\n"},
        {"username": "wsab" + "٠" * 10},  # Arabic-Indic digits
        {"role": 10},
        {"role": 100},
        {"role": True},
        {"role": 1.0},
        {"role": "1"},
        {"display_name": "Admin"},
        {"display_name": "PaaS 42 "},
        {"password": "short"},
        {"password": "x" * 129},
        {"password": "has space" + "x" * 30},
    ],
)
def test_user_create_negative(h, patch):
    body = {**USER_CREATE, **patch}
    res = h.signed("POST", "/api/user/", identity="ap", body=body)
    assert res.status == 403, (patch, res.body)
    assert res.proxy_error == "policy"
    assert h.upstream.requests == []


@pytest.mark.parametrize("missing", list(USER_CREATE))
def test_user_create_missing_field(h, missing):
    body = {k: v for k, v in USER_CREATE.items() if k != missing}
    assert h.signed("POST", "/api/user/", identity="ap", body=body).status == 403


@pytest.mark.parametrize(
    "raw, reason",
    [
        (b'{"username":"a","username":"b"}', "body_json"),  # duplicate key
        (b"[1,2]", "body_not_object"),
        (b'"str"', "body_not_object"),
        (b"{bad json", "body_json"),
        (b'{"role": NaN}', "body_json"),
        (b'{"role": Infinity}', "body_json"),
        (b"\xef\xbb\xbf{}", "body_json"),  # BOM
        (b"\xff\xfe{}", "body_json"),
        (b"", "body_json"),
        (b"[" * 100_000 + b"]" * 100_000, "body_json"),  # RecursionError, not a crash
    ],
    ids=["dup-key", "array", "string", "syntax", "nan", "infinity", "bom", "utf16", "empty", "deep-nesting"],
)
def test_malformed_json_is_400(h, raw, reason):
    res = h.signed("POST", "/api/user/", identity="ap", body=raw)
    assert res.status == 400
    assert res.json["reason"] == reason
    assert h.upstream.requests == []


def test_body_size_limit(h):
    big = json.dumps({**USER_CREATE, "pad": "x" * (256 * 1024)}).encode()
    res = h.signed("POST", "/api/user/", identity="ap", body=big)
    assert res.status == 413
    assert res.proxy_error == "too_large"


def test_body_on_get_is_400(h):
    res = h.signed("GET", "/api/user/5", identity="ap", body=b'{"x":1}')
    assert res.status == 400
    assert res.json["reason"] == "body_not_allowed"


# --- POST /api/token/ ---------------------------------------------------------

PAID = {
    "name": "wp_17_k7q2",
    "expired_time": NOW + 90 * 86400,
    "remain_quota": 50 * 15625,
    "unlimited_quota": False,
    "model_limits_enabled": True,
    "model_limits": "openai/gpt-5.1-mini,anthropic/claude-haiku-4.5",
    "allow_ips": "",
    "group": "default",
    "cross_group_retry": False,
    "allow_user_quota_query": False,
}
FREE = {
    **PAID,
    "name": "wp_18_ab12",
    "group": "free",
    "model_limits": "qwen3-4b-aicoder,qwen3-4b-cluster1",
    "remain_quota": 1,
}


@pytest.mark.parametrize("body", [PAID, FREE, {**PAID, "expired_time": -1, "unlimited_quota": True, "remain_quota": 0}])
def test_token_create_accepted(h, body):
    res = h.user("POST", "/api/token/", body=body, key="web")
    assert res.status == 200, res.body
    assert sent_json(h) == body


@pytest.mark.parametrize(
    "base, patch",
    [
        (PAID, {"group": "internal"}),
        (PAID, {"group": "auto"}),
        (PAID, {"group": ""}),
        (PAID, {"name": "my key"}),
        (PAID, {"name": "wp_17_K7Q2"}),
        (PAID, {"name": "wp__abcd"}),
        (PAID, {"name": "wp_17_k7q2x"}),
        (PAID, {"status": 1}),
        (PAID, {"Group": "internal"}),
        (PAID, {"allow_ips": "0.0.0.0/0"}),
        (PAID, {"cross_group_retry": True}),
        (PAID, {"allow_user_quota_query": True}),
        (PAID, {"unlimited_quota": 0}),
        (PAID, {"remain_quota": -1}),
        (PAID, {"remain_quota": 1.5}),
        (PAID, {"expired_time": 0}),
        (PAID, {"expired_time": "never"}),
        (PAID, {"model_limits_enabled": False}),  # limits given but disabled
        (PAID, {"model_limits": ""}),
        (PAID, {"model_limits": "a,,b"}),
        (FREE, {"model_limits": "qwen3-4b-aicoder,openai/gpt-5.1"}),
        (FREE, {"model_limits_enabled": False, "model_limits": ""}),
        (FREE, {"unlimited_quota": True}),
        (FREE, {"remain_quota": 100}),
    ],
)
def test_token_create_negative(h, base, patch):
    res = h.user("POST", "/api/token/", body={**base, **patch})
    assert res.status == 403, patch
    assert h.upstream.requests == []


@pytest.mark.parametrize("missing", ["allow_ips", "group", "allow_user_quota_query", "remain_quota"])
def test_token_create_missing_field(h, missing):
    body = {k: v for k, v in PAID.items() if k != missing}
    assert h.user("POST", "/api/token/", body=body).status == 403


# --- PUT /api/token/ (full update and status_only), POST /api/token/batch -------------

STATUS_ONLY = [("status_only", "true")]


@pytest.mark.parametrize("body", [{**PAID, "id": 9}, {**FREE, "id": 9}])
def test_token_full_update_is_the_create_rules_plus_id(h, body):
    res = h.user("PUT", "/api/token/", body=dict(reversed(list(body.items()))), key="web")
    assert res.status == 200, res.body
    assert sent_json(h) == body


@pytest.mark.parametrize(
    "patch",
    [
        {},  # no id
        {"id": 0},
        {"id": "9"},
        {"id": 9, "status": 1},  # a full update never carries status
        {"id": 9, "group": "internal"},
        {"id": 9, "allow_ips": "0.0.0.0/0"},
        {"id": 9, "name": "renamed"},
        {"id": 9, "cross_group_retry": True},
        {"id": 9, "user_id": 1},
    ],
)
def test_token_full_update_negative(h, patch):
    res = h.user("PUT", "/api/token/", body={**PAID, **patch})
    assert res.status == 403, patch
    assert h.upstream.requests == []


def test_token_full_update_keeps_the_free_fuse(h):
    res = h.user("PUT", "/api/token/", body={**FREE, "id": 9, "remain_quota": 100})
    assert res.status == 403 and res.json["reason"] == "token_free_quota"
    res = h.user("PUT", "/api/token/", body={**FREE, "id": 9, "model_limits": "openai/gpt-5.1"})
    assert res.status == 403 and res.json["reason"] == "token_free_models"


@pytest.mark.parametrize("status", [1, 2])
def test_token_status_only(h, status):
    res = h.user("PUT", "/api/token/", query_pairs=STATUS_ONLY, body={"status": status, "id": 9}, key="web")
    assert res.status == 200, res.body
    assert sent_json(h) == {"id": 9, "status": status}
    assert h.upstream.requests[0].url.query == b"status_only=true"


@pytest.mark.parametrize(
    "body",
    [
        {"id": 9, "status": 3},  # expired / exhausted are new-api's own states
        {"id": 9, "status": 4},
        {"id": 9, "status": "1"},
        {"id": 9, "status": True},
        {"id": 9},
        {"id": 9, "status": 1, "name": "wp_17_k7q2"},  # extra field
        {**PAID, "id": 9, "status": 1},  # a full body under status_only
    ],
)
def test_token_status_only_negative(h, body):
    res = h.user("PUT", "/api/token/", query_pairs=STATUS_ONLY, body=body)
    assert res.status == 403, body
    assert h.upstream.requests == []


@pytest.mark.parametrize("value", ["1", "false", "TRUE", "yes"])
def test_token_status_only_flag_value(h, value):
    res = h.user("PUT", "/api/token/", query_pairs=[("status_only", value)], body={"id": 9, "status": 2})
    assert res.status == 400 and res.json["reason"] == "query_value"


def test_token_batch_delete(h):
    res = h.user("POST", "/api/token/batch", body={"ids": [3, 4, 5]}, key="web")
    assert res.status == 200
    assert sent_json(h) == {"ids": [3, 4, 5]}


@pytest.mark.parametrize(
    "body",
    [
        {"ids": []},
        {"ids": [3, 3]},
        {"ids": [0]},
        {"ids": ["3"]},
        {"ids": [True]},
        {"ids": [3.0]},
        {"ids": list(range(1, 102))},
        {"ids": 3},
        {"ids": [3], "user_id": 1},
        {"Ids": [3]},
    ],
)
def test_token_batch_delete_negative(h, body):
    res = h.user("POST", "/api/token/batch", body=body)
    assert res.status == 403, body
    assert h.upstream.requests == []


# --- login / verify / access_tokens (CRON only) -----------------------------------


def test_login_only_paas_accounts(h):
    assert h.user("POST", "/api/user/login", auth=None, body={"username": USERNAME, "password": PASSWORD}).status == 200
    for body in (
        {"username": "root", "password": PASSWORD},
        {"username": "wpsvc-stg", "password": PASSWORD},
        {"username": USERNAME, "password": PASSWORD, "password_encrypted": "x"},
        {"username": USERNAME},
        {"username": USERNAME, "password": "short"},
    ):
        res = h.user("POST", "/api/user/login", auth=None, body=body)
        assert res.status == 403, body


GENERATE = {
    "method": "password",
    "scope": "access_token.generate",
    "context": {
        "scopes": ["api_key:read", "api_key:reveal", "api_key:write", "profile:read", "usage:read"],
        "expires_at": NOW + 90 * 86400,
    },
    "password": PASSWORD,
}


def test_verify_generate_and_revoke(h):
    assert h.user("POST", "/api/verify", auth=USER_JWT, body=GENERATE).status == 200
    revoke = {"method": "password", "scope": "access_token.revoke", "context": {"token_id": 9}, "password": PASSWORD}
    assert h.user("POST", "/api/verify", auth=USER_JWT, body=revoke).status == 200


@pytest.mark.parametrize(
    "patch",
    [
        {"method": "2fa"},
        {"scope": "admin.user.manage"},
        {"scope": "admin.user.create"},
        {"scope": "account_security.update"},
        {"context": {"scopes": ["wallet:write"], "expires_at": NOW + 86400}},
        {"context": {"scopes": ["api_key:read", "api_key:read"], "expires_at": NOW + 86400}},
        {"context": {"scopes": [], "expires_at": NOW + 86400}},
        {"context": {"scopes": ["api_key:read"], "expires_at": 0}},  # never expires
        {"context": {"scopes": ["api_key:read"], "expires_at": NOW + 600}},
        {"context": {"scopes": ["api_key:read"], "expires_at": NOW + UP_MAX_LIFETIME + 1}},
        {"context": {"scopes": ["api_key:read"], "expires_at": NOW + 86400, "role": 10}},
        {"code": "123456"},
    ],
)
def test_verify_negative(h, patch):
    res = h.user("POST", "/api/verify", auth=USER_JWT, body={**GENERATE, **patch})
    assert res.status == 403, patch


PAT_CREATE = {
    "name": "woow-paas-up-42-202610161200",
    "scopes": ["api_key:read", "api_key:reveal", "api_key:write", "profile:read", "usage:read"],
    "expires_at": NOW + 90 * 86400,
}


def test_pat_create_requires_proof_and_forwards_it(h):
    res = h.user("POST", "/api/user/access_tokens", auth=USER_JWT, body=PAT_CREATE)
    assert res.status == 400
    assert res.json["reason"] == "proof_missing"
    res = h.user(
        "POST", "/api/user/access_tokens", auth=USER_JWT, body=PAT_CREATE, headers=[("X-Security-Proof", PROOF)]
    )
    assert res.status == 200
    assert h.upstream.requests[0].headers["x-security-proof"] == PROOF


@pytest.mark.parametrize(
    "patch",
    [
        {"name": "woow-paas-ap-prod-20261016"},
        {"name": "anything"},
        {"scopes": ["user:write"]},
        {"scopes": ["api_key:read", "option:read"]},
        {"expires_at": 0},
        {"expires_at": NOW + UP_MAX_LIFETIME + 1},
    ],
)
def test_pat_create_negative(h, patch):
    res = h.user(
        "POST",
        "/api/user/access_tokens",
        auth=USER_JWT,
        body={**PAT_CREATE, **patch},
        headers=[("X-Security-Proof", PROOF)],
    )
    assert res.status == 403, patch
    assert h.upstream.requests == []


@pytest.mark.parametrize("value", ["²", "1e3", "-1", " 12", "1" * 13])
def test_content_length_must_be_ascii_digits(h, value):
    headers = h.sign_headers("POST", "/api/user/", body=b"{}", identity="ap") + [("Content-Length", value)]
    res = h.call("POST", b"/api/user/", b"", headers, b"{}")
    assert res.status == 400
    assert res.json["reason"] == "content_length"
