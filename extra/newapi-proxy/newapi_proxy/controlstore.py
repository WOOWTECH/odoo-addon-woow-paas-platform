"""Bounded, fail-closed PostgreSQL control plane; never retries uncertain commits.

Runtime installs no schema. Every operation uses a fresh connection and a single
transaction. Reservation time is the database clock, not the upstream HTTP start;
callers must bound that gap and account for its sliding-window boundary effect.
Only pass an already redacted request identifier to ``finish`` (syntax validation
cannot determine whether an otherwise safe identifier contains sensitive data).
"""
from __future__ import annotations

import asyncio
import math
import re
import uuid
from typing import Any

import psycopg
from psycopg import waiting

_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z", re.ASCII)
_CREDENTIAL = re.compile(r"nap_|sk-|bearer|[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|[0-9a-f]{64}", re.IGNORECASE | re.ASCII)
_ENV = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
_RESULTS = frozenset({"forwarded", "connect", "reset", "timeout", "cancelled", "not_sent"})
_PRIORITIES = frozenset({"conversion", "free_meter", "other", "balance"})


class ControlStoreError(Exception):
    """Stable value-free category. Driver/server text is never exposed."""

    def __init__(self, kind: str = "unavailable"):
        self.kind = kind if kind in {"invalid", "unavailable", "timeout", "rejected", "protocol", "closed"} else "unavailable"
        super().__init__(self.kind)


def _integer(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _attempt(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except ValueError:
        return False


class _DeadlineConnection(psycopg.AsyncConnection):
    """Disposable connection: cancellation closes instead of cancel-and-drain.

    Psycopg 3.3.6 AsyncConnection.wait adds up to 10 seconds on interruption.
    Its wait method uses the same libpq generator here,
    but never sends a cancellation request or waits for the server to answer.
    All libpq access runs serially on this event loop; finish is synchronous.
    Recheck this adapter and stalled-server tests when upgrading the driver.
    """

    async def wait(self, gen, interval=0.1, timeout=None):
        try:
            return await waiting.wait_async(gen, self.pgconn.socket, interval=interval, timeout=timeout)
        except BaseException:
            try:
                self.pgconn.finish()
            except Exception:
                pass
            raise


class PostgresControlStore:
    def __init__(self, dsn: str, environment: str, op_timeout: float = 0.5):
        if (not isinstance(dsn, str) or not dsn.strip()
                or not isinstance(environment, str) or not _ENV.fullmatch(environment)
                or isinstance(op_timeout, bool) or not isinstance(op_timeout, (float, int))
                or not math.isfinite(op_timeout) or op_timeout <= 0):
            raise ControlStoreError("invalid")
        self._dsn = dsn
        self._environment = environment
        self._op_timeout = float(op_timeout)
        self._closed = False

    def __repr__(self) -> str:
        return "PostgresControlStore(<redacted>)"

    async def _run(self, query: str, values: tuple) -> Any:
        if self._closed:
            raise ControlStoreError("closed")
        conn = None
        try:
            async with asyncio.timeout(self._op_timeout):
                conn = await _DeadlineConnection.connect(self._dsn)
                # Explicitly override any DSN/session defaults for durable commits.
                await conn.execute("SET LOCAL synchronous_commit = on")
                await conn.execute("SET LOCAL search_path = pg_catalog")
                cursor = await conn.execute(query, values)
                row = await cursor.fetchone()
                await conn.commit()
                if row is None or len(row) != 1:
                    raise ControlStoreError("protocol")
                return row[0]
        except ControlStoreError:
            raise
        except TimeoutError:
            raise ControlStoreError("timeout") from None
        except psycopg.errors.RaiseException:
            raise ControlStoreError("rejected") from None
        except Exception:
            # Includes unknown commit outcomes. Never retry or expose diagnostics.
            raise ControlStoreError("unavailable") from None
        finally:
            if conn is not None:
                # libpq finish is synchronous and sends no rollback/query. This
                # cannot stall cleanup, leak a cancelled connection, or mask the
                # original exception. AsyncConnection.close delegates to this too.
                try:
                    conn.pgconn.finish()
                except Exception:
                    pass

    async def acquire(self, priority: str) -> int:
        if not isinstance(priority, str) or priority not in _PRIORITIES:
            raise ControlStoreError("invalid")
        result = await self._run("SELECT proxy_control.acquire(%s, %s)", (self._environment, priority))
        if not _integer(result, 0, 60):
            raise ControlStoreError("protocol")
        return result

    async def begin(self, attempt_id: str, target_user_id: int, quota: int) -> int:
        if not _attempt(attempt_id) or not _integer(target_user_id, 1, 2147483647) or not _integer(quota, 1, 156250000):
            raise ControlStoreError("invalid")
        result = await self._run("SELECT proxy_control.begin_attempt(%s, %s, %s, %s)",
                                 (self._environment, attempt_id, target_user_id, quota))
        if not _integer(result, 1, 9223372036854775807):
            raise ControlStoreError("protocol")
        return result

    async def finish(self, attempt_id: str, result: str, status: int | None = None,
                     request_id: str | None = None) -> None:
        if (not _attempt(attempt_id) or not isinstance(result, str) or result not in _RESULTS
                or (status is not None and not _integer(status, 100, 599))
                or (request_id is not None and (not isinstance(request_id, str) or not _SAFE.fullmatch(request_id)
                    or _CREDENTIAL.search(request_id)))
                or (result == "forwarded" and status is None)
                or (result != "forwarded" and (status is not None or request_id is not None))):
            raise ControlStoreError("invalid")
        await self._run("SELECT proxy_control.finish_attempt(%s, %s, %s, %s, %s)",
                        (self._environment, attempt_id, result, status, request_id))

    async def read(self, after: int, through: int | None = None, limit: int = 100) -> dict:
        if (not _integer(after, 0, 9223372036854775807)
                or (through is not None and not _integer(through, after, 9223372036854775807))
                or not _integer(limit, 1, 100)):
            raise ControlStoreError("invalid")
        result = await self._run("SELECT proxy_control.read_events(%s, %s, %s, %s)",
                                 (self._environment, after, through, limit))
        if (not isinstance(result, dict) or result.get("env") != self._environment
                or not _integer(result.get("highwater"), after, 9223372036854775807)
                or (through is not None and result["highwater"] != through)
                or not isinstance(result.get("events"), list) or len(result["events"]) > limit):
            raise ControlStoreError("protocol")
        expected = min(limit, result["highwater"] - after)
        if len(result["events"]) != expected or any(
            not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] != after + offset
            for offset, event in enumerate(result["events"], 1)
        ):
            raise ControlStoreError("protocol")
        return result

    async def ping(self) -> bool:
        try:
            return await self._run("SELECT proxy_control.ping(%s)", (self._environment,)) is True
        except ControlStoreError:
            return False

    async def aclose(self) -> None:
        self._closed = True
