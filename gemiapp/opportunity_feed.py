"""Opportunity feed read model (C9, blueprint §35 with §34).

Which opportunities should this organization see now, and how are they grouped, filtered and ordered? A read model
over **persisted C8 opportunities only**: nothing here matches, scores, explains, creates, rescores or changes the
status of anything, and there is no model, migration, view, URL, task or schedule.

What the blueprint defines
--------------------------
§35 names the screen «Today's Opportunities», eight filters (priority, signal, ΚΑΔ, region, score, date, Radar,
status) and one sort, «highest score + freshest». §34 wants one company card instead of a card per event
(«Company X — 3 relevant signals»). The blueprint does *not* define the card's score when several Radars found the
same company, which timestamp "freshest" means, what "Today" filters, pagination or expiry. The v1 answers below are
deliberate and documented rather than inferred.

* **«Today's Opportunities» is the screen title**, not a date filter: older opportunities are never silently
  hidden. Restricting to a day is what the explicit date filter is for, with an explicit window.
* **Expiry**: C8 never sets ``expires_at`` and no expiry rule exists, so nothing is filtered on it.

Frozen, never live
------------------
The feed shows C8's frozen capture: score, class, primary reason code and ``scored_as_of`` exactly as captured. It
never recalculates freshness against the current time and never consults the current Radar, so a card's numbers
change only when C8 receives another qualifying signal. ``scored_as_of`` is exposed so nothing implies the score is
live. The full five-component breakdown stays out of the card; ``opportunities.get_opportunity_score_breakdown``
reads it for a detail view.

One card per company (§34)
--------------------------
C8 persists one opportunity per (organization, radar, company). The feed groups them into **one card per
(organization, company)** without merging any row, and keeps every underlying opportunity as an immutable child
summary, so the Radars that found the company stay visible.

**Primary opportunity.** The card's top-level score, class, status, reason and Radar all come from one and the same
underlying opportunity -- never a mix of fields from different rows. It is the first by the feed order:

1. highest frozen score;
2. freshest ``latest_signal.detected_at`` (the event behind the current capture);
3. lowest opportunity id.

**Freshness** is ``latest_signal.detected_at``: the detection time of the event whose capture the opportunity
carries. Not ``updated_at`` (a status change is not news), not the wall clock and not a recomputed score. A card
exposes two distinct times: ``primary_signal_detected_at`` (its primary opportunity's) and
``latest_company_signal_detected_at`` (the most recent across its opportunities).

**Relevant signal count** is the number of *distinct* contributing signals across the card's opportunities: a
signal that qualified for two Radars counts once.

Filters
-------
Opportunity-level, then aggregated: filters select underlying opportunities, and a card holds only the
opportunities that survived, with its primary chosen among them. A card can therefore never be shown because of a
hidden child while presenting an unrelated primary. Different dimensions combine with AND, values within one
dimension with OR.

* ``score_classes`` / ``min_score`` / ``max_score`` -- the frozen class and score; bounds inclusive, 0-100.
* ``statuses`` -- the stored §39 status.
* ``radar_ids`` -- this organization's Radars only; an id that is not one of them is refused, whether or not it
  exists elsewhere.
* ``signal_types`` -- a signal of that type actually contributed to the opportunity (its OpportunitySignal rows).
* ``kads`` -- exact (code, version) in the opportunity's **frozen scoring evidence**, i.e. a Radar KAD the capture
  confirmed. Never the company's current state, a prefix, a description or a crosswalk. An opportunity whose Radar
  targets no KAD carries no KAD evidence and therefore never matches a KAD filter.
* ``regions`` -- exact (level, source id) in the frozen scoring evidence, likewise.
* ``detected_from`` / ``detected_to`` -- inclusive bounds on ``latest_signal.detected_at``; timezone-aware.

Order and pagination
--------------------
Cards are ordered by their primary: score DESC, ``primary_signal_detected_at`` DESC, primary opportunity id ASC.
Because a card's primary is by definition its company's first opportunity in that order, the feed is exactly the
ordered list of primary opportunities. That makes **keyset pagination** possible in SQL: a primary is an
opportunity that no other surviving opportunity of the same company outranks, and the cursor is its ordering tuple.
No offset, no duplicated or skipped card between pages, and the organization's history is never loaded whole.
Default page size 50, maximum 200.

``assigned_to_membership_id`` (D31) restricts the opportunities to one membership's assignments in the same
SQL, before aggregation. Only ``organization_access`` passes it, for a sales user's feed.

Tenancy
-------
Every query is scoped by the explicit organization in SQL first: no user, session or current-organization context,
and no global query filtered in Python. This read model does not pass G5 on its own -- it has no route and no role
authorization, so it is not customer-accessible until the tenant authorization package verifies member isolation.

Only business identifiers leave this module: ids, the GEMI number, the Radar name (the organization's own label),
frozen scores, codes and times. Never a company name, contact detail, person, address, VAT number or payload.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime

from django.apps import apps
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .company_signals import SIGNAL_TYPES

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
REGION_LEVELS = ("prefecture", "municipality")


class FeedError(ValueError):
    """A feed request that cannot be answered as asked. Nothing is ever written, raised or not."""


@dataclass(frozen=True)
class FeedFilters:
    score_classes: tuple = ()
    statuses: tuple = ()
    radar_ids: tuple = ()
    signal_types: tuple = ()
    kads: tuple = ()          # (code, kad_version) pairs
    regions: tuple = ()       # (level, source_id) pairs
    min_score: int | None = None
    max_score: int | None = None
    detected_from: datetime | None = None
    detected_to: datetime | None = None


@dataclass(frozen=True, order=True)
class FeedCursor:
    """The ordering tuple of the last card of a page."""

    score: int
    detected_at: datetime
    opportunity_id: int

    def encode(self) -> str:
        raw = f"{self.score}|{self.detected_at.isoformat()}|{self.opportunity_id}"
        return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")

    @classmethod
    def decode(cls, value: str) -> "FeedCursor":
        try:
            score, detected, opportunity_id = base64.urlsafe_b64decode(value.encode("ascii")).decode("ascii").split("|")
            detected_at = parse_datetime(detected)
            cursor = cls(score=int(score), detected_at=detected_at, opportunity_id=int(opportunity_id))
        except (ValueError, TypeError, AttributeError, binascii.Error, UnicodeError):
            raise FeedError("invalid feed cursor") from None
        if cursor.detected_at is None or timezone.is_naive(cursor.detected_at):
            raise FeedError("invalid feed cursor")
        return cursor


@dataclass(frozen=True)
class FeedRadarOpportunity:
    """One underlying C8 opportunity of a card: which Radar found the company, and its frozen capture."""

    opportunity_id: int
    radar_id: int
    radar_name: str
    score: int
    score_class: str
    status: str
    primary_reason_code: str
    latest_signal_id: int
    latest_signal_type: str
    latest_signal_detected_at: datetime
    scored_as_of: datetime
    created_at: datetime


@dataclass(frozen=True)
class OpportunityFeedCard:
    organization_id: int
    company_id: int
    company_gemi_number: str
    # Every top-level field below comes from this one opportunity.
    primary_opportunity_id: int
    primary_radar_id: int
    score: int
    score_class: str
    status: str
    primary_reason_code: str
    scored_as_of: datetime
    created_at: datetime
    primary_signal_detected_at: datetime
    latest_company_signal_detected_at: datetime
    opportunity_count: int
    relevant_signal_count: int
    opportunities: tuple  # FeedRadarOpportunity, in feed order; the first is the primary


@dataclass(frozen=True)
class OpportunityFeedPage:
    organization_id: int
    cards: tuple
    limit: int
    next_cursor: str | None


def _model(name):
    return apps.get_model("gemiapp", name)


def _validated_filters(organization, filters: FeedFilters) -> FeedFilters:
    Opportunity, OrganizationRadar = _model("Opportunity"), _model("OrganizationRadar")
    if not isinstance(filters, FeedFilters):
        raise FeedError("filters must be a FeedFilters value")
    classes = {value for value, _ in Opportunity.SCORE_CLASSES}
    statuses = {value for value, _ in Opportunity.STATUSES}
    for name, allowed in (("score_classes", classes), ("statuses", statuses), ("signal_types", set(SIGNAL_TYPES))):
        values = getattr(filters, name)
        if not isinstance(values, tuple) or not set(values) <= allowed:
            raise FeedError(f"unknown {name}: {sorted(set(values) - allowed) if isinstance(values, tuple) else values}")
    for name in ("kads", "regions"):
        values = getattr(filters, name)
        if not isinstance(values, tuple) or not all(
                isinstance(pair, tuple) and len(pair) == 2 and all(isinstance(part, str) and part for part in pair)
                for pair in values):
            raise FeedError(f"{name} must be (str, str) pairs")
    if any(level not in REGION_LEVELS for level, _ in filters.regions):
        raise FeedError(f"a region level is one of {REGION_LEVELS}")
    for bound in (filters.min_score, filters.max_score):
        if bound is not None and (isinstance(bound, bool) or not isinstance(bound, int) or not 0 <= bound <= 100):
            raise FeedError("score bounds are whole numbers from 0 to 100")
    if filters.min_score is not None and filters.max_score is not None and filters.min_score > filters.max_score:
        raise FeedError("min_score exceeds max_score")
    for bound in (filters.detected_from, filters.detected_to):
        if bound is not None and (not isinstance(bound, datetime) or timezone.is_naive(bound)):
            raise FeedError("date bounds must be timezone-aware datetimes")
    if filters.detected_from and filters.detected_to and filters.detected_from > filters.detected_to:
        raise FeedError("detected_from is after detected_to")
    if not isinstance(filters.radar_ids, tuple) or not all(
            isinstance(pk, int) and not isinstance(pk, bool) for pk in filters.radar_ids):
        raise FeedError("radar_ids must be whole numbers")
    if filters.radar_ids:
        owned = set(OrganizationRadar.objects.filter(organization=organization, pk__in=filters.radar_ids)
                    .values_list("pk", flat=True))
        if owned != set(filters.radar_ids):
            # One message whether the id is another organization's or does not exist: nothing leaks.
            raise FeedError("a requested Radar is not one of this organization's Radars")
    return filters


def _filtered(organization, filters: FeedFilters, assigned_to_membership_id=None):
    """This organization's opportunities that survive every filter. One SQL expression, reused for the
    primary-selection subquery so a card's primary is always chosen among the survivors."""
    Opportunity, OpportunitySignal = _model("Opportunity"), _model("OpportunitySignal")
    Evidence = _model("OpportunityScoreEvidence")
    queryset = Opportunity.objects.filter(organization=organization)
    if assigned_to_membership_id is not None:
        # D31: a sales user's scope. Applied here, before aggregation, so a hidden sibling opportunity can never
        # enter a card, be chosen as its primary or be counted in its signals.
        queryset = queryset.filter(assigned_to_id=assigned_to_membership_id)
    if filters.score_classes:
        queryset = queryset.filter(score_class__in=filters.score_classes)
    if filters.statuses:
        queryset = queryset.filter(status__in=filters.statuses)
    if filters.radar_ids:
        queryset = queryset.filter(radar_id__in=filters.radar_ids)
    if filters.min_score is not None:
        queryset = queryset.filter(score__gte=filters.min_score)
    if filters.max_score is not None:
        queryset = queryset.filter(score__lte=filters.max_score)
    if filters.detected_from is not None:
        queryset = queryset.filter(latest_signal__detected_at__gte=filters.detected_from)
    if filters.detected_to is not None:
        queryset = queryset.filter(latest_signal__detected_at__lte=filters.detected_to)
    if filters.signal_types:
        queryset = queryset.filter(Exists(OpportunitySignal.objects.filter(
            opportunity=OuterRef("pk"), signal__signal_type__in=filters.signal_types)))
    if filters.kads:
        pairs = Q()
        for code, version in filters.kads:
            pairs |= Q(kad_code=code, kad_version=version)
        queryset = queryset.filter(Exists(Evidence.objects.filter(
            component__opportunity=OuterRef("pk"), kind="kad").filter(pairs)))
    if filters.regions:
        pairs = Q()
        for level, source_id in filters.regions:
            pairs |= Q(region_level=level, region_source_id=source_id)
        queryset = queryset.filter(Exists(Evidence.objects.filter(
            component__opportunity=OuterRef("pk"), kind="region").filter(pairs)))
    return queryset


def _outranked_by() -> Q:
    """Rows ranking strictly before the outer row in feed order (score DESC, detected DESC, id ASC)."""
    score, detected, pk = OuterRef("score"), OuterRef("latest_signal__detected_at"), OuterRef("pk")
    return (Q(score__gt=score) | Q(score=score, latest_signal__detected_at__gt=detected)
            | Q(score=score, latest_signal__detected_at=detected, pk__lt=pk))


def _after(cursor: FeedCursor) -> Q:
    """Rows ranking strictly after the cursor."""
    return (Q(score__lt=cursor.score)
            | Q(score=cursor.score, latest_signal__detected_at__lt=cursor.detected_at)
            | Q(score=cursor.score, latest_signal__detected_at=cursor.detected_at, pk__gt=cursor.opportunity_id))


FEED_ORDER = ("-score", "-latest_signal__detected_at", "pk")


def get_opportunity_feed(organization, filters: FeedFilters | None = None, *, limit: int = DEFAULT_PAGE_SIZE,
                         cursor: str | None = None, assigned_to_membership_id: int | None = None) -> OpportunityFeedPage:
    """One page of this organization's feed: one card per company, frozen captures only. Read-only."""
    Organization = _model("Organization")
    if not isinstance(organization, Organization) or organization.pk is None:
        raise FeedError("an explicit, saved Organization is required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE:
        raise FeedError(f"limit must be a whole number from 1 to {MAX_PAGE_SIZE}")
    filters = _validated_filters(organization, filters or FeedFilters())
    after = FeedCursor.decode(cursor) if cursor is not None else None

    if assigned_to_membership_id is not None and (
            isinstance(assigned_to_membership_id, bool) or not isinstance(assigned_to_membership_id, int)):
        raise FeedError("assigned_to_membership_id must be a membership id")
    survivors = _filtered(organization, filters, assigned_to_membership_id)
    # A primary is a survivor no other survivor of the same company outranks.
    primaries = survivors.filter(~Exists(_filtered(organization, filters, assigned_to_membership_id).filter(
        company_id=OuterRef("company_id")).filter(_outranked_by())))
    if after is not None:
        primaries = primaries.filter(_after(after))
    page = list(primaries.order_by(*FEED_ORDER)
                .values("pk", "company_id", "score", "latest_signal__detected_at")[:limit + 1])
    has_more = len(page) > limit
    page = page[:limit]
    if not page:
        return OpportunityFeedPage(organization_id=organization.pk, cards=(), limit=limit, next_cursor=None)

    company_ids = [row["company_id"] for row in page]
    children = list(
        survivors.filter(company_id__in=company_ids)
        .select_related("radar", "latest_signal", "company")
        .only("pk", "organization_id", "company_id", "company__gemi_number", "radar_id", "radar__name", "score",
              "score_class", "status", "primary_reason_code", "scored_as_of", "created_at", "latest_signal_id",
              "latest_signal__signal_type", "latest_signal__detected_at")
        .order_by(*FEED_ORDER)
    )
    signal_counts = dict(
        _model("OpportunitySignal").objects.filter(opportunity__in=survivors.filter(company_id__in=company_ids))
        .values("opportunity__company_id").annotate(n=Count("signal_id", distinct=True))
        .values_list("opportunity__company_id", "n")
    )
    by_company: dict = {}
    for row in children:
        by_company.setdefault(row.company_id, []).append(row)

    cards = []
    for entry in page:
        rows = by_company[entry["company_id"]]
        primary = rows[0]
        if primary.pk != entry["pk"]:  # the SQL primary and the Python order must agree
            raise FeedError("inconsistent primary selection")
        cards.append(OpportunityFeedCard(
            organization_id=primary.organization_id, company_id=primary.company_id,
            company_gemi_number=primary.company.gemi_number, primary_opportunity_id=primary.pk,
            primary_radar_id=primary.radar_id, score=primary.score, score_class=primary.score_class,
            status=primary.status, primary_reason_code=primary.primary_reason_code,
            scored_as_of=primary.scored_as_of, created_at=primary.created_at,
            primary_signal_detected_at=primary.latest_signal.detected_at,
            latest_company_signal_detected_at=max(row.latest_signal.detected_at for row in rows),
            opportunity_count=len(rows), relevant_signal_count=signal_counts.get(primary.company_id, 0),
            opportunities=tuple(FeedRadarOpportunity(
                opportunity_id=row.pk, radar_id=row.radar_id, radar_name=row.radar.name, score=row.score,
                score_class=row.score_class, status=row.status, primary_reason_code=row.primary_reason_code,
                latest_signal_id=row.latest_signal_id, latest_signal_type=row.latest_signal.signal_type,
                latest_signal_detected_at=row.latest_signal.detected_at, scored_as_of=row.scored_as_of,
                created_at=row.created_at,
            ) for row in rows),
        ))
    last = page[-1]
    next_cursor = (FeedCursor(score=last["score"], detected_at=last["latest_signal__detected_at"],
                              opportunity_id=last["pk"]).encode() if has_more else None)
    return OpportunityFeedPage(organization_id=organization.pk, cards=tuple(cards), limit=limit,
                               next_cursor=next_cursor)
