"""Canonical CompanyActivity metadata, safe upsert, legacy-compatible matching and parity (A7).

What the source publishes
-------------------------
Observed in the live capability spike and in the copied development database (119,564 entries):
every ``activities`` entry is ``{activity: {id, descr, kadVersion}, type, dtFrom, dtTo}``, where

* ``type`` is one of Κύρια, Δευτερεύουσα, Βοηθητική, Λοιπή;
* ``kadVersion`` is ``kad_2026``, ``kad_2008`` or null;
* ``dtFrom`` is a date, and ``dtTo`` is null for an open activity or a date otherwise, 9999-12-31 included;
* a company may publish the same code and type more than once, either in both KAD versions (the
  2008 -> 2026 reclassification ends the 2008 entry and starts a 2026 one) or for two periods of one version.

The legacy importer kept one row per (code, type), the first entry in source order, and deleted and
recreated every row on each import. It stored no version and no dates, so an ended activity kept matching
Radars.

Fields (on CompanyActivity)
---------------------------
* ``activity_type`` (legacy column, unchanged): the type exactly as published.
* ``activity_type_normalized``: ``primary`` / ``secondary`` / ``auxiliary`` / ``other`` for the four
  published types (compared case- and accent-insensitively); ``unknown`` for any other non-blank value,
  which is still stored as published and never fails ingestion; null when the entry has no type.
* ``kad_version``: as published (A3 text rule), never inferred from the code; null when unpublished.
* ``date_from`` / ``date_to`` with ``date_from_quality`` / ``date_to_quality``: the A3 ``DateQuality`` of
  ``dtFrom`` / ``dtTo`` (period bounds up to 2100-12-31). A date is stored only when VALID; an invalid or
  out-of-range source value is never turned into a date. The quality is what tells an open activity
  (``date_to`` null, MISSING) from an unreadable end (``date_to`` null, INVALID).
* ``is_current`` with ``current_as_of``: the A3 rule evaluated on ``current_as_of``. No ``dtTo`` -> True;
  ``dtTo`` after ``current_as_of`` -> True; on or before it -> False (also for an out-of-range date, so
  9999-12-31 is current); unreadable ``dtTo`` -> None. ``current_as_of`` is the day of the GEMI observation
  the row was derived from: the import day for the importer; for the backfill, the local date of the
  company's ``updated_at``, when its stored ``raw_data`` was last written. It is not recomputed per request,
  so a row with a future ``dtTo`` stays current until the company is observed again.
* ``source_key``: the canonical identity, below. Null for a legacy row not reconciled with a GEMI record.
* ``in_latest_source``: True when the activity was in the company's latest reconciled observation, False
  for a row kept after its activity stopped being published, null when never reconciled.
* ``legacy_listed``: True for exactly the rows the legacy importer would hold for the latest observation.

Identity
--------
One row per (company, ``source_key``). The key hashes the activity code (digits, like the legacy ``code``
column), the published KAD version, the type exactly as published and the A3 key of ``dtFrom``:

* the same code in KAD 2008 and KAD 2026 is two rows, and so are two periods of one code;
* ``dtFrom`` identifies a period, while ``dtTo`` and the description are metadata updated in place, so
  ending an activity keeps its row and primary key;
* an entry repeating an identity is collapsed (the first in source order wins) and counted; the
  development data had none.

The legacy constraint "one row per (company, code, activity_type)" now applies to ``legacy_listed`` rows;
a second partial unique constraint enforces (company, source_key).

Legacy projection
-----------------
For one observation, the first entry in source order for each (code, type) is ``legacy_listed``, with the
legacy row's exact code, description and type strings -- the rows the legacy importer created, nothing
more. Legacy reads filter on it: Radar matching while GEMI_MATCH_CURRENT_ACTIVITIES_ONLY is off, the Radar
preview, the dashboard KAD filter, company detail, and the Superadmin KAD filters and activity count.

Upsert (``sync_company_activities``)
------------------------------------
For one company's latest validated activity list, in one transaction:

1. canonicalise the entries (A3 dates and currentness, legacy projection, identity);
2. match existing rows by ``source_key``; a legacy row not yet reconciled is adopted through its
   (code, type), listed activity first, keeping its primary key;
3. update changed metadata in place, and create rows only for new identities;
4. keep rows whose activity is no longer published, with ``in_latest_source`` and ``legacy_listed``
   False -- invisible to legacy reads exactly as the legacy delete made them, but not destroyed.

Nothing is deleted, and an unchanged payload writes nothing, so repeated imports and retries keep primary
keys and never duplicate rows. Demotions are written first, then other updates, then inserts, so the
partial unique constraints hold at every statement. The fallback ActivityCode entries for unknown codes
are created exactly as before. The A5 GemiKad table is not consulted: unknown codes and versions are
accepted, and ingestion never depends on reference data being synchronised.

Backfill (``backfill_company_activities``)
------------------------------------------
Reconciles existing rows with the company's stored ``raw_data`` when it is a GEMI record for that company.
Stricter than the upsert, so that the backfill on its own cannot change any legacy read:

* a legacy row is adopted only by the listed activity of its (code, type);
* its code, description, type and ``legacy_listed`` are never changed;
* every other source activity becomes a new row with ``legacy_listed`` False;
* a row that cannot be mapped (no trustworthy record, or no source activity with its code and type) is
  left untouched and counted as unresolved.

It works in batches by company primary key, one transaction per batch, and is resumable, dry-run capable
and idempotent. It never calls GEMI.

Matching flag
-------------
``GEMI_MATCH_CURRENT_ACTIVITIES_ONLY``, off by default. Off: a row participates in Radar matching iff it
is ``legacy_listed`` -- today's behaviour. On: a row participates iff ``is_current`` is True,
``in_latest_source`` is True and ``kad_version`` is the nomenclature of the Radar KAD catalogue
(ActivityCode, KAD 2026). Unknown currentness (None), KAD 2008 or unknown versions and unreconciled rows
do not participate: none of them is a verified current activity in the catalogue's nomenclature.
``matching_parity`` compares both semantics for every Radar without changing anything.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields
from datetime import date
from typing import Any, Mapping

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..kad import display_kad_code, normalize_kad_code, normalize_kad_search
from .normalizer import NormalizedDate, normalize_activity_entry, normalize_identifier, normalized_date_key
from .schemas import IDENTIFIER

logger = logging.getLogger(__name__)

SOURCE_KEY_VERSION = 1
# The nomenclature of the ActivityCode catalogue Radars choose from. Changes with the A8 catalogue cutover.
RADAR_CATALOGUE_KAD_VERSIONS = frozenset({"kad_2026"})
ACTIVITY_TYPES = {"ΚΥΡΙΑ": "primary", "ΔΕΥΤΕΡΕΥΟΥΣΑ": "secondary", "ΒΟΗΘΗΤΙΚΗ": "auxiliary", "ΛΟΙΠΗ": "other"}
UNKNOWN_ACTIVITY_TYPE = "unknown"
KAD_VERSION_MAX_LENGTH = 32
CANONICAL_FIELDS = (
    "activity_type_normalized", "kad_version", "date_from", "date_from_quality", "date_to", "date_to_quality",
    "is_current", "current_as_of", "source_key", "in_latest_source",
)
IMPORT = "import"
BACKFILL = "backfill"


def normalize_activity_type(value: Any) -> str | None:
    text = normalize_kad_search(value)
    if not text:
        return None
    return ACTIVITY_TYPES.get(text, UNKNOWN_ACTIVITY_TYPE)


def activity_source_key(*, code: str, kad_version: str | None, activity_type: str, date_from: NormalizedDate) -> str:
    material = json.dumps(
        [SOURCE_KEY_VERSION, code, kad_version, activity_type, normalized_date_key(date_from)],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanonicalActivity:
    code: str
    activity_type: str
    description: str
    activity_type_normalized: str | None
    kad_version: str | None
    date_from: NormalizedDate
    date_to: NormalizedDate
    is_current: bool | None
    source_key: str
    legacy_listed: bool

    @property
    def currentness(self) -> str:
        return "unknown" if self.is_current is None else ("current" if self.is_current else "ended")

    def canonical_values(self, as_of: date) -> dict[str, Any]:
        return {
            "activity_type_normalized": self.activity_type_normalized,
            "kad_version": self.kad_version[:KAD_VERSION_MAX_LENGTH] if self.kad_version else None,
            "date_from": self.date_from.value,
            "date_from_quality": self.date_from.quality.value,
            "date_to": self.date_to.value,
            "date_to_quality": self.date_to.quality.value,
            "is_current": self.is_current,
            "current_as_of": as_of,
            "source_key": self.source_key,
            "in_latest_source": True,
        }


@dataclass(frozen=True)
class CanonicalActivities:
    activities: tuple[CanonicalActivity, ...] = ()
    entries: int = 0
    without_code: int = 0
    collapsed: int = 0
    malformed_list: bool = False


def canonicalize_activities(entries: Any, *, as_of: date) -> CanonicalActivities:
    """The canonical activities of one GEMI ``activities`` list, in source order."""
    if entries is None:
        return CanonicalActivities()
    if not isinstance(entries, list):
        return CanonicalActivities(malformed_list=True)
    activities: list[CanonicalActivity] = []
    keys: set[str] = set()
    groups: set[tuple[str, str]] = set()
    without_code = collapsed = 0
    for entry in entries:
        normalized = normalize_activity_entry(entry, as_of=as_of)
        # The legacy importer's code: digits of the published id; an id without digits was skipped.
        code = normalize_kad_code(entry["activity"].get("id")) if normalized is not None else ""
        if not code:
            without_code += 1
            continue
        activity_type = str(entry.get("type") or "")
        key = activity_source_key(
            code=code, kad_version=normalized.kad_version, activity_type=activity_type, date_from=normalized.date_from,
        )
        if key in keys:
            collapsed += 1
            continue
        keys.add(key)
        activities.append(CanonicalActivity(
            code=code,
            activity_type=activity_type,
            description=str(entry["activity"].get("descr") or ""),
            activity_type_normalized=normalize_activity_type(entry.get("type")),
            kad_version=normalized.kad_version,
            date_from=normalized.date_from,
            date_to=normalized.date_to,
            is_current=normalized.is_current,
            source_key=key,
            legacy_listed=(code, activity_type) not in groups,
        ))
        groups.add((code, activity_type))
    return CanonicalActivities(tuple(activities), len(entries), without_code, collapsed)


@dataclass
class ActivityCounts:
    source_entries: int = 0
    without_code: int = 0
    collapsed: int = 0
    malformed_lists: int = 0
    canonical: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    matched_existing_rows: int = 0
    adopted_legacy_rows: int = 0
    preserved_absent: int = 0
    unresolved_legacy_rows: int = 0
    listed_activities_without_legacy_row: int = 0
    description_mismatches: int = 0
    currentness: Counter = field(default_factory=Counter)
    kad_versions: Counter = field(default_factory=Counter)
    activity_types: Counter = field(default_factory=Counter)

    def add(self, other: ActivityCounts) -> None:
        for item in fields(self):
            value = getattr(other, item.name)
            if isinstance(value, Counter):
                getattr(self, item.name).update(value)
            else:
                setattr(self, item.name, getattr(self, item.name) + value)


@dataclass
class _Plan:
    demote: list = field(default_factory=list)
    update: list = field(default_factory=list)
    create: list = field(default_factory=list)
    counts: ActivityCounts = field(default_factory=ActivityCounts)


def _stage(plan: _Plan, row, values: Mapping[str, Any]) -> None:
    changed = [name for name, value in values.items() if getattr(row, name) != value]
    if not changed:
        plan.counts.unchanged += 1
        return
    demoting = row.legacy_listed and values.get("legacy_listed") is False
    for name in changed:
        setattr(row, name, values[name])
    (plan.demote if demoting else plan.update).append((row, changed))
    plan.counts.updated += 1


def _plan(company_id: int, rows, canonical: CanonicalActivities, *, as_of: date, mode: str) -> _Plan:
    CompanyActivity = apps.get_model("gemiapp", "CompanyActivity")
    plan = _Plan()
    counts = plan.counts
    counts.source_entries += canonical.entries
    counts.without_code += canonical.without_code
    counts.collapsed += canonical.collapsed
    counts.malformed_lists += int(canonical.malformed_list)

    rows = sorted(rows, key=lambda row: row.pk)
    by_key = {row.source_key: row for row in rows if row.source_key}
    unkeyed = defaultdict(list)
    for row in rows:
        if not row.source_key:
            unkeyed[(row.code, row.activity_type)].append(row)
    matched = {activity.source_key: by_key[activity.source_key] for activity in canonical.activities if activity.source_key in by_key}
    for activity in sorted(canonical.activities, key=lambda item: not item.legacy_listed):
        if activity.source_key in matched or (mode == BACKFILL and not activity.legacy_listed):
            continue
        candidates = unkeyed.get((activity.code, activity.activity_type))
        if candidates:
            matched[activity.source_key] = candidates.pop(0)
            counts.adopted_legacy_rows += 1

    matched_pks = set()
    for activity in canonical.activities:
        counts.canonical += 1
        counts.currentness[activity.currentness] += 1
        counts.kad_versions[activity.kad_version or "none"] += 1
        counts.activity_types[activity.activity_type_normalized or "missing"] += 1
        values = activity.canonical_values(as_of)
        row = matched.get(activity.source_key)
        if row is None:
            if mode == BACKFILL and activity.legacy_listed:
                counts.listed_activities_without_legacy_row += 1
            plan.create.append(CompanyActivity(
                company_id=company_id, code=activity.code, description=activity.description,
                activity_type=activity.activity_type, legacy_listed=activity.legacy_listed and mode == IMPORT, **values,
            ))
            counts.created += 1
            continue
        matched_pks.add(row.pk)
        counts.matched_existing_rows += 1
        if mode == IMPORT:
            values["description"] = activity.description
            values["legacy_listed"] = activity.legacy_listed
        elif row.description != activity.description:
            counts.description_mismatches += 1
        _stage(plan, row, values)

    for row in rows:
        if row.pk in matched_pks:
            continue
        if row.source_key is None:
            counts.unresolved_legacy_rows += 1
        else:
            counts.preserved_absent += 1
        if mode == IMPORT:
            _stage(plan, row, {"in_latest_source": False, "legacy_listed": False})
    return plan


def _write(plans) -> None:
    CompanyActivity = apps.get_model("gemiapp", "CompanyActivity")
    for bucket in ("demote", "update"):
        rows, changed = [], set()
        for plan in plans:
            for row, names in getattr(plan, bucket):
                rows.append(row)
                changed.update(names)
        if rows:
            CompanyActivity.objects.bulk_update(rows, sorted(changed), batch_size=500)
    created = [row for plan in plans for row in plan.create]
    if created:
        CompanyActivity.objects.bulk_create(created, batch_size=500)


def _ensure_catalogue_entries(canonical: CanonicalActivities) -> None:
    """The legacy importer's fallback: a catalogue entry for every code it stored, first description."""
    ActivityCode = apps.get_model("gemiapp", "ActivityCode")
    fallbacks: dict[str, str] = {}
    for activity in canonical.activities:
        if activity.legacy_listed:
            fallbacks.setdefault(activity.code, activity.description)
    if not fallbacks:
        return
    existing = set(ActivityCode.objects.filter(normalized_code__in=fallbacks).values_list("normalized_code", flat=True))
    ActivityCode.objects.bulk_create([
        ActivityCode(
            code=display_kad_code(code),
            normalized_code=code,
            description=description or "Δραστηριότητα ΓΕΜΗ",
            search_text=normalize_kad_search(f"{display_kad_code(code)} {code} {description}"),
        )
        for code, description in fallbacks.items() if code not in existing
    ], ignore_conflicts=True)


def sync_company_activities(company, source_activities: Any, *, as_of: date | None = None) -> ActivityCounts:
    """Persist one company's latest validated GEMI activity list by diff and upsert (module docstring).
    ``as_of`` is the observation day and defaults to today."""
    CompanyActivity = apps.get_model("gemiapp", "CompanyActivity")
    as_of = timezone.localdate() if as_of is None else as_of
    canonical = canonicalize_activities(source_activities, as_of=as_of)
    with transaction.atomic():
        rows = list(CompanyActivity.objects.select_for_update().filter(company_id=company.pk).order_by("pk"))
        plan = _plan(company.pk, rows, canonical, as_of=as_of, mode=IMPORT)
        _write([plan])
        _ensure_catalogue_entries(canonical)
    return plan.counts


# --- matching -----------------------------------------------------------------------------------

def resolve_current_activities_only(value: bool | None = None) -> bool:
    """``value`` when given, otherwise the GEMI_MATCH_CURRENT_ACTIVITIES_ONLY setting."""
    if value is None:
        return bool(getattr(settings, "GEMI_MATCH_CURRENT_ACTIVITIES_ONLY", False))
    return bool(value)


def activity_participates_in_matching(activity, *, current_only: bool | None = None) -> bool:
    if not resolve_current_activities_only(current_only):
        return bool(activity.legacy_listed)
    return (
        activity.is_current is True
        and activity.in_latest_source is True
        and activity.kad_version in RADAR_CATALOGUE_KAD_VERSIONS
    )


def matching_activity_filter(*, prefix: str = "activity_records__", current_only: bool | None = None) -> dict[str, Any]:
    """Lookups selecting the participating rows; use them in the same ``filter()`` call as the code lookup."""
    if not resolve_current_activities_only(current_only):
        return {f"{prefix}legacy_listed": True}
    return {
        f"{prefix}is_current": True,
        f"{prefix}in_latest_source": True,
        f"{prefix}kad_version__in": sorted(RADAR_CATALOGUE_KAD_VERSIONS),
    }


def non_participation_reason(activity) -> str | None:
    """Why a row does not take part in current-only matching; None when it does."""
    if activity.source_key is None:
        return "unreconciled_legacy_row"
    if activity.in_latest_source is not True:
        return "absent_from_latest_source"
    if activity.is_current is None:
        return "unknown_currentness"
    if activity.is_current is False:
        return "ended"
    if activity.kad_version not in RADAR_CATALOGUE_KAD_VERSIONS:
        return "kad_version_not_in_catalogue"
    return None


# --- backfill -----------------------------------------------------------------------------------

def _is_gemi_record(raw_data: Any, gemi_number: str) -> bool:
    if not isinstance(raw_data, Mapping):
        return False
    ar_gemi = raw_data.get("arGemi")
    return IDENTIFIER.accepts(ar_gemi) and normalize_identifier(ar_gemi) == gemi_number


@dataclass
class ActivityBackfillReport:
    dry_run: bool
    companies_inspected: int = 0
    companies_with_gemi_record: int = 0
    companies_without_gemi_record: int = 0
    companies_with_both_kad_versions: int = 0
    rows_before: int = 0
    batches: int = 0
    last_company_id: int | None = None
    row_errors: int = 0
    counts: ActivityCounts = field(default_factory=ActivityCounts)

    @property
    def rows_after(self) -> int:
        return self.rows_before + self.counts.created

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        c = self.counts
        created = "would create" if self.dry_run else "created"
        updated = "would update" if self.dry_run else "updated"
        types = ("primary", "secondary", "auxiliary", "other", "unknown", "missing")
        return [
            f"{prefix}companies inspected={self.companies_inspected} with_gemi_record={self.companies_with_gemi_record} "
            f"without_gemi_record={self.companies_without_gemi_record} malformed_activity_lists={c.malformed_lists} "
            f"batches={self.batches} last_company_id={self.last_company_id} row_errors={self.row_errors}",
            f"{prefix}activity rows before={self.rows_before} after={self.rows_after}"
            f"{' (projected)' if self.dry_run else ''} deleted=0",
            f"{prefix}source activities observed={c.source_entries} without_code={c.without_code} "
            f"duplicates_collapsed={c.collapsed} canonical={c.canonical}",
            f"{prefix}rows {created}={c.created} {updated}={c.updated} unchanged={c.unchanged} "
            f"mapped_to_existing={c.matched_existing_rows} legacy_rows_adopted={c.adopted_legacy_rows} "
            f"unresolved_legacy_rows_preserved={c.unresolved_legacy_rows} absent_rows_preserved={c.preserved_absent}",
            f"{prefix}listed activities without a legacy row (created outside the legacy list)="
            f"{c.listed_activities_without_legacy_row} legacy description mismatches (kept)={c.description_mismatches}",
            f"{prefix}currentness current={c.currentness['current']} ended={c.currentness['ended']} "
            f"unknown={c.currentness['unknown']}",
            f"{prefix}kad versions kad_2008={c.kad_versions['kad_2008']} kad_2026={c.kad_versions['kad_2026']} "
            f"none={c.kad_versions['none']} other={sum(v for k, v in c.kad_versions.items() if k not in ('kad_2008', 'kad_2026', 'none'))} "
            f"companies_with_both={self.companies_with_both_kad_versions}",
            f"{prefix}activity types " + " ".join(f"{name}={c.activity_types[name]}" for name in types),
        ]


def backfill_company_activities(*, batch_size: int = 200, dry_run: bool = False, start_company_id: int = 0) -> ActivityBackfillReport:
    """Reconcile existing CompanyActivity rows with stored raw_data, in batches. See the module docstring."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    Company = apps.get_model("gemiapp", "Company")
    CompanyActivity = apps.get_model("gemiapp", "CompanyActivity")
    report = ActivityBackfillReport(dry_run=dry_run)
    last_id = start_company_id
    while True:
        companies = list(
            Company.objects.filter(pk__gt=last_id).order_by("pk")
            .values("pk", "gemi_number", "raw_data", "updated_at")[:batch_size]
        )
        if not companies:
            break
        rows_by_company = defaultdict(list)
        for row in CompanyActivity.objects.filter(company_id__in=[item["pk"] for item in companies]).order_by("pk"):
            rows_by_company[row.company_id].append(row)
        plans = []
        for company in companies:
            report.companies_inspected += 1
            rows = rows_by_company.get(company["pk"], [])
            report.rows_before += len(rows)
            try:
                if not _is_gemi_record(company["raw_data"], company["gemi_number"]):
                    report.companies_without_gemi_record += 1
                    plan = _plan(company["pk"], rows, CanonicalActivities(), as_of=date.min, mode=BACKFILL)
                else:
                    report.companies_with_gemi_record += 1
                    as_of = timezone.localdate(company["updated_at"])
                    canonical = canonicalize_activities(company["raw_data"].get("activities"), as_of=as_of)
                    plan = _plan(company["pk"], rows, canonical, as_of=as_of, mode=BACKFILL)
                    versions = {activity.kad_version for activity in canonical.activities}
                    report.companies_with_both_kad_versions += int({"kad_2008", "kad_2026"} <= versions)
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                report.row_errors += 1
                logger.warning("Company %s: activity metadata could not be derived (%s); its rows are unchanged.", company["pk"], type(exc).__name__)
                continue
            report.counts.add(plan.counts)
            plans.append(plan)
        if not dry_run:
            with transaction.atomic():
                _write(plans)
        report.batches += 1
        last_id = report.last_company_id = companies[-1]["pk"]
        logger.info(
            "GEMI activity backfill%s: batch %s up to company %s, %s companies, %s rows %s so far.",
            " (dry run)" if dry_run else "", report.batches, last_id, report.companies_inspected,
            report.counts.created, "would be created" if dry_run else "created",
        )
    return report


# --- parity -------------------------------------------------------------------------------------

@dataclass
class RadarParity:
    radar_id: int
    is_active: bool
    activity_codes: int
    legacy: int = 0
    current_only: int = 0
    removed: list[int] = field(default_factory=list)
    added: list[int] = field(default_factory=list)
    removed_reasons: Counter = field(default_factory=Counter)
    added_reasons: Counter = field(default_factory=Counter)
    existing_matches: int = 0
    existing_matches_not_current_only: int = 0

    @property
    def affected(self) -> bool:
        return bool(self.removed or self.added)

    @property
    def change_pct(self) -> float | None:
        if not self.legacy:
            return None if self.current_only else 0.0
        return (self.current_only - self.legacy) * 100 / self.legacy


def matching_parity(*, chunk_size: int = 2000) -> list[RadarParity]:
    """Every non-deleted Radar evaluated over every company with both activity semantics. Read-only: the
    live predicate is called with the mode forced, and nothing is written."""
    from ..services import company_matches_radar

    Company = apps.get_model("gemiapp", "Company")
    CustomerRadar = apps.get_model("gemiapp", "CustomerRadar")
    RadarMatch = apps.get_model("gemiapp", "RadarMatch")
    radars = list(CustomerRadar.objects.filter(deleted_at__isnull=True).prefetch_related("activity_codes").order_by("pk"))
    wanted = {radar.pk: {item.normalized_code for item in radar.activity_codes.all()} for radar in radars}
    results = {radar.pk: RadarParity(radar.pk, radar.is_active, len(wanted[radar.pk])) for radar in radars}
    legacy_ids = defaultdict(set)
    current_ids = defaultdict(set)
    companies = Company.objects.order_by("pk").prefetch_related("activity_records")
    for company in companies.iterator(chunk_size=chunk_size):
        for radar in radars:
            result = results[radar.pk]
            legacy, _ = company_matches_radar(company, radar, current_activities_only=False)
            current, _ = company_matches_radar(company, radar, current_activities_only=True)
            if legacy:
                legacy_ids[radar.pk].add(company.pk)
            if current:
                current_ids[radar.pk].add(company.pk)
            if legacy and not current:
                result.removed.append(company.pk)
                reasons = {
                    non_participation_reason(activity) or "participating"
                    for activity in company.activity_records.all()
                    if activity.legacy_listed and activity.code in wanted[radar.pk]
                }
                result.removed_reasons["+".join(sorted(reasons))] += 1
            elif current and not legacy:
                result.added.append(company.pk)
                result.added_reasons["canonical_row_not_legacy_listed"] += 1
    for radar in radars:
        result = results[radar.pk]
        result.legacy = len(legacy_ids[radar.pk])
        result.current_only = len(current_ids[radar.pk])
        matched = set(RadarMatch.objects.filter(radar=radar).values_list("company_id", flat=True))
        result.existing_matches = len(matched)
        result.existing_matches_not_current_only = len((matched & legacy_ids[radar.pk]) - current_ids[radar.pk])
    return [results[radar.pk] for radar in radars]
