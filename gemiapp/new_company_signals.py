"""The NEW_COMPANY signal producer (B2): the first real CompanySignal type.

What the event means
--------------------
NEW_COMPANY means **Gemi Leads first observed this company as newly discovered**, on the evidence of
Discovery v2 (A10). It does not mean "incorporated today": a late publication -- a company published now
with an older incorporation date -- is exactly as much a first observation, and the whole point of A10 was
to stop losing those. The incorporation date, when the source gives a usable one, is recorded separately as
the event's effective date.

Eligible evidence
-----------------
A ``GemiDiscoveryObservation`` is a candidate when its classification is one of the newly-discovered
classes -- ``new_incorporation``, ``late_publication`` or ``invalid_date`` -- which A10 writes only for
records that were not already stored locally. ``known`` observations, which A10 records for the
already-local rows a scan pages through so the legacy comparison can tell "seen" from "never reached", are
never candidates.

Evidence from every run mode counts, bootstrap included: a bootstrap scan's backlog rows are genuine first
sightings. Evidence from a run that later failed or hit an ordering anomaly also counts: those guardrails
protect the pagination cursor, not the authenticity of a record GEMI actually returned and A2 validated.

Event identity
--------------
A company has exactly one first-observation event, so the event key is the fixed
``{"event": "first_observed"}``. With the B1 identity that hashes the signal type, the company's GEMI
number and this key, the event is stable forever: repeated observations, several runs, a corrected
incorporation date, a newer rule version and a future promotion to live all resolve to the same signal.
Changing this shape after release would be a deliberate identity migration, not a code tweak.

Discovery time and detected time are distinct
----------------------------------------------
**Discovery observation time** is when Discovery v2 first observed the company: the ``started_at`` of the run
that produced the earliest eligible observation (observations carry only ``created_at``, a bulk-write artefact
identical for every row of a run). When several runs observed the company, the earliest eligible observation
wins. It stays exactly where it is -- on the observation and its run, linked to the signal through
``CompanySignalDiscoveryEvidence`` -- and is never rewritten.

**Signal ``detected_at``** is the first moment Gemi Leads had *both* that discovery evidence *and* a canonical
company state sufficient for deterministic Radar evaluation:

    detected_at = max(discovery observation time, baseline snapshot observed_at)

This is required, not cosmetic. Radar matching (C5) reads only state observed at or before ``detected_at`` and
never looks forward; a company discovered as new is by definition not stored locally when discovery sees it, so
any state of it is observed later. Without this rule every KAD, region and legal-form Radar would be
``INSUFFICIENT_STATE`` for every new company. When no trustworthy state exists (below), there is no baseline and
``detected_at`` is the discovery observation time, exactly as before -- criteria Radars then stay insufficient.

Neither value is ever the materialisation command's run time or "now". The value is fixed when the signal is
created; a rerun never moves it.

Detection-time state (the baseline)
-----------------------------------
Before the signal is recorded, the company's canonical state is made available:

* the company already has a snapshot -> its first (baseline) snapshot is **reused**; nothing is written;
* otherwise a baseline is **created** with the existing B3 writer (``normalize_company`` ->
  ``record_company_snapshot``: same schema and normalizer versions, state hash and quality semantics) from the
  importer's stored record, and only when that record is provably trustworthy:
  - ``Company.raw_data`` passes the A2 ``company_search`` contract as one search result,
  - its ``arGemi`` is this company's GEMI number,
  - no Django admin addition or change was logged for the company, and
  - A6 establishes its observation time (``Company.updated_at``, the moment the importer wrote the record it had
    just received from GEMI -- the same evidence as A6 ``last_seen_at``);
* otherwise (missing, malformed or foreign record, admin-edited row, normalisation failure) **no state**: nothing
  is guessed, fabricated or taken from a later snapshot.

The baseline, the signal and its discovery evidence are written in one transaction, in that order, so a signal
never exists without the state it was detected with, and the G2 pipeline (on commit) finds that state through
the matcher's unchanged ``observed_at <= detected_at`` lookup. No GEMI request is made: the state is what the
approved importer already stored. This narrowly revises B3's "no baselines from stored ``raw_data``": only for a
company being materialised as newly discovered, only from a validated, identity-checked, A6-timestamped record.

Effective date
--------------
A valid source incorporation date (A3 quality ``valid``, which is the only case A10 stores a date for)
becomes ``effective_date`` with DATE precision -- no time of day is invented. A missing, unreadable or
out-of-range date leaves the signal with no source time at all (precision NONE); the detection date is
never substituted for it, and nothing is clamped.

Company materialisation
-----------------------
In shadow mode Discovery v2 stores nothing, so a newly discovered company usually does not exist in
``Company`` yet. A signal must belong to a real company (B1), so such a candidate stays **unmaterialised**:
no signal, no placeholder company, no import, and the observation remains the durable pending evidence.
When the company later arrives through the approved importer, the next run materialises exactly one signal,
still stamped with the original discovery time.

Mode
----
Every signal this producer creates is SHADOW. There is no parameter, flag or command option that produces a
live one: Discovery v2 is not authoritative until its 14-day shadow gate is reviewed, and promotion is a
separate, explicit decision.

Conflicts
---------
A signal that already exists keeps its detected time, its effective date and its original provenance. If
the same event identity is found with different immutable facts, B1 raises and this producer reports the
conflict for that company and moves on to the next; conflicts are counted and surfaced, never smoothed over.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from .company_signals import (
    DISCOVERY,
    NEW_COMPANY,
    SHADOW,
    SignalConflictError,
    build_dedupe_key,
    record_company_signal,
    rule_for,
)
from .company_snapshots import BASELINE_CREATED, record_company_snapshot
from .ingestion.company_metadata import RAW_GEMI_RECORD, company_is_admin_touched, derive_company_metadata
from .ingestion.discovery import INVALID_DATE, LATE_PUBLICATION, NEW_INCORPORATION
from .ingestion.errors import GemiResponseValidationError
from .ingestion.normalizer import normalize_company
from .ingestion.schemas import ResponseFamily, validate_response

logger = logging.getLogger(__name__)

# A10 classifications that mean "newly discovered", i.e. not already stored locally.
ELIGIBLE_CLASSIFICATIONS = (NEW_INCORPORATION, LATE_PUBLICATION, INVALID_DATE)
# One first-observation event per company: the key never varies.
EVENT_KEY = {"event": "first_observed"}
# Discovery is structured evidence that the company was observed; this is confidence in the event, not in
# the incorporation date, so an unusable date does not lower it.
CONFIDENCE = Decimal("1.0000")

CREATED = "created"
EXISTING = "existing"
PENDING_NO_COMPANY = "pending_no_company"
CONFLICT = "conflict"
INVALID_EVIDENCE = "invalid_evidence"
NOT_ELIGIBLE = "not_eligible"


# Detection-time state of a newly recorded signal.
STATE_BASELINE_CREATED = "baseline_created"
STATE_BASELINE_REUSED = "baseline_reused"
STATE_UNAVAILABLE = "state_unavailable"


@dataclass(frozen=True)
class ProducerResult:
    status: str
    gemi_number: str
    signal_id: int | None = None
    detail: str = ""
    detection_state: str = ""        # for a created signal: baseline created / reused / state unavailable


def trusted_importer_observation(company):
    """(observed_at, "") when ``Company.raw_data`` is a trustworthy GEMI observation of this very company, else
    (None, reason). Reads stored data only; never calls GEMI."""
    derived = derive_company_metadata(
        gemi_number=company.gemi_number, raw_data=company.raw_data, imported_at=company.imported_at,
        updated_at=company.updated_at, admin_touched=company_is_admin_touched(company.pk))
    if derived.raw_state != RAW_GEMI_RECORD:
        return None, derived.raw_state
    if derived.admin_touched:
        return None, "admin_touched"
    observed_at = derived.values["last_seen_at"]
    if observed_at is None or timezone.is_naive(observed_at):
        return None, "no_observation_time"
    try:
        validate_response(ResponseFamily.COMPANY_SEARCH, {"searchResults": [company.raw_data]})
    except GemiResponseValidationError:
        return None, "invalid_record"
    return observed_at, ""


def _detection_state(company):
    """(state, baseline snapshot or None, reason): reuse the company's baseline, or create it from a trustworthy
    importer record with the B3 writer, or report that no state is available. Called inside the producer's
    transaction, before the signal is recorded."""
    CompanySnapshot = apps.get_model("gemiapp", "CompanySnapshot")
    baseline = CompanySnapshot.objects.filter(company=company).order_by("observed_at", "id").first()
    if baseline is not None:
        return STATE_BASELINE_REUSED, baseline, ""
    observed_at, reason = trusted_importer_observation(company)
    if observed_at is None:
        return STATE_UNAVAILABLE, None, reason
    try:
        normalized = normalize_company(company.raw_data, as_of=timezone.localdate(observed_at))
    except (TypeError, ValueError):
        return STATE_UNAVAILABLE, None, "normalisation_failed"
    outcome = record_company_snapshot(company, normalized, observed_at)
    if outcome.status != BASELINE_CREATED:  # impossible without a prior snapshot; never guess
        return STATE_UNAVAILABLE, None, outcome.status
    return STATE_BASELINE_CREATED, outcome.snapshot, ""


def eligible_observations():
    """Every newly-discovered discovery observation, oldest evidence first."""
    GemiDiscoveryObservation = apps.get_model("gemiapp", "GemiDiscoveryObservation")
    return (
        GemiDiscoveryObservation.objects.filter(classification__in=ELIGIBLE_CLASSIFICATIONS)
        .select_related("run").order_by("run__started_at", "id")
    )


def _effective_date(observation):
    """The source incorporation date when A3 called it valid; otherwise no source time at all."""
    if observation.incorporation_date_quality == "valid":
        if observation.incorporation_date is None:
            raise ValueError("observation claims a valid incorporation date but stores none")
        return observation.incorporation_date
    return None


def produce_new_company_signal(observation) -> ProducerResult:
    """Materialise the NEW_COMPANY signal for one discovery observation. See the module docstring."""
    Company = apps.get_model("gemiapp", "Company")
    CompanySignalDiscoveryEvidence = apps.get_model("gemiapp", "CompanySignalDiscoveryEvidence")
    if observation.classification not in ELIGIBLE_CLASSIFICATIONS:
        return ProducerResult(NOT_ELIGIBLE, observation.gemi_number, detail=observation.classification)
    company = Company.objects.filter(gemi_number=observation.gemi_number).first()
    if company is None:
        # Expected in shadow mode: the observation stays the durable pending evidence.
        return ProducerResult(PENDING_NO_COMPANY, observation.gemi_number)
    try:
        effective = _effective_date(observation)
    except ValueError as exc:
        return ProducerResult(INVALID_EVIDENCE, observation.gemi_number, detail=str(exc))
    CompanySignal = apps.get_model("gemiapp", "CompanySignal")
    discovered_at = observation.run.started_at
    state, reason = "", ""
    try:
        with transaction.atomic():
            detected_at = discovered_at
            key = build_dedupe_key(signal_type=NEW_COMPANY, gemi_number=company.gemi_number, event_key=dict(EVENT_KEY))
            if not CompanySignal.objects.filter(dedupe_key=key).exists():
                # Detection-time state first, then the signal that is detected with it (see the module docstring).
                state, baseline, reason = _detection_state(company)
                if baseline is not None:
                    detected_at = max(discovered_at, baseline.observed_at)
            signal, created = record_company_signal(
                company=company, signal_type=NEW_COMPANY, source_type=DISCOVERY, event_key=dict(EVENT_KEY),
                effective=effective, confidence=CONFIDENCE, mode=SHADOW,
                detected_at=detected_at,
            )
            # First evidence wins: an existing link is never replaced by a later observation.
            CompanySignalDiscoveryEvidence.objects.get_or_create(
                signal=signal, defaults={"discovery_observation": observation},
            )
    except SignalConflictError as exc:
        return ProducerResult(CONFLICT, observation.gemi_number, detail=str(exc))
    return ProducerResult(CREATED if created else EXISTING, observation.gemi_number, signal_id=signal.pk,
                          detail=reason, detection_state=state if created else "")


@dataclass
class MaterialisationReport:
    dry_run: bool
    observations_inspected: int = 0
    candidate_companies: int = 0
    signals_created: int = 0
    signals_existing: int = 0
    unmaterialised_no_company: int = 0
    late_publications: int = 0
    valid_effective_dates: int = 0
    missing_or_invalid_effective_dates: int = 0
    conflicts: int = 0
    invalid_evidence: int = 0
    baselines_created: int = 0
    baselines_reused: int = 0
    state_unavailable: int = 0
    batches: int = 0
    last_gemi_number: str = ""
    conflicting_gemi_numbers: list = field(default_factory=list)

    def lines(self) -> list[str]:
        prefix = "[dry-run] " if self.dry_run else ""
        verb = "would create" if self.dry_run else "created"
        return [
            f"{prefix}eligible observations inspected={self.observations_inspected} candidate companies="
            f"{self.candidate_companies} batches={self.batches} last_gemi_number={self.last_gemi_number or 'none'}",
            f"{prefix}signals {verb}={self.signals_created} already existing={self.signals_existing} "
            f"unmaterialised (no Company yet)={self.unmaterialised_no_company}",
            f"{prefix}evidence: late publications={self.late_publications} valid effective dates="
            f"{self.valid_effective_dates} missing or invalid dates={self.missing_or_invalid_effective_dates}",
            f"{prefix}detection-time state: baselines created={self.baselines_created} reused="
            f"{self.baselines_reused} unavailable={self.state_unavailable}",
            f"{prefix}conflicts={self.conflicts} invalid evidence={self.invalid_evidence} mode=shadow (always)",
            *([f"{prefix}conflicting gemi numbers: {self.conflicting_gemi_numbers}"] if self.conflicting_gemi_numbers else []),
        ]


def materialize_new_company_signals(
    *, batch_size: int = 500, dry_run: bool = False, start_gemi_number: str = "",
    gemi_numbers: Iterable[str] | None = None,
) -> MaterialisationReport:
    """Turn eligible discovery evidence into SHADOW NEW_COMPANY signals, one per company. Idempotent."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    Company = apps.get_model("gemiapp", "Company")
    CompanySignal = apps.get_model("gemiapp", "CompanySignal")
    report = MaterialisationReport(dry_run=dry_run)
    observations = eligible_observations()
    if gemi_numbers is not None:
        observations = observations.filter(gemi_number__in=list(gemi_numbers))
    numbers = list(
        observations.filter(gemi_number__gt=start_gemi_number).order_by("gemi_number")
        .values_list("gemi_number", flat=True).distinct()
    )
    for start in range(0, len(numbers), batch_size):
        batch = numbers[start:start + batch_size]
        first_evidence = {}
        for observation in observations.filter(gemi_number__in=batch):
            report.observations_inspected += 1
            first_evidence.setdefault(observation.gemi_number, observation)
        companies = set(Company.objects.filter(gemi_number__in=batch).values_list("gemi_number", flat=True))
        for gemi_number in batch:
            observation = first_evidence[gemi_number]
            report.candidate_companies += 1
            report.late_publications += int(observation.classification == LATE_PUBLICATION)
            if observation.incorporation_date_quality == "valid":
                report.valid_effective_dates += 1
            else:
                report.missing_or_invalid_effective_dates += 1
            if gemi_number not in companies:
                report.unmaterialised_no_company += 1
                continue
            if dry_run:
                key = build_dedupe_key(signal_type=NEW_COMPANY, gemi_number=gemi_number, event_key=dict(EVENT_KEY))
                exists = CompanySignal.objects.filter(dedupe_key=key).exists()
                report.signals_existing += int(exists)
                report.signals_created += int(not exists)
                continue
            result = produce_new_company_signal(observation)
            if result.status == CREATED:
                report.signals_created += 1
                report.baselines_created += int(result.detection_state == STATE_BASELINE_CREATED)
                report.baselines_reused += int(result.detection_state == STATE_BASELINE_REUSED)
                report.state_unavailable += int(result.detection_state == STATE_UNAVAILABLE)
            elif result.status == EXISTING:
                report.signals_existing += 1
            elif result.status == CONFLICT:
                report.conflicts += 1
                report.conflicting_gemi_numbers.append(gemi_number)
                logger.warning("NEW_COMPANY signal conflict for GEMI %s: %s", gemi_number, result.detail)
            elif result.status == INVALID_EVIDENCE:
                report.invalid_evidence += 1
                logger.warning("NEW_COMPANY evidence invariant problem for GEMI %s: %s", gemi_number, result.detail)
        report.batches += 1
        report.last_gemi_number = batch[-1]
    logger.info(
        "NEW_COMPANY materialisation%s: %s candidates, %s created, %s existing, %s pending, %s conflicts.",
        " (dry run)" if dry_run else "", report.candidate_companies, report.signals_created,
        report.signals_existing, report.unmaterialised_no_company, report.conflicts,
    )
    return report


def rule_is_implemented() -> bool:
    rule = rule_for(NEW_COMPANY)
    return bool(rule and rule.implemented)
