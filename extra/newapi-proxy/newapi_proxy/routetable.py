"""Route whitelist (DESIGN §1.4.2-5).

Every route names its method, path template, the identities that may use it,
whether a WEB key may call it, its allowed query keys (with value formats) and
its body policy. Anything not listed is 403 ``policy`` — in particular
``/v1/*``, ``/pg/*``, ``/api/option/*`` (except op's GET), ``/api/channel/*``,
``/api/redemption/*``, ``/api/user/topup*`` and ``/api/user/auth/*``.

LLM-01a ships the routes the tracer needs; LLM-02 completes the table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Mapping

from . import policy

#: Path parameter formats. ``{id}``: positive int32 without leading zeros.
PARAM_RE = {
    "id": re.compile(r"[1-9][0-9]{0,9}", re.ASCII),
    "sid": re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII),
}
INT32_MAX = 2_147_483_647

#: Marker: the value must match the configured PaaS username regex.
USERNAME = "username"

_P = re.compile(r"[1-9][0-9]{0,5}", re.ASCII)
_PAGE_SIZE = re.compile(r"[1-9]|[1-9][0-9]|100", re.ASCII)
_TS = re.compile(r"[0-9]{1,12}", re.ASCII)
_LOG_TYPE = re.compile(r"[0-9]{1,2}", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9._:/-]{1,128}", re.ASCII)
_GROUP = re.compile(r"default|free", re.ASCII)
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{1,64}", re.ASCII)

PAGING = {"p": _P, "page_size": _PAGE_SIZE}
LOG_SELF_QUERY = {
    **PAGING,
    "type": _LOG_TYPE,
    "start_timestamp": _TS,
    "end_timestamp": _TS,
    "model_name": _MODEL,
    "token_name": policy.TOKEN_NAME_RE,
    "group": _GROUP,
    "request_id": _REQUEST_ID,
}

BodyPolicy = Callable[[dict, policy.PolicyContext], dict]


@dataclass(frozen=True)
class Route:
    name: str
    method: str
    template: str
    identities: frozenset
    #: WEB keys may call it (only ever true for identity ``user``).
    web: bool = False
    query: Mapping[str, object] = field(default_factory=dict)
    #: "none": body must be empty; "json": a JSON object, see ``body_policy``.
    body: str = "none"
    body_policy: BodyPolicy | None = None
    #: identity user only: "required" or "forbidden" client Authorization.
    user_auth: str = "required"
    #: forward a client ``X-Security-Proof`` (then it is required).
    proof: bool = False
    #: "reveal" | "manage": extra pre-checks in the app.
    special: str | None = None
    #: write an audit line for every attempt.
    audit: str | None = None

    @property
    def segments(self) -> list[str]:
        return self.template.split("/")


def _r(name, method, template, identities, **kw) -> Route:
    return Route(name=name, method=method, template=template, identities=frozenset(identities), **kw)


ROUTES: tuple[Route, ...] = (
    # anon
    _r("anon.status", "GET", "/api/status", {"anon"}),
    # op: option:read PAT owned by root
    _r("op.options", "GET", "/api/option/", {"op"}),
    # ap: admin service account
    _r("ap.user.create", "POST", "/api/user/", {"ap"}, body="json", body_policy=policy.user_create),
    _r(
        "ap.user.search",
        "GET",
        "/api/user/search",
        {"ap"},
        query={"keyword": USERNAME, **PAGING},
    ),
    _r("ap.user.get", "GET", "/api/user/{id}", {"ap"}),
    _r(
        "ap.user.manage",
        "POST",
        "/api/user/manage",
        {"ap"},
        body="json",
        body_policy=policy.manage_add_quota,
        special="manage",
        audit="add_quota",
    ),
    # user: session and PAT management (CRON only: web=False)
    _r(
        "user.login",
        "POST",
        "/api/user/login",
        {"user"},
        body="json",
        body_policy=policy.login,
        user_auth="forbidden",
    ),
    _r("user.verify", "POST", "/api/verify", {"user"}, body="json", body_policy=policy.verify),
    _r("user.pat.list", "GET", "/api/user/access_tokens", {"user"}),
    _r(
        "user.pat.create",
        "POST",
        "/api/user/access_tokens",
        {"user"},
        body="json",
        body_policy=policy.access_token_create,
        proof=True,
    ),
    _r("user.session.delete", "DELETE", "/api/user/sessions/{sid}", {"user"}),
    # user: interactive routes a WEB key may call
    _r("user.self", "GET", "/api/user/self", {"user"}, web=True),
    _r(
        "user.token.create",
        "POST",
        "/api/token/",
        {"user"},
        web=True,
        body="json",
        body_policy=policy.token_create,
    ),
    _r("user.token.list", "GET", "/api/token/", {"user"}, web=True, query=PAGING),
    _r("user.token.get", "GET", "/api/token/{id}", {"user"}, web=True),
    _r(
        "user.token.reveal",
        "POST",
        "/api/token/{id}/key",
        {"user"},
        web=True,
        special="reveal",
        audit="reveal",
    ),
    _r("user.log.self", "GET", "/api/log/self", {"user"}, web=True, query=LOG_SELF_QUERY),
)


@dataclass(frozen=True)
class Match:
    route: Route
    params: Mapping[str, str]

    def upstream_path(self) -> str:
        """Rebuild the path from the template and the validated parameters."""
        out = []
        for segment in self.route.segments:
            if segment.startswith("{") and segment.endswith("}"):
                out.append(self.params[segment[1:-1]])
            else:
                out.append(segment)
        return "/".join(out)


def _match_template(route: Route, path_segments: list[str]) -> dict[str, str] | None:
    template = route.segments
    if len(template) != len(path_segments):
        return None
    params: dict[str, str] = {}
    for want, got in zip(template, path_segments, strict=True):
        if want.startswith("{") and want.endswith("}"):
            name = want[1:-1]
            if not PARAM_RE[name].fullmatch(got):
                return None
            if name == "id" and int(got) > INT32_MAX:
                return None
            params[name] = got
        elif want != got:
            return None
    return params


def match(method: str, path: str) -> Match | None:
    """Exact template match on a canonical path; literal routes win."""
    segments = path.split("/")
    best: Match | None = None
    for route in ROUTES:
        if route.method != method:
            continue
        params = _match_template(route, segments)
        if params is None:
            continue
        if not params:
            return Match(route, params)
        best = best or Match(route, params)
    return best
