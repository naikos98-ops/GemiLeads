"""Company reference ids and lifecycle metadata (A6): derivation from stored data and batched backfill.

A6 adds nine nullable columns to Company and fills them without changing current behaviour: nothing in
the importer, Radars, digests, search, exports or UI reads them yet, and no existing column is written.

Fields
------
* ``status_source_id``, ``legal_type_source_id``, ``gemi_office_source_id``, ``prefecture_source_id``,
  ``municipality_source_id`` -- the ``id`` of the matching reference object in the stored GEMI record
  (``status``, ``legalType``, ``gemiOffice``, ``prefecture``, ``municipality``). Accepted only when it
  satisfies the A2 identifier contract (a non-negative integer or a digit string) and normalised with the
  A3 identifier rule, so ``5`` and ``"5"`` are the same id. An absent or null object, or an object
  without an id, leaves the field null. Descriptions are never used as ids, and an id unknown to the local
  reference tables is kept. These ids are what a later step will use, through GemiCompanyStatus, to stop
  treating deleted companies as active; A6 does not touch ``Company.is_active``.
* ``incorporation_date_quality`` -- the A3 ``DateQuality`` of the stored record's ``incorporationDate``
  (valid / missing / invalid / out_of_range), evaluated with ``normalize_event_date`` against the local
  date of ``updated_at`` (the day the record was last written). ``incorporation_date`` is not changed.
* ``first_seen_at``, ``last_seen_at``, ``last_synced_at`` -- see below.

Lifecycle semantics and evidence
--------------------------------
``first_seen_at``: the earliest time Gemi Leads can establish that it observed this company in a GEMI
response. Historical source: ``Company.imported_at`` (auto_now_add). It is set once, when the importer
(``import_for_date`` or ``import_companies_since_date``, both via ``update_or_create``) creates the row
from a GEMI response it has just received. If a database was ever rebuilt by re-importing, this is the
re-import time -- a genuine observation, though not necessarily the earliest ever; no earlier evidence
exists.

``last_seen_at``: the most recent time Gemi Leads observed this company in a valid GEMI response.
Historical source: ``Company.updated_at`` (auto_now). The importers save a company on every observation
(``update_or_create`` saves even when nothing changed). The only data migration that wrote companies
(0015) used ``bulk_update(["search_name"])``, which does not touch ``updated_at``; the demo seed commands
write only non-GEMI rows. Changes made through the Django admin are detectable in ``django_admin_log``
and exclude a row. Unlogged direct database edits cannot be detected.

Both timestamps are taken from those columns only when ``raw_data`` is a GEMI company record for this very
company (its ``arGemi`` equals ``gemi_number``) and no Django admin addition or change was logged for it.
Otherwise they stay null: demo and seed rows, admin-created or admin-edited rows, rows without a usable
record. Nothing is ever derived from the current time.

``last_synced_at``: the most recent time the canonical GEMI state was synchronised into the structured
company fields. No canonical synchronisation has happened yet, so the backfill never writes it; it stays
null until canonical ingestion sets it.

Reconciliation (idempotent, never destructive)
----------------------------------------------
Ids and date quality are set when a value can be derived and differs from the stored one; a derivable
null never erases a stored value. ``first_seen_at`` only ever moves earlier and ``last_seen_at`` only
later. ``last_synced_at`` is never written. A second run therefore changes nothing. Writes use
``bulk_update`` on these fields only, so ``updated_at`` -- and every other existing column -- is untouched.

Backfill
--------
``backfill_company_metadata`` reads companies in primary-key order, ``batch_size`` at a time, from
``start_id`` (exclusive), and writes each batch in its own transaction, so it can be interrupted and run
again. ``dry_run`` computes the same report and writes nothing. It never calls GEMI and does not need the
GEMI reference tables.

Row-level data problems (a non-object ``raw_data``, an unexpected reference shape, an unreadable value)
are counted and that row's metadata is skipped or partly filled; the batch continues. Anything else --
database errors, programming errors -- is not caught and stops the run, so the batch in progress is rolled
back. Only counts and company primary keys are ever logged or printed; ``raw_data`` values (which include
persons and contact data) are never copied or logged.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from .normalizer import normalize_event_date, normalize_identifier
from .schemas import IDENTIFIER

logger = logging.getLogger(__name__)

REFERENCE_ID_FIELDS = {
    "status_source_id": "status",
    "legal_type_source_id": "legalType",
    "gemi_office_source_id": "gemiOffice",
    "prefecture_source_id": "prefecture",
    "municipality_source_id": "municipality",
}
BACKFILL_FIELDS = (*REFERENCE_ID_FIELDS, "incorporation_date_quality", "first_seen_at", "last_seen_at")
A6_FIELDS = (*BACKFILL_FIELDS, "last_synced_at")

RAW_GEMI_RECORD = "gemi_record"
RAW_MISSING = "missing"
RAW_MALFORMED = "malformed"
RAW_NOT_GEMI_RECORD = "not_gemi_record"


@dataclass(frozen=True)
class DerivedMetadata:
    values: dict[str, Any]
    raw_state: str
    malformed_references: tuple[str, ...] = ()
    admin_touched: bool = False


def derive_company_metadata(
    *, gemi_number: str, raw_data: Any, imported_at, updated_at, admin_touched: bool,
) -> DerivedMetadata:
    """The A6 values derivable for one company from its stored data. None means nothing derivable."""
    values: dict[str, Any] = {name: None for name in BACKFILL_FIELDS}
    if raw_data is None or raw_data == {}:
        return DerivedMetadata(values, RAW_MISSING, admin_touched=admin_touched)
    if not isinstance(raw_data, Mapping):
        return DerivedMetadata(values, RAW_MALFORMED, admin_touched=admin_touched)
    ar_gemi = raw_data.get("arGemi")
    if not IDENTIFIER.accepts(ar_gemi) or normalize_identifier(ar_gemi) != gemi_number:
        return DerivedMetadata(values, RAW_NOT_GEMI_RECORD, admin_touched=admin_touched)

    malformed = []
    for field_name, source_key in REFERENCE_ID_FIELDS.items():
        reference = raw_data.get(source_key)
        if reference is None:
            continue
        if not isinstance(reference, Mapping):
            malformed.append(source_key)
            continue
        source_id = reference.get("id")
        if source_id is None:
            continue
        if not IDENTIFIER.accepts(source_id):
            malformed.append(source_key)
            continue
        values[field_name] = normalize_identifier(source_id)

    values["incorporation_date_quality"] = normalize_event_date(
        raw_data.get("incorporationDate"), as_of=timezone.localdate(updated_at),
    ).quality.value
    if not admin_touched:
        values["first_seen_at"] = imported_at
        values["last_seen_at"] = updated_at
    return DerivedMetadata(values, RAW_GEMI_RECORD, tuple(malformed), admin_touched)


def reconcile(current: Mapping[str, Any], derived: Mapping[str, Any]) -> dict[str, Any]:
    """The changes to apply to ``current``: fill or correct, never erase; first seen only earlier, last
    seen only later."""
    changes: dict[str, Any] = {}
    for name in (*REFERENCE_ID_FIELDS, "incorporation_date_quality"):
        if derived[name] is not None and current[name] != derived[name]:
            changes[name] = derived[name]
    first_seen = derived["first_seen_at"]
    if first_seen is not None and (current["first_seen_at"] is None or first_seen < current["first_seen_at"]):
        changes["first_seen_at"] = first_seen
    last_seen = derived["last_seen_at"]
    if last_seen is not None and (current["last_seen_at"] is None or last_seen > current["last_seen_at"]):
        changes["last_seen_at"] = last_seen
    return changes


@dataclass
class BackfillReport:
    dry_run: bool
    inspected: int = 0
    changed: int = 0
    batches: int = 0
    last_id: int | None = None
    row_errors: int = 0
    admin_touched: int = 0
    raw_states: Counter = field(default_factory=Counter)
    reference_ids_found: Counter = field(default_factory=Counter)
    reference_ids_missing: Counter = field(default_factory=Counter)
    malformed_references: Counter = field(default_factory=Counter)
    date_quality: Counter = field(default_factory=Counter)
    first_seen_derived: int = 0
    last_seen_derived: int = 0

    def record(self, derived: DerivedMetadata) -> None:
        self.raw_states[derived.raw_state] += 1
        self.admin_touched += int(derived.admin_touched)
        if derived.raw_state != RAW_GEMI_RECORD:
            return
        for name in REFERENCE_ID_FIELDS:
            (self.reference_ids_found if derived.values[name] is not None else self.reference_ids_missing)[name] += 1
        for source_key in derived.malformed_references:
            self.malformed_references[source_key] += 1
        self.date_quality[derived.values["incorporation_date_quality"]] += 1
        self.first_seen_derived += int(derived.values["first_seen_at"] is not None)
        self.last_seen_derived += int(derived.values["last_seen_at"] is not None)

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        verb = "would change" if self.dry_run else "changed"
        return [
            f"{prefix}inspected={self.inspected} {verb}={self.changed} batches={self.batches} last_id={self.last_id} row_errors={self.row_errors}",
            f"{prefix}raw_data: " + " ".join(f"{key}={value}" for key, value in sorted(self.raw_states.items())),
            f"{prefix}reference ids found: " + " ".join(f"{name}={self.reference_ids_found[name]}" for name in REFERENCE_ID_FIELDS),
            f"{prefix}reference ids missing: " + " ".join(f"{name}={self.reference_ids_missing[name]}" for name in REFERENCE_ID_FIELDS),
            f"{prefix}malformed references: " + (" ".join(f"{key}={value}" for key, value in sorted(self.malformed_references.items())) or "none"),
            f"{prefix}incorporation date quality: " + (" ".join(f"{key}={value}" for key, value in sorted(self.date_quality.items())) or "none"),
            f"{prefix}lifecycle derivable: first_seen_at={self.first_seen_derived} last_seen_at={self.last_seen_derived} "
            f"admin_touched={self.admin_touched} (last_synced_at is never backfilled)",
        ]


def _admin_touched_company_ids() -> set[int]:
    from django.contrib.admin.models import ADDITION, CHANGE, LogEntry
    from django.contrib.contenttypes.models import ContentType

    Company = apps.get_model("gemiapp", "Company")
    content_type = ContentType.objects.get_for_model(Company)
    object_ids = LogEntry.objects.filter(
        content_type=content_type, action_flag__in=[ADDITION, CHANGE],
    ).values_list("object_id", flat=True)
    return {int(object_id) for object_id in object_ids if str(object_id).isdigit()}


def backfill_company_metadata(*, batch_size: int = 500, dry_run: bool = False, start_id: int = 0) -> BackfillReport:
    """Fill the A6 columns from stored data, in batches. See the module docstring."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    Company = apps.get_model("gemiapp", "Company")
    admin_touched_ids = _admin_touched_company_ids()
    report = BackfillReport(dry_run=dry_run)
    last_id = start_id
    while True:
        rows = list(
            Company.objects.filter(pk__gt=last_id).order_by("pk")
            .values("pk", "gemi_number", "raw_data", "imported_at", "updated_at", *BACKFILL_FIELDS)[:batch_size]
        )
        if not rows:
            break
        updates = []
        for row in rows:
            report.inspected += 1
            try:
                derived = derive_company_metadata(
                    gemi_number=row["gemi_number"], raw_data=row["raw_data"], imported_at=row["imported_at"],
                    updated_at=row["updated_at"], admin_touched=row["pk"] in admin_touched_ids,
                )
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                report.row_errors += 1
                logger.warning("Company %s: A6 metadata could not be derived (%s); row skipped.", row["pk"], type(exc).__name__)
                continue
            report.record(derived)
            changes = reconcile(row, derived.values)
            if changes:
                report.changed += 1
                updates.append(Company(pk=row["pk"], **{name: changes.get(name, row[name]) for name in BACKFILL_FIELDS}))
        if updates and not dry_run:
            with transaction.atomic():
                Company.objects.bulk_update(updates, BACKFILL_FIELDS)
        report.batches += 1
        last_id = report.last_id = rows[-1]["pk"]
        logger.info(
            "GEMI company metadata backfill%s: batch %s up to id %s, %s inspected, %s %s so far.",
            " (dry run)" if dry_run else "", report.batches, last_id, report.inspected,
            report.changed, "would change" if dry_run else "changed",
        )
    return report
