"""Per-hostname traffic from Cloudflare's GraphQL Analytics API (#1234).

Security Access routes are served by Cloudflare straight to the tenant's own
machine — traffic never passes through the platform, so the only place that
knows "did anyone hit this hostname, and what did they get" is Cloudflare's
zone analytics. This module asks it, per hostname, for:

* request count over the window, split by edge status class (2xx/3xx/4xx/5xx)
* the minute of the most recent request

## Failure is a value, not an exception

The API token needs **Zone → Analytics → Read** on the platform zone. As of
2026-09-29 the prod token does not have it (GraphQL answers ``code: authz``).
That is an operator-configuration state the UI should *explain*, not an
outage, so every failure is returned as ``{"available": False, "reason": ...}``
and never raised.

## Cost

One GraphQL request per call: one aliased ``totals`` group-by plus one
``limit: 1`` "latest minute" query per hostname. Results are cached in-process
for ``CACHE_TTL_SECONDS`` keyed by the hostname set + window.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
MAX_HOSTNAMES = 50
MAX_WINDOW_HOURS = 72
CACHE_TTL_SECONDS = 60.0
REQUEST_TIMEOUT = 20.0

_cache: dict = {}   # (frozenset(hostnames), window_hours) -> (monotonic_ts, result)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_query(hostnames: list[str]) -> str:
    """GraphQL document: one totals group-by + one latest-minute alias per host.

    Hostnames go in as variables (``$h0``…), never interpolated into the query.
    """
    var_decls = "".join(f", $h{i}: string" for i in range(len(hostnames)))
    latest = "\n".join(
        f"      last{i}: httpRequestsAdaptiveGroups(limit: 1, "
        f"filter: {{datetime_geq: $since, datetime_lt: $until, clientRequestHTTPHost: $h{i}}}, "
        f"orderBy: [datetimeMinute_DESC]) {{ dimensions {{ datetimeMinute }} }}"
        for i in range(len(hostnames))
    )
    return (
        f"query($zone: string, $since: Time, $until: Time, $hosts: [string!]{var_decls}) {{\n"
        f"  viewer {{ zones(filter: {{zoneTag: $zone}}) {{\n"
        f"      totals: httpRequestsAdaptiveGroups(limit: 5000, "
        f"filter: {{datetime_geq: $since, datetime_lt: $until, clientRequestHTTPHost_in: $hosts}}) "
        f"{{ count dimensions {{ clientRequestHTTPHost edgeResponseStatus }} }}\n"
        f"{latest}\n"
        f"  }} }}\n"
        f"}}"
    )


def parse_response(payload: dict, hostnames: list[str]) -> dict:
    """GraphQL JSON → ``{hostname: {requests, status_2xx.., last_request_at}}``."""
    zones = (((payload or {}).get("data") or {}).get("viewer") or {}).get("zones") or []
    zone = zones[0] if zones else {}
    hosts = {
        h: {"requests": 0, "status_2xx": 0, "status_3xx": 0,
            "status_4xx": 0, "status_5xx": 0, "last_request_at": None}
        for h in hostnames
    }
    for row in zone.get("totals") or []:
        dims = row.get("dimensions") or {}
        host = dims.get("clientRequestHTTPHost")
        if host not in hosts:
            continue
        count = int(row.get("count") or 0)
        hosts[host]["requests"] += count
        klass = int(dims.get("edgeResponseStatus") or 0) // 100
        if 2 <= klass <= 5:
            hosts[host][f"status_{klass}xx"] += count
    for i, host in enumerate(hostnames):
        rows = zone.get(f"last{i}") or []
        if rows:
            hosts[host]["last_request_at"] = (rows[0].get("dimensions") or {}).get("datetimeMinute")
    return hosts


def _unavailable(reason: str, detail: str = "") -> dict:
    return {"available": False, "reason": reason, "detail": detail, "hosts": {}}


async def get_hostname_traffic(hostnames: list[str], window_hours: int = 24) -> dict:
    """Traffic for ``hostnames`` over the last ``window_hours``. Never raises."""
    hostnames = sorted(set(hostnames))
    token, zone = settings.cloudflare_api_token, settings.cloudflare_zone_id
    if not settings.cloudflare_enabled or not token or not zone:
        return _unavailable("disabled", "Cloudflare integration or zone is not configured")
    if not hostnames:
        return {"available": True, "reason": "", "detail": "", "hosts": {},
                "window_hours": window_hours}

    key = (frozenset(hostnames), window_hours)
    cached = _cache.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]

    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=window_hours)
    variables = {"zone": zone, "since": _iso(since), "until": _iso(until), "hosts": hostnames}
    variables.update({f"h{i}": h for i, h in enumerate(hostnames)})
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                GRAPHQL_URL,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"query": build_query(hostnames), "variables": variables},
                timeout=REQUEST_TIMEOUT,
            )
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — a failed lookup is a value here
        logger.warning("CF analytics request failed: %s: %s", type(exc).__name__, exc)
        return _unavailable("error", type(exc).__name__)

    errors = payload.get("errors") or []
    if errors:
        codes = {((e.get("extensions") or {}).get("code") or "") for e in errors}
        message = "; ".join(str(e.get("message", "")) for e in errors)[:300]
        if "authz" in codes or "permission" in message.lower():
            logger.warning("CF analytics: token lacks Zone Analytics Read (%s)", message)
            return _unavailable("permission", "API token lacks Zone > Analytics > Read")
        logger.warning("CF analytics GraphQL error: %s", message)
        return _unavailable("error", message)
    if resp.status_code >= 400:
        return _unavailable("error", f"HTTP {resp.status_code}")

    result = {
        "available": True, "reason": "", "detail": "",
        "window_hours": window_hours, "since": variables["since"], "until": variables["until"],
        "hosts": parse_response(payload, hostnames),
    }
    _cache[key] = (time.monotonic(), result)
    return result
