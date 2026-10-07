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

Identifiers and counts only: no payload, no name, no contact data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.apps import apps

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

# An ImportRun killed before it recorded finished_at is given the django-q task timeout as its window.
UNFINISHED_IMPORT_WINDOW = timedelta(minutes=30)
CHUNK = 500


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

    @property
    def legacy_first(self) -> int:
        return (self.legacy_first_confirmed + self.legacy_first_seen_known + self.awaiting_unified_run
                + len(self.legacy_only) + len(self.unified_failed_to_store))

    @property
    def verdict(self) -> str:
        if not self.ingest_runs:
            return "NOT_MEASURED"       # no unified ingestion run that day: nothing to certify
        if self.legacy_only or self.unified_failed_to_store:
            return "MISS"
        return "PENDING" if self.awaiting_unified_run else "OK"

    def lines(self) -> list[str]:
        return [
            f"UNIFIED INGESTION vs LEGACY IMPORTER for {self.target_date.isoformat()} -- verdict: {self.verdict}",
            f"unified runs={self.ingest_runs} success={self.successful_runs} failed={self.failed_runs} "
            f"incomplete={self.incomplete_runs} ordering_anomaly={self.anomaly_runs}",
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
        ]


def _chunks(values):
    values = list(values)
    for start in range(0, len(values), CHUNK):
        yield values[start:start + CHUNK]


def compare_unified_with_legacy(target_date: date, *, stream: str = STREAM_COMPANIES) -> ParityReport:
    """The parity report for one day. Reads only; see the module docstring for every category."""
    Company = apps.get_model("gemiapp", "Company")
    ImportRun = apps.get_model("gemiapp", "ImportRun")
    GemiDiscoveryRun = apps.get_model("gemiapp", "GemiDiscoveryRun")
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    report = ParityReport(target_date=target_date)
    ingest_runs = GemiDiscoveryRun.objects.filter(stream=stream, mode=INGEST)

    # --- the unified side, by run day ------------------------------------------------------------------
    day_runs = list(ingest_runs.filter(started_at__date=target_date).values("pk", "status", "duplicate_records"))
    report.ingest_runs = len(day_runs)
    report.successful_runs = sum(1 for run in day_runs if run["status"] == "success")
    report.failed_runs = sum(1 for run in day_runs if run["status"] == "failed")
    report.incomplete_runs = sum(1 for run in day_runs if run["status"] == "incomplete")
    report.anomaly_runs = sum(1 for run in day_runs if run["status"] == "anomaly")
    report.duplicate_records = sum(run["duplicate_records"] for run in day_runs)
    day_rows = GemiDiscoveryObservation.objects.filter(run_id__in=[run["pk"] for run in day_runs])
    created = dict(day_rows.filter(ingest_outcome=INGEST_CREATED).values_list("gemi_number", "classification"))
    report.unified_created = len(created)
    report.unified_only_late_publications = sum(1 for value in created.values() if value == LATE_PUBLICATION)
    report.unified_created_same_day = report.unified_created - report.unified_only_late_publications
    report.quarantined_date = (day_rows.filter(ingest_outcome=INGEST_QUARANTINED_DATE)
                               .values("gemi_number").distinct().count())
    report.write_failed = (day_rows.filter(ingest_outcome=INGEST_WRITE_FAILED)
                           .values("gemi_number").distinct().count())

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
    evidence: dict[str, list] = {}
    for chunk in _chunks(company["gemi_number"] for company in companies):
        rows = (GemiDiscoveryObservation.objects
                .filter(gemi_number__in=chunk, run__mode=INGEST, run__stream=stream).order_by("id")
                .values("gemi_number", "classification", "ingest_outcome", "run__started_at"))
        for row in rows:
            evidence.setdefault(row["gemi_number"], []).append(row)

    for company in companies:
        number, stored_at = company["gemi_number"], company["imported_at"]
        seen = evidence.get(number, [])
        if any(row["ingest_outcome"] == INGEST_CREATED for row in seen):
            report.unified_first += 1
            # A later legacy import of that date rewrote the row (update_or_create): legacy saw it too.
            report.unified_first_then_legacy += int(
                company["updated_at"] > stored_at and in_legacy_window(company["updated_at"]))
            continue
        legacy = in_legacy_window(stored_at)
        report.other_writer += int(not legacy)
        discovered = [row for row in seen if row["classification"] != KNOWN]
        if not seen:
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

    report.legacy_only.sort()
    report.unified_failed_to_store.sort()
    report.other_writer_unseen.sort()
    return report
