"""Environment configuration, validated once at start-up.

Every value is stripped and checked against a strict format. A malformed value
is a ``SettingsError`` whose message names the variable and the problem but
never the value (DESIGN §1.4.2-1, 〔REV〕3): the caller exits non-zero, so a
bad rollout never becomes Ready and the old replicas keep serving.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Mapping

#: D5: 1 point = 15,625 quota, fixed forever (R = 32).
QUOTA_PER_POINT = 15625

#: 198.18.0.0/15 (RFC 2544). 198.18.0.0/24 is reserved for system identities.
SYNTHETIC_NET = ipaddress.IPv4Network("198.18.0.0/15")
TENANT_FIRST = ipaddress.IPv4Address("198.18.1.0")
TENANT_LAST = ipaddress.IPv4Address("198.19.255.255")

#: Fixed source address per system identity (DESIGN §1.4.2-2/-4). ``bg`` gets
#: its own address so break-glass calls stand out in new-api's audit log.
SYSTEM_IPS = {
    "ap": "198.18.0.1",
    "anon": "198.18.0.2",
    "op": "198.18.0.3",
    "bg": "198.18.0.4",
}

#: Source address of the proxy's own readiness check (/readyz → /api/status).
#: Not an identity: it keeps readiness out of anon's GA rate-limit bucket.
READINESS_IP = "198.18.0.5"

IDENTITIES = ("ap", "op", "user", "anon", "bg")

#: Break-glass may be opened for at most 24 hours (DESIGN §7.3).
BREAK_GLASS_MAX_SECONDS = 24 * 3600

_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{32,256}", re.ASCII)
_PAT_RE = re.compile(r"nap_[A-Za-z0-9]{32,128}", re.ASCII)
_UPSTREAM_RE = re.compile(r"http://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?", re.ASCII)
_INT_RE = re.compile(r"[0-9]{1,12}", re.ASCII)
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,128}", re.ASCII)
_LOG_LEVELS = ("debug", "info", "warning", "error")
#: PaaS usernames: a literal "w" + lowercase letter prefix, then only
#: lowercase letters / digits, simple classes such as [a-z], \\d and counted
#: repeats {n} / {n,m} — no ".", "*", "+", "?", "|", groups or other escapes,
#: so the pattern can never widen to unrelated accounts ("^.+$",
#: "^wp\\d{10}$|^root$"). The chart applies the same rule (proxy.yaml).
_USERNAME_RE_SHAPE = re.compile(r"\^w[a-z](?:[a-z0-9]|\[[a-z0-9-]+\]|\\d|\{[0-9]{1,3}(?:,[0-9]{1,3})?\})*\$", re.ASCII)
_VALKEY_ADDR_RE = re.compile(r"([a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?):([0-9]{1,5})", re.ASCII)
_VALKEY_USER_RE = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_VALKEY_PASSWORD_RE = re.compile(r"[A-Za-z0-9_-]{16,256}", re.ASCII)


class SettingsError(ValueError):
    """Invalid configuration. The message never contains the offending value."""

    def __init__(self, name: str, problem: str):
        super().__init__(f"{name}: {problem}")
        self.name = name
        self.problem = problem


@dataclass(frozen=True)
class Settings:
    upstream_url: str
    web_keys: Mapping[str, bytes]
    cron_keys: Mapping[str, bytes]
    ap_token: str | None
    op_token: str | None
    username_re: re.Pattern
    tenant_ip_first: int
    tenant_ip_last: int
    add_quota_hourly_cap: int  # quota units, this replica's share
    inflight: Mapping[str, int]
    break_glass_until: int | None
    ap_expires_at: int | None
    op_expires_at: int | None
    free_models: frozenset
    #: Shared replay record (Valkey ACL user); None = this replica only,
    #: allowed only with PROXY_REPLICAS=1.
    valkey_host: str | None = None
    valkey_port: int = 6379
    valkey_user: str = "newapi-proxy"
    valkey_password: str | None = field(default=None, repr=False)
    control_dsn: str | None = field(default=None, repr=False)
    control_environment: str | None = None
    replicas: int = 1
    default_read_timeout: float = 10.0
    connect_timeout: float = 2.0
    max_body_bytes: int = 256 * 1024
    #: Every route answers far less (a 100-item log page is ~100 KB).
    max_response_bytes: int = 1024 * 1024
    replay_cache_max: int = 200_000
    port: int = 8080
    #: /healthz, /readyz, /metrics live on their own listener.
    admin_port: int = 8081
    #: uvicorn limit_concurrency for the proxy port (connections + tasks).
    max_connections: int = 128
    log_level: str = "info"
    #: Literal secrets the log redactor masks wherever they might appear.
    secrets: frozenset = field(default=frozenset(), repr=False)

    def __repr__(self) -> str:  # never print key material
        return "Settings(<redacted>)"

    def key_for(self, kid: str) -> tuple[str, bytes] | None:
        """Return ``(key_class, secret)`` for a kid, or None if unknown."""
        if kid in self.web_keys:
            return "web", self.web_keys[kid]
        if kid in self.cron_keys:
            return "cron", self.cron_keys[kid]
        return None


def _get(env: Mapping[str, str], name: str, default: str = "") -> str:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip()


def parse_key_list(name: str, raw: str) -> dict[str, bytes]:
    """Parse ``kid:secret[,kid:secret]``. First entry is the current key."""
    keys: dict[str, bytes] = {}
    if raw == "":
        return keys
    for index, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        kid, sep, secret = entry.partition(":")
        if not sep:
            raise SettingsError(name, f"entry {index} is not kid:secret")
        if not _KID_RE.fullmatch(kid):
            raise SettingsError(name, f"entry {index} kid must be 1-64 of [A-Za-z0-9_-]")
        if not _SECRET_RE.fullmatch(secret):
            raise SettingsError(name, f"entry {index} secret must be 32-256 of [A-Za-z0-9_-]")
        if kid in keys:
            raise SettingsError(name, f"entry {index} repeats a kid")
        keys[kid] = secret.encode("ascii")
    return keys


def _parse_int(name: str, raw: str, lo: int, hi: int) -> int:
    if not _INT_RE.fullmatch(raw):
        raise SettingsError(name, "must be a decimal integer")
    value = int(raw)
    if not lo <= value <= hi:
        raise SettingsError(name, f"must be between {lo} and {hi}")
    return value


def _parse_epoch(name: str, raw: str) -> int | None:
    """Unix seconds or ``YYYY-MM-DD`` (00:00 UTC); empty means unknown."""
    if raw == "":
        return None
    if _INT_RE.fullmatch(raw):
        return int(raw)
    if _DATE_RE.fullmatch(raw):
        try:
            day = _dt.date.fromisoformat(raw)
        except ValueError:
            raise SettingsError(name, "is not a valid date") from None
        return int(_dt.datetime(day.year, day.month, day.day, tzinfo=_dt.timezone.utc).timestamp())
    raise SettingsError(name, "must be unix seconds or YYYY-MM-DD")


def _parse_ip_range(name: str, raw: str) -> tuple[int, int]:
    first_s, sep, last_s = raw.partition("-")
    if not sep:
        raise SettingsError(name, "must be <first>-<last>")
    try:
        first = ipaddress.IPv4Address(first_s.strip())
        last = ipaddress.IPv4Address(last_s.strip())
    except ValueError:
        raise SettingsError(name, "must hold two dotted IPv4 addresses") from None
    if not (TENANT_FIRST <= first <= last <= TENANT_LAST):
        raise SettingsError(name, "must lie inside 198.18.1.0-198.19.255.255 with first <= last")
    return int(first), int(last)


def _parse_username_re(name: str, raw: str) -> re.Pattern:
    if raw == "":
        raise SettingsError(name, "is required")
    if not (raw.startswith("^") and raw.endswith("$")):
        raise SettingsError(name, "must be anchored with ^ and $")
    try:
        pattern = re.compile(raw, re.ASCII)
    except re.error:
        raise SettingsError(name, "is not a valid regular expression") from None
    if pattern.fullmatch(""):
        raise SettingsError(name, "must not match the empty string")
    if not _USERNAME_RE_SHAPE.fullmatch(raw):
        raise SettingsError(
            name, "must be ^w<lowercase letter> then only [a-z0-9], [a-z0-9-] classes, \\d and {n,m}, ending in $"
        )
    return pattern


def _parse_valkey(env: Mapping[str, str], replicas: int) -> tuple[str | None, int, str, str | None]:
    addr = _get(env, "PROXY_VALKEY_ADDR")
    if addr == "":
        if replicas > 1:
            # A replay sent to another replica must be caught too.
            raise SettingsError("PROXY_VALKEY_ADDR", "is required when PROXY_REPLICAS > 1")
        return None, 6379, "newapi-proxy", None
    match = _VALKEY_ADDR_RE.fullmatch(addr)
    if not match:
        raise SettingsError("PROXY_VALKEY_ADDR", "must be <host>:<port>")
    port = int(match.group(3))
    if not 1 <= port <= 65535:
        raise SettingsError("PROXY_VALKEY_ADDR", "port must be 1-65535")
    user = _get(env, "PROXY_VALKEY_USER", "newapi-proxy")
    if not _VALKEY_USER_RE.fullmatch(user):
        raise SettingsError("PROXY_VALKEY_USER", "must be 1-64 of [A-Za-z0-9_-]")
    password = _get(env, "PROXY_VALKEY_PASSWORD")
    if not _VALKEY_PASSWORD_RE.fullmatch(password):
        raise SettingsError("PROXY_VALKEY_PASSWORD", "must be 16-256 of [A-Za-z0-9_-]")
    return match.group(1), port, user, password


def load_settings(env: Mapping[str, str], now: int) -> tuple[Settings, list[str]]:
    """Validate ``env``. Returns the settings and a list of warning codes."""
    warnings: list[str] = []

    upstream = _get(env, "PROXY_UPSTREAM_URL", "http://newapi:3000")
    if not _UPSTREAM_RE.fullmatch(upstream):
        raise SettingsError("PROXY_UPSTREAM_URL", "must be http://<host>[:<port>] without a path")

    web = parse_key_list("PROXY_AUTH_WEB", _get(env, "PROXY_AUTH_WEB"))
    cron = parse_key_list("PROXY_AUTH_CRON", _get(env, "PROXY_AUTH_CRON"))
    if not web and not cron:
        raise SettingsError("PROXY_AUTH_WEB/PROXY_AUTH_CRON", "no signing key configured")
    if set(web) & set(cron):
        raise SettingsError("PROXY_AUTH_WEB/PROXY_AUTH_CRON", "a kid appears in both lists")
    all_secrets = list(web.values()) + list(cron.values())
    if len(set(all_secrets)) != len(all_secrets):
        # A WEB holder must never be able to sign as CRON.
        raise SettingsError("PROXY_AUTH_WEB/PROXY_AUTH_CRON", "a secret is used more than once")

    tokens = {}
    for name in ("PROXY_AP_TOKEN", "PROXY_OP_TOKEN"):
        raw = _get(env, name)
        if raw and not _PAT_RE.fullmatch(raw):
            raise SettingsError(name, "must be a new-api PAT (nap_ followed by base62)")
        tokens[name] = raw or None
    if tokens["PROXY_AP_TOKEN"] and tokens["PROXY_AP_TOKEN"] == tokens["PROXY_OP_TOKEN"]:
        raise SettingsError("PROXY_AP_TOKEN/PROXY_OP_TOKEN", "must differ")

    username_re = _parse_username_re("PROXY_USERNAME_REGEX", _get(env, "PROXY_USERNAME_REGEX"))
    first, last = _parse_ip_range(
        "PROXY_TENANT_IP_RANGE", _get(env, "PROXY_TENANT_IP_RANGE", "198.18.1.0-198.19.255.255")
    )

    cap_points = _parse_int(
        "PROXY_ADD_QUOTA_HOURLY_CAP_POINTS",
        _get(env, "PROXY_ADD_QUOTA_HOURLY_CAP_POINTS", "100000"),
        0,
        100_000_000,
    )
    replicas = _parse_int("PROXY_REPLICAS", _get(env, "PROXY_REPLICAS", "1"), 1, 64)

    inflight = {}
    for ident, default in (("user", 32), ("ap", 8), ("op", 2), ("anon", 2), ("bg", 2)):
        var = f"PROXY_INFLIGHT_{ident.upper()}"
        inflight[ident] = _parse_int(var, _get(env, var, str(default)), 1, 1024)

    bg_raw = _get(env, "PROXY_BREAK_GLASS_UNTIL")
    break_glass_until = None
    if bg_raw:
        if not _INT_RE.fullmatch(bg_raw):
            raise SettingsError("PROXY_BREAK_GLASS_UNTIL", "must be unix seconds")
        value = int(bg_raw)
        if value > now + BREAK_GLASS_MAX_SECONDS:
            # Fail closed: an over-long window keeps break-glass off.
            warnings.append("break_glass_until_too_far")
        elif value > now:
            break_glass_until = value

    free_raw = _get(env, "PROXY_FREE_MODELS", "qwen3-4b-aicoder,qwen3-4b-cluster1")
    free_models = frozenset(m.strip() for m in free_raw.split(",") if m.strip())
    if not free_models or not all(_MODEL_RE.fullmatch(m) for m in free_models):
        raise SettingsError("PROXY_FREE_MODELS", "must be a comma list of model names")

    read_timeout = _parse_int("PROXY_DEFAULT_READ_TIMEOUT", _get(env, "PROXY_DEFAULT_READ_TIMEOUT", "10"), 1, 30)
    port = _parse_int("PROXY_PORT", _get(env, "PROXY_PORT", "8080"), 1, 65535)
    admin_port = _parse_int("PROXY_ADMIN_PORT", _get(env, "PROXY_ADMIN_PORT", "8081"), 1, 65535)
    if admin_port == port:
        raise SettingsError("PROXY_ADMIN_PORT", "must differ from PROXY_PORT")
    max_connections = _parse_int("PROXY_MAX_CONNECTIONS", _get(env, "PROXY_MAX_CONNECTIONS", "128"), 16, 4096)
    valkey_host, valkey_port, valkey_user, valkey_password = _parse_valkey(env, replicas)
    log_level = _get(env, "PROXY_LOG_LEVEL", "info").lower()
    if log_level not in _LOG_LEVELS:
        raise SettingsError("PROXY_LOG_LEVEL", "must be one of " + "/".join(_LOG_LEVELS))

    control_dsn = _get(env, "PROXY_CONTROL_DSN") or None
    control_environment = _get(env, "PROXY_CONTROL_ENVIRONMENT") or None
    if bool(control_dsn) != bool(control_environment):
        raise SettingsError("PROXY_CONTROL_DSN/PROXY_CONTROL_ENVIRONMENT", "must be configured together")
    if control_environment and not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", control_environment):
        raise SettingsError("PROXY_CONTROL_ENVIRONMENT", "must be a stable environment identifier")
    if control_dsn and (len(control_dsn) > 4096 or any(c in control_dsn for c in "\r\n\x00")):
        raise SettingsError("PROXY_CONTROL_DSN", "invalid connection configuration")
    secrets = frozenset(
        [s.decode("ascii") for s in all_secrets]
        + [t for t in tokens.values() if t]
        + ([valkey_password] if valkey_password else [])
        + ([control_dsn] if control_dsn else [])
    )
    settings = Settings(
        upstream_url=upstream,
        web_keys=web,
        cron_keys=cron,
        ap_token=tokens["PROXY_AP_TOKEN"],
        op_token=tokens["PROXY_OP_TOKEN"],
        username_re=username_re,
        tenant_ip_first=first,
        tenant_ip_last=last,
        add_quota_hourly_cap=cap_points * QUOTA_PER_POINT // replicas,
        inflight=inflight,
        break_glass_until=break_glass_until,
        ap_expires_at=_parse_epoch("PROXY_AP_EXPIRES_AT", _get(env, "PROXY_AP_EXPIRES_AT")),
        op_expires_at=_parse_epoch("PROXY_OP_EXPIRES_AT", _get(env, "PROXY_OP_EXPIRES_AT")),
        free_models=free_models,
        valkey_host=valkey_host,
        valkey_port=valkey_port,
        valkey_user=valkey_user,
        valkey_password=valkey_password,
        control_dsn=control_dsn,
        control_environment=control_environment,
        replicas=replicas,
        default_read_timeout=float(read_timeout),
        port=port,
        admin_port=admin_port,
        max_connections=max_connections,
        log_level=log_level,
        secrets=secrets,
    )
    return settings, warnings
