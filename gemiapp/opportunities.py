"""Opportunity persistence (C8, blueprint §29 with §34).

The first persisted customer-specific artefact of Gemi Leads 2.0. The pipeline is
``signal -> C5 match -> C6 score -> C7 breakdown -> C8 opportunity``, and only the last step writes anything.

«Signal ≠ Opportunity» (§29): a match is a transient evaluation, an opportunity is commercial state. A match
becomes one only after C5 confirmed it, C6 scored it, the Radar's threshold accepted the score and the aggregation
rule decided whether a row already exists.

Identity and aggregation (§34)
------------------------------
One row per **(organization, radar, company)**, enforced by a database constraint -- never one per signal, because
§34 explicitly refuses a card per event («Company X — 3 relevant signals», one company card). Each qualifying
signal is attached through ``OpportunitySignal``; the originating event stays in ``first_signal`` and is never
rewritten, while ``latest_signal`` names the event whose capture is currently frozen on the row. §29's single
``signal_id`` is deliberately split into those two, because one field cannot mean both. A company watched by two
Radars, or by two organizations, has two opportunities: those are different commercial situations, and the feed
(§35, C9) is where several of them collapse into one company card.

Threshold (§31)
---------------
This is where ``OrganizationRadar.score_threshold`` finally matters; it never changes a score. The blueprint
defines no default threshold, so none is invented:

* ``score_threshold`` null -> every confirmed match qualifies, whatever it scored;
* ``score_threshold`` set  -> ``score >= score_threshold`` qualifies.

A non-qualifying evaluation writes nothing at all: it does not create a row, does not attach a signal and never
deletes or downgrades an opportunity that already exists.

Frozen capture
--------------
C7 showed that a derived explanation drifts when a Radar is edited. So the capture is written down: the score, its
class, both rule versions, the ``as_of`` it was calculated at, the five component lines and their canonical
evidence. ``get_opportunity_score_breakdown`` reads exactly those rows back and recomputes nothing, so editing a
Radar afterwards cannot change what a customer was told.

Only the **current** capture is kept: a later qualifying signal replaces the score, class, reason and components
with its own, and the newest qualifying evaluation wins even when it scores lower than the previous one -- there is
no "highest score ever". Per-event history is not lost: every contributing signal keeps its own score and as_of on
its ``OpportunitySignal`` row. The blueprint asks for a current score, so no score-history table is built.

Status (§39) is stored and validated -- ``new`` on creation -- with no transition policy: the workflow package
owns that. ``expires_at`` exists because §29 lists it, but the blueprint defines no expiry rule anywhere, so C8
never sets it and there is no cleanup job. ``reason`` is a stable machine-readable ``primary_reason_code`` (the
reason code of the strongest awarded component), never prose and never AI.

Boundaries
----------
Organization is always ``radar.organization``, never derived from a user, and every id in the C5/C6/C7 values must
agree before anything is written. Nothing here touches ``CustomerRadar``, ``RadarMatch``, ``UserCompanyLead``, A9
monitoring, the B4 planner, subscriptions or billing; no contact detail, person, address, VAT or GEMI payload is
stored; and there is no view, URL, API, feed, task, schedule or network call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.apps import apps
from django.db import IntegrityError, transaction

from . import opportunity_score_breakdown as breakdowns
from . import opportunity_scoring as scoring
from . import organization_radar_matching as matching
from .opportunity_score_breakdown import OpportunityScoreBreakdown
from .opportunity_scoring import OpportunityScore

# Capture-time evidence writers, one per C7 evidence value.
_EVIDENCE_WRITERS = {
    "KadEvidence": lambda item: {"kind": "kad", "kad_code": item.code, "kad_version": item.kad_version},
    "SignalTypeEvidence": lambda item: {"kind": "signal_type", "signal_type": item.signal_type},
    "RegionEvidence": lambda item: {"kind": "region", "region_level": item.level,
                                    "region_source_id": item.source_id},
    "LegalFormEvidence": lambda item: {"kind": "legal_form", "legal_type_source_id": item.source_id},
    "FreshnessEvidence": lambda item: {"kind": "freshness", "detected_at": item.detected_at,
                                       "scored_as_of": item.as_of, "age_seconds": item.age_seconds,
                                       "band_points": item.band_points},
}
_EVIDENCE_READERS = {
    "kad": lambda row: breakdowns.KadEvidence(code=row.kad_code, kad_version=row.kad_version),
    "signal_type": lambda row: breakdowns.SignalTypeEvidence(signal_type=row.signal_type),
    "region": lambda row: breakdowns.RegionEvidence(level=row.region_level, source_id=row.region_source_id),
    "legal_form": lambda row: breakdowns.LegalFormEvidence(source_id=row.legal_type_source_id),
    "freshness": lambda row: breakdowns.FreshnessEvidence(
        detected_at=row.detected_at, as_of=row.scored_as_of, age_seconds=row.age_seconds,
        band_points=row.band_points),
}

BELOW_THRESHOLD = "below_score_threshold"


class OpportunityError(ValueError):
    """An opportunity request that violates the C8 rules. Nothing has been written when it is raised."""


@dataclass(frozen=True)
class OpportunityMaterialization:
    """What one qualifying (or rejected) evaluation did. Immutable; the row itself is the persisted state."""

    opportunity: object | None
    eligible: bool
    created: bool = False
    signal_attached: bool = False
    rescored: bool = False
    ineligible_reason: str | None = None


def _model(name):
    return apps.get_model("gemiapp", name)


def effective_threshold(radar):
    """The score a match must reach for this Radar, or None when the Radar sets none. No default is invented."""
    return radar.score_threshold


def qualifies(radar, score: OpportunityScore) -> bool:
    """Whether a confirmed, scored match may become persisted state. The score itself is never changed."""
    threshold = effective_threshold(radar)
    return threshold is None or score.score >= threshold


def _validated(signal, radar, score: OpportunityScore, breakdown: OpportunityScoreBreakdown):
    """Every id and rule version must agree before anything is written; nothing is trusted from the caller."""
    CompanySignal, OrganizationRadar = _model("CompanySignal"), _model("OrganizationRadar")
    if not isinstance(signal, CompanySignal) or signal.pk is None:
        raise OpportunityError("an existing, saved CompanySignal is required")
    if not isinstance(radar, OrganizationRadar) or radar.pk is None:
        raise OpportunityError("an existing, saved OrganizationRadar is required")
    if not isinstance(score, OpportunityScore) or not isinstance(breakdown, OpportunityScoreBreakdown):
        raise OpportunityError("a C6 OpportunityScore and its C7 breakdown are required")
    if score.scoring_rule_version != scoring.OPPORTUNITY_SCORE_RULE_VERSION:
        raise OpportunityError(f"unsupported scoring rule version: {score.scoring_rule_version}")
    if score.match_rule_version != matching.ORGANIZATION_RADAR_MATCH_RULE_VERSION:
        raise OpportunityError(f"unsupported match rule version: {score.match_rule_version}")
    for name in ("score", "score_class", "scoring_rule_version", "match_rule_version", "signal_id", "company_id",
                 "organization_id", "radar_id", "as_of"):
        if getattr(score, name) != getattr(breakdown, name):
            raise OpportunityError(f"the score and its breakdown disagree on {name}")
    if breakdown.max_points != scoring.TOTAL_POINTS or len(breakdown.components) != len(breakdowns.COMPONENT_ORDER):
        raise OpportunityError("the breakdown is not a complete v1 breakdown")
    if sum(component.awarded_points for component in breakdown.components) != score.score:
        raise OpportunityError("the breakdown does not sum to the score")
    if score.signal_id != signal.pk:
        raise OpportunityError("the score describes another signal")
    if score.radar_id != radar.pk:
        raise OpportunityError("the score describes another Radar")
    if score.company_id != signal.company_id:
        raise OpportunityError("the signal belongs to another company than the score")
    # Organization is the Radar's, never the caller's and never derived from a user.
    if score.organization_id != radar.organization_id:
        raise OpportunityError("the score belongs to another organization than the Radar")
    if not radar.active:
        raise OpportunityError("an inactive Radar does not produce opportunities")
    return radar.organization_id


def _primary_reason_code(breakdown: OpportunityScoreBreakdown) -> str:
    """The strongest awarded component's reason code: a stable summary, never prose. Ties keep the v1 order."""
    awarded = [component for component in breakdown.components if component.awarded_points]
    if not awarded:
        return ""
    return max(awarded, key=lambda component: (component.awarded_points, -COMPONENT_POSITION[component.code])).reason_code


COMPONENT_POSITION = {code: index for index, code in enumerate(breakdowns.COMPONENT_ORDER)}


def _capture(opportunity, breakdown: OpportunityScoreBreakdown) -> None:
    """Freeze the five component lines and their canonical evidence, replacing any previous capture."""
    Component, Evidence = _model("OpportunityScoreComponent"), _model("OpportunityScoreEvidence")
    opportunity.score_components.all().delete()
    for position, component in enumerate(breakdown.components):
        row = Component.objects.create(
            opportunity=opportunity, code=component.code, awarded_points=component.awarded_points,
            max_points=component.max_points, reason_code=component.reason_code, position=position,
        )
        Evidence.objects.bulk_create([
            Evidence(component=row, position=index, **_EVIDENCE_WRITERS[type(item).__name__](item))
            for index, item in enumerate(component.evidence)
        ])


def _capture_matches(opportunity, score: OpportunityScore, breakdown: OpportunityScoreBreakdown) -> bool:
    """Whether the stored capture already is this exact one, so a repeat writes nothing."""
    stored = [(c.code, c.awarded_points, c.reason_code)
              for c in opportunity.score_components.order_by("position")]
    current = [(c.code, c.awarded_points, c.reason_code) for c in breakdown.components]
    return (stored == current and opportunity.score == score.score and opportunity.score_class == score.score_class
            and opportunity.scored_as_of == score.as_of and opportunity.latest_signal_id == score.signal_id
            and opportunity.score_rule_version == score.scoring_rule_version
            and opportunity.match_rule_version == score.match_rule_version)


def materialize_opportunity(*, signal, radar, score: OpportunityScore,
                            breakdown: OpportunityScoreBreakdown) -> OpportunityMaterialization:
    """Persist (or update) the opportunity one qualifying, scored, explained match belongs to.

    Idempotent: the same signal, Radar and capture may be processed repeatedly without creating a second
    opportunity, a second signal link or a second set of component rows.
    """
    organization_id = _validated(signal, radar, score, breakdown)
    Opportunity, OpportunitySignal = _model("Opportunity"), _model("OpportunitySignal")
    if not qualifies(radar, score):
        return OpportunityMaterialization(opportunity=None, eligible=False, ineligible_reason=BELOW_THRESHOLD)

    values = dict(
        score=score.score, score_class=score.score_class, score_rule_version=score.scoring_rule_version,
        match_rule_version=score.match_rule_version, scored_as_of=score.as_of,
        primary_reason_code=_primary_reason_code(breakdown), latest_signal_id=signal.pk,
    )
    with transaction.atomic():
        opportunity, created = _locked_or_created(
            Opportunity, organization_id=organization_id, radar_id=radar.pk, company_id=signal.company_id,
            defaults=dict(first_signal_id=signal.pk, **values),
        )
        if created:
            _capture(opportunity, breakdown)
        rescored = False
        if not created and not _capture_matches(opportunity, score, breakdown):
            for field, value in values.items():
                setattr(opportunity, field, value)
            opportunity.save(update_fields=[*values, "updated_at"])
            _capture(opportunity, breakdown)
            rescored = True
        _, attached = OpportunitySignal.objects.get_or_create(
            opportunity=opportunity, signal=signal,
            defaults={"score": score.score, "scored_as_of": score.as_of},
        )
    return OpportunityMaterialization(opportunity=opportunity, eligible=True, created=created,
                                      signal_attached=attached, rescored=rescored)


def _locked_or_created(Opportunity, *, organization_id, radar_id, company_id, defaults):
    """The existing row under a lock, or a new one. The unique constraint, not a check, prevents duplicates.

    PostgreSQL holds the row lock for a concurrent worker; SQLite does not reproduce that faithfully, so the
    invariant is documented and the constraint proven, rather than claimed to be proven by SQLite.
    """
    identity = dict(organization_id=organization_id, radar_id=radar_id, company_id=company_id)
    existing = Opportunity.objects.select_for_update().filter(**identity).first()
    if existing is not None:
        return existing, False
    try:
        with transaction.atomic():
            return Opportunity.objects.create(**identity, **defaults), True
    except IntegrityError:  # another worker created it first
        return Opportunity.objects.select_for_update().get(**identity), False


def materialize_opportunities_for_signal(signal, *, as_of: datetime) -> tuple:
    """Run the whole pipeline for one persisted signal: match, score, explain, threshold, persist.

    C5 stays the only source of matches and C6 the only source of points; nothing is re-decided here.
    """
    results = []
    for breakdown in breakdowns.explain_opportunity_scores(signal, as_of=as_of):
        radar = _model("OrganizationRadar").objects.get(pk=breakdown.radar_id)
        score = _score_of(breakdown)
        results.append(materialize_opportunity(signal=signal, radar=radar, score=score, breakdown=breakdown))
    return tuple(results)


def _score_of(breakdown: OpportunityScoreBreakdown) -> OpportunityScore:
    """The authoritative score value a breakdown was built from, reassembled from its own frozen fields."""
    points = {f"{component.code}_points": component.awarded_points for component in breakdown.components}
    return OpportunityScore(
        score=breakdown.score, score_class=breakdown.score_class,
        scoring_rule_version=breakdown.scoring_rule_version, signal_id=breakdown.signal_id,
        company_id=breakdown.company_id, organization_id=breakdown.organization_id, radar_id=breakdown.radar_id,
        match_rule_version=breakdown.match_rule_version, as_of=breakdown.as_of, **points,
    )


def get_opportunity_score_breakdown(opportunity) -> OpportunityScoreBreakdown:
    """The frozen explanation of an opportunity's current score, read back from the captured rows.

    Nothing is recomputed and the current Radar is never consulted, so a later Radar edit cannot change it.
    """
    Opportunity = _model("Opportunity")
    if not isinstance(opportunity, Opportunity) or opportunity.pk is None:
        raise OpportunityError("a saved Opportunity is required")
    components = []
    rows = (_model("OpportunityScoreComponent").objects.filter(opportunity_id=opportunity.pk)
            .order_by("position").prefetch_related("evidence"))
    for row in rows:
        components.append(breakdowns.ScoreBreakdownComponent(
            code=row.code, label=breakdowns.COMPONENT_LABELS.get(row.code, row.code),
            awarded_points=row.awarded_points, max_points=row.max_points,
            status=breakdowns.AWARDED if row.awarded_points else breakdowns.NOT_AWARDED,
            reason_code=row.reason_code,
            evidence=tuple(_EVIDENCE_READERS[item.kind](item) for item in row.evidence.all()),
        ))
    if tuple(component.code for component in components) != breakdowns.COMPONENT_ORDER:
        raise OpportunityError("the stored capture is not a complete v1 breakdown")
    if sum(component.awarded_points for component in components) != opportunity.score:
        raise OpportunityError("the stored capture does not sum to the stored score")
    return OpportunityScoreBreakdown(
        score=opportunity.score, score_class=opportunity.score_class,
        scoring_rule_version=opportunity.score_rule_version, match_rule_version=opportunity.match_rule_version,
        signal_id=opportunity.latest_signal_id, company_id=opportunity.company_id,
        organization_id=opportunity.organization_id, radar_id=opportunity.radar_id,
        as_of=opportunity.scored_as_of, components=tuple(components),
    )


def set_opportunity_status(opportunity, status: str):
    """Store a §39 sales-action state. Validation only: C8 enforces no transition order and triggers nothing."""
    Opportunity = _model("Opportunity")
    if not isinstance(opportunity, Opportunity) or opportunity.pk is None:
        raise OpportunityError("a saved Opportunity is required")
    if status not in {value for value, _ in Opportunity.STATUSES}:
        raise OpportunityError(f"unknown opportunity status: {status!r}")
    opportunity.status = status
    opportunity.save(update_fields=["status", "updated_at"])
    return opportunity
