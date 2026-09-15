"""Shared request budget for the ΓΕΜΗ Open Data API.

The gateway allows 8 requests per minute per API key (docs/GEMI_API_CAPABILITY_REPORT.md), and
every consumer of the key -- the daily and intraday imports, Superadmin manual runs and every future
collector, in any worker, cluster or instance -- draws on that one allowance. Before sending
anything, a process claims a slot in the shared database cache.

How the ceiling is guaranteed
-----------------------------
Time is cut into consecutive slots of ``(window + skew margin) / (capacity - 1)`` seconds: 10.33 s
for the default capacity of 7. A request may only be sent by the process that claimed the current
slot, and a slot is claimed with ``cache.add()`` on a key built from the slot's index. The index only
ever grows, so every claim is a fresh INSERT and the cache table's primary key admits exactly one
winner per slot. Any rolling 60-second interval -- even with up to the margin of clock skew between
instances -- overlaps at most 7 slots, so no rolling minute, and therefore no gateway minute, sees
more than 7 requests. Sustained throughput is one request per slot, about 5.8 per minute.

A fixed set of reusable token keys with a TTL would not be safe: Django's DatabaseCache replaces an
*expired* row with a plain UPDATE, which two processes can both "win".

Priority lanes
--------------
A caller that has to wait leaves a short-lived marker for its lane. Before claiming a slot, a caller
checks for markers of every higher-priority lane and yields while one is present, so a backlog of
refresh or document requests cannot delay the imports that feed customer digests.

Gateway feedback
----------------
``defer_until()`` records a shared cooldown taken from ``Retry-After`` or ``RateLimit-Reset``. Every
caller honours it, so one 429 pauses every process -- including when the budget was spent by
something outside this application, such as another environment using the same key.

The budget fails closed: if the shared store cannot be read or written, no request is sent.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Callable, Iterable, Protocol

from django.conf import settings
from django.core.cache import caches

from .errors import GemiBudgetTimeoutError, GemiBudgetUnavailableError

logger = logging.getLogger(__name__)

# Observed gateway limit, and the ceiling this application stays at or below across all processes.
GATEWAY_LIMIT_PER_MINUTE = 8
MAX_REQUESTS_PER_MINUTE = 7


class GemiLane(IntEnum):
    """Priority of a GEMI request. A lower value is served first when callers compete."""

    DISCOVERY = 1
    DIGEST_IMPORT = 2
    MONITORED_REFRESH = 3
    DOCUMENTS = 4


@dataclass(frozen=True)
class BudgetConfig:
    capacity: int = MAX_REQUESTS_PER_MINUTE
    window_seconds: float = 60.0
    clock_skew_margin_seconds: float = 2.0
    poll_interval_seconds: float = 2.0

    def __post_init__(self):
        if not 2 <= self.capacity <= MAX_REQUESTS_PER_MINUTE:
            raise ValueError(f"capacity must be between 2 and {MAX_REQUESTS_PER_MINUTE}, got {self.capacity}")

    @property
    def slot_seconds(self) -> float:
        return (self.window_seconds + self.clock_skew_margin_seconds) / (self.capacity - 1)

    @classmethod
    def from_settings(cls) -> "BudgetConfig":
        requested = int(getattr(settings, "GEMI_RATE_LIMIT_PER_MINUTE", MAX_REQUESTS_PER_MINUTE))
        capacity = min(max(requested, 2), MAX_REQUESTS_PER_MINUTE)
        if capacity != requested:
            logger.warning(
                "GEMI_RATE_LIMIT_PER_MINUTE=%s is outside 2..%s; using %s.",
                requested, MAX_REQUESTS_PER_MINUTE, capacity,
            )
        return cls(capacity=capacity)


class BudgetStore(Protocol):
    def claim(self, key: str, value: float, ttl: float) -> bool: ...

    def get_float(self, key: str) -> float | None: ...

    def set_float(self, key: str, value: float, ttl: float) -> None: ...

    def exists_any(self, keys: Iterable[str]) -> bool: ...


class CacheBudgetStore:
    """BudgetStore on a Django cache alias.

    Defaults to "shared", the DatabaseCache every worker, cluster and instance sees -- never the
    per-process LocMem "default", which would give each process its own full allowance.
    """

    def __init__(self, alias: str = "shared", prefix: str = "gemi-budget"):
        self.alias = alias
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    @staticmethod
    def _timeout(ttl: float) -> int:
        # The database cache stores expiry in whole seconds; rounding up never shortens a claim.
        return max(1, math.ceil(ttl))

    def claim(self, key: str, value: float, ttl: float) -> bool:
        return bool(caches[self.alias].add(self._key(key), value, self._timeout(ttl)))

    def get_float(self, key: str) -> float | None:
        value = caches[self.alias].get(self._key(key))
        return None if value is None else float(value)

    def set_float(self, key: str, value: float, ttl: float) -> None:
        caches[self.alias].set(self._key(key), value, self._timeout(ttl))

    def exists_any(self, keys: Iterable[str]) -> bool:
        return bool(caches[self.alias].get_many([self._key(key) for key in keys]))


class GemiRateBudget:
    COOLDOWN_KEY = "cooldown-until"
    MIN_SLEEP_SECONDS = 0.05

    def __init__(
        self,
        store: BudgetStore | None = None,
        config: BudgetConfig | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.store = store if store is not None else CacheBudgetStore()
        self.config = config if config is not None else BudgetConfig.from_settings()
        self._clock = clock
        self._sleep = sleep

    @property
    def _marker_ttl(self) -> float:
        return self.config.poll_interval_seconds * 2 + 1

    def acquire(self, lane: GemiLane, *, max_wait: float) -> None:
        """Block until this process may send one GEMI request in ``lane``.

        Raises GemiBudgetTimeoutError after ``max_wait`` seconds without a slot, and
        GemiBudgetUnavailableError if the shared store fails.
        """
        lane = GemiLane(lane)
        slot_seconds = self.config.slot_seconds
        started = self._clock()
        deadline = started + max_wait
        while True:
            now = self._clock()
            wait = self._guarded(self._wait_before_claim, lane, now)
            if wait <= 0:
                slot = math.floor(now / slot_seconds)
                if self._guarded(self.store.claim, f"slot:{slot}", now, slot_seconds * 2):
                    if now - started >= 1:
                        logger.debug("GEMI budget: lane %s waited %.1fs for a slot.", lane.name, now - started)
                    return
                wait = (slot + 1) * slot_seconds - now
            if now >= deadline:
                raise GemiBudgetTimeoutError(
                    f"Δεν βρέθηκε διαθέσιμο περιθώριο κλήσεων στο GEMI API μέσα σε {max_wait:.0f}s "
                    f"(lane {lane.name})."
                )
            if lane != max(GemiLane):
                self._guarded(self.store.set_float, f"waiting:{lane.value}", now, self._marker_ttl)
            self._sleep(max(self.MIN_SLEEP_SECONDS, min(wait, self.config.poll_interval_seconds, deadline - now)))

    def defer_until(self, until: float, *, reason: str) -> None:
        """Pause every GEMI request, in every process, until ``until`` (epoch seconds)."""
        now = self._clock()
        if until <= now:
            return
        current = self._guarded(self.store.get_float, self.COOLDOWN_KEY)
        if current is not None and current >= until:
            return
        self._guarded(
            self.store.set_float, self.COOLDOWN_KEY, until, until - now + self.config.clock_skew_margin_seconds
        )
        logger.warning("GEMI budget: pausing all GEMI requests for %.0fs (%s).", until - now, reason)

    def _wait_before_claim(self, lane: GemiLane, now: float) -> float:
        cooldown_until = self.store.get_float(self.COOLDOWN_KEY)
        if cooldown_until is not None and cooldown_until > now:
            return cooldown_until - now
        higher = [f"waiting:{other.value}" for other in GemiLane if other < lane]
        if higher and self.store.exists_any(higher):
            return self.config.poll_interval_seconds
        return 0.0

    def _guarded(self, operation, *args):
        try:
            return operation(*args)
        except Exception as exc:
            logger.error("GEMI budget store unavailable (%s); refusing to send the request.", type(exc).__name__)
            raise GemiBudgetUnavailableError(
                "Ο κοινός μετρητής κλήσεων του GEMI API δεν είναι διαθέσιμος· η κλήση δεν στάλθηκε."
            ) from None
