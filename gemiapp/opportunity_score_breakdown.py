"""Opportunity score breakdown (C7, blueprint §32): the deterministic «why» behind one C6 score.

§32 requires that the reason is always shown -- «Όχι black-box» -- and gives an example breakdown («+25 Exact
industry, +25 New company signal, +15 Attica, +15 Detected today, +7 Contactability, +5 Legal form» = 92/100).
That example belongs to the rejected §30 example table: it scores contactability, omits the components it does not
like and does not add up to the authoritative weights. **C7 explains the actual ``opportunity_score:v1`` contract**
-- KAD fit 30, signal relevance 25, geographic fit 15, legal form fit 10, freshness 20 -- and nothing else.

A read model, not a second scorer
---------------------------------
There is exactly one source of truth for points: the C6 ``OpportunityScore``. This module never recalculates a
score, never re-decides a match and never inspects a snapshot to form its own opinion. It turns the authoritative
component values into structured lines, and it *verifies* rather than repairs: if its view and C6's disagree in any
way -- a sum, a component maximum, a freshness band, a score class, a dimension awarded without a confirming
criterion -- it raises ``ScoreBreakdownError`` instead of silently presenting a plausible story.

The criteria named as evidence come from ``organization_radar_matching.confirmed_criteria``, which applies C5's own
criterion predicates, so evidence can never disagree with the match that produced the score.

Contract
--------
Every breakdown has exactly five components, always in this order, with these maximums::

    industry_fit 30 · signal_relevance 25 · geographic_fit 15 · legal_form_fit 10 · freshness 20   (= 100)

Contactability, available contact, company characteristics, company age and industry-template fit are not part of
v1 and never appear, not even as zero rows. Each line carries a machine-readable ``reason_code`` -- the semantic
contract -- plus a Greek ``label`` that is presentation metadata only and never identity.

Only an ``opportunity_score:v1`` score produced by an ``organization_radar_match:v1`` match is explained; a future
rule version is refused until its own explainer exists, so an old layer can never misrepresent new semantics. The
breakdown always describes the score's own ``as_of``: it cannot drift to another moment.

Evidence is canonical and minimised: KAD identities as (code, version), a region's level and source id, a legal
type's source id, the signal type, and the freshness times with an integer age. Never a name, description, address,
contact detail, individual or payload. Radar ``score_threshold`` and opportunity eligibility are deliberately
absent -- C7 explains a score, it does not decide anything.

Nothing is persisted: no model, no migration, no JSON column. There is no UI, task, schedule or network call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import opportunity_scoring as scoring
from . import organization_radar_matching as matching
from .opportunity_scoring import OpportunityScore, OpportunityScoringContext
from .organization_radar_matching import MatchContext
from .organization_radars import RadarDefinition

# Component codes, in the stable v1 order.
INDUSTRY_FIT = "industry_fit"
SIGNAL_RELEVANCE = "signal_relevance"
GEOGRAPHIC_FIT = "geographic_fit"
LEGAL_FORM_FIT = "legal_form_fit"
FRESHNESS = "freshness"
COMPONENT_ORDER = (INDUSTRY_FIT, SIGNAL_RELEVANCE, GEOGRAPHIC_FIT, LEGAL_FORM_FIT, FRESHNESS)
COMPONENT_MAX_POINTS = {
    INDUSTRY_FIT: scoring.INDUSTRY_FIT_POINTS,
    SIGNAL_RELEVANCE: scoring.SIGNAL_RELEVANCE_POINTS,
    GEOGRAPHIC_FIT: scoring.GEOGRAPHIC_FIT_POINTS,
    LEGAL_FORM_FIT: scoring.LEGAL_FORM_FIT_POINTS,
    FRESHNESS: scoring.FRESHNESS_MAX_POINTS,
}
# Presentation metadata only: never identity, never compared, never scored.
COMPONENT_LABELS = {
    INDUSTRY_FIT: "Ταίριασμα ΚΑΔ",
    SIGNAL_RELEVANCE: "Σχετικότητα Σήματος",
    GEOGRAPHIC_FIT: "Γεωγραφική Συνάφεια",
    LEGAL_FORM_FIT: "Νομική Μορφή",
    FRESHNESS: "Φρεσκάδα Σήματος",
}

AWARDED = "awarded"
NOT_AWARDED = "not_awarded"

# Reason codes: the machine-readable semantics of a line.
EXACT_KAD_MATCH = "exact_kad_match"
RADAR_HAS_NO_KAD_TARGET = "radar_has_no_kad_target"
EXPLICIT_SIGNAL_TYPE_MATCH = "explicit_signal_type_match"
RADAR_ACCEPTS_ANY_SIGNAL_TYPE = "radar_accepts_any_signal_type"
EXPLICIT_REGION_MATCH = "explicit_region_match"
RADAR_HAS_NO_REGION_TARGET = "radar_has_no_region_target"
EXPLICIT_LEGAL_FORM_MATCH = "explicit_legal_form_match"
RADAR_HAS_NO_LEGAL_FORM_TARGET = "radar_has_no_legal_form_target"
FRESH_WITHIN_24H = "fresh_within_24h"
FRESH_WITHIN_72H = "fresh_within_72h"
FRESH_WITHIN_7D = "fresh_within_7d"
FRESH_WITHIN_14D = "fresh_within_14d"
FRESH_WITHIN_30D = "fresh_within_30d"
STALE_OVER_30D = "stale_over_30d"
# One code per C6 freshness band, keyed by the band's own points, so the two cannot drift.
FRESHNESS_REASON_CODES = {
    scoring.FRESHNESS_BANDS[0][1]: FRESH_WITHIN_24H,
    scoring.FRESHNESS_BANDS[1][1]: FRESH_WITHIN_72H,
    scoring.FRESHNESS_BANDS[2][1]: FRESH_WITHIN_7D,
    scoring.FRESHNESS_BANDS[3][1]: FRESH_WITHIN_14D,
    scoring.FRESHNESS_BANDS[4][1]: FRESH_WITHIN_30D,
    0: STALE_OVER_30D,
}

PREFECTURE = "prefecture"
MUNICIPALITY = "municipality"


class ScoreBreakdownError(ValueError):
    """The breakdown and the authoritative score disagree, or the input is not explainable by this version."""


@dataclass(frozen=True)
class KadEvidence:
    code: str
    kad_version: str | None


@dataclass(frozen=True)
class SignalTypeEvidence:
    signal_type: str


@dataclass(frozen=True)
class RegionEvidence:
    level: str  # "prefecture" or "municipality"
    source_id: str


@dataclass(frozen=True)
class LegalFormEvidence:
    source_id: str


@dataclass(frozen=True)
class FreshnessEvidence:
    detected_at: datetime
    as_of: datetime
    age_seconds: int
    band_points: int


@dataclass(frozen=True)
class ScoreBreakdownComponent:
    code: str
    label: str
    awarded_points: int
    max_points: int
    status: str
    reason_code: str
    evidence: tuple = ()


@dataclass(frozen=True)
class OpportunityScoreBreakdown:
    score: int
    score_class: str
    scoring_rule_version: str
    match_rule_version: str
    signal_id: int
    company_id: int
    organization_id: int
    radar_id: int
    as_of: datetime
    components: tuple  # ScoreBreakdownComponent, always in COMPONENT_ORDER

    @property
    def max_points(self) -> int:
        return sum(component.max_points for component in self.components)

    def component(self, code: str) -> ScoreBreakdownComponent:
        for component in self.components:
            if component.code == code:
                return component
        raise ScoreBreakdownError(f"unknown component: {code!r}")


def _line(code, awarded, reason_code, evidence=()):
    maximum = COMPONENT_MAX_POINTS[code]
    if not isinstance(awarded, int) or isinstance(awarded, bool) or not 0 <= awarded <= maximum:
        raise ScoreBreakdownError(f"{code} awarded {awarded}, outside 0-{maximum}")
    return ScoreBreakdownComponent(
        code=code, label=COMPONENT_LABELS[code], awarded_points=awarded, max_points=maximum,
        status=AWARDED if awarded else NOT_AWARDED, reason_code=reason_code, evidence=tuple(evidence),
    )


def _region_evidence(reference) -> RegionEvidence:
    level = PREFECTURE if reference._meta.model_name == "gemiprefecture" else MUNICIPALITY
    return RegionEvidence(level=level, source_id=reference.source_id)


def _targeting_line(code, awarded, confirmed, *, targeted, awarded_reason, missing_reason, evidence_of):
    """One targeting component, cross-checked against the criteria C5 confirmed."""
    if awarded and not confirmed:
        raise ScoreBreakdownError(f"{code} scored {awarded} but no criterion of the Radar was confirmed")
    if confirmed and not awarded:
        raise ScoreBreakdownError(f"{code} scored nothing although a criterion was confirmed")
    if awarded and not targeted:
        raise ScoreBreakdownError(f"{code} scored {awarded} although the Radar targets that dimension nowhere")
    reason = awarded_reason if awarded else missing_reason
    return _line(code, awarded, reason, tuple(evidence_of(item) for item in confirmed) if awarded else ())


def build_opportunity_score_breakdown(*, score: OpportunityScore, scoring_context: OpportunityScoringContext,
                                      match_context: MatchContext,
                                      radar_definition: RadarDefinition) -> OpportunityScoreBreakdown:
    """Explain one authoritative C6 score. Pure: no database access, no writes, no clock.

    Every point value comes from ``score``; this function only names *why* C6 awarded it, and raises rather than
    presenting a breakdown that disagrees with the score it explains.
    """
    if not isinstance(score, OpportunityScore) or not isinstance(scoring_context, OpportunityScoringContext) \
            or not isinstance(match_context, MatchContext) or not isinstance(radar_definition, RadarDefinition):
        raise ScoreBreakdownError(
            "a breakdown takes a C6 OpportunityScore and scoring context, a C5 MatchContext and a RadarDefinition")
    if score.scoring_rule_version != scoring.OPPORTUNITY_SCORE_RULE_VERSION:
        raise ScoreBreakdownError(
            f"this explainer describes {scoring.OPPORTUNITY_SCORE_RULE_VERSION}, not {score.scoring_rule_version}")
    if score.match_rule_version != matching.ORGANIZATION_RADAR_MATCH_RULE_VERSION:
        raise ScoreBreakdownError(f"unexpected match rule version: {score.match_rule_version}")
    identity = ("signal_id", "company_id", "organization_id", "radar_id", "match_rule_version")
    if any(getattr(score, name) != getattr(scoring_context, name) for name in identity):
        raise ScoreBreakdownError("the score and the scoring context describe different matches")
    if not (score.signal_id == match_context.signal_id and score.company_id == match_context.company_id):
        raise ScoreBreakdownError("the score and the match context describe different events")

    confirmed = matching.confirmed_criteria(radar_definition, match_context)
    components = [
        _targeting_line(
            INDUSTRY_FIT, score.industry_fit_points, confirmed[matching.KAD], targeted=bool(radar_definition.kads),
            awarded_reason=EXACT_KAD_MATCH, missing_reason=RADAR_HAS_NO_KAD_TARGET,
            evidence_of=lambda kad: KadEvidence(code=kad.source_id, kad_version=kad.kad_version or None),
        ),
        _line(
            SIGNAL_RELEVANCE, score.signal_relevance_points,
            EXPLICIT_SIGNAL_TYPE_MATCH if score.signal_relevance_points else RADAR_ACCEPTS_ANY_SIGNAL_TYPE,
            (SignalTypeEvidence(signal_type=match_context.signal_type),) if score.signal_relevance_points else (),
        ),
        _targeting_line(
            GEOGRAPHIC_FIT, score.geographic_fit_points, confirmed[matching.REGION],
            targeted=bool(radar_definition.regions), awarded_reason=EXPLICIT_REGION_MATCH,
            missing_reason=RADAR_HAS_NO_REGION_TARGET, evidence_of=_region_evidence,
        ),
        _targeting_line(
            LEGAL_FORM_FIT, score.legal_form_fit_points, confirmed[matching.LEGAL_FORM],
            targeted=bool(radar_definition.legal_forms), awarded_reason=EXPLICIT_LEGAL_FORM_MATCH,
            missing_reason=RADAR_HAS_NO_LEGAL_FORM_TARGET,
            evidence_of=lambda legal_type: LegalFormEvidence(source_id=legal_type.source_id),
        ),
        _freshness_line(score, scoring_context),
    ]
    if bool(score.signal_relevance_points) != (match_context.signal_type in radar_definition.signal_types):
        raise ScoreBreakdownError("signal relevance disagrees with the Radar's declared signal types")

    breakdown = OpportunityScoreBreakdown(
        score=score.score, score_class=score.score_class, scoring_rule_version=score.scoring_rule_version,
        match_rule_version=score.match_rule_version, signal_id=score.signal_id, company_id=score.company_id,
        organization_id=score.organization_id, radar_id=score.radar_id, as_of=score.as_of,
        components=tuple(components),
    )
    _verify(breakdown, score)
    return breakdown


def _freshness_line(score: OpportunityScore, scoring_context: OpportunityScoringContext) -> ScoreBreakdownComponent:
    age = score.as_of - scoring_context.detected_at
    if scoring.freshness_points_for(age) != score.freshness_points:
        raise ScoreBreakdownError(
            f"freshness {score.freshness_points} does not match the band of an age of {age} at the score's as_of")
    reason = FRESHNESS_REASON_CODES.get(score.freshness_points)
    if reason is None:
        raise ScoreBreakdownError(f"no freshness band explains {score.freshness_points} points")
    evidence = FreshnessEvidence(
        detected_at=scoring_context.detected_at, as_of=score.as_of,
        age_seconds=int(age // timedelta(seconds=1)), band_points=score.freshness_points,
    )
    return _line(FRESHNESS, score.freshness_points, reason, (evidence,))


def _verify(breakdown: OpportunityScoreBreakdown, score: OpportunityScore) -> None:
    """The breakdown may never drift from the score it explains."""
    if tuple(component.code for component in breakdown.components) != COMPONENT_ORDER:
        raise ScoreBreakdownError("a breakdown holds exactly the five v1 components, in their fixed order")
    if breakdown.max_points != scoring.TOTAL_POINTS:
        raise ScoreBreakdownError("the component maximums no longer total 100")
    awarded = sum(component.awarded_points for component in breakdown.components)
    if awarded != score.score:
        raise ScoreBreakdownError(f"the breakdown sums to {awarded} but the score is {score.score}")
    if dict(score.component_points) != {c.code: c.awarded_points for c in breakdown.components}:
        raise ScoreBreakdownError("a component line disagrees with the score's own component points")
    # The same C6 helper, never a copied cutoff.
    if breakdown.score_class != scoring.score_class_for(score.score):
        raise ScoreBreakdownError("the score class does not follow from the score")
    for component in breakdown.components:
        if component.status != (AWARDED if component.awarded_points else NOT_AWARDED):
            raise ScoreBreakdownError(f"{component.code} status disagrees with its points")


def explain_opportunity_scores(signal, *, as_of: datetime) -> tuple:
    """Every confirmed match of one persisted signal, scored **and** explained. Read-only.

    C5 provides the matches and C6 the points; this adds one bounded load of the matched Radars' definitions for
    the evidence. The breakdown building itself is pure.
    """
    report = matching.explain_organization_radar_matches(signal)
    if not report.matches:
        return ()
    by_radar = {item.radar_id: item.evaluation for item in report.evaluations}
    definitions = matching.load_radar_definitions([match.radar_id for match in report.matches])
    breakdowns = []
    for match in report.matches:
        evaluation = by_radar[match.radar_id]
        scoring_context = scoring.build_opportunity_scoring_context(
            match=match, evaluation=evaluation, context=report.context)
        score = scoring.calculate_opportunity_score(scoring_context, as_of=as_of)
        breakdowns.append(build_opportunity_score_breakdown(
            score=score, scoring_context=scoring_context, match_context=report.context,
            radar_definition=definitions[match.radar_id][1]))
    return tuple(breakdowns)
