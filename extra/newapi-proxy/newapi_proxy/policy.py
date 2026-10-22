"""Body policies (DESIGN §1.4.2-5, §3.3; 〔REV〕1, 2).

Each policy takes the parsed JSON object and returns the object that is sent
upstream, rebuilt from validated fields only. Unknown, missing or mistyped
fields are refused (403 ``policy``): new-api decodes JSON case-insensitively
and ignores unknown keys, so "only these keys, exactly these types" is the
only rule that cannot be bypassed with ``"Mode"`` or ``"role": true``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .settings import QUOTA_PER_POINT, Settings

#: Proxy-only metadata, covered by the existing signed body hash.
CONVERSION_ATTEMPT_FIELD = "woow_conversion_attempt_id"
_CONVERSION_ATTEMPT_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.ASCII
)

#: `POST /api/user/manage` value range: 1 quota .. 10,000 points.
MANAGE_MAX_VALUE = 10_000 * QUOTA_PER_POINT  # 156,250,000

#: Scopes a per-user PAT (UP) may ever carry (DESIGN §1.3.1).
UP_SCOPES = frozenset({"api_key:read", "api_key:reveal", "api_key:write", "profile:read", "usage:read"})
#: UP lifetime bounds: new-api wants >= 1 h; PaaS issues 90 days (N21).
UP_MIN_LIFETIME = 3600 + 60
UP_MAX_LIFETIME = 92 * 86400

#: new-api caps remain_quota at 1e9 * QuotaPerUnit (500,000).
TOKEN_MAX_REMAIN_QUOTA = 1_000_000_000 * 500_000
TOKEN_MAX_EXPIRED_TIME = 4_102_444_800  # 2100-01-01

TOKEN_NAME_RE = re.compile(r"wp_[0-9]{1,12}_[a-z0-9]{4}", re.ASCII)
UP_NAME_RE = re.compile(r"woow-paas-up-[0-9]{1,10}-[0-9]{12}", re.ASCII)
DISPLAY_NAME_RE = re.compile(r"PaaS [0-9]{1,10}", re.ASCII)
PASSWORD_RE = re.compile(r"[\x21-\x7e]{32,128}", re.ASCII)
MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,128}", re.ASCII)

TOKEN_FIELDS = frozenset(
    {
        "name",
        "expired_time",
        "remain_quota",
        "unlimited_quota",
        "model_limits_enabled",
        "model_limits",
        "allow_ips",
        "group",
        "cross_group_retry",
        "allow_user_quota_query",
    }
)


class PolicyDenied(Exception):
    """403 policy. ``reason`` is a stable code safe to return and log."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PolicyContext:
    settings: Settings
    now: int
    #: The request's identity: one route can mean different things per identity
    #: (``POST /api/user/manage``: ap adds, bg subtracts).
    identity: str = ""
    #: The validated query pairs (``PUT /api/token/?status_only=true``).
    query: Mapping[str, str] = field(default_factory=dict)


def _is_int(value: Any) -> bool:
    return type(value) is int  # bool is an int subclass: excluded on purpose


def _is_bool(value: Any) -> bool:
    return type(value) is bool


def _is_str(value: Any) -> bool:
    return type(value) is str


def _exact_keys(body: dict, keys: frozenset | set, reason: str) -> None:
    if set(body) != set(keys):
        raise PolicyDenied(reason)


def _int_in(value: Any, lo: int, hi: int, reason: str) -> int:
    if not _is_int(value) or not lo <= value <= hi:
        raise PolicyDenied(reason)
    return value


def _up_scopes(value: Any, reason: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PolicyDenied(reason)
    if not all(_is_str(s) for s in value) or len(set(value)) != len(value):
        raise PolicyDenied(reason)
    if not set(value) <= UP_SCOPES:
        raise PolicyDenied(reason)
    return list(value)


def _up_expires_at(value: Any, now: int, reason: str) -> int:
    return _int_in(value, now + UP_MIN_LIFETIME, now + UP_MAX_LIFETIME, reason)


def _password(value: Any, reason: str) -> str:
    if not _is_str(value) or not PASSWORD_RE.fullmatch(value):
        raise PolicyDenied(reason)
    return value


def _username(value: Any, ctx: PolicyContext, reason: str) -> str:
    if not _is_str(value) or not ctx.settings.username_re.fullmatch(value):
        raise PolicyDenied(reason)
    return value


# --- ap ---------------------------------------------------------------------


def user_create(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/user/`: prefixed username, role 1, nothing else."""
    _exact_keys(body, {"username", "password", "display_name", "role"}, "user_create_fields")
    username = _username(body["username"], ctx, "user_create_username")
    password = _password(body["password"], "user_create_password")
    display = body["display_name"]
    if not _is_str(display) or not DISPLAY_NAME_RE.fullmatch(display):
        raise PolicyDenied("user_create_display_name")
    if not _is_int(body["role"]) or body["role"] != 1:
        raise PolicyDenied("user_create_role")
    return {"username": username, "password": password, "display_name": display, "role": 1}


def manage_add_quota(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/user/manage`: add_quota + mode add + bounded value, only."""
    keys = {"id", "action", "mode", "value"}
    if CONVERSION_ATTEMPT_FIELD in body:
        keys.add(CONVERSION_ATTEMPT_FIELD)
        attempt = body[CONVERSION_ATTEMPT_FIELD]
        if (ctx.identity != "ap" or type(attempt) is not str
                or not _CONVERSION_ATTEMPT_RE.fullmatch(attempt)):
            raise PolicyDenied("conversion_attempt")
    _exact_keys(body, keys, "manage_fields")
    target = _int_in(body["id"], 1, 2_147_483_647, "manage_id")
    if body["action"] != "add_quota":
        raise PolicyDenied("manage_action")
    if body["mode"] != "add":
        raise PolicyDenied("manage_mode")
    value = _int_in(body["value"], 1, MANAGE_MAX_VALUE, "manage_value")
    return {"id": target, "action": "add_quota", "mode": "add", "value": value}


def manage_subtract(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/user/manage` as bg: the claw-back after a voided order (DESIGN §4.2.4).

    ``mode: subtract`` only, same value bounds as add. ``override`` is never
    accepted from anyone.
    """
    _exact_keys(body, {"id", "action", "mode", "value"}, "manage_fields")
    target = _int_in(body["id"], 1, 2_147_483_647, "manage_id")
    if body["action"] != "add_quota":
        raise PolicyDenied("manage_action")
    if body["mode"] != "subtract":
        raise PolicyDenied("manage_mode")
    value = _int_in(body["value"], 1, MANAGE_MAX_VALUE, "manage_value")
    return {"id": target, "action": "add_quota", "mode": "subtract", "value": value}


def manage(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/user/manage`: ap may only add, bg may only subtract."""
    if ctx.identity == "bg":
        return manage_subtract(body, ctx)
    return manage_add_quota(body, ctx)


#: Groups a PaaS user may be in: ``free_blocked`` is the free-tier fallback of
#: DESIGN §1.3.3 that only bg sets.
PAAS_USER_GROUPS = frozenset({"default", "free_blocked"})


def manage_target_ok(
    user: Any, target_id: int, ctx: PolicyContext, groups: frozenset = frozenset({"default"})
) -> bool:
    """Pre-check of the manage target read with AP `GET /api/user/{id}`.

    add_quota (ap) only credits ``group == "default"`` users; the bg claw-back
    and the group change also accept ``free_blocked``.
    """
    if not isinstance(user, dict):
        return False
    return (
        user.get("id") == target_id
        and _is_int(user.get("id"))
        and _is_str(user.get("username"))
        and bool(ctx.settings.username_re.fullmatch(user["username"]))
        and _is_int(user.get("role"))
        and user["role"] == 1
        and _is_str(user.get("group"))
        and user["group"] in groups
    )


# --- bg (break-glass) ---------------------------------------------------------


def bg_user_group(body: dict, ctx: PolicyContext) -> dict:
    """`PUT /api/user/` as bg: the client names a user and a group, nothing else.

    new-api's update overwrites ``username``, ``display_name``, ``group`` and
    ``remark`` with whatever it is sent (``model/user.go`` ``EditWithTx``): the
    proxy reads the user first and sends those back with only ``group`` changed
    (:func:`bg_user_group_update`).
    """
    _exact_keys(body, {"id", "group"}, "bg_group_fields")
    target = _int_in(body["id"], 1, 2_147_483_647, "bg_group_id")
    if not _is_str(body["group"]) or body["group"] not in PAAS_USER_GROUPS:
        raise PolicyDenied("bg_group_value")
    return {"id": target, "group": body["group"]}


#: new-api's ``model.User`` limits (``validate:"max=…"``).
_USER_DISPLAY_NAME_MAX = 20
_USER_REMARK_MAX = 255


def bg_user_group_update(user: Any, validated: dict, ctx: PolicyContext) -> dict | None:
    """The full `PUT /api/user/` body for a group change, or None if the target is not ours.

    Built only from the user new-api just returned: same ``username``,
    ``display_name`` and ``remark``, ``role`` 1, no password and no
    ``admin_permissions`` (either would need an admin proof and is never sent).
    """
    if not manage_target_ok(user, validated["id"], ctx, groups=PAAS_USER_GROUPS):
        return None
    display = user.get("display_name")
    remark = user.get("remark")
    if remark is None:
        remark = ""
    if not (_is_str(display) and len(display) <= _USER_DISPLAY_NAME_MAX):
        return None
    if not (_is_str(remark) and len(remark) <= _USER_REMARK_MAX):
        return None
    return {
        "id": validated["id"],
        "username": user["username"],
        "display_name": display,
        "group": validated["group"],
        "remark": remark,
        "role": 1,
    }


# --- user (session / PAT management, CRON only) ------------------------------


def login(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/user/login`: only PaaS-prefixed accounts, plain password."""
    _exact_keys(body, {"username", "password"}, "login_fields")
    return {
        "username": _username(body["username"], ctx, "login_username"),
        "password": _password(body["password"], "login_password"),
    }


def verify(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/verify`: password step-up for UP generate / revoke only."""
    _exact_keys(body, {"method", "scope", "context", "password"}, "verify_fields")
    if body["method"] != "password":
        raise PolicyDenied("verify_method")
    scope = body["scope"]
    context = body["context"]
    if not isinstance(context, dict):
        raise PolicyDenied("verify_context")
    if scope == "access_token.generate":
        _exact_keys(context, {"scopes", "expires_at"}, "verify_context")
        new_context = {
            "scopes": _up_scopes(context["scopes"], "verify_scopes"),
            "expires_at": _up_expires_at(context["expires_at"], ctx.now, "verify_expires_at"),
        }
    elif scope == "access_token.revoke":
        _exact_keys(context, {"token_id"}, "verify_context")
        new_context = {"token_id": _int_in(context["token_id"], 1, 2_147_483_647, "verify_context")}
    else:
        raise PolicyDenied("verify_scope")
    return {
        "method": "password",
        "scope": scope,
        "context": new_context,
        "password": _password(body["password"], "verify_password"),
    }


def access_token_create(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/user/access_tokens`: a UP with UP scopes and bounded expiry."""
    _exact_keys(body, {"name", "scopes", "expires_at"}, "pat_fields")
    name = body["name"]
    if not _is_str(name) or not UP_NAME_RE.fullmatch(name):
        raise PolicyDenied("pat_name")
    return {
        "name": name,
        "scopes": _up_scopes(body["scopes"], "pat_scopes"),
        "expires_at": _up_expires_at(body["expires_at"], ctx.now, "pat_expires_at"),
    }


# --- user (API keys) ----------------------------------------------------------


def _model_list(value: Any, reason: str) -> list[str]:
    if not _is_str(value) or value == "":
        raise PolicyDenied(reason)
    models = value.split(",")
    if not all(MODEL_RE.fullmatch(m) for m in models):
        raise PolicyDenied(reason)
    return models


def token_create(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/token/`: exactly the §3.3 fields, wp_ names, default|free."""
    _exact_keys(body, TOKEN_FIELDS, "token_fields")
    return _token_fields(body, ctx)


#: `PUT /api/token/?status_only=true`: 1 enabled, 2 disabled (3 / 4 are new-api's own).
TOKEN_STATUSES = frozenset({1, 2})


def token_update(body: dict, ctx: PolicyContext) -> dict:
    """`PUT /api/token/`: a full update (the §3.3 fields plus ``id``) or a status change.

    ``?status_only=true`` takes ``{"id", "status"}`` only. Without it new-api
    overwrites every field it is sent (``controller/token.go`` ``UpdateToken``),
    so the same rules as creation apply — a free key stays a fuse.
    """
    if ctx.query.get("status_only") == "true":
        _exact_keys(body, {"id", "status"}, "token_status_fields")
        token_id = _int_in(body["id"], 1, 2_147_483_647, "token_id")
        status = body["status"]
        if not _is_int(status) or status not in TOKEN_STATUSES:
            raise PolicyDenied("token_status")
        return {"id": token_id, "status": status}
    _exact_keys(body, TOKEN_FIELDS | {"id"}, "token_fields")
    token_id = _int_in(body["id"], 1, 2_147_483_647, "token_id")
    return {"id": token_id, **_token_fields(body, ctx)}


#: `POST /api/token/batch` deletes at most this many keys at once (one account has at most 20).
TOKEN_BATCH_MAX = 100


def token_batch_delete(body: dict, ctx: PolicyContext) -> dict:
    """`POST /api/token/batch`: ``{"ids": [...]}``, distinct positive ids only."""
    _exact_keys(body, {"ids"}, "token_batch_fields")
    ids = body["ids"]
    if not isinstance(ids, list) or not 1 <= len(ids) <= TOKEN_BATCH_MAX:
        raise PolicyDenied("token_batch_ids")
    if not all(_is_int(i) and 1 <= i <= 2_147_483_647 for i in ids) or len(set(ids)) != len(ids):
        raise PolicyDenied("token_batch_ids")
    return {"ids": list(ids)}


def _token_fields(body: dict, ctx: PolicyContext) -> dict:
    """The §3.3 token fields, validated and rebuilt (creation and full update)."""
    name = body["name"]
    if not _is_str(name) or not TOKEN_NAME_RE.fullmatch(name):
        raise PolicyDenied("token_name")
    expired = body["expired_time"]
    if not _is_int(expired) or not (expired == -1 or 0 < expired <= TOKEN_MAX_EXPIRED_TIME):
        raise PolicyDenied("token_expired_time")
    remain = _int_in(body["remain_quota"], 0, TOKEN_MAX_REMAIN_QUOTA, "token_remain_quota")
    for key in ("unlimited_quota", "model_limits_enabled", "cross_group_retry", "allow_user_quota_query"):
        if not _is_bool(body[key]):
            raise PolicyDenied(f"token_{key}")
    if body["cross_group_retry"] or body["allow_user_quota_query"]:
        raise PolicyDenied("token_flags")
    if body["allow_ips"] != "":
        raise PolicyDenied("token_allow_ips")
    group = body["group"]
    if group not in ("default", "free"):
        raise PolicyDenied("token_group")
    limits_enabled = body["model_limits_enabled"]
    if limits_enabled:
        models = _model_list(body["model_limits"], "token_model_limits")
    elif body["model_limits"] != "":
        raise PolicyDenied("token_model_limits")
    else:
        models = []
    if group == "free":
        # The free key must stay a fuse (DESIGN §1.3.3): Qwen only, 1 quota.
        if not limits_enabled or not set(models) <= ctx.settings.free_models:
            raise PolicyDenied("token_free_models")
        if body["unlimited_quota"] or remain != 1:
            raise PolicyDenied("token_free_quota")
    return {
        "name": name,
        "expired_time": expired,
        "remain_quota": remain,
        "unlimited_quota": body["unlimited_quota"],
        "model_limits_enabled": limits_enabled,
        "model_limits": ",".join(models),
        "allow_ips": "",
        "group": group,
        "cross_group_retry": False,
        "allow_user_quota_query": False,
    }


#: A key may be revealed only this many seconds after new-api created it.
REVEAL_WINDOW_SECONDS = 120
#: Tolerated clock difference between the proxy and new-api.
REVEAL_CLOCK_SLACK = 5


def reveal_denial(token: Any, token_id: int, now: int) -> str | None:
    """Pre-check of `GET /api/token/{id}` before `POST /api/token/{id}/key`.

    Returns None when the reveal may go ahead, else the denial reason.
    """
    if not isinstance(token, dict) or not _is_int(token.get("id")) or token["id"] != token_id:
        return "reveal_token_mismatch"
    name = token.get("name")
    if not _is_str(name) or not TOKEN_NAME_RE.fullmatch(name):
        return "reveal_name"
    created = token.get("created_time")
    if not _is_int(created) or not now - REVEAL_WINDOW_SECONDS <= created <= now + REVEAL_CLOCK_SLACK:
        return "reveal_window"
    return None
