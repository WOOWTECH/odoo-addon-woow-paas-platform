"""Minimal Prometheus text-format counters and gauges (no extra dependency)."""

from __future__ import annotations

import threading
from typing import Callable

_HELP = {
    "newapi_proxy_requests_total": "Requests by identity, route and status class.",
    "newapi_proxy_rejections_total": "Requests the proxy refused, by error class and reason.",
    "newapi_proxy_upstream_errors_total": "Upstream transport failures by kind.",
    "newapi_proxy_reveal_total": "Token key reveal attempts by result.",
    "newapi_proxy_add_quota_total": "add_quota attempts by result.",
    "newapi_proxy_add_quota_value_total": "Quota units new-api confirmed added (success:true).",
    "newapi_proxy_break_glass_total": "Break-glass (bg) requests by result.",
    "newapi_proxy_replay_store_errors_total": "Shared replay store (Valkey) failures by kind (request got 503 busy).",
    "newapi_proxy_readiness_checks_total": "Readiness checks actually run (cached answers excluded), by result.",
    "newapi_proxy_inflight": "Requests currently in flight per identity.",
    "newapi_proxy_add_quota_budget_used": "add_quota quota units used in the last hour (this replica).",
}


def format_value(value: float) -> str:
    """Exact text for a sample: quota sums reach 1e8+ and ``:g`` keeps 6 digits."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    if float(value).is_integer() and abs(value) < 2**63:
        return str(int(value))
    return repr(float(value))


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = {}
        self._gauges: dict[str, Callable[[], dict[tuple, float]]] = {}

    def inc(self, name: str, value: float = 1, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def get(self, name: str, **labels: str) -> float:
        return self._counters.get((name, tuple(sorted(labels.items()))), 0)

    def gauge(self, name: str, collect: Callable[[], dict[tuple, float]]) -> None:
        self._gauges[name] = collect

    @staticmethod
    def _labels(pairs: tuple) -> str:
        if not pairs:
            return ""
        inner = ",".join(f'{k}="{v}"' for k, v in pairs)
        return "{" + inner + "}"

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            counters = sorted(self._counters.items())
        names_done: set[str] = set()
        for (name, labels), value in counters:
            if name not in names_done:
                lines.append(f"# HELP {name} {_HELP.get(name, name)}")
                lines.append(f"# TYPE {name} counter")
                names_done.add(name)
            lines.append(f"{name}{self._labels(labels)} {format_value(value)}")
        for name, collect in sorted(self._gauges.items()):
            lines.append(f"# HELP {name} {_HELP.get(name, name)}")
            lines.append(f"# TYPE {name} gauge")
            for labels, value in sorted(collect().items()):
                lines.append(f"{name}{self._labels(labels)} {format_value(value)}")
        return "\n".join(lines) + "\n"
