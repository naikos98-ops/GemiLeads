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
Signal exists, no digest changes. ``mode="ingest"`` is refused unless GEMI_DISCOVERY_V2_ENABLED is on; it is
off, and nothing schedules it.

Ingest: the page that was fetched is the company
------------------------------------------------
A ``/companies`` search item is the full company record, so ingest creates the ``Company`` **from the page it
just received** -- one request can create up to a page of companies, and no ``GET /companies?arGemi=<n>``
follows. The write is the shared ``company_writer.create_company_from_search_item``: create-only, one savepoint
per company (company and activities, or nothing), and date-safe. Per newly discovered record the outcome is
stored on its observation (``ingest_outcome``):

* ``created``          -- a valid record with no local row: created from the search payload;
* ``already_local``    -- the row is already stored (see "What counts as newly discovered"): **never written
  over**, so the legacy importer's data is never replaced by a discovery payload;
* ``quarantined_date`` -- classified ``invalid_date``, or the writer refused the payload's own date (missing,
  unreadable, before 1900 or in the future). No Company row: ``company_defaults`` would store such a date as
  *today* and put the company into today's legacy digest. The observation stays as pending evidence;
* ``write_failed``     -- an **unexpected** Company or activity persistence error for that one company. Its
  savepoint rolled back, so no partial row exists.

The first three are expected and never block: they do not change the classification, the frontier rule or the
cursor. A quarantined record advances the cursor exactly as a shadow run would, and is re-judged from its
payload on every later run that still pages past it.

``write_failed`` is different, because it is not a fact about the record but a failure of this run. The rest of
that page is still written (each company is its own savepoint, and every write is idempotent), then the run
**stops paging** (stop reason ``ingest_write_failed``), ends as ``failed`` and **does not advance the cursor**.
Companies already committed stay, and their evidence is stored with the failed run, so their signals can still be
materialised. The affected record is still above the unchanged frontier, so the next run retries it from the
payload it fetches then -- no operator step and no per-company lookup. Advancing past it would have left the
record below the frontier, reachable only while it stayed inside the overlap window.

``update_or_create`` and ``company_defaults``' clamping are never reached from here.

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
produce no duplicate observations, and ingest mode is create-only on the unique ``gemi_number``, so no
duplicate Company rows.

Persistence
-----------
The run row, its observations and the cursor are written in **one transaction**, with the cursor row locked,
so a frontier never advances without the evidence it was advanced on. Companies created by ingest commit per
record, before that: a crash in between leaves companies without observations and a cursor that did not move,
and the next run finds them above the unchanged frontier, already local, and records them then.

Compact observations (frequent runs)
------------------------------------
By default every examined record is stored, every run. A run every few minutes would store the same few
hundred ``known`` rows each time, so ``compact_observations=True`` stores an observation only when it says
something new: no observation of that GEMI number exists yet today (local day), or its latest one today
differs in classification, ingest outcome, source date, date quality or local existence. So the first
sighting, every change and one row per number per day are always kept; an unchanged repeat is counted
(``observations_suppressed``) and not stored. Counters, anomalies, the run row and the cursor are unaffected.

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

from . import company_writer
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
STOP_INGEST_WRITE_FAILED = "ingest_write_failed"

ORDERING_VIOLATION = "ordering_violation"
PAGE_BOUNDARY_VIOLATION = "page_boundary_violation"
INVALID_IDENTIFIER = "invalid_identifier"
DUPLICATE_RECORD = "duplicate_record"
BLOCKING_ANOMALIES = frozenset({ORDERING_VIOLATION, PAGE_BOUNDARY_VIOLATION})

# What ingest did with a newly discovered record (GemiDiscoveryObservation.ingest_outcome). "" = not ingested:
# a shadow or bootstrap run, or a ``known`` record.
INGEST_CREATED = "created"
INGEST_ALREADY_LOCAL = "already_local"
INGEST_QUARANTINED_DATE = "quarantined_date"
INGEST_WRITE_FAILED = "write_failed"
INGEST_WOULD_CREATE = "would_create"     # dry run only; never stored


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
    would_ingest_records: int = 0        # ingest dry run: valid records with no local row
    quarantined_date_records: int = 0    # ingest: no Company row, the date could not be stored as it is
    ingest_failed_records: int = 0       # ingest: an unexpected write error for that one company
    observations_stored: int = 0
    observations_suppressed: int = 0     # compact runs: unchanged repeats of today's evidence, not stored
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

    @property
    def discovered_gemi_numbers(self) -> list[str]:
        """Every identifier this run classified as newly discovered, stored or suppressed: the numbers a scoped
        materialisation of this run has to look at."""
        return [row["gemi_number"] for row in self.observations if row["classification"] != KNOWN]

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
            f"{prefix}ingest would_create={self.would_ingest_records} quarantined_date={self.quarantined_date_records} "
            f"write_failed={self.ingest_failed_records} observations stored={self.observations_stored} "
            f"suppressed={self.observations_suppressed}",
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


def _fetch_page(client, *, offset: int, policy: DiscoveryPolicy, max_wait: float | None = None) -> dict:
    # The lane's own default wait unless the caller bounds it (the frequent ingestion cycle does).
    bounded = {} if max_wait is None else {"max_wait": max_wait}
    with gemi_lane(GemiLane.DISCOVERY):
        return client.search_companies({
            "resultsSortBy": SORT_BY_GEMI_NUMBER_DESCENDING,
            "resultsOffset": offset,
            "resultsSize": policy.page_size,
        }, lane=GemiLane.DISCOVERY, **bounded)


def _scan(client, *, policy: DiscoveryPolicy, max_pages: int, frontier: int | None, as_of: date,
          result: DiscoveryResult, on_new=None, max_wait: float | None = None) -> None:
    """Page from the newest identifier downwards, measuring ordering and overlap. Raises nothing itself.

    ``on_new`` receives each page's newly discovered ``(item, gemi_number, classification)`` records while the
    payload is still in memory and returns ``{gemi_number: ingest outcome}``, stored on their observations.
    """
    offset = 0
    previous_number: int | None = None
    seen: set[str] = set()
    # How many records at or below the frontier the scan has reached. Counted, not a sticky "we are past the
    # frontier" flag: every newness decision below is made per record against ``frontier`` itself.
    at_or_below_frontier = 0
    for page_index in range(max_pages):
        payload = _fetch_page(client, offset=offset, policy=policy, max_wait=max_wait)
        items = payload.get("searchResults") or []
        if not items:
            result.stop_reason = STOP_END_OF_RESULTS
            return
        result.pages_fetched += 1
        page_new: list[tuple[dict, str, str]] = []
        page_rows: dict[str, dict] = {}
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
                    "ingest_outcome": "",
                })
                continue
            classification, value, quality = _classify(item, as_of=as_of)
            result.new_records += 1
            result.rediscovered_local_records += int(exists_locally)
            result.late_publication_records += int(classification == LATE_PUBLICATION)
            result.invalid_date_records += int(classification == INVALID_DATE)
            page_rows[text] = {
                "gemi_number": text, "classification": classification, "incorporation_date": value,
                "incorporation_date_quality": quality, "company_existed": exists_locally, "page_index": page_index,
                "ingest_outcome": "",
            }
            result.observations.append(page_rows[text])
            page_new.append((item, text, classification))

        if on_new is not None and page_new:
            for gemi_number, outcome in on_new(page_new).items():
                # A dry run's "would create" is a report, not a stored outcome.
                page_rows[gemi_number]["ingest_outcome"] = "" if outcome == INGEST_WOULD_CREATE else outcome
        if result.blocking_anomalies:
            result.stop_reason = STOP_ANOMALY
            return
        if result.ingest_failed_records:
            # An unexpected write failure: this page was finished, but no further page is fetched and the
            # frontier will not move, so the next run starts again from here.
            result.stop_reason = STOP_INGEST_WRITE_FAILED
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


def _ingest(records: list[tuple[dict, str, str]], *, dry_run: bool = False) -> dict[str, str]:
    """Create the newly discovered companies of one page from the payload already in memory. Returns the ingest
    outcome per GEMI number. See "Ingest" in the module docstring.

    No GEMI request is made here, and nothing is ever written over: a record above the frontier is newly
    discovered even when its ``Company`` row already exists, and that row stays exactly as it is.
    """
    Company = apps.get_model("gemiapp", "Company")
    numbers = [gemi_number for _, gemi_number, _ in records]
    already_local = set(Company.objects.filter(gemi_number__in=numbers).values_list("gemi_number", flat=True))
    outcomes: dict[str, str] = {}
    for item, gemi_number, classification in records:
        if gemi_number in already_local:
            outcomes[gemi_number] = INGEST_ALREADY_LOCAL
            continue
        if classification == INVALID_DATE:
            outcomes[gemi_number] = INGEST_QUARANTINED_DATE   # never handed to the writer at all
            continue
        try:
            written = company_writer.create_company_from_search_item(gemi_number, item, dry_run=dry_run)
        except Exception as exc:
            # Unexpected (the writer already turns a bad date and an insert race into outcomes). That company's
            # savepoint rolled back; the rest of this page is still written, and the run then fails without
            # advancing the frontier (see _scan and run_discovery), so the next run retries this record.
            outcomes[gemi_number] = INGEST_WRITE_FAILED
            logger.error("Discovery ingest: writing GEMI %s failed: %s. The frontier will not advance.",
                         gemi_number, type(exc).__name__)
            continue
        outcomes[gemi_number] = {
            company_writer.CREATED: INGEST_CREATED,
            company_writer.WOULD_CREATE: INGEST_WOULD_CREATE,
            company_writer.EXISTS: INGEST_ALREADY_LOCAL,
            company_writer.REFUSED_DATE: INGEST_QUARANTINED_DATE,
        }[written.status]
        if written.status == company_writer.REFUSED_DATE:
            logger.warning("Discovery ingest: GEMI %s quarantined: its incorporation date is %s.",
                           gemi_number, written.date_problem)
    return outcomes


def _count_ingest(result: DiscoveryResult, outcomes: dict[str, str]) -> dict[str, str]:
    values = list(outcomes.values())
    result.ingested_records += values.count(INGEST_CREATED)
    result.would_ingest_records += values.count(INGEST_WOULD_CREATE)
    result.quarantined_date_records += values.count(INGEST_QUARANTINED_DATE)
    result.ingest_failed_records += values.count(INGEST_WRITE_FAILED)
    return outcomes


OBSERVATION_FINGERPRINT = ("classification", "ingest_outcome", "incorporation_date", "incorporation_date_quality",
                           "company_existed")


def _fingerprint(row: dict) -> tuple:
    return tuple(row[name] for name in OBSERVATION_FINGERPRINT)


def _worth_storing(observations: list[dict], *, started_at) -> list[dict]:
    """The compact policy: drop an observation that only repeats that GEMI number's latest evidence of today."""
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    day_start = timezone.localtime(started_at).replace(hour=0, minute=0, second=0, microsecond=0)
    numbers = [row["gemi_number"] for row in observations]
    latest_today: dict[str, tuple] = {}
    for start in range(0, len(numbers), 500):
        rows = (GemiDiscoveryObservation.objects
                .filter(gemi_number__in=numbers[start:start + 500], run__started_at__gte=day_start)
                .order_by("id").values("gemi_number", *OBSERVATION_FINGERPRINT))
        for row in rows:
            latest_today[row["gemi_number"]] = _fingerprint(row)
    return [row for row in observations if latest_today.get(row["gemi_number"]) != _fingerprint(row)]


def _save_run(result: DiscoveryResult, cursor, *, stream: str, policy: DiscoveryPolicy, started_at,
              compact: bool = False) -> None:
    """Persist the run, its observations and the cursor. Callers wrap this in one transaction."""
    GemiDiscoveryRun = apps.get_model("gemiapp", "GemiDiscoveryRun")
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    to_store = _worth_storing(result.observations, started_at=started_at) if compact else result.observations
    result.observations_stored = len(to_store)
    result.observations_suppressed = len(result.observations) - len(to_store)
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
    if to_store:
        GemiDiscoveryObservation.objects.bulk_create([
            GemiDiscoveryObservation(run=run, **observation) for observation in to_store
        ], batch_size=500)
    cursor.last_run = run
    cursor.last_statistics = result.summary()
    cursor.save()


def run_discovery(*, mode: str = SHADOW, dry_run: bool = False, max_pages: int | None = None,
                  policy: DiscoveryPolicy | None = None, client=None, as_of: date | None = None,
                  stream: str = STREAM_COMPANIES, compact_observations: bool = False,
                  max_wait: float | None = None) -> DiscoveryResult:
    """One Discovery v2 run. See the module docstring. Never touches customer-facing data in shadow mode.

    ``compact_observations`` applies the compact policy (frequent runs); ``max_wait`` bounds how long each page
    request may wait for a slot in the shared rate budget (None: the lane's default).
    """
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
            with transaction.atomic():
                _save_run(result, cursor, stream=stream, policy=policy, started_at=started_at)
        return result

    # In ingest mode each page's newly discovered records are written from the payload that page returned.
    on_new = (lambda records: _count_ingest(result, _ingest(records, dry_run=dry_run))) if mode == INGEST else None
    try:
        _scan(client, policy=policy, max_pages=max_pages, frontier=cursor.high_water_mark_value, as_of=as_of,
              result=result, on_new=on_new, max_wait=max_wait)
    except Exception as exc:  # the client's own errors: budget timeout, transport, validation, HTTP
        result.status = "failed"
        result.error_message = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("Discovery v2 run failed after %s pages: %s", result.pages_fetched, type(exc).__name__)
    else:
        if result.blocking_anomalies:
            result.status = "anomaly"
        elif result.ingest_failed_records:
            # Never "success": only a successful run may move the frontier, and a record whose write failed
            # unexpectedly must stay above it.
            result.status = "failed"
            result.error_message = (f"{result.ingest_failed_records} company write(s) failed unexpectedly; "
                                    f"the frontier was not advanced and the next run retries them.")
        elif result.stop_reason == STOP_PAGE_LIMIT:
            result.status = "incomplete"
        else:
            result.status = "success"

    if dry_run:
        result.resulting_high_water_mark = cursor.high_water_mark
    else:
        # One transaction, the cursor row locked: the run, the evidence and the frontier move together, and
        # the frontier is compared with what is stored now, never with the copy read before the scan.
        with transaction.atomic():
            cursor = type(cursor).objects.select_for_update().get(pk=cursor.pk)
            result.resulting_high_water_mark = cursor.high_water_mark
            if result.status == "success" and result.highest_gemi_number:
                result.resulting_high_water_mark = max(
                    [result.highest_gemi_number, cursor.high_water_mark or "0"], key=lambda value: int(value or 0),
                )
                result.cursor_advanced = result.resulting_high_water_mark != cursor.high_water_mark
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
            _save_run(result, cursor, stream=stream, policy=policy, started_at=started_at,
                      compact=compact_observations)
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
        with transaction.atomic():
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
