"""GEMI reference data: local canonical copies of the seven metadata endpoints.

Families and tables
-------------------
``activities`` -> GemiKad (identity: code + KAD version), ``prefectures`` -> GemiPrefecture,
``municipalities`` -> GemiMunicipality, ``company_statuses`` -> GemiCompanyStatus, ``legal_types`` ->
GemiLegalType, ``gemi_offices`` -> GemiOffice, ``decision_subjects`` -> GemiDecisionSubject. Every other
table's identity is the source id. These tables are new: ActivityCode, CompanyActivity, Company,
Radar criteria and digests are not read or changed, and nothing in the live application reads these
tables yet.

Stored per item: source id, description, English description, the source's own active flag (only
companyStatuses publishes one), the source's lastUpdated text, plus KAD version (activities) or
upstream prefecture id (municipalities, a plain identifier -- see GemiMunicipality). GEMI office
address and contact fields are not stored. Text and ids follow the A3 normaliser rules: NFC, collapsed
whitespace, blank or null -> None, ids never invented.

Sync algorithm (``sync_reference_data``)
----------------------------------------
1. Fetch every selected endpoint through the shared GemiClient in the lowest-priority lane, each
   validated against its A2 response family. Nothing is written to reference tables in this phase;
   any HTTP, budget or validation failure ends the sync here with every table untouched.
2. Build the proposed canonical state in memory. Exact duplicate items collapse; conflicting items
   with the same identity keep the one whose canonical JSON sorts first and are reported as an anomaly.
3. Compare with the local rows: create, update (content changed), reappear (retired row back in the
   source), unchanged, retire (present locally, absent from the source).
4. Guard against a collapsed response: if a family that has present rows comes back empty, or with
   fewer than half as many items, a warning is logged and that family's retirements are skipped
   (creates and updates still apply) unless ``force_retire`` is set. Counts alone never fail a sync.
5. Apply every family's changes in one database transaction: bulk creates, bulk updates, last-seen
   timestamps and retirements. If anything fails, the whole transaction rolls back.

A sync takes a lock in the shared database cache so two runs cannot interleave. Each non-dry run is
logged in GemiReferenceSyncRun (status, families, counts, anomalies, duration) and in the application
log -- counts and family names only, never payloads.

Retirement: a disappeared item is kept with ``is_present=False`` and ``retired_at`` set; if it returns
it is revived in place (same row). Retiring is not the same as the source's own inactive flag.

Dry run: fetches and validates, computes the same counts and anomalies, writes nothing -- no reference
rows, no run row, and no source records (its client has no recorder, whatever
GEMI_SOURCE_RECORDS_ENABLED says). A normal run uses ``get_gemi_client()``, so reference retrievals are
recorded as source records only when that setting is on; the sync does not depend on it.

Scheduling: intended weekly, but not scheduled -- production scheduling follows the G0/G1 release gates.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator, Mapping

from django.apps import apps
from django.core.cache import caches
from django.db import transaction
from django.utils import timezone

from .client import GemiClient, get_gemi_client
from .normalizer import normalize_identifier, normalize_text
from .rate_budget import GemiLane
from .schemas import ResponseFamily

logger = logging.getLogger(__name__)

# The lowest-priority lane: a weekly reference refresh must never delay discovery, digest-feeding
# imports or monitored refresh. (The lane is named for documents; reference sync shares it.)
REFERENCE_SYNC_LANE = GemiLane.DOCUMENTS
REFERENCE_SYNC_MAX_WAIT_SECONDS = 900.0
REFERENCE_SYNC_LOCK_KEY = "gemi-reference-sync-lock"
REFERENCE_SYNC_LOCK_SECONDS = 3600
# A family whose response holds fewer than this share of the locally present rows retires nothing.
RETIREMENT_GUARD_RATIO = 0.5
_BATCH = 500
_CANONICAL_FIELDS = ("description", "description_en", "source_is_active", "source_last_updated")


@dataclass(frozen=True)
class ReferenceFamily:
    key: str
    endpoint: str
    response_family: ResponseFamily
    model_name: str
    identity_fields: tuple[str, ...] = ("source_id",)
    extra_fields: tuple[str, ...] = ()

    @property
    def model(self):
        return apps.get_model("gemiapp", self.model_name)

    @property
    def compare_fields(self) -> tuple[str, ...]:
        return _CANONICAL_FIELDS + tuple(name for name in self.extra_fields if name not in self.identity_fields)


REFERENCE_FAMILIES: tuple[ReferenceFamily, ...] = (
    ReferenceFamily("activities", "/metadata/activities", ResponseFamily.ACTIVITIES, "GemiKad",
                    identity_fields=("source_id", "kad_version"), extra_fields=("kad_version",)),
    ReferenceFamily("prefectures", "/metadata/prefectures", ResponseFamily.PREFECTURES, "GemiPrefecture"),
    ReferenceFamily("municipalities", "/metadata/municipalities", ResponseFamily.MUNICIPALITIES, "GemiMunicipality",
                    extra_fields=("source_prefecture_id",)),
    ReferenceFamily("company_statuses", "/metadata/companyStatuses", ResponseFamily.COMPANY_STATUSES, "GemiCompanyStatus"),
    ReferenceFamily("legal_types", "/metadata/legalTypes", ResponseFamily.LEGAL_TYPES, "GemiLegalType"),
    ReferenceFamily("gemi_offices", "/metadata/gemiOffices", ResponseFamily.GEMI_OFFICES, "GemiOffice"),
    ReferenceFamily("decision_subjects", "/metadata/assemblySubjects", ResponseFamily.DECISION_SUBJECTS, "GemiDecisionSubject"),
)
FAMILIES_BY_KEY = {family.key: family for family in REFERENCE_FAMILIES}
REFERENCE_FAMILY_KEYS = tuple(FAMILIES_BY_KEY)


@dataclass
class FamilySyncCounts:
    fetched: int = 0
    created: int = 0
    updated: int = 0
    reappeared: int = 0
    unchanged: int = 0
    retired: int = 0
    conflicting_duplicates: int = 0
    retirement_skipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReferenceSyncResult:
    dry_run: bool
    families: dict[str, FamilySyncCounts]
    anomalies: list[str]
    run_id: int | None = None


@dataclass
class _FamilyPlan:
    family: ReferenceFamily
    counts: FamilySyncCounts
    creates: list[dict[str, Any]] = field(default_factory=list)
    changes: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)  # updates and reappearances
    unchanged: list[Any] = field(default_factory=list)
    retires: list[Any] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)


def canonical_entry(family: ReferenceFamily, item: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical local fields for one A2-validated metadata item."""
    source_id = normalize_identifier(item.get("id"))
    if source_id is None:
        raise ValueError(f"{family.key}: a reference item without an id reached the sync; validate it first.")
    last_updated = normalize_text(item.get("lastUpdated"))
    entry: dict[str, Any] = {
        "source_id": source_id,
        "description": normalize_text(item.get("descr")),
        "description_en": normalize_text(item.get("descrEn")),
        "source_is_active": item["isActive"] if isinstance(item.get("isActive"), bool) else None,
        "source_last_updated": last_updated[:32] if last_updated else None,
    }
    if "kad_version" in family.extra_fields:
        entry["kad_version"] = normalize_text(item.get("kadVersion")) or ""
    if "source_prefecture_id" in family.extra_fields:
        entry["source_prefecture_id"] = normalize_identifier(item.get("prefectureId"))
    return entry


def _identity(family: ReferenceFamily, values: Mapping[str, Any] | Any) -> tuple:
    if isinstance(values, Mapping):
        return tuple(values[name] for name in family.identity_fields)
    return tuple(getattr(values, name) for name in family.identity_fields)


def _proposed_state(family: ReferenceFamily, payload: list) -> tuple[dict[tuple, dict[str, Any]], int]:
    proposed: dict[tuple, dict[str, Any]] = {}
    conflicts = 0
    for item in payload:
        entry = canonical_entry(family, item)
        key = _identity(family, entry)
        existing = proposed.get(key)
        if existing is not None and existing != entry:
            conflicts += 1
            entry = min(existing, entry, key=lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False))
        proposed[key] = entry
    return proposed, conflicts


def _plan(family: ReferenceFamily, payload: list, *, force_retire: bool) -> _FamilyPlan:
    proposed, conflicts = _proposed_state(family, payload)
    existing = {_identity(family, row): row for row in family.model.objects.all()}
    plan = _FamilyPlan(family, FamilySyncCounts(fetched=len(payload), conflicting_duplicates=conflicts))

    for key, entry in proposed.items():
        row = existing.get(key)
        if row is None:
            plan.creates.append(entry)
        elif not row.is_present:
            plan.changes.append((row, entry))
            plan.counts.reappeared += 1
        elif any(getattr(row, name) != entry[name] for name in family.compare_fields):
            plan.changes.append((row, entry))
            plan.counts.updated += 1
        else:
            plan.unchanged.append(row)

    absent = [row for key, row in existing.items() if key not in proposed and row.is_present]
    present_before = sum(1 for row in existing.values() if row.is_present)
    if conflicts:
        plan.anomalies.append(f"{family.key}: {conflicts} conflicting item(s) shared an identity; the first by canonical order was kept")
    collapsed = present_before and (not proposed or len(proposed) < present_before * RETIREMENT_GUARD_RATIO)
    if collapsed:
        plan.anomalies.append(
            f"{family.key}: the source returned {len(proposed)} item(s) while {present_before} are present locally"
            + ("" if force_retire else "; retirements skipped")
        )
    if absent and collapsed and not force_retire:
        plan.counts.retirement_skipped = True
    else:
        plan.retires = absent

    plan.counts.created = len(plan.creates)
    plan.counts.unchanged = len(plan.unchanged)
    plan.counts.retired = len(plan.retires)
    return plan


def _chunks(values: list, size: int = _BATCH) -> Iterator[list]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _apply(plans: Iterable[_FamilyPlan], now) -> None:
    with transaction.atomic():
        for plan in plans:
            family, model = plan.family, plan.family.model
            model.objects.bulk_create(
                [model(**entry, is_present=True, first_seen_at=now, last_seen_at=now) for entry in plan.creates],
                batch_size=_BATCH,
            )
            changed = []
            for row, entry in plan.changes:
                for name, value in entry.items():
                    setattr(row, name, value)
                row.is_present, row.retired_at, row.last_seen_at, row.updated_at = True, None, now, now
                changed.append(row)
            if changed:
                model.objects.bulk_update(
                    changed, [*family.compare_fields, "is_present", "retired_at", "last_seen_at", "updated_at"], batch_size=_BATCH,
                )
            for chunk in _chunks([row.pk for row in plan.unchanged]):
                model.objects.filter(pk__in=chunk).update(last_seen_at=now)
            for chunk in _chunks([row.pk for row in plan.retires]):
                model.objects.filter(pk__in=chunk).update(is_present=False, retired_at=now, updated_at=now)


def _select(families: Iterable[str] | None) -> tuple[ReferenceFamily, ...]:
    if not families:
        return REFERENCE_FAMILIES
    wanted = set(families)
    unknown = wanted - set(FAMILIES_BY_KEY)
    if unknown:
        raise ValueError(f"Unknown reference families: {', '.join(sorted(unknown))}")
    return tuple(family for family in REFERENCE_FAMILIES if family.key in wanted)


def _default_client(dry_run: bool) -> GemiClient:
    # A dry run must write nothing, so it never gets a source recorder.
    return GemiClient() if dry_run else get_gemi_client()


def sync_reference_data(
    families: Iterable[str] | None = None,
    *,
    dry_run: bool = False,
    force_retire: bool = False,
    client: GemiClient | None = None,
) -> ReferenceSyncResult:
    """Synchronise the selected (default: all) reference families. See the module docstring."""
    selected = _select(families)
    client = client if client is not None else _default_client(dry_run)
    lock = caches["shared"]
    if not dry_run and not lock.add(REFERENCE_SYNC_LOCK_KEY, "1", REFERENCE_SYNC_LOCK_SECONDS):
        raise RuntimeError("Ένα άλλο GEMI reference sync εκτελείται ήδη.")

    run_model = apps.get_model("gemiapp", "GemiReferenceSyncRun")
    run = None if dry_run else run_model.objects.create(families=[family.key for family in selected])
    started = time.monotonic()
    try:
        payloads = {
            family.key: client.get(
                family.endpoint, lane=REFERENCE_SYNC_LANE, max_wait=REFERENCE_SYNC_MAX_WAIT_SECONDS,
                family=family.response_family,
            )
            for family in selected
        }
        plans = [_plan(family, payloads[family.key], force_retire=force_retire) for family in selected]
        if not dry_run:
            _apply(plans, timezone.now())
    except Exception as exc:
        logger.error(
            "GEMI reference sync failed after %.1fs (%s); no reference table was changed.",
            time.monotonic() - started, type(exc).__name__,
        )
        if run is not None:
            run.status, run.error_message, run.finished_at = "failed", str(exc)[:2000], timezone.now()
            run.save(update_fields=["status", "error_message", "finished_at"])
        raise
    finally:
        if not dry_run:
            lock.delete(REFERENCE_SYNC_LOCK_KEY)

    result = ReferenceSyncResult(
        dry_run=dry_run,
        families={plan.family.key: plan.counts for plan in plans},
        anomalies=[anomaly for plan in plans for anomaly in plan.anomalies],
        run_id=run.pk if run is not None else None,
    )
    for anomaly in result.anomalies:
        logger.warning("GEMI reference sync anomaly: %s.", anomaly)
    for key, counts in result.families.items():
        logger.info(
            "GEMI reference sync%s: %s fetched=%s created=%s updated=%s reappeared=%s unchanged=%s retired=%s%s.",
            " (dry run)" if dry_run else "", key, counts.fetched, counts.created, counts.updated, counts.reappeared,
            counts.unchanged, counts.retired, " retirement_skipped" if counts.retirement_skipped else "",
        )
    if run is not None:
        run.status, run.finished_at = "success", timezone.now()
        run.counts = {key: counts.as_dict() for key, counts in result.families.items()}
        run.anomalies = result.anomalies
        run.save(update_fields=["status", "finished_at", "counts", "anomalies"])
    return result
