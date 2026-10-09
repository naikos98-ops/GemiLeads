"""Parity between the legacy importer and the unified ingestion, for the G4 parallel period. Read-only.

The question
------------
    Did the legacy importer discover any valid company that the scheduled unified ingestion failed to discover
    or store?

Why the older comparison cannot answer it
-----------------------------------------
``ingestion.discovery.compare_with_legacy`` calls "legacy" every ``Company`` row whose ``incorporation_date`` is
the day. That held while Discovery ran in shadow and only the legacy importer created companies. Once the
unified ingestion creates companies too, a row it created itself is counted as a legacy find, and "the row
exists" says nothing about who found it. That function is left as it is for the shadow-only history; this module
is the comparison for the parallel period.

Provenance, not existence
-------------------------
Nothing here infers parity from a ``Company`` row being present. Each company dated the target day is attributed
from recorded evidence:

* **unified ingestion stored it** -- an ingest-mode discovery observation of that GEMI number carries
  ``ingest_outcome = created``. The compact observation policy always stores a first sighting, so this evidence
  is never suppressed.
* **the legacy importer stored it** -- no such observation, and ``Company.imported_at`` (``auto_now_add``, never
  moved) falls inside the window of an ``ImportRun`` for that target date. Anything else that created the row
  (operator hydration, the admin) is reported as ``other writer``, never as legacy.

A company the unified ingestion did not store is then judged by what ingest-mode runs recorded about it:

* ``legacy_first_confirmed`` -- legacy stored it first and a unified run then discovered it as newly seen and
  found it already local. A race, not a miss: unified reached the record on its own.
* ``legacy_first_seen_known`` -- a unified run paged past it only as an already-known record.
* ``unified_failed_to_store`` -- unified discovered it *before* legacy stored it but quarantined its date or
  failed the write, and legacy then stored it. A store failure for a company legacy considered valid.
* ``legacy_only`` -- **the miss**: no ingest-mode run ever recorded the number, although at least one successful
  ingest run started after legacy stored it.
* ``awaiting_unified_run`` -- no run recorded it yet, and none has succeeded since legacy stored it: not judged.

The other side
--------------
For the ingest-mode runs that started on the target day (local time): companies the unified ingestion created,
split into late publications (an older incorporation date -- the legacy importer can never import those, so they
are unified-only by design) and same-day incorporations; quarantined dates; unexpected write failures; duplicate
records; and runs that failed, were incomplete or hit an ordering anomaly.

Two lanes, one verdict
----------------------
The unified ingestion has two discovery lanes with their own streams: the ``-arGemi`` frontier lane and the
(dormant) same-day ``-incorporationDate`` lane (``gemiapp.ingestion.sameday_discovery``). The frontier lane
cannot reach a company with an old GEMI number and today's date, so the report measures each lane on its own
and judges the **union**:

* per lane: runs, their outcomes, requests (pages) and companies created that day;
* per company dated the day: found by the frontier lane only, by the same-day lane only, or by both;
* ``frontier gaps recovered by the same-day lane`` -- the companies only the same-day lane recorded: what would
  have been a legacy-only miss, or never stored at all, with the frontier lane alone;
* the same-day lane's skipped future-dated and unusable source records (the most any one run met: every run
  rescans the same junk, so a sum would mean nothing), its date-order anomalies and write failures.

``legacy_only`` and the verdict consider both lanes. The frontier-lane counters keep their names
(``ingest_runs``, ``unified_created`` ...) and their meaning.

Evidence has to be timely
-------------------------
A miss is a fact about what the ingestion did *at the time*. Evidence recorded later must not turn an old MISS
into OK, so a discovery observation only counts for a target day when its run

* started no later than the end of the following local day -- the legacy importer itself imports a day's
  companies on that day and, in its 09:00 daily run, the day after; and
* was not an operator backfill (``policy["trigger"] == "operator_backfill"``).

A company whose only evidence is late or a backfill stays ``legacy_only`` (or awaiting / other-writer, as
before) and is listed as ``recovered late`` / ``operator backfill evidence`` beside it. A company the unified
ingestion *created* is unified-first whenever that happened: nobody else had stored it.

Identifiers and counts only: no payload, no name, no contact data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.apps import apps

from django.utils import timezone

from .ingestion.discovery import (
    INGEST,
    INGEST_ALREADY_LOCAL,
    INGEST_CREATED,
    INGEST_QUARANTINED_DATE,
    INGEST_WRITE_FAILED,
    KNOWN,
    LATE_PUBLICATION,
    STREAM_COMPANIES,
)
from .ingestion.sameday_discovery import STREAM_INCORPORATION_DATE, TRIGGER_BACKFILL

# An ImportRun killed before it recorded finished_at is given the django-q task timeout as its window.
UNFINISHED_IMPORT_WINDOW = timedelta(minutes=30)
CHUNK = 500
# Evidence counts for a target day when its run started by the end of the following local day.
EVIDENCE_HORIZON = timedelta(days=1)


@dataclass
class ParityReport:
    target_date: date
    # the unified side: ingest-mode runs that started on the target day
    ingest_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    incomplete_runs: int = 0
    anomaly_runs: int = 0
    unified_created: int = 0
    unified_only_late_publications: int = 0
    unified_created_same_day: int = 0
    quarantined_date: int = 0
    write_failed: int = 0
    duplicate_records: int = 0
    # the legacy side: ImportRun rows for the target date
    legacy_import_runs: int = 0
    legacy_failed_runs: int = 0
    legacy_reported_created: int = 0      # sum of ImportRun.created_count: the importer's own count
    # companies dated the target day, by who stored them and what unified recorded
    companies_dated: int = 0
    unified_first: int = 0
    unified_first_then_legacy: int = 0    # ... and a later legacy import of that date updated the row
    legacy_first_confirmed: int = 0
    legacy_first_seen_known: int = 0
    awaiting_unified_run: int = 0
    other_writer: int = 0
    legacy_only: list = field(default_factory=list)
    unified_failed_to_store: list = field(default_factory=list)
    other_writer_unseen: list = field(default_factory=list)
    # the frontier lane's requests, and the same-day lane measured on its own (its runs of the target day)
    frontier_pages: int = 0
    sameday_runs: int = 0
    sameday_successful_runs: int = 0
    sameday_failed_runs: int = 0
    sameday_incomplete_runs: int = 0
    sameday_anomaly_runs: int = 0
    sameday_pages: int = 0
    sameday_created: int = 0
    sameday_already_local: int = 0          # first evidence for a row another writer had stored
    sameday_quarantined_date: int = 0
    sameday_write_failed: int = 0
    sameday_duplicate_records: int = 0
    sameday_date_order_anomalies: int = 0
    sameday_future_skipped: int = 0         # the most any one run skipped (each run rescans the same junk)
    sameday_unusable_skipped: int = 0
    # companies dated that day, by which lane recorded them in time
    found_by_frontier_only: int = 0
    found_by_sameday_only: int = 0
    found_by_both: int = 0
    frontier_gaps_recovered: list = field(default_factory=list)
    # no timely evidence, but something recorded later: never changes the verdict
    late_evidence: list = field(default_factory=list)
    backfill_evidence: list = field(default_factory=list)

    @property
    def legacy_first(self) -> int:
        return (self.legacy_first_confirmed + self.legacy_first_seen_known + self.awaiting_unified_run
                + len(self.legacy_only) + len(self.unified_failed_to_store))

    @property
    def verdict(self) -> str:
        if not self.ingest_runs and not self.sameday_runs:
            return "NOT_MEASURED"       # no unified ingestion run that day: nothing to certify
        if self.legacy_only or self.unified_failed_to_store:
            return "MISS"
        return "PENDING" if self.awaiting_unified_run else "OK"

    def lines(self) -> list[str]:
        return [
            f"UNIFIED INGESTION vs LEGACY IMPORTER for {self.target_date.isoformat()} -- verdict: {self.verdict}",
            f"unified runs={self.ingest_runs} success={self.successful_runs} failed={self.failed_runs} "
            f"incomplete={self.incomplete_runs} ordering_anomaly={self.anomaly_runs} "
            f"(frontier lane, -arGemi; pages={self.frontier_pages})",
            f"unified created={self.unified_created} (late publications, unified-only by design="
            f"{self.unified_only_late_publications} same-day={self.unified_created_same_day}) "
            f"quarantined_date={self.quarantined_date} write_failed={self.write_failed} "
            f"duplicate_records={self.duplicate_records}",
            f"legacy import runs={self.legacy_import_runs} failed={self.legacy_failed_runs} "
            f"created (importer's own count)={self.legacy_reported_created}",
            f"companies dated that day={self.companies_dated}: unified first={self.unified_first} "
            f"(later also imported by legacy={self.unified_first_then_legacy}) legacy first={self.legacy_first} "
            f"other writer={self.other_writer}",
            f"legacy first, by what unified recorded: confirmed as newly discovered (race)="
            f"{self.legacy_first_confirmed} seen as already known={self.legacy_first_seen_known} "
            f"awaiting a unified run={self.awaiting_unified_run}",
            f"LEGACY-ONLY (stored by legacy, never recorded by a later unified run)={len(self.legacy_only)}"
            + (f": {self.legacy_only[:20]}" if self.legacy_only else ""),
            f"UNIFIED FAILED TO STORE (discovered first, then stored by legacy)={len(self.unified_failed_to_store)}"
            + (f": {self.unified_failed_to_store[:20]}" if self.unified_failed_to_store else ""),
            *([f"other writer, never recorded by unified={len(self.other_writer_unseen)}: "
               f"{self.other_writer_unseen[:20]}"] if self.other_writer_unseen else []),
            f"SAME-DAY LANE (-incorporationDate): runs={self.sameday_runs} success={self.sameday_successful_runs} "
            f"failed={self.sameday_failed_runs} incomplete={self.sameday_incomplete_runs} "
            f"date_order_anomaly={self.sameday_anomaly_runs} pages={self.sameday_pages}"
            + ("" if self.sameday_runs else " -- the lane did not run that day"),
            f"same-day created={self.sameday_created} first evidence for an already stored row="
            f"{self.sameday_already_local} quarantined_date={self.sameday_quarantined_date} "
            f"write_failed={self.sameday_write_failed} duplicate_records={self.sameday_duplicate_records}",
            f"same-day source records skipped (most in one run, never written): future-dated="
            f"{self.sameday_future_skipped} missing/unreadable/pre-1900={self.sameday_unusable_skipped}; "
            f"date-order anomalies={self.sameday_date_order_anomalies}",
            f"companies dated that day, by lane (timely evidence): frontier only={self.found_by_frontier_only} "
            f"same-day only={self.found_by_sameday_only} both={self.found_by_both}",
            f"FRONTIER GAPS RECOVERED BY THE SAME-DAY LANE={len(self.frontier_gaps_recovered)}"
            + (f": {self.frontier_gaps_recovered[:20]}" if self.frontier_gaps_recovered else ""),
            *([f"recovered late (evidence after the day's horizon; the verdict is unchanged)="
               f"{len(self.late_evidence)}: {self.late_evidence[:20]}"] if self.late_evidence else []),
            *([f"operator backfill evidence (never counted; the verdict is unchanged)="
               f"{len(self.backfill_evidence)}: {self.backfill_evidence[:20]}"] if self.backfill_evidence else []),
        ]


def _chunks(values):
    values = list(values)
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def _trigger(policy) -> str:
    return str(policy.get("trigger") or "") if isinstance(policy, dict) else ""


def compare_unified_with_legacy(target_date: date, *, stream: str = STREAM_COMPANIES,
                                sameday_stream: str = STREAM_INCORPORATION_DATE) -> ParityReport:
    """The parity report for one day. Reads only; see the module docstring for every category."""
    Company = apps.get_model("gemiapp", "Company")
    ImportRun = apps.get_model("gemiapp", "ImportRun")
    GemiDiscoveryRun = apps.get_model("gemiapp", "GemiDiscoveryRun")
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    report = ParityReport(target_date=target_date)
    streams = (stream, sameday_stream)
    ingest_runs = GemiDiscoveryRun.objects.filter(stream__in=streams, mode=INGEST)

    # --- the unified side, by run day and by lane ------------------------------------------------------
    all_day_runs = list(ingest_runs.filter(started_at__date=target_date)
                        .values("pk", "stream", "status", "duplicate_records", "pages_fetched", "policy"))
    day_runs = [run for run in all_day_runs if run["stream"] == stream]
    lane_runs = [run for run in all_day_runs if run["stream"] == sameday_stream]
    report.ingest_runs = len(day_runs)
    report.successful_runs = sum(1 for run in day_runs if run["status"] == "success")
    report.failed_runs = sum(1 for run in day_runs if run["status"] == "failed")
    report.incomplete_runs = sum(1 for run in day_runs if run["status"] == "incomplete")
    report.anomaly_runs = sum(1 for run in day_runs if run["status"] == "anomaly")
    report.duplicate_records = sum(run["duplicate_records"] for run in day_runs)
    report.frontier_pages = sum(run["pages_fetched"] for run in day_runs)
    report.sameday_runs = len(lane_runs)
    report.sameday_successful_runs = sum(1 for run in lane_runs if run["status"] == "success")
    report.sameday_failed_runs = sum(1 for run in lane_runs if run["status"] == "failed")
    report.sameday_incomplete_runs = sum(1 for run in lane_runs if run["status"] == "incomplete")
    report.sameday_anomaly_runs = sum(1 for run in lane_runs if run["status"] == "anomaly")
    report.sameday_duplicate_records = sum(run["duplicate_records"] for run in lane_runs)
    report.sameday_pages = sum(run["pages_fetched"] for run in lane_runs)
    for run in lane_runs:
        statistics = (run["policy"] or {}).get("statistics") or {}
        report.sameday_date_order_anomalies += int(statistics.get("date_order_anomalies") or 0)
        report.sameday_future_skipped = max(report.sameday_future_skipped,
                                            int(statistics.get("future_date_records") or 0))
        report.sameday_unusable_skipped = max(report.sameday_unusable_skipped,
                                              int(statistics.get("unusable_date_records") or 0))

    stream_of = {run["pk"]: run["stream"] for run in all_day_runs}
    outcomes: dict[str, dict[str, dict]] = {stream: {}, sameday_stream: {}}     # stream -> outcome -> {number: class}
    for row in (GemiDiscoveryObservation.objects.filter(run_id__in=list(stream_of)).exclude(ingest_outcome="")
                .values("run_id", "gemi_number", "classification", "ingest_outcome")):
        outcomes[stream_of[row["run_id"]]].setdefault(row["ingest_outcome"], {})[row["gemi_number"]] = \
            row["classification"]
    created = outcomes[stream].get(INGEST_CREATED, {})
    report.unified_created = len(created)
    report.unified_only_late_publications = sum(1 for value in created.values() if value == LATE_PUBLICATION)
    report.unified_created_same_day = report.unified_created - report.unified_only_late_publications
    report.quarantined_date = len(outcomes[stream].get(INGEST_QUARANTINED_DATE, {}))
    report.write_failed = len(outcomes[stream].get(INGEST_WRITE_FAILED, {}))
    report.sameday_created = len(outcomes[sameday_stream].get(INGEST_CREATED, {}))
    report.sameday_already_local = len(outcomes[sameday_stream].get(INGEST_ALREADY_LOCAL, {}))
    report.sameday_quarantined_date = len(outcomes[sameday_stream].get(INGEST_QUARANTINED_DATE, {}))
    report.sameday_write_failed = len(outcomes[sameday_stream].get(INGEST_WRITE_FAILED, {}))

    # --- the legacy side -------------------------------------------------------------------------------
    imports = list(ImportRun.objects.filter(target_date=target_date)
                   .values("status", "created_count", "started_at", "finished_at"))
    report.legacy_import_runs = len(imports)
    report.legacy_failed_runs = sum(1 for run in imports if run["status"] == "failed")
    report.legacy_reported_created = sum(run["created_count"] for run in imports)
    windows = [(run["started_at"], run["finished_at"] or run["started_at"] + UNFINISHED_IMPORT_WINDOW)
               for run in imports]

    def in_legacy_window(moment) -> bool:
        return any(start <= moment <= end for start, end in windows)

    latest_success = (ingest_runs.filter(status="success").order_by("-started_at")
                      .values_list("started_at", flat=True).first())

    # --- every company dated that day, by provenance ---------------------------------------------------
    companies = list(Company.objects.filter(incorporation_date=target_date)
                     .values("gemi_number", "imported_at", "updated_at"))
    report.companies_dated = len(companies)
    horizon = target_date + EVIDENCE_HORIZON
    evidence: dict[str, list] = {}
    for chunk in _chunks(company["gemi_number"] for company in companies):
        rows = (GemiDiscoveryObservation.objects
                .filter(gemi_number__in=chunk, run__mode=INGEST, run__stream__in=streams).order_by("id")
                .values("gemi_number", "classification", "ingest_outcome", "run__started_at", "run__stream",
                        "run__policy"))
        for row in rows:
            row["backfill"] = _trigger(row.pop("run__policy")) == TRIGGER_BACKFILL
            row["timely"] = not row["backfill"] and timezone.localdate(row["run__started_at"]) <= horizon
            evidence.setdefault(row["gemi_number"], []).append(row)

    for company in companies:
        number, stored_at = company["gemi_number"], company["imported_at"]
        recorded = evidence.get(number, [])
        seen = [row for row in recorded if row["timely"]]        # the only evidence that can clear a miss
        lanes = {row["run__stream"] for row in seen}
        report.found_by_both += int(lanes == set(streams))
        report.found_by_frontier_only += int(lanes == {stream})
        if lanes == {sameday_stream}:
            report.found_by_sameday_only += 1
            report.frontier_gaps_recovered.append(number)
        if any(row["ingest_outcome"] == INGEST_CREATED for row in recorded):
            report.unified_first += 1
            # A later legacy import of that date rewrote the row (update_or_create): legacy saw it too.
            report.unified_first_then_legacy += int(
                company["updated_at"] > stored_at and in_legacy_window(company["updated_at"]))
            continue
        legacy = in_legacy_window(stored_at)
        report.other_writer += int(not legacy)
        discovered = [row for row in seen if row["classification"] != KNOWN]
        if not seen:
            if any(row["backfill"] for row in recorded):
                report.backfill_evidence.append(number)
            if any(not row["backfill"] for row in recorded):
                report.late_evidence.append(number)
            if latest_success is None or latest_success <= stored_at:
                report.awaiting_unified_run += int(legacy)   # no unified run has had the chance yet
            elif legacy:
                report.legacy_only.append(number)
            else:
                report.other_writer_unseen.append(number)
            continue
        if not legacy:
            continue                                         # hydration or admin: not a legacy find
        first = discovered[0] if discovered else None
        if (first is not None and first["run__started_at"] < stored_at
                and first["ingest_outcome"] in (INGEST_QUARANTINED_DATE, INGEST_WRITE_FAILED)
                and not any(row["ingest_outcome"] == INGEST_ALREADY_LOCAL and row["run__started_at"] < stored_at
                            for row in discovered)):
            report.unified_failed_to_store.append(number)
        elif discovered:
            report.legacy_first_confirmed += 1
        else:
            report.legacy_first_seen_known += 1

    for numbers in (report.legacy_only, report.unified_failed_to_store, report.other_writer_unseen,
                    report.frontier_gaps_recovered, report.late_evidence, report.backfill_evidence):
        numbers.sort()
    return report
