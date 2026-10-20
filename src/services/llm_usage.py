"""Pure validation and DTO aggregation for new-api usage responses.

This module intentionally has no Odoo imports so the data contracts can be
tested directly and reused by the controller/cache layer.
"""

from datetime import date, datetime, time, timedelta, timezone
import json
import re
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
QUOTA_PER_POINT = 15625
MAX_FLOW_ROWS = 10000
MAX_LOG_ROWS = 50
MAX_OTHER_JSON = 16384
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)
_SAFE_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class UsageInvalid(ValueError):
    """An invalid range or upstream usage response."""

    def __init__(self, message="Invalid usage data", code="llm_invalid_range"):
        super().__init__(message)
        self.code = code


def _invalid(message="Invalid usage data"):
    return UsageInvalid(message)


def _nonnegative_int(value, field):
    if type(value) is not int or value < 0:
        raise _invalid("Invalid " + field)
    return value


def _today(now):
    if now is None:
        return datetime.now(TAIPEI).date()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise _invalid("Invalid current time")
    return now.astimezone(TAIPEI).date()


def _parse_iso_date(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise _invalid("Invalid " + field)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _invalid("Invalid " + field) from exc


def parse_range(range_name="month", start=None, end=None, now=None):
    """Return an inclusive Taipei date range, bounded to the 180-day window."""
    today = _today(now)
    if not isinstance(range_name, str):
        raise _invalid("Invalid range")

    if range_name == "today":
        start_date = end_date = today
    elif range_name == "7d":
        start_date, end_date = today - timedelta(days=6), today
    elif range_name == "30d":
        start_date, end_date = today - timedelta(days=29), today
    elif range_name == "month":
        start_date, end_date = today.replace(day=1), today
    elif range_name == "last_month":
        end_date = today.replace(day=1) - timedelta(days=1)
        start_date = end_date.replace(day=1)
    elif range_name == "custom":
        start_date = _parse_iso_date(start, "start date")
        end_date = _parse_iso_date(end, "end date")
    else:
        raise _invalid("Invalid range")

    if start_date > end_date:
        raise _invalid("Invalid date order")
    if end_date > today or start_date < today - timedelta(days=179):
        raise _invalid("Date range is outside the allowed window")
    if (end_date - start_date).days + 1 > 31:
        raise _invalid("Date range is longer than 31 days")
    return start_date, end_date


def day_bounds(day):
    """Return inclusive Unix-second bounds for a Taipei calendar day."""
    if type(day) is not date:
        raise _invalid("Invalid day")
    start = datetime.combine(day, time.min, tzinfo=TAIPEI)
    next_start = datetime.combine(day + timedelta(days=1), time.min, tzinfo=TAIPEI)
    return int(start.timestamp()), int(next_start.timestamp()) - 1


def _local_key(token_id, names_by_id):
    if token_id == 0:
        return None, None, None
    info = names_by_id.get(token_id) if isinstance(names_by_id, dict) else None
    if not isinstance(info, dict):
        return None, None, None
    local_id = info.get("id")
    if type(local_id) is not int or local_id <= 0:
        raise _invalid("Invalid local key mapping")
    name = info.get("name")
    kind = info.get("kind")
    if name is not None and (not isinstance(name, str) or len(name) > 256):
        raise _invalid("Invalid local key name")
    if kind is not None and (not isinstance(kind, str) or not _SAFE_KIND.fullmatch(kind)):
        raise _invalid("Invalid local key kind")
    return local_id, name, kind


def normalize_flow(rows, names_by_id, day):
    """Validate grouped rc.44 flow rows and project only safe local DTO fields."""
    if type(day) is not date or not isinstance(rows, list) or len(rows) > MAX_FLOW_ROWS:
        raise _invalid("Invalid flow data")
    day_iso = day.isoformat()
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise _invalid("Invalid flow row")
        token_id = row.get("token_id", 0)
        if type(token_id) is not int or token_id < 0:
            raise _invalid("Invalid token id")
        if not isinstance(row.get("use_group"), str) or not row["use_group"]:
            raise _invalid("Invalid flow group")
        model = row.get("model_name")
        if not isinstance(model, str) or not _SAFE_MODEL.fullmatch(model):
            raise _invalid("Invalid model")
        count = _nonnegative_int(row.get("count"), "request count")
        quota = _nonnegative_int(row.get("quota"), "quota")
        tokens = _nonnegative_int(row.get("token_used"), "token count")
        key_id, key_name, key_kind = _local_key(token_id, names_by_id)
        result.append({
            "day": day_iso,
            "key_id": key_id,
            "key_name": key_name,
            "key_kind": key_kind,
            "model": model,
            "requests": count,
            "quota": quota,
            "tokens": tokens,
        })
    return result


def _usage_totals(rows):
    if not isinstance(rows, list) or len(rows) > MAX_FLOW_ROWS:
        raise _invalid("Invalid usage rows")
    quota_total = request_total = token_total = 0
    for row in rows:
        if not isinstance(row, dict):
            raise _invalid("Invalid usage row")
        quota_total += _nonnegative_int(row.get("quota"), "quota")
        request_value = row.get("requests", row.get("count"))
        token_value = row.get("tokens", row.get("token_used"))
        request_total += _nonnegative_int(request_value, "request count")
        token_total += _nonnegative_int(token_value, "token count")
    return quota_total, request_total, token_total


def summarize(rows, balance_quota=0, recent_rows=None, free_today_count=None):
    """Aggregate exact quota before converting it to displayed point units."""
    if type(balance_quota) is not int:
        raise _invalid("Invalid balance quota")
    balance = max(0, balance_quota)
    quota, requests, tokens = _usage_totals(rows)
    recent_quota = None
    if recent_rows is not None:
        recent_quota, _, _ = _usage_totals(recent_rows)
    free_count = None if free_today_count is None else _nonnegative_int(
        free_today_count, "free request count"
    )
    average_7d = (recent_quota / 7) if recent_quota is not None else None
    days_left = (balance / average_7d) if average_7d and average_7d > 0 else None
    return {
        "quota": quota,
        "spend_points": quota / QUOTA_PER_POINT,
        "requests": requests,
        "tokens": tokens,
        "input_tokens": None,
        "output_tokens": None,
        "days_left": days_left,
        "free_today_count": free_count,
    }


def _validate_dates(start, end):
    if type(start) is not date or type(end) is not date or start > end:
        raise _invalid("Invalid chart dates")
    if (end - start).days + 1 > 31:
        raise _invalid("Chart range is longer than 31 days")


def _filter_key_id(value):
    if value is None:
        return None
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    raise _invalid("Invalid key filter")


def daily_series(rows, start, end, group_by="model", metric="spend", key_id=None, model=None):
    """Build zero-filled date series from local-normalized daily flow rows."""
    _validate_dates(start, end)
    if group_by not in {"model", "key"} or metric not in {"spend", "requests", "tokens"}:
        raise _invalid("Invalid chart grouping")
    local_filter = _filter_key_id(key_id)
    if model is not None and (not isinstance(model, str) or not _SAFE_MODEL.fullmatch(model)):
        raise _invalid("Invalid model filter")
    if not isinstance(rows, list) or len(rows) > MAX_FLOW_ROWS:
        raise _invalid("Invalid chart rows")

    dates = [(start + timedelta(days=offset)).isoformat()
             for offset in range((end - start).days + 1)]
    date_index = {value: index for index, value in enumerate(dates)}
    groups = {}
    for row in rows:
        if not isinstance(row, dict):
            raise _invalid("Invalid chart row")
        row_day = row.get("day")
        if not isinstance(row_day, str) or row_day not in date_index:
            continue
        row_key = row.get("key_id")
        if row_key is not None and (type(row_key) is not int or row_key <= 0):
            raise _invalid("Invalid local key id")
        row_model = row.get("model")
        if not isinstance(row_model, str) or not _SAFE_MODEL.fullmatch(row_model):
            raise _invalid("Invalid model")
        if local_filter is not None and row_key != local_filter:
            continue
        if model is not None and row_model != model:
            continue
        if group_by == "model":
            series_id, label = row_model, row_model
        elif row_key is None:
            series_id, label = "unknown", None
        else:
            series_id = str(row_key)
            label = row.get("key_name")
            if label is not None and (not isinstance(label, str) or len(label) > 256):
                raise _invalid("Invalid key label")
        if series_id not in groups:
            groups[series_id] = {
                "id": series_id,
                "label": label,
                "values": [0] * len(dates),
                "quota": [0] * len(dates),
            }
        series = groups[series_id]
        quota = _nonnegative_int(row.get("quota"), "quota")
        requests = _nonnegative_int(row.get("requests"), "request count")
        tokens = _nonnegative_int(row.get("tokens"), "token count")
        idx = date_index[row_day]
        series["quota"][idx] += quota
        if metric == "spend":
            series["values"][idx] = series["quota"][idx] / QUOTA_PER_POINT
        else:
            series["values"][idx] += requests if metric == "requests" else tokens

    return {
        "dates": dates,
        "series": list(groups.values()),
        "group_by": group_by,
        "metric": metric,
    }


_ERROR_CODES = {
    "invalid_request_error": "invalid_request",
    "bad_request": "invalid_request",
    "authentication_error": "authentication_error",
    "invalid_api_key": "authentication_error",
    "permission_error": "permission_error",
    "not_found_error": "not_found",
    "model_not_found": "not_found",
    "rate_limit_error": "rate_limit",
    "rate_limit_exceeded": "rate_limit",
    "insufficient_quota": "insufficient_quota",
    "context_length_exceeded": "context_length_exceeded",
    "content_policy_violation": "content_policy_violation",
    "timeout": "timeout",
    "server_error": "upstream_error",
}
_STATUS_ERROR_CODES = {
    400: "invalid_request",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found",
    408: "timeout",
    429: "rate_limit",
    500: "upstream_error",
    502: "upstream_error",
    503: "upstream_error",
    504: "timeout",
}


def _safe_other(value):
    if not isinstance(value, str) or len(value) > MAX_OTHER_JSON:
        return {}
    try:
        other = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return other if isinstance(other, dict) else {}


def _error_code(other):
    status = other.get("status_code")
    if type(status) is int and status in _STATUS_ERROR_CODES:
        return _STATUS_ERROR_CODES[status]
    for field in ("error_type", "error_code"):
        raw = other.get(field)
        if isinstance(raw, str):
            mapped = _ERROR_CODES.get(raw.lower())
            if mapped:
                return mapped
    return "upstream_error"


def normalize_logs(data, names_by_name):
    """Validate self-log rows and return a privacy-allowlisted DTO."""
    if not isinstance(data, dict):
        raise _invalid("Invalid log response")
    items = data.get("items")
    total = data.get("total")
    if not isinstance(items, list) or len(items) > MAX_LOG_ROWS:
        raise _invalid("Invalid log items")
    total = _nonnegative_int(total, "log total")
    if total < len(items):
        raise _invalid("Invalid log total")
    if not isinstance(names_by_name, dict):
        raise _invalid("Invalid local key mapping")

    result = []
    for row in items:
        if not isinstance(row, dict):
            raise _invalid("Invalid log row")
        log_type = row.get("type")
        if type(log_type) is not int or log_type not in {2, 5}:
            raise _invalid("Invalid log type")
        created_at = _nonnegative_int(row.get("created_at"), "log timestamp")
        try:
            time_tpe = datetime.fromtimestamp(created_at, timezone.utc).astimezone(TAIPEI)
        except (OverflowError, OSError, ValueError) as exc:
            raise _invalid("Invalid log timestamp") from exc
        model = row.get("model_name")
        if not isinstance(model, str) or not _SAFE_MODEL.fullmatch(model):
            if log_type == 5:
                # Failures before model selection have no valid model identifier.
                model = None
            else:
                raise _invalid("Invalid model")
        prompt_tokens = _nonnegative_int(row.get("prompt_tokens"), "input tokens")
        completion_tokens = _nonnegative_int(row.get("completion_tokens"), "output tokens")
        quota = _nonnegative_int(row.get("quota"), "quota")
        duration_seconds = _nonnegative_int(row.get("use_time"), "duration")
        if duration_seconds > (2**63 - 1) // 1000:
            raise _invalid("Invalid duration")
        raw_request_id = row.get("request_id")
        request_id = (raw_request_id if isinstance(raw_request_id, str)
                      and _SAFE_REQUEST_ID.fullmatch(raw_request_id) else None)

        token_name = row.get("token_name")
        if not isinstance(token_name, str):
            raise _invalid("Invalid token name")
        mapping = names_by_name.get(token_name)
        if isinstance(mapping, dict):
            key_id = mapping.get("id")
            key_name = mapping.get("name")
            if (type(key_id) is not int or key_id <= 0
                    or (key_name is not None and not isinstance(key_name, str))):
                raise _invalid("Invalid local key mapping")
        else:
            key_id = key_name = None

        other = _safe_other(row.get("other"))
        cache_tokens = other.get("cache_tokens", 0)
        if type(cache_tokens) is not int or cache_tokens < 0:
            cache_tokens = 0
        result.append({
            "time_tpe": time_tpe.isoformat(timespec="seconds"),
            "key_id": key_id,
            "key_name": key_name,
            "model": model,
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "cache_tokens": cache_tokens,
            "duration_ms": duration_seconds * 1000,
            "quota": quota,
            "spend_points": quota / QUOTA_PER_POINT,
            "request_id": request_id,
            "status": "success" if log_type == 2 else "error",
            "error_code": None if log_type == 2 else _error_code(other),
        })
    return {"items": result, "total": total}
