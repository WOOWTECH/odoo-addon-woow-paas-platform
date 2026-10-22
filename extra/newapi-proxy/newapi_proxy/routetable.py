"""Route whitelist (DESIGN §1.4.2-5).

Every route names its method, path template, the identities that may use it,
whether a WEB key may call it, its allowed query keys (with value formats) and
its body policy. Anything not listed is 403 ``policy`` — in particular
``/v1/*``, ``/pg/*``, ``/api/option/*`` (except op's GET), ``/api/channel/*``,
``/api/redemption/*``, ``/api/user/topup*`` and ``/api/user/auth/*``.

LLM-01a shipped the routes the tracer needs; LLM-02b added ``GET /api/user/`` (ap) for
the balance refresh — the one route whose answer the proxy rebuilds
(``responses.user_list``: new-api lists every user, root and admins included, with
e-mail and settings). LLM-02 completes the table (§1.4.2-5): the remaining ap reads
(logs, usage data, pricing, verification methods), the user key-management and usage
routes, and the two break-glass (bg) routes — the group fallback ``PUT /api/user/`` and
the ``mode: subtract`` claw-back on ``POST /api/user/manage``.

One route is one (method, template); when several identities share it
(``/api/status``, ``/api/pricing``, ``/api/user/manage``) the body policy gets the
identity in its context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Mapping

from . import policy, responses

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
_SORT_BY_ID = re.compile(r"id", re.ASCII)
_SORT_ORDER = re.compile(r"asc|desc", re.ASCII)
_STATUS_ONLY = re.compile(r"true", re.ASCII)
_AUDIT_SEQ = re.compile(r"0|[1-9][0-9]{0,18}", re.ASCII)
_AUDIT_LIMIT = re.compile(r"[1-9]|[1-9][0-9]|100", re.ASCII)
#: ``GET /api/verify/methods?scope=…``: a verification scope such as ``access_token.generate``.
_VERIFY_SCOPE = re.compile(r"[a-z_]{1,32}\.[a-z_]{1,32}", re.ASCII)

PAGING = {"p": _P, "page_size": _PAGE_SIZE}
#: Log filters a user may use on their own logs (``/api/log/self``, ``/self/stat``).
LOG_FILTERS = {
    "type": _LOG_TYPE,
    "start_timestamp": _TS,
    "end_timestamp": _TS,
    "model_name": _MODEL,
    "token_name": policy.TOKEN_NAME_RE,
    "group": _GROUP,
}
LOG_SELF_QUERY = {**PAGING, **LOG_FILTERS, "request_id": _REQUEST_ID}
LOG_SELF_STAT_QUERY = dict(LOG_FILTERS)
#: The admin views add ``username`` — only ever a PaaS username of this environment.
#: ``channel`` (and ``upstream_request_id``) are never accepted: the channel is internal.
LOG_ADMIN_QUERY = {**LOG_SELF_QUERY, "username": USERNAME}
LOG_ADMIN_STAT_QUERY = {**LOG_SELF_STAT_QUERY, "username": USERNAME}
DATA_RANGE_QUERY = {"start_timestamp": _TS, "end_timestamp": _TS}
DATA_ADMIN_QUERY = {**DATA_RANGE_QUERY, "username": USERNAME}

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
    #: rebuild the upstream answer from an allow-list instead of relaying it
    #: (``responses``); ``None``: relayed as is.
    response: responses.ResponseFilter | None = None

    @property
    def segments(self) -> list[str]:
        return self.template.split("/")


def _r(name, method, template, identities, **kw) -> Route:
    return Route(name=name, method=method, template=template, identities=frozenset(identities), **kw)


ROUTES: tuple[Route, ...] = (
    # Local control-store read, never forwarded to new-api. "anon" means no
    # upstream PAT; every caller still needs a valid CRON HMAC and nonce. The
    # database role binds the proxy to one environment regardless of query.
    _r("control.audit", "GET", "/_control/audit", {"anon"},
       query={"after": _AUDIT_SEQ, "through": _AUDIT_SEQ, "limit": _AUDIT_LIMIT},
       special="control_read"),
    # Shared by several identities (DESIGN §1.4.2-5): anon is the readiness-style check,
    # ap / user read it with their own credential (cron only).
    _r("status", "GET", "/api/status", {"anon", "ap", "user"}),
    _r("pricing", "GET", "/api/pricing", {"ap", "user"}),
    # op: option:read PAT owned by root
    _r("op.options", "GET", "/api/option/", {"op"}),
    # ap: admin service account
    _r("ap.user.create", "POST", "/api/user/", {"ap"}, body="json", body_policy=policy.user_create),
    # LLM-02b: the 10-minute balance refresh reads every user page by page (DESIGN §1.3.6);
    # PaaS filters its own environment's usernames. Sorting only by id keeps pages stable.
    # new-api answers with every row of ``users`` (root, admins, e-mail, settings): the proxy
    # keeps only id / username / quota / used / count / status / DeletedAt of PaaS users.
    _r(
        "ap.user.list",
        "GET",
        "/api/user/",
        {"ap"},
        query={**PAGING, "sort_by": _SORT_BY_ID, "sort_order": _SORT_ORDER},
        response=responses.user_list,
    ),
    _r(
        "ap.user.search",
        "GET",
        "/api/user/search",
        {"ap"},
        query={"keyword": USERNAME, **PAGING},
    ),
    _r("ap.user.get", "GET", "/api/user/{id}", {"ap"}),
    # ap adds (add_quota, mode add); bg subtracts (the claw-back of DESIGN §4.2.4). The body
    # policy picks the rule by identity; both pre-check the target (``special``).
    _r(
        "ap.user.manage",
        "POST",
        "/api/user/manage",
        {"ap", "bg"},
        body="json",
        body_policy=policy.manage,
        special="manage",
        audit="add_quota",
    ),
    # Reconciliation, free-tier meter, margin report (LLM-10 / LLM-12b / LLM-14).
    _r("ap.log.list", "GET", "/api/log/", {"ap"}, query=LOG_ADMIN_QUERY),
    _r("ap.log.stat", "GET", "/api/log/stat", {"ap"}, query=LOG_ADMIN_STAT_QUERY),
    _r("ap.data", "GET", "/api/data/", {"ap"}, query=DATA_ADMIN_QUERY),
    _r("ap.data.users", "GET", "/api/data/users", {"ap"}, query=DATA_RANGE_QUERY),
    _r("ap.data.flow", "GET", "/api/data/flow", {"ap"}, query=DATA_ADMIN_QUERY),
    _r("ap.verify.methods", "GET", "/api/verify/methods", {"ap"}, query={"scope": _VERIFY_SCOPE}),
    # bg only: the free-tier fallback moves a PaaS user between default and free_blocked
    # (DESIGN §1.3.3). The proxy reads the user and rebuilds the full update itself.
    _r(
        "bg.user.group",
        "PUT",
        "/api/user/",
        {"bg"},
        body="json",
        body_policy=policy.bg_user_group,
        special="user_group",
        audit="bg",
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
    # Orphan / old UP revocation (〔PROV〕§3.2 P5, N21): needs the access_token.revoke proof.
    _r("user.pat.delete", "DELETE", "/api/user/access_tokens/{id}", {"user"}, proof=True),
    _r("user.session.delete", "DELETE", "/api/user/sessions/{sid}", {"user"}),
    _r("user.audit.self", "GET", "/api/audit/self", {"user"}, query=PAGING),
    # user: interactive routes a WEB key may call
    _r("user.self", "GET", "/api/user/self", {"user"}, web=True),
    _r("user.models", "GET", "/api/user/models", {"user"}, web=True),
    _r("user.groups", "GET", "/api/user/self/groups", {"user"}, web=True),
    _r(
        "user.token.create",
        "POST",
        "/api/token/",
        {"user"},
        web=True,
        body="json",
        body_policy=policy.token_create,
    ),
    _r(
        "user.token.update",
        "PUT",
        "/api/token/",
        {"user"},
        web=True,
        query={"status_only": _STATUS_ONLY},
        body="json",
        body_policy=policy.token_update,
    ),
    _r(
        "user.token.batch_delete",
        "POST",
        "/api/token/batch",
        {"user"},
        web=True,
        body="json",
        body_policy=policy.token_batch_delete,
    ),
    _r("user.token.list", "GET", "/api/token/", {"user"}, web=True, query=PAGING),
    _r("user.token.get", "GET", "/api/token/{id}", {"user"}, web=True),
    _r("user.token.delete", "DELETE", "/api/token/{id}", {"user"}, web=True),
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
    _r("user.log.self_stat", "GET", "/api/log/self/stat", {"user"}, web=True, query=LOG_SELF_STAT_QUERY),
    _r("user.data.self", "GET", "/api/data/self", {"user"}, web=True, query=DATA_RANGE_QUERY),
    _r("user.data.flow_self", "GET", "/api/data/flow/self", {"user"}, web=True, query=DATA_RANGE_QUERY),
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
