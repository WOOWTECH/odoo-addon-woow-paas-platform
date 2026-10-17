"""Encoding bypasses and query whitelists (〔REV〕17).

Every variant is *correctly signed* over the bytes sent, so the only thing
that can stop it is canonicalisation or the route table.
"""

import pytest

from newapi_proxy import canonical, signing
from proxy_harness import TENANT_IP, USER_PAT, Harness, ok


@pytest.fixture
def h():
    return Harness()


def send_raw_user(h, method, raw_path: bytes, raw_query: bytes = b""):
    """Sign over the raw path/query as-is (what a forging caller would do)."""
    path = raw_path.decode("latin-1")
    pairs = []
    if raw_query:
        for part in raw_query.decode("latin-1").split("&"):
            key, _, value = part.partition("=")
            pairs.append((key, value))
    try:
        headers = h.sign_headers(method, path, pairs, b"", "user", TENANT_IP)
    except UnicodeEncodeError:
        # Not signable as ASCII; the canonical check must reject it first anyway.
        headers = h.sign_headers(method, "/", [], b"", "user", TENANT_IP)
    headers.append(("Authorization", f"Bearer {USER_PAT}"))
    return h.call(method, raw_path, raw_query, headers)


@pytest.mark.parametrize(
    "raw_path, reason",
    [
        (b"/api/token/1%2F..%2F..%2Foption/key", "path_percent"),
        (b"/api/token/%2e%2e/key", "path_percent"),
        (b"/api/token/%2E%2E/key", "path_percent"),
        (b"/api/%74oken/1", "path_percent"),
        (b"/api/token/../option/", "path_dotdot"),
        (b"/api/token/..", "path_dotdot"),
        (b"/api//token/1", "path_double_slash"),
        (b"//api/token/1", "path_double_slash"),
        (b"/api/token/1;/key", "path_semicolon"),
        (b"/api/token/1;jsessionid=x", "path_semicolon"),
        (b"/api\\token\\1", "path_backslash"),
        (b"/api/token/\xc3\xa9", "path_non_ascii"),
        (b"/api/token/\xff", "path_non_ascii"),
        (b"/api/token/1/./key", "path_dot_segment"),
        (b"/api/token/1 /key", "path_control"),
        (b"/api/token/1\t", "path_control"),
        (b"/api/token/~1", "path_chars"),
        (b"/api/token/1@x", "path_chars"),
        (b"http://evil/api/status", "path_double_slash"),
        (b"api/status", "path_chars"),
    ],
)
def test_non_canonical_paths_rejected_400(h, raw_path, reason):
    res = send_raw_user(h, "GET", raw_path)
    assert res.status == 400, res.body
    assert res.proxy_error == "bad_request"
    assert res.json["reason"] == reason
    assert h.upstream.requests == []


def test_too_long_path_rejected(h):
    res = send_raw_user(h, "GET", b"/api/token/" + b"1" * 600)
    assert res.status == 400
    assert res.json["reason"] == "path_too_long"


@pytest.mark.parametrize(
    "raw_path",
    [
        b"/api/token/../key",  # {id} = ..  (caught as dotdot)
        b"/api/token/abc/key",  # {id} not numeric
        b"/api/token/0/key",  # {id} zero
        b"/api/token/007/key",  # leading zeros
        b"/api/token/-1/key",
        b"/api/token/1.0/key",
        b"/api/token/99999999999/key",  # > int32
        b"/api/token/2147483648/key",
        b"/api/token/1/key/",  # trailing slash
        b"/api/token/1/key/extra",
        b"/api/token",  # template is /api/token/
        b"/api/user/sessions/a.b",  # sid must be [A-Za-z0-9_-]
    ],
)
def test_bad_path_parameters_never_reach_upstream(h, raw_path):
    res = send_raw_user(h, "POST" if raw_path.endswith(b"key") else "GET", raw_path)
    assert res.status in (400, 403), res.body
    assert res.proxy_error in ("bad_request", "policy")
    assert h.upstream.requests == []


def test_int32_boundary_id_accepted(h):
    res = h.user("GET", "/api/token/2147483647")
    assert res.status == 200
    assert h.upstream.requests[0].url.raw_path == b"/api/token/2147483647"


@pytest.mark.parametrize(
    "raw_query, reason",
    [
        (b"p=1&p=2", "query_duplicate_key"),
        (b"p=%31", "query_percent"),
        (b"p=1+1", "query_plus"),
        (b"p=1;page_size=2", "query_semicolon"),
        (b"p", "query_pair"),
        (b"p=1=2", "query_pair"),
        (b"p=", "query_empty_value"),
        (b"&p=1", "query_pair"),
        (b"p=1&", "query_pair"),
        (b"P=1", "query_key_format"),
        (b"p=\xc3\xa9", "query_non_ascii"),
        (b"p=1&extra=1", "query_key"),
        (b"p=1&ps=100", "query_key"),  # new-api alias for page_size
        (b"page_size=101", "query_value"),
        (b"page_size=0", "query_value"),
        (b"p=0", "query_value"),
        (b"p=abc", "query_value"),
    ],
)
def test_query_rules(h, raw_query, reason):
    res = send_raw_user(h, "GET", b"/api/token/", raw_query)
    assert res.status == 400, res.body
    assert res.json["reason"] == reason
    assert h.upstream.requests == []


def test_query_is_forwarded_in_canonical_order(h):
    res = h.user("GET", "/api/token/", query_pairs=[("page_size", "10"), ("p", "1")])
    assert res.status == 200
    assert h.upstream.requests[0].url.query == b"p=1&page_size=10"


def test_signature_is_over_sorted_query_regardless_of_wire_order(h):
    headers = h.sign_headers("GET", "/api/token/", [("p", "1"), ("page_size", "10")], b"", "user", TENANT_IP)
    headers.append(("Authorization", f"Bearer {USER_PAT}"))
    res = h.call("GET", b"/api/token/", b"page_size=10&p=1", headers)
    assert res.status == 200
    assert h.upstream.requests[0].url.query == b"p=1&page_size=10"


def test_search_keyword_must_be_a_paas_username(h):
    res = h.signed("GET", "/api/user/search", [("keyword", "wsab0000000042")], identity="ap")
    assert res.status == 200
    for bad in ("root", "wsab000000004", "wpsvc-stg", "ws"):
        res = h.signed("GET", "/api/user/search", [("keyword", bad)], identity="ap")
        assert res.status == 400, bad


def test_user_list_query_values(h):
    """LLM-02b balance refresh: ``GET /api/user/`` takes paging and an id-only sort, nothing else."""
    good = [("p", "3"), ("page_size", "100"), ("sort_by", "id"), ("sort_order", "asc")]
    h.upstream.on("GET", "/api/user/", ok({"items": [], "page": 3, "page_size": 100, "total": 0}))
    res = h.signed("GET", "/api/user/", good, identity="ap")
    assert res.status == 200, res.body
    assert h.upstream.requests[0].url.query == b"p=3&page_size=100&sort_by=id&sort_order=asc"
    for bad in (
        [("sort_by", "quota")],
        [("sort_by", "id;drop")],
        [("sort_order", "ASC")],
        [("page_size", "101")],
        [("keyword", "wsab0000000042")],
        [("group", "default")],
        [("ps", "100")],
    ):
        res = h.signed("GET", "/api/user/", bad, identity="ap")
        assert res.status == 400, (bad, res.body)
    assert len(h.upstream.requests) == 1


def test_log_self_query_values(h):
    good = [
        ("type", "2"),
        ("start_timestamp", "1799990000"),
        ("end_timestamp", "1800000000"),
        ("token_name", "wp_12_ab3d"),
        ("model_name", "openai/gpt-5.1-mini"),
        ("group", "free"),
        ("request_id", "20261016-abc"),
        ("p", "2"),
        ("page_size", "50"),
    ]
    assert h.user("GET", "/api/log/self", query_pairs=good).status == 200
    for key, value in [("group", "vip"), ("token_name", "other"), ("model_name", "a" * 129), ("username", "x")]:
        res = h.user("GET", "/api/log/self", query_pairs=[(key, value)])
        assert res.status == 400, (key, value)


def test_canonical_query_helper_matches_proxy():
    pairs = canonical.parse_query(b"z=1&a=2&m=3")
    assert signing.canonical_query(pairs) == "a=2&m=3&z=1"
