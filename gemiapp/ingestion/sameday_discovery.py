"""The same-day discovery lane: companies by incorporation date, complementing the ``-arGemi`` frontier lane.

Why a second lane
-----------------
Discovery v2 (``gemiapp.ingestion.discovery``) pages ``/companies`` by ``-arGemi`` and stops once it has seen
enough known records at or below its frontier. That finds what is published with a *new, high* GEMI number. It
cannot find a company whose GEMI number is far below the frontier but whose ``incorporationDate`` is today:
no run ever pages down to it. Production parity for 2026-10-08 showed exactly that -- seven companies dated that
day, stored by the legacy importer, with no discovery observation of any kind.

The legacy importer finds them because it pages by ``-incorporationDate``. This lane does the same scan, but
through the unified path: the shared create-only writer, discovery evidence, SHADOW signals. It does **not**
reuse the legacy write path (``update_or_create`` and the date clamp are never reached from here), and it
changes nothing about the frontier lane or the legacy importer.

**Dormant.** ``GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED`` is off by default; the lane refuses without it (and without
``GEMI_DISCOVERY_V2_ENABLED``, because it creates Company rows), dry runs included -- a dry run still spends
GEMI requests.

The query
---------
Two passes, exactly the legacy importer's query, through the shared GemiClient in the DISCOVERY lane::

    GET /companies?isActive=true |false &resultsSortBy=-incorporationDate&resultsOffset=N&resultsSize=200

The passes stay separate on purpose: it is the query the legacy importer proves every day, so a remaining
legacy-only company would be a logic difference, not a query difference. A search item is the full company
record, so no ``GET /companies?arGemi=<n>`` ever follows.

The window
----------
``[as_of - lookback_days, as_of]`` with ``as_of`` the application's local date (Europe/Athens), fixed once by
the caller, and ``lookback_days = 1``: yesterday is included because the legacy daily run imports yesterday's
date, and a company published late in the evening would otherwise be legacy-only. ``incorporationDate`` is a
plain date and is compared as one; nothing is converted between time zones.

Each record
-----------
* a readable date inside the window -> a **candidate** (see "Newness");
* a date later than ``as_of``, missing, unreadable or before 1900 -> **never written, never a stopping
  boundary, no observation**. The descending order puts GEMI's impossible dates (``9011-12-09`` ...) at the very
  top of every scan; recording them would add the same junk rows on every cycle. They are counted
  (``future_date_records``, ``unusable_date_records``) so they stay measurable;
* a readable date older than the window -> a **boundary** record: counted, never written. The page that
  contains the boundary is full of established companies, and none of them is a finding.

Stopping (per pass)
-------------------
* ``older_boundary`` -- at least ``older_boundary_records`` boundary records were seen in the pass **and** the
  last readable date of the page is older than the window. Stricter than the legacy importer's "any older
  record on the page", so one stray misordered record cannot end the scan early;
* ``end_of_results`` -- an empty or short page, or the reported total reached: success;
* ``page_limit`` -- ``max_pages`` requests in one pass: the run is ``incomplete`` (the hard safety limit).

Order guardrail
---------------
``-incorporationDate`` ordering is observed, not contractual. A candidate that appears after the boundary is
established (``older_boundary_records`` older records already seen in that pass) is a **date-order anomaly**:
recorded, the run ends as ``anomaly`` after that page, and the operators are alerted. The record itself is
still processed -- the write is create-only and idempotent. A candidate after fewer older records is tolerated
and counted (``tolerated_order_irregularities``).

Newness
-------
There is no frontier here, so "already stored locally" cannot mean "known": the legacy importer may simply have
stored the company first, and a record recorded as known is not eligible evidence for the NEW_COMPANY producer.
For a candidate:

* no local ``Company`` -> newly discovered, handed to the shared writer (``created`` / ``quarantined_date`` /
  ``write_failed``, as in the frontier lane);
* a local ``Company`` that already has eligible discovery evidence in any stream -> ``known``;
* a local ``Company`` with no such evidence, **first stored inside the window** (the local date of
  ``Company.imported_at`` -- ``auto_now_add``, never moved -- is in ``[as_of - lookback_days, as_of]``) -> newly
  discovered, outcome ``already_local``. Genuine evidence of a race the other writer won: never written over,
  and its signal is materialised;
* a local ``Company`` with no such evidence that was **stored before the window** -> ``known``, counted as
  ``historical_existing_records``. It is an established company whose ``incorporationDate`` GEMI later changed
  or corrected into the window, not a new one: no eligible evidence is recorded, so no NEW_COMPANY signal can
  follow, and the row is not touched.

One Company (unique ``gemi_number``, create-only), one NEW_COMPANY signal (its dedupe key is per company) and
therefore one pass through the opportunity pipeline, whichever lane saw the record first.

Stateless
---------
No cursor and no high-water mark: every run rescans its window from the top. A failed run loses nothing, there
is nothing to advance or roll back, and the next run repeats the scan. Records that shift between two page
requests show up as duplicates (counted, not anomalous) or are picked up by the next run.

Persistence
-----------
The same tables as the frontier lane, under its own stream ``companies_by_incorporation_date``: one
``GemiDiscoveryRun`` per run and one ``GemiDiscoveryObservation`` per candidate, compacted **per stream**. No
schema change: the window, the trigger and the lane's diagnostic counters are stored in the run's ``policy``
JSON (``as_of``, ``window_start``, ``trigger``, ``statistics``). ``trigger`` says who started the run -- the
scheduled cycle, an operator, or (reserved; not implemented here) an operator backfill, which the parity report
never accepts as timely evidence.

Unexpected write failure
------------------------
As in the frontier lane, the rest of that page is still written, paging stops and the run ends ``failed``. With
no cursor there is nothing to hold back; the next run meets the record again in the same window.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .client import get_gemi_client, gemi_lane
from .discovery import (
    INGEST,
    INGEST_WOULD_CREATE,
    INVALID_IDENTIFIER,
    KNOWN,
    LATE_PUBLICATION,
    STOP_END_OF_RESULTS,
    STOP_INGEST_WRITE_FAILED,
    STOP_PAGE_LIMIT,
    DiscoveryResult,
    _classify,
    _count_ingest,
    _ingest,
    _worth_storing,
    ingest_enabled,
    normalize_gemi_number,
)
from .rate_budget import GemiLane

logger = logging.getLogger(__name__)

STREAM_INCORPORATION_DATE = "companies_by_incorporation_date"
SORT_BY_INCORPORATION_DATE_DESCENDING = "-incorporationDate"
# (name, the isActive value sent). Separate passes: see "The query".
PASSES = (("active", "true"), ("inactive", "false"))

STOP_OLDER_BOUNDARY = "older_boundary"
STOP_DATE_ORDER_ANOMALY = "date_order_anomaly"

DATE_ORDER_VIOLATION = "date_order_violation"
MAX_RECORDED_ANOMALIES = 20

# Who started a run (GemiDiscoveryRun.policy["trigger"]).
TRIGGER_CYCLE = "cycle"
TRIGGER_OPERATOR = "operator"
TRIGGER_BACKFILL = "operator_backfill"   # reserved: parity never counts it as timely evidence

IN_WINDOW = "in_window"
FUTURE = "future"
UNUSABLE = "unusable"
OLDER = "older"

LANE_FLAG_OFF = ("GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED is off: the same-day discovery lane is dormant and only "
                 "runs when it is explicitly enabled")
INGEST_FLAG_OFF = ("GEMI_DISCOVERY_V2_ENABLED is off: the same-day lane creates Company rows and only runs when "
                   "company ingest is explicitly enabled")


class SameDayLaneRefused(RuntimeError):
    """A flag is off. Nothing was requested and nothing was written."""


@dataclass(frozen=True)
class SameDayPolicy:
    """The one place for the lane's paging, window and safety limits."""

    page_size: int = 200
    max_pages: int = 10              # per pass: the hard safety limit
    lookback_days: int = 1           # the window is [as_of - lookback_days, as_of]
    # Older-than-window records a pass must have seen before it may stop.
    older_boundary_records: int = 20

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def policy_from_settings() -> SameDayPolicy:
    defaults = SameDayPolicy()
    return SameDayPolicy(
        page_size=int(getattr(settings, "GEMI_DISCOVERY_PAGE_SIZE", defaults.page_size)),
        max_pages=int(getattr(settings, "GEMI_DISCOVERY_SAMEDAY_MAX_PAGES", defaults.max_pages)),
        lookback_days=int(getattr(settings, "GEMI_DISCOVERY_SAMEDAY_LOOKBACK_DAYS", defaults.lookback_days)),
        older_boundary_records=int(getattr(
            settings, "GEMI_DISCOVERY_SAMEDAY_OLDER_BOUNDARY_RECORDS", defaults.older_boundary_records)),
    )


def sameday_enabled() -> bool:
    return bool(getattr(settings, "GEMI_DISCOVERY_SAMEDAY_LANE_ENABLED", False))


@dataclass
class SameDayResult(DiscoveryResult):
    """One same-day run. The shared counters keep their meaning; ``pages_fetched`` counts every request."""

    stream: str = STREAM_INCORPORATION_DATE
    trigger: str = TRIGGER_OPERATOR
    as_of: date | None = None
    window_start: date | None = None
    future_date_records: int = 0             # dated after as_of: never written, never a boundary
    unusable_date_records: int = 0           # missing, unreadable or before 1900: likewise
    older_boundary_records: int = 0          # readable and older than the window: boundary only
    tolerated_order_irregularities: int = 0  # a candidate after a few older records, below the threshold
    # Stored before the window, no discovery evidence, dated into the window upstream: known, never a finding.
    historical_existing_records: int = 0
    date_order_anomalies: int = 0
    passes: list = field(default_factory=list)   # {"pass", "pages", "stop_reason"} per isActive pass

    @property
    def blocking_anomalies(self) -> list:
        return [item for item in self.anomalies if item.get("kind") == DATE_ORDER_VIOLATION]

    def statistics(self) -> dict:
        """The lane's diagnostic counters, as stored on the run row."""
        return {
            "future_date_records": self.future_date_records, "unusable_date_records": self.unusable_date_records,
            "older_boundary_records": self.older_boundary_records,
            "tolerated_order_irregularities": self.tolerated_order_irregularities,
            "date_order_anomalies": self.date_order_anomalies, "already_local_records": self.rediscovered_local_records,
            "historical_existing_records": self.historical_existing_records,
            "quarantined_date_records": self.quarantined_date_records,
            "ingest_failed_records": self.ingest_failed_records, "passes": [dict(item) for item in self.passes],
        }

    def summary(self) -> dict:
        data = super().summary()
        for name in ("as_of", "window_start"):
            data[name] = data[name].isoformat() if data[name] else ""
        return data

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        passes = " ".join(f"{item['pass']}={item['pages']}p/{item['stop_reason'] or 'unfinished'}" for item in self.passes)
        return [
            f"{prefix}stream={self.stream} status={self.status} stop_reason={self.stop_reason} "
            f"requests={self.pages_fetched} run_id={self.run_id} trigger={self.trigger}",
            f"{prefix}window={self.window_start} .. {self.as_of} (local dates) passes: {passes or 'none'}",
            f"{prefix}in-window records examined={self.records_examined} known={self.known_records} "
            f"(stored before the window, never a finding={self.historical_existing_records}) "
            f"new={self.new_records} (already local, first evidence={self.rediscovered_local_records}) "
            f"dated before today={self.late_publication_records} ingested={self.ingested_records}",
            f"{prefix}ingest would_create={self.would_ingest_records} quarantined_date={self.quarantined_date_records} "
            f"write_failed={self.ingest_failed_records} observations stored={self.observations_stored} "
            f"suppressed={self.observations_suppressed}",
            f"{prefix}skipped, never written, no observation: future-dated={self.future_date_records} "
            f"missing/unreadable/pre-1900={self.unusable_date_records} older boundary={self.older_boundary_records}",
            f"{prefix}guardrails duplicates={self.duplicate_records} invalid_identifiers={self.invalid_identifier_records} "
            f"date_order_anomalies={self.date_order_anomalies} tolerated_irregularities="
            f"{self.tolerated_order_irregularities}",
            *([f"{prefix}error={self.error_message}"] if self.error_message else []),
        ]


def triage(item: dict, *, as_of: date, window_start: date) -> str:
    """Where one record's own ``incorporationDate`` puts it relative to the window. See "Each record"."""
    raw = str(item.get("incorporationDate") or "")[:10]
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        return UNUSABLE          # missing or unreadable
    if value > as_of:
        return FUTURE
    if value.year < 1900:
        return UNUSABLE
    return IN_WINDOW if value >= window_start else OLDER


def _fetch_page(client, *, is_active: str, offset: int, policy: SameDayPolicy, max_wait: float | None) -> dict:
    bounded = {} if max_wait is None else {"max_wait": max_wait}
    with gemi_lane(GemiLane.DISCOVERY):
        return client.search_companies({
            "isActive": is_active,
            "resultsSortBy": SORT_BY_INCORPORATION_DATE_DESCENDING,
            "resultsOffset": offset,
            "resultsSize": policy.page_size,
        }, lane=GemiLane.DISCOVERY, **bounded)


def _stored_on(numbers: list[str]) -> dict[str, date]:
    """``{gemi_number: the local date its Company row was first stored}`` for the numbers stored locally."""
    Company = apps.get_model("gemiapp", "Company")
    return {number: timezone.localdate(imported_at) for number, imported_at in
            Company.objects.filter(gemi_number__in=numbers).values_list("gemi_number", "imported_at")}


def _evidenced_numbers(numbers: list[str]) -> set[str]:
    """The numbers that already have eligible (newly-discovered) evidence, from any stream and any run."""
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    return set(GemiDiscoveryObservation.objects.filter(gemi_number__in=numbers).exclude(classification=KNOWN)
               .values_list("gemi_number", flat=True).distinct())


def _scan(client, *, policy: SameDayPolicy, max_pages: int, result: SameDayResult, dry_run: bool,
          max_wait: float | None) -> None:
    """Both passes over the window. Sets ``result.stop_reason``; lets the client's own errors propagate."""
    as_of, window_start = result.as_of, result.window_start
    seen: set[str] = set()
    page_index = 0
    for name, is_active in PASSES:
        progress = {"pass": name, "pages": 0, "stop_reason": ""}
        result.passes.append(progress)
        offset = older_seen = 0
        while True:
            if progress["pages"] >= max_pages:
                progress["stop_reason"] = STOP_PAGE_LIMIT
                break
            payload = _fetch_page(client, is_active=is_active, offset=offset, policy=policy, max_wait=max_wait)
            result.pages_fetched += 1
            progress["pages"] += 1
            items = payload.get("searchResults") or []
            if not items:
                progress["stop_reason"] = STOP_END_OF_RESULTS
                break

            candidates: list[tuple[dict, str]] = []
            last_readable_is_older = False
            for item in items:
                kind = triage(item, as_of=as_of, window_start=window_start)
                if kind == FUTURE:
                    result.future_date_records += 1
                    continue
                if kind == UNUSABLE:
                    result.unusable_date_records += 1
                    continue
                if kind == OLDER:
                    older_seen += 1
                    result.older_boundary_records += 1
                    last_readable_is_older = True
                    continue
                last_readable_is_older = False
                identifier = normalize_gemi_number(item.get("arGemi"))
                if identifier is None:
                    result.invalid_identifier_records += 1
                    result.anomalies.append({"kind": INVALID_IDENTIFIER, "page": page_index, "pass": name})
                    continue
                text = identifier[0]
                if older_seen >= policy.older_boundary_records:
                    result.date_order_anomalies += 1
                    if len(result.blocking_anomalies) < MAX_RECORDED_ANOMALIES:
                        result.anomalies.append({"kind": DATE_ORDER_VIOLATION, "page": page_index, "pass": name,
                                                 "gemi_number": text, "older_records_before": older_seen})
                elif older_seen:
                    result.tolerated_order_irregularities += 1
                if text in seen:
                    result.duplicate_records += 1     # paging drift between two requests, or the other pass
                    continue
                seen.add(text)
                candidates.append((item, text))

            _examine(candidates, result=result, page_index=page_index, dry_run=dry_run)
            page_index += 1

            if result.blocking_anomalies:
                progress["stop_reason"] = result.stop_reason = STOP_DATE_ORDER_ANOMALY
                return
            if result.ingest_failed_records:
                progress["stop_reason"] = result.stop_reason = STOP_INGEST_WRITE_FAILED
                return
            if older_seen >= policy.older_boundary_records and last_readable_is_older:
                progress["stop_reason"] = STOP_OLDER_BOUNDARY
                break
            offset += len(items)
            total = int((payload.get("searchMetadata") or {}).get("totalCount") or 0)
            if len(items) < policy.page_size or (total and offset >= total):
                progress["stop_reason"] = STOP_END_OF_RESULTS
                break

    reasons = {item["stop_reason"] for item in result.passes}
    if STOP_PAGE_LIMIT in reasons:
        result.stop_reason = STOP_PAGE_LIMIT
    elif reasons == {STOP_END_OF_RESULTS}:
        result.stop_reason = STOP_END_OF_RESULTS
    else:
        result.stop_reason = STOP_OLDER_BOUNDARY


def _examine(candidates: list[tuple[dict, str]], *, result: SameDayResult, page_index: int, dry_run: bool) -> None:
    """Classify one page's in-window records and write the new ones from the payload in memory. See "Newness"."""
    if not candidates:
        return
    numbers = [text for _, text in candidates]
    stored_on = _stored_on(numbers)
    evidenced = _evidenced_numbers(numbers)
    page_new: list[tuple[dict, str, str]] = []
    page_rows: dict[str, dict] = {}
    for item, text in candidates:
        result.records_examined += 1
        classification, value, quality = _classify(item, as_of=result.as_of)
        exists_locally = text in stored_on
        row = {
            "gemi_number": text, "classification": classification, "incorporation_date": value,
            "incorporation_date_quality": quality, "company_existed": exists_locally, "page_index": page_index,
            "ingest_outcome": "",
        }
        result.observations.append(row)
        if exists_locally:
            # A race with another writer only when the row itself was first stored inside the window.
            raced = result.window_start <= stored_on[text] <= result.as_of
            if text in evidenced or not raced:
                row["classification"] = KNOWN
                result.known_records += 1
                result.historical_existing_records += int(text not in evidenced)
                continue
        result.new_records += 1
        result.rediscovered_local_records += int(exists_locally)
        result.late_publication_records += int(classification == LATE_PUBLICATION)
        page_rows[text] = row
        page_new.append((item, text, classification))
    if page_new:
        for gemi_number, outcome in _count_ingest(result, _ingest(page_new, dry_run=dry_run)).items():
            page_rows[gemi_number]["ingest_outcome"] = "" if outcome == INGEST_WOULD_CREATE else outcome


def _save_run(result: SameDayResult, *, policy: SameDayPolicy, started_at, compact: bool) -> None:
    """Persist the run and its observations. No cursor exists for this stream. Callers hold one transaction."""
    GemiDiscoveryRun = apps.get_model("gemiapp", "GemiDiscoveryRun")
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    to_store = (_worth_storing(result.observations, started_at=started_at, stream=result.stream)
                if compact else result.observations)
    result.observations_stored = len(to_store)
    result.observations_suppressed = len(result.observations) - len(to_store)
    run = GemiDiscoveryRun.objects.create(
        stream=result.stream, mode=INGEST, status=result.status, finished_at=timezone.now(),
        pages_fetched=result.pages_fetched, records_examined=result.records_examined,
        known_records=result.known_records, new_records=result.new_records,
        late_publication_records=result.late_publication_records, duplicate_records=result.duplicate_records,
        invalid_identifier_records=result.invalid_identifier_records, ingested_records=result.ingested_records,
        stop_reason=result.stop_reason, anomalies=result.anomalies, error_message=result.error_message,
        policy={
            **policy.as_dict(), "trigger": result.trigger, "as_of": result.as_of.isoformat(),
            "window_start": result.window_start.isoformat(), "statistics": result.statistics(),
        },
    )
    GemiDiscoveryRun.objects.filter(pk=run.pk).update(started_at=started_at)
    result.run_id = run.pk
    if to_store:
        GemiDiscoveryObservation.objects.bulk_create([
            GemiDiscoveryObservation(run=run, **observation) for observation in to_store
        ], batch_size=500)


def run_sameday_discovery(*, dry_run: bool = False, as_of: date | None = None, max_pages: int | None = None,
                          policy: SameDayPolicy | None = None, client=None, compact_observations: bool = True,
                          max_wait: float | None = None, trigger: str = TRIGGER_OPERATOR) -> SameDayResult:
    """One same-day run over ``[as_of - lookback, as_of]``. See the module docstring.

    Raises ``SameDayLaneRefused`` before any request or write while either flag is off. ``max_pages`` bounds
    each pass; ``max_wait`` bounds how long each page request may wait for a slot in the shared rate budget.
    """
    if not sameday_enabled():
        raise SameDayLaneRefused(LANE_FLAG_OFF)
    if not ingest_enabled():
        raise SameDayLaneRefused(INGEST_FLAG_OFF)
    policy = policy or policy_from_settings()
    max_pages = policy.max_pages if max_pages is None else max_pages
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1")
    if policy.lookback_days < 0 or policy.older_boundary_records < 1:
        raise ValueError("lookback_days must not be negative and older_boundary_records must be at least 1")
    as_of = timezone.localdate() if as_of is None else as_of
    client = client or get_gemi_client()
    started_at = timezone.now()
    result = SameDayResult(mode=INGEST, dry_run=dry_run, trigger=trigger, as_of=as_of,
                           window_start=as_of - timedelta(days=policy.lookback_days))
    try:
        _scan(client, policy=policy, max_pages=max_pages, result=result, dry_run=dry_run, max_wait=max_wait)
    except Exception as exc:  # the client's own errors: budget timeout, transport, validation, HTTP
        result.status = "failed"
        result.error_message = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning("Same-day discovery run failed after %s requests: %s", result.pages_fetched, type(exc).__name__)
    else:
        if result.blocking_anomalies:
            result.status = "anomaly"
        elif result.ingest_failed_records:
            result.status = "failed"
            result.error_message = (f"{result.ingest_failed_records} company write(s) failed unexpectedly; "
                                    f"the next run rescans the window and retries them.")
        elif result.stop_reason == STOP_PAGE_LIMIT:
            result.status = "incomplete"
        else:
            result.status = "success"

    if not dry_run:
        with transaction.atomic():
            _save_run(result, policy=policy, started_at=started_at, compact=compact_observations)
    logger.info(
        "Same-day discovery run: status=%s requests=%s examined=%s new=%s created=%s future=%s unusable=%s "
        "date_order_anomalies=%s",
        result.status, result.pages_fetched, result.records_examined, result.new_records, result.ingested_records,
        result.future_date_records, result.unusable_date_records, result.date_order_anomalies,
    )
    return result
