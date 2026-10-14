"""Bounded replay of persisted signals of one mode through the opportunity pipeline.

Shared by ``process_shadow_signals`` (SHADOW, unchanged behaviour), the dormant ``process_live_signals`` (LIVE, only
while LIVE processing is enabled) and the read-only readiness report (SHADOW, dry run). Signals are selected in id
order inside a detection window; one ``as_of`` serves the whole replay; C8 makes every rerun idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from django.apps import apps
from django.utils import timezone

from .company_signals import LIVE, SHADOW
from .opportunity_pipeline import process_company_signal

MAX_LIMIT = 1000
COUNTERS = ("considered", "entitled_considered", "skipped_not_entitled", "matched", "insufficient_state", "no_match",
            "created", "updated", "unchanged", "below_threshold", "skipped_live_backed")


@dataclass
class ReplayTotals:
    mode: str
    dry_run: bool
    selected: int = 0
    processed: int = 0
    failed: int = 0
    errors: int = 0
    last_id: int | None = None
    counters: dict = field(default_factory=lambda: dict.fromkeys(COUNTERS, 0))
    runs: list = field(default_factory=list)


def replay_signals(*, mode: str, since_hours: int, limit: int, after_id: int = 0, dry_run: bool = False,
                   as_of=None) -> ReplayTotals:
    """Replay at most ``limit`` signals of ``mode`` detected in the last ``since_hours``, after ``after_id``."""
    if mode not in (SHADOW, LIVE):
        raise ValueError(f"unknown signal mode: {mode}")
    if since_hours < 1:
        raise ValueError("since_hours must be at least 1")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be from 1 to {MAX_LIMIT}")
    as_of = as_of or timezone.now()
    signals = list(apps.get_model("gemiapp", "CompanySignal").objects
                   .filter(mode=mode, detected_at__gte=as_of - timedelta(hours=since_hours), detected_at__lte=as_of,
                           pk__gt=after_id)
                   .order_by("pk")[:limit])
    totals = ReplayTotals(mode=mode, dry_run=dry_run, selected=len(signals),
                          last_id=signals[-1].pk if signals else None)
    for signal in signals:
        run = process_company_signal(signal, as_of=as_of, dry_run=dry_run)
        totals.runs.append(run)
        totals.processed += int(run.processed)
        totals.failed += int(bool(run.skipped_reason))
        totals.errors += len(run.errors)
        for name in COUNTERS:
            totals.counters[name] += getattr(run, name)
    return totals


def replay_window(*, mode: str, since_hours: int, dry_run: bool, page: int = MAX_LIMIT) -> ReplayTotals:
    """The whole window, page by page (id order). Used by the read-only readiness report with ``dry_run=True``."""
    as_of = timezone.now()
    total = ReplayTotals(mode=mode, dry_run=dry_run)
    after = 0
    while True:
        chunk = replay_signals(mode=mode, since_hours=since_hours, limit=page, after_id=after, dry_run=dry_run,
                               as_of=as_of)
        total.selected += chunk.selected
        total.processed += chunk.processed
        total.failed += chunk.failed
        total.errors += chunk.errors
        for name in COUNTERS:
            total.counters[name] += chunk.counters[name]
        if chunk.selected < page:
            total.last_id = chunk.last_id or total.last_id
            return total
        after = total.last_id = chunk.last_id
