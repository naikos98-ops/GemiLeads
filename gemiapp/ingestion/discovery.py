"""Discovery v2: finding newly published GEMI companies, including late publications (A10).

Why
---
The live importer asks GEMI for companies sorted by ``-incorporationDate`` and keeps the ones whose
``incorporationDate`` equals the target day. A company published today with an older incorporation date --
the capability spike saw 2026-09-09 and 2025-12-15 arriving among the newest registrations -- never equals
the target day, and the descending-date scan stops before reaching it. Those companies are missed for good.

Discovery v2 instead pages ``/companies`` sorted by ``-arGemi`` and looks for identifiers beyond the
frontier of what Gemi Leads has already observed. The API has no modified-since, change feed or cursor, and
``-arGemi`` ordering is **observed, not contractual** (a 30-row live sample), so every run measures the
ordering it actually receives and refuses to advance the cursor when the assumption breaks.

Shadow first
------------
A10 changes nothing customer-visible. The legacy importer remains the only source of companies, digests and
matching. ``mode="shadow"`` (the default) fetches, validates, classifies and records **only** into the
discovery tables: no Company or CompanyActivity row is created or changed, no monitoring row is touched, no
Signal exists, no digest changes. ``mode="ingest"`` persists newly discovered companies through the existing
importer path (``company_defaults`` + ``update_or_create`` + ``sync_company_activities``), and is refused
unless GEMI_DISCOVERY_V2_ENABLED is on; it is off, and nothing schedules it. Ingest **never writes over a
company that already exists locally**: a record can now be newly discovered while its row is already stored
(see "What counts as newly discovered"), and rewriting that row would let the future cutover path overwrite
legacy-imported data with a discovery payload. Such a record is skipped by the writer and keeps its
observation.

Data model
----------
``GemiDiscoveryCursor`` -- one row per stream, holding the high-water mark (the highest GEMI number already
observed and trusted), status, bootstrap provenance, run timestamps, failure count, anomaly state and the
last run's statistics. It is pipeline state: never stored on UserSubscription or any customer model.

``GemiDiscoveryRun`` -- one row per run with counts, the frontier before and after, the stop reason, the
anomalies and the policy used. ``GemiDiscoveryObservation`` -- one row per newly discovered company with its
identifier, source incorporation date, A3 date quality and classification. Identifiers, dates and counts
only; no persons, contact data or payloads.

``ImportRun`` is not reused: it is keyed by ``target_date`` and drives the Superadmin pipeline view and the
legacy error messages, and a shadow discovery run has no target date and must not appear as an import.

High-water mark
---------------
The highest GEMI number observed in a successful run, kept as text and as an integer for comparison. It is a
**discovery cursor only**: a higher GEMI number is not proof of a later incorporation date, and it is never
used as a business timestamp. The source incorporation date stays a company fact (A3/A6); when Gemi Leads
first saw a company stays ``Company.first_seen_at`` (A6).

Algorithm
---------
1. Require an initialised cursor (see Bootstrap); without one the run stops immediately rather than guessing.
2. Page ``/companies`` with ``resultsSortBy=-arGemi``, ``resultsSize=page_size`` through the shared
   GemiClient in the Discovery lane, from the newest identifier downwards.
3. For each record: normalise the identifier, detect duplicates, check ordering, compare the identifier with
   the run-start frontier, look up whether the company exists locally (by ``gemi_number``, never by
   incorporation date), and classify (see "What counts as newly discovered").
4. Stop when the overlap policy is satisfied: at least ``overlap_known_records`` already-known records seen
   **after** the first record at or below the frontier, and at least ``min_overlap_pages`` pages fetched. The
   overlap is what catches irregular ordering and late appearance; stopping at the first known record would
   not.
5. Stop early on the end of results, or on the page limit (``max_pages``), which marks the run incomplete.
6. Advance the cursor only when the run succeeded: a satisfied stop condition, no blocking anomaly, not a
   dry run and not a page-limited run.

Ordering guardrails
-------------------
Each run measures: identifiers that are not a positive integer; duplicates across pages; a record whose
identifier is greater than the previous one (within a page, or across a page boundary). Ordering violations
are **blocking**: the run is marked ``anomaly``, the cursor keeps its previous value and its status becomes
``anomaly`` for investigation. Invalid identifiers and duplicates are recorded and counted but do not block,
because one malformed record is not evidence that the whole ordering assumption failed.

What counts as newly discovered
-------------------------------
Newness is a fact about **Discovery's own frontier**, not about when another pipeline happened to write a
Company row:

* identifier **above the frontier captured at the start of the run** -> newly discovered, whatever the local
  ``Company`` table currently holds;
* identifier **at or below that frontier** -> the local-existence rule: already stored locally means
  ``known``, otherwise newly discovered (this is what still finds records the legacy importer never stored).

This removes a race, and the race was losing signals. Newness used to be decided purely by whether a
``Company`` row existed at the moment the page was scanned, so the very same GEMI record was recorded as
``known`` or as newly discovered depending on whether the legacy importer (scheduled seven times a day) had
already run. A record recorded as ``known`` is not eligible evidence for B2, and nothing keeps the payload, so
that first sighting was unrecoverable: silently, the NEW_COMPANY producer saw almost nothing.

The frontier makes the decision race-free because of how it is established and moved. Bootstrap writes the
highest identifier **that exists locally**, and a run advances it only to the highest identifier it actually
examined, only when the run succeeded. So every identifier at or below the frontier was either local at
bootstrap or examined by a successful run, and nothing above it can be an already-established company. The
comparison is made **per record** against that single run-start value -- never a sticky "we are past the
frontier now" flag -- so an out-of-order record is judged by its own identifier, and the ordering guardrails
above keep measuring the disorder itself.

``company_existed`` still records, unchanged, whether the row was already in ``Company`` when the page was
scanned. For an above-frontier record it is now **diagnostic evidence rather than the classification**:

    classification != ``known`` and ``company_existed`` is True

means "Discovery identified a newly seen identifier although the legacy importer had already stored the
company" -- exactly the sighting the old rule discarded. The run counts them as
``rediscovered_local_records``. Nothing hides or overwrites the fact.

Two things deliberately do **not** follow this rule, because they are about local data, not about newness:
the overlap stop condition and ``highest_known_gemi_number`` keep counting local existence (the bootstrap
frontier has to be an identifier that really is stored locally). ``known_records`` keeps counting the
observations classified ``known``, so ``examined = known + new`` still holds.

Late publications
-----------------
A newly discovered company is classified from its own source ``incorporationDate`` under A3 rules:
``late_publication`` when the date is valid and earlier than the run's local date, ``new_incorporation``
when it is the run's date or later, and ``invalid_date`` when the date is missing, unreadable or
out of range. A company is never skipped for having an old or unusable date -- that is the point of A10 --
and no date is ever clamped.

Duplicates and reruns
---------------------
Identifiers seen earlier in the same run are counted once. Overlapping pages, retries and reruns therefore
produce no duplicate observations, and ingest mode upserts on ``gemi_number``, so no duplicate Company rows.

Bootstrap
---------
The cursor is never inferred silently from ``MAX(local gemi_number)``: an unverified frontier could hide
every company above it forever. ``bootstrap_discovery_cursor`` pages from the newest identifier downwards
and requires ``bootstrap_known_confirmations`` records that already exist locally, with no blocking anomaly.
The frontier it writes is the highest identifier **that exists locally**, so every unknown record above it
stays discoverable, and the run records how many such records are already waiting. The cursor keeps the
method (``verified_scan``) and the time.

Comparison with the legacy discovery
------------------------------------
``compare_with_legacy`` puts the companies the legacy pipeline would call new for a day (Company rows whose
``incorporation_date`` is that day) beside what Discovery v2 saw in its runs that day. BOTH and LEGACY_ONLY
use everything the runs saw -- newly discovered records and the already-known ones they paged through --
because in shadow mode the legacy importer stores a company before v2 would, and "already known to v2" must
not read as a miss. LEGACY_ONLY therefore means Discovery v2 never reached that company, which is the miss
signal the shadow gate looks for. V2_ONLY uses only newly discovered records, each classified as
``late_publication``, ``invalid_incorporation_date`` or ``legacy_filter_miss``.

Still pending after this: a company with no local row
------------------------------------------------------
A newly discovered company that the legacy importer never stored has no ``Company`` row, and B1/B3 anchor
every signal and snapshot to one. B2 therefore leaves it as ``pending_no_company``: no signal, no placeholder
company, no import, with the observation as the durable pending evidence. That is unchanged and deliberate --
late publications are exactly this case, and they are a **measured G4 gap**, not something this module
resolves. Resolving it means creating ``Company`` rows outside the legacy importer, which is the gated
cutover decision.

Cutover gate
------------
Discovery v2 cannot replace the legacy discovery until: at least 14 days of shadow runs exist; the
legacy/v2 differences have been reviewed; no ordering anomaly remains unexplained; the request budget is
acceptable; and the missed/duplicate rates are acceptable. Nothing here enables that automatically.

Recovery
--------
A failed run leaves the cursor untouched and increments its failure count, so the next run repeats the same
window. A rate-budget timeout, a transport error or an invalid page ends the run as ``failed`` with the error
recorded. A page-limited run is ``incomplete`` and does not advance the frontier; raising ``max_pages`` or
running again continues from the same place. An ordering anomaly sets the cursor's status to ``anomaly``;
after investigation, ``bootstrap_discovery_cursor(force=True)`` re-verifies and rewrites the frontier. No
manual database editing is needed for any of these.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .client import get_gemi_client, gemi_lane
from .normalizer import normalize_event_date
from .rate_budget import GemiLane

logger = logging.getLogger(__name__)

STREAM_COMPANIES = "companies_by_gemi_number"
SORT_BY_GEMI_NUMBER_DESCENDING = "-arGemi"

SHADOW = "shadow"
INGEST = "ingest"
BOOTSTRAP = "bootstrap"

NEW_INCORPORATION = "new_incorporation"
LATE_PUBLICATION = "late_publication"
INVALID_DATE = "invalid_date"
# Recorded for a record at or below the run-start frontier that already exists locally, so the legacy
# comparison can tell a company Discovery v2 saw from one it never reached. Above the frontier a record is
# newly discovered even when it is already stored locally, and ``company_existed`` carries that fact instead.
# Bounded by the page limit, not by the size of the registry.
KNOWN = "known"

STOP_OVERLAP_SATISFIED = "overlap_satisfied"
STOP_END_OF_RESULTS = "end_of_results"
STOP_PAGE_LIMIT = "page_limit"
STOP_NO_CURSOR = "no_cursor"
STOP_ANOMALY = "ordering_anomaly"

ORDERING_VIOLATION = "ordering_violation"
PAGE_BOUNDARY_VIOLATION = "page_boundary_violation"
INVALID_IDENTIFIER = "invalid_identifier"
DUPLICATE_RECORD = "duplicate_record"
BLOCKING_ANOMALIES = frozenset({ORDERING_VIOLATION, PAGE_BOUNDARY_VIOLATION})


@dataclass(frozen=True)
class DiscoveryPolicy:
    """The one place for paging, overlap and safety limits."""

    page_size: int = 200
    max_pages: int = 10
    # Known records that must be seen beyond the frontier before the run may stop.
    overlap_known_records: int = 200
    min_overlap_pages: int = 1
    bootstrap_known_confirmations: int = 50
    bootstrap_max_pages: int = 10

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def policy_from_settings() -> DiscoveryPolicy:
    defaults = DiscoveryPolicy()
    return DiscoveryPolicy(
        page_size=int(getattr(settings, "GEMI_DISCOVERY_PAGE_SIZE", defaults.page_size)),
        max_pages=int(getattr(settings, "GEMI_DISCOVERY_MAX_PAGES", defaults.max_pages)),
        overlap_known_records=int(getattr(settings, "GEMI_DISCOVERY_OVERLAP_KNOWN_RECORDS", defaults.overlap_known_records)),
        min_overlap_pages=int(getattr(settings, "GEMI_DISCOVERY_MIN_OVERLAP_PAGES", defaults.min_overlap_pages)),
        bootstrap_known_confirmations=int(getattr(settings, "GEMI_DISCOVERY_BOOTSTRAP_CONFIRMATIONS", defaults.bootstrap_known_confirmations)),
        bootstrap_max_pages=int(getattr(settings, "GEMI_DISCOVERY_BOOTSTRAP_MAX_PAGES", defaults.bootstrap_max_pages)),
    )


def ingest_enabled() -> bool:
    return bool(getattr(settings, "GEMI_DISCOVERY_V2_ENABLED", False))


def shadow_only() -> bool:
    return bool(getattr(settings, "GEMI_DISCOVERY_V2_SHADOW", True))


def normalize_gemi_number(value: Any) -> tuple[str, int] | None:
    """The identifier as (text, integer), or None when it is not a positive integer identifier."""
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    return (text, number) if number > 0 else None


@dataclass
class DiscoveryResult:
    mode: str
    dry_run: bool
    status: str = "running"
    stop_reason: str = ""
    pages_fetched: int = 0
    records_examined: int = 0
    known_records: int = 0
    new_records: int = 0
    # Newly discovered above the frontier although the row was already stored locally: the sightings the old
    # local-existence rule silently dropped. A subset of new_records, never of known_records.
    rediscovered_local_records: int = 0
    late_publication_records: int = 0
    invalid_date_records: int = 0
    duplicate_records: int = 0
    invalid_identifier_records: int = 0
    ingested_records: int = 0
    overlap_known_records: int = 0
    highest_gemi_number: str = ""
    # The highest identifier seen that already exists locally: the only frontier a bootstrap may trust.
    highest_known_gemi_number: str = ""
    previous_high_water_mark: str = ""
    resulting_high_water_mark: str = ""
    cursor_advanced: bool = False
    anomalies: list = field(default_factory=list)
    error_message: str = ""
    run_id: int | None = None
    observations: list = field(default_factory=list)

    @property
    def blocking_anomalies(self) -> list:
        return [item for item in self.anomalies if item.get("kind") in BLOCKING_ANOMALIES]

    def summary(self) -> dict:
        data = {key: value for key, value in self.__dict__.items() if key != "observations"}
        data["blocking_anomalies"] = len(self.blocking_anomalies)
        return data

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        return [
            f"{prefix}mode={self.mode} status={self.status} stop_reason={self.stop_reason} pages={self.pages_fetched} "
            f"run_id={self.run_id}",
            f"{prefix}records examined={self.records_examined} known={self.known_records} new={self.new_records} "
            f"(already local={self.rediscovered_local_records}) late_publication={self.late_publication_records} "
            f"invalid_date={self.invalid_date_records} ingested={self.ingested_records}",
            f"{prefix}guardrails duplicates={self.duplicate_records} invalid_identifiers={self.invalid_identifier_records} "
            f"anomalies={len(self.anomalies)} blocking={len(self.blocking_anomalies)} overlap_known={self.overlap_known_records}",
            f"{prefix}frontier previous={self.previous_high_water_mark or 'none'} highest_seen={self.highest_gemi_number or 'none'} "
            f"resulting={self.resulting_high_water_mark or 'none'} advanced={self.cursor_advanced}",
            *([f"{prefix}error={self.error_message}"] if self.error_message else []),
        ]


def get_cursor(stream: str = STREAM_COMPANIES):
    GemiDiscoveryCursor = apps.get_model("gemiapp", "GemiDiscoveryCursor")
    cursor, _ = GemiDiscoveryCursor.objects.get_or_create(stream=stream)
    return cursor


def _known_numbers(numbers: list[str]) -> set[str]:
    Company = apps.get_model("gemiapp", "Company")
    return set(Company.objects.filter(gemi_number__in=numbers).values_list("gemi_number", flat=True))


def _classify(item: dict, *, as_of: date) -> tuple[str, Any, str]:
    normalized = normalize_event_date(item.get("incorporationDate"), as_of=as_of)
    if normalized.quality.value != "valid":
        return INVALID_DATE, None, normalized.quality.value
    if normalized.value < as_of:
        return LATE_PUBLICATION, normalized.value, normalized.quality.value
    return NEW_INCORPORATION, normalized.value, normalized.quality.value


def _fetch_page(client, *, offset: int, policy: DiscoveryPolicy) -> dict:
    with gemi_lane(GemiLane.DISCOVERY):
        return client.search_companies({
            "resultsSortBy": SORT_BY_GEMI_NUMBER_DESCENDING,
            "resultsOffset": offset,
            "resultsSize": policy.page_size,
        }, lane=GemiLane.DISCOVERY)


def _scan(client, *, policy: DiscoveryPolicy, max_pages: int, frontier: int | None, as_of: date,
          result: DiscoveryResult, on_new=None) -> None:
    """Page from the newest identifier downwards, measuring ordering and overlap. Raises nothing itself."""
    offset = 0
    previous_number: int | None = None
    seen: set[str] = set()
    # How many records at or below the frontier the scan has reached. Counted, not a sticky "we are past the
    # frontier" flag: every newness decision below is made per record against ``frontier`` itself.
    at_or_below_frontier = 0
    for page_index in range(max_pages):
        payload = _fetch_page(client, offset=offset, policy=policy)
        items = payload.get("searchResults") or []
        if not items:
            result.stop_reason = STOP_END_OF_RESULTS
            return
        result.pages_fetched += 1
        page_new: list[tuple[dict, str]] = []
        page_numbers: list[str] = []
        parsed: list[tuple[dict, str, int]] = []
        for item in items:
            identifier = normalize_gemi_number(item.get("arGemi"))
            if identifier is None:
                result.invalid_identifier_records += 1
                result.anomalies.append({"kind": INVALID_IDENTIFIER, "page": page_index})
                continue
            text, number = identifier
            if text in seen:
                result.duplicate_records += 1
                result.anomalies.append({"kind": DUPLICATE_RECORD, "page": page_index, "gemi_number": text})
                continue
            if previous_number is not None and number > previous_number:
                kind = PAGE_BOUNDARY_VIOLATION if not parsed else ORDERING_VIOLATION
                result.anomalies.append({"kind": kind, "page": page_index, "gemi_number": text, "previous": str(previous_number)})
            previous_number = number
            seen.add(text)
            parsed.append((item, text, number))
            page_numbers.append(text)

        known = _known_numbers(page_numbers)
        for item, text, number in parsed:
            result.records_examined += 1
            if not result.highest_gemi_number or number > int(result.highest_gemi_number):
                result.highest_gemi_number = text
            # Per record, against the frontier as it was when the run started. Both are False during a
            # bootstrap (no frontier yet), which leaves that scan's semantics exactly as they were.
            above_frontier = frontier is not None and number > frontier
            within_frontier = frontier is not None and number <= frontier
            exists_locally = text in known
            if within_frontier:
                at_or_below_frontier += 1
            if exists_locally:
                # Local existence, not newness: the bootstrap frontier must be an identifier really stored here.
                if not result.highest_known_gemi_number or number > int(result.highest_known_gemi_number):
                    result.highest_known_gemi_number = text
                if within_frontier:
                    result.overlap_known_records += 1
            if exists_locally and not above_frontier:
                result.known_records += 1
                _, known_value, known_quality = _classify(item, as_of=as_of)
                result.observations.append({
                    "gemi_number": text, "classification": KNOWN, "incorporation_date": known_value,
                    "incorporation_date_quality": known_quality, "company_existed": True, "page_index": page_index,
                })
                continue
            classification, value, quality = _classify(item, as_of=as_of)
            result.new_records += 1
            result.rediscovered_local_records += int(exists_locally)
            result.late_publication_records += int(classification == LATE_PUBLICATION)
            result.invalid_date_records += int(classification == INVALID_DATE)
            result.observations.append({
                "gemi_number": text, "classification": classification, "incorporation_date": value,
                "incorporation_date_quality": quality, "company_existed": exists_locally, "page_index": page_index,
            })
            page_new.append((item, text))

        if on_new is not None and page_new:
            on_new(page_new)
        if result.blocking_anomalies:
            result.stop_reason = STOP_ANOMALY
            return
        if frontier is not None and at_or_below_frontier and result.overlap_known_records >= policy.overlap_known_records \
                and result.pages_fetched >= policy.min_overlap_pages:
            result.stop_reason = STOP_OVERLAP_SATISFIED
            return
        offset += len(items)
        total = int((payload.get("searchMetadata") or {}).get("totalCount") or 0)
        if len(items) < policy.page_size or (total and offset >= total):
            result.stop_reason = STOP_END_OF_RESULTS
            return
    result.stop_reason = STOP_PAGE_LIMIT


def _ingest(records: list[tuple[dict, str]]) -> int:
    """Persist newly discovered companies through the existing importer path, creating only.

    A record above the frontier is newly discovered even when its ``Company`` row already exists, so this
    writer -- the one place discovery touches customer-facing data -- refuses to write over a row that is
    already stored, whatever the caller passes. Rewriting it would replace legacy-imported data with a
    discovery payload the moment the cutover path is switched on; skipping it costs nothing, because the
    observation is already recorded either way.
    """
    from ..services import company_defaults, sync_company_activities

    Company = apps.get_model("gemiapp", "Company")
    numbers = [gemi_number for _, gemi_number in records]
    already_local = set(Company.objects.filter(gemi_number__in=numbers).values_list("gemi_number", flat=True))
    ingested = 0
    with transaction.atomic():
        for item, gemi_number in records:
            if gemi_number in already_local:
                logger.info("Discovery ingest kept the stored company %s: never written over.", gemi_number)
                continue
            company, _ = Company.objects.update_or_create(gemi_number=gemi_number, defaults=company_defaults(item))
            sync_company_activities(company, item.get("activities"))
            ingested += 1
    return ingested


def _save_run(result: DiscoveryResult, cursor, *, stream: str, policy: DiscoveryPolicy, started_at) -> None:
    GemiDiscoveryRun = apps.get_model("gemiapp", "GemiDiscoveryRun")
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    run = GemiDiscoveryRun.objects.create(
        stream=stream, mode=result.mode, status=result.status, finished_at=timezone.now(),
        pages_fetched=result.pages_fetched, records_examined=result.records_examined, known_records=result.known_records,
        new_records=result.new_records, late_publication_records=result.late_publication_records,
        invalid_date_records=result.invalid_date_records, duplicate_records=result.duplicate_records,
        invalid_identifier_records=result.invalid_identifier_records, ingested_records=result.ingested_records,
        highest_gemi_number=result.highest_gemi_number, previous_high_water_mark=result.previous_high_water_mark,
        resulting_high_water_mark=result.resulting_high_water_mark, cursor_advanced=result.cursor_advanced,
        overlap_known_records=result.overlap_known_records, stop_reason=result.stop_reason, anomalies=result.anomalies,
        policy=policy.as_dict(), error_message=result.error_message,
    )
    GemiDiscoveryRun.objects.filter(pk=run.pk).update(started_at=started_at)
    result.run_id = run.pk
    if result.observations:
        GemiDiscoveryObservation.objects.bulk_create([
            GemiDiscoveryObservation(run=run, **observation) for observation in result.observations
        ], batch_size=500)
    cursor.last_run = run
    cursor.last_statistics = result.summary()
    cursor.save()


def run_discovery(*, mode: str = SHADOW, dry_run: bool = False, max_pages: int | None = None,
                  policy: DiscoveryPolicy | None = None, client=None, as_of: date | None = None,
                  stream: str = STREAM_COMPANIES) -> DiscoveryResult:
    """One Discovery v2 run. See the module docstring. Never touches customer-facing data in shadow mode."""
    if mode not in (SHADOW, INGEST):
        raise ValueError(f"unsupported discovery mode: {mode}")
    if mode == INGEST and not ingest_enabled():
        raise ValueError("Discovery v2 ingest mode requires GEMI_DISCOVERY_V2_ENABLED; shadow mode is the default.")
    policy = policy or policy_from_settings()
    max_pages = policy.max_pages if max_pages is None else max_pages
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    as_of = timezone.localdate() if as_of is None else as_of
    client = client or get_gemi_client()
    cursor = get_cursor(stream)
    started_at = timezone.now()
    result = DiscoveryResult(mode=mode, dry_run=dry_run, previous_high_water_mark=cursor.high_water_mark)

    if cursor.high_water_mark_value is None:
        result.status, result.stop_reason = "failed", STOP_NO_CURSOR
        result.error_message = "Ο cursor δεν έχει αρχικοποιηθεί· τρέξε πρώτα bootstrap_gemi_discovery_v2."
        if not dry_run:
            _save_run(result, cursor, stream=stream, policy=policy, started_at=started_at)
        return result

    on_new = (lambda records: setattr(result, "ingested_records", result.ingested_records + _ingest(records))) \
        if (mode == INGEST and not dry_run) else None
    try:
        _scan(client, policy=policy, max_pages=max_pages, frontier=cursor.high_water_mark_value, as_of=as_of,
              result=result, on_new=on_new)
    except Exception as exc:  # the client's own errors: budget timeout, transport, validation, HTTP
        result.status = "failed"
        result.error_message = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("Discovery v2 run failed after %s pages: %s", result.pages_fetched, type(exc).__name__)
    else:
        if result.blocking_anomalies:
            result.status = "anomaly"
        elif result.stop_reason == STOP_PAGE_LIMIT:
            result.status = "incomplete"
        else:
            result.status = "success"

    advance = result.status == "success" and not dry_run
    if advance and result.highest_gemi_number:
        result.resulting_high_water_mark = max(
            [result.highest_gemi_number, cursor.high_water_mark or "0"], key=lambda value: int(value or 0),
        )
        result.cursor_advanced = result.resulting_high_water_mark != cursor.high_water_mark
    else:
        result.resulting_high_water_mark = cursor.high_water_mark

    if not dry_run:
        cursor.last_attempted_at = started_at
        if result.status == "success":
            cursor.last_success_at = timezone.now()
            cursor.consecutive_failures = 0
            cursor.high_water_mark = result.resulting_high_water_mark
            cursor.high_water_mark_value = int(result.resulting_high_water_mark or 0) or None
        elif result.status == "failed":
            cursor.consecutive_failures += 1
        if result.blocking_anomalies:
            cursor.status = "anomaly"
            cursor.anomaly_reason = result.blocking_anomalies[0]["kind"]
            cursor.anomaly_detected_at = timezone.now()
        _save_run(result, cursor, stream=stream, policy=policy, started_at=started_at)
    logger.info(
        "Discovery v2 %s run: status=%s pages=%s examined=%s new=%s late=%s cursor_advanced=%s",
        mode, result.status, result.pages_fetched, result.records_examined, result.new_records,
        result.late_publication_records, result.cursor_advanced,
    )
    return result


def bootstrap_discovery_cursor(*, policy: DiscoveryPolicy | None = None, client=None, dry_run: bool = False,
                               force: bool = False, as_of: date | None = None,
                               stream: str = STREAM_COMPANIES) -> DiscoveryResult:
    """Establish the initial trusted frontier by a verified scan. See the module docstring."""
    policy = policy or policy_from_settings()
    as_of = timezone.localdate() if as_of is None else as_of
    client = client or get_gemi_client()
    cursor = get_cursor(stream)
    if cursor.high_water_mark_value is not None and not force:
        raise ValueError("Ο cursor έχει ήδη αρχικοποιηθεί· χρειάζεται ρητό force για επανα-αρχικοποίηση.")
    started_at = timezone.now()
    result = DiscoveryResult(mode=BOOTSTRAP, dry_run=dry_run, previous_high_water_mark=cursor.high_water_mark)
    try:
        _scan(client, policy=policy, max_pages=policy.bootstrap_max_pages, frontier=None, as_of=as_of, result=result)
    except Exception as exc:
        result.status = "failed"
        result.error_message = f"{type(exc).__name__}: {exc}"[:300]
    else:
        confirmed = result.known_records
        if result.blocking_anomalies:
            result.status = "anomaly"
        elif confirmed < policy.bootstrap_known_confirmations:
            result.status = "incomplete"
            result.error_message = (
                f"Μόνο {confirmed} γνωστές εγγραφές επιβεβαιώθηκαν (απαιτούνται {policy.bootstrap_known_confirmations})."
            )
        elif not result.highest_known_gemi_number:
            result.status = "incomplete"
            result.error_message = "Καμία γνωστή εγγραφή δεν βρέθηκε: δεν υπάρχει σύνορο που να εγγυάται πληρότητα."
        else:
            result.status = "success"
            # The highest identifier that already exists locally. Everything above it -- reported as
            # new_records, the backlog waiting to be discovered -- stays discoverable by the first run.
            result.resulting_high_water_mark = result.highest_known_gemi_number
    if not dry_run:
        cursor.last_attempted_at = started_at
        if result.status == "success":
            cursor.status = "ready"
            cursor.bootstrap_method = "verified_scan"
            cursor.bootstrapped_at = timezone.now()
            cursor.last_success_at = timezone.now()
            cursor.anomaly_reason = ""
            cursor.anomaly_detected_at = None
            cursor.consecutive_failures = 0
            cursor.high_water_mark = result.resulting_high_water_mark
            cursor.high_water_mark_value = int(result.resulting_high_water_mark or 0) or None
            result.cursor_advanced = True
        _save_run(result, cursor, stream=stream, policy=policy, started_at=started_at)
    return result


# --- legacy comparison ---------------------------------------------------------------------------

BOTH = "both"
LEGACY_ONLY = "legacy_only"
V2_ONLY = "v2_only"


@dataclass
class ComparisonReport:
    target_date: date
    legacy: int = 0
    v2: int = 0
    v2_seen: int = 0
    both: int = 0
    legacy_only: list = field(default_factory=list)
    v2_only: list = field(default_factory=list)
    v2_only_reasons: dict = field(default_factory=dict)
    runs: int = 0

    def lines(self) -> list[str]:
        reasons = " ".join(f"{key}={value}" for key, value in sorted(self.v2_only_reasons.items())) or "none"
        return [
            f"comparison for {self.target_date.isoformat()} runs={self.runs} legacy={self.legacy} "
            f"v2_discovered={self.v2} v2_seen={self.v2_seen} both={self.both} legacy_only={len(self.legacy_only)} "
            f"v2_only={len(self.v2_only)}",
            f"v2_only reasons: {reasons}",
        ]


def compare_with_legacy(target_date: date, *, stream: str = STREAM_COMPANIES) -> ComparisonReport:
    """What the legacy pipeline would call new that day, beside what Discovery v2 observed. Read-only."""
    Company = apps.get_model("gemiapp", "Company")
    GemiDiscoveryRun = apps.get_model("gemiapp", "GemiDiscoveryRun")
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    runs = GemiDiscoveryRun.objects.filter(stream=stream, started_at__date=target_date).exclude(mode=BOOTSTRAP)
    rows = list(GemiDiscoveryObservation.objects.filter(run__in=runs).values("gemi_number", "classification"))
    # Everything the runs saw, including records already stored locally, and the newly discovered subset.
    seen = {row["gemi_number"] for row in rows}
    discovered = {row["gemi_number"]: row["classification"] for row in rows if row["classification"] != KNOWN}
    legacy = set(Company.objects.filter(incorporation_date=target_date).values_list("gemi_number", flat=True))
    report = ComparisonReport(
        target_date=target_date, legacy=len(legacy), v2=len(discovered), v2_seen=len(seen), runs=runs.count(),
    )
    report.both = len(legacy & seen)
    # Legacy called it new and Discovery v2 never reached it: the miss signal the shadow gate looks for.
    report.legacy_only = sorted(legacy - seen)
    report.v2_only = sorted(set(discovered) - legacy)
    reasons: dict[str, int] = {}
    for gemi_number in report.v2_only:
        classification = discovered[gemi_number]
        reason = {
            LATE_PUBLICATION: "late_publication",
            INVALID_DATE: "invalid_incorporation_date",
            NEW_INCORPORATION: "legacy_filter_miss",
        }.get(classification, "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
    report.v2_only_reasons = reasons
    return report
