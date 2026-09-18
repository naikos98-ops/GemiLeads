"""Opportunity scoring engine (C6): the Gemi Leads Opportunity Score v1.

The blueprint's §30 component table and §32 breakdown are explicitly «Παράδειγμα» -- *examples*, not a
specification, and they contradict each other (§32 shows «+7 Contactability» where §30 says 5, and omits company
characteristics). Taken literally they also leave the product incoherent: two of the seven example components have
no deterministic source today, so the reachable maximum would be 85 while §31 reserves 90-100 for «Priority».

**This module's contract is the authoritative v1 scoring contract**, and it totals exactly 100:

===========================  ======
Industry / KAD fit               30
Signal relevance                 25
Geographic fit                   15
Legal form fit                   10
Freshness                        20
**Total**                   **100**
===========================  ======

*Available contact* and *company characteristics* are **not part of v1 at all** -- not as components, not as
permanent zeroes. Contact availability may never influence a score, and no approved deterministic rule exists for
"company characteristics"; defining one later requires ``opportunity_score:v2``. §31's classes are unchanged and
all four are reachable.

Targeting components
--------------------
**A targeting component scores only when the Radar explicitly targeted that dimension and the canonical company
state confirmed it** -- exactly the C5 ``matched_dimensions`` of a confirmed match. An unrestricted dimension is
not evidence of fit: a Radar that never named a KAD has not demonstrated an industry fit with this company, and a
Radar that accepts every implemented event type has not declared this event especially valuable. Nothing is
inferred and nothing is renormalised; points exist only where a fact is positively proven.

* ``industry_fit`` (30) -- the Radar's exact KAD criteria, identity ``(code, kad_version)``, confirmed by the
  snapshot's verified-current activities. Never a prefix, a description, an IndustryTemplate, an inferred industry
  group, a 2008 -> 2026 crosswalk or AI: C4 established that no canonical KAD hierarchy exists.
* ``signal_relevance`` (25) -- the Radar explicitly lists this signal type. No signal-type criteria means 0, by
  design, even though such a Radar still matches.
* ``geographic_fit`` (15) -- the Radar's exact prefecture or municipality criteria confirmed by the snapshot's
  canonical source ids. Never city or postal text.
* ``legal_form_fit`` (10) -- the Radar's exact legal-type criterion confirmed by the snapshot.

Freshness (20)
--------------
Monotonic decay over the age of the signal at an explicit ``as_of``, measured from ``CompanySignal.detected_at``.
The event's own ``effective_date`` / ``effective_at`` are never used: a DATE-precision signal must not become a
fabricated midnight. Upper bounds are inclusive::

    <= 24h   20 · <= 72h   18 · <= 7d   15 · <= 14d   10 · <= 30d   5 · beyond   0

``as_of`` is always supplied -- the calculator never calls ``timezone.now()``, so a score is reproducible -- and an
``as_of`` earlier than detection is rejected.

Boundaries
----------
C5 decides *whether* a Radar matches; C6 only scores a confirmed ``MATCH``. ``INSUFFICIENT_STATE`` and
``NO_MATCH`` are rejected, never scored as a low number. Radar exclusions are a match veto, never a penalty.
``score_threshold`` never changes a score and no ``meets_threshold`` is exposed: the future Opportunity package
decides eligibility. The ICP contributes nothing. Nothing is persisted -- no score row, no Opportunity, no lead,
no breakdown model (§32 is C7's) -- and there is no UI, task, schedule or network call.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from django.utils import timezone

from . import organization_radar_matching as matching
from .organization_radar_matching import MatchContext, MatchedOrganizationRadar, RadarEvaluation

OPPORTUNITY_SCORE_RULE_VERSION = "opportunity_score:v1"

# The v1 component weights. They total exactly 100.
INDUSTRY_FIT_POINTS = 30
SIGNAL_RELEVANCE_POINTS = 25
GEOGRAPHIC_FIT_POINTS = 15
LEGAL_FORM_FIT_POINTS = 10
FRESHNESS_MAX_POINTS = 20
MIN_SCORE, MAX_SCORE = 0, 100
TOTAL_POINTS = (INDUSTRY_FIT_POINTS + SIGNAL_RELEVANCE_POINTS + GEOGRAPHIC_FIT_POINTS + LEGAL_FORM_FIT_POINTS
                + FRESHNESS_MAX_POINTS)

# Monotonic decay: (inclusive upper bound on the signal's age, points). Beyond the last band: 0.
FRESHNESS_BANDS = (
    (timedelta(hours=24), FRESHNESS_MAX_POINTS),
    (timedelta(hours=72), 18),
    (timedelta(days=7), 15),
    (timedelta(days=14), 10),
    (timedelta(days=30), 5),
)

# §31 classes, with their published boundaries.
PRIORITY, HIGH, MEDIUM, LOW = "priority", "high", "medium", "low"
SCORE_CLASS_BANDS = ((90, PRIORITY), (75, HIGH), (55, MEDIUM), (0, LOW))


class ScoringError(ValueError):
    """A scoring request that violates the C6 rules. Nothing is ever written, raised or not."""


@dataclass(frozen=True)
class OpportunityScoringContext:
    """The minimised facts v1 scores: identifiers, the dimensions a confirmed match proved, and detection time.

    Built from a C5 match; it holds no company name, contact detail, individual, payload or subscription fact,
    and no score.
    """

    signal_id: int
    company_id: int
    organization_id: int
    radar_id: int
    radar_signal_type: str
    detected_at: datetime
    match_rule_version: str
    state_snapshot_id: int | None
    # Dimensions the Radar explicitly targeted and the canonical state confirmed (C5 matched_dimensions).
    industry_confirmed: bool
    geography_confirmed: bool
    legal_form_confirmed: bool
    signal_type_declared: bool


@dataclass(frozen=True)
class OpportunityScore:
    score: int
    score_class: str
    scoring_rule_version: str
    signal_id: int
    company_id: int
    organization_id: int
    radar_id: int
    match_rule_version: str
    # The explicit instant the score was calculated at, so an explanation can never describe another moment.
    as_of: datetime
    # The v1 components; they sum to ``score``.
    industry_fit_points: int = 0
    signal_relevance_points: int = 0
    geographic_fit_points: int = 0
    legal_form_fit_points: int = 0
    freshness_points: int = 0

    @property
    def component_points(self) -> tuple:
        return tuple((f.name[: -len("_points")], getattr(self, f.name))
                     for f in fields(self) if f.name.endswith("_points"))


def score_class_for(score: int) -> str:
    """The §31 class of a score: 90-100 priority, 75-89 high, 55-74 medium, 0-54 low."""
    if isinstance(score, bool) or not isinstance(score, int) or not MIN_SCORE <= score <= MAX_SCORE:
        raise ScoringError(f"a score must be a whole number from {MIN_SCORE} to {MAX_SCORE}")
    for floor, name in SCORE_CLASS_BANDS:
        if score >= floor:
            return name
    raise ScoringError("unreachable: the bands cover 0-100")  # pragma: no cover


def freshness_points_for(age: timedelta) -> int:
    """The v1 decay schedule. Pure; upper bounds inclusive."""
    if not isinstance(age, timedelta):
        raise ScoringError("age must be a timedelta")
    if age < timedelta(0):
        raise ScoringError("a signal cannot be scored before it was detected")
    for limit, points in FRESHNESS_BANDS:
        if age <= limit:
            return points
    return 0


def build_opportunity_scoring_context(*, match: MatchedOrganizationRadar, evaluation: RadarEvaluation,
                                      context: MatchContext) -> OpportunityScoringContext:
    """The scoring facts of one **confirmed** C5 match. Pure: no database access, and nothing else is matched.

    Rejects anything that is not a MATCH of the same signal and match rule; C6 never re-decides matching.
    """
    if not isinstance(match, MatchedOrganizationRadar) or not isinstance(evaluation, RadarEvaluation) \
            or not isinstance(context, MatchContext):
        raise ScoringError("scoring takes a C5 MatchedOrganizationRadar, RadarEvaluation and MatchContext")
    if evaluation.status != matching.MATCH:
        raise ScoringError(f"only a confirmed match is scored, not {evaluation.status}")
    if not (match.signal_id == context.signal_id and match.company_id == context.company_id):
        raise ScoringError("the match and the context describe different events")
    if not (match.match_rule_version == evaluation.match_rule_version == matching.ORGANIZATION_RADAR_MATCH_RULE_VERSION):
        raise ScoringError("the match was produced by a different match rule version")
    if match.signal_type != context.signal_type:
        raise ScoringError("the match and the context describe different signal types")
    matched = evaluation.matched_dimensions
    return OpportunityScoringContext(
        signal_id=match.signal_id, company_id=match.company_id, organization_id=match.organization_id,
        radar_id=match.radar_id, radar_signal_type=match.signal_type, detected_at=context.detected_at,
        match_rule_version=match.match_rule_version, state_snapshot_id=match.state_snapshot_id,
        industry_confirmed=matching.KAD in matched, geography_confirmed=matching.REGION in matched,
        legal_form_confirmed=matching.LEGAL_FORM in matched, signal_type_declared=matching.SIGNAL_TYPE in matched,
    )


def calculate_opportunity_score(context: OpportunityScoringContext, *, as_of: datetime) -> OpportunityScore:
    """Score one confirmed match out of 100. Pure: no database access, no writes, no clock."""
    if not isinstance(context, OpportunityScoringContext):
        raise ScoringError("an OpportunityScoringContext is required")
    if not isinstance(as_of, datetime) or timezone.is_naive(as_of):
        raise ScoringError("as_of must be an explicit timezone-aware datetime")
    if as_of < context.detected_at:
        raise ScoringError("as_of precedes the signal's detection; refusing to score an event that had not happened")

    points = dict(
        industry_fit_points=INDUSTRY_FIT_POINTS if context.industry_confirmed else 0,
        signal_relevance_points=SIGNAL_RELEVANCE_POINTS if context.signal_type_declared else 0,
        geographic_fit_points=GEOGRAPHIC_FIT_POINTS if context.geography_confirmed else 0,
        legal_form_fit_points=LEGAL_FORM_FIT_POINTS if context.legal_form_confirmed else 0,
        freshness_points=freshness_points_for(as_of - context.detected_at),
    )
    score = sum(points.values())
    if not MIN_SCORE <= score <= MAX_SCORE:  # pragma: no cover -- the weights make this impossible
        raise ScoringError(f"score {score} left the {MIN_SCORE}-{MAX_SCORE} scale")
    return OpportunityScore(
        score=score, score_class=score_class_for(score), scoring_rule_version=OPPORTUNITY_SCORE_RULE_VERSION,
        signal_id=context.signal_id, company_id=context.company_id, organization_id=context.organization_id,
        radar_id=context.radar_id, match_rule_version=context.match_rule_version, as_of=as_of, **points,
    )


def score_organization_radar_match(*, match: MatchedOrganizationRadar, evaluation: RadarEvaluation,
                                   context: MatchContext, as_of: datetime) -> OpportunityScore:
    """Score one confirmed C5 match. Pure: builder plus calculator, no database access."""
    return calculate_opportunity_score(
        build_opportunity_scoring_context(match=match, evaluation=evaluation, context=context), as_of=as_of)


def score_matching_organization_radars(signal, *, as_of: datetime) -> tuple:
    """Every confirmed match of one persisted signal, scored. Read-only.

    C5 stays the only source of matches: this consumes one matching report, so the query count is C5's and does
    not grow with the number of matches.
    """
    report = matching.explain_organization_radar_matches(signal)
    by_radar = {item.radar_id: item.evaluation for item in report.evaluations}
    return tuple(
        score_organization_radar_match(match=match, evaluation=by_radar[match.radar_id], context=report.context,
                                       as_of=as_of)
        for match in report.matches
    )
