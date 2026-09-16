"""Monitored company refresh collector (B4).

Acquires fresh GEMI observations for the companies A9 monitors and turns each one into a B3 snapshot:

    due monitoring rows -> refresh plan -> GEMI (A1) -> validation (A2) -> normalisation (A3)
    -> snapshot (B3) -> monitoring check state (A9)

B4 stores *that* state changed. It never looks at *what* changed and never creates a Signal: change
interpretation is B5's work. Nothing here touches Radars, RadarMatch, Opportunities, leads or digests.

Not scheduled, not automatic
----------------------------
Deploying this module performs zero GEMI requests. The collector runs only when
``manage.py run_gemi_company_refresh`` is invoked or ``run_gemi_company_refresh_task`` is called
explicitly; it is deliberately absent from ``gemiapp.apps.SCHEDULES``. No feature flag is used: a flag
would add a second, weaker switch beside the real one (nothing invokes it) and could give the false
impression that setting it to 1 is what makes a scheduled run safe. Release safety is the G0/G1 gates.

Why a planner exists
--------------------
The API allows about 8 requests/minute per key and A1 holds the whole application at 7. The monitored
universe already contains thousands of companies, so one detail request per company is impossible: it
would take days and starve every other lane. B4 therefore plans before it fetches.

Strategy precedence, applied per company in this exact order:

1. **Radar search** -- a company monitored because a Radar matches it is refreshed through
   ``GET /companies`` with that Radar's criteria. One response carries up to 200 complete company
   records, so a search that covers many due targets is far cheaper than one request each.
2. **Direct detail** -- ``GET /companies/{arGemi}``, one request for one company, reserved for
   high-value reasons (ACTIVE_OPPORTUNITY, RECENT_SIGNAL) that no accepted search group covers, and
   capped per run.
3. **Defer** -- everything else, above all the NEW_COMPANY-only population, is classified
   ``deferred_no_efficient_strategy``: no request, no snapshot, no check mark, scheduling state left
   exactly as it was, so the backlog stays visible instead of being hidden by a pushed-forward
   ``next_check_at``.

A company with several reasons is not detailed merely because one of its reasons is high value: if an
accepted search group already covers it, the search covers it.

Translating a Radar into a GEMI query
-------------------------------------
Radar criteria are stored as Greek *descriptions* (prefectures, legal types) and local KAD codes, while
the API filters on *ids*. A criterion is used only when it translates exactly:

* ``activities`` -- the Radar's codes, but only when every one of them is shaped like a GEMI activity id
  (8 digits). The dev Radars hold 4-digit prefixes, which are not ids and are never sent.
* ``prefectures`` / ``legalTypes`` -- resolved through the A5 reference tables; used only if *every*
  value resolves.
* ``isActive`` -- sent when the Radar is "only active companies", which is the same predicate.
* ``name`` -- never sent. The capability report records its matching semantics as unknown, and a query
  that is narrower than intended would silently lose targets.

A criterion that cannot be translated is **dropped, never approximated**. Dropping widens the query, so
the result set stays a superset of the Radar's companies and no due target can be lost by translation;
what it costs is requests, which the efficiency gate below controls. A group with no selective criterion
left (an unrestricted registry scan) is rejected outright.

Grouping and merging
--------------------
Radars whose translated parameters are identical share one group -- the same upstream search is never
issued twice. Two groups are merged only when they differ in the ``activities`` set alone: values inside
one criterion are OR-ed upstream, and every other criterion is identical, so the merged query is exactly
the union of the two originals. Groups differing in two or more criteria are **not** merged: that would
be a cross product returning companies neither Radar wanted. Several correct searches beat one broad one.

Efficiency gate (no network)
----------------------------
Before any request the planner estimates a group's cost from *local* data: how many local companies match
the translated criteria, divided by the page size. A group is accepted only if

* the estimate fits ``max_pages_per_query``, and
* the estimate is smaller than the number of due targets in the group -- otherwise one detail request per
  company would be cheaper, and searching would waste the shared budget.

Rejected groups do not fail; their targets fall through to detail or deferral.

Search is a fetch strategy only
-------------------------------
Membership of a search response has no product meaning here. Companies returned that are not due targets
of this plan are ignored: not created, not monitored, not treated as discovery findings (A10 owns
discovery) and never matched against anything. RadarMatch, Radar criteria, monitoring reasons and leads
are never written.

Absence is not evidence
-----------------------
A due target that a *complete* search does not return is a ``search_miss``: it may no longer match the
upstream query, the local RadarMatch may be stale, or upstream semantics may have changed. It is never
marked checked, never snapshotted and never silently escalated to a detail request. In an *incomplete*
group (page cap reached before the end of the results) a missing target means even less, so the group is
recorded as incomplete and the run is partial.

Observation time
----------------
The run has one ``run_at`` -- due selection, ordering and run metadata. Each HTTP response has its own
``observed_at``, captured when that validated response was received, and A3 runs with
``as_of = local date(observed_at)``. All records carried by one response share that response's time,
because that is when they were observed. The clock is injectable so tests can prove two responses minutes
apart receive different observation times without depending on the wall clock.

Failures
--------
A2 validation failure is treated as a possible upstream contract break: the collector stops, the run
fails, and every unprocessed company stays due. Authentication failure, budget timeout, exhausted
retries and transport failures also stop the run -- partial when work had already succeeded, failed when
none had. One company that cannot be normalised, whose identifier does not match the target, or whose
observation B3 rejects as older than its snapshot span is a company-level failure: it stays due, its
neighbours in the same valid page continue, and no payload is ever logged.

Provenance
----------
A4 records the response inside the client, but ``GemiClient`` does not return the created
``GemiSourceRecord`` to its caller. Guessing it by timestamp would be a fabricated link, so B4 passes
``source_record=None`` to B3. A1/A4 are not redesigned here; exposing the association is future work.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..company_snapshots import (
    BASELINE_CREATED,
    CHANGED_CREATED,
    SnapshotChronologyError,
    record_company_snapshot,
)
from .client import SEARCH_PATH, get_gemi_client
from .errors import GemiApiError, GemiResponseValidationError
from .monitoring import (
    ACTIVE,
    ACTIVE_OPPORTUNITY,
    ACTIVE_RADAR_MATCH,
    DECAYING,
    DEFAULT_POLICY as MONITORING_POLICY,
    PRIORITIES,
    RECENT_SIGNAL,
    compute_next_check_at,
)
from .normalizer import normalize_company
from .rate_budget import GemiLane
from .schemas import ResponseFamily

logger = logging.getLogger(__name__)

# B4's lane. A1 names the monitored-refresh priority MONITORED_REFRESH; it sits below discovery and the
# digest import so a refresh can never delay the pipelines that feed customers.
REFRESH_LANE = GemiLane.MONITORED_REFRESH

# The reasons that justify spending one whole request on one company. MANUAL is excluded on purpose: A9
# documents it as service-layer support with no product workflow, so nothing may allocate requests to it.
DIRECT_DETAIL_REASONS = (ACTIVE_OPPORTUNITY, RECENT_SIGNAL)

# Planner outcomes.
STRATEGY_SEARCH = "search"
STRATEGY_DETAIL = "detail"
DEFERRED_NO_STRATEGY = "deferred_no_efficient_strategy"

# Run statuses, mirroring GemiRefreshRun.STATUSES.
RUNNING, SUCCESS, PARTIAL, FAILED = "running", "success", "partial", "failed"

# Why a run or a plan stopped early; recorded in group_stats["cap"].
CAP_REQUESTS = "max_requests_per_run"
CAP_DETAILS = "max_direct_details_per_run"

# A GEMI activity id as the API accepts it, and as the capability report evidences (e.g. "53200200").
GEMI_ACTIVITY_ID_LENGTH = 8
MAX_ERROR_MESSAGE_LENGTH = 300


class RefreshPlanError(ValueError):
    """The plan cannot be built as asked (bad policy or bad arguments). Never raised for missing data."""


@dataclass(frozen=True)
class RefreshPolicy:
    """The one place B4's caps live; every number comes from settings, none from the code below."""

    max_requests_per_run: int = 20
    max_pages_per_query: int = 5
    max_direct_details_per_run: int = 5
    page_size: int = 200

    def __post_init__(self):
        for name in ("max_requests_per_run", "max_pages_per_query", "max_direct_details_per_run", "page_size"):
            if getattr(self, name) < 1:
                raise RefreshPlanError(f"{name} must be at least 1")
        if self.page_size > 200:
            # The API rejects 201 and above with HTTP 400.
            raise RefreshPlanError("page_size must not exceed the API maximum of 200")


def policy_from_settings() -> RefreshPolicy:
    return RefreshPolicy(
        max_requests_per_run=settings.GEMI_REFRESH_MAX_REQUESTS_PER_RUN,
        max_pages_per_query=settings.GEMI_REFRESH_MAX_PAGES_PER_QUERY,
        max_direct_details_per_run=settings.GEMI_REFRESH_MAX_DIRECT_DETAILS_PER_RUN,
        page_size=settings.GEMI_REFRESH_PAGE_SIZE,
    )


# --- plan ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class RefreshTarget:
    """One due monitored company, before a strategy is chosen."""

    company_id: int
    gemi_number: str
    monitoring_id: int
    priority: str
    next_check_at: datetime
    reasons: frozenset
    radar_ids: tuple


@dataclass(frozen=True)
class SearchGroup:
    """One upstream search that covers several due targets."""

    key: str
    params: tuple  # ((name, value), ...) sorted: hashable, and the exact query minus paging
    radar_ids: tuple
    targets: tuple  # gemi numbers, in the plan's deterministic order
    estimated_pages: int

    def query(self, *, page_size: int, offset: int) -> dict[str, Any]:
        # Ordering by +arGemi is one of the four orderings the API supports and is stable across pages,
        # so offset paging cannot silently skip or repeat rows the way an unordered scan can.
        return {**dict(self.params), "resultsSortBy": "+arGemi", "resultsSize": page_size, "resultsOffset": offset}


@dataclass(frozen=True)
class DetailTarget:
    company_id: int
    gemi_number: str
    reason: str


@dataclass(frozen=True)
class DeferredTarget:
    company_id: int
    gemi_number: str
    classification: str = DEFERRED_NO_STRATEGY


@dataclass
class RefreshPlan:
    run_at: datetime
    policy: RefreshPolicy
    due_companies: int = 0
    targets: tuple = ()
    search_groups: tuple = ()
    detail_targets: tuple = ()
    deferred: tuple = ()
    rejected_groups: int = 0
    untranslatable_radars: int = 0
    truncated_by: str = ""

    @property
    def estimated_search_requests(self) -> int:
        return sum(group.estimated_pages for group in self.search_groups)

    @property
    def estimated_requests(self) -> int:
        return self.estimated_search_requests + len(self.detail_targets)

    @property
    def planned_companies(self) -> int:
        covered = {number for group in self.search_groups for number in group.targets}
        covered.update(target.gemi_number for target in self.detail_targets)
        return len(covered)

    @property
    def companies_in_multiple_groups(self) -> int:
        seen, repeated = set(), set()
        for group in self.search_groups:
            for number in group.targets:
                (repeated if number in seen else seen).add(number)
        return len(repeated)

    def summary(self) -> dict:
        return {
            "run_at": self.run_at.isoformat(),
            "due_companies": self.due_companies,
            "planned_companies": self.planned_companies,
            "search_groups": len(self.search_groups),
            "search_targets": len({number for group in self.search_groups for number in group.targets}),
            "detail_targets": len(self.detail_targets),
            "deferred": len(self.deferred),
            "rejected_groups": self.rejected_groups,
            "untranslatable_radars": self.untranslatable_radars,
            "companies_in_multiple_groups": self.companies_in_multiple_groups,
            "estimated_search_requests": self.estimated_search_requests,
            "estimated_requests": self.estimated_requests,
            "truncated_by": self.truncated_by,
        }

    def lines(self) -> list[str]:
        summary = self.summary()
        return [
            f"run_at={summary['run_at']} due={summary['due_companies']} planned={summary['planned_companies']} "
            f"deferred={summary['deferred']}",
            f"search groups={summary['search_groups']} targets={summary['search_targets']} "
            f"rejected_groups={summary['rejected_groups']} untranslatable_radars={summary['untranslatable_radars']} "
            f"companies_in_multiple_groups={summary['companies_in_multiple_groups']}",
            f"direct details={summary['detail_targets']}",
            f"estimated requests={summary['estimated_requests']} "
            f"(search={summary['estimated_search_requests']} detail={summary['detail_targets']}) "
            f"cap={self.policy.max_requests_per_run} truncated_by={summary['truncated_by'] or '-'}",
        ]


def _priority_rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)


def due_monitoring_targets(*, run_at: datetime, company_ids: Iterable[int] | None = None) -> list[RefreshTarget]:
    """The due monitored companies in the collector's deterministic order.

    Due means exactly what A9 says: a monitored (active or decaying) row whose ``next_check_at`` has
    arrived. Rows that are not yet due are never refreshed just because requests are left over.
    """
    CompanyMonitoring = apps.get_model("gemiapp", "CompanyMonitoring")
    rows = (
        CompanyMonitoring.objects
        .filter(state__in=(ACTIVE, DECAYING), next_check_at__isnull=False, next_check_at__lte=run_at)
        .select_related("company")
        .prefetch_related("reasons")
    )
    if company_ids is not None:
        rows = rows.filter(company_id__in=list(company_ids))
    targets = []
    for row in rows:
        active = {item.reason for item in row.reasons.all() if item.active}
        radar_ids = tuple(sorted(
            int(value)
            for item in row.reasons.all() if item.active and item.reason == ACTIVE_RADAR_MATCH
            for value in (item.source_ids or [])
        ))
        targets.append(RefreshTarget(
            company_id=row.company_id, gemi_number=row.company.gemi_number, monitoring_id=row.pk,
            priority=row.priority, next_check_at=row.next_check_at, reasons=frozenset(active), radar_ids=radar_ids,
        ))
    # Priority first, then the longest-waiting, then a stable identity tie-breaker. Never random.
    targets.sort(key=lambda t: (_priority_rank(t.priority), t.next_check_at, t.gemi_number, t.company_id))
    return targets


def radar_search_params(radar) -> tuple | None:
    """The Radar's criteria as GEMI search parameters, or None when no selective criterion translates.

    Only exactly-translatable criteria are included; anything else is dropped, which widens the query and
    therefore never loses a target. See "Translating a Radar into a GEMI query".
    """
    GemiPrefecture = apps.get_model("gemiapp", "GemiPrefecture")
    GemiLegalType = apps.get_model("gemiapp", "GemiLegalType")
    params: dict[str, str] = {}

    all_codes = [item.normalized_code for item in radar.activity_codes.all()]
    codes = sorted({
        code for code in all_codes if code.isdigit() and len(code) == GEMI_ACTIVITY_ID_LENGTH
    })
    # A partially translatable KAD set is dropped whole: sending only some codes would narrow the query.
    if codes and len(codes) == len(set(all_codes)):
        params["activities"] = ",".join(codes)

    for values, model, name in (
        (radar.prefectures, GemiPrefecture, "prefectures"),
        (radar.legal_types, GemiLegalType, "legalTypes"),
    ):
        if not values:
            continue
        resolved = dict(model.objects.filter(description__in=list(values)).values_list("description", "source_id"))
        if len(resolved) == len(set(values)):
            params[name] = ",".join(sorted(resolved[value] for value in set(values)))
        # Otherwise dropped: a partial id list would exclude companies the Radar wants.

    if not params:
        # Nothing selective survived; searching would scan the whole registry.
        return None
    if radar.only_active:
        params["isActive"] = "true"
    return tuple(sorted(params.items()))


def _local_candidate_count(params: Mapping[str, str]) -> int:
    """How many local companies the translated criteria describe. Local only -- the planner never asks
    GEMI how large a result set is, because that would itself cost a request."""
    from ..services import filter_companies_for_radar

    Company = apps.get_model("gemiapp", "Company")
    GemiPrefecture = apps.get_model("gemiapp", "GemiPrefecture")
    GemiLegalType = apps.get_model("gemiapp", "GemiLegalType")
    prefectures, legal_types = [], []
    if params.get("prefectures"):
        prefectures = list(GemiPrefecture.objects.filter(
            source_id__in=params["prefectures"].split(",")).values_list("description", flat=True))
    if params.get("legalTypes"):
        legal_types = list(GemiLegalType.objects.filter(
            source_id__in=params["legalTypes"].split(",")).values_list("description", flat=True))
    queryset = filter_companies_for_radar(
        Company.objects.all(), prefectures=prefectures, legal_types=legal_types,
        only_active=params.get("isActive") == "true",
        activity_codes=params["activities"].split(",") if params.get("activities") else None,
    )
    return queryset.count()


def _merge_activity_groups(groups: dict) -> dict:
    """Merge groups that differ only in their ``activities`` value set.

    Values inside one criterion are OR-ed upstream and every other criterion is identical, so the merged
    query is exactly the union of the originals. Groups differing in two or more criteria stay separate:
    merging those would be a cross product returning companies neither Radar asked for.
    """
    buckets: dict[tuple, list] = {}
    for params, entry in groups.items():
        rest = tuple(sorted((name, value) for name, value in params if name != "activities"))
        buckets.setdefault(rest, []).append((params, entry))
    merged: dict[tuple, dict] = {}
    for rest, members in buckets.items():
        if len(members) == 1 or not all(dict(params).get("activities") for params, _ in members):
            # A bucket where some member has no activities criterion already contains the broadest query
            # in it; merging would not reduce requests and could only widen it further.
            for params, entry in members:
                merged[params] = entry
            continue
        codes = sorted({code for params, _ in members for code in dict(params)["activities"].split(",")})
        params = tuple(sorted({**dict(rest), "activities": ",".join(codes)}.items()))
        combined = {"radar_ids": set(), "targets": []}
        for _, entry in members:
            combined["radar_ids"].update(entry["radar_ids"])
            combined["targets"].extend(entry["targets"])
        merged[params] = combined
    return merged


def _group_key(params: Sequence) -> str:
    return json.dumps([list(item) for item in params], ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def build_company_refresh_plan(
    *, run_at: datetime, policy: RefreshPolicy | None = None, company_ids: Iterable[int] | None = None,
) -> RefreshPlan:
    """The complete refresh plan for one run. Deterministic and network-free: it reads monitoring, Radars
    and local companies, and asks GEMI nothing."""
    if not isinstance(run_at, datetime):
        raise RefreshPlanError("run_at must be a timezone-aware datetime")
    if timezone.is_naive(run_at):
        raise RefreshPlanError("run_at must be timezone-aware")
    policy = policy or policy_from_settings()

    targets = due_monitoring_targets(run_at=run_at, company_ids=company_ids)
    plan = RefreshPlan(run_at=run_at, policy=policy, due_companies=len(targets), targets=tuple(targets))
    if not targets:
        return plan

    from ..services import eligible_radars

    wanted_radar_ids = {radar_id for target in targets for radar_id in target.radar_ids}
    radars = {radar.pk: radar for radar in eligible_radars() if radar.pk in wanted_radar_ids}

    groups: dict[tuple, dict] = {}
    radar_group: dict[int, tuple] = {}
    untranslatable = set()
    for radar_id in sorted(radars):
        params = radar_search_params(radars[radar_id])
        if params is None:
            untranslatable.add(radar_id)
            continue
        entry = groups.setdefault(params, {"radar_ids": set(), "targets": []})
        entry["radar_ids"].add(radar_id)
        radar_group[radar_id] = params
    # Plan order is preserved inside every group, and a company covered by two Radars in the same group
    # appears once.
    for target in targets:
        for params in dict.fromkeys(radar_group[rid] for rid in target.radar_ids if rid in radar_group):
            groups[params]["targets"].append(target.gemi_number)
    plan.untranslatable_radars = len(untranslatable)

    merged = _merge_activity_groups({params: entry for params, entry in groups.items() if entry["targets"]})

    accepted, rejected = [], 0
    for params in sorted(merged, key=_group_key):
        entry = merged[params]
        ordered = tuple(dict.fromkeys(entry["targets"]))
        estimated_pages = max(1, math.ceil(_local_candidate_count(dict(params)) / policy.page_size))
        # Reject a group that cannot be paged within the cap, or that would cost at least as many requests
        # as simply detailing each of its targets.
        if estimated_pages > policy.max_pages_per_query or estimated_pages >= len(ordered):
            rejected += 1
            continue
        accepted.append(SearchGroup(
            key=_group_key(params), params=params, radar_ids=tuple(sorted(entry["radar_ids"])),
            targets=ordered, estimated_pages=estimated_pages,
        ))
    plan.rejected_groups = rejected

    covered = {number for group in accepted for number in group.targets}
    details, deferred = [], []
    for target in targets:
        if target.gemi_number in covered:
            continue
        reason = next((item for item in DIRECT_DETAIL_REASONS if item in target.reasons), None)
        if reason is None:
            deferred.append(DeferredTarget(company_id=target.company_id, gemi_number=target.gemi_number))
        else:
            details.append(DetailTarget(company_id=target.company_id, gemi_number=target.gemi_number, reason=reason))

    truncated = ""
    if len(details) > policy.max_direct_details_per_run:
        details, truncated = details[:policy.max_direct_details_per_run], CAP_DETAILS

    # The overall request cap, applied in plan order: whole search groups first, then details.
    budget, kept_groups = policy.max_requests_per_run, []
    for group in accepted:
        if group.estimated_pages > budget:
            truncated = truncated or CAP_REQUESTS
            continue
        kept_groups.append(group)
        budget -= group.estimated_pages
    if len(details) > budget:
        details, truncated = details[:max(0, budget)], truncated or CAP_REQUESTS

    plan.search_groups = tuple(kept_groups)
    plan.detail_targets = tuple(details)
    plan.deferred = tuple(deferred)
    plan.truncated_by = truncated
    return plan


# --- execution ----------------------------------------------------------------------------------

@dataclass
class RefreshResult:
    run_at: datetime
    status: str = RUNNING
    run_id: int | None = None
    due_companies: int = 0
    planned_companies: int = 0
    search_groups: int = 0
    deferred_no_strategy: int = 0
    request_count: int = 0
    search_requests: int = 0
    detail_requests: int = 0
    records_examined: int = 0
    target_records_observed: int = 0
    baselines_created: int = 0
    changed_snapshots_created: int = 0
    unchanged_snapshots: int = 0
    search_misses: int = 0
    duplicate_observations_avoided: int = 0
    company_failures: int = 0
    incomplete_groups: int = 0
    group_stats: dict = field(default_factory=dict)
    error_message: str = ""

    def summary(self) -> dict:
        result = {}
        for item in fields(self):
            value = getattr(self, item.name)
            result[item.name] = value.isoformat() if isinstance(value, datetime) else value
        return result

    def lines(self) -> list[str]:
        return [
            f"status={self.status} run_at={self.run_at.isoformat()} due={self.due_companies} "
            f"planned={self.planned_companies} deferred={self.deferred_no_strategy}",
            f"requests={self.request_count} (search={self.search_requests} detail={self.detail_requests}) "
            f"groups={self.search_groups} incomplete={self.incomplete_groups} "
            f"records_examined={self.records_examined} targets_observed={self.target_records_observed}",
            f"snapshots baseline={self.baselines_created} changed={self.changed_snapshots_created} "
            f"unchanged={self.unchanged_snapshots}",
            f"search_misses={self.search_misses} duplicates_avoided={self.duplicate_observations_avoided} "
            f"company_failures={self.company_failures}",
        ]


class _Stop(Exception):
    """A systemic stop: either a cap was reached or the upstream failed. Never a company's own problem."""

    def __init__(self, message: str, *, systemic: bool):
        super().__init__(message)
        self.message = message
        self.systemic = systemic


def _safe_message(exc: Exception) -> str:
    # A1 already keeps the API key out of its messages; never add a response body or company data.
    return f"{type(exc).__name__}: {exc}"[:MAX_ERROR_MESSAGE_LENGTH]


class _Executor:
    def __init__(self, plan: RefreshPlan, *, client, clock: Callable[[], datetime], result: RefreshResult):
        self.plan = plan
        self.client = client
        self.clock = clock
        self.result = result
        self.companies = {}
        self.processed: set[str] = set()

    # -- helpers
    def company_for(self, gemi_number: str):
        if gemi_number not in self.companies:
            Company = apps.get_model("gemiapp", "Company")
            self.companies[gemi_number] = Company.objects.filter(gemi_number=gemi_number).first()
        return self.companies[gemi_number]

    def observed_at(self) -> datetime:
        value = self.clock()
        if timezone.is_naive(value):
            raise _Stop("the refresh clock returned a naive datetime", systemic=True)
        return value

    def call(self, what: str, fn, *args, **kwargs):
        """One logical GEMI request. A1 owns transport retries, so this counts calls, not attempts."""
        if self.result.request_count >= self.plan.policy.max_requests_per_run:
            raise _Stop(CAP_REQUESTS, systemic=False)
        self.result.request_count += 1
        if what == STRATEGY_SEARCH:
            self.result.search_requests += 1
        else:
            self.result.detail_requests += 1
        try:
            return fn(*args, **kwargs)
        except GemiResponseValidationError as exc:
            # A possible upstream contract break: fail closed rather than process more pages.
            raise _Stop(_safe_message(exc), systemic=True) from exc
        except GemiApiError as exc:
            raise _Stop(_safe_message(exc), systemic=True) from exc

    # -- one company
    def process(self, gemi_number: str, record: Mapping[str, Any], observed_at: datetime) -> None:
        if gemi_number in self.processed:
            self.result.duplicate_observations_avoided += 1
            return
        company = self.company_for(gemi_number)
        if company is None:
            # A monitored company always exists locally; if it vanished, this is a company-level anomaly.
            self.processed.add(gemi_number)
            self.result.company_failures += 1
            logger.warning("Refresh: no local company for monitored target %s.", gemi_number)
            return
        try:
            normalized = normalize_company(record, as_of=timezone.localdate(observed_at))
            if normalized.ar_gemi != gemi_number:
                raise ValueError(f"response carries {normalized.ar_gemi}, expected {gemi_number}")
            outcome = record_company_snapshot(company, normalized, observed_at)
        except SnapshotChronologyError as exc:
            self.processed.add(gemi_number)
            self.result.company_failures += 1
            logger.warning("Refresh: chronology refused for company %s: %s", company.pk, exc)
            return
        except Exception as exc:  # normalisation, identity or row-specific storage failure
            self.processed.add(gemi_number)
            self.result.company_failures += 1
            logger.warning("Refresh: company %s (%s) failed: %s", company.pk, gemi_number, _safe_message(exc))
            return

        self.processed.add(gemi_number)
        self.result.target_records_observed += 1
        if outcome.status == BASELINE_CREATED:
            self.result.baselines_created += 1
        elif outcome.status == CHANGED_CREATED:
            self.result.changed_snapshots_created += 1
        else:
            self.result.unchanged_snapshots += 1
        _record_success(company, observed_at)

    # -- strategies
    def run_search_group(self, group: SearchGroup) -> None:
        remaining = set(group.targets)
        pages, complete = 0, False
        try:
            while remaining and pages < self.plan.policy.max_pages_per_query:
                payload = self.call(
                    STRATEGY_SEARCH, self.client.search_companies,
                    group.query(page_size=self.plan.policy.page_size, offset=pages * self.plan.policy.page_size),
                    lane=REFRESH_LANE,
                )
                pages += 1
                records = payload.get("searchResults") or []
                observed_at = self.observed_at()
                self.result.records_examined += len(records)
                for item in records:
                    number = str(item.get("arGemi") or "").strip()
                    # A search returns many companies that are not ours: ignore every one of them. No
                    # Company is created, no monitoring changed, nothing treated as a discovery finding.
                    if number in remaining:
                        remaining.discard(number)
                        self.process(number, item, observed_at)
                total = int((payload.get("searchMetadata") or {}).get("totalCount") or 0)
                if len(records) < self.plan.policy.page_size or (total and pages * self.plan.policy.page_size >= total):
                    complete = True
                    break
            else:
                complete = complete or not remaining
        finally:
            self._record_group(group, pages=pages, remaining=remaining, complete=complete)

    def _record_group(self, group: SearchGroup, *, pages: int, remaining: set, complete: bool) -> None:
        if remaining and not complete:
            self.result.incomplete_groups += 1
        elif remaining:
            # A complete search that did not return the target: its absence is recorded, never acted on.
            self.result.search_misses += len(remaining)
        self.result.group_stats.setdefault("groups", []).append({
            "key": group.key, "pages": pages, "targets": len(group.targets),
            "found": len(group.targets) - len(remaining), "complete": complete,
        })

    def run_detail(self, target: DetailTarget) -> None:
        payload = self.call(
            STRATEGY_DETAIL, self.client.get, f"{SEARCH_PATH}/{target.gemi_number}",
            lane=REFRESH_LANE, family=ResponseFamily.COMPANY_DETAIL,
        )
        observed_at = self.observed_at()
        self.result.records_examined += 1
        if not isinstance(payload, Mapping):
            self.result.company_failures += 1
            return
        number = str(payload.get("arGemi") or "").strip()
        if number != target.gemi_number:
            # Never snapshot company Y under company X.
            self.result.company_failures += 1
            logger.warning("Refresh: detail for %s returned a different company.", target.gemi_number)
            return
        self.process(number, payload, observed_at)


def _record_success(company, observed_at: datetime) -> None:
    """Company lifecycle (A6) and monitoring check state (A9) after one proven fresh observation.

    Only the two lifecycle timestamps a successful observation genuinely evidences are written.
    ``last_synced_at`` is deliberately untouched: B4 does not cut over canonical company persistence, and
    claiming a sync that did not happen would mislead every later consumer. No customer-visible legacy
    field (is_active, status, descriptions, raw_data, activity rows, search_name) is written either.
    """
    Company = apps.get_model("gemiapp", "Company")
    CompanyMonitoring = apps.get_model("gemiapp", "CompanyMonitoring")
    with transaction.atomic():
        updates = {}
        if company.first_seen_at is None or observed_at < company.first_seen_at:
            # Never moved later: the earliest evidence wins, exactly as A6 defines it.
            updates["first_seen_at"] = observed_at
        if company.last_seen_at is None or observed_at > company.last_seen_at:
            updates["last_seen_at"] = observed_at
        if updates:
            # queryset.update() so Company.updated_at (auto_now) and save() side effects stay untouched.
            Company.objects.filter(pk=company.pk).update(**updates)
            for name, value in updates.items():
                setattr(company, name, value)

        row = CompanyMonitoring.objects.select_for_update().filter(company_id=company.pk).first()
        if row is None or row.state not in (ACTIVE, DECAYING):
            return
        row.last_checked_at = observed_at
        row.last_success_at = observed_at
        row.consecutive_failures = 0
        # The one central cadence calculation: A9's own helper, never a copy of its constants.
        row.next_check_at = compute_next_check_at(
            MONITORING_POLICY, priority=row.priority, company_id=company.pk,
            monitored_since=row.monitored_since, last_checked_at=observed_at,
        )
        row.save(update_fields=[
            "last_checked_at", "last_success_at", "consecutive_failures", "next_check_at", "updated_at",
        ])


def run_company_refresh(
    *, run_at: datetime | None = None, policy: RefreshPolicy | None = None, client=None,
    company_ids: Iterable[int] | None = None, plan: RefreshPlan | None = None,
    clock: Callable[[], datetime] | None = None,
) -> RefreshResult:
    """Execute one refresh run: plan, fetch, validate, normalise, snapshot, update monitoring.

    Emits no Signal and interprets no change. See the module docstring.
    """
    run_at = timezone.now() if run_at is None else run_at
    if timezone.is_naive(run_at):
        raise RefreshPlanError("run_at must be timezone-aware")
    if plan is None:
        plan = build_company_refresh_plan(
            run_at=run_at, policy=policy or policy_from_settings(), company_ids=company_ids)
    # One source of truth: a plan carries the caps it was built with, and execution honours exactly those.
    policy = plan.policy
    clock = clock or timezone.now
    client = client or get_gemi_client()

    GemiRefreshRun = apps.get_model("gemiapp", "GemiRefreshRun")
    result = RefreshResult(
        run_at=run_at, due_companies=plan.due_companies, planned_companies=plan.planned_companies,
        search_groups=len(plan.search_groups), deferred_no_strategy=len(plan.deferred),
    )
    run = GemiRefreshRun.objects.create(
        status=RUNNING, run_at=run_at, due_companies=plan.due_companies,
        planned_companies=plan.planned_companies, search_groups=len(plan.search_groups),
        deferred_no_strategy=len(plan.deferred),
    )
    result.run_id = run.pk
    executor = _Executor(plan, client=client, clock=clock, result=result)
    stopped = None
    try:
        for group in plan.search_groups:
            executor.run_search_group(group)
        for target in plan.detail_targets:
            executor.run_detail(target)
    except _Stop as exc:
        stopped = exc
        if exc.systemic:
            result.error_message = exc.message
        else:
            result.group_stats["cap"] = exc.message

    if stopped is not None and stopped.systemic:
        # Work already stored stays true; nothing unprocessed is marked checked either way.
        result.status = PARTIAL if result.target_records_observed else FAILED
    elif (stopped is not None or result.incomplete_groups or result.search_misses or result.company_failures
            or plan.truncated_by or result.request_count >= policy.max_requests_per_run):
        result.status = PARTIAL
    else:
        # Deferral is a planning outcome, not an execution failure, so it never makes a run partial.
        result.status = SUCCESS
    if plan.truncated_by:
        result.group_stats.setdefault("cap", plan.truncated_by)

    for name in (
        "due_companies", "planned_companies", "search_groups", "deferred_no_strategy", "request_count",
        "search_requests", "detail_requests", "records_examined", "target_records_observed",
        "baselines_created", "changed_snapshots_created", "unchanged_snapshots", "search_misses",
        "duplicate_observations_avoided", "company_failures", "incomplete_groups", "group_stats",
        "error_message",
    ):
        setattr(run, name, getattr(result, name))
    run.status = result.status
    run.finished_at = timezone.now()
    run.save()
    logger.info(
        "Company refresh %s: %s due, %s planned, %s requests, %s observations (%s baseline, %s changed).",
        result.status, result.due_companies, result.planned_companies, result.request_count,
        result.target_records_observed, result.baselines_created, result.changed_snapshots_created,
    )
    return result
