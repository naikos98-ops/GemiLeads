"""Organization Radar matching engine (C5, blueprint §28).

§28: for every signal -> company -> eligible active radars -> filters -> match / no match, without looping over
every customer: indexed criteria and candidate selection. This module answers exactly one question -- *which
active OrganizationRadars does this CompanySignal match?* -- and nothing downstream of it.

Internal and read-only
----------------------
Nothing is persisted: there is no match table, no Opportunity, no lead, no score and no notification. Evaluating
the same signal again returns the same result, so there is nothing to clean up or deduplicate. §29 puts the first
persistent customer-specific row at the Opportunity, and §30 puts scoring there too; C6 consumes these values.

``CustomerRadar`` stays the production matcher. Nothing here reads or writes CustomerRadar, RadarMatch,
UserCompanyLead, monitoring or the B4 planner, and nothing there reads this module. Organization ICPs and industry
templates are deliberately not consulted: only the Radar's own criteria target. ``score_threshold`` belongs to
scoring and is ignored. SHADOW and LIVE signals are evaluated identically; batch entry points take the mode
explicitly and never mix the two.

Pipeline
--------
1. ``build_match_context(signal)`` -- the minimised company state the event is evaluated against (below).
2. ``candidate_radar_ids(context)`` -- one SQL query over indexed criterion relations. It may return radars that
   do not match (false positives) but never omits one the evaluator could match or find state-insufficient.
3. ``load_radar_definitions(ids)`` -- the candidates' C3 ``RadarDefinition`` values in a bounded number of queries
   per chunk (one per criterion table), independent of how many radars a chunk holds.
4. ``evaluate_organization_radar(definition, context)`` -- pure: no database access. The final truth.

Company state (context status)
------------------------------
Only approved canonical history is used: the B3 ``CompanySnapshot``. Never ``Company.is_active``, legacy
descriptions, ``CompanyActivity``, ``raw_data`` or free text, and never contact data or persons.

* ``SNAPSHOT_DIFF`` signals (B5): the evidence row's ``current_snapshot`` -- the exact state that produced the
  event, never "the latest snapshot". A missing evidence row, or one citing another company's snapshot, yields
  ``missing_evidence`` and no state.
* ``DISCOVERY`` signals (B2, NEW_COMPANY): discovery evidence is not company state, so the latest snapshot with
  ``observed_at <= detected_at``; a later snapshot never rewrites what was known at detection. None ->
  ``no_snapshot``.
* Any other source type (future document pipelines): ``unsupported_source`` and no state. No semantics are guessed.
* A snapshot of an unknown schema version or shape: ``unsupported_snapshot_schema`` and no state.

Without state, a Radar restricted only by signal type can still match; any Radar restricted by KAD, region, legal
form or exclusions is ``INSUFFICIENT_STATE``.

Match rule v1 (``ORGANIZATION_RADAR_MATCH_RULE_VERSION``)
---------------------------------------------------------
Each criterion evaluates to confirmed, refuted or unknown.

* **OR within a dimension, AND across dimensions.** A dimension is confirmed if any criterion is confirmed, refuted
  if all are refuted, otherwise unknown. A dimension with no criteria is unrestricted.
* **Signal type** -- listed types: the signal's type must be listed. No listed types: any *implemented* signal type
  (a taxonomy-only type never passes an unrestricted Radar). Always determinate.
* **KAD** -- exact (code, version) identity against the snapshot's verified-current activities. No prefix, no
  description, no 2008/2026 equivalence, no template. Unknown when the snapshot counts activities of indeterminate
  currentness (their codes are not in the state), or when the code agrees but either side's version is unpublished.
* **Region** -- a prefecture criterion compares the snapshot's prefecture source id, a municipality criterion the
  municipality source id; exactly, with no inference between levels, from city or postal code, or across the
  Attica sub-units. A null source id is unknown.
* **Legal form** -- exact legal-type source id; null is unknown.
* **Exclusions** -- evaluated independently of positive criteria. Any confirmed exclusion is a hard veto. An
  exclusion that cannot be evaluated is not a veto, but it also cannot be ruled out, so it is unknown.

Result: veto -> ``NO_MATCH``; any refuted dimension -> ``NO_MATCH``; otherwise any unknown dimension or exclusion
-> ``INSUFFICIENT_STATE``; otherwise ``MATCH``. An inactive Radar, or one with no positive criterion, is
``NO_MATCH``. Unknown facts are never reported as a definite non-fit, and never as a fit.

Any change to these semantics is ``organization_radar_match:v2``; v1 behaviour is pinned by tests.

Nothing here calls GEMI, the web, Stripe or email, and there is no URL, view, API, task or schedule.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.apps import apps
from django.db.models import Exists, OuterRef, Q

from . import company_signals
from .company_snapshots import COMPANY_SNAPSHOT_SCHEMA_VERSION
from .organization_radars import RadarDefinition, implemented_signal_types

logger = logging.getLogger(__name__)

ORGANIZATION_RADAR_MATCH_RULE_VERSION = "organization_radar_match:v1"

# Evaluation status.
MATCH = "match"
NO_MATCH = "no_match"
INSUFFICIENT_STATE = "insufficient_state"

# Context status.
SNAPSHOT_EVIDENCE = "snapshot_evidence"
SNAPSHOT_AT_DETECTION = "snapshot_at_detection"
NO_SNAPSHOT = "no_snapshot"
MISSING_EVIDENCE = "missing_evidence"
UNSUPPORTED_SOURCE = "unsupported_source"
UNSUPPORTED_SNAPSHOT_SCHEMA = "unsupported_snapshot_schema"
STATE_BEARING = frozenset({SNAPSHOT_EVIDENCE, SNAPSHOT_AT_DETECTION})

# Dimensions.
SIGNAL_TYPE = "signal_type"
KAD = "kad"
REGION = "region"
LEGAL_FORM = "legal_form"
DIMENSIONS = (SIGNAL_TYPE, KAD, REGION, LEGAL_FORM)

# Candidates are loaded and evaluated in chunks so "IN" lists stay bounded on every database.
DEFINITION_CHUNK_SIZE = 500


class MatchingError(ValueError):
    """A matching request with invalid input. Nothing is ever written, raised or not."""


@dataclass(frozen=True)
class MatchContext:
    """The minimised state one signal is evaluated against: identifiers, KAD identities and times only."""

    signal_id: int
    company_id: int
    signal_type: str
    source_type: str
    detected_at: datetime
    context_status: str
    state_snapshot_id: int | None = None
    # (code, kad_version) of verified-current activities; the version is None when the source published none.
    kads: frozenset = frozenset()
    indeterminate_activity_count: int = 0
    prefecture_source_id: str | None = None
    municipality_source_id: str | None = None
    legal_type_source_id: str | None = None

    @property
    def has_state(self) -> bool:
        return self.context_status in STATE_BEARING


@dataclass(frozen=True)
class RadarEvaluation:
    status: str
    match_rule_version: str
    matched_dimensions: tuple = ()
    unrestricted_dimensions: tuple = ()
    failed_dimensions: tuple = ()
    unknown_dimensions: tuple = ()
    # The subject of the first confirmed exclusion ("kad", "prefecture", "municipality", "legal_form").
    exclusion_hit: str | None = None
    unknown_exclusions: tuple = ()


@dataclass(frozen=True)
class MatchedOrganizationRadar:
    organization_id: int
    radar_id: int
    signal_id: int
    company_id: int
    signal_type: str
    match_rule_version: str
    state_snapshot_id: int | None


@dataclass(frozen=True)
class RadarCandidateEvaluation:
    organization_id: int
    radar_id: int
    evaluation: RadarEvaluation


@dataclass(frozen=True)
class SignalMatchReport:
    context: MatchContext
    candidate_count: int
    evaluations: tuple  # RadarCandidateEvaluation, ordered by (organization_id, radar_id)

    @property
    def matches(self) -> tuple:
        return tuple(
            MatchedOrganizationRadar(
                organization_id=item.organization_id, radar_id=item.radar_id, signal_id=self.context.signal_id,
                company_id=self.context.company_id, signal_type=self.context.signal_type,
                match_rule_version=item.evaluation.match_rule_version,
                state_snapshot_id=self.context.state_snapshot_id,
            )
            for item in self.evaluations if item.evaluation.status == MATCH
        )

    def count(self, status: str) -> int:
        return sum(1 for item in self.evaluations if item.evaluation.status == status)


def _model(name):
    return apps.get_model("gemiapp", name)


# --- context -------------------------------------------------------------------------------------

def _stored_signal(signal):
    CompanySignal = _model("CompanySignal")
    if not isinstance(signal, CompanySignal) or signal.pk is None:
        raise MatchingError("an existing, saved CompanySignal is required")
    row = (CompanySignal.objects.filter(pk=signal.pk)
           .only("pk", "company_id", "signal_type", "source_type", "detected_at").first())
    if row is None:
        raise MatchingError(f"CompanySignal #{signal.pk} does not exist")
    return row


def _state_from_snapshot(base: dict, snapshot, status: str) -> MatchContext:
    entries = snapshot.activities_state
    shape_ok = snapshot.schema_version == COMPANY_SNAPSHOT_SCHEMA_VERSION and isinstance(entries, list) and all(
        isinstance(entry, dict) and isinstance(entry.get("code"), str) and entry["code"]
        and (entry.get("kad_version") is None or isinstance(entry.get("kad_version"), str))
        for entry in entries
    )
    if not shape_ok:
        return MatchContext(**base, context_status=UNSUPPORTED_SNAPSHOT_SCHEMA, state_snapshot_id=snapshot.pk)
    return MatchContext(
        **base, context_status=status, state_snapshot_id=snapshot.pk,
        kads=frozenset((entry["code"], entry.get("kad_version") or None) for entry in entries),
        indeterminate_activity_count=snapshot.unknown_current_activity_count,
        prefecture_source_id=snapshot.prefecture_source_id or None,
        municipality_source_id=snapshot.municipality_source_id or None,
        legal_type_source_id=snapshot.legal_type_source_id or None,
    )


_SNAPSHOT_FIELDS = ("id", "company_id", "schema_version", "activities_state", "unknown_current_activity_count",
                    "prefecture_source_id", "municipality_source_id", "legal_type_source_id")


def build_match_context(signal) -> MatchContext:
    """The canonical state a persisted signal is matched against. Read-only; see the module docstring."""
    row = _stored_signal(signal)
    base = dict(signal_id=row.pk, company_id=row.company_id, signal_type=row.signal_type,
                source_type=row.source_type, detected_at=row.detected_at)
    if row.source_type == company_signals.SNAPSHOT_DIFF:
        evidence = (_model("CompanySignalSnapshotEvidence").objects.filter(signal_id=row.pk)
                    .select_related("current_snapshot")
                    .only(*(f"current_snapshot__{name}" for name in _SNAPSHOT_FIELDS), "signal_id").first())
        if evidence is None or evidence.current_snapshot.company_id != row.company_id:
            return MatchContext(**base, context_status=MISSING_EVIDENCE)
        return _state_from_snapshot(base, evidence.current_snapshot, SNAPSHOT_EVIDENCE)
    if row.source_type == company_signals.DISCOVERY:
        snapshot = (_model("CompanySnapshot").objects
                    .filter(company_id=row.company_id, observed_at__lte=row.detected_at)
                    .order_by("-observed_at", "-id").only(*_SNAPSHOT_FIELDS).first())
        if snapshot is None:
            return MatchContext(**base, context_status=NO_SNAPSHOT)
        return _state_from_snapshot(base, snapshot, SNAPSHOT_AT_DETECTION)
    return MatchContext(**base, context_status=UNSUPPORTED_SOURCE)


# --- pure evaluator ------------------------------------------------------------------------------

def _any_of(results) -> bool | None:
    """OR over three-valued results: True if any is True, False if all are False, otherwise None."""
    results = list(results)
    if any(result is True for result in results):
        return True
    if all(result is False for result in results):
        return False
    return None


def _kad_criterion(kad, context: MatchContext) -> bool | None:
    if not context.has_state:
        return None
    code, version = kad.source_id, kad.kad_version or None
    if version is not None and (code, version) in context.kads:
        return True
    if any(entry_code == code and (version is None or entry_version is None)
           for entry_code, entry_version in context.kads):
        return None  # same code, but a version is unpublished on one side: identity cannot be confirmed
    if context.indeterminate_activity_count:
        return None  # activities of unknown currentness are not in the state and may include this KAD
    return False


def _source_id(value: str | None, context: MatchContext, criterion_id: str) -> bool | None:
    if not context.has_state or value is None:
        return None
    return value == criterion_id


def _reference_criterion(reference, context: MatchContext) -> tuple[str, bool | None]:
    kind = reference._meta.model_name
    if kind == "gemikad":
        return KAD, _kad_criterion(reference, context)
    if kind == "gemiprefecture":
        return "prefecture", _source_id(context.prefecture_source_id, context, reference.source_id)
    if kind == "gemimunicipality":
        return "municipality", _source_id(context.municipality_source_id, context, reference.source_id)
    if kind == "gemilegaltype":
        return LEGAL_FORM, _source_id(context.legal_type_source_id, context, reference.source_id)
    raise MatchingError(f"unsupported criterion reference: {kind}")


def evaluate_organization_radar(definition: RadarDefinition, context: MatchContext) -> RadarEvaluation:
    """Rule v1 for one Radar definition against one context. Pure: no database access, no writes."""
    if not isinstance(definition, RadarDefinition) or not isinstance(context, MatchContext):
        raise MatchingError("evaluate_organization_radar takes a RadarDefinition and a MatchContext")
    version = ORGANIZATION_RADAR_MATCH_RULE_VERSION
    if not definition.active:
        return RadarEvaluation(NO_MATCH, version, failed_dimensions=("active",))
    if not definition.has_positive_criteria:
        return RadarEvaluation(NO_MATCH, version, failed_dimensions=("configured",))

    if definition.signal_types:
        signal_result = context.signal_type in definition.signal_types
    else:
        signal_result = context.signal_type in implemented_signal_types()
    dimensions = {
        SIGNAL_TYPE: (bool(definition.signal_types), signal_result),
        KAD: (bool(definition.kads), _any_of(_reference_criterion(k, context)[1] for k in definition.kads)),
        REGION: (bool(definition.regions), _any_of(_reference_criterion(r, context)[1] for r in definition.regions)),
        LEGAL_FORM: (bool(definition.legal_forms),
                     _any_of(_reference_criterion(f, context)[1] for f in definition.legal_forms)),
    }
    matched, unrestricted, failed, unknown = [], [], [], []
    for name in DIMENSIONS:
        restricted, result = dimensions[name]
        if name == SIGNAL_TYPE and not restricted:
            # Unrestricted, but only implemented types exist for it.
            (unrestricted if result else failed).append(name)
        elif not restricted:
            unrestricted.append(name)
        elif result is True:
            matched.append(name)
        elif result is False:
            failed.append(name)
        else:
            unknown.append(name)

    exclusion_hit, unknown_exclusions = None, []
    for reference in definition.exclusions:
        subject, result = _reference_criterion(reference, context)
        if result is True and exclusion_hit is None:
            exclusion_hit = subject
        elif result is None:
            unknown_exclusions.append(subject)

    if exclusion_hit is not None or failed:
        status = NO_MATCH
    elif unknown or unknown_exclusions:
        status = INSUFFICIENT_STATE
    else:
        status = MATCH
    return RadarEvaluation(
        status=status, match_rule_version=version, matched_dimensions=tuple(matched),
        unrestricted_dimensions=tuple(unrestricted), failed_dimensions=tuple(failed),
        unknown_dimensions=tuple(unknown), exclusion_hit=exclusion_hit,
        unknown_exclusions=tuple(sorted(set(unknown_exclusions))),
    )


# --- indexed candidate selection -----------------------------------------------------------------

def _kad_identity_q(context: MatchContext, prefix: str) -> Q | None:
    """Rows whose KAD is confirmed present in the context: exact (code, version) pairs only."""
    q = None
    for code, version in sorted(context.kads, key=lambda pair: (pair[0], pair[1] or "")):
        if version is None:
            continue
        term = Q(**{f"{prefix}source_id": code, f"{prefix}kad_version": version})
        q = term if q is None else q | term
    return q


def candidate_radars_queryset(context: MatchContext):
    """Active OrganizationRadars that may match: a conservative superset of MATCH and INSUFFICIENT_STATE.

    A dimension prunes only when the fact it needs is known and every criterion of the radar is definitely
    refuted; an unknown fact never removes a radar. Exclusions prune only on a confirmed hit.
    """
    if not isinstance(context, MatchContext):
        raise MatchingError("a MatchContext is required")
    Radar = _model("OrganizationRadar")
    SignalType, Kad = _model("OrganizationRadarSignalType"), _model("OrganizationRadarKad")
    Region, LegalForm = _model("OrganizationRadarRegion"), _model("OrganizationRadarLegalForm")
    Exclusion = _model("OrganizationRadarExclusion")
    radar = OuterRef("pk")

    listed = SignalType.objects.filter(radar=radar)
    watches = Exists(listed.filter(signal_type=context.signal_type))
    if context.signal_type in implemented_signal_types():
        queryset = Radar.objects.filter(Q(active=True) & (~Exists(listed) | watches))
    else:
        queryset = Radar.objects.filter(active=True).filter(watches)
    if not context.has_state:
        return queryset

    # KAD: prune only when the whole current set is determinate.
    if not context.indeterminate_activity_count and all(version for _, version in context.kads):
        kads = Kad.objects.filter(radar=radar)
        keep = ~Exists(kads)
        confirmed = _kad_identity_q(context, "kad__")
        if confirmed is not None:
            keep |= Exists(kads.filter(confirmed))
        codes = sorted({code for code, _ in context.kads})
        if codes:  # an unpublished Radar KAD version with an equal code is unknown, not refuted
            keep |= Exists(kads.filter(kad__source_id__in=codes, kad__kad_version=""))
        queryset = queryset.filter(keep)

    # Region: a level whose fact is unknown keeps every radar with a criterion at that level.
    prefecture, municipality = context.prefecture_source_id, context.municipality_source_id
    regions = Region.objects.filter(radar=radar)
    keep = ~Exists(regions)
    keep |= (Exists(regions.filter(level=Region.PREFECTURE, prefecture__source_id=prefecture)) if prefecture
             else Exists(regions.filter(level=Region.PREFECTURE)))
    keep |= (Exists(regions.filter(level=Region.MUNICIPALITY, municipality__source_id=municipality)) if municipality
             else Exists(regions.filter(level=Region.MUNICIPALITY)))
    queryset = queryset.filter(keep)

    if context.legal_type_source_id:
        forms = LegalForm.objects.filter(radar=radar)
        queryset = queryset.filter(~Exists(forms) | Exists(forms.filter(legal_type__source_id=context.legal_type_source_id)))

    # Exclusions: only a confirmed hit is a veto.
    vetoes = Q(pk__in=[])
    confirmed = _kad_identity_q(context, "kad__")
    if confirmed is not None:
        vetoes |= Q(subject=Exclusion.KAD) & confirmed
    if prefecture:
        vetoes |= Q(subject=Exclusion.PREFECTURE, prefecture__source_id=prefecture)
    if municipality:
        vetoes |= Q(subject=Exclusion.MUNICIPALITY, municipality__source_id=municipality)
    if context.legal_type_source_id:
        vetoes |= Q(subject=Exclusion.LEGAL_FORM, legal_type__source_id=context.legal_type_source_id)
    return queryset.filter(~Exists(Exclusion.objects.filter(radar=radar).filter(vetoes)))


def candidate_radar_ids(context: MatchContext) -> list:
    return list(candidate_radars_queryset(context).order_by("organization_id", "pk").values_list("pk", flat=True))


def load_radar_definitions(radar_ids) -> dict:
    """{radar_id: (organization_id, RadarDefinition)} for these radars, in one query per table.

    The batch equivalent of ``organization_radars.get_organization_radar_definition``: identical values and
    criterion order (insertion order), without per-radar queries.
    """
    ids = sorted(set(radar_ids))
    if not ids:
        return {}
    grouped = {pk: {"kads": [], "regions": [], "legal_forms": [], "signal_types": [], "exclusions": []} for pk in ids}
    for item in _model("OrganizationRadarKad").objects.filter(radar_id__in=ids).select_related("kad").order_by("pk"):
        grouped[item.radar_id]["kads"].append(item.kad)
    for item in (_model("OrganizationRadarRegion").objects.filter(radar_id__in=ids)
                 .select_related("prefecture", "municipality").order_by("pk")):
        grouped[item.radar_id]["regions"].append(item.prefecture or item.municipality)
    for item in (_model("OrganizationRadarLegalForm").objects.filter(radar_id__in=ids)
                 .select_related("legal_type").order_by("pk")):
        grouped[item.radar_id]["legal_forms"].append(item.legal_type)
    for item in _model("OrganizationRadarSignalType").objects.filter(radar_id__in=ids).order_by("pk"):
        grouped[item.radar_id]["signal_types"].append(item.signal_type)
    for item in (_model("OrganizationRadarExclusion").objects.filter(radar_id__in=ids)
                 .select_related("kad", "prefecture", "municipality", "legal_type").order_by("pk")):
        grouped[item.radar_id]["exclusions"].append(item.kad or item.prefecture or item.municipality or item.legal_type)
    definitions = {}
    for row in _model("OrganizationRadar").objects.filter(pk__in=ids).only(
            "pk", "organization_id", "name", "active", "score_threshold"):
        criteria = grouped[row.pk]
        definitions[row.pk] = (row.organization_id, RadarDefinition(
            name=row.name, active=row.active, score_threshold=row.score_threshold,
            **{name: tuple(values) for name, values in criteria.items()},
        ))
    return definitions


# --- services ------------------------------------------------------------------------------------

def explain_organization_radar_matches(signal) -> SignalMatchReport:
    """Every candidate's evaluation for one persisted signal. Read-only."""
    context = build_match_context(signal)
    ids = candidate_radar_ids(context)
    evaluations = []
    for start in range(0, len(ids), DEFINITION_CHUNK_SIZE):
        definitions = load_radar_definitions(ids[start:start + DEFINITION_CHUNK_SIZE])
        for radar_id, (organization_id, definition) in definitions.items():
            evaluations.append(RadarCandidateEvaluation(
                organization_id=organization_id, radar_id=radar_id,
                evaluation=evaluate_organization_radar(definition, context),
            ))
    evaluations.sort(key=lambda item: (item.organization_id, item.radar_id))
    report = SignalMatchReport(context=context, candidate_count=len(ids), evaluations=tuple(evaluations))
    logger.info(
        "Organization radar matching: signal=%s context=%s candidates=%s match=%s insufficient=%s rule=%s",
        context.signal_id, context.context_status, report.candidate_count, report.count(MATCH),
        report.count(INSUFFICIENT_STATE), ORGANIZATION_RADAR_MATCH_RULE_VERSION,
    )
    return report


def find_matching_organization_radars(signal) -> tuple:
    """The active OrganizationRadars this persisted CompanySignal matches, as immutable values. Read-only."""
    return explain_organization_radar_matches(signal).matches


@dataclass(frozen=True)
class MatchingSummary:
    mode: str
    signals: int
    candidates: int
    matches: int
    no_matches: int
    insufficient_state: int
    context_statuses: tuple  # sorted (status, count)


def summarize_organization_radar_matching(*, mode: str, limit: int = 100) -> MatchingSummary:
    """Evaluate the most recent signals of exactly one mode and count outcomes. Read-only; SHADOW and LIVE are
    never combined."""
    if mode not in company_signals.MODES:
        raise MatchingError(f"mode must be one of {company_signals.MODES}")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise MatchingError("limit must be a positive whole number")
    signals = _model("CompanySignal").objects.filter(mode=mode).order_by("-detected_at", "-id")[:limit]
    totals = dict(signals=0, candidates=0, matches=0, no_matches=0, insufficient_state=0)
    statuses: dict = {}
    for signal in signals:
        report = explain_organization_radar_matches(signal)
        totals["signals"] += 1
        totals["candidates"] += report.candidate_count
        totals["matches"] += report.count(MATCH)
        totals["no_matches"] += report.count(NO_MATCH)
        totals["insufficient_state"] += report.count(INSUFFICIENT_STATE)
        statuses[report.context.context_status] = statuses.get(report.context.context_status, 0) + 1
    return MatchingSummary(mode=mode, context_statuses=tuple(sorted(statuses.items())), **totals)
