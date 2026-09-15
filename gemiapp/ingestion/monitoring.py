"""Company monitoring universe and refresh policy (A9).

Decides which companies Gemi Leads 2.0 should monitor, why, how important each one is, and when it is next
eligible for a refresh. Nothing here calls GEMI, refreshes a company or changes anything customer-facing;
nothing reads these tables yet.

Data model
----------
``CompanyMonitoring`` holds one row per company, shared by every customer: the state (active, decaying,
inactive), the effective priority and primary reason, ``next_check_at``, and the collector state a future
refresh collector will write.

``CompanyMonitoringReason`` holds one row per (monitoring, reason), so a company can have several reasons
at once. Each row keeps its history (``first_active_at``, ``activated_at``, ``deactivated_at``,
``activation_count``), an optional ``expires_at`` and ``source_ids`` (e.g. the matching Radar ids).

Reasons
-------
NEW_COMPANY (derived)
    Active while the company was first observed by Gemi Leads (A6 ``Company.first_seen_at``) less than
    ``new_company_window`` before ``as_of``, and was itself new when first observed: its source
    incorporation date is VALID (A6 ``incorporation_date_quality``) and no more than ``new_company_window``
    earlier than that first observation. The second condition keeps a historical bulk import -- on the
    development data, 16,563 companies first observed on 2026-08-19 but incorporated since May -- from
    counting as new. Expires at ``first_seen_at + new_company_window``. Without A6 first-seen evidence
    the company is not new; the importer does not write that field yet (A6), so run
    ``backfill_gemi_company_metadata`` before a recompute until ingestion does.

ACTIVE_RADAR_MATCH (derived)
    Active while at least one Radar the live matcher uses matches the company. The Radars are those of
    ``gemiapp.services.eligible_radars`` (active, not deleted, not muted, owner entitled). The match is
    ``company_matches_radar`` with the production activity semantics (GEMI_MATCH_CURRENT_ACTIVITIES_ONLY,
    off: legacy), restricted, as in the matcher, to companies incorporated on or after the Radar's
    ``monitor_from`` date. ``source_ids`` lists the matching Radar ids. Deactivated when no such Radar
    matches.

ACTIVE_OPPORTUNITY (reserved, not populated)
    UserCompanyLead is not an opportunity. Leads are created automatically by the matcher and by viewing a
    company, their status is a customer's free label, and nothing records a stage, owner or outcome. Reserved
    until an Opportunity model exists.

RECENT_SIGNAL (reserved, not populated)
    Signals do not exist yet.

MANUAL (external)
    No product workflow exists, so there is no UI. ``set_manual_monitoring`` supports it at the service
    layer, optionally with ``expires_at``.

The recompute derives only NEW_COMPANY and ACTIVE_RADAR_MATCH. It respects reasons set through
``set_external_reason`` (MANUAL and, for future producers, ACTIVE_OPPORTUNITY / RECENT_SIGNAL), deactivating
them only when they expire.

Priority (``MonitoringPolicy``)
-------------------------------
Each reason has a tier: ACTIVE_OPPORTUNITY critical; RECENT_SIGNAL high; ACTIVE_RADAR_MATCH high;
NEW_COMPANY normal; MANUAL normal. The effective priority is the highest tier among the active reasons, and
``primary_reason`` is the first active reason in that same order. MANUAL does not override: a manual request
guarantees monitoring at normal priority at least, and no product requirement justifies more. A decaying
company is low.

Cadence and decay
-----------------
Refresh intervals: critical 1 day, high 3 days, normal 7 days, low 30 days.

When the last active reason ends, the company decays: it stays monitored at low priority for
``decay_grace`` (30 days), then becomes inactive (no priority, no ``next_check_at``) while keeping its
reason history. A reason returning during the grace period resumes the same monitored period; after it,
a new period starts. This avoids refresh thrashing, keeps recent context, and stops irrelevant companies
from staying active forever.

``next_check_at``
-----------------
Scheduling eligibility, timezone-aware, a pure function of the priority, the company id and a stable anchor,
so an unchanged company keeps the same value on every recompute:

* never checked: ``monitored_since + interval x j``, which spreads first checks over one interval instead of
  a thundering herd;
* checked: ``last_checked_at + interval + interval x checked_jitter_fraction x j``.

``j`` in [0, 1) is derived from a SHA-256 of the company id -- reproducible, never random. The value moves
only when the policy requires it: a priority change, a new monitored period, or a collector check. The
recompute never writes the collector fields and never pretends a refresh happened.

Recompute
---------
``recompute_company_monitoring`` takes one ``as_of`` (default: now) used everywhere. It derives the evidence
for the whole universe, or for ``company_ids`` only (targeted, e.g. after a Radar change). It then walks the
companies with evidence or an existing monitoring row in primary-key batches, one transaction per batch,
creating, activating, deactivating, decaying and rescheduling. It writes only rows that change, so a second
run on unchanged data changes nothing. ``dry_run`` computes the same report without writing. Output is
counts only.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from typing import Iterable

from django.apps import apps
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

NEW_COMPANY = "new_company"
ACTIVE_RADAR_MATCH = "active_radar_match"
ACTIVE_OPPORTUNITY = "active_opportunity"
RECENT_SIGNAL = "recent_signal"
MANUAL = "manual"
REASONS = (ACTIVE_OPPORTUNITY, RECENT_SIGNAL, ACTIVE_RADAR_MATCH, NEW_COMPANY, MANUAL)
DERIVED_REASONS = (NEW_COMPANY, ACTIVE_RADAR_MATCH)
EXTERNAL_REASONS = (ACTIVE_OPPORTUNITY, RECENT_SIGNAL, MANUAL)
RESERVED_REASONS = (ACTIVE_OPPORTUNITY, RECENT_SIGNAL)

CRITICAL = "critical"
HIGH = "high"
NORMAL = "normal"
LOW = "low"
PRIORITIES = (CRITICAL, HIGH, NORMAL, LOW)

ACTIVE = "active"
DECAYING = "decaying"
INACTIVE = "inactive"
STATES = (ACTIVE, DECAYING, INACTIVE)

MONITORING_FIELDS = (
    "state", "priority", "primary_reason", "policy_version", "monitored_since", "decay_started_at", "inactive_since",
    "next_check_at",
)
REASON_FIELDS = ("active", "first_active_at", "activated_at", "deactivated_at", "activation_count", "expires_at", "source_ids")


@dataclass(frozen=True)
class MonitoringPolicy:
    """The one place for monitoring priorities, cadence and windows."""

    version: int = 1
    # Reasons in order of strength, each with its tier. The order also picks the primary reason.
    reason_tiers: tuple = (
        (ACTIVE_OPPORTUNITY, CRITICAL),
        (RECENT_SIGNAL, HIGH),
        (ACTIVE_RADAR_MATCH, HIGH),
        (NEW_COMPANY, NORMAL),
        (MANUAL, NORMAL),
    )
    decay_priority: str = LOW
    intervals: tuple = (
        (CRITICAL, timedelta(days=1)),
        (HIGH, timedelta(days=3)),
        (NORMAL, timedelta(days=7)),
        (LOW, timedelta(days=30)),
    )
    checked_jitter_fraction: float = 0.1
    new_company_window: timedelta = timedelta(days=30)
    decay_grace: timedelta = timedelta(days=30)

    def priority_for(self, reasons: Iterable[str]) -> str | None:
        tiers = dict(self.reason_tiers)
        found = [tiers[reason] for reason in reasons]
        return min(found, key=PRIORITIES.index) if found else None

    def primary_reason(self, reasons: Iterable[str]) -> str | None:
        wanted = set(reasons)
        return next((reason for reason, _ in self.reason_tiers if reason in wanted), None)

    def interval(self, priority: str) -> timedelta:
        return dict(self.intervals)[priority]


DEFAULT_POLICY = MonitoringPolicy()


def check_jitter_fraction(company_id: int) -> float:
    """A reproducible value in [0, 1) for one company."""
    digest = hashlib.sha256(f"gemi-leads-monitoring:{company_id}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def compute_next_check_at(policy: MonitoringPolicy, *, priority: str, company_id: int, monitored_since: datetime,
                          last_checked_at: datetime | None) -> datetime:
    interval = policy.interval(priority)
    fraction = check_jitter_fraction(company_id)
    if last_checked_at is None:
        return monitored_since + interval * fraction
    return last_checked_at + interval + interval * policy.checked_jitter_fraction * fraction


# --- evidence -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class ReasonEvidence:
    expires_at: datetime | None = None
    source_ids: tuple = ()


def new_company_evidence(*, as_of: datetime, policy: MonitoringPolicy = DEFAULT_POLICY, company_ids=None) -> dict[int, ReasonEvidence]:
    Company = apps.get_model("gemiapp", "Company")
    companies = Company.objects.filter(
        first_seen_at__isnull=False, first_seen_at__lte=as_of, first_seen_at__gt=as_of - policy.new_company_window,
        incorporation_date_quality="valid",
    )
    if company_ids is not None:
        companies = companies.filter(pk__in=company_ids)
    evidence = {}
    for pk, first_seen_at, incorporated in companies.values_list("pk", "first_seen_at", "incorporation_date").iterator(chunk_size=5000):
        if incorporated >= timezone.localdate(first_seen_at) - policy.new_company_window:
            evidence[pk] = ReasonEvidence(expires_at=first_seen_at + policy.new_company_window)
    return evidence


def radar_match_evidence(*, company_ids=None) -> dict[int, ReasonEvidence]:
    """Companies matched by the Radars the live matcher uses, with the production matcher."""
    from ..services import company_matches_radar, eligible_radars, filter_companies_for_radar

    Company = apps.get_model("gemiapp", "Company")
    matches: dict[int, list[int]] = defaultdict(list)
    for radar in eligible_radars():
        # Narrowed in the database; the name criterion and the final decision are the matcher's own.
        candidates = filter_companies_for_radar(
            Company.objects.filter(incorporation_date__gte=timezone.localdate(radar.monitor_from)),
            prefectures=radar.prefectures, legal_types=radar.legal_types, only_active=radar.only_active,
            activity_codes=[item.normalized_code for item in radar.activity_codes.all()],
        )
        if company_ids is not None:
            candidates = candidates.filter(pk__in=company_ids)
        for company in candidates.order_by("pk").prefetch_related("activity_records").iterator(chunk_size=2000):
            if company_matches_radar(company, radar)[0]:
                matches[company.pk].append(radar.pk)
    return {pk: ReasonEvidence(source_ids=tuple(sorted(ids))) for pk, ids in matches.items()}


# --- recompute ----------------------------------------------------------------------------------

@dataclass
class MonitoringReport:
    dry_run: bool
    as_of: datetime
    scope: str
    companies_in_scope: int = 0
    companies_inspected: int = 0
    batches: int = 0
    last_company_id: int | None = None
    monitored_created: int = 0
    entered_monitoring: int = 0
    priority_changed: int = 0
    next_check_changed: int = 0
    decayed: int = 0
    unmonitored: int = 0
    unchanged: int = 0
    reasons_activated: Counter = field(default_factory=Counter)
    reasons_deactivated: Counter = field(default_factory=Counter)
    active_reasons: Counter = field(default_factory=Counter)
    states: Counter = field(default_factory=Counter)
    priorities: Counter = field(default_factory=Counter)
    next_check: Counter = field(default_factory=Counter)
    companies_with_active_reasons: int = 0
    companies_with_multiple_reasons: int = 0

    @property
    def companies_without_reason(self) -> int:
        return self.companies_in_scope - self.companies_with_active_reasons

    def summary(self) -> dict:
        result = {}
        for item in fields(self):
            value = getattr(self, item.name)
            result[item.name] = dict(value) if isinstance(value, Counter) else (value.isoformat() if isinstance(value, datetime) else value)
        result["companies_without_reason"] = self.companies_without_reason
        return result

    def lines(self) -> list[str]:
        p = "[dry-run] " if self.dry_run else ""

        def counts(counter, keys):
            return " ".join(f"{key}={counter[key]}" for key in keys)

        reserved = " ".join(f"({reason} reserved: not populated)" for reason in RESERVED_REASONS)
        return [
            f"{p}as_of={self.as_of.isoformat()} scope={self.scope} companies_in_scope={self.companies_in_scope} "
            f"inspected={self.companies_inspected} batches={self.batches} last_company_id={self.last_company_id}",
            f"{p}monitoring rows created={self.monitored_created} entered_monitoring={self.entered_monitoring} "
            f"priority_changed={self.priority_changed} next_check_changed={self.next_check_changed} decayed={self.decayed} "
            f"unmonitored={self.unmonitored} unchanged={self.unchanged}",
            f"{p}reasons activated {counts(self.reasons_activated, REASONS)}",
            f"{p}reasons deactivated {counts(self.reasons_deactivated, REASONS)}",
            f"{p}active reasons {counts(self.active_reasons, REASONS)} {reserved}",
            f"{p}states {counts(self.states, STATES)} companies_with_multiple_reasons={self.companies_with_multiple_reasons} "
            f"companies_without_reason={self.companies_without_reason}",
            f"{p}priority {counts(self.priorities, PRIORITIES)}",
            f"{p}next check from as_of {counts(self.next_check, ('due_now', 'within_1d', 'within_7d', 'within_30d', 'later'))}",
            f"{p}radar-derived monitored={self.active_reasons[ACTIVE_RADAR_MATCH]} new-company monitored={self.active_reasons[NEW_COMPANY]}",
        ]


class _Batch:
    def __init__(self):
        self.new_rows: list = []  # (CompanyMonitoring, [CompanyMonitoringReason])
        self.updated_rows: dict = {}
        self.new_reasons: list = []
        self.updated_reasons: dict = {}


def _set(obj, values, bucket) -> bool:
    changed = [name for name, value in values.items() if getattr(obj, name) != value]
    for name in changed:
        setattr(obj, name, values[name])
    if changed and obj.pk is not None:
        bucket[obj.pk] = obj
    return bool(changed)


def _next_check_bucket(value, as_of) -> str:
    if value <= as_of:
        return "due_now"
    delta = value - as_of
    if delta <= timedelta(days=1):
        return "within_1d"
    if delta <= timedelta(days=7):
        return "within_7d"
    if delta <= timedelta(days=30):
        return "within_30d"
    return "later"


def _plan_company(company_id, row, derived, *, as_of, policy, batch, report) -> None:
    CompanyMonitoring = apps.get_model("gemiapp", "CompanyMonitoring")
    CompanyMonitoringReason = apps.get_model("gemiapp", "CompanyMonitoringReason")
    reasons = {item.reason: item for item in row.reasons.all()} if row is not None else {}
    if row is None and not derived:
        return
    new_reasons = []

    for reason in DERIVED_REASONS:
        evidence = derived.get(reason)
        existing = reasons.get(reason)
        if evidence is not None:
            context = {"expires_at": evidence.expires_at, "source_ids": list(evidence.source_ids)}
            if existing is None:
                item = CompanyMonitoringReason(
                    reason=reason, active=True, first_active_at=as_of, activated_at=as_of, deactivated_at=None,
                    activation_count=1, **context,
                )
                new_reasons.append(item)
                reasons[reason] = item
                report.reasons_activated[reason] += 1
            else:
                if not existing.active:
                    context.update(active=True, activated_at=as_of, deactivated_at=None, activation_count=existing.activation_count + 1)
                    report.reasons_activated[reason] += 1
                _set(existing, context, batch.updated_reasons)
        elif existing is not None and existing.active:
            _set(existing, {"active": False, "deactivated_at": as_of}, batch.updated_reasons)
            report.reasons_deactivated[reason] += 1

    for reason, existing in reasons.items():
        if reason not in DERIVED_REASONS and existing.active and existing.expires_at is not None and existing.expires_at <= as_of:
            _set(existing, {"active": False, "deactivated_at": existing.expires_at}, batch.updated_reasons)
            report.reasons_deactivated[reason] += 1

    active = {reason for reason, item in reasons.items() if item.active}
    current = {name: getattr(row, name) for name in MONITORING_FIELDS} if row is not None else None
    values = dict(current) if current is not None else {}
    last_checked_at = row.last_checked_at if row is not None else None

    if active:
        if row is None or row.state == INACTIVE:
            values.update(monitored_since=as_of, inactive_since=None)
            report.entered_monitoring += 1
        values.update(
            state=ACTIVE, priority=policy.priority_for(active), primary_reason=policy.primary_reason(active),
            decay_started_at=None,
        )
    elif row.state == ACTIVE:
        values.update(state=DECAYING, priority=policy.decay_priority, primary_reason=None, decay_started_at=as_of)
        report.decayed += 1
    elif row.state == DECAYING and as_of >= row.decay_started_at + policy.decay_grace:
        values.update(state=INACTIVE, priority=None, primary_reason=None, inactive_since=as_of)
        report.unmonitored += 1

    values["policy_version"] = policy.version
    if values["state"] == INACTIVE:
        values["next_check_at"] = None
    else:
        values["next_check_at"] = compute_next_check_at(
            policy, priority=values["priority"], company_id=company_id, monitored_since=values["monitored_since"],
            last_checked_at=last_checked_at,
        )

    if row is None:
        values.setdefault("decay_started_at", None)
        values.setdefault("inactive_since", None)
        created = CompanyMonitoring(company_id=company_id, **values)
        batch.new_rows.append((created, new_reasons))
        report.monitored_created += 1
        final = created
    else:
        report.priority_changed += int(values["priority"] != current["priority"])
        report.next_check_changed += int(values["next_check_at"] != current["next_check_at"])
        row_changed = _set(row, values, batch.updated_rows)
        for item in new_reasons:
            item.monitoring = row
        batch.new_reasons.extend(new_reasons)
        reason_changed = bool(new_reasons) or any(item.pk in batch.updated_reasons for item in reasons.values() if item.pk)
        report.unchanged += int(not row_changed and not reason_changed)
        final = row

    report.states[final.state] += 1
    if final.state != INACTIVE:
        report.priorities[final.priority] += 1
        report.next_check[_next_check_bucket(final.next_check_at, as_of)] += 1
    for reason in active:
        report.active_reasons[reason] += 1
    report.companies_with_active_reasons += int(bool(active))
    report.companies_with_multiple_reasons += int(len(active) > 1)


def _write(batch: _Batch) -> None:
    CompanyMonitoring = apps.get_model("gemiapp", "CompanyMonitoring")
    CompanyMonitoringReason = apps.get_model("gemiapp", "CompanyMonitoringReason")
    now = timezone.now()
    with transaction.atomic():
        if batch.new_rows:
            CompanyMonitoring.objects.bulk_create([row for row, _ in batch.new_rows])
            for row, reasons in batch.new_rows:
                for item in reasons:
                    item.monitoring = row
                    batch.new_reasons.append(item)
        if batch.updated_rows:
            for row in batch.updated_rows.values():
                row.updated_at = now
            CompanyMonitoring.objects.bulk_update(list(batch.updated_rows.values()), [*MONITORING_FIELDS, "updated_at"])
        if batch.new_reasons:
            CompanyMonitoringReason.objects.bulk_create(batch.new_reasons)
        if batch.updated_reasons:
            for item in batch.updated_reasons.values():
                item.updated_at = now
            CompanyMonitoringReason.objects.bulk_update(list(batch.updated_reasons.values()), [*REASON_FIELDS, "updated_at"])


def recompute_company_monitoring(
    *, as_of: datetime | None = None, company_ids: Iterable[int] | None = None, batch_size: int = 1000,
    start_company_id: int = 0, dry_run: bool = False, policy: MonitoringPolicy = DEFAULT_POLICY,
) -> MonitoringReport:
    """Recompute monitoring for every company, or only ``company_ids``. See the module docstring."""
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    as_of = timezone.now() if as_of is None else as_of
    if timezone.is_naive(as_of):
        raise ValueError("as_of must be timezone-aware")
    Company = apps.get_model("gemiapp", "Company")
    CompanyMonitoring = apps.get_model("gemiapp", "CompanyMonitoring")

    if company_ids is not None:
        wanted = set(Company.objects.filter(pk__in=list(company_ids)).values_list("pk", flat=True))
        report = MonitoringReport(dry_run=dry_run, as_of=as_of, scope="targeted", companies_in_scope=len(wanted))
        scope_ids = sorted(wanted)
    else:
        report = MonitoringReport(
            dry_run=dry_run, as_of=as_of, scope="full",
            companies_in_scope=Company.objects.filter(pk__gt=start_company_id).count(),
        )
        scope_ids = None

    evidence: dict[int, dict[str, ReasonEvidence]] = defaultdict(dict)
    for pk, item in new_company_evidence(as_of=as_of, policy=policy, company_ids=scope_ids).items():
        evidence[pk][NEW_COMPANY] = item
    for pk, item in radar_match_evidence(company_ids=scope_ids).items():
        evidence[pk][ACTIVE_RADAR_MATCH] = item

    existing = CompanyMonitoring.objects.all()
    if scope_ids is not None:
        existing = existing.filter(company_id__in=scope_ids)
    universe = set(evidence) | set(existing.values_list("company_id", flat=True))
    universe = sorted(pk for pk in universe if pk > start_company_id) if scope_ids is None else scope_ids

    for start in range(0, len(universe), batch_size):
        ids = universe[start:start + batch_size]
        rows = {row.company_id: row for row in CompanyMonitoring.objects.filter(company_id__in=ids).prefetch_related("reasons")}
        batch = _Batch()
        for company_id in ids:
            report.companies_inspected += 1
            _plan_company(company_id, rows.get(company_id), evidence.get(company_id, {}), as_of=as_of, policy=policy, batch=batch, report=report)
        if not dry_run:
            _write(batch)
        report.batches += 1
        report.last_company_id = ids[-1]
    logger.info(
        "Company monitoring recompute%s (%s): %s inspected, %s created, %s decayed, %s unmonitored.",
        " (dry run)" if dry_run else "", report.scope, report.companies_inspected, report.monitored_created,
        report.decayed, report.unmonitored,
    )
    return report


def set_external_reason(
    company_id: int, reason: str, *, active: bool = True, as_of: datetime | None = None,
    expires_at: datetime | None = None, source_ids: Iterable[int] = (), policy: MonitoringPolicy = DEFAULT_POLICY,
) -> MonitoringReport:
    """Activate or deactivate a reason the recompute does not derive (MANUAL; ACTIVE_OPPORTUNITY and
    RECENT_SIGNAL are for future producers), then recompute that company."""
    if reason not in EXTERNAL_REASONS:
        raise ValueError(f"{reason} is derived by the recompute and cannot be set externally.")
    as_of = timezone.now() if as_of is None else as_of
    if timezone.is_naive(as_of):
        raise ValueError("as_of must be timezone-aware")
    CompanyMonitoring = apps.get_model("gemiapp", "CompanyMonitoring")
    CompanyMonitoringReason = apps.get_model("gemiapp", "CompanyMonitoringReason")
    with transaction.atomic():
        row = CompanyMonitoring.objects.select_for_update().filter(company_id=company_id).first()
        if row is None and active:
            row = CompanyMonitoring.objects.create(
                company_id=company_id, state=ACTIVE, priority=policy.priority_for([reason]),
                primary_reason=reason, policy_version=policy.version, monitored_since=as_of,
                next_check_at=compute_next_check_at(policy, priority=policy.priority_for([reason]), company_id=company_id, monitored_since=as_of, last_checked_at=None),
            )
        if row is not None:
            item = CompanyMonitoringReason.objects.filter(monitoring=row, reason=reason).first()
            context = {"expires_at": expires_at, "source_ids": sorted(source_ids)}
            if item is None and active:
                CompanyMonitoringReason.objects.create(
                    monitoring=row, reason=reason, active=True, first_active_at=as_of, activated_at=as_of,
                    activation_count=1, **context,
                )
            elif item is not None:
                if active and not item.active:
                    context.update(active=True, activated_at=as_of, deactivated_at=None, activation_count=item.activation_count + 1)
                elif not active and item.active:
                    context.update(active=False, deactivated_at=as_of)
                for name, value in context.items():
                    setattr(item, name, value)
                item.save()
    return recompute_company_monitoring(as_of=as_of, company_ids=[company_id], policy=policy)


def set_manual_monitoring(company_id: int, *, active: bool = True, as_of: datetime | None = None,
                          expires_at: datetime | None = None) -> MonitoringReport:
    return set_external_reason(company_id, MANUAL, active=active, as_of=as_of, expires_at=expires_at)
