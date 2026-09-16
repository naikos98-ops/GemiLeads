"""Snapshot change detection and Tier-1 change signals (B5).

B3 records *that* a company's canonical state changed; B5 answers *what* changed. It compares two
consecutive ``CompanySnapshot`` rows of one company and produces SHADOW ``CompanySignal`` events for the
approved Tier-1 types: STATUS_CHANGED, KAD_ADDED, KAD_REMOVED, LEGAL_FORM_CHANGED and LOCATION_CHANGED.

A changed state hash is never a signal by itself. The hash is the broad "something changed" detector; the
field-aware rules below decide which changes are meaningful business events. Many snapshot changes -- a
postal-code correction, a reworded city, a newly populated id, an activity type switching between primary and
secondary -- legitimately produce no signal at all.

Nothing here calls GEMI, schedules anything, runs automatically after a B4 refresh, touches Radars,
matching, monitoring, leads or digests, or notifies anyone.

Architecture
------------
``detect_snapshot_changes(previous, current)`` is the detector: it validates the pair, reads the two
snapshots' business state and returns change candidates plus suppression counts. It performs no query and
no write, so every rule is testable on unsaved instances.

``materialize_snapshot_change_signals(current)`` is the materializer: it selects the canonical predecessor,
calls the detector, records each candidate through B1 ``record_company_signal`` and writes the structured
``CompanySignalSnapshotEvidence``. ``materialize_all_snapshot_change_signals`` batches that over history.

The pair
--------
``previous`` is the immediate predecessor of ``current`` under B3 chronology: the latest snapshot of the same
company with an earlier ``observed_at`` (id breaks a tie). Never an arbitrary older snapshot, never the
baseline when another snapshot lies between. A baseline has no predecessor state and never emits a signal.
Only snapshots of the same, supported schema version are compared; otherwise the pair is reported as
``incompatible_snapshot_schema`` and nothing is guessed.

Event identity (v1, pinned by tests)
------------------------------------
A transition can be reprocessed any number of times and must stay one event, yet a company can genuinely
repeat a transition later (A -> B -> A -> B), and the second A -> B is a new occurrence. The event key
therefore anchors the transition to durable snapshot facts, then names the subject::

    {
      "transition": {"from_state": previous.state_hash, "to_state": current.state_hash,
                     "observed_at": current.observed_at in UTC, ISO-8601},
      "subject": {...}
    }

with subjects ``{"kind": "status" | "legal_form" | "municipality", "before": id, "after": id}`` and
``{"kind": "kad", "code": code, "kad_version": version or null}``. ``current.observed_at`` is source-
observation evidence identifying the occurrence, not processing time. No run id, mode, rule version or
wall-clock value enters it. Changing this shape is an identity migration, not a tweak.

Rules
-----
STATUS_CHANGED (``status_changed:v1``)
    Both status ids known and different. null <-> known is source completeness, not a status change.
    Effective date: the current ``lastStatusChange`` when its A3 quality is VALID (DATE precision);
    otherwise no source time. The observation time is never substituted.

LEGAL_FORM_CHANGED (``legal_form_changed:v1``)
    Both legal-type ids known and different. No source date exists: effective precision NONE.

LOCATION_CHANGED (``location_changed:v1``)
    Municipality level only: both municipality ids known and different. A prefecture, city or postal-code
    change without a municipality change, and null <-> known, produce nothing. Effective precision NONE.

KAD_ADDED / KAD_REMOVED (``kad_added:v1`` / ``kad_removed:v1``)
    Presence identity is ``(code, kad_version)`` over the snapshot's verified-current activities -- not the
    type, the published type, the period or the order. Several entries sharing an identity are present
    once. ``current - previous`` are additions, ``previous - current`` removals, one signal per identity.
    A type-only or period-only change of a code that stays present is not a presence change.

    Effective date: for an addition, the one VALID ``dtFrom`` all supporting current entries agree on; for
    a removal, the one VALID ``dtTo`` all supporting previous entries agree on. Entries without a valid date
    do not vote; disagreeing valid dates, or none at all, leave no source time. Source order never picks one.

Version-quality suppression
    A KAD whose version is unknown on one side and known on the other is metadata becoming more (or less)
    complete, not a removal plus an addition. For each code: an addition or removal with a *known* version
    is suppressed when the opposite snapshot holds the same code with a *null* version, and an addition or
    removal with a *null* version is suppressed when the opposite snapshot holds the same code with a known
    version. A code with a null version and no same-code counterpart on the other side is a genuine
    presence change and is emitted with ``kad_version`` null.

Taxonomy migration suppression (2008 -> 2026)
    GEMI moved activities from KAD 2008 to KAD 2026 at ``KAD_TAXONOMY_TRANSITION_DATE`` (2026-03-01). A
    naive diff would turn that into thousands of false removals and additions. There is no authoritative
    crosswalk, so the rule never matches codes, descriptions or guesses one; it relies on the source's own
    boundary dates:

    * a *boundary removal* is a KAD 2008 removal whose consensus VALID ``dtTo`` is exactly 2026-03-01;
    * a *boundary addition* is a KAD 2026 addition whose consensus VALID ``dtFrom`` is exactly 2026-03-01.

    Boundary removals are suppressed only when the transition carries opposite-side migration evidence:
    the current snapshot holds at least one KAD 2026 entry whose VALID ``dtFrom`` is 2026-03-01 (newly added
    or already present). Boundary additions are suppressed only when the previous snapshot holds at least
    one KAD 2008 entry whose VALID ``dtTo`` is 2026-03-01. A boundary-dated change with no opposite-version
    evidence at all is emitted normally, and any other date -- 2026-06-01, 2026-07-10 -- is never
    suppressed, however close to March the observation was.

Signals
-------
Every signal is recorded through B1 with source type SNAPSHOT_DIFF, confidence 1.0000 (confidence that the
observed transition occurred, not in its legal effective date), ``detected_at = current.observed_at`` and
mode SHADOW, hardcoded here: there is no mode parameter and no live option. Reprocessing reuses the existing
signal with its original detected time; a conflicting identity surfaces B1's conflict and nothing is
overwritten. Each signal has exactly one evidence row naming its exact snapshot pair and subject; an existing
row that disagrees is an invariant failure, never silently replaced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timezone as dt_timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from django.apps import apps
from django.db import transaction
from django.db.models import Q

from .company_signals import (
    KAD_ADDED,
    KAD_REMOVED,
    LEGAL_FORM_CHANGED,
    LOCATION_CHANGED,
    SHADOW,
    SNAPSHOT_DIFF,
    STATUS_CHANGED,
    SignalConflictError,
    build_dedupe_key,
    record_company_signal,
    rule_for,
)
from .company_snapshots import COMPANY_SNAPSHOT_SCHEMA_VERSION
from .ingestion.normalizer import DateQuality

logger = logging.getLogger(__name__)

# Snapshot schema versions whose state B5 v1 knows how to compare. Anything else is never guessed at.
SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS = frozenset({COMPANY_SNAPSHOT_SCHEMA_VERSION})

# The source-taxonomy boundary where GEMI moved activities from KAD 2008 to KAD 2026. Not a business date.
KAD_TAXONOMY_TRANSITION_DATE = date(2026, 3, 1)
KAD_2008 = "kad_2008"
KAD_2026 = "kad_2026"

# Deterministic structured transitions: confidence that the observed change happened.
CONFIDENCE = Decimal("1.0000")

# Evidence subject kinds (mirroring CompanySignalSnapshotEvidence.SUBJECT_KINDS).
SUBJECT_STATUS = "status"
SUBJECT_KAD = "kad"
SUBJECT_LEGAL_FORM = "legal_form"
SUBJECT_MUNICIPALITY = "municipality"

# Detector outcomes.
COMPARED = "compared"
BASELINE = "baseline"
INCOMPATIBLE_SNAPSHOT_SCHEMA = "incompatible_snapshot_schema"

VALID = DateQuality.VALID.value


class SnapshotPairError(ValueError):
    """The two snapshots are not a comparable consecutive pair of one company: a caller bug."""


class SnapshotEvidenceConflictError(SignalConflictError):
    """A signal's existing snapshot evidence names a different pair or subject than this detection."""


@dataclass(frozen=True)
class ChangeCandidate:
    signal_type: str
    subject_kind: str
    event_key: dict
    effective: date | None = None
    before_source_id: str | None = None
    after_source_id: str | None = None
    activity_code: str | None = None
    kad_version: str | None = None

    @property
    def rule_version(self) -> str:
        return rule_for(self.signal_type).rule_version

    def evidence_values(self) -> dict[str, Any]:
        return {
            "subject_kind": self.subject_kind, "before_source_id": self.before_source_id,
            "after_source_id": self.after_source_id, "activity_code": self.activity_code,
            "kad_version": self.kad_version,
        }


@dataclass(frozen=True)
class DetectionResult:
    status: str
    candidates: tuple = ()
    taxonomy_suppressed: int = 0
    version_quality_suppressed: int = 0
    state_changed: bool = False


# --- detector -----------------------------------------------------------------------------------

def transition_anchor(previous, current) -> dict[str, str]:
    """The durable identity of one occurrence of a state transition (event-key v1)."""
    return {
        "from_state": previous.state_hash,
        "to_state": current.state_hash,
        # Normalised to UTC so the same instant read back from any database connection keys identically.
        "observed_at": current.observed_at.astimezone(dt_timezone.utc).isoformat(),
    }


def _event_key(anchor: Mapping[str, str], subject: Mapping[str, Any]) -> dict:
    return {"transition": dict(anchor), "subject": dict(subject)}


def _validate_pair(previous, current) -> None:
    if previous.company_id != current.company_id:
        raise SnapshotPairError("snapshots of different companies cannot be compared")
    if previous.pk is not None and previous.pk == current.pk:
        raise SnapshotPairError("a snapshot cannot be compared with itself")
    same_time_in_id_order = (
        previous.observed_at == current.observed_at and previous.pk is not None and current.pk is not None
        and previous.pk < current.pk
    )
    if previous.observed_at > current.observed_at or (previous.observed_at == current.observed_at and not same_time_in_id_order):
        raise SnapshotPairError("the previous snapshot must be observed before the current one")
    if current.is_baseline:
        raise SnapshotPairError("a baseline snapshot has no predecessor")


def _as_date(value) -> date | None:
    # A snapshot freshly returned by the B3 writer still carries the ISO string it was built from; one read
    # back from the database carries a date. Both mean the same source date.
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _consensus(entries: Iterable[Mapping[str, Any]], value_key: str, quality_key: str) -> date | None:
    """The single VALID date every dated supporting entry agrees on, or None. Undated entries do not vote."""
    values = {entry[value_key] for entry in entries if entry.get(quality_key) == VALID and entry.get(value_key)}
    return date.fromisoformat(values.pop()) if len(values) == 1 else None


def _presence(snapshot) -> dict[tuple, list]:
    present: dict[tuple, list] = {}
    for entry in snapshot.activities_state or []:
        present.setdefault((entry["code"], entry.get("kad_version")), []).append(entry)
    return present


def _versions_by_code(present: Mapping[tuple, list]) -> dict[str, set]:
    versions: dict[str, set] = {}
    for code, version in present:
        versions.setdefault(code, set()).add(version)
    return versions


def _has_boundary_entry(present: Mapping[tuple, list], version: str, value_key: str, quality_key: str) -> bool:
    return any(
        entry.get(quality_key) == VALID and entry.get(value_key) == KAD_TAXONOMY_TRANSITION_DATE.isoformat()
        for (code, kad_version), entries in present.items() if kad_version == version
        for entry in entries
    )


def _reference_candidates(previous, current, anchor) -> list[ChangeCandidate]:
    candidates = []
    for signal_type, field_name, kind in (
        (STATUS_CHANGED, "status_source_id", SUBJECT_STATUS),
        (LEGAL_FORM_CHANGED, "legal_type_source_id", SUBJECT_LEGAL_FORM),
        (LOCATION_CHANGED, "municipality_source_id", SUBJECT_MUNICIPALITY),
    ):
        before, after = getattr(previous, field_name), getattr(current, field_name)
        # Both sides must be known: null <-> known is completeness of the source, not a business change.
        if before is None or after is None or before == after:
            continue
        effective = None
        if signal_type == STATUS_CHANGED and current.last_status_change_quality == VALID:
            effective = _as_date(current.last_status_change)
        candidates.append(ChangeCandidate(
            signal_type=signal_type, subject_kind=kind, effective=effective,
            before_source_id=before, after_source_id=after,
            event_key=_event_key(anchor, {"kind": kind, "before": before, "after": after}),
        ))
    return candidates


def _kad_candidates(previous, current, anchor) -> tuple[list[ChangeCandidate], int, int]:
    before, after = _presence(previous), _presence(current)
    before_versions, after_versions = _versions_by_code(before), _versions_by_code(after)
    added = sorted(set(after) - set(before), key=lambda item: (item[0], item[1] or ""))
    removed = sorted(set(before) - set(after), key=lambda item: (item[0], item[1] or ""))

    # Migration evidence present anywhere on the opposite side of the transition.
    current_has_2026_boundary = _has_boundary_entry(after, KAD_2026, "date_from", "date_from_quality")
    previous_has_2008_boundary = _has_boundary_entry(before, KAD_2008, "date_to", "date_to_quality")

    candidates, taxonomy, version_quality = [], 0, 0
    for signal_type, identities, supporting, opposite_versions in (
        (KAD_ADDED, added, after, before_versions),
        (KAD_REMOVED, removed, before, after_versions),
    ):
        for code, version in identities:
            others = opposite_versions.get(code, set())
            if (version is not None and None in others) or (version is None and others - {None}):
                version_quality += 1
                continue
            entries = supporting[(code, version)]
            if signal_type == KAD_ADDED:
                effective = _consensus(entries, "date_from", "date_from_quality")
                boundary = version == KAD_2026 and effective == KAD_TAXONOMY_TRANSITION_DATE and previous_has_2008_boundary
            else:
                effective = _consensus(entries, "date_to", "date_to_quality")
                boundary = version == KAD_2008 and effective == KAD_TAXONOMY_TRANSITION_DATE and current_has_2026_boundary
            if boundary:
                taxonomy += 1
                continue
            candidates.append(ChangeCandidate(
                signal_type=signal_type, subject_kind=SUBJECT_KAD, effective=effective,
                activity_code=code, kad_version=version,
                event_key=_event_key(anchor, {"kind": SUBJECT_KAD, "code": code, "kad_version": version}),
            ))
    return candidates, taxonomy, version_quality


def detect_snapshot_changes(previous, current) -> DetectionResult:
    """The Tier-1 change candidates between two consecutive snapshots. No query, no write."""
    if previous is None:
        if not current.is_baseline:
            raise SnapshotPairError("a non-baseline snapshot needs its predecessor")
        return DetectionResult(BASELINE)
    _validate_pair(previous, current)
    if previous.schema_version != current.schema_version or current.schema_version not in SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS:
        return DetectionResult(INCOMPATIBLE_SNAPSHOT_SCHEMA, state_changed=previous.state_hash != current.state_hash)
    anchor = transition_anchor(previous, current)
    kads, taxonomy, version_quality = _kad_candidates(previous, current, anchor)
    return DetectionResult(
        COMPARED, candidates=tuple(_reference_candidates(previous, current, anchor) + kads),
        taxonomy_suppressed=taxonomy, version_quality_suppressed=version_quality,
        state_changed=previous.state_hash != current.state_hash,
    )


# --- materializer -------------------------------------------------------------------------------

def immediate_predecessor(snapshot):
    """The canonical predecessor under B3 chronology: latest earlier observation, id as the tie-breaker."""
    CompanySnapshot = apps.get_model("gemiapp", "CompanySnapshot")
    return (
        CompanySnapshot.objects.filter(company_id=snapshot.company_id)
        .filter(Q(observed_at__lt=snapshot.observed_at) | Q(observed_at=snapshot.observed_at, id__lt=snapshot.pk))
        .order_by("-observed_at", "-id").first()
    )


@dataclass
class ChangeReport:
    dry_run: bool
    snapshots_inspected: int = 0
    transitions_inspected: int = 0
    baselines_skipped: int = 0
    incompatible_schema_pairs: int = 0
    missing_predecessors: int = 0
    status_changes: int = 0
    kad_additions: int = 0
    kad_removals: int = 0
    legal_form_changes: int = 0
    location_changes: int = 0
    taxonomy_suppressed: int = 0
    version_quality_suppressed: int = 0
    changed_snapshot_no_tier1_signal: int = 0
    signals_created: int = 0
    signals_reused: int = 0
    conflicts: int = 0
    batches: int = 0
    last_snapshot_id: int | None = None
    conflicting_gemi_numbers: list = field(default_factory=list)

    _TYPE_COUNTERS = {
        STATUS_CHANGED: "status_changes", KAD_ADDED: "kad_additions", KAD_REMOVED: "kad_removals",
        LEGAL_FORM_CHANGED: "legal_form_changes", LOCATION_CHANGED: "location_changes",
    }

    def summary(self) -> dict:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        verb = "would create" if self.dry_run else "created"
        return [
            f"{prefix}snapshots inspected={self.snapshots_inspected} transitions={self.transitions_inspected} "
            f"baselines skipped={self.baselines_skipped} incompatible schema pairs={self.incompatible_schema_pairs} "
            f"missing predecessors={self.missing_predecessors} batches={self.batches} "
            f"last_snapshot_id={self.last_snapshot_id}",
            f"{prefix}status={self.status_changes} kad added={self.kad_additions} kad removed={self.kad_removals} "
            f"legal form={self.legal_form_changes} location={self.location_changes}",
            f"{prefix}suppressed taxonomy migration={self.taxonomy_suppressed} "
            f"version quality={self.version_quality_suppressed} "
            f"changed snapshots with no Tier-1 signal={self.changed_snapshot_no_tier1_signal}",
            f"{prefix}signals {verb}={self.signals_created} reused={self.signals_reused} conflicts={self.conflicts} "
            f"mode=shadow (always)",
            *([f"{prefix}conflicting gemi numbers: {self.conflicting_gemi_numbers}"] if self.conflicting_gemi_numbers else []),
        ]


def _record_candidate(company, previous, current, candidate: ChangeCandidate):
    CompanySignalSnapshotEvidence = apps.get_model("gemiapp", "CompanySignalSnapshotEvidence")
    with transaction.atomic():
        signal, created = record_company_signal(
            company=company, signal_type=candidate.signal_type, source_type=SNAPSHOT_DIFF,
            event_key=candidate.event_key, effective=candidate.effective, confidence=CONFIDENCE, mode=SHADOW,
            rule_version=candidate.rule_version, detected_at=current.observed_at,
        )
        expected = {"previous_snapshot_id": previous.pk, "current_snapshot_id": current.pk, **candidate.evidence_values()}
        evidence, evidence_created = CompanySignalSnapshotEvidence.objects.get_or_create(
            signal=signal, defaults=expected,
        )
        if not evidence_created:
            different = sorted(name for name, value in expected.items() if getattr(evidence, name) != value)
            if different:
                # Rolls back the whole candidate; the existing signal and its evidence stay untouched.
                raise SnapshotEvidenceConflictError(
                    f"signal {signal.pk} already cites different snapshot evidence: {different}"
                )
    return signal, created


def materialize_snapshot_change_signals(current, *, dry_run: bool = False, report: ChangeReport | None = None) -> ChangeReport:
    """Detect and record the Tier-1 change signals of one snapshot against its immediate predecessor.

    A signal conflict is counted for this snapshot and never overwrites anything; any other exception is
    systemic and propagates.
    """
    CompanySignal = apps.get_model("gemiapp", "CompanySignal")
    report = report or ChangeReport(dry_run=dry_run)
    report.snapshots_inspected += 1
    if current.is_baseline:
        report.baselines_skipped += 1
        return report
    previous = immediate_predecessor(current)
    if previous is None:
        report.missing_predecessors += 1
        logger.warning("Snapshot %s is not a baseline but has no predecessor.", current.pk)
        return report
    report.transitions_inspected += 1
    result = detect_snapshot_changes(previous, current)
    if result.status == INCOMPATIBLE_SNAPSHOT_SCHEMA:
        report.incompatible_schema_pairs += 1
        return report
    report.taxonomy_suppressed += result.taxonomy_suppressed
    report.version_quality_suppressed += result.version_quality_suppressed
    if result.state_changed and not result.candidates:
        report.changed_snapshot_no_tier1_signal += 1
    for candidate in result.candidates:
        counter = ChangeReport._TYPE_COUNTERS[candidate.signal_type]
        setattr(report, counter, getattr(report, counter) + 1)

    company = current.company
    if dry_run:
        for candidate in result.candidates:
            key = build_dedupe_key(
                signal_type=candidate.signal_type, gemi_number=company.gemi_number, event_key=candidate.event_key,
            )
            exists = CompanySignal.objects.filter(dedupe_key=key).exists()
            report.signals_reused += int(exists)
            report.signals_created += int(not exists)
        return report
    try:
        for candidate in result.candidates:
            _, created = _record_candidate(company, previous, current, candidate)
            report.signals_created += int(created)
            report.signals_reused += int(not created)
    except SignalConflictError as exc:
        report.conflicts += 1
        report.conflicting_gemi_numbers.append(company.gemi_number)
        logger.warning("Snapshot change signal conflict for GEMI %s: %s", company.gemi_number, exc)
    return report


def materialize_all_snapshot_change_signals(
    *, batch_size: int = 500, dry_run: bool = False, start_snapshot_id: int = 0,
) -> ChangeReport:
    """Re-evaluate every snapshot transition after ``start_snapshot_id``, in id batches. Idempotent: signals
    deduplicate, so no processed flag is needed and history can be safely re-examined."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    CompanySnapshot = apps.get_model("gemiapp", "CompanySnapshot")
    report = ChangeReport(dry_run=dry_run)
    last_id = start_snapshot_id
    while True:
        batch = list(
            CompanySnapshot.objects.filter(pk__gt=last_id).select_related("company").order_by("pk")[:batch_size]
        )
        if not batch:
            break
        for snapshot in batch:
            materialize_snapshot_change_signals(snapshot, dry_run=dry_run, report=report)
        last_id = batch[-1].pk
        report.batches += 1
        report.last_snapshot_id = last_id
    logger.info(
        "Snapshot change materialisation%s: %s transitions, %s created, %s reused, %s conflicts.",
        " (dry run)" if dry_run else "", report.transitions_inspected, report.signals_created,
        report.signals_reused, report.conflicts,
    )
    return report
