"""In-memory, per-replica limiters: replay cache, in-flight caps, add-quota budget.

All three run on the single asyncio event loop; the check-and-update steps
contain no ``await`` so they are atomic with respect to other requests.
Restarting a replica resets them — a documented approximation (DESIGN
§1.4.2-1, -5, -9).
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass


class ReplayCache:
    """Accept each signature once within ``ttl`` seconds."""

    def __init__(self, ttl: float, max_entries: int):
        self.ttl = ttl
        self.max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()

    def _purge(self, now: float) -> None:
        while self._seen:
            sig, expires = next(iter(self._seen.items()))
            if expires > now:
                break
            self._seen.popitem(last=False)

    def add(self, sig: str, now: float) -> str:
        """Return "ok", "replay" or "full" (fail closed when saturated)."""
        self._purge(now)
        if sig in self._seen:
            return "replay"
        if len(self._seen) >= self.max_entries:
            return "full"
        self._seen[sig] = now + self.ttl
        return "ok"

    def __len__(self) -> int:
        return len(self._seen)


class InflightLimiter:
    """Non-blocking per-identity concurrency caps (503 ``busy`` when full)."""

    def __init__(self, limits: dict[str, int]):
        self.limits = dict(limits)
        self.current = {name: 0 for name in limits}

    def try_acquire(self, name: str) -> bool:
        if self.current[name] >= self.limits[name]:
            return False
        self.current[name] += 1
        return True

    def release(self, name: str) -> None:
        self.current[name] -= 1


@dataclass
class Reservation:
    at: float
    amount: int
    released: bool = False


class HourlyBudget:
    """Sliding one-hour cap on add_quota value for this replica."""

    WINDOW = 3600.0

    def __init__(self, cap: int):
        self.cap = cap
        self._entries: deque[Reservation] = deque()

    def _purge(self, now: float) -> None:
        while self._entries and self._entries[0].at <= now - self.WINDOW:
            self._entries.popleft()

    def used(self, now: float) -> int:
        self._purge(now)
        return sum(r.amount for r in self._entries if not r.released)

    def reserve(self, amount: int, now: float) -> Reservation | None:
        if amount > self.cap - self.used(now):
            return None
        reservation = Reservation(at=now, amount=amount)
        self._entries.append(reservation)
        return reservation

    @staticmethod
    def release(reservation: Reservation) -> None:
        """Give the amount back: only when the call certainly did not run."""
        reservation.released = True
