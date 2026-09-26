"""Pending-company hydration: canonical Company rows for Discovery v2 evidence that has none (Gemi Leads 2.0).

Why this exists
---------------
Discovery v2 (A10) records newly discovered companies as ``GemiDiscoveryObservation`` rows -- identifiers,
dates and a classification, deliberately no payload. The NEW_COMPANY producer (B2) can only anchor a signal to
a real ``Company``, so every eligible observation whose company the legacy importer never stored stays
``pending_no_company``. Late publications and invalid-date records never arrive through the legacy importer
(it selects by ``incorporationDate == target day``), so they would stay pending forever. The first G4 SHADOW
cycle in production ended with 578 of them.

This module closes that gap one explicit, operator-run step at a time:

    Discovery observation -> missing local Company -> THIS: canonical Company creation
      -> unchanged ``materialize_new_company_signals`` -> SHADOW NEW_COMPANY signal
      -> unchanged G2 pipeline (on commit) -> SHADOW opportunities

It only creates ``Company`` rows. It does not produce signals, snapshots, opportunities, matches, leads,
notifications or emails: the existing materialiser consumes the now-resolvable evidence on its next run, with
its own semantics unchanged (the evidence link is the original observation; ``detected_at`` follows the B2
rule ``max(discovery time, baseline observed_at)``).

Off by default, never scheduled
-------------------------------
``GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED`` (default False) gates everything. While it is off,
``hydrate_pending_companies`` refuses before selecting, fetching or writing anything -- dry runs included,
because a dry run still spends GEMI requests. It is a dedicated flag: ``GEMI_DISCOVERY_V2_ENABLED`` also opens
Discovery's ingest mode and belongs to the cutover decision, so it is not reused. Nothing in ``apps.SCHEDULES``
or any task calls this module; only ``manage.py hydrate_pending_discovery_companies`` does.

Selection
---------
Exactly the materialiser's own pending set: GEMI numbers with at least one eligible observation (B2's
``ELIGIBLE_CLASSIFICATIONS``: ``new_incorporation``, ``late_publication``, ``invalid_date``) and no local
``Company``. One entry per GEMI number however many observations it has, oldest evidence first (the earliest
run that saw it, then observation id), so a bounded run always works through the backlog in discovery order.
The incorporation date is never a selection criterion: a late or undated record is as real a company.

Source data: one validated search per company
---------------------------------------------
Observations carry no payload, so each company is fetched once with ``GET /companies?arGemi=<number>``:

* the shared ``GemiClient`` -- the one outbound path, with the shared A1 rate budget, bounded retries, G6
  request metrics and (when enabled) A4 source records; nothing here opens a second HTTP path;
* validated as ``ResponseFamily.COMPANY_SEARCH`` by the client before it is returned;
* in the ``MONITORED_REFRESH`` lane, the lane the legacy historical backfill already uses for bulk company
  fetches. It sits below ``DISCOVERY`` and ``DIGEST_IMPORT``, so G4 discovery and the customer-feeding import
  always win a contested slot.

A search item is used rather than ``GET /companies/{arGemi}`` because it is byte-for-byte the record shape the
legacy importer stores in ``Company.raw_data``; the materialiser's trust check re-validates exactly that shape
(as one ``company_search`` result) before it builds the detection-time baseline. Only an item whose own
``arGemi`` normalises to the requested number is accepted -- never company Y under company X. Multi-value
``arGemi`` searches would save requests but are not a verified API behaviour, so they are not used.

Pacing: at most one fetch per ``pace_seconds`` (default 20 s, i.e. <= 3 requests/minute). G6 measured an
effective capacity of 7/minute with an observed peak of 3/minute, so a default run stays inside the measured
headroom even before the budget's own lane priority applies. Retries inside the client still count against
the shared budget and show up in ``report_gemi_request_budget``.

Create-only
-----------
For each number, immediately before writing and inside one savepoint:

1. if a ``Company`` with that number exists -> skip; nothing is updated, no activity is rewritten;
2. otherwise ``Company.objects.create(gemi_number=..., **company_defaults(item))`` and
   ``sync_company_activities(company, item["activities"])`` -- the same two primitives the legacy importer
   uses, so normalisation, legacy-visible activity rows and the ActivityCode catalogue are identical.

``gemi_number`` is unique, so a writer that wins the race between the check and the insert makes this insert
fail with ``IntegrityError``; the savepoint rolls back and the company is counted as a race skip. Company and
activities commit together or not at all: no placeholder or half-written row can remain. The legacy writers
(``update_or_create``, also Discovery's own ingest mode) are deliberately not reused: they overwrite.

What a created Company means for the legacy product
---------------------------------------------------
A ``Company`` row is canonical data, so the legacy customer product sees it like any imported company
(dashboard archive, CSV export and, when its stored ``incorporation_date`` equals an import run's target date,
legacy Radar matching and digests). ``company_defaults`` is reused unchanged, and it stores a missing, invalid
or future source date as ``date.today()``. So:

* ``late_publication`` -> stored with its real, past date: visible in the archive, not in today's digest;
* ``new_incorporation`` -> the date the legacy importer would store itself when it imports the company;
* ``invalid_date`` (or a clamped future date) -> stored as **today**, so it enters today's legacy digest and
  matching as if incorporated today.

The report counts ``stored_as_today`` and ``date_clamped`` for every run (dry runs included), so an operator
sees this before and after enabling. That is the decision the flag protects; it is not hidden here.

Failures
--------
Per-company problems are recorded and the batch continues: a validation failure, no exact match
(``not_found``), an ambiguous answer, or an unexpected write error. Client-level problems stop the batch
(``aborted``): budget timeout or unavailability, exhausted retries, configuration (no key, staging collector
off), authentication or bad request -- they would repeat for every remaining company and only burn budget.
Either way the failed company has no row, the error class is reported, and the command exits non-zero.
No payload value is ever logged or printed.

Dry run
-------
``dry_run=True`` means **no database mutation**, not "no GEMI requests": it fetches and validates each selected
company (that is the only way to know what would be created and with which stored date) and writes nothing.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from django.apps import apps
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, Min, OuterRef

from .ingestion.client import get_gemi_client
from .ingestion.discovery import normalize_gemi_number
from .ingestion.errors import GemiApiError, GemiResponseValidationError
from .ingestion.rate_budget import GemiLane
from .new_company_signals import ELIGIBLE_CLASSIFICATIONS

logger = logging.getLogger(__name__)

HYDRATION_LANE = GemiLane.MONITORED_REFRESH
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
DEFAULT_PACE_SECONDS = 20.0

CREATED = "created"
WOULD_CREATE = "would_create"
RACE_SKIPPED = "race_skipped"
NOT_FOUND = "not_found"
AMBIGUOUS = "ambiguous"


class HydrationDisabled(Exception):
    """The capability is switched off; nothing was selected, fetched or written."""


def hydration_enabled() -> bool:
    return bool(getattr(settings, "GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED", False))


def _pending_observations():
    Company = apps.get_model("gemiapp", "Company")
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    return (
        GemiDiscoveryObservation.objects.filter(classification__in=ELIGIBLE_CLASSIFICATIONS)
        .exclude(Exists(Company.objects.filter(gemi_number=OuterRef("gemi_number"))))
    )


def pending_numbers_queryset():
    """One row per pending GEMI number, oldest discovery evidence first. Read-only."""
    return (
        _pending_observations().values("gemi_number")
        .annotate(first_seen=Min("run__started_at"), first_id=Min("id"))
        .order_by("first_seen", "first_id")
    )


@dataclass
class HydrationReport:
    dry_run: bool
    limit: int
    pace_seconds: float
    pending_observations: int = 0
    pending_before: int = 0
    selected: int = 0
    gemi_requests: int = 0           # logical requests this run issued (client retries are extra; see G6)
    fetched: int = 0                 # requests that returned a validated page
    created: int = 0
    would_create: int = 0
    already_local: int = 0           # found locally before fetching: no request spent
    race_skipped: int = 0            # appeared between the fetch and the write: nothing overwritten
    not_found: int = 0
    ambiguous: int = 0
    validation_failures: int = 0
    write_failures: int = 0
    stored_as_today: int = 0         # created (or would be) with incorporation_date == today: legacy-visible today
    date_clamped: int = 0            # company_defaults replaced a missing/invalid/future source date with today
    activities_created: int = 0
    aborted: bool = False
    abort_reason: str = ""
    pending_after: int = 0
    failed_gemi_numbers: list = field(default_factory=list)

    @property
    def failed(self) -> int:
        return self.not_found + self.ambiguous + self.validation_failures + self.write_failures + int(self.aborted)

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        made = f"would create={self.would_create}" if self.dry_run else f"created={self.created}"
        return [
            f"{prefix}pending before: gemi numbers={self.pending_before} observations={self.pending_observations} "
            f"limit={self.limit} selected={self.selected} pace={self.pace_seconds:g}s lane={HYDRATION_LANE.name}",
            f"{prefix}gemi requests={self.gemi_requests} fetched={self.fetched} {made} "
            f"already_local={self.already_local} race_skipped={self.race_skipped} "
            f"activities_created={self.activities_created}",
            f"{prefix}failed={self.failed} (not_found={self.not_found} ambiguous={self.ambiguous} "
            f"validation={self.validation_failures} write={self.write_failures} aborted={self.aborted})",
            f"{prefix}legacy exposure: stored_as_today={self.stored_as_today} date_clamped={self.date_clamped}",
            f"{prefix}pending after={self.pending_after} · signals are not produced here: run "
            f"materialize_new_company_signals (always SHADOW)",
            *([f"{prefix}abort reason: {self.abort_reason}"] if self.aborted else []),
            *([f"{prefix}failed gemi numbers: {self.failed_gemi_numbers[:20]}"] if self.failed_gemi_numbers else []),
        ]


def _exact_item(payload, number: str):
    """(item, problem): the one search item whose own arGemi is ``number``."""
    matches = [
        item for item in (payload.get("searchResults") or [])
        if isinstance(item, dict) and (normalize_gemi_number(item.get("arGemi")) or ("",))[0] == number
    ]
    if not matches:
        return None, NOT_FOUND
    if len(matches) > 1:
        return None, AMBIGUOUS
    return matches[0], ""


def _legacy_exposure(defaults: dict) -> tuple[bool, bool]:
    """(stored as today, clamped): what company_defaults will store, judged with its own clock."""
    stored = defaults["incorporation_date"]
    source = str(defaults["raw_data"].get("incorporationDate") or "")[:10]
    try:
        clamped = date.fromisoformat(source) != stored
    except ValueError:
        clamped = True
    return stored == date.today(), clamped


def _create_only(number: str, item: dict, defaults: dict):
    """(status, activity rows created). One savepoint: company and activities, or nothing."""
    from .services import sync_company_activities

    Company = apps.get_model("gemiapp", "Company")
    try:
        with transaction.atomic():
            if Company.objects.filter(gemi_number=number).exists():
                return RACE_SKIPPED, 0
            company = Company.objects.create(gemi_number=number, **defaults)
            counts = sync_company_activities(company, item.get("activities"))
    except IntegrityError:
        if Company.objects.filter(gemi_number=number).exists():
            return RACE_SKIPPED, 0      # another writer won the insert; its row is left exactly as it is
        raise
    return CREATED, int(getattr(counts, "created", 0) or 0)


def hydrate_pending_companies(
    *, limit: int = DEFAULT_LIMIT, dry_run: bool = False, pace_seconds: float = DEFAULT_PACE_SECONDS,
    client=None, sleep: Callable[[float], None] = time.sleep,
) -> HydrationReport:
    """Create the missing canonical Company rows for pending Discovery evidence. See the module docstring."""
    if not hydration_enabled():
        raise HydrationDisabled(
            "GEMI_DISCOVERY_PENDING_COMPANY_HYDRATION_ENABLED είναι απενεργοποιημένο: τίποτα δεν επιλέχθηκε, "
            "δεν ζητήθηκε από το ΓΕΜΗ και δεν γράφτηκε."
        )
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    if pace_seconds < 0:
        raise ValueError("pace_seconds must not be negative")
    from .services import company_defaults

    Company = apps.get_model("gemiapp", "Company")
    report = HydrationReport(dry_run=dry_run, limit=limit, pace_seconds=pace_seconds)
    queryset = pending_numbers_queryset()
    report.pending_before = queryset.count()
    report.pending_observations = _pending_observations().count()
    numbers = [row["gemi_number"] for row in queryset[:limit]]
    report.selected = len(numbers)
    fetched_before = False

    for number in numbers:
        if Company.objects.filter(gemi_number=number).exists():
            report.already_local += 1            # no request spent on a company that is already here
            continue
        if fetched_before and pace_seconds:
            sleep(pace_seconds)
        client = client or get_gemi_client()
        report.gemi_requests += 1
        fetched_before = True
        try:
            payload = client.search_companies({"arGemi": number}, lane=HYDRATION_LANE)
        except GemiResponseValidationError as exc:
            report.validation_failures += 1
            report.failed_gemi_numbers.append(number)
            logger.error("Pending hydration: GEMI %s failed %s validation (%s).", number, exc.family, exc.kind)
            continue
        except GemiApiError as exc:
            report.aborted, report.abort_reason = True, type(exc).__name__
            report.failed_gemi_numbers.append(number)
            logger.error("Pending hydration stopped at GEMI %s: %s.", number, type(exc).__name__)
            break
        report.fetched += 1
        item, problem = _exact_item(payload, number)
        if item is None:
            setattr(report, problem, getattr(report, problem) + 1)
            report.failed_gemi_numbers.append(number)
            logger.warning("Pending hydration: GEMI %s -> %s.", number, problem)
            continue
        try:
            defaults = company_defaults(item)
            as_today, clamped = _legacy_exposure(defaults)
            if dry_run:
                exists = Company.objects.filter(gemi_number=number).exists()
                status, activities = (RACE_SKIPPED if exists else WOULD_CREATE), 0
            else:
                status, activities = _create_only(number, item, defaults)
        except Exception as exc:  # the savepoint already rolled back: no partial row
            report.write_failures += 1
            report.failed_gemi_numbers.append(number)
            logger.error("Pending hydration: writing GEMI %s failed: %s.", number, type(exc).__name__)
            continue
        if status == RACE_SKIPPED:
            report.race_skipped += 1
            continue
        report.created += int(status == CREATED)
        report.would_create += int(status == WOULD_CREATE)
        report.activities_created += activities
        report.stored_as_today += int(as_today)
        report.date_clamped += int(clamped)

    report.pending_after = pending_numbers_queryset().count()
    logger.info(
        "Pending hydration%s: selected=%s requests=%s created=%s would_create=%s already_local=%s race=%s "
        "failed=%s aborted=%s pending_after=%s.",
        " (dry run)" if dry_run else "", report.selected, report.gemi_requests, report.created, report.would_create,
        report.already_local, report.race_skipped, report.failed, report.aborted, report.pending_after,
    )
    return report
