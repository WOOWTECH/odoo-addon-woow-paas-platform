"""In-memory, per-replica limiters: replay cache, in-flight caps, add-quota
budget, and a single-flight cache for the readiness check.

All of them run on the single asyncio event loop; the check-and-update steps
contain no ``await`` so they are atomic with respect to other requests.
Restarting a replica resets them.

The replay cache here is only the first, local layer: across replicas the
record lives in Valkey (``replaystore.py``). The add-quota budget stays per
replica (cap ÷ replicas) — a documented approximation (DESIGN §1.4.2-5).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class ReplayCache:
    """Accept each ``kid:nonce`` once within ``ttl`` seconds (this replica)."""

    def __init__(self, ttl: float, max_entries: int):
        self.ttl = ttl
        self.max_entries = max_entries
        self._seen: OrderedDict[str, float] = OrderedDict()

    def _purge(self, now: float) -> None:
        while self._seen:
            key, expires = next(iter(self._seen.items()))
            if expires > now:
                break
            self._seen.popitem(last=False)

    def add(self, key: str, now: float) -> str:
        """Return "ok", "replay" or "full" (fail closed when saturated)."""
        self._purge(now)
        if key in self._seen:
            return "replay"
        if len(self._seen) >= self.max_entries:
            return "full"
        self._seen[key] = now + self.ttl
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


class CachedCheck(Generic[T]):
    """Run ``check`` at most once at a time and reuse its result for a while.

    However many callers ask, at most one check is in flight; its result is
    reused for ``ok_ttl`` seconds (``fail_ttl`` when ``is_ok`` says it
    failed). A caller that gives up does not cancel the shared check.
    """

    def __init__(
        self,
        check: Callable[[], Awaitable[T]],
        monotonic: Callable[[], float],
        is_ok: Callable[[T], bool],
        ok_ttl: float,
        fail_ttl: float,
    ):
        self._check = check
        self._monotonic = monotonic
        self._is_ok = is_ok
        self.ok_ttl = ok_ttl
        self.fail_ttl = fail_ttl
        self._value: T | None = None
        self._expires = 0.0
        self._task: asyncio.Future | None = None
        self.runs = 0

    async def _refresh(self) -> T:
        try:
            self.runs += 1
            value = await self._check()
            self._value = value
            self._expires = self._monotonic() + (self.ok_ttl if self._is_ok(value) else self.fail_ttl)
            return value
        finally:
            self._task = None

    async def get(self) -> T:
        if self._value is not None and self._monotonic() < self._expires:
            return self._value
        if self._task is None:
            self._task = asyncio.ensure_future(self._refresh())
        return await asyncio.shield(self._task)
