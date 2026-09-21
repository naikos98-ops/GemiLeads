"""G6: what the application actually spends of the ΓΕΜΗ request allowance.

Why a table and not the shared cache
------------------------------------
The obvious no-migration answer is the ``shared`` DatabaseCache that already carries the rate budget. It
cannot hold these metrics correctly, for three separate reasons:

* **Counters lose updates.** ``DatabaseCache`` does not implement ``incr``, so it inherits
  ``BaseCache.incr``: ``get()``, add one in Python, ``set()``. Two workers that read the same value both
  write ``n + 1`` and one request disappears from the count. The budget itself never needs this -- it uses
  ``add()`` on a key nobody else can claim, which *is* atomic -- but a counter is a read-modify-write and
  there is no lock behind it.
* **Rows are culled.** ``DatabaseCache._cull`` deletes a share of the table once ``MAX_ENTRIES`` is passed.
  Measurements would vanish silently, and precisely when traffic is highest.
* **The cache cannot be enumerated.** The API has no "list keys", so a 24-hour report would have to guess
  which bucket keys exist and read them back one by one, and a bucket is not an exact rolling minute anyway.

So this uses one small append-only table (``GemiRequestAttempt``, migration 0055). Correctness over avoiding
a migration, as instructed: with one row per attempt and its exact timestamp, the peak over a **rolling**
60-second window is computed exactly rather than approximated by wall-clock minutes.

What is written, and what is never written
------------------------------------------
Per attempt: when it happened, the lane, the attempt number, an outcome class, the HTTP status when there was
a response, how long the caller waited for the budget, and a normalised endpoint. Never: the API key, query
parameters, request or response bodies, ΓΕΜΗ numbers, company or customer data. ``normalise_endpoint``
replaces every numeric path segment with ``{id}`` and drops any query string, so ``/companies/123456789000``
is stored as ``/companies/{id}``.

Production safety
-----------------
Recording makes no GEMI request and adds one small INSERT after the response has already arrived. It is
**fail-open**: any failure is logged and swallowed, so ingestion never breaks because measurement broke. The
INSERT runs in its own savepoint, so a database error cannot poison a transaction the caller may be inside.
It never weakens the budget: a budget failure is recorded and then re-raised unchanged, and the budget stays
fail-closed.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .rate_budget import MAX_REQUESTS_PER_MINUTE, BudgetConfig, GemiLane

logger = logging.getLogger(__name__)

SUCCESS = "success"
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
CLIENT_ERROR = "client_error"
TRANSPORT_ERROR = "transport_error"
BUDGET_TIMEOUT = "budget_timeout"
BUDGET_UNAVAILABLE = "budget_unavailable"

# Outcomes where the attempt reached the transport, so a request slot was really spent.
SENT_OUTCOMES = (SUCCESS, RATE_LIMITED, SERVER_ERROR, CLIENT_ERROR, TRANSPORT_ERROR)

ROLLING_WINDOW_SECONDS = 60.0
# A window is "high utilisation" at this share of the effective capacity, and "saturated" at the capacity.
# Both are judged against the effective configured capacity: that is what the budget actually allows.
HIGH_UTILISATION_SHARE = 0.8

_NUMERIC_SEGMENT = re.compile(r"^\d[\d\-]*$")


def metrics_enabled() -> bool:
    return bool(getattr(settings, "GEMI_REQUEST_METRICS_ENABLED", True))


def classify_status(status: int) -> str:
    """The outcome class of an HTTP status. Only the class is stored alongside the status itself."""
    if 200 <= status < 300:
        return SUCCESS
    if status == 429:
        return RATE_LIMITED
    if 500 <= status < 600:
        return SERVER_ERROR
    if 400 <= status < 500:
        return CLIENT_ERROR
    return CLIENT_ERROR if status < 500 else SERVER_ERROR


def normalise_endpoint(path: str) -> str:
    """A safe endpoint name: no query string, and no identifier in the path.

    ``/companies/123456789000?x=1`` -> ``/companies/{id}``. A ΓΕΜΗ number is a public identifier rather than
    personal data, but it still names one company, and nothing here needs it.
    """
    path = (path or "").split("?", 1)[0].split("#", 1)[0].strip()
    if not path:
        return ""
    segments = ["{id}" if _NUMERIC_SEGMENT.match(segment) else segment for segment in path.split("/")]
    return "/".join(segments)[:64]


def record_attempt(
    *,
    lane: GemiLane,
    attempt: int,
    endpoint: str,
    outcome: str,
    http_status: int | None,
    budget_wait_seconds: float,
    occurred_at: datetime | None = None,
) -> bool:
    """Write one attempt. Returns whether it was written; never raises, never blocks ingestion."""
    if not metrics_enabled():
        return False
    try:
        model = apps.get_model("gemiapp", "GemiRequestAttempt")
        with transaction.atomic():          # its own savepoint: a failure here cannot poison the caller's
            model.objects.create(
                occurred_at=occurred_at or timezone.now(),
                lane=int(GemiLane(lane)),
                attempt=max(1, int(attempt)),
                outcome=outcome,
                http_status=http_status,
                budget_wait_ms=max(0, int(round((budget_wait_seconds or 0.0) * 1000))),
                endpoint=normalise_endpoint(endpoint),
            )
        return True
    except Exception as exc:                # noqa: BLE001 -- observability must never break ingestion
        logger.error("GEMI request metrics: could not record an attempt (%s); the request is unaffected.",
                     type(exc).__name__)
        return False


# --------------------------------------------------------------------------- the report


@dataclass(frozen=True)
class LaneUsage:
    lane: str
    attempts: int = 0            # attempts that consumed a slot
    successes: int = 0
    retries: int = 0
    failures: int = 0            # slot-consuming attempts that were not 2xx
    not_sent: int = 0            # budget_timeout / budget_unavailable: no slot consumed


@dataclass(frozen=True)
class BudgetReport:
    """Facts about one observation window. Nothing here decides whether G6 passes."""

    window_start: datetime
    window_end: datetime
    hours: float
    # The application's safe ceiling across every process (MAX_REQUESTS_PER_MINUTE), the theoretical maximum.
    ceiling_per_minute: int
    # GEMI_RATE_LIMIT_PER_MINUTE as configured, before clamping.
    configured_limit_per_minute: int
    # What the budget really enforces: the configured limit clamped exactly as BudgetConfig.from_settings()
    # clamps it. Any G6 operational decision uses this, not the theoretical ceiling.
    effective_capacity_per_minute: int

    recorded_attempts: int = 0        # every row, including the ones that never reached the transport
    sent_attempts: int = 0            # attempts that consumed a request slot
    logical_calls: int = 0            # rows with attempt == 1
    retries: int = 0                  # rows with attempt > 1 that consumed a slot
    successes: int = 0
    rate_limited: int = 0
    server_errors: int = 0
    client_errors: int = 0
    transport_errors: int = 0
    budget_timeouts: int = 0
    budget_unavailable: int = 0
    # Rows stamped after the end of the window. Always 0 unless a worker's clock runs ahead; they are
    # outside the window by definition, so they are reported rather than silently dropped.
    future_attempts: int = 0

    total_wait_seconds: float = 0.0
    average_wait_seconds: float = 0.0
    max_wait_seconds: float = 0.0

    peak_rolling_60s: int = 0
    peak_window_start: datetime | None = None
    saturated_windows: int = 0              # anchored windows at or above the effective capacity
    high_utilisation_windows: int = 0       # anchored windows at >= HIGH_UTILISATION_SHARE of that capacity
    saturated_ceiling_windows: int = 0      # anchored windows at or above the safe ceiling

    by_lane: tuple[LaneUsage, ...] = field(default_factory=tuple)
    endpoints: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    # Headroom is never negative: a peak above a limit is not "negative room", it is an overrun, reported as
    # its own figure so it cannot be read as capacity left over.

    @property
    def utilisation_vs_ceiling_pct(self) -> float:
        return _share(self.peak_rolling_60s, self.ceiling_per_minute)

    @property
    def headroom_vs_ceiling(self) -> int:
        return max(0, self.ceiling_per_minute - self.peak_rolling_60s)

    @property
    def over_ceiling(self) -> int:
        return max(0, self.peak_rolling_60s - self.ceiling_per_minute)

    @property
    def utilisation_vs_capacity_pct(self) -> float:
        return _share(self.peak_rolling_60s, self.effective_capacity_per_minute)

    @property
    def headroom_vs_capacity(self) -> int:
        return max(0, self.effective_capacity_per_minute - self.peak_rolling_60s)

    @property
    def over_capacity(self) -> int:
        return max(0, self.peak_rolling_60s - self.effective_capacity_per_minute)

    @property
    def at_or_over_capacity(self) -> bool:
        return self.effective_capacity_per_minute > 0 and self.peak_rolling_60s >= self.effective_capacity_per_minute

    @property
    def average_per_minute(self) -> float:
        minutes = (self.window_end - self.window_start).total_seconds() / 60.0
        return self.sent_attempts / minutes if minutes > 0 else 0.0


def _share(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def effective_capacity() -> int:
    """Requests per minute the budget really allows. Deliberately *calls* BudgetConfig.from_settings() rather
    than restating its clamp (2..MAX_REQUESTS_PER_MINUTE), so the report can never disagree with the budget."""
    return BudgetConfig.from_settings().capacity


def _rolling_windows(moments: list[datetime]):
    """Yield (window_start, attempts_in_that_window) for the window anchored at each attempt.

    The maximum number of attempts in *any* 60-second interval is always attained by an interval that
    starts at an attempt, so these anchored windows give the exact rolling peak rather than the
    wall-clock-minute approximation a bucket counter would give. They overlap: a count of windows is a count
    of moments at which the rate was that high, not a count of disjoint periods.
    """
    moments = sorted(moments)
    window = timedelta(seconds=ROLLING_WINDOW_SECONDS)
    start = 0
    for end in range(len(moments)):
        while moments[end] - moments[start] >= window:
            start += 1
        yield moments[start], end - start + 1


def _rolling_peak(moments: list[datetime]) -> tuple[int, datetime | None]:
    """The exact peak over a rolling 60-second window, and when that window began."""
    peak, peak_at = 0, None
    for begins, count in _rolling_windows(moments):
        if count > peak:
            peak, peak_at = count, begins
    return peak, peak_at


def _window_counts(moments: list[datetime], ceiling: int) -> tuple[int, int]:
    """(saturated, high-utilisation) counts over the same anchored rolling windows."""
    if not moments or ceiling <= 0:
        return 0, 0
    # ceil, not int: truncating would count a window at 5/7 (71%) as "at least 80% of the ceiling".
    high_threshold = max(1, math.ceil(HIGH_UTILISATION_SHARE * ceiling))
    saturated = high = 0
    for _begins, count in _rolling_windows(moments):
        saturated += count >= ceiling
        high += count >= high_threshold
    return saturated, high


def build_report(*, hours: float = 24.0, now: datetime | None = None, ceiling: int | None = None) -> BudgetReport:
    """Read-only: summarise the recorded attempts of the last ``hours``. Writes nothing, sends nothing."""
    model = apps.get_model("gemiapp", "GemiRequestAttempt")
    end = now or timezone.now()
    start = end - timedelta(hours=float(hours))
    ceiling = int(ceiling if ceiling is not None else MAX_REQUESTS_PER_MINUTE)
    configured = int(getattr(settings, "GEMI_RATE_LIMIT_PER_MINUTE", MAX_REQUESTS_PER_MINUTE))
    capacity = effective_capacity()

    rows = list(
        model.objects.filter(occurred_at__gte=start, occurred_at__lte=end)
        .order_by("occurred_at")
        .values_list("occurred_at", "lane", "attempt", "outcome", "budget_wait_ms", "endpoint")
    )
    sent = [row for row in rows if row[3] in SENT_OUTCOMES]
    moments = [row[0] for row in sent]
    peak, peak_at = _rolling_peak(moments)
    saturated, high = _window_counts(moments, capacity)
    saturated_ceiling, _ = _window_counts(moments, ceiling)

    waits = [row[4] / 1000.0 for row in rows]
    lanes = []
    for lane in GemiLane:
        lane_rows = [row for row in rows if row[1] == int(lane)]
        lane_sent = [row for row in lane_rows if row[3] in SENT_OUTCOMES]
        lanes.append(LaneUsage(
            lane=lane.name,
            attempts=len(lane_sent),
            successes=sum(1 for row in lane_sent if row[3] == SUCCESS),
            retries=sum(1 for row in lane_sent if row[2] > 1),
            failures=sum(1 for row in lane_sent if row[3] != SUCCESS),
            not_sent=len(lane_rows) - len(lane_sent),
        ))
    endpoints: dict[str, int] = {}
    for row in sent:
        endpoints[row[5]] = endpoints.get(row[5], 0) + 1

    return BudgetReport(
        window_start=start,
        window_end=end,
        hours=float(hours),
        ceiling_per_minute=ceiling,
        configured_limit_per_minute=configured,
        effective_capacity_per_minute=capacity,
        recorded_attempts=len(rows),
        sent_attempts=len(sent),
        logical_calls=sum(1 for row in rows if row[2] == 1),
        retries=sum(1 for row in sent if row[2] > 1),
        successes=sum(1 for row in sent if row[3] == SUCCESS),
        rate_limited=sum(1 for row in sent if row[3] == RATE_LIMITED),
        server_errors=sum(1 for row in sent if row[3] == SERVER_ERROR),
        client_errors=sum(1 for row in sent if row[3] == CLIENT_ERROR),
        transport_errors=sum(1 for row in sent if row[3] == TRANSPORT_ERROR),
        budget_timeouts=sum(1 for row in rows if row[3] == BUDGET_TIMEOUT),
        budget_unavailable=sum(1 for row in rows if row[3] == BUDGET_UNAVAILABLE),
        future_attempts=model.objects.filter(occurred_at__gt=end).count(),
        total_wait_seconds=sum(waits),
        average_wait_seconds=(sum(waits) / len(waits)) if waits else 0.0,
        max_wait_seconds=max(waits, default=0.0),
        peak_rolling_60s=peak,
        peak_window_start=peak_at,
        saturated_windows=saturated,
        high_utilisation_windows=high,
        saturated_ceiling_windows=saturated_ceiling,
        by_lane=tuple(lanes),
        endpoints=tuple(sorted(endpoints.items(), key=lambda item: (-item[1], item[0]))),
    )
