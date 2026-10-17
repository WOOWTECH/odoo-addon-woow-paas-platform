"""Response filters (DESIGN §1.4.2-6; LLM-02b review round 2).

Most upstream answers are relayed as they are: the route itself already limits
what new-api can answer with. ``GET /api/user/`` is the exception. rc.44 lists
every row of the ``users`` table (``model/user.go`` ``GetAllUsers`` uses
``Unscoped()`` and only omits ``password`` and ``access_token``) and, unlike
``GET /api/user/{id}``, it never checks ``canManageTargetRole``. Relayed as is,
a CRON key would read root's and every admin's e-mail, ``setting`` (webhook
secrets, Gotify tokens, Bark URLs), ``aff_code``, Stripe customer ids and OAuth
ids, and on stg every other sandbox's users as well: much more than the
balance refresh needs, and a way around the USERNAME rule of
``ap.user.search``.

So the proxy rebuilds that answer from an allow-list:

* ``200`` + ``success: true``: ``{success, message, data: {page, page_size,
  total, items}}``. Every item becomes ``{id, username, quota, used_quota,
  request_count, status, DeletedAt}`` — but only when ``username`` matches
  ``PROXY_USERNAME_REGEX`` and every one of those values has the expected type;
  any other user becomes ``{"id": <id or null>, "username": null}``. The number
  of items never changes: the balance refresh ends a round when a page is
  shorter than ``page_size``, so dropping rows would end it early.
* any other JSON object (``success: false``, an error status): only the fields
  the client classifies by (``success``, ``message``, ``message_key``,
  ``code``, ``error.type``).
* an empty body: relayed (there is nothing in it).
* anything else — not JSON, duplicate keys, ``data`` / ``items`` / an item of
  the wrong shape — fails closed: :class:`ResponseShapeError`, which the app
  turns into ``500 internal`` (the client's Unknown) without relaying a byte.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .settings import Settings


class ResponseShapeError(Exception):
    """The upstream answer is not the shape the filter knows; ``reason`` is a stable code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


ResponseFilter = Callable[[int, bytes, Settings], bytes]

_INT32_MAX = 2_147_483_647
_INT64_MAX = 2**63 - 1
#: gorm ``DeletedAt``: ``null`` or an RFC 3339 timestamp.
_TIMESTAMP_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]{8,18}(?:Z|[+-][0-9]{2}:[0-9]{2})", re.ASCII)
_ERROR_TYPE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}", re.ASCII)
_MESSAGE_MAX = 512
_CODE_MAX = 128


def _is_int(value: Any) -> bool:
    return type(value) is int  # bool is an int subclass: excluded on purpose


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _load_object(body: bytes) -> dict:
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_no_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ResponseShapeError("response_json") from None
    if not isinstance(value, dict):
        raise ResponseShapeError("response_not_object")
    return value


def _dump(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _message(value: Any) -> str:
    return value[:_MESSAGE_MAX] if type(value) is str else ""


def _error_fields(obj: dict) -> dict:
    """What ``newapi_client.classify_response`` reads from an answer that is not a list."""
    out: dict[str, Any] = {}
    if type(obj.get("success")) is bool:
        out["success"] = obj["success"]
    if "message" in obj:
        out["message"] = _message(obj.get("message"))
    # ``message_key`` is new-api's English source sentence; the client matches it as is.
    message_key = obj.get("message_key")
    if type(message_key) is str and message_key:
        out["message_key"] = message_key[:_MESSAGE_MAX]
    code = obj.get("code")
    if code is not None:
        # A code the client cannot read is still "a definite refusal" (it maps it to UNRECOGNISED).
        out["code"] = code[:_CODE_MAX] if type(code) is str and code else "UNRECOGNISED"
    error = obj.get("error")
    if isinstance(error, dict) and type(error.get("type")) is str and _ERROR_TYPE_RE.fullmatch(error["type"]):
        out["error"] = {"type": error["type"]}
    return out


def _user_item(item: Any, settings: Settings) -> dict:
    if not isinstance(item, dict):
        raise ResponseShapeError("response_item")
    user_id = item.get("id")
    safe_id = user_id if _is_int(user_id) and 0 < user_id <= _INT32_MAX else None
    masked = {"id": safe_id, "username": None}
    username = item.get("username")
    if type(username) is not str or not settings.username_re.fullmatch(username) or safe_id is None:
        return masked
    numbers = {}
    for name in ("quota", "used_quota", "request_count", "status"):
        value = item.get(name)
        if not _is_int(value) or abs(value) > _INT64_MAX:
            return masked
        numbers[name] = value
    deleted = item.get("DeletedAt")
    if deleted is not None and not (type(deleted) is str and _TIMESTAMP_RE.fullmatch(deleted)):
        return masked
    return {"id": safe_id, "username": username, **numbers, "DeletedAt": deleted}


def user_list(status: int, body: bytes, settings: Settings) -> bytes:
    """``ap.user.list``: see the module docstring."""
    if not body.strip():
        return body
    obj = _load_object(body)
    if not (status == 200 and obj.get("success") is True):
        return _dump(_error_fields(obj))
    data = obj.get("data")
    if not isinstance(data, dict):
        raise ResponseShapeError("response_data")
    items = data.get("items")
    if not isinstance(items, list):
        raise ResponseShapeError("response_items")
    out_data: dict[str, Any] = {}
    for name in ("page", "page_size", "total"):
        value = data.get(name)
        if value is None:
            continue
        if not _is_int(value) or not 0 <= value <= _INT64_MAX:
            raise ResponseShapeError("response_paging")
        out_data[name] = value
    out_data["items"] = [_user_item(item, settings) for item in items]
    out = {"success": True, "message": _message(obj.get("message")), "data": out_data}
    code = obj.get("code")
    if code is not None:
        # The client checks ``code`` before ``success``: keep the classification.
        out["code"] = code[:_CODE_MAX] if type(code) is str and code else "UNRECOGNISED"
    return _dump(out)
